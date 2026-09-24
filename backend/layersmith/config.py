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
SOURCE_URL = "https://github.com/r0lfi/layersmith"
LICENSE = "MIT"

#: Release identity. The image build stamps these from the git tag and commit
#: (see the Containerfile and .github/workflows/container.yml); a source
#: checkout reports a development build rather than pretending to be a release.
VERSION = os.environ.get("LAYERSMITH_VERSION") or "0.0.0-dev"
REVISION = os.environ.get("LAYERSMITH_REVISION") or "unknown"


#: Paths an administrator may change at runtime, with their environment
#: variable. The data directory and the database are deliberately absent: the
#: settings themselves live there, so moving them is an operator task, done by
#: changing the volume or the variable and restarting.
MOVABLE_PATHS = {
    "image_dir": "LAYERSMITH_IMAGE_DIR",
    "build_dir": "LAYERSMITH_BUILD_DIR",
    "upload_dir": "LAYERSMITH_UPLOAD_DIR",
    "log_dir": "LAYERSMITH_LOG_DIR",
    "tmp_dir": "LAYERSMITH_TMP_DIR",
    "scan_dir": "LAYERSMITH_SCAN_DIR",
}


#: Scanner variables that configure LayerSmith itself rather than a specific
#: scanner; everything else under LAYERSMITH_SCANNER_ is handed to the scanner.
RESERVED_SCANNER_VARS = {"LAYERSMITH_SCANNER", "LAYERSMITH_SCANNER_TIMEOUT"}

TRUE_VALUES = ("1", "true", "yes", "on")


def _path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser()


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in TRUE_VALUES


def _scanner_options() -> dict[str, str]:
    """LAYERSMITH_SCANNER_<KEY> variables, for the scanner backend to read.

    Scanner-specific settings are passed through as a plain mapping so the
    rest of LayerSmith never has to learn any one scanner's option names.
    """
    prefix = "LAYERSMITH_SCANNER_"
    return {
        name[len(prefix):].lower(): value
        for name, value in os.environ.items()
        if name.startswith(prefix) and name not in RESERVED_SCANNER_VARS
    }


def pinned_by_environment() -> set[str]:
    """Movable paths the operator set explicitly; the UI must not override them."""
    return {field for field, variable in MOVABLE_PATHS.items() if os.environ.get(variable)}


@dataclass
class Settings:
    data_dir: Path
    image_dir: Path
    build_dir: Path
    upload_dir: Path
    log_dir: Path
    tmp_dir: Path
    scan_dir: Path
    database_url: str
    build_backend: str
    podman_binary: str
    docker_binary: str
    default_architecture: str
    default_namespace: str
    max_upload_bytes: int
    max_files_per_image: int
    max_context_bytes: int
    keep_build_contexts: bool
    scanner: str
    scan_after_build: bool
    scan_kinds: tuple[str, ...]
    generate_sbom: bool
    scanner_timeout: int
    scanner_options: dict[str, str]
    agent_url: str
    agent_token: str
    static_dir: Path | None

    def apply_overrides(self, overrides: dict) -> None:
        """Apply administrator-set paths, ignoring any pinned by the environment."""
        pinned = pinned_by_environment()
        for field, value in (overrides or {}).items():
            if field in MOVABLE_PATHS and field not in pinned and value:
                setattr(self, field, Path(value))

    @property
    def directories(self) -> list[Path]:
        return [self.data_dir, self.image_dir, self.build_dir, self.upload_dir, self.log_dir, self.tmp_dir,
                self.scan_dir]

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
        # Scan reports and SBOMs, kept beside the images they describe.
        scan_dir=_path("LAYERSMITH_SCAN_DIR", data_dir / "scans"),
        database_url=os.environ.get("LAYERSMITH_DATABASE_URL", f"sqlite:///{data_dir / 'layersmith.db'}"),
        # auto: prefer Podman, fall back to Docker. See backends/__init__.py.
        build_backend=os.environ.get("LAYERSMITH_BUILD_BACKEND", "auto"),
        podman_binary=os.environ.get("LAYERSMITH_PODMAN_BINARY", "podman"),
        docker_binary=os.environ.get("LAYERSMITH_DOCKER_BINARY", "docker"),
        default_architecture=os.environ.get("LAYERSMITH_DEFAULT_ARCH", "amd64"),
        default_namespace=os.environ.get("LAYERSMITH_DEFAULT_NAMESPACE", "layersmith"),
        max_upload_bytes=int(os.environ.get("LAYERSMITH_MAX_UPLOAD_BYTES", 512 * 1024 * 1024)),
        # Bounds on what one image definition can drag into a build, so a
        # single project cannot fill the data volume.
        max_files_per_image=int(os.environ.get("LAYERSMITH_MAX_FILES_PER_IMAGE", 100)),
        max_context_bytes=int(os.environ.get("LAYERSMITH_MAX_CONTEXT_BYTES", 2 * 1024 * 1024 * 1024)),
        keep_build_contexts=_flag("LAYERSMITH_KEEP_BUILD_CONTEXTS", False),
        # auto: scan if a scanner is reachable, otherwise do not. See
        # scanners/__init__.py. Scanning reports; it never blocks a build.
        scanner=os.environ.get("LAYERSMITH_SCANNER", "auto"),
        scan_after_build=_flag("LAYERSMITH_SCAN_AFTER_BUILD", True),
        scan_kinds=tuple(
            kind.strip().lower()
            for kind in os.environ.get("LAYERSMITH_SCAN_KINDS", "vulnerability,secret").split(",")
            if kind.strip()
        ),
        generate_sbom=_flag("LAYERSMITH_GENERATE_SBOM", True),
        scanner_timeout=int(os.environ.get("LAYERSMITH_SCANNER_TIMEOUT", 600)),
        scanner_options=_scanner_options(),
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


#: Paths that default to living under the data directory, and the name each
#: one gets there.
DERIVED_PATHS = {
    "image_dir": "images", "build_dir": "builds", "upload_dir": "uploads",
    "log_dir": "logs", "tmp_dir": "tmp", "scan_dir": "scans",
}


def reset_for_tests(**overrides) -> Settings:
    """Replace the process settings; used by tests and by the CLI entrypoint.

    Moving the data directory moves everything that defaults to living under
    it, unless the caller named that path too. Without this, redirecting the
    data directory quietly left some paths pointing at the real one - which
    a machine with a writable /data hides completely, and CI does not.
    """
    global _settings
    base = load()
    if "data_dir" in overrides:
        data_dir = Path(overrides["data_dir"])
        for field, name in DERIVED_PATHS.items():
            overrides.setdefault(field, data_dir / name)
        overrides.setdefault("database_url", f"sqlite:///{data_dir / 'layersmith.db'}")
    _settings = Settings(**{**base.__dict__, **overrides})
    return _settings
