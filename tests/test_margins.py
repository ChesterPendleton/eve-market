from __future__ import annotations

from eve_market.analysis.margins import average_daily_volume, compute_spread, screen

from .conftest import make_history, make_order

STATION = 60003760


def test_spread_applies_broker_fee_both_sides_and_sales_tax():
    orders = [
        make_order(order_id=1, is_buy_order=True, price=100.0),
        make_order(order_id=2, is_buy_order=False, price=150.0),
    ]
    spread = compute_spread(
        34, STATION, orders, make_history(), broker_fee=0.02, sales_tax=0.05
    )
    assert spread is not None
    # cost = 100 * 1.02 = 102; revenue = 150 * (1 - 0.02 - 0.05) = 139.5
    assert spread.profit_per_unit == 37.5
    assert round(spread.margin, 6) == round(37.5 / 102.0, 6)


def test_spread_uses_best_prices_not_first_seen():
    orders = [
        make_order(order_id=1, is_buy_order=True, price=90.0),
        make_order(order_id=2, is_buy_order=True, price=110.0),  # best buy
        make_order(order_id=3, is_buy_order=False, price=200.0),
        make_order(order_id=4, is_buy_order=False, price=160.0),  # best sell
    ]
    spread = compute_spread(34, STATION, orders, broker_fee=0.0, sales_tax=0.0)
    assert spread is not None
    assert spread.buy_price == 110.0
    assert spread.sell_price == 160.0


def test_orders_at_other_stations_are_ignored():
    orders = [
        make_order(order_id=1, is_buy_order=True, price=100.0),
        make_order(order_id=2, is_buy_order=False, price=150.0, location_id=60008494),
    ]
    # Only a buy side exists at our station, so there's no spread to trade.
    assert compute_spread(34, STATION, orders) is None


def test_locked_book_returns_none():
    orders = [
        make_order(order_id=1, is_buy_order=True, price=150.0),
        make_order(order_id=2, is_buy_order=False, price=100.0),
    ]
    assert compute_spread(34, STATION, orders) is None


def test_depth_is_summed_per_side():
    orders = [
        make_order(order_id=1, is_buy_order=True, price=100.0, volume_remain=5),
        make_order(order_id=2, is_buy_order=True, price=99.0, volume_remain=7),
        make_order(order_id=3, is_buy_order=False, price=150.0, volume_remain=11),
    ]
    spread = compute_spread(34, STATION, orders)
    assert spread is not None
    assert spread.buy_depth == 12
    assert spread.sell_depth == 11


def test_average_daily_volume_uses_recent_window_only():
    history = make_history(days=60, volume=100)
    history[-1].volume = 1000  # a spike inside the 30-day window
    assert average_daily_volume(history, window=30) == (29 * 100 + 1000) / 30
    assert average_daily_volume([]) == 0.0


def test_screen_filters_low_volume_even_at_high_margin():
    thin = compute_spread(
        34, STATION, [
            make_order(order_id=1, is_buy_order=True, price=100.0),
            make_order(order_id=2, is_buy_order=False, price=400.0),
        ],
        make_history(volume=1),
    )
    assert thin is not None and thin.margin > 0.5
    assert screen([thin], min_daily_volume=10.0) == []


def test_screen_ranks_by_daily_profit():
    high_margin_thin = compute_spread(
        1, STATION,
        [make_order(order_id=1, is_buy_order=True, price=100.0),
         make_order(order_id=2, is_buy_order=False, price=200.0)],
        make_history(volume=20),
    )
    low_margin_thick = compute_spread(
        2, STATION,
        [make_order(order_id=3, is_buy_order=True, price=100.0),
         make_order(order_id=4, is_buy_order=False, price=115.0)],
        make_history(volume=5000),
    )
    ranked = screen(
        [high_margin_thin, low_margin_thick], min_margin=0.01, min_daily_volume=1
    )
    assert [s.type_id for s in ranked] == [2, 1]
