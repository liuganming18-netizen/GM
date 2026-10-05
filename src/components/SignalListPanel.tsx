/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import { CryptoSignal } from '../types/crypto';
import { Target, CheckCircle2, XCircle, Clock, ArrowRight, ShieldCheck } from 'lucide-react';

interface SignalListPanelProps {
  signals: CryptoSignal[];
  selectedSignalId: string | null;
  onSelectSignal: (signal: CryptoSignal) => void;
}

export const SignalListPanel: React.FC<SignalListPanelProps> = ({
  signals,
  selectedSignalId,
  onSelectSignal,
}) => {
  const [filter, setFilter] = useState<'all' | 'valid' | 'invalid'>('all');

  const filteredSignals = signals.filter((s) => {
    if (filter === 'valid') return s.status === 'valid' || s.status === 'forming';
    if (filter === 'invalid') return s.status === 'invalid';
    return true;
  });

  return (
    <div className="w-56 sm:w-60 bg-neutral-900 border-r border-neutral-800 flex flex-col justify-between shrink-0 select-none overflow-hidden">
      <div className="flex flex-col flex-1 overflow-hidden">
        {/* Header */}
        <div className="p-3 border-b border-neutral-800 space-y-2">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-bold text-neutral-100 flex items-center gap-1.5">
              <Target className="w-4 h-4 text-emerald-400" />
              <span>信号区</span>
            </h3>
            <span className="text-[10px] text-neutral-400 font-mono">
              {filteredSignals.length} 条信号
            </span>
          </div>

          {/* Filter Bar: 全部 / 有效 / 失效 */}
          <div className="flex items-center gap-1 bg-neutral-950 p-1 rounded-lg border border-neutral-800">
            <button
              onClick={() => setFilter('all')}
              className={`flex-1 py-1 text-[11px] font-medium rounded transition-colors ${
                filter === 'all'
                  ? 'bg-neutral-800 text-neutral-100 font-semibold shadow'
                  : 'text-neutral-500 hover:text-neutral-300'
              }`}
            >
              全部
            </button>
            <button
              onClick={() => setFilter('valid')}
              className={`flex-1 py-1 text-[11px] font-medium rounded transition-colors ${
                filter === 'valid'
                  ? 'bg-emerald-500/20 text-emerald-400 font-semibold border border-emerald-500/30'
                  : 'text-neutral-500 hover:text-neutral-300'
              }`}
            >
              有效
            </button>
            <button
              onClick={() => setFilter('invalid')}
              className={`flex-1 py-1 text-[11px] font-medium rounded transition-colors ${
                filter === 'invalid'
                  ? 'bg-rose-500/20 text-rose-400 font-semibold border border-rose-500/30'
                  : 'text-neutral-500 hover:text-neutral-300'
              }`}
            >
              失效
            </button>
          </div>
        </div>

        {/* Signals List matching the dashed wireframe in sketch */}
        <div className="flex-1 overflow-y-auto p-2.5 space-y-2.5">
          {filteredSignals.map((signal) => {
            const isSelected = selectedSignalId === signal.id;
            const isLong = signal.direction === 'long';
            const isShort = signal.direction === 'short';

            return (
              <div
                key={signal.id}
                onClick={() => onSelectSignal(signal)}
                className={`p-3 rounded-lg border cursor-pointer transition-all ${
                  isSelected
                    ? 'bg-neutral-800/90 border-emerald-500 shadow-md ring-1 ring-emerald-500/50'
                    : 'bg-neutral-950/70 border-neutral-800 hover:border-neutral-700 hover:bg-neutral-850'
                }`}
              >
                {/* Top Row: Symbol & Exchange */}
                <div className="flex items-center justify-between text-xs mb-1.5">
                  <div className="flex items-center gap-1.5 font-bold text-neutral-100">
                    <span>{signal.tokenSymbol}</span>
                    <span className="text-[10px] text-neutral-500 font-normal">/ {signal.exchange}</span>
                  </div>
                  <span
                    className={`text-[10px] px-1.5 py-0.2 rounded font-semibold ${
                      signal.status === 'valid'
                        ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
                        : signal.status === 'forming'
                        ? 'bg-amber-500/20 text-amber-400 border border-amber-500/30'
                        : 'bg-rose-500/20 text-rose-400 border border-rose-500/30'
                    }`}
                  >
                    {signal.status === 'valid' ? '有效' : signal.status === 'forming' ? '形成中' : '失效'}
                  </span>
                </div>

                {/* Second Row: Stage / Direction */}
                <div className="flex items-center justify-between text-[11px] text-neutral-400 mb-1">
                  <span>策略阶段 / 方向：</span>
                  <span
                    className={`font-semibold ${
                      isLong ? 'text-emerald-400' : isShort ? 'text-rose-400' : 'text-neutral-400'
                    }`}
                  >
                    {signal.stage} · {isLong ? '做多 (Long)' : isShort ? '做空 (Short)' : '中性'}
                  </span>
                </div>

                {/* Third Row: Timeframe / Trigger Time */}
                <div className="flex items-center justify-between text-[11px] text-neutral-400 mb-1">
                  <span>周期 / 触发时间：</span>
                  <span className="font-mono text-neutral-300">
                    {signal.timeframe} · {signal.triggerTime}
                  </span>
                </div>

                {/* Fourth Row: Trigger Price */}
                <div className="flex items-center justify-between text-[11px] text-neutral-400 mb-2">
                  <span>触发表 / 现价：</span>
                  <span className="font-mono text-neutral-200">
                    ${signal.currentPrice.toLocaleString()}
                  </span>
                </div>

                {/* Card Action footer matching sketch: 点击 → 定位中间K线 */}
                <div className="pt-2 border-t border-neutral-800/80 flex items-center justify-between text-[10px] text-neutral-400 group-hover:text-emerald-400">
                  <span className="text-emerald-400/90 font-medium flex items-center gap-1">
                    点击 → 定位中间K线
                  </span>
                  <ArrowRight className="w-3 h-3 text-neutral-500" />
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Footer Notes matching sketch */}
      <div className="p-3 border-t border-neutral-800 bg-neutral-950/80 text-[10px] text-neutral-500 space-y-1">
        <div className="flex items-center gap-1 text-neutral-400 font-medium">
          <ShieldCheck className="w-3.5 h-3.5 text-emerald-400" />
          <span>信号与代币联动机制</span>
        </div>
        <p>• 排名来自后台信号逻辑</p>
        <p>• 成交量轮换不改变代币排名</p>
      </div>
    </div>
  );
};
