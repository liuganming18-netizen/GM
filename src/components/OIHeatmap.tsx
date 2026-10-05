/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useMemo } from 'react';
import { TokenMarketItem } from '../types/crypto';
import { formatWanYi, formatAmountWanYi } from '../utils/formatters';
import { Flame, TrendingUp, TrendingDown, Info, ShieldCheck, Activity } from 'lucide-react';

interface OIHeatmapProps {
  token: TokenMarketItem;
}

interface HourlyOiData {
  hourIndex: number;
  timeLabel: string;
  oiUsd: number;
  deltaUsd: number;
  changeRate: number; // percentage, e.g. 5.4, -3.2
  intensity: number; // 0 to 1 scale for color
  institutionAction: 'heavy_inflow' | 'mild_inflow' | 'neutral' | 'mild_outflow' | 'heavy_outflow';
}

export const OIHeatmap: React.FC<OIHeatmapProps> = ({ token }) => {
  const [hoveredHour, setHoveredHour] = useState<HourlyOiData | null>(null);

  // Generate 24 hours of simulated realistic hourly OI change data tailored to token characteristics
  const hourlyData: HourlyOiData[] = useMemo(() => {
    const hours: HourlyOiData[] = [];
    const baseOi = token.oi.totalUsd;
    
    // Seed-based variation for consistent rendering per token
    const seed = token.symbol.charCodeAt(0) + token.symbol.charCodeAt(token.symbol.length - 1);
    
    // Pattern profiles per token
    const isBullish = token.trendJudgment.includes('涨');
    const isBearish = token.trendJudgment.includes('跌');

    let runningOi = baseOi * 0.92;

    for (let i = 0; i < 24; i++) {
      // Create hour labels (e.g. from 23 hours ago to current 00:00~23:00)
      const hourNum = (new Date().getHours() - 23 + i + 24) % 24;
      const hourStr = `${hourNum.toString().padStart(2, '0')}:00`;

      // Generate variable change rate (-12% to +15%)
      const wave = Math.sin((i + seed) * 0.5) * 4.5;
      const noise = ((Math.sin(i * 99 + seed) * 10000) % 7) - 3.5;
      let changeRate = wave + noise;

      if (isBullish) {
        changeRate += 2.2;
      } else if (isBearish) {
        changeRate -= 2.0;
      }

      // Add a couple of distinct institutional spike hours
      if (i === 11 || i === 18) {
        changeRate = isBearish ? -8.4 : 11.8;
      } else if (i === 4 || i === 15) {
        changeRate = isBearish ? 6.2 : -7.5;
      }

      changeRate = Number(changeRate.toFixed(2));
      const deltaUsd = Math.round(runningOi * (changeRate / 100));
      runningOi += deltaUsd;

      let institutionAction: HourlyOiData['institutionAction'] = 'neutral';
      if (changeRate >= 5.0) institutionAction = 'heavy_inflow';
      else if (changeRate > 1.0) institutionAction = 'mild_inflow';
      else if (changeRate <= -5.0) institutionAction = 'heavy_outflow';
      else if (changeRate < -1.0) institutionAction = 'mild_outflow';

      const absRate = Math.abs(changeRate);
      const intensity = Math.min(1, Math.max(0.1, absRate / 12));

      hours.push({
        hourIndex: i,
        timeLabel: hourStr,
        oiUsd: runningOi,
        deltaUsd,
        changeRate,
        intensity,
        institutionAction,
      });
    }

    return hours;
  }, [token]);

  // Calculated summary statistics
  const peakInflow = useMemo(() => {
    return [...hourlyData].sort((a, b) => b.changeRate - a.changeRate)[0];
  }, [hourlyData]);

  const peakOutflow = useMemo(() => {
    return [...hourlyData].sort((a, b) => a.changeRate - b.changeRate)[0];
  }, [hourlyData]);

  const net24hChange = useMemo(() => {
    const totalDelta = hourlyData.reduce((acc, cur) => acc + cur.deltaUsd, 0);
    const startOi = hourlyData[0].oiUsd;
    const percent = ((totalDelta / startOi) * 100).toFixed(2);
    return { totalDelta, percent: Number(percent) };
  }, [hourlyData]);

  // Color mapper based on change rate
  const getCellColor = (rate: number) => {
    if (rate >= 8.0) {
      return {
        bg: 'bg-emerald-600/90 hover:bg-emerald-500',
        border: 'border-emerald-400/80 shadow-[0_0_10px_rgba(16,185,129,0.35)]',
        text: 'text-white font-black',
        subText: 'text-emerald-100',
      };
    }
    if (rate >= 4.0) {
      return {
        bg: 'bg-emerald-700/80 hover:bg-emerald-600',
        border: 'border-emerald-500/60 shadow-sm',
        text: 'text-emerald-100 font-bold',
        subText: 'text-emerald-200/80',
      };
    }
    if (rate >= 1.5) {
      return {
        bg: 'bg-emerald-900/70 hover:bg-emerald-800',
        border: 'border-emerald-600/40',
        text: 'text-emerald-300 font-semibold',
        subText: 'text-emerald-400/70',
      };
    }
    if (rate > 0.3) {
      return {
        bg: 'bg-emerald-950/60 hover:bg-emerald-900/60',
        border: 'border-emerald-700/30',
        text: 'text-emerald-400 font-medium',
        subText: 'text-emerald-500/60',
      };
    }
    if (rate <= -8.0) {
      return {
        bg: 'bg-rose-600/90 hover:bg-rose-500',
        border: 'border-rose-400/80 shadow-[0_0_10px_rgba(244,63,94,0.35)]',
        text: 'text-white font-black',
        subText: 'text-rose-100',
      };
    }
    if (rate <= -4.0) {
      return {
        bg: 'bg-rose-700/80 hover:bg-rose-600',
        border: 'border-rose-500/60 shadow-sm',
        text: 'text-rose-100 font-bold',
        subText: 'text-rose-200/80',
      };
    }
    if (rate <= -1.5) {
      return {
        bg: 'bg-rose-900/70 hover:bg-rose-800',
        border: 'border-rose-600/40',
        text: 'text-rose-300 font-semibold',
        subText: 'text-rose-400/70',
      };
    }
    if (rate < -0.3) {
      return {
        bg: 'bg-rose-950/60 hover:bg-rose-900/60',
        border: 'border-rose-700/30',
        text: 'text-rose-400 font-medium',
        subText: 'text-rose-500/60',
      };
    }
    // Neutral
    return {
      bg: 'bg-neutral-900/80 hover:bg-neutral-800',
      border: 'border-neutral-800',
      text: 'text-neutral-400',
      subText: 'text-neutral-500',
    };
  };

  const getActionLabel = (action: HourlyOiData['institutionAction']) => {
    switch (action) {
      case 'heavy_inflow':
        return { label: '🟢 机构猛烈增仓（多头强力进场）', color: 'text-emerald-400' };
      case 'mild_inflow':
        return { label: '🟩 温和吸筹建仓', color: 'text-emerald-300' };
      case 'neutral':
        return { label: '⚪ 存量博弈 / 窄幅平衡', color: 'text-neutral-400' };
      case 'mild_outflow':
        return { label: '🟧 获利平仓 / 适度去杠杆', color: 'text-rose-300' };
      case 'heavy_outflow':
        return { label: '🔴 机构急剧减仓（多空踩踏出逃）', color: 'text-rose-400' };
    }
  };

  return (
    <div className="bg-neutral-900/90 rounded-xl border border-neutral-800 p-3.5 space-y-3 shadow-md select-none">
      {/* Top Header & Stat HUD */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-neutral-800 pb-2.5">
        <div className="flex items-center gap-2">
          <div className="p-1.5 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-400">
            <Flame className="w-4 h-4" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h3 className="font-bold text-xs text-neutral-100 flex items-center gap-1.5">
                <span>{token.symbol} OI（持仓量）逐小时变化率热力图</span>
                <span className="text-[10px] px-1.5 py-0.2 rounded bg-indigo-500/15 text-indigo-300 border border-indigo-500/30 font-mono">
                  近24小时 • 机构动向监控
                </span>
              </h3>
            </div>
            <p className="text-[10px] text-neutral-400">
              通过每小时未平仓合约变化率（绿色增仓、红色减仓）快速识别主力大户建仓与平仓节奏。
            </p>
          </div>
        </div>

        {/* 24h Summary Stats */}
        <div className="flex items-center gap-3 text-xs font-mono">
          <div className="px-2.5 py-1 rounded bg-neutral-950 border border-neutral-800 flex items-center gap-1.5">
            <span className="text-neutral-500 font-sans text-[11px]">最大单时增仓:</span>
            <span className="text-emerald-400 font-bold">
              +{peakInflow.changeRate}% ({peakInflow.timeLabel})
            </span>
          </div>

          <div className="px-2.5 py-1 rounded bg-neutral-950 border border-neutral-800 flex items-center gap-1.5">
            <span className="text-neutral-500 font-sans text-[11px]">最大单时减仓:</span>
            <span className="text-rose-400 font-bold">
              {peakOutflow.changeRate}% ({peakOutflow.timeLabel})
            </span>
          </div>

          <div className="px-2.5 py-1 rounded bg-neutral-950 border border-neutral-800 flex items-center gap-1.5">
            <span className="text-neutral-500 font-sans text-[11px]">24h总OI净变:</span>
            <span
              className={`font-bold ${
                net24hChange.percent >= 0 ? 'text-emerald-400' : 'text-rose-400'
              }`}
            >
              {net24hChange.percent >= 0 ? `+${net24hChange.percent}%` : `${net24hChange.percent}%`}
              <span className="text-[10px] font-normal text-neutral-400 ml-1">
                ({formatWanYi(net24hChange.totalDelta)})
              </span>
            </span>
          </div>
        </div>
      </div>

      {/* Heatmap 24-Cell Hourly Grid */}
      <div className="grid grid-cols-6 sm:grid-cols-12 lg:grid-cols-24 gap-1.5 pt-1">
        {hourlyData.map((hour) => {
          const colors = getCellColor(hour.changeRate);
          const isSelected = hoveredHour?.hourIndex === hour.hourIndex;

          return (
            <div
              key={hour.hourIndex}
              onMouseEnter={() => setHoveredHour(hour)}
              onMouseLeave={() => setHoveredHour(null)}
              className={`relative rounded-lg p-2 flex flex-col justify-between items-center transition-all cursor-pointer border ${
                colors.bg
              } ${colors.border} ${
                isSelected ? 'ring-2 ring-amber-400 scale-105 z-20' : ''
              } min-h-[64px]`}
            >
              {/* Hour Label */}
              <span className="text-[10px] font-mono text-neutral-400 font-medium">
                {hour.timeLabel}
              </span>

              {/* Rate Change Badge */}
              <span className={`text-xs font-mono my-0.5 ${colors.text}`}>
                {hour.changeRate >= 0 ? `+${hour.changeRate}%` : `${hour.changeRate}%`}
              </span>

              {/* Mini Inflow Delta Amount */}
              <span className={`text-[9px] font-mono truncate max-w-full ${colors.subText}`}>
                {formatWanYi(hour.deltaUsd)}
              </span>
            </div>
          );
        })}
      </div>

      {/* Interactive Detail HUD Bar when Hovered */}
      <div className="flex flex-wrap items-center justify-between text-xs bg-neutral-950/80 px-3.5 py-2 rounded-lg border border-neutral-800 gap-3 min-h-[36px]">
        {hoveredHour ? (
          <div className="flex flex-wrap items-center gap-4 w-full justify-between">
            <div className="flex items-center gap-3">
              <span className="font-bold text-neutral-200 flex items-center gap-1.5 font-mono">
                <span className="w-2 h-2 rounded-full bg-amber-400 animate-ping"></span>
                时段: {hoveredHour.timeLabel} - {((parseInt(hoveredHour.timeLabel) + 1) % 24).toString().padStart(2, '0')}:00
              </span>
              <span className="text-neutral-400">
                该时点OI持仓: <b className="text-neutral-100 font-mono">{formatWanYi(hoveredHour.oiUsd)}</b>
              </span>
              <span className="text-neutral-400">
                1小时变动额: <b className={`font-mono ${hoveredHour.deltaUsd >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                  {hoveredHour.deltaUsd >= 0 ? `+${formatWanYi(hoveredHour.deltaUsd)}` : formatWanYi(hoveredHour.deltaUsd)}
                </b>
              </span>
              <span className="text-neutral-400">
                变化率: <b className={`font-mono ${hoveredHour.changeRate >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                  {hoveredHour.changeRate >= 0 ? `+${hoveredHour.changeRate}%` : `${hoveredHour.changeRate}%`}
                </b>
              </span>
            </div>

            <div className={`text-xs font-medium ${getActionLabel(hoveredHour.institutionAction).color}`}>
              {getActionLabel(hoveredHour.institutionAction).label}
            </div>
          </div>
        ) : (
          <div className="flex flex-wrap items-center justify-between w-full text-[11px] text-neutral-400">
            <div className="flex items-center gap-1.5">
              <Info className="w-3.5 h-3.5 text-neutral-500" />
              <span>鼠标移入上方任意色块，可即时查看该小时的机构增减仓额与主力博弈意图。</span>
            </div>

            {/* Continuous Gradient Color Legend */}
            <div className="flex items-center gap-2">
              <span className="text-neutral-500 text-[10px]">持仓急剧萎缩</span>
              <div className="flex items-center h-2.5 rounded-sm overflow-hidden border border-neutral-700 w-36">
                <div className="h-full flex-1 bg-rose-600"></div>
                <div className="h-full flex-1 bg-rose-800"></div>
                <div className="h-full flex-1 bg-neutral-800"></div>
                <div className="h-full flex-1 bg-emerald-800"></div>
                <div className="h-full flex-1 bg-emerald-600"></div>
              </div>
              <span className="text-neutral-500 text-[10px]">机构猛烈增仓</span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};
