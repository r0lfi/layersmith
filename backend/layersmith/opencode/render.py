"""Containerfile lines and build-context files of an opencode Shared Terminal image.

The image is an Enterprise Linux base with:

  - every system package updated at build time, then git, jq, the editors and
    archivers an agent uses, and the chosen toolchains; no sudo, no SSH server
  - opencode and ripgrep, downloaded and verified by install-tools.sh (the
    OpenShift Admin Toolkit's installer: HTTPS only, sha256 before install)
  - opencode's offline settings in the environment and /etc/opencode
  - ensure-user, which makes a host user known inside the running container
  - the host kit in /usr/local/share/opencode/host: install-host.sh and the
    files it installs, so the host side always matches the image
  - CMD container-init: the container just keeps running; users' opencode
    sessions are started next to it with `podman exec`

Nothing secret is ever written into a layer: endpoints and keys live in the
host's /etc/opencode and in each user's home.
"""

import shutil
from pathlib import Path

from layersmith.openshift import render as openshift_render

ASSET_DIR = Path(__file__).with_name("assets")
INSTALLER = openshift_render.ASSET_DIR / "build" / "install-tools.sh"
EXECUTABLE_DIRS = ("bin", "build", "libexec")
EXECUTABLE_HOST_FILES = ("install-host.sh", "opencode", "opencode-shell", "opencode-enter", "opencode-container")
LIBEXEC = "/usr/local/libexec/opencode"
SHARE = "/usr/local/share/opencode"
CHECK_PATH = f"{LIBEXEC}/layersmith-opencode-check"
LOCALE = "en_US.UTF-8"
#: The unprivileged identity the "user" check runs as, like a host user before
#: ensure-user has run.
CHECK_USER = "1000:1000"


def labels(recipe: dict) -> dict:
    return {
        "io.layersmith.opencode.profile": recipe["profile"],
        "io.layersmith.opencode.profile-version": str(recipe["profile_version"]),
        "io.layersmith.opencode.version": recipe["opencode"]["version"],
        "io.layersmith.opencode.cpu": recipe["opencode"]["cpu"],
        "io.layersmith.opencode.optional": " ".join(recipe["optional"]) or "none",
        "summary": recipe["profile_name"],
    }


def prelude_lines(recipe: dict) -> list[str]:
    """Before the package install: bring the base up to date."""
    return ["", f"# ---- LayerSmith {recipe['profile_name']} (profile {recipe['profile']} "
                f"v{recipe['profile_version']}): opencode {recipe['opencode']['version']} ----",
            "# System packages are updated at build time; every download is pinned by sha256.",
            "RUN dnf -y upgrade --refresh \\\n    && dnf clean all && rm -rf /var/cache/dnf"]


def install_lines(recipe: dict) -> list[str]:
    """After the package install: downloads, helpers, settings, host kit and the version inventory."""
    env = " \\\n    ".join(f'{key}="{value}"' for key, value in {**recipe["environment"], "LANG": LOCALE}.items())
    return [
        "",
        "# Modes come from the build context (Docker's classic builder has no COPY option for them).",
        f"COPY opencode/build/ {LIBEXEC}/",
        f"COPY opencode/downloads.tsv opencode/checks.tsv {SHARE}/",
        f"RUN {LIBEXEC}/install-tools.sh {SHARE}/downloads.tsv",
        f"COPY opencode/libexec/ {LIBEXEC}/",
        "COPY opencode/bin/ /usr/local/bin/",
        "COPY opencode/etc/opencode.json /etc/opencode/opencode.json",
        f"COPY opencode/host/ {SHARE}/host/",
        "COPY opencode/README.md /usr/share/doc/opencode-shared/README.md",
        f"ENV {env}",
        f'RUN {LIBEXEC}/finish.sh "{LOCALE}"',
        "",
        "# Keeps running; users' opencode sessions are started next to it by the host's `opencode` command.",
        f'CMD ["{LIBEXEC}/container-init"]',
    ]


def downloads_tsv(recipe: dict) -> str:
    lines = ["# url\tsha256\tmember=destination... (member - = the download itself)"]
    for item in recipe["downloads"]:
        targets = " ".join(f"{member or '-'}={dest}" for member, dest in item["files"].items())
        lines.append(f"{item['url']}\t{item['sha256']}\t{targets}")
    return "\n".join(lines) + "\n"


def checks_tsv(recipe: dict) -> str:
    return "".join("\t".join([c["tool"], c["binary"], c["expect"], *c["argv"]]) + "\n" for c in recipe["checks"])


def install_script(image_ref: str, archive: str) -> str:
    """install-host.sh with this build's image and archive as its defaults."""
    text = (ASSET_DIR / "host" / "install-host.sh").read_text(encoding="utf-8")
    return text.replace("@IMAGE@", image_ref).replace("@ARCHIVE@", archive)


def write_context(context: Path, recipe: dict, readme: str, image_ref: str, archive: str) -> list[str]:
    root = context / "opencode"
    if root.exists():
        shutil.rmtree(root)
    shutil.copytree(ASSET_DIR, root)
    shutil.copy(INSTALLER, root / "build" / "install-tools.sh")
    (root / "downloads.tsv").write_text(downloads_tsv(recipe), encoding="utf-8")
    (root / "checks.tsv").write_text(checks_tsv(recipe), encoding="utf-8")
    (root / "README.md").write_text(readme, encoding="utf-8")
    host = root / "host"
    (host / "README.md").write_text(readme, encoding="utf-8")
    shutil.copy(root / "etc" / "opencode.json", host / "opencode.json")
    (host / "install-host.sh").write_text(install_script(image_ref, archive), encoding="utf-8")
    # COPY keeps the context's modes, so they are set here: programs 0755, everything else 0644.
    for path in [root, *root.rglob("*")]:
        executable = (path.is_dir() or path.parent.name in EXECUTABLE_DIRS
                      or (path.parent.name == "host" and path.name in EXECUTABLE_HOST_FILES))
        path.chmod(0o755 if executable else 0o644)
    return sorted(str(p.relative_to(context)) for p in root.rglob("*") if p.is_file())


def check_runs() -> list[dict]:
    """The post-build checks: (id, user, argv). Each prints one result line."""
    return [{"id": "image", "user": None, "argv": [CHECK_PATH, "image"], "timeout": 10 * 60},
            {"id": "user", "user": CHECK_USER, "argv": [CHECK_PATH, "user"], "timeout": 10 * 60}]
