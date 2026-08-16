"""How fast stock actually sells, measured from your closed orders.

``--days-of-stock`` and the stock screen's demand column are estimates from
market history. Your own closed sell orders are ground truth: every one says
how many units were listed, how many actually sold, and whether the order
sold out or died on the wall. That difference is the whole point:

- **sold out** (volume_remain 0): the market absorbed everything you listed —
  you could probably carry more.
- **expired with stock**: 90 days on the wall and it didn't clear — the real
  demand is lower than the screen suggested, or the price was wrong.
- **cancelled**: no verdict either way; excluded from fill rates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from ..esi.character import ClosedOrder


@dataclass(slots=True)
class SellThrough:
    type_id: int
    orders_closed: int
    sold_out: int
    expired_unsold: int
    cancelled: int
    units_listed: int
    units_sold: int
    window_days: float

    @property
    def fill_rate(self) -> float | None:
        """Share of listed units that sold, cancelled orders excluded."""
        listed = self.units_listed - self._cancelled_units
        return self._sold_excl_cancelled / listed if listed else None

    @property
    def daily_velocity(self) -> float:
        """Units per day your orders actually moved across the window."""
        return self.units_sold / self.window_days if self.window_days else 0.0

    # Stored at build time because the dataclass keeps aggregates, not orders.
    _sold_excl_cancelled: int = 0
    _cancelled_units: int = 0


def summarize(
    closed: list[ClosedOrder], *, now: datetime | None = None
) -> list[SellThrough]:
    """Per-type sell-through from closed sell orders, most-moved first.

    Buy orders are excluded: a filled buy order is acquisition, not demand.
    """
    now = now or datetime.now(UTC)
    sells = [o for o in closed if not o.is_buy_order]
    by_type: dict[int, list[ClosedOrder]] = {}
    for o in sells:
        by_type.setdefault(o.type_id, []).append(o)

    out: list[SellThrough] = []
    for type_id, orders in by_type.items():
        first = min(o.issued for o in orders)
        window = max((now - first).total_seconds() / 86400, 1.0)
        cancelled = [o for o in orders if o.state == "cancelled"]
        st = SellThrough(
            type_id=type_id,
            orders_closed=len(orders),
            sold_out=sum(1 for o in orders if o.sold_out),
            expired_unsold=sum(
                1 for o in orders if o.state == "expired" and not o.sold_out
            ),
            cancelled=len(cancelled),
            units_listed=sum(o.volume_total for o in orders),
            units_sold=sum(o.units_sold for o in orders),
            window_days=window,
        )
        st._sold_excl_cancelled = sum(o.units_sold for o in orders if o.state != "cancelled")
        st._cancelled_units = sum(o.volume_total for o in cancelled)
        out.append(st)
    return sorted(out, key=lambda s: s.units_sold, reverse=True)
