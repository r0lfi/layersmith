"""gpu-bench: one command line for every benchmark; the TUI drives the same code.

Each benchmark command creates its own run directory under the results root
(default /results), so earlier results are never overwritten. --run-dir adds
to an existing run instead (the TUI's full benchmark and Slurm jobs use it).
With --events the command prints JSON events on stdout for the TUI.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path

from . import benchmarks, distributed, llm, reports, runs
from .events import EMITTER, StopRequested
from .system import collect_system_info, health_checks, recipe

TOOL_NAMES = ("llm", "compute", "memory", "nccl")
LLM_DEFAULTS = {"input_tokens": 1024, "output_tokens": 256, "concurrency": "1,8,32", "requests": 64,
                "single_requests": 10, "warmup_requests": 4}


def tool_enabled(name: str) -> bool:
    configured = os.environ.get("GPU_BENCH_TOOLS")
    if configured is None:
        tools = recipe().get("tools")
        if not tools:
            return True
        configured = ",".join(tools)
    return name in {value.strip() for value in configured.split(",") if value.strip()}


def _rank() -> int:
    # torchrun's RANK first: under Slurm every local rank of node 0 has SLURM_PROCID=0.
    for name in ("RANK", "OMPI_COMM_WORLD_RANK", "PMIX_RANK", "SLURM_PROCID"):
        value = os.environ.get(name)
        if value and value.isdigit():
            return int(value)
    return 0


# ---------------------------------------------------------------- parser

def _common(parser: argparse.ArgumentParser, gpus: bool = True) -> None:
    parser.add_argument("--output", default=runs.DEFAULT_RESULTS,
                        help="results root; each run gets its own directory (default: %(default)s)")
    parser.add_argument("--run-dir", help="add results to this existing run directory instead of creating one")
    parser.add_argument("--label", help="short label appended to the run directory name")
    parser.add_argument("--events", action="store_true", help=argparse.SUPPRESS)
    if gpus:
        parser.add_argument("--gpus", default="all", help="GPU indices, e.g. 0,1 (default: all visible)")


def _llm_args(parser: argparse.ArgumentParser, required: bool) -> None:
    parser.add_argument("--model", required=required,
                        help="local model directory, e.g. /models/llama-3.1-8b (never downloaded)")
    parser.add_argument("--input-tokens", type=int, default=LLM_DEFAULTS["input_tokens"])
    parser.add_argument("--output-tokens", type=int, default=LLM_DEFAULTS["output_tokens"])
    parser.add_argument("--concurrency", default=LLM_DEFAULTS["concurrency"],
                        help="comma-separated levels; 1 measures single-request speed (default: %(default)s)")
    parser.add_argument("--requests", type=int, default=LLM_DEFAULTS["requests"],
                        help="measured requests per concurrent level (default: %(default)s)")
    parser.add_argument("--single-requests", type=int, default=LLM_DEFAULTS["single_requests"],
                        help="measured requests at concurrency 1 (default: %(default)s)")
    parser.add_argument("--warmup-requests", type=int, default=LLM_DEFAULTS["warmup_requests"],
                        help="unmeasured requests before each level (at least the concurrency, max 64)")
    parser.add_argument("--precision", choices=sorted(set(llm.PRECISIONS)), default="auto",
                        help="model dtype for vLLM (default: the model's own)")
    parser.add_argument("--tensor-parallel-size", type=int, help="default: number of selected GPUs")
    parser.add_argument("--max-model-len", type=int, help="default: input + output rounded up")
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.90)
    parser.add_argument("--trust-remote-code", action="store_true",
                        help="allow Python code shipped inside the model directory (off by default)")
    parser.add_argument("--server-timeout", type=int, default=1800, help="seconds to wait for the model to load")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gpu-bench", description="NVIDIA GPU & LLM benchmark suite (LayerSmith). "
                                      "Run without arguments in a terminal for the interactive dashboard.")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("tui", help="interactive terminal dashboard (default when started with a terminal)")
    p.add_argument("--output", default=runs.DEFAULT_RESULTS, help="results root (default: %(default)s)")
    p.add_argument("--models", default=llm.MODEL_ROOT, help="model root (default: %(default)s)")

    p = sub.add_parser("info", help="GPU, driver, topology, RDMA and host information")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("health", help="check GPU access, driver/CUDA compatibility and mounts")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("topology", help="GPU interconnect matrix (NVLink/PCIe) and P2P support")
    p = sub.add_parser("models", help="list local models under /models")
    p.add_argument("--models", default=llm.MODEL_ROOT)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("llm", help="LLM token speed and latency with vLLM on local models")
    _llm_args(p, required=True)
    _common(p)

    p = sub.add_parser("compute", help="GEMM TFLOPS per precision (CUDA events)")
    p.add_argument("--precision", default=",".join(benchmarks.DEFAULT_PRECISIONS),
                   help=f"comma-separated from {','.join(benchmarks.PRECISIONS)} or 'all' (default: %(default)s)")
    p.add_argument("--duration", type=float, default=10, help="measured seconds per precision (default: %(default)s)")
    p.add_argument("--size", type=int, default=8192, help="square matrix dimension (default: %(default)s)")
    _common(p)

    p = sub.add_parser("memory", help="VRAM/HBM, host<->device (PCIe) and GPU<->GPU bandwidth")
    p.add_argument("--size-mb", type=int, help="buffer size per test (default: 20%% of free VRAM, max 2 GiB)")
    p.add_argument("--no-peer", action="store_true", help="skip the GPU-to-GPU matrix")
    _common(p)

    p = sub.add_parser("nccl", help="nccl-tests on this node, or across nodes with MPI/Slurm")
    p.add_argument("--test", default="all", help=f"comma-separated from {','.join(benchmarks.NCCL_TESTS)} or all")
    p.add_argument("--min-bytes", default="8")
    p.add_argument("--max-bytes", default="1G")
    p.add_argument("--iterations", type=int, default=20)
    p.add_argument("--hosts", help="OpenMPI host list for multi-node, e.g. node1:8,node2:8")
    p.add_argument("--hostfile", help="OpenMPI hostfile for multi-node")
    p.add_argument("--ranks", type=int, help="MPI world size (multi-node)")
    p.add_argument("--gpus-per-rank", type=int, help="GPUs per MPI rank (multi-node, default 1)")
    p.add_argument("--mpi-args", help="extra mpirun arguments, quoted")
    _common(p)

    p = sub.add_parser("dist", help="multi-node NCCL collectives via torchrun (no MPI/SSH needed)")
    p.add_argument("--test", default="all", help=f"comma-separated from {','.join(distributed.COLLECTIVES)} or all")
    p.add_argument("--min-bytes", default="1M")
    p.add_argument("--max-bytes", default="1G")
    p.add_argument("--iterations", type=int, default=20)
    _common(p, gpus=False)

    p = sub.add_parser("full", help="health, compute, memory, NCCL and (with --model) LLM in one run")
    _llm_args(p, required=False)
    p.add_argument("--compute-duration", type=float, default=10)
    p.add_argument("--compute-precision", default=",".join(benchmarks.DEFAULT_PRECISIONS))
    p.add_argument("--skip", default="", help="comma-separated tests to skip: compute,memory,nccl,llm")
    _common(p)

    p = sub.add_parser("report", help="regenerate a run's HTML/JSON/CSV report")
    p.add_argument("run", nargs="?", default="latest", help="run id, run directory or 'latest'")
    p.add_argument("--output", default=runs.DEFAULT_RESULTS, help="results root")
    p.add_argument("--compare", nargs="+", help="also write a comparison against these runs")

    p = sub.add_parser("compare", help="compare two or more runs side by side")
    p.add_argument("runs", nargs="+", help="run ids or directories (the first is the baseline)")
    p.add_argument("--output", default=runs.DEFAULT_RESULTS, help="results root")
    p.add_argument("--to", help="output directory (default: <results>/comparisons/<time>)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("runs", help="list previous runs")
    p.add_argument("--output", default=runs.DEFAULT_RESULTS, help="results root")
    p.add_argument("--json", action="store_true")
    return parser


# ----------------------------------------------------------------- runs

class Session:
    """One run directory; results are saved as each one arrives."""

    def __init__(self, args, command: str, configuration: dict):
        self.write = _rank() == 0
        self.info = collect_system_info()
        self.dir: Path | None = None
        self.paths: dict = {}
        if not self.write:
            EMITTER.configure(json_mode=args.events)
            return
        if args.run_dir:
            self.dir = Path(args.run_dir)
            try:
                self.data = runs.load(self.dir)
            except runs.RunError:
                self.dir.mkdir(parents=True, exist_ok=True)
                (self.dir / "logs").mkdir(exist_ok=True)
                self.data = runs.empty_run(self.dir, self.info)
        else:
            self.dir = runs.new_run(args.output, self.info, args.label)
            self.data = runs.empty_run(self.dir, self.info)
        self.data["system_info"] = self.info
        self.data.setdefault("configuration", {})[command] = configuration
        self.data["tool"] = {"name": "gpu-bench", "image": recipe(), "command": " ".join(sys.argv[1:])}
        EMITTER.configure(json_mode=args.events, log_path=self.dir / "logs" / f"{command}.log")
        EMITTER.run_started(self.dir, self.dir.name)
        self._existing = list(self.data.get("results", []))

    def add(self, entry: dict) -> None:
        if not any(existing is entry for existing in EMITTER.collected):
            EMITTER.result(entry)
        self.save()

    def save(self) -> dict:
        if not self.write or self.dir is None:
            return {}
        self.data["results"] = self._existing + list(EMITTER.collected)
        self.paths = reports.write_run(self.data, self.dir)
        return self.paths


def _status_entry(test: str, status: str, reason: str, name: str | None = None) -> dict:
    return {"test": test, "name": name or test, "status": status, "parameters": {}, "results": {}, "error": reason}


def _guard(session: Session, test: str, call) -> bool:
    """Run one benchmark; turn every failure mode into a recorded result. False = stop."""
    before = len(EMITTER.collected)
    try:
        call()
    except StopRequested:
        if len(EMITTER.collected) == before:
            session.add(_status_entry(test, "STOPPED", "Stopped by user before a result was measured."))
        return False
    except benchmarks.Unavailable as exc:
        session.add(_status_entry(test, "UNAVAILABLE", str(exc)))
    except (benchmarks.BenchmarkError, runs.RunError) as exc:
        session.add(_status_entry(test, "FAILED", str(exc)))
    except Exception as exc:  # a crash in one test must not lose the others
        session.add(_status_entry(test, "FAILED", f"{type(exc).__name__}: {exc}"))
    finally:
        session.save()
    return True


def _print_summary(session: Session) -> None:
    if EMITTER.json_mode:
        return
    for entry in EMITTER.collected:
        line = f"{entry.get('status'):<11} {entry.get('name') or entry.get('test')}"
        if entry.get("error"):
            line += f" - {entry['error'].splitlines()[0][:200]}"
        print(line)
    metrics = reports.headline_metrics({"results": EMITTER.collected})
    if metrics:
        print()
        for metric in metrics:
            print(f"  {metric['label']:<58} {reports.fmt(metric['value']):>12} {metric['unit']}")
    if session.paths:
        print()
        for name, path in session.paths.items():
            print(f"{name}: {path}")


def _finish(session: Session) -> int:
    paths = session.save()
    _print_summary(session)
    statuses = [entry.get("status") for entry in EMITTER.collected]
    status = "STOPPED" if "STOPPED" in statuses else ("FAILED" if "FAILED" in statuses else "PASSED")
    EMITTER.done(status, paths)
    return {"PASSED": 0, "STOPPED": 130}.get(status, 1)


def _llm_kwargs(args) -> dict:
    return {"input_tokens": args.input_tokens, "output_tokens": args.output_tokens, "concurrency": args.concurrency,
            "requests": args.requests, "single_requests": args.single_requests,
            "warmup_requests": args.warmup_requests, "tensor_parallel_size": args.tensor_parallel_size,
            "precision": args.precision, "gpus": args.gpus, "max_model_len": args.max_model_len,
            "gpu_memory_utilization": args.gpu_memory_utilization, "trust_remote_code": args.trust_remote_code,
            "server_timeout": args.server_timeout}


def _require(name: str) -> None:
    if not tool_enabled(name):
        raise benchmarks.Unavailable(f"The {name} component was not selected when this image was built.")


def run_benchmark(args) -> int:
    configuration = {key: value for key, value in vars(args).items()
                     if key not in ("events", "output", "run_dir", "command")}
    session = Session(args, args.command, configuration)
    log_dir = session.dir / "logs" if session.dir else None
    if args.command == "llm":
        _guard(session, "llm", lambda: (_require("llm"), llm.benchmark(args.model, log_dir=log_dir, **_llm_kwargs(args))))
    elif args.command == "compute":
        _guard(session, "compute", lambda: (_require("compute"),
                                            benchmarks.compute(args.precision, args.duration, args.size, args.gpus)))
    elif args.command == "memory":
        _guard(session, "memory", lambda: (_require("memory"),
                                           benchmarks.memory(args.gpus, args.size_mb, not args.no_peer)))
    elif args.command == "nccl":
        _guard(session, "nccl", lambda: (_require("nccl"), benchmarks.nccl(
            args.test, args.min_bytes, args.max_bytes, args.iterations, args.gpus, args.hosts, args.hostfile,
            args.ranks, args.gpus_per_rank, args.mpi_args)))
    elif args.command == "dist":
        _guard(session, "nccl", lambda: (_require("nccl"), distributed.run(
            args.test, args.min_bytes, args.max_bytes, args.iterations)))
    elif args.command == "full":
        run_full(args, session, log_dir)
    return _finish(session)


def run_full(args, session: Session, log_dir) -> None:
    skip = {value.strip() for value in (args.skip or "").split(",") if value.strip()}
    health = health_checks(session.info)
    session.add({"test": "health", "name": "Health", "status": "PASSED" if health["status"] == "PASS" else "FAILED",
                 "parameters": {}, "results": health,
                 "error": None if health["status"] == "PASS" else "One or more required GPU checks failed."})
    gpu_ready = bool(session.info.get("gpus")) and bool(session.info.get("torch_cuda_available"))
    plan = [
        ("compute", lambda: benchmarks.compute(args.compute_precision, args.compute_duration, 8192, args.gpus)),
        ("memory", lambda: benchmarks.memory(args.gpus)),
        ("nccl", lambda: benchmarks.nccl("all", gpus=args.gpus)),
        ("llm", lambda: llm.benchmark(args.model, log_dir=log_dir, **_llm_kwargs(args))),
    ]
    for position, (name, call) in enumerate(plan):
        EMITTER.progress("full", step=name, index=position, steps=len(plan), summary=f"{name} ({position + 1}/{len(plan)})",
                         force=True)
        if EMITTER.stopping:
            session.add(_status_entry(name, "STOPPED", "Not started: the run was stopped."))
            continue
        if name in skip:
            session.add(_status_entry(name, "SKIPPED", "Skipped on request (--skip)."))
            continue
        if not tool_enabled(name):
            session.add(_status_entry(name, "UNAVAILABLE", f"The {name} component is not part of this image."))
            continue
        if not gpu_ready:
            session.add(_status_entry(name, "SKIPPED", "No usable NVIDIA GPU is visible to the container."))
            continue
        if name == "nccl" and session.info.get("gpu_count", 0) < 2 and args.gpus == "all":
            session.add(_status_entry(name, "SKIPPED", "NCCL needs at least two GPUs on this node; "
                                                       "use gpu-bench nccl with MPI/Slurm for multi-node."))
            continue
        if name == "llm" and not args.model:
            found = llm.discover_models()
            session.add(_status_entry(name, "SKIPPED", "No --model given. " + (
                f"Found {len(found)} local model(s), e.g. {found[0]['path']}." if found else
                "No local models under /models; mount them with -v /models:/models:ro.")))
            continue
        if not _guard(session, name, call):
            for rest, _ in plan[position + 1:]:
                session.add(_status_entry(rest, "STOPPED", "Not started: the run was stopped."))
            break


# --------------------------------------------------------------- simple

def _print_info(info: dict) -> None:
    print(f"Host       {info['hostname']}  ({(info.get('host') or {}).get('cpu_model') or 'CPU N/A'})")
    print(f"Driver     {info.get('driver_version') or 'N/A'}   CUDA (driver) {info.get('cuda_driver_version') or 'N/A'}"
          f"   CUDA (PyTorch) {info.get('torch_cuda_version') or 'N/A'}")
    print(f"PyTorch    {info.get('torch_version') or 'N/A'}   vLLM {(info.get('packages') or {}).get('vllm') or 'not installed'}"
          f"   NCCL {info.get('nccl_version') or 'N/A'}")
    gpus = info.get("gpus") or []
    print(f"GPUs       {len(gpus)}" + ("" if gpus else f"  ({info.get('nvml_error') or 'no NVIDIA GPU visible'})"))
    for gpu in gpus:
        link = gpu.get("pcie_link") or {}
        print(f"  [{gpu['index']}] {gpu['name']}  {reports.human_bytes(gpu.get('memory_total_bytes'))}  "
              f"CC {gpu.get('compute_capability') or 'N/A'}  "
              f"PCIe {('Gen' + str(link['generation']) + ' x' + str(link['width'])) if link.get('generation') else 'N/A'}  "
              f"NVLinks {len(gpu.get('nvlink') or []) or 'N/A'}  MIG {(gpu.get('mig_mode') or {}).get('current', 'N/A')}  "
              f"ECC {(gpu.get('ecc') or {}).get('mode', 'N/A')}  {gpu.get('temperature_c', 'N/A')}C  "
              f"{reports.fmt(gpu.get('power_watts'), 0)}W  {gpu.get('uuid') or ''}")
    for device in info.get("rdma_devices") or []:
        ports = "; ".join(f"port {p['port']} {p.get('state')} {p.get('rate')}" for p in device.get("ports") or [])
        print(f"RDMA       {device['name']} ({device.get('transport')}) {','.join(device.get('netdevs') or [])} {ports}")
    if not info.get("rdma_devices"):
        print("RDMA       N/A (no RDMA device exposed)")


def _print_topology(info: dict) -> None:
    if info.get("gpu_topology"):
        print(info["gpu_topology"])
    elif info.get("topology"):
        matrix = info["topology"]["matrix"]
        print("       " + " ".join(f"GPU{i:<4}" for i in range(len(matrix))))
        for i, row in enumerate(matrix):
            print(f"GPU{i:<4}" + " ".join(f"{c:<7}" for c in row))
        print("\n" + info["topology"]["legend"])
    else:
        print("Topology N/A: fewer than two GPUs visible.")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        argv = ["tui"] if sys.stdin.isatty() and sys.stdout.isatty() else ["--help"]
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0

    def request_stop(signum, _frame):
        if EMITTER.stop_event.is_set() and signum == signal.SIGINT:
            raise KeyboardInterrupt
        EMITTER.stop_event.set()
        print(f"\nStopping (signal {signum}); cleaning up child processes...", file=sys.stderr, flush=True)

    if args.command == "tui":
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("gpu-bench tui needs an interactive terminal: start the container with -it "
                  "(podman run --rm -it ...), or use the CLI commands (gpu-bench --help).", file=sys.stderr)
            return 2
        from .tui.app import run_tui

        return run_tui(results=args.output, models=args.models)
    if args.command in ("info", "topology"):
        info = collect_system_info()
        if args.command == "topology":
            _print_topology(info)
        elif args.json:
            print(json.dumps(info, indent=2, sort_keys=True, default=str))
        else:
            _print_info(info)
        return 0
    if args.command == "health":
        result = health_checks()
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            for row in result["checks"]:
                print(f"{row['status']:<5} {row['name']:<28} {row['detail']}")
        return 1 if result["status"] == "FAIL" else 0
    if args.command == "models":
        found = llm.discover_models(args.models)
        if args.json:
            print(json.dumps(found, indent=2))
        elif not found:
            print(f"No local models under {args.models}. Mount a directory of Hugging Face model folders "
                  "(config.json + *.safetensors) with -v /models:/models:ro.")
        for model in found:
            print(f"{model['path']:<50} {reports.human_bytes(model['weight_bytes']):>10}  "
                  f"{','.join(model['architectures']) or model.get('model_type') or '?'}  {model.get('dtype') or ''}"
                  f"{'  quant=' + model['quantization'] if model.get('quantization') else ''}")
        return 0
    if args.command == "runs":
        rows = runs.list_runs(args.output)
        if args.json:
            print(json.dumps(rows, indent=2))
        elif not rows:
            print(f"No runs under {args.output}.")
        for row in rows:
            best = row.get("best_output_tokens_per_second")
            print(f"{row['run_id']:<46} {row.get('gpu_model') or 'no GPU'} x{row['gpu_count']}  "
                  f"{','.join(row['tests'])}  passed {row['passed']} failed {row['failed']}"
                  + (f"  best {best:.0f} tok/s" if best else ""))
        return 0
    if args.command == "report":
        try:
            run_dir = runs.resolve(args.output, args.run)
            data = runs.load(run_dir)
            paths = reports.write_run(data, run_dir)
            if args.compare:
                others = [runs.load(runs.resolve(args.output, ref)) for ref in args.compare]
                paths.update({f"compare_{k}": v for k, v in reports.write_comparison(
                    reports.compare_runs([data, *others]), run_dir / "comparison").items()})
        except (runs.RunError, ValueError, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        for name, path in paths.items():
            print(f"{name}: {path}")
        return 0
    if args.command == "compare":
        try:
            loaded = [runs.load(runs.resolve(args.output, ref)) for ref in args.runs]
            comparison = reports.compare_runs(loaded)
            import datetime as dt

            target = Path(args.to) if args.to else Path(args.output) / "comparisons" / dt.datetime.now().strftime(
                "%Y-%m-%d_%H%M%S")
            paths = reports.write_comparison(comparison, target)
        except (runs.RunError, ValueError, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(comparison, indent=2, default=str))
            return 0
        for warning in comparison["warnings"]:
            print(f"WARNING: {warning}")
        labels = [run["label"] for run in comparison["runs"]]
        print(f"{'Metric':<58}" + "".join(f"{label[:20]:>22}" for label in labels))
        for row in comparison["rows"]:
            cells = []
            for value, diff in zip(row["values"], row["diff_percent"]):
                cells.append(f"{reports.fmt(value)}{f' ({diff:+.0f}%)' if diff is not None else ''}")
            print(f"{row['label'][:57]:<58}" + "".join(f"{cell:>22}" for cell in cells))
        for name, path in paths.items():
            print(f"{name}: {path}")
        return 0

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        return run_benchmark(args)
    except runs.RunError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        EMITTER.done("FAILED")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
