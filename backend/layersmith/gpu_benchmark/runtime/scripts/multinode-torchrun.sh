#!/bin/sh
# Runs INSIDE the container on every node: one torchrun per node, NCCL
# collectives across all GPUs of all nodes. Rank 0 (node 0) writes the run.
#
#   NNODES=2 NODE_RANK=0 MASTER_ADDR=node1 GPUS_PER_NODE=8 multinode-torchrun.sh [gpu-bench dist options]
#
# Slurm sets the values for you (see slurm-dist-nccl.sbatch).
set -eu
NNODES="${NNODES:-${SLURM_NNODES:-1}}"
NODE_RANK="${NODE_RANK:-${SLURM_NODEID:-0}}"
MASTER_ADDR="${MASTER_ADDR:?set MASTER_ADDR to node 0 (hostname or IP on the cluster network)}"
MASTER_PORT="${MASTER_PORT:-29500}"
GPUS_PER_NODE="${GPUS_PER_NODE:-$(nvidia-smi -L 2>/dev/null | wc -l)}"
[ "$GPUS_PER_NODE" -ge 1 ] || { echo "no GPUs visible on $(hostname)" >&2; exit 1; }
echo "node $NODE_RANK/$NNODES on $(hostname): $GPUS_PER_NODE GPU(s), rendezvous $MASTER_ADDR:$MASTER_PORT"
exec torchrun --nnodes "$NNODES" --node-rank "$NODE_RANK" --nproc-per-node "$GPUS_PER_NODE" \
    --master-addr "$MASTER_ADDR" --master-port "$MASTER_PORT" \
    /opt/gpu-bench/bin/gpu-bench dist --label "multinode-${NNODES}n" "$@"
