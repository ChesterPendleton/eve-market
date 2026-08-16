from __future__ import annotations

from datetime import UTC, datetime

from eve_market.analysis import relist
from eve_market.analysis.relist import Status
from eve_market.esi.character import CharacterOrder

from .conftest import make_order

STATION = 60003760
OTHER_STATION = 60008494


def my_order(
    *,
    order_id: int = 100,
    type_id: int = 34,
    price: float = 100.0,
    is_buy: bool = False,
    remaining: int = 50,
    location_id: int = STATION,
) -> CharacterOrder:
    return CharacterOrder(
        order_id=order_id,
        type_id=type_id,
        region_id=10000002,
        location_id=location_id,
        is_buy_order=is_buy,
        price=price,
        volume_total=100,
        volume_remain=remaining,
        min_volume=1,
        duration=90,
        issued=datetime.now(UTC),
        range="station",
    )


def test_sell_order_at_the_top_needs_no_action():
    mine = my_order(price=100.0)
    book = [make_order(order_id=1, is_buy_order=False, price=120.0)]

    action = relist.evaluate_order(mine, book)
    assert action.status is Status.BEST
    assert not action.needs_action
    assert action.suggested_price is None


def test_undercut_sell_order_gets_a_relist_price():
    mine = my_order(price=100.0)
    book = [make_order(order_id=1, is_buy_order=False, price=90.0)]

    action = relist.evaluate_order(mine, book, undercut_isk=0.01)
    assert action.status is Status.UNDERCUT
    assert action.needs_action
    assert action.suggested_price == 89.99
    assert action.best_competing == 90.0


def test_your_own_order_is_never_your_competition():
    """The book contains your order too; matching against it would loop."""
    mine = my_order(order_id=777, price=100.0)
    book = [make_order(order_id=777, is_buy_order=False, price=100.0)]

    action = relist.evaluate_order(mine, book)
    assert action.status is Status.ALONE
    assert action.best_competing is None


def test_orders_at_other_stations_do_not_count_as_competition():
    mine = my_order(price=100.0)
    book = [
        make_order(order_id=1, is_buy_order=False, price=50.0, location_id=OTHER_STATION)
    ]
    action = relist.evaluate_order(mine, book)
    assert action.status is Status.ALONE


def test_buy_orders_do_not_compete_with_sell_orders():
    mine = my_order(price=100.0, is_buy=False)
    book = [make_order(order_id=1, is_buy_order=True, price=99.0)]
    assert relist.evaluate_order(mine, book).status is Status.ALONE


def test_outbid_buy_order_is_raised_not_lowered():
    mine = my_order(price=100.0, is_buy=True)
    book = [make_order(order_id=1, is_buy_order=True, price=110.0)]

    action = relist.evaluate_order(mine, book, undercut_isk=0.01)
    assert action.status is Status.OUTBID
    assert action.suggested_price == 110.01  # outbid upward


def test_winning_buy_order_is_left_alone():
    mine = my_order(price=120.0, is_buy=True)
    book = [make_order(order_id=1, is_buy_order=True, price=110.0)]
    assert relist.evaluate_order(mine, book).status is Status.BEST


def test_relisting_below_the_cost_floor_is_refused():
    mine = my_order(price=100.0)
    book = [make_order(order_id=1, is_buy_order=False, price=80.0)]

    action = relist.evaluate_order(mine, book, floor_price=95.0)
    assert action.status is Status.BELOW_FLOOR
    assert action.suggested_price is None
    assert not action.needs_action


def test_buy_order_will_not_be_bid_above_the_cap():
    mine = my_order(price=100.0, is_buy=True)
    book = [make_order(order_id=1, is_buy_order=True, price=110.0)]

    action = relist.evaluate_order(mine, book, max_bid=105.0)
    assert action.status is Status.BELOW_FLOOR
    assert action.suggested_price is None


def test_worklist_puts_the_costliest_order_first():
    """Ranked by total ISK at stake, not by per-unit gap."""
    big_gap_few_units = my_order(order_id=1, type_id=1, price=200.0, remaining=1)
    small_gap_many_units = my_order(order_id=2, type_id=2, price=101.0, remaining=1000)
    book = [
        make_order(order_id=10, type_id=1, is_buy_order=False, price=100.0),
        make_order(order_id=11, type_id=2, is_buy_order=False, price=100.0),
    ]

    worklist = relist.build_worklist([big_gap_few_units, small_gap_many_units], book)
    # 1 unit x ~100 gap vs 1000 units x ~1 gap — the latter costs more overall.
    assert worklist[0].type_id == 2


def test_worklist_applies_per_type_floors():
    a = my_order(order_id=1, type_id=1, price=100.0)
    b = my_order(order_id=2, type_id=2, price=100.0)
    book = [
        make_order(order_id=10, type_id=1, is_buy_order=False, price=80.0),
        make_order(order_id=11, type_id=2, is_buy_order=False, price=80.0),
    ]

    worklist = relist.build_worklist([a, b], book, floors={1: 95.0})
    by_type = {w.type_id: w for w in worklist}
    assert by_type[1].status is Status.BELOW_FLOOR
    assert by_type[2].status is Status.UNDERCUT


def test_summarize_counts_each_status():
    mine = [my_order(order_id=1, type_id=1, price=100.0)]
    book = [make_order(order_id=10, type_id=1, is_buy_order=False, price=90.0)]
    counts = relist.summarize(relist.build_worklist(mine, book))
    assert counts == {"undercut": 1}


def test_sell_orders_default_to_not_buy_when_esi_omits_the_field():
    """ESI leaves is_buy_order out entirely on sell orders."""
    order = CharacterOrder.model_validate(
        {
            "order_id": 1,
            "type_id": 34,
            "region_id": 10000002,
            "location_id": STATION,
            "price": 100.0,
            "volume_total": 10,
            "volume_remain": 10,
            "duration": 90,
            "issued": "2026-08-16T00:00:00Z",
        }
    )
    assert order.is_buy_order is False
