import json
import os
from typing import Any, Dict, List, Optional, Union
from copy import deepcopy
from unittest import result
import requests


DEFAULT_STATE = {
    "session_id": None,
}


class GeoGuessrAPI:
    """
    GeoGuessr Navigation API.
    """

    def __init__(self, base_url: Optional[str] = None):
        """Create a new GeoGuessr API client.

        Args:
            base_url (Optional[str]): Server URL. Falls back to
                GEOGUESSR_SERVER_URL env var or ``http://geoguessr-worker:8000``.
        """
        self.state: Dict[str, Any] = deepcopy(DEFAULT_STATE)
        self._base_url = (
            base_url
            or os.getenv("GEOGUESSR_SERVER_URL", "http://geoguessr-worker:8000")
        )
        self.session_id: Optional[str] = None
        self._session = requests.Session()
        self._timeout = (3.05, 30)
        self.long_context = False

    # ------------------------------------------------------------------
    #  helper functions
    # ------------------------------------------------------------------

    def _post(self, path: str, body: Optional[Dict] = None) -> Dict[str, Any]:
        """Send a request to the server to get POST.

        Args:
            path (str): URL path to append to the base URL.
            body (Optional[Dict]): JSON body payload. Defaults to `{}`.

        Returns:
            envelope (Dict[str, Any]): Parsed JSON response envelope from the server.
        """
        resp = self._session.post(
            f"{self._base_url}{path}",
            json=body or {},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def _get(self, path: str) -> Dict[str, Any]:
        """Send a GET request to the server.

        Args:
            path (str): URL path to append to the base URL.

        Returns:
            envelope (Dict[str, Any]): Parsed JSON response envelope from the server.
        """
        resp = self._session.get(
            f"{self._base_url}{path}",
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def _call(
        self, method: str, path: str, body: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Make an HTTP call, unwrap the server envelope, and sync local state.

        Args:
            method (str): HTTP method, either ``"GET"`` or ``"POST"``.
            path (str): URL path to append to the base URL.
            body (Optional[Dict]): JSON body for POST requests. Ignored for GET.

        Returns:
            updates (Dict[str, Any]): The ``updates`` dict extracted from the
                server response envelope.

        Raises:
            RuntimeError: On network errors or when the server returns ``ok=false``.
        """
        try:
            if method == "GET":
                envelope = self._get(path)
            else:
                envelope = self._post(path, body)
        except requests.RequestException as e:
            raise RuntimeError(str(e))
        except ValueError as e:
            raise RuntimeError(f"Server returned non-JSON response: {e}")

        if not envelope.get("ok"):
            msg = (envelope.get("error") or {}).get("message", "Unknown server error")
            raise RuntimeError(msg)

        updates = envelope.get("updates", {})
        return updates

    def _load_scenario(
        self,
        scenario: Dict[str, Union[Dict, str, int, float]],
        long_context: bool = False,
    ) -> None:
        """Load a scenario configuration into local state and the server.

        Args:
            scenario (Dict[str, Union[Dict, str, int, float]]): Configuration
                dict. Recognised keys are applied to ``AgentState``:
                - random_seed (int): RNG seed for episode ID generation.
                - image_root (str): Directory for captured images.
            long_context (bool): Whether to enable long context mode.
                Defaults to ``False``.
        """
        for key, value in scenario.items():
            if key in self.state:
                self.state[key] = value
        self.long_context = long_context
        # Forward to server
        self._call("POST", "/load_scenario", scenario) 

    def __eq__(self, value: object) -> bool:
        """Check equality based on agent state.

        Args:
            value (object): Object to compare against.

        Returns:
            is_equal (bool): ``True`` if *value* is a ``GeoGuessrAPI`` instance
                with the same session id and base URL.
        """
        if not isinstance(value, GeoGuessrAPI):
            return False
        return (
            self.state.get("session_id") == value.state.get("session_id")
            and self._base_url == value._base_url
        )

    def get_state(self) -> Dict[str, Any]:
        """Return the local cached state of the current panorama.

        Returns:
            - session_id (Optional[str]): Current session identifier.
        """
        return dict(self.state)

    def get_state_json(self) -> str:
        """Return the local cached state as a JSON string.

        Returns:
            state_json (str): JSON-serialised cached state dictionary.
        """
        return json.dumps(self.get_state(), default=str)

    # ------------------------------------------------------------------
    # Core Connection / Setup
    # ------------------------------------------------------------------

    def connect_host(
        self, api_key: Optional[str] = None, session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Start a Street View host session on the server.

        Args:
            api_key (Optional[str]): Google Maps API key. The server falls
                back to its own ``GOOGLE_MAPS_API_KEY`` env var when ``None``.
            session_id (Optional[str]): Desired session identifier. The server
                generates one automatically when ``None``.

        Returns:
            - session_id (str): Assigned session identifier.
        """
        body: Dict[str, Any] = {}
        if api_key:
            body["api_key"] = api_key
        if session_id:
            body["session_id"] = session_id
        result = self._call("POST", "/connect", body)
        # Pin session header so all subsequent requests route to this engine
        sid = result.get("session_id") or self.state.get("session_id")
        if sid:
            self._session.headers["X-Session-ID"] = sid
            self.state["session_id"] = sid
        return result

    def init_panorama(
        self,
        lat: float,
        lng: float,
        heading: float = 0.0,
        pitch: float = 0.0,
        zoom: float = 1.0,
    ) -> Dict[str, Any]:
        """Initialize the host panorama at a geographic location.

        Args:
            lat (float): Latitude in decimal degrees.
            lng (float): Longitude in decimal degrees.
            heading (float): Initial camera heading in degrees (0=North, 90=East).
                Defaults to ``0.0``.
            pitch (float): Initial camera pitch in degrees (0=horizon).
                Defaults to ``0.0``.
            zoom (float): Initial zoom level. Defaults to ``1.0``.

        Returns:
            - image_base64 (str): Base64-encoded panorama image.
            - available_moves (List[str]): Directions available from this pano.
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

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------

    def check_direction(self) -> Dict[str, Any]:
        """Check the current compass direction the camera is facing.

        Returns:
            - description (str): where is the direction facing at the current state(e.g. ``"Facing N (0.0 degrees)"``).
            - available_moves (List[str]): List of permitted action names.  
        """
        result = self._call("GET", "/check/direction")
        return result

    def check_available_moves(self) -> Dict[str, Any]:
        """Check which compass-direction moves are currently available.

        Returns:
            - available_moves (List[str]): List of permitted action names.
                Includes scroll, zoom actions and directional moves functions (N, NE, E, SE, S, SW, W, NW).
        """
        available_moves = self._call("GET", "/check/available_moves")
        return available_moves

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def capture_view(self) -> Dict[str, Any]:
        """Capture the current panorama image.

        Returns:
            - image_base64 (str): Base64-encoded panorama image.
            - available_moves (List[str]): Actions available at the current location.
        """
        result =  self._call("POST", "/capture/view")
        return result

    # ------------------------------------------------------------------
    # Movements
    # ------------------------------------------------------------------

    def move_north(self) -> Dict[str, Any]:
        """Move to the adjacent panorama in the North direction.

        Returns:
            - available_moves (List[str]): Actions available at the new location.
        """
        data = self._call("POST", "/move/north")
        return data

    def move_northeast(self) -> Dict[str, Any]:
        """Move to the adjacent panorama in the Northeast direction.

        Returns:
            - available_moves (List[str]): Actions available at the new location.
        """
        data = self._call("POST", "/move/northeast")
        return data

    def move_east(self) -> Dict[str, Any]:
        """Move to the adjacent panorama in the East direction.

        Returns:
            
            - available_moves (List[str]): Actions available at the new location.
        """
        data = self._call("POST", "/move/east")
        return data

    def move_southeast(self) -> Dict[str, Any]:
        """Move to the adjacent panorama in the Southeast direction.

        Returns:
            - available_moves (List[str]): Actions available at the new location.
        """
        data = self._call("POST", "/move/southeast")
        return data

    def move_south(self) -> Dict[str, Any]:
        """Move to the adjacent panorama in the South direction.

        Returns:
            - available_moves (List[str]): Actions available at the new location.
        """
        data = self._call("POST", "/move/south")
        return data

    def move_southwest(self) -> Dict[str, Any]:
        """Move to the adjacent panorama in the Southwest direction.

        Returns:
            - available_moves (List[str]): Actions available at the new location.
        """
        data = self._call("POST", "/move/southwest")
        return data

    def move_west(self) -> Dict[str, Any]:
        """Move to the adjacent panorama in the West direction.

        Returns:
            - available_moves (List[str]): Actions available at the new location.
        """
        data = self._call("POST", "/move/west")
        return data

    def move_northwest(self) -> Dict[str, Any]:
        """Move to the adjacent panorama in the Northwest direction.

        Returns:
            - available_moves (List[str]): Actions available at the new location.
        """
        data = self._call("POST", "/move/northwest")
        return data

    # ------------------------------------------------------------------
    # Scroll (camera rotation)
    # ------------------------------------------------------------------

    def scroll_left(self, deg: float) -> Dict[str, Any]:
        """Rotate the camera view to the left (counter-clockwise).

        Args:
            deg (float): Degrees to rotate left. Positive value expected;
            negative values are treated as their absolute value.

        Returns:
            - available_moves (List[str]): Actions available at the new location.
        """
        data = self._call("POST", "/scroll/left", {"delta": deg})
        return data

    def scroll_right(self, deg: float) -> Dict[str, Any]:
        """Rotate the camera view to the right (clockwise).

        Args:
            deg (float): Degrees to rotate right. Positive value expected;
            negative values are treated as their absolute value.

        Returns:
            - available_moves (List[str]): Actions available at the new location.
        """
        data = self._call("POST", "/scroll/right", {"delta": deg})
        return data

    def scroll_up(self, deg: float) -> Dict[str, Any]:
        """Tilt the camera view upward.

        Args:
            deg (float): Degrees to tilt up. Clamped so the resulting pitch
            does not exceed 90.

        Returns:
            - available_moves (List[str]): Actions available at the new location.
        """
        data = self._call("POST", "/scroll/up", {"delta": deg})
        return data

    def scroll_down(self, deg: float) -> Dict[str, Any]:
        """Tilt the camera view downward.

        Args:
            deg (float): Degrees to tilt down. Clamped so the resulting pitch
                does not go below -90.

        Returns:
            - available_moves (List[str]): Actions available at the new location.
        """
        data = self._call("POST", "/scroll/down", {"delta": deg})
        return data

    # ------------------------------------------------------------------
    # Zoom
    # ------------------------------------------------------------------

    def zoom_in(self, delta: float) -> Dict[str, Any]:
        """Zoom the camera view in (increase magnification).

        Args:
            delta (float): Zoom increment to add. Positive value expected;
                negative values are treated as their absolute value.

        Returns:
            - available_moves (List[str]): Actions available at the new location.
        """
        data = self._call("POST", "/zoom/in", {"delta": delta})
        return data

    def zoom_out(self, delta: float) -> Dict[str, Any]:
        """Zoom the camera view out (decrease magnification).

        Args:
            delta (float): Zoom decrement to subtract. Positive value expected;
                negative values are treated as their absolute value.
                The resulting zoom level is clamped at 0.

        Returns:
            - available_moves (List[str]): Actions available at the new location.
        """
        data = self._call("POST", "/zoom/out", {"delta": delta})
        return data

    # ------------------------------------------------------------------
    # Session Control
    # ------------------------------------------------------------------

    def end_session(self) -> Dict[str, Any]:
        """End the current session navigation state and reset local state.

        Returns:
            - step_count (int): Steps taken so far (before reset).
        """
        # If you rename the server route too, change this to "/end_session".
        result = self._call("POST", "/end_session")

        # Reset local cache to defaults, but keep session_id pinned in headers.
        sid = self.state.get("session_id")
        self.state = deepcopy(DEFAULT_STATE)
        self.state["session_id"] = sid
        return result
