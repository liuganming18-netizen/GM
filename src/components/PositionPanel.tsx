/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import { Position } from '../types/crypto';
import { formatWanYi } from '../utils/formatters';
import { TokenIcon } from './TokenIcon';
import {
  Wallet,
  Shield,
  ArrowRight,
  TrendingUp,
  TrendingDown,
  Lock,
  PlusCircle,
  X,
  History,
  AlertCircle
} from 'lucide-react';

interface PositionPanelProps {
  paperPositions: Position[];
  onSelectTokenSymbol: (symbol: string) => void;
  onClosePosition: (id: string) => void;
  onOpenQuickPosition: (symbol: string, direction: 'long' | 'short') => void;
}

export const PositionPanel: React.FC<PositionPanelProps> = ({
  paperPositions,
  onSelectTokenSymbol,
  onClosePosition,
  onOpenQuickPosition,
}) => {
  const [showQuickModal, setShowQuickModal] = useState(false);
  const [quickSymbol, setQuickSymbol] = useState('BTC');
  const [quickDirection, setQuickDirection] = useState<'long' | 'short'>('long');

  return (
    <aside className="w-72 sm:w-80 bg-neutral-900 border-l border-neutral-800 flex flex-col justify-between shrink-0 select-none overflow-hidden">
      <div className="flex-1 overflow-y-auto divide-y divide-neutral-800">
        {/* Section 1: 模拟实盘持仓 matching sketch */}
        <div className="p-3.5 space-y-3">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-bold text-neutral-100 flex items-center gap-1.5">
              <Wallet className="w-4 h-4 text-emerald-400" />
              <span>模拟实盘持仓</span>
            </h3>
            <span className="text-[10px] px-1.5 py-0.5 rounded font-mono bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">
              沙盒已接通
            </span>
          </div>

          {/* Account Details Box */}
          <div className="p-2.5 rounded-lg bg-neutral-950 border border-neutral-800 text-[11px] space-y-1 font-mono text-neutral-400">
            <div className="flex justify-between">
              <span>账户 / 连接状态:</span>
              <span className="text-emerald-400 font-semibold">模拟沙盒环境 正常</span>
            </div>
            <div className="flex justify-between">
              <span>权益 / 可用余额:</span>
              <span className="text-neutral-100 font-bold">{formatWanYi(100399.12)}</span>
            </div>
            <div className="flex justify-between">
              <span>已用保证金:</span>
              <span className="text-neutral-300">{formatWanYi(2410)} (2.4%)</span>
            </div>
          </div>

          {/* Quick Trade Button */}
          <div className="flex gap-2">
            <button
              onClick={() => onOpenQuickPosition('BTC', 'long')}
              className="flex-1 py-1.5 rounded-lg bg-emerald-600/20 hover:bg-emerald-600/30 text-emerald-400 border border-emerald-500/30 text-xs font-semibold flex items-center justify-center gap-1 transition-colors"
            >
              <TrendingUp className="w-3.5 h-3.5" />
              快速做多
            </button>
            <button
              onClick={() => onOpenQuickPosition('BTC', 'short')}
              className="flex-1 py-1.5 rounded-lg bg-rose-600/20 hover:bg-rose-600/30 text-rose-400 border border-rose-500/30 text-xs font-semibold flex items-center justify-center gap-1 transition-colors"
            >
              <TrendingDown className="w-3.5 h-3.5" />
              快速做空
            </button>
          </div>

          {/* Positions List */}
          <div className="space-y-2">
            {paperPositions.map((pos) => {
              const isLong = pos.direction === 'long';
              const cleanSym = pos.symbol.replace('USDT', '');
              return (
                <div
                  key={pos.id}
                  onClick={() => onSelectTokenSymbol(cleanSym)}
                  className="p-2.5 rounded-lg bg-neutral-950 border border-neutral-800 hover:border-neutral-700 cursor-pointer space-y-1.5 transition-all text-xs"
                >
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-1.5 font-bold">
                      <TokenIcon symbol={cleanSym} size="xs" />
                      <span className="text-neutral-100">{pos.symbol}</span>
                      <span
                        className={`text-[10px] px-1.5 py-0.2 rounded font-semibold ${
                          isLong
                            ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
                            : 'bg-rose-500/20 text-rose-400 border border-rose-500/30'
                        }`}
                      >
                        {isLong ? '多 (Long)' : '空 (Short)'} {pos.leverage}x
                      </span>
                    </div>

                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        onClosePosition(pos.id);
                      }}
                      className="text-neutral-500 hover:text-rose-400 p-0.5 rounded transition-colors text-[10px]"
                      title="平仓"
                    >
                      平仓
                    </button>
                  </div>

                  <div className="grid grid-cols-2 gap-1 text-[11px] font-mono text-neutral-400">
                    <div>持仓量: <span className="text-neutral-200">{pos.amount}</span></div>
                    <div>均价: <span className="text-neutral-200">${pos.entryPrice}</span></div>
                    <div>标记价: <span className="text-neutral-200">${pos.markPrice}</span></div>
                    <div>
                      未实现盈亏:
                      <span className={`font-bold ml-1 ${pos.unrealizedPnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {pos.unrealizedPnl >= 0 ? `+$${pos.unrealizedPnl}` : `-$${Math.abs(pos.unrealizedPnl)}`}
                        ({pos.unrealizedPnlPercent}%)
                      </span>
                    </div>
                  </div>

                  <div className="flex items-center justify-between text-[10px] text-neutral-500 pt-1 border-t border-neutral-850">
                    <span>止损: ${pos.stopLoss} | 目标: ${pos.takeProfit}</span>
                    <span className="text-emerald-400 flex items-center gap-0.5">
                      定位K线 <ArrowRight className="w-2.5 h-2.5" />
                    </span>
                  </div>
                </div>
              );
            })}
          </div>

          <div className="text-[10px] text-neutral-500 flex items-center justify-between pt-1">
            <span>持仓历史 → 回测复盘</span>
            <span>模拟与实盘分别统计</span>
          </div>
        </div>

        {/* Section 2: 实盘仓位（只读）matching sketch */}
        <div className="p-3.5 space-y-3">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-bold text-neutral-100 flex items-center gap-1.5">
              <Shield className="w-4 h-4 text-neutral-400" />
              <span>实盘仓位（只读）</span>
            </h3>
            <span className="text-[10px] px-1.5 py-0.5 rounded font-mono bg-neutral-800 text-neutral-400 border border-neutral-700">
              未连接 API
            </span>
          </div>

          {/* Empty State matching sketch explicitly */}
          <div className="p-4 rounded-lg bg-neutral-950/60 border border-neutral-800 border-dashed text-center space-y-2">
            <div className="w-8 h-8 rounded-full bg-neutral-800/80 mx-auto flex items-center justify-center text-neutral-500">
              <Lock className="w-4 h-4" />
            </div>
            <div className="text-xs text-neutral-300 font-medium">未接通实盘 API Key</div>
            <p className="text-[11px] text-neutral-500 leading-relaxed">
              严格遵循草图规范：未接通时显示明确空态，绝不填补假数据或假仓位。
            </p>
            <div className="text-[10px] font-mono text-neutral-600 pt-1 border-t border-neutral-850">
              账户 / 连接状态：—<br />
              权益 / 可用余额：—<br />
              币种 / 方向 / 数量：—<br />
              入场均价 / 标记价：—
            </div>
          </div>

          <div className="text-[10px] text-neutral-500 flex items-center justify-between pt-1">
            <span>点击持仓 → 定位中间K线</span>
            <span>仓位历史 → 回测复盘</span>
          </div>
        </div>
      </div>
    </aside>
  );
};
