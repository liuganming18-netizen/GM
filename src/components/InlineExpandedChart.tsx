/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useMemo } from 'react';
import { TokenMarketItem, Timeframe, CryptoSignal } from '../types/crypto';
import { generateOIPointsForCandles } from '../mock/cryptoData';
import { TokenIcon } from './TokenIcon';
import { OIHeatmap } from './OIHeatmap';
import { OrderFlowHeatmap } from './OrderFlowHeatmap';
import { formatWanYi, formatAmountWanYi } from '../utils/formatters';
import {
  TrendingUp,
  BarChart2,
  Sliders,
  Layers,
  Info,
  Maximize2,
  ChevronDown,
  Calendar,
  AlertCircle,
  HelpCircle,
  Clock
} from 'lucide-react';

interface InlineExpandedChartProps {
  token: TokenMarketItem;
  activeSignal?: CryptoSignal;
  onClose?: () => void;
}

export const InlineExpandedChart: React.FC<InlineExpandedChartProps> = ({
  token,
  activeSignal,
  onClose,
}) => {
  const [selectedTf, setSelectedTf] = useState<Timeframe>('1m');
  const [oiUnit, setOiUnit] = useState<'amount' | 'usd'>('usd');
  const [oiDisplayMode, setOiDisplayMode] = useState<'split' | 'ratio' | 'total'>('split');
  const [hoveredCandleIndex, setHoveredCandleIndex] = useState<number | null>(null);
  const [analysisTab, setAnalysisTab] = useState<'orderflow' | 'oi_heatmap' | 'both'>('orderflow');

  const timeframes: Timeframe[] = ['1m', '5m', '15m', '30m', '1h', '4h', '8h', '12h', '1d'];
  const candles = token.candles[selectedTf] || token.candles['1m'];

  // Dynamically synchronize the OI histogram bars with the selected K-line timeframe and candles
  const oiPoints = useMemo(() => {
    if (token.oiHistory && !Array.isArray(token.oiHistory) && token.oiHistory[selectedTf]) {
      return token.oiHistory[selectedTf];
    }
    const baseOI = token.oi.totalUsd / 66.5;
    return generateOIPointsForCandles(baseOI, candles);
  }, [token, selectedTf, candles]);

  const currentHovered = hoveredCandleIndex !== null ? candles[hoveredCandleIndex] : candles[candles.length - 1];
  const currentHoveredOi = hoveredCandleIndex !== null && oiPoints[hoveredCandleIndex]
    ? oiPoints[hoveredCandleIndex]
    : oiPoints[oiPoints.length - 1];

  // Min and Max for K-line price scaling
  const minPrice = Math.min(...candles.map((c) => c.low));
  const maxPrice = Math.max(...candles.map((c) => c.high));
  const priceRange = maxPrice - minPrice || 1;

  // Max volume for volume subplot scaling
  const maxVol = Math.max(...candles.map((c) => c.volume));

  // Max side for Upper-Short / Lower-Long bidirectional histogram
  const maxSideVal = Math.max(
    ...oiPoints.map((p) =>
      Math.max(
        oiUnit === 'usd' ? p.shortOiUsd : p.shortOiAmount,
        oiUnit === 'usd' ? p.longOiUsd : p.longOiAmount
      )
    )
  ) || 1;

  return (
    <div className="bg-neutral-950 border border-neutral-700/80 rounded-xl overflow-hidden p-4 space-y-4 my-2 shadow-2xl transition-all">
      {/* Title & Timeframe Row */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-neutral-800 pb-2.5">
        <div className="flex items-center gap-2 px-2.5 py-1 rounded-lg bg-neutral-900 border border-neutral-800">
          <TokenIcon symbol={token.symbol} size="xs" />
          <span className="font-bold text-neutral-100 font-mono text-xs">{token.symbol} / USDT</span>
          <span className="text-[10px] text-neutral-400 hidden sm:inline">({token.fullName})</span>
          <span className="text-neutral-600">|</span>
          <span className="text-[11px] text-neutral-400 font-sans flex items-center gap-1">
            <span className="text-neutral-500">来源交易所:</span>
            <span className="font-semibold text-neutral-200 font-mono">{token.exchange || 'Binance'}</span>
          </span>
        </div>

        {/* Timeframe Selector: 1m / 5m / 15m / 30m / 1h / 4h / 8h / 12h / 1d */}
        <div className="flex items-center gap-1 bg-neutral-900 p-1 rounded-lg border border-neutral-800 text-xs">
          {timeframes.map((tf) => (
            <button
              key={tf}
              onClick={() => setSelectedTf(tf)}
              className={`px-2 py-0.5 rounded font-mono font-medium transition-colors ${
                selectedTf === tf
                  ? 'bg-emerald-600 text-white font-bold shadow'
                  : 'text-neutral-400 hover:text-neutral-200'
              }`}
            >
              {tf}
            </button>
          ))}
        </div>
      </div>

      {/* Candlestick & Volume Subplot Area (Full-Width SVG Canvas) */}
      <div className="space-y-2">
        {/* Canvas Toolbar & Ticker Stats */}
        <div className="flex flex-wrap items-center justify-between text-xs text-neutral-400 gap-2 px-1">
          <div className="flex items-center gap-4 font-mono text-[11px]">
            <span>开: <span className="text-neutral-200">{currentHovered?.open}</span></span>
            <span>高: <span className="text-neutral-200">{currentHovered?.high}</span></span>
            <span>低: <span className="text-neutral-200">{currentHovered?.low}</span></span>
            <span>收: <span className={currentHovered?.close >= currentHovered?.open ? 'text-emerald-400' : 'text-rose-400'}>
              {currentHovered?.close}
            </span></span>
            <span>量: <span className="text-neutral-200">{formatAmountWanYi(currentHovered?.volume)} 币</span></span>
            {currentHovered?.signalTag && (
              <span className="px-1.5 py-0.2 rounded bg-indigo-500/20 text-indigo-300 border border-indigo-500/30">
                {currentHovered.signalTag}
              </span>
            )}
          </div>
        </div>

        {/* Candlestick SVG Container */}
        <div className="relative w-full h-56 bg-neutral-900/90 rounded-lg border border-neutral-800 p-2 overflow-hidden select-none">
          <svg
            className="w-full h-full overflow-visible"
            viewBox="0 0 1000 200"
            preserveAspectRatio="none"
          >
            {/* Gridlines */}
            <line x1="0" y1="50" x2="1000" y2="50" stroke="#262626" strokeDasharray="3 3" />
            <line x1="0" y1="100" x2="1000" y2="100" stroke="#262626" strokeDasharray="3 3" />
            <line x1="0" y1="150" x2="1000" y2="150" stroke="#262626" strokeDasharray="3 3" />

            {/* Candlesticks & Volume Bars */}
            {candles.map((candle, idx) => {
              const count = candles.length;
              const barWidth = 1000 / count;
              const x = idx * barWidth + barWidth / 2;
              const isGreen = candle.close >= candle.open;

              // Price Y coords (0 - 140 height range for candles)
              const highY = 140 - ((candle.high - minPrice) / priceRange) * 130;
              const lowY = 140 - ((candle.low - minPrice) / priceRange) * 130;
              const openY = 140 - ((candle.open - minPrice) / priceRange) * 130;
              const closeY = 140 - ((candle.close - minPrice) / priceRange) * 130;

              const bodyTop = Math.min(openY, closeY);
              const bodyHeight = Math.max(Math.abs(closeY - openY), 2);

              // Volume bar Y coords (150 - 195 height range for volume)
              const volHeight = (candle.volume / maxVol) * 45;
              const volY = 195 - volHeight;

              return (
                <g
                  key={idx}
                  onMouseEnter={() => setHoveredCandleIndex(idx)}
                  className="cursor-pointer group"
                >
                  {/* Candlestick Wick */}
                  <line
                    x1={x}
                    y1={highY}
                    x2={x}
                    y2={lowY}
                    stroke={isGreen ? '#168a57' : '#cc334d'}
                    strokeWidth="1.5"
                    strokeDasharray={candle.isClosed ? 'none' : '3 2'}
                  />

                  {/* Candlestick Body */}
                  <rect
                    x={x - barWidth * 0.35}
                    y={bodyTop}
                    width={barWidth * 0.7}
                    height={bodyHeight}
                    fill={isGreen ? '#168a57' : '#cc334d'}
                    stroke={isGreen ? '#168a57' : '#cc334d'}
                    strokeWidth="1"
                    strokeDasharray={candle.isClosed ? 'none' : '3 2'}
                    opacity={candle.isClosed ? 1 : 0.85}
                  />

                  {/* Volume Subplot Bar */}
                  <rect
                    x={x - barWidth * 0.3}
                    y={volY}
                    width={barWidth * 0.6}
                    height={volHeight}
                    fill={isGreen ? '#168a57' : '#cc334d'}
                    opacity={0.4}
                  />

                  {/* Signal Trigger Badge */}
                  {candle.signalTag && (
                    <g transform={`translate(${x}, ${highY - 12})`}>
                      <circle r="4" fill="#6366f1" />
                      <line x1="0" y1="0" x2="0" y2="12" stroke="#6366f1" strokeWidth="1" />
                    </g>
                  )}
                </g>
              );
            })}
          </svg>

          {/* Annotations Note matching sketch */}
          <div className="absolute bottom-1 left-2 text-[10px] text-neutral-500 font-mono">
            区间边界 / 突破 / 独立回踩 / 止损目标标注；点击真实信号触发点 → 查看规则条件
          </div>
          <div className="absolute bottom-1 right-2 text-[10px] text-neutral-500 font-mono">
            1分钟滚动10天｜5分钟导入1年｜15分钟导入2年｜30分钟及以上全部可得历史
          </div>
        </div>
      </div>

      {/* Analytics Heatmap Section */}
      <div className="space-y-2">
        {/* Sub-tab Switcher: 订单流与OK/币安热力图 | OI逐小时变化率热力图 */}
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-neutral-850 pb-1.5 text-xs">
          <div className="flex items-center gap-1.5 bg-neutral-900/90 p-1 rounded-lg border border-neutral-800">
            <button
              onClick={() => setAnalysisTab('orderflow')}
              className={`px-3 py-1 rounded-md font-bold transition-all flex items-center gap-1.5 ${
                analysisTab === 'orderflow'
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-neutral-400 hover:text-neutral-200'
              }`}
            >
              <Layers className="w-3.5 h-3.5" />
              <span>订单流与OKX / 币安热力图</span>
            </button>
            <button
              onClick={() => setAnalysisTab('oi_heatmap')}
              className={`px-3 py-1 rounded-md font-bold transition-all flex items-center gap-1.5 ${
                analysisTab === 'oi_heatmap'
                  ? 'bg-amber-600 text-white shadow-sm'
                  : 'text-neutral-400 hover:text-neutral-200'
              }`}
            >
              <Flame className="w-3.5 h-3.5" />
              <span>OI逐小时变化率热力图</span>
            </button>
            <button
              onClick={() => setAnalysisTab('both')}
              className={`px-2.5 py-1 rounded-md text-[11px] transition-all ${
                analysisTab === 'both'
                  ? 'bg-neutral-800 text-neutral-100 font-bold border border-neutral-700'
                  : 'text-neutral-500 hover:text-neutral-300'
              }`}
            >
              全部展开
            </button>
          </div>

          <span className="text-[10px] text-neutral-500 font-mono">
            {analysisTab === 'orderflow'
              ? 'OKX + Binance 实时聚合挂单与清算深度'
              : analysisTab === 'oi_heatmap'
              ? '近24小时逐小时机构增减仓追踪'
              : '双热力图并列透视'}
          </span>
        </div>

        {/* 1. 订单流与OKX / 币安热力图 */}
        {(analysisTab === 'orderflow' || analysisTab === 'both') && (
          <OrderFlowHeatmap token={token} />
        )}

        {/* 2. OI (持仓量) 逐小时变化率热力图 */}
        {(analysisTab === 'oi_heatmap' || analysisTab === 'both') && (
          <OIHeatmap token={token} />
        )}
      </div>

      {/* OI Section: OI多空持仓变化 */}
      <div className="space-y-1.5 pt-1">
        {/* OI Header Toolbar */}
        <div className="flex flex-wrap items-center justify-between text-xs gap-2">
          <div className="flex items-center gap-3">
            <span className="font-bold text-neutral-100 flex items-center gap-1.5">
              <span className="w-2.5 h-2.5 rounded-sm bg-gradient-to-b from-[#f6465d] to-[#0ecb81]"></span>
              OI多空持仓变化
              <span className="px-1.5 py-0.2 rounded bg-neutral-800 text-neutral-300 border border-neutral-700 font-mono text-[10px]">
                {selectedTf}
              </span>
            </span>

            {/* Live HUD */}
            <div className="flex items-center gap-3 font-mono text-[11px] bg-neutral-900/90 px-2.5 py-0.5 rounded border border-neutral-800">
              <span className="text-neutral-400">时间: {currentHoveredOi?.time}</span>
              
              {/* Short (Upper) */}
              <span className="text-[#f6465d] flex items-center gap-1 font-semibold">
                <span>空:</span>
                <b>
                  {oiUnit === 'usd'
                    ? formatWanYi(currentHoveredOi.shortOiUsd)
                    : `${formatAmountWanYi(currentHoveredOi.shortOiAmount)} 币`}
                </b>
                <span className="text-[10px] opacity-80">({currentHoveredOi.shortRatio}%)</span>
              </span>

              <span className="text-neutral-600">|</span>

              {/* Long (Lower) */}
              <span className="text-[#0ecb81] flex items-center gap-1 font-semibold">
                <span>多:</span>
                <b>
                  {oiUnit === 'usd'
                    ? formatWanYi(currentHoveredOi.longOiUsd)
                    : `${formatAmountWanYi(currentHoveredOi.longOiAmount)} 币`}
                </b>
                <span className="text-[10px] opacity-80">({currentHoveredOi.longRatio}%)</span>
              </span>

              <span className="text-neutral-600">|</span>

              {/* Long/Short Ratio */}
              <span className="text-amber-300 font-bold">
                多空比: {(currentHoveredOi.longRatio / (currentHoveredOi.shortRatio || 1)).toFixed(2)}
              </span>
            </div>
          </div>

          <div className="flex items-center gap-2">
            {/* Mode Switcher */}
            <div className="flex bg-neutral-900 rounded p-0.5 border border-neutral-800 text-[10px]">
              <button
                onClick={() => setOiDisplayMode('split')}
                className={`px-2 py-0.5 rounded transition-colors ${
                  oiDisplayMode === 'split'
                    ? 'bg-neutral-800 text-emerald-400 font-bold shadow-sm'
                    : 'text-neutral-400 hover:text-neutral-200'
                }`}
              >
                分立柱
              </button>
              <button
                onClick={() => setOiDisplayMode('ratio')}
                className={`px-2 py-0.5 rounded transition-colors ${
                  oiDisplayMode === 'ratio'
                    ? 'bg-neutral-800 text-emerald-400 font-bold shadow-sm'
                    : 'text-neutral-400 hover:text-neutral-200'
                }`}
              >
                占比堆叠
              </button>
            </div>

            {/* Unit Switcher */}
            <div className="flex bg-neutral-900 rounded p-0.5 border border-neutral-800 text-[10px]">
              <button
                onClick={() => setOiUnit('usd')}
                className={`px-2 py-0.5 rounded ${
                  oiUnit === 'usd' ? 'bg-neutral-800 text-neutral-100 font-bold' : 'text-neutral-400'
                }`}
              >
                名义额
              </button>
              <button
                onClick={() => setOiUnit('amount')}
                className={`px-2 py-0.5 rounded ${
                  oiUnit === 'amount' ? 'bg-neutral-800 text-neutral-100 font-bold' : 'text-neutral-400'
                }`}
              >
                数量
              </button>
            </div>
          </div>
        </div>

        {/* Upper-Short / Lower-Long Bidirectional Histogram SVG */}
        <div className="w-full h-32 bg-neutral-900/90 rounded-lg border border-neutral-800 p-2 relative overflow-hidden select-none">
          <svg className="w-full h-full overflow-visible" viewBox="0 0 1000 110" preserveAspectRatio="none">
            {/* Background Gridlines */}
            <line x1="0" y1="20" x2="1000" y2="20" stroke="#1f1f1f" strokeDasharray="3 3" />
            <line x1="0" y1="90" x2="1000" y2="90" stroke="#1f1f1f" strokeDasharray="3 3" />

            {/* Central Zero Baseline (分界中轴线) */}
            <line x1="0" y1="55" x2="1000" y2="55" stroke="#404040" strokeWidth="1.5" />

            {/* Mode 1: Split Bidirectional Histogram (上空红色向下生长到中轴，下多绿色从中轴向下生长) */}
            {oiDisplayMode === 'split' &&
              oiPoints.map((point, idx) => {
                const count = oiPoints.length;
                const barWidth = 1000 / count;
                const x = idx * barWidth + barWidth * 0.15;
                const isHovered = hoveredCandleIndex === idx;

                const shortVal = oiUnit === 'usd' ? point.shortOiUsd : point.shortOiAmount;
                const longVal = oiUnit === 'usd' ? point.longOiUsd : point.longOiAmount;

                // Center line is y = 55
                const zeroY = 55;

                // Upper Short Bar: grows upward from zeroY to (zeroY - shortHeight)
                const shortHeight = Math.max((shortVal / maxSideVal) * 46, 3);
                const shortY = zeroY - shortHeight;

                // Lower Long Bar: grows downward from zeroY to (zeroY + longHeight)
                const longHeight = Math.max((longVal / maxSideVal) * 46, 3);
                const longY = zeroY;

                return (
                  <g
                    key={idx}
                    onMouseEnter={() => setHoveredCandleIndex(idx)}
                    className="cursor-pointer group"
                  >
                    {/* Upper Short Bar (红色空头) */}
                    <rect
                      x={x}
                      y={shortY}
                      width={barWidth * 0.7}
                      height={shortHeight}
                      fill="#f6465d"
                      opacity={isHovered ? 1 : 0.8}
                      rx="1"
                    />

                    {/* Lower Long Bar (绿色多头) */}
                    <rect
                      x={x}
                      y={longY}
                      width={barWidth * 0.7}
                      height={longHeight}
                      fill="#0ecb81"
                      opacity={isHovered ? 1 : 0.8}
                      rx="1"
                    />

                    {/* Active vertical hover crosshair */}
                    {isHovered && (
                      <line
                        x1={x + (barWidth * 0.7) / 2}
                        y1={0}
                        x2={x + (barWidth * 0.7) / 2}
                        y2={110}
                        stroke="#ffffff"
                        strokeWidth="1"
                        strokeDasharray="2 2"
                        opacity={0.6}
                      />
                    )}
                  </g>
                );
              })}

            {/* Mode 2: 100% Ratio Stacked Histogram (Upper Short % + Lower Long %) */}
            {oiDisplayMode === 'ratio' &&
              oiPoints.map((point, idx) => {
                const count = oiPoints.length;
                const barWidth = 1000 / count;
                const x = idx * barWidth + barWidth * 0.15;
                const isHovered = hoveredCandleIndex === idx;

                const zeroY = 55;
                const shortHeight = (point.shortRatio / 100) * 48;
                const shortY = zeroY - shortHeight;
                const longHeight = (point.longRatio / 100) * 48;
                const longY = zeroY;

                return (
                  <g
                    key={idx}
                    onMouseEnter={() => setHoveredCandleIndex(idx)}
                    className="cursor-pointer group"
                  >
                    {/* Upper Short % */}
                    <rect
                      x={x}
                      y={shortY}
                      width={barWidth * 0.7}
                      height={shortHeight}
                      fill="#f6465d"
                      opacity={isHovered ? 1 : 0.85}
                      rx="1"
                    />

                    {/* Lower Long % */}
                    <rect
                      x={x}
                      y={longY}
                      width={barWidth * 0.7}
                      height={longHeight}
                      fill="#0ecb81"
                      opacity={isHovered ? 1 : 0.85}
                      rx="1"
                    />
                  </g>
                );
              })}

            {/* Net OI Difference Curve across the center */}
            <polyline
              points={oiPoints
                .map((p, idx) => {
                  const count = oiPoints.length;
                  const barWidth = 1000 / count;
                  const x = idx * barWidth + (barWidth * 0.7) / 2 + barWidth * 0.15;
                  // If long > short, bias below center (towards green), if short > long, bias above center
                  const netRatio = (p.longRatio - p.shortRatio) / 100;
                  const y = 55 + netRatio * 35;
                  return `${x},${y}`;
                })
                .join(' ')}
              fill="none"
              stroke="#fbbf24"
              strokeWidth="1.5"
              strokeDasharray="3 2"
              opacity="0.8"
            />
          </svg>
        </div>
      </div>

      {/* Bottom Dual Panels: 信号逻辑与数据详情 + 相关指标 */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3 pt-1 text-xs">
        {/* Panel 1: 信号逻辑与数据详情 (2 cols wide) */}
        <div className="md:col-span-2 p-3 bg-neutral-900/90 rounded-lg border border-neutral-800 space-y-2">
          <div className="flex items-center justify-between border-b border-neutral-800 pb-1.5">
            <span className="font-bold text-neutral-200 flex items-center gap-1.5">
              <Sliders className="w-3.5 h-3.5 text-indigo-400" />
              信号逻辑与数据详情
            </span>
            <span className="text-[10px] text-emerald-400 font-mono font-medium">
              {activeSignal ? `策略: ${activeSignal.stage} · ${activeSignal.direction.toUpperCase()}` : '自动匹配最新信号'}
            </span>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-[11px]">
            <div className="p-2 rounded bg-neutral-950 border border-neutral-850 space-y-1">
              <div className="text-neutral-400">触发阈值规则：</div>
              <div className="font-mono text-amber-300">
                {activeSignal ? activeSignal.conditionThreshold : '突破上沿 + OI增幅 > 3.5%'}
              </div>
            </div>

            <div className="p-2 rounded bg-neutral-950 border border-neutral-850 space-y-1">
              <div className="text-neutral-400">实际满足数值：</div>
              <div className="font-mono text-emerald-400">
                {activeSignal ? activeSignal.conditionActual : '现价放量上穿，实际增幅 +4.82%'}
              </div>
            </div>
          </div>

          <div className="text-[11px] text-neutral-400 flex flex-wrap items-center justify-between gap-2 pt-1 border-t border-neutral-850">
            <span>
              聪明钱多空持仓: <b className="text-emerald-400">{token.smartMoneyLong.ratioPercent}%</b> / <b className="text-rose-400">{token.smartMoneyShort.ratioPercent}%</b>
            </span>
            <span>
              多空均价: ${token.smartMoneyLong.avgPrice} / ${token.smartMoneyShort.avgPrice}
            </span>
            <span className="text-neutral-500">数据源: 币安/OKX聚合深度 • 无缺口</span>
          </div>
        </div>

        {/* Panel 2: 相关指标 */}
        <div className="p-3 bg-neutral-900/90 rounded-lg border border-neutral-800 space-y-2">
          <div className="flex items-center justify-between border-b border-neutral-800 pb-1.5">
            <span className="font-bold text-neutral-200 flex items-center gap-1.5">
              <BarChart2 className="w-3.5 h-3.5 text-emerald-400" />
              相关指标 (随当前周期展示)
            </span>
            <span className="text-[10px] text-neutral-500">周期: {selectedTf}</span>
          </div>

          <div className="space-y-1.5 text-[11px]">
            <div className="flex justify-between">
              <span className="text-neutral-400">RSI(14):</span>
              <span className="font-mono text-emerald-400 font-semibold">62.8 (多头偏强)</span>
            </div>
            <div className="flex justify-between">
              <span className="text-neutral-400">MACD (12,26,9):</span>
              <span className="font-mono text-emerald-400 font-semibold">+18.4 (金叉发散)</span>
            </div>
            <div className="flex justify-between">
              <span className="text-neutral-400">订单簿流动性失衡:</span>
              <span className="font-mono text-emerald-400 font-semibold">+28% 买方挂单厚</span>
            </div>
            <div className="flex justify-between">
              <span className="text-neutral-400">多空强平清算图谱:</span>
              <span className="font-mono text-rose-400 font-semibold">$68,200 集中清算带</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
