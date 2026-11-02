#!/bin/bash
# install-host.sh - the openshift-admin command for your user (rootless, no sudo).
#
#   bash install-host.sh --archive @ARCHIVE@     load the image and install
#   bash install-host.sh                         the image is already loaded
#
# Tags the image localhost/openshift-admin:current and installs:
#
#   ~/.local/bin/openshift-admin              the command (openshift_admin is the same)
#   ~/.config/systemd/user/openshift-admin.service   keeps the container running
#
# then (re)starts the container on that image. For a new version run the same
# again with the new archive: the tag moves, the container is recreated and
# ~/.kube and the container's home (volume openshift-admin-home) are kept.
# Older images stay loaded; remove them with podman rmi when you no longer need them.
#
# Options:
#   --archive FILE   podman load -i FILE first
#   --image REF      the image to install (default @IMAGE@)
#   --no-start       install, but do not (re)start the container
#   --uninstall      remove the command and the service (images, the volume and ~/.kube are kept)
set -euo pipefail

IMAGE=@IMAGE@
ARCHIVE=
START=1
UNINSTALL=0
CURRENT=localhost/openshift-admin:current
BIN=$HOME/.local/bin
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
UNIT=openshift-admin.service

die() { echo "install-host.sh: $*" >&2; exit 1; }
warn() { echo "install-host.sh: $*" >&2; }
usage() { sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p}' "$0"; exit "${1:-0}"; }

while (($#)); do
  case $1 in
    --archive) ARCHIVE=${2:?--archive needs a file}; shift 2 ;;
    --image) IMAGE=${2:?--image needs an image reference}; shift 2 ;;
    --no-start) START=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h | --help) usage 0 ;;
    *) echo "install-host.sh: unknown option $1" >&2; usage 64 ;;
  esac
done

[[ $(id -u) != 0 ]] || die "run this as the user who will use openshift-admin, not as root (the container is rootless)"
command -v podman >/dev/null 2>&1 || die "podman is not installed"
user_systemd() { command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; }

if [[ $UNINSTALL == 1 ]]; then
  if user_systemd; then
    systemctl --user disable --now "$UNIT" >/dev/null 2>&1 || true
  fi
  podman rm -f -t 5 openshift-admin >/dev/null 2>&1 || true
  rm -f -- "$UNIT_DIR/$UNIT" "$BIN/openshift-admin" "$BIN/openshift_admin"
  if user_systemd; then systemctl --user daemon-reload; fi
  echo "Removed openshift-admin. Kept: the images, the volume openshift-admin-home and ~/.kube."
  exit 0
fi

if [[ -n $ARCHIVE ]]; then
  [[ -r $ARCHIVE ]] || die "cannot read $ARCHIVE"
  echo "+ podman load -i $ARCHIVE"
  podman load -i "$ARCHIVE" || die "podman load failed"
fi
podman image exists "$IMAGE" || die "image $IMAGE is not loaded (give --archive FILE, or --image REF for the name podman images shows)"
podman tag "$IMAGE" "$CURRENT"
echo "Tagged $IMAGE as $CURRENT"

mkdir -p "$BIN"
cat >"$BIN/openshift-admin.new" <<'LAYERSMITH_OPENSHIFT_ADMIN_COMMAND'
@COMMAND@
LAYERSMITH_OPENSHIFT_ADMIN_COMMAND
chmod 755 "$BIN/openshift-admin.new"
mv -f -- "$BIN/openshift-admin.new" "$BIN/openshift-admin"
ln -sfn openshift-admin "$BIN/openshift_admin"
echo "Installed $BIN/openshift-admin (and openshift_admin)"
mkdir -p "$HOME/.kube" && chmod 700 "$HOME/.kube"

if user_systemd; then
  mkdir -p "$UNIT_DIR"
  cat >"$UNIT_DIR/$UNIT" <<'LAYERSMITH_OPENSHIFT_ADMIN_UNIT'
@UNIT@
LAYERSMITH_OPENSHIFT_ADMIN_UNIT
  systemctl --user daemon-reload
  systemctl --user enable "$UNIT" >/dev/null
  echo "Installed $UNIT_DIR/$UNIT (enabled)"
  # Without lingering, your user's services stop when you log out and start at your next login.
  if [[ $(loginctl show-user "$(id -un)" --property=Linger --value 2>/dev/null || true) != yes ]]; then
    if loginctl enable-linger "$(id -un)" 2>/dev/null; then
      echo "Enabled lingering: the container also runs while you are logged out and starts at boot"
    else
      warn "could not enable lingering; the container runs while you are logged in. To keep it running"
      warn "after logout and start it at boot, an administrator runs: sudo loginctl enable-linger $(id -un)"
    fi
  fi
  if [[ $START == 1 ]]; then
    podman rm -f -t 5 openshift-admin >/dev/null 2>&1 || true
    systemctl --user restart "$UNIT"
  fi
else
  warn "no systemd user session here (su or sudo -u?): no service installed;"
  warn "openshift-admin starts the container itself the first time you run it after each boot"
  if [[ $START == 1 ]]; then "$BIN/openshift-admin" --restart >/dev/null; fi
fi

if [[ $START == 1 ]]; then
  for _ in $(seq 1 100); do
    [[ $(podman container inspect --format '{{.State.Status}}' openshift-admin 2>/dev/null) == running ]] && break
    sleep 0.1
  done
  [[ $(podman container inspect --format '{{.State.Status}}' openshift-admin 2>/dev/null) == running ]] \
    || die "the container did not start; see: journalctl --user -u $UNIT, podman logs openshift-admin"
  echo "The container openshift-admin is running on $IMAGE"
fi

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) warn "$BIN is not in your PATH; add it (echo 'export PATH=\$HOME/.local/bin:\$PATH' >> ~/.bashrc) or log in again" ;;
esac
echo
echo "Done. Type openshift-admin for a shell in the container, or e.g.: openshift-admin oc get nodes"
