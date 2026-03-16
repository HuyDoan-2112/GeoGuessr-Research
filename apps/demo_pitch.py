"""Show what different pitch angles and FOV (zoom) levels look like for a location.

Uses the Street View Static API to fetch views at various pitch and FOV values.

Usage:
    python -m apps.demo_pitch
    python -m apps.demo_pitch --lat 48.8584 --lng 2.2945
    python -m apps.demo_pitch --pitches -30,0,30 --fovs 90,45,20
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests

from core.utils.image_utils import crop_google_logo


def fetch_static_view(
    lat: float, lng: float, heading: float, pitch: float, fov: float,
    size: str, api_key: str,
) -> Image.Image:
    """Fetch a Street View Static API image by coordinates."""
    url = (
        "https://maps.googleapis.com/maps/api/streetview"
        f"?size={size}&location={lat},{lng}"
        f"&heading={heading:.2f}&pitch={pitch:.2f}&fov={fov}"
        f"&key={api_key}"
    )
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return crop_google_logo(resp.content)


def label_image(img: Image.Image, text: str) -> Image.Image:
    bar_h = 32
    labeled = Image.new("RGB", (img.width, img.height + bar_h), (30, 30, 30))
    labeled.paste(img, (0, bar_h))
    draw = ImageDraw.Draw(labeled)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 18)
    except (OSError, IOError):
        font = ImageFont.load_default()
    draw.text((10, 6), text, fill=(255, 255, 255), font=font)
    return labeled


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Visualize different pitch angles at a single location"
    )
    parser.add_argument("--lat", type=float, default=40.7580)
    parser.add_argument("--lng", type=float, default=-73.9855)
    parser.add_argument("--heading", type=float, default=0.0)
    parser.add_argument(
        "--pitches", type=str, default="-30,0,30",
        help="Comma-separated pitch values (default: -30,0,30)",
    )
    parser.add_argument(
        "--fovs", type=str, default="120,90,45,30,20",
        help="Comma-separated FOV values: 90=normal, 45=2x zoom, 20=4.5x zoom",
    )
    parser.add_argument("--size", type=str, default="640x640")
    parser.add_argument("--output", type=str, default="demo_pitch.jpg")
    args = parser.parse_args()

    pitches = [float(p) for p in args.pitches.split(",")]
    fovs = [float(f) for f in args.fovs.split(",")]

    api_key = os.getenv("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        print("Error: GOOGLE_MAPS_API_KEY not set")
        sys.exit(1)

    print(f"Location: ({args.lat}, {args.lng}), heading={args.heading}°")
    print(f"Pitches: {pitches}")
    print(f"FOVs: {fovs}")
    print()

    # Grid: rows = pitches, columns = FOVs
    images = []
    for pitch in pitches:
        row_imgs = []
        for fov in fovs:
            print(f"  Fetching pitch={pitch:+.0f}° fov={fov:.0f}°...", end="", flush=True)
            img = fetch_static_view(
                args.lat, args.lng, args.heading, pitch, fov,
                args.size, api_key,
            )
            img = img.resize((640, 640), Image.LANCZOS)
            labeled = label_image(img, f"pitch={pitch:+.0f}°  fov={fov:.0f}° ({90/fov:.1f}x zoom)")
            row_imgs.append(labeled)
            print(" done")
        images.append(row_imgs)

    # Arrange grid: rows = pitches, cols = fovs
    cols = len(fovs)
    rows = len(pitches)
    cell_w, cell_h = images[0][0].width, images[0][0].height
    gap = 4
    canvas = Image.new(
        "RGB",
        (cols * cell_w + (cols - 1) * gap, rows * cell_h + (rows - 1) * gap),
        (60, 60, 60),
    )
    for r, row_imgs in enumerate(images):
        for c, img in enumerate(row_imgs):
            canvas.paste(img, (c * (cell_w + gap), r * (cell_h + gap)))

    canvas.save(args.output, quality=92)
    print(f"\nSaved to: {args.output}")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    main()
