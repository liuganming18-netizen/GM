-- 实时采集与目录分类（可重复、非破坏增量迁移 v5）
-- 目的：
--   1) 实时最新价/标记价/当前费率与**已收盘 K 线分开**存放：K 线只入已收盘记录，
--      未收盘 bar 仅作为图表临时层，不写 candle_* 表。
--   2) 采集进程心跳与轮次留证（谁在跑、跑到哪、最新事件时间）。
--   3) 标的物类别分类（币安 underlyingType / 欧易 instCategory）与排除原因，
--      与 symbol_pool 分开，避免对既有表做 ALTER。
-- 原则：只 CREATE TABLE/INDEX IF NOT EXISTS；不 ALTER、不 DROP、不改删既有行情表。

-- ① 实时最新快照（每个 标的 一行，覆盖式更新）
CREATE TABLE IF NOT EXISTS realtime_quotes (
    exchange             TEXT NOT NULL,
    market               TEXT NOT NULL,
    symbol               TEXT NOT NULL,
    last_price           TEXT,
    price_change_pct_24h TEXT,
    quote_volume_24h     TEXT,     -- 计价（USDT）24h 成交额；**不是**滚动 5m 成交量
    high_24h             TEXT,
    low_24h              TEXT,
    mark_price           TEXT,
    funding_rate         TEXT,     -- 当前费率（非结算事件，结算事件另存 funding_history）
    next_funding_time    INTEGER,
    event_time           INTEGER,  -- 交易所事件时间
    received_at          INTEGER,  -- 本机接收时间
    fetched_at           INTEGER,  -- 本机请求完成时间
    source               TEXT,     -- binance.fapi / okx.v5
    transport            TEXT,     -- ws / rest
    unit_note            TEXT,
    updated_at           INTEGER NOT NULL,
    PRIMARY KEY(exchange, market, symbol)
);
CREATE INDEX IF NOT EXISTS realtime_quotes_by_symbol ON realtime_quotes(exchange, symbol);

-- ② 标的物类别分类（币安 underlyingType / 欧易 instCategory）
CREATE TABLE IF NOT EXISTS symbol_classification (
    exchange        TEXT NOT NULL,
    market          TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    instrument_category TEXT,          -- COIN / INDEX / EQUITY / ... ；欧易为 1/3/4/5/6
    category_name   TEXT,              -- Crypto / Stocks / Commodities / Forex / Bonds
    category_field  TEXT,              -- 依据字段名（underlyingType / instCategory）
    contract_type   TEXT,
    in_active_range INTEGER NOT NULL DEFAULT 0,   -- 1=加密币活动范围
    excluded_reason TEXT,
    raw_json        TEXT,              -- 该标的的原始字段（取证）
    snapshot_utc    INTEGER NOT NULL,
    source_call_id  INTEGER,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY(exchange, market, symbol)
);
CREATE INDEX IF NOT EXISTS symbol_classification_by_cat
    ON symbol_classification(exchange, in_active_range, instrument_category);

-- ③ 采集进程状态与心跳
CREATE TABLE IF NOT EXISTS collector_state (
    name          TEXT PRIMARY KEY,
    pid           INTEGER,
    started_at    INTEGER,
    heartbeat_at  INTEGER,
    status        TEXT,                -- running / paused_no_space / stopped / error
    detail        TEXT,
    counters_json TEXT,
    updated_at    INTEGER NOT NULL
);

-- ④ 采集轮次留证（每轮 REST/WS 快照的覆盖与新鲜度）
CREATE TABLE IF NOT EXISTS collector_rounds (
    id               INTEGER PRIMARY KEY,
    kind             TEXT NOT NULL,     -- ticker_rest / mark_rest / oi_sweep / funding_history / ws_snapshot
    exchange         TEXT,
    market           TEXT,
    started_at       INTEGER NOT NULL,
    finished_at      INTEGER,
    symbols_total    INTEGER,
    symbols_ok       INTEGER,
    symbols_failed   INTEGER,
    newest_event_time INTEGER,
    oldest_event_time INTEGER,
    detail           TEXT
);
CREATE INDEX IF NOT EXISTS collector_rounds_by_kind ON collector_rounds(kind, started_at);
