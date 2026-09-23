"""The build backend interface.

LayerSmith never builds images itself: it prepares a context directory and
hands it to a backend. `podman` runs buildah/podman on the same host as the
API; a remote agent backend can implement the same five operations without
the rest of the application knowing the difference.

Implementations must:
  * take argv lists, never shell strings (no command injection surface),
  * report progress by calling `on_log` line by line, so the UI can stream,
  * raise BuildError with a message meant for the user.
"""

from dataclasses import dataclass, field
from typing import Callable, Protocol

LogSink = Callable[[str], None]


class BuildError(RuntimeError):
    """A backend operation failed; the message is shown to the user."""


@dataclass
class ImageInfo:
    reference: str
    image_id: str | None = None
    digest: str | None = None
    size: int | None = None
    labels: dict = field(default_factory=dict)


class BuildBackend(Protocol):
    name: str

    def available(self) -> tuple[bool, str]:
        """(usable, human-readable detail) - shown on the settings page."""

    def resolve_base(self, reference: str, architecture: str, on_log: LogSink) -> str:
        """Pull/inspect a base image and return its digest (sha256:...)."""

    def build(self, context_dir, image_ref: str, architecture: str, on_log: LogSink) -> ImageInfo:
        """Build the context and return the resulting image."""

    def inspect(self, image_ref: str) -> ImageInfo:
        """Identity and size of a built image."""

    def export(self, image_ref: str, destination, on_log: LogSink) -> int:
        """Write an OCI archive to `destination`; return its size in bytes."""

    def remove(self, image_ref: str) -> None:
        """Delete a built image from local storage."""
