import importlib
import sys
import types

import pytest


@pytest.fixture
def server_module(monkeypatch):
    # Avoid import-time failure if core.utils.image_store lacks create_openai_file.
    stub = types.SimpleNamespace(
        create_openai_file=lambda *args, **kwargs: None,
        save_image=lambda *args, **kwargs: None,
    )
    monkeypatch.setitem(sys.modules, "core.utils.image_store", stub)
    return importlib.import_module("apps.geoguessr_server")


@pytest.fixture
def client(monkeypatch, server_module):
    server = server_module
    server.engines.clear()

    def fake_connect(self, api_key, session_id=None, url_signing_secret=None):
        return {"session_id": session_id or "s1"}

    monkeypatch.setattr(server.Engine, "connect", fake_connect)
    server.app.testing = True
    return server.app.test_client()


def test_health_route_ok(client, server_module):
    resp = client.get("/health")
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["ok"] is True
    assert "active_sessions" in data["updates"]


def test_connect_requires_api_key(client, monkeypatch, server_module):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    resp = client.post("/connect", json={})
    data = resp.get_json()
    assert resp.status_code == 400
    assert data["ok"] is False
    assert "api_key" in data["error"]["message"]


def test_connect_success(client, monkeypatch, server_module):
    server = server_module
    resp = client.post("/connect", json={"api_key": "dummy"})
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["updates"]["session_id"] == "s1"
    assert "s1" in server.engines


def test_state_requires_session_header(client, server_module):
    resp = client.get("/state")
    data = resp.get_json()
    assert resp.status_code == 400
    assert data["ok"] is False
    assert "Unknown session" in data["error"]["message"]


def test_check_direction_with_session(client, server_module):
    server = server_module
    class FakeEngine:
        def check_direction(self):
            return {"description": "Facing N (0.0 degrees)"}

    server.engines["s1"] = FakeEngine()
    resp = client.get("/check/direction", headers={"X-Session-ID": "s1"})
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["updates"]["description"].startswith("Facing")
