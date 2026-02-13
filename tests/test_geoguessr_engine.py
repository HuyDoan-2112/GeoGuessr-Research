import importlib
import sys
import types

import pytest


class FakeResult:
    def __init__(self, updates=None, ok=True):
        self.ok = ok
        self.updates = updates or {}


class FakeHostClient:
    def __init__(self):
        self.closed_sessions = []

    def start(self, session_id, api_key=None):
        return None

    def close_session(self, session_id):
        self.closed_sessions.append(session_id)


@pytest.fixture
def server_module(monkeypatch):
    # Avoid import-time issues if core.utils.image_store is stubbed in other tests
    stub = types.SimpleNamespace(
        create_openai_file=lambda *args, **kwargs: None,
        save_image=lambda *args, **kwargs: None,
    )
    monkeypatch.setitem(sys.modules, "core.utils.image_store", stub)
    if "apps.geoguessr_server" in sys.modules:
        del sys.modules["apps.geoguessr_server"]
    return importlib.import_module("apps.geoguessr_server")


def test_connect_sets_session_id(server_module, monkeypatch):
    monkeypatch.setattr(server_module, "StreetViewHostClient", FakeHostClient)
    eng = server_module.Engine()
    data = eng.connect("dummy", session_id="sid")
    assert data["session_id"] == "sid"
    assert eng.state.session_id == "sid"


def test_init_panorama_updates_available_moves(server_module, monkeypatch):
    eng = server_module.Engine()
    eng._set_host_context(FakeHostClient(), "sid")
    monkeypatch.setattr(server_module.nav_tools, "init_panorama", lambda *a, **k: FakeResult(
        updates={"available_moves": ["move_north"]}
    ))
    monkeypatch.setattr(server_module.Engine, "_pull_host_state", lambda *a, **k: None)
    data = eng.init_panorama(1.0, 2.0)
    assert data["available_moves"] == ["move_north"]
    assert eng.state.available_moves == ["move_north"]


def test_move_increments_step_count(server_module, monkeypatch):
    eng = server_module.Engine()
    eng._set_host_context(FakeHostClient(), "sid")
    monkeypatch.setattr(server_module.nav_tools, "move_north", lambda *a, **k: FakeResult(
        updates={"available_moves": ["move_north"]}
    ))
    monkeypatch.setattr(server_module.Engine, "_pull_host_state", lambda *a, **k: None)
    eng.move("north")
    assert eng.state.step_count == 1


def test_end_session_resets_state(server_module):
    eng = server_module.Engine()
    client = FakeHostClient()
    eng._set_host_context(client, "sid")
    eng.state.pano_id = "p1"
    eng.state.lat = 1.0
    eng.state.lng = 2.0
    eng.state.heading = 10.0
    eng.state.pitch = 5.0
    eng.state.zoom = 2.0
    eng.state.available_moves = ["move_north"]
    eng.state.step_count = 3

    result = eng.end_session()
    assert result["step_count"] == 3
    assert eng.state.pano_id is None
    assert eng.state.lat is None
    assert eng.state.lng is None
    assert eng.state.heading == 0.0
    assert eng.state.pitch == 0.0
    assert eng.state.zoom == 1.0
    assert eng.state.available_moves == []


def test_scroll_rejects_non_finite_delta(server_module):
    eng = server_module.Engine()
    with pytest.raises(ValueError, match="Invalid delta value"):
        eng.scroll("left", float("nan"))
    with pytest.raises(ValueError, match="Invalid delta value"):
        eng.scroll("left", float("inf"))
    with pytest.raises(ValueError, match="Invalid delta value"):
        eng.scroll("left", -float("inf"))


def test_zoom_rejects_non_finite_delta(server_module):
    eng = server_module.Engine()
    with pytest.raises(ValueError, match="Invalid delta value"):
        eng.zoom("in", float("nan"))
