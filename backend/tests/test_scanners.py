"""The scanner interface, and the default of not scanning at all.

These tests also pin the boundary the design depends on: nothing outside
layersmith/scanners may name a particular scanner.
"""

import subprocess
from pathlib import Path

import pytest

from layersmith import config, scanners
from layersmith.scanners import base
from layersmith.scanners.none import NoneScanner
from tests.test_api import client  # noqa: F401  (fixture)

PACKAGE = Path(__file__).resolve().parent.parent / "layersmith"


@pytest.fixture
def settings(tmp_path):
    return config.reset_for_tests(data_dir=tmp_path, scanner="auto")


@pytest.fixture(autouse=True)
def only_registered_scanners(monkeypatch):
    """Selection tests decide what is registered.

    Otherwise the result would depend on whether the machine running the
    tests happens to have a scanner installed.
    """
    monkeypatch.setattr(scanners, "SCANNERS", {"none": lambda _s: NoneScanner()})


def test_default_scanner_is_none(settings):
    assert isinstance(scanners.make_scanner(settings), NoneScanner)


def test_none_scanner_is_honest_about_being_unconfigured(settings):
    scanner = scanners.make_scanner(settings)
    available, detail = scanner.available()
    assert available is False
    assert "not scanned" in detail
    assert scanner.health()["available"] is False
    assert scanner.supported_kinds == ()
    assert scanner.supports_sbom is False


def test_none_scanner_refuses_rather_than_reporting_a_clean_image(settings, tmp_path):
    """An unscanned image is unknown, not clean: no empty 'no findings' result."""
    scanner = scanners.make_scanner(settings)
    archive = tmp_path / "image.tar"
    archive.write_bytes(b"")
    with pytest.raises(base.ScannerUnavailable):
        scanner.scan_image(archive, kinds=(base.VULNERABILITY,))
    with pytest.raises(base.ScannerUnavailable):
        scanner.generate_sbom(archive)


def test_explicit_none_selection(settings):
    settings.scanner = "none"
    assert isinstance(scanners.make_scanner(settings), NoneScanner)


def test_unknown_scanner_is_rejected(settings):
    settings.scanner = "definitely-not-a-scanner"
    with pytest.raises(ValueError) as exc:
        scanners.make_scanner(settings)
    assert "LAYERSMITH_SCANNER" in str(exc.value)


def test_auto_falls_back_to_none_when_nothing_is_reachable(settings):
    """A registered but unusable scanner must not be selected by auto."""

    class Unreachable(NoneScanner):
        name = "unreachable"

        def available(self):
            return False, "not running"

    scanners.SCANNERS["unreachable"] = lambda _s: Unreachable()
    settings.scanner = "auto"
    assert scanners.make_scanner(settings).name == "none"


def test_auto_selects_a_reachable_scanner(settings):
    class Reachable(NoneScanner):
        name = "reachable"
        supported_kinds = (base.VULNERABILITY,)
        supports_sbom = True

        def available(self):
            return True, "ready"

        def version(self):
            return "1.2.3"

    scanners.SCANNERS["reachable"] = lambda _s: Reachable()
    settings.scanner = "auto"
    scanner = scanners.make_scanner(settings)
    assert scanner.name == "reachable"

    described = scanners.describe(settings)
    assert described["name"] == "reachable"
    assert described["available"] is True
    assert described["version"] == "1.2.3"
    assert described["supported_kinds"] == ["vulnerability"]
    assert {row["name"] for row in described["scanners"]} == {"none", "reachable"}


def test_scan_after_build_is_false_while_no_scanner_is_available(settings):
    """Configured on, but reported off: the default install scans nothing."""
    assert settings.scan_after_build is True
    assert scanners.describe(settings)["scan_after_build"] is False


def test_probe_all_reports_every_registered_scanner(settings):
    rows = scanners.probe_all(settings)
    assert [row["name"] for row in rows] == list(scanners.SCANNERS)
    assert all({"name", "available", "detail"} <= set(row) for row in rows)


# --- the interface itself ---------------------------------------------------

def test_none_scanner_satisfies_the_protocol():
    scanner = NoneScanner()
    for method in ("available", "health", "version", "database_info", "scan_image", "generate_sbom"):
        assert callable(getattr(scanner, method))
    assert scanner.input_kind == base.INPUT_IMAGE_ARCHIVE


def test_archive_input_is_the_default_transport():
    """Socket-free scanning is the default; a socket must be an explicit choice."""
    assert NoneScanner.input_kind == base.INPUT_IMAGE_ARCHIVE
    for name, factory in scanners.SCANNERS.items():
        scanner = factory(config.load())
        assert scanner.input_kind in (base.INPUT_IMAGE_ARCHIVE, base.INPUT_IMAGE_REFERENCE), name


def test_finding_normalises_unknown_severity():
    assert base.Finding(kind=base.VULNERABILITY, severity="HIGH", identifier="CVE-1").severity == "high"
    assert base.Finding(kind=base.VULNERABILITY, severity="wat", identifier="CVE-2").severity == "unknown"
    assert base.Finding(kind=base.SECRET, severity="", identifier="aws-key").severity == "unknown"


def test_scan_result_counts_by_kind_and_severity():
    result = base.ScanResult(findings=[
        base.Finding(kind=base.VULNERABILITY, severity="critical", identifier="CVE-1"),
        base.Finding(kind=base.VULNERABILITY, severity="critical", identifier="CVE-2"),
        base.Finding(kind=base.VULNERABILITY, severity="low", identifier="CVE-3"),
        base.Finding(kind=base.SECRET, severity="high", identifier="aws-key"),
    ])
    assert result.counts() == {"critical": 2, "high": 0, "medium": 0, "low": 1, "unknown": 0}
    assert result.counts(base.SECRET)["high"] == 1
    assert result.count_of(base.SECRET) == 1
    assert result.count_of(base.MISCONFIGURATION) == 0


def test_severities_are_ordered_worst_first():
    assert base.SEVERITIES[0] == "critical"
    assert base.SEVERITIES[-1] == "unknown"


def test_no_scanner_is_named_outside_the_scanner_package():
    """LayerSmith must never be wired directly to one scanner.

    Everything outside layersmith/scanners talks to the interface, so a
    scanner's name appearing elsewhere in the package means something
    grew a hard dependency on it.
    """
    known = ["trivy", "grype", "clair", "syft"]
    if not known:
        pytest.skip("no concrete scanner registered yet")
    hits = subprocess.run(
        ["grep", "-ril", "--include=*.py", "-e", r"\(" + r"\|".join(known) + r"\)", str(PACKAGE)],
        capture_output=True, text=True,
    ).stdout.split()
    outside = [path for path in hits if "/scanners/" not in path]
    assert outside == [], f"scanner named outside layersmith/scanners: {outside}"


# --- configuration ----------------------------------------------------------

def test_scanner_options_are_passed_through_without_core_knowing_them(monkeypatch):
    monkeypatch.setenv("LAYERSMITH_SCANNER", "auto")
    monkeypatch.setenv("LAYERSMITH_SCANNER_TIMEOUT", "42")
    monkeypatch.setenv("LAYERSMITH_SCANNER_URL", "http://scanner:4954")
    monkeypatch.setenv("LAYERSMITH_SCANNER_SOME_FUTURE_KNOB", "yes")
    loaded = config.load()
    assert loaded.scanner == "auto"
    assert loaded.scanner_timeout == 42
    # The reserved variables configure LayerSmith, not the scanner.
    assert loaded.scanner_options == {"url": "http://scanner:4954", "some_future_knob": "yes"}


def test_scan_kinds_default_and_override(monkeypatch):
    assert config.load().scan_kinds == ("vulnerability", "secret")
    monkeypatch.setenv("LAYERSMITH_SCAN_KINDS", "vulnerability, misconfiguration ,")
    assert config.load().scan_kinds == ("vulnerability", "misconfiguration")


def test_scan_after_build_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("LAYERSMITH_SCAN_AFTER_BUILD", "false")
    assert config.load().scan_after_build is False
    monkeypatch.setenv("LAYERSMITH_SCAN_AFTER_BUILD", "on")
    assert config.load().scan_after_build is True


# --- API --------------------------------------------------------------------

def test_settings_endpoint_reports_the_scanner(client):
    scanner = client.get("/api/settings").json()["scanner"]
    assert scanner["name"] == "none"
    assert scanner["available"] is False
    assert scanner["scan_after_build"] is False
    assert scanner["database"]["updated_at"] is None


def test_scanner_endpoint(client):
    body = client.get("/api/scanner").json()
    assert body["name"] == "none"
    assert body["health"]["available"] is False
    # Nothing is asked for that the scanner cannot do.
    assert body["scan_kinds"] == []
    assert body["generate_sbom"] is False


def test_scanner_endpoint_never_leaks_scanner_credentials(client):
    """Scanner options may carry a token; the API must not echo one."""
    config.settings().scanner_options = {"token": "super-secret-token"}
    body = client.get("/api/scanner").text
    assert "super-secret-token" not in body
