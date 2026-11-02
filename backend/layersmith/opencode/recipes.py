"""Turn a small opencode Shared Terminal request into everything a build and the wizard need.

A project stores only the request ({"base", "opencode_version", "cpu",
"optional"}); the recipe is resolved again by whichever LayerSmith version
builds it, like the other templates.
"""

from layersmith.core.spec import InvalidSpec, parse_reference
from layersmith.opencode import catalog
from layersmith.opencode.catalog import OpencodeError

REQUEST_KEYS = {"base", "base_reference", "base_el", "opencode_version", "cpu", "optional"}


def _base(request: dict) -> dict:
    key = str(request.get("base") or catalog.DEFAULT_BASE)
    if key == "custom":
        reference = str(request.get("base_reference") or "").strip()
        if not reference:
            raise OpencodeError("Name the custom base image, for example a mirror of almalinux:10 in your registry")
        try:
            parsed = parse_reference(reference)
        except InvalidSpec as exc:
            raise OpencodeError(str(exc)) from exc
        if not parsed["tag"] and not parsed["digest"]:
            raise OpencodeError("Give the custom base a tag or a digest; an implicit 'latest' moves under you")
        el = str(request.get("base_el") or "")
        if el not in catalog.EL_MAJORS:
            raise OpencodeError("Say which Enterprise Linux major the custom base is: 9 or 10")
        return {"key": "custom", "display": parsed["source"], "reference": parsed["source"], "el": el,
                "distribution": None, "version": None, "verified": False}
    if key not in catalog.BASES:
        raise OpencodeError(f"Unknown base: {key}. Choose one of {', '.join(catalog.BASES)} or a custom base.")
    entry = catalog.BASES[key]
    return {"key": key, "display": key, "reference": entry["source"], "el": entry["el"],
            "distribution": entry["distribution"], "version": entry["version"], "verified": True}


def resolve(request: dict | None) -> dict:
    """Validate a request and expand it. Raises OpencodeError with a user-facing message."""
    request = request or {}
    if not isinstance(request, dict):
        raise OpencodeError("opencode must be an object")
    unknown = sorted(set(request) - REQUEST_KEYS)
    if unknown:
        raise OpencodeError("Unknown opencode setting: " + ", ".join(unknown))

    base = _base(request)
    version = str(request.get("opencode_version") or catalog.DEFAULT_OPENCODE)
    if version not in catalog.OPENCODE_VERSIONS:
        raise OpencodeError(f"opencode {version} is not pinned in this LayerSmith. Choose one of "
                            f"{', '.join(catalog.OPENCODE_VERSIONS)}.")
    cpu = str(request.get("cpu") or catalog.DEFAULT_CPU)
    if cpu not in catalog.CPU_VARIANTS:
        raise OpencodeError(f"Unknown CPU variant: {cpu}. Choose one of {', '.join(catalog.CPU_VARIANTS)}.")

    optional = request.get("optional")
    if optional is None:
        optional = list(catalog.DEFAULT_OPTIONAL)
    if not isinstance(optional, list):
        raise OpencodeError("optional must be a list")
    bad = sorted({str(o) for o in optional} - set(catalog.OPTIONAL))
    if bad:
        raise OpencodeError("Unknown optional toolchain: " + ", ".join(bad))
    enabled = [o for o in catalog.OPTIONAL if o in optional]

    opencode = catalog.opencode_download(version, cpu)
    ripgrep = catalog.RIPGREP
    downloads = [{"tool": "opencode", "version": version, **opencode},
                 {"tool": "ripgrep", "version": ripgrep["version"],
                  **{k: ripgrep[k] for k in ("url", "sha256", "files")}}]
    tools = [{"id": "opencode", "label": "opencode", "version": version, "enabled": True, "kind": "download",
              "purpose": "The terminal coding agent (one self-contained binary)",
              "verify": f"GitHub release asset digest ({catalog.CPU_VARIANTS[cpu]['asset']})"},
             {"id": "ripgrep", "label": "ripgrep (rg)", "version": ripgrep["version"], "enabled": True,
              "kind": "download", "purpose": "Code search; opencode would otherwise download it at first use",
              "verify": "GitHub release .sha256 file and asset digest"}]
    checks = [{"tool": "opencode", "binary": "opencode", "argv": ["opencode", "--version"], "expect": version},
              {"tool": "ripgrep", "binary": "rg", "argv": ["rg", "--version"], "expect": f"ripgrep {ripgrep['version']}"}]

    packages = list(catalog.BASE_PACKAGES)
    for group_id, group in catalog.PACKAGE_GROUPS.items():
        on = group_id in enabled
        tools.append({"id": group_id, "label": group["label"], "version": "from the base's repositories",
                      "enabled": on, "kind": "packages", "purpose": group["purpose"],
                      "verify": "dnf, signed by the distribution", "packages": group["packages"]})
        if on:
            packages += group["packages"]
            checks += [{"tool": group_id, **c} for c in group["check"]]
    checks += [{"tool": "base", **c} for c in catalog.BASE_CHECKS]
    packages = list(dict.fromkeys(packages))

    warnings = []
    if not base["verified"]:
        warnings.append(f"{base['display']} is a custom base: LayerSmith assumes it is EL{base['el']}-compatible with "
                        "BaseOS and AppStream repositories; a missing package fails the build")
    if cpu == "x64":
        warnings.append("The x64 build needs a CPU with AVX2. If the target host is older or its virtual CPU hides "
                        "AVX2, opencode stops with 'Illegal instruction': choose the baseline build instead")

    size = (catalog.SIZE_GB["base"] + catalog.SIZE_GB["opencode"] + catalog.SIZE_GB["ripgrep"]
            + sum(catalog.SIZE_GB.get(o, 0) for o in enabled))
    return {
        "profile": catalog.PROFILE["id"],
        "profile_name": catalog.PROFILE["name"],
        "profile_version": catalog.PROFILE["version"],
        "base": base,
        "opencode": {"version": version, "cpu": cpu, "asset": catalog.CPU_VARIANTS[cpu]["asset"]},
        "architecture": catalog.ARCHITECTURE,
        "tools": tools,
        "optional": enabled,
        "downloads": downloads,
        "packages": packages,
        "checks": checks,
        "environment": catalog.ENVIRONMENT,
        "host": catalog.HOST_DEFAULTS,
        "warnings": warnings,
        "estimated_size_gb": round(size, 1),
        "verified": catalog.VERIFIED,
    }


def default_names(recipe: dict) -> dict:
    return {"name": "opencode", "repository": "localhost/opencode", "tag": "1.0.0"}


def summary(recipe: dict) -> dict:
    """The small, stable part of a recipe a build record and its manifest carry."""
    return {"profile": recipe["profile"], "profile_version": recipe["profile_version"],
            "base": recipe["base"]["display"], "opencode": recipe["opencode"]["version"],
            "cpu": recipe["opencode"]["cpu"], "optional": recipe["optional"]}
