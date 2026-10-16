/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * 单策略工作台 · **资金分析视图**：策略资金曲线 vs 买入持有基准、回撤曲线、月度收益。
 *
 * 图形实现（2026-10-16 收敛）
 * ────────────────────────
 * 净值 / 基准 / 回撤**改用项目既有的 lightweight-charts**（官方 `LineSeries` / `AreaSeries`），
 * 时间轴缩放与平移走**官方原生**实现（滚轮缩放、拖动平移、`setVisibleRange` 定位）；
 * 不再自行实现一套交互坐标引擎（旧手写 SVG 视窗已移除）。
 *
 * 数据来源（全部真实，无合成）
 * ──────────────────────────
 * · 策略线：`GET /backtests/{id}/equity` → `items[].total_equity`（账本 equity 表）；
 * · 基准线：同响应的 `benchmark_equity[].equity`（**同成本**买入持有：手续费+滑点+真实资金费）；
 * · 回撤线：由**后台权益序列**按峰值口径展开，**初始峰值取 `/metrics.initial_equity`**；
 *   展开后的峰值必须与 `/metrics.max_drawdown_pct` 逐位一致，页面实时核对并给出徽标；
 * · 月度：`/metrics.monthly_returns.months[]`（账户月界净值口径，`partial_month` 明确区间收益），
 *   表格**复用旧热力图** `MonthlyReturnsHeatmap`。
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AreaSeries,
  ColorType,
  CrosshairMode,
  LineSeries,
  LineStyle,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type MouseEventParams,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts';
import { AlertCircle, TrendingUp } from 'lucide-react';
import type { LabBenchmark, LabEquityPoint, LabMetrics, LabMonthlyReturns } from '../api/labBacktest';
import { MonthlyReturnsHeatmap } from './BacktestReviewView';

const fmtNum = (v: number | null | undefined, digits = 2): string =>
  v === null || v === undefined || !Number.isFinite(v)
    ? '—'
    : v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });

const fmtPct = (v: number | null | undefined, digits = 4): string =>
  v === null || v === undefined || !Number.isFinite(v) ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(digits)}%`;

const fmtUtcDay = (ms: number): string => {
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
};

interface MergedPoint {
  t: number;
  strat: number;
  bench: number | null;
  drawdownPct: number;
}

export interface LabFundingPanelProps {
  points: LabEquityPoint[] | null;
  benchmarkSeries: { t: number; equity: number }[] | null;
  benchmarkMeta: LabBenchmark | null;
  metrics: LabMetrics | null;
  monthly: LabMonthlyReturns | null;
  loading: boolean;
  error: string | null;
  /** 该月**平仓笔数**（按平仓时间，来自逐笔账本；仅用于复用旧热力图的补充事实列）。 */
  monthCloseCounts?: Record<string, number> | null;
  /** 所选 run 的真实窗口（UTC 毫秒）——用于判定每个自然月是否被区间截断。 */
  windowStartMs?: number | null;
  windowEndMs?: number | null;
}

/** 某自然月（`YYYY-MM`）的 UTC 边界。 */
function monthBounds(ym: string): { startMs: number; endMs: number } | null {
  const [y, m] = ym.split('-').map(Number);
  if (!Number.isFinite(y) || !Number.isFinite(m) || m < 1 || m > 12) return null;
  const startMs = Date.UTC(y, m - 1, 1);
  const endMs = m === 12 ? Date.UTC(y + 1, 0, 1) : Date.UTC(y, m, 1);
  return { startMs, endMs };
}

export const LabFundingPanel: React.FC<LabFundingPanelProps> = ({
  points,
  benchmarkSeries,
  benchmarkMeta,
  metrics,
  monthly,
  loading,
  error,
  monthCloseCounts,
  windowStartMs = null,
  windowEndMs = null,
}) => {
  const [mode, setMode] = useState<'both' | 'underwater'>('both');
  const [hover, setHover] = useState<MergedPoint | null>(null);
  const [viewLabel, setViewLabel] = useState<string | null>(null);

  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const stratRef = useRef<ISeriesApi<'Line'> | null>(null);
  const benchRef = useRef<ISeriesApi<'Line'> | null>(null);
  const ddRef = useRef<ISeriesApi<'Area'> | null>(null);
  const byTimeRef = useRef<Map<number, MergedPoint>>(new Map());

  /** 合并策略线 + 基准线（按真实时间戳对齐；基准缺失的时点保持 null，不插值）。 */
  const merged: MergedPoint[] = useMemo(() => {
    if (!points || points.length === 0) return [];
    const benchByT = new Map<number, number>();
    for (const b of benchmarkSeries ?? []) {
      if (Number.isFinite(b?.t) && Number.isFinite(b?.equity)) benchByT.set(b.t, b.equity);
    }
    const out: MergedPoint[] = [];
    // 初始峰值 = 后台 `/metrics.initial_equity`（真实初始权益），不是首点；缺失才退回首点。
    let peak: number | null =
      metrics?.initial_equity !== null && metrics?.initial_equity !== undefined && Number.isFinite(metrics.initial_equity)
        ? metrics.initial_equity
        : null;
    for (const p of points) {
      if (!Number.isFinite(p?.ts_ms) || !Number.isFinite(p?.total_equity)) continue;
      peak = peak === null ? p.total_equity : Math.max(peak, p.total_equity);
      const dd = peak && peak !== 0 ? ((peak - p.total_equity) / peak) * 100 : 0;
      out.push({
        t: p.ts_ms,
        strat: p.total_equity,
        bench: benchByT.has(p.ts_ms) ? (benchByT.get(p.ts_ms) as number) : null,
        drawdownPct: dd,
      });
    }
    return out;
  }, [points, benchmarkSeries, metrics?.initial_equity]);

  const hasBench = useMemo(() => merged.some((p) => p.bench !== null), [merged]);
  const benchComplete = useMemo(() => merged.length > 0 && merged.every((p) => p.bench !== null), [merged]);
  /** 有真实序列才渲染图表容器（无数据时走空态分支）。 */
  const hasData = merged.length > 0;

  /** 展开回撤曲线的峰值 vs 后台 `/metrics.max_drawdown_pct` —— 实时核对，不一致就说不一致。 */
  const ddCheck = useMemo(() => {
    if (merged.length === 0) return null;
    const seriesMax = Math.max(...merged.map((p) => p.drawdownPct));
    const backend = metrics?.max_drawdown_pct ?? null;
    if (backend === null) return { seriesMax, backend: null as number | null, match: null as boolean | null };
    return { seriesMax, backend, match: Math.abs(seriesMax - backend) < 1e-9 };
  }, [merged, metrics]);

  /** 末点 vs 汇总（最终权益）一致性。 */
  const tailCheck = useMemo(() => {
    if (merged.length === 0) return null;
    const last = merged[merged.length - 1];
    const stratOk =
      metrics?.final_equity === null || metrics?.final_equity === undefined
        ? null
        : Math.abs(last.strat - metrics.final_equity) < 1e-6;
    const benchOk =
      !benchComplete || benchmarkMeta?.final_equity === null || benchmarkMeta?.final_equity === undefined
        ? null
        : Math.abs((last.bench as number) - benchmarkMeta.final_equity) < 1e-6;
    return { last, stratOk, benchOk };
  }, [merged, metrics, benchmarkMeta, benchComplete]);

  /* ── 创建官方图表 ──
   * ⚠️ 依赖必须是 `hasData`，不能是 `[]`：无真实序列时组件走**空态分支**，图表容器
   * 不在 DOM 里（`containerRef.current === null`）。若写成 `[]`，首次挂载（数据未到）
   * 时容器不存在 ⇒ 直接 return ⇒ 数据到达后图表永远不会被创建（页面只剩一片空白）。
   * 用 `chartRef` 防重复创建；`hasData` 由 true→false 时清理并销毁实例。 */
  useEffect(() => {
    if (!hasData) return undefined;
    const container = containerRef.current;
    if (!container || chartRef.current) return undefined;

    const chart = createChart(container, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: '#ffffff' },
        textColor: '#586880',
        attributionLogo: false,
      },
      grid: { vertLines: { color: '#eef2f8' }, horzLines: { color: '#e7ecf3' } },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: '#94a3b8', width: 1, style: LineStyle.Dashed, labelBackgroundColor: '#334155' },
        horzLine: { color: '#94a3b8', width: 1, style: LineStyle.Dashed, labelBackgroundColor: '#334155' },
      },
      rightPriceScale: { borderColor: '#d4dbe6', minimumWidth: 76 },
      timeScale: { borderColor: '#d4dbe6', timeVisible: true, secondsVisible: false, rightOffset: 0 },
      handleScroll: true,
      handleScale: true,
    });

    const strat = chart.addSeries(LineSeries, {
      color: '#10b981',
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: true,
      title: '策略净值',
    });
    const bench = chart.addSeries(LineSeries, {
      color: '#94a3b8',
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      priceLineVisible: false,
      lastValueVisible: true,
      title: '买入持有基准',
    });
    // 回撤：负值 + 独立价格轴；`invertFilledArea` ⇒ 填充到 0 线一侧（经典水下图观感）
    const dd = chart.addSeries(AreaSeries, {
      priceScaleId: 'dd',
      lineColor: '#f43f5e',
      topColor: 'rgba(244, 63, 94, 0.22)',
      bottomColor: 'rgba(244, 63, 94, 0.02)',
      invertFilledArea: true,
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: true,
      title: '回撤 %',
    });
    chart.priceScale('dd').applyOptions({ visible: false, scaleMargins: { top: 0.08, bottom: 0.02 } });

    chartRef.current = chart;
    stratRef.current = strat;
    benchRef.current = bench;
    ddRef.current = dd;

    const handler = (param: MouseEventParams<Time>) => {
      const sec = typeof param.time === 'number' ? (param.time as number) : null;
      if (sec === null) {
        setHover(null);
        return;
      }
      const p = byTimeRef.current.get(sec);
      setHover(p ?? null);
    };
    chart.subscribeCrosshairMove(handler);

    const reportView = () => {
      const r = chart.timeScale().getVisibleRange();
      if (r && typeof r.from === 'number' && typeof r.to === 'number') {
        setViewLabel(`${fmtUtcDay((r.from as number) * 1000)} → ${fmtUtcDay((r.to as number) * 1000)}`);
      }
    };
    chart.timeScale().subscribeVisibleTimeRangeChange(reportView);

    return () => {
      chart.unsubscribeCrosshairMove(handler);
      chart.remove();
      chartRef.current = null;
      stratRef.current = null;
      benchRef.current = null;
      ddRef.current = null;
    };
  }, [hasData]);

  /* ── 落数据（真实序列；每次 run 切换整表替换后 fitContent）── */
  useEffect(() => {
    const strat = stratRef.current;
    const bench = benchRef.current;
    const dd = ddRef.current;
    const chart = chartRef.current;
    if (!strat || !bench || !dd || !chart) return;

    const byTime = new Map<number, MergedPoint>();
    const stratData: { time: UTCTimestamp; value: number }[] = [];
    const benchData: { time: UTCTimestamp; value: number }[] = [];
    const ddData: { time: UTCTimestamp; value: number }[] = [];
    for (const p of merged) {
      const sec = Math.floor(p.t / 1000) as UTCTimestamp;
      byTime.set(sec, p);
      stratData.push({ time: sec, value: p.strat });
      if (p.bench !== null) benchData.push({ time: sec, value: p.bench });
      ddData.push({ time: sec, value: -p.drawdownPct });
    }
    byTimeRef.current = byTime;

    strat.setData(stratData);
    bench.setData(benchData);
    dd.setData(ddData);
    if (stratData.length > 0) chart.timeScale().fitContent();
  }, [merged]);

  /* ── 模式切换（官方 visible + 价格轴可见性；不重建图表）── */
  useEffect(() => {
    const chart = chartRef.current;
    const strat = stratRef.current;
    const bench = benchRef.current;
    const dd = ddRef.current;
    if (!chart || !strat || !bench || !dd) return;
    const both = mode === 'both';
    strat.applyOptions({ visible: both });
    bench.applyOptions({ visible: both && hasBench });
    dd.applyOptions({ visible: !both });
    chart.priceScale('right').applyOptions({ visible: both });
    chart.priceScale('dd').applyOptions({ visible: !both });
    // 依赖含 `hasData`：图表是在数据到达后才创建的，模式必须在其后重新应用一次，
    // 否则「净值对比」模式下回撤 Area 会沿用默认 visible=true 一起画出来。
  }, [mode, hasBench, hasData]);

  /* ── 快速区间：官方 setVisibleRange（原生时间轴）── */
  const applyQuickRange = useCallback(
    (bars: number | null) => {
      const chart = chartRef.current;
      if (!chart || merged.length === 0) return;
      if (bars === null) {
        chart.timeScale().fitContent();
        return;
      }
      const last = merged[merged.length - 1].t;
      const first = merged[Math.max(0, merged.length - 1 - bars)].t;
      chart.timeScale().setVisibleRange({
        from: Math.floor(first / 1000) as UTCTimestamp,
        to: Math.floor(last / 1000) as UTCTimestamp,
      });
    },
    [merged],
  );

  /* ── 月度：把真实 `months[]` 适配成旧热力图入参（复用 `MonthlyReturnsHeatmap`）── */
  const heatmapData = useMemo(() => {
    const months = monthly?.months ?? [];
    if (months.length === 0) return null;
    const out: Record<string, Record<string, { pnl: number | null; pnl_usd: number | null; count: number; basis?: string; month_end_equity?: number | null }>> = {};
    for (const m of months) {
      const [yr, mm] = m.month.split('-');
      if (!yr || !mm) continue;
      out[yr] = out[yr] ?? {};
      const pnlUsd =
        m.month_end_equity !== null && m.basis_equity !== null ? m.month_end_equity - m.basis_equity : null;
      out[yr][mm] = {
        pnl: m.return_pct,
        pnl_usd: pnlUsd,
        count: monthCloseCounts?.[m.month] ?? 0,
        basis: m.basis_note ?? undefined,
        month_end_equity: m.month_end_equity,
      };
    }
    return out;
  }, [monthly, monthCloseCounts]);

  /**
   * 月度收益是**乘性**序列 ⇒ 区间累计必须**复合**，不能相加。
   * 这里复合各月收益并与 `/metrics.net_return_pct`（同区间真实收益）实时核对。
   */
  const compoundCheck = useMemo(() => {
    const ms = monthly?.months ?? [];
    if (ms.length === 0) return null;
    let prod = 1;
    let any = false;
    for (const m of ms) {
      if (m.return_pct === null || m.return_pct === undefined || !Number.isFinite(m.return_pct)) continue;
      prod *= 1 + m.return_pct / 100;
      any = true;
    }
    if (!any) return null;
    const compoundPct = (prod - 1) * 100;
    const real = metrics?.net_return_pct ?? null;
    return {
      compoundPct,
      real,
      match: real === null ? (null as boolean | null) : Math.abs(compoundPct - real) < 1e-9,
    };
  }, [monthly, metrics]);

  /**
   * 每个月的**真实完整性**：由所选 run 的窗口与自然月边界推出（不是只看后台 `partial_month`）。
   * 30 天窗口常常「首月被起点截断、末月被终点截断」，末月不能被标成完整月。
   */
  const monthRows = useMemo(() => {
    const ms = monthly?.months ?? [];
    return ms.map((m) => {
      const b = monthBounds(m.month);
      const effStart = b && windowStartMs !== null ? Math.max(b.startMs, windowStartMs) : b?.startMs ?? null;
      const effEnd = b && windowEndMs !== null ? Math.min(b.endMs, windowEndMs) : b?.endMs ?? null;
      const cutStart = !!(b && windowStartMs !== null && windowStartMs > b.startMs);
      const cutEnd = !!(b && windowEndMs !== null && windowEndMs < b.endMs);
      return {
        row: m,
        effStart,
        effEnd,
        cutStart,
        cutEnd,
        isFull: !!b && !cutStart && !cutEnd,
      };
    });
  }, [monthly, windowStartMs, windowEndMs]);

  const cur = hover ?? (merged.length > 0 ? merged[merged.length - 1] : null);
  const eqBase = metrics?.initial_equity ?? (merged.length > 0 ? merged[0].strat : null);
  const stratRet = cur && eqBase ? ((cur.strat - eqBase) / eqBase) * 100 : null;
  const benchRet = cur && eqBase && cur.bench !== null ? ((cur.bench - eqBase) / eqBase) * 100 : null;

  if (merged.length === 0) {
    return (
      <div
        className="bg-white border border-neutral-200 rounded-lg shadow-sm p-6 flex flex-col items-center justify-center gap-1 text-xs text-neutral-500"
        data-testid="lab-funding-panel"
      >
        <AlertCircle className="w-4 h-4 text-neutral-400" />
        <span>{loading ? '正在读取真实净值曲线…' : '暂无真实净值曲线（后台未返回 equity / benchmark）'}</span>
        {error && <span className="text-rose-600">取数错误：{error}</span>}
        <span className="text-[10px] text-neutral-400">不绘制任何合成 / 兜底曲线</span>
      </div>
    );
  }

  return (
    <div
      className="bg-white border border-neutral-200 rounded-lg shadow-sm overflow-hidden text-neutral-100"
      data-testid="lab-funding-panel"
    >
      <div className="px-3 py-2 border-b border-neutral-200 bg-neutral-50 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 flex-wrap">
          <TrendingUp className="w-4 h-4 text-emerald-600" />
          <h3 className="text-xs font-bold text-neutral-100">资金分析 · 策略净值 vs 买入持有基准（同标的同期 · 同成本）</h3>
          {ddCheck && (
            <span
              className={`px-1.5 py-0.5 rounded border text-[10px] font-mono ${
                ddCheck.match === true
                  ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
                  : ddCheck.match === false
                    ? 'bg-rose-50 text-rose-700 border-rose-200'
                    : 'bg-neutral-100 text-neutral-700 border-neutral-200'
              }`}
              title="回撤曲线由后台权益序列展开（初始峰值取 /metrics.initial_equity）；峰值与 /metrics.max_drawdown_pct 实时核对"
            >
              回撤核对：
              {ddCheck.match === true
                ? '与 /metrics 一致'
                : ddCheck.match === false
                  ? `不一致（曲线 ${ddCheck.seriesMax.toFixed(6)}% vs 后台 ${fmtNum(ddCheck.backend, 6)}%）`
                  : '后台未给 max_drawdown_pct'}
            </span>
          )}
          {tailCheck && (
            <span
              className={`px-1.5 py-0.5 rounded border text-[10px] font-mono ${
                tailCheck.stratOk === true && (tailCheck.benchOk === null || tailCheck.benchOk === true)
                  ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
                  : 'bg-rose-50 text-rose-700 border-rose-200'
              }`}
              title="曲线末点必须等于 /metrics 的汇总值"
            >
              末点核对：{tailCheck.stratOk === true ? '策略=汇总' : '策略≠汇总'}
              {tailCheck.benchOk === null ? '' : tailCheck.benchOk ? ' · 基准=汇总' : ' · 基准≠汇总'}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <div className="flex items-center bg-white border border-neutral-200 rounded p-0.5 text-[11px]">
            <button
              onClick={() => setMode('both')}
              className={`px-2 py-0.5 rounded font-medium ${mode === 'both' ? 'bg-emerald-600 text-white font-bold' : 'text-neutral-400 hover:text-neutral-100'}`}
            >
              净值对比
            </button>
            <button
              onClick={() => setMode('underwater')}
              className={`px-2 py-0.5 rounded font-medium ${mode === 'underwater' ? 'bg-emerald-600 text-white font-bold' : 'text-neutral-400 hover:text-neutral-100'}`}
            >
              水下回撤
            </button>
          </div>
          <div className="flex items-center bg-white border border-neutral-200 rounded p-0.5 text-[11px]">
            <button onClick={() => applyQuickRange(null)} className="px-2 py-0.5 rounded text-neutral-400 hover:text-neutral-100">
              全部
            </button>
            <button
              onClick={() => applyQuickRange(96)}
              className="px-2 py-0.5 rounded text-neutral-400 hover:text-neutral-100"
              title="最近 96 根 15m（约 1 天）"
            >
              最近1天
            </button>
            <button
              onClick={() => applyQuickRange(288)}
              className="px-2 py-0.5 rounded text-neutral-400 hover:text-neutral-100"
              title="最近 288 根 15m（约 3 天）"
            >
              最近3天
            </button>
          </div>
          <span className="text-[10px] text-neutral-400 font-mono">
            {viewLabel ? `视窗 ${viewLabel}` : `${merged.length} 点`} · 滚轮缩放 · 拖动平移（官方时间轴）
          </span>
        </div>
      </div>

      {/* 读数条 */}
      <div className="px-3 py-1.5 border-b border-neutral-200 bg-white text-[11px] font-mono flex flex-wrap items-center gap-x-4 gap-y-1">
        <span className="text-neutral-500">时刻 {cur ? fmtUtcDay(cur.t) : '—'} UTC</span>
        <span className="flex items-center gap-1">
          <span className="w-2 h-2 rounded-full bg-emerald-500 inline-block" />
          <span className="text-neutral-400">策略净值</span>
          <b className="text-neutral-100">{cur ? `$${fmtNum(cur.strat, 2)}` : '—'}</b>
          <span className={stratRet !== null && stratRet >= 0 ? 'text-emerald-600' : 'text-rose-600'}>({fmtPct(stratRet, 2)})</span>
        </span>
        <span className="flex items-center gap-1">
          <span className="w-2 h-0.5 bg-neutral-400 inline-block" />
          <span className="text-neutral-500">买入持有基准</span>
          {cur?.bench === null || cur === null ? (
            <span className="text-neutral-400">该时点无基准数据</span>
          ) : (
            <>
              <span className="text-neutral-300">${fmtNum(cur.bench, 2)}</span>
              <span className={benchRet !== null && benchRet >= 0 ? 'text-neutral-400' : 'text-rose-500'}>({fmtPct(benchRet, 2)})</span>
            </>
          )}
        </span>
        <span className="flex items-center gap-1">
          <span className="text-neutral-500">水下回撤</span>
          <b className="text-rose-600">-{cur ? cur.drawdownPct.toFixed(3) : '—'}%</b>
        </span>
      </div>

      <div ref={containerRef} className="w-full h-[260px] bg-white" data-testid="lab-funding-chart" />

      {/* 口径 / 一致性说明 */}
      <div className="px-3 py-2 border-t border-neutral-200 bg-neutral-50 text-[10px] text-neutral-400 space-y-0.5">
        <div>
          基准：{benchmarkMeta?.benchmark_kind ?? '—'} · 净收益 {fmtPct(benchmarkMeta?.net_return_pct, 4)} · 最终权益 $
          {fmtNum(benchmarkMeta?.final_equity, 2)} · 最大回撤 -{fmtNum(benchmarkMeta?.max_drawdown_pct, 4)}%
          {benchmarkMeta?.cost_model ? ` · 成本口径：${benchmarkMeta.cost_model}` : ''}
        </div>
        {!hasBench && <div className="text-amber-700">后台未返回基准序列 ⇒ 只画策略线，不合成基准。</div>}
        {hasBench && !benchComplete && (
          <div className="text-amber-700">基准序列存在缺时点 ⇒ **不插值、不连线**，缺时点处基准为空。</div>
        )}
        <div>
          策略末点 ${fmtNum(tailCheck?.last.strat, 2)}（/metrics.final_equity ${fmtNum(metrics?.final_equity, 2)}）· 回撤曲线峰值 -
          {fmtNum(ddCheck?.seriesMax, 6)}%（/metrics.max_drawdown_pct -{fmtNum(ddCheck?.backend, 6)}%）
        </div>
        <div className="text-neutral-400">
          回撤曲线由**后台权益序列**按峰值口径展开（初始峰值 = /metrics.initial_equity；不重算交易/止损）；
          图形与缩放/平移走项目既有 lightweight-charts 官方实现。颜色沿用本项目既有约定：盈/正为绿、亏/负为红。
        </div>
      </div>

      {/* 月度收益：复用旧热力图（**复合年累计口径**）+ 后台完整性/基准说明 */}
      <div className="px-3 py-2 border-t border-neutral-200 space-y-2">
        {compoundCheck && (
          <div
            className={`rounded px-2 py-1 border text-[10px] font-mono flex flex-wrap items-center gap-2 ${
              compoundCheck.match === true
                ? 'bg-emerald-50 border-emerald-200 text-emerald-800'
                : compoundCheck.match === false
                  ? 'bg-rose-50 border-rose-200 text-rose-700'
                  : 'bg-neutral-50 border-neutral-200 text-neutral-400'
            }`}
            data-testid="lab-period-return"
          >
            <span className="font-bold">
              同区间累计（月度复合 Π(1+r)−1）：{fmtPct(compoundCheck.compoundPct, 6)}
            </span>
            <span>/metrics.net_return_pct：{fmtPct(compoundCheck.real, 6)}</span>
            <span>
              {compoundCheck.match === true
                ? '一致（月度收益是乘性序列，不能相加）'
                : compoundCheck.match === false
                  ? '不一致（见下）'
                  : '后台未给 net_return_pct'}
            </span>
            {compoundCheck.match === false && (
              <span className="text-neutral-400">
                差额 {fmtNum((compoundCheck.compoundPct - (compoundCheck.real as number)), 6)}%
                （月界切点与窗口端点不完全重合时会出现；以 /metrics 同区间真实收益为准）
              </span>
            )}
          </div>
        )}

        <MonthlyReturnsHeatmap
          monthly={heatmapData}
          ytdBasis="compound"
          basisNoteOverride="月度% = 该月**月界净值 / 上月月界净值 − 1**（后台月界口径，同一账户净值）；首月以**窗口起点初始权益**为基准并标 partial_month；YTD = 各月**复合** Π(1+r)−1（月界净值为乘性序列，相加会得出错误的年累计）。"
        />

        <div className="text-[10px] text-neutral-500 font-mono space-y-1">
          <div>来源 `/metrics.monthly_returns.months`：{monthly?.note ?? '—'}</div>
          {monthRows.map(({ row: m, effStart, effEnd, cutStart, cutEnd, isFull }) => (
            <div key={m.month} className="flex flex-wrap items-center gap-2">
              <span className="font-bold text-neutral-300">{m.month}</span>
              <span>区间收益 {fmtPct(m.return_pct, 6)}</span>
              <span
                className={`px-1.5 py-0.5 rounded border ${
                  isFull ? 'bg-neutral-100 text-neutral-700 border-neutral-200' : 'bg-amber-50 text-amber-800 border-amber-200'
                }`}
              >
                {isFull
                  ? '完整自然月'
                  : `部分月：${cutStart ? '起点在月中（区间从 ' + fmtUtcDay(windowStartMs as number) + ' 开始）' : ''}${cutStart && cutEnd ? '；' : ''}${cutEnd ? '终点在月中（区间到 ' + fmtUtcDay(windowEndMs as number) + ' 结束）' : ''}`}
              </span>
              {effStart !== null && effEnd !== null && (
                <span className="text-neutral-500">
                  实际覆盖 {fmtUtcDay(effStart)} → {fmtUtcDay(effEnd)} UTC
                </span>
              )}
              <span className="text-neutral-400">后台 partial_month={String(m.partial_month)}</span>
              <span className="text-neutral-500">{m.basis_note ?? '—'}</span>
            </div>
          ))}
          {monthCloseCounts && Object.keys(monthCloseCounts).length > 0 && (
            <div className="text-neutral-400">
              热力图「笔数」= 该月**平仓笔数**（按平仓时间，来自逐笔账本）；净值变化额 = 期末净值 − 基准净值（均为后台字段）。
            </div>
          )}
          {monthRows.length > 0 && (
            <div className="text-neutral-400">
              完整性由所选 run 的**真实窗口**与自然月边界推出（不只信后台 partial_month）：30 天窗口通常首月被起点、末月被终点各截一次，末月不得标成完整月。
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default LabFundingPanel;
