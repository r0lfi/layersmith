"""benchmark.json / .csv / .html / system-info.json, and run comparison.

Everything is self-contained: the HTML has inline CSS and SVG charts, no
scripts, fonts or CDNs, so it opens offline straight from the results folder.
"""

from __future__ import annotations

import csv
import datetime as dt
import html
import json
import math
from pathlib import Path

PALETTE = ("#22d3ee", "#a78bfa", "#fbbf24", "#34d399", "#f472b6", "#60a5fa", "#fb923c", "#a3e635")


# ------------------------------------------------------------ metrics

def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def headline_metrics(data: dict) -> list[dict]:
    """The comparable figures of one run, each with unit and direction."""
    rows = []

    def add(key, group, label, unit, better, value, context=None):
        value = _num(value)
        if value is not None:
            rows.append({"key": key, "group": group, "label": label, "unit": unit, "better": better,
                         "value": value, "context": context or {}})

    for entry in data.get("results", []):
        if entry.get("status") != "PASSED":
            continue
        test, params, res = entry.get("test"), entry.get("parameters") or {}, entry.get("results") or {}
        if test == "llm":
            c = params.get("concurrency")
            context = {k: params.get(k) for k in ("model_name", "precision", "input_tokens", "output_tokens",
                                                  "concurrency", "gpu_count", "tensor_parallel_size", "requests")}
            mode = "single request" if c == 1 else f"concurrency {c}"
            add(f"llm.c{c}.aggregate", "LLM", f"Aggregate output tokens/s ({mode})", "tok/s", "higher",
                res.get("aggregate_output_tokens_per_second"), context)
            add(f"llm.c{c}.per_request", "LLM", f"Per-request output tokens/s, median ({mode})", "tok/s", "higher",
                (res.get("per_request_output_tokens_per_second") or {}).get("p50"), context)
            add(f"llm.c{c}.ttft_p50", "LLM", f"TTFT P50 ({mode})", "ms", "lower", (res.get("ttft_ms") or {}).get("p50"), context)
            add(f"llm.c{c}.ttft_p95", "LLM", f"TTFT P95 ({mode})", "ms", "lower", (res.get("ttft_ms") or {}).get("p95"), context)
            add(f"llm.c{c}.tpot_p50", "LLM", f"TPOT P50 ({mode})", "ms", "lower", (res.get("tpot_ms") or {}).get("p50"), context)
            add(f"llm.c{c}.e2e_p95", "LLM", f"End-to-end latency P95 ({mode})", "ms", "lower",
                (res.get("e2e_latency_ms") or {}).get("p95"), context)
            if c == 1:
                add("llm.prefill", "LLM", "Prefill tokens/s estimate (single request)", "tok/s", "higher",
                    res.get("prefill_tokens_per_second_estimate"), context)
        elif test == "compute":
            p = (params.get("precision") or res.get("precision") or "?").upper()
            context = {k: params.get(k) for k in ("precision", "matrix_size", "duration_seconds", "gpu_count")}
            add(f"compute.{p}.total", "Compute", f"{p} GEMM TFLOPS, all GPUs", "TFLOPS", "higher",
                res.get("measured_tflops_total"), context)
            add(f"compute.{p}.per_gpu", "Compute", f"{p} GEMM TFLOPS per GPU (mean)", "TFLOPS", "higher",
                res.get("measured_tflops_per_gpu_mean"), context)
        elif test == "memory":
            context = {k: params.get(k) for k in ("gpu_count", "size_mb")}
            add("memory.vram_copy", "Memory", "VRAM/HBM copy bandwidth per GPU (read+write)", "GB/s", "higher",
                res.get("vram_copy_gb_per_second_mean"), context)
            add("memory.h2d", "Memory", "Host to device, pinned (PCIe) per GPU", "GB/s", "higher",
                res.get("host_to_device_pinned_gb_per_second_mean"), context)
            add("memory.d2h", "Memory", "Device to host, pinned (PCIe) per GPU", "GB/s", "higher",
                res.get("device_to_host_pinned_gb_per_second_mean"), context)
            peers = res.get("peer_to_peer") or {}
            values = [v for row in peers.get("gb_per_second") or [] for v in row if _num(v) is not None]
            if values:
                add("memory.p2p", "Memory", "GPU to GPU copy, mean of pairs", "GB/s", "higher",
                    sum(values) / len(values), context)
        elif test == "nccl":
            name = params.get("collective") or res.get("collective")
            context = {k: params.get(k) for k in ("collective", "max_bytes", "gpus_per_rank", "ranks", "multi_node")}
            add(f"nccl.{name}.peak_busbw", "NCCL", f"NCCL {name} peak busbw", "GB/s", "higher",
                res.get("peak_busbw_gb_per_second"), context)
            add(f"nccl.{name}.avg_busbw", "NCCL", f"NCCL {name} average busbw", "GB/s", "higher",
                res.get("avg_busbw_gb_per_second"), context)
    return rows


def run_label(data: dict) -> str:
    info = data.get("system_info") or {}
    gpus = info.get("gpus") or []
    name = (gpus[0].get("name") or "GPU").replace("NVIDIA ", "") if gpus else "no GPU"
    return f"{name} x{len(gpus)}" if gpus else name


def _differences(runs: list[dict]) -> list[str]:
    warnings = []

    def values(getter):
        return [getter(run) for run in runs]

    def differs(seq):
        return len({json.dumps(v, sort_keys=True, default=str) for v in seq}) > 1

    gpu_counts = values(lambda r: (r.get("system_info") or {}).get("gpu_count"))
    if differs(gpu_counts):
        warnings.append(f"GPU count differs ({', '.join(map(str, gpu_counts))}): the runs are compared as complete "
                        "configurations, not per GPU.")
    models = values(lambda r: [g.get("name") for g in (r.get("system_info") or {}).get("gpus") or []][:1])
    if differs(models):
        warnings.append("GPU models differ: " + ", ".join(str(m[0]) if m else "none" for m in models))

    def llm_set(run, key):
        return sorted({str((e.get("parameters") or {}).get(key)) for e in run.get("results", [])
                       if e.get("test") == "llm" and e.get("status") == "PASSED"})

    llm_runs = [run for run in runs if llm_set(run, "model")]
    for key, label in (("model_name", "LLM models"), ("precision", "Precision"),
                       ("input_tokens", "Input token length"), ("output_tokens", "Output token length"),
                       ("concurrency", "Concurrency levels"), ("tensor_parallel_size", "Tensor parallel size"),
                       ("requests", "Request counts")):
        seen = [llm_set(run, key) for run in llm_runs]
        if len(llm_runs) > 1 and differs(seen):
            warnings.append(f"{label} differ: " + " vs ".join("/".join(s) or "-" for s in seen))
    for key, label in (("matrix_size", "GEMM matrix size"), ("duration_seconds", "GEMM duration")):
        seen = [sorted({str((e.get("parameters") or {}).get(key)) for e in r.get("results", [])
                        if e.get("test") == "compute"}) for r in runs]
        seen = [s for s in seen if s]
        if len(seen) > 1 and differs(seen):
            warnings.append(f"{label} differs: " + " vs ".join("/".join(s) for s in seen))
    versions = values(lambda r: {k: v for k, v in ((r.get("system_info") or {}).get("packages") or {}).items()
                                 if k in ("torch", "vllm", "transformers")})
    if differs(versions):
        warnings.append("Benchmark software versions differ: "
                        + " vs ".join(", ".join(f"{k} {v}" for k, v in sorted(ver.items())) or "unknown"
                                      for ver in versions))
    drivers = values(lambda r: (r.get("system_info") or {}).get("driver_version"))
    if differs(drivers):
        warnings.append("NVIDIA driver versions differ: " + ", ".join(map(str, drivers)))
    return warnings


def compare_runs(runs: list[dict]) -> dict:
    """Side-by-side headline metrics; percentages relative to the first run."""
    if len(runs) < 2:
        raise ValueError("Choose at least two runs to compare.")
    per_run = [{row["key"]: row for row in headline_metrics(run)} for run in runs]
    order = []
    for metrics in per_run:
        for key in metrics:
            if key not in order:
                order.append(key)
    group_rank = {"LLM": 0, "Compute": 1, "Memory": 2, "NCCL": 3}
    order.sort(key=lambda key: group_rank.get(next(m[key]["group"] for m in per_run if key in m), 9))
    rows = []
    for key in order:
        sample = next(m[key] for m in per_run if key in m)
        values = [m[key]["value"] if key in m else None for m in per_run]
        base = values[0]
        diffs = [None] + [((v - base) / base * 100) if v is not None and base else None for v in values[1:]]
        contexts = [m[key]["context"] if key in m else None for m in per_run]
        present = [c for c in contexts if c is not None]
        note = None
        if len({json.dumps(c, sort_keys=True, default=str) for c in present}) > 1:
            note = "parameters differ"
        rows.append({"key": key, "group": sample["group"], "label": sample["label"], "unit": sample["unit"],
                     "better": sample["better"], "values": values, "diff_percent": diffs, "note": note})
    return {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "runs": [{"run_id": run.get("run_id"), "label": run_label(run),
                      "hostname": (run.get("system_info") or {}).get("hostname"),
                      "created_at": run.get("created_at"), "path": run.get("_path")} for run in runs],
            "warnings": _differences(runs), "rows": rows,
            "note": "Differences are relative to the first run. Rows marked 'parameters differ' compare "
                    "different test settings and should not be read as like-for-like."}


# --------------------------------------------------------------- files

def _clean(data: dict) -> dict:
    return {k: v for k, v in data.items() if not k.startswith("_")}


def write_csv(data: dict, path: Path) -> None:
    info = data.get("system_info") or {}
    gpus = info.get("gpus") or []
    common = {"run_id": data.get("run_id"), "created_at": data.get("created_at") or info.get("timestamp"),
              "hostname": info.get("hostname"), "gpu_model": gpus[0].get("name") if gpus else None,
              "gpu_count": len(gpus)}
    columns = (*common, "test", "name", "status", "metric", "value", "unit", "parameters", "error")
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        metrics_by_entry = {}
        for metric in headline_metrics(data):
            metrics_by_entry.setdefault(metric["key"], metric)
        for entry in data.get("results", []):
            base = {**common, "test": entry.get("test"), "name": entry.get("name") or entry.get("test"),
                    "status": entry.get("status"),
                    "parameters": json.dumps(entry.get("parameters") or {}, sort_keys=True, default=str),
                    "error": entry.get("error")}
            rows = list(_entry_metrics(entry))
            if not rows:
                writer.writerow({**base, "metric": None, "value": None, "unit": None})
            for metric, value, unit in rows:
                writer.writerow({**base, "metric": metric, "value": value, "unit": unit})


def _entry_metrics(entry: dict):
    res = entry.get("results") or {}
    test = entry.get("test")
    if test == "llm":
        for key, unit in (("aggregate_output_tokens_per_second", "tok/s"), ("total_tokens_per_second", "tok/s"),
                          ("request_throughput_per_second", "req/s"), ("prefill_tokens_per_second_estimate", "tok/s"),
                          ("completed_requests", "count"), ("failed_requests", "count"),
                          ("duration_seconds", "s"), ("total_output_tokens", "tokens"), ("total_input_tokens", "tokens")):
            if _num(res.get(key)) is not None:
                yield key, res[key], unit
        for group, unit in (("per_request_output_tokens_per_second", "tok/s"), ("ttft_ms", "ms"),
                            ("tpot_ms", "ms"), ("itl_ms", "ms"), ("e2e_latency_ms", "ms")):
            for stat, value in (res.get(group) or {}).items():
                if _num(value) is not None:
                    yield f"{group}.{stat}", value, unit
    elif test == "compute":
        if _num(res.get("measured_tflops_total")) is not None:
            yield "measured_tflops_total", res["measured_tflops_total"], "TFLOPS"
        for gpu in res.get("per_gpu") or []:
            if _num(gpu.get("measured_tflops")) is not None:
                yield f"gpu{gpu.get('gpu')}.measured_tflops", gpu["measured_tflops"], "TFLOPS"
    elif test == "memory":
        for gpu in res.get("per_gpu") or []:
            for key in ("vram_copy", "vram_read", "host_to_device_pinned", "device_to_host_pinned",
                        "host_to_device_pageable", "device_to_host_pageable"):
                value = (gpu.get(key) or {}).get("gb_per_second")
                if _num(value) is not None:
                    yield f"gpu{gpu.get('gpu')}.{key}", value, "GB/s"
        peers = res.get("peer_to_peer") or {}
        for i, row in zip(peers.get("gpus") or [], peers.get("gb_per_second") or []):
            for j, value in zip(peers.get("gpus") or [], row):
                if _num(value) is not None:
                    yield f"p2p.gpu{i}->gpu{j}", value, "GB/s"
        for case in ((res.get("nvbandwidth") or {}).get("testcases") or []):
            if _num(case.get("sum_gb_per_second")) is not None:
                yield f"nvbandwidth.{case.get('name')}.sum", case["sum_gb_per_second"], "GB/s"
    elif test == "nccl":
        for row in res.get("rows") or []:
            yield f"busbw@{row['bytes']}", row.get("busbw_gb_per_second"), "GB/s"
            yield f"algbw@{row['bytes']}", row.get("algbw_gb_per_second"), "GB/s"
        if _num(res.get("avg_busbw_gb_per_second")) is not None:
            yield "avg_busbw", res["avg_busbw_gb_per_second"], "GB/s"


def write_run(data: dict, run_dir: str | Path) -> dict:
    """Write the four report files of one run (rewritten as results arrive)."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    clean = _clean(data)
    (run_dir / "benchmark.json").write_text(json.dumps(clean, indent=2, sort_keys=True, default=str) + "\n",
                                            encoding="utf-8")
    (run_dir / "system-info.json").write_text(
        json.dumps(clean.get("system_info", {}), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    write_csv(clean, run_dir / "benchmark.csv")
    (run_dir / "benchmark.html").write_text(render_html(clean), encoding="utf-8")
    return {"json": str(run_dir / "benchmark.json"), "csv": str(run_dir / "benchmark.csv"),
            "html": str(run_dir / "benchmark.html"), "system_info": str(run_dir / "system-info.json")}


def write_comparison(comparison: dict, output: str | Path) -> dict:
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "compare.json").write_text(json.dumps(comparison, indent=2, default=str) + "\n", encoding="utf-8")
    with (output / "compare.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        labels = [f"{r['label']} ({r['run_id']})" for r in comparison["runs"]]
        writer.writerow(["group", "metric", "unit", "better", *labels,
                         *[f"change vs first: {label}" for label in labels[1:]], "note"])
        for row in comparison["rows"]:
            writer.writerow([row["group"], row["label"], row["unit"], row["better"], *row["values"],
                             *row["diff_percent"][1:], row["note"]])
    (output / "compare.html").write_text(render_compare_html(comparison), encoding="utf-8")
    return {"json": str(output / "compare.json"), "csv": str(output / "compare.csv"),
            "html": str(output / "compare.html")}


# ------------------------------------------------------------------ HTML

def _e(value) -> str:
    return html.escape("—" if value is None else str(value))


def fmt(value, digits: int = 1) -> str:
    value = _num(value)
    if value is None:
        return "—"
    if abs(value) >= 10000:
        return f"{value:,.0f}"
    if abs(value) >= 100:
        return f"{value:,.{max(0, digits - 1)}f}"
    return f"{value:,.{digits}f}" if abs(value) >= 1 else f"{value:.{digits + 2}f}"


def human_bytes(value) -> str:
    value = _num(value)
    if value is None:
        return "—"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return str(value)


CSS = """
:root{--bg:#070b16;--panel:#0d1424;--panel2:#111a2e;--line:#1e2a44;--text:#e6edf7;--muted:#8fa0bd;--faint:#5d6d8a;
--accent:#22d3ee;--ok:#34d399;--warn:#fbbf24;--bad:#f87171;--na:#64748b}
*{box-sizing:border-box}html{background:var(--bg)}body{margin:0;color:var(--text);background:radial-gradient(1200px 500px at 20% -10%,#0f2a3d 0,transparent 60%),var(--bg);
font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1240px;margin:0 auto;padding:28px 20px 60px}
.mono,code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header.top{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-end;justify-content:space-between;border-bottom:1px solid var(--line);padding-bottom:18px;margin-bottom:22px}
.kicker{color:var(--accent);font-weight:600;letter-spacing:.12em;text-transform:uppercase;font-size:12px}
h1{font-size:28px;margin:4px 0 2px;letter-spacing:-.01em}h2{font-size:18px;margin:0 0 14px}h3{font-size:14px;margin:18px 0 8px;color:var(--muted);font-weight:600}
.muted{color:var(--muted)}.faint{color:var(--faint);font-size:12.5px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:18px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.tile .k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em}.tile .v{font-size:20px;font-weight:650;margin-top:4px}
.tile .v small{font-size:12px;color:var(--muted);font-weight:500}
.hero .v{font-size:30px;color:var(--accent)}
section{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;margin:16px 0}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px;white-space:nowrap}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.wrap{overflow-x:auto}
.pill{display:inline-block;border-radius:999px;padding:1px 9px;font-size:11.5px;font-weight:650;letter-spacing:.03em}
.PASSED,.PASS{background:#0d3b2e;color:var(--ok)}.FAILED,.FAIL{background:#3d1518;color:var(--bad)}
.SKIPPED,.UNAVAILABLE,.N\\/A{background:#1c2536;color:var(--na)}.WARN,.STOPPED{background:#3a2c0b;color:var(--warn)}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:18px}
.chart{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.chart h4{margin:0 0 6px;font-size:13px;color:var(--muted);font-weight:600}
svg text{fill:var(--muted);font:11.5px ui-sans-serif,system-ui,sans-serif}svg .val{fill:var(--text);font-weight:600}
svg .grid{stroke:var(--line);stroke-width:1}svg .axis{stroke:#2b3a5a}
.legend{display:flex;flex-wrap:wrap;gap:12px;font-size:12px;color:var(--muted);margin-top:6px}.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.warn-box{border:1px solid #5a4510;background:#1f1806;color:#fde68a;border-radius:10px;padding:10px 14px;margin:10px 0}
.warn-box li{margin:3px 0}
pre{background:#0a1020;border:1px solid var(--line);border-radius:10px;padding:12px;overflow:auto;font-size:12px;color:#cbd5e1;max-height:420px}
.up{color:var(--ok)}.down{color:var(--bad)}
details summary{cursor:pointer;color:var(--muted)}
footer{color:var(--faint);font-size:12px;margin-top:26px;border-top:1px solid var(--line);padding-top:12px}
@media (max-width:640px){.charts{grid-template-columns:1fr}h1{font-size:22px}}
"""


def _bar_chart(items: list, unit: str, color: str = PALETTE[0], width: int = 560) -> str:
    items = [(label, value) for label, value in items if _num(value) is not None]
    if not items:
        return '<p class="faint">No measured values.</p>'
    peak = max(value for _, value in items) or 1
    label_w, row_h = 170, 28
    bar_space = width - label_w - 90
    parts = []
    for index, (label, value) in enumerate(items):
        y = index * row_h + 6
        w = max(2, value / peak * bar_space)
        parts.append(f'<text x="0" y="{y + 15}">{_e(label[:28])}</text>'
                     f'<rect x="{label_w}" y="{y}" width="{w:.1f}" height="18" rx="4" fill="{color}"/>'
                     f'<text class="val" x="{label_w + w + 6:.1f}" y="{y + 14}">{fmt(value)} {_e(unit)}</text>')
    height = len(items) * row_h + 10
    return f'<svg viewBox="0 0 {width} {height}" width="100%" role="img">{"".join(parts)}</svg>'


def _line_chart(series: list, unit: str, log_x: bool = True,
                x_format=human_bytes, width: int = 560, height: int = 240) -> str:
    series = [(name, [(x, y) for x, y in points if _num(x) is not None and _num(y) is not None and (x > 0 or not log_x)])
              for name, points in series]
    series = [(name, points) for name, points in series if points]
    if not series:
        return '<p class="faint">No measured values.</p>'
    xs = [x for _, pts in series for x, _ in pts]
    ys = [y for _, pts in series for _, y in pts]
    tx = (lambda v: math.log2(v)) if log_x else (lambda v: v)
    x0, x1 = tx(min(xs)), tx(max(xs))
    y1 = max(ys) * 1.1 or 1
    left, right, top, bottom = 52, 12, 10, 26
    pw, ph = width - left - right, height - top - bottom

    def px(v):
        return left + (0 if x1 == x0 else (tx(v) - x0) / (x1 - x0) * pw)

    def py(v):
        return top + ph - v / y1 * ph

    parts = []
    for i in range(5):
        value = y1 * i / 4
        y = py(value)
        parts.append(f'<line class="grid" x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}"/>'
                     f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{fmt(value, 0)}</text>')
    ticks = sorted(set(xs))
    step = max(1, len(ticks) // 6)
    for value in ticks[::step]:
        parts.append(f'<text x="{px(value):.1f}" y="{height - 6}" text-anchor="middle">{_e(x_format(value))}</text>')
    legend = []
    for index, (name, points) in enumerate(series):
        color = PALETTE[index % len(PALETTE)]
        path = " ".join(f"{'M' if i == 0 else 'L'}{px(x):.1f},{py(y):.1f}" for i, (x, y) in enumerate(sorted(points)))
        parts.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2"/>')
        for x, y in points:
            parts.append(f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="2.3" fill="{color}"><title>{_e(name)}: '
                         f'{fmt(y)} {_e(unit)} @ {_e(x_format(x))}</title></circle>')
        legend.append(f'<span><i style="background:{color}"></i>{_e(name)}</span>')
    parts.append(f'<text x="4" y="{top + 8}">{_e(unit)}</text>')
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" role="img">{"".join(parts)}</svg>'
            f'<div class="legend">{"".join(legend)}</div>')


def _pill(status) -> str:
    status = str(status or "N/A")
    return f'<span class="pill {html.escape(status)}">{html.escape(status)}</span>'


def _gpu_sample_cells(entry: dict) -> str:
    sample = entry.get("gpu_sample") or {}
    util = sample.get("utilization_mean_percent")
    power = sample.get("power_mean_watts_total")
    memory = sample.get("memory_peak_bytes_total")
    return (f'<td class="n">{fmt(util, 0)}%</td><td class="n">{human_bytes(memory)}</td>'
            f'<td class="n">{fmt(power, 0)} W</td>')


def _llm_section(entries: list[dict]) -> str:
    llm = [e for e in entries if e.get("test") == "llm"]
    if not llm:
        return ""
    passed = [e for e in llm if e.get("status") in ("PASSED", "STOPPED")]
    first = (passed or llm)[0].get("parameters") or {}
    rows = []
    for e in llm:
        r, p = e.get("results") or {}, e.get("parameters") or {}
        mode = "Single request" if p.get("concurrency") == 1 else "Concurrent"
        rows.append(
            f"<tr><td>{_pill(e.get('status'))}</td><td>{mode}</td><td class='n'>{_e(p.get('concurrency'))}</td>"
            f"<td class='n'>{_e(r.get('completed_requests'))}/{_e(p.get('requests'))}</td>"
            f"<td class='n'><b>{fmt(r.get('aggregate_output_tokens_per_second'))}</b></td>"
            f"<td class='n'>{fmt((r.get('per_request_output_tokens_per_second') or {}).get('p50'))}</td>"
            f"<td class='n'>{fmt(r.get('total_tokens_per_second'))}</td>"
            f"<td class='n'>{fmt((r.get('ttft_ms') or {}).get('p50'))}</td><td class='n'>{fmt((r.get('ttft_ms') or {}).get('p95'))}</td>"
            f"<td class='n'>{fmt((r.get('ttft_ms') or {}).get('p99'))}</td>"
            f"<td class='n'>{fmt((r.get('tpot_ms') or {}).get('p50'), 2)}</td><td class='n'>{fmt((r.get('itl_ms') or {}).get('p95'), 2)}</td>"
            f"<td class='n'>{fmt((r.get('e2e_latency_ms') or {}).get('p95'), 0)}</td>"
            f"{_gpu_sample_cells(e)}<td>{_e(e.get('error') or '')}</td></tr>")
    agg = [(f"c={(e.get('parameters') or {}).get('concurrency')}", (e.get("results") or {}).get("aggregate_output_tokens_per_second"))
           for e in passed]
    per = [(f"c={(e.get('parameters') or {}).get('concurrency')}",
            ((e.get("results") or {}).get("per_request_output_tokens_per_second") or {}).get("p50")) for e in passed]
    ttft = [("P50 c=" + str((e.get("parameters") or {}).get("concurrency")), ((e.get("results") or {}).get("ttft_ms") or {}).get("p50"))
            for e in passed] + [("P95 c=" + str((e.get("parameters") or {}).get("concurrency")),
                                 ((e.get("results") or {}).get("ttft_ms") or {}).get("p95")) for e in passed]
    single = next((e for e in passed if (e.get("parameters") or {}).get("concurrency") == 1), None)
    verify = []
    for e in passed:
        r = e.get("results") or {}
        if "server_counts_match_client" in r:
            verify.append(f"c={(e.get('parameters') or {}).get('concurrency')}: "
                          + ("server and client token counts match" if r["server_counts_match_client"]
                             else f"MISMATCH server {r.get('server_counters')} vs client "
                                  f"{r.get('total_input_tokens')}/{r.get('total_output_tokens')}"))
    tiles = ""
    best = max((_num(v) or 0 for _, v in agg), default=0) or None
    if single or best is not None:
        single_res = (single or {}).get("results") or {}
        tiles = ('<div class="tiles"><div class="tile hero"><div class="k">Single-request speed</div><div class="v">'
                 + fmt((single_res.get("per_request_output_tokens_per_second") or {}).get("p50"))
                 + ' <small>tok/s per stream</small></div></div><div class="tile"><div class="k">TTFT P50 (single)</div>'
                 + '<div class="v">' + fmt((single_res.get("ttft_ms") or {}).get("p50")) + ' <small>ms</small></div></div>'
                 + '<div class="tile"><div class="k">Prefill estimate</div><div class="v">'
                 + fmt(single_res.get("prefill_tokens_per_second_estimate"), 0) + ' <small>tok/s</small></div></div>'
                 + '<div class="tile hero"><div class="k">Best aggregate throughput</div><div class="v">'
                 + fmt(best) + ' <small>tok/s all streams</small></div></div></div>')
    return f"""<section><h2>LLM inference</h2>
<p class="muted">Model <b class="mono">{_e(first.get('model_name') or first.get('model'))}</b> · {_e(first.get('precision'))} ·
{_e(first.get('input_tokens'))} input / {_e(first.get('output_tokens'))} output tokens · TP {_e(first.get('tensor_parallel_size'))}
on {_e(first.get('gpu_count'))} GPU(s) · {_e(first.get('engine'))}</p>
{tiles}
<div class="charts"><div class="chart"><h4>Aggregate output tokens/s by concurrency</h4>{_bar_chart(agg, "tok/s")}</div>
<div class="chart"><h4>Per-request output tokens/s (median stream)</h4>{_bar_chart(per, "tok/s", PALETTE[1])}</div>
<div class="chart"><h4>Time to first token</h4>{_bar_chart(ttft, "ms", PALETTE[2])}</div></div>
<h3>Results per concurrency level</h3><div class="wrap"><table><thead><tr><th>Status</th><th>Mode</th><th class="n">Conc.</th><th class="n">Done</th>
<th class="n">Aggregate tok/s</th><th class="n">Per-request tok/s</th><th class="n">Total tok/s</th><th class="n">TTFT P50 ms</th>
<th class="n">TTFT P95</th><th class="n">TTFT P99</th><th class="n">TPOT P50 ms</th><th class="n">ITL P95 ms</th><th class="n">E2E P95 ms</th>
<th class="n">GPU util</th><th class="n">VRAM peak</th><th class="n">Power</th><th>Note</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p class="faint">Aggregate = all output tokens of all concurrent requests / measured wall time. Per-request = (output tokens − 1) /
(end − first token) for each stream, median shown. Token counts are the server's own usage records{('; ' + '; '.join(verify)) if verify else ''}.
GPU utilisation, VRAM and power are NVML samples during the measured window.</p></section>"""


def _compute_section(entries: list[dict]) -> str:
    rows = [e for e in entries if e.get("test") == "compute"]
    if not rows:
        return ""
    totals = [((e.get("parameters") or {}).get("precision", "?").upper(), (e.get("results") or {}).get("measured_tflops_total"))
              for e in rows if e.get("status") == "PASSED"]
    lines = []
    for e in rows:
        r, p = e.get("results") or {}, e.get("parameters") or {}
        per = ", ".join(f"GPU{g.get('gpu')}: {fmt(g.get('measured_tflops'))}" if g.get("status") == "PASSED"
                        else f"GPU{g.get('gpu')}: {g.get('status')}" for g in r.get("per_gpu") or [])
        lines.append(f"<tr><td>{_pill(e.get('status'))}</td><td>{_e(str(p.get('precision', '')).upper())}</td>"
                     f"<td class='n'>{_e(p.get('matrix_size'))}</td><td class='n'>{_e(p.get('duration_seconds'))} s</td>"
                     f"<td class='n'><b>{fmt(r.get('measured_tflops_total'))}</b></td>"
                     f"<td class='n'>{fmt(r.get('measured_tflops_per_gpu_mean'))}</td><td class='mono'>{_e(per)}</td>"
                     f"{_gpu_sample_cells(e)}<td>{_e(e.get('error') or '')}</td></tr>")
    return f"""<section><h2>GPU compute (GEMM)</h2><div class="charts"><div class="chart"><h4>Measured TFLOPS, all selected GPUs</h4>
{_bar_chart(totals, "TFLOPS", PALETTE[3])}</div></div><div class="wrap"><table><thead><tr><th>Status</th><th>Precision</th><th class="n">Matrix</th>
<th class="n">Duration</th><th class="n">TFLOPS total</th><th class="n">per GPU</th><th>Per GPU</th><th class="n">GPU util</th>
<th class="n">VRAM peak</th><th class="n">Power</th><th>Note</th></tr></thead><tbody>{''.join(lines)}</tbody></table></div>
<p class="faint">Measured dense GEMM throughput (CUDA events, after warmup, all selected GPUs concurrently). FP32 runs with TF32 off;
TF32 is the same FP32 GEMM with tensor cores allowed. These are measurements, not datasheet or sparsity peaks.</p></section>"""


def _memory_section(entries: list[dict]) -> str:
    rows = [e for e in entries if e.get("test") == "memory"]
    if not rows:
        return ""
    out = []
    for e in rows:
        r = e.get("results") or {}
        if e.get("status") != "PASSED":
            out.append(f"<p>{_pill(e.get('status'))} {_e(e.get('error'))}</p>")
            continue
        table = []
        vram, link = [], []
        for g in r.get("per_gpu") or []:
            table.append(f"<tr><td>GPU {g.get('gpu')}</td><td>{_e(g.get('gpu_name'))}</td><td class='n'>{human_bytes(g.get('test_bytes'))}</td>"
                         + "".join(f"<td class='n'>{fmt((g.get(k) or {}).get('gb_per_second'))}</td>" for k in
                                   ("vram_copy", "vram_read", "host_to_device_pinned", "device_to_host_pinned",
                                    "host_to_device_pageable", "device_to_host_pageable")) + "</tr>")
            vram.append((f"GPU {g.get('gpu')} copy", (g.get("vram_copy") or {}).get("gb_per_second")))
            vram.append((f"GPU {g.get('gpu')} read", (g.get("vram_read") or {}).get("gb_per_second")))
            link.append((f"GPU {g.get('gpu')} H2D", (g.get("host_to_device_pinned") or {}).get("gb_per_second")))
            link.append((f"GPU {g.get('gpu')} D2H", (g.get("device_to_host_pinned") or {}).get("gb_per_second")))
        peers = r.get("peer_to_peer")
        peer_html = ""
        if peers:
            values = [v for row in peers["gb_per_second"] for v in row if _num(v) is not None]
            top = max(values, default=1) or 1
            head = "".join(f"<th class='n'>GPU {g}</th>" for g in peers["gpus"])
            body = []
            for gpu, row, access in zip(peers["gpus"], peers["gb_per_second"], peers["peer_access"]):
                cells = []
                for value, acc in zip(row, access):
                    if value is None:
                        cells.append("<td class='n faint'>—</td>")
                    else:
                        alpha = 0.15 + 0.6 * value / top
                        cells.append(f"<td class='n' style='background:rgba(34,211,238,{alpha:.2f})'>{fmt(value)}"
                                     f"<span class='faint'> {'P2P' if acc == 'yes' else 'via host'}</span></td>")
                body.append(f"<tr><th>GPU {gpu} →</th>{''.join(cells)}</tr>")
            peer_html = (f"<h3>GPU to GPU copies (GB/s, row → column)</h3><div class='wrap'><table><thead><tr><th></th>{head}</tr></thead>"
                         f"<tbody>{''.join(body)}</tbody></table></div>")
        nvbw = r.get("nvbandwidth") or {}
        nv_html = ""
        if nvbw.get("testcases"):
            nv_rows = "".join(f"<tr><td class='mono'>{_e(c.get('name'))}</td><td>{_e(c.get('status'))}</td>"
                              f"<td class='n'>{fmt(c.get('sum_gb_per_second'))}</td><td>{_e(c.get('description') or c.get('waiver_reason') or '')}</td></tr>"
                              for c in nvbw["testcases"])
            nv_html = (f"<h3>nvbandwidth {_e(nvbw.get('version') or '')}</h3><div class='wrap'><table><thead><tr><th>Test</th><th>Status</th>"
                       f"<th class='n'>Sum GB/s</th><th>Description</th></tr></thead><tbody>{nv_rows}</tbody></table></div>")
        elif nvbw:
            nv_html = f"<p class='faint'>nvbandwidth: {_e(nvbw.get('status'))} {_e(nvbw.get('reason') or nvbw.get('error'))}</p>"
        out.append(f"""<div class="charts"><div class="chart"><h4>VRAM / HBM bandwidth (on-GPU)</h4>{_bar_chart(vram, "GB/s", PALETTE[5])}</div>
<div class="chart"><h4>Host ↔ device, pinned (PCIe / C2C)</h4>{_bar_chart(link, "GB/s", PALETTE[2])}</div></div>
<div class="wrap"><table><thead><tr><th>GPU</th><th>Model</th><th class="n">Buffer</th><th class="n">VRAM copy</th><th class="n">VRAM read</th>
<th class="n">H2D pinned</th><th class="n">D2H pinned</th><th class="n">H2D pageable</th><th class="n">D2H pageable</th></tr></thead>
<tbody>{''.join(table)}</tbody></table></div>{peer_html}{nv_html}<p class="faint">{_e(r.get('units_note'))}</p>""")
    return "<section><h2>GPU memory and transfers</h2>" + "".join(out) + "</section>"


def _nccl_section(entries: list[dict]) -> str:
    rows = [e for e in entries if e.get("test") == "nccl"]
    if not rows:
        return ""
    series = [((e.get("parameters") or {}).get("collective") or e.get("name"),
               [(row["bytes"], row["busbw_gb_per_second"]) for row in (e.get("results") or {}).get("rows") or []])
              for e in rows if e.get("status") == "PASSED"]
    table = []
    for e in rows:
        r, p = e.get("results") or {}, e.get("parameters") or {}
        table.append(f"<tr><td>{_pill(e.get('status'))}</td><td>{_e(p.get('collective'))}</td><td>{_e(p.get('launcher'))}</td>"
                     f"<td class='n'>{_e(p.get('gpus_per_rank'))}{'' if not p.get('ranks') else ' × ' + str(p.get('ranks')) + ' ranks'}</td>"
                     f"<td class='n'><b>{fmt(r.get('peak_busbw_gb_per_second'))}</b></td><td class='n'>{fmt(r.get('peak_algbw_gb_per_second'))}</td>"
                     f"<td class='n'>{human_bytes(r.get('peak_at_bytes'))}</td><td class='n'>{fmt(r.get('avg_busbw_gb_per_second'))}</td>"
                     f"<td>{_e(e.get('error') or r.get('out_of_bounds') or '')}</td></tr>")
    return f"""<section><h2>Multi-GPU communication (NCCL)</h2><div class="charts"><div class="chart"><h4>Bus bandwidth vs message size</h4>
{_line_chart(series, "GB/s busbw")}</div></div><div class="wrap"><table><thead><tr><th>Status</th><th>Collective</th><th>Launcher</th><th class="n">GPUs</th>
<th class="n">Peak busbw GB/s</th><th class="n">Peak algbw GB/s</th><th class="n">at size</th><th class="n">Avg busbw</th><th>Note</th></tr></thead>
<tbody>{''.join(table)}</tbody></table></div><p class="faint">nccl-tests in-place results. algbw = data size / time; busbw applies the
collective's correction factor so it can be compared with link speed. Neither is raw physical link bandwidth.</p></section>"""


def _system_section(info: dict) -> str:
    gpus = info.get("gpus") or []
    rows = []
    for g in gpus:
        link = g.get("pcie_link") or {}
        ecc = g.get("ecc") or {}
        mig = g.get("mig_mode") or {}
        rows.append(f"<tr><td>{g.get('index')}</td><td>{_e(g.get('name'))}</td><td class='mono faint'>{_e(g.get('uuid'))}</td>"
                    f"<td class='n'>{human_bytes(g.get('memory_total_bytes'))}</td><td>{_e(g.get('compute_capability'))}</td>"
                    f"<td>{'Gen' + str(link.get('generation')) + ' x' + str(link.get('width')) if link.get('generation') else 'N/A'}</td>"
                    f"<td>{len(g.get('nvlink') or []) or 'N/A'}</td><td>{_e(mig.get('current') or 'N/A')}</td>"
                    f"<td>{_e(ecc.get('mode') or 'N/A')}</td><td class='n'>{fmt(g.get('power_limit_watts'), 0)} W</td>"
                    f"<td class='n'>{_e(g.get('sm_clock_max_mhz') or 'N/A')}</td></tr>")
    topo = info.get("gpu_topology") or ""
    if not topo and info.get("topology"):
        matrix = info["topology"]["matrix"]
        topo = "\n".join(["      " + " ".join(f"GPU{i:<3}" for i in range(len(matrix)))]
                         + [f"GPU{i:<3}" + " ".join(f"{c:<6}" for c in row) for i, row in enumerate(matrix)]
                         + ["", info["topology"]["legend"]])
    rdma = info.get("rdma_devices") or []
    rdma_rows = "".join(f"<tr><td>{_e(d.get('name'))}</td><td>{_e(d.get('transport'))}</td><td>{_e(', '.join(d.get('netdevs') or []))}</td>"
                        f"<td>{_e('; '.join(str(p.get('port')) + ': ' + str(p.get('state')) + ' ' + str(p.get('rate')) for p in d.get('ports') or []))}</td></tr>"
                        for d in rdma)
    host = info.get("host") or {}
    return f"""<section><h2>System</h2><div class="wrap"><table><thead><tr><th>#</th><th>GPU</th><th>UUID</th><th class="n">VRAM</th><th>CC</th>
<th>PCIe</th><th>NVLinks</th><th>MIG</th><th>ECC</th><th class="n">Power limit</th><th class="n">Max SM MHz</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan="11">No NVIDIA GPU was visible.</td></tr>'}</tbody></table></div>
<h3>Host</h3><p class="muted mono">{_e(info.get('hostname'))} · {_e(host.get('cpu_model'))} ({_e(host.get('cpu_count'))} CPUs) ·
{human_bytes(host.get('memory_total_bytes'))} RAM · kernel {_e(host.get('kernel'))} · driver {_e(info.get('driver_version'))} ·
CUDA runtime {_e(info.get('cuda_version'))} · PyTorch {_e((info.get('packages') or {}).get('torch'))} · vLLM {_e((info.get('packages') or {}).get('vllm'))}</p>
{('<h3>GPU topology</h3><pre>' + _e(topo) + '</pre>') if topo else ''}
{('<h3>RDMA devices</h3><div class="wrap"><table><thead><tr><th>Device</th><th>Transport</th><th>Netdev</th><th>Ports</th></tr></thead><tbody>' + rdma_rows + '</tbody></table></div>') if rdma else ''}
<details><summary>Full system-info.json</summary><pre>{_e(json.dumps(info, indent=2, sort_keys=True, default=str))}</pre></details></section>"""


def _health_section(entries: list[dict]) -> str:
    health = next((e for e in entries if e.get("test") == "health"), None)
    if not health:
        return ""
    rows = "".join(f"<tr><td>{_pill(c.get('status'))}</td><td>{_e(c.get('name'))}</td><td>{_e(c.get('detail'))}</td></tr>"
                   for c in (health.get("results") or {}).get("checks") or [])
    return f"<section><h2>Health</h2><div class='wrap'><table><tbody>{rows}</tbody></table></div></section>"


def render_html(data: dict) -> str:
    info = data.get("system_info") or {}
    entries = data.get("results") or []
    gpus = info.get("gpus") or []
    image = info.get("image") or {}
    created = data.get("created_at") or info.get("timestamp")
    vram = sum(g.get("memory_total_bytes") or 0 for g in gpus)
    statuses = [e.get("status") for e in entries if e.get("test") != "health"]
    skipped = [e for e in entries if e.get("status") in ("SKIPPED", "UNAVAILABLE", "FAILED", "STOPPED")]
    skipped_html = ""
    if skipped:
        skipped_html = ("<section><h2>Not measured</h2><div class='wrap'><table><tbody>" + "".join(
            f"<tr><td>{_pill(e.get('status'))}</td><td>{_e(e.get('name') or e.get('test'))}</td><td>{_e(e.get('error'))}</td></tr>"
            for e in skipped) + "</tbody></table></div></section>")
    best = max((((e.get("results") or {}).get("aggregate_output_tokens_per_second") or 0) for e in entries
                if e.get("test") == "llm" and e.get("status") == "PASSED"), default=None) or None
    tflops = max((((e.get("results") or {}).get("measured_tflops_total") or 0) for e in entries
                  if e.get("test") == "compute" and e.get("status") == "PASSED"), default=None) or None
    params = json.dumps(data.get("configuration") or {}, indent=2, sort_keys=True, default=str)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GPU benchmark {_e(data.get('run_id'))}</title><style>{CSS}</style></head><body><main>
<header class="top"><div><div class="kicker">NVIDIA GPU &amp; LLM Benchmark · LayerSmith</div><h1>{_e(run_label(data))}</h1>
<div class="muted">Run <span class="mono">{_e(data.get('run_id'))}</span> · {_e(created)} · host {_e(info.get('hostname'))}</div></div>
<div class="muted mono" style="text-align:right">{_e(image.get('image') or 'gpu-bench')}<br>CUDA {_e(image.get('cuda_version') or info.get('cuda_version'))} · driver {_e(info.get('driver_version'))}</div></header>
<div class="tiles"><div class="tile"><div class="k">GPUs</div><div class="v">{len(gpus)} <small>{_e(gpus[0].get('name') if gpus else 'none visible')}</small></div></div>
<div class="tile"><div class="k">Total VRAM</div><div class="v">{human_bytes(vram) if vram else '—'}</div></div>
<div class="tile hero"><div class="k">Best LLM throughput</div><div class="v">{fmt(best)} <small>tok/s</small></div></div>
<div class="tile"><div class="k">Best GEMM</div><div class="v">{fmt(tflops)} <small>TFLOPS</small></div></div>
<div class="tile"><div class="k">Tests</div><div class="v">{statuses.count('PASSED')} <small>passed of {len(statuses)}</small></div></div></div>
{_llm_section(entries)}{_compute_section(entries)}{_memory_section(entries)}{_nccl_section(entries)}{skipped_html}{_health_section(entries)}
{_system_section(info)}<section><h2>Parameters</h2><pre>{_e(params)}</pre></section>
<footer>Generated by gpu-bench (LayerSmith NVIDIA GPU &amp; LLM Benchmark). All figures are measured in this run; nothing is simulated
or taken from datasheets. N/A means the hardware or driver does not report that value.</footer></main></body></html>"""


def render_compare_html(comparison: dict) -> str:
    runs = comparison["runs"]
    head = "".join(f"<th class='n'>{_e(r['label'])}<br><span class='faint mono'>{_e(r['run_id'])}</span></th>" for r in runs)
    body, group = [], None
    for row in comparison["rows"]:
        if row["group"] != group:
            group = row["group"]
            body.append(f"<tr><th colspan='{len(runs) + 3}' style='color:var(--accent);padding-top:16px'>{_e(group)}</th></tr>")
        cells = []
        for value, diff in zip(row["values"], row["diff_percent"]):
            text = fmt(value)
            if diff is not None:
                good = (diff > 0) == (row["better"] == "higher")
                text += f" <span class='{'up' if good else 'down'}'>{diff:+.1f}%</span>"
            cells.append(f"<td class='n'>{text}</td>")
        body.append(f"<tr><td>{_e(row['label'])}</td><td class='faint'>{_e(row['unit'])}, {_e(row['better'])} is better</td>"
                    f"{''.join(cells)}<td class='faint'>{_e(row['note'] or '')}</td></tr>")
    warnings = "".join(f"<li>{_e(w)}</li>" for w in comparison["warnings"])
    charts = []
    for row in comparison["rows"]:
        if row["key"].endswith(".aggregate") or row["key"].endswith(".total") or row["key"] in ("memory.vram_copy",) \
                or row["key"].endswith("peak_busbw"):
            charts.append(f"<div class='chart'><h4>{_e(row['label'])}</h4>"
                          + _bar_chart([(r["label"], v) for r, v in zip(runs, row["values"])], row["unit"],
                                       PALETTE[len(charts) % len(PALETTE)]) + "</div>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GPU benchmark comparison</title><style>{CSS}</style></head><body><main><header class="top"><div><div class="kicker">NVIDIA GPU &amp; LLM Benchmark · LayerSmith</div>
<h1>Benchmark comparison</h1><div class="muted">{len(runs)} runs · generated {_e(comparison['generated_at'])}</div></div></header>
{('<div class="warn-box"><b>Check before comparing</b><ul>' + warnings + '</ul></div>') if warnings else ''}
<section><div class="charts">{''.join(charts[:8])}</div></section>
<section><div class="wrap"><table><thead><tr><th>Metric</th><th>Unit</th>{head}<th></th></tr></thead><tbody>{''.join(body)}</tbody></table></div>
<p class="faint">{_e(comparison['note'])}</p></section><footer>Generated by gpu-bench compare.</footer></main></body></html>"""
