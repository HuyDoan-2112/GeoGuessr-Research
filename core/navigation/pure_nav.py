from __future__ import annotations

import json 
import math
from typing import Any, Dict, List, Optional
from core.navigation.protocol import parse_state, build_command, dump_command


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

def _normalize_heading(heading: float) -> float:
    if heading is None:
        return 0.0
    
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

def _in_cone(h: float, cones: List[tuple[float, float]]) -> bool:
    h = _normalize_heading(h)
    for lo, hi in cones:
        if lo <= hi and lo <= h < hi:
            return True
        if lo > hi and (h >= lo or h < hi):
            return True
    return False

def _current_heading(state: Dict[str, Any]) -> float:
    return float(state["pov"]["heading"])

def _current_pitch(state: Dict[str, Any]) -> float:
    return float(state["pov"]["pitch"])

def _current_zoom(state: Dict[str, Any]) -> float:
    return float(state["pov"]["zoom"])

def _links(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(state.get("links") or [])

def _result(updates: Dict[str, Any]) -> str:
    return json.dumps({"type": "result", "updates": updates})

def _command(method: str, params: Dict[str, Any]) -> str:
    cmd = build_command(method, params)
    # @HuanzhiMao FIXME: do we need to dump and load the command? can we just return the command directly?
    return json.dumps({"type": "command", "command": cmd}) 

def check_direction(state_json: str) -> str:
    state = parse_state(state_json)
    heading =  _current_heading(state)
    direction = _heading_to_direction(heading)

    return _result(
        {
            "heading": heading,
            "direction": direction,
            "description": f"Facing {direction} ({heading:.1f} degrees)"
        }
    )

def check_available_moves(state_json: str) -> str:
    state = parse_state(state_json)
    current_heading = _current_heading(state)
    move_actions: List[str] = []
    _DIR_TO_FULL = {
        "N": "north", "NE": "northeast", "E": "east", "SE": "southeast",
        "S": "south", "SW": "southwest", "W": "west", "NW": "northwest",
    }
    for link in _links(state):
        move_heading = float(link["heading"])
        direction = _heading_to_direction(move_heading)
        move_actions.append(f"move_{_DIR_TO_FULL[direction]}")
    
    universal_actions = [
        "capture_view",
        "scroll_up",
        "scroll_left",
        "scroll_right",
        "scroll_down",
        "zoom_in",
        "zoom_out",
    ]
    return _result(
        {
            "available_moves": universal_actions + move_actions,
        }
    )

def _move_and_command(state_json: str, direction_key: str) -> str:
    state = parse_state(state_json)
    candidates = [
        link for link in _links(state)
        if _in_cone(link["heading"], DIR_CONES[direction_key])
    ]
    if not candidates:
        return _result({"ok": False, "error": f"no moves in {direction_key} cone"})
    target = candidates[0]
    return _command("setPano", {"panoId": target["panoId"]})


def move_north(state_json: str) -> str:
    return _move_and_command(state_json, "N")
def move_northeast(state_json: str) -> str:
    return _move_and_command(state_json, "NE")
def move_east(state_json: str) -> str:
    return _move_and_command(state_json, "E")
def move_southeast(state_json: str) -> str:
    return _move_and_command(state_json, "SE")
def move_south(state_json: str) -> str:
    return _move_and_command(state_json, "S")
def move_southwest(state_json: str) -> str:
    return _move_and_command(state_json, "SW")
def move_west(state_json: str) -> str:
    return _move_and_command(state_json, "W")
def move_northwest(state_json: str) -> str:
    return _move_and_command(state_json, "NW")

def scroll_left(state_json: str, delta_deg: float) -> str:
    if delta_deg is None or not math.isfinite(float(delta_deg)):
        return _result({"ok": False, "error": "missing_or_invalid_delta"})
    state = parse_state(state_json)
    step = abs(float(delta_deg))
    current = _current_heading(state)
    new_heading = _normalize_heading(current - step)
    return _command("setPov", {"heading": new_heading})

def scroll_right(state_json: str, delta_deg: float) -> str:
    if delta_deg is None or not math.isfinite(float(delta_deg)):
        return _result({"ok": False, "error": "missing_or_invalid_delta"})
    state = parse_state(state_json)
    step = abs(float(delta_deg))
    current = _current_heading(state)
    new_heading = _normalize_heading(current + step)
    return _command("setPov", {"heading": new_heading})

def scroll_up(state_json: str, delta_deg: float) -> str:
    if delta_deg is None or not math.isfinite(float(delta_deg)):
        return _result({"ok": False, "error": "missing_or_invalid_delta"})
    state = parse_state(state_json)
    step = abs(float(delta_deg))
    current = _current_pitch(state)
    new_pitch = min(current + step, 90.0)
    return _command("setPov", {"pitch": new_pitch})

def scroll_down(state_json: str, delta_deg: float) -> str:
    if delta_deg is None or not math.isfinite(float(delta_deg)):
        return _result({"ok": False, "error": "missing_or_invalid_delta"})
    state = parse_state(state_json)
    step = abs(float(delta_deg))
    current = _current_pitch(state)
    new_pitch = max(current - step, -90.0)
    return _command("setPov", {"pitch": new_pitch})

def zoom_in(state_json: str, delta: float) -> str:
    if delta is None or not math.isfinite(float(delta)):
        return _result({"ok": False, "error": "missing_or_invalid_delta"})
    state = parse_state(state_json)
    step = abs(float(delta))
    current = _current_zoom(state)
    new_zoom = current + step
    return _command("setPov", {"zoom": new_zoom})

def zoom_out(state_json: str, delta: float) -> str:
    if delta is None or not math.isfinite(float(delta)):
        return _result({"ok": False, "error": "missing_or_invalid_delta"})
    state = parse_state(state_json)
    step = abs(float(delta))
    current = _current_zoom(state)
    new_zoom = max(current - step, 0.0)
    return _command("setPov", {"zoom": new_zoom})


