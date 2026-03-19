"""Compare MapCrunch screenshots with Street View Static API images.

Generates a side-by-side comparison grid at various pitch and zoom settings
so you can evaluate how well the MapCrunch capture matches the Static API.

Usage:
    python -m apps.demo_compare_mapcrunch
    python -m apps.demo_compare_mapcrunch --lat 48.8584 --lng 2.2945
    python -m apps.demo_compare_mapcrunch --pitches -20,0,20 --zooms 1.0,2.0,3.0

Prerequisites:
    pip install playwright
    python -m playwright install chromium
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Tuple

import requests
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.utils.image_utils import crop_google_logo
from apps.mapcrunch_crawler import (
    MAPCRUNCH_BASE,
    CHROME_USER_AGENT,
    AD_BLOCK_PATTERNS,
    OVERLAY_HIDE_CSS,
    FIND_AND_CONFIGURE_PANORAMA_JS,
    DOM_CLEANUP_JS,
    RESIZE_PANORAMA_JS,
    WAIT_TILES_JS,
    SET_POV_JS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def zoom_to_fov(zoom: float) -> float:
    """Convert Google Maps JS panorama zoom level to Static API FOV (degrees).

    An empirical offset of +0.1 is applied so that the Static API image
    matches the JS panorama at the same zoom level (e.g. zoom 0.9 ≈ FOV 90°).
    """
    return max(1.0, min(120.0, 180.0 / (2.0 ** (zoom + 0.1))))


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
    """Add a dark label bar on top of an image."""
    bar_h = 28
    labeled = Image.new("RGB", (img.width, img.height + bar_h), (30, 30, 30))
    labeled.paste(img, (0, bar_h))
    draw = ImageDraw.Draw(labeled)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 16)
    except (OSError, IOError):
        font = ImageFont.load_default()
    draw.text((8, 5), text, fill=(255, 255, 255), font=font)
    return labeled


def _placeholder(size: int, text: str = "NO IMAGE") -> Image.Image:
    """Red placeholder when a capture fails."""
    img = Image.new("RGB", (size, size), (100, 20, 20))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 20)
    except (OSError, IOError):
        font = ImageFont.load_default()
    draw.text((size // 4, size // 2 - 10), text, fill=(255, 255, 255), font=font)
    return img


# ---------------------------------------------------------------------------
# MapCrunch batch capture
# ---------------------------------------------------------------------------

async def capture_mapcrunch_views(
    lat: float,
    lng: float,
    heading: float,
    pitch_zoom_pairs: List[Tuple[float, float]],
    viewport_w: int,
    viewport_h: int,
) -> Dict[Tuple[float, float], Image.Image]:
    """Open MapCrunch once, capture multiple views by changing POV in-place.

    Returns ``{(pitch, zoom): PIL.Image}`` for each successful capture.
    """
    from playwright.async_api import async_playwright

    results: Dict[Tuple[float, float], Image.Image] = {}
    if not pitch_zoom_pairs:
        return results

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--no-first-run",
        ],
    )
    context = await browser.new_context(
        viewport={"width": viewport_w, "height": viewport_h},
        locale="en-US",
        user_agent=CHROME_USER_AGENT,
    )
    page = await context.new_page()
    for pattern in AD_BLOCK_PATTERNS:
        await page.route(pattern, lambda route: route.abort())

    try:
        # --- initial page load ---
        first_p, first_z = pitch_zoom_pairs[0]
        url = f"{MAPCRUNCH_BASE}/p/{lat}_{lng}_{heading}_{first_p}_{first_z}"
        print(f"  Loading MapCrunch at ({lat}, {lng}) ...", end="", flush=True)
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        try:
            await page.wait_for_function(
                "window.google && window.google.maps", timeout=20_000,
            )
        except Exception:
            print(" Google Maps failed to load")
            return results

        await asyncio.sleep(3)

        # overlay removal
        await page.add_style_tag(content=OVERLAY_HIDE_CSS)
        cfg = await page.evaluate(FIND_AND_CONFIGURE_PANORAMA_JS)
        await page.evaluate(DOM_CLEANUP_JS)
        # Re-trigger resize after DOM cleanup so panorama fills viewport
        await page.evaluate(RESIZE_PANORAMA_JS)
        await page.evaluate(WAIT_TILES_JS, 8000)
        await asyncio.sleep(1)

        pano_ok = cfg.get("found", False)
        print(f" ready (panorama={'found' if pano_ok else 'NOT found'})")

        # --- capture each view ---
        for pitch, zoom in pitch_zoom_pairs:
            print(
                f"    MapCrunch  pitch={pitch:+.0f}\u00b0  zoom={zoom:.2f} ...",
                end="", flush=True,
            )

            # always set POV explicitly (MapCrunch overrides URL-based POV)
            changed = await page.evaluate(SET_POV_JS, [heading, pitch, zoom])
            if changed:
                await page.evaluate(WAIT_TILES_JS, 5000)
                await asyncio.sleep(0.5)
            else:
                # fallback: full reload
                url2 = (
                    f"{MAPCRUNCH_BASE}/p/{lat}_{lng}_{heading}_{pitch}_{zoom}"
                )
                await page.goto(
                    url2, wait_until="domcontentloaded", timeout=30_000,
                )
                try:
                    await page.wait_for_function(
                        "window.google && window.google.maps", timeout=20_000,
                    )
                except Exception:
                    print(" failed")
                    continue
                await asyncio.sleep(3)
                await page.add_style_tag(content=OVERLAY_HIDE_CSS)
                await page.evaluate(FIND_AND_CONFIGURE_PANORAMA_JS)
                await page.evaluate(DOM_CLEANUP_JS)
                await page.evaluate(RESIZE_PANORAMA_JS)
                await page.evaluate(WAIT_TILES_JS, 8000)
                await asyncio.sleep(1)

            img_bytes = await page.screenshot(type="png")
            results[(pitch, zoom)] = Image.open(BytesIO(img_bytes)).convert("RGB")
            print(" done")

    except Exception as e:
        print(f"\n  MapCrunch error: {e}")
    finally:
        await context.close()
        await browser.close()
        await pw.stop()

    return results


# ---------------------------------------------------------------------------
# Grid assembly
# ---------------------------------------------------------------------------

def build_grid(
    static_images: Dict[Tuple[float, float], Image.Image],
    mc_images: Dict[Tuple[float, float], Image.Image],
    pitches: List[float],
    zooms: List[float],
    cell_size: int,
) -> Image.Image:
    """Build the comparison grid.

    Each cell is [Static API | MapCrunch] side by side.
    Rows = pitches, columns = zooms.
    """
    pair_gap = 4
    grid_gap = 6

    rows_imgs: List[List[Image.Image]] = []
    for pitch in pitches:
        row: List[Image.Image] = []
        for zoom in zooms:
            fov = zoom_to_fov(zoom)

            s_img = static_images.get((pitch, zoom))
            if s_img is None:
                s_img = _placeholder(cell_size, "STATIC FAIL")
            else:
                s_img = s_img.resize((cell_size, cell_size), Image.LANCZOS)

            m_img = mc_images.get((pitch, zoom))
            if m_img is None:
                m_img = _placeholder(cell_size, "MC FAIL")
            else:
                m_img = m_img.resize((cell_size, cell_size), Image.LANCZOS)

            s_lbl = label_image(
                s_img,
                f"Static API  pitch={pitch:+.0f}\u00b0  fov={fov:.0f}\u00b0",
            )
            m_lbl = label_image(
                m_img,
                f"MapCrunch   pitch={pitch:+.0f}\u00b0  zoom={zoom:.1f}",
            )

            pw = s_lbl.width + pair_gap + m_lbl.width
            ph = max(s_lbl.height, m_lbl.height)
            pair = Image.new("RGB", (pw, ph), (60, 60, 60))
            pair.paste(s_lbl, (0, 0))
            pair.paste(m_lbl, (s_lbl.width + pair_gap, 0))
            row.append(pair)
        rows_imgs.append(row)

    cell_w = rows_imgs[0][0].width
    cell_h = rows_imgs[0][0].height
    cols = len(zooms)
    n_rows = len(pitches)
    canvas = Image.new(
        "RGB",
        (cols * cell_w + (cols - 1) * grid_gap,
         n_rows * cell_h + (n_rows - 1) * grid_gap),
        (60, 60, 60),
    )
    for r, row_cells in enumerate(rows_imgs):
        for c, cell in enumerate(row_cells):
            canvas.paste(cell, (c * (cell_w + grid_gap), r * (cell_h + grid_gap)))

    return canvas


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> None:
    pitches = [float(p) for p in args.pitches.split(",")]
    zooms = [float(z) for z in args.zooms.split(",")]

    api_key = os.getenv("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        print("Error: GOOGLE_MAPS_API_KEY not set")
        sys.exit(1)

    print(f"Location : ({args.lat}, {args.lng}), heading={args.heading}\u00b0")
    print(f"Pitches  : {pitches}")
    print(f"Zooms    : {zooms}")
    print()

    # -- 1. Static API images --
    static_images: Dict[Tuple[float, float], Image.Image] = {}
    for pitch in pitches:
        for zoom in zooms:
            fov = zoom_to_fov(zoom)
            print(
                f"  Static API  pitch={pitch:+.0f}\u00b0  fov={fov:.0f}\u00b0 ...",
                end="", flush=True,
            )
            try:
                img = fetch_static_view(
                    args.lat, args.lng, args.heading, pitch, fov,
                    args.size, api_key,
                )
                static_images[(pitch, zoom)] = img
                print(" done")
            except Exception as e:
                print(f" error: {e}")

    # -- 2. MapCrunch images --
    pitch_zoom_pairs: List[Tuple[float, float]] = []
    for pitch in pitches:
        for zoom in zooms:
            pitch_zoom_pairs.append((pitch, zoom))

    mc_images = await capture_mapcrunch_views(
        args.lat, args.lng, args.heading,
        pitch_zoom_pairs,
        viewport_w=args.cell_size,
        viewport_h=args.cell_size,
    )

    # -- 3. Assemble grid --
    print("\nBuilding comparison grid ...")
    canvas = build_grid(static_images, mc_images, pitches, zooms, args.cell_size)
    canvas.save(args.output, quality=92)
    print(f"Saved to: {args.output} ({canvas.width}\u00d7{canvas.height})")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare MapCrunch vs Street View Static API at various angles",
    )
    parser.add_argument("--lat", type=float, default=40.7580)
    parser.add_argument("--lng", type=float, default=-73.9855)
    parser.add_argument("--heading", type=float, default=0.0)
    parser.add_argument(
        "--pitches", type=str, default="-40,-20,0,20,40",
        help="Comma-separated pitch values (default: -40,-20,0,20,40)",
    )
    parser.add_argument(
        "--zooms", type=str, default="0.5,1.0,1.5,2.0,3.0",
        help="Comma-separated zoom levels (default: 0.5,1.0,1.5,2.0,3.0)",
    )
    parser.add_argument("--size", type=str, default="640x640",
                        help="Static API image size (default: 640x640)")
    parser.add_argument("--cell-size", type=int, default=400,
                        help="Size of each image in the grid (default: 400)")
    parser.add_argument("--output", type=str, default="demo_compare_mapcrunch.jpg")
    args = parser.parse_args()

    asyncio.run(run(args))


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    main()
