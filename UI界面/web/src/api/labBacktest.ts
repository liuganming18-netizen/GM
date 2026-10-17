/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * **单策略研究回测工作台**（`WX_OI_RESONANCE_V1`）—— 真实 API 客户端。
 *
 * 契约来源：`框架/backend/backtest_api.py`（`contract_version = bt_api_v1`），
 * 挂在**现有** adapter HTTPServer（8787）上；本文件只读契约、不重算任何交易/止损。
 *
 * 端点
 * ────
 *   GET  /backtests                        实验列表 + registry_entry
 *   POST /backtests                        创建（202 + run_id，后台线程执行）
 *   GET  /backtests/{id}                   状态 + config.spec / config.config
 *   GET  /backtests/{id}/metrics           metrics / benchmark_buy_hold / monthly_returns
 *   GET  /backtests/{id}/equity            items（策略线）+ benchmark_equity
 *   GET  /backtests/{id}/trades            逐笔
 *   GET  /backtests/{id}/trades/{trade_id} 单笔（trade/orders/funding/original_signal）
 *   GET  /backtests/{id}/orders            全局订单/成交明细
 *   GET  /backtests/{id}/rejected          拒单明细
 *   GET  /backtests/{id}/chart             chart.candles / signals / trades / benchmark
 *   GET  /backtests/{id}/quality           data_quality / hashes / window
 *
 * 纪律
 * ────
 * · 字段一律按**实际响应**声明；缺失保持 `null`，不用 0 / 默认值顶替。
 * · 参数白名单在下方镜像（来源：候选包 `config.py::SIGNAL_VALIDATION / EXEC_VALIDATION`），
 *   **只用于前端即时提示**；权威校验在后台 POST（400 + `errors[]`，原样显示）。
 */

import { ApiError, apiGet, apiPost } from './client';

export const LAB_STRATEGY_ID = 'WX_EVENT_TREND_V0_1';
export const LAB_CONTRACT_VERSION = 'bt_api_v1';

/** 研究回测信封（**不含**旧 Envelope 的 account_id / rule_version / data_version 等）。 */
export interface LabEnvelope<T = never> {
  mode: string | null;
  contract_version: string | null;
  strategy_id: string | null;
  run_id: string | null;
  status: string | null;
  quality: { status: string | null; age_seconds: number | null; note: string | null } | null;
  missing_reason: string | null;
  items?: T[];
  total?: number | null;
  live_order_enabled?: boolean;
  note?: string | null;
  failure_reason?: string | null;
  errors?: string[] | null;
}

/* ══════════════════════════ 白名单（镜像后台；后台为唯一权威）══════════════════════════ */

export type LabParamScope = 'signal' | 'exec';
/** 界面单位：决定控件标注与「显示值 ↔ 线上值」换算。 */
export type LabParamUnit = 'usd' | 'decimal' | 'percent' | 'bps' | 'count' | 'multiple';
export type LabParamGroup = 'exec_cost' | 'exec_risk' | 'sig_structure' | 'sig_oi';

export interface LabParamSpec {
  key: string;
  scope: LabParamScope;
  type: 'int' | 'float';
  min: number | null;
  max: number | null;
  /** 界面中文名 */
  label: string;
  unit: LabParamUnit;
  group: LabParamGroup;
  /** 首屏展开的常用项 */
  primary?: boolean;
  /** 口径提示（为什么是这个单位 / 特殊语义） */
  hint?: string;
}

export const LAB_PARAM_SPECS: LabParamSpec[] = [
  // ── 执行：成本 ────────────────────────────────────────────────────────────
  { key: 'initial_equity', scope: 'exec', type: 'float', min: 0, max: null, label: '初始资金', unit: 'usd', group: 'exec_cost', primary: true },
  { key: 'fee_rate', scope: 'exec', type: 'float', min: 0, max: 1, label: '基准手续费率', unit: 'decimal', group: 'exec_cost', primary: false, hint: '小数；0.0005 = 5 bps = 0.05%' },
  { key: 'maker_fee_rate', scope: 'exec', type: 'float', min: 0, max: 1, label: 'Maker 挂单费率', unit: 'decimal', group: 'exec_cost', primary: true, hint: '币安 VIP 0: 0.0002 = 万2' },
  { key: 'taker_fee_rate', scope: 'exec', type: 'float', min: 0, max: 1, label: 'Taker 吃单费率', unit: 'decimal', group: 'exec_cost', primary: true, hint: '币安 VIP 0: 0.0005 = 万5' },
  { key: 'slippage_bps', scope: 'exec', type: 'float', min: 0, max: null, label: '撮合滑点', unit: 'bps', group: 'exec_cost', primary: true, hint: 'bps；3 = 0.03% (万3)' },
  // ── 执行：风险 / 仓位 ─────────────────────────────────────────────────────
  { key: 'risk_per_trade', scope: 'exec', type: 'float', min: 0, max: 1, label: '单笔最大风险', unit: 'percent', group: 'exec_risk', primary: true, hint: '规范推荐: 0.5%~1.0% (默认 0.8%)' },
  { key: 'max_concurrent_trades', scope: 'exec', type: 'int', min: 1, max: 20, label: '最大并发持仓', unit: 'count', group: 'exec_risk', primary: true, hint: '规范推荐: 3~5 笔 (默认 3 笔)' },
  { key: 'max_portfolio_margin_pct', scope: 'exec', type: 'float', min: 0, max: 1, label: '总保证金占用上限', unit: 'percent', group: 'exec_risk', primary: true, hint: '规范推荐: 30%~50% (默认 40%)' },
  { key: 'max_portfolio_risk', scope: 'exec', type: 'float', min: 0, max: 1, label: '组合总风险上限', unit: 'percent', group: 'exec_risk', primary: true, hint: '百分数；4% ⇒ 线上 0.04' },
  { key: 'max_leverage_per_symbol', scope: 'exec', type: 'float', min: 0, max: null, label: '单标的杠杆上限', unit: 'multiple', group: 'exec_risk', hint: '倍 (默认 2.0 倍)' },
  // ── 信号：结构 ────────────────────────────────────────────────────────────
  { key: 'donchian_entry_window', scope: 'signal', type: 'int', min: 1, max: null, label: '唐奇安入场上轨', unit: 'count', group: 'sig_structure', primary: true, hint: '根（1h 信号 bar）' },
  { key: 'donchian_exit_window', scope: 'signal', type: 'int', min: 1, max: null, label: '唐奇安离场上轨', unit: 'count', group: 'sig_structure', primary: true, hint: '不得大于入场上轨' },
  { key: 'atr_period', scope: 'signal', type: 'int', min: 2, max: null, label: 'ATR 周期', unit: 'count', group: 'sig_structure', primary: true },
  { key: 'atr_mult', scope: 'signal', type: 'float', min: 0, max: null, label: 'ATR 止损倍数', unit: 'decimal', group: 'sig_structure', primary: true, hint: '小数倍（3.0 = 3×ATR）' },
  { key: 'structural_buffer_atr', scope: 'signal', type: 'float', min: 0, max: null, label: '结构缓冲', unit: 'decimal', group: 'sig_structure', hint: '×ATR' },
  { key: 'pullback_window', scope: 'signal', type: 'int', min: 1, max: null, label: '回踩窗口', unit: 'count', group: 'sig_structure' },
  { key: 'pullback_proximity_atr', scope: 'signal', type: 'float', min: 0, max: null, label: '回踩接近度', unit: 'decimal', group: 'sig_structure', hint: '×ATR' },
  { key: 'sc_vol_mult', scope: 'signal', type: 'float', min: 0, max: null, label: 'SC 量能倍数', unit: 'decimal', group: 'sig_structure' },
  { key: 'sc_range_atr_mult', scope: 'signal', type: 'float', min: 0, max: null, label: 'SC 区间幅度', unit: 'decimal', group: 'sig_structure', hint: '×ATR' },
  { key: 'sc_lower_wick_frac', scope: 'signal', type: 'float', min: 0, max: 1, label: 'SC 下影占比', unit: 'decimal', group: 'sig_structure' },
  { key: 'price_step', scope: 'signal', type: 'float', min: 0, max: null, label: '价格步长', unit: 'decimal', group: 'sig_structure' },
  // ── 信号：OI 因子 ─────────────────────────────────────────────────────────
  { key: 'oi_min_resonance', scope: 'signal', type: 'float', min: 0, max: 1, label: 'OI 共振门槛', unit: 'decimal', group: 'sig_oi', primary: true, hint: '默认 0 = 因子门槛**未启用**（因子只作证据记录，不拒单）' },
  { key: 'bars_per_day', scope: 'signal', type: 'int', min: 1, max: null, label: '每日信号 bar 数', unit: 'count', group: 'sig_oi' },
  { key: 'signal_bar_seconds', scope: 'signal', type: 'int', min: 1, max: null, label: '信号 bar 秒数', unit: 'count', group: 'sig_oi' },
  { key: 'oi_chg_window', scope: 'signal', type: 'int', min: 1, max: null, label: 'OI 变化窗口', unit: 'count', group: 'sig_oi' },
  { key: 'oi_rank_days', scope: 'signal', type: 'int', min: 1, max: null, label: 'OI 分位回看天数', unit: 'count', group: 'sig_oi' },
  { key: 'oi_rank_min_days', scope: 'signal', type: 'int', min: 1, max: null, label: 'OI 分位最少天数', unit: 'count', group: 'sig_oi', hint: '不得大于回看天数' },
  { key: 'oi_price_div_window', scope: 'signal', type: 'int', min: 1, max: null, label: 'OI/价格背离窗口', unit: 'count', group: 'sig_oi' },
  { key: 'oi_mom_flux_window', scope: 'signal', type: 'int', min: 1, max: null, label: 'OI 动量通量窗口', unit: 'count', group: 'sig_oi' },
  { key: 'oi_vol_ma_window', scope: 'signal', type: 'int', min: 1, max: null, label: 'OI 量能均线窗口', unit: 'count', group: 'sig_oi' },
  { key: 'oi_vol_ratio_clip', scope: 'signal', type: 'float', min: 0, max: null, label: 'OI 量比截断', unit: 'decimal', group: 'sig_oi' },
];

export const LAB_PARAM_BY_KEY: Record<string, LabParamSpec> = Object.fromEntries(
  LAB_PARAM_SPECS.map((s) => [s.key, s]),
);

export const LAB_UNIT_LABEL: Record<LabParamUnit, string> = {
  usd: 'USD',
  decimal: '小数',
  percent: '百分数(%)',
  bps: 'bps',
  count: '根',
  multiple: '倍',
};

/** 界面显示值 → 线上值（percent 输入按 % 存，线上为小数）。 */
export function labToWire(spec: LabParamSpec, shown: number): number {
  if (spec.unit === 'percent') return shown / 100;
  if (spec.type === 'int') return Math.trunc(shown);
  return shown;
}

/** 线上值 → 界面显示值。 */
export function labFromWire(spec: LabParamSpec, wire: number): number {
  if (spec.unit === 'percent') return wire * 100;
  return wire;
}

/** 白名单校验（**前端即时提示**；后台 POST 仍会独立校验并以 400 + errors 返回）。 */
export function labValidateParams(values: Record<string, number>): string[] {
  const errs: string[] = [];
  for (const spec of LAB_PARAM_SPECS) {
    const v = values[spec.key];
    if (v === undefined || v === null) continue;
    if (!Number.isFinite(v)) {
      errs.push(`${spec.label}（${spec.key}）必须为有限数值`);
      continue;
    }
    if (spec.type === 'int' && !Number.isInteger(labToWire(spec, v))) {
      errs.push(`${spec.label}（${spec.key}）必须为整数`);
      continue;
    }
    const wire = labToWire(spec, v);
    if (spec.min !== null && wire < spec.min) errs.push(`${spec.label}（${spec.key}）必须 >= ${spec.min}`);
    if (spec.max !== null && wire > spec.max) errs.push(`${spec.label}（${spec.key}）必须 <= ${spec.max}`);
  }
  const entry = values['donchian_entry_window'];
  const exit = values['donchian_exit_window'];
  if (Number.isFinite(entry) && Number.isFinite(exit) && exit > entry) {
    errs.push('唐奇安离场上轨不得大于入场上轨（donchian_exit_window <= donchian_entry_window）');
  }
  const rankDays = values['oi_rank_days'];
  const rankMin = values['oi_rank_min_days'];
  if (Number.isFinite(rankDays) && Number.isFinite(rankMin) && rankMin > rankDays) {
    errs.push('OI 分位最少天数不得大于回看天数（oi_rank_min_days <= oi_rank_days）');
  }
  return errs;
}

/* ── 周期 / 交易所 / 市场：仅确认项可选，其余**禁用并解释**（不假装能选）── */

export interface LabOption {
  value: string;
  label: string;
  enabled: boolean;
  /** 禁用原因（enabled=false 时必填） */
  reason?: string;
}

export const LAB_EXCHANGE_OPTIONS: LabOption[] = [
  { value: 'binance', label: 'binance', enabled: true },
  { value: 'okx', label: 'okx', enabled: false, reason: '后台白名单仅确认 binance（ALLOWED_EXCHANGE=("binance",)）；okx 未验证 ⇒ 禁用' },
];

export const LAB_MARKET_OPTIONS: LabOption[] = [
  { value: 'um', label: 'um（USDT 本位永续）', enabled: true },
  { value: 'coin', label: 'coin（币本位）', enabled: false, reason: '后台白名单仅确认 um（ALLOWED_MARKET=("um",)）；coin 未验证 ⇒ 禁用' },
];

export const LAB_SIGNAL_TF_OPTIONS: LabOption[] = [
  { value: '1h', label: '1h', enabled: true },
  { value: '5m', label: '5m', enabled: false, reason: '信号周期仅确认 1h（ALLOWED_SIGNAL_TF）；5m 未验证 ⇒ 禁用' },
  { value: '4h', label: '4h', enabled: false, reason: '信号周期仅确认 1h；4h 未验证 ⇒ 禁用' },
  { value: '1d', label: '1d', enabled: false, reason: '信号周期仅确认 1h；1d 未验证 ⇒ 禁用' },
];

export const LAB_EXEC_TF_OPTIONS: LabOption[] = [
  { value: '15m', label: '15m', enabled: true },
  { value: '5m', label: '5m', enabled: false, reason: '执行周期仅确认 15m（ALLOWED_EXEC_TF）；5m 未验证 ⇒ 禁用' },
  { value: '1h', label: '1h', enabled: false, reason: '执行周期仅确认 15m；1h 未验证 ⇒ 禁用' },
];

/** 窗口端点须对齐 15m 网格（后台 `WINDOW_GRID_MS`），最短 1 天。 */
export const LAB_WINDOW_GRID_MS = 900_000;
export const LAB_MIN_WINDOW_MS = 86_400_000;

/* ══════════════════════════ 响应类型（按实际响应声明）══════════════════════════ */

export interface LabWindow {
  symbol: string;
  exchange: string;
  market: string;
  signal_timeframe: string;
  exec_timeframe: string;
  window_source: string;
  data_end_ms: number;
  sig_end_ms: number;
  exec_end_ms: number;
  signal_series_start_ms: number;
  win_start_ms: number;
  win_end_ms: number;
  window_days: number;
  warmup_bars_requested: number | null;
  warmup_bars: number | null;
  trade_start_sec: number;
  convention: string;
}

export interface LabRunSummary {
  run_id: string;
  status: string;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  strategy_id: string | null;
  /** 归属：`current_strategy` = 当前策略；其它值 ⇒ 身份存疑（列表已排除） */
  strategy_key?: string | null;
  attribution?: string | null;
  symbols: string[] | null;
  window: LabWindow | null;
  net_return_pct: number | null;
  trades_count: number | null;
  max_drawdown_pct: number | null;
  failure_reason: string | null;
}

export interface LabSpec {
  strategy_id: string;
  exchange: string;
  market: string;
  symbols: string[];
  signal_timeframe: string;
  exec_timeframe: string;
  window: { mode: string; start_ms?: number; end_ms?: number; span_days?: number; window_days?: number | null };
  warmup_bars: number | null;
  params: Record<string, number>;
  signal_overrides: Record<string, number>;
  exec_overrides: Record<string, number>;
  note: string | null;
}

export interface LabRunDetail extends LabEnvelope {
  config: {
    spec: LabSpec | null;
    config: {
      signal_params?: Record<string, number> | null;
      exec_params?: Record<string, number> | null;
      signal_params_hash?: string | null;
      exec_params_hash?: string | null;
    } | null;
    actual_config?: Record<string, unknown> | null;
  } | null;
  hashes: Record<string, string> | null;
  window: LabWindow | null;
  engine: Record<string, unknown> | null;
  ledger_counts: Record<string, number | string> | null;
  chart_file: string | null;
  snapshot_files: string[] | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface LabMetrics {
  initial_equity: number | null;
  final_equity: number | null;
  net_pnl: number | null;
  net_return_pct: number | null;
  gross_pnl: number | null;
  total_entry_fee: number | null;
  total_exit_fee: number | null;
  total_fees: number | null;
  total_funding_fee: number | null;
  trades_count: number | null;
  wins: number | null;
  losses: number | null;
  flats: number | null;
  win_rate_pct: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  avg_win_loss_ratio: number | null;
  profit_factor: number | null;
  expectancy_usd: number | null;
  max_drawdown_pct: number | null;
  sharpe_annualized_sqrt365: number | null;
  sharpe_basis: string | null;
  daily_points: number | null;
  open_positions_remaining: unknown[] | null;
  note: string | null;
  funding_events_total: number | null;
  metrics_from_ledger?: Record<string, unknown> | null;
  metrics_match?: boolean | null;
  metrics_match_diffs?: string[] | null;
}

export interface LabBenchmark {
  benchmark_kind: string;
  symbol: string;
  entry_time_ms: number;
  exit_time_ms: number;
  entry_price: number;
  exit_price: number;
  qty: number;
  cum_funding: number;
  gross_return_pct: number;
  net_return_pct: number;
  final_equity: number;
  series_last_equity: number;
  max_drawdown_pct: number;
  price_only_return_pct: number;
  cost_model: string;
  note: string;
  equity_series?: LabBenchmarkPoint[];
}

export interface LabBenchmarkPoint {
  t: number;
  equity: number;
  cum_funding: number;
  mark: number;
  is_exit?: boolean;
  exit_fill?: number;
}

export interface LabMonthRow {
  month: string;
  month_end_equity: number | null;
  basis_equity: number | null;
  return_pct: number | null;
  partial_month: boolean;
  basis_note: string | null;
}

export interface LabMonthlyReturns {
  months: LabMonthRow[];
  initial_equity: number | null;
  note: string | null;
}

export interface LabMetricsResponse extends LabEnvelope {
  metrics: LabMetrics | null;
  benchmark_buy_hold: LabBenchmark | null;
  monthly_returns: LabMonthlyReturns | null;
  formal_m4_gate: Record<string, unknown> | null;
  m4_disclaimer: string | null;
}

export interface LabEquityPoint {
  run_id: string;
  ts_ms: number;
  cash_equity: number;
  unrealized_pnl: number;
  total_equity: number;
  open_positions_count: number;
  open_risk_pct: number;
}

export interface LabEquityResponse extends LabEnvelope<LabEquityPoint> {
  benchmark: LabBenchmark | null;
  benchmark_equity: LabBenchmarkPoint[] | null;
}

export interface LabTradeRow {
  run_id: string;
  trade_id: string;
  signal_id: string | null;
  symbol: string;
  direction: string;
  signal_type: number | null;
  entry_time_ms: number;
  exit_time_ms: number;
  entry_price: number;
  exit_price: number;
  qty: number;
  gross_pnl: number;
  entry_fee: number;
  exit_fee: number;
  fees: number;
  funding_fee: number;
  net_pnl: number;
  exit_reason: string;
  mfe: number | null;
  mae: number | null;
  holding_hours: number | null;
  funding_events: number | null;
  funding_gaps_json: string | null;
  stop_trace_json: string | null;
}

export interface LabTradesResponse extends LabEnvelope<LabTradeRow> {}

export interface LabOrdersResponse extends LabEnvelope<LabOrderRow> {}

/** 拒单明细行（账本 `rejected` 表：时点 / 标的 / 相位 / 原因）。 */
export interface LabRejectedRow {
  run_id: string;
  ts_ms: number;
  symbol: string;
  phase: string;
  reason: string;
}

export interface LabRejectedResponse extends LabEnvelope<LabRejectedRow> {}

/** 退役元数据（列表响应内嵌；旧策略已退役、当前仅新策略可运行）。 */
export interface LabRetirementInfo {
  retired_strategy_keys?: string[] | null;
  retired_at?: string | null;
  successor_strategy_id?: string | null;
  successor_strategy_key?: string | null;
  current_runnable_keys?: string[] | null;
  legacy_readonly?: string[] | null;
  note?: string | null;
  available?: boolean;
  error?: string | null;
}

export interface LabOrderRow {
  run_id: string;
  order_id: string;
  trade_id: string;
  signal_id: string | null;
  symbol: string;
  side: string;
  phase: string;
  ts_ms: number;
  action: string;
  reason: string;
  price: number;
  qty: number | null;
  fee: number | null;
  stop_price: number | null;
  signal_type: number | null;
}

export interface LabFundingRow {
  run_id: string;
  trade_id: string;
  funding_time_ms: number;
  rate: number;
  mark: number;
  qty: number;
  amount: number;
  source: string;
}

export interface LabOriginalSignal {
  run_id: string;
  signal_id: string;
  symbol: string;
  signal_bar_open_ms: number;
  confirm_ms: number;
  kind: string;
  direction: string;
  stop_price: number | null;
  atr: number | null;
  oi_score: number | null;
  oi_available: number | null;
  oi_quality: string | null;
  rank_key: string | null;
  in_window: number | null;
}

export interface LabTradeDetail extends LabEnvelope<LabTradeRow> {
  trade: LabTradeRow | null;
  orders: LabOrderRow[] | null;
  funding: LabFundingRow[] | null;
  original_signal: LabOriginalSignal | null;
  signal_id: string | null;
}

export interface LabChartCandle {
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  /** **原始 base 成交量**（不是 USDT 名义额） */
  v: number;
}

/**
 * `chart.candles` 的**线上实际格式**是长度 6 的数组元组，不是对象。
 *
 * 事实（2026-10-17 实测，`回测日志/event_trend_v0_1/runs/<run>/chart.json` 与
 * `GET /api/v1/backtests/{id}/chart` 一致）：
 *   candles[0] === [1704067200000, 11.665, 11.719, 11.652, 11.709, 12762.9]
 *   顺序 = [t, o, h, l, c, v]
 * 而 `backtest_runner.py` / `backtest_api.py` 的源码写的是 `{"t":…,"o":…}` 对象 ⇒
 * **落盘格式与源码不一致**（落盘端被压缩过，源码未同步）。前端两种都接受，
 * 由 `labCandlesToBars` 统一归一化 —— 这样落盘端无论往哪边收敛都不会再画不出 K 线。
 */
export type LabChartCandleTuple = [number, number, number, number, number, number];

export interface LabChartSignal {
  t: number;
  kind: string;
  direction: string;
  stop_price: number | null;
  atr: number | null;
  oi_score: number | null;
  oi_available: boolean | null;
  oi_quality: string | null;
}

export interface LabChartStopPoint {
  t: number;
  stop: number;
  reason: string;
}

export interface LabChartTrade {
  trade_id: string;
  symbol: string;
  direction: string;
  signal_type: number | null;
  entry: { t: number; price: number; qty: number };
  exit: { t: number; price: number; reason: string };
  stop_trace: LabChartStopPoint[];
}

export interface LabChartFundingEvent {
  t: number;
  rate: number;
  mark: number;
  amount: number;
  trade_id: string;
}

export interface LabChart {
  symbol: string;
  exec_timeframe: string;
  window: { start_ms: number; end_ms: number };
  candles: (LabChartCandle | LabChartCandleTuple)[];
  signals: LabChartSignal[];
  trades: LabChartTrade[];
  funding_events: LabChartFundingEvent[];
  equity_curve: { t: number; equity: number; cash: number; unrealized: number }[];
  benchmark: LabBenchmark | null;
  /** 固定止盈规则：`{exists:false, note}` ⇒ 明确显示「未配置固定止盈」 */
  take_profit: { exists: boolean; note: string } | null;
  time_precision: Record<string, string> | null;
  stop_trace_source: string | null;
}

export interface LabChartResponse extends LabEnvelope {
  chart: LabChart | null;
}

export interface LabDataQuality {
  status: string | null;
  reasons: string[] | null;
  signal_full_window_ok: boolean | null;
  exec_full_window_ok: boolean | null;
  signal_contiguous: boolean | null;
  exec_contiguous: boolean | null;
  all_closed: boolean | null;
  signal_expected_bars: number | null;
  signal_actual_bars: number | null;
  exec_expected_bars: number | null;
  exec_actual_bars: number | null;
  oi_bars_missing: number | null;
  oi_fake_fill: string | null;
  funding_model: string | null;
  funding_is_assumption: boolean | null;
  funding_frequency: Record<string, unknown> | null;
  funding_expected_settlements: number | null;
  funding_actual_settlements: number | null;
  funding_missing_settlements_count: number | null;
  funding_coverage_ok: boolean | null;
  funding_events_used: number | null;
  funding_events_recomputed: number | null;
  funding_evidence_reconcile: string | null;
  oi_min_resonance: number | null;
  oi_gate_enabled: boolean | null;
  oi_gate_note: string | null;
  reconcile: Record<string, number | boolean | null> | null;
  reconcile_ok: boolean | null;
  metrics_match: boolean | null;
  metrics_match_diffs: string[] | null;
  ledger_counts: Record<string, number> | null;
  time_precision_note: string | null;
  actual_config: Record<string, number | string | null> | null;
}

export interface LabQualityResponse extends LabEnvelope {
  data_quality: LabDataQuality | null;
  hashes: Record<string, string> | null;
  window: LabWindow | null;
  ledger_counts: Record<string, number> | null;
}

export interface LabRegistryEntry {
  strategy_key?: string;
  strategy_id?: string;
  name?: string;
  family?: string;
  timeframe?: string;
  direction?: string;
  available?: boolean;
  import_error?: string | null;
  signal_versions?: Record<string, string>;
  experiment_id?: string;
  signal_params_hash?: string;
  exec_params_hash?: string;
  oi_weights?: Record<string, number>;
  signal_names?: Record<string, string>;
  stop_modes?: string[];
  formal_run_gate?: Record<string, unknown>;
  live_order_enabled?: boolean;
  inherits_legacy_results?: boolean;
}

export interface LabListResponse extends LabEnvelope<LabRunSummary> {
  runs_root?: string;
  registry_entry?: LabRegistryEntry | null;
  /** WORK-04：旧策略退役汇总（新旧归属）。 */
  retirement?: LabRetirementInfo | null;
  /** 被排除出当前列表的非当前策略 run（只登记，不当成新策略结果）。 */
  excluded_legacy?: LabRunSummary[] | null;
  excluded_legacy_total?: number | null;
}

/* ══════════════════════════ 创建载荷 ══════════════════════════ */

export interface LabCreatePayload {
  strategy_id: string;
  exchange: string;
  market: string;
  symbols: string[];
  signal_timeframe: string;
  exec_timeframe: string;
  window_start: string;
  window_end: string;
  /** **全部费用/风险/结构参数都在 params 里**，不放顶层。 */
  params: Record<string, number>;
  note?: string;
}

export interface LabCreateResponse extends LabEnvelope<LabRunSummary> {}

/* ══════════════════════════ 调用 ══════════════════════════ */

export const labListRuns = (limit = 50) =>
  apiGet<LabListResponse>(`/backtests?limit=${encodeURIComponent(String(limit))}`);

export const labGetRun = (id: string) =>
  apiGet<LabRunDetail>(`/backtests/${encodeURIComponent(id)}`);

export const labGetMetrics = (id: string) =>
  apiGet<LabMetricsResponse>(`/backtests/${encodeURIComponent(id)}/metrics`);

export const labGetEquity = (id: string) =>
  apiGet<LabEquityResponse>(`/backtests/${encodeURIComponent(id)}/equity`);

export const labGetTrades = (id: string) =>
  apiGet<LabTradesResponse>(`/backtests/${encodeURIComponent(id)}/trades`);

/** 全局订单/成交明细（同一 run_id 账本 orders 表）。 */
export const labGetOrders = (id: string) =>
  apiGet<LabOrdersResponse>(`/backtests/${encodeURIComponent(id)}/orders`);

/** 拒单明细（同一 run_id 账本 rejected 表）。 */
export const labGetRejected = (id: string) =>
  apiGet<LabRejectedResponse>(`/backtests/${encodeURIComponent(id)}/rejected`);

export const labGetTrade = (id: string, tradeId: string) =>
  apiGet<LabTradeDetail>(`/backtests/${encodeURIComponent(id)}/trades/${encodeURIComponent(tradeId)}`);

export const labGetChart = (id: string) =>
  apiGet<LabChartResponse>(`/backtests/${encodeURIComponent(id)}/chart`);

export const labGetQuality = (id: string) =>
  apiGet<LabQualityResponse>(`/backtests/${encodeURIComponent(id)}/quality`);

export const labCreateRun = (payload: LabCreatePayload) =>
  apiPost<LabCreateResponse>('/backtests', payload);

/** 后台 400 的 `errors[]`（若有）—— 原样显示，不吞掉。 */
export function labErrorsOf(err: unknown): string[] {
  if (err instanceof ApiError) {
    const body = err.body as { errors?: unknown; missing_reason?: unknown } | null;
    const list = body?.errors;
    if (Array.isArray(list) && list.length > 0) return list.map((x) => String(x));
    if (typeof body?.missing_reason === 'string' && body.missing_reason) return [body.missing_reason];
    return [err.message];
  }
  return [(err as Error)?.message ?? '未知错误'];
}

/* ══════════════════════════ 标的池（真实数据池，只读）══════════════════════════ */

export interface LabPoolItem {
  exchange: string;
  market_id: string;
  symbol: string;
  base: string | null;
  quote: string | null;
  contract_type: string | null;
  status: string | null;
}

export interface LabPoolResponse {
  items?: LabPoolItem[];
  total?: number | null;
  pool_all_total?: number | null;
  quality?: { status?: string | null; note?: string | null } | null;
  missing_reason?: string | null;
}

/** `GET /pool` —— 工作台活动池（真实标的，只读）。 */
export const labGetPool = () => apiGet<LabPoolResponse>('/pool');

/* ══════════════════════════ 列表行的配置 hash ══════════════════════════ */

export interface LabRunHashes {
  spec_sha256: string | null;
  signal_params_hash: string | null;
  exec_params_hash: string | null;
  code_sha256: string | null;
  input_sha256: string | null;
  signal_frame_sha256: string | null;
}

/**
 * 批量取「配置 hash」。
 *
 * 为什么需要：实验列表端点（`GET /backtests`）的 `_summary` **不含任何 hash**
 * （精确缺口：`backtest_api._summary()` 未带 `spec_sha256` / `*_params_hash`），
 * 而实验记录列表要求显示「配置 hash」。这里用只读 `GET /backtests/{id}` 补齐，
 * 带上并发上限避免打爆单线程 HTTPServer；单条失败只留空，不伪造 hash。
 */
export async function labFetchRunHashMap(
  ids: string[],
  concurrency = 4,
): Promise<Record<string, LabRunHashes>> {
  const out: Record<string, LabRunHashes> = {};
  let cursor = 0;
  const worker = async () => {
    while (cursor < ids.length) {
      const id = ids[cursor++];
      try {
        const d = await labGetRun(id);
        const h = d.hashes ?? {};
        out[id] = {
          spec_sha256: h['spec_sha256'] ?? null,
          signal_params_hash: h['signal_params_hash'] ?? null,
          exec_params_hash: h['exec_params_hash'] ?? null,
          code_sha256: h['code_sha256'] ?? null,
          input_sha256: h['input_sha256'] ?? null,
          signal_frame_sha256: h['signal_frame_sha256'] ?? null,
        };
      } catch {
        out[id] = {
          spec_sha256: null, signal_params_hash: null, exec_params_hash: null,
          code_sha256: null, input_sha256: null, signal_frame_sha256: null,
        };
      }
    }
  };
  await Promise.all(Array.from({ length: Math.max(1, Math.min(concurrency, ids.length)) }, worker));
  return out;
}

/* ══════════════════════════ 默认值（来自后台真实运行，不硬编码冻结参数）══════════════ */

export interface LabDefaults {
  /** 生效的信号参数（来自「无覆盖」的成功 run ⇒ 即冻结参数） */
  signal_params: Record<string, number> | null;
  /** 生效的执行参数 */
  exec_params: Record<string, number> | null;
  /** 默认窗口（毫秒，UTC，已对齐 15m 网格） */
  window_start_ms: number | null;
  window_end_ms: number | null;
  symbol: string | null;
  exchange: string | null;
  market: string | null;
  signal_timeframe: string | null;
  exec_timeframe: string | null;
  /** 参数默认值的来源 run_id（供页面标注） */
  params_from_run: string | null;
  /** 窗口默认值的来源 run_id */
  window_from_run: string | null;
}

const EMPTY_DEFAULTS: LabDefaults = {
  signal_params: null,
  exec_params: {
    initial_equity: 2000.0,
    risk_per_trade: 0.005,
    fee_rate: 0.0005,
    maker_fee_rate: 0.0002,
    taker_fee_rate: 0.0005,
    slippage_bps: 3.0,
    max_concurrent_trades: 3,
  },
  window_start_ms: 1704067200000, // 2024-01-01 00:00:00 UTC
  window_end_ms: 1719705600000,   // 2024-06-30 00:00:00 UTC
  symbol: 'BTCUSDT',
  exchange: 'binance',
  market: 'um',
  signal_timeframe: '1h',
  exec_timeframe: '15m',
  params_from_run: null,
  window_from_run: null,
};

/**
 * 从**后台真实 run** 推导表单默认值：
 * · 参数默认值取「最近一次成功且 signal/exec 覆盖均为空」的 run 的**生效配置**
 *   （即候选包冻结参数）—— 避免在前端硬编码一份可能与候选包漂移的参数表；
 * · 窗口默认值取最近一次成功 run 的真实窗口。
 */
export async function labLoadDefaults(scanLimit = 12): Promise<LabDefaults> {
  const list = await labListRuns(50);
  const items = (list.items ?? []).filter((r) => r.status === 'succeeded');
  const out: LabDefaults = { ...EMPTY_DEFAULTS };
  if (items.length === 0) return out;

  const first = items[0];
  try {
    const d = await labGetRun(first.run_id);
    out.window_start_ms = d.window?.win_start_ms ?? null;
    out.window_end_ms = d.window?.win_end_ms ?? null;
    out.symbol = d.config?.spec?.symbols?.[0] ?? first.symbols?.[0] ?? null;
    out.exchange = d.config?.spec?.exchange ?? d.window?.exchange ?? null;
    out.market = d.config?.spec?.market ?? d.window?.market ?? null;
    out.signal_timeframe = d.config?.spec?.signal_timeframe ?? d.window?.signal_timeframe ?? null;
    out.exec_timeframe = d.config?.spec?.exec_timeframe ?? d.window?.exec_timeframe ?? null;
    out.window_from_run = first.run_id;
  } catch {
    /* 窗口默认值缺失 ⇒ 页面显示为空并要求填写；不造默认窗口 */
  }

  for (const r of items.slice(0, Math.max(1, scanLimit))) {
    try {
      const d = await labGetRun(r.run_id);
      const spec = d.config?.spec;
      const sigOv = spec?.signal_overrides ?? {};
      const execOv = spec?.exec_overrides ?? {};
      if (Object.keys(sigOv).length === 0 && Object.keys(execOv).length === 0) {
        out.signal_params = d.config?.config?.signal_params ?? null;
        out.exec_params = d.config?.config?.exec_params ?? null;
        out.params_from_run = r.run_id;
        break;
      }
    } catch {
      /* 单次读取失败不影响整体默认值；继续扫描 */
    }
  }
  return out;
}
