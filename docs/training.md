# LLM Training & Fine-tuning

LayerSmith can build ready-to-run environments for training and fine-tuning
language models. It **builds and documents** the environment; the training
itself runs in the finished image, on your own GPU machine. Model weights and
training data are never part of the image: you mount them when you start it.

In the dashboard: **Create image → LLM Training & Fine-tuning**. The flow is

1. purpose and profile,
2. hardware target (and, where a profile has several, the software stack),
3. the recommended software, with what each tool is for,
4. compatible add-ons,
5. image name and version,
6. summary and generated Containerfile,
7. build, checks, export - and **Getting started** for that exact image on the build page.

## Profiles

| Profile | For | Target | Stack |
| --- | --- | --- | --- |
| **Hugging Face Fine-tuning** (recommended) | SFT and DPO with LoRA or QLoRA | NVIDIA GPU, linux/amd64 | PyTorch 2.13.0 + CUDA 12.9 |
| **PyTorch / Hugging Face Advanced** | Full fine-tuning, continued pretraining, own scripts, multi-GPU (DDP, FSDP, DeepSpeed) | NVIDIA GPU, linux/amd64 | PyTorch 2.13.0 + CUDA 12.9, or PyTorch 2.8.0 + CUDA 12.8 with FlashAttention 2 |
| **LLaMA-Factory - Web UI** | Fine-tuning from LLaMA Board or LLaMA-Factory YAML files | NVIDIA GPU, linux/amd64 | LLaMA-Factory 0.9.5, PyTorch 2.6.0 + CUDA 12.4 |
| **Dataset Preparation - CPU** | Validating, filtering, splitting and converting data; token statistics | CPU, linux/amd64 | Python 3.12, no PyTorch |

Every profile starts from `ubuntu:24.04` (pinned by digest) with Python 3.12
in `/opt/venv`. Selecting DeepSpeed switches the base to NVIDIA's CUDA
development image (pinned by digest), because DeepSpeed compiles its CUDA ops
with `nvcc` the first time they are used.

Axolotl and Unsloth are not offered yet. Each will get its own stack, locks
and checks before it appears in the dashboard.

### Versions

Library versions are not chosen per build. Each stack is resolved once, with
hashes, into lock files under `backend/layersmith/training/stacks/<stack>/`:

| Stack | Key versions |
| --- | --- |
| `hf-cu129` | torch 2.13.0+cu129, transformers 5.17.0, datasets 4.8.4, accelerate 1.15.0, peft 0.21.0, trl 1.13.0, bitsandbytes 0.50.2, huggingface_hub 1.18.0 - the combination Axolotl 0.20.0 pins and tests |
| `fa2-cu128` | torch 2.8.0+cu128 with the same libraries, plus flash-attn 2.8.3.post1 (prebuilt wheel) |
| `llamafactory-cu124` | llamafactory 0.9.5, torch 2.6.0+cu124, transformers 5.6.0, peft 0.18.1, trl 0.24.0, datasets 4.0.0 - inside LLaMA-Factory's own bounds |
| `cpu-data` | datasets 4.8.4, transformers 5.17.0, tokenizers 0.23.1, pandas 3.0.6, pyarrow 25.0.1 |

Add-ons (TensorBoard, JupyterLab, lm-evaluation-harness, DeepSpeed,
FlashAttention) have their own lock files, resolved against one combined
resolution so any combination is consistent. Regenerate with
`python backend/scripts/lock_training.py` (needs network and `uv`).

### Reference builds

Measured on 2026-10-02 (8-core x86_64 VM with Docker, no GPU; the CPU profile
on a 4-core VM with Podman). Time includes the post-build checks.

| Recipe | Image | Archive | Build + checks |
| --- | --- | --- | --- |
| Hugging Face Fine-tuning + TensorBoard + JupyterLab | 13.5 GB | 4.8 GB | 28 min |
| LLaMA-Factory + TensorBoard | 11.4 GB | 3.8 GB | 28 min |
| Advanced, FlashAttention stack + DeepSpeed + TensorBoard | 28.0 GB | 9.8 GB | 48 min |
| Dataset Preparation - CPU + JupyterLab | 0.9 GB | 0.3 GB | 4 min |

The CUDA development base that DeepSpeed needs roughly doubles the image.
Plan disk space for the image, its export and an air-gap bundle together.

The backend validates every request (`POST /api/training/resolve`, and again
when a project is saved and built). Incompatible combinations are refused with
an explanation and a working alternative - for example FlashAttention on the
PyTorch 2.13 stack, for which no prebuilt wheel exists.

## Checks

After a build, LayerSmith runs three checks inside the image with networking
disabled and every capability dropped. The build page shows exactly what was
verified:

| Status | Meaning |
| --- | --- |
| **Image built** | The container build completed. |
| **Dependencies checked** | Every locked package is installed at its locked version, `pip check` passes, the profile's libraries import. |
| **CPU smoke test passed** | A tiny, locally created model and synthetic data: forward pass, backward pass, optimizer step, save and load. |
| **GPU test not run** | LayerSmith never runs GPU tests: the build host says nothing about the training machine. |
| **GPU training test passed** | A GPU report from the target machine was recorded for this image. |
| **Offline example verified** | The bundled example ran without network (`HF_HUB_OFFLINE=1`) on local resources. |

Run the GPU check on the machine that will train and paste its JSON into the
build page:

```bash
docker run --rm --gpus all --shm-size=16g IMAGE layersmith-check gpu --json
podman run --rm --device nvidia.com/gpu=all --security-opt=label=disable --shm-size=16g IMAGE layersmith-check gpu --json
```

Results belong to one build. An image with extra packages, files or scripts is
marked as a modified recipe; its checks are its own.

## Running an image

Inside every image:

| Path | Purpose |
| --- | --- |
| `/workspace` | your code and configuration |
| `/models` | local model files (mount read-only) |
| `/datasets` | training and validation data (mount read-only) |
| `/outputs` | adapters, models, checkpoints |
| `/logs` | training logs, TensorBoard data |
| `/cache` | Hugging Face and tool caches |
| `/opt/layersmith` | `README.md`, `recipe.json`, locks, `examples/`, `checks/` |

The image runs as `trainer` (uid 1000); the writable directories also work
with `docker run --user "$(id -u):$(id -g)"` or `podman run --userns=keep-id`,
so results on the host belong to you. GPU access: `--gpus all` with the NVIDIA
Container Toolkit, or CDI (`--device nvidia.com/gpu=all`, after
`nvidia-ctk cdi generate`). JupyterLab and LLaMA Board start with a token or
password and the examples publish them on `127.0.0.1` only; use an SSH tunnel
from other machines. TensorBoard has no login and is published the same way.

The build page renders complete Docker and Podman commands for the actual
image, including examples for SFT with LoRA and QLoRA, DPO, resuming from a
checkpoint, comparing base model and adapter, multi-GPU with Accelerate, FSDP
and DeepSpeed.

## Offline and air-gapped use

Four things are separate, and only the first is in an export:

1. the container image with its tools,
2. model files - weights (every shard), config, tokenizer,
3. training and validation data,
4. evaluation datasets and anything else a framework fetches on first use.

The air-gap bundle adds `training/` to the usual contents: a README with
Getting started for that image, `recipe.json`, the locks, the examples, the
check results and `EXTERNAL-FILES.md` listing what must be brought separately.
Fetch models on a connected machine at a fixed revision
(`hf download ORG/MODEL --revision SHA --local-dir models/MODEL`), copy them
over, verify with `layersmith-check model /models/MODEL`, and run with
`--network none -e HF_HUB_OFFLINE=1`.

Running an image offline is supported and verified. **Rebuilding** an image
offline is not: a build needs the base image, PyPI and download.pytorch.org
(and GitHub for FlashAttention).

Telemetry and automatic uploads are off in every image
(`HF_HUB_DISABLE_TELEMETRY=1`, `WANDB_MODE=disabled`, Gradio analytics off).
Tokens such as `HF_TOKEN` are passed at runtime and never written into an
image or an export.
