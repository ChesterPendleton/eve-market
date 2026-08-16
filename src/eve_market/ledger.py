"""Cost basis tracking: what you paid, what you must charge, what you made.

Three prices matter when you're deciding what to list at, and they are not the
same number:

* **Landed cost** — what the stock in your hangar actually cost, including the
  broker fee to buy it and its share of the haul. Sunk.
* **Break-even price** — the list price at which, after broker fee and sales
  tax, you recover that landed cost. This is your historical floor.
* **Replacement price** — what it would cost to buy and haul that item *again*
  at today's Jita price. This is the floor that actually matters.

Selling above break-even but below replacement feels profitable and quietly
shrinks the business: every sale funds less stock than it consumed. So the
recommended floor is the higher of the two, which is what ``price_floor``
returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .analysis.logistics import HaulProfile, allocate_by_volume
from .db import Database


@dataclass(slots=True)
class Lot:
    id: int
    type_id: int
    qty: int
    qty_remaining: int
    unit_price: float
    fees: float
    haul_cost: float
    acquired_at: datetime

    @property
    def landed_total(self) -> float:
        return self.unit_price * self.qty + self.fees + self.haul_cost

    @property
    def unit_landed_cost(self) -> float:
        return self.landed_total / self.qty if self.qty else 0.0


@dataclass(slots=True)
class Position:
    type_id: int
    qty_on_hand: int
    avg_landed_cost: float
    capital_tied_up: float


@dataclass(slots=True)
class PriceGuide:
    """Everything needed to decide a list price for one item."""

    type_id: int
    qty_on_hand: int
    avg_landed_cost: float
    break_even_price: float
    replacement_price: float | None
    floor_price: float  # max(break_even, replacement)
    market_price: float | None  # current best sell at destination
    suggested_price: float | None
    beats_floor: bool


class Ledger:
    """Cost basis operations over the Postgres tables."""

    def __init__(self, db: Database):
        self.db = db

    # -- purchases -------------------------------------------------------

    async def record_purchase(
        self,
        type_id: int,
        qty: int,
        unit_price: float,
        *,
        broker_fee: float = 0.0,
        station_id: int | None = None,
        note: str | None = None,
    ) -> int:
        """Record buying ``qty`` units. Returns the new lot id.

        ``broker_fee`` is the *rate*; the ISK amount is derived, and is zero
        when you bought instantly off someone's sell order rather than placing
        your own buy order.
        """
        if qty <= 0:
            raise ValueError("qty must be positive")
        pool = self.db._require_pool()
        fees = unit_price * qty * broker_fee
        return await pool.fetchval(
            "INSERT INTO purchase_lot "
            "(type_id, qty, qty_remaining, unit_price, fees, station_id, note) "
            "VALUES ($1,$2,$2,$3,$4,$5,$6) RETURNING id",
            type_id,
            qty,
            round(unit_price, 2),
            round(fees, 2),
            station_id,
            note,
        )

    async def open_lots(self, type_id: int | None = None) -> list[Lot]:
        """Lots with stock left, oldest first (FIFO order)."""
        pool = self.db._require_pool()
        rows = await pool.fetch(
            "SELECT id, type_id, qty, qty_remaining, unit_price, fees, haul_cost, "
            "acquired_at FROM purchase_lot "
            "WHERE qty_remaining > 0 AND ($1::int IS NULL OR type_id = $1) "
            "ORDER BY acquired_at, id",
            type_id,
        )
        return [
            Lot(
                id=r["id"],
                type_id=r["type_id"],
                qty=r["qty"],
                qty_remaining=r["qty_remaining"],
                unit_price=float(r["unit_price"]),
                fees=float(r["fees"]),
                haul_cost=float(r["haul_cost"]),
                acquired_at=r["acquired_at"],
            )
            for r in rows
        ]

    # -- hauling ---------------------------------------------------------

    async def assign_haul_cost(
        self, lot_ids: list[int], total_cost: float, volumes: dict[int, float]
    ) -> dict[int, float]:
        """Spread one trip's cost across the lots it carried, by m3.

        ``volumes`` maps type_id to unit m3. Lots whose type has no known
        volume fall back to an even split rather than being carried free.
        """
        if not lot_ids:
            return {}
        pool = self.db._require_pool()
        rows = await pool.fetch(
            "SELECT id, type_id, qty FROM purchase_lot WHERE id = ANY($1::bigint[])",
            lot_ids,
        )
        items = [(r["id"], r["qty"] * volumes.get(r["type_id"], 0.0)) for r in rows]
        allocation = allocate_by_volume(total_cost, items)
        await pool.executemany(
            "UPDATE purchase_lot SET haul_cost = haul_cost + $2 WHERE id = $1",
            [(lot_id, round(cost, 2)) for lot_id, cost in allocation.items()],
        )
        return allocation

    # -- positions and pricing -------------------------------------------

    async def position(self, type_id: int) -> Position:
        lots = await self.open_lots(type_id)
        qty = sum(lot.qty_remaining for lot in lots)
        if qty == 0:
            return Position(type_id, 0, 0.0, 0.0)
        # Weighted by units still held, not units originally bought.
        capital = sum(lot.unit_landed_cost * lot.qty_remaining for lot in lots)
        return Position(type_id, qty, capital / qty, capital)

    async def positions(self) -> list[Position]:
        lots = await self.open_lots()
        by_type: dict[int, list[Lot]] = {}
        for lot in lots:
            by_type.setdefault(lot.type_id, []).append(lot)
        out = []
        for type_id, group in by_type.items():
            qty = sum(lot.qty_remaining for lot in group)
            capital = sum(lot.unit_landed_cost * lot.qty_remaining for lot in group)
            out.append(Position(type_id, qty, capital / qty if qty else 0.0, capital))
        return sorted(out, key=lambda p: p.capital_tied_up, reverse=True)

    async def price_guide(
        self,
        type_id: int,
        *,
        sales_tax: float,
        broker_fee: float,
        replacement_unit_cost: float | None = None,
        haul: HaulProfile | None = None,
        unit_volume_m3: float | None = None,
        market_price: float | None = None,
        undercut_isk: float = 0.01,
    ) -> PriceGuide:
        """Work out what this item must sell for, and what it can sell for.

        ``replacement_unit_cost`` is today's Jita acquisition price. Pass it and
        the restock floor is included; omit it and only the historical
        break-even is used.
        """
        pos = await self.position(type_id)
        fee_multiplier = 1 - sales_tax - broker_fee
        if fee_multiplier <= 0:
            raise ValueError("sales tax and broker fee exceed 100%")

        break_even = pos.avg_landed_cost / fee_multiplier if pos.qty_on_hand else 0.0

        replacement_price = None
        if replacement_unit_cost is not None:
            landed_replacement = replacement_unit_cost
            if haul is not None:
                landed_replacement += haul.unit_cost(unit_volume_m3, replacement_unit_cost)
            replacement_price = landed_replacement / fee_multiplier

        floor = max(break_even, replacement_price or 0.0)

        suggested = None
        if market_price is not None:
            undercut = market_price - undercut_isk
            # Never suggest a price that loses money just to win the undercut.
            suggested = undercut if undercut >= floor else None
        elif floor > 0:
            suggested = floor

        return PriceGuide(
            type_id=type_id,
            qty_on_hand=pos.qty_on_hand,
            avg_landed_cost=pos.avg_landed_cost,
            break_even_price=break_even,
            replacement_price=replacement_price,
            floor_price=floor,
            market_price=market_price,
            suggested_price=suggested,
            beats_floor=suggested is not None,
        )

    # -- sales -----------------------------------------------------------

    async def record_sale(
        self,
        type_id: int,
        qty: int,
        unit_price: float,
        *,
        sales_tax: float,
        broker_fee: float = 0.0,
        station_id: int | None = None,
    ) -> dict[str, float]:
        """Record a sale, consuming lots FIFO. Returns the realised numbers.

        ``broker_fee`` is zero when you sold into someone's buy order, since
        you never placed an order of your own.
        """
        if qty <= 0:
            raise ValueError("qty must be positive")
        pool = self.db._require_pool()

        async with pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "SELECT id, qty, qty_remaining, unit_price, fees, haul_cost "
                "FROM purchase_lot WHERE type_id = $1 AND qty_remaining > 0 "
                "ORDER BY acquired_at, id FOR UPDATE",
                type_id,
            )
            available = sum(r["qty_remaining"] for r in rows)
            if available < qty:
                raise ValueError(
                    f"only {available} units of type {type_id} on hand, tried to sell {qty}"
                )

            gross = unit_price * qty
            fees = gross * (sales_tax + broker_fee)

            remaining = qty
            cogs = 0.0
            consumed: list[tuple[int, int, float]] = []
            for r in rows:
                if remaining <= 0:
                    break
                take = min(remaining, r["qty_remaining"])
                unit_landed = (
                    float(r["unit_price"]) * r["qty"] + float(r["fees"]) + float(r["haul_cost"])
                ) / r["qty"]
                cogs += unit_landed * take
                consumed.append((r["id"], take, unit_landed))
                remaining -= take

            sale_id = await conn.fetchval(
                "INSERT INTO sale "
                "(type_id, qty, unit_price, gross, fees, cogs, realized_profit, station_id) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id",
                type_id,
                qty,
                round(unit_price, 2),
                round(gross, 2),
                round(fees, 2),
                round(cogs, 2),
                round(gross - fees - cogs, 2),
                station_id,
            )
            await conn.executemany(
                "UPDATE purchase_lot SET qty_remaining = qty_remaining - $2 WHERE id = $1",
                [(lot_id, take) for lot_id, take, _ in consumed],
            )
            await conn.executemany(
                "INSERT INTO lot_consumption (sale_id, lot_id, qty, unit_landed_cost) "
                "VALUES ($1,$2,$3,$4)",
                [(sale_id, lot_id, take, round(cost, 2)) for lot_id, take, cost in consumed],
            )

        return {
            "sale_id": float(sale_id),
            "gross": gross,
            "fees": fees,
            "cogs": cogs,
            "profit": gross - fees - cogs,
            "margin": (gross - fees - cogs) / cogs if cogs else 0.0,
        }

    async def realized_pnl(self, type_id: int | None = None) -> dict[str, float]:
        """Totals across recorded sales."""
        pool = self.db._require_pool()
        row = await pool.fetchrow(
            "SELECT COALESCE(SUM(gross),0) AS gross, COALESCE(SUM(fees),0) AS fees, "
            "COALESCE(SUM(cogs),0) AS cogs, COALESCE(SUM(realized_profit),0) AS profit, "
            "COUNT(*) AS sales FROM sale WHERE ($1::int IS NULL OR type_id = $1)",
            type_id,
        )
        assert row is not None
        return {
            "gross": float(row["gross"]),
            "fees": float(row["fees"]),
            "cogs": float(row["cogs"]),
            "profit": float(row["profit"]),
            "sales": float(row["sales"]),
        }
