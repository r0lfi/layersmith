"""Build orchestration: context preparation, queue, live logs, export, air-gap.

One worker thread runs builds one at a time; podman itself is the expensive
part and parallel builds on one host mostly fight over IO. Each build writes
its log to a file and to any live subscribers, so the browser sees output as
it happens and a reload still shows everything from the start.

A build record is immutable once it finishes: the Containerfile, resolved
packages, base digest and file checksums are stored on the row, not looked up
again from the project.
"""

import hashlib
import json
import queue
import shutil
import tarfile
import threading
from datetime import timezone
from pathlib import Path

from layersmith import config
from layersmith.backends.base import BuildError
from layersmith.core import containerfile as cf
from layersmith.core.spec import export_filename, image_ref
from layersmith.models import Build, Project, utcnow

MAX_LOG_BYTES = 32 * 1024 * 1024

INSTALL_TEXT = """LayerSmith air-gap bundle
=========================

  {image_ref}

Verify, then load the image on the offline host:

  sha256sum -c SHA256SUMS
  {load_command}

The archive is a {archive_format}; both podman and docker can load it, so the
offline host does not have to run the same runtime that built it.

The bundle also contains:

  metadata/manifest.json   what was built, from which base digest
  source/Containerfile     the exact file this image was built from

No network access is required to load this image.
"""


#: Mode for the artifacts LayerSmith produces: image archives and bundles.
#: They are already served over HTTP without authentication, so the data
#: directory is the access boundary, not the file mode - and a scanner
#: running with every capability dropped cannot read a file it does not own
#: unless the mode says it may. Without this, scanning silently fails on any
#: host where the process umask is 077.
ARTIFACT_MODE = 0o644


def readable_artifact(path: Path) -> Path:
    """Give an artifact the mode everything downstream expects."""
    try:
        path.chmod(ARTIFACT_MODE)
    except OSError:
        pass  # a read-only or unusual filesystem must not fail the build
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_for(project: Project, build: Build, export_sha: str | None = None) -> dict:
    return {
        "project": project.name,
        "repository": project.repository,
        "version": build.version,
        "build": build.number,
        "image": build.image_ref,
        "mode": build.mode,
        "base_image": build.base_image,
        "base_digest": build.base_digest,
        "architecture": build.architecture,
        "packages": build.packages or [],
        "files": build.files or [],
        "image_id": build.image_id,
        "image_digest": build.image_digest,
        "image_size": build.image_size,
        "created": build.finished_at.astimezone(timezone.utc).isoformat() if build.finished_at else None,
        "export_sha256": export_sha,
        "layersmith_version": config.VERSION,
        "layersmith_revision": config.REVISION,
    }


class LogBus:
    """Fan-out of build log lines to live subscribers."""

    def __init__(self):
        self._subscribers: dict[str, list[queue.Queue]] = {}
        self._lock = threading.Lock()

    def subscribe(self, build_id: str) -> queue.Queue:
        channel: queue.Queue = queue.Queue(maxsize=10000)
        with self._lock:
            self._subscribers.setdefault(build_id, []).append(channel)
        return channel

    def unsubscribe(self, build_id: str, channel: queue.Queue) -> None:
        with self._lock:
            channels = self._subscribers.get(build_id, [])
            if channel in channels:
                channels.remove(channel)
            if not channels:
                self._subscribers.pop(build_id, None)

    def publish(self, build_id: str, message: dict) -> None:
        with self._lock:
            channels = list(self._subscribers.get(build_id, []))
        for channel in channels:
            try:
                channel.put_nowait(message)
            except queue.Full:
                pass  # a stalled reader must never slow the build down


class BuildService:
    def __init__(self, session_factory, backend, settings=None, log_bus: LogBus | None = None):
        self.session_factory = session_factory
        self.backend = backend
        self.settings = settings or config.settings()
        self.logs = log_bus or LogBus()
        self._queue: queue.Queue[str] = queue.Queue()
        self._worker: threading.Thread | None = None
        #: Called with a build id once an image is ready. The application
        #: wires scanning in here; the build pipeline knows nothing about it.
        self.on_built = None
        #: Called with (build, directory) while bundling, to add whatever
        #: security artifacts exist. Same arrangement: no scanner knowledge
        #: here, and a bundle is still produced when nothing is wired in.
        self.security_artifacts = None

    # ------------------------------------------------------------- queue

    def start(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._loop, name="layersmith-builder", daemon=True)
            self._worker.start()

    def enqueue(self, build_id: str) -> None:
        self._queue.put(build_id)
        self.start()

    def _loop(self) -> None:
        while True:
            build_id = self._queue.get()
            try:
                self.run(build_id)
            except Exception as exc:  # a crash here must not kill the worker
                self._fail(build_id, f"Build worker error: {exc}")
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------ helpers

    def log_path(self, build_id: str) -> Path:
        return Path(self.settings.log_dir) / f"{build_id}.log"

    def context_dir(self, build_id: str) -> Path:
        return Path(self.settings.build_dir) / build_id

    def _emit(self, build_id: str, line: str) -> None:
        # A build that prints endlessly must not fill the log volume. Past the
        # cap the log stops growing on disk; live subscribers still see the
        # output, and the failure reason is usually in the last lines anyway.
        path = self.log_path(build_id)
        try:
            truncated = path.stat().st_size > MAX_LOG_BYTES
        except FileNotFoundError:
            truncated = False
        if not truncated:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()  # stat() below must see this write, not the buffer
                if path.stat().st_size > MAX_LOG_BYTES:
                    handle.write(f"[log truncated at {MAX_LOG_BYTES // (1024 * 1024)} MiB]\n")
        self.logs.publish(build_id, {"type": "log", "line": line})

    def _set_status(self, build_id: str, status: str, **fields) -> None:
        with self.session_factory() as session:
            build = session.get(Build, build_id)
            if build is None:
                return
            build.status = status
            for key, value in fields.items():
                setattr(build, key, value)
            session.commit()
        self.logs.publish(build_id, {"type": "status", "status": status})

    def _fail(self, build_id: str, message: str) -> None:
        self._emit(build_id, f"ERROR: {message}")
        self._set_status(build_id, "failed", error=message[:4000], finished_at=utcnow())
        self.logs.publish(build_id, {"type": "done", "status": "failed"})

    # ------------------------------------------------------- context prep

    def prepare_context(self, build: Build) -> Path:
        """Write the Containerfile, scripts and blobs a build needs."""
        context = self.context_dir(build.id)
        if context.exists():
            shutil.rmtree(context)
        (context / "files").mkdir(parents=True, exist_ok=True)
        (context / "tools").mkdir(parents=True, exist_ok=True)
        (context / "scripts").mkdir(parents=True, exist_ok=True)
        (context / "Containerfile").write_text(build.containerfile, encoding="utf-8")
        total = 0

        for path, kind, source in cf.context_plan(build.spec or {}):
            target = context / path
            if kind == "script":
                target.write_text(cf.script_body((build.spec.get("scripts") or {}).get(source, "")), encoding="utf-8")
                target.chmod(0o755)
            else:
                blob = Path(self.settings.upload_dir) / source
                # is_symlink before is_file: a symlink pointing at a host file
                # would otherwise be copied into the build context. The
                # checksum check below would catch it too, but refusing the
                # link outright is the boundary we actually mean.
                if blob.is_symlink():
                    raise BuildError(f"Uploaded file {source[:12]}… is a link, which is not accepted")
                if not blob.is_file():
                    raise BuildError(f"Uploaded file {source[:12]}… is missing from storage")
                if sha256_file(blob) != source:
                    raise BuildError(f"Uploaded file {source[:12]}… failed its checksum check")
                total += blob.stat().st_size
                if total > self.settings.max_context_bytes:
                    raise BuildError(
                        f"Build context is larger than "
                        f"{self.settings.max_context_bytes // (1024 * 1024)} MiB"
                    )
                shutil.copy2(blob, target, follow_symlinks=False)
        return context

    # --------------------------------------------------------------- run

    def run(self, build_id: str) -> None:
        with self.session_factory() as session:
            build = session.get(Build, build_id)
            if build is None or build.status not in ("queued",):
                return
            project = session.get(Project, build.project_id)
            if project is None:
                self._fail(build_id, "The project this build belongs to no longer exists")
                return
            build.started_at = utcnow()
            build.status = "preparing"
            session.commit()
            project_snapshot = {"name": project.name, "repository": project.repository}

        self.log_path(build_id).parent.mkdir(parents=True, exist_ok=True)
        self._emit(build_id, f"LayerSmith {config.VERSION} - build #{build.number} "
                             f"{project_snapshot['name']} {build.version}")
        try:
            with self.session_factory() as session:
                build = session.get(Build, build_id)

                self._set_status(build_id, "pulling")
                digest = self.backend.resolve_base(build.base_image, build.architecture,
                                                   lambda line: self._emit(build_id, line))
                build.base_digest = digest
                if build.mode == "gui":
                    text, packages, warnings = cf.generate(project_snapshot, build.spec, base_digest=digest)
                    build.containerfile, build.packages = text, packages
                    for warning in warnings:
                        self._emit(build_id, f"WARNING: {warning}")
                session.commit()

                reference = build.image_ref
                context = self.prepare_context(build)
                self._set_status(build_id, "building")
                info = self.backend.build(context, reference, build.architecture,
                                          lambda line: self._emit(build_id, line))

                self._set_status(build_id, "exporting")
                export = Path(self.settings.image_dir) / export_filename(project_snapshot["repository"],
                                                                         build.version)
                size = self.backend.export(reference, export, lambda line: self._emit(build_id, line))
                readable_artifact(export)
                checksum = sha256_file(export)

                build.image_id, build.image_digest, build.image_size = info.image_id, info.digest, info.size
                build.export_path, build.export_size, build.export_sha256 = str(export), size, checksum
                build.status, build.finished_at = "ready", utcnow()
                build.log = self.log_path(build_id).read_text(encoding="utf-8")[-500000:]
                session.commit()

            self._emit(build_id, f"Image ready: {reference} ({size / 1e6:.1f} MB archive, sha256:{checksum[:12]}…)")
            self.logs.publish(build_id, {"type": "done", "status": "ready"})
            if self.on_built is not None:
                try:
                    self.on_built(build_id)
                except Exception as exc:  # a post-build step never fails a build
                    self._emit(build_id, f"NOTE: post-build step did not run: {exc}")
        except BuildError as exc:
            self._fail(build_id, str(exc))
        except Exception as exc:
            self._fail(build_id, f"Unexpected error: {exc}")
        finally:
            # A failed build left its context behind before this; contexts hold
            # uploaded files and can be large.
            if not self.settings.keep_build_contexts:
                shutil.rmtree(self.context_dir(build_id), ignore_errors=True)

    # -------------------------------------------------------- air-gap

    def airgap_bundle(self, build_id: str) -> Path:
        """Build a self-contained tar.gz that can be loaded without a network."""
        with self.session_factory() as session:
            build = session.get(Build, build_id)
            if build is None or build.status != "ready":
                raise BuildError("Only a finished build can be bundled")
            project = session.get(Project, build.project_id)
            export = Path(build.export_path or "")
            if not export.is_file():
                raise BuildError("The exported archive is missing; rebuild or re-export first")

            stem = f"{project.repository.replace('/', '-')}-{build.version}"
            staging = Path(self.settings.tmp_dir) / f"airgap-{build.id}"
            if staging.exists():
                shutil.rmtree(staging)
            (staging / "image").mkdir(parents=True)
            (staging / "metadata").mkdir()
            (staging / "source").mkdir()

            shutil.copy2(export, staging / "image" / export.name)
            (staging / "metadata" / "manifest.json").write_text(
                json.dumps(manifest_for(project, build, build.export_sha256), indent=2) + "\n", encoding="utf-8")
            (staging / "source" / "Containerfile").write_text(build.containerfile, encoding="utf-8")
            load_command = self.backend.load_command(f"image/{export.name}")
            (staging / "INSTALL.txt").write_text(
                INSTALL_TEXT.format(image_ref=build.image_ref, load_command=load_command,
                                    archive_format=getattr(self.backend, "archive_format", "OCI archive")),
                encoding="utf-8")

            # What was known about this image when it was bundled. Absent
            # when it was never scanned - and then the bundle says so rather
            # than staying silent about it.
            if self.security_artifacts is not None:
                self.security_artifacts(build, staging / "security")

            sums = []
            for path in sorted(staging.rglob("*")):
                if path.is_file() and path.name != "SHA256SUMS":
                    sums.append(f"{sha256_file(path)}  {path.relative_to(staging)}")
            (staging / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")

            bundle = Path(self.settings.image_dir) / f"{stem}-airgap.tar.gz"
            with tarfile.open(bundle, "w:gz") as archive:
                for path in sorted(staging.rglob("*")):
                    # recursive=False: rglob already yields every entry, and
                    # letting tarfile recurse into directories would add each
                    # file a second time (and double the bundle size).
                    archive.add(path, arcname=str(Path(stem) / path.relative_to(staging)), recursive=False)
            shutil.rmtree(staging, ignore_errors=True)

            readable_artifact(bundle)
            build.airgap_path = str(bundle)
            build.airgap_size = bundle.stat().st_size
            build.airgap_sha256 = sha256_file(bundle)
            session.commit()
            return bundle
