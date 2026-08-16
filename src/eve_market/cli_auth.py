"""Authenticated commands: login, your orders, the relist worklist, sync.

Registered onto the main Typer app by :func:`register`.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import auth
from . import clipboard as clip
from .analysis import relist as relist_mod
from .analysis.relist import Status
from .config import settings
from .db import Database
from .esi import character
from .esi.client import build_client
from .ledger import Ledger

console = Console()

STATUS_STYLE = {
    Status.BEST: "[green]best[/]",
    Status.UNDERCUT: "[red]undercut[/]",
    Status.OUTBID: "[red]outbid[/]",
    Status.BELOW_FLOOR: "[yellow]below floor[/]",
    Status.ALONE: "[cyan]no rivals[/]",
}


def _require_tokens() -> auth.Tokens:
    tokens = auth.ensure_fresh(settings.client_id)
    if tokens is None:
        console.print("[red]not logged in[/] — run `eve-market login`")
        raise typer.Exit(1)
    return tokens


def login(
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Print the URL instead of opening a browser"
    ),
) -> None:
    """Authorise this tool against your EVE character via SSO.

    Register a native application at developers.eveonline.com first, set
    EVE_CLIENT_ID, and give it a callback URL matching EVE_CALLBACK_URL.
    """
    if not settings.client_id:
        console.print(
            Panel(
                "No EVE_CLIENT_ID set.\n\n"
                "1. Go to https://developers.eveonline.com/applications\n"
                "2. Create an application, connection type "
                "'Authentication & API Access'\n"
                f"3. Set the callback URL to exactly: {settings.callback_url}\n"
                "4. Put the client id in .env as EVE_CLIENT_ID\n\n"
                "No client secret is needed — this uses the PKCE flow.",
                border_style="red",
                title="Setup required",
            )
        )
        raise typer.Exit(1)

    console.print("Opening EVE SSO in your browser...")
    try:
        tokens = auth.login(
            settings.client_id,
            settings.callback_url,
            open_browser=not no_browser,
        )
    except auth.AuthError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc

    path = auth.save(tokens)
    console.print(f"[green]logged in[/] as [bold]{tokens.character_name}[/]")
    console.print(f"[dim]tokens stored at {path} (owner-readable only)[/]")


def whoami() -> None:
    """Show the logged-in character and the scopes granted."""
    tokens = _require_tokens()
    table = Table("Field", "Value")
    table.add_row("Character", f"{tokens.character_name} ({tokens.character_id})")
    table.add_row("Scopes", "\n".join(tokens.scopes) or "none")
    console.print(table)

    missing = auth.missing_scopes(tokens, auth.SCOPES)
    if missing:
        console.print(
            Panel(
                "Missing scopes:\n" + "\n".join(missing) + "\n\nRun `eve-market login` again.",
                border_style="yellow",
            )
        )


def orders() -> None:
    """List your live market orders."""
    tokens = _require_tokens()

    async def run() -> None:
        async with build_client(access_token=tokens.access_token) as esi:
            mine = await character.my_orders(esi, tokens.character_id)
        async with Database(settings.database_url) as db:
            names = await db.type_names([o.type_id for o in mine])

        table = Table("Item", "Side", "Price", "Remaining", "Location")
        for o in sorted(mine, key=lambda o: o.price * o.volume_remain, reverse=True):
            table.add_row(
                names.get(o.type_id, str(o.type_id)),
                "buy" if o.is_buy_order else "sell",
                f"{o.price:,.2f}",
                f"{o.volume_remain:,}/{o.volume_total:,}",
                str(o.location_id),
            )
        console.print(table)
        console.print(f"[bold]{len(mine)} open orders[/]")

    asyncio.run(run())


def relist(
    open_window: bool = typer.Option(
        False, "--open", help="Open each item's market window in the EVE client"
    ),
    only_action: bool = typer.Option(
        True, help="Hide orders that are already winning"
    ),
    limit: int = typer.Option(20),
) -> None:
    """Show which of your orders have been undercut, and the price to relist at.

    Walks the worklist one order at a time: it copies the new price to your
    clipboard and, with --open, opens that item's market window in the client.
    You press Ctrl+V and confirm — ESI has no endpoint to change an order, so
    the last step is always yours.
    """
    tokens = _require_tokens()

    async def run() -> None:
        async with build_client(access_token=tokens.access_token) as esi:
            mine = await character.my_orders(esi, tokens.character_id)
        if not mine:
            console.print("[yellow]no open orders[/]")
            return

        async with Database(settings.database_url) as db:
            ledger = Ledger(db)
            regions = {o.region_id for o in mine}
            book = []
            for region_id in regions:
                snapshot_id = await db.latest_snapshot(region_id)
                if snapshot_id is None:
                    continue
                for type_id in {o.type_id for o in mine}:
                    book.extend(await db.orders_for_type(snapshot_id, type_id))

            if not book:
                console.print(
                    "[red]no market snapshot covering your orders.[/] Run "
                    "`eve-market snapshot <region>` for the regions you trade in."
                )
                raise typer.Exit(1)

            # Sell orders are floored at cost; buy orders are capped so you
            # don't win a bidding war into negative margin.
            floors: dict[int, float] = {}
            for type_id in {o.type_id for o in mine if not o.is_buy_order}:
                guide = await ledger.price_guide(
                    type_id,
                    sales_tax=settings.sales_tax,
                    broker_fee=settings.broker_fee,
                )
                if guide.floor_price > 0:
                    floors[type_id] = guide.floor_price

            actions = relist_mod.build_worklist(
                mine, book, undercut_isk=settings.undercut_isk, floors=floors
            )
            names = await db.type_names([a.type_id for a in actions])

        shown = [a for a in actions if a.needs_action] if only_action else actions
        shown = shown[:limit]

        counts = relist_mod.summarize(actions)
        console.print(
            "  ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "no orders"
        )

        if not shown:
            console.print("[green]nothing to relist — you're on top everywhere.[/]")
            return

        table = Table("Item", "Side", "Status", "Yours", "Best rival", "Relist at", "At stake")
        for a in shown:
            table.add_row(
                names.get(a.type_id, str(a.type_id)),
                "buy" if a.order.is_buy_order else "sell",
                STATUS_STYLE.get(a.status, a.status.value),
                f"{a.my_price:,.2f}",
                f"{a.best_competing:,.2f}" if a.best_competing else "—",
                f"{a.suggested_price:,.2f}" if a.suggested_price else "—",
                f"{(a.gain_per_unit or 0) * a.units_at_stake:,.0f}",
            )
        console.print(table)

        blocked = [a for a in actions if a.status is Status.BELOW_FLOOR]
        if blocked:
            console.print(
                Panel(
                    f"{len(blocked)} order(s) undercut below your cost floor. "
                    "Matching those prices would lose money — hold, or sell into "
                    "the buy orders instead.",
                    border_style="yellow",
                )
            )

        # Walk the worklist interactively.
        actionable = [a for a in shown if a.needs_action and a.suggested_price]
        if not actionable:
            return
        console.print(
            f"\n[bold]Walking {len(actionable)} order(s).[/] "
            "Enter to take the next, q to stop."
        )
        async with build_client(access_token=tokens.access_token) as esi:
            for a in actionable:
                name = names.get(a.type_id, str(a.type_id))
                price = clip.format_price(a.suggested_price or 0.0)
                copied = clip.try_copy(price)
                if open_window:
                    try:
                        await character.open_market_window(esi, a.type_id)
                    except Exception as exc:  # noqa: BLE001
                        console.print(f"[yellow]could not open window: {exc}[/]")
                console.print(
                    f"\n[bold]{name}[/] — relist at [bold green]{price}[/]"
                    + ("  [dim](copied)[/]" if copied else "")
                )
                if input("  ").strip().lower() == "q":
                    break

    asyncio.run(run())


def sync(
    broker_fee_on_buys: float = typer.Option(
        0.0, help="Assume this broker fee rate on imported purchases"
    ),
) -> None:
    """Import wallet transactions into the cost basis ledger.

    ESI keeps roughly the last 30 days of transactions, so syncing regularly
    matters — history that ages out is gone for good.
    """
    tokens = _require_tokens()

    async def run() -> None:
        async with build_client(access_token=tokens.access_token) as esi:
            transactions = await character.my_transactions(esi, tokens.character_id)

        async with Database(settings.database_url) as db:
            counts = await Ledger(db).sync_transactions(
                transactions,
                sales_tax=settings.sales_tax,
                broker_fee_on_buys=broker_fee_on_buys,
            )

        table = Table("", "Count")
        table.add_row("Purchases imported", str(counts["purchases"]))
        table.add_row("Sales imported", str(counts["sales"]))
        table.add_row("Skipped (no matching stock)", str(counts["skipped"]))
        table.add_row("Already imported", str(counts["already_seen"]))
        console.print(table)
        if counts["skipped"]:
            console.print(
                "[yellow]Skipped sales had no purchase on record.[/] That's normal "
                "on a first sync — record those lots with `eve-market buy` if you "
                "want their profit tracked."
            )

    asyncio.run(run())


def open_market(item: str = typer.Argument(..., help="Item name or type id")) -> None:
    """Open an item's market window in your running EVE client."""
    tokens = _require_tokens()

    async def run() -> None:
        async with Database(settings.database_url) as db:
            from .cli_stock import _type_id_for

            type_id = await _type_id_for(db, item)
        async with build_client(access_token=tokens.access_token) as esi:
            await character.open_market_window(esi, type_id)
        console.print(f"[green]opened market window[/] for type {type_id}")

    asyncio.run(run())


def register(app: typer.Typer) -> None:
    """Attach the authenticated commands to the main CLI app."""
    app.command()(login)
    app.command()(whoami)
    app.command()(orders)
    app.command()(relist)
    app.command()(sync)
    app.command("open")(open_market)
