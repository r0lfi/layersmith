#!/bin/bash
# Multi-node NCCL bandwidth with Slurm + Podman: one container per node,
# torchrun inside, TCP rendezvous on node 0. Submit from any login node:
#
#   sbatch --nodes=2 slurm-dist-nccl.sbatch
#
# The image must already be loaded on every node (podman load -i ...).
#SBATCH --job-name=gpu-bench-nccl
#SBATCH --nodes=2
#SBATCH --ntasks-per-node=1
#SBATCH --gpus-per-node=8
#SBATCH --exclusive
#SBATCH --time=00:30:00
#SBATCH --output=gpu-bench-nccl-%j.out
set -euo pipefail
export IMAGE="${IMAGE:-localhost/gpu-benchmark:1.0.0}"
export RESULTS="${RESULTS:-/opt/gpu-results}"
export MASTER_ADDR="$(scontrol show hostnames "$SLURM_JOB_NODELIST" | head -n 1)"
export MASTER_PORT="${MASTER_PORT:-29500}"
export NNODES="$SLURM_NNODES"
# Set these for your fabric (examples for a 400G RoCE ConnectX-7 setup):
# export NCCL_SOCKET_IFNAME=ens1f0np0       # bootstrap/control interface
# export NCCL_IB_HCA=mlx5_0,mlx5_1          # the RDMA devices to use
# export NCCL_IB_GID_INDEX=3                # RoCEv2 GID index (check show_gids)
srun --ntasks-per-node=1 --kill-on-bad-exit=1 bash -c '
  export NODE_RANK=$SLURM_NODEID
  RDMA=""; [ -d /dev/infiniband ] && RDMA="--device /dev/infiniband"
  podman run --rm --network=host --ipc=host --device nvidia.com/gpu=all $RDMA \
    --ulimit memlock=-1:-1 --cap-add=IPC_LOCK --security-opt=label=disable --shm-size=16g \
    -v "$RESULTS:/results" \
    -e NNODES -e NODE_RANK -e MASTER_ADDR -e MASTER_PORT -e GPUS_PER_NODE \
    -e NCCL_SOCKET_IFNAME -e NCCL_IB_HCA -e NCCL_IB_GID_INDEX -e NCCL_NET_GDR_LEVEL -e NCCL_DEBUG \
    "$IMAGE" /opt/gpu-bench/scripts/multinode-torchrun.sh --max-bytes 8G'
echo "Results: $RESULTS on node $MASTER_ADDR (latest *_multinode-* run folder)"
