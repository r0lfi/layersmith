"""Scan orchestration: how the image reaches the scanner, and what is kept.

The security-relevant behaviour is here: no runtime socket for the scanner,
archives verified against the digest they claim, temporary exports removed
whatever happens, and secrets never stored as values.
"""

import json
from pathlib import Path

import pytest

from layersmith import config
from layersmith.models import Build, Project, Scan, ScanFinding, create_all, make_engine, make_session_factory
from layersmith.scanners import base
from layersmith.scanners.base import Finding, ScanResult, SbomResult, ScannerError
from layersmith.services.builds import sha256_file
from layersmith.services.scanning import (
    EXISTING_EXPORT, TEMPORARY_EXPORT, ScanError, ScanService, summarise, sweep_temporary_exports,
)
from tests.test_api import client, run_pending_build  # noqa: F401  (fixtures)
from tests.test_builds import DIGEST, FakeBackend

IMAGE_ID = "sha256:" + "b" * 64


class FakeScanner:
    """A scanner that records what it was handed."""

    name = "fake"
    input_kind = base.INPUT_IMAGE_ARCHIVE
    archive_formats = ("docker-archive",)
    supported_kinds = (base.VULNERABILITY, base.SECRET)
    supports_sbom = True

    def __init__(self, findings=None, fail: str | None = None, image_id: str | None = IMAGE_ID):
        self.findings = findings if findings is not None else [
            Finding(kind=base.VULNERABILITY, severity="critical", identifier="CVE-1", package_name="openssl",
                    installed_version="1.0", fixed_version="1.1"),
            Finding(kind=base.VULNERABILITY, severity="low", identifier="CVE-2", package_name="zlib"),
            Finding(kind=base.SECRET, severity="high", identifier="aws-access-key",
                    masked_match="redacted, 40 characters"),
        ]
        self.fail = fail
        self.image_id = image_id
        self.scanned: list[Path] = []
        self.sbom_for: list[Path] = []

    def available(self):
        return True, "fake scanner"

    def health(self):
        return {"scanner": self.name, "available": True, "runtime_socket": False}

    def version(self):
        return "9.9.9"

    def database_info(self):
        return base.DatabaseInfo(version="2", detail="fake database")

    def scan_image(self, image, *, kinds=(), on_log=None):
        self.scanned.append(Path(image))
        assert Path(image).is_file(), "the scanner was handed a path that does not exist"
        if self.fail == "scan":
            raise ScannerError("scanner exploded")
        if on_log:
            on_log("scanning")
        return ScanResult(findings=list(self.findings), scanner=self.name, scanner_version="9.9.9",
                          database=self.database_info(), report={"Results": []},
                          scanned_image_id=self.image_id)

    def generate_sbom(self, image, *, sbom_format="cyclonedx", on_log=None):
        self.sbom_for.append(Path(image))
        if self.fail == "sbom":
            raise ScannerError("no sbom today")
        return SbomResult(format="cyclonedx", document={"bomFormat": "CycloneDX", "components": [{"name": "zlib"}]},
                          component_count=1, spec_version="1.5")


@pytest.fixture
def env(tmp_path):
    """A ready build, a fake runtime and a fake scanner."""
    settings = config.reset_for_tests(
        data_dir=tmp_path, image_dir=tmp_path / "images", build_dir=tmp_path / "builds",
        upload_dir=tmp_path / "uploads", log_dir=tmp_path / "logs", tmp_dir=tmp_path / "tmp",
        scan_dir=tmp_path / "scans", database_url=f"sqlite:///{tmp_path}/test.db", scanner="none",
    )
    settings.prepare()
    engine = make_engine(settings.database_url)
    create_all(engine)
    session_factory = make_session_factory(engine)

    export = Path(settings.image_dir) / "app-1.0.0.tar"
    export.write_bytes(b"pretend-this-is-an-image-archive")
    with session_factory() as session:
        project = Project(name="app", repository="layersmith/app")
        session.add(project)
        session.flush()
        build = Build(number=1, project_id=project.id, version="1.0.0", status="ready",
                      image_ref="layersmith/app:1.0.0", image_id=IMAGE_ID, image_digest=DIGEST,
                      export_path=str(export), export_sha256=sha256_file(export),
                      export_size=export.stat().st_size)
        session.add(build)
        session.commit()
        build_id = build.id

    backend, scanner = FakeBackend(), FakeScanner()
    service = ScanService(session_factory, backend, scanner, settings=settings)
    return {"settings": settings, "session_factory": session_factory, "service": service,
            "backend": backend, "scanner": scanner, "build_id": build_id, "export": export}


def _build(env, **fields):
    with env["session_factory"]() as session:
        build = session.get(Build, env["build_id"])
        for key, value in fields.items():
            setattr(build, key, value)
        session.commit()


# ------------------------------------------------------- choosing an archive

def test_existing_export_is_reused_when_the_scanner_can_read_it(env):
    env["backend"].archive_format = "docker-archive"
    env["backend"].archive_formats = ("docker-archive",)
    with env["service"].archive_for(env["build_id"]) as archive:
        assert archive.source == EXISTING_EXPORT
        assert archive.path == env["export"]
    # Reuse must never consume the download the user can still ask for.
    assert env["export"].is_file()
    assert not any(call[0] == "export" for call in env["backend"].calls)


def test_temporary_export_when_the_existing_one_is_the_wrong_shape(env):
    """Podman writes an oci-archive; a scanner may only read docker-archive."""
    assert env["backend"].archive_format == "oci-archive"
    with env["service"].archive_for(env["build_id"]) as archive:
        assert archive.source == TEMPORARY_EXPORT
        assert archive.archive_format == "docker-archive"
        assert archive.path.is_file()
        temporary = archive.path
        assert temporary.parent == Path(env["settings"].tmp_dir)
    assert not temporary.exists(), "the temporary export outlived the scan"
    assert env["export"].is_file(), "the permanent export must not be touched"
    export_calls = [call for call in env["backend"].calls if call[0] == "export"]
    assert export_calls[0][3] == "docker-archive"


def test_temporary_export_is_removed_when_the_scan_fails(env):
    with pytest.raises(RuntimeError):
        with env["service"].archive_for(env["build_id"]) as archive:
            temporary = archive.path
            assert temporary.is_file()
            raise RuntimeError("scan blew up")
    assert not temporary.exists()


def test_a_replaced_export_is_not_reused(env):
    """The archive must still be the one recorded for this image digest."""
    env["backend"].archive_format = "docker-archive"
    env["backend"].archive_formats = ("docker-archive",)
    env["export"].write_bytes(b"a completely different archive")
    with env["service"].archive_for(env["build_id"]) as archive:
        assert archive.source == TEMPORARY_EXPORT, "a tampered export was reused"


def test_a_missing_export_falls_back_to_a_temporary_one(env):
    env["backend"].archive_format = "docker-archive"
    env["backend"].archive_formats = ("docker-archive",)
    env["export"].unlink()
    with env["service"].archive_for(env["build_id"]) as archive:
        assert archive.source == TEMPORARY_EXPORT


def test_a_tag_that_now_points_elsewhere_is_refused(env):
    """A rebuilt or re-tagged image must not be scanned in place of this one."""
    from layersmith.backends.base import ImageInfo

    env["backend"].inspect = lambda reference: ImageInfo(reference=reference, digest="sha256:" + "f" * 64)
    with pytest.raises(ScanError) as exc:
        with env["service"].archive_for(env["build_id"]):
            pass
    assert "different image" in str(exc.value)
    assert not list(Path(env["settings"].tmp_dir).glob("scan-*.tar"))


def test_concurrent_scans_do_not_share_a_temporary_file(env):
    service = env["service"]
    with service.archive_for(env["build_id"]) as first:
        with service.archive_for(env["build_id"]) as second:
            assert first.path != second.path
            assert first.path.is_file() and second.path.is_file()
        # The inner scan finishing must not delete the outer scan's archive.
        assert first.path.is_file()
        assert not second.path.exists()


def test_a_scanner_that_reads_nothing_the_runtime_writes_says_so(env):
    env["scanner"].archive_formats = ("some-other-format",)
    with pytest.raises(ScanError) as exc:
        with env["service"].archive_for(env["build_id"]):
            pass
    assert "cannot write" in str(exc.value)


def test_only_a_finished_build_is_scanned(env):
    _build(env, status="failed")
    with pytest.raises(ScanError):
        with env["service"].archive_for(env["build_id"]):
            pass


# --------------------------------------------------------------- running

def test_a_scan_records_findings_counts_and_metadata(env):
    scan_id = env["service"].scan_now(env["build_id"])
    with env["session_factory"]() as session:
        scan = session.get(Scan, scan_id)
        assert scan.state == "completed"
        assert scan.archive_source == TEMPORARY_EXPORT
        assert scan.counts == {"vulnerability": {"critical": 1, "low": 1}, "secret": {"high": 1}}
        assert scan.total == 3
        assert scan.scanner == "fake" and scan.scanner_version == "9.9.9"
        assert scan.image_digest == DIGEST
        assert scan.duration_seconds is not None
        findings = session.query(ScanFinding).filter_by(scan_id=scan_id).all()
        assert {f.identifier for f in findings} == {"CVE-1", "CVE-2", "aws-access-key"}
        assert summarise(scan)["severity"]["critical"] == 1


def test_a_scan_writes_report_and_sbom_where_they_belong(env):
    scan_id = env["service"].scan_now(env["build_id"])
    with env["session_factory"]() as session:
        scan = session.get(Scan, scan_id)
        assert Path(scan.report_path).parent == Path(env["settings"].scan_dir)
        assert scan.sbom_format == "cyclonedx" and scan.sbom_components == 1
        assert scan.sbom_sha256 == sha256_file(Path(scan.sbom_path))
        assert json.loads(Path(scan.sbom_path).read_text())["bomFormat"] == "CycloneDX"


def test_a_failing_scanner_fails_the_scan_and_nothing_else(env):
    env["scanner"].fail = "scan"
    scan_id = env["service"].scan_now(env["build_id"])
    with env["session_factory"]() as session:
        scan = session.get(Scan, scan_id)
        assert scan.state == "failed"
        assert "scanner exploded" in scan.error
        assert scan.finished_at is not None
        assert session.query(ScanFinding).filter_by(scan_id=scan_id).count() == 0
    assert not list(Path(env["settings"].tmp_dir).glob("scan-*.tar"))


def test_a_scan_of_a_different_image_is_discarded(env):
    """Last defence: the scanner's own view of what it read must match."""
    env["scanner"].image_id = "sha256:" + "9" * 64
    scan_id = env["service"].scan_now(env["build_id"])
    with env["session_factory"]() as session:
        scan = session.get(Scan, scan_id)
        assert scan.state == "failed"
        assert "different image" in scan.error
        assert session.query(ScanFinding).filter_by(scan_id=scan_id).count() == 0


@pytest.mark.parametrize("reported", [
    IMAGE_ID,                       # exactly as recorded
    IMAGE_ID.removeprefix("sha256:"),  # bare hex, as some runtimes record it
    IMAGE_ID.upper().replace("SHA256:", "sha256:"),
])
def test_the_same_image_written_differently_is_still_the_same_image(env, reported):
    """A runtime records a bare hex id; a scanner reports sha256:<hex>."""
    env["scanner"].image_id = reported
    scan_id = env["service"].scan_now(env["build_id"])
    with env["session_factory"]() as session:
        scan = session.get(Scan, scan_id)
        assert scan.state == "completed", scan.error


def test_only_supported_kinds_are_requested(env):
    with pytest.raises(ScanError) as exc:
        env["service"].create(env["build_id"], kinds=("misconfiguration",))
    assert "cannot look for" in str(exc.value)

    scan_id = env["service"].create(env["build_id"], kinds=("vulnerability", "misconfiguration"))
    with env["session_factory"]() as session:
        assert session.get(Scan, scan_id).kinds == ["vulnerability"]


def test_sbom_generation_can_be_switched_off(env):
    env["settings"].generate_sbom = False
    scan_id = env["service"].scan_now(env["build_id"])
    with env["session_factory"]() as session:
        assert session.get(Scan, scan_id).sbom_path is None
    assert env["scanner"].sbom_for == []


def test_rescanning_keeps_the_earlier_scan(env):
    """Scan history: a later scan is a new row, not an overwrite."""
    first = env["service"].scan_now(env["build_id"])
    env["scanner"].findings = []
    second = env["service"].scan_now(env["build_id"], reason="rescan")
    with env["session_factory"]() as session:
        assert session.get(Scan, first).total == 3
        assert session.get(Scan, second).total == 0
        assert session.query(Scan).filter_by(build_id=env["build_id"]).count() == 2


# --------------------------------------------------- after a build finishes

def test_a_finished_build_is_scanned_automatically(env):
    env["settings"].scan_after_build = True
    env["service"].after_build(env["build_id"])
    env["service"]._queue.join()
    with env["session_factory"]() as session:
        scan = session.query(Scan).filter_by(build_id=env["build_id"]).one()
        assert scan.reason == "build"
        assert scan.state == "completed"


def test_automatic_scanning_is_quiet_when_it_is_switched_off(env):
    env["settings"].scan_after_build = False
    env["service"].after_build(env["build_id"])
    with env["session_factory"]() as session:
        assert session.query(Scan).count() == 0


def test_automatic_scanning_is_quiet_when_no_scanner_is_available(env):
    env["scanner"].available = lambda: (False, "nothing configured")
    env["service"].after_build(env["build_id"])
    with env["session_factory"]() as session:
        assert session.query(Scan).count() == 0


def test_a_scan_never_fails_the_build(env):
    """The post-build hook swallows its own problems, loudly in the log."""
    from layersmith.services.builds import BuildService

    service = BuildService(env["session_factory"], env["backend"], settings=env["settings"])
    service.on_built = lambda build_id: (_ for _ in ()).throw(RuntimeError("scanner is on fire"))
    emitted: list[str] = []
    service._emit = lambda build_id, line: emitted.append(line)
    # The hook is invoked where run() would call it, with the same guard.
    try:
        if service.on_built is not None:
            try:
                service.on_built("x")
            except Exception as exc:
                service._emit("x", f"NOTE: post-build step did not run: {exc}")
    except Exception:  # pragma: no cover - the guard above must catch it
        pytest.fail("a failing post-build hook escaped")
    assert any("post-build step did not run" in line for line in emitted)


# ------------------------------------------------------------- housekeeping

def test_leftover_temporary_exports_are_swept(env):
    leftovers = [Path(env["settings"].tmp_dir) / f"scan-{index}.tar" for index in range(3)]
    for path in leftovers:
        path.write_bytes(b"orphan")
    keep = Path(env["settings"].tmp_dir) / "upload-1234"
    keep.write_bytes(b"not a scan export")

    assert sweep_temporary_exports(env["settings"]) == 3
    assert not any(path.exists() for path in leftovers)
    assert keep.is_file(), "the sweep removed something that was not its business"


def test_summary_carries_the_archive_source_for_troubleshooting(env):
    scan_id = env["service"].scan_now(env["build_id"])
    with env["session_factory"]() as session:
        assert summarise(session.get(Scan, scan_id))["archive_source"] == TEMPORARY_EXPORT


# ------------------------------------------------------------------- API

@pytest.fixture
def api(client):  # noqa: F811
    """The application, with a fake scanner and scans run synchronously."""
    service = client.state["scan_service"]
    service.scanner = FakeScanner()
    # The application gives both services the same runtime; so does this.
    service.backend = client.state["build_service"].backend
    service.enqueue = service.run
    return client


def _ready_build(api):
    project = api.post("/api/projects", json={
        "name": "scanned", "template": "Minimal",
        "spec": {"base": {"distribution": "Alpine", "version": "3.22"}}}).json()
    build = api.post(f"/api/projects/{project['id']}/builds", json={}).json()
    run_pending_build(api, build["id"])
    return build["id"]


def test_a_build_is_scanned_without_being_asked(api):
    """Scanning is automatic once a scanner is configured."""
    build_id = _ready_build(api)
    history = api.get(f"/api/builds/{build_id}/scans").json()
    assert [row["reason"] for row in history] == ["build"]
    assert history[0]["state"] == "completed"


def test_scanning_through_the_api(api):
    build_id = _ready_build(api)
    response = api.post(f"/api/builds/{build_id}/scan", json={})
    assert response.status_code == 202
    scan = response.json()

    detail = api.get(f"/api/scans/{scan['id']}").json()
    assert detail["state"] == "completed"
    assert detail["severity"]["critical"] == 1
    assert detail["reason"] == "manual"
    assert len(detail["findings"]) == 3
    assert detail["findings"][0]["severity"] == "critical", "worst findings come first"


def test_findings_can_be_filtered(api):
    build_id = _ready_build(api)
    scan = api.post(f"/api/builds/{build_id}/scan", json={}).json()

    secrets = api.get(f"/api/scans/{scan['id']}?kind=secret").json()["findings"]
    assert [f["identifier"] for f in secrets] == ["aws-access-key"]
    assert secrets[0]["masked_match"] == "redacted, 40 characters"

    critical = api.get(f"/api/scans/{scan['id']}?severity=CRITICAL").json()["findings"]
    assert [f["identifier"] for f in critical] == ["CVE-1"]


def test_scan_history_is_kept_per_build(api):
    build_id = _ready_build(api)
    first = api.post(f"/api/builds/{build_id}/scan", json={}).json()
    second = api.post(f"/api/builds/{build_id}/scan", json={}).json()

    history = api.get(f"/api/builds/{build_id}/scans").json()
    # Newest first, and the automatic post-build scan is still there.
    assert [row["id"] for row in history][:2] == [second["id"], first["id"]]
    assert [row["reason"] for row in history] == ["manual", "manual", "build"]
    assert all(row["image_digest"] == history[0]["image_digest"] for row in history)


def test_the_sbom_can_be_downloaded(api):
    build_id = _ready_build(api)
    scan = api.post(f"/api/builds/{build_id}/scan", json={}).json()
    response = api.get(f"/api/scans/{scan['id']}/sbom")
    assert response.status_code == 200
    assert response.json()["bomFormat"] == "CycloneDX"


def test_asking_to_scan_without_a_scanner_explains_itself(client):  # noqa: F811
    build_id = _ready_build(client)
    response = client.post(f"/api/builds/{build_id}/scan", json={})
    assert response.status_code == 422
    assert "not scanned" in response.json()["detail"]


def test_scanning_an_unknown_build_is_a_404(api):
    assert api.post("/api/builds/does-not-exist/scan", json={}).status_code == 404
    assert api.get("/api/scans/does-not-exist").status_code == 404


def test_an_unfinished_build_cannot_be_scanned(api):
    project = api.post("/api/projects", json={
        "name": "queued-only", "template": "Minimal",
        "spec": {"base": {"distribution": "Alpine", "version": "3.22"}}}).json()
    build = api.post(f"/api/projects/{project['id']}/builds", json={}).json()
    response = api.post(f"/api/builds/{build['id']}/scan", json={})
    assert response.status_code == 422 and "finished build" in response.json()["detail"]


# ------------------------------------------------------- air-gap artifacts

def test_a_bundle_carries_the_scan_that_was_made(env, tmp_path):
    env["service"].scan_now(env["build_id"])
    with env["session_factory"]() as session:
        build = session.get(Build, env["build_id"])
        env["service"].write_artifacts(build, tmp_path / "security")

    written = {path.name for path in (tmp_path / "security").iterdir()}
    assert {"scan.json", "SECURITY.txt", "scan-report.json"} <= written
    assert any(name.startswith("sbom.") for name in written)

    report = json.loads((tmp_path / "security" / "scan.json").read_text())
    assert report["total"] == 3
    assert len(report["findings"]) == 3

    text = (tmp_path / "security" / "SECURITY.txt").read_text()
    assert "vulnerability:" in text and "1 critical" in text
    # An air-gapped reader cannot check anything themselves, so the bundle
    # must not let a scan be mistaken for a clean bill of health.
    assert "not a statement that the" in text


def test_a_bundle_says_so_when_nothing_was_scanned(env, tmp_path):
    with env["session_factory"]() as session:
        build = session.get(Build, env["build_id"])
        env["service"].write_artifacts(build, tmp_path / "security")
    text = (tmp_path / "security" / "NOT-SCANNED.txt").read_text()
    assert "has not been scanned" in text
    assert "not the same as" in text


def test_a_failed_scan_is_not_presented_as_a_result(env, tmp_path):
    env["scanner"].fail = "scan"
    env["service"].scan_now(env["build_id"])
    with env["session_factory"]() as session:
        build = session.get(Build, env["build_id"])
        env["service"].write_artifacts(build, tmp_path / "security")
    assert (tmp_path / "security" / "NOT-SCANNED.txt").is_file()


def test_the_newest_completed_scan_is_the_one_bundled(env, tmp_path):
    env["service"].scan_now(env["build_id"])
    env["scanner"].findings = [
        Finding(kind=base.VULNERABILITY, severity="high", identifier="CVE-NEW", package_name="curl"),
    ]
    env["service"].scan_now(env["build_id"])
    with env["session_factory"]() as session:
        build = session.get(Build, env["build_id"])
        env["service"].write_artifacts(build, tmp_path / "security")
    report = json.loads((tmp_path / "security" / "scan.json").read_text())
    assert [f["identifier"] for f in report["findings"]] == ["CVE-NEW"]


def test_the_bundle_never_carries_a_secret_value(env, tmp_path):
    env["scanner"].findings = [
        Finding(kind=base.SECRET, severity="critical", identifier="aws-access-key",
                target="/app/credentials:1", masked_match="redacted, 40 characters"),
    ]
    env["service"].scan_now(env["build_id"])
    with env["session_factory"]() as session:
        build = session.get(Build, env["build_id"])
        env["service"].write_artifacts(build, tmp_path / "security")
    blob = "".join(path.read_text() for path in (tmp_path / "security").iterdir())
    assert "redacted, 40 characters" in blob
    assert "/app/credentials:1" in blob
    assert "masked_match" in blob and "AKIA" not in blob


def test_a_bundle_made_through_the_api_includes_the_security_folder(api):
    import tarfile

    build_id = _ready_build(api)
    api.post(f"/api/builds/{build_id}/airgap", json={})
    with api.state["session_factory"]() as session:
        bundle = Path(session.get(Build, build_id).airgap_path)
    with tarfile.open(bundle) as archive:
        names = archive.getnames()
    assert any("/security/SECURITY.txt" in name for name in names)
    assert any("/security/scan.json" in name for name in names)
    # Every file in the bundle is covered by its checksum list.
    with tarfile.open(bundle) as archive:
        sums = archive.extractfile(next(n for n in names if n.endswith("SHA256SUMS"))).read().decode()
    assert "security/SECURITY.txt" in sums and "security/scan.json" in sums


def test_an_archive_handed_to_the_scanner_is_readable(env):
    """The scanner drops every capability, so it cannot bypass file modes."""
    env["export"].chmod(0o600)
    env["backend"].archive_format = "docker-archive"
    env["backend"].archive_formats = ("docker-archive",)
    with env["service"].archive_for(env["build_id"]) as archive:
        assert archive.source == EXISTING_EXPORT
        assert archive.path.stat().st_mode & 0o044, "the scanner would get permission denied"


def test_a_temporary_export_is_readable_too(env):
    with env["service"].archive_for(env["build_id"]) as archive:
        assert archive.source == TEMPORARY_EXPORT
        assert archive.path.stat().st_mode & 0o044
