"""Turn a small OpenShift Admin Toolkit request into everything a build and the wizard need.

A project stores only the request ({"base", "ocp_version", "versions",
"optional", ...}); the recipe is resolved again by whichever LayerSmith version
builds it, like the other templates.
"""

import re

from layersmith.core.spec import InvalidSpec, parse_reference
from layersmith.openshift import catalog
from layersmith.openshift.catalog import OpenShiftError

REQUEST_KEYS = {"base", "base_reference", "base_el", "ocp_version", "versions", "optional", "timezone",
                "prod_pattern"}
TIMEZONE_RE = re.compile(r"^(UTC|[A-Z][A-Za-z_]+(/[A-Za-z0-9_+-]+){1,2})$")
#: Becomes part of an extended regular expression in the shell profile; kept to
#: characters that cannot leave the quoted value or change how bash reads it.
PROD_PATTERN_RE = re.compile(r"^[A-Za-z0-9._|-]{1,64}$")


def _minor(version: str) -> str:
    return ".".join(version.split(".")[:2])


def _base(request: dict) -> dict:
    key = str(request.get("base") or catalog.DEFAULT_BASE)
    if key == "custom":
        reference = str(request.get("base_reference") or "").strip()
        if not reference:
            raise OpenShiftError("Name the custom base image, for example a mirror of almalinux:10 in your registry")
        try:
            parsed = parse_reference(reference)
        except InvalidSpec as exc:
            raise OpenShiftError(str(exc)) from exc
        if not parsed["tag"] and not parsed["digest"]:
            raise OpenShiftError("Give the custom base a tag or a digest; an implicit 'latest' moves under you")
        el = str(request.get("base_el") or "")
        if el not in catalog.EL_PACKAGES:
            raise OpenShiftError("Say which Enterprise Linux major the custom base is: 9 or 10")
        return {"key": "custom", "display": parsed["source"], "reference": parsed["source"], "el": el,
                "distribution": None, "version": None, "verified": False}
    if key not in catalog.BASES:
        raise OpenShiftError(f"Unknown base: {key}. Choose one of {', '.join(catalog.BASES)} or a custom base.")
    entry = catalog.BASES[key]
    return {"key": key, "display": key, "reference": entry["source"], "el": entry["el"],
            "distribution": entry["distribution"], "version": entry["version"], "verified": True}


def _version(tool_id: str, tool: dict, requested: dict, ocp_version: str) -> str:
    if tool.get("follows_ocp"):
        default = ocp_version
    else:
        default = tool["default_version"]
    version = str(requested.get(tool_id) or default)
    if tool_id == "oc" and version != ocp_version:
        raise OpenShiftError("oc and kubectl always come from the OpenShift client version chosen above")
    if version not in tool["versions"]:
        raise OpenShiftError(f"{tool['label']} {version} is not a pinned version in this LayerSmith. Choose one of "
                             f"{', '.join(tool['versions'])}; other versions are added with "
                             "backend/scripts/pin_openshift.py, never fetched unpinned.")
    return version


def _checks(entries: list[dict], version: str | None, ocp: dict) -> list[dict]:
    out = []
    for entry in entries:
        expect = entry["expect"].format(version=version or "", kubernetes=ocp["kubernetes"], minor=ocp["minor"])
        out.append({"binary": entry["binary"], "argv": entry["argv"], "expect": expect})
    return out


def resolve(request: dict | None) -> dict:
    """Validate a toolkit request and expand it. Raises OpenShiftError with a user-facing message."""
    request = request or {}
    if not isinstance(request, dict):
        raise OpenShiftError("openshift must be an object")
    unknown = sorted(set(request) - REQUEST_KEYS)
    if unknown:
        raise OpenShiftError("Unknown OpenShift toolkit setting: " + ", ".join(unknown))

    base = _base(request)
    ocp_version = str(request.get("ocp_version") or catalog.DEFAULT_OCP)
    if ocp_version not in catalog.OCP_RELEASES:
        raise OpenShiftError(f"OpenShift client {ocp_version} is not pinned in this LayerSmith. Choose one of "
                             f"{', '.join(catalog.OCP_RELEASES)}.")
    release = catalog.OCP_RELEASES[ocp_version]
    ocp = {"version": ocp_version, "minor": _minor(ocp_version), **release}

    requested_versions = request.get("versions") or {}
    if not isinstance(requested_versions, dict):
        raise OpenShiftError("versions must map a tool to a version")
    stray = sorted(set(requested_versions) - set(catalog.TOOLS))
    if stray:
        raise OpenShiftError("No version can be chosen for: " + ", ".join(map(str, stray)))

    optional = request.get("optional")
    if optional is None:
        optional = list(catalog.OPTIONAL)
    if not isinstance(optional, list):
        raise OpenShiftError("optional must be a list")
    bad = sorted({str(o) for o in optional} - set(catalog.OPTIONAL))
    if bad:
        raise OpenShiftError("Unknown optional tool: " + ", ".join(bad))
    enabled = [o for o in catalog.OPTIONAL if o in optional]

    tools, downloads, checks = [], [], []
    for tool_id, tool in catalog.TOOLS.items():
        on = tool.get("required") or tool_id in enabled
        version = _version(tool_id, tool, requested_versions, ocp_version)
        files = tool["versions"][version]
        files = files if isinstance(files, list) else [files]
        tools.append({"id": tool_id, "label": tool["label"], "purpose": tool["purpose"], "group": tool["group"],
                      "required": bool(tool.get("required")), "enabled": bool(on), "version": version,
                      "versions": list(tool["versions"]), "verify": tool["verify"],
                      "follows_ocp": bool(tool.get("follows_ocp")), "kind": "download"})
        if on:
            for item in files:
                downloads.append({"tool": tool_id, "version": version, **item})
            checks += [{"tool": tool_id, **c} for c in _checks(tool["check"], version, ocp)]

    packages = [*catalog.BASE_PACKAGES, *catalog.EL_PACKAGES[base["el"]]]
    for group_id, group in catalog.PACKAGE_GROUPS.items():
        on = group_id in enabled
        tools.append({"id": group_id, "label": group["label"], "purpose": group["purpose"], "group": group["group"],
                      "required": False, "enabled": on, "version": "from the base's repositories",
                      "versions": [], "verify": "dnf, signed by the distribution", "follows_ocp": False,
                      "kind": "packages", "packages": group["packages"]})
        if on:
            packages += group["packages"]
            checks += [{"tool": group_id, **c} for c in _checks(group["check"], None, ocp)]
    checks += [{"tool": "base", **c} for c in _checks(catalog.BASE_CHECKS, None, ocp)]
    packages = list(dict.fromkeys(packages))

    timezone = str(request.get("timezone") or catalog.DEFAULTS["timezone"])
    if not TIMEZONE_RE.match(timezone):
        raise OpenShiftError(f"Invalid time zone: {timezone!r} (use a tz database name such as Europe/Oslo or UTC)")
    prod_pattern = str(request.get("prod_pattern") or catalog.DEFAULTS["prod_pattern"])
    if not PROD_PATTERN_RE.match(prod_pattern):
        raise OpenShiftError("The production pattern may use letters, digits, '.', '_', '-' and '|' (alternatives)")

    warnings = []
    if not base["verified"]:
        warnings.append(f"{base['display']} is a custom base: LayerSmith assumes it is EL{base['el']}-compatible with "
                        "BaseOS and AppStream repositories; a missing package fails the build")
    if "container-tools" in enabled:
        warnings.append("podman, buildah and skopeo are installed as CLIs. Inspecting and copying images with skopeo "
                        "works out of the box; building inside Distrobox needs nested user namespaces, so the "
                        "toolkit also offers host-podman, which runs the host's Podman explicitly")
    if ocp["channel"].startswith("fast"):
        warnings.append(f"OpenShift {ocp_version} was on the {ocp['channel']} channel when it was pinned; "
                        "stable is the conservative choice")

    size = catalog.SIZE_GB["base"] + sum(catalog.SIZE_GB.get(t["id"], 0) for t in tools if t["enabled"])
    major, minor = (int(p) for p in ocp["minor"].split("."))
    return {
        "profile": catalog.PROFILE["id"],
        "profile_name": catalog.PROFILE["name"],
        "profile_version": catalog.PROFILE["version"],
        "base": base,
        "ocp": ocp,
        "architecture": catalog.ARCHITECTURE,
        "tools": tools,
        "optional": enabled,
        "downloads": downloads,
        "packages": packages,
        "checks": checks,
        "timezone": timezone,
        "locale": catalog.DEFAULTS["locale"],
        "prod_pattern": prod_pattern,
        "helpers": catalog.HELPERS,
        "aliases": catalog.ALIASES,
        "compatibility": {
            "client": ocp_version,
            "kubernetes": ocp["kubernetes"],
            "recommended_cluster": ocp["minor"],
            "supported_clusters": [f"{major}.{minor - 1}", f"{major}.{minor}", f"{major}.{minor + 1}"],
            "note": f"oc {ocp['minor']} is made for OpenShift {ocp['minor']} clusters. kubectl {ocp['kubernetes']} "
                    f"follows the Kubernetes skew policy (one minor older or newer than the API server), which "
                    f"covers OpenShift {major}.{minor - 1} to {major}.{minor + 1}. oc-version-check compares "
                    "against the cluster you are logged in to.",
        },
        "warnings": warnings,
        "estimated_size_gb": round(size, 1),
        "verified": catalog.VERIFIED,
    }


def default_names(recipe: dict) -> dict:
    """Suggested name, repository, version and the friendly alias tag (e.g. 4.22)."""
    return {"name": "openshift-admin", "repository": "localhost/openshift-admin", "tag": "1.0.0",
            "alias": recipe["ocp"]["minor"]}


def summary(recipe: dict) -> dict:
    """The small, stable part of a recipe a build record and its manifest carry."""
    return {"profile": recipe["profile"], "profile_version": recipe["profile_version"],
            "base": recipe["base"]["display"], "ocp": recipe["ocp"]["version"],
            "tools": {t["id"]: t["version"] for t in recipe["tools"] if t["enabled"] and t["kind"] == "download"},
            "optional": recipe["optional"], "timezone": recipe["timezone"]}
