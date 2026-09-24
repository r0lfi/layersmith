"""Shared implementation for runtimes driven through a command-line client.

Podman and Docker expose nearly the same operations with different flags, so
the process handling, log streaming and JSON parsing live here and each
runtime only declares how it differs:

                        podman                     docker
  pull architecture     --arch amd64               --platform linux/amd64
  avoid implicit pull   --pull=never               (default: uses local image)
  layer caching off     --layers=false             (not available)
  export format         --format oci-archive       docker-archive (docker save)

Commands are argv lists; no user value is ever interpreted by a shell.
"""

import json
import subprocess
from pathlib import Path

from layersmith.backends.base import BuildError, ImageInfo, LogSink


class CliBackend:
    """Base for CLI-driven runtimes. Subclasses set name, binary and flags."""

    name = "cli"
    #: Archive format produced by `export`; both runtimes can load either.
    archive_format = "oci-archive"
    #: Every format this runtime can write, for callers that need a specific
    #: one - a scanner, for instance, that cannot read an oci-archive tar.
    archive_formats = ("oci-archive",)

    def __init__(self, binary: str, timeout: int = 3 * 60 * 60):
        self.binary = binary
        self.timeout = timeout

    # ------------------------------------------------- runtime differences

    def _platform_args(self, architecture: str) -> list[str]:
        raise NotImplementedError

    def _build_args(self, architecture: str) -> list[str]:
        raise NotImplementedError

    def _save_args(self, destination: Path, archive_format: str) -> list[str]:
        raise NotImplementedError

    def load_command(self, archive_name: str) -> str:
        """The command a recipient runs to load an exported archive."""
        return f"{self.binary} load -i {archive_name}"

    # ------------------------------------------------------------ plumbing

    def _run(self, args: list[str], on_log: LogSink | None = None, timeout: int | None = None) -> str:
        argv = [self.binary, *args]
        if on_log is None:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout or 120)
            if done.returncode != 0:
                raise BuildError((done.stderr or done.stdout or "").strip()[:2000] or f"{argv[0]} failed")
            return done.stdout
        on_log("$ " + " ".join(argv))
        collected: list[str] = []
        with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              bufsize=1) as process:
            try:
                for line in process.stdout:
                    line = line.rstrip("\n")
                    collected.append(line)
                    on_log(line)
                code = process.wait(timeout=timeout or self.timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                raise BuildError(f"Timed out after {(timeout or self.timeout) // 60} minutes")
        if code != 0:
            tail = "\n".join(collected[-15:])
            raise BuildError(f"{' '.join(argv[:2])} exited with status {code}\n{tail}")
        return "\n".join(collected)

    def _inspect_json(self, reference: str) -> dict:
        try:
            data = json.loads(self._run(["image", "inspect", reference]))
        except ValueError:
            raise BuildError(f"Could not read image metadata for {reference}")
        if not data:
            raise BuildError(f"Image not found: {reference}")
        return data[0]

    @staticmethod
    def _digest_of(data: dict) -> str | None:
        digests = data.get("RepoDigests") or []
        return data.get("Digest") or (digests[0].split("@", 1)[1] if digests and "@" in digests[0] else None)

    # ------------------------------------------------------------- backend

    def available(self) -> tuple[bool, str]:
        try:
            version = self._run(["version", "--format", "{{.Client.Version}}"]).strip()
        except (BuildError, FileNotFoundError, OSError) as exc:
            return False, f"{self.binary} is not usable: {exc}"
        return True, f"{self.binary} {version}"

    def resolve_base(self, reference: str, architecture: str, on_log: LogSink) -> str:
        self._run(["pull", *self._platform_args(architecture), reference], on_log=on_log, timeout=45 * 60)
        digest = self._digest_of(self._inspect_json(reference))
        if not digest:
            raise BuildError(f"{reference} has no digest; the registry may not support content addressing")
        on_log(f"Base resolved: {reference} -> {digest}")
        return digest

    def build(self, context_dir, image_ref: str, architecture: str, on_log: LogSink) -> ImageInfo:
        context = Path(context_dir)
        if not (context / "Containerfile").is_file():
            raise BuildError("Build context has no Containerfile")
        # Absolute path: Docker resolves --file against the working directory,
        # Podman against the build context. Spelling it out works for both.
        self._run(
            ["build", *self._build_args(architecture), "--tag", image_ref,
             "--file", str(context / "Containerfile"), str(context)],
            on_log=on_log,
        )
        return self.inspect(image_ref)

    def inspect(self, image_ref: str) -> ImageInfo:
        data = self._inspect_json(image_ref)
        return ImageInfo(reference=image_ref, image_id=data.get("Id"), digest=self._digest_of(data),
                         size=data.get("Size"), labels=(data.get("Labels") or data.get("Config", {}).get("Labels") or {}))

    def export(self, image_ref: str, destination, on_log: LogSink, archive_format: str | None = None) -> int:
        destination = Path(destination)
        archive_format = archive_format or self.archive_format
        if archive_format not in self.archive_formats:
            raise BuildError(f"{self.name} cannot write a {archive_format}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run(["save", *self._save_args(destination, archive_format), image_ref],
                  on_log=on_log, timeout=60 * 60)
        if not destination.is_file() or destination.stat().st_size == 0:
            raise BuildError("Export produced no archive")
        return destination.stat().st_size

    def remove(self, image_ref: str) -> None:
        try:
            self._run(["rmi", "--force", image_ref])
        except BuildError:
            pass  # already gone; deleting a build record must not fail on this
