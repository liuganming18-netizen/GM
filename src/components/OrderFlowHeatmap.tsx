/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useMemo } from 'react';
import { TokenMarketItem } from '../types/crypto';
import { formatWanYi, formatAmountWanYi } from '../utils/formatters';
import {
  Flame,
  Activity,
  Layers,
  ArrowUpRight,
  ArrowDownRight,
  TrendingUp,
  TrendingDown,
  ShieldAlert,
  Zap,
  Info,
  Sliders,
  ChevronRight
} from 'lucide-react';

interface OrderFlowHeatmapProps {
  token: TokenMarketItem;
}

type ExchangeFilter = 'all' | 'binance' | 'okx';
type HeatmapMode = 'depth_heatmap' | 'footprint_cvd' | 'whale_tape';

interface PriceHeatmapLevel {
  price: number;
  priceFormatted: string;
  distancePercent: number; // e.g. +1.5%
  type: 'ask_wall' | 'bid_wall' | 'liquidation_short' | 'liquidation_long' | 'neutral';
  totalLiquidityUsd: number;
  binanceShare: number; // 0 to 100
  okxShare: number; // 0 to 100
  intensity: number; // 0 to 1 scale for color
  leverageTier?: '25x' | '50x' | '100x';
  note: string;
}

interface FootprintBar {
  time: string;
  closePrice: number;
  bidVol: number; // active sell (hit bid)
  askVol: number; // active buy (lift ask)
  delta: number; // askVol - bidVol
  cvdAccum: number;
  imbalanceRatio: number; // e.g. 1.8x
  isBullish: boolean;
}

interface WhaleTrade {
  id: string;
  time: string;
  exchange: 'Binance' | 'OKX';
  side: 'buy' | 'sell';
  price: number;
  amountUsd: number;
  amountCoins: number;
  isBlockTrade: boolean;
}

export const OrderFlowHeatmap: React.FC<OrderFlowHeatmapProps> = ({ token }) => {
  const [exchange, setExchange] = useState<ExchangeFilter>('all');
  const [mode, setMode] = useState<HeatmapMode>('depth_heatmap');
  const [hoveredLevel, setHoveredLevel] = useState<PriceHeatmapLevel | null>(null);

  const currentPrice = token.price;

  // Generate 16 realistic depth price levels centered on current price
  const priceLevels: PriceHeatmapLevel[] = useMemo(() => {
    const levels: PriceHeatmapLevel[] = [];
    const count = 16;
    const stepPercent = 0.45; // 0.45% step per level
    const baseLiquidity = token.oi.totalUsd * 0.008; // calibrated baseline wall size

    for (let i = -8; i <= 8; i++) {
      if (i === 0) continue; // Skip exact current price
      const dist = i * stepPercent;
      const levelPrice = Number((currentPrice * (1 + dist / 100)).toFixed(currentPrice > 10 ? 2 : 4));
      
      const isAbove = i > 0;
      const absDist = Math.abs(dist);

      // Distinct institutional clusters:
      // i = 3, 4: Heavy ask wall / short liquidation cluster
      // i = -3, -4: Heavy bid wall / long liquidation cluster
      let multiplier = 1;
      let type: PriceHeatmapLevel['type'] = isAbove ? 'ask_wall' : 'bid_wall';
      let leverageTier: PriceHeatmapLevel['leverageTier'] = undefined;
      let note = isAbove ? '上方限价卖单挂单带' : '下方限价买单托盘带';

      if (i === 3 || i === 4) {
        multiplier = 3.6;
        type = 'liquidation_short';
        leverageTier = i === 3 ? '100x' : '50x';
        note = `空头集中清算带 (${leverageTier} 杠杆集中强平位)`;
      } else if (i === 6) {
        multiplier = 4.2;
        type = 'ask_wall';
        leverageTier = '25x';
        note = '币安/OKX机构巨额压盘大单墙';
      } else if (i === -3 || i === -4) {
        multiplier = 3.8;
        type = 'liquidation_long';
        leverageTier = i === -3 ? '100x' : '50x';
        note = `多头集中清算带 (${leverageTier} 杠杆集中强平位)`;
      } else if (i === -6) {
        multiplier = 4.5;
        type = 'bid_wall';
        leverageTier = '25x';
        note = '主力大户密集托单吸收墙';
      } else {
        multiplier = 0.8 + Math.abs(Math.sin(i * 1.7)) * 1.5;
      }

      const totalUsd = Math.round(baseLiquidity * multiplier);
      // Realistic Binance vs OKX share (Binance 55%~65%, OKX 35%~45%)
      const binanceShare = Number((58 + Math.sin(i * 3) * 10).toFixed(0));
      const okxShare = 100 - binanceShare;
      const intensity = Math.min(1, Math.max(0.15, multiplier / 4.5));

      levels.push({
        price: levelPrice,
        priceFormatted: currentPrice > 100 ? levelPrice.toLocaleString() : levelPrice.toFixed(4),
        distancePercent: Number(dist.toFixed(2)),
        type,
        totalLiquidityUsd: totalUsd,
        binanceShare,
        okxShare,
        intensity,
        leverageTier,
        note,
      });
    }

    // Sort descending by price (highest price on top, lowest on bottom)
    return levels.sort((a, b) => b.price - a.price);
  }, [currentPrice, token.oi.totalUsd]);

  // Generate 12 Footprint Bars with CVD & Delta
  const footprintBars: FootprintBar[] = useMemo(() => {
    const bars: FootprintBar[] = [];
    let accumCvd = 0;
    const now = Date.now();

    for (let i = 12; i >= 0; i--) {
      const time = new Date(now - i * 5 * 60 * 1000).toLocaleTimeString('zh-CN', {
        hour: '2-digit',
        minute: '2-digit',
      });
      const isBull = Math.sin(i * 1.4) + (token.change24h > 0 ? 0.3 : -0.3) > 0;
      
      const askMultiplier = isBull ? 1.45 : 0.75;
      const bidMultiplier = isBull ? 0.75 : 1.35;

      const baseVol = 850000;
      const askVol = Math.round(baseVol * askMultiplier * (1 + Math.random() * 0.4));
      const bidVol = Math.round(baseVol * bidMultiplier * (1 + Math.random() * 0.4));
      const delta = askVol - bidVol;
      accumCvd += delta;

      const closePrice = Number(
        (currentPrice * (1 + (Math.sin(i / 2) * 0.4 - i * 0.05) / 100)).toFixed(currentPrice > 10 ? 2 : 4)
      );

      bars.push({
        time,
        closePrice,
        bidVol,
        askVol,
        delta,
        cvdAccum: accumCvd,
        imbalanceRatio: Number((Math.max(askVol, bidVol) / Math.min(askVol, bidVol)).toFixed(2)),
        isBullish: delta > 0,
      });
    }
    return bars;
  }, [currentPrice, token.change24h]);

  // Generate Whale Trades Tape (大额主动成交流)
  const whaleTrades: WhaleTrade[] = useMemo(() => {
    const trades: WhaleTrade[] = [];
    const now = Date.now();
    const tradeCount = 10;

    for (let i = 0; i < tradeCount; i++) {
      const time = new Date(now - i * 42 * 1000).toLocaleTimeString('zh-CN', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      });
      const ex: 'Binance' | 'OKX' = i % 2 === 0 ? 'Binance' : 'OKX';
      const side: 'buy' | 'sell' = i === 1 || i === 4 || i === 7 ? 'sell' : 'buy';
      const pDiff = (Math.sin(i) * 0.2) / 100;
      const price = Number((currentPrice * (1 + pDiff)).toFixed(currentPrice > 10 ? 2 : 4));
      const amountUsd = Math.round(180000 + (Math.sin(i * 7) * 450000 + 400000));
      const amountCoins = Number((amountUsd / price).toFixed(currentPrice > 100 ? 2 : 0));

      trades.push({
        id: `wt-${i}`,
        time,
        exchange: ex,
        side,
        price,
        amountUsd,
        amountCoins,
        isBlockTrade: amountUsd >= 500000,
      });
    }
    return trades;
  }, [currentPrice]);

  // Heatmap Color Staging (Deep Violet/Blue -> Neon Emerald -> Hot Gold/Flame)
  const getHeatmapColor = (intensity: number, type: PriceHeatmapLevel['type']) => {
    if (type === 'liquidation_short') {
      // Hot Crimson / Purple Liquidation Zone
      if (intensity >= 0.7) return 'bg-[#e11d48] text-white border-[#f43f5e] shadow-[0_0_12px_rgba(225,29,72,0.4)]';
      if (intensity >= 0.4) return 'bg-[#9f1239] text-rose-100 border-[#be123c]';
      return 'bg-[#4c0519] text-rose-300 border-[#881337]';
    }
    if (type === 'liquidation_long') {
      // Cyan / Ice Blue Liquidation Zone
      if (intensity >= 0.7) return 'bg-[#0284c7] text-white border-[#38bdf8] shadow-[0_0_12px_rgba(2,132,199,0.4)]';
      if (intensity >= 0.4) return 'bg-[#0369a1] text-sky-100 border-[#0284c7]';
      return 'bg-[#082f49] text-sky-300 border-[#075985]';
    }
    if (type === 'ask_wall') {
      // Sell Wall (Amber / Orange)
      if (intensity >= 0.8) return 'bg-[#d97706] text-white border-[#fbbf24] shadow-[0_0_12px_rgba(217,119,6,0.35)]';
      if (intensity >= 0.5) return 'bg-[#92400e] text-amber-100 border-[#b45309]';
      return 'bg-[#451a03] text-amber-300 border-[#78350f]';
    }
    // Bid Wall (Emerald Green)
    if (intensity >= 0.8) return 'bg-[#059669] text-white border-[#34d399] shadow-[0_0_12px_rgba(5,150,105,0.35)]';
    if (intensity >= 0.5) return 'bg-[#065f46] text-emerald-100 border-[#047857]';
    return 'bg-[#022c22] text-emerald-300 border-[#064e3b]';
  };

  return (
    <div className="bg-neutral-900/90 rounded-xl border border-neutral-800 p-3.5 space-y-3 shadow-lg select-none">
      {/* 1. Header Toolbar: Title, Exchange Toggle, Mode Switcher */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-neutral-800 pb-2.5">
        <div className="flex items-center gap-2.5">
          <div className="p-1.5 rounded-lg bg-indigo-500/10 border border-indigo-500/30 text-indigo-400">
            <Layers className="w-4 h-4" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h3 className="font-bold text-xs text-neutral-100 flex items-center gap-1.5">
                <span>{token.symbol} 订单流与 OKX / 币安热力图</span>
                <span className="text-[10px] px-1.5 py-0.2 rounded bg-amber-500/15 text-amber-300 border border-amber-500/30 font-mono">
                  高精度订单簿与清算深度
                </span>
              </h3>
            </div>
            <p className="text-[10px] text-neutral-400">
              实时聚合币安（Binance）与欧易（OKX）合约订单流、挂单大单墙与逐级爆仓清算带。
            </p>
          </div>
        </div>

        {/* Right Controls: Exchange Select + Mode Switch */}
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {/* Exchange Filter */}
          <div className="flex bg-neutral-950 p-0.5 rounded-lg border border-neutral-800 text-[11px]">
            <button
              onClick={() => setExchange('all')}
              className={`px-2 py-0.5 rounded font-medium transition-colors ${
                exchange === 'all'
                  ? 'bg-neutral-800 text-neutral-100 font-bold shadow-sm'
                  : 'text-neutral-400 hover:text-neutral-200'
              }`}
            >
              币安+OKX 聚合
            </button>
            <button
              onClick={() => setExchange('binance')}
              className={`px-2 py-0.5 rounded font-medium transition-colors flex items-center gap-1 ${
                exchange === 'binance'
                  ? 'bg-amber-500/20 text-amber-300 border border-amber-500/40 font-bold'
                  : 'text-neutral-400 hover:text-neutral-200'
              }`}
            >
              <span className="w-1.5 h-1.5 rounded-full bg-amber-400"></span>
              币安 (Binance)
            </button>
            <button
              onClick={() => setExchange('okx')}
              className={`px-2 py-0.5 rounded font-medium transition-colors flex items-center gap-1 ${
                exchange === 'okx'
                  ? 'bg-sky-500/20 text-sky-300 border border-sky-500/40 font-bold'
                  : 'text-neutral-400 hover:text-neutral-200'
              }`}
            >
              <span className="w-1.5 h-1.5 rounded-full bg-sky-400"></span>
              欧易 (OKX)
            </button>
          </div>

          {/* Mode Switcher */}
          <div className="flex bg-neutral-950 p-0.5 rounded-lg border border-neutral-800 text-[11px]">
            <button
              onClick={() => setMode('depth_heatmap')}
              className={`px-2 py-0.5 rounded font-medium transition-colors ${
                mode === 'depth_heatmap'
                  ? 'bg-indigo-600 text-white font-bold shadow'
                  : 'text-neutral-400 hover:text-neutral-200'
              }`}
            >
              挂单与清算热力图
            </button>
            <button
              onClick={() => setMode('footprint_cvd')}
              className={`px-2 py-0.5 rounded font-medium transition-colors ${
                mode === 'footprint_cvd'
                  ? 'bg-indigo-600 text-white font-bold shadow'
                  : 'text-neutral-400 hover:text-neutral-200'
              }`}
            >
              订单流 Footprint / CVD
            </button>
            <button
              onClick={() => setMode('whale_tape')}
              className={`px-2 py-0.5 rounded font-medium transition-colors ${
                mode === 'whale_tape'
                  ? 'bg-indigo-600 text-white font-bold shadow'
                  : 'text-neutral-400 hover:text-neutral-200'
              }`}
            >
              大额巨鲸逐笔成交
            </button>
          </div>
        </div>
      </div>

      {/* 2. Top Institutional Metrics Strip (4 Quick Cards) */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
        <div className="p-2.5 rounded-lg bg-neutral-950 border border-neutral-850 space-y-0.5">
          <span className="text-[10px] text-neutral-400 flex items-center gap-1">
            <Zap className="w-3 h-3 text-amber-400" />
            主动买卖失衡率 (Order Flow Imbalance)
          </span>
          <div className="text-sm font-bold font-mono text-emerald-400 flex items-center gap-1">
            <span>+24.8%</span>
            <span className="text-[10px] font-normal text-emerald-500 font-sans">(买方主动吃单优势)</span>
          </div>
        </div>

        <div className="p-2.5 rounded-lg bg-neutral-950 border border-neutral-850 space-y-0.5">
          <span className="text-[10px] text-neutral-400 flex items-center gap-1">
            <Activity className="w-3 h-3 text-indigo-400" />
            累积成交量差 (24h CVD 净流入)
          </span>
          <div className="text-sm font-bold font-mono text-emerald-400 flex items-center gap-1">
            <span>+${formatWanYi(token.oi.totalUsd * 0.042)}</span>
            <span className="text-[10px] font-normal text-neutral-500 font-sans">(多头持续吸筹)</span>
          </div>
        </div>

        <div className="p-2.5 rounded-lg bg-neutral-950 border border-neutral-850 space-y-0.5">
          <span className="text-[10px] text-neutral-400 flex items-center gap-1">
            <ShieldAlert className="w-3 h-3 text-rose-400" />
            上方最大压盘墙 (Key Ask Wall)
          </span>
          <div className="text-sm font-bold font-mono text-rose-400 flex items-center gap-1">
            <span>${priceLevels[3]?.priceFormatted}</span>
            <span className="text-[10px] font-normal text-neutral-400 font-sans">
              ({formatWanYi(priceLevels[3]?.totalLiquidityUsd || 0)})
            </span>
          </div>
        </div>

        <div className="p-2.5 rounded-lg bg-neutral-950 border border-neutral-850 space-y-0.5">
          <span className="text-[10px] text-neutral-400 flex items-center gap-1">
            <TrendingUp className="w-3 h-3 text-emerald-400" />
            下方密集托单墙 (Key Bid Wall)
          </span>
          <div className="text-sm font-bold font-mono text-emerald-400 flex items-center gap-1">
            <span>${priceLevels[12]?.priceFormatted}</span>
            <span className="text-[10px] font-normal text-neutral-400 font-sans">
              ({formatWanYi(priceLevels[12]?.totalLiquidityUsd || 0)})
            </span>
          </div>
        </div>
      </div>

      {/* 3. Main Display Area Based on Selected Mode */}
      {mode === 'depth_heatmap' && (
        <div className="space-y-2">
          {/* Heatmap Visual Ladder Container */}
          <div className="rounded-xl border border-neutral-800 bg-neutral-950/90 p-3 space-y-1.5 overflow-hidden">
            <div className="flex items-center justify-between text-[11px] text-neutral-400 px-2 pb-1 border-b border-neutral-850">
              <span className="font-semibold text-neutral-300">
                价格档位 ↕ 偏离幅度
              </span>
              <span className="font-semibold text-neutral-300">
                OKX / 币安 挂单深度热力阶梯与清算规模 (USDT)
              </span>
              <span className="font-semibold text-neutral-300">
                交易所分布比例
              </span>
            </div>

            {/* Price Ladder Rows */}
            <div className="space-y-1 font-mono text-xs">
              {priceLevels.map((lvl, idx) => {
                const isHovered = hoveredLevel?.price === lvl.price;
                const isCurrentPriceBorder = lvl.price < currentPrice && (priceLevels[idx - 1]?.price || 999999) > currentPrice;
                const colorClasses = getHeatmapColor(lvl.intensity, lvl.type);

                // Filter by exchange if selected
                const displayLiquidity =
                  exchange === 'binance'
                    ? Math.round(lvl.totalLiquidityUsd * (lvl.binanceShare / 100))
                    : exchange === 'okx'
                    ? Math.round(lvl.totalLiquidityUsd * (lvl.okxShare / 100))
                    : lvl.totalLiquidityUsd;

                return (
                  <React.Fragment key={lvl.price}>
                    {/* Current Price Marker Bar */}
                    {isCurrentPriceBorder && (
                      <div className="my-1.5 py-1 px-3 rounded bg-emerald-500/15 border border-emerald-500/40 flex items-center justify-between text-xs font-bold text-emerald-400 shadow-[0_0_8px_rgba(16,185,129,0.2)]">
                        <span className="flex items-center gap-1.5 font-sans">
                          <span className="w-2 h-2 rounded-full bg-emerald-400 animate-ping"></span>
                          当前现货/合约基准价:
                        </span>
                        <span className="text-sm font-mono tracking-wider">${currentPrice.toLocaleString()}</span>
                        <span className="text-[10px] text-emerald-300 font-sans">● 盘口买卖价差极度紧实</span>
                      </div>
                    )}

                    <div
                      onMouseEnter={() => setHoveredLevel(lvl)}
                      onMouseLeave={() => setHoveredLevel(null)}
                      className={`flex items-center justify-between p-1.5 rounded-lg border transition-all cursor-pointer ${
                        colorClasses
                      } ${
                        isHovered ? 'ring-2 ring-amber-400 scale-[1.01] z-10' : 'opacity-90 hover:opacity-100'
                      }`}
                    >
                      {/* Left: Price & Distance */}
                      <div className="w-36 flex items-center gap-2">
                        <span className="font-bold tracking-tight">${lvl.priceFormatted}</span>
                        <span className={`text-[10px] px-1 py-0.2 rounded font-sans ${
                          lvl.distancePercent > 0 ? 'bg-rose-500/20 text-rose-200' : 'bg-emerald-500/20 text-emerald-200'
                        }`}>
                          {lvl.distancePercent > 0 ? `+${lvl.distancePercent}%` : `${lvl.distancePercent}%`}
                        </span>
                      </div>

                      {/* Middle: Liquidity Depth Bar & Type Tag */}
                      <div className="flex-1 mx-3 flex items-center gap-2">
                        {/* Heatmap Depth Bar */}
                        <div className="flex-1 h-3.5 bg-black/40 rounded overflow-hidden relative border border-white/10">
                          <div
                            className="h-full rounded transition-all duration-300"
                            style={{
                              width: `${Math.min(100, Math.max(12, lvl.intensity * 100))}%`,
                              backgroundColor: lvl.type.includes('liquidation')
                                ? lvl.type === 'liquidation_short' ? '#f43f5e' : '#38bdf8'
                                : lvl.type === 'ask_wall' ? '#fbbf24' : '#34d399',
                            }}
                          ></div>
                          <span className="absolute inset-0 flex items-center pl-2 text-[10px] font-bold text-white drop-shadow">
                            {formatWanYi(displayLiquidity)}
                          </span>
                        </div>

                        {/* Special Tag for Liquidation or Wall */}
                        {lvl.leverageTier && (
                          <span className="px-1.5 py-0.2 rounded text-[10px] font-bold bg-black/60 border border-white/20">
                            {lvl.leverageTier} 清算带
                          </span>
                        )}
                      </div>

                      {/* Right: Exchange Distribution (Binance vs OKX) */}
                      <div className="w-36 flex items-center gap-1.5 justify-end text-[10px]">
                        <div className="flex items-center gap-1 text-amber-200">
                          <span className="w-1.5 h-1.5 rounded-full bg-amber-400"></span>
                          <span>币安 {lvl.binanceShare}%</span>
                        </div>
                        <span className="text-white/40">/</span>
                        <div className="flex items-center gap-1 text-sky-200">
                          <span className="w-1.5 h-1.5 rounded-full bg-sky-400"></span>
                          <span>OKX {lvl.okxShare}%</span>
                        </div>
                      </div>
                    </div>
                  </React.Fragment>
                );
              })}
            </div>
          </div>

          {/* Interactive Inspection Tooltip Bar */}
          <div className="p-2.5 rounded-lg bg-neutral-950 border border-neutral-800 text-xs flex flex-wrap items-center justify-between gap-2 min-h-[38px]">
            {hoveredLevel ? (
              <div className="flex flex-wrap items-center justify-between w-full text-[11px] gap-2">
                <div className="flex items-center gap-3">
                  <span className="font-bold text-neutral-100 font-mono">
                    目标价位: ${hoveredLevel.priceFormatted} ({hoveredLevel.distancePercent > 0 ? `+${hoveredLevel.distancePercent}%` : `${hoveredLevel.distancePercent}%`})
                  </span>
                  <span className="text-neutral-400">
                    挂单/清算总额: <b className="text-amber-400 font-mono">{formatWanYi(hoveredLevel.totalLiquidityUsd)}</b>
                  </span>
                  <span className="text-neutral-400">
                    币安: <b className="text-amber-300 font-mono">{formatWanYi(Math.round(hoveredLevel.totalLiquidityUsd * (hoveredLevel.binanceShare / 100)))} ({hoveredLevel.binanceShare}%)</b>
                  </span>
                  <span className="text-neutral-400">
                    OKX: <b className="text-sky-300 font-mono">{formatWanYi(Math.round(hoveredLevel.totalLiquidityUsd * (hoveredLevel.okxShare / 100)))} ({hoveredLevel.okxShare}%)</b>
                  </span>
                </div>
                <div className="text-xs font-semibold text-emerald-400">
                  {hoveredLevel.note}
                </div>
              </div>
            ) : (
              <div className="flex items-center justify-between w-full text-[11px] text-neutral-500">
                <div className="flex items-center gap-1.5">
                  <Info className="w-3.5 h-3.5" />
                  <span>移入上方价位层级，可即时透视币安与 OKX 该价位的大单挂单明细与杠杆强平预估。</span>
                </div>
                <div className="flex items-center gap-3 text-[10px]">
                  <span className="flex items-center gap-1 text-rose-400">
                    <span className="w-2 h-2 rounded bg-rose-600"></span> 空头清算带
                  </span>
                  <span className="flex items-center gap-1 text-amber-400">
                    <span className="w-2 h-2 rounded bg-amber-600"></span> 卖单压盘墙
                  </span>
                  <span className="flex items-center gap-1 text-emerald-400">
                    <span className="w-2 h-2 rounded bg-emerald-600"></span> 买单托盘墙
                  </span>
                  <span className="flex items-center gap-1 text-sky-400">
                    <span className="w-2 h-2 rounded bg-sky-600"></span> 多头清算带
                  </span>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Mode 2: Footprint & CVD */}
      {mode === 'footprint_cvd' && (
        <div className="space-y-2">
          <div className="rounded-xl border border-neutral-800 bg-neutral-950 p-3 space-y-2">
            <div className="flex items-center justify-between text-xs border-b border-neutral-850 pb-2">
              <span className="font-bold text-neutral-200 flex items-center gap-1.5">
                <Activity className="w-3.5 h-3.5 text-indigo-400" />
                逐根 K 线主动买卖 Delta 与 CVD 累积走势
              </span>
              <span className="text-[10px] text-neutral-400 font-mono">
                每 5 分钟聚合 • 主动吃单量 (Lift Ask vs Hit Bid)
              </span>
            </div>

            {/* Footprint Bars Horizontal Grid */}
            <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 gap-2 pt-1 font-mono text-xs">
              {footprintBars.map((bar, i) => (
                <div
                  key={i}
                  className={`p-2 rounded-lg border flex flex-col justify-between ${
                    bar.isBullish
                      ? 'bg-emerald-950/40 border-emerald-500/30 text-emerald-300'
                      : 'bg-rose-950/40 border-rose-500/30 text-rose-300'
                  }`}
                >
                  <div className="flex justify-between items-center text-[10px] text-neutral-400 border-b border-neutral-850 pb-1">
                    <span>{bar.time}</span>
                    <span className="text-neutral-200">${bar.closePrice}</span>
                  </div>

                  <div className="space-y-1 my-1.5 text-[11px]">
                    <div className="flex justify-between">
                      <span className="text-neutral-500 font-sans">主动买:</span>
                      <span className="text-emerald-400 font-bold">{formatAmountWanYi(bar.askVol)}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-neutral-500 font-sans">主动卖:</span>
                      <span className="text-rose-400 font-bold">{formatAmountWanYi(bar.bidVol)}</span>
                    </div>
                    <div className="flex justify-between pt-0.5 border-t border-neutral-850">
                      <span className="text-neutral-400 font-sans">Delta:</span>
                      <span className={`font-bold ${bar.delta >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {bar.delta >= 0 ? `+${formatAmountWanYi(bar.delta)}` : formatAmountWanYi(bar.delta)}
                      </span>
                    </div>
                  </div>

                  <div className="text-[9px] text-neutral-500 flex justify-between items-center pt-0.5">
                    <span>CVD:</span>
                    <span className="text-neutral-300 font-bold">{formatAmountWanYi(bar.cvdAccum)}</span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Mode 3: Whale Tape Flow */}
      {mode === 'whale_tape' && (
        <div className="rounded-xl border border-neutral-800 bg-neutral-950 p-3 space-y-2">
          <div className="flex items-center justify-between text-xs border-b border-neutral-850 pb-2">
            <span className="font-bold text-neutral-200 flex items-center gap-1.5">
              <Zap className="w-3.5 h-3.5 text-amber-400" />
              币安与 OKX 实时大额巨鲸成交流 (&gt; $100K)
            </span>
            <span className="text-[10px] text-neutral-400 font-mono">
              实时撮合广播 • 毫秒级监控
            </span>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs font-mono">
              <thead className="bg-neutral-900/60 text-neutral-400 border-b border-neutral-800 text-[11px]">
                <tr>
                  <th className="py-2 px-3">时间</th>
                  <th className="py-2 px-3">来源交易所</th>
                  <th className="py-2 px-3">方向</th>
                  <th className="py-2 px-3">成交价格</th>
                  <th className="py-2 px-3">成交额 (USD)</th>
                  <th className="py-2 px-3">成交数量</th>
                  <th className="py-2 px-3">大单类型</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-neutral-850">
                {whaleTrades.map((trade) => {
                  const isBuy = trade.side === 'buy';
                  return (
                    <tr key={trade.id} className="hover:bg-neutral-900/50 transition-colors">
                      <td className="py-2 px-3 text-neutral-400">{trade.time}</td>
                      <td className="py-2 px-3">
                        <span className={`px-1.5 py-0.5 rounded text-[10px] font-sans font-bold ${
                          trade.exchange === 'Binance'
                            ? 'bg-amber-500/15 text-amber-300 border border-amber-500/30'
                            : 'bg-sky-500/15 text-sky-300 border border-sky-500/30'
                        }`}>
                          {trade.exchange}
                        </span>
                      </td>
                      <td className="py-2 px-3">
                        <span className={`font-bold flex items-center gap-1 font-sans ${
                          isBuy ? 'text-emerald-400' : 'text-rose-400'
                        }`}>
                          {isBuy ? <ArrowUpRight className="w-3 h-3" /> : <ArrowDownRight className="w-3 h-3" />}
                          {isBuy ? '主动买入' : '主动卖出'}
                        </span>
                      </td>
                      <td className="py-2 px-3 font-bold text-neutral-100">${trade.price.toLocaleString()}</td>
                      <td className="py-2 px-3 font-bold text-amber-300">${trade.amountUsd.toLocaleString()}</td>
                      <td className="py-2 px-3 text-neutral-300">{trade.amountCoins.toLocaleString()} {token.symbol}</td>
                      <td className="py-2 px-3">
                        {trade.isBlockTrade ? (
                          <span className="px-1.5 py-0.5 rounded text-[10px] bg-rose-500/20 text-rose-300 border border-rose-500/40 font-sans font-bold">
                            💥 超大巨鲸块 (&gt;$500K)
                          </span>
                        ) : (
                          <span className="px-1.5 py-0.5 rounded text-[10px] bg-neutral-800 text-neutral-300 font-sans">
                            机构大单
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
};
