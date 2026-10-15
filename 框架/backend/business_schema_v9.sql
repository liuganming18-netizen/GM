-- v9 symbol_supply：**流通量来源表**（用于接通「流动市值」列）
--
-- 为什么需要它：
--   · 原版界面的第 2 列「流动市值」一直显示 `—`，交接状态记为「无真实来源」；
--   · 交易所**不提供**流通量/市值；第三方 CoinGecko / CMC 的历史点时值需要付费 Key
--     （项目口径：不申请 Key、不付费、不用现市值回填历史）；
--   · 但币安**公开**端点 `/futures/data/openInterestHist` 的**同一行**里就带
--     `CMCCirculatingSupply`（CMC 流通量，**以合约标的币为单位**），
--     而采集器的 `oi_sweep` **本来就在逐币调用这个端点**（每轮 528 次，仅取 limit=1）。
--     因此这份数据是「**已经付过请求成本、只是此前被丢弃**」的真实官方字段。
--
-- 口径纪律（必须逐条遵守）：
--   · 只存**官方返回的原始值**，不做任何换算、不猜单位；单位由 `unit_note` 显式写明；
--   · 市值由**读取侧**现算：`market_cap_usd = 最新价 × circulating_supply`，
--     分子分母同为**合约标的币单位**；价格取不到或流通量取不到 ⇒ 该行市值留空（不补值）；
--   · 这是**当前值**（不是点时历史值），`market_cap_as_of` 必须同时带上
--     价格时点与供应量时点中**较晚**的那个，绝不把「快照何时写」当成「指标何时发生」；
--   · OKX 公开端点**无**流通量字段；同名标的币（如 BTC）可复用币安同名标的的流通量，
--     由读取侧按 `base` 回退匹配，并在响应里以 `supply_source` 如实标注来源。
--
-- 主键 = (exchange, market, symbol)：每个合约**只有一行**（当前流通量），UPSERT 覆盖。
CREATE TABLE IF NOT EXISTS symbol_supply (
    exchange            TEXT    NOT NULL,
    market              TEXT    NOT NULL,
    symbol              TEXT    NOT NULL,
    base                TEXT,              -- 合约标的币（如 BTC / 1000PEPE），供跨所按币回退匹配
    circulating_supply  TEXT,              -- 官方原值（字符串保精度），单位见 unit_note
    unit_note           TEXT,              -- 单位说明（写明「以合约标的币为单位」）
    source              TEXT,              -- 例如 binance.fapi.openInterestHist
    supply_event_time   INTEGER,           -- 该行在交易所侧的 5m 桶时间（毫秒）
    fetched_at          INTEGER,           -- 本机取到该行的时间
    updated_at          INTEGER NOT NULL,
    PRIMARY KEY(exchange, market, symbol)
);

-- 按标的币回退匹配（OKX → 币安同名币）用
CREATE INDEX IF NOT EXISTS symbol_supply_base ON symbol_supply(base);
