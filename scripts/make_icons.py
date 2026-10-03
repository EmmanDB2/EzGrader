#!/usr/bin/env python3
"""Draw EzGrader's app icon (the favicon's checkmark) into assets/.

Writes EzGrader.png (1024 px), EzGrader.ico (Windows), and on macOS EzGrader.icns.
Only needed when the icon changes; the results are committed. Needs Pillow:
    pip install pillow && python scripts/make_icons.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent.parent / "assets"
INDIGO = (79, 70, 229, 255)
WHITE = (255, 255, 255, 255)


def draw(size: int = 1024) -> Image.Image:
    scale = 4  # draw big, then shrink, for smooth edges
    big = size * scale
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    margin = big * 100 // 1024  # macOS icons sit inside a margin
    side = big - 2 * margin
    d.rounded_rectangle([margin, margin, margin + side, margin + side], radius=side * 0.225, fill=INDIGO)

    # The favicon's check: M9 16.5 L13.5 21 L23 11.5 in a 32-unit box, stroke 3.
    unit = side / 32
    points = [(margin + x * unit, margin + y * unit) for x, y in ((9, 16.5), (13.5, 21), (23, 11.5))]
    width = round(3 * unit)
    d.line(points, fill=WHITE, width=width, joint="curve")
    for x, y in (points[0], points[-1]):
        r = width / 2
        d.ellipse([x - r, y - r, x + r, y + r], fill=WHITE)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    icon = draw()
    icon.save(ASSETS / "EzGrader.png")
    icon.save(ASSETS / "EzGrader.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    if sys.platform == "darwin" and shutil.which("iconutil"):
        with tempfile.TemporaryDirectory() as tmp:
            iconset = Path(tmp) / "EzGrader.iconset"
            iconset.mkdir()
            for base in (16, 32, 128, 256, 512):
                icon.resize((base, base), Image.LANCZOS).save(iconset / f"icon_{base}x{base}.png")
                icon.resize((base * 2, base * 2), Image.LANCZOS).save(iconset / f"icon_{base}x{base}@2x.png")
            subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(ASSETS / "EzGrader.icns")], check=True)
    print("Wrote", ", ".join(sorted(p.name for p in ASSETS.iterdir())))


if __name__ == "__main__":
    main()
