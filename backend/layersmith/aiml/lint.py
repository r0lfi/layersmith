"""Checks for a hand-written Containerfile of an AI/ML image (Advanced mode).

LayerSmith builds what it is given in Advanced mode, so this is the place to
stop what must never reach an exported image - credentials - and to point out
what makes an image unfit for an air-gapped GPU cluster. Errors refuse the
save; warnings are shown.
"""

import re

from layersmith.core.spec import SENSITIVE_RE

INSTRUCTION_RE = re.compile(r"^\s*([A-Za-z]+)\s+(.*)$")
URL_CREDENTIALS_RE = re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@", re.I)
TOKEN_RE = re.compile(r"\b(?:hf_[A-Za-z0-9]{30,}|ghp_[A-Za-z0-9]{30,}|nvapi-[A-Za-z0-9_-]{30,}|"
                      r"AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{32,})\b")
KNOWN = {"FROM", "RUN", "CMD", "LABEL", "EXPOSE", "ENV", "ADD", "COPY", "ENTRYPOINT", "VOLUME", "USER", "WORKDIR",
         "ARG", "ONBUILD", "STOPSIGNAL", "HEALTHCHECK", "SHELL", "MAINTAINER"}


def _logical_lines(text: str):
    """Instructions with their continuation lines joined, and the line each starts on."""
    current, start = "", 0
    for number, raw in enumerate((text or "").splitlines(), 1):
        line = raw.rstrip()
        if not current and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if not current:
            start = number
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        yield start, current + line
        current = ""
    if current:
        yield start, current


def lint(text: str) -> dict:
    errors, warnings = [], []
    if len(text or "") > 256 * 1024:
        return {"errors": ["The Containerfile is larger than 256 KiB"], "warnings": []}
    froms = 0
    for number, line in _logical_lines(text):
        match = INSTRUCTION_RE.match(line)
        if not match:
            errors.append(f"line {number}: not a Containerfile instruction")
            continue
        instruction, rest = match.group(1).upper(), match.group(2)
        where = f"line {number}"
        if instruction not in KNOWN:
            errors.append(f"{where}: unknown instruction {instruction}")
        if URL_CREDENTIALS_RE.search(rest) or TOKEN_RE.search(rest):
            errors.append(f"{where}: contains a credential or access token; it would be stored in the image and "
                          "its history. Pass secrets at run time (podman run --secret), never at build time.")
        if instruction in ("ENV", "ARG"):
            names = re.findall(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=", rest) or [rest.split()[0] if rest.split() else ""]
            for name in names:
                if SENSITIVE_RE.search(name):
                    errors.append(f"{where}: {instruction} {name} looks like a secret and would be baked into the "
                                  "image; pass it at run time instead")
        if instruction == "FROM":
            froms += 1
            image = rest.split()[0] if rest.split() else ""
            if image.startswith("--platform"):
                image = rest.split()[1] if len(rest.split()) > 1 else ""
            if "@sha256:" not in image and image.lower() != "scratch" and "$" not in image:
                warnings.append(f"{where}: {image} is not pinned by digest; LayerSmith pins it when it pulls, but "
                                "the file itself will follow the tag")
            if image.endswith(":latest") or (":" not in image.split("/")[-1] and "@" not in image):
                warnings.append(f"{where}: {image} uses an implicit or 'latest' tag")
        if instruction == "ADD" and re.search(r"\bhttps?://", rest):
            warnings.append(f"{where}: ADD from a URL is not verified by checksum; download in RUN and check "
                            "sha256sum, or COPY a file you uploaded")
        if instruction == "RUN":
            if re.search(r"(curl|wget)[^|;&]*\|\s*(ba|z)?sh\b", rest):
                warnings.append(f"{where}: pipes a download straight into a shell")
            if re.search(r"--privileged|--security-opt[ =]?label=disable|setenforce\s+0", rest):
                errors.append(f"{where}: disables isolation inside the build; not allowed")
            if re.search(r"\bpip3?\b.*\binstall\b.*(--trusted-host|--index-url\s+http://|-i\s+http://)", rest):
                warnings.append(f"{where}: installs Python packages without TLS verification")
            if re.search(r"\bpip3?\b.*\binstall\b.*\b(torch|torchvision|torchaudio|tensorrt|flash-attn)\b", rest):
                warnings.append(f"{where}: reinstalls a framework package; on an NVIDIA base this replaces the "
                                "build made for its CUDA version")
            if re.search(r"chmod\s+(-R\s+)?777\b", rest):
                warnings.append(f"{where}: makes files world-writable")
    if froms == 0:
        errors.append("The Containerfile has no FROM line")
    return {"errors": errors, "warnings": warnings}
