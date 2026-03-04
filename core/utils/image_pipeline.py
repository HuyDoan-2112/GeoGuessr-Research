from __future__ import annotations

from typing import Any, Dict, Optional

from core.utils.image_store import save_image
from core.utils.image_utils import crop_google_logo, fetch_image
import base64

from io import BytesIO

def _encode_jpeg_base64(img) -> str:
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return base64.b64encode(buf.getvalue()).decode("utf-8")

def _capture_and_save(
    state: Dict[str, Any],
    session_id: str,
    root_dir: str,
    step: int | None = None,
    trim_bottom: int = 60,
    size: str = "640x640",
    api_key: Optional[str] = None,
    signing_secret: Optional[str] = None,
) -> tuple[Any, str]:
    pano_id = state["panoId"]
    pov = state["pov"]
    heading = pov.get("heading", 0.0)
    pitch = pov.get("pitch", 0.0)
    zoom = pov.get("zoom", 1.0)

    img_bytes = fetch_image(
        pano_id=pano_id,
        heading=heading,
        pitch=pitch,
        zoom=zoom,
        size=size,
        api_key=api_key,
        signing_secret=signing_secret,
    )
    cropped = crop_google_logo(img_bytes, trim_bottom=trim_bottom)
    path = save_image(
        cropped,
        root_dir=root_dir,
        session_id=session_id,
        pano_id=pano_id,
        heading=heading,
        pitch=pitch,
        zoom=zoom,
        step=step,
    )
    return cropped, path


def capture_state_image(
    state: Dict[str, Any],
    session_id: str,
    root_dir: str,
    step: int | None = None,
    trim_bottom: int = 60,
    size: str = "640x640",
    api_key: Optional[str] = None,
    signing_secret: Optional[str] = None,
) -> str:
    _, path = _capture_and_save(
        state,
        session_id=session_id,
        root_dir=root_dir,
        step=step,
        trim_bottom=trim_bottom,
        size=size,
        api_key=api_key,
        signing_secret=signing_secret,
    )
    return path


def capture_state_image_base64(
    state: Dict[str, Any],
    session_id: str,
    root_dir: str,
    step: int | None = None,
    trim_bottom: int = 60,
    size: str = "640x640",
    api_key: Optional[str] = None,
    signing_secret: Optional[str] = None,
) -> tuple[str, str]:
    cropped, path = _capture_and_save(
        state,
        session_id=session_id,
        root_dir=root_dir,
        step=step,
        trim_bottom=trim_bottom,
        size=size,
        api_key=api_key,
        signing_secret=signing_secret,
    )
    return _encode_jpeg_base64(cropped), path

