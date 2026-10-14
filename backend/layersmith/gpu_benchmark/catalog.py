"""Versioned, supported NVIDIA GPU benchmark image recipes."""

import re

CUDA_VARIANTS = {
    "12.8.2": {
        "base": "nvidia/cuda:12.8.2-devel-ubuntu22.04",
        "torch_index": "https://download.pytorch.org/whl/cu128",
        "torch": "2.11.0",
        "vllm": None,
        "cuda_samples": "v12.8",
    },
    "13.0.2": {
        "base": "nvidia/cuda:13.0.2-devel-ubuntu22.04",
        "torch_index": "https://download.pytorch.org/whl/cu130",
        "torch": "2.11.0",
        "vllm": "0.26.0",
        # bandwidthTest was removed from CUDA Samples 12.9 onward. Keep this
        # requested legacy utility pinned to the last release containing it.
        "cuda_samples": "v12.8",
    },
}

PYTHON_VERSIONS = {"3.12": "3.12.15", "3.13": "3.13.16"}
TOOL_GROUPS = {
    "llm": "vLLM, PyTorch, Transformers and Tokenizers",
    "compute": "CUDA and PyTorch compute benchmarks",
    "memory": "CUDA Samples bandwidthTest and transfer tests",
    "nccl": "NCCL, nccl-tests, OpenMPI and RDMA diagnostics",
}
DEFAULT_TOOLS = list(TOOL_GROUPS)
MIRROR_URL_RE = re.compile(r"^(?:https?|file)://[A-Za-z0-9._:/?#\[\]%&=+~-]{1,400}$")
MIRROR_KEYS = ("apt_main", "apt_security", "pypi_index", "python_mirror",
               "cuda_samples_repository", "nccl_tests_repository")

CATEGORY = {
    "id": "gpu-benchmark",
    "name": "AI / ML / GPU",
    "intro": "Build a versioned NVIDIA GPU benchmarking toolkit for one GPU, one host or a multi-node cluster.",
    "template": "NVIDIA GPU Benchmark Toolkit",
    "description": "NVIDIA GPU benchmarking toolkit for LLM inference, CUDA compute, memory bandwidth and multi-node NCCL performance.",
}


class RecipeError(ValueError):
    """A user-facing error in a GPU benchmark image selection."""


def resolve(request: dict | None = None) -> dict:
    request = request or {}
    if not isinstance(request, dict):
        raise RecipeError("gpu_benchmark must be an object")
    cuda = str(request.get("cuda_version") or "13.0.2")
    python = str(request.get("python_version") or "3.12")
    if cuda not in CUDA_VARIANTS:
        raise RecipeError(f"Unsupported CUDA version: {cuda}")
    if python not in PYTHON_VERSIONS:
        raise RecipeError(f"Unsupported Python version: {python}")
    tools = request.get("tools", DEFAULT_TOOLS)
    if not isinstance(tools, list) or not tools or not all(isinstance(value, str) for value in tools):
        raise RecipeError("Select at least one benchmark tool group")
    unknown = sorted(set(tools) - set(TOOL_GROUPS))
    if unknown:
        raise RecipeError("Unknown benchmark tool group: " + ", ".join(unknown))
    variant = CUDA_VARIANTS[cuda]
    if "llm" in tools and not variant["vllm"]:
        raise RecipeError("CUDA 12.8.2 has no matching pinned vLLM wheel in this recipe. Choose CUDA 13.0.2 or omit the LLM tool group.")
    # Keep deterministic order so identical selections render the same source.
    selected = [name for name in TOOL_GROUPS if name in set(tools)]
    supplied_mirrors = request.get("mirrors", {})
    if supplied_mirrors is None:
        supplied_mirrors = {}
    if not isinstance(supplied_mirrors, dict):
        raise RecipeError("mirrors must be an object of HTTP(S) mirror URLs")
    unknown_mirrors = sorted(set(supplied_mirrors) - set((*MIRROR_KEYS, "torch_index")))
    if unknown_mirrors:
        raise RecipeError("Unknown mirror setting: " + ", ".join(unknown_mirrors))
    mirrors = {
        "apt_main": "http://archive.ubuntu.com/ubuntu/",
        "apt_security": "http://security.ubuntu.com/ubuntu/",
        "pypi_index": "https://pypi.org/simple",
        "torch_index": variant["torch_index"],
        "python_mirror": "https://github.com/astral-sh/python-build-standalone/releases/download",
        "cuda_samples_repository": "https://github.com/NVIDIA/cuda-samples.git",
        "nccl_tests_repository": "https://github.com/NVIDIA/nccl-tests.git",
    }
    for key in (*MIRROR_KEYS, "torch_index"):
        value = supplied_mirrors.get(key)
        if value is not None:
            if not isinstance(value, str) or not MIRROR_URL_RE.fullmatch(value):
                raise RecipeError(f"{key} must be an HTTP(S) or file URL without embedded credentials")
            mirrors[key] = value
    return {
        "cuda_version": cuda,
        "python_version": python,
        "python_full_version": PYTHON_VERSIONS[python],
        "tools": selected,
        "base": variant["base"],
        "torch_index": variant["torch_index"],
        "torch": variant["torch"],
        "vllm": variant["vllm"],
        "transformers": "5.17.0" if "llm" in selected else None,
        "tokenizers": "0.22.2" if "llm" in selected else None,
        "mirrors": mirrors,
        "cuda_samples": variant["cuda_samples"],
        "nccl_tests": "v2.18.3",
    }
