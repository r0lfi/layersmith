"""The LayerSmith terminal theme: every colour is defined here, once.

Dark blue-grey surfaces with blue accents. Saturated blue marks what is
active or focused; it is never the background of a large area. The values
are truecolor; Textual and Rich reduce them for terminals with 256 or 16
colours and drop them under NO_COLOR. The surfaces are deliberately close to
grey, so a 256-colour terminal rounds them to dark greys rather than to the
pure blue (#00005f) a saturated navy would become. Every status is also a
word, so nothing depends on colour alone.

The stylesheet (layersmith.tcss) uses these through Textual's theme
variables, plus the `ls-*` variables below.
"""

from __future__ import annotations

import re

from rich.text import Text
from textual.theme import Theme

from layersmith_client.text import clean

PALETTE = {
    "background": "#0B1016",   # app background
    "panel": "#101923",        # panels
    "raised": "#152231",       # raised surfaces, table headers
    "border": "#2C4156",       # ordinary frames
    "text": "#E6EDF3",         # primary text
    "muted": "#94A3B8",        # labels, metadata
    "blue": "#4DA3FF",         # accent: focus, titles, the active choice
    "cyan": "#63C7E6",         # information accent
    "selected": "#1D3D63",     # selected row, active menu entry
    "hover": "#182B3F",        # hover, unfocused selection
    "success": "#6BCB8B",
    "warning": "#E9B75A",
    "error": "#F17878",
}

#: Variables for the stylesheet, beyond Textual's own. Textual reads these
#: through LayerSmithApp.get_theme_variable_defaults().
VARIABLES = {
    "ls-border": PALETTE["border"],
    "ls-muted": PALETTE["muted"],
    "ls-raised": PALETTE["raised"],
    "ls-selected": PALETTE["selected"],
    "ls-hover": PALETTE["hover"],
    "ls-cyan": PALETTE["cyan"],
}

LAYERSMITH_THEME = Theme(
    name="layersmith",
    primary=PALETTE["blue"],
    secondary=PALETTE["cyan"],
    accent=PALETTE["cyan"],
    foreground=PALETTE["text"],
    background=PALETTE["background"],
    surface=PALETTE["panel"],
    panel=PALETTE["raised"],
    success=PALETTE["success"],
    warning=PALETTE["warning"],
    error=PALETTE["error"],
    dark=True,
    variables={
        # Textual's own variables, so no stock widget brings its own blue.
        "text-muted": PALETTE["muted"],
        "border": PALETTE["blue"],
        "border-blurred": PALETTE["border"],
        "block-cursor-background": PALETTE["selected"],
        "block-cursor-foreground": PALETTE["text"],
        "block-cursor-text-style": "none",
        "block-cursor-blurred-background": PALETTE["hover"],
        "block-cursor-blurred-foreground": PALETTE["text"],
        "block-cursor-blurred-text-style": "none",
        "block-hover-background": PALETTE["hover"],
        "footer-background": PALETTE["background"],
        "footer-foreground": PALETTE["muted"],
        "footer-key-foreground": PALETTE["blue"],
        "footer-key-background": "transparent",
        "footer-description-foreground": PALETTE["muted"],
        "footer-description-background": "transparent",
        "footer-item-background": "transparent",
        "scrollbar": PALETTE["border"],
        "scrollbar-hover": PALETTE["muted"],
        "scrollbar-active": PALETTE["blue"],
        "scrollbar-background": PALETTE["panel"],
        "scrollbar-background-hover": PALETTE["panel"],
        "scrollbar-background-active": PALETTE["panel"],
        "scrollbar-corner-color": PALETTE["panel"],
        "input-cursor-background": PALETTE["text"],
        "input-cursor-foreground": PALETTE["background"],
        "input-selection-background": PALETTE["selected"],
        "button-foreground": PALETTE["text"],
        "button-color-foreground": PALETTE["text"],
        "button-focus-text-style": "bold",
        "markdown-h1-color": PALETTE["blue"],
    },
)

#: Named text styles for the Python side, from the same palette.
STYLE = {
    "muted": PALETTE["muted"],
    "accent": PALETTE["cyan"],
    "error": f"bold {PALETTE['error']}",
    "warning": f"bold {PALETTE['warning']}",
    "success": f"bold {PALETTE['success']}",
}

#: tone (see text.py) -> palette colour
TONE_COLOUR = {"ok": "success", "warn": "warning", "bad": "error", "active": "cyan", "muted": "muted"}


def tone_style(tone: str, bold: bool = True) -> str:
    colour = PALETTE[TONE_COLOUR.get(tone, "muted")]
    return f"bold {colour}" if bold else colour


def badge(app, label: str, tone: str) -> Text:
    """A status word in its colour. The word carries the meaning."""
    return Text(label, style=tone_style(tone))


def muted(value: object) -> Text:
    return Text(clean(value), style=PALETTE["muted"])


def safe(value: object, style: str = "") -> Text:
    """Untrusted server text as a Rich Text: no markup, no escapes."""
    return Text(clean(value), style=style)


def safe_block(value: object, style: str = "") -> Text:
    return Text(clean(value, keep_newlines=True), style=style)


#: Log levels the client can recognise. Only these are marked; a line
#: without one is shown as it is.
LOG_LEVELS = (
    ("ERROR", PALETTE["error"]), ("FAILED", PALETTE["error"]),
    ("WARNING", PALETTE["warning"]), ("WARN", PALETTE["warning"]),
    ("NOTE", PALETTE["cyan"]), ("INFO", PALETTE["cyan"]),
)


def log_line(line: str) -> Text:
    """One log line: level words coloured, the message in normal text.

    LayerSmith's own lines start with `ERROR:`, `WARNING:` or `NOTE:`, and
    a finished build ends with `Image ready:`; tools inside a build print
    their own levels. Nothing else is guessed.
    """
    text = Text(clean(line))
    plain = text.plain
    stripped = plain.lstrip()
    offset = len(plain) - len(stripped)
    if stripped.startswith("Image ready:"):
        text.stylize(f"bold {PALETTE['success']}", offset, offset + len("Image ready:"))
        return text
    if stripped.startswith("STEP "):
        end = stripped.find(":")
        text.stylize(PALETTE["blue"], offset, offset + (end if end > 0 else 4))
        return text
    for word, colour in LOG_LEVELS:
        if re.match(rf"\[?{word}\b", stripped):
            start = offset + (1 if stripped.startswith("[") else 0)
            text.stylize(f"bold {colour}", start, start + len(word))
            return text
    return text
