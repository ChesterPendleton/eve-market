from __future__ import annotations

import pytest

from eve_market.analysis import sourcing
from eve_market.analysis.logistics import HaulProfile, profile_for

from .conftest import make_order

JITA = 60003760
JITA_SYSTEM = 30000142
AHBAZON_SYSTEM = 30005196

FREE_HAUL = HaulProfile(ship=profile_for("dst", route_is_lowsec=False).ship, cost_per_m3=0.0, risk_pct=0.0)


def dest_order(**kw):
    kw.setdefault("location_id", 60012345)
    order = make_order(**kw)
    return order.model_copy(update={"system_id": AHBAZON_SYSTEM})


def test_summarize_side_reports_count_depth_and_best():
    orders = [
        make_order(order_id=1, is_buy_order=True, price=100.0, volume_remain=5),
        make_order(order_id=2, is_buy_order=True, price=120.0, volume_remain=7),
        make_order(order_id=3, is_buy_order=False, price=200.0, volume_remain=3),
    ]
    buys = sourcing.summarize_side(orders, buy=True)
    assert buys.order_count == 2
    assert buys.volume == 12
    assert buys.best_price == 120.0

    sells = sourcing.summarize_side(orders, buy=False)
    assert sells.best_price == 200.0
    assert not sells.is_empty


def test_in_system_filters_region_wide_book():
    orders = [
        dest_order(order_id=1),
        make_order(order_id=2),  # Jita system
    ]
    assert len(sourcing.in_system(orders, AHBAZON_SYSTEM)) == 1
    assert len(sourcing.in_system(orders, None)) == 2


def test_undercuts_lowest_sell_at_destination():
    src = [make_order(order_id=1, is_buy_order=False, price=100.0)]
    dst = [dest_order(order_id=2, is_buy_order=False, price=200.0)]

    opp = sourcing.evaluate(
        34, src, dst, haul=FREE_HAUL, sales_tax=0.0, broker_fee=0.0,
        source_station_id=JITA, dest_system_id=AHBAZON_SYSTEM, undercut_isk=0.01,
    )
    assert opp is not None
    assert opp.list_price == 199.99
    assert opp.profit_per_unit == pytest.approx(99.99)
    assert not opp.no_competition


def test_no_sellers_at_destination_uses_markup_not_undercut():
    src = [make_order(order_id=1, is_buy_order=False, price=100.0)]
    # Buy orders only — nobody is selling, so we set the price.
    dst = [dest_order(order_id=2, is_buy_order=True, price=150.0)]

    opp = sourcing.evaluate(
        34, src, dst, haul=FREE_HAUL, sales_tax=0.0, broker_fee=0.0,
        source_station_id=JITA, dest_system_id=AHBAZON_SYSTEM, greenfield_markup=0.5,
    )
    assert opp is not None
    assert opp.no_competition
    assert opp.list_price == 150.0  # 100 * 1.5
    assert opp.demand_ratio == float("inf")


def test_list_price_never_falls_below_floor_when_greenfield():
    """A fat haul cost must push the greenfield price up, not be absorbed."""
    src = [make_order(order_id=1, is_buy_order=False, price=100.0)]
    dst = [dest_order(order_id=2, is_buy_order=True, price=150.0)]
    costly = HaulProfile(ship=FREE_HAUL.ship, cost_per_m3=0.0, risk_pct=0.5)

    opp = sourcing.evaluate(
        34, src, dst, haul=costly, sales_tax=0.0, broker_fee=0.0,
        source_station_id=JITA, dest_system_id=AHBAZON_SYSTEM, greenfield_markup=0.1,
    )
    assert opp is not None
    # Landed cost is 150 (100 + 50 risk), so a 10% markup on 100 would lose money.
    assert opp.list_price == opp.floor_price == 150.0
    assert opp.profit_per_unit == 0.0


def test_flags_when_market_price_is_below_break_even():
    src = [make_order(order_id=1, is_buy_order=False, price=100.0)]
    dst = [dest_order(order_id=2, is_buy_order=False, price=95.0)]

    opp = sourcing.evaluate(
        34, src, dst, haul=FREE_HAUL, sales_tax=0.05, broker_fee=0.03,
        source_station_id=JITA, dest_system_id=AHBAZON_SYSTEM,
    )
    assert opp is not None
    assert opp.undercuts_floor
    assert not opp.viable
    assert opp.profit_per_unit < 0


def test_buy_order_acquisition_outbids_and_pays_broker_fee():
    src = [
        make_order(order_id=1, is_buy_order=False, price=100.0),
        make_order(order_id=2, is_buy_order=True, price=80.0),
    ]
    dst = [dest_order(order_id=3, is_buy_order=False, price=200.0)]

    instant = sourcing.evaluate(
        34, src, dst, haul=FREE_HAUL, sales_tax=0.0, broker_fee=0.02,
        source_station_id=JITA, dest_system_id=AHBAZON_SYSTEM, buy_with_orders=False,
    )
    patient = sourcing.evaluate(
        34, src, dst, haul=FREE_HAUL, sales_tax=0.0, broker_fee=0.02,
        source_station_id=JITA, dest_system_id=AHBAZON_SYSTEM, buy_with_orders=True,
        undercut_isk=0.01,
    )
    assert instant is not None and patient is not None
    assert instant.acquire_price == 100.0
    # (80 + 0.01) * 1.02
    assert round(patient.acquire_price, 4) == 81.6102
    assert patient.profit_per_unit > instant.profit_per_unit


def test_haul_risk_is_charged_on_cargo_value():
    src = [make_order(order_id=1, is_buy_order=False, price=1000.0)]
    dst = [dest_order(order_id=2, is_buy_order=False, price=2000.0)]
    risky = HaulProfile(ship=FREE_HAUL.ship, cost_per_m3=10.0, risk_pct=0.15)

    opp = sourcing.evaluate(
        34, src, dst, haul=risky, sales_tax=0.0, broker_fee=0.0,
        source_station_id=JITA, dest_system_id=AHBAZON_SYSTEM, unit_volume_m3=2.0,
    )
    assert opp is not None
    # freight 2 m3 * 10 = 20, risk 15% of 1000 = 150
    assert opp.haul_cost_per_unit == 170.0
    assert opp.landed_cost == 1170.0


def test_sizing_comes_from_turnover_not_buy_order_depth():
    """Buy orders are a demand signal, not customers for your sell order."""
    src = [make_order(order_id=1, is_buy_order=False, price=100.0)]
    dst = [
        dest_order(order_id=2, is_buy_order=False, price=200.0),
        dest_order(order_id=3, is_buy_order=True, price=50.0, volume_remain=999_999),
    ]
    opp = sourcing.evaluate(
        34, src, dst, haul=FREE_HAUL, sales_tax=0.0, broker_fee=0.0,
        source_station_id=JITA, dest_system_id=AHBAZON_SYSTEM,
        dest_daily_volume=100.0, days_of_stock=7.0, capture_rate=0.25,
    )
    assert opp is not None
    # 100/day * 7 days * 25% capture = 175, not the 999,999 sitting in bids.
    assert opp.suggested_qty == 175


def test_instant_sale_into_buy_orders_pays_tax_but_no_broker_fee():
    src = [make_order(order_id=1, is_buy_order=False, price=100.0)]
    dst = [
        dest_order(order_id=2, is_buy_order=False, price=200.0),
        dest_order(order_id=3, is_buy_order=True, price=150.0),
    ]
    opp = sourcing.evaluate(
        34, src, dst, haul=FREE_HAUL, sales_tax=0.10, broker_fee=0.05,
        source_station_id=JITA, dest_system_id=AHBAZON_SYSTEM,
    )
    assert opp is not None
    assert opp.instant_sale_price == 150.0
    # 150 * 0.9 - 100 = 35, with no broker fee deducted
    assert opp.instant_profit_per_unit == 35.0


def test_source_orders_outside_the_hub_station_are_ignored():
    src = [make_order(order_id=1, is_buy_order=False, price=100.0, location_id=60099999)]
    dst = [dest_order(order_id=2, is_buy_order=False, price=200.0)]
    assert (
        sourcing.evaluate(
            34, src, dst, haul=FREE_HAUL, sales_tax=0.0, broker_fee=0.0,
            source_station_id=JITA, dest_system_id=AHBAZON_SYSTEM,
        )
        is None
    )


def test_liquidity_screen_applies_volume_and_price_band():
    from .conftest import make_history

    histories = {
        1: make_history(volume=1000),  # liquid, in band
        2: make_history(volume=1),  # too thin
        3: make_history(volume=1000),  # too cheap
        4: make_history(volume=1000),  # too dear
    }
    prices = {1: 5000.0, 2: 5000.0, 3: 1.0, 4: 10_000_000.0}
    kept = sourcing.liquidity_screen(
        histories, prices, min_daily_volume=50, min_price=100, max_price=1_000_000
    )
    assert kept == [1]


def test_rank_prefers_profit_density_over_headline_profit():
    src = [make_order(order_id=1, is_buy_order=False, price=100.0)]
    bulky = sourcing.evaluate(
        1, src, [dest_order(order_id=2, is_buy_order=False, price=400.0)],
        haul=FREE_HAUL, sales_tax=0.0, broker_fee=0.0, source_station_id=JITA,
        dest_system_id=AHBAZON_SYSTEM, unit_volume_m3=100.0, dest_daily_volume=100,
    )
    compact = sourcing.evaluate(
        2, src, [dest_order(order_id=3, is_buy_order=False, price=250.0)],
        haul=FREE_HAUL, sales_tax=0.0, broker_fee=0.0, source_station_id=JITA,
        dest_system_id=AHBAZON_SYSTEM, unit_volume_m3=0.1, dest_daily_volume=100,
    )
    assert bulky is not None and compact is not None
    assert bulky.profit_per_unit > compact.profit_per_unit
    ranked = sourcing.rank([bulky, compact])
    assert ranked[0].type_id == 2  # far better ISK per m3


def test_fit_to_hold_trims_quantities_to_capacity():
    src = [make_order(order_id=1, is_buy_order=False, price=100.0)]
    opp = sourcing.evaluate(
        1, src, [dest_order(order_id=2, is_buy_order=False, price=400.0)],
        haul=FREE_HAUL, sales_tax=0.0, broker_fee=0.0, source_station_id=JITA,
        dest_system_id=AHBAZON_SYSTEM, unit_volume_m3=10.0, dest_daily_volume=10_000,
    )
    assert opp is not None
    chosen = sourcing.fit_to_hold([opp], cargo_m3=100.0)
    assert chosen[0].suggested_qty == 10
    assert chosen[0].total_m3 == 100.0
    assert chosen[0].total_profit == opp.profit_per_unit * 10
