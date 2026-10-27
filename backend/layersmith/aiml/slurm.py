"""Slurm batch scripts that run an image with Podman on exactly the GPUs Slurm allocated.

How the GPUs are found, on every node, before the container starts:

  1. GRES: the step must have been allocated GPUs (SLURM_STEP_GPUS /
     SLURM_JOB_GPUS), and as many as were asked for.
  2. Device isolation: for every GPU the driver knows (/proc/driver/nvidia),
     the script tries to open its /dev/nvidiaN. With ConstrainDevices=yes the
     job's cgroup lets it open only the allocated ones; those are the GPUs the
     container gets. If the job can open GPUs it was not allocated, isolation
     is not configured, and the script stops with LAYERSMITH_PREREQ_MISSING
     instead of handing the container GPUs that belong to another job.
  3. CDI: each allocated GPU is passed by its UUID name
     (--device nvidia.com/gpu=GPU-...), which does not depend on how Slurm
     and NVML number devices. The CDI spec must contain UUID names.

Never `nvidia.com/gpu=all`, never a hard-coded index, never --privileged.
SELinux stays enforcing: either the container_use_devices boolean is on
(checked), or - when chosen explicitly - labels are disabled for this one
container only.
"""

from __future__ import annotations

import base64
import re
import shlex

from layersmith import config
from layersmith.aiml import render
from layersmith.aiml.catalog import AimlError
from layersmith.core.spec import InvalidSpec, check_path, parse_reference

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
GPU_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
MEM_RE = re.compile(r"^\d{1,7}[KMGT]?$")
TIME_RE = re.compile(r"^(?:\d{1,3}-)?\d{1,2}:\d{2}:\d{2}$|^\d{1,6}$")
LOG_RE = re.compile(r"^[A-Za-z0-9._/%-]{1,255}$")
SHM_RE = re.compile(r"^\d{1,5}[mgMG]$")
#: Host paths a job container must never see.
FORBIDDEN_SOURCES = ("/", "/etc", "/root", "/boot", "/usr", "/var", "/var/run", "/run", "/proc", "/sys", "/dev",
                     "/home")
FORBIDDEN_PREFIXES = ("/etc/", "/boot/", "/proc/", "/sys/", "/dev/", "/run/", "/var/run/", "/var/lib/containers",
                      "/usr/")

DEFAULTS = {
    "job_name": "layersmith-gpu",
    "partition": "gpu",
    "account": "",
    "nodes": 1,
    "gpus_per_node": 1,
    "gpu_type": "",
    "cpus_per_task": 16,
    "mem": "128G",
    "time": "01:00:00",
    "command": "python3 -c 'import torch; print(torch.cuda.get_device_name(0))'",
    "mounts": [{"source": "/models", "target": "/models", "mode": "ro"},
               {"source": "/datasets", "target": "/datasets", "mode": "ro"},
               {"source": "/scratch", "target": "/scratch", "mode": "rw"}],
    "workdir": "",
    "output": "layersmith-%x-%j.out",
    "error": "layersmith-%x-%j.err",
    "network": "none",
    "shm_size": "16g",
    "selinux": "container_use_devices",
    "kind": "job",
}
SELINUX_MODES = ("container_use_devices", "label_disable")


def _int(value, what, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise AimlError(f"{what} must be a whole number")
    if not low <= number <= high:
        raise AimlError(f"{what} must be between {low} and {high}")
    return number


def _match(pattern, value, what):
    value = str(value or "").strip()
    if not pattern.match(value):
        raise AimlError(f"Invalid {what}: {value!r}" if value else f"Missing {what}")
    return value


def _mount(item) -> dict:
    if not isinstance(item, dict):
        raise AimlError("Each bind mount is {source, target, mode}")
    try:
        source = check_path(item.get("source"), "mount source")
        target = check_path(item.get("target"), "mount target")
    except InvalidSpec as exc:
        raise AimlError(str(exc)) from exc
    if source in FORBIDDEN_SOURCES or source.startswith(FORBIDDEN_PREFIXES) or "podman.sock" in source \
            or "docker.sock" in source:
        raise AimlError(f"{source} must not be mounted into a job container")
    if target in ("/", "/usr", "/etc", "/proc", "/sys", "/dev") or target.startswith(("/proc/", "/sys/", "/dev/")):
        raise AimlError(f"{target} is not a valid place to mount data in the container")
    mode = item.get("mode") or "ro"
    if mode not in ("ro", "rw"):
        raise AimlError("Mount mode is ro or rw")
    return {"source": source, "target": target, "mode": mode}


def validate(params: dict | None, image: str) -> dict:
    params = {**DEFAULTS, **{k: v for k, v in (params or {}).items() if v is not None}}
    unknown = sorted(set(params) - set(DEFAULTS))
    if unknown:
        raise AimlError("Unknown Slurm setting: " + ", ".join(unknown))
    try:
        parse_reference(image)
    except InvalidSpec as exc:
        raise AimlError(str(exc)) from exc
    clean = {
        "image": image,
        "job_name": _match(NAME_RE, params["job_name"], "job name"),
        "partition": _match(NAME_RE, params["partition"], "partition"),
        "account": _match(NAME_RE, params["account"], "account") if params["account"] else "",
        "nodes": _int(params["nodes"], "Nodes", 1, 256),
        "gpus_per_node": _int(params["gpus_per_node"], "GPUs per node", 1, 16),
        "gpu_type": _match(GPU_TYPE_RE, params["gpu_type"], "GPU type") if params["gpu_type"] else "",
        "cpus_per_task": _int(params["cpus_per_task"], "CPUs per task", 1, 1024),
        "mem": _match(MEM_RE, params["mem"], "memory (for example 128G)"),
        "time": _match(TIME_RE, params["time"], "time limit (HH:MM:SS, D-HH:MM:SS or minutes)"),
        "output": _match(LOG_RE, params["output"], "output log path"),
        "error": _match(LOG_RE, params["error"], "error log path"),
        "network": params["network"] if params["network"] in ("none", "host") else None,
        "shm_size": _match(SHM_RE, params["shm_size"], "shared memory size (for example 16g)"),
        "selinux": params["selinux"] if params["selinux"] in SELINUX_MODES else None,
        "kind": params["kind"] if params["kind"] in ("job", "test") else None,
    }
    for key, what in (("network", "Network is none or host"), ("selinux", "SELinux mode is "
                      "container_use_devices or label_disable"), ("kind", "Kind is job or test")):
        if clean[key] is None:
            raise AimlError(what)
    command = str(params["command"] or "").strip()
    if clean["kind"] == "job":
        if not command:
            raise AimlError("Give the command the job runs in the container")
        if len(command) > 4000 or "\n" in command or "\r" in command or "\x00" in command:
            raise AimlError("The command is one line of at most 4000 characters")
    clean["command"] = command
    mounts = params["mounts"] or []
    if not isinstance(mounts, list) or len(mounts) > 20:
        raise AimlError("At most 20 bind mounts")
    clean["mounts"] = [_mount(m) for m in mounts]
    targets = [m["target"] for m in clean["mounts"]]
    if len(set(targets)) != len(targets):
        raise AimlError("Two bind mounts use the same container path")
    workdir = str(params["workdir"] or "").strip()
    if workdir:
        try:
            workdir = check_path(workdir, "working directory")
        except InvalidSpec as exc:
            raise AimlError(str(exc)) from exc
    clean["workdir"] = workdir
    if clean["nodes"] > 1 and clean["network"] == "none":
        # Multi-node NCCL needs the hosts' interconnect; say so rather than fail at run time.
        clean["warning"] = ("Network 'none' isolates each node's container, so a multi-node job cannot "
                            "communicate (NCCL, torchrun). Choose 'host' for distributed training.")
    return clean


PREFLIGHT = r'''
ls_node() {
  set -uo pipefail
  local node; node="$(hostname -s)"
  prereq() { echo "LAYERSMITH_PREREQ_MISSING node=$node: $*" >&2; echo "LAYERSMITH_PREREQ_MISSING node=$node: $*"; exit 97; }

  [ -n "${SLURM_JOB_ID:-}" ] || prereq "not running inside a Slurm job"
  command -v podman >/dev/null 2>&1 || prereq "podman is not installed on this node"

  # 1. GRES: what Slurm allocated to this step on this node.
  local alloc="${SLURM_STEP_GPUS:-${SLURM_JOB_GPUS:-}}"
  [ -n "$alloc" ] || prereq "Slurm allocated no GPUs: is GRES gpu configured (gres.conf, GresTypes=gpu)?"
  local want=0 part
  for part in ${alloc//,/ }; do
    if [[ "$part" == *-* ]]; then want=$((want + ${part#*-} - ${part%-*} + 1)); else want=$((want + 1)); fi
  done
  if [ -z "${XDG_RUNTIME_DIR:-}" ] && [ -d "/run/user/$(id -u)" ]; then export XDG_RUNTIME_DIR="/run/user/$(id -u)"; fi
  [ "$want" -eq "$LS_GPUS_PER_NODE" ] || prereq "Slurm allocated $want GPU(s) on $node, the job asked for $LS_GPUS_PER_NODE"

  # 2. cgroup device isolation: the GPUs this job can actually open are the allocated ones.
  local info minor uuid total=0 denied=0
  local -a uuids=()
  # LS_PROC_NVIDIA and LS_DEV exist for LayerSmith's own tests; jobs use the defaults.
  for info in "${LS_PROC_NVIDIA:-/proc/driver/nvidia}"/gpus/*/information; do
    [ -r "$info" ] || continue
    minor="$(awk -F: '/^Device Minor/ {gsub(/[ \t]/, "", $2); print $2}' "$info")"
    uuid="$(awk -F: '/^GPU UUID/ {gsub(/[ \t]/, "", $2); print $2}' "$info")"
    [ -n "$minor" ] && [ -n "$uuid" ] || continue
    total=$((total + 1))
    if ( : < "${LS_DEV:-/dev}/nvidia$minor" ) 2>/dev/null; then uuids+=("$uuid"); else denied=$((denied + 1)); fi
  done
  [ "$total" -gt 0 ] || prereq "no NVIDIA GPUs in /proc/driver/nvidia: is the driver loaded?"
  local isolation="enforced ($denied of $total GPUs blocked by the job's cgroup)"
  if [ "${#uuids[@]}" -ne "$want" ]; then
    if [ "$denied" -eq 0 ]; then
      prereq "GPU device isolation is not configured: this job can open all $total GPUs but was allocated $want. Set ConstrainDevices=yes in cgroup.conf (with ProctrackType=proctrack/cgroup and TaskPlugin=task/cgroup) before running GPU containers."
    fi
    prereq "this job can open ${#uuids[@]} GPU(s) but Slurm allocated $want: check gres.conf against the hardware"
  fi
  [ "$want" -eq "$total" ] && isolation="whole node allocated ($total GPUs; isolation not testable here)"

  # 3. NVIDIA CDI: every allocated GPU must have a UUID-named CDI device.
  local spec_dirs="${LS_CDI_DIRS:-/etc/cdi /var/run/cdi}"
  local cdi_list=""
  if command -v nvidia-ctk >/dev/null 2>&1; then cdi_list="$(nvidia-ctk cdi list 2>/dev/null || true)"; fi
  local -a devices=()
  for uuid in "${uuids[@]}"; do
    if ! printf '%s\n' "$cdi_list" | grep -qx "nvidia.com/gpu=$uuid" \
       && ! grep -rqs -- "$uuid" $spec_dirs; then
      prereq "the NVIDIA CDI spec has no device nvidia.com/gpu=$uuid. Generate it with UUID names: nvidia-ctk cdi generate --device-name-strategy=uuid --output=/etc/cdi/nvidia.yaml"
    fi
    devices+=(--device "nvidia.com/gpu=$uuid")
  done

  # 4. SELinux stays enforcing; the container needs permission to use devices.
  local -a selinux=()
  if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce)" = "Enforcing" ]; then
    if [ "$LS_SELINUX" = "label_disable" ]; then
      selinux=(--security-opt label=disable)
    elif ! getsebool container_use_devices 2>/dev/null | grep -q -- '--> on$'; then
      prereq "SELinux boolean container_use_devices is off: an administrator runs 'setsebool -P container_use_devices on' (or regenerate this script with SELinux mode label_disable)"
    fi
  fi

  # 5. The image is loaded and the data is where the mounts say.
  podman image exists "$LS_IMAGE" || prereq "image $LS_IMAGE is not loaded on $node: podman load -i <archive>.tar"
  local src
  for src in "${LS_MOUNT_SOURCES[@]}"; do
    [ -e "$src" ] || prereq "bind mount source $src does not exist on $node"
  done

  echo "LayerSmith: node=$node job=$SLURM_JOB_ID gpus=${uuids[*]} isolation=$isolation"
  local -a podman_args=(run --rm --network="$LS_NETWORK" --cap-drop=ALL --security-opt=no-new-privileges
    --shm-size="$LS_SHM" "${selinux[@]}" "${devices[@]}" "${LS_MOUNT_ARGS[@]}"
    -e SLURM_JOB_ID -e SLURM_JOB_NAME -e SLURM_NNODES -e SLURM_NODEID -e SLURM_JOB_NODELIST
    -e LAYERSMITH_SLURM_NODE="$node" -e LAYERSMITH_SLURM_GPUS="${uuids[*]}"
    -e LAYERSMITH_SLURM_ISOLATION="$isolation" -e LAYERSMITH_SLURM_PARTITION="${SLURM_JOB_PARTITION:-}")
  [ -n "$LS_WORKDIR" ] && podman_args+=(--workdir "$LS_WORKDIR")
  local rc
'''

RUN_JOB = r'''
  if [ "$LS_ENTRYPOINT_WRAPS" = 1 ]; then
    podman "${podman_args[@]}" "$LS_IMAGE" /bin/bash -c "$LS_COMMAND"
  else
    podman "${podman_args[@]}" --entrypoint /bin/bash "$LS_IMAGE" -c "$LS_COMMAND"
  fi
  rc=$?
  echo "LAYERSMITH_SLURM_EXIT {\"node\": \"$node\", \"job\": \"$SLURM_JOB_ID\", \"exit_code\": $rc}"
  return $rc
}
'''

RUN_TEST = r'''
  local check; check="$(mktemp)"
  printf '%s' "$LS_CHECK_B64" | base64 -d > "$check"
  if [ "$LS_ENTRYPOINT_WRAPS" = 1 ]; then
    podman "${podman_args[@]}" -i -e LAYERSMITH_IMAGE="$LS_IMAGE" "$LS_IMAGE" \
      python3 - gpu --expect-gpus "$LS_GPUS_PER_NODE" < "$check"
  else
    podman "${podman_args[@]}" -i -e LAYERSMITH_IMAGE="$LS_IMAGE" --entrypoint python3 "$LS_IMAGE" \
      - gpu --expect-gpus "$LS_GPUS_PER_NODE" < "$check"
  fi
  rc=$?
  rm -f "$check"
  echo "LAYERSMITH_SLURM_EXIT {\"node\": \"$node\", \"job\": \"$SLURM_JOB_ID\", \"exit_code\": $rc}"
  return $rc
}
'''


def generate(params: dict | None, image: str, entrypoint_wraps: bool = True) -> dict:
    """Return {"script", "filename", "params", "warnings"} for a validated request."""
    p = validate(params, image)
    q = shlex.quote
    gres = f"gpu:{p['gpu_type']}:{p['gpus_per_node']}" if p["gpu_type"] else f"gpu:{p['gpus_per_node']}"
    mount_args = []
    for m in p["mounts"]:
        mount_args += ["-v", f"{m['source']}:{m['target']}:{m['mode']}"]
    what = "GPU test (LayerSmith AI/ML check)" if p["kind"] == "test" else "job"
    header = [
        "#!/bin/bash",
        f"# Generated by LayerSmith {config.VERSION}: Podman {what} for {p['image']}",
        "#",
        "# Slurm reserves the GPUs through GRES; the script passes exactly those GPUs to Podman by",
        "# their CDI UUID names, after checking that the job's cgroup blocks every other GPU.",
        "# It stops with LAYERSMITH_PREREQ_MISSING when a prerequisite is not met.",
        "# Rootless Podman, no --privileged, SELinux left enforcing.",
        "#",
        f"#   sbatch {p['job_name']}.sbatch",
        f"#SBATCH --job-name={p['job_name']}",
        f"#SBATCH --partition={p['partition']}",
        *([f"#SBATCH --account={p['account']}"] if p["account"] else []),
        f"#SBATCH --nodes={p['nodes']}",
        "#SBATCH --ntasks-per-node=1",
        f"#SBATCH --gres={gres}",
        f"#SBATCH --cpus-per-task={p['cpus_per_task']}",
        f"#SBATCH --mem={p['mem']}",
        f"#SBATCH --time={p['time']}",
        f"#SBATCH --output={p['output']}",
        f"#SBATCH --error={p['error']}",
        "set -euo pipefail",
        "",
        f"export LS_IMAGE={q(p['image'])}",
        f"export LS_GPUS_PER_NODE={p['gpus_per_node']}",
        f"export LS_NETWORK={p['network']}",
        f"export LS_SHM={p['shm_size']}",
        f"export LS_SELINUX={p['selinux']}",
        f"export LS_WORKDIR={q(p['workdir'])}",
        f"export LS_ENTRYPOINT_WRAPS={1 if entrypoint_wraps else 0}",
        f"export LS_COMMAND={q(p['command'])}",
        # Arrays cannot be exported; they are rebuilt from these strings on each node.
        f"export LS_MOUNT_SPEC={q(' '.join(mount_args))}",
        f"export LS_MOUNT_SRC={q(' '.join(m['source'] for m in p['mounts']))}",
    ]
    if p["kind"] == "test":
        encoded = base64.b64encode(render.CHECK_SCRIPT.read_bytes()).decode()
        header.append(f"export LS_CHECK_B64={q(encoded)}")
    body = PREFLIGHT.replace(
        "  local src\n",
        "  local -a LS_MOUNT_ARGS LS_MOUNT_SOURCES\n"
        "  read -r -a LS_MOUNT_ARGS <<< \"$LS_MOUNT_SPEC\"\n"
        "  read -r -a LS_MOUNT_SOURCES <<< \"$LS_MOUNT_SRC\"\n"
        "  local src\n") + (RUN_TEST if p["kind"] == "test" else RUN_JOB)
    footer = [
        "export -f ls_node",
        "",
        "# One container per node; each node maps its own allocation.",
        f"srun --ntasks-per-node=1 --gres={gres} bash -c ls_node",
    ]
    script = "\n".join(header) + "\n" + body + "\n" + "\n".join(footer) + "\n"
    warnings = [p["warning"]] if p.get("warning") else []
    if p["selinux"] == "label_disable":
        warnings.append("SELinux labels are disabled for this container only (--security-opt label=disable). "
                        "Prefer the container_use_devices boolean where the cluster allows it.")
    return {"script": script, "filename": f"{p['job_name']}.sbatch", "params": p, "warnings": warnings}
