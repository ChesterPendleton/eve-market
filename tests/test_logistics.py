from __future__ import annotations

import pytest

from eve_market.analysis.logistics import (
    RISK_LOWSEC_FREIGHTER,
    allocate_by_volume,
    profile_for,
    trips_needed,
    warn_for,
)


def test_freighter_through_lowsec_carries_the_heaviest_risk():
    profile = profile_for("freighter", route_is_lowsec=True)
    assert profile.risk_pct == RISK_LOWSEC_FREIGHTER
    assert warn_for(profile, route_is_lowsec=True) is not None


def test_cloaky_hauler_through_lowsec_is_far_cheaper_in_risk():
    freighter = profile_for("freighter", route_is_lowsec=True)
    runner = profile_for("blockade_runner", route_is_lowsec=True)
    assert runner.risk_pct < freighter.risk_pct
    assert warn_for(runner, route_is_lowsec=True) is None


def test_highsec_route_is_near_riskless_for_any_ship():
    profile = profile_for("freighter", route_is_lowsec=False)
    assert profile.risk_pct < 0.01
    assert warn_for(profile, route_is_lowsec=False) is None


def test_self_hauling_has_no_freight_rate():
    assert profile_for("dst", route_is_lowsec=True, self_hauling=True).cost_per_m3 == 0.0
    assert profile_for("dst", route_is_lowsec=True, self_hauling=False).cost_per_m3 > 0.0


def test_explicit_rates_override_the_derived_ones():
    profile = profile_for(
        "freighter", route_is_lowsec=True, cost_per_m3=42.0, risk_pct=0.0
    )
    assert profile.cost_per_m3 == 42.0
    assert profile.risk_pct == 0.0


def test_unknown_ship_is_rejected():
    with pytest.raises(ValueError, match="unknown ship"):
        profile_for("titan", route_is_lowsec=False)


def test_unit_cost_combines_freight_and_risk():
    profile = profile_for("dst", route_is_lowsec=True, cost_per_m3=100.0, risk_pct=0.02)
    # 3 m3 * 100 = 300 freight, plus 2% of 10,000 = 200 risk
    assert profile.unit_cost(3.0, 10_000.0) == 500.0


def test_unit_cost_without_known_volume_still_charges_risk():
    profile = profile_for("dst", route_is_lowsec=True, cost_per_m3=100.0, risk_pct=0.02)
    assert profile.unit_cost(None, 10_000.0) == 200.0


def test_allocation_is_proportional_to_volume():
    allocation = allocate_by_volume(1000.0, [(1, 75.0), (2, 25.0)])
    assert allocation == {1: 750.0, 2: 250.0}
    assert sum(allocation.values()) == 1000.0


def test_allocation_falls_back_to_even_split_without_volumes():
    allocation = allocate_by_volume(900.0, [(1, 0.0), (2, 0.0), (3, 0.0)])
    assert allocation == {1: 300.0, 2: 300.0, 3: 300.0}


def test_allocation_of_nothing_is_empty():
    assert allocate_by_volume(500.0, []) == {}


def test_trips_needed_rounds_up():
    profile = profile_for("dst", route_is_lowsec=False)  # 62,000 m3
    assert trips_needed(0.0, profile) == 0
    assert trips_needed(62_000.0, profile) == 1
    assert trips_needed(62_001.0, profile) == 2
