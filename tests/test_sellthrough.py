"""Sell-through: measured demand from your own closed orders."""

from datetime import UTC, datetime

import pytest

from eve_market.analysis import sellthrough
from eve_market.esi import character
from eve_market.esi.client import build_client

NOW = datetime(2026, 8, 16, tzinfo=UTC)


@pytest.mark.asyncio
async def test_wrapper_parses_history_fixture():
    async with build_client() as esi:
        closed = await character.my_order_history(esi, 12345678)
    assert len(closed) == 4
    by_id = {o.order_id: o for o in closed}
    assert by_id[800001].sold_out is True
    assert by_id[800001].units_sold == 100
    assert by_id[800002].units_sold == 15
    assert by_id[800003].state == "cancelled"


@pytest.mark.asyncio
async def test_summarize_measures_per_type():
    async with build_client() as esi:
        closed = await character.my_order_history(esi, 12345678)
    stats = sellthrough.summarize(closed, now=NOW)
    by_type = {s.type_id: s for s in stats}

    wd = by_type[3244]
    # The buy order is acquisition, not demand — excluded entirely.
    assert wd.orders_closed == 2
    assert wd.sold_out == 1
    assert wd.expired_unsold == 1
    assert wd.units_listed == 150
    assert wd.units_sold == 115
    # 115 sold, none cancelled: fill rate is measured over everything listed.
    assert wd.fill_rate == pytest.approx(115 / 150)
    # Window runs from the OLDEST closed order: May 1 08:00 -> Aug 16 00:00.
    assert wd.window_days == pytest.approx(106.67, abs=0.05)
    assert wd.daily_velocity == pytest.approx(115 / wd.window_days)


@pytest.mark.asyncio
async def test_cancelled_orders_carry_no_verdict():
    async with build_client() as esi:
        closed = await character.my_order_history(esi, 12345678)
    stats = {s.type_id: s for s in sellthrough.summarize(closed, now=NOW)}
    dc = stats[2048]
    # The one DC II order was cancelled: sold units still count toward
    # velocity, but the fill rate has no denominator left to judge.
    assert dc.cancelled == 1
    assert dc.units_sold == 80
    assert dc.fill_rate is None


def test_empty_history_is_empty():
    assert sellthrough.summarize([], now=NOW) == []


@pytest.mark.asyncio
async def test_ranking_is_by_units_sold():
    async with build_client() as esi:
        closed = await character.my_order_history(esi, 12345678)
    stats = sellthrough.summarize(closed, now=NOW)
    # WD II sold 115 units, DC II sold 80: most-moved first.
    assert [s.type_id for s in stats] == [3244, 2048]
    assert stats[0].units_sold >= stats[1].units_sold
