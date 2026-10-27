"""What an AI/ML build hands over for the air-gapped side, next to the image archive.

  packages.json      installed Python packages and versions, read from the image
  requirements.lock  the same as name==version lines
  sbom.cdx.json      CycloneDX 1.5: the base image, NVIDIA libraries and every
                     Python distribution the basic test found. OS packages are
                     not in it; a configured scanner (Settings → Security)
                     adds a full SBOM to the bundle's security/ folder.
  README.md          verify, load and test commands for the offline host

The archive itself is the build's export; a Docker-format archive can be
produced next to it on request.
"""

from __future__ import annotations

import json
import uuid
from datetime import timezone
from urllib.parse import quote

from layersmith import config
from layersmith.aiml import render
from layersmith.aiml.checks import TESTS


def basic_info(checks: dict | None) -> dict:
    return (((checks or {}).get("results") or {}).get("basic") or {}).get("info") or {}


def packages(build, recipe: dict) -> dict:
    info = basic_info(build.checks)
    return {"image": build.image_ref, "image_digest": build.image_digest, "image_id": build.image_id,
            "base": recipe["base"]["display"], "base_digest": build.base_digest or recipe["base"]["digest"],
            "template": recipe["template"], "template_version": recipe["template_version"], "mode": recipe["mode"],
            "python": info.get("python"), "torch": info.get("torch"), "torch_cuda": info.get("torch_cuda"),
            "tensorflow": info.get("tensorflow"), "cuda": (info.get("env") or {}).get("CUDA_VERSION") or recipe["cuda"],
            "os": info.get("os"), "compiled_archs": info.get("compiled_archs"),
            "requested": recipe["install"], "packages": info.get("packages") or {},
            "source": "read from the image by the basic test" if info.get("packages") else
                      "not available: the basic test did not run or found no Python"}


def lock(build) -> str:
    found = basic_info(build.checks).get("packages") or {}
    return "".join(f"{name}=={version}\n" for name, version in sorted(found.items()))


def _purl_oci(image: str, digest: str | None, tag: str | None) -> str:
    name = image.rsplit("/", 1)[-1]
    qualifiers = f"repository_url={quote(image.rsplit('/', 1)[0] + '/' + name, safe='')}"
    if tag:
        qualifiers += f"&tag={quote(tag, safe='')}"
    return f"pkg:oci/{name}{'@' + quote(digest, safe='') if digest else ''}?{qualifiers}"


def sbom(build, recipe: dict) -> dict:
    """A CycloneDX 1.5 document for what LayerSmith itself observed in the image."""
    info = basic_info(build.checks)
    env = info.get("env") or {}
    created = build.finished_at.astimezone(timezone.utc).isoformat() if build.finished_at else None
    components = [{
        "type": "container", "bom-ref": "base-image", "name": recipe["base"]["image"],
        "version": recipe["base"]["tag"] or (build.base_digest or ""),
        "purl": _purl_oci(recipe["base"]["image"], build.base_digest or recipe["base"]["digest"], recipe["base"]["tag"]),
        "description": f"Base image of the {recipe['template_name']} template",
    }]
    for key, name in (("CUDA_VERSION", "CUDA"), ("CUDNN_VERSION", "cuDNN"), ("NCCL_VERSION", "NCCL"),
                      ("TRT_VERSION", "TensorRT")):
        if env.get(key):
            components.append({"type": "library", "bom-ref": f"nvidia-{name.lower()}", "name": f"NVIDIA {name}",
                               "version": env[key], "supplier": {"name": "NVIDIA"},
                               "properties": [{"name": "layersmith:source", "value": f"image environment {key}"}]})
    for name, version in sorted((info.get("packages") or {}).items()):
        components.append({"type": "library", "bom-ref": f"pypi:{name}", "name": name, "version": version,
                           "purl": f"pkg:pypi/{quote(name, safe='')}@{quote(version, safe='')}"})
    image_name = (build.image_ref or "image").rsplit(":", 1)
    return {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'layersmith:{build.id}:{build.image_digest}')}",
        "metadata": {
            "timestamp": created,
            "tools": {"components": [{"type": "application", "name": "LayerSmith", "version": config.VERSION}]},
            "component": {"type": "container", "bom-ref": "image", "name": image_name[0],
                          "version": image_name[1] if len(image_name) > 1 else "",
                          **({"hashes": [{"alg": "SHA-256", "content": build.image_digest.split(":", 1)[1]}]}
                             if build.image_digest and build.image_digest.startswith("sha256:") else {})},
            "properties": [{"name": "layersmith:scope",
                            "value": "Base image, NVIDIA library versions and Python distributions read from the "
                                     "image. OS packages are not listed: configure a scanner for a full SBOM."}],
        },
        "components": components,
        "dependencies": [{"ref": "image", "dependsOn": [c["bom-ref"] for c in components]}],
    }


def readme(build, recipe: dict, archive: str, sha256: str | None) -> str:
    image = build.image_ref
    name = archive.rsplit("/", 1)[-1]
    run = render.run_command(recipe, image)
    tests = "\n".join(f"- **{t['label']}** ({t['where']}): {t['what']}" for t in TESTS.values())
    return f"""# {image}

{recipe['template_name']} {recipe['version']} — {'unchanged upstream image (import)' if recipe['mode'] == 'import'
                                              else 'custom image built by LayerSmith'}.
Base: `{recipe['base']['display']}` pinned at `{build.base_digest or recipe['base']['digest']}`.
CUDA {recipe['cuda'] or (basic_info(build.checks).get('env') or {}).get('CUDA_VERSION') or 'not found in the image'}.
Built by LayerSmith {config.VERSION}.

Everything the image needs is inside it: nothing is downloaded when the
container starts. The NVIDIA driver is not in the image; it comes from the
GPU host through NVIDIA CDI.

## Verify and load on the offline host

```bash
cd image                           # in the air-gap bundle; next to the download otherwise
sha256sum -c {name}.sha256
podman load -i {name}
podman images
```

Expected SHA-256: `{sha256 or 'see SHA256SUMS'}`. Both `podman load` and
`docker load` read the archive.

## Run with only the GPUs you choose

```bash
nvidia-ctk cdi list                # nvidia.com/gpu=GPU-<uuid> names must be listed
{run['podman']}
```

{run['note']}
Never use `--privileged` and never `--device nvidia.com/gpu=all` on shared
hosts. With SELinux enforcing, an administrator enables the
`container_use_devices` boolean once instead of disabling SELinux.

## Tests

{tests}

GPU test on a worker, with the script in this folder (works for unchanged images too):

```bash
{render.gpu_test_command(recipe, image)}
```

Under Slurm, use the batch script LayerSmith generates for this build
(build page → Slurm). Paste the output back on the build page to record it.

## Files

- `packages.json`, `requirements.lock` — Python packages and versions in the image
- `sbom.cdx.json` — CycloneDX SBOM of what LayerSmith read from the image
- `layersmith-aiml-check` — the basic/GPU test script
- `../source/Containerfile` — the exact file this image was built from
"""
