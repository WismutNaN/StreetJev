import gzip
import json
import time

from streetjev.infrastructure.vector_cache import VectorCache, expires_at


def test_cache_freshness_uses_date_age_and_server_revalidation():
    now = 1_000_000
    assert expires_at({}, now) == now + 7 * 86400
    assert expires_at({"cache-control": 'public, max-age="100"', "age": "20"}, now) == now + 80
    assert expires_at({"cache-control": "max-age=100", "date": "Mon, 12 Jan 1970 13:45:50 GMT"}, now) == now + 50
    assert expires_at({"expires": "Mon, 12 Jan 1970 13:46:50 GMT"}, now) == now + 10
    assert expires_at({"cache-control": "max-age=100, no-store"}, now) == now
    assert expires_at({"cache-control": 'no-cache="ETag"'}, now) == now


def test_prepared_cache_restart_expiration_source_and_corruption(tmp_path):
    cache = VectorCache(tmp_path)
    details = {"roads": [[True, [[0, 0], [1, 1]]]], "labels": [[0.5, 0.5, "Город", 123]]}
    details.update(water=[], rivers=[], landmarks=[])
    url = "https://example.test/14/200/300.mvt"
    expiry = time.time() + 100
    cache.write(url, details, expiry)
    assert VectorCache(tmp_path).read(url) == (details, expiry)
    assert cache.read(url.replace("example.test", "another.test")) is None
    cache.path(url).write_bytes(gzip.compress(json.dumps({"expires": 0, "details": details}).encode()))
    assert cache.read(url) is None
    cache.path(url).write_bytes(b"broken gzip")
    assert cache.read(url) is None
    cache.write(url + "?private", details, time.time() - 1)
    assert not cache.path(url + "?private").exists()


def test_cache_write_failure_is_optional(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("keep")
    cache = VectorCache(blocked)
    cache.write("url", {"roads": [], "labels": []}, time.time() + 100)
    assert cache.read("url") is None
    assert blocked.read_text() == "keep"
