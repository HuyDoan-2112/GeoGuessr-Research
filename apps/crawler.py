"""Street View BFS crawler — download panorama tiles + metadata to local SQLite.

Uses the Google Maps Platform Street View Tiles API for high-quality tile
downloads and panorama metadata.

Usage:
    python -m apps.crawler --coords 37.7749,-122.4194
    python -m apps.crawler --coords-file locations.csv --max-depth 50
    python -m apps.crawler --resume --db crawl.db
"""

from __future__ import annotations

import argparse
import json
import logging
import math
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

import requests
from PIL import Image
from requests.adapters import HTTPAdapter
from tenacity import (
    retry,
    wait_exponential_jitter,
    retry_if_exception,
    before_sleep_log,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.utils.retry import is_retryable_http_error

logger = logging.getLogger(__name__)



# ---------------------------------------------------------------------------
# TilesAPIClient
# ---------------------------------------------------------------------------

TILES_API_BASE = "https://tile.googleapis.com/v1"


class TilesAPIClient:
    """Client for the Google Maps Platform Street View Tiles API."""

    def __init__(self, api_key: str, max_concurrent: int = 16, request_delay: float = 0.1) -> None:
        self.api_key = api_key
        self._request_delay = request_delay
        self._http = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=max_concurrent, pool_maxsize=max_concurrent
        )
        self._http.mount("https://", adapter)
        self._token: Optional[str] = None
        self._token_expiry: float = 0
        self._lock = threading.Lock()

    def _ensure_token(self) -> str:
        """Create or refresh the Tiles API session token."""
        with self._lock:
            if self._token and time.time() < self._token_expiry - 60:
                return self._token
            resp = self._http.post(
                f"{TILES_API_BASE}/createSession",
                params={"key": self.api_key},
                json={
                    "mapType": "streetview",
                    "language": "en-US",
                    "region": "US",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data["session"]
            self._token_expiry = int(data["expiry"])
            logger.info("Tiles API session created (expires %s)", self._token_expiry)
            return self._token

    @retry(
        wait=wait_exponential_jitter(initial=1, max=120, jitter=2),
        retry=retry_if_exception(is_retryable_http_error),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    def get_metadata(
        self,
        pano_id: Optional[str] = None,
        lat: Optional[float] = None,
        lng: Optional[float] = None,
        radius: int = 50,
    ) -> Dict[str, Any]:
        """Fetch panorama metadata by pano_id or coordinates."""
        token = self._ensure_token()
        params: Dict[str, Any] = {"session": token, "key": self.api_key}
        if pano_id:
            params["panoId"] = pano_id
        elif lat is not None and lng is not None:
            params["lat"] = lat
            params["lng"] = lng
            params["radius"] = radius
        else:
            raise ValueError("Either pano_id or (lat, lng) must be provided")
        time.sleep(self._request_delay)
        resp = self._http.get(
            f"{TILES_API_BASE}/streetview/metadata", params=params
        )
        resp.raise_for_status()
        return resp.json()

    @retry(
        wait=wait_exponential_jitter(initial=1, max=120, jitter=2),
        retry=retry_if_exception(is_retryable_http_error),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    def get_tile(self, pano_id: str, zoom: int, x: int, y: int) -> bytes:
        """Download a single tile."""
        token = self._ensure_token()
        time.sleep(self._request_delay)
        resp = self._http.get(
            f"{TILES_API_BASE}/streetview/tiles/{zoom}/{x}/{y}",
            params={
                "session": token,
                "key": self.api_key,
                "panoId": pano_id,
            },
        )
        resp.raise_for_status()
        return resp.content

    def close(self) -> None:
        self._http.close()


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
        max_depth       INTEGER NOT NULL DEFAULT 100,
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
        tile_zoom       INTEGER DEFAULT 5,
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

    def create_job(self, lat: float, lng: float, max_depth: int) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO crawl_jobs (start_lat, start_lng, max_depth, status, started_at) "
                "VALUES (?, ?, ?, 'running', ?)",
                (lat, lng, max_depth, now),
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
                link.get("description", link.get("text", "")),
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

    def find_completed_job(
        self, lat: float, lng: float
    ) -> Optional[Dict[str, Any]]:
        """Find the most recent completed job for the given starting coords."""
        row = self._conn.execute(
            "SELECT * FROM crawl_jobs WHERE start_lat = ? AND start_lng = ? "
            "AND status = 'completed' ORDER BY job_id DESC LIMIT 1",
            (lat, lng),
        ).fetchone()
        return dict(row) if row else None

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
# TileCapture
# ---------------------------------------------------------------------------

class TileCapture:
    """Downloads Street View tiles and stitches into equirectangular panoramas."""

    def __init__(
        self,
        tiles_client: TilesAPIClient,
        image_root: str,
        tile_zoom: int = 5,
        quality: int = 95,
    ) -> None:
        self.tiles_client = tiles_client
        self.image_root = image_root
        self.tile_zoom = tile_zoom
        self.quality = quality

    @staticmethod
    def _tile_grid(
        image_width: int, image_height: int,
        tile_width: int, tile_height: int,
        zoom: int,
    ) -> Tuple[int, int, int]:
        """Compute (actual_zoom, num_x, num_y) for the tile grid.

        Caps zoom to the max level supported by the panorama's resolution.
        """
        max_zoom = math.ceil(
            math.log2(max(image_width / tile_width, image_height / tile_height))
        )
        actual_zoom = min(zoom, max_zoom)
        scale = 2 ** (max_zoom - actual_zoom)
        num_x = math.ceil(image_width / (tile_width * scale))
        num_y = math.ceil(image_height / (tile_height * scale))
        return actual_zoom, num_x, num_y

    def capture_equirectangular(
        self, pano_id: str, metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, int, int, int]:
        """Download tiles at the configured zoom and stitch into equirectangular.

        Returns (relative_path, width, height, actual_zoom).
        """
        meta = metadata or {}
        tile_w = meta.get("tileWidth", 512)
        tile_h = meta.get("tileHeight", 512)
        img_w = meta.get("imageWidth", tile_w * (2 ** self.tile_zoom))
        img_h = meta.get("imageHeight", tile_h * (2 ** max(0, self.tile_zoom - 1)))

        zoom, num_x, num_y = self._tile_grid(img_w, img_h, tile_w, tile_h, self.tile_zoom)
        if zoom != self.tile_zoom:
            logger.info(
                f"Zoom capped {self.tile_zoom} -> {zoom} for {pano_id} "
                f"(image {img_w}x{img_h})"
            )

        total_w = num_x * tile_w
        total_h = num_y * tile_h

        # Download tiles sequentially (respects rate limiter)
        tiles: Dict[Tuple[int, int], Image.Image] = {}
        for ty in range(num_y):
            for tx in range(num_x):
                tile_bytes = self.tiles_client.get_tile(pano_id, zoom, tx, ty)
                tiles[(tx, ty)] = Image.open(BytesIO(tile_bytes))

        # Stitch
        equirect = Image.new("RGB", (total_w, total_h))
        for (tx, ty), tile_img in tiles.items():
            equirect.paste(tile_img, (tx * tile_w, ty * tile_h))

        # Crop to actual panorama dimensions (edge tiles may have padding)
        if total_w > img_w or total_h > img_h:
            equirect = equirect.crop((0, 0, img_w, img_h))
            total_w, total_h = img_w, img_h

        # Save
        safe_pano = re.sub(r"[^A-Za-z0-9_-]", "_", pano_id)
        subdir = os.path.join(self.image_root, safe_pano[:8])
        os.makedirs(subdir, exist_ok=True)
        filename = f"{safe_pano}_equirect.jpg"
        file_path = os.path.join(subdir, filename)
        equirect.save(file_path, format="JPEG", quality=self.quality)

        rel_path = os.path.relpath(file_path, self.image_root)
        return rel_path, total_w, total_h, zoom


# ---------------------------------------------------------------------------
# BFSCrawler
# ---------------------------------------------------------------------------

@dataclass
class CrawlStats:
    panos_visited: int = 0
    tiles_downloaded: int = 0
    errors: int = 0
    capture_times: list = field(default_factory=list)
    job_start_time: float = 0.0
    job_end_time: float = 0.0

    @property
    def job_duration(self) -> float:
        return self.job_end_time - self.job_start_time if self.job_start_time else 0.0

    @property
    def avg_capture_time(self) -> float:
        return sum(self.capture_times) / len(self.capture_times) if self.capture_times else 0.0


class BFSCrawler:
    """BFS traversal of Street View panorama graph using Tiles API."""

    def __init__(
        self,
        tiles_client: TilesAPIClient,
        db: CrawlDatabase,
        tile_capture: TileCapture,
        dry_run: bool = False,
    ) -> None:
        self.tiles_client = tiles_client
        self.db = db
        self.tile_capture = tile_capture
        self.dry_run = dry_run

    def crawl(
        self,
        start_lat: float,
        start_lng: float,
        job_id: int,
        max_depth: int = 100,
        visited: Optional[Set[str]] = None,
        initial_queue: Optional[List[Tuple[str, int]]] = None,
    ) -> CrawlStats:
        stats = CrawlStats()
        stats.job_start_time = time.time()

        if visited is None:
            visited = set()

        queue: deque[Tuple[str, int]] = deque()

        if initial_queue:
            queue.extend(initial_queue)
        else:
            logger.info(f"Looking up panorama at ({start_lat}, {start_lng})")
            try:
                meta = self.tiles_client.get_metadata(
                    lat=start_lat, lng=start_lng
                )
            except Exception as e:
                logger.error(f"No panorama at ({start_lat}, {start_lng}): {e}")
                self.db.update_job_status(
                    job_id, "failed", error_message="no_pano_at_start"
                )
                return stats
            start_pano = meta.get("panoId")
            if not start_pano:
                logger.error("No panorama found at starting coordinates")
                self.db.update_job_status(
                    job_id, "failed", error_message="no_pano_at_start"
                )
                return stats
            queue.append((start_pano, 0))

        while queue:
            pano_id, depth = queue.popleft()

            if pano_id in visited:
                continue

            # Fetch metadata from Tiles API
            try:
                metadata = self.tiles_client.get_metadata(pano_id=pano_id)
            except Exception as e:
                logger.warning(f"Failed to get metadata for {pano_id}: {e}")
                stats.errors += 1
                continue

            # Handle redirects
            actual_pano_id = metadata.get("panoId")
            if not actual_pano_id:
                logger.warning(f"No panoId in metadata for {pano_id}")
                stats.errors += 1
                continue

            if actual_pano_id != pano_id:
                logger.info(f"Redirect: {pano_id} -> {actual_pano_id}")
                if actual_pano_id in visited:
                    continue
                pano_id = actual_pano_id

            visited.add(pano_id)

            # Save metadata
            self.db.insert_panorama(
                pano_id=pano_id,
                lat=metadata.get("lat"),
                lng=metadata.get("lng"),
                date=metadata.get("date"),
                metadata_json=json.dumps(metadata),
                job_id=job_id,
                bfs_depth=depth,
            )

            # Save links and enqueue neighbours within depth limit
            links = metadata.get("links") or []
            self.db.insert_links(pano_id, links)

            if depth < max_depth:
                for link in links:
                    neighbor_id = link.get("panoId")
                    if neighbor_id and neighbor_id not in visited:
                        queue.append((neighbor_id, depth + 1))

            # Download equirectangular panorama tiles
            if not self.dry_run and not self.db.has_equirect(pano_id):
                try:
                    t0 = time.time()
                    rel_path, w, h, actual_zoom = self.tile_capture.capture_equirectangular(
                        pano_id, metadata=metadata
                    )
                    capture_dur = time.time() - t0
                    stats.capture_times.append(capture_dur)
                    self.db.update_equirect(
                        pano_id, rel_path, w, h, actual_zoom
                    )
                    stats.tiles_downloaded += 1
                    logger.info(f"Captured {pano_id} in {capture_dur:.1f}s")
                except Exception as e:
                    logger.error(f"Tile download failed for {pano_id}: {e}")
                    stats.errors += 1

            stats.panos_visited += 1
            self.db.update_job_status(
                job_id, "running", panos_visited=stats.panos_visited
            )

            if stats.panos_visited % 10 == 0:
                logger.info(
                    f"Progress: {stats.panos_visited} panos (depth {depth}/{max_depth}), "
                    f"queue={len(queue)}, errors={stats.errors}"
                )

        stats.job_end_time = time.time()
        return stats


# ---------------------------------------------------------------------------
# CrawlerOrchestrator
# ---------------------------------------------------------------------------

@dataclass
class CrawlerConfig:
    starting_points: List[Tuple[float, float]] = field(default_factory=list)
    max_depth: int = 100
    db_path: str = "crawl.db"
    image_root: str = "crawl_images"
    resume: bool = False
    dry_run: bool = False
    api_key: Optional[str] = None
    tile_zoom: int = 5


class CrawlerOrchestrator:
    """Top-level coordinator: manages DB, Tiles API client, and BFS crawls."""

    def __init__(self, config: CrawlerConfig) -> None:
        self.config = config
        self.db = CrawlDatabase(config.db_path)
        api_key = config.api_key or os.getenv("GOOGLE_MAPS_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "GOOGLE_MAPS_API_KEY not provided and not set in environment"
            )
        self.tiles_client = TilesAPIClient(api_key)
        self.tile_capture = TileCapture(
            tiles_client=self.tiles_client,
            image_root=config.image_root,
            tile_zoom=config.tile_zoom,
        )

    @staticmethod
    def _print_stats(label: str, stats: CrawlStats) -> None:
        dur = stats.job_duration
        mins, secs = divmod(dur, 60)
        logger.info(f"--- {label} ---")
        logger.info(f"  Panoramas visited : {stats.panos_visited}")
        logger.info(f"  Tiles downloaded  : {stats.tiles_downloaded}")
        logger.info(f"  Errors            : {stats.errors}")
        logger.info(f"  Total time        : {int(mins)}m {secs:.1f}s")
        if stats.capture_times:
            logger.info(f"  Avg capture time  : {stats.avg_capture_time:.1f}s")
            logger.info(f"  Min capture time  : {min(stats.capture_times):.1f}s")
            logger.info(f"  Max capture time  : {max(stats.capture_times):.1f}s")

    def run(self) -> None:
        all_stats: list[CrawlStats] = []
        run_start = time.time()

        try:
            if self.config.resume:
                self._resume_jobs(all_stats)

            for lat, lng in self.config.starting_points:
                existing = self.db.find_completed_job(lat, lng)
                if existing:
                    job_id = existing["job_id"]
                    visited = self.db.get_visited_pano_ids(job_id)
                    frontier = self.db.get_unvisited_neighbors(job_id)
                    frontier = [
                        (pid, d)
                        for pid, d in frontier
                        if d <= self.config.max_depth
                    ]
                    if not frontier:
                        logger.info(
                            f"Skipping ({lat}, {lng}): job {job_id} already "
                            f"complete with no unvisited neighbors within depth {self.config.max_depth}"
                        )
                        continue
                    logger.info(
                        f"Continuing job {job_id} at ({lat}, {lng}): "
                        f"{len(visited)} visited, {len(frontier)} in frontier"
                    )
                    self.db.update_job_status(
                        job_id, "running",
                        max_depth=self.config.max_depth,
                    )
                    crawler = BFSCrawler(
                        self.tiles_client,
                        self.db,
                        self.tile_capture,
                        dry_run=self.config.dry_run,
                    )
                    stats = crawler.crawl(
                        lat, lng, job_id,
                        max_depth=self.config.max_depth,
                        visited=visited,
                        initial_queue=frontier,
                    )
                    self.db.update_job_status(
                        job_id, "completed",
                        panos_visited=len(visited) + stats.panos_visited,
                    )
                    self._print_stats(f"Job {job_id} (continued)", stats)
                    all_stats.append(stats)
                else:
                    job_id = self.db.create_job(lat, lng, self.config.max_depth)
                    logger.info(
                        f"Job {job_id}: crawling from ({lat}, {lng}), "
                        f"max_depth={self.config.max_depth}"
                    )
                    crawler = BFSCrawler(
                        self.tiles_client,
                        self.db,
                        self.tile_capture,
                        dry_run=self.config.dry_run,
                    )
                    stats = crawler.crawl(
                        lat, lng, job_id, self.config.max_depth
                    )
                    self.db.update_job_status(
                        job_id, "completed", panos_visited=stats.panos_visited
                    )
                    self._print_stats(f"Job {job_id} ({lat}, {lng})", stats)
                    all_stats.append(stats)
        except KeyboardInterrupt:
            logger.info("Interrupted. Progress saved. Use --resume to continue.")
        except Exception as e:
            logger.exception(f"Crawler failed: {e}")
        finally:
            if all_stats:
                total_dur = time.time() - run_start
                total_panos = sum(s.panos_visited for s in all_stats)
                total_tiles = sum(s.tiles_downloaded for s in all_stats)
                total_errors = sum(s.errors for s in all_stats)
                all_capture_times = [
                    t for s in all_stats for t in s.capture_times
                ]
                mins, secs = divmod(total_dur, 60)
                logger.info("=== CRAWL SUMMARY ===")
                logger.info(f"  Starting points   : {len(all_stats)}")
                logger.info(f"  Total panoramas   : {total_panos}")
                logger.info(f"  Total tiles DL'd  : {total_tiles}")
                logger.info(f"  Total errors      : {total_errors}")
                logger.info(f"  Total time        : {int(mins)}m {secs:.1f}s")
                if all_capture_times:
                    avg = sum(all_capture_times) / len(all_capture_times)
                    logger.info(f"  Avg capture time  : {avg:.1f}s")
                    logger.info(f"  Min capture time  : {min(all_capture_times):.1f}s")
                    logger.info(f"  Max capture time  : {max(all_capture_times):.1f}s")
                if total_panos > 0 and total_dur > 0:
                    logger.info(
                        f"  Avg time per pano : {total_dur / total_panos:.1f}s"
                    )

            self.tiles_client.close()
            self.db.close()

    def _resume_jobs(self, all_stats: list[CrawlStats]) -> None:
        jobs = self.db.get_running_jobs()
        if not jobs:
            logger.info("No interrupted jobs to resume.")
            return

        for job in jobs:
            job_id = job["job_id"]
            start_lat = job["start_lat"]
            start_lng = job["start_lng"]
            max_depth = job["max_depth"]

            visited = self.db.get_visited_pano_ids(job_id)
            frontier = self.db.get_unvisited_neighbors(job_id)
            frontier = [(pid, d) for pid, d in frontier if d <= max_depth]

            if not frontier:
                self.db.update_job_status(
                    job_id, "completed", panos_visited=len(visited)
                )
                logger.info(
                    f"Job {job_id}: already complete ({len(visited)} panos)"
                )
                continue

            logger.info(
                f"Resuming job {job_id} from ({start_lat}, {start_lng}): "
                f"{len(visited)} visited, {len(frontier)} in frontier"
            )

            crawler = BFSCrawler(
                self.tiles_client,
                self.db,
                self.tile_capture,
                dry_run=self.config.dry_run,
            )
            stats = crawler.crawl(
                start_lat,
                start_lng,
                job_id,
                max_depth=max_depth,
                visited=visited,
                initial_queue=frontier,
            )
            self.db.update_job_status(
                job_id,
                "completed",
                panos_visited=len(visited) + stats.panos_visited,
            )
            self._print_stats(f"Job {job_id} (resumed)", stats)
            all_stats.append(stats)


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
        description="Street View BFS crawler using Tiles API"
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
        "--max-depth", type=int, default=100,
        help="Max BFS depth from each starting point (default: 100)",
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
        "--tile-zoom", type=int, default=5,
        help="Tile zoom level — higher means better quality (default: 5)",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume interrupted crawl jobs from the database",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="BFS traversal + metadata only, skip tile download",
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

    config = CrawlerConfig(
        starting_points=starting_points,
        max_depth=args.max_depth,
        db_path=args.db,
        image_root=args.image_dir,
        resume=args.resume,
        dry_run=args.dry_run,
        tile_zoom=args.tile_zoom,
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
