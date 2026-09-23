"""HTTP API. The web UI is one client of it; nothing is UI-only.

Routes are deliberately thin: they validate input through core.spec, then
call the build service. Anything that touches the filesystem resolves the
path against the configured directory and refuses to leave it.
"""

import asyncio
import hashlib
import json
import uuid
import queue
import shutil
from pathlib import Path

from fastapi import (
    APIRouter, Depends, FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect,
)
from fastapi.responses import FileResponse
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
    Blob, Build, Project, create_all, make_engine, make_session_factory, next_build_number, utcnow,
)
from layersmith.services import storage
from layersmith.services.builds import BuildService, manifest_for, sha256_file

router = APIRouter(prefix="/api")

#: Bounds on how much of a build log is handed to a client at once.
LOG_REPLY_CHARS = 500_000
LOG_REPLAY_LINES = 5_000


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


class StorageIn(BaseModel):
    paths: dict[str, str]


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
        "project_name": project.name if project else None,
        "version": build.version, "status": build.status, "reason": build.reason, "error": build.error,
        "mode": build.mode, "architecture": build.architecture,
        "base_image": build.base_image, "base_digest": build.base_digest,
        "packages": build.packages or [], "image_ref": build.image_ref, "image_id": build.image_id,
        "image_digest": build.image_digest, "image_size": build.image_size,
        "export_size": build.export_size, "export_sha256": build.export_sha256,
        "has_export": bool(build.export_path), "has_airgap": bool(build.airgap_path),
        "airgap_size": build.airgap_size, "airgap_sha256": build.airgap_sha256,
        "started_at": build.started_at, "finished_at": build.finished_at, "created_at": build.created_at,
        "duration_seconds": build.duration_seconds,
    }


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
            "revision": config.REVISION}


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
        "paths": path_rows,
        "storage": {"total": usage.total, "used": usage.used, "free": usage.free},
    }


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


@router.get("/stats")
def stats(state=Depends(get_state)):
    with state["session_factory"]() as session:
        projects = session.scalar(select(func.count()).select_from(Project).where(Project.deleted_at.is_(None)))
        builds = session.scalar(select(func.count()).select_from(Build))
        images = session.scalar(select(func.count()).select_from(Build).where(Build.status == "ready"))
        stored = session.scalar(select(func.sum(Build.export_size)).where(Build.export_size.isnot(None))) or 0
        recent = session.scalars(select(Build).order_by(Build.number.desc()).limit(5)).all()
        names = {p.id: p.name for p in session.scalars(select(Project)).all()}
        return {
            "projects": projects, "builds": builds, "images": images, "storage_bytes": int(stored),
            "recent": [{**_build_json(b), "project_name": names.get(b.project_id)} for b in recent],
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
            text, packages, warnings = cf.generate({"name": project.name, "description": project.description},
                                                   project.spec, base_digest=project.base_digest)
            data["containerfile"] = text
            data["packages"] = packages
            data["warnings"] = warnings
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
                                                                                   "exporting")))):
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
        names = {p.id: p.name for p in session.scalars(select(Project)).all()}
        return [{**_build_json(b), "project_name": names.get(b.project_id)} for b in builds]


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
                "manifest": manifest_for(project, build, build.export_sha256)}


@router.post("/builds/{build_id}/airgap", status_code=201)
def create_airgap(build_id: str, state=Depends(get_state)):
    try:
        bundle = state["build_service"].airgap_bundle(build_id)
    except BuildError as exc:
        raise HTTPException(409, str(exc))
    return {"filename": bundle.name, "size": bundle.stat().st_size, "sha256": sha256_file(bundle)}


@router.get("/builds/{build_id}/download/{kind}")
def download(build_id: str, kind: str, state=Depends(get_state)):
    if kind not in ("export", "airgap"):
        raise HTTPException(404, "Unknown download")
    with state["session_factory"]() as session:
        build = session.get(Build, build_id)
        if build is None:
            raise HTTPException(404, "Build not found")
        settings = config.settings()
        path = _safe_download(build.export_path if kind == "export" else build.airgap_path,
                              [settings.image_dir, *storage.previous_image_dirs(session)])
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")


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
    return {"containerfile": text, "packages": packages, "warnings": warnings}


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

    app = FastAPI(title=config.APP_NAME, version=config.VERSION,
                  description=f"{config.TAGLINE} by xnett.org")
    app.state.layersmith = {"session_factory": session_factory, "build_service": service, "settings": settings}
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
