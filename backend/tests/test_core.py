import pytest

from layersmith.core import catalog, containerfile
from layersmith.core.spec import (
    InvalidSpec, export_filename, image_ref, next_patch, parse_reference,
    resolve_packages, validate_spec, version_key,
)

PROJECT = {"name": "network-tools", "description": "Packet tools"}


def spec(**overrides):
    base = {"base": {"distribution": "AlmaLinux", "version": "10"}, "presets": ["Network Tools"]}
    base.update(overrides)
    return validate_spec(base)


# ---------------------------------------------------------------- catalog

@pytest.mark.parametrize("distribution,family,package", [
    ("AlmaLinux", "rpm", "bind-utils"),
    ("Ubuntu", "deb", "dnsutils"),
    ("Alpine", "apk", "bind-tools"),
])
def test_capability_maps_to_the_distro_package(distribution, family, package):
    version = next(iter(catalog.DISTROS[distribution]["versions"]))
    resolved, _ = resolve_packages(spec(base={"distribution": distribution, "version": version},
                                        presets=[], packages=["dns-tools"]))
    assert resolved == [package]
    assert catalog.family_of(distribution) == family


def test_capability_without_a_package_warns_instead_of_failing():
    resolved, warnings = resolve_packages(spec(presets=[], packages=["venv", "git"]))
    assert resolved == ["git"]
    assert any("venv" in w and "custom tool" in w for w in warnings)


def test_preset_tools_warn_until_configured():
    _, warnings = resolve_packages(spec(presets=["Kubernetes"]))
    assert any("kubectl" in w for w in warnings)


def test_every_template_and_preset_resolves_on_every_distribution():
    for distribution, entry in catalog.DISTROS.items():
        version = next(iter(entry["versions"]))
        for template in catalog.TEMPLATES:
            merged = {**template["spec"], "base": {"distribution": distribution, "version": version}}
            packages, _ = resolve_packages(validate_spec(merged))
            assert all(isinstance(p, str) and p for p in packages)


# ------------------------------------------------------------- references

@pytest.mark.parametrize("reference,expected", [
    ("alpine:3.22", {"registry": None, "repository": "alpine", "tag": "3.22"}),
    ("docker.io/library/almalinux:10", {"registry": "docker.io", "repository": "library/almalinux", "tag": "10"}),
    ("quay.io/centos/centos:stream10", {"registry": "quay.io", "repository": "centos/centos", "tag": "stream10"}),
    ("localhost:5000/app", {"registry": "localhost:5000", "repository": "app", "tag": None}),
])
def test_parse_reference(reference, expected):
    parsed = parse_reference(reference)
    for key, value in expected.items():
        assert parsed[key] == value
    assert parsed["source"] == reference


def test_parse_reference_keeps_digest():
    digest = "sha256:" + "a" * 64
    assert parse_reference(f"alpine@{digest}")["digest"] == digest


@pytest.mark.parametrize("reference", [
    "alpine:3.22; rm -rf /", "alpine:$(id)", "alpine:3.22 && whoami", "../etc/passwd",
    "alpine@sha256:zzzz", "alpine:tag with space", "", "-alpine:3.22",
])
def test_parse_reference_rejects_junk(reference):
    with pytest.raises(InvalidSpec):
        parse_reference(reference)


# -------------------------------------------------------------- spec rules

@pytest.mark.parametrize("bad", [
    {"packages": ["git; rm -rf /"]},
    {"packages": ["$(id)"]},
    {"files": [{"sha256": "a" * 64, "destination": "/etc/../root/.ssh/authorized_keys"}]},
    {"files": [{"sha256": "a" * 64, "destination": "relative/path"}]},
    {"files": [{"sha256": "nothex", "destination": "/opt/app"}]},
    {"env": [{"name": "BAD NAME", "value": "x"}]},
    {"env": [{"name": "OK", "value": "line1\nline2"}]},
    {"workdir": "/proc/self"},
    {"architecture": "riscv"},
    {"presets": ["Does Not Exist"]},
    {"base": {"distribution": "AlmaLinux", "version": "7"}},
    {"base": {"distribution": "Nonexistent Linux", "version": "1"}},
    {"scripts": {"unknown_hook": "echo hi"}},
    {"tests": ["a" * 501]},
])
def test_validate_spec_rejects(bad):
    with pytest.raises(InvalidSpec):
        spec(**bad)


def test_secret_looking_env_is_refused_with_guidance():
    with pytest.raises(InvalidSpec) as excinfo:
        spec(env=[{"name": "REGISTRY_PASSWORD", "value": "hunter2"}])
    assert "runtime" in str(excinfo.value)


def test_validate_spec_fills_defaults():
    result = spec()
    assert result["base"]["source"] == "docker.io/library/almalinux:10"
    assert result["base"]["family"] == "rpm"
    assert result["architecture"] == "amd64"
    assert result["tag_latest"] is False


def test_custom_base_needs_an_explicit_family():
    with pytest.raises(InvalidSpec):
        validate_spec({"base": {"source": "registry.example.com/team/base:1"}})
    ok = validate_spec({"base": {"source": "registry.example.com/team/base:1", "family": "deb"}})
    assert ok["base"]["source"] == "registry.example.com/team/base:1"


def test_versions_and_filenames():
    assert next_patch("1.2.9") == "1.2.10"
    assert sorted(["1.10.0", "1.9.0"], key=version_key) == ["1.9.0", "1.10.0"]
    assert image_ref("layersmith/network-tools", "1.0.0") == "layersmith/network-tools:1.0.0"
    assert export_filename("layersmith/network-tools", "1.2.0") == "layersmith-network-tools-1.2.0.tar"


# ---------------------------------------------------------- containerfile

def test_generate_pins_digest_and_installs_packages():
    digest = "sha256:" + "b" * 64
    text, packages, warnings = containerfile.generate(PROJECT, spec(), base_digest=digest)
    assert f"FROM docker.io/library/almalinux@{digest}" in text
    assert "dnf install -y" in text and "dnf clean all" in text
    assert "bind-utils" in text and "bind-utils" in packages
    assert warnings == []
    assert 'org.opencontainers.image.base.digest="sha256:' in text


def test_generate_uses_the_package_manager_of_the_family():
    ubuntu, _, _ = containerfile.generate(PROJECT, spec(base={"distribution": "Ubuntu", "version": "24.04"}))
    alpine, _, _ = containerfile.generate(PROJECT, spec(base={"distribution": "Alpine", "version": "3.22"}))
    assert "apt-get install" in ubuntu and "rm -rf /var/lib/apt/lists/*" in ubuntu
    assert "apk add --no-cache" in alpine


def test_generated_env_values_cannot_break_out_of_quoting():
    text, _, _ = containerfile.generate(PROJECT, spec(env=[{"name": "GREETING", "value": 'he said "hi" $USER'}]))
    assert 'ENV GREETING="he said \\"hi\\" \\$USER"' in text


def test_scripts_files_and_user_appear_in_order():
    text, _, _ = containerfile.generate(PROJECT, spec(
        scripts={"pre_build": "echo pre", "post_install": "echo post", "entrypoint": "exec \"$@\""},
        files=[{"sha256": "c" * 64, "destination": "/opt/app/config.yml", "mode": "0640"}],
        user={"name": "builder", "uid": 1001, "shell": "/bin/bash"},
        workdir="/workspace",
    ))
    order = [text.index(fragment) for fragment in (
        "scripts/pre-build.sh", "dnf install", "files/" + "c" * 64, "scripts/post-install.sh",
        "useradd", "WORKDIR /workspace", "USER builder", "ENTRYPOINT",
    )]
    assert order == sorted(order)
    assert "COPY --chmod=0640 files/" in text


def test_context_plan_lists_blobs_and_scripts():
    plan = containerfile.context_plan(spec(
        files=[{"sha256": "d" * 64, "destination": "/opt/f"}], scripts={"pre_build": "echo hi"}))
    assert ("files/" + "d" * 64, "blob", "d" * 64) in plan
    assert ("scripts/pre-build.sh", "script", "pre_build") in plan


def test_script_body_adds_a_shebang_only_when_missing():
    assert containerfile.script_body("echo hi").startswith("#!/bin/sh")
    assert containerfile.script_body("#!/bin/bash\necho hi").startswith("#!/bin/bash")


@pytest.mark.parametrize("text,expected", [
    ("FROM alpine:3.22\nRUN apk add git", ["alpine:3.22"]),
    ("FROM golang:1 AS build\nFROM alpine:3.22\nCOPY --from=build /x /x", ["golang:1", "alpine:3.22"]),
    ("FROM scratch\nCOPY x /", []),
    ("FROM --platform=linux/arm64 debian:13", ["debian:13"]),
    ("FROM $BASE_IMAGE", []),
])
def test_import_finds_external_bases(text, expected):
    assert containerfile.bases(text) == expected


@pytest.mark.parametrize("distribution,version,expected", [
    ("AlmaLinux", "10", None),          # EPEL-only there: skipped with a warning
    ("Fedora", "43", "ShellCheck"),     # present in Fedora's own repositories
    ("Ubuntu", "24.04", "shellcheck"),
    ("Alpine", "3.22", "shellcheck"),
])
def test_epel_only_packages_are_skipped_on_enterprise_linux(distribution, version, expected):
    resolved, warnings = resolve_packages(spec(
        base={"distribution": distribution, "version": version}, presets=[], packages=["shellcheck"]))
    if expected is None:
        assert resolved == [] and any("EPEL" in w for w in warnings)
    else:
        assert resolved == [expected] and warnings == []


def test_epel_only_packages_are_kept_when_epel_is_enabled():
    resolved, warnings = resolve_packages(spec(presets=[], packages=["shellcheck"], enable_epel=True))
    assert resolved == ["ShellCheck"] and warnings == []


@pytest.mark.parametrize("capability,family,expected", [
    ("npm", "rpm", "nodejs"),     # no separate npm package on rpm distributions
    ("npm", "deb", "npm"),
    ("iotop", "rpm", "iotop-c"),  # packaged under a different name
    ("iotop", "deb", "iotop"),
])
def test_package_names_that_differ_by_more_than_spelling(capability, family, expected):
    base = {"rpm": {"distribution": "AlmaLinux", "version": "10"},
            "deb": {"distribution": "Ubuntu", "version": "24.04"}}[family]
    resolved, _ = resolve_packages(spec(base=base, presets=[], packages=[capability]))
    assert resolved == [expected]
