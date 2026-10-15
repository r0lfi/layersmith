#!/bin/sh
# Copy every node's run folders to this machine and compare them.
#   ./collect-results.sh node1 node2 node3
set -eu
DEST="${DEST:-/opt/gpu-results}"
REMOTE="${REMOTE:-/opt/gpu-results}"
IMAGE="${IMAGE:-localhost/gpu-benchmark:1.0.0}"
for node in "$@"; do
  echo "== $node"
  rsync -a --ignore-existing "$node:$REMOTE/" "$DEST/"
done
podman run --rm -v "$DEST:/results" "$IMAGE" gpu-bench runs
echo "Compare: podman run --rm -v $DEST:/results $IMAGE gpu-bench compare <run-a> <run-b> [...]"
