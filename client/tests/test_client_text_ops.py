"""Untrusted text, status vocabulary and the shared operations."""

import pytest

from layersmith_client import ops
from layersmith_client.api import Unreachable
from layersmith_client.text import check_status, clean, security_status


@pytest.mark.parametrize("raw", [
    "\x1b[31mred\x1b[0m", "\x1b]0;window title\x07text", "\x1b]8;;http://evil\x1b\\link\x1b]8;;\x1b\\",
    "a\x9b2Jb", "bell\x07", "back\x08space", "\x1bPdcs\x1b\\x",
])
def test_clean_removes_escape_sequences_and_controls(raw):
    cleaned = clean(raw)
    assert not any(ord(ch) < 32 or 0x7f <= ord(ch) <= 0x9f for ch in cleaned)


def test_clean_keeps_text_and_optionally_newlines():
    assert clean("plain [bold]markup[/bold] text") == "plain [bold]markup[/bold] text"
    assert clean("a\nb") == "a b"
    assert clean("a\nb\x1b[2J", keep_newlines=True) == "a\nb"


def test_check_status_never_turns_unknown_into_a_pass():
    assert check_status("passed") == "PASSED"
    assert check_status("not_run") == "NOT RUN"
    assert check_status("failed") == "FAILED"
    assert check_status("skipped") == "UNKNOWN"
    assert check_status(None) == "UNKNOWN"


NONE = {"name": "none", "available": False, "detail": "No scanner configured"}
TRIVY = {"name": "trivy", "available": True, "detail": "ok"}
TRIVY_DOWN = {"name": "trivy", "available": False, "detail": "image not pulled"}


@pytest.mark.parametrize("scanner, scans, label", [
    (NONE, [], "NOT SCANNED"),
    (TRIVY_DOWN, [], "NOT SCANNED"),
    (TRIVY, [], "NOT SCANNED"),
    (None, [], "NOT SCANNED"),
    (TRIVY, None, "UNKNOWN"),
    (TRIVY, [{"state": "scanning"}], "SCAN SCANNING"),
    (TRIVY, [{"state": "queued"}], "SCAN QUEUED"),
    (TRIVY, [{"state": "failed", "error": "timeout"}], "SCAN FAILED"),
    (TRIVY, [{"state": "completed", "total": 0, "severity": {}}], "NO FINDINGS"),
    (TRIVY, [{"state": "completed", "total": 3, "severity": {"high": 1, "low": 2}}], "3 FINDINGS"),
])
def test_security_states_stay_distinct(scanner, scans, label):
    assert security_status(scanner, scans)[0] == label


def test_only_a_completed_scan_without_findings_reads_as_clean():
    for scanner, scans in [(NONE, []), (TRIVY_DOWN, []), (TRIVY, None), (TRIVY, [{"state": "failed"}]),
                           (TRIVY, [{"state": "scanning"}])]:
        label, tone, _ = security_status(scanner, scans)
        assert tone != "ok" and "NO FINDINGS" not in label
    explanation = security_status(NONE, [])[2]
    assert "not the same as it being clean" in explanation


# ------------------------------------------------------------- operations

class FakeClient:
    """Answers GETs from a script; records everything."""

    def __init__(self, responses, features=("log-offset",)):
        self.responses = responses
        self.calls = []
        self._features = list(features)

    def has_feature(self, name):
        return name in self._features

    def get(self, path, params=None, **kwargs):
        self.calls.append(("GET", path, params))
        answer = self.responses[path]
        value = answer.pop(0) if isinstance(answer, list) else answer
        if isinstance(value, Exception):
            raise value
        return value(params) if callable(value) else value


def log_responder(text, status="ready"):
    data = text.encode()

    def respond(params):
        offset = params.get("offset", 0) if "tail" not in params else max(0, len(data) - params["tail"])
        return {"status": status, "finished": status == "ready", "offset": offset, "next_offset": len(data),
                "size": len(data), "complete": status == "ready", "text": data[offset:].decode()}
    return respond


def test_log_follower_reads_incrementally_and_reports_skipped_bytes():
    client = FakeClient({"/builds/b/log": log_responder("line one\nline two\nline three\n")})
    follower = ops.LogFollower(client, "b", tail_bytes=11)
    lines = list(follower.poll())
    assert lines[0].startswith("[") and "not shown" in lines[0]
    assert lines[-1] == "line three" and follower.finished


def test_log_follower_resumes_from_its_offset_after_a_lost_connection():
    first = {"status": "building", "finished": False, "offset": 0, "next_offset": 9, "size": 9,
             "complete": False, "text": "line one\n"}
    second = {"status": "ready", "finished": True, "offset": 9, "next_offset": 18, "size": 18,
              "complete": True, "text": "line two\n"}
    client = FakeClient({"/builds/b/log": [first, Unreachable("gone"), second]})
    follower = ops.LogFollower(client, "b", tail_bytes=None)
    assert list(follower.poll()) == ["line one"]
    with pytest.raises(Unreachable):
        list(follower.poll())
    assert list(follower.poll()) == ["line two"]
    assert client.calls[-1][2] == {"offset": 9}  # resumed, nothing read twice


def test_log_follower_falls_back_for_older_servers():
    client = FakeClient({"/builds/b": [{"status": "building", "log": "a\nb\n"},
                                       {"status": "ready", "log": "a\nb\nc\n"}]}, features=())
    follower = ops.LogFollower(client, "b")
    assert list(follower.poll()) == ["a", "b"]
    assert list(follower.poll()) == ["c"] and follower.finished


def test_wait_for_build_reports_a_lost_connection_and_never_a_result():
    client = FakeClient({"/builds/b": [Unreachable("down"), Unreachable("down"),
                                       {"status": "building"}, {"status": "ready", "number": 1}]})
    statuses = []
    build = ops.wait_for_build(client, "b", on_status=statuses.append, sleep=lambda s: None)
    assert build["status"] == "ready"
    assert statuses[0].startswith("connection lost") and statuses[-1] == "ready"


def test_wait_for_build_gives_up_as_unreachable_not_as_finished(monkeypatch):
    client = FakeClient({"/builds/b": [Unreachable("down")] * 50})
    times = iter(range(0, 10000, 100))
    monkeypatch.setattr(ops.time, "monotonic", lambda: next(times))
    with pytest.raises(Unreachable):
        ops.wait_for_build(client, "b", sleep=lambda s: None, reconnect_seconds=300)


def test_materialise_uploads_local_files_and_never_sends_their_paths(tmp_path):
    (tmp_path / "motd").write_text("hi")

    class Uploader:
        uploaded = []

        def upload(self, path):
            self.uploaded.append(path)
            return {"sha256": "a" * 64, "filename": "motd", "size": 2}

    document = {"name": "demo", "spec": {"files": [{"path": "motd", "destination": "/etc/motd"},
                                                   {"sha256": "b" * 64, "destination": "/etc/other"}]}}
    payload, uploads = ops.materialise(Uploader(), document, tmp_path)
    files = payload["spec"]["files"]
    assert files[0] == {"sha256": "a" * 64, "destination": "/etc/motd", "filename": "motd"}
    assert files[1] == {"sha256": "b" * 64, "destination": "/etc/other"}
    assert "path" not in str(payload) and str(tmp_path) not in str(payload)
    assert document["spec"]["files"][0]["path"] == "motd"  # the caller's document is untouched


def test_spec_document_round_trips_a_project():
    project = {"name": "demo", "description": "d", "repository": "team/demo", "template": "Minimal",
               "mode": "gui", "spec": {"base": {"distribution": "Alpine", "version": "3.22"}}}
    document = ops.spec_document(project)
    assert document["format"] == ops.SPEC_FORMAT and document["spec"] == project["spec"]
    advanced = ops.spec_document({**project, "mode": "advanced", "containerfile": "FROM x\n"})
    assert advanced["containerfile"] == "FROM x\n" and "spec" not in advanced
