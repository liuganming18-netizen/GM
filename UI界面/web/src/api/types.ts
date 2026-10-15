/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * 真实后端（MarketDataHubAdapter /api/v1）响应类型。
 * 仅描述实际存在的字段；后端未提供的字段一律不存在，不做推测。
 */

export interface EnvelopeTime {
  event_time: string | null;
  available_at: string | null;
  received_at: string | null;
  fetched_at: string | null;
}

export interface EnvelopeQuality {
  status: string | null;
  age_seconds: number | null;
  note: string | null;
}

export interface Envelope<T> {
  mode: string | null;
  account_id: string | null;
  run_id: string | null;
  strategy_version: string | null;
  rule_version: string | null;
  param_hash: string | null;
  data_version: string | null;
  as_of: string | null;
  quality: EnvelopeQuality | null;
  source_url: string | null;
  evidence_level: string | null;
  missing_reason: string | null;
  time: EnvelopeTime;
  items?: T[];
  has_more?: boolean;
  next_cursor?: string | null;
  total?: number | null;
}

/**
 * `/markets` 行内的 `coverage[]`。
 * 注意 `source.imported_at` 是**真实入库时间**（官方归档文件导入数据库的时刻），
 * 与 `latest`（K 线止点 = 交易事件时间）不是一回事，不可混用。
 */
export interface RawMarketCoverageRow {
  timeframe: string;
  market: string | null;
  rows: number | null;
  expected: number | null;
  missing_within_observed_range: number | null;
  earliest: string | null;
  latest: string | null;
  gaps: unknown[] | null;
  gaps_note: string | null;
  unit: string | null;
  source: {
    source_id: number | null;
    sha256: string | null;
    source_url: string | null;
    imported_at: string | null;
  } | null;
  available_at: string | null;
  available_at_known: boolean | null;
  evidence_level: string | null;
}

/** /api/v1/markets 与 /api/v1/markets/{exchange}/{market_id} 的行结构 */
export interface RawMarket {
  exchange: string;
  market_id: string;
  symbol: string | null;
  base: string | null;
  quote: string | null;
  contract_type: string | null;
  is_usdt_perp: number | null;
  spot_status: string | null;
  eligibility: string | null;
  eligibility_reason: string | null;
  market_cap_usd: number | null;
  market_cap_as_of: string | null;
  /**
   * 市值口径说明（后端如实标注来源）。
   * 例：「流通量取自币安公开 openInterestHist 的 CMCCirculatingSupply（同合约）」，
   * 或 OKX 行的「按**标的币 X** 复用币安同名合约的流通量（非本所来源）」。
   */
  market_cap_note: string | null;
  /** 流通量**官方原值**（以合约标的币为单位，如 1000PEPE 合约给的是 1000PEPE 计数）；无来源则 null */
  circulating_supply: number | null;
  liquidity_status: string | null;
  quote_volume_median_30d: number | null;
  available_timeframes: string[] | null;
  coverage: RawMarketCoverageRow[] | null;
  oi: {
    available: boolean | null;
    rows: number | null;
    latest_ts: number | null;
    open_interest: string | null;
    oi_ccy: string | null;
    oi_usd: number | null;
    source: string | null;
    note: string | null;
  } | null;
  funding: {
    available: boolean | null;
    latest_funding_time: number | null;
    funding_rate: string | null;
    rate_type: string | null;
    source: string | null;
    note: string | null;
  } | null;
  /**
   * 聪明钱槽位（后端 → 前端）。
   *
   * 2026-10-07 更正：此前 `available` 恒 `false`，理由是「币安公开 API 没有金额/均价/身份」。
   * 该判断只对 `/fapi/v1/futures/data/*` 那些**聚合比率**端点成立；币安另有**公开无鉴权**
   * 聪明钱接口（`/bapi/futures/v1/public/future/smart-money/signal/*`，实测 200），
   * 提供 多空各自的 当前仓位(USDT) / 平均开仓价 / 交易员人数 / 巨鲸人数。
   *
   * 契约：后端把该接口原始响应**原样透传**到 `overview` / `stats`，
   * 前端只做字段映射（`mapBinanceSmartMoney`），不重算、不推算。
   * 后端尚未采集到的标的：`available:false` 或 `overview`/`stats` 缺省 ⇒ 前端六槽保持 null。
   */
  smart_money: {
    available: boolean | null;
    reason: string | null;
    /** 来源标识，如 `binance-bapi-smart-money` */
    source?: string | null;
    /** 交易所合约标识（币安：symbol，如 `ARXUSDT`） */
    symbol?: string | null;
    /** `…/signal/overview` 原始 `data`（币安字段名，原样透传） */
    overview?: unknown | null;
    /** `…/signal/details/stats` 原始 `data`（币安字段名，原样透传） */
    stats?: unknown | null;
    /**
     * 来源 `overview.updateTime`（毫秒纪元）。
     * ⚠️ **不是新鲜度字段**：2026-10-07 实测间隔 73s 两次采样，全部业务字段（totalPositions /
     * longTraders / longWhales / 各均价 / 各 Positions）**均已变化**，而 `updateTime` 恒为
     * `1791353989563`（age ≈ 9.6h 且持续增长）。**不得用它判断数据是否新鲜**；
     * 真实新鲜度应取 `details/list[].lastTradeTime`（同刻实测 age ≈ 59s）或本地取得时刻。
     */
    update_time_ms?: number | null;
    /** 后端采集时刻（ISO 8601） */
    fetched_at?: string | null;
  } | null;
  summary: {
    timeframe: string | null;
    latest_close: number | null;
    change_ratio: number | null;
    quote_volume: number | null;
    event_time: string | null;
  } | null;
  data_time: string | null;
  data_missing_reason: string | null;
  last_price: number | null;
  price_change_ratio_24h: number | null;
  realtime_time: string | null;
  realtime_received_at: string | null;
  realtime_source: string | null;
  realtime_status: string | null;
  volume_24h_quote: number | null;
  volume_24h_base: number | null;
  volume_source: string | null;
  volume_24h_quote_note: string | null;
  volume_by_timeframe: Record<string, {
    quote_volume: number | null;
    base_volume: number | null;
    close: number | null;
    close_time: string | null;
  } | null> | null;
  volume_by_timeframe_note: string | null;
  mark_price: number | null;
  mark_time: string | null;
  mark_source: string | null;
  funding_rate: number | null;
  next_funding_time: string | null;
  funding_time: string | null;
  funding_source: string | null;
  open_interest_total: number | null;
  open_interest_unit: string | null;
  open_interest_usd: number | null;
  oi_time: string | null;
  oi_source: string | null;
  instrument_category: string | null;
  category_name: string | null;
  in_active_range: number | null;
  excluded_reason: string | null;
  list_section: string | null;
  signal_status: string | null;
  signal_direction: string | null;
  signal_ref: string | null;
  note: string | null;
}

/** /markets/{ex}/{mid}/candles → bars[] */
export interface RawBar {
  open_time: string;
  close_time: string | null;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  base_volume: number | null;
  contract_volume: number | null;
  quote_volume: number | null;
  trades: number | null;
  is_closed: boolean | null;
}

/** 后端 annotations（全部来自**真实已存信号**，无信号时为 null，不生成推测标注）。 */
export interface RawAnnotationMark {
  time: string;
  price: number | null;
  note: string | null;
  signal_id?: string | null;
  status?: string | null;
  strategy_key?: string | null;
  source_masks?: string[] | null;
  execution_label?: string | null;
}

export interface RawAnnotations {
  frozen_ranges: Array<{ start_time: string | null; end_time: string | null;
    upper: number | null; lower: number | null; bar_count: number | null;
    note: string | null; signal_id?: string | null }>;
  breakouts: RawAnnotationMark[];
  retests: RawAnnotationMark[];
  stops: RawAnnotationMark[];
  targets: RawAnnotationMark[];
  /** 信号确认标记（2026-10-11 新增）：用于把 signalTag 落到确认 bar 上 */
  signals?: RawAnnotationMark[];
}

export interface RawCandlesEnvelope extends Envelope<never> {
  exchange: string;
  market_id: string;
  timeframe: string;
  timezone: string | null;
  bars: RawBar[];
  forming_bar: RawBar | null;
  is_native: boolean | null;
  annotations: RawAnnotations | null;
}

/** /markets/{ex}/{mid}/oi → items[] */
export interface RawOiRow {
  event_time: string;
  period: string | null;
  open_interest: string | null;
  open_interest_value: number | null;
  oi_ccy: string | null;
  oi_usd: number | null;
  source: string | null;
  fetched_at: number | null;
}

/** /markets/{ex}/{mid}/funding → items[] */
export interface RawFundingRow {
  event_time: string;
  funding_rate: string | null;
  mark_price: string | null;
  rate_type: string | null;
  source: string | null;
  fetched_at: number | null;
}

/**
 * /markets/{ex}/{mid}/public-extras —— **币安公开**扩展字段。
 *
 * 来源：后端独立模块 `后端实现/public_market_read.py`（复用统一网关限频/缓存 +
 * 严格标的身份；只读、不落库）。**只有币安公开 API**；币安没有该代币才补 OKX。
 *
 * 纪律（与后端一致，前端不得改写语义）：
 *  - `long_short.*` 是**各自独立命名的聚合原始指标**（持仓量多空比 / 账户数多空比 /
 *    全局账户多空比 / taker 买卖量比），**不得**当作「聪明钱金额/均价/身份」。
 *  - `smart_money`：本模块**不**提供；但币安另有公开无鉴权聪明钱接口
 *    （`/bapi/futures/v1/public/future/smart-money/signal/*`，2026-10-07 实测 200），
 *    金额/均价/身份**确实存在**，取数走 `src/api/smartMoney.ts` 与 `/markets` 的
 *    `smart_money.overview|stats` 透传字段，**不从本模块拼凑**。
 */
export interface RawPublicExtras {
  available: boolean;
  exchange?: string | null;
  market_id?: string | null;
  base?: string | null;
  as_of?: string | null;
  is_supplement?: boolean | null;
  fields?: string[] | null;
  missing_reason?: string | null;
  open_interest?: {
    available: boolean;
    /** 全市场总持仓**数量**（标的币/张） */
    amount?: number | null;
    /** 全市场总持仓**名义额(USD)** —— 唯一公开来源 openInterestHist.sumOpenInterestValue */
    usd?: number | null;
    amount_event_time?: string | null;
    usd_event_time?: string | null;
    source?: string | null;
    note?: string | null;
    reason?: string | null;
  } | null;
  long_short?: {
    period?: string | null;
    top_position_long_short_ratio?: RawRatioBlock | null;
    top_account_long_short_ratio?: RawRatioBlock | null;
    global_account_long_short_ratio?: RawRatioBlock | null;
    taker_buy_sell_ratio?: RawRatioBlock | null;
    source?: string | null;
    note?: string | null;
  } | null;
  orderbook?: {
    available: boolean;
    unit?: string | null;
    bids?: unknown;
    asks?: unknown;
    best_bid?: unknown;
    best_ask?: unknown;
    event_time?: string | null;
    source?: string | null;
    note?: string | null;
    reason?: string | null;
  } | null;
  smart_money?: { available: boolean | null; reason?: string | null } | null;
  quality?: { status: string | null; age_seconds: number | null; note: string | null } | null;
  snapshot?: {
    refreshed_at?: string | null;
    refreshed_at_ms?: number | null;
    period?: string | null;
    written_by?: string | null;
    note?: string | null;
  } | null;
  source_urls?: string[] | null;
}

/** public-extras 里的单个比率块（原始字段为字符串，前端只做数值化，不改语义） */
export interface RawRatioBlock {
  available: boolean;
  period?: string | null;
  event_time?: string | null;
  fetched_at?: string | null;
  origin?: string | null;
  value?: string | number | null;
  long_account?: string | number | null;
  short_account?: string | number | null;
  buy_vol?: string | number | null;
  sell_vol?: string | number | null;
  reason?: string | null;
}

/** /signals → items[] */
export interface RawSignal {
  signal_id: string;
  exchange: string;
  market_id: string;
  symbol: string;
  status: string | null;
  direction: string | null;
  strength: number | null;
  created_at: string | null;
  expires_at: string | null;
  timeframe: string | null;
  // ── 事件身份与门禁（2026-10-11）：后端真实字段，原样携带 ──
  confirmed_at?: string | null;
  event_time?: string | null;
  strategy_key?: string | null;
  strategy_version?: string | null;
  config_hash?: string | null;
  data_version?: string | null;
  source_id?: number | null;
  source_masks?: string[] | null;
  execution_label?: string | null;
  actionable?: boolean | null;
  actionable_reason?: string | null;
  current_direction?: string | null;
  // ── 实时信号层扩展（2026-10-13 信号与持仓接通）────────────────────────────
  /**
   * 来源层：`live` = 实时增量生成；`historical_research` = 旧研究窗口导入的**历史**信号
   * （明确标注，**不冒充当前**）。缺失（库内 `signals` 表路径）为 undefined。
   */
  origin?: string | null;
  /** 展示态：实时层恒 `displayable`。与「可执行态」严格分列。 */
  display_state?: string | null;
  /**
   * 可执行态（后端判定，**不适用 G01–G05 正式回测门禁**）：
   * `enterable` / `display_only_same_instant` / `blocked_no_signal` /
   * `blocked_not_admitted` / `blocked_missing_daily_list` / `not_this_arm_structure`。
   */
  executable_state?: string | null;
  /** 结构类型：`spring`（3c）/ `sc`（3d）等 */
  structural_kind?: string | null;
  /** 事件证据 bar 的开/收盘时刻（ms 纪元） */
  bar_open_time?: number | null;
  bar_close_time?: number | null;
  /** 结构检测时的低点与 ATR（模拟开仓的止损基数） */
  detection_low?: number | null;
  detection_atr?: number | null;
  /** 准入状态（该币是否在已批准准入清单内） */
  admission?: string | null;
  /** 真实可得时刻（非归档假设） */
  available_at?: string | null;
  /** 所属启用族（3a/3b/3c/3d）——页面据此把开仓请求路由到对应纸面臂 */
  arm?: string | null;
  /**
   * 事件角色（**当前区判据之一**，2026-10-14 信号区清理）：
   * `enterable_arm_entry` = 冻结策略自己的真实臂入场事件；
   * `enterable_structure` / `display_only_same_instant` / `not_this_arm_structure`
   * 等为**结构证据提示**，不属于当前可交易信号。缺失 ⇒ 身份不可验证。
   */
  role?: string | null;
  // ── 同一买点多臂合卡（2026-10-14 09 契约 §4）──────────────────────────────
  /**
   * 命中**同一买点**（交易所/币种/周期/真实 bar/方向 相同）的各臂身份与去向。
   *
   * 后端只在**展示层**聚合，库内事实与执行幂等仍逐臂独立（不跨臂抹掉独立实验）。
   * 不同时间的新事件 bar 身份不同 ⇒ 不会被合并。
   */
  arms?: RawSignalArm[] | null;
  /** 该卡命中的臂数（= `arms.length`；后端给出，前端不重算） */
  arm_count?: number | null;
  /** 命中臂列表（如 ["3a","3c","3d"]），顺序与 `arms[]` 一致 */
  arms_hit?: string[] | null;
}

/** 合卡后卡上「某一臂」的身份与去向（字段原样来自后端，不推断）。 */
export interface RawSignalArm {
  arm?: string | null;
  signal_id?: string | null;
  status?: string | null;
  executable_state?: string | null;
  actionable?: boolean | null;
  actionable_reason?: string | null;
  structural_kind?: string | null;
  source_masks?: string[] | null;
  execution_label?: string | null;
}

/** /strategies → items[] */
export interface RawStrategy {
  strategy_key: string;
  name: string | null;
  timeframe: string | null;
  direction: string | null;
  status: string | null;
  config_status: string | null;
  missing_config: unknown;
  config_errors: unknown;
  required_config: unknown;
  config_version: string | null;
  config_hash: string | null;
  frozen: number | null;
  config: unknown;
  note: string | null;
}

/** /pool/status */
export interface RawDownloadTask {
  timeframe: string;
  status: string;
  n: number | null;
}

export interface RawRun {
  id: number;
  kind: string | null;
  phase: string | null;
  started_at: string | null;
  finished_at: string | null;
  status: string | null;
  note: string | null;
}

export interface RawPoolStatus extends Envelope<never> {
  pool_total: number | null;
  pool_all_total: number | null;
  pool_with_candles: number | null;
  pool_without_candles: number | null;
  covered_combos: number | null;
  candles: number | null;
  active_scope: {
    binance_coins?: number;
    binance_index?: number;
    okx_crypto_total?: number;
    okx_supplement?: number;
    okx_duplicated_excluded?: number;
    active_total?: number;
    index_note?: string;
  } | null;
  download_tasks: RawDownloadTask[] | null;
  recent_runs: RawRun[] | null;
}

/** /health → items[] */
export interface RawHealthItem {
  name: string;
  status: string | null;
  last_event_time: string | null;
  last_received_at: string | null;
  age_seconds: number | null;
  detail: string | null;
}

export type RawHealthEnvelope = Envelope<RawHealthItem>;

/** /data/status → coverage[] */
export interface RawDataStatusRow {
  exchange: string;
  market: string | null;
  symbol: string;
  timeframe: string;
  first_open: number | null;
  last_close: number | null;
  rows: number | null;
  expected: number | null;
  missing_within_observed_range: number | null;
}

export interface RawDataStatus extends Envelope<never> {
  database: string | null;
  source_files: number | null;
  coverage: RawDataStatusRow[] | null;
  external_calls: number | null;
  external_calls_ok: number | null;
  external_calls_failed: number | null;
  scope_state: { scope: string; blocked: number | null; reason: string | null }[] | null;
  note: string | null;
}

/** /data/import-plan */
export interface RawImportPlan extends Envelope<never> {
  plan: {
    order?: string[];
    retention?: Record<string, string>;
    native_skip?: { okx?: string[]; rule?: string };
    chart_default?: string;
    status?: string;
  } | null;
}

/** /strategies → items[] */
export interface RawStrategyRow {
  strategy_key: string;
  name: string | null;
  timeframe: string | null;
  direction: string | null;
  status: string | null;
  config_status: string | null;
  missing_config: unknown;
  config_errors: unknown;
  required_config: unknown;
  config_version: string | null;
  config_hash: string | null;
  frozen: boolean | null;
  config: unknown;
  note: string | null;
}

export type RawStrategiesEnvelope = Envelope<RawStrategyRow>;

/** /backtests、/reviews 等未接通模块：items 恒为空，quality.status = not_connected */
export interface RawStubList extends Envelope<never> {
  total: number | null;
}

// ── 模拟盘 / 实盘只读（2026-10-13 信号与持仓接通）─────────────────────────────

/** `/paper/positions` 行内的**未平仓持仓**（服务端账本口径）。 */
export interface RawPaperPositionRow {
  position_id: number;
  symbol: string;
  direction: 'long' | 'short';
  qty: number;
  entry_price: number;
  mark_price: number | null;
  mark_source: string | null;
  mark_time: number | null;
  unrealized_pnl: number | null;
  unrealized_pnl_pct: number | null;
  stop_price: number | null;
  atr_at_entry: number | null;
  entry_ts: number;
  signal_uid: string | null;
  leverage: number;
  notional: number;
  opened_by: string | null;
}

/** `/paper/positions` 行（在持仓字段上追加所属臂）。 */
export interface RawPaperPositionFlat extends RawPaperPositionRow {
  arm: string;
  account_id: string;
}

/**
 * `/paper/accounts` 的**未来待成交挂单**（因果修正 2026-10-13）：
 * 应成交时刻（`earliest_fill_ms`）尚未到达 ⇒ 挂单等真实进程时钟，**不提前、不回填旧价**。
 */
export interface RawPaperPending {
  pending_id: number;
  symbol: string;
  origin: string | null;
  signal_uid: string | null;
  event_time: number | null;
  available_at: number | null;
  requested_at: number;
  earliest_fill_ms: number;
  created_at: number;
  status: string;
  reason: string | null;
  note?: string | null;
}

/** `/paper/accounts` 单臂账户快照（服务端账本，四臂各 5000U 初始）。 */
export interface RawPaperAccount {  arm: string;
  account_id: string;
  mode: string;
  initial_equity: number;
  cash: number;
  equity: number | null;
  unrealized_pnl: number;
  used_margin: number;
  gross_notional: number;
  drawdown: number;
  drawdown_locked: boolean;
  peak_equity: number;
  realized_pnl_total: number;
  fees_paid_total: number;
  funding_paid_total: number;
  positions: RawPaperPositionRow[];
  /** 未来待成交挂单（服务端登记，真实时钟推进） */
  pending: RawPaperPending[];
  n_pending: number;
  n_orders: number;
  n_rejected: number;
  n_closed?: number;
  realized_history: Record<string, unknown>[];
  params_source: string | null;
  model_version: string;
  quote_freshness: unknown;
}

/** `/accounts` 单条**实盘只读**账户快照。 */
export interface RawLiveAccount {
  exchange: string;
  account_label: string;
  /** connected / connected_no_positions / not_configured / auth_failed / error / reader_unavailable / stale */
  status: string;
  raw_status: string;
  detail: string | null;
  error_class: string | null;
  equity_usd: number | null;
  available_usd: number | null;
  used_margin_usd: number | null;
  positions: Array<Record<string, unknown>>;
  n_positions: number;
  source: string | null;
  read_at: string | null;
  age_seconds: number | null;
  readonly: boolean;
  no_real_order: boolean;
}
