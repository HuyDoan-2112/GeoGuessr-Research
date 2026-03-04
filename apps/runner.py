"""Runner entrypoint (interactive pipeline)."""

import argparse
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from adapters.streetview_js.client import StreetViewHostClient
from core.tools.contracts import ToolContext, ToolResult
from core.tools import nav_tools


ALIASES = {
    "init": "init_panorama",
    "north": "move_north",
    "south": "move_south",
    "east": "move_east",
    "west": "move_west",
    "ne": "move_northeast",
    "nw": "move_northwest",
    "se": "move_southeast",
    "sw": "move_southwest",
    "move_north": "move_north",
    "move_south": "move_south",
    "move_east": "move_east",
    "move_west": "move_west",
    "move_northeast": "move_northeast",
    "move_northwest": "move_northwest",
    "move_southeast": "move_southeast",
    "move_southwest": "move_southwest",
    "scroll_left": "scroll_left",
    "scroll_right": "scroll_right",
    "scroll_up": "scroll_up",
    "scroll_down": "scroll_down",
    "zoom_in": "zoom_in",
    "zoom_out": "zoom_out",
    "check_direction": "check_direction",
    "check_available_moves": "check_available_moves",
    "capture_view": "capture_view",
}


def _parse_float(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _print_json(payload) -> None:
    print(json.dumps(payload, indent=2))


def _prompt_float(label: str, default: float | None = None) -> float:
    while True:
        suffix = f" [{default}]" if default is not None else ""
        raw = input(f"{label}{suffix}: ").strip()
        if not raw and default is not None:
            return default
        value = _parse_float(raw)
        if value is not None:
            return value
        print("Invalid number. Try again.")


def _parse_command(raw: str) -> tuple[str, list[str]]:
    raw = raw.strip()
    if not raw:
        return "", []
    func_match = re.match(r"^([a-zA-Z_]+)\(([^)]*)\)$", raw)
    if func_match:
        cmd = func_match.group(1).lower()
        arg = func_match.group(2).strip()
        return cmd, [arg] if arg else []
    tokens = shlex.split(raw)
    if not tokens:
        return "", []
    cmd = tokens[0].lower()
    return cmd, tokens[1:]


def _run_tool(ctx: ToolContext, name: str, args: dict) -> ToolResult:
    tool_fn = getattr(nav_tools, name)
    result = tool_fn(ctx, args)
    payload = {
        "ok": result.ok,
        "updates": result.updates,
        "debug": result.debug,
    }
    _print_json(payload)
    return result


def _print_help() -> None:
    print("Commands:")
    print("  init <lat> <lng> [heading] [pitch] [zoom]")
    print("  move north|south|east|west|ne|nw|se|sw")
    print("  scroll left|right|up|down <delta>")
    print("  zoom in|out <delta>")
    print("  check direction")
    print("  check available_moves")
    print("  capture view")
    print("  state")
    print("  help")
    print("  exit")


def main() -> None:
    load_dotenv(ROOT / ".env")
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_MAPS_API_KEY not set")

    parser = argparse.ArgumentParser(description="Interactive pipeline runner")
    parser.add_argument("--lat", type=float)
    parser.add_argument("--lng", type=float)
    parser.add_argument("--question", type=str)
    parser.add_argument("--heading", type=float, default=90.0)
    parser.add_argument("--pitch", type=float, default=0.0)
    parser.add_argument("--zoom", type=float, default=1.0)
    args = parser.parse_args()

    lat = args.lat
    lng = args.lng
    if lat is None or lng is None:
        print("Enter initial coordinates:")
        lat = _prompt_float("lat", 37.7749)
        lng = _prompt_float("lng", -122.4194)

    client = StreetViewHostClient()
    try:
        session_id = f"session_{int(time.time())}"

        client.start(session_id, api_key=api_key)
        ctx = ToolContext(
            session_id=session_id,
            meta={
                "host_client": client,
                "image_root": os.getenv("IMAGE_OUTPUT_DIR", "images"),
                "image_step": 1,
                "api_key": api_key,
                "url_signing_secret": os.getenv("GOOGLE_MAPS_URL_SIGNING_SECRET"),
            },
        )
        _run_tool(
            ctx,
            "init_panorama",
            {
                "lat": lat,
                "lng": lng,
                "heading": args.heading,
                "pitch": args.pitch,
                "zoom": args.zoom,
            },
        )

        print("Ready. Type 'help' for commands.")
        while True:
            try:
                raw = input("cmd> ")
            except EOFError:
                print()
                break

            cmd, rest = _parse_command(raw)
            if not cmd:
                continue
            if cmd in ("exit", "quit"):
                break
            if cmd in ("help", "?"):
                _print_help()
                continue
            if cmd == "state":
                _print_json(client.get_state(session_id))
                continue
            if cmd == "init":
                if len(rest) < 2:
                    print("Usage: init <lat> <lng> [heading] [pitch] [zoom]")
                    continue
                lat = _parse_float(rest[0])
                lng = _parse_float(rest[1])
                heading = _parse_float(rest[2]) if len(rest) > 2 else 0.0
                pitch = _parse_float(rest[3]) if len(rest) > 3 else 0.0
                zoom = _parse_float(rest[4]) if len(rest) > 4 else 1.0
                if lat is None or lng is None:
                    print("Invalid lat/lng.")
                    continue
                _run_tool(
                    ctx,
                    "init_panorama",
                    {
                        "lat": lat,
                        "lng": lng,
                        "heading": heading,
                        "pitch": pitch,
                        "zoom": zoom,
                    },
                )
                continue
            if cmd == "move" and rest:
                cmd = rest[0].lower()
                rest = []

            if cmd == "scroll" and rest:
                cmd = f"scroll_{rest[0].lower()}"
                rest = rest[1:]

            if cmd == "zoom" and rest:
                cmd = f"zoom_{rest[0].lower()}"
                rest = rest[1:]

            if cmd == "check" and rest:
                cmd = f"check_{rest[0].lower()}"
                rest = rest[1:]
            if cmd == "capture" and rest:
                cmd = f"capture_{rest[0].lower()}"
                rest = rest[1:]

            tool_name = ALIASES.get(cmd)
            if not tool_name:
                print("Unknown command. Type 'help' to list commands.")
                continue

            if tool_name.startswith("scroll_"):
                if not rest:
                    print("Missing delta.")
                    continue
                delta = _parse_float(rest[0])
                if delta is None:
                    print("Invalid delta.")
                    continue
                _run_tool(ctx, tool_name, {"delta": delta})
                continue

            if tool_name.startswith("zoom_"):
                if not rest:
                    print("Missing delta.")
                    continue
                delta = _parse_float(rest[0])
                if delta is None:
                    print("Invalid delta.")
                    continue
                _run_tool(ctx, tool_name, {"delta": delta})
                continue

            _run_tool(ctx, tool_name, {})
    finally:
        client.close()


if __name__ == "__main__":
    main()
