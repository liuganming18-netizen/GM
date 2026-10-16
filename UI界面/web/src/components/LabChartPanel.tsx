/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * 单策略工作台 · **执行视图**：真实 15m K 线 + 原始成交量 + 真实信号 + 开平仓标记 +
 * 保护止损轨迹。选中一笔交易 ⇒ 用现有 `ChartHandle.setViewport` 定位其 entry/exit 区间，
 * 并高亮该笔标记。
 *
 * 纪律
 * ────
 * · K 线/成交量/信号/开平仓/止损轨迹**全部**来自 `GET /backtests/{id}/chart` 的真实字段，
 *   前端不重算任何成交或止损。
 * · `v` 是**原始 base 成交量**（不是 USDT 名义额）⇒ 只填 `Candle.volume`，
 *   `volumeUsd` 保持 `null`（不用名义额冒名）。
 * · 止损轨迹用**现有 `onReady` 句柄**叠加官方 `LineSeries`（不重写 K 线绘制引擎）；
 *   不使用 SVG 假 K 线、不使用静态 demo。
 * · 没有固定止盈规则时明确显示「未配置固定止盈」，**不造 TP 点**。
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  LineSeries,
  LineStyle,
  LineType,
  type ISeriesApi,
  type SeriesMarker,
  type Time,
} from 'lightweight-charts';
import {
  CandleVolumeChart,
  buildCandleGrid,
  createChartSyncHub,
  toChartSeconds,
  type ChartHandle,
  type ChartHover,
  type ChartViewport,
} from './LightweightChart';
import type { Candle } from '../types/crypto';
import type { LabChart, LabChartTrade, LabOrderRow } from '../api/labBacktest';

/* ── 格式化（未知一律 '—'）────────────────────────────────────────────────── */
const fmtNum = (v: number | null | undefined, digits = 2): string =>
  v === null || v === undefined || !Number.isFinite(v)
    ? '—'
    : v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });

const fmtPct = (v: number | null | undefined, digits = 2): string =>
  v === null || v === undefined || !Number.isFinite(v) ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(digits)}%`;

const fmtUtc = (ms: number | null | undefined): string => {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms <= 0) return '—';
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return '—';
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
};

const fmtVolume = (v: number | null | undefined): string => {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(3)}M`;
  if (Math.abs(v) >= 1e3) return `${(v / 1e3).toFixed(3)}K`;
  return v.toFixed(4);
};

/** 真实 chart.candles → 前端 `Candle`（`volumeUsd` 保持 null，不用名义额冒名）。 */
export function labCandlesToBars(chart: LabChart | null): Candle[] {
  if (!chart?.candles) return [];
  const out: Candle[] = [];
  for (const c of chart.candles) {
    if (!Number.isFinite(c.t) || c.t <= 0) continue;
    if (![c.o, c.h, c.l, c.c].every((x) => typeof x === 'number' && Number.isFinite(x))) continue;
    out.push({
      time: new Date(c.t).toISOString(),
      timestamp: c.t,
      open: c.o,
      high: c.h,
      low: c.l,
      close: c.c,
      volume: Number.isFinite(c.v) ? c.v : null,
      volumeUsd: null,
      isClosed: true,
    });
  }
  return out;
}

/** 开/平仓 + 信号标记（真实 trades / signals ⇒ 官方 markers）。 */
function buildTradeMarkers(chart: LabChart | null, selectedTradeId: string | null): SeriesMarker<Time>[] {
  const out: SeriesMarker<Time>[] = [];
  if (!chart?.trades) return out;
  for (const t of chart.trades) {
    const sel = t.trade_id === selectedTradeId;
    if (Number.isFinite(t.entry?.t)) {
      out.push({
        id: `entry-${t.trade_id}`,
        time: toChartSeconds(t.entry.t),
        position: 'belowBar',
        shape: 'arrowUp',
        color: sel ? '#047857' : '#0b8a5e',
        text: sel ? '开仓(选中)' : '开仓',
        size: sel ? 2 : 1,
      });
    }
    if (Number.isFinite(t.exit?.t)) {
      out.push({
        id: `exit-${t.trade_id}`,
        time: toChartSeconds(t.exit.t),
        position: 'aboveBar',
        shape: 'arrowDown',
        color: sel ? '#b91c1c' : '#d92d48',
        text: sel ? '平仓(选中)' : '平仓',
        size: sel ? 2 : 1,
      });
    }
  }
  return out;
}

/** 保护止损轨迹（引擎实况记录，不重建、不造点）。 */
const LAB_STOP_COLOR = '#b45309';

/**
 * 订单/委托标记（账本 `orders` 表 ⇒ 官方 markers）。
 *
 * 与「成交」标记区分：成交用上下箭头（belowBar/aboveBar），订单用 **inBar 圆点**并按 `phase` 着色，
 * 便于在同一根 K 线上把「委托 → 成交」分层看出；同一 order 一行一标记，不做任何去重/合并。
 * 本策略为次根 15m 开盘撮合 ⇒ 成交价与订单价一致，圆点与箭头落在同一根上，属真实事实，不隐藏。
 */
const ORDER_PHASE_COLOR: Record<string, string> = {
  C_entry: '#1d4ed8',
  D_intrabar: '#7c3aed',
  EOD: '#64748b',
};

function buildOrderMarkers(orders: LabOrderRow[] | null | undefined): SeriesMarker<Time>[] {
  const out: SeriesMarker<Time>[] = [];
  if (!orders) return out;
  for (const o of orders) {
    if (!Number.isFinite(o.ts_ms)) continue;
    out.push({
      id: `ord-${o.order_id}`,
      time: toChartSeconds(o.ts_ms),
      position: 'inBar',
      shape: 'circle',
      color: ORDER_PHASE_COLOR[o.phase] ?? '#64748b',
      size: 1,
    });
  }
  return out;
}

export interface LabChartPanelProps {
  chart: LabChart | null;
  loading: boolean;
  error: string | null;
  missingReason: string | null;
  selectedTradeId: string | null;
  onSelectTrade: (tradeId: string | null) => void;
  /** 账本 `orders` 表（真实委托明细）；缺失 ⇒ 不画订单标记，不造点。 */
  orders?: LabOrderRow[] | null;
}

export const LabChartPanel: React.FC<LabChartPanelProps> = ({
  chart,
  loading,
  error,
  missingReason,
  selectedTradeId,
  onSelectTrade,
  orders = null,
}) => {
  const bars = useMemo(() => labCandlesToBars(chart), [chart]);
  const gridTimes = useMemo(() => buildCandleGrid(bars), [bars]);
  const viewKey = useMemo(
    () => (chart ? `lab:${chart.symbol}:${chart.exec_timeframe}:${chart.window?.start_ms ?? 0}` : 'lab:empty'),
    [chart],
  );
  /** 单图工作台：同步组只含这一张图（复用既有 hub 契约，行为与旧调用一致）。 */
  const syncHub = useMemo(() => createChartSyncHub(), [viewKey]);

  const [handle, setHandle] = useState<ChartHandle | null>(null);
  const [hover, setHover] = useState<ChartHover | null>(null);
  const [viewport, setViewport] = useState<ChartViewport | null>(null);
  const [locateInfo, setLocateInfo] = useState<{ fromMs: number; toMs: number; ok: boolean } | null>(null);

  const onReady = useCallback((h: ChartHandle) => setHandle(h), []);
  const onHover = useCallback((h: ChartHover | null) => setHover(h), []);
  const onViewportChange = useCallback((v: ChartViewport) => setViewport(v), []);

  const extraMarkers = useMemo(
    () => [...buildTradeMarkers(chart, selectedTradeId), ...buildOrderMarkers(orders)],
    [chart, selectedTradeId, orders],
  );

  const selectedTrade: LabChartTrade | null = useMemo(
    () => chart?.trades?.find((t) => t.trade_id === selectedTradeId) ?? null,
    [chart, selectedTradeId],
  );

  /* ── 止损轨迹叠加：用现有 onReady 句柄加官方 LineSeries，不重写 K 线引擎 ── */
  const overlayRef = useRef<ISeriesApi<'Line'>[]>([]);
  useEffect(() => {
    const api = handle?.chart;
    overlayRef.current = [];
    if (!api || !chart?.trades?.length) return;

    const created: ISeriesApi<'Line'>[] = [];
    for (const t of chart.trades) {
      const trace = (t.stop_trace ?? []).filter((p) => Number.isFinite(p.t) && Number.isFinite(p.stop));
      if (trace.length < 2) continue;
      const isSel = t.trade_id === selectedTradeId;
      try {
        const s = api.addSeries(LineSeries, {
          color: isSel ? LAB_STOP_COLOR : 'rgba(180, 83, 9, 0.35)',
          lineWidth: isSel ? 2 : 1,
          lineStyle: LineStyle.Solid,
          lineType: LineType.WithSteps,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
          title: isSel ? '保护止损(选中)' : '',
        });
        s.setData(trace.map((p) => ({ time: toChartSeconds(p.t), value: p.stop })));
        created.push(s);
      } catch {
        /* 官方 API 异常不得打断渲染 */
      }
    }
    overlayRef.current = created;

    return () => {
      for (const s of created) {
        try {
          api.removeSeries(s);
        } catch {
          /* 图已销毁：忽略 */
        }
      }
      overlayRef.current = [];
    };
  }, [handle, chart, selectedTradeId]);

  /* ── 选中交易 ⇒ 用现有 setViewport 定位 entry/exit 区间 ── */
  useEffect(() => {
    if (!selectedTrade || !handle) {
      setLocateInfo(null);
      return;
    }
    const PAD_MS = 3_600_000; // 前后各 1h 上下文
    const fromMs = selectedTrade.entry.t - PAD_MS;
    const toMs = selectedTrade.exit.t + PAD_MS;
    const ok = handle.setViewport(fromMs, toMs);
    setLocateInfo({ fromMs, toMs, ok });
  }, [selectedTrade, handle]);

  /* ── 图上点击 = 选中该 bar 上的真实开/平仓（精确命中才选中，避免误选）── */
  useEffect(() => {
    const api = handle?.chart;
    if (!api || !chart?.trades?.length) return;
    const byTime = new Map<number, string>();
    for (const t of chart.trades) {
      if (Number.isFinite(t.entry?.t)) byTime.set(toChartSeconds(t.entry.t), t.trade_id);
      if (Number.isFinite(t.exit?.t)) byTime.set(toChartSeconds(t.exit.t), t.trade_id);
    }
    const handler = (param: { time?: unknown }) => {
      const sec = typeof param?.time === 'number' ? (param.time as number) : null;
      if (sec === null) return;
      const id = byTime.get(sec);
      if (id) onSelectTrade(id);
    };
    try {
      api.subscribeClick(handler as never);
    } catch {
      return;
    }
    return () => {
      try {
        api.unsubscribeClick(handler as never);
      } catch {
        /* 图已销毁：忽略 */
      }
    };
  }, [handle, chart, onSelectTrade]);

  const hoveredBar = hover?.candle ?? null;
  const hoveredSignal = useMemo(() => {
    if (!hoveredBar || !chart?.signals) return null;
    return chart.signals.find((s) => s.t === hoveredBar.timestamp) ?? null;
  }, [hoveredBar, chart]);

  const tp = chart?.take_profit ?? null;
  const noFixedTp = tp ? tp.exists === false : null;

  return (
    <div
      className="bg-white border border-neutral-200 rounded-lg shadow-sm overflow-hidden text-neutral-100"
      data-testid="lab-chart-panel"
    >
      <div className="px-3 py-2 border-b border-neutral-200 bg-neutral-50 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 flex-wrap">
          <h3
            className="text-xs font-bold text-neutral-100"
            title="真实 K 线 / 成交量 / 信号 / 开平仓 / 保护止损轨迹；成交量 = 原始 base 量（不是 USDT 名义额）"
          >
            ④ 执行视图 · {chart?.symbol ?? '—'} · {chart?.exec_timeframe ?? '—'}
          </h3>
          <span
            className="px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700 border border-emerald-200 text-[10px] font-mono"
            title="来自后台真实 K 线载荷的根数（非合成 / 非静态 demo）"
          >
            {bars.length} 根
          </span>
          {chart?.trades && (
            <span className="px-1.5 py-0.5 rounded bg-sky-50 text-sky-700 border border-sky-200 text-[10px] font-mono">
              开平仓 {chart.trades.length} 笔
            </span>
          )}
          {orders && orders.length > 0 && (
            <span
              className="px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-700 border border-indigo-200 text-[10px] font-mono"
              title="委托标记为 inBar 圆点，按 phase 着色"
            >
              委托 {orders.length} 条
            </span>
          )}
          {selectedTrade && (
            <button
              onClick={() => onSelectTrade(null)}
              className="px-2 py-0.5 rounded border border-neutral-200 bg-white text-[10px] text-neutral-300 hover:bg-neutral-50"
            >
              清除选中
            </button>
          )}
          {/* 视窗范围：常显（这是「现在看的是哪一段」），交互提示收进 title */}
          <span
            className="text-[10px] text-neutral-400 font-mono"
            title="滚轮缩放 / 拖动平移（走项目既有 lightweight-charts 官方时间轴）"
          >
            {viewport ? `视窗 ${fmtUtc(viewport.fromMs)} → ${fmtUtc(viewport.toMs)}` : ''}
          </span>
        </div>
      </div>

      {/* 止盈口径 + 标记图例 + 成交时刻口径（**同一行**；口径原文移到 title，不占视野） */}
      <div className="px-3 py-1 border-b border-neutral-200 bg-white text-[10px] flex flex-wrap items-center gap-x-3 gap-y-1">
        {noFixedTp === true && (
          <span
            className="px-1.5 py-0.5 rounded bg-amber-50 text-amber-800 border border-amber-200 font-semibold"
            title={tp?.note ? `口径：${tp.note}` : undefined}
          >
            无固定止盈
          </span>
        )}
        {noFixedTp === false && (
          <span
            className="px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700 border border-emerald-200"
            title={tp?.note ? `口径：${tp.note}` : undefined}
          >
            止盈规则已配置
          </span>
        )}
        {noFixedTp === null && <span className="text-neutral-400">止盈规则字段缺失 ⇒ 不做假设</span>}
        <span className="text-neutral-500 flex flex-wrap items-center gap-x-2">
          <span className="font-semibold">图例</span>
          <span>▲ 开仓</span>
          <span>▼ 平仓</span>
          <span className="text-amber-700">— 保护止损</span>
          <span className="text-indigo-700">● C_entry</span>
          <span className="text-violet-700">● D_intrabar</span>
          <span className="text-neutral-500">● EOD</span>
        </span>
        {/* 时间精度：bar 级假设如实显示（行内折叠，口径不删、不占高） */}
        {chart?.time_precision && (
          <details className="text-[10px]" data-testid="lab-chart-timeprecision">
            <summary
              className="cursor-pointer text-neutral-500 hover:text-neutral-100 list-none"
              title="后台 time_precision 原值（bar 级假设，非 tick 精确）"
            >
              成交时刻口径 ▾
            </summary>
            <div className="text-[10px] space-y-0.5 pt-1">
              {Object.entries(chart.time_precision).map(([k, v]) => (
                <div key={k} className="font-mono">
                  <span className="text-amber-700">{k}</span>：{v}
                </div>
              ))}
            </div>
          </details>
        )}
      </div>

      {/* 悬停读数 */}
      <div className="px-3 py-1.5 border-b border-neutral-200 bg-neutral-50 text-[11px] font-mono flex flex-wrap items-center gap-x-4 gap-y-1">
        {hoveredBar ? (
          <>
            <span className="text-neutral-500">时刻 {fmtUtc(hoveredBar.timestamp)} UTC</span>
            <span>开 <b className="text-neutral-100">{fmtNum(hoveredBar.open, 2)}</b></span>
            <span>高 <b className="text-neutral-100">{fmtNum(hoveredBar.high, 2)}</b></span>
            <span>低 <b className="text-neutral-100">{fmtNum(hoveredBar.low, 2)}</b></span>
            <span>
              收 <b className={(hoveredBar.close ?? 0) >= (hoveredBar.open ?? 0) ? 'text-emerald-600' : 'text-rose-600'}>
                {fmtNum(hoveredBar.close, 2)}
              </b>
            </span>
            <span>量（base） <b className="text-neutral-100">{fmtVolume(hoveredBar.volume)}</b></span>
            {hoveredSignal ? (
              <span className="px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-700 border border-indigo-200">
                真实信号：{hoveredSignal.kind} / {hoveredSignal.direction}
                {hoveredSignal.stop_price !== null ? ` · 止损 ${fmtNum(hoveredSignal.stop_price, 2)}` : ''}
                {hoveredSignal.atr !== null ? ` · ATR ${fmtNum(hoveredSignal.atr, 2)}` : ''}
                {hoveredSignal.oi_quality ? ` · OI ${hoveredSignal.oi_quality}` : ''}
              </span>
            ) : (
              <span className="text-neutral-400">该根无真实信号记录</span>
            )}
          </>
        ) : (
          <span className="text-neutral-400">将鼠标移到 K 线上查看该根真实 OHLC / 原始成交量 / 信号</span>
        )}
      </div>

      <div className="relative w-full h-[273px] bg-white">
        {bars.length === 0 ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center text-xs text-neutral-500 gap-1">
            <span>{loading ? '正在读取真实图表载荷…' : '无真实 K 线数据（不绘制任何合成 K 线 / 静态 demo）'}</span>
            {error && <span className="text-rose-600">取数错误：{error}</span>}
            {missingReason && <span className="text-amber-700">后台说明：{missingReason}</span>}
          </div>
        ) : (
          <CandleVolumeChart
            key={viewKey}
            bars={bars}
            timeframe="15m"
            gridTimes={gridTimes}
            viewKey={viewKey}
            initialViewport={null}
            onViewportChange={onViewportChange}
            onHover={onHover}
            onReady={onReady}
            syncHub={syncHub}
            extraMarkers={extraMarkers}
          />
        )}
      </div>

      {/* 选中定位读数 */}
      <div className="px-3 py-1.5 border-t border-neutral-200 bg-neutral-50 text-[10px] font-mono flex flex-wrap gap-x-4 gap-y-1">
        {selectedTrade ? (
          <>
            <span className="text-emerald-700 font-semibold">已选中 trade_id = {selectedTrade.trade_id}</span>
            <span>entry {fmtUtc(selectedTrade.entry.t)} @ {fmtNum(selectedTrade.entry.price, 4)}</span>
            <span>exit {fmtUtc(selectedTrade.exit.t)} @ {fmtNum(selectedTrade.exit.price, 4)}（{selectedTrade.exit.reason}）</span>
            <span>止损轨迹 {selectedTrade.stop_trace?.length ?? 0} 点（引擎实况）</span>
            {locateInfo && (
              <span className={locateInfo.ok ? 'text-neutral-400' : 'text-amber-700'}>
                定位范围 {fmtUtc(locateInfo.fromMs)} → {fmtUtc(locateInfo.toMs)}（{locateInfo.ok ? '已落实' : '未落实（图未就绪）'}）
              </span>
            )}
          </>
        ) : (
          <span className="text-neutral-400">
            点击下方明细行或图上开/平仓标记 ⇒ 定位该笔 entry/exit 区间并高亮标记
          </span>
        )}
      </div>
    </div>
  );
};

export default LabChartPanel;
