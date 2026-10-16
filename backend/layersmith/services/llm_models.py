"""Download LLMs from Hugging Face for offline GPU hosts.

A model is fetched once into its own folder under the model directory, with
only the files vLLM loads (see llm/catalog.py). Every file is checked against
what Hugging Face publishes for it - the LFS sha256 for weights, the Git blob
id for small files - so a truncated or tampered download never becomes
"ready". Downloads resume: a restart picks unfinished ones up again, and a
partly written file continues from where it stopped.

Once a model is complete it is described as a tar stream (services/vtar.py)
and split into parts under 1 GB, exactly like an image archive, so it can
cross the same transfer limits. No copy of the model is ever written.

Only open models are supported: a gated repository (licence click-through,
token) is refused with a message saying so.
"""

import hashlib
import json
import queue
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from sqlalchemy import select

from layersmith import config
from layersmith.llm import catalog
from layersmith.models import LlmModel, utcnow
from layersmith.services import splitting, vtar

ACTIVE = ("queued", "downloading", "verifying")
#: Space kept free beyond the model itself, so a download never fills the
#: volume LayerSmith also builds on.
HEADROOM = 2 * 1000**3
READ_CHUNK = 4 * 1024 * 1024
TIMEOUT = 60
#: Fixed mtime of every member of a model's tar stream (the layout must not
#: change from one request to the next). 2024-01-01T00:00:00Z.
TAR_MTIME = 1704067200


class ModelError(Exception):
    """A model could not be requested or downloaded; the message is for the user."""


class Cancelled(Exception):
    pass


def archive_name(model_name: str) -> str:
    return f"{model_name}.model.tar"


def install_script_name(model_name: str) -> str:
    return f"install-{model_name}.sh"


def checksum_names(model_name: str) -> tuple[str, str]:
    """(checksums of the parts, checksums of the files inside the model folder)."""
    return f"{model_name}.parts.sha256", f"{model_name}.files.sha256"


def _git_blob_id(path: Path, size: int) -> str:
    digest = hashlib.sha1(f"blob {size}\0".encode())  # noqa: S324 - Git's object id, not a security hash
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(READ_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ModelService:
    def __init__(self, session_factory, settings=None, opener=None):
        self.session_factory = session_factory
        self.settings = settings or config.settings()
        self.opener = opener or urllib.request.build_opener()
        self._queue: queue.Queue[str] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()
        self._cancel: set[str] = set()
        #: Bytes written so far per downloading model; the database is only
        #: updated every few seconds.
        self._progress: dict[str, int] = {}

    # ------------------------------------------------------------- HTTP

    def _get(self, url: str, headers: dict | None = None):
        request = urllib.request.Request(url, headers={"User-Agent": f"LayerSmith/{config.VERSION}",
                                                       **(headers or {})})
        return self.opener.open(request, timeout=TIMEOUT)

    def metadata(self, repo_id: str, revision: str = "main") -> dict:
        """Hugging Face's listing of one revision, with sizes and checksums."""
        if not catalog.REPO_RE.match(repo_id or ""):
            raise ModelError(f"{repo_id!r} is not a Hugging Face model id like Qwen/Qwen3-8B")
        if not catalog.REVISION_RE.match(revision or "") or ".." in revision:
            raise ModelError(f"{revision!r} is not a valid revision")
        url = (f"{self.settings.hf_endpoint}/api/models/{repo_id}/revision/"
               f"{urllib.parse.quote(revision, safe='')}?blobs=true")
        try:
            with self._get(url) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):
                raise ModelError(f"{repo_id} ({revision}) was not found on Hugging Face, or it needs a login. "
                                 "Only open models can be downloaded.")
            raise ModelError(f"Hugging Face answered {exc.code} for {repo_id}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelError(f"Cannot reach {self.settings.hf_endpoint}: {getattr(exc, 'reason', exc)}")
        if data.get("gated") not in (None, False):
            raise ModelError(f"{repo_id} is gated (its licence must be accepted on huggingface.co and a token is "
                             "needed). Only open models are supported; for Llama, the RedHatAI copies are open.")
        if data.get("private"):
            raise ModelError(f"{repo_id} is private")
        return data

    # ------------------------------------------------------------ requests

    def request(self, repo_id: str, revision: str = "main", catalog_key: str | None = None) -> str:
        """Check the model, reserve its folder name and queue the download."""
        data = self.metadata(repo_id, revision)
        commit = data.get("sha")
        listing = {s["rfilename"]: s for s in data.get("siblings") or []}
        files = catalog.select_files(list(listing.values()))
        if not catalog.has_weights(files):
            raise ModelError(f"{repo_id} has no config.json with .safetensors weights at the top level; "
                             "vLLM cannot load it from /models")
        for entry in files:
            entry["blob_id"] = listing[entry["name"]].get("blobId")
            entry["expected_sha256"] = entry.pop("sha256")
        total = sum(f["size"] for f in files)
        with self.session_factory() as session:
            existing = session.scalar(select(LlmModel).where(LlmModel.repo_id == repo_id,
                                                             LlmModel.revision == commit))
            if existing is not None:
                if existing.status == "failed":
                    existing.status, existing.error = "queued", None
                    session.commit()
                    self.enqueue(existing.id)
                return existing.id
            self.check_space(total)
            taken = {name for (name,) in session.execute(select(LlmModel.name))}
            license_name = (data.get("cardData") or {}).get("license")
            model = LlmModel(name=catalog.folder_name(repo_id, taken), repo_id=repo_id, revision=commit,
                             requested_revision=revision, catalog_key=catalog_key, files=files, total_bytes=total,
                             license=str(license_name)[:64] if license_name else None)
            session.add(model)
            session.commit()
            model_id = model.id
        self.enqueue(model_id)
        return model_id

    def check_space(self, needed: int) -> None:
        directory = Path(self.settings.model_dir)
        directory.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(directory).free
        if free < needed + HEADROOM:
            raise ModelError(f"Not enough space in the model directory {directory}: the model needs "
                             f"{needed / 1e9:.1f} GB, {free / 1e9:.1f} GB is free (and {HEADROOM // 1000**3} GB is "
                             "kept in reserve). Free space or point LAYERSMITH_MODEL_DIR (Settings -> Storage) "
                             "at a larger volume.")

    # --------------------------------------------------------------- queue

    def enqueue(self, model_id: str) -> None:
        self._queue.put(model_id)
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, name="layersmith-models", daemon=True)
                self._worker.start()

    def recover(self) -> None:
        """Resume downloads a stop or restart interrupted."""
        with self.session_factory() as session:
            pending = session.scalars(select(LlmModel).where(LlmModel.status.in_(ACTIVE))
                                      .order_by(LlmModel.created_at)).all()
            for model in pending:
                model.status = "queued"
            session.commit()
            ids = [model.id for model in pending]
        for model_id in ids:
            self.enqueue(model_id)

    def _loop(self) -> None:
        while True:
            model_id = self._queue.get()
            try:
                self.run(model_id)
            except Exception as exc:  # never let one model stop the worker
                self._set(model_id, status="failed", error=f"Download worker error: {exc}"[:2000])
            finally:
                self._queue.task_done()

    def _set(self, model_id: str, **fields) -> None:
        with self.session_factory() as session:
            model = session.get(LlmModel, model_id)
            if model is not None:
                for key, value in fields.items():
                    setattr(model, key, value)
                session.commit()

    def progress(self, model: LlmModel) -> int:
        with self._lock:
            return self._progress.get(model.id, model.done_bytes or 0)

    # ------------------------------------------------------------ download

    def staging_dir(self, model_id: str) -> Path:
        return Path(self.settings.model_dir) / f".download-{model_id}"

    def run(self, model_id: str) -> None:
        with self.session_factory() as session:
            model = session.get(LlmModel, model_id)
            if model is None or model.status not in ("queued", "downloading"):
                return
            model.status, model.error = "downloading", None
            session.commit()
            repo_id, commit, name = model.repo_id, model.revision, model.name
            files = [dict(f) for f in model.files]
        staging = self.staging_dir(model_id)
        final = Path(self.settings.model_dir) / name
        try:
            staging.mkdir(parents=True, exist_ok=True)
            already = sum(min((staging / f["name"]).stat().st_size, f["size"])
                          for f in files if (staging / f["name"]).is_file())
            self.check_space(sum(f["size"] for f in files) - already)
            done = 0
            last_saved = time.monotonic()
            for entry in files:
                for written in self._download(model_id, repo_id, commit, entry, staging / entry["name"]):
                    with self._lock:
                        self._progress[model_id] = done + written
                    if time.monotonic() - last_saved > 5:
                        self._set(model_id, done_bytes=done + written)
                        last_saved = time.monotonic()
                done += entry["size"]

            self._set(model_id, status="verifying", done_bytes=done)
            for entry in files:
                entry["sha256"] = self._verify(staging / entry["name"], entry)
                (staging / entry["name"]).chmod(0o644)
            if model_id in self._cancel:
                raise Cancelled()
            if final.exists():
                raise ModelError(f"{final} already exists and is not managed by LayerSmith; move it away")
            staging.rename(final)
            clean = [{"name": f["name"], "size": f["size"], "sha256": f["sha256"]} for f in files]
            parts = self.compute_parts(final, name, clean)
            self._set(model_id, status="ready", path=str(final), files=clean, parts=parts, done_bytes=done,
                      finished_at=utcnow())
        except Cancelled:
            shutil.rmtree(staging, ignore_errors=True)
        except (ModelError, splitting.SplitError) as exc:
            self._set(model_id, status="failed", error=str(exc)[:2000])
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Kept on disk: the next attempt resumes the partial file.
            reason = getattr(exc, "reason", None) or exc
            self._set(model_id, status="failed",
                      error=f"Download interrupted: {reason}. Retry to continue where it stopped."[:2000])
        finally:
            with self._lock:
                self._progress.pop(model_id, None)
                self._cancel.discard(model_id)

    def _download(self, model_id: str, repo_id: str, commit: str, entry: dict, target: Path):
        """Fetch one file, resuming a partial one; yields bytes written so far."""
        size = entry["size"]
        have = target.stat().st_size if target.is_file() else 0
        if have > size:
            target.unlink()
            have = 0
        if have == size:
            yield have
            return
        url = f"{self.settings.hf_endpoint}/{repo_id}/resolve/{commit}/{urllib.parse.quote(entry['name'])}"
        headers = {"Range": f"bytes={have}-"} if have else {}
        with self._get(url, headers) as response:
            if have and response.status != 206:
                have = 0  # the server ignored the range; start over
            with open(target, "ab" if have else "wb") as handle:
                while True:
                    if model_id in self._cancel:
                        raise Cancelled()
                    chunk = response.read(READ_CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    have += len(chunk)
                    if have > size:
                        raise ModelError(f"{entry['name']} is larger than Hugging Face said ({size} bytes)")
                    yield have
        if have != size:
            raise OSError(f"{entry['name']} stopped at {have} of {size} bytes")

    @staticmethod
    def _verify(path: Path, entry: dict) -> str:
        """sha256 of a downloaded file, after checking it against Hugging Face's own id."""
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(READ_CHUNK), b""):
                digest.update(chunk)
        checksum = digest.hexdigest()
        if entry.get("expected_sha256"):
            if checksum != entry["expected_sha256"]:
                path.unlink(missing_ok=True)
                raise ModelError(f"{entry['name']} does not match the sha256 Hugging Face publishes; "
                                 "it was removed, retry the download")
        elif entry.get("blob_id"):
            if _git_blob_id(path, entry["size"]) != entry["blob_id"]:
                path.unlink(missing_ok=True)
                raise ModelError(f"{entry['name']} does not match its Git blob id on Hugging Face; "
                                 "it was removed, retry the download")
        return checksum

    # --------------------------------------------------------------- split

    def layout(self, folder: Path, name: str, files: list[dict]) -> vtar.Layout:
        return vtar.layout(folder, name, files, TAR_MTIME)

    def compute_parts(self, folder: Path, name: str, files: list[dict]) -> dict:
        lay = self.layout(folder, name, files)
        archive = archive_name(name)
        parts, checksum = splitting.hash_parts(lay.read(), lay.size, archive, splitting.part_size(self.settings))
        identity = {f["name"]: (folder / f["name"]).stat().st_mtime_ns for f in files}
        return {"version": 1, "archive": archive, "archive_size": lay.size, "archive_sha256": checksum,
                "part_size": splitting.part_size(self.settings),
                "parts": parts, "identity": identity, "created_at": utcnow().isoformat()}

    @staticmethod
    def current_parts(model: LlmModel) -> dict | None:
        """The stored split, if the files on disk are still the ones it describes."""
        if model.status != "ready" or not model.parts or not model.path:
            return None
        folder = Path(model.path)
        try:
            for entry in model.files:
                stat = (folder / entry["name"]).stat()
                if stat.st_size != entry["size"] or model.parts["identity"].get(entry["name"]) != stat.st_mtime_ns:
                    return None
        except (OSError, KeyError):
            return None
        return model.parts

    def read_part(self, model: LlmModel, part: dict):
        lay = self.layout(Path(model.path), model.name, model.files)
        return lay.read(part["offset"], part["size"])

    # -------------------------------------------------------------- delete

    def delete(self, model_id: str) -> int:
        """Remove a model and its folder, cancelling a running download."""
        with self.session_factory() as session:
            model = session.get(LlmModel, model_id)
            if model is None:
                raise LookupError("Model not found")
            freed = sum(f["size"] for f in model.files) if model.status == "ready" else 0
            path = Path(model.path) if model.path else None
            active = model.status in ACTIVE
            session.delete(model)
            session.commit()
        if active:
            with self._lock:
                self._cancel.add(model_id)
        root = Path(self.settings.model_dir).resolve()
        for folder in (path, self.staging_dir(model_id)):
            if folder and folder.resolve().is_relative_to(root) and folder.resolve() != root:
                shutil.rmtree(folder, ignore_errors=True)
        return freed
