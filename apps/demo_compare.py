"""Side-by-side comparison: online Static API vs local Tiles API rendering.

Requires GOOGLE_MAPS_API_KEY set.

Usage:
    python -m apps.demo_compare
    python -m apps.demo_compare --lat 48.8584 --lng 2.2945   # Eiffel Tower
    python -m apps.demo_compare --headings 0,90,180,270       # four directions

"""

from __future__ import annotations

import argparse
import os
import sys
import time
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.crawler import TilesAPIClient, TileCapture
from core.utils.equirect import equirect_to_perspective
from core.utils.image_utils import fetch_image, crop_google_logo, zoom_to_fov


def label_image(img: Image.Image, text: str) -> Image.Image:
    """Add a text label bar at the top of an image."""
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


def fetch_online_view(
    pano_id: str,
    heading: float,
    pitch: float,
    zoom: float,
    size: str = "640x640",
) -> Image.Image:
    """Fetch a perspective view from the Google Static API."""
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    signing_secret = os.getenv("GOOGLE_MAPS_URL_SIGNING_SECRET")
    raw = fetch_image(
        pano_id=pano_id,
        heading=heading,
        pitch=pitch,
        zoom=zoom,
        size=size,
        api_key=api_key,
        signing_secret=signing_secret,
    )
    return crop_google_logo(raw)


def render_local_view(
    equirect: np.ndarray,
    heading: float,
    pitch: float,
    zoom: float,
    out_w: int = 640,
    out_h: int = 640,
) -> Image.Image:
    """Render a perspective view from the local equirectangular panorama."""
    fov = zoom_to_fov(zoom)
    perspective = equirect_to_perspective(equirect, heading, pitch, fov, out_w, out_h)
    return Image.fromarray(perspective)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare online API vs local Tiles API rendering"
    )
    parser.add_argument("--lat", type=float, default=40.7580, help="Latitude (default: Times Square)")
    parser.add_argument("--lng", type=float, default=-73.9855, help="Longitude (default: Times Square)")
    parser.add_argument(
        "--headings", type=str, default="0,15,30,45,60,75,90,105,120,135,150,165,180,195,210,225,240,255,270,285,300,315,330,345",
        help="Comma-separated headings to compare (default: every 15 degrees)",
    )
    parser.add_argument("--pitch", type=float, default=0.0)
    parser.add_argument("--zoom", type=float, default=1.0)
    parser.add_argument("--tile-zoom", type=int, default=5, help="Tile zoom level (default: 5)")
    parser.add_argument("--output", type=str, default="demo_comparison.jpg")
    args = parser.parse_args()

    headings = [float(h) for h in args.headings.split(",")]

    api_key = os.getenv("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        print("Error: GOOGLE_MAPS_API_KEY not set")
        sys.exit(1)

    print(f"Location: ({args.lat}, {args.lng})")
    print(f"Headings: {headings}")
    print()

    # Step 1: Look up panorama via Tiles API
    print("1. Looking up panorama via Tiles API...")
    tiles_client = TilesAPIClient(api_key)

    try:
        metadata = tiles_client.get_metadata(lat=args.lat, lng=args.lng)
        pano_id = metadata["panoId"]
        print(f"   Panorama ID: {pano_id}")
        print(f"   Actual position: ({metadata.get('lat')}, {metadata.get('lng')})")
        print()

        # Step 2: Download equirectangular via Tiles API
        tmp_dir = os.path.join(ROOT, "_demo_tiles")
        capture = TileCapture(
            tiles_client=tiles_client,
            image_root=tmp_dir,
            tile_zoom=args.tile_zoom,
        )
        print(f"2. Downloading tiles at zoom {args.tile_zoom}...")
        t0 = time.time()
        rel_path, eq_w, eq_h, actual_zoom = capture.capture_equirectangular(pano_id, metadata=metadata)
        equirect_path = os.path.join(tmp_dir, rel_path)
        print(f"   Equirectangular: {eq_w}x{eq_h} saved to {equirect_path} ({time.time() - t0:.1f}s)")
        print()

        # Load equirectangular as numpy array for perspective extraction
        equirect = np.array(Image.open(equirect_path).convert("RGB"))

        # Step 3: For each heading, fetch online + render local
        print(f"3. Generating {len(headings)} comparisons...")
        rows = []
        for h in headings:
            print(f"   Heading {h:.0f}°: fetching online...", end="", flush=True)
            online_img = fetch_online_view(pano_id, h, args.pitch, args.zoom)
            # Resize online to match (it may be 640x580 after crop)
            online_img = online_img.resize((640, 640), Image.LANCZOS)
            print(" rendering local...", end="", flush=True)
            local_img = render_local_view(
                equirect, h, args.pitch, args.zoom, 640, 640
            )
            print(" done")

            online_labeled = label_image(online_img, f"Online API — heading {h:.0f}°")
            local_labeled = label_image(local_img, f"Local (Tiles API) — heading {h:.0f}°")

            # Side by side for this heading
            pair = Image.new(
                "RGB",
                (online_labeled.width + local_labeled.width + 4, online_labeled.height),
                (60, 60, 60),
            )
            pair.paste(online_labeled, (0, 0))
            pair.paste(local_labeled, (online_labeled.width + 4, 0))
            rows.append(pair)

        # Step 4: Stack all rows vertically
        total_w = max(r.width for r in rows)
        total_h = sum(r.height for r in rows) + 4 * (len(rows) - 1)
        canvas = Image.new("RGB", (total_w, total_h), (60, 60, 60))
        y = 0
        for row in rows:
            canvas.paste(row, (0, y))
            y += row.height + 4

        canvas.save(args.output, quality=92)
        print()
        print(f"Comparison saved to: {args.output}")
        print(f"  Left column:  Online Google Static API (640x640, cropped + resized)")
        print(f"  Right column: Local Tiles API equirectangular render")

    finally:
        tiles_client.close()


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    import logging
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    main()
