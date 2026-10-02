"""What the LLM Training & Fine-tuning category offers, as data.

Only profiles with a working, verified build recipe are listed in PROFILES.
Further profiles (Axolotl, Unsloth) belong here once each has its own stack
directory, version locks and checks; a card without a recipe is not added.

Versions are not written in this file: they are read from the lock files of
each stack (see recipes.locked_versions), so what the dashboard shows is what
an image build installs.
"""

CATEGORY = {
    "id": "llm-training",
    "name": "LLM Training & Fine-tuning",
    "intro": (
        "Build a ready-to-run environment for training or fine-tuning language models. "
        "LayerSmith builds and documents the environment; you run the training yourself, "
        "in the finished image, on your own GPU machine. Models and training data are not "
        "part of the image - you mount them when you start the container."
    ),
    "before_build": (
        "You are building the training environment. You choose the model and the dataset when you "
        "run the container. The machine that runs GPU training needs a compatible NVIDIA driver and "
        "container GPU support (NVIDIA Container Toolkit or CDI)."
    ),
}

#: Basic terms, shown in an expandable section of the category.
CONCEPTS = [
    {"id": "fine-tuning", "term": "Fine-tuning",
     "text": "Further training of an existing model on a focused dataset, to adapt it to particular "
             "tasks, answer formats or kinds of content."},
    {"id": "sft", "term": "SFT - Supervised Fine-Tuning",
     "text": "The model is trained on examples of the text or conversations you want, for example "
             "instructions paired with good answers."},
    {"id": "lora", "term": "LoRA",
     "text": "The base model's weights normally stay frozen while small adapters are trained. The "
             "result can be a set of adapter files that are used together with the matching base model."},
    {"id": "qlora", "term": "QLoRA",
     "text": "LoRA training on a quantized base model, often 4-bit. This can reduce the GPU memory "
             "needed. The compressed base weights stay frozen while the adapters are trained."},
    {"id": "dpo", "term": "DPO - Direct Preference Optimization",
     "text": "The model is trained on preference examples: a preferred and a less wanted answer to "
             "the same prompt. This needs a dataset that contains both."},
    {"id": "full", "term": "Full fine-tuning",
     "text": "All or most of the model's parameters are updated. For the same model this normally "
             "needs considerably more memory than adapter training."},
    {"id": "continued-pretraining", "term": "Continued pretraining",
     "text": "Further training of an existing model on new bodies of plain text, for example from a "
             "particular field."},
    {"id": "pretraining", "term": "Pretraining from scratch",
     "text": "Training from randomly initialised weights. Large language models need very large "
             "amounts of data and compute for this. The small examples in LayerSmith are function "
             "tests and learning exercises, not a way to produce a useful model."},
    {"id": "checkpoint", "term": "Checkpoint",
     "text": "A saved training state. It lets you resume an interrupted run, or evaluate the model "
             "as it was at an earlier point."},
    {"id": "vram", "term": "How much GPU memory?",
     "text": "Memory use depends on the model size, sequence length, batch size, precision and "
             "training method. LayerSmith does not promise that a model fits in a given amount of "
             "memory: start small (short sequences, batch size 1, gradient accumulation) and increase."},
]

#: Three separate questions. SFT and LoRA are combined, not alternatives: the
#: task says what the model learns, the method says which weights change.
INTENTS = {
    "task": {
        "label": "Training task", "hint": "What should the model learn?",
        "options": [
            {"id": "sft", "label": "Instruction / chat tuning (SFT)"},
            {"id": "dpo", "label": "Preference tuning (DPO)"},
            {"id": "continued-pretraining", "label": "Continued pretraining"},
            {"id": "pretraining", "label": "Pretraining from scratch (function test only)"},
            {"id": "data-prep", "label": "Prepare training data"},
        ],
    },
    "method": {
        "label": "Adaptation method", "hint": "Which weights change?",
        "options": [
            {"id": "lora", "label": "LoRA adapters"},
            {"id": "qlora", "label": "QLoRA (4-bit base + LoRA)"},
            {"id": "full", "label": "Full fine-tuning"},
            {"id": "none", "label": "No training"},
        ],
    },
    "execution": {
        "label": "Execution", "hint": "Where will it run?",
        "options": [
            {"id": "single-gpu", "label": "One GPU"},
            {"id": "multi-gpu", "label": "Several GPUs in one server"},
            {"id": "cpu", "label": "CPU only"},
        ],
    },
}

#: Hardware targets. Other platforms need their own documented recipe before
#: they are offered.
TARGETS = {
    "nvidia-cuda": {
        "name": "NVIDIA GPU - Linux x86_64",
        "architecture": "amd64",
        "gpu": True,
        "summary": "CUDA user-space libraries are inside the image. The NVIDIA driver and the container "
                   "GPU integration stay on the machine that runs the training.",
        "host_requirements": [
            "Linux x86_64 with an NVIDIA GPU supported by the recipe's CUDA version",
            "NVIDIA driver at least the version the recipe names (see the stack)",
            "Docker with the NVIDIA Container Toolkit, or Podman/Docker with CDI "
            "(nvidia-ctk cdi generate)",
        ],
    },
    "cpu": {
        "name": "CPU only - Linux x86_64",
        "architecture": "amd64",
        "gpu": False,
        "summary": "No GPU and no CUDA. For data preparation, development and small function tests.",
        "host_requirements": ["Linux x86_64 with Docker or Podman"],
    },
}

#: Software stacks: one consistent, locked resolution each. Base images are
#: pinned by digest; "devel" is the CUDA toolkit image used when something in
#: the image compiles CUDA code at runtime (DeepSpeed's JIT-built ops).
UBUNTU_2404 = {"image": "docker.io/library/ubuntu", "tag": "24.04",
               "digest": "sha256:a853f94d226358a79c740cfc7bce0c289748f3fe3488d921d038ccd752c61b60"}

STACKS = {
    "hf-cu129": {
        "name": "PyTorch 2.13 - CUDA 12.9",
        "summary": "Current Hugging Face training stack. Library versions follow the combination "
                   "Axolotl 0.20.0 pins and tests.",
        "target": "nvidia-cuda",
        "python": "3.12",
        "torch": "2.13.0", "cuda": "12.9",
        "driver": "NVIDIA driver 575 or newer (CUDA 12.9). Older 12.x drivers (525+) may work through "
                  "CUDA minor-version compatibility; that is not verified.",
        "gpu_support": "Turing (sm_75) and newer, including Blackwell. Older GPUs are not supported by "
                       "this PyTorch build.",
        "base": UBUNTU_2404,
        "devel_base": {"image": "docker.io/nvidia/cuda", "tag": "12.9.1-devel-ubuntu24.04",
                       "digest": "sha256:020bc241a628776338f4d4053fed4c38f6f7f3d7eb5919fecb8de313bb8ba47c"},
    },
    "fa2-cu128": {
        "name": "PyTorch 2.8 - CUDA 12.8 - FlashAttention 2",
        "summary": "Same Hugging Face libraries on PyTorch 2.8, the newest release FlashAttention 2.8.3 "
                   "publishes prebuilt CUDA 12 wheels for. Choose it only when you need FlashAttention.",
        "target": "nvidia-cuda",
        "python": "3.12",
        "torch": "2.8.0", "cuda": "12.8",
        "driver": "NVIDIA driver 570 or newer (CUDA 12.8). Older 12.x drivers (525+) may work through "
                  "CUDA minor-version compatibility; that is not verified.",
        "gpu_support": "Turing (sm_75) and newer for PyTorch; FlashAttention 2 itself needs Ampere "
                       "(sm_80) or newer.",
        "base": UBUNTU_2404,
        "devel_base": {"image": "docker.io/nvidia/cuda", "tag": "12.8.1-devel-ubuntu24.04",
                       "digest": "sha256:520292dbb4f755fd360766059e62956e9379485d9e073bbd2f6e3c20c270ed66"},
    },
    "llamafactory-cu124": {
        "name": "LLaMA-Factory 0.9.5 - PyTorch 2.6 - CUDA 12.4",
        "summary": "LLaMA-Factory's own requirements: PyTorch 2.6.0 as its documentation recommends, every "
                   "other library inside the bounds llamafactory 0.9.5 declares.",
        "target": "nvidia-cuda",
        "python": "3.12",
        "torch": "2.6.0", "cuda": "12.4",
        "driver": "NVIDIA driver 550 or newer (CUDA 12.4). Older 12.x drivers (525+) may work through "
                  "CUDA minor-version compatibility; that is not verified.",
        "gpu_support": "Volta (sm_70) and newer.",
        "base": UBUNTU_2404,
        "devel_base": None,
    },
    "cpu-data": {
        "name": "Python 3.12 - CPU",
        "summary": "Data tools without PyTorch. datasets, transformers and tokenizers match the GPU "
                   "stacks, so data prepared here loads the same way there.",
        "target": "cpu",
        "python": "3.12",
        "torch": None, "cuda": None,
        "driver": None,
        "gpu_support": None,
        "base": UBUNTU_2404,
        "devel_base": None,
    },
}

DOCS = {
    "pytorch": "https://pytorch.org/get-started/locally/",
    "transformers": "https://huggingface.co/docs/transformers/training",
    "datasets": "https://huggingface.co/docs/datasets/index",
    "accelerate": "https://huggingface.co/docs/accelerate/index",
    "peft": "https://huggingface.co/docs/peft/index",
    "trl": "https://huggingface.co/docs/trl/index",
    "bitsandbytes": "https://huggingface.co/docs/transformers/quantization/bitsandbytes",
    "huggingface_hub": "https://huggingface.co/docs/huggingface_hub/index",
    "safetensors": "https://huggingface.co/docs/safetensors/index",
    "tokenizers": "https://huggingface.co/docs/tokenizers/index",
    "jupyter": "https://jupyterlab.readthedocs.io/",
    "tensorboard": "https://www.tensorflow.org/tensorboard",
    "lm-eval": "https://github.com/EleutherAI/lm-evaluation-harness",
    "deepspeed": "https://www.deepspeed.ai/tutorials/advanced-install/",
    "flash-attn": "https://github.com/Dao-AILab/flash-attention",
    "llamafactory": "https://github.com/hiyouga/LlamaFactory",
    "pandas": "https://pandas.pydata.org/docs/",
    "pyarrow": "https://arrow.apache.org/docs/python/",
    "fsdp": "https://huggingface.co/docs/accelerate/usage_guides/fsdp",
    "nvidia-ctk": "https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/cdi-support.html",
    "hf-offline": "https://huggingface.co/docs/transformers/installation",
}

#: Every tool the category can show. "package" names the distribution whose
#: locked version is displayed.
TOOLS = {
    "pytorch": {
        "name": "PyTorch", "package": "torch",
        "what": "The engine that runs the model computations.",
        "used_for": "Computes gradients and updates the model's parameters during training.",
        "when": "Always, for any training in these images.",
        "pulls_in": "CUDA user-space libraries (cuBLAS, cuDNN, NCCL) as Python wheels, and Triton.",
        "limitations": "The CUDA build decides which GPUs and drivers work; see the stack.",
    },
    "transformers": {
        "name": "Transformers", "package": "transformers",
        "what": "Model architectures, tokenizers and pretrained models behind one interface.",
        "used_for": "Loads models and tokenizers, applies chat templates, and trains and tests many "
                    "model families the same way.",
        "when": "Whenever you load a Hugging Face format model.",
        "pulls_in": "huggingface_hub, tokenizers, safetensors.",
        "limitations": "Very new model families can need a newer release than the one locked here.",
    },
    "datasets": {
        "name": "Datasets", "package": "datasets",
        "what": "Reads, processes and streams training data.",
        "used_for": "Loads local JSONL, CSV and Parquet files and makes training and validation splits.",
        "when": "Whenever training data is loaded from files.",
        "pulls_in": "pyarrow, pandas, fsspec.",
        "limitations": "Hub datasets need network access the first time unless they are already cached.",
    },
    "accelerate": {
        "name": "Accelerate", "package": "accelerate",
        "what": "Runs the same training code on one or several GPUs.",
        "used_for": "Mixed precision, multi-GPU launch (accelerate launch) and FSDP/DeepSpeed integration.",
        "when": "Always used by the trainers; used directly for multi-GPU runs.",
        "pulls_in": "psutil, pyyaml.",
        "limitations": "Training across several servers also needs a working network and host setup.",
    },
    "peft": {
        "name": "PEFT", "package": "peft",
        "what": "Parameter-efficient fine-tuning, such as LoRA adapters.",
        "used_for": "Trains a small set of extra parameters instead of the whole model. This normally "
                    "reduces memory use and the size of the result.",
        "when": "LoRA and QLoRA training.",
        "pulls_in": "Nothing beyond the base stack.",
        "limitations": "An adapter only works with the base model (and revision) it was trained on.",
    },
    "trl": {
        "name": "TRL", "package": "trl",
        "what": "Ready-made trainers for language model training.",
        "used_for": "Supervised fine-tuning (SFTTrainer) and preference training (DPOTrainer), with "
                    "Transformers and, if you want, PEFT.",
        "when": "SFT and DPO.",
        "pulls_in": "Nothing beyond the base stack.",
        "limitations": "Expects datasets in its documented formats (messages, or prompt/chosen/rejected).",
    },
    "bitsandbytes": {
        "name": "bitsandbytes", "package": "bitsandbytes",
        "what": "4- and 8-bit quantization and memory-saving optimizers.",
        "used_for": "Loads the base model compressed to 4 bits in the QLoRA recipe while adapters train.",
        "when": "QLoRA, and 8-bit optimizers.",
        "pulls_in": "Native CUDA kernels shipped in the wheel.",
        "limitations": "Quantized training needs an NVIDIA GPU; it is not exercised by the CPU smoke test.",
    },
    "huggingface_hub": {
        "name": "huggingface_hub", "package": "huggingface-hub",
        "what": "Downloads and caches model files and other resources from Hugging Face.",
        "used_for": "Fetching models into the mounted cache, and running offline from that cache "
                    "(HF_HUB_OFFLINE=1).",
        "when": "Whenever a model is referred to by its Hub name instead of a local path.",
        "pulls_in": "hf-xet, httpx.",
        "limitations": "Private or gated models need a token, passed at runtime - never baked into the image.",
    },
    "safetensors": {
        "name": "Safetensors", "package": "safetensors",
        "what": "A file format and library for storing and loading model weights.",
        "used_for": "Saving adapters and checkpoints safely (no code execution on load).",
        "when": "Always.",
        "pulls_in": "Nothing.",
        "limitations": "None in practice.",
    },
    "tokenizers": {
        "name": "Tokenizers", "package": "tokenizers",
        "what": "Fast tokenizer implementations.",
        "used_for": "Turns text into token ids; also used to count tokens when preparing data.",
        "when": "Always.",
        "pulls_in": "Nothing.",
        "limitations": "Use the tokenizer that belongs to the model you train.",
    },
    "sentencepiece": {
        "name": "SentencePiece", "package": "sentencepiece",
        "what": "Tokenizer library some model families still need.",
        "used_for": "Loading slow/legacy tokenizers (for example some Llama, T5 and Mistral variants).",
        "when": "When a model's tokenizer is a SentencePiece model.",
        "pulls_in": "Nothing.",
        "limitations": "Other model-specific dependencies (tiktoken, protobuf) may be needed by some models.",
    },
    "pyyaml": {
        "name": "PyYAML", "package": "pyyaml",
        "what": "YAML support.",
        "used_for": "Reading training and Accelerate configuration files.",
        "when": "Configuration files.",
        "pulls_in": "Nothing.",
        "limitations": "None.",
    },
    "jupyter": {
        "name": "JupyterLab + ipykernel", "package": "jupyterlab",
        "what": "A browser workspace for Python and notebooks.",
        "used_for": "Exploring datasets and developing training code step by step.",
        "when": "Interactive work. Not needed for scripted training runs.",
        "pulls_in": "About 70 packages (Jupyter server, ipykernel, ipywidgets).",
        "limitations": "Started with a token; the examples publish it on the host's localhost only.",
    },
    "tensorboard": {
        "name": "TensorBoard", "package": "tensorboard",
        "what": "Shows training curves and metrics from local log files.",
        "used_for": "Watching loss and learning rate, and comparing training runs. Logs stay on your disk.",
        "when": "Recommended for every training run; the simple choice for local logging.",
        "pulls_in": "A few small packages (grpcio, markdown, werkzeug).",
        "limitations": "External tracking services (W&B and the like) are not included and are off by default.",
    },
    "lm-eval": {
        "name": "lm-evaluation-harness", "package": "lm-eval",
        "what": "Standardised evaluations for language models.",
        "used_for": "Comparing the base model with a fine-tuned model on the same benchmarks.",
        "when": "After training, to measure the effect.",
        "pulls_in": "evaluate, scikit-learn, sacrebleu, rouge-score and more.",
        "limitations": "Benchmark datasets are downloaded on first use; for offline use pre-fetch them "
                       "into the cache.",
    },
    "deepspeed": {
        "name": "DeepSpeed", "package": "deepspeed",
        "what": "Advanced training features, such as ZeRO memory partitioning and CPU/NVMe offload.",
        "used_for": "Training models that do not fit with plain data parallelism.",
        "when": "Full fine-tuning of larger models, usually on several GPUs.",
        "pulls_in": "The CUDA development base image (nvcc), because DeepSpeed compiles its CUDA ops "
                    "when they are first used; ninja, hjson, pydantic.",
        "limitations": "A successful install does not prove every CUDA op works: ops compile on the "
                       "GPU machine at first use. Run ds_report and the GPU check there.",
    },
    "flash-attn": {
        "name": "FlashAttention 2", "package": "flash-attn",
        "what": "Optimised attention kernels.",
        "used_for": "Can reduce memory use and run time for attention on supported GPUs "
                    "(attn_implementation='flash_attention_2').",
        "when": "Long sequences on Ampere or newer GPUs.",
        "pulls_in": "A prebuilt wheel matched to PyTorch 2.8 / CUDA 12 (no compilation at build time).",
        "limitations": "Needs an Ampere (sm_80) or newer GPU and a supported model. Without it, PyTorch's "
                       "built-in SDPA attention (attn_implementation='sdpa') is the alternative.",
    },
    "llamafactory": {
        "name": "LLaMA-Factory", "package": "llamafactory",
        "what": "Fine-tuning framework with a browser interface (LLaMA Board) and a YAML command line.",
        "used_for": "Configuring and running SFT, DPO, LoRA and QLoRA without writing training code.",
        "when": "When you prefer a web interface or LLaMA-Factory's YAML recipes.",
        "pulls_in": "Gradio, modelscope, matplotlib, and its own pinned ranges of the Hugging Face libraries.",
        "limitations": "Follows its own dependency bounds, so its library versions are older than the "
                       "Hugging Face profile's.",
    },
    "nltk-metrics": {
        "name": "Metrics (nltk, jieba, rouge-chinese)", "package": "nltk",
        "what": "LLaMA-Factory's optional metrics requirements.",
        "used_for": "BLEU/ROUGE scores during prediction runs.",
        "when": "Evaluation in LLaMA-Factory.",
        "pulls_in": "Nothing large.",
        "limitations": "nltk data packages are downloaded separately when needed.",
    },
    "pandas": {
        "name": "pandas", "package": "pandas",
        "what": "Tables in memory.",
        "used_for": "Inspecting, cleaning and converting CSV, JSONL and Parquet data.",
        "when": "Data preparation.",
        "pulls_in": "numpy, python-dateutil.",
        "limitations": "Very large datasets are better processed with Datasets (memory-mapped Arrow).",
    },
    "pyarrow": {
        "name": "PyArrow", "package": "pyarrow",
        "what": "Apache Arrow and Parquet support.",
        "used_for": "Reading and writing Parquet; the storage format under Datasets.",
        "when": "Data preparation.",
        "pulls_in": "Nothing.",
        "limitations": "None.",
    },
    "numpy": {
        "name": "NumPy", "package": "numpy",
        "what": "Numerical arrays.",
        "used_for": "Statistics over token lengths and other data checks.",
        "when": "Data preparation.",
        "pulls_in": "Nothing.",
        "limitations": "None.",
    },
}
for _tool_id, _tool in TOOLS.items():
    _tool["docs"] = DOCS.get(_tool_id) or DOCS.get(_tool["package"], "")

#: System tools that come from the base image's package manager. Capability
#: names, translated by the core catalog like any other package.
SYSTEM_TOOLS = {
    "gpu": {
        "packages": ["python3", "venv", "python3-devel", "git", "ca-certificates", "curl", "gcc",
                     "libc6-dev", "procps", "less"],
        "why": "Python 3.12 and venv for the locked environment; git and CA certificates to fetch code "
               "and models; gcc, libc headers and Python headers because Triton (shipped with PyTorch) "
               "compiles a small launcher the first time a Triton kernel runs, for example with "
               "torch.compile or bitsandbytes.",
    },
    "cpu": {
        "packages": ["python3", "venv", "git", "ca-certificates", "curl", "procps", "less"],
        "why": "Python 3.12 and venv for the locked environment; git and CA certificates. No compiler: "
               "nothing in this profile builds native code.",
    },
}

#: Optional add-ons. "lock" names the <addon>.lock file in the stack directory.
ADDONS = {
    "tensorboard": {
        "name": "TensorBoard", "tools": ["tensorboard"], "lock": "tensorboard",
        "summary": "Local training curves. Recommended.",
        "ports": [{"port": 6006, "name": "TensorBoard"}],
    },
    "jupyter": {
        "name": "JupyterLab", "tools": ["jupyter"], "lock": "jupyter",
        "summary": "Notebook workspace in the browser, started with token authentication.",
        "ports": [{"port": 8888, "name": "JupyterLab"}],
    },
    "lm-eval": {
        "name": "lm-evaluation-harness", "tools": ["lm-eval"], "lock": "lm-eval",
        "summary": "Standardised benchmarks to compare base and fine-tuned models.",
        "ports": [],
    },
    "deepspeed": {
        "name": "DeepSpeed", "tools": ["deepspeed"], "lock": "deepspeed",
        "summary": "ZeRO and offload for large full fine-tuning. Switches to the CUDA development base.",
        "needs_devel_base": True,
        "ports": [],
    },
    "flash-attn": {
        "name": "FlashAttention 2", "tools": ["flash-attn"], "lock": "flash-attn",
        "summary": "Faster attention on Ampere and newer GPUs. Needs the PyTorch 2.8 stack.",
        "stacks": ["fa2-cu128"],
        "ports": [],
    },
}

GPU_CORE_TOOLS = [
    ("pytorch", "Runs every computation in training."),
    ("transformers", "Loads the model, its tokenizer and chat template."),
    ("datasets", "Loads your JSONL/CSV/Parquet training and validation files."),
    ("accelerate", "Used by every trainer for device placement, mixed precision and multi-GPU launch."),
    ("peft", "Provides the LoRA adapters the recipe trains."),
    ("trl", "Provides SFTTrainer and DPOTrainer used by the examples."),
    ("bitsandbytes", "Needed for the QLoRA recipe (4-bit base model)."),
    ("huggingface_hub", "Downloads models into the mounted cache and enables offline use."),
    ("safetensors", "Stores adapters and checkpoints."),
    ("tokenizers", "Fast tokenization for every model."),
    ("sentencepiece", "Some model families' tokenizers need it."),
    ("pyyaml", "Reads Accelerate and training configuration files."),
]

PROFILES = [
    {
        "id": "hf-finetune",
        "name": "Hugging Face Fine-tuning",
        "recommended": True,
        "summary": "Adapt an existing model to your own data with LoRA or QLoRA.",
        "description": (
            "Builds an environment for adapting an existing language model with your own training data. "
            "The profile contains the tools to load models, process datasets and train LoRA or QLoRA "
            "adapters. Model files and training data are added separately."
        ),
        "best_for": ["Instruction tuning (SFT) with LoRA or QLoRA", "Preference tuning (DPO)",
                     "One GPU, or several GPUs in one server"],
        "targets": ["nvidia-cuda"],
        "stacks": ["hf-cu129"],
        "tools": GPU_CORE_TOOLS,
        "addons": ["tensorboard", "jupyter", "lm-eval"],
        "default_addons": ["tensorboard"],
        "examples": "hf",
        "intent": {"task": "sft", "method": "lora", "execution": "single-gpu"},
        "fits": {"task": ["sft", "dpo"], "method": ["lora", "qlora", "full"],
                 "execution": ["single-gpu", "multi-gpu"]},
    },
    {
        "id": "pytorch-advanced",
        "name": "PyTorch / Hugging Face Advanced Training",
        "recommended": False,
        "summary": "Your own training scripts, full fine-tuning or continued pretraining.",
        "description": (
            "For running your own training code, full fine-tuning or continued pretraining. Includes the "
            "PyTorch and Hugging Face base stack, single-GPU examples and documented multi-GPU "
            "configurations (DDP and FSDP with Accelerate). DeepSpeed and FlashAttention are controlled "
            "add-ons that change the base image or stack when selected."
        ),
        "best_for": ["Full fine-tuning", "Continued pretraining on your own text",
                     "Custom training loops", "Several GPUs in one server (DDP, FSDP, DeepSpeed)"],
        "targets": ["nvidia-cuda"],
        "stacks": ["hf-cu129", "fa2-cu128"],
        "tools": GPU_CORE_TOOLS,
        "addons": ["tensorboard", "jupyter", "lm-eval", "deepspeed", "flash-attn"],
        "default_addons": ["tensorboard"],
        "examples": "advanced",
        "intent": {"task": "continued-pretraining", "method": "full", "execution": "multi-gpu"},
        "fits": {"task": ["sft", "dpo", "continued-pretraining", "pretraining"],
                 "method": ["lora", "qlora", "full"], "execution": ["single-gpu", "multi-gpu"]},
    },
    {
        "id": "llama-factory",
        "name": "LLaMA-Factory - Web UI",
        "recommended": False,
        "summary": "Configure and run fine-tuning from LLaMA-Factory's browser interface.",
        "description": (
            "A self-contained image for configuring and running fine-tuning from LLaMA Board, "
            "LLaMA-Factory's web interface, or from its YAML command line. Installed the way the project "
            "documents (pip, with the metrics and bitsandbytes extras) and locked within its own "
            "dependency bounds. The interface is started with a login."
        ),
        "best_for": ["Fine-tuning without writing code", "LoRA/QLoRA SFT and DPO from a browser",
                     "Repeatable runs from LLaMA-Factory YAML files"],
        "targets": ["nvidia-cuda"],
        "stacks": ["llamafactory-cu124"],
        "tools": [
            ("llamafactory", "The application this profile is built around."),
            ("pytorch", "LLaMA-Factory's recommended PyTorch release."),
            ("transformers", "Model loading, within LLaMA-Factory's supported range."),
            ("datasets", "Dataset loading, within LLaMA-Factory's supported range."),
            ("peft", "LoRA and QLoRA adapters."),
            ("trl", "DPO and other preference trainers LLaMA-Factory uses."),
            ("accelerate", "Device placement and multi-GPU launch."),
            ("bitsandbytes", "QLoRA (4-bit) in LLaMA Board."),
            ("nltk-metrics", "Included by LLaMA-Factory's documented install."),
            ("huggingface_hub", "Model downloads into the mounted cache."),
        ],
        "addons": ["tensorboard"],
        "default_addons": ["tensorboard"],
        "examples": "llamafactory",
        "intent": {"task": "sft", "method": "lora", "execution": "single-gpu"},
        "fits": {"task": ["sft", "dpo"], "method": ["lora", "qlora", "full"],
                 "execution": ["single-gpu", "multi-gpu"]},
        "ports": [{"port": 7860, "name": "LLaMA Board"}],
    },
    {
        "id": "dataset-prep",
        "name": "Dataset Preparation - CPU",
        "recommended": False,
        "summary": "Validate, clean, convert and split training data. No GPU needed.",
        "description": (
            "A lighter environment for preparing training data without a GPU: validation, filtering, "
            "splitting and format conversion between JSONL, CSV and Parquet, and token counts with the "
            "tokenizer of the model you will train. Meant for data preparation, development and small "
            "function tests - not for training models."
        ),
        "best_for": ["Checking JSONL chat and preference data", "Train/validation splits",
                     "Converting between JSONL, CSV and Parquet", "Token length statistics"],
        "targets": ["cpu"],
        "stacks": ["cpu-data"],
        "tools": [
            ("datasets", "Loads and splits JSONL, CSV and Parquet files."),
            ("pandas", "Inspects and cleans tables."),
            ("numpy", "Statistics over lengths and labels."),
            ("pyarrow", "Parquet reading and writing."),
            ("tokenizers", "Counts tokens with the target model's tokenizer."),
            ("transformers", "Loads a model's tokenizer and chat template (no PyTorch needed)."),
            ("sentencepiece", "Some tokenizers need it."),
            ("huggingface_hub", "Fetches tokenizers into the cache, or works offline from it."),
            ("pyyaml", "Configuration files."),
        ],
        "addons": ["jupyter"],
        "default_addons": ["jupyter"],
        "examples": "data",
        "intent": {"task": "data-prep", "method": "none", "execution": "cpu"},
        "fits": {"task": ["data-prep"], "method": ["none"], "execution": ["cpu"]},
    },
]

PROFILES_BY_ID = {profile["id"]: profile for profile in PROFILES}

#: Reference builds of the standard recipes: what was actually built and
#: checked, where, and what it measured. Shown as the basis for size and time
#: estimates before a build; a build's own measurements replace them after.
#: GPU checks are listed only when one has really been run on recorded hardware.
REFERENCE_BUILDS = [
    {"profile": "hf-finetune", "stack": "hf-cu129", "addons": ["tensorboard", "jupyter"],
     "date": "2026-10-02", "host": "8-core x86_64 VM, Docker 29, no GPU",
     "image_bytes": 13_511_904_794, "archive_bytes": 4_750_807_040, "build_seconds": 1679,
     "checks": ["image", "deps", "cpu", "offline"], "gpu": None},
    {"profile": "llama-factory", "stack": "llamafactory-cu124", "addons": ["tensorboard"],
     "date": "2026-10-02", "host": "8-core x86_64 VM, Docker 29, no GPU",
     "image_bytes": 11_366_620_357, "archive_bytes": 3_815_855_104, "build_seconds": 1698,
     "checks": ["image", "deps", "cpu", "offline"], "gpu": None,
     "also": "LLaMA Board login (401 without password) and JupyterLab token checked"},
    {"profile": "pytorch-advanced", "stack": "fa2-cu128", "addons": ["tensorboard", "deepspeed", "flash-attn"],
     "date": "2026-10-02", "host": "8-core x86_64 VM, Docker 29, no GPU",
     "image_bytes": 27_967_410_265, "archive_bytes": 9_754_527_232, "build_seconds": 2884,
     "checks": ["image", "deps", "cpu", "offline"], "gpu": None,
     "also": "DeepSpeed builds from source with nvcc 12.8; the FlashAttention CUDA extension loads"},
    {"profile": "dataset-prep", "stack": "cpu-data", "addons": ["jupyter"],
     "date": "2026-10-02", "host": "4-core x86_64 VM, Podman 5.8, no GPU",
     "image_bytes": 897_818_710, "archive_bytes": 291_542_016, "build_seconds": 261,
     "checks": ["image", "deps", "cpu", "offline"], "gpu": None,
     "also": "Air-gap bundle checksums verified and the archive loaded with podman load"},
]
