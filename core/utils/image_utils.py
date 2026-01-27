import os
import random
import threading
import time
from io import BytesIO
from typing import Optional

import requests
from PIL import Image
from requests.adapters import HTTPAdapter


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


def _get_session() -> requests.Session:
    session = getattr(_SESSION_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _SESSION_LOCAL.session = session
    return session


def _get_with_retries(url: str, timeout: float) -> bytes:
    max_attempts = max(1, int(os.getenv("IMAGE_FETCH_MAX_ATTEMPTS", "4")))
    backoff = float(os.getenv("IMAGE_FETCH_BACKOFF_SECS", "0.6"))
    jitter = float(os.getenv("IMAGE_FETCH_JITTER_SECS", "0.2"))
    session = _get_session()
    last_exc: Optional[Exception] = None

    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.content
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response else None
            if status is not None and 500 <= status < 600:
                last_exc = exc
            else:
                raise
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
        if attempt < max_attempts:
            delay = backoff * (2 ** (attempt - 1)) + random.uniform(0, jitter)
            time.sleep(delay)

    if last_exc:
        raise last_exc
    raise RuntimeError("image_fetch_failed")


def fetch_image(
    pano_id,
    heading,
    pitch=0,
    zoom=None,
    size="640x640",
    fov=None,
) -> bytes:
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

    timeout = float(os.getenv("IMAGE_FETCH_TIMEOUT_SECS", "5"))
    return _get_with_retries(url, timeout=timeout)

def crop_google_logo(img_bytes: bytes, trim_bottom: int = 60) -> Image.Image:
    """Crop pixels off the bottom to remove the Google logo."""
    with Image.open(BytesIO(img_bytes)) as img:
        img.load()
        w, h = img.size
        new_h = max(h - int(trim_bottom), 1)
        return img.crop((0, 0, w, new_h)).copy()
