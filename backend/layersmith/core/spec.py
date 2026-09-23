"""Validation of image specifications and image references.

Nothing from a request reaches a Containerfile, a filename or a build command
without passing through here first. Values are matched against explicit
patterns (allow-lists), never escaped-and-hoped-for: a package name is
[A-Za-z0-9.+_:~-], a destination path is absolute with no '..', and so on.

Build commands are assembled as argv lists (see backends/), so these patterns
are a second line of defence rather than the only one.
"""

import json
import re

from layersmith.core import catalog

MAX_SCRIPT = 64 * 1024
SCRIPT_KEYS = ("pre_build", "post_install", "entrypoint")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
REPOSITORY_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*){0,3}$")
VERSION_RE = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,6}$")
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_:~-]{0,127}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._@+/-]{1,255}$")
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SHELL_RE = re.compile(r"^/[a-z0-9/_-]{1,63}$")
MODE_RE = re.compile(r"^0[0-7]{3}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
REGISTRY_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?$")

# An environment variable that looks like a credential is refused: values in a
# Containerfile end up in the image and in `podman history`.
SENSITIVE_RE = re.compile(r"PASS(WORD|WD)?|TOKEN|SECRET|API_?KEY|PRIVATE_?KEY|CREDENTIAL|AUTH", re.I)
FORBIDDEN_PREFIXES = ("/proc/", "/sys/", "/dev/")


class InvalidSpec(ValueError):
    """Raised with a message intended to be shown to the user."""


def check(pattern, value, what):
    value = "" if value is None else str(value).strip()
    if not pattern.match(value):
        raise InvalidSpec(f"Invalid {what}: {value!r}" if value else f"Missing {what}")
    return value


def check_path(value, what="path"):
    path = check(PATH_RE, value, what)
    if ".." in path or path.endswith("/") or path.startswith(FORBIDDEN_PREFIXES):
        raise InvalidSpec(f"Invalid {what}: {path}")
    return path


def check_mode(value):
    return check(MODE_RE, value or "0644", "file mode")


def parse_reference(reference):
    """Split an image reference into registry/repository/tag/digest.

    Accepts 'alpine:3.22', 'docker.io/library/alpine:3.22' and
    'quay.io/org/name@sha256:...'. Rejects anything else rather than guessing.
    """
    value = check(re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}$"), reference, "image reference")
    digest = None
    if "@" in value:
        value, digest = value.split("@", 1)
        check(DIGEST_RE, digest, "image digest")
    tag = None
    head, _, maybe_tag = value.rpartition(":")
    if head and "/" not in maybe_tag:
        value, tag = head, check(TAG_RE, maybe_tag, "image tag")
    parts = value.split("/")
    registry = None
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry = check(REGISTRY_RE, parts[0], "registry host")
        parts = parts[1:]
    repository = "/".join(check(re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$"), p, "repository path") for p in parts)
    if not repository:
        raise InvalidSpec(f"Invalid image reference: {reference!r}")
    source = "/".join(filter(None, [registry, repository]))
    if tag:
        source = f"{source}:{tag}"
    if digest:
        source = f"{source}@{digest}"
    return {"registry": registry, "repository": repository, "tag": tag, "digest": digest, "source": source}


def next_patch(version):
    major, minor, patch = (int(p) for p in check(VERSION_RE, version, "version").split("."))
    return f"{major}.{minor}.{patch + 1}"


def version_key(version):
    return tuple(int(p) for p in version.split("."))


def image_ref(repository, version):
    return f"{check(REPOSITORY_RE, repository, 'repository')}:{check(VERSION_RE, version, 'version')}"


def export_filename(repository, version):
    return f"{repository.replace('/', '-')}-{version}.tar"


def validate_spec(spec):
    """Normalise and validate a wizard specification. Raises InvalidSpec."""
    spec = json.loads(json.dumps(spec or {}))  # reject non-JSON values early

    base = spec.get("base") or {}
    distribution = base.get("distribution")
    if distribution and distribution in catalog.DISTROS:
        version = str(base.get("version", ""))
        if version not in catalog.DISTROS[distribution]["versions"]:
            raise InvalidSpec(f"Unsupported {distribution} version: {version or '(none)'}")
        base.setdefault("source", catalog.default_source(distribution, version))
        base["family"] = catalog.family_of(distribution)
    elif distribution:
        raise InvalidSpec(f"Unknown distribution: {distribution}")
    if not base.get("source"):
        raise InvalidSpec("Choose a base image")
    base["source"] = parse_reference(base["source"])["source"]
    if base.get("family") not in catalog.FAMILIES:
        raise InvalidSpec("Choose the package family of the base image (rpm, deb or apk)")
    spec["base"] = base

    spec["architecture"] = spec.get("architecture") or "amd64"
    if spec["architecture"] not in catalog.ARCHITECTURES:
        raise InvalidSpec("Architecture must be amd64 or arm64")

    for key in ("packages", "extra_packages"):
        spec[key] = [check(PACKAGE_RE, p, "package name") for p in spec.get(key) or [] if str(p).strip()]

    requested_presets = spec.get("presets") or []
    spec["presets"] = []
    for name in requested_presets:
        if name not in catalog.PRESETS_BY_NAME:
            raise InvalidSpec(f"Unknown preset: {name}")
        spec["presets"].append(name)

    for tool in spec.setdefault("tools", []):
        check(NAME_RE, tool.get("name"), "tool name")
        check(SHA256_RE, tool.get("sha256"), "tool file (upload or fetch it first)")
        check_path(tool.setdefault("install_path", f"/usr/local/bin/{tool['name']}"), "tool install path")
        tool["mode"] = check_mode(tool.get("mode", "0755"))
        tool["architecture"] = tool.get("architecture") or spec["architecture"]
        if tool["architecture"] != spec["architecture"]:
            raise InvalidSpec(f"Tool {tool['name']} is {tool['architecture']}, the image is {spec['architecture']}")

    for item in spec.setdefault("files", []):
        check(SHA256_RE, item.get("sha256"), "file (upload it first)")
        check_path(item.get("destination"), "file destination")
        item["mode"] = check_mode(item.get("mode", "0644"))

    scripts = spec.setdefault("scripts", {})
    for key, body in list(scripts.items()):
        if key not in SCRIPT_KEYS:
            raise InvalidSpec(f"Unknown script: {key}")
        if len(body or "") > MAX_SCRIPT:
            raise InvalidSpec(f"Script {key} is larger than {MAX_SCRIPT // 1024} KiB")

    for item in spec.setdefault("env", []):
        check(ENV_NAME_RE, item.get("name"), "environment variable name")
        if SENSITIVE_RE.search(item["name"]):
            raise InvalidSpec(
                f"{item['name']} looks like a secret. Secrets must not be baked into an image; "
                "pass them at runtime instead (podman run --secret / -e)."
            )
        value = str(item.get("value", ""))
        if "\n" in value or len(value) > 1000:
            raise InvalidSpec(f"Value of {item['name']} must be a single line")
        item["value"] = value

    for key, value in (spec.setdefault("labels", {}) or {}).items():
        check(LABEL_RE, key, "label name")
        if "\n" in str(value):
            raise InvalidSpec("Label values must be single lines")

    user = spec.get("user") or {}
    if user.get("name"):
        check(USER_RE, user["name"], "user name")
        for key in ("uid", "gid"):
            if user.get(key) not in (None, "") and not (isinstance(user[key], int) and 1 <= user[key] <= 2**31 - 2):
                raise InvalidSpec(f"Invalid {key}")
        check(SHELL_RE, user.setdefault("shell", "/bin/sh"), "shell")
        spec["user"] = user

    if spec.get("workdir"):
        spec["workdir"] = check_path(spec["workdir"], "working directory")

    spec["tests"] = [t for t in (str(x).strip() for x in spec.get("tests") or []) if t][:50]
    if any("\n" in t or len(t) > 500 for t in spec["tests"]):
        raise InvalidSpec("Each test is a single command line")

    spec["tag_latest"] = bool(spec.get("tag_latest"))
    spec["enable_epel"] = bool(spec.get("enable_epel"))
    return spec


def resolve_packages(spec):
    """Capability names -> distro package names, plus warnings for gaps."""
    family = spec["base"]["family"]
    generic, tools = [], []
    for name in spec.get("presets") or []:
        preset = catalog.PRESETS_BY_NAME[name]
        generic += preset.get("packages", [])
        tools += preset.get("tools", [])
    generic += spec.get("packages") or []

    names, warnings = [], []
    for name in generic:
        mapped = catalog.PACKAGE_MAP.get(name, {}).get(family, name) if name in catalog.PACKAGE_MAP else name
        if mapped is None:
            warnings.append(
                f"{name} has no package in the {catalog.FAMILIES[family]} repositories; add it as a custom tool"
            )
            continue
        check(PACKAGE_RE, mapped, "package name")
        if mapped not in names:
            names.append(mapped)
    for name in spec.get("extra_packages") or []:
        if name not in names:
            names.append(name)

    configured = {t["name"] for t in spec.get("tools") or []}
    for tool in dict.fromkeys(tools):
        if tool not in configured:
            warnings.append(f"Preset tool {tool} is not configured yet: add it under Custom tools")
    return names, warnings
