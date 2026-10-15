/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * TradingView Lightweight Charts™ 官方 5.x 实现的**薄封装**。
 *
 * 定位：只做「容器生命周期 + 真实数据映射 + 视窗保持 + 事件转发」，
 * 蜡烛绘制、价格轴/时间轴、十字线、滚轮缩放、拖动平移**全部**由官方库原生提供，
 * 本文件不重写任何图表引擎逻辑。
 *
 * 官方文档：
 *  - https://tradingview.github.io/lightweight-charts/docs
 *  - https://tradingview.github.io/lightweight-charts/docs/panes
 *  - https://tradingview.github.io/lightweight-charts/docs/migrations/from-v4-to-v5
 *
 * 数据纪律（与项目既有约定一致）：
 *  - 时间戳毫秒 → 官方要求的 **UTC 秒**（`UTCTimestamp`），排序 + 去重；
 *  - 缺任一 OHLC / 成交量 / OI 值 ⇒ 该点**留空**，**绝不补 0、不合成、不插值**；
 *  - 未收盘临时层只作**单根独立配色 + 明确「未收盘」文字**临时展示，
 *    不并入闭合历史序列（闭合序列由调用方 `candles` 单独维护，缺口/指标统计只认它）。
 *
 * 署名：`layout.attributionLogo = false`（2026-10-12 按用户要求关闭图内 TradingView 图标）；
 * 署名合规由页面渲染的 `LIGHTWEIGHT_CHARTS_ATTRIBUTION` **可见文本链接**承担
 * （指向 tradingview.com，满足库 README 的链接要求）。
 *
 * ---------------------------------------------------------------------------
 * 四处关键实现依据（均读官方 5.2.1 源码 / 类型定义核实，不靠猜测）
 * ---------------------------------------------------------------------------
 * 1) `TimeScale._internal_logicalRangeForTimeRange()` → `_internal_timeToIndex()`：
 *    仅当该图时间轴 `points.length < 1`（**完全没有时间点**）时返回 `null`，
 *    再经 `ensureNotNull` 抛 `Value is null`；范围落在数据域**之外**不会抛错，
 *    而是被夹到最近的时间点。⇒ 安全判据是「该图有 ≥1 个时间点」，不是「范围必须落在域内」。
 * 2) `DataLayer._internal_setSeriesData()` 对**每一条**数据（含 whitespace）都建
 *    `pointDataByTimePoint` 并 `isTimeScaleAffected = true` ⇒ **whitespace 点会进入时间轴点集**；
 *    而 `LinePaneViewBase` 只遍历有效点、`walkLine` 对相邻有效点持续 `lineTo`
 *    ⇒ **whitespace 不能断线**，断线必须用多条 `LineSeries` 分段。
 * 3) `TimeScale._private__setVisibleRange()` 仅在逻辑范围**确实变化**时才
 *    `logicalRangeChanged._internal_fire()` ⇒ 按真实时间同步不会形成回环。
 * 4) 官方时间轴是**离散索引**（`barSpacing` 均匀、与时间点集密度无关）：
 *    两图若时间点集不同，即使可见 UTC 窗口一致，同一时间点也会落在不同 x 上。
 *    故两图必须使用**同一个真实时间点并集**（各自补纯 whitespace 锚点，真实值不变），
 *    并且价格轴宽度必须一致（`PriceScaleOptions.minimumWidth`，官方文档即为此场景）。
 */

import React, { useCallback, useEffect, useMemo, useRef } from 'react';
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type CandlestickData,
  type ChartOptions,
  type DeepPartial,
  type HistogramData,
  type IChartApi,
  type IPriceLine,
  type IPriceScaleApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type LineData,
  type LogicalRange,
  type MouseEventParams,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
  type WhitespaceData,
} from 'lightweight-charts';
import type { Candle, OIPoint, Timeframe } from '../types/crypto';
import { formatWanYi } from '../utils/formatters';

/** TradingView Lightweight Charts™ 署名（Apache-2.0）。 */
export const LIGHTWEIGHT_CHARTS_ATTRIBUTION = {
  text: 'Charts by TradingView Lightweight Charts™',
  url: 'https://www.tradingview.com/lightweight-charts/',
  license: 'Apache-2.0',
} as const;

export const TIMEFRAME_SECONDS: Record<Timeframe, number> = {
  '5m': 300,
  '15m': 900,
  '30m': 1_800,
  '1h': 3_600,
  '4h': 14_400,
  '8h': 28_800,
  '12h': 43_200,
  '1d': 86_400,
};

/**
 * 浅色清爽风下的官方库配色。
 *
 * 口径**完全沿用原版**（涨绿跌红、多绿空红），只把明度从「深底可读」调到
 * 「浅底可读」：底色由 #0d0d0d 改为 #ffffff，网格/坐标轴/文字改为浅灰系，
 * 蜡烛与成交量保持同一色相、提高饱和度以保证在白色背景上的对比度。
 * 未收盘临时层仍用琥珀色单根标识，语义未变。
 */
const COLOR = {
  up: '#0b8a5e',
  down: '#d92d48',
  /**
   * 中性灰。**当前无引用**：原为 OI 柱形三态着色（涨 / 跌 / 中性）里的「中性」一档，
   * 柱色改为统一单色（`oiBar`）后不再需要；保留此键仅作调色板记录。
   */
  neutral: '#8491a5',
  /** 未收盘临时层：单根独立颜色（库无虚线蜡烛样式，故用颜色 + 文字标识区分） */
  forming: '#ad6800',
  /** 原手写 SVG 的「信号触发标记」颜色（#6366f1 圆点 + 竖线），改用官方 markers 保留 */
  signalMarker: '#4f5bd5',
  gridHorz: '#e7ecf3',
  gridVert: '#eef2f8',
  text: '#586880',
  border: '#d4dbe6',
  /**
   * OI 总量柱形的**单一颜色**（口径见 `buildOiBars`）。
   *
   * 2026-10-12 用户指令「OI 总持仓量柱形图用绿色，别用灰色」⇒ 由中性蓝灰
   * `#5b6b85` 改为**绿色**（与本文件「涨」色同值 `COLOR.up`），与图例色块同源。
   *
   * 语义边界**未变**：仍然是**所有柱同一个颜色**，颜色不承载任何语义 ——
   * 不表示多空方向、也不表示环比增减。这里改用绿色只是配色选择（强调色），
   * 不是「按涨跌/方向着色」；柱高依旧只表达「总量」这一个量。
   *
   * 为什么不能按涨跌 / 方向着色（原判断保留）：
   *   · 后端只给**总持仓量**（数量 + 名义额），**没有多空拆分**；OI 是净额、
   *     多空恒等，OI 增加 = 新开合约（新多 + 新空同时进场），OI 减少 = 合约平掉，
   *     与「多头 / 空头」无关 ⇒ 用红/绿**两色**会被读成多空，是误读；
   *   · 同屏 K 线蜡烛也用红绿表示**价格**涨跌，同色不同义必然混淆。
   */
  oiBar: '#0b8a5e',
  background: '#ffffff',
} as const;

/**
 * OI 柱形的单一柱色，供面板标题前的图例色块复用（避免图例与柱色对不上）。
 * 语义见上方 `COLOR.oiBar`：柱色不承载方向 / 增减。
 */
export const OI_BAR_COLOR = COLOR.oiBar;

/** 价格轴起始最小宽度：最终由 `alignPriceScaleWidths` 按两图实际需要对齐。 */
const PRICE_SCALE_MIN_WIDTH = 72;

/**
 * OI 柱形基线的下缘余量（占**可视窗内真实区间**的比例）。
 *
 * 0.12 ⇒ 窗内最低柱仍有 12% 柱高、最高柱占满绘图区，高低差一眼可辨。
 * 见 `OiBarChartInner` 的 `recomputeOiBase`：`base = 窗内最小值 - 区间 × 本余量`。
 */
const OI_BASE_MARGIN = 0.12;

const BASE_OPTIONS: DeepPartial<ChartOptions> = {
  autoSize: true,
  layout: {
    background: { type: ColorType.Solid, color: COLOR.background },
    textColor: COLOR.text,
    // 图内 TradingView 图标（`a#tv-attr-logo`，35×19px SVG）**已按用户要求关闭**
    // （2026-10-12：用户反馈「鼠标停留几秒会弹出图标/说明，删掉」）。
    //
    // 署名合规性未受影响：库 README 的要求是「在页面上放一个指向
    // https://www.tradingview.com/ 的链接」，并说明 `attributionLogo` 只是**其中一种**
    // 满足方式。本页在展开图表工具栏里始终渲染可见文本链接
    // `LIGHTWEIGHT_CHARTS_ATTRIBUTION`（→ tradingview.com/lightweight-charts/），
    // 因此关掉图内图标后，链接署名依然在页面上存在。
    attributionLogo: false,
  },
  grid: {
    vertLines: { color: COLOR.gridVert },
    horzLines: { color: COLOR.gridHorz },
  },
  crosshair: {
    mode: CrosshairMode.Normal,
    vertLine: { color: '#94a3b8', width: 1, style: LineStyle.Dashed, labelBackgroundColor: '#334155' },
    horzLine: { color: '#94a3b8', width: 1, style: LineStyle.Dashed, labelBackgroundColor: '#334155' },
  },
  rightPriceScale: { borderColor: COLOR.border, minimumWidth: PRICE_SCALE_MIN_WIDTH },
  timeScale: {
    borderColor: COLOR.border,
    timeVisible: true,
    secondsVisible: false,
    // 两图同为 0：右侧不留空档，K 线与 OI 的可见时间窗与横坐标严格一一对应
    rightOffset: 0,
  },
  // 滚轮缩放 / 拖动平移 / 捏合缩放全部走库原生实现
  handleScroll: true,
  handleScale: true,
};

/**
 * 毫秒 → 官方库要求的 **UTC 秒**（`UTCTimestamp`，必须为有限非负整数）。
 *
 * 非有限 / 非正输入一律回 `0`：官方库对 `NaN`/`Infinity` 时间会直接拒绝或抛出，
 * 绝不能让脏时间透传进图表（脏值一律回 `null`/缺省，不透传 —— 项目既有数据纪律）。
 */
export function toChartSeconds(ms: number): UTCTimestamp {
  if (!Number.isFinite(ms) || ms <= 0) return 0 as UTCTimestamp;
  return Math.floor(ms / 1000) as UTCTimestamp;
}

/** 排序 + 按「秒」去重（同秒后写覆盖），保证官方库要求的严格递增唯一时间。 */
function sortUniqueByTime<T extends { timestamp: number }>(items: T[]): T[] {
  const bySec = new Map<number, T>();
  for (const item of items) {
    if (!Number.isFinite(item.timestamp) || item.timestamp <= 0) continue;
    bySec.set(toChartSeconds(item.timestamp), item);
  }
  return [...bySec.entries()].sort((a, b) => a[0] - b[0]).map(([, value]) => value);
}

const hasOhlc = (candle: Candle): boolean =>
  [candle.open, candle.high, candle.low, candle.close].every((v) => typeof v === 'number' && Number.isFinite(v));

const isFiniteNumber = (v: number | null | undefined): v is number =>
  typeof v === 'number' && Number.isFinite(v);

/** 真实 K 线 → 官方 CandlestickData（缺 OHLC 的根直接跳过 = 留空）。 */
export function buildCandleData(bars: Candle[]): CandlestickData[] {
  const out: CandlestickData[] = [];
  for (const candle of sortUniqueByTime(bars)) {
    if (!hasOhlc(candle)) continue; // 留空：不补 0、不合成
    const item: CandlestickData = {
      time: toChartSeconds(candle.timestamp),
      open: candle.open as number,
      high: candle.high as number,
      low: candle.low as number,
      close: candle.close as number,
    };
    out.push(item);
  }
  return out;
}

/**
 * K 线图真实时间点集（UTC 秒）——与 `buildCandleData` 的时间点集**逐点一致**
 * （同样跳过缺 OHLC 的根），用于构造两图共享的时间网格。
 */
export function buildCandleGrid(bars: Candle[]): number[] {
  const out: number[] = [];
  for (const candle of sortUniqueByTime(bars)) {
    if (!hasOhlc(candle)) continue;
    out.push(toChartSeconds(candle.timestamp));
  }
  return out;
}

/**
 * OI 真实样本时点（UTC 秒，升序去重）。
 *
 * 已经过 `alignOiToTimeframe` 周期对齐的样本，按 `alignedTimestamp` 落位 —— 这样本图的时间点集
 * 才与 K 线图**完全重合**（并集网格退化为 K 线网格），两图逐点对齐且蜡烛等距铺满。
 * 未对齐的样本仍用真实 `timestamp`（行为与修复前逐字一致）。
 */
export function oiSampleTimes(points: OIPoint[]): number[] {
  const bySec = new Set<number>();
  for (const point of points) {
    const ms = point.alignedTimestamp ?? point.timestamp;
    if (!Number.isFinite(ms) || ms <= 0) continue;
    bySec.add(toChartSeconds(ms));
  }
  return [...bySec].sort((a, b) => a - b);
}

/** 两个已升序去重的秒数组 → 并集（升序去重）。 */
export function mergeSortedUnique(left: number[], right: number[]): number[] {
  const out: number[] = [];
  let i = 0;
  let j = 0;
  let last = Number.NaN;
  while (i < left.length || j < right.length) {
    const a = i < left.length ? left[i] : Number.POSITIVE_INFINITY;
    const b = j < right.length ? right[j] : Number.POSITIVE_INFINITY;
    const next = a <= b ? left[i++] : right[j++];
    if (!Number.isFinite(next)) continue;
    if (next !== last) {
      out.push(next);
      last = next;
    }
  }
  return out;
}

/** 真实成交量 → 官方 HistogramData（缺成交量留空；颜色沿用涨绿跌红）。 */
export function buildVolumeData(bars: Candle[]): HistogramData[] {
  const out: HistogramData[] = [];
  for (const candle of sortUniqueByTime(bars)) {
    if (!isFiniteNumber(candle.volume)) continue; // 留空
    let color: string;
    if (!candle.isClosed) color = 'rgba(173, 104, 0, 0.45)';
    else if (!hasOhlc(candle)) color = 'rgba(132, 145, 165, 0.40)';
    else color = (candle.close as number) >= (candle.open as number)
      ? 'rgba(11, 138, 94, 0.45)'
      : 'rgba(217, 45, 72, 0.45)';
    out.push({ time: toChartSeconds(candle.timestamp), value: candle.volume, color });
  }
  return out;
}

/**
 * 原手写 SVG 的「信号触发标记」（`candle.signalTag` 处的 #6366f1 圆点 + 竖线）
 * → 官方 `createSeriesMarkers` 的 markers。
 *
 * 只对**有 signalTag 且 OHLC 完整**的根出标记（与旧组件同一判据）；
 * `position: 'aboveBar'` 对应旧的「画在该根最高价上方」语义。
 * 后端 `annotations` 当前恒为空（`adaptCandles` 里 `signalTag: undefined`），
 * 因此实际不产生标记；一旦后端提供标注即自动生效，不伪造、不推断。
 */
export function buildSignalMarkers(bars: Candle[]): SeriesMarker<Time>[] {
  const out: SeriesMarker<Time>[] = [];
  for (const candle of sortUniqueByTime(bars)) {
    if (!candle.signalTag || !hasOhlc(candle)) continue;
    out.push({
      time: toChartSeconds(candle.timestamp),
      position: 'aboveBar',
      shape: 'circle',
      color: COLOR.signalMarker,
    });
  }
  return out;
}

/* ------------------------------------------------------------------ *
 * OI 总量柱形图：真实柱高 + 单一柱色（颜色不承载语义）
 * ------------------------------------------------------------------ */

export interface OiBarData {
  /** 每根柱：真实样本时点 + 该时点总量 + 统一柱色（不表达方向 / 增减） */
  bars: HistogramData[];
  /** 全部真实样本时点（UTC 秒，升序去重），用于网格去重与间隔统计 */
  sampleTimes: number[];
}

/**
 * 真实 OI 序列 → 官方 HistogramSeries 柱形数据（**柱高 = 该时点总持仓量**）。
 *
 * 与旧折线版语义一致，但改用柱形：
 *  - 该时点真实值为 `null` ⇒ **不出柱**。柱形天然「断柱」：没有点就没有柱，
 *    不存在折线那种「跨缺口把两侧连起来」的问题，故无需多段序列；
 *    空值与跨缺口（`point.hasGap`）都靠**柱间空档**在几何上呈现。
 *  - **所有柱用同一个颜色 `COLOR.oiBar`（2026-10-12 起为绿色），颜色不承载任何语义。**
 *    是**统一单色**而非按涨跌/方向着色（后者不可取）：
 *      · 后端只给**总持仓量**（数量 + 名义额），**没有多空拆分**；OI 是净额、
 *        多空恒等，OI 增加 = 新开合约（新多 + 新空同时进场），OI 减少 = 合约平掉，
 *        与「多头 / 空头」无关 ⇒ 用红/绿**两色**会**被读成多空**，是误读。
 *      · 即便改成按「环比增减」着色，同一屏里的 K 线蜡烛也用红绿表示**价格**涨跌，
 *        同色不同义必然混淆 ⇒ 干脆不用颜色表达第二个维度。
 *    柱高只表达「总量」这一个量；增减请从柱高变化、或 tooltip 的环比数值读取。
 *  - 真实样本一律原样保留：不聚合、不改写、不重采样、不伪造时间。
 */
export function buildOiBars(points: OIPoint[], unit: 'amount' | 'usd'): OiBarData {
  const bars: HistogramData[] = [];
  const sampleTimes: number[] = [];
  for (const point of sortUniqueByTime(points)) {
    const value = unit === 'usd' ? point.oiUsd : point.oiAmount;
    if (!isFiniteNumber(value)) continue; // 该时点无真实值：不出柱
    /**
     * 已做周期对齐的样本按 `alignedTimestamp` 落位（与共享网格、K 线时点重合）；
     * 未对齐时用真实时点。数值仍是真实快照，只是横坐标对齐到周期边界。
     */
    const time = toChartSeconds(point.alignedTimestamp ?? point.timestamp);
    bars.push({ time, value, color: COLOR.oiBar });
    sampleTimes.push(time);
  }
  return { bars, sampleTimes };
}

/* ------------------------------------------------------------------ *
 * 共享时间网格（两图并集）
 * ------------------------------------------------------------------ */

/**
 * 本图缺失的网格时点 → 纯 whitespace 锚点（只含时间、**不含任何数值**）。
 *
 * 这些点：
 *  - 不参与画线（官方 `LinePaneViewBase` 只遍历有效点）；
 *  - 不参与价格轴（无值即不贡献 autoscale）；
 *  - 唯一作用是让本图时间轴点集**补齐到两图共享的并集**，
 *    使两图拥有完全相同的离散时间点集 ⇒ 相同索引 ⇒ 相同横坐标映射
 *    ⇒ 同一 UTC 时间窗下，两图同一时间点落在同一 x（刻度不再错位）。
 *
 * 这不是「造数据」：锚点不携带任何数值，时间点全部来自真实数据；
 * 真实 K 线 / 真实 OI 样本既未聚合也未重采样、未改时间戳。
 */
export function buildGridAnchor(gridTimes: number[], ownTimes: number[]): WhitespaceData[] {
  const occupied = new Set<number>(ownTimes);
  const seen = new Set<number>();
  const out: WhitespaceData[] = [];
  for (const raw of gridTimes) {
    if (!Number.isFinite(raw)) continue;
    const sec = Math.floor(raw);
    if (sec <= 0 || occupied.has(sec) || seen.has(sec)) continue;
    seen.add(sec);
    out.push({ time: sec as UTCTimestamp });
  }
  out.sort((a, b) => (a.time as number) - (b.time as number));
  return out;
}

/* ------------------------------------------------------------------ *
 * 增量更新：只在「末尾追加 / 末根变化」时用官方 `update()`，
 * 历史重置（切周期、切币、回填更早历史、缺口变化、结构变化）才用 `setData()`。
 * 目的：实时刷新不整表重设，避免官方库在 setData 时重置可见范围。
 * ------------------------------------------------------------------ */

const DATA_KEYS = ['time', 'open', 'high', 'low', 'close', 'value', 'color', 'borderColor', 'wickColor'] as const;

function sameDataPoint(a: unknown, b: unknown): boolean {
  const left = a as Record<string, unknown>;
  const right = b as Record<string, unknown>;
  for (const key of DATA_KEYS) {
    if (left[key] !== right[key]) return false;
  }
  return true;
}

function sameDataArray(a: unknown[], b: unknown[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i += 1) {
    if (!sameDataPoint(a[i], b[i])) return false;
  }
  return true;
}

function timeOf(item: unknown): number | null {
  const value = (item as { time?: unknown } | null | undefined)?.time;
  return typeof value === 'number' ? value : null;
}

/**
 * 判断 `next` 是否只是 `prev` 的「末根变化 / 追加一根」。
 *
 * 除最后一根外的前缀必须逐字段一致（前缀变化 = 历史被重置，走 setData）；
 * 且 `next` 中与 `prev` 末根**对齐位置**的那一根，时间**不得向前移动**
 * —— 否则说明是切币/切周期/回填更早历史造成的整表替换，必须走 setData。
 *
 * 注意：本函数**不保证** `prev` 的末根在 `next` 中没变（追加新根时旧末根可能
 * 同时从 forming 变为 closed）。因此调用方必须另行核对/更新旧末根，见 `applySeriesUpdate`。
 *
 * 导出仅为可执行验证（纯函数，无副作用）。
 */
export function isIncrementalTail(prev: unknown[], next: unknown[]): boolean {
  if (prev.length === 0) return false;
  if (next.length < prev.length) return false; // 变短（未收盘根消失等）→ 重置
  if (next.length - prev.length > 1) return false; // 一次补多根 → 重置
  for (let i = 0; i < prev.length - 1; i += 1) {
    if (!sameDataPoint(prev[i], next[i])) return false;
  }
  const prevLastTime = timeOf(prev[prev.length - 1]);
  const alignedTime = timeOf(next[prev.length - 1]);
  if (prevLastTime !== null && alignedTime !== null && alignedTime < prevLastTime) return false;
  return true;
}

/** 官方序列写入接口的结构子集（`ISeriesApi` 天然满足）。 */
interface SeriesWriter<TData> {
  setData(data: TData[]): void;
  update(bar: TData, historicalUpdate?: boolean): void;
}

/**
 * 把新数据下发到官方序列，并尽量走增量 `update()`。
 *
 * - 前缀/结构变化或显式重置 ⇒ `setData()`；
 * - 同长度（末根变化）⇒ 只 `update()` 末根；
 * - 追加一根 ⇒ **先核对并更新旧末根**（它可能刚从 forming 变为最终闭合，
 *   OHLC/成交量被上游修正），再 `update()` 新根。若只 update 新根，
 *   旧末根会停留在过期值。
 *
 * 导出仅为可执行验证（纯函数，无副作用）。
 */
export function applySeriesUpdate<TData>(
  series: SeriesWriter<TData>,
  prev: TData[],
  next: TData[],
  forceReset: boolean,
): void {
  if (forceReset || !isIncrementalTail(prev, next)) {
    series.setData(next);
    return;
  }
  if (next.length === prev.length) {
    series.update(next[next.length - 1]);
    return;
  }
  const prevLast = prev[prev.length - 1];
  const nextAlignedLast = next[next.length - 2];
  if (!sameDataPoint(prevLast, nextAlignedLast)) series.update(nextAlignedLast);
  series.update(next[next.length - 1]);
}

/**
 * 价格精度推断：真实池价格可低至 1.25e-05，官方默认精度 2 位会把价格抹平。
 * 只按数量级选择**展示精度**，不改动任何数值。
 *
 * 用 `reduce` 而非 `Math.min(...array)`：后者受引擎**实参个数上限**限制，
 * 拖入长历史时会直接抛 `RangeError`。
 */
function inferPriceFormat(data: CandlestickData[]): { precision: number; minMove: number } {
  const min = data.reduce<number>((acc, item) => {
    let next = acc;
    for (const value of [item.open, item.high, item.low, item.close]) {
      if (Number.isFinite(value) && value > 0 && value < next) next = value;
    }
    return next;
  }, Number.POSITIVE_INFINITY);
  if (!Number.isFinite(min)) return { precision: 2, minMove: 0.01 };
  let precision: number;
  if (min >= 1000) precision = 2;
  else if (min >= 1) precision = 4;
  else if (min >= 0.01) precision = 5;
  else if (min >= 0.0001) precision = 6;
  else precision = 8;
  return { precision, minMove: Math.pow(10, -precision) };
}

/**
 * 大数值（OI 数量 / 名义额）刻度精度：只影响刻度显示位数。
 * 同样用 `reduce`，避免实参个数上限。
 */
function inferValueFormat(values: number[]): { precision: number; minMove: number } {
  const max = values.reduce<number>((acc, value) => {
    if (!Number.isFinite(value)) return acc;
    const abs = Math.abs(value);
    return abs > acc ? abs : acc;
  }, Number.NEGATIVE_INFINITY);
  if (!Number.isFinite(max)) return { precision: 2, minMove: 0.01 };
  let precision: number;
  if (max >= 1000) precision = 0;
  else if (max >= 1) precision = 2;
  else precision = 4;
  return { precision, minMove: Math.pow(10, -precision) };
}

/* ------------------------------------------------------------------ *
 * 视窗与跨图同步
 * ------------------------------------------------------------------ */

export interface ChartViewport {
  fromMs: number;
  toMs: number;
  /** true = 跟随最新（视窗右缘贴住最新根）；false = 用户已拖到历史视窗 */
  followLatest: boolean;
}

/**
 * 同组图表同步成员。
 * 同步按**真实时间范围**（不是逻辑索引）——K 线与 OI 的样本数不同，
 * 按逻辑索引同步会导致两者时间窗错位。
 */
export interface ChartSyncMember {
  chart: IChartApi;
  /** 该图真实数据域（UTC 秒）；无任何时间点为 `null` */
  domain: { from: number; to: number } | null;
  /** 已销毁：销毁后一律不再调用该图的任何官方 API */
  disposed: boolean;
  /**
   * 时间窗**主图 / 跟随图**角色。
   *
   * 主图（K 线）首次落位只认自己的「恢复视窗 / 全部已加载数据」，
   * **绝不复制跟随图（OI）的当前视窗** —— 跟随图可能在自身数据到达后
   * 先做了一次 fitContent，若主图去复制它，用户视窗就会被那个默认窗口顶掉（实测缺陷）。
   */
  role: 'leader' | 'follower';
}

export interface ChartSyncHub {
  members: ChartSyncMember[];
  /** 程序化同步期间置位，避免同一次同步内的双向回环 */
  syncing: boolean;
  /**
   * 「用户直接操作」窗口的截止时刻（`performance.now()` 基准）。
   *
   * 时间窗同步是**单向**的：只有 `leader`（K 线）驱动 `follower`（OI）。
   * 否则 OI 图在自身数据/单位变化时产生的范围事件会**反向覆盖**主图视窗
   * （实测：切周期重置期间 OI 旧窗口把主图恢复后的视窗顶掉）。
   * 但用户**主动**在 OI 图上滚轮/拖动时，仍应联动主图 —— 由调用方在交互时
   * 把本字段推到「当前 + 500ms」，该窗口内允许 `follower` 反向同步。
   */
  userDrivenUntil: number;
}

export function createChartSyncHub(): ChartSyncHub {
  return { members: [], syncing: false, userDrivenUntil: 0 };
}

/** 用户交互后允许 `follower` 反向同步的时长（毫秒）。 */
export const USER_DRIVEN_SYNC_MS = 500;

/**
 * 程序化写入（`setData` / `update` / `setVisibleRange` / `fitContent` / `scrollToRealTime`）
 * 之后的时间静默窗：窗口内由官方库派发的可见范围事件**不得**触发视窗上报，
 * 否则重置期间的中间范围会污染当前 viewKey 的视窗缓存。
 */
const PROGRAMMATIC_QUIET_MS = 400;

/** 官方 API 兜底：任何图表调用异常都不得冒泡打断页面渲染。 */
function safeTimeScaleCall(fn: () => void): void {
  try {
    fn();
  } catch {
    // 官方库在空数据/销毁竞态下会抛错；此处静默降级，保持页面可用
  }
}

function safeCall<T>(fn: () => T): T | null {
  try {
    return fn();
  } catch {
    return null;
  }
}

/**
 * 两图价格轴宽度对齐。
 *
 * 官方价格轴宽度按**标签内容**自适应：K 线标签（如 `0.00001234`）与 OI 标签
 * （如 `24,774,762`）宽度不同 ⇒ 绘图区宽度不同 ⇒ 即使两图时间点集完全一致，
 * 同一时间点仍会落在不同 x 上（刻度错位）。官方为此提供 `PriceScaleOptions.minimumWidth`：
 * 读取同组各图右价格轴的真实宽度，取最大值回写 `minimumWidth`，使两图同宽。
 * 该操作幂等（第二次读到的即同一最大值），不会震荡。
 */
function alignPriceScaleWidths(hub: ChartSyncHub): void {
  const scales: { scale: IPriceScaleApi }[] = [];
  for (const member of hub.members) {
    if (member.disposed) continue;
    const scale = safeCall(() => member.chart.priceScale('right'));
    if (scale) scales.push({ scale });
  }
  if (scales.length < 2) return;
  let maxWidth = 0;
  for (const item of scales) {
    const width = safeCall(() => item.scale.width()) ?? 0;
    if (width > maxWidth) maxWidth = width;
  }
  if (maxWidth <= 0) return;
  for (const item of scales) {
    safeTimeScaleCall(() => item.scale.applyOptions({ minimumWidth: maxWidth }));
  }
}

/**
 * 把**同一条真实 UTC 时间范围**应用到同组其它图表。
 *
 * **必须严格守卫**：官方 `TimeScale._internal_timeToIndex()` 只在该图
 * `points.length < 1` 时返回 `null`，再经 `ensureNotNull` 抛 `Value is null`
 * （异常沿 `rangeHandler` 冒泡会打挂整个 React 渲染，实测白屏）。
 *
 * 因此这里逐条守卫：
 *  1. 目标已销毁 → 跳过；
 *  2. 目标**没有任何时间点** → 跳过（此时任何 setVisibleRange 都是非法的）；
 *  3. 仍以防万一包一层 try/catch —— 同步是增强能力，失败绝不允许影响页面。
 *
 * **不把范围夹到目标自身的数据域**：夹取会让两图显示不同时间窗
 * （历史窗落在 OI 域外时会保留旧窗口）。这里原样下发同一条 UTC 窗口，
 * 目标图样本域外自然显示为空白。
 */
function applyTimeRangeTo(member: ChartSyncMember, fromSec: number, toSec: number): boolean {
  if (member.disposed || !member.domain) return false;
  if (!(toSec > fromSec)) return false;
  try {
    member.chart.timeScale().setVisibleRange({
      from: fromSec as UTCTimestamp,
      to: toSec as UTCTimestamp,
    });
    return true;
  } catch {
    return false;
  }
}

/** 该图当前是否具备可安全调用 `setVisibleRange` 的条件（有真实时间点 + 未销毁）。 */
function canSetVisibleRange(member: ChartSyncMember): boolean {
  return !member.disposed && member.domain !== null && member.domain.to > member.domain.from;
}

/**
 * 该图当前可见的**逻辑范围**（已销毁 / 无时间点 / 区间非法 → `null`，绝不抛错）。
 *
 * 为什么跨图同步用它、而不是真实时间范围：
 * 两图的时间点集被构造成**同一个共享并集网格**（各自只补纯 whitespace 锚点，
 * 真实 K 线值 / 真实 OI 值一律不变），因此同一个「逻辑索引」在两张图上指同一时间点。
 * 而 `getVisibleRange()` 只精确到**整秒**：把它回写成 `setVisibleRange` 后，
 * 目标图的逻辑边界会被吸附（实测主图 441.5239 → 跟随图 441），`barSpacing` 随之不等
 * （实测 3.937282… vs 3.930070…），同一 UTC 时间点横坐标最多差 5.4px。
 * 主图边界本身是分数索引时误差还会更大。改用逻辑范围后两图逐点一致（实测 0px）。
 */
function getVisibleLogicalRange(member: ChartSyncMember): LogicalRange | null {
  if (member.disposed) return null;
  const range = safeCall(() => member.chart.timeScale().getVisibleLogicalRange());
  if (!range) return null;
  if (typeof range.from !== 'number' || typeof range.to !== 'number') return null;
  if (!Number.isFinite(range.from) || !Number.isFinite(range.to)) return null;
  if (!(range.to > range.from)) return null;
  return range;
}

/**
 * 把同一条**可见逻辑范围**应用到目标图。
 * 与 `applyTimeRangeTo` 同一套守卫：已销毁 / 无任何时间点 / 区间非法 → `false`，绝不抛错。
 */
function applyLogicalRangeTo(member: ChartSyncMember, range: LogicalRange | null): boolean {
  if (member.disposed || !member.domain || !range) return false;
  try {
    member.chart.timeScale().setVisibleLogicalRange({ from: range.from, to: range.to });
    return true;
  } catch {
    return false;
  }
}

/**
 * 把**主图（K 线）**当前的可见逻辑范围应用到本图（时间窗跟随图专用）。
 *
 * 用于跟随图自身数据变化之后重新对齐 —— 跟随图的 `setData` 会让官方库按新点集
 * 重算它自己的范围，若此时不去对齐主图，两图就会显示不同时间窗
 * （实测切周期/补历史后 1m 上出现 1576px 的横向错位）。
 * 找不到主图 / 主图暂无范围 → `false`，调用方再决定回落策略。
 */
function alignToLeader(hub: ChartSyncHub, member: ChartSyncMember): boolean {
  const leader = hub.members.find(
    (item) => item !== member && !item.disposed && item.role === 'leader',
  );
  if (!leader) return false;
  const range = getVisibleLogicalRange(leader);
  if (!range) return false;
  hub.syncing = true;
  try {
    return applyLogicalRangeTo(member, range);
  } finally {
    hub.syncing = false;
  }
}

/** 图表实例句柄：供调用方做跨图十字线联动与视窗落实。 */
export interface ChartHandle {
  chart: IChartApi;
  /** 时点不在本图可见范围时只清除十字线，不越界定位 */
  crosshairAt(timeSec: UTCTimestamp, price: number | null): void;
  clearCrosshair(): void;
  /**
   * 按**真实 UTC 时间**落位可见范围（入参毫秒，内部转官方 UTC 秒）。
   *
   * 供调用方在「更早历史补齐之后」重新落实用户视窗；与其它时间轴调用同样守卫：
   * 已销毁 / 该图无任何时间点 / 区间非法 → 返回 `false`，绝不抛错打断渲染。
   */
  setViewport(fromMs: number, toMs: number): boolean;
  /**
   * 读取当前可见范围（毫秒）；已销毁 / 无任何时间点 → `null`，绝不抛错。
   *
   * 调用方（父组件）在用户交互时用它与 `setViewport` 配合固化视窗 ——
   * 切周期会**原生卸载**旧实例，父组件持有的旧句柄此时已失效，
   * 因此这里必须自带守卫而不是让父组件去猜实例是否还有效。
   */
  visibleRange(): { fromMs: number; toMs: number } | null;
}

/** 十字线联动前先确认时点落在对方可见范围内。 */
function isTimeVisible(chart: IChartApi, timeSec: number): boolean {
  const visible = chart.timeScale().getVisibleRange();
  if (!visible || typeof visible.from !== 'number' || typeof visible.to !== 'number') return true;
  return timeSec >= (visible.from as number) && timeSec <= (visible.to as number);
}

export interface ChartHover {
  /** 触发悬停的图表来源 */
  source: 'candle' | 'oi';
  /** 悬停时点（毫秒，来自官方十字线） */
  timeMs: number;
  /** 命中真实 K 线时给出该根原始对象（含 volumeUsd / isClosed / signalTag） */
  candle: Candle | null;
  /** 命中真实 OI 行时给出该行原始对象（含原单位数量与名义额） */
  oiPoint?: OIPoint | null;
}

interface SharedChartProps {
  viewKey: string;
  /** 该 viewKey 首次挂载时的恢复视窗；null = 显示全部已加载数据 */
  initialViewport: ChartViewport | null;
  /**
   * 视窗上报。**必须携带所属 viewKey** —— 该键在**调度时刻**捕获，
   * 调用方据此拒绝「上一周期的迟到报告」，避免用旧周期的范围污染新周期的视窗缓存。
   */
  onViewportChange: (viewport: ChartViewport, viewKey: string) => void;
  onHover: (hover: ChartHover | null) => void;
  onReady: (handle: ChartHandle) => void;
  syncHub: ChartSyncHub;
  /** 两图共享的真实时间点并集（UTC 秒）：本图补纯 whitespace 锚点补齐到它 */
  gridTimes: number[];
}

/** 复用的图表生命周期骨架：创建 / 销毁 / 十字线 / 时间轴同步 / 视窗上报。 */
function useChartLifecycle(
  build: (chart: IChartApi) => {
    /** 该图的时间基准序列（用于十字线定位） */
    series: ISeriesApi<'Candlestick'> | ISeriesApi<'Line'>;
    onCrosshair: (param: MouseEventParams<Time>) => void;
    /** 销毁前清理（如 marker 插件 detach）；在 `chart.remove()` 之前调用 */
    onDispose?: () => void;
  },
  followToleranceSec: React.MutableRefObject<number>,
  syncHub: ChartSyncHub,
  onHover: SharedChartProps['onHover'],
  onReady: SharedChartProps['onReady'],
  onViewportChange: SharedChartProps['onViewportChange'],
  /**
   * 本图在时间窗同步中的角色：
   *  - `leader`（K 线）：首次落位只认「恢复视窗 / 全部已加载数据」；
   *  - `follower`（OI）：首次落位优先复制 `leader` 的当前时间窗。
   * 见 `applyInitialViewport` 与 `ChartSyncMember.role`。
   */
  role: ChartSyncMember['role'],
  /** 本图当前所属视图键（`tokenId:timeframe`）；视窗上报**按调度时刻**绑定它 */
  viewKey: string,
) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | ISeriesApi<'Line'> | null>(null);
  const memberRef = useRef<ChartSyncMember | null>(null);
  const lastBarSecRef = useRef<number | null>(null);
  const followRef = useRef(true);
  const initRef = useRef(false);
  /** 数据 effect 内用于判定「视图键是否变化」的已应用键 */
  const appliedViewKeyRef = useRef<string | null>(null);
  /** 每次渲染都同步为最新视图键：供**上报归属**使用（调度时刻取值） */
  const renderViewKeyRef = useRef(viewKey);
  renderViewKeyRef.current = viewKey;
  const reportTimerRef = useRef<number | null>(null);
  /** 程序化写入静默窗截止时刻（`performance.now()` 基准） */
  const quietUntilRef = useRef(0);

  const onHoverRef = useRef(onHover);
  onHoverRef.current = onHover;
  const onReadyRef = useRef(onReady);
  onReadyRef.current = onReady;
  const onViewportChangeRef = useRef(onViewportChange);
  onViewportChangeRef.current = onViewportChange;

  /**
   * 标记「刚发生程序化写入」：此后 `PROGRAMMATIC_QUIET_MS` 内由官方库派发的
   * 可见范围事件不触发上报。重置/落位/整表替换都会调用它。
   */
  const markProgrammatic = useCallback(() => {
    quietUntilRef.current = performance.now() + PROGRAMMATIC_QUIET_MS;
  }, []);

  /** 立即取消待发的上报（切周期/切币时调用，避免旧周期的范围落到新视图键上）。 */
  const cancelPendingReport = useCallback(() => {
    if (reportTimerRef.current !== null) {
      window.clearTimeout(reportTimerRef.current);
      reportTimerRef.current = null;
    }
  }, []);

  /**
   * 调度一次视窗上报（250ms 去抖）。
   *
   * **归属绑定**：`viewKey` 在**调度时刻**捕获，而不是在定时器触发时读取 ——
   * 否则「切周期期间调度的报告」会在触发时被记到新视图键上，
   * 用旧周期的范围污染新周期的视窗缓存（实测缺陷）。
   */
  const scheduleReport = useCallback(() => {
    if (reportTimerRef.current !== null) return;
    const key = renderViewKeyRef.current;
    reportTimerRef.current = window.setTimeout(() => {
      reportTimerRef.current = null;
      // 视窗上报的同时顺带对齐价格轴宽度（窗口尺寸变化也会走到这里）
      alignPriceScaleWidths(syncHub);
      // 程序化写入静默窗内：本次范围来自库的重算，不是用户视窗，丢弃
      if (performance.now() < quietUntilRef.current) return;
      const chart = chartRef.current;
      if (!chart) return;
      const range = chart.timeScale().getVisibleRange();
      if (!range || typeof range.from !== 'number' || typeof range.to !== 'number') return;
      onViewportChangeRef.current({
        fromMs: (range.from as number) * 1000,
        toMs: (range.to as number) * 1000,
        followLatest: followRef.current,
      }, key);
    }, 250);
  }, [syncHub]);

  /**
   * 结束程序化阶段：清除静默窗并取消当前待发上报。
   *
   * 官方库的落位（`setVisibleRange` / `fitContent`）是**异步生效**的 ——
   * 调用返回后立刻 `getVisibleRange()` 读到的是**落位前**的旧范围
   * （实测：状态位显示 10-05T20:37–10-08T13:00，而画布刻度已是 10-04～10-07）。
   * 因此这里**不读取**任何范围，只把「可以上报了」这件事放开；
   * 真正的上报由**原生可见范围事件**驱动去抖回调在 250ms 后读取 ——
   * 那时范围已落实，读到的才是与画布一致的值。
   */
  const endProgrammatic = useCallback(() => {
    quietUntilRef.current = 0;
    cancelPendingReport();
  }, [cancelPendingReport]);

  /** 视图键变化：取消待发上报并进入静默窗（新周期从「干净」状态开始）。 */
  useEffect(() => {
    cancelPendingReport();
    markProgrammatic();
  }, [viewKey, cancelPendingReport, markProgrammatic]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return undefined;

    const chart = createChart(container, BASE_OPTIONS);
    const built = build(chart);
    chartRef.current = chart;
    seriesRef.current = built.series;
    const member: ChartSyncMember = { chart, domain: null, disposed: false, role };
    memberRef.current = member;
    syncHub.members.push(member);

    const crosshairHandler = (param: MouseEventParams<Time>) => built.onCrosshair(param);
    chart.subscribeCrosshairMove(crosshairHandler);

    const rangeHandler = (_range: LogicalRange | null) => {
      const visible = chart.timeScale().getVisibleRange();
      if (visible && typeof visible.from === 'number' && typeof visible.to === 'number') {
        /**
         * 时间窗同步**单向**：只有 `leader`（K 线）驱动 `follower`（OI）。
         * `follower` 的范围变化只有两种来源 —— ①被主图同步（`syncing` 已置位）；
         * ②自身数据/单位变化引起的库重算 —— 两者都**不得**反向覆盖主图视窗
         * （实测：切周期重置期间 OI 旧窗口把主图恢复后的视窗顶掉）。
         * 例外：用户**主动**在 OI 图上滚轮/拖动（调用方把 `userDrivenUntil` 推到未来 500ms）。
         */
        const mayDrive = member.role === 'leader' || performance.now() < syncHub.userDrivenUntil;
        if (!syncHub.syncing && mayDrive) {
          /**
           * 按**可见逻辑范围**同步给同组其它图表（不再按真实时间范围）。
           *
           * 两图共用同一个时间点并集网格 ⇒ 逻辑索引在两张图上指同一时间点，
           * 逻辑范围可以直接互用；而按时间范围同步会因整秒取整把目标图的逻辑边界
           * 吸附成整数、`barSpacing` 与主图不等 ⇒ 同一时间点横坐标差最多 5.4px（实测）。
           * 官方库仅在逻辑范围**确实变化**时才派发事件，故按逻辑范围同步同样不会回环。
           */
          const logical = getVisibleLogicalRange(member);
          if (logical) {
            syncHub.syncing = true;
            try {
              for (const other of syncHub.members) {
                if (other.chart !== chart) {
                  applyLogicalRangeTo(other, logical);
                }
              }
            } finally {
              syncHub.syncing = false;
            }
          }
        }
        const last = lastBarSecRef.current;
        if (last !== null) {
          followRef.current = (visible.to as number) >= last - followToleranceSec.current;
        }
      }
      scheduleReport();
    };
    chart.timeScale().subscribeVisibleLogicalRangeChange(rangeHandler);

    // 窗口尺寸变化会改变价格轴所需宽度 → 重新对齐两图价格轴
    const resizeHandler = () => alignPriceScaleWidths(syncHub);
    window.addEventListener('resize', resizeHandler);

    onReadyRef.current({
      chart,
      crosshairAt: (timeSec, price) => {
        if (price === null || !Number.isFinite(price) || member.disposed) {
          safeTimeScaleCall(() => chart.clearCrosshairPosition());
          return;
        }
        safeTimeScaleCall(() => {
          if (!isTimeVisible(chart, timeSec)) {
            chart.clearCrosshairPosition();
            return;
          }
          chart.setCrosshairPosition(price, timeSec, built.series);
        });
      },
      clearCrosshair: () => safeTimeScaleCall(() => chart.clearCrosshairPosition()),
      /**
       * 按**真实 UTC 时间**重新落实可见范围（入参毫秒）。
       *
       * 用途：调用方在「更早历史补齐之后」把用户视窗原样落实回来 ——
       * 首屏只有一页数据时官方库会把窗口夹到已有数据，若不重新落实，
       * 用户视窗就会被那一页的默认窗口顶掉（实测「收起→再展开只剩 60 根」缺陷）。
       *
       * 与其它时间轴调用同一套守卫：已销毁 / 无任何时间点 / 区间非法 → `false`，绝不抛错。
       */
      setViewport: (fromMs, toMs) => {
        if (member.disposed || !member.domain) return false;
        const fromSec = toChartSeconds(fromMs);
        const toSec = toChartSeconds(toMs);
        if (!(toSec > fromSec)) return false;
        return applyTimeRangeTo(member, fromSec, toSec);
      },
      visibleRange: () => {
        if (member.disposed || !member.domain) return null;
        const range = safeCall(() => chart.timeScale().getVisibleRange());
        if (!range || typeof range.from !== 'number' || typeof range.to !== 'number') return null;
        return { fromMs: (range.from as number) * 1000, toMs: (range.to as number) * 1000 };
      },
    });

    return () => {
      if (reportTimerRef.current !== null) {
        window.clearTimeout(reportTimerRef.current);
        reportTimerRef.current = null;
      }
      window.removeEventListener('resize', resizeHandler);
      chart.unsubscribeCrosshairMove(crosshairHandler);
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(rangeHandler);
      // 先摘出同步组并标记已销毁，避免销毁过程中仍被同步调用
      member.disposed = true;
      syncHub.members = syncHub.members.filter((item) => item !== member);
      // marker 插件先 detach，再销毁图表
      safeTimeScaleCall(() => built.onDispose?.());
      // 官方销毁：移除 canvas 与全部内部观察器/订阅，避免残留与重复 canvas
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      memberRef.current = null;
      lastBarSecRef.current = null;
      initRef.current = false;
      appliedViewKeyRef.current = null;
    };
    // build / syncHub / role / viewKey 均为调用方稳定引用（build 由 useCallback 固定）
  }, [build, scheduleReport, syncHub, followToleranceSec, role, markProgrammatic]);

  return useMemo(
    () => ({
      containerRef,
      chartRef,
      seriesRef,
      memberRef,
      lastBarSecRef,
      followRef,
      initRef,
      appliedViewKeyRef,
      scheduleReport,
      endProgrammatic,
      markProgrammatic,
      alignPriceScales: () => alignPriceScaleWidths(syncHub),
    }),
    [scheduleReport, endProgrammatic, markProgrammatic, syncHub],
  );
}

/** 首次视窗结果：是否已按显式视窗落位、是否应跟随最新、是否需要调用方补更早历史。 */
interface InitialViewportResult {
  applied: boolean;
  followLatest: boolean;
  /**
   * 恢复视窗的起点**早于**本图当前已加载域 ⇒ 调用方应继续加载更早历史，
   * 并在每页到达后用 `ChartHandle.setViewport` 把用户视窗原样落实回来。
   */
  needsEarlierHistory: boolean;
}

/**
 * 首次视窗（官方库原生 `setVisibleRange` / `fitContent`，不重写任何坐标引擎）：
 *
 *  1. **跟随图**（OI）：优先复制**主图**（K 线）的当前时间窗（按真实 UTC 时间，不按逻辑索引）。
 *  2. **主图**（K 线）：**绝不**复制跟随图 —— 跟随图可能在自身数据到达后先做了一次
 *     `fitContent`，主图若去复制它，用户视窗就会被那个默认窗口顶掉（实测缺陷）。
 *     主图只认自己的「恢复视窗」，其次才是 `fitContent`。
 *  3. 恢复判据：**区间合法即可原样落位**，不夹到本图数据域、不自造范围。
 *     官方 `_internal_logicalRangeForTimeRange` 只在「本图完全没有时间点」时抛错，
 *     范围落在域外只是被夹到最近点 ⇒ 域外部分自然显示**空白**（缺数据就是缺数据，不假装有）。
 *     旧实现在此要求「恢复范围完全落在域内」，首屏只有一页数据时必然被拒 → 退回 `fitContent`
 *     → 用户视窗被首屏默认窗口顶掉（实测「收起→再展开」缺陷根因）。
 */
function applyInitialViewport(
  chart: IChartApi,
  syncHub: ChartSyncHub,
  member: ChartSyncMember,
  domain: { from: number; to: number } | null,
  restore: ChartViewport | null,
): InitialViewportResult {
  if (member.role === 'follower') {
    /**
     * 跟随图首屏**复制主图的可见逻辑范围**（不是真实时间范围）。
     * 两图共用同一个时间点并集网格 ⇒ 逻辑索引一一对应；按时间范围复制会因整秒取整
     * 把逻辑边界吸附成整数、barSpacing 与主图不等（实测同一时间点差最多 5.4px）。
     */
    if (alignToLeader(syncHub, member)) {
      return { applied: true, followLatest: true, needsEarlierHistory: false };
    }
  }

  if (restore) {
    const fromSec = toChartSeconds(restore.fromMs);
    const toSec = toChartSeconds(restore.toMs);
    if (toSec > fromSec) {
      syncHub.syncing = true;
      let applied = false;
      try {
        applied = applyTimeRangeTo(member, fromSec, toSec);
      } finally {
        syncHub.syncing = false;
      }
      if (applied) {
        return {
          applied: true,
          followLatest: restore.followLatest,
          needsEarlierHistory: !!domain && fromSec < domain.from,
        };
      }
    }
  }

  syncHub.syncing = true;
  try {
    /**
     * 首屏落位：**主图按自身真实数据域落位**，而不是 `fitContent()`。
     *
     * 为什么不能直接 `fitContent()`：两图共用同一套「时间点并集网格」，K 线图上会被
     * 补进大量**只有 OI 有、K 线没有**的纯 whitespace 锚点（锚点不带任何数值，只用于
     * 对齐逻辑索引）。官方库的时间轴范围取**全图所有序列**的并集，于是 K 线图的时间域
     * 被这些远端锚点撑宽 —— 实测 BTC 1m：K 线一页 500 根 ≈ 8.3 小时，OI 快照却有 4.6 天，
     * 并集 4.6 天，`fitContent()` 便按 4.6 天落位，蜡烛被压到最右侧约 4% 宽度、
     * 左侧 96% 全空（「K 线没有完整导入」的实际观感；数据本身一根不缺）。
     *
     * 修法：主图落位用**自己的 K 线域**，蜡烛铺满绘图区；时间轴点集仍是共享并集，
     * 逻辑索引对齐与跨图十字线联动全部不受影响。跟随图（OI）本就以主图为准
     * （每次数据更新都会 `alignToLeader`），两图时间窗依旧严格一致。
     */
    const landedOnOwnDomain =
      member.role === 'leader' && domain
        ? applyTimeRangeTo(member, domain.from, domain.to)
        : false;
    if (!landedOnOwnDomain) safeTimeScaleCall(() => chart.timeScale().fitContent());
  } finally {
    syncHub.syncing = false;
  }
  return { applied: false, followLatest: true, needsEarlierHistory: false };
}

/* ------------------------------------------------------------------ *
 * K 线 + 成交量
 * ------------------------------------------------------------------ */

export interface ChartPriceLine {
  price: number;
  title: string;
  color: string;
  lineStyle?: LineStyle;
  lineWidth?: 1 | 2 | 3 | 4;
}

export interface CandleVolumeChartProps extends SharedChartProps {
  bars: Candle[];
  timeframe: Timeframe;
  priceLines?: ChartPriceLine[];
}

const CandleVolumeChartInner: React.FC<CandleVolumeChartProps> = ({
  bars,
  timeframe,
  priceLines,
  gridTimes,
  viewKey,
  initialViewport,
  onViewportChange,
  onHover,
  onReady,
  syncHub,
}) => {
  const volumeRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  /** 共享网格锚点序列（只含时间、不含数值；不画线、不参与价格轴） */
  const gridSeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  /** 秒 → 原始 Candle，供十字线还原真实字段（不做任何推算） */
  const candleBySecRef = useRef<Map<number, Candle>>(new Map());
  /** 上一轮已下发给官方库的数据，用于判定「末根变化 / 追加一根」走 update() 而非 setData() */
  const lastCandleDataRef = useRef<CandlestickData[]>([]);
  const lastVolumeDataRef = useRef<HistogramData[]>([]);
  const lastGridRef = useRef<WhitespaceData[]>([]);
  const priceFormatRef = useRef<string>('');
  const followToleranceRef = useRef(30);
  const timeframeRef = useRef(timeframe);
  timeframeRef.current = timeframe;
  const onHoverRef = useRef(onHover);
  onHoverRef.current = onHover;

  const build = useCallback(
    (chart: IChartApi) => {
      const candleSeries = chart.addSeries(CandlestickSeries, {
        upColor: COLOR.up,
        downColor: COLOR.down,
        borderUpColor: COLOR.up,
        borderDownColor: COLOR.down,
        wickUpColor: COLOR.up,
        wickDownColor: COLOR.down,
        priceLineVisible: false,
        lastValueVisible: true,
      });
      // 成交量：官方 HistogramSeries，叠加在价格 pane 底部（独立 priceScale，不挤压价格轴）
      const volumeSeries = chart.addSeries(HistogramSeries, {
        priceScaleId: 'volume',
        priceFormat: { type: 'volume' },
        priceLineVisible: false,
        lastValueVisible: false,
      });
      volumeSeries.priceScale().applyOptions({
        scaleMargins: { top: 0.78, bottom: 0 },
        visible: false,
      });
      /**
       * 共享网格锚点：补齐到两图并集（OI 样本时点里 K 线没有的那些）。
       * 只含时间、不含数值 ⇒ 不画线、不参与价格轴，仅让两图时间点集一致。
       */
      const gridSeries = chart.addSeries(LineSeries, {
        lineVisible: false,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
      // 原手写 SVG 的 signalTag 圆点标记 → 官方 markers 插件
      const markers = createSeriesMarkers(candleSeries, []);
      volumeRef.current = volumeSeries;
      gridSeriesRef.current = gridSeries;
      markersRef.current = markers;
      return {
        series: candleSeries,
        onDispose: () => markers.detach(),
        onCrosshair: (param: MouseEventParams<Time>) => {
          if (!param.point || typeof param.time !== 'number') {
            onHoverRef.current(null);
            return;
          }
          const candle = candleBySecRef.current.get(param.time) ?? null;
          onHoverRef.current({ source: 'candle', timeMs: param.time * 1000, candle });
        },
      };
    },
    [],
  );

  const lifecycle = useChartLifecycle(
    build,
    followToleranceRef,
    syncHub,
    onHover,
    onReady,
    onViewportChange,
    // K 线是时间窗**主图**：首次落位只认恢复视窗/已加载数据，绝不复制 OI 的默认窗口
    'leader',
    viewKey,
  );

  const createdPriceLinesRef = useRef<IPriceLine[]>([]);

  // 价格线绘制与生命周期联动
  useEffect(() => {
    const candleSeries = lifecycle.seriesRef.current as ISeriesApi<'Candlestick'> | null;
    if (!candleSeries) return;

    for (const pl of createdPriceLinesRef.current) {
      try {
        candleSeries.removePriceLine(pl);
      } catch {}
    }
    createdPriceLinesRef.current = [];

    if (priceLines && priceLines.length > 0) {
      for (const line of priceLines) {
        if (typeof line.price === 'number' && Number.isFinite(line.price) && line.price > 0) {
          try {
            const pl = candleSeries.createPriceLine({
              price: line.price,
              color: line.color,
              lineWidth: (line.lineWidth ?? 1) as any,
              lineStyle: line.lineStyle ?? LineStyle.Dashed,
              axisLabelVisible: true,
              title: line.title,
            });
            createdPriceLinesRef.current.push(pl);
          } catch {}
        }
      }
    }

    return () => {
      for (const pl of createdPriceLinesRef.current) {
        try {
          candleSeries.removePriceLine(pl);
        } catch {}
      }
      createdPriceLinesRef.current = [];
    };
  }, [priceLines, lifecycle.seriesRef.current, viewKey]);

  // 数据更新：不重建图表、不强制 fitContent（除首次 / 切周期外）
  useEffect(() => {
    const chart = lifecycle.chartRef.current;
    const candleSeries = lifecycle.seriesRef.current as ISeriesApi<'Candlestick'> | null;
    const volumeSeries = volumeRef.current;
    const gridSeries = gridSeriesRef.current;
    if (!chart || !candleSeries || !volumeSeries || !gridSeries) return;

    const candleData = buildCandleData(bars);
    const volumeData = buildVolumeData(bars);
    const gridData = buildGridAnchor(gridTimes, buildCandleGrid(bars));
    const markerData = buildSignalMarkers(bars);
    const bySec = new Map<number, Candle>();
    for (const candle of bars) {
      if (Number.isFinite(candle.timestamp) && candle.timestamp > 0) {
        bySec.set(toChartSeconds(candle.timestamp), candle);
      }
    }
    candleBySecRef.current = bySec;
    const first = candleData.length ? (candleData[0].time as number) : null;
    const last = candleData.length ? (candleData[candleData.length - 1].time as number) : null;
    lifecycle.lastBarSecRef.current = last;
    if (lifecycle.memberRef.current) {
      lifecycle.memberRef.current.domain = first !== null && last !== null ? { from: first, to: last } : null;
    }
    followToleranceRef.current = Math.max(30, TIMEFRAME_SECONDS[timeframeRef.current] / 2);

    // 先记录「本次更新前」的跟随意图与视窗：setData/update 会触发可见范围事件，
    // 若不先快照，followLatest 会被事件改写，实时刷新就会错误地跳回最新或错误地停在历史。
    const followBefore = lifecycle.followRef.current;
    const previousRange = chart.timeScale().getVisibleRange();

    /**
     * 切币 / 切周期：整表替换，强制走 setData（并保留既有视窗恢复逻辑）。
     *
     * 这类「重置」会让官方库派发一串中间可见范围事件 —— 进入静默窗，
     * 避免它们被当成用户视窗上报（切周期时会把旧范围写到新视图键上）。
     * 同周期内的**实时增量刷新**不进入静默窗：那只是末根 update，
     * 视窗读数应继续正常上报（否则页面上的「视窗 …–…」会停更）。
     */
    const viewChanged = lifecycle.appliedViewKeyRef.current !== viewKey;
    if (viewChanged) {
      lifecycle.appliedViewKeyRef.current = viewKey;
      lifecycle.initRef.current = false;
      lifecycle.markProgrammatic();
    }

    const format = inferPriceFormat(candleData);
    const formatKey = `${format.precision}:${format.minMove}`;
    if (priceFormatRef.current !== formatKey) {
      priceFormatRef.current = formatKey;
      candleSeries.applyOptions({ priceFormat: { type: 'price', ...format } });
    }

    // 网格锚点先落位，再落真实序列：保证同一轮里两图时间点集一致
    applySeriesUpdate<WhitespaceData>(gridSeries, lastGridRef.current, gridData, viewChanged);
    lastGridRef.current = gridData;

    // 增量更新：末根变化 / 追加一根 → update()（官方库不会重置可见范围）；
    // 历史重置（切周期、回填更早历史、未收盘根消失、前缀变化）→ setData()。
    applySeriesUpdate<CandlestickData | WhitespaceData>(candleSeries, lastCandleDataRef.current, candleData, viewChanged);
    applySeriesUpdate<HistogramData | WhitespaceData>(volumeSeries, lastVolumeDataRef.current, volumeData, viewChanged);
    lastCandleDataRef.current = candleData;
    lastVolumeDataRef.current = volumeData;

    // signalTag 官方 marker：只保留真实存在的标注根，不推断、不伪造
    safeTimeScaleCall(() => markersRef.current?.setMarkers(markerData));

    // 首次落位必须等到「本图确有真实时间点」：空数据时官方时间轴无点可映射，
    // 任何 `setVisibleRange` / `fitContent` 都非法（实测抛 `Value is null` 打挂渲染），
    // 而且会把唯一一次「按恢复视窗落位」的机会白白消耗掉。
    if (!lifecycle.initRef.current) {
      if (first === null || last === null) return;
      lifecycle.initRef.current = true;
      const initial = applyInitialViewport(
        chart,
        syncHub,
        lifecycle.memberRef.current as ChartSyncMember,
        { from: first, to: last },
        initialViewport,
      );
      lifecycle.followRef.current = initial.followLatest;
      lifecycle.alignPriceScales();
      /**
       * 结束程序化阶段，但**不在这里读取范围**：官方库的落位异步生效，
       * 立即读取会拿到落位前的旧范围，导致状态位与画布刻度互相矛盾（实测缺陷）。
       * 改为放开静默窗后交给去抖上报 —— 由原生可见范围事件驱动，
       * 250ms 后读到的已是落实后的范围。
       */
      lifecycle.endProgrammatic();
      lifecycle.scheduleReport();
      return;
    }

    // 只在「本图确有真实时间点」时才调用时间轴 API —— 空数据时官方库会抛
    // `Value is null`（时间轴无点可映射），必须避免。
    const currentMember = lifecycle.memberRef.current;
    if (currentMember && canSetVisibleRange(currentMember)) {
      if (followBefore) {
        // 仍在跟随最新：只把右缘对齐到最新根，不改变缩放级别（绝不 fitContent）
        safeTimeScaleCall(() => chart.timeScale().scrollToRealTime());
      } else if (previousRange) {
        // 用户已拖到历史视窗 / 正在加载更早历史：原样保持当前视窗
        safeTimeScaleCall(() => chart.timeScale().setVisibleRange(previousRange));
      }
    }
    // 以本次更新前的意图为准写回，避免被 setData/update 期间的范围事件污染
    lifecycle.followRef.current = followBefore;
    lifecycle.alignPriceScales();
    // 实时刷新/追加历史：仍走静默窗内的去抖上报（程序化范围变化不写缓存）
    lifecycle.scheduleReport();
  }, [bars, timeframe, gridTimes, viewKey, initialViewport, syncHub, lifecycle]);

  return (
    <div ref={lifecycle.containerRef} className="w-full h-full" data-chart="lightweight-candles" />
  );
};

/**
 * memo 化：调用方的视窗上报会触发父组件重渲染，这里必须**零重渲染**，
 * 否则会形成「上报 → 重渲染 → 再上报」的重绘循环。
 */
export const CandleVolumeChart = React.memo(CandleVolumeChartInner);
CandleVolumeChart.displayName = 'CandleVolumeChart';

/* ------------------------------------------------------------------ *
 * OI 总量柱形图（官方 HistogramSeries：真实柱高 + 共享时间网格）
 * ------------------------------------------------------------------ */

interface OiBarChartProps extends SharedChartProps {
  points: OIPoint[];
  unit: 'amount' | 'usd';
  /** 无数据时的空态文案（沿用原版措辞，不用实时值伪造历史） */
  emptyText: string;
}

const OiBarChartInner: React.FC<OiBarChartProps> = ({
  points,
  unit,
  gridTimes,
  emptyText,
  viewKey,
  initialViewport,
  onViewportChange,
  onHover,
  onReady,
  syncHub,
}) => {
  const oiBySecRef = useRef<Map<number, OIPoint>>(new Map());
  /** 上一轮已下发的柱数据，用于判定增量更新 */
  const lastBarsRef = useRef<HistogramData[]>([]);
  /** 上一轮已下发的网格锚点数据 */
  const lastGridRef = useRef<WhitespaceData[]>([]);
  /** 当前图表实例上的柱序列（随图表重挂载而重建） */
  const barSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  const boundChartRef = useRef<IChartApi | null>(null);
  /** 上一轮已下发的价格格式档位（精度:最小变动），避免每轮重复 applyOptions */
  const lastFormatKeyRef = useRef<string>('');
  /** 相邻快照的真实间隔（秒，取中位数），用于判定「是否仍在跟随最新」 */
  const stepSecRef = useRef<number>(60);
  const followToleranceRef = useRef(30);
  const unitRef = useRef(unit);
  unitRef.current = unit;
  const onHoverRef = useRef(onHover);
  onHoverRef.current = onHover;

  const build = useCallback(
    (chart: IChartApi) => {
      /**
       * 网格锚点序列：只含时间、不含数值（whitespace）。
       * 官方 `LinePaneViewBase` 只遍历有效点，故它**不画任何线**；
       * 但它会让本图时间轴点集补齐到两图共享并集，使两图横坐标映射一致。
       */
      const gridSeries = chart.addSeries(LineSeries, {
        lineVisible: false,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
      return {
        series: gridSeries,
        onCrosshair: (param: MouseEventParams<Time>) => {
          if (!param.point || typeof param.time !== 'number') {
            onHoverRef.current(null);
            return;
          }
          const point = oiBySecRef.current.get(param.time) ?? null;
          onHoverRef.current({ source: 'oi', timeMs: param.time * 1000, candle: null, oiPoint: point });
        },
      };
    },
    [],
  );

  const lifecycle = useChartLifecycle(
    build,
    followToleranceRef,
    syncHub,
    onHover,
    onReady,
    onViewportChange,
    // OI 是时间窗**跟随图**：首次落位优先复制主图（K 线）的当前时间窗
    'follower',
    viewKey,
  );

  /**
   * 柱形基线（`HistogramSeries.base`）——修复「OI 柱形没有高低波浪，看不出量」。
   *
   * **缺陷**：官方 `HistogramSeries` 的 `base` **默认 0**，柱从 0 画到该时点总量。
   * 而 OI 是**绝对存量**（BTC 91.6k~98.8k），可视窗内的相对波动本来就极小 ——
   * 实测当前 3 小时窗只有 11 个样本、区间 585（**0.64%**）。以 0 为基线时柱高落在
   * 92.7%~100%，差异仅 **7.3 个百分点**，视觉上就是一排等高栅栏，量级完全读不出来。
   *
   * **修法**：`base` 取**当前可视窗内真实柱值**的下缘（再低 12% 余量），使窗内波动铺满
   * 绘图区。轴刻度仍是**真实总量**（不缩放、不改数值、不聚合、不重采样、不伪造时点），
   * 只是把「柱的起点」从 0 换成窗内下缘 —— 与「OI 看图看的是变化」的读图目的相符。
   *
   * **为什么必须跟随可视窗重算**：
   *  · 若固定用全量（4.6 天）下缘做基线，3 小时窗的波动只占柱高 8%，仍然近齐平；
   *  · 且平移到数值更低的窗口时，柱会落到基线以下（负柱），图形直接错。
   * 本重算只改**价格轴**（右轴）刻度，不触碰时间轴范围，故不会与两图同步形成回环。
   */
  const oiBaseRef = useRef<number | null>(null);
  const recomputeOiBase = useCallback(() => {
    const chart = lifecycle.chartRef.current;
    const series = barSeriesRef.current;
    if (!chart || !series) return;
    if (lifecycle.memberRef.current?.disposed) return;
    const bars = lastBarsRef.current;
    if (bars.length === 0) return;

    const range = safeCall(() => chart.timeScale().getVisibleRange());
    const from = range && typeof range.from === 'number' ? (range.from as number) : null;
    const to = range && typeof range.to === 'number' ? (range.to as number) : null;

    let min = Infinity;
    let max = -Infinity;
    const scan = (useWindow: boolean) => {
      for (const bar of bars) {
        const time = bar.time as number;
        if (useWindow && from !== null && to !== null && (time < from || time > to)) continue;
        const value = bar.value;
        if (!Number.isFinite(value)) continue;
        if (value < min) min = value;
        if (value > max) max = value;
      }
    };
    if (from !== null && to !== null) scan(true);
    if (!Number.isFinite(min) || !Number.isFinite(max)) {
      // 视窗内一个真实样本都没有（例如刚切周期）→ 回落到全量已载入数据
      min = Infinity;
      max = -Infinity;
      scan(false);
    }
    if (!Number.isFinite(min) || !Number.isFinite(max)) return;

    const span = max - min;
    // 全窗同值（span = 0）时退回「略低于该值」，避免柱高归零
    const base = span > 0 ? min - span * OI_BASE_MARGIN : min * (1 - 1e-3);
    const prev = oiBaseRef.current;
    // 变化不足 0.02% ⇒ 不下发，避免拖动过程中无谓的整图重绘
    if (prev !== null && Math.abs(base - prev) <= Math.abs(prev) * 2e-4) return;
    oiBaseRef.current = base;
    safeCall(() => series.applyOptions({ base }));
  }, [lifecycle]);

  /**
   * 可视窗变化 → 重算柱形基线。
   *
   * 只订阅**时间轴**的范围事件：柱形基线仅影响右价格轴刻度，不会反向改变时间轴范围，
   * 因此本订阅不可能形成回环。图表实例随 `viewKey` 重挂载（React key），本 effect 亦随之
   * 重建，不存在悬挂订阅。
   */
  useEffect(() => {
    const chart = lifecycle.chartRef.current;
    if (!chart) return;
    const handler = () => recomputeOiBase();
    chart.timeScale().subscribeVisibleLogicalRangeChange(handler);
    return () => chart.timeScale().unsubscribeVisibleLogicalRangeChange(handler);
  }, [lifecycle, recomputeOiBase]);

  useEffect(() => {
    const chart = lifecycle.chartRef.current;
    const gridSeries = lifecycle.seriesRef.current as ISeriesApi<'Line'> | null;
    if (!chart || !gridSeries) return;

    // 本 effect 内全部为程序化写入（分段 setData / 网格 setData / 视窗保持）→ 先进入静默窗
    lifecycle.markProgrammatic();

    // 图表实例更换（重挂载）→ 旧的柱序列句柄已随 chart.remove() 失效，必须重建
    if (boundChartRef.current !== chart) {
      boundChartRef.current = chart;
      barSeriesRef.current = null;
      lastBarsRef.current = [];
      lastGridRef.current = [];
      // 新实例上的柱序列是全新的（base 回到库默认 0）→ 基线缓存一并作废，等下一轮重算
      oiBaseRef.current = null;
    }

    const { bars, sampleTimes } = buildOiBars(points, unit);
    const gridData = buildGridAnchor(gridTimes, sampleTimes);
    const bySec = new Map<number, OIPoint>();
    for (const point of points) {
      /**
       * 键必须用**柱实际落位的时点**（对齐后 = `alignedTimestamp`），否则十字线命中的是
       * 「对齐后的柱时点」，而这里按真实快照时点建键 ⇒ 读数取不到值。
       * 取到的 `point` 仍是**真实样本对象**：读数条显示真实快照时间与数值。
       */
      const ms = point.alignedTimestamp ?? point.timestamp;
      if (Number.isFinite(ms) && ms > 0) {
        bySec.set(toChartSeconds(ms), point);
      }
    }
    oiBySecRef.current = bySec;

    // 本图真实数据域 = 真实样本 ∪ 网格锚点（升序去重后的最小/最大时点）
    let first: number | null = null;
    let last: number | null = null;
    const track = (sec: number) => {
      if (first === null || sec < first) first = sec;
      if (last === null || sec > last) last = sec;
    };
    for (const sec of sampleTimes) track(sec);
    for (const item of gridData) track(item.time as number);
    lifecycle.lastBarSecRef.current = last;
    if (lifecycle.memberRef.current) {
      lifecycle.memberRef.current.domain = first !== null && last !== null ? { from: first, to: last } : null;
    }
    // 真实间隔中位数：不把「最后 24 条」当「近 24 小时」，也不假设固定周期
    if (sampleTimes.length >= 2) {
      const diffs: number[] = [];
      for (let i = 1; i < sampleTimes.length; i += 1) diffs.push(sampleTimes[i] - sampleTimes[i - 1]);
      diffs.sort((a, b) => a - b);
      stepSecRef.current = diffs[Math.floor(diffs.length / 2)] || 60;
    }
    followToleranceRef.current = Math.max(60, stepSecRef.current / 2);

    // 先记录当前视窗：实时刷新不得让视窗跳回
    const previousRange = chart.timeScale().getVisibleRange();
    const viewChanged = lifecycle.appliedViewKeyRef.current !== viewKey;
    if (viewChanged) {
      lifecycle.appliedViewKeyRef.current = viewKey;
      lifecycle.initRef.current = false;
    }

    // 柱序列：图表实例上不存在则创建（柱高=该时点总量；柱色由 buildOiBars 统一给成
    // COLOR.oiBar，逐点下发但取值恒等，不承载方向 / 增减语义）
    if (!barSeriesRef.current) {
      barSeriesRef.current = chart.addSeries(HistogramSeries, {
        priceLineVisible: false,
        lastValueVisible: true,
      });
    }
    const barSeries = barSeriesRef.current;

    // 价格格式：只在精度档位变化时下发，避免无谓的选项抖动。
    //
    // 2026-10-12（用户指令「持仓数量单位按万跟亿计算，不要全部显示阿拉伯数字」）：
    // OI 柱形的纵轴刻度 / 末值标签 / 十字线标签一律走 **万·亿** 口径
    // （官方 `type:'custom'` + formatter），不再把 1234567.89 这样的原始阿拉伯数字铺到轴上。
    // 数量（张/标的币）与名义额（USD）两种模式同口径；读数条本来就已是万/亿，此处与之对齐。
    // 只改**显示**，不改数值：柱高与读数仍是来源原值，不做任何换算。
    const values: number[] = [];
    for (const bar of bars) values.push(bar.value);
    const format = inferValueFormat(values);
    const formatKey = `${format.precision}:${format.minMove}:wanyi`;
    if (lastFormatKeyRef.current !== formatKey) {
      lastFormatKeyRef.current = formatKey;
      barSeries.applyOptions({
        priceFormat: {
          type: 'custom',
          minMove: format.minMove,
          formatter: (price: number) => formatWanYi(price, '', 2),
        },
      });
    }

    // 网格锚点先落位，再落真实柱（柱形天然断柱：缺口/空值处没有柱，无需分段）
    applySeriesUpdate<WhitespaceData>(gridSeries, lastGridRef.current, gridData, viewChanged);
    lastGridRef.current = gridData;
    applySeriesUpdate<HistogramData>(barSeries, lastBarsRef.current, bars, viewChanged);
    lastBarsRef.current = bars;
    // 柱值已更新 → 按新的可视窗重算基线（首次落位前视窗可能尚未就绪，下面还会再算一次）
    recomputeOiBase();

    // 同 K 线图：必须等到本图确有真实时间点才做首次落位（空时间轴调用官方 API 会抛错）
    if (!lifecycle.initRef.current) {
      if (first === null || last === null) return;
      lifecycle.initRef.current = true;
      applyInitialViewport(
        chart,
        syncHub,
        lifecycle.memberRef.current as ChartSyncMember,
        { from: first, to: last },
        initialViewport,
      );
      lifecycle.alignPriceScales();
      return;
    }

    /**
     * OI 图是时间窗的**跟随方**：数据更新后必须**重新对齐主图当前范围**，
     * 绝不 scrollToRealTime / fitContent —— 否则会把主图（K 线）拉回最新。
     *
     * 为什么不能沿用「本图先前的可见范围」：跟随图的 `setData` 会让官方库按**新点集**
     * 重算它自己的范围（切周期/补更早历史后尤其明显），此时把它自己那份过期范围写回去，
     * 就会覆盖掉主图刚同步过来的窗口 —— 实测切周期/补历史后 1m 上两图相差 1576px。
     * 因此改为「对齐主图当前逻辑范围」；只有主图暂无范围时才回落到保持自身范围。
     *
     * 本图不调用 `scheduleReport`：它的视窗上报通道是 `noopViewportChange`，
     * 而且 follower 的范围变化被同步层禁止反向驱动主图（见 `rangeHandler`）。
     */
    const currentMember = lifecycle.memberRef.current;
    if (currentMember && canSetVisibleRange(currentMember)) {
      if (!alignToLeader(syncHub, currentMember) && previousRange) {
        safeTimeScaleCall(() => chart.timeScale().setVisibleRange(previousRange));
      }
    }
    lifecycle.alignPriceScales();
  }, [points, unit, gridTimes, viewKey, initialViewport, syncHub, lifecycle, recomputeOiBase]);

  return (
    <div className="relative w-full h-full">
      <div ref={lifecycle.containerRef} className="w-full h-full" data-chart="lightweight-oi" />
      {points.length === 0 && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
          <span className="text-[13px] text-neutral-400">{emptyText}</span>
        </div>
      )}
    </div>
  );
};

/** memo 化：理由同 `CandleVolumeChart`（阻断「视窗上报 → 重渲染」的重绘循环）。 */
export const OiBarChart = React.memo(OiBarChartInner);
OiBarChart.displayName = 'OiBarChart';
