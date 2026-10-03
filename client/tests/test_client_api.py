"""The HTTP client against a mocked transport: retries, errors, transfers."""

import hashlib
import json

import httpx
import pytest

from layersmith_client import api
from layersmith_client.config import ClientConfig

SERVER = "https://layersmith.example.org"


def make(handler, **cfg) -> api.ApiClient:
    return api.ApiClient(ClientConfig(server=SERVER, timeout=5, **cfg), transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda seconds: None)


def test_reads_are_retried_on_transient_failures():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json=[{"id": "p"}])

    assert make(handler).get("/projects") == [{"id": "p"}]
    assert len(calls) == 3


def test_reads_are_retried_on_gateway_errors_and_then_reported():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={"detail": "restarting"})

    with pytest.raises(api.ServerError):
        make(handler).get("/projects")
    assert len(calls) == api.READ_ATTEMPTS


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("dropped")])
def test_a_mutation_is_never_repeated_and_an_unknown_outcome_says_so(failure):
    calls = []

    def handler(request):
        calls.append(request)
        raise failure

    with pytest.raises(api.OutcomeUnknown) as caught:
        make(handler).post("/projects/x/builds", {})
    assert len(calls) == 1  # a lost answer must not start a second build
    assert caught.value.exit_code == api.EXIT_UNKNOWN_OUTCOME
    assert "not repeated" in caught.value.hint


def test_a_mutation_that_never_connected_is_reported_as_unchanged():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(api.Unreachable) as caught:
        make(handler).post("/projects", {"name": "x"})
    assert "Nothing was changed" in caught.value.hint


def test_validation_errors_carry_the_server_message():
    def handler(request):
        return httpx.Response(422, json={"detail": "Invalid project name: 'Bad Name'"})

    with pytest.raises(api.Rejected, match="Invalid project name") as caught:
        make(handler).post("/projects", {"name": "Bad Name"})
    assert caught.value.exit_code == api.EXIT_REJECTED


def test_pydantic_validation_details_are_readable():
    def handler(request):
        return httpx.Response(422, json={"detail": [{"loc": ["body", "name"], "msg": "Field required"}]})

    with pytest.raises(api.Rejected, match="name: Field required"):
        make(handler).post("/projects", {})


def test_conflict_and_not_found_have_their_own_exit_codes():
    with pytest.raises(api.Rejected):
        make(lambda r: httpx.Response(409, json={"detail": "Version 1.0.0 has already been built"})).post("/x", {})
    with pytest.raises(api.NotFound) as caught:
        make(lambda r: httpx.Response(404, json={"detail": "Build not found"})).get("/builds/x")
    assert caught.value.exit_code == api.EXIT_NOT_FOUND


def test_an_html_login_page_is_not_a_success():
    page = "<html><body><form action=/login>Sign in</form></body></html>"

    def handler(request):
        return httpx.Response(200, text=page, headers={"content-type": "text/html"})

    with pytest.raises(api.AuthenticationError, match="web page") as caught:
        make(handler).get("/projects")
    assert caught.value.exit_code == api.EXIT_AUTH


def test_redirects_are_not_followed():
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://sso.example.net/login"})

    with pytest.raises(api.AuthenticationError, match="redirect"):
        make(handler).get("/projects")
    assert calls == ["layersmith.example.org"]  # nothing was sent to the other host


def test_basic_auth_is_sent_and_never_to_a_redirect_target(monkeypatch):
    monkeypatch.setenv("LAYERSMITH_PASSWORD", "s3cret")
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers.get("authorization")))
        return httpx.Response(307, headers={"location": "https://elsewhere.example.net/api/projects"})

    with pytest.raises(api.AuthenticationError):
        make(handler, username="ops").get("/projects")
    assert len(seen) == 1 and seen[0][0] == "layersmith.example.org" and seen[0][1].startswith("Basic ")


def test_missing_password_is_explained_without_prompting(monkeypatch):
    with pytest.raises(api.AuthenticationError, match="no password"):
        api.ApiClient(ClientConfig(server=SERVER, username="ops"))


def test_401_from_a_proxy_mentions_the_supported_methods():
    def handler(request):
        return httpx.Response(401, headers={"www-authenticate": 'Basic realm="x"'}, text="no")

    with pytest.raises(api.AuthenticationError) as caught:
        make(handler).get("/projects")
    assert "Basic" in str(caught.value) and "client certificates" in caught.value.hint


def test_tls_failures_point_at_the_ca_option_not_at_disabling_verification():
    def handler(request):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")

    with pytest.raises(api.Unreachable) as caught:
        make(handler).get("/health")
    assert "ca_cert" in caught.value.hint and "never switched off" in caught.value.hint


def test_missing_ca_bundle_is_reported(tmp_path):
    with pytest.raises(api.ClientError, match="CA bundle not found"):
        api.ApiClient(ClientConfig(server=SERVER, ca_cert=str(tmp_path / "missing.pem")))


def test_a_server_that_is_not_layersmith_is_recognised():
    with pytest.raises(api.ServerError, match="does not identify itself"):
        make(lambda r: httpx.Response(200, json={"status": "ok"})).health()


def test_feature_detection_tolerates_older_servers():
    client = make(lambda r: httpx.Response(200, json={"status": "ok", "app": "LayerSmith", "version": "0.3.1"}))
    assert client.features() == [] and not client.has_feature("log-offset")


def test_server_error_messages_are_cleaned_of_escape_sequences():
    def handler(request):
        return httpx.Response(422, json={"detail": "bad\x1b]0;owned\x07 name\x1b[2J"})

    with pytest.raises(api.Rejected) as caught:
        make(handler).post("/projects", {})
    assert "\x1b" not in str(caught.value) and "\x07" not in str(caught.value)


# ------------------------------------------------------------- downloads

CONTENT = b"oci-archive-content" * 1000
DIGEST = hashlib.sha256(CONTENT).hexdigest()


def file_handler(content=CONTENT, name="layersmith-demo-1.0.0.tar", status=200, length=None):
    def handler(request):
        headers = {"content-type": "application/octet-stream",
                   "content-disposition": f'attachment; filename="{name}"'}
        if length is not None:
            headers["content-length"] = str(length)
        return httpx.Response(status, content=content, headers=headers)
    return handler


def test_download_is_verified_and_written_atomically(tmp_path):
    result = make(file_handler()).download("/builds/b/download/export", tmp_path / "image.tar",
                                           expected_sha256=DIGEST)
    assert (tmp_path / "image.tar").read_bytes() == CONTENT
    assert result["verified"] and result["sha256"] == DIGEST
    assert [p.name for p in tmp_path.iterdir()] == ["image.tar"]  # no temporary file left


def test_checksum_mismatch_leaves_nothing_behind(tmp_path):
    with pytest.raises(api.FileProblem, match="Checksum mismatch") as caught:
        make(file_handler()).download("/builds/b/download/export", tmp_path / "image.tar",
                                      expected_sha256="0" * 64)
    assert caught.value.exit_code == api.EXIT_FILE
    assert list(tmp_path.iterdir()) == []


def test_a_short_download_is_detected(tmp_path):
    with pytest.raises(api.FileProblem, match="incomplete"):
        make(file_handler(length=len(CONTENT) + 10)).download(
            "/builds/b/download/export", tmp_path / "image.tar", expected_sha256=None)
    assert list(tmp_path.iterdir()) == []


def test_an_interrupted_download_leaves_nothing_behind(tmp_path):
    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            yield b"partial"
            raise httpx.ReadError("connection reset")

    def handler(request):
        return httpx.Response(200, stream=Broken(), headers={"content-type": "application/octet-stream"})

    with pytest.raises(api.FileProblem, match="interrupted"):
        make(handler).download("/builds/b/download/export", tmp_path / "image.tar", expected_sha256=DIGEST)
    assert list(tmp_path.iterdir()) == []


def test_existing_files_are_not_replaced_without_overwrite(tmp_path):
    target = tmp_path / "image.tar"
    target.write_bytes(b"keep me")
    with pytest.raises(api.FileProblem, match="already exists"):
        make(file_handler()).download("/x", target, expected_sha256=DIGEST)
    assert target.read_bytes() == b"keep me"
    make(file_handler()).download("/x", target, expected_sha256=DIGEST, overwrite=True)
    assert target.read_bytes() == CONTENT


@pytest.mark.parametrize("server_name", ["../../etc/passwd", "/etc/cron.d/evil", ".bashrc", "a\\..\\b",
                                         "name with spaces.tar", ""])
def test_server_file_names_cannot_escape_the_directory(tmp_path, server_name):
    result = make(file_handler(name=server_name)).download("/x", tmp_path, expected_sha256=DIGEST,
                                                           fallback_name="fallback.tar")
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert len(written) == 1 and written[0].parent == tmp_path
    assert result["path"] == str(written[0].resolve())
    assert not written[0].name.startswith(".")


def test_a_safe_server_file_name_is_used_in_a_directory(tmp_path):
    make(file_handler()).download("/x", tmp_path, expected_sha256=DIGEST)
    assert (tmp_path / "layersmith-demo-1.0.0.tar").is_file()


def test_missing_file_on_the_server_is_a_file_problem(tmp_path):
    def handler(request):
        return httpx.Response(404, json={"detail": "File is no longer available"})

    with pytest.raises(api.FileProblem, match="no longer available"):
        make(handler).download("/x", tmp_path / "a.tar", expected_sha256=DIGEST)
    assert list(tmp_path.iterdir()) == []


def test_unverified_download_is_reported_as_unverified(tmp_path):
    result = make(file_handler()).download("/x", tmp_path / "a.tar", expected_sha256=None)
    assert result["verified"] is False


def test_upload_is_checked_against_the_local_checksum(tmp_path):
    local = tmp_path / "motd"
    local.write_text("hello\n")
    digest = hashlib.sha256(b"hello\n").hexdigest()
    received = {}

    def handler(request):
        received["body"] = request.read()
        return httpx.Response(201, json={"sha256": digest, "filename": "motd", "size": 6})

    result = make(handler).upload(local)
    assert result["sha256"] == digest and b"hello" in received["body"]
    # The local path never reaches the server.
    assert str(tmp_path).encode() not in received["body"]

    def wrong(request):
        return httpx.Response(201, json={"sha256": "f" * 64, "filename": "motd", "size": 6})

    with pytest.raises(api.FileProblem, match="different checksum"):
        make(wrong).upload(local)


def test_json_body_is_sent_as_json():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.read())
        seen["type"] = request.headers["content-type"]
        return httpx.Response(201, json={"id": "p"})

    make(handler).post("/projects", {"name": "demo"})
    assert seen == {"body": {"name": "demo"}, "type": "application/json"}
