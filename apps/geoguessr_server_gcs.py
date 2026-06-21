"""
Flask shim exposing remote GCS Bucket implementation (aka Path A) StreetViewAPI over BFCL's HTTP contract.

BFCL's client at
``bfcl_eval/eval_checker/multi_turn_eval/func_source_code/street_view.py``
talks to this server on ``GEOGUESSR_SERVER_URL`` (default
``http://127.0.0.1:18000``). Every request lands here, gets routed to a
per-session ``StreetViewAPI`` instance, and is wrapped in BFCL's response
envelope ``{"ok": bool, "updates": {...}, "error": {...}}``.

Run:
    export MAPCRUNCH_DB_PATH=/abs/path/to/mapcrunch.db
    export MAPCRUNCH_GCS_BUCKET=geoguesr
    export MAPCRUNCH_GCS_PREFIX=mapcrunch_images/
    export MAPCRUNCH_CACHE_DIR=/tmp/mapcrunch_cache
    export MAPCRUNCH_CACHE_SIZE_GB=5
    export GOOGLE_CLOUD_PROJECT=placeholder  # required by ADC, any value works
    python -m apps.geoguessr_server_gcs

Differences from BFCL's expectations that this shim normalizes:
- ``lat``/``lng`` arrive as strings in ``/init_panorama``; coerced to float.
- ``delta`` in scroll/zoom is continuous; translated into N discrete grid
  steps (30 deg horizontal, 15 deg vertical, 1 zoom level), clamped at edges.
- Path A's scroll/zoom return images internally; we discard them since BFCL's
  client follows scroll/zoom with its own ``/capture/view`` call.
- Path A's ``RuntimeError`` (no link in cone, edge-of-range) is caught and
  returned as ``{"ok": false, "error": {"message": ...}}``.
"""
import base64
import io
import logging
import os
import sys
import threading
import uuid
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from PIL import Image

# Make the project root importable when the file is run as a script.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Load .env from the project root if present. Real shell env wins over .env
# (override=False) so callers can still tweak vars at the command line.
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=False)

from apps.geoguessr_wrapper import (  # noqa: E402
    ALLOWED_PITCHES,
    ALLOWED_ZOOMS,
    SCROLL_HORIZONTAL_STEP,
    SCROLL_VERTICAL_STEP,
    ZOOM_STEP,
    StreetViewAPI,
)

# Ensure the GCS client always has a billing project. ADC requires one even
# for buckets you only have object-read access on; the value isn't checked
# when you're not making project-billed calls.
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "placeholder")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("geoguessr_server_gcs")

# ---------------------------------------------------------------------------
# Server-wide configuration (read once at startup)
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("MAPCRUNCH_DB_PATH")
GCS_BUCKET = os.environ.get("MAPCRUNCH_GCS_BUCKET")
GCS_PREFIX = os.environ.get("MAPCRUNCH_GCS_PREFIX", "mapcrunch_images/")
CACHE_DIR = os.environ.get("MAPCRUNCH_CACHE_DIR", "/tmp/mapcrunch_cache")
CACHE_SIZE_GB = float(os.environ.get("MAPCRUNCH_CACHE_SIZE_GB", "5"))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "18000"))

# FOV-crop ablation. GEOGUESSR_CROP = off | top | bottom (read once at boot;
# the running server's value IS the ablation condition, so restart the shim to
# change it). "top" keeps the upper half (sky/skyline/terrain), "bottom" keeps
# the lower half (road/signage/ground). "off" returns the full uncropped image.
CROP_MODE = os.environ.get("GEOGUESSR_CROP", "off").strip().lower()
if CROP_MODE not in ("off", "top", "bottom"):
    log.warning(
        "GEOGUESSR_CROP=%r is invalid (expected off|top|bottom); defaulting to 'off'",
        CROP_MODE,
    )
    CROP_MODE = "off"

if not DB_PATH:
    raise RuntimeError(
        "MAPCRUNCH_DB_PATH is required (absolute path to local mapcrunch.db)."
    )
if not os.path.exists(DB_PATH):
    raise RuntimeError(f"DB file does not exist: {DB_PATH}")
if not GCS_BUCKET:
    log.warning(
        "MAPCRUNCH_GCS_BUCKET is unset; StreetViewAPI will fall back to local "
        "captures.image_path lookups, which won't exist on this machine."
    )

# ---------------------------------------------------------------------------
# Session registry
# ---------------------------------------------------------------------------

_SESSIONS: Dict[str, StreetViewAPI] = {}
_SESSIONS_LOCK = threading.Lock()


def _new_session_id() -> str:
    return uuid.uuid4().hex


def _build_api() -> StreetViewAPI:
    return StreetViewAPI(
        db_path=DB_PATH,
        gcs_bucket=GCS_BUCKET,
        gcs_prefix=GCS_PREFIX,
        cache_dir=CACHE_DIR,
        cache_size_gb=CACHE_SIZE_GB,
    )


def _get_session() -> Optional[StreetViewAPI]:
    sid = request.headers.get("X-Session-ID")
    if not sid:
        return None
    with _SESSIONS_LOCK:
        return _SESSIONS.get(sid)


# ---------------------------------------------------------------------------
# Response envelope helpers
# ---------------------------------------------------------------------------

def ok(updates: Optional[Dict[str, Any]] = None, status: int = 200):
    return jsonify({"ok": True, "updates": updates or {}}), status


def fail(message: str, status: int = 400):
    return jsonify({"ok": False, "error": {"message": message}}), status


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/connect", methods=["POST"])
def route_connect():
    """Mint a new session and instantiate a per-session StreetViewAPI.

    Body may contain ``api_key`` / ``url_signing_secret`` / ``session_id``;
    Path A doesn't need the first two, and we always mint a fresh id (BFCL
    accepts and pins whatever id we return).
    """
    try:
        api = _build_api()
    except Exception as e:
        log.exception("Failed to build StreetViewAPI")
        return fail(f"Failed to initialize StreetViewAPI: {e}", status=500)

    sid = _new_session_id()
    with _SESSIONS_LOCK:
        _SESSIONS[sid] = api
    log.info("Connected session %s (active=%d)", sid, len(_SESSIONS))
    return ok({"session_id": sid})


@app.route("/init_panorama", methods=["POST"])
def route_init_panorama():
    api = _get_session()
    if api is None:
        return fail("Unknown session — call /connect first", status=400)

    body = request.get_json(force=True, silent=True) or {}

    # BFCL forwards lat/lng as strings; Path A's _load_scenario does float
    # arithmetic on them. Coerce here at the boundary.
    try:
        scenario = {
            "lat": float(body.get("lat", 0.0)),
            "lng": float(body.get("lng", 0.0)),
            "heading": float(body.get("heading", 0.0)),
            "pitch": float(body.get("pitch", 0.0)),
            "zoom": float(body.get("zoom", 1.0)),
        }
    except (TypeError, ValueError) as e:
        return fail(f"Invalid scenario value: {e}")

    try:
        api._load_scenario(scenario)
    except Exception as e:
        log.exception("init_panorama failed")
        return fail(str(e))

    return ok({"available_moves": api.available_moves})


@app.route("/check/direction", methods=["GET"])
def route_check_direction():
    api = _get_session()
    if api is None:
        return fail("Unknown session — call /connect first", status=400)
    try:
        result = api.check_direction()
    except Exception as e:
        return fail(str(e))
    return ok(
        {
            "description": result.get("description", ""),
            "available_moves": api.available_moves or [],
        }
    )


def _apply_crop(image_base64: str) -> str:
    """Apply the FOV-crop ablation to a base64 JPEG, per CROP_MODE.

    'top'    -> upper half  (0, 0, W, H//2)   (sky/skyline/terrain)
    'bottom' -> lower half  (0, H//2, W, H)   (road/signage/ground)
    'off'    -> returned unchanged.

    Returns raw base64 with NO data-URL prefix (the BFCL client wraps the mime
    type separately, so a prefix would corrupt the image).
    """
    if CROP_MODE == "off":
        return image_base64
    im = Image.open(io.BytesIO(base64.b64decode(image_base64)))
    width, height = im.size
    if CROP_MODE == "top":
        im = im.crop((0, 0, width, height // 2))
    else:  # "bottom"
        im = im.crop((0, height // 2, width, height))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=95)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


@app.route("/capture/view", methods=["POST"])
def route_capture_view():
    api = _get_session()
    if api is None:
        return fail("Unknown session — call /connect first", status=400)
    try:
        img = api.capture_view()
    except Exception as e:
        return fail(str(e))

    # FOV-crop ablation: every captured image passes through this single
    # chokepoint regardless of navigation path. _apply_crop is a no-op when
    # CROP_MODE == "off". GCS originals and the disk LRU stay uncropped — the
    # crop is in-memory on the way out only.
    image_base64 = _apply_crop(img.image_base64)

    return ok(
        {
            "image_base64": image_base64,
            "available_moves": api.available_moves or [],
        }
    )


# --- Move ------------------------------------------------------------------

_MOVE_METHODS = {
    "north": "move_north",
    "northeast": "move_northeast",
    "east": "move_east",
    "southeast": "move_southeast",
    "south": "move_south",
    "southwest": "move_southwest",
    "west": "move_west",
    "northwest": "move_northwest",
}


@app.route("/move/<direction>", methods=["POST"])
def route_move(direction):
    api = _get_session()
    if api is None:
        return fail("Unknown session — call /connect first", status=400)
    method_name = _MOVE_METHODS.get(direction)
    if method_name is None:
        return fail(f"Unknown direction: {direction}")
    try:
        getattr(api, method_name)()
    except Exception as e:
        return fail(str(e))
    return ok({"available_moves": api.available_moves or []})


# --- Scroll / Zoom: continuous delta → N discrete grid steps --------------


def _apply_repeated(api: StreetViewAPI, method_name: str, steps: int) -> None:
    """Call api.<method_name>() up to `steps` times, stopping on edge clamps.

    Path A raises RuntimeError when scroll_up/down/zoom_in/out is already at
    an extreme of its allowed range; we treat that as a soft clamp rather
    than a request failure (matches the docstring promise that
    scroll_up "is clamped so the resulting pitch does not exceed 90"). The
    horizontal scrolls (scroll_left/right) wrap and never raise.
    """
    method = getattr(api, method_name)
    for _ in range(steps):
        try:
            method()
        except RuntimeError:
            # Edge of pitch/zoom range — stop, leave state at the clamp.
            break


def _delta_to_steps(delta: Any, step_size: float) -> int:
    """``delta`` arrives as float|int|str; coerce, take absolute value, round."""
    try:
        d = abs(float(delta))
    except (TypeError, ValueError):
        return 0
    return int(round(d / step_size))


@app.route("/scroll/<direction>", methods=["POST"])
def route_scroll(direction):
    api = _get_session()
    if api is None:
        return fail("Unknown session — call /connect first", status=400)
    body = request.get_json(force=True, silent=True) or {}
    delta = body.get("delta", 0)

    if direction in ("left", "right"):
        steps = _delta_to_steps(delta, SCROLL_HORIZONTAL_STEP)
        method_name = f"scroll_{direction}"
    elif direction in ("up", "down"):
        steps = _delta_to_steps(delta, SCROLL_VERTICAL_STEP)
        method_name = f"scroll_{direction}"
    else:
        return fail(f"Unknown scroll direction: {direction}")

    try:
        _apply_repeated(api, method_name, steps)
    except Exception as e:
        return fail(str(e))

    return ok({"available_moves": api.available_moves or []})


@app.route("/zoom/<direction>", methods=["POST"])
def route_zoom(direction):
    api = _get_session()
    if api is None:
        return fail("Unknown session — call /connect first", status=400)
    if direction not in ("in", "out"):
        return fail(f"Unknown zoom direction: {direction}")

    body = request.get_json(force=True, silent=True) or {}
    delta = body.get("delta", 0)
    steps = _delta_to_steps(delta, ZOOM_STEP)
    method_name = f"zoom_{direction}"

    try:
        _apply_repeated(api, method_name, steps)
    except Exception as e:
        return fail(str(e))

    return ok({"available_moves": api.available_moves or []})


# --- Session teardown -----------------------------------------------------


@app.route("/end_session", methods=["POST"])
def route_end_session():
    sid = request.headers.get("X-Session-ID")
    if not sid:
        return fail("Missing X-Session-ID header")
    with _SESSIONS_LOCK:
        api = _SESSIONS.pop(sid, None)
    if api is None:
        return fail("Unknown session", status=404)
    try:
        api._end_session()
    except Exception:
        log.exception("end_session cleanup failed (ignored)")
    log.info("Ended session %s (active=%d)", sid, len(_SESSIONS))
    return ok({})


# --- Optional debug helpers (not in BFCL's contract; safe to keep) --------


@app.route("/health", methods=["GET"])
def route_health():
    with _SESSIONS_LOCK:
        active = len(_SESSIONS)
    return jsonify(
        {
            "ok": True,
            "db": DB_PATH,
            "bucket": GCS_BUCKET,
            "prefix": GCS_PREFIX,
            "cache_dir": CACHE_DIR,
            "active_sessions": active,
            "allowed": {
                "pitches": ALLOWED_PITCHES,
                "zooms": ALLOWED_ZOOMS,
                "horizontal_step": SCROLL_HORIZONTAL_STEP,
                "vertical_step": SCROLL_VERTICAL_STEP,
                "zoom_step": ZOOM_STEP,
            },
        }
    )


if __name__ == "__main__":
    log.info(
        "Starting Path A geoguessr server: host=%s port=%d db=%s bucket=%s prefix=%s",
        HOST,
        PORT,
        DB_PATH,
        GCS_BUCKET,
        GCS_PREFIX,
    )
    app.run(host=HOST, port=PORT, threaded=True)
