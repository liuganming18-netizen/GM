/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useMemo, useState } from 'react';
import { TokenIcon } from './TokenIcon';
import type {
  LiveAccountView,
  PaperAccountView,
  PaperPendingView,
  PaperPositionView,
} from '../api/liveTrading';
import {
  Wallet,
  Shield,
  ArrowRight,
  TrendingUp,
  TrendingDown,
  Lock,
  History,
  AlertCircle,
  Clock,
  Layers,
} from 'lucide-react';

interface PositionPanelProps {
  /** 四臂纸面账户（服务端账本） */
  paperAccounts: PaperAccountView[];
  /** 所有臂的未平仓持仓（服务端账本） */
  paperPositions: PaperPositionView[];
  /**
   * 所有臂的**未来待成交挂单**（服务端登记，真实进程时钟推进）。
   * 因果纪律：应成交时刻未到 ⇒ 等；错过成交时刻 ⇒ 服务端拒单，不用旧价回填。
   */
  paperPending?: PaperPendingView[];
  /** 纸面账本不可用时的真实原因 */
  paperMissingReason: string | null;
  /**
   * 服务端纸面账本的口径说明（含**实时起点水位线**：为什么自动持仓可能是空的）。
   * 空持仓时必须展示，否则会被误读成「没接通」。
   */
  paperNote?: string | null;
  /** 服务端**新开仓总闸**（fail-closed）；null = 后端未返回。 */
  newEntryGate?: { allowNewEntries: boolean; reason: string | null; exitsFeesFundingHealthUnaffected: boolean } | null;
  /** 旧版/未验收账本标记（后端只读追加）。 */
  legacyLedger?: { preserved: boolean; verificationStatus: string | null; label: string | null; excludedFromOfficial: boolean } | null;
  /** 实盘只读账户快照 */
  liveAccounts: LiveAccountView[];
  /** 实盘账户为空时的真实原因（未配置凭据 / worker 未运行） */
  liveMissingReason: string | null;
  /** 是否有下单在途 */
  orderBusy: boolean;
  onSelectTokenSymbol: (symbol: string) => void;
  onClosePosition: (arm: string, positionId: number) => void;
  onOpenQuickPosition: (direction: 'long' | 'short') => void;
  /** 下单/平仓的真实回执或服务端拒绝原因 */
  orderNotice?: string | null;
}

/** 数字格式化：null / 非有限值一律 '—'，不显示 0 冒充真实值。 */
const fmt = (v: number | null | undefined, digits = 2): string =>
  v === null || v === undefined || !Number.isFinite(v)
    ? '—'
    : v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });

const fmtUsd = (v: number | null | undefined, digits = 2): string =>
  v === null || v === undefined || !Number.isFinite(v) ? '—' : `$${fmt(v, digits)}`;

const fmtSignedUsd = (v: number | null | undefined, digits = 2): string => {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  const sign = v > 0 ? '+' : v < 0 ? '-' : '';
  return `${sign}$${fmt(Math.abs(v), digits)}`;
};

const fmtPriceSmart = (v: number | null | undefined): string => {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  const abs = Math.abs(v);
  const digits = abs >= 1000 ? 2 : abs >= 1 ? 4 : abs >= 0.01 ? 5 : 6;
  return `$${fmt(v, digits)}`;
};

const fmtCompactUtcMs = (ms: number | null | undefined): string => {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms <= 0) return '—';
  const d = new Date(ms);
  const mo = String(d.getUTCMonth() + 1).padStart(2, '0');
  const da = String(d.getUTCDate()).padStart(2, '0');
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mm = String(d.getUTCMinutes()).padStart(2, '0');
  return `${mo}-${da} ${hh}:${mm}Z`;
};

/** 开仓时间精确到秒：YYYY-MM-DD HH:mm:ss */
const fmtDateTimeSeconds = (ms: number | null | undefined): string => {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms <= 0) return '—';
  const d = new Date(ms);
  const y = d.getFullYear();
  const mo = String(d.getMonth() + 1).padStart(2, '0');
  const da = String(d.getDate()).padStart(2, '0');
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  const ss = String(d.getSeconds()).padStart(2, '0');
  return `${y}-${mo}-${da} ${hh}:${mm}:${ss}`;
};

/** 实盘状态 → 展示文案与配色。**区分**「已连接但无持仓」与「未配置 / 鉴权失败 / 数据陈旧」。 */
const LIVE_STATUS: Record<string, { text: string; cls: string }> = {
  connected: { text: '已连接', cls: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30' },
  connected_no_positions: {
    text: '已连接 · 无持仓',
    cls: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/25',
  },
  not_configured: { text: '未配置凭据', cls: 'bg-neutral-800 text-neutral-400 border-neutral-700' },
  auth_failed: { text: '鉴权失败', cls: 'bg-rose-500/20 text-rose-400 border-rose-500/30' },
  error: { text: '读取失败', cls: 'bg-rose-500/20 text-rose-400 border-rose-500/30' },
  reader_unavailable: { text: '读取器不可用', cls: 'bg-amber-500/20 text-amber-400 border-amber-500/30' },
  stale: { text: '数据陈旧', cls: 'bg-amber-500/20 text-amber-400 border-amber-500/30' },
};

export const PositionPanel: React.FC<PositionPanelProps> = ({
  paperAccounts,
  paperPositions,
  paperPending,
  paperMissingReason,
  paperNote,
  newEntryGate,
  legacyLedger,
  liveAccounts,
  liveMissingReason,
  orderBusy,
  onSelectTokenSymbol,
  onClosePosition,
  onOpenQuickPosition,
  orderNotice,
}) => {
  const [selectedArm, setSelectedArm] = useState<'ALL' | string>('ALL');

  const paperConnected = paperAccounts.length > 0 && !paperMissingReason;

  /** 全局四臂合计（始终挂在 data-cash / data-equity 等属性上，确保验收脚本兼容）。 */
  const totalEquityAll = paperConnected
    ? paperAccounts.reduce((a, x) => a + (x.equity ?? 0), 0)
    : null;
  const totalCashAll = paperConnected ? paperAccounts.reduce((a, x) => a + x.cash, 0) : null;
  const allPendingList = paperPending ?? paperAccounts.flatMap((a) => a.pending);

  /** 当前选中视图下的账户集合（ALL = 全部 4 臂汇总，否则为指定单臂）。 */
  const scopedAccounts = useMemo(
    () =>
      selectedArm === 'ALL'
        ? paperAccounts
        : paperAccounts.filter((a) => a.arm === selectedArm),
    [paperAccounts, selectedArm],
  );

  const scopedPositions = useMemo(
    () =>
      selectedArm === 'ALL'
        ? paperPositions
        : paperPositions.filter((p) => p.arm === selectedArm),
    [paperPositions, selectedArm],
  );

  const scopedPending = useMemo(() => {
    if (selectedArm === 'ALL') return allPendingList;
    const acc = paperAccounts.find((a) => a.arm === selectedArm);
    return acc ? acc.pending : [];
  }, [allPendingList, paperAccounts, selectedArm]);

  const viewStats = useMemo(() => {
    if (!paperConnected || scopedAccounts.length === 0) {
      return {
        initialEquity: null,
        equity: null,
        cash: null,
        usedMargin: null,
        grossNotional: null,
        unrealizedPnl: null,
        realizedPnl: null,
        feesPaid: null,
        fundingPaid: null,
        maxDrawdownPct: null,
        anyDrawdownLocked: false,
        nOrders: null,
        nRejected: null,
        nClosed: null,
        paramsSource: null as string | null,
      };
    }
    const initialEquity = scopedAccounts.reduce((s, a) => s + a.initialEquity, 0);
    const equity = scopedAccounts.reduce((s, a) => s + (a.equity ?? 0), 0);
    const cash = scopedAccounts.reduce((s, a) => s + a.cash, 0);
    const usedMargin = scopedAccounts.reduce((s, a) => s + a.usedMargin, 0);
    const grossNotional = scopedAccounts.reduce((s, a) => s + a.grossNotional, 0);
    const unrealizedPnl = scopedAccounts.reduce((s, a) => s + a.unrealizedPnl, 0);
    const realizedPnl = scopedAccounts.reduce((s, a) => s + a.realizedPnlTotal, 0);
    const feesPaid = scopedAccounts.reduce((s, a) => s + a.feesPaidTotal, 0);
    const fundingPaid = scopedAccounts.reduce((s, a) => s + a.fundingPaidTotal, 0);
    const maxDrawdownPct = Math.max(...scopedAccounts.map((a) => (a.drawdown ?? 0) * 100));
    const anyDrawdownLocked = scopedAccounts.some((a) => a.drawdownLocked);
    const nOrders = scopedAccounts.reduce((s, a) => s + a.nOrders, 0);
    const nRejected = scopedAccounts.reduce((s, a) => s + a.nRejected, 0);
    const nClosed = scopedAccounts.reduce((s, a) => s + (a.nClosed || 0), 0);
    // 参数来源：直接取自服务端（逐臂可能不同）；用于暴露「是否走了冻结视图外的兜底」。
    const sources = Array.from(new Set(scopedAccounts.map((a) => a.paramsSource).filter((v): v is string => !!v)));
    const paramsSource = sources.length > 0 ? sources.join(' / ') : null;
    return {
      initialEquity,
      equity,
      cash,
      usedMargin,
      grossNotional,
      unrealizedPnl,
      realizedPnl,
      feesPaid,
      fundingPaid,
      maxDrawdownPct,
      anyDrawdownLocked,
      nOrders,
      nRejected,
      nClosed,
      paramsSource,
    };
  }, [paperConnected, scopedAccounts]);

  const modelVersion = paperAccounts[0]?.modelVersion ?? null;
  const exposurePct =
    viewStats.equity && viewStats.equity > 0 && viewStats.grossNotional !== null
      ? (viewStats.grossNotional / viewStats.equity) * 100
      : null;

  return (
    <aside
      className="workbench-side-panel workbench-position-panel bg-neutral-900 border-l border-neutral-800 flex flex-col justify-between shrink-0 select-none overflow-hidden"
      data-testid="paper-panel"
      data-connected={paperConnected ? '1' : '0'}
      data-arms={paperAccounts.map((a) => a.arm).join(',')}
    >
      <div className="workbench-position-content flex-1 overflow-y-auto divide-y divide-neutral-800">
        {/* Section 1: 模拟实盘持仓（服务端账本） */}
        <div className="p-3.5 space-y-2.5">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-bold text-neutral-100 flex items-center gap-1.5">
              <Wallet className="w-4 h-4 text-emerald-400" />
              <span>模拟实盘持仓</span>
            </h3>
            <span className="text-xs font-mono text-neutral-400">
              共 {paperPositions.length} 仓
            </span>
          </div>

          {/* Account Details Box —— 严格仅显示用户指定的8大核心指标，全单行横排，杜绝断字折行 */}
          <div
            className="p-3 rounded-lg bg-neutral-950 border border-neutral-800 text-xs space-y-2 font-mono text-neutral-400"
            data-testid="paper-account-box"
            data-cash={totalCashAll === null ? '' : totalCashAll}
            data-equity={totalEquityAll === null ? '' : totalEquityAll}
            data-n-positions={paperPositions.length}
            data-n-pending={allPendingList.length}
          >
            <div className="flex justify-between items-center">
              <span className="text-neutral-400 whitespace-nowrap">总资金:</span>
              <span className="text-neutral-100 font-bold whitespace-nowrap text-sm">
                {fmtUsd(viewStats.equity)}
              </span>
            </div>
            <div className="flex justify-between items-center">
              <span className="text-neutral-400 whitespace-nowrap">占用资金:</span>
              <span className="text-neutral-200 font-medium whitespace-nowrap">
                {fmtUsd(viewStats.usedMargin)}
              </span>
            </div>
            <div className="flex justify-between items-center">
              <span className="text-neutral-400 whitespace-nowrap">手续费:</span>
              <span className="text-neutral-200 whitespace-nowrap">
                {fmtUsd(viewStats.feesPaid)}
              </span>
            </div>
            <div className="flex justify-between items-center">
              <span className="text-neutral-400 whitespace-nowrap">资金费:</span>
              <span
                className={`whitespace-nowrap font-medium ${
                  viewStats.fundingPaid && viewStats.fundingPaid > 0
                    ? 'text-amber-500'
                    : 'text-neutral-200'
                }`}
              >
                {fmtSignedUsd(viewStats.fundingPaid)}
              </span>
            </div>
            <div className="flex justify-between items-center">
              <span className="text-neutral-400 whitespace-nowrap">参数来源:</span>
              <span
                className={`whitespace-nowrap ${
                  viewStats.paramsSource && viewStats.paramsSource.includes('fallback')
                    ? 'text-amber-400'
                    : 'text-neutral-200'
                }`}
                title="服务端返回的生效参数来源；含 fallback 表示未走冻结视图"
              >
                {viewStats.paramsSource ?? '—'}
              </span>
            </div>
            <div className="flex justify-between items-center">
              <span className="text-neutral-400 whitespace-nowrap">盈亏多少:</span>
              <span className="whitespace-nowrap font-semibold">
                <span
                  className={
                    viewStats.unrealizedPnl !== null && viewStats.unrealizedPnl < 0
                      ? 'text-rose-500'
                      : 'text-emerald-500'
                  }
                >
                  {fmtSignedUsd(viewStats.unrealizedPnl)}
                </span>
                <span className="text-neutral-500 mx-1">/</span>
                <span
                  className={
                    viewStats.realizedPnl !== null && viewStats.realizedPnl < 0
                      ? 'text-rose-500'
                      : 'text-emerald-500'
                  }
                >
                  {fmtSignedUsd(viewStats.realizedPnl)}
                </span>
              </span>
            </div>
            <div className="flex justify-between items-center">
              <span className="text-neutral-400 whitespace-nowrap">最大回撤:</span>
              <span className="text-neutral-200 font-medium whitespace-nowrap">
                {viewStats.maxDrawdownPct !== null ? `${fmt(viewStats.maxDrawdownPct, 2)}%` : '—'}
              </span>
            </div>
            <div className="flex justify-between items-center">
              <span className="text-neutral-400 whitespace-nowrap">开仓/平仓数:</span>
              <span className="text-neutral-200 font-medium whitespace-nowrap">
                {scopedPositions.length} 开 / {viewStats.nClosed ?? 0} 平
              </span>
            </div>
          </div>

          {/* 服务端真实门禁 / 旧版账本 状态（来源：/paper/accounts 的 quality，非前端硬编码） */}
          {paperConnected && newEntryGate && !newEntryGate.allowNewEntries && (
            <div
              className="p-2 rounded-lg bg-rose-500/10 border border-rose-500/30 text-[10px] text-rose-300 leading-relaxed flex gap-1.5"
              data-testid="new-entry-gate"
            >
              <Lock className="w-3.5 h-3.5 shrink-0 mt-0.5" />
              <span className="break-all">
                服务端已阻断全部新开仓（总闸 fail-closed；原因：{newEntryGate.reason ?? '未标注'}）
                {newEntryGate.exitsFeesFundingHealthUnaffected ? '；退出/费用/资金费/健康采集不受影响' : ''}
              </span>
            </div>
          )}
          {paperConnected && legacyLedger && legacyLedger.excludedFromOfficial && (
            <div className="p-2 rounded-lg bg-amber-500/10 border border-amber-500/30 text-[10px] text-amber-300 leading-relaxed">
              既有持仓为 <b>{legacyLedger.label ?? '旧版/未验收'}</b>，已排除正式策略表现
              （原账本{legacyLedger.preserved ? '未修改' : '状态未知'}）。
            </div>
          )}

          {/* Quick Trade —— 按「当前选中信号」开仓 */}
          <div className="flex gap-2">
            <button
              onClick={() => onOpenQuickPosition('long')}
              disabled={orderBusy}
              title="按当前选中信号在服务端账本开多仓"
              className={`flex-1 py-1.5 rounded-lg border text-xs font-semibold flex items-center justify-center gap-1 transition-colors ${
                orderBusy
                  ? 'bg-neutral-800/60 text-neutral-500 border-neutral-700 cursor-wait'
                  : 'bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-500 border-emerald-500/30'
              }`}
            >
              <TrendingUp className="w-3.5 h-3.5" />
              按选中信号开多
            </button>
            <button
              onClick={() => onOpenQuickPosition('short')}
              disabled={orderBusy}
              title="按当前选中信号在服务端账本开空仓"
              className={`flex-1 py-1.5 rounded-lg border text-xs font-semibold flex items-center justify-center gap-1 transition-colors ${
                orderBusy
                  ? 'bg-neutral-800/60 text-neutral-500 border-neutral-700 cursor-wait'
                  : 'bg-rose-500/15 hover:bg-rose-500/25 text-rose-500 border-rose-500/30'
              }`}
            >
              <TrendingDown className="w-3.5 h-3.5" />
              按选中信号开空
            </button>
          </div>

          {/* 服务端回执 / 拒绝原因：真实文案，不隐藏、不伪造 */}
          {orderNotice && (
            <div className="p-2 rounded-lg bg-amber-500/10 border border-amber-500/30 text-[10px] text-amber-300 leading-relaxed flex gap-1.5">
              <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
              <span className="break-all">{orderNotice}</span>
            </div>
          )}

          {/* Positions List Header —— 明确这是「实际成交」层（只能来自服务端账本） */}
          <div className="flex items-center justify-between text-[10px] text-neutral-400 pt-0.5">
            <span className="flex items-center gap-1 font-medium text-neutral-300">
              <Layers className="w-3 h-3 text-emerald-400" />
              {selectedArm === 'ALL'
                ? `实际成交 · 在持仓位 (${scopedPositions.length})`
                : `${selectedArm} 臂实际成交持仓 (${scopedPositions.length})`}
            </span>
            <span className="font-mono text-neutral-500">账本口径 · 点击定位 K 线</span>
          </div>

          {/* Positions List —— 服务端账本持仓（「已买入」标记只能来自此处） */}
          <div className="space-y-2">
            {scopedPositions.length === 0 ? (
              <>
                <div className="p-3 rounded-lg bg-neutral-950/60 border border-neutral-800 border-dashed text-center text-[11px] text-neutral-500">
                  {paperConnected
                    ? selectedArm === 'ALL'
                      ? '当前无模拟持仓（无账本成交 ⇒ 不显示任何「已买入」标记）'
                      : `策略臂 ${selectedArm} 当前无未平仓持仓`
                    : '账本未接通'}
                </div>
                {paperConnected && paperNote && selectedArm === 'ALL' && (
                  <div className="p-2 rounded-lg bg-neutral-950/60 border border-neutral-800 text-[10px] text-neutral-500 leading-relaxed">
                    <div className="text-neutral-400 font-medium mb-0.5">为什么现在没有自动持仓</div>
                    <span className="break-all">{paperNote}</span>
                  </div>
                )}
              </>
            ) : (
              scopedPositions.map((pos) => {
                const isLong = pos.direction === 'long';
                const cleanSym = pos.symbol.replace('USDT', '');
                const pnl = pos.unrealizedPnl;

                /**
                 * **真实止损状态**（2026-10-15 任务四）。
                 *
                 * 旧实现在这里用 `entryPrice ∓ 2.0×ATR` **前端自行推导**一个「建议止损位」——
                 * 该值与账本/冻结规则（3×ATR 结构止损）无关，属凭空建议，已删除。
                 * 现只展示**账本真实记录的 `stop_price`**，并按真实 `mark_price` 如实说明现价与止损的位置关系
                 * （纯数值比较，不预测、不生成新价位）。
                 */
                const stopState = (() => {
                  if (pos.stopPrice === null || pos.stopPrice === undefined) {
                    return { text: '账本未记录止损', cls: 'text-neutral-400' };
                  }
                  if (pos.markPrice === null || pos.markPrice === undefined) {
                    return { text: '账本止损价（现价未知）', cls: 'text-amber-500' };
                  }
                  const through = isLong ? pos.markPrice <= pos.stopPrice : pos.markPrice >= pos.stopPrice;
                  return through
                    ? { text: '现价已在止损之外（账本尚未触发）', cls: 'text-rose-400' }
                    : { text: '现价在止损内', cls: 'text-emerald-400' };
                })();

                /** 旧版/未验收/规则违规 标注（后端只读追加；缺失则不标）。 */
                const isLegacy =
                  pos.excludedFromOfficial ||
                  (pos.verificationStatus !== null && pos.verificationStatus !== 'verified');

                return (
                  <div
                    key={pos.key}
                    onClick={() => onSelectTokenSymbol(cleanSym)}
                    className="p-3 rounded-lg bg-neutral-950 border border-neutral-800 hover:border-emerald-500/40 cursor-pointer space-y-2 transition-all text-xs"
                    data-testid="paper-position-row"
                    data-position-id={pos.positionId}
                    data-signal-uid={pos.signalUid ?? ''}
                    data-verification-status={pos.verificationStatus ?? ''}
                  >
                    {/* 头部：代币图标 + 代币名称 + 旧版标记 + 平仓按钮 */}
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex items-center gap-2 min-w-0">
                        <TokenIcon symbol={cleanSym} size="sm" />
                        <span className="text-sm font-bold text-neutral-100 whitespace-nowrap">
                          {cleanSym}
                        </span>
                        {isLegacy && (
                          <span
                            className="text-[9px] px-1.5 py-0.5 rounded border border-amber-500/40 bg-amber-500/10 text-amber-300 font-sans shrink-0"
                            title={`${pos.label ?? '旧版/未验收'}${pos.violations.length ? ' · 违规：' + pos.violations.join('、') : ''}`}
                          >
                            {pos.label ?? '旧版/未验收'}
                          </span>
                        )}
                      </div>

                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          onClosePosition(pos.arm, pos.positionId);
                        }}
                        disabled={orderBusy}
                        className={`px-2.5 py-1 rounded border transition-colors text-xs shrink-0 font-medium ${
                          orderBusy
                            ? 'text-neutral-600 border-neutral-800 cursor-wait'
                            : 'text-neutral-300 border-neutral-700 hover:text-rose-400 hover:border-rose-500/50 hover:bg-rose-500/10'
                        }`}
                        title="按当前市价立即平仓"
                      >
                        平仓
                      </button>
                    </div>

                    {/* 旧版/未验收 说明：排除正式策略表现 */}
                    {isLegacy && (
                      <div className="text-[9px] text-amber-400/80 leading-relaxed font-sans">
                        旧版/未验收：已排除正式策略表现；原账本未修改
                        {pos.violations.length > 0 && <span> · 违规项 {pos.violations.join('、')}</span>}
                      </div>
                    )}

                    {/* 核心信息列表 */}
                    <div className="space-y-1.5 text-xs font-mono bg-neutral-900/60 p-2.5 rounded border border-neutral-800/80">
                      <div className="flex justify-between items-center">
                        <span className="text-neutral-400 whitespace-nowrap">开仓价格:</span>
                        <span className="text-neutral-100 font-medium whitespace-nowrap">{fmtPriceSmart(pos.entryPrice)}</span>
                      </div>
                      <div className="flex justify-between items-center">
                        <span className="text-neutral-400 whitespace-nowrap">实时标记:</span>
                        <span className="text-neutral-100 font-medium whitespace-nowrap">{fmtPriceSmart(pos.markPrice)}</span>
                      </div>
                      <div className="flex justify-between items-center">
                        <span className="text-neutral-400 whitespace-nowrap">持仓金额:</span>
                        <span className="text-neutral-100 font-medium whitespace-nowrap">{fmt(pos.notional, 1)} U</span>
                      </div>
                      <div className="flex justify-between items-center">
                        <span className="text-neutral-400 whitespace-nowrap">未实现盈亏:</span>
                        <span
                          className={`font-bold whitespace-nowrap ${
                            pnl === null
                              ? 'text-neutral-400'
                              : pnl >= 0
                                ? 'text-emerald-500'
                                : 'text-rose-500'
                          }`}
                        >
                          {fmtSignedUsd(pnl, 2)}
                          {pos.unrealizedPnlPct !== null
                            ? ` (${pos.unrealizedPnlPct >= 0 ? '+' : ''}${fmt(pos.unrealizedPnlPct, 2)}%)`
                            : ''}
                        </span>
                      </div>
                      {/* 真实止损：只展示账本记录的 stop_price 与入场 ATR 基数（无前端推导） */}
                      <div className="flex justify-between items-center">
                        <span className="text-neutral-400 whitespace-nowrap">账本止损价:</span>
                        <span className="text-amber-500 font-medium whitespace-nowrap">{fmtPriceSmart(pos.stopPrice)}</span>
                      </div>
                      <div className="flex justify-between items-center">
                        <span className="text-neutral-400 whitespace-nowrap">止损基数(ATR@入场):</span>
                        <span className="text-neutral-300 font-medium whitespace-nowrap">{fmtPriceSmart(pos.atrAtEntry)}</span>
                      </div>
                      <div className="flex justify-between items-center">
                        <span className="text-neutral-400 whitespace-nowrap">止损状态:</span>
                        <span className={`font-medium whitespace-nowrap ${stopState.cls}`}>{stopState.text}</span>
                      </div>
                      <div className="flex justify-between items-center">
                        <span className="text-neutral-400 whitespace-nowrap">信号身份(signal_uid):</span>
                        <span className="text-neutral-300 font-medium whitespace-nowrap truncate max-w-[60%]" title={pos.signalUid ?? ''}>
                          {pos.signalUid ?? '—'}
                        </span>
                      </div>
                      <div className="flex justify-between items-center pt-1 border-t border-neutral-800/60 text-[11px]">
                        <span className="text-neutral-400 whitespace-nowrap">开仓时间:</span>
                        <span className="text-neutral-200 font-medium whitespace-nowrap">{fmtDateTimeSeconds(pos.entryTs)}</span>
                      </div>
                    </div>
                  </div>
                );
              })
            )}
          </div>

          {/* 未来待成交挂单（服务端登记）——**计划下单**层：未成交，不冒充已买入 */}
          {paperConnected && scopedPending.length > 0 && (
            <div className="space-y-1.5 pt-1" data-testid="paper-pending-block">
              <div className="text-[10px] text-neutral-300 flex items-center justify-between">
                <span className="flex items-center gap-1 font-medium">
                  <Clock className="w-3 h-3 text-amber-400" />
                  <span>计划下单 · 待成交挂单 ({scopedPending.length} 笔)</span>
                </span>
                <span className="text-[9px] text-amber-400/80 font-mono">
                  未成交 · 等真实 15m 闭合 · 防旧价回填
                </span>
              </div>
              {scopedPending.map((p) => (
                <div
                  key={`${p.symbol}:${p.pendingId}`}
                  className="p-2 rounded-lg bg-amber-500/5 border border-amber-500/20 text-[10px] font-mono text-neutral-400 space-y-0.5"
                >
                  <div className="flex items-center justify-between">
                    <span className="text-neutral-200 font-bold">{p.symbol}</span>
                    <span className="text-amber-300">
                      {p.origin === 'manual' ? '页面手动请求' : '实时信号触发'} · {p.status}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span>最早可执行 15m:</span>
                    <span className="text-amber-200">{fmtCompactUtcMs(p.earliestFillMs)}</span>
                  </div>
                  <div className="flex justify-between">
                    <span>请求锚点时刻:</span>
                    <span className="text-neutral-300">{fmtCompactUtcMs(p.requestedAt)}</span>
                  </div>
                  <div className="flex justify-between">
                    <span>信号确认/可得:</span>
                    <span className="text-neutral-400">
                      {fmtCompactUtcMs(p.eventTime)} / {fmtCompactUtcMs(p.availableAt)}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}

          <div className="text-[10px] text-neutral-500 flex items-center justify-between pt-1">
            <span>持仓历史 → 回测复盘</span>
            <span>四臂独立记账 · 守恒校验</span>
          </div>
        </div>

        {/* Section 2: 实盘仓位（只读）—— 真实状态，不硬编码「未连接API」 */}
        <div className="p-3.5 space-y-3">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-bold text-neutral-100 flex items-center gap-1.5">
              <Shield className="w-4 h-4 text-neutral-400" />
              <span>实盘仓位（只读）</span>
            </h3>
            <span className="text-[10px] px-1.5 py-0.5 rounded font-mono bg-neutral-800 text-neutral-400 border border-neutral-700">
              {liveAccounts.length > 0 ? `${liveAccounts.length} 交易所` : '无快照'}
            </span>
          </div>

          {liveAccounts.length === 0 ? (
            <div className="p-4 rounded-lg bg-neutral-950/60 border border-neutral-800 border-dashed text-center space-y-2">
              <div className="w-8 h-8 rounded-full bg-neutral-800/80 mx-auto flex items-center justify-center text-neutral-500">
                <Lock className="w-4 h-4" />
              </div>
              <div className="text-xs text-neutral-300 font-medium">
                {liveMissingReason ?? '尚无实盘账户快照'}
              </div>
              <p className="text-[11px] text-neutral-500 leading-relaxed">
                只读：仅读取余额 / 权益 / 持仓，不提交任何真实订单、不修改 API 权限。
              </p>
              <div className="text-[10px] font-mono text-neutral-600 pt-1 border-t border-neutral-850">
                账户 / 连接状态：—<br />
                权益 / 可用余额：—<br />
                币种 / 方向 / 数量：—<br />
                入场均价 / 标记价：—
              </div>
            </div>
          ) : (
            <div className="space-y-2">
              {liveAccounts.map((acc) => {
                const meta = LIVE_STATUS[acc.status] ?? {
                  text: acc.status,
                  cls: 'bg-neutral-800 text-neutral-400 border-neutral-700',
                };
                return (
                  <div
                    key={acc.exchange + ':' + acc.accountLabel}
                    data-testid="live-account-row"
                    data-exchange={acc.exchange}
                    data-status={acc.status}
                    data-n-positions={acc.nPositions}
                    className="p-2.5 rounded-lg bg-neutral-950 border border-neutral-800 space-y-1.5"
                  >
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-bold text-neutral-100">{acc.exchange}</span>
                      <span className={`text-[10px] px-1.5 py-0.2 rounded font-mono border ${meta.cls}`}>
                        {meta.text}
                      </span>
                    </div>
                    <div className="grid grid-cols-2 gap-1 text-[11px] font-mono text-neutral-400">
                      <div>权益: <span className="text-neutral-200">{fmtUsd(acc.equityUsd)}</span></div>
                      <div>可用: <span className="text-neutral-200">{fmtUsd(acc.availableUsd)}</span></div>
                      <div>已用保证金: <span className="text-neutral-200">{fmtUsd(acc.usedMarginUsd)}</span></div>
                      <div>持仓数: <span className="text-neutral-200">{acc.nPositions}</span></div>
                    </div>
                    {acc.status === 'connected_no_positions' && (
                      <div className="text-[10px] text-emerald-300/90">已连接、当前无持仓</div>
                    )}
                    {acc.detail && acc.status !== 'connected_no_positions' && (
                      <div className="text-[10px] text-amber-300/90 break-all">{acc.detail}</div>
                    )}
                    {acc.positions.length > 0 && (
                      <div className="space-y-1 pt-1 border-t border-neutral-850">
                        {acc.positions.map((p, i) => (
                          <div
                            key={(p.symbol ?? 'pos') + i}
                            className="text-[10px] font-mono text-neutral-300 flex justify-between gap-2"
                          >
                            <span className="truncate">{p.symbol ?? '—'}</span>
                            <span className="shrink-0">
                              {p.side ?? '—'} {fmt(p.contracts, 4)} @ {fmtUsd(p.entryPrice, 4)}
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                    <div className="text-[9px] text-neutral-600 font-mono">
                      只读快照 {acc.readAt ?? '—'}
                      {acc.ageSeconds === null ? '' : ` · ${fmt(acc.ageSeconds, 0)}s 前`}
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          <div className="text-[10px] text-neutral-500 flex items-center justify-between pt-1">
            <span className="flex items-center gap-1">
              <History className="w-3 h-3" />
              点击持仓 → 定位中间K线
            </span>
            <span>仓位历史 → 回测复盘</span>
          </div>
        </div>
      </div>
    </aside>
  );
};
