"""Storage backends for tracking state.

BlobStore is the default whenever BLOB_READ_WRITE_TOKEN is set (private
Vercel Blob, stdlib urllib, no SDK). MemoryStore is for tests/simulate.
RedisStore is optional: KV/Upstash env AND FLIGHTCHECK_STORAGE=redis.

Layout:
  tracks/{tracking_id}.json
  events/{tracking_id}/{ts}-{seq}.json
  sink/{ts}.json
  budget/{YYYY-MM-DD}.json
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional
from urllib.parse import urlencode

from .exceptions import CheckError, StorageError

BLOB_API = "https://blob.vercel-storage.com"
BLOB_API_VERSION = "7"
EVENTS_MAX = 200
SINK_MAX = 50
PREFIX = "fc:"


class StorageNotConfigured(CheckError):
    def __init__(self):
        super().__init__(
            "storage_not_configured: set BLOB_READ_WRITE_TOKEN "
            "(or FLIGHTCHECK_STORAGE=redis with KV/Upstash env, or FLIGHTCHECK_STORAGE=memory)",
            code="storage_not_configured",
            http_status=503,
        )


class StaleWrite(CheckError):
    def __init__(self, path: str):
        super().__init__(f"stale write skipped for {path}", code="conflict", http_status=409)


class Storage:
    """Path-based JSON store. `kind` is blob | memory | redis."""

    kind = "none"

    def get_json(self, path: str) -> Optional[dict]:
        raise NotImplementedError

    def put_json(self, path: str, obj: dict) -> None:
        raise NotImplementedError

    def delete(self, path: str) -> None:
        raise NotImplementedError

    def list_prefix(self, prefix: str) -> list[str]:
        raise NotImplementedError

    def put_json_if_current(self, path: str, obj: dict, expected_version: int) -> bool:
        """Write obj with version=expected+1 unless a newer version is already stored."""
        current = self.get_json(path)
        if current is not None:
            have = int(current.get("version") or 0)
            if have > int(expected_version or 0):
                return False
        nxt = dict(obj)
        nxt["version"] = int(expected_version or 0) + 1
        self.put_json(path, nxt)
        obj["version"] = nxt["version"]
        return True


class MemoryStore(Storage):
    kind = "memory"

    def __init__(self):
        self._lock = threading.Lock()
        self.objects: dict[str, str] = {}

    def get_json(self, path: str) -> Optional[dict]:
        with self._lock:
            raw = self.objects.get(_norm(path))
        if not raw:
            return None
        return json.loads(raw)

    def put_json(self, path: str, obj: dict) -> None:
        blob = json.dumps(obj, separators=(",", ":"))
        with self._lock:
            self.objects[_norm(path)] = blob

    def delete(self, path: str) -> None:
        with self._lock:
            self.objects.pop(_norm(path), None)

    def list_prefix(self, prefix: str) -> list[str]:
        p = _norm(prefix)
        with self._lock:
            return sorted(k for k in self.objects if k.startswith(p))

    def put_json_if_current(self, path: str, obj: dict, expected_version: int) -> bool:
        with self._lock:
            raw = self.objects.get(_norm(path))
            if raw:
                have = int((json.loads(raw) or {}).get("version") or 0)
                if have > int(expected_version or 0):
                    return False
            nxt = dict(obj)
            nxt["version"] = int(expected_version or 0) + 1
            self.objects[_norm(path)] = json.dumps(nxt, separators=(",", ":"))
            obj["version"] = nxt["version"]
            return True


class RedisStore(Storage):
    """Optional Upstash REST mapping of the same path layout."""

    kind = "redis"

    def __init__(self, url: str, token: str, fetch=None):
        self.url = url.rstrip("/")
        self.token = token
        self.fetch = fetch or self._http

    def _http(self, commands):
        body = json.dumps(commands).encode()
        path = "/pipeline" if commands and isinstance(commands[0], list) else "/"
        req = urllib.request.Request(
            self.url + path,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")[:240]
            raise CheckError(f"redis HTTP {exc.code}: {raw}", code="upstream_error", http_status=exc.code)

    def _cmd(self, *parts):
        data = self.fetch(list(parts))
        if isinstance(data, list):
            data = data[0] if data else {}
        if isinstance(data, dict):
            return data.get("result")
        return data

    def _rkey(self, path: str) -> str:
        return PREFIX + "path:" + _norm(path)

    def get_json(self, path: str) -> Optional[dict]:
        val = self._cmd("GET", self._rkey(path))
        if val is None:
            return None
        return json.loads(str(val))

    def put_json(self, path: str, obj: dict) -> None:
        p = _norm(path)
        blob = json.dumps(obj, separators=(",", ":"))
        self.fetch([
            ["SET", self._rkey(p), blob],
            ["SADD", PREFIX + "paths", p],
        ])

    def delete(self, path: str) -> None:
        p = _norm(path)
        self.fetch([
            ["DEL", self._rkey(p)],
            ["SREM", PREFIX + "paths", p],
        ])

    def list_prefix(self, prefix: str) -> list[str]:
        val = self._cmd("SMEMBERS", PREFIX + "paths") or []
        p = _norm(prefix)
        return sorted(str(x) for x in val if str(x).startswith(p))


class BlobStore(Storage):
    """Private Vercel Blob via the HTTP API (stdlib urllib)."""

    kind = "blob"

    def __init__(self, token: str, base_url: str | None = None, fetch=None):
        self.token = token
        self.base_url = (base_url or os.environ.get("BLOB_API_URL") or BLOB_API).rstrip("/")
        self.fetch = fetch
        self._store_id = _blob_store_id(token)

    def _headers(self, extra=None) -> dict:
        h = {
            "Authorization": f"Bearer {self.token}",
            "x-api-version": BLOB_API_VERSION,
            "Cache-Control": "no-store",
        }
        if extra:
            h.update(extra)
        return h

    def _open(self, req: urllib.request.Request):
        if self.fetch:
            return self.fetch(req)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            raw = exc.read() if exc.fp else b""
            return exc.code, raw, dict(exc.headers or {})

    def put_json(self, path: str, obj: dict) -> None:
        # @vercel/blob putOptionHeaderMap: access → x-vercel-blob-access.
        # Private stores reject PUT without that header even if access= is in
        # the query string ("Cannot use public access on a private store").
        pathname = _norm(path)
        body = json.dumps(obj, separators=(",", ":")).encode()
        qs = urlencode({
            "pathname": pathname,
            "addRandomSuffix": "false",
            "allowOverwrite": "true",
            "cacheControlMaxAge": "0",
        })
        req = urllib.request.Request(
            self.base_url + "/?" + qs,
            data=body,
            method="PUT",
            headers=self._headers({
                "x-vercel-blob-access": "private",
                "x-content-type": "application/json",
                "x-add-random-suffix": "0",
                "x-allow-overwrite": "1",
                "x-cache-control-max-age": "0",
                "Content-Type": "application/json",
            }),
        )
        status, raw, _ = self._open(req)
        if status not in (200, 201):
            _raise_blob("put", status, raw)

    def get_json(self, path: str) -> Optional[dict]:
        # Private reads: {storeId}.private.blob.vercel-storage.com + Bearer.
        # useCache:false → cache=0 (SDK get.ts). List/del do not send access.
        pathname = _norm(path)
        url = self._object_url(pathname)
        sep = "&" if "?" in url else "?"
        req = urllib.request.Request(
            url + sep + "cache=0",
            method="GET",
            headers=self._headers(),
        )
        status, raw, _ = self._open(req)
        if status in (404, 400):
            return None
        if status != 200:
            _raise_blob("get", status, raw)
        if not raw:
            return None
        return json.loads(raw.decode())

    def delete(self, path: str) -> None:
        # SDK del.ts: POST /delete JSON {urls}, Content-Type + optional x-if-match.
        pathname = _norm(path)
        url = self._object_url(pathname)
        body = json.dumps({"urls": [url]}).encode()
        req = urllib.request.Request(
            self.base_url + "/delete",
            data=body,
            method="POST",
            headers=self._headers({"Content-Type": "application/json"}),
        )
        status, raw, _ = self._open(req)
        if status not in (200, 204, 404):
            _raise_blob("delete", status, raw)

    def list_prefix(self, prefix: str) -> list[str]:
        # SDK list: GET ?prefix=&limit=&cursor= with Authorization + x-api-version.
        out = []
        cursor = None
        p = _norm(prefix)
        while True:
            params = {"prefix": p, "limit": "1000"}
            if cursor:
                params["cursor"] = cursor
            req = urllib.request.Request(
                self.base_url + "/?" + urlencode(params),
                method="GET",
                headers=self._headers(),
            )
            status, raw, _ = self._open(req)
            if status != 200:
                _raise_blob("list", status, raw)
            data = json.loads(raw.decode() or "{}")
            for blob in data.get("blobs") or []:
                name = blob.get("pathname") or blob.get("url") or ""
                if name:
                    out.append(_norm(name))
            if not data.get("hasMore"):
                break
            cursor = data.get("cursor")
            if not cursor:
                break
        return sorted(out)

    def _object_url(self, pathname: str) -> str:
        if self._store_id and "blob.vercel-storage.com" in self.base_url:
            return f"https://{self._store_id}.private.blob.vercel-storage.com/{pathname}"
        return self.base_url.rstrip("/") + "/" + pathname


def _raise_blob(op: str, status, raw) -> None:
    text = (raw or b"").decode("utf-8", "replace")[:240] if isinstance(raw, (bytes, bytearray)) else str(raw or "")[:240]
    code = int(status or 0)
    msg = f"blob {op} HTTP {code}"
    if text:
        msg = f"{msg}: {text}"
    raise StorageError(msg, http_status=503, snippet=text[:160])


def _norm(path: str) -> str:
    return str(path or "").lstrip("/")


def _blob_store_id(token: str) -> str:
    # vercel_blob_rw_<storeId>_<secret>
    parts = (token or "").split("_")
    if len(parts) >= 4 and parts[0] == "vercel" and parts[1] == "blob":
        return parts[3]
    return ""


def redis_env() -> tuple[str, str] | None:
    url = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL") or ""
    token = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN") or ""
    if url and token:
        return url.rstrip("/"), token
    return None


def blob_token() -> str:
    return os.environ.get("BLOB_READ_WRITE_TOKEN") or ""


_OVERRIDE: Storage | None = None
_PROCESS_MEMORY: MemoryStore | None = None


def use_store(store: Storage | None) -> None:
    global _OVERRIDE
    _OVERRIDE = store


def reset_process_memory() -> MemoryStore:
    global _PROCESS_MEMORY
    _PROCESS_MEMORY = MemoryStore()
    return _PROCESS_MEMORY


def process_memory() -> MemoryStore:
    global _PROCESS_MEMORY
    if _PROCESS_MEMORY is None:
        _PROCESS_MEMORY = MemoryStore()
    return _PROCESS_MEMORY


def storage_kind() -> str:
    if _OVERRIDE is not None:
        return getattr(_OVERRIDE, "kind", "memory")
    explicit = (os.environ.get("FLIGHTCHECK_STORAGE") or "").strip().lower()
    if explicit == "redis":
        return "redis" if redis_env() else "none"
    if explicit == "memory":
        return "memory"
    if blob_token():
        return "blob"
    if explicit == "blob":
        return "none"
    return "none"


def get_store() -> Storage:
    if _OVERRIDE is not None:
        return _OVERRIDE
    explicit = (os.environ.get("FLIGHTCHECK_STORAGE") or "").strip().lower()
    if explicit == "redis":
        env = redis_env()
        if not env:
            raise StorageNotConfigured()
        return RedisStore(*env)
    if explicit == "memory":
        return process_memory()
    token = blob_token()
    if token:
        return BlobStore(token)
    raise StorageNotConfigured()
