"""opencode Shared Terminal: verified downloads, packages and options.

One long-running container per host holds opencode (the terminal coding
agent) and its tools. Users never enter it by hand: the host gets an
`opencode` command that runs opencode inside the container as the calling
user, in the directory they are in. Several users can work at the same time;
each one's sessions and settings stay in their own home.

Every download below is pinned by sha256. Each pin was checked on the date in
VERIFIED against the sha256 digest GitHub records for the release asset
(the "digest" field of the release API), and for ripgrep also against the
.sha256 file attached to its release. The build downloads each file again and
refuses it unless it has exactly the pinned checksum.

Only opencode 1.x is offered: opencode's own installer (opencode.ai/install)
installs nothing but 1.x releases; 2.x is a different product line.
"""

from layersmith.openshift import catalog as openshift_catalog

VERIFIED = "2026-11-02"

PROFILE = {
    "id": "opencode",
    "name": "opencode Shared Terminal",
    "version": 1,
    "category": "Developer tools / AI coding agents",
    "description": "opencode, the terminal coding agent, in one always-running container on a host. Users type "
                   "`opencode` on the host and are working inside it as themselves; several users at once, "
                   "made for air-gapped networks with a local LLM endpoint.",
}

CATEGORY = {"id": "opencode", "name": PROFILE["name"], "intro": PROFILE["description"]}

SOURCES = {
    "opencode": "https://github.com/anomalyco/opencode/releases",
    "opencode-docs": "https://opencode.ai/docs/",
    "providers": "https://opencode.ai/docs/providers/",
    "ripgrep": "https://github.com/BurntSushi/ripgrep/releases",
}

ARCHITECTURE = "amd64"

#: Enterprise Linux bases, the same set as the OpenShift Admin Toolkit.
BASES = openshift_catalog.BASES
DEFAULT_BASE = "AlmaLinux 10"
EL_MAJORS = ("9", "10")

GH = "https://github.com"

#: opencode is one self-contained binary. "x64" needs a CPU with AVX2;
#: "x64-baseline" runs on any x86-64 CPU (older servers, some virtual CPUs).
CPU_VARIANTS = {
    "x64": {"label": "Modern x86-64 (AVX2)", "asset": "opencode-linux-x64.tar.gz"},
    "x64-baseline": {"label": "Any x86-64 CPU (baseline build, no AVX2 needed)",
                     "asset": "opencode-linux-x64-baseline.tar.gz"},
}
DEFAULT_CPU = "x64"

OPENCODE_VERSIONS = {
    "1.19.5": {"x64": "588310a336fa3b9df36b3f0225b26f679d453efaad0cf3d66c3651ef3ec1647a",
               "x64-baseline": "17c200fd4b2289954cc01552559f9b6d03ec157b2ddfb5eb54144f491656333d"},
    "1.19.4": {"x64": "849bd6b3d959a1d2ef200ed1634a51c79a45ca236aa26665d75debd694805cce",
               "x64-baseline": "1697ef40558d378bd2f72efa8fafe1bed1f0e664f09247b9357556b95dbc7cbc"},
}
DEFAULT_OPENCODE = "1.19.5"

#: opencode searches with ripgrep and downloads it from GitHub when `rg` is not
#: on PATH - which fails offline. 15.1.0 is the version opencode 1.19 itself
#: would fetch; the musl build is static.
RIPGREP = {
    "version": "15.1.0",
    "url": f"{GH}/BurntSushi/ripgrep/releases/download/15.1.0/ripgrep-15.1.0-x86_64-unknown-linux-musl.tar.gz",
    "sha256": "1c9297be4a084eea7ecaedf93eb03d058d6faae29bbc57ecdaf5063921491599",
    "files": {"ripgrep-15.1.0-x86_64-unknown-linux-musl/rg": "/usr/local/bin/rg"},
}


def opencode_download(version: str, cpu: str) -> dict:
    asset = CPU_VARIANTS[cpu]["asset"]
    return {"url": f"{GH}/anomalyco/opencode/releases/download/v{version}/{asset}",
            "sha256": OPENCODE_VERSIONS[version][cpu], "files": {"opencode": "/usr/local/bin/opencode"}}


#: Always installed: what an agent needs to read, change and version code.
#: No sudo and no SSH server: users only ever run as themselves in here.
BASE_PACKAGES = [
    "bash", "bash-completion", "ca-certificates", "diffutils", "file", "findutils", "git-core", "glibc-langpack-en",
    "gzip", "jq", "less", "ncurses", "openssh-clients", "patch", "procps-ng", "tar", "tree", "unzip", "util-linux",
    "vim-enhanced", "which", "xz", "zip",
]

#: Optional toolchains from the base's own repositories, so the agent can build
#: and test what it changes. Anything the agent should run must be in the image.
PACKAGE_GROUPS = {
    "python": {"label": "Python 3 + pip", "purpose": "Run and test Python code",
               "packages": ["python3", "python3-pip"],
               "check": [{"binary": "python3", "argv": ["python3", "--version"], "expect": "Python 3."}]},
    "build-tools": {"label": "gcc, g++, make", "purpose": "Compile C/C++ and run Makefiles",
                    "packages": ["gcc", "gcc-c++", "make"],
                    "check": [{"binary": "gcc", "argv": ["gcc", "--version"], "expect": "gcc"},
                              {"binary": "make", "argv": ["make", "--version"], "expect": "GNU Make"}]},
    "nodejs": {"label": "Node.js + npm", "purpose": "Run and test JavaScript/TypeScript projects",
               "packages": ["nodejs", "npm"],
               "check": [{"binary": "node", "argv": ["node", "--version"], "expect": "v"}]},
}
OPTIONAL = ["python", "build-tools", "nodejs"]
DEFAULT_OPTIONAL = ["python"]

BASE_CHECKS = [
    {"binary": "git", "argv": ["git", "--version"], "expect": "git version"},
    {"binary": "jq", "argv": ["rpm", "-q", "--qf", "jq %{VERSION}\\n", "jq"], "expect": "jq 1."},
    {"binary": "bash", "argv": ["bash", "--version"], "expect": "GNU bash"},
]

#: Set in the image so opencode never reaches for the internet on its own.
ENVIRONMENT = {
    "OPENCODE_CONFIG": "/etc/opencode/opencode.json",
    "OPENCODE_DISABLE_AUTOUPDATE": "1",
    "OPENCODE_DISABLE_MODELS_FETCH": "1",
    "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
}

#: Host side: what install-host.sh sets up unless told otherwise.
HOST_DEFAULTS = {"container": "opencode", "group": "opencode", "mounts": "/home"}

SIZE_GB = {"base": 0.35, "opencode": 0.22, "ripgrep": 0.01, "python": 0.05, "build-tools": 0.15, "nodejs": 0.1}


class OpencodeError(ValueError):
    """A request the profile cannot build, with a message for the user."""
