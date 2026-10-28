"""README, Distrobox Assemble manifest and ready-made commands for an OpenShift Admin Toolkit image.

The same text goes into the image (/usr/share/doc/openshift-admin/README.md),
the air-gap bundle and the build page, so what a user reads offline matches
what LayerSmith showed.
"""

from layersmith.openshift import catalog

CONTAINER = "openshift-admin"


def alias_ref(recipe: dict, image_ref: str) -> str:
    """localhost/openshift-admin:1.0.0 -> localhost/openshift-admin:4.22, the name Distrobox uses."""
    repository = image_ref.rsplit(":", 1)[0] if ":" in image_ref.rsplit("/", 1)[-1] else image_ref
    return f"{repository}:{recipe['ocp']['minor']}"


def commands(recipe: dict, image_ref: str, archive: str) -> dict:
    friendly = alias_ref(recipe, image_ref)
    return {
        "verify": f"sha256sum -c {archive}.sha256",
        "load": f"podman load -i {archive}",
        "tag": f"podman tag {image_ref} {friendly}",
        "skopeo_import": f"skopeo copy oci-archive:{archive} containers-storage:{friendly}",
        "inspect_archive": f"skopeo inspect oci-archive:{archive}",
        "oci_export": f"podman save --format oci-archive -o {archive} {image_ref}",
        "distrobox_create": f"distrobox create --name {CONTAINER} --image {friendly}",
        "distrobox_create_own_network": f"distrobox create --name {CONTAINER} --image {friendly} --unshare-netns",
        "distrobox_enter": f"distrobox enter {CONTAINER}",
        "distrobox_assemble": f"distrobox assemble create --file {CONTAINER}.ini",
        "distrobox_remove": f"distrobox rm --force {CONTAINER}",
        # Rootless, own network namespace, nothing from the host but ~/.kube (:z relabels it for SELinux).
        "podman_run": (f"podman run --rm -it --name {CONTAINER} --userns=keep-id \\\n"
                       f"  --tmpfs /home/admin:rw,mode=1777 -v \"$HOME/.kube:/home/admin/.kube:z\" \\\n"
                       f"  -e HOME=/home/admin -w /home/admin {friendly}"),
        "versions": "openshift-admin-versions",
        "image": image_ref,
        "alias": friendly,
    }


def distrobox_ini(recipe: dict, image_ref: str) -> str:
    friendly = alias_ref(recipe, image_ref)
    return f"""# Distrobox Assemble manifest: {catalog.PROFILE['name']}, OpenShift clients {recipe['ocp']['version']}.
#
# Load the image first (no registry needed):
#   podman load -i <archive>.tar && podman tag {image_ref} {friendly}
# Then create and enter:
#   distrobox assemble create --file {CONTAINER}.ini
#   distrobox enter {CONTAINER}
#
# Distrobox itself starts every container rootless-privileged, shares your home
# and mounts the host's root at /run/host; that is how Distrobox integrates with
# the host, not something this image needs. For a container that sees only
# ~/.kube, use the plain `podman run` from the README instead.

[{CONTAINER}]
image={friendly}
pull=false
init=false
nvidia=false
start_now=true
# The toolkit does not need the host's network namespace.
unshare_netns=true
unshare_ipc=true
# Trust your organisation's CAs inside the container (then: sudo update-ca-trust):
#volume=/etc/pki/ca-trust/source/anchors:/etc/pki/ca-trust/source/anchors:ro
# What marks a production context on this machine (extended regex):
#additional_flags="--env OPENSHIFT_ADMIN_PROD_PATTERN=prod|prd"
"""


def tool_rows(recipe: dict) -> list[str]:
    rows = []
    for tool in recipe["tools"]:
        state = tool["version"] if tool["enabled"] else "not included"
        rows.append(f"| {tool['label']} | {state} | {tool['purpose']} | {tool['verify'] if tool['enabled'] else ''} |")
    return rows


def markdown(recipe: dict, image_ref: str, archive: str = "<archive>.tar") -> str:
    c = commands(recipe, image_ref, archive)
    compat = recipe["compatibility"]
    helpers = "\n".join(f"| `{h['name']}` | {h['what']} |" for h in recipe["helpers"])
    aliases = "\n".join(f"| `{name}` | `{value}` |" for name, value in recipe["aliases"].items())
    tools = "\n".join(tool_rows(recipe))
    return f"""# {catalog.PROFILE['name']}

{catalog.PROFILE['description']}

Image `{image_ref}` (alias `{c['alias']}`), {recipe['base']['display']} base, {catalog.ARCHITECTURE},
OpenShift clients {recipe['ocp']['version']} (Kubernetes {recipe['ocp']['kubernetes']}),
time zone {recipe['timezone']}, locale {recipe['locale']}.
Built by LayerSmith with profile {recipe['profile']} v{recipe['profile_version']}; downloads verified {recipe['verified']}.

## Start

```bash
{c['verify']}
{c['load']}
{c['tag']}

{c['distrobox_create']}
{c['distrobox_enter']}
```

Or with the Assemble manifest (`{CONTAINER}.ini`, next to this README in the bundle):

```bash
{c['distrobox_assemble']}
{c['distrobox_enter']}
```

Then, inside:

```bash
oc login https://api.<cluster>.<domain>:6443
oc whoami
oc get nodes
oc get co
oc get pods -A
helm list -A
k9s
oc-status
```

Without Distrobox, a container that sees only `~/.kube` (rootless, no host network, no host filesystem):

```bash
{c['podman_run']}
```

The archive is an OCI archive; `{c['skopeo_import']}` imports it without
`podman load`, and `{c['inspect_archive']}` shows its labels and layers.

## What is inside

| Tool | Version | Purpose | Verified against |
|---|---|---|---|
{tools}

Also: git, jq, curl, wget, openssl, dig/nslookup, nc, traceroute, vim, less, tar,
unzip, gzip, findutils, procps-ng, iproute, bash-completion, sudo and every command
`distrobox enter` needs. Run `openshift-admin-versions` for the installed versions;
the same list was written at build time to `/usr/local/share/openshift-admin/versions.json`.

No NVIDIA or CUDA driver is installed: drivers belong on the GPU nodes, where the
NVIDIA GPU Operator manages them.

## Commands

| Command | What it does |
|---|---|
{helpers}

| Alias | Runs |
|---|---|
{aliases}

Exit codes of the `oc-*` commands: 0 ok, 1 problems found, 2 not logged in / no
context, 3 cluster unreachable or TLS failure, 4 not allowed, 64 usage.
`openshift-admin-help` lists everything; every command has `--help`.

## Clusters, kubeconfig and login

Nothing about a cluster is stored in the image. Distrobox shares your home, so
`~/.kube` from the host is used as it is.

```text
~/.kube/
├── config                 oc login writes here; the current context lives here
└── clusters/
    ├── openshift-lab.yaml
    ├── openshift-ai-test.yaml
    └── openshift-ai-prod.yaml
```

When `KUBECONFIG` is not set, the shell profile sets it to `~/.kube/config` plus
every `*.yaml`, `*.yml` and `*.kubeconfig` in `~/.kube/clusters`
(`OPENSHIFT_ADMIN_KUBECONFIG_AUTOLOAD=0` turns that off). A `KUBECONFIG` you set
yourself is always respected. `oc-context` lists and switches contexts across all
of them; `oc login` adds new ones.

The prompt shows `[cluster(user):namespace]`. A context whose name or API server
matches `{recipe['prod_pattern']}` (`OPENSHIFT_ADMIN_PROD_PATTERN`, an extended
regex) is shown as **PROD** in red; switching to it, starting a shell in it and
`oc-debug-node` on it warn, and `oc-debug-node` asks before it starts.

Tokens: `oc login` stores the session token in your kubeconfig (on the host,
in your home), never in the image. Commands that begin with or contain `--token`,
`--password` or `oc login ... -p` are kept out of the shell history. `oc-prom`
passes the token to curl through a file descriptor, not the command line. The
helpers read kubeconfigs only through `oc config view`, which redacts secrets.

TLS: certificates are always verified. Trust a cluster CA with
`oc login --certificate-authority=ca.crt ...`, or inside the container with
`sudo cp ca.crt /etc/pki/ca-trust/source/anchors/ && sudo update-ca-trust`
(`openshift-admin-help ca`). Nothing in the toolkit uses `--insecure-skip-tls-verify`.

## Client and cluster versions

oc {compat['client']} is built for OpenShift {compat['recommended_cluster']}. {compat['note']}
Supported cluster range for this image: {', '.join(compat['supported_clusters'])}.
For another cluster version, build the image with the matching client version.

## OpenShift AI and GPUs

* `oc-gpu` - GPU product, memory, capacity, allocatable and requested GPUs per node,
  MIG resources and taints; pods requesting `nvidia.com/gpu` with requests and limits;
  pending GPU pods with the scheduler's reason; the NVIDIA GPU Operator (ClusterPolicy),
  Node Feature Discovery and NVIDIA Network Operator.
* `oc-ai` - OpenShift AI operator, DSCInitialization and DataScienceCluster components,
  KServe InferenceServices and ServingRuntimes, their Routes, and vLLM pods.
* `oc-prom gpu` - GPU utilisation and memory from the DCGM exporter.
* `oc-storage`, `oc-routes` - PVCs for model storage, and how models are exposed.

Every command reports a missing operator, a missing API or the absence of GPUs as
such, instead of failing.

## Podman, Buildah and Skopeo inside Distrobox

They are installed as CLIs. `skopeo inspect/copy` and `podman pull/images/save`
work rootless. Building images inside Distrobox needs nested user namespaces
(subuid/subgid for your user inside the container, fuse-overlayfs) and is not set
up by default. The explicit alternative is the host's Podman:
`host-podman build -t localhost/app:1 .` (runs `distrobox-host-exec podman`).
The Podman socket is never mounted.

## Security

* Rootless: no USER, no ENTRYPOINT and no service in the image; no SSH server.
* No kubeconfig, token, password, key or registry credential in any layer;
  LayerSmith's post-build check searches the image for them.
* Every downloaded client is pinned by sha256 and verified before it is installed;
  the Red Hat checksum files are signed with Red Hat's release key. No installer
  script is downloaded and run.
* Packages come from the base's own signed repositories and are updated at build time.
* LayerSmith's image scanning (SBOM and vulnerabilities) applies when it is enabled.

## Updating

Rebuild in LayerSmith with a new version number: the base is pulled and pinned by
digest again and every package is updated. New client versions are added to
LayerSmith's catalogue (`backend/scripts/pin_openshift.py` prints verified entries),
never fetched as "latest". Then load the new archive, `podman tag` it as
`{c['alias']}` and recreate the container (`{c['distrobox_remove']}`, then create
again); your home and kubeconfigs are untouched.
"""
