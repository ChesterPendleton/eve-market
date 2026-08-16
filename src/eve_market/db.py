"""Postgres persistence for market snapshots and history."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, Self

import asyncpg

from .esi.models import HistoryDay, MarketOrder

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "sql" / "schema.sql"


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self.pool is None:
            self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=8)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _require_pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("Database.connect() has not been awaited")
        return self.pool

    async def migrate(self) -> None:
        """Apply the schema. Safe to run repeatedly."""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(SCHEMA_PATH.read_text())

    async def save_snapshot(self, region_id: int, orders: Sequence[MarketOrder]) -> int:
        """Persist a whole order book as one snapshot; returns its id."""
        pool = self._require_pool()
        async with pool.acquire() as conn, conn.transaction():
            snapshot_id: int = await conn.fetchval(
                "INSERT INTO market_snapshot (region_id, order_count) "
                "VALUES ($1, $2) RETURNING id",
                region_id,
                len(orders),
            )
            # copy_records_to_table is dramatically faster than executemany for
            # the 100k+ row books the big regions produce.
            await conn.copy_records_to_table(
                "market_order",
                records=[
                    (
                        snapshot_id,
                        o.order_id,
                        o.type_id,
                        o.location_id,
                        o.system_id,
                        o.is_buy_order,
                        round(o.price, 2),
                        o.volume_remain,
                        o.volume_total,
                        o.min_volume,
                        o.duration,
                        o.issued,
                        o.range,
                    )
                    for o in orders
                ],
                columns=[
                    "snapshot_id",
                    "order_id",
                    "type_id",
                    "location_id",
                    "system_id",
                    "is_buy_order",
                    "price",
                    "volume_remain",
                    "volume_total",
                    "min_volume",
                    "duration",
                    "issued",
                    "range",
                ],
            )
        return snapshot_id

    async def latest_snapshot(self, region_id: int) -> int | None:
        pool = self._require_pool()
        return await pool.fetchval(
            "SELECT id FROM market_snapshot WHERE region_id = $1 "
            "ORDER BY taken_at DESC LIMIT 1",
            region_id,
        )

    async def orders_for_type(self, snapshot_id: int, type_id: int) -> list[MarketOrder]:
        pool = self._require_pool()
        rows = await pool.fetch(
            "SELECT order_id, type_id, location_id, system_id, is_buy_order, "
            "price, volume_remain, volume_total, min_volume, duration, issued, range "
            "FROM market_order WHERE snapshot_id = $1 AND type_id = $2",
            snapshot_id,
            type_id,
        )
        return [MarketOrder.model_validate({**dict(r), "price": float(r["price"])}) for r in rows]

    async def save_history(
        self, region_id: int, type_id: int, days: Sequence[HistoryDay]
    ) -> int:
        """Upsert daily aggregates; returns the number of rows written."""
        if not days:
            return 0
        pool = self._require_pool()
        await pool.executemany(
            "INSERT INTO market_history "
            "(region_id, type_id, date, average, highest, lowest, order_count, volume) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) "
            "ON CONFLICT (region_id, type_id, date) DO UPDATE SET "
            "average = EXCLUDED.average, highest = EXCLUDED.highest, "
            "lowest = EXCLUDED.lowest, order_count = EXCLUDED.order_count, "
            "volume = EXCLUDED.volume",
            [
                (
                    region_id,
                    type_id,
                    d.date,
                    round(d.average, 2),
                    round(d.highest, 2),
                    round(d.lowest, 2),
                    d.order_count,
                    d.volume,
                )
                for d in days
            ],
        )
        return len(days)

    async def history_for_type(
        self, region_id: int, type_id: int, since: date | None = None
    ) -> list[HistoryDay]:
        pool = self._require_pool()
        rows = await pool.fetch(
            "SELECT date, average, highest, lowest, order_count, volume "
            "FROM market_history WHERE region_id = $1 AND type_id = $2 "
            "AND ($3::date IS NULL OR date >= $3) ORDER BY date",
            region_id,
            type_id,
            since,
        )
        return [
            HistoryDay.model_validate(
                {
                    **dict(r),
                    "average": float(r["average"]),
                    "highest": float(r["highest"]),
                    "lowest": float(r["lowest"]),
                }
            )
            for r in rows
        ]

    async def type_names(self, type_ids: Sequence[int]) -> dict[int, str]:
        """Resolve type ids to names, for whatever the SDE load covered."""
        if not type_ids:
            return {}
        pool = self._require_pool()
        rows = await pool.fetch(
            "SELECT type_id, type_name FROM inv_type WHERE type_id = ANY($1::int[])",
            list(type_ids),
        )
        return {r["type_id"]: r["type_name"] for r in rows}

    async def type_volumes(self, type_ids: Sequence[int]) -> dict[int, float]:
        """Packaged volume per type, for hauling capacity maths."""
        if not type_ids:
            return {}
        pool = self._require_pool()
        rows = await pool.fetch(
            "SELECT type_id, COALESCE(packaged_volume, volume) AS v "
            "FROM inv_type WHERE type_id = ANY($1::int[])",
            list(type_ids),
        )
        return {r["type_id"]: float(r["v"]) for r in rows if r["v"] is not None}

    async def upsert_closed_orders(self, orders: Sequence[Any]) -> int:
        """Store closed orders from the history endpoint; upsert by order id.

        ESI forgets after ~90 days; this table is what makes sell-through
        measurable over a longer horizon.
        """
        if not orders:
            return 0
        pool = self._require_pool()
        existing = await pool.fetchval(
            "SELECT COUNT(*) FROM character_order_history WHERE order_id = ANY($1::bigint[])",
            [o.order_id for o in orders],
        )
        await pool.executemany(
            "INSERT INTO character_order_history "
            "(order_id, type_id, region_id, location_id, is_buy_order, price, "
            " volume_total, volume_remain, duration, issued, state) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) "
            "ON CONFLICT (order_id) DO UPDATE SET "
            "volume_remain = EXCLUDED.volume_remain, state = EXCLUDED.state",
            [
                (
                    o.order_id,
                    o.type_id,
                    o.region_id,
                    o.location_id,
                    o.is_buy_order,
                    round(o.price, 2),
                    o.volume_total,
                    o.volume_remain,
                    o.duration,
                    o.issued,
                    o.state,
                )
                for o in orders
            ],
        )
        return len(orders) - existing

    async def closed_orders(self) -> list[dict[str, Any]]:
        """Everything the order-history sync has accumulated."""
        pool = self._require_pool()
        rows = await pool.fetch(
            "SELECT order_id, type_id, region_id, location_id, is_buy_order, "
            "price, volume_total, volume_remain, duration, issued, state "
            "FROM character_order_history ORDER BY issued"
        )
        return [{**dict(r), "price": float(r["price"])} for r in rows]

    async def upsert_types(self, rows: Sequence[dict[str, Any]]) -> int:
        """Load the SDE slice used by naming and hauling."""
        if not rows:
            return 0
        pool = self._require_pool()
        await pool.executemany(
            "INSERT INTO inv_type "
            "(type_id, type_name, group_id, volume, packaged_volume, published) "
            "VALUES ($1,$2,$3,$4,$5,$6) "
            "ON CONFLICT (type_id) DO UPDATE SET "
            "type_name = EXCLUDED.type_name, group_id = EXCLUDED.group_id, "
            "volume = EXCLUDED.volume, packaged_volume = EXCLUDED.packaged_volume, "
            "published = EXCLUDED.published",
            [
                (
                    r["type_id"],
                    r["type_name"],
                    r.get("group_id"),
                    r.get("volume"),
                    r.get("packaged_volume"),
                    r.get("published", True),
                )
                for r in rows
            ],
        )
        return len(rows)
