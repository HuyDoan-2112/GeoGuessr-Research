"""Verify Tenacity retry works for rate limits and transient errors."""

import logging
from unittest.mock import patch, MagicMock

import requests

logging.basicConfig(level=logging.WARNING)


def _make_http_error(status_code: int) -> requests.exceptions.HTTPError:
    """Create an HTTPError with the given status code on its response."""
    resp = MagicMock(status_code=status_code)
    err = requests.exceptions.HTTPError(response=resp)
    return err


def test_image_retry_on_429():
    """fetch_with_tenacity retries on 429, then succeeds on the third call."""
    from core.utils.image_utils import fetch_with_tenacity

    call_count = [0]

    def mock_get(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] < 3:
            raise _make_http_error(429)
        resp = MagicMock(status_code=200, content=b"fake_image_data")
        resp.raise_for_status = MagicMock()  # no-op on 200
        return resp

    with patch("core.utils.image_utils._get_session") as mock_session_fn:
        session = MagicMock()
        session.get.side_effect = mock_get
        mock_session_fn.return_value = session

        data = fetch_with_tenacity("https://example.com/img.jpg", timeout=5)

    assert call_count[0] == 3, f"Expected 3 attempts, got {call_count[0]}"
    assert data == b"fake_image_data"


def test_host_client_retry_on_timeout():
    """StreetViewHostClient._post retries on Timeout, then succeeds."""
    from adapters.streetview_js.client import StreetViewHostClient

    client = StreetViewHostClient("http://localhost:3000")

    call_count = [0]

    def mock_post(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] < 3:
            raise requests.exceptions.Timeout("timeout")
        resp = MagicMock()
        resp.json.return_value = {"ok": True, "result": {"test": "data"}}
        resp.raise_for_status = MagicMock()  # no-op on success
        return resp

    with patch.object(client._http, "post", side_effect=mock_post):
        result = client._post("/test", {})

    assert call_count[0] == 3, f"Expected 3 attempts, got {call_count[0]}"
    assert result == {"test": "data"}


def test_no_retry_on_4xx():
    """Non-retryable errors (e.g. 404) propagate immediately."""
    from adapters.streetview_js.client import StreetViewHostClient

    client = StreetViewHostClient("http://localhost:3000")

    call_count = [0]

    def mock_post(*args, **kwargs):
        call_count[0] += 1
        resp = MagicMock()
        resp.raise_for_status.side_effect = _make_http_error(404)
        return resp

    with patch.object(client._http, "post", side_effect=mock_post):
        try:
            client._post("/test", {})
            assert False, "Should have raised"
        except requests.exceptions.HTTPError:
            pass

    assert call_count[0] == 1, f"Should not retry on 404, got {call_count[0]} attempts"


if __name__ == "__main__":
    test_image_retry_on_429()
    test_host_client_retry_on_timeout()
    test_no_retry_on_4xx()
