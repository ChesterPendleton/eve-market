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
