"""opencode Shared Terminal: catalog, recipe, Containerfile, scripts and the API end to end."""

import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from layersmith.core import containerfile as cf
from layersmith.core.spec import InvalidSpec, validate_spec
from layersmith.opencode import catalog, checks, docs, recipes, render
from layersmith.opencode.catalog import OpencodeError
from tests.test_api import client, run_pending_build  # noqa: F401  (fixture)
from tests.test_builds import FakeBackend

ASSETS = render.ASSET_DIR
BASH = shutil.which("bash")


# ------------------------------------------------------------ catalog

def test_every_download_is_pinned_https_and_never_latest():
    for version in catalog.OPENCODE_VERSIONS:
        assert version.startswith("1."), "opencode's installer offers only 1.x"
        for cpu in catalog.CPU_VARIANTS:
            item = catalog.opencode_download(version, cpu)
            assert item["url"].startswith("https://github.com/anomalyco/opencode/releases/download/v")
            assert f"/v{version}/" in item["url"] and "latest" not in item["url"]
            assert len(item["sha256"]) == 64 and int(item["sha256"], 16) >= 0
            assert item["files"] == {"opencode": "/usr/local/bin/opencode"}
    assert catalog.RIPGREP["url"].startswith("https://") and len(catalog.RIPGREP["sha256"]) == 64
    pins = [catalog.OPENCODE_VERSIONS[v][c] for v in catalog.OPENCODE_VERSIONS for c in catalog.CPU_VARIANTS]
    assert len(set(pins)) == len(pins)


def test_defaults():
    recipe = recipes.resolve({})
    assert recipe["base"]["display"] == "AlmaLinux 10" and recipe["base"]["verified"]
    assert recipe["opencode"] == {"version": catalog.DEFAULT_OPENCODE, "cpu": "x64", "asset": "opencode-linux-x64.tar.gz"}
    assert recipe["optional"] == ["python"]
    assert [d["tool"] for d in recipe["downloads"]] == ["opencode", "ripgrep"]
    assert {"git-core", "jq", "openssh-clients", "python3"} <= set(recipe["packages"])
    assert "sudo" not in recipe["packages"] and "openssh-server" not in recipe["packages"]
    assert recipe["environment"]["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
    assert any("AVX2" in w for w in recipe["warnings"])


def test_baseline_cpu_and_toolchains():
    recipe = recipes.resolve({"cpu": "x64-baseline", "opencode_version": "1.19.4",
                              "optional": ["build-tools", "nodejs"]})
    assert recipe["downloads"][0]["url"].endswith("/v1.19.4/opencode-linux-x64-baseline.tar.gz")
    assert recipe["downloads"][0]["sha256"] == catalog.OPENCODE_VERSIONS["1.19.4"]["x64-baseline"]
    assert {"gcc", "make", "nodejs", "npm"} <= set(recipe["packages"]) and "python3" not in recipe["packages"]
    assert {c["binary"] for c in recipe["checks"]} >= {"opencode", "rg", "gcc", "node", "git"}
    assert not any("AVX2" in w for w in recipe["warnings"])
    assert recipes.resolve({"optional": []})["optional"] == []


@pytest.mark.parametrize("request_, message", [
    ({"opencode_version": "latest"}, "not pinned"),
    ({"opencode_version": "2.2.5"}, "not pinned"),
    ({"cpu": "arm64"}, "Unknown CPU"),
    ({"optional": ["rust"]}, "Unknown optional"),
    ({"base": "Ubuntu 24.04"}, "Unknown base"),
    ({"base": "custom", "base_reference": "registry.example.com/alma"}, "tag or a digest"),
    ({"base": "custom", "base_reference": "registry.example.com/alma:10"}, "Enterprise Linux major"),
    ({"endpoint": "http://x"}, "Unknown opencode setting"),
])
def test_invalid_requests_are_refused(request_, message):
    with pytest.raises(OpencodeError, match=message):
        recipes.resolve(request_)


def test_custom_base_is_allowed_and_flagged():
    recipe = recipes.resolve({"base": "custom", "base_reference": "registry.example.com/alma:10", "base_el": "10"})
    assert recipe["base"]["reference"] == "registry.example.com/alma:10" and not recipe["base"]["verified"]
    assert any("custom base" in w for w in recipe["warnings"])


# ------------------------------------------------------------ spec and Containerfile

def test_spec_stores_the_small_request_and_owns_the_layout():
    spec = validate_spec({"opencode": {"cpu": "x64-baseline"}})
    assert spec["opencode"] == {"base": "AlmaLinux 10", "opencode_version": catalog.DEFAULT_OPENCODE,
                                "cpu": "x64-baseline", "optional": ["python"]}
    assert spec["base"]["distribution"] == "AlmaLinux" and spec["architecture"] == "amd64"
    with pytest.raises(InvalidSpec, match="made from its profile"):
        validate_spec({"opencode": {}, "tools": [{"sha256": "a" * 64}]})
    with pytest.raises(InvalidSpec, match="not several"):
        validate_spec({"opencode": {}, "openshift": {}})
    with pytest.raises(InvalidSpec, match="linux/amd64"):
        validate_spec({"opencode": {}, "architecture": "arm64"})


def test_containerfile_installs_verifies_and_keeps_running():
    spec = validate_spec({"opencode": {}})
    text, packages, _ = cf.generate({"name": "opencode", "description": ""}, spec)
    assert "dnf -y upgrade --refresh" in text
    assert "install-tools.sh /usr/local/share/opencode/downloads.tsv" in text
    assert 'OPENCODE_DISABLE_MODELS_FETCH="1"' in text and 'OPENCODE_CONFIG="/etc/opencode/opencode.json"' in text
    assert "COPY opencode/host/ /usr/local/share/opencode/host/" in text
    assert text.rstrip().endswith('CMD ["/usr/local/libexec/opencode/container-init"]')
    assert "--chmod" not in text and "\nUSER " not in text
    assert 'io.layersmith.opencode.version="1.19.5"' in text
    assert "git-core" in packages


def test_build_context_holds_the_manifests_scripts_and_host_kit(tmp_path):
    recipe = recipes.resolve({})
    files = render.write_context(tmp_path, recipe, "# readme\n", "localhost/opencode:2.0.0", "opencode-2.0.0.tar")
    assert {"opencode/downloads.tsv", "opencode/checks.tsv", "opencode/build/install-tools.sh",
            "opencode/build/finish.sh", "opencode/build/layersmith-opencode-check", "opencode/libexec/ensure-user",
            "opencode/libexec/container-init", "opencode/bin/opencode-versions", "opencode/etc/opencode.json",
            "opencode/host/install-host.sh", "opencode/host/opencode", "opencode/host/opencode-enter",
            "opencode/host/opencode-container", "opencode/host/opencode-container.service",
            "opencode/host/sudoers", "opencode/host/README.md", "opencode/host/opencode.json"} <= set(files)
    root = tmp_path / "opencode"
    installer = (root / "host/install-host.sh").read_text()
    assert "IMAGE=localhost/opencode:2.0.0" in installer and "--archive opencode-2.0.0.tar" in installer
    assert "@IMAGE@" not in installer
    downloads = (root / "downloads.tsv").read_text()
    assert recipe["downloads"][0]["sha256"] in downloads and "opencode=/usr/local/bin/opencode" in downloads
    assert "ripgrep-15.1.0-x86_64-unknown-linux-musl/rg=/usr/local/bin/rg" in downloads
    for name in ("host/install-host.sh", "host/opencode-enter", "libexec/ensure-user", "build/finish.sh"):
        assert os.access(root / name, os.X_OK), name
    for name in ("host/sudoers", "host/opencode-container.service", "etc/opencode.json"):
        assert (root / name).stat().st_mode & 0o777 == 0o644, name


def test_site_config_template_is_valid_jsonc():
    text = (ASSETS / "etc/opencode.json").read_text()
    # opencode reads JSONC; strip the comment lines and trailing commas, the rest must be JSON.
    stripped = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
    assert json.loads(stripped) == {"$schema": "https://opencode.ai/config.json", "autoupdate": False,
                                    "share": "disabled"}
    assert '"npm": "@ai-sdk/openai-compatible"' in text


# ------------------------------------------------------------ shell scripts

SCRIPTS = sorted([*ASSETS.glob("bin/*"), *ASSETS.glob("build/*"), *ASSETS.glob("libexec/*"),
                  *(ASSETS / "host" / name for name in render.EXECUTABLE_HOST_FILES)])


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_shell_scripts_parse(script):
    shell = "sh" if script.read_text().startswith("#!/bin/sh") else BASH
    subprocess.run([shell, "-n", str(script)], check=True)


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck is not installed")
def test_shell_scripts_pass_shellcheck():
    result = subprocess.run(["shellcheck", "-x", "-S", "warning", *map(str, SCRIPTS)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


def _ensure_user_env(tmp_path):
    """ensure-user against copies of passwd/group, by rewriting its fixed paths (it only runs as root otherwise)."""
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "passwd").write_text("root:x:0:0:root:/root:/bin/bash\nnobody:x:65534:65534::/:/sbin/nologin\n"
                                "bob:x:900:900::/var/bob:/sbin/nologin\n")
    (etc / "group").write_text("root:x:0:\nwheel:x:10:\ndevs:x:4300:carol\n")
    script = (ASSETS / "libexec/ensure-user").read_text()
    script = (script.replace("/etc/", f"{etc}/").replace("/run/opencode-ensure-user.lock", f"{tmp_path}/lock")
              .replace('[[ $(id -u) == 0 ]] || die "must run as root"', ":"))
    path = tmp_path / "ensure-user"
    path.write_text(script)
    path.chmod(0o755)
    return path, etc


def test_ensure_user_writes_and_follows_the_host(tmp_path):
    script, etc = _ensure_user_env(tmp_path)
    run = lambda *args: subprocess.run([BASH, str(script), *args], capture_output=True, text=True)  # noqa: E731
    first = run("alice", "4242", "4242", "/home/alice", "4242:alice", "4300:devs", "4301:ops")
    assert first.returncode == 0 and first.stdout.strip() == "alice", first.stderr
    assert "alice:x:4242:4242:opencode user:/home/alice:/bin/bash" in (etc / "passwd").read_text()
    group = (etc / "group").read_text()
    assert "devs:x:4300:carol,alice" in group and "ops:x:4301:alice" in group and "alice:x:4242:" in group

    again = run("alice", "4242", "4242", "/srv/alice", "4242:alice", "4300:devs")
    assert again.returncode == 0
    passwd = (etc / "passwd").read_text()
    assert [l for l in passwd.splitlines() if l.startswith("alice:")] == ["alice:x:4242:4242:opencode user:/srv/alice:/bin/bash"]
    group = (etc / "group").read_text()
    assert "devs:x:4300:carol,alice" in group and "ops:x:4301:\n" in group  # dropped on the host -> dropped here

    # A name an image account already has, and a directory-service name, get a unique one.
    assert run("bob", "4400", "4400", "/home/bob").stdout.strip() == "u4400"
    assert run("dave@example.com", "4401", "4401", "/home/dave").stdout.strip() == "u4401"
    for bad in (["root", "0", "0", "/root"], ["eve", "4500", "4500", "relative"], ["eve", "4500", "4500", "/a:b"],
                ["eve", "x", "4500", "/home/eve"], ["eve", "4500", "4500", "/home/eve", "x:evil"]):
        assert run(*bad).returncode != 0, bad
    assert "eve" not in (etc / "passwd").read_text()


def test_opencode_enter_trusts_only_sudo():
    text = (ASSETS / "host/opencode-enter").read_text()
    assert 'uid=${SUDO_UID:-}' in text and 'getent passwd "$uid"' in text
    assert "--user 0" in text and text.count("--user \"$uid\"") == 2  # root only for ensure-user
    sudoers = (ASSETS / "host/sudoers").read_text()
    assert "NOPASSWD: /usr/local/libexec/opencode-container/opencode-enter\n" in sudoers
    assert "env_keep" in sudoers and "OPENCODE" not in sudoers.split("env_keep", 1)[1]


def test_opencode_enter_refuses_without_root_or_sudo(tmp_path):
    result = subprocess.run([BASH, str(ASSETS / "host/opencode-enter")], capture_output=True, text=True,
                            env={"PATH": os.environ["PATH"], "SUDO_UID": "1000"})
    if os.geteuid() != 0:
        assert result.returncode != 0 and "opencode command" in result.stderr


@pytest.mark.skipif(not shutil.which("visudo"), reason="visudo is not installed")
def test_sudoers_rule_validates(tmp_path):
    rule = tmp_path / "sudoers"
    rule.write_text((ASSETS / "host/sudoers").read_text().replace("@GROUP@", "opencode"))
    result = subprocess.run(["visudo", "-cf", str(rule)], capture_output=True, text=True)
    if "permission" in (result.stderr + result.stdout).lower():
        pytest.skip("visudo needs root here")
    assert result.returncode == 0, result.stdout + result.stderr


def test_container_command_is_unprivileged(tmp_path):
    """`opencode-container command` with a test config: no capabilities, shared dirs at the same path."""
    conf = tmp_path / "container.conf"
    shared = tmp_path / "home"
    shared.mkdir()
    conf.write_text(f"RUNTIME=podman\nIMAGE=localhost/opencode:1.0.0\nMOUNTS={shared}\nEXTRA_ARGS=(--memory 8g)\n")
    script = tmp_path / "opencode-container"
    script.write_text((ASSETS / "host/opencode-container").read_text()
                      .replace("CONF=/etc/opencode-container/container.conf", f"CONF={conf}"))
    result = subprocess.run([BASH, str(script), "command"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    for part in ("--cap-drop ALL", "--security-opt no-new-privileges", "--init", "--network host",
                 f"-v {shared}:{shared}:rslave", "-v /etc/opencode:/etc/opencode:ro", "--memory 8g",
                 "localhost/opencode:1.0.0 /usr/local/libexec/opencode/container-init"):
        assert part in out, part
    assert "--privileged" not in out and "docker.sock" not in out


# ------------------------------------------------------------ API, end to end

def result_line(check, status="passed"):
    steps = [{"name": "tools", "status": status, "detail": "x"}]
    info = {"tools": [{"tool": "opencode", "binary": "opencode", "version": "1.19.5", "status": "ok",
                       "expected": "1.19.5", "path": "/usr/local/bin/opencode"}]} \
        if check == "user" else {"os": "AlmaLinux 10.2", "uid": 0}
    return checks.RESULT_PREFIX + json.dumps({"check": check, "status": status, "image": "", "steps": steps,
                                              "info": info})


class OpencodeBackend(FakeBackend):
    def __init__(self, failing=()):
        super().__init__()
        self.failing = failing
        self.runs = []

    def build(self, context_dir, image_ref, architecture, on_log):
        self.context_files = sorted(str(p.relative_to(context_dir)) for p in Path(context_dir).rglob("*")
                                    if p.is_file())
        self.installer = (Path(context_dir) / "opencode/host/install-host.sh").read_text()
        return super().build(context_dir, image_ref, architecture, on_log)

    def run_container(self, image_ref, argv, on_log, timeout=1800, network="none", entrypoint=None, user=None):
        self.runs.append({"argv": argv, "user": user, "network": network})
        check = argv[-1]
        line = result_line(check, "failed" if check in self.failing else "passed")
        on_log(line)
        return 0, line


@pytest.fixture
def oc_client(client):  # noqa: F811
    client.state["build_service"].backend = OpencodeBackend()
    return client


def create_and_build(client, spec=None):
    project = client.post("/api/projects", json={
        "name": "opencode", "repository": "localhost/opencode", "template": "opencode Shared Terminal",
        "spec": {"opencode": spec or {}}}).json()
    build = client.post(f"/api/projects/{project['id']}/builds", json={}).json()
    run_pending_build(client, build["id"])
    return project, build["id"]


def test_catalog_and_resolve_endpoints(oc_client):
    view = oc_client.get("/api/opencode/catalog").json()
    assert view["profile"]["name"] == "opencode Shared Terminal"
    assert view["default_version"] == catalog.DEFAULT_OPENCODE and "x64-baseline" in view["cpu_variants"]
    assert "sha256" not in json.dumps(view)
    ok = oc_client.post("/api/opencode/resolve", json={"opencode": {"optional": []}}).json()
    assert ok["ok"] and ok["names"]["repository"] == "localhost/opencode"
    assert ok["commands"]["install"] == "sudo bash install-host.sh --archive opencode-1.0.0.tar"
    bad = oc_client.post("/api/opencode/resolve", json={"opencode": {"opencode_version": "latest"}}).json()
    assert not bad["ok"] and "not pinned" in bad["error"]
    assert "opencode" in oc_client.get("/api/health").json()["api_features"]


def test_build_runs_both_checks_and_offers_the_host_kit(oc_client):
    project, build_id = create_and_build(oc_client)
    backend = oc_client.state["build_service"].backend
    assert [(r["argv"][-1], r["user"], r["network"]) for r in backend.runs] == [
        ("image", None, "none"), ("user", "1000:1000", "none")]
    assert "opencode/host/opencode-enter" in backend.context_files
    assert "IMAGE=localhost/opencode:1.0.0" in backend.installer
    detail = oc_client.get(f"/api/builds/{build_id}").json()
    assert detail["status"] == "ready" and detail["opencode_version"] == catalog.DEFAULT_OPENCODE
    oc = detail["opencode"]
    assert [t["status"] for t in oc["tests"]] == ["passed", "passed"]
    assert oc["tools_installed"][0]["binary"] == "opencode"
    assert {f["name"] for f in oc["downloads"]} == {"opencode-1.0.0.tar", "opencode-1.0.0.tar.sha256",
                                                    "install-host.sh", "README.md"}
    assert "@ai-sdk/openai-compatible" in oc["site_config"]
    assert detail["manifest"]["opencode"]["opencode"] == catalog.DEFAULT_OPENCODE
    assert detail["manifest"]["opencode"]["checks"] == {"image": "passed", "user": "passed"}
    assert detail["openshift"] is None
    assert oc_client.get(f"/api/projects/{project['id']}").json()["opencode"]["profile"] == "opencode"

    installer = oc_client.get(f"/api/builds/{build_id}/opencode/files/install-host.sh")
    assert installer.status_code == 200 and "IMAGE=localhost/opencode:1.0.0" in installer.text
    assert "--archive opencode-1.0.0.tar" in installer.text
    readme = oc_client.get(f"/api/builds/{build_id}/opencode/files/README.md").text
    assert "sudo bash install-host.sh --archive opencode-1.0.0.tar" in readme and "usermod -aG opencode" in readme
    assert oc_client.get(f"/api/builds/{build_id}/opencode/files/versions.json").json()[0]["version"] == "1.19.5"
    assert oc_client.get(f"/api/builds/{build_id}/opencode/files/..%2F..%2Fetc%2Fpasswd").status_code == 404


def test_failed_check_is_shown_but_does_not_fail_the_build(oc_client):
    oc_client.state["build_service"].backend = OpencodeBackend(failing=("user",))
    _, build_id = create_and_build(oc_client)
    detail = oc_client.get(f"/api/builds/{build_id}").json()
    assert detail["status"] == "ready"
    assert {t["id"]: t["status"] for t in detail["opencode"]["tests"]} == {"image": "passed", "user": "failed"}


def test_airgap_bundle_carries_the_installer(oc_client):
    _, build_id = create_and_build(oc_client)
    bundle = oc_client.post(f"/api/builds/{build_id}/airgap", json={}).json()
    path = Path(oc_client.state["settings"].image_dir) / bundle["filename"]
    with tarfile.open(path) as archive:
        names = archive.getnames()
        stem = names[0].split("/")[0]
        assert {f"{stem}/opencode/README.md", f"{stem}/opencode/install-host.sh", f"{stem}/opencode/opencode.json",
                f"{stem}/opencode/checks.json"} <= set(names)
        assert archive.getmember(f"{stem}/opencode/install-host.sh").mode & 0o111
        install = archive.extractfile(f"{stem}/INSTALL.txt").read().decode()
    assert "opencode/install-host.sh" in install


def test_other_builds_have_no_opencode_detail(oc_client):
    project = oc_client.post("/api/projects", json={
        "name": "plain", "template": "Minimal", "spec": {"base": {"distribution": "Alpine", "version": "3.22"}}}).json()
    build = oc_client.post(f"/api/projects/{project['id']}/builds", json={}).json()
    run_pending_build(oc_client, build["id"])
    detail = oc_client.get(f"/api/builds/{build['id']}").json()
    assert detail["opencode"] is None and "opencode" not in detail["manifest"]
    assert oc_client.get(f"/api/builds/{build['id']}/opencode/files/README.md").status_code == 404


def test_readme_covers_install_endpoint_users_and_updates():
    recipe = recipes.resolve({})
    text = docs.markdown(recipe, "localhost/opencode:1.0.0", "opencode-1.0.0.tar")
    for part in ("sha256sum -c opencode-1.0.0.tar.sha256", "/etc/opencode/opencode.json", "usermod -aG opencode",
                 "opencode-shell", "OPENCODE_DISABLE_LSP_DOWNLOAD", "systemctl restart opencode-container",
                 "--uninstall", "ripgrep"):
        assert part in text, part
