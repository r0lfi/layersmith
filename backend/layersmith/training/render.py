"""The Containerfile section and build-context files of a training image.

Layout inside the image:

  /opt/venv                 the locked Python environment (on PATH)
  /opt/layersmith/          recipe.json, requirements locks, README.md,
                            bin/ (launchers), checks/ (layersmith-check),
                            examples/ (scripts, configs, synthetic sample data)
  /workspace /models /datasets /outputs /logs /cache
                            mount points; see docs.DIRECTORIES

Nothing large is downloaded into the image beyond the locked packages: no
model weights, no datasets, no tokens.
"""

import json
import shutil
from pathlib import Path

from layersmith.training import catalog, recipes

OPT = "/opt/layersmith"
VENV = "/opt/venv"
USER = "trainer"
UID = 1000

ASSET_GROUPS = {
    "hf": ["common", "hf"],
    "advanced": ["common", "hf", "advanced"],
    "llamafactory": ["common", "llamafactory"],
    "data": ["common", "dataprep"],
}

#: Environment for every training image. Caches go to /cache so a mounted
#: volume keeps downloads between runs; telemetry and automatic uploads are
#: off unless the user turns them on explicitly at runtime.
BASE_ENV = {
    "VIRTUAL_ENV": VENV,
    "PIP_NO_CACHE_DIR": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PYTHONUNBUFFERED": "1",
    "HOME": f"/home/{USER}",
    "XDG_CACHE_HOME": "/cache",
    "HF_HOME": "/cache/huggingface",
    "TRITON_CACHE_DIR": "/cache/triton",
    "TORCHINDUCTOR_CACHE_DIR": "/cache/torchinductor",
    "MPLCONFIGDIR": "/cache/matplotlib",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "DO_NOT_TRACK": "1",
    "WANDB_MODE": "disabled",
}
PROFILE_ENV = {
    "llama-factory": {"GRADIO_ANALYTICS_ENABLED": "False", "GRADIO_TEMP_DIR": "/cache/gradio",
                      "GRADIO_SERVER_PORT": "7860", "USE_MODELSCOPE_HUB": "0", "NLTK_DATA": "/cache/nltk_data"},
}
ADDON_ENV = {
    "jupyter": {"JUPYTER_CONFIG_DIR": "/cache/jupyter/config", "JUPYTER_DATA_DIR": "/cache/jupyter/data",
                "JUPYTER_RUNTIME_DIR": "/cache/jupyter/runtime", "IPYTHONDIR": "/cache/ipython"},
    # DeepSpeed's setup would otherwise try to build ops at install time.
    "deepspeed": {"DS_BUILD_OPS": "0", "CUDA_HOME": "/usr/local/cuda"},
}

#: Modules imported by `layersmith-check deps`, per profile and add-on.
IMPORTS = {
    "hf-finetune": ["torch", "transformers", "datasets", "accelerate", "peft", "trl", "bitsandbytes",
                    "huggingface_hub", "safetensors", "tokenizers", "sentencepiece", "yaml"],
    "llama-factory": ["torch", "transformers", "datasets", "accelerate", "peft", "trl", "bitsandbytes", "gradio",
                      "llamafactory", "nltk", "jieba"],
    "dataset-prep": ["datasets", "transformers", "tokenizers", "pandas", "numpy", "pyarrow", "sentencepiece", "yaml"],
}
IMPORTS["pytorch-advanced"] = IMPORTS["hf-finetune"]
ADDON_IMPORTS = {"tensorboard": ["tensorboard"], "jupyter": ["jupyterlab", "ipykernel"], "lm-eval": ["lm_eval"],
                 "deepspeed": [], "flash-attn": []}


def environment(recipe: dict) -> dict:
    env = {**BASE_ENV, **PROFILE_ENV.get(recipe["profile"], {})}
    for addon in recipe["addons"]:
        env.update(ADDON_ENV.get(addon, {}))
    return env


def recipe_file(recipe: dict) -> dict:
    """recipe.json inside the image: what it is, and what `layersmith-check deps` expects."""
    imports = list(IMPORTS[recipe["profile"]])
    for addon in recipe["addons"]:
        imports += ADDON_IMPORTS.get(addon, [])
    return {
        "profile": recipe["profile"], "profile_name": recipe["profile_name"],
        "profile_version": recipe["profile_version"], "stack": recipe["stack"], "addons": recipe["addons"],
        "target": recipe["target"], "architecture": recipe["architecture"],
        "base": {k: recipe["base"][k] for k in ("image", "tag", "digest")},
        "python": recipe["python"], "torch": recipe["torch"], "cuda": recipe["cuda"], "driver": recipe["driver"],
        "lock_digest": recipe["lock_digest"], "locked": recipe["versions"], "compiled": recipe["compiled"],
        "extra_python": recipe["extra_python"], "imports": imports,
    }


def _env_line(env: dict) -> str:
    return "ENV " + " \\\n    ".join(f'{key}="{value}"' for key, value in env.items())


def install_lines(recipe: dict) -> list[str]:
    """After the system packages: the locked Python environment and LayerSmith's files."""
    has_source = bool(recipes.lock_text(recipe["stack"], recipe["addons"], source=True))
    pip = f"{VENV}/bin/pip"
    lines = [
        "",
        "# ---- Training environment " + "-" * 50,
        f"# Profile {recipe['profile_name']} {recipe['profile_version']}; stack {recipe['stack_name']}; "
        f"add-ons: {', '.join(recipe['addons']) or 'none'}.",
        "# Python packages come from hash-locked files: every build installs exactly these versions.",
        # $PATH is expanded by the builder: the CUDA development base adds /usr/local/cuda/bin to it.
        _env_line({**environment(recipe), "PATH": f"{VENV}/bin:$PATH"}),
        "",
        f"COPY training/requirements.lock {OPT}/requirements.lock",
        f"RUN python3 -m venv {VENV} \\\n"
        f"    && {pip} install --require-hashes --no-deps --only-binary=:all: -r {OPT}/requirements.lock",
    ]
    if has_source:
        lines += [
            "# Packages published only as source, built against the environment above",
            "# (their build scripts import packages installed in the previous step).",
            f"COPY training/requirements-source.lock {OPT}/requirements-source.lock",
            f"RUN {pip} install --require-hashes --no-deps --no-build-isolation -r {OPT}/requirements-source.lock",
        ]
    if recipe["extra_python"]:
        lines += [
            "# Extra packages requested for this image, outside the locked recipe.",
            f"COPY training/requirements-extra.txt {OPT}/requirements-extra.txt",
            f"RUN {pip} install -r {OPT}/requirements-extra.txt",
        ]
    lines += [f"RUN {pip} check", "", f"COPY training/opt/ {OPT}/"]
    setup = [f"chmod 0755 {OPT}/bin/* {OPT}/checks/run.py", f"ln -s {OPT}/bin/* /usr/local/bin/"]
    if "jupyter" in recipe["addons"]:
        setup += [f"mkdir -p {VENV}/share/jupyter/lab/settings",
                  f"cp {OPT}/jupyter/overrides.json {VENV}/share/jupyter/lab/settings/overrides.json"]
    lines.append("RUN " + " \\\n    && ".join(setup))
    return lines


def final_lines(recipe: dict, image_ref: str | None) -> list[str]:
    """User, directories and defaults: the last layers, so earlier ones cache."""
    ports = sorted({p["port"] for p in recipe["ports"]})
    lines = [
        "",
        "# Unprivileged user (uid 1000) and the documented mount points. The writable ones",
        "# are world-writable with the sticky bit, so `--user $(id -u):$(id -g)` works too.",
        "RUN (userdel --remove ubuntu 2>/dev/null || true) \\\n"
        f"    && useradd --create-home --uid {UID} --shell /bin/bash {USER} \\\n"
        "    && mkdir -p /workspace /models /datasets /outputs /logs /cache \\\n"
        f"    && chown {USER}:{USER} /workspace /outputs /logs /cache \\\n"
        f"    && chmod 1777 /workspace /outputs /logs /cache /home/{USER}",
        "WORKDIR /workspace",
        f"USER {USER}",
    ]
    if ports:
        lines.append("EXPOSE " + " ".join(map(str, ports)))
    lines.append('CMD ["/bin/bash"]')
    if image_ref:
        # Last, and tiny: lets examples and the GPU check name the image they ran in.
        lines += ["", f'ENV LAYERSMITH_IMAGE="{image_ref}"']
    return lines


def labels(recipe: dict) -> dict:
    return {
        "io.layersmith.training.profile": recipe["profile"],
        "io.layersmith.training.profile-version": recipe["profile_version"],
        "io.layersmith.training.stack": recipe["stack"],
        "io.layersmith.training.addons": ",".join(recipe["addons"]),
        "io.layersmith.training.target": recipe["target"],
        "io.layersmith.training.lock-digest": recipe["lock_digest"],
    }


def write_context(context: Path, recipe: dict, readme: str) -> list[str]:
    """Put the training files into a build context; returns the paths written."""
    root = context / "training"
    if root.exists():
        shutil.rmtree(root)
    opt = root / "opt"
    opt.mkdir(parents=True)
    (root / "requirements.lock").write_text(recipes.lock_text(recipe["stack"], recipe["addons"], source=False))
    source = recipes.lock_text(recipe["stack"], recipe["addons"], source=True)
    if source:
        (root / "requirements-source.lock").write_text(source)
    if recipe["extra_python"]:
        (root / "requirements-extra.txt").write_text("\n".join(recipe["extra_python"]) + "\n")
    for group in ASSET_GROUPS[recipe["examples"]]:
        shutil.copytree(recipes.ASSET_DIR / group, opt, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if "jupyter" in recipe["addons"]:
        shutil.copytree(recipes.ASSET_DIR / "jupyter", opt / "jupyter", dirs_exist_ok=True)
    # Copy the requirement files next to the recipe too, so the image documents itself.
    for name in ("requirements.lock", "requirements-source.lock", "requirements-extra.txt"):
        if (root / name).is_file():
            shutil.copy2(root / name, opt / name)
    (opt / "recipe.json").write_text(json.dumps(recipe_file(recipe), indent=2) + "\n")
    (opt / "README.md").write_text(readme)
    return sorted(str(p.relative_to(context)) for p in root.rglob("*") if p.is_file())


def asset_listing(recipe: dict) -> list[str]:
    """Files under /opt/layersmith that come from the assets, for documentation and tests."""
    names = set()
    for group in ASSET_GROUPS[recipe["examples"]]:
        base = recipes.ASSET_DIR / group
        names |= {str(p.relative_to(base)) for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    return sorted(names)


def describe_addons() -> list[dict]:
    return [{"id": key, **value} for key, value in catalog.ADDONS.items()]
