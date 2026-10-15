"""LLM token-speed benchmark: a local vLLM server and a streaming load generator.

vLLM serves a model from /models on 127.0.0.1 only. The load generator sends
prompts as exact token-ID lists, streams every response and timestamps each
chunk. Token counts come from the server's own usage records (continuous
usage stats), and the server's Prometheus counters are read before and after
each measured phase to cross-check the client totals.

Two things are always kept apart:
  * single-request speed: how fast one stream generates (concurrency 1), and
  * aggregate throughput: all tokens from all concurrent streams per second.
"""

from __future__ import annotations

import datetime as dt
import http.client
import json
import math
import os
import random
import re
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .benchmarks import (BenchmarkError, Unavailable, _torch_cuda, die_with_parent, parse_gpu_list, terminate_group,
                         visible_devices_env)
from .events import EMITTER, StopRequested
from .system import GpuMonitor

MODEL_ROOT = os.environ.get("GPU_BENCH_MODELS", "/models")
SERVED_NAME = "gpu-bench"
PRECISIONS = {"auto": "auto", "float16": "float16", "fp16": "float16", "bfloat16": "bfloat16",
              "bf16": "bfloat16", "float32": "float32", "fp32": "float32"}
WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".pth")


# --------------------------------------------------------------- models

def _model_config(path: Path) -> dict:
    try:
        config = json.loads((path / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def _text_config(config: dict) -> dict:
    nested = config.get("text_config") or config.get("llm_config")
    return nested if isinstance(nested, dict) else config


def model_details(path: str | Path) -> dict:
    path = Path(path)
    config = _model_config(path)
    text = _text_config(config)
    weights = [p for p in path.iterdir() if p.is_file() and p.name.endswith(WEIGHT_SUFFIXES)] if path.is_dir() else []
    quant = config.get("quantization_config") or text.get("quantization_config") or {}
    return {
        "path": str(path),
        "name": path.name,
        "architectures": config.get("architectures") or text.get("architectures") or [],
        "model_type": config.get("model_type"),
        "dtype": config.get("torch_dtype") or config.get("dtype") or text.get("torch_dtype"),
        "quantization": quant.get("quant_method") if isinstance(quant, dict) else None,
        "max_position_embeddings": text.get("max_position_embeddings") or config.get("max_position_embeddings"),
        "num_attention_heads": text.get("num_attention_heads"),
        "num_key_value_heads": text.get("num_key_value_heads"),
        "hidden_size": text.get("hidden_size"),
        "num_layers": text.get("num_hidden_layers"),
        "vocab_size": text.get("vocab_size") or config.get("vocab_size"),
        "weight_bytes": sum(p.stat().st_size for p in weights),
        "weight_files": len(weights),
        "has_tokenizer": any((path / name).exists() for name in
                             ("tokenizer.json", "tokenizer.model", "tokenizer_config.json", "vocab.json")),
    }


def discover_models(root: str | Path = MODEL_ROOT, max_depth: int = 3) -> list[dict]:
    """Hugging Face style model directories (config.json + weights) under root."""
    root = Path(root)
    found = []
    if not root.is_dir():
        return found

    def visit(directory: Path, depth: int):
        try:
            children = sorted(directory.iterdir())
        except OSError:
            return
        if (directory / "config.json").is_file() and any(c.name.endswith(WEIGHT_SUFFIXES) for c in children):
            details = model_details(directory)
            details["relative"] = str(directory.relative_to(root)) if directory != root else "."
            found.append(details)
            return
        if depth >= max_depth:
            return
        for child in children:
            if child.is_dir() and not child.name.startswith("."):
                visit(child, depth + 1)

    visit(root, 0)
    return found


# --------------------------------------------------------------- server

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def scrape_counters(port: int) -> dict:
    """Sum of vLLM's Prometheus token and request counters (all label sets)."""
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request("GET", "/metrics")
        text = connection.getresponse().read().decode("utf-8", errors="replace")
        connection.close()
    except (OSError, http.client.HTTPException):
        return {}
    totals = {}
    for name, key in (("vllm:prompt_tokens_total", "prompt_tokens"),
                      ("vllm:generation_tokens_total", "generation_tokens"),
                      ("vllm:request_success_total", "requests_succeeded")):
        values = re.findall(rf"^{re.escape(name)}(?:{{[^}}]*}})?\s+([0-9.eE+-]+)\s*$", text, re.M)
        if values:
            totals[key] = sum(float(value) for value in values)
    return totals


class VllmServer:
    """A vLLM OpenAI server on localhost, in its own process group."""

    def __init__(self, model: Path, tensor_parallel_size: int, dtype: str, devices: list[int],
                 max_model_len: int, gpu_memory_utilization: float, max_num_seqs: int,
                 log_path: Path, trust_remote_code: bool = False, extra_args: list[str] | None = None):
        self.model = model
        self.port = _free_port()
        self.log_path = log_path
        self.process: subprocess.Popen | None = None
        vllm = shutil.which("vllm")
        if not vllm:
            raise Unavailable("vLLM is not installed in this image (the LLM component was not selected).")
        self.command = [vllm, "serve", str(model), "--host", "127.0.0.1", "--port", str(self.port),
                        "--served-model-name", SERVED_NAME, "--tensor-parallel-size", str(tensor_parallel_size),
                        "--dtype", dtype, "--max-model-len", str(max_model_len),
                        "--gpu-memory-utilization", str(gpu_memory_utilization),
                        "--max-num-seqs", str(max_num_seqs), "--seed", "0", "--disable-uvicorn-access-log",
                        # Every prompt is unique anyway; this also keeps a
                        # warmup from ever serving measured prompts from cache.
                        "--no-enable-prefix-caching"]
        if trust_remote_code:
            self.command.append("--trust-remote-code")
        self.command += extra_args or []
        self.env = dict(os.environ, CUDA_VISIBLE_DEVICES=visible_devices_env(devices),
                        HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1",
                        HF_HUB_DISABLE_TELEMETRY="1", VLLM_NO_USAGE_STATS="1", DO_NOT_TRACK="1")
        self._follow_stop = threading.Event()

    def _follow_log(self) -> None:
        offset = 0
        while not self._follow_stop.is_set():
            try:
                with self.log_path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(offset)
                    for line in handle:
                        if line.endswith("\n"):
                            EMITTER.log(line.rstrip(), source="vllm", level="output")
                            offset += len(line.encode("utf-8"))
                        else:
                            break
            except OSError:
                pass
            self._follow_stop.wait(0.5)

    def start(self, timeout: int = 1800) -> None:
        EMITTER.log("$ " + " ".join(self.command), source="vllm")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = self.log_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(self.command, stdout=log, stderr=subprocess.STDOUT, env=self.env,
                                        start_new_session=True, preexec_fn=die_with_parent)
        log.close()
        threading.Thread(target=self._follow_log, daemon=True).start()
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        while time.monotonic() < deadline:
            if EMITTER.stopping:
                raise StopRequested("Stopped by user")
            if self.process.poll() is not None:
                tail = self.log_path.read_text(errors="replace")[-4000:]
                raise BenchmarkError(f"vLLM exited with status {self.process.returncode} while loading the model.\n{tail}")
            try:
                connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
                connection.request("GET", "/v1/models")
                if connection.getresponse().status == 200:
                    connection.close()
                    EMITTER.log(f"vLLM ready after {time.monotonic() - started:.0f}s", source="vllm")
                    return
            except (OSError, http.client.HTTPException):
                pass
            EMITTER.progress("llm-load", elapsed=time.monotonic() - started,
                             summary=f"loading model {time.monotonic() - started:.0f}s")
            time.sleep(1)
        raise BenchmarkError(f"vLLM did not become ready within {timeout}s; see {self.log_path}")

    def stop(self) -> None:
        if self.process is not None:
            terminate_group(self.process, grace=30)
        self._follow_stop.set()


# -------------------------------------------------------------- prompts

def make_prompts(model: Path, input_tokens: int, count: int, seed: int = 0,
                 trust_remote_code: bool = False) -> tuple[list[list[int]], str]:
    """Random token-ID prompts of exactly input_tokens, avoiding special tokens."""
    rng = random.Random(seed)
    special: set[int] = set()
    vocab = None
    source = "tokenizer"
    try:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(str(model), local_files_only=True,
                                                  trust_remote_code=trust_remote_code)
        vocab = int(getattr(tokenizer, "vocab_size", 0) or len(tokenizer))
        special = set(getattr(tokenizer, "all_special_ids", []) or [])
        special |= {int(i) for i in (getattr(tokenizer, "added_tokens_decoder", {}) or {}).keys()}
    except Exception as exc:
        EMITTER.log(f"Tokenizer not loadable ({exc}); using vocab_size from config.json", level="warning")
        source = "config.json vocab_size"
    if not vocab:
        vocab = int(model_details(model).get("vocab_size") or 0)
    if vocab < 1000:
        raise BenchmarkError("Cannot determine the model vocabulary to build token prompts.")
    # Stay clear of the low IDs, where most tokenizers keep control tokens.
    low = min(1000, vocab // 10)
    prompts = []
    for _ in range(count):
        prompt = []
        while len(prompt) < input_tokens:
            token = rng.randrange(low, vocab)
            if token not in special:
                prompt.append(token)
        prompts.append(prompt)
    return prompts, source


# --------------------------------------------------------- load generator

@dataclass
class RequestRecord:
    index: int
    start: float = 0.0
    first_token: float | None = None
    end: float | None = None
    chunk_times: list = field(default_factory=list)
    chunk_tokens: list = field(default_factory=list)
    prompt_tokens: int | None = None
    output_tokens: int = 0
    count_source: str = "server usage"
    error: str | None = None


class LoadGenerator:
    def __init__(self, port: int, output_tokens: int, timeout: float = 1800):
        self.port = port
        self.output_tokens = output_tokens
        self.timeout = timeout
        self._sockets: set = set()
        self._lock = threading.Lock()

    def _body(self, prompt: list[int]) -> bytes:
        return json.dumps({
            "model": SERVED_NAME, "prompt": prompt, "max_tokens": self.output_tokens,
            "min_tokens": self.output_tokens, "ignore_eos": True, "temperature": 0.0, "stream": True,
            "stream_options": {"include_usage": True, "continuous_usage_stats": True},
        }).encode("utf-8")

    def request(self, record: RequestRecord, prompt: list[int]) -> RequestRecord:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        sock = None
        record.start = time.perf_counter()
        try:
            connection.request("POST", "/v1/completions", body=self._body(prompt),
                               headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
            # Keep the socket itself: http.client drops connection.sock once a
            # response is marked will-close, and stopping must still reach it.
            sock = connection.sock
            with self._lock:
                self._sockets.add(sock)
            response = connection.getresponse()
            if response.status != 200:
                record.error = f"HTTP {response.status}: {response.read()[:500].decode(errors='replace')}"
                return record
            counted_chunks = 0
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                now = time.perf_counter()
                chunk = json.loads(payload)
                usage = chunk.get("usage") or {}
                choices = chunk.get("choices") or []
                if usage.get("prompt_tokens") is not None:
                    record.prompt_tokens = int(usage["prompt_tokens"])
                if choices:
                    if usage.get("completion_tokens") is not None:
                        new_total = int(usage["completion_tokens"])
                    else:
                        record.count_source = "stream chunks"
                        counted_chunks += 1 if choices[0].get("text") else 0
                        new_total = counted_chunks
                    added = new_total - record.output_tokens
                    if added > 0:
                        if record.first_token is None:
                            record.first_token = now
                        record.chunk_times.append(now)
                        record.chunk_tokens.append(added)
                        record.output_tokens = new_total
                elif usage.get("completion_tokens") is not None:
                    # Final usage chunk: the server's authoritative count.
                    record.output_tokens = int(usage["completion_tokens"])
            record.end = time.perf_counter()
            if EMITTER.stopping and record.output_tokens < self.output_tokens:
                record.error = "Aborted: the benchmark was stopped."
            elif record.first_token is None:
                record.error = "The server returned no tokens."
        except (OSError, http.client.HTTPException, ValueError) as exc:
            record.error = record.error or ("Aborted: the benchmark was stopped." if EMITTER.stopping
                                            else f"{type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                self._sockets.discard(sock)
            connection.close()
        return record

    def abort_all(self) -> None:
        with self._lock:
            sockets = [sock for sock in self._sockets if sock is not None]
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def run(self, prompts: list[list[int]], concurrency: int, on_tick=None) -> tuple[list[RequestRecord], float, float]:
        """Send len(prompts) requests with at most `concurrency` in flight."""
        records = [RequestRecord(index=i) for i in range(len(prompts))]
        cursor = {"next": 0}
        lock = threading.Lock()

        def worker():
            while not EMITTER.stopping:
                with lock:
                    index = cursor["next"]
                    if index >= len(prompts):
                        return
                    cursor["next"] += 1
                self.request(records[index], prompts[index])

        started = time.perf_counter()
        threads = [threading.Thread(target=worker, daemon=True) for _ in range(min(concurrency, len(prompts)))]
        for thread in threads:
            thread.start()
        while any(thread.is_alive() for thread in threads):
            if EMITTER.stopping:
                self.abort_all()
            if on_tick:
                on_tick(records, started)
            time.sleep(0.5)
        for thread in threads:
            thread.join()
        finished = max((r.end for r in records if r.end), default=time.perf_counter())
        if on_tick:
            on_tick(records, started)
        return records, started, finished


# ---------------------------------------------------------------- metrics

def percentiles(values: list[float], points=(50, 90, 95, 99)) -> dict:
    """Linear-interpolated percentiles (numpy's default method)."""
    if not values:
        return {}
    ordered = sorted(values)
    out = {"mean": sum(ordered) / len(ordered), "min": ordered[0], "max": ordered[-1]}
    for point in points:
        rank = (len(ordered) - 1) * point / 100
        low, high = math.floor(rank), math.ceil(rank)
        out[f"p{point}"] = ordered[low] + (ordered[high] - ordered[low]) * (rank - low)
    return out


def summarize(records: list[RequestRecord], started: float, finished: float, concurrency: int,
              input_tokens: int, output_tokens: int) -> dict:
    ok = [r for r in records if r.error is None and r.end and r.first_token]
    failed = [r for r in records if r.error is not None]
    duration = max(finished - started, 1e-9)
    out_total = sum(r.output_tokens for r in ok)
    in_total = sum(r.prompt_tokens or 0 for r in ok)
    ttft = [(r.first_token - r.start) * 1000 for r in ok]
    e2e = [(r.end - r.start) * 1000 for r in ok]
    decoding = [r for r in ok if r.output_tokens > 1 and r.end > r.first_token]
    tpot = [(r.end - r.first_token) * 1000 / (r.output_tokens - 1) for r in decoding]
    stream_rate = [(r.output_tokens - 1) / (r.end - r.first_token) for r in decoding]
    itl = []
    for r in ok:
        for previous, current, tokens in zip(r.chunk_times, r.chunk_times[1:], r.chunk_tokens[1:]):
            itl.append((current - previous) * 1000 / max(tokens, 1))
    metrics = {
        "mode": "single-request" if concurrency == 1 else "concurrent",
        "concurrency": concurrency,
        "requested": len(records),
        "completed_requests": len(ok),
        "failed_requests": len(failed),
        "duration_seconds": duration,
        "total_input_tokens": in_total,
        "total_output_tokens": out_total,
        "aggregate_output_tokens_per_second": out_total / duration if ok else None,
        "total_tokens_per_second": (in_total + out_total) / duration if ok else None,
        "request_throughput_per_second": len(ok) / duration if ok else None,
        "per_request_output_tokens_per_second": percentiles(stream_rate, (10, 50, 90)),
        "ttft_ms": percentiles(ttft),
        "tpot_ms": percentiles(tpot),
        "itl_ms": percentiles(itl),
        "e2e_latency_ms": percentiles(e2e),
        "input_tokens_mismatched_requests": sum(1 for r in ok if r.prompt_tokens != input_tokens),
        "output_tokens_mismatched_requests": sum(1 for r in ok if r.output_tokens != output_tokens),
        "token_count_source": sorted({r.count_source for r in ok}) or None,
        "errors": sorted({r.error for r in failed})[:5],
    }
    if concurrency == 1 and ok:
        estimates = [(r.prompt_tokens or 0) / (r.first_token - r.start) for r in ok if r.first_token > r.start]
        metrics["prefill_tokens_per_second_estimate"] = sum(estimates) / len(estimates) if estimates else None
        metrics["prefill_note"] = ("prompt tokens / TTFT at concurrency 1; includes HTTP and scheduling "
                                   "overhead, so it slightly understates the engine's prefill rate.")
    return metrics


def _live_state(records: list[RequestRecord], started: float, window: list) -> dict:
    now = time.perf_counter()
    tokens = sum(r.output_tokens for r in records)
    window.append((now, tokens))
    while len(window) > 2 and now - window[0][0] > 5:
        window.pop(0)
    rate = ((window[-1][1] - window[0][1]) / (window[-1][0] - window[0][0])
            if len(window) > 1 and window[-1][0] > window[0][0] else 0.0)
    done = [r for r in records if r.end and r.error is None]
    ttft = [(r.first_token - r.start) * 1000 for r in done if r.first_token]
    stats = percentiles(ttft, (50, 95))
    return {"elapsed": now - started, "completed": len(done),
            "failed": sum(1 for r in records if r.error), "total": len(records),
            "in_flight": sum(1 for r in records if r.start and not r.end and not r.error),
            "output_tokens": tokens, "window_tokens_per_second": rate,
            "running_ttft_p50_ms": stats.get("p50"), "running_ttft_p95_ms": stats.get("p95")}


# ------------------------------------------------------------- benchmark

def _levels(concurrency) -> list[int]:
    if isinstance(concurrency, int):
        return [concurrency]
    try:
        values = [int(part) for part in str(concurrency).split(",") if part.strip()]
    except ValueError as exc:
        raise BenchmarkError("Concurrency must be comma-separated positive integers, e.g. 1,8,32.") from exc
    if not values or any(value < 1 for value in values):
        raise BenchmarkError("Concurrency must contain positive integers.")
    return values


def benchmark(model: str, input_tokens: int = 1024, output_tokens: int = 256, concurrency="1,8,32",
              requests: int = 64, warmup_requests: int = 4, tensor_parallel_size: int | None = None,
              precision: str = "auto", gpus=None, single_requests: int = 10, max_model_len: int | None = None,
              gpu_memory_utilization: float = 0.90, server_timeout: int = 1800, log_dir: str | Path | None = None,
              trust_remote_code: bool = False, extra_server_args: list[str] | None = None,
              seed: int = 0) -> list[dict]:
    path = Path(model).expanduser()
    if not path.is_dir():
        raise BenchmarkError(f"Model directory does not exist: {model}. Mount local models with -v /models:/models:ro; "
                             "nothing is downloaded.")
    if not (path / "config.json").is_file():
        raise BenchmarkError(f"{model} has no config.json; point --model at a Hugging Face model directory.")
    if precision not in PRECISIONS:
        raise BenchmarkError(f"Unknown precision {precision}; choose {', '.join(sorted(set(PRECISIONS)))}")
    dtype = PRECISIONS[precision]
    if input_tokens < 1 or output_tokens < 1 or requests < 1:
        raise BenchmarkError("Input tokens, output tokens and requests must be positive.")
    levels = _levels(concurrency)
    torch = _torch_cuda()
    devices = parse_gpu_list(gpus, torch.cuda.device_count())
    del torch  # the server owns the GPUs; this process must not hold CUDA memory
    tp = tensor_parallel_size or len(devices)
    if tp < 1 or tp > len(devices):
        raise BenchmarkError(f"Tensor parallel size {tp} needs at least {tp} selected GPU(s); {len(devices)} selected.")
    details = model_details(path)
    heads = details.get("num_attention_heads")
    if isinstance(heads, int) and heads % tp:
        raise BenchmarkError(f"The model has {heads} attention heads, not divisible by tensor parallel size {tp}.")
    needed = input_tokens + output_tokens + 8
    model_max = details.get("max_position_embeddings")
    if isinstance(model_max, int) and needed > model_max:
        raise BenchmarkError(f"Input + output ({needed} tokens) exceeds the model's context of {model_max}.")
    length = max_model_len or (min(model_max, int(math.ceil(needed / 256) * 256)) if isinstance(model_max, int)
                               else int(math.ceil(needed / 256) * 256))
    log_dir = Path(log_dir) if log_dir else Path("/tmp/gpu-bench-logs")
    plan = []
    for level in levels:
        measured = single_requests if level == 1 else requests
        warmup = max(warmup_requests, min(level, 64)) if warmup_requests else 0
        plan.append((level, measured, warmup))
    prompts, prompt_source = make_prompts(path, input_tokens, sum(m + w for _, m, w in plan), seed,
                                          trust_remote_code)
    server = VllmServer(path, tp, dtype, devices, length, gpu_memory_utilization, max(256, max(levels)),
                        log_dir / "vllm-server.log", trust_remote_code, extra_server_args)
    entries = []
    try:
        EMITTER.log(f"Starting vLLM: {path.name}, TP={tp}, GPUs {devices}, dtype {dtype}, max-model-len {length}")
        server.start(server_timeout)
        generator = LoadGenerator(server.port, output_tokens)
        offset = 0
        for level, measured, warmup in plan:
            EMITTER.check_stop()
            # Every request in the run gets its own, never repeated, prompt.
            warm_prompts = prompts[offset:offset + warmup]
            measured_prompts = prompts[offset + warmup:offset + warmup + measured]
            offset += warmup + measured
            if warmup:
                EMITTER.log(f"Warmup: {warmup} request(s) at concurrency {level} (not measured)")
                warm_records, _, _ = generator.run(warm_prompts, level)
                bad = [r.error for r in warm_records if r.error]
                if bad and len(bad) == len(warm_records):
                    raise BenchmarkError(f"Every warmup request failed: {bad[0]}")
            EMITTER.check_stop()
            before = scrape_counters(server.port)
            EMITTER.log(f"Measuring {measured} request(s) at concurrency {level}: "
                        f"{input_tokens} in / {output_tokens} out tokens")
            window: list = []
            started_at = dt.datetime.now(dt.timezone.utc).isoformat()
            with GpuMonitor(interval=0.5, indices=devices) as monitor:
                def tick(records, started):
                    state = _live_state(records, started, window)
                    EMITTER.progress("llm", concurrency=level, gpus=monitor.latest, **state,
                                     summary=f"c={level} {state['completed']}/{state['total']} "
                                             f"{state['window_tokens_per_second']:.0f} tok/s")

                records, started, finished = generator.run(measured_prompts, level, on_tick=tick)
            after = scrape_counters(server.port)
            metrics = summarize(records, started, finished, level, input_tokens, output_tokens)
            server_counts = {key: after[key] - before.get(key, 0) for key in after}
            metrics["server_counters"] = server_counts
            if server_counts.get("generation_tokens") is not None:
                metrics["server_counts_match_client"] = (
                    int(server_counts["generation_tokens"]) == metrics["total_output_tokens"]
                    and int(server_counts.get("prompt_tokens", -1)) == metrics["total_input_tokens"])
            stopped = EMITTER.stopping
            status = "STOPPED" if stopped else ("PASSED" if metrics["completed_requests"] and
                                                not metrics["failed_requests"] else
                                                "FAILED" if not metrics["completed_requests"] else "PASSED")
            entry = {"test": "llm", "name": f"LLM {'single request' if level == 1 else f'concurrency {level}'}",
                     "status": status, "started_at": started_at,
                     "parameters": {"model": str(path), "model_name": path.name, "input_tokens": input_tokens,
                                    "output_tokens": output_tokens, "concurrency": level, "requests": measured,
                                    "warmup_requests": warmup, "precision": dtype, "tensor_parallel_size": tp,
                                    "gpus": devices, "gpu_count": len(devices), "max_model_len": length,
                                    "gpu_memory_utilization": gpu_memory_utilization,
                                    "prompt_source": f"random token IDs ({prompt_source}), seed {seed}",
                                    "sampling": "greedy, ignore_eos, fixed output length",
                                    "engine": "vLLM OpenAI server (/v1/completions, streaming)"},
                     "results": metrics, "gpu_sample": monitor.summary(),
                     "model_info": details,
                     "error": ("Stopped by user: partial measurement, not a benchmark result." if stopped else
                               ("; ".join(metrics["errors"]) or None))}
            EMITTER.result(entry)
            entries.append(entry)
            if stopped:
                raise StopRequested("Stopped by user")
    finally:
        EMITTER.log("Stopping vLLM server")
        server.stop()
    return entries
