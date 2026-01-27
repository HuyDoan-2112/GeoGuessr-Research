import json
import pytest

from apps.geoguessr_wrapper import GeoGuessrAPI


def test_sync_state_updates_known_fields_only():
    api = GeoGuessrAPI(base_url="http://localhost:9999")
    api._sync_state({"lat": 10.5, "lng": -20.25, "unknown": "value"})
    state = api.get_state()
    assert state["lat"] == 10.5
    assert state["lng"] == -20.25
    assert "unknown" not in state


def test_call_raises_on_ok_false():
    api = GeoGuessrAPI(base_url="http://localhost:9999")
    api._post = lambda path, body=None: {"ok": False, "error": {"message": "nope"}}
    with pytest.raises(RuntimeError, match="nope"):
        api._call("POST", "/test", {})


def test_call_raises_on_non_json():
    api = GeoGuessrAPI(base_url="http://localhost:9999")
    def _raise_value_error(path, body=None):
        raise ValueError("bad json")
    api._post = _raise_value_error
    with pytest.raises(RuntimeError, match="non-JSON"):
        api._call("POST", "/test", {})


def test_connect_host_sets_session_header():
    api = GeoGuessrAPI(base_url="http://localhost:9999")
    api._call = lambda *args, **kwargs: {"session_id": "s1"}
    api.connect_host(api_key="dummy")
    assert api._session.headers["X-Session-ID"] == "s1"
    assert api.state["session_id"] == "s1"


def test_get_state_json_is_valid():
    api = GeoGuessrAPI(base_url="http://localhost:9999")
    raw = api.get_state_json()
    parsed = json.loads(raw)
    assert "session_id" in parsed
