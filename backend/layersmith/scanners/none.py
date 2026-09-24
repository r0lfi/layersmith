"""The default: no scanner.

LayerSmith ships with scanning switched off. Nothing about a build changes,
and the UI says plainly that images are not being scanned rather than
implying they were found clean. An unscanned image is unknown, not safe.
"""

from pathlib import Path

from layersmith.scanners.base import (
    DOCKER_ARCHIVE, INPUT_IMAGE_ARCHIVE, DatabaseInfo, LogSink, ScanResult, SbomResult, ScannerUnavailable,
)

NOT_CONFIGURED = (
    "No scanner configured. Images are built and exported normally, but they "
    "are not scanned, so nothing is known about their vulnerabilities."
)


class NoneScanner:
    """Reports that scanning is disabled, and refuses to pretend otherwise."""

    name = "none"
    input_kind = INPUT_IMAGE_ARCHIVE
    archive_formats = (DOCKER_ARCHIVE,)
    supported_kinds: tuple[str, ...] = ()
    supports_sbom = False

    def available(self) -> tuple[bool, str]:
        return False, NOT_CONFIGURED

    def health(self) -> dict:
        return {"scanner": self.name, "available": False, "detail": NOT_CONFIGURED}

    def version(self) -> str:
        return ""

    def database_info(self) -> DatabaseInfo:
        return DatabaseInfo(detail="No scanner configured.")

    def scan_image(self, image: Path | str, *, kinds: tuple[str, ...] = (),
                   on_log: LogSink | None = None) -> ScanResult:
        raise ScannerUnavailable(NOT_CONFIGURED)

    def generate_sbom(self, image: Path | str, *, sbom_format: str = "cyclonedx",
                      on_log: LogSink | None = None) -> SbomResult:
        raise ScannerUnavailable(NOT_CONFIGURED)
