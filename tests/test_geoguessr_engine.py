import importlib
import math
import sys
import types

import pytest


class FakeClient:
    pass


class FakeResult:
    def __init__(self, updates=None, ok=True):
        self.ok = ok
        self.updates = updates or {}


def _ok_tool_result(*args, **kwargs):
    return FakeResult(
        updates={"image_path": "img.png", "available_moves": ["move_N"]}
    )


@pytest.fixture
def engine_class(monkeypatch):
    stub = types.SimpleNamespace(
        create_openai_file=lambda *args, **kwargs: None,
        save_image=lambda *args, **kwargs: None,
    )
    monkeypatch.setitem(sys.modules, "core.utils.image_store", stub)
    if "apps.geoguessr_server" in sys.modules:
        del sys.modules["apps.geoguessr_server"]
    module = importlib.import_module("apps.geoguessr_server")
    return module.Engine


def test_load_scenario_updates_state(engine_class):
    eng = engine_class()
    data = eng.load_scenario({"random_seed": 7, "max_steps": 5, "image_root": "out"})
    assert data["loaded"] is True
    assert eng.state.random_seed == 7
    assert eng.state.max_steps == 5
    assert eng.state.image_root == "out"


def test_move_increments_step_count(engine_class, monkeypatch):
    eng = engine_class()
    eng._set_host_context(FakeClient(), "sid")
    monkeypatch.setattr(engine_class, "MOVE_TOOLS", {"north": _ok_tool_result})
    monkeypatch.setattr(engine_class, "_pull_host_state", lambda *a, **k: None)
    result = eng.move("north")
    assert result["step_count"] == 1
    assert eng.state.step_count == 1
    assert result["image_path"] == "img.png"


def test_move_respects_max_steps(engine_class):
    eng = engine_class()
    eng.state.max_steps = 0
    with pytest.raises(RuntimeError, match="Max steps reached"):
        eng.move("north")


def test_scroll_rejects_non_finite_delta(engine_class):
    eng = engine_class()
    with pytest.raises(ValueError, match="Invalid delta value"):
        eng.scroll("left", float("nan"))
    with pytest.raises(ValueError, match="Invalid delta value"):
        eng.scroll("left", float("inf"))
    with pytest.raises(ValueError, match="Invalid delta value"):
        eng.scroll("left", -float("inf"))


def test_zoom_rejects_non_finite_delta(engine_class):
    eng = engine_class()
    with pytest.raises(ValueError, match="Invalid delta value"):
        eng.zoom("in", math.nan)
