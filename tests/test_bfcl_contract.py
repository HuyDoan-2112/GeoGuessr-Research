"""Exercise the HTTP boundary, including the real BFCL consumer when available."""

import ast
import base64
import io
import json
import os
import sqlite3
import threading
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests
from PIL import Image

DIRECTIONS = (
    "north", "northeast", "east", "southeast",
    "south", "southwest", "west", "northwest",
)
UNIVERSAL_ACTIONS = {
    "capture_view", "check_direction", "scroll_up", "scroll_left", "scroll_right",
    "scroll_down", "zoom_in", "zoom_out",
}
BFCL_PATH = (
    Path(__file__).resolve().parents[2]
    / "gorilla-hans/berkeley-function-call-leaderboard/bfcl_eval"
    / "eval_checker/multi_turn_eval/func_source_code/street_view.py"
)


class MemoryImages:
    def __init__(self):
        output = io.BytesIO()
        image = Image.new("RGB", (12, 10), "red")
        image.paste("blue", (0, 5, 12, 10))
        image.save(output, format="JPEG", subsampling=0, quality=100)
        self.jpeg = output.getvalue()
        self.calls = []
        self.fail = False

    def get(self, pano_id, heading, pitch, zoom):
        self.calls.append((pano_id, heading, pitch, zoom))
        if self.fail:
            raise RuntimeError("capture unavailable")
        return self.jpeg


@pytest.fixture
def server(tmp_path):
    from apps.geoguessr_server_gcs import create_app

    database = tmp_path / "panoramas.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE panoramas (pano_id TEXT PRIMARY KEY, lat REAL, lng REAL, "
            "metadata_json TEXT);"
            "CREATE TABLE panorama_links (from_pano_id TEXT, to_pano_id TEXT, "
            "heading REAL, description TEXT, link_date TEXT);"
        )
        links = [
            {"panoId": direction, "heading": index * 45.0}
            for index, direction in enumerate(DIRECTIONS)
        ]
        connection.execute(
            "INSERT INTO panoramas VALUES (?, ?, ?, ?)",
            ("start", 10.0, 20.0, json.dumps({"links": links})),
        )
        for index, direction in enumerate(DIRECTIONS):
            connection.execute(
                "INSERT INTO panoramas VALUES (?, ?, ?, ?)",
                (direction, 11.0 + index, 20.0,
                 json.dumps({"links": [{"panoId": "start", "heading": 180.0}]})),
            )

    images = MemoryImages()
    app = create_app({
        "TESTING": True,
        "DB_PATH": str(database),
        "GCS_BUCKET": "unused-test-bucket",
        "GCS_PREFIX": "mapcrunch_images/",
        "CACHE_DIR": str(tmp_path / "cache"),
        "CACHE_SIZE_GB": 0.001,
        "CROP_MODE": "off",
        "IMAGE_STORE": images,
    })
    try:
        yield app, images
    finally:
        sessions = app.extensions["street_view_sessions"]
        for session in list(sessions.values()):
            session.api.close()
        sessions.clear()


def successful(response):
    envelope = response.get_json()
    assert response.status_code == 200, envelope
    assert envelope["ok"] is True, envelope
    return envelope["updates"]


def connect(client):
    # Credentials may still be forwarded by BFCL although this backend needs none.
    updates = successful(client.post("/connect", json={
        "api_key": "unused", "url_signing_secret": "unused",
    }))
    assert updates["session_id"]
    return {"X-Session-ID": updates["session_id"]}


def initialize(client, headers):
    return successful(client.post("/init_panorama", headers=headers, json={
        "lat": "10.000001", "lng": "20.000001",
    }))


def test_http_contract_without_external_checkout(server):
    app, images = server
    client = app.test_client()
    headers = connect(client)
    updates = initialize(client, headers)
    assert set(updates["available_moves"]) == UNIVERSAL_ACTIONS | {
        f"move_{direction}" for direction in DIRECTIONS
    }
    assert images.calls == []
    direction = successful(client.get("/check/direction", headers=headers))
    assert direction["description"] == "Facing N (0.0 degrees)"
    assert direction["available_moves"] == updates["available_moves"]

    # A move changes panorama but leaves capture to the caller.
    for direction in DIRECTIONS:
        initialize(client, headers)
        before = len(images.calls)
        moved = successful(client.post(f"/move/{direction}", headers=headers, json={}))
        assert set(moved["available_moves"]) == UNIVERSAL_ACTIONS | {"move_south"}
        assert len(images.calls) == before
        captured = successful(client.post("/capture/view", headers=headers, json={}))
        assert base64.b64decode(captured["image_base64"], validate=True) == images.jpeg
        assert images.calls[-1] == (direction, 0.0, 0.0, 1.0)

    initialize(client, headers)
    for path, delta, expected in [
        ("/scroll/right", -90, (90.0, 0.0, 1.0)),
        ("/scroll/left", 120, (330.0, 0.0, 1.0)),
        ("/scroll/up", 1000, (330.0, 30.0, 1.0)),
        ("/scroll/down", 1000, (330.0, -30.0, 1.0)),
        ("/zoom/in", -100, (330.0, -30.0, 2.0)),
        ("/zoom/out", 100, (330.0, -30.0, 0.0)),
    ]:
        before = len(images.calls)
        updates = successful(client.post(path, headers=headers, json={"delta": delta}))
        assert "available_moves" in updates
        assert len(images.calls) == before
        successful(client.post("/capture/view", headers=headers, json={}))
        assert len(images.calls) == before + 1
        assert images.calls[-1] == ("start", *expected)

    successful(client.post("/end_session", headers=headers, json={}))
    expired = client.post("/capture/view", headers=headers, json={})
    assert expired.status_code >= 400
    assert expired.get_json()["ok"] is False


def test_sessions_are_isolated_and_capture_failures_are_enveloped(server):
    app, images = server
    client = app.test_client()
    first, second = connect(client), connect(client)
    assert first != second
    initialize(client, first)
    initialize(client, second)
    successful(client.post("/scroll/right", headers=first, json={"delta": 90}))
    assert successful(client.get("/check/direction", headers=first))["description"] == (
        "Facing E (90.0 degrees)"
    )
    assert successful(client.get("/check/direction", headers=second))["description"] == (
        "Facing N (0.0 degrees)"
    )
    images.fail = True
    response = client.post("/capture/view", headers=first, json={})
    assert response.status_code >= 400
    envelope = response.get_json()
    assert envelope["ok"] is False
    assert "capture unavailable" in envelope["error"]["message"]
    successful(client.post("/end_session", headers=first, json={}))
    successful(client.get("/check/direction", headers=second))
    successful(client.post("/end_session", headers=second, json={}))


@pytest.mark.parametrize("crop_mode", ["off", "top", "bottom"])
def test_capture_crop_preserves_original_image_bytes(server, crop_mode):
    from apps.geoguessr_server_gcs import create_app

    original_app, images = server
    app = create_app({**original_app.config, "CROP_MODE": crop_mode})
    client = app.test_client()
    headers = connect(client)
    initialize(client, headers)
    original = images.jpeg
    result = successful(client.post("/capture/view", headers=headers, json={}))
    captured = base64.b64decode(result["image_base64"], validate=True)
    assert images.jpeg == original
    with Image.open(io.BytesIO(captured)) as image:
        assert image.format == "JPEG"
        assert image.size == (12, 10 if crop_mode == "off" else 5)
        top = image.getpixel((0, 0))
        bottom = image.getpixel((0, image.height - 1))
        if crop_mode == "off":
            assert captured == original
            assert top[0] > 240 and top[2] < 15
            assert bottom[2] > 240 and bottom[0] < 15
        else:
            channel = 0 if crop_mode == "top" else 2
            other_channel = 2 if crop_mode == "top" else 0
            for pixel in (top, bottom):
                assert pixel[channel] > 240
                assert pixel[other_channel] < 15
    successful(client.post("/end_session", headers=headers, json={}))


def test_teardown_removes_session_and_clears_navigation(server):
    app, _ = server
    client = app.test_client()
    headers = connect(client)
    sid = headers["X-Session-ID"]
    registry = app.extensions["street_view_sessions"]
    api = registry[sid].api
    initialize(client, headers)
    successful(client.post("/end_session", headers=headers, json={}))
    assert sid not in registry
    assert api.available_moves == []
    with pytest.raises(RuntimeError, match="No panorama loaded"):
        api.capture_view()


@pytest.fixture
def clock(monkeypatch):
    from apps import geoguessr_server_gcs

    now = [1000.0]
    monkeypatch.setattr(geoguessr_server_gcs, "monotonic", lambda: now[0])
    return now


def test_connect_reclaims_sessions_after_twelve_idle_hours(server, clock, monkeypatch):
    app, _ = server
    client = app.test_client()
    headers = connect(client)
    sid = headers["X-Session-ID"]
    registry = app.extensions["street_view_sessions"]
    session = registry[sid]
    closed = []
    original_close = session.api.close

    def close():
        closed.append(sid)
        original_close()

    monkeypatch.setattr(session.api, "close", close)
    assert app.config["SESSION_IDLE_TIMEOUT"] == 12 * 60 * 60
    clock[0] += 12 * 60 * 60 - 1
    connect(client)
    assert sid in registry
    assert closed == []

    clock[0] += 1
    connect(client)
    assert sid not in registry
    assert closed == [sid]
    response = client.get("/check/direction", headers=headers)
    assert response.get_json()["ok"] is False
    connect(client)
    assert closed == [sid]


@pytest.mark.parametrize("capture_fails", [False, True])
def test_session_activity_extends_idle_timeout(server, clock, capture_fails):
    app, images = server
    client = app.test_client()
    headers = connect(client)
    initialize(client, headers)
    sid = headers["X-Session-ID"]
    timeout = app.config["SESSION_IDLE_TIMEOUT"]
    clock[0] += timeout - 1
    images.fail = capture_fails
    response = client.post("/capture/view", headers=headers, json={})
    assert response.get_json()["ok"] is not capture_fails

    clock[0] += timeout - 1
    connect(client)
    assert sid in app.extensions["street_view_sessions"]
    clock[0] += 1
    connect(client)
    assert sid not in app.extensions["street_view_sessions"]


def test_connect_keeps_in_flight_session_and_completion_refreshes_idle_time(
    server, clock, monkeypatch,
):
    app, images = server
    client = app.test_client()
    headers = connect(client)
    initialize(client, headers)
    sid = headers["X-Session-ID"]
    timeout = app.config["SESSION_IDLE_TIMEOUT"]
    entered, release = threading.Event(), threading.Event()
    responses = []

    def capture():
        entered.set()
        assert release.wait(5), "capture was not released"
        return images.jpeg

    api = app.extensions["street_view_sessions"][sid].api
    monkeypatch.setattr(api, "capture_view", capture)
    thread = threading.Thread(target=lambda: responses.append(
        app.test_client().post("/capture/view", headers=headers, json={})
    ), daemon=True)
    thread.start()
    try:
        assert entered.wait(5), "capture did not start"
        clock[0] += timeout + 1
        connect(client)
        assert sid in app.extensions["street_view_sessions"]
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(responses) == 1
    successful(responses[0])
    clock[0] += timeout - 1
    connect(client)
    assert sid in app.extensions["street_view_sessions"]
    clock[0] += 1
    connect(client)
    assert sid not in app.extensions["street_view_sessions"]


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_session_idle_timeout_is_rejected(server, timeout):
    from apps.geoguessr_server_gcs import create_app

    app, _ = server
    with pytest.raises(ValueError, match="SESSION_IDLE_TIMEOUT"):
        create_app({**app.config, "SESSION_IDLE_TIMEOUT": timeout})


def test_same_session_serializes_mutation_capture_and_teardown(server, monkeypatch):
    app, images = server
    client = app.test_client()
    headers = connect(client)
    initialize(client, headers)
    sid = headers["X-Session-ID"]
    session = app.extensions["street_view_sessions"][sid]
    mutation_entered = threading.Event()
    capture_entered = threading.Event()
    close_entered = threading.Event()
    release_mutation = threading.Event()
    release_capture = threading.Event()
    allow_teardown_lock = threading.Event()
    attempts = {name: threading.Event() for name in ("mutation", "capture", "teardown")}
    trace, responses, errors = [], {}, []
    original_scroll = session.api.scroll
    original_capture = session.api.capture_view
    original_close = session.api.close

    class ObservedLock:
        """Expose arrival at the real lock without depending on thread timing."""

        def __init__(self):
            self.lock = threading.Lock()

        def __enter__(self):
            name = threading.current_thread().name
            attempts[name].set()
            if name == "teardown":
                assert allow_teardown_lock.wait(5), "teardown was not released"
            assert self.lock.acquire(timeout=5), "session lock acquisition timed out"

        def __exit__(self, *_):
            self.lock.release()

    def blocked_scroll(*args):
        mutation_entered.set()
        assert release_mutation.wait(5), "mutation was not released"
        trace.append("mutation")
        return original_scroll(*args)

    def blocked_capture():
        capture_entered.set()
        assert release_capture.wait(5), "capture was not released"
        trace.append("capture")
        return original_capture()

    def observed_close():
        close_entered.set()
        trace.append("teardown")
        return original_close()

    monkeypatch.setattr(session, "lock", ObservedLock())
    monkeypatch.setattr(session.api, "scroll", blocked_scroll)
    monkeypatch.setattr(session.api, "capture_view", blocked_capture)
    monkeypatch.setattr(session.api, "close", observed_close)

    def send(name, path, body):
        try:
            responses[name] = app.test_client().post(path, headers=headers, json=body)
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(
            target=send, args=("mutation", "/scroll/right", {"delta": 90}),
            name="mutation", daemon=True,
        ),
        threading.Thread(
            target=send, args=("capture", "/capture/view", {}),
            name="capture", daemon=True,
        ),
        threading.Thread(
            target=send, args=("teardown", "/end_session", {}),
            name="teardown", daemon=True,
        ),
    ]
    started = []
    try:
        threads[0].start()
        started.append(threads[0])
        assert mutation_entered.wait(5)
        threads[1].start()
        started.append(threads[1])
        assert attempts["capture"].wait(5)
        threads[2].start()
        started.append(threads[2])
        assert attempts["teardown"].wait(5)
        assert not capture_entered.is_set()
        assert not close_entered.is_set()
        release_mutation.set()
        assert capture_entered.wait(5)
        allow_teardown_lock.set()
        assert not close_entered.is_set()
        release_capture.set()
    finally:
        release_mutation.set()
        release_capture.set()
        allow_teardown_lock.set()
        for thread in started:
            thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in started)
    assert not errors
    assert trace == ["mutation", "capture", "teardown"]
    for response in responses.values():
        successful(response)
    assert len(responses) == 3
    assert images.calls == [("start", 90.0, 0.0, 1.0)]
    assert sid not in app.extensions["street_view_sessions"]
    with pytest.raises(RuntimeError, match="No panorama loaded"):
        session.api.capture_view()


@pytest.mark.parametrize("path, method", [
    ("/init_panorama", "POST"), ("/check/direction", "GET"),
    ("/capture/view", "POST"), ("/move/north", "POST"),
    ("/scroll/left", "POST"), ("/zoom/in", "POST"), ("/end_session", "POST"),
])
def test_session_header_is_required(server, path, method):
    app, _ = server
    response = app.test_client().open(path, method=method, json={})
    assert response.status_code >= 400
    assert response.get_json()["ok"] is False
    assert response.get_json()["error"]["message"]


class ConsumerImageResult:
    """Only replace BFCL's unrelated image-result dependency, not its client."""

    def __init__(self, image_base64, mime_type):
        self.image_bytes = base64.b64decode(image_base64, validate=True)
        self.mime_type = mime_type


class FlaskAdapter(requests.adapters.BaseAdapter):
    """Route real requests.PreparedRequest objects through Flask without sockets."""

    def __init__(self, app, init_failures=0):
        super().__init__()
        self.client = app.test_client()
        self.calls = []
        self.init_failures = init_failures

    def send(self, request, **kwargs):
        path = urlsplit(request.url).path
        body = request.body
        if path == "/init_panorama" and self.init_failures:
            self.init_failures -= 1
            body = json.dumps({"lat": "invalid-coordinate"})
        result = self.client.open(
            path, method=request.method, headers=dict(request.headers), data=body,
        )
        self.calls.append((path, result.status_code, result.get_json()))
        response = requests.Response()
        response.status_code = result.status_code
        response._content = result.data
        response.headers.update(result.headers)
        response.url = request.url
        response.request = request
        return response

    def close(self):
        pass


@pytest.fixture
def bfcl_client_class():
    path = Path(os.environ.get("BFCL_STREET_VIEW_PATH", BFCL_PATH))
    if not path.is_file():
        pytest.skip("Set BFCL_STREET_VIEW_PATH to run against the external BFCL consumer")
    tree = ast.parse(path.read_text(), filename=str(path))
    dependency = "bfcl_eval.eval_checker.multi_turn_eval.func_source_code"
    imports = [
        node for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == dependency
    ]
    assert len(imports) == 1
    assert [name.name for name in imports[0].names] == ["ImageResult"]
    tree.body.remove(imports[0])
    namespace = {"__name__": "bfcl_street_view_contract", "ImageResult": ConsumerImageResult}
    exec(compile(tree, str(path), "exec"), namespace)
    client_class = namespace["StreetViewAPI"]
    client_class._INIT_RETRY_DELAY = 0
    return client_class


def consumer(client_class, app, monkeypatch, init_failures=0):
    monkeypatch.setenv("GEOGUESSR_SERVER_URL", "http://bfcl.test")
    api = client_class()
    adapter = FlaskAdapter(app, init_failures=init_failures)
    api._session.mount("http://bfcl.test", adapter)
    return api, adapter


def test_real_bfcl_client_lifecycle(server, bfcl_client_class, monkeypatch):
    app, images = server
    api, adapter = consumer(bfcl_client_class, app, monkeypatch)
    api._load_scenario({"lat": "10.0", "lng": "20.0"})
    assert api.session_id
    assert api._session.headers["X-Session-ID"] == api.session_id
    assert "move_north" in api._get_updated_tool_list()
    assert "check_direction" in api._get_updated_tool_list()
    assert api.check_direction() == {"description": "Facing N (0.0 degrees)"}
    assert api.capture_view().image_bytes == images.jpeg

    for direction in DIRECTIONS:
        api._call("POST", "/init_panorama", {"lat": "10", "lng": "20"})
        assert getattr(api, f"move_{direction}")() == {"status": "success"}
        assert set(api.available_moves) == UNIVERSAL_ACTIONS | {"move_south"}

    api._call("POST", "/init_panorama", {"lat": "10", "lng": "20"})
    for action, delta in [
        ("scroll_left", -30), ("scroll_right", 90),
        ("scroll_up", 15), ("scroll_down", 30),
        ("zoom_in", 1), ("zoom_out", 2),
    ]:
        before = len(images.calls)
        result = getattr(api, action)(delta)
        assert result.image_bytes == images.jpeg
        assert result.mime_type == "image/jpeg"
        assert len(images.calls) == before + 1
        assert adapter.calls[-1][0] == "/capture/view"

    sid = api.session_id
    api._end_session()
    assert api.session_id is None
    assert api.available_moves == []
    assert "X-Session-ID" not in api._session.headers
    response = app.test_client().post("/capture/view", headers={"X-Session-ID": sid})
    assert response.get_json()["ok"] is False


@pytest.mark.parametrize("failures", [1, 3])
def test_real_bfcl_initialization_retries_close_failed_sessions(
    server, bfcl_client_class, monkeypatch, failures,
):
    app, _ = server
    api, adapter = consumer(bfcl_client_class, app, monkeypatch, init_failures=failures)
    if failures == 3:
        with pytest.raises(RuntimeError, match="initialization failed after 3 attempts"):
            api._load_scenario({"lat": "10", "lng": "20"})
        assert api.session_id is None
        assert api.available_moves == []
    else:
        api._load_scenario({"lat": "10", "lng": "20"})
        assert api.session_id
        api._end_session()

    failed_attempts = adapter.calls[: failures * 3]
    assert [path for path, _, _ in failed_attempts] == [
        "/connect", "/init_panorama", "/end_session",
    ] * failures
    for path, _, envelope in adapter.calls:
        if path == "/connect":
            sid = envelope["updates"]["session_id"]
            response = app.test_client().get(
                "/check/direction", headers={"X-Session-ID": sid},
            )
            assert response.get_json()["ok"] is False


def test_real_bfcl_capture_error_and_session_isolation(server, bfcl_client_class, monkeypatch):
    app, images = server
    first, _ = consumer(bfcl_client_class, app, monkeypatch)
    second, _ = consumer(bfcl_client_class, app, monkeypatch)
    first._load_scenario({"lat": "10", "lng": "20"})
    second._load_scenario({"lat": "10", "lng": "20"})
    assert first.session_id != second.session_id
    first.scroll_right(90)
    assert first.check_direction() == {"description": "Facing E (90.0 degrees)"}
    assert second.check_direction() == {"description": "Facing N (0.0 degrees)"}
    images.fail = True
    with pytest.raises(RuntimeError):
        first.capture_view()
    first._end_session()
    second._end_session()
