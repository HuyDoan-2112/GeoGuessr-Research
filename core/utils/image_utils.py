import base64
import hashlib
import hmac
import logging
import os
import random
import threading
import time
from io import BytesIO
from typing import Optional
from urllib.parse import urlparse

import requests
from PIL import Image
from requests.adapters import HTTPAdapter
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential_jitter,
    retry_if_exception,
    before_sleep_log,
)

from core.utils.retry import is_retryable_http_error

logger = logging.getLogger(__name__)

def zoom_to_fov(zoom, default=90):
    if zoom is None:
        return default
    try:
        z = float(zoom)
    except (TypeError, ValueError):
        return default
    
    fov = int(round(180.0 / (2**z)))
    return max(10, min(120, fov))

_SESSION_LOCAL = threading.local()

# Semaphore to limit concurrent image fetches (prevent 429 storms)
_max_concurrent = (
    os.getenv("IMAGE_FETCH_MAX_CONCURRENT")
    or os.getenv("IMAGE_FETCH_CONCURRENCY")
    or "8"
)
IMAGE_FETCH_SEMAPHORE = threading.Semaphore(int(_max_concurrent))

def _get_session() -> requests.Session:
    session = getattr(_SESSION_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _SESSION_LOCAL.session = session
    return session
@retry(
    stop=stop_after_attempt(int(os.getenv("IMAGE_FETCH_MAX_ATTEMPTS", "6"))),
    wait=wait_exponential_jitter(
        initial=float(os.getenv("IMAGE_FETCH_BACKOFF_SECS", "1.0")),
        max=60,
        jitter=float(os.getenv("IMAGE_FETCH_JITTER_SECS", "5")),
    ),
    retry=retry_if_exception(is_retryable_http_error),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)

def fetch_with_tenacity(url: str, timeout: float) -> bytes:
    """Fetch URL with Tenacity retry for rate limits and transient errors."""
    session = _get_session()
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def _sign_url(url: str, signing_secret: str) -> str:
    """Append a Google Maps URL signature using HMAC-SHA1."""
    parsed = urlparse(url)
    url_to_sign = parsed.path + "?" + parsed.query
    decoded_key = base64.urlsafe_b64decode(signing_secret)
    signature = hmac.new(decoded_key, url_to_sign.encode("utf-8"), hashlib.sha1)
    encoded_sig = base64.urlsafe_b64encode(signature.digest()).decode("utf-8")
    return url + "&signature=" + encoded_sig


def fetch_image(
    pano_id,
    heading,
    pitch=0,
    zoom=None,
    size="640x640",
    fov=None,
) -> bytes:
    """Fetch Street View image with rate limit protection."""
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_MAPS_API_KEY environment variable is not set")

    h = float(heading) if heading is not None else 0.0
    p = float(pitch) if pitch is not None else 0.0
    if fov is None:
        fov = zoom_to_fov(zoom)

    url = (
        "https://maps.googleapis.com/maps/api/streetview"
        f"?size={size}&pano={pano_id}"
        f"&heading={h:.2f}&pitch={p:.2f}&fov={fov}"
        f"&key={api_key}"
    )

    signing_secret = os.getenv("GOOGLE_MAPS_URL_SIGNING_SECRET")
    if signing_secret:
        url = _sign_url(url, signing_secret)

    timeout = float(os.getenv("IMAGE_FETCH_TIMEOUT_SECS", "10"))
    
    with IMAGE_FETCH_SEMAPHORE:
        return fetch_with_tenacity(url, timeout=timeout)

def crop_google_logo(img_bytes: bytes, trim_bottom: int = 60) -> Image.Image:
    """Crop pixels off the bottom to remove the Google logo."""
    with Image.open(BytesIO(img_bytes)) as img:
        img.load()
        w, h = img.size
        new_h = max(h - int(trim_bottom), 1)
        return img.crop((0, 0, w, new_h)).copy()
