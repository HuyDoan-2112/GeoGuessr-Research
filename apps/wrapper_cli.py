"""CLI to exercise GeoGuessrAPI against the server."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
from typing import Any, Dict, Tuple, List

from apps.geoguessr_wrapper import GeoGuessrAPI


ALIASES = {
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
}


def _parse_float(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _parse_value(text: str) -> Any:
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    num = _parse_float(text)
    if num is not None:
        return num
    return text


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _parse_command(raw: str) -> Tuple[str, List[str]]:
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
    return tokens[0].lower(), tokens[1:]


def _print_help() -> None:
    print("Commands:")
    print("  init <lat> <lng> [heading] [pitch] [zoom]")
    print("  move north|south|east|west|ne|nw|se|sw")
    print("  scroll left|right|up|down <delta>")
    print("  zoom in|out <delta>")
    print("  check direction|available_moves")
    print("  load key=value [key=value ...]")
    print("  state")
    print("  server_state")
    print("  end")
    print("  help")
    print("  exit")


def main() -> None:
    parser = argparse.ArgumentParser(description="GeoGuessr wrapper CLI")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--lat", type=float)
    parser.add_argument("--lng", type=float)
    parser.add_argument("--heading", type=float, default=0.0)
    parser.add_argument("--pitch", type=float, default=0.0)
    parser.add_argument("--zoom", type=float, default=1.0)
    parser.add_argument("--no-init", action="store_true")
    args = parser.parse_args()

    api = GeoGuessrAPI(base_url=args.base_url)
    api.connect_host(api_key=args.api_key, session_id=args.session_id)

    if not args.no_init:
        lat = args.lat
        lng = args.lng
        if lat is None or lng is None:
            raw = input("Enter initial coords as 'lat lng': ").strip()
            if raw:
                parts = raw.split()
                if len(parts) >= 2:
                    lat = _parse_float(parts[0])
                    lng = _parse_float(parts[1])
        if lat is None or lng is None:
            lat, lng = 37.7749, -122.4194
        api.init_panorama(lat, lng, args.heading, args.pitch, args.zoom)

    print("Ready. Type 'help' for commands.")
    while True:
        raw = input("cmd> ")
        cmd, rest = _parse_command(raw)
        if not cmd:
            continue
        if cmd in {"exit", "quit"}:
            break
        if cmd in {"help", "?"}:
            _print_help()
            continue

        if cmd == "state":
            _print_json(api.get_state())
            continue
        if cmd == "server_state":
            _print_json(api._call("GET", "/state"))
            continue
        if cmd == "load":
            scenario: Dict[str, Any] = {}
            for item in rest:
                if "=" not in item:
                    print(f"Invalid item: {item}")
                    continue
                key, val = item.split("=", 1)
                scenario[key] = _parse_value(val)
            _print_json(api._load_scenario(scenario))
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
            _print_json(api.init_panorama(lat, lng, heading, pitch, zoom))
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
        if cmd == "end":
            _print_json(api.end_session())
            continue

        tool_name = ALIASES.get(cmd)
        if not tool_name:
            print("Unknown command. Type 'help' to list commands.")
            continue

        if tool_name.startswith("scroll_") or tool_name.startswith("zoom_"):
            if not rest:
                print("Missing delta.")
                continue
            delta = _parse_float(rest[0])
            if delta is None:
                print("Invalid delta.")
                continue
            _print_json(getattr(api, tool_name)(delta))
            continue

        if tool_name == "check_direction":
            _print_json({"description": api.check_direction()})
            continue
        if tool_name == "check_available_moves":
            _print_json({"available_moves": api.check_available_moves()})
            continue

        _print_json(getattr(api, tool_name)())


if __name__ == "__main__":
    main()
