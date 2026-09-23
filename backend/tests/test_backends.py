"""Backend selection and the argv each runtime produces.

The commands are asserted rather than run: these tests must pass on a host
with neither runtime installed. test_podman_smoke.py does the real thing.
"""

from pathlib import Path

import pytest

from layersmith import config
from layersmith.backends import BACKENDS, make_backend, probe_all
from layersmith.backends.base import BuildError
from layersmith.backends.docker import DockerBackend
from layersmith.backends.podman import PodmanBackend


class Recorder:
    """Stands in for the subprocess layer and records the argv it was given."""

    def __init__(self, backend, output=""):
        self.backend = backend
        self.output = output
        self.calls: list[list[str]] = []
        backend._run = self._run  # type: ignore[method-assign]

    def _run(self, args, on_log=None, timeout=None):
        self.calls.append([self.backend.binary, *args])
        if on_log:
            on_log("line")
        return self.output

    @property
    def last(self) -> list[str]:
        return self.calls[-1]


def settings_with(**overrides):
    return config.reset_for_tests(**overrides)


# ------------------------------------------------------------- selection

def test_explicit_choice_wins(tmp_path):
    assert isinstance(make_backend(settings_with(build_backend="docker")), DockerBackend)
    assert isinstance(make_backend(settings_with(build_backend="podman")), PodmanBackend)


def test_auto_prefers_podman_then_docker(monkeypatch):
    monkeypatch.setattr(PodmanBackend, "available", lambda self: (True, "podman 5.0"))
    assert make_backend(settings_with(build_backend="auto")).name == "podman"

    monkeypatch.setattr(PodmanBackend, "available", lambda self: (False, "not installed"))
    monkeypatch.setattr(DockerBackend, "available", lambda self: (True, "docker 27"))
    assert make_backend(settings_with(build_backend="auto")).name == "docker"


def test_auto_with_no_runtime_still_returns_something_explainable(monkeypatch):
    monkeypatch.setattr(PodmanBackend, "available", lambda self: (False, "podman missing"))
    monkeypatch.setattr(DockerBackend, "available", lambda self: (False, "docker missing"))
    backend = make_backend(settings_with(build_backend="auto"))
    assert backend.name == "podman" and backend.available() == (False, "podman missing")


def test_unknown_backend_name_is_rejected():
    with pytest.raises(ValueError):
        make_backend(settings_with(build_backend="containerd"))


def test_probe_reports_every_runtime(monkeypatch):
    monkeypatch.setattr(PodmanBackend, "available", lambda self: (True, "podman 5.0"))
    monkeypatch.setattr(DockerBackend, "available", lambda self: (False, "docker missing"))
    probed = {entry["name"]: entry for entry in probe_all(settings_with())}
    assert set(probed) == set(BACKENDS)
    assert probed["podman"]["available"] is True and probed["docker"]["available"] is False


# ----------------------------------------------------------------- podman

def test_podman_pull_and_build_flags():
    backend = PodmanBackend()
    recorder = Recorder(backend, output='[{"Digest": "sha256:aa", "Id": "sha256:bb", "Size": 1}]')
    backend.resolve_base("alpine:3.22", "arm64", lambda line: None)
    assert recorder.calls[0] == ["podman", "pull", "--arch", "arm64", "alpine:3.22"]

    backend.build(_context(), "repo/app:1.0.0", "amd64", lambda line: None)
    build_argv = [call for call in recorder.calls if call[1] == "build"][0]
    assert "--pull=never" in build_argv and "--layers=false" in build_argv
    assert build_argv[2:4] == ["--arch", "amd64"]


def test_podman_exports_an_oci_archive(tmp_path):
    backend = PodmanBackend()
    recorder = Recorder(backend)
    destination = tmp_path / "out.tar"
    destination.write_bytes(b"x")
    backend.export("repo/app:1.0.0", destination, lambda line: None)
    assert recorder.last == ["podman", "save", "--format", "oci-archive", "--output", str(destination),
                             "repo/app:1.0.0"]
    assert backend.load_command("image/out.tar") == "podman load -i image/out.tar"


# ----------------------------------------------------------------- docker

def test_docker_uses_platform_and_disables_cache():
    backend = DockerBackend()
    recorder = Recorder(backend, output='[{"Id": "sha256:bb", "RepoDigests": ["repo/app@sha256:cc"], "Size": 2}]')
    backend.resolve_base("alpine:3.22", "amd64", lambda line: None)
    assert recorder.calls[0] == ["docker", "pull", "--platform", "linux/amd64", "alpine:3.22"]

    backend.build(_context(), "repo/app:1.0.0", "arm64", lambda line: None)
    build_argv = [call for call in recorder.calls if call[1] == "build"][0]
    assert build_argv[2:4] == ["--platform", "linux/arm64"]
    assert "--no-cache" in build_argv
    # Flags Docker does not have must not leak in from the shared base.
    assert "--pull=never" not in build_argv and "--layers=false" not in build_argv


def test_docker_export_and_load_command(tmp_path):
    backend = DockerBackend()
    recorder = Recorder(backend)
    destination = tmp_path / "out.tar"
    destination.write_bytes(b"x")
    backend.export("repo/app:1.0.0", destination, lambda line: None)
    assert recorder.last == ["docker", "save", "--output", str(destination), "repo/app:1.0.0"]
    assert backend.load_command("image/out.tar") == "docker load -i image/out.tar"
    assert backend.archive_format == "docker-archive"


# ------------------------------------------------------------------ both

@pytest.mark.parametrize("factory", [PodmanBackend, DockerBackend])
def test_digest_is_read_from_either_field(factory):
    backend = factory()
    Recorder(backend, output='[{"RepoDigests": ["repo/app@sha256:dd"], "Id": "sha256:ee", "Size": 3}]')
    assert backend.resolve_base("repo/app:1", "amd64", lambda line: None) == "sha256:dd"


@pytest.mark.parametrize("factory", [PodmanBackend, DockerBackend])
def test_missing_digest_is_an_error_not_a_guess(factory):
    backend = factory()
    Recorder(backend, output='[{"Id": "sha256:ee"}]')
    with pytest.raises(BuildError):
        backend.resolve_base("repo/app:1", "amd64", lambda line: None)


@pytest.mark.parametrize("factory", [PodmanBackend, DockerBackend])
def test_build_passes_an_absolute_containerfile_path(factory):
    """Docker resolves --file against the cwd, so a bare name breaks there."""
    backend = factory()
    recorder = Recorder(backend, output='[{"Digest": "sha256:aa", "Id": "sha256:bb", "Size": 1}]')
    context = _context()
    backend.build(context, "repo/app:1.0.0", "amd64", lambda line: None)
    argv = recorder.calls[0]
    assert argv[argv.index("--file") + 1] == str(context / "Containerfile")
    assert argv[-1] == str(context)


@pytest.mark.parametrize("factory", [PodmanBackend, DockerBackend])
def test_build_refuses_a_context_without_a_containerfile(factory, tmp_path):
    with pytest.raises(BuildError):
        factory().build(tmp_path, "repo/app:1.0.0", "amd64", lambda line: None)


def _context() -> Path:
    import tempfile

    directory = Path(tempfile.mkdtemp())
    (directory / "Containerfile").write_text("FROM alpine:3.22\n")
    return directory
