"""Jita -> destination sourcing: what to stock, how much, and at what price.

The screen runs in three stages, matching how you'd actually think about it:

1. **Liquidity screen on Jita.** Items that move enough volume, in a price band
   worth the cargo space they take.
2. **Demand screen on the destination.** Of those, the ones where buyers are
   competing and sellers aren't — many active buy orders, thin or absent sell
   orders. That gap is the whole opportunity.
3. **Pricing.** What you'd have to list at to undercut the market, what that
   nets after fees and hauling, and whether it clears your cost floor.

A key asymmetry the tool reports separately: **buy orders at the destination
are a demand signal, not your customers.** People with buy orders up want the
item cheaper than it's being sold. When you list a sell order you're serving a
different buyer — the one who wants it *now*. So sizing comes from the
destination region's traded volume, not from buy-order depth.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..esi.models import HistoryDay, MarketOrder
from .logistics import HaulProfile
from .margins import average_daily_volume


@dataclass(slots=True)
class BookSide:
    """One side of an order book, summarised."""

    order_count: int
    volume: int
    best_price: float | None

    @property
    def is_empty(self) -> bool:
        return self.order_count == 0


@dataclass(slots=True)
class Opportunity:
    type_id: int

    # Source (Jita)
    jita_sell: float | None  # lowest sell — buy instantly at this
    jita_buy: float | None  # highest buy — what you'd outbid to buy patiently
    jita_daily_volume: float

    # Destination
    dest_buy: BookSide
    dest_sell: BookSide
    dest_daily_volume: float

    # Economics
    acquire_price: float  # what you actually pay per unit at source
    haul_cost_per_unit: float
    landed_cost: float  # acquire + haul
    floor_price: float  # minimum list price that breaks even after fees
    list_price: float  # what we recommend listing at
    net_revenue: float  # list_price after broker fee and sales tax
    profit_per_unit: float
    margin: float

    # Selling into existing buy orders instead of listing
    instant_sale_price: float | None
    instant_profit_per_unit: float | None

    # Sizing and ranking
    unit_volume_m3: float | None
    profit_per_m3: float | None
    suggested_qty: int
    total_profit: float
    total_m3: float | None

    # Signals
    no_competition: bool  # nobody is selling this at the destination
    demand_ratio: float  # destination buy volume / sell volume
    undercuts_floor: bool  # market price is below your break-even

    @property
    def viable(self) -> bool:
        return self.profit_per_unit > 0 and not self.undercuts_floor


def summarize_side(orders: list[MarketOrder], *, buy: bool) -> BookSide:
    """Count, depth and best price for one side of a book."""
    side = [o for o in orders if o.is_buy_order is buy]
    if not side:
        return BookSide(0, 0, None)
    best = max(o.price for o in side) if buy else min(o.price for o in side)
    return BookSide(
        order_count=len(side),
        volume=sum(o.volume_remain for o in side),
        best_price=best,
    )


def in_system(orders: list[MarketOrder], system_id: int | None) -> list[MarketOrder]:
    """Narrow a region-wide book to one solar system.

    Region order pulls cover the whole region, but "the Ahbazon market" means
    the orders sitting in that one system. Orders with no system id (which
    shouldn't normally happen) are dropped rather than silently included.
    """
    if system_id is None:
        return orders
    return [o for o in orders if o.system_id == system_id]


def liquidity_screen(
    histories: dict[int, list[HistoryDay]],
    prices: dict[int, float],
    *,
    min_daily_volume: float = 50.0,
    min_price: float = 100.0,
    max_price: float | None = None,
    min_daily_isk: float = 0.0,
) -> list[int]:
    """Stage 1: liquid items in a sensible price band.

    ``min_price`` filters out the bulk commodities where hauling cost swamps
    any margin; ``max_price`` guards against tying your capital up in a
    handful of very expensive units.
    """
    out = []
    for type_id, history in histories.items():
        price = prices.get(type_id)
        if price is None or price < min_price:
            continue
        if max_price is not None and price > max_price:
            continue
        volume = average_daily_volume(history)
        if volume < min_daily_volume:
            continue
        if volume * price < min_daily_isk:
            continue
        out.append(type_id)
    return out


def evaluate(
    type_id: int,
    source_orders: list[MarketOrder],
    dest_orders: list[MarketOrder],
    *,
    haul: HaulProfile,
    sales_tax: float,
    broker_fee: float,
    source_station_id: int | None = None,
    dest_system_id: int | None = None,
    source_daily_volume: float = 0.0,
    dest_daily_volume: float = 0.0,
    unit_volume_m3: float | None = None,
    buy_with_orders: bool = False,
    undercut_isk: float = 0.01,
    greenfield_markup: float = 0.35,
    days_of_stock: float = 7.0,
    capture_rate: float = 0.25,
    cargo_m3: float | None = None,
) -> Opportunity | None:
    """Evaluate stocking one item at the destination. ``None`` if unsourceable.

    ``buy_with_orders`` picks the acquisition strategy: buying instantly off
    sell orders costs more but is immediate; placing a buy order costs the
    broker fee and requires patience, but is usually meaningfully cheaper.

    ``greenfield_markup`` only applies when nobody is selling at the
    destination and you're setting the price yourself.
    """
    src = source_orders if source_station_id is None else [
        o for o in source_orders if o.location_id == source_station_id
    ]
    dst = in_system(dest_orders, dest_system_id)

    src_sell = summarize_side(src, buy=False)
    src_buy = summarize_side(src, buy=True)
    dest_sell = summarize_side(dst, buy=False)
    dest_buy = summarize_side(dst, buy=True)

    # --- acquisition --------------------------------------------------
    if buy_with_orders:
        if src_buy.best_price is None:
            return None
        # Outbid the current best buy order, and pay the broker fee to place it.
        acquire = (src_buy.best_price + undercut_isk) * (1 + broker_fee)
    else:
        if src_sell.best_price is None:
            return None
        acquire = src_sell.best_price

    haul_cost = haul.unit_cost(unit_volume_m3, acquire)
    landed = acquire + haul_cost

    # Break-even list price: what you must charge so that after the broker fee
    # and sales tax you still recover the landed cost.
    fee_multiplier = 1 - broker_fee - sales_tax
    if fee_multiplier <= 0:
        return None
    floor = landed / fee_multiplier

    # --- pricing ------------------------------------------------------
    no_competition = dest_sell.is_empty
    if no_competition:
        # You set the market. Anchor on source price plus a markup rather than
        # inventing a number, and never go below the floor.
        list_price = max(acquire * (1 + greenfield_markup), floor)
        undercuts_floor = False
    else:
        assert dest_sell.best_price is not None
        list_price = dest_sell.best_price - undercut_isk
        undercuts_floor = list_price < floor

    net = list_price * fee_multiplier
    profit = net - landed
    margin = profit / landed if landed > 0 else 0.0

    # --- selling straight into buy orders -----------------------------
    instant_price = dest_buy.best_price
    instant_profit = None
    if instant_price is not None:
        # Filling someone's buy order pays sales tax but no broker fee.
        instant_profit = instant_price * (1 - sales_tax) - landed

    # --- sizing -------------------------------------------------------
    # Sized off destination turnover, not buy-order depth: buy orders are
    # demand signal, but they're bids below market, not your customers.
    by_turnover = dest_daily_volume * days_of_stock * capture_rate
    qty = max(int(by_turnover), 1)
    if unit_volume_m3 and cargo_m3:
        qty = min(qty, max(int(cargo_m3 / unit_volume_m3 + 1e-9), 1))

    total_m3 = qty * unit_volume_m3 if unit_volume_m3 else None
    profit_per_m3 = (profit / unit_volume_m3) if unit_volume_m3 else None

    demand_ratio = dest_buy.volume / dest_sell.volume if dest_sell.volume else float("inf")

    return Opportunity(
        type_id=type_id,
        jita_sell=src_sell.best_price,
        jita_buy=src_buy.best_price,
        jita_daily_volume=source_daily_volume,
        dest_buy=dest_buy,
        dest_sell=dest_sell,
        dest_daily_volume=dest_daily_volume,
        acquire_price=acquire,
        haul_cost_per_unit=haul_cost,
        landed_cost=landed,
        floor_price=floor,
        list_price=list_price,
        net_revenue=net,
        profit_per_unit=profit,
        margin=margin,
        instant_sale_price=instant_price,
        instant_profit_per_unit=instant_profit,
        unit_volume_m3=unit_volume_m3,
        profit_per_m3=profit_per_m3,
        suggested_qty=qty,
        total_profit=profit * qty,
        total_m3=total_m3,
        no_competition=no_competition,
        demand_ratio=demand_ratio,
        undercuts_floor=undercuts_floor,
    )


def rank(
    opportunities: list[Opportunity],
    *,
    min_margin: float = 0.10,
    min_profit_per_unit: float = 0.0,
    min_demand_ratio: float = 0.0,
    require_viable: bool = True,
    by: str = "profit_per_m3",
) -> list[Opportunity]:
    """Filter and order opportunities.

    Default ranking is profit per m3: cargo space is the constraint on every
    haul, so the best item is the one that earns most per unit of hold, not
    the one with the biggest headline margin.
    """
    out = [
        o
        for o in opportunities
        if (not require_viable or o.viable)
        and o.margin >= min_margin
        and o.profit_per_unit >= min_profit_per_unit
        and o.demand_ratio >= min_demand_ratio
    ]
    if by == "profit_per_m3" and any(o.profit_per_m3 is not None for o in out):
        return sorted(out, key=lambda o: o.profit_per_m3 or 0.0, reverse=True)
    if by == "margin":
        return sorted(out, key=lambda o: o.margin, reverse=True)
    return sorted(out, key=lambda o: o.total_profit, reverse=True)


def fit_to_hold(
    opportunities: list[Opportunity], cargo_m3: float
) -> list[Opportunity]:
    """Greedily fill a hold with the best ISK/m3 items that fit.

    A greedy pass by profit density is the right call here — this is a
    fractional-knapsack in practice, since you can always take fewer units of
    an item rather than all or nothing.
    """
    remaining = cargo_m3
    chosen: list[Opportunity] = []
    for opp in opportunities:
        if opp.unit_volume_m3 is None or opp.unit_volume_m3 <= 0:
            continue
        affordable = int(remaining / opp.unit_volume_m3 + 1e-9)
        if affordable <= 0:
            continue
        qty = min(opp.suggested_qty, affordable)
        if qty <= 0:
            continue
        opp.suggested_qty = qty
        opp.total_profit = opp.profit_per_unit * qty
        opp.total_m3 = qty * opp.unit_volume_m3
        remaining -= opp.total_m3
        chosen.append(opp)
        if remaining <= 0:
            break
    return chosen
