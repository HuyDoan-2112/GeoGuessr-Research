"""Exercise the recorded navigation contract against a real SQLite database."""

import json
import sqlite3
from contextlib import closing

import pytest

from apps.street_view import StreetViewSession


class ImageStore:
    def __init__(self):
        self.requests = []

    def get(self, *view):
        self.requests.append(view)
        return b"recorded JPEG"


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "panoramas.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            "CREATE TABLE panoramas (pano_id TEXT PRIMARY KEY, lat REAL, lng REAL, metadata_json TEXT);"
            "CREATE TABLE panorama_links (from_pano_id TEXT, to_pano_id TEXT, heading REAL);"
        )
        links = [
            {"panoId": "north", "heading": 337.5},
            {"panoId": "duplicate", "heading": 0},
            {"panoId": "northeast", "heading": 22.5},
            {"panoId": "missing", "heading": 90},
        ]
        conn.executemany(
            "INSERT INTO panoramas VALUES (?, ?, ?, ?)",
            [
                ("start", 1.0, 2.0, json.dumps({"links": links})),
                ("north", 1.1, 2.0, "{invalid metadata"),
                ("duplicate", 1.2, 2.0, "{}"),
                ("northeast", 1.1, 2.1, '{"links": []}'),
            ],
        )
        conn.executemany(
            "INSERT INTO panorama_links VALUES (?, ?, ?)",
            [("start", "duplicate", 180), ("north", "start", 180)],
        )
        conn.commit()
    return path


@pytest.fixture
def session(database):
    session = StreetViewSession(database, ImageStore())
    yield session
    session.close()


@pytest.fixture
def recorded_connections(database, monkeypatch):
    connect = sqlite3.connect
    connections = []

    def record(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", record)
    return connections


def assert_connections_closed(connections):
    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_nearest_string_coordinates_and_metadata_take_precedence(session):
    result = session.init_panorama("1.01", "2.0", heading="-30", pitch="15", zoom="2")
    assert result["available_moves"] == [
        "capture_view", "check_direction", "scroll_up", "scroll_left", "scroll_right", "scroll_down",
        "zoom_in", "zoom_out", "move_north", "move_northeast", "move_east",
    ]
    assert session.check_direction()["description"] == "Facing NW (330.0 degrees)"
    assert session.capture_view() == b"recorded JPEG"
    assert session._image_store.requests == [("start", 330.0, 15.0, 2.0)]


def test_first_link_in_cone_and_table_fallback(session):
    session.init_panorama(1, 2)
    result = session.move("north")
    assert result["available_moves"][-1] == "move_south"
    session.capture_view()
    assert session._image_store.requests[-1][0] == "north"
    session.move("south")
    session.move("northeast")
    assert not any(action.startswith("move_") for action in session.available_moves)


def test_unavailable_or_missing_target_keeps_current_panorama(session):
    session.init_panorama(1, 2)
    with pytest.raises(RuntimeError, match="No moves available"):
        session.move("south")
    with pytest.raises(RuntimeError, match="not found"):
        session.move("east")
    session.capture_view()
    assert session._image_store.requests[-1][0] == "start"


@pytest.mark.parametrize(
    "direction,delta,expected",
    [("right", 15, 0), ("right", -45, 60), ("left", "60", 300), ("right", 390, 30)],
)
def test_horizontal_rounding_and_wrap(session, direction, delta, expected):
    session.init_panorama(1, 2)
    session.scroll(direction, delta)
    session.capture_view()
    assert session._image_store.requests[-1][1] == expected


def test_scroll_and_zoom_clamp_without_fetching_intermediate_images(session):
    session.init_panorama(1, 2)
    session.scroll("up", 10000)
    session.zoom("in", 10000)
    assert session._image_store.requests == []
    session.capture_view()
    assert session._image_store.requests[-1] == ("start", 0.0, 30.0, 2.0)

    session.scroll("down", -10000)
    session.zoom("out", -10000)
    session.capture_view()
    assert session._image_store.requests[-1] == ("start", 0.0, -30.0, 0.0)


def test_huge_horizontal_delta_wraps_without_iterating(session):
    session.init_panorama(1, 2)
    session.scroll("right", 1e300)
    session.capture_view()
    heading = session._image_store.requests[-1][1]
    assert heading in range(0, 360, 30)


@pytest.mark.parametrize("delta", [None, "bad", float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("method,direction", [("scroll", "right"), ("zoom", "in")])
def test_invalid_delta_does_not_change_view(session, delta, method, direction):
    session.init_panorama(1, 2)
    with pytest.raises(ValueError, match="Invalid delta"):
        getattr(session, method)(direction, delta)
    session.capture_view()
    assert session._image_store.requests[-1] == ("start", 0.0, 0.0, 1.0)


@pytest.mark.parametrize(
    "kwargs", [{"heading": 10}, {"pitch": 90}, {"zoom": 3}, {"lat": "nan"}]
)
def test_invalid_scenario_does_not_partially_change_state(session, kwargs):
    session.init_panorama(1, 2)
    scenario = {"lat": 1.1, "lng": 2.0, **kwargs}
    with pytest.raises(ValueError):
        session.init_panorama(**scenario)
    session.capture_view()
    assert session._image_store.requests[-1][0] == "start"


def test_session_requires_initialization_and_close_clears_state(session):
    with pytest.raises(RuntimeError, match="No panorama loaded"):
        session.capture_view()
    session.init_panorama(1, 2)
    session.close()
    assert session.available_moves == []
    with pytest.raises(RuntimeError, match="No panorama loaded"):
        session.capture_view()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        session.init_panorama(1, 2)


def test_only_metadata_operations_open_connections_and_close_them(
    database, recorded_connections,
):
    session = StreetViewSession(database, ImageStore())
    assert recorded_connections == []

    session.init_panorama(1, 2)
    assert len(recorded_connections) == 1
    assert_connections_closed(recorded_connections)

    session.scroll("right", 30)
    session.zoom("in", 1)
    assert session.check_direction()["description"] == "Facing NE (30.0 degrees)"
    session.capture_view()
    assert session._image_store.requests[-1] == ("start", 30.0, 0.0, 2.0)
    assert len(recorded_connections) == 1

    session.move("north")
    assert session.available_moves[-1] == "move_south"
    assert len(recorded_connections) == 2
    assert_connections_closed(recorded_connections)
    session.capture_view()
    assert session._image_store.requests[-1] == ("north", 30.0, 0.0, 2.0)

    with pytest.raises(RuntimeError, match="No moves available"):
        session.move("east")
    assert len(recorded_connections) == 2
    session.close()
    assert_connections_closed(recorded_connections)


def test_missing_move_target_closes_connection_and_preserves_view(
    session, recorded_connections,
):
    session.init_panorama(1, 2)
    with pytest.raises(RuntimeError, match="not found"):
        session.move("east")
    assert len(recorded_connections) == 2
    assert_connections_closed(recorded_connections)
    session.capture_view()
    assert session._image_store.requests[-1][0] == "start"


@pytest.mark.parametrize(
    "setup,error,match",
    [
        ("DELETE FROM panoramas", RuntimeError, "No panoramas"),
        ("DROP TABLE panoramas", sqlite3.OperationalError, "no such table"),
        (
            "UPDATE panoramas SET metadata_json = '{\"links\": [{\"panoId\": \"north\"}]}'",
            KeyError,
            "heading",
        ),
    ],
)
def test_initialization_failures_close_metadata_connections(
    database, recorded_connections, setup, error, match,
):
    with closing(sqlite3.connect(database)) as conn:
        conn.execute(setup)
        conn.commit()
    recorded_connections.clear()

    session = StreetViewSession(database, ImageStore())
    with pytest.raises(error, match=match):
        session.init_panorama(1, 2)
    assert len(recorded_connections) == 1
    assert_connections_closed(recorded_connections)


def test_metadata_connections_are_read_only_and_close_after_write_failure(
    session, recorded_connections,
):
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        with session._database() as conn:
            conn.execute("DELETE FROM panoramas")
    assert_connections_closed(recorded_connections)
    session.init_panorama(1, 2)
    assert_connections_closed(recorded_connections)


def test_nonexistent_database_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        StreetViewSession(path, ImageStore())
    assert not path.exists()
