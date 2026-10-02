"""Resolve a training request into one concrete, validated recipe.

The request in a project's spec is small:

    {"profile": "hf-finetune", "target": "nvidia-cuda", "stack": "hf-cu129",
     "addons": ["tensorboard"], "intent": {...}, "extra_python": ["einops==0.8.2"]}

resolve() turns it into everything a build and the dashboard need: base image
and digest, Python and PyTorch versions, every locked package version, the
tools with explanations, the lock files to install, and notes on decisions
made automatically. A request that cannot work raises TrainingConflict, which
carries a concrete alternative the client can apply.

The same function runs for the dashboard preview, project validation and the
build itself, so the frontend and backend cannot disagree about compatibility.
"""

import hashlib
import re
from functools import lru_cache
from pathlib import Path

from layersmith.core.spec import InvalidSpec
from layersmith.training import catalog

STACK_DIR = Path(__file__).resolve().parent / "stacks"
ASSET_DIR = Path(__file__).resolve().parent / "assets"

#: Bumped when a profile's recipe changes in a way a user should notice
#: (different examples, checks or system packages). Library versions are
#: covered separately by the lock digest.
PROFILE_VERSION = "1.0.0"

EXTRA_PYTHON_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]{0,99})(\[[a-z0-9,_-]{1,60}\])?==([A-Za-z0-9.+!_-]{1,40})$")
MAX_EXTRA_PYTHON = 30
NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*(==|@)\s*(\S+)")
WHEEL_VERSION_RE = re.compile(r"/[^/]+?-([0-9][^-]*?)-(?:cp|py)\d")


class TrainingConflict(InvalidSpec):
    """A request that cannot be built, with an alternative that can.

    `alternative` is a partial training request (for example {"stack":
    "fa2-cu128"}) the client can merge into its request; `label` describes it.
    """

    def __init__(self, message, alternative=None, label=None):
        super().__init__(message)
        self.alternative = alternative
        self.label = label


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# ------------------------------------------------------------------ locks

@lru_cache(maxsize=None)
def lock_blocks(stack: str, component: str) -> tuple[tuple[str, str], ...]:
    """(name, requirement block) pairs of one lock file, in file order."""
    path = STACK_DIR / stack / f"{component}.lock"
    found, name, lines = [], None, []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            if name:
                found.append((name, "\n".join(lines)))
            match = NAME_RE.match(line)
            if not match:
                raise ValueError(f"{path}: unrecognised line {line!r}")
            name, lines = normalise(match.group(1)), [line]
        else:
            lines.append(line)
    if name:
        found.append((name, "\n".join(lines)))
    return tuple(found)


def block_version(block: str) -> str:
    first = block.splitlines()[0].rstrip(" \\")
    match = NAME_RE.match(first)
    if match.group(2) == "==":
        return match.group(3)
    # A direct URL to a wheel: the version is in the file name, URL-encoded.
    url = match.group(3).replace("%2B", "+")
    version = WHEEL_VERSION_RE.search(url)
    if not version:
        raise ValueError(f"cannot read a version from {url}")
    return version.group(1)


@lru_cache(maxsize=None)
def source_builds(stack: str) -> frozenset[str]:
    path = STACK_DIR / stack / "source-builds.txt"
    if not path.is_file():
        return frozenset()
    return frozenset(normalise(line.strip()) for line in path.read_text().splitlines()
                     if line.strip() and not line.startswith("#"))


def merged_lock(stack: str, addons: list[str]) -> list[tuple[str, str]]:
    """Base lock plus the chosen add-ons' locks, as one deduplicated list.

    The lock script resolves each add-on against one combined resolution, so
    the same package can only ever appear with the same pin here. That is
    checked anyway: a hand-edited lock must fail loudly, not install twice.
    """
    merged: dict[str, str] = {}
    for component in ["base", *[catalog.ADDONS[a]["lock"] for a in addons]]:
        for name, block in lock_blocks(stack, component):
            if name in merged and block_version(merged[name]) != block_version(block):
                raise ValueError(f"{stack}: {name} is locked at two versions ({component})")
            merged.setdefault(name, block)
    return sorted(merged.items())


def locked_versions(stack: str, addons: list[str]) -> dict[str, str]:
    return {name: block_version(block) for name, block in merged_lock(stack, addons)}


def lock_text(stack: str, addons: list[str], *, source: bool) -> str:
    """requirements text for pip: binary-only packages, or the source-built ones."""
    sources = source_builds(stack)
    blocks = [block for name, block in merged_lock(stack, addons) if (name in sources) == source]
    if not blocks:
        return ""
    header = (f"# LayerSmith training stack {stack}, add-ons: {', '.join(addons) or 'none'}.\n"
              f"# {'Built from source' if source else 'Wheels only'}; hash-checked (pip --require-hashes).\n")
    return header + "\n".join(blocks) + "\n"


def lock_digest(stack: str, addons: list[str]) -> str:
    text = lock_text(stack, addons, source=False) + lock_text(stack, addons, source=True)
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


# ------------------------------------------------------------- resolution

def _tool_entry(tool_id: str, versions: dict, required: bool, why: str = "", addon: str | None = None) -> dict:
    tool = catalog.TOOLS[tool_id]
    return {"id": tool_id, **{k: v for k, v in tool.items()}, "version": versions.get(normalise(tool["package"])),
            "required": required, "why": why, "addon": addon}


def recommend(intent: dict | None) -> dict:
    """Which profile fits the stated purpose, and why."""
    intent = intent or {}
    task, method, execution = intent.get("task"), intent.get("method"), intent.get("execution")
    if task == "data-prep" or execution == "cpu":
        return {"profile": "dataset-prep",
                "reason": "Preparing data needs no GPU. The CPU profile is small and builds quickly."}
    if task in ("continued-pretraining", "pretraining"):
        return {"profile": "pytorch-advanced",
                "reason": "Continued pretraining and pretraining use plain causal-LM training on your own "
                          "text; the Advanced profile has that example and the multi-GPU configurations."}
    if method == "full" and execution == "multi-gpu":
        return {"profile": "pytorch-advanced",
                "reason": "Full fine-tuning on several GPUs usually needs FSDP or DeepSpeed, which the "
                          "Advanced profile documents."}
    return {"profile": "hf-finetune",
            "reason": "SFT or DPO with LoRA/QLoRA is exactly what the recommended Hugging Face profile is for."}


def resolve(request: dict | None, architecture: str | None = None) -> dict:
    """Validate a training request and expand it into a recipe. Raises TrainingConflict."""
    request = dict(request or {})
    profile = catalog.PROFILES_BY_ID.get(request.get("profile") or "")
    if profile is None:
        raise TrainingConflict(f"Unknown training profile: {request.get('profile') or '(none)'}",
                               {"profile": "hf-finetune"}, "Use Hugging Face Fine-tuning")

    target = request.get("target") or profile["targets"][0]
    if target not in catalog.TARGETS:
        raise TrainingConflict(f"Unknown hardware target: {target}", {"target": profile["targets"][0]},
                               f"Use {catalog.TARGETS[profile['targets'][0]]['name']}")
    if target not in profile["targets"]:
        if target == "cpu":
            raise TrainingConflict(
                f"{profile['name']} is built for NVIDIA GPUs. For CPU-only work choose Dataset Preparation - "
                "CPU. (The GPU image builds without a GPU, and its CPU smoke test runs without one too.)",
                {"profile": "dataset-prep", "target": "cpu"}, "Switch to Dataset Preparation - CPU")
        raise TrainingConflict(
            f"{profile['name']} does not train on GPUs; it is a CPU profile for preparing data.",
            {"profile": "hf-finetune", "target": "nvidia-cuda"}, "Switch to Hugging Face Fine-tuning")
    target_info = catalog.TARGETS[target]
    if architecture and architecture != target_info["architecture"]:
        raise TrainingConflict(
            f"{target_info['name']} recipes exist for {target_info['architecture']} only; {architecture} has no "
            "documented, verified recipe yet.",
            {"architecture": target_info["architecture"]}, f"Build for {target_info['architecture']}")

    stack_id = request.get("stack") or profile["stacks"][0]
    if stack_id not in profile["stacks"]:
        raise TrainingConflict(f"Stack {stack_id} is not offered for {profile['name']}.",
                               {"stack": profile["stacks"][0]}, f"Use {catalog.STACKS[profile['stacks'][0]]['name']}")
    stack = catalog.STACKS[stack_id]

    # No "addons" key means the profile's defaults; an empty list means none.
    requested = request.get("addons")
    addons = list(dict.fromkeys(profile["default_addons"] if requested is None else requested))
    for addon in addons:
        if addon not in catalog.ADDONS:
            raise TrainingConflict(f"Unknown add-on: {addon}")
        if addon not in profile["addons"]:
            other = next((p for p in catalog.PROFILES if addon in p["addons"]), None)
            raise TrainingConflict(
                f"{catalog.ADDONS[addon]['name']} is not offered for {profile['name']}"
                + (f"; it is part of {other['name']}." if other else "."),
                {"addons": [a for a in addons if a != addon]}, f"Remove {catalog.ADDONS[addon]['name']}")
        allowed = catalog.ADDONS[addon].get("stacks")
        if allowed and stack_id not in allowed:
            needed = catalog.STACKS[allowed[0]]
            raise TrainingConflict(
                f"FlashAttention 2.8.3 publishes prebuilt wheels only for PyTorch 2.8 and older "
                f"(CUDA 12); this stack uses PyTorch {stack['torch']}, and building FlashAttention from source "
                "takes hours and a large machine. Switch to the stack "
                f"'{needed['name']}', or leave FlashAttention out and use PyTorch's built-in SDPA attention "
                "(attn_implementation='sdpa'), which includes a fused attention kernel.",
                {"stack": allowed[0]}, f"Switch to {needed['name']}")
    # Same order every time, so the same choice always produces the same Containerfile.
    addons = [a for a in profile["addons"] if a in addons]

    notes = []
    base = stack["base"]
    base_reason = (f"Ubuntu 24.04 provides Python {stack['python']}; "
                   + ("PyTorch brings its CUDA user-space libraries as Python wheels, so no CUDA base image "
                      "is needed." if stack["cuda"] else "no CUDA is needed for this profile."))
    if any(catalog.ADDONS[a].get("needs_devel_base") for a in addons):
        base = stack["devel_base"]
        base_reason = (f"DeepSpeed compiles its CUDA ops with nvcc when they are first used, so this image starts "
                       f"from NVIDIA's CUDA {stack['cuda']} development image instead of plain Ubuntu "
                       "(larger download, same Python and libraries).")
        notes.append("Base image switched to the CUDA development image because DeepSpeed is selected.")
    if stack_id != profile["stacks"][0]:
        notes.append(f"Stack {stack['name']} instead of the default: {stack['summary']}")

    extra_python = []
    versions = locked_versions(stack_id, addons)
    for raw in request.get("extra_python") or []:
        item = str(raw).strip()
        if not item:
            continue
        match = EXTRA_PYTHON_RE.match(item)
        if not match:
            raise TrainingConflict(
                f"Extra Python package {item!r} must be pinned exactly, as name==version (for example "
                "einops==0.8.2). Unpinned packages would make every build install whatever is newest.")
        name = normalise(match.group(1))
        if name in versions and versions[name] != match.group(3):
            raise TrainingConflict(
                f"{match.group(1)} is locked at {versions[name]} by this recipe. Changing a locked version is "
                "not supported here because the rest of the stack was resolved against it. Remove it, or use "
                "Advanced mode with your own Containerfile.",
                {"extra_python": [x for x in request.get("extra_python") or [] if str(x).strip() != item]},
                f"Remove {item}")
        if name not in versions:
            extra_python.append(item)
    if len(extra_python) > MAX_EXTRA_PYTHON:
        raise TrainingConflict(f"At most {MAX_EXTRA_PYTHON} extra Python packages")
    if extra_python:
        notes.append("Extra Python packages are installed after the locked stack without hash checks, and "
                     "pip check verifies they do not break it. Earlier test results of the standard recipe "
                     "do not apply to this image.")

    # Add-ons not chosen yet still show the version they would install.
    offered = [a for a in profile["addons"] if stack_id in catalog.ADDONS[a].get("stacks", [stack_id])]
    shown = {**locked_versions(stack_id, offered), **versions}
    for addon in profile["addons"]:  # an add-on tied to another stack shows that stack's version
        for other in catalog.ADDONS[addon].get("stacks", []):
            if other != stack_id:
                shown = {**locked_versions(other, [addon]), **shown}
    tools = [_tool_entry(tool_id, versions, True, why) for tool_id, why in profile["tools"]]
    for addon in profile["addons"]:
        for tool_id in catalog.ADDONS[addon]["tools"]:
            tools.append(_tool_entry(tool_id, shown, False, addon=addon))

    system = catalog.SYSTEM_TOOLS["gpu" if target_info["gpu"] else "cpu"]
    ports = list(profile.get("ports", []))
    for addon in addons:
        ports += catalog.ADDONS[addon].get("ports", [])

    intent = request.get("intent") or profile["intent"]
    if intent.get("task") == "pretraining":
        notes.append("Pretraining from scratch is offered as a function test and learning exercise: a useful "
                     "language model needs far more data and compute than an example can show.")
    if intent.get("method") == "qlora" and "bitsandbytes" not in versions:
        raise TrainingConflict("QLoRA needs bitsandbytes, which this profile does not include.",
                               {"profile": "hf-finetune"}, "Use Hugging Face Fine-tuning")

    return {
        "profile": profile["id"], "profile_name": profile["name"], "profile_version": PROFILE_VERSION,
        "target": target, "target_name": target_info["name"], "gpu": target_info["gpu"],
        "architecture": target_info["architecture"],
        "stack": stack_id, "stack_name": stack["name"], "stack_summary": stack["summary"],
        "addons": addons, "intent": intent,
        "base": {"image": base["image"], "tag": base["tag"], "digest": base["digest"],
                 "reference": f"{base['image']}@{base['digest']}", "display": f"{base['image']}:{base['tag']}",
                 "reason": base_reason},
        "python": stack["python"], "torch": stack["torch"], "cuda": stack["cuda"],
        "driver": stack["driver"], "gpu_support": stack["gpu_support"],
        "host_requirements": target_info["host_requirements"],
        "versions": versions, "lock_digest": lock_digest(stack_id, addons),
        "compiled": sorted(source_builds(stack_id) & set(versions)),
        "tools": tools,
        "system_packages": system["packages"], "system_why": system["why"],
        "extra_python": extra_python,
        "ports": ports,
        "notes": notes,
        "examples": profile["examples"],
        "reference": reference_for(profile["id"], stack_id, addons),
    }


def reference_for(profile_id: str, stack_id: str, addons: list[str]) -> dict | None:
    """The closest reference build: same profile and stack, preferring the same add-ons.

    Says whether it matches exactly, so an estimate is never presented as a
    measurement of this exact combination.
    """
    candidates = [r for r in catalog.REFERENCE_BUILDS if r["profile"] == profile_id and r["stack"] == stack_id]
    if not candidates:
        return None
    best = max(candidates, key=lambda r: len(set(r["addons"]) & set(addons)) - len(set(r["addons"]) ^ set(addons)))
    devel = any(catalog.ADDONS[a].get("needs_devel_base") for a in addons)
    if devel != any(catalog.ADDONS[a].get("needs_devel_base") for a in best["addons"]):
        return None  # a different base image: the numbers would mislead
    return {**best, "exact": sorted(best["addons"]) == sorted(addons)}


def is_customized(spec: dict) -> bool:
    """Whether the image differs from the standard recipe beyond its own choices."""
    training = spec.get("training") or {}
    scripts = {k: v for k, v in (spec.get("scripts") or {}).items() if v and v.strip()}
    return bool(training.get("extra_python") or spec.get("packages") or spec.get("extra_packages")
                or spec.get("presets") or spec.get("files") or spec.get("tools") or scripts or spec.get("env"))
