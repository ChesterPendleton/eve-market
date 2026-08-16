from __future__ import annotations

from eve_market.analysis.hauling import find_route, rank

from .conftest import make_order

FORGE, DOMAIN = 10000002, 10000043


def test_route_charges_sales_tax_but_no_broker_fee_when_filling_buy_orders():
    source = [make_order(order_id=1, is_buy_order=False, price=100.0, volume_remain=500)]
    dest = [make_order(order_id=2, is_buy_order=True, price=200.0, volume_remain=500)]

    route = find_route(34, source, dest, FORGE, DOMAIN, sales_tax=0.05)
    assert route is not None
    # 200 * 0.95 - 100 = 90
    assert route.profit_per_unit == 90.0


def test_route_charges_broker_fee_when_listing_own_sell_orders():
    source = [make_order(order_id=1, is_buy_order=False, price=100.0)]
    dest = [make_order(order_id=2, is_buy_order=True, price=200.0)]

    route = find_route(
        34, source, dest, FORGE, DOMAIN,
        sales_tax=0.05, broker_fee=0.03, sell_to_buy_orders=False,
    )
    assert route is not None
    # 200 * (1 - 0.05 - 0.03) - 100 = 84
    assert route.profit_per_unit == 84.0


def test_units_limited_by_thinner_side():
    source = [make_order(order_id=1, is_buy_order=False, price=100.0, volume_remain=50)]
    dest = [make_order(order_id=2, is_buy_order=True, price=200.0, volume_remain=1000)]

    route = find_route(34, source, dest, FORGE, DOMAIN, sales_tax=0.0)
    assert route is not None
    assert route.units_available == 50


def test_units_limited_by_cargo_capacity():
    source = [make_order(order_id=1, is_buy_order=False, price=100.0, volume_remain=10_000)]
    dest = [make_order(order_id=2, is_buy_order=True, price=200.0, volume_remain=10_000)]

    route = find_route(
        34, source, dest, FORGE, DOMAIN,
        sales_tax=0.0, unit_volume_m3=0.01, cargo_m3=60.0,
    )
    assert route is not None
    assert route.units_available == 6000
    assert route.total_volume_m3 == 60.0


def test_unprofitable_route_returns_none():
    source = [make_order(order_id=1, is_buy_order=False, price=200.0)]
    dest = [make_order(order_id=2, is_buy_order=True, price=100.0)]
    assert find_route(34, source, dest, FORGE, DOMAIN) is None


def test_missing_side_returns_none():
    source = [make_order(order_id=1, is_buy_order=True, price=100.0)]  # no sell orders
    dest = [make_order(order_id=2, is_buy_order=True, price=200.0)]
    assert find_route(34, source, dest, FORGE, DOMAIN) is None


def test_isk_per_m3_ranks_above_raw_profit():
    bulky = find_route(
        1,
        [make_order(order_id=1, is_buy_order=False, price=100.0, volume_remain=1000)],
        [make_order(order_id=2, is_buy_order=True, price=300.0, volume_remain=1000)],
        FORGE, DOMAIN, sales_tax=0.0, unit_volume_m3=100.0, cargo_m3=1_000_000,
    )
    compact = find_route(
        2,
        [make_order(order_id=3, is_buy_order=False, price=100.0, volume_remain=500)],
        [make_order(order_id=4, is_buy_order=True, price=200.0, volume_remain=500)],
        FORGE, DOMAIN, sales_tax=0.0, unit_volume_m3=0.1, cargo_m3=1_000_000,
    )
    assert bulky is not None and compact is not None
    # Bulky makes more total ISK, but compact is far better per m3 of hold.
    assert bulky.total_profit > compact.total_profit
    assert rank([bulky, compact], min_total_profit=0)[0].type_id == 2
    assert rank([bulky, compact], min_total_profit=0, by="total_profit")[0].type_id == 1


def test_rank_drops_routes_below_profit_floor():
    small = find_route(
        1,
        [make_order(order_id=1, is_buy_order=False, price=100.0, volume_remain=1)],
        [make_order(order_id=2, is_buy_order=True, price=110.0, volume_remain=1)],
        FORGE, DOMAIN, sales_tax=0.0,
    )
    assert small is not None
    assert rank([small], min_total_profit=1_000_000) == []
