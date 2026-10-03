"""Detail pages: one project, one build. Escape returns to the list."""

from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Select, Static, TabbedContent, TabPane, TextArea

from layersmith_client import ops
from layersmith_client.api import ClientError
from layersmith_client.text import (
    BUILD_ACTIVE, BUILD_FINISHED, CHECK_TONE, SEVERITIES, SEVERITY_TONE, check_status, clean, format_bytes,
    format_duration, format_when, security_status, severity_summary,
)
from layersmith_client.tui.app import AppHeader
from layersmith_client.tui.theme import STYLE, badge, safe, safe_block
from layersmith_client.tui.views import (
    REFRESH_SECONDS, build_facts, build_status_cell, create_bundle, download_dialog, new_table, save_spec_dialog,
    start_build_dialog,
)
from layersmith_client.tui.widgets import (
    ConfirmScreen, DownloadScreen, LogPanel, Panel, PromptScreen, TextScreen, facts,
)


class DetailScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Back")]

    def compose_header(self) -> ComposeResult:
        yield AppHeader(id="header")

    def on_mount(self) -> None:
        self.app._update_header()
        self.reload()

    def action_back(self) -> None:
        self.dismiss(None)

    def reload(self) -> None:
        pass


# ---------------------------------------------------------------- project

class ProjectScreen(DetailScreen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("b", "build", "Build"),
        Binding("c", "clone", "Clone"),
        Binding("s", "export_spec", "Save spec"),
        Binding("v", "cli", "CLI command"),
        Binding("d", "delete", "Delete"),
    ]

    def __init__(self, project_id: str):
        super().__init__()
        self.project_id = project_id
        self.project: dict | None = None

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        with TabbedContent(id="project-tabs", classes="screen-body"):
            with TabPane("Overview", id="overview"):
                yield VerticalScroll(Static("Loading...", id="project-overview"))
            with TabPane("Containerfile", id="containerfile"):
                yield Static("", id="cf-note", classes="hint")
                yield VerticalScroll(Static("", id="cf-view", classes="code"), id="cf-scroll")
                yield TextArea("", id="cf-edit", show_line_numbers=True)
                yield Horizontal(Button("Edit", id="cf-edit-button"), Button("Save", id="cf-save", variant="primary"),
                                 Button("Cancel", id="cf-cancel"), classes="buttons")
            with TabPane("Builds", id="builds"):
                yield new_table("Build", "Version", "Status", "Started", "Size", id="project-builds")
        yield Footer()

    def reload(self) -> None:
        self.app.run_api(lambda c: c.get(f"/projects/{self.project_id}"), self.show, group="project")

    def show(self, project: dict) -> None:
        self.project = project
        rows = [
            ("Name", project["name"]), ("Description", project.get("description")),
            ("Repository", project.get("repository")), ("Template", project.get("template")),
            ("Mode", "Advanced (own Containerfile)" if project.get("mode") == "advanced"
             else "Generated from the definition"),
            ("Base image", project.get("base_image")), ("Base digest", project.get("base_digest")),
            ("Next version", project.get("next_version")), ("Created", project.get("created_at")),
            ("ID", project["id"]),
        ]
        spec = project.get("spec") or {}
        if spec:
            rows += [("Architecture", spec.get("architecture")), ("Presets", ", ".join(spec.get("presets") or [])),
                     ("Capabilities", " ".join(spec.get("packages") or [])),
                     ("Extra packages", " ".join(spec.get("extra_packages") or [])),
                     ("Files", "\n".join(f"{f.get('destination')} ({f.get('mode', '0644')})"
                                         for f in spec.get("files") or [])),
                     ("Scripts", ", ".join(k for k, v in (spec.get("scripts") or {}).items() if v)),
                     ("Tests", "\n".join(spec.get("tests") or []))]
        if project.get("packages"):
            rows.append(("Resolved packages", " ".join(project["packages"])))
        recipe = project.get("training")
        if recipe:
            rows.append(("Training template", f"{recipe.get('profile_name')} {recipe.get('profile_version')} - "
                                              f"{recipe.get('stack_name')}, {recipe.get('target_name')}"))
        overview = Table.grid()
        overview.add_row(facts(rows))
        for warning in project.get("warnings") or []:
            overview.add_row(Text.assemble(("WARNING ", STYLE["warning"]), clean(warning)))
        self.query_one("#project-overview", Static).update(overview)

        advanced = project.get("mode") == "advanced"
        self.query_one("#cf-note", Static).update(
            "Advanced mode: this Containerfile is yours. Edit and save it; earlier builds keep their own copy."
            if advanced else "Generated from the definition on every build. Use Advanced mode (import) to write "
                             "your own; editing here is not offered so nothing is overwritten by a regeneration.")
        self.query_one("#cf-view", Static).update(safe_block(project.get("containerfile") or "No Containerfile."))
        self._editing(False)
        self.query_one("#cf-edit-button", Button).display = advanced

        table = self.query_one("#project-builds", DataTable)
        table.clear()
        for build in project.get("builds") or []:
            table.add_row(f"#{build['number']}", safe(build["version"]), build_status_cell(self.app, build["status"]),
                          format_when(build.get("started_at") or build.get("created_at")),
                          format_bytes(build.get("image_size")), key=build["id"])

    def _editing(self, on: bool) -> None:
        self.query_one("#cf-scroll").display = not on
        self.query_one("#cf-edit", TextArea).display = on
        self.query_one("#cf-save", Button).display = on
        self.query_one("#cf-cancel", Button).display = on
        self.query_one("#cf-edit-button", Button).display = not on and (self.project or {}).get("mode") == "advanced"

    @on(Button.Pressed, "#cf-edit-button")
    def edit(self) -> None:
        area = self.query_one("#cf-edit", TextArea)
        area.text = (self.project or {}).get("containerfile") or ""
        self._editing(True)
        area.focus()

    @on(Button.Pressed, "#cf-cancel")
    def cancel_edit(self) -> None:
        self._editing(False)

    @on(Button.Pressed, "#cf-save")
    def save(self) -> None:
        project = self.project
        if not project:
            return
        text = self.query_one("#cf-edit", TextArea).text
        payload = {"name": project["name"], "description": project.get("description") or "",
                   "repository": project.get("repository"), "template": project.get("template"),
                   "mode": "advanced", "containerfile": text}
        self.app.run_api(lambda c: c.put(f"/projects/{project['id']}", payload),
                         lambda _: (self.app.notify("Containerfile saved. Build a new version to use it.",
                                                    markup=False), self.reload()), group="project-save")

    @on(DataTable.RowSelected, "#project-builds")
    def open_build(self, event: DataTable.RowSelected) -> None:
        self.app.push_screen(BuildScreen(str(event.row_key.value)))

    def action_build(self) -> None:
        if self.project:
            start_build_dialog(self.app, self.project)

    def action_clone(self) -> None:
        project = self.project
        if not project:
            return

        def got(name):
            if name:
                self.app.run_api(lambda c: c.post(f"/projects/{project['id']}/clone", {"name": name}),
                                 lambda p: self.app.notify(f"Cloned as {clean(p['name'])}", markup=False),
                                 group="clone")
        self.app.push_screen(PromptScreen("Clone as", f"{project['name']}-copy"), got)

    def action_export_spec(self) -> None:
        save_spec_dialog(self.app, self.project_id)

    def action_cli(self) -> None:
        if self.project:
            name = self.project["name"]
            self.app.push_screen(TextScreen("Equivalent commands", (
                f"layersmith projects show {name}\n"
                f"layersmith projects export-spec {name} --output {name}.json\n"
                f"layersmith projects create --spec {name}.json --name {name}-copy\n"
                f"layersmith build {name} --version {self.project.get('next_version')} --follow\n")))

    def action_delete(self) -> None:
        project = self.project
        if not project:
            return

        def confirmed(yes):
            if yes:
                self.app.run_api(lambda c: c.delete(f"/projects/{project['id']}"),
                                 lambda _: (self.app.notify(f"Deleted {clean(project['name'])}", markup=False),
                                            self.dismiss(None)), group="delete")
        self.app.push_screen(ConfirmScreen(f"Delete project {clean(project['name'])}? Built images and archives "
                                           "are kept.", "Delete"), confirmed)


# ---------------------------------------------------------------- build

class BuildScreen(DetailScreen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("x", "download('export')", "Archive"),
        Binding("a", "bundle", "Bundle"),
        Binding("g", "download('airgap')", "Get bundle"),
        Binding("s", "scan", "Scan"),
        Binding("v", "cli", "CLI command"),
    ]

    def __init__(self, build_id: str, tab: str = "overview"):
        super().__init__()
        self.build_id = build_id
        self.initial_tab = tab
        self.build: dict | None = None
        self.timer = None
        self.scans: list[dict] | None = None
        self.scanner: dict | None = None
        self.selected_scan: str | None = None

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        with TabbedContent(id="build-tabs", initial=self.initial_tab, classes="screen-body"):
            with TabPane("Overview", id="overview"):
                yield VerticalScroll(Static("Loading...", id="build-overview"))
            with TabPane("Log", id="log"):
                yield LogPanel(id="log-view-panel", title="Log")
            with TabPane("Containerfile", id="containerfile"):
                yield VerticalScroll(Static("", id="build-cf", classes="code"))
            with TabPane("Security", id="security"):
                yield Static("", id="security-state")
                with Horizontal(classes="filters"):
                    yield Select([("All kinds", ""), ("Vulnerabilities", "vulnerability"), ("Secrets", "secret"),
                                  ("Misconfigurations", "misconfiguration")], value="", allow_blank=False,
                                 id="kind-filter", compact=True)
                    yield Select([("All severities", "")] + [(s.capitalize(), s) for s in SEVERITIES], value="",
                                 allow_blank=False, id="severity-filter", compact=True)
                    yield Button("Scan / rescan", id="scan-button")
                    yield Button("Download SBOM", id="sbom-button")
                yield new_table("Severity", "Kind", "Finding", "Package / location", "Installed", "Fixed / match",
                                "Title", id="findings")
                yield new_table("Scan", "State", "Findings", "Scanner", "When", id="scan-history")
            with TabPane("Training", id="training"):
                yield VerticalScroll(Static("", id="training-view"))
            with TabPane("Downloads", id="downloads"):
                yield VerticalScroll(
                    Static("", id="download-facts"),
                    Horizontal(Button("Download image archive", id="dl-export", variant="primary"),
                               Button("Create air-gap bundle", id="mk-bundle"),
                               Button("Download air-gap bundle", id="dl-airgap"), classes="buttons"),
                    Static("Files are saved on this machine and checked against the SHA256 the server recorded. "
                           "Archives are not unpacked or loaded into a local container runtime.", classes="hint"))
        yield Footer()

    def on_mount(self) -> None:
        super().on_mount()
        self.timer = self.set_interval(REFRESH_SECONDS, self.tick)

    def tick(self) -> None:
        if self.app.screen is not self:
            return
        if self.build and self.build.get("status") in BUILD_ACTIVE:
            self.reload(quiet=True)
        if self.scans and self.scans[0].get("state") in ("queued", "exporting", "scanning"):
            self.load_scans()

    def reload(self, quiet: bool = False) -> None:
        self.app.run_api(lambda c: c.get(f"/builds/{self.build_id}"), self.show, group=f"build-{self.build_id}")
        if not quiet:
            self.load_scans()

    def show(self, build: dict) -> None:
        self.build = build
        overview = Table.grid()
        overview.add_row(build_facts(self.app, build))
        if build.get("packages"):
            overview.add_row(Text(f"\nPackages ({len(build['packages'])})", style="bold"))
            overview.add_row(safe(" ".join(build["packages"])))
        if build.get("status") in BUILD_ACTIVE:
            overview.add_row(Text("\nThe build runs on the server. Leaving this page or quitting the client "
                                  "does not stop it.", style=STYLE["muted"]))
        self.query_one("#build-overview", Static).update(overview)
        self.query_one("#build-cf", Static).update(safe_block(build.get("containerfile") or
                                                              "The Containerfile is generated when the build starts."))
        self.show_downloads(build)
        self.show_training(build)
        self.query_one("#log-view-panel", LogPanel).show_build(
            self.app, build, f"{build.get('project_name') or ''} #{build['number']} {build['version']}".strip(),
            tail_bytes=None)

    # ---- downloads

    def show_downloads(self, build: dict) -> None:
        ready = build.get("status") == "ready"
        self.query_one("#download-facts", Static).update(facts([
            ("Image archive", f"{format_bytes(build.get('export_size'))}, sha256 {build.get('export_sha256')}"
             if build.get("has_export") else badge(self.app, "NOT AVAILABLE", "warn")),
            ("Air-gap bundle", f"{format_bytes(build.get('airgap_size'))}, sha256 {build.get('airgap_sha256')}"
             if build.get("has_airgap") else badge(self.app, "NOT CREATED", "muted")),
            ("Load command", "podman load -i <archive>  /  docker load -i <archive>"),
        ]))
        self.query_one("#dl-export", Button).disabled = not build.get("has_export")
        self.query_one("#dl-airgap", Button).disabled = not build.get("has_airgap")
        self.query_one("#mk-bundle", Button).disabled = not (ready and build.get("has_export"))
        self.query_one("#mk-bundle", Button).label = "Recreate air-gap bundle" if build.get("has_airgap") \
            else "Create air-gap bundle"

    @on(Button.Pressed, "#dl-export")
    def _dl_export(self) -> None:
        self.action_download("export")

    @on(Button.Pressed, "#dl-airgap")
    def _dl_airgap(self) -> None:
        self.action_download("airgap")

    @on(Button.Pressed, "#mk-bundle")
    def _mk_bundle(self) -> None:
        self.action_bundle()

    def action_download(self, kind: str) -> None:
        download_dialog(self.app, self.build_id, kind, on_done=lambda _: self.reload(quiet=True))

    def action_bundle(self) -> None:
        if self.build and self.build.get("status") == "ready":
            create_bundle(self.app, self.build, on_done=lambda: self.reload(quiet=True))

    def action_cli(self) -> None:
        build_id = self.build_id
        self.app.push_screen(TextScreen("Equivalent commands", (
            f"layersmith builds show {build_id}\n"
            f"layersmith builds wait {build_id} --logs     # reconnect to a running build\n"
            f"layersmith builds logs {build_id} --follow\n"
            f"layersmith export {build_id} --output ./\n"
            f"layersmith bundle export {build_id} --output ./\n"
            f"layersmith scan {build_id} --wait\n"
            f"layersmith scans list {build_id}\n"
            f"layersmith training checks {build_id}\n")))

    # ---- security

    def load_scans(self) -> None:
        def fetch(client):
            try:
                scanner = client.get("/scanner")
            except ClientError:
                scanner = None
            try:
                scans = client.get(f"/builds/{self.build_id}/scans")
            except ClientError:
                scans = None
            return scanner, scans
        self.app.run_api(fetch, self.show_scans, group=f"scans-{self.build_id}")

    def show_scans(self, data) -> None:
        self.scanner, self.scans = data
        label, tone, explanation = security_status(self.scanner, self.scans)
        text = Text("Security: ", style=STYLE["muted"])
        text.append_text(badge(self.app, label, tone))
        text.append(f"  {clean(explanation)}")
        latest = (self.scans or [None])[0]
        if latest and latest.get("state") == "completed":
            database = latest.get("database") or {}
            text.append(f"\nScanned {format_when(latest.get('finished_at'))} with {clean(latest.get('scanner'))} "
                        f"{clean(latest.get('scanner_version'))}, data from {clean(database.get('updated_at') or '?')}"
                        f"{' (offline import)' if database.get('offline') else ''}. A scan reflects what that "
                        "database knew then.", style=STYLE["muted"])
        self.query_one("#security-state", Static).update(text)
        history = self.query_one("#scan-history", DataTable)
        history.clear()
        for scan in self.scans or []:
            history.add_row(scan["id"][:8], badge(self.app, scan["state"].upper(),
                                                  {"completed": "ok", "failed": "bad"}.get(scan["state"], "active")),
                            str(scan.get("total")) if scan["state"] == "completed" else "-",
                            safe(f"{scan.get('scanner')} {scan.get('scanner_version') or ''}"),
                            format_when(scan.get("finished_at") or scan.get("created_at")), key=scan["id"])
        available = bool(self.scanner and self.scanner.get("available"))
        self.query_one("#scan-button", Button).disabled = not available
        self.query_one("#sbom-button", Button).disabled = not (latest and latest.get("sbom"))
        if latest and latest.get("state") == "completed":
            self.selected_scan = latest["id"]
            self.load_findings()
        else:
            self.query_one("#findings", DataTable).clear()

    @on(Select.Changed)
    def filters_changed(self) -> None:
        self.load_findings()

    @on(DataTable.RowSelected, "#scan-history")
    def pick_scan(self, event: DataTable.RowSelected) -> None:
        self.selected_scan = str(event.row_key.value)
        self.load_findings()

    def load_findings(self) -> None:
        if not self.selected_scan:
            return
        params = {"kind": self.query_one("#kind-filter", Select).value or None,
                  "severity": self.query_one("#severity-filter", Select).value or None}
        params = {k: v for k, v in params.items() if v}
        scan_id = self.selected_scan
        self.app.run_api(lambda c: c.get(f"/scans/{scan_id}", params=params), self.show_findings,
                         group=f"findings-{self.build_id}")

    def show_findings(self, scan: dict) -> None:
        table = self.query_one("#findings", DataTable)
        table.clear()
        if scan.get("state") != "completed":
            return
        for finding in scan.get("findings") or []:
            table.add_row(badge(self.app, finding["severity"].upper(), SEVERITY_TONE.get(finding["severity"], "muted")),
                          safe(finding["kind"]), safe(finding["identifier"]),
                          safe(finding.get("package_name") or finding.get("target")),
                          safe(finding.get("installed_version")),
                          # A secret finding carries a masked description, never the value.
                          safe(finding.get("fixed_version") or finding.get("masked_match")),
                          safe(finding.get("title")))
        shown, total = len(scan.get("findings") or []), scan.get("finding_total", 0)
        table.border_title = (f"{shown} of {total} findings (worst first)" if total > shown else
                              f"{total} findings" if total else "No findings match: not a guarantee")

    @on(Button.Pressed, "#scan-button")
    def action_scan(self) -> None:
        if not (self.scanner and self.scanner.get("available")):
            self.app.notify("No scanner is available on the server.", severity="warning", markup=False)
            return
        self.app.run_api(lambda c: ops.start_scan(c, self.build_id),
                         lambda scan: (self.app.notify(f"Scan {scan['state']}", markup=False), self.load_scans()),
                         group="scan-start", exclusive=False)

    @on(Button.Pressed, "#sbom-button")
    def sbom(self) -> None:
        latest = (self.scans or [None])[0]
        if not latest or not latest.get("sbom"):
            return
        client = self.app.client
        self.app.push_screen(DownloadScreen(
            "Download SBOM", self.app.cfg.download_dir or ".",
            lambda path, overwrite, progress: ops.download_sbom(client, latest, path, overwrite), "SBOM"))

    # ---- training

    def show_training(self, build: dict) -> None:
        pane = self.query_one("#build-tabs", TabbedContent)
        training = build.get("training")
        if not training:
            pane.hide_tab("training")
            return
        pane.show_tab("training")
        recipe = training.get("recipe") or {}
        grid = Table.grid()
        grid.add_row(Text(f"{clean(recipe.get('profile_name'))} {clean(recipe.get('profile_version'))} - "
                          f"{clean(recipe.get('stack_name'))}, {clean(recipe.get('target_name'))}", style="bold"))
        grid.add_row(Text(f"Check results apply to {clean((training.get('checks') or [{}])[0].get('applies_to'))}.",
                          style=STYLE["muted"]))
        checks = Table(box=None, show_header=True, header_style="bold", expand=True)
        checks.add_column("Result", no_wrap=True)
        checks.add_column("Check")
        checks.add_column("Summary", overflow="fold")
        for check in training.get("checks") or []:
            checks.add_row(badge(self.app, check_status(check.get("status")), CHECK_TONE.get(check.get("status"),
                                                                                              "muted")),
                           safe(check.get("label") or check.get("id")), safe(check.get("summary")))
        grid.add_row(checks)
        for check in training.get("checks") or []:
            if check.get("command"):
                grid.add_row(Text("\nGPU test - run on the machine that will train, then record the report with\n"
                                  f"  layersmith training gpu-report {build['id']} --file report.json", style="bold"))
                grid.add_row(safe_block(f"docker: {check['command'].get('docker')}\n"
                                        f"podman: {check['command'].get('podman')}"))
        grid.add_row(Text("\nGetting started", style="bold"))
        for section in (training.get("getting_started") or {}).get("sections") or []:
            grid.add_row(Text(clean(section.get("title")), style="bold"))
            for key in ("text", "code", "docker", "podman", "note"):
                if section.get(key):
                    grid.add_row(safe_block(section[key], style="" if key in ("text", "note") else STYLE["accent"]))
            for step in section.get("steps") or []:
                grid.add_row(safe(f"* {step.get('title')}"))
                for key in ("text", "code", "docker", "podman"):
                    if step.get(key):
                        grid.add_row(safe_block(f"  {step[key]}"))
            for row in section.get("table") or []:
                grid.add_row(safe(f"  {row['path']}  <- {row['host']} ({row['mode']}): {row['purpose']}"))
        contents = training.get("export_contents") or {}
        grid.add_row(Text("\nThe export contains", style="bold"))
        grid.add_row(safe_block("\n".join(f"- {x}" for x in contents.get("included") or [])))
        grid.add_row(Text("\nBring separately (not in the image)", style="bold"))
        grid.add_row(safe_block("\n".join(f"- {x}" for x in contents.get("external") or [])))
        self.query_one("#training-view", Static).update(grid)
