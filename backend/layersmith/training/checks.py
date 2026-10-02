"""What is verified about a training image, and how results are recorded.

After a successful build LayerSmith runs three checks inside the new image,
with networking disabled: `deps`, `cpu` and `offline` (see
assets/common/checks/run.py). It never runs the GPU check: the build host's
GPU, or lack of one, says nothing about the machine that will train. That
result can be reported back from the target machine and is recorded as
reported, with the hardware it ran on.

Results belong to one build. A later build - a different version, or the same
profile with extra packages - starts with no results of its own.
"""

import json
from datetime import datetime, timezone

RESULT_PREFIX = "LAYERSMITH_CHECK_RESULT "

#: Run by LayerSmith after the build, in this order.
AUTOMATIC = [
    {"id": "deps", "argv": ["layersmith-check", "deps"], "timeout": 15 * 60},
    {"id": "cpu", "argv": ["layersmith-check", "cpu"], "timeout": 30 * 60},
    {"id": "offline", "argv": ["layersmith-check", "offline"], "timeout": 45 * 60},
]

LABELS = {
    "image": {"passed": "Image built", "failed": "Image build failed", "not_run": "Image not built"},
    "deps": {"passed": "Dependencies checked", "failed": "Dependency check failed",
             "not_run": "Dependencies not checked"},
    "cpu": {"passed": "CPU smoke test passed", "failed": "CPU smoke test failed", "not_run": "CPU smoke test not run"},
    "gpu": {"passed": "GPU training test passed", "failed": "GPU test failed", "not_run": "GPU test not run"},
    "offline": {"passed": "Offline example verified", "failed": "Offline example failed",
                "not_run": "Offline example not verified"},
}

MEANING = {
    "image": "The container build completed.",
    "deps": "Every locked package is installed at its locked version, pip finds no broken requirements, and the "
            "profile's libraries import.",
    "cpu": "A small local function test ran on CPU with a tiny, locally created model and synthetic data: "
           "forward pass, backward pass, optimizer step, and saving and loading.",
    "gpu": "An actual small training run on a GPU, with the profile's training method.",
    "offline": "The bundled example ran inside the image without network (networking disabled, "
               "HF_HUB_OFFLINE=1) using only local resources.",
}


def parse_output(text: str) -> dict | None:
    """The result line a check prints last, or None."""
    for line in reversed(text.splitlines()):
        if line.startswith(RESULT_PREFIX):
            try:
                return json.loads(line[len(RESULT_PREFIX):])
            except ValueError:
                return None
    return None


def gpu_command(recipe: dict, image: str) -> dict:
    return {
        "docker": f"docker run --rm --gpus all --shm-size=16g {image} layersmith-check gpu --json",
        "podman": f"podman run --rm --device nvidia.com/gpu=all --security-opt=label=disable --shm-size=16g "
                  f"{image} layersmith-check gpu --json",
    }


def _step_summary(result: dict) -> str:
    steps = result.get("steps") or []
    failed = [s for s in steps if s.get("status") == "failed"]
    if failed:
        return f"{len(failed)} of {len(steps)} steps failed: " + "; ".join(
            f"{s['name']} ({s.get('detail', '')[:160]})" for s in failed[:3])
    return ", ".join(s["name"] for s in steps if s.get("status") == "passed")


def summarise(build_status: str, recipe: dict, recorded: dict | None, image: str | None,
              customized: bool) -> list[dict]:
    """The five statuses shown for a build, in a fixed order."""
    recorded = recorded or {}
    results = recorded.get("results") or {}
    rows = []

    # The image exists from the moment the checks start, before it is exported.
    built = build_status in ("testing", "exporting", "ready") or bool(results)
    image_status = "passed" if built else "failed" if build_status == "failed" else "not_run"
    rows.append({"id": "image", "status": image_status, "label": LABELS["image"][image_status],
                 "meaning": MEANING["image"], "summary": MEANING["image"] if image_status == "passed" else ""})

    for check in ("deps", "cpu", "offline"):
        result = results.get(check)
        status = result.get("status", "failed") if result else "not_run"
        if result and status not in ("passed", "failed"):
            status = "failed"  # a result without a clear verdict is not a pass
        summary = _step_summary(result) if result else (
            "Running…" if build_status == "testing" else "Not run for this build.")
        rows.append({"id": check, "status": status, "label": LABELS[check][status], "meaning": MEANING[check],
                     "summary": summary, "steps": (result or {}).get("steps", []),
                     "info": (result or {}).get("info", {}), "finished": (result or {}).get("finished")})

    gpu = results.get("gpu")
    if recipe["gpu"]:
        if gpu:
            devices = ", ".join(d.get("name", "?") for d in (gpu.get("info") or {}).get("devices", [])) or "unknown GPU"
            driver = (gpu.get("info") or {}).get("driver")
            summary = (f"{_step_summary(gpu)}. Reported from the target machine ({devices}"
                       f"{', driver ' + driver if driver else ''}) on {gpu.get('finished', '?')}; "
                       "LayerSmith did not run this test itself.")
            status = gpu.get("status") if gpu.get("status") in ("passed", "failed") else "failed"
        else:
            status, summary = "not_run", ("No actual GPU test has been performed. Run the command below on the "
                                          "machine that will train, and record its report here.")
        rows.append({"id": "gpu", "status": status, "label": LABELS["gpu"][status], "meaning": MEANING["gpu"],
                     "summary": summary, "steps": (gpu or {}).get("steps", []), "info": (gpu or {}).get("info", {}),
                     "finished": (gpu or {}).get("finished"),
                     "command": gpu_command(recipe, image) if image else None})
    # Fixed order for the dashboard: build, packages, CPU, GPU, offline.
    order = {"image": 0, "deps": 1, "cpu": 2, "gpu": 3, "offline": 4}
    rows.sort(key=lambda row: order[row["id"]])
    for row in rows:
        row["applies_to"] = "this build only" + (" (modified recipe)" if customized else "")
    return rows


class InvalidReport(ValueError):
    pass


def validate_gpu_report(report, image_ref: str) -> dict:
    """Accept a `layersmith-check gpu --json` report for exactly this image."""
    if isinstance(report, str):
        text = report.strip()
        parsed = parse_output(text)
        if parsed is None:
            try:
                parsed = json.loads(text[text.index("{"):]) if "{" in text else None
            except ValueError:
                parsed = None
        report = parsed
    if not isinstance(report, dict) or report.get("check") != "gpu":
        raise InvalidReport("Not a GPU check report. Paste the output of `layersmith-check gpu --json`.")
    if report.get("image") != image_ref:
        raise InvalidReport(f"The report is from {report.get('image') or 'an unknown image'}, not {image_ref}. "
                            "GPU results are recorded only for the image they ran in.")
    if report.get("status") not in ("passed", "failed"):
        raise InvalidReport("The report has no passed/failed status")
    steps = report.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > 50:
        raise InvalidReport("The report has no steps")
    clean_steps = [{"name": str(s.get("name", ""))[:120], "status": str(s.get("status", ""))[:10],
                    "detail": str(s.get("detail", ""))[:600], "seconds": s.get("seconds")}
                   for s in steps if isinstance(s, dict)]
    info = report.get("info") if isinstance(report.get("info"), dict) else {}
    devices = [{k: d.get(k) for k in ("index", "name", "capability", "memory_gb")}
               for d in (info.get("devices") or [])[:16] if isinstance(d, dict)]
    return {"check": "gpu", "status": report["status"], "steps": clean_steps,
            "info": {"devices": devices, "driver": str(info.get("driver") or "")[:40] or None,
                     "torch": str(info.get("torch") or "")[:40], "torch_cuda": str(info.get("torch_cuda") or "")[:20]},
            "image": image_ref, "finished": str(report.get("finished") or "")[:40],
            "source": "reported", "recorded_at": datetime.now(timezone.utc).isoformat()}
