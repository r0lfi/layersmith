"""OpenShift Admin Toolkit: pins, recipes, Containerfile, build scripts, helpers, checks and API."""

import hashlib
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import tarfile
import threading
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

from layersmith.core import containerfile as cf
from layersmith.core.spec import InvalidSpec, validate_spec
from layersmith.openshift import catalog, checks, docs, recipes, render
from layersmith.openshift.catalog import OpenShiftError
from tests.test_api import client, run_pending_build  # noqa: F401  (fixture)
from tests.test_builds import FakeBackend

ASSETS = render.ASSET_DIR
BASH = shutil.which("bash")
needs_tools = pytest.mark.skipif(not all(shutil.which(t) for t in ("bash", "jq", "column")),
                                 reason="the helper tests need bash, jq and column")


# ------------------------------------------------------------ catalog

def test_every_download_is_pinned_https_and_never_latest():
    for tool_id, tool in catalog.TOOLS.items():
        for version, entries in tool["versions"].items():
            for entry in entries if isinstance(entries, list) else [entries]:
                assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]), (tool_id, version)
                assert entry["url"].startswith("https://"), entry["url"]
                assert "latest" not in entry["url"] and "stable-" not in entry["url"]
                assert version.replace("-90", "") in entry["url"] or tool_id in ("oc-mirror",), entry["url"]
                assert all(dest.startswith("/usr/local/bin/") for dest in entry["files"].values())


def test_red_hat_clients_exist_for_every_ocp_release():
    for version in catalog.OCP_RELEASES:
        for tool_id in ("oc", "oc-mirror", "opm"):
            assert version in catalog.TOOLS[tool_id]["versions"]
            assert f"/{version}/" in catalog.TOOLS[tool_id]["versions"][version]["url"]
    assert catalog.DEFAULT_OCP.startswith("4.22.")
    assert catalog.OCP_RELEASES[catalog.DEFAULT_OCP]["channel"] == "stable-4.22"


def test_defaults_are_the_requested_environment():
    recipe = recipes.resolve({})
    assert recipe["base"]["display"] == "AlmaLinux 10" and recipe["base"]["reference"].endswith("almalinux:10")
    assert recipe["architecture"] == "amd64" and recipe["timezone"] == "Europe/Oslo"
    assert recipe["locale"] == "en_US.UTF-8" and recipe["ocp"]["version"] == "4.22.18"
    assert recipe["optional"] == catalog.OPTIONAL
    assert {"sudo", "shadow-utils", "util-linux", "bash-completion", "glibc-langpack-en", "podman", "buildah",
            "skopeo", "ansible-core", "python3", "jq", "git-core", "bind-utils", "nmap-ncat",
            "traceroute"} <= set(recipe["packages"])
    assert "openssh-server" not in recipe["packages"]
    expect = {c["binary"]: c["expect"] for c in recipe["checks"]}
    assert expect["oc"] == "Client Version: 4.22.18" and expect["kubectl"] == "Client Version: v1.35.2"
    assert expect["oc-mirror"] == '"gitVersion": "4.22.0-'
    assert recipe["compatibility"]["supported_clusters"] == ["4.21", "4.22", "4.23"]


def test_versions_are_chosen_per_tool_and_never_guessed():
    recipe = recipes.resolve({"ocp_version": "4.21.37", "versions": {"oc-mirror": "4.22.18", "helm": "4.3.0"}})
    versions = {t["id"]: t["version"] for t in recipe["tools"]}
    assert versions["oc"] == "4.21.37" and versions["opm"] == "4.21.37"  # follows the client
    assert versions["oc-mirror"] == "4.22.18" and versions["helm"] == "4.3.0"
    with pytest.raises(OpenShiftError, match="not pinned"):
        recipes.resolve({"ocp_version": "4.22.99"})
    with pytest.raises(OpenShiftError, match="not a pinned version"):
        recipes.resolve({"versions": {"k9s": "latest"}})
    with pytest.raises(OpenShiftError, match="same|always come from"):
        recipes.resolve({"versions": {"oc": "4.21.37"}})
    with pytest.raises(OpenShiftError, match="No version can be chosen"):
        recipes.resolve({"versions": {"kubectl-neat": "1.0"}})


def test_optional_tools_can_be_left_out():
    recipe = recipes.resolve({"optional": ["helm"], "base": "AlmaLinux 9"})
    downloaded = {d["tool"] for d in recipe["downloads"]}
    assert downloaded == {"oc", "helm"}
    assert {c["tool"] for c in recipe["checks"]} == {"oc", "helm", "base"}
    assert "podman" not in recipe["packages"] and "ansible-core" not in recipe["packages"]
    assert "passwd" in recipe["packages"]  # EL9 has a separate passwd package; EL10 has it in shadow-utils
    assert "passwd" not in recipes.resolve({})["packages"]
    with pytest.raises(OpenShiftError, match="Unknown optional tool"):
        recipes.resolve({"optional": ["kubectl-ai"]})


@pytest.mark.parametrize("request_, message", [
    ({"timezone": "Europe/Oslo; rm -rf /"}, "time zone"),
    ({"timezone": "../../etc/passwd"}, "time zone"),
    ({"prod_pattern": "prod$(id)"}, "production pattern"),
    ({"prod_pattern": 'prod" ; echo'}, "production pattern"),
    ({"base": "Ubuntu 24.04"}, "Unknown base"),
    ({"base": "custom", "base_reference": "registry.example.com/el10/almalinux", "base_el": "10"}, "tag or a digest"),
    ({"base": "custom", "base_reference": "registry.example.com/el10/almalinux:10"}, "9 or 10"),
    ({"surprise": 1}, "Unknown OpenShift toolkit setting"),
])
def test_invalid_requests_are_refused(request_, message):
    with pytest.raises(OpenShiftError, match=message):
        recipes.resolve(request_)


def test_custom_base_is_allowed_and_flagged():
    recipe = recipes.resolve({"base": "custom", "base_reference": "registry.example.com/mirror/almalinux:10",
                              "base_el": "10"})
    assert recipe["base"]["reference"] == "registry.example.com/mirror/almalinux:10" and not recipe["base"]["verified"]
    assert any("custom base" in w for w in recipe["warnings"])


# ------------------------------------------------------------ spec and Containerfile

def test_spec_stores_the_small_request_and_owns_the_layout():
    spec = validate_spec({"openshift": {"versions": {"helm": "4.3.0"}}, "extra_packages": ["tmux"],
                          "env": [{"name": "EDITOR", "value": "vim"}]})
    assert spec["openshift"] == {"versions": {"helm": "4.3.0"}, "base": "AlmaLinux 10", "ocp_version": "4.22.18",
                                 "optional": catalog.OPTIONAL, "timezone": "Europe/Oslo", "prod_pattern": "prod"}
    assert spec["base"]["source"] == "docker.io/library/almalinux:10" and spec["architecture"] == "amd64"
    for key, value in (("presets", ["Basic Linux"]), ("user", {"name": "admin"}), ("workdir", "/srv"),
                       ("files", [{"sha256": "0" * 64, "destination": "/etc/x"}])):
        with pytest.raises(InvalidSpec, match="made from its profile"):
            validate_spec({"openshift": {}, key: value})
    with pytest.raises(InvalidSpec, match="amd64"):
        validate_spec({"openshift": {}, "architecture": "arm64"})
    with pytest.raises(InvalidSpec, match="not several"):
        validate_spec({"openshift": {}, "aiml": {"template": "nvidia-pytorch"}})
    with pytest.raises(InvalidSpec, match="looks like a secret"):
        validate_spec({"openshift": {}, "env": [{"name": "OPENSHIFT_TOKEN", "value": "x"}]})


def test_containerfile_updates_verifies_and_leaves_user_to_distrobox():
    spec = validate_spec({"openshift": {}, "extra_packages": ["tmux"]})
    text, packages, _ = cf.generate({"name": "openshift-admin"}, spec, base_digest="sha256:" + "a" * 64)
    assert text.startswith("# Generated by LayerSmith")
    assert "FROM docker.io/library/almalinux@sha256:" + "a" * 64 in text
    assert text.index("dnf -y upgrade --refresh") < text.index("dnf install -y")
    assert "tmux" in packages and "tmux" in text
    assert "install-tools.sh /usr/local/share/openshift-admin/downloads.tsv" in text
    assert 'finish.sh "Europe/Oslo" "en_US.UTF-8"' in text and 'CMD ["/bin/bash", "-l"]' in text
    assert not re.search(r"^(USER|ENTRYPOINT|EXPOSE|VOLUME)\b", text, re.M)
    assert 'io.layersmith.openshift.ocp="4.22.18"' in text
    for forbidden in ("insecure", "KUBECONFIG", "--token", "curl | sh", "| bash", "systemctl", "sshd"):
        assert forbidden not in text, forbidden


def test_build_context_holds_the_manifests_and_scripts(tmp_path):
    recipe = recipes.resolve({"optional": ["helm", "kubectx"]})
    files = render.write_context(tmp_path, recipe, docs.markdown(recipe, "localhost/openshift-admin:1.0.0"))
    assert {"openshift/downloads.tsv", "openshift/checks.tsv", "openshift/toolkit.env", "openshift/README.md",
            "openshift/build/install-tools.sh", "openshift/build/finish.sh",
            "openshift/build/layersmith-openshift-check", "openshift/bin/oc-status",
            "openshift/profile.d/openshift-admin.sh"} <= set(files)
    downloads = [line.split("\t") for line in (tmp_path / "openshift/downloads.tsv").read_text().splitlines()
                 if not line.startswith("#")]
    assert len(downloads) == 4  # oc, helm, kubectx, kubens
    assert downloads[0][2] == "oc=/usr/local/bin/oc kubectl=/usr/local/bin/kubectl"
    env = (tmp_path / "openshift/toolkit.env").read_text()
    assert ': "${OPENSHIFT_ADMIN_PROD_PATTERN:=prod}"' in env


# ------------------------------------------------------------ shell scripts

SCRIPTS = sorted([*ASSETS.glob("bin/*"), *ASSETS.glob("build/*"), ASSETS / "lib/common.sh",
                  ASSETS / "profile.d/openshift-admin.sh", ASSETS / "completion/openshift-admin"])


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_shell_scripts_parse(script):
    subprocess.run([BASH, "-n", str(script)], check=True)


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck is not installed")
def test_shell_scripts_pass_shellcheck():
    result = subprocess.run(["shellcheck", "-x", "-s", "bash", "-S", "warning", *map(str, SCRIPTS)],
                            capture_output=True, text=True, cwd=ASSETS / "bin")
    assert result.returncode == 0, result.stdout


def _https_server(directory: Path, tmp_path: Path):
    """A local HTTPS server with a throwaway certificate, so the installer's HTTPS-only rule holds."""
    if not shutil.which("openssl"):
        pytest.skip("openssl is needed for the local HTTPS server")
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj",
                    "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    server = HTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(directory)))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, cert


def _tarball(path: Path, members: dict[str, bytes]) -> str:
    with tarfile.open(path, "w:gz") as archive:
        for name, body in members.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(body), 0o644
            archive.addfile(info, io.BytesIO(body))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _install(tmp_path, manifest_lines, cert=None):
    manifest = tmp_path / "downloads.tsv"
    manifest.write_text("\n".join(manifest_lines) + "\n")
    env = {**os.environ, **({"CURL_CA_BUNDLE": str(cert)} if cert else {})}
    return subprocess.run([BASH, str(ASSETS / "build/install-tools.sh"), str(manifest)], capture_output=True,
                          text=True, env=env, timeout=120)


def test_installer_verifies_checksums_before_installing(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    client_sha = _tarball(served / "client.tar.gz", {"oc": b"#!/bin/sh\necho oc\n", "kubectl": b"#!/bin/sh\necho oc\n",
                                                     "README.md": b"x"})
    bare = b"#!/bin/sh\necho tool\n"
    (served / "tool").write_bytes(bare)
    server, cert = _https_server(served, tmp_path)
    base = f"https://127.0.0.1:{server.server_address[1]}"
    bin_dir = tmp_path / "bin"
    try:
        good = _install(tmp_path, [
            f"{base}/client.tar.gz\t{client_sha}\toc={bin_dir}/oc kubectl={bin_dir}/kubectl",
            f"{base}/tool\t{hashlib.sha256(bare).hexdigest()}\t-={bin_dir}/tool"], cert)
        assert good.returncode == 0, good.stderr
        assert (bin_dir / "tool").read_bytes() == bare and os.access(bin_dir / "tool", os.X_OK)
        assert (bin_dir / "oc").stat().st_ino == (bin_dir / "kubectl").stat().st_ino  # identical -> hard link
        assert not (bin_dir / "README.md").exists()

        bad = _install(tmp_path, [f"{base}/tool\t{'0' * 64}\t-={tmp_path}/other/tool"], cert)
        assert bad.returncode != 0 and "checksum mismatch" in bad.stderr
        assert not (tmp_path / "other").exists()

        missing_member = _install(tmp_path, [f"{base}/client.tar.gz\t{client_sha}\tnope={tmp_path}/x/nope"], cert)
        assert missing_member.returncode != 0 and not (tmp_path / "x" / "nope").exists()
    finally:
        server.shutdown()


def test_installer_refuses_plain_http(tmp_path):
    result = _install(tmp_path, [f"http://127.0.0.1:9/tool\t{'0' * 64}\t-={tmp_path}/tool"])
    assert result.returncode != 0 and not (tmp_path / "tool").exists()


# ------------------------------------------------------------ helpers against a fake oc

FAKE_OC = r"""#!/bin/bash
# Answers like oc from fixture files in $FAKE_OC; anything unknown fails like a missing resource.
args=" $* "
fx=$FAKE_OC
case $args in
  *" config current-context "*) cat "$fx/context" 2>/dev/null; [ -s "$fx/context" ] ;;
  *" config view --minify -o jsonpath={.clusters[0].cluster.server} "*) cat "$fx/server" ;;
  *" config view --minify -o jsonpath={..namespace} "*) echo default ;;
  *" config view -o json "*) cat "$fx/config.json" ;;
  *" config use-context "*) echo "${@: -1}" >"$fx/context" ;;
  *" whoami "*) if [ -e "$fx/unreachable" ]; then
                  echo "The connection to the server api.lab.example.com:6443 was refused" >&2; exit 1
                fi; echo admin ;;
  *" api-resources "*) group=${args##*--api-group=}; group=${group%% *}
                       grep -qx "$group" "$fx/apis" 2>/dev/null && echo "x.$group" ;;
  *" auth can-i "*) exit 0 ;;
  *" get nodes -o json "*) cat "$fx/nodes.json" ;;
  *" get pods -A "*) cat "$fx/pods.json" ;;
  *" get csv -A -o json "*) echo '{"items": []}' ;;
  *" get nodes -l "*) ;;
  *) echo "error: the server doesn't have a resource type" >&2; exit 1 ;;
esac
"""


@pytest.fixture
def fake_cluster(tmp_path):
    bin_dir, fx = tmp_path / "fakebin", tmp_path / "fixtures"
    bin_dir.mkdir()
    fx.mkdir()
    (bin_dir / "oc").write_text(FAKE_OC)
    (bin_dir / "oc").chmod(0o755)
    (fx / "server").write_text("https://api.lab.example.com:6443\n")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_OC": str(fx), "NO_COLOR": "1",
           "OPENSHIFT_ADMIN_LIB": str(ASSETS / "lib"), "HOME": str(tmp_path)}
    env.pop("KUBECONFIG", None)

    def run(command, *args):
        return subprocess.run([BASH, str(ASSETS / "bin" / command), *args], capture_output=True, text=True,
                              env=env, timeout=60)
    run.fx = fx
    return run


def _gpu_node(name, product, gpus):
    return {"metadata": {"name": name, "labels": {"nvidia.com/gpu.product": product,
                                                   "nvidia.com/gpu.memory": "143771"}},
            "spec": {"taints": [{"key": "nvidia.com/gpu", "effect": "NoSchedule"}]},
            "status": {"capacity": {"nvidia.com/gpu": str(gpus)}, "allocatable": {"nvidia.com/gpu": str(gpus)},
                       "conditions": [{"type": "Ready", "status": "True"}]}}


def _gpu_pod(name, node, gpus, phase="Running"):
    return {"metadata": {"namespace": "llm", "name": name},
            "spec": {"nodeName": node, "containers": [{"image": "quay.io/vllm/vllm:x",
                                                        "resources": {"requests": {"nvidia.com/gpu": str(gpus)},
                                                                      "limits": {"nvidia.com/gpu": str(gpus)}}}]},
            "status": {"phase": phase}}


@needs_tools
def test_helpers_explain_a_missing_login(fake_cluster):
    for command in ("oc-status", "oc-gpu", "oc-ai", "oc-storage", "oc-routes", "oc-health"):
        result = fake_cluster(command)
        assert result.returncode == 2, (command, result.stderr)
        assert "not logged in" in result.stderr.lower() and "oc login" in result.stderr


@needs_tools
def test_unreachable_cluster_is_exit_3(fake_cluster):
    (fake_cluster.fx / "context").write_text("lab\n")
    (fake_cluster.fx / "unreachable").write_text("")
    result = fake_cluster("oc-status")
    assert result.returncode == 3 and "Cannot reach the API server" in result.stderr


@needs_tools
def test_gpu_capacity_allocatable_and_requests(fake_cluster):
    (fake_cluster.fx / "context").write_text("lab\n")
    (fake_cluster.fx / "nodes.json").write_text(json.dumps({"items": [
        _gpu_node("gpu-h200-1", "NVIDIA-H200", 8), _gpu_node("gpu-rtx-1", "NVIDIA-RTX-PRO-6000", 4),
        {"metadata": {"name": "worker-1", "labels": {}}, "status": {"capacity": {"cpu": "16"}}}]}))
    (fake_cluster.fx / "pods.json").write_text(json.dumps({"items": [
        _gpu_pod("vllm-a", "gpu-h200-1", 2), _gpu_pod("vllm-b", "gpu-h200-1", 1),
        {**_gpu_pod("waiting", None, 4, "Pending"),
         "status": {"phase": "Pending", "conditions": [{"type": "PodScheduled", "status": "False",
                                                        "message": "0/3 nodes: Insufficient nvidia.com/gpu"}]}}]}))
    nodes = fake_cluster("oc-gpu", "nodes")
    assert nodes.returncode == 0, nodes.stderr
    h200 = next(line.split() for line in nodes.stdout.splitlines() if line.startswith("gpu-h200-1"))
    assert h200[1:6] == ["NVIDIA-H200", "140", "GiB", "8", "8"] and h200[6] == "3"
    assert "worker-1" not in nodes.stdout and "Allocatable GPUs in the cluster: 12" in nodes.stdout
    pending = fake_cluster("oc-gpu", "pending")
    assert "Insufficient nvidia.com/gpu" in pending.stdout
    assert "No NVIDIA GPU Operator" in fake_cluster("oc-gpu", "operators").stdout


@needs_tools
def test_no_gpus_is_an_answer_not_an_error(fake_cluster):
    (fake_cluster.fx / "context").write_text("lab\n")
    (fake_cluster.fx / "nodes.json").write_text(json.dumps({"items": [
        {"metadata": {"name": "worker-1", "labels": {}}, "status": {"capacity": {"cpu": "16"}}}]}))
    (fake_cluster.fx / "pods.json").write_text('{"items": []}')
    result = fake_cluster("oc-gpu")
    assert result.returncode == 0
    assert "No node advertises nvidia.com/gpu" in result.stdout and "No running or pending pod" in result.stdout
    assert "Not installed (no nvidia.com API)" in result.stdout


@needs_tools
def test_oc_ai_without_openshift_ai(fake_cluster):
    (fake_cluster.fx / "context").write_text("lab\n")
    (fake_cluster.fx / "pods.json").write_text(json.dumps({"items": [_gpu_pod("vllm-a", "gpu-1", 1)]}))
    result = fake_cluster("oc-ai")
    assert result.returncode == 0, result.stderr
    assert "OpenShift AI is not installed" in result.stdout and "KServe) is not installed" in result.stdout
    assert "vllm-a" in result.stdout


@needs_tools
def test_contexts_are_listed_switched_and_production_is_marked(fake_cluster):
    (fake_cluster.fx / "context").write_text("openshift-lab\n")
    (fake_cluster.fx / "config.json").write_text(json.dumps({
        "clusters": [{"name": c, "cluster": {"server": f"https://api.{c}.example.com:6443"}}
                     for c in ("openshift-lab", "openshift-ai-prod")],
        "contexts": [{"name": c, "context": {"cluster": c, "user": "admin"}} for c in ("openshift-lab", "openshift-ai-prod")]}))
    listed = fake_cluster("oc-context")
    rows = {line.split()[-1] if "PROD" in line else "": line for line in listed.stdout.splitlines()}
    assert listed.returncode == 0 and "openshift-ai-prod" in rows["PROD"]
    assert any(line.startswith("*") and "openshift-lab" in line for line in listed.stdout.splitlines())
    switched = fake_cluster("oc-context", "ai-prod")
    assert switched.returncode == 0 and "PRODUCTION" in switched.stderr
    assert (fake_cluster.fx / "context").read_text().strip() == "openshift-ai-prod"
    ambiguous = fake_cluster("oc-context", "openshift")
    assert ambiguous.returncode == 64 and "several contexts" in ambiguous.stderr


@needs_tools
def test_debug_node_on_production_needs_confirmation(fake_cluster):
    (fake_cluster.fx / "context").write_text("openshift-ai-prod\n")
    (fake_cluster.fx / "server").write_text("https://api.ai-prod.example.com:6443\n")
    result = subprocess.run([BASH, str(ASSETS / "bin/oc-debug-node"), "worker-1"], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, timeout=30,
                            env={**os.environ, "PATH": f"{fake_cluster.fx.parent / 'fakebin'}:{os.environ['PATH']}",
                                 "FAKE_OC": str(fake_cluster.fx), "OPENSHIFT_ADMIN_LIB": str(ASSETS / "lib")})
    # The fake oc has no `get node`: the node lookup fails first, which must not start anything either.
    assert result.returncode != 0 and "debug" not in result.stdout


@needs_tools
def test_versions_report_missing_and_mismatched_tools(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {"good": "echo 'good version v1.2.3'", "old": "echo 'old version v0.9'",
                       "broken": "echo boom >&2; exit 3"}.items():
        (bin_dir / name).write_text(f"#!/bin/sh\n{body}\n")
        (bin_dir / name).chmod(0o755)
    manifest = tmp_path / "checks.tsv"
    manifest.write_text("t\tgood\tv1.2.3\tgood\t--version\nt\told\tv1.2.3\told\nt\tbroken\tv1\tbroken\n"
                        "t\tabsent\tv1\tabsent\t--version\n")
    result = subprocess.run([BASH, str(ASSETS / "bin/openshift-admin-versions"), "--json"], capture_output=True,
                            text=True, timeout=60, env={**os.environ, "OPENSHIFT_ADMIN_CHECKS": str(manifest),
                                                        "PATH": f"{bin_dir}:{os.environ['PATH']}"})
    assert result.returncode == 1
    status = {row["binary"]: (row["status"], row["version"]) for row in json.loads(result.stdout)}
    assert status == {"good": ("ok", "v1.2.3"), "old": ("mismatch", "v0.9"), "broken": ("error", "boom"),
                      "absent": ("missing", "-")}


# ------------------------------------------------------------ API, end to end

def result_line(check, status="passed", tools=None):
    steps = [{"name": "tools", "status": status, "detail": "x"}]
    info = {"tools": tools or [{"tool": "oc", "binary": "oc", "version": "4.22.18", "status": "ok",
                                "expected": "Client Version: 4.22.18", "path": "/usr/local/bin/oc"}]} \
        if check == "user" else {"os": "AlmaLinux 10.2", "uid": 0}
    return checks.RESULT_PREFIX + json.dumps({"check": check, "status": status, "image": "", "steps": steps,
                                              "info": info})


class OpenShiftBackend(FakeBackend):
    def __init__(self, failing=()):
        super().__init__()
        self.failing = failing
        self.runs = []

    def build(self, context_dir, image_ref, architecture, on_log):
        self.context_files = sorted(str(p.relative_to(context_dir)) for p in Path(context_dir).rglob("*")
                                    if p.is_file())
        self.readme = (Path(context_dir) / "openshift/README.md").read_text()
        return super().build(context_dir, image_ref, architecture, on_log)

    def run_container(self, image_ref, argv, on_log, timeout=1800, network="none", entrypoint=None, user=None):
        self.runs.append({"argv": argv, "user": user, "network": network})
        check = argv[-1]
        line = result_line(check, "failed" if check in self.failing else "passed")
        on_log(line)
        return 0, line


@pytest.fixture
def os_client(client):  # noqa: F811
    client.state["build_service"].backend = OpenShiftBackend()
    return client


def create_and_build(client, spec=None):
    project = client.post("/api/projects", json={
        "name": "openshift-admin", "repository": "localhost/openshift-admin", "template": "OpenShift Admin Toolkit",
        "spec": {"openshift": spec or {}}}).json()
    build = client.post(f"/api/projects/{project['id']}/builds", json={}).json()
    run_pending_build(client, build["id"])
    return project, build["id"]


def test_catalog_and_resolve_endpoints(os_client):
    view = os_client.get("/api/openshift/catalog").json()
    assert view["profile"]["name"] == "OpenShift Admin Toolkit"
    assert view["profile"]["category"] == "DevOps / Kubernetes / OpenShift"
    assert view["default_ocp"] == "4.22.18" and "4.22.18" in view["tools"]["oc"]["versions"]
    assert '"sha256":' not in json.dumps(view["tools"])  # version lists only; the pins stay on the server
    ok = os_client.post("/api/openshift/resolve", json={"openshift": {"optional": []}}).json()
    assert ok["ok"] and ok["names"]["alias"] == "4.22"
    assert ok["commands"]["distrobox_create"] == ("distrobox create --name openshift-admin "
                                                  "--image localhost/openshift-admin:4.22")
    bad = os_client.post("/api/openshift/resolve", json={"openshift": {"ocp_version": "latest"}}).json()
    assert not bad["ok"] and "not pinned" in bad["error"]


def test_build_runs_both_checks_and_shows_versions_and_commands(os_client):
    project, build_id = create_and_build(os_client)
    backend = os_client.state["build_service"].backend
    assert [(r["argv"][-1], r["user"], r["network"]) for r in backend.runs] == [
        ("image", None, "none"), ("user", "1000:1000", "none")]
    assert "openshift/downloads.tsv" in backend.context_files and "openshift/bin/oc-gpu" in backend.context_files
    assert "localhost/openshift-admin:1.0.0" in backend.readme
    detail = os_client.get(f"/api/builds/{build_id}").json()
    assert detail["status"] == "ready" and detail["openshift_ocp"] == "4.22.18"
    shift = detail["openshift"]
    assert [t["status"] for t in shift["tests"]] == ["passed", "passed"]
    assert shift["tools_installed"][0]["version"] == "4.22.18"
    assert shift["commands"]["tag"] == "podman tag localhost/openshift-admin:1.0.0 localhost/openshift-admin:4.22"
    assert shift["commands"]["distrobox_enter"] == "distrobox enter openshift-admin"
    assert {f["name"] for f in shift["downloads"]} == {"openshift-admin-1.0.0.tar", "openshift-admin-1.0.0.tar.sha256",
                                                       "openshift-admin.ini", "README.md"}
    assert detail["manifest"]["openshift"]["ocp"] == "4.22.18"
    assert detail["manifest"]["openshift"]["checks"] == {"image": "passed", "user": "passed"}
    project_view = os_client.get(f"/api/projects/{project['id']}").json()
    assert project_view["openshift"]["profile"] == "openshift-admin"

    ini = os_client.get(f"/api/builds/{build_id}/openshift/files/openshift-admin.ini")
    assert ini.status_code == 200 and "image=localhost/openshift-admin:4.22" in ini.text and "pull=false" in ini.text
    versions = os_client.get(f"/api/builds/{build_id}/openshift/files/versions.json").json()
    assert versions[0]["binary"] == "oc"
    readme = os_client.get(f"/api/builds/{build_id}/openshift/files/README.md").text
    assert "podman load -i openshift-admin-1.0.0.tar" in readme and "oc-gpu" in readme
    assert os_client.get(f"/api/builds/{build_id}/openshift/files/../../etc/passwd").status_code == 404


def test_failed_check_is_shown_but_does_not_fail_the_build(os_client):
    os_client.state["build_service"].backend = OpenShiftBackend(failing=("image",))
    _, build_id = create_and_build(os_client)
    detail = os_client.get(f"/api/builds/{build_id}").json()
    assert detail["status"] == "ready"
    assert {t["id"]: t["status"] for t in detail["openshift"]["tests"]} == {"image": "failed", "user": "passed"}


def test_airgap_bundle_carries_readme_manifest_and_checks(os_client):
    _, build_id = create_and_build(os_client)
    bundle = os_client.post(f"/api/builds/{build_id}/airgap", json={}).json()
    path = Path(os_client.state["settings"].image_dir) / bundle["filename"]
    with tarfile.open(path) as archive:
        names = archive.getnames()
        stem = names[0].split("/")[0]
        assert {f"{stem}/openshift/README.md", f"{stem}/openshift/openshift-admin.ini",
                f"{stem}/openshift/checks.json"} <= set(names)
        install = archive.extractfile(f"{stem}/INSTALL.txt").read().decode()
        sums = archive.extractfile(f"{stem}/SHA256SUMS").read().decode()
    assert "openshift/README.md" in install and "openshift/openshift-admin.ini" in sums


def test_other_builds_have_no_openshift_detail(os_client):
    project = os_client.post("/api/projects", json={
        "name": "plain", "template": "Minimal", "spec": {"base": {"distribution": "Alpine", "version": "3.22"}}}).json()
    build = os_client.post(f"/api/projects/{project['id']}/builds", json={}).json()
    run_pending_build(os_client, build["id"])
    detail = os_client.get(f"/api/builds/{build['id']}").json()
    assert detail["openshift"] is None and "openshift" not in detail["manifest"]
    assert os_client.get(f"/api/builds/{build['id']}/openshift/files/README.md").status_code == 404
