"""Podman/Buildah backend.

Podman is the preferred runtime: it builds rootless without a daemon, and
`--pull=never` guarantees a build uses the digest resolved earlier rather
than whatever the tag points at now.
"""

from pathlib import Path

from layersmith.backends.cli import CliBackend


class PodmanBackend(CliBackend):
    name = "podman"
    archive_format = "oci-archive"

    def __init__(self, binary: str = "podman", timeout: int = 3 * 60 * 60):
        super().__init__(binary, timeout)

    def _platform_args(self, architecture: str) -> list[str]:
        return ["--arch", architecture]

    def _build_args(self, architecture: str) -> list[str]:
        # --pull=never: the base was resolved and pulled in the previous step.
        # --layers=false: no intermediate layer cache, so a rebuild of the
        # same version really rebuilds rather than replaying stale layers.
        return ["--arch", architecture, "--pull=never", "--layers=false"]

    def _save_args(self, destination: Path) -> list[str]:
        return ["--format", "oci-archive", "--output", str(destination)]
