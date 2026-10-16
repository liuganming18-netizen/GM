/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * 回测复盘看板 —— **只展示服务端真实账本/曲线**（2026-10-15 任务四：真实展示）。
 *
 * 清理的假展示（原实现）：
 *   · 合成 BTC 基准曲线：`initEquity * (1 + sin(progress*8)*0.02 + progress*0.015)`；
 *   · 固定月度收益矩阵：硬编码 2026-01..10 / 2025-10..12 的 pnl 与笔数；
 *   · 默认绩效兜底：净收益 2.32 / 回撤 0.83 / 夏普 2.34 / 盈亏比 2.18 / 胜率 58.3 / 收益因子 2.18；
 *   · 多空胜率 61.2% / 54.5%（凭空）；
 *   · 假「运行回测」反馈（扫描 K 线 4709 根 / 成交 38 笔 / 耗时 320ms）；
 *   · 无效「参数调优」抽屉（止损 ATR / 延迟 / 风险 / Short 开关，改动不影响任何后端计算）；
 *   · 持仓时长兜底 '1h 15m'、盈亏兜底 0、初始权益兜底 20000。
 *
 * 现规则：一律消费 `GET /backtests` 真实字段（summary/equity_curve/monthly_returns/
 * arm_comparison/symbol_attribution/funnel/halted_info），缺失显示 `—`，无数据显示显式空态，
 * 绝不用任何默认值或合成曲线把空数据包装成「漂亮成绩」。
 *
 * 口径声明：
 *   · `mode=history`      = **历史研究回测**（研究窗口账本，**非正式生产策略收益**）；
 *   · `mode=paper_review` = 服务端模拟盘复盘；
 *   · `mode=signal_logs`  = 实时信号审计流（信号 → 意图 → 成交漏斗）。
 */

import React, { useMemo, useState, useCallback, useRef } from 'react';
import {
  History,
  TrendingUp,
  Calendar,
  Layers,
  AlertCircle,
  RefreshCw,
  ArrowUpRight,
  ArrowDownRight,
  Search,
  Info,
} from 'lucide-react';
import { useEndpoint } from '../api/useEndpoint';
import { BacktestWorkbench } from './BacktestWorkbench';
import type {
  RawArmComparison,
  RawBacktestReview,
  RawBacktestSummary,
  RawBacktestTrade,
  RawEquityPoint,
  RawSymbolAttribution,
} from '../api/types';
import { TokenIcon } from './TokenIcon';

const REFRESH_MS = 15_000;

// ── 格式化：未知一律 '—'，绝不补 0 / 默认值 ─────────────────────────────────
const fmtNum = (v: number | null | undefined, digits = 2): string => {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  return v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
};

const fmtUsd = (v: number | null | undefined, digits = 2): string =>
  v === null || v === undefined || !Number.isFinite(v) ? '—' : `$${fmtNum(v, digits)}`;

const fmtSignedUsd = (v: number | null | undefined, digits = 2): string => {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  const sign = v > 0 ? '+' : v < 0 ? '-' : '';
  return `${sign}$${fmtNum(Math.abs(v), digits)}`;
};

const fmtPct = (v: number | null | undefined, digits = 2, showPlus = true): string => {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  const prefix = showPlus && v > 0 ? '+' : '';
  return `${prefix}${v.toFixed(digits)}%`;
};

/** 时间戳（ms 或 ISO）→ `YYYY-MM-DD HH:mm:ss`（本地时区，与逐笔表一致）；无法解析 ⇒ '—'。 */
const fmtDateTime = (ms: number | string | null | undefined): string => {
  if (ms === null || ms === undefined || ms === '') return '—';
  const ts = typeof ms === 'string' ? Date.parse(ms) : ms;
  if (!Number.isFinite(ts) || (ts as number) <= 0) return '—';
  const d = new Date(ts as number);
  if (Number.isNaN(d.getTime())) return '—';
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};

/**
 * 模块 A：净值与回撤曲线（**只用真实点**）。
 *
 * 基准线：仅当后端为**全部**点都给出真实 `benchmark_equity` 时才绘制；否则不画，
 * 并在图例标注「无数据」。绝不合成任何基准曲线。
 */
const EquityChart: React.FC<{
  points: RawEquityPoint[];
  initEquity: number | null;
  viewMode: 'both' | 'underwater';
}> = ({ points, initEquity, viewMode }) => {
  const svgRef = useRef<SVGSVGElement>(null);
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);

  const hasBenchmark = useMemo(
    () => points.length > 0 && points.every((p) => p.benchmark_equity !== null && p.benchmark_equity !== undefined),
    [points],
  );

  const handleMouseMove = useCallback(
    (e: React.MouseEvent<SVGSVGElement>) => {
      if (!svgRef.current || points.length <= 1) return;
      const rect = svgRef.current.getBoundingClientRect();
      if (rect.width <= 0) return;
      const relX = (e.clientX - rect.left) / rect.width;
      const svgX = relX * 640;
      const normX = Math.max(0, Math.min(1, (svgX - 45) / (640 - 45 - 16)));
      setHoverIndex(Math.round(normX * (points.length - 1)));
    },
    [points.length],
  );

  if (points.length === 0) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center p-8 text-xs text-neutral-500 font-sans h-full gap-1">
        <AlertCircle className="w-4 h-4 text-neutral-400" />
        <span>暂无真实净值曲线（服务端未返回 equity_curve）</span>
        <span className="text-[10px] text-neutral-400">不绘制任何合成/兜底曲线</span>
      </div>
    );
  }

  const width = 640;
  const height = 200;
  const paddingLeft = 45;
  const paddingRight = 16;
  const paddingTop = 20;
  const paddingBottom = 26;
  const chartW = width - paddingLeft - paddingRight;
  const chartH = height - paddingTop - paddingBottom;

  const equities = points.map((p) => p.equity);
  const benchmarks = hasBenchmark ? points.map((p) => p.benchmark_equity as number) : [];
  const drawdowns = points.map((p) => p.drawdown_pct);

  const eqBase = initEquity ?? equities[0];
  const minEq = Math.min(...equities, ...benchmarks, eqBase * 0.98);
  const maxEq = Math.max(...equities, ...benchmarks, eqBase * 1.02);
  const eqRange = maxEq - minEq || 1;
  const maxDd = Math.max(...drawdowns, 0.1);

  const getX = (idx: number) => paddingLeft + (idx / (points.length - 1 || 1)) * chartW;
  const getEqY = (val: number) => paddingTop + chartH - ((val - minEq) / eqRange) * chartH;
  const getDdY = (ddPct: number) => paddingTop + (ddPct / maxDd) * chartH;

  const eqPolyline = points.map((p, i) => `${getX(i).toFixed(1)},${getEqY(p.equity).toFixed(1)}`).join(' ');
  const eqArea = `${getX(0).toFixed(1)},${height - paddingBottom} ${eqPolyline} ${getX(points.length - 1).toFixed(1)},${height - paddingBottom}`;
  const btcPolyline = hasBenchmark
    ? points.map((p, i) => `${getX(i).toFixed(1)},${getEqY(p.benchmark_equity as number).toFixed(1)}`).join(' ')
    : '';
  const ddPolyline = points.map((p, i) => `${getX(i).toFixed(1)},${getDdY(p.drawdown_pct).toFixed(1)}`).join(' ');
  const ddArea = `${getX(0).toFixed(1)},${paddingTop} ${ddPolyline} ${getX(points.length - 1).toFixed(1)},${paddingTop}`;

  const lastIndex = points.length - 1;
  const curIdx = hoverIndex !== null && hoverIndex >= 0 && hoverIndex <= lastIndex ? hoverIndex : lastIndex;
  const curPoint = points[curIdx];
  const curX = getX(curIdx);
  const curEqY = getEqY(curPoint.equity);
  const curBtcY = hasBenchmark ? getEqY(curPoint.benchmark_equity as number) : curEqY;
  const curDdY = getDdY(curPoint.drawdown_pct);

  const stratReturn = ((curPoint.equity - eqBase) / eqBase) * 100;
  const btcReturn = hasBenchmark ? (((curPoint.benchmark_equity as number) - eqBase) / eqBase) * 100 : null;
  const alpha = btcReturn === null ? null : stratReturn - btcReturn;

  return (
    <div className="relative w-full h-full flex flex-col justify-between select-none">
      <div className="flex items-center justify-between text-[11px] font-mono px-3 py-1.5 bg-neutral-50 border-b border-neutral-200">
        <div className="flex items-center gap-4">
          <span className="text-neutral-500 font-sans">
            时刻:{' '}
            <span className="font-mono text-neutral-200 font-semibold">
              {curPoint.time ? curPoint.time.replace('T', ' ').replace('Z', '') : fmtDateTime(curPoint.timestamp)}
            </span>
          </span>
          <div className="flex items-center gap-1.5">
            <span className="w-2 h-2 rounded-full bg-emerald-500 inline-block" />
            <span className="text-neutral-400 font-sans">策略净值:</span>
            <span className="font-bold text-neutral-100">
              ${fmtNum(curPoint.equity, 2)}
            </span>
            <span className={`font-semibold ml-1 ${stratReturn >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
              ({fmtPct(stratReturn, 2)})
            </span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="w-2 h-0.5 bg-neutral-400 inline-block" />
            <span className="text-neutral-500 font-sans">BTC基准:</span>
            {hasBenchmark ? (
              <>
                <span className="text-neutral-300 font-mono">${fmtNum(curPoint.benchmark_equity as number, 2)}</span>
                <span className={`text-[10px] ${(btcReturn as number) >= 0 ? 'text-neutral-400' : 'text-rose-500'}`}>
                  ({fmtPct(btcReturn, 2)})
                </span>
              </>
            ) : (
              <span className="text-neutral-400 font-sans">无数据</span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-1">
            <span className="text-neutral-500 font-sans">超额收益 (Alpha):</span>
            <span className={`font-bold ${alpha !== null && alpha >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
              {alpha === null ? '—' : fmtPct(alpha, 2)}
            </span>
          </div>
          <div className="flex items-center gap-1">
            <span className="text-neutral-500 font-sans">水线回撤:</span>
            <span className="font-bold text-rose-600">-{curPoint.drawdown_pct.toFixed(2)}%</span>
          </div>
        </div>
      </div>

      <div className="flex-1 relative cursor-crosshair">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${width} ${height}`}
          className="w-full h-full"
          preserveAspectRatio="none"
          onMouseMove={handleMouseMove}
          onMouseLeave={() => setHoverIndex(null)}
        >
          <defs>
            <linearGradient id="eqGradient" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#10b981" stopOpacity="0.22" />
              <stop offset="100%" stopColor="#10b981" stopOpacity="0.0" />
            </linearGradient>
            <linearGradient id="ddGradient" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#f43f5e" stopOpacity="0.25" />
              <stop offset="100%" stopColor="#f43f5e" stopOpacity="0.02" />
            </linearGradient>
          </defs>

          <line x1={paddingLeft} y1={paddingTop} x2={width - paddingRight} y2={paddingTop} stroke="#f1f5f9" strokeWidth="1" />
          <line x1={paddingLeft} y1={paddingTop + chartH * 0.5} x2={width - paddingRight} y2={paddingTop + chartH * 0.5} stroke="#f1f5f9" strokeWidth="1" strokeDasharray="3 3" />
          <line x1={paddingLeft} y1={height - paddingBottom} x2={width - paddingRight} y2={height - paddingBottom} stroke="#e2e8f0" strokeWidth="1" />

          {viewMode === 'both' ? (
            <>
              <text x={paddingLeft - 6} y={paddingTop + 4} textAnchor="end" className="text-[9px] fill-neutral-400 font-mono">${Math.round(maxEq)}</text>
              <text x={paddingLeft - 6} y={paddingTop + chartH * 0.5 + 3} textAnchor="end" className="text-[9px] fill-neutral-400 font-mono">${Math.round((maxEq + minEq) / 2)}</text>
              <text x={paddingLeft - 6} y={height - paddingBottom} textAnchor="end" className="text-[9px] fill-neutral-400 font-mono">${Math.round(minEq)}</text>
            </>
          ) : (
            <>
              <text x={paddingLeft - 6} y={paddingTop + 4} textAnchor="end" className="text-[9px] fill-neutral-400 font-mono">0%</text>
              <text x={paddingLeft - 6} y={paddingTop + chartH * 0.5 + 3} textAnchor="end" className="text-[9px] fill-neutral-400 font-mono">-{(maxDd / 2).toFixed(1)}%</text>
              <text x={paddingLeft - 6} y={height - paddingBottom} textAnchor="end" className="text-[9px] fill-neutral-400 font-mono">-{maxDd.toFixed(1)}%</text>
            </>
          )}

          {viewMode === 'both' ? (
            <>
              {hasBenchmark && (
                <polyline points={btcPolyline} fill="none" stroke="#94a3b8" strokeWidth="1.5" strokeDasharray="4 4" strokeLinecap="round" />
              )}
              <polygon points={eqArea} fill="url(#eqGradient)" />
              <polyline points={eqPolyline} fill="none" stroke="#10b981" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
              {hasBenchmark && <circle cx={curX} cy={curBtcY} r="3" fill="#94a3b8" />}
              <circle cx={curX} cy={curEqY} r="4.5" fill="#10b981" stroke="#ffffff" strokeWidth="2" />
            </>
          ) : (
            <>
              <polygon points={ddArea} fill="url(#ddGradient)" />
              <polyline points={ddPolyline} fill="none" stroke="#f43f5e" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
              <circle cx={curX} cy={curDdY} r="4" fill="#f43f5e" stroke="#ffffff" strokeWidth="1.5" />
            </>
          )}

          {hoverIndex !== null && (
            <line x1={curX} y1={paddingTop} x2={curX} y2={height - paddingBottom} stroke="#64748b" strokeWidth="1" strokeDasharray="2 2" />
          )}
        </svg>
      </div>
    </div>
  );
};

/**
 * 模块 B：月度收益热力矩阵（**只用后端 `monthly_returns` 真实聚合**）。
 * 后端无数据 ⇒ 显式空态，绝不填任何硬编码月份。
 *
 * 2026-10-16：导出供**单策略工作台**复用（同一套浅色 GM 视觉与同一「月界净值」口径），
 * 组件本身行为未改。
 */
export const MonthlyReturnsHeatmap: React.FC<{
  monthly: Record<string, Record<string, {
    /** 月界净值收益%（首月无上月月界 ⇒ `null`） */
    pnl: number | null;
    /** 月界净值变化额（首月 ⇒ `null`） */
    pnl_usd: number | null;
    /** 该月**平仓笔数**（按平仓时间，补充事实，非收益口径） */
    count: number;
    basis?: string;
    month_end_equity?: number | null;
    realized_pnl_by_close_month?: number | null;
  }>> | null;
  /**
   * **可选**年累计口径（默认 `'sum'`，与旧通道逐字一致）。
   *
   * 为什么需要：月度收益是**乘性**序列（月界净值环比），简单相加会得出错误的年累计 ——
   * 实测 ETHUSDT 30 天 run `bt_20261016T030224Z_0741dc7b`：各月 `+8.73822467%` 与
   * `-5.77947945%` 相加得 `+2.958745%`，而真实同区间 `net_return_pct = +2.453721%`；
   * 复合 `Π(1+r)−1 = +2.453721%` 才与真实一致。
   * 新工作台传 `'compound'`；旧通道不传 ⇒ 行为不变。
   */
  ytdBasis?: 'sum' | 'compound';
  /** **可选**底部口径说明覆盖（新通道首月有基准，不再出现「首月显示 —」）。 */
  basisNoteOverride?: string | null;
}> = ({ monthly, ytdBasis = 'sum', basisNoteOverride = null }) => {
  const months = ['01', '02', '03', '04', '05', '06', '07', '08', '09', '10', '11', '12'];
  const years = useMemo(() => Object.keys(monthly ?? {}).sort((a, b) => b.localeCompare(a)), [monthly]);
  const hasData = years.length > 0;

  // 后端「缺结果返回缺失态」⇒ `pnl` 可能是 `null`（如首月无上月月界）。
  // 旧写法只判 `undefined` ⇒ `null.toFixed()` 会直接抛错；改为 `== null` 一并覆盖。
  const getHeatmapColor = (pct: number | null | undefined) => {
    if (pct === null || pct === undefined) return 'bg-neutral-50 text-neutral-300';
    if (pct > 3.0) return 'bg-emerald-600 text-white font-bold';
    if (pct > 1.5) return 'bg-emerald-500/80 text-white font-medium';
    if (pct > 0) return 'bg-emerald-100 text-emerald-800 font-semibold';
    if (pct === 0) return 'bg-neutral-100 text-neutral-700';
    if (pct > -1.5) return 'bg-rose-100 text-rose-800 font-semibold';
    return 'bg-rose-500 text-white font-bold';
  };

  return (
    <div className="flex flex-col h-full bg-white border border-neutral-200 rounded-lg p-3 shadow-sm select-none text-neutral-100">
      <div className="flex items-center justify-between pb-2 border-b border-neutral-200 mb-2">
        <div className="flex items-center gap-1.5">
          <Calendar className="w-4 h-4 text-emerald-600" />
          <h3 className="text-xs font-bold text-neutral-100">月度收益热力矩阵 (Monthly Returns)</h3>
        </div>
        <span className="text-[10px] text-neutral-400 font-mono">色阶: 盈(绿) / 亏(红) · 来源 /backtests</span>
      </div>

      {!hasData ? (
        <div className="flex-1 flex flex-col items-center justify-center text-[11px] text-neutral-500 font-sans gap-1">
          <AlertCircle className="w-4 h-4 text-neutral-400" />
          <span>暂无真实月度收益聚合</span>
          <span className="text-[10px] text-neutral-400">不填任何默认月份</span>
        </div>
      ) : (
        <div className="flex-1 overflow-x-auto">
          <table className="w-full text-center text-[10px] font-mono border-collapse">
            <thead>
              <tr className="text-neutral-500 border-b border-neutral-100">
                <th className="py-1 text-left font-sans pl-1">年份</th>
                {months.map((m) => (
                  <th key={m} className="py-1 px-1 font-normal">{parseInt(m)}月</th>
                ))}
                <th className="py-1 px-1 font-bold text-neutral-200 font-sans">YTD</th>
              </tr>
            </thead>
            <tbody>
              {years.map((yr) => {
                const yrMap = monthly?.[yr] ?? {};
                // `'sum'`（旧通道默认）= 各月直接相加；`'compound'` = Π(1+r)−1（月界净值为乘性序列）。
                let ytdSum = 0;
                let ytdCompound = 1;
                let any = false;
                months.forEach((m) => {
                  const p = yrMap[m]?.pnl;
                  // 缺失月（`null`）不计入累计，也不把累计变成「已测得」——否则会少算/假算。
                  if (p !== null && p !== undefined) {
                    ytdSum += p;
                    ytdCompound *= 1 + p / 100;
                    any = true;
                  }
                });
                const ytd = ytdBasis === 'compound' ? (ytdCompound - 1) * 100 : ytdSum;
                return (
                  <tr key={yr} className="border-b border-neutral-100/60">
                    <td className="py-1 text-left font-bold text-neutral-200 font-mono pl-1">{yr}</td>
                    {months.map((m) => {
                      const cell = yrMap[m];
                      const val = cell?.pnl;
                      return (
                        <td key={m} className="py-1 px-0.5">
                          <div
                            className={`w-full py-1 rounded text-[10px] transition-transform hover:scale-105 ${getHeatmapColor(val)}`}
                            title={cell ? `${yr}-${m}: ${fmtPct(val, 2)}（${cell.count} 笔平仓 · 净值变化 ${fmtSignedUsd(cell.pnl_usd, 2)}）` : '无该月成交'}
                          >
                            {val !== null && val !== undefined ? `${val > 0 ? '+' : ''}${val.toFixed(1)}%` : '—'}
                          </div>
                        </td>
                      );
                    })}
                    <td className="py-1 px-1 font-bold">
                      <span className={`px-1.5 py-0.5 rounded ${!any ? 'text-neutral-400' : ytd >= 0 ? 'text-emerald-700 bg-emerald-50' : 'text-rose-700 bg-rose-50'}`}>
                        {any ? fmtPct(ytd, 2) : '—'}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div className="text-[9px] text-neutral-400 mt-1.5 font-sans">
            {basisNoteOverride ??
              '月度% = 该月**月界净值 / 上月月界净值 − 1**（后端口径，同一账户净值）；首月无上月月界 ⇒ 显示 —；YTD = 各月加总（仅有效月份）。'}
          </div>
        </div>
      )}
    </div>
  );
};

/** 8 项核心指标（**全部来自后端 summary**，缺失显示 '—'）。 */
function buildMetrics(
  summary: RawBacktestSummary | null,
  hasCurve: boolean,
): { title: string; value: string; sub: string; color: string }[] {
  const g = (v: number | null | undefined): number | null =>
    v === null || v === undefined || !Number.isFinite(v) ? null : v;

  const netReturn = g(summary?.net_return_pct);
  const maxDd = g(summary?.max_drawdown_pct);
  const sharpeRaw = g(summary?.sharpe_ratio);
  const profitFactorRaw = g(summary?.profit_factor);
  const winRateRaw = g(summary?.win_rate_pct);
  const winLossRaw = g(summary?.win_loss_ratio);
  const closed = g(summary?.closed_trades);
  const totalFees = g(summary?.total_fees);

  // 无成交 / 无曲线时，派生统计量无意义 ⇒ 显示 '—'，不把 0 当作已测得的成绩。
  const hasTrades = closed !== null && closed > 0;
  const sharpe = hasCurve ? sharpeRaw : null;
  const profitFactor = hasTrades ? profitFactorRaw : null;
  const winRate = hasTrades ? winRateRaw : null;
  const winLoss = hasTrades ? winLossRaw : null;
  const calmar = netReturn !== null && maxDd !== null && maxDd > 0 ? netReturn / maxDd : null;

  return [
    { title: '净收益率 (Net Return)', value: fmtPct(netReturn, 2), sub: '已平仓净收益 / 初始权益', color: netReturn === null ? 'text-neutral-400' : netReturn >= 0 ? 'text-emerald-600' : 'text-rose-600' },
    { title: '最大回撤 (Max DD)', value: maxDd === null ? '—' : `-${maxDd.toFixed(2)}%`, sub: '峰值动态水线', color: maxDd === null ? 'text-neutral-400' : 'text-rose-600' },
    { title: '夏普比率 (Sharpe)', value: sharpe === null ? '—' : `${sharpe}`, sub: hasCurve ? '净值差分年化' : '无曲线 ⇒ —', color: 'text-neutral-100' },
    { title: '卡尔玛比率 (Calmar)', value: calmar === null ? '—' : calmar.toFixed(2), sub: maxDd === null || maxDd === 0 ? '无回撤 ⇒ —' : '净收益 / 最大回撤', color: 'text-neutral-100' },
    { title: '交易胜率 (Win Rate)', value: winRate === null ? '—' : `${winRate.toFixed(1)}%`, sub: closed === null ? '已平笔数未知' : `已平 ${closed} 笔`, color: 'text-neutral-100' },
    { title: '盈亏比 (W/L Ratio)', value: winLoss === null ? '—' : `${winLoss}`, sub: hasTrades ? '均盈利 / 均亏损' : '无成交 ⇒ —', color: 'text-neutral-100' },
    { title: '收益因子 (Profit Factor)', value: profitFactor === null ? '—' : `${profitFactor}`, sub: hasTrades ? '总盈利 / 总亏损' : '无成交 ⇒ —', color: 'text-neutral-100' },
    { title: '累计摩擦费用 (Fees)', value: totalFees === null ? '—' : fmtUsd(totalFees, 2), sub: '手续费 + 资金费', color: 'text-neutral-300' },
  ];
}

/**
 * 旧通道「回测复盘」阅读入口（`GET /backtests?mode=history|paper_review|signal_logs`）。
 *
 * 2026-10-16：本组件降级为**历史阅读**面板 —— 页面默认进入新的「单策略工作台」
 * （`BacktestWorkbench`，走 `POST/GET /api/v1/backtests` 研究回测新通道）。
 * 旧通道的数据是 3a–3d / 模拟盘 / 信号审计的**既有账本**，**不得**被当成新策略结果；
 * 因此两个入口在视觉与文案上完全分开，且新工作台不消费旧通道任何字段。
 */
const LegacyReviewView: React.FC = () => {
  const [activeTab, setActiveTab] = useState<'history' | 'paper_review' | 'signal_logs'>('paper_review');
  const [timeRange, setTimeRange] = useState<'7D' | '30D' | '90D' | 'ALL'>('ALL');
  const [chartView, setChartView] = useState<'both' | 'underwater'>('both');

  const [searchSymbol, setSearchSymbol] = useState('');
  const [filterDirection, setFilterDirection] = useState<'all' | 'long' | 'short'>('all');
  const [filterPnl, setFilterPnl] = useState<'all' | 'win' | 'loss'>('all');
  const [filterArm, setFilterArm] = useState<'all' | '3a' | '3b' | '3c' | '3d'>('all');

  const endpointUrl = `/backtests?mode=${activeTab}&range=${timeRange}`;
  const q = useEndpoint<RawBacktestReview>(endpointUrl, REFRESH_MS);
  const data = q.data;

  const trades: RawBacktestTrade[] = useMemo(() => (data?.items ?? []) as RawBacktestTrade[], [data]);
  const curvePoints: RawEquityPoint[] = useMemo(() => (data?.equity_curve ?? []) as RawEquityPoint[], [data]);
  const summary: RawBacktestSummary | null = data?.summary ?? null;
  const armComparison: RawArmComparison[] = useMemo(() => (data?.arm_comparison ?? []) as RawArmComparison[], [data]);
  const monthly = data?.monthly_returns ?? null;
  const funnel = data?.funnel ?? null;
  const attribution = data?.symbol_attribution ?? null;
  const haltedInfo = data?.halted_info ?? null;

  /** 真实数据是否为空（用于显示显式空态，而不是把 0 包装成成绩）。 */
  const isEmpty = !!data && trades.length === 0 && curvePoints.length === 0 && armComparison.length === 0;

  const metrics = useMemo(() => buildMetrics(summary, curvePoints.length > 1), [summary, curvePoints.length]);

  const filteredTrades = useMemo(() => {
    return trades.filter((t) => {
      if (searchSymbol.trim()) {
        const query = searchSymbol.trim().toUpperCase();
        if (!t.symbol.toUpperCase().includes(query)) return false;
      }
      if (filterDirection === 'long' && !(t.direction === '做多' || t.direction === 'long')) return false;
      if (filterDirection === 'short' && !(t.direction === '做空' || t.direction === 'short')) return false;
      if (filterPnl === 'win' && !((t.pnl ?? 0) > 0)) return false;
      if (filterPnl === 'loss' && !((t.pnl ?? 0) < 0)) return false;
      if (filterArm !== 'all' && t.arm !== filterArm) return false;
      return true;
    });
  }, [trades, searchSymbol, filterDirection, filterPnl, filterArm]);

  const modeLabel =
    activeTab === 'history' ? '历史研究回测' : activeTab === 'paper_review' ? '实盘模拟仓复盘' : '实时信号审计流';
  const isHistoryResearch = activeTab === 'history';

  return (
    <div className="flex-1 flex flex-col h-full overflow-hidden bg-neutral-100 p-3 gap-2.5 select-none text-neutral-800 font-sans">
      {/* 1. 顶部控制条 */}
      <div className="flex flex-wrap items-center justify-between bg-white border border-neutral-200 rounded-lg px-3.5 py-2.5 gap-3 shadow-sm">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 rounded-lg bg-emerald-50 border border-emerald-200 flex items-center justify-center text-emerald-600">
              <History className="w-4 h-4" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-sm font-bold text-neutral-100 tracking-wide leading-tight">回测复盘 · 真实账本对账</h1>
                <span className="px-1.5 py-0.5 rounded bg-neutral-100 text-neutral-700 font-mono text-[10px] border border-neutral-200">
                  只读 /api/v1/backtests
                </span>
              </div>
              <span className="text-[11px] text-neutral-500 font-mono">
                全部数值取自服务端账本；缺失显示 —，无数据不造数
              </span>
            </div>
          </div>

          <div className="flex items-center gap-1 bg-neutral-100 p-1 rounded-md border border-neutral-200 text-xs font-medium ml-2">
            {([
              ['history', '历史研究回测'],
              ['paper_review', '模拟仓复盘'],
              ['signal_logs', '信号审计流'],
            ] as const).map(([id, label]) => (
              <button
                key={id}
                onClick={() => setActiveTab(id)}
                className={`px-2.5 py-1 rounded transition-colors ${
                  activeTab === id ? 'bg-white text-neutral-100 font-bold shadow-sm' : 'text-neutral-600 hover:text-neutral-900'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        <div className="flex items-center gap-2">
          <div className="flex items-center bg-neutral-100 p-0.5 rounded-md border border-neutral-200 text-xs font-mono font-medium">
            {(['7D', '30D', '90D', 'ALL'] as const).map((r) => (
              <button
                key={r}
                onClick={() => setTimeRange(r)}
                className={`px-2.5 py-1 rounded transition-colors ${
                  timeRange === r ? 'bg-emerald-600 text-white font-bold shadow-sm' : 'text-neutral-600 hover:text-neutral-900'
                }`}
              >
                {r}
              </button>
            ))}
          </div>
          {/* 仅重新拉取真实端点；不伪造「撮合计算」 */}
          <button
            onClick={() => void q.refresh()}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-white border border-neutral-200 text-xs font-medium text-neutral-300 hover:bg-neutral-50 transition-colors shadow-sm"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${q.loading ? 'animate-spin' : ''}`} />
            <span>刷新</span>
          </button>
        </div>
      </div>

      {/* 口径 / 状态条：明确数据来源与「非正式策略收益」声明 */}
      <div
        className={`rounded-lg px-3 py-2 text-[11px] flex flex-wrap items-center gap-x-4 gap-y-1 border ${
          isHistoryResearch ? 'bg-amber-50 border-amber-200 text-amber-800' : 'bg-white border-neutral-200 text-neutral-400'
        }`}
        data-testid="backtest-source-banner"
        data-mode={activeTab}
      >
        <span className="flex items-center gap-1 font-semibold">
          <Info className="w-3.5 h-3.5" />
          {modeLabel}
        </span>
        {isHistoryResearch && (
          <span className="text-amber-700">
            ⚠ 研究窗口历史账本，**非正式生产策略收益**；不得作为对外业绩口径。
          </span>
        )}
        <span className="font-mono">
          数据版本：{data?.data_version ?? '—'} · as_of：{data?.as_of ?? '—'}
        </span>
        {q.error && <span className="text-rose-600">取数错误：{q.error}</span>}
        {data?.quality?.note && <span className="text-neutral-500">口径：{data.quality.note}</span>}
        {haltedInfo && Object.keys(haltedInfo).length > 0 && (
          <span className="text-rose-600">
            停机臂：{Object.entries(haltedInfo).filter(([, v]) => v).map(([k, v]) => `${k}(${v})`).join('、') || '—'}
          </span>
        )}
      </div>

      {/* 2. 8 项核心指标 */}
      <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 gap-2" data-testid="backtest-metrics">
        {metrics.map((m, idx) => (
          <div key={idx} className="p-2.5 bg-white border border-neutral-200 rounded-lg space-y-1 shadow-sm">
            <span className="text-[11px] font-medium text-neutral-500 block truncate" title={m.title}>{m.title}</span>
            <div className={`text-lg font-bold font-mono tracking-tight ${m.color}`}>{m.value}</div>
            <span className="text-[10px] text-neutral-400 block truncate font-mono">{m.sub}</span>
          </div>
        ))}
      </div>

      {/* 3. 信号 → 成交漏斗（区分「计划下单」与「实际成交」） */}
      {funnel && (
        <div className="bg-white border border-neutral-200 rounded-lg px-3 py-2 shadow-sm flex flex-wrap items-center gap-x-6 gap-y-1 text-[11px] font-mono" data-testid="backtest-funnel">
          <span className="font-sans font-semibold text-neutral-300 flex items-center gap-1">
            <Layers className="w-3.5 h-3.5 text-emerald-600" />信号 → 成交漏斗
          </span>
          <span className="text-neutral-400">结构/信号：<b className="text-neutral-100">{funnel.signals_count}</b></span>
          <span className="text-neutral-400">计划下单(意图)：<b className="text-sky-700">{funnel.intents_count}</b></span>
          <span className="text-neutral-400">拒单：<b className="text-amber-700">{funnel.rejected_count}</b></span>
          <span className="text-neutral-400">实际成交(账本)：<b className="text-emerald-700">{funnel.filled_positions}</b></span>
          <span className="text-neutral-400 font-sans">（仅账本成交计入实际成交）</span>
        </div>
      )}

      {/* 4. 净值曲线 + 月度矩阵 */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-2.5 h-64 shrink-0">
        <div className="lg:col-span-7 bg-white border border-neutral-200 rounded-lg flex flex-col shadow-sm overflow-hidden">
          <div className="px-3 py-2 border-b border-neutral-200 flex items-center justify-between bg-neutral-50">
            <div className="flex items-center gap-2">
              <TrendingUp className="w-4 h-4 text-emerald-600" />
              <h2 className="text-xs font-bold text-neutral-100">策略净值走势 vs BTC 基准 (Equity & Benchmark)</h2>
            </div>
            <div className="flex items-center gap-1 bg-white border border-neutral-200 rounded p-0.5 text-[11px]">
              <button
                onClick={() => setChartView('both')}
                className={`px-2 py-0.5 rounded font-medium ${chartView === 'both' ? 'bg-emerald-600 text-white font-bold' : 'text-neutral-400 hover:text-neutral-100'}`}
              >
                净值对比
              </button>
              <button
                onClick={() => setChartView('underwater')}
                className={`px-2 py-0.5 rounded font-medium ${chartView === 'underwater' ? 'bg-emerald-600 text-white font-bold' : 'text-neutral-400 hover:text-neutral-100'}`}
              >
                水下回撤
              </button>
            </div>
          </div>
          <div className="flex-1 min-h-0">
            <EquityChart points={curvePoints} initEquity={summary?.initial_equity ?? null} viewMode={chartView} />
          </div>
        </div>

        <div className="lg:col-span-5 h-full">
          <MonthlyReturnsHeatmap monthly={monthly} />
        </div>
      </div>

      {/* 5. 四臂横向对标 + 单币归因 */}
      {armComparison.length > 0 && (
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-2.5 shrink-0">
          <div className="lg:col-span-8 bg-white border border-neutral-200 rounded-lg overflow-hidden shadow-sm" data-testid="arm-comparison">
            <div className="px-3 py-1.5 border-b border-neutral-200 bg-neutral-50 text-xs font-bold text-neutral-100 flex items-center gap-1.5">
              <Layers className="w-3.5 h-3.5 text-emerald-600" />四臂横向对标（来自账本）
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-[11px] font-mono">
                <thead className="bg-neutral-50 text-neutral-400 border-b border-neutral-200">
                  <tr>
                    <th className="py-1.5 px-2">臂</th>
                    <th className="py-1.5 px-2">规则</th>
                    <th className="py-1.5 px-2 text-right">初始</th>
                    <th className="py-1.5 px-2 text-right">期末</th>
                    <th className="py-1.5 px-2 text-right">净收益</th>
                    <th className="py-1.5 px-2 text-right">净收益率</th>
                    <th className="py-1.5 px-2 text-right">最大回撤</th>
                    <th className="py-1.5 px-2 text-right">已平/胜率</th>
                    <th className="py-1.5 px-2 text-right">盈亏比</th>
                    <th className="py-1.5 px-2 text-right">拒单</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-neutral-100">
                  {armComparison.map((a) => (
                    <tr key={a.arm} className={a.arm === 'ALL' ? 'bg-emerald-50/40 font-semibold' : ''}>
                      <td className="py-1.5 px-2 text-neutral-100">{a.label} <span className="text-neutral-400">({a.arm})</span></td>
                      <td className="py-1.5 px-2 text-neutral-500 font-sans">{a.rule}</td>
                      <td className="py-1.5 px-2 text-right">{fmtUsd(a.initial_equity, 0)}</td>
                      <td className="py-1.5 px-2 text-right">{fmtUsd(a.final_equity, 0)}</td>
                      <td className={`py-1.5 px-2 text-right ${a.net_profit >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>{fmtSignedUsd(a.net_profit, 2)}</td>
                      <td className={`py-1.5 px-2 text-right ${(a.net_return_pct ?? 0) >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>{fmtPct(a.net_return_pct, 2)}</td>
                      <td className="py-1.5 px-2 text-right text-rose-600">
                        {a.max_drawdown_pct === null || a.max_drawdown_pct === undefined
                          ? '—' : `-${fmtNum(a.max_drawdown_pct, 2)}%`}
                      </td>
                      <td className="py-1.5 px-2 text-right">
                        {a.closed_trades} / {a.win_rate_pct === null || a.win_rate_pct === undefined
                          ? '—' : `${fmtNum(a.win_rate_pct, 1)}%`}
                      </td>
                      <td className="py-1.5 px-2 text-right">{fmtNum(a.win_loss_ratio, 2)}</td>
                      <td className="py-1.5 px-2 text-right">{a.rejected_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="lg:col-span-4 bg-white border border-neutral-200 rounded-lg overflow-hidden shadow-sm" data-testid="symbol-attribution">
            <div className="px-3 py-1.5 border-b border-neutral-200 bg-neutral-50 text-xs font-bold text-neutral-100">
              单币种盈亏归因（Top / Bottom 5）
            </div>
            <div className="p-2 grid grid-cols-2 gap-2 text-[11px] font-mono">
              {(['top_winners', 'top_losers'] as const).map((k) => (
                <div key={k} className="space-y-1">
                  <div className={`font-sans font-semibold ${k === 'top_winners' ? 'text-emerald-700' : 'text-rose-700'}`}>
                    {k === 'top_winners' ? '贡献币种' : '拖累币种'}
                  </div>
                  {((attribution?.[k] ?? []) as RawSymbolAttribution[]).length === 0 ? (
                    <div className="text-neutral-400 font-sans">无数据</div>
                  ) : (
                    (attribution?.[k] ?? []).map((s) => (
                      <div key={s.symbol} className="flex items-center justify-between gap-1">
                        <span className="text-neutral-300 truncate">{s.symbol}</span>
                        <span className={s.net_pnl >= 0 ? 'text-emerald-600' : 'text-rose-600'}>{fmtSignedUsd(s.net_pnl, 2)}</span>
                      </div>
                    ))
                  )}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* 6. 逐笔明细表 */}
      <div className="flex-1 flex flex-col bg-white border border-neutral-200 rounded-lg overflow-hidden shadow-sm min-h-0">
        <div className="px-3 py-2 border-b border-neutral-200 flex flex-wrap items-center justify-between bg-neutral-50 gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <div className="flex items-center gap-1.5 mr-2">
              <h3 className="text-xs font-bold text-neutral-100">
                {activeTab === 'signal_logs' ? '实时策略信号审计流' : '逐笔成交与持仓明细'}
              </h3>
              <span className="px-2 py-0.5 rounded bg-emerald-50 text-emerald-700 border border-emerald-200 text-[10px] font-mono font-bold">
                共 {filteredTrades.length} 笔
              </span>
            </div>

            <div className="relative">
              <Search className="w-3.5 h-3.5 text-neutral-400 absolute left-2 top-2" />
              <input
                type="text"
                placeholder="搜索代币 (如 SOL)..."
                value={searchSymbol}
                onChange={(e) => setSearchSymbol(e.target.value)}
                className="pl-7 pr-2 py-1 text-xs rounded border border-neutral-200 bg-white focus:outline-none focus:border-emerald-500 font-mono w-36"
              />
            </div>

            <div className="flex items-center bg-white border border-neutral-200 rounded p-0.5 text-[11px] font-medium">
              {(['all', 'long', 'short'] as const).map((dir) => (
                <button
                  key={dir}
                  onClick={() => setFilterDirection(dir)}
                  className={`px-2 py-0.5 rounded ${filterDirection === dir ? 'bg-emerald-600 text-white font-bold' : 'text-neutral-400 hover:text-neutral-100'}`}
                >
                  {dir === 'all' ? '全部方向' : dir === 'long' ? '仅做多' : '仅做空'}
                </button>
              ))}
            </div>

            <div className="flex items-center bg-white border border-neutral-200 rounded p-0.5 text-[11px] font-medium">
              {(['all', 'win', 'loss'] as const).map((p) => (
                <button
                  key={p}
                  onClick={() => setFilterPnl(p)}
                  className={`px-2 py-0.5 rounded ${filterPnl === p ? 'bg-emerald-600 text-white font-bold' : 'text-neutral-400 hover:text-neutral-100'}`}
                >
                  {p === 'all' ? '全部盈亏' : p === 'win' ? '盈利单' : '亏损单'}
                </button>
              ))}
            </div>

            <div className="flex items-center bg-white border border-neutral-200 rounded p-0.5 text-[11px] font-medium font-mono">
              {(['all', '3a', '3b', '3c', '3d'] as const).map((arm) => (
                <button
                  key={arm}
                  onClick={() => setFilterArm(arm)}
                  className={`px-1.5 py-0.5 rounded ${filterArm === arm ? 'bg-emerald-600 text-white font-bold' : 'text-neutral-400 hover:text-neutral-100'}`}
                >
                  {arm === 'all' ? '全部臂' : arm}
                </button>
              ))}
            </div>
          </div>

          <div className="text-[11px] text-neutral-400 font-mono">
            {q.fetchedAt ? `最后同步 ${fmtDateTime(q.fetchedAt)}` : '尚未取数'} · 逐笔与持仓同源（服务端账本）
          </div>
        </div>

        <div className="flex-1 overflow-auto">
          <table className="w-full text-left text-xs">
            <thead className="bg-neutral-50 border-b border-neutral-200 text-neutral-400 font-semibold sticky top-0 select-none z-10">
              {activeTab === 'signal_logs' ? (
                <tr>
                  <th className="py-2 px-3">信号时间</th>
                  <th className="py-2 px-3">标的代币</th>
                  <th className="py-2 px-3">策略臂</th>
                  <th className="py-2 px-3">建议方向</th>
                  <th className="py-2 px-3">触发基准价</th>
                  <th className="py-2 px-3">执行状态</th>
                  <th className="py-2 px-3">信号角色 / 结构</th>
                </tr>
              ) : (
                <tr>
                  <th className="py-2 px-3">开仓时间</th>
                  <th className="py-2 px-3">标的代币</th>
                  <th className="py-2 px-3">策略臂</th>
                  <th className="py-2 px-3">方向</th>
                  <th className="py-2 px-3">开仓价 → 出场价</th>
                  <th className="py-2 px-3">持仓时长</th>
                  <th className="py-2 px-3">净盈亏 (ROI%)</th>
                  <th className="py-2 px-3">出场原因</th>
                </tr>
              )}
            </thead>
            <tbody className="divide-y divide-neutral-100 font-mono">
              {q.error && !data ? (
                <tr>
                  <td colSpan={activeTab === 'signal_logs' ? 7 : 8} className="text-center py-12 text-rose-500 text-xs font-sans">
                    取数失败：{q.error}
                  </td>
                </tr>
              ) : filteredTrades.length === 0 ? (
                <tr>
                  <td colSpan={activeTab === 'signal_logs' ? 7 : 8} className="text-center py-12 text-neutral-400 text-xs font-sans">
                    {trades.length === 0
                      ? (isEmpty
                          ? '暂无真实成交/信号记录（服务端账本为空）——不显示任何演示成绩。'
                          : '暂无真实成交记录（服务端账本为空）——不显示任何演示成绩。')
                      : '暂无符合条件的记录'}
                  </td>
                </tr>
              ) : (
                filteredTrades.map((row) => {
                  const isLong = row.direction === '做多' || row.direction === 'long';
                  const pnl = row.pnl;
                  const isProfit = pnl !== null && pnl !== undefined && pnl >= 0;

                  if (activeTab === 'signal_logs') {
                    return (
                      <tr key={row.id} data-trade-id={row.id} className="hover:bg-neutral-50/90 transition-colors">
                        <td className="py-2 px-3 text-neutral-500 text-[11px] whitespace-nowrap">{fmtDateTime(row.timestamp ?? row.time)}</td>
                        <td className="py-2 px-3 font-bold text-neutral-100 whitespace-nowrap">
                          <div className="flex items-center gap-1.5">
                            <TokenIcon symbol={row.symbol} size="xs" />
                            <span>{row.symbol}</span>
                          </div>
                        </td>
                        <td className="py-2 px-3 whitespace-nowrap">
                          <span className="px-1.5 py-0.5 rounded bg-neutral-100 text-neutral-700 text-[10px] font-bold border border-neutral-200">{row.arm}</span>
                        </td>
                        <td className="py-2 px-3 whitespace-nowrap">
                          <span className={`inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-bold ${isLong ? 'bg-emerald-50 text-emerald-700 border border-emerald-200' : 'bg-rose-50 text-rose-700 border border-rose-200'}`}>
                            {isLong ? <ArrowUpRight className="w-3 h-3" /> : <ArrowDownRight className="w-3 h-3" />}
                            {isLong ? '做多' : '做空'}
                          </span>
                        </td>
                        <td className="py-2 px-3 text-neutral-100 font-semibold whitespace-nowrap">{row.entry_price === null ? '—' : `$${row.entry_price}`}</td>
                        <td className="py-2 px-3 text-[11px] whitespace-nowrap">
                          <span className={`px-1.5 py-0.5 rounded font-sans font-medium ${row.status === 'enterable' ? 'bg-emerald-50 text-emerald-700 border border-emerald-200' : 'bg-neutral-100 text-neutral-700'}`}>
                            {row.status === 'enterable' ? '可入场执行' : (row.status ?? '—')}
                          </span>
                        </td>
                        <td className="py-2 px-3 text-neutral-500 text-[11px] whitespace-nowrap font-mono">{row.exit_reason ?? '—'}</td>
                      </tr>
                    );
                  }

                  return (
                    <tr key={row.id} data-trade-id={row.id} className="hover:bg-neutral-50/90 transition-colors">
                      <td className="py-2 px-3 text-neutral-500 text-[11px] whitespace-nowrap">{fmtDateTime(row.timestamp ?? row.time)}</td>
                      <td className="py-2 px-3 font-bold text-neutral-100 whitespace-nowrap">
                        <div className="flex items-center gap-1.5">
                          <TokenIcon symbol={row.symbol} size="xs" />
                          <span>{row.symbol}</span>
                        </div>
                      </td>
                      <td className="py-2 px-3 whitespace-nowrap">
                        <span className="px-1.5 py-0.5 rounded bg-neutral-100 text-neutral-700 text-[10px] font-bold border border-neutral-200">{row.arm}</span>
                      </td>
                      <td className="py-2 px-3 whitespace-nowrap">
                        <span className={`inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-bold ${isLong ? 'bg-emerald-50 text-emerald-700 border border-emerald-200' : 'bg-rose-50 text-rose-700 border border-rose-200'}`}>
                          {isLong ? <ArrowUpRight className="w-3 h-3" /> : <ArrowDownRight className="w-3 h-3" />}
                          {isLong ? '做多' : '做空'}
                        </span>
                      </td>
                      <td className="py-2 px-3 text-neutral-300 whitespace-nowrap">
                        <span className="text-neutral-500">{row.entry_price === null ? '—' : `$${row.entry_price}`}</span>
                        <span className="text-neutral-400 mx-1">→</span>
                        <span className="font-semibold text-neutral-100">{row.exit_price === null ? '—' : `$${row.exit_price}`}</span>
                      </td>
                      <td className="py-2 px-3 text-neutral-500 text-[11px] whitespace-nowrap">{row.duration_str ?? '—'}</td>
                      <td className="py-2 px-3 whitespace-nowrap font-semibold">
                        <span className={pnl === null || pnl === undefined ? 'text-neutral-400' : isProfit ? 'text-emerald-600' : 'text-rose-600'}>
                          {fmtSignedUsd(pnl, 2)}
                        </span>
                        {row.pnl_pct !== null && row.pnl_pct !== undefined && (
                          <span className={`text-[10px] ml-1 ${isProfit ? 'text-emerald-500' : 'text-rose-500'}`}>({fmtPct(row.pnl_pct, 2)})</span>
                        )}
                      </td>
                      <td className="py-2 px-3 text-[11px] whitespace-nowrap text-neutral-400">
                        <span className="px-1.5 py-0.5 rounded bg-neutral-100 text-neutral-700 font-sans">{row.status ?? '—'}</span>
                        <span className="text-neutral-400 text-[10px] ml-1.5 font-mono">{row.exit_reason ?? '—'}</span>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
};

/**
 * 回测复盘页（页面级容器）。
 *
 * 默认进入**单策略工作台**（新研究回测通道 `bt_api_v1`：真实 POST 创建 + 轮询 + 账本结果），
 * 旧通道（`?mode=history|paper_review|signal_logs`）保留为**历史阅读**入口 —— 两者数据源、
 * 身份与口径完全分离，旧账本绝不当成新策略结果。
 */
export const BacktestReviewView: React.FC = () => {
  const [view, setView] = useState<'workbench' | 'legacy'>('workbench');

  return (
    <div className="flex-1 flex flex-col h-full overflow-hidden bg-neutral-100">
      <div className="shrink-0 px-3 pt-3">
        <div className="flex flex-wrap items-center justify-between gap-2 bg-white border border-neutral-200 rounded-lg px-3.5 py-2 shadow-sm">
          <div className="flex items-center gap-2">
            <div className="flex items-center bg-neutral-100 p-1 rounded-md border border-neutral-200 text-xs font-medium">
              <button
                onClick={() => setView('workbench')}
                className={`px-2.5 py-1 rounded transition-colors ${
                  view === 'workbench' ? 'bg-white text-neutral-100 font-bold shadow-sm' : 'text-neutral-600 hover:text-neutral-900'
                }`}
                data-testid="review-tab-workbench"
              >
                单策略工作台（新）
              </button>
              <button
                onClick={() => setView('legacy')}
                className={`px-2.5 py-1 rounded transition-colors ${
                  view === 'legacy' ? 'bg-white text-neutral-100 font-bold shadow-sm' : 'text-neutral-600 hover:text-neutral-900'
                }`}
                data-testid="review-tab-legacy"
              >
                历史阅读（旧通道）
              </button>
            </div>
            <span className="text-[10px] text-neutral-400 font-mono">
              {view === 'workbench'
                ? 'WX_OI_RESONANCE_V1 · /api/v1/backtests（研究回测新通道）'
                : '3a–3d / 模拟盘 / 信号审计既有账本（旧通道；非新策略结果）'}
            </span>
          </div>
          <span className="text-[10px] text-neutral-400 font-mono">
            {view === 'workbench' ? '独立候选研究 · 非正式 M4' : '旧数据不冒充新策略'}
          </span>
        </div>
      </div>
      <div className="flex-1 min-h-0 overflow-auto">
        {view === 'workbench' ? <BacktestWorkbench /> : <LegacyReviewView />}
      </div>
    </div>
  );
};

