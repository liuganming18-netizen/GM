-- 下载任务状态（可重复、非破坏增量迁移 v3）
-- 原则：只 CREATE TABLE/INDEX IF NOT EXISTS；不 ALTER、不 DROP、不改删既有行情表。
-- 与批量下载器 batch_download.py 配套：进度既写批量 manifest，也写本表。

CREATE TABLE IF NOT EXISTS download_runs (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,          -- pool_snapshot / batch / verify
    phase       TEXT,                   -- 周期，如 1d；整体运行为 NULL
    started_at  INTEGER NOT NULL,
    finished_at INTEGER,
    status      TEXT NOT NULL,          -- running / done / paused_no_space / refused / failed
    note        TEXT,
    meta_json   TEXT
);

-- 每个 (标的, 周期) 一行汇总；细到文件级的进度在 batch_state/<tf>/<SYMBOL>.json
CREATE TABLE IF NOT EXISTS download_tasks (
    exchange       TEXT NOT NULL,
    market         TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    timeframe      TEXT NOT NULL,
    status         TEXT NOT NULL,       -- done / missing_only / partial / failed / skipped_no_window
    planned_files  INTEGER NOT NULL DEFAULT 0,
    imported_files INTEGER NOT NULL DEFAULT 0,
    cached_files   INTEGER NOT NULL DEFAULT 0,
    missing_files  INTEGER NOT NULL DEFAULT 0,
    failed_files   INTEGER NOT NULL DEFAULT 0,
    rows           INTEGER NOT NULL DEFAULT 0,
    first_open     INTEGER,
    last_close     INTEGER,
    gaps           INTEGER,
    started_at     INTEGER,
    finished_at    INTEGER,
    note           TEXT,
    updated_at     INTEGER NOT NULL,
    PRIMARY KEY(exchange, market, symbol, timeframe)
);
CREATE INDEX IF NOT EXISTS download_tasks_status ON download_tasks(status, timeframe);

-- 当前交易中合约快照（每周期刷新一次；只代表 snapshot_utc 时刻）
CREATE TABLE IF NOT EXISTS symbol_pool (
    exchange          TEXT NOT NULL,
    market            TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    contract_type     TEXT,
    quote_asset       TEXT,
    margin_asset      TEXT,
    status            TEXT,
    onboard_time      INTEGER,
    base_asset        TEXT,
    spot_symbol       TEXT,
    spot_status       TEXT,
    spot_match_method TEXT,
    priority_hint     TEXT,
    snapshot_utc      INTEGER NOT NULL,
    source_call_id    INTEGER,
    updated_at        INTEGER NOT NULL,
    PRIMARY KEY(exchange, market, symbol)
);
