#!/bin/bash
# Multi-node nccl-tests with MPI, for clusters whose Slurm runs containers
# through Pyxis/Enroot and PMIx (srun --mpi=pmix). Each Slurm task is one MPI
# rank driving one GPU. Without Pyxis use slurm-dist-nccl.sbatch instead.
#
#   sbatch --nodes=2 slurm-nccl-mpi.sbatch
#
# Pyxis imports the image from a squashfs or registry, e.g.
#   enroot import -o gpu-benchmark.sqsh podman://localhost/gpu-benchmark:1.0.0
#SBATCH --job-name=gpu-bench-nccl-mpi
#SBATCH --nodes=2
#SBATCH --ntasks-per-node=8
#SBATCH --gpus-per-node=8
#SBATCH --exclusive
#SBATCH --time=00:30:00
#SBATCH --output=gpu-bench-nccl-mpi-%j.out
set -euo pipefail
SQSH="${SQSH:-$PWD/gpu-benchmark.sqsh}"
RESULTS="${RESULTS:-/opt/gpu-results}"
# export NCCL_IB_HCA=mlx5_0,mlx5_1 NCCL_SOCKET_IFNAME=ens1f0np0 NCCL_IB_GID_INDEX=3
srun --mpi=pmix --container-image="$SQSH" --container-mounts="$RESULTS:/results" \
  bash -c 'export CUDA_VISIBLE_DEVICES=$SLURM_LOCALID; gpu-bench nccl --test all_reduce,all_gather,reduce_scatter --max-bytes 8G --gpus-per-rank 1 --label mpi-$SLURM_NNODES-nodes'
