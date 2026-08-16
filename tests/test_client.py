from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from eve_market.cache import Cache
from eve_market.config import Settings
from eve_market.esi.client import EsiClient, EsiError, FixtureTransport, build_client


def cfg(**kw) -> Settings:
    return Settings(
        contact_email="test@example.com",
        redis_url="",  # force the in-memory cache
        esi_concurrency=4,
        **kw,
    )


def expires_in(seconds: int) -> str:
    return format_datetime(datetime.now(UTC) + timedelta(seconds=seconds), usegmt=True)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Keep backoff logic exercised without actually waiting."""
    import asyncio

    async def instant(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", instant)


async def test_user_agent_includes_contact_email():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json=[], headers={"expires": expires_in(300)})

    async with EsiClient(
        cache=Cache(None), config=cfg(), transport=httpx.MockTransport(handler)
    ) as esi:
        await esi.get("/v1/markets/prices/")

    assert "test@example.com" in seen["user-agent"]


async def test_fresh_cache_avoids_second_request():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, json=[{"type_id": 34}], headers={"expires": expires_in(300)}
        )

    async with EsiClient(
        cache=Cache(None), config=cfg(), transport=httpx.MockTransport(handler)
    ) as esi:
        first = await esi.get("/v1/markets/10000002/orders/")
        second = await esi.get("/v1/markets/10000002/orders/")

    assert calls == 1
    assert first.from_cache is False
    assert second.from_cache is True
    assert second.data == [{"type_id": 34}]


async def test_stale_entry_revalidates_with_etag_and_reuses_body_on_304():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(304, headers={"expires": expires_in(300)})

    cache = Cache(None)
    await cache.connect()
    # Seed a stale entry that still carries its ETag.
    await cache.set_json(
        "esi:/v1/markets/prices/?datasource=tranquility",
        {
            "data": [{"type_id": 34, "average_price": 5.5}],
            "etag": '"abc123"',
            "expires": (datetime.now(UTC) - timedelta(seconds=10)).isoformat(),
            "pages": 1,
        },
        ttl=3600,
    )

    async with EsiClient(
        cache=cache, config=cfg(), transport=httpx.MockTransport(handler)
    ) as esi:
        page = await esi.get("/v1/markets/prices/")

    assert requests[0].headers["if-none-match"] == '"abc123"'
    assert page.from_cache is True
    assert page.data == [{"type_id": 34, "average_price": 5.5}]


async def test_get_all_pages_follows_x_pages():
    seen_pages: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        seen_pages.append(page)
        return httpx.Response(
            200,
            json=[{"order_id": int(page or 1)}],
            headers={"expires": expires_in(300), "x-pages": "3"},
        )

    async with EsiClient(
        cache=Cache(None), config=cfg(), transport=httpx.MockTransport(handler)
    ) as esi:
        rows = await esi.get_all_pages("/v1/markets/10000002/orders/")

    assert sorted(seen_pages, key=lambda p: p or "0") == [None, "2", "3"]
    assert sorted(r["order_id"] for r in rows) == [1, 2, 3]


async def test_low_error_budget_pauses_further_requests():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[],
            headers={
                "expires": expires_in(300),
                "x-esi-error-limit-remain": "3",
                "x-esi-error-limit-reset": "42",
            },
        )

    async with EsiClient(
        cache=Cache(None), config=cfg(), transport=httpx.MockTransport(handler)
    ) as esi:
        await esi.get("/v1/markets/prices/")
        # Budget below the floor must arm the pause rather than charging on.
        assert esi._error_reset_at is not None


async def test_transient_5xx_is_retried_then_succeeds():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, text="backend down")
        return httpx.Response(200, json=[{"ok": True}], headers={"expires": expires_in(300)})

    async with EsiClient(
        cache=Cache(None), config=cfg(), transport=httpx.MockTransport(handler)
    ) as esi:
        page = await esi.get("/v1/status/")

    assert calls == 2
    assert page.data == [{"ok": True}]


async def test_client_error_is_raised_not_retried():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, text="not found")

    async with EsiClient(
        cache=Cache(None), config=cfg(), transport=httpx.MockTransport(handler)
    ) as esi:
        with pytest.raises(EsiError) as excinfo:
            await esi.get("/v1/markets/99999999/orders/")

    assert calls == 1
    assert excinfo.value.status == 404


async def test_datasource_is_always_sent():
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json=[], headers={"expires": expires_in(300)})

    async with EsiClient(
        cache=Cache(None), config=cfg(), transport=httpx.MockTransport(handler)
    ) as esi:
        await esi.get("/v1/markets/prices/")

    assert seen[0].params["datasource"] == "tranquility"


async def test_fixture_mode_needs_no_network():
    """build_client() in offline mode must serve recorded JSON from disk."""
    async with build_client(
        config=cfg(esi_live=False, fixture_dir="tests/fixtures"), cache=Cache(None)
    ) as esi:
        page = await esi.get("/v1/status/")

    assert page.data["players"] > 0


async def test_fixture_transport_reports_missing_fixture():
    transport = FixtureTransport("tests/fixtures")
    async with EsiClient(cache=Cache(None), config=cfg(), transport=transport) as esi:
        with pytest.raises(EsiError) as excinfo:
            await esi.get("/v1/does/not/exist/")
    assert excinfo.value.status == 404
