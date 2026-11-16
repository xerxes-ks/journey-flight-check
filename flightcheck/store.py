"""KV store: Upstash Redis REST in production, in-memory for tests/simulate.

Env (either pair):
  KV_REST_API_URL + KV_REST_API_TOKEN
  UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from typing import Optional

from .exceptions import CheckError

PREFIX = "fc:"
SINK_MAX = 50
EVENTS_MAX = 200


class StorageNotConfigured(CheckError):
    def __init__(self):
        super().__init__(
            "storage_not_configured: set KV_REST_API_URL/KV_REST_API_TOKEN or UPSTASH_REDIS_REST_URL/UPSTASH_REDIS_REST_TOKEN",
            code="storage_not_configured",
            http_status=503,
        )


def redis_env() -> tuple[str, str] | None:
    url = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL") or ""
    token = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN") or ""
    if url and token:
        return url.rstrip("/"), token
    return None


class Store:
    def get(self, key: str) -> Optional[str]:
        raise NotImplementedError

    def set(self, key: str, value: str) -> None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def sadd(self, key: str, member: str) -> None:
        raise NotImplementedError

    def srem(self, key: str, member: str) -> None:
        raise NotImplementedError

    def smembers(self, key: str) -> list[str]:
        raise NotImplementedError

    def lpush(self, key: str, value: str, maxlen: int | None = None) -> None:
        raise NotImplementedError

    def lrange(self, key: str, start: int = 0, stop: int = -1) -> list[str]:
        raise NotImplementedError

    def incrbyfloat(self, key: str, amount: float) -> float:
        raise NotImplementedError


class MemoryStore(Store):
    def __init__(self):
        self._lock = threading.Lock()
        self.kv: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.lists: dict[str, list[str]] = {}

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            return self.kv.get(key)

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self.kv[key] = value

    def delete(self, key: str) -> None:
        with self._lock:
            self.kv.pop(key, None)
            self.sets.pop(key, None)
            self.lists.pop(key, None)

    def sadd(self, key: str, member: str) -> None:
        with self._lock:
            self.sets.setdefault(key, set()).add(member)

    def srem(self, key: str, member: str) -> None:
        with self._lock:
            if key in self.sets:
                self.sets[key].discard(member)

    def smembers(self, key: str) -> list[str]:
        with self._lock:
            return list(self.sets.get(key, ()))

    def lpush(self, key: str, value: str, maxlen: int | None = None) -> None:
        with self._lock:
            lst = self.lists.setdefault(key, [])
            lst.insert(0, value)
            if maxlen is not None:
                del lst[maxlen:]

    def lrange(self, key: str, start: int = 0, stop: int = -1) -> list[str]:
        with self._lock:
            lst = self.lists.get(key, [])
            if stop == -1:
                return list(lst[start:])
            return list(lst[start : stop + 1])

    def incrbyfloat(self, key: str, amount: float) -> float:
        with self._lock:
            cur = float(self.kv.get(key) or 0)
            cur = round(cur + float(amount), 6)
            self.kv[key] = str(cur)
            return cur


class RedisStore(Store):
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

    def get(self, key: str) -> Optional[str]:
        val = self._cmd("GET", key)
        if val is None:
            return None
        return str(val)

    def set(self, key: str, value: str) -> None:
        self._cmd("SET", key, value)

    def delete(self, key: str) -> None:
        self._cmd("DEL", key)

    def sadd(self, key: str, member: str) -> None:
        self._cmd("SADD", key, member)

    def srem(self, key: str, member: str) -> None:
        self._cmd("SREM", key, member)

    def smembers(self, key: str) -> list[str]:
        val = self._cmd("SMEMBERS", key) or []
        return [str(x) for x in val]

    def lpush(self, key: str, value: str, maxlen: int | None = None) -> None:
        if maxlen is None:
            self._cmd("LPUSH", key, value)
            return
        self.fetch([["LPUSH", key, value], ["LTRIM", key, 0, int(maxlen) - 1]])

    def lrange(self, key: str, start: int = 0, stop: int = -1) -> list[str]:
        val = self._cmd("LRANGE", key, str(start), str(stop)) or []
        return [str(x) for x in val]

    def incrbyfloat(self, key: str, amount: float) -> float:
        val = self._cmd("INCRBYFLOAT", key, str(amount))
        return float(val or 0)


_OVERRIDE: Store | None = None


def use_store(store: Store | None) -> None:
    global _OVERRIDE
    _OVERRIDE = store


def get_store() -> Store:
    if _OVERRIDE is not None:
        return _OVERRIDE
    env = redis_env()
    if not env:
        raise StorageNotConfigured()
    return RedisStore(*env)


def json_get(store: Store, key: str):
    raw = store.get(key)
    if not raw:
        return None
    return json.loads(raw)


def json_set(store: Store, key: str, value) -> None:
    store.set(key, json.dumps(value, separators=(",", ":")))
