"""The main views of the TUI, one per sidebar entry."""

from __future__ import annotations

import json
from pathlib import Path

from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Input, Static

from layersmith_client import __version__, ops
from layersmith_client import config as client_config
from layersmith_client.api import ApiClient, ClientError
from layersmith_client.text import (
    BUILD_ACTIVE, BUILD_TONE, clean, format_bytes, format_duration, format_when, security_status,
)
from layersmith_client.tui.theme import STYLE, PALETTE, badge, muted, safe
from layersmith_client.tui.widgets import (
    ConfirmScreen, DownloadScreen, LogPanel, LsTable, Panel, PromptScreen, StatTile, facts, right, section,
)

#: How often an open view refreshes running builds, in seconds.
REFRESH_SECONDS = 3.0


class View(Vertical):
    """A page in the content area. activate() loads its data from the server."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_class("view")

    def activate(self) -> None:
        pass

    def focus_main(self) -> None:
        tables = self.query(DataTable)
        if tables:
            tables.first(DataTable).focus()


def build_status_cell(app, status: str | None) -> Text:
    return badge(app, (status or "unknown").upper(), BUILD_TONE.get(status or "", "muted"))


def new_table(*columns: str, id: str | None = None) -> LsTable:
    return LsTable(*columns, id=id)


def build_label(build: dict, project_name: str | None = None) -> str:
    name = project_name or build.get("project_name") or ""
    return f"{name} #{build['number']} {build['version']}".strip()


class WorkspaceView(View):
    """A list with the selected item's details and log: the shared page layout.

    Wide terminals show all three. Narrower ones show the list with either
    the details or the log underneath; `t` switches between them.
    """

    BINDINGS = [
        Binding("t", "toggle_pane", "Details/Log"),
        Binding("l", "focus_log", "Log"),
    ]

    def check_action(self, action: str, parameters) -> bool | None:
        if action == "toggle_pane":
            return getattr(self.app, "layout_mode", "wide") != "wide"
        return True

    def workspace(self):
        return self.query_one(".workspace")

    def action_toggle_pane(self) -> None:
        space = self.workspace()
        showing_log = space.has_class("show-log")
        space.set_class(not showing_log, "show-log")
        space.set_class(showing_log, "show-details")

    def action_focus_log(self) -> None:
        space = self.workspace()
        if getattr(self.app, "layout_mode", "wide") != "wide":
            space.set_class(True, "show-log")
            space.set_class(False, "show-details")
        self.query_one(LogPanel).view.focus()


# ---------------------------------------------------------------- dashboard

class DashboardView(View):
    """Key figures, recent builds, server health and the selected build's log."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.recent: dict[str, dict] = {}

    def compose(self) -> ComposeResult:
        with Horizontal(classes="stats"):
            yield StatTile("Projects", id="stat-projects")
            yield StatTile("Builds", id="stat-builds")
            yield StatTile("Images ready", id="stat-images")
            yield StatTile("Stored archives", id="stat-storage", classes="last")
        with Horizontal(classes="dashboard-row"):
            yield Panel(new_table("Build", "Project", "Version", "Status", "Started", "Size", id="recent"),
                        title="Recent builds", id="recent-panel")
            with Vertical(classes="side-column"):
                yield Panel(Static("", id="server-facts"), title="Server health", id="health-panel")
                yield Panel(Static("", id="activity"), title="Activity", id="activity-panel")
        yield LogPanel(id="dashboard-log", title="Log")

    def on_mount(self) -> None:
        self.set_interval(REFRESH_SECONDS * 2, self.tick)

    def tick(self) -> None:
        if self.display and self.app.screen is self.screen and \
                any(b["status"] in BUILD_ACTIVE for b in self.recent.values()):
            self.activate()

    def activate(self) -> None:
        self.app.run_api(lambda c: (c.get("/stats"), c.get("/settings")), self.show, group="dashboard")

    def show(self, data) -> None:
        stats, settings = data
        self.query_one("#stat-projects", StatTile).show(stats["projects"])
        self.query_one("#stat-builds", StatTile).show(stats["builds"])
        self.query_one("#stat-images", StatTile).show(stats["images"])
        self.query_one("#stat-storage", StatTile).show(format_bytes(stats["storage_bytes"]))
        table = self.query_one("#recent", LsTable)
        selected = table.selected_key()
        table.clear()
        recent = stats.get("recent") or []
        self.recent = {b["id"]: b for b in recent}
        for build in recent:
            table.add_row(f"#{build['number']}", safe(build.get("project_name")), safe(build["version"]),
                          build_status_cell(self.app, build["status"]),
                          format_when(build.get("started_at") or build.get("created_at")),
                          right(format_bytes(build.get("image_size"))), key=build["id"])
        table.select_key(selected)
        panel = self.query_one("#recent-panel", Panel)
        active = [b for b in recent if b["status"] in BUILD_ACTIVE]
        panel.set_title("Recent builds", f"{len(active)} active" if active else "no active builds")
        if not recent:
            self.query_one("#dashboard-log", LogPanel).show_empty("No builds yet. Create an image to start.")

        backend = settings.get("build_backend") or {}
        scanner = settings.get("scanner") or {}
        storage = settings.get("storage") or {}
        scanner_text = (badge(self.app, "Not configured", "warn") if scanner.get("name") == "none" else
                        badge(self.app, f"{clean(scanner.get('name'))} available", "ok")
                        if scanner.get("available") else badge(self.app, "Unavailable", "bad"))
        self.query_one("#server-facts", Static).update(facts([
            ("Version", settings.get("version")),
            ("Build backend", badge(self.app, f"{clean(backend.get('name'))} "
                                              f"{'available' if backend.get('available') else 'unavailable'}",
                                    "ok" if backend.get("available") else "bad")),
            ("Scanner", scanner_text),
            ("Storage free", f"{format_bytes(storage.get('free'))} of {format_bytes(storage.get('total'))}"),
            ("Namespace", settings.get("default_namespace")),
        ]))
        if active:
            activity = Table.grid(padding=(0, 1))
            for build in active:
                activity.add_row(build_status_cell(self.app, build["status"]),
                                 safe(build_label(build)))
        else:
            activity = Text("No active builds", style=PALETTE["muted"])
        self.query_one("#activity", Static).update(activity)
        self.highlighted()

    @on(DataTable.RowHighlighted, "#recent")
    def highlighted(self) -> None:
        build = self.recent.get(self.query_one("#recent", LsTable).selected_key() or "")
        if build:
            self.query_one("#dashboard-log", LogPanel).show_build(self.app, build, build_label(build))

    @on(DataTable.RowSelected, "#recent")
    def open_build(self, event: DataTable.RowSelected) -> None:
        from layersmith_client.tui.detail import BuildScreen
        self.app.push_screen(BuildScreen(str(event.row_key.value)))


# ---------------------------------------------------------------- projects

class ProjectsView(WorkspaceView):
    """The reference page: projects, the selected one's details, its latest log."""

    BINDINGS = [
        Binding("n", "new", "New"),
        Binding("b", "build", "Build"),
        Binding("slash", "search", "Search"),
        # Shown in the details panel and in Help, to keep the footer readable.
        Binding("i", "import_file", "Import", show=False),
        Binding("c", "clone", "Clone", show=False),
        Binding("s", "export_spec", "Save spec", show=False),
        Binding("d", "delete", "Delete", show=False),
        Binding("escape", "clear_search", "Clear search", show=False),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.projects: dict[str, dict] = {}
        self.latest: dict[str, dict] = {}
        self.counts: dict[str, int] = {}
        self.filter = ""

    def compose(self) -> ComposeResult:
        with Vertical(classes="workspace show-details"):
            with Panel(id="projects-panel", classes="table-panel"):
                yield Input(placeholder="Filter by name, template or description - Esc clears", id="project-filter")
                yield new_table("Name", "Template", "Latest", "Status", "Builds", "Updated", id="project-table")
            with Panel(title="Project details", id="project-detail", classes="detail-panel"):
                with VerticalScroll(classes="detail-body"):
                    yield Static("", id="project-facts")
                    with Horizontal(classes="actions"):
                        yield Button("Build", id="project-build", variant="primary")
                        yield Button("Open", id="project-open")
                        yield Button("Logs", id="project-logs")
                    yield Static(Text("More: i import  c clone  s save spec  d delete", style=STYLE["muted"]),
                                 classes="more-keys")
                yield Static("t: show the log", classes="pane-hint")
            yield LogPanel(id="project-log", title="Log")

    def on_mount(self) -> None:
        self.query_one("#project-filter", Input).display = False
        self.query_one("#projects-panel", Panel).set_title("Projects")
        self.set_interval(REFRESH_SECONDS * 2, self.tick)

    def tick(self) -> None:
        if self.display and self.app.screen is self.screen and \
                any(b["status"] in BUILD_ACTIVE for b in self.latest.values()):
            self.activate()

    def activate(self) -> None:
        def fetch(client: ApiClient):
            return client.get("/projects"), client.get("/builds", params={"limit": 200})
        self.app.run_api(fetch, self.show, group="projects")

    def show(self, data) -> None:
        projects, builds = data
        self.projects = {p["id"]: p for p in projects}
        self.latest = {}
        for build in builds:  # newest first
            self.latest.setdefault(build["project_id"], build)
        self.render_table()

    def render_table(self) -> None:
        table = self.query_one("#project-table", LsTable)
        selected = table.selected_key()
        table.clear()
        needle = self.filter.lower()
        shown = 0
        for project in self.projects.values():
            haystack = f"{project['name']} {project.get('template')} {project.get('description')}".lower()
            if needle and needle not in haystack:
                continue
            shown += 1
            latest = self.latest.get(project["id"])
            status = build_status_cell(self.app, latest["status"]) if latest else muted("no builds")
            table.add_row(Text(clean(project["name"]), style="bold"), safe(project.get("template")),
                          safe(latest["version"]) if latest else muted("-"), status,
                          right(project.get("build_count", 0)), muted(format_when(project.get("updated_at"))),
                          key=project["id"])
        total = len(self.projects)
        note = f"{shown} of {total}" if needle else f"{total} project{'s' if total != 1 else ''}"
        self.query_one("#projects-panel", Panel).set_title("Projects", note)
        table.select_key(selected)
        if not self.projects:
            self.query_one("#project-facts", Static).update(Text.assemble(
                ("No projects yet.\n\n", f"bold {PALETTE['text']}"),
                ("Press n to create an image, or i to import a Dockerfile/Containerfile.", PALETTE["muted"])))
            self.query_one("#project-log", LogPanel).show_empty("No builds yet.")
        self.highlighted()

    def selected_id(self) -> str | None:
        return self.query_one("#project-table", LsTable).selected_key()

    def selected(self) -> dict | None:
        key = self.selected_id()
        return self.projects.get(key) if key else None

    @on(DataTable.RowHighlighted, "#project-table")
    def highlighted(self) -> None:
        project = self.selected()
        for button in self.query(".actions Button"):
            button.disabled = project is None
        if not project:
            return
        latest = self.latest.get(project["id"])
        details = Table.grid()
        details.add_row(Text(clean(project["name"]), style=f"bold {PALETTE['text']}"))
        if project.get("description"):
            details.add_row(muted(project["description"]))
        details.add_row("")
        details.add_row(section("Configuration"))
        details.add_row(facts([
            ("Template", project.get("template")),
            ("Mode", "Advanced" if project.get("mode") == "advanced" else "Generated"),
            ("Base image", project.get("base_image")),
            ("Repository", project.get("repository")),
            ("Next version", project.get("next_version")),
        ]))
        details.add_row("")
        details.add_row(section("Latest build"))
        if latest:
            details.add_row(facts([
                ("Build", f"#{latest['number']}  {latest['version']}"),
                ("Status", build_status_cell(self.app, latest["status"])),
                ("Started", format_when(latest.get("started_at") or latest.get("created_at"))),
                ("Image size", format_bytes(latest.get("image_size"))),
            ]))
            if latest.get("error"):
                details.add_row(Text(clean(latest["error"])[:200], style=PALETTE["error"]))
        else:
            details.add_row(muted("No builds yet. Press b to build."))
        self.query_one("#project-facts", Static).update(details)
        log = self.query_one("#project-log", LogPanel)
        if latest:
            log.show_build(self.app, latest, build_label(latest, project["name"]))
        else:
            log.show_empty(f"No builds yet for {clean(project['name'])}.")

    @on(DataTable.RowSelected, "#project-table")
    def open_project(self, event: DataTable.RowSelected) -> None:
        self._open(str(event.row_key.value))

    def _open(self, project_id: str) -> None:
        from layersmith_client.tui.detail import ProjectScreen
        self.app.push_screen(ProjectScreen(project_id), lambda _: self.activate())

    @on(Button.Pressed, "#project-build")
    def _build_pressed(self) -> None:
        self.action_build()

    @on(Button.Pressed, "#project-open")
    def _open_pressed(self) -> None:
        if self.selected_id():
            self._open(self.selected_id())

    @on(Button.Pressed, "#project-logs")
    def _logs_pressed(self) -> None:
        self.action_focus_log()

    def action_search(self) -> None:
        field = self.query_one("#project-filter", Input)
        field.display = True
        field.focus()

    @on(Input.Changed, "#project-filter")
    def filter_changed(self, event: Input.Changed) -> None:
        self.filter = event.value.strip()
        self.render_table()

    @on(Input.Submitted, "#project-filter")
    def filter_done(self) -> None:
        self.query_one("#project-table", LsTable).focus()

    def action_clear_search(self) -> None:
        field = self.query_one("#project-filter", Input)
        if field.display:
            field.value = ""
            field.display = False
            self.filter = ""
            self.render_table()
            self.query_one("#project-table", LsTable).focus()

    def action_new(self) -> None:
        self.app.show_view("create")

    def action_import_file(self) -> None:
        def got_path(path):
            if not path:
                return
            try:
                text = ops.read_text_file(Path(path))
            except ClientError as exc:
                self.app.error(exc)
                return

            def got_name(name):
                if name:
                    self.app.run_api(lambda c: c.post("/projects/import", {"name": name, "containerfile": text}),
                                     self._imported, group="projects-import")
            self.app.push_screen(PromptScreen("Project name", Path(path).parent.name.lower()[:60] or "imported",
                                              hint="Lowercase letters, digits and dashes."), got_name)

        self.app.push_screen(PromptScreen("Local Dockerfile/Containerfile to import", "./Containerfile",
                                          hint="Read on this machine and sent to the server as text. The project "
                                               "uses Advanced mode: its Containerfile is yours to edit."), got_path)

    def _imported(self, project: dict) -> None:
        self.app.notify(f"Imported as {clean(project['name'])} (Advanced mode)", markup=False)
        self.activate()

    def action_build(self) -> None:
        project = self.selected()
        if project:
            start_build_dialog(self.app, project)

    def action_clone(self) -> None:
        project = self.selected()
        if not project:
            return

        def got(name):
            if name:
                self.app.run_api(lambda c: c.post(f"/projects/{project['id']}/clone", {"name": name}),
                                 lambda p: (self.app.notify(f"Cloned as {clean(p['name'])}", markup=False),
                                            self.activate()), group="projects-clone")
        self.app.push_screen(PromptScreen(f"Clone {clean(project['name'])} as", f"{project['name']}-copy"), got)

    def action_delete(self) -> None:
        project = self.selected()
        if not project:
            return

        def confirmed(yes):
            if yes:
                self.app.run_api(lambda c: c.delete(f"/projects/{project['id']}"),
                                 lambda _: (self.app.notify(f"Deleted {clean(project['name'])}", markup=False),
                                            self.activate()), group="projects-delete")
        self.app.push_screen(ConfirmScreen(f"Delete project {clean(project['name'])}? Built images and archives "
                                           "are kept.", "Delete"), confirmed)

    def action_export_spec(self) -> None:
        project = self.selected()
        if project:
            save_spec_dialog(self.app, project["id"])


def start_build_dialog(app, project: dict, on_started=None) -> None:
    """Ask for a version and start a build; then open it."""
    def got(version):
        if version is None:
            return

        def started(build):
            app.notify(f"Build #{build['number']} accepted ({build['status']})", markup=False)
            from layersmith_client.tui.detail import BuildScreen
            app.push_screen(BuildScreen(build["id"]))
            if on_started:
                on_started(build)
        app.run_api(lambda c: ops.start_build(c, project["id"], version.strip() or None), started,
                    group="start-build", exclusive=False)
    app.push_screen(PromptScreen(f"Build {clean(project['name'])}: version", project.get("next_version") or "",
                                 hint="Versions are immutable: a version is built once and never overwritten. "
                                      "The build runs on the server and continues if this client quits."), got)


def save_spec_dialog(app, project_id: str) -> None:
    def got(path):
        if not path:
            return

        def fetched(project):
            target = Path(path).expanduser()
            document = ops.spec_document(project)

            def write(_=True):
                if not _:
                    return
                try:
                    target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
                except OSError as exc:
                    app.error(ClientError(f"Cannot write {target}: {exc.strerror}"))
                    return
                app.notify(f"Saved {target}. Recreate with: layersmith projects create --spec {target} "
                           "--name NEW-NAME", timeout=10, markup=False)
            if target.exists():
                app.push_screen(ConfirmScreen(f"{target} exists on this machine. Replace it?", "Replace"), write)
            else:
                write()
        app.run_api(lambda c: c.get(f"/projects/{project_id}"), fetched, group="spec")
    app.push_screen(PromptScreen("Save the project definition as a spec file (on this machine)",
                                 "./layersmith-project.json"), got)


# ---------------------------------------------------------------- builds

class BuildsView(WorkspaceView):
    """Build history, the selected build's details, and its log."""

    BINDINGS = [
        Binding("x", "download('export')", "Download archive"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.builds: dict[str, dict] = {}

    def compose(self) -> ComposeResult:
        with Vertical(classes="workspace show-details"):
            yield Panel(new_table("Build", "Project", "Version", "Status", "Started", "Took", "Size",
                                  id="build-table"), title="Builds", id="builds-panel", classes="table-panel")
            with Panel(title="Build details", id="build-detail", classes="detail-panel"):
                with VerticalScroll(classes="detail-body"):
                    yield Static("Select a build.", id="build-facts")
                    with Horizontal(classes="actions"):
                        yield Button("Open", id="build-open", variant="primary")
                        yield Button("Logs", id="build-logs")
                yield Static("t: show the log", classes="pane-hint")
            yield LogPanel(id="build-log", title="Log")

    def on_mount(self) -> None:
        self.set_interval(REFRESH_SECONDS, self.tick)

    def activate(self) -> None:
        self.app.run_api(lambda c: c.get("/builds", params={"limit": 100}), self.show, group="builds")

    def tick(self) -> None:
        if not self.display or self.app.screen is not self.screen:
            return
        if any(b["status"] in BUILD_ACTIVE for b in self.builds.values()):
            self.activate()

    def show(self, builds: list[dict]) -> None:
        table = self.query_one("#build-table", LsTable)
        selected = table.selected_key()
        self.builds = {b["id"]: b for b in builds}
        table.clear()
        for build in builds:
            table.add_row(f"#{build['number']}", Text(clean(build.get("project_name")), style="bold"),
                          safe(build["version"]), build_status_cell(self.app, build["status"]),
                          muted(format_when(build.get("started_at") or build.get("created_at"))),
                          right(format_duration(build.get("duration_seconds"))),
                          right(format_bytes(build.get("image_size"))), key=build["id"])
        active = sum(1 for b in builds if b["status"] in BUILD_ACTIVE)
        self.query_one("#builds-panel", Panel).set_title(
            "Builds", f"{len(builds)} recent" + (f", {active} active" if active else ""))
        table.select_key(selected)
        if not builds:
            self.query_one("#build-facts", Static).update(muted("No builds yet."))
            self.query_one("#build-log", LogPanel).show_empty("No builds yet.")
        self.highlighted()

    def selected_id(self) -> str | None:
        return self.query_one("#build-table", LsTable).selected_key()

    @on(DataTable.RowHighlighted, "#build-table")
    def highlighted(self) -> None:
        build = self.builds.get(self.selected_id() or "")
        if not build:
            return
        self.query_one("#build-facts", Static).update(build_facts(self.app, build))
        self.query_one("#build-log", LogPanel).show_build(self.app, build, build_label(build), tail_bytes=16 * 1024)

    @on(DataTable.RowSelected, "#build-table")
    def open_build(self, event: DataTable.RowSelected) -> None:
        self._open(str(event.row_key.value))

    def _open(self, build_id: str) -> None:
        from layersmith_client.tui.detail import BuildScreen
        self.app.push_screen(BuildScreen(build_id), lambda _: self.activate())

    @on(Button.Pressed, "#build-open")
    def _open_pressed(self) -> None:
        if self.selected_id():
            self._open(self.selected_id())

    @on(Button.Pressed, "#build-logs")
    def _logs_pressed(self) -> None:
        self.action_focus_log()

    def action_download(self, kind: str) -> None:
        if self.selected_id():
            download_dialog(self.app, self.selected_id(), kind, on_done=lambda _: self.activate())


def build_facts(app, build: dict):
    details = Table.grid()
    details.add_row(Text(f"#{build['number']}  {clean(build.get('project_name'))}  {clean(build['version'])}",
                         style=f"bold {PALETTE['text']}"))
    details.add_row(build_status_cell(app, build["status"]))
    if build.get("error"):
        details.add_row(Text(clean(build["error"])[:300], style=PALETTE["error"]))
    details.add_row("")
    details.add_row(section("Image"))
    details.add_row(facts([
        ("Image", build.get("image_ref")), ("Architecture", build.get("architecture")),
        ("Base", build.get("base_image")), ("Size", format_bytes(build.get("image_size"))),
        ("Training", build.get("training_profile") or "-"),
    ]))
    details.add_row("")
    details.add_row(section("Artifacts"))
    details.add_row(facts([
        ("Image archive", format_bytes(build.get("export_size")) if build.get("has_export") else muted("not available")),
        ("Air-gap bundle", format_bytes(build.get("airgap_size")) if build.get("has_airgap") else muted("not created")),
    ]))
    details.add_row("")
    details.add_row(section("Timing"))
    details.add_row(facts([
        ("Started", format_when(build.get("started_at"))), ("Duration", format_duration(build.get("duration_seconds"))),
        ("ID", build["id"]),
    ]))
    return details


# ---------------------------------------------------------------- scans

class ScansView(View):
    """Security status per image. Not scanned is never shown as clean."""

    BINDINGS = [Binding("s", "scan", "Scan now")]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.rows: dict[str, dict] = {}
        self.scanner: dict | None = None

    def compose(self) -> ComposeResult:
        yield Panel(Static("", id="scanner-facts"), title="Scanner on the server", classes="compact-panel")
        yield Panel(new_table("Build", "Project", "Version", "Security", "Detail", id="scan-table"),
                    title="Images (20 most recent ready builds)", classes="main-panel")

    def activate(self) -> None:
        def fetch(client: ApiClient):
            try:
                scanner = client.get("/scanner")
            except ClientError:
                scanner = None
            builds = [b for b in client.get("/builds", params={"limit": 100}) if b["status"] == "ready"][:20]
            rows = []
            for build in builds:
                try:
                    scans = client.get(f"/builds/{build['id']}/scans")
                except ClientError:
                    scans = None
                rows.append((build, scans))
            return scanner, rows
        self.app.run_api(fetch, self.show, group="scans")

    def show(self, data) -> None:
        scanner, rows = data
        self.scanner = scanner
        if scanner is None:
            text = facts([("Scanner", badge(self.app, "UNKNOWN", "muted")),
                          ("Detail", "The scanner status could not be read.")])
        elif scanner.get("name") == "none":
            text = facts([("Scanner", badge(self.app, "NOT CONFIGURED", "warn")), ("Detail", scanner.get("detail"))])
        else:
            text = facts([
                ("Scanner", badge(self.app, f"{clean(scanner.get('name')).upper()} "
                                            f"{'AVAILABLE' if scanner.get('available') else 'UNAVAILABLE'}",
                                  "ok" if scanner.get("available") else "bad")),
                ("Version", scanner.get("version")), ("Kinds", ", ".join(scanner.get("scan_kinds") or [])),
                ("SBOM", "yes" if scanner.get("generate_sbom") else "no"),
                ("After each build", "yes" if scanner.get("scan_after_build") else "no"),
                ("Database", (scanner.get("database") or {}).get("detail")),
            ])
        self.query_one("#scanner-facts", Static).update(text)
        table = self.query_one("#scan-table", DataTable)
        table.clear()
        self.rows = {}
        for build, scans in rows:
            label, tone, explanation = security_status(scanner, scans)
            self.rows[build["id"]] = build
            table.add_row(f"#{build['number']}", safe(build.get("project_name")), safe(build["version"]),
                          badge(self.app, label, tone), safe(explanation), key=build["id"])

    @on(DataTable.RowSelected, "#scan-table")
    def open_build(self, event: DataTable.RowSelected) -> None:
        from layersmith_client.tui.detail import BuildScreen
        self.app.push_screen(BuildScreen(str(event.row_key.value), tab="security"), lambda _: self.activate())

    def action_scan(self) -> None:
        table = self.query_one("#scan-table", DataTable)
        if not table.row_count:
            return
        build_id = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        self.app.run_api(lambda c: ops.start_scan(c, build_id),
                         lambda scan: (self.app.notify(f"Scan {scan['state']}", markup=False), self.activate()),
                         group="scan-start", exclusive=False)


# ---------------------------------------------------------------- exports

class ExportsView(View):
    """Image archives and air-gap bundles, downloaded to this machine."""

    BINDINGS = [
        Binding("x", "download('export')", "Download archive"),
        Binding("a", "create_bundle", "Create bundle"),
        Binding("g", "download('airgap')", "Download bundle"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.builds: dict[str, dict] = {}

    def compose(self) -> ComposeResult:
        yield Panel(new_table("Build", "Project", "Version", "Image archive", "Air-gap bundle", id="export-table"),
                    title="Exports on the server", id="exports-panel", classes="table-panel")

    def activate(self) -> None:
        self.app.run_api(lambda c: [b for b in c.get("/builds", params={"limit": 100}) if b["status"] == "ready"],
                         self.show, group="exports")

    def show(self, builds: list[dict]) -> None:
        table = self.query_one("#export-table", DataTable)
        self.builds = {b["id"]: b for b in builds}
        table.clear()
        for build in builds:
            archive = badge(self.app, f"AVAILABLE {format_bytes(build.get('export_size'))}", "ok") \
                if build.get("has_export") else badge(self.app, "NOT AVAILABLE", "warn")
            bundle = badge(self.app, f"AVAILABLE {format_bytes(build.get('airgap_size'))}", "ok") \
                if build.get("has_airgap") else badge(self.app, "NOT CREATED", "muted")
            table.add_row(f"#{build['number']}", safe(build.get("project_name")), safe(build["version"]),
                          archive, bundle, key=build["id"])

    def selected(self) -> dict | None:
        table = self.query_one("#export-table", DataTable)
        if not table.row_count:
            return None
        return self.builds.get(str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value))

    def action_download(self, kind: str) -> None:
        build = self.selected()
        if build:
            download_dialog(self.app, build["id"], kind, on_done=lambda _: self.activate())

    def action_create_bundle(self) -> None:
        build = self.selected()
        if build:
            create_bundle(self.app, build, on_done=self.activate)


def download_dialog(app, build_id: str, kind: str, on_done=None) -> None:
    what = "image archive" if kind == "export" else "air-gap bundle"
    client = app.client

    def run(path: Path, overwrite: bool, progress):
        # Fresh status first: the file may have gone since the list was read.
        build = client.get(f"/builds/{build_id}")
        return ops.download_artifact(client, build, kind, path, overwrite=overwrite, progress=progress)

    default = app.cfg.download_dir or "."
    app.push_screen(DownloadScreen(f"Download {what}", default, run, what), on_done)


def create_bundle(app, build: dict, on_done=None) -> None:
    def confirmed(yes):
        if not yes:
            return
        app.notify("Creating the air-gap bundle on the server. Large images take a while; you can keep working.",
                   timeout=8)
        app.run_api(lambda c: ops.create_bundle(c, build["id"]),
                    lambda r: (app.notify(f"Bundle ready: {clean(r['filename'])} ({format_bytes(r['size'])})",
                                          markup=False), on_done and on_done()),
                    group=f"bundle-{build['id']}", exclusive=False)
    verb = "Recreate" if build.get("has_airgap") else "Create"
    app.push_screen(ConfirmScreen(f"{verb} the air-gap bundle for build #{build['number']} on the server?",
                                  verb, danger=False), confirmed)


# ---------------------------------------------------------------- settings

class SettingsView(View):
    BINDINGS = [Binding("e", "edit_path", "Edit server path")]

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Panel(Static("", id="about"), title="Server", classes="compact-panel")
            yield Panel(new_table("Field", "What", "Path on the server", "Source", "Editable", id="paths"),
                        title="Server storage (paths on the server, not on this machine)", classes="compact-panel")
            yield Panel(Static("", id="client-settings"), title="This client", classes="compact-panel")

    def activate(self) -> None:
        self.show_client()
        self.app.run_api(lambda c: c.get("/settings"), self.show, group="settings")

    def show_client(self) -> None:
        cfg = self.app.cfg
        rows = [(row["setting"], f"{row['value'] if row['value'] is not None else '-'}  ({row['source']})")
                for row in cfg.describe()]
        rows.insert(0, ("Client version", __version__))
        rows.insert(1, ("Config file", str(cfg.path)))
        self.query_one("#client-settings", Static).update(facts(rows))

    def show(self, settings: dict) -> None:
        backend = settings.get("build_backend") or {}
        scanner = settings.get("scanner") or {}
        limits = settings.get("limits") or {}
        rows = [
            ("LayerSmith", f"{clean(settings.get('version'))} (revision {clean(settings.get('revision'))[:12]})"),
            ("Source", settings.get("source_url")), ("License", settings.get("license")),
            ("Default architecture", settings.get("default_architecture")),
            ("Default namespace", settings.get("default_namespace")),
            ("Build backend", badge(self.app, f"{clean(backend.get('name')).upper()} "
                                              f"{'AVAILABLE' if backend.get('available') else 'UNAVAILABLE'}",
                                    "ok" if backend.get("available") else "bad")),
            ("Backend detail", backend.get("detail")), ("Archive format", backend.get("archive_format")),
            ("Runtimes", ", ".join(f"{r['name']}: {'available' if r['available'] else 'unavailable'}"
                                   for r in backend.get("runtimes") or [])),
            ("Scanner", "none (not configured)" if scanner.get("name") == "none" else
             f"{scanner.get('name')} {'available' if scanner.get('available') else 'unavailable'}"),
            ("Max upload", format_bytes(limits.get("max_upload_bytes")) if limits else "not reported"),
            ("Max files per image", limits.get("max_files_per_image") if limits else "not reported"),
            ("Max build context", format_bytes(limits.get("max_context_bytes")) if limits else "not reported"),
            ("Server storage", f"{format_bytes((settings.get('storage') or {}).get('free'))} free"),
        ]
        self.query_one("#about", Static).update(facts(rows))
        table = self.query_one("#paths", DataTable)
        table.clear()
        self.paths = {p["field"]: p for p in settings.get("paths") or []}
        for path in settings.get("paths") or []:
            table.add_row(safe(path["field"]), safe(path["label"]), safe(path["path"]), safe(path["source"]),
                          "yes" if path["editable"] else "no (" + clean(path.get("note") or "fixed") + ")",
                          key=path["field"])

    def action_edit_path(self) -> None:
        table = self.query_one("#paths", DataTable)
        if not table.row_count:
            return
        field = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        row = getattr(self, "paths", {}).get(field)
        if not row or not row["editable"]:
            self.app.notify("This path is fixed on the server (set by its environment or volume).", markup=False)
            return

        def got(path):
            if not path or path == row["path"]:
                return

            def confirmed(yes):
                if yes:
                    self.app.run_api(lambda c: c.put("/settings/storage", {"paths": {field: path}}),
                                     lambda _: (self.app.notify("Server path changed", markup=False),
                                                self.activate()), group="settings-save")
            self.app.push_screen(ConfirmScreen(f"Change where the SERVER writes new {row['label'].lower()} to "
                                               f"{path}? Existing files stay where they are.", "Change"), confirmed)
        self.app.push_screen(PromptScreen(f"{row['label']}: new path on the server", row["path"]), got)


# ---------------------------------------------------------------- connection

class ConnectionView(View):
    """Which server, whether it answers, and the doctor's findings."""

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Panel(
                Static("", id="conn-state"),
                Input(placeholder="https://layersmith.example.org", id="server-url"),
                Horizontal(Button("Connect", id="connect", variant="primary"),
                           Button("Save as default", id="save"), Button("Run doctor", id="doctor"),
                           classes="buttons"),
                Static("Connect uses the address for this session. Save as default stores it in the client config "
                       "file. Nothing is started on the server, and there is no fallback to another server.",
                       classes="hint"),
                title="Server connection", classes="compact-panel")
            yield Panel(new_table("Scope", "Status", "Check", "Detail", id="doctor-table"),
                        title="Doctor (read-only)", classes="compact-panel")

    def on_mount(self) -> None:
        self.query_one("#server-url", Input).value = self.app.cfg.server

    def activate(self) -> None:
        self.query_one("#server-url", Input).value = self.app.cfg.server
        self.show_state(self.app.connection)

    def focus_main(self) -> None:
        self.query_one("#server-url", Input).focus()

    def show_state(self, state: str, detail: str = "") -> None:
        tones = {"CONNECTED": "ok", "CONNECTING": "warn", "DISCONNECTED": "bad", "ERROR": "bad"}
        words = {"CONNECTED": "Connected", "CONNECTING": "Connecting", "DISCONNECTED": "Disconnected",
                 "ERROR": "Not configured"}
        text = Text("Server  ", style=STYLE["muted"])
        text.append(clean(self.app.cfg.server))
        text.append("\nStatus  ", style=STYLE["muted"])
        text.append_text(badge(self.app, words.get(state, state.title()), tones.get(state, "muted")))
        if state in ("DISCONNECTED", "ERROR"):
            text.append("\n\nThe server could not be reached. Check the address, that LayerSmith is running there, "
                        "and that this machine may connect to it (firewall, proxy, VPN). Builds already running on "
                        "the server are not affected.", style=STYLE["muted"])
        if detail:
            text.append("\n" + clean(detail), style=STYLE["muted"])
        self.query_one("#conn-state", Static).update(text)

    def _config_with(self, url: str):
        try:
            server = client_config.normalise_server(url)
        except client_config.ConfigError as exc:
            self.app.error(ClientError(str(exc)))
            return None
        cfg = self.app.cfg
        new = client_config.ClientConfig(**{**cfg.__dict__, "server": server,
                                            "sources": {**cfg.sources, "server": "this session"}})
        return new

    @on(Button.Pressed, "#connect")
    @on(Input.Submitted, "#server-url")
    def connect(self) -> None:
        cfg = self._config_with(self.query_one("#server-url", Input).value)
        if cfg:
            self.app.connect(cfg)

    @on(Button.Pressed, "#save")
    def save(self) -> None:
        cfg = self._config_with(self.query_one("#server-url", Input).value)
        if not cfg:
            return
        try:
            stored = client_config.load_file(self.app.cfg.path)
            stored["server"] = cfg.server
            path = client_config.save_file(stored, self.app.cfg.path)
        except (client_config.ConfigError, OSError) as exc:
            self.app.error(ClientError(f"Cannot save the client config: {exc}"))
            return
        self.app.notify(f"Saved server {cfg.server} to {path}", markup=False)
        self.app.connect(cfg)

    @on(Button.Pressed, "#doctor")
    def doctor(self) -> None:
        cfg = self.app.cfg
        factory = self.app.client_factory

        def job():
            rows = ops.doctor(cfg, lambda: factory(cfg))
            self.app.call_from_thread(self.show_doctor, rows)
        self.app.run_worker(job, thread=True, group="doctor", exclusive=True)

    def show_doctor(self, rows: list[dict]) -> None:
        tones = {"ok": "ok", "warn": "warn", "fail": "bad", "unknown": "muted"}
        table = self.query_one("#doctor-table", DataTable)
        table.clear()
        for row in rows:
            table.add_row(row["scope"], badge(self.app, row["status"].upper(), tones.get(row["status"], "muted")),
                          safe(row["check"]), safe(row["detail"]))


# ---------------------------------------------------------------- help

HELP = """\
NAVIGATION
  Up/Down        move in the menu and in tables
  Enter          open the highlighted menu page / the selected item
  Tab, Shift+Tab move between the menu, lists, panels, fields and buttons
  Space          toggle a checkbox or selection
  Escape         close a dialog, clear a search, or go back from a detail page
  F2             jump to the menu          ?   this help (F1 too)
  Ctrl+R         reload from the server    Ctrl+Q  quit (also q outside text fields)

LISTS (Projects, Builds, Dashboard)
  /              filter projects           l   go to the log
  t              switch details/log underneath (narrower terminals)
  f              follow the log again after scrolling back in it

WHAT HAPPENS WHERE
  Builds, scans, bundles, history and settings live on the LayerSmith server.
  Quitting this program, closing the SSH session or losing the network does not
  stop a build or a scan: open it again under Builds, or use
      layersmith builds wait <build-id> --logs
  Downloads are written to THIS machine and checked against the SHA256 the
  server recorded. Files you add to an image are uploaded from this machine.

PAGES
  Projects       n new, i import a Dockerfile/Containerfile, b build, c clone,
                 d delete, s save as spec file, / filter, Enter details
  Create Image   the general wizard: template, base, tools, packages, files,
                 scripts, image name, review with the generated Containerfile
  AI / LLM       training templates: purpose, hardware, software, add-ons,
                 review; check results and Getting started per build
  Builds         history, details and the live log of the selected build;
                 x download its image archive
  Scans          security status per image (not scanned is never "clean")
  Exports        x image archive, a create bundle, g download bundle
  Settings       server settings (e edits a server storage path) and this
                 client's configuration
  Connection     change the server address, retry, run the doctor

STATUS WORDS
  READY / PASSED / CONNECTED      success
  QUEUED, BUILDING, ...           in progress on the server
  FAILED / DISCONNECTED           failure, or no answer from the server
  NOT RUN / NOT SCANNED / UNKNOWN nothing is known - never the same as passed

The same actions exist on the command line: layersmith --help
"""


class HelpView(View):
    def compose(self) -> ComposeResult:
        yield Panel(VerticalScroll(Static(Text(HELP))), title="Help", classes="main-panel")
