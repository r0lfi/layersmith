"""Presentation helpers shared by the CLI and the TUI.

Everything the server returns is treated as untrusted text: project names,
descriptions, build logs and scanner findings can all contain bytes written
by whoever made the image. Before any of it reaches a terminal, escape
sequences and other control characters are removed, so a log line cannot
move the cursor, retitle the window or clear the screen.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# CSI (ESC [ ... final), OSC (ESC ] ... BEL or ST), other two-byte escapes,
# and C1 controls. Order matters: sequences first, stray ESC bytes after.
_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?"
    r"|\x1b[@-Z\\-_]"
    r"|[\x80-\x9f]"
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean(value: object, keep_newlines: bool = False) -> str:
    """Untrusted text made safe to print: no escapes, no control characters."""
    if value is None:
        return ""
    text = _ANSI_RE.sub("", str(value))
    if keep_newlines:
        text = text.replace("\r\n", "\n")
        lines = [_CONTROL_RE.sub("", line.replace("\t", "    ")) for line in text.split("\n")]
        return "\n".join(lines)
    return _CONTROL_RE.sub(" ", text.replace("\r\n", " ").replace("\n", " ").replace("\t", " "))


def short(value: object, width: int) -> str:
    text = clean(value)
    return text if len(text) <= width else text[: max(1, width - 3)] + "..."


# ------------------------------------------------------------- statuses
#
# Every status has a word, and colour is only ever added on top of the word:
# a terminal without colour (or NO_COLOR) loses nothing.

BUILD_ACTIVE = ("queued", "preparing", "pulling", "building", "testing", "exporting")
BUILD_FINISHED = ("ready", "failed", "cancelled")
SCAN_ACTIVE = ("queued", "exporting", "scanning")

#: Tone per status: ok / warn / bad / active / muted. The TUI maps tones to
#: theme colours; the CLI prints only the word.
BUILD_TONE = {
    "ready": "ok", "failed": "bad", "cancelled": "muted",
    **{state: "active" for state in BUILD_ACTIVE},
}

CHECK_LABEL = {"passed": "PASSED", "failed": "FAILED", "not_run": "NOT RUN"}
CHECK_TONE = {"passed": "ok", "failed": "bad", "not_run": "muted"}

SEVERITIES = ("critical", "high", "medium", "low", "unknown")
SEVERITY_TONE = {"critical": "bad", "high": "bad", "medium": "warn", "low": "muted", "unknown": "muted"}


def build_status(status: str | None) -> str:
    return (status or "unknown").upper()


def check_status(status: str | None) -> str:
    """Training check status. Anything unrecognised is UNKNOWN, never a pass."""
    return CHECK_LABEL.get(status or "", "UNKNOWN")


def security_status(scanner: dict | None, scans: list | None) -> tuple[str, str, str]:
    """(label, tone, explanation) for an image's security state.

    These cases are deliberately kept apart: an image is only described as
    having no findings when a scan actually completed without any. Not
    configured, unavailable, unknown, running and failed are all different
    from clean.
    """
    if scans is None:
        return "UNKNOWN", "muted", "The scan history could not be read."
    if scans:
        latest = scans[0]
        state = latest.get("state")
        if state in SCAN_ACTIVE:
            return f"SCAN {state.upper()}", "active", "A scan is in progress on the server."
        if state == "failed":
            return "SCAN FAILED", "bad", clean(latest.get("error") or "The scan did not finish.")
        if state == "completed":
            total = latest.get("total") or 0
            if total == 0:
                return ("NO FINDINGS", "ok",
                        "The scan completed without findings: the absence of a match, not a guarantee.")
            severity = latest.get("severity") or {}
            worst = next((level for level in SEVERITIES if severity.get(level)), None)
            tone = SEVERITY_TONE.get(worst or "", "warn")
            return f"{total} FINDINGS", tone, severity_summary(severity) or f"{total} findings"
        return "UNKNOWN", "muted", f"Unrecognised scan state {clean(state)!r}."
    if scanner is None:
        return "NOT SCANNED", "warn", "Not scanned, and the scanner status could not be read."
    if scanner.get("name") == "none":
        return ("NOT SCANNED", "warn",
                "No scanner is configured on the server, so nothing is known about this image's "
                "vulnerabilities - which is not the same as it being clean.")
    if not scanner.get("available"):
        return ("NOT SCANNED", "warn",
                f"The scanner is unavailable on the server: {clean(scanner.get('detail'))}")
    return "NOT SCANNED", "warn", "This image has not been scanned yet."


def severity_summary(severity: dict) -> str:
    return ", ".join(f"{severity[level]} {level}" for level in SEVERITIES if severity.get(level))


# ------------------------------------------------------------- formatting

def format_bytes(value: int | float | None) -> str:
    if value is None:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    number = float(value)
    unit = 0
    while number >= 1024 and unit < len(units) - 1:
        number /= 1024
        unit += 1
    return f"{number:.0f} {units[unit]}" if number >= 10 or unit == 0 else f"{number:.1f} {units[unit]}"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    return f"{minutes}m {round(seconds - minutes * 60)}s"


def format_when(value: str | None) -> str:
    if not value:
        return "-"
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return clean(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    diff = (datetime.now(timezone.utc) - moment).total_seconds()
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{int(diff // 60)} min ago"
    if diff < 86400:
        return f"{int(diff // 3600)} h ago"
    return moment.strftime("%Y-%m-%d")
