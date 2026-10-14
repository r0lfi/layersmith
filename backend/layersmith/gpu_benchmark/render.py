"""Generated Containerfile lines, runtime files and getting-started content."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from layersmith.gpu_benchmark import catalog

RUNTIME_DIR = Path(__file__).with_name("runtime")
OPT = "/opt/gpu-bench"
VENV = "/opt/gpu-bench/venv"
APT_COMMON = ["ca-certificates", "curl", "git", "python3", "python3-pip", "make", "gcc", "g++",
              "cmake", "ninja-build", "iproute2"]
APT_NCCL = ["openmpi-bin", "libopenmpi-dev", "libibverbs-dev", "ibverbs-providers", "ibverbs-utils",
            "rdma-core", "perftest", "ethtool"]


def recipe_for(request: dict | None) -> dict:
    return catalog.resolve(request)


def system_packages(recipe: dict) -> list[str]:
    return sorted(set(APT_COMMON + (APT_NCCL if "nccl" in recipe["tools"] else [])))


def python_packages(recipe: dict) -> list[str]:
    packages = ["nvidia-ml-py==13.580.82"]
    packages += [f"torch=={recipe['torch']}", "torchvision==0.26.0", "torchaudio==2.11.0"]
    if "llm" in recipe["tools"]:
        packages += [f"vllm=={recipe['vllm']}", f"transformers=={recipe['transformers']}",
                     f"tokenizers=={recipe['tokenizers']}"]
    return packages


def apt_mirror_prelude(recipe: dict) -> list[str]:
    """Redirect Ubuntu main/security sources before the normal package install."""
    python = (
        "import os,re; from pathlib import Path; "
        "paths=[Path(\"/etc/apt/sources.list\"),*Path(\"/etc/apt/sources.list.d\").glob(\"*.sources\")]; "
        "rx=re.compile(r\"(?m)^(URIs:\\s*|deb\\s+)(?:https?://)?(archive|security)\\.ubuntu\\.com/ubuntu/?(?=\\s|$)\"); "
        "[(p.write_text(rx.sub(lambda m: m.group(1)+os.environ[\"GPU_BENCH_APT_MAIN\" if m.group(2)==\"archive\" else \"GPU_BENCH_APT_SECURITY\"],p.read_text())) if p.is_file() else None) for p in paths]"
    )
    return [f"ARG GPU_BENCH_APT_MAIN={recipe['mirrors']['apt_main']}",
            f"ARG GPU_BENCH_APT_SECURITY={recipe['mirrors']['apt_security']}",
            "RUN python3 -c '" + python + "'"]


def install_lines(recipe: dict) -> list[str]:
    """Install exact Python versions and compile source-pinned CUDA utilities."""
    lines = [
        "",
        "# ---- NVIDIA GPU benchmark toolkit ----",
        f"# CUDA {recipe['cuda_version']}; Python {recipe['python_full_version']}; selected: {', '.join(recipe['tools'])}.",
        "# The NVIDIA driver and kernel modules remain on the host; this image contains user-space CUDA only.",
        "ENV DEBIAN_FRONTEND=noninteractive \\",
        "    HF_HUB_OFFLINE=1 \\",
        "    TRANSFORMERS_OFFLINE=1 \\",
        "    HF_DATASETS_OFFLINE=1 \\",
        "    HF_HUB_DISABLE_TELEMETRY=1 \\",
        "    PIP_DISABLE_PIP_VERSION_CHECK=1 \\",
        "    PIP_NO_CACHE_DIR=1 \\",
        "    UV_PYTHON_INSTALL_DIR=/opt/uv/python \\",
        "    UV_CACHE_DIR=/opt/uv/cache \\",
        f"    GPU_BENCH_PYPI_INDEX={recipe['mirrors']['pypi_index']} \\",
        f"    GPU_BENCH_TORCH_INDEX={recipe['mirrors']['torch_index']} \\",
        f"    GPU_BENCH_PYTHON_MIRROR={recipe['mirrors']['python_mirror']} \\",
        f"    PATH={VENV}/bin:$PATH",
        "RUN python3 -m pip install --no-cache-dir uv==0.12.17 \\",
        f"    && uv python install --mirror \"$GPU_BENCH_PYTHON_MIRROR\" {recipe['python_full_version']} \\",
        f"    && uv venv --python {recipe['python_full_version']} {VENV} --seed",
        f"COPY gpu-benchmark/ {OPT}/",
        f"RUN chmod 0755 {OPT}/bin/gpu-bench",
    ]
    torch = [f"torch=={recipe['torch']}", "torchvision==0.26.0", "torchaudio==2.11.0"]
    lines += [f"RUN uv pip install --python {VENV}/bin/python --index-url \"$GPU_BENCH_TORCH_INDEX\" \\",
              "    " + " \\\n    ".join(torch)]
    extras = [name for name in python_packages(recipe) if not name.startswith(("torch==", "torchvision==", "torchaudio=="))]
    if extras:
        lines += [f"RUN uv pip install --python {VENV}/bin/python --index-url \"$GPU_BENCH_PYPI_INDEX\" \\",
                  "    " + " \\\n    ".join(extras)]
    lines += [f"RUN {VENV}/bin/python -m pip check"]
    if "memory" in recipe["tools"]:
        lines += [
            f"ARG CUDA_SAMPLES_REPOSITORY={recipe['mirrors']['cuda_samples_repository']}",
            f"RUN git clone --depth 1 --branch {recipe['cuda_samples']} \"$CUDA_SAMPLES_REPOSITORY\" /tmp/cuda-samples \\",
            "    && cmake -S /tmp/cuda-samples/Samples/1_Utilities/bandwidthTest \\",
            "        -B /tmp/cuda-samples-build -DCMAKE_BUILD_TYPE=Release '-DCMAKE_CUDA_ARCHITECTURES=90;100;120' \\",
            "    && cmake --build /tmp/cuda-samples-build --parallel \\",
            f"    && install -D -m 0755 /tmp/cuda-samples-build/bandwidthTest {OPT}/bin/bandwidthTest \\",
            "    && rm -rf /tmp/cuda-samples /tmp/cuda-samples-build",
        ]
    if "nccl" in recipe["tools"]:
        lines += [
            f"ARG NCCL_TESTS_REPOSITORY={recipe['mirrors']['nccl_tests_repository']}",
            f"RUN git clone --depth 1 --branch {recipe['nccl_tests']} \"$NCCL_TESTS_REPOSITORY\" /tmp/nccl-tests \\",
            f"    && NCCL_HOME=\"$({VENV}/bin/python -c 'import nvidia.nccl; print(list(nvidia.nccl.__path__)[0])')\" \\",
            "    && ln -sf \"$NCCL_HOME/lib/libnccl.so.2\" /usr/local/lib/libnccl.so.2 \\",
            "    && ln -sf \"$NCCL_HOME/lib/libnccl.so.2\" /usr/local/lib/libnccl.so \\",
            "    && ln -sf \"$NCCL_HOME/include/nccl.h\" /usr/local/include/nccl.h \\",
            "    && ldconfig \\",
            "    && make -C /tmp/nccl-tests -j2 MPI=1 MPI_HOME=/usr CUDA_HOME=/usr/local/cuda NCCL_HOME=\"$NCCL_HOME\" \\",
            "        NVCC_GENCODE=\"-gencode=arch=compute_75,code=sm_75 -gencode=arch=compute_80,code=sm_80 \\",
            "        -gencode=arch=compute_86,code=sm_86 -gencode=arch=compute_89,code=sm_89 \\",
            "        -gencode=arch=compute_90,code=sm_90 -gencode=arch=compute_100,code=sm_100 \\",
            "        -gencode=arch=compute_120,code=sm_120\" \\",
            f"    && install -d {OPT}/bin \\",
            f"    && for binary in all_reduce_perf all_gather_perf reduce_scatter_perf broadcast_perf; do install -m 0755 /tmp/nccl-tests/build/$binary {OPT}/bin/; done \\",
            "    && rm -rf /tmp/nccl-tests",
        ]
    lines += [
        f"ENV GPU_BENCH_TOOLS={','.join(recipe['tools'])}",
        f"RUN ln -s {OPT}/bin/gpu-bench /usr/local/bin/gpu-bench \\",
        f"    && mkdir -p /models /results /workspace /cache \\",
        "    && chmod 1777 /results /workspace /cache",
        "WORKDIR /workspace",
        'CMD ["gpu-bench", "--help"]',
    ]
    return lines


def labels(recipe: dict) -> dict:
    return {"io.layersmith.gpu-benchmark.cuda": recipe["cuda_version"],
            "io.layersmith.gpu-benchmark.python": recipe["python_full_version"],
            "io.layersmith.gpu-benchmark.tools": ",".join(recipe["tools"]),
            "io.layersmith.gpu-benchmark.vllm": recipe.get("vllm") or "not-installed",
            "io.layersmith.gpu-benchmark.nccl-tests": recipe["nccl_tests"]}


def getting_started(recipe: dict, image: str = "gpu-benchmark:1.0.0") -> dict:
    run_flags = ["--rm", "--device nvidia.com/gpu=all", "--security-opt=label=disable", "--ipc=host",
                 "--uts=host", "-v ./models:/models:ro,Z", "-v ./results:/results:Z"]
    podman = "podman run " + " \\\n  ".join(run_flags + [image, "gpu-bench full --model /models/llama-8b --output /results"])
    docker = ("docker run --rm --gpus all --ipc=host --uts=host -v ./models:/models:ro,Z -v ./results:/results:Z "
              + image + " gpu-bench full --model /models/llama-8b --output /results")
    return {
        "title": "NVIDIA GPU Benchmark Toolkit",
        "summary": "Local-model GPU inference, CUDA compute, memory transfer and multi-GPU/NCCL tests.",
        "image": image,
        "cuda": recipe["cuda_version"], "python": recipe["python_full_version"], "tools": recipe["tools"],
        "podman": podman, "docker": docker,
        "commands": [
            "gpu-bench info",
            "gpu-bench health",
            "gpu-bench compute --precision bf16 --duration 60",
            "gpu-bench memory",
            "gpu-bench nccl --test all_reduce",
            "gpu-bench llm --model /models/llama-8b --input-tokens 1024 --output-tokens 256 --concurrency 1,8,16,32 --requests 100",
            "gpu-bench full --model /models/llama-8b --output /results",
            "gpu-bench report --input /results/benchmark.json --compare /results/previous/benchmark.json",
        ],
        "notes": [
            "The host supplies the NVIDIA kernel driver and GPU device access; the image installs no driver.",
            "Local model files are required. Hugging Face is in offline mode and no model or package is downloaded at runtime.",
            "For multi-node MPI or Slurm, share the image on every node, expose GPUs and RDMA devices, and configure routable interfaces and NCCL variables on the host.",
        ],
    }


def markdown(recipe: dict, image: str = "gpu-benchmark:1.0.0") -> str:
    content = getting_started(recipe, image)
    return ("# NVIDIA GPU Benchmark Toolkit\n\n" + content["summary"] + "\n\n"
            + "## Versions and components\n\n"
            + f"- CUDA Toolkit: `{recipe['cuda_version']}`\n- Python: `{recipe['python_full_version']}`\n"
            + f"- PyTorch: `{recipe['torch'] if recipe['torch'] else 'not selected'}`\n"
            + f"- vLLM: `{recipe['vllm'] or 'not selected (CUDA 12.8 recipe)'}`\n"
            + f"- Tool groups: `{', '.join(recipe['tools'])}`\n\n"
            + "## Run with Podman\n\n```sh\n" + content["podman"] + "\n```\n\n"
            + "## Run with Docker\n\n```sh\n" + content["docker"] + "\n```\n\n"
            + "## Commands\n\n```sh\n" + "\n".join(content["commands"]) + "\n```\n\n"
            + "## Runtime requirements\n\n" + "\n".join(f"- {note}" for note in content["notes"]) + "\n\n"
            + "Set `NCCL_DEBUG`, `NCCL_SOCKET_IFNAME`, and `NCCL_IB_HCA` at runtime for your fabric. "
            + "Use `--network=host` for MPI/RDMA only when your cluster policy requires it; Docker/Podman "
            + "GPU flags alone do not configure RDMA, IP routing, host SSH, or Slurm.\n")


def write_context(context: Path, recipe: dict, image: str = "gpu-benchmark:1.0.0") -> list[str]:
    root = context / "gpu-benchmark"
    if root.exists():
        shutil.rmtree(root)
    shutil.copytree(RUNTIME_DIR, root, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (root / "README.md").write_text(markdown(recipe, image), encoding="utf-8")
    (root / "recipe.json").write_text(json.dumps({key: recipe.get(key) for key in
                                                  ("cuda_version", "python_full_version", "tools", "torch",
                                                   "vllm", "transformers", "tokenizers", "nccl_tests")},
                                                 indent=2) + "\n", encoding="utf-8")
    return sorted(str(path.relative_to(context)) for path in root.rglob("*") if path.is_file())
