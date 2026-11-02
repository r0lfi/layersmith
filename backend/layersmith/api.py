"""HTTP API. The web UI is one client of it; nothing is UI-only.

Routes are deliberately thin: they validate input through core.spec, then
call the build service. Anything that touches the filesystem resolves the
path against the configured directory and refuses to leave it.
"""

import asyncio
import hashlib
import json
import re
import uuid
import queue
import shutil
import threading
from pathlib import Path

from fastapi import (
    APIRouter, Depends, FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect,
)
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from layersmith import config
from layersmith.backends.base import BuildError
from layersmith.backends import make_backend, probe_all
from layersmith.core import catalog
from layersmith.core import containerfile as cf
from layersmith.core.spec import (
    InvalidSpec, NAME_RE, REPOSITORY_RE, VERSION_RE, check, image_ref, next_patch, parse_reference,
    validate_spec, version_key,
)
from layersmith.models import (
    ACTIVE_STATES, Blob, Build, LlmModel, Project, Scan, ScanFinding, create_all, make_engine, make_session_factory,
    next_build_number, utcnow,
)
from layersmith import scanners
from layersmith.services import llm_models, splitting, storage
from layersmith.llm import catalog as llm_catalog
from layersmith.llm import scripts as llm_scripts
from layersmith.services.builds import BuildService, manifest_for, sha256_file
from layersmith.services.scanning import ScanError, ScanService, finding_row, summarise, sweep_temporary_exports
from layersmith.services.builds import training_recipe
from layersmith.services.builds import gpu_benchmark_recipe
from layersmith.training import catalog as training_catalog
from layersmith.training import checks as training_checks
from layersmith.training import docs as training_docs
from layersmith.training import recipes as training_recipes
from layersmith.gpu_benchmark import catalog as gpu_benchmark_catalog
from layersmith.gpu_benchmark import render as gpu_benchmark_render
from layersmith.aiml import catalog as aiml_catalog
from layersmith.aiml import checks as aiml_checks
from layersmith.aiml import compat as aiml_compat
from layersmith.aiml import export as aiml_export
from layersmith.aiml import lint as aiml_lint
from layersmith.aiml import recipes as aiml_recipes
from layersmith.aiml import render as aiml_render
from layersmith.aiml import slurm as aiml_slurm
from layersmith.aiml import upstream as aiml_upstream
from layersmith.services.builds import aiml_recipe
from layersmith.openshift import catalog as openshift_catalog
from layersmith.openshift import checks as openshift_checks
from layersmith.openshift import docs as openshift_docs
from layersmith.openshift import host as openshift_host
from layersmith.openshift import recipes as openshift_recipes
from layersmith.services.builds import openshift_recipe
from layersmith.opencode import catalog as opencode_catalog
from layersmith.opencode import checks as opencode_checks
from layersmith.opencode import docs as opencode_docs
from layersmith.opencode import recipes as opencode_recipes
from layersmith.opencode import render as opencode_render
from layersmith.services.builds import opencode_recipe

router = APIRouter(prefix="/api")

#: Bounds on how much of a build log is handed to a client at once.
LOG_REPLY_CHARS = 500_000
LOG_REPLAY_LINES = 5_000
#: Default and largest slice of a log returned by one incremental read.
LOG_CHUNK_BYTES = 256 * 1024
LOG_CHUNK_MAX_BYTES = 1024 * 1024

#: Optional API additions a client can check for instead of guessing from the
#: version. Additive only: a client must keep working without any of them.
API_FEATURES = ["log-offset", "limits", "split-archive", "delete-build", "llm-models", "gpu-kit", "aiml", "openshift",
                "opencode"]


# ------------------------------------------------------------- schemas

class ProjectIn(BaseModel):
    name: str
    description: str = ""
    repository: str | None = None
    template: str = "Custom"
    mode: str = "gui"
    spec: dict = Field(default_factory=dict)
    containerfile: str = ""


class BuildIn(BaseModel):
    version: str | None = None
    reason: str = "build"


class ImportIn(BaseModel):
    name: str
    description: str = ""
    containerfile: str


class CloneIn(BaseModel):
    name: str


class ScanIn(BaseModel):
    kinds: list[str] | None = None


class StorageIn(BaseModel):
    paths: dict[str, str]


class TrainingResolveIn(BaseModel):
    training: dict = Field(default_factory=dict)
    architecture: str | None = None


class GpuBenchmarkResolveIn(BaseModel):
    gpu_benchmark: dict = Field(default_factory=dict)


class AimlResolveIn(BaseModel):
    aiml: dict = Field(default_factory=dict)
    driver: str | None = None
    gpus: list[str] | None = None


class OpenShiftResolveIn(BaseModel):
    openshift: dict = Field(default_factory=dict)


class OpencodeResolveIn(BaseModel):
    opencode: dict = Field(default_factory=dict)


class AimlLintIn(BaseModel):
    containerfile: str


class AimlSlurmIn(BaseModel):
    params: dict = Field(default_factory=dict)
    image: str | None = None


class AimlReportIn(BaseModel):
    #: The output of the GPU test or of a Slurm job, or its result JSON.
    report: dict | str


class AimlExportIn(BaseModel):
    format: str = "docker-archive"


class ModelIn(BaseModel):
    repo_id: str
    revision: str = "main"
    catalog_key: str | None = None


class GpuReportIn(BaseModel):
    #: The JSON `layersmith-check gpu --json` printed, or its whole output.
    report: dict | str


# ------------------------------------------------------------- helpers

def get_state(request: Request) -> dict:
    """Shared session factory and build service, set up in create_app()."""
    return request.app.state.layersmith


def _project_json(project: Project, builds: list[Build] | None = None) -> dict:
    data = {
        "id": project.id, "name": project.name, "description": project.description,
        "repository": project.repository, "template": project.template, "mode": project.mode,
        "spec": project.spec, "containerfile": project.containerfile, "next_version": project.next_version,
        "base_image": project.base_image, "base_digest": project.base_digest,
        "base_upstream_digest": project.base_upstream_digest,
        "base_update_available": project.base_update_available,
        "created_at": project.created_at, "updated_at": project.updated_at,
    }
    if builds is not None:
        data["builds"] = [_build_json(b) for b in builds]
    return data


def _build_json(build: Build, project: Project | None = None) -> dict:
    return {
        "id": build.id, "number": build.number, "project_id": build.project_id,
        # A deleted project's row keeps its builds but gave up its name
        # (name~<id>, see _free_name); show the name it had.
        "project_name": (project.name.split("~", 1)[0] if project else None),
        "project_deleted": bool(project and project.deleted_at),
        "version": build.version, "status": build.status, "reason": build.reason, "error": build.error,
        "mode": build.mode, "architecture": build.architecture,
        "base_image": build.base_image, "base_digest": build.base_digest,
        "packages": build.packages or [], "image_ref": build.image_ref, "image_id": build.image_id,
        "image_digest": build.image_digest, "image_size": build.image_size,
        "export_size": build.export_size, "export_sha256": build.export_sha256,
        # Whether the file is actually there, not just recorded: an archive
        # removed from disk would otherwise be offered as a download that
        # answers 404.
        "has_export": _downloadable(build.export_path), "has_airgap": _downloadable(build.airgap_path),
        "export_name": Path(build.export_path).name if build.export_path else None,
        "airgap_name": Path(build.airgap_path).name if build.airgap_path else None,
        "airgap_size": build.airgap_size, "airgap_sha256": build.airgap_sha256,
        "started_at": build.started_at, "finished_at": build.finished_at, "created_at": build.created_at,
        "duration_seconds": build.duration_seconds,
        "training_profile": ((build.spec or {}).get("training") or {}).get("profile"),
        "aiml_template": ((build.spec or {}).get("aiml") or {}).get("template"),
        "openshift_ocp": ((build.spec or {}).get("openshift") or {}).get("ocp_version"),
        "opencode_version": ((build.spec or {}).get("opencode") or {}).get("opencode_version"),
    }


def _training_detail(build: Build, scanned: bool = False) -> dict | None:
    """Recipe, check statuses, Getting started and export contents of a training build."""
    recipe = training_recipe(build.spec)
    if recipe is None:
        return None
    customized = training_recipes.is_customized(build.spec or {})
    image = build.image_ref or "IMAGE"
    archive = Path(build.export_path).name if build.export_path else None
    return {
        "recipe": recipe, "customized": customized,
        "checks": training_checks.summarise(build.status, recipe, build.checks, build.image_ref, customized),
        "getting_started": training_docs.getting_started(recipe, image, archive),
        "export_contents": training_docs.export_contents(recipe, scanned=scanned),
        "before_build": training_docs.before_build(recipe),
    }


def _gpu_benchmark_detail(build: Build, backend: str = "podman") -> dict | None:
    recipe = gpu_benchmark_recipe(build.spec)
    if recipe is None:
        return None
    image = build.image_ref or "IMAGE"
    started = gpu_benchmark_render.getting_started(recipe, image)
    archive = Path(build.export_path).name if build.export_path else started["archive"]
    started["archive"] = archive
    started["load"] = f"podman load -i {archive}\npodman images | grep gpu-benchmark"
    started["verify"] = f"sha256sum -c {archive}.sha256"
    return {"recipe": recipe, "getting_started": started,
            "readme": gpu_benchmark_render.markdown(recipe, image),
            "export": {"archive": archive, "format": "OCI archive (podman save --format oci-archive)"
                       if backend == "podman" else "Docker archive with OCI layout (docker save); podman load reads it",
                       "built_with": backend, "size": build.export_size, "sha256": build.export_sha256,
                       "available": _downloadable(build.export_path),
                       "status": ("exported" if _downloadable(build.export_path) else
                                  "exporting" if build.status == "exporting" else
                                  "missing" if build.status == "ready" else build.status)}}



def _aiml_detail(build: Build, backend: str = "podman") -> dict | None:
    """Recipe, metadata read from the image, tests, compatibility and exports of an AI/ML build."""
    try:
        recipe = aiml_recipe(build.spec)
    except aiml_catalog.AimlError as exc:  # a saved recipe this version no longer offers
        return {"error": str(exc)}
    if recipe is None:
        return None
    image = build.image_ref or "IMAGE"
    results = (build.checks or {}).get("results") or {}
    basic = (results.get("basic") or {}).get("info") or {}
    reported = results.get("slurm") or results.get("gpu")
    archive = Path(build.export_path).name if build.export_path else _aiml_archive_name(build)
    exports = {"primary": {"format": "OCI archive (podman save --format oci-archive)" if backend == "podman"
                           else "Docker archive (docker save)",
                           "archive": archive, "size": build.export_size, "sha256": build.export_sha256,
                           "available": _downloadable(build.export_path)}}
    for fmt, entry in ((build.aiml or {}).get("exports") or {}).items():
        exports[fmt] = {**{k: v for k, v in entry.items() if k != "path"},
                        "archive": Path(entry.get("path", "")).name,
                        "available": entry.get("status") == "ready" and _downloadable(entry.get("path"))}
    metadata = {
        "base": recipe["base"]["display"], "base_digest": build.base_digest or recipe["base"]["digest"],
        "base_verified": recipe["base"]["verified"],
        "cuda": (basic.get("env") or {}).get("CUDA_VERSION") or recipe["cuda"],
        "framework": recipe["framework"],
        "torch": basic.get("torch"), "torch_cuda": basic.get("torch_cuda"), "tensorflow": basic.get("tensorflow"),
        "python": basic.get("python") or recipe["python"], "os": basic.get("os") or recipe["os"],
        "cudnn": (basic.get("env") or {}).get("CUDNN_VERSION") or basic.get("cudnn"),
        "nccl": (basic.get("env") or {}).get("NCCL_VERSION"),
        "compiled_archs": basic.get("compiled_archs"),
        "image_size": build.image_size, "image_digest": build.image_digest,
        "package_count": len(basic.get("packages") or {}),
        "requested": recipe["install"], "status": build.status,
    }
    try:
        compatibility = aiml_compat.check(recipe, basic=basic, reported=reported)
    except aiml_catalog.AimlError:
        compatibility = []
    return {
        "recipe": recipe, "metadata": metadata, "compatibility": compatibility,
        "tests": aiml_checks.summarise(build.status, build.checks),
        "exports": exports,
        "offline": {"verify": f"sha256sum -c {archive}.sha256", "load": f"podman load -i {archive}",
                    "list": "podman images", "run": aiml_render.run_command(recipe, image),
                    "gpu_test": aiml_render.gpu_test_command(recipe, image)},
        "slurm_defaults": {**aiml_slurm.DEFAULTS, "job_name": f"{recipe['template']}-gpu"[:63]},
        "target": aiml_catalog.TARGET,
    }


def _openshift_detail(build: Build, backend: str = "podman") -> dict | None:
    """Recipe, check results, installed versions, commands and downloads of an OpenShift Admin Toolkit build."""
    try:
        recipe = openshift_recipe(build.spec)
    except openshift_catalog.OpenShiftError as exc:  # a saved request this version no longer offers
        return {"error": str(exc)}
    if recipe is None:
        return None
    image = build.image_ref or "IMAGE"
    archive = Path(build.export_path).name if build.export_path else _aiml_archive_name(build)
    tests = openshift_checks.summarise(build.status, build.checks)
    base = f"/api/builds/{build.id}"
    downloads = []
    if build.status == "ready" and _downloadable(build.export_path):
        downloads = [
            {"name": archive, "kind": "part", "size": build.export_size, "sha256": build.export_sha256,
             "url": f"{base}/download/export"},
            {"name": f"{archive}.sha256", "kind": "checksum", "url": f"{base}/download/export.sha256"},
            {"name": openshift_host.INSTALLER, "kind": "script",
             "url": f"{base}/openshift/files/{openshift_host.INSTALLER}"},
            {"name": f"{openshift_docs.CONTAINER}.ini", "kind": "script",
             "url": f"{base}/openshift/files/{openshift_docs.CONTAINER}.ini"},
            {"name": "README.md", "kind": "script", "url": f"{base}/openshift/files/README.md"},
        ]
    return {
        "recipe": recipe,
        "tests": tests["rows"],
        "tools_installed": tests["tools"],
        "archive": archive,
        "archive_format": ("OCI archive (podman save --format oci-archive)" if backend == "podman"
                           else "Docker archive (docker save)"),
        "commands": openshift_docs.commands(recipe, image, archive),
        "distrobox_ini": openshift_docs.distrobox_ini(recipe, image),
        "downloads": downloads,
    }


def _opencode_detail(build: Build, backend: str = "podman") -> dict | None:
    """Recipe, check results, installed versions, host commands and downloads of an opencode build."""
    try:
        recipe = opencode_recipe(build.spec)
    except opencode_catalog.OpencodeError as exc:  # a saved request this version no longer offers
        return {"error": str(exc)}
    if recipe is None:
        return None
    image = build.image_ref or "IMAGE"
    archive = Path(build.export_path).name if build.export_path else _aiml_archive_name(build)
    tests = opencode_checks.summarise(build.status, build.checks)
    base = f"/api/builds/{build.id}"
    downloads = []
    if build.status == "ready" and _downloadable(build.export_path):
        downloads = [
            {"name": archive, "kind": "part", "size": build.export_size, "sha256": build.export_sha256,
             "url": f"{base}/download/export"},
            {"name": f"{archive}.sha256", "kind": "checksum", "url": f"{base}/download/export.sha256"},
            {"name": opencode_docs.INSTALLER, "kind": "script", "url": f"{base}/opencode/files/{opencode_docs.INSTALLER}"},
            {"name": "README.md", "kind": "script", "url": f"{base}/opencode/files/README.md"},
        ]
    return {
        "recipe": recipe,
        "tests": tests["rows"],
        "tools_installed": tests["tools"],
        "archive": archive,
        "archive_format": ("OCI archive (podman save --format oci-archive)" if backend == "podman"
                           else "Docker archive (docker save)"),
        "commands": opencode_docs.commands(recipe, image, archive),
        "site_config": (opencode_render.ASSET_DIR / "etc" / "opencode.json").read_text(encoding="utf-8"),
        "downloads": downloads,
    }


OPENCODE_FILES = {"README.md": "text/markdown", opencode_docs.INSTALLER: "text/x-shellscript",
                  "opencode.json": "application/json", "versions.json": "application/json",
                  "checks.json": "application/json"}

OPENSHIFT_FILES = {"README.md": "text/markdown", f"{openshift_docs.CONTAINER}.ini": "text/plain",
                   openshift_host.INSTALLER: "text/x-shellscript",
                   "versions.json": "application/json", "checks.json": "application/json"}


def _aiml_archive_name(build: Build) -> str:
    from layersmith.core.spec import export_filename

    return export_filename((build.image_ref or "image:1.0.0").rsplit(":", 1)[0], build.version)


def _downloadable(path_value: str | None) -> bool:
    if not path_value:
        return False
    try:
        return Path(path_value).is_file()
    except OSError:
        return False


def _free_name(session, name: str) -> None:
    """Give up a deleted project's claim on a name.

    Deleting keeps the row so its builds stay listed, but projects.name is
    unique in the database: without this, re-creating a deleted project's
    name failed on the constraint with a 500. The deleted row is renamed to
    name~<id>, which no new project can be called (NAME_RE has no "~").
    """
    for project in session.scalars(select(Project).where(Project.name == name, Project.deleted_at.isnot(None))):
        project.name = f"{name[:54]}~{project.id[:8]}"
    session.flush()


def _load_project(session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None or project.deleted_at:
        raise HTTPException(404, "Project not found")
    return project


def _prepare_spec(payload_spec: dict, template: str) -> dict:
    """Merge a template's fragment under the user's own choices, then validate."""
    fragment = catalog.TEMPLATES_BY_NAME.get(template, {}).get("spec", {})
    merged = {**fragment, **payload_spec}
    for key in ("presets", "packages", "tests"):
        if key in fragment and key in payload_spec:
            merged[key] = list(dict.fromkeys([*fragment[key], *payload_spec[key]]))
    return validate_spec(merged)


def _lint_ai_containerfile(payload: "ProjectIn") -> None:
    """A hand-written Containerfile of an AI/ML project must not carry credentials or disable isolation."""
    if payload.template in aiml_catalog.TEMPLATE_NAMES.values():
        errors = aiml_lint.lint(payload.containerfile)["errors"]
        if errors:
            raise InvalidSpec("; ".join(errors[:5]))


def _safe_download(path_value: str | None, allowed_dirs: list[Path]) -> Path:
    """Serve a recorded archive, and only from a directory LayerSmith writes to.

    More than one directory is allowed because the image path is editable: an
    archive written before a change still lives in the previous directory.
    """
    if not path_value:
        raise HTTPException(404, "Nothing to download")
    path = Path(path_value).resolve()
    if not path.is_file() or not any(path.is_relative_to(Path(d).resolve()) for d in allowed_dirs):
        raise HTTPException(404, "File is no longer available")
    return path


# --------------------------------------------------------------- routes

@router.get("/health")
def health():
    """Liveness for the container healthcheck and for upgrade checks."""
    return {"status": "ok", "app": config.APP_NAME, "version": config.VERSION,
            "revision": config.REVISION, "api_features": API_FEATURES}


@router.get("/catalog")
def get_catalog():
    """Everything the wizard needs to render its choices."""
    return {
        "distributions": [
            {"name": name, "family": entry["family"], "el": bool(entry.get("el")),
             "versions": list(entry["versions"]), "sources": entry["versions"]}
            for name, entry in catalog.DISTROS.items()
        ],
        "families": catalog.FAMILIES,
        "architectures": list(catalog.ARCHITECTURES),
        "templates": catalog.TEMPLATES,
        "presets": catalog.PRESETS,
        "categories": catalog.CATEGORIES,
        "tools": catalog.TOOL_CATALOG,
    }


@router.get("/settings")
def get_settings(state=Depends(get_state)):
    app_settings = config.settings()
    usage = shutil.disk_usage(app_settings.data_dir)
    with state["session_factory"]() as session:
        path_rows = storage.describe(session, app_settings)
    backend = make_backend(app_settings)
    available, detail = backend.available()
    return {
        "app_name": config.APP_NAME, "version": config.VERSION, "revision": config.REVISION,
        "tagline": config.TAGLINE, "source_url": config.SOURCE_URL, "license": config.LICENSE,
        "default_architecture": app_settings.default_architecture,
        "default_namespace": app_settings.default_namespace,
        "build_backend": {"name": backend.name, "selection": app_settings.build_backend,
                          "available": available, "detail": detail,
                          "archive_format": backend.archive_format,
                          "runtimes": probe_all(app_settings)},
        "scanner": scanners.describe(app_settings),
        "paths": path_rows,
        "storage": {"total": usage.total, "used": usage.used, "free": usage.free},
        # What the server refuses, so a remote client can say so before it
        # uploads half a gigabyte. The server still enforces every one.
        "limits": {"max_upload_bytes": app_settings.max_upload_bytes,
                   "max_files_per_image": app_settings.max_files_per_image,
                   "max_context_bytes": app_settings.max_context_bytes},
    }


@router.get("/scanner")
def scanner_status(state=Depends(get_state)):
    """Scanner status, including what it would actually be asked to look for."""
    app_settings = config.settings()
    status = scanners.describe(app_settings)
    scanner = scanners.make_scanner(app_settings)
    status["health"] = scanner.health()
    status["scan_kinds"] = [kind for kind in app_settings.scan_kinds if kind in scanner.supported_kinds]
    status["generate_sbom"] = app_settings.generate_sbom and scanner.supports_sbom
    return status


@router.put("/settings/storage")
def update_storage(payload: StorageIn, state=Depends(get_state)):
    """Change where new data is written. Existing files are left where they are."""
    app_settings = config.settings()
    with state["session_factory"]() as session:
        try:
            storage.save(session, app_settings, payload.paths)
        except storage.StorageError as exc:
            raise HTTPException(422, str(exc))
        except RuntimeError as exc:  # prepare() refused a directory
            raise HTTPException(422, str(exc))
        return {"paths": storage.describe(session, app_settings)}


@router.post("/builds/{build_id}/scan", status_code=202)
def start_scan(build_id: str, payload: ScanIn | None = None, state=Depends(get_state)):
    """Scan, or rescan, the image this build produced."""
    service = state["scan_service"]
    with state["session_factory"]() as session:
        if session.get(Build, build_id) is None:
            raise HTTPException(404, "Build not found")
    try:
        # A rescan is just a later scan; the history keeps them apart by time.
        scan_id = service.create(build_id, reason="manual", kinds=(payload.kinds if payload else None))
    except ScanError as exc:
        raise HTTPException(422, str(exc))
    service.enqueue(scan_id)
    with state["session_factory"]() as session:
        return summarise(session.get(Scan, scan_id))


@router.get("/builds/{build_id}/scans")
def build_scans(build_id: str, state=Depends(get_state)):
    """Every scan of this build, newest first: the image's scan history."""
    with state["session_factory"]() as session:
        scans = session.scalars(
            select(Scan).where(Scan.build_id == build_id).order_by(Scan.created_at.desc())
        ).all()
        return [summarise(scan) for scan in scans]


#: A base image can carry thousands of findings, which is a table no one
#: reads and a page that struggles to render. The worst are returned first
#: and the response says how many were left out.
FINDING_LIMIT = 500


@router.get("/scans/{scan_id}")
def get_scan(scan_id: str, kind: str | None = None, severity: str | None = None,
             limit: int = FINDING_LIMIT, state=Depends(get_state)):
    limit = max(1, min(limit, 5000))
    with state["session_factory"]() as session:
        scan = session.get(Scan, scan_id)
        if scan is None:
            raise HTTPException(404, "Scan not found")
        query = select(ScanFinding).where(ScanFinding.scan_id == scan_id)
        if kind:
            query = query.where(ScanFinding.kind == kind)
        if severity:
            query = query.where(ScanFinding.severity == severity.lower())
        findings = list(session.scalars(query).all())
        order = {level: index for index, level in enumerate(scanners.SEVERITIES)}
        findings.sort(key=lambda row: (order.get(row.severity, 99), row.package_name or "", row.identifier))
        return {**summarise(scan), "finding_total": len(findings), "finding_limit": limit,
                "findings": [finding_row(row) for row in findings[:limit]]}


@router.get("/scans/{scan_id}/sbom")
def get_sbom(scan_id: str, state=Depends(get_state)):
    """The SBOM as a file, in the format the scanner produced."""
    with state["session_factory"]() as session:
        scan = session.get(Scan, scan_id)
        if scan is None or not scan.sbom_path:
            raise HTTPException(404, "This scan has no SBOM")
        path = _safe_download(scan.sbom_path, [Path(config.settings().scan_dir)])
        return FileResponse(path, media_type="application/json",
                            filename=f"sbom-{scan.id}.{scan.sbom_format or 'json'}.json")


@router.get("/stats")
def stats(state=Depends(get_state)):
    with state["session_factory"]() as session:
        projects = session.scalar(select(func.count()).select_from(Project).where(Project.deleted_at.is_(None)))
        builds = session.scalar(select(func.count()).select_from(Build))
        images = session.scalar(select(func.count()).select_from(Build).where(Build.status == "ready"))
        stored = session.scalar(select(func.sum(Build.export_size)).where(Build.export_size.isnot(None))) or 0
        recent = session.scalars(select(Build).order_by(Build.number.desc()).limit(5)).all()
        owners = {p.id: p for p in session.scalars(select(Project)).all()}
        return {
            "projects": projects, "builds": builds, "images": images, "storage_bytes": int(stored),
            "recent": [_build_json(b, owners.get(b.project_id)) for b in recent],
        }


@router.get("/projects")
def list_projects(state=Depends(get_state)):
    with state["session_factory"]() as session:
        projects = session.scalars(
            select(Project).where(Project.deleted_at.is_(None)).order_by(Project.name)).all()
        counts = dict(session.execute(select(Build.project_id, func.count()).group_by(Build.project_id)).all())
        return [{**_project_json(p), "build_count": counts.get(p.id, 0)} for p in projects]


@router.post("/projects", status_code=201)
def create_project(payload: ProjectIn, state=Depends(get_state)):
    try:
        name = check(NAME_RE, payload.name, "project name")
        repository = check(REPOSITORY_RE,
                           payload.repository or f"{config.settings().default_namespace}/{name}", "repository")
        if payload.mode == "gui":
            spec = _prepare_spec(payload.spec, payload.template)
            containerfile_text = ""
            base_image = spec["base"]["source"]
        else:
            _lint_ai_containerfile(payload)
            bases = cf.bases(payload.containerfile)
            if not bases:
                raise InvalidSpec("The Containerfile has no FROM line to build from")
            spec, containerfile_text = {}, payload.containerfile
            base_image = parse_reference(bases[0])["source"]
    except InvalidSpec as exc:
        raise HTTPException(422, str(exc))

    with state["session_factory"]() as session:
        if session.scalar(select(Project).where(Project.name == name, Project.deleted_at.is_(None))):
            raise HTTPException(409, f"A project named {name} already exists")
        _free_name(session, name)
        project = Project(name=name, description=payload.description[:2000], repository=repository,
                          template=payload.template, mode=payload.mode, spec=spec,
                          containerfile=containerfile_text, base_image=base_image)
        session.add(project)
        session.commit()
        return _project_json(project)


@router.post("/projects/import", status_code=201)
def import_containerfile(payload: ImportIn, state=Depends(get_state)):
    """Advanced mode from a pasted or uploaded Dockerfile/Containerfile."""
    return create_project(ProjectIn(name=payload.name, description=payload.description, mode="advanced",
                                    template="Custom", containerfile=payload.containerfile), state)


@router.get("/projects/{project_id}")
def get_project(project_id: str, state=Depends(get_state)):
    with state["session_factory"]() as session:
        project = _load_project(session, project_id)
        builds = session.scalars(
            select(Build).where(Build.project_id == project_id).order_by(Build.number.desc())).all()
        data = _project_json(project, builds)
        if project.mode == "gui" and project.spec:
            try:
                text, packages, warnings = cf.generate({"name": project.name, "description": project.description},
                                                       project.spec, base_digest=project.base_digest)
            except InvalidSpec as exc:  # a saved recipe this version no longer offers
                text, packages, warnings = "", [], [str(exc)]
            data["containerfile"] = text
            data["packages"] = packages
            data["warnings"] = warnings
            if project.spec.get("aiml"):
                try:
                    data["aiml"] = aiml_recipes.resolve(project.spec["aiml"])
                except aiml_catalog.AimlError:
                    data["aiml"] = None
            if project.spec.get("openshift"):
                try:
                    data["openshift"] = openshift_recipes.resolve(project.spec["openshift"])
                except openshift_catalog.OpenShiftError:
                    data["openshift"] = None
            if project.spec.get("opencode"):
                try:
                    data["opencode"] = opencode_recipes.resolve(project.spec["opencode"])
                except opencode_catalog.OpencodeError:
                    data["opencode"] = None
            if project.spec.get("training"):
                try:
                    data["training"] = training_recipes.resolve(project.spec["training"],
                                                                project.spec.get("architecture"))
                except InvalidSpec:
                    data["training"] = None
        return data


@router.put("/projects/{project_id}")
def update_project(project_id: str, payload: ProjectIn, state=Depends(get_state)):
    with state["session_factory"]() as session:
        project = _load_project(session, project_id)
        try:
            project.description = payload.description[:2000]
            project.mode = payload.mode
            if payload.mode == "gui":
                project.spec = _prepare_spec(payload.spec, payload.template)
                project.base_image = project.spec["base"]["source"]
            else:
                _lint_ai_containerfile(payload)
                bases = cf.bases(payload.containerfile)
                if not bases:
                    raise InvalidSpec("The Containerfile has no FROM line to build from")
                project.containerfile = payload.containerfile
                project.base_image = parse_reference(bases[0])["source"]
            if payload.repository:
                project.repository = check(REPOSITORY_RE, payload.repository, "repository")
        except InvalidSpec as exc:
            raise HTTPException(422, str(exc))
        session.commit()
        return _project_json(project)


@router.post("/projects/{project_id}/clone", status_code=201)
def clone_project(project_id: str, payload: CloneIn, state=Depends(get_state)):
    with state["session_factory"]() as session:
        source = _load_project(session, project_id)
        try:
            name = check(NAME_RE, payload.name, "project name")
        except InvalidSpec as exc:
            raise HTTPException(422, str(exc))
        if session.scalar(select(Project).where(Project.name == name, Project.deleted_at.is_(None))):
            raise HTTPException(409, f"A project named {name} already exists")
        _free_name(session, name)
        clone = Project(name=name, description=source.description, template=source.template, mode=source.mode,
                        spec=dict(source.spec or {}), containerfile=source.containerfile,
                        repository=f"{source.repository.rsplit('/', 1)[0]}/{name}"
                        if "/" in source.repository else name,
                        base_image=source.base_image)
        session.add(clone)
        session.commit()
        return _project_json(clone)


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str, state=Depends(get_state)):
    with state["session_factory"]() as session:
        project = _load_project(session, project_id)
        project.deleted_at = utcnow()
        session.commit()


@router.post("/projects/{project_id}/builds", status_code=202)
def start_build(project_id: str, payload: BuildIn, state=Depends(get_state)):
    service: BuildService = state["build_service"]
    with state["session_factory"]() as session:
        project = _load_project(session, project_id)
        try:
            version = check(VERSION_RE, payload.version or project.next_version, "version")
            reference = image_ref(project.repository, version)
        except InvalidSpec as exc:
            raise HTTPException(422, str(exc))
        if session.scalar(select(Build).where(Build.project_id == project_id, Build.version == version)):
            raise HTTPException(409, f"Version {version} has already been built. Versions are never overwritten.")
        if session.scalar(select(func.count()).select_from(Build)
                          .where(Build.project_id == project_id, Build.status.in_(("queued", "preparing",
                                                                                   "pulling", "building",
                                                                                   "testing", "exporting")))):
            raise HTTPException(409, "A build for this project is already running")

        build = Build(project_id=project.id, number=next_build_number(session), version=version,
                      reason=payload.reason, mode=project.mode, spec=dict(project.spec or {}),
                      containerfile=project.containerfile, base_image=project.base_image,
                      architecture=(project.spec or {}).get("architecture",
                                                            config.settings().default_architecture),
                      image_ref=reference, files=(project.spec or {}).get("files", []))
        session.add(build)
        # Only move the suggestion forward: building an older version
        # explicitly must not rewind it.
        if version_key(next_patch(version)) > version_key(project.next_version):
            project.next_version = next_patch(version)
        try:
            session.commit()
        except IntegrityError:
            # Two requests raced past the checks above; the unique constraints
            # on (project, version) and build number are the real guard.
            session.rollback()
            raise HTTPException(409, f"Version {version} is already being built")
        build_id, payload_out = build.id, _build_json(build, project)

    service.enqueue(build_id)
    return payload_out


@router.get("/builds")
def list_builds(limit: int = 50, state=Depends(get_state)):
    with state["session_factory"]() as session:
        builds = session.scalars(select(Build).order_by(Build.number.desc()).limit(min(limit, 200))).all()
        projects = {p.id: p for p in session.scalars(select(Project)).all()}
        return [_build_json(b, projects.get(b.project_id)) for b in builds]


@router.get("/builds/{build_id}")
def get_build(build_id: str, state=Depends(get_state)):
    with state["session_factory"]() as session:
        build = session.get(Build, build_id)
        if build is None:
            raise HTTPException(404, "Build not found")
        project = session.get(Project, build.project_id)
        log = build.log
        if not log:
            log_file = state["build_service"].log_path(build_id)
            # Bounded: a running build's log can reach the truncation cap, and
            # this endpoint is polled. The live stream carries the rest.
            log = log_file.read_text(encoding="utf-8")[-LOG_REPLY_CHARS:] if log_file.is_file() else ""
        return {**_build_json(build, project), "containerfile": build.containerfile, "log": log,
                "manifest": manifest_for(project, build, build.export_sha256),
                "training": _training_detail(build, scanned=_has_completed_scan(session, build.id)),
                "gpu_benchmark": _gpu_benchmark_detail(build, state["build_service"].backend.name),
                "aiml": _aiml_detail(build, state["build_service"].backend.name),
                "openshift": _openshift_detail(build, state["build_service"].backend.name),
                "opencode": _opencode_detail(build, state["build_service"].backend.name)}


@router.get("/builds/{build_id}/log")
def build_log(build_id: str, offset: int = 0, limit: int = LOG_CHUNK_BYTES, tail: int | None = None,
              state=Depends(get_state)):
    """A slice of the build log by byte offset, for clients that poll.

    The WebSocket suits a browser; a terminal client on a jumphost may lose
    its connection at any time and has to resume where it stopped without
    fetching the whole log again. `tail` starts that many bytes before the
    end instead, on a line boundary. Only whole lines are returned while the
    build runs, unless one line is larger than the slice.
    """
    limit = max(1, min(limit, LOG_CHUNK_MAX_BYTES))
    with state["session_factory"]() as session:
        build = session.get(Build, build_id)
        if build is None:
            raise HTTPException(404, "Build not found")
        status, stored = build.status, build.log or ""
    finished = status in ("ready", "failed", "cancelled")

    log_file = state["build_service"].log_path(build_id)
    try:
        size = log_file.stat().st_size
    except FileNotFoundError:
        log_file, size = None, len(stored.encode("utf-8"))  # what the record kept
    if tail is not None:
        offset = size - max(0, tail)
    offset = max(0, min(offset, size))
    if log_file is not None:
        with open(log_file, "rb") as handle:
            handle.seek(offset)
            data = handle.read(limit + 1)
    else:
        data = stored.encode("utf-8")[offset:offset + limit + 1]

    if tail is not None and offset > 0:
        newline = data.find(b"\n")
        if newline >= 0:
            offset, data = offset + newline + 1, data[newline + 1:]
    more = len(data) > limit
    data = data[:limit]
    if more or not finished:
        last = data.rfind(b"\n")
        if last >= 0:
            data = data[:last + 1]
    next_offset = offset + len(data)
    return {"build_id": build_id, "status": status, "finished": finished, "offset": offset,
            "next_offset": next_offset, "size": size, "complete": finished and next_offset >= size,
            "text": data.decode("utf-8", errors="replace")}


def _has_completed_scan(session, build_id: str) -> bool:
    return session.scalar(select(func.count()).select_from(Scan)
                          .where(Scan.build_id == build_id, Scan.state == "completed")) > 0


@router.post("/builds/{build_id}/gpu-report")
def record_gpu_report(build_id: str, payload: GpuReportIn, state=Depends(get_state)):
    """Record the GPU check a user ran on the target machine, for this image only."""
    with state["session_factory"]() as session:
        build = session.get(Build, build_id)
        if build is None:
            raise HTTPException(404, "Build not found")
        if not (build.spec or {}).get("training") or build.status != "ready":
            raise HTTPException(409, "GPU results can be recorded for finished training images only")
        try:
            result = training_checks.validate_gpu_report(payload.report, build.image_ref)
        except training_checks.InvalidReport as exc:
            raise HTTPException(422, str(exc))
        recorded = dict(build.checks or {})
        recorded["results"] = {**(recorded.get("results") or {}), "gpu": result}
        build.checks = recorded
        session.commit()
        return _training_detail(build, scanned=_has_completed_scan(session, build.id))


@router.get("/training/catalog")
def training_catalog_view():
    """The LLM Training & Fine-tuning category: profiles, tools, concepts, targets."""
    return {
        "category": training_catalog.CATEGORY,
        "concepts": training_catalog.CONCEPTS,
        "intents": training_catalog.INTENTS,
        "targets": training_catalog.TARGETS,
        "stacks": {key: {**value, "versions": training_recipes.locked_versions(key, [])}
                   for key, value in training_catalog.STACKS.items()},
        "addons": training_catalog.ADDONS,
        "tools": training_catalog.TOOLS,
        "profiles": training_catalog.PROFILES,
        "directories": training_docs.DIRECTORIES,
    }


@router.post("/training/resolve")
def training_resolve(payload: TrainingResolveIn):
    """Validate a training request. A conflict is an answer here, not an error."""
    try:
        recipe = training_recipes.resolve(payload.training, payload.architecture)
    except training_recipes.TrainingConflict as exc:
        return {"ok": False, "error": str(exc), "alternative": exc.alternative, "label": exc.label}
    return {"ok": True, "recipe": recipe, "recommendation": training_recipes.recommend(payload.training.get("intent")),
            "export_contents": training_docs.export_contents(recipe, scanned=None),
            "before_build": training_docs.before_build(recipe),
            "getting_started": training_docs.getting_started(recipe, "IMAGE")}


@router.get("/gpu-benchmark/catalog")
def gpu_benchmark_catalog_view():
    from layersmith.services.builds import gpu_benchmark_storage

    return {"category": gpu_benchmark_catalog.CATEGORY,
            "cuda_variants": gpu_benchmark_catalog.CUDA_VARIANTS,
            "python_versions": gpu_benchmark_catalog.PYTHON_VERSIONS,
            "tool_groups": gpu_benchmark_catalog.TOOL_GROUPS,
            "core_components": gpu_benchmark_catalog.CORE_COMPONENTS,
            "compatibility": gpu_benchmark_catalog.compatibility_matrix(),
            "pins": gpu_benchmark_catalog.PINS,
            "storage": gpu_benchmark_storage(),
            "defaults": {"cuda_version": "13.0.2", "python_version": "3.12",
                         "tools": gpu_benchmark_catalog.DEFAULT_TOOLS, "name": "gpu-benchmark",
                         "repository": "localhost/gpu-benchmark", "tag": "1.0.0"}}


@router.post("/gpu-benchmark/resolve")
def gpu_benchmark_resolve(payload: GpuBenchmarkResolveIn):
    try:
        recipe = gpu_benchmark_catalog.resolve(payload.gpu_benchmark)
    except gpu_benchmark_catalog.RecipeError as exc:
        return {"ok": False, "error": str(exc)}
    from layersmith.services.builds import gpu_benchmark_storage

    return {"ok": True, "recipe": recipe,
            "getting_started": gpu_benchmark_render.getting_started(recipe, "localhost/gpu-benchmark:1.0.0"),
            "storage": gpu_benchmark_storage(recipe)}


@router.get("/aiml/catalog")
def aiml_catalog_view():
    templates = {}
    for key, template in aiml_catalog.TEMPLATES.items():
        versions = {}
        for version, entry in template["versions"].items():
            included = aiml_recipes.base_packages(entry.get("packages"))
            versions[version] = {**{k: v for k, v in entry.items() if k != "packages"},
                                 "reference": f"{entry['image']}:{entry['tag']}@{entry['digest']}",
                                 "included": {catalog_name: included[catalog_name] for catalog_name in
                                              sorted(included) if catalog_name in aiml_recipes.HIGHLIGHT},
                                 "package_count": len(included)}
        templates[key] = {**{k: v for k, v in template.items() if k not in ("versions", "upstream")},
                          "versions": versions}
    return {"category": aiml_catalog.CATEGORY, "templates": templates, "tools": aiml_catalog.TOOLS,
            "gpus": aiml_catalog.GPUS, "target": aiml_catalog.TARGET, "verified": aiml_catalog.VERIFIED,
            "tests": aiml_checks.TESTS, "sources": aiml_catalog.SOURCES,
            "slurm_defaults": aiml_slurm.DEFAULTS}


@router.post("/aiml/resolve")
def aiml_resolve(payload: AimlResolveIn):
    """Validate an AI/ML request. A conflict is an answer here, not an error."""
    try:
        recipe = aiml_recipes.resolve(payload.aiml)
        compatibility = aiml_compat.check(recipe, driver=payload.driver, gpus=payload.gpus)
    except aiml_catalog.AimlError as exc:
        return {"ok": False, "error": str(exc)}
    from layersmith.services.builds import gpu_benchmark_storage

    return {"ok": True, "recipe": recipe, "compatibility": compatibility, "names": aiml_recipes.default_names(recipe),
            "storage": gpu_benchmark_storage(recipe) if recipe.get("estimated_size_gb") else None}


@router.get("/aiml/upstream/{template}")
def aiml_upstream_tags(template: str):
    try:
        return aiml_upstream.available(template)
    except aiml_catalog.AimlError as exc:
        raise HTTPException(404, str(exc))


@router.post("/aiml/lint")
def aiml_lint_view(payload: AimlLintIn):
    return aiml_lint.lint(payload.containerfile)


def _aiml_build(session, build_id: str) -> tuple[Build, dict]:
    build = session.get(Build, build_id)
    if build is None:
        raise HTTPException(404, "Build not found")
    try:
        recipe = aiml_recipe(build.spec)
    except aiml_catalog.AimlError as exc:
        raise HTTPException(409, str(exc))
    if recipe is None:
        raise HTTPException(409, "This is not an AI/ML/GPU build")
    return build, recipe


@router.post("/builds/{build_id}/aiml/slurm")
def aiml_slurm_script(build_id: str, payload: AimlSlurmIn, state=Depends(get_state)):
    """Generate a batch script for this build's image (or another image named in the request)."""
    with state["session_factory"]() as session:
        build, recipe = _aiml_build(session, build_id)
        image = payload.image or build.image_ref
    try:
        return aiml_slurm.generate(payload.params, image, entrypoint_wraps=recipe["entrypoint_wraps"])
    except aiml_catalog.AimlError as exc:
        raise HTTPException(422, str(exc))


@router.post("/builds/{build_id}/aiml/report")
def aiml_record_report(build_id: str, payload: AimlReportIn, state=Depends(get_state)):
    """Record a GPU or Slurm test that ran on a worker, for exactly this image."""
    with state["session_factory"]() as session:
        build, _recipe = _aiml_build(session, build_id)
        if build.status != "ready":
            raise HTTPException(409, "Test results can be recorded for finished builds only")
        try:
            result = aiml_checks.validate_report(payload.report, build.image_ref)
        except aiml_checks.InvalidReport as exc:
            raise HTTPException(422, str(exc))
        checks = dict(build.checks or {})
        checks["results"] = {**(checks.get("results") or {}), result["check"]: result}
        build.checks = checks
        session.commit()
        return _aiml_detail(build, state["build_service"].backend.name)


@router.post("/builds/{build_id}/aiml/export", status_code=202)
def aiml_export_format(build_id: str, payload: AimlExportIn, state=Depends(get_state)):
    """Write the image as another archive format in the background (docker-archive for Docker hosts)."""
    service: BuildService = state["build_service"]
    if payload.format not in ("docker-archive", "oci-archive"):
        raise HTTPException(422, "format is docker-archive or oci-archive")
    with state["session_factory"]() as session:
        build, _recipe = _aiml_build(session, build_id)
        if build.status != "ready":
            raise HTTPException(409, "Only a finished build can be exported")
        current = ((build.aiml or {}).get("exports") or {}).get(payload.format) or {}
        if current.get("status") == "exporting":
            raise HTTPException(409, "This export is already running")
        if payload.format == getattr(service.backend, "archive_format", None):
            raise HTTPException(409, f"The build's archive already is a {payload.format}")
        if payload.format not in getattr(service.backend, "archive_formats", ()):
            raise HTTPException(409, f"The {service.backend.name} backend cannot write a {payload.format}")

    def work():
        try:
            service.export_format(build_id, payload.format)
        except BuildError:
            pass  # recorded on the build as a failed export

    threading.Thread(target=work, name=f"layersmith-export-{build_id[:8]}", daemon=True).start()
    return {"status": "exporting", "format": payload.format}


AIML_FILES = {
    "packages.json": "application/json", "requirements.lock": "text/plain", "sbom.cdx.json": "application/json",
    "layersmith-aiml-check": "text/x-python", "README.md": "text/markdown",
}


@router.get("/builds/{build_id}/aiml/files/{name}")
def aiml_file(build_id: str, name: str, state=Depends(get_state)):
    with state["session_factory"]() as session:
        build, recipe = _aiml_build(session, build_id)
        if name in ("docker-archive", "docker-archive.sha256", "oci-archive", "oci-archive.sha256"):
            fmt = name.split(".")[0]
            entry = ((build.aiml or {}).get("exports") or {}).get(fmt) or {}
            if entry.get("status") != "ready":
                raise HTTPException(404, "This archive has not been exported")
            path = _safe_download(entry.get("path"), [config.settings().image_dir,
                                                      *storage.previous_image_dirs(session)])
            if name.endswith(".sha256"):
                return Response(f"{entry['sha256']}  {path.name}\n", media_type="text/plain",
                                headers={"Content-Disposition": f'attachment; filename="{path.name}.sha256"'})
            return FileResponse(path, filename=path.name, media_type="application/octet-stream")
        if name not in AIML_FILES:
            raise HTTPException(404, "Unknown file")
        if name == "layersmith-aiml-check":
            body = aiml_render.CHECK_SCRIPT.read_text(encoding="utf-8")
        elif name == "packages.json":
            body = json.dumps(aiml_export.packages(build, recipe), indent=2) + "\n"
        elif name == "requirements.lock":
            body = aiml_export.lock(build)
            if not body:
                raise HTTPException(404, "No package list: the basic test did not run or found no Python")
        elif name == "sbom.cdx.json":
            body = json.dumps(aiml_export.sbom(build, recipe), indent=2) + "\n"
        else:
            archive = Path(build.export_path).name if build.export_path else _aiml_archive_name(build)
            body = aiml_export.readme(build, recipe, archive, build.export_sha256)
        return Response(body, media_type=AIML_FILES[name],
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/openshift/catalog")
def openshift_catalog_view():
    tools = {key: {k: v for k, v in tool.items() if k not in ("versions", "check")}
             | {"versions": list(tool["versions"])} for key, tool in openshift_catalog.TOOLS.items()}
    return {"profile": openshift_catalog.PROFILE, "category": openshift_catalog.CATEGORY,
            "bases": openshift_catalog.BASES, "default_base": openshift_catalog.DEFAULT_BASE,
            "ocp_releases": openshift_catalog.OCP_RELEASES, "default_ocp": openshift_catalog.DEFAULT_OCP,
            "tools": tools, "package_groups": {k: {kk: vv for kk, vv in v.items() if kk != "check"}
                                               for k, v in openshift_catalog.PACKAGE_GROUPS.items()},
            "optional": openshift_catalog.OPTIONAL, "helpers": openshift_catalog.HELPERS,
            "aliases": openshift_catalog.ALIASES, "defaults": openshift_catalog.DEFAULTS,
            "tests": openshift_checks.TESTS, "sources": openshift_catalog.SOURCES,
            "verified": openshift_catalog.VERIFIED}


@router.post("/openshift/resolve")
def openshift_resolve(payload: OpenShiftResolveIn):
    """Validate a toolkit request. An invalid choice is an answer here, not an error."""
    try:
        recipe = openshift_recipes.resolve(payload.openshift)
    except openshift_catalog.OpenShiftError as exc:
        return {"ok": False, "error": str(exc)}
    names = openshift_recipes.default_names(recipe)
    image = f"{names['repository']}:{names['tag']}"
    return {"ok": True, "recipe": recipe, "names": names,
            "commands": openshift_docs.commands(recipe, image, "openshift-admin-1.0.0.tar")}


@router.get("/builds/{build_id}/openshift/files/{name}")
def openshift_file(build_id: str, name: str, state=Depends(get_state)):
    if name not in OPENSHIFT_FILES:
        raise HTTPException(404, "Unknown file")
    with state["session_factory"]() as session:
        build = session.get(Build, build_id)
        if build is None:
            raise HTTPException(404, "Build not found")
        try:
            recipe = openshift_recipe(build.spec)
        except openshift_catalog.OpenShiftError as exc:
            raise HTTPException(409, str(exc))
        if recipe is None:
            raise HTTPException(404, "Not an OpenShift Admin Toolkit build")
        image = build.image_ref or "IMAGE"
        results = (build.checks or {}).get("results") or {}
        if name == "README.md":
            archive = Path(build.export_path).name if build.export_path else _aiml_archive_name(build)
            body = openshift_docs.markdown(recipe, image, archive)
        elif name == "versions.json":
            tools = ((results.get("user") or {}).get("info") or {}).get("tools")
            if not tools:
                raise HTTPException(404, "No versions recorded: the user check did not run for this build")
            body = json.dumps(tools, indent=2) + "\n"
        elif name == "checks.json":
            body = json.dumps(results, indent=2) + "\n"
        elif name == openshift_host.INSTALLER:
            archive = Path(build.export_path).name if build.export_path else _aiml_archive_name(build)
            body = openshift_host.installer(image, archive)
        else:
            body = openshift_docs.distrobox_ini(recipe, image)
        return Response(body, media_type=OPENSHIFT_FILES[name],
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/opencode/catalog")
def opencode_catalog_view():
    """What the opencode wizard offers. Versions only; the pins stay on the server."""
    return {"profile": opencode_catalog.PROFILE, "category": opencode_catalog.CATEGORY,
            "bases": opencode_catalog.BASES, "default_base": opencode_catalog.DEFAULT_BASE,
            "versions": list(opencode_catalog.OPENCODE_VERSIONS), "default_version": opencode_catalog.DEFAULT_OPENCODE,
            "cpu_variants": opencode_catalog.CPU_VARIANTS, "default_cpu": opencode_catalog.DEFAULT_CPU,
            "ripgrep": opencode_catalog.RIPGREP["version"],
            "package_groups": {k: {key: v[key] for key in ("label", "purpose", "packages")}
                               for k, v in opencode_catalog.PACKAGE_GROUPS.items()},
            "optional": opencode_catalog.OPTIONAL, "default_optional": opencode_catalog.DEFAULT_OPTIONAL,
            "environment": opencode_catalog.ENVIRONMENT, "host": opencode_catalog.HOST_DEFAULTS,
            "tests": opencode_checks.TESTS, "sources": opencode_catalog.SOURCES,
            "verified": opencode_catalog.VERIFIED}


@router.post("/opencode/resolve")
def opencode_resolve(payload: OpencodeResolveIn):
    """Validate an opencode request. An invalid choice is an answer here, not an error."""
    try:
        recipe = opencode_recipes.resolve(payload.opencode)
    except opencode_catalog.OpencodeError as exc:
        return {"ok": False, "error": str(exc)}
    names = opencode_recipes.default_names(recipe)
    image = f"{names['repository']}:{names['tag']}"
    return {"ok": True, "recipe": recipe, "names": names,
            "commands": opencode_docs.commands(recipe, image, "opencode-1.0.0.tar")}


@router.get("/builds/{build_id}/opencode/files/{name}")
def opencode_file(build_id: str, name: str, state=Depends(get_state)):
    if name not in OPENCODE_FILES:
        raise HTTPException(404, "Unknown file")
    with state["session_factory"]() as session:
        build = session.get(Build, build_id)
        if build is None:
            raise HTTPException(404, "Build not found")
        try:
            recipe = opencode_recipe(build.spec)
        except opencode_catalog.OpencodeError as exc:
            raise HTTPException(409, str(exc))
        if recipe is None:
            raise HTTPException(404, "Not an opencode Shared Terminal build")
        image = build.image_ref or "IMAGE"
        archive = Path(build.export_path).name if build.export_path else _aiml_archive_name(build)
        results = (build.checks or {}).get("results") or {}
        if name == "README.md":
            body = opencode_docs.markdown(recipe, image, archive)
        elif name == opencode_docs.INSTALLER:
            body = opencode_render.install_script(image, archive)
        elif name == "opencode.json":
            body = (opencode_render.ASSET_DIR / "etc" / "opencode.json").read_text(encoding="utf-8")
        elif name == "versions.json":
            tools = ((results.get("user") or {}).get("info") or {}).get("tools")
            if not tools:
                raise HTTPException(404, "No versions recorded: the user check did not run for this build")
            body = json.dumps(tools, indent=2) + "\n"
        else:
            body = json.dumps(results, indent=2) + "\n"
        return Response(body, media_type=OPENCODE_FILES[name],
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.delete("/builds/{build_id}")
def delete_build(build_id: str, remove_image: bool = True, state=Depends(get_state)):
    """Delete a finished build with its archives, log, scans and local image."""
    with state["session_factory"]() as session:
        build = session.get(Build, build_id)
        if build is None:
            raise HTTPException(404, "Build not found")
        if build.status in ACTIVE_STATES:
            raise HTTPException(409, "This build is still running; wait for it to finish before deleting it")
        image_dirs = [config.settings().image_dir, *storage.previous_image_dirs(session)]
    try:
        state["scan_service"].delete_for_build(build_id)
        return state["build_service"].delete(build_id, image_dirs, remove_image=remove_image)
    except ScanError as exc:
        raise HTTPException(409, str(exc))
    except BuildError as exc:
        raise HTTPException(409, str(exc))
    except LookupError:
        raise HTTPException(404, "Build not found")


@router.post("/builds/{build_id}/airgap", status_code=201)
def create_airgap(build_id: str, state=Depends(get_state)):
    try:
        bundle = state["build_service"].airgap_bundle(build_id)
    except BuildError as exc:
        raise HTTPException(409, str(exc))
    return {"filename": bundle.name, "size": bundle.stat().st_size, "sha256": sha256_file(bundle)}


@router.get("/builds/{build_id}/download/{kind}")
def download(build_id: str, kind: str, state=Depends(get_state)):
    if kind not in ("export", "airgap", "export.sha256"):
        raise HTTPException(404, "Unknown download")
    with state["session_factory"]() as session:
        build = session.get(Build, build_id)
        if build is None:
            raise HTTPException(404, "Build not found")
        settings = config.settings()
        path = _safe_download(build.airgap_path if kind == "airgap" else build.export_path,
                              [settings.image_dir, *storage.previous_image_dirs(session)])
        if kind == "export.sha256":
            # For `sha256sum -c` next to the downloaded archive.
            if not build.export_sha256:
                raise HTTPException(404, "No checksum recorded for this export")
            return Response(f"{build.export_sha256}  {path.name}\n", media_type="text/plain",
                            headers={"Content-Disposition": f'attachment; filename="{path.name}.sha256"'})
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")


def _split_build(session, build_id: str) -> Build:
    build = session.get(Build, build_id)
    if build is None:
        raise HTTPException(404, "Build not found")
    return build


@router.get("/builds/{build_id}/split")
def split_status(build_id: str, state=Depends(get_state)):
    """Planned or finished split of the export into parts under 1 GB."""
    with state["session_factory"]() as session:
        return state["split_service"].status(_split_build(session, build_id))


@router.post("/builds/{build_id}/split", status_code=202)
def start_split(build_id: str, state=Depends(get_state)):
    """Compute part checksums in the background; poll GET for progress."""
    with state["session_factory"]() as session:
        _split_build(session, build_id)
    try:
        state["split_service"].start(build_id)
    except splitting.SplitError as exc:
        raise HTTPException(409, str(exc))
    with state["session_factory"]() as session:
        return state["split_service"].status(_split_build(session, build_id))


@router.get("/builds/{build_id}/split/{filename}")
def split_download(build_id: str, filename: str, request: Request, state=Depends(get_state)):
    """One file of a split download. Only names listed in the manifest exist."""
    with state["session_factory"]() as session:
        build = _split_build(session, build_id)
        settings = config.settings()
        manifest = splitting.current_manifest(build)
        if manifest is None:
            raise HTTPException(404, "This archive has not been split, or has changed since; split it again")
        path = _safe_download(build.export_path, [settings.image_dir, *storage.previous_image_dirs(session)])
    attachment = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if filename == DOWNLOAD_ALL_SCRIPT:
        text = llm_scripts.download_all(f"split archive {manifest['archive']}", _server_url(request),
                                        _split_files(build_id, manifest), f"bash {splitting.SCRIPT_NAME} --load",
                                        host="receiving host")
        return Response(text, media_type="text/plain", headers=attachment)
    tar_sums, part_sums = splitting.checksum_file_names(manifest["archive"])
    text = {splitting.SCRIPT_NAME: splitting.assemble_script, tar_sums: splitting.archive_checksum,
            part_sums: splitting.parts_checksums}.get(filename)
    if text is not None:
        return Response(text(manifest), media_type="text/plain", headers=attachment)
    part = next((part for part in manifest["parts"] if part["name"] == filename), None)
    if part is None:
        raise HTTPException(404, "No such file in this split archive")
    return _ranged(request, part["size"], lambda start, length: splitting.read_range(
        path, part["offset"] + start, length), attachment)


#: The script that fetches every file of a split archive with curl.
DOWNLOAD_ALL_SCRIPT = "download-all.sh"


def _split_files(build_id: str, manifest: dict) -> list[dict]:
    return [{**f, "url": f"/api/builds/{build_id}/split/{f['name']}"} for f in splitting.downloads(manifest)]


@router.get("/builds/{build_id}/files")
def build_files(build_id: str, state=Depends(get_state)):
    """Every file of a build that can be downloaded right now, each with its URL.

    The same URLs the buttons use: plain GETs, so wget and curl fetch them
    as well as a browser does.
    """
    with state["session_factory"]() as session:
        build = session.get(Build, build_id)
        if build is None:
            raise HTTPException(404, "Build not found")
        files = _build_files(session, build)
    return {"build_id": build_id, "files": files}


def _build_files(session, build: Build) -> list[dict]:
    base = f"/api/builds/{build.id}"
    files: list[dict] = []

    def add(group: str, name: str, url: str, kind: str = "file", **extra):
        files.append({"group": group, "name": name, "kind": kind, "url": url,
                      **{k: v for k, v in extra.items() if v is not None}})

    exported = build.status == "ready" and _downloadable(build.export_path)
    if exported:
        archive = Path(build.export_path).name
        add("Image archive", archive, f"{base}/download/export", "part", size=build.export_size,
            sha256=build.export_sha256)
        if build.export_sha256:
            add("Image archive", f"{archive}.sha256", f"{base}/download/export.sha256", "checksum")
        manifest = splitting.current_manifest(build)
        if manifest:
            for f in _split_files(build.id, manifest):
                add("Split archive", f["name"], f["url"], f["kind"], size=f.get("size"), sha256=f.get("sha256"))
            add("Split archive", DOWNLOAD_ALL_SCRIPT, f"{base}/split/{DOWNLOAD_ALL_SCRIPT}", "script")
    if _downloadable(build.airgap_path):
        add("Air-gap bundle", Path(build.airgap_path).name, f"{base}/download/airgap", "part",
            size=build.airgap_size, sha256=build.airgap_sha256)

    ready = build.status == "ready"
    try:
        aiml = aiml_recipe(build.spec)
    except aiml_catalog.AimlError:
        aiml = None
    if aiml is not None:
        for fmt, entry in ((build.aiml or {}).get("exports") or {}).items():
            if entry.get("status") == "ready" and _downloadable(entry.get("path")):
                name = Path(entry["path"]).name
                add("AI / ML / GPU", name, f"{base}/aiml/files/{fmt}", "part", size=entry.get("size"),
                    sha256=entry.get("sha256"))
                add("AI / ML / GPU", f"{name}.sha256", f"{base}/aiml/files/{fmt}.sha256", "checksum")
        if ready:
            for name in AIML_FILES:
                if name == "requirements.lock" and not aiml_export.lock(build):
                    continue
                add("AI / ML / GPU", name, f"{base}/aiml/files/{name}")
    for group, recipe_of, error, names, path in (
            ("OpenShift Admin Toolkit", openshift_recipe, openshift_catalog.OpenShiftError, OPENSHIFT_FILES,
             "openshift"),
            ("opencode Shared Terminal", opencode_recipe, opencode_catalog.OpencodeError, OPENCODE_FILES,
             "opencode")):
        try:
            recipe = recipe_of(build.spec)
        except error:
            recipe = None
        if recipe is None or not ready:
            continue
        results = (build.checks or {}).get("results") or {}
        for name in names:
            if name == "versions.json" and not ((results.get("user") or {}).get("info") or {}).get("tools"):
                continue
            add(group, name, f"{base}/{path}/files/{name}")

    scan = session.scalars(select(Scan).where(Scan.build_id == build.id, Scan.sbom_path.is_not(None))
                           .order_by(Scan.created_at.desc())).first()
    if scan is not None and Path(scan.sbom_path).is_file():
        add("Security", f"sbom-{scan.id}.{scan.sbom_format or 'json'}.json", f"/api/scans/{scan.id}/sbom",
            sha256=scan.sbom_sha256)
    return files


_RANGE = re.compile(r"bytes=(\d*)-(\d*)")


def _ranged(request: Request, size: int, read, headers: dict) -> Response:
    """Stream size bytes from read(start, length), honouring one Range header.

    Parts are cut from a larger file on the fly, so FileResponse cannot serve
    them; without this, `wget -c` and `curl -C -` could not resume a part.
    """
    headers = {**headers, "Accept-Ranges": "bytes"}
    match = _RANGE.fullmatch((request.headers.get("range") or "").strip())
    if match and (match[1] or match[2]):
        if match[1]:
            start = int(match[1])
            end = min(int(match[2]), size - 1) if match[2] else size - 1
        else:  # bytes=-N: the last N bytes
            start, end = max(size - int(match[2]), 0), size - 1
        if start >= size or start > end:
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
        length = end - start + 1
        return StreamingResponse(read(start, length), status_code=206, media_type="application/octet-stream",
                                 headers={**headers, "Content-Range": f"bytes {start}-{end}/{size}",
                                          "Content-Length": str(length)})
    return StreamingResponse(read(0, size), media_type="application/octet-stream",
                             headers={**headers, "Content-Length": str(size)})


# ------------------------------------------------------------- LLM models

def _model_json(model: LlmModel, service: llm_models.ModelService) -> dict:
    parts = service.current_parts(model)
    return {"id": model.id, "name": model.name, "repo_id": model.repo_id, "revision": model.revision,
            "requested_revision": model.requested_revision, "catalog_key": model.catalog_key,
            "status": model.status, "error": model.error, "license": model.license,
            "total_bytes": model.total_bytes, "done_bytes": service.progress(model),
            "file_count": len(model.files or []), "part_count": len(parts["parts"]) if parts else 0,
            "created_at": model.created_at, "finished_at": model.finished_at}


def _load_model(session, model_id: str) -> LlmModel:
    model = session.get(LlmModel, model_id)
    if model is None:
        raise HTTPException(404, "Model not found")
    return model


def _model_files(model: LlmModel, parts: dict) -> list[dict]:
    """Every file of one model's split download, in fetch order."""
    base = f"/api/llm-models/{model.id}/files/"
    part_sums, file_sums = llm_models.checksum_names(model.name)
    files = [{"name": p["name"], "kind": "part", "size": p["size"], "sha256": p["sha256"], "url": base + p["name"]}
             for p in parts["parts"]]
    for name, kind, text in ((llm_models.install_script_name(model.name), "script",
                              llm_scripts.install_model(model, parts)),
                             (part_sums, "checksum", "".join(f"{p['sha256']}  {p['name']}\n" for p in parts["parts"])),
                             (file_sums, "checksum", "".join(f"{f['sha256']}  {f['name']}\n" for f in model.files))):
        files.append({"name": name, "kind": kind, "size": len(text.encode()), "url": base + name, "_text": text})
    return files


def _public(files: list[dict]) -> list[dict]:
    return [{k: v for k, v in f.items() if not k.startswith("_")} for f in files]


def _server_url(request: Request) -> str:
    """The address the browser used, also behind a reverse proxy."""
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host.split(',')[0].strip()}"


@router.get("/llm-models/catalog")
def llm_model_catalog(state=Depends(get_state)):
    with state["session_factory"]() as session:
        local = {m.repo_id: m for m in session.scalars(select(LlmModel)).all()}
        service = state["model_service"]
        entries = [{**entry, "local": _model_json(local[entry["repo_id"]], service)
                    if entry["repo_id"] in local else None} for entry in llm_catalog.CATALOG]
    directory = Path(config.settings().model_dir)
    usage = shutil.disk_usage(directory) if directory.is_dir() else None
    return {"purposes": llm_catalog.PURPOSES, "models": entries, "gpu": "NVIDIA H200 (141 GB HBM3e)",
            "model_dir": str(directory), "free_bytes": usage.free if usage else None}


@router.get("/llm-models")
def list_llm_models(state=Depends(get_state)):
    with state["session_factory"]() as session:
        models = session.scalars(select(LlmModel).order_by(LlmModel.created_at.desc())).all()
        return [_model_json(m, state["model_service"]) for m in models]


@router.post("/llm-models", status_code=202)
def request_llm_model(payload: ModelIn, state=Depends(get_state)):
    """Check the model on Hugging Face, then download it in the background."""
    service = state["model_service"]
    repo_id = payload.repo_id.strip()
    if payload.catalog_key:
        entry = llm_catalog.BY_KEY.get(payload.catalog_key)
        if entry is None:
            raise HTTPException(422, "Unknown catalog entry")
        repo_id = entry["repo_id"]
    try:
        model_id = service.request(repo_id, payload.revision.strip() or "main", payload.catalog_key)
    except llm_models.ModelError as exc:
        raise HTTPException(422, str(exc))
    with state["session_factory"]() as session:
        return _model_json(_load_model(session, model_id), service)


@router.post("/llm-models/{model_id}/retry", status_code=202)
def retry_llm_model(model_id: str, state=Depends(get_state)):
    with state["session_factory"]() as session:
        model = _load_model(session, model_id)
        if model.status != "failed":
            raise HTTPException(409, "Only a failed download can be retried")
        model.status, model.error = "queued", None
        session.commit()
    state["model_service"].enqueue(model_id)
    with state["session_factory"]() as session:
        return _model_json(_load_model(session, model_id), state["model_service"])


@router.delete("/llm-models/{model_id}")
def delete_llm_model(model_id: str, state=Depends(get_state)):
    try:
        freed = state["model_service"].delete(model_id)
    except LookupError:
        raise HTTPException(404, "Model not found")
    return {"deleted": model_id, "freed_bytes": freed}


@router.get("/llm-models/{model_id}/files")
def llm_model_files(model_id: str, request: Request, state=Depends(get_state)):
    with state["session_factory"]() as session:
        model = _load_model(session, model_id)
        parts = state["model_service"].current_parts(model)
        if parts is None:
            raise HTTPException(409, "The model is not ready, or its files changed on disk")
        files = _public(_model_files(model, parts))
        script = f"download-{model.name}.sh"
        files.append({"name": script, "kind": "script", "url": f"/api/llm-models/{model.id}/files/{script}"})
        return {"model": _model_json(model, state["model_service"]), "files": files,
                "install": f"./{llm_models.install_script_name(model.name)} --dir /opt/gpu_benchmark/models"}


@router.get("/llm-models/{model_id}/files/{filename}")
def llm_model_file(model_id: str, filename: str, request: Request, state=Depends(get_state)):
    """One file of a model's split download; only listed names exist."""
    service = state["model_service"]
    with state["session_factory"]() as session:
        model = _load_model(session, model_id)
        parts = service.current_parts(model)
        if parts is None:
            raise HTTPException(404, "The model is not ready, or its files changed on disk")
        files = _model_files(model, parts)
        session.expunge(model)
    attachment = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if filename == f"download-{model.name}.sh":
        text = llm_scripts.download_all(f"model {model.repo_id}", _server_url(request), _public(files),
                                        f"./{llm_models.install_script_name(model.name)}")
        return Response(text, media_type="text/plain", headers=attachment)
    item = next((f for f in files if f["name"] == filename), None)
    if item is None:
        raise HTTPException(404, "No such file for this model")
    if "_text" in item:
        return Response(item["_text"], media_type="text/plain", headers=attachment)
    part = next(p for p in parts["parts"] if p["name"] == filename)
    return _ranged(request, part["size"], lambda start, length: service.read_part(
        model, {**part, "offset": part["offset"] + start, "size": length}), attachment)


# ---------------------------------------------------------------- GPU kit

def _kit(build_id: str, model_ids: str, state) -> dict:
    """Image parts + model parts + scripts for one GPU benchmark build."""
    service = state["model_service"]
    with state["session_factory"]() as session:
        build = session.get(Build, build_id)
        if build is None:
            raise HTTPException(404, "Build not found")
        if gpu_benchmark_recipe(build.spec) is None:
            raise HTTPException(404, "Kits exist for GPU benchmark builds only")
        image = state["split_service"].status(build)
        wanted = [m for m in dict.fromkeys(filter(None, (model_ids or "").split(",")))]
        models, problems = [], []
        for model_id in wanted:
            model = session.get(LlmModel, model_id)
            parts = service.current_parts(model) if model else None
            if parts is None:
                problems.append(f"Model {model.name if model else model_id} is not ready")
                continue
            session.expunge(model)
            models.append((model, parts))
        manifest = splitting.current_manifest(build)
        files = []
        if manifest:
            files += [{**f, "url": f"/api/builds/{build.id}/split/{f['name']}"} for f in splitting.downloads(manifest)]
        for model, parts in models:
            files += _model_files(model, parts)
        image_ref = build.image_ref or "localhost/gpu-benchmark:1.0.0"
        archive = image.get("archive") or "image.tar"
        setup_text = llm_scripts.setup(image_ref, build.image_id, archive, build.export_size or 0,
                                       build.image_size or 0, [(m.name, sum(f["size"] for f in m.files))
                                                               for m, _ in models])
        start_text = llm_scripts.start_container(image_ref)
        selection = ",".join(m.id for m, _ in models)
        query = f"?models={selection}" if selection else ""
        for name, text in (("setup.sh", setup_text), ("start_container.sh", start_text)):
            files.append({"name": name, "kind": "script", "size": len(text.encode()),
                          "url": f"/api/builds/{build.id}/kit/{name}{query}", "_text": text})
        # Models picked in the wizard: ticked by default once downloaded.
        planned = ((build.spec or {}).get("gpu_benchmark") or {}).get("models") or []
        local = {m.catalog_key: m for m in session.scalars(select(LlmModel).where(LlmModel.catalog_key.in_(planned)))}
        suggested = [local[key].id for key in planned if key in local and local[key].status == "ready"]
        pending = [{"key": key, "name": local[key].name if key in local else llm_catalog.BY_KEY[key]["repo_id"],
                    "status": local[key].status if key in local else "not downloaded"}
                   for key in planned if key in llm_catalog.BY_KEY and not (key in local and local[key].status == "ready")]
        need = (build.export_size or 0) * 2 + (build.image_size or 0) + sum(
            sum(f["size"] for f in m.files) for m, _ in models)
        return {"build_id": build.id, "image": image_ref, "image_split": image["state"],
                "image_split_progress": image.get("progress"), "models": [m.id for m, _ in models],
                "problems": problems, "files": files, "query": query,
                "suggested": suggested, "pending": pending,
                "total_bytes": sum(int(f.get("size") or 0) for f in files),
                "gpu_host_needs_bytes": need, "ready": bool(manifest) and not problems}


@router.get("/builds/{build_id}/kit")
def gpu_kit(build_id: str, models: str = "", state=Depends(get_state)):
    kit = _kit(build_id, models, state)
    files = _public(kit["files"])
    files.append({"name": "download-all.sh", "kind": "script",
                  "url": f"/api/builds/{build_id}/kit/download-all.sh{kit['query']}"})
    return {**kit, "files": files}


@router.get("/builds/{build_id}/kit/{filename}")
def gpu_kit_file(build_id: str, filename: str, request: Request, models: str = "", state=Depends(get_state)):
    kit = _kit(build_id, models, state)
    attachment = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if filename == "download-all.sh":
        if not kit["ready"]:
            raise HTTPException(409, "Split the image archive first (and wait for the models to be ready)")
        text = llm_scripts.download_all(f"GPU kit for {kit['image']}", _server_url(request), _public(kit["files"]),
                                        "chmod +x *.sh && ./setup.sh")
        return Response(text, media_type="text/plain", headers=attachment)
    item = next((f for f in kit["files"] if f["name"] == filename and "_text" in f), None)
    if item is None:
        raise HTTPException(404, "No such kit file")
    return Response(item["_text"], media_type="text/plain", headers=attachment)


@router.post("/uploads", status_code=201)
async def upload(file: UploadFile = File(...), state=Depends(get_state)):
    """Content-addressed upload: the checksum is the identity used in specs."""
    settings = config.settings()
    digest = hashlib.sha256()
    size = 0
    # uuid, not a timestamp: two uploads in the same microsecond would share a
    # name. The file is removed on any failure, including a dropped request.
    temporary = Path(settings.tmp_dir) / f"upload-{uuid.uuid4()}"
    try:
        with open(temporary, "wb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(413,
                                        f"File is larger than {settings.max_upload_bytes // (1024 * 1024)} MiB")
                digest.update(chunk)
                handle.write(chunk)
        checksum = digest.hexdigest()
        temporary.replace(Path(settings.upload_dir) / checksum)
    finally:
        temporary.unlink(missing_ok=True)

    with state["session_factory"]() as session:
        if session.get(Blob, checksum) is None:
            session.add(Blob(sha256=checksum, filename=Path(file.filename or "file").name[:255], size=size))
            session.commit()
    return {"sha256": checksum, "filename": Path(file.filename or "file").name, "size": size}


@router.post("/preview")
def preview(payload: ProjectIn):
    """Containerfile preview for the wizard's review step; nothing is stored."""
    try:
        spec = _prepare_spec(payload.spec, payload.template)
        text, packages, warnings = cf.generate({"name": payload.name or "image", "description": payload.description},
                                               spec)
    except InvalidSpec as exc:
        raise HTTPException(422, str(exc))
    return {"containerfile": text, "packages": packages, "warnings": warnings, "spec": spec}


def create_app(settings=None) -> FastAPI:
    settings = settings or config.settings()
    # The data directory must exist before the database in it can be opened;
    # the rest may have been moved by an administrator, so read those first.
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings.database_url)
    create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        settings.apply_overrides(storage.load(session))
    settings.prepare()
    backend = make_backend(settings)
    service = BuildService(session_factory, backend, settings=settings)
    # Before any worker starts: a build that was running when the process
    # stopped can never finish, and must not show as running forever.
    service.recover_interrupted()
    model_service = llm_models.ModelService(session_factory, settings=settings)
    scanner = scanners.make_scanner(settings)
    scan_service = ScanService(session_factory, backend, scanner, settings=settings, log_bus=service.logs)
    # Scanning is bolted on here rather than inside the build pipeline, so a
    # build neither waits for a scan nor knows a scanner exists.
    split_service = splitting.SplitService(session_factory, settings=settings)

    def after_build(build_id: str) -> None:
        scan_service.after_build(build_id)
        # A GPU benchmark image is meant for the GPU kit: split it right away,
        # so the kit is complete when the build is.
        with session_factory() as session:
            build = session.get(Build, build_id)
            gpu = build is not None and gpu_benchmark_recipe(build.spec) is not None
        if gpu:
            try:
                split_service.start(build_id)
            except splitting.SplitError:
                pass  # the kit offers "Prepare image parts" instead

    service.on_built = after_build
    service.security_artifacts = scan_service.write_artifacts
    # Temporary scan exports are removed as each scan ends; this clears any
    # left behind by a crash or a kill.
    sweep_temporary_exports(settings)
    scan_service.recover_interrupted()

    app = FastAPI(title=config.APP_NAME, version=config.VERSION,
                  description=f"{config.TAGLINE} by xnett.org")
    app.state.layersmith = {"session_factory": session_factory, "build_service": service,
                            "scan_service": scan_service, "settings": settings,
                            "split_service": split_service,
                            "model_service": model_service}
    # Unfinished model downloads continue where they stopped.
    model_service.recover()
    app.include_router(router)

    # The built web UI, when it has been compiled into ./static (see the
    # Containerfile). A client-side router means unknown paths must return
    # index.html rather than 404, but /api must never be swallowed by it.
    static_dir = settings.static_dir or Path(__file__).resolve().parent.parent / "static"
    if static_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

        root = static_dir.resolve()

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            # An unknown API path must answer as the API, not as the web app:
            # otherwise a typo (or a client that collapsed "../.." before
            # sending) gets index.html with 200 instead of a JSON 404.
            if full_path == "api" or full_path.startswith("api/"):
                raise HTTPException(404, "Not found")
            candidate = (root / full_path).resolve()
            # is_relative_to, not startswith: "/srv/static" must not match a
            # sibling "/srv/static-private".
            if full_path and candidate.is_file() and candidate.is_relative_to(root):
                return FileResponse(candidate)
            return FileResponse(root / "index.html")

    @app.websocket("/api/builds/{build_id}/logs")
    async def build_logs(websocket: WebSocket, build_id: str):
        """Replay the log so far, then stream new lines until the build ends.

        Subscribing before the replay means a line produced during the replay
        is queued rather than lost; the browser may see it twice at worst.
        """
        await websocket.accept()
        channel = service.logs.subscribe(build_id)
        try:
            log_file = service.log_path(build_id)
            if log_file.is_file():
                lines = log_file.read_text(encoding="utf-8").splitlines()
                if len(lines) > LOG_REPLAY_LINES:
                    await websocket.send_text(json.dumps(
                        {"type": "log", "line": f"[showing the last {LOG_REPLAY_LINES} of {len(lines)} lines]"}))
                    lines = lines[-LOG_REPLAY_LINES:]
                for line in lines:
                    await websocket.send_text(json.dumps({"type": "log", "line": line}))
            with session_factory() as session:
                build = session.get(Build, build_id)
                finished = build.status if build and build.status in ("ready", "failed", "cancelled") else None
            if finished:
                await websocket.send_text(json.dumps({"type": "done", "status": finished}))
                return
            while True:
                try:
                    message = await asyncio.to_thread(channel.get, True, 30)
                except queue.Empty:
                    await websocket.send_text(json.dumps({"type": "ping"}))
                    continue
                await websocket.send_text(json.dumps(message, default=str))
                if message.get("type") == "done":
                    return
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            service.logs.unsubscribe(build_id, channel)

    return app
