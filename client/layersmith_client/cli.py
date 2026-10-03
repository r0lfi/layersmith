"""`layersmith`: command line interface to a LayerSmith server.

Results go to stdout; progress, warnings and errors go to stderr, so output
can be piped. With --json every result is one JSON document (or JSON Lines
for followed logs) with no colour and no prompts. Exit codes are listed in
EXIT_CODES and in docs/client.md.

Run without arguments in a terminal, it opens the interactive TUI.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
import time
from pathlib import Path

from layersmith_client import __version__, ops
from layersmith_client import config as client_config
from layersmith_client.api import (
    EXIT_AUTH, EXIT_ERROR, EXIT_FILE, EXIT_INTERRUPTED, EXIT_JOB_FAILED, EXIT_NOT_FOUND, EXIT_OK, EXIT_REJECTED,
    EXIT_UNKNOWN_OUTCOME, EXIT_UNREACHABLE, EXIT_UNSUPPORTED, EXIT_USAGE, ApiClient, ClientError, FileProblem,
    JobFailed, Rejected,
)
from layersmith_client.text import (
    SEVERITIES, build_status, check_status, clean, format_bytes, format_duration, format_when,
    security_status, severity_summary, short,
)

EXIT_CODES = {
    EXIT_OK: "success",
    EXIT_ERROR: "unexpected error, or a server error (HTTP 5xx)",
    EXIT_USAGE: "invalid command line, or a confirmation was required and not given",
    EXIT_UNREACHABLE: "the server could not be reached (network, DNS, TLS); nothing was changed",
    EXIT_REJECTED: "the server refused the request (validation, conflict, too large)",
    EXIT_NOT_FOUND: "project, build, scan or file not found",
    EXIT_AUTH: "authentication needed or refused, a redirect, or an HTML page instead of the API",
    EXIT_UNKNOWN_OUTCOME: "a change was sent but no answer arrived; check before repeating it",
    EXIT_FILE: "local file problem: exists, missing, checksum mismatch, interrupted transfer",
    EXIT_JOB_FAILED: "with --wait: the build or scan finished unsuccessfully",
    EXIT_UNSUPPORTED: "the server is too old for this operation",
    EXIT_INTERRUPTED: "interrupted (Ctrl+C); server jobs keep running",
}


class Abort(ClientError):
    exit_code = EXIT_USAGE


# ------------------------------------------------------------- output

class Output:
    """Where and how results are written for one invocation."""

    def __init__(self, as_json: bool):
        self.json = as_json
        self.interactive = sys.stdin.isatty() and sys.stderr.isatty() and not as_json
        self.progress_tty = sys.stderr.isatty() and not as_json

    def result(self, data, human=None) -> None:
        """Print a command's result: JSON, or the human rendering."""
        if self.json:
            sys.stdout.write(json.dumps(data, indent=2, default=str) + "\n")
        elif human is not None:
            human(data)
        sys.stdout.flush()

    def jsonl(self, record: dict) -> None:
        sys.stdout.write(json.dumps(record, default=str) + "\n")
        sys.stdout.flush()

    def info(self, message: str) -> None:
        """Progress and diagnostics: stderr, in every mode, so stdout stays pure."""
        sys.stderr.write(message + "\n")
        sys.stderr.flush()

    def warn(self, message: str) -> None:
        sys.stderr.write("warning: " + message + "\n")
        sys.stderr.flush()

    def confirm(self, question: str, assume_yes: bool) -> None:
        """Ask before a destructive action. Without a terminal, --yes is required."""
        if assume_yes:
            return
        if not self.interactive:
            raise Abort(f"{question} Refusing without confirmation in non-interactive mode; pass --yes.")
        sys.stderr.write(f"{question} [y/N] ")
        sys.stderr.flush()
        answer = sys.stdin.readline().strip().lower()
        if answer not in ("y", "yes"):
            raise Abort("Cancelled; nothing was changed.")


def width() -> int:
    return max(60, shutil.get_terminal_size((100, 24)).columns)


def table(rows: list[list[object]], headers: list[str], flex: int | None = None) -> None:
    """Plain-text table. `flex` names the column that is shortened to fit."""
    cells = [[clean(cell) for cell in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in cells:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    # Only shorten for a terminal; piped output keeps every character.
    if flex is not None and sys.stdout.isatty():
        budget = width() - sum(w + 2 for i, w in enumerate(widths) if i != flex)
        widths[flex] = max(10, min(widths[flex], budget))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sys.stdout.write(line.rstrip() + "\n")
    for row in cells:
        sys.stdout.write("  ".join(short(c, widths[i]).ljust(widths[i]) for i, c in enumerate(row)).rstrip() + "\n")


def facts(pairs: list[tuple[str, object]]) -> None:
    label_width = max(len(label) for label, _ in pairs) if pairs else 0
    for label, value in pairs:
        sys.stdout.write(f"{label.ljust(label_width)}  {clean(value) if value not in (None, '') else '-'}\n")


def block(title: str, text: str) -> None:
    sys.stdout.write(f"\n{title}\n{'-' * len(title)}\n{clean(text, keep_newlines=True).rstrip()}\n")


class Progress:
    """Byte progress on stderr, only when stderr is a terminal; throttled."""

    def __init__(self, out: Output, label: str):
        self.out, self.label, self.last = out, label, 0.0

    def __call__(self, done: int, total: int | None) -> None:
        if not self.out.progress_tty:
            return
        now = time.monotonic()
        if now - self.last < 0.3 and (total is None or done < total):
            return
        self.last = now
        amount = f"{format_bytes(done)} of {format_bytes(total)}" if total else format_bytes(done)
        sys.stderr.write(f"\r{self.label}: {amount}   ")
        sys.stderr.flush()

    def done(self) -> None:
        if self.out.progress_tty and self.last:
            sys.stderr.write("\n")


# --------------------------------------------------------- client setup

def config_from_args(args) -> client_config.ClientConfig:
    flags = {"server": args.server, "ca_cert": args.ca_cert, "timeout": args.timeout}
    path = Path(args.config).expanduser() if args.config else None
    try:
        return client_config.resolve(flags, path=path)
    except client_config.ConfigError as exc:
        raise Abort(str(exc))


def make_client(args, out: Output) -> ApiClient:
    cfg = config_from_args(args)

    def prompt():
        if not out.interactive:
            return None
        return getpass.getpass(f"Password for {cfg.username} at {cfg.server}: ", stream=sys.stderr)

    return ApiClient(cfg, password_prompt=prompt)


# ------------------------------------------------------------ commands

def cmd_version(args, out):
    data = {"client": __version__, "python": sys.version.split()[0]}
    out.result(data, lambda d: sys.stdout.write(f"layersmith-client {d['client']} (Python {d['python']})\n"))


def cmd_config_show(args, out):
    cfg = config_from_args(args)
    rows = cfg.describe()
    data = {"path": str(cfg.path), "settings": rows, "unknown_keys": cfg.unknown_keys}

    def human(d):
        sys.stdout.write(f"Config file: {d['path']}\n\n")
        table([[r["setting"], r["value"] if r["value"] is not None else "-", r["source"], r["variable"]]
               for r in d["settings"]], ["SETTING", "VALUE", "SOURCE", "ENVIRONMENT"], flex=1)
        if d["unknown_keys"]:
            out.warn(f"unknown keys in the config file are ignored: {', '.join(d['unknown_keys'])}")
    out.result(data, human)


def cmd_config_set(args, out):
    path = Path(args.config).expanduser() if args.config else client_config.config_path()
    try:
        value = client_config.check_value(args.key, args.value)
        stored = client_config.load_file(path)
    except client_config.ConfigError as exc:
        raise Rejected(str(exc))
    stored[args.key] = value
    client_config.save_file(stored, path)
    variable = client_config.SETTINGS[args.key][0]
    if os.environ.get(variable):
        out.warn(f"{variable} is set in the environment and takes precedence over the config file")
    out.result({"path": str(path), "setting": args.key, "value": value},
               lambda d: sys.stdout.write(f"{d['setting']} = {d['value']}  ({d['path']})\n"))


def cmd_config_unset(args, out):
    path = Path(args.config).expanduser() if args.config else client_config.config_path()
    try:
        stored = client_config.load_file(path)
    except client_config.ConfigError as exc:
        raise Rejected(str(exc))
    removed = stored.pop(args.key, None) is not None
    if removed:
        client_config.save_file(stored, path)
    out.result({"path": str(path), "setting": args.key, "removed": removed},
               lambda d: sys.stdout.write(f"{d['setting']} {'removed' if removed else 'was not set'}\n"))


def cmd_config_path(args, out):
    path = Path(args.config).expanduser() if args.config else client_config.config_path()
    out.result({"path": str(path)}, lambda d: sys.stdout.write(d["path"] + "\n"))


def cmd_doctor(args, out):
    cfg = config_from_args(args)
    rows = ops.doctor(cfg, lambda: make_client(args, out))

    def human(data):
        for scope in ("client", "server"):
            sys.stdout.write(f"\n{scope.upper()}\n")
            table([[r["status"].upper(), r["check"], r["detail"]] for r in data if r["scope"] == scope],
                  ["STATUS", "CHECK", "DETAIL"], flex=2)
    out.result(rows, human)
    if any(r["status"] == "fail" for r in rows):
        return EXIT_UNREACHABLE if any(r["check"] == "API reachable" and r["status"] == "fail"
                                       for r in rows) else EXIT_ERROR
    return EXIT_OK


def cmd_dashboard(args, out):
    with make_client(args, out) as client:
        stats = client.get("/stats")

    def human(d):
        facts([("Projects", d["projects"]), ("Builds", d["builds"]), ("Images ready", d["images"]),
               ("Stored archives", format_bytes(d["storage_bytes"]))])
        sys.stdout.write("\nRecent builds\n")
        builds_table(d.get("recent") or [])
    out.result(stats, human)


def builds_table(builds: list[dict]) -> None:
    table([[f"#{b['number']}", b.get("project_name") or "-", b["version"], build_status(b["status"]),
            format_when(b.get("started_at") or b.get("created_at")), format_duration(b.get("duration_seconds")),
            format_bytes(b.get("image_size")), b["id"]] for b in builds],
          ["BUILD", "PROJECT", "VERSION", "STATUS", "STARTED", "TOOK", "SIZE", "ID"], flex=1)


# ---- catalog

CATALOG_SECTIONS = ("distributions", "architectures", "templates", "presets", "packages", "tools", "families")


def cmd_catalog(args, out):
    with make_client(args, out) as client:
        catalog = client.get("/catalog")
    section = args.section
    if section == "packages":
        data = catalog.get("categories", [])
    else:
        data = catalog.get(section) if section else catalog

    def human(d):
        if section is None:
            for name in CATALOG_SECTIONS:
                sys.stdout.write(f"{name}\n")
            sys.stdout.write("\nShow one with: layersmith catalog <section>\n")
        elif section == "distributions":
            table([[x["name"], x["family"], ", ".join(f"{v} ({x['sources'][v]})" for v in x["versions"])]
                   for x in d], ["DISTRIBUTION", "FAMILY", "VERSIONS (BASE IMAGE)"], flex=2)
        elif section == "architectures":
            for arch in d:
                sys.stdout.write(f"{arch}\n")
        elif section == "families":
            table([[k, v] for k, v in d.items()], ["FAMILY", "PACKAGE MANAGER"])
        elif section == "templates":
            table([[t["name"], t.get("description", ""), ", ".join((t.get("spec") or {}).get("presets") or [])]
                   for t in d], ["TEMPLATE", "DESCRIPTION", "PRESETS"], flex=1)
        elif section == "presets":
            table([[p["name"], p.get("description", ""), " ".join(p.get("packages") or [])] for p in d],
                  ["PRESET", "DESCRIPTION", "CAPABILITIES"], flex=1)
        elif section == "packages":
            for category in d:
                sys.stdout.write(f"{clean(category['name'])}: {clean(' '.join(category['packages']))}\n")
        elif section == "tools":
            table([[k, v.get("install_path"), v.get("default_version")] for k, v in d.items()],
                  ["TOOL", "INSTALL PATH", "DEFAULT VERSION"])
    out.result(data, human)


def cmd_templates_list(args, out):
    args.section = "templates"
    cmd_catalog(args, out)


# ---- projects

def cmd_projects_list(args, out):
    with make_client(args, out) as client:
        projects = client.get("/projects")
    out.result(projects, lambda d: table(
        [[p["name"], p.get("template") or "-", "Advanced" if p.get("mode") == "advanced" else "Generated",
          p.get("base_image") or "-", p.get("next_version"), p.get("build_count", 0), p["id"]] for p in d],
        ["NAME", "TEMPLATE", "MODE", "BASE IMAGE", "NEXT", "BUILDS", "ID"], flex=3))


def project_human(project: dict, show_containerfile: bool = True) -> None:
    facts([("Name", project["name"]), ("Description", project.get("description")),
           ("Repository", project.get("repository")), ("Template", project.get("template")),
           ("Mode", "Advanced (own Containerfile)" if project.get("mode") == "advanced"
            else "Generated from the definition"),
           ("Base image", project.get("base_image")), ("Base digest", project.get("base_digest")),
           ("Next version", project.get("next_version")), ("Created", project.get("created_at")),
           ("ID", project["id"])])
    if project.get("packages"):
        block("Resolved packages", " ".join(project["packages"]))
    for warning in project.get("warnings") or []:
        sys.stdout.write(f"WARNING: {clean(warning)}\n")
    if project.get("training"):
        recipe = project["training"]
        block("LLM training template", f"{recipe.get('profile_name')} {recipe.get('profile_version')} - "
                                       f"{recipe.get('stack_name')} on {recipe.get('target_name')}")
    if show_containerfile and project.get("containerfile"):
        block("Containerfile", project["containerfile"])
    builds = project.get("builds") or []
    sys.stdout.write("\nBuilds\n")
    if builds:
        builds_table(builds)
    else:
        sys.stdout.write("No builds yet.\n")


def cmd_projects_show(args, out):
    with make_client(args, out) as client:
        project = ops.resolve_project(client, args.project)
    out.result(project, lambda d: project_human(d, show_containerfile=not args.brief))


def spec_from_flags(args, document: dict) -> dict:
    """Apply the definition flags on top of a spec file (or an empty one)."""
    spec = document.setdefault("spec", {})
    if args.template:
        document["template"] = args.template
    if args.distribution or args.distro_version:
        base = {key: value for key, value in (spec.get("base") or {}).items() if key in ("distribution", "version")}
        if args.distribution:
            base["distribution"] = args.distribution
        if args.distro_version:
            base["version"] = args.distro_version
        spec["base"] = base
    if args.base_image:
        spec["base"] = {"source": args.base_image, "family": args.family}
    if args.arch:
        spec["architecture"] = args.arch
    for key, values in (("presets", args.preset), ("packages", args.package), ("extra_packages", args.extra_package),
                        ("tests", args.test)):
        if values:
            spec[key] = list(dict.fromkeys([*(spec.get(key) or []), *values]))
    if args.enable_epel:
        spec["enable_epel"] = True
    for item in args.file or []:
        parts = item.split(":")
        if len(parts) < 2:
            raise Abort(f"--file expects LOCAL:DESTINATION[:MODE], not {item!r}")
        entry = {"path": parts[0], "destination": parts[1]}
        if len(parts) > 2:
            entry["mode"] = parts[2]
        spec.setdefault("files", []).append(entry)
    for key, flag in (("pre_build", args.pre_build), ("post_install", args.post_install),
                      ("entrypoint", args.entrypoint)):
        if flag:
            spec.setdefault("scripts", {})[key] = ops.read_text_file(Path(flag), limit=64 * 1024)
    for item in args.env or []:
        name, _, value = item.partition("=")
        spec.setdefault("env", []).append({"name": name, "value": value})
    if not spec:
        document.pop("spec")
    return document


def load_document(args) -> tuple[dict, Path]:
    if args.spec:
        path = Path(args.spec).expanduser()
        return ops.load_spec_file(path), path.resolve().parent
    return {}, Path.cwd()


def cmd_projects_create(args, out):
    document, base_dir = load_document(args)
    if args.name:
        document["name"] = args.name
    if args.description is not None:
        document["description"] = args.description
    if args.repository:
        document["repository"] = args.repository
    document = spec_from_flags(args, document)
    document.setdefault("mode", "gui")
    if document.get("mode") == "gui" and not document.get("spec"):
        raise Abort("Nothing to create: give --spec FILE, or definition flags such as "
                    "--template Developer --distribution Ubuntu --distro-version 24.04")
    with make_client(args, out) as client:
        project = ops.create_from_document(
            client, document, base_dir,
            on_upload=lambda local, r: out.info(f"Uploaded {local} (sha256 {r['sha256'][:12]}...)"))
        out.info(f"Created project {project['name']} ({project['id']})")
        build = None
        if args.build:
            build = start_and_maybe_wait(client, project["id"], args, out)
    finish_create(out, args, project, build)
    return exit_for_build(build)


def finish_create(out: Output, args, project: dict, build: dict | None) -> None:
    if out.json and build is not None and (args.wait or args.follow):
        out.jsonl({"type": "done", "project": project, "build": summary_of(build)})
    elif build is not None:
        out.result({"project": project, "build": summary_of(build)},
                   lambda d: sys.stdout.write(f"{project['id']}\n{build['id']}\n"))
    else:
        out.result(project, lambda d: sys.stdout.write(f"{project['id']}\n"))


def cmd_projects_import(args, out):
    text = ops.read_text_file(Path(args.containerfile))
    with make_client(args, out) as client:
        project = client.post("/projects/import", {"name": args.name, "description": args.description or "",
                                                    "containerfile": text})
    out.info(f"Imported {args.containerfile} as project {project['name']} (Advanced mode)")
    out.result(project, lambda d: sys.stdout.write(f"{d['id']}\n"))


def cmd_projects_update(args, out):
    with make_client(args, out) as client:
        project = ops.resolve_project(client, args.project)
        if args.containerfile:
            if project.get("mode") != "advanced":
                raise Rejected(f"{project['name']} generates its Containerfile from its definition; "
                               "only Advanced-mode projects take a Containerfile")
            payload = {"name": project["name"], "description": project.get("description") or "",
                       "repository": project.get("repository"), "template": project.get("template"),
                       "mode": "advanced", "containerfile": ops.read_text_file(Path(args.containerfile))}
        else:
            document, base_dir = load_document(args)
            if not document:
                document = ops.spec_document(project)
            if document.get("mode", "gui") == "advanced":
                raise Rejected("Use --containerfile to change an Advanced-mode project")
            document = spec_from_flags(args, document)
            document["name"] = project["name"]
            payload, _ = ops.materialise(
                client, document, base_dir,
                on_upload=lambda local, r: out.info(f"Uploaded {local} (sha256 {r['sha256'][:12]}...)"))
            payload["mode"] = "gui"
            payload.setdefault("template", project.get("template") or "Custom")
        if args.description is not None:
            payload["description"] = args.description
        if args.repository:
            payload["repository"] = args.repository
        updated = client.put(f"/projects/{project['id']}", payload)
    out.info(f"Updated project {updated['name']}. Existing builds are unchanged; build a new version to use it.")
    out.result(updated, lambda d: sys.stdout.write(f"{d['id']}\n"))


def cmd_projects_clone(args, out):
    with make_client(args, out) as client:
        project = ops.resolve_project(client, args.project)
        clone = client.post(f"/projects/{project['id']}/clone", {"name": args.name})
    out.info(f"Cloned {project['name']} as {clone['name']}")
    out.result(clone, lambda d: sys.stdout.write(f"{d['id']}\n"))


def cmd_projects_delete(args, out):
    with make_client(args, out) as client:
        project = ops.resolve_project(client, args.project)
        out.confirm(f"Delete project {clean(project['name'])}? Built images and archives are kept.", args.yes)
        client.delete(f"/projects/{project['id']}")
    out.result({"deleted": project["id"], "name": project["name"]},
               lambda d: sys.stdout.write(f"Deleted project {clean(d['name'])}\n"))


def cmd_projects_containerfile(args, out):
    with make_client(args, out) as client:
        project = ops.resolve_project(client, args.project)
    data = {"project": project["id"], "mode": project.get("mode"), "containerfile": project.get("containerfile"),
            "warnings": project.get("warnings") or []}
    out.result(data, lambda d: sys.stdout.write(clean(d["containerfile"], keep_newlines=True)))


def cmd_projects_export_spec(args, out):
    with make_client(args, out) as client:
        project = ops.resolve_project(client, args.project)
    document = ops.spec_document(project)
    if args.output:
        path = Path(args.output).expanduser()
        if path.exists() and not args.force:
            raise FileProblem(f"{path} already exists", hint="Pass --force to replace it.")
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        out.info(f"Wrote {path}. Recreate with: layersmith projects create --spec {path} --name <new-name>")
        out.result({"path": str(path), "spec": document}, lambda d: None)
    else:
        sys.stdout.write(json.dumps(document, indent=2) + "\n")


def cmd_preview(args, out):
    document, base_dir = load_document(args)
    if args.name:
        document["name"] = args.name
    document = spec_from_flags(args, document)
    # Preview stores nothing, so local files are only checksummed, not uploaded.
    payload = {key: document[key] for key in ops.PROJECT_FIELDS if key in document}
    for section, index, local in ops.local_references(document):
        path = Path(local).expanduser()
        path = path if path.is_absolute() else base_dir / path
        entry = payload["spec"][section][index]
        entry.pop("path", None)
        entry["sha256"] = sha256_of(path)
    payload.setdefault("name", "image")
    with make_client(args, out) as client:
        preview = client.post("/preview", payload)

    def human(d):
        for warning in d.get("warnings") or []:
            sys.stderr.write(f"WARNING: {clean(warning)}\n")
        sys.stdout.write(clean(d["containerfile"], keep_newlines=True))
    out.result(preview, human)


def sha256_of(path: Path) -> str:
    import hashlib
    if not path.is_file():
        raise FileProblem(f"Not a file: {path}")
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cmd_upload(args, out):
    results = []
    with make_client(args, out) as client:
        for local in args.files:
            progress = Progress(out, f"Uploading {local}")
            results.append(client.upload(Path(local), progress=progress))
            progress.done()
    out.result(results, lambda d: table([[r["local_path"], r["sha256"], format_bytes(r["size"])] for r in d],
                                        ["LOCAL FILE", "SHA256 (use in a spec)", "SIZE"]))


# ---- builds

_waiting_on: dict = {}


def start_and_maybe_wait(client: ApiClient, project_id: str, args, out: Output) -> dict:
    build = ops.start_build(client, project_id, args.version)
    out.info(f"Build #{build['number']} accepted: {build['image_ref']} (id {build['id']}, status {build['status']})")
    if args.wait or args.follow:
        build = follow_build(client, build["id"], args, out)
    return build


def follow_build(client: ApiClient, build_id: str, args, out: Output) -> dict:
    _waiting_on.update(kind="build", id=build_id)
    show_log = getattr(args, "follow", False)

    def on_line(line):
        if out.json:
            out.jsonl({"type": "log", "build_id": build_id, "line": line})
        else:
            sys.stderr.write(clean(line) + "\n")

    def on_status(status):
        if out.json:
            out.jsonl({"type": "status", "build_id": build_id, "status": status})
        else:
            out.info(f"[build {build_status(status)}]")

    build = ops.wait_for_build(client, build_id, on_status=on_status, on_line=on_line if show_log else None)
    _waiting_on.clear()
    return build


def exit_for_build(build: dict | None) -> int:
    if build and build.get("status") in ("failed", "cancelled"):
        sys.stderr.write(f"Build #{build['number']} {build['status']}: {clean(build.get('error') or '')}\n")
        return EXIT_JOB_FAILED
    return EXIT_OK


def cmd_build(args, out):
    with make_client(args, out) as client:
        project = ops.resolve_project(client, args.project)
        build = start_and_maybe_wait(client, project["id"], args, out)
    if out.json and (args.follow or args.wait):
        out.jsonl({"type": "done", "build": summary_of(build)})
    else:
        out.result(build, lambda d: build_human(d) if (args.wait or args.follow) else
                   sys.stdout.write(f"{d['id']}\n"))
    return exit_for_build(build) if (args.wait or args.follow) else EXIT_OK


def summary_of(build: dict) -> dict:
    return {key: value for key, value in build.items() if key not in ("log", "containerfile")}


def cmd_builds_list(args, out):
    with make_client(args, out) as client:
        if args.project:
            builds = ops.resolve_project(client, args.project).get("builds") or []
        else:
            builds = client.get("/builds", params={"limit": args.limit})
    if args.status:
        builds = [b for b in builds if b["status"] == args.status]
    out.result(builds, builds_table)


def build_human(build: dict) -> None:
    facts([("Build", f"#{build['number']}"), ("Project", build.get("project_name")),
           ("Version", build["version"]), ("Status", build_status(build["status"])),
           ("Error", build.get("error")), ("Image", build.get("image_ref")),
           ("Architecture", build.get("architecture")), ("Base image", build.get("base_image")),
           ("Base digest", build.get("base_digest")), ("Image digest", build.get("image_digest")),
           ("Image size", format_bytes(build.get("image_size"))),
           ("Image archive", f"{format_bytes(build.get('export_size'))}, sha256 {build.get('export_sha256')}"
            if build.get("has_export") else "not available on the server"),
           ("Air-gap bundle", f"{format_bytes(build.get('airgap_size'))}, sha256 {build.get('airgap_sha256')}"
            if build.get("has_airgap") else "not created (layersmith bundle create)"),
           ("Started", build.get("started_at")), ("Finished", build.get("finished_at")),
           ("Duration", format_duration(build.get("duration_seconds"))),
           ("Training profile", build.get("training_profile")), ("ID", build["id"])])
    if build.get("packages"):
        block(f"Packages ({len(build['packages'])})", " ".join(build["packages"]))
    if build.get("training"):
        sys.stdout.write("\nTraining checks (this build only)\n")
        checks_table(build["training"].get("checks") or [])


def cmd_builds_show(args, out):
    with make_client(args, out) as client:
        build = ops.get_build(client, args.build)
    out.result(summary_of(build) if not args.full else build, build_human)


def cmd_builds_containerfile(args, out):
    with make_client(args, out) as client:
        build = ops.get_build(client, args.build)
    out.result({"build": build["id"], "containerfile": build.get("containerfile")},
               lambda d: sys.stdout.write(clean(d["containerfile"] or "", keep_newlines=True)))


def cmd_builds_manifest(args, out):
    with make_client(args, out) as client:
        build = ops.get_build(client, args.build)
    sys.stdout.write(json.dumps(build.get("manifest"), indent=2) + "\n")


def cmd_builds_logs(args, out):
    with make_client(args, out) as client:
        build_id = ops.resolve_build_id(client, args.build)
        follower = ops.LogFollower(client, build_id, tail_bytes=args.tail * 200 if args.tail else None)
        _waiting_on.update(kind="build", id=build_id)

        def emit(line):
            if out.json:
                out.jsonl({"type": "log", "build_id": build_id, "line": line})
            else:
                sys.stdout.write(clean(line) + "\n")
                sys.stdout.flush()

        lost_since = None
        while True:
            try:
                for line in follower.poll():
                    emit(line)
                lost_since = None
            except ClientError as exc:
                if not args.follow or exc.exit_code != EXIT_UNREACHABLE:
                    raise
                lost_since = lost_since or time.monotonic()
                if time.monotonic() - lost_since > 300:
                    raise
                out.info("[connection lost - retrying; the build continues on the server]")
                time.sleep(ops.POLL_SECONDS * 2)
                continue
            if not args.follow or follower.finished:
                break
            if follower.status in ("ready", "failed", "cancelled") and not follower.incremental:
                break
            time.sleep(ops.POLL_SECONDS)
        _waiting_on.clear()
        if args.follow and out.json:
            out.jsonl({"type": "done", "build_id": build_id, "status": follower.status})
        elif args.follow:
            out.info(f"[build {build_status(follower.status)}]")
    if args.follow and follower.status in ("failed", "cancelled"):
        return EXIT_JOB_FAILED
    return EXIT_OK


def cmd_builds_wait(args, out):
    args.follow = args.logs
    with make_client(args, out) as client:
        build = follow_build(client, ops.resolve_build_id(client, args.build), args, out)
    if out.json:
        out.jsonl({"type": "done", "build": summary_of(build)})
    else:
        out.result(summary_of(build), build_human)
    return exit_for_build(build)


# ---- export and bundles

def cmd_export(args, out):
    with make_client(args, out) as client:
        build = ops.get_build(client, args.build)
        progress = Progress(out, "Downloading image archive")
        result = ops.download_artifact(client, build, "export", Path(args.output), overwrite=args.force,
                                       progress=progress)
        progress.done()
    report_download(out, result)


def report_download(out: Output, result: dict) -> None:
    if not result["verified"]:
        out.warn("the server recorded no checksum for this file, so it was NOT verified")
    out.result(result, lambda d: sys.stdout.write(
        f"{d['path']}\n{format_bytes(d['size'])}, sha256 {d['sha256']} "
        f"({'verified against the server' if d['verified'] else 'not verified'})\n"))


def cmd_bundle_create(args, out):
    with make_client(args, out) as client:
        build = ops.get_build(client, args.build)
        out.info(f"Creating the air-gap bundle for build #{build['number']} on the server; large images take a "
                 "while. Interrupting only stops waiting for the answer.")
        _waiting_on.update(kind="bundle", id=build["id"])
        bundle = ops.create_bundle(client, build["id"])
        _waiting_on.clear()
    out.result(bundle, lambda d: sys.stdout.write(
        f"{d['filename']}  {format_bytes(d['size'])}  sha256 {d['sha256']}\n"))


def cmd_bundle_export(args, out):
    with make_client(args, out) as client:
        build = ops.get_build(client, args.build)
        if not build.get("has_airgap"):
            if args.no_create:
                raise FileProblem(f"Build #{build['number']} has no air-gap bundle on the server",
                                  hint=f"Create it: layersmith bundle create {build['id']}")
            out.info(f"No bundle on the server yet; creating it for build #{build['number']}...")
            _waiting_on.update(kind="bundle", id=build["id"])
            ops.create_bundle(client, build["id"])
            _waiting_on.clear()
            build = ops.get_build(client, build["id"])
        progress = Progress(out, "Downloading air-gap bundle")
        result = ops.download_artifact(client, build, "airgap", Path(args.output), overwrite=args.force,
                                       progress=progress)
        progress.done()
    report_download(out, result)


# ---- scanning

def cmd_scanner(args, out):
    with make_client(args, out) as client:
        status = client.get("/scanner")

    def human(d):
        configured = d.get("name") != "none"
        facts([("Scanner", d.get("name") if configured else "none (not configured)"),
               ("Selection", d.get("selection")), ("Available", "yes" if d.get("available") else "no"),
               ("Detail", d.get("detail")), ("Version", d.get("version")),
               ("Scan kinds requested", ", ".join(d.get("scan_kinds") or []) or "-"),
               ("Supported kinds", ", ".join(d.get("supported_kinds") or []) or "-"),
               ("SBOM", "yes" if d.get("generate_sbom") else "no"),
               ("Scan after build", "yes" if d.get("scan_after_build") else "no"),
               ("Database", (d.get("database") or {}).get("detail") or (d.get("database") or {}).get("version"))])
        others = d.get("scanners") or []
        if others:
            sys.stdout.write("\nScanners known to the server\n")
            table([[s["name"], "available" if s["available"] else "unavailable", s.get("detail")] for s in others],
                  ["SCANNER", "STATE", "DETAIL"], flex=2)
    out.result(status, human)


def scans_table(scans: list[dict]) -> None:
    table([[s["id"], s["state"].upper(), s.get("reason"), f"{s.get('scanner')} {s.get('scanner_version') or ''}",
            s.get("total") if s["state"] == "completed" else "-", severity_summary(s.get("severity") or {}) or "-",
            format_when(s.get("finished_at") or s.get("created_at"))] for s in scans],
          ["SCAN", "STATE", "REASON", "SCANNER", "FINDINGS", "SEVERITY", "WHEN"], flex=5)


def cmd_scan(args, out):
    with make_client(args, out) as client:
        build_id = ops.resolve_build_id(client, args.build)
        scan = ops.start_scan(client, build_id, args.kind)
        out.info(f"Scan {scan['id']} accepted ({scan['state']})")
        if args.wait:
            _waiting_on.update(kind="scan", id=scan["id"], build=build_id)
            scan = ops.wait_for_scan(client, build_id, scan["id"],
                                     on_status=lambda s: out.info(f"[scan {s.upper()}]"))
            _waiting_on.clear()
    out.result(scan, lambda d: scans_table([d]))
    if args.wait and scan.get("state") == "failed":
        sys.stderr.write(f"Scan failed: {clean(scan.get('error'))}\n")
        return EXIT_JOB_FAILED
    return EXIT_OK


def cmd_scans_list(args, out):
    with make_client(args, out) as client:
        build_id = ops.resolve_build_id(client, args.build)
        scans = client.get(f"/builds/{build_id}/scans")
        try:
            scanner = client.get("/scanner")
        except ClientError:
            scanner = None
    label, _, explanation = security_status(scanner, scans)

    def human(d):
        sys.stdout.write(f"Security status: {label} - {explanation}\n\n")
        if d:
            scans_table(d)
    out.result({"status": label, "explanation": explanation, "scans": scans} if out.json else scans, human)


def cmd_scans_show(args, out):
    with make_client(args, out) as client:
        scan = client.get(f"/scans/{args.scan}", params={k: v for k, v in
                                                         {"kind": args.kind, "severity": args.severity,
                                                          "limit": args.limit}.items() if v})

    def human(d):
        facts([("Scan", d["id"]), ("State", d["state"].upper()), ("Error", d.get("error")),
               ("Scanner", f"{d.get('scanner')} {d.get('scanner_version') or ''}"),
               ("Database", f"{(d.get('database') or {}).get('version') or '-'}, updated "
                            f"{(d.get('database') or {}).get('updated_at') or '-'}"
                            f"{' (offline import)' if (d.get('database') or {}).get('offline') else ''}"),
               ("Kinds", ", ".join(d.get("kinds") or [])), ("Total findings", d.get("total")),
               ("Vulnerabilities", severity_summary(d.get("severity") or {}) or "none"),
               ("SBOM", f"{d['sbom']['format']}, {d['sbom']['components']} components" if d.get("sbom") else "none"),
               ("Note", d.get("identity_note")), ("Finished", d.get("finished_at"))])
        findings = d.get("findings") or []
        if d["state"] == "completed" and not findings:
            sys.stdout.write("\nNo findings match - the absence of a match, not a guarantee.\n")
        if findings:
            sys.stdout.write("\n")
            table([[f["severity"].upper(), f["kind"], f["identifier"],
                    f.get("package_name") or f.get("target"),
                    f.get("installed_version") or "", f.get("fixed_version") or f.get("masked_match") or "",
                    f.get("title")] for f in findings],
                  ["SEVERITY", "KIND", "ID", "PACKAGE/LOCATION", "INSTALLED", "FIXED/MATCH", "TITLE"], flex=6)
            if d.get("finding_total", 0) > len(findings):
                sys.stdout.write(f"\nShowing the {len(findings)} worst of {d['finding_total']}; "
                                 "narrow with --severity/--kind or raise --limit.\n")
    out.result(scan, human)


def cmd_scans_sbom(args, out):
    with make_client(args, out) as client:
        scan = client.get(f"/scans/{args.scan}", params={"limit": 1})
        result = ops.download_sbom(client, scan, Path(args.output), overwrite=args.force)
    report_download(out, result)


# ---- training

def training_request(args) -> dict:
    request = {"profile": args.profile}
    if args.target:
        request["target"] = args.target
    if args.stack:
        request["stack"] = args.stack
    if getattr(args, "no_addons", False):
        request["addons"] = []
    elif args.addon is not None:
        request["addons"] = args.addon
    if args.intent:
        intent = {}
        for item in args.intent:
            key, _, value = item.partition("=")
            if not value:
                raise Abort(f"--intent expects KEY=VALUE, not {item!r}")
            intent[key] = value
        request["intent"] = intent
    if args.extra_python:
        request["extra_python"] = args.extra_python
    return request


def cmd_training_profiles(args, out):
    with make_client(args, out) as client:
        catalog = client.get("/training/catalog")
    profiles = catalog.get("profiles") or []
    targets = catalog.get("targets") or {}

    def human(d):
        sys.stdout.write(clean(catalog.get("category", {}).get("intro", "")) + "\n\n")
        table([[p["id"], p["name"] + (" (recommended)" if p.get("recommended") else ""), p.get("summary"),
                " / ".join(targets.get(t, {}).get("name", t) for t in p.get("targets") or [])] for p in d],
              ["PROFILE", "NAME", "SUMMARY", "TARGETS"], flex=2)
        sys.stdout.write("\nDetails: layersmith training profile <PROFILE>\n")
    out.result(profiles, human)


def cmd_training_catalog(args, out):
    with make_client(args, out) as client:
        catalog = client.get("/training/catalog")
    data = catalog.get(args.section) if args.section else catalog

    def human(d):
        if args.section == "concepts":
            for concept in d:
                sys.stdout.write(f"{clean(concept['term'])}\n  {clean(concept['text'])}\n")
        elif args.section == "intents":
            for key, group in d.items():
                sys.stdout.write(f"{key}: {clean(group['label'])} - {clean(group['hint'])}\n")
                for option in group["options"]:
                    sys.stdout.write(f"    {key}={option['id']}  {clean(option['label'])}\n")
        elif args.section == "targets":
            table([[k, v["name"], v["architecture"], "yes" if v["gpu"] else "no", v["summary"]] for k, v in d.items()],
                  ["TARGET", "NAME", "ARCH", "GPU", "SUMMARY"], flex=4)
        elif args.section == "stacks":
            table([[k, v["name"], v.get("torch") or "-", v.get("cuda") or "-", v.get("summary")] for k, v in d.items()],
                  ["STACK", "NAME", "TORCH", "CUDA", "SUMMARY"], flex=4)
        elif args.section == "addons":
            table([[k, v["name"], ", ".join(v.get("tools") or []), v.get("summary")] for k, v in d.items()],
                  ["ADD-ON", "NAME", "TOOLS", "SUMMARY"], flex=3)
        elif args.section == "tools":
            table([[k, v["name"], v.get("what")] for k, v in d.items()], ["TOOL", "NAME", "WHAT IT IS"], flex=2)
        elif args.section == "directories":
            table([[r["path"], r["host"], r["mode"], r["purpose"]] for r in d],
                  ["PATH IN IMAGE", "ON THE HOST", "MODE", "PURPOSE"], flex=3)
        else:
            sys.stdout.write(json.dumps(d, indent=2) + "\n")
    out.result(data, human)


def recipe_human(recipe: dict, resolved: dict | None = None) -> None:
    facts([("Profile", f"{recipe['profile_name']} {recipe.get('profile_version')} ({recipe['profile']})"),
           ("Target", recipe.get("target_name")), ("Stack", f"{recipe.get('stack_name')} - {recipe.get('stack_summary')}"),
           ("Base image", f"{recipe['base'].get('display')} ({recipe['base'].get('digest')})"),
           ("Why this base", recipe["base"].get("reason")), ("Architecture", f"linux/{recipe.get('architecture')}"),
           ("Python", recipe.get("python")), ("PyTorch", recipe.get("torch")), ("CUDA", recipe.get("cuda")),
           ("NVIDIA driver", recipe.get("driver")), ("GPU support", recipe.get("gpu_support")),
           ("Add-ons", ", ".join(recipe.get("addons") or []) or "none"),
           ("Lock digest", recipe.get("lock_digest"))])
    if recipe.get("host_requirements"):
        block("The machine that runs the image needs", "\n".join(f"- {r}" for r in recipe["host_requirements"]))
    tools = recipe.get("tools") or []
    if tools:
        sys.stdout.write("\nIncluded tools\n")
        table([[t["name"], t.get("version") or "-", "required" if t.get("required") else "optional",
                t.get("what")] for t in tools], ["TOOL", "VERSION", "", "WHAT IT IS"], flex=3)
    if recipe.get("versions"):
        block("Locked versions", "\n".join(f"{k}=={v}" for k, v in sorted(recipe["versions"].items())))
    for note in recipe.get("notes") or []:
        sys.stdout.write(f"NOTE: {clean(note)}\n")
    if resolved:
        if resolved.get("before_build"):
            block("Before you build", resolved["before_build"])
        contents = resolved.get("export_contents") or {}
        if contents:
            block("The export contains", "\n".join(f"- {x}" for x in contents.get("included") or []))
            block("Bring separately", "\n".join(f"- {x}" for x in contents.get("external") or []))


def cmd_training_profile(args, out):
    with make_client(args, out) as client:
        catalog = client.get("/training/catalog")
        profile = next((p for p in catalog.get("profiles") or [] if p["id"] == args.profile), None)
        if profile is None:
            raise Rejected(f"Unknown training profile {args.profile!r}",
                           hint="List them with: layersmith training profiles")
        resolved = client.post("/training/resolve", {"training": {"profile": profile["id"],
                                                                  "addons": profile.get("default_addons")}})

    def human(d):
        facts([("Profile", f"{profile['name']} ({profile['id']})"), ("Summary", profile.get("summary"))])
        block("Description", profile.get("description") or "")
        block("Best for", "\n".join(f"- {x}" for x in profile.get("best_for") or []))
        facts([("Targets", ", ".join(profile.get("targets") or [])), ("Stacks", ", ".join(profile.get("stacks") or [])),
               ("Add-ons", ", ".join(profile.get("addons") or [])),
               ("Default add-ons", ", ".join(profile.get("default_addons") or []))])
        if resolved.get("ok"):
            sys.stdout.write("\nDefault recipe\n")
            recipe_human(resolved["recipe"], resolved)
    out.result({"profile": profile, "default": resolved}, human)


def cmd_training_resolve(args, out):
    with make_client(args, out) as client:
        resolved = client.post("/training/resolve", {"training": training_request(args), "architecture": args.arch})

    def human(d):
        if not d.get("ok"):
            sys.stdout.write(f"CONFLICT: {clean(d.get('error'))}\n")
            if d.get("alternative"):
                sys.stdout.write(f"Working alternative ({clean(d.get('label') or '')}): "
                                 f"{json.dumps(d['alternative'])}\n")
            return
        recommendation = d.get("recommendation") or {}
        if recommendation:
            sys.stdout.write(f"Suggested for this purpose: {clean(recommendation.get('profile'))} - "
                             f"{clean(recommendation.get('reason'))}\n\n")
        recipe_human(d["recipe"], d)
    out.result(resolved, human)
    return EXIT_OK if resolved.get("ok") else EXIT_REJECTED


def cmd_training_create(args, out):
    request = training_request(args)
    spec = {"training": request}
    if args.extra_package:
        spec["extra_packages"] = args.extra_package
    with make_client(args, out) as client:
        catalog = client.get("/training/catalog")
        profile = next((p for p in catalog.get("profiles") or [] if p["id"] == args.profile), None)
        if profile is None:
            raise Rejected(f"Unknown training profile {args.profile!r}")
        if "addons" not in request:
            request["addons"] = profile.get("default_addons") or []
        resolved = client.post("/training/resolve", {"training": request})
        if not resolved.get("ok"):
            raise Rejected(clean(resolved.get("error")),
                           hint=f"Working alternative: {json.dumps(resolved.get('alternative'))}"
                           if resolved.get("alternative") else None)
        project = client.post("/projects", {"name": args.name, "description": args.description or "",
                                            "repository": args.repository, "template": f"LLM: {profile['name']}",
                                            "mode": "gui", "spec": spec})
        out.info(f"Created training project {project['name']} ({project['id']})")
        build = start_and_maybe_wait(client, project["id"], args, out) if args.build else None
    finish_create(out, args, project, build)
    return exit_for_build(build)


def checks_table(checks: list[dict]) -> None:
    table([[check_status(c.get("status")), c.get("label") or c.get("id"), c.get("summary")] for c in checks],
          ["RESULT", "CHECK", "SUMMARY"], flex=2)


def cmd_training_checks(args, out):
    with make_client(args, out) as client:
        build = ops.get_build(client, args.build)
    training = build.get("training")
    if not training:
        raise Rejected(f"Build #{build['number']} is not an LLM training build")

    def human(d):
        sys.stdout.write(f"Checks for build #{build['number']} ({build['status']}), "
                         f"{clean(d[0].get('applies_to') if d else '')}\n\n")
        checks_table(d)
        for row in d:
            if row.get("command"):
                block(f"Run the {row['id'].upper()} test on the target machine",
                      f"docker: {row['command']['docker']}\npodman: {row['command']['podman']}\n\n"
                      f"Then record it: layersmith training gpu-report {build['id']} --file report.json")
    out.result(training.get("checks") or [], human)


def cmd_training_guide(args, out):
    with make_client(args, out) as client:
        build = ops.get_build(client, args.build)
    training = build.get("training")
    if not training:
        raise Rejected(f"Build #{build['number']} is not an LLM training build")
    guide = training.get("getting_started") or {}

    def human(d):
        sys.stdout.write(f"Getting started with {clean(d.get('image'))}\n")
        for section in d.get("sections") or []:
            parts = [section.get("text") or ""]
            for key in ("code", "docker", "podman"):
                if section.get(key):
                    parts.append(f"[{key}]\n{section[key]}" if key != "code" else section[key])
            for step in section.get("steps") or []:
                parts.append(f"* {step.get('title')}")
                for key in ("text", "code", "docker", "podman"):
                    if step.get(key):
                        parts.append(f"  {step[key]}")
            for row in section.get("table") or []:
                parts.append(f"  {row['path']}  <- {row['host']} ({row['mode']}): {row['purpose']}")
            if section.get("note"):
                parts.append(f"Note: {section['note']}")
            block(section.get("title") or section.get("id") or "", "\n".join(p for p in parts if p))
        contents = training.get("export_contents") or {}
        block("The export contains", "\n".join(f"- {x}" for x in contents.get("included") or []))
        block("Bring separately (not in the image)", "\n".join(f"- {x}" for x in contents.get("external") or []))
    out.result({"getting_started": guide, "export_contents": training.get("export_contents")},
               lambda d: human(d["getting_started"]))


def cmd_training_gpu_report(args, out):
    text = sys.stdin.read() if args.file == "-" else ops.read_text_file(Path(args.file))
    try:
        report: object = json.loads(text)
    except ValueError:
        report = text  # the whole output; the server finds the result line
    with make_client(args, out) as client:
        build_id = ops.resolve_build_id(client, args.build)
        detail = client.post(f"/builds/{build_id}/gpu-report", {"report": report})
    out.result(detail.get("checks"), checks_table)


# ---- settings

def cmd_settings_show(args, out):
    with make_client(args, out) as client:
        settings = client.get("/settings")

    def human(d):
        backend = d.get("build_backend") or {}
        facts([("Server", f"{d.get('app_name')} {d.get('version')} (revision {str(d.get('revision'))[:12]})"),
               ("Source", d.get("source_url")), ("License", d.get("license")),
               ("Default architecture", d.get("default_architecture")),
               ("Default namespace", d.get("default_namespace")),
               ("Build backend", f"{backend.get('name')} (selected {backend.get('selection')}, "
                                 f"{'available' if backend.get('available') else 'UNAVAILABLE'}) {backend.get('detail')}"),
               ("Archive format", backend.get("archive_format"))])
        limits = d.get("limits")
        if limits:
            facts([("Max upload", format_bytes(limits.get("max_upload_bytes"))),
                   ("Max files per image", limits.get("max_files_per_image")),
                   ("Max build context", format_bytes(limits.get("max_context_bytes")))])
        storage = d.get("storage") or {}
        sys.stdout.write(f"\nServer storage: {format_bytes(storage.get('free'))} free of "
                         f"{format_bytes(storage.get('total'))}\n")
        sys.stdout.write("\nServer paths (on the server, not on this machine)\n")
        table([[p["field"], p["label"], p["path"], p["source"], "yes" if p["editable"] else "no"]
               for p in d.get("paths") or []], ["FIELD", "WHAT", "PATH", "SOURCE", "EDITABLE"], flex=2)
        sys.stdout.write("\nBuild runtimes\n")
        table([[r["name"], "available" if r["available"] else "unavailable", r.get("detail")]
               for r in backend.get("runtimes") or []], ["RUNTIME", "STATE", "DETAIL"], flex=2)
    out.result(settings, human)


def cmd_settings_storage_set(args, out):
    with make_client(args, out) as client:
        out.confirm(f"Change where the SERVER writes new {args.field} data to {args.path}? "
                    "Existing files stay where they are.", args.yes)
        result = client.put("/settings/storage", {"paths": {args.field: args.path}})
    out.result(result, lambda d: table([[p["field"], p["path"], p["source"]] for p in d["paths"]],
                                       ["FIELD", "PATH", "SOURCE"]))


def cmd_tui(args, out):
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise Abort("The TUI needs an interactive terminal. Use the subcommands instead; see: layersmith --help")
    cfg = config_from_args(args)
    from layersmith_client.tui.app import run
    return run(cfg)


# --------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="layersmith",
        description="LayerSmith client: build container images on a LayerSmith server from a terminal. "
                    "Without a command, opens the interactive interface.",
        epilog="Exit codes:\n" + "\n".join(f"  {code:>3}  {text}" for code, text in EXIT_CODES.items())
               + "\n\nDocumentation: docs/client.md in the LayerSmith repository.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--server", metavar="URL", help="LayerSmith server (overrides LAYERSMITH_SERVER and config)")
    parser.add_argument("--ca-cert", metavar="PEM", help="CA bundle for the server's certificate (internal CA)")
    parser.add_argument("--timeout", metavar="SECONDS", help="seconds to wait for the server")
    parser.add_argument("--config", metavar="FILE", help="client config file (default: XDG config directory)")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output; never prompts")
    parser.add_argument("--version", action="version", version=f"layersmith-client {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    def command(parent, name, handler, help_text, aliases=()):
        sub = parent.add_parser(name, help=help_text, description=help_text, aliases=list(aliases))
        sub.set_defaults(handler=handler)
        return sub

    def group(name, help_text):
        sub = commands.add_parser(name, help=help_text, description=help_text)
        sub.set_defaults(handler=None, group_parser=sub)
        return sub.add_subparsers(dest="subcommand", metavar="SUBCOMMAND")

    def output_options(sub, what="file"):
        sub.add_argument("--output", "-o", required=True, metavar="PATH",
                         help=f"local {what} or directory on THIS machine")
        sub.add_argument("--force", action="store_true", help="replace an existing local file")

    def definition_options(sub):
        sub.add_argument("--spec", metavar="FILE", help="project spec file (JSON, see docs/client.md)")
        sub.add_argument("--description")
        sub.add_argument("--repository", help="image repository, e.g. team/tools (default: namespace/name)")
        sub.add_argument("--template", help="template from `layersmith templates list`")
        sub.add_argument("--distribution", help="distribution from `layersmith catalog distributions`")
        sub.add_argument("--distro-version", help="version of the distribution")
        sub.add_argument("--base-image", help="custom base image instead of a distribution")
        sub.add_argument("--family", default="rpm", choices=["rpm", "deb", "apk"],
                         help="package family of --base-image")
        sub.add_argument("--arch", help="amd64 or arm64")
        sub.add_argument("--preset", action="append", help="tool set (repeatable)")
        sub.add_argument("--package", action="append", help="capability from `catalog packages` (repeatable)")
        sub.add_argument("--extra-package", action="append", help="exact distribution package (repeatable)")
        sub.add_argument("--enable-epel", action="store_true", help="enable EPEL on Enterprise Linux")
        sub.add_argument("--file", action="append", metavar="LOCAL:DEST[:MODE]",
                         help="upload a local file into the image (repeatable)")
        sub.add_argument("--pre-build", metavar="FILE", help="script run before packages are installed")
        sub.add_argument("--post-install", metavar="FILE", help="script run after packages are installed")
        sub.add_argument("--entrypoint", metavar="FILE", help="script that becomes the entrypoint")
        sub.add_argument("--env", action="append", metavar="NAME=VALUE", help="environment variable (repeatable)")
        sub.add_argument("--test", action="append", help="test command run in the built image (repeatable)")

    def wait_options(sub):
        sub.add_argument("--version", dest="version", metavar="X.Y.Z",
                         help="image version (default: the project's next version)")
        sub.add_argument("--wait", action="store_true", help="wait until the build finishes")
        sub.add_argument("--follow", action="store_true", help="wait and stream the build log to stderr")

    command(commands, "tui", cmd_tui, "open the interactive terminal interface")
    command(commands, "version", cmd_version, "show the client version (no server needed)")
    command(commands, "doctor", cmd_doctor, "check the client setup and the server, read-only")
    command(commands, "dashboard", cmd_dashboard, "counts and recent builds")

    sub = group("config", "client configuration (this machine only)")
    command(sub, "show", cmd_config_show, "show the effective settings and where each comes from")
    s = command(sub, "set", cmd_config_set, "store a setting in the client config file")
    s.add_argument("key", choices=list(client_config.SETTINGS))
    s.add_argument("value")
    s = command(sub, "unset", cmd_config_unset, "remove a setting from the client config file")
    s.add_argument("key", choices=list(client_config.SETTINGS))
    command(sub, "path", cmd_config_path, "print the config file location")

    s = command(commands, "catalog", cmd_catalog, "what the server offers: distributions, templates, presets...")
    s.add_argument("section", nargs="?", choices=CATALOG_SECTIONS)
    sub = group("templates", "image templates")
    command(sub, "list", cmd_templates_list, "list templates")

    sub = group("projects", "image projects (definitions)")
    command(sub, "list", cmd_projects_list, "list projects")
    s = command(sub, "show", cmd_projects_show, "show a project, its Containerfile and builds")
    s.add_argument("project", help="project id or name")
    s.add_argument("--brief", action="store_true", help="without the Containerfile")
    s = command(sub, "create", cmd_projects_create, "create a project from a spec file and/or flags")
    s.add_argument("--name", help="project name (lowercase, digits, dashes)")
    definition_options(s)
    s.add_argument("--build", action="store_true", help="start a build right away")
    wait_options(s)
    s = command(sub, "import", cmd_projects_import, "import a Dockerfile/Containerfile (Advanced mode)")
    s.add_argument("--name", required=True)
    s.add_argument("--containerfile", required=True, metavar="FILE", help="local Dockerfile or Containerfile")
    s.add_argument("--description")
    s = command(sub, "update", cmd_projects_update, "change a project's definition (existing builds are kept)")
    s.add_argument("project", help="project id or name")
    s.add_argument("--containerfile", metavar="FILE", help="new Containerfile (Advanced-mode projects)")
    definition_options(s)
    s = command(sub, "clone", cmd_projects_clone, "copy a project under a new name")
    s.add_argument("project")
    s.add_argument("--name", required=True)
    s = command(sub, "delete", cmd_projects_delete, "delete a project (built images and archives are kept)")
    s.add_argument("project")
    s.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    s = command(sub, "containerfile", cmd_projects_containerfile, "print the project's Containerfile")
    s.add_argument("project")
    s = command(sub, "export-spec", cmd_projects_export_spec, "write the project as a spec file")
    s.add_argument("project")
    s.add_argument("--output", "-o", metavar="FILE", help="file to write (default: stdout)")
    s.add_argument("--force", action="store_true")
    s = command(sub, "preview", cmd_preview, "generate the Containerfile for a definition without saving it")
    s.add_argument("--name")
    definition_options(s)

    s = command(commands, "upload", cmd_upload, "upload local files to the server (content addressed)")
    s.add_argument("files", nargs="+", metavar="FILE")

    s = command(commands, "build", cmd_build, "start a build of a project")
    s.add_argument("project", help="project id or name")
    wait_options(s)

    sub = group("builds", "builds, logs and history")
    s = command(sub, "list", cmd_builds_list, "recent builds")
    s.add_argument("--project", help="only this project")
    s.add_argument("--status", help="only builds with this status")
    s.add_argument("--limit", type=int, default=50)
    s = command(sub, "show", cmd_builds_show, "build details, checks and artifacts")
    s.add_argument("build", help="build id or number (#12)")
    s.add_argument("--full", action="store_true", help="with --json: include log and Containerfile")
    s = command(sub, "logs", cmd_builds_logs, "print a build log; --follow streams it")
    s.add_argument("build")
    s.add_argument("--follow", "-f", action="store_true", help="keep streaming until the build ends")
    s.add_argument("--tail", type=int, metavar="LINES", help="start near the end (approximate)")
    s = command(sub, "wait", cmd_builds_wait, "wait for a running build (reconnect after a lost session)")
    s.add_argument("build")
    s.add_argument("--logs", action="store_true", help="stream the log to stderr while waiting")
    s = command(sub, "containerfile", cmd_builds_containerfile, "the exact Containerfile the build used")
    s.add_argument("build")
    s = command(sub, "manifest", cmd_builds_manifest, "the build manifest (JSON)")
    s.add_argument("build")

    s = command(commands, "export", cmd_export, "download the image archive to this machine")
    s.add_argument("build")
    output_options(s, "file")
    sub = group("bundle", "air-gap bundles")
    s = command(sub, "create", cmd_bundle_create, "create (or recreate) the air-gap bundle on the server")
    s.add_argument("build")
    s = command(sub, "export", cmd_bundle_export, "download the air-gap bundle (created first if missing)")
    s.add_argument("build")
    output_options(s, "file")
    s.add_argument("--no-create", action="store_true", help="fail instead of creating a missing bundle")

    command(commands, "scanner", cmd_scanner, "scanner status on the server")
    s = command(commands, "scan", cmd_scan, "scan (or rescan) a build's image")
    s.add_argument("build")
    s.add_argument("--kind", action="append", help="vulnerability, secret, misconfiguration (repeatable)")
    s.add_argument("--wait", action="store_true", help="wait for the scan to finish")
    sub = group("scans", "scan history, findings and SBOMs")
    s = command(sub, "list", cmd_scans_list, "scan history and security status of a build")
    s.add_argument("build")
    s = command(sub, "show", cmd_scans_show, "one scan with its findings")
    s.add_argument("scan", help="scan id")
    s.add_argument("--kind", help="vulnerability, secret or misconfiguration")
    s.add_argument("--severity", choices=list(SEVERITIES))
    s.add_argument("--limit", type=int, help="most findings to return (server default 500, max 5000)")
    s = command(sub, "sbom", cmd_scans_sbom, "download a scan's SBOM to this machine")
    s.add_argument("scan")
    output_options(s, "file")

    sub = group("training", "LLM training & fine-tuning templates")
    command(sub, "profiles", cmd_training_profiles, "list the training profiles the server offers")
    s = command(sub, "profile", cmd_training_profile, "one profile: tools, locked versions, limitations")
    s.add_argument("profile")
    s = command(sub, "catalog", cmd_training_catalog, "concepts, intents, targets, stacks, add-ons, tools")
    s.add_argument("section", nargs="?",
                   choices=["concepts", "intents", "targets", "stacks", "addons", "tools", "directories"])

    def training_options(sub):
        sub.add_argument("--profile", required=True)
        sub.add_argument("--target")
        sub.add_argument("--stack")
        sub.add_argument("--addon", action="append", help="add-on (repeatable; default: the profile's defaults)")
        sub.add_argument("--no-addons", action="store_true", help="none of the profile's add-ons")
        sub.add_argument("--intent", action="append", metavar="KEY=VALUE", help="e.g. task=sft method=lora")
        sub.add_argument("--extra-python", action="append", metavar="PACKAGE")

    s = command(sub, "resolve", cmd_training_resolve, "check a combination and show the resulting recipe")
    training_options(s)
    s.add_argument("--arch")
    s = command(sub, "create", cmd_training_create, "create a training image project")
    s.add_argument("--name", required=True)
    s.add_argument("--description")
    s.add_argument("--repository")
    training_options(s)
    s.add_argument("--extra-package", action="append", metavar="PACKAGE")
    s.add_argument("--build", action="store_true", help="start a build right away")
    wait_options(s)
    s = command(sub, "checks", cmd_training_checks, "check results of a training build")
    s.add_argument("build")
    s = command(sub, "guide", cmd_training_guide, "Getting started for a training build")
    s.add_argument("build")
    s = command(sub, "gpu-report", cmd_training_gpu_report,
                "record the output of `layersmith-check gpu --json` run on the target machine")
    s.add_argument("build")
    s.add_argument("--file", required=True, metavar="FILE", help="report file, or - for stdin")

    sub = group("settings", "server settings")
    command(sub, "show", cmd_settings_show, "server version, build backend, storage paths, limits")
    storage = sub.add_parser("storage", help="server storage paths")
    storage_sub = storage.add_subparsers(dest="storage_command", metavar="SUBCOMMAND")
    s = command(storage_sub, "set", cmd_settings_storage_set, "change where the server writes new data")
    s.add_argument("field", choices=["image_dir", "build_dir", "upload_dir", "log_dir", "tmp_dir", "scan_dir"])
    s.add_argument("path", help="a path ON THE SERVER")
    s.add_argument("--yes", "-y", action="store_true")
    storage.set_defaults(handler=None, group_parser=storage)
    return parser


def interrupted_message() -> str:
    if _waiting_on.get("kind") == "build":
        return (f"Stopped waiting. The build continues on the server; reconnect with:\n"
                f"  layersmith builds wait {_waiting_on['id']} --logs")
    if _waiting_on.get("kind") == "scan":
        return (f"Stopped waiting. The scan continues on the server; check it with:\n"
                f"  layersmith scans list {_waiting_on['build']}")
    if _waiting_on.get("kind") == "bundle":
        return ("Stopped waiting. The server may still be creating the bundle; check with:\n"
                f"  layersmith builds show {_waiting_on['id']}")
    return "Interrupted."


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)
    out = Output(args.json)

    if args.command is None:
        if sys.stdin.isatty() and sys.stdout.isatty() and not args.json:
            args.handler = cmd_tui
        else:
            sys.stderr.write("No terminal for the interactive interface; use a command. "
                             "For example: layersmith projects list\n\n")
            parser.print_help(sys.stderr)
            return EXIT_USAGE
    if getattr(args, "handler", None) is None:
        args.group_parser.print_help(sys.stderr)
        return EXIT_USAGE
    try:
        code = args.handler(args, out)
        return EXIT_OK if code is None else code
    except KeyboardInterrupt:
        sys.stderr.write("\n" + interrupted_message() + "\n")
        return EXIT_INTERRUPTED
    except ClientError as exc:
        if out.json:
            sys.stdout.write(json.dumps({"error": str(exc), "hint": exc.hint, "status": exc.status,
                                         "exit_code": exc.exit_code}) + "\n")
        sys.stderr.write(f"error: {exc}\n")
        if exc.hint:
            sys.stderr.write(f"hint: {exc.hint}\n")
        return exc.exit_code
    except BrokenPipeError:
        # `layersmith builds logs X | head` closes the pipe early; not an error.
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
