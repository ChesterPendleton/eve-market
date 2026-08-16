"""Name and id resolution for regions, systems and stations.

Hardcoding hub ids is fine for Jita and Amarr, whose ids everyone knows. For
anywhere else, guessing an id from memory is how you end up analysing the
wrong market and not noticing. These helpers ask ESI instead.
"""

from __future__ import annotations

from dataclasses import dataclass

from .client import EsiClient


@dataclass(slots=True)
class SystemInfo:
    system_id: int
    name: str
    region_id: int
    region_name: str
    security_status: float
    station_ids: list[int]

    @property
    def is_lowsec(self) -> bool:
        # EVE rounds security for display: 0.45 shows as 0.5 and is highsec.
        # Anything that displays below 0.5 is lowsec; 0.0 and below is null.
        return 0.0 < round(self.security_status, 1) < 0.5

    @property
    def is_nullsec(self) -> bool:
        return round(self.security_status, 1) <= 0.0

    @property
    def security_band(self) -> str:
        if self.is_nullsec:
            return "nullsec"
        return "lowsec" if self.is_lowsec else "highsec"


async def resolve_names(esi: EsiClient, names: list[str]) -> dict:
    """Resolve names to ids via ``/universe/ids/``."""
    result = await esi.post("/v1/universe/ids/", names)
    return result if isinstance(result, dict) else {}


async def system_info(esi: EsiClient, name: str) -> SystemInfo | None:
    """Look up a solar system by name, with its region and security status."""
    resolved = await resolve_names(esi, [name])
    systems = resolved.get("systems") or []
    match = next((s for s in systems if s["name"].lower() == name.lower()), None)
    if match is None:
        return None

    system_id = match["id"]
    detail = await esi.get(f"/v4/universe/systems/{system_id}/")
    if not isinstance(detail.data, dict):
        return None

    constellation = await esi.get(
        f"/v1/universe/constellations/{detail.data['constellation_id']}/"
    )
    region_id = (
        constellation.data["region_id"] if isinstance(constellation.data, dict) else 0
    )
    region = await esi.get(f"/v1/universe/regions/{region_id}/")
    region_name = region.data.get("name", "") if isinstance(region.data, dict) else ""

    return SystemInfo(
        system_id=system_id,
        name=detail.data.get("name", name),
        region_id=region_id,
        region_name=region_name,
        security_status=float(detail.data.get("security_status", 0.0)),
        station_ids=list(detail.data.get("stations") or []),
    )


async def station_names(esi: EsiClient, station_ids: list[int]) -> dict[int, str]:
    """Resolve NPC station ids to names.

    Player structures are not covered: they need authentication and docking
    access, and will simply be absent here.
    """
    out: dict[int, str] = {}
    for station_id in station_ids:
        try:
            page = await esi.get(f"/v2/universe/stations/{station_id}/")
        except Exception:  # noqa: BLE001 - a missing station shouldn't abort
            continue
        if isinstance(page.data, dict):
            out[station_id] = page.data.get("name", str(station_id))
    return out
