"""Deleting a build from the Builds list, and builds a restart left running."""

from pathlib import Path

from layersmith import config
from layersmith.api import create_app
from layersmith.models import Build, Scan, make_engine, make_session_factory
from tests.test_api import client, run_pending_build  # noqa: F401 - fixture

PROJECT = {"name": "gpu-benchmark", "template": "Minimal",
           "spec": {"base": {"distribution": "Alpine", "version": "3.22"}}}


def built(client, body=PROJECT):
    project = client.post("/api/projects", json=body).json()
    build = client.post(f"/api/projects/{project['id']}/builds", json={}).json()
    run_pending_build(client, build["id"])
    return project, client.get(f"/api/builds/{build['id']}").json()


def export_path(client, build_id) -> Path:
    with client.state["session_factory"]() as session:
        return Path(session.get(Build, build_id).export_path)


def test_delete_removes_row_archives_log_and_image(client):
    project, build = built(client)
    client.post(f"/api/builds/{build['id']}/airgap")
    archive = export_path(client, build["id"])
    log = client.state["build_service"].log_path(build["id"])
    with client.state["session_factory"]() as session:
        bundle = Path(session.get(Build, build["id"]).airgap_path)
    assert archive.is_file() and bundle.is_file() and log.is_file()

    response = client.delete(f"/api/builds/{build['id']}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["number"] == build["number"] and body["image_removed"] and body["freed_bytes"] > 0
    assert not archive.exists() and not bundle.exists() and not log.exists()
    assert ("remove", build["image_ref"]) in client.state["build_service"].backend.calls
    assert client.get(f"/api/builds/{build['id']}").status_code == 404
    assert client.get("/api/builds").json() == []
    assert client.get(f"/api/projects/{project['id']}").status_code == 200
    assert client.delete(f"/api/builds/{build['id']}").status_code == 404


def test_delete_can_keep_the_image(client):
    _, build = built(client)
    assert client.delete(f"/api/builds/{build['id']}?remove_image=false").json()["image_removed"] is False
    assert not [call for call in client.state["build_service"].backend.calls if call[0] == "remove"]


def test_files_and_image_shared_with_another_build_are_kept(client):
    """A deleted and re-created project builds the same reference and archive name."""
    first_project, first = built(client)
    client.delete(f"/api/projects/{first_project['id']}")
    _, second = built(client)
    assert first["image_ref"] == second["image_ref"]
    assert export_path(client, first["id"]) == export_path(client, second["id"])

    body = client.delete(f"/api/builds/{first['id']}").json()
    assert body["image_removed"] is False and body["freed_bytes"] == 0
    assert any("another build" in note for note in body["notes"])
    assert export_path(client, second["id"]).is_file()
    assert client.get(f"/api/builds/{second['id']}").json()["has_export"]


def test_running_builds_cannot_be_deleted(client):
    project = client.post("/api/projects", json=PROJECT).json()
    queued = client.post(f"/api/projects/{project['id']}/builds", json={}).json()
    response = client.delete(f"/api/builds/{queued['id']}")
    assert response.status_code == 409 and "still running" in response.json()["detail"]


def test_scans_of_a_deleted_build_go_with_it(client, tmp_path):
    _, build = built(client)
    report = Path(client.state["settings"].scan_dir) / "x-report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("{}")
    with client.state["session_factory"]() as session:
        session.add(Scan(build_id=build["id"], state="completed", report_path=str(report)))
        session.add(Scan(build_id=build["id"], state="scanning"))
        session.commit()
    assert client.delete(f"/api/builds/{build['id']}").status_code == 409  # one scan still running
    with client.state["session_factory"]() as session:
        for scan in session.query(Scan).all():
            scan.state = "completed"
        session.commit()
    assert client.delete(f"/api/builds/{build['id']}").status_code == 200
    assert not report.exists()
    with client.state["session_factory"]() as session:
        assert session.query(Scan).count() == 0


def test_deleted_projects_builds_show_the_projects_own_name(client):
    project, build = built(client)
    client.delete(f"/api/projects/{project['id']}")
    built(client)  # takes the name over; the old row becomes gpu-benchmark~<id>
    listed = {b["id"]: b for b in client.get("/api/builds").json()}
    assert listed[build["id"]]["project_name"] == "gpu-benchmark"
    assert listed[build["id"]]["project_deleted"] is True


def test_builds_a_restart_left_running_are_marked_failed(client, tmp_path):
    project = client.post("/api/projects", json=PROJECT).json()
    stuck = client.post(f"/api/projects/{project['id']}/builds", json={}).json()
    with client.state["session_factory"]() as session:
        session.get(Build, stuck["id"]).status = "building"
        session.commit()
    settings = client.state["settings"]
    create_app(config.reset_for_tests(**settings.__dict__))  # what a restart does
    with make_session_factory(make_engine(settings.database_url))() as session:
        build = session.get(Build, stuck["id"])
        assert build.status == "failed" and "Interrupted" in build.error and build.finished_at
    assert client.delete(f"/api/builds/{stuck['id']}").status_code == 200
