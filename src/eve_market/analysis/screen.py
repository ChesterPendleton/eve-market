"""The stock screen's data-gathering, shared by the CLI and the dashboard.

`sourcing` holds the per-item economics; this module holds the orchestration
around it — candidate discovery, history lookups, evaluation, ranking and
hold-fitting — so the terminal and the web UI cannot drift apart. Both are
promised to be "the same brain"; this is the brain.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import settings
from ..db import Database
from . import sourcing
from .logistics import HaulProfile


@dataclass(slots=True)
class StockFilters:
    buy_orders: bool = False
    min_margin: float = 0.10
    min_volume: float = 20.0
    min_price: float = 1000.0
    max_price: float | None = None
    undersupplied: bool = False
    fit: bool = True
    limit: int = 25


@dataclass(slots=True)
class StockScreen:
    opportunities: list[sourcing.Opportunity] = field(default_factory=list)
    names: dict[int, str] = field(default_factory=dict)
    error: str | None = None
    hint: str | None = None


async def stock_screen(
    db: Database,
    profile: HaulProfile,
    capacity: float,
    filters: StockFilters,
) -> StockScreen:
    """Everything the stock table shows, for whichever surface renders it."""
    src_snap = await db.latest_snapshot(settings.source_region_id)
    dst_snap = await db.latest_snapshot(settings.dest_region_id)
    if src_snap is None or dst_snap is None:
        return StockScreen(
            error="need snapshots of both the source and destination regions",
            hint="Snapshot both regions, then fetch history for each.",
        )

    pool = db._require_pool()
    # Candidates: anything quoted at the destination system, either side. A
    # buy order with no matching sell order is the best case, so we must not
    # require both sides to exist.
    dest_types = [
        r["type_id"]
        for r in await pool.fetch(
            "SELECT DISTINCT type_id FROM market_order "
            "WHERE snapshot_id = $1 AND ($2::int = 0 OR system_id = $2)",
            dst_snap,
            settings.dest_system_id,
        )
    ]
    if not dest_types:
        return StockScreen(
            error="no orders found at the destination system",
            hint="Check EVE_DEST_SYSTEM_ID (run `eve-market resolve`), or the "
            "market may be a citadel — see EVE_DEST_STRUCTURE_ID or "
            "`import-marketlog`.",
        )

    volumes = await db.type_volumes(dest_types)
    opportunities = []
    for type_id in dest_types:
        src_orders = await db.orders_for_type(src_snap, type_id)
        if not src_orders:
            continue
        src_vol = sourcing.average_daily_volume(
            await db.history_for_type(settings.source_region_id, type_id)
        )
        if src_vol < filters.min_volume:
            continue
        opp = sourcing.evaluate(
            type_id,
            src_orders,
            await db.orders_for_type(dst_snap, type_id),
            haul=profile,
            sales_tax=settings.sales_tax,
            broker_fee=settings.broker_fee,
            source_station_id=settings.source_station_id,
            dest_system_id=settings.dest_system_id or None,
            source_daily_volume=src_vol,
            dest_daily_volume=sourcing.average_daily_volume(
                await db.history_for_type(settings.dest_region_id, type_id)
            ),
            unit_volume_m3=volumes.get(type_id),
            buy_with_orders=filters.buy_orders,
            undercut_isk=settings.undercut_isk,
            greenfield_markup=settings.greenfield_markup,
            days_of_stock=settings.days_of_stock,
            capture_rate=settings.capture_rate,
            cargo_m3=capacity,
        )
        if opp is None or opp.acquire_price < filters.min_price:
            continue
        if filters.max_price is not None and opp.acquire_price > filters.max_price:
            continue
        opportunities.append(opp)

    best = sourcing.rank(
        opportunities,
        min_margin=filters.min_margin,
        min_demand_ratio=1.0 if filters.undersupplied else 0.0,
    )
    if filters.fit:
        best = sourcing.fit_to_hold(best, capacity)
    best = best[: filters.limit]
    names = await db.type_names([o.type_id for o in best])
    return StockScreen(opportunities=best, names=names)
