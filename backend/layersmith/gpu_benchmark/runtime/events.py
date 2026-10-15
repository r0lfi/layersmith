"""Progress, log and result events shared by the CLI and the TUI.

Every benchmark reports through one emitter. On the command line the events
become readable log lines on stderr; with ``--events`` they are JSON lines on
stdout, which is how the TUI drives the very same CLI code in a subprocess.
Log lines are also appended to the run's ``logs/`` directory when one is set.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import threading
import time
from pathlib import Path


class StopRequested(Exception):
    """Raised inside a benchmark when the user asked it to stop."""


class Emitter:
    def __init__(self) -> None:
        self.json_mode = False
        self.log_path: Path | None = None
        self.stop_event = threading.Event()
        self.collected: list[dict] = []
        self._lock = threading.Lock()
        self._last_progress = 0.0

    def configure(self, json_mode: bool = False, log_path: Path | None = None) -> None:
        self.json_mode = json_mode
        self.log_path = log_path
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ output

    def _write_json(self, payload: dict) -> None:
        with self._lock:
            sys.stdout.write(json.dumps(payload, default=str) + "\n")
            sys.stdout.flush()

    def _append_log(self, line: str) -> None:
        if not self.log_path:
            return
        try:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass

    def log(self, message: str, level: str = "info", source: str = "gpu-bench") -> None:
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        line = f"{stamp} [{source}] {level.upper()}: {message}"
        self._append_log(line)
        if self.json_mode:
            self._write_json({"event": "log", "level": level, "source": source, "message": message})
        else:
            with self._lock:
                print(line, file=sys.stderr, flush=True)

    def progress(self, phase: str, force: bool = False, **data) -> None:
        """Live state; throttled on a terminal, always sent to the TUI."""
        if self.json_mode:
            self._write_json({"event": "progress", "phase": phase, **data})
            return
        now = time.monotonic()
        if not force and now - self._last_progress < 5:
            return
        self._last_progress = now
        summary = data.get("summary")
        if summary:
            with self._lock:
                print(f"  .. {phase}: {summary}", file=sys.stderr, flush=True)

    def result(self, entry: dict) -> None:
        """Every finished result passes here, so a stopped run keeps what it measured."""
        self.collected.append(entry)
        if self.json_mode:
            self._write_json({"event": "result", "entry": entry})

    def run_started(self, run_dir: Path, run_id: str) -> None:
        if self.json_mode:
            self._write_json({"event": "run", "run_dir": str(run_dir), "run_id": run_id})
        else:
            self.log(f"Run {run_id} -> {run_dir}")

    def done(self, status: str, paths: dict | None = None) -> None:
        if self.json_mode:
            self._write_json({"event": "done", "status": status, "paths": paths or {}})

    # ---------------------------------------------------------- stopping

    def check_stop(self) -> None:
        if self.stop_event.is_set():
            raise StopRequested("Stopped by user")

    @property
    def stopping(self) -> bool:
        return self.stop_event.is_set()


EMITTER = Emitter()
