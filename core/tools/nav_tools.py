from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

from core.tools.contracts import ToolContext, ToolResult
from core.navigation import pure_nav
from core.utils.image_pipeline import capture_state_image
from adapters.streetview_js.client import StreetViewHostClient
from core.exceptions import MissingContextError, InvalidArgumentError

def _get_client(ctx: ToolContext) -> StreetViewHostClient:
    client = ctx.meta.get("host_client")
    if not client:
        raise MissingContextError("missing_host_client")
    return client

def _get_state(ctx: ToolContext) -> Dict[str, Any]:
    client = _get_client(ctx)
    client.wait_for_stable(ctx.session_id)
    state = client.get_state(ctx.session_id)
    return state

def _execute_command(ctx: ToolContext, cmd: Dict[str, Any]) -> Dict[str, Any]:
    client = _get_client(ctx)
    method = cmd.get("method")
    params = cmd.get("params") or {}
    if method == "setPov":
        client.set_pov(
            ctx.session_id,
            heading=params.get("heading"),
            pitch=params.get("pitch"),
            zoom=params.get("zoom"),
        )

    elif method == "setPano":
        client.set_pano(ctx.session_id, params["panoId"])
    elif method == "setPosition":
        client.set_position(ctx.session_id, params["lat"], params["lng"])
    else:
        raise RuntimeError(f"unknown_command:{method}")

    client.wait_for_stable(ctx.session_id)
    new_state = client.get_state(ctx.session_id)
    return new_state

def _capture_image(ctx: ToolContext, state: Dict[str, Any]) -> Optional[str]:
    if ctx.meta.get("capture_images") is False:
        return None
    session_id = ctx.session_id or f"session_{int(time.time())}"
    image_root = ctx.meta.get("image_root") or os.getenv(
        "IMAGE_OUTPUT_DIR", "images"
    )
    step = ctx.meta.get("image_step", 1)
    path = capture_state_image(state, session_id, image_root, step=step)
    ctx.meta["image_step"] = step + 1
    return path


def _available_moves_from_state(state: Dict[str, Any]) -> list[str]:
    try:
        payload = json.loads(pure_nav.check_available_moves(json.dumps(state)))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    updates = payload.get("updates") or {}
    available_moves = updates.get("available_moves")
    if isinstance(available_moves, list):
        return available_moves
    return []


def _handle_pure_result(
    ctx: ToolContext,
    payload: Dict[str, Any],
    state: Dict[str, Any],
) -> ToolResult:
    result_type = payload.get("type")
    if result_type == "result":
        updates = payload.get("updates") or {}
        ok = updates.get("ok", True)
        if ok is False:
            return ToolResult(ok=False, debug=updates)
        updates["available_moves"] = _available_moves_from_state(state)
        return ToolResult(updates=updates)
    
    if result_type == "command":
        cmd = payload.get("command") or {}
        new_state = _execute_command(ctx, cmd)
        image_path = _capture_image(ctx, new_state)
        updates = {
            "image_path": image_path,
            "available_moves": _available_moves_from_state(new_state),
        }
        return ToolResult(updates=updates)

    return ToolResult(ok=False, debug={"error": "invalid_pure_result"})

def _run_pure(ctx: ToolContext, func, *args) -> ToolResult:
    state = _get_state(ctx)
    state_json = json.dumps(state)
    output_json = func(state_json, *args) if args else func(state_json)
    payload = json.loads(output_json)
    return _handle_pure_result(ctx, payload, state)


def init_panorama(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    client = _get_client(ctx)
    try:
        lat = float(args["lat"])
        lng = float(args["lng"])
    except (KeyError, TypeError, ValueError):
        return ToolResult(ok=False, debug={"error": "invalid_lat_lng"})

    heading = args.get("heading", 0.0)
    pitch = args.get("pitch", 0.0)
    zoom = args.get("zoom", 1.0)

    client.init(ctx.session_id, lat=lat, lng=lng, heading=heading, pitch=pitch, zoom=zoom)
    client.wait_for_stable(ctx.session_id)
    state = client.get_state(ctx.session_id)
    image_path = _capture_image(ctx, state)
    updates = {
        "image_path": image_path,
        "available_moves": _available_moves_from_state(state),
    }
    return ToolResult(updates=updates)

def check_direction(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return _run_pure(ctx, pure_nav.check_direction)


def check_available_moves(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return _run_pure(ctx, pure_nav.check_available_moves)


def move_north(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return _run_pure(ctx, pure_nav.move_north)


def move_northeast(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return _run_pure(ctx, pure_nav.move_northeast)


def move_east(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return _run_pure(ctx, pure_nav.move_east)


def move_southeast(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return _run_pure(ctx, pure_nav.move_southeast)


def move_south(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return _run_pure(ctx, pure_nav.move_south)


def move_southwest(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return _run_pure(ctx, pure_nav.move_southwest)


def move_west(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return _run_pure(ctx, pure_nav.move_west)


def move_northwest(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return _run_pure(ctx, pure_nav.move_northwest)


def scroll_left(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    delta = args.get("delta")
    if delta is None:
        return ToolResult(ok=False, debug={"error": "missing_delta", "arg": "delta"})
    return _run_pure(ctx, pure_nav.scroll_left, delta)


def scroll_right(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    delta = args.get("delta")
    if delta is None:
        return ToolResult(ok=False, debug={"error": "missing_delta", "arg": "delta"})
    return _run_pure(ctx, pure_nav.scroll_right, delta)


def scroll_up(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    delta = args.get("delta")
    if delta is None:
        return ToolResult(ok=False, debug={"error": "missing_delta", "arg": "delta"})
    return _run_pure(ctx, pure_nav.scroll_up, delta)


def scroll_down(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    delta = args.get("delta")
    if delta is None:
        return ToolResult(ok=False, debug={"error": "missing_delta", "arg": "delta"})
    return _run_pure(ctx, pure_nav.scroll_down, delta)


def zoom_in(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    delta = args.get("delta") if "delta" in args else args.get("amount")
    if delta is None:
        return ToolResult(ok=False, debug={"error": "missing_delta", "arg": "delta"})
    return _run_pure(ctx, pure_nav.zoom_in, delta)


def zoom_out(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    delta = args.get("delta") if "delta" in args else args.get("amount")
    if delta is None:
        return ToolResult(ok=False, debug={"error": "missing_delta", "arg": "delta"})
    return _run_pure(ctx, pure_nav.zoom_out, delta)
