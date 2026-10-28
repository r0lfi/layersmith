"""Containerfile lines and build-context files of an OpenShift Admin Toolkit image.

The image is an Enterprise Linux base with:

  - every system package updated at build time, then the toolkit's packages
    (distrobox-init's dependencies included, so the first `distrobox enter`
    installs nothing and needs no network)
  - the pinned clients, downloaded and verified by install-tools.sh
  - the oc-* helpers, the shell profile and bash completion
  - no USER, no ENTRYPOINT and no service: Distrobox creates the user and runs
    its own init; `podman run` gets a login shell

Nothing secret or cluster-specific is ever written into a layer: kubeconfigs
and tokens come from the user's home at run time.
"""

import shutil
from pathlib import Path

ASSET_DIR = Path(__file__).with_name("assets")
LIBEXEC = "/usr/local/libexec/openshift-admin"
SHARE = "/usr/local/share/openshift-admin"
CHECK_PATH = f"{LIBEXEC}/layersmith-openshift-check"
#: The unprivileged identity the "user" check runs as: no passwd entry, like a
#: Distrobox user before distrobox-init has created it.
CHECK_USER = "1000:1000"


def labels(recipe: dict) -> dict:
    tools = " ".join(f"{t['id']}={t['version']}" for t in recipe["tools"] if t["enabled"] and t["kind"] == "download")
    return {
        "io.layersmith.openshift.profile": recipe["profile"],
        "io.layersmith.openshift.profile-version": str(recipe["profile_version"]),
        "io.layersmith.openshift.ocp": recipe["ocp"]["version"],
        "io.layersmith.openshift.tools": tools[:900],
        "io.layersmith.openshift.optional": " ".join(recipe["optional"]) or "none",
        "io.layersmith.openshift.distrobox": "compatible",
        "summary": recipe["profile_name"],
    }


def prelude_lines(recipe: dict) -> list[str]:
    """Before the package install: bring the base up to date."""
    return ["", f"# ---- LayerSmith {recipe['profile_name']} (profile {recipe['profile']} "
                f"v{recipe['profile_version']}): OpenShift clients {recipe['ocp']['version']} ----",
            "# System packages are updated at build time; every downloaded client is pinned by sha256.",
            "RUN dnf -y upgrade --refresh \\\n    && dnf clean all && rm -rf /var/cache/dnf"]


def install_lines(recipe: dict) -> list[str]:
    """After the package install: clients, helpers, shell setup, completion and the version inventory."""
    return [
        "",
        f"COPY --chmod=0755 openshift/build/ {LIBEXEC}/",
        f"COPY --chmod=0644 openshift/downloads.tsv openshift/checks.tsv {SHARE}/",
        f"RUN {LIBEXEC}/install-tools.sh {SHARE}/downloads.tsv",
        "COPY --chmod=0755 openshift/bin/ /usr/local/bin/",
        "COPY --chmod=0644 openshift/lib/common.sh /usr/local/lib/openshift-admin/common.sh",
        "COPY --chmod=0644 openshift/profile.d/openshift-admin.sh /etc/profile.d/openshift-admin.sh",
        "COPY --chmod=0644 openshift/completion/openshift-admin /usr/share/bash-completion/completions/openshift-admin",
        "COPY --chmod=0644 openshift/toolkit.env /etc/openshift-admin/toolkit.env",
        "COPY --chmod=0644 openshift/README.md /usr/share/doc/openshift-admin/README.md",
        f'ENV LANG="{recipe["locale"]}" TZ="{recipe["timezone"]}"',
        f'RUN {LIBEXEC}/finish.sh "{recipe["timezone"]}" "{recipe["locale"]}"',
        "",
        "# No USER and no ENTRYPOINT: Distrobox creates your user and runs its own init.",
        'CMD ["/bin/bash", "-l"]',
    ]


def downloads_tsv(recipe: dict) -> str:
    lines = ["# url\tsha256\tmember=destination... (member - = the download itself)"]
    for item in recipe["downloads"]:
        targets = " ".join(f"{member or '-'}={dest}" for member, dest in item["files"].items())
        lines.append(f"{item['url']}\t{item['sha256']}\t{targets}")
    return "\n".join(lines) + "\n"


def checks_tsv(recipe: dict) -> str:
    return "".join("\t".join([c["tool"], c["binary"], c["expect"], *c["argv"]]) + "\n" for c in recipe["checks"])


def toolkit_env(recipe: dict) -> str:
    return ("# OpenShift Admin Toolkit settings baked in at build time; the environment overrides the pattern.\n"
            f": \"${{OPENSHIFT_ADMIN_PROD_PATTERN:={recipe['prod_pattern']}}}\"\n"
            "export OPENSHIFT_ADMIN_PROD_PATTERN\n"
            f"OPENSHIFT_ADMIN_PROFILE={recipe['profile']}\n"
            f"OPENSHIFT_ADMIN_OCP_VERSION={recipe['ocp']['version']}\n")


def write_context(context: Path, recipe: dict, readme: str) -> list[str]:
    root = context / "openshift"
    if root.exists():
        shutil.rmtree(root)
    shutil.copytree(ASSET_DIR, root)
    (root / "downloads.tsv").write_text(downloads_tsv(recipe), encoding="utf-8")
    (root / "checks.tsv").write_text(checks_tsv(recipe), encoding="utf-8")
    (root / "toolkit.env").write_text(toolkit_env(recipe), encoding="utf-8")
    (root / "README.md").write_text(readme, encoding="utf-8")
    return sorted(str(p.relative_to(context)) for p in root.rglob("*") if p.is_file())


def check_runs() -> list[dict]:
    """The post-build checks: (id, user, argv). Each prints one result line."""
    return [{"id": "image", "user": None, "argv": [CHECK_PATH, "image"], "timeout": 10 * 60},
            {"id": "user", "user": CHECK_USER, "argv": [CHECK_PATH, "user"], "timeout": 10 * 60}]
