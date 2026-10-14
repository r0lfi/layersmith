"""The gpu-bench command line interface."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

from . import benchmarks, llm, reports
from .system import collect_system_info, health_checks

DEFAULT_OUTPUT = "/results"
RECIPE_FILE = Path("/opt/gpu-bench/recipe.json")


def _tool_enabled(name: str) -> bool:
    configured = os.environ.get("GPU_BENCH_TOOLS")
    if configured is None:
        try:
            configured = ",".join(json.loads(RECIPE_FILE.read_text()).get("tools", []))
        except (OSError, ValueError, TypeError):
            return True
    return name in {value.strip() for value in configured.split(",") if value.strip()}


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="gpu-bench", description="Measure NVIDIA GPU, CUDA, vLLM and NCCL performance.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("info", help="show GPU, driver, topology and host information")
    sub.add_parser("health", help="check required GPU and optional network capabilities")

    p = sub.add_parser("llm", help="benchmark local LLM inference using vLLM's serving benchmark")
    p.add_argument("--model", required=True, help="local model directory; no automatic downloads")
    p.add_argument("--input-tokens", type=int, default=1024)
    p.add_argument("--output-tokens", type=int, default=256)
    p.add_argument("--concurrency", default="1,8,16,32", help="comma-separated concurrency levels")
    p.add_argument("--requests", type=int, default=100)
    p.add_argument("--warmup-requests", type=int, default=5)
    p.add_argument("--precision", choices=("auto", "float16", "bfloat16"), default="auto")
    p.add_argument("--tensor-parallel-size", type=int)
    p.add_argument("--output", default=DEFAULT_OUTPUT)

    p = sub.add_parser("compute", help="measure CUDA GEMM throughput with CUDA events")
    p.add_argument("--precision", choices=("fp32", "fp16", "bf16"), default="bf16")
    p.add_argument("--duration", type=int, default=60, help="minimum measured GPU time in seconds")
    p.add_argument("--size", type=int, default=4096, help="square matrix dimension")
    p.add_argument("--output", default=DEFAULT_OUTPUT)

    p = sub.add_parser("memory", help="measure CUDA Samples and PyTorch transfer bandwidth")
    p.add_argument("--output", default=DEFAULT_OUTPUT)

    p = sub.add_parser("nccl", help="run nccl-tests locally, with MPI, or under Slurm")
    p.add_argument("--test", choices=(*benchmarks.NCCL_TESTS, "all"), default="all")
    p.add_argument("--gpus", type=int, help="visible GPUs per rank; defaults to every visible GPU")
    p.add_argument("--min-bytes", default="8")
    p.add_argument("--max-bytes", default="128M")
    p.add_argument("--iterations", type=int, default=20)
    p.add_argument("--hosts", help="OpenMPI host list, for example node-a,node-b")
    p.add_argument("--hostfile", help="OpenMPI hostfile path")
    p.add_argument("--ranks", type=int, help="MPI world size")
    p.add_argument("--output", default=DEFAULT_OUTPUT)

    p = sub.add_parser("full", help="run available tests and write the complete report bundle")
    p.add_argument("--model", help="optional local model path; the LLM test is skipped if omitted")
    p.add_argument("--input-tokens", type=int, default=1024)
    p.add_argument("--output-tokens", type=int, default=256)
    p.add_argument("--concurrency", default="1,8,16,32")
    p.add_argument("--requests", type=int, default=100)
    p.add_argument("--warmup-requests", type=int, default=5)
    p.add_argument("--precision", choices=("fp32", "fp16", "bf16"), default="bf16")
    p.add_argument("--output", default=DEFAULT_OUTPUT)

    p = sub.add_parser("report", help="regenerate reports or compare a previous benchmark JSON")
    p.add_argument("--input", default=f"{DEFAULT_OUTPUT}/benchmark.json")
    p.add_argument("--compare", help="another benchmark.json to compare with")
    p.add_argument("--output", default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def _entry(name: str, call, parameters: dict | None = None) -> list[dict]:
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        value = call()
        entries = value if isinstance(value, list) else [value]
        for item in entries:
            item.setdefault("test", name)
            item.setdefault("status", "PASSED")
            item.setdefault("started_at", started)
            item.setdefault("parameters", parameters or {})
        return entries
    except Exception as exc:
        return [{"test": name, "status": "FAILED", "started_at": started,
                 "parameters": parameters or {}, "results": {}, "error": str(exc)}]


def _write(output: str, info: dict, entries: list[dict], configuration: dict | None = None,
           append: bool = True) -> dict:
    slurm_rank = os.environ.get("SLURM_PROCID")
    if slurm_rank is not None and slurm_rank != "0":
        return {}
    destination = Path(output)
    existing_path = destination / "benchmark.json"
    data = {"schema_version": 1, "system_info": info, "configuration": configuration or {}, "results": []}
    if append and existing_path.is_file():
        try:
            data = json.loads(existing_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        data.setdefault("results", []).extend(entries)
        data["system_info"] = info
        if configuration:
            data.setdefault("configuration", {}).update(configuration)
    else:
        data["results"] = entries
    return reports.write_report(data, destination)


def _print_paths(paths: dict) -> None:
    for name, path in paths.items():
        print(f"{name}: {path}")


def _run_full(args) -> int:
    info = collect_system_info()
    health = health_checks(info)
    entries = [{"test": "health", "status": health["status"], "parameters": {}, "results": health,
                "error": None if health["status"] == "PASS" else "One or more required GPU checks failed."}]
    gpu_ready = health["status"] == "PASS"
    if gpu_ready:
        if _tool_enabled("compute"):
            entries += _entry("compute", lambda: benchmarks.compute(args.precision, duration=10),
                              {"precision": args.precision, "duration_seconds": 10})
        else:
            entries.append({"test": "compute", "status": "SKIPPED", "parameters": {}, "results": {},
                            "error": "Compute benchmark tools were not selected when this image was built."})
        if _tool_enabled("memory"):
            entries += _entry("memory", benchmarks.memory)
        else:
            entries.append({"test": "memory", "status": "SKIPPED", "parameters": {}, "results": {},
                            "error": "Memory benchmark tools were not selected when this image was built."})
        if not _tool_enabled("nccl"):
            entries.append({"test": "nccl", "status": "SKIPPED", "parameters": {}, "results": {},
                            "error": "NCCL tools were not selected when this image was built."})
        elif info.get("gpu_count", 0) >= 1:
            entries += _entry("nccl", lambda: benchmarks.nccl(test="all", gpus=info["gpu_count"]))
        else:
            entries.append({"test": "nccl", "status": "SKIPPED", "parameters": {}, "results": {},
                            "error": "No visible GPU is available for NCCL; use MPI/Slurm for a multi-node run."})
        if not _tool_enabled("llm"):
            entries.append({"test": "llm", "status": "SKIPPED", "parameters": {}, "results": {},
                            "error": "LLM benchmark tools were not selected when this image was built."})
        elif args.model:
            llm_precision = {"fp32": "float32", "fp16": "float16", "bf16": "bfloat16"}[args.precision]
            entries += _entry("llm", lambda: llm.benchmark(args.model, args.input_tokens, args.output_tokens,
                                                            args.concurrency, args.requests, args.warmup_requests,
                                                            precision=llm_precision),
                              {"model": args.model, "input_tokens": args.input_tokens,
                               "output_tokens": args.output_tokens, "concurrency": args.concurrency,
                               "requests": args.requests, "precision": llm_precision})
        else:
            entries.append({"test": "llm", "status": "SKIPPED", "parameters": {}, "results": {},
                            "error": "No local model path supplied. Mount a model under /models and pass --model."})
    else:
        reason = "No usable NVIDIA GPU is visible. Pass the host GPU devices to the container."
        for name in ("compute", "memory", "nccl", "llm"):
            entries.append({"test": name, "status": "SKIPPED", "parameters": {}, "results": {}, "error": reason})
    paths = _write(args.output, info, entries,
                   {"model": args.model, "precision": args.precision, "input_tokens": args.input_tokens,
                    "output_tokens": args.output_tokens, "concurrency": args.concurrency, "requests": args.requests},
                   append=False)
    for entry in entries:
        print(f"{entry['status']}: {entry['test']}" + (f" — {entry['error']}" if entry.get("error") else ""))
    _print_paths(paths)
    # Optional benchmark failures and missing host capabilities stay in the report;
    # the report generation itself is the success condition for `full`.
    return 0


def main(argv=None) -> int:
    args = _parse_args(argv)
    if args.command == "full" and os.environ.get("SLURM_PROCID") is not None:
        print("ERROR: gpu-bench full is a single-invocation command; run it once outside the Slurm task step.",
              file=sys.stderr)
        return 2
    if args.command == "info":
        info = collect_system_info()
        print(json.dumps(info, indent=2, sort_keys=True))
        return 0
    if args.command == "health":
        result = health_checks()
        for row in result["checks"]:
            print(f"{row['status']}: {row['name']} — {row['detail']}")
        return 1 if result["status"] == "FAIL" else 0
    if args.command == "full":
        return _run_full(args)
    if args.command == "report":
        source = Path(args.input)
        if not source.is_file():
            print(f"ERROR: benchmark input not found: {source}", file=sys.stderr)
            return 2
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
            paths = reports.write_report(data, args.output, args.compare)
        except (OSError, ValueError, TypeError) as exc:
            print(f"ERROR: cannot generate report: {exc}", file=sys.stderr)
            return 2
        _print_paths(paths)
        if data.get("comparison"):
            for row in data["comparison"]["differences"]:
                print(f"PARAMETER DIFFERENCE: {row['parameter']}: {row['first']} → {row['second']}")
        return 0

    info = collect_system_info()
    if args.command == "llm":
        if not _tool_enabled("llm"):
            print("ERROR: LLM benchmark tools were not selected when this image was built.", file=sys.stderr)
            return 2
        params = {"model": args.model, "input_tokens": args.input_tokens, "output_tokens": args.output_tokens,
                  "concurrency": args.concurrency, "requests": args.requests,
                  "warmup_requests": args.warmup_requests, "precision": args.precision,
                  "tensor_parallel_size": args.tensor_parallel_size}
        entries = _entry("llm", lambda: llm.benchmark(args.model, args.input_tokens, args.output_tokens,
                                                      args.concurrency, args.requests, args.warmup_requests,
                                                      args.tensor_parallel_size, args.precision), params)
    elif args.command == "compute":
        if not _tool_enabled("compute"):
            print("ERROR: Compute benchmark tools were not selected when this image was built.", file=sys.stderr)
            return 2
        params = {"precision": args.precision, "duration_seconds": args.duration, "matrix_size": args.size}
        entries = _entry("compute", lambda: benchmarks.compute(args.precision, args.duration, args.size), params)
    elif args.command == "memory":
        if not _tool_enabled("memory"):
            print("ERROR: Memory benchmark tools were not selected when this image was built.", file=sys.stderr)
            return 2
        params, entries = {}, _entry("memory", benchmarks.memory)
    elif args.command == "nccl":
        if not _tool_enabled("nccl"):
            print("ERROR: NCCL tools were not selected when this image was built.", file=sys.stderr)
            return 2
        params = {"test": args.test, "gpus": args.gpus, "min_bytes": args.min_bytes,
                  "max_bytes": args.max_bytes, "iterations": args.iterations, "hosts": args.hosts,
                  "hostfile": args.hostfile, "ranks": args.ranks}
        entries = _entry("nccl", lambda: benchmarks.nccl(args.test, args.min_bytes, args.max_bytes,
                                                         args.iterations, args.gpus, args.hosts,
                                                         args.hostfile, args.ranks), params)
    else:
        return 2
    paths = _write(args.output, info, entries, {**params, "precision": params.get("precision")})
    for entry in entries:
        print(f"{entry['status']}: {entry['test']}" + (f" — {entry['error']}" if entry.get("error") else ""))
        if entry.get("stdout"):
            print(entry["stdout"])
    _print_paths(paths)
    return 1 if any(entry["status"] == "FAILED" for entry in entries) else 0


if __name__ == "__main__":
    raise SystemExit(main())
