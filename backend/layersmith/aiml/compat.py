"""GPU compatibility: driver, CUDA, image and GPU architecture, each with its evidence.

A row is `pass`, `warn`, `fail` or `unknown`, and says what it rests on:

  documented   the publisher's documentation (catalog.SOURCES)
  image        what the basic test read from this exact image after the build
               (the GPU architectures its kernels were compiled for)
  hardware     a GPU test reported back from a worker

Detecting a GPU is not evidence: an image can see an H200 and still have no
kernels for it, or need a newer driver than the host has.
"""

import re

from layersmith.aiml import catalog
from layersmith.aiml.catalog import AimlError, capability_tuple, driver_tuple


def _row(row_id, label, status, detail, evidence, source=None):
    return {"id": row_id, "label": label, "status": status, "detail": detail, "evidence": evidence,
            "source": source}


def driver_row(cuda: str | None, driver: str) -> dict:
    have = driver_tuple(driver)
    if not cuda:
        return _row("driver", "NVIDIA driver", "unknown",
                    "The image's CUDA version is not known yet; it is read from the image after the build.",
                    "image")
    major_minor = ".".join(cuda.split(".")[:2])
    major = cuda.split(".")[0]
    source = catalog.SOURCES["cuda-notes"]
    branch = catalog.CUDA_DRIVER_BRANCH.get(major_minor)
    if branch is not None:
        if have[0] >= branch:
            return _row("driver", "NVIDIA driver", "pass",
                        f"Driver {driver} is branch R{have[0]}; CUDA {major_minor} needs R{branch} or newer.",
                        "documented", source)
        floor = catalog.MINOR_COMPAT_FLOOR[major]
        if have >= driver_tuple(floor):
            return _row("driver", "NVIDIA driver", "warn",
                        f"Driver {driver} is older than R{branch}, which CUDA {major_minor} is built for. It runs "
                        f"only through CUDA minor version compatibility (>= {floor}): features new in "
                        f"{major_minor} and PTX JIT for newer code are not available.", "documented",
                        catalog.SOURCES["cuda-compat"])
        return _row("driver", "NVIDIA driver", "fail",
                    f"Driver {driver} is too old for CUDA {cuda}: R{branch} is needed (at least {floor} with "
                    "minor version compatibility).", "documented", source)
    if major == "12":
        need = catalog.CUDA_12_DRIVER.get(major_minor)
        if have[0] >= 580 or (need and have >= driver_tuple(need)):
            return _row("driver", "NVIDIA driver", "pass",
                        f"Driver {driver} is newer than every CUDA 12 release and runs CUDA {cuda} images "
                        "(drivers are backward compatible).", "documented", source)
        if have >= driver_tuple(catalog.MINOR_COMPAT_FLOOR["12"]):
            return _row("driver", "NVIDIA driver", "warn",
                        f"Driver {driver} runs CUDA {cuda} through minor version compatibility only.",
                        "documented", catalog.SOURCES["cuda-compat"])
        return _row("driver", "NVIDIA driver", "fail", f"Driver {driver} is too old for any CUDA 12 image.",
                    "documented", source)
    return _row("driver", "NVIDIA driver", "unknown",
                f"No documented driver requirement for CUDA {cuda} in this LayerSmith version.", "documented")


def _sm(capability: str) -> str:
    major, minor = capability_tuple(capability)
    return f"sm_{major}{minor}"


def _ptx_capability(arch: str) -> tuple[int, int] | None:
    """compute_90 -> (9, 0). Arch-specific PTX (compute_90a) cannot be JIT-compiled for other GPUs."""
    match = re.fullmatch(r"compute_(\d+)(\d)", arch)
    return (int(match.group(1)), int(match.group(2))) if match else None


def gpu_row(recipe: dict, gpu_id: str, compiled: list[str] | None, reported: dict | None) -> dict:
    gpu = catalog.GPUS[gpu_id]
    capability = capability_tuple(gpu["capability"])
    sm = _sm(gpu["capability"])
    label = f"{gpu['name']} ({sm})"
    row_id = f"gpu:{gpu_id}"

    for device in (reported or {}).get("devices", []) + (reported or {}).get("torch_devices", []):
        if str(device.get("capability")) == gpu["capability"] or gpu["name"].lower() in str(device.get("name", "")).lower():
            if reported.get("status") == "passed":
                return _row(row_id, label, "pass", f"GPU test passed on {device.get('name')} "
                            f"(reported {reported.get('finished') or ''}).", "hardware")
            return _row(row_id, label, "fail", "The GPU test reported from this GPU failed; see the test results.",
                        "hardware")

    if compiled:
        archs = set(compiled)
        native = sm in archs or sm.replace("sm_", "") in archs or gpu["capability"] in archs \
            or f"{sm}a" in archs
        ptx = [a for a in archs if _ptx_capability(a) and _ptx_capability(a) <= capability]
        if native:
            return _row(row_id, label, "pass", f"The image has kernels compiled for {sm}. Run the GPU test on "
                        "the worker to prove it on the hardware.", "image")
        if ptx:
            return _row(row_id, label, "warn", f"No {sm} kernels; {', '.join(sorted(ptx))} PTX will be "
                        "JIT-compiled by the driver on first use (slow start, not documented by the publisher).",
                        "image")
        return _row(row_id, label, "fail", f"No kernels for {sm} and no PTX that can be compiled for it "
                    f"(compiled for {' '.join(sorted(archs))}).", "image")

    minimum = recipe.get("min_capability")
    maximum = recipe.get("max_native_capability")
    sources = recipe.get("sources") or []
    if minimum and capability < capability_tuple(minimum):
        return _row(row_id, label, "fail", f"Needs compute capability {minimum} or newer.", "documented",
                    sources[0] if sources else None)
    if maximum and capability > capability_tuple(maximum):
        return _row(row_id, label, "warn", f"Built with CUDA {recipe.get('cuda')}, which has no {sm} code. "
                    "It can only run through PTX JIT, which the publisher does not document. Validate it "
                    "with the GPU test.", "documented", sources[0] if sources else None)
    if minimum:
        return _row(row_id, label, "pass", f"Documented: compute capability {minimum} and newer. The basic "
                    "test confirms the compiled architectures after the build.", "documented",
                    sources[0] if sources else None)
    return _row(row_id, label, "unknown", "Not documented for this base; known after the build's basic test.",
                "image")


def check(recipe: dict, driver: str | None = None, gpus: list[str] | None = None,
          basic: dict | None = None, reported: dict | None = None) -> list[dict]:
    """Compatibility rows for a recipe on the target environment.

    `basic` and `reported` are the info of this build's basic test and of a
    GPU test reported from a worker, when they exist; they replace what is
    only documented with what was observed.
    """
    driver = driver or catalog.TARGET["driver"]
    gpus = gpus or catalog.TARGET["gpus"]
    unknown = [g for g in gpus if g not in catalog.GPUS]
    if unknown:
        raise AimlError("Unknown GPU: " + ", ".join(unknown))
    basic = basic or {}
    cuda = recipe.get("cuda") or (basic.get("env") or {}).get("CUDA_VERSION")
    if cuda:
        cuda = ".".join(cuda.split(".")[:3])
    rows = [driver_row(cuda, driver)]
    if not cuda and basic:
        rows[0] = _row("driver", "NVIDIA driver", "fail", "The basic test found no CUDA in this image, so it cannot "
                       "use an NVIDIA GPU as it is.", "image")
    for key, label, version_key in (("torch_cuda", "PyTorch", "torch"), ("tf_cuda", "TensorFlow", "tensorflow")):
        built = basic.get(key)
        if not built:
            continue
        if cuda and built.split(".")[0] != cuda.split(".")[0]:
            rows.append(_row("framework-cuda", "Framework CUDA build", "warn",
                             f"{label} is built for CUDA {built}, the image's CUDA libraries are {cuda}: "
                             "different major versions.", "image"))
        else:
            same = not cuda or ".".join(built.split(".")[:2]) == ".".join(cuda.split(".")[:2])
            rows.append(_row("framework-cuda", "Framework CUDA build", "pass",
                             f"{label} {basic.get(version_key)} is built for CUDA {built}"
                             + ("." if same else f"; the image's CUDA libraries are {cuda} (same major version, "
                                                  "minor version compatible)."), "image"))
        break
    rows.append(_row("container", "Container runtime", "pass",
                     "The driver is not in the image: Podman injects it from the host through NVIDIA CDI. "
                     "The host needs the NVIDIA Container Toolkit and a generated CDI spec.", "documented",
                     "https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/cdi-support.html"))
    compiled = basic.get("compiled_archs") or None
    reported_info = None
    if reported:
        infos = [reported.get("info") or {}] + [n.get("info") or {} for n in reported.get("nodes") or []]
        reported_info = {"devices": [d for i in infos for d in i.get("devices", [])],
                         "torch_devices": [d for i in infos for d in i.get("torch_devices", [])],
                         "status": reported.get("status"), "finished": reported.get("finished")}
    for gpu_id in gpus:
        rows.append(gpu_row({**recipe, "cuda": cuda}, gpu_id, compiled, reported_info))
    return rows
