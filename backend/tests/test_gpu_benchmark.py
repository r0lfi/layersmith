"""NVIDIA GPU & LLM Benchmark: recipe, Containerfile, LayerSmith integration and the gpu-bench runtime.

Nothing here needs a GPU. The LLM load generator is exercised against a fake
OpenAI-compatible streaming server, so these tests prove the measuring and
reporting code, not GPU performance.
"""

import json
import subprocess
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from layersmith.api import (GpuBenchmarkResolveIn, ProjectIn, _gpu_benchmark_detail, gpu_benchmark_catalog_view,
                            gpu_benchmark_resolve, preview)
from layersmith.core import containerfile as cf
from layersmith.core.spec import InvalidSpec, export_filename, validate_spec
from layersmith.gpu_benchmark import catalog, render
from layersmith.gpu_benchmark.runtime import cli, distributed, llm, reports, runs
from layersmith.gpu_benchmark.runtime.benchmarks import (BenchmarkError, _parse_nccl_metrics, parse_gpu_list,
                                                         visible_devices_env)
from layersmith.gpu_benchmark.runtime.events import EMITTER
from layersmith.gpu_benchmark.runtime.system import health_checks
from layersmith.models import Build, Project
from layersmith.services.builds import manifest_for
from tests.test_builds import env, make_build  # noqa: F401

PROJECT = {"name": "gpu-benchmark", "description": "GPU tests"}


@pytest.fixture(autouse=True)
def _reset_emitter():
    EMITTER.stop_event.clear()
    EMITTER.collected.clear()
    EMITTER.configure(json_mode=False, log_path=None)
    yield
    EMITTER.stop_event.clear()
    EMITTER.collected.clear()
    EMITTER.configure(json_mode=False, log_path=None)


# ------------------------------------------------------------- recipe

def test_default_recipe_is_the_compatible_cuda13_torch_vllm_set():
    recipe = catalog.resolve()
    assert recipe["base"] == "docker.io/nvidia/cuda:13.0.2-base-ubuntu24.04"
    assert (recipe["torch"], recipe["vllm"]) == ("2.11.0", "0.26.0")
    # transformers 5.17 requires tokenizers >= 0.23.1 (0.5.0 pinned 0.22.2, which cannot install).
    assert recipe["transformers"] == "5.17.0" and recipe["tokenizers"].startswith("0.23.")
    assert recipe["tools"] == ["llm", "compute", "memory", "nccl"]
    assert 90 in recipe["gpu_architectures"] and 120 in recipe["gpu_architectures"]
    assert recipe["driver_minimum"] == "580.65.06"
    assert 5 < recipe["estimated_size_gb"] < 20
    cuda12 = catalog.resolve({"cuda_version": "12.8.2", "tools": ["compute", "memory"]})
    assert cuda12["vllm"] is None and cuda12["separate_torch_index"]
    with pytest.raises(catalog.RecipeError, match="no matching vLLM"):
        catalog.resolve({"cuda_version": "12.8.2"})


@pytest.mark.parametrize("request_", [
    {"cuda_version": "latest"}, {"python_version": "3.14"}, {"tools": ["unknown"]}, {"tools": []},
    {"tools": [[]]}, {"mirrors": "not-an-object"}, {"mirrors": {"pypi_index": "https://mirror.example/\necho x"}},
    {"mirrors": {"private_token": "https://mirror.example"}}, {"mirrors": {"pypi_index": "https://m.example/#x"}},
])
def test_invalid_requests_and_mirrors_are_rejected(request_):
    with pytest.raises(catalog.RecipeError):
        catalog.resolve(request_)


def test_legacy_050_spec_with_cuda_samples_mirror_still_resolves():
    recipe = catalog.resolve({"mirrors": {"cuda_samples_repository": "https://github.com/NVIDIA/cuda-samples.git",
                                          "torch_index": "https://download.pytorch.org/whl/cu130"}})
    assert recipe["vllm"] == "0.26.0"


def test_spec_pins_base_and_amd64_and_keeps_mirrors():
    spec = validate_spec({"gpu_benchmark": {"mirrors": {"pypi_index": "https://mirror.example/simple"}}})
    assert spec["base"]["source"] == "docker.io/nvidia/cuda:13.0.2-base-ubuntu24.04"
    assert spec["architecture"] == "amd64"
    assert spec["gpu_benchmark"]["mirrors"]["pypi_index"] == "https://mirror.example/simple"
    with pytest.raises(InvalidSpec, match="amd64"):
        validate_spec({"architecture": "arm64", "gpu_benchmark": {}})
    with pytest.raises(InvalidSpec, match="either"):
        validate_spec({"training": {"profile": "hf-finetune"}, "gpu_benchmark": {}})


def test_localhost_repository_exports_as_plain_archive_name():
    assert export_filename("localhost/gpu-benchmark", "1.0.0") == "gpu-benchmark-1.0.0.tar"
    assert export_filename("layersmith/gpu-benchmark", "1.0.0") == "layersmith-gpu-benchmark-1.0.0.tar"
    assert render.archive_name("localhost/gpu-benchmark:1.0.0") == "gpu-benchmark-1.0.0.tar"


# -------------------------------------------------------- Containerfile

def _containerfile(tools=None):
    spec = validate_spec({"gpu_benchmark": {"tools": tools} if tools else {}})
    return cf.generate(PROJECT, spec)


def test_containerfile_starts_tui_offline_and_never_touches_host_drivers():
    text, packages, warnings = _containerfile()
    assert not warnings
    assert "FROM docker.io/nvidia/cuda:13.0.2-base-ubuntu24.04" in text
    assert 'ENTRYPOINT ["/usr/bin/tini", "--"]' in text and 'CMD ["gpu-bench", "tui"]' in text
    for offline in ("HF_HUB_OFFLINE=1", "TRANSFORMERS_OFFLINE=1", "VLLM_NO_USAGE_STATS=1"):
        assert offline in text
    assert "vllm==0.26.0" in text and "torch==2.11.0" in text and "textual==8.2.8" in text
    assert "nvidia-driver" not in text and "nvidia-utils" not in text
    assert "pip check" in text and "requirements.lock" in text
    assert {"tini", "cuda-nvcc-13-0", "openmpi-bin", "perftest", "rdma-core"} <= set(packages)
    # The apt mirror step runs before Python exists in the base image.
    prelude = text.index("GPU_BENCH_APT_MAIN")
    assert "python3 -c" not in text[prelude:text.index("apt-get update")]


def test_containerfile_builds_mpi_and_single_node_nccl_tests_and_nvbandwidth():
    text, _, _ = _containerfile()
    assert "MPI=0" in text and "MPI=1" in text and "sendrecv alltoall" in text
    assert "compute_120,code=sm_120" in text and "compute_90,code=sm_90" in text
    assert "compute_120,code=compute_120" in text  # PTX for newer GPUs
    assert "nvbandwidth" in text and "CMAKE_CUDA_ARCHITECTURES=75;80;86;89;90;100;120" in text
    only_compute, packages, _ = _containerfile(["compute"])
    assert "NCCL_TESTS_REPOSITORY" not in only_compute and "NVBANDWIDTH_REPOSITORY" not in only_compute
    assert "vllm==" not in only_compute and "nccl-tests=\"not-installed\"" in only_compute
    assert "openmpi-bin" not in packages
    assert "GPU_BENCH_TOOLS=compute" in only_compute


def test_flashinfer_jit_gets_the_wheel_headers_and_a_compile_check():
    text, _, _ = _containerfile()
    assert "site-packages/nvidia/*/include" in text and "cublas cublasLt curand nvrtc" in text
    assert "#include <curand.h>" in text and "nvcc -c /tmp/jit-check.cu" in text
    assert "FLASHINFER_WORKSPACE_BASE=/cache" in text
    only_compute, _, _ = _containerfile(["compute"])
    assert "jit-check" not in only_compute


def test_generated_run_instructions_have_valid_shell_syntax():
    for tools in (None, ["compute"], ["nccl", "memory"]):
        text, _, _ = _containerfile(tools)
        commands, current = [], None
        for line in text.splitlines():
            if current is None:
                if line.startswith("RUN "):
                    current = line[4:]
            else:
                current += "\n" + line
            if current is not None and not current.rstrip().endswith("\\"):
                commands.append(current.replace("\\\n", " "))
                current = None
        assert commands
        for command in commands:
            result = subprocess.run(["/bin/sh", "-n", "-c", command], capture_output=True, text=True)
            assert result.returncode == 0, result.stderr + "\n" + command


def test_cuda_12_variant_installs_torch_from_its_own_index_without_vllm():
    spec = validate_spec({"gpu_benchmark": {"cuda_version": "12.8.2", "tools": ["compute", "nccl"]}})
    text, packages, _ = cf.generate(PROJECT, spec)
    assert "FROM docker.io/nvidia/cuda:12.8.2-base-ubuntu24.04" in text
    assert "GPU_BENCH_TORCH_INDEX=https://download.pytorch.org/whl/cu128" in text
    assert "vllm==" not in text and "cuda-nvcc-12-8" in packages


# ------------------------------------------------------ LayerSmith glue

def test_build_context_has_runtime_package_scripts_and_airgap_files(env, monkeypatch):
    monkeypatch.setenv("LAYERSMITH_GPU_MIN_FREE_GB", "0")
    build_id = make_build(env, {"gpu_benchmark": {"tools": ["compute", "memory"]}})
    env["service"].run(build_id)
    with env["factory"]() as session:
        build = session.get(Build, build_id)
        project = session.get(Project, build.project_id)
        assert build.status == "ready", build.error
        context = env["backend"].context
        for path in ("gpu-benchmark/bin/gpu-bench", "gpu-benchmark/gpu_bench/cli.py",
                     "gpu-benchmark/gpu_bench/tui/app.py", "gpu-benchmark/gpu_bench/tui/app.tcss",
                     "gpu-benchmark/scripts/slurm-dist-nccl.sbatch", "gpu-benchmark/recipe.json"):
            assert context.joinpath(path).is_file(), path
        assert not context.joinpath("gpu-benchmark/gpu_bench/bin").exists()
        detail = _gpu_benchmark_detail(build, "podman")
        assert detail["export"]["available"] and detail["export"]["sha256"] == build.export_sha256
        assert detail["export"]["format"].startswith("OCI archive")
        assert "podman load -i" in detail["getting_started"]["load"]
        assert "--device nvidia.com/gpu=all" in detail["getting_started"]["podman"]
        assert manifest_for(project, build)["gpu_benchmark"]["tools"] == ["compute", "memory"]
    with tarfile.open(env["service"].airgap_bundle(build_id)) as archive:
        names = archive.getnames()
    assert any(name.endswith("/gpu-benchmark/README.md") for name in names)


def test_gpu_build_stops_early_without_disk_space(env, monkeypatch):
    monkeypatch.setenv("LAYERSMITH_GPU_MIN_FREE_GB", "999999")
    build_id = make_build(env, {"gpu_benchmark": {"tools": ["compute"]}})
    env["service"].run(build_id)
    with env["factory"]() as session:
        build = session.get(Build, build_id)
        assert build.status == "failed" and "Not enough disk space" in build.error


def test_catalog_resolve_and_preview_api():
    view = gpu_benchmark_catalog_view()
    assert view["category"]["name"] == "NVIDIA GPU & LLM Benchmark"
    assert set(view["core_components"]) == {"diagnostics", "reports", "tui"}
    assert view["defaults"]["repository"] == "localhost/gpu-benchmark"
    assert {"required_free_gb", "free_gb"} <= set(view["storage"])
    resolved = gpu_benchmark_resolve(GpuBenchmarkResolveIn(gpu_benchmark={"tools": ["nccl"]}))
    assert resolved["ok"] and resolved["recipe"]["tools"] == ["nccl"]
    bad = gpu_benchmark_resolve(GpuBenchmarkResolveIn(gpu_benchmark={"cuda_version": "12.8.2"}))
    assert not bad["ok"] and "vLLM" in bad["error"]
    text = preview(ProjectIn(name="gpu-benchmark", template="NVIDIA GPU & LLM Benchmark",
                             spec={"gpu_benchmark": {"tools": ["nccl"]}}))["containerfile"]
    assert "NCCL_TESTS_REPOSITORY" in text


# -------------------------------------------------------- runtime: CLI

def test_every_command_has_help(capsys):
    for command in (None, "tui", "info", "health", "topology", "models", "llm", "compute", "memory", "nccl",
                    "dist", "full", "report", "compare", "runs"):
        argv = ["--help"] if command is None else [command, "--help"]
        with pytest.raises(SystemExit) as raised:
            cli.main(argv)
        assert raised.value.code == 0
        assert "usage:" in capsys.readouterr().out.lower()


def test_tui_refuses_without_a_terminal(capsys):
    assert cli.main(["tui"]) == 2
    assert "-it" in capsys.readouterr().err


def _no_gpu_info():
    return {"timestamp": "2026-10-15T12:00:00+00:00", "hostname": "test-host", "gpu_count": 0, "gpus": [],
            "torch_cuda_available": False, "rdma_devices": [], "network_interfaces": [], "packages": {}}


def test_full_without_gpu_records_skips_in_its_own_run_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "collect_system_info", _no_gpu_info)
    # Exit status 1: the health check failed (no GPU); the report is still complete.
    assert cli.main(["full", "--output", str(tmp_path)]) == 1
    first = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(first) == 1 and first[0].name.endswith("_nogpu")
    data = json.loads((first[0] / "benchmark.json").read_text())
    by_test = {row["test"]: row for row in data["results"]}
    assert by_test["health"]["status"] == "FAILED"
    assert all(by_test[t]["status"] == "SKIPPED" and by_test[t]["error"] for t in ("compute", "memory", "nccl", "llm"))
    for name in ("benchmark.json", "benchmark.csv", "benchmark.html", "system-info.json"):
        assert (first[0] / name).is_file()
    assert (first[0] / "logs").is_dir()
    # A second run never overwrites the first.
    EMITTER.collected.clear()
    assert cli.main(["full", "--output", str(tmp_path)]) == 1
    assert len([p for p in tmp_path.iterdir() if p.is_dir()]) == 2


def test_compute_without_gpu_is_recorded_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "collect_system_info", _no_gpu_info)
    code = cli.main(["compute", "--output", str(tmp_path), "--duration", "1"])
    run = next(tmp_path.iterdir())
    entry = json.loads((run / "benchmark.json").read_text())["results"][0]
    assert entry["status"] in ("UNAVAILABLE", "FAILED") and entry["error"]
    assert code in (0, 1)


def test_non_zero_rank_never_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "collect_system_info", _no_gpu_info)
    monkeypatch.setenv("RANK", "3")
    cli.main(["full", "--output", str(tmp_path)])
    assert not list(tmp_path.iterdir())


def test_events_mode_prints_json_lines(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "collect_system_info", _no_gpu_info)
    cli.main(["full", "--output", str(tmp_path), "--events"])
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    kinds = [event["event"] for event in events]
    assert kinds[0] == "run" and kinds[-1] == "done" and "result" in kinds
    assert events[-1]["paths"]["html"].endswith("benchmark.html")


def test_health_marks_unsupported_features_na_and_checks_driver():
    info = {"gpu_count": 1, "gpus": [{"index": 0, "name": "NVIDIA H200", "compute_capability": "9.0"}],
            "torch_cuda_available": True, "torch_version": "2.11.0", "torch_cuda_version": "13.0",
            "driver_version": "580.65.06", "torch_arch_list": ["sm_90", "sm_120"], "rdma_devices": [],
            "network_interfaces": [], "host": {}}
    checks = {row["name"]: row for row in health_checks(info)["checks"]}
    assert checks["CUDA driver compatibility"]["status"] == "PASS"
    assert checks["GPU 0 architecture"]["status"] == "PASS"
    assert checks["RDMA devices"]["status"] == "N/A"
    info["driver_version"] = "570.26.00"
    assert {r["name"]: r for r in health_checks(info)["checks"]}["CUDA driver compatibility"]["status"] == "FAIL"


def test_gpu_selection_helpers(monkeypatch):
    assert parse_gpu_list("all", 4) == [0, 1, 2, 3]
    assert parse_gpu_list("2,0", 4) == [0, 2]
    with pytest.raises(BenchmarkError):
        parse_gpu_list("5", 4)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "4,5,6")
    assert visible_devices_env([0, 2]) == "4,6"


def test_nccl_output_parser_and_transport_detection():
    output = ("#       size    count  type  redop  root   time  algbw  busbw #wrong   time  algbw  busbw #wrong\n"
              "     1048576   262144 float    sum    -1  45.10  23.25  40.69      0  44.90  23.35  40.87      0\n"
              "# Avg bus bandwidth    : 40.78\n")
    rows = _parse_nccl_metrics(output)
    assert rows[0]["bytes"] == 1048576 and rows[0]["busbw_gb_per_second"] == 40.87
    assert rows[0]["out_of_place_busbw_gb_per_second"] == 40.69
    assert _parse_nccl_metrics("no rows") == []
    assert distributed.nccl_transport("NCCL INFO NET/IB : Using [0]mlx5_0:1/RoCE [1]mlx5_1:1/RoCE")[
        "transport"].startswith("RDMA")
    assert distributed.nccl_transport("NCCL INFO NET/Socket : Using [0]eth0:10.0.0.1<0>")[
        "transport"].startswith("TCP")
    assert distributed.parse_size("1G") == 1 << 30 and distributed.parse_size("64K") == 65536


# ------------------------------------------------- runtime: LLM metrics

class FakeVllm(BaseHTTPRequestHandler):
    """Streams completions like vLLM with continuous usage stats; counts tokens for /metrics."""

    totals = {"prompt": 0, "generation": 0}
    delay = 0.002

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/metrics":
            body = (f"vllm:prompt_tokens_total{{model_name=\"gpu-bench\"}} {self.totals['prompt']}\n"
                    f"vllm:generation_tokens_total{{model_name=\"gpu-bench\"}} {self.totals['generation']}\n").encode()
        else:
            body = b'{"data": []}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert request["ignore_eos"] and request["stream_options"]["continuous_usage_stats"]
        prompt = len(request["prompt"])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for index in range(1, request["max_tokens"] + 1):
                time.sleep(type(self).delay)
                chunk = {"choices": [{"text": "x"}], "usage": {"prompt_tokens": prompt, "completion_tokens": index}}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()
            final = {"choices": [], "usage": {"prompt_tokens": prompt, "completion_tokens": request["max_tokens"]}}
            self.wfile.write(f"data: {json.dumps(final)}\n\ndata: [DONE]\n\n".encode())
        except OSError:
            return  # client aborted
        type(self).totals["prompt"] += prompt
        type(self).totals["generation"] += request["max_tokens"]


@pytest.fixture
def fake_server():
    FakeVllm.totals = {"prompt": 0, "generation": 0}
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeVllm)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()


def test_load_generator_measures_streams_with_server_token_counts(fake_server):
    generator = llm.LoadGenerator(fake_server, output_tokens=20)
    prompts = [[1000 + i] * 64 for i in range(12)]
    before = llm.scrape_counters(fake_server)
    records, started, finished = generator.run(prompts, concurrency=4)
    after = llm.scrape_counters(fake_server)
    metrics = llm.summarize(records, started, finished, 4, 64, 20)
    assert metrics["completed_requests"] == 12 and metrics["failed_requests"] == 0
    assert metrics["total_output_tokens"] == 240 and metrics["total_input_tokens"] == 768
    assert metrics["output_tokens_mismatched_requests"] == 0 and metrics["input_tokens_mismatched_requests"] == 0
    assert metrics["mode"] == "concurrent" and metrics["token_count_source"] == ["server usage"]
    assert metrics["aggregate_output_tokens_per_second"] > metrics["per_request_output_tokens_per_second"]["p50"]
    assert metrics["ttft_ms"]["p50"] > 0 and metrics["tpot_ms"]["p50"] > 0 and metrics["itl_ms"]["p95"] > 0
    assert after["generation_tokens"] - before["generation_tokens"] == 240
    single = llm.summarize(*generator.run(prompts[:2], 1), 1, 64, 20)
    assert single["mode"] == "single-request" and single["prefill_tokens_per_second_estimate"] > 0


def test_stopping_aborts_requests_and_measures_nothing(fake_server):
    FakeVllm.delay = 0.05
    try:
        generator = llm.LoadGenerator(fake_server, output_tokens=200)
        threading.Timer(0.3, EMITTER.stop_event.set).start()
        started = time.monotonic()
        records, begin, end = generator.run([[1] * 8] * 6, concurrency=2)
        assert time.monotonic() - started < 5
        assert llm.summarize(records, begin, end, 2, 8, 200)["completed_requests"] == 0
    finally:
        FakeVllm.delay = 0.002


def test_percentiles_match_numpy_linear_method():
    stats = llm.percentiles([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], (50, 95, 99))
    assert stats["p50"] == pytest.approx(5.5) and stats["p95"] == pytest.approx(9.55)
    assert stats["p99"] == pytest.approx(9.91) and stats["mean"] == 5.5


def test_model_discovery_and_validation(tmp_path):
    model = tmp_path / "org" / "tiny-llama"
    model.mkdir(parents=True)
    (model / "config.json").write_text(json.dumps({"architectures": ["LlamaForCausalLM"], "num_attention_heads": 6,
                                                   "max_position_embeddings": 2048, "vocab_size": 32000}))
    (model / "model.safetensors").write_bytes(b"0" * 100)
    (tmp_path / "not-a-model").mkdir()
    found = llm.discover_models(tmp_path)
    assert [m["relative"] for m in found] == ["org/tiny-llama"] and found[0]["weight_bytes"] == 100
    with pytest.raises(BenchmarkError, match="does not exist"):
        llm.benchmark(str(tmp_path / "missing"))
    with pytest.raises(BenchmarkError, match="config.json"):
        llm.benchmark(str(tmp_path / "not-a-model"))


# ------------------------------------------------ runtime: reports/runs

def _run(gpu: str, count: int, tok: float, ttft: float, model: str = "llama", tflops: float = 500.0) -> dict:
    gpus = [{"index": i, "name": gpu, "memory_total_bytes": 141 * 2**30} for i in range(count)]
    return {"run_id": f"{gpu}-{count}", "created_at": "2026-10-15T12:00:00+00:00",
            "system_info": {"gpu_count": count, "gpus": gpus, "hostname": "h", "driver_version": "580.82",
                            "packages": {"torch": "2.11.0", "vllm": "0.26.0"}},
            "results": [
                {"test": "llm", "status": "PASSED",
                 "parameters": {"model_name": model, "model": f"/models/{model}", "precision": "bfloat16",
                                "input_tokens": 1024, "output_tokens": 256, "concurrency": 8, "gpu_count": count,
                                "tensor_parallel_size": count, "requests": 64},
                 "results": {"aggregate_output_tokens_per_second": tok, "ttft_ms": {"p50": ttft, "p95": ttft * 2},
                             "per_request_output_tokens_per_second": {"p50": tok / 8}, "tpot_ms": {"p50": 8.0}},
                 "gpu_sample": {"utilization_mean_percent": 90, "power_mean_watts_total": 700}},
                {"test": "compute", "status": "PASSED", "parameters": {"precision": "bf16", "matrix_size": 8192},
                 "results": {"precision": "bf16", "measured_tflops_total": tflops * count,
                             "measured_tflops_per_gpu_mean": tflops, "per_gpu": []}},
                {"test": "nccl", "status": "PASSED", "parameters": {"collective": "all_reduce"},
                 "results": {"collective": "all_reduce", "rows": [{"bytes": 1 << 20, "busbw_gb_per_second": 300.0,
                                                                   "algbw_gb_per_second": 160.0}],
                             "peak_busbw_gb_per_second": 300.0, "avg_busbw_gb_per_second": 280.0}},
            ]}


def test_compare_runs_reports_differences_and_percentages():
    comparison = reports.compare_runs([_run("NVIDIA H200", 2, 2000, 100),
                                       _run("NVIDIA RTX PRO 6000 Blackwell", 4, 2500, 80, model="qwen")])
    rows = {row["key"]: row for row in comparison["rows"]}
    assert rows["llm.c8.aggregate"]["diff_percent"][1] == pytest.approx(25.0)
    assert rows["llm.c8.ttft_p50"]["better"] == "lower" and rows["llm.c8.ttft_p50"]["diff_percent"][1] == -20
    assert rows["llm.c8.aggregate"]["note"] == "parameters differ"
    text = " ".join(comparison["warnings"])
    assert "GPU count differs" in text and "GPU models differ" in text and "LLM models differ" in text
    with pytest.raises(ValueError):
        reports.compare_runs([_run("A", 1, 1, 1)])


def test_reports_are_offline_and_contain_measured_sections(tmp_path):
    paths = reports.write_run(_run("NVIDIA H200", 2, 2000, 100), tmp_path)
    html = Path(paths["html"]).read_text()
    assert "<script" not in html and "http://" not in html and "https://" not in html
    for section in ("LLM inference", "GPU compute", "Multi-GPU communication", "<svg"):
        assert section in html
    csv_text = Path(paths["csv"]).read_text()
    assert "aggregate_output_tokens_per_second" in csv_text and "busbw@1048576" in csv_text
    comparison = reports.write_comparison(reports.compare_runs([_run("NVIDIA H200", 2, 2000, 100),
                                                                _run("NVIDIA H200", 2, 2100, 90)]), tmp_path / "cmp")
    compare_html = Path(comparison["html"]).read_text()
    assert "<script" not in compare_html and "+5.0%" in compare_html


def test_run_listing_resolution_and_cli_compare(tmp_path, capsys):
    for name, data in (("2026-10-15_120000_h200x2", _run("NVIDIA H200", 2, 2000, 100)),
                       ("2026-10-15_130000_rtx", _run("NVIDIA RTX PRO 6000 Blackwell", 4, 2500, 80))):
        (tmp_path / name).mkdir()
        data["created_at"] = name
        reports.write_run(data, tmp_path / name)
    listed = runs.list_runs(tmp_path)
    assert [row["run_id"] for row in listed] == ["2026-10-15_130000_rtx", "2026-10-15_120000_h200x2"]
    assert runs.resolve(tmp_path, "latest").name == "2026-10-15_130000_rtx"
    assert cli.main(["compare", "2026-10-15_120000_h200x2", "2026-10-15_130000_rtx", "--output", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "WARNING: GPU count differs" in out and "compare.html" in out
    assert cli.main(["report", "latest", "--output", str(tmp_path)]) == 0
    assert cli.main(["runs", "--output", str(tmp_path)]) == 0
    slug = runs.gpu_slug({"gpus": [{"name": "NVIDIA RTX PRO 6000 Blackwell Server Edition"}] * 4})
    assert slug.startswith("rtx-pro-6000") and slug.endswith("x4")


# ------------------------------------------------------------- TUI

def test_tui_opens_every_page_without_a_gpu(tmp_path):
    pytest.importorskip("textual")
    import asyncio

    from layersmith.gpu_benchmark.runtime.tui.app import GpuBenchApp

    (tmp_path / "models").mkdir()
    (tmp_path / "results").mkdir()
    reports.write_run(_run("NVIDIA H200", 2, 2000, 100), tmp_path / "results" / "2026-10-15_120000_h200x2")

    async def drive():
        app = GpuBenchApp(results=str(tmp_path / "results"), models=str(tmp_path / "models"))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(1.5)
            for key in "123456789l0":
                await pilot.press(key)
                await pilot.pause(0.1)
            await pilot.press("8")
            await pilot.pause(0.3)
            assert app.query_one("#content").current == "results"
            assert app.query_one("#results-runs").row_count == 1
        return app

    app = asyncio.run(drive())
    assert app.health["status"] == "FAIL"
