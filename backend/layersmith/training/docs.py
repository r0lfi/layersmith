"""Getting started for one concrete training image, and the export documentation.

Every command uses the real image reference of the build. The same sections
render as structured data for the dashboard and as Markdown for the README in
the export bundle and inside the image.
"""

from layersmith.training import catalog

DIRECTORIES = [
    {"path": "/workspace", "host": "workspace", "mode": "rw", "purpose": "Your code and configuration."},
    {"path": "/models", "host": "models", "mode": "ro", "purpose": "Local model files (one directory per model)."},
    {"path": "/datasets", "host": "datasets", "mode": "ro", "purpose": "Training and validation data."},
    {"path": "/outputs", "host": "outputs", "mode": "rw", "purpose": "Adapters, models and checkpoints."},
    {"path": "/logs", "host": "logs", "mode": "rw", "purpose": "Training logs and TensorBoard data."},
    {"path": "/cache", "host": "cache", "mode": "rw",
     "purpose": "Caches for models, datasets and tools (Hugging Face, Triton, ...)."},
]

HOST_ROOT = "$HOME/llm"


def _mounts(indent="  "):
    return " \\\n".join(
        f'{indent}-v "$LLM/{d["host"]}:{d["path"]}{":ro" if d["mode"] == "ro" else ""}"' for d in DIRECTORIES)


def _run(engine: str, recipe: dict, image: str, tail: str, extra: list[str] | None = None,
         interactive=True, gpu=True) -> str:
    """One `run` command for an engine, with GPU access, user mapping and mounts."""
    lines = [f"{engine} run --rm{' -it' if interactive else ''}"]
    if recipe["gpu"] and gpu:
        lines += ["--gpus all"] if engine == "docker" else ["--device nvidia.com/gpu=all",
                                                            "--security-opt=label=disable"]
    elif engine == "podman":
        lines.append("--security-opt=label=disable")
    lines.append('--user "$(id -u):$(id -g)"' if engine == "docker" else "--userns=keep-id")
    if recipe["gpu"]:
        lines.append("--shm-size=16g")
    lines += extra or []
    body = " \\\n  ".join(lines)
    return f"{body} \\\n{_mounts()} \\\n  {image} {tail}"


def _both(recipe, image, tail, extra=None, interactive=True, gpu=True):
    return {"docker": _run("docker", recipe, image, tail, extra, interactive, gpu),
            "podman": _run("podman", recipe, image, tail, extra, interactive, gpu)}


def _example_commands(recipe: dict) -> list[dict]:
    """What to run inside the container, per profile."""
    ex = "/opt/layersmith/examples"
    profile = recipe["profile"]
    if profile in ("hf-finetune", "pytorch-advanced"):
        steps = [
            {"title": "Check your data",
             "text": "Validate the JSONL file and split off a validation set once, before training. "
                     "Validation records are written to their own file and never used for training.",
             "code": f"python {ex}/validate_dataset.py /datasets/all.jsonl --split 0.1 --out /workspace/data \\\n"
                     f"    --tokenizer /models/my-model --max-length 2048"},
            {"title": "SFT with LoRA",
             "text": "Trains a LoRA adapter on chat data ({\"messages\": [...]} per line), using the model's own "
                     "chat template. The adapter, checkpoints and adapter_provenance.json (base model and "
                     "revision) go to /outputs/sft-lora-run1.",
             "code": f"python {ex}/sft_lora.py --model /models/my-model \\\n"
                     f"    --train /workspace/data/train.jsonl --val /workspace/data/validation.jsonl \\\n"
                     f"    --output /outputs/sft-lora-run1"},
            {"title": "SFT with QLoRA (GPU)",
             "text": "The same with the base model loaded in 4-bit (bitsandbytes NF4). Uses less GPU memory; "
                     "needs an NVIDIA GPU.",
             "code": f"python {ex}/sft_lora.py --qlora --model /models/my-model \\\n"
                     f"    --train /workspace/data/train.jsonl --val /workspace/data/validation.jsonl \\\n"
                     f"    --output /outputs/sft-qlora-run1"},
            {"title": "Resume from a checkpoint",
             "text": "Run the same command again with --resume: training continues from the newest "
                     "checkpoint-* in the output directory.",
             "code": f"python {ex}/sft_lora.py ... --output /outputs/sft-lora-run1 --resume"},
            {"title": "DPO (preference data)",
             "text": "Needs prompt/chosen/rejected records. Usually run on an SFT-tuned model.",
             "code": f"python {ex}/dpo_lora.py --model /models/my-sft-model \\\n"
                     f"    --train /datasets/preferences.jsonl --output /outputs/dpo-run1"},
            {"title": "Compare base model and adapter",
             "text": "Greedy answers from the base model and base + adapter on the same prompts. The base model "
                     "is read from adapter_provenance.json.",
             "code": f"python {ex}/compare_adapter.py --adapter /outputs/sft-lora-run1 \\\n"
                     f"    --prompts /workspace/data/validation.jsonl --limit 5"},
            {"title": "Several GPUs in one server",
             "text": "Data parallel with Accelerate. Start the container with all GPUs and --shm-size (or "
                     "--ipc=host).",
             "code": f"accelerate launch --config_file {ex}/configs/multi_gpu.yaml --num_processes 2 \\\n"
                     f"    {ex}/sft_lora.py --model /models/my-model --train /workspace/data/train.jsonl \\\n"
                     f"    --output /outputs/sft-lora-ddp"},
        ]
        if profile == "pytorch-advanced":
            steps += [
                {"title": "Continued pretraining / full fine-tuning",
                 "text": "Every parameter is trained. Plain text ({\"text\": ...}) is packed into blocks; chat "
                         "data uses the chat template.",
                 "code": f"python {ex}/train_causal_lm.py --model /models/my-model \\\n"
                         f"    --train /datasets/corpus.jsonl --output /outputs/cpt-run1 --gradient-checkpointing"},
                {"title": "FSDP over several GPUs (PyTorch feature, configured through Accelerate)",
                 "text": "Shards parameters, gradients and optimizer state across the GPUs of one server.",
                 "code": f"accelerate launch --config_file {ex}/configs/fsdp.yaml --num_processes 4 \\\n"
                         f"    {ex}/train_causal_lm.py --model /models/my-model --train /datasets/corpus.jsonl \\\n"
                         f"    --output /outputs/cpt-fsdp"},
            ]
            if "deepspeed" in recipe["addons"]:
                steps.append({"title": "DeepSpeed ZeRO-3 with CPU offload",
                              "text": "DeepSpeed compiles its CUDA ops on first use; run ds_report first.",
                              "code": f"ds_report\naccelerate launch --config_file {ex}/configs/deepspeed.yaml "
                                      f"--num_processes 4 \\\n    {ex}/train_causal_lm.py --model /models/my-model "
                                      f"--train /datasets/corpus.jsonl --output /outputs/cpt-ds"})
            if "flash-attn" in recipe["addons"]:
                steps.append({"title": "FlashAttention 2",
                              "text": "Ampere (sm_80) or newer GPUs and supported models only.",
                              "code": f"python {ex}/sft_lora.py --attn flash_attention_2 --model /models/my-model ..."})
        return steps
    if profile == "llama-factory":
        return [
            {"title": "Your dataset",
             "text": "LLaMA-Factory reads datasets through a dataset_info.json in the dataset directory. The "
                     "bundled one shows the OpenAI-style 'messages' format and a preference (DPO) entry.",
             "code": f"cat {ex}/llamafactory/dataset_info.json\n"
                     "# copy it next to your files in $LLM/datasets and add your own entries"},
            {"title": "Train from the command line",
             "text": "Copy an example YAML to /workspace, set model_name_or_path, template and dataset, then:",
             "code": f"cp {ex}/llamafactory/lora_sft.yaml /workspace/\n"
                     "llamafactory-cli train /workspace/lora_sft.yaml \\\n"
                     "    model_name_or_path=/models/my-model template=llama3 dataset_dir=/datasets dataset=my_data"},
            {"title": "QLoRA and DPO",
             "text": "qlora_sft.yaml adds quantization_bit: 4 (NVIDIA GPU); lora_dpo.yaml trains on preference data.",
             "code": "llamafactory-cli train /opt/layersmith/examples/llamafactory/qlora_sft.yaml model_name_or_path=..."},
            {"title": "Resume from a checkpoint",
             "text": "Set resume_from_checkpoint to a checkpoint directory under /outputs.",
             "code": "llamafactory-cli train /workspace/lora_sft.yaml "
                     "resume_from_checkpoint=/outputs/llamafactory/lora-sft/checkpoint-200"},
        ]
    return [
        {"title": "Validate and split", "text": "Checks fields and roles, reports duplicates and token lengths, "
                                                "and writes separate train and validation files.",
         "code": f"python {ex}/validate_dataset.py /datasets/all.jsonl --split 0.1 --out /workspace/prepared \\\n"
                 f"    --tokenizer /models/my-model --max-length 2048"},
        {"title": "Filter", "text": "Drops empty, over-long and duplicate records and says how many and why.",
         "code": f"python {ex}/filter_dataset.py /workspace/prepared/train.jsonl --out /workspace/prepared/train.clean.jsonl \\\n"
                 f"    --tokenizer /models/my-model --max-tokens 2048 --dedupe"},
        {"title": "Convert", "text": "JSONL, CSV and Parquet, by file extension.",
         "code": f"python {ex}/convert_dataset.py /workspace/prepared/train.clean.jsonl /workspace/prepared/train.parquet"},
        {"title": "Token statistics", "text": "Helps choose the max length for training.",
         "code": f"python {ex}/token_stats.py /workspace/prepared/train.parquet --tokenizer /models/my-model"},
    ]


def getting_started(recipe: dict, image: str, archive: str | None = None) -> dict:
    """Structured Getting started for the dashboard."""
    sections = []
    gpu = recipe["gpu"]
    sections.append({
        "id": "what", "title": "What this image contains",
        "text": (f"{recipe['profile_name']} on {recipe['base']['display']} with Python {recipe['python']}"
                 + (f", PyTorch {recipe['versions'].get('torch')} (CUDA {recipe['cuda']})" if recipe["torch"] else "")
                 + ". Model files and training data are not in the image: you mount them when you start it."),
    })
    if archive:
        sections.append({"id": "load", "title": "Load the image on the target machine",
                         "text": "From the TAR archive or the air-gap bundle (verify with SHA256SUMS first).",
                         "docker": f"docker load -i {archive}", "podman": f"podman load -i {archive}"})
    sections.append({
        "id": "dirs", "title": "Create the host directories",
        "text": "Everything the container writes lands here, so it survives the container. The examples run as "
                "your own user (docker --user, podman --userns=keep-id), so results are owned by you.",
        "code": f'export LLM={HOST_ROOT}\nmkdir -p "$LLM"/{{{",".join(d["host"] for d in DIRECTORIES)}}}',
        "table": DIRECTORIES,
    })
    if gpu:
        sections.append({
            "id": "gpu", "title": "Give the container the GPU",
            "text": ("The NVIDIA driver and the container GPU integration live on the host, not in the image. "
                     "Docker: install the NVIDIA Container Toolkit and use --gpus all (or CDI: "
                     "--device nvidia.com/gpu=all on Docker 25+ with CDI enabled). Podman: generate the CDI "
                     "specification once (sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml) and use "
                     "--device nvidia.com/gpu=all. --security-opt=label=disable lets the GPU device through "
                     "SELinux. Then run the GPU check on the machine that will train:"),
            **_both(recipe, image, "layersmith-check gpu --json", interactive=False),
            "note": "The check prints a JSON report at the end. Paste it into this build's GPU result in "
                    "LayerSmith to record it. --shm-size (or --ipc=host) gives PyTorch data loaders and NCCL "
                    "enough shared memory.",
            "link": catalog.DOCS["nvidia-ctk"],
        })
    sections.append({"id": "shell", "title": "Start a shell in the container",
                     "text": "Model directory under $LLM/models, data under $LLM/datasets (both mounted read-only).",
                     **_both(recipe, image, "bash")})
    sections.append({"id": "examples", "title": "Run an example",
                     "text": "Inside the container. Every example has --help; all take local paths.",
                     "steps": _example_commands(recipe)})
    ports = {p["port"]: p["name"] for p in recipe["ports"]}
    if 6006 in ports:
        sections.append({"id": "tensorboard", "title": "TensorBoard",
                         "text": "Reads the training logs in $LLM/logs. TensorBoard has no login, so the port is "
                                 "published on the host's localhost only.",
                         **_both(recipe, image, "layersmith-tensorboard", ["-p 127.0.0.1:6006:6006"],
                                 interactive=False, gpu=False)})
    if 8888 in ports:
        sections.append({"id": "jupyter", "title": "JupyterLab (token login)",
                         "text": "The token is read from a file you keep on the host; without one, a random token "
                                 "is generated and printed. The port is published on the host's localhost only.",
                         "code": 'openssl rand -hex 24 > "$LLM/jupyter_token" && chmod 600 "$LLM/jupyter_token"',
                         **_both(recipe, image, "layersmith-jupyter",
                                 ["-p 127.0.0.1:8888:8888",
                                  '-v "$LLM/jupyter_token:/run/secrets/jupyter_token:ro"'])})
    if 7860 in ports:
        sections.append({"id": "llamaboard", "title": "LLaMA Board (login)",
                         "text": "LLaMA-Factory's web UI, started with a user name (trainer) and the password "
                                 "from a file you keep on the host. Training results go to $LLM/outputs; put a "
                                 "dataset_info.json in $LLM/datasets to use your own data.",
                         "code": 'openssl rand -hex 18 > "$LLM/llamaboard_password" && chmod 600 "$LLM/llamaboard_password"',
                         **_both(recipe, image, "layersmith-llamaboard",
                                 ["-p 127.0.0.1:7860:7860",
                                  '-v "$LLM/llamaboard_password:/run/secrets/llamaboard_password:ro"'])})
    if ports:
        first = min(ports)
        sections.append({"id": "tunnel", "title": "Reach a web UI from another machine",
                         "text": "Ports are published on the GPU server's localhost only. Forward them over SSH, "
                                 "then open the address on your own machine.",
                         "code": f"ssh -N -L {first}:127.0.0.1:{first} you@gpu-server\n"
                                 f"# then open http://127.0.0.1:{first}"})
    sections.append({"id": "results", "title": "Where the results are",
                     "text": "On the host: adapters, models and checkpoints in $LLM/outputs/<run>, TensorBoard "
                             "logs in $LLM/logs/<run>, downloaded models and datasets in $LLM/cache. "
                             "adapter_provenance.json next to each adapter names the base model and revision it "
                             "belongs to."})
    sections.append(offline_section(recipe, image))
    sections.append({"id": "secrets", "title": "Tokens and other secrets",
                     "text": "Pass them at run time and never write them into a Containerfile, an image or an "
                             "export. For gated Hugging Face models: export HF_TOKEN on the host and add "
                             "-e HF_TOKEN (the value is taken from your environment, not the command line), or "
                             "use podman --secret. Telemetry and automatic uploads are off in this image "
                             "(HF_HUB_DISABLE_TELEMETRY=1, WANDB_MODE=disabled"
                             + (", GRADIO_ANALYTICS_ENABLED=False" if recipe["profile"] == "llama-factory" else "")
                             + ")."})
    if gpu:
        sections.append({"id": "memory", "title": "GPU memory",
                         "text": "VRAM use depends on the model size, sequence length, batch size, precision and "
                                 "training method; no model is guaranteed to fit in a given amount of memory. "
                                 "Start with --batch-size 1, a short --max-length and gradient accumulation, then "
                                 "increase. QLoRA uses the least memory, full fine-tuning the most. Several "
                                 "servers need a working network between them (NCCL) and matching host setup, "
                                 "which these examples do not cover."})
    return {"image": image, "sections": sections}


def offline_section(recipe: dict, image: str) -> dict:
    return {
        "id": "offline", "title": "Offline and air-gapped use",
        "text": ("Four things are separate, and only the first is in the export: (1) this container image with "
                 "its tools; (2) model files - weights (every shard), config and tokenizer; (3) training and "
                 "validation data; (4) evaluation resources and anything else a framework would otherwise fetch "
                 "on first use. Bring 2-4 with you, then run with networking off."),
        "steps": [
            {"title": "On a connected machine: fetch the model at a fixed revision",
             "code": "pip install -U huggingface_hub   # or use this image: it includes the hf command\n"
                     "hf download ORG/MODEL --revision COMMIT_SHA --local-dir models/MODEL"},
            {"title": "Copy models and data to the offline host",
             "code": "rsync -a models/MODEL offline-host:llm/models/\nrsync -a my-data/ offline-host:llm/datasets/"},
            {"title": "Check the model directory is complete (config, tokenizer, all shards)",
             "code": f"layersmith-check model /models/MODEL"},
            {"title": "Run without network",
             "text": "HF_HUB_OFFLINE=1 makes the Hugging Face libraries use local files only; the examples' "
                     "--offline flag also passes local_files_only=True.",
             **_both(recipe, image, "bash", ["--network none", "-e HF_HUB_OFFLINE=1", "-e HF_DATASETS_OFFLINE=1"])},
            {"title": "Evaluation resources",
             "text": "Benchmark datasets (for example for lm-evaluation-harness) are downloaded on first use. Run "
                     "the evaluation once on a connected machine with the same /cache mount, then copy "
                     "$LLM/cache along with the models."},
        ],
        "note": ("Running this image offline is supported and verified by the 'Offline example verified' check. "
                 "Rebuilding the image offline is a different thing: it needs the base image, PyPI and "
                 "download.pytorch.org (and GitHub for FlashAttention). LayerSmith does not provide an offline "
                 "rebuild; the export includes the exact locks if you maintain your own mirror."),
    }


def export_contents(recipe: dict, scanned: bool | None) -> dict:
    """What an air-gap bundle of this image holds. scanned=None: not known yet (before a build)."""
    included = [
        "Container image (image/*.tar)",
        "Generated Containerfile (source/Containerfile)",
        "Profile and version information (training/recipe.json)",
        "Hash-locked Python requirements (training/requirements*.lock)",
        "README with Getting started for this image (training/README.md)",
        "Example scripts, configurations and synthetic sample data (training/examples/)",
        "Check results of this build (training/checks.json)",
        "Manifest and checksums (metadata/manifest.json, SHA256SUMS)",
    ]
    if scanned:
        included.append("Scan results and SBOM (security/)")
    elif scanned is None:
        included.append("Scan results and SBOM (security/), when the image has been scanned")
    else:
        included.append("A note that the image was not scanned (security/NOT-SCANNED.txt)")
    return {
        "summary": "Container image and example setup are included. Model weights and training data must be "
                   "transferred separately.",
        "included": included,
        "external": [
            "Model files: weights (all shards), config.json, tokenizer files, and generation_config.json if any",
            "Your training and validation data",
            "Evaluation datasets and anything a framework downloads on first use (pre-fill /cache)",
            "On the target machine: NVIDIA driver and container GPU support" if recipe["gpu"]
            else "On the target machine: Docker or Podman",
        ],
    }


# ----------------------------------------------------------------- markdown

def _code(text: str, lang: str = "bash") -> str:
    return f"```{lang}\n{text}\n```\n"


def markdown(recipe: dict, image: str, archive: str | None = None, checks: list | None = None,
             contents: dict | None = None) -> str:
    """README for the export bundle (and, with a placeholder image, inside the image)."""
    guide = getting_started(recipe, image, archive)
    out = [f"# {recipe['profile_name']} - training environment\n\n",
           f"Image: `{image}`\n\n",
           f"Built by LayerSmith. {before_build(recipe)}\n\n"]
    out.append("## Software\n\n")
    rows = [("Profile", f"{recipe['profile_name']} {recipe['profile_version']}"),
            ("Stack", recipe["stack_name"]), ("Base image", f"{recipe['base']['display']} @ {recipe['base']['digest']}"),
            ("Architecture", f"linux/{recipe['architecture']}"), ("Python", recipe["python"])]
    if recipe["torch"]:
        rows += [("PyTorch", recipe["versions"].get("torch", recipe["torch"])), ("CUDA", recipe["cuda"]),
                 ("Driver", recipe["driver"]), ("GPUs", recipe["gpu_support"])]
    rows.append(("Add-ons", ", ".join(recipe["addons"]) or "none"))
    rows.append(("Lock digest", recipe["lock_digest"]))
    out.append("| | |\n|---|---|\n" + "".join(f"| {k} | {v} |\n" for k, v in rows))
    out.append("\n| Tool | Version | |\n|---|---|---|\n")
    for tool in recipe["tools"]:
        if tool["required"] or tool["addon"] in recipe["addons"]:
            out.append(f"| {tool['name']} | {tool['version'] or '-'} | {tool['what']} |\n")
    if contents:
        out.append("\n## What this package contains\n\n" + contents["summary"] + "\n\n")
        out += [f"- {item}\n" for item in contents["included"]]
        out.append("\nNot included - bring these separately:\n\n")
        out += [f"- {item}\n" for item in contents["external"]]
    if checks:
        out.append("\n## Checks of this build\n\n")
        for check in checks:
            out.append(f"- **{check['label']}** - {check['summary']}\n")
    out.append("\n## Getting started\n\n")
    for section in guide["sections"]:
        out.append(f"\n### {section['title']}\n\n")
        if section.get("text"):
            out.append(section["text"] + "\n\n")
        if section.get("table"):
            out.append("| Container | Host | Purpose |\n|---|---|---|\n")
            out += [f"| `{d['path']}` | `$LLM/{d['host']}` ({d['mode']}) | {d['purpose']} |\n" for d in section["table"]]
            out.append("\n")
        if section.get("code"):
            out.append(_code(section["code"]))
        if section.get("docker"):
            out.append("Docker:\n\n" + _code(section["docker"]) + "\nPodman:\n\n" + _code(section["podman"]))
        for step in section.get("steps", []):
            out.append(f"\n**{step['title']}**" + (f" - {step['text']}" if step.get("text") else "") + "\n\n")
            if step.get("code"):
                out.append(_code(step["code"]))
            if step.get("docker"):
                out.append("Docker:\n\n" + _code(step["docker"]) + "\nPodman:\n\n" + _code(step["podman"]))
        if section.get("note"):
            out.append("\n> " + section["note"] + "\n")
    return "".join(out).replace("\n\n\n", "\n\n")


def before_build(recipe: dict) -> str:
    if recipe["gpu"]:
        return catalog.CATEGORY["before_build"]
    return ("You are building the data preparation environment. You choose the data and tokenizer when you run "
            "the container. No GPU is needed.")
