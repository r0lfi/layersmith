"""Regression tests for the problems found in the 0.1.0 review."""

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from layersmith import config
from layersmith.api import create_app
from layersmith.models import Build, Project, make_engine, next_build_number, utcnow
from layersmith.services.builds import MAX_LOG_BYTES
from tests.test_api import client, run_pending_build  # noqa: F401  (fixtures)
from tests.test_builds import env, make_build  # noqa: F401


# ------------------------------------------------------------ database

def test_sqlite_uses_wal_and_waits_for_a_busy_database(tmp_path):
    """The API threads and the build worker write concurrently."""
    engine = make_engine(f"sqlite:///{tmp_path}/pragma.db")
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert connection.execute(text("PRAGMA busy_timeout")).scalar() >= 5000


def test_concurrent_builds_of_one_version_yield_one_build_not_a_500(client):  # noqa: F811
    project = client.post("/api/projects", json={
        "name": "racer", "template": "Minimal",
        "spec": {"base": {"distribution": "Alpine", "version": "3.22"}}}).json()

    results: list[int] = []
    barrier = threading.Barrier(4)

    def start():
        barrier.wait()
        results.append(client.post(f"/api/projects/{project['id']}/builds", json={"version": "1.0.0"}).status_code)

    threads = [threading.Thread(target=start) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [202, 409, 409, 409], results
    assert len(client.get("/api/builds").json()) == 1


def test_building_an_older_version_does_not_rewind_the_suggestion(client):  # noqa: F811
    project = client.post("/api/projects", json={
        "name": "app", "template": "Minimal",
        "spec": {"base": {"distribution": "Alpine", "version": "3.22"}}}).json()
    client.post(f"/api/projects/{project['id']}/builds", json={"version": "2.0.0"})
    assert client.get(f"/api/projects/{project['id']}").json()["next_version"] == "2.0.1"

    client.post(f"/api/projects/{project['id']}/builds", json={"version": "1.5.0"})
    assert client.get(f"/api/projects/{project['id']}").json()["next_version"] == "2.0.1"


# -------------------------------------------------------------- uploads

def test_upload_leaves_no_temporary_file_behind(client):  # noqa: F811
    settings = client.state["settings"]
    client.post("/api/uploads", files={"file": ("a.txt", b"hello")})
    assert list(settings.tmp_dir.glob("upload-*")) == []


def test_oversized_upload_is_refused_and_cleaned_up(client):  # noqa: F811
    settings = client.state["settings"]
    settings.max_upload_bytes = 1024
    response = client.post("/api/uploads", files={"file": ("big.bin", b"x" * 4096)})
    assert response.status_code == 413
    assert list(settings.tmp_dir.glob("upload-*")) == []
    assert list(settings.upload_dir.iterdir()) == []


# ------------------------------------------------------- static serving

def test_spa_fallback_cannot_escape_the_static_directory(tmp_path):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html>ui")
    # A sibling whose name starts with the same characters as the static dir.
    sibling = tmp_path / "static-private"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("do not serve me")

    settings = config.reset_for_tests(
        data_dir=tmp_path / "data", image_dir=tmp_path / "data/images", build_dir=tmp_path / "data/builds",
        upload_dir=tmp_path / "data/uploads", log_dir=tmp_path / "data/logs", tmp_dir=tmp_path / "data/tmp",
        database_url=f"sqlite:///{tmp_path}/spa.db", static_dir=static,
    )
    with TestClient(create_app(settings)) as web:
        assert web.get("/").status_code == 200
        escaping = web.get("/../static-private/secret.txt")
        assert "do not serve me" not in escaping.text
        assert web.get("/builds").text.startswith("<!doctype html>")


# --------------------------------------------------------- build runner

def test_queued_build_survives_the_project_being_deleted(env):  # noqa: F811
    """Deletion is a soft delete, so a build already queued still finishes."""
    build_id = make_build(env)
    with env["factory"]() as session:
        build = session.get(Build, build_id)
        session.get(Project, build.project_id).deleted_at = utcnow()
        session.commit()

    env["service"].run(build_id)

    with env["factory"]() as session:
        assert session.get(Build, build_id).status == "ready"


def test_failed_build_removes_its_context(env):  # noqa: F811
    from tests.test_builds import FakeBackend

    env["settings"].keep_build_contexts = False
    env["service"].backend = FakeBackend(fail_on="build")
    build_id = make_build(env)
    env["service"].run(build_id)
    assert not env["service"].context_dir(build_id).exists()


def test_log_file_stops_growing_at_the_cap(env):  # noqa: F811
    build_id = make_build(env)
    line = "x" * 4096
    for _ in range((MAX_LOG_BYTES // len(line)) + 50):
        env["service"]._emit(build_id, line)

    size = env["service"].log_path(build_id).stat().st_size
    assert size <= MAX_LOG_BYTES + len(line) + 100
    assert "log truncated" in env["service"].log_path(build_id).read_text()[-200:]


def test_queue_survives_a_build_that_raises(env):  # noqa: F811
    """One broken build must not take the worker thread down with it."""
    service = env["service"]
    service.run = lambda build_id: (_ for _ in ()).throw(RuntimeError("boom"))
    service.start()
    good = make_build(env)
    service.enqueue(good)
    service._queue.join()
    assert service._worker.is_alive()


# ------------------------------------------------------- storage settings

def test_storage_paths_can_be_changed_and_survive_a_restart(client, tmp_path):  # noqa: F811
    new_images = tmp_path / "elsewhere" / "images"
    response = client.put("/api/settings/storage", json={"paths": {"image_dir": str(new_images)}})
    assert response.status_code == 200
    assert new_images.is_dir()

    row = [r for r in client.get("/api/settings").json()["paths"] if r["field"] == "image_dir"][0]
    assert row["path"] == str(new_images) and row["source"] == "setting"

    # A fresh app against the same database picks the change up.
    from layersmith.api import create_app as rebuild
    from fastapi.testclient import TestClient as Client

    with Client(rebuild(config.reset_for_tests(**{**client.state["settings"].__dict__,
                                                  "image_dir": client.state["settings"].data_dir / "images"}))) as web:
        again = [r for r in web.get("/api/settings").json()["paths"] if r["field"] == "image_dir"][0]
        assert again["path"] == str(new_images)


def test_archives_written_before_a_move_stay_downloadable(client, tmp_path):  # noqa: F811
    project = client.post("/api/projects", json={
        "name": "mover", "template": "Minimal",
        "spec": {"base": {"distribution": "Alpine", "version": "3.22"}}}).json()
    build = client.post(f"/api/projects/{project['id']}/builds", json={}).json()
    run_pending_build(client, build["id"])
    assert client.get(f"/api/builds/{build['id']}/download/export").status_code == 200

    client.put("/api/settings/storage", json={"paths": {"image_dir": str(tmp_path / "new-images")}})
    assert client.get(f"/api/builds/{build['id']}/download/export").status_code == 200


@pytest.mark.parametrize("bad", ["relative/path", "/etc", "/", "/proc/self", "../escape"])
def test_unsafe_storage_paths_are_refused(client, bad):  # noqa: F811
    response = client.put("/api/settings/storage", json={"paths": {"image_dir": bad}})
    assert response.status_code == 422


def test_environment_pinned_paths_cannot_be_changed_from_the_ui(client, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LAYERSMITH_LOG_DIR", str(tmp_path / "pinned-logs"))
    row = [r for r in client.get("/api/settings").json()["paths"] if r["field"] == "log_dir"][0]
    assert row["editable"] is False and row["source"] == "environment"

    response = client.put("/api/settings/storage", json={"paths": {"log_dir": str(tmp_path / "other")}})
    assert response.status_code == 422 and "LAYERSMITH_LOG_DIR" in response.json()["detail"]


def test_data_directory_is_not_movable_from_the_ui(client, tmp_path):  # noqa: F811
    response = client.put("/api/settings/storage", json={"paths": {"data_dir": str(tmp_path / "nope")}})
    assert response.status_code == 422
