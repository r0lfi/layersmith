"""Docker backend, for hosts that run Docker rather than Podman.

Differences that matter, all handled here:

* `--platform linux/<arch>` instead of `--arch`.
* No `--pull=never`. Docker's default is to use a locally present image, and
  the base was pulled in the previous step, so the effect is the same; but
  Docker will pull if the image is absent, whereas Podman fails loudly.
* No `--layers=false`; the build cache is disabled with `--no-cache` so that
  rebuilding a version does not replay stale layers.
* `docker save` writes a docker-archive rather than an OCI archive. Both
  `docker load` and `podman load` read it, so bundles stay usable either way;
  the air-gap INSTALL.txt names the command for the runtime that built it.
"""

from pathlib import Path

from layersmith.backends.cli import CliBackend


class DockerBackend(CliBackend):
    name = "docker"
    archive_format = "docker-archive"
    archive_formats = ("docker-archive",)

    def __init__(self, binary: str = "docker", timeout: int = 3 * 60 * 60):
        super().__init__(binary, timeout)

    def _platform_args(self, architecture: str) -> list[str]:
        return ["--platform", f"linux/{architecture}"]

    def _build_args(self, architecture: str) -> list[str]:
        return ["--platform", f"linux/{architecture}", "--no-cache"]

    def _save_args(self, destination: Path, archive_format: str) -> list[str]:
        return ["--output", str(destination)]
