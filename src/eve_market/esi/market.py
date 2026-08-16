"""Typed wrappers around the public ESI market endpoints.

None of these require authentication — public market data is open. SSO is only
needed for character-owned orders, wallet and player-structure markets.
"""

from __future__ import annotations

import asyncio

from .client import EsiClient
from .models import AdjustedPrice, HistoryDay, MarketOrder


async def structure_book(
    esi: EsiClient, structure_id: int, system_id: int | None = None
) -> list[MarketOrder]:
    """A player structure's order book, shaped like region orders.

    Structure orders carry no ``system_id``; the caller passes the system the
    structure lives in so downstream screens can filter by destination system
    exactly as they do for NPC stations.
    """
    rows = await esi.get_all_pages(f"/v1/markets/structures/{structure_id}/")
    out = []
    for r in rows:
        if system_id is not None:
            r = {**r, "system_id": system_id}
        out.append(MarketOrder.model_validate(r))
    return out


async def region_orders(
    esi: EsiClient,
    region_id: int,
    type_id: int | None = None,
    order_type: str = "all",
) -> list[MarketOrder]:
    """Every order in a region, optionally narrowed to one item type.

    Without ``type_id`` this walks the region's whole order book, which for
    The Forge is well over a hundred thousand orders across many pages.
    """
    params: dict[str, object] = {"order_type": order_type}
    if type_id is not None:
        params["type_id"] = type_id
    rows = await esi.get_all_pages(f"/v1/markets/{region_id}/orders/", params)
    return [MarketOrder.model_validate(r) for r in rows]


async def type_history(esi: EsiClient, region_id: int, type_id: int) -> list[HistoryDay]:
    """Up to 13 months of daily aggregates for one type in one region."""
    page = await esi.get(f"/v1/markets/{region_id}/history/", {"type_id": type_id})
    return [HistoryDay.model_validate(r) for r in page.data]


async def adjusted_prices(esi: EsiClient) -> list[AdjustedPrice]:
    """Global adjusted/average prices for every type."""
    page = await esi.get("/v1/markets/prices/")
    return [AdjustedPrice.model_validate(r) for r in page.data]


async def histories(
    esi: EsiClient, region_id: int, type_ids: list[int]
) -> dict[int, list[HistoryDay]]:
    """Fetch history for many types concurrently.

    History is one request per type, so this is the slowest part of any
    screening run; the client's semaphore keeps concurrency in bounds.
    """

    async def one(tid: int) -> tuple[int, list[HistoryDay]]:
        try:
            return tid, await type_history(esi, region_id, tid)
        except Exception:  # noqa: BLE001 - a missing type shouldn't kill the run
            return tid, []

    results = await asyncio.gather(*(one(t) for t in type_ids))
    return dict(results)
