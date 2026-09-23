"""Build backend selection.

LAYERSMITH_BUILD_BACKEND picks the runtime:

    auto    (default) use Podman if it works, otherwise Docker
    podman  require Podman
    docker  require Docker

`auto` prefers Podman because it builds rootless and without a daemon, but a
Docker host needs no configuration to work.
"""

from layersmith.backends.base import BuildError, ImageInfo  # noqa: F401
from layersmith.backends.docker import DockerBackend
from layersmith.backends.podman import PodmanBackend

BACKENDS = {"podman": PodmanBackend, "docker": DockerBackend}


def make_backend(settings):
    """Return the configured backend, or the first usable one for 'auto'.

    Selection never fails on an unusable runtime: the backend is still
    returned so the Settings page can explain what is wrong, instead of the
    application refusing to start.
    """
    choice = (settings.build_backend or "auto").lower()
    if choice == "docker":
        return DockerBackend(settings.docker_binary)
    if choice == "podman":
        return PodmanBackend(settings.podman_binary)
    if choice != "auto":
        raise ValueError(f"LAYERSMITH_BUILD_BACKEND must be auto, podman or docker, not {choice!r}")

    podman = PodmanBackend(settings.podman_binary)
    if podman.available()[0]:
        return podman
    docker = DockerBackend(settings.docker_binary)
    if docker.available()[0]:
        return docker
    return podman  # report Podman's problem; it is the preferred runtime


def probe_all(settings) -> list[dict]:
    """Availability of every runtime, for the Settings page."""
    results = []
    for name, factory in BACKENDS.items():
        binary = settings.podman_binary if name == "podman" else settings.docker_binary
        available, detail = factory(binary).available()
        results.append({"name": name, "available": available, "detail": detail})
    return results
