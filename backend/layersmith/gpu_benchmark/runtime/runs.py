"""One directory per benchmark run, so no run ever overwrites another.

    /results/
      2026-10-15_120000_h200x2/
        benchmark.json  benchmark.csv  benchmark.html  system-info.json
        logs/
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

SCHEMA_VERSION = 2
DEFAULT_RESULTS = os.environ.get("GPU_BENCH_RESULTS", "/results")
RUN_FILE = "benchmark.json"


class RunError(ValueError):
    pass


def gpu_slug(info: dict) -> str:
    gpus = info.get("gpus") or []
    if not gpus:
        return "nogpu"
    name = str(gpus[0].get("name") or "gpu")
    name = re.sub(r"(?i)\bnvidia\b", "", name)
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32].strip("-") or "gpu"
    return f"{slug}x{len(gpus)}" if len(gpus) > 1 else slug


def new_run(root: str | Path, info: dict, label: str | None = None) -> Path:
    root = Path(root)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RunError(f"Cannot create the results directory {root}: {exc}. "
                       "Mount a writable host directory, for example -v /opt/gpu-results:/results:Z") from exc
    if not os.access(root, os.W_OK):
        raise RunError(f"The results directory {root} is not writable. Check the volume mount and SELinux label.")
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    suffix = gpu_slug(info)
    if label:
        suffix += "_" + re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-")[:40]
    base = f"{stamp}_{suffix}"
    candidate, number = root / base, 2
    while True:
        try:
            candidate.mkdir()
            break
        except FileExistsError:
            candidate = root / f"{base}-{number}"
            number += 1
    (candidate / "logs").mkdir()
    return candidate


def empty_run(run_dir: Path, info: dict, configuration: dict | None = None, tool: dict | None = None) -> dict:
    return {"schema_version": SCHEMA_VERSION, "run_id": run_dir.name,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "tool": tool or {}, "system_info": info, "configuration": configuration or {}, "results": []}


def load(path: str | Path) -> dict:
    path = Path(path)
    if path.is_dir():
        path = path / RUN_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RunError(f"No benchmark result at {path}") from exc
    except (OSError, ValueError) as exc:
        raise RunError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RunError(f"{path} is not a benchmark result")
    data["run_id"] = path.parent.name  # the folder is the identity, even if renamed
    data.setdefault("results", [])
    data.setdefault("system_info", {})
    data["_path"] = str(path.parent)
    return data


def resolve(root: str | Path, reference: str) -> Path:
    """A run directory from a path, a run id, or 'latest'."""
    candidate = Path(reference)
    if candidate.is_file() and candidate.name.endswith(".json"):
        return candidate.parent
    if candidate.is_dir() and (candidate / RUN_FILE).is_file():
        return candidate
    runs = list_runs(root)
    if reference == "latest":
        if not runs:
            raise RunError(f"No previous runs under {root}")
        return Path(runs[0]["path"])
    for run in runs:
        if run["run_id"] == reference:
            return Path(run["path"])
    matches = [run for run in runs if run["run_id"].startswith(reference)]
    if len(matches) == 1:
        return Path(matches[0]["path"])
    raise RunError(f"Unknown run '{reference}' under {root}")


def summary(data: dict) -> dict:
    info = data.get("system_info") or {}
    gpus = info.get("gpus") or []
    statuses = [entry.get("status") for entry in data.get("results", [])]
    models = sorted({str((entry.get("parameters") or {}).get("model")) for entry in data.get("results", [])
                     if entry.get("test") == "llm" and (entry.get("parameters") or {}).get("model")})
    headline = None
    for entry in data.get("results", []):
        value = (entry.get("results") or {}).get("aggregate_output_tokens_per_second")
        if entry.get("test") == "llm" and entry.get("status") == "PASSED" and isinstance(value, (int, float)):
            headline = max(headline or 0, value)
    return {
        "run_id": data.get("run_id"), "path": data.get("_path"), "created_at": data.get("created_at")
        or info.get("timestamp"), "hostname": info.get("hostname"),
        "gpu_model": gpus[0].get("name") if gpus else None, "gpu_count": len(gpus),
        "tests": sorted({entry.get("test") for entry in data.get("results", []) if entry.get("test")}),
        "passed": statuses.count("PASSED"), "failed": statuses.count("FAILED"),
        "skipped": sum(1 for s in statuses if s in ("SKIPPED", "UNAVAILABLE")),
        "stopped": statuses.count("STOPPED"), "models": models,
        "best_output_tokens_per_second": headline,
    }


def list_runs(root: str | Path) -> list[dict]:
    root = Path(root)
    if not root.is_dir():
        return []
    rows = []
    for child in root.iterdir():
        if not (child / RUN_FILE).is_file():
            continue
        try:
            rows.append(summary(load(child)))
        except RunError:
            continue
    rows.sort(key=lambda row: (row.get("created_at") or "", row["run_id"]), reverse=True)
    return rows
