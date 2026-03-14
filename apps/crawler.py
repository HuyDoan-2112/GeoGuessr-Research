"""Street View BFS crawler — download panorama tiles + metadata to local SQLite.

Usage:
    python -m apps.crawler --coords 37.7749,-122.4194
    python -m apps.crawler --coords-file locations.csv --max-steps 50
    python -m apps.crawler --resume --db crawl.db
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.streetview_js.client import StreetViewHostClient
from core.utils.equirect import perspectives_to_equirect
from core.utils.image_utils import zoom_to_fov

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CrawlDatabase
# ---------------------------------------------------------------------------

class CrawlDatabase:
    """SQLite wrapper for crawl metadata and panorama storage."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS crawl_jobs (
        job_id          INTEGER PRIMARY KEY AUTOINCREMENT,
        start_lat       REAL NOT NULL,
        start_lng       REAL NOT NULL,
        max_steps       INTEGER NOT NULL DEFAULT 100,
        status          TEXT NOT NULL DEFAULT 'pending',
        started_at      TEXT,
        completed_at    TEXT,
        panos_visited   INTEGER NOT NULL DEFAULT 0,
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
        equirect_path   TEXT,
        equirect_width  INTEGER,
        equirect_height INTEGER,
        tile_zoom       INTEGER DEFAULT 3,
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

    CREATE INDEX IF NOT EXISTS idx_panoramas_job ON panoramas(job_id);
    CREATE INDEX IF NOT EXISTS idx_links_from ON panorama_links(from_pano_id);
    CREATE INDEX IF NOT EXISTS idx_links_to ON panorama_links(to_pano_id);
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- crawl_jobs --

    def create_job(self, lat: float, lng: float, max_steps: int) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO crawl_jobs (start_lat, start_lng, max_steps, status, started_at) "
                "VALUES (?, ?, ?, 'running', ?)",
                (lat, lng, max_steps, now),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def update_job_status(
        self, job_id: int, status: str, **kwargs: Any
    ) -> None:
        sets = ["status = ?"]
        vals: list = [status]
        if status in ("completed", "failed"):
            sets.append("completed_at = ?")
            vals.append(datetime.now(timezone.utc).isoformat())
        for k, v in kwargs.items():
            sets.append(f"{k} = ?")
            vals.append(v)
        vals.append(job_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE crawl_jobs SET {', '.join(sets)} WHERE job_id = ?",
                vals,
            )
            self._conn.commit()

    def get_running_jobs(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM crawl_jobs WHERE status = 'running'"
        ).fetchall()
        return [dict(r) for r in rows]

    # -- panoramas --

    def has_panorama(self, pano_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM panoramas WHERE pano_id = ?", (pano_id,)
        ).fetchone()
        return row is not None

    def has_equirect(self, pano_id: str) -> bool:
        row = self._conn.execute(
            "SELECT equirect_path FROM panoramas WHERE pano_id = ?", (pano_id,)
        ).fetchone()
        return row is not None and row["equirect_path"] is not None

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
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO panoramas "
                "(pano_id, lat, lng, date, metadata_json, job_id, bfs_depth) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (pano_id, lat, lng, date, metadata_json, job_id, bfs_depth),
            )
            self._conn.commit()

    def update_equirect(
        self, pano_id: str, path: str, width: int, height: int, zoom: int
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE panoramas SET equirect_path=?, equirect_width=?, "
                "equirect_height=?, tile_zoom=? WHERE pano_id=?",
                (path, width, height, zoom, pano_id),
            )
            self._conn.commit()

    def get_panorama(self, pano_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM panoramas WHERE pano_id = ?", (pano_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_visited_pano_ids(self, job_id: int) -> Set[str]:
        rows = self._conn.execute(
            "SELECT pano_id FROM panoramas WHERE job_id = ?", (job_id,)
        ).fetchall()
        return {r["pano_id"] for r in rows}

    def find_nearest_pano(self, lat: float, lng: float) -> Optional[Dict[str, Any]]:
        """Find the panorama closest to (lat, lng) using Euclidean distance."""
        row = self._conn.execute(
            "SELECT *, (lat - ?) * (lat - ?) + (lng - ?) * (lng - ?) AS dist "
            "FROM panoramas WHERE lat IS NOT NULL AND lng IS NOT NULL "
            "ORDER BY dist LIMIT 1",
            (lat, lat, lng, lng),
        ).fetchone()
        return dict(row) if row else None

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
                link.get("description", ""),
                link.get("date"),
            ))
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO panorama_links "
                "(from_pano_id, to_pano_id, heading, description, link_date) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()

    def get_links(self, pano_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM panorama_links WHERE from_pano_id = ?", (pano_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_unvisited_neighbors(self, job_id: int) -> List[Tuple[str, int]]:
        """For resume: find (pano_id, depth+1) of unvisited neighbours."""
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


# ---------------------------------------------------------------------------
# ScreenshotCapture — captures panoramas via Playwright browser screenshots
# ---------------------------------------------------------------------------

# Default screenshot grid: 8 headings × 5 pitches = 40 shots per panorama.
# With a square 800×800 viewport at zoom 1, HFOV = VFOV = 90° (rectilinear).
# 8 headings every 45° covers 360° with 50% horizontal overlap.
# 5 pitches from -80° to +80° covers full vertical with generous overlap.
DEFAULT_HEADINGS = [0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0]
DEFAULT_PITCHES = [-80.0, -40.0, 0.0, 40.0, 80.0]

# Empirically verified: Google Maps JS StreetViewPanorama at zoom=1 uses
# rectilinear (pinhole) projection with ~90° FOV on a square viewport.
GMAPS_FOV = 90.0


class ScreenshotCapture:
    """Captures full equirectangular panoramas via Playwright browser screenshots.

    Instead of downloading raw tiles (private Google API), this uses the
    authenticated Google Maps JS API session in the Playwright browser to
    render Street View at multiple heading/pitch angles, then stitches the
    screenshots into an equirectangular image using inverse projection.
    """

    def __init__(
        self,
        client: StreetViewHostClient,
        session_id: str,
        image_root: str,
        headings: Optional[List[float]] = None,
        pitches: Optional[List[float]] = None,
        screenshot_zoom: float = 1.0,
        equirect_width: int = 8192,
        equirect_height: int = 4096,
        quality: int = 95,
        screenshot_format: str = "jpeg",
    ) -> None:
        self.client = client
        self.session_id = session_id
        self.image_root = image_root
        self.headings = headings or DEFAULT_HEADINGS
        self.pitches = pitches or DEFAULT_PITCHES
        self.screenshot_zoom = screenshot_zoom
        self.equirect_width = equirect_width
        self.equirect_height = equirect_height
        self.quality = quality
        self.screenshot_format = screenshot_format
        # FOV for the zoom level used during screenshots
        self.fov = GMAPS_FOV
        self.last_equirect_array: Optional[np.ndarray] = None
        self.last_views: Optional[list] = None

    def shutdown(self) -> None:
        pass  # no thread pool to clean up

    def capture_equirectangular(self, pano_id: str) -> Tuple[str, int, int]:
        """Capture screenshots at multiple angles and stitch into equirectangular.

        The client must already be on the correct panorama (set_pano called).

        Returns (relative_path, width, height).
        """
        views: list[tuple[np.ndarray, float, float, float]] = []

        for pitch in self.pitches:
            for heading in self.headings:
                # Set POV and wait for render
                self.client.set_pov(
                    self.session_id,
                    heading=heading,
                    pitch=pitch,
                    zoom=self.screenshot_zoom,
                )
                self.client.wait_for_stable(self.session_id)
                # Wait for rendering to fully settle (Google Maps animates POV changes)
                time.sleep(0.4)

                # Take screenshot
                img_b64 = self.client.screenshot(
                    self.session_id, quality=self.quality,
                    fmt=self.screenshot_format,
                )
                img_bytes = base64.b64decode(img_b64)
                img = np.array(Image.open(BytesIO(img_bytes)).convert("RGB"))
                views.append((img, heading, pitch, self.fov))

        self.last_views = views

        logger.info(
            f"Stitching {len(views)} screenshots into "
            f"{self.equirect_width}x{self.equirect_height} equirectangular"
        )
        equirect = perspectives_to_equirect(
            views, self.equirect_width, self.equirect_height
        )
        self.last_equirect_array = equirect

        # Save
        safe_pano = re.sub(r"[^A-Za-z0-9_-]", "_", pano_id)
        subdir = os.path.join(self.image_root, safe_pano[:8])
        os.makedirs(subdir, exist_ok=True)
        filename = f"{safe_pano}_equirect.jpg"
        file_path = os.path.join(subdir, filename)
        Image.fromarray(equirect).save(file_path, format="JPEG", quality=self.quality)

        rel_path = os.path.relpath(file_path, self.image_root)
        return rel_path, self.equirect_width, self.equirect_height


# ---------------------------------------------------------------------------
# BFSCrawler
# ---------------------------------------------------------------------------

@dataclass
class CrawlStats:
    panos_visited: int = 0
    tiles_downloaded: int = 0
    errors: int = 0


class BFSCrawler:
    """BFS traversal of Street View panorama graph."""

    def __init__(
        self,
        client: StreetViewHostClient,
        session_id: str,
        db: CrawlDatabase,
        screenshot_capture: ScreenshotCapture,
        dry_run: bool = False,
    ) -> None:
        self.client = client
        self.session_id = session_id
        self.db = db
        self.screenshot_capture = screenshot_capture
        self.dry_run = dry_run

    def crawl(
        self,
        start_lat: float,
        start_lng: float,
        job_id: int,
        max_steps: int = 100,
        visited: Optional[Set[str]] = None,
        initial_queue: Optional[List[Tuple[str, int]]] = None,
    ) -> CrawlStats:
        stats = CrawlStats()

        if visited is None:
            visited = set()

        queue: deque[Tuple[str, int]] = deque()

        if initial_queue:
            # Resume mode: use provided frontier
            queue.extend(initial_queue)
        else:
            # Fresh start: init from coordinates
            logger.info(f"Initializing panorama at ({start_lat}, {start_lng})")
            self.client.init(
                self.session_id, lat=start_lat, lng=start_lng
            )
            self.client.wait_for_stable(self.session_id)
            state = self.client.get_state(self.session_id)
            start_pano = state.get("panoId")
            if not start_pano:
                logger.error("No panorama found at starting coordinates")
                self.db.update_job_status(
                    job_id, "failed", error_message="no_pano_at_start"
                )
                return stats
            queue.append((start_pano, 0))

        while queue and stats.panos_visited < max_steps:
            pano_id, depth = queue.popleft()

            if pano_id in visited:
                continue

            # Navigate to this panorama
            try:
                self.client.set_pano(self.session_id, pano_id)
                self.client.wait_for_stable(self.session_id)
                state = self.client.get_state(self.session_id)
            except Exception as e:
                logger.warning(f"Failed to navigate to pano {pano_id}: {e}")
                stats.errors += 1
                continue

            # Handle redirects
            actual_pano_id = state.get("panoId")
            if not actual_pano_id:
                logger.warning(f"No panoId in state for {pano_id}")
                stats.errors += 1
                continue

            if actual_pano_id != pano_id:
                logger.info(f"Redirect: {pano_id} -> {actual_pano_id}")
                if actual_pano_id in visited:
                    continue
                pano_id = actual_pano_id

            visited.add(pano_id)

            # Save metadata
            position = state.get("position") or {}
            self.db.insert_panorama(
                pano_id=pano_id,
                lat=position.get("lat"),
                lng=position.get("lng"),
                date=state.get("date"),
                metadata_json=json.dumps(state),
                job_id=job_id,
                bfs_depth=depth,
            )

            # Save links and enqueue neighbours
            links = state.get("links") or []
            self.db.insert_links(pano_id, links)
            for link in links:
                neighbor_id = link.get("panoId")
                if neighbor_id and neighbor_id not in visited:
                    queue.append((neighbor_id, depth + 1))

            # Capture equirectangular panorama via browser screenshots
            if not self.dry_run and not self.db.has_equirect(pano_id):
                try:
                    rel_path, w, h = self.screenshot_capture.capture_equirectangular(
                        pano_id
                    )
                    self.db.update_equirect(pano_id, rel_path, w, h, 0)
                    stats.tiles_downloaded += 1
                except Exception as e:
                    logger.error(f"Screenshot capture failed for {pano_id}: {e}")
                    stats.errors += 1

            stats.panos_visited += 1
            self.db.update_job_status(
                job_id, "running", panos_visited=stats.panos_visited
            )

            if stats.panos_visited % 10 == 0:
                logger.info(
                    f"Progress: {stats.panos_visited}/{max_steps} panos, "
                    f"queue={len(queue)}, errors={stats.errors}"
                )

        return stats


# ---------------------------------------------------------------------------
# CrawlerOrchestrator
# ---------------------------------------------------------------------------

@dataclass
class CrawlerConfig:
    starting_points: List[Tuple[float, float]] = field(default_factory=list)
    max_steps: int = 100
    db_path: str = "crawl.db"
    image_root: str = "crawl_images"
    host_url: Optional[str] = None
    resume: bool = False
    dry_run: bool = False
    api_key: Optional[str] = None
    equirect_width: int = 4096
    equirect_height: int = 2048


class CrawlerOrchestrator:
    """Top-level coordinator: manages DB, host session, and BFS crawls."""

    def __init__(self, config: CrawlerConfig) -> None:
        self.config = config
        self.db = CrawlDatabase(config.db_path)
        self.client = StreetViewHostClient(
            host_url=config.host_url
        )
        self._session_id: Optional[str] = None
        self._screenshot_capture: Optional[ScreenshotCapture] = None

    def run(self) -> None:
        session_id = f"crawler_{int(time.time())}"
        self._session_id = session_id
        api_key = self.config.api_key or os.getenv("GOOGLE_MAPS_API_KEY", "")
        self.client.start(session_id, api_key=api_key)

        self._screenshot_capture = ScreenshotCapture(
            client=self.client,
            session_id=session_id,
            image_root=self.config.image_root,
            equirect_width=self.config.equirect_width,
            equirect_height=self.config.equirect_height,
        )

        try:
            # Resume interrupted jobs
            if self.config.resume:
                self._resume_jobs(session_id)

            # New starting points
            for lat, lng in self.config.starting_points:
                job_id = self.db.create_job(lat, lng, self.config.max_steps)
                logger.info(
                    f"Job {job_id}: crawling from ({lat}, {lng}), "
                    f"max_steps={self.config.max_steps}"
                )
                crawler = BFSCrawler(
                    self.client,
                    session_id,
                    self.db,
                    self._screenshot_capture,
                    dry_run=self.config.dry_run,
                )
                stats = crawler.crawl(lat, lng, job_id, self.config.max_steps)
                self.db.update_job_status(
                    job_id, "completed", panos_visited=stats.panos_visited
                )
                logger.info(
                    f"Job {job_id} completed: {stats.panos_visited} panos, "
                    f"{stats.tiles_downloaded} screenshots, {stats.errors} errors"
                )
        except KeyboardInterrupt:
            logger.info("Interrupted. Progress saved. Use --resume to continue.")
        except Exception as e:
            logger.exception(f"Crawler failed: {e}")
        finally:
            try:
                self.client.close_session(session_id)
            except Exception:
                pass
            self.client.close()
            self.db.close()

    def _resume_jobs(self, session_id: str) -> None:
        jobs = self.db.get_running_jobs()
        if not jobs:
            logger.info("No interrupted jobs to resume.")
            return

        for job in jobs:
            job_id = job["job_id"]
            start_lat = job["start_lat"]
            start_lng = job["start_lng"]
            max_steps = job["max_steps"]

            visited = self.db.get_visited_pano_ids(job_id)
            frontier = self.db.get_unvisited_neighbors(job_id)
            remaining = max_steps - len(visited)

            if remaining <= 0 or not frontier:
                self.db.update_job_status(
                    job_id, "completed", panos_visited=len(visited)
                )
                logger.info(f"Job {job_id}: already complete ({len(visited)} panos)")
                continue

            logger.info(
                f"Resuming job {job_id} from ({start_lat}, {start_lng}): "
                f"{len(visited)} visited, {len(frontier)} in frontier, "
                f"{remaining} remaining"
            )

            crawler = BFSCrawler(
                self.client,
                session_id,
                self.db,
                self._screenshot_capture,
                dry_run=self.config.dry_run,
            )
            stats = crawler.crawl(
                start_lat,
                start_lng,
                job_id,
                max_steps=remaining,
                visited=visited,
                initial_queue=frontier,
            )
            self.db.update_job_status(
                job_id,
                "completed",
                panos_visited=len(visited) + stats.panos_visited,
            )
            logger.info(
                f"Job {job_id} resumed and completed: "
                f"{stats.panos_visited} new panos, {stats.errors} errors"
            )


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
    coords = []
    with open(path) as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Support both comma and space/tab separation
            parts = re.split(r"[,\s\t]+", line)
            if len(parts) < 2:
                logger.warning(f"Skipping line {line_num}: '{line}'")
                continue
            try:
                coords.append((float(parts[0]), float(parts[1])))
            except ValueError:
                logger.warning(f"Skipping line {line_num}: '{line}'")
    return coords


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Street View BFS crawler — download panorama tiles + metadata to local SQLite"
    )

    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument(
        "--coords",
        nargs="+",
        metavar="LAT,LNG",
        help="Starting coordinates as lat,lng pairs",
    )
    input_group.add_argument(
        "--coords-file",
        type=str,
        help="Path to file with one lat,lng pair per line",
    )

    parser.add_argument(
        "--max-steps", type=int, default=100,
        help="Max BFS steps per starting point (default: 100)",
    )
    parser.add_argument(
        "--db", type=str, default="crawl.db",
        help="SQLite database path (default: crawl.db)",
    )
    parser.add_argument(
        "--image-dir", type=str, default="crawl_images",
        help="Root directory for equirectangular images (default: crawl_images)",
    )
    parser.add_argument(
        "--host-url", type=str, default=None,
        help="Playwright host URL (default: $STREETVIEW_HOST_URL)",
    )
    parser.add_argument(
        "--equirect-size", type=str, default="4096x2048",
        help="Equirectangular output size WxH (default: 4096x2048)",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume interrupted crawl jobs from the database",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="BFS traversal + metadata only, skip screenshot capture",
    )

    args = parser.parse_args()

    # Validate input
    starting_points: List[Tuple[float, float]] = []
    if args.coords:
        starting_points = [_parse_coord(c) for c in args.coords]
    elif args.coords_file:
        starting_points = _load_coords_file(args.coords_file)

    if not starting_points and not args.resume:
        parser.error("Either --coords, --coords-file, or --resume is required")

    # Parse equirect size
    eq_parts = args.equirect_size.split("x")
    eq_w = int(eq_parts[0]) if len(eq_parts) >= 1 else 4096
    eq_h = int(eq_parts[1]) if len(eq_parts) >= 2 else 2048

    config = CrawlerConfig(
        starting_points=starting_points,
        max_steps=args.max_steps,
        db_path=args.db,
        image_root=args.image_dir,
        host_url=args.host_url,
        resume=args.resume,
        dry_run=args.dry_run,
        equirect_width=eq_w,
        equirect_height=eq_h,
    )

    orchestrator = CrawlerOrchestrator(config)
    orchestrator.run()


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    main()
