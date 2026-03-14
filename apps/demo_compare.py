"""Side-by-side comparison: online Static API vs local equirectangular rendering.

Requires the Playwright host to be running and GOOGLE_MAPS_API_KEY set.

Usage:
    python -m apps.demo_compare
    python -m apps.demo_compare --lat 48.8584 --lng 2.2945   # Eiffel Tower
    python -m apps.demo_compare --headings 0,90,180,270       # four directions


# Make sure the Playwright host is running first
node adapters/streetview_js/host.js

# Then in another terminal (defaults to Times Square, 4 headings)
python -m apps.demo_compare

# Or pick a specific location
python -m apps.demo_compare --lat 48.8584 --lng 2.2945  # Eiffel Tower

"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.streetview_js.client import StreetViewHostClient
from apps.crawler import ScreenshotCapture
from core.utils.equirect import perspectives_to_perspective
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
    views: list,
    heading: float,
    pitch: float,
    zoom: float,
    out_w: int = 640,
    out_h: int = 640,
) -> Image.Image:
    """Render a perspective view directly from raw screenshots (single interpolation)."""
    fov = zoom_to_fov(zoom)
    perspective = perspectives_to_perspective(views, heading, pitch, fov, out_w, out_h)
    return Image.fromarray(perspective)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare online API vs local equirectangular rendering"
    )
    parser.add_argument("--lat", type=float, default=40.7580, help="Latitude (default: Times Square)")
    parser.add_argument("--lng", type=float, default=-73.9855, help="Longitude (default: Times Square)")
    parser.add_argument(
        "--headings", type=str, default="0,15,30,45,60,75,90,105,120,135,150,165,180,195,210,225,240,255,270,285,300,315,330,345",
        help="Comma-separated headings to compare (default: 0,90,180,270)",
    )
    parser.add_argument("--pitch", type=float, default=0.0)
    parser.add_argument("--zoom", type=float, default=1.0)
    parser.add_argument("--output", type=str, default="demo_comparison.jpg")
    parser.add_argument("--host-url", type=str, default=None)
    args = parser.parse_args()

    headings = [float(h) for h in args.headings.split(",")]

    print(f"Location: ({args.lat}, {args.lng})")
    print(f"Headings: {headings}")
    print()

    # Step 1: Connect to Playwright host and get pano ID
    print("1. Connecting to Playwright host...")
    client = StreetViewHostClient(host_url=args.host_url)
    session_id = f"demo_{int(time.time())}"
    api_key = os.getenv("GOOGLE_MAPS_API_KEY", "")
    client.start(session_id, api_key=api_key)

    try:
        client.init(session_id, lat=args.lat, lng=args.lng)
        client.wait_for_stable(session_id)
        state = client.get_state(session_id)

        pano_id = state["panoId"]
        pos = state.get("position") or {}
        print(f"   Panorama ID: {pano_id}")
        print(f"   Actual position: ({pos.get('lat')}, {pos.get('lng')})")
        print()

        # Step 2: Capture equirectangular via browser screenshots
        tmp_dir = os.path.join(ROOT, "_demo_screenshots")
        capture = ScreenshotCapture(
            client=client,
            session_id=session_id,
            image_root=tmp_dir,
            screenshot_format="png",
        )
        n_shots = len(capture.headings) * len(capture.pitches)
        print(f"2. Capturing equirectangular via browser screenshots ({n_shots} angles)...")
        rel_path, eq_w, eq_h = capture.capture_equirectangular(pano_id)
        equirect_path = os.path.join(tmp_dir, rel_path)
        raw_views = capture.last_views
        print(f"   Equirectangular: {eq_w}x{eq_h} saved to {equirect_path}")
        print()

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
                raw_views, h, args.pitch, args.zoom, 640, 640
            )
            print(" done")

            online_labeled = label_image(online_img, f"Online API — heading {h:.0f}°")
            local_labeled = label_image(local_img, f"Local (screenshot stitch) — heading {h:.0f}°")

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
        print(f"  Right column: Local screenshot-stitched equirectangular render")

    finally:
        try:
            client.close_session(session_id)
        except Exception:
            pass
        client.close()


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    import logging
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    main()
