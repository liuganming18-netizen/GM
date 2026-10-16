/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * 信号与持仓接通（2026-10-13）· 纸面账本 + 实盘只读 的取数与下单编排。
 *
 * 关键纪律：
 *  · **持仓不来自浏览器**：`paperPositions` 只由服务端账本 `GET /paper/accounts` 产生；
 *    开/平仓走 `POST /paper/orders`，成功后**立即重取**账本，不在本地增删。
 *  · **实盘只读**：`GET /accounts` 仅余额/权益/持仓；不提交订单、不返回密钥。
 *  · 刷新节拍与行情层一致（5s）；失败保留上一次成功值并标记 `stale`，**不伪造空仓**。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  describeOrderError,
  fetchLiveAccounts,
  fetchPaperLedger,
  postPaperClose,
  postPaperOpen,
  type LiveAccountView,
  type PaperAccountView,
  type PaperLedger,
  type PaperOrderResult,
  type PaperPendingView,
  type PaperPositionView,
} from './liveTrading';

/** 与行情层同一刷新节拍（5s）。 */
const LIVE_REFRESH_MS = 5000;

/**
 * WORK-04 退役提示：Paper 未接新共享核心期间，任何开/平仓一律 fail-closed。
 * 文案明确「不回退旧执行通道」，避免被读成临时故障。
 */
const NOT_CONNECTED_ORDER_NOTICE =
  '模拟实盘尚未接通新策略核心（NOT_CONNECTED）：已禁止新开仓与真实下单，不回退旧执行通道（exec_bridge）。';

export interface LiveTradingState {
  /** 四臂账户（服务端账本） */
  paperAccounts: PaperAccountView[];
  /** 所有臂的未平仓持仓（扁平，服务端账本） */
  paperPositions: PaperPositionView[];
  /** 所有臂的**未来待成交挂单**（扁平，服务端登记，真实时钟推进） */
  paperPending: PaperPendingView[];
  /** 纸面账本不可用时的真实原因（可用时 null） */
  paperMissingReason: string | null;
  /** 纸面账本口径说明（含实时起点水位线：解释空持仓） */
  paperNote: string | null;
  /** 服务端**新开仓总闸**（fail-closed）；null = 后端未返回。 */
  newEntryGate: PaperLedger['newEntryGate'];
  /** 旧版/未验收账本标记（后端只读追加）。 */
  legacyLedger: PaperLedger['legacyLedger'];
  // ── WORK-04 退役：Paper 未接新共享核心时的 NOT_CONNECTED 身份 ──────────────
  /** 服务端 `quality.status`（`ok`/`not_connected`/`unavailable`/`retired`）。 */
  paperStatus: string | null;
  /** Paper 是否处于 `NOT_CONNECTED`（未接新核心 ⇒ 禁止新开仓 / 真实下单）。 */
  paperNotConnected: boolean;
  /** 新共享核心策略 id（后端 `quality.new_core`）。 */
  paperNewCore: string | null;
  /** 新共享核心是否已接线到 Paper。 */
  paperNewCoreWired: boolean | null;
  /** 已退役旧策略注册表键（`3a/3b/3c/3d/A/B`）。 */
  paperRetiredKeys: string[];
  /** Paper / Live 接线状态块（后端 `quality.paper_live`）。 */
  paperLive: PaperLedger['paperLive'];
  /** 实时起点水位线（UTC）。 */
  liveStartUtc: string | null;
  /** 水位线前「仅展示」信号数 / 之后可自动开仓信号数（后端真实计数）。 */
  nPreWatermarkDisplayOnly: number | null;
  nAfterWatermark: number | null;
  /** 实盘只读账户快照 */
  liveAccounts: LiveAccountView[];
  /** 实盘账户为空时的真实原因（如未配置凭据 / worker 未运行） */
  liveMissingReason: string | null;
  /** 最近一次下单/平仓的提示（成功或服务端拒绝的真实原因） */
  orderNotice: string | null;
  /** 是否有下单在途 */
  orderBusy: boolean;
  /** 首次加载完成 */
  loaded: boolean;
  /** 服务端模拟开仓 */
  openPosition: (input: {
    arm: string;
    symbol: string;
    direction?: 'long' | 'short';
    atr?: number | null;
    stopPrice?: number | null;
    signalUid?: string | null;
  }) => Promise<PaperOrderResult | null>;
  /** 服务端模拟平仓 */
  closePosition: (arm: string, positionId: number, reason?: string) => Promise<PaperOrderResult | null>;
  /** 清除提示 */
  clearNotice: () => void;
  /** 立即重取账本（下单成功后调用） */
  refresh: () => void;
}

/** 纸面账本空态（字段齐全，避免 undefined 访问）。 */
const EMPTY_LEDGER: PaperLedger = {
  accounts: [],
  positions: [],
  pending: [],
  missingReason: null,
  qualityNote: null,
  newEntryGate: null,
  legacyLedger: null,
  liveStartUtc: null,
  nPreWatermarkDisplayOnly: null,
  nAfterWatermark: null,
  status: null,
  newCore: null,
  newCoreWired: null,
  retiredStrategyKeys: [],
  paperLive: null,
};

export function useLiveTrading(): LiveTradingState {
  const [ledger, setLedger] = useState<PaperLedger>(EMPTY_LEDGER);
  const [liveAccounts, setLiveAccounts] = useState<LiveAccountView[]>([]);
  const [liveMissingReason, setLiveMissingReason] = useState<string | null>(null);
  const [orderNotice, setOrderNotice] = useState<string | null>(null);
  const [orderBusy, setOrderBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const alive = useRef(true);
  const busy = useRef(false);
  const pending = useRef(false);
  /**
   * 最近一次成功读取的 `quality.status`。
   * 用 ref（而非 state）供**下单守卫**同步读取，避免把 `ledger` 拖进 `openPosition`
   * 的依赖而令回调引用每次刷新都变化。
   */
  const statusRef = useRef<string | null>(null);

  const pull = useCallback(async () => {
    if (busy.current) {
      pending.current = true; // 在途时被要求重取 → 结束后补一轮
      return;
    }
    busy.current = true;
    try {
      // 纸面账本：失败保留上次成功值（不清空，避免下单后闪空）
      try {
        const l = await fetchPaperLedger();
        if (alive.current) {
          setLedger(l);
          statusRef.current = l.status;
        }
      } catch {
        if (alive.current) {
          statusRef.current = null;
          setLedger((prev) => ({ ...prev, missingReason: '纸面账本读取失败（后端不可达或未初始化）' }));
        }
      }
      // 实盘只读：失败保留上次成功值
      try {
        const r = await fetchLiveAccounts();
        if (alive.current) {
          setLiveAccounts(r.accounts);
          setLiveMissingReason(r.missingReason);
        }
      } catch {
        if (alive.current) setLiveMissingReason('实盘账户读取失败（后端不可达）');
      }
      if (alive.current) setLoaded(true);
    } finally {
      busy.current = false;
      if (pending.current) {
        pending.current = false;
        void pull();
      }
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    void pull();
    const timer = setInterval(() => void pull(), LIVE_REFRESH_MS);
    return () => {
      alive.current = false;
      clearInterval(timer);
    };
  }, [pull]);

  const openPosition = useCallback<LiveTradingState['openPosition']>(
    async (input) => {
      // ── WORK-04 退役（fail-closed）：Paper 未接新核心 ⇒ 客户端也不发单，不回退旧 exec_bridge ──
      if (statusRef.current === 'not_connected') {
        setOrderNotice(NOT_CONNECTED_ORDER_NOTICE);
        return null;
      }
      setOrderBusy(true);
      setOrderNotice(null);
      try {
        const r = await postPaperOpen(input);
        setOrderNotice(
          r.ok
            ? `已按服务端账本开仓 ${input.symbol}（成交价 ${r.entryPrice ?? '—'}，数量 ${r.qty ?? '—'}，止损 ${r.stopPrice ?? '—'}）`
            : r.pending
              ? `已登记挂单 ${input.symbol}：应成交时刻尚未到达（${r.earliestFillMs ? new Date(r.earliestFillMs).toISOString() : '待定'} 之后的第一根已闭合 15m bar），由服务端真实时钟推进；不提前成交、不回填旧价`
              : `服务端未成交：${r.reason}`,
        );
        void pull();
        return r;
      } catch (err) {
        setOrderNotice(describeOrderError(err));
        return null;
      } finally {
        setOrderBusy(false);
      }
    },
    [pull],
  );

  const closePosition = useCallback<LiveTradingState['closePosition']>(
    async (arm, positionId, reason = 'manual') => {
      // ── WORK-04 退役（fail-closed）：Paper 未接新核心 ⇒ 客户端也不发单 ──
      if (statusRef.current === 'not_connected') {
        setOrderNotice(NOT_CONNECTED_ORDER_NOTICE);
        return null;
      }
      setOrderBusy(true);
      setOrderNotice(null);
      try {
        const r = await postPaperClose(arm, positionId, reason);
        setOrderNotice(r.ok ? `已按服务端账本平仓（position ${positionId}）` : `服务端未平仓：${r.reason}`);
        void pull();
        return r;
      } catch (err) {
        setOrderNotice(describeOrderError(err));
        return null;
      } finally {
        setOrderBusy(false);
      }
    },
    [pull],
  );

  const clearNotice = useCallback(() => setOrderNotice(null), []);

  return {
    paperAccounts: ledger.accounts,
    paperPositions: ledger.positions,
    paperPending: ledger.pending,
    paperMissingReason: ledger.missingReason,
    paperNote: ledger.qualityNote,
    newEntryGate: ledger.newEntryGate,
    legacyLedger: ledger.legacyLedger,
    paperStatus: ledger.status,
    paperNotConnected: ledger.status === 'not_connected',
    paperNewCore: ledger.newCore,
    paperNewCoreWired: ledger.newCoreWired,
    paperRetiredKeys: ledger.retiredStrategyKeys,
    paperLive: ledger.paperLive,
    liveStartUtc: ledger.liveStartUtc,
    nPreWatermarkDisplayOnly: ledger.nPreWatermarkDisplayOnly,
    nAfterWatermark: ledger.nAfterWatermark,
    liveAccounts,
    liveMissingReason,
    orderNotice,
    orderBusy,
    loaded,
    openPosition,
    closePosition,
    clearNotice,
    refresh: () => void pull(),
  };
}
