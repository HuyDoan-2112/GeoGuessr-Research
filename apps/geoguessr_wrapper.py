import base64
import json
import math
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional

"""
Shared utilities for executable backend functions.
"""


class ImageResult:
    """
    Return type for functions that produce image output.

    Usage:
        from bfcl_eval.eval_checker.multi_turn_eval.func_source_code import ImageResult

        def fetch_image(self, url: str) -> ImageResult:
            # ... fetch image ...
            return ImageResult(base64_data, "image/jpeg")
    """

    def __init__(
        self,
        image_base64: str = "",
        image_bytes: bytes = b"",
        mime_type: str = "image/jpeg",
    ):
        if image_base64 and image_bytes:
            self.image_base64 = image_base64
            self.image_bytes = image_bytes
        elif image_base64:
            self.image_base64 = image_base64
            self.image_bytes = base64.b64decode(image_base64)
        elif image_bytes:
            self.image_bytes = image_bytes
            self.image_base64 = base64.b64encode(image_bytes).decode("utf-8")
        else:
            raise ValueError("Either image_base64 or image_bytes must be provided")

        self.type = mime_type

    def to_dict(self) -> dict:
        return {
            "image_base64": self.image_base64,
            "image_bytes": self.image_bytes,
            "type": self.type,
        }


# ---------------------------------------------------------------------------
# Direction cones (mirrored from core/navigation/pure_nav.py)
# ---------------------------------------------------------------------------

DIR_CONES = {
    "N":  [(337.5, 360.0), (0.0, 22.5)],
    "NE": [(22.5, 67.5)],
    "E":  [(67.5, 112.5)],
    "SE": [(112.5, 157.5)],
    "S":  [(157.5, 202.5)],
    "SW": [(202.5, 247.5)],
    "W":  [(247.5, 292.5)],
    "NW": [(292.5, 337.5)],
}

_DIR_TO_FULL = {
    "N": "north", "NE": "northeast", "E": "east", "SE": "southeast",
    "S": "south", "SW": "southwest", "W": "west", "NW": "northwest",
}


def _normalize_heading(heading: float) -> float:
    try:
        x = float(heading)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(x):
        return 0.0
    return ((x % 360) + 360) % 360


def _heading_to_direction(heading: float) -> str:
    heading = _normalize_heading(heading)
    for direction, ranges in DIR_CONES.items():
        for start, end in ranges:
            if (start <= heading < end) or (start == 0.0 and heading == 360.0):
                return direction
    return "N"


def _in_cone(h: float, cones: List[tuple]) -> bool:
    h = _normalize_heading(h)
    for lo, hi in cones:
        if lo <= hi and lo <= h < hi:
            return True
        if lo > hi and (h >= lo or h < hi):
            return True
    return False


# ---------------------------------------------------------------------------
# Allowed discrete values for scroll and zoom
# These match the default capture grid from mapcrunch_crawler.py
# ---------------------------------------------------------------------------

ALLOWED_HEADINGS = [float(i) for i in range(0, 360, 30)]  # 0, 30, 60, ..., 330
ALLOWED_PITCHES = [-30.0, -15.0, 0.0, 15.0, 30.0]
ALLOWED_ZOOMS = [0.0, 1.0, 2.0]

# Scroll step sizes (degrees)
SCROLL_HORIZONTAL_STEP = 30.0  # matches heading grid spacing
SCROLL_VERTICAL_STEP = 15.0    # matches pitch grid spacing
ZOOM_STEP = 1.0                # matches zoom grid spacing


def _snap_to_nearest(value: float, allowed: List[float]) -> float:
    """Snap a value to the nearest allowed value."""
    return min(allowed, key=lambda x: abs(x - value))


# ---------------------------------------------------------------------------
# Disk-backed LRU cache for image bytes
# ---------------------------------------------------------------------------


class _DiskImageCache:
    """Persistent LRU cache keyed on GCS-style object paths.

    Stores each blob as a file under ``cache_dir`` mirroring its key, uses
    mtime as the access timestamp, and evicts oldest-first once the total
    size exceeds ``max_size_bytes``. Safe for concurrent readers/writers.
    """

    def __init__(self, cache_dir: str, max_size_bytes: int):
        self.cache_dir = os.path.expanduser(cache_dir)
        self.max_size_bytes = max_size_bytes
        self._lock = threading.Lock()
        self._puts_since_evict = 0
        os.makedirs(self.cache_dir, exist_ok=True)

    def _path(self, key: str) -> str:
        return os.path.join(self.cache_dir, key)

    def get(self, key: str) -> Optional[bytes]:
        path = self._path(key)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return None
        try:
            os.utime(path, None)
        except OSError:
            pass
        return data

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = f"{path}.{os.getpid()}.tmp"
        with open(tmp_path, "wb") as f:
            f.write(data)
        os.replace(tmp_path, path)

        with self._lock:
            self._puts_since_evict += 1
            if self._puts_since_evict >= 100:
                self._puts_since_evict = 0
                self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        entries = []
        total = 0
        for root, _, names in os.walk(self.cache_dir):
            for name in names:
                p = os.path.join(root, name)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                entries.append((st.st_mtime, st.st_size, p))
                total += st.st_size

        if total <= self.max_size_bytes:
            return

        entries.sort()
        target = int(self.max_size_bytes * 0.8)
        for _, size, p in entries:
            if total <= target:
                break
            try:
                os.remove(p)
                total -= size
            except OSError:
                pass


class StreetViewAPI:
    """
    StreetView API backed by a local SQLite database.

    Reads panorama data, links, and pre-captured screenshots from the
    database produced by ``apps/mapcrunch_crawler.py``.
    """

    def __init__(
        self,
        db_path: str = "mapcrunch.db",
        gcs_bucket: Optional[str] = None,
        cache_dir: Optional[str] = None,
        cache_size_gb: Optional[float] = None,
    ):
        """Create a new StreetView API client.

        Args:
            db_path: Path to the SQLite database produced by the crawler.
            gcs_bucket: Optional GCS bucket containing ``{pano_id}/{heading}_{pitch}_{zoom}.jpg``
                images. Falls back to ``MAPCRUNCH_GCS_BUCKET`` env var. When unset,
                images are read from the local ``image_path`` recorded in the DB.
            cache_dir: Optional directory for a persistent on-disk image cache.
                Falls back to ``MAPCRUNCH_CACHE_DIR`` env var. Only used when
                fetching from GCS.
            cache_size_gb: Soft cap on cache size in GB; oldest entries are
                evicted once exceeded. Falls back to ``MAPCRUNCH_CACHE_SIZE_GB``
                env var, then to 5.0.
        """
        self._api_description = "This tool belongs to the StreetView API, which is used to navigate and capture street views."

        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()

        bucket_name = gcs_bucket or os.environ.get("MAPCRUNCH_GCS_BUCKET")
        self._gcs_bucket = None
        if bucket_name:
            from google.cloud import storage  # lazy import
            self._gcs_bucket = storage.Client().bucket(bucket_name)

        cache_path = cache_dir or os.environ.get("MAPCRUNCH_CACHE_DIR")
        if cache_size_gb is None:
            cache_size_gb = float(os.environ.get("MAPCRUNCH_CACHE_SIZE_GB", "5"))
        self._cache = (
            _DiskImageCache(cache_path, int(cache_size_gb * 1024 ** 3))
            if cache_path and self._gcs_bucket is not None
            else None
        )

        # Session state
        self.session_id: Optional[str] = None
        self.available_moves: List[str] = None

        # Navigation state
        self._pano_id: Optional[str] = None
        self._heading: float = 0.0
        self._pitch: float = 0.0
        self._zoom: float = 1.0
        self._links: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    #  Database helpers
    # ------------------------------------------------------------------

    def _query_one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _query_all(self, sql: str, params: tuple = ()) -> list:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------------
    #  State helpers
    # ------------------------------------------------------------------

    def _load_links(self) -> List[Dict[str, Any]]:
        """Load neighbor links for the current panorama."""
        if not self._pano_id:
            return []

        # First try metadata_json (authoritative)
        row = self._query_one(
            "SELECT metadata_json FROM panoramas WHERE pano_id = ?",
            (self._pano_id,),
        )
        if row and row["metadata_json"]:
            try:
                metadata = json.loads(row["metadata_json"])
                links = metadata.get("links") or []
                if links:
                    return links
            except (json.JSONDecodeError, TypeError):
                pass

        # Fall back to panorama_links table
        link_rows = self._query_all(
            "SELECT to_pano_id, heading, description, link_date "
            "FROM panorama_links WHERE from_pano_id = ?",
            (self._pano_id,),
        )
        return [
            {
                "panoId": lr["to_pano_id"],
                "heading": lr["heading"],
                "description": lr["description"] or "",
                "date": lr["link_date"],
            }
            for lr in link_rows
        ]

    def _compute_available_moves(self) -> List[str]:
        """Compute available moves based on current links and state."""
        move_actions = []
        for link in self._links:
            move_heading = float(link["heading"])
            direction = _heading_to_direction(move_heading)
            action = f"move_{_DIR_TO_FULL[direction]}"
            if action not in move_actions:
                move_actions.append(action)

        universal_actions = [
            "capture_view",
            "scroll_up",
            "scroll_left",
            "scroll_right",
            "scroll_down",
            "zoom_in",
            "zoom_out",
        ]
        return universal_actions + move_actions

    def _update_state_after_move(self) -> None:
        """Refresh links and available moves after a panorama change."""
        self._links = self._load_links()
        self.available_moves = self._compute_available_moves()

    def _get_updated_tool_list(self) -> List[str]:
        return self.available_moves

    # ------------------------------------------------------------------
    # Core Connection / Setup
    # ------------------------------------------------------------------

    def _load_scenario(
        self,
        scenario: Dict[str, float],
        long_context: bool = False,
    ) -> None:
        """
        Set the starting coordinates for the scenario.
        Args:
            scenario (Dict[str, float]): Configuration dict with lat/lng keys.
        """
        lat = scenario.get("lat", 0.0)
        lng = scenario.get("lng", 0.0)
        heading = scenario.get("heading", 0.0)
        pitch = scenario.get("pitch", 0.0)
        zoom = scenario.get("zoom", 1.0)

        # Snap to nearest panorama in the database
        row = self._query_one(
            "SELECT pano_id, lat, lng FROM panoramas "
            "WHERE lat IS NOT NULL AND lng IS NOT NULL "
            "ORDER BY (lat - ?) * (lat - ?) + (lng - ?) * (lng - ?) "
            "LIMIT 1",
            (lat, lat, lng, lng),
        )
        if not row:
            raise RuntimeError("No panoramas in local database")

        self._pano_id = row["pano_id"]
        self._heading = _snap_to_nearest(_normalize_heading(heading), ALLOWED_HEADINGS)
        self._pitch = _snap_to_nearest(pitch, ALLOWED_PITCHES)
        self._zoom = _snap_to_nearest(zoom, ALLOWED_ZOOMS)
        self._update_state_after_move()

        return {
            "available_moves": self.available_moves,
        }

    def __eq__(self, value: object) -> bool:
        if not isinstance(value, StreetViewAPI):
            return False
        return (self._db_path, self._pano_id) == (value._db_path, value._pano_id)

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------

    def check_direction(self) -> Dict[str, Any]:
        """
        Check the current compass direction the camera is facing.

        Returns:
            - description (str): where is the direction facing at the current state(e.g. ``"Facing N (0.0 degrees)"``).
        """
        direction = _heading_to_direction(self._heading)
        description = f"Facing {direction} ({self._heading:.1f} degrees)"
        return {"description": description}

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def _fetch_image(
        self, pano_id: str, heading: float, pitch: float, zoom: float
    ) -> bytes:
        """Return JPEG bytes for a capture, checking cache then GCS then local DB."""
        key = f"{pano_id}/{heading}_{pitch}_{zoom}.jpg"

        if self._cache is not None:
            cached = self._cache.get(key)
            if cached is not None:
                return cached

        if self._gcs_bucket is not None:
            data = self._gcs_bucket.blob(key).download_as_bytes()
            if self._cache is not None:
                self._cache.put(key, data)
            return data

        row = self._query_one(
            "SELECT image_path FROM captures "
            "WHERE pano_id = ? AND heading = ? AND pitch = ? AND zoom = ? "
            "AND image_path IS NOT NULL "
            "LIMIT 1",
            (pano_id, heading, pitch, zoom),
        )
        if not row:
            raise RuntimeError(
                f"No captured image for pano={pano_id} "
                f"heading={heading} pitch={pitch} zoom={zoom}"
            )
        with open(row["image_path"], "rb") as f:
            return f.read()

    def capture_view(self) -> Dict[str, Any]:
        """
        Capture the current panorama image. Returns an image of the current panorama.
        """
        if not self._pano_id:
            raise RuntimeError("No panorama loaded — call _load_scenario first")

        image_bytes = self._fetch_image(
            self._pano_id, self._heading, self._pitch, self._zoom
        )
        return ImageResult(image_bytes=image_bytes, mime_type="image/jpeg")

    # ------------------------------------------------------------------
    # Movements
    # ------------------------------------------------------------------

    def _move_direction(self, direction_key: str) -> Dict[str, Any]:
        """Move to the adjacent panorama in the given compass direction."""
        candidates = [
            link for link in self._links
            if _in_cone(link["heading"], DIR_CONES[direction_key])
        ]
        if not candidates:
            raise RuntimeError(f"No moves available in {_DIR_TO_FULL[direction_key]} direction")

        target = candidates[0]
        next_pano_id = target["panoId"]

        # Verify the target panorama exists in our database
        row = self._query_one(
            "SELECT pano_id FROM panoramas WHERE pano_id = ?",
            (next_pano_id,),
        )
        if not row:
            raise RuntimeError(f"Target panorama {next_pano_id} not found in local database")

        self._pano_id = next_pano_id
        self._update_state_after_move()
        return {"status": "success"}

    def move_north(self) -> Dict[str, Any]:
        """
        Move to the adjacent panorama in the North direction.

        Returns:
            status (bool): True if the operation is successful, False otherwise.
        """
        return self._move_direction("N")

    def move_northeast(self) -> Dict[str, Any]:
        """
        Move to the adjacent panorama in the Northeast direction.

        Returns:
            status (bool): True if the operation is successful, False otherwise.
        """
        return self._move_direction("NE")

    def move_east(self) -> Dict[str, Any]:
        """
        Move to the adjacent panorama in the East direction.

        Returns:
            status (bool): True if the operation is successful, False otherwise.
        """
        return self._move_direction("E")

    def move_southeast(self) -> Dict[str, Any]:
        """
        Move to the adjacent panorama in the Southeast direction.

        Returns:
            status (bool): True if the operation is successful, False otherwise.
        """
        return self._move_direction("SE")

    def move_south(self) -> Dict[str, Any]:
        """
        Move to the adjacent panorama in the South direction.

        Returns:
            status (bool): True if the operation is successful, False otherwise.
        """
        return self._move_direction("S")

    def move_southwest(self) -> Dict[str, Any]:
        """
        Move to the adjacent panorama in the Southwest direction.

        Returns:
            status (bool): True if the operation is successful, False otherwise.
        """
        return self._move_direction("SW")

    def move_west(self) -> Dict[str, Any]:
        """
        Move to the adjacent panorama in the West direction.

        Returns:
            status (bool): True if the operation is successful, False otherwise.
        """
        return self._move_direction("W")

    def move_northwest(self) -> Dict[str, Any]:
        """
        Move to the adjacent panorama in the Northwest direction.

        Returns:
            status (bool): True if the operation is successful, False otherwise.
        """
        return self._move_direction("NW")

    # ------------------------------------------------------------------
    # Scroll (camera rotation) — snapped to allowed grid values
    # ------------------------------------------------------------------

    def scroll_left(self) -> Dict[str, Any]:
        """
        Rotate the camera view to the left (counter-clockwise) by one step. Returns an image of the view.
        """
        new_heading = _normalize_heading(self._heading - SCROLL_HORIZONTAL_STEP)
        self._heading = _snap_to_nearest(new_heading, ALLOWED_HEADINGS)
        return self.capture_view()

    def scroll_right(self) -> Dict[str, Any]:
        """
        Rotate the camera view to the right (clockwise) by one step. Returns an image of the view.
        """
        new_heading = _normalize_heading(self._heading + SCROLL_HORIZONTAL_STEP)
        self._heading = _snap_to_nearest(new_heading, ALLOWED_HEADINGS)
        return self.capture_view()

    def scroll_up(self) -> Dict[str, Any]:
        """
        Tilt the camera view upward by one step. Returns an image of the view.
        """
        idx = ALLOWED_PITCHES.index(self._pitch) if self._pitch in ALLOWED_PITCHES else 2
        if idx < len(ALLOWED_PITCHES) - 1:
            self._pitch = ALLOWED_PITCHES[idx + 1]
        return self.capture_view()

    def scroll_down(self) -> Dict[str, Any]:
        """
        Tilt the camera view downward by one step. Returns an image of the view.
        """
        idx = ALLOWED_PITCHES.index(self._pitch) if self._pitch in ALLOWED_PITCHES else 2
        if idx > 0:
            self._pitch = ALLOWED_PITCHES[idx - 1]
        return self.capture_view()

    # ------------------------------------------------------------------
    # Zoom — snapped to allowed grid values
    # ------------------------------------------------------------------

    def zoom_in(self) -> Dict[str, Any]:
        """
        Zoom the camera view in (increase magnification) by one step. Returns an image of the view.
        """
        idx = ALLOWED_ZOOMS.index(self._zoom) if self._zoom in ALLOWED_ZOOMS else 0
        if idx < len(ALLOWED_ZOOMS) - 1:
            self._zoom = ALLOWED_ZOOMS[idx + 1]
        return self.capture_view()

    def zoom_out(self) -> Dict[str, Any]:
        """
        Zoom the camera view out (decrease magnification) by one step. Returns an image of the view.
        """
        idx = ALLOWED_ZOOMS.index(self._zoom) if self._zoom in ALLOWED_ZOOMS else 0
        if idx > 0:
            self._zoom = ALLOWED_ZOOMS[idx - 1]
        return self.capture_view()

    # ------------------------------------------------------------------
    # Session Control
    # ------------------------------------------------------------------

    def _end_session(self) -> Dict[str, Any]:
        """End the current session and reset state."""
        self._pano_id = None
        self._heading = 0.0
        self._pitch = 0.0
        self._zoom = 1.0
        self._links = []
        self.available_moves = None
        self.session_id = None
        return {"closed": True}
