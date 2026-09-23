"""End-to-end build against a real container runtime (Podman and/or Docker).

Skipped unless LAYERSMITH_SMOKE=1, and each runtime is skipped unless its
binary is on PATH, so this runs what the host can actually run. It pulls a
base image from the network. Everything else in the suite uses a fake backend.

    LAYERSMITH_SMOKE=1 pytest tests/test_podman_smoke.py -v
"""

import os
import shutil
import subprocess
import tarfile

import pytest

from layersmith import config
from layersmith.backends.docker import DockerBackend
from layersmith.backends.podman import PodmanBackend
from layersmith.core.spec import validate_spec
from layersmith.models import Build, Project, create_all, make_engine, make_session_factory, next_build_number
from layersmith.services.builds import BuildService, sha256_file

pytestmark = pytest.mark.skipif(not os.environ.get("LAYERSMITH_SMOKE"), reason="needs LAYERSMITH_SMOKE=1")

RUNTIMES = [
    pytest.param(
        factory, id=name,
        marks=pytest.mark.skipif(not shutil.which(name), reason=f"{name} is not installed"),
    )
    for name, factory in (("podman", PodmanBackend), ("docker", DockerBackend))
]

IMAGE = "layersmith-smoke/alpine-tools"
VERSION = "1.0.0"


@pytest.fixture(params=RUNTIMES)
def service(request, tmp_path):
    settings = config.reset_for_tests(
        data_dir=tmp_path, image_dir=tmp_path / "images", build_dir=tmp_path / "builds",
        upload_dir=tmp_path / "uploads", log_dir=tmp_path / "logs", tmp_dir=tmp_path / "tmp",
        database_url=f"sqlite:///{tmp_path}/smoke.db",
    )
    settings.prepare()
    engine = make_engine(settings.database_url)
    create_all(engine)
    factory = make_session_factory(engine)
    backend = request.param()
    yield BuildService(factory, backend, settings=settings), factory
    subprocess.run([backend.binary, "rmi", "-f", f"{IMAGE}:{VERSION}"], capture_output=True)


def test_runtime_is_usable(service):
    build_service, _ = service
    available, detail = build_service.backend.available()
    assert available, detail


def test_real_build_export_and_airgap_bundle(service, tmp_path):
    build_service, factory = service
    # Alpine keeps the pull small; the packages exercise real apk resolution.
    spec = validate_spec({"base": {"distribution": "Alpine", "version": "3.21"},
                          "packages": ["git", "dns-tools", "jq"],
                          "env": [{"name": "LAYERSMITH_SMOKE", "value": "1"}],
                          "scripts": {"post_install": "echo built-by-layersmith > /etc/layersmith-smoke"}})
    with factory() as session:
        project = Project(name="alpine-tools", repository=IMAGE, spec=spec, mode="gui",
                          base_image=spec["base"]["source"])
        session.add(project)
        session.flush()
        build = Build(project_id=project.id, number=next_build_number(session), version=VERSION, mode="gui",
                      spec=spec, base_image=spec["base"]["source"], architecture="amd64",
                      image_ref=f"{IMAGE}:{VERSION}")
        session.add(build)
        session.commit()
        build_id = build.id

    build_service.run(build_id)

    with factory() as session:
        built = session.get(Build, build_id)
        assert built.status == "ready", built.error
        assert built.base_digest and built.base_digest.startswith("sha256:")
        assert built.image_id and built.image_size and built.image_size > 0
        assert "bind-tools" in built.packages  # capability resolved for apk
        assert built.export_sha256 == sha256_file(__import__("pathlib").Path(built.export_path))
        reference = built.image_ref

    # The image really contains what was asked for.
    for command, expected in [
        (["sh", "-c", "command -v git && command -v dig && command -v jq"], "git"),
        (["cat", "/etc/layersmith-smoke"], "built-by-layersmith"),
        (["sh", "-c", "echo $LAYERSMITH_SMOKE"], "1"),
    ]:
        done = subprocess.run([build_service.backend.binary, "run", "--rm", reference, *command],
                              capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr
        assert expected in done.stdout

    bundle = build_service.airgap_bundle(build_id)
    with tarfile.open(bundle) as archive:
        names = archive.getnames()
    assert any(name.endswith(".tar") for name in names)
    assert any(name.endswith("SHA256SUMS") for name in names)

    # The exported archive can be loaded back, which is what an air-gap
    # recipient actually does.
    with factory() as session:
        export_path = session.get(Build, build_id).export_path
    binary = build_service.backend.binary
    subprocess.run([binary, "rmi", "-f", reference], capture_output=True)
    loaded = subprocess.run([binary, "load", "-i", export_path], capture_output=True, text=True, timeout=300)
    assert loaded.returncode == 0, loaded.stderr
