"""NVIDIA GPU & LLM Benchmark image recipes: pinned, compatibility-checked versions.

Compatibility matrix (verified against the published package metadata):

  CUDA 13.0.2 (default)  driver >= 580.65.06
    PyTorch 2.11.0 from PyPI is built for CUDA 13.0.2 (cuda-toolkit 13.0.2,
    NCCL 2.28.9). vLLM 0.26.0's PyPI wheel is its CUDA 13.0 build and pins
    torch==2.11.0, so the two always match. Kernels: sm_75 ... sm_120, which
    covers Hopper (H100/H200, sm_90) and Blackwell (B200 sm_100, RTX PRO 6000
    sm_120).
  CUDA 12.8.2            driver >= 570.26.00
    PyTorch 2.11.0+cu128 from the PyTorch index. No vLLM: the vLLM 0.26.0
    CUDA 12 wheel is built for 12.9. For older drivers; compute, memory and
    NCCL tests only.

The NVIDIA driver is never part of an image; it comes from the host.
"""

import re

CUDA_VARIANTS = {
    "13.0.2": {
        "base": "docker.io/nvidia/cuda:13.0.2-base-ubuntu24.04",
        "cuda_packages": "13-0",
        "driver_minimum": "580.65.06",
        "torch": "2.11.0",
        "torchvision": "0.26.0",
        "torchaudio": "2.11.0",
        # PyPI's default torch 2.11.0 wheel is the CUDA 13.0 build.
        "torch_index": None,
        "vllm": "0.26.0",
        "nccl": "2.28.9",
        "label": "CUDA 13.0.2 · PyTorch 2.11.0 · vLLM 0.26.0 (recommended for H200 and Blackwell)",
    },
    "12.8.2": {
        "base": "docker.io/nvidia/cuda:12.8.2-base-ubuntu24.04",
        "cuda_packages": "12-8",
        "driver_minimum": "570.26.00",
        "torch": "2.11.0",
        "torchvision": "0.26.0",
        "torchaudio": "2.11.0",
        "torch_index": "https://download.pytorch.org/whl/cu128",
        "vllm": None,
        "nccl": None,
        "label": "CUDA 12.8.2 · PyTorch 2.11.0 · no vLLM (for drivers 570-579)",
    },
}

PYTHON_VERSIONS = {"3.12": "3.12.14", "3.13": "3.13.15"}  # what uv 0.12.17 can install
PINS = {"uv": "0.12.17", "transformers": "5.17.0", "tokenizers": "0.23.3", "textual": "8.2.8",
        "nvidia-ml-py": "13.580.82", "nccl-tests": "v2.21.1", "nvbandwidth": "v0.10.0"}
GPU_ARCHITECTURES = (75, 80, 86, 89, 90, 100, 120)

#: Selectable benchmark components (image contents).
TOOL_GROUPS = {
    "llm": "LLM inference and token speed (vLLM, Transformers, Tokenizers)",
    "compute": "GPU compute: FP32/TF32/FP16/BF16/FP8 GEMM (PyTorch, CUDA events)",
    "memory": "GPU memory bandwidth: VRAM/HBM, PCIe, GPU-to-GPU (PyTorch, nvbandwidth)",
    "nccl": "Multi-GPU and multi-node: NCCL, nccl-tests, OpenMPI, RDMA tools",
}
#: Always part of the image; shown in the wizard as fixed.
CORE_COMPONENTS = {
    "diagnostics": "NVIDIA GPU diagnostics and health (NVML)",
    "reports": "HTML, JSON and CSV reports and comparisons",
    "tui": "Interactive terminal dashboard (Textual)",
}
DEFAULT_TOOLS = list(TOOL_GROUPS)

MIRROR_URL_RE = re.compile(r"^(?:https?|file)://[A-Za-z0-9._:/?\[\]%&=+~-]{1,400}$")
MIRROR_KEYS = ("apt_main", "apt_security", "pypi_index", "torch_index", "python_mirror",
               "nccl_tests_repository", "nvbandwidth_repository")
#: Accepted and ignored: saved by 0.5.0, whose bandwidthTest is replaced by nvbandwidth.
LEGACY_MIRROR_KEYS = ("cuda_samples_repository",)

CATEGORY = {
    "id": "gpu-benchmark",
    "name": "NVIDIA GPU & LLM Benchmark",
    "intro": "Build one portable Podman image that benchmarks NVIDIA GPUs and local LLMs, offline.",
    "template": "NVIDIA GPU & LLM Benchmark",
    "description": ("Benchmark NVIDIA GPUs and AI models. Measure token speed, inference latency, GPU compute, "
                    "memory bandwidth and multi-GPU performance."),
}
#: Template names projects may carry (0.5.0 used the first).
TEMPLATE_NAMES = ("NVIDIA GPU Benchmark Toolkit", CATEGORY["template"])

# Installed sizes in GB, from the reference build (see docs/gpu-benchmark.md).
SIZE_GB = {"base": 0.25, "toolchain": 1.3, "python": 0.15, "torch": {"13.0.2": 6.4, "12.8.2": 5.6},
           "llm": 2.4, "nccl": 0.15, "memory": 0.02, "tui": 0.03}


class RecipeError(ValueError):
    """A user-facing error in a GPU benchmark image selection."""


def estimate_size_gb(cuda_version: str, tools: list[str]) -> float:
    size = SIZE_GB["base"] + SIZE_GB["toolchain"] + SIZE_GB["python"] + SIZE_GB["tui"]
    size += SIZE_GB["torch"][cuda_version]
    for tool in ("llm", "nccl", "memory"):
        if tool in tools:
            size += SIZE_GB[tool]
    return round(size, 1)


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
        raise RecipeError("Select at least one benchmark component")
    unknown = sorted(set(tools) - set(TOOL_GROUPS))
    if unknown:
        raise RecipeError("Unknown benchmark component: " + ", ".join(unknown))
    variant = CUDA_VARIANTS[cuda]
    if "llm" in tools and not variant["vllm"]:
        raise RecipeError("CUDA 12.8.2 has no matching vLLM build in this recipe. Choose CUDA 13.0.2 for LLM "
                          "benchmarks, or leave the LLM component out.")
    # Deterministic order so identical selections render identical files.
    selected = [name for name in TOOL_GROUPS if name in set(tools)]
    supplied = request.get("mirrors") or {}
    if not isinstance(supplied, dict):
        raise RecipeError("mirrors must be an object of HTTP(S) mirror URLs")
    unknown_mirrors = sorted(set(supplied) - set(MIRROR_KEYS) - set(LEGACY_MIRROR_KEYS))
    if unknown_mirrors:
        raise RecipeError("Unknown mirror setting: " + ", ".join(unknown_mirrors))
    mirrors = {
        "apt_main": "http://archive.ubuntu.com/ubuntu/",
        "apt_security": "http://security.ubuntu.com/ubuntu/",
        "pypi_index": "https://pypi.org/simple",
        "torch_index": variant["torch_index"] or "https://pypi.org/simple",
        "python_mirror": "https://github.com/astral-sh/python-build-standalone/releases/download",
        "nccl_tests_repository": "https://github.com/NVIDIA/nccl-tests.git",
        "nvbandwidth_repository": "https://github.com/NVIDIA/nvbandwidth.git",
    }
    for key in MIRROR_KEYS:
        value = supplied.get(key)
        if value is not None:
            if not isinstance(value, str) or not MIRROR_URL_RE.fullmatch(value):
                raise RecipeError(f"{key} must be an HTTP(S) or file URL without spaces or credentials")
            mirrors[key] = value
    llm = "llm" in selected
    return {
        "cuda_version": cuda,
        "python_version": python,
        "python_full_version": PYTHON_VERSIONS[python],
        "tools": selected,
        "base": variant["base"],
        "cuda_packages": variant["cuda_packages"],
        "driver_minimum": variant["driver_minimum"],
        "torch": variant["torch"],
        "torchvision": variant["torchvision"],
        "torchaudio": variant["torchaudio"],
        "torch_index": mirrors["torch_index"],
        "separate_torch_index": variant["torch_index"] is not None,
        "vllm": variant["vllm"] if llm else None,
        "transformers": PINS["transformers"] if llm else None,
        "tokenizers": PINS["tokenizers"] if llm else None,
        "textual": PINS["textual"],
        "nvidia_ml_py": PINS["nvidia-ml-py"],
        "uv": PINS["uv"],
        "mirrors": mirrors,
        "nccl_tests": PINS["nccl-tests"],
        "nvbandwidth": PINS["nvbandwidth"],
        "gpu_architectures": list(GPU_ARCHITECTURES),
        "estimated_size_gb": estimate_size_gb(cuda, selected),
    }


def compatibility_matrix() -> list[dict]:
    return [{"cuda_version": version, "base": variant["base"], "driver_minimum": variant["driver_minimum"],
             "torch": variant["torch"], "vllm": variant["vllm"], "label": variant["label"],
             "gpu_architectures": [f"sm_{arch}" for arch in GPU_ARCHITECTURES],
             "hopper_h200": True, "blackwell_rtx_pro_6000": True}
            for version, variant in CUDA_VARIANTS.items()]
