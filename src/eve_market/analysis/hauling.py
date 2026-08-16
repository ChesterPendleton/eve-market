"""Cross-region arbitrage: buy in one hub, haul, sell in another.

Fee model differs from station trading because you fill existing orders rather
than placing your own:

* Buying from a sell order costs nothing beyond the price.
* Selling *into* a buy order pays sales tax but no broker fee, since you never
  placed an order.

If you'd rather list your own sell orders at the destination, pass
``sell_to_buy_orders=False`` and the broker fee is applied as well.

The binding constraint on hauling is almost always cargo volume, not ISK, so
ISK-per-m3 is the metric that actually ranks these — raw profit does not.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import settings as default_settings
from ..esi.models import MarketOrder


@dataclass(slots=True)
class HaulRoute:
    type_id: int
    source_region: int
    dest_region: int
    buy_price: float  # what you pay at the source
    sell_price: float  # what you receive at the destination
    profit_per_unit: float
    margin: float
    units_available: int  # limited by both source supply and destination demand
    unit_volume_m3: float | None
    total_profit: float
    total_volume_m3: float | None

    @property
    def isk_per_m3(self) -> float | None:
        if not self.total_volume_m3:
            return None
        return self.total_profit / self.total_volume_m3


def find_route(
    type_id: int,
    source_orders: list[MarketOrder],
    dest_orders: list[MarketOrder],
    source_region: int,
    dest_region: int,
    *,
    unit_volume_m3: float | None = None,
    cargo_m3: float | None = None,
    sales_tax: float | None = None,
    broker_fee: float | None = None,
    sell_to_buy_orders: bool = True,
) -> HaulRoute | None:
    """Best single-type haul between two regions, or ``None`` if unprofitable."""
    cfg = default_settings
    tax = cfg.sales_tax if sales_tax is None else sales_tax
    broker = 0.0 if sell_to_buy_orders else (cfg.broker_fee if broker_fee is None else broker_fee)

    source_sells = [o for o in source_orders if not o.is_buy_order]
    dest_buys = [o for o in dest_orders if o.is_buy_order]
    if not source_sells or not dest_buys:
        return None

    buy_price = min(o.price for o in source_sells)
    sell_price = max(o.price for o in dest_buys)

    profit = sell_price * (1 - tax - broker) - buy_price
    if profit <= 0:
        return None

    # You can only move as much as the source will sell you at that price and
    # the destination will buy from you at theirs.
    supply = sum(o.volume_remain for o in source_sells if o.price <= buy_price)
    demand = sum(o.volume_remain for o in dest_buys if o.price >= sell_price)
    units = min(supply, demand)

    if unit_volume_m3 and cargo_m3:
        # Nudge before truncating: unit volumes are small decimals that don't
        # round-trip in binary, so 60.0 // 0.01 evaluates to 5999.0, not 6000.
        units = min(units, int(cargo_m3 / unit_volume_m3 + 1e-9))
    if units <= 0:
        return None

    total_volume = units * unit_volume_m3 if unit_volume_m3 else None
    return HaulRoute(
        type_id=type_id,
        source_region=source_region,
        dest_region=dest_region,
        buy_price=buy_price,
        sell_price=sell_price,
        profit_per_unit=profit,
        margin=profit / buy_price if buy_price > 0 else 0.0,
        units_available=units,
        unit_volume_m3=unit_volume_m3,
        total_profit=profit * units,
        total_volume_m3=total_volume,
    )


def rank(
    routes: list[HaulRoute],
    *,
    min_total_profit: float = 1_000_000.0,
    by: str = "isk_per_m3",
) -> list[HaulRoute]:
    """Rank routes, preferring ISK/m3 when volumes are known."""
    out = [r for r in routes if r.total_profit >= min_total_profit]
    if by == "isk_per_m3" and any(r.isk_per_m3 is not None for r in out):
        return sorted(out, key=lambda r: r.isk_per_m3 or 0.0, reverse=True)
    return sorted(out, key=lambda r: r.total_profit, reverse=True)
