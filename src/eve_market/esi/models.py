"""Pydantic models mirroring the ESI market schemas."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


class MarketOrder(BaseModel):
    """One order from /markets/{region_id}/orders/."""

    order_id: int
    type_id: int
    location_id: int
    system_id: int | None = None
    is_buy_order: bool
    price: float
    volume_remain: int
    volume_total: int
    min_volume: int = 1
    duration: int
    issued: datetime
    # "station", "region", "solarsystem", or a jump count as a string.
    range: str = "station"


class HistoryDay(BaseModel):
    """One day from /markets/{region_id}/history/."""

    date: date
    average: float
    highest: float
    lowest: float
    order_count: int
    volume: int


class AdjustedPrice(BaseModel):
    """One entry from /markets/prices/ (used for industry cost indices)."""

    type_id: int
    adjusted_price: float | None = None
    average_price: float | None = None


class EsiPage(BaseModel):
    """A single ESI response plus the cache metadata we care about.

    Most market endpoints return a JSON array, but some (``/status/``, single
    type lookups) return an object, so ``data`` accepts either.
    """

    data: list[dict] | dict[str, Any]
    etag: str | None = None
    expires: datetime | None = None
    pages: int = 1
    from_cache: bool = False
    # ESI's shared error budget. Drops on 4xx/5xx, refills on reset.
    error_limit_remain: int | None = Field(default=None)
    error_limit_reset: int | None = Field(default=None)
