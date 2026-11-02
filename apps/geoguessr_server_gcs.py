"""SQLite/GCS Street View service for BFCL's street_view.py HTTP client."""

import base64
import logging
import math
import os
import threading
import uuid
from dataclasses import dataclass, field
from functools import wraps
from io import BytesIO
from pathlib import Path
from time import monotonic

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from PIL import Image

from apps.image_store import ImageStore
from apps.street_view import StreetViewSession

log = logging.getLogger(__name__)


@dataclass
class Session:
    api: StreetViewSession
    last_used: float
    lock: threading.Lock = field(default_factory=threading.Lock)


def _encode_image(data: bytes, crop_mode: str) -> str:
    if crop_mode != "off":
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            box = (0, 0, width, height // 2) if crop_mode == "top" else (
                0, height // 2, width, height
            )
            with BytesIO() as output:
                image.crop(box).save(output, format="JPEG", quality=95)
                data = output.getvalue()
    return base64.b64encode(data).decode("ascii")


def create_app(config=None) -> Flask:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    app = Flask(__name__, static_folder=None)
    app.config.update(
        DB_PATH=os.getenv("MAPCRUNCH_DB_PATH"),
        GCS_BUCKET=os.getenv("MAPCRUNCH_GCS_BUCKET"),
        GCS_PREFIX=os.getenv("MAPCRUNCH_GCS_PREFIX", "mapcrunch_images/"),
        CACHE_DIR=os.getenv("MAPCRUNCH_CACHE_DIR", "/tmp/mapcrunch_cache"),
        CACHE_SIZE_GB=float(os.getenv("MAPCRUNCH_CACHE_SIZE_GB", "5")),
        CROP_MODE=os.getenv("GEOGUESSR_CROP", "off").strip().lower(),
        SESSION_IDLE_TIMEOUT=float(os.getenv("SESSION_IDLE_TIMEOUT", "43200")),
    )
    app.config.update(config or {})
    db_path = app.config["DB_PATH"]
    if not db_path or not Path(db_path).is_file():
        raise ValueError("MAPCRUNCH_DB_PATH must point to an existing SQLite metadata database")
    crop_mode = app.config["CROP_MODE"]
    if crop_mode not in {"off", "top", "bottom"}:
        raise ValueError("GEOGUESSR_CROP must be off, top, or bottom")
    idle_timeout = float(app.config["SESSION_IDLE_TIMEOUT"])
    if not math.isfinite(idle_timeout) or idle_timeout <= 0:
        raise ValueError("SESSION_IDLE_TIMEOUT must be finite and positive")
    image_store = app.config.get("IMAGE_STORE")
    if image_store is None:
        if not app.config["GCS_BUCKET"]:
            raise ValueError("MAPCRUNCH_GCS_BUCKET is required")
        image_store = ImageStore(
            app.config["GCS_BUCKET"],
            gcs_prefix=app.config["GCS_PREFIX"],
            cache_dir=app.config["CACHE_DIR"],
            cache_size_gb=app.config["CACHE_SIZE_GB"],
        )

    sessions = {}
    sessions_lock = threading.Lock()
    app.extensions["street_view_sessions"] = sessions

    def cleanup_idle_sessions():
        with sessions_lock:
            candidates = list(sessions.items())
        for sid, session in candidates:
            # An in-flight request is active even if it takes longer than the timeout.
            if not session.lock.acquire(blocking=False):
                continue
            try:
                with sessions_lock:
                    if sessions.get(sid) is not session:
                        continue
                    if monotonic() - session.last_used < idle_timeout:
                        continue
                    sessions.pop(sid)
                session.api.close()
            finally:
                session.lock.release()

    def ok(updates=None):
        return jsonify({"ok": True, "updates": updates or {}, "error": {}})

    def fail(message, status=400):
        return jsonify({"ok": False, "updates": {}, "error": {"message": message}}), status

    def body():
        data = request.get_json(force=True, silent=True) or {}
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        return data

    def with_session(fn):
        @wraps(fn)
        def handle(*args, **kwargs):
            sid = request.headers.get("X-Session-ID")
            with sessions_lock:
                session = sessions.get(sid)
            if session is None:
                return fail("Unknown session — call /connect first")
            with session.lock:
                # Teardown may have completed while this request waited.
                with sessions_lock:
                    if sessions.get(sid) is not session:
                        return fail("Unknown session — call /connect first")
                try:
                    return ok(fn(session.api, *args, **kwargs))
                except (ValueError, RuntimeError) as error:
                    return fail(str(error))
                except Exception as error:
                    log.exception("Street View request failed")
                    return fail(str(error), 500)
                finally:
                    session.last_used = monotonic()
        return handle

    @app.post("/connect")
    def connect():
        # BFCL accepts the returned ID; Google Maps credentials are unnecessary.
        cleanup_idle_sessions()
        try:
            api = StreetViewSession(db_path, image_store)
        except Exception as error:
            log.exception("Failed to create Street View session")
            return fail(str(error), 500)
        sid = uuid.uuid4().hex
        with sessions_lock:
            sessions[sid] = Session(api, last_used=monotonic())
        return ok({"session_id": sid})

    @app.post("/init_panorama")
    @with_session
    def init_panorama(api):
        data = body()
        return api.init_panorama(
            lat=data.get("lat", 0.0),
            lng=data.get("lng", 0.0),
            heading=data.get("heading", 0.0),
            pitch=data.get("pitch", 0.0),
            zoom=data.get("zoom", 1.0),
        )

    @app.get("/check/direction")
    @with_session
    def check_direction(api):
        return api.check_direction()

    @app.post("/capture/view")
    @with_session
    def capture_view(api):
        return {"image_base64": _encode_image(api.capture_view(), crop_mode)}

    @app.post("/move/<direction>")
    @with_session
    def move(api, direction):
        return api.move(direction)

    @app.post("/scroll/<direction>")
    @with_session
    def scroll(api, direction):
        return api.scroll(direction, body().get("delta", 0))

    @app.post("/zoom/<direction>")
    @with_session
    def zoom(api, direction):
        return api.zoom(direction, body().get("delta", 0))

    @app.post("/end_session")
    @with_session
    def end_session(api):
        with sessions_lock:
            sessions.pop(request.headers["X-Session-ID"])
        api.close()
        return {}

    return app


if __name__ == "__main__":
    from waitress import serve

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = create_app()
    serve(
        app,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "18000")),
        threads=int(os.getenv("SERVER_THREADS", "8")),
        connection_limit=int(os.getenv("SERVER_CONNECTION_LIMIT", "100")),
    )
