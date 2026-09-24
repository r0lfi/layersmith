"""The Trivy backend: how it is invoked, and what it refuses to carry.

Nothing here runs Trivy. What matters is the command LayerSmith constructs -
above all what privileges it does *not* hand to the scanner - and that a
report's secret values never survive the trip back.
"""

import json
from pathlib import Path

import pytest

from layersmith import config
from layersmith.scanners import base
from layersmith.scanners.trivy import DEFAULT_IMAGE, MOUNT_PATH, TrivyScanner, _mask, _redact


def scanner(**options):
    settings = config.reset_for_tests(data_dir=Path("/data"), scanner="trivy",
                                      scanner_options={"mode": "container", **options})
    return TrivyScanner(settings)


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "image.tar"
    path.write_bytes(b"archive")
    return path


# ------------------------------------------------- what the scanner is given

def test_the_scanner_container_gets_no_runtime_socket(archive):
    argv = " ".join(scanner()._argv(["image"], archive))
    assert "docker.sock" not in argv
    assert "podman.sock" not in argv
    assert "/var/run" not in argv
    assert "DOCKER_HOST" not in argv


def test_the_scanner_container_gets_no_extra_privileges(archive):
    argv = scanner()._argv(["image"], archive)
    assert "--privileged" not in argv
    assert "--cap-add" not in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"


def test_the_archive_is_mounted_read_only_and_alone(archive):
    argv = scanner()._argv(["image"], archive)
    mounts = [argv[index + 1] for index, value in enumerate(argv) if value == "-v"]
    assert f"{archive}:{MOUNT_PATH}:ro,z" in mounts
    # Only the archive and the database cache; no host directories.
    assert len(mounts) == 2
    assert all(mount.endswith((":ro,z", "/root/.cache")) for mount in mounts)


def test_selinux_relabelling_stays_shared(archive):
    """'Z' would make the reused export unreadable to LayerSmith itself."""
    argv = " ".join(scanner()._argv(["image"], archive))
    assert ":ro,z" in argv and ":ro,Z" not in argv


def test_the_default_image_is_pinned():
    assert ":" in DEFAULT_IMAGE and not DEFAULT_IMAGE.endswith(":latest")
    assert scanner().image == DEFAULT_IMAGE


def test_a_version_check_mounts_no_archive():
    argv = scanner()._argv(["version", "--format", "json"])
    mounts = [argv[index + 1] for index, value in enumerate(argv) if value == "-v"]
    assert len(mounts) == 1 and mounts[0].endswith("/root/.cache")


def test_no_network_is_requested_unless_one_is_configured(archive):
    assert "--network" not in scanner()._argv(["image"], archive)
    assert "--network" in scanner(network="scan-net")._argv(["image"], archive)


def test_binary_mode_runs_trivy_directly(tmp_path, archive):
    settings = config.reset_for_tests(data_dir=tmp_path, scanner="trivy",
                                      scanner_options={"mode": "binary", "binary": "/usr/bin/trivy"})
    argv = TrivyScanner(settings)._argv(["image", "--input", str(archive)])
    assert argv[0] == "/usr/bin/trivy"
    assert "run" not in argv


# ----------------------------------------------------------- configuration

@pytest.mark.parametrize("value", [
    "trivy; id", "trivy && id", "trivy$(id)", "trivy`id`", "--privileged", "-v/:/host",
    "trivy image\nrm -rf /", "trivy image|sh",
])
def test_a_dangerous_scanner_image_is_refused(value):
    with pytest.raises(ValueError) as exc:
        scanner(image=value)
    assert "LAYERSMITH_SCANNER_IMAGE" in str(exc.value)


@pytest.mark.parametrize("value", ["cache; id", "-v/:/host", "../escape", "a b"])
def test_a_dangerous_cache_volume_is_refused(value):
    with pytest.raises(ValueError):
        scanner(cache_volume=value)


@pytest.mark.parametrize("value", ["net; id", "--privileged", "host net"])
def test_a_dangerous_network_is_refused(value):
    with pytest.raises(ValueError):
        scanner(network=value)


def test_paths_are_translated_for_a_sibling_container(tmp_path):
    """LayerSmith's /data is not the runtime's /data when it runs in a container."""
    settings = config.reset_for_tests(data_dir=Path("/data"), scanner="trivy", scanner_options={
        "mode": "container", "host_data_dir": "/srv/layersmith"})
    translated = TrivyScanner(settings)._host_path(Path("/data/tmp/scan-1.tar"))
    assert translated == "/srv/layersmith/tmp/scan-1.tar"


def test_a_path_outside_the_data_directory_is_left_alone():
    settings = config.reset_for_tests(data_dir=Path("/data"), scanner="trivy", scanner_options={
        "mode": "container", "host_data_dir": "/srv/layersmith"})
    assert TrivyScanner(settings)._host_path(Path("/elsewhere/image.tar")) == "/elsewhere/image.tar"


def test_offline_mode_asks_trivy_not_to_phone_home(monkeypatch, archive):
    trivy = scanner(offline="1")
    seen: dict = {}

    def fake_json(args, arc=None, on_log=None):
        seen["args"] = args
        return {"Results": [], "Trivy": {"Version": "0.74.0"}}

    monkeypatch.setattr(trivy, "_json", fake_json)
    monkeypatch.setattr(trivy, "database_info", lambda: base.DatabaseInfo())
    trivy.scan_image(archive, kinds=(base.VULNERABILITY,))
    assert "--skip-db-update" in seen["args"] and "--offline-scan" in seen["args"]


def test_availability_does_not_pull_the_image_behind_your_back(monkeypatch):
    """The image being present locally is what opts a host in."""
    calls: list[list[str]] = []

    class Done:
        returncode = 1
        stdout = stderr = ""

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return Done()

    monkeypatch.setattr("layersmith.scanners.trivy.subprocess.run", fake_run)
    available, detail = scanner().available()
    assert available is False
    assert "pull" in detail and DEFAULT_IMAGE in detail
    assert all("pull" not in argv for argv in calls), "available() pulled an image"


def test_health_states_that_no_socket_is_used():
    health = scanner().health()
    assert health["runtime_socket"] is False
    assert health["mode"] == "container"


# ------------------------------------------------------ reading the report

REPORT = {
    "Trivy": {"Version": "0.74.0"},
    "Metadata": {"ImageID": "sha256:" + "b" * 64},
    "Results": [{
        "Target": "app (alpine 3.19)",
        "Vulnerabilities": [{
            "VulnerabilityID": "CVE-2024-1", "PkgName": "busybox", "InstalledVersion": "1.36",
            "FixedVersion": "1.37", "Severity": "MEDIUM", "Title": "something",
            "PrimaryURL": "https://example.invalid/cve",
        }],
        "Secrets": [{
            "RuleID": "aws-access-key-id", "Severity": "CRITICAL", "Title": "AWS Access Key",
            "StartLine": 12, "Match": "AKIAIOSFODNN7EXAMPLE and more", "Code": {"Lines": ["secret"]},
        }],
        "Misconfigurations": [{"ID": "DS002", "Severity": "HIGH", "Title": "Root user"}],
    }],
}


def test_a_report_becomes_scanner_neutral_findings(monkeypatch, archive):
    trivy = scanner()
    monkeypatch.setattr(trivy, "_json", lambda *a, **k: json.loads(json.dumps(REPORT)))
    monkeypatch.setattr(trivy, "database_info", lambda: base.DatabaseInfo(version="2"))
    result = trivy.scan_image(archive, kinds=(base.VULNERABILITY, base.SECRET, base.MISCONFIGURATION))

    assert result.counts() == {"critical": 0, "high": 0, "medium": 1, "low": 0, "unknown": 0}
    assert result.count_of(base.SECRET) == 1 and result.count_of(base.MISCONFIGURATION) == 1
    assert result.scanned_image_id == REPORT["Metadata"]["ImageID"]

    vulnerability = next(f for f in result.findings if f.kind == base.VULNERABILITY)
    assert (vulnerability.identifier, vulnerability.package_name, vulnerability.fixed_version) == \
           ("CVE-2024-1", "busybox", "1.37")


def test_a_secret_value_never_leaves_the_scanner(monkeypatch, archive):
    trivy = scanner()
    monkeypatch.setattr(trivy, "_json", lambda *a, **k: json.loads(json.dumps(REPORT)))
    monkeypatch.setattr(trivy, "database_info", lambda: base.DatabaseInfo())
    result = trivy.scan_image(archive, kinds=(base.SECRET,))

    secret = next(f for f in result.findings if f.kind == base.SECRET)
    assert secret.identifier == "aws-access-key-id"
    assert secret.masked_match == "redacted, 29 characters"
    # Not in the finding, and not in the report that gets written to disk.
    serialised = json.dumps(result.report) + json.dumps(secret.__dict__)
    assert "AKIAIOSFODNN7EXAMPLE" not in serialised


def test_the_image_build_history_is_dropped_from_the_stored_report():
    """A RUN command can carry a secret, and the report is kept on disk."""
    report = json.loads(json.dumps(REPORT))
    report["Metadata"]["ImageConfig"] = {"history": [
        {"created_by": "/bin/sh -c printf 'token=AKIA3KQP7ZXWVU2NLMRT' > /root/.aws/credentials"}]}
    redacted = _redact(report)
    assert "AKIA3KQP7ZXWVU2NLMRT" not in json.dumps(redacted)
    # What the scan is actually about survives.
    assert redacted["Metadata"]["ImageID"] == REPORT["Metadata"]["ImageID"]
    assert redacted["Results"][0]["Vulnerabilities"][0]["VulnerabilityID"] == "CVE-2024-1"


def test_redaction_keeps_the_finding_but_drops_the_value():
    redacted = _redact(json.loads(json.dumps(REPORT)))
    secret = redacted["Results"][0]["Secrets"][0]
    assert secret["RuleID"] == "aws-access-key-id" and secret["StartLine"] == 12
    assert secret["Match"] == "[redacted by LayerSmith]"
    assert secret["Code"] == "[redacted by LayerSmith]"


def test_masking_describes_a_secret_without_carrying_it():
    assert _mask("hunter2") == "redacted, 7 characters"
    assert _mask(None) is None
    assert "hunter2" not in (_mask("hunter2") or "")


def test_unknown_severities_do_not_become_low(monkeypatch, archive):
    report = json.loads(json.dumps(REPORT))
    report["Results"][0]["Vulnerabilities"][0]["Severity"] = "SOMETHING_NEW"
    trivy = scanner()
    monkeypatch.setattr(trivy, "_json", lambda *a, **k: report)
    monkeypatch.setattr(trivy, "database_info", lambda: base.DatabaseInfo())
    result = trivy.scan_image(archive, kinds=(base.VULNERABILITY,))
    assert result.counts()["unknown"] == 1


def test_database_age_is_read_from_the_version_output(monkeypatch):
    trivy = scanner()
    monkeypatch.setattr(trivy, "_version_payload", lambda: {
        "Version": "0.74.0",
        "VulnerabilityDB": {"Version": 2, "UpdatedAt": "2026-09-23T20:20:57Z",
                            "NextUpdate": "2026-09-24T20:20:57Z"},
    })
    database = trivy.database_info()
    assert database.version == "2"
    assert database.updated_at.year == 2026
    assert "2026-09-23" in database.detail
    assert database.age_seconds > 0


def test_a_missing_database_is_described_not_invented(monkeypatch):
    trivy = scanner()
    monkeypatch.setattr(trivy, "_version_payload", lambda: {"Version": "0.74.0"})
    database = trivy.database_info()
    assert database.updated_at is None
    assert "downloaded on the first scan" in database.detail


def test_scanning_a_missing_archive_is_refused(tmp_path):
    with pytest.raises(base.ScannerError):
        scanner().scan_image(tmp_path / "nope.tar", kinds=(base.VULNERABILITY,))


def test_an_unsupported_sbom_format_is_refused(archive):
    with pytest.raises(base.ScannerError):
        scanner().generate_sbom(archive, sbom_format="spdx")


def test_a_named_data_volume_is_mounted_read_only_for_compose(tmp_path):
    """In compose both containers see the same volume, so no host path is needed."""
    settings = config.reset_for_tests(data_dir=tmp_path, scanner="trivy", scanner_options={
        "mode": "container", "data_volume": "layersmith-data"})
    trivy = TrivyScanner(settings)
    archive = tmp_path / "tmp" / "scan-1.tar"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"x")

    argv = trivy._argv(["image"], archive)
    mounts = [argv[index + 1] for index, value in enumerate(argv) if value == "-v"]
    assert f"layersmith-data:{tmp_path}:ro,z" in mounts
    assert len(mounts) == 2  # the data volume and the database cache, nothing else
    # Trivy reads it at the path it already has, so nothing has to be rewritten.
    assert trivy._input_path(archive) == str(archive)


def test_an_archive_outside_the_data_volume_is_bind_mounted(tmp_path):
    settings = config.reset_for_tests(data_dir=tmp_path / "data", scanner="trivy", scanner_options={
        "mode": "container", "data_volume": "layersmith-data"})
    (tmp_path / "data").mkdir()
    outside = tmp_path / "elsewhere.tar"
    outside.write_bytes(b"x")
    trivy = TrivyScanner(settings)
    assert trivy._archive_mount(outside) == f"{outside}:{MOUNT_PATH}:ro,z"
    assert trivy._input_path(outside) == MOUNT_PATH


@pytest.mark.parametrize("value", ["vol; id", "-v/:/host", "../escape"])
def test_a_dangerous_data_volume_is_refused(value):
    with pytest.raises(ValueError):
        scanner(data_volume=value)
