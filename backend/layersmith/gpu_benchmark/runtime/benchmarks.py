"""Measured CUDA compute, memory-transfer and NCCL performance tests."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from .system import run

BIN_DIR = Path("/opt/gpu-bench/bin")


class BenchmarkError(RuntimeError):
    pass


def _torch_cuda():
    try:
        import torch
    except Exception as exc:
        raise BenchmarkError(f"PyTorch is not installed: {exc}") from exc
    if not torch.cuda.is_available():
        raise BenchmarkError("CUDA is not available. Pass the host GPU devices to the container.")
    return torch


def compute(precision: str = "bf16", duration: int = 60, size: int = 4096) -> dict:
    torch = _torch_cuda()
    dtypes = {"fp32": torch.float32, "fp16": torch.float16, "bf16": torch.bfloat16}
    if precision not in dtypes:
        raise BenchmarkError(f"Unsupported precision: {precision}")
    dtype = dtypes[precision]
    try:
        a = torch.randn((size, size), device="cuda", dtype=dtype)
        b = torch.randn((size, size), device="cuda", dtype=dtype)
        for _ in range(5):
            torch.mm(a, b)
        torch.cuda.synchronize()
        start = torch.cuda.Event(enable_timing=True)
        end = torch.cuda.Event(enable_timing=True)
        elapsed = 0.0
        iterations = 0
        while elapsed < duration:
            start.record()
            for _ in range(10):
                torch.mm(a, b)
            end.record()
            end.synchronize()
            elapsed += start.elapsed_time(end) / 1000.0
            iterations += 10
        seconds = elapsed
        operations = 2 * size**3 * iterations
        return {"precision": precision, "matrix_size": size, "warmup_iterations": 5,
                "iterations": iterations, "measured_gpu_seconds": seconds,
                "measured_tflops": operations / seconds / 1e12,
                "measurement_note": "Measured CUDA-event GEMM throughput; not theoretical peak performance."}
    except Exception as exc:
        raise BenchmarkError(f"CUDA {precision} matrix benchmark failed: {exc}") from exc


def _timed_bandwidth(torch, src, dst, operation, byte_count: int, rounds: int = 20) -> dict:
    start, end = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
    for _ in range(3):
        operation()
    torch.cuda.synchronize()
    start.record()
    for _ in range(rounds):
        operation()
    end.record()
    end.synchronize()
    seconds = start.elapsed_time(end) / 1000.0
    return {"bytes_per_transfer": byte_count, "rounds": rounds, "seconds": seconds,
            "gb_per_second": (byte_count * rounds / seconds) / 1e9}


def memory() -> dict:
    torch = _torch_cuda()
    free_bytes, total_bytes = torch.cuda.mem_get_info()
    byte_count = min(256 * 1024 * 1024, int(free_bytes * 0.20))
    if byte_count < 16 * 1024 * 1024:
        raise BenchmarkError("Less than 16 MiB of free GPU memory is available for transfer tests.")
    elems = byte_count // 4
    device = torch.empty(elems, dtype=torch.float32, device="cuda")
    device2 = torch.empty_like(device)
    host = torch.empty(elems, dtype=torch.float32, pin_memory=True)
    h2d = _timed_bandwidth(torch, host, device, lambda: device.copy_(host, non_blocking=True), byte_count)
    d2h = _timed_bandwidth(torch, device, host, lambda: host.copy_(device, non_blocking=True), byte_count)
    d2d = _timed_bandwidth(torch, device, device2, lambda: device2.copy_(device), byte_count)
    sample = Path("/opt/gpu-bench/bin/bandwidthTest")
    sample_metrics = {}
    sample_detail = {"status": "SKIPPED", "reason": "CUDA Samples bandwidthTest was not included in this image."}
    if sample.exists():
        sample_result = run([str(sample)], timeout=90)
        if sample_result.returncode:
            sample_detail = {"status": "SKIPPED", "reason": f"bandwidthTest exited {sample_result.returncode}: "
                            f"{(sample_result.stderr or sample_result.stdout)[-1000:]}"}
        else:
            for line in sample_result.stdout.splitlines():
                if re.search(r"Device to Device", line, re.I):
                    sample_metrics["device_to_device_sample"] = line.strip()
                elif re.search(r"Host to Device", line, re.I):
                    sample_metrics["host_to_device_sample"] = line.strip()
                elif re.search(r"Device to Host", line, re.I):
                    sample_metrics["device_to_host_sample"] = line.strip()
            sample_detail = ({"status": "PASSED", "classification": "legacy reference",
                             "metrics": sample_metrics} if sample_metrics else
                            {"status": "SKIPPED", "reason": "bandwidthTest produced no recognized bandwidth metrics."})
    return {"available_gpu_memory_bytes": int(free_bytes), "total_gpu_memory_bytes": int(total_bytes),
            "gpu_memory_allocation": {"status": "PASSED", "allocated_bytes": int(byte_count * 2)},
            "transfers": {"host_to_device_pcie": h2d, "device_to_host_pcie": d2h,
                          "device_to_device_gpu_memory": d2d},
            "cuda_samples_bandwidth_test_legacy_reference": sample_detail,
            "memory_latency": {"status": "SKIPPED",
                               "detail": "No portable pointer-chase kernel is bundled; launch latency is not reported as memory latency."},
            "units_note": "Transfer measurements are GB/s. Host/device values describe PCIe movement; device/device describes GPU memory copies, not guaranteed peak HBM bandwidth. CUDA Samples bandwidthTest is a legacy reference because NVIDIA marks it outdated and inaccurate."}


NCCL_TESTS = {
    "all_reduce": "all_reduce_perf",
    "all_gather": "all_gather_perf",
    "reduce_scatter": "reduce_scatter_perf",
    "broadcast": "broadcast_perf",
}


def _parse_nccl_metrics(output: str) -> list[dict]:
    rows = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 8 and fields[0].isdigit():
            try:
                rows.append({"bytes": int(fields[0]), "time_us": float(fields[-4]),
                             "algbw_gb_per_second": float(fields[-3]),
                             "busbw_gb_per_second": float(fields[-2])})
            except ValueError:
                continue
    return rows


def nccl(test: str = "all_reduce", min_bytes: str = "8", max_bytes: str = "128M",
         iterations: int = 20, gpus: int | None = None, hosts: str | None = None,
         hostfile: str | None = None, ranks: int | None = None) -> list[dict]:
    torch = _torch_cuda()
    visible = torch.cuda.device_count()
    gpu_count = gpus or visible
    tests = list(NCCL_TESTS) if test == "all" else [test]
    if any(name not in NCCL_TESTS for name in tests):
        raise BenchmarkError(f"Unknown NCCL test {test}; choose " + ", ".join([*NCCL_TESTS, "all"]))
    if gpu_count > visible:
        raise BenchmarkError(f"Requested {gpu_count} GPUs but only {visible} are visible to this container.")
    results = []
    for name in tests:
        binary = BIN_DIR / NCCL_TESTS[name]
        if not binary.is_file():
            raise BenchmarkError(f"NCCL test binary is missing: {binary}")
        command = [str(binary), "-b", min_bytes, "-e", max_bytes, "-f", "2", "-g", str(gpu_count),
                   "-n", str(iterations), "-w", "5"]
        launched_by = "direct"
        if os.environ.get("SLURM_PROCID") is not None:
            launched_by = "slurm"
        elif hosts or hostfile or ranks:
            mpirun = shutil.which("mpirun")
            if not mpirun:
                raise BenchmarkError("MPI launch requested but mpirun is not installed.")
            world = ranks or gpu_count
            launcher = [mpirun, "--allow-run-as-root", "-np", str(world)]
            if hosts:
                launcher += ["--host", hosts]
            if hostfile:
                launcher += ["--hostfile", hostfile]
            command = launcher + command
            launched_by = "openmpi"
        started = time.monotonic()
        result = run(command, timeout=1800)
        elapsed = time.monotonic() - started
        if result.returncode:
            raise BenchmarkError(f"NCCL {name} exited {result.returncode}: {(result.stderr or result.stdout)[-3000:]}")
        rows = _parse_nccl_metrics(result.stdout)
        if not rows:
            raise BenchmarkError(f"NCCL {name} completed without recognizable timing, algbw, or busbw rows.")
        results.append({"test": name, "status": "PASSED", "launcher": launched_by,
                        "command": shlex.join(command), "gpus_per_rank": gpu_count,
                        "runtime_seconds": elapsed, "metrics": rows,
                        "measurement_note": "NCCL algbw and busbw are collective-test metrics; neither is a raw physical link bandwidth."})
    return results
