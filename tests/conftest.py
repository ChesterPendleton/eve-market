from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from eve_market.cache import Cache
from eve_market.esi.models import HistoryDay, MarketOrder


@pytest.fixture
def memory_cache() -> Cache:
    """Cache with no Redis URL, so it uses the in-memory fallback."""
    return Cache(url=None)


def make_order(
    *,
    order_id: int = 1,
    type_id: int = 34,
    location_id: int = 60003760,
    is_buy_order: bool = False,
    price: float = 100.0,
    volume_remain: int = 1000,
) -> MarketOrder:
    return MarketOrder(
        order_id=order_id,
        type_id=type_id,
        location_id=location_id,
        system_id=30000142,
        is_buy_order=is_buy_order,
        price=price,
        volume_remain=volume_remain,
        volume_total=volume_remain,
        min_volume=1,
        duration=90,
        issued=datetime.now(UTC),
        range="station",
    )


def make_history(days: int = 30, volume: int = 1000, average: float = 100.0) -> list[HistoryDay]:
    start = date.today() - timedelta(days=days)
    return [
        HistoryDay(
            date=start + timedelta(days=i),
            average=average,
            highest=average * 1.1,
            lowest=average * 0.9,
            order_count=50,
            volume=volume,
        )
        for i in range(days)
    ]
