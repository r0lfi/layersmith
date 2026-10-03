"""Operations shared by the CLI and the TUI.

Each function is one user-level action expressed as API calls. The CLI and
the TUI both call these; neither runs the other. Nothing here validates an
image definition: the server does that, and its answer is what the user sees.
"""

from __future__ import annotations

import copy
import json
import platform
import sys
import time
from pathlib import Path
from typing import Callable, Iterator

from layersmith_client import __version__
from layersmith_client.api import (
    ApiClient, ClientError, FileProblem, JobFailed, NotFound, Rejected, Unreachable, Unsupported,
)
from layersmith_client.config import ClientConfig
from layersmith_client.text import BUILD_FINISHED, SCAN_ACTIVE, clean

SPEC_FORMAT = "layersmith-project/v1"
#: Fields of a spec file that are sent to the server as they are.
PROJECT_FIELDS = ("name", "description", "repository", "template", "mode", "spec", "containerfile")

#: Seconds between status polls while waiting. Statuses change on the scale
#: of seconds, so polling faster only loads the server.
POLL_SECONDS = 2.0
#: Lines of log kept in memory by followers; older lines are dropped and the
#: drop is reported.
LOG_BUFFER_LINES = 5000
#: How much of a log to show first when attaching to a long build.
LOG_TAIL_BYTES = 64 * 1024
#: Air-gap bundles are made synchronously on the server; give it time.
BUNDLE_TIMEOUT = 3600.0


# ------------------------------------------------------- lookups by name

def resolve_project(client: ApiClient, ref: str) -> dict:
    """A project by id or by name."""
    ref = (ref or "").strip()
    if not ref:
        raise Rejected("A project id or name is required")
    if len(ref) == 36 and ref.count("-") == 4:
        try:
            return client.get(f"/projects/{ref}")
        except NotFound:
            pass
    for project in client.get("/projects"):
        if project.get("name") == ref:
            return client.get(f"/projects/{project['id']}")
    raise NotFound(f"No project with id or name {ref!r}", hint="List them with: layersmith projects list")


def resolve_build_id(client: ApiClient, ref: str) -> str:
    """A build id from an id, or from its number (`12` or `#12`)."""
    ref = (ref or "").strip()
    number = ref[1:] if ref.startswith("#") else ref
    if number.isdigit():
        for build in client.get("/builds", params={"limit": 200}):
            if str(build.get("number")) == number:
                return build["id"]
        raise NotFound(f"No build number {number} among the 200 most recent builds",
                       hint="Use the build id instead (layersmith builds list).")
    if not ref:
        raise Rejected("A build id or number is required")
    return ref


def get_build(client: ApiClient, ref: str) -> dict:
    return client.get(f"/builds/{resolve_build_id(client, ref)}")


# ------------------------------------------------------------ spec files

def load_spec_file(path: Path) -> dict:
    """Read a project spec file (JSON). See docs/client.md for the format."""
    path = Path(path).expanduser()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileProblem(f"Spec file not found: {path}")
    except ValueError as exc:
        raise Rejected(f"{path} is not valid JSON: {exc}")
    if not isinstance(document, dict):
        raise Rejected(f"{path} must contain a JSON object")
    fmt = document.get("format")
    if fmt not in (None, SPEC_FORMAT):
        raise Rejected(f"{path} has format {fmt!r}; this client reads {SPEC_FORMAT!r}")
    return document


def local_references(document: dict) -> list[tuple[str, int, str]]:
    """(section, index, local path) for every spec entry that names a local file."""
    found = []
    spec = document.get("spec") or {}
    for section in ("files", "tools"):
        for index, entry in enumerate(spec.get(section) or []):
            if isinstance(entry, dict) and entry.get("path"):
                found.append((section, index, str(entry["path"])))
    return found


def materialise(client: ApiClient, document: dict, base_dir: Path,
                on_upload: Callable[[str, dict], None] | None = None) -> tuple[dict, list[dict]]:
    """Upload the local files a spec names and return the payload for the server.

    A spec may say `"path": "./motd"` for a file on this machine. The file is
    uploaded through the API and the entry then carries the checksum, like an
    upload made in the web UI. A local path is never sent to the server: the
    server cannot see this machine's files.
    """
    payload = copy.deepcopy({key: document[key] for key in PROJECT_FIELDS if key in document})
    if document.get("containerfile_path"):
        local = (base_dir / document["containerfile_path"]).expanduser()
        payload["containerfile"] = read_text_file(local)
        payload.setdefault("mode", "advanced")
    uploads = []
    for section, index, local_path in local_references(document):
        local = Path(local_path).expanduser()
        if not local.is_absolute():
            local = base_dir / local
        result = client.upload(local)
        uploads.append(result)
        if on_upload:
            on_upload(local_path, result)
        entry = payload["spec"][section][index]
        entry.pop("path", None)
        entry["sha256"] = result["sha256"]
        entry.setdefault("filename", result.get("filename"))
        if section == "files":
            entry.setdefault("destination", f"/opt/{result.get('filename')}")
    return payload, uploads


def read_text_file(path: Path, limit: int = 1024 * 1024) -> str:
    path = Path(path).expanduser()
    if not path.is_file():
        raise FileProblem(f"Not a file: {path}")
    if path.stat().st_size > limit:
        raise FileProblem(f"{path} is larger than {limit // 1024} KiB")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise FileProblem(f"{path} is not UTF-8 text")


def spec_document(project: dict) -> dict:
    """A project as a spec file that recreates the same definition."""
    document = {"format": SPEC_FORMAT, "name": project["name"], "description": project.get("description") or "",
                "repository": project.get("repository"), "template": project.get("template") or "Custom",
                "mode": project.get("mode") or "gui"}
    if document["mode"] == "gui":
        document["spec"] = project.get("spec") or {}
    else:
        document["containerfile"] = project.get("containerfile") or ""
    return document


def create_from_document(client: ApiClient, document: dict, base_dir: Path, name: str | None = None,
                         on_upload=None) -> dict:
    payload, _ = materialise(client, document, base_dir, on_upload)
    if name:
        payload["name"] = name
    if not payload.get("name"):
        raise Rejected("The project needs a name (in the spec file or with --name)")
    return client.post("/projects", payload)


# ------------------------------------------------------------- builds

def start_build(client: ApiClient, project_id: str, version: str | None = None) -> dict:
    body = {"version": version} if version else {}
    return client.post(f"/projects/{project_id}/builds", body)


class LogFollower:
    """Reads a build log incrementally and resumes after a lost connection.

    Uses the server's offset-based log endpoint when it has one. Older
    servers only return the last part of the log with the build, so new text
    is found by comparing with what was already seen, and anything that
    scrolled out of that window is reported as skipped.
    """

    def __init__(self, client: ApiClient, build_id: str, tail_bytes: int | None = LOG_TAIL_BYTES):
        self.client = client
        self.build_id = build_id
        self.offset: int | None = None
        self.tail_bytes = tail_bytes
        self.finished = False
        self.status: str | None = None
        self._legacy_seen = ""
        self._partial = ""
        self.incremental = None

    def poll(self) -> Iterator[str]:
        """New complete lines since the last call. Raises ClientError on failure."""
        if self.incremental is None:
            self.incremental = self.client.has_feature("log-offset")
        if self.incremental:
            yield from self._poll_offset()
        else:
            yield from self._poll_legacy()

    def _poll_offset(self) -> Iterator[str]:
        while True:
            params: dict = {"offset": self.offset or 0}
            if self.offset is None and self.tail_bytes is not None:
                params = {"tail": self.tail_bytes}
            chunk = self.client.get(f"/builds/{self.build_id}/log", params=params)
            before = self.offset
            if self.offset is None and chunk.get("offset", 0) > 0:
                yield f"[{chunk['offset']} earlier bytes of the log not shown]"
            self.offset = chunk.get("next_offset", 0)
            self.status = chunk.get("status")
            lines = (self._partial + (chunk.get("text") or "")).split("\n")
            # The last element is an unfinished line (or "" after a newline);
            # it is kept until the rest of it arrives, or the log is complete.
            self._partial = lines.pop()
            yield from lines
            if chunk.get("complete"):
                if self._partial:
                    yield self._partial
                    self._partial = ""
                self.finished = True
                return
            if self.offset >= chunk.get("size", 0) or self.offset == before:
                return

    def _poll_legacy(self) -> Iterator[str]:
        build = self.client.get(f"/builds/{self.build_id}")
        self.status = build.get("status")
        log = build.get("log") or ""
        seen = self._legacy_seen
        if log.startswith(seen):
            new = log[len(seen):]
        else:
            # The server's window moved past what was seen: find the overlap.
            anchor = seen[-2000:]
            position = log.rfind(anchor) if anchor else -1
            if position >= 0:
                new = log[position + len(anchor):]
            else:
                yield "[part of the log was not shown: the server only returns its most recent part]"
                new = log
        self._legacy_seen = log
        lines = new.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        yield from lines
        if self.status in BUILD_FINISHED:
            self.finished = True


def wait_for_build(client: ApiClient, build_id: str, *, on_status: Callable[[str], None] | None = None,
                   on_line: Callable[[str], None] | None = None, poll: float = POLL_SECONDS,
                   sleep=time.sleep, reconnect_seconds: float = 300.0) -> dict:
    """Follow a build until it finishes and return its final record.

    Only waits: interrupting this (Ctrl+C) leaves the build running on the
    server. A lost connection is retried for `reconnect_seconds`, reported
    through on_status, and then raised as Unreachable - never as a result.
    """
    follower = LogFollower(client, build_id) if on_line else None
    last_status = None
    lost_since: float | None = None
    while True:
        try:
            if follower is not None and on_line is not None:
                for line in follower.poll():
                    on_line(line)
            build = client.get(f"/builds/{build_id}")
            lost_since = None
        except Unreachable:
            now = time.monotonic()
            lost_since = lost_since or now
            if now - lost_since > reconnect_seconds:
                raise
            if on_status:
                on_status("connection lost - retrying (the build continues on the server)")
            sleep(poll * 2)
            continue
        status = build.get("status")
        if status != last_status:
            last_status = status
            if on_status:
                on_status(status)
        if status in BUILD_FINISHED:
            if follower is not None and on_line is not None and not follower.finished:
                for line in follower.poll():
                    on_line(line)
            return build
        sleep(poll)


def raise_for_build(build: dict) -> None:
    if build.get("status") != "ready":
        raise JobFailed(f"Build #{build.get('number')} {build.get('status')}: "
                        f"{clean(build.get('error') or 'no reason recorded')}")


# --------------------------------------------------------------- scans

def start_scan(client: ApiClient, build_id: str, kinds: list[str] | None = None) -> dict:
    return client.post(f"/builds/{build_id}/scan", {"kinds": kinds} if kinds else {})


def wait_for_scan(client: ApiClient, build_id: str, scan_id: str, *, on_status=None, poll: float = POLL_SECONDS,
                  sleep=time.sleep) -> dict:
    last = None
    while True:
        scans = client.get(f"/builds/{build_id}/scans")
        scan = next((row for row in scans if row.get("id") == scan_id), None)
        if scan is None:
            raise NotFound(f"Scan {scan_id} is not in the history of build {build_id}")
        if scan.get("state") != last:
            last = scan.get("state")
            if on_status:
                on_status(last)
        if scan.get("state") not in SCAN_ACTIVE:
            return scan
        sleep(poll)


# ------------------------------------------------------------ downloads

def download_artifact(client: ApiClient, build: dict, kind: str, output: Path, overwrite: bool = False,
                      progress=None) -> dict:
    """Download a build's image archive (`export`) or air-gap bundle (`airgap`)."""
    flag = "has_export" if kind == "export" else "has_airgap"
    if not build.get(flag):
        what = "image archive" if kind == "export" else "air-gap bundle"
        hint = ("Create it first: layersmith bundle create " + build["id"]) if kind == "airgap" else \
            "The build has no archive on the server (not finished, failed, or the file was removed)."
        raise FileProblem(f"Build #{build.get('number')} has no {what} on the server", hint=hint)
    expected = build.get("export_sha256" if kind == "export" else "airgap_sha256")
    fallback = f"layersmith-build-{build.get('number')}-{'image.tar' if kind == 'export' else 'airgap.tar.gz'}"
    return client.download(f"/builds/{build['id']}/download/{kind}", Path(output), expected_sha256=expected,
                           overwrite=overwrite, fallback_name=fallback, progress=progress,
                           timeout=BUNDLE_TIMEOUT)


def create_bundle(client: ApiClient, build_id: str) -> dict:
    return client.post(f"/builds/{build_id}/airgap", {}, timeout=BUNDLE_TIMEOUT)


def download_sbom(client: ApiClient, scan: dict, output: Path, overwrite: bool = False) -> dict:
    sbom = scan.get("sbom")
    if not sbom:
        raise FileProblem("This scan has no SBOM")
    return client.download(f"/scans/{scan['id']}/sbom", Path(output), expected_sha256=sbom.get("sha256"),
                           overwrite=overwrite, fallback_name=f"sbom-{scan['id']}.json")


# --------------------------------------------------------------- doctor

def doctor(cfg: ClientConfig, client_factory: Callable[[], ApiClient]) -> list[dict]:
    """Read-only checks. Client facts and server facts are reported separately.

    Each row: scope (client|server), check, status (ok|warn|fail|unknown),
    detail. Anything that could not be determined is `unknown`, not `ok`.
    """
    rows = [
        {"scope": "client", "check": "Client version", "status": "ok", "detail": __version__},
        {"scope": "client", "check": "Python", "status": "ok",
         "detail": f"{platform.python_version()} ({sys.executable})"},
        {"scope": "client", "check": "Configured server", "status": "ok",
         "detail": f"{cfg.server} (from {cfg.sources.get('server', 'default')})"},
        {"scope": "client", "check": "Config file", "status": "ok" if not cfg.unknown_keys else "warn",
         "detail": f"{cfg.path}" + (" (exists)" if cfg.path and cfg.path.exists() else " (not created; defaults)")
         + (f"; unknown keys ignored: {', '.join(cfg.unknown_keys)}" if cfg.unknown_keys else "")},
    ]
    if cfg.ca_cert:
        rows.append({"scope": "client", "check": "CA bundle", "status": "ok" if Path(cfg.ca_cert).is_file() else "fail",
                     "detail": cfg.ca_cert})
    try:
        client = client_factory()
    except ClientError as exc:
        rows.append({"scope": "client", "check": "Connection settings", "status": "fail", "detail": str(exc)})
        rows.append({"scope": "server", "check": "API reachable", "status": "unknown",
                     "detail": "Not checked: the client could not be set up."})
        return rows
    with client:
        try:
            health = client.health()
        except ClientError as exc:
            rows.append({"scope": "server", "check": "API reachable", "status": "fail",
                         "detail": str(exc) + (f" - {exc.hint}" if exc.hint else "")})
            return rows
        rows.append({"scope": "server", "check": "API reachable", "status": "ok",
                     "detail": f"{client.server}/api/health answered"})
        version = clean(health.get("version"))
        rows.append({"scope": "server", "check": "Server version", "status": "ok",
                     "detail": f"{version} (revision {clean(health.get('revision'))[:12]})"})
        missing = [feature for feature in ("log-offset", "limits") if feature not in client.features()]
        rows.append({"scope": "server", "check": "API compatibility", "status": "warn" if missing else "ok",
                     "detail": ("All features this client uses are available" if not missing else
                                f"Server lacks {', '.join(missing)}: it is older than this client. Logs are "
                                "followed from the last part the server returns; everything else works.")})
        try:
            settings = client.get("/settings")
        except ClientError as exc:
            rows.append({"scope": "server", "check": "Settings", "status": "unknown", "detail": str(exc)})
            return rows
        backend = settings.get("build_backend") or {}
        rows.append({"scope": "server", "check": "Build backend",
                     "status": "ok" if backend.get("available") else "fail",
                     "detail": f"{clean(backend.get('name'))} (selected: {clean(backend.get('selection'))}) - "
                               f"{clean(backend.get('detail'))}"})
        storage = settings.get("storage") or {}
        if storage.get("total"):
            free = storage.get("free", 0) / storage["total"]
            rows.append({"scope": "server", "check": "Server storage", "status": "ok" if free > 0.1 else "warn",
                         "detail": f"{storage.get('free', 0) / 1e9:.1f} GB free of {storage['total'] / 1e9:.1f} GB "
                                   "(the server's data volume, not this machine)"})
        else:
            rows.append({"scope": "server", "check": "Server storage", "status": "unknown",
                         "detail": "Not reported"})
        scanner = settings.get("scanner") or {}
        if scanner.get("name") == "none":
            status, detail = "warn", "No scanner configured: images are built but not scanned. " \
                                     + clean(scanner.get("detail"))
        elif scanner.get("available"):
            status, detail = "ok", f"{clean(scanner.get('name'))} {clean(scanner.get('version'))}"
        elif scanner:
            status, detail = "fail", f"{clean(scanner.get('name'))} unavailable: {clean(scanner.get('detail'))}"
        else:
            status, detail = "unknown", "Not reported"
        rows.append({"scope": "server", "check": "Scanner", "status": status, "detail": detail})
    return rows


def require_feature(client: ApiClient, feature: str, what: str) -> None:
    if not client.has_feature(feature):
        raise Unsupported(f"The server does not support {what}",
                          hint="It is older than this client; upgrade the server (see layersmith doctor).")
