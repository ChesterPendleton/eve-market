"""Command line interface.

Run ``eve-market doctor`` first on a new machine — it checks every external
dependency and tells you which ones aren't ready yet.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .analysis import hauling as haul_mod
from .analysis import margins as margin_mod
from .config import REGIONS, STATIONS, settings
from .db import Database
from .esi import market
from .esi.client import build_client

app = typer.Typer(help="EVE Online market and trading analysis.", no_args_is_help=True)
console = Console()


def _region_id(name_or_id: str) -> int:
    if name_or_id.isdigit():
        return int(name_or_id)
    key = name_or_id.lower().replace(" ", "_").replace("-", "_")
    if key not in REGIONS:
        raise typer.BadParameter(f"unknown region {name_or_id!r}; known: {', '.join(REGIONS)}")
    return REGIONS[key]


def _station_id(name_or_id: str) -> int:
    if name_or_id.isdigit():
        return int(name_or_id)
    key = name_or_id.lower().replace(" ", "_").replace("-", "_")
    if key not in STATIONS:
        raise typer.BadParameter(f"unknown station {name_or_id!r}; known: {', '.join(STATIONS)}")
    return STATIONS[key]


@app.command()
def doctor() -> None:
    """Check ESI, Redis and Postgres connectivity, and report what's missing."""

    async def run() -> None:
        table = Table("Check", "Status", "Detail")
        ok = True

        # --- ESI --------------------------------------------------------
        try:
            async with build_client() as esi:
                page = await esi.get("/v1/status/")
                mode = "live" if settings.esi_live else "fixtures"
                detail = f"{mode}; players={page.data.get('players', 'n/a')}"
                table.add_row("ESI", "[green]ok[/]", detail)
        except Exception as exc:  # noqa: BLE001
            ok = False
            table.add_row("ESI", "[red]fail[/]", str(exc)[:80])

        # --- Redis ------------------------------------------------------
        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(settings.redis_url)
            await client.ping()
            await client.aclose()
            table.add_row("Redis", "[green]ok[/]", settings.redis_url)
        except Exception as exc:  # noqa: BLE001
            table.add_row("Redis", "[yellow]degraded[/]", f"in-memory fallback ({exc})"[:80])

        # --- Postgres ---------------------------------------------------
        try:
            async with Database(settings.database_url) as db:
                pool = db.pool
                assert pool is not None
                version = await pool.fetchval("SELECT version()")
                table.add_row("Postgres", "[green]ok[/]", str(version).split(",")[0][:60])
        except Exception as exc:  # noqa: BLE001
            ok = False
            table.add_row("Postgres", "[red]fail[/]", str(exc)[:80])

        # --- Config -----------------------------------------------------
        if settings.contact_email:
            table.add_row("User-Agent", "[green]ok[/]", settings.user_agent)
        else:
            ok = False
            table.add_row(
                "User-Agent", "[red]fail[/]", "set EVE_CONTACT_EMAIL — CCP requires it"
            )

        console.print(table)
        if not ok:
            console.print("\n[yellow]Some checks failed. See README 'Setup on your PC'.[/]")
            raise typer.Exit(1)

    asyncio.run(run())


@app.command()
def migrate() -> None:
    """Create the database schema."""

    async def run() -> None:
        async with Database(settings.database_url) as db:
            await db.migrate()
        console.print("[green]schema applied[/]")

    asyncio.run(run())


@app.command()
def snapshot(region: str = typer.Argument("the_forge")) -> None:
    """Pull a region's entire order book and store it."""
    region_id = _region_id(region)

    async def run() -> None:
        async with build_client() as esi, Database(settings.database_url) as db:
            with console.status(f"fetching order book for {region}..."):
                orders = await market.region_orders(esi, region_id)
            snapshot_id = await db.save_snapshot(region_id, orders)
        console.print(f"[green]saved[/] {len(orders):,} orders as snapshot {snapshot_id}")

    asyncio.run(run())


@app.command()
def spreads(
    region: str = typer.Argument("the_forge"),
    station: str = typer.Option("jita_4_4", help="Station to trade at"),
    min_margin: float = typer.Option(0.05, help="Minimum net margin, e.g. 0.05 = 5%"),
    min_volume: float = typer.Option(10.0, help="Minimum average units/day"),
    limit: int = typer.Option(20),
) -> None:
    """Rank station-trading spreads by expected daily profit."""
    region_id = _region_id(region)
    station_id = _station_id(station)

    async def run() -> None:
        async with Database(settings.database_url) as db:
            snapshot_id = await db.latest_snapshot(region_id)
            if snapshot_id is None:
                console.print("[red]no snapshot yet[/] — run `eve-market snapshot` first")
                raise typer.Exit(1)

            pool = db.pool
            assert pool is not None
            # Only types actually quoted on both sides at this station can spread.
            type_ids = [
                r["type_id"]
                for r in await pool.fetch(
                    "SELECT type_id FROM market_order "
                    "WHERE snapshot_id = $1 AND location_id = $2 "
                    "GROUP BY type_id "
                    "HAVING bool_or(is_buy_order) AND bool_or(NOT is_buy_order)",
                    snapshot_id,
                    station_id,
                )
            ]

            results = []
            for type_id in type_ids:
                orders = await db.orders_for_type(snapshot_id, type_id)
                history = await db.history_for_type(region_id, type_id)
                spread = margin_mod.compute_spread(type_id, station_id, orders, history)
                if spread:
                    results.append(spread)

            best = margin_mod.screen(
                results, min_margin=min_margin, min_daily_volume=min_volume
            )[:limit]
            names = await db.type_names([s.type_id for s in best])

        table = Table("Item", "Buy", "Sell", "Margin", "Vol/day", "Est. ISK/day")
        for s in best:
            table.add_row(
                names.get(s.type_id, str(s.type_id)),
                f"{s.buy_price:,.2f}",
                f"{s.sell_price:,.2f}",
                f"{s.margin_pct:.1f}%",
                f"{s.daily_volume:,.0f}",
                f"{s.daily_profit:,.0f}",
            )
        console.print(table)
        if not best:
            console.print("[yellow]nothing cleared the filters[/] — try lowering --min-margin")

    asyncio.run(run())


@app.command()
def haul(
    source: str = typer.Argument("the_forge"),
    dest: str = typer.Argument("domain"),
    cargo: float = typer.Option(60000.0, help="Cargo capacity in m3"),
    limit: int = typer.Option(20),
) -> None:
    """Find profitable cross-region hauls, ranked by ISK per m3."""
    source_id, dest_id = _region_id(source), _region_id(dest)

    async def run() -> None:
        async with Database(settings.database_url) as db:
            src_snap = await db.latest_snapshot(source_id)
            dst_snap = await db.latest_snapshot(dest_id)
            if src_snap is None or dst_snap is None:
                console.print("[red]need a snapshot of both regions[/]")
                raise typer.Exit(1)

            pool = db.pool
            assert pool is not None
            shared = [
                r["type_id"]
                for r in await pool.fetch(
                    "SELECT DISTINCT a.type_id FROM market_order a "
                    "JOIN market_order b ON a.type_id = b.type_id "
                    "WHERE a.snapshot_id = $1 AND NOT a.is_buy_order "
                    "AND b.snapshot_id = $2 AND b.is_buy_order",
                    src_snap,
                    dst_snap,
                )
            ]
            volumes = await db.type_volumes(shared)

            routes = []
            for type_id in shared:
                route = haul_mod.find_route(
                    type_id,
                    await db.orders_for_type(src_snap, type_id),
                    await db.orders_for_type(dst_snap, type_id),
                    source_id,
                    dest_id,
                    unit_volume_m3=volumes.get(type_id),
                    cargo_m3=cargo,
                )
                if route:
                    routes.append(route)

            best = haul_mod.rank(routes)[:limit]
            names = await db.type_names([r.type_id for r in best])

        table = Table("Item", "Buy", "Sell", "Units", "Profit", "ISK/m3")
        for r in best:
            table.add_row(
                names.get(r.type_id, str(r.type_id)),
                f"{r.buy_price:,.2f}",
                f"{r.sell_price:,.2f}",
                f"{r.units_available:,}",
                f"{r.total_profit:,.0f}",
                f"{r.isk_per_m3:,.0f}" if r.isk_per_m3 else "n/a",
            )
        console.print(table)

    asyncio.run(run())


@app.command("load-sde")
def load_sde(path: Path = typer.Argument(..., help="JSON array of type rows")) -> None:
    """Load the SDE slice used for item names and cargo volumes.

    Expects a JSON array of objects with at least ``type_id`` and ``type_name``;
    ``setup.sh`` can build this for you from the official SDE download.
    """

    async def run() -> None:
        rows = json.loads(path.read_text())
        async with Database(settings.database_url) as db:
            written = await db.upsert_types(rows)
        console.print(f"[green]loaded[/] {written:,} types")

    asyncio.run(run())


# Trading and authenticated commands live in their own modules to keep this
# one readable.
from .cli_auth import register as _register_auth
from .cli_stock import register as _register_stock

_register_stock(app)
_register_auth(app)


if __name__ == "__main__":
    app()
