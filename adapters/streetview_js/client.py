"""Street View host client (HTTP) with tenacity retry."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import requests
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential_jitter,
    retry_if_exception,
    before_sleep_log,
)
from core.exceptions import HostTimeoutError, HostResponseError, MissingContextError
from core.utils.retry import is_retryable_http_error

logger = logging.getLogger(__name__)

# Configuration via environment
REQUEST_TIMEOUT = float(os.getenv("HOST_CLIENT_TIMEOUT", "30"))  # seconds
MAX_ATTEMPTS = int(os.getenv("HOST_CLIENT_MAX_ATTEMPTS", "3"))

class StreetViewHostClient:
    def __init__(self, host_url: Optional[str] = None) -> None:
        self.host_url = (
            host_url
            or os.getenv("STREETVIEW_HOST_URL", "http://localhost:13000")
        ).rstrip("/")
        self._http = requests.Session()
        self._http.headers.update({"Content-Type": "application/json"})

    # ------------------------------------------------------------------
    # Internal helpers with tenacity retry
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(MAX_ATTEMPTS),
        wait=wait_exponential_jitter(initial=1, max=30, jitter=2),
        retry=retry_if_exception(is_retryable_http_error),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )

    def _post(self, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        """Post with retry for timeout/connection errors."""
        url = f"{self.host_url}{path}"
        logger.debug("POST %s body=%s", url, body)
        try:
            resp = self._http.post(url, json=body or {}, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.exceptions.Timeout:
            logger.warning(f"Timeout on POST {path}")
            raise
        except requests.exceptions.ConnectionError as exc:
            logger.warning(f"Connection error on POST {path}: {exc}")
            raise
        except requests.exceptions.HTTPError as exc:
            logger.warning(f"HTTP error on POST {path}: {exc.response.status_code}")
            raise
        return self._unwrap(resp, path)

    @retry(
        stop=stop_after_attempt(MAX_ATTEMPTS),
        wait=wait_exponential_jitter(initial=1, max=30, jitter=2),
        retry=retry_if_exception(is_retryable_http_error),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )

    def _get(self, path: str) -> Any:
        """GET with retry for timeout/connection errors."""
        url = f"{self.host_url}{path}"
        logger.debug("GET %s", url)
        try:
            resp = self._http.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.exceptions.Timeout:
            logger.warning(f"Timeout on GET {path}")
            raise
        except requests.exceptions.ConnectionError as exc:
            logger.warning(f"Connection error on GET {path}: {exc}")
            raise
        except requests.exceptions.HTTPError as exc:
            logger.warning(f"HTTP error on GET {path}: {exc.response.status_code}")
            raise
        return self._unwrap(resp, path)

    def _unwrap(self, resp: requests.Response, method: str) -> Any:
        """Parse response - no retry here (parsing, not network)."""
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
    # Public API
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
