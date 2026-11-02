#!/usr/bin/env python3
"""Generate all Mission Control icons from one drawn mark.

The mark: a planet with a tilted orbit ring and a satellite on the ring,
mission-patch style. Outputs:
  assets/icon-1024.png       app icon (badge background)
  assets/app.icns            macOS app icon (built with iconutil, macOS only)
  assets/app.ico             Windows app icon
  assets/tray-{idle,thinking,doing}.png   menu bar / tray states, 44px
  web/logo.png               favicon / in-app logo, 64px
"""
import math
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ASSETS.mkdir(exist_ok=True)

# Palette
SPACE = (23, 24, 38, 255)        # badge background
PLANET = (122, 162, 255, 255)    # planet blue
RING = (240, 244, 255, 255)      # orbit white
SAT = (94, 234, 212, 255)        # satellite teal

STATE_COLORS = {
    "idle": ((150, 152, 160, 255), (150, 152, 160, 255), (150, 152, 160, 255)),
    "thinking": ((250, 173, 60, 255), (250, 173, 60, 255), (250, 173, 60, 255)),
    "doing": ((60, 200, 120, 255), (60, 200, 120, 255), (60, 200, 120, 255)),
}


def draw_mark(size: int, planet, ring, sat, scale: float = 1.0) -> Image.Image:
    """The bare mark on transparent background."""
    ss = 4  # supersample
    s = size * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = cy = s / 2
    pr = s * 0.30 * scale            # planet radius
    stroke = max(int(s * 0.055 * scale), ss)

    # Orbit ring: ellipse split into back/front arcs ALONG ITS OWN MAJOR
    # AXIS before rotating, so the halves rejoin seamlessly at the axis
    # ends instead of showing a horizontal cut.
    ring_w, ring_h = s * 0.92 * scale, s * 0.34 * scale
    box = [cx - ring_w / 2, cy - ring_h / 2, cx + ring_w / 2, cy + ring_h / 2]

    def _arc_half(top: bool) -> Image.Image:
        layer = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        ld.ellipse(box, outline=ring, width=stroke)
        if top:
            layer.paste((0, 0, 0, 0), (0, int(cy), s, s))
        else:
            layer.paste((0, 0, 0, 0), (0, 0, s, int(cy)))
        return layer.rotate(-24, resample=Image.BICUBIC, center=(cx, cy))

    img.alpha_composite(_arc_half(top=True))     # behind the planet
    d.ellipse([cx - pr, cy - pr, cx + pr, cy + pr], fill=planet)
    img.alpha_composite(_arc_half(top=False))    # in front of the planet

    # Satellite exactly on the ring path, upper right where the ring is
    # clear of the planet: parametric point rotated with the ring.
    ang = math.radians(-24)
    t = math.radians(-38)
    ex = ring_w / 2 * math.cos(t)
    ey = ring_h / 2 * math.sin(t)
    sx = cx + ex * math.cos(ang) - ey * math.sin(ang)
    sy = cy + ex * math.sin(ang) + ey * math.cos(ang)
    sr = s * 0.075 * scale
    d.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], fill=sat)

    return img.resize((size, size), Image.LANCZOS)


def rounded_badge(size: int) -> Image.Image:
    """App icon: mark on a rounded-square space background."""
    ss = 4
    s = size * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = s * 0.045
    d.rounded_rectangle([m, m, s - m, s - m], radius=s * 0.22, fill=SPACE)
    img = img.resize((size, size), Image.LANCZOS)
    mark = draw_mark(size, PLANET, RING, SAT, scale=0.78)
    img.alpha_composite(mark)
    return img


def main():
    badge = rounded_badge(1024)
    badge.save(ASSETS / "icon-1024.png")

    # Windows .ico
    badge.save(ASSETS / "app.ico",
               sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

    # web logo / favicon
    badge.resize((64, 64), Image.LANCZOS).save(ROOT / "web" / "logo.png")

    # tray state icons (bare mark, color-coded, 44px for retina menu bar)
    for state, (p, r, sat) in STATE_COLORS.items():
        draw_mark(44, p, r, sat).save(ASSETS / f"tray-{state}.png")

    # macOS .icns
    if sys.platform == "darwin":
        with tempfile.TemporaryDirectory() as td:
            iconset = Path(td) / "app.iconset"
            iconset.mkdir()
            for pts in (16, 32, 128, 256, 512):
                badge.resize((pts, pts), Image.LANCZOS).save(iconset / f"icon_{pts}x{pts}.png")
                badge.resize((pts * 2, pts * 2), Image.LANCZOS).save(
                    iconset / f"icon_{pts}x{pts}@2x.png")
            subprocess.run(["iconutil", "-c", "icns", str(iconset),
                            "-o", str(ASSETS / "app.icns")], check=True)
    print("icons written to", ASSETS)


if __name__ == "__main__":
    main()
