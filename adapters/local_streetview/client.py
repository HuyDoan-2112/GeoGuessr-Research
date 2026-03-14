"""Local Street View client backed by a pre-crawled SQLite database.

Drop-in replacement for ``StreetViewHostClient`` — same public API, but reads
from the local database instead of talking to the Playwright host.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class _Session:
    """In-memory state for a single local session."""

    __slots__ = ("pano_id", "heading", "pitch", "zoom")

    def __init__(self) -> None:
        self.pano_id: Optional[str] = None
        self.heading: float = 0.0
        self.pitch: float = 0.0
        self.zoom: float = 1.0


class LocalStreetViewClient:
    """Offline ``StreetViewHostClient`` that serves data from a crawl database.

    Parameters
    ----------
    db_path : str
        Path to the SQLite database produced by ``apps/crawler.py``.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._sessions: Dict[str, _Session] = {}

    # ------------------------------------------------------------------
    # Public API — mirrors StreetViewHostClient
    # ------------------------------------------------------------------

    def start(self, session_id: str, api_key: Optional[str] = None) -> Any:
        """Create a session (api_key is ignored in local mode)."""
        self._sessions[session_id] = _Session()
        return {"sessionId": session_id, "created": True}

    def init(
        self,
        session_id: str,
        lat: float,
        lng: float,
        heading: float = 0.0,
        pitch: float = 0.0,
        zoom: float = 1.0,
    ) -> Any:
        """Snap to the nearest panorama in the DB by lat/lng."""
        sess = self._get_session(session_id)

        row = self._query_one(
            "SELECT pano_id, lat, lng, metadata_json "
            "FROM panoramas WHERE lat IS NOT NULL AND lng IS NOT NULL "
            "ORDER BY (lat - ?) * (lat - ?) + (lng - ?) * (lng - ?) "
            "LIMIT 1",
            (lat, lat, lng, lng),
        )
        if not row:
            raise RuntimeError("No panoramas in local database")

        sess.pano_id = row["pano_id"]
        sess.heading = heading
        sess.pitch = pitch
        sess.zoom = zoom

        return self._build_state(sess)

    def get_state(self, session_id: str) -> Any:
        sess = self._get_session(session_id)
        return self._build_state(sess)

    def set_pano(self, session_id: str, pano_id: str) -> Any:
        sess = self._get_session(session_id)
        row = self._query_one(
            "SELECT pano_id FROM panoramas WHERE pano_id = ?", (pano_id,)
        )
        if not row:
            raise RuntimeError(f"Panorama {pano_id} not found in local database")
        sess.pano_id = pano_id
        return self._build_state(sess)

    def set_pov(
        self,
        session_id: str,
        heading: Optional[float] = None,
        pitch: Optional[float] = None,
        zoom: Optional[float] = None,
    ) -> Any:
        sess = self._get_session(session_id)
        if heading is not None:
            sess.heading = heading
        if pitch is not None:
            sess.pitch = pitch
        if zoom is not None:
            sess.zoom = zoom
        return self._build_state(sess)

    def set_position(self, session_id: str, lat: float, lng: float) -> Any:
        """Same as init without resetting POV."""
        return self.init(session_id, lat, lng)

    def wait_for_stable(
        self, session_id: str, timeoutMs: int = 1500, debounceMs: int = 200
    ) -> Any:
        """No-op — local data is always stable."""
        sess = self._get_session(session_id)
        return self._build_state(sess)

    def close_session(self, session_id: str) -> Any:
        self._sessions.pop(session_id, None)
        return {"closed": True}

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "LocalStreetViewClient":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, tb: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_session(self, session_id: str) -> _Session:
        sess = self._sessions.get(session_id)
        if sess is None:
            raise RuntimeError(f"Local session {session_id} not found")
        return sess

    def _query_one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _query_all(self, sql: str, params: tuple = ()) -> list:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _build_state(self, sess: _Session) -> Dict[str, Any]:
        """Construct a state dict matching the format from the Playwright host."""
        if sess.pano_id is None:
            return {
                "panoId": None,
                "position": None,
                "pov": {"heading": sess.heading, "pitch": sess.pitch, "zoom": sess.zoom},
                "links": [],
                "date": None,
            }

        row = self._query_one(
            "SELECT * FROM panoramas WHERE pano_id = ?", (sess.pano_id,)
        )
        if not row:
            return {
                "panoId": sess.pano_id,
                "position": None,
                "pov": {"heading": sess.heading, "pitch": sess.pitch, "zoom": sess.zoom},
                "links": [],
                "date": None,
            }

        # Parse stored metadata for links (the authoritative source)
        metadata = {}
        if row["metadata_json"]:
            try:
                metadata = json.loads(row["metadata_json"])
            except (json.JSONDecodeError, TypeError):
                pass

        # Use stored links from metadata, falling back to link table
        links = metadata.get("links") or []
        if not links:
            link_rows = self._query_all(
                "SELECT to_pano_id, heading, description, link_date "
                "FROM panorama_links WHERE from_pano_id = ?",
                (sess.pano_id,),
            )
            links = [
                {
                    "panoId": lr["to_pano_id"],
                    "heading": lr["heading"],
                    "description": lr["description"] or "",
                    "date": lr["link_date"],
                }
                for lr in link_rows
            ]

        return {
            "panoId": sess.pano_id,
            "position": {"lat": row["lat"], "lng": row["lng"]},
            "pov": {
                "heading": sess.heading,
                "pitch": sess.pitch,
                "zoom": sess.zoom,
            },
            "links": links,
            "date": row["date"],
        }
