# AI / ML / GPU images

GPU-optimised OCI images for AI, machine learning, CUDA and HPC, built on a
connected LayerSmith host and run on air-gapped GPU servers with Podman and
Slurm. It is a category of the ordinary **Create image** wizard: the result is
a normal LayerSmith build, with the same immutable versions, live log, export,
air-gap bundle and scanning as any other.

**Dashboard → Quick build → AI / ML / GPU**, or **Create image → AI / ML / GPU**.

## Templates

| Template | Base image (verified 2026-10-27) | Purpose |
| --- | --- | --- |
| NVIDIA PyTorch | `nvcr.io/nvidia/pytorch:26.09-py3` (also 26.08, 26.07) | AI, ML, deep learning |
| NVIDIA CUDA | `docker.io/nvidia/cuda:13.4.1-{devel,runtime,base}-ubuntu24.04`, `13.4.1-devel-rockylinux10`, `13.0.2-devel-ubuntu24.04` | CUDA development, GPU tests |
| vLLM | `docker.io/vllm/vllm-openai:v0.32.0` (CUDA 13.0), `v0.32.0-cu129`, `v0.31.0` | LLM inference, OpenAI-compatible API |
| TensorFlow GPU | `docker.io/tensorflow/tensorflow:2.21.0-gpu`, `2.21.0-gpu-jupyter` | Machine learning |
| Custom AI Container | any image with a tag or digest | Your own environment |

Every listed version is pinned by digest, and its CUDA, framework and Python
versions were read from the image's own environment and its publisher's
release notes - nothing is inferred from a tag's name. **Show tags in the
registry** lists what the registry offers now; tags LayerSmith has not
verified are built through the Custom AI Container, where the versions are
read from the image after the build.

NVIDIA PyTorch 26.09-py3 contains PyTorch 2.14.0a0+b2c75dd built for CUDA
13.4.1, Python 3.12, Ubuntu 24.04, cuDNN 9.26.0, NCCL 2.31.2, TensorRT 11.3,
Transformer Engine 2.19, JupyterLab 4.6.3 and FlashAttention 2.8.3 plus 4.0.0b24
(beta). The wizard lists the Python packages each verified image already has
(read from its layers), so nothing is installed twice.

TensorFlow is marked *limited*: NVIDIA stopped publishing NGC TensorFlow
containers after 25.02, and TensorFlow 2.21's own image is built on CUDA 12.3.
Its basic test reads the kernels it was compiled for: `sm_60 sm_70 sm_80
sm_89` and `compute_90` PTX. Neither the H200 (sm_90) nor the RTX PRO 6000
(sm_120) has native kernels in it; both run it only through PTX JIT
compilation, which TensorFlow does not document - validate it with the GPU
test before relying on it.

## Import or build

- **Import unchanged** - `FROM <image>@<digest>` and LayerSmith's OCI labels.
  No `RUN`, `COPY` or `ENV`: every layer is the publisher's.
- **Build a custom image** - the same base, plus the Python tools you tick and
  any `name`, `name==1.2.3` or `name>=1,<2` you type. Nothing is installed
  unless you choose it.

Python packages are installed by `layersmith-aiml-pip` at build time:

1. Every package the base image was built with and pip must not replace -
   PyTorch, torchvision, Triton, TensorRT, Transformer Engine, FlashAttention,
   `nvidia-*`, `cuda-*`, anything with an NVIDIA local version (`+nv26.9`), and
   for the NVIDIA PyTorch image NumPy - is pinned at its installed version in
   a constraints file. NVIDIA's `/etc/pip/constraint.txt` cannot do this: in
   the 26.09 image it is empty.
2. `pip check` is recorded before the install, so conflicts the base already
   had are not blamed on your packages.
3. pip installs with those constraints. A package that needs another version
   of a protected one fails the build there, with the reason.
4. Protected versions are compared afterwards, and any new `pip check`
   problem fails the build.

Requesting a protected package directly (`torch==…`) is refused in the
wizard. Package input is a strict subset of PEP 508: no URLs, paths, VCS
references, environment markers or pip options. An internal index can be set
per image (`https://` without credentials; plain `http://` is warned about).

Custom images also get offline defaults (`HF_HUB_OFFLINE=1`,
`TRANSFORMERS_OFFLINE=1`, `HF_DATASETS_OFFLINE=1`, telemetry off), so the
container never reaches for the network on the air-gapped side.

## Tests

| Test | Runs on | What it checks |
| --- | --- | --- |
| Basic | LayerSmith build server, after every build | Python version, PyTorch/TensorFlow import, CUDA version, the GPU architectures the kernels were compiled for, installed libraries, `pip check` |
| GPU | a GPU worker | `nvidia-smi`, `torch.cuda.is_available()`, GPU model and memory, FP32 matrix multiplication checked against the CPU, BF16 throughput |
| Slurm | a GPU worker through Slurm | the GPU test as a batch job: GRES allocation, cgroup device isolation, Podman CDI access to exactly the allocated GPUs, result and exit code |

The basic test runs inside the image with `--network none --cap-drop ALL`.
Its script is passed inline (a shell here-document), so an unchanged upstream
image is tested the same way as a customised one. The build server needs no
GPU, and LayerSmith never runs the GPU test itself: the build host's GPU (or
lack of one) says nothing about the worker's.

On the worker, with `aiml/layersmith-aiml-check` from the bundle (it works for
imports too, through standard input):

```bash
podman run --rm -i --device nvidia.com/gpu=GPU-<uuid> --security-opt=no-new-privileges \
  --cap-drop=ALL --network=none -e LAYERSMITH_IMAGE=<image> <image> python3 - gpu \
  < aiml/layersmith-aiml-check
```

Paste the output (or the Slurm job's `.out` file) into **Record result** on
the build page. A result is accepted only for the image it names.

## GPU compatibility

The wizard and the build page grade four things, each with what the verdict
rests on - publisher documentation, the image itself, or a GPU test from a
worker:

- **Driver** - CUDA 13.4 needs driver branch R615, 13.3 R610, 13.0 R580. An
  older 13.x driver (>= 580) runs only through minor version compatibility
  and is a warning; older than that fails. CUDA 12.x images run on any newer
  driver. The target 615.71.09 runs CUDA 13.4.1 natively.
- **Framework CUDA build** - PyTorch's own CUDA version against the image's.
- **Container runtime** - the driver comes from the host through NVIDIA CDI.
- **Each target GPU** - H200 NVL (Hopper, sm_90) and RTX PRO 6000 Blackwell
  Server Edition (sm_120). Before a build: the documented minimum compute
  capability. After it: whether the image actually has `sm_90`/`sm_120`
  kernels, or only PTX the driver must JIT-compile, or neither. After a
  reported GPU test: the hardware result.

A detected GPU is never taken as proof of compatibility.

## Export for the air-gapped side

The build exports the image as an OCI archive (Podman) with its SHA-256; a
Docker-format archive can be written next to it from the build page. Both
load with `podman load` and `docker load`. Files are named
`<repository>-<tag>.tar`, for example `layersmith-pytorch-cuda13.4-1.0.0.tar`.

The air-gap bundle adds to the usual image, manifest, Containerfile and
`SHA256SUMS`:

```
image/<archive>.tar.sha256    for sha256sum -c next to the archive
aiml/README.md                load, run and test commands for this image
aiml/packages.json            Python packages and versions read from the image
aiml/requirements.lock        the same as name==version lines
aiml/sbom.cdx.json            CycloneDX 1.5: base image, NVIDIA libraries, Python packages
aiml/checks.json              test results recorded for this build
aiml/layersmith-aiml-check    the basic/GPU test script
```

The CycloneDX SBOM lists what LayerSmith itself read from the image; it does
not list OS packages. Configure a scanner (Settings → Security, see
[scanning.md](scanning.md)) for a full SBOM and vulnerability scan; the bundle
then carries those in `security/`.

On the isolated AlmaLinux GPU worker:

```bash
cd image
sha256sum -c layersmith-pytorch-cuda13.4-1.0.0.tar.sha256
podman load -i layersmith-pytorch-cuda13.4-1.0.0.tar
podman images
```

Disk space: NVIDIA PyTorch 26.09 is 11.7 GB compressed and roughly 25 GB
unpacked; the build host needs about 2.5 times the unpacked size free (image
layers plus the uncompressed archive). The build stops before pulling when
there is not enough; `LAYERSMITH_GPU_MIN_FREE_GB` overrides the check.

## Slurm

**Build page → Slurm → Generate Slurm batch script** writes an `.sbatch` file
for the image: partition, nodes, GPUs per node (GRES, optionally with a type),
CPUs, memory, time limit, the command, bind mounts for models, datasets and
`/scratch`, output and error logs. Choose **Slurm GPU test** to run the GPU
test as the job.

On every node, before the container starts, the script checks:

1. **GRES** - Slurm allocated GPUs to the step, as many as were asked for.
2. **Device isolation** - it tries to open each GPU's `/dev/nvidiaN`. With
   `ConstrainDevices=yes`, the job's cgroup only lets it open the allocated
   ones, and those are the GPUs the container gets. If the job can open GPUs
   it was not allocated, isolation is not configured and the script stops.
3. **NVIDIA CDI** - each allocated GPU is passed as
   `--device nvidia.com/gpu=GPU-<uuid>`, which does not depend on how Slurm
   and NVML number devices. The CDI spec must contain UUID names
   (`nvidia-ctk cdi generate --device-name-strategy=uuid`).
4. **SELinux** - stays enforcing. The `container_use_devices` boolean must be
   on; alternatively the script can be generated to disable labels for that
   one container only (`--security-opt label=disable`). SELinux is never
   switched off on the host.
5. **Image and data** - the image is loaded on the node, and every bind-mount
   source exists.

A missing prerequisite is printed as `LAYERSMITH_PREREQ_MISSING node=…: …` and
the job fails with exit status 97. The container runs with rootless Podman,
`--cap-drop=ALL`, `--security-opt=no-new-privileges` and `--network=none`
(or `host` for multi-node NCCL), never `--privileged`, never
`nvidia.com/gpu=all`, never a hard-coded GPU index. Each node prints
`LAYERSMITH_SLURM_EXIT {...}` with its container's exit code.

Cluster prerequisites, once per cluster:

```
# slurm.conf
GresTypes=gpu
ProctrackType=proctrack/cgroup
TaskPlugin=task/cgroup,task/affinity
# cgroup.conf
ConstrainDevices=yes
# gres.conf
AutoDetect=nvml
# each GPU node
nvidia-ctk cdi generate --device-name-strategy=uuid --output=/etc/cdi/nvidia.yaml
setsebool -P container_use_devices on
```

## Security

- LayerSmith has no login by design (see the README); put an authenticating
  proxy in front when other people can reach it. The AI/ML endpoints use the
  same model as the rest of the API.
- Builds use LayerSmith's configured runtime - with Podman its rootless
  socket. Nothing in this feature needs or hands out a privileged socket.
- Credentials never enter an image: package input cannot carry URLs, index
  URLs refuse credentials, and an Advanced-mode Containerfile of an AI/ML
  project is refused when it contains tokens, credentials in URLs,
  secret-looking `ENV`/`ARG` names, or commands that switch isolation off.
- Base images are pulled by digest; the build log says whether the base was
  verified by LayerSmith or is a custom one.
- Templates carry a version (`io.layersmith.aiml.template-version` label and
  manifest), so an image says which recipe made it.

## Limitations

- GPU and Slurm tests need a GPU worker; LayerSmith records their reports but
  cannot run them. The Slurm preflight was tested against a simulated node
  (fake `/proc/driver/nvidia`, device files, CDI spec, Podman and srun), not
  on a real cluster.
- Python tools are not version-pinned by LayerSmith: pip resolves them against
  the protected base at build time, and the build records exactly what it
  installed (`requirements.lock`). Pin versions in the package field for
  reproducible rebuilds.
- The CUDA templates have no Python, so they take no Python tools; their basic
  test reports the CUDA version from the shell.
