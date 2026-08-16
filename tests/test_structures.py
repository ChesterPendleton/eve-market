"""Citadel order books: fetched with character access, shaped like region data."""

import pytest

from eve_market.esi import market
from eve_market.esi.client import build_client

STRUCTURE_ID = 1035466617946


@pytest.mark.asyncio
async def test_structure_book_fills_system_id():
    async with build_client() as esi:
        book = await market.structure_book(esi, STRUCTURE_ID, system_id=30005196)
    assert len(book) == 2
    # Structure orders arrive without a system id; the caller's is stamped on
    # so destination-system filters treat citadel orders like station orders.
    assert all(o.system_id == 30005196 for o in book)
    assert all(o.location_id == STRUCTURE_ID for o in book)


@pytest.mark.asyncio
async def test_structure_book_without_system_id():
    async with build_client() as esi:
        book = await market.structure_book(esi, STRUCTURE_ID)
    assert all(o.system_id is None for o in book)


@pytest.mark.asyncio
async def test_sides_survive_the_mapping():
    async with build_client() as esi:
        book = await market.structure_book(esi, STRUCTURE_ID, system_id=1)
    sides = {o.order_id: o.is_buy_order for o in book}
    assert sides[900001] is False
    assert sides[900002] is True
