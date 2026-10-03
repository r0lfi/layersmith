"""Render Textual SVG screenshots as PNG on an exact character grid.

    python client/scripts/svg_grid_png.py --font-dir DIR shot.svg [...]

Writes shot.grid.png next to each SVG. General SVG renderers lay text out
with font fallback, which distorts box-drawing and block characters; this
puts every character in its own cell, as a terminal does. DIR must contain
DejaVuSansMono.ttf and DejaVuSansMono-Bold.ttf. Needs Pillow (a development
tool; the client does not depend on it).
"""
import argparse
import html
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CELL_W, CELL_H, SIZE = 10, 20, 17


def render(svg_path: Path, png_path: Path, regular, bold) -> None:
    svg = svg_path.read_text()
    styles = {}
    for name, body in re.findall(r"\.([\w-]+)\s*\{([^}]*)\}", svg):
        fill = re.search(r"fill:\s*(#[0-9a-fA-F]{6})", body)
        styles[name] = {"fill": fill.group(1) if fill else None, "bold": "bold" in body}
    # Textual's SVG: char width 12.2, line height 24.4 by default; read from a rect
    cw, lh = 12.2, 24.4  # Textual's SVG cell size
    rects = re.findall(r'<rect fill="(#[0-9a-fA-F]{6})" x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"', svg)
    texts = re.findall(r'<text class="([^"]*)" x="([\d.]+)" y="([\d.]+)" textLength="[\d.]+"[^>]*>(.*?)</text>', svg)
    max_x = max(float(x) + float(w) for _, x, _, w, _ in rects)
    max_y = max(float(y) + float(h) for _, _, y, _, h in rects)
    width, height = int(max_x / cw + 0.5), int(max_y / lh + 0.5)
    image = Image.new("RGB", (width * CELL_W, height * CELL_H), "#0B1016")
    draw = ImageDraw.Draw(image)
    for fill, x, y, w, h in rects:
        c0, r0 = round(float(x) / cw), round(float(y) / lh)
        c1, r1 = round((float(x) + float(w)) / cw), round((float(y) + float(h)) / lh)
        if c1 <= c0 or r1 <= r0:
            continue
        draw.rectangle([c0 * CELL_W, r0 * CELL_H, c1 * CELL_W - 1, r1 * CELL_H - 1], fill=fill)
    for cls, x, y, text in texts:
        style = {}
        for name in cls.split():
            style.update({k: v for k, v in styles.get(name, {}).items() if v})
        col = round(float(x) / cw)
        row = round((float(y) - lh * 0.75) / lh)
        font = bold if style.get("bold") else regular
        for index, ch in enumerate(html.unescape(text)):
            if ch in (" ", "\xa0"):
                continue
            draw.text(((col + index) * CELL_W, row * CELL_H + 1), ch, font=font,
                      fill=style.get("fill") or "#E6EDF3")
    image.save(png_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--font-dir", required=True, type=Path)
    parser.add_argument("svg", nargs="+", type=Path)
    args = parser.parse_args()
    regular = ImageFont.truetype(str(args.font_dir / "DejaVuSansMono.ttf"), SIZE)
    bold = ImageFont.truetype(str(args.font_dir / "DejaVuSansMono-Bold.ttf"), SIZE)
    for source in args.svg:
        render(source, source.with_suffix(".grid.png"), regular, bold)


if __name__ == "__main__":
    main()
