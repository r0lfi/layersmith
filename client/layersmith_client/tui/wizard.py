"""The create-image wizards: general purpose, and LLM training.

The steps and choices are the web UI's, read from the server's catalogs; the
server validates and generates. The draft lives in a plain object, so going
back and forth never loses what was filled in, and the review step offers
the exact command and spec file that recreate the same definition.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button, Checkbox, ContentSwitcher, DataTable, Input, Label, OptionList, Select, SelectionList, Static, TextArea,
)
from textual.widgets.option_list import Option

from layersmith_client import ops
from layersmith_client.api import ClientError
from layersmith_client.text import clean
from layersmith_client.tui.theme import PALETTE, STYLE, badge, safe, safe_block
from layersmith_client.tui.views import View, new_table
from layersmith_client.tui.widgets import ConfirmScreen, Panel, PromptScreen, TextScreen, facts

#: Placeholder until the catalog has been read from the server.
LOADING = [("Loading from the server...", "")]
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
VERSION_RE = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,6}$")


def split_words(value: str) -> list[str]:
    return [word for word in re.split(r"[\s,]+", value or "") if word]


def image_problems(name: str, version: str) -> dict[str, str]:
    """Field -> problem, for the Image step's name and version."""
    problems = {}
    if not NAME_RE.match(name or ""):
        problems["name"] = "Lowercase letters, digits and dashes, starting with a letter or digit."
    if not VERSION_RE.match(version or ""):
        problems["version"] = "Three numbers, like 1.0.0."
    return problems


def show_field_errors(wizard, prefix: str, problems: dict[str, str]) -> str:
    """Put each problem under its field; return a one-line summary, or ""."""
    for field_name in ("name", "version"):
        message = problems.get(field_name, "")
        wizard.query_one(f"#{prefix}-{field_name}-error", Static).update(message)
        wizard.query_one(f"#{prefix}-{field_name}", Input).set_class(bool(message), "-invalid")
    return "Fix the marked fields." if problems else ""


class Wizard(View):
    """Step navigation shared by both wizards."""

    STEPS: tuple = ()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.step = 0
        self.catalog: dict | None = None

    def compose_steps_bar(self) -> ComposeResult:
        yield Static("", classes="steps", id=f"{self.id}-steps")

    def compose_nav(self) -> ComposeResult:
        with Horizontal(classes="wizard-nav"):
            yield Static("", classes="wizard-error", id=f"{self.id}-error")
            yield Button("Back", id=f"{self.id}-back")
            yield Button("Next", id=f"{self.id}-next", variant="primary")

    def render_steps(self) -> None:
        text = Text()
        if getattr(self.app, "layout_mode", "wide") == "compact":
            # Too narrow for every step: where you are, and what comes next.
            text.append(f" ▸ {self.step + 1}/{len(self.STEPS)} {self.STEPS[self.step]} ",
                        style=f"bold {PALETTE['blue']} on {PALETTE['selected']}")
            if self.step < len(self.STEPS) - 1:
                text.append(f"  next: {self.STEPS[self.step + 1]}", style=PALETTE["muted"])
            self.query_one(f"#{self.id}-steps", Static).update(text)
            self._after_steps()
            return
        for index, label in enumerate(self.STEPS):
            if index == self.step:
                style = f"bold {PALETTE['blue']} on {PALETTE['selected']}"
            elif index < self.step:
                style = PALETTE["text"]
            else:
                style = PALETTE["muted"]
            marker = "▸" if index == self.step else ("✓" if index < self.step else " ")
            text.append(f" {marker} {index + 1} {label} ", style=style)
            if index < len(self.STEPS) - 1:
                text.append(" ", style=PALETTE["muted"])
        self.query_one(f"#{self.id}-steps", Static).update(text)
        self._after_steps()

    def on_resize(self) -> None:
        if self.catalog is not None:
            self.render_steps()

    def _after_steps(self) -> None:
        self.query_one(f"#{self.id}-back", Button).disabled = self.step == 0
        self.query_one(f"#{self.id}-next", Button).display = self.step < len(self.STEPS) - 1
        self.query_one(ContentSwitcher).current = f"{self.id}-step-{self.step}"
        self.update_summary()

    def set_error(self, message: str = "") -> None:
        self.query_one(f"#{self.id}-error", Static).update(
            Text(clean(message), style=STYLE["error"]) if message else "")

    def go(self, step: int) -> None:
        self.collect()
        if step > self.step:
            problem = self.problem()
            if problem:
                self.set_error(problem)
                return
        self.set_error()
        self.step = max(0, min(step, len(self.STEPS) - 1))
        self.render_steps()
        self.entered(self.step)

    @on(Button.Pressed)
    def nav_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == f"{self.id}-back":
            self.go(self.step - 1)
        elif event.button.id == f"{self.id}-next":
            self.go(self.step + 1)

    # hooks
    def update_summary(self) -> None:
        """The side panel on wide terminals: the choices so far."""

    def collect(self) -> None:
        """Copy widget values into the draft."""

    def problem(self) -> str:
        """Why the current step cannot be left forward, or ""."""
        return ""

    def entered(self, step: int) -> None:
        """Refresh a step's widgets from the draft and the catalog."""


# ======================================================== general wizard

@dataclass
class Draft:
    template: str = "Developer"
    distribution: str = "AlmaLinux"
    version: str = ""
    custom_source: str = ""
    custom_family: str = "rpm"
    architecture: str = "amd64"
    presets: list = field(default_factory=list)
    packages: list = field(default_factory=list)
    extra_packages: str = ""
    enable_epel: bool = False
    files: list = field(default_factory=list)  # {sha256, filename, destination, local}
    scripts: dict = field(default_factory=lambda: {"pre_build": "", "post_install": "", "entrypoint": ""})
    name: str = ""
    repository: str = ""
    image_version: str = "1.0.0"
    description: str = ""
    presets_for: str = ""  # the template whose presets were last applied


class GeneralWizard(Wizard):
    STEPS = ("Purpose", "Base", "Version", "Tools", "Packages", "Files", "Scripts", "Image", "Review")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.draft = Draft()
        self.preview: dict | None = None

    def compose(self) -> ComposeResult:
        yield from self.compose_steps_bar()
        with Horizontal(classes="wizard-columns"):
            with ContentSwitcher(initial="create-step-0", classes="wizard-body panel"):
                with VerticalScroll(id="create-step-0"):
                    yield Static("A template preselects tool sets; everything can be changed in the next steps. "
                                 "Already have a Dockerfile? Import it under Projects (i).", classes="hint")
                    yield OptionList(id="templates")
                    yield Static("", id="template-detail", classes="hint")
                with VerticalScroll(id="create-step-1"):
                    yield Label("Base distribution")
                    yield OptionList(id="distributions")
                    yield Label("Custom base image (for Custom)")
                    yield Input(placeholder="registry.example.com/team/base:1.2", id="custom-source")
                    yield Label("Package manager of the custom image (LayerSmith cannot detect it)")
                    yield Select(LOADING, id="custom-family", allow_blank=False)
                with VerticalScroll(id="create-step-2"):
                    yield Label("Version", id="version-label")
                    yield Select(LOADING, id="distro-version", allow_blank=False)
                    yield Label("Architecture")
                    yield Select(LOADING, id="architecture", allow_blank=False)
                with VerticalScroll(id="create-step-3"):
                    yield Static("Tool sets (Space toggles). Package names are resolved per distribution.", classes="hint")
                    yield SelectionList(id="presets")
                    yield Static("Individual tools", classes="section-title")
                    yield SelectionList(id="capabilities")
                with VerticalScroll(id="create-step-4"):
                    yield Label("Additional packages: exact names for the chosen distribution, separated by spaces")
                    yield Input(placeholder="ncdu tcpflow", id="extra-packages")
                    yield Checkbox("Enable EPEL (Enterprise Linux: tools such as shellcheck and htop live there)",
                                   id="epel")
                with VerticalScroll(id="create-step-5"):
                    yield Static("Files are read on THIS machine, uploaded to the server now, and copied into the "
                                 "image at build time.", classes="hint")
                    yield Horizontal(Input(placeholder="./local/file", id="file-path"),
                                     Input(placeholder="/opt/file (destination in the image)", id="file-dest"),
                                     Button("Upload & add", id="file-add"), classes="file-row")
                    yield new_table("File", "Destination", "SHA256", id="file-table")
                    yield Static("Select a row and press Delete to remove it.", classes="hint")
                with VerticalScroll(id="create-step-6"):
                    yield Label("pre_build - runs before packages are installed")
                    yield TextArea("", id="script-pre_build", classes="script")
                    yield Label("post_install - runs after packages are installed")
                    yield TextArea("", id="script-post_install", classes="script")
                    yield Label("entrypoint - becomes the image entrypoint")
                    yield TextArea("", id="script-entrypoint", classes="script")
                with VerticalScroll(id="create-step-7"):
                    yield Label("Image name (lowercase letters, digits and dashes)")
                    yield Input(placeholder="network-tools", id="image-name")
                    yield Static("", id="image-name-error", classes="field-error")
                    yield Label("Repository (default: the server's namespace plus the name)")
                    yield Input(placeholder="layersmith/network-tools", id="image-repository")
                    yield Label("Version (immutable: a version is built once)")
                    yield Input(value="1.0.0", id="image-version")
                    yield Static("", id="image-version-error", classes="field-error")
                    yield Label("Description")
                    yield Input(id="image-description")
                with VerticalScroll(id="create-step-8"):
                    yield Static("", id="review-summary")
                    yield Static("", id="review-warnings")
                    yield Panel(Static("Generating...", id="review-containerfile", classes="code"),
                                title="Containerfile (generated by the server)")
                    yield Horizontal(Button("Create & build", id="create-build", variant="primary"),
                                     Button("Create only", id="create-only"),
                                     Button("CLI command", id="show-cli"), Button("Save spec file", id="save-spec"),
                                     classes="buttons")
            yield Panel(Static("", id="create-summary"), title="Summary", classes="wizard-summary")
        yield from self.compose_nav()

    def activate(self) -> None:
        if self.catalog is None:
            self.app.run_api(lambda c: c.get("/catalog"), self.loaded, group="catalog")
        else:
            self.render_steps()

    def focus_main(self) -> None:
        self.query_one("#templates", OptionList).focus()

    def loaded(self, catalog: dict) -> None:
        self.catalog = catalog
        templates = self.query_one("#templates", OptionList)
        templates.clear_options()
        for template in catalog["templates"]:
            prompt = Text(clean(template["name"]), style="bold")
            prompt.append(f" · {clean(template.get('description'))}", style=STYLE["muted"])
            templates.add_option(Option(prompt, id=template["name"]))
        names = [t["name"] for t in catalog["templates"]]
        if self.draft.template in names:
            templates.highlighted = names.index(self.draft.template)
        distributions = self.query_one("#distributions", OptionList)
        distributions.clear_options()
        for entry in catalog["distributions"] + [{"name": "Custom", "family": None}]:
            prompt = Text(clean(entry["name"]), style="bold")
            prompt.append(" · " + (clean(catalog["families"].get(entry["family"], "")) if entry["family"]
                                  else "any OCI image"), style=STYLE["muted"])
            distributions.add_option(Option(prompt, id=entry["name"]))
        self.query_one("#custom-family", Select).set_options(
            [(f"{manager} ({family})", family) for family, manager in catalog["families"].items()])
        self.query_one("#architecture", Select).set_options([(a, a) for a in catalog["architectures"]])
        self.query_one("#architecture", Select).value = self.draft.architecture
        self.query_one("#capabilities", SelectionList).clear_options()
        self.query_one("#capabilities", SelectionList).add_options(
            [(Text(f"{clean(category['name'])}: {clean(name)}"), name, name in self.draft.packages)
             for category in catalog["categories"] for name in category["packages"]])
        self.render_steps()
        self.entered(self.step)

    def update_summary(self) -> None:
        d = self.draft
        base = (f"{d.custom_source or '(not set)'} ({d.custom_family})" if d.distribution == "Custom"
                else f"{d.distribution} {d.version}".strip())
        rows = [("Template", d.template), ("Base", base), ("Architecture", d.architecture),
                ("Tool sets", ", ".join(d.presets) or "-"), ("Tools", " ".join(d.packages) or "-"),
                ("Extra packages", d.extra_packages or "-"), ("Files", str(len(d.files))),
                ("Scripts", ", ".join(k for k, v in d.scripts.items() if v.strip()) or "-"),
                ("Image", f"{d.repository or d.name}:{d.image_version}" if d.name else "(named in step 8)")]
        grid = Table.grid()
        grid.add_row(facts(rows))
        grid.add_row("")
        grid.add_row(Text(f"Step {self.step + 1} of {len(self.STEPS)}: {self.STEPS[self.step]}", style=STYLE["muted"]))
        self.query_one("#create-summary", Static).update(grid)

    # ---- per-step behaviour

    def distribution(self) -> dict | None:
        return next((d for d in (self.catalog or {}).get("distributions", []) if d["name"] == self.draft.distribution),
                    None)

    def template(self) -> dict | None:
        return next((t for t in (self.catalog or {}).get("templates", []) if t["name"] == self.draft.template), None)

    @on(OptionList.OptionHighlighted, "#templates")
    def template_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.draft.template = event.option.id or self.draft.template
        template = self.template() or {}
        presets = (template.get("spec") or {}).get("presets") or []
        self.query_one("#template-detail", Static).update(
            safe(f"{template.get('description', '')}. Preselected tool sets: {', '.join(presets) or 'none'}."))

    @on(OptionList.OptionHighlighted, "#distributions")
    def distribution_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.draft.distribution = event.option.id or self.draft.distribution

    def collect(self) -> None:
        d = self.draft
        q = self.query_one
        d.custom_source = q("#custom-source", Input).value.strip()
        for selector, attribute in (("#custom-family", "custom_family"), ("#distro-version", "version"),
                                    ("#architecture", "architecture")):
            value = q(selector, Select).value
            if value not in (Select.BLANK, None, ""):
                setattr(d, attribute, value)
        if self.step == 3:
            d.presets = list(q("#presets", SelectionList).selected)
            d.packages = list(q("#capabilities", SelectionList).selected)
        d.extra_packages = q("#extra-packages", Input).value
        d.enable_epel = q("#epel", Checkbox).value
        for key in d.scripts:
            d.scripts[key] = q(f"#script-{key}", TextArea).text
        d.name = q("#image-name", Input).value.strip()
        d.repository = q("#image-repository", Input).value.strip()
        d.image_version = q("#image-version", Input).value.strip()
        d.description = q("#image-description", Input).value

    def problem(self) -> str:
        if self.catalog is None:
            return "The catalog has not been loaded from the server yet."
        if self.step == 1 and self.draft.distribution == "Custom" and not self.draft.custom_source:
            return "Enter the custom base image."
        if self.step == 7:
            return show_field_errors(self, "image", image_problems(self.draft.name, self.draft.image_version))
        return ""

    def entered(self, step: int) -> None:
        if self.catalog is None:
            return
        d = self.draft
        if step == 2:
            distribution = self.distribution()
            select = self.query_one("#distro-version", Select)
            label = self.query_one("#version-label", Label)
            if distribution:
                label.update(f"{d.distribution} version")
                select.set_options([(f"{d.distribution} {v} - {distribution['sources'][v]}", v)
                                    for v in distribution["versions"]])
                if d.version not in distribution["versions"]:
                    d.version = distribution["versions"][0]
                select.value = d.version
                select.display = True
            else:
                label.update(f"Using {d.custom_source or 'a custom image'}.")
                select.display = False
        if step == 3:
            if d.presets_for != d.template:
                # Choosing a template seeds its presets; the user may change them.
                d.presets = list(((self.template() or {}).get("spec") or {}).get("presets") or [])
                d.presets_for = d.template
            presets = self.query_one("#presets", SelectionList)
            presets.clear_options()
            for preset in self.catalog["presets"]:
                prompt = Text(clean(preset["name"]), style="bold")
                prompt.append(f" - {clean(preset.get('description'))}", style=STYLE["muted"])
                presets.add_option((prompt, preset["name"], preset["name"] in d.presets))
        if step == 4:
            self.query_one("#epel", Checkbox).display = bool((self.distribution() or {}).get("el"))
        if step == 5:
            self.show_files()
        if step == 8:
            self.load_preview()

    # ---- files

    def show_files(self) -> None:
        table = self.query_one("#file-table", DataTable)
        table.clear()
        for item in self.draft.files:
            table.add_row(safe(item["filename"]), safe(item["destination"]), item["sha256"][:16] + "...",
                          key=item["sha256"] + item["destination"])

    @on(Button.Pressed, "#file-add")
    @on(Input.Submitted, "#file-dest")
    def add_file(self) -> None:
        local = Path(self.query_one("#file-path", Input).value.strip()).expanduser()
        destination = self.query_one("#file-dest", Input).value.strip() or f"/opt/{local.name}"
        if not local.is_file():
            self.set_error(f"Not a file on this machine: {local}")
            return
        self.set_error()

        def done(result):
            self.draft.files.append({"sha256": result["sha256"], "filename": result["filename"],
                                     "destination": destination, "local": str(local)})
            self.query_one("#file-path", Input).value = ""
            self.query_one("#file-dest", Input).value = ""
            self.show_files()
            self.app.notify(f"Uploaded {local.name}", markup=False)
        self.app.run_api(lambda c: c.upload(local), done, failed=lambda exc: self.set_error(str(exc)),
                         group="wizard-upload")

    def on_key(self, event) -> None:
        table = self.query_one("#file-table", DataTable)
        if event.key == "delete" and table.has_focus and table.row_count:
            key = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
            self.draft.files = [f for f in self.draft.files if f["sha256"] + f["destination"] != key]
            self.show_files()

    # ---- review

    def spec(self) -> dict:
        d = self.draft
        custom = d.distribution == "Custom"
        template = self.template() or {}
        return {
            "base": {"source": d.custom_source, "family": d.custom_family} if custom
            else {"distribution": d.distribution, "version": d.version},
            "architecture": d.architecture,
            "presets": d.presets,
            "packages": d.packages,
            "extra_packages": split_words(d.extra_packages),
            "enable_epel": d.enable_epel,
            "files": [{"sha256": f["sha256"], "destination": f["destination"]} for f in d.files],
            "scripts": {k: v for k, v in d.scripts.items() if v.strip()},
            "tests": (template.get("spec") or {}).get("tests") or [],
        }

    def document(self) -> dict:
        d = self.draft
        document = {"format": ops.SPEC_FORMAT, "name": d.name, "description": d.description,
                    "template": d.template, "mode": "gui", "spec": self.spec()}
        if d.repository:
            document["repository"] = d.repository
        return document

    def load_preview(self) -> None:
        d = self.draft
        payload = {"name": d.name or "image", "description": d.description, "template": d.template,
                   "spec": self.spec()}
        self.query_one("#review-containerfile", Static).update("Generating...")
        self.app.run_api(lambda c: c.post("/preview", payload), self.show_preview,
                         failed=self.preview_failed, group="preview")
        base = (f"{d.custom_source} ({d.custom_family})" if d.distribution == "Custom"
                else f"{d.distribution} {d.version}")
        self.query_one("#review-summary", Static).update(facts([
            ("Template", d.template), ("Base", base), ("Architecture", d.architecture),
            ("Image", f"{d.repository or '<namespace>/' + d.name}:{d.image_version}"),
            ("Tool sets", ", ".join(d.presets) or "none"), ("Tools", " ".join(d.packages) or "none"),
            ("Extra packages", d.extra_packages or "none"),
            ("Files", ", ".join(f"{f['filename']} -> {f['destination']}" for f in d.files) or "none"),
            ("Scripts", ", ".join(k for k, v in d.scripts.items() if v.strip()) or "none"),
        ]))

    def show_preview(self, preview: dict) -> None:
        self.preview = preview
        self.query_one("#review-containerfile", Static).update(safe_block(preview["containerfile"]))
        warnings = Text()
        for warning in preview.get("warnings") or []:
            warnings.append("WARNING ", style=STYLE["warning"])
            warnings.append(clean(warning) + "\n")
        warnings.append(f"{len(preview.get('packages') or [])} packages: ", style=STYLE["muted"])
        warnings.append(clean(" ".join(preview.get("packages") or [])))
        self.query_one("#review-warnings", Static).update(warnings)

    def preview_failed(self, exc: ClientError) -> None:
        self.preview = None
        self.query_one("#review-containerfile", Static).update(
            Text(f"The server rejected this definition: {clean(str(exc))}\nGo back and change it.", style="bold"))
        self.set_error(str(exc))

    @on(Button.Pressed, "#create-build")
    def create_and_build(self) -> None:
        self.create(build=True)

    @on(Button.Pressed, "#create-only")
    def create_only(self) -> None:
        self.create(build=False)

    def create(self, build: bool) -> None:
        self.collect()
        problems = image_problems(self.draft.name, self.draft.image_version)
        if problems:
            self.set_error("Fix the image name and version on the Image step.")
            return
        document = self.document()
        version = self.draft.image_version

        def job(client):
            project = client.post("/projects", {k: v for k, v in document.items() if k in ops.PROJECT_FIELDS})
            started = ops.start_build(client, project["id"], version) if build else None
            return project, started

        def done(result):
            project, started = result
            self.app.notify(f"Created {clean(project['name'])}" + (f", build #{started['number']} accepted"
                                                                   if started else ""), markup=False)
            self.draft = Draft()
            self.step = 0
            self.reset_widgets()
            from layersmith_client.tui.detail import BuildScreen, ProjectScreen
            self.app.push_screen(BuildScreen(started["id"]) if started else ProjectScreen(project["id"]))
        self.app.run_api(job, done, failed=lambda exc: self.set_error(str(exc)), group="wizard-create")

    def reset_widgets(self) -> None:
        for selector in ("#custom-source", "#extra-packages", "#image-name", "#image-repository",
                         "#image-description", "#file-path", "#file-dest"):
            self.query_one(selector, Input).value = ""
        self.query_one("#image-version", Input).value = "1.0.0"
        for key in self.draft.scripts:
            self.query_one(f"#script-{key}", TextArea).text = ""
        self.query_one("#epel", Checkbox).value = False
        if self.catalog:
            self.loaded(self.catalog)

    @on(Button.Pressed, "#show-cli")
    def show_cli(self) -> None:
        self.collect()
        name = self.draft.name or "my-image"
        self.app.push_screen(TextScreen("Equivalent command", (
            "Save the spec file first (Save spec file), then on any machine with the client:\n\n"
            f"  layersmith projects create --spec {name}.json --build --version "
            f"{shlex.quote(self.draft.image_version)} --follow\n\n"
            "The spec file holds exactly this definition. Files already uploaded are referenced by their\n"
            "SHA256, which the server keeps, so the command recreates the same image definition.\n\n"
            "Spec file content:\n\n" + json.dumps(self.document(), indent=2))))

    @on(Button.Pressed, "#save-spec")
    def save_spec(self) -> None:
        self.collect()
        document = self.document()

        def got(path):
            if not path:
                return
            target = Path(path).expanduser()

            def write(yes=True):
                if not yes:
                    return
                try:
                    target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
                except OSError as exc:
                    self.app.error(ClientError(f"Cannot write {target}: {exc.strerror}"))
                    return
                self.app.notify(f"Saved {target}", markup=False)
            if target.exists():
                self.app.push_screen(ConfirmScreen(f"{target} exists. Replace it?", "Replace"), write)
            else:
                write()
        self.app.push_screen(PromptScreen("Save spec file on this machine",
                                          f"./{self.draft.name or 'my-image'}.json"), got)


# ======================================================== training wizard

@dataclass
class TrainingDraft:
    intent: dict = field(default_factory=lambda: {"task": "sft", "method": "lora", "execution": "single-gpu"})
    profile: str = "hf-finetune"
    target: str = ""
    stack: str = ""
    addons: list = field(default_factory=lambda: ["tensorboard"])
    extra_python: str = ""
    extra_packages: str = ""
    name: str = ""
    repository: str = ""
    image_version: str = "1.0.0"
    description: str = ""

    def request(self) -> dict:
        request = {"profile": self.profile, "addons": list(self.addons), "intent": dict(self.intent),
                   "extra_python": split_words(self.extra_python)}
        if self.target:
            request["target"] = self.target
        if self.stack:
            request["stack"] = self.stack
        return request


class TrainingWizard(Wizard):
    STEPS = ("Purpose", "Hardware", "Software", "Add-ons", "Image", "Review")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.draft = TrainingDraft()
        self.resolved: dict | None = None
        self.last_recipe: dict | None = None

    def compose(self) -> ComposeResult:
        yield from self.compose_steps_bar()
        yield Static("", id="conflict")
        with Horizontal(id="conflict-actions"):
            yield Button("Use the working alternative", id="use-alternative")
        with Horizontal(classes="wizard-columns"):
            with ContentSwitcher(initial="training-step-0", classes="wizard-body panel"):
                with VerticalScroll(id="training-step-0"):
                    yield Static("", id="training-intro", classes="hint")
                    yield Vertical(id="intents")
                    yield Static("", id="recommendation")
                    yield Button("Choose the suggested profile", id="use-recommended")
                    yield Static("Profile", classes="section-title")
                    yield OptionList(id="profiles")
                    yield Static("", id="profile-description")
                    yield Static("", id="concepts", classes="hint")
                with VerticalScroll(id="training-step-1"):
                    yield Static("Hardware target", classes="section-title")
                    yield OptionList(id="targets")
                    yield Static("Software stack", classes="section-title", id="stack-title")
                    yield OptionList(id="stacks")
                    yield Static("", id="host-requirements")
                with VerticalScroll(id="training-step-2"):
                    yield Static("", id="recipe-facts")
                    yield new_table("Tool", "Version", "", "What it is", "Used for", id="recipe-tools")
                    yield Static("", id="recipe-versions", classes="hint")
                with VerticalScroll(id="training-step-3"):
                    yield Static("Optional add-ons for this profile (Space toggles)", classes="section-title")
                    yield SelectionList(id="addons")
                    yield Label("Extra Python packages (name or name==version; resolved against the lock at build time)")
                    yield Input(id="extra-python")
                    yield Label("Extra system packages (Debian package names)")
                    yield Input(id="training-extra-packages")
                with VerticalScroll(id="training-step-4"):
                    yield Label("Image name (lowercase letters, digits and dashes)")
                    yield Input(placeholder="llm-finetune", id="training-name")
                    yield Static("", id="training-name-error", classes="field-error")
                    yield Label("Repository (default: the server's namespace plus the name)")
                    yield Input(id="training-repository")
                    yield Label("Version")
                    yield Input(value="1.0.0", id="training-version")
                    yield Static("", id="training-version-error", classes="field-error")
                    yield Label("Description")
                    yield Input(id="training-description")
                with VerticalScroll(id="training-step-5"):
                    yield Static("", id="training-review")
                    yield Panel(Static("Generating...", id="training-containerfile", classes="code"),
                                title="Containerfile (generated by the server)")
                    yield Horizontal(Button("Create & build", id="training-create-build", variant="primary"),
                                     Button("Create only", id="training-create-only"),
                                     Button("CLI command", id="training-cli"), classes="buttons")
            yield Panel(Static("", id="training-summary"), title="Summary", classes="wizard-summary")
        yield from self.compose_nav()

    def activate(self) -> None:
        if self.catalog is None:
            self.app.run_api(lambda c: c.get("/training/catalog"), self.loaded, group="training-catalog")
        else:
            self.render_steps()

    def focus_main(self) -> None:
        self.query_one("#profiles", OptionList).focus()

    def loaded(self, catalog: dict) -> None:
        self.catalog = catalog
        self.query_one("#training-intro", Static).update(safe(catalog.get("category", {}).get("intro")))
        intents = self.query_one("#intents", Vertical)
        intents.remove_children()
        for key, group in catalog.get("intents", {}).items():
            options = [(Text(clean(o["label"])), o["id"]) for o in group["options"]]
            value = self.draft.intent.get(key, group["options"][0]["id"])
            select = Select(options, value=value, allow_blank=False, id=f"intent-{key}", compact=True)
            intents.mount(Label(Text(f"{clean(group['label'])} - {clean(group['hint'])}")))
            intents.mount(select)
        profiles = self.query_one("#profiles", OptionList)
        profiles.clear_options()
        for profile in catalog.get("profiles") or []:
            prompt = Text(clean(profile["name"]), style="bold")
            if profile.get("recommended"):
                prompt.append(" (recommended)", style=STYLE["success"])
            prompt.append(f"\n  {clean(profile.get('summary'))}", style=STYLE["muted"])
            profiles.add_option(Option(prompt, id=profile["id"]))
        ids = [p["id"] for p in catalog.get("profiles") or []]
        if self.draft.profile in ids:
            profiles.highlighted = ids.index(self.draft.profile)
        self.query_one("#concepts", Static).update(Text("\n".join(
            f"{clean(c['term'])}: {clean(c['text'])}" for c in catalog.get("concepts") or [])))
        self.render_steps()
        self.resolve()

    def profile(self) -> dict | None:
        return next((p for p in (self.catalog or {}).get("profiles") or [] if p["id"] == self.draft.profile), None)

    def update_summary(self) -> None:
        d = self.draft
        recipe = (self.resolved or {}).get("recipe") if (self.resolved or {}).get("ok") else self.last_recipe
        intents = ", ".join(f"{k}={v}" for k, v in d.intent.items())
        rows = [("Profile", (self.profile() or {}).get("name", d.profile)), ("Purpose", intents),
                ("Target", (recipe or {}).get("target_name") or d.target or "-"),
                ("Stack", (recipe or {}).get("stack_name") or "-"), ("Add-ons", ", ".join(d.addons) or "none"),
                ("Image", f"{d.repository or d.name}:{d.image_version}" if d.name else "(named in step 5)")]
        grid = Table.grid()
        grid.add_row(facts(rows))
        grid.add_row("")
        if self.resolved and not self.resolved.get("ok"):
            grid.add_row(Text("CONFLICT - see above", style=STYLE["warning"]))
        elif self.resolved:
            grid.add_row(Text("Compatible", style=STYLE["success"]))
        self.query_one("#training-summary", Static).update(grid)

    def choose_profile(self, profile_id: str) -> None:
        if profile_id == self.draft.profile:
            return
        profile = next((p for p in (self.catalog or {}).get("profiles") or [] if p["id"] == profile_id), None)
        if profile:
            self.draft.profile, self.draft.target, self.draft.stack = profile_id, "", ""
            self.draft.addons = list(profile.get("default_addons") or [])
            self.resolve()

    @on(OptionList.OptionHighlighted, "#profiles")
    def profile_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id:
            self.choose_profile(event.option.id)
        profile = self.profile() or {}
        text = Text(clean(profile.get("description")))
        if profile.get("best_for"):
            text.append("\nBest for: " + "; ".join(clean(x) for x in profile["best_for"]), style=STYLE["muted"])
        self.query_one("#profile-description", Static).update(text)

    @on(Select.Changed)
    def intent_changed(self, event: Select.Changed) -> None:
        if event.select.id and event.select.id.startswith("intent-") and event.value is not Select.BLANK:
            self.draft.intent[event.select.id[len("intent-"):]] = event.value
            self.resolve()

    @on(OptionList.OptionSelected, "#targets")
    @on(OptionList.OptionHighlighted, "#targets")
    def target_chosen(self, event) -> None:
        if event.option.id and event.option.id != (self.draft.target or None):
            self.draft.target = event.option.id
            self.resolve()

    @on(OptionList.OptionHighlighted, "#stacks")
    def stack_chosen(self, event) -> None:
        if event.option.id and event.option.id != (self.draft.stack or None):
            self.draft.stack = event.option.id
            self.resolve()

    @on(Button.Pressed, "#use-recommended")
    def use_recommended(self) -> None:
        recommendation = (self.resolved or {}).get("recommendation") or {}
        if recommendation.get("profile"):
            ids = [p["id"] for p in (self.catalog or {}).get("profiles") or []]
            if recommendation["profile"] in ids:
                self.query_one("#profiles", OptionList).highlighted = ids.index(recommendation["profile"])

    @on(Button.Pressed, "#use-alternative")
    def use_alternative(self) -> None:
        alternative = (self.resolved or {}).get("alternative")
        if not alternative:
            return
        d = self.draft
        if alternative.get("profile") and alternative["profile"] != d.profile:
            self.choose_profile(alternative["profile"])
            d.target = alternative.get("target") or ""
        else:
            for key, attribute in (("target", "target"), ("stack", "stack")):
                if key in alternative:
                    setattr(d, attribute, alternative[key] or "")
            if "addons" in alternative:
                d.addons = list(alternative["addons"])
            if "extra_python" in alternative:
                d.extra_python = " ".join(alternative["extra_python"])
                self.query_one("#extra-python", Input).value = d.extra_python
        self.resolve()
        self.entered(self.step)

    def resolve(self) -> None:
        """The server decides compatibility; the wizard shows what it says."""
        request = self.draft.request()
        self.app.run_api(lambda c: c.post("/training/resolve", {"training": request}), self.show_resolved,
                         group="training-resolve")

    def show_resolved(self, resolved: dict) -> None:
        self.resolved = resolved
        conflict = self.query_one("#conflict", Static)
        if resolved.get("ok"):
            self.last_recipe = resolved.get("recipe")
            conflict.update("")
            conflict.display = False
            self.query_one("#conflict-actions").display = False
        else:
            conflict.display = True
            conflict.update(Text.assemble(("CONFLICT ", STYLE["warning"]), clean(resolved.get("error"))))
            self.query_one("#conflict-actions").display = bool(resolved.get("alternative"))
            if resolved.get("label"):
                self.query_one("#use-alternative", Button).label = clean(resolved["label"])
        recommendation = resolved.get("recommendation") or {}
        names = {p["id"]: p["name"] for p in (self.catalog or {}).get("profiles") or []}
        self.query_one("#recommendation", Static).update(
            Text.assemble(("Suggested for this purpose: ", "dim"), (clean(names.get(recommendation.get("profile"),
                                                                                 recommendation.get("profile"))),
                                                                    "bold"), f" - {clean(recommendation.get('reason'))}")
            if recommendation else "")
        self.query_one("#use-recommended", Button).display = bool(
            recommendation and recommendation.get("profile") != self.draft.profile)
        self.entered(self.step)
        self.update_summary()

    def collect(self) -> None:
        d, q = self.draft, self.query_one
        if self.step == 3:
            d.addons = list(q("#addons", SelectionList).selected)
        d.extra_python = q("#extra-python", Input).value
        d.extra_packages = q("#training-extra-packages", Input).value
        d.name = q("#training-name", Input).value.strip()
        d.repository = q("#training-repository", Input).value.strip()
        d.image_version = q("#training-version", Input).value.strip()
        d.description = q("#training-description", Input).value

    def go(self, step: int) -> None:
        previous = (self.draft.addons, self.draft.extra_python)
        super().go(step)
        if (self.draft.addons, self.draft.extra_python) != previous:
            self.resolve()

    def problem(self) -> str:
        if self.catalog is None:
            return "The training catalog has not been loaded from the server yet."
        if self.step >= 1 and not (self.resolved or {}).get("ok"):
            return "Resolve the conflict first: " + clean((self.resolved or {}).get("error") or "checking...")
        if self.step == 4:
            return show_field_errors(self, "training", image_problems(self.draft.name, self.draft.image_version))
        return ""

    def entered(self, step: int) -> None:
        if self.catalog is None:
            return
        catalog, d = self.catalog, self.draft
        profile = self.profile() or {}
        recipe = (self.resolved or {}).get("recipe") if (self.resolved or {}).get("ok") else self.last_recipe
        if step == 1:
            targets = self.query_one("#targets", OptionList)
            targets.clear_options()
            ids = list(catalog.get("targets", {}))
            for target_id, target in catalog.get("targets", {}).items():
                prompt = Text(clean(target["name"]), style="bold")
                if target_id not in profile.get("targets", []):
                    prompt.append(f"  (not a target of {clean(profile.get('name'))})", style=STYLE["warning"])
                prompt.append(f"\n  {clean(target['summary'])}", style=STYLE["muted"])
                targets.add_option(Option(prompt, id=target_id))
            chosen = d.target or (profile.get("targets") or [None])[0]
            if chosen in ids:
                targets.highlighted = ids.index(chosen)
            stacks = self.query_one("#stacks", OptionList)
            stacks.clear_options()
            for stack_id in profile.get("stacks") or []:
                stack = catalog["stacks"][stack_id]
                prompt = Text(clean(stack["name"]), style="bold")
                prompt.append(f"\n  {clean(stack.get('summary'))}  {clean(stack.get('driver') or '')}", style=STYLE["muted"])
                stacks.add_option(Option(prompt, id=stack_id))
            show_stacks = len(profile.get("stacks") or []) > 1
            stacks.display = show_stacks
            self.query_one("#stack-title").display = show_stacks
            if show_stacks:
                chosen_stack = d.stack or profile["stacks"][0]
                stacks.highlighted = profile["stacks"].index(chosen_stack) if chosen_stack in profile["stacks"] else 0
            if recipe:
                text = Text("The machine that runs the image needs:\n", style="bold")
                for item in recipe.get("host_requirements") or []:
                    text.append(f"- {clean(item)}\n")
                if recipe.get("driver"):
                    text.append(f"{clean(recipe.get('driver'))} {clean(recipe.get('gpu_support'))}\n", style=STYLE["muted"])
                text.append("Building needs no GPU: the server builds and checks the image on CPU.", style=STYLE["muted"])
                self.query_one("#host-requirements", Static).update(text)
        if step == 2 and recipe:
            base = recipe.get("base") or {}
            self.query_one("#recipe-facts", Static).update(facts([
                ("Base image", base.get("display")), ("Digest", base.get("digest")), ("Why", base.get("reason")),
                ("Architecture", f"linux/{recipe.get('architecture')}"), ("Python", recipe.get("python")),
                ("PyTorch", recipe.get("torch")), ("CUDA", recipe.get("cuda")),
                ("Driver", recipe.get("driver")), ("Stack", recipe.get("stack_name")),
                ("Notes", "\n".join(recipe.get("notes") or [])),
            ]))
            tools = self.query_one("#recipe-tools", DataTable)
            tools.clear()
            for tool in recipe.get("tools") or []:
                tools.add_row(safe(tool["name"]), safe(tool.get("version")),
                              badge(self.app, "REQUIRED" if tool.get("required") else "OPTIONAL",
                                    "active" if tool.get("required") else "muted"),
                              safe(tool.get("what")), safe(tool.get("used_for")))
            self.query_one("#recipe-versions", Static).update(Text(
                "Locked versions: " + ", ".join(f"{clean(k)}=={clean(v)}"
                                                for k, v in sorted((recipe.get("versions") or {}).items()))))
        if step == 3:
            addons = self.query_one("#addons", SelectionList)
            addons.clear_options()
            for addon_id in profile.get("addons") or []:
                addon = catalog.get("addons", {}).get(addon_id, {})
                prompt = Text(clean(addon.get("name", addon_id)), style="bold")
                prompt.append(f" - {clean(addon.get('summary'))}", style=STYLE["muted"])
                addons.add_option((prompt, addon_id, addon_id in d.addons))
        if step == 5:
            self.review(recipe)

    def review(self, recipe: dict | None) -> None:
        resolved = self.resolved or {}
        d = self.draft
        grid = Table.grid()
        grid.add_row(facts([("Profile", (self.profile() or {}).get("name")), ("Image", f"{d.repository or '<namespace>/' + d.name}:{d.image_version}"),
                            ("Stack", (recipe or {}).get("stack_name")), ("Add-ons", ", ".join(d.addons) or "none")]))
        if resolved.get("before_build"):
            grid.add_row(Text("\nBefore you build", style="bold"))
            grid.add_row(safe_block(resolved["before_build"]))
        contents = resolved.get("export_contents") or {}
        if contents:
            grid.add_row(Text("\nThe export will contain", style="bold"))
            grid.add_row(safe_block("\n".join(f"- {x}" for x in contents.get("included") or [])))
            grid.add_row(Text("\nBring separately (models, datasets are never downloaded or bundled)", style="bold"))
            grid.add_row(safe_block("\n".join(f"- {x}" for x in contents.get("external") or [])))
        grid.add_row(Text("\nChecks after the build: dependencies, CPU smoke test and offline example run on the "
                          "server; a GPU test is NOT run there - it stays NOT RUN until you record one.", style=STYLE["muted"]))
        self.query_one("#training-review", Static).update(grid)
        payload = {"name": d.name or "training-image", "description": d.description,
                   "template": f"LLM: {(self.profile() or {}).get('name', d.profile)}", "spec": self.spec()}
        self.app.run_api(lambda c: c.post("/preview", payload),
                         lambda p: self.query_one("#training-containerfile", Static).update(
                             safe_block(p["containerfile"])),
                         failed=lambda exc: self.query_one("#training-containerfile", Static).update(
                             Text(f"The server rejected this definition: {clean(str(exc))}", style="bold")),
                         group="training-preview")

    def spec(self) -> dict:
        return {"training": self.draft.request(), "extra_packages": split_words(self.draft.extra_packages)}

    @on(Button.Pressed, "#training-create-build")
    def create_build(self) -> None:
        self.create(True)

    @on(Button.Pressed, "#training-create-only")
    def create_only(self) -> None:
        self.create(False)

    def create(self, build: bool) -> None:
        self.collect()
        problems = image_problems(self.draft.name, self.draft.image_version)
        if problems:
            self.set_error("Fix the image name and version on the Image step.")
            return
        d = self.draft
        payload = {"name": d.name, "description": d.description, "repository": d.repository or None,
                   "template": f"LLM: {(self.profile() or {}).get('name', d.profile)}", "mode": "gui",
                   "spec": self.spec()}
        version = d.image_version

        def job(client):
            project = client.post("/projects", payload)
            return project, (ops.start_build(client, project["id"], version) if build else None)

        def done(result):
            project, started = result
            self.app.notify(f"Created {clean(project['name'])}" + (f", build #{started['number']} accepted"
                                                                   if started else ""), markup=False)
            from layersmith_client.tui.detail import BuildScreen, ProjectScreen
            self.app.push_screen(BuildScreen(started["id"]) if started else ProjectScreen(project["id"]))
        self.app.run_api(job, done, failed=lambda exc: self.set_error(str(exc)), group="training-create")

    @on(Button.Pressed, "#training-cli")
    def cli(self) -> None:
        self.collect()
        d = self.draft
        parts = ["layersmith", "training", "create", "--name", d.name or "my-training-image", "--profile", d.profile]
        for flag, value in (("--target", d.target), ("--stack", d.stack), ("--description", d.description),
                            ("--repository", d.repository)):
            if value:
                parts += [flag, value]
        if d.addons:
            for addon in d.addons:
                parts += ["--addon", addon]
        else:
            parts.append("--no-addons")
        for key, value in d.intent.items():
            parts += ["--intent", f"{key}={value}"]
        for package in split_words(d.extra_python):
            parts += ["--extra-python", package]
        for package in split_words(d.extra_packages):
            parts += ["--extra-package", package]
        parts += ["--build", "--version", d.image_version, "--follow"]
        self.app.push_screen(TextScreen("Equivalent command", " ".join(shlex.quote(p) for p in parts) + "\n"))
