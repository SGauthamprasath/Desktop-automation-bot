"""
sprite_clean.py
Turns the raw AI-generated sprite sheet (magenta background, drawn-in grid lines,
2272x1888, ~13k colors per cell) into a clean, game-ready sheet:
  - transparent background
  - uniform 128x128 cells, 6 columns x 5 rows
  - consistent feet baseline within each row
  - limited palette + hard alpha edges (pixel-art look)
  - a JSON file describing rows and frame counts
"""
import json
import numpy as np
from PIL import Image

SRC = "/mnt/user-data/uploads/Gemini_Generated_Image_dhqe7xdhqe7xdhqe.png"
OUT_DIR = "/mnt/user-data/outputs/companion-assets"
CELL_OUT = 128          # final cell size in px
PALETTE_COLORS = 32     # palette size after quantizing
INSET = 3               # px trimmed inside each cell so grid lines never leak in

# Interior origin of each cell, measured from the grid lines in the source image.
XS = [7, 385, 764, 1142, 1520, 1898]
YS = [4, 382, 762, 1142, 1521]
CELL_SRC = 367

# Row layout as generated: name and how many real frames each row has.
ROWS = [("idle", 6), ("walk", 6), ("thinking", 6), ("alert", 4), ("talking", 4)]

src = Image.open(SRC).convert("RGB")
arr = np.array(src).astype(int)


def background_mask(rgb):
    """True where a pixel is magenta or a magenta blend (anti-aliased edge)."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    return (g < 100) & (r > 90) & (b > 90) & ((r + b) > 2 * g + 150)


# 1) Cut every cell out with a transparent background.
cells = {}
for ri, (name, n) in enumerate(ROWS):
    for ci in range(n):
        x0 = XS[ci] + INSET
        y0 = YS[ri] + INSET
        size = CELL_SRC - 2 * INSET
        crop = arr[y0:y0 + size, x0:x0 + size]
        alpha = np.where(background_mask(crop), 0, 255).astype(np.uint8)
        rgba = np.dstack([crop.astype(np.uint8), alpha])
        cells[(ri, ci)] = rgba

SIZE = CELL_SRC - 2 * INSET


def feet_y(rgba):
    ys = np.where(rgba[..., 3] > 0)[0]
    return ys.max()


# 2) Per-row baseline alignment: shift every frame in a row so the row's median
#    feet position lands on the same line. The relative motion inside a row
#    (e.g. the alert hop) is preserved.
TARGET_BASELINE = SIZE - 8
aligned = {}
for ri, (name, n) in enumerate(ROWS):
    med = int(np.median([feet_y(cells[(ri, ci)]) for ci in range(n)]))
    shift = TARGET_BASELINE - med
    for ci in range(n):
        canvas = np.zeros_like(cells[(ri, ci)])
        src_cell = cells[(ri, ci)]
        if shift >= 0:
            canvas[shift:, :] = src_cell[:SIZE - shift, :]
        else:
            canvas[:SIZE + shift, :] = src_cell[-shift:, :]
        aligned[(ri, ci)] = canvas
    print(f"row {ri + 1} ({name}): median feet y={med}, shift={shift:+d}")

# 3) Downscale every cell to CELL_OUT x CELL_OUT.
small = {}
for key, rgba in aligned.items():
    img = Image.fromarray(rgba, "RGBA").resize((CELL_OUT, CELL_OUT), Image.LANCZOS)
    small[key] = np.array(img)

# 4) Assemble the sheet (6 columns x 5 rows, empty cells stay transparent).
COLS = 6
sheet = np.zeros((len(ROWS) * CELL_OUT, COLS * CELL_OUT, 4), dtype=np.uint8)
for (ri, ci), cell in small.items():
    sheet[ri * CELL_OUT:(ri + 1) * CELL_OUT, ci * CELL_OUT:(ci + 1) * CELL_OUT] = cell

# 5) Hard alpha edges + palette quantization = crisp pixel-art look.
alpha = np.where(sheet[..., 3] > 128, 255, 0).astype(np.uint8)
opaque = alpha == 255


def palette_from(pixels, k):
    """Median-cut palette (k colors) from an (N,3) array of RGB pixels."""
    q = Image.fromarray(pixels.reshape(-1, 1, 3).astype(np.uint8), "RGB").quantize(
        colors=k, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    return np.unique(np.array(q.convert("RGB")).reshape(-1, 3), axis=0)


# A single global palette merges rare colors away (the alert row's orange is only a
# few pixels per frame). So: shared palette for the whole sheet, PLUS a small
# dedicated palette built only from the alert row, then map every pixel to the
# nearest color in the combined palette.
rgb = sheet[..., :3]
shared = palette_from(rgb[opaque], PALETTE_COLORS - 8)
alert_rows = slice(3 * CELL_OUT, 4 * CELL_OUT)
row_rgb = rgb[alert_rows].astype(int)
warm = opaque[alert_rows] & (row_rgb[..., 0] > 120) & (row_rgb[..., 0] - row_rgb[..., 2] > 60)
print("warm pixels in alert row:", int(warm.sum()))
alert_pal = palette_from(rgb[alert_rows][warm], 5)
palette = np.unique(np.vstack([shared, alert_pal]), axis=0)
print("palette size:", len(palette))

flat = rgb.reshape(-1, 3).astype(int)
dist = ((flat[:, None, :] - palette[None, :, :].astype(int)) ** 2).sum(axis=2)
mapped = palette[dist.argmin(axis=1)].reshape(rgb.shape).astype(np.uint8)

final = np.dstack([mapped, alpha])
final[alpha == 0, :3] = 0

import os
os.makedirs(OUT_DIR, exist_ok=True)
Image.fromarray(final, "RGBA").save(f"{OUT_DIR}/bot_spritesheet.png")

meta = {
    "image": "bot_spritesheet.png",
    "frameWidth": CELL_OUT,
    "frameHeight": CELL_OUT,
    "columns": COLS,
    "states": {name: {"row": ri, "frames": n} for ri, (name, n) in enumerate(ROWS)},
}
with open(f"{OUT_DIR}/bot_spritesheet.json", "w") as f:
    json.dump(meta, f, indent=2)

# 6) Preview on a dark background so edges/halo problems are easy to spot.
bg = Image.new("RGBA", (final.shape[1], final.shape[0]), (32, 36, 48, 255))
bg.alpha_composite(Image.fromarray(final, "RGBA"))
bg.resize((bg.width * 2, bg.height * 2), Image.NEAREST).save("/home/claude/preview_dark.png")
print("done", final.shape)
