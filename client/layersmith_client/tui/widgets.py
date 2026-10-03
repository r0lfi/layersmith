"""Small building blocks for the TUI: panels, fact lists and dialogs."""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Optional

from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from rich.markup import escape
from textual.widgets import Button, DataTable, Input, Label, ProgressBar, RichLog, Static

from layersmith_client import ops
from layersmith_client.api import ClientError
from layersmith_client.text import BUILD_TONE, clean, format_bytes
from layersmith_client.tui.theme import STYLE, PALETTE, TONE_COLOUR, log_line, safe


def facts(rows: list[tuple[str, object]], label_style: str = PALETTE["muted"]) -> Table:
    """Label/value pairs as a borderless two-column table. Values are untrusted."""
    table = Table.grid(padding=(0, 2))
    table.add_column(style=label_style, no_wrap=True)
    table.add_column(overflow="fold")
    for label, value in rows:
        if isinstance(value, Text):
            table.add_row(label, value)
        else:
            table.add_row(label, safe(value if value not in (None, "") else "-"))
    return table


def section(title: str) -> Text:
    """A small heading inside a panel."""
    return Text(title.upper(), style=f"bold {PALETTE['cyan']}")


class Panel(Vertical):
    """A bordered section with a title, the basic unit of every view."""

    def __init__(self, *children, title: str = "", **kwargs):
        super().__init__(*children, **kwargs)
        self.add_class("panel")
        self.border_title = title

    def set_title(self, title: str, note: str = "") -> None:
        """Title in the frame; the note in muted text after it. Both are escaped."""
        muted = PALETTE["muted"]
        self.border_title = f"[b]{escape(title)}[/b]" + (f" [{muted}]{escape(note)}[/]" if note else "")


class LsTable(DataTable):
    """A row-select table that keeps each cell's own colour on the selected row,
    so a FAILED status stays red when it is the selected one."""

    BINDINGS = [Binding("enter", "select_cursor", "Open")]

    def __init__(self, *columns: str, id: str | None = None):
        super().__init__(id=id, cursor_type="row", zebra_stripes=False, cursor_foreground_priority="renderable")
        for column in columns:
            self.add_column(column, key=column)

    def selected_key(self) -> str | None:
        if not self.row_count:
            return None
        try:
            return str(self.coordinate_to_cell_key(self.cursor_coordinate).row_key.value)
        except Exception:
            return None

    def select_key(self, key: str | None) -> None:
        if key is None:
            return
        try:
            self.move_cursor(row=self.get_row_index(key))
        except Exception:
            pass


def right(value: object) -> Text:
    return Text(clean(value), justify="right")


class LogPanel(Vertical):
    """The log of one build, followed live, in a titled panel.

    The title always names the project, build and version the lines belong
    to. New lines scroll the view only while it follows; scrolling up pauses
    following until `f` resumes it. Reads run in a worker, one at a time, and
    at most LOG_BUFFER_LINES lines are kept.
    """

    BINDINGS = [Binding("f", "follow", "Follow log")]
    can_focus = False

    def __init__(self, *, id: str | None = None, classes: str = "", title: str = "Log"):
        super().__init__(id=id, classes=f"panel log-panel {classes}".strip())
        self.base_title = title
        self.follower: ops.LogFollower | None = None
        self.build: dict | None = None
        self.label = ""
        self.following = True
        self.busy = False
        #: Plain copy of what is shown, for tests and for copying.
        self.plain: deque = deque(maxlen=ops.LOG_BUFFER_LINES)

    def compose(self):
        yield RichLog(max_lines=ops.LOG_BUFFER_LINES, wrap=False, markup=False, highlight=False,
                      auto_scroll=True)

    def on_mount(self) -> None:
        self.set_interval(3.0, self.poll)
        self.show_empty("Select a build to see its log.")

    @property
    def view(self) -> RichLog:
        return self.query_one(RichLog)

    def _title(self) -> None:
        if not self.build:
            self.border_title = f"[b]{escape(self.base_title)}[/b]"
            return
        status = (self.follower.status if self.follower and self.follower.status else self.build.get("status")) \
            or "unknown"
        colour = PALETTE[TONE_COLOUR.get(BUILD_TONE.get(status, "muted"), "muted")]
        state = "following" if self.following else "paused - f to follow"
        self.border_title = (f"[b]{escape(self.base_title)}[/b] [{PALETTE['muted']}]{escape(self.label)}[/] "
                             f"[b {colour}]{escape(status.upper())}[/] [{PALETTE['muted']}]{state}[/]")

    def show_empty(self, message: str) -> None:
        self.follower, self.build = None, None
        self.view.clear()
        self.plain.clear()
        self.view.write(Text(message, style=PALETTE["muted"]))
        self.placeholder = True
        self._title()

    def show_build(self, app, build: dict | None, label: str, tail_bytes: int | None = ops.LOG_TAIL_BYTES) -> None:
        """Follow this build's log. The same build again changes nothing."""
        if build is None:
            return
        if self.build and self.build.get("id") == build.get("id"):
            self.build = build
            self._title()
            return
        self.build, self.label = build, label
        self.view.clear()
        self.plain.clear()
        self.following = True
        self.view.auto_scroll = True
        self.follower = ops.LogFollower(app.client, build["id"], tail_bytes=tail_bytes) if app.client else None
        self.busy = False
        self.placeholder = True
        self.view.write(Text("No log output yet." if build.get("status") in ("queued", "preparing")
                             else "Reading the log from the server...", style=PALETTE["muted"]))
        self._title()
        self.poll()

    def poll(self) -> None:
        follower = self.follower
        if follower is None or follower.finished or self.busy or not self.display:
            return
        if self.app.screen is not self.screen:
            return
        self.busy = True

        def fetch(_client):
            return follower, list(follower.poll())

        def failed(exc) -> None:
            self.busy = False
            self.border_title = f"[b]{escape(self.base_title)}[/b] [b {PALETTE['error']}]CONNECTION LOST[/] " \
                                f"[{PALETTE['muted']}]retrying; the build continues on the server[/]"

        self.app.run_api(fetch, self._lines, failed=failed, group=f"log-{self.id}", exclusive=False)

    def _lines(self, result) -> None:
        self.busy = False
        follower, lines = result
        if follower is not self.follower:
            return
        view = self.view
        if lines and getattr(self, "placeholder", False):
            view.clear()
            self.placeholder = False
        if lines:
            if self.following and view.max_scroll_y > 0 and not view.is_vertical_scroll_end:
                self.following = False  # the reader scrolled back: do not pull them down
            view.auto_scroll = self.following
            for line in lines:
                self.plain.append(clean(line))
                view.write(log_line(line), scroll_end=self.following)
        self._title()

    def action_follow(self) -> None:
        self.following = True
        self.view.auto_scroll = True
        self.view.scroll_end(animate=False)
        self._title()


class StatTile(Static):
    """A key figure: muted label above a bold value."""

    DEFAULT_CLASSES = "stat"

    def __init__(self, label: str, **kwargs):
        super().__init__("", **kwargs)
        self.add_class("stat")
        self.label = label
        self.show("-")

    def show(self, value: object, note: str = "") -> None:
        text = Text(self.label, style=PALETTE["muted"])
        text.append("\n")
        text.append(clean(value), style=f"bold {PALETTE['text']}")
        if note:
            text.append(f"  {note}", style=PALETTE["muted"])
        self.update(text)


class ConfirmScreen(ModalScreen[bool]):
    """Yes/no question. Escape and "No" both answer no."""

    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel")]

    def __init__(self, question: str, confirm_label: str = "Confirm", danger: bool = True):
        super().__init__()
        self.question, self.confirm_label, self.danger = question, confirm_label, danger

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(safe(self.question), classes="dialog-text")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="no")
                yield Button(self.confirm_label, id="yes", variant="error" if self.danger else "primary")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    @on(Button.Pressed)
    def answer(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class PromptScreen(ModalScreen[Optional[str]]):
    """One line of input. Enter submits, Escape cancels."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, question: str, value: str = "", placeholder: str = "", hint: str = ""):
        super().__init__()
        self.question, self.value, self.placeholder, self.hint = question, value, placeholder, hint

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.question, classes="dialog-title")
            yield Input(value=self.value, placeholder=self.placeholder, id="answer")
            if self.hint:
                yield Static(safe(self.hint), classes="hint")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("OK", id="ok", variant="primary")

    @on(Input.Submitted)
    def submitted(self) -> None:
        self.dismiss(self.query_one("#answer", Input).value)

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.query_one("#answer", Input).value if event.button.id == "ok" else None)


class TextScreen(ModalScreen[None]):
    """Read-only text (a Containerfile, a CLI command, a report). Escape closes."""

    BINDINGS = [Binding("escape", "dismiss", "Close")]

    def __init__(self, title: str, text: str):
        super().__init__()
        self.title_text, self.text = title, text

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label(self.title_text, classes="dialog-title")
            with VerticalScroll(classes="dialog-scroll"):
                yield Static(Text(clean(self.text, keep_newlines=True)), classes="code")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Close", id="close", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#close", Button).focus()

    @on(Button.Pressed)
    def close(self) -> None:
        self.dismiss(None)


class DownloadScreen(ModalScreen[Optional[dict]]):
    """Choose a local destination, then download with progress.

    The path is on the machine running the client. The transfer runs in a
    worker thread so the interface stays responsive; closing the dialog
    while it runs is not offered, because a half-written file is discarded
    anyway and the dialog shows why it failed.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, default_path: str, run_download, what: str):
        super().__init__()
        self.title_text, self.default_path, self.run_download, self.what = title, default_path, run_download, what
        self.running = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label(self.title_text, classes="dialog-title")
            yield Static(f"Saved on THIS machine. A file or a directory; an existing file is not replaced "
                         f"unless you confirm it.", classes="hint")
            yield Input(value=self.default_path, id="path")
            yield ProgressBar(id="progress", show_eta=False)
            yield Static("", id="status")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Download", id="go", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#progress", ProgressBar).display = False
        self.query_one("#path", Input).focus()

    def action_cancel(self) -> None:
        if not self.running:
            self.dismiss(None)

    @on(Input.Submitted)
    @on(Button.Pressed, "#go")
    def start(self) -> None:
        if self.running:
            return
        path = Path(self.query_one("#path", Input).value.strip() or ".").expanduser()
        target = path
        if path.is_dir():
            target = None  # the server's name is used; existence checked during download
        if target is not None and target.exists():
            self.app.push_screen(ConfirmScreen(f"{target} exists on this machine. Replace it?", "Replace"),
                                 lambda yes: self._go(path, overwrite=True) if yes else None)
            return
        self._go(path, overwrite=False)

    @on(Button.Pressed, "#cancel")
    def cancel_pressed(self) -> None:
        self.action_cancel()

    def _go(self, path: Path, overwrite: bool) -> None:
        self.running = True
        self.query_one("#go", Button).disabled = True
        self.query_one("#cancel", Button).disabled = True
        bar = self.query_one("#progress", ProgressBar)
        bar.display = True
        self.query_one("#status", Static).update(f"Downloading {self.what}...")
        self._download(path, overwrite)

    @work(thread=True, exclusive=True)
    def _download(self, path: Path, overwrite: bool) -> None:
        last = [0.0]

        def progress(done: int, total: int | None) -> None:
            import time
            now = time.monotonic()
            if now - last[0] < 0.2 and (total is None or done < total):
                return
            last[0] = now
            self.app.call_from_thread(self._progress, done, total)

        try:
            result = self.run_download(path, overwrite, progress)
        except ClientError as exc:
            self.app.call_from_thread(self._failed, exc)
            return
        except OSError as exc:
            self.app.call_from_thread(self._failed, ClientError(f"Local file error: {exc}"))
            return
        self.app.call_from_thread(self._done, result)

    def _progress(self, done: int, total: int | None) -> None:
        bar = self.query_one("#progress", ProgressBar)
        if total:
            bar.update(total=total, progress=done)
        self.query_one("#status", Static).update(
            f"{format_bytes(done)}" + (f" of {format_bytes(total)}" if total else ""))

    def _failed(self, exc: ClientError) -> None:
        self.running = False
        self.query_one("#go", Button).disabled = False
        self.query_one("#cancel", Button).disabled = False
        self.query_one("#progress", ProgressBar).display = False
        message = Text("FAILED: ", style=STYLE["error"])
        message.append(clean(str(exc)))
        if exc.hint:
            message.append("\n" + clean(exc.hint), style=STYLE["muted"])
        self.query_one("#status", Static).update(message)

    def _done(self, result: dict) -> None:
        self.running = False
        verified = "verified against the server's SHA256" if result["verified"] else \
            "NOT verified: the server recorded no checksum"
        self.app.notify(f"Saved {result['path']} ({format_bytes(result['size'])}, {verified})",
                        severity="information" if result["verified"] else "warning", timeout=10)
        self.dismiss(result)
