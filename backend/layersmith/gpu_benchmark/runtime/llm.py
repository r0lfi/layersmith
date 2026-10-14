"""vLLM's built-in serving benchmark, with a local-only temporary server."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from .benchmarks import BenchmarkError, _torch_cuda
from .system import NvmlSampler


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_ready(process: subprocess.Popen, port: int, log_path: Path, timeout: int = 900) -> None:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(2)
    try:
        tail = log_path.read_text(errors="replace")[-5000:]
    except OSError:
        tail = ""
    raise BenchmarkError(f"vLLM server did not become ready within {timeout}s. {tail}")


def _metric(text: str, pattern: str) -> float | None:
    match = re.search(pattern + r"\s*:\s*([0-9]+(?:\.[0-9]+)?)", text, re.I)
    return float(match.group(1)) if match else None


def _parse_bench_output(text: str, concurrency: int) -> dict:
    output = _metric(text, r"Output token throughput\s*\(tok/s\)")
    total = _metric(text, r"Total token throughput\s*\(tok/s\)")
    requests = _metric(text, r"Request throughput\s*\(req/s\)")
    values = {"output_tokens_per_second": output, "total_tokens_per_second": total,
              "tokens_per_second_per_active_request": output / concurrency if output is not None else None,
              "request_throughput_per_second": requests}
    for metric in ("ttft", "tpot", "e2el"):
        values[metric] = {}
        for percentile in ("Mean", "P50", "Median", "P95", "P99"):
            value = _metric(text, rf"{percentile} {metric.upper()}\s*\(ms\)")
            if value is not None:
                values[metric][percentile.lower()] = value
    return values


def benchmark(model: str, input_tokens: int = 1024, output_tokens: int = 256,
              concurrency: str = "1,8,16,32", requests: int = 100,
              warmup_requests: int = 5, tensor_parallel_size: int | None = None,
              precision: str = "auto") -> list[dict]:
    path = Path(model).expanduser()
    if not path.is_dir():
        raise BenchmarkError(f"Model directory does not exist: {model}. Mount local model files under /models; downloads are disabled.")
    if not (path / "config.json").is_file():
        raise BenchmarkError(f"Model directory has no config.json: {model}")
    try:
        import vllm  # noqa: F401
    except Exception as exc:
        raise BenchmarkError(f"vLLM is not installed in this image: {exc}") from exc
    torch = _torch_cuda()
    tp = tensor_parallel_size or torch.cuda.device_count()
    if tp < 1 or tp > torch.cuda.device_count():
        raise BenchmarkError(f"Tensor parallel size {tp} is invalid for {torch.cuda.device_count()} visible GPU(s).")
    try:
        levels = [int(value.strip()) for value in concurrency.split(",") if value.strip()]
    except ValueError as exc:
        raise BenchmarkError("Concurrency must be a comma-separated list of positive integers.") from exc
    if not levels or any(value < 1 for value in levels):
        raise BenchmarkError("Concurrency must contain positive integers.")
    port = _free_port()
    env = dict(os.environ)
    env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1", "VLLM_CONFIGURE_LOGGING": "0"})
    server = ["vllm", "serve", str(path), "--host", "127.0.0.1", "--port", str(port),
              "--served-model-name", "gpu-bench", "--tensor-parallel-size", str(tp), "--dtype", precision,
              "--disable-log-requests"]
    results = []
    with tempfile.TemporaryDirectory(prefix="gpu-bench-vllm-") as tmp:
        work = Path(tmp)
        server_log = work / "vllm-server.log"
        with server_log.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(server, stdout=log, stderr=subprocess.STDOUT, env=env)
            try:
                _wait_ready(process, port, server_log)
                for level in levels:
                    vllm_json = work / f"vllm-concurrency-{level}.json"
                    command = ["vllm", "bench", "serve", "--backend", "openai", "--host", "127.0.0.1",
                               "--port", str(port), "--endpoint", "/v1/completions", "--model", "gpu-bench",
                               "--dataset-name", "random", "--num-prompts", str(requests),
                               "--max-concurrency", str(level), "--random-input-len", str(input_tokens),
                               "--random-output-len", str(output_tokens), "--num-warmups", str(warmup_requests),
                               "--percentile-metrics", "ttft,tpot,e2el", "--metric-percentiles", "50,95,99",
                               "--ignore-eos", "--save-result", "--result-dir", str(work),
                               "--result-filename", vllm_json.name]
                    started = dt.datetime.now(dt.timezone.utc).isoformat()
                    with NvmlSampler() as sampler:
                        result = subprocess.run(command, capture_output=True, text=True, timeout=7200, env=env)
                    if result.returncode:
                        raise BenchmarkError(f"vLLM benchmark at concurrency {level} exited {result.returncode}: "
                                             f"{(result.stderr or result.stdout)[-3000:]}")
                    metrics = _parse_bench_output(result.stdout + "\n" + result.stderr, level)
                    try:
                        saved = json.loads(vllm_json.read_text())
                    except (OSError, ValueError):
                        saved = None
                    results.append({"test": "llm", "status": "PASSED", "started_at": started,
                                    "parameters": {"model": str(path), "input_tokens": input_tokens,
                                                   "output_tokens": output_tokens, "concurrency": level,
                                                   "requests": requests, "warmup_requests": warmup_requests,
                                                   "precision": precision, "tensor_parallel_size": tp,
                                                   "benchmark_engine": "vllm bench serve"},
                                    "results": metrics, "gpu_sample": sampler.summary(),
                                    "vllm_result_file": saved, "stdout": result.stdout[-12000:]})
            finally:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
    return results
