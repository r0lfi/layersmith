"""Administrator-editable storage paths.

Where LayerSmith keeps images, build contexts, uploads, logs and scratch space
is editable at runtime and stored in the database, so it survives a restart
without editing environment variables.

Two rules make this safe rather than a foot-gun:

* A path an operator pinned with an environment variable wins, and the UI
  shows it as read-only. Deployment decisions stay with whoever wrote the
  unit file or the compose file.
* Moving a path never moves existing files. Builds record absolute paths, so
  an archive written before a change stays downloadable: previous image
  directories are remembered for exactly that.

The data directory and the database are not movable here; the settings
themselves live there.
"""

import os
from pathlib import Path

from layersmith import config
from layersmith.models import Setting

KEY = "storage"
#: Paths that would put application data somewhere a mistake is expensive.
REFUSED_PREFIXES = ("/proc", "/sys", "/dev", "/boot", "/etc", "/bin", "/sbin", "/lib", "/usr")


class StorageError(ValueError):
    """Raised with a message meant for the administrator."""


def load(session) -> dict:
    row = session.get(Setting, KEY)
    return dict(row.value or {}) if row else {}


def previous_image_dirs(session) -> list[Path]:
    return [Path(p) for p in load(session).get("previous_image_dirs", [])]


def validate(field: str, value: str) -> Path:
    if field not in config.MOVABLE_PATHS:
        raise StorageError(f"{field} cannot be changed here")
    path = Path(str(value).strip()).expanduser()
    if not path.is_absolute():
        raise StorageError(f"{value!r} is not an absolute path")
    if ".." in path.parts:
        raise StorageError(f"{value!r} must not contain '..'")
    if str(path) == "/" or str(path).startswith(REFUSED_PREFIXES):
        raise StorageError(f"{path} is a system directory; choose a data directory instead")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageError(f"Cannot create {path}: {exc}")
    if not os.access(path, os.W_OK | os.X_OK):
        raise StorageError(f"{path} is not writable by uid {os.getuid()}")
    return path


def save(session, settings: config.Settings, requested: dict) -> dict:
    """Validate, persist and apply path changes. Returns the stored overrides."""
    pinned = config.pinned_by_environment()
    stored = load(session)
    previous = list(stored.get("previous_image_dirs", []))

    for field, value in (requested or {}).items():
        if field in pinned:
            raise StorageError(f"{field} is set by {config.MOVABLE_PATHS[field]} and cannot be changed here")
        path = validate(field, value)
        # Remember where archives already written can still be found.
        if field == "image_dir" and str(settings.image_dir) != str(path):
            if str(settings.image_dir) not in previous:
                previous.append(str(settings.image_dir))
        stored[field] = str(path)

    stored["previous_image_dirs"] = previous[-10:]
    row = session.get(Setting, KEY)
    if row is None:
        session.add(Setting(key=KEY, value=stored))
    else:
        row.value = dict(stored)
    session.commit()

    settings.apply_overrides(stored)
    settings.prepare()
    return stored


def describe(session, settings: config.Settings) -> list[dict]:
    """Every path with where its value came from, for the settings page."""
    pinned = config.pinned_by_environment()
    stored = load(session)
    rows = [{
        "field": "data_dir", "label": "Application data", "path": str(settings.data_dir),
        "variable": "LAYERSMITH_DATA_DIR", "editable": False,
        "source": "environment" if os.environ.get("LAYERSMITH_DATA_DIR") else "default",
        "note": "Holds the database; change it with the volume or variable and restart.",
    }]
    labels = {
        "image_dir": "Image exports and bundles",
        "build_dir": "Build workspace",
        "upload_dir": "Uploaded files",
        "log_dir": "Build logs",
        "tmp_dir": "Temporary",
    }
    for field, variable in config.MOVABLE_PATHS.items():
        rows.append({
            "field": field, "label": labels[field], "path": str(getattr(settings, field)),
            "variable": variable, "editable": field not in pinned,
            "source": "environment" if field in pinned else ("setting" if field in stored else "default"),
            "note": "Set in the environment, so it cannot be changed here." if field in pinned else "",
        })
    return rows
