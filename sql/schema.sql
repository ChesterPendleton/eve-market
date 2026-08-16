-- Schema for cached ESI market data.
--
-- Order books are snapshotted rather than mutated in place: ESI gives you the
-- whole book at a point in time, and keeping history lets you see how spreads
-- and depth move rather than only where they are right now.

CREATE TABLE IF NOT EXISTS market_snapshot (
    id          BIGSERIAL PRIMARY KEY,
    region_id   INTEGER     NOT NULL,
    taken_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    order_count INTEGER     NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS market_snapshot_region_time_idx
    ON market_snapshot (region_id, taken_at DESC);

CREATE TABLE IF NOT EXISTS market_order (
    snapshot_id   BIGINT      NOT NULL REFERENCES market_snapshot(id) ON DELETE CASCADE,
    order_id      BIGINT      NOT NULL,
    type_id       INTEGER     NOT NULL,
    location_id   BIGINT      NOT NULL,
    system_id     INTEGER,
    is_buy_order  BOOLEAN     NOT NULL,
    price         NUMERIC(18,2) NOT NULL,
    volume_remain INTEGER     NOT NULL,
    volume_total  INTEGER     NOT NULL,
    min_volume    INTEGER     NOT NULL DEFAULT 1,
    duration      INTEGER     NOT NULL,
    issued        TIMESTAMPTZ NOT NULL,
    range         TEXT        NOT NULL DEFAULT 'station',
    PRIMARY KEY (snapshot_id, order_id)
);

-- The screening queries all filter by type within a snapshot, then split on
-- buy/sell, so this composite covers them.
CREATE INDEX IF NOT EXISTS market_order_lookup_idx
    ON market_order (snapshot_id, type_id, is_buy_order, price);

CREATE INDEX IF NOT EXISTS market_order_location_idx
    ON market_order (snapshot_id, location_id, type_id);

-- Daily aggregates are immutable once a day closes, so these upsert by key
-- instead of being snapshotted.
CREATE TABLE IF NOT EXISTS market_history (
    region_id   INTEGER       NOT NULL,
    type_id     INTEGER       NOT NULL,
    date        DATE          NOT NULL,
    average     NUMERIC(18,2) NOT NULL,
    highest     NUMERIC(18,2) NOT NULL,
    lowest      NUMERIC(18,2) NOT NULL,
    order_count INTEGER       NOT NULL,
    volume      BIGINT        NOT NULL,
    PRIMARY KEY (region_id, type_id, date)
);

CREATE INDEX IF NOT EXISTS market_history_recent_idx
    ON market_history (region_id, type_id, date DESC);

-- Minimal slice of the Static Data Export: enough to name items and compute
-- cargo volume for hauling. Populated by `eve-market load-sde`.
CREATE TABLE IF NOT EXISTS inv_type (
    type_id     INTEGER PRIMARY KEY,
    type_name   TEXT    NOT NULL,
    group_id    INTEGER,
    volume      DOUBLE PRECISION,
    packaged_volume DOUBLE PRECISION,
    published   BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS inv_type_name_idx ON inv_type (lower(type_name));

-- ---------------------------------------------------------------------------
-- Cost basis ledger
--
-- Inventory is tracked as lots rather than a running average, because the
-- question "what did THIS stock cost me" needs the individual purchase to
-- still be there. Sales consume lots FIFO, which matches how you'd actually
-- clear old stock first.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS purchase_lot (
    id            BIGSERIAL PRIMARY KEY,
    type_id       INTEGER       NOT NULL,
    qty           INTEGER       NOT NULL CHECK (qty > 0),
    qty_remaining INTEGER       NOT NULL CHECK (qty_remaining >= 0),
    unit_price    NUMERIC(18,2) NOT NULL,
    -- Broker fee paid to place the buy order, if bought that way.
    fees          NUMERIC(18,2) NOT NULL DEFAULT 0,
    -- Freight and risk allocated to this lot; filled in by `haul` after the
    -- trip, since you don't know the trip cost at purchase time.
    haul_cost     NUMERIC(18,2) NOT NULL DEFAULT 0,
    station_id    BIGINT,
    acquired_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),
    note          TEXT
);

-- Open lots are read constantly for pricing; closed ones only for history.
CREATE INDEX IF NOT EXISTS purchase_lot_open_idx
    ON purchase_lot (type_id, acquired_at) WHERE qty_remaining > 0;

CREATE TABLE IF NOT EXISTS sale (
    id              BIGSERIAL PRIMARY KEY,
    type_id         INTEGER       NOT NULL,
    qty             INTEGER       NOT NULL CHECK (qty > 0),
    unit_price      NUMERIC(18,2) NOT NULL,
    gross           NUMERIC(18,2) NOT NULL,
    fees            NUMERIC(18,2) NOT NULL DEFAULT 0,
    cogs            NUMERIC(18,2) NOT NULL DEFAULT 0,
    realized_profit NUMERIC(18,2) NOT NULL DEFAULT 0,
    station_id      BIGINT,
    sold_at         TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS sale_type_time_idx ON sale (type_id, sold_at DESC);

-- Which lots covered which sale. Kept so a surprising profit number can be
-- traced back to the purchases behind it.
-- Wallet transactions already folded into the ledger. ESI returns a rolling
-- window that overlaps heavily between syncs, so this is what stops the same
-- purchase being counted five times.
CREATE TABLE IF NOT EXISTS imported_transaction (
    transaction_id BIGINT PRIMARY KEY,
    imported_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS lot_consumption (
    sale_id          BIGINT NOT NULL REFERENCES sale(id) ON DELETE CASCADE,
    lot_id           BIGINT NOT NULL REFERENCES purchase_lot(id) ON DELETE CASCADE,
    qty              INTEGER       NOT NULL,
    unit_landed_cost NUMERIC(18,2) NOT NULL,
    PRIMARY KEY (sale_id, lot_id)
);

-- Closed orders from /characters/{id}/orders/history/. ESI keeps ~90 days;
-- syncing regularly builds a durable record of how fast stock actually sells.
CREATE TABLE IF NOT EXISTS character_order_history (
    order_id      BIGINT PRIMARY KEY,
    type_id       INTEGER       NOT NULL,
    region_id     INTEGER,
    location_id   BIGINT,
    is_buy_order  BOOLEAN       NOT NULL,
    price         NUMERIC(18,2) NOT NULL,
    volume_total  INTEGER       NOT NULL,
    volume_remain INTEGER       NOT NULL,
    duration      INTEGER       NOT NULL,
    issued        TIMESTAMPTZ   NOT NULL,
    state         TEXT          NOT NULL,
    recorded_at   TIMESTAMPTZ   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_order_history_type
    ON character_order_history (type_id, issued);
