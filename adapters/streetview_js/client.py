"""Street View host client (HTTP)."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import requests

from core.exceptions import HostTimeoutError, HostResponseError, MissingContextError

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30  # seconds


class StreetViewHostClient:
    def __init__(self, host_url: Optional[str] = None) -> None:
        self.host_url = (
            host_url
            or os.getenv("STREETVIEW_HOST_URL", "http://localhost:3000")
        ).rstrip("/")
        self._http = requests.Session()
        self._http.headers.update({"Content-Type": "application/json"})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _post(self, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.host_url}{path}"
        logger.debug("POST %s body=%s", url, body)
        try:
            resp = self._http.post(url, json=body or {}, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.Timeout:
            raise HostTimeoutError(method=path, timeout=REQUEST_TIMEOUT, req_id=0)
        except requests.exceptions.ConnectionError as exc:
            raise HostResponseError(error=f"connection_error: {exc}", method=path)
        return self._unwrap(resp, path)

    def _get(self, path: str) -> Any:
        url = f"{self.host_url}{path}"
        logger.debug("GET %s", url)
        try:
            resp = self._http.get(url, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.Timeout:
            raise HostTimeoutError(method=path, timeout=REQUEST_TIMEOUT, req_id=0)
        except requests.exceptions.ConnectionError as exc:
            raise HostResponseError(error=f"connection_error: {exc}", method=path)
        return self._unwrap(resp, path)

    def _unwrap(self, resp: requests.Response, method: str) -> Any:
        try:
            payload = resp.json()
        except ValueError:
            raise HostResponseError(error="invalid_json_response", method=method)
        if not payload.get("ok"):
            raise HostResponseError(
                error=payload.get("error") or "unknown",
                method=method,
            )
        return payload.get("result")

    # ------------------------------------------------------------------
    # Public API  (same signatures as the old JSONL client)
    # ------------------------------------------------------------------

    def start(self, session_id: str, api_key: Optional[str] = None) -> Any:
        if not session_id:
            raise MissingContextError("session_id")
        params: Dict[str, Any] = {}
        if api_key:
            params["apiKey"] = api_key
        # Ensure host is started with API key
        self._post("/start", params)
        # Acquire a page from the pool
        return self._post("/session/create", {"sessionId": session_id})

    def init(
        self,
        session_id: str,
        lat: float,
        lng: float,
        heading: float = 0.0,
        pitch: float = 0.0,
        zoom: float = 1.0,
    ) -> Any:
        if not session_id:
            raise MissingContextError("session_id")
        return self._post(
            f"/session/{session_id}/init",
            {"lat": lat, "lng": lng, "heading": heading, "pitch": pitch, "zoom": zoom},
        )

    def get_state(self, session_id: str) -> Any:
        if not session_id:
            raise MissingContextError("session_id")
        return self._get(f"/session/{session_id}/getState")

    def set_pov(
        self,
        session_id: str,
        heading: Optional[float] = None,
        pitch: Optional[float] = None,
        zoom: Optional[float] = None,
    ) -> Any:
        if not session_id:
            raise MissingContextError("session_id")
        return self._post(
            f"/session/{session_id}/setPov",
            {"heading": heading, "pitch": pitch, "zoom": zoom},
        )

    def set_pano(self, session_id: str, pano_id: str) -> Any:
        if not session_id:
            raise MissingContextError("session_id")
        return self._post(f"/session/{session_id}/setPano", {"panoId": pano_id})

    def set_position(self, session_id: str, lat: float, lng: float) -> Any:
        if not session_id:
            raise MissingContextError("session_id")
        return self._post(
            f"/session/{session_id}/setPosition", {"lat": lat, "lng": lng}
        )

    def wait_for_stable(
        self,
        session_id: str,
        timeoutMs: int = 1500,
        debounceMs: int = 200,
    ) -> Any:
        if not session_id:
            raise MissingContextError("session_id")
        return self._post(
            f"/session/{session_id}/waitForStable",
            {"timeoutMs": timeoutMs, "debounceMs": debounceMs},
        )

    def close_session(self, session_id: str) -> Any:
        if not session_id:
            raise MissingContextError("session_id")
        return self._post(f"/session/{session_id}/closeSession", {})

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "StreetViewHostClient":
        return self

    def __exit__(self, exc_type, exc_val, tb) -> None:
        self.close()
