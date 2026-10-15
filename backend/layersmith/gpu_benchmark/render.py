"""Containerfile lines, build-context files and getting-started text for gpu-bench images.

The image is built once with network access and then runs fully offline: every
binary and Python package is installed at build time, nothing is fetched at
start-up, and Hugging Face/vLLM are forced into offline mode.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from layersmith.gpu_benchmark import catalog

RUNTIME_DIR = Path(__file__).with_name("runtime")
OPT = "/opt/gpu-bench"
VENV = f"{OPT}/venv"
PACKAGE = "gpu_bench"   # the runtime package's name inside the image

APT_COMMON = ["ca-certificates", "curl", "git", "python3", "python3-pip", "make", "gcc", "g++", "cmake",
              "iproute2", "pciutils", "procps", "less", "tini", "numactl"]
APT_NCCL = ["openmpi-bin", "libopenmpi-dev", "openssh-client", "rdma-core", "ibverbs-utils", "ibverbs-providers",
            "infiniband-diags", "perftest", "ethtool"]


def recipe_for(request: dict | None) -> dict:
    return catalog.resolve(request)


def system_packages(recipe: dict) -> list[str]:
    cuda = recipe["cuda_packages"]
    # nvcc and the CUDA headers stay in the image: nccl-tests and nvbandwidth are
    # compiled here, and Triton/FlashInfer compile kernels at runtime, offline.
    toolkit = [f"cuda-nvcc-{cuda}", f"cuda-cudart-dev-{cuda}", f"cuda-cccl-{cuda}", f"cuda-nvml-dev-{cuda}"]
    return sorted(set(APT_COMMON + toolkit + (APT_NCCL if "nccl" in recipe["tools"] else [])))


def python_packages(recipe: dict) -> list[str]:
    packages = [f"torch=={recipe['torch']}", f"torchvision=={recipe['torchvision']}",
                f"torchaudio=={recipe['torchaudio']}", f"nvidia-ml-py=={recipe['nvidia_ml_py']}",
                f"textual=={recipe['textual']}"]
    if "llm" in recipe["tools"]:
        packages += [f"vllm=={recipe['vllm']}", f"transformers=={recipe['transformers']}",
                     f"tokenizers=={recipe['tokenizers']}"]
    return packages


def apt_mirror_prelude(recipe: dict) -> list[str]:
    """Point Ubuntu's sources at the configured mirrors (sed only: no Python in the base yet)."""
    return [f"ARG GPU_BENCH_APT_MAIN={recipe['mirrors']['apt_main']}",
            f"ARG GPU_BENCH_APT_SECURITY={recipe['mirrors']['apt_security']}",
            "RUN for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list; do \\",
            "      [ -f \"$f\" ] || continue; \\",
            "      sed -i -E \"s|https?://archive\\.ubuntu\\.com/ubuntu/?|${GPU_BENCH_APT_MAIN}|g; "
            "s|https?://security\\.ubuntu\\.com/ubuntu/?|${GPU_BENCH_APT_SECURITY}|g\" \"$f\"; \\",
            "    done"]


def _gencode(recipe: dict) -> str:
    archs = recipe["gpu_architectures"]
    codes = [f"-gencode=arch=compute_{a},code=sm_{a}" for a in archs]
    codes.append(f"-gencode=arch=compute_{archs[-1]},code=compute_{archs[-1]}")  # PTX for newer GPUs
    return " ".join(codes)


def install_lines(recipe: dict) -> list[str]:
    tools = recipe["tools"]
    mirrors = recipe["mirrors"]
    py = f"{VENV}/bin/python"
    lines = [
        "",
        "# ---- NVIDIA GPU & LLM Benchmark (gpu-bench) ----",
        f"# CUDA {recipe['cuda_version']} user space, Python {recipe['python_full_version']}, components: {', '.join(tools)}.",
        "# The NVIDIA driver stays on the host (Podman + NVIDIA CDI). Nothing below runs at container start.",
        "ENV DEBIAN_FRONTEND=noninteractive \\",
        "    PIP_DISABLE_PIP_VERSION_CHECK=1 \\",
        "    PIP_NO_CACHE_DIR=1 \\",
        "    UV_NO_CACHE=1 \\",
        "    UV_PYTHON_INSTALL_DIR=/opt/uv/python",
        f"ARG GPU_BENCH_PYPI_INDEX={mirrors['pypi_index']}",
        f"ARG GPU_BENCH_TORCH_INDEX={mirrors['torch_index']}",
        f"ARG GPU_BENCH_PYTHON_MIRROR={mirrors['python_mirror']}",
        f"RUN python3 -m pip install --no-cache-dir --break-system-packages --index-url \"$GPU_BENCH_PYPI_INDEX\" uv=={recipe['uv']} \\",
        f"    && uv python install --mirror \"$GPU_BENCH_PYTHON_MIRROR\" {recipe['python_full_version']} \\",
        f"    && uv venv --python {recipe['python_full_version']} --seed {VENV}",
    ]
    packages = python_packages(recipe)
    if recipe["separate_torch_index"]:
        torch = [p for p in packages if p.split("==")[0] in ("torch", "torchvision", "torchaudio")]
        rest = [p for p in packages if p not in torch]
        lines += [f"RUN uv pip install --python {py} --index-url \"$GPU_BENCH_TORCH_INDEX\" \\",
                  "      " + " ".join(torch) + " \\",
                  f"    && uv pip install --python {py} --index-url \"$GPU_BENCH_PYPI_INDEX\" \\",
                  "      " + " ".join(rest)]
    else:
        lines += [f"RUN uv pip install --python {py} --index-url \"$GPU_BENCH_PYPI_INDEX\" \\",
                  "      " + " \\\n      ".join(packages)]
    lines += [f"RUN {py} -m pip check \\",
              f"    && uv pip freeze --python {py} > {OPT}-requirements.lock"]
    # NCCL from the PyTorch wheels, so nccl-tests use exactly the library PyTorch uses.
    lines += [
        f"RUN NCCL_LIB=\"$(find {VENV}/lib -name 'libnccl.so.2' | head -n 1)\" \\",
        f"    && NCCL_INC=\"$(find {VENV}/lib -name 'nccl.h' | head -n 1)\" \\",
        "    && test -n \"$NCCL_LIB\" -a -n \"$NCCL_INC\" \\",
        "    && mkdir -p /opt/nccl/lib \\",
        "    && ln -sf \"$NCCL_LIB\" /opt/nccl/lib/libnccl.so.2 && ln -sf libnccl.so.2 /opt/nccl/lib/libnccl.so \\",
        "    && ln -sfn \"$(dirname \"$NCCL_INC\")\" /opt/nccl/include \\",
        "    && echo /opt/nccl/lib > /etc/ld.so.conf.d/gpu-bench-nccl.conf && ldconfig",
    ]
    if "nccl" in tools:
        gencode = _gencode(recipe)
        lines += [
            f"ARG NCCL_TESTS_REPOSITORY={mirrors['nccl_tests_repository']}",
            f"RUN git clone --depth 1 --branch {recipe['nccl_tests']} \"$NCCL_TESTS_REPOSITORY\" /tmp/nccl-tests \\",
            f"    && install -d {OPT}/bin/mpi \\",
            "    && make -C /tmp/nccl-tests -j2 BUILDDIR=/tmp/nccl-tests/single MPI=0 \\",
            "         CUDA_HOME=/usr/local/cuda NCCL_HOME=/opt/nccl \\",
            f"         NVCC_GENCODE=\"{gencode}\" \\",
            "    && make -C /tmp/nccl-tests -j2 BUILDDIR=/tmp/nccl-tests/mpi MPI=1 MPI_HOME=/usr/lib/x86_64-linux-gnu/openmpi \\",
            "         CUDA_HOME=/usr/local/cuda NCCL_HOME=/opt/nccl \\",
            f"         NVCC_GENCODE=\"{gencode}\" \\",
            "    && for t in all_reduce all_gather reduce_scatter broadcast sendrecv alltoall; do \\",
            f"         install -m 0755 /tmp/nccl-tests/single/${{t}}_perf {OPT}/bin/ \\",
            f"         && install -m 0755 /tmp/nccl-tests/mpi/${{t}}_perf {OPT}/bin/mpi/; done \\",
            "    && rm -rf /tmp/nccl-tests",
        ]
    if "memory" in tools:
        archs = ";".join(str(a) for a in recipe["gpu_architectures"])
        lines += [
            f"ARG NVBANDWIDTH_REPOSITORY={mirrors['nvbandwidth_repository']}",
            f"RUN git clone --depth 1 --branch {recipe['nvbandwidth']} \"$NVBANDWIDTH_REPOSITORY\" /tmp/nvbandwidth \\",
            "    && LIBRARY_PATH=/usr/local/cuda/lib64/stubs cmake -S /tmp/nvbandwidth -B /tmp/nvbandwidth/build \\",
            f"         -DCMAKE_BUILD_TYPE=Release '-DCMAKE_CUDA_ARCHITECTURES={archs}' \\",
            "         -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc \\",
            "    && LIBRARY_PATH=/usr/local/cuda/lib64/stubs cmake --build /tmp/nvbandwidth/build -j2 \\",
            f"    && install -D -m 0755 /tmp/nvbandwidth/build/nvbandwidth {OPT}/bin/nvbandwidth \\",
            "    && rm -rf /tmp/nvbandwidth",
        ]
    lines += [
        f"COPY gpu-benchmark/ {OPT}/",
        f"RUN chmod 0755 {OPT}/bin/gpu-bench {OPT}/scripts/*.sh \\",
        f"    && ln -sf {OPT}/bin/gpu-bench /usr/local/bin/gpu-bench \\",
        f"    && mv {OPT}-requirements.lock {OPT}/requirements.lock \\",
        "    && mkdir -p /models /results /workspace /cache \\",
        "    && chmod 1777 /results /workspace /cache \\",
        f"    && {py} -m compileall -q {OPT}/{PACKAGE}",
        "ENV PATH=" + VENV + "/bin:$PATH \\",
        f"    GPU_BENCH_HOME={OPT} \\",
        "    GPU_BENCH_RESULTS=/results \\",
        "    GPU_BENCH_MODELS=/models \\",
        f"    GPU_BENCH_TOOLS={','.join(tools)} \\",
        "    CUDA_DEVICE_ORDER=PCI_BUS_ID \\",
        "    HF_HUB_OFFLINE=1 \\",
        "    TRANSFORMERS_OFFLINE=1 \\",
        "    HF_DATASETS_OFFLINE=1 \\",
        "    HF_HUB_DISABLE_TELEMETRY=1 \\",
        "    VLLM_NO_USAGE_STATS=1 \\",
        "    DO_NOT_TRACK=1 \\",
        "    XDG_CACHE_HOME=/cache \\",
        "    HF_HOME=/cache/huggingface \\",
        "    TRITON_CACHE_DIR=/cache/triton \\",
        "    TORCHINDUCTOR_CACHE_DIR=/cache/inductor \\",
        "    VLLM_CACHE_ROOT=/cache/vllm \\",
        "    OMPI_ALLOW_RUN_AS_ROOT=1 \\",
        "    OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1 \\",
        "    TERM=xterm-256color \\",
        "    COLORTERM=truecolor",
        "WORKDIR /workspace",
        'ENTRYPOINT ["/usr/bin/tini", "--"]',
        'CMD ["gpu-bench", "tui"]',
    ]
    return lines


def labels(recipe: dict) -> dict:
    return {"io.layersmith.gpu-benchmark.cuda": recipe["cuda_version"],
            "io.layersmith.gpu-benchmark.python": recipe["python_full_version"],
            "io.layersmith.gpu-benchmark.tools": ",".join(recipe["tools"]),
            "io.layersmith.gpu-benchmark.torch": recipe["torch"],
            "io.layersmith.gpu-benchmark.vllm": recipe.get("vllm") or "not-installed",
            "io.layersmith.gpu-benchmark.nccl-tests": recipe["nccl_tests"] if "nccl" in recipe["tools"] else "not-installed",
            "io.layersmith.gpu-benchmark.driver-minimum": recipe["driver_minimum"]}


def archive_name(image: str) -> str:
    """gpu-benchmark-1.0.0.tar for localhost/gpu-benchmark:1.0.0."""
    name, _, tag = image.rpartition(":")
    if not name:
        name, tag = image, "latest"
    if name.startswith("localhost/"):
        name = name[len("localhost/"):]
    return f"{name.replace('/', '-')}-{tag}.tar"


def run_command(image: str) -> str:
    return ("podman run --rm -it \\\n"
            "  --device nvidia.com/gpu=all \\\n"
            "  --security-opt=label=disable \\\n"
            "  --shm-size=16g \\\n"
            "  -v /models:/models:ro \\\n"
            "  -v /opt/gpu-results:/results \\\n"
            f"  {image}")


def getting_started(recipe: dict, image: str = "localhost/gpu-benchmark:1.0.0") -> dict:
    archive = archive_name(image)
    return {
        "title": "NVIDIA GPU & LLM Benchmark",
        "summary": "One offline image: live GPU dashboard (TUI), LLM token speed with vLLM, GEMM compute, "
                   "memory bandwidth, NCCL multi-GPU/multi-node tests, HTML/JSON/CSV reports and comparisons.",
        "image": image,
        "archive": archive,
        "cuda": recipe["cuda_version"], "python": recipe["python_full_version"], "tools": recipe["tools"],
        "driver_minimum": recipe["driver_minimum"],
        "check_host": "nvidia-smi\nnvidia-ctk cdi list        # must list nvidia.com/gpu=all\n"
                      "sudo mkdir -p /opt/gpu-results",
        "load": f"podman load -i {archive}\npodman images | grep gpu-benchmark",
        "verify": f"sha256sum -c {archive}.sha256   # or compare with the checksum shown in LayerSmith",
        "podman": run_command(image),
        "commands": [
            "gpu-bench tui            # default when the container starts with -it",
            "gpu-bench info",
            "gpu-bench health",
            "gpu-bench models",
            "gpu-bench llm --model /models/<model> --input-tokens 1024 --output-tokens 256 --concurrency 1,8,32",
            "gpu-bench compute --precision fp32,tf32,fp16,bf16,fp8",
            "gpu-bench memory",
            "gpu-bench nccl --test all",
            "gpu-bench full --model /models/<model>",
            "gpu-bench runs",
            "gpu-bench compare <run-a> <run-b>",
            "gpu-bench report latest",
        ],
        "batch_example": f"podman run --rm --device nvidia.com/gpu=all --security-opt=label=disable --shm-size=16g \\\n"
                         f"  -v /models:/models:ro -v /opt/gpu-results:/results {image} \\\n"
                         "  gpu-bench full --model /models/<model>",
        "notes": [
            f"The host needs the NVIDIA driver {recipe['driver_minimum']} or newer and the NVIDIA Container Toolkit "
            "with CDI (nvidia-ctk cdi generate). The image never installs or changes drivers.",
            "Models are read from /models only; nothing is downloaded at runtime (Hugging Face offline mode).",
            "Every run is saved in its own folder under /results; earlier runs are never overwritten.",
            "Multi-node NCCL needs RDMA devices, host networking and memlock limits: see the multi-node guide "
            "in docs/gpu-benchmark.md and the scripts under /opt/gpu-bench/scripts.",
        ],
    }


def markdown(recipe: dict, image: str = "localhost/gpu-benchmark:1.0.0") -> str:
    content = getting_started(recipe, image)
    versions = [f"- CUDA user space: `{recipe['cuda_version']}` (host driver >= `{recipe['driver_minimum']}`)",
                f"- Python: `{recipe['python_full_version']}`",
                f"- PyTorch: `{recipe['torch']}`",
                f"- vLLM: `{recipe['vllm'] or 'not installed'}`",
                f"- nccl-tests: `{recipe['nccl_tests']}`" if "nccl" in recipe["tools"] else "- nccl-tests: not installed",
                f"- nvbandwidth: `{recipe['nvbandwidth']}`" if "memory" in recipe["tools"] else "- nvbandwidth: not installed",
                f"- GPU kernels: {', '.join('sm_' + str(a) for a in recipe['gpu_architectures'])}",
                f"- Components: {', '.join(recipe['tools'])} + diagnostics, reports, TUI"]
    return ("# NVIDIA GPU & LLM Benchmark\n\n" + content["summary"] + "\n\n## Versions\n\n" + "\n".join(versions)
            + "\n\nExact Python package versions: `/opt/gpu-bench/requirements.lock` in the image.\n\n"
            + "## 1. Check the GPU server\n\n```sh\n" + content["check_host"] + "\n```\n\n"
            + "## 2. Load the image (no network needed)\n\n```sh\n" + content["load"] + "\n```\n\n"
            + "## 3. Start the dashboard\n\n```sh\n" + content["podman"] + "\n```\n\n"
            + "## Scripted runs\n\n```sh\n" + content["batch_example"] + "\n```\n\n"
            + "## Commands\n\n```sh\n" + "\n".join(content["commands"]) + "\n```\n\n"
            + "## Notes\n\n" + "\n".join(f"- {note}" for note in content["notes"]) + "\n")


def recipe_file(recipe: dict, image: str | None = None) -> dict:
    keys = ("cuda_version", "python_full_version", "tools", "torch", "vllm", "transformers", "tokenizers",
            "textual", "nccl_tests", "nvbandwidth", "driver_minimum", "gpu_architectures")
    return {**{key: recipe.get(key) for key in keys}, "image": image}


def write_context(context: Path, recipe: dict, image: str = "localhost/gpu-benchmark:1.0.0") -> list[str]:
    """context/gpu-benchmark/ becomes /opt/gpu-bench in the image."""
    root = context / "gpu-benchmark"
    if root.exists():
        shutil.rmtree(root)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "bin", "scripts")
    shutil.copytree(RUNTIME_DIR, root / PACKAGE, ignore=ignore)
    shutil.copytree(RUNTIME_DIR / "bin", root / "bin")
    shutil.copytree(RUNTIME_DIR / "scripts", root / "scripts")
    (root / "README.md").write_text(markdown(recipe, image), encoding="utf-8")
    (root / "recipe.json").write_text(json.dumps(recipe_file(recipe, image), indent=2) + "\n", encoding="utf-8")
    return sorted(str(path.relative_to(context)) for path in root.rglob("*") if path.is_file())
