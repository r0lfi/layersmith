"""The openshift-admin command on the host: an always-running container you enter by name.

install-host.sh is one self-contained script (the command and the systemd
user service are inside it), served on the build page and run by
`assemble-image.sh --install` for an OpenShift Admin Toolkit build. It is
rootless and per user: no sudo, no root service, nothing shared between users,
because kubeconfigs and oc login tokens are personal.
"""

import shlex
from pathlib import Path

HOST_DIR = Path(__file__).with_name("host")
INSTALLER = "install-host.sh"
COMMAND = "openshift-admin"
CURRENT = "localhost/openshift-admin:current"


def _asset(name: str) -> str:
    return (HOST_DIR / name).read_text(encoding="utf-8").rstrip("\n")


def installer(image_ref: str, archive: str = "<archive>.tar") -> str:
    """install-host.sh for one image, with the command and the unit embedded."""
    return (_asset(INSTALLER)
            .replace("@COMMAND@", _asset(COMMAND))
            .replace("@UNIT@", _asset(f"{COMMAND}.service"))
            .replace("IMAGE=@IMAGE@", f"IMAGE={shlex.quote(image_ref)}")
            .replace("@IMAGE@", image_ref)
            .replace("@ARCHIVE@", archive)) + "\n"
