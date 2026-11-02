"""Post-build checks of opencode Shared Terminal images and how the build page shows them.

  image  as root inside the image: no sudo, SSH server or stored credentials,
         the offline settings, the host kit, and ensure-user
  user   as an unprivileged uid (not set up by ensure-user): every tool and its
         version, opencode's configuration, a local provider and code search
         with no network, and per-user data in $HOME

Both run with networking disabled and every capability dropped; neither
fails the build - a failed check is a result the build page shows.
"""

from layersmith.training.checks import RESULT_PREFIX, parse_output  # one result-line format for all checks

TESTS = {
    "image": {"label": "Image contents", "runs_as": "root",
              "what": "No sudo, no SSH server, no SSH keys or opencode credentials in any layer; opencode's offline "
                      "settings; the host kit; ensure-user adds a host user with its groups and follows changes."},
    "user": {"label": "Offline use", "runs_as": "uid 1000",
             "what": "Every tool reports its pinned version; opencode loads its configuration, lists a local "
                     "OpenAI-compatible model and searches code with networking disabled; data stays in $HOME."},
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
