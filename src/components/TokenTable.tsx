/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import { TokenMarketItem, Timeframe, CryptoSignal } from '../types/crypto';
import { InlineExpandedChart } from './InlineExpandedChart';
import { TokenIcon } from './TokenIcon';
import { formatWanYi, formatAmountWanYi } from '../utils/formatters';
import {
  Search,
  Filter,
  ArrowUpDown,
  ChevronRight,
  ChevronDown,
  RefreshCw,
  TrendingUp,
  TrendingDown,
  Zap,
  Sparkles
} from 'lucide-react';

interface TokenTableProps {
  tokens: TokenMarketItem[];
  expandedTokenId: string | null;
  onToggleExpand: (tokenId: string) => void;
  activeSignal?: CryptoSignal;
}

export const TokenTable: React.FC<TokenTableProps> = ({
  tokens,
  expandedTokenId,
  onToggleExpand,
  activeSignal,
}) => {
  const [searchQuery, setSearchQuery] = useState('');

  const filteredTokens = tokens.filter((t) => {
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      return (
        t.symbol.toLowerCase().includes(q) ||
        t.originalSymbol.toLowerCase().includes(q) ||
        t.fullName.toLowerCase().includes(q)
      );
    }
    return true;
  });

  return (
    <div className="flex-1 flex flex-col overflow-hidden bg-neutral-950 p-4 space-y-3">
      {/* Top Header of Table matching sketch */}
      <div className="flex flex-wrap items-center justify-between gap-3 select-none">
        <div>
          <h2 className="text-base font-bold text-neutral-100 flex items-center gap-2">
            <span>全市场代币排序</span>
            <span className="text-xs px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-normal">
              7 组标准列结构
            </span>
          </h2>
          <p className="text-[11px] text-neutral-400">
            代币简称已完成倍率映射去前缀；成交量动态列每 5 秒轮换
          </p>
        </div>

        {/* Sort Indicator matching sketch: 按信号逻辑排序 ↓ */}
        <div className="flex items-center gap-2 text-xs font-semibold text-emerald-400 bg-neutral-900 px-3 py-1.5 rounded-lg border border-neutral-800">
          <span>按信号逻辑排序 ↓</span>
          <ArrowUpDown className="w-3.5 h-3.5" />
        </div>
      </div>

      {/* Filter & Search Bar */}
      <div className="flex flex-wrap items-center justify-between gap-2 p-2.5 bg-neutral-900 rounded-xl border border-neutral-800 text-xs">
        <div className="flex flex-wrap items-center gap-3">
          {/* Search Box */}
          <div className="relative">
            <Search className="w-3.5 h-3.5 text-neutral-400 absolute left-2.5 top-1/2 -translate-y-1/2" />
            <input
              type="text"
              placeholder="搜索代币 (如 BTC, BOB, 0G)..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="bg-neutral-950 border border-neutral-700/80 rounded-lg pl-8 pr-3 py-1 text-xs text-neutral-200 placeholder-neutral-500 focus:outline-none focus:border-emerald-500 w-48 sm:w-56"
            />
          </div>
        </div>

        <div className="text-[11px] text-neutral-500 font-mono">
          共 {filteredTokens.length} 个标的
        </div>
      </div>

      {/* Main Table Container */}
      <div className="flex-1 overflow-auto rounded-xl border border-neutral-800 bg-neutral-900/60 shadow-lg">
        <table className="w-full text-left border-collapse text-xs">
          {/* 7 Columns Table Header matching sketch */}
          <thead className="sticky top-0 z-20 bg-neutral-900 border-b border-neutral-800 text-neutral-300 font-semibold select-none shadow-sm">
            <tr>
              <th className="py-3 px-3 w-8"></th>
              {/* Col 1 */}
              <th className="py-3 px-3">1. 代币 (简称)</th>
              {/* Col 2: Circulating Market Cap (流动市值) */}
              <th className="py-3 px-3">2. 流动市值</th>
              {/* Col 3: Fixed 24H Volume */}
              <th className="py-3 px-3">3. 24h成交量</th>
              {/* Col 4 */}
              <th className="py-3 px-3 text-emerald-400">4. 聪明钱多头</th>
              {/* Col 5 */}
              <th className="py-3 px-3 text-rose-400">5. 聪明钱空头</th>
              {/* Col 6 */}
              <th className="py-3 px-3">6. 总OI持仓</th>
              {/* Col 7 */}
              <th className="py-3 px-3">7. 资金费率 (8h)</th>
              {/* Col 8 */}
              <th className="py-3 px-3 text-center">8. 近24h小时图</th>
              <th className="py-3 px-3 text-right">展开详情</th>
            </tr>
          </thead>

          <tbody className="divide-y divide-neutral-800/60">
            {filteredTokens.map((token) => {
              const isExpanded = expandedTokenId === token.id;

              return (
                <React.Fragment key={token.id}>
                  {/* Token Row */}
                  <tr
                    onClick={() => onToggleExpand(token.id)}
                    className={`cursor-pointer transition-colors ${
                      isExpanded
                        ? 'bg-neutral-850/90 font-medium'
                        : 'hover:bg-neutral-850/50 bg-neutral-900/30'
                    }`}
                  >
                    {/* Expand Chevron */}
                    <td className="py-3 px-3 text-neutral-500">
                      {isExpanded ? (
                        <ChevronDown className="w-4 h-4 text-emerald-400" />
                      ) : (
                        <ChevronRight className="w-4 h-4" />
                      )}
                    </td>

                    {/* Col 1: Token (Logo, Name, Symbol, Price) */}
                    <td className="py-3 px-3">
                      <div className="flex items-center gap-2.5">
                        <TokenIcon symbol={token.symbol} size="sm" />
                        <div>
                          <div className="font-bold text-neutral-100 flex items-center gap-1.5">
                            <span>{token.symbol}</span>
                            {token.activeSignalCount > 0 && (
                              <span className="w-1.5 h-1.5 rounded-full bg-indigo-500 animate-ping"></span>
                            )}
                          </div>
                          <div className="font-mono text-[11px] text-neutral-400 flex items-center gap-1.5">
                            <span>${token.price.toLocaleString()}</span>
                            <span
                              className={
                                token.change24h >= 0 ? 'text-emerald-400' : 'text-rose-400'
                              }
                            >
                              {token.change24h >= 0 ? `+${token.change24h}%` : `${token.change24h}%`}
                            </span>
                          </div>
                        </div>
                      </div>
                    </td>

                    {/* Col 2: Circulating Market Cap (流动市值) */}
                    <td className="py-3 px-3 font-mono">
                      <div className="text-neutral-100 font-bold text-xs">
                        {formatWanYi(token.marketCapUsd)}
                      </div>
                      <div className="text-[10px] text-neutral-400 flex items-center gap-1 mt-0.5">
                        <span className="text-neutral-500 font-sans">流通率:</span>
                        <span className="text-neutral-300 font-semibold">{token.circulatingPercent}%</span>
                      </div>
                    </td>

                    {/* Col 3: Fixed 24H Volume */}
                    <td className="py-3 px-3 font-mono">
                      <div className="text-neutral-100 font-bold text-xs">
                        {formatWanYi(token.volume24hUsd)}
                      </div>
                      <div className="text-[10px] text-neutral-400 mt-0.5">
                        {formatAmountWanYi(token.volume24hAmount)} 币
                      </div>
                    </td>

                    {/* Col 3: Smart Money Long (持仓额, 占比, 均价) */}
                    <td className="py-3 px-3 font-mono">
                      <div className="text-emerald-400 font-bold flex items-center gap-1">
                        <span>{formatWanYi(token.smartMoneyLong.amountUsd)}</span>
                        <span className="text-[10px] text-emerald-400/80 font-normal">
                          ({token.smartMoneyLong.ratioPercent}%)
                        </span>
                      </div>
                      <div className="text-[10px] text-neutral-400">
                        均价: ${token.smartMoneyLong.avgPrice.toLocaleString()}
                      </div>
                    </td>

                    {/* Col 4: Smart Money Short (持仓额, 占比, 均价) */}
                    <td className="py-3 px-3 font-mono">
                      <div className="text-rose-400 font-bold flex items-center gap-1">
                        <span>{formatWanYi(token.smartMoneyShort.amountUsd)}</span>
                        <span className="text-[10px] text-rose-400/80 font-normal">
                          ({token.smartMoneyShort.ratioPercent}%)
                        </span>
                      </div>
                      <div className="text-[10px] text-neutral-400">
                        均价: ${token.smartMoneyShort.avgPrice.toLocaleString()}
                      </div>
                    </td>

                    {/* Col 5: Total OI */}
                    <td className="py-3 px-3 font-mono">
                      <div className="text-neutral-100 font-semibold">
                        {formatWanYi(token.oi.totalUsd)}
                      </div>
                      <div className="text-[10px] text-emerald-400 flex items-center gap-0.5">
                        <TrendingUp className="w-2.5 h-2.5" />
                        <span>+{token.oi.changePercent24h}% (多空比 {token.oi.longShortRatio})</span>
                      </div>
                    </td>

                    {/* Col 6: Funding Rate (8h) */}
                    <td className="py-3 px-3 font-mono">
                      <span
                        className={`font-semibold px-2 py-0.5 rounded text-[11px] ${
                          token.fundingRate >= 0
                            ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
                            : 'bg-rose-500/10 text-rose-400 border border-rose-500/20'
                        }`}
                      >
                        {token.fundingRate >= 0
                          ? `+${(token.fundingRate * 100).toFixed(4)}%`
                          : `${(token.fundingRate * 100).toFixed(4)}%`}
                      </span>
                    </td>

                    {/* Col 7: Sparkline mini chart */}
                    <td className="py-3 px-3">
                      <div className="w-24 h-7 mx-auto">
                        <svg className="w-full h-full" viewBox="0 0 100 30">
                          {(() => {
                            const min = Math.min(...token.sparkline24h);
                            const max = Math.max(...token.sparkline24h);
                            const range = max - min || 1;
                            const pts = token.sparkline24h
                              .map((val, idx) => {
                                const x = (idx / (token.sparkline24h.length - 1)) * 96 + 2;
                                const y = 28 - ((val - min) / range) * 24;
                                return `${x},${y}`;
                              })
                              .join(' ');
                            return (
                              <polyline
                                points={pts}
                                fill="none"
                                stroke={token.change24h >= 0 ? '#10b981' : '#f43f5e'}
                                strokeWidth="2"
                                strokeLinecap="round"
                              />
                            );
                          })()}
                        </svg>
                      </div>
                    </td>

                    {/* Action Column */}
                    <td className="py-3 px-3 text-right">
                      <div className="flex items-center justify-end gap-1.5">
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            onToggleExpand(token.id);
                          }}
                          className={`px-2.5 py-1 rounded text-[11px] font-bold transition-all flex items-center gap-1 ${
                            isExpanded
                              ? 'bg-neutral-800 text-neutral-300'
                              : 'bg-indigo-600/90 hover:bg-indigo-500 text-white shadow-sm ring-1 ring-indigo-400/50'
                          }`}
                        >
                          <span>🔥 订单流热力图</span>
                        </button>
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            onToggleExpand(token.id);
                          }}
                          className="px-2 py-1 rounded bg-neutral-800 hover:bg-neutral-700 text-neutral-300 text-[11px] font-medium transition-colors"
                        >
                          {isExpanded ? '收起' : 'K线'}
                        </button>
                      </div>
                    </td>
                  </tr>

                  {/* Inline Expanded Area Row */}
                  {isExpanded && (
                    <tr>
                      <td colSpan={10} className="p-0 border-b border-neutral-700/80 bg-neutral-950">
                        <InlineExpandedChart
                          token={token}
                          activeSignal={activeSignal}
                          onClose={() => onToggleExpand(token.id)}
                        />
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Table Footer Notes matching sketch */}
      <div className="flex flex-wrap items-center justify-between text-[11px] text-neutral-500 pt-1 select-none">
        <span>
          代币仅简称：确认倍率映射后去前缀（如 1000000BOB→BOB），0G、1INCH等固有数字保留；方向绿多红空、K线涨绿跌红。
        </span>
        <span className="font-mono text-neutral-400">
          第2列：流动市值（万/亿）与流通率；第3列：固定 24 小时总成交额与数量
        </span>
      </div>
    </div>
  );
};
