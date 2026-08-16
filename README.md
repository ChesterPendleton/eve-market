# eve-market

Market and trading analysis for EVE Online, built on ESI.

Finds two things:

- **Station-trading spreads** — buy low on a buy order, sell high on a sell
  order, at the same station.
- **Cross-region hauls** — buy in one hub, move the goods, sell in another.

Everything runs against **public** ESI endpoints, so no EVE login is needed to
use it. SSO is only required if you later add character-owned orders or wallet
history.

---

## Setup on your PC

```bash
git clone <your-remote> eve-market
cd eve-market
./setup.sh
```

`setup.sh` is idempotent — re-run it any time. It will:

1. Check for Python 3.11+
2. Create `.venv` and install the package
3. Write `.env` and prompt for your contact email
4. Check that ESI is reachable
5. Start Postgres and Redis via `docker compose`
6. Apply the database schema
7. Download static data (item names, cargo volumes) and load it
8. Run `eve-market doctor` to verify all of the above

Pass `--no-sde` to skip the ~40 MB static data download. Without it the tool
still works, but items display as numeric type ids and hauling can't rank by
ISK/m³.

### First run

```bash
source .venv/bin/activate

eve-market snapshot the_forge          # pull Jita's order book
eve-market spreads the_forge           # rank station-trading spreads
eve-market snapshot domain             # pull Amarr too
eve-market haul the_forge domain       # find hauls between them
```

`snapshot` on a major region pulls 100k+ orders across a dozen-plus pages;
expect it to take a minute the first time and to be near-instant afterwards
while the cache is warm.

---

## Offline mode

Set `EVE_ESI_LIVE=false` and every ESI call is served from recorded JSON in
`tests/fixtures/` instead of the network. The whole app — CLI included — runs
with no internet access at all.

This is how the project was built and tested, and it's useful for working on
analysis logic without burning ESI's error budget. To record new fixtures,
save a real response as `tests/fixtures/<path with / replaced by _>.json`, e.g.
`/v1/markets/10000002/orders/` becomes `_v1_markets_10000002_orders_.json`.

---

## Configuration

All settings come from `.env` (see `.env.example`) and are prefixed `EVE_`.

| Variable | Default | Notes |
| --- | --- | --- |
| `EVE_CONTACT_EMAIL` | — | **Required.** CCP mandates a contact address in the User-Agent |
| `EVE_ESI_LIVE` | `false` | `true` hits real ESI, `false` uses fixtures |
| `EVE_DATABASE_URL` | `postgresql://eve:eve@localhost:5432/eve_market` | |
| `EVE_REDIS_URL` | `redis://localhost:6379/0` | Falls back to in-memory if absent |
| `EVE_SALES_TAX` | `0.036` | 8% base, −11%/level of Accounting. `0.036` = Accounting V |
| `EVE_BROKER_FEE` | `0.015` | 3% base, reduced by Broker Relations and standings |
| `EVE_ESI_CONCURRENCY` | `8` | Max in-flight ESI requests |

**Set the tax and fee rates to match your character.** They're applied to every
profit calculation, and leaving them at the defaults when your skills are lower
will make marginal trades look profitable when they aren't.

---

## How the numbers are calculated

**Station trading** — you place both orders, so the broker fee is charged
twice and sales tax once:

```
cost    = buy_price  × (1 + broker_fee)
revenue = sell_price × (1 − broker_fee − sales_tax)
profit  = revenue − cost
```

**Hauling** — you fill existing orders, so selling into a buy order pays sales
tax but no broker fee:

```
profit = dest_buy_price × (1 − sales_tax) − source_sell_price
```

Pass `sell_to_buy_orders=False` if you'd rather list your own sell orders at
the destination, and the broker fee applies there too.

Two deliberate conservatisms, because screeners that omit them produce numbers
that never materialise:

- **Capture rate.** `Est. ISK/day` assumes you win only **10%** of an item's
  daily volume. You are not the only trader in Jita.
- **ISK/m³ ranking.** Hauls rank by ISK per cubic metre, not total profit,
  because cargo space is the binding constraint on any real haul.

---

## Commands

| Command | Purpose |
| --- | --- |
| `eve-market doctor` | Check ESI, Redis, Postgres and config; run this first |
| `eve-market migrate` | Create the database schema |
| `eve-market snapshot <region>` | Pull and store a region's order book |
| `eve-market spreads <region>` | Rank station-trading spreads |
| `eve-market haul <src> <dst>` | Rank cross-region hauls |
| `eve-market load-sde <file>` | Load item names and volumes |

Regions and stations accept either a name or a raw id:
`the_forge`, `domain`, `sinq_laison`, `heimatar`, `metropolis`.

---

## ESI etiquette

The client implements the rules CCP asks third-party developers to follow.
These aren't optional niceties — ignoring them gets an application
rate-limited or banned:

- A descriptive **User-Agent** with a contact address on every request.
- Responses cached until their `Expires` header; nothing is re-requested
  early.
- Stale entries revalidated with `If-None-Match`, so a 304 costs no bandwidth
  and no error budget.
- `X-ESI-Error-Limit-Remain` watched, and requests **paused** when the shared
  budget runs low rather than burning it to zero.
- Transient 5xx retried with exponential backoff; 4xx raised immediately.

Note that Tranquility has a daily downtime around **11:00–11:15 UTC**, during
which ESI returns errors. That's expected, not a bug.

---

## Development

```bash
source .venv/bin/activate
pytest                       # 33 tests
ruff check .
```

Database tests skip automatically if Postgres isn't reachable, so the suite
runs on a bare checkout.

```
src/eve_market/
  config.py          settings, region and station ids
  cache.py           Redis with in-memory fallback
  db.py              Postgres persistence
  esi/
    client.py        ETag caching, pagination, error-limit backoff
    market.py        typed endpoint wrappers
    models.py        pydantic schemas
  analysis/
    margins.py       station-trading spread maths
    hauling.py       cross-region arbitrage
  cli.py             typer CLI
sql/schema.sql       snapshots, history, item data
```

---

## Where to take it next

- **Character data** — the SSO scaffolding is stubbed in `config.py`
  (`client_id` / `client_secret` / `callback_url`). Register an app at
  [developers.eveonline.com](https://developers.eveonline.com/applications),
  add the OAuth2 PKCE flow, and `/characters/{id}/orders/` gives you your own
  positions to track against these spreads.
- **Scheduled snapshots** — a cron or systemd timer running `snapshot` hourly
  turns the snapshot table into a real time series, which is what you need to
  spot spreads widening rather than just spreads existing.
- **Player structures** — `/markets/structures/{id}/` covers trade hubs in
  citadels, but it needs auth and a structure id you have docking access to.
- **A web UI** — the analysis functions are pure and take plain lists of
  orders, so wrapping them in FastAPI is straightforward.
