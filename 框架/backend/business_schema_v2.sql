-- 业务表迁移 v2：扩展信号唯一键。
--
-- 背景：v1 的唯一键为 (strategy_key, config_hash, exchange, market, symbol, timeframe, confirmed_at)，
--       不含 strategy_version 与 data_version。结果是：同一参数但不同策略版本或不同数据版本
--       产生的同一 confirmed_at 事件会被 INSERT OR IGNORE 静默吞掉。
-- 处置：以新唯一键重建 signals / signal_conditions；既有行原样保留（INSERT ... SELECT）。
-- 纪律：不改、不删任何既有 K 线表；两表当前为空，重建无数据损失；本脚本只执行一次
--       （由 schema_migrations 记录，见 migrate.py）。

CREATE TABLE IF NOT EXISTS signals_v2 (
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
    UNIQUE(strategy_key, strategy_version, config_hash, exchange, market, symbol,
           timeframe, confirmed_at, data_version)
);

CREATE TABLE IF NOT EXISTS signal_conditions_v2 (
    id        INTEGER PRIMARY KEY,
    signal_id INTEGER NOT NULL,
    name      TEXT NOT NULL,
    actual    TEXT,
    threshold TEXT,
    unit      TEXT,
    satisfied INTEGER,
    note      TEXT
);

INSERT OR IGNORE INTO signals_v2
    (id, strategy_key, strategy_version, config_hash, exchange, market, symbol, timeframe,
     status, direction, event_time, confirmed_at, data_version, source_id, evidence_json,
     created_at, missing_reason)
SELECT id, strategy_key, strategy_version, config_hash, exchange, market, symbol, timeframe,
       status, direction, event_time, confirmed_at, data_version, source_id, evidence_json,
       created_at, missing_reason
FROM signals;

INSERT OR IGNORE INTO signal_conditions_v2
    (id, signal_id, name, actual, threshold, unit, satisfied, note)
SELECT id, signal_id, name, actual, threshold, unit, satisfied, note
FROM signal_conditions;

DROP TABLE IF EXISTS signal_conditions;
DROP TABLE IF EXISTS signals;
ALTER TABLE signals_v2 RENAME TO signals;
ALTER TABLE signal_conditions_v2 RENAME TO signal_conditions;

CREATE INDEX IF NOT EXISTS signals_lookup
    ON signals(exchange, market, symbol, timeframe, confirmed_at);
CREATE INDEX IF NOT EXISTS signals_by_strategy
    ON signals(strategy_key, created_at);
CREATE INDEX IF NOT EXISTS signal_conditions_by_signal
    ON signal_conditions(signal_id);
