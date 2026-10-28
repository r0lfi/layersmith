"""OpenShift Admin Toolkit: verified client downloads, packages and options.

Every download below is pinned by sha256. Each pin was checked on the date in
VERIFIED against the publisher's own checksum file:

  Red Hat clients (oc, kubectl, oc-mirror, opm)
      mirror.openshift.com/pub/openshift-v4/x86_64/clients/ocp/<version>/
      sha256sum.txt, whose signature (sha256sum.txt.gpg) verifies with Red Hat
      release key 2, fingerprint 567E 347A D004 4ADE 55BA 8A5F 199E 2F91 FD43 1D51.
  Helm (Red Hat build)
      mirror.openshift.com/pub/cgw/helm/<version>/sha256sum.txt
  operator-sdk, kustomize, k9s, stern, kubectx/kubens, yq
      the checksum file attached to the project's GitHub release.

The build downloads each file again and refuses it unless it has exactly the
pinned checksum, so a moved tag or a replaced file fails the build instead of
landing in the image. No version here is "latest": a new version is added by
running backend/scripts/pin_openshift.py and pasting its output.

The el9 ("rhel9") builds of the Red Hat clients are used: they are
dynamically linked against glibc 2.34, which AlmaLinux, Rocky Linux and
CentOS Stream 9 and 10 all provide.
"""

from layersmith.core import catalog as core_catalog

VERIFIED = "2026-10-28"

PROFILE = {
    "id": "openshift-admin",
    "name": "OpenShift Admin Toolkit",
    "version": 1,
    "category": "DevOps / Kubernetes / OpenShift",
    "description": "Complete containerized administration environment for Red Hat OpenShift, Kubernetes, "
                   "OpenShift AI and GPU infrastructure, optimized for Podman and Distrobox.",
}

CATEGORY = {
    "id": "openshift",
    "name": PROFILE["name"],
    "intro": PROFILE["description"],
}

SOURCES = {
    "ocp-clients": "https://mirror.openshift.com/pub/openshift-v4/x86_64/clients/ocp/",
    "redhat-keys": "https://access.redhat.com/security/team/key",
    "version-skew": "https://kubernetes.io/releases/version-skew-policy/#kubectl",
    "oc-docs": "https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/cli_tools/"
               "openshift-cli-oc",
    "distrobox": "https://distrobox.it/",
}

ARCHITECTURE = "amd64"

# ---------------------------------------------------------------- bases

#: Enterprise Linux bases the toolkit is tested on. The el9 client builds need
#: glibc >= 2.34, which rules out EL8.
BASES = {
    f"{distribution} {version}": {"distribution": distribution, "version": version, "el": version,
                                  "source": core_catalog.DISTROS[distribution]["versions"][version]}
    for distribution in ("AlmaLinux", "Rocky Linux", "CentOS Stream")
    for version in ("10", "9")
}
DEFAULT_BASE = "AlmaLinux 10"

# ---------------------------------------------------------------- clients

RH = "https://mirror.openshift.com/pub/openshift-v4/x86_64/clients/ocp"
GH = "https://github.com"

#: OpenShift client releases. channel = the update channel whose head this
#: release was on VERIFIED; kubernetes = what `kubectl version --client` says.
OCP_RELEASES = {
    "4.22.18": {"channel": "stable-4.22", "kubernetes": "1.35.2"},
    "4.22.19": {"channel": "fast-4.22", "kubernetes": "1.35.2"},
    "4.21.37": {"channel": "stable-4.21", "kubernetes": "1.34.1"},
}
DEFAULT_OCP = "4.22.18"

_RH_VERIFY = "Red Hat sha256sum.txt, signed with Red Hat release key 2"


def _ocp(version, client, mirror, opm):
    return {
        "oc": {"url": f"{RH}/{version}/openshift-client-linux-amd64-rhel9-{version}.tar.gz", "sha256": client,
               "files": {"oc": "/usr/local/bin/oc", "kubectl": "/usr/local/bin/kubectl"}},
        "oc-mirror": {"url": f"{RH}/{version}/oc-mirror.rhel9.tar.gz", "sha256": mirror,
                      "files": {"oc-mirror": "/usr/local/bin/oc-mirror"}},
        "opm": {"url": f"{RH}/{version}/opm-linux-rhel9-{version}.tar.gz", "sha256": opm,
                "files": {"opm-rhel9": "/usr/local/bin/opm"}},
    }


_OCP_PINS = {
    "4.22.18": _ocp("4.22.18", "3255cddb8b32800ec3aa2a414204e7e284b7cbfeda0e8e4348a31ba0ee286c4e",
                    "23a9cb577b7eba119728dddead6de9fd33b76accb1f0cad2a2556a025e225e4a",
                    "3205c727b7cd5984323e098d5be61d3c403a9b69b0d14839420ca12d4d80d068"),
    "4.22.19": _ocp("4.22.19", "58d2306368d3a3e4cc12af2a1460e781043955937083db071df44e78a3a45eb4",
                    "24c2cc27c108ab1f3c6ec0bd8dfd4d2c2433c7325b8c8b82dc14f81a70afb30d",
                    "ed77e041b7d65d30e1da96265bd2c54e8100e8e53b8e9df0db9ed382c0402bff"),
    "4.21.37": _ocp("4.21.37", "bd76e87bfacd72b515d59d2985b25897c60927c8ef4cb23e5e7391006bcec638",
                    "98378956e13d87b8e485d1ab54396a7df223c81b4adbc5d994d801f9364f9a7b",
                    "ac7dfb23a54ecb23f21ecf0a705ded4c5ac52aec675efb12cb361cbee5bfe047"),
}


def _gh(repo, tag, asset):
    return f"{GH}/{repo}/releases/download/{tag}/{asset}"


#: Downloaded tools. "versions" maps a version to its download; "files" maps an
#: archive member (or None for a bare binary) to where it is installed.
#: "check" is the command whose output must contain "expect" (the version as
#: that tool prints it) - the post-build check runs exactly this.
TOOLS = {
    "oc": {
        "label": "oc + kubectl", "purpose": "OpenShift CLI and the Kubernetes CLI from the same Red Hat client package",
        "group": "OpenShift", "required": True, "follows_ocp": True, "verify": _RH_VERIFY,
        "versions": {v: pins["oc"] for v, pins in _OCP_PINS.items()},
        "check": [{"binary": "oc", "argv": ["oc", "version", "--client"], "expect": "Client Version: {version}"},
                  {"binary": "kubectl", "argv": ["kubectl", "version", "--client"],
                   "expect": "Client Version: v{kubernetes}"}],
    },
    "oc-mirror": {
        "label": "oc-mirror", "purpose": "Mirror OpenShift releases and operator catalogs (disconnected clusters)",
        "group": "OpenShift", "follows_ocp": True, "verify": _RH_VERIFY,
        "versions": {v: pins["oc-mirror"] for v, pins in _OCP_PINS.items()},
        # oc-mirror reports the release branch (4.22.0-<build>), not the z-stream.
        "check": [{"binary": "oc-mirror", "argv": ["oc-mirror", "version", "--output=json"],
                   "expect": '"gitVersion": "{minor}.0-'}],
    },
    "opm": {
        "label": "opm", "purpose": "Build, inspect and prune operator catalogs (file-based catalogs)",
        "group": "OpenShift", "follows_ocp": True, "verify": _RH_VERIFY,
        "versions": {v: pins["opm"] for v, pins in _OCP_PINS.items()},
        # opm reports the commit it was built from, not a release number.
        "check": [{"binary": "opm", "argv": ["opm", "version"], "expect": 'OpmVersion:"'}],
    },
    "operator-sdk": {
        "label": "operator-sdk", "purpose": "Develop, bundle and scorecard-test operators",
        "group": "OpenShift", "verify": "GitHub release checksums.txt (operator-framework/operator-sdk)",
        "default_version": "1.42.3",
        "versions": {
            "1.42.3": {"url": _gh("operator-framework/operator-sdk", "v1.42.3", "operator-sdk_linux_amd64"),
                       "sha256": "887a3bb0d63ccc4ca47a522d0c8ffac56d9d5246f6a2bd886b4ed23eb2e2672f",
                       "files": {None: "/usr/local/bin/operator-sdk"}},
            "1.41.1": {"url": _gh("operator-framework/operator-sdk", "v1.41.1", "operator-sdk_linux_amd64"),
                       "sha256": "348284cbd5298f70e2b0a01f9f86820a3149aa6e7e19272e886a9d5769c7fb69",
                       "files": {None: "/usr/local/bin/operator-sdk"}},
        },
        "check": [{"binary": "operator-sdk", "argv": ["operator-sdk", "version"], "expect": '"v{version}"'}],
    },
    "helm": {
        "label": "helm", "purpose": "Helm package manager (Red Hat build)", "group": "Kubernetes",
        "verify": "Red Hat sha256sum.txt (mirror.openshift.com/pub/cgw/helm)", "default_version": "3.22.0",
        "versions": {
            "3.22.0": {"url": "https://mirror.openshift.com/pub/cgw/helm/3.22.0-90/helm-linux-amd64.tar.gz",
                       "sha256": "4ee38ef9f0f7281d7adf5fb62c0d325ec4112dac203c41debbfca32a48aba59b",
                       "files": {"helm-linux-amd64": "/usr/local/bin/helm"}},
            "4.3.0": {"url": "https://mirror.openshift.com/pub/cgw/helm/4.3.0/helm-linux-amd64.tar.gz",
                      "sha256": "469dc2b06cf798a0a75a7c202adcdee29767ea5ebe7ed2f14d4f6469ee8006f5",
                      "files": {"helm-linux-amd64": "/usr/local/bin/helm"}},
        },
        "check": [{"binary": "helm", "argv": ["helm", "version", "--short"], "expect": "v{version}"}],
    },
    "kustomize": {
        "label": "kustomize", "purpose": "Kubernetes configuration management (oc kustomize is built in as well)",
        "group": "Kubernetes", "verify": "GitHub release checksums.txt (kubernetes-sigs/kustomize)",
        "default_version": "5.8.3",
        "versions": {
            v: {"url": _gh("kubernetes-sigs/kustomize", f"kustomize%2Fv{v}", f"kustomize_v{v}_linux_amd64.tar.gz"),
                "sha256": sha, "files": {"kustomize": "/usr/local/bin/kustomize"}}
            for v, sha in (("5.8.3", "cb9e31198d3f63b44848bd0afc4c8efc56e6cfef9c93a4a39a211940e6decd61"),
                           ("5.7.1", "ea375e7372f9aa029129d4b2d16c66b7750b7f1213c4f66f910d981c895818d8"))
        },
        "check": [{"binary": "kustomize", "argv": ["kustomize", "version"], "expect": "v{version}"}],
    },
    "k9s": {
        "label": "k9s", "purpose": "Kubernetes terminal UI", "group": "Kubernetes",
        "verify": "GitHub release checksums.sha256 (derailed/k9s)", "default_version": "0.51.0",
        "versions": {
            v: {"url": _gh("derailed/k9s", f"v{v}", "k9s_Linux_amd64.tar.gz"), "sha256": sha,
                "files": {"k9s": "/usr/local/bin/k9s"}}
            for v, sha in (("0.51.0", "c3752ad51a5a4015a113819c4eeb6e55a4d0e4b8e652494797532f6fc8161dd7"),
                           ("0.50.18", "0b697ed4aa80997f7de4deeed6f1fba73df191b28bf691b1f28d2f45fa2a9e9b"))
        },
        "check": [{"binary": "k9s", "argv": ["k9s", "version", "--short"], "expect": "v{version}"}],
    },
    "stern": {
        "label": "stern", "purpose": "Follow logs from many pods and containers at once", "group": "Kubernetes",
        "verify": "GitHub release checksums.txt (stern/stern)", "default_version": "1.35.0",
        "versions": {
            v: {"url": _gh("stern/stern", f"v{v}", f"stern_{v}_linux_amd64.tar.gz"), "sha256": sha,
                "files": {"stern": "/usr/local/bin/stern"}}
            for v, sha in (("1.35.0", "03729b01a8fcfab25f6ea53389bd38bc37400ff86d90cd2dcda541f248cf843e"),
                           ("1.34.0", "7754adfa653939240f7d20fff4ada9b69cda40c9e70732301f67bb8045f1ef3e"))
        },
        "check": [{"binary": "stern", "argv": ["stern", "--version"], "expect": "version: {version}"}],
    },
    "kubectx": {
        "label": "kubectx + kubens", "purpose": "Switch Kubernetes context and namespace", "group": "Kubernetes",
        "verify": "GitHub release checksums.txt (ahmetb/kubectx)", "default_version": "0.11.0",
        # Two archives per version: kubectx and kubens are released separately.
        "versions": {
            "0.11.0": [
                {"url": _gh("ahmetb/kubectx", "v0.11.0", "kubectx_v0.11.0_linux_x86_64.tar.gz"),
                 "sha256": "08e031c54fbffb3f100e904e4eae94bba2730fedf4869921fda79e4d7a8f5d4c",
                 "files": {"kubectx": "/usr/local/bin/kubectx"}},
                {"url": _gh("ahmetb/kubectx", "v0.11.0", "kubens_v0.11.0_linux_x86_64.tar.gz"),
                 "sha256": "326c021c7b35468ed9a187b361198d0f22ae32828139c65eb6670c0d8301cc09",
                 "files": {"kubens": "/usr/local/bin/kubens"}}],
            "0.10.2": [
                {"url": _gh("ahmetb/kubectx", "v0.10.2", "kubectx_v0.10.2_linux_x86_64.tar.gz"),
                 "sha256": "91ac5ef510763cfa78953bd377c90c68cb1f8c30eb2a5ec46196cc72e8c0aa09",
                 "files": {"kubectx": "/usr/local/bin/kubectx"}},
                {"url": _gh("ahmetb/kubectx", "v0.10.2", "kubens_v0.10.2_linux_x86_64.tar.gz"),
                 "sha256": "1cc96c6a6b1f7fb0b36964b92e5953d1f79b0bcbe6e998a800ce84ed82fe9aca",
                 "files": {"kubens": "/usr/local/bin/kubens"}}],
        },
        "check": [{"binary": "kubectx", "argv": ["kubectx", "--version"], "expect": "{version}"},
                  {"binary": "kubens", "argv": ["kubens", "--version"], "expect": "{version}"}],
    },
    "yq": {
        "label": "yq", "purpose": "YAML processor (mikefarah/yq)", "group": "Kubernetes",
        "verify": "GitHub release checksums-bsd (mikefarah/yq)", "default_version": "4.54.1",
        "versions": {
            v: {"url": _gh("mikefarah/yq", f"v{v}", "yq_linux_amd64.tar.gz"), "sha256": sha,
                "files": {"./yq_linux_amd64": "/usr/local/bin/yq"}}
            for v, sha in (("4.54.1", "e68a456f90c577af3fe4960184b3a3cf5c461e0348407c10f107da3a5fec8972"),
                           ("4.53.6", "38b907b21b1b04327fb9481c595331d925a67c6ee1aabd0ef419d0b7d12dfb3d"))
        },
        "check": [{"binary": "yq", "argv": ["yq", "--version"], "expect": "version v{version}"}],
    },
}

# ---------------------------------------------------------------- packages

#: Always installed. The first line is what `distrobox enter` would otherwise
#: install on first start (which needs network access the target may not
#: have); the rest is the admin baseline from the profile.
BASE_PACKAGES = [
    # distrobox-init's dependency list on dnf systems, minus graphics drivers. curl and
    # coreutils are not listed: every EL base has them already, as curl-minimal and
    # coreutils-single on some, and asking for the full packages conflicts with those.
    "bash", "bash-completion", "bc", "bzip2", "diffutils", "findutils", "gnupg2",
    "hostname", "iproute", "iputils", "keyutils", "less", "lsof", "man-db", "ncurses", "pigz", "pinentry",
    "procps-ng", "rsync", "shadow-utils", "sudo", "tar", "time", "tree", "tzdata", "unzip", "util-linux",
    "wget", "which", "xorg-x11-xauth", "xz", "zip", "openssh-clients",
    # locale and administration baseline
    "glibc-langpack-en", "gzip", "vim-enhanced", "git-core", "jq", "openssl", "ca-certificates",
    "bind-utils", "nmap-ncat", "traceroute",
]

#: Package names that differ between EL majors: on EL10 `passwd` is part of shadow-utils.
EL_PACKAGES = {"9": ["passwd"], "10": []}

#: Optional system-package groups, installed with dnf from the base's own repositories.
PACKAGE_GROUPS = {
    "container-tools": {
        "label": "podman, buildah, skopeo", "group": "Containers",
        "purpose": "Inspect, copy and mirror images; build OCI images (rootless inside Distrobox needs extra setup)",
        "packages": ["podman", "buildah", "skopeo", "fuse-overlayfs", "shadow-utils-subid"],
        "check": [{"binary": "podman", "argv": ["podman", "--version"], "expect": "podman version"},
                  {"binary": "buildah", "argv": ["buildah", "--version"], "expect": "buildah version"},
                  {"binary": "skopeo", "argv": ["skopeo", "--version"], "expect": "skopeo version"}],
    },
    "ansible": {
        "label": "ansible-core", "group": "Automation",
        "purpose": "Automation (needs Python, which it brings along)",
        "packages": ["ansible-core", "python3"],
        "check": [{"binary": "ansible", "argv": ["ansible", "--version"], "expect": "ansible [core"}],
    },
    "python": {
        "label": "python3 + pip", "group": "Automation", "purpose": "Scripts and automation",
        "packages": ["python3", "python3-pip"],
        "check": [{"binary": "python3", "argv": ["python3", "--version"], "expect": "Python 3."}],
    },
}

#: Always-present tools the check also reports, so the version list is complete.
BASE_CHECKS = [
    {"binary": "git", "argv": ["git", "--version"], "expect": "git version"},
    # EL10's jq prints only "jq-" for --version; the package knows its version.
    {"binary": "jq", "argv": ["rpm", "-q", "--qf", "jq %{VERSION}\\n", "jq"], "expect": "jq 1."},
    {"binary": "curl", "argv": ["curl", "--version"], "expect": "curl "},
    {"binary": "wget", "argv": ["wget", "--version"], "expect": "Wget"},
    {"binary": "openssl", "argv": ["openssl", "version"], "expect": "OpenSSL"},
    {"binary": "dig", "argv": ["dig", "-v"], "expect": "DiG"},
    {"binary": "nslookup", "argv": ["nslookup", "-version"], "expect": "nslookup"},
    {"binary": "nc", "argv": ["nc", "--version"], "expect": "Ncat"},
    {"binary": "traceroute", "argv": ["traceroute", "--version"], "expect": "traceroute"},
    {"binary": "vim", "argv": ["vim", "--version"], "expect": "VIM"},
    {"binary": "bash", "argv": ["bash", "--version"], "expect": "GNU bash"},
]

#: Optional parts, in the order the wizard lists them. All are on by default.
OPTIONAL = ["oc-mirror", "opm", "operator-sdk", "helm", "kustomize", "k9s", "stern", "kubectx", "yq",
            "container-tools", "ansible", "python"]

#: Commands the toolkit adds; the wizard and the image's help list them.
HELPERS = [
    {"name": "oc-status", "what": "Cluster overview: who you are, version, nodes, operators, failing pods"},
    {"name": "oc-health", "what": "ClusterOperator, MachineConfigPool and node health; exit 1 when degraded"},
    {"name": "oc-gpu", "what": "GPU capacity, allocatable and requested per node; GPU pods; GPU/NFD/Network operators"},
    {"name": "oc-ai", "what": "OpenShift AI: DataScienceCluster, model serving (KServe, vLLM) and its routes"},
    {"name": "oc-storage", "what": "StorageClasses (default marked), PVCs that are not Bound, PVC usage per namespace"},
    {"name": "oc-routes", "what": "Routes and Ingresses with host, TLS termination and target service"},
    {"name": "oc-logs", "what": "Logs of a pod, deployment or label selector, with --errors to summarise failures"},
    {"name": "oc-context", "what": "List, switch and show contexts across ~/.kube/config and ~/.kube/clusters/*"},
    {"name": "oc-debug-node", "what": "Debug shell on a node (asks first on a production context)"},
    {"name": "oc-version-check", "what": "Client against server versions, with the supported skew"},
    {"name": "oc-prom", "what": "Run a PromQL query against the cluster monitoring stack (gpu: GPU utilisation)"},
    {"name": "openshift-admin-versions", "what": "Installed tools and their versions (--json)"},
    {"name": "host-podman", "what": "Run the host's Podman from inside Distrobox (explicit, nothing is mounted)"},
]

ALIASES = {
    "k": "kubectl",
    "ocnodes": "oc get nodes -o wide",
    "ocpods": "oc get pods -A",
    "ochealth": "oc get clusteroperators",
    "ocmcp": "oc get mcp",
    "ocgpu": r"oc get nodes -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\\.com/gpu",
}

DEFAULTS = {"timezone": "Europe/Oslo", "locale": "en_US.UTF-8", "prod_pattern": "prod"}

#: Rough unpacked size in GB: base and packages plus every download unpacked.
SIZE_GB = {"base": 0.45, "oc": 0.13, "oc-mirror": 0.31, "opm": 0.1, "operator-sdk": 0.12, "helm": 0.07,
           "kustomize": 0.02, "k9s": 0.13, "stern": 0.05, "kubectx": 0.04, "yq": 0.02,
           "container-tools": 0.12, "ansible": 0.1, "python": 0.05}


class OpenShiftError(ValueError):
    """A request the toolkit cannot build, with a message for the user."""
