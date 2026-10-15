-- 库存摘要缓存（可重复、非破坏增量迁移 v4）
-- 目的：全池入库后 candle_versions 规模达上亿行，`SELECT ... FROM candles GROUP BY ...`
--       每次 HTTP 请求都全表扫描不可接受。本表由批量下载器在每完成一个
--       (标的, 周期) 后增量维护，适配层优先读本表；本表为空时回退原全表统计。
-- 原则：只 CREATE TABLE/INDEX IF NOT EXISTS；不 ALTER、不 DROP、不改删既有行情表。

CREATE TABLE IF NOT EXISTS coverage_cache (
    exchange    TEXT NOT NULL,
    market      TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    timeframe   TEXT NOT NULL,
    rows        INTEGER NOT NULL,
    first_open  INTEGER,
    last_close  INTEGER,
    expected    INTEGER,
    missing     INTEGER,
    source_count INTEGER NOT NULL DEFAULT 0,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY(exchange, market, symbol, timeframe)
);
CREATE INDEX IF NOT EXISTS coverage_cache_by_symbol ON coverage_cache(exchange, symbol, timeframe);
