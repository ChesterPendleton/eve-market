"""Station-trading analysis: buy low on a buy order, sell high on a sell order.

Fee model (matches current TQ mechanics):

* Placing a buy order costs the broker fee on the order's value.
* Placing a sell order costs the broker fee again, and the sale itself is
  charged sales tax.

So per unit::

    cost    = buy_price  * (1 + broker_fee)
    revenue = sell_price * (1 - broker_fee - sales_tax)

Both rates depend on skills and standings, which is why they're configuration
rather than constants. Defaults assume Accounting V and decent Broker Relations.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import settings as default_settings
from ..esi.models import HistoryDay, MarketOrder


@dataclass(slots=True)
class Spread:
    """A tradeable spread for one type at one station."""

    type_id: int
    location_id: int
    buy_price: float  # highest buy order — what you'd outbid
    sell_price: float  # lowest sell order — what you'd undercut
    profit_per_unit: float
    margin: float  # profit as a fraction of capital deployed
    daily_volume: float  # units/day, averaged over the history window
    daily_profit: float  # profit_per_unit * volume you could realistically win
    buy_depth: int  # units on competing buy orders
    sell_depth: int  # units on competing sell orders

    @property
    def margin_pct(self) -> float:
        return self.margin * 100.0


def _best_prices(
    orders: list[MarketOrder], location_id: int
) -> tuple[float | None, float | None, int, int]:
    """Highest buy and lowest sell at one station, with their depths."""
    buys = [o for o in orders if o.is_buy_order and o.location_id == location_id]
    sells = [o for o in orders if not o.is_buy_order and o.location_id == location_id]
    best_buy = max((o.price for o in buys), default=None)
    best_sell = min((o.price for o in sells), default=None)
    return (
        best_buy,
        best_sell,
        sum(o.volume_remain for o in buys),
        sum(o.volume_remain for o in sells),
    )


def average_daily_volume(history: list[HistoryDay], window: int = 30) -> float:
    """Mean units traded per day over the most recent ``window`` days."""
    if not history:
        return 0.0
    recent = sorted(history, key=lambda h: h.date)[-window:]
    return sum(h.volume for h in recent) / len(recent)


def compute_spread(
    type_id: int,
    location_id: int,
    orders: list[MarketOrder],
    history: list[HistoryDay] | None = None,
    *,
    broker_fee: float | None = None,
    sales_tax: float | None = None,
    capture_rate: float = 0.1,
) -> Spread | None:
    """Build a :class:`Spread`, or ``None`` if the item isn't two-sided here.

    ``capture_rate`` is the share of daily volume you assume you can actually
    win against competing traders. Ten percent is a deliberately conservative
    default — assuming you capture the whole market is the classic way these
    screeners produce numbers that never materialise.
    """
    cfg = default_settings
    broker = cfg.broker_fee if broker_fee is None else broker_fee
    tax = cfg.sales_tax if sales_tax is None else sales_tax

    best_buy, best_sell, buy_depth, sell_depth = _best_prices(orders, location_id)
    if best_buy is None or best_sell is None:
        return None
    if best_sell <= best_buy:
        # Crossed or locked book: no spread to trade.
        return None

    cost = best_buy * (1 + broker)
    revenue = best_sell * (1 - broker - tax)
    profit = revenue - cost
    margin = profit / cost if cost > 0 else 0.0

    volume = average_daily_volume(history or [])
    return Spread(
        type_id=type_id,
        location_id=location_id,
        buy_price=best_buy,
        sell_price=best_sell,
        profit_per_unit=profit,
        margin=margin,
        daily_volume=volume,
        daily_profit=profit * volume * capture_rate,
        buy_depth=buy_depth,
        sell_depth=sell_depth,
    )


def screen(
    spreads: list[Spread],
    *,
    min_margin: float = 0.05,
    min_daily_volume: float = 10.0,
    min_profit_per_unit: float = 0.0,
    max_capital_per_unit: float | None = None,
) -> list[Spread]:
    """Filter and rank spreads by expected daily profit.

    The volume floor matters more than the margin floor: a 40% margin on an
    item that trades twice a week is not a trade, it's a hobby.
    """
    out = [
        s
        for s in spreads
        if s.margin >= min_margin
        and s.daily_volume >= min_daily_volume
        and s.profit_per_unit >= min_profit_per_unit
        and (max_capital_per_unit is None or s.buy_price <= max_capital_per_unit)
    ]
    return sorted(out, key=lambda s: s.daily_profit, reverse=True)
