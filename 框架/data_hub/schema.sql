PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_versions (
    version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY, exchange TEXT NOT NULL, market TEXT NOT NULL,
    symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
    source_url TEXT NOT NULL, file_path TEXT NOT NULL, sha256 TEXT NOT NULL,
    imported_at INTEGER NOT NULL, downloaded_at INTEGER,
    first_open INTEGER NOT NULL, last_close INTEGER NOT NULL, row_count INTEGER NOT NULL,
    UNIQUE(exchange, market, symbol, timeframe, sha256)
);
CREATE TABLE IF NOT EXISTS candle_versions (
    exchange TEXT NOT NULL, market TEXT NOT NULL, symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL, open_time INTEGER NOT NULL, close_time INTEGER NOT NULL,
    open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL,
    volume TEXT NOT NULL, quote_volume TEXT NOT NULL, trade_count INTEGER NOT NULL,
    taker_buy_volume TEXT NOT NULL, taker_buy_quote_volume TEXT NOT NULL,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    available_at INTEGER, is_closed INTEGER NOT NULL CHECK(is_closed = 1),
    PRIMARY KEY(exchange, market, symbol, timeframe, open_time, source_id)
);
CREATE TABLE IF NOT EXISTS candle_heads (
    exchange TEXT NOT NULL, market TEXT NOT NULL, symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL, open_time INTEGER NOT NULL, source_id INTEGER NOT NULL,
    PRIMARY KEY(exchange, market, symbol, timeframe, open_time),
    FOREIGN KEY(exchange, market, symbol, timeframe, open_time, source_id)
      REFERENCES candle_versions(exchange, market, symbol, timeframe, open_time, source_id)
);
CREATE VIEW IF NOT EXISTS candles AS
SELECT v.* FROM candle_versions v JOIN candle_heads h
USING(exchange, market, symbol, timeframe, open_time, source_id);
CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY, request_key TEXT NOT NULL, endpoint TEXT NOT NULL,
    scope TEXT NOT NULL, params_json TEXT NOT NULL, started_at INTEGER NOT NULL,
    finished_at INTEGER, status TEXT NOT NULL, http_status INTEGER, business_code TEXT,
    response_json TEXT, response_sha256 TEXT, response_headers_json TEXT,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS api_calls_scope_time ON api_calls(scope, started_at);
CREATE TABLE IF NOT EXISTS api_cache (
    request_key TEXT PRIMARY KEY, call_id INTEGER NOT NULL REFERENCES api_calls(id),
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS api_leases (
    request_key TEXT PRIMARY KEY, owner TEXT NOT NULL, until_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS api_scope_state (
    scope TEXT PRIMARY KEY, cooldown_until INTEGER NOT NULL DEFAULT 0,
    blocked INTEGER NOT NULL DEFAULT 0, reason TEXT
);
