"""
GeoGuessr Navigation Server.

Flask server that exposes the GeoGuessr navigation engine as REST endpoints.
Runs inside Docker alongside Playwright/Street View.

All core.*, adapters.*, and nav_tools logic lives here — the client
wrapper (geoguessr_wrapper.py) communicates via HTTP only.
"""

import json
import math
import os
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, request, jsonify

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.navigation import pure_nav
from core.utils.image_pipeline import capture_state_image
from core.tools import nav_tools
from core.tools.contracts import ToolContext
from adapters.streetview_js.client import StreetViewHostClient

# ---------------------------------------------------------------------------
# Engine State
# ---------------------------------------------------------------------------

@dataclass
class EngineState:
    """Server-side state for the GeoGuessr navigation engine."""

    # Configuration
    random_seed: int = 42
    image_root: str = "images"
    max_steps: int = 100
    session_id: Optional[str] = None

    # Episode
    episode_id: Optional[str] = None
    episode_active: bool = False
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
    _image_step: int = 1


# ---------------------------------------------------------------------------
# Engine (server-side logic, manages host + state + nav_tools)
# ---------------------------------------------------------------------------

class Engine:
    """
    Server-side GeoGuessr navigation engine.

    Manages StreetViewHostClient, ToolContext, nav_tools execution,
    image capture, and state tracking.
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
        self.state._image_step = self._ctx.meta.get(
            "image_step", self.state._image_step
        )

    def _apply_tool_result(self, result) -> None:
        updates = getattr(result, "updates", {}) or {}
        if "image_path" in updates:
            self.state.image_path = updates["image_path"]
        if "available_moves" in updates:
            self.state.available_moves = updates["available_moves"]
        if "heading" in updates:
            self.state.heading = updates["heading"]
        if "pitch" in updates:
            self.state.pitch = updates["pitch"]
        if "zoom" in updates:
            self.state.zoom = updates["zoom"]
        self._sync_image_step()

    def _tool_error(self, result, fallback: str) -> str:
        debug = getattr(result, "debug", None) or {}
        if isinstance(debug, dict):
            return debug.get("error") or debug.get("message") or fallback
        return fallback

    def _pull_host_state(self, capture: bool = False) -> None:
        if not self._host_enabled():
            return
        assert self._host_client is not None
        host_state = self._host_client.get_state(self.state.session_id)
        self._update_from_host_state(host_state, capture=capture)

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

    def _capture_image(self) -> Optional[str]:
        if not self.state.pano_id or not self.state.episode_id:
            return None
        try:
            state = self._build_host_state()
            path = capture_state_image(
                state=state,
                session_id=self.state.episode_id,
                root_dir=self.state.image_root,
                step=self.state._image_step,
            )
            self.state._image_step += 1
            self.state.image_path = path
            return path
        except Exception:
            return None

    def _update_from_host_state(self, state: Dict[str, Any], capture: bool = True) -> None:
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

        self._refresh_available_moves()
        if capture:
            self._capture_image()

    def _refresh_available_moves(self) -> None:
        try:
            state = self._build_host_state()
            moves_json = pure_nav.check_available_moves(json.dumps(state))
            moves_payload = json.loads(moves_json)
            updates = moves_payload.get("updates") or {}
            self.state.available_moves = updates.get("available_moves", [])
        except Exception:
            self.state.available_moves = []

    def _state_snapshot(self) -> Dict[str, Any]:
        return {
            "episode_id": self.state.episode_id,
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
        client = self._host_client or StreetViewHostClient()
        if not session_id:
            session_id = f"session_{int(time.time())}"
        client.start(session_id, api_key=api_key)
        self._set_host_context(client, session_id)
        return {"session_id": session_id}

    def load_scenario(self, scenario: Dict[str, Any]) -> Dict[str, Any]:
        for key, value in scenario.items():
            if hasattr(self.state, key):
                setattr(self.state, key, value)
        self._random = random.Random(self.state.random_seed)
        if not scenario.get("image_root"):
            self.state.image_root = os.getenv("IMAGE_OUTPUT_DIR", "images")
        if self._ctx is not None:
            self._ctx.meta["image_root"] = self.state.image_root
            self._ctx.meta["image_step"] = self.state._image_step
        return {"loaded": True}

    def init_panorama(self, lat: float, lng: float,
                      heading: float = 0.0, pitch: float = 0.0,
                      zoom: float = 1.0) -> Dict[str, Any]:
        if not self._host_enabled():
            raise RuntimeError("Host not connected")
        ctx = self._ensure_ctx()
        result = nav_tools.init_panorama(
            ctx, {"lat": lat, "lng": lng, "heading": heading, "pitch": pitch, "zoom": zoom},
        )
        if not result.ok:
            raise RuntimeError(self._tool_error(result, "init_failed"))
        self._apply_tool_result(result)
        self._pull_host_state(capture=False)
        return {
            "pano_id": self.state.pano_id,
            "lat": self.state.lat,
            "lng": self.state.lng,
            "heading": self.state.heading,
            "pitch": self.state.pitch,
            "zoom": self.state.zoom,
            "available_moves": self.state.available_moves,
            "image_path": self.state.image_path,
        }

    def start_episode(self) -> Dict[str, Any]:
        if self.state.episode_active:
            raise RuntimeError("Episode already active")
        episode_id = f"episode_{self._random.randint(100000, 999999)}"
        self.state.episode_id = episode_id
        self.state.episode_active = True
        self.state.step_count = 1
        self.state._image_step = 1
        self.state.image_path = None
        if self._host_enabled():
            self._pull_host_state(capture=True)
        return self._state_snapshot()

    def get_episode_state(self) -> Dict[str, Any]:
        if not self.state.episode_active:
            raise RuntimeError("No active episode")
        return self._state_snapshot()

    def move(self, direction: str) -> Dict[str, Any]:
        if not self.state.episode_active:
            raise RuntimeError("No active episode")
        if self.state.step_count >= self.state.max_steps:
            raise RuntimeError("Max steps reached")
        fn = self.MOVE_TOOLS.get(direction)
        if not fn:
            raise ValueError(f"Invalid direction: {direction}")
        ctx = self._ensure_ctx()
        result = fn(ctx, {})
        if not result.ok:
            raise RuntimeError(self._tool_error(result, "move_failed"))
        self.state.step_count += 1
        self._apply_tool_result(result)
        self._pull_host_state(capture=False)
        return {
            "image_path": self.state.image_path,
            "available_moves": self.state.available_moves,
        }

    def scroll(self, direction: str, delta: float) -> Dict[str, Any]:
        if not self.state.episode_active:
            raise RuntimeError("No active episode")
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
        self._pull_host_state(capture=False)
        if not self.state.image_path:
            raise RuntimeError("Image capture failed after scroll")
        return {"image_path": self.state.image_path}

    def zoom(self, direction: str, delta: float) -> Dict[str, Any]:
        if not self.state.episode_active:
            raise RuntimeError("No active episode")
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
        self._pull_host_state(capture=False)
        if not self.state.image_path:
            raise RuntimeError("Image capture failed after zoom")
        return {"image_path": self.state.image_path}

    def end_episode(self) -> Dict[str, Any]:
        if not self.state.episode_active:
            raise RuntimeError("No active episode")
        result_data = {
            "episode_id": self.state.episode_id,
            "step_count": self.state.step_count,
        }
        self.state.episode_id = None
        self.state.episode_active = False
        self.state.pano_id = None
        self.state.lat = None
        self.state.lng = None
        self.state.heading = 0.0
        self.state.pitch = 0.0
        self.state.zoom = 1.0
        self.state.links = []
        self.state.available_moves = []
        self.state.step_count = 0
        self.state.date = None
        self.state.image_path = None
        self.state._image_step = 1
        return result_data

    def check_direction(self) -> Dict[str, Any]:
        if not self.state.episode_active:
            raise RuntimeError("No active episode")
        ctx = self._ensure_ctx()
        result = nav_tools.check_direction(ctx, {})
        if not result.ok:
            raise RuntimeError(self._tool_error(result, "direction_failed"))
        updates = result.updates or {}
        self._apply_tool_result(result)
        return {"description": updates.get("description", "")}

    def check_available_moves(self) -> Dict[str, Any]:
        if not self.state.episode_active:
            raise RuntimeError("No active episode")
        ctx = self._ensure_ctx()
        result = nav_tools.check_available_moves(ctx, {})
        if not result.ok:
            raise RuntimeError(self._tool_error(result, "moves_failed"))
        self._apply_tool_result(result)
        return {"available_moves": self.state.available_moves}


# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------

app = Flask(__name__)
engines: Dict[str, Engine] = {}


def _ok(data: Dict[str, Any]):
    return jsonify({"ok": True, "updates": data, "error": {}})


def _err(message: str, status: int = 400):
    return jsonify({"ok": False, "updates": {}, "error": {"message": message}}), status


def _get_engine():
    """Look up the Engine for the current request's X-Session-ID header."""
    sid = request.headers.get("X-Session-ID")
    if not sid or sid not in engines:
        return None
    return engines[sid]


def _safe(fn, *args, **kwargs):
    """Execute engine method, return ok/error envelope."""
    try:
        data = fn(*args, **kwargs)
        return _ok(data)
    except Exception as e:
        return _err(str(e))


@app.route("/connect", methods=["POST"])
def route_connect():
    body = request.get_json(force=True, silent=True) or {}
    api_key = body.get("api_key") or os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        return _err("GOOGLE_MAPS_API_KEY not set")
    eng = Engine()
    try:
        data = eng.connect(api_key, body.get("session_id"))
    except Exception as e:
        return _err(str(e))
    sid = data["session_id"]
    engines[sid] = eng
    return _ok(data)


@app.route("/load_scenario", methods=["POST"])
def route_load_scenario():
    eng = _get_engine()
    if not eng:
        return _err("Unknown session — call /connect first")
    body = request.get_json(force=True, silent=True) or {}
    return _safe(eng.load_scenario, body)


@app.route("/init_panorama", methods=["POST"])
def route_init_panorama():
    eng = _get_engine()
    if not eng:
        return _err("Unknown session — call /connect first")
    body = request.get_json(force=True, silent=True) or {}
    return _safe(
        eng.init_panorama,
        lat=body.get("lat", 0.0),
        lng=body.get("lng", 0.0),
        heading=body.get("heading", 0.0),
        pitch=body.get("pitch", 0.0),
        zoom=body.get("zoom", 1.0),
    )


@app.route("/start_episode", methods=["POST"])
def route_start_episode():
    eng = _get_engine()
    if not eng:
        return _err("Unknown session — call /connect first")
    return _safe(eng.start_episode)


@app.route("/episode", methods=["GET"])
def route_episode_state():
    eng = _get_engine()
    if not eng:
        return _err("Unknown session — call /connect first")
    return _safe(eng.get_episode_state)


@app.route("/move/<direction>", methods=["POST"])
def route_move(direction):
    eng = _get_engine()
    if not eng:
        return _err("Unknown session — call /connect first")
    return _safe(eng.move, direction)


@app.route("/scroll/<direction>", methods=["POST"])
def route_scroll(direction):
    eng = _get_engine()
    if not eng:
        return _err("Unknown session — call /connect first")
    body = request.get_json(force=True, silent=True) or {}
    delta = body.get("delta", 0.0)
    return _safe(eng.scroll, direction, delta)


@app.route("/zoom/<direction>", methods=["POST"])
def route_zoom(direction):
    eng = _get_engine()
    if not eng:
        return _err("Unknown session — call /connect first")
    body = request.get_json(force=True, silent=True) or {}
    delta = body.get("delta", 0.0)
    return _safe(eng.zoom, direction, delta)


@app.route("/end_episode", methods=["POST"])
def route_end_episode():
    eng = _get_engine()
    if not eng:
        return _err("Unknown session — call /connect first")
    return _safe(eng.end_episode)


@app.route("/check/direction", methods=["GET"])
def route_check_direction():
    eng = _get_engine()
    if not eng:
        return _err("Unknown session — call /connect first")
    return _safe(eng.check_direction)


@app.route("/check/available_moves", methods=["GET"])
def route_check_available_moves():
    eng = _get_engine()
    if not eng:
        return _err("Unknown session — call /connect first")
    return _safe(eng.check_available_moves)


@app.route("/state", methods=["GET"])
def route_state():
    eng = _get_engine()
    if not eng:
        return _err("Unknown session — call /connect first")
    return _ok(eng._state_snapshot())


@app.route("/health", methods=["GET"])
def route_health():
    return _ok({"status": "ok", "active_sessions": len(engines)})


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    port = int(os.getenv("SERVER_PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
