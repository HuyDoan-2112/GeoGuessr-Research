"""Navigation over the recorded panorama grid used by BFCL."""

import json
import math
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

ALLOWED_HEADINGS = tuple(float(value) for value in range(0, 360, 30))
ALLOWED_PITCHES = (-30.0, -15.0, 0.0, 15.0, 30.0)
ALLOWED_ZOOMS = (0.0, 1.0, 2.0)
SCROLL_HORIZONTAL_STEP = 30.0
SCROLL_VERTICAL_STEP = 15.0
ZOOM_STEP = 1.0

DIRECTIONS = (
    ("N", "north"),
    ("NE", "northeast"),
    ("E", "east"),
    ("SE", "southeast"),
    ("S", "south"),
    ("SW", "southwest"),
    ("W", "west"),
    ("NW", "northwest"),
)
UNIVERSAL_ACTIONS = (
    "capture_view",
    "check_direction",
    "scroll_up",
    "scroll_left",
    "scroll_right",
    "scroll_down",
    "zoom_in",
    "zoom_out",
)


def _number(value, name):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"Invalid {name} value") from error
    if not math.isfinite(number):
        raise ValueError(f"Invalid {name} value")
    return number


def _direction(heading):
    return DIRECTIONS[int(((float(heading) % 360.0) + 22.5) % 360.0 / 45.0)]


def _grid_value(value, allowed, name):
    if value not in allowed:
        raise ValueError(f"Invalid {name} value {value}; must be one of {list(allowed)}")
    return value


class StreetViewSession:
    """One navigation session; the HTTP server serializes access to it.

    Metadata is read from SQLite and images come from the shared image store.
    Database connections are closed after each initialization or move.
    Only ``capture_view`` fetches image bytes.
    """

    def __init__(self, db_path, image_store):
        path = Path(db_path).expanduser().resolve()
        if not path.is_file():
            raise sqlite3.OperationalError("unable to open database file")
        self._db_uri = path.as_uri() + "?mode=ro"
        self._closed = False
        self._image_store = image_store
        self._pano_id = None
        self._heading = 0.0
        self._pitch = 0.0
        self._zoom = 1.0
        self._links = []
        self.available_moves = []

    @contextmanager
    def _database(self):
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
        with closing(sqlite3.connect(self._db_uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            yield conn

    def _load_links(self, conn, pano_id):
        row = conn.execute(
            "SELECT metadata_json FROM panoramas WHERE pano_id = ?", (pano_id,)
        ).fetchone()
        if row and row["metadata_json"]:
            try:
                metadata = json.loads(row["metadata_json"])
                if isinstance(metadata, dict) and metadata.get("links"):
                    return metadata["links"]
            except (json.JSONDecodeError, TypeError):
                pass

        rows = conn.execute(
            "SELECT to_pano_id, heading FROM panorama_links WHERE from_pano_id = ?",
            (pano_id,),
        ).fetchall()
        return [{"panoId": row["to_pano_id"], "heading": row["heading"]} for row in rows]

    def _set_panorama(self, conn, pano_id):
        links = self._load_links(conn, pano_id)
        moves = list(UNIVERSAL_ACTIONS)
        for link in links:
            action = f"move_{_direction(link['heading'])[1]}"
            if action not in moves:
                moves.append(action)
        self._pano_id = pano_id
        self._links = links
        self.available_moves = moves

    def _updates(self):
        return {"available_moves": self.available_moves}

    def _require_panorama(self):
        if self._pano_id is None:
            raise RuntimeError("No panorama loaded — call /init_panorama first")

    def init_panorama(self, lat, lng, heading=0.0, pitch=0.0, zoom=1.0):
        lat = _number(lat, "lat")
        lng = _number(lng, "lng")
        heading = _grid_value(_number(heading, "heading") % 360.0, ALLOWED_HEADINGS, "heading")
        pitch = _grid_value(_number(pitch, "pitch"), ALLOWED_PITCHES, "pitch")
        zoom = _grid_value(_number(zoom, "zoom"), ALLOWED_ZOOMS, "zoom")
        with self._database() as conn:
            row = conn.execute(
                "SELECT pano_id FROM panoramas "
                "WHERE lat IS NOT NULL AND lng IS NOT NULL "
                "ORDER BY (lat - ?) * (lat - ?) + (lng - ?) * (lng - ?) LIMIT 1",
                (lat, lat, lng, lng),
            ).fetchone()
            if row is None:
                raise RuntimeError("No panoramas in local database")
            self._set_panorama(conn, row["pano_id"])
        self._heading, self._pitch, self._zoom = heading, pitch, zoom
        return self._updates()

    def check_direction(self):
        self._require_panorama()
        return {
            "description": f"Facing {_direction(self._heading)[0]} ({self._heading:.1f} degrees)",
            **self._updates(),
        }

    def move(self, direction):
        self._require_panorama()
        if direction not in {full for _, full in DIRECTIONS}:
            raise ValueError(f"Unknown direction: {direction}")
        target = next(
            (link for link in self._links if _direction(link["heading"])[1] == direction),
            None,
        )
        if target is None:
            raise RuntimeError(f"No moves available in {direction} direction")
        pano_id = target["panoId"]
        with self._database() as conn:
            if conn.execute(
                "SELECT 1 FROM panoramas WHERE pano_id = ?", (pano_id,)
            ).fetchone() is None:
                raise RuntimeError(f"Target panorama {pano_id} not found in local database")
            self._set_panorama(conn, pano_id)
        return self._updates()

    def scroll(self, direction, delta):
        self._require_panorama()
        delta = abs(_number(delta, "delta"))
        if direction in ("left", "right"):
            steps = round(delta / SCROLL_HORIZONTAL_STEP) % len(ALLOWED_HEADINGS)
            sign = -1 if direction == "left" else 1
            self._heading = (self._heading + sign * steps * SCROLL_HORIZONTAL_STEP) % 360.0
        elif direction in ("up", "down"):
            steps = round(delta / SCROLL_VERTICAL_STEP)
            index = ALLOWED_PITCHES.index(self._pitch)
            index += steps if direction == "up" else -steps
            self._pitch = ALLOWED_PITCHES[max(0, min(index, len(ALLOWED_PITCHES) - 1))]
        else:
            raise ValueError(f"Unknown scroll direction: {direction}")
        return self._updates()

    def zoom(self, direction, delta):
        self._require_panorama()
        if direction not in ("in", "out"):
            raise ValueError(f"Unknown zoom direction: {direction}")
        steps = round(abs(_number(delta, "delta")) / ZOOM_STEP)
        index = ALLOWED_ZOOMS.index(self._zoom)
        index += steps if direction == "in" else -steps
        self._zoom = ALLOWED_ZOOMS[max(0, min(index, len(ALLOWED_ZOOMS) - 1))]
        return self._updates()

    def capture_view(self):
        self._require_panorama()
        return self._image_store.get(self._pano_id, self._heading, self._pitch, self._zoom)

    def close(self):
        self._closed = True
        self._pano_id = None
        self._links = []
        self.available_moves = []
