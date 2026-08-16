# eve-market

Market trading tooling for EVE Online, built around one job: **stocking a
secondary market from Jita profitably.** Default destination is Ahbazon.

It answers, in order:

1. What in Jita is liquid and worth the cargo space?
2. Of those, what does the destination actually want — many buy orders, thin
   or absent sell orders?
3. What must I list at, and what does that net after fees and hauling?
4. What did my stock cost me, and what's my real profit?

It also does plain station trading in one hub (`spreads`) and generic
region-to-region arbitrage (`haul`).

---

## How it talks to the game

Same model as EVE Tycoon and other ESI trading tools:

- **It reads your character through ESI** — live orders, wallet transactions,
  assets — over SSO. That's what drives the relist worklist and fills the
  cost-basis ledger automatically.
- **It opens windows in your running client** via ESI's official UI endpoints
  (`/ui/openwindow/marketdetails/`, `/ui/autopilot/waypoint/`), gated behind
  the `esi-ui.*` scopes. `eve-market relist --open` pops each item's market
  window as it walks your worklist.
- **It puts the price on your clipboard.** You press Ctrl+V.

The one hard limit: **ESI has no endpoint to place, modify, or cancel a market
order.** CCP has never exposed order writes. So the tool can open the window
and hand you the exact number, but confirming the order is always yours. No
keystroke or mouse automation is involved anywhere — that would be an EULA
violation, and it isn't needed for this workflow.

---

## Setup on your PC

```bash
./setup.sh
source .venv/bin/activate
eve-market doctor
```

Then confirm the destination — **do this before trusting any number**:

```bash
eve-market resolve Ahbazon
```

That prints the real system id, region id and security status from ESI, plus
the `.env` lines to paste. The shipped defaults are unverified guesses; if the
region id is wrong you'll analyse an empty market and not notice.

`resolve` also warns you if the system is lowsec, and if it has no NPC stations
(meaning the market is a citadel — see *Citadel markets* below).

---

## The trading loop

```bash
# 1. Pull both order books and their history
eve-market snapshot the_forge
eve-market snapshot genesis
eve-market fetch-history the_forge
eve-market fetch-history genesis

# 2. What should I stock?
eve-market stock                      # ranked by ISK per m3
eve-market stock --buy-orders         # price it as buying via buy orders
eve-market stock --undersupplied      # only where demand outweighs supply
eve-market stock --ship freighter     # see what lowsec risk does to margins
eve-market stock --no-fit             # everything, not just one hold's worth

# 3. Buy it
eve-market buy-list                   # Multibuy list, copied to clipboard
eve-market price "Warp Disruptor II" --side buy   # bid to top the Jita book

# 4. Record what you paid
eve-market buy "Warp Disruptor II" 500 1180000
eve-market buy "Damage Control II" 300 498000 --via-order

# 5. After the trip, load the freight cost in
eve-market haul-cost 12000000         # split across lots by m3

# 6. Price your listings
eve-market price "Warp Disruptor II"  # floored at break-even AND restock cost

# 7. Record sales and check the damage
eve-market sell "Warp Disruptor II" 200 1949999.99
eve-market sell "Warp Disruptor II" 100 1400000 --to-buy-order
eve-market position
eve-market pnl
```

### What `stock` shows

```
Item                  Jita        Landed      List at     Profit/u   Margin  ISK/m3   Demand
Warp Disruptor II     1,180,000   1,191,800   1,949,999   658,749    55.3%   131,750  2 bids / 1 asks
Damage Control II       498,000     502,980     672,300   135,032    26.8%    27,007  no sellers
```

**`no sellers`** is the flag to care about. Nobody is competing, so you set the
price rather than undercutting one — that's the niche you're trying to fill.

Ranking is by **ISK per m3**, not margin or total profit, because cargo space
is what limits a run. `--no-fit` shows everything; by default the list is
trimmed to what fits in one trip.

---

## Connecting your character (SSO)

```bash
eve-market login      # opens EVE SSO in your browser
eve-market whoami     # confirm character and granted scopes
```

First register a **native** application at
[developers.eveonline.com/applications](https://developers.eveonline.com/applications),
set its callback URL to exactly your `EVE_CALLBACK_URL`, and put the client id
in `.env` as `EVE_CLIENT_ID`. No client secret — this uses the PKCE flow.

Tokens are stored at `~/.config/eve-market/tokens.json` with `0600`
permissions and refreshed automatically. The refresh token is a long-lived
credential: anyone holding it can read your character data until you revoke it
in EVE's third-party application settings.

Then the relist loop:

```bash
eve-market orders               # your live market orders
eve-market relist               # what's been undercut, and the price to fix it
eve-market relist --open        # ...and open each market window in the client
eve-market sync                 # import wallet transactions into the ledger
eve-market open "Warp Disruptor II"   # open one item's market window
```

`relist` output:

```
best: 1  outbid: 1  undercut: 1
Item                              Side  Status    Yours         Best rival    Relist at     At stake
Warp Disruptor II                 sell  undercut  2,100,000.00  1,950,000.00  1,949,999.99  12,000,001
Multispectrum Shield Hardener II  buy   outbid       80,000.00     91,000.00     91,000.01   5,500,005
```

Then it walks them one at a time — copying each price and, with `--open`,
opening that item's market window — so the loop is: Enter, alt-tab, Ctrl+V,
confirm, back.

Sorted by **ISK at stake** (units remaining × the price gap), not by the size
of the gap, so the order actually costing you the most is first. Orders you're
already winning are hidden by default: relisting those burns a broker fee for
nothing. And an order undercut *below your cost floor* is flagged rather than
suggested — matching that price would lose money.

`sync` imports wallet transactions and dedupes them, so purchases and sales
land in the ledger without typing `buy`/`sell` by hand. ESI keeps roughly the
last 30 days, so sync regularly — aged-out history is gone for good.

---

## Pricing: the three numbers

`eve-market price` reports three prices, and the distinction is the whole point:

| | Meaning |
| --- | --- |
| **Break-even** | Recovers what this stock actually cost, including its share of the haul. Sunk cost. |
| **Replacement** | Recovers what it would cost to buy and haul it *again* at today's Jita price. |
| **Floor** | The higher of the two. Never list below this. |

Selling above break-even but below replacement feels profitable and quietly
shrinks the business — every sale funds less stock than it consumed. That's
what "build in restock costs" means, and it's enforced: if undercutting the
market would drop you under the floor, the tool refuses to suggest a price and
tells you to hold or sell into the buy orders instead.

---

## Hauling and risk

Ahbazon is a lowsec chokepoint, so risk is priced as a share of cargo value:

| Ship | Capacity | Lowsec risk charged |
| --- | --- | --- |
| Blockade Runner | 11,000 m³ | 1% |
| Deep Space Transport | 62,000 m³ | 1% |
| Freighter | 435,000 m³ | **15%** |

That 15% is not decoration. On the numbers above it takes Damage Control II
from a 26.8% margin to 11.4%. A freighter through a camped lowsec gate is
bait, and the tool prices it that way rather than letting the margin look
better than it is. Override with `EVE_HAUL_RISK_PCT` if you disagree.

Haul costs are allocated across lots **by m³**, not by ISK value — volume is
what filled the hold. Allocating by value would make cheap bulky goods look
like they shipped for free.

---

## Two asymmetries the tool gets right

**Buy orders are a demand signal, not your customers.** Someone with a buy
order up wants the item *cheaper* than it's selling for. When you list a sell
order you're serving a different buyer — the one who wants it now. So buy-order
depth ranks demand, but quantities are sized from the destination region's
actual traded volume.

**Fees differ by how you transact.** Placing an order costs the broker fee;
filling someone else's does not. So buying via buy orders costs broker fee,
buying off sell orders doesn't; listing costs broker fee plus sales tax, while
selling into a buy order pays sales tax only. All four paths are modelled
separately.

---

## Citadel markets

If the destination market is a player citadel, unauthenticated ESI cannot see
it at all. Use the client's own export:

1. In EVE, open the market and click export.
2. `eve-market import-marketlog ~/Documents/EVE/logs/Marketlogs`

That loads the client's CSV as a snapshot, and everything downstream works
normally. Reading a file the client wrote is not automation.

---

## Configuration

`.env`, all prefixed `EVE_`. See `.env.example`.

| Variable | Default | Notes |
| --- | --- | --- |
| `EVE_CONTACT_EMAIL` | — | **Required.** CCP mandates it in the User-Agent |
| `EVE_ESI_LIVE` | `false` | `false` runs entirely from fixtures, no network |
| `EVE_DEST_SYSTEM_ID` | `0` | **Set this via `resolve`** — 0 means unresolved |
| `EVE_DEST_IS_LOWSEC` | `true` | Drives the risk model; wrong value distorts every margin |
| `EVE_SHIP` | `dst` | `blockade_runner`, `dst`, `freighter` |
| `EVE_SALES_TAX` | `0.036` | 8% base, −11%/level Accounting. `0.036` = Accounting V |
| `EVE_BROKER_FEE` | `0.015` | 3% base, less Broker Relations and standings |
| `EVE_CAPTURE_RATE` | `0.25` | Share of destination turnover you assume you win |
| `EVE_DAYS_OF_STOCK` | `7.0` | How many days of demand to carry |
| `EVE_GREENFIELD_MARKUP` | `0.35` | Markup when nobody else is selling |

**Set the tax and fee rates to match your character.** They're applied to every
calculation, and leaving them optimistic makes marginal trades look viable.

---

## Offline mode

`EVE_ESI_LIVE=false` serves every call from `tests/fixtures/`. The entire app,
CLI included, runs with no internet. This is how it was developed and tested.

To record a real fixture, save the response as the request path with `/`
replaced by `_`: `/v1/markets/10000002/orders/` →
`_v1_markets_10000002_orders_.json`.

---

## Commands

| Command | Purpose |
| --- | --- |
| `doctor` | Check ESI, Redis, Postgres, config |
| `login` / `whoami` | Connect your character via SSO |
| `orders` | Your live market orders |
| `relist` | **Undercut worklist** with the price to fix each |
| `sync` | Import wallet transactions into the ledger |
| `open <item>` | Open an item's market window in the client |
| `resolve <system>` | Confirm destination ids and security status |
| `migrate` | Create the schema |
| `snapshot <region>` | Pull and store a region's order book |
| `fetch-history <region>` | Pull daily volume history |
| `stock` | **Main screen** — what to buy in Jita for the destination |
| `buy-list` | Multibuy shopping list → clipboard |
| `price <item>` | Price to list or bid at → clipboard |
| `buy` / `sell` | Record purchases and sales |
| `haul-cost <isk>` | Allocate a trip's cost across lots |
| `position` / `pnl` | Stock on hand, capital, realised profit |
| `import-marketlog <dir>` | Import the client's market export |
| `spreads <region>` | Station trading within one hub |
| `haul <src> <dst>` | Generic region-to-region arbitrage |

---

## Development

```bash
pytest        # 115 tests
ruff check .
```

Database tests run against a **separate** `eve_market_test` database, created
by `setup.sh`. They TRUNCATE, so pointing them at your working database would
destroy your ledger. Override with `EVE_TEST_DATABASE_URL`; they skip entirely
if that database isn't reachable.

```
src/eve_market/
  config.py            settings, hub ids, ship and pricing defaults
  cache.py             Redis with in-memory fallback
  db.py                Postgres persistence
  ledger.py            cost basis, FIFO sales, restock pricing
  clipboard.py         Multibuy lists and price formatting
  marketlog.py         EVE client market log import
  auth.py              EVE SSO, PKCE flow, token storage and refresh
  esi/
    client.py          ETag caching, pagination, error-limit backoff
    market.py          market endpoint wrappers
    character.py       your orders, transactions, assets, client UI
    universe.py        name and id resolution
  analysis/
    sourcing.py        the Jita -> destination screen
    relist.py          undercut detection and the relist worklist
    logistics.py       ship capacity, freight and risk
    margins.py         station trading spreads
    hauling.py         generic region arbitrage
```

---

## ESI etiquette

Implemented, because ignoring these gets an app rate-limited or banned:
descriptive User-Agent with contact address; responses cached until `Expires`;
stale entries revalidated with `If-None-Match` so 304s cost no error budget;
`X-ESI-Error-Limit-Remain` watched and requests **paused** before the shared
budget drains; 5xx retried with backoff, 4xx raised immediately.

Tranquility's daily downtime is roughly **11:00–11:15 UTC**; ESI errors then
are expected.

---

## Next steps

- **Citadel order books.** `esi-markets.structure_markets.v1` is already in the
  requested scopes and `esi/character.py` has the wrapper; wiring
  `structure_orders` into `snapshot` would cover Ahbazon citadels without the
  market-log export.
- **Scheduled snapshots** so spreads can be watched widening, not just existing.
- **Order-state history** from `/characters/{id}/orders/history/`, to measure
  how long stock actually takes to clear at a given price. Right now
  `--days-of-stock` is your estimate; that endpoint would make it measured.
- **Corporation wallets**, if you ever run this out of a corp.
