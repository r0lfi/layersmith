"""Runtime configuration: where LayerSmith stores things, and how it builds.

Every path is configurable and nothing is hardcoded to a deployment. The
defaults sit under one data directory so the container form is a single
volume mount, while an operator running it directly can point each path
somewhere else.

Directories are created at startup and checked for writability, so a bad
mount fails immediately with a clear message instead of halfway through a
build.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "LayerSmith"
TAGLINE = "Build container images with purpose."
VERSION = "0.1.0"


def _path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser()


@dataclass
class Settings:
    data_dir: Path
    image_dir: Path
    build_dir: Path
    upload_dir: Path
    log_dir: Path
    tmp_dir: Path
    database_url: str
    build_backend: str
    podman_binary: str
    docker_binary: str
    default_architecture: str
    default_namespace: str
    max_upload_bytes: int
    keep_build_contexts: bool
    agent_url: str
    agent_token: str
    static_dir: Path | None

    @property
    def directories(self) -> list[Path]:
        return [self.data_dir, self.image_dir, self.build_dir, self.upload_dir, self.log_dir, self.tmp_dir]

    def prepare(self) -> None:
        """Create the directories and fail loudly if any is not writable."""
        for directory in self.directories:
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise RuntimeError(f"Cannot create {directory}: {exc}. Check the volume mount and permissions.")
            if not os.access(directory, os.W_OK | os.X_OK):
                raise RuntimeError(
                    f"{directory} is not writable by uid {os.getuid()}. "
                    "Mount it with write access or set the matching LAYERSMITH_*_DIR variable."
                )


def load() -> Settings:
    data_dir = _path("LAYERSMITH_DATA_DIR", Path("/data"))
    return Settings(
        data_dir=data_dir,
        image_dir=_path("LAYERSMITH_IMAGE_DIR", data_dir / "images"),
        build_dir=_path("LAYERSMITH_BUILD_DIR", data_dir / "builds"),
        upload_dir=_path("LAYERSMITH_UPLOAD_DIR", data_dir / "uploads"),
        log_dir=_path("LAYERSMITH_LOG_DIR", data_dir / "logs"),
        tmp_dir=_path("LAYERSMITH_TMP_DIR", data_dir / "tmp"),
        database_url=os.environ.get("LAYERSMITH_DATABASE_URL", f"sqlite:///{data_dir / 'layersmith.db'}"),
        # auto: prefer Podman, fall back to Docker. See backends/__init__.py.
        build_backend=os.environ.get("LAYERSMITH_BUILD_BACKEND", "auto"),
        podman_binary=os.environ.get("LAYERSMITH_PODMAN_BINARY", "podman"),
        docker_binary=os.environ.get("LAYERSMITH_DOCKER_BINARY", "docker"),
        default_architecture=os.environ.get("LAYERSMITH_DEFAULT_ARCH", "amd64"),
        default_namespace=os.environ.get("LAYERSMITH_DEFAULT_NAMESPACE", "layersmith"),
        max_upload_bytes=int(os.environ.get("LAYERSMITH_MAX_UPLOAD_BYTES", 512 * 1024 * 1024)),
        keep_build_contexts=os.environ.get("LAYERSMITH_KEEP_BUILD_CONTEXTS", "").lower() in ("1", "true", "yes"),
        agent_url=os.environ.get("LAYERSMITH_AGENT_URL", ""),
        agent_token=os.environ.get("LAYERSMITH_AGENT_TOKEN", ""),
        # Where the built web UI lives. Defaults to ./static next to the
        # package, which is what the Containerfile produces; an installed
        # deployment points it wherever the assets were unpacked.
        static_dir=Path(os.environ["LAYERSMITH_STATIC_DIR"]) if os.environ.get("LAYERSMITH_STATIC_DIR") else None,
    )


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load()
    return _settings


def reset_for_tests(**overrides) -> Settings:
    """Replace the process settings; used by tests and by the CLI entrypoint."""
    global _settings
    _settings = Settings(**{**load().__dict__, **overrides})
    return _settings
