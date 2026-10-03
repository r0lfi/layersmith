"""The interactive LayerSmith terminal interface.

Same server, same API as the web UI and the CLI: every action here goes
through ApiClient and the shared operations in ops.py. Network calls run in
worker threads, so a slow server or a large log never freezes the screen.
Quitting the interface never stops anything on the server.
"""

from __future__ import annotations

from typing import Callable

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import ContentSwitcher, Footer, OptionList, Static
from textual.widgets.option_list import Option

from layersmith_client import __version__
from layersmith_client.api import ApiClient, ClientError, Unreachable
from layersmith_client.config import ClientConfig
from layersmith_client.text import clean
from layersmith_client.tui.theme import LAYERSMITH_THEME, PALETTE, VARIABLES, badge

MIN_WIDTH, MIN_HEIGHT = 80, 24
#: Layout modes by terminal size: "wide" shows list, details and log side by
#: side; "medium" keeps the menu and puts details and log in one switchable
#: pane; "compact" does the same with tighter spacing and a short header.
WIDE = (140, 40)
COMPACT = (100, 28)

#: Sidebar entries: (view id, label). None is a separator between the work
#: pages and the settings pages.
NAVIGATION = [
    ("dashboard", "Dashboard"),
    ("projects", "Projects"),
    ("create", "Create Image"),
    ("training", "AI / LLM Training"),
    ("builds", "Builds"),
    ("scans", "Scans"),
    ("exports", "Exports"),
    None,
    ("settings", "Settings"),
    ("connection", "Connection"),
    ("help", "Help"),
]
VIEWS = [entry for entry in NAVIGATION if entry]

#: The mark: stacked layers with the front one labelled. Box-drawing
#: characters only, so any terminal font draws it; a three-layer version for
#: large terminals and a two-layer one where the header is lower.
def _mark(rows) -> Text:
    text = Text()
    for index, row in enumerate(rows):
        for segment, style in row:
            text.append(segment, style=style)
        if index < len(rows) - 1:
            text.append("\n")
    return text


def logo(large: bool) -> Text:
    back, middle, front = PALETTE["border"], PALETTE["cyan"], PALETTE["blue"]
    letter_l, letter_s = f"bold {PALETTE['text']}", f"bold {PALETTE['blue']}"
    if large:
        return _mark([
            [("    ╭───╮", back)],
            [("  ╭─", middle), ("┴", back), ("─╮ ", middle), ("│", back)],
            [("╭─", front), ("┴", middle), ("─╮ ", front), ("├", middle), ("─╯", back)],
            [("│ ", front), ("L", letter_l), ("S", letter_s), ("├", front), ("─╯", middle)],
            [("╰───╯", front)],
        ])
    return _mark([
        [("  ╭─────╮", middle)],
        [("╭─", front), ("┴", middle), ("───╮ ", front), ("│", middle)],
        [("│ ", front), ("L", letter_l), (" ", front), ("S", letter_s), (" ├", front), ("─╯", middle)],
        [("╰─────╯", front)],
    ])


def wordmark() -> Text:
    return Text.assemble(("Layer", f"bold {PALETTE['text']}"), ("Smith", f"bold {PALETTE['blue']}"))


class AppHeader(Horizontal):
    """Identity on the left; on the right, which server and whether it answers."""

    def compose(self) -> ComposeResult:
        yield Static(logo(large=True), id="logo-large", classes="logo")
        yield Static(logo(large=False), id="logo-medium", classes="logo")
        with Vertical(id="brand"):
            yield Static(wordmark(), id="brand-name")
            yield Static("Build container images with purpose", id="brand-tagline")
            yield Static("by xnett.org", id="brand-by")
        yield Static("", id="connection-state")

    def show(self, server: str, state: str, server_version: str | None = None) -> None:
        tones = {"CONNECTED": "ok", "CONNECTING": "warn", "DISCONNECTED": "bad", "ERROR": "bad"}
        words = {"CONNECTED": "Connected", "CONNECTING": "Connecting", "DISCONNECTED": "Disconnected",
                 "ERROR": "Not configured"}
        text = Text(justify="right")
        text.append("Server ", style=PALETTE["muted"])
        text.append(clean(server), style=PALETTE["text"])
        text.append("\n")
        text.append("● ", style=PALETTE[{"ok": "success", "warn": "warning", "bad": "error"}[tones.get(state, "warn")]])
        text.append_text(badge(self.app, words.get(state, state.title()), tones.get(state, "muted")))
        text.append("\n")
        text.append(f"client {__version__}", style=PALETTE["muted"])
        if server_version:
            text.append(f"  ·  server {clean(server_version)}", style=PALETTE["muted"])
        self.query_one("#connection-state", Static).update(text)


class Sidebar(Vertical):
    def compose(self) -> ComposeResult:
        options: list = []
        for entry in NAVIGATION:
            options.append(None if entry is None else Option(entry[1], id=entry[0]))
        yield OptionList(*options, id="nav")
        yield Static(Text("F2 menu  ·  ? help", style=PALETTE["muted"]), id="sidebar-foot")


class LayerSmithApp(App):
    CSS_PATH = "layersmith.tcss"
    TITLE = "LayerSmith"
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("q", "quit", "Quit", show=False),
        Binding("question_mark", "show_view('help')", "Help"),
        Binding("f1", "show_view('help')", "Help", show=False),
        Binding("ctrl+r", "refresh", "Refresh", show=False),
        Binding("f2", "focus_navigation", "Menu"),
    ]

    def get_theme_variable_defaults(self) -> dict[str, str]:
        # The ls-* colours of the stylesheet, from the one palette in theme.py.
        return {**super().get_theme_variable_defaults(), **VARIABLES}

    def __init__(self, cfg: ClientConfig, client_factory: Callable[[ClientConfig], ApiClient] | None = None):
        super().__init__()
        self.cfg = cfg
        self.client_factory = client_factory or (lambda c: ApiClient(c))
        self.client: ApiClient | None = None
        self.connection = "CONNECTING"
        self.server_info: dict = {}

    # ------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        from layersmith_client.tui import views, wizard

        yield AppHeader(id="header")
        with Horizontal(id="body"):
            yield Sidebar(id="sidebar")
            with ContentSwitcher(id="content", initial="dashboard"):
                yield views.DashboardView(id="dashboard")
                yield views.ProjectsView(id="projects")
                yield wizard.GeneralWizard(id="create")
                yield wizard.TrainingWizard(id="training")
                yield views.BuildsView(id="builds")
                yield views.ScansView(id="scans")
                yield views.ExportsView(id="exports")
                yield views.SettingsView(id="settings")
                yield views.ConnectionView(id="connection")
                yield views.HelpView(id="help")
        yield Static("", id="too-small")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(LAYERSMITH_THEME)
        self.theme = "layersmith"
        self.active_view = "dashboard"
        self.query_one("#nav", OptionList).highlighted = 0
        self._update_header()
        self._render_nav()
        self._check_size()
        self.connect()

    # ---------------------------------------------------------- connection

    def connect(self, cfg: ClientConfig | None = None) -> None:
        """(Re)create the client and check the server, without blocking."""
        if cfg is not None:
            self.cfg = cfg
        if self.client is not None:
            self.client.close()
            self.client = None
        self.connection = "CONNECTING"
        self._update_header()
        try:
            self.client = self.client_factory(self.cfg)
        except ClientError as exc:
            self.set_connection("ERROR", str(exc))
            self.show_view("connection")
            return
        self.run_api(lambda client: client.health(), self._connected, self._not_connected, group="health")

    def _connected(self, health: dict) -> None:
        self.server_info = health
        self.set_connection("CONNECTED")
        self.current_view().activate()

    def _not_connected(self, exc: ClientError) -> None:
        self.show_view("connection")

    def set_connection(self, state: str, detail: str = "") -> None:
        self.connection = state
        self._update_header(detail)
        try:
            self.query_one("#connection").show_state(state, detail)
        except Exception:  # the connection view may not be mounted yet
            pass

    def _update_header(self, detail: str = "") -> None:
        version = self.server_info.get("version") if self.connection == "CONNECTED" else None
        # Every screen has its own header (detail pages included).
        for screen in self.screen_stack:
            for header in screen.query(AppHeader).results():
                header.show(self.cfg.server, self.connection, version)

    def run_api(self, fetch: Callable, done: Callable | None = None, failed: Callable | None = None,
                group: str = "api", exclusive: bool = True) -> None:
        """Run fetch(client) in a worker thread; hand the result to done() on the UI thread.

        A lost connection is shown in the header as such, never as an empty
        result or a finished job.
        """
        client = self.client
        if client is None:
            if failed:
                failed(Unreachable("Not connected to a server"))
            return

        def job() -> None:
            try:
                result = fetch(client)
            except ClientError as exc:
                self.call_from_thread(self._api_failed, exc, failed)
                return
            except Exception as exc:  # a bug must not kill the interface
                self.call_from_thread(self._api_failed, ClientError(f"Unexpected error: {exc}"), failed)
                return
            self.call_from_thread(self._api_done, result, done)

        self.run_worker(job, thread=True, group=group, exclusive=exclusive, exit_on_error=False)

    def _api_done(self, result, done) -> None:
        if self.connection != "CONNECTED" and self.server_info:
            self.set_connection("CONNECTED")
        if done:
            done(result)

    def _api_failed(self, exc: ClientError, failed) -> None:
        if isinstance(exc, Unreachable):
            self.set_connection("DISCONNECTED", "server not reachable - jobs on the server continue")
        if failed:
            failed(exc)
        else:
            self.error(exc)

    def error(self, exc: ClientError) -> None:
        message = str(exc) + (f"\n{exc.hint}" if exc.hint else "")
        self.notify(message, title="Error", severity="error", timeout=12, markup=False)

    # ------------------------------------------------------------ navigation

    def current_view(self):
        switcher = self.query_one("#content", ContentSwitcher)
        return self.query_one(f"#{switcher.current}")

    def show_view(self, view_id: str) -> None:
        """Open a page. The menu marks it as the active page."""
        while len(self.screen_stack) > 1:
            self.pop_screen()
        switcher = self.query_one("#content", ContentSwitcher)
        nav = self.query_one("#nav", OptionList)
        index = nav.get_option_index(view_id)
        if nav.highlighted != index:
            nav.highlighted = index
        self.active_view = view_id
        self._render_nav()
        if switcher.current != view_id:
            switcher.current = view_id
            if self.connection == "CONNECTED" or view_id in ("connection", "help", "settings"):
                self.query_one(f"#{view_id}").activate()

    def _render_nav(self) -> None:
        """The active page: marker, raised background, bold - also when the
        menu does not have focus. The cursor (focus) is styled separately."""
        nav = self.query_one("#nav", OptionList)
        width = max(10, nav.content_region.width or (nav.size.width - 2) or 20)
        for key, label in VIEWS:
            if key == getattr(self, "active_view", None):
                prompt = Text("▍", style=f"bold {PALETTE['blue']} on {PALETTE['selected']}")
                prompt.append(f" {label}".ljust(width - 1), style=f"bold {PALETTE['text']} on {PALETTE['selected']}")
            else:
                prompt = Text(f"  {label}", style=PALETTE["text"])
            nav.replace_option_prompt(key, prompt)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # Enter (or a click) opens a page; moving the cursor alone does not.
        if event.option_list.id == "nav" and event.option.id:
            self.show_view(event.option.id)
            self.current_view().focus_main()

    def action_show_view(self, view_id: str) -> None:
        self.show_view(view_id)

    def action_focus_navigation(self) -> None:
        if len(self.screen_stack) > 1:
            return
        self.query_one("#nav", OptionList).focus()

    def action_refresh(self) -> None:
        if self.connection != "CONNECTED":
            self.connect()
            return
        screen = self.screen
        if hasattr(screen, "reload"):
            screen.reload()
        else:
            self.current_view().activate()

    # ------------------------------------------------------------ size

    def on_resize(self, event) -> None:
        self._check_size(event.size.width, event.size.height)

    def _check_size(self, width: int | None = None, height: int | None = None) -> None:
        width = self.size.width if width is None else width
        height = self.size.height if height is None else height
        small = width < MIN_WIDTH or height < MIN_HEIGHT
        self.set_class(small, "too-small")
        wide = width >= WIDE[0] and height >= WIDE[1]
        compact = width < COMPACT[0] or height < COMPACT[1]
        self.layout_mode = "wide" if wide else "compact" if compact else "medium"
        for mode in ("wide", "medium", "compact"):
            self.set_class(self.layout_mode == mode, mode)
        self.set_class(height >= 44, "tall")
        self.call_after_refresh(self._render_nav)
        if small:
            self.query_one("#too-small", Static).update(
                f"The terminal is {width}x{height}.\nLayerSmith needs at least {MIN_WIDTH}x{MIN_HEIGHT}.\n\n"
                "Enlarge the window, or use the command line (layersmith --help).\nCtrl+Q quits.")


def run(cfg: ClientConfig) -> int:
    LayerSmithApp(cfg).run()
    return 0
