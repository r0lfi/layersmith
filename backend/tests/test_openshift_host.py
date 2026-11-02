"""The openshift-admin host command: install-host.sh, the command itself and assemble-image.sh --install.

Podman, systemctl and loginctl are replaced by fakes that record their
arguments, so the tests cover what the scripts ask for without a container
runtime or a user session.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from layersmith.openshift import host
from tests.test_api import client, run_pending_build  # noqa: F401 - fixture
from tests.test_openshift import OpenShiftBackend, create_and_build
from tests.test_splitting import assemble, download_all, split

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")

FAKE_PODMAN = """#!/bin/bash
echo "podman $*" >> "$FAKE_LOG"
# A container is "running" from podman run until podman rm, like the real one.
state=${FAKE_LOG%/*}/running
case "$1 $2" in
  "container inspect") if [[ -e $state ]]; then echo running; else echo absent; exit 1; fi ;;
  "image exists") [[ -z ${FAKE_NO_IMAGE:-} ]] ;;
  "run -d" | "run --rm") touch "$state" ;;
  "rm -f") rm -f "$state" ;;
  *) exit 0 ;;
esac
"""
FAKE_SYSTEMCTL = """#!/bin/bash
echo "systemctl $*" >> "$FAKE_LOG"
[[ -z ${FAKE_NO_SYSTEMD:-} ]] || exit 1
# The service's ExecStart runs the container.
[[ $* == "--user restart openshift-admin.service" || $* == "--user start openshift-admin.service" ]] \\
  && touch "${FAKE_LOG%/*}/running"
exit 0
"""
FAKE_LOGINCTL = """#!/bin/bash
echo "loginctl $*" >> "$FAKE_LOG"
[[ $1 == show-user ]] && echo no
exit 0
"""


@pytest.fixture
def fake_host(tmp_path):
    bin_dir, home = tmp_path / "fakebin", tmp_path / "home"
    bin_dir.mkdir()
    home.mkdir()
    for name, body in (("podman", FAKE_PODMAN), ("systemctl", FAKE_SYSTEMCTL), ("loginctl", FAKE_LOGINCTL)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    log = tmp_path / "calls.log"
    env = {"HOME": str(home), "PATH": f"{bin_dir}:{home}/.local/bin:/usr/bin:/bin", "FAKE_LOG": str(log),
           "TERM": "dumb"}

    def run(argv, extra=None, **kw):
        return subprocess.run(argv, capture_output=True, text=True, env={**env, **(extra or {})}, timeout=60, **kw)

    def calls():
        return log.read_text().splitlines() if log.exists() else []

    return {"home": home, "run": run, "calls": calls, "log": log, "tmp": tmp_path}


def write_installer(tmp_path, image="localhost/openshift-admin:1.0.1"):
    path = tmp_path / host.INSTALLER
    path.write_text(host.installer(image, "openshift-admin-1.0.1.tar"))
    return path


def test_installer_embeds_the_command_and_the_unit_and_parses(tmp_path):
    text = host.installer("localhost/openshift-admin:1.0.1", "openshift-admin-1.0.1.tar")
    assert "@COMMAND@" not in text and "@UNIT@" not in text and "@IMAGE@" not in text and "@ARCHIVE@" not in text
    assert "IMAGE=localhost/openshift-admin:1.0.1" in text and "--archive openshift-admin-1.0.1.tar" in text
    assert "ExecStart=%h/.local/bin/openshift-admin --serve" in text
    code = [line for line in text.splitlines() if not line.lstrip().startswith(("#", "warn "))]
    assert not [line for line in code if "sudo" in line]  # rootless: nothing runs through sudo
    subprocess.run([BASH, "-n", str(write_installer(tmp_path))], check=True)
    subprocess.run([BASH, "-n", str(host.HOST_DIR / host.COMMAND)], check=True)


def test_install_loads_tags_and_starts_the_user_service(fake_host):
    installer = write_installer(fake_host["tmp"])
    result = fake_host["run"]([BASH, str(installer), "--archive", "openshift-admin-1.0.1.tar"],
                              cwd=fake_host["tmp"])
    (fake_host["tmp"] / "openshift-admin-1.0.1.tar").write_bytes(b"")
    assert result.returncode != 0 and "cannot read" in result.stderr  # the archive did not exist yet
    result = fake_host["run"]([BASH, str(installer), "--archive", "openshift-admin-1.0.1.tar"],
                              cwd=fake_host["tmp"])
    assert result.returncode == 0, result.stderr
    calls = fake_host["calls"]()
    assert "podman load -i openshift-admin-1.0.1.tar" in calls
    assert "podman tag localhost/openshift-admin:1.0.1 localhost/openshift-admin:current" in calls
    assert "systemctl --user enable openshift-admin.service" in calls
    assert "systemctl --user restart openshift-admin.service" in calls
    assert any(c.startswith("loginctl enable-linger") for c in calls)
    home = fake_host["home"]
    command = home / ".local/bin/openshift-admin"
    assert command.read_text() == (host.HOST_DIR / host.COMMAND).read_text()
    assert os.access(command, os.X_OK) and os.readlink(home / ".local/bin/openshift_admin") == "openshift-admin"
    unit = home / ".config/systemd/user/openshift-admin.service"
    assert unit.read_text() == (host.HOST_DIR / "openshift-admin.service").read_text()
    assert (home / ".kube").stat().st_mode & 0o777 == 0o700
    assert "Type openshift-admin" in result.stdout


def test_a_new_version_moves_the_tag_and_recreates_the_container(fake_host):
    installer = write_installer(fake_host["tmp"])
    assert fake_host["run"]([BASH, str(installer)]).returncode == 0
    fake_host["log"].unlink()
    result = fake_host["run"]([BASH, str(installer), "--image", "localhost/openshift-admin:1.0.2"])
    assert result.returncode == 0, result.stderr
    calls = fake_host["calls"]()
    assert "podman tag localhost/openshift-admin:1.0.2 localhost/openshift-admin:current" in calls
    assert calls.index("podman rm -f -t 5 openshift-admin") < calls.index("systemctl --user restart openshift-admin.service")
    assert not any("volume rm" in c or "rmi" in c for c in calls)  # home volume and old images are kept


def test_without_a_user_session_the_command_starts_the_container_itself(fake_host):
    installer = write_installer(fake_host["tmp"])
    result = fake_host["run"]([BASH, str(installer)], {"FAKE_NO_SYSTEMD": "1"})
    assert result.returncode == 0, result.stderr
    assert "no systemd user session" in result.stderr
    assert not (fake_host["home"] / ".config/systemd/user/openshift-admin.service").exists()
    run = [c for c in fake_host["calls"]() if c.startswith("podman run")]
    assert run and run[0].startswith("podman run -d --replace --name openshift-admin")
    assert "--userns=keep-id" in run[0] and f"{fake_host['home']}/.kube:/home/admin/.kube:z" in run[0]
    assert "openshift-admin-home:/home/admin:U" in run[0] and run[0].endswith("localhost/openshift-admin:current sleep infinity")


def test_a_missing_image_is_refused(fake_host):
    installer = write_installer(fake_host["tmp"])
    result = fake_host["run"]([BASH, str(installer)], {"FAKE_NO_IMAGE": "1"})
    assert result.returncode == 1 and "is not loaded" in result.stderr


def test_command_runs_a_shell_or_one_command_through_a_login_shell(fake_host):
    command = str(host.HOST_DIR / host.COMMAND)
    (fake_host["tmp"] / "running").touch()
    assert fake_host["run"]([BASH, command, "oc", "get", "pods", "-n", "my ns"], stdin=subprocess.DEVNULL).returncode == 0
    assert fake_host["run"]([BASH, command], stdin=subprocess.DEVNULL).returncode == 0
    execs = [c for c in fake_host["calls"]() if c.startswith("podman exec")]
    assert execs[0] == ('podman exec -i -e TERM=dumb openshift-admin /bin/bash -lc "$@" openshift-admin '
                        "oc get pods -n my ns")
    assert execs[1] == "podman exec -i -e TERM=dumb openshift-admin /bin/bash -l"
    assert fake_host["run"]([BASH, command, "--bogus"]).returncode == 64
    help_text = fake_host["run"]([BASH, command, "--help"]).stdout
    assert "openshift-admin oc get nodes" in help_text and "set -euo" not in help_text


def test_uninstall_keeps_images_volume_and_kubeconfigs(fake_host):
    installer = write_installer(fake_host["tmp"])
    assert fake_host["run"]([BASH, str(installer)]).returncode == 0
    result = fake_host["run"]([BASH, str(installer), "--uninstall"])
    assert result.returncode == 0, result.stderr
    home = fake_host["home"]
    assert not (home / ".local/bin/openshift-admin").exists() and not (home / ".local/bin/openshift_admin").is_symlink()
    assert not (home / ".config/systemd/user/openshift-admin.service").exists() and (home / ".kube").is_dir()
    assert not any("volume rm" in c or "rmi" in c for c in fake_host["calls"]())


# ------------------------------------------------------------ build page and assemble-image.sh

@pytest.fixture
def os_client(client):  # noqa: F811
    client.state["build_service"].backend = OpenShiftBackend()
    return client


def _small_export(client, build_id, size=2_500_000):
    import hashlib

    from layersmith.models import Build

    client.state["settings"].split_part_bytes = 1_000_000
    with client.state["session_factory"]() as session:
        build = session.get(Build, build_id)
        data = os.urandom(size)
        Path(build.export_path).write_bytes(data)
        build.export_size, build.export_sha256 = size, hashlib.sha256(data).hexdigest()
        session.commit()


def test_build_page_offers_install_host_sh(os_client):
    _, build_id = create_and_build(os_client)
    shift = os_client.get(f"/api/builds/{build_id}").json()["openshift"]
    assert shift["commands"]["install"] == "bash install-host.sh --archive openshift-admin-1.0.0.tar"
    assert shift["commands"]["assemble_install"] == "./assemble-image.sh --install"
    assert "install-host.sh" in {f["name"] for f in shift["downloads"]}
    response = os_client.get(f"/api/builds/{build_id}/openshift/files/install-host.sh")
    assert response.status_code == 200 and "IMAGE=localhost/openshift-admin:1.0.0" in response.text
    readme = os_client.get(f"/api/builds/{build_id}/openshift/files/README.md").text
    assert "bash install-host.sh --archive openshift-admin-1.0.0.tar" in readme and "openshift_admin" in readme


def test_assemble_install_restores_loads_and_installs(os_client, fake_host):
    _, build_id = create_and_build(os_client)
    _small_export(os_client, build_id)
    directory = download_all(os_client, build_id, split(os_client, build_id), fake_host["tmp"] / "transfer")
    help_text = assemble(directory, "--help").stdout
    assert "--install" in help_text
    env = {**os.environ, "HOME": str(fake_host["home"]), "FAKE_LOG": str(fake_host["log"]),
           "PATH": f"{fake_host['tmp']}/fakebin:{fake_host['home']}/.local/bin:/usr/bin:/bin"}
    result = assemble(directory, "--install", env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = fake_host["calls"]()
    assert calls[0] == "podman load -i openshift-admin-1.0.0.tar"
    assert "podman tag localhost/openshift-admin:1.0.0 localhost/openshift-admin:current" in calls
    assert (fake_host["home"] / ".local/bin/openshift-admin").read_text() == (host.HOST_DIR / host.COMMAND).read_text()
    assert "Type openshift-admin" in result.stdout
    # --load alone points at --install.
    assert "assemble-image.sh --install" in assemble(directory, "--load", env=env).stdout


def test_other_split_archives_have_no_install(client, tmp_path):  # noqa: F811
    from tests.test_splitting import make_ready_build

    build_id = make_ready_build(client, 10)
    directory = download_all(client, build_id, split(client, build_id), tmp_path / "transfer")
    assert "--install" not in assemble(directory, "--help").stdout
    assert assemble(directory, "--install").returncode == 1
