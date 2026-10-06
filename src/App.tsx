/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import { initialSignals, initialTokens, initialPaperPositions } from './mock/cryptoData';
import { CryptoSignal, Position, TokenMarketItem } from './types/crypto';
import { NavigationSidebar } from './components/NavigationSidebar';
import { TopStatusBar } from './components/TopStatusBar';
import { SignalListPanel } from './components/SignalListPanel';
import { TokenTable } from './components/TokenTable';
import { PositionPanel } from './components/PositionPanel';
import { SketchDrawerModal } from './components/SketchDrawerModal';
import { DataCenterView } from './components/DataCenterView';
import { HeatmapView } from './components/HeatmapView';
import { StrategyManagementView } from './components/StrategyManagementView';
import { BacktestReviewView } from './components/BacktestReviewView';
import { formatWanYi } from './utils/formatters';
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
  const [tokens, setTokens] = useState<TokenMarketItem[]>(initialTokens);
  const [signals, setSignals] = useState<CryptoSignal[]>(initialSignals);
  const [paperPositions, setPaperPositions] = useState<Position[]>(initialPaperPositions);
  const [paperBalance, setPaperBalance] = useState<number>(100399.12);
  const [expandedTokenId, setExpandedTokenId] = useState<string | null>('btc');
  const [selectedSignal, setSelectedSignal] = useState<CryptoSignal | undefined>(initialSignals[0]);
  const [isSketchDrawerOpen, setIsSketchDrawerOpen] = useState(false);

  // Subtle real-time price tick simulation for realism
  useEffect(() => {
    const interval = setInterval(() => {
      setTokens((prevTokens) =>
        prevTokens.map((t) => {
          const delta = (Math.random() - 0.49) * 0.001 * t.price;
          const newPrice = Number((t.price + delta).toFixed(t.price < 1 ? 4 : 2));
          return {
            ...t,
            price: newPrice,
          };
        })
      );
    }, 3000);
    return () => clearInterval(interval);
  }, []);

  // When user clicks a signal -> locate corresponding token & expand K-line chart
  const handleSelectSignal = (signal: CryptoSignal) => {
    setSelectedSignal(signal);
    const targetToken = tokens.find(
      (t) => t.symbol.toLowerCase() === signal.tokenSymbol.toLowerCase()
    );
    if (targetToken) {
      setExpandedTokenId(targetToken.id);
    }
  };

  // When user clicks a token row in the table -> toggle expand
  const handleToggleExpand = (tokenId: string) => {
    setExpandedTokenId((prev) => (prev === tokenId ? null : tokenId));
  };

  // Close position handler
  const handleClosePosition = (id: string) => {
    const pos = paperPositions.find((p) => p.id === id);
    if (pos) {
      setPaperBalance((prev) => Number((prev + pos.unrealizedPnl).toFixed(2)));
      setPaperPositions((prev) => prev.filter((p) => p.id !== id));
    }
  };

  // Quick Open Position handler
  const handleOpenQuickPosition = (symbol: string, direction: 'long' | 'short') => {
    const token = tokens.find((t) => t.symbol === symbol) || tokens[0];
    const newPos: Position = {
      id: `pos-${Date.now()}`,
      symbol: `${token.symbol}USDT`,
      direction,
      amount: direction === 'long' ? 0.1 : 0.1,
      entryPrice: token.price,
      markPrice: token.price,
      unrealizedPnl: 0,
      unrealizedPnlPercent: 0,
      stopLoss: Number((token.price * (direction === 'long' ? 0.98 : 1.02)).toFixed(2)),
      takeProfit: Number((token.price * (direction === 'long' ? 1.05 : 0.95)).toFixed(2)),
      riskRatioPercent: 15.0,
      leverage: 10,
      openTime: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
    };
    setPaperPositions([newPos, ...paperPositions]);
  };

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-neutral-950 text-neutral-100 font-sans selection:bg-emerald-500/30">
      {/* 1. Left Sidebar: 大类导航 (Navigation) */}
      <NavigationSidebar
        currentNav={currentNav}
        onSelectNav={setCurrentNav}
        onOpenSketchDrawer={() => setIsSketchDrawerOpen(true)}
      />

      {/* Main Content Area */}
      <div className="flex-1 flex flex-col h-full overflow-hidden min-w-0">
        {/* 2. Top Header: 顶部总览 / 状态区 */}
        <TopStatusBar
          btcPrice={tokens.find((t) => t.symbol === 'BTC')?.price || 67120}
          btcChange={tokens.find((t) => t.symbol === 'BTC')?.change24h || 3.42}
          ethPrice={tokens.find((t) => t.symbol === 'ETH')?.price || 2638}
          ethChange={tokens.find((t) => t.symbol === 'ETH')?.change24h || 1.85}
          paperBalance={paperBalance}
        />

        {/* 3. Center Workspace Body */}
        {currentNav === 'workbench' ? (
          <div className="flex-1 flex overflow-hidden">
            {/* 3A. Left Sub-Column: 信号区 (Signals) */}
            <SignalListPanel
              signals={signals}
              selectedSignalId={selectedSignal?.id || null}
              onSelectSignal={handleSelectSignal}
            />

            {/* 3B. Center Column: 全市场代币排序表 + 行内展开K线 */}
            <TokenTable
              tokens={tokens}
              expandedTokenId={expandedTokenId}
              onToggleExpand={handleToggleExpand}
              activeSignal={selectedSignal}
            />

            {/* 3C. Right Sub-Column: 模拟实盘持仓 + 实盘只读仓位 */}
            <PositionPanel
              paperPositions={paperPositions}
              onSelectTokenSymbol={(sym) => {
                const target = tokens.find((t) => t.symbol === sym);
                if (target) setExpandedTokenId(target.id);
              }}
              onClosePosition={handleClosePosition}
              onOpenQuickPosition={handleOpenQuickPosition}
            />
          </div>
        ) : currentNav === 'heatmap' ? (
          /* 热力图专区 (全景订单流与OK/币安深度清算热力图) */
          <HeatmapView />
        ) : currentNav === 'datacenter' ? (
          /* 数据中心 (按数据中心全页面草图.svg渲染) */
          <DataCenterView />
        ) : currentNav === 'strategy' ? (
          /* 策略管理 (按策略管理全页面草图.svg渲染) */
          <StrategyManagementView />
        ) : (
          /* 回测复盘 (按回测复盘全页面草图.svg渲染) */
          <BacktestReviewView />
        )}
      </div>

      {/* 4. Original Sketches Overlay / Comparison Drawer Modal */}
      <SketchDrawerModal
        isOpen={isSketchDrawerOpen}
        onClose={() => setIsSketchDrawerOpen(false)}
      />
    </div>
  );
}
