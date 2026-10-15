"""Measured CUDA compute, GPU memory/transfer and NCCL communication tests.

Every number here is measured on the GPUs in front of it: GPU time comes from
CUDA events after explicit synchronisation, never from wall-clock guesses, and
nothing is compared with or replaced by datasheet figures.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time

from .events import EMITTER, StopRequested
from .system import GPU_BENCH_HOME, GpuMonitor

BIN_DIR = GPU_BENCH_HOME / "bin"
PRECISIONS = ("fp32", "tf32", "fp16", "bf16", "fp8")
DEFAULT_PRECISIONS = ("fp32", "tf32", "fp16", "bf16")


class BenchmarkError(RuntimeError):
    pass


class Unavailable(BenchmarkError):
    """The GPU, driver or image cannot run this test; not a benchmark failure."""


def _torch_cuda():
    try:
        import torch
    except Exception as exc:
        raise BenchmarkError(f"PyTorch is not installed: {exc}") from exc
    if not torch.cuda.is_available():
        raise Unavailable("CUDA is not available. Pass the host GPUs to the container "
                          "(podman run --device nvidia.com/gpu=all).")
    return torch


def parse_gpu_list(value, visible: int) -> list[int]:
    """'all', None, '0,2' or [0, 2] -> sorted CUDA indices, validated."""
    if value in (None, "", "all"):
        return list(range(visible))
    if isinstance(value, int):
        value = [value]
    if isinstance(value, str):
        try:
            value = [int(part) for part in value.split(",") if part.strip()]
        except ValueError as exc:
            raise BenchmarkError(f"GPU list must be 'all' or comma-separated indices, not {value!r}") from exc
    indices = sorted(set(int(v) for v in value))
    bad = [i for i in indices if i < 0 or i >= visible]
    if bad or not indices:
        raise BenchmarkError(f"GPU index {bad or value} is not visible; {visible} GPU(s) available (0-{visible - 1}).")
    return indices


def visible_devices_env(indices: list[int]) -> str:
    """CUDA_VISIBLE_DEVICES for a child process, relative to what this process already sees."""
    current = [part.strip() for part in os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",") if part.strip()]
    if current:
        return ",".join(current[i] for i in indices if i < len(current))
    return ",".join(str(i) for i in indices)


# ------------------------------------------------------------- compute

def _gemm_worker(torch, device: int, precision: str, size: int, duration: float, out: dict, barrier) -> None:
    try:
        torch.cuda.set_device(device)
        stream = torch.cuda.Stream(device=device)
        with torch.cuda.stream(stream):
            if precision == "fp8":
                if not hasattr(torch, "float8_e4m3fn") or not hasattr(torch, "_scaled_mm"):
                    raise Unavailable("This PyTorch build has no FP8 matmul.")
                major, minor = torch.cuda.get_device_capability(device)
                if (major, minor) < (8, 9):
                    raise Unavailable(f"FP8 tensor cores need compute capability 8.9 or newer (this GPU is {major}.{minor}).")
                a = torch.randn((size, size), device=device, dtype=torch.float16).to(torch.float8_e4m3fn)
                b = torch.randn((size, size), device=device, dtype=torch.float16).to(torch.float8_e4m3fn).t()
                scale = torch.ones((), device=device, dtype=torch.float32)

                def op():
                    return torch._scaled_mm(a, b, scale_a=scale, scale_b=scale, out_dtype=torch.bfloat16)
            else:
                dtype = {"fp32": torch.float32, "tf32": torch.float32, "fp16": torch.float16,
                         "bf16": torch.bfloat16}[precision]
                a = torch.randn((size, size), device=device, dtype=dtype)
                b = torch.randn((size, size), device=device, dtype=dtype)

                def op():
                    return torch.mm(a, b)
            for _ in range(5):
                op()
            stream.synchronize()
            barrier.wait(timeout=600)
            start, end = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
            elapsed, iterations, batch = 0.0, 0, 10
            while elapsed < duration:
                if EMITTER.stopping:
                    raise StopRequested("Stopped by user")
                start.record(stream)
                for _ in range(batch):
                    op()
                end.record(stream)
                end.synchronize()
                elapsed += start.elapsed_time(end) / 1000.0
                iterations += batch
                out["progress"] = min(1.0, elapsed / duration)
            flops = 2 * size ** 3 * iterations
            out.update({"status": "PASSED", "gpu": device, "gpu_name": torch.cuda.get_device_name(device),
                        "iterations": iterations, "measured_gpu_seconds": elapsed,
                        "measured_tflops": flops / elapsed / 1e12})
    except Unavailable as exc:
        out.update({"status": "UNAVAILABLE", "gpu": device, "error": str(exc)})
    except StopRequested:
        out.update({"status": "STOPPED", "gpu": device, "error": "Stopped by user"})
    except Exception as exc:
        out.update({"status": "FAILED", "gpu": device, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        try:
            barrier.abort() if out.get("status") not in (None, "PASSED") and not barrier.broken else None
        except Exception:
            pass


def compute(precisions=DEFAULT_PRECISIONS, duration: float = 10, size: int = 8192, gpus=None) -> list[dict]:
    """GEMM throughput per precision; all selected GPUs run concurrently.

    fp32 runs with TF32 disabled; tf32 is the same FP32 GEMM with TF32 tensor
    cores allowed. Result: one entry per precision with per-GPU TFLOPS.
    """
    torch = _torch_cuda()
    if isinstance(precisions, str):
        precisions = list(PRECISIONS) if precisions == "all" else [p.strip() for p in precisions.split(",") if p.strip()]
    unknown = [p for p in precisions if p not in PRECISIONS]
    if unknown:
        raise BenchmarkError(f"Unknown precision {', '.join(unknown)}; choose from {', '.join(PRECISIONS)}")
    devices = parse_gpu_list(gpus, torch.cuda.device_count())
    entries = []
    for precision in precisions:
        EMITTER.check_stop()
        torch.backends.cuda.matmul.allow_tf32 = precision == "tf32"
        torch.backends.cudnn.allow_tf32 = precision == "tf32"
        EMITTER.log(f"Compute {precision.upper()}: {size}x{size} GEMM on GPU(s) {devices} for {duration:g}s")
        outputs = [{} for _ in devices]
        barrier = threading.Barrier(len(devices))
        threads = [threading.Thread(target=_gemm_worker, args=(torch, d, precision, size, duration, o, barrier))
                   for d, o in zip(devices, outputs)]
        started = time.time()
        with GpuMonitor(interval=0.5, indices=devices) as monitor:
            for thread in threads:
                thread.start()
            while any(thread.is_alive() for thread in threads):
                done = [o.get("progress", 0) for o in outputs]
                EMITTER.progress("compute", precision=precision, fraction=sum(done) / len(done),
                                 gpus=monitor.latest, summary=f"{precision} {100 * sum(done) / len(done):.0f}%")
                time.sleep(0.5)
            for thread in threads:
                thread.join()
        statuses = {o.get("status") for o in outputs}
        passed = [o for o in outputs if o.get("status") == "PASSED"]
        status = ("PASSED" if statuses == {"PASSED"} else "STOPPED" if "STOPPED" in statuses
                  else "UNAVAILABLE" if statuses == {"UNAVAILABLE"} else "FAILED")
        total = sum(o["measured_tflops"] for o in passed) if passed else None
        entry = {"test": "compute", "name": f"GEMM {precision.upper()}", "status": status,
                 "parameters": {"precision": precision, "matrix_size": size, "duration_seconds": duration,
                                "gpus": devices, "gpu_count": len(devices),
                                "tf32_allowed": precision == "tf32"},
                 "results": {"precision": precision, "matrix_size": size,
                             "measured_tflops_total": total,
                             "measured_tflops_per_gpu_mean": total / len(passed) if passed else None,
                             "per_gpu": outputs,
                             "wall_seconds": time.time() - started,
                             "measurement_note": "Measured with CUDA events after warmup; dense GEMM, not a "
                                                 "theoretical or sparsity peak. All selected GPUs ran concurrently."},
                 "gpu_sample": monitor.summary(),
                 "error": "; ".join(sorted({o['error'] for o in outputs if o.get('error')})) or None}
        for o in outputs:
            o.pop("progress", None)
        EMITTER.result(entry)
        entries.append(entry)
        if status == "STOPPED":
            raise StopRequested("Stopped by user")
    return entries


# -------------------------------------------------------------- memory

def _event_time(torch, operation, rounds: int, device: int) -> float:
    start, end = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
    for _ in range(3):
        operation()
    torch.cuda.synchronize(device)
    start.record()
    for _ in range(rounds):
        operation()
    end.record()
    end.synchronize()
    return start.elapsed_time(end) / 1000.0


def _bandwidth(byte_count: int, rounds: int, seconds: float, factor: int = 1) -> dict:
    return {"bytes_per_transfer": byte_count, "rounds": rounds, "seconds": seconds,
            "gb_per_second": byte_count * rounds * factor / seconds / 1e9,
            "gib_per_second": byte_count * rounds * factor / seconds / 2**30}


def _gpu_memory(torch, device: int, size_mb: int | None) -> dict:
    torch.cuda.set_device(device)
    free, total = torch.cuda.mem_get_info(device)
    byte_count = (size_mb * 2**20) if size_mb else min(2 * 2**30, int(free * 0.2))
    byte_count -= byte_count % 4096
    if byte_count < 64 * 2**20:
        raise BenchmarkError(f"GPU {device}: less than 64 MiB free for memory tests.")
    elements = byte_count // 4
    src = torch.empty(elements, dtype=torch.float32, device=device).fill_(1.0)
    dst = torch.empty_like(src)
    rounds = 20
    result = {"gpu": device, "gpu_name": torch.cuda.get_device_name(device), "test_bytes": byte_count,
              "free_bytes_before": int(free), "total_bytes": int(total)}
    # On-GPU copy reads and writes every byte, so it moves 2x the buffer.
    result["vram_copy"] = {**_bandwidth(byte_count, rounds, _event_time(torch, lambda: dst.copy_(src), rounds, device), 2),
                           "kind": "VRAM/HBM", "counts": "read + write"}
    result["vram_read"] = {**_bandwidth(byte_count, rounds, _event_time(torch, lambda: src.sum(), rounds, device)),
                           "kind": "VRAM/HBM", "counts": "read only (reduction kernel)"}
    host_bytes = min(byte_count, 1 << 30)
    host_elements = host_bytes // 4
    pinned = torch.empty(host_elements, dtype=torch.float32, pin_memory=True)
    pageable = torch.empty(host_elements, dtype=torch.float32)
    device_part = src[:host_elements]
    for name, host in (("pinned", pinned), ("pageable", pageable)):
        result[f"host_to_device_{name}"] = {
            **_bandwidth(host_bytes, 10, _event_time(torch, lambda h=host: device_part.copy_(h, non_blocking=True), 10, device)),
            "kind": "PCIe/C2C host link", "host_memory": name}
        result[f"device_to_host_{name}"] = {
            **_bandwidth(host_bytes, 10, _event_time(torch, lambda h=host: h.copy_(device_part, non_blocking=True), 10, device)),
            "kind": "PCIe/C2C host link", "host_memory": name}
    del src, dst, pinned, pageable, device_part
    torch.cuda.empty_cache()
    return result


def _peer_matrix(torch, devices: list[int], size_mb: int) -> dict:
    byte_count = size_mb * 2**20
    elements = byte_count // 4
    buffers = {d: torch.empty(elements, dtype=torch.float32, device=d).fill_(1.0) for d in devices}
    targets = {d: torch.empty(elements, dtype=torch.float32, device=d) for d in devices}
    matrix, access = [], []
    for i in devices:
        row, access_row = [], []
        for j in devices:
            if i == j:
                row.append(None)
                access_row.append("X")
                continue
            EMITTER.check_stop()
            access_row.append("yes" if torch.cuda.can_device_access_peer(i, j) else "no")
            torch.cuda.set_device(i)
            seconds = _event_time(torch, lambda a=buffers[i], b=targets[j]: b.copy_(a, non_blocking=True), 10, i)
            torch.cuda.synchronize(j)
            row.append(byte_count * 10 / seconds / 1e9)
        matrix.append(row)
        access.append(access_row)
    del buffers, targets
    torch.cuda.empty_cache()
    return {"gpus": devices, "test_bytes": byte_count, "gb_per_second": matrix, "peer_access": access,
            "kind": "GPU-to-GPU (NVLink or PCIe depending on topology)", "direction": "row -> column"}


def _nvbandwidth(devices: list[int]) -> dict:
    binary = BIN_DIR / "nvbandwidth"
    if not binary.is_file():
        return {"status": "UNAVAILABLE", "reason": "nvbandwidth is not part of this image."}
    prefixes = ["host_to_device_memcpy_ce", "device_to_host_memcpy_ce", "device_to_device_memcpy_read_ce",
                "device_to_device_bidirectional_memcpy_read_ce", "device_local_copy"]
    env = dict(os.environ, CUDA_VISIBLE_DEVICES=",".join(map(str, devices)))
    command = [str(binary), "--format", "json", "-p", *prefixes]
    EMITTER.log("$ " + shlex.join(command), source="nvbandwidth")
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=900, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "FAILED", "reason": str(exc)}
    try:
        data = json.loads(done.stdout[done.stdout.find("{"):])
    except ValueError:
        return {"status": "FAILED", "reason": f"exit {done.returncode}: {(done.stderr or done.stdout)[-800:]}"}
    root = data.get("nvbandwidth", data)
    cases = []
    for case in root.get("testcases") or []:
        cases.append({"name": case.get("name"), "status": case.get("status"),
                      "description": case.get("bandwidth_description"),
                      "sum_gb_per_second": case.get("sum"), "matrix": case.get("bandwidth_matrix"),
                      "waiver_reason": case.get("waiver_reason")})
    return {"status": "PASSED" if cases else "FAILED", "version": root.get("version"),
            "error": root.get("error"), "testcases": cases,
            "note": "nvbandwidth (NVIDIA) copy-engine and SM copy tests; GB/s as reported by the tool."}


def memory(gpus=None, size_mb: int | None = None, peer: bool = True) -> list[dict]:
    """VRAM/HBM bandwidth, host<->device (PCIe) and GPU<->GPU transfers, kept separate."""
    torch = _torch_cuda()
    devices = parse_gpu_list(gpus, torch.cuda.device_count())
    entries = []
    with GpuMonitor(interval=0.5, indices=devices) as monitor:
        per_gpu = []
        for position, device in enumerate(devices):
            EMITTER.check_stop()
            EMITTER.progress("memory", fraction=position / (len(devices) + 1), gpus=monitor.latest,
                             summary=f"GPU {device}", force=True)
            EMITTER.log(f"Memory bandwidth on GPU {device}")
            per_gpu.append(_gpu_memory(torch, device, size_mb))
        peers = None
        if peer and len(devices) > 1:
            EMITTER.progress("memory", fraction=len(devices) / (len(devices) + 1), summary="GPU-to-GPU", force=True)
            EMITTER.log("GPU-to-GPU copy matrix")
            peers = _peer_matrix(torch, devices, size_mb or 512)
        EMITTER.check_stop()
        EMITTER.log("nvbandwidth")
        nvbw = _nvbandwidth(devices)
    vram = [g["vram_copy"]["gb_per_second"] for g in per_gpu]
    h2d = [g["host_to_device_pinned"]["gb_per_second"] for g in per_gpu]
    d2h = [g["device_to_host_pinned"]["gb_per_second"] for g in per_gpu]
    entry = {"test": "memory", "name": "GPU memory and transfers", "status": "PASSED",
             "parameters": {"gpus": devices, "gpu_count": len(devices), "size_mb": size_mb or "auto"},
             "results": {
                 "vram_copy_gb_per_second_mean": sum(vram) / len(vram),
                 "host_to_device_pinned_gb_per_second_mean": sum(h2d) / len(h2d),
                 "device_to_host_pinned_gb_per_second_mean": sum(d2h) / len(d2h),
                 "per_gpu": per_gpu, "peer_to_peer": peers, "nvbandwidth": nvbw,
                 "units_note": "GB/s = 10^9 bytes/s (GiB/s also given per test). VRAM/HBM, host<->device "
                               "(PCIe or NVLink-C2C) and GPU<->GPU figures measure different links and are "
                               "never combined. VRAM copy counts read+write bytes."},
             "gpu_sample": monitor.summary(), "error": None}
    EMITTER.result(entry)
    entries.append(entry)
    return entries


# ---------------------------------------------------------------- NCCL

NCCL_TESTS = {
    "all_reduce": "all_reduce_perf",
    "all_gather": "all_gather_perf",
    "reduce_scatter": "reduce_scatter_perf",
    "broadcast": "broadcast_perf",
    "sendrecv": "sendrecv_perf",
    "alltoall": "alltoall_perf",
}
DEFAULT_NCCL_TESTS = ("all_reduce", "all_gather", "reduce_scatter", "broadcast", "sendrecv")


def _parse_nccl_metrics(output: str) -> list[dict]:
    """In-place columns of each nccl-tests size row (out-of-place kept too)."""
    rows = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 8 or not fields[0].isdigit():
            continue
        numbers = []
        for value in fields[1:]:
            try:
                numbers.append(float(value))
            except ValueError:
                numbers.append(None)
        try:
            row = {"bytes": int(fields[0]), "time_us": float(fields[-4]),
                   "algbw_gb_per_second": float(fields[-3]), "busbw_gb_per_second": float(fields[-2])}
        except ValueError:
            continue
        if len(fields) >= 12:
            try:
                row["out_of_place_algbw_gb_per_second"] = float(fields[-7])
                row["out_of_place_busbw_gb_per_second"] = float(fields[-6])
            except ValueError:
                pass
        rows.append(row)
    return rows


def _avg_busbw(output: str) -> float | None:
    match = re.search(r"Avg bus bandwidth\s*:\s*([0-9.]+)", output)
    return float(match.group(1)) if match else None


def _run_streaming(command: list[str], env: dict, label: str, timeout: int = 3600) -> tuple[int, str]:
    """Run a tool in its own process group, stream lines as events, kill cleanly on stop."""
    EMITTER.log("$ " + shlex.join(command), source=label)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                               env=env, start_new_session=True, preexec_fn=die_with_parent)
    lines: list[str] = []
    deadline = time.monotonic() + timeout

    def killer():
        while process.poll() is None:
            if EMITTER.stopping or time.monotonic() > deadline:
                terminate_group(process)
                return
            time.sleep(0.3)

    watcher = threading.Thread(target=killer, daemon=True)
    watcher.start()
    for line in process.stdout:
        line = line.rstrip("\n")
        lines.append(line)
        EMITTER.log(line, source=label, level="output")
    code = process.wait()
    watcher.join(timeout=2)
    if EMITTER.stopping:
        raise StopRequested("Stopped by user")
    if time.monotonic() > deadline:
        raise BenchmarkError(f"{label} timed out after {timeout}s")
    return code, "\n".join(lines)


def die_with_parent() -> None:
    """preexec_fn: the child gets SIGTERM if gpu-bench itself is killed (no orphaned servers)."""
    try:
        import ctypes

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:
        pass


def terminate_group(process: subprocess.Popen, grace: float = 10) -> None:
    """SIGTERM the process group, then SIGKILL; leaves no orphaned workers."""
    if process.poll() is not None:
        return
    try:
        pgid = os.getpgid(process.pid)
    except OSError:
        return
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5)):
        try:
            os.killpg(pgid, sig)
        except OSError:
            return
        try:
            process.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def nccl(test: str | list = "all", min_bytes: str = "8", max_bytes: str = "1G", iterations: int = 20,
         gpus=None, hosts: str | None = None, hostfile: str | None = None, ranks: int | None = None,
         gpus_per_rank: int | None = None, mpi_args: str | None = None) -> list[dict]:
    """nccl-tests on one node (all selected GPUs in one process) or across nodes via MPI/Slurm."""
    if isinstance(test, str):
        tests = list(DEFAULT_NCCL_TESTS) if test == "all" else [t.strip() for t in test.split(",") if t.strip()]
    else:
        tests = list(test)
    unknown = [t for t in tests if t not in NCCL_TESTS]
    if unknown:
        raise BenchmarkError(f"Unknown NCCL test {', '.join(unknown)}; choose from {', '.join(NCCL_TESTS)} or all")
    multi_node = bool(hosts or hostfile or ranks or os.environ.get("SLURM_PROCID") is not None
                      or os.environ.get("OMPI_COMM_WORLD_RANK") is not None)
    env = dict(os.environ)
    if multi_node and not env.get("NCCL_DEBUG"):
        # Lets the result say which network NCCL really used (IB/RoCE or sockets).
        env.update({"NCCL_DEBUG": "INFO", "NCCL_DEBUG_SUBSYS": "INIT,NET"})
    if multi_node:
        devices = None
        per_rank = gpus_per_rank or 1
        bin_dir = BIN_DIR / "mpi"
    else:
        torch = _torch_cuda()
        devices = parse_gpu_list(gpus, torch.cuda.device_count())
        per_rank = len(devices)
        env["CUDA_VISIBLE_DEVICES"] = visible_devices_env(devices)
        bin_dir = BIN_DIR
    entries = []
    for name in tests:
        EMITTER.check_stop()
        binary = bin_dir / NCCL_TESTS[name]
        if not binary.is_file():
            raise Unavailable(f"NCCL test binary is missing: {binary}")
        command = [str(binary), "-b", str(min_bytes), "-e", str(max_bytes), "-f", "2", "-g", str(per_rank),
                   "-n", str(iterations), "-w", "5", "-c", "0"]
        launcher = "single-process"
        if os.environ.get("SLURM_PROCID") is not None or os.environ.get("OMPI_COMM_WORLD_RANK") is not None:
            launcher = "external (srun/mpirun started this rank)"
        elif hosts or hostfile or ranks:
            mpirun = shutil.which("mpirun")
            if not mpirun:
                raise Unavailable("MPI launch requested but mpirun is not installed in this image.")
            world = ranks or per_rank
            prefix = [mpirun, "--allow-run-as-root", "-np", str(world), "--bind-to", "none",
                      "-x", "NCCL_DEBUG", "-x", "NCCL_SOCKET_IFNAME", "-x", "NCCL_IB_HCA",
                      "-x", "NCCL_IB_GID_INDEX", "-x", "NCCL_NET_GDR_LEVEL", "-x", "LD_LIBRARY_PATH", "-x", "PATH"]
            if hosts:
                prefix += ["--host", hosts]
            if hostfile:
                prefix += ["--hostfile", hostfile]
            if mpi_args:
                prefix += shlex.split(mpi_args)
            command = prefix + command
            launcher = "openmpi"
        started = time.time()
        with GpuMonitor(interval=0.5, indices=devices) as monitor:
            EMITTER.progress("nccl", test=name, summary=name, force=True)
            code, output = _run_streaming(command, env, f"nccl:{name}")
        rows = _parse_nccl_metrics(output)
        error = None
        status = "PASSED"
        rank = next((int(os.environ[v]) for v in ("OMPI_COMM_WORLD_RANK", "PMIX_RANK", "SLURM_PROCID")
                     if (os.environ.get(v) or "").isdigit()), 0)
        if code:
            status, error = "FAILED", f"{NCCL_TESTS[name]} exited {code}: {output[-1500:]}"
        elif not rows and rank > 0:
            pass  # nccl-tests prints results on rank 0 only
        elif not rows:
            status, error = "FAILED", f"{NCCL_TESTS[name]} printed no result rows: {output[-1500:]}"
        wrong = re.search(r"Out of bounds values\s*:\s*(\d+)\s+(OK|FAILED)", output)
        from .distributed import nccl_transport

        network = nccl_transport(output)
        peak = max(rows, key=lambda r: r["busbw_gb_per_second"]) if rows else None
        entry = {"test": "nccl", "name": f"NCCL {name}", "status": status,
                 "parameters": {"collective": name, "min_bytes": str(min_bytes), "max_bytes": str(max_bytes),
                                "iterations": iterations, "gpus": devices, "gpus_per_rank": per_rank,
                                "ranks": ranks, "launcher": launcher, "multi_node": multi_node},
                 "results": {"collective": name, "rows": rows, "avg_busbw_gb_per_second": _avg_busbw(output),
                             "peak_busbw_gb_per_second": peak["busbw_gb_per_second"] if peak else None,
                             "peak_algbw_gb_per_second": max((r["algbw_gb_per_second"] for r in rows), default=None),
                             "peak_at_bytes": peak["bytes"] if peak else None,
                             "out_of_bounds": wrong.group(0) if wrong else None,
                             "command": shlex.join(command), "runtime_seconds": time.time() - started,
                             "nccl_network": network if network.get("transport") else None,
                             "measurement_note": "algbw = bytes/time; busbw applies the collective's correction "
                                                 "factor. Neither is raw link speed."},
                 "gpu_sample": monitor.summary(), "error": error}
        EMITTER.result(entry)
        entries.append(entry)
    return entries
