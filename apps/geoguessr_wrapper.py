import base64
import json
import os
import logging
import time
import random
from typing import Any, Dict, List, Optional

import requests
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential_jitter,
    retry_if_exception,
    before_sleep_log,
    RetryCallState
)
logger = logging.getLogger(__name__)

def is_retryable_http_error(exception: BaseException) -> bool:
    """Check if http error is retryable (rate limit, server error, network)."""
    if isinstance(exception, requests.exceptions.HTTPError):
        status = exception.response.status_code if exception.response else None
        return status in {403, 429, 500, 502, 503, 504}
    
    if isinstance(exception, (
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
    )):
        return True
    return False
def get_retry_after_delay(retry_state: RetryCallState) -> float:
    """
    Check retry after header on 429 responses indicating how 
    long to wait before retrying.
    
    Returns:
        delay(float): a float number of seconds to wait, or None if not specified.

    """
    exc = retry_state.outcome.exception()

    # Try to extract Retry-after header
    if exc is not None and hasattr(exc, "response") and exc.response is not None:
        retry_after = exc.response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                delay  = float(retry_after)
                logger.info(f"Retry-after header: waiting{delay}")
                return min(delay, 60) # Cap at 60s
            except (TypeError, ValueError):
                pass
        
    # Fallback exponential backoff with jitter
    attempt = retry_state.attempt_number
    delay = min(2 ** attempt + random.uniform(0, 1), 30)
    logger.info(f"No valid retry-after header: waiting {delay:.2f}s (attempt {attempt})")
    return delay


class ImageResult:
    """
    Return type for functions that produce image output.

    Usage:
        from bfcl_eval.eval_checker.multi_turn_eval.func_source_code import ImageResult

        def fetch_image(self, url: str) -> ImageResult:
            # ... fetch image ...
            return ImageResult(base64_data, "image/jpeg")
    """

    def __init__(
        self,
        image_base64: str = "",
        image_bytes: bytes = b"",
        mime_type: str = "image/jpeg",
    ):
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

    def __init__(self, base_url: Optional[str] = None):
        """Create a new StreetView API client with retry and proper session lifecycle."""
        self._api_description = "This tool belongs to the StreetView API."
        self._base_url = base_url or os.getenv("GEOGUESSR_SERVER_URL", "http://127.0.0.1:8000")
        self._session = requests.Session()
        self._timeout = (15, 180)  # (connect timeout, read timeout)
        self.session_id: Optional[str] = None
        self.available_moves: List[str] = []

    # ------------------------------------------------------------------
    #  helper functions with tenacity retry
    # ------------------------------------------------------------------
    @retry(
        stop=stop_after_attempt(10),
        wait=get_retry_after_delay,
        retry=retry_if_exception(is_retryable_http_error),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
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
    
    @retry(
        stop=stop_after_attempt(10),
        wait=get_retry_after_delay,
        retry=retry_if_exception(is_retryable_http_error),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
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
    # ------------------------------------------------------------------
    # Session lifecycle 
    # ------------------------------------------------------------------
    def _connect_host(
        self,
        session_id: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Start a Street View host session on the server.

        Args:
            session_id (Optional[str]): Desired session identifier. The server
                generates one automatically when ``None``.

        Returns:
            - session_id (str): Assigned session identifier.
        """
        # Already connected, reuse unless explicitly requesting new session
        if self.session_id and not session_id:
            logger.debug(f"Reusing existing session: {self.session_id}")
            return {"session_id": self.session_id} 
        
        # If we have an old session and want to reconnect, close it first
        if self.session_id:
            logger.debug(f"Ending old session before connecting new: {self.session_id}")
            try:
                self._end_session()
            except Exception as e:
                logger.warning(f"Failed to end old session {self.session_id}: {e}")
        
        # create new session
        body: Dict[str, Any] = {}
        key = api_key or os.getenv("GOOGLE_MAPS_API_KEY")
        if key:
            body["api_key"] = key
        if session_id:
            body["session_id"] = session_id
        result = self._call("POST", "/connect", body)
        # Pin session header so all subsequent requests route to this engine
        sid = result.get("session_id") or self.session_id
        if sid:
            self._session.headers["X-Session-ID"] = sid
            self.session_id = sid
            logger.info(f"Connected to session: {sid}")
        return result

    def connect_host(
        self,
        api_key: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._connect_host(session_id=session_id, api_key=api_key)
    
    def _load_scenario(
        self,
        scenario: Dict[str, float],
        long_context: bool = False,
    ) -> Dict[str, Any]:
        """
        Set the starting coordinates for the scenario only when it not already connected.
        Args:
            scenario (Dict[str, float]): Configuration dict. Forwarded to the server.
        """
        if not self.session_id:
            self._connect_host()

        result = self._call("POST", "/init_panorama", scenario)
        self.available_moves = result.get("available_moves", [])
        return result

    def init_panorama(
        self,
        lat: float,
        lng: float,
        heading: float = 0.0,
        pitch: float = 0.0,
        zoom: float = 1.0,
    ) -> Dict[str, Any]:
        return self._load_scenario(
            {"lat": lat, "lng": lng, "heading": heading, "pitch": pitch, "zoom": zoom}
        )
    
    def _end_session(self) -> Dict[str, Any]:
        """End the current session on the server and clear local session id."""
        if not self.session_id:
            logger.debug("No session to end")
            return {"step_count": 0}
            
        try:
            result = self._call("POST", "/end_session")
        finally:
            # Always clear local state, even if server call fails
            old_sid = self.session_id
            self.session_id = None
            self._session.headers.pop("X-Session-ID", None)
            logger.info(f"Ended session: {old_sid}")
        return result

    def end_session(self) -> Dict[str, Any]:
        return self._end_session()

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
    # Context manager for guaranteed cleanup
    # ------------------------------------------------------------------

    def __enter__(self) -> "StreetViewAPI":
        return self
    
    def __exit__(self, exc_type, exc_value, traceback) -> None:
        """Guarantee session cleanup on exit."""
        if self.session_id:
            try:
                self._end_session()
            except Exception as e:
                logger.warning(f"Failed to end session {self.session_id} on exit: {e}")

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


    def check_available_moves(self) -> Dict[str, Any]:
        """
        Check which compass-direction moves are currently available.

        Returns:
            - available_moves (List[str]): List of permitted action names.
               Includes scroll, zoom actions and directional moves functions (N, NE, E, SE, S, SW, W, NW).
        """
        available_moves = self._call("GET", "/check/available_moves")
        self.available_moves = available_moves.get("available_moves", [])
        return available_moves

    def get_state(self) -> Dict[str, Any]:
        if not self.session_id:
            return {"session_id": None}
        updates = self._call("GET", "/state")
        return {"session_id": self.session_id, **updates}

    def get_state_json(self) -> str:
        return json.dumps(self.get_state(), default=str)

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
