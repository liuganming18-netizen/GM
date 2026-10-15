/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import { formatWanYi } from '../utils/formatters';
import { TokenIcon } from './TokenIcon';
import {
  TrendingUp,
  TrendingDown,
  Clock,
  Shield,
  Zap,
  BarChart3,
  DollarSign
} from 'lucide-react';

interface TopStatusBarProps {
  btcPrice: number | null;
  btcChange: number | null;
  ethPrice: number | null;
  ethChange: number | null;
  paperBalance: number | null;
  /** 本系统活动池 OI USD 名义额：来源未全覆盖时保持 null，不代表全网 */
  networkOiUsd: number | null;
  /** /markets 中有可比 USD 名义额的标的数 / 本系统活动池标的数 */
  oiUsdCoverage: number;
  oiPoolTotal: number;
  /** 聪明钱多空比：无真实来源，恒为 null */
  smartMoneyRatio: number | null;
  /** 活跃池真实 funding_rate 均值 */
  avgFundingRate: number | null;
  /** 后端 as_of（刷新时间） */
  asOf: string | null;
  /** 取数错误（仅用于控制台提示，不新增界面元素） */
  feedError?: string | null;
}

/** 未知一律显示 '—'，不显示 0 或默认值。 */
const fmtNum = (v: number | null | undefined, digits = 0): string =>
  v === null || v === undefined || !Number.isFinite(v)
    ? '—'
    : v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });

const fmtSignedPercent = (v: number | null | undefined, digits = 2): string =>
  v === null || v === undefined || !Number.isFinite(v) ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(digits)}%`;

export const TopStatusBar: React.FC<TopStatusBarProps> = ({
  btcPrice,
  btcChange,
  ethPrice,
  ethChange,
  paperBalance,
  networkOiUsd,
  oiUsdCoverage,
  oiPoolTotal,
  smartMoneyRatio,
  avgFundingRate,
  asOf,
  feedError,
}) => {
  const [timeStr, setTimeStr] = useState<string>('');

  useEffect(() => {
    // 该位置自述为 UTC，故按 UTC 呈现（后端时间均为 UTC）。
    const updateTime = () => {
      const now = new Date();
      setTimeStr(now.toLocaleTimeString('zh-CN', { hour12: false, timeZone: 'UTC' }));
    };
    updateTime();
    const interval = setInterval(updateTime, 1000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    if (feedError) console.warn('[真实数据] 取数失败：', feedError);
  }, [feedError]);

  return (
    <header className="h-14 bg-neutral-900 border-b border-neutral-800 px-4 flex items-center justify-between gap-4 shrink-0 select-none overflow-x-auto ai-scanline">
      {/* Left: Section Label & Key Market Tickers */}
      <div className="flex items-center gap-4 shrink-0">
        <div className="flex items-center gap-1.5 px-2.5 py-1 rounded bg-neutral-800/80 border border-neutral-700/60 text-xs font-semibold text-neutral-200">
          <span className="w-2 h-2 rounded-full bg-emerald-400 ai-pulse"></span>
          <span>顶部总览 · 市场全局状态</span>
        </div>

        {/* BTC Ticker */}
        <div className="flex items-center gap-2 text-xs">
          <TokenIcon symbol="BTC" size="xs" />
          <span className="font-bold text-neutral-300">BTC</span>
          <span className="font-mono font-semibold text-neutral-100">${fmtNum(btcPrice)}</span>
          <span
            className={`flex items-center text-[11px] font-mono font-medium ${
              (btcChange ?? 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'
            }`}
          >
            {(btcChange ?? 0) >= 0 ? <TrendingUp className="w-3 h-3 mr-0.5" /> : <TrendingDown className="w-3 h-3 mr-0.5" />}
            {fmtSignedPercent(btcChange)}
          </span>
        </div>

        {/* ETH Ticker */}
        <div className="flex items-center gap-2 text-xs border-l border-neutral-800 pl-4">
          <TokenIcon symbol="ETH" size="xs" />
          <span className="font-bold text-neutral-300">ETH</span>
          <span className="font-mono font-semibold text-neutral-100">${fmtNum(ethPrice)}</span>
          <span
            className={`flex items-center text-[11px] font-mono font-medium ${
              (ethChange ?? 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'
            }`}
          >
            {(ethChange ?? 0) >= 0 ? <TrendingUp className="w-3 h-3 mr-0.5" /> : <TrendingDown className="w-3 h-3 mr-0.5" />}
            {fmtSignedPercent(ethChange)}
          </span>
        </div>
      </div>

      {/* Middle: Macro Sentiment & OI summary */}
      <div className="hidden lg:flex items-center gap-5 text-xs text-neutral-400 shrink-0">
        <div
          className="flex items-center gap-1.5"
          title={`仅统计本系统活动行情池。当前前端数据中取得 USD 名义额的标的 ${oiUsdCoverage}/${oiPoolTotal}；覆盖不足，不汇总，也不代表全网。`}
        >
          <BarChart3 className="w-3.5 h-3.5 text-indigo-400" />
          <span>本系统池 OI(USD):</span>
          <span className="font-mono font-semibold text-neutral-200">{formatWanYi(networkOiUsd)}</span>
          <span className="text-[10px] text-neutral-400 font-mono">覆盖 {oiUsdCoverage}/{oiPoolTotal}</span>
        </div>

        <div className="flex items-center gap-1.5">
          <Zap className="w-3.5 h-3.5 text-amber-400" />
          <span>聪明钱多空比:</span>
          <span className="font-mono font-semibold text-emerald-400">
            {smartMoneyRatio === null ? '—' : smartMoneyRatio.toFixed(2)}
          </span>
        </div>

        <div className="flex items-center gap-1.5">
          <Shield className="w-3.5 h-3.5 text-emerald-400" />
          <span>平均费率 (8h):</span>
          <span className="font-mono text-emerald-400">
            {avgFundingRate === null ? '—' : fmtSignedPercent(avgFundingRate * 100, 4)}
          </span>
        </div>
      </div>

      {/* Right: Simulated Balance & Clock */}
      <div className="flex items-center gap-3 shrink-0">
        <div className="flex items-center gap-2 px-3 py-1 rounded-lg bg-neutral-800/60 border border-neutral-700/60 text-xs">
          <DollarSign className="w-3.5 h-3.5 text-emerald-400" />
          <span className="text-neutral-400">模拟账户可用:</span>
          <span className="font-mono font-bold text-emerald-400">{formatWanYi(paperBalance)}</span>
        </div>

        <div className="flex items-center gap-1.5 text-xs font-mono text-neutral-400 bg-neutral-950 px-2.5 py-1 rounded border border-neutral-800">
          <Clock className="w-3.5 h-3.5 text-neutral-500" />
          <span>{timeStr || '14:30:00'}</span>
          <span className="text-[10px] text-neutral-600">UTC</span>
        </div>
      </div>
    </header>
  );
};
