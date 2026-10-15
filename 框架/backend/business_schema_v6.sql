-- 字段缺失标注（可重复、非破坏增量迁移 v6）
-- 背景：`candle_versions` 的 `taker_buy_volume` / `taker_buy_quote_volume` 是
--       `TEXT NOT NULL`（币安归档每个文件都带这两列），而**欧易 K 线不提供 taker 字段**。
--       既不能改既有表的约束（会触碰 68 万行既有行情），也不能用别的字段冒充 taker。
--       因此：缺失值在 candle_versions 里存空串 ''（满足 NOT NULL），
--       同时在本表**显式标注**该字段确实缺失及其原因，供上层判断「B 策略数据不足」。
-- 原则：只 CREATE TABLE/INDEX IF NOT EXISTS；不 ALTER、不 DROP、不改删既有行情表。

CREATE TABLE IF NOT EXISTS candle_field_notes (
    exchange    TEXT NOT NULL,
    market      TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    timeframe   TEXT NOT NULL,
    field       TEXT NOT NULL,      -- 缺失字段名，如 taker_buy_volume
    stored_as   TEXT NOT NULL,      -- 实际落库表示（'' 空串 / NULL）
    reason      TEXT NOT NULL,      -- 缺失原因
    impact      TEXT,               -- 对下游的影响，如「B 策略数据不足」
    source      TEXT,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY(exchange, market, symbol, timeframe, field)
);
CREATE INDEX IF NOT EXISTS candle_field_notes_by_field
    ON candle_field_notes(exchange, field);
