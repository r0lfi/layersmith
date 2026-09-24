"""Scanner selection.

LAYERSMITH_SCANNER picks the scanner:

    auto    (default) use a scanner if one is reachable, otherwise none
    none    never scan

Additional scanners register themselves in SCANNERS. `auto` means an
operator who starts the optional scanner container gets scanning without
editing configuration, and everyone else gets a LayerSmith that builds
images and says clearly that it is not scanning them.

Nothing outside this package names a particular scanner. Callers ask the
settings for a scanner, check `available()`, and work through
ScannerBackend.
"""

from layersmith.scanners.base import (  # noqa: F401
    INPUT_IMAGE_ARCHIVE, INPUT_IMAGE_REFERENCE, MISCONFIGURATION, SECRET, SEVERITIES, VULNERABILITY,
    DatabaseInfo, Finding, SbomResult, ScanResult, ScannerBackend, ScannerError, ScannerUnavailable,
)
from layersmith.scanners.none import NoneScanner
from layersmith.scanners.trivy import TrivyScanner

#: name -> factory taking Settings. Order is the `auto` preference order;
#: `none` is the fallback and is never chosen by preference.
SCANNERS = {
    "trivy": lambda settings: TrivyScanner(settings),
    "none": lambda settings: NoneScanner(),
}


def make_scanner(settings) -> ScannerBackend:
    """Return the configured scanner, or NoneScanner when there is none.

    Like build backend selection, this never fails on an unusable scanner:
    the scanner is returned anyway so the Settings page can explain what is
    wrong instead of the application refusing to start.
    """
    choice = (getattr(settings, "scanner", None) or "auto").lower()
    if choice in SCANNERS:
        return SCANNERS[choice](settings)
    if choice != "auto":
        raise ValueError(
            f"LAYERSMITH_SCANNER must be auto, {' or '.join(SCANNERS)}, not {choice!r}"
        )

    for name, factory in SCANNERS.items():
        if name == "none":
            continue
        scanner = factory(settings)
        if scanner.available()[0]:
            return scanner
    return NoneScanner()


def probe_all(settings) -> list[dict]:
    """Availability of every scanner, for the Settings page."""
    results = []
    for name, factory in SCANNERS.items():
        scanner = factory(settings)
        available, detail = scanner.available()
        results.append({"name": name, "available": available, "detail": detail})
    return results


def describe(settings) -> dict:
    """Scanner status for the API: what is in use and what it can do."""
    scanner = make_scanner(settings)
    available, detail = scanner.available()
    database = scanner.database_info()
    return {
        "name": scanner.name,
        "selection": (getattr(settings, "scanner", None) or "auto").lower(),
        "available": available,
        "detail": detail,
        "version": scanner.version(),
        "supported_kinds": list(scanner.supported_kinds),
        "supports_sbom": scanner.supports_sbom,
        "scan_after_build": bool(getattr(settings, "scan_after_build", False)) and available,
        "database": {
            "version": database.version,
            "updated_at": database.updated_at.isoformat() if database.updated_at else None,
            "offline": database.offline,
            "detail": database.detail,
        },
        "scanners": probe_all(settings),
    }
