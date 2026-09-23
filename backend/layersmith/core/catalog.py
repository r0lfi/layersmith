"""Distributions, capability -> package mapping, tools, presets and templates.

The catalog is what makes LayerSmith distro-aware: a template asks for a
capability ("dns-tools"), not a package name, and the catalog translates it
to bind-utils / dnsutils / bind-tools depending on the base image family.

Everything here is data with no environment-specific values, so a fork can
extend it by editing this file or, later, by loading user-defined templates
from the database.
"""

# Package family -> package manager.
FAMILIES = {"rpm": "dnf", "deb": "apt", "apk": "apk"}
ARCHITECTURES = ("amd64", "arm64")

# Distribution -> family, and the public image for each supported version.
DISTROS = {
    "AlmaLinux": {
        "family": "rpm", "el": True,
        "versions": {"10": "docker.io/library/almalinux:10", "9": "docker.io/library/almalinux:9"},
    },
    "Rocky Linux": {
        "family": "rpm", "el": True,
        "versions": {"10": "docker.io/library/rockylinux:10", "9": "docker.io/library/rockylinux:9"},
    },
    "CentOS Stream": {
        "family": "rpm", "el": True,
        "versions": {"10": "quay.io/centos/centos:stream10", "9": "quay.io/centos/centos:stream9"},
    },
    "Fedora": {
        "family": "rpm", "el": False,
        "versions": {"44": "registry.fedoraproject.org/fedora:44", "43": "registry.fedoraproject.org/fedora:43"},
    },
    "Ubuntu": {
        "family": "deb",
        "versions": {"24.04": "docker.io/library/ubuntu:24.04", "22.04": "docker.io/library/ubuntu:22.04"},
    },
    "Debian": {
        "family": "deb",
        "versions": {"13": "docker.io/library/debian:13", "12": "docker.io/library/debian:12"},
    },
    "Alpine": {
        "family": "apk",
        "versions": {"3.22": "docker.io/library/alpine:3.22", "3.21": "docker.io/library/alpine:3.21"},
    },
}

# Capability -> package name per family. A capability missing from this map is
# passed through unchanged (same name everywhere). None means the capability
# has no package in that family: it is skipped with a warning rather than
# failing the build, and can be added as a custom tool instead.
PACKAGE_MAP = {
    "iproute": {"rpm": "iproute", "deb": "iproute2", "apk": "iproute2"},
    "ping": {"rpm": "iputils", "deb": "iputils-ping", "apk": "iputils"},
    "traceroute": {"rpm": "traceroute", "deb": "traceroute", "apk": "traceroute"},
    "dns-tools": {"rpm": "bind-utils", "deb": "dnsutils", "apk": "bind-tools"},
    "netcat": {"rpm": "nmap-ncat", "deb": "netcat-openbsd", "apk": "netcat-openbsd"},
    "ssh-client": {"rpm": "openssh-clients", "deb": "openssh-client", "apk": "openssh-client"},
    "g++": {"rpm": "gcc-c++", "deb": "g++", "apk": "g++"},
    "pip": {"rpm": "python3-pip", "deb": "python3-pip", "apk": "py3-pip"},
    "python3": {"rpm": "python3", "deb": "python3", "apk": "python3"},
    "python3-devel": {"rpm": "python3-devel", "deb": "python3-dev", "apk": "python3-dev"},
    "venv": {"rpm": None, "deb": "python3-venv", "apk": None},
    "nodejs": {"rpm": "nodejs", "deb": "nodejs", "apk": "nodejs"},
    "npm": {"rpm": "npm", "deb": "npm", "apk": "npm"},
    "shellcheck": {"rpm": "ShellCheck", "deb": "shellcheck", "apk": "shellcheck"},
    "procps": {"rpm": "procps-ng", "deb": "procps", "apk": "procps"},
    "which": {"rpm": "which", "deb": "debianutils", "apk": "which"},
    "mtr": {"rpm": "mtr", "deb": "mtr-tiny", "apk": "mtr"},
    "vim": {"rpm": "vim-enhanced", "deb": "vim", "apk": "vim"},
    "sysstat": {"rpm": "sysstat", "deb": "sysstat", "apk": "sysstat"},
    "build-essentials": {"rpm": "make", "deb": "build-essential", "apk": "build-base"},
    "bash-completion": {"rpm": "bash-completion", "deb": "bash-completion", "apk": "bash-completion"},
    "iotop": {"rpm": "iotop", "deb": "iotop", "apk": "iotop"},
    "tcpdump": {"rpm": "tcpdump", "deb": "tcpdump", "apk": "tcpdump"},
    "nmap": {"rpm": "nmap", "deb": "nmap", "apk": "nmap"},
    "iperf3": {"rpm": "iperf3", "deb": "iperf3", "apk": "iperf3"},
    "socat": {"rpm": "socat", "deb": "socat", "apk": "socat"},
    "ethtool": {"rpm": "ethtool", "deb": "ethtool", "apk": "ethtool"},
    "strace": {"rpm": "strace", "deb": "strace", "apk": "strace"},
    "lsof": {"rpm": "lsof", "deb": "lsof", "apk": "lsof"},
    "htop": {"rpm": "htop", "deb": "htop", "apk": "htop"},
    "ansible-core": {"rpm": "ansible-core", "deb": "ansible-core", "apk": "ansible-core"},
}

# Tools with no distro package: a released binary is fetched (or uploaded for
# offline use), checksummed and copied into the image. {version} and {arch}
# are substituted; nothing here is fetched without an explicit user action.
TOOL_CATALOG = {
    "kubectl": {
        "url": "https://dl.k8s.io/release/v{version}/bin/linux/{arch}/kubectl",
        "member": None, "install_path": "/usr/local/bin/kubectl", "default_version": "1.34.1",
    },
    "helm": {
        "url": "https://get.helm.sh/helm-v{version}-linux-{arch}.tar.gz",
        "member": "linux-{arch}/helm", "install_path": "/usr/local/bin/helm", "default_version": "3.19.0",
    },
    "kustomize": {
        "url": "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv{version}/"
               "kustomize_v{version}_linux_{arch}.tar.gz",
        "member": "kustomize", "install_path": "/usr/local/bin/kustomize", "default_version": "5.7.1",
    },
}

# Tool groups shown as checkbox categories in the wizard.
CATEGORIES = [
    {"name": "Editors", "packages": ["vim", "nano", "less"]},
    {"name": "Networking", "packages": ["iproute", "ping", "traceroute", "tcpdump", "nmap", "dns-tools",
                                        "netcat", "socat", "mtr", "ethtool", "iperf3"]},
    {"name": "Development", "packages": ["git", "gcc", "g++", "make", "cmake", "python3", "pip",
                                         "nodejs", "npm", "shellcheck", "build-essentials"]},
    {"name": "Administration", "packages": ["lsof", "strace", "procps", "sysstat", "htop", "iotop",
                                            "tree", "rsync", "tar", "gzip", "unzip", "which", "file"]},
    {"name": "Automation", "packages": ["ansible-core", "ssh-client", "jq", "curl", "wget", "tmux"]},
]

# Reusable package sets referenced by templates.
PRESETS = [
    {"name": "Basic Linux", "description": "Everyday shell tools",
     "packages": ["git", "vim", "nano", "curl", "wget", "jq", "tar", "gzip", "unzip", "rsync",
                  "tmux", "less", "file", "which"]},
    {"name": "Network Tools", "description": "Connectivity and packet troubleshooting",
     "packages": ["iproute", "ping", "traceroute", "tcpdump", "nmap", "dns-tools", "netcat",
                  "socat", "ethtool", "mtr", "curl", "wget", "iperf3"]},
    {"name": "Development", "description": "Compilers, Python, Node and linters",
     "packages": ["git", "gcc", "g++", "make", "cmake", "python3", "pip", "nodejs", "npm",
                  "shellcheck", "jq"]},
    {"name": "Linux Admin", "description": "Process, IO and system inspection",
     "packages": ["lsof", "strace", "procps", "sysstat", "tree", "htop", "iotop", "rsync",
                  "tmux", "vim", "curl", "wget", "jq", "tar", "gzip", "unzip", "ssh-client"]},
    {"name": "Ansible", "description": "ansible-core with SSH and Git",
     "packages": ["python3", "pip", "git", "ssh-client", "ansible-core", "rsync", "jq", "vim"]},
    {"name": "Kubernetes", "description": "kubectl, helm and kustomize as custom tools",
     "packages": ["bash-completion", "git", "curl", "jq", "ssh-client"],
     "tools": ["kubectl", "helm", "kustomize"]},
    {"name": "OpenShift", "description": "oc and kubectl as custom tools",
     "packages": ["bash-completion", "git", "curl", "jq"], "tools": ["oc", "kubectl", "helm"]},
]

# What the wizard offers in step 1. spec fragments are merged into a new project.
TEMPLATES = [
    {"name": "Minimal", "description": "Base image plus essential shell tools",
     "spec": {"presets": ["Basic Linux"]}},
    {"name": "Developer", "description": "General Linux development environment",
     "spec": {"presets": ["Basic Linux", "Development"], "env": [{"name": "EDITOR", "value": "vim"}],
              "tests": ["git --version", "python3 --version"]}},
    {"name": "Linux Admin", "description": "Inspection and troubleshooting toolbox",
     "spec": {"presets": ["Basic Linux", "Linux Admin"], "tests": ["vim --version"]}},
    {"name": "Network Tools", "description": "Connectivity and packet analysis",
     "spec": {"presets": ["Basic Linux", "Network Tools"], "tests": ["tcpdump --version", "nmap --version"]}},
    {"name": "Ansible", "description": "Ansible control node",
     "spec": {"presets": ["Basic Linux", "Ansible"], "tests": ["ansible --version"]}},
    {"name": "Kubernetes", "description": "Cluster clients",
     "spec": {"presets": ["Basic Linux", "Kubernetes"], "tests": ["kubectl version --client"]}},
    {"name": "OpenShift", "description": "OpenShift clients",
     "spec": {"presets": ["Basic Linux", "OpenShift"], "tests": ["oc version --client"]}},
    {"name": "Custom", "description": "Start from an empty specification", "spec": {}},
]

PRESETS_BY_NAME = {preset["name"]: preset for preset in PRESETS}
TEMPLATES_BY_NAME = {template["name"]: template for template in TEMPLATES}


def family_of(distribution: str) -> str:
    """Package family for a known distribution; 'rpm' is not assumed for unknown ones."""
    entry = DISTROS.get(distribution)
    if entry is None:
        raise KeyError(distribution)
    return entry["family"]


def default_source(distribution: str, version: str) -> str:
    return DISTROS[distribution]["versions"][version]
