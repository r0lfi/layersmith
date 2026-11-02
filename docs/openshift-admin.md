# OpenShift Admin Toolkit

Complete containerized administration environment for Red Hat OpenShift,
Kubernetes, OpenShift AI and GPU infrastructure, optimized for Podman and
Distrobox.

Category **DevOps / Kubernetes / OpenShift**. In the web UI: **Create image →
OpenShift Admin Toolkit** (or the tile on the dashboard). Through the API, a
project whose spec is `{"openshift": {...}}`.

## What you choose

| Setting | Default | Options |
|---|---|---|
| Base image | AlmaLinux 10 | AlmaLinux, Rocky Linux, CentOS Stream 10 or 9; or a custom EL9/EL10-compatible reference (an internal mirror) with a tag or digest |
| OpenShift client | 4.22.18 (stable-4.22) | 4.22.19 (fast-4.22), 4.21.37 (stable-4.21) |
| Tool versions | newest pinned | oc-mirror and opm follow the client unless chosen; operator-sdk, helm, kustomize, k9s, stern, kubectx/kubens and yq have their own versions |
| Optional tools | all on | oc-mirror, opm, operator-sdk, helm, kustomize, k9s, stern, kubectx/kubens, yq, podman/buildah/skopeo, ansible-core, python3 |
| Time zone | Europe/Oslo | any tz database name |
| Production pattern | `prod` | an extended regex matched against context names and API server URLs |

Architecture is linux/amd64. Locale en_US.UTF-8. Extra distro packages,
environment variables and labels can be added through the API
(`extra_packages`, `env`, `labels`); secrets in `env` are refused as everywhere
in LayerSmith.

```json
{"openshift": {"base": "AlmaLinux 10", "ocp_version": "4.22.18",
               "versions": {"helm": "4.3.0"}, "optional": ["helm", "k9s", "kubectx"],
               "timezone": "Europe/Oslo", "prod_pattern": "prod|prd"}}
```

## Where the tools come from

| Tool | Source | Verified against |
|---|---|---|
| oc, kubectl | `mirror.openshift.com/.../ocp/<version>/openshift-client-linux-amd64-rhel9-<version>.tar.gz` | `sha256sum.txt`, signed with Red Hat release key 2 |
| oc-mirror | same directory, `oc-mirror.rhel9.tar.gz` | same |
| opm | same directory, `opm-linux-rhel9-<version>.tar.gz` | same |
| helm | `mirror.openshift.com/pub/cgw/helm/<version>/` (Red Hat build) | Red Hat `sha256sum.txt` |
| operator-sdk | GitHub release, operator-framework/operator-sdk | the release's `checksums.txt` |
| kustomize, k9s, stern, kubectx/kubens, yq | each project's GitHub release | the release's checksum file |
| everything else | the base image's own dnf repositories | the distribution's package signatures |

Every download is pinned by sha256 in `backend/layersmith/openshift/catalog.py`;
the build downloads it again and refuses anything with another checksum, so a
moved or replaced file fails the build instead of reaching the image. No
version is ever "latest", and no installer script is downloaded and run. The
Red Hat binaries are the el9 builds (glibc 2.34+), which is why the bases are
Enterprise Linux 9 or 10. `kubectl` is the binary from the same Red Hat client
archive (identical to `oc`, stored once as a hard link).

New client versions: `python backend/scripts/pin_openshift.py 4.22.20` verifies
Red Hat's signature and the archives and prints the catalogue entries, including
the Kubernetes version read from kubectl.

### Client and cluster versions

oc 4.22 is made for OpenShift 4.22. kubectl follows the Kubernetes skew policy
(one minor older or newer than the API server), so one image serves OpenShift
4.21 to 4.23; for upgrades and support cases use the matching client.
`oc-version-check` compares against the cluster you are logged in to and exits 1
outside the skew.

## What the image contains

* Every system package updated at build time (`dnf -y upgrade --refresh`).
* All commands `distrobox enter` would otherwise install on first start (sudo,
  shadow-utils, passwd, util-linux, findutils, procps-ng, ...), so the first
  start needs no network.
* git, jq, curl, wget, openssl, dig/nslookup, nc, traceroute, vim, less, tar,
  unzip, gzip, iproute, bash-completion.
* Bash completion for oc, kubectl, helm, kustomize, k9s, stern, opm,
  operator-sdk, yq and the toolkit's own commands.
* `/etc/profile.d/openshift-admin.sh`: aliases, multi-cluster kubeconfig, the
  context/namespace prompt with a PROD marker, credentials kept out of history.
* No USER, no ENTRYPOINT, no systemd, no SSH server, no NVIDIA/CUDA driver.
  `podman run` gets a login shell (`CMD ["/bin/bash", "-l"]`).
* `/usr/share/doc/openshift-admin/README.md`: the same README the build page and
  the air-gap bundle carry.

## Commands

| Command | What it does |
|---|---|
| `oc-status` | Session, cluster version, node readiness, cluster operators with problems, pods not running |
| `oc-health` | Every ClusterOperator, MachineConfigPools, nodes with pressure; exit 1 when degraded |
| `oc-gpu [nodes\|pods\|pending\|operators]` | GPU product/memory, capacity, allocatable and requested GPUs per node, MIG, taints; GPU pods with requests and limits; pending GPU pods with the scheduler's reason; NVIDIA GPU Operator (ClusterPolicy), NFD, NVIDIA Network Operator |
| `oc-ai [platform\|serving\|vllm]` | OpenShift AI operator, DSCInitialization, DataScienceCluster components; KServe InferenceServices, ServingRuntimes and their Routes; vLLM pods |
| `oc-storage` | StorageClasses (default marked), VolumeSnapshotClasses, PVCs not Bound, capacity per namespace |
| `oc-routes` | Routes and Ingresses with host, TLS termination and service; flags plain HTTP |
| `oc-logs` | Logs of a pod, KIND/NAME or selector; `--errors` with the most frequent error lines; `--problems` for restarting and waiting pods |
| `oc-context` | List and switch contexts across all loaded kubeconfigs; `-n` sets the namespace |
| `oc-debug-node` | `oc debug node/...`; asks first on a production context |
| `oc-version-check` | Client against cluster versions |
| `oc-prom 'QUERY'\|gpu\|alerts` | PromQL through Thanos Querier, token passed through a file descriptor, TLS verified with the cluster's ingress CA |
| `openshift-admin-versions [--json]` | Every tool's version, checked against what the image was built with |
| `host-podman` | The host's Podman, inside Distrobox only |
| `openshift-admin-help [ca\|kubeconfig\|podman]` | Overview and short how-tos |

Aliases: `k`, `ocnodes`, `ocpods`, `ochealth`, `ocmcp`, `ocgpu`.

Exit codes: 0 ok, 1 problems found, 2 not logged in / no context, 3 cluster
unreachable or TLS failure, 4 not allowed, 64 usage. A missing operator, CRD or
GPU is reported as such, never as a failure of the command.

## Build, checks and export

LayerSmith builds the image like any other and then runs two checks inside it,
with networking disabled and every capability dropped:

* **Image contents** (as root): no kubeconfig-shaped file anywhere, no registry
  auth, `.netrc`, `.ssh` or `.kube` for any user, no SSH server, every
  distrobox-init prerequisite present, sudo and sudoers valid, locale and time zone.
* **Non-root use** (uid 1000 without a passwd entry, like a new Distrobox user):
  every tool runs and reports its pinned version, bash completion loads, the
  helpers explain a missing login (exit 2), three kubeconfigs in
  `~/.kube/clusters` are merged, listed and switched (the switch is written to
  `~/.kube/config`, the cluster files stay untouched), the production warning
  appears, an unreachable cluster is exit 3, and the prompt shows the context.

A failed check does not fail the build; the build page shows which step failed.
A tool that is missing or reports another version is shown as `missing`,
`mismatch` or `error`, never as installed. A download that fails or does not
match its pin fails the build itself.

The build page shows the installed versions, the Distrobox commands, the
Assemble manifest and the plain Podman command, with a **Download all** for the
archive, its checksum, the manifest and the README. With the Podman backend the
export is an OCI archive (`podman save --format oci-archive`). The air-gap
bundle adds `openshift/README.md`, `openshift/openshift-admin.ini` and
`openshift/checks.json`. Image scanning and SBOMs work as for every image when
a scanner is configured (see [scanning.md](scanning.md)).

## Using it

### The openshift-admin command (recommended)

`install-host.sh` from the build page (also `openshift/install-host.sh` in the
air-gap bundle) loads the image and gives your user a container that is always
running, entered by typing `openshift-admin` (`openshift_admin` works too):

```bash
sha256sum -c openshift-admin-1.0.0.tar.sha256
bash install-host.sh --archive openshift-admin-1.0.0.tar

openshift-admin                         # login shell: oc login ..., oc get nodes, k9s, oc-status
openshift-admin oc get co               # one command, then back on the host
openshift-admin oc apply -f - < app.yaml
openshift-admin --status                # container, image, service
```

With a [split download](split-archive.md), `./assemble-image.sh --install`
restores, verifies, loads and installs in one step.

What it sets up, all rootless and for your user only (no sudo):

| Path | What |
|---|---|
| `~/.local/bin/openshift-admin`, `openshift_admin` | the command |
| `~/.config/systemd/user/openshift-admin.service` | runs the container (`openshift-admin --serve`), restarts it if it stops |
| `localhost/openshift-admin:current` | the tag the container runs |
| volume `openshift-admin-home` | the container's `/home/admin`: shell history, helm and oc caches |
| `~/.kube` | mounted at `/home/admin/.kube`: kubeconfigs and `oc login` tokens stay on the host |
| `~/.config/openshift-admin/env` | optional `VAR=value` lines for the container (`HTTPS_PROXY`, `NO_PROXY`, `OPENSHIFT_ADMIN_PROD_PATTERN`) |

The container runs as you (`--userns=keep-id`) with its own network namespace and
no other host directory. The installer turns on lingering
(`loginctl enable-linger`) when it is allowed, so the container keeps running
after logout and starts at boot; otherwise it runs while you are logged in. Without a
systemd user session (`su`, `sudo -u`) no service is installed and
`openshift-admin` starts the container itself when it is not running.

A new version: run the same command with the new archive. The tag moves, the
container is recreated on the new image, and `~/.kube` and the home volume are
kept. Old images stay loaded until you `podman rmi` them.
`bash install-host.sh --uninstall` removes the command and the service.

### Distrobox

```bash
sha256sum -c openshift-admin-1.0.0.tar.sha256
podman load -i openshift-admin-1.0.0.tar
podman tag localhost/openshift-admin:1.0.0 localhost/openshift-admin:4.22

distrobox create --name openshift-admin --image localhost/openshift-admin:4.22
distrobox enter openshift-admin
```

or, with the manifest from the build page:

```bash
distrobox assemble create --file openshift-admin.ini
distrobox enter openshift-admin
```

`skopeo copy oci-archive:openshift-admin-1.0.0.tar containers-storage:localhost/openshift-admin:4.22`
imports without `podman load`; `skopeo inspect oci-archive:...` shows labels and layers.

### What Distrobox does, and the alternative

The image needs no privileged mode, no host network and no host filesystem.
Distrobox itself, by design, starts containers with `--privileged` (rootless:
only your own user's rights), shares your home and mounts the host's root at
`/run/host`; it uses the host network unless `--unshare-netns` is given (the
manifest sets `unshare_netns=true`). When that is too much, run the image with
Podman alone; it then sees only `~/.kube`:

```bash
podman run --rm -it --name openshift-admin --userns=keep-id \
  --tmpfs /home/admin:rw,mode=1777 -v "$HOME/.kube:/home/admin/.kube:z" \
  -e HOME=/home/admin -w /home/admin localhost/openshift-admin:4.22
```

`:z` relabels `~/.kube` for SELinux; on an NFS home use
`--security-opt label=disable` instead.

### Several clusters

```text
~/.kube/
├── config                     oc login writes here; the current context lives here
└── clusters/
    ├── openshift-lab.yaml
    ├── openshift-ai-test.yaml
    └── openshift-ai-prod.yaml
```

Without `KUBECONFIG`, the profile sets it to `~/.kube/config` plus every
`*.yaml`, `*.yml` and `*.kubeconfig` in `~/.kube/clusters` (and creates an empty
`~/.kube/config`, mode 600, so context switches never rewrite a cluster file).
`OPENSHIFT_ADMIN_KUBECONFIG_AUTOLOAD=0` turns this off; a `KUBECONFIG` you set is
always respected. Tokens from `oc login` live in your kubeconfig in your home,
never in the image. TLS is always verified: add a CA with
`oc login --certificate-authority=...` or `sudo update-ca-trust` inside the
container (`openshift-admin-help ca`).

### Podman inside the container

podman, buildah and skopeo are installed as CLIs. Inspecting and copying images
(`skopeo inspect/copy`, `podman pull/save`) works rootless. Building images
inside Distrobox needs nested user namespaces (subuid/subgid for your user
inside the container, fuse-overlayfs) and is not configured. The explicit
alternative is `host-podman build ...`, which runs the host's Podman through
`distrobox-host-exec`. The Podman socket is never mounted.

## Updating

Build a new version of the project in LayerSmith: the base is pulled and pinned
by digest again and every package is updated. With the `openshift-admin`
command, `bash install-host.sh --archive <new archive>` (or
`./assemble-image.sh --install`) is all. With Distrobox: load the new archive, tag it as
`localhost/openshift-admin:4.22`, then `distrobox rm --force openshift-admin`
and create it again. Your home and kubeconfigs are not touched.
