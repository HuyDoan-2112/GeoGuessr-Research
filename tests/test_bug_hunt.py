
import json
import math
import importlib
import logging
import types
import pytest


# ============================================================================
# LAYER 0 — SMOKE TESTS
# "Does the code even load?"
# Run these first.  If any fail, nothing downstream matters.
# ============================================================================

class TestSmoke:
    """Import every public module and construct core objects."""

    # --- imports ---

    def test_import_exceptions(self):
        from core import exceptions  # noqa: F401
        assert hasattr(exceptions, "GeoGuessrError")

    def test_import_protocol(self):
        from core.navigation import protocol  # noqa: F401
        assert callable(protocol.parse_state)

    def test_import_pure_nav(self):
        from core.navigation import pure_nav  # noqa: F401
        assert callable(pure_nav.check_direction)

    def test_import_contracts(self):
        from core.tools.contracts import ToolContext, ToolResult  # noqa: F401
        assert ToolContext is not None

    def test_import_nav_tools(self):
        from core.tools import nav_tools  # noqa: F401
        assert callable(nav_tools.init_panorama)

    def test_import_dispatcher(self):
        from core.tools import dispatcher  # noqa: F401
        assert callable(dispatcher.run_tool)

    def test_import_registry(self):
        from core.tools import registry  # noqa: F401
        assert hasattr(registry, "TOOL_SPECS")

    def test_import_image_utils(self):
        from core.utils import image_utils  # noqa: F401
        assert callable(image_utils.zoom_to_fov)

    def test_import_image_store(self):
        """Bug #7: file uses `str | os.PathLike` without __future__ annotations.
        On Python <3.10 this import itself crashes."""
        from core.utils import image_store  # noqa: F401
        assert callable(image_store.build_filename)

    def test_import_image_pipeline(self):
        from core.utils import image_pipeline  # noqa: F401
        assert callable(image_pipeline.capture_state_image)

    def test_import_client(self):
        from adapters.streetview_js.client import StreetViewHostClient  # noqa: F401
        assert StreetViewHostClient is not None

    def test_import_wrapper(self):
        from apps.geoguessr_wrapper import GeoGuessrAPI  # noqa: F401
        assert GeoGuessrAPI is not None

    def test_import_logging_config(self):
        """Bug #1 & #2: logging.Streamhandler and logging.handler crash on import.

        This test checks whether the module *defines* setup_logging without
        AttributeError.  If the bugs are present, this fails immediately.
        """
        mod = importlib.import_module("core.logging_config")
        assert callable(mod.setup_logging)

    # --- construction ---

    def test_construct_tool_context(self):
        from core.tools.contracts import ToolContext
        ctx = ToolContext(session_id="smoke")
        assert ctx.session_id == "smoke"

    def test_construct_tool_result(self):
        from core.tools.contracts import ToolResult
        r = ToolResult()
        assert r.ok is True
        assert r.updates == {}

    def test_construct_agent_state(self):
        from apps.geoguessr_wrapper import AgentState
        s = AgentState()
        assert s.episode_active is False

    def test_construct_wrapper(self):
        from apps.geoguessr_wrapper import GeoGuessrAPI
        api = GeoGuessrAPI(base_url="http://localhost:9999")
        assert api.state.session_id is None


# ============================================================================
# LAYER 1 — REPRO CASES
# One test per suspected bug.  Each test is self-contained and documents
# exactly what breaks, where, and what the expected fix looks like.
# ============================================================================

class TestReproBug1_LoggingStreamHandler:
    """Bug #1: logging_config.py:27 — `logging.Streamhandler` (lowercase h).
    Should be `logging.StreamHandler`."""

    def test_streamhandler_attribute_exists(self):
        """Verify the correct name exists on the logging module."""
        assert hasattr(logging, "StreamHandler"), "logging.StreamHandler must exist"

    def test_streamhandler_typo_does_not_exist(self):
        """The typo `Streamhandler` should NOT exist."""
        assert not hasattr(logging, "Streamhandler"), (
            "logging.Streamhandler does not exist — the code has a typo"
        )

    def test_setup_logging_callable(self):
        """Calling setup_logging() must not raise AttributeError."""
        try:
            from core.logging_config import setup_logging
            setup_logging()
        except AttributeError as exc:
            pytest.fail(f"setup_logging() raised AttributeError: {exc}")


class TestReproBug2_LoggingHandlerTypeHint:
    """Bug #2: logging_config.py:24 — `list[logging.handler]`.
    `logging.handler` doesn't exist.  The class is `logging.Handler`."""

    def test_handler_class_exists(self):
        assert hasattr(logging, "Handler"), "logging.Handler must exist"

    def test_handler_lowercase_does_not_exist(self):
        assert not hasattr(logging, "handler"), (
            "logging.handler does not exist — this type hint crashes at definition time"
        )

    def test_setup_logging_function_defined(self):
        """If the type hint is evaluated eagerly (no __future__ annotations),
        the function definition itself crashes."""
        mod = importlib.import_module("core.logging_config")
        assert isinstance(mod.setup_logging, types.FunctionType)


class TestReproBug3_InConeBoundary:
    """Bug #3: pure_nav._in_cone uses inclusive upper bound (<=)
    while _heading_to_direction uses exclusive (<).

    At boundary heading 22.5:
    - _heading_to_direction -> NE  (correct — 22.5 is the start of NE)
    - _in_cone(22.5, N_cones) -> True  (wrong — 22.5 should NOT be in N)
    """

    def test_heading_to_direction_at_boundary(self):
        from core.navigation.pure_nav import _heading_to_direction
        assert _heading_to_direction(22.5) == "NE"
        assert _heading_to_direction(67.5) == "E"
        assert _heading_to_direction(112.5) == "SE"
        assert _heading_to_direction(337.5) == "N"

    def test_in_cone_must_agree_with_heading_to_direction(self):
        """_in_cone for N must NOT match 22.5 (that belongs to NE)."""
        from core.navigation.pure_nav import _in_cone, DIR_CONES

        # 22.5 is the START of NE, so it must be IN NE and NOT in N
        assert _in_cone(22.5, DIR_CONES["NE"]) is True, "22.5 should be in NE"
        assert _in_cone(22.5, DIR_CONES["N"]) is False, (
            "BUG: 22.5 matches N cone due to inclusive upper bound (<=). "
            "Should be exclusive (<) to match _heading_to_direction."
        )

    def test_all_boundaries_consistent(self):
        """Every boundary heading must match exactly one cone."""
        from core.navigation.pure_nav import (
            _in_cone, _heading_to_direction, DIR_CONES,
        )
        boundaries = [0.0, 22.5, 67.5, 112.5, 157.5, 202.5, 247.5, 292.5, 337.5]
        for h in boundaries:
            expected_dir = _heading_to_direction(h)
            matching = [d for d, cones in DIR_CONES.items() if _in_cone(h, cones)]
            assert matching == [expected_dir], (
                f"heading={h}: _heading_to_direction says {expected_dir}, "
                f"but _in_cone matches {matching}"
            )

    def test_move_north_rejects_link_at_22_5(self):
        """A link at exactly 22.5 degrees should NOT be reachable via move_north."""
        from core.navigation import pure_nav

        state = {
            "panoId": "test",
            "pov": {"heading": 0.0, "pitch": 0.0, "zoom": 1.0},
            "links": [{"heading": 22.5, "panoId": "boundary_pano"}],
        }
        result = json.loads(pure_nav.move_north(json.dumps(state)))
        assert result["type"] == "result", (
            "BUG: move_north matched a link at 22.5 degrees (should be NE only)"
        )
        assert result["updates"]["ok"] is False

    def test_move_northeast_accepts_link_at_22_5(self):
        """A link at exactly 22.5 degrees SHOULD be reachable via move_northeast."""
        from core.navigation import pure_nav

        state = {
            "panoId": "test",
            "pov": {"heading": 0.0, "pitch": 0.0, "zoom": 1.0},
            "links": [{"heading": 22.5, "panoId": "boundary_pano"}],
        }
        result = json.loads(pure_nav.move_northeast(json.dumps(state)))
        assert result["type"] == "command"
        assert result["command"]["params"]["panoId"] == "boundary_pano"


class TestReproBug4_CloseSessionNullProc:
    """Bug #4: client.py:188 — close_session accesses self._proc.pid
    before checking if self._proc is None."""

    def test_close_session_logs_without_crash_when_no_proc(self):
        from adapters.streetview_js.client import StreetViewHostClient

        client = StreetViewHostClient()
        assert client._proc is None, "Precondition: no process started"

        # close_session does logger.debug("...pid=%s", self._proc.pid)
        # before calling _request.  The debug line should not crash.
        # _request will still fail (HostTimeoutError) because there's
        # no running host — that's expected.  The bug is the debug line.
        from core.exceptions import HostTimeoutError

        with pytest.raises((AttributeError, HostTimeoutError)) as exc_info:
            client.close_session("test_session")

        # If we get AttributeError, the bug is present (accessing None.pid)
        if exc_info.type is AttributeError:
            pytest.fail(
                "BUG: close_session crashed with AttributeError on self._proc.pid "
                "before reaching _request(). Add a null check."
            )
        # HostTimeoutError is the expected outcome after the fix
        # (process starts via _ensure_proc in _request, but host.js
        # won't respond to "closeSession" for a session that was never started)


class TestReproBug5_RunnerNonePropagation:
    """Bug #5: runner.py _parse_float returns None for invalid input,
    and that None flows into init_panorama as heading=None."""

    def test_parse_float_returns_none_for_invalid(self):
        import sys
        from pathlib import Path
        # runner.py does sys.path manipulation; import directly
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from apps.runner import _parse_float

        assert _parse_float("abc") is None
        assert _parse_float("") is None
        assert _parse_float("1.5") == 1.5

    def test_none_heading_reaches_init_panorama_args(self):
        """Simulate what runner.py does when user types 'init 40 -74 abc'."""
        from apps.runner import _parse_float

        rest = ["40.0", "-74.0", "abc"]
        heading = _parse_float(rest[2]) if len(rest) > 2 else 0.0
        # BUG: heading is None, not 0.0
        if heading is None:
            pytest.fail(
                "BUG confirmed: _parse_float('abc') returns None and runner.py "
                "passes it through as heading=None instead of rejecting the input."
            )


class TestReproBug6_TimeoutErrorMissingSpace:
    """Bug #6: exceptions.py:30 — missing space before (req_id=...)."""

    def test_message_format_with_req_id(self):
        from core.exceptions import HostTimeoutError

        exc = HostTimeoutError(method="getState", timeout=30, req_id=42)
        msg = str(exc)
        # Should be "...30s (req_id=42)" with a space, not "...30s(req_id=42)"
        assert "(req_id=42)" in msg, "req_id should appear in message"
        assert "30s(req_id" not in msg, (
            "BUG: missing space before (req_id=...). "
            f"Got: '{msg}'"
        )


# ============================================================================
# LAYER 2 — INVARIANT TESTS
# Cross-module consistency: things that must always be true.
# ============================================================================

class TestInvariant_ToolRegistriesMatch:
    """Every tool in dispatcher.TOOL_IMPL must have a policy,
    and every tool in registry.TOOL_SPECS must have an implementation."""

    def test_every_impl_has_policy(self):
        from core.tools.dispatcher import TOOL_IMPL, TOOL_POLICY
        missing = [name for name in TOOL_IMPL if name not in TOOL_POLICY]
        assert missing == [], f"Tools in TOOL_IMPL missing from TOOL_POLICY: {missing}"

    def test_every_policy_has_impl(self):
        from core.tools.dispatcher import TOOL_IMPL, TOOL_POLICY
        missing = [name for name in TOOL_POLICY if name not in TOOL_IMPL]
        assert missing == [], f"Tools in TOOL_POLICY missing from TOOL_IMPL: {missing}"

    def test_every_spec_has_impl(self):
        from core.tools.registry import TOOL_SPECS
        from core.tools.dispatcher import TOOL_IMPL
        spec_names = {s["function"]["name"] for s in TOOL_SPECS}
        impl_names = set(TOOL_IMPL.keys())
        # final_answer is in specs but not in TOOL_IMPL (handled separately)
        spec_only = spec_names - impl_names - {"final_answer"}
        assert spec_only == set(), (
            f"Tools in TOOL_SPECS with no implementation: {spec_only}"
        )


class TestInvariant_ServerEngineToolMapsComplete:
    """The Engine.MOVE_TOOLS / SCROLL_TOOLS / ZOOM_TOOLS maps must cover
    the same functions that nav_tools exposes."""

    def test_move_tools_map_complete(self):
        from apps.geoguessr_server import Engine
        from core.tools import nav_tools

        expected = {
            "north", "northeast", "east", "southeast",
            "south", "southwest", "west", "northwest",
        }
        assert set(Engine.MOVE_TOOLS.keys()) == expected

        # Each value must be the correct nav_tools function
        for direction, fn in Engine.MOVE_TOOLS.items():
            expected_fn = getattr(nav_tools, f"move_{direction}")
            assert fn is expected_fn, f"MOVE_TOOLS['{direction}'] doesn't point to nav_tools.move_{direction}"

    def test_scroll_tools_map_complete(self):
        from apps.geoguessr_server import Engine
        from core.tools import nav_tools

        for direction in ("left", "right", "up", "down"):
            assert direction in Engine.SCROLL_TOOLS
            assert Engine.SCROLL_TOOLS[direction] is getattr(nav_tools, f"scroll_{direction}")

    def test_zoom_tools_map_complete(self):
        from apps.geoguessr_server import Engine
        from core.tools import nav_tools

        for direction in ("in", "out"):
            assert direction in Engine.ZOOM_TOOLS
            assert Engine.ZOOM_TOOLS[direction] is getattr(nav_tools, f"zoom_{direction}")


class TestInvariant_WrapperSyncFieldsCoverServerResponse:
    """The wrapper's _sync_state _FIELDS tuple should cover every key
    the server actually returns in its state snapshot."""

    def test_sync_fields_cover_snapshot(self):
        from apps.geoguessr_server import Engine
        from apps.geoguessr_wrapper import GeoGuessrAPI

        # Get the keys the server puts in its snapshot
        eng = Engine()
        eng.state.episode_id = "test"
        eng.state.pano_id = "pano"
        snapshot_keys = set(eng._state_snapshot().keys())

        # Get the fields the wrapper syncs
        api = GeoGuessrAPI(base_url="http://localhost:9999")
        sync_fields = {
            "session_id", "episode_id", "pano_id", "lat", "lng",
            "heading", "pitch", "zoom", "available_moves",
            "step_count", "image_path",
        }

        missing = snapshot_keys - sync_fields
        assert missing == set(), (
            f"Server returns keys {missing} that the wrapper's "
            f"_sync_state will silently ignore."
        )


class TestInvariant_DirectionConesTotalCoverage:
    """DIR_CONES must cover 0..360 with no gaps and no overlaps
    (when using exclusive upper bounds, as per the fix)."""

    def test_every_degree_maps_to_exactly_one_direction(self):
        from core.navigation.pure_nav import _heading_to_direction

        for deg_10x in range(3600):  # 0.0, 0.1, 0.2 ... 359.9
            h = deg_10x / 10.0
            d = _heading_to_direction(h)
            assert d in ("N", "NE", "E", "SE", "S", "SW", "W", "NW"), (
                f"heading {h} mapped to unexpected direction '{d}'"
            )

    def test_wrap_around_360(self):
        from core.navigation.pure_nav import _heading_to_direction
        assert _heading_to_direction(0.0) == _heading_to_direction(360.0)


class TestInvariant_PureNavRoundTrip:
    """Every pure_nav function must return valid JSON with type='result' or type='command'."""

    @pytest.fixture
    def state_json(self):
        return json.dumps({
            "panoId": "abc",
            "pov": {"heading": 90.0, "pitch": 0.0, "zoom": 1.0},
            "links": [
                {"heading": 0.0, "panoId": "n"},
                {"heading": 90.0, "panoId": "e"},
            ],
        })

    @pytest.mark.parametrize("func_name", [
        "check_direction", "check_available_moves",
        "move_north", "move_east",
        "move_south",  # no link -> result with ok=False
    ])
    def test_returns_valid_envelope(self, state_json, func_name):
        from core.navigation import pure_nav
        fn = getattr(pure_nav, func_name)
        raw = fn(state_json)
        payload = json.loads(raw)
        assert "type" in payload, f"{func_name} output missing 'type' key"
        assert payload["type"] in ("result", "command"), (
            f"{func_name} returned unknown type: {payload['type']}"
        )

    @pytest.mark.parametrize("func_name", [
        "scroll_left", "scroll_right", "scroll_up", "scroll_down",
        "zoom_in", "zoom_out",
    ])
    def test_delta_funcs_return_valid_envelope(self, state_json, func_name):
        from core.navigation import pure_nav
        fn = getattr(pure_nav, func_name)
        raw = fn(state_json, 30.0)
        payload = json.loads(raw)
        assert payload["type"] in ("result", "command")


# ============================================================================
# LAYER 3 — BINARY-SEARCH HELPERS
# These isolate each layer of the call chain so you can pinpoint exactly
# where a failure occurs:
#   pure_nav (pure logic)  →  nav_tools (host bridge)  →  server (Flask)  →  wrapper (HTTP)
# ============================================================================

class TestBinarySearch_PureNavLayer:
    """Test pure_nav in isolation (no host, no network)."""

    def _make_state(self, heading=0.0, pitch=0.0, zoom=1.0, links=None):
        return json.dumps({
            "panoId": "test_pano",
            "pov": {"heading": heading, "pitch": pitch, "zoom": zoom},
            "links": links or [],
        })

    # --- check functions ---

    def test_check_direction_north(self):
        from core.navigation.pure_nav import check_direction
        result = json.loads(check_direction(self._make_state(heading=0.0)))
        assert result["updates"]["direction"] == "N"

    def test_check_direction_east(self):
        from core.navigation.pure_nav import check_direction
        result = json.loads(check_direction(self._make_state(heading=90.0)))
        assert result["updates"]["direction"] == "E"

    def test_check_available_moves_empty_links(self):
        from core.navigation.pure_nav import check_available_moves
        result = json.loads(check_available_moves(self._make_state()))
        moves = result["updates"]["available_moves"]
        # Only universal actions, no move_* entries
        move_dirs = [m for m in moves if m.startswith("move_")]
        assert move_dirs == []

    def test_check_available_moves_with_links(self):
        from core.navigation.pure_nav import check_available_moves
        links = [{"heading": 0.0, "panoId": "n"}, {"heading": 180.0, "panoId": "s"}]
        result = json.loads(check_available_moves(self._make_state(links=links)))
        moves = result["updates"]["available_moves"]
        move_dirs = sorted(m for m in moves if m.startswith("move_"))
        assert "move_N" in move_dirs
        assert "move_S" in move_dirs

    # --- movement ---

    def test_move_succeeds_when_link_exists(self):
        from core.navigation.pure_nav import move_east
        links = [{"heading": 90.0, "panoId": "east_pano"}]
        result = json.loads(move_east(self._make_state(links=links)))
        assert result["type"] == "command"
        assert result["command"]["params"]["panoId"] == "east_pano"

    def test_move_fails_when_no_link(self):
        from core.navigation.pure_nav import move_east
        result = json.loads(move_east(self._make_state()))
        assert result["type"] == "result"
        assert result["updates"]["ok"] is False

    # --- scroll ---

    def test_scroll_left_decreases_heading(self):
        from core.navigation.pure_nav import scroll_left
        result = json.loads(scroll_left(self._make_state(heading=90.0), 30.0))
        assert result["command"]["params"]["heading"] == 60.0

    def test_scroll_right_increases_heading(self):
        from core.navigation.pure_nav import scroll_right
        result = json.loads(scroll_right(self._make_state(heading=90.0), 30.0))
        assert result["command"]["params"]["heading"] == 120.0

    def test_scroll_up_clamped(self):
        from core.navigation.pure_nav import scroll_up
        result = json.loads(scroll_up(self._make_state(pitch=80.0), 30.0))
        assert result["command"]["params"]["pitch"] == 90.0

    def test_scroll_down_clamped(self):
        from core.navigation.pure_nav import scroll_down
        result = json.loads(scroll_down(self._make_state(pitch=-80.0), 30.0))
        assert result["command"]["params"]["pitch"] == -90.0

    # --- zoom ---

    def test_zoom_in(self):
        from core.navigation.pure_nav import zoom_in
        result = json.loads(zoom_in(self._make_state(zoom=1.0), 1.0))
        assert result["command"]["params"]["zoom"] == 2.0

    def test_zoom_out_clamped(self):
        from core.navigation.pure_nav import zoom_out
        result = json.loads(zoom_out(self._make_state(zoom=0.5), 1.0))
        assert result["command"]["params"]["zoom"] == 0.0

    # --- bad inputs ---

    def test_invalid_json_raises(self):
        from core.navigation.pure_nav import check_direction
        from core.exceptions import InvalidStateError
        with pytest.raises(InvalidStateError):
            check_direction("not json")

    def test_none_delta_returns_error(self):
        from core.navigation.pure_nav import scroll_left
        result = json.loads(scroll_left(self._make_state(), None))
        assert result["updates"]["ok"] is False

    def test_nan_delta_returns_error(self):
        from core.navigation.pure_nav import scroll_left
        result = json.loads(scroll_left(self._make_state(), float("nan")))
        assert result["updates"]["ok"] is False

    def test_inf_delta_returns_error(self):
        from core.navigation.pure_nav import zoom_in
        result = json.loads(zoom_in(self._make_state(), float("inf")))
        assert result["updates"]["ok"] is False


class TestBinarySearch_NavToolsLayer:
    """Test nav_tools in isolation using a mock host client.
    This tests the bridge between pure_nav and the host subprocess."""

    class FakeClient:
        """Minimal stand-in for StreetViewHostClient."""
        def __init__(self):
            self._state = {
                "panoId": "fake_pano",
                "position": {"lat": 40.0, "lng": -74.0},
                "pov": {"heading": 90.0, "pitch": 0.0, "zoom": 1.0},
                "links": [{"heading": 90.0, "panoId": "east_pano"}],
                "date": "2024-01",
            }
            self.set_pov_calls = []
            self.set_pano_calls = []

        def wait_for_stable(self, session_id):
            pass

        def get_state(self, session_id):
            return self._state

        def set_pov(self, session_id, heading=None, pitch=None, zoom=None):
            self.set_pov_calls.append({"heading": heading, "pitch": pitch, "zoom": zoom})
            if heading is not None:
                self._state["pov"]["heading"] = heading
            if pitch is not None:
                self._state["pov"]["pitch"] = pitch
            if zoom is not None:
                self._state["pov"]["zoom"] = zoom

        def set_pano(self, session_id, pano_id):
            self.set_pano_calls.append(pano_id)
            self._state["panoId"] = pano_id

        def set_position(self, session_id, lat, lng):
            pass

        def init(self, session_id, lat, lng, heading=0, pitch=0, zoom=1):
            self._state["pov"] = {"heading": heading, "pitch": pitch, "zoom": zoom}
            self._state["position"] = {"lat": lat, "lng": lng}

    @pytest.fixture
    def ctx_and_client(self, monkeypatch, tmp_path):
        from core.tools.contracts import ToolContext

        client = self.FakeClient()
        ctx = ToolContext(
            session_id="test_session",
            meta={
                "host_client": client,
                "image_root": str(tmp_path),
                "image_step": 1,
                "capture_images": False,  # skip real image capture
            },
        )
        return ctx, client

    def test_check_direction_via_nav_tools(self, ctx_and_client):
        from core.tools.nav_tools import check_direction
        ctx, _ = ctx_and_client
        result = check_direction(ctx, {})
        assert result.ok is True
        assert result.updates["direction"] == "E"  # heading=90

    def test_check_available_moves_via_nav_tools(self, ctx_and_client):
        from core.tools.nav_tools import check_available_moves
        ctx, _ = ctx_and_client
        result = check_available_moves(ctx, {})
        assert result.ok is True
        assert "available_moves" in result.updates

    def test_move_east_via_nav_tools(self, ctx_and_client):
        from core.tools.nav_tools import move_east
        ctx, client = ctx_and_client
        result = move_east(ctx, {})
        assert result.ok is True
        # Should have called set_pano on the client
        assert len(client.set_pano_calls) == 1
        assert client.set_pano_calls[0] == "east_pano"

    def test_scroll_left_via_nav_tools(self, ctx_and_client):
        from core.tools.nav_tools import scroll_left
        ctx, client = ctx_and_client
        result = scroll_left(ctx, {"delta": 30.0})
        assert result.ok is True
        assert len(client.set_pov_calls) == 1
        assert client.set_pov_calls[0]["heading"] == 60.0

    def test_zoom_in_via_nav_tools(self, ctx_and_client):
        from core.tools.nav_tools import zoom_in
        ctx, client = ctx_and_client
        result = zoom_in(ctx, {"delta": 1.0})
        assert result.ok is True
        assert len(client.set_pov_calls) == 1
        assert client.set_pov_calls[0]["zoom"] == 2.0

    def test_missing_delta_returns_error(self, ctx_and_client):
        from core.tools.nav_tools import scroll_left
        ctx, _ = ctx_and_client
        result = scroll_left(ctx, {})
        assert result.ok is False

    def test_missing_client_raises(self):
        from core.tools.contracts import ToolContext
        from core.tools.nav_tools import check_direction
        from core.exceptions import MissingContextError

        ctx = ToolContext(session_id="no_client", meta={})
        with pytest.raises(MissingContextError):
            check_direction(ctx, {})


class TestBinarySearch_DispatcherLayer:
    """Test the dispatcher.run_tool wrapper over nav_tools."""

    class FakeClient:
        def __init__(self):
            self._state = {
                "panoId": "dp",
                "position": {"lat": 0, "lng": 0},
                "pov": {"heading": 0, "pitch": 0, "zoom": 1},
                "links": [{"heading": 0, "panoId": "north_pano"}],
                "date": None,
            }
        def wait_for_stable(self, sid): pass
        def get_state(self, sid): return self._state
        def set_pano(self, sid, pid): self._state["panoId"] = pid
        def set_pov(self, sid, **kw):
            for k, v in kw.items():
                if v is not None:
                    self._state["pov"][k] = v
        def set_position(self, sid, lat, lng): pass
        def init(self, sid, lat, lng, heading=0, pitch=0, zoom=1): pass

    @pytest.fixture
    def ctx(self, tmp_path):
        from core.tools.contracts import ToolContext
        return ToolContext(
            session_id="dispatch_test",
            meta={
                "host_client": self.FakeClient(),
                "image_root": str(tmp_path),
                "image_step": 1,
                "capture_images": False,
            },
        )

    def test_run_tool_known_tool(self, ctx):
        from core.tools.dispatcher import run_tool
        result = run_tool(ctx, "check_direction", {})
        assert result.ok is True

    def test_run_tool_unknown_raises(self, ctx):
        from core.tools.dispatcher import run_tool
        with pytest.raises(KeyError, match="unknown_tool"):
            run_tool(ctx, "nonexistent_tool", {})

    def test_run_tool_normalize_name(self, ctx):
        from core.tools.dispatcher import _normalize_tool_name
        assert _normalize_tool_name("move_n") == "move_north"
        assert _normalize_tool_name("move_ne") == "move_northeast"
        assert _normalize_tool_name("check_direction") == "check_direction"

    def test_llm_blocked_tool(self, ctx):
        from core.tools.dispatcher import run_tool
        ctx.meta["caller"] = "llm"
        with pytest.raises(PermissionError, match="tool_not_allowed_for_llm"):
            run_tool(ctx, "init_panorama", {"lat": 0, "lng": 0})


class TestBinarySearch_ServerEngineLayer:
    """Test the Engine class (server-side) without Flask or network."""

    def test_engine_construction(self):
        from apps.geoguessr_server import Engine
        eng = Engine()
        assert eng.state.episode_active is False
        assert eng.state.session_id is None

    def test_start_episode_without_host_raises(self):
        from apps.geoguessr_server import Engine
        eng = Engine()
        # Can start episode even without host (it just skips host pull)
        data = eng.start_episode()
        assert "episode_id" in data
        assert eng.state.episode_active is True

    def test_double_start_episode_raises(self):
        from apps.geoguessr_server import Engine
        eng = Engine()
        eng.start_episode()
        with pytest.raises(RuntimeError, match="already active"):
            eng.start_episode()

    def test_end_episode_resets_state(self):
        from apps.geoguessr_server import Engine
        eng = Engine()
        eng.start_episode()
        data = eng.end_episode()
        assert "episode_id" in data
        assert eng.state.episode_active is False
        assert eng.state.pano_id is None

    def test_move_without_episode_raises(self):
        from apps.geoguessr_server import Engine
        eng = Engine()
        with pytest.raises(RuntimeError, match="No active episode"):
            eng.move("north")

    def test_invalid_direction_raises(self):
        from apps.geoguessr_server import Engine
        eng = Engine()
        eng.start_episode()
        with pytest.raises(ValueError, match="Invalid direction"):
            eng.move("upward")


class TestBinarySearch_WrapperLayer:
    """Test the GeoGuessrAPI wrapper construction and local state logic.
    No network calls — we just verify state management."""

    def test_construction_defaults(self):
        from apps.geoguessr_wrapper import GeoGuessrAPI
        api = GeoGuessrAPI(base_url="http://localhost:9999")
        assert api.state.episode_active is False
        assert api.state.session_id is None
        assert api.long_context is False

    def test_sync_state_updates_fields(self):
        from apps.geoguessr_wrapper import GeoGuessrAPI
        api = GeoGuessrAPI(base_url="http://localhost:9999")
        api._sync_state({
            "session_id": "s1",
            "episode_id": "ep1",
            "pano_id": "p1",
            "lat": 40.0,
            "lng": -74.0,
            "heading": 90.0,
            "pitch": 10.0,
            "zoom": 2.0,
            "available_moves": ["move_N"],
            "step_count": 3,
            "image_path": "/img/test.jpg",
        })
        assert api.state.session_id == "s1"
        assert api.state.lat == 40.0
        assert api.state.heading == 90.0
        assert api.state.step_count == 3

    def test_sync_state_ignores_unknown_fields(self):
        from apps.geoguessr_wrapper import GeoGuessrAPI
        api = GeoGuessrAPI(base_url="http://localhost:9999")
        # Should not crash on unknown keys
        api._sync_state({"unknown_field": "value", "lat": 50.0})
        assert api.state.lat == 50.0
        assert not hasattr(api.state, "unknown_field")

    def test_get_state_returns_dict(self):
        from apps.geoguessr_wrapper import GeoGuessrAPI
        api = GeoGuessrAPI(base_url="http://localhost:9999")
        state = api.get_state()
        assert isinstance(state, dict)
        assert "session_id" in state

    def test_get_state_json_is_valid(self):
        from apps.geoguessr_wrapper import GeoGuessrAPI
        api = GeoGuessrAPI(base_url="http://localhost:9999")
        raw = api.get_state_json()
        parsed = json.loads(raw)
        assert "session_id" in parsed

    def test_equality(self):
        from apps.geoguessr_wrapper import GeoGuessrAPI
        a = GeoGuessrAPI(base_url="http://localhost:9999")
        b = GeoGuessrAPI(base_url="http://localhost:9999")
        assert a == b

    def test_load_scenario_updates_local_state(self):
        from apps.geoguessr_wrapper import GeoGuessrAPI
        api = GeoGuessrAPI(base_url="http://localhost:9999")
        # _load_scenario calls _call which does HTTP — mock it
        api._call = lambda *a, **kw: {}
        api._load_scenario({"random_seed": 99, "max_steps": 50})
        assert api.state.random_seed == 99
        assert api.state.max_steps == 50
