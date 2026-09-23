"""Podman/Buildah backend: builds on the same host as the API.

Commands are argv lists, so no user value is ever interpreted by a shell.
Image references still go through spec.parse_reference first, which is what
keeps a crafted project name from turning into a podman flag.

Builds use --pull=never after the base has been resolved explicitly, so a
rebuild uses the digest recorded in the build record rather than whatever
the tag points at today.
"""

import json
import subprocess
from pathlib import Path

from layersmith.backends.base import BuildError, ImageInfo, LogSink


class PodmanBackend:
    name = "podman"

    def __init__(self, binary: str = "podman", timeout: int = 3 * 60 * 60):
        self.binary = binary
        self.timeout = timeout

    # ----------------------------------------------------------- plumbing

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
        except (ValueError, IndexError):
            raise BuildError(f"Could not read image metadata for {reference}")
        if not data:
            raise BuildError(f"Image not found: {reference}")
        return data[0]

    # ------------------------------------------------------------ backend

    def available(self) -> tuple[bool, str]:
        try:
            version = self._run(["version", "--format", "{{.Client.Version}}"]).strip()
        except (BuildError, FileNotFoundError, OSError) as exc:
            return False, f"{self.binary} is not usable: {exc}"
        return True, f"{self.binary} {version}"

    def resolve_base(self, reference: str, architecture: str, on_log: LogSink) -> str:
        self._run(["pull", "--arch", architecture, reference], on_log=on_log, timeout=45 * 60)
        data = self._inspect_json(reference)
        digests = data.get("RepoDigests") or []
        digest = data.get("Digest") or (digests[0].split("@", 1)[1] if digests and "@" in digests[0] else None)
        if not digest:
            raise BuildError(f"{reference} has no digest; the registry may not support content addressing")
        on_log(f"Base resolved: {reference} -> {digest}")
        return digest

    def build(self, context_dir, image_ref: str, architecture: str, on_log: LogSink) -> ImageInfo:
        context = Path(context_dir)
        if not (context / "Containerfile").is_file():
            raise BuildError("Build context has no Containerfile")
        self._run(
            ["build", "--arch", architecture, "--pull=never", "--layers=false",
             "--tag", image_ref, "--file", "Containerfile", str(context)],
            on_log=on_log,
        )
        return self.inspect(image_ref)

    def inspect(self, image_ref: str) -> ImageInfo:
        data = self._inspect_json(image_ref)
        digests = data.get("RepoDigests") or []
        return ImageInfo(
            reference=image_ref,
            image_id=data.get("Id"),
            digest=data.get("Digest") or (digests[0].split("@", 1)[1] if digests and "@" in digests[0] else None),
            size=data.get("Size"),
            labels=(data.get("Labels") or {}),
        )

    def export(self, image_ref: str, destination, on_log: LogSink) -> int:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run(["save", "--format", "oci-archive", "--output", str(destination), image_ref],
                  on_log=on_log, timeout=60 * 60)
        if not destination.is_file() or destination.stat().st_size == 0:
            raise BuildError("Export produced no archive")
        return destination.stat().st_size

    def remove(self, image_ref: str) -> None:
        try:
            self._run(["rmi", "--force", image_ref])
        except BuildError:
            pass  # already gone; deleting a build record must not fail on this
