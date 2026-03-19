"""MapCrunch Street View screenshot capture — clean panorama images via headless Chrome.

Uses Playwright to visit MapCrunch, remove all overlay UI, and capture
clean Street View screenshots at specified coordinates and view angles.

Usage:
    python -m apps.mapcrunch_crawler --coords 29.958574,-90.065712
    python -m apps.mapcrunch_crawler --coords-file locations.csv
    python -m apps.mapcrunch_crawler --coords 37.7749,-122.4194 --headings 0 90 180 270

Prerequisites:
    pip install playwright
    python -m playwright install chromium
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

logger = logging.getLogger(__name__)

MAPCRUNCH_BASE = "https://www.mapcrunch.com"

CHROME_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

AD_BLOCK_PATTERNS = [
    "**/*adthrive*",
    "**/*doubleclick*",
    "**/*googletag*",
    "**/*google-analytics*",
    "**/*googletagmanager*",
    "**/*plausible*",
]

# ---------------------------------------------------------------------------
# CSS / JS payloads for overlay removal
# ---------------------------------------------------------------------------

OVERLAY_HIDE_CSS = """
/* ---- Google Maps Street View UI controls ---- */
.gm-style-cc,
.gm-bundled-control,
.gmnoprint,
.gm-svpc,
.gm-fullscreen-control,
.gm-control-active,
div.gm-iv-address,
div[class*="scene-footer"],
div[class*="scene-description"],
a[href*="maps.google.com"],
a[href*="google.com/maps"],
img[src*="google_white"],
img[alt="Google"],
/* ---- MapCrunch overlay elements ---- */
#panel, #bar, #topbar, #controls, #settings,
#go, #hide, #go-button, #hide-button, #random-button,
#settings-button, #share, #share-button, #social,
#map-toggle, #map-canvas, #mini-map, #mini_map,
#info, #logo, #header, #footer, #sidebar, #watermark,
#top-box, #bottom-box, #map-box, #options-panel,
#navi-panel, #share-panel, #chooser, #container > nav,
nav, footer, header,
.mc-controls, .mc-toolbar, .mc-button, .mc-overlay,
.hover, .tipsy,
[id*="overlay"]:not(.gm-style):not([id*="pano"]),
/* ---- Ads and iframes ---- */
iframe, [id*="adthrive"], [class*="adthrive"],
[id*="google_ads"], [data-ad], .ad-wrapper, .ad-container {
    display: none !important;
    visibility: hidden !important;
    pointer-events: none !important;
}
body {
    margin: 0 !important;
    padding: 0 !important;
    overflow: hidden !important;
}
#pano-box, #pano {
    position: fixed !important;
    top: 0 !important;
    left: 0 !important;
    width: 100vw !important;
    height: 100vh !important;
    z-index: 9999 !important;
}
"""

FIND_AND_CONFIGURE_PANORAMA_JS = """() => {
    const result = { found: false, method: null, panoId: null, lat: null, lng: null };

    // Strategy 1: well-known global names MapCrunch might use
    const names = [
        'panorama', 'sv', 'pano', 'streetView', 'svPano',
        'streetViewPanorama', 'viewer',
    ];
    for (const n of names) {
        const obj = window[n];
        if (obj && typeof obj.getPov === 'function' && typeof obj.setPov === 'function') {
            window.__svPanorama = obj;
            result.found = true;
            result.method = 'global:' + n;
            break;
        }
    }

    // Strategy 2: scan all window own-properties
    if (!result.found) {
        for (const key of Object.getOwnPropertyNames(window)) {
            try {
                const obj = window[key];
                if (obj && typeof obj === 'object' &&
                    typeof obj.getPov === 'function' &&
                    typeof obj.setPov === 'function' &&
                    typeof obj.getPano === 'function') {
                    window.__svPanorama = obj;
                    result.found = true;
                    result.method = 'window_scan:' + key;
                    break;
                }
            } catch (e) {}
        }
    }

    // Configure panorama if found
    if (window.__svPanorama) {
        const p = window.__svPanorama;

        // Clear MapCrunch event listeners that interfere with POV changes
        const events = ['pov_changed', 'position_changed', 'pano_changed',
                        'zoom_changed', 'links_changed'];
        for (const ev of events) {
            google.maps.event.clearListeners(p, ev);
        }
        // Kill MapCrunch's panoFix timer
        if (window.panoFixEvent) clearTimeout(window.panoFixEvent);

        p.setOptions({
            linksControl: false,
            addressControl: false,
            zoomControl: false,
            panControl: false,
            fullscreenControl: false,
            motionTracking: false,
            motionTrackingControl: false,
            showRoadLabels: false,
            clickToGo: false,
            scrollwheel: false,
            enableCloseButton: false,
            imageDateControl: false,
        });
        try {
            const pos = p.getPosition();
            result.panoId = p.getPano() || null;
            result.lat = pos ? pos.lat() : null;
            result.lng = pos ? pos.lng() : null;
        } catch (e) {}
    }

    // Make panorama container fill the viewport
    const gmStyle = document.querySelector('.gm-style');
    if (gmStyle) {
        let el = gmStyle;
        while (el && el !== document.body) {
            el.style.setProperty('position', 'fixed', 'important');
            el.style.setProperty('top', '0', 'important');
            el.style.setProperty('left', '0', 'important');
            el.style.setProperty('width', '100vw', 'important');
            el.style.setProperty('height', '100vh', 'important');
            el.style.setProperty('z-index', '1', 'important');
            el.style.setProperty('margin', '0', 'important');
            el.style.setProperty('padding', '0', 'important');
            el = el.parentElement;
        }
        // Also resize the panorama's own container div
        if (window.__svPanorama) {
            const panoDiv = window.__svPanorama.getDiv ? window.__svPanorama.getDiv() : null;
            if (panoDiv) {
                panoDiv.style.setProperty('width', '100vw', 'important');
                panoDiv.style.setProperty('height', '100vh', 'important');
            }
        }
        // Force layout reflow before triggering resize
        void document.body.offsetHeight;
        if (window.__svPanorama && window.google) {
            google.maps.event.trigger(window.__svPanorama, 'resize');
        }
    }

    return result;
}"""

SET_POV_JS = """([heading, pitch, zoom]) => {
    if (!window.__svPanorama) return false;
    window.__svPanorama.setPov({ heading: heading, pitch: pitch });
    if (zoom !== null && zoom !== undefined) {
        window.__svPanorama.setZoom(zoom);
    }
    return true;
}"""

GET_STATE_JS = """() => {
    if (!window.__svPanorama) return null;
    const p = window.__svPanorama;
    const pos = p.getPosition();
    const pov = p.getPov();
    return {
        panoId: p.getPano() || null,
        lat: pos ? pos.lat() : null,
        lng: pos ? pos.lng() : null,
        heading: pov ? pov.heading : null,
        pitch: pov ? pov.pitch : null,
        zoom: p.getZoom(),
    };
}"""

RESIZE_PANORAMA_JS = """() => {
    if (!window.__svPanorama || !window.google) return false;
    const panoDiv = window.__svPanorama.getDiv ? window.__svPanorama.getDiv() : null;
    if (panoDiv) {
        panoDiv.style.setProperty('width', '100vw', 'important');
        panoDiv.style.setProperty('height', '100vh', 'important');
    }
    void document.body.offsetHeight;
    google.maps.event.trigger(window.__svPanorama, 'resize');
    return true;
}"""

WAIT_TILES_JS = """(timeoutMs) => new Promise(resolve => {
    if (!window.__svPanorama) { resolve(false); return; }
    let done = false;
    const t = setTimeout(() => { if (!done) { done = true; resolve(true); } }, timeoutMs);
    google.maps.event.addListenerOnce(window.__svPanorama, 'tilesloaded', () => {
        if (!done) { done = true; clearTimeout(t); resolve(true); }
    });
})"""

DOM_CLEANUP_JS = """() => {
    // Remove all non-panorama children from #container
    const container = document.getElementById('container');
    const panoBox = document.getElementById('pano-box');
    if (container && panoBox) {
        for (const child of Array.from(container.children)) {
            if (child !== panoBox) child.remove();
        }
    }

    // Inside .gm-style, hide sibling divs that aren't the canvas path
    const gmStyle = document.querySelector('.gm-style');
    if (!gmStyle) return;
    const canvas = gmStyle.querySelector('canvas');
    if (!canvas) return;
    let current = canvas.parentElement;
    while (current && current !== gmStyle) {
        const parent = current.parentElement;
        if (parent) {
            for (const sibling of parent.children) {
                if (sibling === current) continue;
                if (sibling.tagName === 'DIV' && !sibling.querySelector('canvas')) {
                    sibling.style.setProperty('display', 'none', 'important');
                }
            }
        }
        current = parent;
    }
}"""


# ---------------------------------------------------------------------------
# CaptureDatabase
# ---------------------------------------------------------------------------

class CaptureDatabase:
    """SQLite storage for MapCrunch screenshot metadata."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS captures (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        pano_id         TEXT,
        lat             REAL NOT NULL,
        lng             REAL NOT NULL,
        heading         REAL NOT NULL DEFAULT 0,
        pitch           REAL NOT NULL DEFAULT 0,
        zoom            REAL NOT NULL DEFAULT 0,
        image_path      TEXT,
        width           INTEGER,
        height          INTEGER,
        captured_at     TEXT,
        metadata_json   TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_captures_pano ON captures(pano_id);
    CREATE INDEX IF NOT EXISTS idx_captures_coords ON captures(lat, lng);
    """

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def has_capture(
        self, lat: float, lng: float, heading: float, pitch: float, zoom: float,
    ) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM captures "
            "WHERE lat=? AND lng=? AND heading=? AND pitch=? AND zoom=? "
            "AND image_path IS NOT NULL",
            (lat, lng, heading, pitch, zoom),
        ).fetchone()
        return row is not None

    def insert_capture(
        self,
        pano_id: Optional[str],
        lat: float,
        lng: float,
        heading: float,
        pitch: float,
        zoom: float,
        image_path: str,
        width: int,
        height: int,
        metadata_json: Optional[str] = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            "INSERT INTO captures (pano_id, lat, lng, heading, pitch, zoom, "
            "image_path, width, height, captured_at, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pano_id, lat, lng, heading, pitch, zoom,
             image_path, width, height, now, metadata_json),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# MapCrunchCapture
# ---------------------------------------------------------------------------

class MapCrunchCapture:
    """Headless-Chrome screenshot capture via MapCrunch."""

    def __init__(
        self,
        headless: bool = True,
        viewport_width: int = 1920,
        viewport_height: int = 1080,
        quality: int = 95,
    ) -> None:
        self._headless = headless
        self._vw = viewport_width
        self._vh = viewport_height
        self._quality = quality
        self._playwright: Any = None
        self._browser: Any = None

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-first-run",
            ],
        )
        logger.info("Browser launched (headless=%s)", self._headless)

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def capture_location(
        self,
        lat: float,
        lng: float,
        angles: List[Tuple[float, float, float]],
        image_root: str,
    ) -> List[Dict[str, Any]]:
        """Capture screenshots for one location at multiple view angles.

        *angles* is a list of (heading, pitch, zoom) tuples.
        Returns list of result dicts.
        """
        if not angles:
            return []

        h0, p0, z0 = angles[0]
        url = f"{MAPCRUNCH_BASE}/p/{lat}_{lng}_{h0}_{p0}_{z0}"

        context = await self._browser.new_context(
            viewport={"width": self._vw, "height": self._vh},
            locale="en-US",
            user_agent=CHROME_USER_AGENT,
        )
        page = await context.new_page()
        for pattern in AD_BLOCK_PATTERNS:
            await page.route(pattern, lambda route: route.abort())
        results: List[Dict[str, Any]] = []

        try:
            # Load page
            logger.info("Loading %s", url)
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

            # Wait for Google Maps JS API
            try:
                await page.wait_for_function(
                    "window.google && window.google.maps", timeout=20_000,
                )
            except Exception:
                logger.warning("Google Maps API did not load — skipping location")
                return results

            # Let panorama initialise
            await asyncio.sleep(3)

            # --- overlay removal pipeline ---
            await page.add_style_tag(content=OVERLAY_HIDE_CSS)
            cfg = await page.evaluate(FIND_AND_CONFIGURE_PANORAMA_JS)
            await page.evaluate(DOM_CLEANUP_JS)
            await page.evaluate(RESIZE_PANORAMA_JS)

            if cfg.get("found"):
                logger.info(
                    "Panorama found via %s (pano=%s)",
                    cfg.get("method"), cfg.get("panoId"),
                )
            else:
                logger.warning(
                    "Could not find panorama object — using CSS-only overlay removal"
                )

            # Wait for tiles then settle
            await page.evaluate(WAIT_TILES_JS, 8000)
            await asyncio.sleep(1)

            # Capture all angles — always set POV explicitly
            for heading, pitch, zoom in angles:
                changed = await page.evaluate(SET_POV_JS, [heading, pitch, zoom])
                if changed:
                    await page.evaluate(WAIT_TILES_JS, 5000)
                    await asyncio.sleep(0.5)
                else:
                    # Fallback: full page reload with new URL
                    url2 = f"{MAPCRUNCH_BASE}/p/{lat}_{lng}_{heading}_{pitch}_{zoom}"
                    await page.goto(
                        url2, wait_until="domcontentloaded", timeout=30_000,
                    )
                    try:
                        await page.wait_for_function(
                            "window.google && window.google.maps", timeout=20_000,
                        )
                    except Exception:
                        logger.warning("Reload failed for angle h=%.1f", heading)
                        continue
                    await asyncio.sleep(3)
                    await page.add_style_tag(content=OVERLAY_HIDE_CSS)
                    cfg = await page.evaluate(FIND_AND_CONFIGURE_PANORAMA_JS)
                    await page.evaluate(DOM_CLEANUP_JS)
                    await page.evaluate(RESIZE_PANORAMA_JS)
                    await page.evaluate(WAIT_TILES_JS, 8000)
                    await asyncio.sleep(1)

                r = await self._take_screenshot(
                    page, lat, lng, heading, pitch, zoom, image_root, cfg,
                )
                if r:
                    results.append(r)

        except Exception as e:
            logger.error("Capture failed for (%.6f, %.6f): %s", lat, lng, e)
        finally:
            await context.close()

        return results

    async def _take_screenshot(
        self,
        page: Any,
        lat: float,
        lng: float,
        heading: float,
        pitch: float,
        zoom: float,
        image_root: str,
        cfg: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        state = await page.evaluate(GET_STATE_JS)
        pano_id = None
        if state:
            pano_id = state.get("panoId")
        if not pano_id:
            pano_id = cfg.get("panoId")

        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", pano_id or f"{lat}_{lng}")
        subdir = os.path.join(image_root, safe_id[:12])
        os.makedirs(subdir, exist_ok=True)
        filename = f"{safe_id}_h{heading:.1f}_p{pitch:.1f}_z{zoom:.1f}.jpg"
        filepath = os.path.join(subdir, filename)

        try:
            await page.screenshot(
                path=filepath, type="jpeg", quality=self._quality,
            )
            rel_path = os.path.relpath(filepath, image_root)
            logger.info("Saved %s", rel_path)
            return {
                "pano_id": pano_id,
                "heading": heading,
                "pitch": pitch,
                "zoom": zoom,
                "image_path": rel_path,
                "width": self._vw,
                "height": self._vh,
                "metadata": state,
            }
        except Exception as e:
            logger.error("Screenshot failed: %s", e)
            return None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

@dataclass
class CaptureConfig:
    starting_points: List[Tuple[float, float]] = field(default_factory=list)
    headings: List[float] = field(default_factory=lambda: [0.0])
    pitches: List[float] = field(default_factory=lambda: [0.0])
    zooms: List[float] = field(default_factory=lambda: [0.0])
    db_path: str = "mapcrunch_captures.db"
    image_root: str = "mapcrunch_images"
    viewport_width: int = 1920
    viewport_height: int = 1080
    headless: bool = True
    quality: int = 95
    skip_existing: bool = True


class MapCrunchOrchestrator:
    """Top-level coordinator for batch MapCrunch screenshot capture."""

    def __init__(self, config: CaptureConfig) -> None:
        self.config = config
        self.db = CaptureDatabase(config.db_path)
        self.capture = MapCrunchCapture(
            headless=config.headless,
            viewport_width=config.viewport_width,
            viewport_height=config.viewport_height,
            quality=config.quality,
        )

    async def run(self) -> None:
        os.makedirs(self.config.image_root, exist_ok=True)
        await self.capture.start()

        total_captures = 0
        total_skipped = 0
        total_errors = 0
        t_start = time.time()

        try:
            for i, (lat, lng) in enumerate(self.config.starting_points, 1):
                logger.info(
                    "Location %d/%d: (%.6f, %.6f)",
                    i, len(self.config.starting_points), lat, lng,
                )

                # Build angle combinations, skipping already-captured
                angles: List[Tuple[float, float, float]] = []
                for h in self.config.headings:
                    for p in self.config.pitches:
                        for z in self.config.zooms:
                            if self.config.skip_existing and self.db.has_capture(
                                lat, lng, h, p, z,
                            ):
                                total_skipped += 1
                                continue
                            angles.append((h, p, z))

                if not angles:
                    logger.info("  All angles already captured — skipping")
                    continue

                logger.info("  Capturing %d angle combinations", len(angles))
                results = await self.capture.capture_location(
                    lat, lng, angles, self.config.image_root,
                )

                for r in results:
                    self.db.insert_capture(
                        pano_id=r["pano_id"],
                        lat=lat,
                        lng=lng,
                        heading=r["heading"],
                        pitch=r["pitch"],
                        zoom=r["zoom"],
                        image_path=r["image_path"],
                        width=r["width"],
                        height=r["height"],
                        metadata_json=(
                            json.dumps(r["metadata"]) if r.get("metadata") else None
                        ),
                    )
                    total_captures += 1

                missed = len(angles) - len(results)
                if missed > 0:
                    total_errors += missed

        except KeyboardInterrupt:
            logger.info("Interrupted — progress saved in %s", self.config.db_path)
        except Exception as e:
            logger.exception("Orchestrator failed: %s", e)
        finally:
            dur = time.time() - t_start
            mins, secs = divmod(dur, 60)
            logger.info("=== CAPTURE SUMMARY ===")
            logger.info("  Locations        : %d", len(self.config.starting_points))
            logger.info("  Screenshots taken: %d", total_captures)
            logger.info("  Skipped (exist)  : %d", total_skipped)
            logger.info("  Errors           : %d", total_errors)
            logger.info("  Total time       : %dm %.1fs", int(mins), secs)
            if total_captures > 0:
                logger.info("  Avg time/capture : %.1fs", dur / total_captures)

            await self.capture.close()
            self.db.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_coord(s: str) -> Tuple[float, float]:
    parts = s.strip().split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(
            f"Invalid coordinate '{s}'. Expected format: lat,lng"
        )
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Invalid coordinate '{s}'. lat and lng must be numbers."
        )


def _load_coords_file(path: str) -> List[Tuple[float, float]]:
    coords: List[Tuple[float, float]] = []
    with open(path) as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = re.split(r"[,\s\t]+", line)
            if len(parts) < 2:
                logger.warning("Skipping line %d: '%s'", line_num, line)
                continue
            try:
                coords.append((float(parts[0]), float(parts[1])))
            except ValueError:
                logger.warning("Skipping line %d: '%s'", line_num, line)
    return coords


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture clean Street View screenshots via MapCrunch",
    )

    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "--coords", nargs="+", metavar="LAT,LNG",
        help="Starting coordinates as lat,lng pairs",
    )
    input_group.add_argument(
        "--coords-file", type=str,
        help="Path to file with one lat,lng pair per line",
    )

    parser.add_argument(
        "--headings", nargs="+", type=float, default=[0.0],
        help="Heading angles in degrees (default: 0)",
    )
    parser.add_argument(
        "--pitches", nargs="+", type=float, default=[0.0],
        help="Pitch angles in degrees (default: 0)",
    )
    parser.add_argument(
        "--zooms", nargs="+", type=float, default=[0.0],
        help="Zoom levels (default: 0)",
    )
    parser.add_argument(
        "--db", type=str, default="mapcrunch_captures.db",
        help="SQLite database path (default: mapcrunch_captures.db)",
    )
    parser.add_argument(
        "--image-dir", type=str, default="mapcrunch_images",
        help="Root directory for screenshots (default: mapcrunch_images)",
    )
    parser.add_argument(
        "--viewport", type=str, default="1920x1080",
        help="Viewport WxH in pixels (default: 1920x1080)",
    )
    parser.add_argument(
        "--quality", type=int, default=95,
        help="JPEG quality 1-100 (default: 95)",
    )
    parser.add_argument(
        "--no-headless", action="store_true",
        help="Run browser in visible mode (for debugging)",
    )
    parser.add_argument(
        "--no-skip-existing", action="store_true",
        help="Re-capture even if screenshot already exists in DB",
    )

    args = parser.parse_args()

    starting_points: List[Tuple[float, float]] = []
    if args.coords:
        starting_points = [_parse_coord(c) for c in args.coords]
    elif args.coords_file:
        starting_points = _load_coords_file(args.coords_file)

    try:
        vw_s, vh_s = args.viewport.lower().split("x")
        viewport_width, viewport_height = int(vw_s), int(vh_s)
    except ValueError:
        parser.error(
            f"Invalid viewport '{args.viewport}'. Expected format: WIDTHxHEIGHT"
        )

    config = CaptureConfig(
        starting_points=starting_points,
        headings=args.headings,
        pitches=args.pitches,
        zooms=args.zooms,
        db_path=args.db,
        image_root=args.image_dir,
        viewport_width=viewport_width,
        viewport_height=viewport_height,
        headless=not args.no_headless,
        quality=args.quality,
        skip_existing=not args.no_skip_existing,
    )

    orchestrator = MapCrunchOrchestrator(config)
    asyncio.run(orchestrator.run())


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    main()
