#!/usr/bin/env python3
"""Print verified catalogue pins for new OpenShift client releases.

    python backend/scripts/pin_openshift.py 4.22.20 4.23.1

For each version, downloads sha256sum.txt and its signature from
mirror.openshift.com, verifies the signature with Red Hat release key 2
(fingerprint below, fetched from access.redhat.com and checked), then
downloads the el9 oc, oc-mirror and opm archives and checks them against the
signed list. Prints the _ocp(...) lines and the OCP_RELEASES entry to paste
into layersmith/openshift/catalog.py, including the Kubernetes version read
from kubectl itself. Needs network access, gpg and curl; nothing here runs at
build time.
"""

import hashlib
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

MIRROR = "https://mirror.openshift.com/pub/openshift-v4/x86_64/clients/ocp"
KEY_URL = "https://access.redhat.com/security/data/fd431d51.txt"
FINGERPRINT = "567E347AD0044ADE55BA8A5F199E2F91FD431D51"


def fetch(url: str, path: Path) -> Path:
    subprocess.run(["curl", "--proto", "=https", "-fsSL", "-o", str(path), url], check=True)
    return path


def main(versions: list[str]) -> None:
    if not versions:
        sys.exit(__doc__)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        home = work / "gnupg"
        home.mkdir(mode=0o700)
        gpg = ["gpg", "--homedir", str(home), "--batch"]
        subprocess.run([*gpg, "--import", str(fetch(KEY_URL, work / "key.asc"))], check=True, capture_output=True)
        listing = subprocess.run([*gpg, "--with-colons", "--fingerprint"], check=True, capture_output=True,
                                 text=True).stdout
        if f"fpr:::::::::{FINGERPRINT}:" not in listing:
            sys.exit("The downloaded Red Hat key does not have the expected fingerprint")
        for version in versions:
            sums_file = fetch(f"{MIRROR}/{version}/sha256sum.txt.gpg", work / f"{version}.gpg")
            # The signed file wraps sha256sum.txt; status lines go to stderr, the content to a file.
            status =subprocess.run([*gpg, "--status-fd", "2", "--output", str(work / f"{version}.txt"),
                                     "--decrypt", str(sums_file)], capture_output=True, text=True)
            if status.returncode != 0 or f"VALIDSIG {FINGERPRINT}" not in status.stderr:
                sys.exit(f"{version}: sha256sum.txt is not signed by Red Hat release key 2")
            sums = {line.split()[-1]: line.split()[0]
                    for line in (work / f"{version}.txt").read_text().splitlines() if line.strip()}
            names = {"client": f"openshift-client-linux-amd64-rhel9-{version}.tar.gz",
                     "mirror": "oc-mirror.rhel9.tar.gz", "opm": f"opm-linux-rhel9-{version}.tar.gz"}
            pins = {}
            for key, name in names.items():
                path = fetch(f"{MIRROR}/{version}/{name}", work / name)
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if sums.get(name) != digest:
                    sys.exit(f"{version}: {name} does not match the signed checksum list")
                pins[key] = digest
            with tarfile.open(work / names["client"]) as archive:
                archive.extract("kubectl", work / version, filter="data")
            kubernetes = subprocess.run([str(work / version / "kubectl"), "version", "--client"], check=True,
                                        capture_output=True, text=True).stdout.split("Client Version: v", 1)[1].split()[0]
            print(f'    "{version}": _ocp("{version}", "{pins["client"]}",\n'
                  f'                    "{pins["mirror"]}",\n'
                  f'                    "{pins["opm"]}"),')
            print(f'    # OCP_RELEASES: "{version}": {{"channel": "<channel>", "kubernetes": "{kubernetes}"}},')


if __name__ == "__main__":
    main(sys.argv[1:])
