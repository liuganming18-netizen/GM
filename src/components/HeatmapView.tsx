/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useMemo } from 'react';
import { TokenMarketItem } from '../types/crypto';
import { initialTokens } from '../mock/cryptoData';
import { TokenIcon } from './TokenIcon';
import { OrderFlowHeatmap } from './OrderFlowHeatmap';
import { OIHeatmap } from './OIHeatmap';
import { formatWanYi, formatAmountWanYi } from '../utils/formatters';
import {
  Flame,
  Layers,
  Activity,
  Zap,
  ShieldAlert,
  TrendingUp,
  TrendingDown,
  ArrowUpRight,
  ArrowDownRight,
  Filter,
  BarChart2,
  RefreshCw,
  Info
} from 'lucide-react';

type HeatmapCategoryTab = 'orderflow_depth' | 'market_oi_matrix' | 'token_oi_single';

export const HeatmapView: React.FC = () => {
  const [tokens] = useState<TokenMarketItem[]>(initialTokens);
  const [selectedTokenId, setSelectedTokenId] = useState<string>('btc');
  const [currentTab, setCurrentTab] = useState<HeatmapCategoryTab>('orderflow_depth');
  const [exchangeScope, setExchangeScope] = useState<'all' | 'binance' | 'okx'>('all');

  const selectedToken = useMemo(() => {
    return tokens.find((t) => t.id === selectedTokenId) || tokens[0];
  }, [tokens, selectedTokenId]);

  // Generate 24-hour hourly matrix data for ALL tokens for the Market-Wide Matrix mode
  const marketMatrixData = useMemo(() => {
    return tokens.map((t) => {
      const seed = t.symbol.charCodeAt(0) + t.symbol.charCodeAt(t.symbol.length - 1);
      const isBull = t.change24h > 0;
      const hours = [];
      const baseOi = t.oi.totalUsd;

      for (let h = 0; h < 24; h++) {
        const hourNum = (new Date().getHours() - 23 + h + 24) % 24;
        const timeStr = `${hourNum.toString().padStart(2, '0')}:00`;
        const wave = Math.sin((h + seed) * 0.5) * 5;
        const noise = ((Math.sin(h * 77 + seed) * 10000) % 6) - 3;
        let rate = wave + noise + (isBull ? 2.5 : -2.5);
        if (h === 14 || h === 19) rate = isBull ? 12.4 : -9.2;
        if (h === 4 || h === 9) rate = isBull ? -6.8 : 8.5;
        rate = Number(rate.toFixed(1));

        const deltaUsd = Math.round(baseOi * (rate / 100));
        hours.push({
          timeStr,
          rate,
          deltaUsd,
        });
      }

      return {
        token: t,
        hours,
      };
    });
  }, [tokens]);

  const getMatrixCellColor = (rate: number) => {
    if (rate >= 8) return 'bg-emerald-600 text-white font-bold shadow-sm';
    if (rate >= 4) return 'bg-emerald-700/80 text-emerald-100';
    if (rate >= 1) return 'bg-emerald-900/60 text-emerald-300';
    if (rate <= -8) return 'bg-rose-600 text-white font-bold shadow-sm';
    if (rate <= -4) return 'bg-rose-700/80 text-rose-100';
    if (rate <= -1) return 'bg-rose-900/60 text-rose-300';
    return 'bg-neutral-900 text-neutral-500';
  };

  return (
    <div className="flex-1 flex flex-col h-full overflow-hidden bg-neutral-950 p-4 gap-3 select-none text-neutral-200">
      {/* 1. Header Bar with Category Title & Global Macro Metrics */}
      <div className="flex flex-wrap items-center justify-between border-b border-neutral-800 pb-3 gap-3">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-xl bg-gradient-to-br from-indigo-500/20 to-amber-500/20 border border-indigo-500/30 flex items-center justify-center text-amber-400">
            <Flame className="w-5 h-5" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-xl font-bold text-neutral-100 tracking-wide flex items-center gap-2">
                <span>热力图大类</span>
                <span className="text-xs px-2 py-0.5 rounded-full bg-amber-500/15 text-amber-300 border border-amber-500/30 font-medium">
                  OKX & 币安 聚合订单流
                </span>
              </h1>
            </div>
            <p className="text-[11px] text-neutral-400">
              全网衍生品大单挂单深度、集中强平爆仓带、CVD 订单流及 24H OI 机构异动热力矩阵。
            </p>
          </div>
        </div>

        {/* Global Macro Stats */}
        <div className="flex flex-wrap items-center gap-2 text-xs font-mono">
          <div className="px-3 py-1.5 rounded-lg bg-neutral-900 border border-neutral-800 flex items-center gap-2">
            <span className="text-neutral-500 font-sans text-[11px]">24H全网爆仓额:</span>
            <span className="text-rose-400 font-bold">$2.84亿</span>
            <span className="text-[10px] text-neutral-500 font-sans">(空爆 $1.72亿 / 多爆 $1.12亿)</span>
          </div>

          <div className="px-3 py-1.5 rounded-lg bg-neutral-900 border border-neutral-800 flex items-center gap-2">
            <span className="text-neutral-500 font-sans text-[11px]">盘口失衡度:</span>
            <span className="text-emerald-400 font-bold">+21.8%</span>
            <span className="text-[10px] text-emerald-500 font-sans">(买方托盘占优)</span>
          </div>

          <div className="px-3 py-1.5 rounded-lg bg-neutral-900 border border-neutral-800 flex items-center gap-2">
            <span className="text-neutral-500 font-sans text-[11px]">监控标的:</span>
            <span className="text-neutral-200 font-bold">{tokens.length} 个主流币</span>
          </div>
        </div>
      </div>

      {/* 2. Mode Sub-tabs & Exchange Controls */}
      <div className="flex flex-wrap items-center justify-between gap-3 bg-neutral-900/90 p-2 rounded-xl border border-neutral-800 text-xs">
        {/* Navigation Tabs */}
        <div className="flex items-center gap-1.5">
          <button
            onClick={() => setCurrentTab('orderflow_depth')}
            className={`px-3 py-1.5 rounded-lg font-bold transition-all flex items-center gap-1.5 ${
              currentTab === 'orderflow_depth'
                ? 'bg-indigo-600 text-white shadow-[0_0_12px_rgba(99,102,241,0.4)]'
                : 'text-neutral-400 hover:text-neutral-200 hover:bg-neutral-800'
            }`}
          >
            <Layers className="w-3.5 h-3.5 text-amber-300" />
            <span>单币 订单流与 OKX / 币安热力图</span>
          </button>

          <button
            onClick={() => setCurrentTab('market_oi_matrix')}
            className={`px-3 py-1.5 rounded-lg font-bold transition-all flex items-center gap-1.5 ${
              currentTab === 'market_oi_matrix'
                ? 'bg-amber-600 text-white shadow-[0_0_12px_rgba(217,119,6,0.4)]'
                : 'text-neutral-400 hover:text-neutral-200 hover:bg-neutral-800'
            }`}
          >
            <Flame className="w-3.5 h-3.5 text-yellow-300" />
            <span>全市场 24H OI 逐小时热力矩阵</span>
          </button>

          <button
            onClick={() => setCurrentTab('token_oi_single')}
            className={`px-3 py-1.5 rounded-lg font-bold transition-all flex items-center gap-1.5 ${
              currentTab === 'token_oi_single'
                ? 'bg-emerald-600 text-white shadow-[0_0_12px_rgba(16,185,129,0.4)]'
                : 'text-neutral-400 hover:text-neutral-200 hover:bg-neutral-800'
            }`}
          >
            <BarChart2 className="w-3.5 h-3.5" />
            <span>单币 OI 增减仓节奏卡片</span>
          </button>
        </div>

        {/* Token Selector Pills */}
        <div className="flex items-center gap-1.5 overflow-x-auto py-0.5">
          <span className="text-[11px] text-neutral-500 font-sans mr-1">切换标的:</span>
          {tokens.map((t) => {
            const isSelected = selectedTokenId === t.id;
            return (
              <button
                key={t.id}
                onClick={() => setSelectedTokenId(t.id)}
                className={`px-2.5 py-1 rounded-lg text-xs font-mono font-bold flex items-center gap-1.5 transition-all ${
                  isSelected
                    ? 'bg-neutral-800 text-amber-300 border border-amber-500/40 shadow-sm'
                    : 'bg-neutral-950 text-neutral-400 hover:text-neutral-200 border border-neutral-800'
                }`}
              >
                <TokenIcon symbol={t.symbol} size="xs" />
                <span>{t.symbol}</span>
                <span className={`text-[10px] ${t.change24h >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                  {t.change24h >= 0 ? `+${t.change24h}%` : `${t.change24h}%`}
                </span>
              </button>
            );
          })}
        </div>
      </div>

      {/* 3. Main Content Display Body */}
      <div className="flex-1 overflow-y-auto space-y-3 pr-1">
        {/* Tab 1: Order Flow & Binance/OKX Heatmap */}
        {currentTab === 'orderflow_depth' && (
          <div className="space-y-3">
            {/* Quick Target Info Bar */}
            <div className="flex flex-wrap items-center justify-between bg-neutral-900/60 px-4 py-2 rounded-xl border border-neutral-800 text-xs">
              <div className="flex items-center gap-3">
                <TokenIcon symbol={selectedToken.symbol} size="sm" />
                <div>
                  <span className="text-sm font-bold text-neutral-100 font-mono">
                    {selectedToken.symbol} / USDT
                  </span>
                  <span className="text-neutral-400 text-xs ml-2">({selectedToken.fullName})</span>
                </div>
                <span className="text-neutral-600">|</span>
                <span className="text-neutral-400">
                  来源交易所: <b className="text-neutral-200 font-mono">{selectedToken.exchange}</b>
                </span>
                <span className="text-neutral-600">|</span>
                <span className="text-neutral-400">
                  现价: <b className="text-neutral-100 font-mono text-sm">${selectedToken.price.toLocaleString()}</b>
                </span>
              </div>

              <div className="text-neutral-500 text-[11px] font-mono">
                数据源: Binance Futures + OKX Swap 深度聚合
              </div>
            </div>

            {/* Embedded Order Flow Heatmap Component */}
            <OrderFlowHeatmap token={selectedToken} />
          </div>
        )}

        {/* Tab 2: Market-Wide 24H OI Matrix Heatmap */}
        {currentTab === 'market_oi_matrix' && (
          <div className="space-y-3">
            <div className="p-3.5 bg-neutral-900/90 rounded-xl border border-neutral-800 space-y-3 shadow-md">
              <div className="flex flex-wrap items-center justify-between border-b border-neutral-800 pb-2.5">
                <div>
                  <h3 className="font-bold text-xs text-neutral-100 flex items-center gap-2">
                    <Flame className="w-4 h-4 text-amber-400" />
                    <span>全市场代币 24H OI 逐小时机构增减仓热力矩阵</span>
                  </h3>
                  <p className="text-[10px] text-neutral-400 mt-0.5">
                    一览全市场所有监控标的在过去 24 小时每小时的资金持仓流向（绿色增仓、红色减仓），一眼看透主力在哪个币上集结。
                  </p>
                </div>

                <div className="flex items-center gap-3 text-xs">
                  <div className="flex items-center gap-1.5 text-[11px] text-neutral-400">
                    <span className="w-2.5 h-2.5 rounded bg-emerald-600"></span>
                    <span>强增仓 (&gt; +8%)</span>
                    <span className="w-2.5 h-2.5 rounded bg-emerald-900 ml-2"></span>
                    <span>温和增仓</span>
                    <span className="w-2.5 h-2.5 rounded bg-neutral-800 ml-2"></span>
                    <span>平盘</span>
                    <span className="w-2.5 h-2.5 rounded bg-rose-900 ml-2"></span>
                    <span>温和减仓</span>
                    <span className="w-2.5 h-2.5 rounded bg-rose-600 ml-2"></span>
                    <span>急剧减仓 (&lt; -8%)</span>
                  </div>
                </div>
              </div>

              {/* Matrix Table */}
              <div className="overflow-x-auto">
                <table className="w-full text-left font-mono text-xs border-collapse">
                  <thead>
                    <tr className="border-b border-neutral-800 text-[11px] text-neutral-500">
                      <th className="py-2 px-3 w-32">标的代币</th>
                      <th className="py-2 px-3 w-28">总OI持仓</th>
                      {marketMatrixData[0].hours.map((h, i) => (
                        <th key={i} className="py-2 px-1 text-center font-normal text-[10px]">
                          {h.timeStr.slice(0, 2)}h
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-neutral-850">
                    {marketMatrixData.map(({ token: t, hours }) => (
                      <tr key={t.id} className="hover:bg-neutral-800/40 transition-colors">
                        <td className="py-2.5 px-3">
                          <div className="flex items-center gap-2">
                            <TokenIcon symbol={t.symbol} size="xs" />
                            <span className="font-bold text-neutral-200">{t.symbol}</span>
                            <span className={`text-[10px] ${t.change24h >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                              {t.change24h >= 0 ? `+${t.change24h}%` : `${t.change24h}%`}
                            </span>
                          </div>
                        </td>
                        <td className="py-2.5 px-3 text-neutral-300 font-bold">
                          {formatWanYi(t.oi.totalUsd)}
                        </td>
                        {hours.map((h, i) => (
                          <td key={i} className="py-2.5 px-1">
                            <div
                              title={`${t.symbol} ${h.timeStr}: ${h.rate >= 0 ? '+' : ''}${h.rate}% (${formatWanYi(h.deltaUsd)})`}
                              className={`h-7 rounded flex items-center justify-center text-[10px] cursor-pointer transition-transform hover:scale-110 ${getMatrixCellColor(
                                h.rate
                              )}`}
                            >
                              {h.rate >= 0 ? `+${h.rate}` : h.rate}
                            </div>
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}

        {/* Tab 3: Token Single OI Heatmap */}
        {currentTab === 'token_oi_single' && (
          <div className="space-y-3">
            <OIHeatmap token={selectedToken} />
          </div>
        )}
      </div>

      {/* 4. Footer Note */}
      <div className="text-[11px] text-neutral-500 border-t border-neutral-800 pt-2 flex flex-wrap items-center justify-between gap-2">
        <span>全网热力图大类 • 实时聚合币安（Binance）与欧易（OKX）合约盘口大单、清算强平带与订单流。</span>
        <span className="font-mono text-neutral-600">热力图专区 • 实时监控中</span>
      </div>
    </div>
  );
};
