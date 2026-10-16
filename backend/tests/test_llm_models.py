"""LLM model downloads, the model tar stream, install-<model>.sh and the GPU kit.

A small fake Hugging Face server (API listing plus resolve URLs with Range
support) stands in for huggingface.co; the generated scripts run for real
with bash against files downloaded through the API.
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from layersmith.llm import catalog
from layersmith.models import Build, LlmModel
from layersmith.services import llm_models, splitting, vtar
from tests.test_api import client, run_pending_build  # noqa: F401 - fixture

# LAYERSMITH_TEST_BASH runs the scripts with another bash, e.g. an old one in a container.
BASH = os.environ.get("LAYERSMITH_TEST_BASH") or shutil.which("bash")
needs_shell = pytest.mark.skipif(BASH is None or shutil.which("sha256sum") is None or shutil.which("tar") is None,
                                 reason="needs bash, sha256sum and tar")
COMMIT = "a" * 40
PART = splitting.MIN_PART_SIZE


def blob_id(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


class FakeHub:
    """Repositories: {repo_id: {filename: bytes}}; big files are 'LFS'."""

    def __init__(self):
        self.repos: dict[str, dict[str, bytes]] = {}
        self.gated: set[str] = set()
        self.corrupt: set[str] = set()     # serve different bytes than the listing says
        self.drop_after: dict[str, int] = {}  # close the connection after N bytes, once
        self.ranges: list[str] = []
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                if path.startswith("/api/models/"):
                    repo = "/".join(path.split("/")[3:5])
                    if repo not in hub.repos:
                        return self.send_error(404)
                    siblings = []
                    for name, data in hub.repos[repo].items():
                        entry = {"rfilename": name, "size": len(data), "blobId": blob_id(data)}
                        if len(data) > 1000 or name.endswith(".safetensors"):
                            entry["lfs"] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                        siblings.append(entry)
                    body = json.dumps({"id": repo, "sha": COMMIT, "gated": "manual" if repo in hub.gated else False,
                                       "siblings": siblings, "cardData": {"license": "apache-2.0"}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    return self.wfile.write(body)
                parts = path.split("/")
                repo, name = "/".join(parts[1:3]), "/".join(parts[5:])
                data = hub.repos.get(repo, {}).get(name)
                if data is None or parts[3] != "resolve":
                    return self.send_error(404)
                if name in hub.corrupt:
                    data = bytes(len(data))
                start = 0
                if self.headers.get("Range"):
                    hub.ranges.append(self.headers["Range"])
                    start = int(self.headers["Range"].split("=")[1].rstrip("-"))
                    self.send_response(206)
                else:
                    self.send_response(200)
                body = data[start:]
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                limit = hub.drop_after.pop(name, None)
                self.wfile.write(body if limit is None else body[:limit])

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def hub(client):
    fake = FakeHub()
    fake.repos["Org/Tiny-Model"] = {
        "config.json": b'{"model_type": "qwen2"}',
        "tokenizer.json": b'{"version": "1.0"}' * 10,
        "model-00001-of-00002.safetensors": os.urandom(int(1.4 * PART)),
        "model-00002-of-00002.safetensors": os.urandom(PART // 3),
        "model.safetensors.index.json": b'{"weight_map": {}}',
        "consolidated.safetensors": os.urandom(5000),
        "original/params.json": b"{}",
        "modeling_custom.py": b"print('not fetched')",
    }
    client.state["settings"].hf_endpoint = fake.url
    client.state["settings"].split_part_bytes = PART
    service = client.state["model_service"]
    service.enqueue = lambda model_id: None  # tests run downloads synchronously
    yield fake
    fake.server.shutdown()


def request(client, repo="Org/Tiny-Model"):
    return client.post("/api/llm-models", json={"repo_id": repo})


def download(client, repo="Org/Tiny-Model") -> dict:
    response = request(client, repo)
    assert response.status_code == 202, response.text
    client.state["model_service"].run(response.json()["id"])
    return client.get("/api/llm-models").json()[0]


def fetch_all(client, url_files: list[dict], target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    for item in url_files:
        response = client.get(item["url"])
        assert response.status_code == 200, (item["name"], response.text)
        (target / item["name"]).write_bytes(response.content)
    return target


# ------------------------------------------------------------ catalogue

def test_file_selection_keeps_what_vllm_loads():
    siblings = [{"rfilename": n, "size": 1} for n in (
        "config.json", "generation_config.json", "tokenizer.json", "tokenizer.model", "merges.txt",
        "chat_template.jinja", "model-00001-of-00002.safetensors", "model.safetensors.index.json",
        "consolidated.safetensors", "original/consolidated.00.pth", "metal/model.bin", "modeling_x.py",
        "pytorch_model.bin", ".gitattributes", "README.md")]
    names = [f["name"] for f in catalog.select_files(siblings)]
    assert "consolidated.safetensors" not in names and "pytorch_model.bin" not in names
    assert not any("/" in n for n in names) and "modeling_x.py" not in names and ".gitattributes" not in names
    assert {"config.json", "tokenizer.model", "chat_template.jinja", "model.safetensors.index.json"} <= set(names)
    # Without sharded model-* weights, a single consolidated file is all there is.
    alone = catalog.select_files([{"rfilename": "config.json", "size": 1},
                                  {"rfilename": "consolidated.safetensors", "size": 1}])
    assert [f["name"] for f in alone] == ["config.json", "consolidated.safetensors"]


def test_catalog_entries_are_open_and_well_formed():
    assert len({e["key"] for e in catalog.CATALOG}) == len(catalog.CATALOG)
    for entry in catalog.CATALOG:
        assert catalog.REPO_RE.match(entry["repo_id"]) and entry["purpose"] in catalog.PURPOSES
        assert entry["gpus"] >= 1 and entry["size_gb"] > 0
        assert not entry["repo_id"].startswith(("meta-llama/", "google/"))  # gated upstream


def test_folder_names_are_short_and_unique():
    assert catalog.folder_name("Qwen/Qwen3-8B", set()) == "Qwen3-8B"
    assert catalog.folder_name("Other/Qwen3-8B", {"Qwen3-8B"}) == "Other--Qwen3-8B"
    assert catalog.folder_name("Other/Qwen3-8B", {"Qwen3-8B", "Other--Qwen3-8B"}) == "Other--Qwen3-8B-2"


# ------------------------------------------------------------ tar stream

def test_tar_stream_is_a_standard_archive_and_ranges_add_up(tmp_path):
    folder = tmp_path / "m"
    folder.mkdir()
    files = []
    for name, size in (("a.json", 10), ("b.safetensors", 3 * 512 + 7), ("c.txt", 0), ("d" * 120 + ".json", 5)):
        (folder / name).write_bytes(os.urandom(size))
        files.append({"name": name, "size": size})
    lay = vtar.layout(folder, "Tiny", files, mtime=1704067200)
    whole = vtar.as_bytes(lay)
    assert len(whole) == lay.size and lay.size % 512 == 0
    with tarfile.open(fileobj=io.BytesIO(whole)) as archive:
        members = archive.getmembers()
        assert [m.name for m in members] == ["Tiny"] + [f"Tiny/{f['name']}" for f in files]
        for f in files:
            assert archive.extractfile(f"Tiny/{f['name']}").read() == (folder / f["name"]).read_bytes()
        assert all(m.uid == 0 and m.mtime == 1704067200 for m in members)
    # Any split of the stream concatenates back to it.
    for size in (1, 511, 512, 1000, 4096):
        joined = b"".join(b"".join(lay.read(o, n)) for o, n in splitting.plan(lay.size, size))
        assert joined == whole
    # Deterministic: the same folder gives the same bytes.
    assert vtar.as_bytes(vtar.layout(folder, "Tiny", files, mtime=1704067200)) == whole


@needs_shell
def test_tar_stream_unpacks_with_gnu_tar(tmp_path):
    folder = tmp_path / "m"
    folder.mkdir()
    (folder / "config.json").write_bytes(b"{}")
    (folder / ("w" * 150 + ".safetensors")).write_bytes(os.urandom(2000))
    files = [{"name": p.name, "size": p.stat().st_size} for p in sorted(folder.iterdir())]
    (tmp_path / "x.tar").write_bytes(vtar.as_bytes(vtar.layout(folder, "Tiny", files, mtime=1704067200)))
    out = tmp_path / "out"
    out.mkdir()
    subprocess.run(["tar", "-xf", str(tmp_path / "x.tar"), "-C", str(out)], check=True)
    for f in files:
        assert (out / "Tiny" / f["name"]).read_bytes() == (folder / f["name"]).read_bytes()


# --------------------------------------------------------------- download

def test_download_fetches_only_wanted_files_and_verifies_them(client, hub):
    model = download(client)
    assert model["status"] == "ready", model["error"]
    assert model["name"] == "Tiny-Model" and model["revision"] == COMMIT and model["license"] == "apache-2.0"
    folder = Path(client.state["settings"].model_dir) / "Tiny-Model"
    assert sorted(p.name for p in folder.iterdir()) == sorted(
        ["config.json", "tokenizer.json", "model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors",
         "model.safetensors.index.json"])
    for name in ("model-00001-of-00002.safetensors", "config.json"):
        assert (folder / name).read_bytes() == hub.repos["Org/Tiny-Model"][name]
    assert not list(Path(client.state["settings"].model_dir).glob(".download-*"))
    assert model["part_count"] >= 2 and model["done_bytes"] == model["total_bytes"]
    # Asking again returns the same model rather than downloading it twice.
    assert request(client).json()["id"] == model["id"]
    catalog_view = client.get("/api/llm-models/catalog").json()
    assert catalog_view["models"] and catalog_view["model_dir"] == str(client.state["settings"].model_dir)


def test_gated_missing_and_weightless_repositories_are_refused(client, hub):
    hub.repos["Meta/Gated"] = dict(hub.repos["Org/Tiny-Model"])
    hub.gated.add("Meta/Gated")
    hub.repos["Org/NoWeights"] = {"config.json": b"{}", "pytorch_model.bin": b"x"}
    gated = request(client, "Meta/Gated")
    assert gated.status_code == 422 and "gated" in gated.json()["detail"]
    assert request(client, "Org/Nope").status_code == 422
    assert "safetensors" in request(client, "Org/NoWeights").json()["detail"]
    assert request(client, "not a repo").status_code == 422
    assert client.get("/api/llm-models").json() == []


def test_not_enough_space_is_refused_before_downloading(client, hub, monkeypatch):
    monkeypatch.setattr(llm_models.shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(10, 10, 1000))
    response = request(client)
    assert response.status_code == 422 and "Not enough space" in response.json()["detail"]


def test_a_corrupt_download_never_becomes_ready(client, hub):
    hub.corrupt.add("model-00001-of-00002.safetensors")
    model = download(client)
    assert model["status"] == "failed" and "does not match the sha256" in model["error"]
    hub.corrupt.clear()
    retried = client.post(f"/api/llm-models/{model['id']}/retry")
    assert retried.status_code == 202
    client.state["model_service"].run(model["id"])
    assert client.get("/api/llm-models").json()[0]["status"] == "ready"


def test_an_interrupted_download_resumes_with_a_range_request(client, hub):
    hub.drop_after["model-00001-of-00002.safetensors"] = PART // 2
    model = download(client)
    assert model["status"] == "failed" and "Retry to continue" in model["error"]
    client.post(f"/api/llm-models/{model['id']}/retry")
    client.state["model_service"].run(model["id"])
    assert client.get("/api/llm-models").json()[0]["status"] == "ready"
    assert hub.ranges == [f"bytes={PART // 2}-"]


def test_unfinished_downloads_are_resumed_after_a_restart(client, hub):
    model_id = request(client).json()["id"]
    service = client.state["model_service"]
    queued = []
    service.enqueue = queued.append
    service.recover()
    assert queued == [model_id]


def test_delete_removes_the_folder(client, hub):
    model = download(client)
    folder = Path(client.state["settings"].model_dir) / model["name"]
    assert folder.is_dir()
    assert client.delete(f"/api/llm-models/{model['id']}").json()["freed_bytes"] == model["total_bytes"]
    assert not folder.exists() and client.get("/api/llm-models").json() == []


def test_changed_files_invalidate_the_split(client, hub):
    model = download(client)
    folder = Path(client.state["settings"].model_dir) / model["name"]
    (folder / "config.json").write_bytes(b'{"changed": true}')
    assert client.get(f"/api/llm-models/{model['id']}/files").status_code == 409


# -------------------------------------------------------- install script

def model_kit(client, tmp_path) -> tuple[dict, Path]:
    model = download(client)
    listing = client.get(f"/api/llm-models/{model['id']}/files").json()
    for item in listing["files"]:
        if item["kind"] == "part":
            assert item["size"] <= PART
    return model, fetch_all(client, listing["files"], tmp_path / "transfer")


@needs_shell
def test_install_script_puts_the_model_in_the_models_dir(client, hub, tmp_path):
    model, transfer = model_kit(client, tmp_path)
    script = transfer / f"install-{model['name']}.sh"
    models_dir = tmp_path / "opt-models"
    result = subprocess.run([BASH, str(script), "--dir", str(models_dir)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Verifying model files... OK" in result.stdout and f"/models/{model['name']}" in result.stdout
    installed = models_dir / model["name"]
    for name, data in hub.repos["Org/Tiny-Model"].items():
        if (installed / name).exists():
            assert (installed / name).read_bytes() == data
    assert not list(models_dir.glob(".*partial*"))
    # The checksum list of the files works with plain sha256sum -c inside the folder.
    shutil.copy(transfer / f"{model['name']}.files.sha256", installed)
    assert subprocess.run(["sha256sum", "-c", f"{model['name']}.files.sha256"], cwd=installed,
                          capture_output=True).returncode == 0
    (installed / f"{model['name']}.files.sha256").unlink()
    again = subprocess.run([BASH, str(script), "--dir", str(models_dir), "--remove-parts"],
                           capture_output=True, text=True)
    assert again.returncode == 0 and "already installed" in again.stdout
    assert not list(transfer.glob("*.part-*"))


@needs_shell
def test_install_script_reports_missing_and_corrupt_parts(client, hub, tmp_path):
    model, transfer = model_kit(client, tmp_path)
    script = transfer / f"install-{model['name']}.sh"
    parts = sorted(transfer.glob("*.part-*"))
    data = parts[0].read_bytes()
    parts[0].write_bytes(bytes([data[0] ^ 1]) + data[1:])
    corrupt = subprocess.run([BASH, str(script), "--dir", str(tmp_path / "m")], capture_output=True, text=True)
    assert corrupt.returncode == 3 and f"corrupt: {parts[0].name}" in corrupt.stderr
    parts[1].unlink()
    missing = subprocess.run([BASH, str(script), "--dir", str(tmp_path / "m")], capture_output=True, text=True)
    assert missing.returncode == 2 and f"missing: {parts[1].name}" in missing.stderr
    assert not (tmp_path / "m" / model["name"]).exists()


@needs_shell
def test_install_script_refuses_a_different_existing_folder(client, hub, tmp_path):
    model, transfer = model_kit(client, tmp_path)
    (tmp_path / "m" / model["name"]).mkdir(parents=True)
    (tmp_path / "m" / model["name"] / "config.json").write_text("{}")
    script = transfer / f"install-{model['name']}.sh"
    refused = subprocess.run([BASH, str(script), "--dir", str(tmp_path / "m")], capture_output=True, text=True)
    assert refused.returncode == 5
    forced = subprocess.run([BASH, str(script), "--dir", str(tmp_path / "m"), "--force"],
                            capture_output=True, text=True)
    assert forced.returncode == 0, forced.stderr


# ----------------------------------------------------------------- GPU kit

def gpu_build(client, monkeypatch, size: int) -> str:
    monkeypatch.setenv("LAYERSMITH_GPU_MIN_FREE_GB", "0")
    project = client.post("/api/projects", json={
        "name": "gpu-benchmark", "repository": "localhost/gpu-benchmark",
        "template": "NVIDIA GPU & LLM Benchmark", "spec": {"gpu_benchmark": {}}})
    assert project.status_code == 201, project.text
    build_id = client.post(f"/api/projects/{project.json()['id']}/builds", json={}).json()["id"]
    run_pending_build(client, build_id)
    with client.state["session_factory"]() as session:
        build = session.get(Build, build_id)
        assert build.status == "ready", build.error
        data = os.urandom(size)
        Path(build.export_path).write_bytes(data)
        build.export_size, build.export_sha256 = size, hashlib.sha256(data).hexdigest()
        build.image_size, build.image_id = 3 * size, "sha256:" + "b" * 64
        session.commit()
    return build_id


def test_kit_lists_image_parts_model_parts_and_scripts(client, hub, monkeypatch):
    build_id = gpu_build(client, monkeypatch, int(1.5 * PART))
    model = download(client)
    first = client.get(f"/api/builds/{build_id}/kit?models={model['id']}").json()
    assert first["ready"] is False and first["image_split"] == "none"
    assert client.get(f"/api/builds/{build_id}/kit/download-all.sh?models={model['id']}").status_code == 409
    client.state["split_service"].run(build_id)
    kit = client.get(f"/api/builds/{build_id}/kit?models={model['id']}").json()
    assert kit["ready"] and kit["models"] == [model["id"]]
    names = [f["name"] for f in kit["files"]]
    assert "gpu-benchmark-1.0.0.tar.part-000" in names and "assemble-image.sh" in names
    assert f"install-{model['name']}.sh" in names and "setup.sh" in names and "start_container.sh" in names
    assert names[-1] == "download-all.sh"
    script = client.get(f"/api/builds/{build_id}/kit/download-all.sh?models={model['id']}",
                        headers={"x-forwarded-proto": "https", "x-forwarded-host": "layersmith.example"}).text
    assert "https://layersmith.example" in script and f"/api/llm-models/{model['id']}/files/" in script
    setup = client.get(f"/api/builds/{build_id}/kit/setup.sh?models={model['id']}").text
    assert f"  {model['name']}" in setup and "localhost/gpu-benchmark:1.0.0" in setup
    # Not a GPU benchmark build: no kit.
    other = client.post("/api/projects", json={"name": "plain", "template": "Minimal",
                                               "spec": {"base": {"distribution": "Alpine", "version": "3.22"}}})
    plain = client.post(f"/api/projects/{other.json()['id']}/builds", json={}).json()["id"]
    run_pending_build(client, plain)
    assert client.get(f"/api/builds/{plain}/kit").status_code == 404


def fake_tools(directory: Path, log: Path, image: str) -> dict:
    """podman, nvidia-ctk and nvidia-smi stand-ins that record what they were asked."""
    directory.mkdir()
    state = directory / "loaded"
    (directory / "podman").write_text(f"""#!/bin/sh
echo "podman $*" >> {log}
case "$1 $2" in
  "image exists") [ -f {state} ] ;;
  "image inspect") echo sha256:{'b' * 64} ;;
  "info --format") echo {directory}/store ;;
  "load -i") touch {state} ;;
  *) exit 0 ;;
esac
""")
    (directory / "nvidia-ctk").write_text("#!/bin/sh\necho nvidia.com/gpu=0; echo nvidia.com/gpu=all\n")
    (directory / "nvidia-smi").write_text("#!/bin/sh\necho 'GPU 0: NVIDIA H200 (UUID: GPU-test)'\n")
    for tool in ("podman", "nvidia-ctk", "nvidia-smi"):
        (directory / tool).chmod(0o755)
    (directory / "store").mkdir()
    return {**os.environ, "PATH": f"{directory}:{os.environ['PATH']}"}


@needs_shell
def test_setup_restores_loads_installs_and_starts(client, hub, monkeypatch, tmp_path):
    build_id = gpu_build(client, monkeypatch, int(1.5 * PART))
    model = download(client)
    client.state["split_service"].run(build_id)
    kit = client.get(f"/api/builds/{build_id}/kit?models={model['id']}").json()
    transfer = fetch_all(client, [f for f in kit["files"] if f["name"] != "download-all.sh"], tmp_path / "kit")
    log = tmp_path / "podman.log"
    env = {**fake_tools(tmp_path / "bin", log, kit["image"]), "TMPDIR": str(tmp_path),
           "RESULTS_DIR": str(tmp_path / "results")}
    models_dir = tmp_path / "models"
    result = subprocess.run([BASH, str(transfer / "setup.sh"), "--models-dir", str(models_dir)],
                            capture_output=True, text=True, env=env, cwd=tmp_path, stdin=subprocess.DEVNULL)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text()
    assert "podman load -i gpu-benchmark-1.0.0.tar" in calls
    assert f"-v {models_dir}:/models:ro" in calls and "--device nvidia.com/gpu=all" in calls
    assert (models_dir / model["name"] / "config.json").is_file()
    assert "NVIDIA H200" in result.stdout and "== 5/5 Ready" in result.stdout
    assert not list(transfer.glob("*.part-*"))  # removed once restored and verified
    # Running it again finds everything done.
    again = subprocess.run([BASH, str(transfer / "setup.sh"), "--models-dir", str(models_dir), "--no-start"],
                           capture_output=True, text=True, env=env, cwd=tmp_path)
    assert again.returncode == 0, again.stderr
    assert "already loaded" in again.stdout and "already installed" in again.stdout


@needs_shell
def test_setup_stops_when_the_disk_is_too_small(client, hub, monkeypatch, tmp_path):
    build_id = gpu_build(client, monkeypatch, int(1.5 * PART))
    with client.state["session_factory"]() as session:
        session.get(Build, build_id).image_size = 10**15  # a petabyte image
        session.commit()
    client.state["split_service"].run(build_id)
    kit = client.get(f"/api/builds/{build_id}/kit").json()
    transfer = fetch_all(client, [f for f in kit["files"] if f["name"] != "download-all.sh"], tmp_path / "kit")
    env = {**fake_tools(tmp_path / "bin", tmp_path / "log", kit["image"]), "TMPDIR": str(tmp_path)}
    result = subprocess.run([BASH, str(transfer / "setup.sh"), "--no-start"], capture_output=True, text=True,
                            env=env, cwd=tmp_path)
    assert result.returncode == 4 and "NOT ENOUGH" in result.stdout and "Not enough disk space" in result.stderr
    assert sorted(transfer.glob("*.part-*"))  # nothing touched


@needs_shell
def test_download_all_fetches_and_verifies(client, hub, monkeypatch, tmp_path):
    """download-all.sh against the real API, served by uvicorn in a thread."""
    import uvicorn

    model = download(client)
    app = client.app
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        pass
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        script = client.get(f"/api/llm-models/{model['id']}/files/download-{model['name']}.sh").text
        target = tmp_path / "home"
        target.mkdir()
        (target / "download.sh").write_text(script)
        result = subprocess.run([BASH, "download.sh", f"http://127.0.0.1:{port}"], cwd=target,
                                capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "downloaded and verified" in result.stdout
        assert (target / f"install-{model['name']}.sh").is_file() and sorted(target.glob("*.part-*"))
        again = subprocess.run([BASH, "download.sh", f"http://127.0.0.1:{port}"], cwd=target,
                               capture_output=True, text=True, timeout=120)
        assert again.stdout.count("already here") >= 2
    finally:
        server.should_exit = True
        thread.join(timeout=10)
