"""Local web dashboard: the stock screen, relist worklist and ledger in a browser.

Design constraints, in order:

- **Local only.** Binds 127.0.0.1. Your SSO tokens and ledger never leave the
  machine; there is no cloud half to this.
- **Same brain as the CLI.** Every number comes from the same analysis and
  ledger code the terminal commands use — the UI adds buttons, not new math.
- **The game stays the interface for orders.** ESI cannot place, change or
  cancel a market order, so the dashboard does what the CLI does: hands you
  the exact price (copy button) and opens the right market window in the
  client. Confirming is always yours.

Run with ``eve-market ui``. Requires the ``[ui]`` extra (fastapi + uvicorn).
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from . import auth
from . import clipboard as clip
from .analysis import relist as relist_mod
from .analysis import sourcing
from .analysis.logistics import SHIPS, profile_for, warn_for
from .config import REGIONS, settings
from .db import Database
from .esi import character, market
from .esi.client import build_client
from .esi.universe import system_info
from .ledger import Ledger

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="eve-market", docs_url=None, redoc_url=None)


# ---------------------------------------------------------------------------
# One-at-a-time background jobs (snapshot, history, sync, login)
# ---------------------------------------------------------------------------
# Snapshots of big regions take a minute or more; an HTTP request shouldn't
# hang that long. One job at a time is deliberate: these all contend for the
# same ESI error budget, and the UI has no queue worth managing.

_job: dict[str, Any] = {"name": None, "state": "idle", "detail": "", "finished": None}
_job_task: asyncio.Task | None = None


def _job_running() -> bool:
    return _job_task is not None and not _job_task.done()


def _start_job(name: str, coro: Any) -> None:
    global _job_task
    if _job_running():
        raise HTTPException(409, f"a job is already running: {_job['name']}")
    _job.update(name=name, state="running", detail="", finished=None)

    async def wrap() -> None:
        try:
            detail = await coro
            _job.update(state="done", detail=detail or "")
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI, not lost
            _job.update(state="error", detail=str(exc)[:300])
        finally:
            _job.update(finished=datetime.now(UTC).isoformat())

    _job_task = asyncio.get_running_loop().create_task(wrap())


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _tokens() -> auth.Tokens | None:
    try:
        return auth.ensure_fresh(settings.client_id)
    except Exception:  # noqa: BLE001 - a broken refresh reads as "not logged in"
        return None


def _region_id(name_or_id: str) -> int:
    if name_or_id.isdigit():
        return int(name_or_id)
    key = name_or_id.lower().replace(" ", "_").replace("-", "_")
    if key in REGIONS:
        return REGIONS[key]
    raise HTTPException(400, f"unknown region {name_or_id!r}")


async def _snapshot_meta(db: Database) -> list[dict[str, Any]]:
    pool = db._require_pool()
    rows = await pool.fetch(
        "SELECT DISTINCT ON (region_id) region_id, taken_at, order_count "
        "FROM market_snapshot ORDER BY region_id, taken_at DESC"
    )
    region_names = {v: k for k, v in REGIONS.items()}
    out = []
    for r in rows:
        taken = r["taken_at"]
        if taken.tzinfo is None:
            taken = taken.replace(tzinfo=UTC)
        age_min = (datetime.now(UTC) - taken).total_seconds() / 60
        out.append(
            {
                "region_id": r["region_id"],
                "region": region_names.get(r["region_id"], str(r["region_id"])),
                "orders": r["order_count"],
                "age_minutes": round(age_min),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
        '<rect width="16" height="16" rx="3" fill="#1a1a19"/>'
        '<path d="M3 10l3-4 3 2 4-5" stroke="#3987e5" stroke-width="1.6" fill="none"/>'
        '<circle cx="13" cy="3" r="1.4" fill="#0ca30c"/></svg>'
    )
    return Response(svg, media_type="image/svg+xml")


# ---------------------------------------------------------------------------
# Read APIs
# ---------------------------------------------------------------------------


@app.get("/api/status")
async def status() -> dict[str, Any]:
    tokens = _tokens()
    snapshots: list[dict[str, Any]] = []
    sde_types = 0
    db_ok = True
    db_error = ""
    try:
        async with Database(settings.database_url) as db:
            snapshots = await _snapshot_meta(db)
            sde_types = await db._require_pool().fetchval("SELECT COUNT(*) FROM inv_type")
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        db_error = str(exc)[:200]

    return {
        "esi_live": settings.esi_live,
        "db_ok": db_ok,
        "db_error": db_error,
        "sde_types": sde_types,
        "snapshots": snapshots,
        "character": tokens.character_name if tokens else None,
        "sso_configured": bool(settings.client_id),
        "dest": {
            "name": settings.dest_name,
            "region_id": settings.dest_region_id,
            "system_id": settings.dest_system_id,
            "is_lowsec": settings.dest_is_lowsec,
            "resolved": settings.dest_system_id != 0,
        },
        "ship": settings.ship,
        "ships": list(SHIPS),
        "regions": list(REGIONS),
        "sales_tax": settings.sales_tax,
        "broker_fee": settings.broker_fee,
    }


@app.get("/api/job")
async def job() -> dict[str, Any]:
    return dict(_job, running=_job_running())


@app.get("/api/stock")
async def stock(
    ship: str | None = None,
    buy_orders: bool = False,
    min_margin: float = 0.10,
    min_volume: float = 20.0,
    min_price: float = 1000.0,
    undersupplied: bool = False,
    fit: bool = True,
    limit: int = 40,
) -> dict[str, Any]:
    profile = profile_for(
        ship or settings.ship,
        route_is_lowsec=settings.dest_is_lowsec,
        self_hauling=settings.self_hauling,
        cost_per_m3=settings.haul_cost_per_m3,
        risk_pct=settings.haul_risk_pct,
    )
    capacity = settings.cargo_m3 or profile.capacity_m3

    async with Database(settings.database_url) as db:
        src_snap = await db.latest_snapshot(settings.source_region_id)
        dst_snap = await db.latest_snapshot(settings.dest_region_id)
        if src_snap is None or dst_snap is None:
            return {
                "error": "need snapshots of both the source and destination regions",
                "hint": "Run the two Snapshot buttons above, then Fetch history.",
                "rows": [],
            }
        pool = db._require_pool()
        dest_types = [
            r["type_id"]
            for r in await pool.fetch(
                "SELECT DISTINCT type_id FROM market_order "
                "WHERE snapshot_id = $1 AND ($2::int = 0 OR system_id = $2)",
                dst_snap,
                settings.dest_system_id,
            )
        ]
        if not dest_types:
            return {
                "error": "no orders found at the destination system",
                "hint": "Check EVE_DEST_SYSTEM_ID, or the market may be a citadel "
                "(see import-marketlog in the README).",
                "rows": [],
            }

        volumes = await db.type_volumes(dest_types)
        opportunities = []
        for type_id in dest_types:
            src_orders = await db.orders_for_type(src_snap, type_id)
            if not src_orders:
                continue
            src_vol = sourcing.average_daily_volume(
                await db.history_for_type(settings.source_region_id, type_id)
            )
            if src_vol < min_volume:
                continue
            opp = sourcing.evaluate(
                type_id,
                src_orders,
                await db.orders_for_type(dst_snap, type_id),
                haul=profile,
                sales_tax=settings.sales_tax,
                broker_fee=settings.broker_fee,
                source_station_id=settings.source_station_id,
                dest_system_id=settings.dest_system_id or None,
                source_daily_volume=src_vol,
                dest_daily_volume=sourcing.average_daily_volume(
                    await db.history_for_type(settings.dest_region_id, type_id)
                ),
                unit_volume_m3=volumes.get(type_id),
                buy_with_orders=buy_orders,
                undercut_isk=settings.undercut_isk,
                greenfield_markup=settings.greenfield_markup,
                days_of_stock=settings.days_of_stock,
                capture_rate=settings.capture_rate,
                cargo_m3=capacity,
            )
            if opp is None or opp.acquire_price < min_price:
                continue
            opportunities.append(opp)

        best = sourcing.rank(
            opportunities,
            min_margin=min_margin,
            min_demand_ratio=1.0 if undersupplied else 0.0,
        )
        if fit:
            best = sourcing.fit_to_hold(best, capacity)
        best = best[:limit]
        names = await db.type_names([o.type_id for o in best])

    rows = []
    for o in best:
        d = asdict(o)
        d["name"] = names.get(o.type_id, str(o.type_id))
        rows.append(d)
    return {
        "rows": rows,
        "profile": {
            "ship": profile.ship.name,
            "capacity_m3": capacity,
            "cost_per_m3": profile.cost_per_m3,
            "risk_pct": profile.risk_pct,
        },
        "warning": warn_for(profile, settings.dest_is_lowsec),
        "totals": {
            "profit": sum(o.total_profit for o in best),
            "spend": sum(o.acquire_price * o.suggested_qty for o in best),
            "m3": sum(o.total_m3 or 0 for o in best),
        },
    }


@app.get("/api/buy-list")
async def buy_list(ship: str | None = None, min_margin: float = 0.10, limit: int = 40) -> dict:
    data = await stock(ship=ship, min_margin=min_margin, limit=limit)
    lines = [f"{r['name']} x{r['suggested_qty']}" for r in data["rows"] if r["suggested_qty"]]
    return {"text": "\n".join(lines), "count": len(lines), "error": data.get("error")}


@app.get("/api/relist")
async def relist() -> dict[str, Any]:
    tokens = _tokens()
    if tokens is None:
        return {"error": "not_logged_in", "rows": []}

    async with build_client(access_token=tokens.access_token) as esi:
        mine = await character.my_orders(esi, tokens.character_id)
    if not mine:
        return {"rows": [], "counts": {}, "message": "no open orders"}

    async with Database(settings.database_url) as db:
        ledger = Ledger(db)
        book = []
        for region_id in {o.region_id for o in mine}:
            snapshot_id = await db.latest_snapshot(region_id)
            if snapshot_id is None:
                continue
            for type_id in {o.type_id for o in mine}:
                book.extend(await db.orders_for_type(snapshot_id, type_id))
        if not book:
            return {
                "error": "no market snapshot covering your orders",
                "hint": "Take a snapshot of the regions you trade in first.",
                "rows": [],
            }
        floors: dict[int, float] = {}
        for type_id in {o.type_id for o in mine if not o.is_buy_order}:
            guide = await ledger.price_guide(
                type_id, sales_tax=settings.sales_tax, broker_fee=settings.broker_fee
            )
            if guide.floor_price > 0:
                floors[type_id] = guide.floor_price
        actions = relist_mod.build_worklist(
            mine, book, undercut_isk=settings.undercut_isk, floors=floors
        )
        names = await db.type_names([a.type_id for a in actions])

    rows = []
    for a in actions:
        rows.append(
            {
                "type_id": a.type_id,
                "name": names.get(a.type_id, str(a.type_id)),
                "side": "buy" if a.order.is_buy_order else "sell",
                "status": a.status.value,
                "my_price": a.my_price,
                "best_competing": a.best_competing,
                "suggested_price": a.suggested_price,
                "floor_price": a.floor_price,
                "at_stake": (a.gain_per_unit or 0) * a.units_at_stake,
                "remaining": a.order.volume_remain,
                "needs_action": a.needs_action,
            }
        )
    rows.sort(key=lambda r: r["at_stake"], reverse=True)
    return {"rows": rows, "counts": relist_mod.summarize(actions)}


@app.get("/api/orders")
async def orders() -> dict[str, Any]:
    tokens = _tokens()
    if tokens is None:
        return {"error": "not_logged_in", "rows": []}
    async with build_client(access_token=tokens.access_token) as esi:
        mine = await character.my_orders(esi, tokens.character_id)
    async with Database(settings.database_url) as db:
        names = await db.type_names([o.type_id for o in mine])
    rows = [
        {**o.model_dump(mode="json"), "name": names.get(o.type_id, str(o.type_id))}
        for o in sorted(mine, key=lambda o: o.price * o.volume_remain, reverse=True)
    ]
    return {"rows": rows}


@app.get("/api/positions")
async def positions() -> dict[str, Any]:
    async with Database(settings.database_url) as db:
        pos = await Ledger(db).positions()
        names = await db.type_names([p.type_id for p in pos])
    return {
        "rows": [
            {**asdict(p), "name": names.get(p.type_id, str(p.type_id))} for p in pos
        ],
        "total_capital": sum(p.capital_tied_up for p in pos),
    }


@app.get("/api/pnl")
async def pnl() -> dict[str, Any]:
    async with Database(settings.database_url) as db:
        return await Ledger(db).realized_pnl()


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


class RegionBody(BaseModel):
    region: str


class CopyBody(BaseModel):
    text: str


class TypeBody(BaseModel):
    type_id: int


@app.post("/api/actions/snapshot")
async def action_snapshot(body: RegionBody) -> dict[str, Any]:
    region_id = _region_id(body.region)

    async def run() -> str:
        async with build_client() as esi:
            book = await market.region_orders(esi, region_id)
        async with Database(settings.database_url) as db:
            snapshot_id = await db.save_snapshot(region_id, book)
        return f"saved {len(book):,} orders as snapshot {snapshot_id}"

    _start_job(f"snapshot {body.region}", run())
    return {"started": True}


@app.post("/api/actions/fetch-history")
async def action_history(body: RegionBody) -> dict[str, Any]:
    region_id = _region_id(body.region)

    async def run() -> str:
        async with Database(settings.database_url) as db, build_client() as esi:
            snapshot_id = await db.latest_snapshot(region_id)
            if snapshot_id is None:
                raise RuntimeError("no snapshot for that region — snapshot it first")
            pool = db._require_pool()
            rows = await pool.fetch(
                "SELECT type_id, SUM(volume_remain) AS depth FROM market_order "
                "WHERE snapshot_id = $1 GROUP BY type_id ORDER BY depth DESC LIMIT 400",
                snapshot_id,
            )
            type_ids = [r["type_id"] for r in rows]
            results = await market.histories(esi, region_id, type_ids)
            written = 0
            for type_id, days in results.items():
                written += await db.save_history(region_id, type_id, days)
        return f"stored {written:,} history rows for {len(type_ids)} types"

    _start_job(f"fetch history {body.region}", run())
    return {"started": True}


@app.post("/api/actions/sync")
async def action_sync() -> dict[str, Any]:
    tokens = _tokens()
    if tokens is None:
        raise HTTPException(401, "not logged in")

    async def run() -> str:
        async with build_client(access_token=tokens.access_token) as esi:
            transactions = await character.my_transactions(esi, tokens.character_id)
        async with Database(settings.database_url) as db:
            counts = await Ledger(db).sync_transactions(
                transactions, sales_tax=settings.sales_tax
            )
        return (
            f"imported {counts['purchases']} purchases, {counts['sales']} sales "
            f"({counts['already_seen']} already imported, {counts['skipped']} skipped)"
        )

    _start_job("sync wallet", run())
    return {"started": True}


@app.post("/api/actions/login")
async def action_login() -> dict[str, Any]:
    if not settings.client_id:
        raise HTTPException(
            400,
            "No EVE_CLIENT_ID configured. Register a native app at "
            "developers.eveonline.com, set the callback URL to "
            f"{settings.callback_url}, and put the client id in .env.",
        )

    async def run() -> str:
        # auth.login blocks on a localhost callback server; keep it off the
        # event loop so the UI stays responsive while the browser round-trips.
        tokens = await asyncio.to_thread(
            auth.login, settings.client_id, settings.callback_url
        )
        auth.save(tokens)
        return f"logged in as {tokens.character_name}"

    _start_job("EVE SSO login", run())
    return {"started": True}


@app.post("/api/actions/resolve")
async def action_resolve() -> dict[str, Any]:
    async with build_client() as esi:
        info = await system_info(esi, settings.dest_name)
    if info is None:
        raise HTTPException(404, f"no system named {settings.dest_name!r}")
    return {
        "name": info.name,
        "system_id": info.system_id,
        "region_id": info.region_id,
        "security": info.security_status,
        "is_lowsec": info.is_lowsec,
        "env_lines": (
            f"EVE_DEST_NAME={info.name}\n"
            f"EVE_DEST_REGION_ID={info.region_id}\n"
            f"EVE_DEST_SYSTEM_ID={info.system_id}\n"
            f"EVE_DEST_IS_LOWSEC={str(info.is_lowsec).lower()}"
        ),
    }


@app.post("/api/actions/copy")
async def action_copy(body: CopyBody) -> dict[str, Any]:
    # Fallback for when the browser clipboard API is unavailable; pyperclip
    # writes to the OS clipboard from the server process, which is the same
    # machine by construction (we bind 127.0.0.1).
    return {"copied": clip.try_copy(body.text)}


@app.post("/api/actions/open-window")
async def action_open_window(body: TypeBody) -> dict[str, Any]:
    tokens = _tokens()
    if tokens is None:
        raise HTTPException(401, "not logged in")
    async with build_client(access_token=tokens.access_token) as esi:
        await character.open_market_window(esi, body.type_id)
    return {"opened": body.type_id}


@app.exception_handler(Exception)
async def _unhandled(request: Any, exc: Exception) -> JSONResponse:
    with contextlib.suppress(Exception):
        import logging

        logging.getLogger(__name__).exception("unhandled error in %s", request.url.path)
    return JSONResponse(status_code=500, content={"error": str(exc)[:300]})
