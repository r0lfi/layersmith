"""Benchmark jobs: the TUI runs the gpu-bench CLI itself in a subprocess.

The CLI prints JSON events (--events); this module turns them into calls on
the app. The job gets its own process group, and the CLI gets SIGTERM if the
TUI dies, so stopping or quitting never leaves vLLM or nccl-tests behind.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parent.parent
ROOT_PACKAGE = (__package__ or "gpu_bench.tui").rsplit(".tui", 1)[0]
IMPORT_ROOT = RUNTIME_DIR.parents[len(ROOT_PACKAGE.split(".")) - 1]


def cli_command(*args: str) -> list[str]:
    return [sys.executable, "-m", f"{ROOT_PACKAGE}.cli", *args]


def _die_with_parent() -> None:
    try:
        import ctypes

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)
    except Exception:
        pass


class Job:
    """One running gpu-bench command; callbacks run on the reader thread."""

    def __init__(self, kind: str, args: list[str], on_event, on_exit, torchrun: list[str] | None = None):
        self.kind = kind
        self.args = args
        self.torchrun = torchrun
        self.on_event = on_event
        self.on_exit = on_exit
        self.process: subprocess.Popen | None = None
        self.started = time.monotonic()
        self.run_dir: str | None = None
        self.status = "RUNNING"
        self.stopping = False

    @property
    def command(self) -> list[str]:
        if self.torchrun is not None:
            return [sys.executable, "-m", "torch.distributed.run", *self.torchrun,
                    "-m", f"{ROOT_PACKAGE}.cli", *self.args, "--events"]
        return cli_command(*self.args, "--events")

    def start(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(IMPORT_ROOT), env.get("PYTHONPATH")]))
        env["PYTHONUNBUFFERED"] = "1"
        self.process = subprocess.Popen(self.command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                        bufsize=1, env=env, start_new_session=True, preexec_fn=_die_with_parent)
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                event = {"event": "log", "level": "output", "source": self.kind, "message": line}
            if event.get("event") == "run":
                self.run_dir = event.get("run_dir")
            if event.get("event") == "done":
                self.status = event.get("status") or self.status
            self.on_event(self, event)
        code = self.process.wait()
        if self.status == "RUNNING":
            self.status = "STOPPED" if self.stopping else ("PASSED" if code == 0 else "FAILED")
        self.on_exit(self, code)

    def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        for line in self.process.stderr:
            line = line.rstrip()
            if line:
                self.on_event(self, {"event": "log", "level": "stderr", "source": self.kind, "message": line})

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def stop(self, wait: float = 45) -> None:
        """Ask the CLI to stop (it stops vLLM/nccl-tests and saves partial results), then force."""
        self.stopping = True
        if not self.running:
            return
        assert self.process
        try:
            pgid = os.getpgid(self.process.pid)
        except OSError:
            return
        for sig, grace in ((signal.SIGTERM, wait), (signal.SIGKILL, 5)):
            try:
                os.killpg(pgid, sig)
            except OSError:
                return
            try:
                self.process.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                continue
