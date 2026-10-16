"""Curated LLMs for GPU benchmarking, and which files of a model to fetch.

Every entry is an open model on Hugging Face (no licence click-through, no
token). Sizes are what LayerSmith actually downloads - the files vLLM loads,
not the whole repository - measured when the entry was added; the download
itself always uses the sizes Hugging Face reports at that moment.

`gpus` is the smallest number of NVIDIA H200 (141 GB HBM3e) that serves the
model with vLLM at its stored precision and leaves room for the KV cache.
"""

import re

#: Kinds of test, in the order the Models page shows them.
PURPOSES = {
    "smoke": "Quick smoke test",
    "baseline": "8B baseline",
    "fp8": "FP8 (Hopper/Blackwell FP8 kernels)",
    "moe": "Mixture of experts",
    "mid": "Mid-size dense",
    "large": "Large, one GPU",
    "multi": "Multi-GPU (tensor parallel)",
}

CATALOG = [
    {"key": "qwen2.5-0.5b-instruct", "repo_id": "Qwen/Qwen2.5-0.5B-Instruct", "purpose": "smoke",
     "params": "0.5B", "precision": "BF16", "size_gb": 1.0, "gpus": 1, "license": "apache-2.0",
     "why": "Loads in seconds; proves the whole vLLM path works before a long run."},
    {"key": "qwen3-8b", "repo_id": "Qwen/Qwen3-8B", "purpose": "baseline",
     "params": "8B", "precision": "BF16", "size_gb": 16.4, "gpus": 1, "license": "apache-2.0",
     "why": "The common reference point for tokens/s per GPU; compare runs and GPUs on this first."},
    {"key": "llama-3.1-8b-instruct-fp8", "repo_id": "RedHatAI/Meta-Llama-3.1-8B-Instruct-FP8", "purpose": "fp8",
     "params": "8B", "precision": "FP8", "size_gb": 9.1, "gpus": 1, "license": "llama3.1",
     "why": "Same size class as the baseline in FP8, so the FP8 speed-up on H200 shows directly."},
    {"key": "gpt-oss-20b", "repo_id": "openai/gpt-oss-20b", "purpose": "moe",
     "params": "21B (3.6B active)", "precision": "MXFP4", "size_gb": 13.8, "gpus": 1, "license": "apache-2.0",
     "why": "Small MoE with MXFP4 weights: high throughput, exercises a different kernel path."},
    {"key": "phi-4", "repo_id": "microsoft/phi-4", "purpose": "mid",
     "params": "14B", "precision": "BF16", "size_gb": 29.3, "gpus": 1, "license": "mit",
     "why": "Dense mid-size model between the 8B and 32B classes."},
    {"key": "qwen3-30b-a3b", "repo_id": "Qwen/Qwen3-30B-A3B", "purpose": "moe",
     "params": "30B (3B active)", "precision": "BF16", "size_gb": 61.1, "gpus": 1, "license": "apache-2.0",
     "why": "MoE whose weights fill much of one GPU while compute per token stays small."},
    {"key": "qwen3-32b", "repo_id": "Qwen/Qwen3-32B", "purpose": "mid",
     "params": "32B", "precision": "BF16", "size_gb": 65.5, "gpus": 1, "license": "apache-2.0",
     "why": "Dense 32B in BF16: memory-bandwidth bound decode, a good HBM3e test."},
    {"key": "gpt-oss-120b", "repo_id": "openai/gpt-oss-120b", "purpose": "large",
     "params": "117B (5.1B active)", "precision": "MXFP4", "size_gb": 65.2, "gpus": 1, "license": "apache-2.0",
     "why": "Designed to fit one 80 GB GPU; on H200 it leaves a large KV cache for high concurrency."},
    {"key": "llama-3.3-70b-instruct-fp8", "repo_id": "RedHatAI/Llama-3.3-70B-Instruct-FP8-dynamic",
     "purpose": "large", "params": "70B", "precision": "FP8", "size_gb": 72.7, "gpus": 1, "license": "llama3.3",
     "why": "The classic 70B on one H200 thanks to FP8; ungated copy of Meta's model."},
    {"key": "qwen2.5-72b-instruct", "repo_id": "Qwen/Qwen2.5-72B-Instruct", "purpose": "multi",
     "params": "72B", "precision": "BF16", "size_gb": 145.4, "gpus": 2, "license": "qwen",
     "why": "Does not fit one GPU in BF16: tests tensor parallelism (--tensor-parallel-size 2) and NVLink."},
    {"key": "qwen3-235b-a22b-fp8", "repo_id": "Qwen/Qwen3-235B-A22B-FP8", "purpose": "multi",
     "params": "235B (22B active)", "precision": "FP8", "size_gb": 239.0, "gpus": 2, "license": "apache-2.0",
     "why": "Large MoE across 2-4 GPUs; the heaviest realistic single-node test."},
]

BY_KEY = {entry["key"]: entry for entry in CATALOG}

REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
#: Folder names, used as-is on the GPU host and inside generated scripts.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

#: What vLLM reads from a Hugging Face model folder. Only top-level files:
#: subfolders hold other formats (original/, metal/, onnx/, gguf ...).
WANTED = re.compile(
    r"^(?:[^/]+\.safetensors|[^/]+\.json|tokenizer\.model|[^/]+\.tiktoken|merges\.txt|vocab\.txt"
    r"|[^/]+\.jinja|README\.md|LICENSE(?:\.[a-z]+)?|NOTICE(?:\.[a-z]+)?|USE_POLICY\.md)$")


def select_files(siblings: list[dict]) -> list[dict]:
    """The files to download from a model repository listing.

    `siblings` is Hugging Face's list ({"rfilename", "size", "lfs"}). Mistral
    and others ship the same weights twice (consolidated.safetensors for
    their own loader, model-*.safetensors for transformers); only the second
    is kept when both are there.
    """
    files = [s for s in siblings if WANTED.match(s["rfilename"])]
    names = {s["rfilename"] for s in files}
    if any(n.startswith("model") and n.endswith(".safetensors") for n in names):
        files = [s for s in files if not s["rfilename"].startswith("consolidated")]
    return [{"name": s["rfilename"], "size": int((s.get("lfs") or {}).get("size") or s.get("size") or 0),
             "sha256": (s.get("lfs") or {}).get("sha256")} for s in files]


def has_weights(files: list[dict]) -> bool:
    return any(f["name"].endswith(".safetensors") for f in files) and any(f["name"] == "config.json" for f in files)


def folder_name(repo_id: str, taken: set[str]) -> str:
    """Qwen/Qwen3-8B -> Qwen3-8B, or Org--Name when the short name is taken."""
    owner, _, name = repo_id.partition("/")
    for candidate in (name, f"{owner}--{name}"):
        if candidate not in taken and NAME_RE.match(candidate):
            return candidate
    index = 2
    while f"{owner}--{name}-{index}" in taken:
        index += 1
    return f"{owner}--{name}-{index}"
