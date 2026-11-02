#!/bin/bash
# Install the shared opencode container on this host (run as root).
#
#   sudo bash install-host.sh --archive @ARCHIVE@
#
# Loads the image (no registry needed), takes the host files from the image
# itself so they always match it, and sets up:
#
#   /usr/local/bin/opencode, opencode-shell    the commands users type
#   /usr/local/libexec/opencode-container/     opencode-enter (via sudo), opencode-container
#   /etc/sudoers.d/opencode-container          members of the group may use it
#   /etc/opencode-container/container.conf     image, runtime, shared directories
#   /etc/opencode/opencode.json                site opencode settings (your LLM endpoint) - never overwritten
#   opencode-container.service                 keeps the container running (enabled, started)
#
# Options:
#   --archive FILE     load the image from this archive first
#   --image REF        the image to run (default @IMAGE@)
#   --runtime NAME     podman or docker (default: podman if installed, else docker)
#   --mounts "DIRS"    directories shared with the container at the same path (default /home)
#   --group NAME       who may use opencode (default opencode; created if missing)
#   --users "A B"      add these users to the group
#   --no-start         install, but do not (re)start the container
#   --uninstall        remove what this script installed (settings and the group are kept)
set -euo pipefail

IMAGE=@IMAGE@
ARCHIVE=
RUNTIME=
MOUNTS=
GROUP=opencode
USERS=
START=1
UNINSTALL=0

die() { echo "install-host.sh: $*" >&2; exit 1; }
usage() { sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p}' "$0"; exit "${1:-0}"; }

while (($#)); do
  case $1 in
    --archive) ARCHIVE=${2:?}; shift 2 ;;
    --image) IMAGE=${2:?}; shift 2 ;;
    --runtime) RUNTIME=${2:?}; shift 2 ;;
    --mounts) MOUNTS=${2:?}; shift 2 ;;
    --group) GROUP=${2:?}; shift 2 ;;
    --users) USERS=${2:?}; shift 2 ;;
    --no-start) START=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h | --help) usage 0 ;;
    *) echo "unknown option: $1" >&2; usage 64 ;;
  esac
done

[[ $(id -u) == 0 ]] || die "run as root (sudo bash $0 ...)"
command -v systemctl >/dev/null || die "systemd is required"
[[ $GROUP =~ ^[A-Za-z_][A-Za-z0-9._-]*$ ]] || die "invalid group name: $GROUP"

CONF_DIR=/etc/opencode-container
CONF=$CONF_DIR/container.conf
LIBEXEC=/usr/local/libexec/opencode-container
UNIT=/etc/systemd/system/opencode-container.service
SUDOERS=/etc/sudoers.d/opencode-container

if ((UNINSTALL)); then
  systemctl disable --now opencode-container.service 2>/dev/null || true
  rm -f "$UNIT" "$SUDOERS" /usr/local/bin/opencode /usr/local/bin/opencode-shell
  rm -rf "$LIBEXEC"
  systemctl daemon-reload
  echo "Removed. Kept: $CONF_DIR, /etc/opencode, the $GROUP group, the image and everyone's ~/.local/share/opencode."
  exit 0
fi

if [[ -z $RUNTIME ]]; then
  if command -v podman >/dev/null; then RUNTIME=podman
  elif command -v docker >/dev/null; then RUNTIME=docker
  else die "neither podman nor docker is installed"
  fi
fi
[[ $RUNTIME == podman || $RUNTIME == docker ]] || die "--runtime must be podman or docker"
RUNTIME_PATH=$(command -v "$RUNTIME") || die "$RUNTIME is not installed"

if [[ -n $ARCHIVE ]]; then
  [[ -r $ARCHIVE ]] || die "cannot read $ARCHIVE"
  if [[ -r $ARCHIVE.sha256 ]]; then
    (cd "$(dirname "$ARCHIVE")" && sha256sum -c "$(basename "$ARCHIVE").sha256") || die "checksum mismatch: $ARCHIVE"
  fi
  echo "--> loading $ARCHIVE"
  "$RUNTIME" load -i "$ARCHIVE"
fi
"$RUNTIME" image inspect "$IMAGE" >/dev/null 2>&1 || die "image $IMAGE is not loaded (use --archive FILE)"

echo "--> taking the host files from $IMAGE"
kit=$(mktemp -d)
trap 'rm -rf "$kit"' EXIT
"$RUNTIME" run --rm --network none --entrypoint /bin/tar "$IMAGE" -C /usr/local/share/opencode/host -cf - . \
  | tar -xf - -C "$kit"
for f in opencode opencode-shell opencode-enter opencode-container opencode-container.service sudoers README.md; do
  [[ -f $kit/$f ]] || die "$IMAGE has no host file $f: is it an opencode Shared Terminal image?"
done

install -d -m 0755 "$LIBEXEC" "$CONF_DIR" /etc/opencode
install -m 0755 "$kit/opencode-enter" "$kit/opencode-container" "$LIBEXEC/"
install -m 0755 "$kit/opencode" "$kit/opencode-shell" /usr/local/bin/
install -m 0644 "$kit/README.md" "$CONF_DIR/README.md"
install -m 0644 "$kit/opencode-container.service" "$UNIT"

if [[ ! -e /etc/opencode/opencode.json ]]; then
  install -m 0644 "$kit/opencode.json" /etc/opencode/opencode.json
  echo "--> wrote /etc/opencode/opencode.json: add your LLM endpoint there"
fi

if [[ -e $CONF ]]; then
  # Keep the administrator's settings; follow the image (and anything given on the command line).
  sed -i "s|^IMAGE=.*|IMAGE=$(printf '%q' "$IMAGE")|" "$CONF"
  [[ -n $MOUNTS ]] && sed -i "s|^MOUNTS=.*|MOUNTS=$(printf '%q' "$MOUNTS")|" "$CONF"
  sed -i "s|^RUNTIME=.*|RUNTIME=$(printf '%q' "$RUNTIME_PATH")|" "$CONF"
  echo "--> updated $CONF (image $IMAGE)"
else
  cat >"$CONF" <<EOF
# Shared opencode container. Read by opencode-enter and opencode-container (bash syntax).
# After a change: systemctl restart opencode-container  (stops running opencode sessions)

# Container runtime and image.
RUNTIME=$(printf '%q' "$RUNTIME_PATH")
IMAGE=$(printf '%q' "$IMAGE")
NAME=opencode

# Directories shared with the container, at the same path. Users must have their
# home and their projects inside one of them (space separated).
MOUNTS=$(printf '%q' "${MOUNTS:-/home}")

# host: the container uses the host's network, so an LLM endpoint on localhost or
# the LAN is reached as from the host. Any other value is passed to --network.
NETWORK=host

# auto: trust the host's CA bundle (endpoints with internal certificates). no: don't.
HOST_CA=auto

# Anything else for the runtime, for example limits for many users:
#EXTRA_ARGS=(--memory 32g --pids-limit 8192)
EXTRA_ARGS=()
EOF
  chmod 0644 "$CONF"
  echo "--> wrote $CONF"
fi

if ! getent group "$GROUP" >/dev/null; then
  groupadd -r "$GROUP"
  echo "--> created group $GROUP"
fi
for user in $USERS; do
  usermod -aG "$GROUP" "$user"
  echo "--> added $user to $GROUP"
done

sed "s/@GROUP@/$GROUP/g" "$kit/sudoers" >"$kit/sudoers.out"
visudo -cf "$kit/sudoers.out" >/dev/null || die "the sudoers rule did not validate"
install -m 0440 "$kit/sudoers.out" "$SUDOERS"

systemctl daemon-reload
systemctl enable opencode-container.service >/dev/null
if ((START)); then
  systemctl restart opencode-container.service
  for _ in $(seq 30); do
    [[ $("$RUNTIME" container inspect --format '{{.State.Running}}' opencode 2>/dev/null) == true ]] && break
    sleep 1
  done
  "$LIBEXEC/opencode-container" status
fi

cat <<EOF

Done. Next:
  1. Put your LLM endpoint in /etc/opencode/opencode.json (see the example in the file).
  2. Give people access:  usermod -aG $GROUP <user>   (they log in again afterwards)
  3. As a user, in a project under ${MOUNTS:-/home}:  opencode
Status: $LIBEXEC/opencode-container status   ·   Guide: $CONF_DIR/README.md
EOF
