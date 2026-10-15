#!/bin/bash
# The full single-node benchmark on every node of the allocation, one run
# folder per node, so nodes can be compared afterwards with gpu-bench compare.
#
#   sbatch --nodes=2 --export=ALL,MODEL=/models/your-model slurm-gpu-bench.sbatch
#SBATCH --job-name=gpu-bench-full
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --gpus-per-node=8
#SBATCH --exclusive
#SBATCH --time=02:00:00
#SBATCH --output=gpu-bench-full-%j-%N.out
set -euo pipefail
export IMAGE="${IMAGE:-localhost/gpu-benchmark:1.0.0}"
export RESULTS="${RESULTS:-/opt/gpu-results}"
export MODELS="${MODELS:-/models}"
export MODEL="${MODEL:-}"
srun --ntasks-per-node=1 bash -c '
  ARGS=""; [ -n "$MODEL" ] && ARGS="--model $MODEL"
  podman run --rm --device nvidia.com/gpu=all --security-opt=label=disable --shm-size=16g \
    -v "$MODELS:/models:ro" -v "$RESULTS:/results" "$IMAGE" \
    gpu-bench full $ARGS --label "$(hostname -s)"'
