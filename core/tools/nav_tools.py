from __future__ import annotations

import json
import os
import time
from typing import Any, Dict

from core.navigation import pure_nav
from core.tools.contracts import ToolContext, ToolResult
from core.utils.image_pipeline import capture_state_image_base64
from core.utils.local_image_utils import local_fetch_image
from adapters.streetview_js.client import StreetViewHostClient
from core.exceptions import MissingContextError


class NavTools:
    """Stateful navigation tool runner that owns a ToolContext."""

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    def get_client(self) -> StreetViewHostClient:
        client = self.ctx.meta.get("host_client")
        if not client:
            raise MissingContextError("missing_host_client")
        return client

    def get_state(self) -> Dict[str, Any]:
        client = self.get_client()
        client.wait_for_stable(self.ctx.session_id)
        return client.get_state(self.ctx.session_id)

    def apply_updates(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        client = self.get_client()
        changed = False

        next_pano_id = updates.get("next_pano_id")
        if next_pano_id:
            client.set_pano(self.ctx.session_id, next_pano_id)
            changed = True

        pov = {}
        if "new_heading" in updates:
            pov["heading"] = updates["new_heading"]
        if "new_pitch" in updates:
            pov["pitch"] = updates["new_pitch"]
        if "new_zoom" in updates:
            pov["zoom"] = updates["new_zoom"]

        if pov:
            client.set_pov(self.ctx.session_id, **pov)
            changed = True

        if changed:
            client.wait_for_stable(self.ctx.session_id)

        return client.get_state(self.ctx.session_id)

    def capture_image(self, state: Dict[str, Any]) -> tuple[str | None, str | None]:
        if self.ctx.meta.get("capture_images") is False:
            return None, None

        local_db = self.ctx.meta.get("local_db_path")
        if local_db:
            return self._capture_local(state, local_db)

        session_id = self.ctx.session_id or f"session_{int(time.time())}"
        image_root = self.ctx.meta.get("image_root") or os.getenv(
            "IMAGE_OUTPUT_DIR", "images"
        )
        step = self.ctx.meta.get("image_step", 1)
        image_base64, path = capture_state_image_base64(
            state, session_id, image_root, step=step,
            api_key=self.ctx.meta.get("api_key"),
            signing_secret=self.ctx.meta.get("url_signing_secret"),
        )
        self.ctx.meta["image_step"] = step + 1
        return image_base64, path

    def _capture_local(self, state: Dict[str, Any], db_path: str) -> tuple[str | None, str | None]:
        """Render a perspective view from a local equirectangular panorama."""
        import base64
        pano_id = state.get("panoId")
        if not pano_id:
            return None, None
        pov = state.get("pov") or {}
        image_root = self.ctx.meta.get("local_image_root", "crawl_images")
        img_bytes = local_fetch_image(
            pano_id=pano_id,
            heading=pov.get("heading", 0.0),
            pitch=pov.get("pitch", 0.0),
            zoom=pov.get("zoom", 1.0),
            db_path=db_path,
            image_root=image_root,
        )
        return base64.b64encode(img_bytes).decode("utf-8"), None

    def available_moves_from_state(self, state: Dict[str, Any]) -> list[str]:
        try:
            payload = json.loads(pure_nav.check_available_moves(json.dumps(state)))
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        updates = payload.get("updates") or {}
        available_moves = updates.get("available_moves")
        if isinstance(available_moves, list):
            return available_moves
        return []

    def handle_pure_result(self, payload: Dict[str, Any], state: Dict[str, Any]) -> ToolResult:
        if payload.get("type") != "result":
            return ToolResult(ok=False, debug={"error": "invalid_pure_result"})

        updates = payload.get("updates") or {}
        if updates.get("ok") is False:
            return ToolResult(ok=False, debug=updates)

        if any(k in updates for k in ("next_pano_id", "new_heading", "new_pitch", "new_zoom")):
            new_state = self.apply_updates(updates)
            return ToolResult(
                updates={"available_moves": self.available_moves_from_state(new_state)}
            )

        updates["available_moves"] = self.available_moves_from_state(state)
        return ToolResult(updates=updates)

    def run_pure(self, func, *args) -> ToolResult:
        state = self.get_state()
        state_json = json.dumps(state)
        output_json = func(state_json, *args) if args else func(state_json)
        payload = json.loads(output_json)
        return self.handle_pure_result(payload, state)

    # --- public tools ---

    def init_panorama(self, args: Dict[str, Any]) -> ToolResult:
        client = self.get_client()
        try:
            lat = float(args["lat"])
            lng = float(args["lng"])
        except (KeyError, TypeError, ValueError):
            return ToolResult(ok=False, debug={"error": "invalid_lat_lng"})

        heading = args.get("heading", 0.0)
        pitch = args.get("pitch", 0.0)
        zoom = args.get("zoom", 1.0)

        client.init(
            self.ctx.session_id,
            lat=lat,
            lng=lng,
            heading=heading,
            pitch=pitch,
            zoom=zoom,
        )
        client.wait_for_stable(self.ctx.session_id)
        state = client.get_state(self.ctx.session_id)
        image_base64, image_path = self.capture_image(state)
        updates = {
            "image_path": image_path,
            "image_base64": image_base64,
            "available_moves": self.available_moves_from_state(state),
        }
        return ToolResult(updates=updates)

    def check_direction(self, args: Dict[str, Any]) -> ToolResult:
        return self.run_pure(pure_nav.check_direction)

    def check_available_moves(self, args: Dict[str, Any]) -> ToolResult:
        return self.run_pure(pure_nav.check_available_moves)

    def capture_view(self, args: Dict[str, Any]) -> ToolResult:
        state = self.get_state()
        image_base64, image_path = self.capture_image(state)
        updates = {
            "image_base64": image_base64,
            "available_moves": self.available_moves_from_state(state),
        }
        return ToolResult(updates=updates)

    def move_north(self, args: Dict[str, Any]) -> ToolResult:
        return self.run_pure(pure_nav.move_north)

    def move_northeast(self, args: Dict[str, Any]) -> ToolResult:
        return self.run_pure(pure_nav.move_northeast)

    def move_east(self, args: Dict[str, Any]) -> ToolResult:
        return self.run_pure(pure_nav.move_east)

    def move_southeast(self, args: Dict[str, Any]) -> ToolResult:
        return self.run_pure(pure_nav.move_southeast)

    def move_south(self, args: Dict[str, Any]) -> ToolResult:
        return self.run_pure(pure_nav.move_south)

    def move_southwest(self, args: Dict[str, Any]) -> ToolResult:
        return self.run_pure(pure_nav.move_southwest)

    def move_west(self, args: Dict[str, Any]) -> ToolResult:
        return self.run_pure(pure_nav.move_west)

    def move_northwest(self, args: Dict[str, Any]) -> ToolResult:
        return self.run_pure(pure_nav.move_northwest)

    def scroll_left(self, args: Dict[str, Any]) -> ToolResult:
        delta = args.get("delta")
        if delta is None:
            return ToolResult(ok=False, debug={"error": "missing_delta", "arg": "delta"})
        return self.run_pure(pure_nav.scroll_left, delta)

    def scroll_right(self, args: Dict[str, Any]) -> ToolResult:
        delta = args.get("delta")
        if delta is None:
            return ToolResult(ok=False, debug={"error": "missing_delta", "arg": "delta"})
        return self.run_pure(pure_nav.scroll_right, delta)

    def scroll_up(self, args: Dict[str, Any]) -> ToolResult:
        delta = args.get("delta")
        if delta is None:
            return ToolResult(ok=False, debug={"error": "missing_delta", "arg": "delta"})
        return self.run_pure(pure_nav.scroll_up, delta)

    def scroll_down(self, args: Dict[str, Any]) -> ToolResult:
        delta = args.get("delta")
        if delta is None:
            return ToolResult(ok=False, debug={"error": "missing_delta", "arg": "delta"})
        return self.run_pure(pure_nav.scroll_down, delta)

    def zoom_in(self, args: Dict[str, Any]) -> ToolResult:
        delta = args.get("delta") if "delta" in args else args.get("amount")
        if delta is None:
            return ToolResult(ok=False, debug={"error": "missing_delta", "arg": "delta"})
        return self.run_pure(pure_nav.zoom_in, delta)

    def zoom_out(self, args: Dict[str, Any]) -> ToolResult:
        delta = args.get("delta") if "delta" in args else args.get("amount")
        if delta is None:
            return ToolResult(ok=False, debug={"error": "missing_delta", "arg": "delta"})
        return self.run_pure(pure_nav.zoom_out, delta)


# ---- compatibility wrappers (so server/runner still work) ----

def init_panorama(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return NavTools(ctx).init_panorama(args)


def check_direction(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return NavTools(ctx).check_direction(args)


def check_available_moves(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return NavTools(ctx).check_available_moves(args)


def capture_view(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return NavTools(ctx).capture_view(args)


def move_north(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return NavTools(ctx).move_north(args)


def move_northeast(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return NavTools(ctx).move_northeast(args)


def move_east(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return NavTools(ctx).move_east(args)


def move_southeast(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return NavTools(ctx).move_southeast(args)


def move_south(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return NavTools(ctx).move_south(args)


def move_southwest(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return NavTools(ctx).move_southwest(args)


def move_west(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return NavTools(ctx).move_west(args)


def move_northwest(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return NavTools(ctx).move_northwest(args)


def scroll_left(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return NavTools(ctx).scroll_left(args)


def scroll_right(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return NavTools(ctx).scroll_right(args)


def scroll_up(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return NavTools(ctx).scroll_up(args)


def scroll_down(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return NavTools(ctx).scroll_down(args)


def zoom_in(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return NavTools(ctx).zoom_in(args)


def zoom_out(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return NavTools(ctx).zoom_out(args)
