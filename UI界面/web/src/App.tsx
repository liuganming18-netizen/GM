/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { CryptoSignal } from './types/crypto';
import { tokenId } from './api/adapters';
import { useMarketData } from './api/useMarketData';
import { useLiveTrading } from './api/useLiveTrading';
import { NavigationSidebar } from './components/NavigationSidebar';
import { SignalListPanel } from './components/SignalListPanel';
import { TokenTable } from './components/TokenTable';
import { PositionPanel } from './components/PositionPanel';
import { DataCenterView } from './components/DataCenterView';
import { StrategyManagementView } from './components/StrategyManagementView';
import { BacktestReviewView } from './components/BacktestReviewView';
import {
  Database,
  Sliders,
  History,
  CheckCircle2,
  TrendingUp,
  Image as ImageIcon,
  Activity,
  Layers,
  Sparkles
} from 'lucide-react';

export default function App() {
  const [currentNav, setCurrentNav] = useState('workbench');

  // 真实数据：全池行情 + 信号 + 顶部总览聚合；刷新由数据层按 5s 周期驱动。
  const {
    tokens,
    signals,
    error,
    asOf,
    aggregates,
    health,
    ensureDetail,
    setActiveDetailId,
    noteVisibleRows,
    publicExtras,
    importedAt,
  } = useMarketData();

  /**
   * 模拟持仓 = **服务端纸面账本**（2026-10-13 信号与持仓接通）。
   *
   * 历史沿革：2026-10-11 移除了浏览器内存里按固定 0.1 张 / 10 倍 / 2% 止损的本地造仓路径；
   * 当时后端无 accounts 模块 ⇒ 只能保持空态。现在后端已接通 `paper_engine` 服务端账本
   * （四臂各 5000U 初始、仅多、1 倍、3×ATR 固定止损、0.5% 风险、无 ROI），
   * 于是持仓与开平仓**全部来自服务端**：浏览器不造仓、不本地增删。
   */
  const {
    paperAccounts,
    paperPositions,
    paperPending,
    paperMissingReason,
    paperNote,
    liveAccounts,
    liveMissingReason,
    orderNotice,
    orderBusy,
    openPosition,
    closePosition,
  } = useLiveTrading();

  const [expandedTokenId, setExpandedTokenId] = useState<string | null>(null);
  const [selectedSignal, setSelectedSignal] = useState<CryptoSignal | undefined>(undefined);
  /** 本地下单前置提示（如未选信号）；服务端回执/拒绝由 useLiveTrading 的 orderNotice 提供。 */
  const [hintNotice, setHintNotice] = useState<string | null>(null);

  /**
   * 原版默认展开 BTC。只在**首次**拿到真实池时初始化一次：
   * 之后用户主动「收起」置为 null 必须保持收起，5 秒轮询刷新不得把展开状态重置回 BTC。
   */
  const didInitExpand = useRef(false);
  useEffect(() => {
    if (didInitExpand.current) return;
    if (tokens.length === 0) return;
    didInitExpand.current = true;
    const btc = tokens.find((t) => t.symbol === 'BTC');
    if (btc) setExpandedTokenId(btc.id);
  }, [tokens]);

  // 展开行 → 按需加载该合约的 K 线与 OI 序列；收起时告知数据层停止有界刷新
  useEffect(() => {
    setActiveDetailId(expandedTokenId);
  }, [expandedTokenId, setActiveDetailId]);

  useEffect(() => {
    if (expandedTokenId) ensureDetail(expandedTokenId);
  }, [expandedTokenId, ensureDetail]);

  /**
   * 选中信号**随刷新重新解析**（UI04 修复 2026-10-11；信号区清理 2026-10-14 收紧）。
   *
   * 按 signal_id 在当前（`scope=current`）列表中重解析，取最新生命周期字段。
   * **已不在当前列表中 ⇒ 清除选中引用**（返回 undefined）：
   * 被移出的记录（历史研究 / 启动前仅展示 / 结构提示 / 失效过期）不得再作为
   * 可开仓对象留在缓存里，否则空态下仍可能用旧信号卡触发模拟开仓。
   */
  useEffect(() => {
    setSelectedSignal((prev) => {
      if (!prev) return prev;
      const fresh = signals.find((s) => s.id === prev.id);
      if (!fresh) return undefined;
      return fresh;
    });
  }, [signals]);

  // When user clicks a signal -> locate corresponding token & expand K-line chart
  const handleSelectSignal = (signal: CryptoSignal) => {
    setSelectedSignal(signal);
    /**
     * UI02 修复（2026-10-11）：用**原始交易所 + market_id（合约身份）**精确定位，
     * 不再按“去倍率后的简称”找第一个匹配 —— 同名跨所（Binance/OKX 的 PEPE）
     * 或倍率合约（1000PEPE）会被打开成错误行情。
     * 无目标合约时明确不展开，**不回退首币**。
     */
    const target = tokens.find(
      (t) => t.id === tokenId(signal.rawExchange ?? '', signal.rawSymbol)
    );
    if (target) setExpandedTokenId(target.id);
  };

  /**
   * When user clicks a token row in the table -> toggle expand
   *
   * 引用必须稳定：`TokenTable` 的行组件是 memo 化的，若这个回调每次渲染都换新引用，
   * 全部已渲染行都会因 props 变化而重渲染，memo 失效（列表性能整改 2026-10-07）。
   */
  const handleToggleExpand = useCallback((tokenId: string) => {
    setExpandedTokenId((prev) => (prev === tokenId ? null : tokenId));
  }, []);

  /**
   * Close position handler：**服务端账本**平仓（按最近一根已闭合 15m 收盘）。
   * 成功/失败均由服务端回执决定，本地不预扣盈亏、不先行移除。
   */
  const handleClosePosition = useCallback(
    (arm: string, positionId: number) => {
      setHintNotice(null);
      void closePosition(arm, positionId, 'manual_ui');
    },
    [closePosition],
  );

  /** 已批准的四个启用族（开仓按信号所属臂路由；未知则回落 3a）。 */
  const ARMS = useMemo(() => ['3a', '3b', '3c', '3d'], []);

  /**
   * 按「当前选中信号」在服务端纸面账本开仓。
   *
   * 为什么必须绑定信号：开仓需要**该信号的 ATR** 作止损基数（止损 = 入场 − 3×ATR），
   * 且信号身份用于幂等去重（同一 signal_uid 只开一次，避免重复下单）。
   * 未选信号 / 信号缺 ATR ⇒ 本地先给出明确原因；其余一律交给服务端判定，
   * 服务端拒绝时显示其**真实**原因（不伪造成交、不本地造仓）。
   */
  const handleOpenQuickPosition = useCallback(
    (direction: 'long' | 'short') => {
      setHintNotice(null);
      const sig = selectedSignal;
      if (!sig) {
        setHintNotice('请先在信号区选择一个信号：开仓需要该信号的 ATR 作为止损基数。');
        return;
      }
      // ── 当前交易信号区身份校验（2026-10-14 信号区清理）────────────────────────
      // 只有「实时真实臂入场」信号可开仓；历史研究 / 启动前仅展示 / 结构提示
      // （display_only / not_this_arm_structure 等）一律拒绝，不依赖 UI 是否隐藏。
      if (
        sig.origin !== 'live' ||
        sig.executableState !== 'enterable' ||
        sig.role !== 'enterable_arm_entry'
      ) {
        setHintNotice('该信号不在当前交易信号区（历史研究 / 启动前仅展示 / 结构提示），不可开仓。');
        return;
      }
      if (sig.executableState && sig.executableState !== 'enterable') {
        setHintNotice(`该信号当前可执行态为「${sig.executableState}」，不可开仓（后端会拒绝）。`);
        return;
      }
      if (sig.detectionAtr === null || sig.detectionAtr === undefined || !Number.isFinite(sig.detectionAtr)) {
        setHintNotice(
          `信号 ${sig.tokenSymbol} 未携带 ATR，无法计算止损；服务端会以 missing_atr_and_stop 拒绝。`,
        );
        return;
      }
      const arm = sig.arm && ARMS.includes(sig.arm) ? sig.arm : '3a';
      void openPosition({
        arm,
        symbol: sig.rawSymbol,
        direction,
        atr: sig.detectionAtr,
        signalUid: sig.id,
      });
    },
    [selectedSignal, openPosition, ARMS],
  );

  /** 顶栏「模拟账户可用」= 四臂服务端账本可用现金合计（无账本时 null ⇒ '—'）。 */
  const paperBalance = paperAccounts.length > 0
    ? paperAccounts.reduce((a, x) => a + x.cash, 0)
    : null;

  return (
    <div className="flex h-[100dvh] w-screen overflow-hidden bg-neutral-950 text-neutral-100 font-sans selection:bg-emerald-500/30">
      {/* 1. Left Sidebar: 大类导航 (Navigation) */}
      <NavigationSidebar
        currentNav={currentNav}
        onSelectNav={setCurrentNav}
        realtimeAgeSeconds={health.realtimeAgeSeconds}
        realtimeStatus={health.realtimeStatus}
      />

      {/* Main Content Area */}
      <div className="flex-1 flex flex-col h-full overflow-hidden min-w-0">

        {/* 3. Center Workspace Body */}
        {currentNav === 'workbench' ? (
          <div className="workbench-columns flex-1 min-h-0 flex overflow-hidden">
            {/* 3A. Left Sub-Column: 信号区 (Signals) */}
            <SignalListPanel
              signals={signals}
              tokens={tokens}
              selectedSignalId={selectedSignal?.id || null}
              onSelectSignal={handleSelectSignal}
            />

            {/* 3B. Center Column: 全市场代币排序表 + 行内展开K线 */}
            <TokenTable
              tokens={tokens}
              expandedTokenId={expandedTokenId}
              onToggleExpand={handleToggleExpand}
              activeSignal={selectedSignal}
              onVisibleTokenIdsChange={noteVisibleRows}
              publicExtras={publicExtras}
            />

            {/* 3C. Right Sub-Column: 模拟实盘持仓（服务端账本） + 实盘只读仓位 */}
            <PositionPanel
              paperAccounts={paperAccounts}
              paperPositions={paperPositions}
              paperPending={paperPending}
              paperMissingReason={paperMissingReason}
              paperNote={paperNote}
              liveAccounts={liveAccounts}
              liveMissingReason={liveMissingReason}
              orderBusy={orderBusy}
              onSelectTokenSymbol={(sym) => {
                const target = tokens.find((t) => t.symbol === sym);
                if (target) setExpandedTokenId(target.id);
              }}
              onClosePosition={handleClosePosition}
              onOpenQuickPosition={handleOpenQuickPosition}
              orderNotice={hintNotice ?? orderNotice}
            />
          </div>
        ) : currentNav === 'datacenter' ? (
          /* 数据中心 (按数据中心全页面草图.svg渲染) */
          <DataCenterView importedAt={importedAt} activeMarketKeys={tokens.map((token) => token.id)} />
        ) : currentNav === 'strategy' ? (
          /* 策略管理 (按策略管理全页面草图.svg渲染) */
          <StrategyManagementView />
        ) : (
          /* 回测复盘 (按回测复盘全页面草图.svg渲染) */
          <BacktestReviewView />
        )}
      </div>

    </div>
  );
}
