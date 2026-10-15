#!/bin/sh
# Runs on the HOST of every node (after `podman load -i gpu-benchmark-X.Y.Z.tar`).
# Starts the benchmark container with GPUs, RDMA devices and host networking,
# then multinode-torchrun.sh inside it. Run it on all nodes within a minute.
#
#   NNODES=2 NODE_RANK=0 MASTER_ADDR=node1 ./podman-multinode.sh            # on node1
#   NNODES=2 NODE_RANK=1 MASTER_ADDR=node1 ./podman-multinode.sh            # on node2
#
# Needs rootful Podman for RDMA (memlock), NVIDIA CDI, and /dev/infiniband.
set -eu
IMAGE="${IMAGE:-localhost/gpu-benchmark:1.0.0}"
RESULTS="${RESULTS:-/opt/gpu-results}"
mkdir -p "$RESULTS"
RDMA=""
[ -d /dev/infiniband ] && RDMA="--device /dev/infiniband"
# shellcheck disable=SC2086
exec podman run --rm --network=host --ipc=host \
    --device nvidia.com/gpu=all $RDMA \
    --ulimit memlock=-1:-1 --cap-add=IPC_LOCK \
    --security-opt=label=disable --shm-size=16g \
    -v "$RESULTS:/results" \
    -e NNODES -e NODE_RANK -e MASTER_ADDR -e MASTER_PORT -e GPUS_PER_NODE \
    -e NCCL_SOCKET_IFNAME -e NCCL_IB_HCA -e NCCL_IB_GID_INDEX -e NCCL_NET_GDR_LEVEL -e NCCL_DEBUG \
    "$IMAGE" /opt/gpu-bench/scripts/multinode-torchrun.sh "$@"
