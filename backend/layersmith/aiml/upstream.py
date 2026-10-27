"""Which tags a template's registry offers right now, next to the ones LayerSmith has verified.

Read-only and anonymous (the registries' public pull tokens). A tag found here
but not in the catalogue is shown as "available upstream, not verified": it
can be used through the Custom AI Container, where LayerSmith reads its CUDA
and framework versions from the image after the build instead of trusting
the tag's name.
"""

import json
import re
import urllib.request

from layersmith.aiml import catalog

TIMEOUT = 10
MAX_TAGS = 2000


def _get(url: str, token: str | None = None) -> dict:
    headers = {"Accept": "application/json", "User-Agent": "LayerSmith"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT) as response:
        return json.loads(response.read(4 * 1024 * 1024))


def _tags(registry: str, repository: str) -> list[str]:
    if registry == "nvcr.io":
        token = _get(f"https://nvcr.io/proxy_auth?scope=repository:{repository}:pull")["token"]
        return _get(f"https://nvcr.io/v2/{repository}/tags/list", token).get("tags") or []
    token = _get("https://auth.docker.io/token?service=registry.docker.io"
                 f"&scope=repository:{repository}:pull")["token"]
    tags, url = [], f"https://registry-1.docker.io/v2/{repository}/tags/list?n=1000"
    while url and len(tags) < MAX_TAGS:
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                                       "User-Agent": "LayerSmith"})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            tags += json.loads(response.read(4 * 1024 * 1024)).get("tags") or []
            link = response.headers.get("Link") or ""
        match = re.search(r"<([^>]+)>;\s*rel=\"next\"", link)
        url = f"https://registry-1.docker.io{match.group(1)}" if match else None
    return tags


def _sort_key(tag: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", tag)]


def available(template_id: str) -> dict:
    template = catalog.TEMPLATES.get(template_id)
    if template is None:
        raise catalog.AimlError(f"Unknown AI/ML template: {template_id}")
    upstream = template.get("upstream")
    verified = list(template["versions"])
    if not upstream:
        return {"template": template_id, "verified": verified, "upstream": [], "error": None}
    try:
        tags = _tags(upstream["registry"], upstream["repository"])
    except Exception as exc:  # offline build host, registry down: an answer, not an error
        return {"template": template_id, "verified": verified, "upstream": [],
                "error": f"Could not list {upstream['registry']}/{upstream['repository']}: {exc}"}
    pattern = re.compile(upstream["pattern"])
    matching = sorted((t for t in tags if pattern.match(t)), key=_sort_key, reverse=True)[:40]
    image = f"{upstream['registry']}/{upstream['repository']}"
    return {"template": template_id, "verified": verified, "error": None,
            "upstream": [{"tag": tag, "reference": f"{image}:{tag}", "verified": tag in verified}
                         for tag in matching]}
