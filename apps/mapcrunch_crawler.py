"""MapCrunch Street View screenshot capture with BFS neighbor discovery.

Two-phase pipeline:
  1. BFS Discovery — uses Google Tiles API to find all panoramas reachable
     within --max-depth steps from each starting coordinate.  Stores panorama
     metadata and neighbor links in SQLite.
  2. Screenshot Capture — for every discovered panorama, opens MapCrunch in
     headless Chrome and captures clean Street View screenshots at each
     heading / pitch / zoom combination.

Usage:
    python -m apps.mapcrunch_crawler --coords 29.958574,-90.065712
    python -m apps.mapcrunch_crawler --coords-file locations.csv
    python -m apps.mapcrunch_crawler --coords 37.7749,-122.4194 --headings 0 90 180 270
    python -m apps.mapcrunch_crawler --resume --db mapcrunch.db

Prerequisites:
    pip install playwright requests tenacity
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
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.crawler import TilesAPIClient

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
# Database
# ---------------------------------------------------------------------------

class CaptureDatabase:
    """SQLite storage for BFS graph + screenshot capture tracking."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS crawl_jobs (
        job_id          INTEGER PRIMARY KEY AUTOINCREMENT,
        start_lat       REAL NOT NULL,
        start_lng       REAL NOT NULL,
        max_depth       INTEGER NOT NULL DEFAULT 50,
        status          TEXT NOT NULL DEFAULT 'pending',
        started_at      TEXT,
        completed_at    TEXT,
        panos_discovered INTEGER NOT NULL DEFAULT 0,
        panos_captured  INTEGER NOT NULL DEFAULT 0,
        error_message   TEXT
    );

    CREATE TABLE IF NOT EXISTS panoramas (
        pano_id         TEXT PRIMARY KEY,
        lat             REAL,
        lng             REAL,
        date            TEXT,
        metadata_json   TEXT,
        job_id          INTEGER,
        bfs_depth       INTEGER,
        capture_status  TEXT NOT NULL DEFAULT 'pending',
        FOREIGN KEY (job_id) REFERENCES crawl_jobs(job_id)
    );

    CREATE TABLE IF NOT EXISTS panorama_links (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        from_pano_id    TEXT NOT NULL,
        to_pano_id      TEXT NOT NULL,
        heading         REAL NOT NULL,
        description     TEXT,
        link_date       TEXT,
        UNIQUE(from_pano_id, to_pano_id)
    );

    CREATE TABLE IF NOT EXISTS captures (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        pano_id         TEXT NOT NULL,
        heading         REAL NOT NULL,
        pitch           REAL NOT NULL,
        zoom            REAL NOT NULL,
        image_path      TEXT,
        width           INTEGER,
        height          INTEGER,
        captured_at     TEXT,
        UNIQUE(pano_id, heading, pitch, zoom)
    );

    CREATE INDEX IF NOT EXISTS idx_panoramas_job ON panoramas(job_id);
    CREATE INDEX IF NOT EXISTS idx_panoramas_status ON panoramas(capture_status);
    CREATE INDEX IF NOT EXISTS idx_links_from ON panorama_links(from_pano_id);
    CREATE INDEX IF NOT EXISTS idx_links_to ON panorama_links(to_pano_id);
    CREATE INDEX IF NOT EXISTS idx_captures_pano ON captures(pano_id);
    """

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- crawl_jobs --

    def create_job(self, lat: float, lng: float, max_depth: int) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            "INSERT INTO crawl_jobs (start_lat, start_lng, max_depth, status, started_at) "
            "VALUES (?, ?, ?, 'running', ?)",
            (lat, lng, max_depth, now),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def update_job(self, job_id: int, **kwargs: Any) -> None:
        if not kwargs:
            return
        sets = []
        vals: list = []
        for k, v in kwargs.items():
            sets.append(f"{k} = ?")
            vals.append(v)
        vals.append(job_id)
        self._conn.execute(
            f"UPDATE crawl_jobs SET {', '.join(sets)} WHERE job_id = ?", vals,
        )
        self._conn.commit()

    def find_existing_job(self, lat: float, lng: float) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM crawl_jobs WHERE start_lat = ? AND start_lng = ? "
            "ORDER BY job_id DESC LIMIT 1",
            (lat, lng),
        ).fetchone()
        return dict(row) if row else None

    def get_running_jobs(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM crawl_jobs WHERE status IN ('running', 'discovering', 'capturing')"
        ).fetchall()
        return [dict(r) for r in rows]

    # -- panoramas --

    def has_panorama(self, pano_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM panoramas WHERE pano_id = ?", (pano_id,)
        ).fetchone()
        return row is not None

    def insert_panorama(
        self,
        pano_id: str,
        lat: Optional[float],
        lng: Optional[float],
        date: Optional[str],
        metadata_json: str,
        job_id: int,
        bfs_depth: int,
    ) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO panoramas "
            "(pano_id, lat, lng, date, metadata_json, job_id, bfs_depth) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pano_id, lat, lng, date, metadata_json, job_id, bfs_depth),
        )
        self._conn.commit()

    def mark_pano_captured(self, pano_id: str) -> None:
        self._conn.execute(
            "UPDATE panoramas SET capture_status = 'done' WHERE pano_id = ?",
            (pano_id,),
        )
        self._conn.commit()

    def mark_pano_failed(self, pano_id: str) -> None:
        self._conn.execute(
            "UPDATE panoramas SET capture_status = 'failed' WHERE pano_id = ?",
            (pano_id,),
        )
        self._conn.commit()

    def get_pending_panos(self, job_id: int) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM panoramas WHERE job_id = ? AND capture_status = 'pending' "
            "ORDER BY bfs_depth",
            (job_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_visited_pano_ids(self, job_id: int) -> Set[str]:
        rows = self._conn.execute(
            "SELECT pano_id FROM panoramas WHERE job_id = ?", (job_id,)
        ).fetchall()
        return {r["pano_id"] for r in rows}

    def get_unvisited_neighbors(self, job_id: int) -> List[Tuple[str, int]]:
        rows = self._conn.execute(
            """
            SELECT DISTINCT pl.to_pano_id, p.bfs_depth + 1 AS next_depth
            FROM panorama_links pl
            JOIN panoramas p ON p.pano_id = pl.from_pano_id AND p.job_id = ?
            WHERE pl.to_pano_id NOT IN (
                SELECT pano_id FROM panoramas WHERE job_id = ?
            )
            """,
            (job_id, job_id),
        ).fetchall()
        return [(r["to_pano_id"], r["next_depth"]) for r in rows]

    def count_panos(self, job_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM panoramas WHERE job_id = ?", (job_id,)
        ).fetchone()
        return row["cnt"] if row else 0

    # -- links --

    def insert_links(self, from_pano_id: str, links: List[Dict[str, Any]]) -> None:
        rows = []
        for link in links:
            to_id = link.get("panoId")
            if not to_id:
                continue
            rows.append((
                from_pano_id,
                to_id,
                link.get("heading", 0.0),
                link.get("description", link.get("text", "")),
                link.get("date"),
            ))
        if not rows:
            return
        self._conn.executemany(
            "INSERT OR IGNORE INTO panorama_links "
            "(from_pano_id, to_pano_id, heading, description, link_date) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()

    # -- captures --

    def has_capture(
        self, pano_id: str, heading: float, pitch: float, zoom: float,
    ) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM captures "
            "WHERE pano_id=? AND heading=? AND pitch=? AND zoom=? "
            "AND image_path IS NOT NULL",
            (pano_id, heading, pitch, zoom),
        ).fetchone()
        return row is not None

    def insert_capture(
        self,
        pano_id: str,
        heading: float,
        pitch: float,
        zoom: float,
        image_path: str,
        width: int,
        height: int,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            "INSERT OR REPLACE INTO captures "
            "(pano_id, heading, pitch, zoom, image_path, width, height, captured_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pano_id, heading, pitch, zoom, image_path, width, height, now),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def count_captures(self, pano_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM captures WHERE pano_id = ? AND image_path IS NOT NULL",
            (pano_id,),
        ).fetchone()
        return row["cnt"] if row else 0


# ---------------------------------------------------------------------------
# BFS Discovery (Phase 1)
# ---------------------------------------------------------------------------

def _is_user_pano(metadata: Dict[str, Any]) -> bool:
    """Return True if the panorama appears to be user-contributed (not official Google)."""
    copyright_str = metadata.get("copyright", "")
    return "Google" not in copyright_str


def _find_official_pano(
    tiles_client: TilesAPIClient,
    lat: float,
    lng: float,
    api_key: str,
) -> Optional[Dict[str, Any]]:
    """Try to find an official Google Street View pano at the given coordinates.

    Uses the Street View Static Metadata API with source=outdoor, then looks
    up the full metadata via the Tiles API if found.
    """
    import requests as _requests

    try:
        resp = _requests.get(
            "https://maps.googleapis.com/maps/api/streetview/metadata",
            params={
                "key": api_key,
                "location": f"{lat},{lng}",
                "source": "outdoor",
                "radius": 50,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "OK":
            return None
        official_pano_id = data.get("pano_id")
        if not official_pano_id:
            return None
        # Fetch full metadata via Tiles API
        return tiles_client.get_metadata(pano_id=official_pano_id)
    except Exception as e:
        logger.debug("Official pano lookup failed at (%.6f, %.6f): %s", lat, lng, e)
        return None


def bfs_discover(
    tiles_client: TilesAPIClient,
    db: CaptureDatabase,
    start_lat: float,
    start_lng: float,
    job_id: int,
    max_depth: int,
    visited: Optional[Set[str]] = None,
    initial_queue: Optional[List[Tuple[str, int]]] = None,
    api_key: Optional[str] = None,
) -> int:
    """BFS traverse the Street View graph, storing metadata + links.

    Returns the number of newly discovered panoramas.
    """
    if visited is None:
        visited = set()

    queue: deque[Tuple[str, int]] = deque()
    discovered = 0
    errors = 0

    if initial_queue:
        queue.extend(initial_queue)
    else:
        logger.info("Looking up panorama at (%.6f, %.6f)", start_lat, start_lng)
        try:
            meta = tiles_client.get_metadata(lat=start_lat, lng=start_lng)
        except Exception as e:
            logger.error("No panorama at (%.6f, %.6f): %s", start_lat, start_lng, e)
            return 0
        start_pano = meta.get("panoId")
        if not start_pano:
            logger.error("No panorama found at starting coordinates")
            return 0
        queue.append((start_pano, 0))

    while queue:
        pano_id, depth = queue.popleft()

        if pano_id in visited:
            continue

        try:
            metadata = tiles_client.get_metadata(pano_id=pano_id)
        except Exception as e:
            logger.warning("Failed to get metadata for %s: %s", pano_id, e)
            errors += 1
            continue

        actual_pano_id = metadata.get("panoId")
        if not actual_pano_id:
            errors += 1
            continue

        if actual_pano_id != pano_id:
            logger.debug("Redirect: %s -> %s", pano_id, actual_pano_id)
            if actual_pano_id in visited:
                continue
            pano_id = actual_pano_id

        # If this is a user-contributed pano, try to find the official one
        # at the same coordinates (official panos have neighbor links).
        if _is_user_pano(metadata) and api_key:
            lat = metadata.get("lat")
            lng = metadata.get("lng")
            if lat is not None and lng is not None:
                official = _find_official_pano(tiles_client, lat, lng, api_key)
                if official and official.get("panoId"):
                    official_id = official["panoId"]
                    if official_id not in visited:
                        logger.info(
                            "Replacing user pano %s with official %s",
                            pano_id, official_id,
                        )
                        metadata = official
                        pano_id = official_id

        visited.add(pano_id)

        db.insert_panorama(
            pano_id=pano_id,
            lat=metadata.get("lat"),
            lng=metadata.get("lng"),
            date=metadata.get("date"),
            metadata_json=json.dumps(metadata),
            job_id=job_id,
            bfs_depth=depth,
        )
        discovered += 1

        links = metadata.get("links") or []
        db.insert_links(pano_id, links)

        if depth < max_depth:
            for link in links:
                neighbor_id = link.get("panoId")
                if neighbor_id and neighbor_id not in visited:
                    queue.append((neighbor_id, depth + 1))

        if discovered % 10 == 0:
            db.update_job(job_id, panos_discovered=len(visited))
            logger.info(
                "Discovery: %d panos (depth %d/%d), queue=%d, errors=%d",
                discovered, depth, max_depth, len(queue), errors,
            )

    db.update_job(job_id, panos_discovered=len(visited))
    return discovered


# ---------------------------------------------------------------------------
# MapCrunch Screenshot Capture (Phase 2)
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

    async def capture_pano(
        self,
        pano_id: str,
        lat: float,
        lng: float,
        angles: List[Tuple[float, float, float]],
        image_root: str,
    ) -> List[Dict[str, Any]]:
        """Capture screenshots for one panorama at multiple view angles.

        Images are saved to <image_root>/<pano_id>/<heading>_<pitch>_<zoom>.jpg

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
            logger.info("Loading %s", url)
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

            try:
                await page.wait_for_function(
                    "window.google && window.google.maps", timeout=20_000,
                )
            except Exception:
                logger.warning("Google Maps API did not load — skipping pano %s", pano_id)
                return results

            await asyncio.sleep(3)

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

            await page.evaluate(WAIT_TILES_JS, 8000)
            await asyncio.sleep(1)

            # Capture all angles
            for heading, pitch, zoom in angles:
                changed = await page.evaluate(SET_POV_JS, [heading, pitch, zoom])
                if changed:
                    await page.evaluate(WAIT_TILES_JS, 1500)
                    await asyncio.sleep(0.3)
                else:
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
                    page, pano_id, heading, pitch, zoom, image_root,
                )
                if r:
                    results.append(r)

        except Exception as e:
            logger.error("Capture failed for pano %s: %s", pano_id, e)
        finally:
            await context.close()

        return results

    async def _take_screenshot(
        self,
        page: Any,
        pano_id: str,
        heading: float,
        pitch: float,
        zoom: float,
        image_root: str,
    ) -> Optional[Dict[str, Any]]:
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", pano_id)
        subdir = os.path.join(image_root, safe_id)
        os.makedirs(subdir, exist_ok=True)

        filename = f"{heading:.1f}_{pitch:.1f}_{zoom:.1f}.jpg"
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
    max_depth: int = 50
    headings: List[float] = field(default_factory=lambda: [float(i) for i in range(0, 360, 10)])
    pitches: List[float] = field(default_factory=lambda: [float(i) for i in range(-40, 50, 10)])
    zooms: List[float] = field(default_factory=lambda: [0.0, 1.0, 1.5, 2.0, 3.0])
    db_path: str = "mapcrunch.db"
    image_root: str = "mapcrunch_images"
    viewport_width: int = 1920
    viewport_height: int = 1080
    headless: bool = True
    quality: int = 95
    skip_existing: bool = True
    resume: bool = False
    api_key: Optional[str] = None


class MapCrunchOrchestrator:
    """Two-phase pipeline: BFS discovery then MapCrunch screenshot capture."""

    def __init__(self, config: CaptureConfig) -> None:
        self.config = config
        self.db = CaptureDatabase(config.db_path)

        api_key = config.api_key or os.getenv("GOOGLE_MAPS_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "GOOGLE_MAPS_API_KEY not provided and not set in environment"
            )
        self.tiles_client = TilesAPIClient(api_key)

        self.capture = MapCrunchCapture(
            headless=config.headless,
            viewport_width=config.viewport_width,
            viewport_height=config.viewport_height,
            quality=config.quality,
        )

    async def run(self) -> None:
        os.makedirs(self.config.image_root, exist_ok=True)
        t_start = time.time()

        total_discovered = 0
        total_captured = 0
        total_skipped = 0
        total_errors = 0

        try:
            # --- Resume interrupted jobs ---
            if self.config.resume:
                for job in self.db.get_running_jobs():
                    job_id = job["job_id"]
                    lat, lng = job["start_lat"], job["start_lng"]
                    max_depth = job["max_depth"]
                    logger.info("Resuming job %d at (%.6f, %.6f)", job_id, lat, lng)

                    # Continue BFS if there are unvisited neighbors
                    visited = self.db.get_visited_pano_ids(job_id)
                    frontier = [
                        (pid, d) for pid, d in self.db.get_unvisited_neighbors(job_id)
                        if d <= max_depth
                    ]
                    if frontier:
                        n = bfs_discover(
                            self.tiles_client, self.db,
                            lat, lng, job_id, max_depth,
                            visited=visited, initial_queue=frontier,
                            api_key=self.config.api_key or os.getenv("GOOGLE_MAPS_API_KEY", ""),
                        )
                        total_discovered += n

                    # Capture pending panos
                    captured, skipped, errors = await self._capture_job(job_id)
                    total_captured += captured
                    total_skipped += skipped
                    total_errors += errors

                    self.db.update_job(
                        job_id, status="completed",
                        completed_at=datetime.now(timezone.utc).isoformat(),
                        panos_captured=self.db.count_panos(job_id) - len(self.db.get_pending_panos(job_id)),
                    )

            # --- Process each starting point ---
            for i, (lat, lng) in enumerate(self.config.starting_points, 1):
                logger.info(
                    "=== Starting point %d/%d: (%.6f, %.6f) ===",
                    i, len(self.config.starting_points), lat, lng,
                )

                # Check for existing job
                existing = self.db.find_existing_job(lat, lng)
                if existing and existing["status"] == "completed":
                    # Re-use existing BFS, just capture any pending panos
                    job_id = existing["job_id"]
                    pending = self.db.get_pending_panos(job_id)
                    if not pending:
                        logger.info("Job %d already complete — skipping", job_id)
                        continue
                    logger.info(
                        "Job %d has %d pending panos — capturing", job_id, len(pending),
                    )
                else:
                    job_id = self.db.create_job(lat, lng, self.config.max_depth)

                    # Phase 1: BFS Discovery
                    logger.info("Phase 1: BFS discovery (max_depth=%d)", self.config.max_depth)
                    self.db.update_job(job_id, status="discovering")
                    t0 = time.time()
                    n = bfs_discover(
                        self.tiles_client, self.db,
                        lat, lng, job_id, self.config.max_depth,
                        api_key=self.config.api_key or os.getenv("GOOGLE_MAPS_API_KEY", ""),
                    )
                    total_discovered += n
                    logger.info(
                        "Discovery complete: %d panoramas in %.1fs",
                        n, time.time() - t0,
                    )

                # Phase 2: MapCrunch Capture
                logger.info("Phase 2: MapCrunch screenshot capture")
                self.db.update_job(job_id, status="capturing")
                captured, skipped, errors = await self._capture_job(job_id)
                total_captured += captured
                total_skipped += skipped
                total_errors += errors

                self.db.update_job(
                    job_id, status="completed",
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    panos_captured=captured,
                )

        except KeyboardInterrupt:
            logger.info("Interrupted — progress saved in %s. Use --resume to continue.", self.config.db_path)
        except Exception as e:
            logger.exception("Orchestrator failed: %s", e)
        finally:
            dur = time.time() - t_start
            mins, secs = divmod(dur, 60)
            logger.info("=== SUMMARY ===")
            logger.info("  Panos discovered : %d", total_discovered)
            logger.info("  Screenshots taken: %d", total_captured)
            logger.info("  Skipped (exist)  : %d", total_skipped)
            logger.info("  Errors           : %d", total_errors)
            logger.info("  Total time       : %dm %.1fs", int(mins), secs)

            await self.capture.close()
            self.tiles_client.close()
            self.db.close()

    async def _capture_job(self, job_id: int) -> Tuple[int, int, int]:
        """Capture all pending panoramas for a job.

        Returns (captured, skipped, errors).
        """
        pending = self.db.get_pending_panos(job_id)
        if not pending:
            return 0, 0, 0

        # Lazily start browser only when we need it
        if not self.capture._browser:
            await self.capture.start()

        captured = 0
        skipped = 0
        errors = 0
        total = len(pending)

        for idx, pano in enumerate(pending, 1):
            pano_id = pano["pano_id"]
            lat = pano["lat"]
            lng = pano["lng"]

            if lat is None or lng is None:
                logger.warning("Pano %s has no coordinates — skipping", pano_id)
                self.db.mark_pano_failed(pano_id)
                errors += 1
                continue

            # Build angle list, skipping already-captured
            angles: List[Tuple[float, float, float]] = []
            for h in self.config.headings:
                for p in self.config.pitches:
                    for z in self.config.zooms:
                        if self.config.skip_existing and self.db.has_capture(
                            pano_id, h, p, z,
                        ):
                            skipped += 1
                            continue
                        angles.append((h, p, z))

            if not angles:
                logger.info(
                    "[%d/%d] Pano %s: all angles already captured", idx, total, pano_id,
                )
                self.db.mark_pano_captured(pano_id)
                continue

            logger.info(
                "[%d/%d] Pano %s: capturing %d angles", idx, total, pano_id, len(angles),
            )
            results = await self.capture.capture_pano(
                pano_id, lat, lng, angles, self.config.image_root,
            )

            for r in results:
                self.db.insert_capture(
                    pano_id=r["pano_id"],
                    heading=r["heading"],
                    pitch=r["pitch"],
                    zoom=r["zoom"],
                    image_path=r["image_path"],
                    width=r["width"],
                    height=r["height"],
                )
                captured += 1

            missed = len(angles) - len(results)
            if missed > 0:
                errors += missed

            if len(results) > 0:
                self.db.mark_pano_captured(pano_id)
            else:
                self.db.mark_pano_failed(pano_id)

        return captured, skipped, errors


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
        description="BFS discovery + MapCrunch Street View screenshot capture",
    )

    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument(
        "--coords", nargs="+", metavar="LAT,LNG",
        help="Starting coordinates as lat,lng pairs",
    )
    input_group.add_argument(
        "--coords-file", type=str,
        help="Path to file with one lat,lng pair per line",
    )

    parser.add_argument(
        "--max-depth", type=int, default=50,
        help="Max BFS depth from each starting point (default: 50)",
    )
    parser.add_argument(
        "--headings", nargs="+", type=float, default=[float(i) for i in range(0, 360, 10)],
        help="Heading angles in degrees (default: every 10 deg from 0-350)",
    )
    parser.add_argument(
        # @HuanzhiMao TODO: Change to -40 to 50?
        "--pitches", nargs="+", type=float, default=[float(i) for i in range(-30, 40, 10)],
        help="Pitch angles in degrees (default: every 10 deg from -40 to +40)",
    )
    parser.add_argument(
        "--zooms", nargs="+", type=float, default=[0.0, 1.0, 1.5, 2.0, 3.0],
        help="Zoom levels (default: 0 1 1.5 2 3)",
    )
    parser.add_argument(
        "--db", type=str, default="mapcrunch.db",
        help="SQLite database path (default: mapcrunch.db)",
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
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume interrupted jobs from the database",
    )

    args = parser.parse_args()

    starting_points: List[Tuple[float, float]] = []
    if args.coords:
        starting_points = [_parse_coord(c) for c in args.coords]
    elif args.coords_file:
        starting_points = _load_coords_file(args.coords_file)

    if not starting_points and not args.resume:
        parser.error("Either --coords, --coords-file, or --resume is required")

    try:
        vw_s, vh_s = args.viewport.lower().split("x")
        viewport_width, viewport_height = int(vw_s), int(vh_s)
    except ValueError:
        parser.error(
            f"Invalid viewport '{args.viewport}'. Expected format: WIDTHxHEIGHT"
        )

    config = CaptureConfig(
        starting_points=starting_points,
        max_depth=args.max_depth,
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
        resume=args.resume,
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
