from __future__ import annotations

import os
import re
from pathlib import Path

from openai import OpenAI

def build_filename(pano_id, heading, pitch, zoom, ext=".jpg", step=None):
    h = float(heading) if heading is not None else 0.0
    p = float(pitch) if pitch is not None else 0.0
    z = "na" if zoom is None else str(zoom)
    safe_pano = re.sub(r"[^A-Za-z0-9_-]", "_", str(pano_id))
    prefix = f"step_{step:04d}_" if step is not None else ""
    return f"{prefix}pano_{safe_pano}_h{h:.1f}_p{p:.1f}_z{z}{ext}"

def save_image(
    img,
    root_dir,
    session_id,
    pano_id,
    heading,
    pitch,
    zoom,
    overwrite=False,
    step=None,
) -> str:
    session_dir = os.path.join(root_dir, session_id)
    os.makedirs(session_dir, exist_ok=True)

    filename = build_filename(pano_id, heading, pitch, zoom, step=step)
    path = os.path.join(session_dir, filename)

    if not overwrite:
        if os.path.exists(path):
            base, ext = os.path.splitext(filename)
            i = 1
            while os.path.exists(path):
                path = os.path.join(session_dir, f"{base}_{i}{ext}")
                i += 1
    

    img.save(path, format="JPEG", quality=95)
    return path

