"""Split download of an image archive and the generated assemble-image.sh.

The script tests run the real generated script with bash against parts
downloaded through the API, so they cover the whole path a user takes.
"""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from layersmith.models import Build
from layersmith.services import splitting
from tests.test_api import client, run_pending_build  # noqa: F401 - fixture

PART = splitting.MIN_PART_SIZE
BASH = shutil.which("bash")


def make_ready_build(client, size: int) -> str:
    """A ready build whose export is `size` bytes of non-repeating data."""
    client.state["settings"].split_part_bytes = PART
    project = client.post("/api/projects", json={
        "name": "gpu-benchmark", "repository": "localhost/gpu-benchmark", "template": "Minimal",
        "spec": {"base": {"distribution": "Alpine", "version": "3.22"}}}).json()
    build_id = client.post(f"/api/projects/{project['id']}/builds", json={}).json()["id"]
    run_pending_build(client, build_id)
    with client.state["session_factory"]() as session:
        build = session.get(Build, build_id)
        data = os.urandom(size)
        Path(build.export_path).write_bytes(data)
        build.export_size, build.export_sha256 = size, hashlib.sha256(data).hexdigest()
        session.commit()
    return build_id


def split(client, build_id) -> dict:
    client.state["split_service"].run(build_id)
    return client.get(f"/api/builds/{build_id}/split").json()


def download_all(client, build_id, status, target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    for item in status["files"]:
        response = client.get(f"/api/builds/{build_id}/split/{item['name']}")
        assert response.status_code == 200, item
        (target / item["name"]).write_bytes(response.content)
    return target


def assemble(directory: Path, *args, env=None):
    return subprocess.run([BASH, str(directory / splitting.SCRIPT_NAME), *args], cwd=directory,
                          capture_output=True, text=True, env=env, timeout=120)


def original(client, build_id) -> bytes:
    with client.state["session_factory"]() as session:
        return Path(session.get(Build, build_id).export_path).read_bytes()


# ---------------------------------------------------------------- planning

def test_plan_covers_every_byte_with_parts_no_larger_than_the_limit():
    assert splitting.plan(0, 10) == [(0, 0)]
    assert splitting.plan(10, 10) == [(0, 10)]
    assert splitting.plan(25, 10) == [(0, 10), (10, 10), (20, 5)]
    total = 4_300_000_000
    parts = splitting.plan(total, splitting.PART_SIZE)
    assert len(parts) == 5 and sum(length for _, length in parts) == total
    assert all(length <= splitting.PART_SIZE < 1_000_000_000 for _, length in parts)


def test_part_names_sort_in_order_and_widen_only_when_needed():
    assert splitting.part_name("a.tar", 0, 6) == "a.tar.part-000"
    assert splitting.part_name("a.tar", 5, 6) == "a.tar.part-005"
    assert splitting.part_name("a.tar", 1000, 1001) == "a.tar.part-1000"
    assert splitting.part_name("a.tar", 7, 1001) == "a.tar.part-0007"


def test_configured_part_size_can_never_exceed_900_mb(client):
    settings = client.state["settings"]
    settings.split_part_bytes = 5_000_000_000
    assert splitting.part_size(settings) == splitting.PART_SIZE
    settings.split_part_bytes = 1
    assert splitting.part_size(settings) == splitting.MIN_PART_SIZE


# ------------------------------------------------------------------- API

def test_status_before_splitting_shows_the_plan(client):
    build_id = make_ready_build(client, int(2.5 * PART))
    status = client.get(f"/api/builds/{build_id}/split").json()
    assert status["state"] == "none" and status["available"]
    assert status["planned_parts"] == 3 and status["part_size"] == PART
    assert status["archive"] == "gpu-benchmark-1.0.0.tar" and status["files"] == []
    assert "split-archive" in client.get("/api/health").json()["api_features"]


def test_split_lists_parts_checksums_and_script_and_writes_no_files(client):
    build_id = make_ready_build(client, int(2.5 * PART))
    image_dir = Path(client.state["settings"].image_dir)
    before = sorted(image_dir.iterdir())
    status = split(client, build_id)
    assert status["state"] == "ready"
    names = [item["name"] for item in status["files"]]
    assert names == ["gpu-benchmark-1.0.0.tar.part-000", "gpu-benchmark-1.0.0.tar.part-001",
                     "gpu-benchmark-1.0.0.tar.part-002", "assemble-image.sh",
                     "gpu-benchmark-1.0.0.tar.sha256", "gpu-benchmark-1.0.0.parts.sha256"]
    assert [item["size"] for item in status["files"][:3]] == [PART, PART, PART // 2]
    # Parts are byte ranges of the export, not copies of it.
    assert sorted(image_dir.iterdir()) == before

    data = original(client, build_id)
    joined = b"".join(client.get(f"/api/builds/{build_id}/split/{name}").content for name in names[:3])
    assert joined == data
    for item in status["files"]:
        response = client.get(f"/api/builds/{build_id}/split/{item['name']}")
        assert len(response.content) == item["size"] < 1_000_000_000
        assert f'filename="{item["name"]}"' in response.headers["content-disposition"]
    sums = client.get(f"/api/builds/{build_id}/split/gpu-benchmark-1.0.0.parts.sha256").text
    assert sums.splitlines()[0] == f"{status['files'][0]['sha256']}  gpu-benchmark-1.0.0.tar.part-000"
    # The full TAR download is untouched.
    assert client.get(f"/api/builds/{build_id}/download/export").content == data


def test_split_is_idempotent_and_only_listed_names_are_served(client):
    build_id = make_ready_build(client, PART + 10)
    first = split(client, build_id)
    again = client.post(f"/api/builds/{build_id}/split").json()
    assert again["state"] == "ready" and again["files"] == first["files"]
    for name in ("../layersmith.db", "gpu-benchmark-1.0.0.tar", "gpu-benchmark-1.0.0.tar.part-009", "x"):
        assert client.get(f"/api/builds/{build_id}/split/{name}").status_code == 404


def test_changed_archive_invalidates_the_manifest(client):
    build_id = make_ready_build(client, PART + 10)
    split(client, build_id)
    with client.state["session_factory"]() as session:
        Path(session.get(Build, build_id).export_path).write_bytes(b"replaced")
    status = client.get(f"/api/builds/{build_id}/split").json()
    assert status["state"] == "none"
    assert client.get(f"/api/builds/{build_id}/split/assemble-image.sh").status_code == 404


def test_archive_that_no_longer_matches_its_checksum_is_refused(client):
    build_id = make_ready_build(client, PART + 10)
    with client.state["session_factory"]() as session:
        session.get(Build, build_id).export_sha256 = "0" * 64
        session.commit()
    status = split(client, build_id)
    assert status["state"] == "failed" and "no longer matches" in status["error"]
    with client.state["session_factory"]() as session:
        assert session.get(Build, build_id).export_parts is None


def test_split_needs_a_ready_build_with_an_export(client):
    build_id = make_ready_build(client, 10)
    with client.state["session_factory"]() as session:
        Path(session.get(Build, build_id).export_path).unlink()
    response = client.post(f"/api/builds/{build_id}/split")
    assert response.status_code == 409 and "no longer on disk" in response.json()["detail"]
    assert client.post("/api/builds/nope/split").status_code == 404


def test_progress_is_reported_while_the_checksums_run(client):
    build_id = make_ready_build(client, 3 * PART)
    service = client.state["split_service"]
    seen = []
    original_progress = service._progress

    def record(job_id, done):
        original_progress(job_id, done)
        with client.state["session_factory"]() as session:
            seen.append(service.status(session.get(Build, job_id)))

    service._progress = record
    service.run(build_id)
    assert seen and all(entry["state"] == "running" for entry in seen)
    assert seen[-1]["progress"] == {"done": 3 * PART, "total": 3 * PART}


def test_interrupted_split_leaves_the_build_unchanged_and_can_be_retried(client):
    build_id = make_ready_build(client, 3 * PART)
    service = client.state["split_service"]
    original_progress = service._progress

    def interrupt(job_id, done):
        if done > PART:
            raise OSError("read error: device went away")
        original_progress(job_id, done)

    service._progress = interrupt
    service.run(build_id)
    status = client.get(f"/api/builds/{build_id}/split").json()
    assert status["state"] == "failed" and "device went away" in status["error"]
    with client.state["session_factory"]() as session:
        assert session.get(Build, build_id).export_parts is None

    service._progress = original_progress
    assert split(client, build_id)["state"] == "ready"


# ------------------------------------------------------- assemble-image.sh

needs_shell = pytest.mark.skipif(BASH is None or shutil.which("sha256sum") is None,
                                       reason="needs bash and sha256sum")


@needs_shell
def test_script_restores_a_byte_identical_archive(client, tmp_path):
    build_id = make_ready_build(client, int(2.5 * PART))
    directory = download_all(client, build_id, split(client, build_id), tmp_path / "transfer")
    result = assemble(directory)
    assert result.returncode == 0, result.stdout + result.stderr
    for line in ("Checking archive parts... OK", "Verifying checksums... OK", "Reassembling image archive... OK",
                 "Verifying original archive... OK", "Image archive restored successfully!",
                 "podman load -i gpu-benchmark-1.0.0.tar"):
        assert line in result.stdout
    assert (directory / "gpu-benchmark-1.0.0.tar").read_bytes() == original(client, build_id)
    assert not list(directory.glob("*.partial*"))
    # Re-running finds the restored archive and verifies it instead of redoing it.
    again = assemble(directory)
    assert again.returncode == 0 and "already restored" in again.stdout
    # The checksum files work with plain sha256sum -c as well.
    for name in ("gpu-benchmark-1.0.0.parts.sha256", "gpu-benchmark-1.0.0.tar.sha256"):
        assert subprocess.run(["sha256sum", "-c", name], cwd=directory, capture_output=True).returncode == 0


@needs_shell
def test_script_reports_missing_and_incomplete_parts(client, tmp_path):
    build_id = make_ready_build(client, int(2.5 * PART))
    directory = download_all(client, build_id, split(client, build_id), tmp_path / "transfer")
    (directory / "gpu-benchmark-1.0.0.tar.part-001").unlink()
    with open(directory / "gpu-benchmark-1.0.0.tar.part-002", "r+b") as handle:
        handle.truncate(100)
    result = assemble(directory)
    assert result.returncode == 2
    assert "missing: gpu-benchmark-1.0.0.tar.part-001" in result.stderr
    assert "incomplete: gpu-benchmark-1.0.0.tar.part-002" in result.stderr
    assert not (directory / "gpu-benchmark-1.0.0.tar").exists()


@needs_shell
def test_script_reports_corrupt_parts_and_writes_nothing(client, tmp_path):
    build_id = make_ready_build(client, int(2.5 * PART))
    directory = download_all(client, build_id, split(client, build_id), tmp_path / "transfer")
    with open(directory / "gpu-benchmark-1.0.0.tar.part-000", "r+b") as handle:
        handle.seek(1234)
        byte = handle.read(1)
        handle.seek(1234)
        handle.write(bytes([byte[0] ^ 0xFF]))
    result = assemble(directory)
    assert result.returncode == 3 and "corrupt: gpu-benchmark-1.0.0.tar.part-000" in result.stderr
    assert sorted(path.name for path in directory.iterdir()) == sorted(
        item["name"] for item in client.get(f"/api/builds/{build_id}/split").json()["files"])


@needs_shell
def test_script_refuses_a_different_existing_archive_unless_forced(client, tmp_path):
    build_id = make_ready_build(client, PART + 10)
    directory = download_all(client, build_id, split(client, build_id), tmp_path / "transfer")
    (directory / "gpu-benchmark-1.0.0.tar").write_bytes(b"something else")
    result = assemble(directory)
    assert result.returncode == 5 and "--force" in result.stderr
    forced = assemble(directory, "--force", "--remove-parts")
    assert forced.returncode == 0, forced.stderr
    assert (directory / "gpu-benchmark-1.0.0.tar").read_bytes() == original(client, build_id)
    assert not list(directory.glob("*.part-*"))


@needs_shell
def test_script_load_runs_podman_load_and_dir_option_works(client, tmp_path):
    build_id = make_ready_build(client, PART + 10)
    directory = download_all(client, build_id, split(client, build_id), tmp_path / "transfer")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "podman").write_text(f'#!/bin/sh\necho "$@" > {tmp_path}/podman-args\n')
    (fake_bin / "podman").chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    script = tmp_path / "elsewhere" / splitting.SCRIPT_NAME
    script.parent.mkdir()
    shutil.move(directory / splitting.SCRIPT_NAME, script)
    result = subprocess.run([BASH, str(script), "--dir", str(directory), "--load"], capture_output=True,
                            text=True, env=env, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "podman-args").read_text().strip() == "load -i gpu-benchmark-1.0.0.tar"


@needs_shell
def test_script_help_and_unknown_options(client, tmp_path):
    build_id = make_ready_build(client, 10)
    directory = download_all(client, build_id, split(client, build_id), tmp_path / "transfer")
    help_text = assemble(directory, "--help")
    assert help_text.returncode == 0 and "--remove-parts" in help_text.stdout and "set -u" not in help_text.stdout
    assert assemble(directory, "--bogus").returncode == 1
    assert (directory / "gpu-benchmark-1.0.0.tar").exists() is False
