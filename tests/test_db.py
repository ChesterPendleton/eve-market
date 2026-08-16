"""Database tests.

These need a real Postgres. They're skipped automatically when one isn't
reachable, so the rest of the suite still runs on a bare checkout.
"""

from __future__ import annotations

import os

import pytest

from eve_market.db import Database

from .conftest import make_history, make_order

# A SEPARATE database: these tests TRUNCATE, and pointing them at the
# working database would destroy your ledger and snapshots.
DSN = os.environ.get(
    "EVE_TEST_DATABASE_URL", "postgresql://eve:eve@localhost:5432/eve_market_test"
)


@pytest.fixture
async def db():
    try:
        database = Database(DSN)
        await database.connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"postgres unavailable: {exc}")

    await database.migrate()
    pool = database.pool
    assert pool is not None
    # Start each test from a clean slate; snapshots cascade to their orders.
    await pool.execute("TRUNCATE market_snapshot CASCADE")
    await pool.execute("TRUNCATE market_history")
    await pool.execute("TRUNCATE inv_type")
    yield database
    await database.close()


async def test_migrate_is_idempotent(db: Database):
    await db.migrate()
    await db.migrate()


async def test_snapshot_round_trip_preserves_orders(db: Database):
    orders = [
        make_order(order_id=1, type_id=34, is_buy_order=True, price=5.10),
        make_order(order_id=2, type_id=34, is_buy_order=False, price=5.94),
        make_order(order_id=3, type_id=35, is_buy_order=False, price=11.5),
    ]
    snapshot_id = await db.save_snapshot(10000002, orders)

    assert await db.latest_snapshot(10000002) == snapshot_id

    tritanium = await db.orders_for_type(snapshot_id, 34)
    assert len(tritanium) == 2
    assert sorted(o.price for o in tritanium) == [5.10, 5.94]
    assert {o.is_buy_order for o in tritanium} == {True, False}


async def test_latest_snapshot_returns_newest(db: Database):
    first = await db.save_snapshot(10000002, [make_order(order_id=1)])
    second = await db.save_snapshot(10000002, [make_order(order_id=2)])
    assert first != second
    assert await db.latest_snapshot(10000002) == second


async def test_latest_snapshot_is_scoped_per_region(db: Database):
    await db.save_snapshot(10000002, [make_order(order_id=1)])
    assert await db.latest_snapshot(10000043) is None


async def test_history_upsert_overwrites_same_day(db: Database):
    days = make_history(days=5, volume=100)
    assert await db.save_history(10000002, 34, days) == 5

    days[0].volume = 999
    await db.save_history(10000002, 34, days)

    stored = await db.history_for_type(10000002, 34)
    assert len(stored) == 5  # not 10 — the second write updated in place
    assert stored[0].volume == 999


async def test_type_names_and_volumes(db: Database):
    await db.upsert_types([
        {"type_id": 34, "type_name": "Tritanium", "volume": 0.01, "packaged_volume": 0.01},
        {"type_id": 44992, "type_name": "PLEX", "volume": 0.01, "packaged_volume": None},
    ])
    assert await db.type_names([34, 44992]) == {34: "Tritanium", 44992: "PLEX"}
    # packaged_volume is null for PLEX, so it must fall back to volume.
    assert await db.type_volumes([34, 44992]) == {34: 0.01, 44992: 0.01}


async def test_empty_inputs_are_no_ops(db: Database):
    assert await db.save_history(10000002, 34, []) == 0
    assert await db.upsert_types([]) == 0
    assert await db.type_names([]) == {}


async def test_closed_orders_roundtrip_and_upsert(db):
    from datetime import UTC, datetime

    from eve_market.esi.character import ClosedOrder

    order = ClosedOrder(
        order_id=800100,
        type_id=3244,
        region_id=10000067,
        location_id=60012721,
        price=1949999.99,
        volume_total=100,
        volume_remain=40,
        duration=90,
        issued=datetime(2026, 8, 1, tzinfo=UTC),
        state="expired",
    )
    assert await db.upsert_closed_orders([order]) == 1

    # The same order arriving again with more sold must update, not duplicate.
    order.volume_remain = 0
    await db.upsert_closed_orders([order])
    rows = await db.closed_orders()
    mine = [r for r in rows if r["order_id"] == 800100]
    assert len(mine) == 1
    assert mine[0]["volume_remain"] == 0
