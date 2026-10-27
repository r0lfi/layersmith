"""Turn a small AI/ML request into everything a build, the wizard and an export need.

A project stores only the request ({"template", "version", "mode", "tools",
"pip", ...}); the recipe is resolved again by whichever LayerSmith version
builds it, like the training profiles.
"""

import json
from functools import lru_cache
from pathlib import Path

from layersmith.aiml import catalog
from layersmith.aiml.catalog import AimlError
from layersmith.core.spec import DIGEST_RE, InvalidSpec, parse_reference

PACKAGE_DIR = Path(__file__).with_name("packages")
MODES = ("import", "custom")
REQUEST_KEYS = {"template", "version", "mode", "tools", "pip", "base", "family", "pip_index_url"}


@lru_cache(maxsize=None)
def base_packages(key: str | None) -> dict[str, str]:
    """Python distributions recorded from a published image, {normalised name: version}.

    Read from the image's layers when the template version was verified, so
    the wizard can say what is already there before anything is pulled.
    """
    if not key:
        return {}
    path = PACKAGE_DIR / f"{key}.json"
    if not path.is_file():
        return {}
    return {catalog.normalise(name): version for name, version in json.loads(path.read_text()).items()}


def _base_for(template: dict, request: dict) -> tuple[dict, dict]:
    """(version entry, base reference) for the request."""
    if template["framework"] == "custom":
        reference = str(request.get("base") or "").strip()
        if not reference:
            raise AimlError("Name the base image of a Custom AI Container, for example "
                            "nvcr.io/nvidia/pytorch:26.09-py3")
        try:
            parsed = parse_reference(reference)
        except InvalidSpec as exc:
            raise AimlError(str(exc)) from exc
        if not parsed["tag"] and not parsed["digest"]:
            raise AimlError("Give the base image a tag or a digest; an implicit 'latest' moves under you")
        family = request.get("family") or "deb"
        if family not in ("deb", "rpm"):
            raise AimlError("The package family of a custom base is deb or rpm")
        source = parsed["source"]
        repository = parsed["repository"]
        if not parsed["registry"] and "/" not in repository:
            repository = f"library/{repository}"
        image = f"{parsed['registry'] or 'docker.io'}/{repository}"
        entry = {"image": image, "tag": parsed["tag"], "digest": parsed["digest"], "cuda": None,
                 "framework_version": None,
                 "python": None, "os": None, "components": {}, "compressed_gb": None, "family": family,
                 "sources": [], "custom": True}
        verified_source = f"{image}/".startswith(catalog.TRUSTED_REGISTRIES)
        display = source.split("@")[0] if parsed["tag"] else source
        return entry, {"reference": source, "display": display, "digest": parsed["digest"],
                       "verified": False, "trusted_registry": verified_source}

    versions = template["versions"]
    version = str(request.get("version") or template["default_version"])
    if version not in versions:
        raise AimlError(f"{template['name']} {version} is not a verified version in this LayerSmith. "
                        f"Choose one of {', '.join(versions)}, or use the Custom AI Container for any other tag.")
    entry = versions[version]
    if not DIGEST_RE.match(entry["digest"]):
        raise AimlError(f"{template['name']} {version} has no pinned digest")
    return entry, {"reference": f"{entry['image']}:{entry['tag']}@{entry['digest']}",
                   "display": f"{entry['image']}:{entry['tag']}", "digest": entry["digest"],
                   "verified": True, "trusted_registry": True}


def resolve(request: dict | None) -> dict:
    """Validate an AI/ML request and expand it. Raises AimlError with a user-facing message."""
    request = request or {}
    if not isinstance(request, dict):
        raise AimlError("aiml must be an object")
    unknown = sorted(set(request) - REQUEST_KEYS)
    if unknown:
        raise AimlError("Unknown AI/ML setting: " + ", ".join(unknown))
    template_id = str(request.get("template") or "nvidia-pytorch")
    if template_id not in catalog.TEMPLATES:
        raise AimlError(f"Unknown AI/ML template: {template_id}")
    template = catalog.TEMPLATES[template_id]
    entry, base = _base_for(template, request)
    version = entry["tag"] if template["framework"] == "custom" else str(request.get("version")
                                                                        or template["default_version"])

    mode = str(request.get("mode") or "custom")
    if mode not in MODES:
        raise AimlError("Mode is 'import' (the upstream image as it is) or 'custom' (build on top of it)")

    tools_requested = request.get("tools") or []
    pip_requested = request.get("pip") or []
    if not isinstance(tools_requested, list) or not isinstance(pip_requested, list):
        raise AimlError("tools and pip must be lists")
    if mode == "import" and (tools_requested or pip_requested):
        raise AimlError("An import keeps the upstream image unchanged, so it cannot add packages. "
                        "Switch to 'Build a custom image' to add tools.")
    if (tools_requested or pip_requested) and not template["python"]:
        raise AimlError(f"{template['name']} images have no Python, so Python tools cannot be added. "
                        "Use NVIDIA PyTorch or a Custom AI Container for Python work.")

    included = base_packages(entry.get("packages"))
    known_inventory = bool(included)
    tools, install, warnings, refused = [], [], [], []
    for tool_id in dict.fromkeys(str(t) for t in tools_requested):
        tool = catalog.TOOLS_BY_ID.get(tool_id)
        if tool is None:
            raise AimlError(f"Unknown AI tool: {tool_id}")
        if template["framework"] not in tool.get("frameworks", [template["framework"]]):
            raise AimlError(f"{tool['label']} is not offered for {template['name']} images")
        name = catalog.normalise(tool["pip"])
        if name in included:
            tools.append({**tool, "status": "included", "version": included[name]})
            continue
        if catalog.is_protected(name, template_id):
            # Building it would replace or bypass the vendor's build (flash-attn
            # against NVIDIA's PyTorch); refused rather than silently skipped.
            refused.append(f"{tool['label']} is built against the framework in the base image and cannot be "
                           f"added to {template['name']} by pip")
            continue
        tools.append({**tool, "status": "install", "version": None})
        install.append(tool["pip"])
        if tool.get("note") and tool_id == "bitsandbytes":
            warnings.append(tool["note"])
    if refused:
        raise AimlError("; ".join(refused))

    if len(pip_requested) > catalog.MAX_PIP:
        raise AimlError(f"At most {catalog.MAX_PIP} extra Python packages")
    extra, seen = [], {catalog.normalise(p) for p in install}
    for value in pip_requested:
        parsed = catalog.parse_requirement(value)
        if catalog.is_protected(parsed["name"], template_id):
            have = included.get(parsed["name"])
            raise AimlError(f"{parsed['name']} is provided by the base image"
                            f"{f' ({have})' if have else ''} and is protected: installing it from PyPI would "
                            "replace a build made for this CUDA version. Choose another base version instead.")
        if parsed["name"] in seen:
            raise AimlError(f"{parsed['name']} is listed twice")
        seen.add(parsed["name"])
        if parsed["name"] in included and not parsed["pinned"]:
            warnings.append(f"{parsed['name']} {included[parsed['name']]} is already in the base image; "
                            "pip keeps it unless your version specifier asks for another one")
        if not parsed["pinned"]:
            warnings.append(f"{parsed['requirement']} is not pinned: pip picks the newest compatible version at "
                            "build time, and the build records which one")
        extra.append(parsed["requirement"])

    index_url = request.get("pip_index_url")
    if index_url:
        if not catalog.INDEX_URL_RE.match(str(index_url)):
            raise AimlError("pip_index_url must be an http(s) URL without credentials, spaces or query strings")
        if str(index_url).startswith("http://"):
            warnings.append("The package index is plain HTTP: packages are fetched without transport security")
    if not base["trusted_registry"]:
        warnings.append(f"{base['display']} is not from a vendor or project registry LayerSmith knows; its "
                        "contents and origin are your responsibility")
    if template["framework"] == "custom":
        warnings.append("Custom base: CUDA, framework and Python versions are read from the image after the build, "
                        "and GPU compatibility can only be checked then")
    if template_id == "tensorflow":
        warnings.append("TensorFlow 2.21's kernels are compiled up to sm_89 with compute_90 PTX: on the H200 and "
                        "the RTX PRO 6000 it runs only through PTX JIT compilation (slow first start), which "
                        "TensorFlow does not document. Validate it with the GPU test on the worker.")

    family = entry.get("family") or template["family"]
    compressed = entry.get("compressed_gb")
    # podman save writes layers uncompressed; an unpacked image is typically
    # about twice its registry (compressed) size.
    estimated = round(compressed * 2.2 + (1.5 if install or extra else 0), 1) if compressed else None
    pinned_cuda = entry.get("cuda")
    return {
        "template": template_id,
        "template_name": template["name"],
        "template_version": template["template_version"],
        "framework": template["framework"],
        "version": version,
        "mode": mode,
        "base": {**base, "image": entry["image"], "tag": entry["tag"]},
        "family": family,
        "cuda": pinned_cuda,
        "cuda_short": ".".join(pinned_cuda.split(".")[:2]) if pinned_cuda else None,
        "framework_version": entry.get("framework_version"),
        "python": entry.get("python"),
        "os": entry.get("os"),
        "components": entry.get("components", {}),
        "min_capability": entry.get("min_capability"),
        "max_native_capability": entry.get("max_native_capability"),
        "entrypoint_wraps": template["entrypoint_wraps"],
        "has_python": template["python"],
        "tools": tools,
        "pip": extra,
        "install": install + extra,
        "pip_index_url": index_url or None,
        "included_packages_known": known_inventory,
        "included": {name: included[name] for name in sorted(included) if name in HIGHLIGHT} if included else {},
        "warnings": list(dict.fromkeys(warnings)),
        "estimated_size_gb": estimated,
        "compressed_gb": compressed,
        "sources": entry.get("sources", []),
        "verified": catalog.VERIFIED if base["verified"] else None,
    }


#: Distributions worth showing in the wizard's "already in the base image" list.
HIGHLIGHT = {catalog.normalise(t["pip"]) for t in catalog.TOOLS} | {
    "torch", "torchvision", "torchaudio", "triton", "tensorrt", "torch-tensorrt", "transformer-engine",
    "flash-attn", "flash-attn-4", "notebook", "huggingface-hub", "safetensors", "tokenizers", "pyarrow",
    "tensorboard", "nvidia-modelopt", "vllm", "tensorflow", "keras", "xformers", "flashinfer-python", "pillow",
}


def default_names(recipe: dict) -> dict:
    """Suggested project name, repository and tag, e.g. layersmith-pytorch-cuda13.4 / 1.0.0."""
    short = {"nvidia-pytorch": "pytorch", "nvidia-cuda": "cuda", "vllm": "vllm", "tensorflow": "tensorflow",
             "custom": "ai"}[recipe["template"]]
    cuda = f"-cuda{recipe['cuda_short']}" if recipe.get("cuda_short") else ""
    return {"name": f"{short}{cuda.replace('.', '')}", "repository": f"localhost/layersmith-{short}{cuda}",
            "tag": "1.0.0"}


def summary(recipe: dict) -> dict:
    """The small, stable part of a recipe a build record and its labels carry."""
    return {key: recipe[key] for key in ("template", "template_version", "version", "mode", "cuda",
                                         "framework", "framework_version", "python", "install")}
