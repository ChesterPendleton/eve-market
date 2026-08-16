"""Hauling economics: ship capacity, per-trip cost, and cost allocation.

A haul's cost is not just fuel. The two components that actually matter are:

* **Freight rate** — ISK per m3, whether that's a courier contract or your own
  time valued honestly.
* **Risk** — the expected loss from flying the route. Through highsec this is
  near zero. Through lowsec it very much is not, and pretending otherwise is
  how people discover their margin was negative all along.

Risk is expressed as a percentage of cargo value, so an expensive cargo pays
more for the same trip. That's correct: losing a freighter of PLEX and losing
a freighter of trit are not the same event.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Ship:
    key: str
    name: str
    capacity_m3: float
    lowsec_safe: bool
    note: str


# Capacities are sensible fitted defaults, not hard limits — override with
# --cargo when your actual fit differs.
SHIPS: dict[str, Ship] = {
    "blockade_runner": Ship(
        "blockade_runner",
        "Blockade Runner",
        11_000,
        lowsec_safe=True,
        note="Covert cloak; the safe way through a camped lowsec gate.",
    ),
    "dst": Ship(
        "dst",
        "Deep Space Transport",
        62_000,
        lowsec_safe=True,
        note="Tanky with an MJD escape; good bulk-vs-risk compromise.",
    ),
    "freighter": Ship(
        "freighter",
        "Freighter",
        435_000,
        lowsec_safe=False,
        note="Huge, slow, uncloakable. Through lowsec this is bait.",
    ),
}

# Typical published courier rates, for when you'd rather contract it out.
COURIER_RATE_HIGHSEC = 800.0  # ISK per m3
COURIER_RATE_LOWSEC = 1500.0
COURIER_COLLATERAL_PCT = 0.01

# Rough expected-loss rates. Deliberately pessimistic for lowsec: these are
# meant to stop marginal trades looking viable, not to model reality exactly.
RISK_HIGHSEC = 0.001
RISK_LOWSEC_CLOAKY = 0.01
RISK_LOWSEC_FREIGHTER = 0.15


@dataclass(frozen=True, slots=True)
class HaulProfile:
    """How you're moving goods, and what that costs per unit carried."""

    ship: Ship
    cost_per_m3: float
    risk_pct: float

    @property
    def capacity_m3(self) -> float:
        return self.ship.capacity_m3

    def unit_cost(self, unit_volume_m3: float | None, unit_value: float) -> float:
        """Cost attributable to moving one unit."""
        freight = (unit_volume_m3 or 0.0) * self.cost_per_m3
        return freight + unit_value * self.risk_pct


def profile_for(
    ship_key: str,
    *,
    route_is_lowsec: bool,
    self_hauling: bool = True,
    cost_per_m3: float | None = None,
    risk_pct: float | None = None,
) -> HaulProfile:
    """Build a haul profile, defaulting the rates from route and ship.

    Flying it yourself has no freight rate but the same risk. Contracting it
    out inverts that: you pay a rate and the risk becomes the courier's, minus
    whatever collateral doesn't cover.
    """
    ship = SHIPS.get(ship_key)
    if ship is None:
        raise ValueError(f"unknown ship {ship_key!r}; known: {', '.join(SHIPS)}")

    if cost_per_m3 is None:
        if self_hauling:
            cost_per_m3 = 0.0
        else:
            cost_per_m3 = COURIER_RATE_LOWSEC if route_is_lowsec else COURIER_RATE_HIGHSEC

    if risk_pct is None:
        if not self_hauling:
            risk_pct = COURIER_COLLATERAL_PCT
        elif not route_is_lowsec:
            risk_pct = RISK_HIGHSEC
        elif ship.lowsec_safe:
            risk_pct = RISK_LOWSEC_CLOAKY
        else:
            risk_pct = RISK_LOWSEC_FREIGHTER

    return HaulProfile(ship=ship, cost_per_m3=cost_per_m3, risk_pct=risk_pct)


def warn_for(profile: HaulProfile, route_is_lowsec: bool) -> str | None:
    """A one-line warning when the ship choice doesn't suit the route."""
    if route_is_lowsec and not profile.ship.lowsec_safe:
        return (
            f"{profile.ship.name} through lowsec: {profile.risk_pct:.0%} of cargo value "
            "is being charged as expected loss. Consider a DST or blockade runner."
        )
    return None


def allocate_by_volume(
    total_cost: float, items: list[tuple[int, float]]
) -> dict[int, float]:
    """Split one trip's cost across its cargo, proportional to m3.

    ``items`` is ``[(item_key, total_m3), ...]``. Volume is the right basis
    because volume is what filled the hold. Allocating by ISK value would
    make cheap bulky goods look free, which is exactly backwards.

    Falls back to an even split when no volumes are known.
    """
    if not items:
        return {}
    total_m3 = sum(m3 for _, m3 in items)
    if total_m3 <= 0:
        share = total_cost / len(items)
        return {key: share for key, _ in items}
    return {key: total_cost * (m3 / total_m3) for key, m3 in items}


def trips_needed(total_m3: float, profile: HaulProfile) -> int:
    """How many round trips this cargo takes."""
    if total_m3 <= 0:
        return 0
    import math

    return math.ceil(total_m3 / profile.capacity_m3)
