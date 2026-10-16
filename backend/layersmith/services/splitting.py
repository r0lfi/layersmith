"""Split download of an image archive, for transfers that refuse large files.

An exported archive can be several GB, while some environments only accept
files under 1 GB. Splitting here does not write any part to disk: a part is a
byte range of the one export, described by a manifest (offset, size, sha256)
stored on the build. Downloading a part streams that range. Nothing is
duplicated, there are no temporary files to clean up, and the original
"Download TAR" keeps serving the same file untouched.

What has to be computed is the checksums, which means reading the whole
archive once. That runs in a background thread with progress; the manifest is
written in one commit at the end, so an interrupted or failed run leaves the
build exactly as it was. The same pass recomputes the archive's own sha256 and
refuses to describe an archive that no longer matches the checksum recorded
at export time.

The generated assemble-image.sh only needs bash, cat and sha256sum: the
receiving host has neither LayerSmith nor necessarily a container runtime
until the archive is restored.
"""

import hashlib
import re
import shlex
import threading
from pathlib import Path

from layersmith import config
from layersmith.models import Build, utcnow

#: Default and largest part. 900 MB (10^6, not MiB) keeps every part well under
#: 1 GB however the limit is counted (10^9 or 2^30 bytes).
PART_SIZE = 900_000_000
MIN_PART_SIZE = 1_000_000
READ_CHUNK = 4 * 1024 * 1024

SCRIPT_NAME = "assemble-image.sh"
#: Archive names LayerSmith writes (see core.spec.export_filename). Anything
#: else is refused rather than quoted: these names end up in file names on
#: someone else's machine.
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,200}$")


class SplitError(Exception):
    pass


def part_size(settings=None) -> int:
    configured = (settings or config.settings()).split_part_bytes or PART_SIZE
    return max(MIN_PART_SIZE, min(PART_SIZE, int(configured)))


def plan(total: int, size: int) -> list[tuple[int, int]]:
    """(offset, length) of each part. An empty archive still gets one part."""
    if total <= 0:
        return [(0, 0)]
    return [(offset, min(size, total - offset)) for offset in range(0, total, size)]


def part_name(archive: str, index: int, count: int) -> str:
    # Zero-padded so `ls` and shell globs sort them in order: 000..999, and
    # wider only for an archive that needs more than a thousand parts.
    return f"{archive}.part-{index:0{max(3, len(str(count - 1)))}d}"


def stem(archive: str) -> str:
    return archive[:-4] if archive.endswith(".tar") else archive


def checksum_file_names(archive: str) -> tuple[str, str]:
    """(archive checksum file, parts checksum file): x.tar.sha256, x.parts.sha256."""
    return f"{archive}.sha256", f"{stem(archive)}.parts.sha256"


def archive_name(build: Build) -> str:
    name = Path(build.export_path or "").name
    if not SAFE_NAME.match(name):
        raise SplitError(f"Unexpected archive name {name!r}; refusing to split it")
    return name


def _file_identity(path: Path) -> dict:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def current_manifest(build: Build) -> dict | None:
    """The stored manifest, if it still describes the archive on disk."""
    manifest = build.export_parts
    if not manifest or not build.export_path:
        return None
    try:
        identity = _file_identity(Path(build.export_path))
    except OSError:
        return None
    if (manifest.get("archive") != Path(build.export_path).name
            or manifest.get("archive_sha256") != build.export_sha256
            or manifest.get("archive_size") != identity["size"]
            or manifest.get("archive_mtime_ns") != identity["mtime_ns"]):
        return None
    return manifest


def downloads(manifest: dict) -> list[dict]:
    """Every file of a split download, in the order a person would fetch them."""
    archive = manifest["archive"]
    tar_sums, part_sums = checksum_file_names(archive)
    files = [{"name": part["name"], "kind": "part", "size": part["size"], "sha256": part["sha256"]}
             for part in manifest["parts"]]
    files += [{"name": SCRIPT_NAME, "kind": "script", "size": len(assemble_script(manifest).encode())},
              {"name": tar_sums, "kind": "checksum", "size": len(archive_checksum(manifest).encode())},
              {"name": part_sums, "kind": "checksum", "size": len(parts_checksums(manifest).encode())}]
    return files


def archive_checksum(manifest: dict) -> str:
    return f"{manifest['archive_sha256']}  {manifest['archive']}\n"


def parts_checksums(manifest: dict) -> str:
    return "".join(f"{part['sha256']}  {part['name']}\n" for part in manifest["parts"])


def read_range(path: Path, offset: int, length: int):
    """Stream one part straight from the archive, never holding it in memory."""
    with open(path, "rb") as handle:
        handle.seek(offset)
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(READ_CHUNK, remaining))
            if not chunk:
                raise SplitError("The archive ended before this part did; split it again")
            remaining -= len(chunk)
            yield chunk


class SplitService:
    """Computes split manifests in the background, one run per build at a time."""

    def __init__(self, session_factory, settings=None):
        self.session_factory = session_factory
        self.settings = settings or config.settings()
        self._lock = threading.Lock()
        #: build id -> {"state": "running"|"failed", "done", "total", "error"}.
        #: In memory on purpose: a run that dies with the process simply never
        #: happened, and the next request starts over.
        self._jobs: dict[str, dict] = {}

    # ------------------------------------------------------------ status

    def status(self, build: Build) -> dict:
        size = part_size(self.settings)
        total = build.export_size or 0
        available = bool(build.status == "ready" and build.export_path and Path(build.export_path).is_file())
        result = {"part_size": size, "archive_size": total, "planned_parts": len(plan(total, size)),
                  "available": available, "state": "none", "progress": None, "error": None,
                  "archive": Path(build.export_path).name if build.export_path else None,
                  "archive_sha256": build.export_sha256, "files": [], "script": SCRIPT_NAME}
        with self._lock:
            job = dict(self._jobs.get(build.id) or {})
        manifest = current_manifest(build) if available else None
        if job.get("state") == "running":
            result.update(state="running", progress={"done": job["done"], "total": job["total"]})
        elif manifest:
            result.update(state="ready", part_size=manifest["part_size"],
                          planned_parts=len(manifest["parts"]), files=downloads(manifest),
                          created_at=manifest.get("created_at"))
        elif job.get("state") == "failed":
            result.update(state="failed", error=job["error"])
        return result

    # ------------------------------------------------------------- start

    def start(self, build_id: str) -> threading.Thread | None:
        """Start computing the manifest; None when it exists or is already running."""
        with self.session_factory() as session:
            build = session.get(Build, build_id)
            if build is None:
                raise SplitError("Build not found")
            if build.status != "ready" or not build.export_path or not build.export_sha256:
                raise SplitError("The build has no exported archive to split")
            path = Path(build.export_path)
            if not path.is_file():
                raise SplitError("The exported archive is no longer on disk")
            archive = archive_name(build)
            if current_manifest(build):
                return None
            expected, image = build.export_sha256, build.image_ref
        size = part_size(self.settings)
        with self._lock:
            if (self._jobs.get(build_id) or {}).get("state") == "running":
                return None
            self._jobs[build_id] = {"state": "running", "done": 0, "total": path.stat().st_size, "error": None}
        thread = threading.Thread(target=self._run, args=(build_id, path, archive, expected, image, size),
                                  name=f"layersmith-split-{build_id[:8]}", daemon=True)
        thread.start()
        return thread

    def run(self, build_id: str) -> None:
        """Synchronous variant, for tests."""
        thread = self.start(build_id)
        if thread is not None:
            thread.join()

    def _progress(self, build_id: str, done: int) -> None:
        with self._lock:
            job = self._jobs.get(build_id)
            if job:
                job["done"] = done

    def _run(self, build_id: str, path: Path, archive: str, expected: str, image: str | None, size: int) -> None:
        try:
            manifest = self.compute(path, archive, expected, size, lambda done: self._progress(build_id, done))
            manifest["image_ref"] = image
            with self.session_factory() as session:
                build = session.get(Build, build_id)
                # The archive may have been replaced while it was being read.
                if build is None or build.export_sha256 != expected or Path(build.export_path or "") != path:
                    raise SplitError("The archive changed while it was being split; try again")
                build.export_parts = manifest
                session.commit()
            with self._lock:
                self._jobs.pop(build_id, None)
        except Exception as exc:  # reported through status(), never raised into a thread
            with self._lock:
                self._jobs[build_id] = {"state": "failed", "done": 0, "total": 0, "error": str(exc)[:1000]}

    @staticmethod
    def compute(path: Path, archive: str, expected: str | None, size: int, progress=None) -> dict:
        """One read of the archive: a sha256 per part plus the archive's own."""
        before = _file_identity(path)
        ranges = plan(before["size"], size)
        whole = hashlib.sha256()
        parts = []
        done = 0
        with open(path, "rb") as handle:
            for index, (offset, length) in enumerate(ranges):
                digest = hashlib.sha256()
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(READ_CHUNK, remaining))
                    if not chunk:
                        raise SplitError("The archive is shorter than it was a moment ago; try again")
                    digest.update(chunk)
                    whole.update(chunk)
                    remaining -= len(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done)
                parts.append({"index": index, "name": part_name(archive, index, len(ranges)),
                              "offset": offset, "size": length, "sha256": digest.hexdigest()})
        if _file_identity(path) != before:
            raise SplitError("The archive changed while it was being split; try again")
        checksum = whole.hexdigest()
        if expected and checksum != expected:
            raise SplitError(f"The archive on disk no longer matches its recorded checksum "
                             f"(sha256:{expected[:12]}… recorded, sha256:{checksum[:12]}… now); "
                             "rebuild or re-export the image")
        return {"version": 1, "archive": archive, "archive_size": before["size"],
                "archive_mtime_ns": before["mtime_ns"], "archive_sha256": checksum, "part_size": size,
                "parts": parts, "created_at": utcnow().isoformat()}


# ------------------------------------------------------- assemble script

SCRIPT = r"""#!/usr/bin/env bash
# assemble-image.sh - generated by LayerSmith @@VERSION@@
#
# Restores @@ARCHIVE@@ from its @@COUNT@@ part(s), verifies it and optionally
# loads it. Needs only bash and coreutils (cat, sha256sum, df); no LayerSmith,
# Python or container runtime is required to restore the archive.
#
#   chmod +x assemble-image.sh
#   ./assemble-image.sh            restore and verify
#   ./assemble-image.sh --load     ... then podman load -i (docker if no podman)
#
# Options:
#   --load          load the restored archive with podman (or docker)
#   --remove-parts  delete the parts once the archive is restored and verified
#   --force         replace an existing archive that does not match
#   --dir DIR       where the parts are and the archive is written
#                   (default: the directory this script is in)
#   -h, --help      this text
#
# Exit codes: 0 ok, 1 usage, 2 missing part, 3 corrupt part, 4 not enough
# space or write error, 5 existing archive differs, 6 load failed.

set -u -o pipefail

IMAGE=@@IMAGE@@
ARCHIVE=@@ARCHIVE_Q@@
ARCHIVE_SIZE=@@ARCHIVE_SIZE@@
ARCHIVE_SHA256=@@ARCHIVE_SHA256@@
PART_NAMES=(
@@PART_NAMES@@
)
PART_SIZES=(
@@PART_SIZES@@
)
PART_SHA256=(
@@PART_SHA256@@
)

load=0 remove_parts=0 force=0
dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
while [ $# -gt 0 ]; do
  case "$1" in
    --load) load=1 ;;
    --remove-parts) remove_parts=1 ;;
    --force) force=1 ;;
    --dir) [ $# -ge 2 ] || { echo "--dir needs a directory" >&2; exit 1; }; dir=$2; shift ;;
    -h|--help) sed -n '2,/^$/s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)" >&2; exit 1 ;;
  esac
  shift
done
cd "$dir" || { echo "Cannot enter $dir" >&2; exit 1; }

if command -v sha256sum >/dev/null 2>&1; then
  sha256() { sha256sum "$@"; }
elif command -v shasum >/dev/null 2>&1; then
  sha256() { shasum -a 256 "$@"; }
else
  echo "Neither sha256sum nor shasum is installed" >&2; exit 1
fi
first_field() { local line; read -r line; printf '%s\n' "${line%% *}"; }
size_of() { wc -c <"$1" | tr -d ' '; }

partial="$ARCHIVE.partial.$$"
cleanup() { rm -f -- "$partial"; }
trap cleanup EXIT
trap 'echo; echo "Interrupted; nothing was restored." >&2; exit 130' INT TERM

fail() { echo "FAILED"; echo; printf '%s\n' "$@" >&2; echo >&2; }

restore() {
  local i name missing=() wrong=() corrupt=() actual
  printf 'Checking archive parts... '
  for i in "${!PART_NAMES[@]}"; do
    name=${PART_NAMES[$i]}
    if [ ! -f "$name" ]; then
      missing+=("$name")
    elif [ "$(size_of "$name")" != "${PART_SIZES[$i]}" ]; then
      wrong+=("$name: $(size_of "$name") bytes, expected ${PART_SIZES[$i]}")
    fi
  done
  if [ ${#missing[@]} -gt 0 ] || [ ${#wrong[@]} -gt 0 ]; then
    fail "${#PART_NAMES[@]} part(s) expected in $dir." \
         ${missing[@]+"${missing[@]/#/  missing: }"} \
         ${wrong[@]+"${wrong[@]/#/  incomplete: }"} \
         "Copy the missing or incomplete part(s) again, then re-run this script."
    exit 2
  fi
  echo "OK (${#PART_NAMES[@]} parts)"

  printf 'Verifying checksums... '
  for i in "${!PART_NAMES[@]}"; do
    name=${PART_NAMES[$i]}
    actual=$(sha256 "$name" | first_field) || actual=unreadable
    [ "$actual" = "${PART_SHA256[$i]}" ] || corrupt+=("$name")
  done
  if [ ${#corrupt[@]} -gt 0 ]; then
    fail "Checksum mismatch, the file(s) are corrupt:" "${corrupt[@]/#/  corrupt: }" \
         "Copy the corrupt part(s) again, then re-run this script."
    exit 3
  fi
  echo OK

  local free
  free=$(df -Pk . 2>/dev/null | awk 'NR==2 {print $4}')
  if [ -n "$free" ] && [ $((free * 1024)) -lt "$ARCHIVE_SIZE" ]; then
    printf 'Reassembling image archive... '
    fail "Not enough space in $dir: $((ARCHIVE_SIZE / 1000000)) MB needed, $((free * 1024 / 1000000)) MB free." \
         "Free space, or use --dir on a larger volume (the parts must be there too)."
    exit 4
  fi

  printf 'Reassembling image archive... '
  if ! actual=$(cat -- "${PART_NAMES[@]}" | tee -- "$partial" | sha256 | first_field); then
    fail "Could not write $dir/$partial (disk full or read-only?)."
    exit 4
  fi
  echo OK

  printf 'Verifying original archive... '
  if [ "$actual" != "$ARCHIVE_SHA256" ] || [ "$(size_of "$partial")" != "$ARCHIVE_SIZE" ]; then
    fail "The restored archive does not match the original:" \
         "  expected sha256 $ARCHIVE_SHA256" "  got      sha256 $actual"
    exit 3
  fi
  mv -f -- "$partial" "$ARCHIVE" || { fail "Could not rename $partial to $ARCHIVE."; exit 4; }
  echo OK
}

if [ -e "$ARCHIVE" ]; then
  printf 'Found existing %s, verifying... ' "$ARCHIVE"
  if [ "$(size_of "$ARCHIVE")" = "$ARCHIVE_SIZE" ] && [ "$(sha256 "$ARCHIVE" | first_field)" = "$ARCHIVE_SHA256" ]; then
    echo "OK (already restored)"
  elif [ "$force" = 1 ]; then
    echo "differs, replacing it (--force)"
    restore
  else
    fail "$ARCHIVE exists but is not the original archive." \
         "Move it away, or re-run with --force to replace it."
    exit 5
  fi
else
  restore
fi

if [ "$remove_parts" = 1 ]; then
  rm -f -- "${PART_NAMES[@]}" && echo "Removed the ${#PART_NAMES[@]} part(s)."
fi

echo
echo "Image archive restored successfully!"
echo
echo "File: $ARCHIVE"
echo "SHA-256: $ARCHIVE_SHA256"
echo "Image: $IMAGE"
echo
echo "Load image using:"
echo "podman load -i $ARCHIVE"

if [ "$load" = 1 ]; then
  if command -v podman >/dev/null 2>&1; then runtime=podman
  elif command -v docker >/dev/null 2>&1; then runtime=docker
  else echo "--load: neither podman nor docker is installed" >&2; exit 6
  fi
  echo
  echo "+ $runtime load -i $ARCHIVE"
  "$runtime" load -i "$ARCHIVE" || { echo "$runtime load failed" >&2; exit 6; }
fi
"""


def assemble_script(manifest: dict) -> str:
    image = manifest.get("image_ref") or ""
    quote = shlex.quote
    values = {
        "@@VERSION@@": config.VERSION,
        "@@ARCHIVE@@": manifest["archive"],
        "@@ARCHIVE_Q@@": quote(manifest["archive"]),
        "@@COUNT@@": str(len(manifest["parts"])),
        "@@IMAGE@@": quote(image),
        "@@ARCHIVE_SIZE@@": str(int(manifest["archive_size"])),
        "@@ARCHIVE_SHA256@@": quote(manifest["archive_sha256"]),
        "@@PART_NAMES@@": "\n".join(f"  {quote(part['name'])}" for part in manifest["parts"]),
        "@@PART_SIZES@@": "\n".join(f"  {int(part['size'])}" for part in manifest["parts"]),
        "@@PART_SHA256@@": "\n".join(f"  {quote(part['sha256'])}" for part in manifest["parts"]),
    }
    text = SCRIPT
    for key, value in values.items():
        text = text.replace(key, value)
    return text
