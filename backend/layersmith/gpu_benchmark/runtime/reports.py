"""Portable JSON/CSV/HTML benchmark output and explicit comparison metadata."""

from __future__ import annotations

import csv
import datetime as dt
import html
import json
from pathlib import Path


def _flat_metrics(entry: dict) -> dict:
    flat = {}

    def visit(prefix: str, value):
        if isinstance(value, dict):
            for key, nested in value.items():
                visit(f"{prefix}_{key}" if prefix else str(key), nested)
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                visit(f"{prefix}_{index}", nested)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            flat[prefix] = value

    visit("", entry.get("results") or {})
    return flat


def compare(first: dict, second: dict) -> dict:
    a, b = first.get("system_info", {}), second.get("system_info", {})
    config_a, config_b = first.get("configuration", {}), second.get("configuration", {})
    differences = []
    labels = {"model": "model", "precision": "precision", "gpu_count": "GPU count",
              "input_tokens": "input tokens", "output_tokens": "output tokens",
              "concurrency": "concurrency", "requests": "requests", "warmup_requests": "warmup requests",
              "duration_seconds": "duration", "matrix_size": "matrix size", "test": "test",
              "gpus": "GPUs per rank", "min_bytes": "minimum message size", "max_bytes": "maximum message size",
              "iterations": "iterations", "tensor_parallel_size": "tensor parallel size"}
    for key in sorted(set(config_a) | set(config_b)):
        label = labels.get(key, key.replace("_", " "))
        left = config_a.get(key)
        right = config_b.get(key)
        if left != right:
            differences.append({"parameter": label, "first": left, "second": right})
    if a.get("gpu_count") != b.get("gpu_count"):
        differences.append({"parameter": "visible GPU count", "first": a.get("gpu_count"), "second": b.get("gpu_count")})
    if [g.get("name") for g in a.get("gpus", [])] != [g.get("name") for g in b.get("gpus", [])]:
        differences.append({"parameter": "GPU model", "first": [g.get("name") for g in a.get("gpus", [])],
                            "second": [g.get("name") for g in b.get("gpus", [])]})
    if not differences:
        differences.append({"parameter": "test configuration", "first": "matching", "second": "matching"})
    def row_key(row):
        params = row.get("parameters") or {}
        # Keep LLM concurrency sweeps aligned even when model, precision, or
        # other parameters differ; those differences are shown explicitly.
        return (row.get("test"), params.get("concurrency"))

    first_rows = {row_key(row): row for row in first.get("results", [])}
    second_rows = {row_key(row): row for row in second.get("results", [])}
    metrics = []
    for key in sorted(set(first_rows) & set(second_rows)):
        name = key[0]
        params_a, params_b = first_rows[key].get("parameters") or {}, second_rows[key].get("parameters") or {}
        if params_a != params_b:
            suffix = f" (concurrency {key[1]})" if key[1] is not None else ""
            differences.append({"parameter": f"{name} test parameters{suffix}",
                                "first": params_a, "second": params_b})
        left, right = _flat_metrics(first_rows[key]), _flat_metrics(second_rows[key])
        for key in sorted(set(left) & set(right)):
            if isinstance(left[key], (int, float)) and not isinstance(left[key], bool) and isinstance(right[key], (int, float)):
                metrics.append({"test": name, "metric": key, "first": left[key], "second": right[key],
                                "change_percent": ((right[key] - left[key]) / left[key] * 100) if left[key] else None})
    return {"differences": differences, "metrics": metrics}


def _series_html(results: list[dict]) -> str:
    points = []
    for row in results:
        flat = _flat_metrics(row)
        metric = (flat.get("measured_tflops") or flat.get("output_tokens_per_second")
                  or flat.get("transfers_device_to_device_gpu_memory_gb_per_second")
                  or flat.get("transfers_host_to_device_pcie_gb_per_second"))
        if isinstance(metric, (int, float)):
            points.append((row.get("test", "test"), float(metric)))
    if not points:
        return '<p class="muted">No numeric throughput result is available yet.</p>'
    peak = max(value for _, value in points) or 1
    bars = []
    for name, value in points:
        width = max(2, int(value / peak * 560))
        bars.append(f'<g><text x="0" y="{len(bars) * 34 + 18}">{html.escape(name)}</text>'
                    f'<rect x="150" y="{len(bars) * 34}" width="{width}" height="22" rx="5"/>'
                    f'<text class="value" x="{160 + width}" y="{len(bars) * 34 + 17}">{value:.2f}</text></g>')
    return '<svg viewBox="0 0 820 ' + str(max(80, len(points) * 34 + 20)) + '" role="img" aria-label="Measured throughput">' + "".join(bars) + "</svg>"


def render_html(data: dict, comparison: dict | None = None) -> str:
    system = data.get("system_info", {})
    gpu_text = ", ".join(f"{gpu.get('name', 'GPU')} × {system.get('gpu_count', 1)}" for gpu in system.get("gpus", [])[:1]) or "No visible NVIDIA GPU"
    rows = []
    for entry in data.get("results", []):
        rows.append("<tr>" + "".join(f"<td>{html.escape(str(value if value is not None else '—'))}</td>" for value in
                                      (entry.get("test"), entry.get("status"), json.dumps(entry.get("parameters") or {}, sort_keys=True),
                                       json.dumps(entry.get("results") or {}, sort_keys=True), entry.get("error"))) + "</tr>")
    compare_block = ""
    if comparison:
        mismatch_rows = "".join(f"<tr><td>{html.escape(str(row['parameter']))}</td><td>{html.escape(str(row['first']))}</td>"
                                 f"<td>{html.escape(str(row['second']))}</td></tr>" for row in comparison["differences"])
        metric_rows = []
        for row in comparison["metrics"]:
            change = "—" if row["change_percent"] is None else f"{row['change_percent']:+.1f}%"
            metric_rows.append(f"<tr><td>{html.escape(str(row['test']))}</td><td>{html.escape(str(row['metric']))}</td>"
                               f"<td>{row['first']:.4g}</td><td>{row['second']:.4g}</td><td>{change}</td></tr>")
        compare_block = ("<section><h2>Comparison</h2><p>Differences in model, precision, GPU model/count or test parameters "
                         "are shown before interpreting performance.</p><table><thead><tr><th>Parameter</th><th>Current</th><th>Compared</th></tr>"
                         f"</thead><tbody>{mismatch_rows}</tbody></table><h3>Numeric result changes</h3><table><thead><tr>"
                         "<th>Test</th><th>Metric</th><th>Current</th><th>Compared</th><th>Change</th></tr></thead>"
                         f"<tbody>{''.join(metric_rows)}</tbody></table></section>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>GPU Benchmark Report</title><style>
:root{{--bg:#0b1020;--card:#121a2d;--line:#26334c;--text:#edf3ff;--muted:#a6b4cc;--accent:#82e0b2;--warn:#ffd27a}}
*{{box-sizing:border-box}}body{{margin:0;background:linear-gradient(130deg,#101b32,var(--bg) 60%);color:var(--text);font:15px/1.55 system-ui,sans-serif}}
main{{max-width:1120px;margin:36px auto;padding:0 22px}}header,section,.stat{{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px;margin:16px 0}}
h1{{margin:.1rem 0;font-size:2rem}}h2{{margin-top:0}}.muted{{color:var(--muted)}}.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}}
.stat span{{display:block;color:var(--muted);font-size:.85rem}}.stat strong{{font-size:1.05rem}}table{{width:100%;border-collapse:collapse;display:block;overflow:auto}}th,td{{text-align:left;border-bottom:1px solid var(--line);padding:10px;vertical-align:top}}
th{{color:var(--muted);font-size:.82rem}}code,td{{overflow-wrap:anywhere}}svg{{width:100%;max-height:480px}}svg rect{{fill:var(--accent)}}svg text{{fill:var(--muted);font:13px system-ui}}svg .value{{fill:var(--text)}}.warning{{color:var(--warn)}}
</style></head><body><main><header><p class="muted">NVIDIA GPU Benchmark Toolkit</p><h1>Benchmark report</h1>
<p>{html.escape(str(system.get('timestamp', 'Unknown time')))} · {html.escape(str(system.get('hostname', 'Unknown host')))}</p></header>
<div class="stats"><div class="stat"><span>GPU</span><strong>{html.escape(gpu_text)}</strong></div>
<div class="stat"><span>Driver / CUDA</span><strong>{html.escape(str(system.get('driver_version') or '—'))} / {html.escape(str(system.get('cuda_version') or '—'))}</strong></div>
<div class="stat"><span>Results</span><strong>{len(data.get('results', []))} benchmark entries</strong></div></div>
<section><h2>Measured throughput</h2>{_series_html(data.get('results', []))}</section>
<section><h2>Results</h2><table><thead><tr><th>Test</th><th>Status</th><th>Parameters</th><th>Measured results</th><th>Error / skip reason</th></tr></thead><tbody>{''.join(rows)}</tbody></table></section>
{compare_block}<section><h2>System information</h2><pre>{html.escape(json.dumps(system, indent=2, sort_keys=True))}</pre>
<p class="muted">Throughput values are measured for this run. They are not theoretical hardware limits. NCCL algbw and busbw are collective-operation metrics and do not equal physical network bandwidth.</p></section>
</main></body></html>"""


def write_report(data: dict, output: str | Path, compare_to: str | Path | None = None) -> dict:
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    comparison = None
    if compare_to:
        comparison = compare(data, json.loads(Path(compare_to).read_text(encoding="utf-8")))
        data["comparison"] = comparison
    (output / "benchmark.json").write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")
    (output / "system-info.json").write_text(json.dumps(data.get("system_info", {}), indent=2, sort_keys=True) + "\n")
    columns = ("timestamp", "hostname", "gpu_count", "gpu_model", "test", "status", "parameters", "results", "error")
    with (output / "benchmark.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        system = data.get("system_info", {})
        model = ", ".join(dict.fromkeys(gpu.get("name", "") for gpu in system.get("gpus", [])))
        for entry in data.get("results", []):
            writer.writerow({"timestamp": system.get("timestamp"), "hostname": system.get("hostname"),
                             "gpu_count": system.get("gpu_count", 0), "gpu_model": model,
                             "test": entry.get("test"), "status": entry.get("status"),
                             "parameters": json.dumps(entry.get("parameters") or {}, sort_keys=True),
                             "results": json.dumps(entry.get("results") or {}, sort_keys=True),
                             "error": entry.get("error")})
    (output / "benchmark.html").write_text(render_html(data, comparison), encoding="utf-8")
    return {"json": str(output / "benchmark.json"), "csv": str(output / "benchmark.csv"),
            "html": str(output / "benchmark.html"), "system_info": str(output / "system-info.json")}
