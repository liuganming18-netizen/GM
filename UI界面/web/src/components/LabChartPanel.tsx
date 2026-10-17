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
import type { Candle, Timeframe } from '../types/crypto';
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

/* ══════════════════ 执行视图周期选择（日线 – 5分线）═════════════════════════
 *
 * 事实边界（不得含糊）：
 * · 本 run 的图表载荷 `GET /backtests/{id}/chart` **只有执行周期（15m）的真实 K 线**。
 * · 比执行周期**更粗**的周期，由这些真实 15m K 线按 **UTC 边界精确聚合**得到：
 *     O = 桶内首根 open，H = 桶内 max(high)，L = 桶内 min(low)，C = 桶内末根 close，
 *     V = 桶内 volume 求和，t = 桶起点。
 *   这是这些周期的**定义式**算法（不是均值、不是插值、不改时间戳），成交量精确可加。
 * · 比执行周期**更细**的 5m **无法**从 15m 还原 ⇒ 明确禁用并给出原因（不做假图）。
 *
 * 聚合只是「换个尺度看同一份真实数据」，标记（开平仓/订单/止损轨迹）仍落在**真实时点**上。
 */
const TF_MS: Record<Timeframe, number> = {
  '5m': 300_000,
  '15m': 900_000,
  '30m': 1_800_000,
  '1h': 3_600_000,
  '4h': 14_400_000,
  '8h': 28_800_000,
  '12h': 43_200_000,
  '1d': 86_400_000,
};

export interface LabChartTfOption {
  value: Timeframe;
  label: string;
  enabled: boolean;
  reason?: string;
}

/** 周期选项：日线 → 5分线（与全站 `Timeframe` 同一套值，不做第二套命名）。 */
export const LAB_CHART_TF_OPTIONS: LabChartTfOption[] = [
  { value: '1d', label: '日线', enabled: true },
  { value: '12h', label: '12h', enabled: true },
  { value: '8h', label: '8h', enabled: true },
  { value: '4h', label: '4h', enabled: true },
  { value: '1h', label: '1h', enabled: true },
  { value: '30m', label: '30m', enabled: true },
  { value: '15m', label: '15m', enabled: true },
  {
    value: '5m',
    label: '5分线',
    enabled: false,
    reason: '本 run 的执行周期是 15m，比它更细的 5m 无法从 15m 还原（不插值、不合成）⇒ 禁用',
  },
];

/**
 * 把基础周期 K 线按 UTC 边界聚合到更粗周期。
 * 目标周期 ≤ 基础周期时**原样返回**（不反向合成、不编造细粒度）。
 */
export function bucketCandles(base: Candle[], baseTf: string, targetTf: string): Candle[] {
  if (!base.length || targetTf === baseTf) return base;
  const step = TF_MS[targetTf as Timeframe];
  const baseStep = TF_MS[baseTf as Timeframe];
  if (!step || !baseStep || step <= baseStep) return base;

  const out: Candle[] = [];
  let cur: Candle | null = null;
  let curBucket = Number.NaN;
  for (const b of base) {
    if (!Number.isFinite(b?.timestamp)) continue;
    const bucket = Math.floor(b.timestamp / step) * step;
    if (bucket !== curBucket) {
      if (cur) out.push(cur);
      curBucket = bucket;
      cur = { ...b, time: new Date(bucket).toISOString(), timestamp: bucket };
    } else if (cur) {
      cur.high = Math.max(cur.high ?? b.high ?? 0, b.high ?? 0);
      cur.low = Math.min(cur.low ?? b.low ?? Number.POSITIVE_INFINITY, b.low ?? Number.POSITIVE_INFINITY);
      cur.close = b.close;
      cur.volume = (cur.volume ?? 0) + (b.volume ?? 0);
    }
  }
  if (cur) out.push(cur);
  return out;
}

/**
 * 归一化一根 K 线：线上实际是 `[t,o,h,l,c,v]` **数组元组**，源码里写的是对象。
 * 两种都接受（见 `LabChartCandleTuple` 注释）；都不合法 ⇒ 返回 null（该根丢弃，不补 0）。
 */
function normalizeCandle(raw: unknown): Candle | null {
  let t: unknown, o: unknown, h: unknown, l: unknown, c: unknown, v: unknown;
  if (Array.isArray(raw)) {
    [t, o, h, l, c, v] = raw;
  } else if (raw && typeof raw === 'object') {
    const r = raw as Record<string, unknown>;
    t = r.t;
    o = r.o;
    h = r.h;
    l = r.l;
    c = r.c;
    v = r.v;
  } else {
    return null;
  }
  if (!Number.isFinite(t) || (t as number) <= 0) return null;
  if (![o, h, l, c].every((x) => typeof x === 'number' && Number.isFinite(x))) return null;
  return {
    time: new Date(t as number).toISOString(),
    timestamp: t as number,
    open: o as number,
    high: h as number,
    low: l as number,
    close: c as number,
    volume: typeof v === 'number' && Number.isFinite(v) ? v : null,
    volumeUsd: null,
    isClosed: true,
  };
}

/** 真实 chart.candles → 前端 `Candle`（`volumeUsd` 保持 null，不用名义额冒名）。 */
export function labCandlesToBars(chart: LabChart | null): Candle[] {
  if (!chart?.candles) return [];
  const out: Candle[] = [];
  for (const c of chart.candles) {
    const bar = normalizeCandle(c);
    if (bar) out.push(bar);
  }
  return out;
}

/** 对齐时间戳到当前显示周期的时间桶（秒），使日线等粗周期能精准落位 markers */
function alignTimeToTf(ms: number, tf: Timeframe): number {
  const step = TF_MS[tf];
  if (!step || step <= 900_000) {
    return toChartSeconds(ms);
  }
  const bucket = Math.floor(ms / step) * step;
  return toChartSeconds(bucket);
}

/** 开/平仓 + 信号标记（真实 trades / signals ⇒ 官方 markers）。 */
function buildTradeMarkers(
  chart: LabChart | null,
  selectedTrade: LabChartTrade | null,
  idOf: (t: LabChartTrade) => string | null,
  tf: Timeframe = '1d',
): SeriesMarker<Time>[] {
  const out: SeriesMarker<Time>[] = [];
  if (!chart?.trades) return out;
  for (const t of chart.trades) {
    const sel = selectedTrade !== null && idOf(t) === idOf(selectedTrade);
    if (Number.isFinite(t.entry?.t)) {
      out.push({
        id: `entry-${idOf(t) ?? t.entry.t}`,
        time: alignTimeToTf(t.entry.t, tf) as unknown as Time,
        position: 'belowBar',
        shape: 'arrowUp',
        color: sel ? '#047857' : '#0b8a5e',
        text: sel ? '开仓(选中)' : '开仓',
        size: sel ? 2 : 1,
      });
    }
    if (Number.isFinite(t.exit?.t)) {
      out.push({
        id: `exit-${idOf(t) ?? t.exit.t}`,
        time: alignTimeToTf(t.exit.t, tf) as unknown as Time,
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

/**
 * 候选信号标记（真实 `chart.signals` ⇒ 官方 markers）。
 *
 * 这是「K 线 → 逐笔」链条的**第一环**：箱体突破那一刻的候选。没有它，
 * 图上就只剩开/平仓两支箭头，看不出「为什么在这根开仓」。
 *
 * `t <= 0` 的一律丢弃（历史 run 的 `chart.signals` 曾被上游写死成 `t: 0`）——
 * 宁可少画一个点，也不在错误的位置画一个假点。
 */
function buildSignalMarkers(chart: LabChart | null, tf: Timeframe = '1d'): SeriesMarker<Time>[] {
  const out: SeriesMarker<Time>[] = [];
  for (const s of chart?.signals ?? []) {
    if (!Number.isFinite(s?.t) || s.t <= 0) continue;
    out.push({
      id: `sig-${s.t}`,
      time: alignTimeToTf(s.t, tf) as unknown as Time,
      position: 'aboveBar',
      shape: 'circle',
      color: '#3b6ef6',
      text: s.kind === 'box_breakout' ? '箱体突破' : '候选',
      size: 1,
    });
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

function buildOrderMarkers(orders: LabOrderRow[] | null | undefined, tf: Timeframe = '1d'): SeriesMarker<Time>[] {
  const out: SeriesMarker<Time>[] = [];
  if (!orders) return out;
  for (const o of orders) {
    if (!Number.isFinite(o.ts_ms)) continue;
    out.push({
      id: `ord-${o.order_id}`,
      time: alignTimeToTf(o.ts_ms, tf) as unknown as Time,
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
  /**
   * 逐笔明细行索引（真实 trade_id + 进出场毫秒）。
   *
   * 为什么需要：线上 `chart.trades[].trade_id` 实测为 `null`（落盘端缺陷，2026-10-17），
   * 而 `GET /trades` 给的是真实 `trd_N`。只靠图载荷 ⇒ 选中高亮 / 图上点击定位全失效。
   * 这里用**两边都有的真实时间戳**把 id 补回来；不用猜、不造假 id。
   */
  tradeIndex?: { tradeId: string; entryMs: number; exitMs: number }[] | null;
}

export const LabChartPanel: React.FC<LabChartPanelProps> = ({
  chart,
  loading,
  error,
  missingReason,
  selectedTradeId,
  onSelectTrade,
  orders = null,
  tradeIndex = null,
}) => {
  /* 执行视图优先展示日线（1d），并允许在界面自由切换 12h/8h/4h/1h/30m/15m */
  const baseTf = (chart?.exec_timeframe ?? '15m') as string;
  const [tf, setTf] = useState<Timeframe>('1d');
  /* 换 run（标的 / 执行周期变化）⇒ 依然优先日线，保证大局观与大趋势优先呈现 */
  useEffect(() => {
    setTf('1d');
  }, [baseTf, chart?.symbol]);

  const rawBars = useMemo(() => labCandlesToBars(chart), [chart]);
  /** 展示用 K 线：等于执行周期时原样，更粗时按 UTC 边界精确聚合（见 bucketCandles 注释） */
  const bars = useMemo(() => bucketCandles(rawBars, baseTf, tf), [rawBars, baseTf, tf]);
  const isAggregated = tf !== baseTf;
  const gridTimes = useMemo(() => buildCandleGrid(bars), [bars]);
  const viewKey = useMemo(
    () => (chart ? `lab:${chart.symbol}:${chart.exec_timeframe}:${tf}:${chart.window?.start_ms ?? 0}` : 'lab:empty'),
    [chart, tf],
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

  /**
   * 图载荷里一笔的**真实 id**：优先用载荷自带的 `trade_id`；
   * 若为 `null`（落盘端缺陷）⇒ 用 entry 时刻去逐笔明细索引里换回真实 id。
   * 两边用的都是真实毫秒时间戳，不猜、不造。
   */
  const idByEntryMs = useMemo(() => {
    const m = new Map<number, string>();
    for (const x of tradeIndex ?? []) {
      if (Number.isFinite(x?.entryMs) && x.tradeId) m.set(x.entryMs, x.tradeId);
    }
    return m;
  }, [tradeIndex]);

  const idOf = useCallback(
    (t: LabChartTrade): string | null =>
      t.trade_id ?? (Number.isFinite(t.entry?.t) ? (idByEntryMs.get(t.entry.t) ?? null) : null),
    [idByEntryMs],
  );

  const selectedTrade: LabChartTrade | null = useMemo(() => {
    const list = chart?.trades ?? [];
    if (!selectedTradeId || list.length === 0) return null;
    return list.find((t) => idOf(t) === selectedTradeId) ?? null;
  }, [chart, selectedTradeId, idOf]);

  const extraMarkers = useMemo(
    () => [
      ...buildSignalMarkers(chart, tf),
      ...buildTradeMarkers(chart, selectedTrade, idOf, tf),
      ...buildOrderMarkers(orders, tf),
    ],
    [chart, selectedTrade, idOf, orders, tf],
  );

  /* ── 止损轨迹叠加：用现有 onReady 句柄加官方 LineSeries，不重写 K 线引擎 ── */
  const overlayRef = useRef<ISeriesApi<'Line'>[]>([]);
  useEffect(() => {
    const api = handle?.chart;
    overlayRef.current = [];
    if (!api || !chart?.trades?.length) return;

    const created: ISeriesApi<'Line'>[] = [];
    for (const t of chart.trades) {
      // 字段契约兼容：官方 runner 落 `t`，历史批量脚本落 `ts`（同值不同名）。
      // 两种都接受 ⇒ 上游无论往哪边收敛都能画；两者都没有的点直接丢弃，不猜时间。
      const trace = (t.stop_trace ?? [])
        .map((p) => ({
          t: Number.isFinite(p?.t) ? p.t : Number((p as unknown as { ts?: number })?.ts),
          stop: p?.stop,
        }))
        .filter((p) => Number.isFinite(p.t) && Number.isFinite(p.stop));
      // 单点（只有 `init`、之后没有止损调整）**不合成轨迹**：一个点画不出线，
      // 硬拉一条会伪造「止损一路没动」的走势。如实显示在下方读数行里。
      if (trace.length < 2) continue;
      const isSel = selectedTrade !== null && idOf(t) === idOf(selectedTrade);
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
  }, [handle, chart, selectedTrade, idOf]);

  /* ── 选中交易 ⇒ 用现有 setViewport 定位 entry/exit 区间（自适应当前显示周期） ── */
  useEffect(() => {
    if (!selectedTrade || !handle) {
      setLocateInfo(null);
      return;
    }
    const stepMs = TF_MS[tf] ?? 900_000;
    // 上下文 padding：日线展示前后各 10 天，细周期展示前后各 10 根 bar，兼顾细节与宏观
    const padMs = Math.max(3_600_000, stepMs * 10);
    const fromMs = selectedTrade.entry.t - padMs;
    const toMs = selectedTrade.exit.t + padMs;
    const ok = handle.setViewport(fromMs, toMs);
    setLocateInfo({ fromMs, toMs, ok });
  }, [selectedTrade, handle, tf]);

  /* ── 图上点击 = 选中该 bar 上的真实开/平仓（精确命中才选中，按当前周期对齐）── */
  useEffect(() => {
    const api = handle?.chart;
    if (!api || !chart?.trades?.length) return;
    const byTime = new Map<number, string>();
    for (const t of chart.trades) {
      const id = idOf(t);
      if (!id) continue;
      if (Number.isFinite(t.entry?.t)) byTime.set(alignTimeToTf(t.entry.t, tf), id);
      if (Number.isFinite(t.exit?.t)) byTime.set(alignTimeToTf(t.exit.t, tf), id);
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
  }, [handle, chart, onSelectTrade, idOf, tf]);

  const hoveredBar = hover?.candle ?? null;
  const hoveredSignal = useMemo(() => {
    if (!hoveredBar || !chart?.signals) return null;
    return chart.signals.find((s) => s.t === hoveredBar.timestamp) ?? null;
  }, [hoveredBar, chart]);

  const tp = chart?.take_profit ?? null;
  const noFixedTp = tp ? tp.exists === false : null;

  /** 选中交易的止损轨迹读数：**账本有几个点就报几个点**。
   *  只有 1 个 `init` 点（无后续调整）时如实说明，**不合成轨迹**。 */
  const stopTraceInfo = useMemo(() => {
    if (!selectedTrade) return null;
    const tr = (selectedTrade.stop_trace ?? [])
      .map((p) => ({
        t: Number.isFinite(p?.t) ? p.t : Number((p as unknown as { ts?: number })?.ts),
        stop: p?.stop,
      }))
      .filter((p) => Number.isFinite(p.t) && Number.isFinite(p.stop));
    return { n: tr.length, init: tr.length > 0 ? tr[0].stop : null,
             adjustments: Math.max(0, tr.length - 1) };
  }, [selectedTrade]);

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
            ④ 执行视图 · {chart?.symbol ?? '—'}
          </h3>

          {/* 周期选择：日线 – 5分线（与全站 Timeframe 同一套值）。
              比执行周期更粗 ⇒ 由本 run 真实 K 线按 UTC 边界精确聚合；更细的 5m 明确禁用。 */}
          <div
            className="flex items-center bg-white border border-neutral-200 rounded p-0.5 text-[10px] shrink-0"
            role="group"
            aria-label="K 线周期"
            data-testid="lab-chart-tf"
          >
            {LAB_CHART_TF_OPTIONS.map((o) => (
              <button
                key={o.value}
                type="button"
                disabled={!o.enabled}
                title={
                  o.reason ??
                  (o.value === baseTf
                    ? '本 run 的执行周期（原始粒度，未做任何聚合）'
                    : `由本 run 的 ${baseTf} 真实 K 线按 UTC 边界聚合（O=首/H=最高/L=最低/C=末/V=求和）`)
                }
                aria-pressed={tf === o.value}
                onClick={() => o.enabled && setTf(o.value)}
                data-tf={o.value}
                className={`px-1.5 py-0.5 rounded whitespace-nowrap transition-colors ${
                  !o.enabled
                    ? 'text-neutral-300 cursor-not-allowed'
                    : tf === o.value
                      ? 'bg-[var(--ds-accent)] text-white font-bold'
                      : 'text-neutral-500 hover:text-neutral-100'
                }`}
              >
                {o.label}
              </button>
            ))}
          </div>

          {/* 计数合并成一行小字（原为三个独立徽章） */}
          <span
            className="text-[10px] text-neutral-400 font-mono whitespace-nowrap"
            title={
              isAggregated
                ? `展示 ${tf}：由本 run 的 ${baseTf} 真实 K 线按 UTC 边界精确聚合（不是第二个数据源）`
                : `本 run 执行周期 ${baseTf} 的真实 K 线根数（非合成 / 非静态 demo）`
            }
          >
            {bars.length} 根
            {isAggregated ? `（${tf}·聚合）` : ''}
            {chart?.trades ? ` · 开平仓 ${chart.trades.length} 笔` : ''}
            {orders && orders.length > 0 ? ` · 委托 ${orders.length} 条` : ''}
          </span>

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

      {/* 止盈口径 + 标记图例（同一行；口径原文在 title，不占视野）。
          常态（止盈已配置）**不刷徽章**，只报「无固定止盈」这类需要解释的异常。 */}
      <div className="px-3 py-1 border-b border-neutral-200 bg-white text-[10px] flex flex-wrap items-center gap-x-3 gap-y-1">
        {noFixedTp === true && (
          <span
            className="px-1.5 py-0.5 rounded bg-amber-50 text-amber-800 border border-amber-200 font-semibold"
            title={tp?.note ? `口径：${tp.note}` : undefined}
          >
            无固定止盈
          </span>
        )}
        {noFixedTp === null && <span className="text-neutral-400">止盈规则字段缺失 ⇒ 不做假设</span>}
        <span className="text-neutral-500 flex flex-wrap items-center gap-x-2">
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
          <span className="text-neutral-400">悬停 K 线看该根真实 OHLC / 量 / 信号</span>
        )}
      </div>

      <div
        className="relative w-full h-[273px] bg-white"
        title="悬停看该根真实 OHLC / 原始成交量 / 信号；点击下方明细行或图上开/平仓标记 ⇒ 定位该笔 entry/exit 区间并高亮"
      >
        {bars.length === 0 ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center text-xs text-neutral-500 gap-1">
            <span>{loading ? '正在读取真实图表载荷…' : '无真实 K 线数据（不绘制任何合成 K 线 / 静态 demo）'}</span>
            {error && <span className="text-rose-600">取数错误：{error}</span>}
            {missingReason && <span className="text-amber-700">后台说明：{missingReason}</span>}
          </div>        ) : (
          <CandleVolumeChart
            key={viewKey}
            bars={bars}
            timeframe={tf}
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

      {/* 选中定位读数：**只有真的选中某笔才出现**。
          未选中时不再常驻「点击下方明细行…」这类操作提示 —— 它属于说明而非数据，
          已收进图表区 title（悬停可见），不再占一行高度。 */}
      {selectedTrade && (
        <div className="px-3 py-1.5 border-t border-neutral-200 bg-neutral-50 text-[10px] font-mono flex flex-wrap gap-x-4 gap-y-1">
          <span className="text-emerald-700 font-semibold">已选中 {selectedTrade.trade_id}</span>
          <span>entry {fmtUtc(selectedTrade.entry.t)} @ {fmtNum(selectedTrade.entry.price, 4)}</span>
          <span>
            exit {fmtUtc(selectedTrade.exit.t)} @ {fmtNum(selectedTrade.exit.price, 4)}（{selectedTrade.exit.reason}）
          </span>
          <span>
            止损轨迹 {stopTraceInfo?.n ?? 0} 点
            {stopTraceInfo?.n === 0 && '（账本无止损点 ⇒ 不合成）'}
            {stopTraceInfo?.n === 1 &&
              `（仅初始止损 ${fmtNum(stopTraceInfo.init, 4)}，无后续调整 ⇒ 不合成轨迹）`}
            {stopTraceInfo !== null && stopTraceInfo.n > 1 &&
              `（含 ${stopTraceInfo.adjustments} 次调整）`}
          </span>
          {locateInfo && (
            <span className={locateInfo.ok ? 'text-neutral-400' : 'text-amber-700'}>
              定位 {fmtUtc(locateInfo.fromMs)} → {fmtUtc(locateInfo.toMs)}
              {locateInfo.ok ? '' : '（未落实：图未就绪）'}
            </span>
          )}
        </div>
      )}
    </div>
  );
};

export default LabChartPanel;
