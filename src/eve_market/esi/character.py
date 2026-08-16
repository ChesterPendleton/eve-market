"""Authenticated ESI: your orders, transactions, assets, and the client UI.

The UI endpoints are the sanctioned way for a third-party tool to interact with
a running EVE client. They open a window; they do not click anything. Placing
and modifying orders remains manual, because ESI exposes no endpoint to do it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel

from .client import EsiClient


class CharacterOrder(BaseModel):
    """One of your live market orders.

    ESI omits ``is_buy_order`` entirely on sell orders rather than sending
    false, so it defaults here.
    """

    order_id: int
    type_id: int
    region_id: int
    location_id: int
    is_buy_order: bool = False
    price: float
    volume_total: int
    volume_remain: int
    min_volume: int = 1
    duration: int
    issued: datetime
    range: str = "station"
    escrow: float | None = None


class WalletTransaction(BaseModel):
    transaction_id: int
    type_id: int
    location_id: int
    unit_price: float
    quantity: int
    is_buy: bool
    date: datetime
    client_id: int | None = None
    journal_ref_id: int | None = None


class AssetItem(BaseModel):
    item_id: int
    type_id: int
    quantity: int
    location_id: int
    location_flag: str
    location_type: str
    is_singleton: bool = False


@dataclass(slots=True)
class Character:
    character_id: int
    name: str


async def my_orders(esi: EsiClient, character_id: int) -> list[CharacterOrder]:
    """Your currently open market orders."""
    page = await esi.get(f"/v2/characters/{character_id}/orders/")
    rows = page.data if isinstance(page.data, list) else []
    return [CharacterOrder.model_validate(r) for r in rows]


async def my_transactions(
    esi: EsiClient, character_id: int
) -> list[WalletTransaction]:
    """Recent wallet transactions.

    ESI returns roughly the last 30 days or 2,500 entries, whichever is
    smaller, so a long gap between syncs loses history permanently.
    """
    page = await esi.get(f"/v1/characters/{character_id}/wallet/transactions/")
    rows = page.data if isinstance(page.data, list) else []
    return [WalletTransaction.model_validate(r) for r in rows]


async def my_assets(esi: EsiClient, character_id: int) -> list[AssetItem]:
    """Everything you own, across every station and container."""
    rows = await esi.get_all_pages(f"/v5/characters/{character_id}/assets/")
    return [AssetItem.model_validate(r) for r in rows]


async def structure_orders(esi: EsiClient, structure_id: int) -> list[dict]:
    """Order book of a player structure you have docking access to.

    This is the authenticated answer to citadel markets, which are invisible
    to unauthenticated ESI.
    """
    return await esi.get_all_pages(f"/v1/markets/structures/{structure_id}/")


async def structure_info(esi: EsiClient, structure_id: int) -> dict:
    """Name and solar system of a structure you have docking access to."""
    page = await esi.get(f"/v2/universe/structures/{structure_id}/")
    return page.data


async def search_structures(
    esi: EsiClient, character_id: int, name: str
) -> list[int]:
    """Structure ids matching a name, seen through this character's access."""
    page = await esi.get(
        f"/v3/characters/{character_id}/search/",
        params={"categories": "structure", "search": name},
    )
    return list(page.data.get("structure", []))


async def open_market_window(esi: EsiClient, type_id: int) -> None:
    """Open an item's market details window in the running client.

    Official CCP endpoint, gated behind the ``esi-ui.open_window.v1`` scope.
    It opens a window and nothing more — you still enter the price yourself.
    """
    await esi.post(f"/v1/ui/openwindow/marketdetails/?type_id={type_id}", None)


async def open_info_window(esi: EsiClient, target_id: int) -> None:
    """Open the info window for a character, corporation or item."""
    await esi.post(f"/v1/ui/openwindow/information/?target_id={target_id}", None)


async def set_waypoint(
    esi: EsiClient,
    destination_id: int,
    *,
    clear_others: bool = True,
    add_to_beginning: bool = False,
) -> None:
    """Set an autopilot waypoint, for plotting the haul route."""
    query = (
        f"destination_id={destination_id}"
        f"&clear_other_waypoints={str(clear_others).lower()}"
        f"&add_to_beginning={str(add_to_beginning).lower()}"
    )
    await esi.post(f"/v2/ui/autopilot/waypoint/?{query}", None)
