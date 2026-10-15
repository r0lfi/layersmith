"""NVIDIA GPU & LLM Benchmark Suite - the interactive terminal dashboard.

Works over plain SSH in any 80x24 or larger terminal. The live GPU view reads
NVML directly; every benchmark is the gpu-bench CLI run as a subprocess with
JSON events, so the TUI and scripted runs share one engine and one report
format. Only one benchmark runs at a time.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import (Button, Checkbox, ContentSwitcher, DataTable, Footer, Input, Label, OptionList,
                             ProgressBar, RichLog, Select, SelectionList, Sparkline, Static)
from textual.widgets.option_list import Option

from .. import llm, reports, runs
from ..benchmarks import DEFAULT_NCCL_TESTS, DEFAULT_PRECISIONS, NCCL_TESTS, PRECISIONS
from ..system import GpuMonitor, collect_system_info, health_checks
from .jobs import Job

PALETTE = {"background": "#0A0F14", "panel": "#0F1820", "raised": "#15212C", "border": "#2A3B4B",
           "text": "#E6EDF3", "muted": "#94A3B8", "green": "#76B900", "cyan": "#5CC8E6", "blue": "#4DA3FF",
           "success": "#6BCB8B", "warning": "#E9B75A", "error": "#F17878", "selected": "#1F3A1A",
           "hover": "#16262F"}

THEME = Theme(name="gpu-bench", primary=PALETTE["green"], secondary=PALETTE["cyan"], accent=PALETTE["cyan"],
              foreground=PALETTE["text"], background=PALETTE["background"], surface=PALETTE["panel"],
              panel=PALETTE["raised"], success=PALETTE["success"], warning=PALETTE["warning"],
              error=PALETTE["error"], dark=True,
              variables={"text-muted": PALETTE["muted"], "border": PALETTE["green"],
                         "border-blurred": PALETTE["border"], "block-cursor-background": PALETTE["selected"],
                         "block-cursor-foreground": PALETTE["text"], "block-cursor-text-style": "none",
                         "block-cursor-blurred-background": PALETTE["hover"],
                         "footer-background": PALETTE["background"], "footer-key-foreground": PALETTE["green"],
                         "footer-description-foreground": PALETTE["muted"], "footer-key-background": "transparent",
                         "input-cursor-background": PALETTE["green"], "gb-border": PALETTE["border"],
                         "gb-muted": PALETTE["muted"]})

NAVIGATION = [
    ("overview", "0", "Overview"),
    ("health", "1", "GPU Info & Health"),
    ("llm", "2", "LLM Token Speed"),
    ("compute", "3", "GPU Compute"),
    ("memory", "4", "Memory Bandwidth"),
    ("nccl", "5", "Multi-GPU / NCCL"),
    ("multinode", "6", "Multi-Node"),
    ("full", "7", "Full Benchmark"),
    None,
    ("results", "8", "Previous Results"),
    ("compare", "9", "Compare Benchmarks"),
    ("logs", "L", "Logs"),
]
STATUS_STYLE = {"PASSED": "success", "PASS": "success", "FAILED": "error", "FAIL": "error", "WARN": "warning",
                "STOPPED": "warning", "SKIPPED": "muted", "UNAVAILABLE": "muted", "N/A": "muted",
                "RUNNING": "cyan"}


def badge(status: str) -> Text:
    color = PALETTE.get(STATUS_STYLE.get(status, "muted"), PALETTE["muted"])
    return Text(f" {status} ", style=f"bold {color}")


def bar(fraction: float | None, width: int = 16, color: str = PALETTE["green"]) -> Text:
    if fraction is None:
        return Text("N/A".ljust(width), style=PALETTE["muted"])
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    return Text.assemble(("█" * filled, color), ("░" * (width - filled), PALETTE["border"]))


def gib(value) -> str:
    return "N/A" if value is None else f"{value / 2**30:,.1f} GiB"


def clock(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def muted(text: str) -> Text:
    return Text(text, style=PALETTE["muted"])


# ------------------------------------------------------------------ modals

class Confirm(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel"), Binding("y", "dismiss(True)", "Yes"),
                Binding("n", "dismiss(False)", "No")]

    def __init__(self, question: str, yes: str = "Yes"):
        super().__init__()
        self.question, self.yes = question, yes

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Static(self.question, id="confirm-text")
            with Horizontal(id="confirm-buttons"):
                yield Button(self.yes, variant="error", id="yes")
                yield Button("Cancel", id="no")

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


# ------------------------------------------------------------------- views

class Pane(VerticalScroll):
    """A page in the content area; refresh() is called when it is shown."""

    def refresh_view(self) -> None:
        pass


class GpuPicker(SelectionList):
    """All visible GPUs, all selected by default."""

    def fill(self, gpus: list[dict]) -> None:
        self.clear_options()
        self.border_subtitle = "" if gpus else "no GPU visible"
        for gpu in gpus:
            self.add_option((f"GPU {gpu['index']}  {gpu['name'].replace('NVIDIA ', '')}  {gib(gpu.get('memory_total_bytes'))}",
                             gpu["index"], True))

    def value_arg(self) -> str:
        selected = sorted(self.selected)
        return ",".join(map(str, selected)) if selected else ""


def field(label: str, widget, hint: str | None = None) -> Vertical:
    children = [Label(label, classes="field-label"), widget]
    if hint:
        children.append(Static(hint, classes="hint"))
    return Vertical(*children, classes="field")


class OverviewPane(Pane):
    def compose(self) -> ComposeResult:
        yield Static(id="overview-system", classes="card")
        yield Static("LIVE GPU STATUS", classes="section-title")
        yield DataTable(id="overview-gpus", cursor_type="none", zebra_stripes=True)
        yield Sparkline([0], id="overview-util", summary_function=max)
        yield Static(id="overview-util-label", classes="hint")
        yield Static(id="overview-hints", classes="card")

    def on_mount(self) -> None:
        table = self.query_one("#overview-gpus", DataTable)
        table.add_columns("GPU", "Model", "Util", "VRAM used", "Temp", "Power", "SM clock")

    def refresh_view(self) -> None:
        app: GpuBenchApp = self.app  # type: ignore[assignment]
        info = app.info
        gpus = info.get("gpus") or []
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style=PALETTE["muted"], no_wrap=True)
        grid.add_column()
        grid.add_column(style=PALETTE["muted"], no_wrap=True)
        grid.add_column()
        model = gpus[0]["name"] if gpus else "No NVIDIA GPU visible"
        vram = gib(gpus[0].get("memory_total_bytes")) if gpus else "N/A"
        grid.add_row("GPU model", Text(model, style=f"bold {PALETTE['green']}"), "Host", info.get("hostname") or "N/A")
        grid.add_row("GPUs detected", str(len(gpus)), "CPU", ((info.get("host") or {}).get("cpu_model") or "N/A")[:40])
        grid.add_row("VRAM per GPU", vram, "Driver", info.get("driver_version") or "N/A")
        grid.add_row("CUDA (PyTorch)", info.get("torch_cuda_version") or "N/A",
                     "CUDA (driver)", info.get("cuda_driver_version") or "N/A")
        grid.add_row("PyTorch", info.get("torch_version") or "N/A",
                     "vLLM", (info.get("packages") or {}).get("vllm") or "not installed")
        grid.add_row("NCCL", info.get("nccl_version") or "N/A", "RDMA",
                     ", ".join(d["name"] for d in info.get("rdma_devices") or []) or "none exposed")
        title = Text("SYSTEM OVERVIEW", style=f"bold {PALETTE['cyan']}")
        self.query_one("#overview-system", Static).update(Group(title, grid))
        hints = Text()
        if not gpus:
            hints.append("No GPU is visible. ", style=f"bold {PALETTE['warning']}")
            hints.append("Start the container with --device nvidia.com/gpu=all and check `nvidia-ctk cdi list` "
                         "on the host. Info, reports and comparisons still work.\n", style=PALETTE["muted"])
        models = app.models
        if models:
            hints.append(f"{len(models)} local model(s) under {app.model_root}. ", style=PALETTE["text"])
        else:
            hints.append(f"No local models under {app.model_root}: ", style=PALETTE["warning"])
            hints.append("LLM tests need -v /models:/models:ro. Compute, memory and NCCL tests work without.\n",
                         style=PALETTE["muted"])
        hints.append(f"Results are saved under {app.results_root}, one folder per run.", style=PALETTE["muted"])
        self.query_one("#overview-hints", Static).update(hints)
        self.update_live()

    def update_live(self) -> None:
        app: GpuBenchApp = self.app  # type: ignore[assignment]
        table = self.query_one("#overview-gpus", DataTable)
        table.clear()
        names = {g["index"]: g["name"].replace("NVIDIA ", "") for g in app.info.get("gpus") or []}
        for gpu in app.monitor.latest:
            used, total = gpu.get("memory_used_bytes"), gpu.get("memory_total_bytes")
            util = gpu.get("utilization_percent")
            table.add_row(str(gpu["index"]), names.get(gpu["index"], "?")[:28],
                          Text.assemble(bar(None if util is None else util / 100, 10), f" {util if util is not None else 'N/A'}%"),
                          Text.assemble(bar(used / total if used is not None and total else None, 10),
                                        f" {gib(used)}"),
                          f"{gpu.get('temperature_c', 'N/A')} C", f"{reports.fmt(gpu.get('power_watts'), 0)} W",
                          f"{gpu.get('sm_clock_mhz') or 'N/A'} MHz")
        if not app.monitor.latest:
            table.add_row("-", app.monitor.error or "No NVML data", "", "", "", "", "")
        history = [sum((g.get("utilization_percent") or 0) for g in s["gpus"]) / max(1, len(s["gpus"]))
                   for s in app.monitor.history] or [0]
        spark = self.query_one("#overview-util", Sparkline)
        # A flat series draws as a solid block; show it only once there is signal.
        spark.display = max(history) > 0
        spark.data = history[-90:]
        self.query_one("#overview-util-label", Static).update(
            f"Mean GPU utilisation, last {len(history)} s (peak {max(history):.0f}%)" if app.monitor.latest
            else "Mean GPU utilisation: N/A (no NVML data)")


class HealthPane(Pane):
    def compose(self) -> ComposeResult:
        with Horizontal(classes="toolbar"):
            yield Static("GPU INFORMATION & HEALTH", classes="section-title grow")
            yield Button("Refresh", id="health-refresh")
        yield DataTable(id="health-checks", cursor_type="none")
        yield Static("GPUs", classes="section-title")
        yield Static(id="health-gpus", classes="card")
        yield Static("Topology", classes="section-title")
        yield Static(id="health-topology", classes="card mono")
        yield Static("Network and RDMA", classes="section-title")
        yield Static(id="health-network", classes="card")

    def on_mount(self) -> None:
        self.query_one("#health-checks", DataTable).add_columns("Status", "Check", "Detail")

    @on(Button.Pressed, "#health-refresh")
    def _refresh(self) -> None:
        self.app.reload_info()  # type: ignore[attr-defined]

    def refresh_view(self) -> None:
        app: GpuBenchApp = self.app  # type: ignore[assignment]
        info = app.info
        table = self.query_one("#health-checks", DataTable)
        table.clear()
        for check in app.health["checks"]:
            table.add_row(badge(check["status"]), check["name"], Text(check["detail"], overflow="fold"))
        gpus = Table(box=None, header_style=f"bold {PALETTE['muted']}", pad_edge=False)
        for column in ("#", "Model", "UUID", "VRAM total/used", "Util", "Temp", "Power/limit", "Clock/max",
                       "PCIe", "NVLink", "MIG", "ECC (unc.)"):
            gpus.add_column(column, no_wrap=column not in ("Model", "UUID"))
        for gpu in info.get("gpus") or []:
            link = gpu.get("pcie_link") or {}
            ecc = gpu.get("ecc") or {}
            gpus.add_row(str(gpu["index"]), gpu["name"], Text(str(gpu.get("uuid") or "N/A"), style=PALETTE["muted"]),
                         f"{gib(gpu.get('memory_total_bytes'))} / {gib(gpu.get('memory_used_bytes'))}",
                         f"{gpu.get('utilization_percent', 'N/A')}%", f"{gpu.get('temperature_c', 'N/A')} C",
                         f"{reports.fmt(gpu.get('power_watts'), 0)} / {reports.fmt(gpu.get('power_limit_watts'), 0)} W",
                         f"{gpu.get('sm_clock_mhz') or 'N/A'} / {gpu.get('sm_clock_max_mhz') or 'N/A'}",
                         f"Gen{link.get('generation')} x{link.get('width')}" if link.get("generation") else "N/A",
                         str(len(gpu.get("nvlink") or [])) if gpu.get("nvlink") else "N/A",
                         (gpu.get("mig_mode") or {}).get("current", "N/A"),
                         f"{ecc.get('mode', 'N/A')} ({ecc.get('volatile_uncorrected', 'N/A')})" if ecc else "N/A")
        self.query_one("#health-gpus", Static).update(gpus if info.get("gpus") else muted(
            "No NVIDIA GPU visible. " + (info.get("nvml_error") or "")))
        topology = info.get("gpu_topology")
        if not topology and info.get("topology"):
            matrix = info["topology"]["matrix"]
            lines = ["       " + " ".join(f"GPU{i:<4}" for i in range(len(matrix)))]
            lines += [f"GPU{i:<4}" + " ".join(f"{c:<7}" for c in row) for i, row in enumerate(matrix)]
            topology = "\n".join(lines) + "\n\n" + info["topology"]["legend"]
        self.query_one("#health-topology", Static).update(
            Text(topology) if topology else muted("N/A: needs two or more GPUs."))
        net = Table(box=None, header_style=f"bold {PALETTE['muted']}")
        for column in ("Device", "Type", "Netdev", "Ports"):
            net.add_column(column)
        for device in info.get("rdma_devices") or []:
            net.add_row(device["name"], device.get("transport") or "N/A", ",".join(device.get("netdevs") or []),
                        "; ".join(f"{p['port']}: {p.get('state')} {p.get('rate')}" for p in device.get("ports") or []))
        interfaces = ", ".join(f"{i['name']} ({i.get('state')}, {i.get('speed_mbps') or '?'} Mb/s, MTU {i.get('mtu')})"
                               for i in info.get("network_interfaces") or [])
        self.query_one("#health-network", Static).update(Group(
            net if info.get("rdma_devices") else muted("RDMA: none exposed to the container (N/A)."),
            Text("Interfaces: " + (interfaces or "N/A"), style=PALETTE["muted"])))


class FormPane(Pane):
    """Shared start/stop handling for the benchmark pages."""

    kind = ""

    def start_args(self) -> list[str] | None:
        raise NotImplementedError

    @on(Button.Pressed, ".start")
    def _start(self) -> None:
        args = self.start_args()
        if args is not None:
            self.app.start_job(self.kind, args)  # type: ignore[attr-defined]

    @on(Button.Pressed, ".stop")
    def _stop(self) -> None:
        self.app.action_stop_job()  # type: ignore[attr-defined]

    def int_value(self, selector: str, name: str, minimum: int = 1) -> int | None:
        raw = self.query_one(selector, Input).value.strip()
        try:
            value = int(raw)
            if value < minimum:
                raise ValueError
            return value
        except ValueError:
            self.app.notify(f"{name} must be a whole number >= {minimum}.", severity="error")
            return None

    def gpus_arg(self, selector: str) -> str | None:
        value = self.query_one(selector, GpuPicker).value_arg()
        if not value:
            self.app.notify("Select at least one GPU.", severity="error")
            return None
        return value

    def results_table(self) -> DataTable:
        return self.query_one(f"#{self.kind}-results", DataTable)

    def on_result(self, entry: dict) -> None:
        pass

    def on_progress(self, event: dict) -> None:
        pass

    def on_log(self, text: Text) -> None:
        try:
            self.query_one(f"#{self.kind}-log", RichLog).write(text)
        except Exception:
            pass

    def on_started(self) -> None:
        pass

    def on_finished(self, status: str) -> None:
        pass


class LlmPane(FormPane):
    kind = "llm"

    def compose(self) -> ComposeResult:
        yield Static("LLM TOKEN SPEED", classes="section-title")
        yield Static(id="llm-models-note", classes="hint")
        with Horizontal(classes="form-row"):
            yield field("Model (from /models)", Select([], id="llm-model", prompt="Select a local model"))
            yield field("Precision", Select([(p, p) for p in ("auto", "bfloat16", "float16")], id="llm-precision",
                                            value="auto", allow_blank=False))
        with Horizontal(classes="form-row"):
            yield field("Input tokens", Input("1024", id="llm-input", type="integer"))
            yield field("Output tokens", Input("256", id="llm-output", type="integer"))
            yield field("Concurrency levels", Input("1,8,32", id="llm-concurrency"), "1 = single-request speed")
        with Horizontal(classes="form-row"):
            yield field("Requests per level", Input("64", id="llm-requests", type="integer"))
            yield field("Requests at c=1", Input("10", id="llm-single", type="integer"))
            yield field("Warmup requests", Input("4", id="llm-warmup", type="integer"))
            yield field("Tensor parallel", Input("", id="llm-tp", placeholder="auto = #GPUs"))
        yield field("GPUs", GpuPicker(id="llm-gpus"))
        with Horizontal(classes="toolbar"):
            yield Button("Start LLM benchmark", variant="success", classes="start", id="llm-start")
            yield Button("Stop", variant="error", classes="stop")
        yield Static("LIVE", classes="section-title")
        yield Static(id="llm-live", classes="card")
        yield ProgressBar(id="llm-progress", show_eta=False)
        yield Label("Output tokens/s, 5 s window", classes="hint")
        yield Sparkline([0], id="llm-spark", summary_function=max, classes="hidden-empty")
        yield Static("RESULTS", classes="section-title")
        yield DataTable(id="llm-results", cursor_type="row", zebra_stripes=True)
        yield RichLog(id="llm-log", max_lines=400, wrap=True, markup=False, classes="joblog")

    def on_mount(self) -> None:
        self.results_table().add_columns("Status", "Mode", "Conc.", "Done", "Aggregate tok/s",
                                         "Per-request tok/s", "TTFT P50", "TTFT P95", "TPOT P50", "GPU util",
                                         "VRAM peak", "Power")
        self.history: list[float] = []
        self.live_state: dict = {}
        self.paused = False
        self.query_one("#llm-live", Static).update(muted("Not running. Select a model and press Start."))

    def refresh_view(self) -> None:
        app: GpuBenchApp = self.app  # type: ignore[assignment]
        select = self.query_one("#llm-model", Select)
        current = select.value
        select.set_options([(f"{m['relative']}  ({reports.human_bytes(m['weight_bytes'])}"
                             f"{', ' + m['quantization'] if m.get('quantization') else ''})", m["path"])
                            for m in app.models])
        if current in [m["path"] for m in app.models]:
            select.value = current
        note = self.query_one("#llm-models-note", Static)
        if app.models:
            note.update(f"{len(app.models)} model(s) found under {app.model_root}. Nothing is ever downloaded.")
        else:
            note.update(Text(f"No local models found under {app.model_root}. Mount Hugging Face model folders with "
                             "-v /models:/models:ro and press F5. Compute, memory and NCCL tests still work.",
                             style=PALETTE["warning"]))
        picker = self.query_one("#llm-gpus", GpuPicker)
        if not picker.option_count:
            picker.fill(app.info.get("gpus") or [])

    def start_args(self) -> list[str] | None:
        model = self.query_one("#llm-model", Select).value
        if not isinstance(model, str):
            self.app.notify("Select a local model first.", severity="error")
            return None
        values = [self.int_value(s, n) for s, n in (("#llm-input", "Input tokens"), ("#llm-output", "Output tokens"),
                                                    ("#llm-requests", "Requests"), ("#llm-single", "Requests at 1"))]
        warmup = self.int_value("#llm-warmup", "Warmup", 0)
        gpus = self.gpus_arg("#llm-gpus")
        if None in values or warmup is None or gpus is None:
            return None
        concurrency = self.query_one("#llm-concurrency", Input).value.strip()
        if not concurrency or not all(p.strip().isdigit() and int(p) > 0 for p in concurrency.split(",")):
            self.app.notify("Concurrency must be like 1,8,32.", severity="error")
            return None
        args = ["llm", "--model", model, "--input-tokens", str(values[0]), "--output-tokens", str(values[1]),
                "--requests", str(values[2]), "--single-requests", str(values[3]), "--warmup-requests", str(warmup),
                "--concurrency", concurrency, "--precision", str(self.query_one("#llm-precision", Select).value),
                "--gpus", gpus]
        tp = self.query_one("#llm-tp", Input).value.strip()
        if tp:
            if not tp.isdigit():
                self.app.notify("Tensor parallel must be a number or empty.", severity="error")
                return None
            args += ["--tensor-parallel-size", tp]
        return args

    def on_started(self) -> None:
        self.history = []
        self.results_table().clear()
        self.query_one("#llm-progress", ProgressBar).update(total=None, progress=0)

    def on_progress(self, event: dict) -> None:
        if event.get("phase") == "llm-load":
            self.query_one("#llm-live", Static).update(Text(
                f"Loading model into vLLM… {clock(event.get('elapsed') or 0)}  (first start may compile kernels)",
                style=PALETTE["cyan"]))
            return
        if event.get("phase") != "llm":
            return
        self.live_state = event
        self.history.append(event.get("window_tokens_per_second") or 0)
        if self.paused:
            return
        self.render_live()

    def render_live(self) -> None:
        event = self.live_state
        app: GpuBenchApp = self.app  # type: ignore[assignment]
        gpus = event.get("gpus") or app.monitor.latest
        util = [g["utilization_percent"] for g in gpus if g.get("utilization_percent") is not None]
        memory = sum(g.get("memory_used_bytes") or 0 for g in gpus)
        power = sum(g.get("power_watts") or 0 for g in gpus)
        job = app.job
        model = self.query_one("#llm-model", Select).value
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style=PALETTE["muted"], no_wrap=True)
        grid.add_column(no_wrap=True)
        grid.add_column(style=PALETTE["muted"], no_wrap=True)
        grid.add_column(no_wrap=True)
        names = [g["name"].replace("NVIDIA ", "") for g in app.info.get("gpus") or []]
        grid.add_row("Model", str(model), "GPUs", f"{names[0] if names else 'N/A'} x{len(gpus)}")
        grid.add_row("Concurrency", f"{event.get('concurrency')} "
                     f"({'single request' if event.get('concurrency') == 1 else 'concurrent'})",
                     "Elapsed", clock(event.get("elapsed") or 0))
        grid.add_row("Completed", f"{event.get('completed')} / {event.get('total')}",
                     "In flight / failed", f"{event.get('in_flight')} / {event.get('failed')}")
        grid.add_row("Output throughput", Text(f"{event.get('window_tokens_per_second', 0):,.0f} tok/s",
                                               style=f"bold {PALETTE['green']}"),
                     "Output tokens", f"{event.get('output_tokens', 0):,}")
        grid.add_row("TTFT P50 / P95", f"{reports.fmt(event.get('running_ttft_p50_ms'))} / "
                                        f"{reports.fmt(event.get('running_ttft_p95_ms'))} ms",
                     "GPU util", Text.assemble(bar(sum(util) / len(util) / 100 if util else None, 10),
                                               f" {sum(util) / len(util):.0f}%" if util else ""))
        grid.add_row("VRAM in use", gib(memory) if gpus else "N/A", "Power draw", f"{power:,.0f} W" if gpus else "N/A")
        note = Text("Live values are running figures, not final results. Final results appear per level below.",
                    style=PALETTE["muted"])
        header = Text.assemble(("LIVE LLM BENCHMARK  ", f"bold {PALETTE['cyan']}"),
                               badge("PAUSED" if self.paused else (job.status if job else "RUNNING")))
        self.query_one("#llm-live", Static).update(Group(header, grid, note))
        total = event.get("total") or 0
        self.query_one("#llm-progress", ProgressBar).update(total=total or None, progress=event.get("completed") or 0)
        spark = self.query_one("#llm-spark", Sparkline)
        spark.display = max(self.history or [0]) > 0
        spark.data = self.history[-120:] or [0]

    def on_result(self, entry: dict) -> None:
        if entry.get("test") != "llm":
            return
        r, p, g = entry.get("results") or {}, entry.get("parameters") or {}, entry.get("gpu_sample") or {}
        self.results_table().add_row(
            badge(entry.get("status", "?")), "single" if p.get("concurrency") == 1 else "concurrent",
            str(p.get("concurrency", "-")), f"{r.get('completed_requests', '-')}/{p.get('requests', '-')}",
            Text(reports.fmt(r.get("aggregate_output_tokens_per_second")), style=f"bold {PALETTE['green']}"),
            reports.fmt((r.get("per_request_output_tokens_per_second") or {}).get("p50")),
            f"{reports.fmt((r.get('ttft_ms') or {}).get('p50'))} ms", f"{reports.fmt((r.get('ttft_ms') or {}).get('p95'))} ms",
            f"{reports.fmt((r.get('tpot_ms') or {}).get('p50'), 2)} ms",
            f"{reports.fmt(g.get('utilization_mean_percent'), 0)}%", reports.human_bytes(g.get("memory_peak_bytes_total")),
            f"{reports.fmt(g.get('power_mean_watts_total'), 0)} W")
        if entry.get("error"):
            self.on_log(Text(f"{entry.get('name')}: {entry['error']}", style=PALETTE["warning"]))

    def on_finished(self, status: str) -> None:
        self.query_one("#llm-live", Static).update(Group(
            Text.assemble(("LLM BENCHMARK  ", f"bold {PALETTE['cyan']}"), badge(status)),
            muted("Final results per concurrency level are in the table below and in the run report.")))


class ComputePane(FormPane):
    kind = "compute"

    def compose(self) -> ComposeResult:
        yield Static("GPU COMPUTE (GEMM)", classes="section-title")
        with Horizontal(classes="form-row"):
            yield field("Precisions", SelectionList(*[(p.upper(), p, p in DEFAULT_PRECISIONS) for p in PRECISIONS],
                                                    id="compute-precisions"), "FP8 needs Hopper/Ada/Blackwell")
            yield field("GPUs", GpuPicker(id="compute-gpus"))
        with Horizontal(classes="form-row"):
            yield field("Seconds per precision", Input("10", id="compute-duration", type="integer"))
            yield field("Matrix size", Input("8192", id="compute-size", type="integer"))
        with Horizontal(classes="toolbar"):
            yield Button("Start compute benchmark", variant="success", classes="start")
            yield Button("Stop", variant="error", classes="stop")
        yield ProgressBar(id="compute-progress", total=100, show_eta=False)
        yield DataTable(id="compute-results", cursor_type="row", zebra_stripes=True)
        yield Static("Measured with CUDA events after warmup; all selected GPUs run concurrently. Not datasheet "
                     "peaks.", classes="hint")
        yield RichLog(id="compute-log", max_lines=300, wrap=True, markup=False, classes="joblog")

    def on_mount(self) -> None:
        self.results_table().add_columns("Status", "Precision", "Matrix", "TFLOPS total", "TFLOPS / GPU",
                                         "Per GPU", "GPU util", "Power", "Note")

    def refresh_view(self) -> None:
        picker = self.query_one("#compute-gpus", GpuPicker)
        if not picker.option_count:
            picker.fill(self.app.info.get("gpus") or [])  # type: ignore[attr-defined]

    def start_args(self) -> list[str] | None:
        precisions = list(self.query_one("#compute-precisions", SelectionList).selected)
        duration = self.int_value("#compute-duration", "Duration")
        size = self.int_value("#compute-size", "Matrix size", 256)
        gpus = self.gpus_arg("#compute-gpus")
        if not precisions:
            self.app.notify("Select at least one precision.", severity="error")
            return None
        if None in (duration, size, gpus):
            return None
        return ["compute", "--precision", ",".join(precisions), "--duration", str(duration), "--size", str(size),
                "--gpus", str(gpus)]

    def on_started(self) -> None:
        self.results_table().clear()

    def on_progress(self, event: dict) -> None:
        if event.get("phase") == "compute":
            self.query_one("#compute-progress", ProgressBar).update(total=100, progress=100 * (event.get("fraction") or 0))

    def on_result(self, entry: dict) -> None:
        if entry.get("test") != "compute":
            return
        r, g = entry.get("results") or {}, entry.get("gpu_sample") or {}
        per = ", ".join(f"{x.get('gpu')}:{reports.fmt(x.get('measured_tflops'))}" for x in r.get("per_gpu") or []
                        if x.get("status") == "PASSED")
        self.results_table().add_row(badge(entry.get("status", "?")), str(r.get("precision", "")).upper(),
                                     str(r.get("matrix_size")),
                                     Text(reports.fmt(r.get("measured_tflops_total")), style=f"bold {PALETTE['green']}"),
                                     reports.fmt(r.get("measured_tflops_per_gpu_mean")), per or "-",
                                     f"{reports.fmt(g.get('utilization_mean_percent'), 0)}%",
                                     f"{reports.fmt(g.get('power_mean_watts_total'), 0)} W",
                                     Text((entry.get("error") or "")[:60], style=PALETTE["muted"]))


class MemoryPane(FormPane):
    kind = "memory"

    def compose(self) -> ComposeResult:
        yield Static("GPU MEMORY BANDWIDTH", classes="section-title")
        with Horizontal(classes="form-row"):
            yield field("GPUs", GpuPicker(id="memory-gpus"))
            with Vertical(classes="field"):
                yield field("Buffer MiB (empty = auto)", Input("", id="memory-size", type="integer"))
                yield Checkbox("GPU-to-GPU matrix", True, id="memory-peer")
        with Horizontal(classes="toolbar"):
            yield Button("Start memory benchmark", variant="success", classes="start")
            yield Button("Stop", variant="error", classes="stop")
        yield DataTable(id="memory-results", cursor_type="row", zebra_stripes=True)
        yield Static(id="memory-peers", classes="card")
        yield Static("VRAM/HBM figures are on-GPU; host<->device figures are the PCIe (or C2C) link; GPU<->GPU uses "
                     "NVLink or PCIe per topology. They are never combined. GB/s = 10^9 bytes/s.", classes="hint")
        yield RichLog(id="memory-log", max_lines=300, wrap=True, markup=False, classes="joblog")

    def on_mount(self) -> None:
        self.results_table().add_columns("GPU", "VRAM copy (r+w)", "VRAM read", "H2D pinned", "D2H pinned",
                                         "H2D pageable", "D2H pageable", "Buffer")

    def refresh_view(self) -> None:
        picker = self.query_one("#memory-gpus", GpuPicker)
        if not picker.option_count:
            picker.fill(self.app.info.get("gpus") or [])  # type: ignore[attr-defined]

    def start_args(self) -> list[str] | None:
        gpus = self.gpus_arg("#memory-gpus")
        if gpus is None:
            return None
        args = ["memory", "--gpus", gpus]
        if self.query_one("#memory-size", Input).value.strip():
            size = self.int_value("#memory-size", "Buffer", 64)
            if size is None:
                return None
            args += ["--size-mb", str(size)]
        if not self.query_one("#memory-peer", Checkbox).value:
            args.append("--no-peer")
        return args

    def on_started(self) -> None:
        self.results_table().clear()
        self.query_one("#memory-peers", Static).update("")

    def on_result(self, entry: dict) -> None:
        if entry.get("test") != "memory":
            return
        r = entry.get("results") or {}
        if entry.get("status") != "PASSED":
            self.results_table().add_row(badge(entry.get("status", "?")), entry.get("error") or "", "", "", "", "", "", "")
            return
        for g in r.get("per_gpu") or []:
            self.results_table().add_row(
                f"GPU {g.get('gpu')}", *[Text(f"{reports.fmt((g.get(k) or {}).get('gb_per_second'))} GB/s",
                                              style=f"bold {PALETTE['green']}" if k == "vram_copy" else "")
                                         for k in ("vram_copy", "vram_read", "host_to_device_pinned",
                                                   "device_to_host_pinned", "host_to_device_pageable",
                                                   "device_to_host_pageable")],
                reports.human_bytes(g.get("test_bytes")))
        peers = r.get("peer_to_peer")
        if peers:
            table = Table(title="GPU → GPU copy GB/s (row → column)", box=None, title_style=PALETTE["muted"])
            table.add_column("")
            for gpu in peers["gpus"]:
                table.add_column(f"GPU {gpu}", justify="right")
            for gpu, row, access in zip(peers["gpus"], peers["gb_per_second"], peers["peer_access"]):
                table.add_row(f"GPU {gpu}", *["—" if v is None else f"{v:,.1f}{'' if a == 'yes' else ' (host)'}"
                                              for v, a in zip(row, access)])
            self.query_one("#memory-peers", Static).update(table)
        nvbw = r.get("nvbandwidth") or {}
        if nvbw.get("testcases"):
            for case in nvbw["testcases"]:
                self.on_log(Text(f"nvbandwidth {case.get('name')}: {case.get('status')} "
                                 f"sum {reports.fmt(case.get('sum_gb_per_second'))} GB/s"))


class NcclPane(FormPane):
    kind = "nccl"

    def compose(self) -> ComposeResult:
        yield Static("MULTI-GPU / NCCL", classes="section-title")
        with Horizontal(classes="form-row"):
            yield field("Collectives", SelectionList(*[(t, t, t in DEFAULT_NCCL_TESTS) for t in NCCL_TESTS],
                                                     id="nccl-tests"))
            yield field("GPUs", GpuPicker(id="nccl-gpus"))
        with Horizontal(classes="form-row"):
            yield field("Min size", Input("8", id="nccl-min"))
            yield field("Max size", Input("1G", id="nccl-max"))
            yield field("Iterations", Input("20", id="nccl-iters", type="integer"))
        with Horizontal(classes="toolbar"):
            yield Button("Start NCCL tests", variant="success", classes="start")
            yield Button("Stop", variant="error", classes="stop")
        yield DataTable(id="nccl-results", cursor_type="row", zebra_stripes=True)
        yield Static("busbw applies each collective's correction factor; algbw = size / time. Neither is raw link "
                     "speed.", classes="hint")
        yield Static("GPU TOPOLOGY", classes="section-title")
        yield Static(id="nccl-topology", classes="card mono")
        yield RichLog(id="nccl-log", max_lines=600, wrap=False, markup=False, classes="joblog")

    def on_mount(self) -> None:
        self.results_table().add_columns("Status", "Collective", "GPUs", "Peak busbw", "Peak algbw", "At size",
                                         "Avg busbw", "Note")

    def refresh_view(self) -> None:
        app: GpuBenchApp = self.app  # type: ignore[assignment]
        picker = self.query_one("#nccl-gpus", GpuPicker)
        if not picker.option_count:
            picker.fill(app.info.get("gpus") or [])
        topology = app.info.get("gpu_topology")
        if not topology and app.info.get("topology"):
            topology = "\n".join(" ".join(f"{c:<6}" for c in row) for row in app.info["topology"]["matrix"])
        self.query_one("#nccl-topology", Static).update(Text(topology) if topology else muted(
            "N/A: one GPU or none visible. NCCL needs two or more GPUs here, or MPI across nodes (page 6)."))

    def start_args(self) -> list[str] | None:
        tests = list(self.query_one("#nccl-tests", SelectionList).selected)
        gpus = self.gpus_arg("#nccl-gpus")
        iterations = self.int_value("#nccl-iters", "Iterations")
        if not tests:
            self.app.notify("Select at least one collective.", severity="error")
            return None
        if gpus is None or iterations is None:
            return None
        return ["nccl", "--test", ",".join(tests), "--gpus", gpus, "--min-bytes",
                self.query_one("#nccl-min", Input).value.strip() or "8", "--max-bytes",
                self.query_one("#nccl-max", Input).value.strip() or "1G", "--iterations", str(iterations)]

    def on_started(self) -> None:
        self.results_table().clear()

    def on_result(self, entry: dict) -> None:
        if entry.get("test") != "nccl":
            return
        r, p = entry.get("results") or {}, entry.get("parameters") or {}
        self.results_table().add_row(badge(entry.get("status", "?")), str(p.get("collective") or entry.get("name")),
                                     str(p.get("gpus_per_rank") or "-"),
                                     Text(f"{reports.fmt(r.get('peak_busbw_gb_per_second'))} GB/s", style=f"bold {PALETTE['green']}"),
                                     f"{reports.fmt(r.get('peak_algbw_gb_per_second'))} GB/s",
                                     reports.human_bytes(r.get("peak_at_bytes")),
                                     f"{reports.fmt(r.get('avg_busbw_gb_per_second'))} GB/s",
                                     Text((entry.get("error") or "")[:60], style=PALETTE["muted"]))


class MultiNodePane(FormPane):
    kind = "multinode"

    def compose(self) -> ComposeResult:
        yield Static("MULTI-NODE BENCHMARK", classes="section-title")
        yield Static(id="multinode-env", classes="card")
        yield Static("torchrun (recommended): start the same command on every node; no MPI or SSH needed",
                     classes="section-title")
        with Horizontal(classes="form-row"):
            yield field("Nodes", Input("2", id="mn-nnodes", type="integer"))
            yield field("This node's rank", Input("0", id="mn-rank", type="integer"), "0 on the master node")
            yield field("Master address", Input("", id="mn-master", placeholder="node1 or its IP"))
            yield field("GPUs per node", Input("", id="mn-gpn", placeholder="all"))
        with Horizontal(classes="form-row"):
            yield field("Collectives", Input("all_reduce,all_gather,reduce_scatter", id="mn-colls"))
            yield field("Max size", Input("8G", id="mn-max"))
        with Horizontal(classes="toolbar"):
            yield Button("Start torchrun on this node", variant="success", id="mn-torchrun")
            yield Button("Stop", variant="error", classes="stop")
        yield Static(id="mn-command", classes="hint")
        yield Static("mpirun (clusters with OpenMPI and SSH between containers)", classes="section-title")
        with Horizontal(classes="form-row"):
            yield field("Hostfile (in container)", Input("", id="mn-hostfile", placeholder="/results/hostfile"))
            yield field("or hosts", Input("", id="mn-hosts", placeholder="node1:8,node2:8"))
            yield field("Total ranks", Input("16", id="mn-ranks", type="integer"))
            yield field("Extra mpirun args", Input("", id="mn-mpi", placeholder="-x NCCL_IB_HCA=mlx5_0"))
        with Horizontal(classes="toolbar"):
            yield Button("Start mpirun nccl-tests", variant="primary", classes="start")
        yield DataTable(id="multinode-results", cursor_type="row")
        yield Static(id="multinode-help", classes="card")
        yield RichLog(id="multinode-log", max_lines=600, wrap=False, markup=False, classes="joblog")

    def on_mount(self) -> None:
        self.results_table().add_columns("Status", "Collective", "Ranks", "Nodes", "Peak busbw", "Avg busbw",
                                         "NCCL network", "Note")
        home = Path(os.environ.get("GPU_BENCH_HOME", "/opt/gpu-bench"))
        self.query_one("#multinode-help", Static).update(Text(
            "Every node needs the same image, GPUs and RDMA devices exposed and host networking:\n"
            "  podman run --rm -it --network=host --ipc=host --device nvidia.com/gpu=all --device /dev/infiniband \\\n"
            "    --ulimit memlock=-1:-1 --cap-add=IPC_LOCK --security-opt=label=disable --shm-size=16g ...\n"
            f"Scripts for hosts and Slurm: {home}/scripts (podman-multinode.sh, slurm-dist-nccl.sbatch, "
            "slurm-nccl-mpi.sbatch, check-rdma.sh).\n"
            "Set NCCL_SOCKET_IFNAME, NCCL_IB_HCA and NCCL_IB_GID_INDEX (RoCE) for your fabric. The result shows "
            "which network NCCL really used: RDMA (IB verbs) or TCP sockets.\n\n"
            "Distributed LLM inference across nodes is not started here: vLLM needs its own Ray cluster spanning the "
            "nodes, and a working NCCL fabric does not imply that works. LLM tests run per node.",
            style=PALETTE["muted"]))

    def refresh_view(self) -> None:
        info = self.app.info  # type: ignore[attr-defined]
        env = Table.grid(padding=(0, 2))
        env.add_column(style=PALETTE["muted"])
        env.add_column()
        rdma = info.get("rdma_devices") or []
        env.add_row("RDMA devices", ", ".join(f"{d['name']} ({d.get('transport')}, "
                                              f"{'/'.join(str(p.get('state')) for p in d.get('ports') or [])})"
                                              for d in rdma) or Text("none exposed (N/A)", style=PALETTE["warning"]))
        env.add_row("uverbs visible", "yes" if any(Path("/dev/infiniband").glob("uverbs*")) else "no")
        env.add_row("Interfaces", ", ".join(i["name"] for i in info.get("network_interfaces") or []) or "N/A")
        for name in ("NCCL_SOCKET_IFNAME", "NCCL_IB_HCA", "NCCL_IB_GID_INDEX", "NCCL_NET_GDR_LEVEL", "NCCL_DEBUG",
                     "SLURM_JOB_ID"):
            env.add_row(name, os.environ.get(name) or muted("unset"))
        self.query_one("#multinode-env", Static).update(env)

    def torchrun_spec(self) -> tuple[list[str], list[str]] | None:
        nnodes = self.int_value("#mn-nnodes", "Nodes")
        rank = self.int_value("#mn-rank", "Node rank", 0)
        master = self.query_one("#mn-master", Input).value.strip()
        gpn = self.query_one("#mn-gpn", Input).value.strip() or str(len(self.app.info.get("gpus") or []))  # type: ignore[attr-defined]
        if nnodes is None or rank is None:
            return None
        if not master:
            self.app.notify("Set the master address (node with rank 0).", severity="error")
            return None
        if not gpn.isdigit() or int(gpn) < 1:
            self.app.notify("No GPUs visible on this node.", severity="error")
            return None
        if rank >= nnodes:
            self.app.notify("Node rank must be below the number of nodes.", severity="error")
            return None
        launcher = ["--nnodes", str(nnodes), "--node-rank", str(rank), "--nproc-per-node", gpn,
                    "--master-addr", master, "--master-port", "29500"]
        args = ["dist", "--test", self.query_one("#mn-colls", Input).value.strip() or "all",
                "--max-bytes", self.query_one("#mn-max", Input).value.strip() or "8G", "--label", f"multinode-{nnodes}n"]
        return launcher, args

    @on(Button.Pressed, "#mn-torchrun")
    def _torchrun(self) -> None:
        spec = self.torchrun_spec()
        if spec is None:
            return
        launcher, args = spec
        command = ("torchrun " + " ".join(launcher) + " /opt/gpu-bench/bin/gpu-bench " + " ".join(args))
        self.query_one("#mn-command", Static).update(
            "Run the same on the other nodes with their own --node-rank:\n  " + command)
        self.app.start_job(self.kind, args, torchrun=launcher)  # type: ignore[attr-defined]

    def start_args(self) -> list[str] | None:
        hostfile = self.query_one("#mn-hostfile", Input).value.strip()
        hosts = self.query_one("#mn-hosts", Input).value.strip()
        ranks = self.int_value("#mn-ranks", "Ranks")
        if not (hostfile or hosts):
            self.app.notify("Give a hostfile or a host list for mpirun.", severity="error")
            return None
        if ranks is None:
            return None
        args = ["nccl", "--test", self.query_one("#mn-colls", Input).value.strip() or "all", "--ranks", str(ranks),
                "--gpus-per-rank", "1", "--max-bytes", self.query_one("#mn-max", Input).value.strip() or "8G",
                "--label", "multinode-mpi"]
        args += ["--hostfile", hostfile] if hostfile else ["--hosts", hosts]
        extra = self.query_one("#mn-mpi", Input).value.strip()
        if extra:
            args += ["--mpi-args", extra]
        return args

    def on_started(self) -> None:
        self.results_table().clear()

    def on_result(self, entry: dict) -> None:
        if entry.get("test") != "nccl":
            return
        r, p = entry.get("results") or {}, entry.get("parameters") or {}
        network = (r.get("nccl_network") or {}).get("transport") or "not reported"
        self.results_table().add_row(badge(entry.get("status", "?")), str(p.get("collective")), str(p.get("ranks")),
                                     str(p.get("nodes") or "-"),
                                     f"{reports.fmt(r.get('peak_busbw_gb_per_second'))} GB/s",
                                     f"{reports.fmt(r.get('avg_busbw_gb_per_second'))} GB/s", network,
                                     Text((entry.get("error") or "")[:80], style=PALETTE["muted"]))


class FullPane(FormPane):
    kind = "full"
    STEPS = ("health", "compute", "memory", "nccl", "llm")

    def compose(self) -> ComposeResult:
        yield Static("FULL BENCHMARK", classes="section-title")
        yield Static("Health, compute (FP32/TF32/FP16/BF16), memory, NCCL (2+ GPUs) and, with a model, the LLM test "
                     "- all in one run folder and one report.", classes="hint")
        with Horizontal(classes="form-row"):
            yield field("Model (optional)", Select([], id="full-model", prompt="No LLM test"))
            yield field("Concurrency levels", Input("1,8,32", id="full-concurrency"))
        with Horizontal(classes="form-row"):
            yield field("Input tokens", Input("1024", id="full-input", type="integer"))
            yield field("Output tokens", Input("256", id="full-output", type="integer"))
            yield field("Compute seconds/precision", Input("10", id="full-duration", type="integer"))
        with Horizontal(classes="toolbar"):
            yield Button("Start full benchmark", variant="success", classes="start")
            yield Button("Stop", variant="error", classes="stop")
        yield Static(id="full-steps", classes="card")
        yield DataTable(id="full-results", cursor_type="row", zebra_stripes=True)
        yield RichLog(id="full-log", max_lines=600, wrap=True, markup=False, classes="joblog")

    def on_mount(self) -> None:
        self.results_table().add_columns("Status", "Test", "Headline", "Note")
        self.step_state = {step: "PENDING" for step in self.STEPS}
        self.render_steps()

    def render_steps(self) -> None:
        text = Text()
        for step in self.STEPS:
            text.append_text(badge(self.step_state[step]))
            text.append(f" {step}   ")
        self.query_one("#full-steps", Static).update(text)

    def refresh_view(self) -> None:
        app: GpuBenchApp = self.app  # type: ignore[assignment]
        select = self.query_one("#full-model", Select)
        current = select.value
        select.set_options([(m["relative"], m["path"]) for m in app.models])
        if current in [m["path"] for m in app.models]:
            select.value = current

    def start_args(self) -> list[str] | None:
        values = [self.int_value(s, n) for s, n in (("#full-input", "Input tokens"), ("#full-output", "Output tokens"),
                                                    ("#full-duration", "Duration"))]
        if None in values:
            return None
        args = ["full", "--input-tokens", str(values[0]), "--output-tokens", str(values[1]),
                "--compute-duration", str(values[2]),
                "--concurrency", self.query_one("#full-concurrency", Input).value.strip() or "1,8,32"]
        model = self.query_one("#full-model", Select).value
        if isinstance(model, str):
            args += ["--model", model]
        return args

    def on_started(self) -> None:
        self.results_table().clear()
        self.step_state = {step: "PENDING" for step in self.STEPS}
        self.render_steps()

    def on_progress(self, event: dict) -> None:
        if event.get("phase") == "full":
            step = event.get("step")
            for name in self.STEPS:
                if self.step_state[name] == "RUNNING":
                    self.step_state[name] = "DONE"
            if step in self.step_state:
                self.step_state[step] = "RUNNING"
            self.render_steps()

    def on_result(self, entry: dict) -> None:
        test = entry.get("test")
        metrics = reports.headline_metrics({"results": [entry]})
        headline = metrics[0] if metrics else None
        self.results_table().add_row(badge(entry.get("status", "?")), entry.get("name") or test or "?",
                                     f"{headline['label']}: {reports.fmt(headline['value'])} {headline['unit']}"
                                     if headline else "-", Text((entry.get("error") or "")[:70], style=PALETTE["muted"]))
        if test in self.step_state and entry.get("status") in ("FAILED", "SKIPPED", "UNAVAILABLE", "STOPPED"):
            self.step_state[test] = entry["status"]
            self.render_steps()

    def on_finished(self, status: str) -> None:
        for name in self.STEPS:
            if self.step_state[name] in ("RUNNING", "DONE"):
                self.step_state[name] = "PASSED" if status != "STOPPED" else "STOPPED"
        self.render_steps()


class ResultsPane(Pane):
    def compose(self) -> ComposeResult:
        with Horizontal(classes="toolbar"):
            yield Static("PREVIOUS RESULTS", classes="section-title grow")
            yield Button("Generate HTML report", id="results-report", variant="primary")
            yield Button("Refresh", id="results-refresh")
        yield DataTable(id="results-runs", cursor_type="row", zebra_stripes=True)
        yield Static(id="results-detail", classes="card")

    def on_mount(self) -> None:
        self.query_one("#results-runs", DataTable).add_columns("Run", "GPU", "Tests", "Passed", "Failed",
                                                              "Best tok/s", "Model")
        self.rows: list[dict] = []

    def refresh_view(self) -> None:
        app: GpuBenchApp = self.app  # type: ignore[assignment]
        table = self.query_one("#results-runs", DataTable)
        table.clear()
        self.rows = runs.list_runs(app.results_root)
        for row in self.rows:
            best = row.get("best_output_tokens_per_second")
            table.add_row(row["run_id"], f"{(row.get('gpu_model') or 'no GPU').replace('NVIDIA ', '')} x{row['gpu_count']}",
                          ",".join(row["tests"]), str(row["passed"]), str(row["failed"]),
                          reports.fmt(best) if best else "-", ",".join(m.rsplit("/", 1)[-1] for m in row["models"])[:30],
                          key=row["run_id"])
        if not self.rows:
            self.query_one("#results-detail", Static).update(muted(f"No runs under {app.results_root} yet."))
        elif table.row_count:
            self.show_detail(self.rows[0])

    def selected(self) -> dict | None:
        table = self.query_one("#results-runs", DataTable)
        if not self.rows or table.cursor_row is None or table.cursor_row >= len(self.rows):
            return None
        return self.rows[table.cursor_row]

    @on(DataTable.RowHighlighted, "#results-runs")
    def _highlight(self) -> None:
        row = self.selected()
        if row:
            self.show_detail(row)

    def show_detail(self, row: dict) -> None:
        try:
            data = runs.load(row["path"])
        except runs.RunError as exc:
            self.query_one("#results-detail", Static).update(Text(str(exc), style=PALETTE["error"]))
            return
        table = Table(box=None, header_style=f"bold {PALETTE['muted']}")
        table.add_column("Metric")
        table.add_column("Value", justify="right")
        table.add_column("Unit", style=PALETTE["muted"])
        for metric in reports.headline_metrics(data):
            table.add_row(metric["label"], reports.fmt(metric["value"]), metric["unit"])
        statuses = Text()
        for entry in data.get("results", []):
            statuses.append_text(badge(entry.get("status", "?")))
            statuses.append(f" {entry.get('name') or entry.get('test')}  ")
        self.query_one("#results-detail", Static).update(Group(
            Text(f"{row['run_id']}  ·  {row['path']}", style=f"bold {PALETTE['cyan']}"), statuses, table,
            muted(f"Report: {row['path']}/benchmark.html  (open it on the host from the mounted results folder)")))

    @on(Button.Pressed, "#results-refresh")
    def _refresh(self) -> None:
        self.refresh_view()

    @on(Button.Pressed, "#results-report")
    def _report(self) -> None:
        self.app.action_report()  # type: ignore[attr-defined]


class ComparePane(Pane):
    def compose(self) -> ComposeResult:
        with Horizontal(classes="toolbar"):
            yield Static("COMPARE BENCHMARKS", classes="section-title grow")
            yield Button("Compare selected", id="compare-run", variant="primary")
        yield Static("Select two or more runs; the first selected (oldest-listed last) is the baseline.", classes="hint")
        yield SelectionList(id="compare-runs")
        yield Static(id="compare-warnings")
        yield DataTable(id="compare-table", cursor_type="row", zebra_stripes=True)
        yield Static(id="compare-paths", classes="hint")

    def refresh_view(self) -> None:
        app: GpuBenchApp = self.app  # type: ignore[assignment]
        selection = self.query_one("#compare-runs", SelectionList)
        chosen = set(selection.selected)
        selection.clear_options()
        for row in runs.list_runs(app.results_root):
            best = row.get("best_output_tokens_per_second")
            selection.add_option((f"{row['run_id']}  {(row.get('gpu_model') or 'no GPU').replace('NVIDIA ', '')} "
                                  f"x{row['gpu_count']}  {','.join(row['tests'])}"
                                  + (f"  {best:,.0f} tok/s" if best else ""), row["path"], row["path"] in chosen))

    @on(Button.Pressed, "#compare-run")
    def _compare(self) -> None:
        app: GpuBenchApp = self.app  # type: ignore[assignment]
        paths = list(self.query_one("#compare-runs", SelectionList).selected)
        if len(paths) < 2:
            app.notify("Select at least two runs.", severity="warning")
            return
        try:
            loaded = [runs.load(path) for path in paths]
            comparison = reports.compare_runs(loaded)
            target = Path(app.results_root) / "comparisons" / time.strftime("%Y-%m-%d_%H%M%S")
            written = reports.write_comparison(comparison, target)
        except (runs.RunError, ValueError, OSError) as exc:
            app.notify(str(exc), severity="error")
            return
        warnings = Text()
        for warning in comparison["warnings"]:
            warnings.append("⚠ " + warning + "\n", style=PALETTE["warning"])
        self.query_one("#compare-warnings", Static).update(warnings)
        table = self.query_one("#compare-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Metric", *[f"{r['label']}\n{r['run_id'][:17]}" for r in comparison["runs"]], "Note")
        for row in comparison["rows"]:
            cells = []
            for value, diff in zip(row["values"], row["diff_percent"]):
                cell = Text(f"{reports.fmt(value)} {row['unit']}")
                if diff is not None:
                    good = (diff > 0) == (row["better"] == "higher")
                    cell.append(f" {diff:+.1f}%", style=PALETTE["success"] if good else PALETTE["error"])
                cells.append(cell)
            table.add_row(row["label"], *cells, Text(row["note"] or "", style=PALETTE["warning"]))
        self.query_one("#compare-paths", Static).update(f"Saved: {written['html']}  ·  {written['csv']}")


class LogsPane(Pane):
    def compose(self) -> ComposeResult:
        yield Static("LOGS", classes="section-title")
        yield RichLog(id="logs-all", max_lines=5000, wrap=True, markup=False)


# --------------------------------------------------------------------- app

PANES = {"overview": OverviewPane, "health": HealthPane, "llm": LlmPane, "compute": ComputePane,
         "memory": MemoryPane, "nccl": NcclPane, "multinode": MultiNodePane, "full": FullPane,
         "results": ResultsPane, "compare": ComparePane, "logs": LogsPane}


class GpuBenchApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "NVIDIA GPU & LLM Benchmark Suite"
    BINDINGS = [
        *[Binding(key.lower(), f"show('{view}')", label, show=False) for view, key, label in
          [entry for entry in NAVIGATION if entry]],
        Binding("r", "report", "HTML report"),
        Binding("s", "stop_job", "Stop test"),
        Binding("p", "pause", "Pause display"),
        Binding("f5", "reload", "Rescan"),
        Binding("q", "quit_app", "Quit"),
        Binding("ctrl+c", "stop_or_quit", "Stop/Quit", show=False, priority=True),
    ]

    def __init__(self, results: str = runs.DEFAULT_RESULTS, models: str = llm.MODEL_ROOT):
        super().__init__()
        self.results_root = results
        self.model_root = models
        self.info: dict = {"gpus": []}
        self.health: dict = {"checks": [], "status": "N/A"}
        self.models: list[dict] = []
        self.monitor = GpuMonitor(interval=1.0, history=300)
        self.job: Job | None = None
        self.job_pane: FormPane | None = None
        self.last_run_dir: str | None = None

    def get_theme_variable_defaults(self) -> dict[str, str]:
        return {"gb-border": PALETTE["border"], "gb-muted": PALETTE["muted"]}

    def compose(self) -> ComposeResult:
        with Horizontal(id="header"):
            yield Static(Text.assemble(("▐█▌ ", f"bold {PALETTE['green']}"),
                                       ("NVIDIA GPU & LLM BENCHMARK SUITE", f"bold {PALETTE['text']}"),
                                       ("\n    LayerSmith Edition", PALETTE["muted"])), id="title")
            yield Static("", id="job-state")
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                options: list = []
                for entry in NAVIGATION:
                    if entry is None:
                        options.append(None)
                    else:
                        view, key, label = entry
                        options.append(Option(Text.assemble((f"[{key}] ", PALETTE["green"]), label), id=view))
                options += [None, Option(Text.assemble(("[R] ", PALETTE["green"]), "HTML report"), id="report"),
                            Option(Text.assemble(("[Q] ", PALETTE["green"]), "Quit"), id="quit")]
                yield OptionList(*options, id="nav")
            with ContentSwitcher(initial="overview", id="content"):
                for view, pane in PANES.items():
                    yield pane(id=view)
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(THEME)
        self.theme = "gpu-bench"
        self.monitor.start()
        self.reload_info()
        self.set_interval(1.0, self._tick)
        self.log_line(Text(f"gpu-bench TUI started. Results: {self.results_root}  Models: {self.model_root}"))

    # ----------------------------------------------------------- data

    @work(thread=True, exclusive=True, group="info")
    def reload_info(self) -> None:
        info = collect_system_info()
        health = health_checks(info)
        models = llm.discover_models(self.model_root)
        self.call_from_thread(self._apply_info, info, health, models)

    def _apply_info(self, info: dict, health: dict, models: list[dict]) -> None:
        self.info, self.health, self.models = info, health, models
        for pane in self.query(Pane):
            try:
                pane.refresh_view()
            except Exception as exc:  # one broken view must not take the app down
                self.log_line(Text(f"view {pane.id}: {exc}", style=PALETTE["error"]))

    def _tick(self) -> None:
        content = self.query_one("#content", ContentSwitcher)
        if content.current == "overview":
            self.query_one(OverviewPane).update_live()
        state = Text(justify="right")
        if self.job and self.job.running:
            state.append_text(badge("STOPPING" if self.job.stopping else "RUNNING"))
            state.append(f" {self.job.kind}  {clock(self.job.elapsed())}\n", style=PALETTE["text"])
        elif self.job:
            state.append_text(badge(self.job.status))
            state.append(f" last: {self.job.kind}\n", style=PALETTE["muted"])
        else:
            state.append("idle\n", style=PALETTE["muted"])
        gpus = self.monitor.latest
        util = [g["utilization_percent"] for g in gpus if g.get("utilization_percent") is not None]
        state.append(f"{len(self.info.get('gpus') or [])} GPU(s)", style=PALETTE["muted"])
        if util:
            state.append(f"  util {sum(util) / len(util):.0f}%  {sum(g.get('power_watts') or 0 for g in gpus):,.0f} W",
                         style=PALETTE["muted"])
        self.query_one("#job-state", Static).update(state)

    def log_line(self, text: Text) -> None:
        stamp = time.strftime("%H:%M:%S ")
        self.query_one("#logs-all", RichLog).write(Text.assemble((stamp, PALETTE["muted"]), text))

    # -------------------------------------------------------- navigation

    def action_show(self, view: str) -> None:
        if view not in PANES:
            return
        self.query_one("#content", ContentSwitcher).current = view
        pane = self.query_one(f"#{view}", Pane)
        if view in ("results", "compare", "overview"):
            pane.refresh_view()
        nav = self.query_one("#nav", OptionList)
        try:
            nav.highlighted = nav.get_option_index(view)
        except Exception:
            pass

    @on(OptionList.OptionSelected, "#nav")
    def _nav(self, event: OptionList.OptionSelected) -> None:
        target = event.option.id
        if target == "report":
            self.action_report()
        elif target == "quit":
            self.action_quit_app()
        elif target:
            self.action_show(target)

    def action_reload(self) -> None:
        self.reload_info()
        self.notify("Rescanning GPUs and /models…", timeout=2)

    def action_pause(self) -> None:
        pane = self.query_one(LlmPane)
        pane.paused = not pane.paused
        if not pane.paused and pane.live_state:
            pane.render_live()
        self.notify("Live display paused (measurement continues)" if pane.paused else "Live display resumed", timeout=2)

    def action_report(self) -> None:
        target = None
        content = self.query_one("#content", ContentSwitcher)
        if content.current == "results":
            row = self.query_one(ResultsPane).selected()
            target = row["path"] if row else None
        target = target or self.last_run_dir
        if not target:
            rows = runs.list_runs(self.results_root)
            target = rows[0]["path"] if rows else None
        if not target:
            self.notify("No run to report on yet.", severity="warning")
            return
        try:
            paths = reports.write_run(runs.load(target), target)
        except (runs.RunError, OSError) as exc:
            self.notify(f"Report failed: {exc}", severity="error")
            return
        self.notify(f"HTML report written: {paths['html']}", timeout=8)
        self.log_line(Text(f"Report: {paths['html']}", style=PALETTE["success"]))

    # -------------------------------------------------------------- jobs

    def start_job(self, kind: str, args: list[str], torchrun: list[str] | None = None) -> None:
        if self.job and self.job.running:
            self.notify(f"A {self.job.kind} benchmark is already running. Stop it first (S).", severity="warning")
            return
        args = [*args, "--output", self.results_root]
        pane = self.query_one(f"#{kind}", FormPane)
        self.job_pane = pane
        self.job = Job(kind, args, self._event_from_thread, self._exit_from_thread, torchrun=torchrun)
        self.log_line(Text("$ " + " ".join(self.job.command), style=PALETTE["cyan"]))
        pane.on_started()
        try:
            self.job.start()
        except OSError as exc:
            self.notify(f"Could not start: {exc}", severity="error")
            self.job = None
            return
        self.notify(f"{kind} benchmark started", timeout=3)

    def _event_from_thread(self, job: Job, event: dict) -> None:
        self.call_from_thread(self._handle_event, job, event)

    def _exit_from_thread(self, job: Job, code: int) -> None:
        self.call_from_thread(self._handle_exit, job, code)

    def _handle_event(self, job: Job, event: dict) -> None:
        pane = self.job_pane
        kind = event.get("event")
        if kind == "log":
            level = event.get("level")
            style = {"error": PALETTE["error"], "warning": PALETTE["warning"], "stderr": PALETTE["muted"],
                     "output": PALETTE["text"]}.get(level, PALETTE["cyan"])
            text = Text(f"[{event.get('source')}] {event.get('message')}", style=style)
            self.log_line(text)
            if pane and level != "stderr":
                pane.on_log(text)
        elif kind == "progress" and pane:
            pane.on_progress(event)
        elif kind == "result" and pane:
            pane.on_result(event.get("entry") or {})
        elif kind == "run":
            self.last_run_dir = event.get("run_dir")
            self.log_line(Text(f"Run folder: {self.last_run_dir}", style=PALETTE["success"]))
        elif kind == "done":
            html = (event.get("paths") or {}).get("html")
            if html:
                self.log_line(Text(f"Report: {html}", style=PALETTE["success"]))

    def _handle_exit(self, job: Job, code: int) -> None:
        status = job.status
        if self.job_pane:
            self.job_pane.on_finished(status)
        severity = "information" if status == "PASSED" else ("warning" if status == "STOPPED" else "error")
        self.notify(f"{job.kind} finished: {status}" + (f" — {job.run_dir}" if job.run_dir else ""),
                    severity=severity, timeout=8)
        self.log_line(Text(f"{job.kind} exited {code} ({status})", style=PALETTE["muted"]))
        self.query_one(ResultsPane).refresh_view()
        self.query_one(ComparePane).refresh_view()

    def action_stop_job(self) -> None:
        if not (self.job and self.job.running):
            self.notify("No benchmark is running.", timeout=2)
            return

        def answer(yes: bool | None) -> None:
            if yes and self.job and self.job.running:
                self.notify("Stopping… partial results are saved and marked STOPPED.", timeout=4)
                self._stop_worker(self.job)

        self.push_screen(Confirm(f"Stop the running {self.job.kind} benchmark?", "Stop"), answer)

    @work(thread=True, group="stop")
    def _stop_worker(self, job: Job) -> None:
        job.stop()

    def action_quit_app(self) -> None:
        if self.job and self.job.running:
            def answer(yes: bool | None) -> None:
                if yes:
                    self._quit_after_stop()

            self.push_screen(Confirm("A benchmark is running. Stop it and quit?", "Stop and quit"), answer)
        else:
            self.exit(0)

    @work(thread=True, group="stop")
    def _quit_after_stop(self) -> None:
        if self.job:
            self.job.stop()
        self.call_from_thread(self.exit, 0)

    def action_stop_or_quit(self) -> None:
        if self.job and self.job.running:
            self.action_stop_job()
        else:
            self.action_quit_app()

    def on_unmount(self) -> None:
        self.monitor.stop()
        if self.job and self.job.running:
            self.job.stop(wait=20)


def run_tui(results: str = runs.DEFAULT_RESULTS, models: str = llm.MODEL_ROOT) -> int:
    app = GpuBenchApp(results=results, models=models)
    try:
        app.run()
    finally:
        if app.job and app.job.running:
            app.job.stop(wait=20)
    return 0
