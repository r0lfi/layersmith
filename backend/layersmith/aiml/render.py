"""Containerfile lines, build-context files and check commands for AI/ML images.

Two ways to make an image:

  import   FROM the upstream image by digest, plus LayerSmith's OCI labels.
           No RUN, COPY or ENV: every layer is the publisher's, byte for byte.
  custom   The same base, then the requested Python packages installed by
           layersmith-aiml-pip (which protects the base image's framework
           builds), the check script, and offline defaults.

Either way everything is installed at build time; nothing is fetched when the
container starts on the air-gapped side.
"""

from __future__ import annotations

import shlex
import shutil
from pathlib import Path

from layersmith.aiml import catalog

ASSET_DIR = Path(__file__).with_name("assets")
CHECK_SCRIPT = ASSET_DIR / "layersmith-aiml-check"
PIP_SCRIPT = ASSET_DIR / "layersmith-aiml-pip"
OPT = "/opt/layersmith/aiml"
CHECK_PATH = "/usr/local/bin/layersmith-aiml-check"

#: Runtime defaults for an image that will never see the internet.
OFFLINE_ENV = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
               "HF_HUB_DISABLE_TELEMETRY": "1", "VLLM_NO_USAGE_STATS": "1", "DO_NOT_TRACK": "1",
               "MLFLOW_ENABLE_ARTIFACTS_PROGRESS_BAR": "false"}


def labels(recipe: dict) -> dict:
    return {
        "io.layersmith.aiml.template": recipe["template"],
        "io.layersmith.aiml.template-version": str(recipe["template_version"]),
        "io.layersmith.aiml.mode": recipe["mode"],
        "io.layersmith.aiml.base": recipe["base"]["display"],
        "io.layersmith.aiml.base-digest": recipe["base"]["digest"] or "resolved at build",
        "io.layersmith.aiml.cuda": recipe["cuda"] or "unknown",
        "io.layersmith.aiml.packages": " ".join(recipe["install"])[:900],
    }


def protected_env(recipe: dict) -> tuple[str, str]:
    names = sorted(catalog.PROTECTED_NAMES | set(catalog.TEMPLATES[recipe["template"]].get("protect", [])))
    return " ".join(names), " ".join(catalog.PROTECTED_PREFIXES)


def install_lines(recipe: dict, image_ref: str | None) -> list[str]:
    """Everything after FROM and LABEL. Empty for an import."""
    if recipe["mode"] == "import":
        return []
    names, prefixes = protected_env(recipe)
    lines = ["", f"# ---- LayerSmith AI/ML: {recipe['template_name']} {recipe['version']} "
                 f"(template {recipe['template']} v{recipe['template_version']}) ----",
             "# Packages are installed now, with network; the image never fetches anything at run time.",
             f"COPY --chmod=0755 aiml/layersmith-aiml-check {CHECK_PATH}"]
    if recipe["has_python"]:
        lines += [f"COPY --chmod=0644 aiml/requirements.txt {OPT}/requirements.txt",
                  f"COPY --chmod=0755 aiml/layersmith-aiml-pip {OPT}/layersmith-aiml-pip",
                  f'RUN LAYERSMITH_AIML_PROTECTED="{names}" \\',
                  f'    LAYERSMITH_AIML_PROTECTED_PREFIXES="{prefixes}" \\',
                  '    PY="$(command -v python3 || command -v python)" \\',
                  f'    && "$PY" {OPT}/layersmith-aiml-pip install {OPT}/requirements.txt'
                  + (f" --index-url {recipe['pip_index_url']}" if recipe.get("pip_index_url") else "")]
    env = {**OFFLINE_ENV, "LAYERSMITH_AIML_FRAMEWORK": recipe["framework"],
           "LAYERSMITH_AIML_EXPECT": " ".join(catalog.normalise(_name(r)) for r in recipe["install"])}
    if image_ref:
        # Last and tiny, so the check and its reports can name the image they ran in.
        env["LAYERSMITH_IMAGE"] = image_ref
    lines += ["", "ENV " + " \\\n    ".join(f'{key}="{value}"' for key, value in env.items())]
    return lines


def _name(requirement: str) -> str:
    for separator in ("[", "=", "<", ">", "!", "~"):
        requirement = requirement.split(separator, 1)[0]
    return requirement


def write_context(context: Path, recipe: dict) -> list[str]:
    root = context / "aiml"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    shutil.copy2(CHECK_SCRIPT, root / "layersmith-aiml-check")
    shutil.copy2(PIP_SCRIPT, root / "layersmith-aiml-pip")
    (root / "requirements.txt").write_text("".join(f"{r}\n" for r in recipe["install"]), encoding="utf-8")
    return sorted(str(p.relative_to(context)) for p in root.iterdir())


def check_env(recipe: dict, image_ref: str) -> dict:
    return {"LAYERSMITH_IMAGE": image_ref, "LAYERSMITH_AIML_FRAMEWORK": recipe["framework"],
            "LAYERSMITH_AIML_EXPECT": " ".join(catalog.normalise(_name(r)) for r in recipe["install"])}


def inline_check(recipe: dict, image_ref: str, mode: str = "basic") -> tuple[str, list[str]]:
    """(entrypoint, argv) that runs the check in any image, including an unchanged upstream one.

    The script travels in the argv as a shell here-document instead of a
    bind mount: LayerSmith itself usually runs in a container, and a path on
    its side means nothing to the runtime that starts the check.
    """
    exports = " ".join(f"{key}={shlex.quote(value)}" for key, value in check_env(recipe, image_ref).items())
    script = CHECK_SCRIPT.read_text(encoding="utf-8")
    body = (f"export {exports}\n"
            'PY="$(command -v python3 || command -v python || true)"\n'
            'if [ -z "$PY" ]; then\n'
            f"  {_shell_fallback(mode)}\n"
            "fi\n"
            f'exec "$PY" - {mode} <<\'LAYERSMITH_AIML_CHECK_EOF\'\n{script}\nLAYERSMITH_AIML_CHECK_EOF\n')
    return "/bin/sh", ["-c", body]


def _shell_fallback(mode: str) -> str:
    """A result line for images without Python (the CUDA images): report what the shell can see."""
    return ("V=\"${CUDA_VERSION:-}\"; N=\"$(command -v nvcc >/dev/null 2>&1 && nvcc --version | tail -n 1 | "
            "tr -d '\"\\\\' || echo none)\"; "
            "if [ -n \"$V\" ]; then S=passed; else S=failed; fi; "
            "printf 'LAYERSMITH_CHECK_RESULT {\"check\": \"%s\", \"status\": \"%s\", \"image\": \"%s\", "
            "\"steps\": [{\"name\": \"cuda version\", \"status\": \"%s\", \"detail\": \"image CUDA %s; nvcc: %s\"}, "
            "{\"name\": \"python\", \"status\": \"skipped\", \"detail\": \"no Python in this image\"}], "
            "\"info\": {\"env\": {\"CUDA_VERSION\": \"%s\"}, \"nvcc\": \"%s\"}}\\n' "
            f"{mode} \"$S\" \"$LAYERSMITH_IMAGE\" \"$S\" \"$V\" \"$N\" \"$V\" \"$N\"; "
            "[ \"$S\" = passed ]; exit $?")


def run_command(recipe: dict, image: str) -> dict:
    """How to start the image on a GPU host with only the GPUs it should have."""
    shown = "nvidia.com/gpu=GPU-<uuid>"
    common = (f"podman run --rm -it --device {shown} --security-opt=no-new-privileges "
              "--shm-size=16g -v /models:/models:ro")
    if recipe["framework"] == "vllm":
        return {"podman": f"{common} -p 8000:8000 {image} --model /models/<model>",
                "note": "The image's entrypoint is `vllm serve`; everything after the image goes to it."}
    if recipe["framework"] == "tensorflow":
        return {"podman": f"{common} {image} python3 -c 'import tensorflow as tf; "
                          "print(tf.config.list_physical_devices(\"GPU\"))'", "note": ""}
    return {"podman": f"{common} {image} python3 -c 'import torch; print(torch.cuda.get_device_name(0))'",
            "note": ""}


def gpu_test_command(recipe: dict, image: str) -> str:
    """The GPU test on a worker, with the check script from the bundle (works for imports too)."""
    entry = "" if recipe["entrypoint_wraps"] else "--entrypoint python3 "
    run = "python3 - gpu" if recipe["entrypoint_wraps"] else "- gpu"
    return (f"podman run --rm -i --device nvidia.com/gpu=GPU-<uuid> --security-opt=no-new-privileges "
            f"--cap-drop=ALL --network=none -e LAYERSMITH_IMAGE={image} {entry}{image} {run} "
            "< aiml/layersmith-aiml-check")
