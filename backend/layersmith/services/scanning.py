"""Scan orchestration, and getting the image to the scanner safely.

The security decision that shapes this file: **the scanner is handed an
exported archive, never the container runtime's socket.** A scanner parses
untrusted image contents for a living; giving it the ability to start
containers to save some I/O is a bad trade. So LayerSmith does the export.

Which archive a scan gets:

* **existing_export** - the build already has an export, it is still the
  archive that was written for this build's image digest (re-checked by
  checksum), and it is in a format the scanner can read. Reused as is, and
  never deleted by a scan.
* **temporary_export** - anything else. The image is exported to a
  uniquely-named file under the temporary directory, scanned, and deleted
  afterwards, whether the scan succeeded or failed. Asking for a scan never
  leaves a permanent "Download TAR" behind.

Before a temporary export, the image the reference points at is inspected
and its digest compared with the build record, so a tag that now resolves to
a different image is caught rather than silently scanned in its place.
Concurrent scans never share a temporary file: each gets its own name.
"""

import json
import queue
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from layersmith import config
from layersmith.backends.base import BuildError
from layersmith.models import Build, Scan, ScanFinding, utcnow
from layersmith.scanners import base as scanner_base
from layersmith.scanners.base import ScannerError, ScannerUnavailable
from layersmith.services.builds import sha256_file

EXISTING_EXPORT = "existing_export"
TEMPORARY_EXPORT = "temporary_export"


class ScanError(RuntimeError):
    """A scan could not be run; the message is meant for the user."""


@dataclass
class ImageArchive:
    path: Path
    source: str
    archive_format: str


class ScanService:
    """Runs scans, records them, and cleans up after itself."""

    def __init__(self, session_factory, backend, scanner, settings=None, log_bus=None):
        self.session_factory = session_factory
        self.backend = backend
        self.scanner = scanner
        self.settings = settings or config.settings()
        self.logs = log_bus
        self._queue: queue.Queue[str] = queue.Queue()
        self._worker: threading.Thread | None = None

    # -------------------------------------------------------------- queue

    def start(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._loop, name="layersmith-scanner", daemon=True)
            self._worker.start()

    def enqueue(self, scan_id: str) -> None:
        """Queue a scan. Scans run on their own worker: a slow scan of one
        image must not hold up the next build."""
        self._queue.put(scan_id)
        self.start()

    def _loop(self) -> None:
        while True:
            scan_id = self._queue.get()
            try:
                self.run(scan_id)
            except Exception as exc:  # a crash here must not kill the worker
                self._fail(scan_id, f"Scan worker error: {exc}")
            finally:
                self._queue.task_done()

    def after_build(self, build_id: str) -> None:
        """Called when a build finishes. Quiet when scanning is off or
        unconfigured: a build must never fail because of a scanner."""
        if not self.settings.scan_after_build:
            return
        try:
            if not self.scanner.available()[0]:
                return
            self.enqueue(self.create(build_id, reason="build"))
        except (ScanError, ScannerError, OSError):
            return

    # ------------------------------------------------------- the archive

    def _reusable_export(self, build: Build) -> Path | None:
        """The build's own export, if it is still the archive we recorded.

        Verified by checksum against the value stored when the image was
        exported, so an archive that was replaced, truncated or written by
        some other build is never scanned in place of this image.
        """
        if not (build.export_path and build.export_sha256):
            return None
        export = Path(build.export_path)
        if not export.is_file():
            return None
        if self.backend.archive_format not in getattr(self.scanner, "archive_formats", ()):
            return None  # readable file, wrong shape for this scanner
        if sha256_file(export) != build.export_sha256:
            return None
        return export

    def _export_format(self) -> str:
        """The first format the scanner reads that this runtime can write."""
        for wanted in getattr(self.scanner, "archive_formats", ()):
            if wanted in getattr(self.backend, "archive_formats", (self.backend.archive_format,)):
                return wanted
        raise ScanError(
            f"{self.scanner.name} reads {', '.join(self.scanner.archive_formats) or 'nothing'}, "
            f"which {self.backend.name} cannot write"
        )

    @contextmanager
    def archive_for(self, build_id: str, on_log=None):
        """Yield an ImageArchive for this build, cleaning up a temporary one."""
        with self.session_factory() as session:
            build = session.get(Build, build_id)
            if build is None:
                raise ScanError("That build no longer exists")
            if build.status != "ready" or not build.image_ref:
                raise ScanError("Only a finished build can be scanned")
            digest, reference = build.image_digest, build.image_ref
            reusable = self._reusable_export(build)

        if reusable is not None:
            yield ImageArchive(reusable, EXISTING_EXPORT, self.backend.archive_format)
            return

        archive_format = self._export_format()
        # A unique name per scan: two scans of the same image, running at the
        # same time, must not share or delete each other's file.
        temporary = Path(self.settings.tmp_dir) / f"scan-{uuid.uuid4().hex}.tar"
        temporary.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._verify_reference(reference, digest)
            self.backend.export(reference, temporary, on_log or (lambda _line: None),
                                archive_format=archive_format)
            yield ImageArchive(temporary, TEMPORARY_EXPORT, archive_format)
        finally:
            # Success or failure: a scan does not leave archives behind.
            temporary.unlink(missing_ok=True)

    def _verify_reference(self, reference: str, digest: str | None) -> None:
        """Refuse to export a reference that no longer is the image we mean."""
        if not digest:
            return  # nothing recorded to compare against; the build predates this
        try:
            current = self.backend.inspect(reference)
        except BuildError as exc:
            raise ScanError(f"The image {reference} is no longer available to scan: {exc}")
        if current.digest and current.digest != digest:
            raise ScanError(
                f"{reference} now points at a different image ({current.digest[:19]}… instead of "
                f"{digest[:19]}…), so it was not scanned. Rebuild to scan the current image."
            )

    # ------------------------------------------------------------- scans

    def create(self, build_id: str, reason: str = "manual", kinds=None) -> str:
        """Record a queued scan and return its id."""
        available, detail = self.scanner.available()
        if not available:
            raise ScanError(detail)
        wanted = tuple(kinds or self.settings.scan_kinds)
        supported = [kind for kind in wanted if kind in self.scanner.supported_kinds]
        if not supported:
            raise ScanError(f"{self.scanner.name} cannot look for {', '.join(wanted) or 'anything'}")
        with self.session_factory() as session:
            build = session.get(Build, build_id)
            if build is None or build.status != "ready":
                raise ScanError("Only a finished build can be scanned")
            scan = Scan(build_id=build_id, image_digest=build.image_digest, image_id=build.image_id,
                        reason=reason, kinds=list(supported), scanner=self.scanner.name, state="queued")
            session.add(scan)
            session.commit()
            return scan.id

    def run(self, scan_id: str) -> None:
        """Export if needed, scan, record findings. Never raises."""
        log = self._logger(scan_id)
        try:
            with self.session_factory() as session:
                scan = session.get(Scan, scan_id)
                if scan is None or scan.state != "queued":
                    return
                build_id, kinds = scan.build_id, tuple(scan.kinds or ())
                scan.state, scan.started_at = "exporting", utcnow()
                session.commit()

            with self.archive_for(build_id, on_log=log) as archive:
                self._set_state(scan_id, "scanning", archive_source=archive.source)
                log(f"Scanning {archive.path.name} ({archive.source.replace('_', ' ')})")
                result = self.scanner.scan_image(archive.path, kinds=kinds, on_log=log)
                sbom = None
                if self.settings.generate_sbom and self.scanner.supports_sbom:
                    log("Generating a CycloneDX SBOM")
                    sbom = self.scanner.generate_sbom(archive.path, on_log=log)

            self._record(scan_id, result, sbom)
        except (ScanError, ScannerError, ScannerUnavailable, BuildError) as exc:
            self._fail(scan_id, str(exc))
        except Exception as exc:  # a scan failing must never take the app down
            self._fail(scan_id, f"Unexpected error: {exc}")

    def scan_now(self, build_id: str, reason: str = "manual", kinds=None) -> str:
        scan_id = self.create(build_id, reason=reason, kinds=kinds)
        self.run(scan_id)
        return scan_id

    # ---------------------------------------------------------- recording

    def _record(self, scan_id: str, result, sbom) -> None:
        counts: dict[str, dict[str, int]] = {}
        for finding in result.findings:
            counts.setdefault(finding.kind, {}).setdefault(finding.severity, 0)
            counts[finding.kind][finding.severity] += 1

        with self.session_factory() as session:
            scan = session.get(Scan, scan_id)
            if scan is None:
                return
            mismatch = self._digest_mismatch(scan, result)
            if mismatch:
                scan.state, scan.error, scan.finished_at = "failed", mismatch, utcnow()
                session.commit()
                return

            report_path = self._write_json(scan_id, "report", result.report)
            scan.scanner = result.scanner or scan.scanner
            scan.scanner_version = result.scanner_version or ""
            scan.database_version = result.database.version
            scan.database_updated_at = result.database.updated_at
            scan.database_offline = bool(result.database.offline)
            scan.counts, scan.total = counts, len(result.findings)
            scan.report_path = str(report_path) if report_path else None
            if sbom is not None:
                sbom_path = self._write_json(scan_id, "sbom", sbom.document)
                scan.sbom_format, scan.sbom_components = sbom.format, sbom.component_count
                scan.sbom_path = str(sbom_path) if sbom_path else None
                scan.sbom_sha256 = sha256_file(sbom_path) if sbom_path else None
            scan.state, scan.finished_at = "completed", utcnow()
            session.add_all([
                ScanFinding(
                    scan_id=scan_id, kind=finding.kind, severity=finding.severity,
                    identifier=finding.identifier[:80], title=finding.title[:2000],
                    target=finding.target[:500], package_name=finding.package_name,
                    installed_version=finding.installed_version, fixed_version=finding.fixed_version,
                    url=finding.url, masked_match=finding.masked_match,
                )
                for finding in result.findings
            ])
            session.commit()

    @staticmethod
    def _digest_mismatch(scan: Scan, result) -> str | None:
        """Last line of defence: the scanner's own view of what it read."""
        seen = getattr(result, "scanned_image_id", None)
        if seen and scan.image_id and not _same_image(seen, scan.image_id):
            return (f"The scanner read a different image ({seen[:19]}…) than the one recorded for this "
                    f"build ({scan.image_id[:19]}…), so the result was discarded.")
        return None

    def _write_json(self, scan_id: str, kind: str, document) -> Path | None:
        if not document:
            return None
        directory = Path(self.settings.scan_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{scan_id}-{kind}.json"
        path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        return path

    def _set_state(self, scan_id: str, state: str, **fields) -> None:
        with self.session_factory() as session:
            scan = session.get(Scan, scan_id)
            if scan is None:
                return
            scan.state = state
            for key, value in fields.items():
                setattr(scan, key, value)
            session.commit()

    def _fail(self, scan_id: str, message: str) -> None:
        self._set_state(scan_id, "failed", error=message[:2000], finished_at=utcnow())

    def _logger(self, scan_id: str):
        def log(line: str) -> None:
            if self.logs is not None:
                self.logs.publish(f"scan:{scan_id}", {"type": "log", "line": line})
        return log

    # ------------------------------------------------------------ removal

    def delete_artifacts(self, scan: Scan) -> None:
        """Remove a scan's report and SBOM from disk."""
        for path in (scan.report_path, scan.sbom_path):
            if path:
                Path(path).unlink(missing_ok=True)


def _same_image(left: str, right: str) -> bool:
    """Compare two image identities written in different notations.

    A runtime may record `abc123…` where a scanner reports
    `sha256:abc123…`; that is the same image, and treating it as a mismatch
    would throw away every scan.
    """
    def hex_part(value: str) -> str:
        return str(value).strip().rsplit(":", 1)[-1].lower()

    return bool(hex_part(left)) and hex_part(left) == hex_part(right)


def sweep_temporary_exports(settings) -> int:
    """Delete scan exports left behind by a crash. Returns how many."""
    removed = 0
    for leftover in Path(settings.tmp_dir).glob("scan-*.tar"):
        try:
            leftover.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def summarise(scan: Scan) -> dict:
    """A scan as the API and UI see it."""
    vulnerabilities = (scan.counts or {}).get(scanner_base.VULNERABILITY, {})
    return {
        "id": scan.id,
        "build_id": scan.build_id,
        "image_digest": scan.image_digest,
        "state": scan.state,
        "reason": scan.reason,
        "error": scan.error,
        "kinds": scan.kinds or [],
        "scanner": scan.scanner,
        "scanner_version": scan.scanner_version,
        "database": {
            "version": scan.database_version,
            "updated_at": scan.database_updated_at.isoformat() if scan.database_updated_at else None,
            "offline": scan.database_offline,
        },
        # Troubleshooting detail: why one scan took far longer than another.
        "archive_source": scan.archive_source,
        "counts": scan.counts or {},
        "severity": {level: vulnerabilities.get(level, 0) for level in scanner_base.SEVERITIES},
        "total": scan.total,
        "sbom": {"format": scan.sbom_format, "components": scan.sbom_components,
                 "sha256": scan.sbom_sha256} if scan.sbom_format else None,
        "started_at": scan.started_at.isoformat() if scan.started_at else None,
        "finished_at": scan.finished_at.isoformat() if scan.finished_at else None,
        "duration_seconds": scan.duration_seconds,
        "created_at": scan.created_at.isoformat() if scan.created_at else None,
    }


def finding_row(finding: ScanFinding) -> dict:
    return {
        "id": finding.id, "kind": finding.kind, "severity": finding.severity,
        "identifier": finding.identifier, "title": finding.title, "target": finding.target,
        "package_name": finding.package_name, "installed_version": finding.installed_version,
        "fixed_version": finding.fixed_version, "url": finding.url,
        "masked_match": finding.masked_match,
    }
