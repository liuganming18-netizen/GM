/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useMemo, useEffect, useRef, useCallback } from 'react';
import { Candle, TokenMarketItem, Timeframe, CryptoSignal, OIPoint } from '../types/crypto';
import { TokenIcon } from './TokenIcon';
import {
  CandleVolumeChart,
  OI_BAR_COLOR,
  OiBarChart,
  USER_DRIVEN_SYNC_MS,
  buildCandleGrid,
  createChartSyncHub,
  mergeSortedUnique,
  oiSampleTimes,
  toChartSeconds,
  type ChartHandle,
  type ChartHover,
  type ChartPriceLine,
  type ChartSyncHub,
  type ChartViewport,
} from './LightweightChart';
import { LineStyle } from 'lightweight-charts';
import { adaptCandles, alignOiToTimeframe } from '../api/adapters';
import { apiGet, marketPath } from '../api/client';
import type { RawCandlesEnvelope } from '../api/types';
import type { PublicExtrasView } from '../api/useMarketData';
import { formatWanYi, formatAmountWanYi, formatOiQuantityUnit } from '../utils/formatters';
import {
  TrendingUp,
  Layers,
  Info,
  Maximize2,
  ChevronDown,
  Calendar,
  AlertCircle,
  HelpCircle,
  Clock
} from 'lucide-react';

interface InlineExpandedChartProps {
  token: TokenMarketItem;
  activeSignal?: CryptoSignal;
  publicExtras?: PublicExtrasView;
  onClose?: () => void;
}

/**
 * 展示层可选周期（顺序与文案保持不变；提取到模块级仅为让「按交易所过滤」的结果保持稳定引用）。
 *
 * **1m 已按用户指令永久删除**：后端 `realtime_collector.py` 的
 * `TF_ORDER_RT = ["1d","12h","8h","4h","1h","30m","15m","5m"]`（53 号任务：不下载、不订阅、不写库，
 * 原始 1m 数据亦已永久删除，释放约 32.74 GiB）。采集器已于 2026-10-10T13:39:44Z 重载生效
 * （日志：`WS 币安 K 线已连接（100 标的 × 8 周期 = 800 条流）`，由 9 周期降为 8）。
 *
 * 若前端继续提供 1m，则**每个标的的展开图都会停在一个冻结的旧窗口上**：
 * 实测 BTCUSDT 1m 闭合序列止于 `2026-10-10T16:29:00Z`、WS 尾段止于 `2026-10-10T13:39:00Z`
 * （落后约 19 小时），而顶栏实时价已是当前值 —— 这正是「K线/OI 显示不正确」的根源。
 *
 * 处置与既有「OKX 无原生 8h ⇒ 不提供该周期」完全一致：不显示按钮、不给空态占位、不跨周期替代。
 */
const ALL_TIMEFRAMES: Timeframe[] = ['5m', '15m', '30m', '1h', '4h', '8h', '12h', '1d'];
const TIMEFRAME_MS: Record<Timeframe, number> = {
  '5m': 300_000,
  '15m': 900_000,
  '30m': 1_800_000,
  '1h': 3_600_000,
  '4h': 14_400_000,
  '8h': 28_800_000,
  '12h': 43_200_000,
  '1d': 86_400_000,
};

type LoadedCandleHistory = { tokenId: string; byTf: Partial<Record<Timeframe, Candle[]>> };
type LocalHistoryPage = { cursor: string | null; hasMore: boolean; loading: boolean; loadedPages: number; error: string | null };

/**
 * 分页历史（本地库存）单页根数。
 *
 * 取 1500 对齐**原版实测口径**：原版首屏调 `Qme(symbol, 1500)` 一次取满 1500 根，
 * 且其合并函数 `$me` 末尾是 `.slice(-1500)` —— 即原版把「已导入的 K 线」定为 1500 根。
 * 本项目走本地库存分页（`history_only=1`，后端单页上限 2000），故首屏一页取 1500 根，
 * 与「展开图实时尾段 180 根」（`DETAIL_BAR_LIMIT`）合并后，首屏即具备与原版同量级的历史。
 * 实测该页 581 KB / 57 ms（本地后端），后续每次「加载更早历史」再取一页。
 */
const HISTORY_PAGE_LIMIT = 1500;

function mergeLoadedCandles(existing: Candle[], incoming: Candle[]): Candle[] {
  if (incoming.length === 0) return existing;
  const left = existing.filter((c) => Number.isFinite(c.timestamp) && c.timestamp > 0);
  const right = incoming.filter((c) => Number.isFinite(c.timestamp) && c.timestamp > 0);
  if (left.length === 0) return [...right].sort((a, b) => a.timestamp - b.timestamp);
  if (right.length === 0) return left;
  // 两边通常已按时间排序（较早页在左，最新详情在右）；线性归并保留所有已读页。
  const a = [...left].sort((x, y) => x.timestamp - y.timestamp);
  const b = [...right].sort((x, y) => x.timestamp - y.timestamp);
  const merged: Candle[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i].timestamp < b[j].timestamp) merged.push(a[i++]);
    else if (a[i].timestamp > b[j].timestamp) merged.push(b[j++]);
    else { merged.push(b[j++]); i += 1; } // 重叠时间以最新响应为准
  }
  while (i < a.length) merged.push(a[i++]);
  while (j < b.length) merged.push(b[j++]);
  return merged;
}

function sameCandleHistory(left: Candle[], right: Candle[]): boolean {
  return left.length === right.length && left.every((candle, index) => {
    const next = right[index];
    return candle.timestamp === next.timestamp && candle.open === next.open && candle.high === next.high &&
      candle.low === next.low && candle.close === next.close && candle.volume === next.volume &&
      candle.volumeUsd === next.volumeUsd && candle.isClosed === next.isClosed;
  });
}

const CHART_TIMEFRAME_STORAGE_KEY = 'ai-studio.chart-timeframe.v1';
/**
 * 视窗持久化键（替换原手写 SVG 版的 `ai-studio.chart-view.v1`）。
 * 原键存的是手写坐标域，与官方库的时间视窗不是同一套口径，故另立新键、不读取旧键。
 */
const CHART_VIEWPORT_STORAGE_KEY = 'ai-studio.lwc-viewport.v1';

function readSessionMap(key: string): Record<string, unknown> {
  try {
    const raw = window.sessionStorage.getItem(key);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

function writeSessionMapValue(storageKey: string, itemKey: string, value: unknown): void {
  try {
    const current = readSessionMap(storageKey);
    current[itemKey] = value;
    window.sessionStorage.setItem(storageKey, JSON.stringify(current));
  } catch {
    // Session storage may be disabled; in-memory chart interaction still works.
  }
}

function readStoredTimeframe(tokenId: string): { timeframe: Timeframe; userPicked: boolean } | null {
  const value = readSessionMap(CHART_TIMEFRAME_STORAGE_KEY)[tokenId] as
    | { timeframe?: unknown; userPicked?: unknown }
    | undefined;
  if (!value || !ALL_TIMEFRAMES.includes(value.timeframe as Timeframe)) return null;
  return { timeframe: value.timeframe as Timeframe, userPicked: value.userPicked === true };
}

/** 会话内已存视窗（毫秒时间范围），用于「收起再展开」保持同一视窗。 */
function readStoredViewport(key: string): ChartViewport | null {
  const value = readSessionMap(CHART_VIEWPORT_STORAGE_KEY)[key] as Partial<ChartViewport> | undefined;
  if (
    !value ||
    !Number.isFinite(value.fromMs) ||
    !Number.isFinite(value.toMs) ||
    (value.toMs ?? 0) <= (value.fromMs ?? 0)
  ) {
    return null;
  }
  return {
    fromMs: value.fromMs as number,
    toMs: value.toMs as number,
    followLatest: value.followLatest === true,
  };
}

/* ------------------------------------------------------------------ *
 * 模块级会话缓存（**跨组件挂载存活**）
 * ------------------------------------------------------------------ *
 * 为什么必须是模块级：展开图是「收起即卸载、展开即重新挂载」的组件，
 * 组件内的 `useRef` / `useState` 在收起时**全部丢失**。实测缺陷正是如此：
 * 收起再展开后只剩列表带来的首屏 60 根，且用户视窗被首屏 `fitContent` 顶掉。
 * 因此把「最后用户视窗」与「已加载历史」提到模块级，键为 `${tokenId}:${tf}`，
 * 使同一会话内同一标的同一周期的收起/展开能**原样恢复**。
 * 真实数据不因此变化：历史仍是真实 K 线（只是不再重复下载），视窗仍是官方库上报的真实 UTC 时间范围。
 * ------------------------------------------------------------------ */

/** 该 token:tf 的最后用户视窗（模块级，优先于 sessionStorage）。 */
const viewportSessionCache = new Map<string, ChartViewport>();

/** 该 token:tf 已加载的历史根 + 分页游标（模块级），避免收起再展开重复下载。 */
interface CachedHistoryEntry {
  candles: Candle[];
  page: LocalHistoryPage | null;
}
const historySessionCache = new Map<string, CachedHistoryEntry>();
/** 上限：会话内最多缓存 24 个 `token:tf`（超出按最早写入淘汰），避免长会话内存无界增长。 */
const HISTORY_SESSION_CACHE_LIMIT = 24;

function historyCacheKey(tokenId: string, tf: Timeframe): string {
  return `${tokenId}:${tf}`;
}

function readCachedHistory(tokenId: string, tf: Timeframe): CachedHistoryEntry | null {
  return historySessionCache.get(historyCacheKey(tokenId, tf)) ?? null;
}

function writeCachedHistory(tokenId: string, tf: Timeframe, entry: CachedHistoryEntry): void {
  const key = historyCacheKey(tokenId, tf);
  if (historySessionCache.has(key)) historySessionCache.delete(key);
  historySessionCache.set(key, entry);
  while (historySessionCache.size > HISTORY_SESSION_CACHE_LIMIT) {
    const oldest = historySessionCache.keys().next().value;
    if (oldest === undefined) break;
    historySessionCache.delete(oldest);
  }
}

function readCachedViewport(key: string): ChartViewport | null {
  return viewportSessionCache.get(key) ?? null;
}

function writeCachedViewport(key: string, viewport: ChartViewport): void {
  viewportSessionCache.set(key, viewport);
}

/** 重新挂载时用模块级缓存预热「已加载历史 + 分页游标」。 */
function seedHistoryFromCache(tokenId: string): {
  byTf: Partial<Record<Timeframe, Candle[]>>;
  pages: Partial<Record<Timeframe, LocalHistoryPage>>;
} {
  const byTf: Partial<Record<Timeframe, Candle[]>> = {};
  const pages: Partial<Record<Timeframe, LocalHistoryPage>> = {};
  for (const tf of ALL_TIMEFRAMES) {
    const entry = readCachedHistory(tokenId, tf);
    if (!entry) continue;
    if (entry.candles.length > 0) byTf[tf] = entry.candles;
    if (entry.page) pages[tf] = { ...entry.page, loading: false };
  }
  return { byTf, pages };
}

/* ------------------------------------------------------------------ *
 * 恢复落实决策（纯函数，导出以便可执行验证）
 * ------------------------------------------------------------------ */

/**
 * 下一步该做什么：
 *  - `apply`         —— 恢复区间已被真实数据覆盖（或本地已到最早记录）→ 现在就把用户视窗**原样**落实；
 *  - `load-earlier`  —— 起点早于已加载的最早根、且本地仍有更早历史 → 先补一页再落实；
 *  - `idle`          —— 无恢复目标 / 未在恢复中 / 正在加载中 → 什么都不做。
 *
 * 提取成纯函数的理由：「收起→再展开」缺陷就出在这条判定上（旧实现要求恢复范围
 * **完全落在已加载域内**，首屏只有一页时必然被拒 → 退回 `fitContent` → 用户视窗被顶掉）。
 * 纯函数化后可被脚本逐例验证，不必只靠肉眼。
 */
export type RestoreStep = 'apply' | 'load-earlier' | 'idle';

export function nextRestoreStep(input: {
  pending: boolean;
  target: { fromMs: number; toMs: number } | null;
  /** 当前已加载的最早一根的真实时间（毫秒）；无数据为 null */
  earliestLoadedMs: number | null;
  /** 本地是否还有更早历史；分页信息未知为 null */
  hasMore: boolean | null;
  loading: boolean;
}): RestoreStep {
  if (!input.pending || !input.target) return 'idle';
  const covered = input.earliestLoadedMs !== null && input.earliestLoadedMs <= input.target.fromMs;
  const exhausted = input.hasMore === false;
  if (covered || exhausted) return 'apply';
  if (input.loading) return 'idle';
  return 'load-earlier';
}

const isNum = (v: number | null | undefined): v is number =>
  v !== null && v !== undefined && Number.isFinite(v);

/** 原版风格：数值原样输出（OHLC / 均价，真实池价格可低至 1.25e-05，固定位数会被抹平）。 */
const fmtRaw = (v: number | null | undefined): string => (isNum(v) ? String(v) : '—');

const InlineExpandedChartInner: React.FC<InlineExpandedChartProps> = ({
  token,
  activeSignal,
  publicExtras,
  onClose,
}) => {
  /**
   * 周期可用性（用户明确口径）：主接**币安 USDT 加密币永续**，OKX 仅作**补充**。
   * 币安有原生 8h → 保留；**OKX 无原生 8h → 不提供该周期**
   * （不显示按钮、不给空态占位，也不跨所拼接；后端对 OKX 8h 返回 `is_native: false`、`bars: []`）。
   * 本改动**只决定「8h 这一个按钮是否出现」**，其余原版周期顺序/文案/布局/模块一律不变。
   */
  const isOkx = /okx/i.test(token.exchange ?? '');
  const timeframes: Timeframe[] = useMemo(
    () => (isOkx ? ALL_TIMEFRAMES.filter((tf) => tf !== '8h') : ALL_TIMEFRAMES),
    [isOkx],
  );
  /** 默认周期优先展示日线（1d）；已存周期若已不再提供则回落到 1d。 */
  const [selectedTf, setSelectedTf] = useState<Timeframe>(() => readStoredTimeframe(token.id)?.timeframe ?? '1d');
  const [oiUnit, setOiUnit] = useState<'amount' | 'usd'>('amount');
  /** 十字线悬停态：由官方库的 `subscribeCrosshairMove` 驱动（不再是数组下标） */
  const [hover, setHover] = useState<ChartHover | null>(null);
  /** 官方库当前视窗（毫秒），仅用于「数据源 / 缺口」状态位的视窗文案 */
  const [viewportState, setViewportState] = useState<{ key: string; viewport: ChartViewport } | null>(null);
  /**
   * 重新挂载（收起→再展开）时用模块级缓存预热「已加载历史根 + 分页游标」。
   * 只在本次挂载的首帧计算一次，不参与后续重渲染。
   */
  const seededHistoryRef = useRef<ReturnType<typeof seedHistoryFromCache> | null>(null);
  if (seededHistoryRef.current === null) seededHistoryRef.current = seedHistoryFromCache(token.id);
  const seededHistory = seededHistoryRef.current;
  const [loadedHistory, setLoadedHistory] = useState<LoadedCandleHistory>(() => ({
    tokenId: token.id,
    byTf: seededHistory.byTf,
  }));
  const [historyPages, setHistoryPages] = useState<Partial<Record<Timeframe, LocalHistoryPage>>>(
    () => seededHistory.pages,
  );
  const historyPagesRef = useRef<Partial<Record<Timeframe, LocalHistoryPage>>>(seededHistory.pages);
  const fetchedHistoryPages = useRef(new Set<string>());
  const historyLoadKey = useRef<string | null>(null);

  /**
   * 图表价格线（触发价、现价、止损价、目标价）。
   *
   * **2026-10-15 闭环整改·阶段A（假展示清理）**：删除 BTC 兜底价
   * 67,150 / 67,200 / 66,400 / 69,000（无信号时凭空画线），并**不再把
   * `activeSignal.currentPrice` 当作「入场价」**（现价冒充入场价）。
   * 现规则：
   *   · 只用信号**自带真实字段**；缺失 ⇒ 不画该线；
   *   · 「现价」线如实标注为**现价**；
   *   · **成交/入场价只由服务端账本 fill 产生**（由成交标记绘制），此处不代填。
   */
  const chartPriceLines: ChartPriceLine[] = useMemo(() => {
    const lines: ChartPriceLine[] = [];

    const trigger = activeSignal?.triggerPrice ?? null;
    const current = activeSignal?.currentPrice ?? null;
    const stopLoss = activeSignal?.stopLossPrice ?? null;
    const target = activeSignal?.targetPrice ?? null;

    if (trigger !== null && trigger !== undefined && Number.isFinite(trigger) && trigger > 0) {
      lines.push({
        price: trigger,
        title: `触发价 ${trigger.toLocaleString()}`,
        color: '#38bdf8',
        lineStyle: LineStyle.Dashed,
        lineWidth: 1,
      });
    }

    if (current !== null && current !== undefined && Number.isFinite(current) && current > 0) {
      lines.push({
        price: current,
        title: `现价 ${current.toLocaleString()}`,
        color: '#10b981',
        lineStyle: LineStyle.Solid,
        lineWidth: 2,
      });
    }

    if (stopLoss !== null && stopLoss !== undefined && Number.isFinite(stopLoss) && stopLoss > 0) {
      lines.push({
        price: stopLoss,
        title: `止损价 ${stopLoss.toLocaleString()}`,
        color: '#ef4444',
        lineStyle: LineStyle.Dashed,
        lineWidth: 1,
      });
    }

    if (target !== null && target !== undefined && Number.isFinite(target) && target > 0) {
      lines.push({
        price: target,
        title: `目标价 ${target.toLocaleString()}`,
        color: '#a855f7',
        lineStyle: LineStyle.Dashed,
        lineWidth: 1,
      });
    }

    return lines;
  }, [activeSignal]);

  /**
   * 官方图表句柄与同组同步枢纽。
   * - `syncHub`：K 线与 OI 两张官方图按**真实时间窗**互相同步；
   * - `candleHandleRef` / `oiHandleRef`：跨图十字线联动（用官方 `setCrosshairPosition`）；
   * - `crosshairSyncRef`：联动镜像回调的忽略闸，避免状态覆盖与回环。
   */
  const syncHubRef = useRef<ChartSyncHub | null>(null);
  if (syncHubRef.current === null) syncHubRef.current = createChartSyncHub();
  const syncHub = syncHubRef.current;
  const candleHandleRef = useRef<ChartHandle | null>(null);
  const oiHandleRef = useRef<ChartHandle | null>(null);
  const crosshairSyncRef = useRef(false);
  const viewKeyRef = useRef('');
  /**
   * 视窗恢复门闸。
   *
   * `true` = 本次挂载仍处于「按用户视窗恢复」阶段：此时图表上报的可见范围来自
   * **程序化落位**（首屏 60 根的 `fitContent`、或尚未初始化的 OI 图落位），
   * 一律**不得写回存储** —— 否则用户视窗会被首屏默认窗口顶掉（实测缺陷）。
   * 解除条件：恢复区间已被真实数据覆盖并原样落实，或用户主动交互（滚轮/拖动/触摸）。
   */
  const restorePendingRef = useRef(false);
  /** 已为哪个 viewKey 初始化过门闸，避免重渲染时反复重置。 */
  const restoreKeyRef = useRef<string | null>(null);

  /**
   * 真实数据适配：原版默认周期为 1m，且原版样例数据每个周期都有值，所以「默认 1m」是原版交互。
   * 真实池里多数合约没有 1m（只有 1d/12h/8h/4h/1h），因此这里沿用原版「展开时停在默认周期」的行为，
   * 只把默认值收敛为「按原版顺序的第一个有数据周期」。
   *
   * 关键点：明细是按周期并发请求、**返回顺序不定**，若只做一次性判定，会停在先返回的周期
   * （实测 BTC 会停在 5m，从而看不到 1m 的实时尾段）。因此改为**单调收敛**：
   * 只要原版顺序中更靠前的周期已有数据，就切过去；收敛后不再变化。
   * 用户一旦手动点过周期，就不再自动改。
   */
  const tfPickedByUser = useRef(readStoredTimeframe(token.id)?.userPicked ?? false);
  const timeframeTokenId = useRef(token.id);
  useEffect(() => {
    if (timeframeTokenId.current === token.id) return;
    timeframeTokenId.current = token.id;
    const saved = readStoredTimeframe(token.id);
    tfPickedByUser.current = saved?.userPicked ?? false;
    setSelectedTf(saved?.timeframe ?? '5m');
    historyLoadKey.current = null;
    // 切标的：用**新标的**的模块级缓存预热（没有就为空），不沿用上一个标的的历史
    const seeded = seedHistoryFromCache(token.id);
    historyPagesRef.current = seeded.pages;
    fetchedHistoryPages.current.clear();
    setHistoryPages(seeded.pages);
    setLoadedHistory({ tokenId: token.id, byTf: seeded.byTf });
    setHover(null);
    setViewportState(null);
  }, [token.id]);

  useEffect(() => {
    writeSessionMapValue(CHART_TIMEFRAME_STORAGE_KEY, token.id, {
      timeframe: selectedTf,
      userPicked: tfPickedByUser.current,
    });
  }, [token.id, selectedTf]);

  const loadHistoryPage = async (tf: Timeframe, initial: boolean) => {
    const existing = historyPagesRef.current[tf] ?? {
      cursor: null, hasMore: true, loading: false, loadedPages: 0, error: null,
    };
    if (existing.loading || (!initial && !existing.hasMore)) return;
    if (initial && existing.loadedPages > 0) return; // 切回已加载周期直接复用
    const before = initial ? null : existing.cursor;
    const requestKey = `${token.id}:${tf}:${before ?? 'latest'}`;
    if (fetchedHistoryPages.current.has(requestKey)) return;
    fetchedHistoryPages.current.add(requestKey);
    historyLoadKey.current = requestKey;
    const mark = (next: LocalHistoryPage) => {
      historyPagesRef.current = { ...historyPagesRef.current, [tf]: next };
      setHistoryPages((previous) => ({ ...previous, [tf]: next }));
    };
    mark({ ...existing, loading: true, error: null });
    const [exchange, ...marketParts] = token.id.split(':');
    const marketId = marketParts.join(':');
    try {
      const cursor = before ? `&before=${encodeURIComponent(before)}` : '';
      const env = await apiGet<RawCandlesEnvelope>(
        `${marketPath(exchange, marketId, 'candles')}?timeframe=${encodeURIComponent(tf)}&limit=${HISTORY_PAGE_LIMIT}&history_only=1${cursor}`,
      );
      if (historyLoadKey.current !== requestKey) {
        fetchedHistoryPages.current.delete(requestKey);
        if (timeframeTokenId.current === token.id) {
          const pending = historyPagesRef.current[tf];
          if (pending?.loading) mark({ ...pending, loading: false });
        }
        return; // 旧标的/周期的迟到响应不覆盖当前图
      }
      const incoming = adaptCandles(env, tf);
      setLoadedHistory((previous) => {
        const sameToken = previous.tokenId === token.id;
        const byTf = sameToken ? { ...previous.byTf } : {};
        byTf[tf] = mergeLoadedCandles(byTf[tf] ?? [], incoming);
        return { tokenId: token.id, byTf };
      });
      const hasMore = env.has_more === true;
      const nextCursor = env.next_cursor ?? null;
      if (hasMore && !nextCursor) throw new Error('历史分页缺少游标');
      mark({ cursor: hasMore ? nextCursor : null, hasMore, loading: false,
        loadedPages: existing.loadedPages + 1, error: null });
    } catch (error) {
      if (historyLoadKey.current !== requestKey) {
        fetchedHistoryPages.current.delete(requestKey);
        if (timeframeTokenId.current === token.id) {
          const pending = historyPagesRef.current[tf];
          if (pending?.loading) mark({ ...pending, loading: false });
        }
        return;
      }
      fetchedHistoryPages.current.delete(requestKey);
      mark({ ...existing, loading: false, error: (error as Error).message });
    } finally {
      if (historyLoadKey.current === requestKey) historyLoadKey.current = null;
    }
  };

  const handleSelectTf = (tf: Timeframe) => {
    tfPickedByUser.current = true;
    writeSessionMapValue(CHART_TIMEFRAME_STORAGE_KEY, token.id, { timeframe: tf, userPicked: true });
    setSelectedTf(tf);
    void loadHistoryPage(tf, true);
  };

  /**
   * 首屏历史页补齐（本次修复：K 线「没有完整导入」）。
   *
   * 缺陷：展开图首屏只有 `ensureDetail` 拉的 60 根实时尾段（`DETAIL_BAR_LIMIT = 60`），
   * 而**分页历史一页都没拉过**，原因有四层叠加：
   *   ① `loadHistoryPage(tf, true)` 只在用户手点周期按钮（`handleSelectTf`）时才被调用；
   *   ② 正常首屏走的是「自动收敛」出的 `selectedTf`（下面那个 effect），它从不触发补页；
   *   ③ 于是 `historyPages[selectedTf]` 恒为空 → `activeHistoryPage` 为 `undefined`
   *      → 「加载更早历史」按钮的 `activeHistoryPage?.hasMore` 为 `undefined` → 按钮根本不渲染；
   *   ④ 唯一的自动补页 effect 又被 `restorePendingRef` 挡住（无存档视窗时它是 `false`，直接 return）。
   * 四者叠加 ⇒ 展开图永远卡在 60 根（1m 约 1 小时），且**没有任何途径**补更早历史；
   * 观感就是「K 线没有完整导入」——但后端数据连续、分页接口也正常（实测
   * `history_only=1` 按 `HISTORY_PAGE_LIMIT` 如数返回 + `has_more:true` + `next_cursor`），
   * 纯粹是前端没请求。
   *
   * 修法：为「当前标的 × 当前周期」补一次**首屏历史页**（`initial = true`）。该调用天然幂等：
   *   · `loadHistoryPage` 对「已加载过（`loadedPages > 0`）」直接 return（收起再展开时命中模块级缓存）；
   *   · 对「同 `requestKey` 已请求过」也直接 return（与 `handleSelectTf`、恢复流程互不重复发请求）。
   * 它只**追加真实历史根**（走 `mergeLoadedCandles` 合并，不合成、不重采样、不借别的周期），
   * 并使「加载更早历史」按钮按其原有条件正常出现，用户可继续逐页回溯。
   */
  const historySeedKey = useRef<string | null>(null);
  useEffect(() => {
    const key = `${token.id}:${selectedTf}`;
    if (historySeedKey.current === key) return;
    historySeedKey.current = key;
    void loadHistoryPage(selectedTf, true);
    // loadHistoryPage 每次渲染重建（闭包捕获最新 token），用 historySeedKey 保证「每个 标的×周期」只播种一次。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token.id, selectedTf]);
  useEffect(() => {
    if (tfPickedByUser.current) return;
    const hasData = (tf: Timeframe) =>
      (token.candles[tf] ?? []).length > 0 || !!token.formingCandles?.[tf];
    const firstWithData = timeframes.find(hasData);
    if (!firstWithData) return; // 全部可见周期都还没数据：保持默认周期（5m）的空态
    const cur = timeframes.indexOf(selectedTf);
    const tgt = timeframes.indexOf(firstWithData);
    // 当前周期没数据（如该合约确无 5m），或存在更靠前（最细）的有数据周期 → 收敛过去
    if (cur < 0 || !hasData(selectedTf) || tgt < cur) setSelectedTf(firstWithData);
  }, [token.candles, token.formingCandles, selectedTf]);

  /**
   * 周期可用性收尾：若当前选中周期**不在可见列表内**（例如从币安 8h 切到 OKX 补充标的，
   * 而 OKX 无原生 8h），切换到**该标的已有的原生周期**（优先第一个有数据的可见周期）。
   * 该判定不受「用户是否手动点过周期」影响 —— 因为此时选中的周期已不存在。
   * 币安（可见列表含全部 8 个周期）永不触发，选择行为与原版一致。
   */
  useEffect(() => {
    if (timeframes.includes(selectedTf)) return;
    const hasData = (tf: Timeframe) =>
      (token.candles[tf] ?? []).length > 0 || !!token.formingCandles?.[tf];
    setSelectedTf(timeframes.find(hasData) ?? timeframes[0]);
  }, [timeframes, selectedTf, token.candles, token.formingCandles]);

  /**
   * 闭合历史序列：**只**取当前周期自己的 `candles`。
   * 原版此处存在 `|| token.candles['1m']` 的跨周期回退，会把 1 分钟数据画在 4h/1d 标题下，
   * 属于「拿别周期替代」——已按规约删除；该周期没有入库就是空数组 → 原版空态。
   */
  const incomingCandles = token.candles[selectedTf] ?? [];
  const cachedCandles = loadedHistory.tokenId === token.id ? loadedHistory.byTf[selectedTf] ?? [] : [];
  const candles = useMemo(
    () => mergeLoadedCandles(cachedCandles, incomingCandles),
    [cachedCandles, incomingCandles],
  );

  useEffect(() => {
    setLoadedHistory((previous) => {
      const sameToken = previous.tokenId === token.id;
      const nextByTf: Partial<Record<Timeframe, Candle[]>> = sameToken ? { ...previous.byTf } : {};
      let changed = !sameToken;
      for (const tf of ALL_TIMEFRAMES) {
        const incoming = token.candles[tf] ?? [];
        if (incoming.length === 0) continue;
        const merged = mergeLoadedCandles(nextByTf[tf] ?? [], incoming);
        if (!sameCandleHistory(nextByTf[tf] ?? [], merged)) {
          nextByTf[tf] = merged;
          changed = true;
        }
      }
      return changed ? { tokenId: token.id, byTf: nextByTf } : previous;
    });
  }, [token.id, token.candles]);

  /**
   * 已加载历史写回模块级缓存：收起（卸载）后仍能原样恢复，不必重复下载同一页。
   * 分页游标一并保存；若本轮没有新的分页信息则**保留缓存里已有的游标**，不覆盖成 null。
   */
  useEffect(() => {
    if (loadedHistory.tokenId !== token.id) return;
    for (const tf of ALL_TIMEFRAMES) {
      const list = loadedHistory.byTf[tf];
      if (!list || list.length === 0) continue;
      const page = historyPages[tf] ?? readCachedHistory(token.id, tf)?.page ?? null;
      writeCachedHistory(token.id, tf, { candles: list, page });
    }
  }, [loadedHistory, historyPages, token.id]);

  /**
   * 未收盘临时层：后端 `forming_bar` 单独映射而来（`isClosed: false`），与 `candles` 严格分离。
   * 它不写进 `candles`，因此不参与缺口判定、迷你图与成交量口径，也不会被当成历史闭合记录。
   * 缺失即为 null（空态），不合成、不借别的周期。
   * 官方库没有「虚线蜡烛」样式，故改用**单根独立颜色/边框** + 明确的「未收盘」文字标识区分，
   * 绝不假装闭合。
   */
  const formingRaw = token.formingCandles?.[selectedTf] ?? null;
  /**
   * **陈旧/重复尾段闸门（显示层缺陷修复）**
   *
   * `renderBars = [...candles, forming]` 会经 `buildCandleData` → `sortUniqueByTime` 做
   * 「同秒后写覆盖」。WS 尾段一旦停摆，后端仍会照旧返回 `forming_bar`，而它的时点会**落在
   * 已闭合区间之内**（甚至与某根已闭合根同秒）。此时保留它会同时造成两个错误：
   *   1. 把**真实已闭合根**在图上改写掉 —— 实测 BTCUSDT 1m `13:39` 的真实闭合根
   *      `O82785.1/H82785.1/L82781.0/C82781.0/V2.978` 被 WS 半成品 `L82785.0/C82785.0/V1.258` 顶掉；
   *   2. 读数条与「未收盘」徽标会把一根 19 小时前的历史根冒充成「当前这一根」。
   * 因此只有在未收盘层**严格晚于**最后一根已闭合根时，才认它是「正在走的这一根」；
   * 否则丢弃（闭合序列原样保留）。不合成、不挪时间戳、不猜、不借别的周期替代。
   */
  const forming = useMemo(() => {
    if (!formingRaw) return null;
    const last = candles.length ? candles[candles.length - 1] : null;
    if (!last) return formingRaw;
    if (!Number.isFinite(formingRaw.timestamp) || !Number.isFinite(last.timestamp)) return formingRaw;
    return formingRaw.timestamp > last.timestamp ? formingRaw : null;
  }, [formingRaw, candles]);
  /**
   * 绘制序列 = 闭合根 + 未收盘层（0 或 1）。
   * 用同一条渲染路径交给官方库，未收盘根走 `isClosed === false` 分支（独立琥珀色 + 页面文字标识）。
   */
  const renderBars = useMemo(() => (forming ? [...candles, forming] : candles), [candles, forming]);

  /**
   * 两图共享的**真实时间点并集**（UTC 秒）= K 线时间点集 ∪ OI 真实样本时点。
   *
   * 为什么必须并集：官方时间轴是**离散索引**（`barSpacing` 均匀，与点集密度无关），
   * 两图点集不同 ⇒ 同一时间点落在不同索引 ⇒ 即使可见 UTC 窗口一致，刻度/蜡烛与 OI 点
   * 仍会横向错位。让两图都补齐到同一并集（各自只补**纯 whitespace 锚点**，
   * 真实 K 线值 / 真实 OI 值一律不变、不重采样、不聚合），两图横坐标映射才严格一致。
   */
  const candleGridTimes = useMemo(() => buildCandleGrid(renderBars), [renderBars]);

  // OI 序列来自真实 /markets/{ex}/{mid}/oi；接口只有全市场总量，没有多空拆分。
  const oiPoints = useMemo<OIPoint[]>(() => {
    const h = token.oiHistory;
    if (!h) return [];
    if (Array.isArray(h)) return h;
    return h[selectedTf] || [];
  }, [token.oiHistory, selectedTf]);

  /**
   * OI 序列的**真实时间跨度**（首末样本时点 + 点数），用于在面板上自证数据范围。
   *
   * 为什么要有这一行：用户此前的反馈是「OI 总持仓量就几天」。根因不在数据，
   * 而在取数口径（`/oi` 默认只给 `snapshot`，且 5m 被 `LIMIT 2000` 卡死）。
   * 修好之后把跨度直接写在面板上，**不用点开也能核对**，也便于回归时一眼发现退化。
   * 只读真实 `timestamp`，不参与任何绘图或数值计算。
   */
  const oiSpanLabel = useMemo(() => {
    const ts = oiPoints
      .map((p) => p.timestamp)
      .filter((t) => Number.isFinite(t) && t > 0);
    if (ts.length < 2) return null;
    const fmt = (ms: number) => {
      const d = new Date(ms);
      const p2 = (n: number) => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`;
    };
    return `${fmt(Math.min(...ts))} → ${fmt(Math.max(...ts))} · ${ts.length} 点`;
  }, [oiPoints]);

  /**
   * **裁剪到当前 K 线窗口**的 OI 序列（两图共享时间轴的必要条件）。
   *
   * 为什么必须裁：`/oi?period=auto` 现在能一次给到**自 2020-09 起约 6 年**的归档
   * （BTCUSDT 1446 点）。但本图 K 线只有 `DETAIL_BAR_LIMIT = 180` 根，
   * 首次展开又默认「显示全部已加载数据」——若把 6 年 OI 直接并进共享并集网格，
   * 时间轴会被撑到 6 年，180 根蜡烛被挤成最右端一小撮（就是 2026-10-11 修过的
   * 「横向拉伸 / 孤立单根」缺陷的翻版）。因此显示层只取**落在本周期 K 线时点区间内**
   * 的 OI 样本：两图时间跨度一致 ⇒ 并集网格退化为 K 线网格 ⇒ 蜡烛等距铺满。
   *
   * 边界纪律：只做**筛选**（丢掉窗口外的点），不聚合、不插值、不改时间戳、不补点；
   * 窗口内一个样本都不剩（例如归档早于 K 线起点）⇒ 原样退回全量序列，
   * 由下游按真实时间自行落位，绝不造点。
   */
  const oiWindowedPoints = useMemo<OIPoint[]>(() => {
    if (oiPoints.length === 0 || candles.length === 0) return oiPoints;
    let lo = Infinity;
    let hi = -Infinity;
    for (const c of candles) {
      if (!Number.isFinite(c.timestamp) || c.timestamp <= 0) continue;
      if (c.timestamp < lo) lo = c.timestamp;
      if (c.timestamp > hi) hi = c.timestamp;
    }
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) return oiPoints;
    const win = oiPoints.filter((p) => p.timestamp >= lo && p.timestamp <= hi);
    return win.length > 0 ? win : oiPoints;
  }, [oiPoints, candles]);

  /**
   * OI **显示序列**：按当前 K 线周期对齐后的真实快照（每桶取最新一条，见 `alignOiToTimeframe`）。
   *
   * 只用于「图表落位 + 共享网格」：OI 原生约 20 分钟一个样本，与 1d/4h 这类粗周期混进同一张
   * 并集网格会把粗周期那一侧撑成「孤立单根」（根因与实测见 `alignOiToTimeframe` 注释）。
   * 读数条 / 24h 净变 / 悬停取值仍用原生 `oiPoints` —— 保留真实快照时间与全部样本条数。
   */
  const oiDisplayPoints = useMemo(
    () => alignOiToTimeframe(oiWindowedPoints, TIMEFRAME_MS[selectedTf]),
    [oiWindowedPoints, selectedTf],
  );

  /** OI 真实样本时点 + 两图共享并集（见上方注释） */
  const oiGridTimes = useMemo(() => oiSampleTimes(oiDisplayPoints), [oiDisplayPoints]);
  const sharedGridTimes = useMemo(
    () => mergeSortedUnique(candleGridTimes, oiGridTimes),
    [candleGridTimes, oiGridTimes],
  );

  const viewKey = `${token.id}:${selectedTf}`;
  viewKeyRef.current = viewKey;

  /**
   * 该 viewKey 的**恢复视窗**：模块级会话缓存优先（跨收起/展开存活），其次 sessionStorage。
   * 无则返回 null = 显示全部已加载数据（首次展开的原版行为）。
   */
  const restoreTarget = useMemo<ChartViewport | null>(
    () => readCachedViewport(viewKey) ?? readStoredViewport(viewKey),
    [viewKey],
  );
  const initialViewport = restoreTarget;

  /**
   * 门闸初始化：viewKey 变化（切币/切周期/重新展开）时，只要存在恢复目标就进入「恢复中」，
   * 期间丢弃程序化落位产生的视窗上报。
   */
  if (restoreKeyRef.current !== viewKey) {
    restoreKeyRef.current = viewKey;
    restorePendingRef.current = restoreTarget !== null;
  }

  const handleViewportChange = useCallback((viewport: ChartViewport, key: string) => {
    /**
     * **归属校验**：报告只承认「当前视图键」的。
     *
     * 切周期时官方图表实例会按 React `key={viewKey}` 原生卸载重建，
     * 旧实例的订阅/定时器已在 cleanup 中清除；这里再兜一层，
     * 确保任何迟到的旧周期报告都**不可能**落到新周期的视窗缓存上（实测缺陷根因之一）。
     */
    if (key !== viewKeyRef.current) return;
    /**
     * 恢复未落实期间：一律只更新展示读数，**不写缓存、不写存储**。
     *
     * 这里**不做**「偏离目标较多就当成真实用户变化」的推断 —— 程序化落位
     * （首屏 `fitContent`、数据重置、历史补齐后的重新落实）在异步生效过程中
     * 可能先后报出与目标相差很大的中间范围，把它当成用户视窗就会覆盖掉用户目标。
     * 解除门闸的唯一途径是**真实输入事件**（滚轮/拖动/触摸 → `markUserViewport`）。
     */
    if (restorePendingRef.current) {
      setViewportState((previous) =>
        previous?.key === key &&
        previous.viewport.fromMs === viewport.fromMs &&
        previous.viewport.toMs === viewport.toMs &&
        previous.viewport.followLatest === viewport.followLatest
          ? previous
          : { key, viewport },
      );
      return;
    }
    writeCachedViewport(key, viewport);
    writeSessionMapValue(CHART_VIEWPORT_STORAGE_KEY, key, viewport);
    setViewportState((previous) =>
      previous?.key === key &&
      previous.viewport.fromMs === viewport.fromMs &&
      previous.viewport.toMs === viewport.toMs &&
      previous.viewport.followLatest === viewport.followLatest
        ? previous
        : { key, viewport },
    );
  }, []);
  /** OI 图是时间窗跟随方，不写视窗持久化（避免与主图互相覆盖）。 */
  const noopViewportChange = useCallback(() => {}, []);

  /**
   * 用户主动接管视窗（滚轮缩放 / 拖动平移 / 触摸）→ 立即解除恢复门闸。
   *
   * 只有**真实用户交互**才能解除：程序化落位（首屏 `fitContent`、数据重置、
   * 历史补齐后的重新落位）一律不得写入新周期的视窗缓存。
   * 同时把同步枢纽的「用户驱动窗口」推到未来 500ms，使 OI 图上的主动操作仍能联动主图
   * （程序化的 OI 范围变化则永远不反向覆盖主图）。
   * 不做 `preventDefault`，官方库的缩放/平移完全不受影响。
   */
  const markUserViewport = useCallback(() => {
    syncHub.userDrivenUntil = performance.now() + USER_DRIVEN_SYNC_MS;
    if (!restorePendingRef.current) return;
    restorePendingRef.current = false;
    const range = candleHandleRef.current?.visibleRange();
    if (!range) return;
    const viewport: ChartViewport = {
      fromMs: range.fromMs,
      toMs: range.toMs,
      followLatest: false,
    };
    writeCachedViewport(viewKeyRef.current, viewport);
    writeSessionMapValue(CHART_VIEWPORT_STORAGE_KEY, viewKeyRef.current, viewport);
  }, [syncHub]);

  /**
   * 恢复落实（本次挂载的核心修复）。
   *
   * 目标：收起→再展开后，「该币/该周期最后用户视窗」必须**原样恢复** ——
   * 不能因为首屏只有一页 60 根就被官方库夹到那一页的范围，更不能让首屏 `fitContent`
   * 产生的范围事件把存储里的用户视窗改写掉。
   *
   * 步骤：
   *  1. 若恢复区间起点早于当前已加载的最早根、且本地还有更早历史 → 继续加载一页（逐页收敛）；
   *  2. 数据域已覆盖恢复区间（或本地已到最早记录）→ 用 `ChartHandle.setViewport`
   *     按**真实 UTC 时间**把用户视窗原样落实回来，随后解除门闸并把目标固化到存储；
   *  3. 用户一旦主动交互，门闸立即解除（`markUserViewport`），本 effect 不再干预。
   */
  useEffect(() => {
    if (!restorePendingRef.current) return;
    if (!restoreTarget) {
      restorePendingRef.current = false;
      return;
    }
    /**
     * 跟随最新：恢复后由官方库的 `scrollToRealTime` 继续跟随，
     * 不再把窗口钉在历史目标上（否则会把实时跟随拉回旧窗口）。
     */
    if (restoreTarget.followLatest) return;
    const page = historyPagesRef.current[selectedTf];
    const step = nextRestoreStep({
      pending: restorePendingRef.current,
      target: restoreTarget,
      earliestLoadedMs: candles.length ? candles[0].timestamp : null,
      hasMore: page ? page.hasMore : null,
      loading: !!page?.loading,
    });
    if (step === 'idle') return;
    if (step === 'load-earlier') {
      // 还没有分页信息（如整页刷新后只剩 sessionStorage 里的视窗）也会走这一步，
      // 否则恢复永远无法落实；`loadHistoryPage` 内部自带请求去重与并发闸。
      void loadHistoryPage(selectedTf, false);
      return;
    }
    const handle = candleHandleRef.current;
    if (!handle) return;
    /**
     * 只**落实**用户视窗，**不解除门闸**。
     * 门闸的唯一解除途径是真实输入事件（`markUserViewport`）——
     * 这样程序化落位产生的任何范围都永远写不进用户视窗缓存。
     */
    handle.setViewport(restoreTarget.fromMs, restoreTarget.toMs);
    // 依赖只列「会真正改变判定结果」的量：loadHistoryPage 由闭包捕获，
    // viewKey 变化即重建闭包，不列它以免每帧重跑本 effect。
  }, [candles, historyPages, restoreTarget, selectedTf, viewKey]);

  /** 悬停时点对应的真实 OI 值（当前单位口径），用于跨图十字线联动；无值返回 null。 */
  const oiValueAt = useCallback((ms: number): number | null => {
    if (oiPoints.length === 0) return null;
    const tolerance = Math.max(2 * 60 * 60 * 1000, TIMEFRAME_MS[selectedTf]);
    let closest: OIPoint | null = null;
    for (const point of oiPoints) {
      if (!Number.isFinite(point.timestamp)) continue;
      if (!closest || Math.abs(point.timestamp - ms) < Math.abs(closest.timestamp - ms)) closest = point;
    }
    if (!closest || Math.abs(closest.timestamp - ms) > tolerance) return null;
    const value = oiUnit === 'usd' ? closest.oiUsd : closest.oiAmount;
    return isNum(value) ? value : null;
  }, [oiPoints, oiUnit, selectedTf]);

  /** 悬停时点对应的真实收盘价，用于跨图十字线联动；无值返回 null。 */
  const candleCloseAt = useCallback((ms: number): number | null => {
    const tolerance = TIMEFRAME_MS[selectedTf] / 2;
    let closest: Candle | null = null;
    for (const candle of renderBars) {
      if (!Number.isFinite(candle.timestamp)) continue;
      if (!closest || Math.abs(candle.timestamp - ms) < Math.abs(closest.timestamp - ms)) closest = candle;
    }
    if (!closest || Math.abs(closest.timestamp - ms) > tolerance) return null;
    return isNum(closest.close) ? closest.close : null;
  }, [renderBars, selectedTf]);

  /**
   * 十字线：官方库回传真实时点 → 更新 OHLC/OI 读数，并把时点同步到另一张图。
   * `crosshairSyncRef` 为真说明本次回调由联动镜像触发，直接忽略以免覆盖状态与回环。
   */
  const handleHover = useCallback((next: ChartHover | null) => {
    if (crosshairSyncRef.current) return;
    setHover(next);
    crosshairSyncRef.current = true;
    try {
      if (!next) {
        candleHandleRef.current?.clearCrosshair();
        oiHandleRef.current?.clearCrosshair();
        return;
      }
      const seconds = toChartSeconds(next.timeMs);
      if (next.source === 'candle') {
        oiHandleRef.current?.crosshairAt(seconds, oiValueAt(next.timeMs));
      } else {
        candleHandleRef.current?.crosshairAt(seconds, candleCloseAt(next.timeMs));
      }
    } finally {
      crosshairSyncRef.current = false;
    }
  }, [oiValueAt, candleCloseAt]);

  const handleCandleReady = useCallback((handle: ChartHandle) => {
    candleHandleRef.current = handle;
  }, []);
  const handleOiReady = useCallback((handle: ChartHandle) => {
    oiHandleRef.current = handle;
  }, []);

  /**
   * 悬停读数：有十字线时用该时点的真实根（缺 OHLC 时如实 '—'）；
   * 无十字线时回落到未收盘层 / 最后一根闭合根（原版行为）。
   */
  const currentHovered: Candle | null = hover
    ? hover.candle ?? null
    : forming ?? candles[candles.length - 1] ?? null;

  /** 悬停 K 线按真实时间找最近 OI 样本；显示其自身事件时间，不按数组下标错配。 */
  const currentHoveredOi = useMemo(() => {
    if (hover?.oiPoint) return hover.oiPoint;
    if (oiPoints.length === 0) return undefined;
    const target = hover?.timeMs ?? currentHovered?.timestamp;
    if (!isNum(target)) return oiPoints[oiPoints.length - 1];
    let closest = oiPoints[0];
    for (const point of oiPoints) {
      if (Math.abs(point.timestamp - target) < Math.abs(closest.timestamp - target)) closest = point;
    }
    return Math.abs(closest.timestamp - target) <= 2 * 60 * 60 * 1000 ? closest : undefined;
  }, [oiPoints, hover?.oiPoint, hover?.timeMs, currentHovered?.timestamp]);

  return (
    <div className="bg-white border border-gray-200 rounded-lg overflow-hidden p-4 space-y-3.5 my-2">
      {/* Title & Timeframe Row */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-gray-200 pb-2.5">
        <div className="flex items-center gap-2 px-2.5 py-1 rounded-md bg-gray-50 border border-gray-200">
          <TokenIcon symbol={token.symbol} base={token.originalSymbol} size="xs" />
          <span className="font-bold text-gray-900 font-mono text-xs">{token.symbol}</span>
          <span className="text-[10px] text-gray-400 hidden sm:inline">({token.fullName})</span>
          <span className="text-gray-300">|</span>
          <span className="text-[11px] text-gray-600 font-sans flex items-center gap-1">
            <span className="text-gray-400">来源:</span>
            <span className="font-semibold text-gray-800 font-mono">{token.exchange || 'Binance'}</span>
          </span>
        </div>

        {/* Timeframe Selector: 5m / 15m / 30m / 1h / 4h / 8h / 12h / 1d */}
        <div className="flex items-center gap-1 bg-gray-50 p-1 rounded-md border border-gray-200 text-xs">
          {timeframes.map((tf) => (
            <button
              key={tf}
              onClick={() => handleSelectTf(tf)}
              className={`px-2.5 py-1 rounded font-mono font-medium transition-colors ${
                selectedTf === tf
                  ? 'bg-emerald-600 text-white font-bold'
                  : 'text-gray-600 hover:text-gray-900 hover:bg-gray-100'
              }`}
            >
              {tf}
            </button>
          ))}
        </div>
      </div>

      {/* Candlestick & Volume Subplot Area（主 K 线区 340px） */}
      <div className="space-y-2">
        {/* Canvas Toolbar & Ticker Stats */}
        <div className="flex flex-wrap items-center justify-between text-xs text-gray-500 gap-2 px-1">
          <div className="flex items-center gap-4 font-mono text-[11px]">
            <span>开: <span className="text-gray-900 font-medium">{fmtRaw(currentHovered?.open)}</span></span>
            <span>高: <span className="text-gray-900 font-medium">{fmtRaw(currentHovered?.high)}</span></span>
            <span>低: <span className="text-gray-900 font-medium">{fmtRaw(currentHovered?.low)}</span></span>
            <span>收: <span className={`font-semibold ${(currentHovered?.close ?? 0) >= (currentHovered?.open ?? 0) ? 'text-emerald-600' : 'text-rose-600'}`}>
              {fmtRaw(currentHovered?.close)}
            </span></span>
            <span>量: <span className="text-gray-900 font-medium">{formatAmountWanYi(currentHovered?.volume)} 币</span></span>
            {currentHovered?.signalTag && (
              <span className="px-1.5 py-0.2 rounded bg-indigo-50 text-indigo-700 border border-indigo-200">
                {currentHovered.signalTag}
              </span>
            )}
          </div>
        </div>

        {/* Candlestick / Volume Chart Container（340px，白底清爽）
            2026-10-15 收敛：原 420px 时展开一整块（K线 420 + OI 140）高达 ~660px，
            把下方行情表挤到只剩 1 行可见。收至 340px 后仍保留完整 K 线主图与
            缩放/平移/悬停交互，展开状态下可多看到约 1.5 行行情。 */}
        <div
          className="relative w-full h-[340px] bg-white rounded-md border border-gray-200 p-1 overflow-hidden"
          title="滚轮缩放、拖动平移；K线、成交量与OI联动。"
          onWheelCapture={markUserViewport}
          onPointerDown={markUserViewport}
          onTouchStart={markUserViewport}
        >
          <CandleVolumeChart
            key={viewKey}
            bars={renderBars}
            timeframe={selectedTf}
            priceLines={chartPriceLines}
            gridTimes={sharedGridTimes}
            viewKey={viewKey}
            initialViewport={initialViewport}
            onViewportChange={handleViewportChange}
            onHover={handleHover}
            onReady={handleCandleReady}
            syncHub={syncHub}
          />
        </div>

        {/* Annotations Note */}
        <div className="px-1 text-[10px] text-gray-400 font-mono">
          <span>区间边界 / 突破 / 独立回踩 / 止损目标标注</span>
        </div>
      </div>

      {/* OI Section: total open interest（副图高度 116px） */}
      <div className="space-y-1.5 pt-1">
        <div className="flex flex-wrap items-center justify-between text-xs gap-2">
          <div className="flex items-center gap-3">
            <span className="font-bold text-gray-800 flex items-center gap-1.5 text-xs">
              <span
                className="w-2.5 h-2.5 rounded-sm"
                style={{ backgroundColor: OI_BAR_COLOR }}
                title="柱色统一：总持仓量是净额（多空恒等），柱高只表达总量。"
              ></span>
              OI总持仓量（柱形）
              <span className="px-1.5 py-0.2 rounded bg-gray-100 text-gray-600 border border-gray-200 font-mono text-[10px]">
                {selectedTf}
              </span>
              {oiSpanLabel && (
                <span
                  className="px-1.5 py-0.2 rounded bg-gray-50 text-gray-500 border border-gray-200 font-mono text-[10px]"
                >
                  {oiSpanLabel}
                </span>
              )}
            </span>
            <div className="flex items-center gap-3 font-mono text-[11px] bg-gray-50 px-2.5 py-0.5 rounded border border-gray-200">
              <span className="text-gray-500">时点: {currentHoveredOi?.time ?? '—'}</span>
              <span className="text-gray-800 font-semibold">
                总OI: {oiUnit === 'usd'
                  ? `${formatWanYi(currentHoveredOi?.oiUsd, '')} USD`
                  : `${formatAmountWanYi(currentHoveredOi?.oiAmount)} ${formatOiQuantityUnit(token.oi.quantityUnit)}`}
              </span>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <div className="flex bg-gray-100 rounded p-0.5 border border-gray-200 text-[10px]">
              <button
                onClick={() => setOiUnit('amount')}
                className={`px-2 py-0.5 rounded ${
                  oiUnit === 'amount' ? 'bg-white text-gray-900 font-bold' : 'text-gray-500'
                }`}
              >
                数量
              </button>
              <button
                onClick={() => setOiUnit('usd')}
                className={`px-2 py-0.5 rounded ${
                  oiUnit === 'usd' ? 'bg-white text-gray-900 font-bold' : 'text-gray-500'
                }`}
              >
                名义额 USD
              </button>
            </div>
          </div>
        </div>

        {/* OI 柱形图容器（116px，白底清爽；2026-10-15 由 140px 收敛） */}
        <div
          className="w-full h-[116px] bg-white rounded-md border border-gray-200 p-1 relative overflow-hidden"
          onWheelCapture={markUserViewport}
          onPointerDown={markUserViewport}
          onTouchStart={markUserViewport}
        >
          <OiBarChart
            key={`${viewKey}:oi`}
            points={oiDisplayPoints}
            unit={oiUnit}
            gridTimes={sharedGridTimes}
            emptyText={
              oiUnit === 'usd'
                ? '该周期没有名义额历史点'
                : '该周期没有总OI数量历史点'
            }
            viewKey={`${viewKey}:oi`}
            initialViewport={null}
            onViewportChange={noopViewportChange}
            onHover={handleHover}
            onReady={handleOiReady}
            syncHub={syncHub}
          />
        </div>
      </div>
    </div>
  );
};

/**
 * memo 化（性能整改 2026-10-07）：
 * 展开图是本页最重的组件（K 线 + 成交量 + OI 柱形）。原先它随列表每 5s
 * 刷新 / 每 10s 新鲜度 tick 一起重渲染；memo 后只有 `token` / `activeSignal` /
 * `publicExtras` / `onClose` 引用变化时才重渲染（`onClose` 已由 TokenTable 用
 * useCallback 固定引用）。
 */
export const InlineExpandedChart = React.memo(InlineExpandedChartInner);
