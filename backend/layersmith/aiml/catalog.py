"""AI / ML / GPU templates: verified upstream images and what is known about them.

Every image reference here was checked against its registry, and every
version number against the image's own environment (CUDA_VERSION,
PYTORCH_VERSION, ...) and its publisher's release notes; `verified` says when.
A tag is never invented: anything not listed is offered only through the
Custom AI Container, and is then marked as unverified.

Compatibility facts are kept with their source, because "the GPU was
detected" proves nothing about whether the kernels in an image can run on it:

  CUDA 13.4 -> driver branch R615, CUDA 13.3 -> R610, CUDA 13.0 -> R580
  (CUDA Toolkit 13.4 release notes, table 2). An older 13.x driver (>= 580)
  runs 13.x images only through minor version compatibility (table 3).
  CUDA 12.x images need >= 525 for minor version compatibility; newer drivers
  are backward compatible with them.

The driver is never part of an image: it comes from the GPU host, through
the NVIDIA Container Toolkit's CDI devices.
"""

import re

VERIFIED = "2026-10-27"

SOURCES = {
    "ngc-pytorch": "https://docs.nvidia.com/deeplearning/frameworks/pytorch-release-notes/rel-26-09.html",
    "ngc-matrix": "https://docs.nvidia.com/deeplearning/frameworks/support-matrix/index.html",
    "cuda-notes": "https://docs.nvidia.com/cuda/cuda-toolkit-release-notes/index.html",
    "cuda-compat": "https://docs.nvidia.com/deploy/cuda-compatibility/",
    "cuda-images": "https://hub.docker.com/r/nvidia/cuda",
    "vllm-docker": "https://docs.vllm.ai/en/latest/deployment/docker/",
    "tf-docker": "https://www.tensorflow.org/install/docker",
}

#: Native driver branch per CUDA minor version, and the floor for minor
#: version compatibility per major version (CUDA release notes, tables 2-4).
CUDA_DRIVER_BRANCH = {"13.4": 615, "13.3": 610, "13.0": 580}
#: Linux x86_64 minimums for the CUDA 12 versions templates use (release notes, table 4).
CUDA_12_DRIVER = {"12.9": "575.57.08"}
MINOR_COMPAT_FLOOR = {"13": "580", "12": "525"}

#: GPUs of the target environment. Compute capability is what decides whether
#: an image's compiled kernels (sm_XX) can run.
GPUS = {
    "h200-nvl": {"name": "NVIDIA H200 NVL", "architecture": "Hopper", "capability": "9.0", "memory_gb": 141},
    "rtx-pro-6000-bse": {"name": "NVIDIA RTX PRO 6000 Blackwell Server Edition", "architecture": "Blackwell",
                         "capability": "12.0", "memory_gb": 96},
}

TARGET = {
    "os": "AlmaLinux 10",
    "runtime": "Podman with the NVIDIA Container Toolkit (CDI)",
    "driver": "615.71.09",
    "gpus": list(GPUS),
    "scheduler": "Slurm (GRES, cgroup device constraints)",
    "network": "Air-gapped: nothing is downloaded when a container starts",
}

CATEGORY = {
    "id": "ai-ml",
    "name": "AI / ML / GPU",
    "intro": "GPU-optimised OCI images for AI, machine learning, CUDA and HPC: import an upstream image as it "
             "is, or add tools on top, then export it for an offline GPU cluster with Podman and Slurm.",
}

# ------------------------------------------------------------- templates

_PT_SOURCES = [SOURCES["ngc-pytorch"], SOURCES["ngc-matrix"]]

TEMPLATES = {
    "nvidia-pytorch": {
        "name": "NVIDIA PyTorch",
        "template_version": 1,
        "purpose": "AI, ML, deep learning",
        "description": "NVIDIA's NGC PyTorch container: PyTorch built against the CUDA, cuDNN, NCCL and "
                       "TensorRT versions it ships, with JupyterLab and FlashAttention.",
        "framework": "pytorch",
        "python": True,
        "family": "deb",
        # nvidia_entrypoint.sh runs the command it is given, so commands go after the image.
        "entrypoint_wraps": True,
        # NVIDIA built this image with numpy<=2.1 (its build-time constraint file); keep it there.
        "protect": ["numpy"],
        "default_version": "26.09-py3",
        "status": "supported",
        "versions": {
            "26.09-py3": {
                "image": "nvcr.io/nvidia/pytorch", "tag": "26.09-py3",
                "digest": "sha256:6e8ccc607fc2a51e3741667b86316a0889418ba8b78ed0ba96e8d282b648a7a6",
                "cuda": "13.4.1", "framework_version": "2.14.0a0+b2c75dd", "python": "3.12", "os": "Ubuntu 24.04",
                "components": {"cuDNN": "9.26.0.51", "NCCL": "2.31.2", "TensorRT": "11.3.0.99",
                               "Transformer Engine": "2.19", "JupyterLab": "4.6.3",
                               "FlashAttention": "2.8.3, and 4.0.0b24 (beta)", "Triton": "3.8.0"},
                "compressed_gb": 11.7, "min_capability": "7.5", "packages": "pt2609",
                "sources": _PT_SOURCES,
            },
            "26.08-py3": {
                "image": "nvcr.io/nvidia/pytorch", "tag": "26.08-py3",
                "digest": "sha256:3becd068f49bd2ad38f90db5f9a4803019a76933a24e63d821376c44e7a9200a",
                "cuda": "13.4.1", "framework_version": "2.14.0a0+4fdf77b", "python": "3.12", "os": "Ubuntu 24.04",
                "components": {"cuDNN": "9.25.0.28", "NCCL": "2.30.7", "TensorRT": "11.2.1.2"},
                "compressed_gb": 11.3, "min_capability": "7.5", "packages": "pt2608",
                "sources": _PT_SOURCES,
            },
            "26.07-py3": {
                "image": "nvcr.io/nvidia/pytorch", "tag": "26.07-py3",
                "digest": "sha256:2140e699b3beaf7f96a0081fd9c9406bc3832b435cdb60dfa2d261f7d2f34a1c",
                "cuda": "13.3.1", "framework_version": "2.13.0a0+9186a08", "python": "3.12", "os": "Ubuntu 24.04",
                "components": {"cuDNN": "9.24.0.43", "NCCL": "2.30.7", "TensorRT": "11.1.0.106"},
                "compressed_gb": 10.6, "min_capability": "7.5", "packages": "pt2607",
                "sources": _PT_SOURCES,
            },
        },
        "upstream": {"registry": "nvcr.io", "repository": "nvidia/pytorch", "pattern": r"^\d{2}\.\d{2}-py3$"},
    },
    "nvidia-cuda": {
        "name": "NVIDIA CUDA",
        "template_version": 1,
        "purpose": "CUDA development and GPU tests",
        "description": "The official CUDA images: the toolkit (devel), runtime libraries only (runtime) or the "
                       "bare minimum (base). No Python, so no Python tools are added.",
        "framework": "cuda",
        "python": False,
        "family": "deb",
        "entrypoint_wraps": True,
        "default_version": "13.4.1-devel-ubuntu24.04",
        "status": "supported",
        "versions": {
            "13.4.1-devel-ubuntu24.04": {
                "image": "docker.io/nvidia/cuda", "tag": "13.4.1-devel-ubuntu24.04",
                "digest": "sha256:a3f014424ccc86cf66c84d700d2896491424205dd9457e3116ed4f0e96381311",
                "cuda": "13.4.1", "framework_version": "13.4.1", "python": None, "os": "Ubuntu 24.04",
                "components": {"NCCL": "2.31.2", "nvcc": "13.4"}, "compressed_gb": 4.5, "min_capability": "7.5",
                "sources": [SOURCES["cuda-images"], SOURCES["cuda-notes"]],
            },
            "13.4.1-devel-rockylinux10": {
                "image": "docker.io/nvidia/cuda", "tag": "13.4.1-devel-rockylinux10",
                "digest": "sha256:cb81c6606a4cf1932c9d6621aec4c4bf788e5d976bedaa0b60abb8783eb4607f",
                "cuda": "13.4.1", "framework_version": "13.4.1", "python": None, "os": "Rocky Linux 10 (EL10)",
                "components": {"NCCL": "2.31.2", "nvcc": "13.4"}, "compressed_gb": 4.3, "min_capability": "7.5",
                "family": "rpm",
                "sources": [SOURCES["cuda-images"], SOURCES["cuda-notes"]],
            },
            "13.4.1-runtime-ubuntu24.04": {
                "image": "docker.io/nvidia/cuda", "tag": "13.4.1-runtime-ubuntu24.04",
                "digest": "sha256:cdfca8f2adfd99287ebbd4a86b3ad6470425c731aea023ca1d4c653a0d7fec7b",
                "cuda": "13.4.1", "framework_version": "13.4.1", "python": None, "os": "Ubuntu 24.04",
                "components": {"NCCL": "2.31.2"}, "compressed_gb": 1.9, "min_capability": "7.5",
                "sources": [SOURCES["cuda-images"], SOURCES["cuda-notes"]],
            },
            "13.4.1-base-ubuntu24.04": {
                "image": "docker.io/nvidia/cuda", "tag": "13.4.1-base-ubuntu24.04",
                "digest": "sha256:3eb2d952d9cff2d7a4e42a1cedc19cfa610a7f72c3b8428811731dcea5a175fd",
                "cuda": "13.4.1", "framework_version": "13.4.1", "python": None, "os": "Ubuntu 24.04",
                "components": {}, "compressed_gb": 0.2, "min_capability": "7.5",
                "sources": [SOURCES["cuda-images"], SOURCES["cuda-notes"]],
            },
            "13.0.2-devel-ubuntu24.04": {
                "image": "docker.io/nvidia/cuda", "tag": "13.0.2-devel-ubuntu24.04",
                "digest": "sha256:5dc1bca23d05bd37b011be68ec470c03b403a5da07ec3a86e41af9470e9d0cc6",
                "cuda": "13.0.2", "framework_version": "13.0.2", "python": None, "os": "Ubuntu 24.04",
                "components": {"NCCL": "2.28.3", "nvcc": "13.0"}, "compressed_gb": 4.0, "min_capability": "7.5",
                "sources": [SOURCES["cuda-images"], SOURCES["cuda-notes"]],
            },
        },
        "upstream": {"registry": "docker.io", "repository": "nvidia/cuda",
                     "pattern": r"^13\.\d+\.\d+-(base|runtime|devel|cudnn-runtime|cudnn-devel)-(ubuntu24\.04|rockylinux10)$"},
    },
    "vllm": {
        "name": "vLLM",
        "template_version": 1,
        "purpose": "LLM inference and an OpenAI-compatible API",
        "description": "The vLLM project's own image. Its entrypoint is `vllm serve`; models are mounted at run "
                       "time, never built in.",
        "framework": "vllm",
        "python": True,
        "family": "deb",
        "entrypoint_wraps": False,
        "default_version": "v0.32.0",
        "status": "supported",
        "versions": {
            "v0.32.0": {
                "image": "docker.io/vllm/vllm-openai", "tag": "v0.32.0",
                "digest": "sha256:befccf0e7e4089aa23926bb6f887a85c0d46c2a99e6de5c5ffdf5cb7957c5b34",
                "cuda": "13.0.2", "framework_version": "0.32.0", "torch": "2.13.0+cu130", "python": "3.12", "os": None,
                "components": {}, "compressed_gb": 9.2, "min_capability": "7.5", "packages": "vllm0320",
                "sources": [SOURCES["vllm-docker"]],
            },
            "v0.32.0-cu129": {
                "image": "docker.io/vllm/vllm-openai", "tag": "v0.32.0-cu129",
                "digest": "sha256:bc0445dac61accbf1c3b17fbb524a71eee0b1b32f55ba45952095e3be8991c3f",
                "cuda": "12.9.1", "framework_version": "0.32.0", "python": "3.12", "os": None,
                "components": {}, "compressed_gb": 11.2, "min_capability": "7.5", "packages": "vllm0320cu129",
                "sources": [SOURCES["vllm-docker"]],
            },
            "v0.31.0": {
                "image": "docker.io/vllm/vllm-openai", "tag": "v0.31.0",
                "digest": "sha256:c1c9f6fd5c109ba7f0546a59f5b2f15fb87f64c77782e90a27b648b42a8e67c3",
                "cuda": "13.0.2", "framework_version": "0.31.0", "python": "3.12", "os": None,
                "components": {}, "compressed_gb": 9.0, "min_capability": "7.5", "packages": "vllm0310",
                "sources": [SOURCES["vllm-docker"]],
            },
        },
        "upstream": {"registry": "docker.io", "repository": "vllm/vllm-openai", "pattern": r"^v\d+\.\d+\.\d+(-cu\d+)?$"},
    },
    "tensorflow": {
        "name": "TensorFlow GPU",
        "template_version": 1,
        "purpose": "Machine learning",
        "description": "TensorFlow's official GPU image. NVIDIA stopped publishing NGC TensorFlow containers "
                       "after 25.02, so this is the maintained one. Its kernels are compiled for sm_60-sm_89 with "
                       "compute_90 PTX: Hopper and Blackwell run it only through PTX JIT compilation, which "
                       "TensorFlow does not document. Validate it with the GPU test on the worker.",
        "framework": "tensorflow",
        "python": True,
        "family": "deb",
        "entrypoint_wraps": True,
        "default_version": "2.21.0-gpu",
        "status": "limited",
        "versions": {
            "2.21.0-gpu": {
                "image": "docker.io/tensorflow/tensorflow", "tag": "2.21.0-gpu",
                "digest": "sha256:61fe1ce25bd26b0a38e310463a5588d4067d2d01b6bdb058a3ca4f5cf2e18f15",
                "cuda": "12.3.0", "framework_version": "2.21.0", "python": "3.11", "os": None,
                "components": {}, "compressed_gb": 3.8, "min_capability": "6.0", "packages": "tf2210",
                # Read from the image (tf.sysconfig): sm_60 sm_70 sm_80 sm_89 + compute_90 PTX.
                "max_native_capability": "8.9",
                "sources": [SOURCES["tf-docker"]],
            },
            "2.21.0-gpu-jupyter": {
                "image": "docker.io/tensorflow/tensorflow", "tag": "2.21.0-gpu-jupyter",
                "digest": "sha256:742a91e780b2d1d51a3352d1daf71415a73cc50594a3d7c0110d5f9949b7435b",
                "cuda": "12.3.0", "framework_version": "2.21.0", "python": "3.11", "os": None,
                "components": {"Jupyter Notebook": "included"}, "compressed_gb": 4.0, "min_capability": "6.0",
                "packages": "tf2210j", "max_native_capability": "8.9",
                "sources": [SOURCES["tf-docker"]],
            },
        },
        "upstream": {"registry": "docker.io", "repository": "tensorflow/tensorflow",
                     "pattern": r"^\d+\.\d+\.\d+-gpu(-jupyter)?$"},
    },
    "custom": {
        "name": "Custom AI Container",
        "template_version": 1,
        "purpose": "Your own development environment",
        "description": "Any base image you name. LayerSmith pins it by digest when it pulls it, but cannot "
                       "vouch for its contents: CUDA and framework versions are read from the image after the "
                       "build.",
        "framework": "custom",
        "python": True,
        "family": "deb",
        "entrypoint_wraps": False,
        "default_version": None,
        "status": "supported",
        "versions": {},
    },
}

TEMPLATE_NAMES = {key: entry["name"] for key, entry in TEMPLATES.items()}

#: Registries whose images are published by the project or vendor itself.
TRUSTED_REGISTRIES = ("nvcr.io/nvidia/", "docker.io/nvidia/", "docker.io/vllm/", "docker.io/tensorflow/",
                      "docker.io/library/", "docker.io/pytorch/")

# ------------------------------------------------------------ AI tools

#: Optional Python tools. `pip` is the distribution name; nothing is pinned
#: here - pip resolves against the base image's own versions, which are
#: protected (see render.protected), and the resolved set is recorded with
#: the build, so the exported image documents exactly what it holds.
TOOLS = [
    {"id": "transformers", "pip": "transformers", "label": "Hugging Face Transformers", "group": "Hugging Face"},
    {"id": "datasets", "pip": "datasets", "label": "Hugging Face Datasets", "group": "Hugging Face"},
    {"id": "accelerate", "pip": "accelerate", "label": "Accelerate", "group": "Hugging Face"},
    {"id": "peft", "pip": "peft", "label": "PEFT (LoRA, QLoRA)", "group": "Hugging Face"},
    {"id": "sentence-transformers", "pip": "sentence-transformers", "label": "Sentence Transformers",
     "group": "Hugging Face", "frameworks": ["pytorch", "vllm", "custom"]},
    {"id": "scikit-learn", "pip": "scikit-learn", "label": "scikit-learn", "group": "Data science"},
    {"id": "pandas", "pip": "pandas", "label": "pandas", "group": "Data science"},
    {"id": "numpy", "pip": "numpy", "label": "NumPy", "group": "Data science"},
    {"id": "scipy", "pip": "scipy", "label": "SciPy", "group": "Data science"},
    {"id": "matplotlib", "pip": "matplotlib", "label": "Matplotlib", "group": "Data science"},
    {"id": "jupyterlab", "pip": "jupyterlab", "label": "JupyterLab", "group": "Notebooks"},
    {"id": "mlflow", "pip": "mlflow", "label": "MLflow", "group": "Experiments",
     "note": "Tracking server and UI; point it at a local file store, there is no hosted backend offline."},
    {"id": "optuna", "pip": "optuna", "label": "Optuna", "group": "Experiments"},
    {"id": "bitsandbytes", "pip": "bitsandbytes", "label": "bitsandbytes", "group": "Quantisation",
     "frameworks": ["pytorch", "vllm", "custom"],
     "note": "Ships prebuilt CUDA kernels: whether its wheel covers this image's CUDA version is only "
             "proven by the GPU test on a worker."},
    {"id": "flash-attn", "pip": "flash-attn", "label": "FlashAttention", "group": "Kernels",
     "frameworks": ["pytorch"], "needs_compiler": True,
     "note": "Compiled against the exact PyTorch build. Already part of the NVIDIA PyTorch image; "
             "it is never reinstalled over NVIDIA's build."},
]
TOOLS_BY_ID = {tool["id"]: tool for tool in TOOLS}

#: Distributions the base image provides and pip must never replace: they are
#: built together (PyTorch against its CUDA, cuDNN and NCCL, TensorRT and
#: FlashAttention against that PyTorch), and a PyPI wheel of the same name is a
#: different build. Requesting one of them directly is refused.
PROTECTED_NAMES = {
    "torch", "torchvision", "torchaudio", "triton", "pytorch-triton", "torch-tensorrt", "tensorrt",
    "transformer-engine", "flash-attn", "flash-attn-4", "apex",
    "tensorflow", "tensorflow-gpu", "tf-keras", "keras", "jax", "jaxlib", "vllm", "xformers", "flashinfer-python",
    "cupy", "numba-cuda", "cuda-python", "nccl",
}
PROTECTED_PREFIXES = ("nvidia-", "cuda-", "tensorrt-", "transformer-engine-", "torch-", "cupy-", "flashinfer")

# ------------------------------------------------------- validation

PIP_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$")
PIP_EXTRAS_RE = re.compile(r"^\[[A-Za-z0-9._-]+(?:,[A-Za-z0-9._-]+){0,9}\]$")
PIP_SPEC_RE = re.compile(r"^(?:===|==|!=|~=|>=|<=|<|>)[A-Za-z0-9.*+!_-]{1,64}$")
INDEX_URL_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(?::\d{1,5})?(?:/[A-Za-z0-9._~%/+-]*)?$")
MAX_PIP = 60


class AimlError(ValueError):
    """A user-facing error in an AI/ML image selection."""


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def is_protected(name: str, template_id: str | None = None) -> bool:
    name = normalise(name)
    extra = TEMPLATES.get(template_id or "", {}).get("protect", [])
    return name in PROTECTED_NAMES or name in extra or name.startswith(PROTECTED_PREFIXES)


def parse_requirement(value: str) -> dict:
    """One `name[extras]<spec>,<spec>` requirement, or AimlError.

    Deliberately a small subset of PEP 508: no URLs, paths, VCS references,
    environment markers or pip options, so nothing a user types can make pip
    fetch from somewhere else or change how it runs.
    """
    text = str(value or "").strip()
    if not text:
        raise AimlError("Empty package requirement")
    if len(text) > 200 or any(c.isspace() for c in text):
        raise AimlError(f"Invalid package requirement {text!r}: use name, name==1.2.3 or name>=1.2,<2")
    if text.startswith("-") or any(token in text for token in ("://", "@", ";", "/", "\\", "#", "$", "`")):
        raise AimlError(f"{text!r} is not allowed: only package names with optional extras and version "
                        "specifiers, no URLs, paths, markers or pip options")
    match = re.match(r"^([A-Za-z0-9._-]+)(\[[^\]]*\])?(.*)$", text)
    name, extras, rest = match.group(1), match.group(2) or "", match.group(3)
    if not PIP_NAME_RE.match(name):
        raise AimlError(f"Invalid package name {name!r}")
    if extras and not PIP_EXTRAS_RE.match(extras):
        raise AimlError(f"Invalid extras in {text!r}")
    specs = [s for s in rest.split(",")] if rest else []
    for spec in specs:
        if not PIP_SPEC_RE.match(spec):
            raise AimlError(f"Invalid version specifier {spec!r} in {text!r}")
    return {"name": normalise(name), "requirement": text, "pinned": any(s.startswith("==") for s in specs)}


def versions_of(template_id: str) -> dict:
    return TEMPLATES[template_id]["versions"]


def driver_tuple(value: str) -> tuple[int, ...]:
    if not re.match(r"^\d{3}(?:\.\d{1,3}){0,2}$", str(value or "")):
        raise AimlError(f"Invalid NVIDIA driver version {value!r}: expected for example 615.71.09")
    return tuple(int(part) for part in str(value).split("."))


def capability_tuple(value: str) -> tuple[int, int]:
    major, _, minor = str(value).partition(".")
    return int(major), int(minor or 0)
