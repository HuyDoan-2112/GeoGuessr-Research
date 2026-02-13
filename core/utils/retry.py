"""Shared retry predicates for tenacity-based HTTP retries."""

from __future__ import annotations

import requests


#: HTTP status codes worth retrying (rate-limit + server errors).
RETRYABLE_STATUS_CODES = {403, 429, 500, 502, 503, 504}


def is_retryable_http_error(exception: BaseException) -> bool:
    """Return True if *exception* is a transient HTTP/network error.

    Retries on:
        - ``requests.exceptions.Timeout``
        - ``requests.exceptions.ConnectionError``
        - ``requests.exceptions.SSLError``
        - ``requests.exceptions.HTTPError`` with status in
          :data:`RETRYABLE_STATUS_CODES`
    """
    if isinstance(exception, (
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
        requests.exceptions.SSLError,
    )):
        return True

    if isinstance(exception, requests.exceptions.HTTPError):
        status = exception.response.status_code if exception.response else None
        return status in RETRYABLE_STATUS_CODES

    return False
