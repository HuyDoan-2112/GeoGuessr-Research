import json
import os
from pickle import NONE
from tkinter import NO
from typing import Any, Dict, List, Optional
import requests
# from bfcl_eval.eval_checker.multi_turn_eval.func_source_code import ImageResult

import base64


class ImageResult:
    """
    Return type for functions that produce image output.
    
    Usage:
        from bfcl_eval.eval_checker.multi_turn_eval.func_source_code import ImageResult
        
        def fetch_image(self, url: str) -> ImageResult:
            # ... fetch image ...
            return ImageResult(base64_data, "image/jpeg")
    """
    
    def __init__(self, image_base64: str="", image_bytes: bytes=b"", mime_type: str = "image/jpeg"):
        if image_base64 and image_bytes:
            self.image_base64 = image_base64
            self.image_bytes = image_bytes
        elif image_base64:
            self.image_base64 = image_base64
            self.image_bytes = base64.b64decode(image_base64)
        elif image_bytes:
            self.image_bytes = image_bytes
            self.image_base64 = base64.b64encode(image_bytes).decode("utf-8")
        else:
            raise ValueError("Either image_base64 or image_bytes must be provided")
        
        self.type = mime_type
    
    def to_dict(self) -> dict:
        return {
            "image_base64": self.image_base64,
            "image_bytes": self.image_bytes,
            "type": self.type,
        }

class StreetViewAPI:
    """
    StreetView API.
    """

    def __init__(self):
        """Create a new StreetView API client."""
        self._api_description = "This tool belongs to the StreetView API, which is used to navigate and capture street views."

        self._base_url = os.getenv("GEOGUESSR_SERVER_URL", "http://127.0.0.1:8000")
        self._session = requests.Session()
        self._timeout = (15, 6000)  # (connect timeout, read timeout)
        # The client only retains the session identifier. All other state lives
        # on the server and can be fetched via GET /state when needed.
        self.session_id: Optional[str] = None
        self.available_moves: List[str] = None

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

    def _call(self, method: str, path: str, body: Optional[Dict] = None) -> Dict[str, Any]:
        """Make an HTTP call and unwrap the server envelope.

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
        # Only keep track of the session id client-side.
        sid = updates.get("session_id")
        if sid:
            self.session_id = sid
            self._session.headers["X-Session-ID"] = sid
        return updates

    def _load_scenario(
        self,
        scenario: Dict[str, float],
        long_context: bool = False,
    ) -> None:
        """
        Set the starting coordinates for the scenario.
        Args:
            scenario (Dict[str, float]): Configuration dict. Forwarded to the server.
        """
        self._connect_host()

        result = self._call("POST", "/init_panorama", scenario)
        self.available_moves = result.get("available_moves", [])
        return result

    def __eq__(self, value: object) -> bool:
        """Check equality based on session identity.

        Args:
            value (object): Object to compare against.

        Returns:
            is_equal (bool): ``True`` if *value* is a ``StreetViewAPI`` instance
                with the same base URL and session id.
        """
        if not isinstance(value, StreetViewAPI):
            return False
        return (self._base_url, self.session_id) == (value._base_url, value.session_id)

    def _get_updated_tool_list(self) -> List[str]:
        return self.available_moves

    # ------------------------------------------------------------------
    # Core Connection / Setup
    # ------------------------------------------------------------------

    def _connect_host(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Start a Street View host session on the server.

        Args:
            session_id (Optional[str]): Desired session identifier. The server
                generates one automatically when ``None``.

        Returns:
            - session_id (str): Assigned session identifier.
        """
        body: Dict[str, Any] = {}
        if os.getenv("GOOGLE_MAPS_API_KEY"):
            body["api_key"] = os.getenv("GOOGLE_MAPS_API_KEY")
        if session_id:
            body["session_id"] = session_id
        result = self._call("POST", "/connect", body)
        # Pin session header so all subsequent requests route to this engine
        sid = result.get("session_id") or self.session_id
        if sid:
            self._session.headers["X-Session-ID"] = sid
            self.session_id = sid
        return result

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------

    def check_direction(self) -> Dict[str, Any]:
        """
        Check the current compass direction the camera is facing.

        Returns:
            - description (str): where is the direction facing at the current state(e.g. ``"Facing N (0.0 degrees)"``).
        """
        result = self._call("GET", "/check/direction")
        self.available_moves = result.get("available_moves", [])
        return {"description": result.get("description", "")}

    # def check_available_moves(self) -> Dict[str, Any]:
    #     """Check which compass-direction moves are currently available.

    #     Returns:
    #         - available_moves (List[str]): List of permitted action names.
    #             Includes scroll, zoom actions and directional moves functions (N, NE, E, SE, S, SW, W, NW).
    #     """
    #     available_moves = self._call("GET", "/check/available_moves")
    #     return available_moves

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def capture_view(self) -> Dict[str, Any]:
        """
        Capture the current panorama image. Returns an image of the current panorama.
        """
        result = self._call("POST", "/capture/view")
        return ImageResult(
            image_base64=result.get("image_base64", ""), mime_type="image/jpeg"
        )

    # ------------------------------------------------------------------
    # Movements
    # ------------------------------------------------------------------

    def move_north(self) -> Dict[str, Any]:
        """
        Move to the adjacent panorama in the North direction.

        Returns:
            status (bool): True if the operation is successful, False otherwise.
        """
        result = self._call("POST", "/move/north")
        self.available_moves = result.get("available_moves", [])
        return {"status": "success"}

    def move_northeast(self) -> Dict[str, Any]:
        """
        Move to the adjacent panorama in the Northeast direction.

        Returns:
            status (bool): True if the operation is successful, False otherwise.
        """
        result = self._call("POST", "/move/northeast")
        self.available_moves = result.get("available_moves", [])
        return {"status": "success"}

    def move_east(self) -> Dict[str, Any]:
        """
        Move to the adjacent panorama in the East direction.

        Returns:
            status (bool): True if the operation is successful, False otherwise.
        """
        result = self._call("POST", "/move/east")
        self.available_moves = result.get("available_moves", [])
        return {"status": "success"}

    def move_southeast(self) -> Dict[str, Any]:
        """
        Move to the adjacent panorama in the Southeast direction.

        Returns:
            status (bool): True if the operation is successful, False otherwise.
        """
        result = self._call("POST", "/move/southeast")
        self.available_moves = result.get("available_moves", [])
        return {"status": "success"}

    def move_south(self) -> Dict[str, Any]:
        """
        Move to the adjacent panorama in the South direction.

        Returns:
            status (bool): True if the operation is successful, False otherwise.
        """
        result = self._call("POST", "/move/south")
        self.available_moves = result.get("available_moves", [])
        return {"status": "success"}

    def move_southwest(self) -> Dict[str, Any]:
        """
        Move to the adjacent panorama in the Southwest direction.

        Returns:
            status (bool): True if the operation is successful, False otherwise.
        """
        result = self._call("POST", "/move/southwest")
        self.available_moves = result.get("available_moves", [])
        return {"status": "success"}

    def move_west(self) -> Dict[str, Any]:
        """
        Move to the adjacent panorama in the West direction.

        Returns:
            status (bool): True if the operation is successful, False otherwise.
        """
        result = self._call("POST", "/move/west")
        self.available_moves = result.get("available_moves", [])
        return {"status": "success"}

    def move_northwest(self) -> Dict[str, Any]:
        """
        Move to the adjacent panorama in the Northwest direction.

        Returns:
            status (bool): True if the operation is successful, False otherwise.
        """
        result = self._call("POST", "/move/northwest")
        self.available_moves = result.get("available_moves", [])
        return {"status": "success"}

    # ------------------------------------------------------------------
    # Scroll (camera rotation)
    # ------------------------------------------------------------------

    def scroll_left(self, deg: float) -> Dict[str, Any]:
        """
        Rotate the camera view to the left (counter-clockwise). Returns an image of the view.

        Args:
            deg (float): Degrees to rotate left. Positive value expected; negative values are treated as their absolute value.
        """
        result = self._call("POST", "/scroll/left", {"delta": deg})
        self.available_moves = result.get("available_moves", [])
        return self.capture_view()

    def scroll_right(self, deg: float) -> Dict[str, Any]:
        """
        Rotate the camera view to the right (clockwise). Returns an image of the view.

        Args:
            deg (float): Degrees to rotate right. Positive value expected; negative values are treated as their absolute value.
        """
        result = self._call("POST", "/scroll/right", {"delta": deg})
        self.available_moves = result.get("available_moves", [])
        return self.capture_view()

    def scroll_up(self, deg: float) -> Dict[str, Any]:
        """
        Tilt the camera view upward. Returns an image of the view.

        Args:
            deg (float): Degrees to tilt up. Clamped so the resulting pitch does not exceed 90.
        """
        result = self._call("POST", "/scroll/up", {"delta": deg})
        self.available_moves = result.get("available_moves", [])
        return self.capture_view()

    def scroll_down(self, deg: float) -> Dict[str, Any]:
        """
        Tilt the camera view downward. Returns an image of the view.

        Args:
            deg (float): Degrees to tilt down. Clamped so the resulting pitch does not go below -90.
        """
        result = self._call("POST", "/scroll/down", {"delta": deg})
        self.available_moves = result.get("available_moves", [])
        return self.capture_view()

    # ------------------------------------------------------------------
    # Zoom
    # ------------------------------------------------------------------

    def zoom_in(self, delta: float) -> Dict[str, Any]:
        """
        Zoom the camera view in (increase magnification). Returns an image of the view.

        Args:
            delta (float): Zoom increment to add. Positive value expected; negative values are treated as their absolute value.
        """
        result = self._call("POST", "/zoom/in", {"delta": delta})
        self.available_moves = result.get("available_moves", [])
        return self.capture_view()

    def zoom_out(self, delta: float) -> Dict[str, Any]:
        """
        Zoom the camera view out (decrease magnification). Returns an image of the view.

        Args:
            delta (float): Zoom decrement to subtract. Positive value expected; negative values are treated as their absolute value. The resulting zoom level is clamped at 0.
        """
        result = self._call("POST", "/zoom/out", {"delta": delta})
        self.available_moves = result.get("available_moves", [])
        return self.capture_view()

    # ------------------------------------------------------------------
    # Session Control
    # ------------------------------------------------------------------

    def _end_session(self) -> Dict[str, Any]:
        """End the current session on the server and clear local session id."""
        result = self._call("POST", "/end_session")
        # Server drops the session. Clear client-side session routing info.
        self.session_id = None
        self._session.headers.pop("X-Session-ID", None)
        return result
