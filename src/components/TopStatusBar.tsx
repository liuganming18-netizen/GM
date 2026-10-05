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
  btcPrice: number;
  btcChange: number;
  ethPrice: number;
  ethChange: number;
  paperBalance: number;
}

export const TopStatusBar: React.FC<TopStatusBarProps> = ({
  btcPrice,
  btcChange,
  ethPrice,
  ethChange,
  paperBalance,
}) => {
  const [timeStr, setTimeStr] = useState<string>('');

  useEffect(() => {
    const updateTime = () => {
      const now = new Date();
      setTimeStr(now.toLocaleTimeString('zh-CN', { hour12: false }));
    };
    updateTime();
    const interval = setInterval(updateTime, 1000);
    return () => clearInterval(interval);
  }, []);

  return (
    <header className="h-14 bg-neutral-900 border-b border-neutral-800 px-4 flex items-center justify-between gap-4 shrink-0 select-none overflow-x-auto">
      {/* Left: Section Label & Key Market Tickers */}
      <div className="flex items-center gap-4 shrink-0">
        <div className="flex items-center gap-1.5 px-2.5 py-1 rounded bg-neutral-800/80 border border-neutral-700/60 text-xs font-semibold text-neutral-200">
          <span className="w-2 h-2 rounded-full bg-emerald-400"></span>
          <span>顶部总览 · 市场全局状态</span>
        </div>

        {/* BTC Ticker */}
        <div className="flex items-center gap-2 text-xs">
          <TokenIcon symbol="BTC" size="xs" />
          <span className="font-bold text-neutral-300">BTC</span>
          <span className="font-mono font-semibold text-neutral-100">${btcPrice.toLocaleString()}</span>
          <span
            className={`flex items-center text-[11px] font-mono font-medium ${
              btcChange >= 0 ? 'text-emerald-400' : 'text-rose-400'
            }`}
          >
            {btcChange >= 0 ? <TrendingUp className="w-3 h-3 mr-0.5" /> : <TrendingDown className="w-3 h-3 mr-0.5" />}
            {btcChange >= 0 ? `+${btcChange}%` : `${btcChange}%`}
          </span>
        </div>

        {/* ETH Ticker */}
        <div className="flex items-center gap-2 text-xs border-l border-neutral-800 pl-4">
          <TokenIcon symbol="ETH" size="xs" />
          <span className="font-bold text-neutral-300">ETH</span>
          <span className="font-mono font-semibold text-neutral-100">${ethPrice.toLocaleString()}</span>
          <span
            className={`flex items-center text-[11px] font-mono font-medium ${
              ethChange >= 0 ? 'text-emerald-400' : 'text-rose-400'
            }`}
          >
            {ethChange >= 0 ? <TrendingUp className="w-3 h-3 mr-0.5" /> : <TrendingDown className="w-3 h-3 mr-0.5" />}
            {ethChange >= 0 ? `+${ethChange}%` : `${ethChange}%`}
          </span>
        </div>
      </div>

      {/* Middle: Macro Sentiment & OI summary */}
      <div className="hidden lg:flex items-center gap-5 text-xs text-neutral-400 shrink-0">
        <div className="flex items-center gap-1.5">
          <BarChart3 className="w-3.5 h-3.5 text-indigo-400" />
          <span>全网OI持仓额:</span>
          <span className="font-mono font-semibold text-neutral-200">{formatWanYi(38450000000)}</span>
          <span className="text-[10px] text-emerald-400 font-mono">+4.2%</span>
        </div>

        <div className="flex items-center gap-1.5">
          <Zap className="w-3.5 h-3.5 text-amber-400" />
          <span>聪明钱多空比:</span>
          <span className="font-mono font-semibold text-emerald-400">1.68 (偏多)</span>
        </div>

        <div className="flex items-center gap-1.5">
          <Shield className="w-3.5 h-3.5 text-emerald-400" />
          <span>平均费率 (8h):</span>
          <span className="font-mono text-emerald-400">+0.0112%</span>
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
