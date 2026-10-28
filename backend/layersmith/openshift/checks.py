"""Post-build checks of OpenShift Admin Toolkit images and how the build page shows them.

  image  as root inside the image: no kubeconfig, credentials or SSH server,
         Distrobox prerequisites, sudo, locale and time zone
  user   as an unprivileged uid without a passwd entry: every tool and its
         pinned version, bash completion, the helpers without a cluster,
         several kubeconfigs, the production warning and the prompt

Both run with networking disabled and every capability dropped; neither
fails the build - a failed check is a result the build page shows.
"""

from layersmith.training.checks import RESULT_PREFIX, parse_output  # one result-line format for all checks

TESTS = {
    "image": {"label": "Image contents", "runs_as": "root",
              "what": "No kubeconfig, token, registry credential or SSH key in any layer; no SSH server; "
                      "everything distrobox-init needs; sudo; locale and time zone."},
    "user": {"label": "Non-root use", "runs_as": "uid 1000, no passwd entry",
             "what": "Every tool runs and reports its pinned version; bash completion; the oc-* helpers without "
                     "a cluster; three kubeconfigs in ~/.kube/clusters; the production warning and prompt."},
}

__all__ = ["RESULT_PREFIX", "parse_output", "summarise", "TESTS"]


def summarise(build_status: str, recorded: dict | None) -> dict:
    """Rows for the build page and the tool table read by the user check."""
    results = (recorded or {}).get("results") or {}
    rows = []
    for test_id, test in TESTS.items():
        result = results.get(test_id)
        if result:
            status = result.get("status") if result.get("status") in ("passed", "failed") else "failed"
        else:
            status = "not_run"
        steps = (result or {}).get("steps") or []
        if not result:
            summary = "Running…" if build_status == "testing" else "Not run for this build."
        else:
            failed = [s for s in steps if s.get("status") == "failed"]
            summary = ("; ".join(f"{s['name']}: {str(s.get('detail', ''))[:200]}" for s in failed[:3]) if failed
                       else f"{len(steps)} steps passed")
        rows.append({"id": test_id, **test, "status": status, "summary": summary, "steps": steps})
    tools = ((results.get("user") or {}).get("info") or {}).get("tools") or []
    return {"rows": rows, "tools": [t for t in tools if isinstance(t, dict)]}
