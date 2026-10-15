-- v8 realtime_klines：**未收盘 K 线临时层**（只给图表用，绝不混入已收盘历史）
--
-- 为什么必须独立成表：
--   · 已收盘 K 线表 candle_versions 有 `CHECK(is_closed = 1)`，语义上只放已收盘 bar；
--     未收盘 bar 的 high/low/close 还会继续变，写进去会污染历史与回测输入。
--   · 图表需要"当前正在走的那根"，因此另立临时层：一行 = 某 (exchange,market,symbol,
--     timeframe) 的**当前未收盘 bar**，随 WS 消息原地更新（不是追加）。
--
-- 主键 = (exchange,market,symbol,timeframe)：每个合约每周期**只有一行**（当前那根）。
--   · 新周期开始时，同一主键被 UPSERT 覆盖为新的 open_time（旧的已收盘 bar 走 candle_*）。
--   · bar 收盘时本行被删除（收盘值写入 candle_versions + candle_heads）。
--
-- 时点三列（与 realtime_quotes 同一纪律，不能只更新时间戳）：
--   · event_time  = 交易所消息里的 K 线事件时间（币安 k.t / 欧易 ts）
--   · received_at = 本机 WS 收到该帧的时间
--   · updated_at  = 本行落库时间
-- 来源两列：source（binance.fstream / okx.ws）+ transport（ws / rest）。
CREATE TABLE IF NOT EXISTS realtime_klines (
    exchange               TEXT    NOT NULL,
    market                 TEXT    NOT NULL,
    symbol                 TEXT    NOT NULL,
    timeframe              TEXT    NOT NULL,
    open_time              INTEGER NOT NULL,
    close_time             INTEGER,
    open                   TEXT,
    high                   TEXT,
    low                    TEXT,
    close                  TEXT,
    volume                 TEXT,
    quote_volume           TEXT,
    trade_count            INTEGER,
    taker_buy_volume       TEXT,
    taker_buy_quote_volume TEXT,
    is_closed              INTEGER NOT NULL DEFAULT 0,
    event_time             INTEGER,
    received_at            INTEGER,
    source                 TEXT,
    transport              TEXT,
    updated_at             INTEGER NOT NULL,
    PRIMARY KEY(exchange, market, symbol, timeframe)
);

CREATE INDEX IF NOT EXISTS realtime_klines_lookup
    ON realtime_klines(exchange, symbol, timeframe);

-- v8 kline_stream_state：**每 (交易所,周期) 一条**的实时 K 线链路状态。
-- 用途（直接对应验收要求「每周期列出采集覆盖/缺口/最后闭合和未收盘时点及来源」）：
--   不靠 running 记录冒充健康，而是如实记录真实消息时点、闭合入库计数、断线次数、
--   退避秒数与补缺结果。由采集器每条 K 线消息/每次重连后更新。
CREATE TABLE IF NOT EXISTS kline_stream_state (
    exchange              TEXT    NOT NULL,
    market                TEXT    NOT NULL,
    timeframe             TEXT    NOT NULL,
    symbols_planned       INTEGER,
    symbols_seen          INTEGER,
    last_msg_at           INTEGER,   -- 最近一条真实 WS 消息的本机接收时间
    last_closed_open_time INTEGER,   -- 最近一根**已收盘** bar 的 open_time
    last_closed_written_at INTEGER,  -- 该闭合 bar 的入库时间
    last_forming_open_time INTEGER,  -- 当前未收盘 bar 的 open_time
    closed_written        INTEGER,   -- 累计闭合入库根数
    gapfill_calls         INTEGER,   -- 断线后经统一网关补缺的 REST 调用数
    gapfill_filled        INTEGER,   -- 补缺成功写入的根数
    gapfill_skipped       INTEGER,   -- 因预算上限被跳过的 (标的,周期) 数（诚实留证）
    reconnects            INTEGER,
    backoff_s             INTEGER,   -- 当前/最近一次退避秒数
    status                TEXT,      -- ok / no_messages / reconnect_backoff / native_absent
    detail                TEXT,
    updated_at            INTEGER NOT NULL,
    PRIMARY KEY(exchange, market, timeframe)
);
