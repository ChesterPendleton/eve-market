"""Cost basis tests. Skipped when Postgres isn't reachable."""

from __future__ import annotations

import os

import pytest

from eve_market.analysis.logistics import profile_for
from eve_market.db import Database
from eve_market.ledger import Ledger

# A SEPARATE database: these tests TRUNCATE, and pointing them at the
# working database would destroy your ledger and snapshots.
DSN = os.environ.get(
    "EVE_TEST_DATABASE_URL", "postgresql://eve:eve@localhost:5432/eve_market_test"
)

TRIT = 34
PLEX = 44992


@pytest.fixture
async def ledger():
    try:
        db = Database(DSN)
        await db.connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"postgres unavailable: {exc}")

    await db.migrate()
    pool = db.pool
    assert pool is not None
    await pool.execute("TRUNCATE sale, lot_consumption, purchase_lot RESTART IDENTITY CASCADE")
    await pool.execute("TRUNCATE inv_type")
    yield Ledger(db)
    await db.close()


async def test_purchase_creates_a_lot_with_no_fee_when_bought_instantly(ledger: Ledger):
    await ledger.record_purchase(TRIT, 100, 10.0)
    lots = await ledger.open_lots(TRIT)
    assert len(lots) == 1
    assert lots[0].fees == 0.0
    assert lots[0].unit_landed_cost == 10.0


async def test_buy_order_purchase_adds_the_broker_fee_to_cost(ledger: Ledger):
    await ledger.record_purchase(TRIT, 100, 10.0, broker_fee=0.02)
    lots = await ledger.open_lots(TRIT)
    # 100 * 10 = 1000, plus 2% = 20 in fees, so 10.20 landed per unit.
    assert lots[0].fees == 20.0
    assert lots[0].unit_landed_cost == 10.2


async def test_haul_cost_is_allocated_by_volume_not_evenly(ledger: Ledger):
    bulky = await ledger.record_purchase(TRIT, 1000, 10.0)  # 1000 * 1 m3
    compact = await ledger.record_purchase(PLEX, 100, 10.0)  # 100 * 0.01 m3

    allocation = await ledger.assign_haul_cost(
        [bulky, compact], 1000.0, {TRIT: 1.0, PLEX: 0.01}
    )
    # 1000 m3 vs 1 m3 — the bulky lot should carry essentially all of it.
    assert allocation[bulky] > 999
    assert allocation[compact] < 1
    assert round(sum(allocation.values()), 2) == 1000.0

    lots = {lot.type_id: lot for lot in await ledger.open_lots()}
    assert lots[TRIT].haul_cost >= 999


async def test_position_weights_by_units_still_held(ledger: Ledger):
    await ledger.record_purchase(TRIT, 100, 10.0)
    await ledger.record_purchase(TRIT, 100, 20.0)

    pos = await ledger.position(TRIT)
    assert pos.qty_on_hand == 200
    assert pos.avg_landed_cost == 15.0
    assert pos.capital_tied_up == 3000.0


async def test_sale_consumes_lots_fifo(ledger: Ledger):
    await ledger.record_purchase(TRIT, 100, 10.0)  # cheap, bought first
    await ledger.record_purchase(TRIT, 100, 30.0)  # dear, bought second

    result = await ledger.record_sale(TRIT, 100, 50.0, sales_tax=0.0, broker_fee=0.0)
    # FIFO must consume the 10 ISK lot, not average with the 30 ISK one.
    assert result["cogs"] == 1000.0
    assert result["profit"] == 4000.0

    pos = await ledger.position(TRIT)
    assert pos.qty_on_hand == 100
    assert pos.avg_landed_cost == 30.0


async def test_sale_spanning_two_lots_blends_their_costs(ledger: Ledger):
    await ledger.record_purchase(TRIT, 100, 10.0)
    await ledger.record_purchase(TRIT, 100, 30.0)

    result = await ledger.record_sale(TRIT, 150, 50.0, sales_tax=0.0, broker_fee=0.0)
    # 100 units at 10 plus 50 units at 30 = 2500
    assert result["cogs"] == 2500.0


async def test_sale_charges_tax_and_broker_fee(ledger: Ledger):
    await ledger.record_purchase(TRIT, 100, 10.0)
    result = await ledger.record_sale(TRIT, 100, 100.0, sales_tax=0.05, broker_fee=0.03)
    assert result["gross"] == 10_000.0
    assert result["fees"] == 800.0
    assert result["profit"] == 10_000.0 - 800.0 - 1000.0


async def test_selling_into_a_buy_order_skips_the_broker_fee(ledger: Ledger):
    await ledger.record_purchase(TRIT, 200, 10.0)
    listed = await ledger.record_sale(TRIT, 100, 100.0, sales_tax=0.05, broker_fee=0.03)
    filled = await ledger.record_sale(TRIT, 100, 100.0, sales_tax=0.05, broker_fee=0.0)
    assert filled["fees"] < listed["fees"]
    assert filled["profit"] > listed["profit"]


async def test_overselling_is_refused(ledger: Ledger):
    await ledger.record_purchase(TRIT, 10, 10.0)
    with pytest.raises(ValueError, match="only 10 units"):
        await ledger.record_sale(TRIT, 11, 50.0, sales_tax=0.0)
    # The failed sale must not have consumed anything.
    assert (await ledger.position(TRIT)).qty_on_hand == 10


async def test_break_even_price_covers_fees(ledger: Ledger):
    await ledger.record_purchase(TRIT, 100, 90.0)
    guide = await ledger.price_guide(TRIT, sales_tax=0.05, broker_fee=0.05)
    # 90 / (1 - 0.10) = 100
    assert guide.break_even_price == pytest.approx(100.0)

    # Selling at exactly break-even must net zero against landed cost.
    result = await ledger.record_sale(
        TRIT, 100, guide.break_even_price, sales_tax=0.05, broker_fee=0.05
    )
    assert round(result["profit"], 6) == 0.0


async def test_floor_uses_replacement_cost_when_restocking_got_dearer(ledger: Ledger):
    """The whole point of restock pricing: old cheap stock must not be sold
    at a price that can't fund its replacement."""
    await ledger.record_purchase(TRIT, 100, 50.0)  # bought cheap
    guide = await ledger.price_guide(
        TRIT, sales_tax=0.0, broker_fee=0.0, replacement_unit_cost=200.0
    )
    assert guide.break_even_price == 50.0
    assert guide.replacement_price == 200.0
    assert guide.floor_price == 200.0


async def test_floor_keeps_break_even_when_restocking_got_cheaper(ledger: Ledger):
    await ledger.record_purchase(TRIT, 100, 200.0)
    guide = await ledger.price_guide(
        TRIT, sales_tax=0.0, broker_fee=0.0, replacement_unit_cost=50.0
    )
    assert guide.floor_price == 200.0  # your actual cost still governs


async def test_replacement_price_includes_the_haul(ledger: Ledger):
    await ledger.record_purchase(TRIT, 100, 10.0)
    haul = profile_for("dst", route_is_lowsec=True, cost_per_m3=0.0, risk_pct=0.10)
    guide = await ledger.price_guide(
        TRIT, sales_tax=0.0, broker_fee=0.0,
        replacement_unit_cost=100.0, haul=haul, unit_volume_m3=1.0,
    )
    assert guide.replacement_price == 110.0  # 100 + 10% risk


async def test_no_suggestion_when_the_market_sits_below_your_floor(ledger: Ledger):
    await ledger.record_purchase(TRIT, 100, 100.0)
    guide = await ledger.price_guide(
        TRIT, sales_tax=0.0, broker_fee=0.0, market_price=50.0
    )
    assert guide.suggested_price is None
    assert not guide.beats_floor


async def test_suggestion_undercuts_when_the_market_is_above_your_floor(ledger: Ledger):
    await ledger.record_purchase(TRIT, 100, 100.0)
    guide = await ledger.price_guide(
        TRIT, sales_tax=0.0, broker_fee=0.0, market_price=500.0, undercut_isk=0.01
    )
    assert guide.suggested_price == 499.99
    assert guide.beats_floor


async def test_realized_pnl_totals_across_sales(ledger: Ledger):
    await ledger.record_purchase(TRIT, 100, 10.0)
    await ledger.record_sale(TRIT, 50, 100.0, sales_tax=0.0, broker_fee=0.0)
    await ledger.record_sale(TRIT, 50, 200.0, sales_tax=0.0, broker_fee=0.0)

    totals = await ledger.realized_pnl(TRIT)
    assert totals["sales"] == 2
    assert totals["gross"] == 15_000.0
    assert totals["cogs"] == 1000.0
    assert totals["profit"] == 14_000.0
