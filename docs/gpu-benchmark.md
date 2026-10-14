# NVIDIA GPU Benchmark Toolkit

**NVIDIA GPU Benchmark Toolkit** is a first-party image template in LayerSmith's
**Create image → AI / ML / GPU** category. It builds an ordinary OCI image that
can be exported, published, moved to another host, and run with Docker or
Podman. The default image is `layersmith/gpu-benchmark:1.0.0`.

## Build the image in LayerSmith

Choose CUDA, Python, tool groups, optional internal build mirrors, and an image
repository/tag in the wizard. LayerSmith shows the generated Containerfile and
streams the normal image build log. The CUDA base, Python, PyTorch, vLLM,
Transformers, Tokenizers, CUDA Samples, and nccl-tests versions are pinned in
the recipe. CUDA 13.0.2 is the default and includes vLLM; CUDA 12.8.2 is
available for non-LLM benchmark groups. The host provides NVIDIA kernel drivers:
the image does not install a kernel driver.

The four selectable groups are:

| Group | Contents |
| --- | --- |
| LLM | PyTorch CUDA, vLLM, Transformers, Tokenizers, NVML |
| Compute | CUDA Toolkit and CUDA-event PyTorch GEMM tests |
| Memory | CUDA Samples `bandwidthTest` and PCIe/device-copy tests |
| NCCL | NCCL tests, OpenMPI, RDMA/RoCE diagnostics |

PyTorch and NVML are included as the runtime foundation in every recipe.
vLLM is only available in the CUDA 13.0.2 recipe. A build without a selected
test group marks that test `SKIPPED` in `gpu-bench full`.

## Host requirements

- Runtime hosts can use AlmaLinux 9/10, RHEL 9/10, or another Linux release
  supported by the installed NVIDIA driver and container runtime. The toolkit
  targets H200 (Hopper) and RTX PRO 6000 Blackwell as well as other CUDA GPUs;
  actual support depends on the selected CUDA recipe and host driver. The image
  recipe currently targets Linux `amd64`.
- An NVIDIA GPU and a host NVIDIA driver compatible with the image's CUDA
  runtime. The CUDA development image includes user-space libraries and `nvcc`,
  not the host driver or kernel modules.
- Use at least NVIDIA driver `580.65.06` for the default CUDA 13.0 recipe or
  `570.26.00` for CUDA 12.8. These are the Linux driver versions associated
  with the CUDA Toolkit GA releases; `gpu-bench health` checks the selected
  runtime against these minimums.
- Docker with NVIDIA Container Toolkit (`--gpus all`), or Podman with NVIDIA
  CDI configured (`--device nvidia.com/gpu=all`).
- Enough shared memory for PyTorch/vLLM (`--ipc=host` is used in the examples).
- A local model directory for LLM tests. Models are never downloaded at
  runtime; the image sets Hugging Face offline mode.

Check the host driver/runtime pairing before moving the image to an offline
system. `gpu-bench health` reports CUDA/PyTorch/driver failures separately from
optional NCCL, RDMA, and interface capabilities.

## Run with Podman

With NVIDIA CDI configured on the host:

```bash
mkdir -p models results
podman run --rm -it \
  --device nvidia.com/gpu=all \
  --security-opt=label=disable \
  --ipc=host --uts=host \
  -v ./models:/models:ro,Z \
  -v ./results:/results:Z \
  layersmith/gpu-benchmark:1.0.0 \
  gpu-bench full --model /models/llama-8b --output /results
```

`--security-opt=label=disable` is useful with CDI GPU devices on SELinux
systems. For bind mounts with SELinux separation enabled, use `:Z` for a
private relabel or `:z` when multiple containers share the directory. Relabels
change the host directory's SELinux label; use paths dedicated to this
container. The model mount is read-only.

## Run with Docker

```bash
mkdir -p models results
docker run --rm -it --gpus all \
  --ipc=host --uts=host \
  -v ./models:/models:ro,Z \
  -v ./results:/results:Z \
  layersmith/gpu-benchmark:1.0.0 \
  gpu-bench full --model /models/llama-8b --output /results
```

Docker requires NVIDIA Container Toolkit on the host for `--gpus all`. On
SELinux hosts, use `:Z` or `:z` on bind mounts when container label separation
is enabled. Rootless runtimes may also need host ownership/group permissions
for the results directory.

## CLI

```bash
gpu-bench --help
gpu-bench info
gpu-bench health
gpu-bench compute --precision bf16 --duration 60
gpu-bench memory
gpu-bench nccl --test all_reduce --gpus 8
gpu-bench llm --model /models/llama-8b \
  --input-tokens 1024 --output-tokens 256 \
  --concurrency 1,8,16,32 --requests 100 --warmup-requests 5
gpu-bench full --model /models/llama-8b --output /results
gpu-bench report --input /results/benchmark.json \
  --compare /results/previous/benchmark.json --output /results/comparison
```

`info` reports the visible GPU models/count, total and free VRAM, host driver,
CUDA version, compute capability, PCIe link, GPU topology, NVLink status, power
and temperature. `health` marks missing CUDA/driver/PyTorch/GPU memory as
failures, and unavailable NCCL/RDMA/network features as optional capabilities.

`full` writes `benchmark.json`, `benchmark.csv`, `benchmark.html`, and
`system-info.json`. Individual commands append their result to the JSON/CSV/HTML
bundle in the output directory. Missing optional hardware, tools, or model
paths are recorded as `SKIPPED` with a reason. Errors from a requested test are
recorded as `FAILED`; the report remains available.

## LLM inference measurements

The LLM command starts a local vLLM server against the mounted model directory,
then uses vLLM's `vllm bench serve` with the random dataset. It runs the
configured warmup requests separately before each measured concurrency level.
The report records output and total tokens/s, request throughput, tokens/s per
configured concurrent request, TTFT, TPOT, end-to-end latency percentiles,
GPU memory/utilization samples, model, precision, token lengths, and request
counts. The reported tokens/s-per-request is aggregate output throughput
divided by that run's concurrency cap; it is not an individual request's
measured decode rate.

The model path must be a local directory containing `config.json` and its
weights/tokenizer files. For air-gapped hosts, copy the complete model into the
host's `models/` directory and mount it at `/models`. No Hugging Face download
is attempted.

## Compute and memory measurements

Compute uses CUDA events with synchronization for measured GEMM iterations and
reports measured TFLOPS for FP32, FP16, or BF16. Warmup is excluded. Values are
observed benchmark throughput, not theoretical GPU peak performance.

Memory results distinguish host-to-device and device-to-host PCIe transfers
from device-to-device GPU memory copies. If CUDA Samples `bandwidthTest` is
available it also records its measured copy values as a legacy reference.
NVIDIA marks that sample outdated and inaccurate and recommends NVBandwidth for
current platform measurements; the CUDA-event PyTorch transfer measurements
remain the primary results in this image. A portable pointer-chase
memory-latency kernel is not included, so latency is explicitly marked
`SKIPPED` instead of presenting launch overhead as HBM latency.

## NCCL, multiple GPUs, multiple nodes, and Slurm

On one server, select all GPUs and run, for example:

```bash
gpu-bench nccl --test all_reduce --gpus 8 --min-bytes 8 --max-bytes 128M
```

`gpu-bench nccl --test all` covers all-reduce, all-gather, reduce-scatter, and
broadcast. NCCL `algbw` and `busbw` are reported separately as collective test
metrics. They are not raw physical-link measurements.

For a multi-node run, every rank must see its GPU, the same compatible image
and NCCL binary, working host-to-host IP connectivity, and the required RDMA
device/provider. GPU CDI or Docker `--gpus all` alone does not create an RDMA
path between containers. Configure the host fabric and container networking
first. A common container runtime setup uses host networking and explicitly
exposes the host's RDMA character devices, for example:

```bash
podman run --rm --network=host --device nvidia.com/gpu=all \
  --device=/dev/infiniband/uverbs0 --device=/dev/infiniband/rdma_cm \
  --ipc=host --security-opt=label=disable \
  -e NCCL_DEBUG=INFO \
  -e NCCL_SOCKET_IFNAME=ens1f0 \
  -e NCCL_IB_HCA=mlx5_0 \
  layersmith/gpu-benchmark:1.0.0 \
  gpu-bench nccl --test all_reduce --gpus 8
```

Replace the RDMA device names, NIC, HCA, and GPU count with values from each
host. Use the same runtime configuration on all participating hosts. Where a
cluster uses independent per-node containers, use its MPI container launcher
or Slurm container integration; the CLI does not make separate containers
communicate automatically.

For OpenMPI environments where remote ranks can resolve the same
`/opt/gpu-bench/bin` executable and libraries (for example, through a shared
mount or a site-configured container launcher), the CLI accepts `--hosts`,
`--hostfile`, and `--ranks`:

```bash
NCCL_DEBUG=INFO NCCL_SOCKET_IFNAME=ens1f0 NCCL_IB_HCA=mlx5_0 \
  gpu-bench nccl --test all_reduce --gpus 1 --hosts node-a:8,node-b:8 --ranks 16
```

OpenMPI remote launch requires SSH/rank setup and a compatible runtime on every
node. Verify name resolution, ports, firewall rules, device access, and the
container's ability to reach the selected host interfaces before benchmarking.

For a Slurm cluster with a site-supported OCI container integration such as
Pyxis/Enroot, make the image available to all compute nodes, bind the model and
results directories, and run one rank per GPU. `gpu-bench nccl` runs the
nccl-tests executable in each task and writes the report only from Slurm rank
zero; run `gpu-bench full` once outside the multi-rank task step. Example
(adapt the container image and mounts to the site's plugin):

```bash
srun --mpi=pmix --nodes=2 --ntasks-per-node=8 --gpus-per-task=1 \
  --container-image=layersmith/gpu-benchmark:1.0.0 \
  --container-mounts=/shared/models:/models,/shared/results:/results \
  --container-env=NCCL_DEBUG,NCCL_SOCKET_IFNAME,NCCL_IB_HCA \
  gpu-bench nccl --test all_reduce --gpus 1 --output /results
```

Slurm must expose the correct GPU to each task and provide compatible PMIx/MPI
integration to the container. Set fabric variables in the Slurm job or
container environment. Useful NCCL settings include `NCCL_DEBUG=INFO`,
`NCCL_SOCKET_IFNAME=<interface>`, and `NCCL_IB_HCA=<HCA>`; none are hard-coded
in the image. `--network=host`, `--device=/dev/infiniband/...`, memlock limits,
and `IPC_LOCK` capabilities depend on the cluster's security and fabric setup.

## Offline and internal-mirror workflows

These are separate stages:

1. **Online image build:** LayerSmith needs access to the selected CUDA base,
   Ubuntu package repositories, the configured Python package indexes, the
   CPython distribution mirror, and selected source repositories.
2. **Build through internal mirrors:** choose internal Ubuntu APT/security,
   Python package, PyTorch wheel, CPython distribution, CUDA Samples, and
   nccl-tests mirrors in the wizard. Configure the container
   builder's registry mirror for `nvidia/cuda`. Mirror the exact pinned tags
   and CUDA/Python wheels required by the recipe. The mirror endpoints must be
   reachable from the LayerSmith build backend; they are only used while the
   image is built.
3. **Offline runtime:** export the built image or create an air-gap bundle,
   transfer it to the target host, and load it with `podman load` or `docker
   load`. Transfer local models separately and mount them read-only. Runtime
   package installation and model downloads are disabled.

For stronger offline reproducibility, mirror the exact upstream images and
source tags into an internal registry/Git mirror, retain the resulting image
digest, and test the image build from that mirror before disconnecting the
build network. If a network mirror uses an internal CA, install that CA in the
LayerSmith build host and builder before building. The OCI air-gap bundle
contains the built image, Containerfile, manifest, checksums, toolkit README,
and recipe metadata; it does not bundle model weights or build dependencies.
