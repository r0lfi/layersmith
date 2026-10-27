"""Where each AI/ML test runs, and how its results are recorded.

  basic   LayerSmith runs it after every build, inside the new image, with
          networking disabled and every capability dropped. No GPU needed.
  gpu     Only meaningful on a GPU worker: nvidia-smi, torch.cuda, a matrix
          multiplication checked against the CPU. LayerSmith never runs it on
          the build server, whose GPU (or lack of one) says nothing about the
          worker's. Its report is pasted back and recorded as reported.
  slurm   The same GPU test started by a generated batch script: also proves
          GRES allocation, cgroup device isolation and Podman CDI access, and
          carries the job's exit code.

Results belong to one build: a report for another image is refused.
"""

import json
import re
from datetime import datetime, timezone

from layersmith.training.checks import RESULT_PREFIX, parse_output  # one result-line format for all checks

EXIT_PREFIX = "LAYERSMITH_SLURM_EXIT "
PREREQ_PREFIX = "LAYERSMITH_PREREQ_MISSING"

AUTOMATIC = [{"id": "basic", "timeout": 20 * 60}]

TESTS = {
    "basic": {"label": "Basic test", "where": "build server",
              "what": "Python version, PyTorch/TensorFlow import, CUDA version, compiled GPU architectures, "
                      "installed libraries and a pip dependency check, with networking disabled."},
    "gpu": {"label": "GPU test", "where": "GPU worker",
            "what": "nvidia-smi, torch.cuda.is_available(), GPU model and memory, and a matrix multiplication "
                    "on every GPU checked against the CPU result."},
    "slurm": {"label": "Slurm test", "where": "GPU worker, through Slurm",
              "what": "The GPU test as a batch job: GRES allocation, cgroup device isolation, Podman CDI access "
                      "to exactly the allocated GPUs, the test result and the job's exit code."},
}

__all__ = ["RESULT_PREFIX", "parse_output", "summarise", "validate_report", "InvalidReport"]


class InvalidReport(ValueError):
    pass


def summarise(build_status: str, recorded: dict | None) -> list[dict]:
    results = (recorded or {}).get("results") or {}
    rows = []
    for test_id, test in TESTS.items():
        result = results.get(test_id)
        if result:
            status = result.get("status") if result.get("status") in ("passed", "failed") else "failed"
        else:
            status = "not_run"
        if test_id == "basic" and not result:
            summary = "Running…" if build_status == "testing" else "Not run for this build."
        elif not result:
            summary = ("Not run yet. It needs a GPU worker: run the command or batch script below there and "
                       "paste its output here.")
        else:
            steps = result.get("steps") or []
            failed = [s for s in steps if s.get("status") == "failed"]
            summary = ("; ".join(f"{s['name']}: {s.get('detail', '')[:160]}" for s in failed[:3]) if failed
                       else ", ".join(s["name"] for s in steps if s.get("status") == "passed"))
            if result.get("source") == "reported":
                summary += f" — reported from {result.get('host') or 'the worker'} on {result.get('finished', '?')}"
        rows.append({"id": test_id, **test, "status": status, "summary": summary,
                     "steps": (result or {}).get("steps", []), "info": _public_info((result or {}).get("info")),
                     "nodes": (result or {}).get("nodes"), "exit_code": (result or {}).get("exit_code")})
    return rows


def _public_info(info: dict | None) -> dict:
    """Everything but the full package inventory, which has its own downloads."""
    return {k: v for k, v in (info or {}).items() if k != "packages"}


def _clean_result(report: dict) -> dict:
    steps = report.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > 50:
        raise InvalidReport("The report has no steps")
    info = report.get("info") if isinstance(report.get("info"), dict) else {}
    clean_info = {}
    for key in ("python", "torch", "torch_cuda", "tensorflow", "driver"):
        if info.get(key):
            clean_info[key] = str(info[key])[:60]
    for key in ("devices", "torch_devices", "matmul"):
        if isinstance(info.get(key), list):
            clean_info[key] = [{k: (v if isinstance(v, (int, float, bool)) or v is None else str(v)[:80])
                                for k, v in item.items()} for item in info[key][:16] if isinstance(item, dict)]
    if isinstance(info.get("compiled_archs"), list):
        clean_info["compiled_archs"] = [str(a)[:20] for a in info["compiled_archs"][:40]]
    if isinstance(info.get("slurm"), dict):
        clean_info["slurm"] = {str(k)[:40]: str(v)[:300] for k, v in list(info["slurm"].items())[:20]}
    return {"check": report.get("check"), "status": report.get("status"),
            "host": str(report.get("host") or "")[:80], "finished": str(report.get("finished") or "")[:40],
            "steps": [{"name": str(s.get("name", ""))[:120], "status": str(s.get("status", ""))[:10],
                       "detail": str(s.get("detail", ""))[:600], "seconds": s.get("seconds")}
                      for s in steps if isinstance(s, dict)],
            "info": clean_info}


def _result_lines(text: str) -> list[dict]:
    found = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(RESULT_PREFIX):
            try:
                found.append(json.loads(line[len(RESULT_PREFIX):]))
            except ValueError:
                continue
    return found


def validate_report(report, image_ref: str) -> dict:
    """Accept the output of a GPU test or Slurm job for exactly this image.

    A Slurm job's output can hold one result per node, prerequisite failures
    and the exit code of each node's container; all of it is recorded.
    """
    if isinstance(report, dict):
        text = RESULT_PREFIX + json.dumps(report)
    elif isinstance(report, str):
        text = report[:2_000_000]
    else:
        raise InvalidReport("Paste the output of the GPU test or the Slurm job")
    results = _result_lines(text)
    if not results and text.strip().startswith("{"):
        try:
            results = [json.loads(text)]
        except ValueError:
            pass
    exits = []
    for match in re.finditer(re.escape(EXIT_PREFIX) + r"(\{.*\})", text):
        try:
            exits.append(json.loads(match.group(1)))
        except ValueError:
            continue
    prereqs = [line.strip()[:400] for line in text.splitlines() if PREREQ_PREFIX in line][:20]
    if not results and not prereqs:
        raise InvalidReport("No LayerSmith test result found. Paste the whole output of "
                            "`layersmith-aiml-check gpu` or of the Slurm job (its .out and .err files).")
    for result in results:
        if result.get("check") not in ("gpu", "slurm"):
            raise InvalidReport(f"This is a {result.get('check') or 'unknown'} result; only GPU and Slurm test "
                                "reports are recorded here (the basic test is run by LayerSmith itself).")
        if result.get("image") != image_ref:
            raise InvalidReport(f"The report is from {result.get('image') or 'an unnamed image'}, not {image_ref}. "
                                "Results are recorded only for the image they ran in.")
        if result.get("status") not in ("passed", "failed"):
            raise InvalidReport("The report has no passed/failed status")

    slurm = bool(exits or prereqs or any(r.get("check") == "slurm" for r in results))
    nodes = [_clean_result(r) for r in results]
    exit_codes = [e.get("exit_code") for e in exits if isinstance(e.get("exit_code"), int)]
    failed = (any(n["status"] != "passed" for n in nodes) or bool(prereqs)
              or any(code != 0 for code in exit_codes) or not nodes)
    combined = {
        "check": "slurm" if slurm else "gpu",
        "status": "failed" if failed else "passed",
        "image": image_ref,
        "host": ", ".join(n["host"] for n in nodes if n["host"])[:200],
        "finished": max((n["finished"] for n in nodes), default=""),
        "steps": ([{"name": "prerequisites", "status": "failed", "detail": "; ".join(prereqs)[:600]}] if prereqs
                  else []) + [{**s, "name": f"{n['host']}: {s['name']}" if len(nodes) > 1 else s["name"]}
                              for n in nodes for s in n["steps"]],
        "info": nodes[0]["info"] if len(nodes) == 1 else {"nodes": len(nodes)},
        "nodes": [{"host": n["host"], "status": n["status"], "info": n["info"]} for n in nodes] if slurm else None,
        "exit_code": max(exit_codes, key=abs) if exit_codes else None,
        "exits": [{k: str(v)[:200] if not isinstance(v, int) else v for k, v in e.items()} for e in exits[:64]],
        "source": "reported",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    if slurm and not exits:
        combined["steps"].append({"name": "exit code", "status": "warning",
                                  "detail": "No LAYERSMITH_SLURM_EXIT line: paste the job's whole output"})
    return combined
