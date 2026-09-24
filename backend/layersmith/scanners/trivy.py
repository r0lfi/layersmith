"""Trivy scanner backend.

Everything Trivy-specific in LayerSmith lives in this file. The rest of the
application knows only ScannerBackend.

**Trivy is never installed into the LayerSmith application image.** It runs
one of two ways:

    container  (default) LayerSmith runs the Trivy image for one scan and
               throws the container away
    binary     a `trivy` executable on the host, for native installs

In container mode LayerSmith starts Trivy through the same runtime it already
uses to build images. That is LayerSmith spending a privilege it already has;
the Trivy container itself gets none of it:

    * no Docker or Podman socket,
    * no --privileged, no added capabilities - all of them dropped,
    * no-new-privileges,
    * read-only mount of one file: the image archive being scanned,
    * a named volume for the vulnerability database, so it is downloaded once.

Trivy reads a docker-archive tar or an OCI layout directory; it cannot read
an oci-archive tar, which is what Podman writes by default. `archive_formats`
says so, and the scan service exports into a format Trivy can actually read
rather than this file reaching back into the build pipeline.
"""

import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from layersmith.scanners.base import (
    DOCKER_ARCHIVE, INPUT_IMAGE_ARCHIVE, MISCONFIGURATION, SECRET, VULNERABILITY,
    DatabaseInfo, Finding, LogSink, SbomResult, ScanResult, ScannerError, ScannerUnavailable,
)

#: Pinned by default: a scanner that changes under you is a poor scanner.
#: Override with LAYERSMITH_SCANNER_IMAGE.
DEFAULT_IMAGE = "ghcr.io/aquasecurity/trivy:0.74.0"
DEFAULT_CACHE_VOLUME = "layersmith-trivy-db"

#: Where the archive and the database live inside the Trivy container.
MOUNT_PATH = "/scan/image"
CACHE_PATH = "/root/.cache"

KIND_TO_SCANNER = {VULNERABILITY: "vuln", SECRET: "secret", MISCONFIGURATION: "misconfig"}

#: Trivy image references are LayerSmith configuration, not user input, but
#: they still end up in an argv list, so they are checked like everything else.
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}$")
VOLUME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
NETWORK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _truthy(value: str | None, default: bool = False) -> bool:
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "on")


class TrivyScanner:
    """Scans an exported image archive with Trivy."""

    name = "trivy"
    input_kind = INPUT_IMAGE_ARCHIVE
    #: docker-archive only: see the module docstring.
    archive_formats = (DOCKER_ARCHIVE,)
    supported_kinds = (VULNERABILITY, SECRET, MISCONFIGURATION)
    supports_sbom = True

    def __init__(self, settings):
        options = dict(getattr(settings, "scanner_options", None) or {})
        self.timeout = int(getattr(settings, "scanner_timeout", 600) or 600)
        self.mode = (options.get("mode") or "auto").lower()
        self.binary = options.get("binary") or "trivy"
        self.image = options.get("image") or DEFAULT_IMAGE
        self.runtime = options.get("runtime") or getattr(settings, "podman_binary", "podman")
        self.cache_volume = options.get("cache_volume") or DEFAULT_CACHE_VOLUME
        self.network = options.get("network") or ""
        # SELinux: 'z' relabels shared, so the archive stays readable by
        # LayerSmith itself. 'Z' would give it a private label and break the
        # download of an export we reused.
        self.mount_flags = options.get("mount_flags") or "ro,z"
        self.offline = _truthy(options.get("offline"))
        # When LayerSmith runs in a container and starts sibling containers,
        # a path inside LayerSmith is not the path the runtime sees. Two ways
        # out: name the volume that holds the data directory (compose), or
        # give the host path it is bind-mounted from.
        self.data_volume = options.get("data_volume") or ""
        self.host_data_dir = options.get("host_data_dir") or ""
        self.data_dir = Path(getattr(settings, "data_dir", "/data"))
        for value, pattern, label in ((self.image, IMAGE_RE, "LAYERSMITH_SCANNER_IMAGE"),
                                      (self.cache_volume, VOLUME_RE, "LAYERSMITH_SCANNER_CACHE_VOLUME"),
                                      (self.data_volume or "data", VOLUME_RE, "LAYERSMITH_SCANNER_DATA_VOLUME"),
                                      (self.network or "bridge", NETWORK_RE, "LAYERSMITH_SCANNER_NETWORK")):
            if not pattern.match(value):
                raise ValueError(f"{label} is not a valid value: {value!r}")

    # ------------------------------------------------------------ plumbing

    def _host_path(self, path: Path) -> str:
        """Translate a LayerSmith path into one the container runtime sees."""
        path = Path(path)
        if not self.host_data_dir:
            return str(path)
        try:
            return str(Path(self.host_data_dir) / path.relative_to(self.data_dir))
        except ValueError:
            # Outside the data directory: nothing to translate, and guessing
            # would be worse than handing over the path as it stands.
            return str(path)

    def _use_container(self) -> bool:
        if self.mode == "container":
            return True
        if self.mode == "binary":
            return False
        return shutil.which(self.binary) is None

    def _argv(self, trivy_args: list[str], archive: Path | None = None) -> list[str]:
        """Full argv for one Trivy invocation, container or binary."""
        if not self._use_container():
            return [self.binary, *trivy_args]
        run = [self.runtime, "run", "--rm",
               "--security-opt", "no-new-privileges",
               "--cap-drop", "ALL",
               "-v", f"{self.cache_volume}:{CACHE_PATH}"]
        if self.network:
            run += ["--network", self.network]
        if archive is not None:
            run += ["-v", self._archive_mount(archive)]
        return [*run, self.image, *trivy_args]

    def _archive_mount(self, archive: Path) -> str:
        """The single -v that gives Trivy the archive, and nothing else."""
        if self.data_volume and self._inside_data_dir(archive):
            # Compose: the archive is in a named volume both containers can
            # see, so the volume goes in read-only and the path is unchanged.
            return f"{self.data_volume}:{self.data_dir}:{self.mount_flags}"
        return f"{self._host_path(archive)}:{MOUNT_PATH}:{self.mount_flags}"

    def _inside_data_dir(self, archive: Path) -> bool:
        try:
            Path(archive).resolve().relative_to(self.data_dir.resolve())
            return True
        except (ValueError, OSError):
            return False

    def _input_path(self, archive: Path) -> str:
        """Where the archive appears to Trivy."""
        if not self._use_container():
            return str(archive)
        if self.data_volume and self._inside_data_dir(archive):
            return str(archive)
        return MOUNT_PATH

    def _run(self, trivy_args: list[str], archive: Path | None = None,
             on_log: LogSink | None = None, timeout: int | None = None) -> str:
        argv = self._argv(trivy_args, archive)
        if on_log:
            on_log("$ " + " ".join(argv))
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout or self.timeout)
        except FileNotFoundError:
            raise ScannerUnavailable(f"{argv[0]} is not installed")
        except subprocess.TimeoutExpired:
            raise ScannerError(f"Trivy timed out after {(timeout or self.timeout) // 60} minutes")
        if on_log:
            for line in (done.stderr or "").splitlines():
                on_log(line)
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or "").strip().splitlines()
            raise ScannerError("Trivy failed: " + (detail[-1][:500] if detail else f"exit {done.returncode}"))
        return done.stdout

    def _json(self, trivy_args: list[str], archive: Path | None = None,
              on_log: LogSink | None = None) -> dict:
        output = self._run(trivy_args, archive, on_log)
        try:
            return json.loads(output or "{}")
        except ValueError:
            raise ScannerError("Trivy returned output that is not JSON")

    # ------------------------------------------------------------- backend

    def available(self) -> tuple[bool, str]:
        if not self._use_container():
            try:
                version = self._version_payload().get("Version", "")
            except (ScannerError, OSError) as exc:
                return False, f"{self.binary} is not usable: {exc}"
            return True, f"{self.binary} {version}"

        # Do not pull several hundred megabytes behind the operator's back:
        # the image being present locally is what opts this host in.
        try:
            present = subprocess.run([self.runtime, "image", "exists", self.image],
                                     capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"{self.runtime} is not usable: {exc}"
        if present.returncode != 0:
            return False, (f"{self.image} is not present. Run "
                           f"`{self.runtime} pull {self.image}` to enable scanning.")
        try:
            version = self._version_payload().get("Version", "")
        except ScannerError as exc:
            return False, str(exc)
        return True, f"trivy {version} ({self.image})"

    def _version_payload(self) -> dict:
        return self._json(["version", "--format", "json"])

    def version(self) -> str:
        try:
            return str(self._version_payload().get("Version") or "")
        except (ScannerError, OSError):
            return ""

    def health(self) -> dict:
        available, detail = self.available()
        return {
            "scanner": self.name,
            "available": available,
            "detail": detail,
            "mode": "container" if self._use_container() else "binary",
            "image": self.image if self._use_container() else "",
            "runtime": self.runtime if self._use_container() else "",
            "offline": self.offline,
            # No socket is mounted into the scanner; stated so it can be seen.
            "runtime_socket": False,
        }

    def database_info(self) -> DatabaseInfo:
        try:
            payload = self._version_payload()
        except (ScannerError, OSError) as exc:
            return DatabaseInfo(detail=str(exc))
        database = payload.get("VulnerabilityDB") or {}
        updated = _parse_time(database.get("UpdatedAt"))
        return DatabaseInfo(
            version=str(database.get("Version")) if database.get("Version") is not None else None,
            updated_at=updated,
            next_update_at=_parse_time(database.get("NextUpdate")),
            offline=self.offline,
            detail=(f"Trivy database v{database.get('Version')}, updated "
                    f"{updated.strftime('%Y-%m-%d %H:%M UTC')}" if updated else
                    "No vulnerability database yet; it is downloaded on the first scan."),
        )

    # ---------------------------------------------------------------- scan

    def scan_image(self, image, *, kinds=(VULNERABILITY,), on_log: LogSink | None = None) -> ScanResult:
        archive = Path(image)
        if not archive.exists():
            raise ScannerError(f"There is no archive to scan at {archive}")
        requested = [KIND_TO_SCANNER[kind] for kind in kinds if kind in KIND_TO_SCANNER]
        if not requested:
            raise ScannerError("No scan kind was requested that Trivy supports")

        args = ["image", "--input", self._input_path(archive),
                "--scanners", ",".join(requested), "--format", "json", "--quiet",
                "--timeout", f"{self.timeout}s"]
        if self.offline:
            args += ["--skip-db-update", "--skip-java-db-update", "--offline-scan"]
        report = self._json(args, archive, on_log)
        # Findings first, then redact: _mask must measure the secret Trivy
        # found, not the placeholder that replaces it.
        findings = list(_findings(report))
        report = _redact(report)

        return ScanResult(findings=findings, scanner=self.name,
                          scanner_version=str((report.get("Trivy") or {}).get("Version") or ""),
                          database=self.database_info(), report=report,
                          scanned_image_id=(report.get("Metadata") or {}).get("ImageID"))

    def generate_sbom(self, image, *, sbom_format: str = "cyclonedx", on_log: LogSink | None = None) -> SbomResult:
        if sbom_format != "cyclonedx":
            raise ScannerError(f"Trivy is asked here for CycloneDX, not {sbom_format}")
        archive = Path(image)
        if not archive.exists():
            raise ScannerError(f"There is no archive to describe at {archive}")
        args = ["image", "--input", self._input_path(archive),
                "--format", "cyclonedx", "--quiet", "--timeout", f"{self.timeout}s"]
        if self.offline:
            args += ["--skip-db-update", "--offline-scan"]
        document = self._json(args, archive, on_log)
        return SbomResult(format="cyclonedx", document=document,
                          component_count=len(document.get("components") or []),
                          spec_version=str(document.get("specVersion") or ""))


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _findings(report: dict):
    """Flatten a Trivy report into scanner-neutral findings."""
    for result in report.get("Results") or []:
        target = str(result.get("Target") or "")
        for row in result.get("Vulnerabilities") or []:
            yield Finding(
                kind=VULNERABILITY,
                severity=str(row.get("Severity") or "unknown"),
                identifier=str(row.get("VulnerabilityID") or ""),
                title=str(row.get("Title") or row.get("Description") or "")[:500],
                target=target,
                package_name=row.get("PkgName"),
                installed_version=row.get("InstalledVersion"),
                fixed_version=row.get("FixedVersion") or None,
                url=row.get("PrimaryURL"),
            )
        for row in result.get("Secrets") or []:
            yield Finding(
                kind=SECRET,
                severity=str(row.get("Severity") or "unknown"),
                identifier=str(row.get("RuleID") or "secret"),
                title=str(row.get("Title") or "")[:500],
                target=f"{target}:{row.get('StartLine')}" if row.get("StartLine") else target,
                # Never the value. Trivy already returns a partially masked
                # match; LayerSmith stores a length and nothing else.
                masked_match=_mask(row.get("Match")),
            )
        for row in result.get("Misconfigurations") or []:
            yield Finding(
                kind=MISCONFIGURATION,
                severity=str(row.get("Severity") or "unknown"),
                identifier=str(row.get("ID") or ""),
                title=str(row.get("Title") or "")[:500],
                target=target,
                url=row.get("PrimaryURL"),
            )


#: Fields in a Trivy report that carry the matched secret itself.
SECRET_VALUE_FIELDS = ("Match", "Code", "Lines")
REDACTED = "[redacted by LayerSmith]"


def _redact(report: dict) -> dict:
    """Strip secret values out of the report before anything stores it.

    The stored report is written to disk, can be read back through the API
    and goes into an air-gap bundle. A detected secret must be reported as a
    location and a rule, never as the value itself.

    Two places carry values:

    * the secret findings themselves, and
    * `Metadata.ImageConfig`, which holds the image's build history - every
      RUN command, and so any secret that was passed on one. LayerSmith
      already records the Containerfile it built from, so nothing is lost by
      dropping it here.
    """
    for result in report.get("Results") or []:
        for row in result.get("Secrets") or []:
            for field in SECRET_VALUE_FIELDS:
                if field in row:
                    row[field] = REDACTED
    metadata = report.get("Metadata")
    if isinstance(metadata, dict) and "ImageConfig" in metadata:
        metadata["ImageConfig"] = REDACTED
    return report


def _mask(match) -> str | None:
    """Describe a secret match without carrying the secret anywhere."""
    if not match:
        return None
    return f"redacted, {len(str(match))} characters"
