import json
import math
import os
import random
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
import logging

from flask import Flask, request, jsonify

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.navigation import pure_nav
from core.tools import nav_tools
from core.tools.contracts import ToolContext
from adapters.streetview_js.client import StreetViewHostClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SESSION_IDLE_TIMEOUT = float(os.getenv("SESSION_IDLE_TIMEOUT", "300"))  # 5 min
SESSION_MAX_AGE = float(os.getenv("SESSION_MAX_AGE", "1800"))  # 30 min
SWEEP_INTERVAL = float(os.getenv("SESSION_SWEEP_INTERVAL", "60"))  # 1 min

# ---------------------------------------------------------------------------
# Per-session locks
# ---------------------------------------------------------------------------
_SESSION_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Engine registry lock
# ---------------------------------------------------------------------------
ENGINES_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Session tracking for sweeper (NEW)
# ---------------------------------------------------------------------------
SESSION_CREATED: Dict[str, float] = {}
SESSION_LAST_ACTIVE: Dict[str, float] = {}
SESSION_TRACKING_LOCK = threading.Lock()

def _get_session_lock(sid: str) -> threading.Lock:
    with _LOCKS_LOCK:
        if sid not in _SESSION_LOCKS:
            _SESSION_LOCKS[sid] = threading.Lock()
        return _SESSION_LOCKS[sid]


def _drop_session(sid: str) -> None:
    """Remove session from all registries"""
    with _LOCKS_LOCK:
        _SESSION_LOCKS.pop(sid, None)
    with SESSION_TRACKING_LOCK:
        SESSION_CREATED.pop(sid, None)
        SESSION_LAST_ACTIVE.pop(sid, None)
    with ENGINES_LOCK:
        engines.pop(sid, None)

def _register_session(sid: str) -> None:
    """Track new session creation time."""
    now = time.time()
    with SESSION_TRACKING_LOCK:
        SESSION_CREATED[sid] = now
        SESSION_LAST_ACTIVE[sid] = now
    logger.info(f"Session registered: {sid}")

def _touch_session(sid: str) -> None:
    """Update last active time for session."""
    now = time.time()
    with SESSION_TRACKING_LOCK:
        if sid in SESSION_LAST_ACTIVE:
            SESSION_LAST_ACTIVE[sid] = now
            logger.debug(f"Session touched: {sid}")
        else:
            logger.warning(f"Attempted to touch unknown session: {sid}")

def _get_session_info(sid: str) -> Optional[Dict[str, float]]:
    """Get session tracking info."""
    with SESSION_TRACKING_LOCK:
        if sid not in SESSION_CREATED:
            return None
        now = time.time()
        created = SESSION_CREATED.get(sid, now)
        last_active = SESSION_LAST_ACTIVE.get(sid, now)
        return {
            "session_id": sid,
            "created_at": created,
            "last_active": last_active,
            "age_seconds": now - created,
            "idle_seconds": now - last_active,
        }
    
# ---------------------------------------------------------------------------
# Sweeper Thread (NEW)
# ---------------------------------------------------------------------------
def _sweep_zombie_sessions() -> None:
    """Periodically close idle or expired sessions."""
    logger.info(f"Sweeper started: idle_timeout={SESSION_IDLE_TIMEOUT}s, max_age={SESSION_MAX_AGE}s")
    
    while True:
        time.sleep(SWEEP_INTERVAL)
        
        now = time.time()
        zombies = []
        
        # Find zombie sessions
        with SESSION_TRACKING_LOCK:
            for sid in list(SESSION_CREATED.keys()):
                created = SESSION_CREATED.get(sid, now)
                last_active = SESSION_LAST_ACTIVE.get(sid, now)
                
                age = now - created
                idle = now - last_active
                
                if age > SESSION_MAX_AGE:
                    zombies.append((sid, f"max_age_exceeded ({age:.0f}s > {SESSION_MAX_AGE}s)"))
                elif idle > SESSION_IDLE_TIMEOUT:
                    zombies.append((sid, f"idle_timeout ({idle:.0f}s > {SESSION_IDLE_TIMEOUT}s)"))
        
        # Close zombie sessions
        for sid, reason in zombies:
            logger.warning(f"Sweeping zombie session {sid}: {reason}")
            try:
                with ENGINES_LOCK:
                    eng = engines.get(sid)
                if eng:
                    try:
                        eng.end_session()
                    except Exception as e:
                        logger.error(f"Error ending session {sid}: {e}")
            except Exception as e:
                logger.error(f"Sweep error for {sid}: {e}")
            finally:
                _drop_session(sid)
        
        if zombies:
            logger.info(f"Swept {len(zombies)} zombie sessions")


# Start sweeper thread on module load
_sweeper_thread = threading.Thread(target=_sweep_zombie_sessions, daemon=True, name="session-sweeper")
_sweeper_thread.start()


# ---------------------------------------------------------------------------
# Engine State
# ---------------------------------------------------------------------------

@dataclass
class EngineState:
    """Server-side state for the GeoGuessr navigation engine (per-session)."""

    # Configuration
    random_seed: int = 1053
    image_root: str = field(default_factory=lambda: os.getenv("IMAGE_OUTPUT_DIR", "images"))
    session_id: Optional[str] = None

    # Per-session progress
    step_count: int = 0

    # Navigation
    pano_id: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    heading: float = 0.0
    pitch: float = 0.0
    zoom: float = 1.0

    # Context
    date: Optional[str] = None
    links: List[Dict[str, Any]] = field(default_factory=list)
    available_moves: List[str] = field(default_factory=list)

    # Outputs
    image_path: Optional[str] = None
    image_base64: Optional[str] = None
    _image_step: int = 1


# ---------------------------------------------------------------------------
# Engine (server-side logic, manages host + state + nav_tools)
# ---------------------------------------------------------------------------

class Engine:
    """
    Server-side GeoGuessr navigation engine.

    Manages StreetViewHostClient, ToolContext, nav_tools execution,
    image capture, and state tracking (per session).
    """

    MOVE_TOOLS = {
        "north":     nav_tools.move_north,
        "northeast": nav_tools.move_northeast,
        "east":      nav_tools.move_east,
        "southeast": nav_tools.move_southeast,
        "south":     nav_tools.move_south,
        "southwest": nav_tools.move_southwest,
        "west":      nav_tools.move_west,
        "northwest": nav_tools.move_northwest,
    }

    SCROLL_TOOLS = {
        "left":  nav_tools.scroll_left,
        "right": nav_tools.scroll_right,
        "up":    nav_tools.scroll_up,
        "down":  nav_tools.scroll_down,
    }

    ZOOM_TOOLS = {
        "in":  nav_tools.zoom_in,
        "out": nav_tools.zoom_out,
    }

    def __init__(self):
        self.state = EngineState()
        self._random = random.Random(self.state.random_seed)
        self._host_client: Optional[StreetViewHostClient] = None
        self._ctx: Optional[ToolContext] = None

    # --- Internal helpers ---

    def _host_enabled(self) -> bool:
        return (
            self._host_client is not None
            and self.state.session_id
            and self._ctx is not None
        )

    def _set_host_context(self, client: StreetViewHostClient, session_id: str) -> None:
        self._host_client = client
        self.state.session_id = session_id
        self._ctx = ToolContext(
            session_id=session_id,
            meta={
                "host_client": client,
                "image_root": self.state.image_root,
                "image_step": self.state._image_step,
            },
        )

    def _ensure_ctx(self) -> ToolContext:
        if not self._host_enabled():
            raise RuntimeError("Host not connected")
        assert self._ctx is not None
        self._ctx.meta["host_client"] = self._host_client
        self._ctx.meta["image_root"] = self.state.image_root
        return self._ctx

    def _sync_image_step(self) -> None:
        if self._ctx is None:
            return
        self.state._image_step = self._ctx.meta.get("image_step", self.state._image_step)

    def _apply_tool_result(self, result) -> None:
        """
        Update the engine state with the result of a tool execution.
        """
        updates = getattr(result, "updates", {}) or {}
        # @HuanzhiMao FIXME: no need to store image_path and image_base64 in the state
        # Also, no capture view after each move, unless called explicitly
        if "image_path" in updates:
            self.state.image_path = updates["image_path"]
        if "image_base64" in updates:
            self.state.image_base64 = updates["image_base64"]
        if "available_moves" in updates:
            self.state.available_moves = updates["available_moves"]
        self._sync_image_step()

    def _tool_error(self, result, fallback: str) -> str:
        debug = getattr(result, "debug", None) or {}
        if isinstance(debug, dict):
            return debug.get("error") or debug.get("message") or fallback
        return fallback

    def _pull_host_state(self) -> None:
        if not self._host_enabled():
            return
        assert self._host_client is not None
        host_state = self._host_client.get_state(self.state.session_id)
        self._update_from_host_state(host_state)

    def _build_host_state(self) -> Dict[str, Any]:
        return {
            "panoId": self.state.pano_id,
            "position": {"lat": self.state.lat, "lng": self.state.lng},
            "pov": {
                "heading": self.state.heading,
                "pitch": self.state.pitch,
                "zoom": self.state.zoom,
            },
            "links": self.state.links,
            "date": self.state.date,
        }

    def _update_from_host_state(self, state: Dict[str, Any]) -> None:
        self.state.pano_id = state.get("panoId")
        position = state.get("position") or {}
        self.state.lat = position.get("lat")
        self.state.lng = position.get("lng")

        pov = state.get("pov") or {}
        self.state.heading = pov.get("heading", 0.0)
        self.state.pitch = pov.get("pitch", 0.0)
        self.state.zoom = pov.get("zoom", 1.0)

        self.state.links = state.get("links") or []
        self.state.date = state.get("date")

    def _state_snapshot(self) -> Dict[str, Any]:
        return {
            "pano_id": self.state.pano_id,
            "lat": self.state.lat,
            "lng": self.state.lng,
            "heading": self.state.heading,
            "pitch": self.state.pitch,
            "zoom": self.state.zoom,
            "available_moves": self.state.available_moves,
            "step_count": self.state.step_count,
            "image_path": self.state.image_path,
        }

    # --- Public actions (called by Flask routes) ---

    def connect(self, api_key: str, session_id: Optional[str] = None) -> Dict[str, Any]:
        client = StreetViewHostClient()
        if not session_id:
            session_id = f"session_{uuid.uuid4().hex}"
        client.start(session_id, api_key=api_key)
        self._set_host_context(client, session_id)

        # reset per-session counters/outputs
        self.state.step_count = 0
        self.state._image_step = 1
        self.state.image_path = None

        return {"session_id": session_id, "available_moves": self.state.available_moves}

    def init_panorama(
        self,
        lat: float,
        lng: float,
        heading: float = 0.0,
        pitch: float = 0.0,
        zoom: float = 1.0,
    ) -> Dict[str, Any]:
        if not self._host_enabled():
            raise RuntimeError("Host not connected")
        ctx = self._ensure_ctx()
        result = nav_tools.init_panorama(
            ctx,
            {"lat": lat, "lng": lng, "heading": heading, "pitch": pitch, "zoom": zoom},
        )
        if not result.ok:
            raise RuntimeError(self._tool_error(result, "init_failed"))

        self._apply_tool_result(result)
        self._pull_host_state()

        return {
            "available_moves": self.state.available_moves,
        }

    def move(self, direction: str) -> Dict[str, Any]:

        fn = self.MOVE_TOOLS.get(direction)
        if not fn:
            raise ValueError(f"Invalid direction: {direction}")

        ctx = self._ensure_ctx()
        result = fn(ctx, {})
        if not result.ok:
            raise RuntimeError(self._tool_error(result, "move_failed"))

        self.state.step_count += 1
        self._apply_tool_result(result)
        self._pull_host_state()

        return {
            "available_moves": self.state.available_moves,
        }

    def scroll(self, direction: str, delta: float) -> Dict[str, Any]:
        if delta is None or not math.isfinite(delta):
            raise ValueError("Invalid delta value")

        fn = self.SCROLL_TOOLS.get(direction)
        if not fn:
            raise ValueError(f"Invalid scroll direction: {direction}")

        ctx = self._ensure_ctx()
        result = fn(ctx, {"delta": delta})
        if not result.ok:
            raise RuntimeError(self._tool_error(result, "scroll_failed"))

        self._apply_tool_result(result)
        self._pull_host_state()

        return {
            "available_moves": self.state.available_moves,
        }

    def zoom(self, direction: str, delta: float) -> Dict[str, Any]:
        if delta is None or not math.isfinite(delta):
            raise ValueError("Invalid delta value")

        fn = self.ZOOM_TOOLS.get(direction)
        if not fn:
            raise ValueError(f"Invalid zoom direction: {direction}")

        ctx = self._ensure_ctx()
        result = fn(ctx, {"delta": delta})
        if not result.ok:
            raise RuntimeError(self._tool_error(result, "zoom_failed"))

        self._apply_tool_result(result)
        self._pull_host_state()

        return {
            "available_moves": self.state.available_moves,
        }

    def end_session(self) -> Dict[str, Any]:
        """
        Ends the session: notifies the host to close the session, then resets navigation state.
        """
        result_data = {
            "step_count": self.state.step_count,
        }

        # Notify the host to close the session
        if self._host_client is not None and self.state.session_id:
            try:
                self._host_client.close_session(self.state.session_id)
            except Exception:
                # Log but don't fail the end_session call if host cleanup fails
                pass

        # Reset navigation state
        self.state.pano_id = None
        self.state.lat = None
        self.state.lng = None
        self.state.heading = 0.0
        self.state.pitch = 0.0
        self.state.zoom = 1.0
        self.state.links = []
        self.state.available_moves = []
        self.state.date = None

        # Reset outputs/counters
        self.state.image_path = None
        self.state._image_step = 1
        self.state.step_count = 0

        return result_data

    def check_direction(self) -> Dict[str, Any]:
        ctx = self._ensure_ctx()
        result = nav_tools.check_direction(ctx, {})
        if not result.ok:
            raise RuntimeError(self._tool_error(result, "direction_failed"))
        updates = result.updates or {}
        self._apply_tool_result(result)
        return {"description": updates.get("description", ""), "available_moves": self.state.available_moves}

    def check_available_moves(self) -> Dict[str, Any]:
        ctx = self._ensure_ctx()
        result = nav_tools.check_available_moves(ctx, {})
        if not result.ok:
            raise RuntimeError(self._tool_error(result, "moves_failed"))
        self._apply_tool_result(result)
        return {"available_moves": self.state.available_moves}

    def capture_view(self) -> Dict[str, Any]:
        ctx = self._ensure_ctx()
        result = nav_tools.capture_view(ctx, {})
        if not result.ok:
            raise RuntimeError(self._tool_error(result, "capture_failed"))
        self._apply_tool_result(result)
        self._pull_host_state()
        return {
            # @HuanzhiMao FIXME: we should just call result.image_base64
            "image_base64": self.state.image_base64,
            "available_moves": self.state.available_moves,
        }

# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------

app = Flask(__name__)
engines: Dict[str, Engine] = {}


def ok_response(data: Dict[str, Any]):
    return jsonify({"ok": True, "updates": data, "error": {}})


def error_response(message: str, status: int = 400):
    return jsonify({"ok": False, "updates": {}, "error": {"message": message}}), status


def _get_engine():
    """Look up the Engine and touch session timestamp."""
    sid = request.headers.get("X-Session-ID")
    if not sid:
        return None
    with ENGINES_LOCK:
        eng = engines.get(sid)
    if not eng:
        return None
    _touch_session(sid) # Update last_active on every request
    return eng


def safe_call(fn, *args, **kwargs):
    """Execute engine method, return ok/error envelope."""
    try:
        data = fn(*args, **kwargs)
        return ok_response(data)
    except (ValueError, RuntimeError) as e:
        return error_response(str(e), 400)
    except Exception as e:
        app.logger.exception("Unhandled server error")
        return error_response(str(e), 500)


@app.route("/connect", methods=["POST"])
def route_connect():
    body = request.get_json(force=True, silent=True) or {}
    api_key = body.get("api_key") or os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        return error_response("GOOGLE_MAPS_API_KEY not set")
    eng = Engine()
    try:
        data = eng.connect(api_key, body.get("session_id"))
    except Exception as e:
        return error_response(str(e))
    sid = data["session_id"]
    with ENGINES_LOCK:
        engines[sid] = eng
    _register_session(sid) # Track session creation
    return ok_response(data)


@app.route("/init_panorama", methods=["POST"])
def route_init_panorama():
    eng = _get_engine()
    if not eng:
        return error_response("Unknown session — call /connect first")
    sid = request.headers.get("X-Session-ID")
    body = request.get_json(force=True, silent=True) or {}

    # NOTE: kept your behavior (defaults), but you may want to validate lat/lng later.
    with _get_session_lock(sid):
        return safe_call(
            eng.init_panorama,
            lat=body.get("lat", 0.0),
            lng=body.get("lng", 0.0),
            heading=body.get("heading", 0.0),
            pitch=body.get("pitch", 0.0),
            zoom=body.get("zoom", 1.0),
        )


@app.route("/move/<direction>", methods=["POST"])
def route_move(direction):
    eng = _get_engine()
    if not eng:
        return error_response("Unknown session — call /connect first")
    sid = request.headers.get("X-Session-ID")
    with _get_session_lock(sid):
        return safe_call(eng.move, direction)


@app.route("/scroll/<direction>", methods=["POST"])
def route_scroll(direction):
    eng = _get_engine()
    if not eng:
        return error_response("Unknown session — call /connect first")
    sid = request.headers.get("X-Session-ID")
    body = request.get_json(force=True, silent=True) or {}
    delta = body.get("delta", 0.0)
    with _get_session_lock(sid):
        return safe_call(eng.scroll, direction, delta)


@app.route("/zoom/<direction>", methods=["POST"])
def route_zoom(direction):
    eng = _get_engine()
    if not eng:
        return error_response("Unknown session — call /connect first")
    sid = request.headers.get("X-Session-ID")
    body = request.get_json(force=True, silent=True) or {}
    delta = body.get("delta", 0.0)
    with _get_session_lock(sid):
        return safe_call(eng.zoom, direction, delta)


@app.route("/end_session", methods=["POST"])
def route_end_session():
    eng = _get_engine()
    if not eng:
        return error_response("Unknown session — call /connect first")
    sid = request.headers.get("X-Session-ID")
    with _get_session_lock(sid):
        resp = safe_call(eng.end_session)
    payload = resp[0].get_json() if isinstance(resp, tuple) else resp.get_json()
    if payload and payload.get("ok"):
        _drop_session(sid) # This now also cleans up tracking
    return resp


@app.route("/check/direction", methods=["GET"])
def route_check_direction():
    eng = _get_engine()
    if not eng:
        return error_response("Unknown session — call /connect first")
    sid = request.headers.get("X-Session-ID")
    with _get_session_lock(sid):
        return safe_call(eng.check_direction)


@app.route("/check/available_moves", methods=["GET"])
def route_check_available_moves():
    eng = _get_engine()
    if not eng:
        return error_response("Unknown session - call /connect first")
    sid = request.headers.get("X-Session-ID")
    with _get_session_lock(sid):
        return safe_call(eng.check_available_moves)


@app.route("/capture/view", methods=["POST"])
def route_capture_view():
    eng = _get_engine()
    if not eng:
        return error_response("Unknown session - call /connect first")
    sid = request.headers.get("X-Session-ID")
    with _get_session_lock(sid):
        return safe_call(eng.capture_view)


# @HuanzhiMao FIXME: do bytes conversion in the caller wrapper
@app.route("/state", methods=["GET"])
def route_state():
    eng = _get_engine()
    if not eng:
        return error_response("Unknown session — call /connect first")
    return ok_response(eng._state_snapshot())


@app.route("/health", methods=["GET"])
def route_health():
    with ENGINES_LOCK:
        active = len(engines)
    return ok_response({"status": "ok", "active_sessions": active})

# Observability endpoints
@app.route("/sessions", methods=["GET"])
def route_sessions():
    """List all sessions with their tracking info (for debugguing)."""
    sessions = []
    with ENGINES_LOCK:
        engine_ids = list(engines.keys())
    for sid in engine_ids:
        info = _get_session_info(sid)
        if info:
            sessions.append(info)
    return ok_response({
        "sessions": sessions,
        "count": len(sessions),
        "config": {
            "idle_timeout": SESSION_IDLE_TIMEOUT,
            "max_age": SESSION_MAX_AGE,
            "sweep_interval": SWEEP_INTERVAL,
        }
    })


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    
    port = int(os.getenv("SERVER_PORT", "8000"))
    use_waitress = os.getenv("USE_WAITRESS", "").lower() in {"1", "true", "yes"}
    if use_waitress:
        try:
            from waitress import serve
            serve(app, host="0.0.0.0", port=port)
        except ImportError:
            app.run(host="0.0.0.0", port=port)
    else:
        app.run(host="0.0.0.0", port=port)
