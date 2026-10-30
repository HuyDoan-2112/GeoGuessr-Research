"""Fetch pre-rendered Street View JPEGs from GCS with a shared disk cache."""

import hashlib
import math
import os
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path


class _DiskImageCache:
    """Bounded LRU cache; atomic files survive restarts and concurrent readers."""

    def __init__(self, directory: str, max_bytes: int):
        self.directory = Path(directory).expanduser() / "bfcl-images"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        entries = []
        for path in self.directory.glob("*.jpg"):
            try:
                stat = path.stat()
                entries.append((stat.st_mtime_ns, path, stat.st_size))
            except FileNotFoundError:
                continue
        self._entries = OrderedDict((path, size) for _, path, size in sorted(entries))
        self._size = sum(self._entries.values())
        self._evict()

    def _path(self, key: str) -> Path:
        return self.directory / (hashlib.sha256(key.encode()).hexdigest() + ".jpg")

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        with self._lock:
            try:
                data = path.read_bytes()
                path.touch()
            except FileNotFoundError:
                self._size -= self._entries.pop(path, 0)
                return None
            self._size -= self._entries.pop(path, 0)
            self._entries[path] = len(data)
            self._size += len(data)
            return data

    def put(self, key: str, data: bytes) -> None:
        if len(data) > self.max_bytes:
            return
        path = self._path(key)
        with self._lock:
            # A unique name also makes replacement safe across server processes.
            with tempfile.NamedTemporaryFile(dir=self.directory, suffix=".tmp", delete=False) as tmp:
                temp_path = Path(tmp.name)
                try:
                    tmp.write(data)
                    tmp.close()
                    os.replace(temp_path, path)
                finally:
                    temp_path.unlink(missing_ok=True)
            self._size -= self._entries.pop(path, 0)
            self._entries[path] = len(data)
            self._size += len(data)
            self._evict()

    def _evict(self) -> None:
        while self._size > self.max_bytes and self._entries:
            path, size = self._entries.popitem(last=False)
            path.unlink(missing_ok=True)
            self._size -= size


class ImageStore:
    """Shared GCS reader for the capture grid used by the BFCL HTTP server."""

    def __init__(
        self,
        bucket_name: str,
        gcs_prefix: str = "mapcrunch_images/",
        cache_dir: str = "/tmp/mapcrunch_cache",
        cache_size_gb: float = 5,
    ):
        if not bucket_name:
            raise ValueError("MAPCRUNCH_GCS_BUCKET is required")
        size = float(cache_size_gb)
        if not math.isfinite(size) or size < 0:
            raise ValueError("MAPCRUNCH_CACHE_SIZE_GB must be finite and nonnegative")
        from google.cloud import storage

        self._bucket_name = bucket_name
        self._bucket = storage.Client().bucket(bucket_name)
        self._prefix = gcs_prefix.rstrip("/") + "/" if gcs_prefix else ""
        self._cache = (
            _DiskImageCache(cache_dir, int(size * 1024**3)) if cache_dir and size else None
        )

    def get(self, pano_id: str, heading: float, pitch: float, zoom: float) -> bytes:
        safe_pano = pano_id.replace(".", "_")
        key = f"{self._prefix}{safe_pano}/{float(heading)}_{float(pitch)}_{float(zoom)}.jpg"
        cache_key = self._bucket_name + "\0" + key
        if self._cache is not None:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached
        data = self._bucket.blob(key).download_as_bytes()
        if self._cache is not None:
            self._cache.put(cache_key, data)
        return data
