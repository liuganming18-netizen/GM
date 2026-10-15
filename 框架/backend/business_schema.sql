-- 业务表增量迁移（可重复、非破坏）
-- 原则：只 CREATE TABLE/INDEX IF NOT EXISTS；不 ALTER、不 DROP、不改删既有 K 线表。
-- 既有行情表（sources / candle_versions / candle_heads / candles 视图 / api_*）保持原样。

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at INTEGER NOT NULL
);

-- 策略配置：显式配置并版本化；未配置即“待配置”
CREATE TABLE IF NOT EXISTS strategy_configs (
    strategy_key TEXT NOT NULL,
    version      TEXT NOT NULL,
    config_json  TEXT NOT NULL,
    config_hash  TEXT NOT NULL,
    frozen       INTEGER NOT NULL DEFAULT 0 CHECK(frozen IN (0,1)),
    note         TEXT,
    created_at   INTEGER NOT NULL,
    PRIMARY KEY (strategy_key, version)
);

-- 信号日志（事件追加，不覆盖；信号 != 下单）
CREATE TABLE IF NOT EXISTS signals (
    id               INTEGER PRIMARY KEY,
    strategy_key     TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    config_hash      TEXT NOT NULL,
    exchange         TEXT NOT NULL,
    market           TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    timeframe        TEXT NOT NULL,
    status           TEXT NOT NULL,
    direction        TEXT,
    event_time       INTEGER,
    confirmed_at     INTEGER NOT NULL,
    data_version     TEXT,
    source_id        INTEGER,
    evidence_json    TEXT,
    created_at       INTEGER NOT NULL,
    missing_reason   TEXT,
    UNIQUE(strategy_key, config_hash, exchange, market, symbol, timeframe, confirmed_at)
);
CREATE INDEX IF NOT EXISTS signals_lookup ON signals(exchange, market, symbol, timeframe, confirmed_at);
CREATE INDEX IF NOT EXISTS signals_by_strategy ON signals(strategy_key, created_at);

-- 条件快照：实际值 vs 阈值，均由后端给出，前端不做比较推断
CREATE TABLE IF NOT EXISTS signal_conditions (
    id        INTEGER PRIMARY KEY,
    signal_id INTEGER NOT NULL REFERENCES signals(id),
    name      TEXT NOT NULL,
    actual    TEXT,
    threshold TEXT,
    unit      TEXT,
    satisfied INTEGER,
    note      TEXT
);
CREATE INDEX IF NOT EXISTS signal_conditions_by_signal ON signal_conditions(signal_id);

-- 持仓量历史（读取结构；本轮不采集，空表即返回缺失）
CREATE TABLE IF NOT EXISTS oi_history (
    exchange            TEXT NOT NULL,
    market              TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    period              TEXT NOT NULL,
    ts                  INTEGER NOT NULL,
    open_interest       TEXT,
    open_interest_value TEXT,
    oi_ccy              TEXT,
    oi_usd              TEXT,
    source              TEXT,
    fetched_at          INTEGER,
    PRIMARY KEY(exchange, market, symbol, period, ts)
);

-- 资金费历史（读取结构；本轮不采集，空表即返回缺失）
CREATE TABLE IF NOT EXISTS funding_history (
    exchange     TEXT NOT NULL,
    market       TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    funding_time INTEGER NOT NULL,
    funding_rate TEXT,
    mark_price   TEXT,
    rate_type    TEXT,
    source       TEXT,
    fetched_at   INTEGER,
    PRIMARY KEY(exchange, market, symbol, funding_time)
);
