"""The CLI against the real LayerSmith API over HTTP (fake build runtime).

Skipped automatically where the server package is not installed. These
tests prove the client uses the same API and validation as the web UI: what
the CLI creates is what /api/projects returns to the browser.
"""

import hashlib
import json
import shutil
from pathlib import Path

import httpx
import pytest

from layersmith_client import cli, ops
from layersmith_client.api import ApiClient

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def run(server, argv, capsys):
    code = cli.main(["--server", server.url, *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def create_example(server, capsys, tmp_path, name="alpine-tools", extra=()):
    shutil.copy(EXAMPLES / "alpine-tools.json", tmp_path / "spec.json")
    shutil.copy(EXAMPLES / "motd.txt", tmp_path / "motd.txt")
    code, out, err = run(server, ["--json", "projects", "create", "--spec", str(tmp_path / "spec.json"),
                                  "--name", name, *extra], capsys)
    assert code == 0, err
    return json.loads(out), err


def test_project_created_in_the_terminal_is_the_one_the_web_ui_sees(server, capsys, tmp_path):
    project, err = create_example(server, capsys, tmp_path)
    assert "Uploaded motd.txt" in err  # progress on stderr, result on stdout
    web_view = httpx.get(f"{server.url}/api/projects/{project['id']}").json()
    assert web_view["name"] == "alpine-tools" and web_view["mode"] == "gui"
    motd = hashlib.sha256((EXAMPLES / "motd.txt").read_bytes()).hexdigest()
    assert web_view["spec"]["files"][0]["sha256"] == motd
    # The file reached the server by upload, not as a path on this machine.
    assert (server.data_dir / "uploads" / motd).is_file()
    assert str(tmp_path) not in json.dumps(web_view)

    code, out, _ = run(server, ["--json", "projects", "list"], capsys)
    assert [p["name"] for p in json.loads(out)] == ["alpine-tools"]


def test_server_validation_is_the_validation(server, capsys, tmp_path):
    code, _, err = run(server, ["projects", "create", "--name", "Bad Name", "--distribution", "Alpine",
                                "--distro-version", "3.22"], capsys)
    assert code == 4 and "Invalid project name" in err
    code, _, err = run(server, ["projects", "create", "--name", "ok", "--distribution", "Alpine",
                                "--distro-version", "9.99"], capsys)
    assert code == 4 and "Unsupported Alpine version" in err
    code, _, err = run(server, ["projects", "create", "--name", "ok", "--distribution", "Alpine",
                                "--distro-version", "3.22", "--env", "API_TOKEN=x"], capsys)
    assert code == 4 and "looks like a secret" in err


def test_build_wait_reconnect_logs_export_and_bundle(server, capsys, tmp_path):
    project, _ = create_example(server, capsys, tmp_path)
    # Accepted is not finished: without --wait only the id comes back.
    code, out, err = run(server, ["build", "alpine-tools", "--version", "1.0.0"], capsys)
    build_id = out.strip()
    assert code == 0 and "accepted" in err

    # A new client process attaches to the same build by id.
    code, out, err = run(server, ["--json", "builds", "wait", build_id, "--logs"], capsys)
    records = [json.loads(line) for line in out.splitlines()]  # JSON Lines, one record each
    assert code == 0 and any(r["type"] == "log" for r in records) and any(r["type"] == "status" for r in records)
    assert records[-1]["type"] == "done" and records[-1]["build"]["status"] == "ready"

    code, out, _ = run(server, ["builds", "logs", build_id], capsys)
    assert "STEP 1/2" in out and "Image ready" in out

    # Versions are immutable, whichever interface asks.
    code, _, err = run(server, ["build", "alpine-tools", "--version", "1.0.0"], capsys)
    assert code == 4 and "never overwritten" in err

    download = tmp_path / "client-side"
    download.mkdir()
    code, out, err = run(server, ["--json", "export", build_id, "--output", str(download / "image.tar")], capsys)
    result = json.loads(out)
    assert code == 0 and result["verified"]
    assert hashlib.sha256((download / "image.tar").read_bytes()).hexdigest() == result["sha256"]
    build = httpx.get(f"{server.url}/api/builds/{build_id}").json()
    assert result["sha256"] == build["export_sha256"]

    code, _, err = run(server, ["export", build_id, "--output", str(download / "image.tar")], capsys)
    assert code == 8 and "already exists" in err

    code, _, err = run(server, ["bundle", "export", build_id, "--output", str(download), "--no-create"], capsys)
    assert code == 8 and "no air-gap bundle" in err
    code, out, err = run(server, ["--json", "bundle", "export", build_id, "--output", str(download)], capsys)
    bundle = json.loads(out)
    assert code == 0 and bundle["verified"] and bundle["path"].endswith("-airgap.tar.gz")
    assert Path(bundle["path"]).parent == download.resolve()


def test_a_failed_build_fails_the_wait(server, capsys, tmp_path):
    server.state["build_service"].backend.fail_on = "build"
    create_example(server, capsys, tmp_path)
    code, out, err = run(server, ["build", "alpine-tools", "--wait"], capsys)
    assert code == 9 and "RUN dnf install failed" in err


def test_export_that_is_gone_on_the_server_is_not_offered(server, capsys, tmp_path):
    create_example(server, capsys, tmp_path)
    code, out, _ = run(server, ["--json", "build", "alpine-tools", "--wait"], capsys)
    build_id = json.loads(out.splitlines()[-1])["build"]["id"]
    for archive in (server.data_dir / "images").glob("*.tar"):
        archive.unlink()
    code, _, err = run(server, ["export", build_id, "--output", str(tmp_path / "x.tar")], capsys)
    assert code == 8 and "no image archive" in err and not (tmp_path / "x.tar").exists()


def test_missing_scanner_is_not_reported_as_clean(server, capsys, tmp_path):
    create_example(server, capsys, tmp_path)
    code, out, _ = run(server, ["--json", "build", "alpine-tools", "--wait"], capsys)
    build_id = json.loads(out.splitlines()[-1])["build"]["id"]
    code, out, _ = run(server, ["scans", "list", build_id], capsys)
    assert code == 0 and "NOT SCANNED" in out and "not the same as it being clean" in out
    code, _, err = run(server, ["scan", build_id], capsys)
    assert code == 4  # the server refuses: no scanner configured


def test_import_clone_update_and_delete(server, capsys, tmp_path):
    containerfile = tmp_path / "Containerfile"
    containerfile.write_text("FROM docker.io/library/alpine:3.22\nRUN echo hi\n")
    code, out, _ = run(server, ["--json", "projects", "import", "--name", "imported", "--containerfile",
                                str(containerfile)], capsys)
    assert code == 0 and json.loads(out)["mode"] == "advanced"

    containerfile.write_text("FROM docker.io/library/alpine:3.21\n")
    code, _, _ = run(server, ["projects", "update", "imported", "--containerfile", str(containerfile)], capsys)
    code, out, _ = run(server, ["projects", "containerfile", "imported"], capsys)
    assert "alpine:3.21" in out

    code, _, _ = run(server, ["projects", "clone", "imported", "--name", "imported-copy"], capsys)
    assert code == 0
    code, _, _ = run(server, ["projects", "delete", "imported-copy", "--yes"], capsys)
    names = [p["name"] for p in httpx.get(f"{server.url}/api/projects").json()]
    assert names == ["imported"]


def test_export_spec_recreates_the_same_definition(server, capsys, tmp_path):
    project, _ = create_example(server, capsys, tmp_path)
    spec_file = tmp_path / "exported.json"
    code, _, _ = run(server, ["projects", "export-spec", "alpine-tools", "--output", str(spec_file)], capsys)
    code, out, _ = run(server, ["--json", "projects", "create", "--spec", str(spec_file), "--name", "again"], capsys)
    again = json.loads(out)
    original = httpx.get(f"{server.url}/api/projects/{project['id']}").json()
    assert again["spec"] == original["spec"]
    copy = httpx.get(f"{server.url}/api/projects/{again['id']}").json()
    assert copy["containerfile"].replace("again", "alpine-tools") == original["containerfile"]


def test_training_catalog_comes_from_the_server(server, capsys):
    code, out, _ = run(server, ["--json", "training", "profiles"], capsys)
    ids = [p["id"] for p in json.loads(out)]
    assert {"hf-finetune", "pytorch-advanced", "llama-factory", "dataset-prep"} <= set(ids)
    code, out, _ = run(server, ["training", "profile", "dataset-prep"], capsys)
    assert code == 0 and "Bring separately" in out
    code, out, _ = run(server, ["--json", "training", "resolve", "--profile", "dataset-prep", "--target", "nvidia"],
                       capsys)
    resolved = json.loads(out)
    assert code == 4 and resolved["ok"] is False and resolved["error"]


def test_training_build_checks_keep_not_run_apart_from_passed(server, capsys):
    code, out, err = run(server, ["--json", "training", "create", "--name", "tuner", "--profile", "hf-finetune",
                                  "--build", "--wait"], capsys)
    build = json.loads(out.splitlines()[-1])["build"]
    code, out, _ = run(server, ["--json", "training", "checks", build["id"]], capsys)
    checks = {c["id"]: c["status"] for c in json.loads(out)}
    assert checks["gpu"] == "not_run"  # never run on the build host
    code, out, _ = run(server, ["training", "checks", build["id"]], capsys)
    assert "NOT RUN" in out and "gpu-report" in out
    code, out, _ = run(server, ["training", "guide", build["id"]], capsys)
    assert code == 0 and "Bring separately" in out


def test_doctor_separates_client_and_server(server, capsys):
    code, out, _ = run(server, ["--json", "doctor"], capsys)
    rows = json.loads(out)
    assert code == 0
    assert {r["scope"] for r in rows} == {"client", "server"}
    scanner = next(r for r in rows if r["check"] == "Scanner")
    assert scanner["status"] == "warn" and "not scanned" in scanner["detail"]


def test_a_post_is_sent_once_even_when_the_answer_is_slow(server, monkeypatch):
    """A timeout on the build POST must not produce a second build."""
    project = httpx.post(f"{server.url}/api/projects", json={
        "name": "slow", "spec": {"base": {"distribution": "Alpine", "version": "3.22"}}}).json()
    client = ApiClient(server.config(timeout=0.001))
    with pytest.raises(Exception):
        ops.start_build(client, project["id"])
    import time
    time.sleep(1)
    builds = httpx.get(f"{server.url}/api/projects/{project['id']}").json()["builds"]
    assert len(builds) <= 1
