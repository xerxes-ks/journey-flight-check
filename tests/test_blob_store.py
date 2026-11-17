"""Private Vercel Blob HTTP API: fake server, version CAS, storage selection."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from flightcheck.exceptions import StorageError
from flightcheck.store import (
    BLOB_API,
    BLOB_API_VERSION,
    BlobStore,
    MemoryStore,
    RedisStore,
    StorageNotConfigured,
    get_store,
    reset_process_memory,
    storage_kind,
    use_store,
)
from flightcheck.track import load_track, save_track, start_track, tick, untrack
from tests.conftest import NEAR_DEP, scenario_fetch
from flightcheck.model import parse_time


class FakeBlobState:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.requests: list[tuple[str, str, dict, str]] = []


def _handler(state: FakeBlobState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def _auth(self) -> bool:
            return (self.headers.get("Authorization") or "").startswith("Bearer ")

        def _read(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def _send(self, status: int, payload, content_type="application/json"):
            raw = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_PUT(self):
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            body = self._read()
            state.requests.append(("PUT", self.path, dict(self.headers), parsed.query))
            if not self._auth():
                self._send(401, {"error": "unauthorized"})
                return
            access = (self.headers.get("x-vercel-blob-access") or "").lower()
            if access != "private":
                self._send(400, {"error": "Cannot use public access on a private store"})
                return
            pathname = (qs.get("pathname") or [parsed.path.lstrip("/")])[0]
            state.objects[pathname] = body
            self._send(200, {
                "url": f"http://{self.headers.get('Host')}/{pathname}",
                "pathname": pathname,
                "contentType": "application/json",
            })

        def do_GET(self):
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            state.requests.append(("GET", self.path, dict(self.headers), parsed.query))
            if not self._auth():
                self._send(401, {"error": "unauthorized"})
                return
            if "prefix" in qs:
                prefix = qs.get("prefix", [""])[0]
                blobs = [{"pathname": k, "url": f"http://host/{k}"} for k in sorted(state.objects) if k.startswith(prefix)]
                self._send(200, {"blobs": blobs, "hasMore": False})
                return
            pathname = parsed.path.lstrip("/")
            if pathname not in state.objects:
                self._send(404, {"error": "not found"})
                return
            self._send(200, state.objects[pathname], "application/json")

        def do_POST(self):
            parsed = urlparse(self.path)
            body = self._read()
            state.requests.append(("POST", self.path, dict(self.headers), parsed.query))
            if not self._auth():
                self._send(401, {"error": "unauthorized"})
                return
            if parsed.path.rstrip("/") == "/delete":
                data = json.loads(body.decode() or "{}")
                for url in data.get("urls") or []:
                    name = urlparse(url).path.lstrip("/")
                    state.objects.pop(name, None)
                self._send(200, {"success": True})
                return
            self._send(404, {"error": "not found"})

    return Handler


@pytest.fixture
def blob_server():
    state = FakeBlobState()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    yield state, f"http://{host}:{port}"
    httpd.shutdown()
    httpd.server_close()


def test_blob_put_get_list_delete_private_no_cache(blob_server):
    state, base = blob_server
    store = BlobStore("vercel_blob_rw_teststore_secret", base_url=base)
    rec = {"tracking_id": "trk_ab", "version": 1, "flight": "XX123"}
    store.put_json("tracks/trk_ab.json", rec)
    assert store.get_json("tracks/trk_ab.json")["flight"] == "XX123"
    assert "tracks/trk_ab.json" in store.list_prefix("tracks/")
    methods = [(m, path, hdr, q) for m, path, hdr, q in state.requests]
    put = next(x for x in methods if x[0] == "PUT")
    hdr = {k.lower(): v for k, v in put[2].items()}
    assert hdr.get("authorization", "").startswith("Bearer ")
    assert hdr.get("x-api-version") == "13"
    assert hdr.get("x-add-random-suffix") == "0"
    assert hdr.get("x-allow-overwrite") == "1"
    assert hdr.get("x-cache-control-max-age") == "0"
    assert hdr.get("x-vercel-blob-access") == "private"
    qs = parse_qs(put[3])
    assert set(qs) == {"pathname"}
    assert qs["pathname"] == ["tracks/trk_ab.json"]
    assert "addRandomSuffix" not in qs
    get = next(x for x in methods if x[0] == "GET" and "prefix=" not in x[1])
    assert {k.lower(): v for k, v in get[2].items()}.get("authorization", "").startswith("Bearer ")
    assert "cache=0" in get[1]
    store.delete("tracks/trk_ab.json")
    assert store.get_json("tracks/trk_ab.json") is None
    assert store.list_prefix("tracks/") == []


def test_blob_put_pins_sdk_url_query_and_headers():
    """PUT must match current @vercel/blob: vercel.com/api/blob, v13, pathname-only query."""
    captured = []

    def fetch(req):
        captured.append(req)
        return 200, b'{"pathname":"tracks/a.json"}', {"content-type": "application/json"}

    store = BlobStore("vercel_blob_rw_teststore_secret", fetch=fetch)
    store.put_json("tracks/a.json", {"ok": True})
    assert len(captured) == 1
    req = captured[0]
    parsed = urlparse(req.get_full_url())
    assert parsed.scheme == "https"
    assert parsed.netloc == "vercel.com"
    assert parsed.path == "/api/blob/"
    qs = parse_qs(parsed.query)
    assert set(qs) == {"pathname"}
    assert qs["pathname"] == ["tracks/a.json"]
    hdr = {k.lower(): v for k, v in req.header_items()}
    assert req.get_method() == "PUT"
    assert BLOB_API == "https://vercel.com/api/blob"
    assert BLOB_API_VERSION == "13"
    assert hdr.get("x-api-version") == "13"
    assert hdr.get("x-vercel-blob-access") == "private"
    assert hdr.get("x-add-random-suffix") == "0"
    assert hdr.get("x-allow-overwrite") == "1"
    assert hdr.get("x-content-type") == "application/json"
    assert hdr.get("x-cache-control-max-age") == "0"
    assert hdr.get("authorization", "").startswith("Bearer ")
    assert hdr.get("x-vercel-blob-store-id") == "teststore"
    assert "addRandomSuffix" not in parsed.query
    assert "allowOverwrite" not in parsed.query
    assert "cacheControlMaxAge" not in parsed.query
    assert "access=" not in parsed.query


def test_blob_get_on_prod_api_uses_private_host():
    captured = []

    def fetch(req):
        captured.append(req)
        return 404, b"", {}

    store = BlobStore("vercel_blob_rw_teststore_secret", fetch=fetch)
    assert store.get_json("tracks/a.json") is None
    url = captured[0].get_full_url()
    assert url.startswith("https://teststore.private.blob.vercel-storage.com/tracks/a.json")
    assert "cache=0" in url


def test_blob_put_without_access_header_is_storage_error(blob_server):
    _, base = blob_server
    store = BlobStore("vercel_blob_rw_teststore_secret", base_url=base)
    orig = store._headers

    def stripped(extra=None):
        h = orig(extra)
        return {k: v for k, v in h.items() if k.lower() != "x-vercel-blob-access"}

    store._headers = stripped
    with pytest.raises(StorageError) as exc:
        store.put_json("tracks/nope.json", {"tracking_id": "nope"})
    assert exc.value.code == "storage_error"
    assert exc.value.http_status == 503
    assert exc.value.to_dict()["retryable"] is False
    assert "public access" in str(exc.value).lower() or "private store" in str(exc.value).lower()


def test_blob_version_skips_stale_write(blob_server):
    _, base = blob_server
    store = BlobStore("vercel_blob_rw_teststore_secret", base_url=base)
    rec = {"tracking_id": "trk_cas", "checkpoint": "t_minus_24h", "version": 0}
    assert save_track(store, rec) is True
    assert rec["version"] == 1
    stale = dict(rec)
    stale["checkpoint"] = "t_minus_6h"
    stale["version"] = 0
    assert save_track(store, stale) is False
    loaded = load_track(store, "trk_cas")
    assert loaded["checkpoint"] == "t_minus_24h"
    assert loaded["version"] == 1


def test_memory_version_cas_and_track_layout():
    store = MemoryStore()
    started = start_track(
        {
            "flight": "XX123",
            "date": "2027-03-15",
            "origin": "XXA",
            "webhook_url": "https://example.test/hook",
        },
        store=store,
        now=parse_time(NEAR_DEP),
        fetch=scenario_fetch(),
        deliver=lambda *a, **k: {"ok": True},
        webhook_secret="x",
    )
    tid = started["tracking_id"]
    assert store.get_json(f"tracks/{tid}.json")
    assert store.list_prefix(f"events/{tid}/")
    rec = load_track(store, tid)
    rec["ended"] = False
    rec["next_check_at"] = "2020-01-01T00:00:00Z"
    save_track(store, rec)
    out = tick(store=store, now=parse_time(NEAR_DEP), fetch=scenario_fetch(), deliver=lambda *a, **k: {"ok": True})
    assert out["ok"] is True
    gone = untrack(tid, store=store)
    assert gone["ok"] is True
    assert load_track(store, tid) is None


def test_redis_path_mapping():
    kv: dict[str, str] = {}
    members: set[str] = set()

    def fetch(commands):
        seq = commands if commands and isinstance(commands[0], list) else [commands]
        out = []
        for cmd in seq:
            op = cmd[0]
            if op == "GET":
                out.append({"result": kv.get(cmd[1])})
            elif op == "SET":
                kv[cmd[1]] = cmd[2]
                out.append({"result": "OK"})
            elif op == "DEL":
                kv.pop(cmd[1], None)
                out.append({"result": 1})
            elif op == "SADD":
                members.add(cmd[2])
                out.append({"result": 1})
            elif op == "SREM":
                members.discard(cmd[2])
                out.append({"result": 1})
            elif op == "SMEMBERS":
                out.append({"result": list(members)})
            else:
                out.append({"result": None})
        return out

    store = RedisStore("https://example.upstash.io", "tok", fetch=fetch)
    store.put_json("budget/2027-03-15.json", {"spent": 0.01, "version": 1})
    assert store.get_json("budget/2027-03-15.json")["spent"] == 0.01
    assert "budget/2027-03-15.json" in store.list_prefix("budget/")
    store.delete("budget/2027-03-15.json")
    assert store.get_json("budget/2027-03-15.json") is None


def test_get_store_prefers_blob_over_redis(monkeypatch):
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "vercel_blob_rw_abc_secret")
    monkeypatch.setenv("KV_REST_API_URL", "https://example.upstash.io")
    monkeypatch.setenv("KV_REST_API_TOKEN", "tok")
    monkeypatch.delenv("FLIGHTCHECK_STORAGE", raising=False)
    use_store(None)
    assert storage_kind() == "blob"
    assert get_store().kind == "blob"


def test_get_store_redis_only_when_explicit(monkeypatch):
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    monkeypatch.setenv("KV_REST_API_URL", "https://example.upstash.io")
    monkeypatch.setenv("KV_REST_API_TOKEN", "tok")
    monkeypatch.setenv("FLIGHTCHECK_STORAGE", "redis")
    use_store(None)
    assert storage_kind() == "redis"
    assert get_store().kind == "redis"


def test_storage_not_configured(monkeypatch):
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    monkeypatch.delenv("KV_REST_API_URL", raising=False)
    monkeypatch.delenv("KV_REST_API_TOKEN", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.delenv("FLIGHTCHECK_STORAGE", raising=False)
    use_store(None)
    assert storage_kind() == "none"
    with pytest.raises(StorageNotConfigured) as exc:
        get_store()
    assert exc.value.code == "storage_not_configured"


def test_memory_backend_when_explicit(monkeypatch):
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    monkeypatch.setenv("FLIGHTCHECK_STORAGE", "memory")
    use_store(None)
    reset_process_memory()
    assert storage_kind() == "memory"
    assert get_store().kind == "memory"
