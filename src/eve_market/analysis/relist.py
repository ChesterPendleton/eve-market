"""Compare your live orders against the market and decide what to relist.

The relist loop is where a trading operation actually leaks ISK. An order that
has been undercut sits there earning nothing while the seller below you clears
the demand; an order you've *already* got at the top costs you nothing to leave
alone, and relisting it burns a broker fee for no gain.

So each order lands in one of four states, and only two of them are work.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..esi.character import CharacterOrder
from ..esi.models import MarketOrder


class Status(str, Enum):
    BEST = "best"  # you're at the top of the book; nothing to do
    UNDERCUT = "undercut"  # someone beat you; relist to regain the top
    OUTBID = "outbid"  # buy-order equivalent of undercut
    BELOW_FLOOR = "below_floor"  # regaining the top would lose money
    ALONE = "alone"  # no competition at this station


@dataclass(slots=True)
class RelistAction:
    order: CharacterOrder
    type_id: int
    status: Status
    my_price: float
    best_competing: float | None
    suggested_price: float | None
    floor_price: float | None
    # Positive means you'd gain this much per unit by relisting.
    gain_per_unit: float | None

    @property
    def needs_action(self) -> bool:
        return self.status in (Status.UNDERCUT, Status.OUTBID)

    @property
    def units_at_stake(self) -> int:
        return self.order.volume_remain


def _competitors(
    book: list[MarketOrder], order: CharacterOrder
) -> list[MarketOrder]:
    """Orders competing with yours: same station, same side, not your own."""
    return [
        o
        for o in book
        if o.type_id == order.type_id
        and o.location_id == order.location_id
        and o.is_buy_order == order.is_buy_order
        and o.order_id != order.order_id
    ]


def evaluate_order(
    order: CharacterOrder,
    book: list[MarketOrder],
    *,
    undercut_isk: float = 0.01,
    floor_price: float | None = None,
    max_bid: float | None = None,
) -> RelistAction:
    """Decide what, if anything, to do about one of your orders.

    ``floor_price`` applies to sell orders: the price below which relisting
    would lose money. ``max_bid`` is its buy-side twin — the most you're
    willing to pay before a buy order stops being worth winning.
    """
    rivals = _competitors(book, order)

    if not rivals:
        return RelistAction(
            order=order,
            type_id=order.type_id,
            status=Status.ALONE,
            my_price=order.price,
            best_competing=None,
            suggested_price=None,
            floor_price=floor_price,
            gain_per_unit=None,
        )

    if order.is_buy_order:
        best = max(o.price for o in rivals)
        winning = order.price >= best
        target = best + undercut_isk
        blocked = max_bid is not None and target > max_bid
        gain = None if winning else target - order.price
    else:
        best = min(o.price for o in rivals)
        winning = order.price <= best
        target = best - undercut_isk
        blocked = floor_price is not None and target < floor_price
        gain = None if winning else order.price - target

    if winning:
        status = Status.BEST
        suggested = None
    elif blocked:
        status = Status.BELOW_FLOOR
        suggested = None
    else:
        status = Status.OUTBID if order.is_buy_order else Status.UNDERCUT
        suggested = target

    return RelistAction(
        order=order,
        type_id=order.type_id,
        status=status,
        my_price=order.price,
        best_competing=best,
        suggested_price=suggested,
        floor_price=floor_price if not order.is_buy_order else max_bid,
        gain_per_unit=gain,
    )


def build_worklist(
    orders: list[CharacterOrder],
    book: list[MarketOrder],
    *,
    undercut_isk: float = 0.01,
    floors: dict[int, float] | None = None,
    max_bids: dict[int, float] | None = None,
) -> list[RelistAction]:
    """Evaluate every order, worst first.

    Sorted by total ISK at stake — units remaining times the per-unit gain —
    so the order costing you the most sits at the top of the list.
    """
    floors = floors or {}
    max_bids = max_bids or {}
    actions = [
        evaluate_order(
            order,
            book,
            undercut_isk=undercut_isk,
            floor_price=floors.get(order.type_id),
            max_bid=max_bids.get(order.type_id),
        )
        for order in orders
    ]
    return sorted(
        actions,
        key=lambda a: (a.gain_per_unit or 0.0) * a.units_at_stake,
        reverse=True,
    )


def summarize(actions: list[RelistAction]) -> dict[str, int]:
    """Count actions by status, for a one-line summary."""
    counts: dict[str, int] = {}
    for action in actions:
        counts[action.status.value] = counts.get(action.status.value, 0) + 1
    return counts
