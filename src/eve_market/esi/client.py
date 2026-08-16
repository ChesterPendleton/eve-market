"""Async ESI client with ETag caching, pagination and error-limit backoff.

Design notes — these follow CCP's published ESI etiquette, and getting them
wrong is the usual reason a third-party app gets rate-limited:

* Every request carries a descriptive User-Agent with a contact address.
* Responses are cached until their ``Expires`` header. Re-requesting before
  then is served from cache without touching the network.
* On a stale-but-present cache entry we revalidate with ``If-None-Match``;
  a 304 costs no error budget and no bandwidth.
* ESI publishes a shared error budget in ``X-ESI-Error-Limit-Remain``. When
  it runs low we stop and wait for the reset rather than burning it down,
  because exhausting it gets the whole application temporarily banned.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Self

import httpx

from ..cache import Cache
from ..config import Settings
from ..config import settings as default_settings
from .models import EsiPage

log = logging.getLogger(__name__)

# Stop issuing requests when the remaining error budget drops below this.
ERROR_LIMIT_FLOOR = 10
# Retry these transient statuses with exponential backoff.
RETRY_STATUSES = {500, 502, 503, 504}
MAX_RETRIES = 4


class EsiError(RuntimeError):
    def __init__(self, status: int, path: str, body: str = ""):
        super().__init__(f"ESI {status} for {path}: {body[:200]}")
        self.status = status
        self.path = path


class ErrorLimited(EsiError):
    """Raised when ESI's shared error budget is nearly exhausted."""


class EsiClient:
    """Async client for ESI's public market endpoints.

    Use as an async context manager::

        async with EsiClient() as esi:
            page = await esi.get("/v1/markets/10000002/orders/")
    """

    def __init__(
        self,
        cache: Cache | None = None,
        config: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.config = config or default_settings
        self.cache = cache or Cache(self.config.redis_url)
        self._sem = asyncio.Semaphore(self.config.esi_concurrency)
        self._client = httpx.AsyncClient(
            base_url=self.config.esi_base_url,
            timeout=self.config.esi_timeout,
            headers={
                "User-Agent": self.config.user_agent,
                "Accept": "application/json",
            },
            transport=transport,
            follow_redirects=True,
        )
        # Set when ESI tells us the budget is low; blocks further requests.
        self._error_reset_at: float | None = None

    async def __aenter__(self) -> Self:
        await self.cache.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()
        await self.cache.close()

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _cache_key(path: str, params: dict[str, Any] | None) -> str:
        qs = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        return f"esi:{path}?{qs}"

    def _note_error_budget(self, response: httpx.Response) -> tuple[int | None, int | None]:
        remain = response.headers.get("x-esi-error-limit-remain")
        reset = response.headers.get("x-esi-error-limit-reset")
        remain_i = int(remain) if remain is not None else None
        reset_i = int(reset) if reset is not None else None
        if remain_i is not None and remain_i < ERROR_LIMIT_FLOOR:
            loop = asyncio.get_running_loop()
            self._error_reset_at = loop.time() + (reset_i or 60)
            log.warning("ESI error budget low (%s left), pausing %ss", remain_i, reset_i)
        return remain_i, reset_i

    async def _await_error_budget(self) -> None:
        if self._error_reset_at is None:
            return
        loop = asyncio.get_running_loop()
        delay = self._error_reset_at - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        self._error_reset_at = None

    @staticmethod
    def _parse_expires(response: httpx.Response) -> datetime | None:
        raw = response.headers.get("expires")
        if not raw:
            return None
        try:
            return parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None

    # -- public API --------------------------------------------------------

    async def get(self, path: str, params: dict[str, Any] | None = None) -> EsiPage:
        """Fetch one page, honouring the cache and the error budget."""
        params = dict(params or {})
        params.setdefault("datasource", self.config.esi_datasource)
        key = self._cache_key(path, params)

        cached = await self.cache.get_json(key)
        now = datetime.now(UTC)
        if cached:
            expires_raw = cached.get("expires")
            expires = datetime.fromisoformat(expires_raw) if expires_raw else None
            if expires and expires > now:
                # Still fresh — no request at all.
                return EsiPage(
                    data=cached["data"],
                    etag=cached.get("etag"),
                    expires=expires,
                    pages=cached.get("pages", 1),
                    from_cache=True,
                )

        headers = {}
        if cached and cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]

        response = await self._request(path, params, headers)

        if response.status_code == 304 and cached:
            # Unchanged upstream; refresh our copy's expiry and reuse the body.
            page = EsiPage(
                data=cached["data"],
                etag=cached.get("etag"),
                expires=self._parse_expires(response),
                pages=cached.get("pages", 1),
                from_cache=True,
            )
            await self._store(key, page)
            return page

        page = EsiPage(
            data=response.json(),
            etag=response.headers.get("etag"),
            expires=self._parse_expires(response),
            pages=int(response.headers.get("x-pages", 1)),
        )
        await self._store(key, page)
        return page

    async def _request(
        self, path: str, params: dict[str, Any], headers: dict[str, str]
    ) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            await self._await_error_budget()
            async with self._sem:
                try:
                    response = await self._client.get(path, params=params, headers=headers)
                except httpx.TransportError as exc:  # network flake
                    last_exc = exc
                    await asyncio.sleep(2**attempt)
                    continue

            self._note_error_budget(response)

            if response.status_code in RETRY_STATUSES:
                last_exc = EsiError(response.status_code, path, response.text)
                await asyncio.sleep(2**attempt)
                continue
            if response.status_code == 420:  # ESI's dedicated error-limited code
                raise ErrorLimited(420, path, response.text)
            if response.status_code >= 400 and response.status_code != 304:
                raise EsiError(response.status_code, path, response.text)
            return response

        assert last_exc is not None
        raise last_exc

    async def _store(self, key: str, page: EsiPage) -> None:
        if not page.expires:
            return
        ttl = int((page.expires - datetime.now(UTC)).total_seconds())
        if ttl <= 0:
            return
        await self.cache.set_json(
            key,
            {
                "data": page.data,
                "etag": page.etag,
                "expires": page.expires.isoformat(),
                "pages": page.pages,
            },
            # Keep the entry past its freshness window so the ETag stays
            # available for cheap revalidation.
            ttl=ttl + 3600,
        )

    async def get_all_pages(
        self, path: str, params: dict[str, Any] | None = None
    ) -> list[dict]:
        """Fetch page 1, then the remaining pages concurrently.

        Region-wide order books run to a dozen-plus pages, so serial fetching
        is the difference between a few seconds and a minute.
        """
        first = await self.get(path, params)
        if not isinstance(first.data, list):
            raise TypeError(f"{path} returned an object, not a paginated list")
        if first.pages <= 1:
            return list(first.data)

        async def fetch(page_no: int) -> list[dict]:
            page = await self.get(path, {**(params or {}), "page": page_no})
            return list(page.data)

        rest = await asyncio.gather(*(fetch(n) for n in range(2, first.pages + 1)))
        out = list(first.data)
        for chunk in rest:
            out.extend(chunk)
        return out


class FixtureTransport(httpx.AsyncBaseTransport):
    """Serves recorded JSON from disk so the app runs with no network.

    Fixture files are named after the request path with slashes replaced by
    double underscores, e.g. ``_v1_markets_10000002_orders_.json``.
    """

    def __init__(self, fixture_dir: str):
        from pathlib import Path

        self.dir = Path(fixture_dir)

    def _path_for(self, url: httpx.URL) -> Any:
        name = url.path.replace("/", "_") + ".json"
        return self.dir / name

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        target = self._path_for(request.url)
        if not target.exists():
            return httpx.Response(404, json={"error": f"no fixture: {target.name}"})
        body = json.loads(target.read_text())
        # Give fixtures a far-future expiry so cache logic behaves normally.
        return httpx.Response(
            200,
            json=body,
            headers={
                "expires": "Wed, 21 Oct 2099 07:28:00 GMT",
                "etag": f'"fixture-{target.stem}"',
                "x-pages": "1",
            },
        )


def build_client(config: Settings | None = None, cache: Cache | None = None) -> EsiClient:
    """Construct a client wired for live or fixture mode per configuration."""
    config = config or default_settings
    transport = None if config.esi_live else FixtureTransport(config.fixture_dir)
    return EsiClient(cache=cache, config=config, transport=transport)
