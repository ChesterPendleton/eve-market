"""Commands for stocking a destination market from Jita.

Registered onto the main Typer app by :func:`register`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import clipboard as clip
from .analysis import screen, sourcing
from .analysis.logistics import SHIPS, HaulProfile, profile_for, warn_for
from .config import settings
from .db import Database
from .esi import market
from .esi.client import build_client
from .esi.universe import system_info
from .ledger import Ledger
from .marketlog import load_directory

console = Console()


def _haul_profile(ship: str | None = None, cargo: float | None = None) -> HaulProfile:
    profile = profile_for(
        ship or settings.ship,
        route_is_lowsec=settings.dest_is_lowsec,
        self_hauling=settings.self_hauling,
        cost_per_m3=settings.haul_cost_per_m3,
        risk_pct=settings.haul_risk_pct,
    )
    return profile


def _capacity(profile: HaulProfile, cargo: float | None) -> float:
    if cargo:
        return cargo
    if settings.cargo_m3:
        return settings.cargo_m3
    return profile.capacity_m3


async def _type_id_for(db: Database, item: str) -> int:
    if item.isdigit():
        return int(item)
    pool = db._require_pool()
    row = await pool.fetchrow(
        "SELECT type_id, type_name FROM inv_type WHERE lower(type_name) = lower($1)", item
    )
    if row is None:
        row = await pool.fetchrow(
            "SELECT type_id, type_name FROM inv_type WHERE type_name ILIKE $1 "
            "ORDER BY length(type_name) LIMIT 1",
            f"%{item}%",
        )
    if row is None:
        raise typer.BadParameter(f"no item matching {item!r} — has the SDE been loaded?")
    return row["type_id"]


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def resolve(name: str = typer.Argument(..., help="System name, e.g. Ahbazon")) -> None:
    """Look up a system's real ids and security status, and print .env lines.

    Run this before trusting the destination defaults. Guessing ids from memory
    is how you end up analysing the wrong market without noticing.
    """

    async def run() -> None:
        async with build_client() as esi:
            info = await system_info(esi, name)

        if info is None:
            console.print(f"[red]no system named {name!r}[/]")
            raise typer.Exit(1)

        table = Table("Field", "Value")
        table.add_row("System", f"{info.name} ({info.system_id})")
        table.add_row("Region", f"{info.region_name} ({info.region_id})")
        table.add_row("Security", f"{info.security_status:.2f} — {info.security_band}")
        table.add_row("NPC stations", str(len(info.station_ids)))
        console.print(table)

        if info.is_lowsec:
            console.print(
                Panel(
                    f"{info.name} is [bold]lowsec[/]. A freighter here is uncloakable and "
                    "slow; a DST or blockade runner is the right call. Risk is charged as "
                    "a share of cargo value, so this materially changes every margin.",
                    border_style="yellow",
                )
            )
        if not info.station_ids:
            console.print(
                Panel(
                    "No NPC stations in this system, so the market is likely a player "
                    "citadel. Unauthenticated ESI cannot see citadel markets — use "
                    "`eve-market import-marketlog` with the client's market export "
                    "instead.",
                    border_style="yellow",
                )
            )

        console.print("\n[bold]Add to .env:[/]")
        console.print(f"EVE_DEST_NAME={info.name}")
        console.print(f"EVE_DEST_REGION_ID={info.region_id}")
        console.print(f"EVE_DEST_SYSTEM_ID={info.system_id}")
        console.print(f"EVE_DEST_IS_LOWSEC={str(info.is_lowsec).lower()}")

    asyncio.run(run())


def fetch_history(
    region: str = typer.Argument("the_forge"),
    limit: int = typer.Option(400, help="Cap on how many types to fetch"),
) -> None:
    """Fetch daily history for the types in a region's latest snapshot.

    History is one ESI request per item, so this is the slow part. The cap
    exists to keep a first run from taking an hour.
    """
    from .config import REGIONS

    region_id = REGIONS.get(region, None) or (int(region) if region.isdigit() else None)
    if region_id is None:
        raise typer.BadParameter(f"unknown region {region!r}")

    async def run() -> None:
        async with Database(settings.database_url) as db, build_client() as esi:
            snapshot_id = await db.latest_snapshot(region_id)
            if snapshot_id is None:
                console.print("[red]no snapshot for that region[/] — run `snapshot` first")
                raise typer.Exit(1)
            pool = db._require_pool()
            rows = await pool.fetch(
                "SELECT type_id, SUM(volume_remain) AS depth FROM market_order "
                "WHERE snapshot_id = $1 GROUP BY type_id ORDER BY depth DESC LIMIT $2",
                snapshot_id,
                limit,
            )
            type_ids = [r["type_id"] for r in rows]
            with console.status(f"fetching history for {len(type_ids)} types..."):
                results = await market.histories(esi, region_id, type_ids)
            written = 0
            for type_id, days in results.items():
                written += await db.save_history(region_id, type_id, days)
        console.print(f"[green]stored[/] {written:,} history rows for {len(type_ids)} types")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# The main screen
# ---------------------------------------------------------------------------


def stock(
    ship: str = typer.Option(None, help=f"One of: {', '.join(SHIPS)}"),
    cargo: float = typer.Option(None, help="Override cargo capacity in m3"),
    buy_orders: bool = typer.Option(
        False, "--buy-orders", help="Acquire in Jita via buy orders instead of instantly"
    ),
    min_margin: float = typer.Option(0.10, help="Minimum net margin after all costs"),
    min_volume: float = typer.Option(20.0, help="Minimum Jita units/day"),
    min_price: float = typer.Option(1000.0, help="Ignore items cheaper than this"),
    max_price: float = typer.Option(None, help="Ignore items dearer than this"),
    undersupplied: bool = typer.Option(
        False, "--undersupplied", help="Only items where demand outweighs supply"
    ),
    fit: bool = typer.Option(True, help="Trim the list to what fits in one trip"),
    limit: int = typer.Option(25),
) -> None:
    """Find what to buy in Jita and stock at the destination market.

    Ranks by profit per m3, because cargo space — not ISK — is what limits a
    run. Items nobody is selling at the destination are flagged separately:
    those let you set the price rather than undercut one.
    """
    profile = _haul_profile(ship)
    capacity = _capacity(profile, cargo)

    async def run() -> None:
        filters = screen.StockFilters(
            buy_orders=buy_orders,
            min_margin=min_margin,
            min_volume=min_volume,
            min_price=min_price,
            max_price=max_price,
            undersupplied=undersupplied,
            fit=fit,
            limit=limit,
        )
        async with Database(settings.database_url) as db:
            result = await screen.stock_screen(db, profile, capacity, filters)
        if result.error:
            console.print(f"[red]{result.error}[/]")
            if result.hint:
                console.print(f"[dim]{result.hint}[/]")
            raise typer.Exit(1)
        best = result.opportunities
        names = result.names

        warning = warn_for(profile, settings.dest_is_lowsec)
        if warning:
            console.print(Panel(warning, border_style="yellow"))

        header = (
            f"{profile.ship.name} · {capacity:,.0f} m3 · "
            f"freight {profile.cost_per_m3:,.0f} ISK/m3 · risk {profile.risk_pct:.1%} · "
            f"acquire {'via buy orders' if buy_orders else 'instantly'}"
        )
        console.print(f"[dim]{header}[/]")

        table = Table(
            "Item", "Jita", "Landed", "List at", "Profit/u", "Margin",
            "ISK/m3", "Qty", "Total", "Demand",
        )
        for o in best:
            demand = "[green]no sellers[/]" if o.no_competition else (
                f"{o.dest_buy.order_count} bids / {o.dest_sell.order_count} asks"
            )
            table.add_row(
                names.get(o.type_id, str(o.type_id)),
                f"{o.acquire_price:,.2f}",
                f"{o.landed_cost:,.2f}",
                f"{o.list_price:,.2f}",
                f"{o.profit_per_unit:,.2f}",
                f"{o.margin:.1%}",
                f"{o.profit_per_m3:,.0f}" if o.profit_per_m3 else "n/a",
                f"{o.suggested_qty:,}",
                f"{o.total_profit:,.0f}",
                demand,
            )
        console.print(table)

        if best:
            total = sum(o.total_profit for o in best)
            used = sum(o.total_m3 or 0 for o in best)
            spend = sum(o.acquire_price * o.suggested_qty for o in best)
            console.print(
                f"\n[bold]{len(best)} items[/] · {used:,.0f}/{capacity:,.0f} m3 · "
                f"spend {spend:,.0f} ISK · est. profit [green]{total:,.0f} ISK[/]"
            )
            console.print("[dim]Next: `eve-market buy-list` to paste into Multibuy.[/]")
        else:
            console.print(
                "[yellow]nothing cleared the filters.[/] Try --min-margin 0.05, or "
                "--buy-orders to acquire more cheaply in Jita."
            )

    asyncio.run(run())


def buy_list(
    ship: str = typer.Option(None),
    cargo: float = typer.Option(None),
    buy_orders: bool = typer.Option(False, "--buy-orders"),
    min_margin: float = typer.Option(0.10),
    min_volume: float = typer.Option(20.0),
    limit: int = typer.Option(25),
    copy: bool = typer.Option(True, help="Copy the list to the clipboard"),
) -> None:
    """Build a Multibuy shopping list for the current best opportunities.

    Paste the output into EVE's Multibuy window in Jita and it builds the whole
    basket at once. You place the orders; nothing is automated.
    """
    profile = _haul_profile(ship)
    capacity = _capacity(profile, cargo)

    async def run() -> None:
        filters = screen.StockFilters(
            buy_orders=buy_orders, min_margin=min_margin, min_volume=min_volume, limit=limit
        )
        async with Database(settings.database_url) as db:
            result = await screen.stock_screen(db, profile, capacity, filters)
        if result.error:
            console.print(f"[red]{result.error}[/]")
            raise typer.Exit(1)
        best = result.opportunities
        names = result.names

        items = [(names.get(o.type_id, str(o.type_id)), o.suggested_qty) for o in best]
        text = clip.multibuy_list(items)
        if not text:
            console.print("[yellow]nothing to buy under the current filters[/]")
            return

        console.print(Panel(text, title="Paste into EVE Multibuy", border_style="green"))
        if copy and clip.try_copy(text):
            console.print("[green]copied to clipboard[/]")
        elif copy:
            console.print("[dim]clipboard unavailable — copy the text above manually[/]")

        if buy_orders:
            console.print(
                "\n[dim]Multibuy fills from sell orders. To place buy orders instead, "
                "use `eve-market price <item>` per item for the bid to enter.[/]"
            )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Pricing helper
# ---------------------------------------------------------------------------


def price(
    item: str = typer.Argument(..., help="Item name or type id"),
    side: str = typer.Option("sell", help="'sell' to list at the destination, 'buy' for Jita"),
    copy: bool = typer.Option(True, help="Copy the recommended price to the clipboard"),
) -> None:
    """Work out the price to enter, and put it on your clipboard.

    For ``--side sell`` this is your destination list price, floored at the
    higher of break-even and replacement cost. For ``--side buy`` it's the bid
    to enter in Jita to take the top of the buy book.

    You paste it into the client yourself — nothing here touches EVE.
    """

    async def run() -> None:
        async with Database(settings.database_url) as db:
            type_id = await _type_id_for(db, item)
            names = await db.type_names([type_id])
            name = names.get(type_id, str(type_id))

            src_snap = await db.latest_snapshot(settings.source_region_id)
            dst_snap = await db.latest_snapshot(settings.dest_region_id)
            src_orders = await db.orders_for_type(src_snap, type_id) if src_snap else []
            dst_orders = await db.orders_for_type(dst_snap, type_id) if dst_snap else []

            jita = [o for o in src_orders if o.location_id == settings.source_station_id]
            jita_sell = sourcing.summarize_side(jita, buy=False)
            jita_buy = sourcing.summarize_side(jita, buy=True)

            dest = sourcing.in_system(dst_orders, settings.dest_system_id or None)
            dest_sell = sourcing.summarize_side(dest, buy=False)
            dest_buy = sourcing.summarize_side(dest, buy=True)

            volumes = await db.type_volumes([type_id])
            profile = _haul_profile()

            ledger = Ledger(db)
            guide = await ledger.price_guide(
                type_id,
                sales_tax=settings.sales_tax,
                broker_fee=settings.broker_fee,
                replacement_unit_cost=jita_sell.best_price,
                haul=profile,
                unit_volume_m3=volumes.get(type_id),
                market_price=dest_sell.best_price,
                undercut_isk=settings.undercut_isk,
            )

        table = Table("", "Price", "Depth")
        table.add_row("Jita lowest sell", _fmt(jita_sell.best_price), f"{jita_sell.volume:,}")
        table.add_row("Jita highest buy", _fmt(jita_buy.best_price), f"{jita_buy.volume:,}")
        table.add_row(
            f"{settings.dest_name} lowest sell",
            _fmt(dest_sell.best_price),
            f"{dest_sell.volume:,} ({dest_sell.order_count} asks)",
        )
        table.add_row(
            f"{settings.dest_name} highest buy",
            _fmt(dest_buy.best_price),
            f"{dest_buy.volume:,} ({dest_buy.order_count} bids)",
        )
        console.print(Panel(table, title=name))

        if side == "buy":
            if jita_buy.best_price is None:
                console.print("[yellow]no buy orders in Jita to outbid[/]")
                return
            target = jita_buy.best_price + settings.undercut_isk
            console.print(f"Bid to top the Jita buy book: [bold]{target:,.2f}[/]")
            _emit(target, copy)
            return

        cost = Table("", "ISK")
        cost.add_row("Units on hand", f"{guide.qty_on_hand:,}")
        cost.add_row("Avg landed cost", _fmt(guide.avg_landed_cost))
        cost.add_row("Break-even list price", _fmt(guide.break_even_price))
        cost.add_row("Replacement list price", _fmt(guide.replacement_price))
        cost.add_row("[bold]Floor (do not go below)", f"[bold]{_fmt(guide.floor_price)}")
        console.print(Panel(cost, title="Your costs"))

        if guide.suggested_price is None:
            console.print(
                Panel(
                    f"The market is at {_fmt(dest_sell.best_price)} but your floor is "
                    f"{_fmt(guide.floor_price)}. Undercutting here loses money.\n"
                    "Hold, sell into the buy orders, or take it elsewhere.",
                    border_style="red",
                    title="Do not undercut",
                )
            )
            if dest_buy.best_price is not None:
                instant = dest_buy.best_price * (1 - settings.sales_tax)
                verdict = "above" if instant > guide.avg_landed_cost else "below"
                console.print(
                    f"Selling into the top buy order nets {instant:,.2f}/unit — "
                    f"{verdict} your landed cost of {guide.avg_landed_cost:,.2f}."
                )
            return

        console.print(f"List at: [bold green]{guide.suggested_price:,.2f}[/]")
        margin = guide.suggested_price * (
            1 - settings.sales_tax - settings.broker_fee
        ) - guide.avg_landed_cost
        console.print(f"Nets [bold]{margin:,.2f}[/] per unit after fees.")
        _emit(guide.suggested_price, copy)

    asyncio.run(run())


def _fmt(value: float | None) -> str:
    return f"{value:,.2f}" if value is not None else "—"


def _emit(value: float, copy: bool) -> None:
    text = clip.format_price(value)
    if copy and clip.try_copy(text):
        console.print(f"[green]copied[/] [bold]{text}[/] — alt-tab and Ctrl+V")
    else:
        console.print(f"Paste this: [bold]{text}[/]")


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def buy(
    item: str = typer.Argument(...),
    qty: int = typer.Argument(...),
    unit_price: float = typer.Argument(...),
    via_order: bool = typer.Option(
        False, "--via-order", help="Bought by placing a buy order (adds the broker fee)"
    ),
    note: str = typer.Option(None),
) -> None:
    """Record a purchase so its cost can be tracked through to the sale."""

    async def run() -> None:
        async with Database(settings.database_url) as db:
            type_id = await _type_id_for(db, item)
            lot_id = await Ledger(db).record_purchase(
                type_id,
                qty,
                unit_price,
                broker_fee=settings.broker_fee if via_order else 0.0,
                station_id=settings.source_station_id,
                note=note,
            )
            names = await db.type_names([type_id])
        fee_note = " (incl. broker fee)" if via_order else ""
        console.print(
            f"[green]lot {lot_id}[/]: {qty:,} × {names.get(type_id, type_id)} "
            f"@ {unit_price:,.2f}{fee_note}"
        )
        console.print("[dim]After hauling, run `eve-market haul-cost` to load freight in.[/]")

    asyncio.run(run())


def sell(
    item: str = typer.Argument(...),
    qty: int = typer.Argument(...),
    unit_price: float = typer.Argument(...),
    to_buy_order: bool = typer.Option(
        False, "--to-buy-order", help="Filled someone's buy order (no broker fee)"
    ),
) -> None:
    """Record a sale, consuming stock FIFO and computing realised profit."""

    async def run() -> None:
        async with Database(settings.database_url) as db:
            type_id = await _type_id_for(db, item)
            try:
                result = await Ledger(db).record_sale(
                    type_id,
                    qty,
                    unit_price,
                    sales_tax=settings.sales_tax,
                    broker_fee=0.0 if to_buy_order else settings.broker_fee,
                    station_id=settings.dest_station_id or None,
                )
            except ValueError as exc:
                console.print(f"[red]{exc}[/]")
                raise typer.Exit(1) from exc

        colour = "green" if result["profit"] > 0 else "red"
        console.print(
            f"gross {result['gross']:,.2f} − fees {result['fees']:,.2f} − "
            f"cost {result['cogs']:,.2f} = [{colour}]{result['profit']:,.2f}[/] "
            f"({result['margin']:.1%})"
        )

    asyncio.run(run())


def haul_cost(
    total_cost: float = typer.Argument(..., help="Total ISK the trip cost"),
    lots: str = typer.Option(None, help="Comma-separated lot ids; default is all open lots"),
) -> None:
    """Spread a trip's freight and risk cost across the lots it carried.

    Allocated by m3, because volume is what filled the hold. Allocating by
    value would make cheap bulky goods look like they shipped for free.
    """

    async def run() -> None:
        async with Database(settings.database_url) as db:
            ledger = Ledger(db)
            if lots:
                lot_ids = [int(x) for x in lots.split(",") if x.strip()]
            else:
                lot_ids = [lot.id for lot in await ledger.open_lots()]
            if not lot_ids:
                console.print("[yellow]no open lots to charge[/]")
                return
            open_lots = {lot.id: lot for lot in await ledger.open_lots()}
            volumes = await db.type_volumes(
                [lot.type_id for lot in open_lots.values() if lot.id in lot_ids]
            )
            allocation = await ledger.assign_haul_cost(lot_ids, total_cost, volumes)

        table = Table("Lot", "Allocated ISK")
        for lot_id, cost in sorted(allocation.items()):
            table.add_row(str(lot_id), f"{cost:,.2f}")
        console.print(table)

    asyncio.run(run())


def position(item: str = typer.Argument(None, help="Optional item filter")) -> None:
    """Show stock on hand and the capital tied up in it."""

    async def run() -> None:
        async with Database(settings.database_url) as db:
            ledger = Ledger(db)
            if item:
                type_id = await _type_id_for(db, item)
                rows = [await ledger.position(type_id)]
            else:
                rows = await ledger.positions()
            names = await db.type_names([p.type_id for p in rows])

        table = Table("Item", "On hand", "Avg landed cost", "Capital")
        for p in rows:
            if p.qty_on_hand == 0:
                continue
            table.add_row(
                names.get(p.type_id, str(p.type_id)),
                f"{p.qty_on_hand:,}",
                f"{p.avg_landed_cost:,.2f}",
                f"{p.capital_tied_up:,.0f}",
            )
        console.print(table)
        total = sum(p.capital_tied_up for p in rows)
        console.print(f"[bold]Capital deployed: {total:,.0f} ISK[/]")

    asyncio.run(run())


def pnl(item: str = typer.Argument(None)) -> None:
    """Realised profit across recorded sales."""

    async def run() -> None:
        async with Database(settings.database_url) as db:
            type_id = await _type_id_for(db, item) if item else None
            totals = await Ledger(db).realized_pnl(type_id)

        table = Table("", "ISK")
        table.add_row("Sales recorded", f"{totals['sales']:,.0f}")
        table.add_row("Gross revenue", f"{totals['gross']:,.2f}")
        table.add_row("Fees and tax", f"{totals['fees']:,.2f}")
        table.add_row("Cost of goods", f"{totals['cogs']:,.2f}")
        colour = "green" if totals["profit"] > 0 else "red"
        table.add_row("[bold]Realised profit", f"[{colour}]{totals['profit']:,.2f}[/]")
        console.print(table)

    asyncio.run(run())


def import_marketlog(
    directory: Path = typer.Argument(..., help="EVE Marketlogs folder"),
    region: int = typer.Option(None, help="Region id to file these orders under"),
) -> None:
    """Import market data exported by the EVE client.

    Needed when the destination market is a player citadel, which
    unauthenticated ESI cannot see. In the client: open the market, click
    export, then point this at Documents/EVE/logs/Marketlogs.
    """

    async def run() -> None:
        orders = load_directory(directory)
        if not orders:
            console.print(f"[yellow]no market logs found in {directory}[/]")
            raise typer.Exit(1)
        region_id = region or settings.dest_region_id
        async with Database(settings.database_url) as db:
            snapshot_id = await db.save_snapshot(region_id, orders)
        console.print(
            f"[green]imported[/] {len(orders):,} orders as snapshot {snapshot_id} "
            f"for region {region_id}"
        )

    asyncio.run(run())


def register(app: typer.Typer) -> None:
    """Attach these commands to the main CLI app."""
    app.command()(resolve)
    app.command("fetch-history")(fetch_history)
    app.command()(stock)
    app.command("buy-list")(buy_list)
    app.command()(price)
    app.command()(buy)
    app.command()(sell)
    app.command("haul-cost")(haul_cost)
    app.command()(position)
    app.command()(pnl)
    app.command("import-marketlog")(import_marketlog)
