"""Redis-backed cache with an in-memory fallback.

The fallback keeps tests and first-run experiences working before anyone has
started the compose stack; it is not a substitute for Redis in real use.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger(__name__)


class Cache:
    def __init__(self, url: str | None = None):
        self.url = url
        self._redis: Any = None
        self._memory: dict[str, tuple[float, str]] = {}

    async def connect(self) -> None:
        if self._redis is not None or not self.url:
            return
        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(self.url, decode_responses=True)
            await client.ping()
            self._redis = client
        except Exception as exc:  # noqa: BLE001 - any failure means fall back
            log.warning("Redis unavailable (%s); using in-memory cache", exc)
            self._redis = None

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def get_json(self, key: str) -> dict | None:
        if self._redis is not None:
            raw = await self._redis.get(key)
        else:
            entry = self._memory.get(key)
            if entry and entry[0] < time.time():
                del self._memory[key]
                entry = None
            raw = entry[1] if entry else None
        return json.loads(raw) if raw else None

    async def set_json(self, key: str, value: dict, ttl: int) -> None:
        raw = json.dumps(value, default=str)
        if self._redis is not None:
            await self._redis.set(key, raw, ex=ttl)
        else:
            self._memory[key] = (time.time() + ttl, raw)
