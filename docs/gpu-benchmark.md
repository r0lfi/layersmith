# NVIDIA GPU & LLM Benchmark

One portable Podman image that benchmarks NVIDIA GPUs and local LLMs, fully
offline. Build it in LayerSmith, download one `.tar`, `podman load` it on a GPU
server and an interactive terminal dashboard opens:

- live GPU status (utilisation, VRAM, temperature, power, clocks)
- LLM token speed and latency with vLLM on models from `/models`
- GEMM compute (FP32, TF32, FP16, BF16, FP8), memory bandwidth (VRAM/HBM,
  PCIe, GPU-to-GPU), NCCL multi-GPU and multi-node tests
- HTML/JSON/CSV reports per run, and side-by-side comparisons of runs

Everything shown is measured on the machine in front of it. Nothing is
simulated, and no number is taken from a datasheet.

## Workflow

1. **LayerSmith → Dashboard → Quick build → NVIDIA GPU & LLM Benchmark.**
   Keep the defaults (all components, CUDA 13.0.2, image
   `localhost/gpu-benchmark:1.0.0`) and press **Build**.
2. When the build is ready, the build page shows image name, tag, image ID,
   archive size, SHA-256 and export status. Press **Download Podman Image
   (.tar)** and, optionally, **SHA-256 file**.
3. Copy both files to the GPU server (USB, scp, internal file share).
4. On the GPU server:

```sh
nvidia-smi                       # the host driver works
nvidia-ctk cdi list              # must list nvidia.com/gpu=all
sha256sum -c gpu-benchmark-1.0.0.tar.sha256
podman load -i gpu-benchmark-1.0.0.tar
podman images                    # localhost/gpu-benchmark  1.0.0
sudo mkdir -p /opt/gpu-results /opt/models

podman run --rm -it \
  --device nvidia.com/gpu=all \
  --security-opt=label=disable \
  --shm-size=16g \
  -v /opt/models:/models:ro \
  -v /opt/gpu-results:/results \
  localhost/gpu-benchmark:1.0.0
```

The dashboard starts automatically. Results land in `/opt/gpu-results` on the
host, one folder per run.

The archive is written with `podman save --format oci-archive`: the whole
image with its layers and metadata (never `podman export`, which would only be
a container's file system). A LayerSmith server that builds with Docker writes
`docker save` output instead, which contains the same OCI layout and which
`podman load` reads as well; the build page names the format.

### The GPU kit: one command on the GPU host

When files over 1 GB cannot be moved, or you want models along, use the
**GPU kit** on the build page instead of steps 2-4:

1. Download models on the **Models** page (see [models.md](models.md)).
2. On the build page, tick the models, press **Prepare image parts**, then
   download **download-all.sh** and run it where LayerSmith is reachable:
   `mkdir gpu-kit && cd gpu-kit && bash ~/Downloads/download-all.sh`. It
   fetches every file (image and model parts of at most 900 MB, the scripts),
   resumes interrupted downloads and checks every part's sha256.
3. Copy the directory to the GPU host and run `bash setup.sh` there.

`setup.sh` checks the host and the free space per disk (archive, podman's
scratch space in `$TMPDIR`, the image store, the models directory), restores
and loads the image, installs each model into `/opt/models/<name>`, generates
the CDI spec with `sudo nvidia-ctk cdi generate` if it is missing, and starts
the dashboard through `start_container.sh`. Every step is skipped once done,
so it is safe to run again; `--no-start` only prepares, `--models-dir` and
`TMPDIR=` move things to another disk. Start the dashboard again later with
`bash start_container.sh` (`GPUS=0`, `MODELS_DIR=...`, or a `gpu-bench`
command as arguments).

Only the image, without models or setup: **Download split archive** under
Image archive and `./assemble-image.sh`; see [split-archive.md](split-archive.md).

## Host requirements

The image contains CUDA user space only. The GPU server provides:

| Requirement | Check |
|---|---|
| NVIDIA driver ≥ 580.65.06 (CUDA 13.0 image) or ≥ 570.26 (CUDA 12.8 image) | `nvidia-smi` |
| NVIDIA Container Toolkit with CDI specs | `nvidia-ctk cdi list` shows `nvidia.com/gpu=all` |
| Podman 4.x or newer | `podman --version` |

If `nvidia-ctk cdi list` is empty, generate the spec once as root:
`nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml`. LayerSmith and the
image never install or change drivers, kernel modules or host packages.

**SELinux:** `--security-opt=label=disable` lets the container use the GPU
device nodes and the `/models` bind mount without relabelling your model
files. If you prefer labels, drop that option and mount with `:z` — be aware
that `:z` relabels the host directory.

**Privileges:** single-node tests need no `--privileged`, no capabilities and
no Docker or Podman socket. `/models` is mounted read-only; nothing in it is
ever written or deleted.

## Versions (compatibility matrix)

| Image | Base | PyTorch | vLLM | Driver | GPU kernels |
|---|---|---|---|---|---|
| CUDA 13.0.2 (default) | `docker.io/nvidia/cuda:13.0.2-base-ubuntu24.04` | 2.11.0 (PyPI, CUDA 13.0 build) | 0.26.0 (PyPI CUDA 13 wheel, pins torch 2.11.0) | ≥ 580.65.06 | sm_75 … sm_120 + PTX |
| CUDA 12.8.2 | `docker.io/nvidia/cuda:12.8.2-base-ubuntu24.04` | 2.11.0+cu128 | not installed | ≥ 570.26.00 | sm_75 … sm_120 + PTX |

Hopper (H100/H200, sm_90) and Blackwell (B200 sm_100, RTX PRO 6000 sm_120) are
native targets. Other pins: Python 3.12.14 or 3.13.15 (python-build-standalone
via uv 0.12.17), Transformers 5.17.0, Tokenizers 0.23.3, Textual 8.2.8,
nvidia-ml-py 13.580.82, nccl-tests v2.21.1 (NCCL 2.28.9 from the PyTorch
wheels), nvbandwidth v0.10.0. Every resolved Python package is recorded in
`/opt/gpu-bench/requirements.lock` inside the image.

vLLM's PyPI CUDA 12 wheel is built for CUDA 12.9, so the 12.8 image has no
vLLM rather than a mismatched one. `pip check` runs during the build.

**Size:** the full image is about 10 GB unpacked as Podman counts it, up to
15 GB as a Docker server reports it; the `.tar` is about 4.6 GB. CUDA
libraries in PyTorch (cuDNN, cuBLAS, NCCL …) and vLLM are most of it.
Loading it needs the unpacked size free in podman's image store
(`podman info --format '{{.Store.GraphRoot}}'`) plus the archive size in
`$TMPDIR` (default `/var/tmp`) while `podman load` runs. The LayerSmith server needs roughly 2.5× that free (image layers plus the
exported archive); a GPU build stops before pulling anything when the space
is not there (`LAYERSMITH_GPU_MIN_FREE_GB` overrides the check).

## The dashboard (TUI)

Keys: `0` overview · `1` GPU info & health · `2` LLM token speed · `3` GPU
compute · `4` memory bandwidth · `5` multi-GPU/NCCL · `6` multi-node · `7` full
benchmark · `8` previous results · `9` compare · `L` logs · `R` HTML report ·
`S` stop the running test · `P` pause the live display · `F5` rescan GPUs and
models · `Q` quit. `Ctrl+C` stops a running test (after confirmation) or quits.

It works over plain SSH in any terminal of 80×24 or more. It needs `-it`;
without a terminal the image prints how to start it, and every function is
also available as a CLI command.

Each benchmark the dashboard starts is the `gpu-bench` CLI in a subprocess
(same code, same reports). Stopping sends it SIGTERM: it stops vLLM or
nccl-tests (their own process groups), keeps what was measured, marks it
`STOPPED` and writes the report. Child processes also get SIGTERM if the
dashboard itself dies, and `tini` reaps everything, so no orphaned GPU
processes stay behind.

## LLM token speed

`gpu-bench llm --model /models/<dir>` (or page 2) starts a vLLM OpenAI server
on 127.0.0.1 inside the container and drives it with a streaming load
generator:

- prompts are random token IDs of exactly `--input-tokens`, avoiding the
  tokenizer's special tokens, unique per request (prefix caching is off), so
  prompt length is exact and no cache inflates the numbers;
- generation is greedy with `ignore_eos` and `min_tokens = max_tokens`, so
  every request produces exactly `--output-tokens`;
- token counts come from the server's own usage records on every chunk
  (`continuous_usage_stats`), and vLLM's Prometheus counters are read before
  and after each level to cross-check the totals (`server_counts_match_client`);
- warmup requests run before each level and are not measured.

Two kinds of results, never mixed:

| | Meaning |
|---|---|
| **Single request** (concurrency 1) | How fast one stream generates: per-request output tok/s = (output tokens − 1) / (end − first token). Also TTFT and a prefill estimate (prompt tokens / TTFT, includes HTTP overhead). |
| **Aggregate** (concurrency N) | All output tokens of all concurrent requests / measured wall time. |

Per level the report also has total tok/s (input + output), request
throughput, TTFT, TPOT, inter-token latency and end-to-end latency as mean,
P50, P90, P95, P99, completed and failed requests, and NVML samples of GPU
utilisation, peak VRAM and power during the measured window. The live view
shows a 5-second throughput window and running TTFT percentiles, labelled as
running figures; final results are only those per completed level.

Defaults: 1024 in / 256 out, concurrency `1,8,32`, 10 requests at
concurrency 1 and 64 at each other level, the model's own dtype, tensor
parallel size = number of selected GPUs. `--trust-remote-code` is off unless
you ask for it (it runs Python shipped inside a model folder).

Models: Hugging Face model directories (`config.json` plus `*.safetensors`)
anywhere up to three levels under `/models`. Nothing is downloaded:
`HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE` and vLLM usage stats are off in the
image. If no model is found the dashboard says so; compute, memory and NCCL
tests still work.

When comparing GPUs, use the same model, precision, token lengths,
concurrency levels and request counts. The comparison warns when any differ.

## Compute, memory and NCCL

**Compute** (`gpu-bench compute`): square GEMMs (default 8192, 10 s per
precision) on all selected GPUs at once, timed with CUDA events after warmup;
TFLOPS per GPU and total. FP32 runs with TF32 off; TF32 is the same FP32 GEMM
with TF32 tensor cores allowed; FP8 (`torch._scaled_mm`, e4m3) needs compute
capability 8.9+ and reports `UNAVAILABLE` otherwise. These are measured dense
GEMM figures, not datasheet or sparsity peaks.

**Memory** (`gpu-bench memory`), kept strictly apart:

- VRAM/HBM: on-GPU copy (counts read + write bytes) and a read-only reduction;
- host ↔ device over PCIe (or NVLink-C2C): pinned and pageable, both ways;
- GPU ↔ GPU: a copy matrix with peer-access status (NVLink or PCIe, per
  topology);
- NVIDIA nvbandwidth (copy-engine and SM copy tests) as an independent
  reference.

Units are GB/s (10⁹ bytes/s); GiB/s are in the JSON as well.

**NCCL on one server** (`gpu-bench nccl`): nccl-tests `all_reduce`,
`all_gather`, `reduce_scatter`, `broadcast`, `sendrecv` (and `alltoall`) over
the selected GPUs in one process, 8 B to 1 GiB. Reported: algbw and busbw per
message size, peak and average busbw, and nccl-tests' data check. busbw
applies each collective's correction factor; neither number is raw link
speed. Page 1 shows the GPU topology (`nvidia-smi topo -m`, or NVML when
nvidia-smi is not mounted) and P2P support.

## Multi-node and Slurm

The same image runs on every node. Two launch methods:

**torchrun (recommended with Podman).** `gpu-bench dist` runs NCCL
collectives through `torch.distributed`, timed like nccl-tests (slowest rank,
same busbw factors). torchrun's TCP rendezvous on node 0 needs no MPI and no
SSH between containers. **mpirun** (`gpu-bench nccl --hostfile …`) runs the
MPI build of nccl-tests for clusters that already provide SSH between
containers or Slurm with PMIx/Pyxis.

### Container options for RDMA

```sh
podman run --rm -it --network=host --ipc=host \
  --device nvidia.com/gpu=all --device /dev/infiniband \
  --ulimit memlock=-1:-1 --cap-add=IPC_LOCK \
  --security-opt=label=disable --shm-size=16g \
  -v /opt/gpu-results:/results localhost/gpu-benchmark:1.0.0
```

- `--network=host`: NCCL bootstrap and RoCE need the host's interfaces and GIDs.
- `--device /dev/infiniband`: the verbs devices (`uverbs*`, `rdma_cm`).
- `--ulimit memlock=-1:-1` and `--cap-add=IPC_LOCK`: RDMA pins (registers)
  memory. These are the only extra rights; `--privileged` is not needed.
- Use rootful Podman for RDMA: rootless Podman cannot raise memlock above the
  host user's hard limit.
- GPUDirect RDMA additionally needs `nvidia-peermem` or dma-buf support on
  the host; NCCL works without it (with staging through host memory).

### NCCL environment

| Variable | Typical value (400G RoCE, ConnectX-7) | Purpose |
|---|---|---|
| `NCCL_SOCKET_IFNAME` | `ens1f0np0` | interface for bootstrap/control traffic |
| `NCCL_IB_HCA` | `mlx5_0,mlx5_1` | RDMA devices NCCL may use |
| `NCCL_IB_GID_INDEX` | `3` | RoCE v2 GID index (see `check-rdma.sh`) |
| `NCCL_NET_GDR_LEVEL` | `PHB` | how far apart GPU and NIC may be for GPUDirect |
| `NCCL_DEBUG` | `INFO` | verbose NCCL logs |

Pass them with `-e`. Multi-node runs log NCCL's INIT/NET subsystem
automatically and the result states which network NCCL really used:
**RDMA (IB verbs: InfiniBand or RoCE)** or **TCP sockets (no RDMA)**.

### Checking that RoCE/RDMA works

```sh
podman run --rm --network=host --device /dev/infiniband localhost/gpu-benchmark:1.0.0 \
  /opt/gpu-bench/scripts/check-rdma.sh         # devices, port state, RoCE GIDs, memlock
# raw link bandwidth between two nodes, outside NCCL:
#   node2: ib_write_bw -d mlx5_0 -x 3 --report_gbits
#   node1: ib_write_bw -d mlx5_0 -x 3 --report_gbits node2
```

Then run a multi-node NCCL test and confirm the "NCCL network" line says RDMA.

### Running across nodes

Scripts are in the image under `/opt/gpu-bench/scripts`
(`podman cp` them out, or run them from inside):

| Script | Where | What |
|---|---|---|
| `podman-multinode.sh` | each host | starts the container with GPU/RDMA options and torchrun |
| `multinode-torchrun.sh` | in the container | one torchrun per node, `gpu-bench dist` |
| `slurm-dist-nccl.sbatch` | `sbatch` | Slurm + Podman, one container per node, torchrun |
| `slurm-nccl-mpi.sbatch` | `sbatch` | Slurm + Pyxis/Enroot + PMIx, nccl-tests with MPI |
| `slurm-gpu-bench.sbatch` | `sbatch` | the full single-node benchmark on every node |
| `check-rdma.sh` | either | RDMA devices, GIDs, memlock, perftest hints |
| `collect-results.sh` | head node | rsync every node's run folders, list them for `compare` |

By hand on two nodes:

```sh
# node1 (rank 0)
NNODES=2 NODE_RANK=0 MASTER_ADDR=node1 ./podman-multinode.sh --max-bytes 8G
# node2
NNODES=2 NODE_RANK=1 MASTER_ADDR=node1 ./podman-multinode.sh --max-bytes 8G
```

With Slurm: `sbatch --nodes=2 slurm-dist-nccl.sbatch` (load the image on every
node first). Only rank 0 writes results, into node 0's `/opt/gpu-results`.
The dashboard's page 6 starts the local node's torchrun and shows the command
for the other nodes.

**LLM inference across nodes is not offered.** vLLM multi-node serving needs
its own Ray cluster spanning the nodes; a working NCCL fabric does not mean
that works, so the image does not pretend it does. LLM tests run per node
(tensor parallel across that node's GPUs), and communication and inference
results are reported separately.

## Results and reports

```text
/results/
  2026-10-15_120000_h200x2/
    benchmark.json  benchmark.csv  benchmark.html  system-info.json
    logs/  (one log per command, plus vllm-server.log)
  comparisons/2026-10-15_130500/
    compare.html  compare.json  compare.csv
```

Every command creates a new run folder (time + GPU model × count + optional
`--label`); nothing is ever overwritten. `--run-dir` adds to an existing run.
Results are saved as each test finishes, so a stopped or crashed run keeps
what it measured.

- `benchmark.html`: a dark, self-contained dashboard (inline CSS and SVG
  charts, no scripts, no CDN) — opens offline from the results folder.
- `benchmark.csv`: one row per metric (run, GPU, test, status, metric, value,
  unit, parameters) for spreadsheets.
- `benchmark.json` / `system-info.json`: everything, including per-GPU and
  per-message-size data and the image's recipe.

**Compare** (`gpu-bench compare RUN RUN [RUN…]` or page 9): headline metrics
side by side with percentage change against the first run, coloured by
whether higher or lower is better, and warnings when models, precision,
token lengths, concurrency, request counts, GPU models or counts, matrix
sizes, driver or software versions differ. Different GPU counts are compared
as complete configurations and flagged as such.

## CLI

```text
gpu-bench tui | info | health | topology | models | runs
gpu-bench llm --model /models/<m> [--input-tokens --output-tokens --concurrency 1,8,32 --requests --gpus 0,1 ...]
gpu-bench compute [--precision fp32,tf32,fp16,bf16,fp8 --duration 10 --size 8192 --gpus]
gpu-bench memory [--gpus --size-mb --no-peer]
gpu-bench nccl [--test all --min-bytes 8 --max-bytes 1G --gpus | --hostfile/--hosts --ranks]
gpu-bench dist [--test --max-bytes]        # under torchrun, multi-node
gpu-bench full [--model /models/<m>] [--skip nccl,llm]
gpu-bench report [RUN|latest] [--compare RUN ...]
gpu-bench compare RUN RUN [...]
```

Exit status: 0 when every test passed, 1 when one failed (including a failed
health check, e.g. no GPU), 130 when stopped, 2 for usage errors. Scripted,
without the dashboard:

```sh
podman run --rm --device nvidia.com/gpu=all --security-opt=label=disable --shm-size=16g \
  -v /opt/models:/models:ro -v /opt/gpu-results:/results localhost/gpu-benchmark:1.0.0 \
  gpu-bench full --model /models/<model>
```

## Offline guarantees

The build needs network access (or the mirrors under *Internal build
mirrors*). The finished image does not: no `pip`/`apt` at start-up, no model
downloads, Hugging Face offline mode, vLLM usage stats off, no external
resources in the TUI or reports. Triton and FlashInfer kernels that vLLM
compiles on first use are compiled locally (nvcc is in the image) into
`/cache`. Multi-node traffic stays on the cluster network you configure.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `No NVIDIA GPU is visible` | Missing `--device nvidia.com/gpu=all`, or no CDI spec: `nvidia-ctk cdi generate`. |
| `CUDA driver compatibility: FAIL` | Host driver older than the image needs; use the CUDA 12.8 image or upgrade the host driver. |
| TUI does not start | Add `-it`. Over SSH make sure the terminal is at least 80×24. |
| Results folder read-only | Create `/opt/gpu-results` on the host; with SELinux use `--security-opt=label=disable` or `:Z`. |
| vLLM runs out of memory | Lower `--gpu-memory-utilization`, use more GPUs (`--tensor-parallel-size`), or shorter `--max-model-len`. |
| NCCL uses TCP sockets | `--device /dev/infiniband`, `--network=host`, memlock, `NCCL_IB_HCA`, `NCCL_IB_GID_INDEX`; check with `check-rdma.sh`. |
| `/dev/shm` warning | Use `--shm-size=16g` (or `--ipc=host`). |
| `statfs /models: no such file or directory` | The host folder in `-v <host>:/models:ro` does not exist: `sudo mkdir -p /opt/models`, or drop that `-v` to run without models. `start_container.sh` skips the mount when the folder is missing. |
| `unresolvable CDI devices nvidia.com/gpu=all` | No CDI spec: `sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml` (needs the NVIDIA Container Toolkit and a working `nvidia-smi`). `setup.sh` does this for you. |
| `podman load`: `no space left on device` | The archive is fine; the image store or `$TMPDIR` is full. Free space, `TMPDIR=/bigdisk/tmp podman load ...`, or move `graphroot` in `storage.conf`. `setup.sh` checks this before it starts. |
