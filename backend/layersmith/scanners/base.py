"""The scanner backend interface.

Scanning is optional. LayerSmith builds, exports and bundles images perfectly
well with no scanner configured, and nothing outside this package knows which
scanner is in use - the same arrangement as build backends.

A scanner declares how it wants the image handed to it:

    INPUT_IMAGE_ARCHIVE   an exported OCI/docker archive on disk (default)
    INPUT_IMAGE_REFERENCE a reference the scanner resolves itself

The archive form is the default on purpose. It lets the scanner run in its
own container with no access to the build runtime's socket: LayerSmith
exports the image and hands over a single file. A scanner that genuinely
needs the runtime can declare the reference form, but that is then an
explicit, documented choice rather than a convenience.

Preparing that archive - reusing the stored export when it belongs to the
exact same image digest, or making a temporary one and removing it
afterwards - is the service layer's job, not the scanner's, so a future
backend with a different transport does not inherit those mechanics.
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

#: What a scanner wants to be given.
INPUT_IMAGE_ARCHIVE = "image_archive"
INPUT_IMAGE_REFERENCE = "image_reference"

#: Finding categories. A scanner reports only what it supports.
VULNERABILITY = "vulnerability"
SECRET = "secret"
MISCONFIGURATION = "misconfiguration"

#: Ordered worst first, which is also the display order.
SEVERITIES = ("critical", "high", "medium", "low", "unknown")

LogSink = Callable[[str], None]


class ScannerError(RuntimeError):
    """A scanner operation failed; the message is meant for the user."""


class ScannerUnavailable(ScannerError):
    """The scanner is not configured or cannot be reached."""


@dataclass
class DatabaseInfo:
    """State of the scanner's vulnerability data."""

    version: str | None = None
    updated_at: datetime | None = None
    next_update_at: datetime | None = None
    #: True when the data came from an offline import rather than a download.
    offline: bool = False
    detail: str = ""

    @property
    def age_seconds(self) -> float | None:
        if self.updated_at is None:
            return None
        reference = datetime.now(self.updated_at.tzinfo)
        return (reference - self.updated_at).total_seconds()


@dataclass
class Finding:
    """One result. Fields a scanner does not report stay None."""

    kind: str
    severity: str
    identifier: str
    title: str = ""
    target: str = ""
    package_name: str | None = None
    installed_version: str | None = None
    fixed_version: str | None = None
    url: str | None = None
    #: Never the secret itself: scanners report a match, we keep a redaction.
    masked_match: str | None = None

    def __post_init__(self):
        self.severity = (self.severity or "unknown").lower()
        if self.severity not in SEVERITIES:
            self.severity = "unknown"


@dataclass
class ScanResult:
    """Everything one scan produced."""

    findings: list[Finding] = field(default_factory=list)
    scanner: str = ""
    scanner_version: str = ""
    database: DatabaseInfo = field(default_factory=DatabaseInfo)
    #: Raw scanner output, stored verbatim for troubleshooting and bundles.
    report: dict | None = None

    def counts(self, kind: str = VULNERABILITY) -> dict[str, int]:
        counts = {severity: 0 for severity in SEVERITIES}
        for finding in self.findings:
            if finding.kind == kind:
                counts[finding.severity] += 1
        return counts

    def count_of(self, kind: str) -> int:
        return sum(1 for finding in self.findings if finding.kind == kind)


@dataclass
class SbomResult:
    """A software bill of materials, in an open format."""

    #: cyclonedx today; the model allows spdx without changing callers.
    format: str
    document: dict
    component_count: int = 0
    spec_version: str = ""


class ScannerBackend(Protocol):
    """What every scanner implements. Mirrors BuildBackend deliberately."""

    name: str
    #: How the image should be handed over; see the module docstring.
    input_kind: str
    #: Finding kinds this scanner can produce.
    supported_kinds: tuple[str, ...]
    #: Whether it can produce an SBOM at all.
    supports_sbom: bool

    def available(self) -> tuple[bool, str]:
        """(usable, human-readable detail) for the settings page."""

    def health(self) -> dict:
        """Connectivity and configuration detail, for 'Test connection'."""

    def version(self) -> str:
        """Scanner version, or '' when unknown."""

    def database_info(self) -> DatabaseInfo:
        """Vulnerability database state."""

    def scan_image(self, image: Path | str, *, kinds: tuple[str, ...],
                   on_log: LogSink | None = None) -> ScanResult:
        """Scan an archive (or reference, per input_kind) and return findings."""

    def generate_sbom(self, image: Path | str, *, sbom_format: str = "cyclonedx",
                      on_log: LogSink | None = None) -> SbomResult:
        """Produce an SBOM for the same image."""
