"""NVIDIA GPU image recipe, CLI, reporting, and LayerSmith integration."""

import json
from pathlib import Path
import subprocess

import pytest

from layersmith.core import containerfile as cf
from layersmith.core.spec import InvalidSpec, validate_spec
from layersmith.gpu_benchmark import catalog, render
from layersmith.gpu_benchmark.runtime import cli, llm, reports
from layersmith.gpu_benchmark.runtime.system import health_checks
from layersmith.gpu_benchmark.runtime.benchmarks import BenchmarkError, _parse_nccl_metrics
from tests.test_builds import env, make_build  # noqa: F401
from layersmith.api import (
    GpuBenchmarkResolveIn, ProjectIn, _gpu_benchmark_detail, gpu_benchmark_catalog_view,
    gpu_benchmark_resolve, preview,
)
from layersmith.models import Build, Project
from layersmith.services.builds import manifest_for


def test_recipe_pins_supported_toolchain_and_honors_tool_selection():
    recipe = catalog.resolve()
    assert recipe["base"] == "nvidia/cuda:13.0.2-devel-ubuntu22.04"
    assert recipe["python_full_version"] == "3.12.15"
    assert recipe["torch"] == "2.11.0" and recipe["vllm"] == "0.26.0"
    assert recipe["transformers"] and recipe["tokenizers"] and recipe["nccl_tests"]
    assert recipe["cuda_samples"] == "v12.8"

    cuda12 = catalog.resolve({"cuda_version": "12.8.2", "tools": ["compute", "memory"]})
    assert cuda12["vllm"] is None and cuda12["tools"] == ["compute", "memory"]
    with pytest.raises(catalog.RecipeError, match="no matching pinned vLLM"):
        catalog.resolve({"cuda_version": "12.8.2"})


@pytest.mark.parametrize("request_", [
    {"cuda_version": "latest"},
    {"python_version": "3.14"},
    {"tools": ["unknown"]},
    {"tools": []},
    {"tools": [[]]},
    {"mirrors": "not-an-object"},
    {"mirrors": {"pypi_index": "https://mirror.example/\necho unsafe"}},
    {"mirrors": {"private_token": "https://mirror.example"}},
])
def test_invalid_version_components_and_mirrors_are_rejected(request_):
    with pytest.raises(catalog.RecipeError):
        catalog.resolve(request_)


def test_spec_pins_cuda_base_defaults_amd64_and_preserves_mirrors():
    spec = validate_spec({"gpu_benchmark": {"mirrors": {"pypi_index": "https://mirror.example/simple"}}})
    assert spec["base"]["source"] == "nvidia/cuda:13.0.2-devel-ubuntu22.04"
    assert spec["architecture"] == "amd64"
    assert spec["gpu_benchmark"]["mirrors"]["pypi_index"] == "https://mirror.example/simple"
    with pytest.raises(InvalidSpec, match="amd64"):
        validate_spec({"architecture": "arm64", "gpu_benchmark": {}})
    with pytest.raises(InvalidSpec, match="either"):
        validate_spec({"training": {"profile": "hf-finetune"}, "gpu_benchmark": {}})


def test_generated_containerfile_has_selected_components_and_no_host_driver_install():
    spec = validate_spec({"gpu_benchmark": {"tools": ["compute", "memory"]}})
    text, packages, warnings = cf.generate({"name": "gpu-benchmark", "description": "GPU tests"}, spec)
    assert not warnings
    assert "FROM nvidia/cuda:13.0.2-devel-ubuntu22.04" in text
    assert "GPU_BENCH_APT_MAIN" in text and "GPU_BENCH_PYPI_INDEX" in text
    assert "torch==2.11.0" in text and "vllm==" not in text
    assert "bandwidthTest" in text and "nccl-tests.git" not in text
    assert "CMAKE_CUDA_ARCHITECTURES=90;100;120" in text
    assert "apt-get install nvidia-driver" not in text
    assert "GPU_BENCH_TOOLS=compute,memory" in text
    assert "ca-certificates" in packages and "cmake" in packages
    assert "CMD [\"gpu-bench\", \"--help\"]" in text
    nccl_spec = validate_spec({"gpu_benchmark": {"tools": ["nccl"]}})
    nccl_text, _, _ = cf.generate({"name": "gpu-benchmark", "description": "GPU tests"}, nccl_spec)
    assert "NVCC_GENCODE" in nccl_text and "compute_120,code=sm_120" in nccl_text


def test_generated_run_instructions_have_valid_shell_syntax():
    spec = validate_spec({"gpu_benchmark": {}})
    text, _, _ = cf.generate({"name": "gpu-benchmark", "description": "GPU tests"}, spec)
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


def test_gpu_context_contains_cli_and_recipe_and_airgap_bundle(env):
    build_id = make_build(env, {"gpu_benchmark": {"tools": ["compute", "memory"]}})
    env["service"].run(build_id)
    with env["factory"]() as session:
        build = session.get(Build, build_id)
        project = session.get(Project, build.project_id)
        assert build.status == "ready"
        detail = _gpu_benchmark_detail(build)
        assert detail["recipe"]["cuda_version"] == "13.0.2"
        assert "podman run" in detail["getting_started"]["podman"]
        assert "GPU_BENCH_TOOLS=compute,memory" in build.containerfile
        assert env["backend"].context.joinpath("gpu-benchmark/bin/gpu-bench").is_file()
        assert manifest_for(project, build)["gpu_benchmark"]["tools"] == ["compute", "memory"]

    bundle_path = env["service"].airgap_bundle(build_id)
    import tarfile

    with tarfile.open(bundle_path) as archive:
        names = archive.getnames()
        assert any(name.endswith("/gpu-benchmark/README.md") for name in names)
        assert any(name.endswith("/gpu-benchmark/recipe.json") for name in names)


def test_gpu_benchmark_catalog_and_preview_are_available_in_layer_smith():
    assert gpu_benchmark_catalog_view()["category"]["name"] == "AI / ML / GPU"
    resolved = gpu_benchmark_resolve(GpuBenchmarkResolveIn(gpu_benchmark={"tools": ["nccl"]}))
    assert resolved["ok"] and resolved["recipe"]["tools"] == ["nccl"]
    container = preview(ProjectIn(
        name="gpu-benchmark", template="NVIDIA GPU Benchmark Toolkit",
        spec={"gpu_benchmark": {"tools": ["nccl"]}},
    ))["containerfile"]
    assert "FROM nvidia/cuda:13.0.2-devel-ubuntu22.04" in container
    assert "NCCL_TESTS_REPOSITORY" in container


def test_vllm_output_parser_keeps_requested_percentiles_and_throughput():
    text = """Output token throughput (tok/s): 720.50
Total Token throughput (tok/s): 1800.20
Request throughput (req/s): 2.10
Mean TTFT (ms): 15.20
P50 TTFT (ms): 14.10
P95 TTFT (ms): 24.30
P99 TTFT (ms): 40.70
Mean TPOT (ms): 3.20
P50 TPOT (ms): 3.00
Mean E2EL (ms): 850.00
P95 E2EL (ms): 970.00
"""
    result = llm._parse_bench_output(text, 8)
    assert result["output_tokens_per_second"] == 720.5
    assert result["total_tokens_per_second"] == 1800.2
    assert result["tokens_per_second_per_active_request"] == 90.0625
    assert result["ttft"]["p50"] == 14.1 and result["ttft"]["p95"] == 24.3
    assert result["ttft"]["p99"] == 40.7 and result["tpot"]["mean"] == 3.2
    assert result["e2el"]["p95"] == 970


def test_nccl_output_parser_keeps_algbw_and_busbw_separate():
    rows = _parse_nccl_metrics("# size count type redop root time algbw busbw wrong\n"
                               "8 2 float sum -1 3.19 0.01 0.02 0")
    assert rows == [{"bytes": 8, "time_us": 3.19, "algbw_gb_per_second": 0.01,
                     "busbw_gb_per_second": 0.02}]
    assert _parse_nccl_metrics("NCCL ran without measurement rows") == []


def test_missing_local_model_fails_before_any_network_or_gpu_access(tmp_path):
    with pytest.raises(BenchmarkError, match="Model directory does not exist"):
        llm.benchmark(str(tmp_path / "missing-model"))


def test_health_reports_host_driver_cuda_compatibility():
    info = {"gpu_count": 1, "gpus": [{"memory_free_bytes": 1024}], "torch_cuda_available": True,
            "torch_version": "2.11.0", "torch_cuda_version": "13.0", "driver_version": "580.65.06",
            "rdma_devices": [], "network_interfaces": []}
    checks = {row["name"]: row for row in health_checks(info)["checks"]}
    assert checks["CUDA driver compatibility"]["status"] == "PASS"
    info["driver_version"] = "570.26.00"
    checks = {row["name"]: row for row in health_checks(info)["checks"]}
    assert checks["CUDA driver compatibility"]["status"] == "FAIL"


def test_all_command_help_parsers_exit_successfully(capsys):
    for command in (None, "info", "health", "llm", "compute", "memory", "nccl", "full", "report"):
        argv = ["--help"] if command is None else [command, "--help"]
        with pytest.raises(SystemExit) as raised:
            cli.main(argv)
        assert raised.value.code == 0
        assert "usage:" in capsys.readouterr().out.lower()


def test_full_without_gpu_writes_skipped_results_and_all_report_formats(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "collect_system_info", lambda: {
        "timestamp": "2026-10-12T12:00:00+00:00", "hostname": "test-host", "gpu_count": 0, "gpus": [],
    })
    monkeypatch.setattr(cli, "health_checks", lambda info: {"status": "FAIL", "checks": []})
    assert cli.main(["full", "--output", str(tmp_path)]) == 0
    data = json.loads((tmp_path / "benchmark.json").read_text())
    skipped = [row for row in data["results"] if row["test"] in {"llm", "compute", "memory", "nccl"}]
    assert len(skipped) == 4 and all(row["status"] == "SKIPPED" and row["error"] for row in skipped)
    for filename in ("benchmark.json", "benchmark.csv", "benchmark.html", "system-info.json"):
        assert (tmp_path / filename).is_file()


def test_slurm_task_rank_zero_only_writes_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("SLURM_PROCID", "1")
    assert cli._write(str(tmp_path), {}, [], {}) == {}
    assert not (tmp_path / "benchmark.json").exists()
    assert cli.main(["full", "--output", str(tmp_path)]) == 2


def test_report_comparison_aligns_llm_concurrency_and_explains_differences(tmp_path):
    first = {"system_info": {"gpu_count": 1, "gpus": [{"name": "NVIDIA H200"}]},
             "configuration": {"model": "/models/a", "precision": "bf16", "gpu_count": 1},
             "results": [{"test": "llm", "parameters": {"concurrency": 1, "requests": 100},
                          "results": {"output_tokens_per_second": 100}}]}
    second = {"system_info": {"gpu_count": 2, "gpus": [{"name": "RTX PRO 6000"}]},
              "configuration": {"model": "/models/b", "precision": "fp16", "gpu_count": 2},
              "results": [{"test": "llm", "parameters": {"concurrency": 1, "requests": 200},
                           "results": {"output_tokens_per_second": 125}}]}
    previous = tmp_path / "previous.json"
    previous.write_text(json.dumps(second))
    paths = reports.write_report(first, tmp_path / "comparison", previous)
    saved = json.loads(Path(paths["json"]).read_text())
    assert saved["comparison"]["metrics"][0]["change_percent"] == 25
    assert {row["parameter"] for row in saved["comparison"]["differences"]} >= {
        "model", "precision", "visible GPU count", "GPU model",
    }
    assert any("llm test parameters" in row["parameter"] for row in saved["comparison"]["differences"])
    assert "Comparison" in Path(paths["html"]).read_text()
