"""Test script: crawl a single location, show neighbors, and create a labeled mosaic.

Usage:
    python -m apps.test_mapcrunch_single
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import List, Tuple

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.crawler import TilesAPIClient
from apps.mapcrunch_crawler import (
    CaptureConfig,
    CaptureDatabase,
    MapCrunchCapture,
    bfs_discover,
)

logger = logging.getLogger(__name__)

# Official Google Street View pano at Times Square (avoids user photospheres)
LAT, LNG = 40.75798548022977, -73.98552675217574
DB_PATH = "test_mapcrunch_single.db"
IMAGE_ROOT = "test_mapcrunch_single_images"

# Modest set of angles for testing
HEADINGS = [0.0, 90.0, 180.0, 270.0]
PITCHES = [float(i) for i in range(-40, 50, 10)]
ZOOMS = [0.0, 1.0, 1.5, 2.0, 3.0]


class CountingTilesClient(TilesAPIClient):
    """Thin wrapper that counts API calls."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.api_calls = 0

    def get_metadata(self, **kwargs):
        self.api_calls += 1
        return super().get_metadata(**kwargs)

    def get_tile(self, *args, **kwargs):
        self.api_calls += 1
        return super().get_tile(*args, **kwargs)


def run_bfs(api_key: str) -> Tuple[int, CaptureDatabase, int]:
    """BFS discover only the starting pano (depth=0) to collect neighbor info.

    Returns (job_id, db, api_call_count).
    """
    # Clean up any previous test run
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        logger.info("Removed stale database %s", DB_PATH)

    db = CaptureDatabase(DB_PATH)
    tiles = CountingTilesClient(api_key)

    job_id = db.create_job(LAT, LNG, max_depth=1)
    # depth=1 so we discover the start pano AND record its neighbor links,
    # but we only capture screenshots for depth-0 panos.
    n = bfs_discover(tiles, db, LAT, LNG, job_id, max_depth=1, api_key=api_key)
    api_calls = tiles.api_calls
    tiles.close()
    logger.info("BFS discovered %d panoramas (%d API calls)", n, api_calls)
    return job_id, db, api_calls


def print_neighbor_info(db: CaptureDatabase, job_id: int) -> None:
    """Print all neighbor links discovered during BFS."""
    # Get the starting pano (depth 0)
    rows = db._conn.execute(
        "SELECT * FROM panoramas WHERE job_id = ? AND bfs_depth = 0", (job_id,)
    ).fetchall()

    if not rows:
        logger.error("No starting panorama found")
        return

    start = dict(rows[0])
    print(f"\n{'='*70}")
    print(f"Starting panorama: {start['pano_id']}")
    print(f"  Location: ({start['lat']:.6f}, {start['lng']:.6f})")
    print(f"  Date: {start['date']}")
    print(f"{'='*70}")

    # Get all links from the starting pano
    links = db._conn.execute(
        "SELECT * FROM panorama_links WHERE from_pano_id = ? ORDER BY heading",
        (start["pano_id"],),
    ).fetchall()

    print(f"\nNeighbor links ({len(links)} total):")
    print(f"{'Heading':>8}  {'To Pano ID':<30}  {'Description'}")
    print(f"{'-'*8}  {'-'*30}  {'-'*30}")
    for link in links:
        link = dict(link)
        print(
            f"{link['heading']:8.1f}  {link['to_pano_id']:<30}  {link.get('description', '')}"
        )

    # Also show depth-1 panos (the neighbors themselves)
    neighbors = db._conn.execute(
        "SELECT * FROM panoramas WHERE job_id = ? AND bfs_depth = 1 ORDER BY pano_id",
        (job_id,),
    ).fetchall()
    print(f"\nDiscovered neighbor panoramas ({len(neighbors)}):")
    for nb in neighbors:
        nb = dict(nb)
        lat_s = f"{nb['lat']:.6f}" if nb["lat"] else "N/A"
        lng_s = f"{nb['lng']:.6f}" if nb["lng"] else "N/A"
        print(f"  {nb['pano_id']}  ({lat_s}, {lng_s})  date={nb['date']}")

    print()


async def capture_start_pano(db: CaptureDatabase, job_id: int) -> None:
    """Capture screenshots only for the depth-0 (starting) pano."""
    rows = db._conn.execute(
        "SELECT * FROM panoramas WHERE job_id = ? AND bfs_depth = 0", (job_id,)
    ).fetchall()
    if not rows:
        logger.error("No starting panorama to capture")
        return

    pano = dict(rows[0])
    pano_id = pano["pano_id"]
    lat, lng = pano["lat"], pano["lng"]

    angles: List[Tuple[float, float, float]] = []
    for h in HEADINGS:
        for p in PITCHES:
            for z in ZOOMS:
                angles.append((h, p, z))

    logger.info("Capturing %d angles for pano %s", len(angles), pano_id)

    cap = MapCrunchCapture(headless=True, viewport_width=1920, viewport_height=1080)
    await cap.start()

    try:
        results = await cap.capture_pano(pano_id, lat, lng, angles, IMAGE_ROOT)
        for r in results:
            db.insert_capture(
                pano_id=r["pano_id"],
                heading=r["heading"],
                pitch=r["pitch"],
                zoom=r["zoom"],
                image_path=r["image_path"],
                width=r["width"],
                height=r["height"],
            )
        logger.info("Captured %d / %d screenshots", len(results), len(angles))
    finally:
        await cap.close()


def create_mosaic(db: CaptureDatabase, job_id: int) -> str:
    """Stack all captured images into a labeled mosaic grid.

    Layout: rows = heading x pitch combos, columns = zoom levels.
    """
    rows = db._conn.execute(
        """
        SELECT c.* FROM captures c
        JOIN panoramas p ON p.pano_id = c.pano_id
        WHERE p.job_id = ? AND c.image_path IS NOT NULL
        ORDER BY c.heading, c.pitch, c.zoom
        """,
        (job_id,),
    ).fetchall()

    if not rows:
        logger.error("No captures to mosaic")
        return ""

    captures = [dict(r) for r in rows]

    # Determine grid dimensions
    headings_set = sorted(set(c["heading"] for c in captures))
    pitches_set = sorted(set(c["pitch"] for c in captures))
    zooms_set = sorted(set(c["zoom"] for c in captures))

    # Grid: each row is a (heading, pitch) pair, each column is a zoom level
    row_keys = [(h, p) for h in headings_set for p in pitches_set]
    col_keys = zooms_set

    # Build lookup
    lookup = {}
    for c in captures:
        key = (c["heading"], c["pitch"], c["zoom"])
        lookup[key] = c

    # Load one image to get cell size
    sample = captures[0]
    sample_path = os.path.join(IMAGE_ROOT, sample["image_path"])
    with Image.open(sample_path) as img:
        orig_w, orig_h = img.size

    # Scale down for the mosaic
    scale = 0.25
    cell_w = int(orig_w * scale)
    cell_h = int(orig_h * scale)
    label_h = 30
    header_h = 40

    n_cols = len(col_keys)
    n_rows = len(row_keys)

    mosaic_w = cell_w * n_cols + 200  # 200px left margin for row labels
    mosaic_h = (cell_h + label_h) * n_rows + header_h

    mosaic = Image.new("RGB", (mosaic_w, mosaic_h), "white")
    draw = ImageDraw.Draw(mosaic)

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 16)
        font_small = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 13)
    except Exception:
        font = ImageFont.load_default()
        font_small = font

    # Column headers (zoom levels)
    for ci, z in enumerate(col_keys):
        x = 200 + ci * cell_w + cell_w // 2
        draw.text((x, 10), f"zoom={z:.1f}", fill="black", font=font, anchor="mt")

    # Draw each cell
    for ri, (h, p) in enumerate(row_keys):
        y_base = header_h + ri * (cell_h + label_h)

        # Row label
        draw.text(
            (10, y_base + cell_h // 2),
            f"h={h:.0f} p={p:.0f}",
            fill="black",
            font=font_small,
            anchor="lm",
        )

        for ci, z in enumerate(col_keys):
            x_base = 200 + ci * cell_w
            key = (h, p, z)
            cap = lookup.get(key)

            if cap:
                img_path = os.path.join(IMAGE_ROOT, cap["image_path"])
                try:
                    with Image.open(img_path) as img:
                        img_resized = img.resize((cell_w, cell_h), Image.LANCZOS)
                        mosaic.paste(img_resized, (x_base, y_base))
                except Exception as e:
                    draw.rectangle(
                        [x_base, y_base, x_base + cell_w, y_base + cell_h],
                        fill="gray",
                    )
                    draw.text(
                        (x_base + cell_w // 2, y_base + cell_h // 2),
                        "ERR",
                        fill="red",
                        font=font,
                        anchor="mm",
                    )
            else:
                draw.rectangle(
                    [x_base, y_base, x_base + cell_w, y_base + cell_h],
                    fill="lightgray",
                )
                draw.text(
                    (x_base + cell_w // 2, y_base + cell_h // 2),
                    "N/A",
                    fill="gray",
                    font=font,
                    anchor="mm",
                )

    output_path = os.path.join(IMAGE_ROOT, "mosaic.jpg")
    mosaic.save(output_path, quality=90)
    logger.info("Mosaic saved to %s (%d x %d)", output_path, mosaic_w, mosaic_h)
    return output_path


async def main() -> None:
    import time as _time
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")

    api_key = os.getenv("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        print("ERROR: Set GOOGLE_MAPS_API_KEY in .env or environment")
        sys.exit(1)

    t_total = _time.time()
    timings: list[tuple[str, float]] = []

    # Phase 1: BFS discovery (depth=1 to get neighbor links)
    print("Phase 1: BFS Discovery...")
    t0 = _time.time()
    job_id, db, api_calls = run_bfs(api_key)
    dt = _time.time() - t0
    timings.append(("BFS Discovery", dt))
    print(f"  -> {dt:.1f}s ({api_calls} Google API calls)")

    # Show neighbor info
    print_neighbor_info(db, job_id)

    # Phase 2: Capture screenshots for starting pano only
    print("Phase 2: Capturing screenshots for starting pano...")
    t0 = _time.time()
    await capture_start_pano(db, job_id)
    dt = _time.time() - t0
    timings.append(("Screenshot Capture", dt))
    print(f"  -> {dt:.1f}s")

    # Phase 3: Create mosaic
    print("Phase 3: Creating labeled mosaic...")
    t0 = _time.time()
    mosaic_path = create_mosaic(db, job_id)
    dt = _time.time() - t0
    timings.append(("Mosaic Creation", dt))
    print(f"  -> {dt:.1f}s")

    if mosaic_path:
        print(f"\nMosaic saved to: {mosaic_path}")

    # Timing summary
    total = _time.time() - t_total
    print(f"\n{'='*40}")
    print("Summary")
    print(f"{'='*40}")
    print(f"  Google API calls: {api_calls}")
    print()
    for name, dt in timings:
        print(f"  {name:<25} {dt:6.1f}s")
    print(f"  {'─'*32}")
    print(f"  {'Total':<25} {total:6.1f}s")
    print(f"{'='*40}")

    db.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    asyncio.run(main())
