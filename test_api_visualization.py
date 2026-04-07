"""
Test script for StreetViewAPI: exercises navigation, scrolling, and zoom,
then produces a single composite image visualizing every step.
"""

import os
import sys
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO

sys.path.insert(0, os.path.dirname(__file__))
from apps.geoguessr_wrapper import StreetViewAPI, ImageResult

# ── Monkey-patch capture_view to fix image paths ──────────────────────
_orig_capture = StreetViewAPI.capture_view

def _patched_capture(self):
    """Prefix image_path with mapcrunch_images/ so files are found."""
    if not self._pano_id:
        raise RuntimeError("No panorama loaded")
    row = self._query_one(
        "SELECT image_path FROM captures "
        "WHERE pano_id = ? AND heading = ? AND pitch = ? AND zoom = ? "
        "AND image_path IS NOT NULL LIMIT 1",
        (self._pano_id, self._heading, self._pitch, self._zoom),
    )
    if not row:
        raise RuntimeError(f"No captured image for pano={self._pano_id}")
    path = row["image_path"]
    if not os.path.exists(path):
        path = os.path.join("mapcrunch_images", path)
    with open(path, "rb") as f:
        image_bytes = f.read()
    return ImageResult(image_bytes=image_bytes, mime_type="image/jpeg")

StreetViewAPI.capture_view = _patched_capture

# ── Setup ──────────────────────────────────────────────────────────────
api = StreetViewAPI(db_path="mapcrunch.db")
api._load_scenario({"lat": 78.22295351095083, "lng": 15.627656919871827, "heading": 0, "pitch": 0, "zoom": 1})

OUTPUT_DIR = "test_api_output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

steps = []  # list of (label, PIL.Image)


def run_step(label: str, action_fn):
    """Run an API action, save the image, and record the step."""
    result = action_fn()
    if isinstance(result, ImageResult):
        img = Image.open(BytesIO(result.image_bytes))
    elif isinstance(result, dict) and "status" in result:
        # move actions don't return images; capture manually after
        capture = api.capture_view()
        img = Image.open(BytesIO(capture.image_bytes))
    else:
        return
    fname = f"{len(steps):02d}_{label.replace(' ', '_')}.jpg"
    img.save(os.path.join(OUTPUT_DIR, fname))
    steps.append((label, img))
    state = f"pano={api._pano_id[:12]}… h={api._heading} p={api._pitch} z={api._zoom}"
    print(f"  Step {len(steps):2d}: {label:25s}  |  {state}")


# ── Execute steps ──────────────────────────────────────────────────────
print("Running API test steps…\n")

# 1. Initial capture
run_step("initial capture", api.capture_view)

# 2-5. Scroll around
run_step("scroll right", api.scroll_right)
run_step("scroll right", api.scroll_right)
run_step("scroll left", api.scroll_left)
run_step("scroll left", api.scroll_left)

# 6-7. Scroll up/down
run_step("scroll up", api.scroll_up)
run_step("scroll down", api.scroll_down)

# 8-9. Zoom in/out
run_step("zoom in", api.zoom_in)
# run_step("zoom out", api.zoom_out)

# 10. Check direction
direction = api.check_direction()
print(f"  Direction check: {direction['description']}")

# 11. Move SE (heading ~118° link exists)
run_step("move southeast", api.move_southeast)

run_step("zoom out", api.zoom_out)

# 12. Capture at new location
run_step("capture after move", api.capture_view)

# 13-14. Scroll at new location
run_step("scroll right (new loc)", api.scroll_right)
run_step("scroll left (new loc)", api.scroll_left)

# 15. Move SW (heading ~298° → NW, or ~152° → SE)
run_step("move southeast 2", api.move_southeast)

# 16. Final capture
run_step("final capture", api.capture_view)

print(f"\n✓ {len(steps)} steps completed. Building composite image…")

# ── Build composite ────────────────────────────────────────────────────
COLS = 4
THUMB_W, THUMB_H = 480, 320
PAD = 12
LABEL_H = 36
ROWS = (len(steps) + COLS - 1) // COLS

comp_w = COLS * THUMB_W + (COLS + 1) * PAD
comp_h = ROWS * (THUMB_H + LABEL_H) + (ROWS + 1) * PAD + 60  # +60 for title

composite = Image.new("RGB", (comp_w, comp_h), (30, 30, 30))
draw = ImageDraw.Draw(composite)

# Try to load a nice font, fall back to default
try:
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
    font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
except OSError:
    font = ImageFont.load_default()
    font_sm = font

# Title
draw.text((PAD, PAD), "StreetView API Test — Svalbard (78.223°N, 15.628°E)", fill=(255, 255, 255), font=font)

for i, (label, img) in enumerate(steps):
    col = i % COLS
    row = i // COLS
    x = PAD + col * (THUMB_W + PAD)
    y = 60 + PAD + row * (THUMB_H + LABEL_H + PAD)

    thumb = img.resize((THUMB_W, THUMB_H), Image.LANCZOS)
    composite.paste(thumb, (x, y))

    step_label = f"Step {i + 1}: {label}"
    draw.text((x + 4, y + THUMB_H + 4), step_label, fill=(200, 200, 200), font=font_sm)

out_path = os.path.join(OUTPUT_DIR, "composite.jpg")
composite.save(out_path, quality=92)
print(f"✓ Composite saved to {out_path}")
