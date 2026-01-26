
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

import requests


@dataclass
class AgentState:
    """
    Local cache of the server-side navigation state.
    """

    # Configuration
    random_seed: int = 42
    image_root: str = "images"
    max_steps: int = 100
    session_id: Optional[str] = None

    # Episode Status
    episode_id: Optional[str] = None
    episode_active: bool = False
    step_count: int = 0

    # Navigation / Position
    pano_id: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    heading: float = 0.0
    pitch: float = 0.0
    zoom: float = 1.0

    # Context
    date: Optional[str] = None
    available_moves: List[str] = field(default_factory=list)

    # Outputs
    image_path: Optional[str] = None


class GeoGuessrAPI:
    """
    GeoGuessr Navigation API for LLM agents.

    Pure HTTP client that communicates with the GeoGuessr server
    running inside Docker.  All navigation logic, Street View interaction,
    and image capture happen server-side.
    """

    def __init__(self, base_url: Optional[str] = None):
        self.state = AgentState()
        self._base_url = (
            base_url
            or os.getenv("GEOGUESSR_SERVER_URL", "http://geoguessr-worker:8000")
        )
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

        self._api_description = (
            "This tool belongs to the GeoGuessr navigation system, which allows "
            "agents to explore Street View panoramas, navigate in compass directions, "
            "control the camera (scroll/zoom)"
        )
        self.long_context = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _post(self, path: str, body: Optional[Dict] = None) -> Dict[str, Any]:
        """POST to server, return parsed JSON envelope."""
        resp = self._session.post(f"{self._base_url}{path}", json=body or {})
        return resp.json()

    def _get(self, path: str) -> Dict[str, Any]:
        """GET from server, return parsed JSON envelope."""
        resp = self._session.get(f"{self._base_url}{path}")
        return resp.json()

    def _call(
        self, method: str, path: str, body: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Make an HTTP call, unwrap the server envelope, and sync local state.

        Returns only the ``updates`` dict on success.
        Raises ``RuntimeError`` on server or network errors.
        """
        try:
            if method == "GET":
                envelope = self._get(path)
            else:
                envelope = self._post(path, body)
        except requests.RequestException as e:
            raise RuntimeError(str(e))

        if not envelope.get("ok"):
            msg = (envelope.get("error") or {}).get("message", "Unknown server error")
            raise RuntimeError(msg)

        updates = envelope.get("updates", {})
        self._sync_state(updates)
        return updates

    def _sync_state(self, updates: Dict[str, Any]) -> None:
        """Update local AgentState cache from server response fields."""
        _FIELDS = (
            "session_id",
            "episode_id",
            "pano_id",
            "lat",
            "lng",
            "heading",
            "pitch",
            "zoom",
            "available_moves",
            "step_count",
            "image_path",
        )
        for key in _FIELDS:
            if key in updates:
                setattr(self.state, key, updates[key])


    def _load_scenario(
        self,
        scenario: Dict[str, Union[Dict, str, int, float]],
        long_context: bool = False,
    ) -> None:
        """
        Load a scenario configuration.

        Updates both local state and the server.

        Args:
            scenario: Configuration dict (random_seed, image_root, max_steps, …)
            long_context: Whether to enable long context mode.
        """
        for key, value in scenario.items():
            if hasattr(self.state, key):
                setattr(self.state, key, value)
        self.long_context = long_context
        # Forward to server
        self._call("POST", "/load_scenario", scenario)

    def __eq__(self, value: object) -> bool:
        if not isinstance(value, GeoGuessrAPI):
            return False
        return self.state == value.state

    def get_state(self) -> Dict[str, Any]:
        """Return the local cached state as a dict."""
        return self.state.__dict__

    def get_state_json(self) -> str:
        """Return the local cached state as a JSON string."""
        return json.dumps(self.get_state(), default=str)

    # ------------------------------------------------------------------
    # Core Connection / Setup
    # ------------------------------------------------------------------

    def connect_host(
        self, api_key: Optional[str] = None, session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Start a Street View host session on the server.

        Args:
            api_key: Google Maps API key (server falls back to its own env).
            session_id: Optional session identifier.

        Returns:
            ``{ok, updates: {session_id}, error}``
        """
        body: Dict[str, Any] = {}
        if api_key:
            body["api_key"] = api_key
        if session_id:
            body["session_id"] = session_id
        result = self._call("POST", "/connect", body)
        # Pin session header so all subsequent requests route to this engine
        sid = result.get("session_id") or self.state.session_id
        if sid:
            self._session.headers["X-Session-ID"] = sid
        return result

    def init_panorama(
        self,
        lat: float,
        lng: float,
        heading: float = 0.0,
        pitch: float = 0.0,
        zoom: float = 1.0,
    ) -> Dict[str, Any]:
        """
        Initialize the host panorama location.

        Returns:
            ``{ok, updates: {pano_id, lat, lng, heading, pitch, zoom, available_moves, image_path}, error}``
        """
        return self._call(
            "POST",
            "/init_panorama",
            {
                "lat": lat,
                "lng": lng,
                "heading": heading,
                "pitch": pitch,
                "zoom": zoom,
            },
        )

    def start_episode(self) -> Dict[str, Any]:
        """
        Start a new GeoGuessr episode.

        Returns:
            ``{ok, updates: {episode_id, pano_id, …, available_moves, image_path}, error}``
        """
        result = self._call("POST", "/start_episode")
        self.state.episode_active = True
        return result

    def get_episode_state(self) -> Dict[str, Any]:
        """
        Get the current episode state from the server.

        Returns:
            ``{ok, updates: {episode_id, pano_id, …, step_count, image_path}, error}``
        """
        return self._call("GET", "/episode")
    
    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------

    def check_direction(self) -> Dict[str, Any]:
        """
        Check the current facing direction.

        Returns:
            ``{ok, updates: {description}, error}``
        """
        return self._call("GET", "/check/direction")

    def check_available_moves(self) -> Dict[str, Any]:
        """
        Check which compass-direction moves are currently available.

        Returns:
            ``{ok, updates: {available_moves}, error}``
        """
        return self._call("GET", "/check/available_moves")


    # ------------------------------------------------------------------
    # Movements
    # ------------------------------------------------------------------

    def move_north(self) -> Dict[str, Any]:
        """
        Move to the panorama in the North direction.

        Returns:
            Dict with ``image_path`` and ``available_moves``.
        """
        return self._call("POST", "/move/north")

    def move_northeast(self) -> Dict[str, Any]:
        """
        Move to the panorama in the Northeast direction.

        Returns:
            Dict with ``image_path`` and ``available_moves``.
        """
        return self._call("POST", "/move/northeast")

    def move_east(self) -> Dict[str, Any]:
        """
        Move to the panorama in the East direction.

        Returns:
            Dict with ``image_path`` and ``available_moves``.
        """
        return self._call("POST", "/move/east")

    def move_southeast(self) -> Dict[str, Any]:
        """
        Move to the panorama in the Southeast direction.

        Returns:
            Dict with ``image_path`` and ``available_moves``.
        """
        return self._call("POST", "/move/southeast")

    def move_south(self) -> Dict[str, Any]:
        """
        Move to the panorama in the South direction.

        Returns:
            Dict with ``image_path`` and ``available_moves``.
        """
        return self._call("POST", "/move/south")

    def move_southwest(self) -> Dict[str, Any]:
        """
        Move to the panorama in the Southwest direction.

        Returns:
            Dict with ``image_path`` and ``available_moves``.
        """
        return self._call("POST", "/move/southwest")

    def move_west(self) -> Dict[str, Any]:
        """
        Move to the panorama in the West direction.

        Returns:
            Dict with ``image_path`` and ``available_moves``.
        """
        return self._call("POST", "/move/west")

    def move_northwest(self) -> Dict[str, Any]:
        """
        Move to the panorama in the Northwest direction.

        Returns:
            Dict with ``image_path`` and ``available_moves``.
        """
        return self._call("POST", "/move/northwest")

    # ------------------------------------------------------------------
    # Scroll (camera rotation)
    # ------------------------------------------------------------------

    def scroll_left(self, deg: float) -> Dict[str, Any]:
        """
        Rotate the camera view to the left (counter-clockwise).

        Args:
            deg: Degrees to rotate left (positive value).

        Returns:
            Dict with ``image_path``.
        """
        return self._call("POST", "/scroll/left", {"delta": deg})

    def scroll_right(self, deg: float) -> Dict[str, Any]:
        """
        Rotate the camera view to the right (clockwise).

        Args:
            deg: Degrees to rotate right (positive value).

        Returns:
            Dict with ``image_path``.
        """
        return self._call("POST", "/scroll/right", {"delta": deg})

    def scroll_up(self, deg: float) -> Dict[str, Any]:
        """
        Tilt the camera view upward.

        Args:
            deg: Degrees to tilt up (positive value, max 90).

        Returns:
            Dict with ``image_path``.
        """
        return self._call("POST", "/scroll/up", {"delta": deg})

    def scroll_down(self, deg: float) -> Dict[str, Any]:
        """
        Tilt the camera view downward.

        Args:
            deg: Degrees to tilt down (positive value).

        Returns:
            Dict with ``image_path``.
        """
        return self._call("POST", "/scroll/down", {"delta": deg})

    # ------------------------------------------------------------------
    # Zoom
    # ------------------------------------------------------------------

    def zoom_in(self, delta: float) -> Dict[str, Any]:
        """
        Zoom the camera view in (increase magnification).

        Args:
            delta: Zoom level to add (positive value).

        Returns:
            Dict with ``image_path``.
        """
        return self._call("POST", "/zoom/in", {"delta": delta})

    def zoom_out(self, delta: float) -> Dict[str, Any]:
        """
        Zoom the camera view out (decrease magnification).

        Args:
            delta: Zoom level to subtract (positive value).

        Returns:
            Dict with ``image_path``.
        """
        return self._call("POST", "/zoom/out", {"delta": delta})

    # ------------------------------------------------------------------
    # Episode Control
    # ------------------------------------------------------------------

    def end_episode(self) -> Dict[str, Any]:
        """
        End the current episode and reset state.

        Returns:
            ``{ok, updates: {episode_id, step_count}, error}``
        """
        result = self._call("POST", "/end_episode")
        self.state.episode_active = False
        self.state.episode_id = None
        self.state.pano_id = None
        self.state.lat = None
        self.state.lng = None
        self.state.heading = 0.0
        self.state.pitch = 0.0
        self.state.zoom = 1.0
        self.state.available_moves = []
        self.state.step_count = 0
        self.state.date = None
        self.state.image_path = None
        return result

