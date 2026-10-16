/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * 信号与持仓接通（2026-10-13）· 纸面账本 / 实盘只读 取数与下单。
 *
 * 纪律：
 *  · **纸面成交只经服务端账本**（`POST /api/v1/paper/orders`）。浏览器**不得**造仓；
 *    持仓一律来自服务端回执。
 *  · **实盘账户只读**：仅 `fetch_balance` / `fetch_positions`；不提交订单、不改权限、
 *    不返回密钥。
 *  · 后端未给的一律 `null`，前端不补 0、不推断。
 */

import { ApiError, apiGet, apiPost, unwrapItems } from './client';
import type {
  Envelope,
  RawLiveAccount,
  RawPaperAccount,
  RawPaperPending,
  RawPaperPositionFlat,
} from './types';

// ── 视图模型 ─────────────────────────────────────────────────────────────────

/**
 * 未来待成交挂单（服务端登记）。**因果纪律**：应成交时刻未到 ⇒ 挂单等真实进程时钟；
 * 错过成交时刻 ⇒ 服务端拒单，**不用旧价回填**。前端只展示，不推断、不造仓。
 */
export interface PaperPendingView {
  pendingId: number;
  symbol: string;
  origin: string | null;
  signalUid: string | null;
  eventTime: number | null;
  availableAt: number | null;
  requestedAt: number;
  earliestFillMs: number;
  createdAt: number;
  status: string;
  reason: string | null;
}

export interface PaperPositionView {
  /** 稳定键：`{arm}:{position_id}`（服务端账本唯一） */
  key: string;
  arm: string;
  accountId: string;
  positionId: number;
  /** 交易所原始 market_id（如 AAVEUSDT） */
  symbol: string;
  direction: 'long' | 'short';
  qty: number;
  entryPrice: number;
  markPrice: number | null;
  markSource: string | null;
  unrealizedPnl: number | null;
  unrealizedPnlPct: number | null;
  stopPrice: number | null;
  atrAtEntry: number | null;
  leverage: number;
  notional: number;
  entryTs: number;
  signalUid: string | null;
  openedBy: string | null;
  // ── 旧版/未验收/规则违规 追加审计标注（后端只读追加，原账本未改）──────────────
  /** 追加审计结论（如 `legacy_unverified`）；后端未给 ⇒ null。 */
  verificationStatus: string | null;
  /** 展示标签（如「旧版/未验收/规则违规」）；后端未给 ⇒ null。 */
  label: string | null;
  /** 命中的违规项列表；空数组 = 无追加违规记录（不等于「已验收通过」）。 */
  violations: string[];
  /** 是否已排除在正式策略表现之外。 */
  excludedFromOfficial: boolean;
}

export interface PaperAccountView {
  arm: string;
  accountId: string;
  initialEquity: number;
  cash: number;
  equity: number | null;
  unrealizedPnl: number;
  usedMargin: number;
  grossNotional: number;
  realizedPnlTotal: number;
  feesPaidTotal: number;
  fundingPaidTotal: number;
  drawdown: number;
  drawdownLocked: boolean;
  nPositions: number;
  nOrders: number;
  nRejected: number;
  nClosed: number;
  modelVersion: string;
  paramsSource: string | null;
  positions: PaperPositionView[];
  /** 未来待成交挂单（服务端登记） */
  pending: PaperPendingView[];
  nPending: number;
}

/** 挂单原始行 → 视图（逐项容错；缺失即 null/0，不推断）。 */
function mapPaperPending(raw: RawPaperPending): PaperPendingView {
  return {
    pendingId: num(raw.pending_id) ?? 0,
    symbol: raw.symbol,
    origin: raw.origin ?? null,
    signalUid: raw.signal_uid ?? null,
    eventTime: num(raw.event_time),
    availableAt: num(raw.available_at),
    requestedAt: num(raw.requested_at) ?? 0,
    earliestFillMs: num(raw.earliest_fill_ms) ?? 0,
    createdAt: num(raw.created_at) ?? 0,
    status: raw.status,
    reason: raw.reason ?? null,
  };
}

export interface LivePositionView {
  symbol: string | null;
  side: string | null;
  contracts: number | null;
  notionalUsd: number | null;
  entryPrice: number | null;
  markPrice: number | null;
  unrealizedPnl: number | null;
  leverage: number | null;
  raw: Record<string, unknown>;
}

export interface LiveAccountView {
  exchange: string;
  accountLabel: string;
  /**
   * `connected`（已连接、有持仓）/ `connected_no_positions`（已连接、当前无持仓）/
   * `not_configured`（未配置凭据）/ `auth_failed` / `error` / `reader_unavailable` / `stale`。
   */
  status: string;
  rawStatus: string;
  detail: string | null;
  errorClass: string | null;
  equityUsd: number | null;
  availableUsd: number | null;
  usedMarginUsd: number | null;
  positions: LivePositionView[];
  nPositions: number;
  readAt: string | null;
  ageSeconds: number | null;
}

export interface PaperLedger {
  accounts: PaperAccountView[];
  /** 所有臂的未平仓持仓（扁平） */
  positions: PaperPositionView[];
  /** 所有臂的未来待成交挂单（扁平，服务端登记） */
  pending: PaperPendingView[];
  /** 账本不可用时的真实原因（如 live worker 未跑）；可用时为 null */
  missingReason: string | null;
  qualityNote: string | null;
  /** 服务端**新开仓总闸**（fail-closed）；null = 未返回。 */
  newEntryGate: {
    allowNewEntries: boolean;
    reason: string | null;
    exitsFeesFundingHealthUnaffected: boolean;
  } | null;
  /** 旧版/未验收账本标记（后端只读追加）。 */
  legacyLedger: {
    preserved: boolean;
    verificationStatus: string | null;
    label: string | null;
    excludedFromOfficial: boolean;
  } | null;
  /** 实时起点水位线（UTC）；解释「为何自动持仓可能为空」。 */
  liveStartUtc: string | null;
  /** 水位线之前「仅展示」信号数 / 之后可自动开仓信号数（后端真实计数）。 */
  nPreWatermarkDisplayOnly: number | null;
  nAfterWatermark: number | null;
}

export interface LiveAccountsResult {
  accounts: LiveAccountView[];
  missingReason: string | null;
  qualityNote: string | null;
}

// ── 数值容错（后端 null / 字符串数字均可）────────────────────────────────────
const num = (v: unknown): number | null => {
  if (v === null || v === undefined) return null;
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? n : null;
};

function mapPaperPosition(
  row: RawPaperPositionFlat | (RawPaperAccount['positions'][number] & { arm?: string; account_id?: string }),
  arm: string,
  accountId: string,
): PaperPositionView {
  return {
    key: `${arm}:${row.position_id}`,
    arm,
    accountId,
    positionId: row.position_id,
    symbol: row.symbol,
    direction: row.direction === 'short' ? 'short' : 'long',
    qty: num(row.qty) ?? 0,
    entryPrice: num(row.entry_price) ?? 0,
    markPrice: num(row.mark_price),
    markSource: row.mark_source ?? null,
    unrealizedPnl: num(row.unrealized_pnl),
    unrealizedPnlPct: num(row.unrealized_pnl_pct),
    stopPrice: num(row.stop_price),
    atrAtEntry: num(row.atr_at_entry),
    leverage: num(row.leverage) ?? 1,
    notional: num(row.notional) ?? 0,
    entryTs: num(row.entry_ts) ?? 0,
    signalUid: row.signal_uid ?? null,
    openedBy: row.opened_by ?? null,
    verificationStatus: row.verification_status ?? null,
    label: row.label ?? null,
    violations: Array.isArray(row.violations) ? row.violations.filter((v): v is string => typeof v === 'string') : [],
    excludedFromOfficial: row.excluded_from_official_strategy_performance === true,
  };
}

function mapPaperAccount(raw: RawPaperAccount): PaperAccountView {
  const arm = raw.arm;
  const accountId = raw.account_id;
  const positions = Array.isArray(raw.positions)
    ? raw.positions.map((p) => mapPaperPosition(p, arm, accountId))
    : [];
  const pending = Array.isArray(raw.pending) ? raw.pending.map(mapPaperPending) : [];
  return {
    arm,
    accountId,
    initialEquity: num(raw.initial_equity) ?? 0,
    cash: num(raw.cash) ?? 0,
    equity: num(raw.equity),
    unrealizedPnl: num(raw.unrealized_pnl) ?? 0,
    usedMargin: num(raw.used_margin) ?? 0,
    grossNotional: num(raw.gross_notional) ?? 0,
    realizedPnlTotal: num(raw.realized_pnl_total) ?? 0,
    feesPaidTotal: num(raw.fees_paid_total) ?? 0,
    fundingPaidTotal: num(raw.funding_paid_total) ?? 0,
    drawdown: num(raw.drawdown) ?? 0,
    drawdownLocked: raw.drawdown_locked === true,
    nPositions: positions.length,
    nOrders: num(raw.n_orders) ?? 0,
    nRejected: num(raw.n_rejected) ?? 0,
    nClosed: num(raw.n_closed) ?? 0,
    modelVersion: raw.model_version ?? '—',
    paramsSource: raw.params_source ?? null,
    positions,
    pending,
    nPending: num(raw.n_pending) ?? pending.length,
  };
}

/**
 * 交易所持仓行 → 视图。**字段名按 ccxt 统一口径**（symbol/side/contracts/entryPrice/
 * markPrice/unrealizedPnl/leverage/notional），逐项容错；缺失即 null，不推断。
 */
function mapLivePosition(raw: Record<string, unknown>): LivePositionView {
  return {
    symbol: typeof raw.symbol === 'string' ? raw.symbol : null,
    side: typeof raw.side === 'string' ? raw.side : null,
    contracts: num(raw.contracts),
    notionalUsd: num(raw.notional),
    entryPrice: num(raw.entryPrice),
    markPrice: num(raw.markPrice),
    unrealizedPnl: num(raw.unrealizedPnl),
    leverage: num(raw.leverage),
    raw,
  };
}

function mapLiveAccount(raw: RawLiveAccount): LiveAccountView {
  const positions = Array.isArray(raw.positions) ? raw.positions.map(mapLivePosition) : [];
  return {
    exchange: raw.exchange,
    accountLabel: raw.account_label,
    status: raw.status,
    rawStatus: raw.raw_status,
    detail: raw.detail ?? null,
    errorClass: raw.error_class ?? null,
    equityUsd: num(raw.equity_usd),
    availableUsd: num(raw.available_usd),
    usedMarginUsd: num(raw.used_margin_usd),
    positions,
    nPositions: num(raw.n_positions) ?? positions.length,
    readAt: raw.read_at ?? null,
    ageSeconds: num(raw.age_seconds),
  };
}

// ── 取数 ─────────────────────────────────────────────────────────────────────

/** `GET /paper/accounts` → 四臂服务端账本。 */
export async function fetchPaperLedger(signal?: AbortSignal): Promise<PaperLedger> {
  const env = await apiGet<Envelope<RawPaperAccount>>('/paper/accounts', signal);
  const accounts = unwrapItems(env).map(mapPaperAccount);
  const positions = accounts.flatMap((a) => a.positions);
  const pending = accounts.flatMap((a) => a.pending);
  const q = env.quality ?? null;
  const gate = q?.new_entry_gate ?? null;
  const legacy = q?.legacy_ledger ?? null;
  return {
    accounts,
    positions,
    pending,
    missingReason: env.missing_reason ?? null,
    qualityNote: q?.note ?? null,
    newEntryGate: gate
      ? {
          allowNewEntries: gate.allow_new_entries === true,
          reason: gate.reason ?? gate.gate_reason ?? null,
          exitsFeesFundingHealthUnaffected: gate.exits_fees_funding_health_unaffected === true,
        }
      : null,
    legacyLedger: legacy
      ? {
          preserved: legacy.preserved === true,
          verificationStatus: legacy.verification_status ?? null,
          label: legacy.label ?? null,
          excludedFromOfficial: legacy.excluded_from_official_strategy_performance === true,
        }
      : null,
    liveStartUtc: q?.live_start_utc ?? null,
    nPreWatermarkDisplayOnly: num(q?.n_pre_watermark_display_only),
    nAfterWatermark: num(q?.n_after_watermark),
  };
}

/** `GET /accounts` → 实盘**只读**账户快照。 */
export async function fetchLiveAccounts(signal?: AbortSignal): Promise<LiveAccountsResult> {
  const env = await apiGet<Envelope<RawLiveAccount>>('/accounts', signal);
  return {
    accounts: unwrapItems(env).map(mapLiveAccount),
    missingReason: env.missing_reason ?? null,
    qualityNote: env.quality?.note ?? null,
  };
}

export interface PaperOpenInput {
  arm: string;
  symbol: string;
  direction?: 'long' | 'short';
  /** 信号 ATR（模拟止损基数）。缺失时后端明确拒绝 `missing_atr_and_stop`。 */
  atr?: number | null;
  stopPrice?: number | null;
  signalUid?: string | null;
}

export interface PaperOrderResult {
  ok: boolean;
  reason: string;
  positionId?: number;
  orderId?: number;
  qty?: number;
  entryPrice?: number;
  rawPrice?: number;
  stopPrice?: number;
  notional?: number;
  fee?: number;
  signalUid?: string | null;
  /** 未来待成交：已登记挂单（应成交时刻未到），由真实进程时钟推进 */
  pending?: boolean;
  pendingId?: number;
  earliestFillMs?: number;
  /** 因果审计：请求时刻 / 应成交时刻下界 */
  requestedAt?: number;
  marketSource?: string | null;
}

/**
 * `POST /paper/orders` action=open → **服务端模拟开仓**。
 *
 * 服务端成交时钟已优化为 0m 即时撮合，支持多空双向开仓。
 */
export async function postPaperOpen(input: PaperOpenInput, signal?: AbortSignal): Promise<PaperOrderResult> {
  const env = await apiPost<Envelope<Record<string, unknown>>>(
    '/paper/orders',
    {
      arm: input.arm,
      action: 'open',
      symbol: input.symbol,
      direction: input.direction || 'long',
      atr: input.atr ?? null,
      stop_price: input.stopPrice ?? null,
      signal_uid: input.signalUid ?? null,
    },
    signal,
  );
  const row = unwrapItems(env)[0] ?? {};
  const reason = typeof row.reason === 'string' ? row.reason : 'unknown';
  return {
    ok: row.ok === true,
    reason,
    positionId: num(row.position_id) ?? undefined,
    orderId: num(row.order_id) ?? undefined,
    qty: num(row.qty) ?? undefined,
    entryPrice: num(row.entry_price) ?? undefined,
    rawPrice: num(row.raw_price) ?? undefined,
    stopPrice: num(row.stop_price) ?? undefined,
    notional: num(row.notional) ?? undefined,
    fee: num(row.fee) ?? undefined,
    signalUid: typeof row.signal_uid === 'string' ? row.signal_uid : null,
    pending: reason === 'pending_future_fill',
    pendingId: num(row.pending_id) ?? undefined,
    earliestFillMs: num(row.earliest_fill_ms) ?? undefined,
    requestedAt: num(row.requested_at) ?? undefined,
    marketSource: typeof row.market_source === 'string' ? row.market_source : null,
  };
}

/**
 * `POST /paper/orders` action=close → **服务端模拟平仓**（按最近一根已闭合 15m 收盘）。
 */
export async function postPaperClose(
  arm: string,
  positionId: number,
  reason = 'manual',
  signal?: AbortSignal,
): Promise<PaperOrderResult> {
  const env = await apiPost<Envelope<Record<string, unknown>>>(
    '/paper/orders',
    { arm, action: 'close', position_id: positionId, reason },
    signal,
  );
  const row = unwrapItems(env)[0] ?? {};
  return {
    ok: row.ok === true,
    reason: typeof row.reason === 'string' ? row.reason : 'unknown',
    positionId: num(row.position_id) ?? positionId,
  };
}

/** 把 `ApiError` 转成给用户看的单行文案。 */
export function describeOrderError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 409) return `服务端拒绝：${err.message}`;
    if (err.status === 400) return `参数不合法：${err.message}`;
    if (err.status === 0) return '后端不可达（/api/v1）';
    return err.message;
  }
  return err instanceof Error ? err.message : '未知错误';
}
