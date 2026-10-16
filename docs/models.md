# LLM models for offline GPU hosts

The GPU benchmark image measures LLM token speed with vLLM on models from
`/models`, and never downloads anything at runtime. The **Models** page gets
those models onto a GPU host that has no network and may refuse files of
1 GB or more:

1. LayerSmith downloads the model from Hugging Face once, into its own folder
   under the model directory, and checks every file.
2. It hands the model over as parts of at most 900 MB plus an
   `install-<model>.sh`.
3. On the GPU host the script checks the parts, unpacks the model into
   `/opt/models/<model>` and checks every file again. The container sees it
   as `/models/<model>`.

The GPU benchmark wizard has the same list as an **LLM models** card: the
models ticked there start downloading when you press Build, and are ticked in
the build's GPU kit as soon as they are ready.

The **GPU kit** on a GPU benchmark build page bundles the image, the chosen
models, `setup.sh` and `start_container.sh`, so the GPU host needs a single
`bash setup.sh`; see [gpu-benchmark.md](gpu-benchmark.md#the-gpu-kit-one-command-on-the-gpu-host).

## Recommended models for NVIDIA H200

All open on Hugging Face (no login, no licence click-through). Sizes are what
LayerSmith downloads - the files vLLM loads, not the whole repository.
*GPUs* is the smallest number of H200 (141 GB HBM3e) that serves the model at
its stored precision with room for the KV cache.

| Purpose | Model | Params · precision | Download | GPUs |
|---|---|---|---|---|
| Quick smoke test | `Qwen/Qwen2.5-0.5B-Instruct` | 0.5B · BF16 | 1.0 GB | 1 |
| 8B baseline | `Qwen/Qwen3-8B` | 8B · BF16 | 16.4 GB | 1 |
| FP8 kernels | `RedHatAI/Meta-Llama-3.1-8B-Instruct-FP8` | 8B · FP8 | 9.1 GB | 1 |
| Mixture of experts | `openai/gpt-oss-20b` | 21B (3.6B active) · MXFP4 | 13.8 GB | 1 |
| Mixture of experts | `Qwen/Qwen3-30B-A3B` | 30B (3B active) · BF16 | 61.1 GB | 1 |
| Mid-size dense | `microsoft/phi-4` | 14B · BF16 | 29.3 GB | 1 |
| Mid-size dense | `Qwen/Qwen3-32B` | 32B · BF16 | 65.5 GB | 1 |
| Large, one GPU | `openai/gpt-oss-120b` | 117B (5.1B active) · MXFP4 | 65.2 GB | 1 |
| Large, one GPU | `RedHatAI/Llama-3.3-70B-Instruct-FP8-dynamic` | 70B · FP8 | 72.7 GB | 1 |
| Multi-GPU | `Qwen/Qwen2.5-72B-Instruct` | 72B · BF16 | 145.4 GB | 2 |
| Multi-GPU | `Qwen/Qwen3-235B-A22B-FP8` | 235B (22B active) · FP8 | 239.0 GB | 2 |

A sensible order: the smoke test first (it proves the whole vLLM path in
seconds), then the 8B baseline in BF16 and FP8 to compare GPUs and see the FP8
speed-up, then one large model to fill HBM and stress memory bandwidth, and a
multi-GPU model when the host has NVLink-connected GPUs
(`gpu-bench llm --model /models/<m> --tensor-parallel-size 2`).

Any other open model works too: enter its id (and optionally a revision) under
**Any Hugging Face model**. It needs a top-level `config.json` and
`.safetensors` weights, which is what vLLM loads.

**Gated models are refused.** Meta Llama and Google Gemma need their licence
accepted on huggingface.co and a token; LayerSmith does not store tokens.
RedHatAI publishes open, vLLM-ready copies of Llama (the FP8 ones above).

## What is downloaded and checked

Only top-level files vLLM reads: `config.json` and other `*.json`,
`*.safetensors`, tokenizer files (`tokenizer.model`, `*.tiktoken`,
`merges.txt`, `vocab.txt`), chat templates (`*.jinja`), and `README.md` /
`LICENSE`. Subfolders (`original/`, `metal/`, `onnx/` …), `pytorch_model.bin`
and Python code are skipped, and so is `consolidated.safetensors` when the
same weights are also there as `model-*.safetensors` (Mistral ships both).

The listing is resolved to one commit when the download starts, and every file
is fetched from that commit. Weights are checked against the sha256 Hugging
Face publishes for them, small files against their Git blob id; a file that
does not match is removed and the model is marked failed. A download needs the
model's size free in the model directory plus 2 GB, and is refused up front
otherwise.

Downloads run one at a time in the background, with progress on the page.
An interrupted download - network error, restart, upgrade - continues where it
stopped: unfinished downloads are picked up again at startup, and a partly
written file is resumed with an HTTP range request. **Retry** does the same
after a failure. **Delete** removes the folder (or cancels a running
download); models already installed on GPU hosts are not affected.

## How a model is split

A model is stored once, as a plain folder. The download is an uncompressed
tar of that folder (`<model>.model.tar`) that is never written to disk:
LayerSmith describes the archive and streams any byte range of it straight
from the files, so splitting a 70 GB model takes no extra space. The archive
is deterministic (fixed owner, mode and time stamp), so its part checksums are
computed once, when the download completes, and stay valid as long as the
files are unchanged.

For one model the page offers:

```
Qwen3-8B.model.tar.part-000 … part-018   at most 900 MB each
install-Qwen3-8B.sh                      checks, unpacks, verifies
Qwen3-8B.parts.sha256                    sha256sum -c for the parts
Qwen3-8B.files.sha256                    sha256sum -c inside the model folder
download-Qwen3-8B.sh                     fetches all of the above, resumable
```

## On the GPU host

```sh
bash install-Qwen3-8B.sh                   # into /opt/models (or $MODELS_DIR)
bash install-Qwen3-8B.sh --dir /data/models --remove-parts
```

It needs bash, coreutils and tar. In order: every part present with the
expected size; every part's sha256; that the models directory has room;
`cat parts | tar -x` into a temporary folder next to the target (no
reassembled archive on disk); every file's size and sha256; then the folder is
moved into place. A missing or corrupt part is named and nothing is
installed; an interrupted run removes its temporary folder. Running it again
verifies an installed model and stops. It creates the models directory with
sudo when needed but never changes the owner of a directory that already
exists.

Exit codes: `0` ok, `1` usage, `2` missing or incomplete part, `3` corrupt
part or file, `4` not enough space or a write error, `5` a different folder of
that name exists (`--force` replaces it).

By hand, without the script:

```sh
sha256sum -c Qwen3-8B.parts.sha256
cat Qwen3-8B.model.tar.part-* | tar -xf - -C /opt/models
(cd /opt/models/Qwen3-8B && sha256sum -c "$OLDPWD/Qwen3-8B.files.sha256")
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LAYERSMITH_MODEL_DIR` | `$DATA/models` | Downloaded models; also editable in **Settings → Storage** |
| `LAYERSMITH_HF_ENDPOINT` | `https://huggingface.co` | Hugging Face, or a mirror with the same API |

Models are often the largest thing LayerSmith stores: put the model directory
on its own volume when the data volume is small. Moving it never moves
existing models; they stay where they were downloaded.

API: `GET /api/llm-models/catalog`, `GET|POST /api/llm-models`,
`POST /api/llm-models/{id}/retry`, `DELETE /api/llm-models/{id}`,
`GET /api/llm-models/{id}/files[/{file}]`, and for the GPU kit
`GET /api/builds/{id}/kit?models=<id>,<id>` and
`GET /api/builds/{id}/kit/{file}?models=...`. `/api/health` lists
`llm-models` and `gpu-kit` in `api_features`.
