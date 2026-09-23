"""Attack cases against the backend, which is the security boundary.

The frontend is not trusted: every test here talks to the API or to the core
validators directly. Each case is an attack someone would actually try, and
the assertion is that it is refused or neutralised - not merely that the
happy path still works.
"""

import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from layersmith.core import containerfile as cf
from layersmith.core.spec import InvalidSpec, parse_reference, validate_spec
from layersmith.services.builds import BuildService
from tests.test_api import client, run_pending_build  # noqa: F401  (fixtures)
from tests.test_builds import env, make_build  # noqa: F401

# Shell metacharacters and expansions that must never reach a command.
INJECTIONS = [
    "; id",
    "&& id",
    "| id",
    "$(id)",
    "`id`",
    "${HOME}",
    "foo;rm -rf /",
    "foo && touch /tmp/layersmith-pwned",
    "foo\nid",
    "foo\r\nid",
    "--option-injection",
    "-rf",
    "foo'; id; '",
    'foo"; id; "',
]

TRAVERSALS = [
    "../../../etc/passwd",
    "../../layersmith.db",
    "/foo/../../../etc/shadow",
    "..%2f..%2fetc%2fpasswd",
    "....//....//etc/passwd",
    "/etc/passwd\x00/opt/app",
    "/opt/../../etc/passwd",
    "/opt/app/．．/secret",
]


def base_spec(**overrides):
    spec = {"base": {"distribution": "Alpine", "version": "3.22"}}
    spec.update(overrides)
    return spec


# ------------------------------------------------------- command injection

@pytest.mark.parametrize("payload", INJECTIONS)
def test_package_names_reject_shell_metacharacters(payload):
    with pytest.raises(InvalidSpec):
        validate_spec(base_spec(packages=[payload]))


@pytest.mark.parametrize("payload", INJECTIONS)
def test_extra_packages_reject_shell_metacharacters(payload):
    with pytest.raises(InvalidSpec):
        validate_spec(base_spec(extra_packages=[payload]))


@pytest.mark.parametrize("payload", INJECTIONS)
def test_image_references_reject_shell_metacharacters(payload):
    with pytest.raises(InvalidSpec):
        parse_reference(f"alpine:{payload}")


@pytest.mark.parametrize("payload", INJECTIONS)
def test_project_and_version_fields_reject_injection(client, payload):  # noqa: F811
    response = client.post("/api/projects", json={
        "name": payload, "template": "Minimal", "spec": base_spec()})
    assert response.status_code == 422, response.text

    ok = client.post("/api/projects", json={
        "name": "victim", "template": "Minimal", "spec": base_spec()})
    if ok.status_code == 201:
        bad_version = client.post(f"/api/projects/{ok.json()['id']}/builds", json={"version": payload})
        assert bad_version.status_code == 422
        client.delete(f"/api/projects/{ok.json()['id']}")


@pytest.mark.parametrize("payload", INJECTIONS)
def test_environment_variable_names_reject_injection(payload):
    with pytest.raises(InvalidSpec):
        validate_spec(base_spec(env=[{"name": payload, "value": "x"}]))


def test_generated_containerfile_never_contains_raw_user_metacharacters():
    """Values that do pass validation must still be quoted where they land."""
    spec = validate_spec(base_spec(
        env=[{"name": "GREETING", "value": 'x"; id; echo "'}],
        labels={"org.example.note": 'a"; id; echo "'},
    ))
    text, _, _ = cf.generate({"name": "demo", "description": 'desc"; id'}, spec)
    for line in text.splitlines():
        if line.startswith(("ENV ", "LABEL ", "      org.")):
            # Every double quote inside a value must be escaped.
            body = line.split("=", 1)[1] if "=" in line else ""
            assert '"; id' not in body.replace('\\"', "")


def test_build_command_is_an_argv_list_not_a_string(env):  # noqa: F811
    """A backend receives arguments as a list; nothing is concatenated."""
    build_id = make_build(env)
    env["service"].run(build_id)
    call = [c for c in env["service"].backend.calls if c[0] == "build"][0]
    assert isinstance(call, tuple) and all(isinstance(part, (str, Path)) for part in call)


# --------------------------------------------------------- path traversal

@pytest.mark.parametrize("payload", TRAVERSALS)
def test_file_destinations_reject_traversal(payload):
    with pytest.raises(InvalidSpec):
        validate_spec(base_spec(files=[{"sha256": "a" * 64, "destination": payload}]))


@pytest.mark.parametrize("payload", TRAVERSALS)
def test_tool_install_paths_reject_traversal(payload):
    with pytest.raises(InvalidSpec):
        validate_spec(base_spec(tools=[{"name": "tool", "sha256": "a" * 64, "install_path": payload}]))


@pytest.mark.parametrize("payload", TRAVERSALS)
def test_workdir_rejects_traversal(payload):
    with pytest.raises(InvalidSpec):
        validate_spec(base_spec(workdir=payload))


@pytest.mark.parametrize("payload", ["../../../etc/passwd", "..%2f..%2fetc%2fpasswd", "....//etc/passwd"])
def test_static_file_serving_rejects_traversal(client, payload):  # noqa: F811
    response = client.get(f"/{payload}")
    # Either refused, or the SPA fallback - never the contents of a host file.
    assert "root:x:0:0" not in response.text


def test_download_kind_is_restricted(client):  # noqa: F811
    project = client.post("/api/projects", json={
        "name": "app", "template": "Minimal", "spec": base_spec()}).json()
    build = client.post(f"/api/projects/{project['id']}/builds", json={}).json()
    run_pending_build(client, build["id"])
    for kind in ("../../etc/passwd", "export/../../..%2fetc", "manifest"):
        assert client.get(f"/api/builds/{build['id']}/download/{kind}").status_code == 404


def test_storage_paths_reject_traversal_and_system_directories(client):  # noqa: F811
    for path in ["../../etc", "/etc", "/etc/../etc", "/proc/self", "relative", "/"]:
        response = client.put("/api/settings/storage", json={"paths": {"image_dir": path}})
        assert response.status_code == 422, f"{path} was accepted"


# --------------------------------------------------------------- symlinks

def test_a_symlinked_upload_is_not_followed_into_the_build_context(env):  # noqa: F811
    """A blob that is a symlink to a host file must not be copied into a build."""
    checksum = "b" * 64
    uploads = Path(env["settings"].upload_dir)
    secret = Path(env["settings"].data_dir) / "host-secret.txt"
    secret.write_text("SENSITIVE")
    (uploads / checksum).symlink_to(secret)

    build_id = make_build(env, {"files": [{"sha256": checksum, "destination": "/opt/app/x"}]})
    env["service"].run(build_id)

    context_file = env["service"].context_dir(build_id) / "files" / checksum
    assert not context_file.exists() or context_file.read_text() != "SENSITIVE"


def test_symlinked_storage_target_outside_the_data_root_is_refused(client, tmp_path):  # noqa: F811
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link-to-outside"
    link.symlink_to(outside)
    response = client.put("/api/settings/storage", json={"paths": {"image_dir": str(link / ".." / "etc")}})
    assert response.status_code == 422


# ---------------------------------------------------------------- archives

def test_airgap_bundle_contains_no_absolute_or_traversing_paths(env):  # noqa: F811
    build_id = make_build(env)
    env["service"].run(build_id)
    bundle = env["service"].airgap_bundle(build_id)

    with tarfile.open(bundle) as archive:
        for member in archive.getmembers():
            assert not member.name.startswith("/"), member.name
            assert ".." not in Path(member.name).parts, member.name
            assert not member.issym() and not member.islnk(), f"{member.name} is a link"


def test_bundle_never_includes_the_database_or_other_data(env):  # noqa: F811
    build_id = make_build(env)
    env["service"].run(build_id)
    bundle = env["service"].airgap_bundle(build_id)
    with tarfile.open(bundle) as archive:
        names = archive.getnames()
    assert not any(name.endswith((".db", ".sqlite", ".env")) for name in names), names


# ------------------------------------------------------- uploads and DoS

def test_upload_size_limit_is_enforced(client):  # noqa: F811
    client.state["settings"].max_upload_bytes = 2048
    response = client.post("/api/uploads", files={"file": ("big.bin", b"x" * 8192)})
    assert response.status_code == 413
    assert list(client.state["settings"].upload_dir.iterdir()) == []


def test_upload_filename_cannot_escape_the_upload_directory(client):  # noqa: F811
    response = client.post("/api/uploads", files={"file": ("../../evil.txt", b"data")})
    assert response.status_code == 201
    stored = client.state["settings"].upload_dir
    # The name on disk is the checksum, never anything the client chose.
    assert [p.name for p in stored.iterdir()] == [response.json()["sha256"]]
    assert not (stored.parent / "evil.txt").exists()


def test_scripts_are_size_limited():
    with pytest.raises(InvalidSpec):
        validate_spec(base_spec(scripts={"pre_build": "echo hi\n" * 200_000}))


def test_test_commands_are_bounded():
    with pytest.raises(InvalidSpec):
        validate_spec(base_spec(tests=["x" * 501]))
    spec = validate_spec(base_spec(tests=[f"echo {i}" for i in range(200)]))
    assert len(spec["tests"]) <= 50


# ---------------------------------------------------------------- secrets

@pytest.mark.parametrize("name", [
    "PASSWORD", "DB_PASSWORD", "API_KEY", "APIKEY", "AWS_SECRET_ACCESS_KEY",
    "GITHUB_TOKEN", "REGISTRY_PASSWORD", "PRIVATE_KEY", "CLIENT_SECRET",
    "AUTH_TOKEN", "npm_token", "ssh_private_key",
])
def test_credential_shaped_environment_variables_are_refused(name):
    with pytest.raises(InvalidSpec) as excinfo:
        validate_spec(base_spec(env=[{"name": name, "value": "hunter2"}]))
    assert "runtime" in str(excinfo.value).lower() or "secret" in str(excinfo.value).lower()


def test_build_manifest_carries_no_environment_values(env):  # noqa: F811
    """Values are legitimate in an image, but must not leak into metadata."""
    build_id = make_build(env, {"env": [{"name": "GREETING", "value": "not-a-secret-but-still-private"}]})
    env["service"].run(build_id)
    bundle = env["service"].airgap_bundle(build_id)
    with tarfile.open(bundle) as archive:
        manifest = json.loads(archive.extractfile(
            [m for m in archive.getmembers() if m.name.endswith("manifest.json")][0]).read())
    assert "not-a-secret-but-still-private" not in json.dumps(manifest)


def test_unknown_api_paths_answer_as_api_not_as_the_web_app(tmp_path):
    """A mistyped API path must be a JSON 404, never index.html with 200."""
    from fastapi.testclient import TestClient
    from layersmith import config
    from layersmith.api import create_app

    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html>ui")
    settings = config.reset_for_tests(
        data_dir=tmp_path / "d", image_dir=tmp_path / "d/i", build_dir=tmp_path / "d/b",
        upload_dir=tmp_path / "d/u", log_dir=tmp_path / "d/l", tmp_dir=tmp_path / "d/t",
        database_url=f"sqlite:///{tmp_path}/spa404.db", static_dir=static)

    with TestClient(create_app(settings)) as web:
        for path in ["/api/nope", "/api/builds/etc/passwd", "/api/builds/ID/x", "/api"]:
            response = web.get(path)
            assert response.status_code == 404, f"{path} returned {response.status_code}"
            assert "<!doctype html>" not in response.text.lower(), path
        # Application routes still fall back to the web app.
        assert web.get("/builds/anything").text.startswith("<!doctype html>")


def test_a_symlinked_blob_is_refused_explicitly(env):  # noqa: F811
    """Not merely caught by the checksum: links are refused as links."""
    payload = b"whatever"
    import hashlib
    checksum = hashlib.sha256(payload).hexdigest()
    uploads = Path(env["settings"].upload_dir)
    real = Path(env["settings"].data_dir) / "real.bin"
    real.write_bytes(payload)
    (uploads / checksum).symlink_to(real)   # correct checksum, still a link

    build_id = make_build(env, {"files": [{"sha256": checksum, "destination": "/opt/app/x"}]})
    env["service"].run(build_id)
    with env["factory"]() as session:
        from layersmith.models import Build
        assert "link" in (session.get(Build, build_id).error or "")


def test_an_image_cannot_reference_unbounded_files():
    files = [{"sha256": f"{i:064x}", "destination": f"/opt/app/f{i}"} for i in range(150)]
    with pytest.raises(InvalidSpec):
        validate_spec(base_spec(files=files))


def test_build_context_size_is_capped(env):  # noqa: F811
    import hashlib
    env["settings"].max_context_bytes = 1024
    payload = b"x" * 4096
    checksum = hashlib.sha256(payload).hexdigest()
    (Path(env["settings"].upload_dir) / checksum).write_bytes(payload)

    build_id = make_build(env, {"files": [{"sha256": checksum, "destination": "/opt/app/big"}]})
    env["service"].run(build_id)
    with env["factory"]() as session:
        from layersmith.models import Build
        assert "larger than" in (session.get(Build, build_id).error or "")
