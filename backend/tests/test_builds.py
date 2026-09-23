import json
import tarfile
import time
from pathlib import Path

import pytest

from layersmith import config
from layersmith.backends.base import BuildError, ImageInfo
from layersmith.core import containerfile as cf
from layersmith.core.spec import validate_spec
from layersmith.models import (
    Blob, Build, Project, create_all, make_engine, make_session_factory, next_build_number,
)
from layersmith.services.builds import BuildService, sha256_file

DIGEST = "sha256:" + "a" * 64


class FakeBackend:
    """Records what a real backend would have been asked to do."""

    name = "fake"

    def __init__(self, fail_on: str | None = None):
        self.fail_on = fail_on
        self.calls: list[tuple] = []

    def available(self):
        return True, "fake"

    def resolve_base(self, reference, architecture, on_log):
        self.calls.append(("resolve_base", reference, architecture))
        if self.fail_on == "resolve_base":
            raise BuildError("no such image")
        on_log(f"Pulling {reference}")
        return DIGEST

    def build(self, context_dir, image_ref, architecture, on_log):
        self.calls.append(("build", str(context_dir), image_ref, architecture))
        if self.fail_on == "build":
            raise BuildError("RUN dnf install failed")
        self.context = Path(context_dir)
        self.containerfile = (self.context / "Containerfile").read_text()
        on_log("STEP 1/2 FROM ...")
        on_log("STEP 2/2 RUN ...")
        return ImageInfo(reference=image_ref, image_id="sha256:" + "b" * 64, digest="sha256:" + "c" * 64,
                         size=123_456_789)

    def inspect(self, image_ref):
        return ImageInfo(reference=image_ref, image_id="sha256:" + "b" * 64)

    def export(self, image_ref, destination, on_log):
        self.calls.append(("export", image_ref, str(destination)))
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"oci-archive-content")
        on_log("Exported")
        return Path(destination).stat().st_size

    def remove(self, image_ref):
        self.calls.append(("remove", image_ref))


@pytest.fixture
def env(tmp_path):
    settings = config.reset_for_tests(
        data_dir=tmp_path, image_dir=tmp_path / "images", build_dir=tmp_path / "builds",
        upload_dir=tmp_path / "uploads", log_dir=tmp_path / "logs", tmp_dir=tmp_path / "tmp",
        database_url=f"sqlite:///{tmp_path}/test.db", keep_build_contexts=True,
    )
    settings.prepare()
    engine = make_engine(settings.database_url)
    create_all(engine)
    factory = make_session_factory(engine)
    backend = FakeBackend()
    service = BuildService(factory, backend, settings=settings)
    return {"settings": settings, "factory": factory, "backend": backend, "service": service}


def make_build(env, spec_overrides=None, mode="gui", containerfile_text=""):
    spec = validate_spec({"base": {"distribution": "AlmaLinux", "version": "10"},
                          "presets": ["Basic Linux"], **(spec_overrides or {})})
    with env["factory"]() as session:
        project = Project(name="network-tools", repository="layersmith/network-tools", spec=spec, mode=mode,
                          base_image=spec["base"]["source"], containerfile=containerfile_text)
        session.add(project)
        session.flush()
        build = Build(project_id=project.id, number=next_build_number(session), version="1.0.0", mode=mode, spec=spec,
                      base_image=spec["base"]["source"], architecture="amd64",
                      image_ref="layersmith/network-tools:1.0.0", containerfile=containerfile_text)
        session.add(build)
        session.commit()
        return build.id


def test_successful_build_records_everything_needed_to_explain_it(env):
    build_id = make_build(env)
    env["service"].run(build_id)

    with env["factory"]() as session:
        build = session.get(Build, build_id)
        assert build.status == "ready" and build.error is None
        assert build.base_digest == DIGEST
        assert f"FROM docker.io/library/almalinux@{DIGEST}" in build.containerfile
        assert "git" in build.packages
        assert build.image_id and build.image_digest and build.image_size == 123_456_789
        assert build.export_sha256 == sha256_file(Path(build.export_path))
        assert build.started_at and build.finished_at and build.duration_seconds >= 0
        assert "STEP 1/2" in build.log


def test_build_streams_log_lines_to_subscribers(env):
    build_id = make_build(env)
    channel = env["service"].logs.subscribe(build_id)
    env["service"].run(build_id)

    messages = []
    while not channel.empty():
        messages.append(channel.get_nowait())
    lines = [m["line"] for m in messages if m["type"] == "log"]
    statuses = [m["status"] for m in messages if m["type"] in ("status", "done")]
    assert any("STEP 1/2" in line for line in lines)
    assert statuses[-1] == "ready"
    assert ["pulling", "building", "exporting"] == [s for s in statuses if s in ("pulling", "building", "exporting")]


def test_failed_build_is_recorded_not_raised(env):
    env["service"].backend = FakeBackend(fail_on="build")
    build_id = make_build(env)
    env["service"].run(build_id)

    with env["factory"]() as session:
        build = session.get(Build, build_id)
        assert build.status == "failed"
        assert "dnf install failed" in build.error
        assert build.finished_at is not None


def test_advanced_mode_builds_the_users_own_containerfile(env):
    text = "FROM alpine:3.22\nRUN apk add --no-cache git\n"
    build_id = make_build(env, mode="advanced", containerfile_text=text)
    env["service"].run(build_id)

    with env["factory"]() as session:
        build = session.get(Build, build_id)
        assert build.status == "ready"
        assert build.containerfile == text  # never regenerated
    assert env["service"].backend.containerfile == text


def test_context_contains_scripts_and_verified_uploads(env):
    payload = b"hello from an uploaded file"
    checksum = __import__("hashlib").sha256(payload).hexdigest()
    (Path(env["settings"].upload_dir) / checksum).write_bytes(payload)
    with env["factory"]() as session:
        session.add(Blob(sha256=checksum, filename="config.yml", size=len(payload)))
        session.commit()

    build_id = make_build(env, {"files": [{"sha256": checksum, "destination": "/opt/app/config.yml"}],
                                "scripts": {"post_install": "echo done"}})
    env["service"].run(build_id)

    context = env["service"].context_dir(build_id)
    assert (context / "files" / checksum).read_bytes() == payload
    assert (context / "scripts" / "post-install.sh").read_text().startswith("#!/bin/sh")
    assert (context / "scripts" / "post-install.sh").stat().st_mode & 0o111


def test_tampered_upload_fails_the_build(env):
    checksum = "d" * 64
    (Path(env["settings"].upload_dir) / checksum).write_bytes(b"not what was uploaded")
    build_id = make_build(env, {"files": [{"sha256": checksum, "destination": "/opt/app/x"}]})
    env["service"].run(build_id)

    with env["factory"]() as session:
        assert "checksum" in session.get(Build, build_id).error


def test_airgap_bundle_is_self_contained_and_verifiable(env, tmp_path):
    build_id = make_build(env)
    env["service"].run(build_id)
    bundle = env["service"].airgap_bundle(build_id)

    extracted = tmp_path / "extracted"
    with tarfile.open(bundle) as archive:
        names = archive.getnames()
        archive.extractall(extracted)
    root = extracted / "layersmith-network-tools-1.0.0"

    assert any(n.endswith("image/layersmith-network-tools-1.0.0.tar") for n in names)
    assert (root / "INSTALL.txt").read_text().count("podman load") == 1
    assert (root / "source" / "Containerfile").read_text().startswith("# Generated by LayerSmith")

    manifest = json.loads((root / "metadata" / "manifest.json").read_text())
    assert manifest["base_digest"] == DIGEST and manifest["version"] == "1.0.0"
    assert manifest["packages"] and manifest["layersmith_version"] == config.VERSION

    # every checksum in SHA256SUMS matches the extracted file
    for line in (root / "SHA256SUMS").read_text().splitlines():
        expected, name = line.split("  ", 1)
        assert sha256_file(root / name) == expected, name

    with env["factory"]() as session:
        build = session.get(Build, build_id)
        assert build.airgap_sha256 == sha256_file(bundle) and build.airgap_size > 0


def test_airgap_requires_a_finished_build(env):
    build_id = make_build(env)
    with pytest.raises(BuildError):
        env["service"].airgap_bundle(build_id)


def test_queued_build_runs_in_the_background_worker(env):
    """enqueue() must actually start the worker and finish without help."""
    build_id = make_build(env)
    env["service"].enqueue(build_id)

    deadline = time.time() + 30
    while time.time() < deadline:
        with env["factory"]() as session:
            status = session.get(Build, build_id).status
        if status in ("ready", "failed"):
            break
        time.sleep(0.1)
    assert status == "ready"
