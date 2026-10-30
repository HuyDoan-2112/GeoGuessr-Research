import os
import sys
import types
from concurrent.futures import ThreadPoolExecutor

import pytest

from apps.image_store import ImageStore, _DiskImageCache


@pytest.fixture
def cloud(monkeypatch):
    downloads = []

    class Bucket:
        def __init__(self, name):
            self.name = name

        def blob(self, key):
            def download():
                downloads.append((self.name, key))
                return f"{self.name}/{key}".encode()

            return types.SimpleNamespace(download_as_bytes=download)

    storage = types.ModuleType("google.cloud.storage")
    storage.Client = lambda: types.SimpleNamespace(bucket=Bucket)
    google = types.ModuleType("google")
    google.cloud = types.ModuleType("google.cloud")
    google.cloud.storage = storage
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.cloud", google.cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", storage)
    return downloads


def test_gcs_layout_and_cache_survive_restart(tmp_path, cloud):
    store = ImageStore("panos", "images", str(tmp_path))
    expected = b"panos/images/pano_/30.0_-15.0_1.0.jpg"
    assert store.get("pano.", 30, -15, 1) == expected
    assert store.get("pano.", 30, -15, 1) == expected
    restarted = ImageStore("panos", "images/", str(tmp_path))
    assert restarted.get("pano.", 30, -15, 1) == expected
    assert cloud == [("panos", "images/pano_/30.0_-15.0_1.0.jpg")]


def test_bucket_names_are_part_of_cache_identity(tmp_path, cloud):
    first = ImageStore("first", cache_dir=str(tmp_path))
    second = ImageStore("second", cache_dir=str(tmp_path))
    assert first.get("p", 0, 0, 1).startswith(b"first/")
    assert second.get("p", 0, 0, 1).startswith(b"second/")
    assert len(cloud) == 2


def test_zero_cache_size_disables_cache(tmp_path, cloud):
    store = ImageStore("panos", cache_dir=str(tmp_path), cache_size_gb=0)
    store.get("p", 0, 0, 1)
    store.get("p", 0, 0, 1)
    assert len(cloud) == 2
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("size", [-1, float("nan"), float("inf")])
def test_invalid_cache_size_is_rejected(tmp_path, cloud, size):
    with pytest.raises(ValueError, match="nonnegative"):
        ImageStore("panos", cache_dir=str(tmp_path), cache_size_gb=size)


def test_cache_evicts_least_recently_read_and_skips_oversized_images(tmp_path):
    cache = _DiskImageCache(str(tmp_path), 6)
    cache.put("a", b"aaa")
    cache.put("b", b"bbb")
    assert cache.get("a") == b"aaa"
    cache.put("c", b"ccc")
    assert cache.get("b") is None
    assert cache.get("a") == b"aaa"
    assert cache.get("c") == b"ccc"
    cache.put("large", b"1234567")
    assert cache.get("large") is None
    assert sum(path.stat().st_size for path in cache.directory.glob("*.jpg")) <= 6


def test_restart_uses_persisted_access_order_and_reduces_cache_limit(tmp_path):
    cache = _DiskImageCache(str(tmp_path), 9)
    for key in ("a", "b", "c"):
        cache.put(key, key.encode() * 3)
    for timestamp, key in enumerate(("b", "c", "a"), start=1):
        os.utime(cache._path(key), (timestamp, timestamp))
    restarted = _DiskImageCache(str(tmp_path), 6)
    assert restarted.get("b") is None
    assert restarted.get("a") == b"aaa"
    assert restarted.get("c") == b"ccc"


def test_concurrent_replacements_are_atomic(tmp_path):
    caches = [_DiskImageCache(str(tmp_path), 1024**2) for _ in range(2)]
    payloads = [bytes([i]) * 4096 for i in range(24)]

    def write(index):
        cache = caches[index % 2]
        cache.put("same-key", payloads[index])
        assert cache.get("same-key") in payloads

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(len(payloads))))
    assert caches[0].get("same-key") in payloads
    assert not list(caches[0].directory.glob("*.tmp"))
