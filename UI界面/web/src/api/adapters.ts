/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * 真实后端 → 原版 UI 类型的字段/单位/时间映射。
 *
 * 铁律：后端没给的，一律 null / 空数组，绝不补 0、""、默认值或推测值。
 * 唯一的“计算”是从同一真实序列内部做差（OI 环比、24h 涨跌幅单位换算），
 * 且任一端缺失即返回 null。
 */

import type {
  Candle,
  CryptoSignal,
  OIPoint,
  SignalArm,
  SmartMoneySide,
  Timeframe,
  TokenMarketItem,
  TokenVolumeInfo,
} from '../types/crypto';
import type {
  Envelope,
  RawAnnotations,
  RawBar,
  RawCandlesEnvelope,
  RawFundingRow,
  RawMarket,
  RawOiRow,
  RawPublicExtras,
  RawSignal,
  RawSignalArm,
} from './types';
import { unwrapItems } from './client';
import {
  mapBinanceSmartMoney,
  type BinanceSmartMoneyOverview,
  type BinanceSmartMoneyStats,
  type SmartMoneyMapped,
} from './smartMoneyMapping';

// 1m 已于 2026-10-11 彻底删除（库内数据 + 全部口径）。这里不再请求该周期，
// 展开详情一次只拉 8 个周期。
export const TIMEFRAMES: Timeframe[] = ['5m', '15m', '30m', '1h', '4h', '8h', '12h', '1d'];

const TF_SET = new Set<string>(TIMEFRAMES);

export function isTimeframe(v: string | null | undefined): v is Timeframe {
  return !!v && TF_SET.has(v);
}

/**
 * 代币简称：去掉交易所的倍率前缀。
 * 依据原版说明「确认倍率映射后去前缀（如 1000000BOB→BOB），0G、1INCH 等固有数字保留」。
 * 规则：1 后跟 3 个以上 0，或 1M，且后面还有字母 —— 才算倍率前缀。
 *   1000000BOB → BOB / 1000PEPE → PEPE / 1MBABYDOGE → BABYDOGE
 *   0G、2Z、4、1INCH（1 后面不是 0 也不是 M）→ 原样保留
 */
export function stripMultiplierPrefix(base: string): string {
  const m = /^1(?:0{3,}|M)(?=[A-Z0-9])/.exec(base);
  if (!m) return base;
  const rest = base.slice(m[0].length);
  return rest.length > 0 ? rest : base;
}

/** 交易所展示名：保持原版风格（Binance / OKX）。 */
export function toExchangeLabel(exchange: string): string {
  if (exchange === 'binance') return 'Binance';
  if (exchange === 'okx') return 'OKX';
  return exchange;
}

/** 稳定且唯一的行标识：合约身份 = exchange + market_id，不用裸 symbol。 */
export function tokenId(exchange: string, marketId: string): string {
  return `${exchange}:${marketId}`;
}

/** 与 mock 版一致的本地时间显示口径（后端给的是 UTC open_time，按浏览器本地时区呈现）。 */
export function formatCandleTime(iso: string, tf: Timeframe): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  if (tf === '1d') return `${d.getMonth() + 1}-${d.getDate()}`;
  if (tf === '4h' || tf === '8h' || tf === '12h') return `${d.getMonth() + 1}/${d.getDate()} ${hh}:00`;
  return `${hh}:${mm}`;
}

function formatOiTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `${d.getMonth() + 1}-${d.getDate()} ${hh}:${mm}`;
}

function emptyVolumes(): Record<Timeframe, TokenVolumeInfo> {
  const out = {} as Record<Timeframe, TokenVolumeInfo>;
  for (const tf of TIMEFRAMES) {
    out[tf] = {
      volume: null,
      volumeUsd: null,
      isSurge: null,
      ratio24hPercent: null,
      surgeMultiplierVs24hAvg: null,
    };
  }
  return out;
}

export function emptyCandleMap(): Record<Timeframe, Candle[]> {
  const out = {} as Record<Timeframe, Candle[]>;
  for (const tf of TIMEFRAMES) out[tf] = [];
  return out;
}

/**
 * 后端 `/markets` 的 `volume_by_timeframe` → `{ [周期]: 计价成交额(USDT) }`。
 *
 * 口径（与后端逐字一致，不得改写）：各周期取**最近一根已收盘 bar** 的 `quote_volume`。
 * 纪律：
 *   · 只保留**已知周期**的键（后端若新增周期而前端未支持，宁可不显示，也不猜着画）；
 *   · 值为 null / 非有限数 ⇒ **不建这个键**（缺就是缺，不补 0、不借别的周期顶替）；
 *   · 不读 `base_volume`：界面这一列是**成交额(USDT)**，与 24h 列的 `volume_24h_quote` 同口径。
 */
export function volumeByTimeframeFromRaw(
  raw: RawMarket['volume_by_timeframe'],
): Partial<Record<Timeframe, number>> {
  const out: Partial<Record<Timeframe, number>> = {};
  if (!raw || typeof raw !== 'object') return out;
  for (const tf of TIMEFRAMES) {
    const cell = (raw as Record<string, { quote_volume?: unknown } | null>)[tf];
    const v = cell ? cell.quote_volume : null;
    const n = typeof v === 'number' ? v : typeof v === 'string' ? Number(v) : NaN;
    if (Number.isFinite(n)) out[tf] = n;
  }
  return out;
}

/** 未收盘临时层的空态：每个周期均为 null（没有未收盘根，不是“零根”）。 */
export function emptyFormingMap(): Record<Timeframe, Candle | null> {
  const out = {} as Record<Timeframe, Candle | null>;
  for (const tf of TIMEFRAMES) out[tf] = null;
  return out;
}

/**
 * 聪明钱六槽空态。
 * 后端未提供该合约的聪明钱时保持全 `null` —— 不补 0、不用其它指标推算。
 * 用**同一个冻结对象**，让 `mergeMarketRow` 的身份保持判定在同为空态时走引用相等（省重渲染）。
 */
export const EMPTY_SMART_MONEY_SIDE: SmartMoneySide = Object.freeze({
  amountUsd: null,
  ratioPercent: null,
  avgPrice: null,
  change24h: null,
  traders: null,
  whaleTraders: null,
  whaleAvgPrice: null,
});

/** 六槽逐字段等值比较（用于刷新时的身份保持，避免无变化也生成新对象）。 */
export function sameSmartMoneySide(a: SmartMoneySide, b: SmartMoneySide): boolean {
  return (
    a.amountUsd === b.amountUsd &&
    a.ratioPercent === b.ratioPercent &&
    a.avgPrice === b.avgPrice &&
    a.change24h === b.change24h &&
    (a.traders ?? null) === (b.traders ?? null) &&
    (a.whaleTraders ?? null) === (b.whaleTraders ?? null) &&
    (a.whaleAvgPrice ?? null) === (b.whaleAvgPrice ?? null)
  );
}

function toSmartMoneySide(side: SmartMoneyMapped['long']): SmartMoneySide {
  return {
    amountUsd: side.amountUsd,
    ratioPercent: side.ratioPercent,
    avgPrice: side.avgPrice,
    change24h: side.change24h,
    traders: side.traders,
    whaleTraders: side.whaleTraders,
    whaleAvgPrice: side.whaleAvgPrice,
  };
}

/**
 * 后端透传的币安聪明钱原始响应 → 原版六槽（**纯字段映射**）。
 *
 * 数据来源：`/markets` 行内的 `smart_money.overview` / `smart_money.stats`
 * （币安 `/bapi/futures/v1/public/future/smart-money/signal/{overview,details/stats}`
 * 的 `data` 原样透传）。字段名、单位、口径全部来自实测响应，见 `smartMoneyMapping.ts`。
 * 两者都缺省 ⇒ 返回全 null 空态（不是"零"，是"没有"）。
 */
export function adaptSmartMoney(row: Pick<RawMarket, 'smart_money'>): {
  long: SmartMoneySide;
  short: SmartMoneySide;
} {
  const sm = row.smart_money;
  const overview = (sm?.overview ?? null) as BinanceSmartMoneyOverview | null;
  const stats = (sm?.stats ?? null) as BinanceSmartMoneyStats | null;
  if (!overview && !stats) return { long: EMPTY_SMART_MONEY_SIDE, short: EMPTY_SMART_MONEY_SIDE };
  const mapped = mapBinanceSmartMoney(overview, stats);
  return { long: toSmartMoneySide(mapped.long), short: toSmartMoneySide(mapped.short) };
}

/**
 * 真实行情行 → 原版 TokenMarketItem 的“列表字段”。
 * 蜡烛 / OI 序列 / 24h 迷你图属于按需加载，先置空。
 */
export function adaptMarket(row: RawMarket): TokenMarketItem {
  const base = row.base ?? row.market_id;
  const price = row.last_price ?? row.mark_price ?? row.summary?.latest_close ?? null;
  const changeRatio = row.price_change_ratio_24h;
  const smartMoney = adaptSmartMoney(row);
  return {
    id: tokenId(row.exchange, row.market_id),
    symbol: stripMultiplierPrefix(base),
    originalSymbol: base,
    // 后端无“项目全称”字段：回退为交易所原始 base 代号，不编造名称。
    fullName: base,
    exchange: toExchangeLabel(row.exchange),
    price,
    change24h: changeRatio === null || changeRatio === undefined ? null : changeRatio * 100,
    marketCapUsd: row.market_cap_usd ?? null,
    marketCapNote: row.market_cap_note ?? null,
    circulatingSupply: row.circulating_supply ?? null,
    marketCapAsOf: row.market_cap_as_of ?? null,
    // 流通率 = 流通量 / 最大供应量；后端**没有**最大供应量来源 ⇒ 恒 null（不推算）
    circulatingPercent: null,
    volume24hUsd: row.volume_24h_quote ?? null,
    volume24hAmount: row.volume_24h_base ?? null,
    volumes: emptyVolumes(),
    volumeByTimeframe: volumeByTimeframeFromRaw(row.volume_by_timeframe),
    // 币安公开聪明钱接口透传字段 → 六槽；后端未提供时保持空态（不补值）。
    smartMoneyLong: smartMoney.long,
    smartMoneyShort: smartMoney.short,
    oi: {
      totalUsd: row.open_interest_usd ?? row.oi?.oi_usd ?? null,
      totalAmount: row.open_interest_total ?? toNum(row.oi?.open_interest),
      quantityUnit: row.open_interest_unit ?? null,
      amountEventTime: row.oi_time ?? null,
      usdEventTime: row.oi_time ?? null,
      source: row.oi_source ?? row.oi?.source ?? null,
      changePercent24h: null,
      longShortRatio: null,
    },
    fundingRate: row.funding_rate ?? null,
    predictedFundingRate: null,
    sparkline24h: [],
    trendJudgment: null,
    activeSignalCount: 0,
    candles: emptyCandleMap(),
    formingCandles: emptyFormingMap(),
    oiHistory: [],
  };
}

/** 该侧是否**真的有值**（任一槽非 null 即算有）。用于区分「来源给了值」与「来源没有该合约」。 */
export function hasSmartMoneyData(side: SmartMoneySide): boolean {
  return (
    side.amountUsd !== null ||
    side.ratioPercent !== null ||
    side.avgPrice !== null ||
    (side.traders ?? null) !== null ||
    (side.whaleTraders ?? null) !== null ||
    (side.whaleAvgPrice ?? null) !== null
  );
}

/**
 * 两个「周期成交额」映射是否逐键等值（键集合相同且每个值相同）。
 * 用于 `mergeMarketRow` 的身份保持判定：不同 ⇒ 必须更新，否则这一列会停在旧值。
 */
function sameVolumeByTimeframe(
  a: Partial<Record<Timeframe, number>>,
  b: Partial<Record<Timeframe, number>>,
): boolean {
  for (const tf of TIMEFRAMES) {
    if ((a[tf] ?? null) !== (b[tf] ?? null)) return false;
  }
  return true;
}

/**
 * 只覆盖“会随刷新变化”的列表字段，保留已按需加载的蜡烛 / 未收盘层 / OI / 迷你图，避免刷新丢图。
 *
 * 聪明钱特别处理（2026-10-07 实测缺陷）：列表刷新走 `/markets`，该接口当前**不含**
 * 聪明钱原始字段 ⇒ `adaptMarket` 会给出空态；而展开行是按需从币安公开接口取到的**真值**。
 * 若刷新时无条件用列表值覆盖，**每 5 秒就会把刚取到的真值抹成空**（探针实测：展开后
 * 面板恒为「— / 暂无数据」）。因此这里只在列表**确实带了值**时才替换，否则沿用上一轮。
 * 与 OI / 蜡烛的既有约定一致：取不到不视为「已清空」，避免一次抖动清空已有数据。
 */
export function mergeMarketRow(prev: TokenMarketItem, next: TokenMarketItem): TokenMarketItem {
  const nextLong = hasSmartMoneyData(next.smartMoneyLong) ? next.smartMoneyLong : prev.smartMoneyLong;
  const nextShort = hasSmartMoneyData(next.smartMoneyShort) ? next.smartMoneyShort : prev.smartMoneyShort;
  /**
   * 身份保持（性能整改 2026-10-07）：
   * 若本轮刷新要写入的字段与上一轮**完全相同**，直接返回 `prev`。
   * 这样 memo 化的行组件可跨 5s 刷新跳过重渲染——原来每次轮询都对全部
   * 549 个 token 生成新对象，导致整表全量重渲染。
   * 判定必须覆盖下面所有被写入的字段，否则会漏更新。
   */
  const sameOi =
    prev.oi.totalAmount === next.oi.totalAmount &&
    prev.oi.totalUsd === next.oi.totalUsd &&
    prev.oi.quantityUnit === next.oi.quantityUnit &&
    prev.oi.amountEventTime === next.oi.amountEventTime &&
    prev.oi.usdEventTime === next.oi.usdEventTime &&
    prev.oi.source === next.oi.source;
  // 周期成交额：每根 bar 收盘时才会变（最短 5 分钟），逐键等值判定成本极低（≤8 键）。
  const sameTfVolume = sameVolumeByTimeframe(prev.volumeByTimeframe, next.volumeByTimeframe);
  if (
    sameOi &&
    sameTfVolume &&
    // 引用相等（同一对象/共享空态）或**逐字段等值**都算没变化 —— 后者保证
    // 后端开始透传聪明钱后，值没变的行仍能跨刷新保持身份、跳过重渲染。
    (nextLong === prev.smartMoneyLong || sameSmartMoneySide(prev.smartMoneyLong, nextLong)) &&
    (nextShort === prev.smartMoneyShort || sameSmartMoneySide(prev.smartMoneyShort, nextShort)) &&
    prev.price === next.price &&
    prev.change24h === next.change24h &&
    prev.marketCapUsd === next.marketCapUsd &&
    prev.marketCapNote === next.marketCapNote &&
    prev.circulatingSupply === next.circulatingSupply &&
    prev.marketCapAsOf === next.marketCapAsOf &&
    prev.volume24hUsd === next.volume24hUsd &&
    prev.volume24hAmount === next.volume24hAmount &&
    prev.fundingRate === next.fundingRate &&
    prev.exchange === next.exchange &&
    prev.symbol === next.symbol &&
    prev.originalSymbol === next.originalSymbol &&
    prev.fullName === next.fullName
  ) {
    return prev;
  }
  return {
    ...prev,
    price: next.price,
    change24h: next.change24h,
    marketCapUsd: next.marketCapUsd,
    marketCapNote: next.marketCapNote,
    circulatingSupply: next.circulatingSupply,
    marketCapAsOf: next.marketCapAsOf,
    volume24hUsd: next.volume24hUsd,
    volume24hAmount: next.volume24hAmount,
    volumeByTimeframe: next.volumeByTimeframe,
    oi: {
      ...prev.oi,
      totalAmount: next.oi.totalAmount,
      totalUsd: next.oi.totalUsd,
      quantityUnit: next.oi.quantityUnit,
      amountEventTime: next.oi.amountEventTime,
      usdEventTime: next.oi.usdEventTime,
      source: next.oi.source,
    },
    fundingRate: next.fundingRate,
    exchange: next.exchange,
    symbol: next.symbol,
    originalSymbol: next.originalSymbol,
    fullName: next.fullName,
    // 列表透传的聪明钱：只在列表真的带了值时才替换（否则沿用按需取到的真值）
    smartMoneyLong: nextLong,
    smartMoneyShort: nextShort,
  };
}

/**
 * 后端 annotations → 时间 → 标注标签 的索引（用于把 `signalTag` 落到**确认 bar** 上）。
 *
 * 为什么需要（UI03 修复 2026-10-11）：旧实现把 `signalTag` 恒置 undefined，即使
 * 后端给出真实标注也不会画 marker —— 即“后端有证据、前端不消费”。
 * 定位口径：**按 UTC 时刻**（annotations 的 time 与 bar 的 open_time 对齐），
 * 不回填未来确认；同一时刻多条时以「信号确认」优先。
 */
function buildAnnotationIndex(ann: RawAnnotations | null | undefined): Map<number, string> {
  const idx = new Map<number, string>();
  if (!ann) return idx;
  const put = (t: string | null | undefined, tag: string) => {
    if (!t) return;
    const ms = Date.parse(t);
    if (Number.isNaN(ms)) return;
    if (!idx.has(ms)) idx.set(ms, tag);
  };
  for (const m of ann.breakouts ?? []) put(m.time, '突破');
  for (const m of ann.retests ?? []) put(m.time, '回踩');
  for (const m of ann.signals ?? []) {
    if (m.time) {
      const ms = Date.parse(m.time);
      if (!Number.isNaN(ms)) idx.set(ms, '确认'); // 确认标记优先覆盖突破/回踩
    }
  }
  return idx;
}

/** 真实 K 线 → 原版 Candle。annotations 为空时没有 signalTag（后端不生成推测标注）。 */
export function adaptCandles(env: RawCandlesEnvelope, tf: Timeframe): Candle[] {
  const bars = Array.isArray(env?.bars) ? env.bars : [];
  const annIndex = buildAnnotationIndex(env?.annotations);
  const out: Candle[] = [];
  for (const b of bars) {
    if (!b || !b.open_time) continue;
    const ts = Date.parse(b.open_time);
    out.push({
      time: formatCandleTime(b.open_time, tf),
      timestamp: Number.isNaN(ts) ? 0 : ts,
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
      volume: b.base_volume,
      // 后端 bars[] 确有 quote_volume（计价币成交额）字段，原先漏映射 → 现如实取用。
      volumeUsd: b.quote_volume,
      isClosed: b.is_closed !== false,
      // signalTag 来自后端**真实已存标注**；无标注即 undefined（不造）。
      signalTag: Number.isNaN(ts) ? undefined : annIndex.get(ts),
    });
  }
  return out;
}

/**
 * 未收盘临时层：后端把「当前尚未闭合的那根 bar」单独放在 `forming_bar`，
 * 与 `bars`（已闭合历史）严格分离。这里原样映射为一条 `isClosed: false` 的 Candle，
 * **不并入** `candles`（闭合序列），因此不会污染缺口判定、迷你图与成交量口径，
 * 也不会被当成历史闭合记录。
 * `forming_bar` 为 null（例如高周期末尾停在归档日）⇒ 返回 null，保持空态，不合成、不借其他周期替代。
 */
export function adaptFormingBar(env: RawCandlesEnvelope, tf: Timeframe): Candle | null {
  const b = env?.forming_bar;
  if (!b || !b.open_time) return null;
  const ts = Date.parse(b.open_time);
  return {
    time: formatCandleTime(b.open_time, tf),
    timestamp: Number.isNaN(ts) ? 0 : ts,
    open: b.open,
    high: b.high,
    low: b.low,
    close: b.close,
    volume: b.base_volume,
    volumeUsd: b.quote_volume,
    isClosed: false,
    signalTag: undefined,
  };
}

/** 由真实 1h 收盘价序列生成 24h 迷你图；不足 2 个点则留空（原版空态）。 */
export function sparklineFromCandles(candles: Candle[]): number[] {
  const closes = candles
    .filter((c) => typeof c.close === 'number' && Number.isFinite(c.close))
    .map((c) => c.close as number);
  if (closes.length < 2) return [];
  return closes.slice(-24);
}

function toNum(v: string | number | null | undefined): number | null {
  if (v === null || v === undefined) return null;
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

/** 「环比参照」判定的最小间隔下限（旧行为写死的 2 小时）。 */
const OI_GAP_FLOOR_MS = 2 * 60 * 60 * 1000;
/** 局部节奏的滑动窗口长度（相邻间隔条数）。 */
const OI_CADENCE_WINDOW = 12;
/** 间隔超过「局部节奏中位数 × 该倍数」即视为跨缺口，不构成相邻环比。 */
const OI_GAP_FACTOR = 4;

/**
 * 真实 OI 快照序列 → 原版 OIPoint。
 * 后端只有“总量”快照，没有多空拆分，故 longOiUsd / shortOiUsd / longRatio /
 * shortRatio / netOiUsd 全部 null。
 * oiDelta / deltaPercent 由同一条真实序列相邻两点做差得出；缺前值即 null。
 *
 * `hasGap`（=「本柱与上一柱不构成相邻环比」）改用**采样节奏自适应**阈值：
 *   · 旧实现写死 2 小时，前提是「序列只有 snapshot（实测相邻 5~17 分钟）」。
 *   · 现在 `/oi?period=auto` 会一次给到 5m 归档的自适应金字塔
 *     （近 2 天 5m、2–30 天 1h、30–365 天 1d、更早 1w），**同一序列里节奏本身就在变**；
 *     沿用 2 小时会把**每一根日线/周线桶**都误判成「环比未知」，
 *     而日与日之间的环比其实是**已知**的。
 *   · 故阈值取「最近 N 个相邻间隔的中位数 × 4」，并保留 2 小时下限：
 *     5m 段阈值 2h（snapshot 的 5~17 分钟间隔照旧不算缺口）、
 *     1h 段阈值 4h、日线段阈值 4d、周线段阈值 4w。
 *   这只是**判定口径**，不参与任何数值：点数、时间戳、取值一律原样保留。
 *
 * 注：`hasGap` 原先用于 OI 柱形的三态着色（涨 / 跌 / 中性）。柱色现已改为
 * **统一单色**（见 `LightweightChart.tsx` 的 `COLOR.oiBar`），因此它不再影响
 * 任何渲染，只作为序列元信息保留（后端与前端都无多空拆分，柱色不承载语义）。
 */
export function adaptOiHistory(items: RawOiRow[]): OIPoint[] {
  const rows = (items || []).filter((r) => r && r.event_time);
  const out: OIPoint[] = [];
  const dts: number[] = [];
  for (let i = 0; i < rows.length; i += 1) {
    const r = rows[i];
    const ts = Date.parse(r.event_time);
    const amount = toNum(r.open_interest);
    const prev = i > 0 ? toNum(rows[i - 1].open_interest) : null;
    const prevTs = i > 0 ? Date.parse(rows[i - 1].event_time) : NaN;
    const usd = toNum(r.oi_usd);
    const prevUsd = i > 0 ? toNum(rows[i - 1].oi_usd) : null;

    const oiDelta = amount !== null && prev !== null ? amount - prev : null;
    const deltaPercent =
      oiDelta !== null && prev !== null && prev !== 0 ? (oiDelta / prev) * 100 : null;
    const oiDeltaUsd = usd !== null && prevUsd !== null ? usd - prevUsd : null;

    let hasGap = false;
    if (i > 0 && !Number.isNaN(ts) && !Number.isNaN(prevTs)) {
      const dt = ts - prevTs;
      const recent = dts.slice(-OI_CADENCE_WINDOW).sort((a, b) => a - b);
      const median = recent.length ? recent[recent.length >> 1] : dt;
      if (dt > Math.max(OI_GAP_FLOOR_MS, median * OI_GAP_FACTOR)) hasGap = true;
      dts.push(dt);
    }

    out.push({
      time: formatOiTime(r.event_time),
      timestamp: Number.isNaN(ts) ? 0 : ts,
      oiAmount: amount,
      oiUsd: usd,
      oiDelta,
      oiDeltaUsd,
      deltaPercent,
      longOiUsd: null,
      shortOiUsd: null,
      longOiAmount: null,
      shortOiAmount: null,
      longRatio: null,
      shortRatio: null,
      netOiUsd: null,
      hasGap,
    });
  }
  return out;
}

const HOUR_MS = 60 * 60 * 1000;

/**
 * 真实 OI 的「小时桶」。
 *
 * 后端 `/oi` 给的是 `period=snapshot` 的不等间隔快照（实测相邻 5~17 分钟），
 * 因此绝不能把「最后 24 条快照」当成「近 24 小时」——那样同一个整点会出现多次，
 * 既不是逐小时，也不是 24 小时。
 *
 * 这里改为：按真实时间把快照归入整点小时端点，每桶取该小时内时间最晚的一条快照；
 * 某小时内没有任何快照 → 该桶留空（hasData=false，数值全 null），不补 0、不借邻格。
 */
export interface OiHourBucket {
  /** 该小时起点（毫秒） */
  hourStartMs: number;
  /**
   * 该桶实际采用的那条快照的**真实时间**（毫秒）。
   * 必须保留：整点桶只是归并口径，真实跨度要按 snapshotMs 核对，
   * 否则会出现「整点差 ≥ 24h、但实际快照间隔不足 24h」的误判。
   */
  snapshotMs: number | null;
  /** 'HH:00'（本地时区，与原版展示口径一致） */
  timeLabel: string;
  oiAmount: number | null;
  oiUsd: number | null;
  /** 与「紧邻的上一个小时桶」的差；上一小时缺数据 → null（不跨缺口编造变化） */
  deltaAmount: number | null;
  deltaUsd: number | null;
  deltaPercent: number | null;
  hasData: boolean;
}

/** 把真实 OI 快照序列归入整点小时桶（连续区间，缺小时留空）。 */
export function bucketOiByHour(points: OIPoint[]): OiHourBucket[] {
  const byHour = new Map<number, OIPoint>();
  for (const p of points || []) {
    if (!p || !Number.isFinite(p.timestamp) || p.timestamp <= 0) continue;
    const h = Math.floor(p.timestamp / HOUR_MS) * HOUR_MS;
    const prev = byHour.get(h);
    if (!prev || p.timestamp > prev.timestamp) byHour.set(h, p);
  }
  if (byHour.size === 0) return [];

  let latest = -Infinity;
  let earliest = Infinity;
  byHour.forEach((_v, h) => {
    if (h > latest) latest = h;
    if (h < earliest) earliest = h;
  });

  const out: OiHourBucket[] = [];
  for (let h = earliest; h <= latest; h += HOUR_MS) {
    const p = byHour.get(h);
    const d = new Date(h);
    out.push({
      hourStartMs: h,
      snapshotMs: p ? p.timestamp : null,
      timeLabel: Number.isNaN(d.getTime()) ? '—' : `${String(d.getHours()).padStart(2, '0')}:00`,
      oiAmount: p ? p.oiAmount : null,
      oiUsd: p ? p.oiUsd : null,
      deltaAmount: null,
      deltaUsd: null,
      deltaPercent: null,
      hasData: !!p,
    });
  }

  // 变化率只与紧邻的上一个小时比：上一小时无数据 → 该格变化率未知（null），不跨缺口推算
  for (let i = 1; i < out.length; i += 1) {
    const cur = out[i];
    const prev = out[i - 1];
    if (!cur.hasData || !prev.hasData) continue;
    if (cur.oiAmount !== null && prev.oiAmount !== null) {
      cur.deltaAmount = cur.oiAmount - prev.oiAmount;
      if (prev.oiAmount !== 0) cur.deltaPercent = (cur.deltaAmount / prev.oiAmount) * 100;
    }
    if (cur.oiUsd !== null && prev.oiUsd !== null) cur.deltaUsd = cur.oiUsd - prev.oiUsd;
  }
  return out;
}

/** 取最后 n 个连续小时桶（用于 24 格热力图；缺小时的那格仍在，只是留空）。 */
export function lastHourBuckets(buckets: OiHourBucket[], n: number): OiHourBucket[] {
  if (!buckets || buckets.length === 0) return [];
  return buckets.slice(Math.max(0, buckets.length - n));
}

/**
 * OI **显示层周期对齐**：把真实快照按**选定 K 线周期**分桶，每桶只保留**最新一条真实快照**，
 * 并把它标记到桶起点（`alignedTimestamp`，毫秒）。
 *
 * ---------------------------------------------------------------------------
 * 为什么必须对齐（K 线横向拉伸/孤立单根缺陷的根因，2026-10-11 实测）
 * ---------------------------------------------------------------------------
 * 两图（K 线 / OI）必须拥有**完全相同的时间点集合**，才能做到「同一时间点落在同一横坐标」。
 * 但官方库的时间轴是**离散索引**：每个不同的时间点占 1 个索引，索引之间等距铺开，
 * 与真实时间间隔**无关**。于是当两套数据的采样密度差很多时，并集网格会把密集的那一段撑开。
 *
 * 实测 BTCUSDT（币安，1d，`limit=180`）：
 *   · K 线时点 181 个（2026-04-14 → 2026-10-11，1 天 1 根）
 *   · OI 快照 453 个（2026-10-05 04:22 → 2026-10-11 09:00，**约 20 分钟 1 个**）
 *   · 并集网格 631 点；相邻蜡烛在网格中的索引间隔：**174 个间隔 = 1**（左侧密集团），
 *     其余 6 个间隔 = 71 / 68 / 52 / 73 / 90（右侧孤立单根）
 *   · **OI 覆盖的最后 6.2 天占 72.2% 宽度，却只含 6 根蜡烛；前 174 天占 27.8%，却含 175 根**
 * 渲染结果就是用户截图里的「左侧密集团 + 右侧一根一根孤立排开」，而**数据本身一根不缺**。
 *
 * 对齐到 K 线周期后，桶起点与 K 线时点重合 ⇒ 并集网格退化为 K 线网格 ⇒ 蜡烛等距铺满，
 * 两图依旧逐点对齐（共享网格机制不变）。
 *
 * ---------------------------------------------------------------------------
 * 数据纪律
 * ---------------------------------------------------------------------------
 * **不改数据**：数值、样本条数、真实时间戳（`timestamp` / `time`）一律原样保留 ——
 * 读数条仍显示真实快照时间。不聚合求均值、不插值、不给空桶造点；
 * 桶内只取**已存在的真实样本**，只是把它「画在哪个横坐标」对齐到周期边界。
 */
export function alignOiToTimeframe(points: OIPoint[], tfMs: number): OIPoint[] {
  if (!Array.isArray(points) || points.length === 0) return [];
  if (!Number.isFinite(tfMs) || tfMs <= 0) return points;
  const byBucket = new Map<number, OIPoint>();
  for (const p of points) {
    if (!p || !Number.isFinite(p.timestamp) || p.timestamp <= 0) continue;
    const bucket = Math.floor(p.timestamp / tfMs) * tfMs;
    const prev = byBucket.get(bucket);
    if (!prev || p.timestamp > prev.timestamp) byBucket.set(bucket, p);
  }
  return [...byBucket.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([bucket, p]) => ({ ...p, alignedTimestamp: bucket }));
}

export interface OiNet24h {
  percent: number | null;
  deltaAmount: number | null;
  deltaUsd: number | null;
  /** 实际用到的参照小时标签，便于核对口径 */
  refLabel: string | null;
  /** 参照点与最新点的真实小时跨度（应 ≥ 24） */
  spanHours: number;
}

/**
 * 24h 净变：参照点必须按**真实快照时间**至少早 24 小时。
 * 只比整点桶差是不够的 —— 两个桶的真实快照可能分别落在各自小时的边界，
 * 整点差 ≥24h 而实际间隔不足 24h。故这里统一用 `snapshotMs` 判定：
 *  - 参照点取「真实快照时间 ≤ 最新快照时间 − 24h」中**最晚**的一个有数据的桶；
 *  - 找不到 → null（数据不足 24 小时 / 参照小时缺数据）；
 *  - 实际跨度 < 24h → 同样返回 null，不输出 24h 净变。
 * 注意传入的必须是**全量**小时桶，而不是仅用于渲染的那 24 格。
 */
export function oiNet24h(buckets: OiHourBucket[]): OiNet24h | null {
  if (!buckets || buckets.length === 0) return null;
  const last = buckets[buckets.length - 1];
  if (!last.hasData || last.oiAmount === null || last.snapshotMs === null) return null;

  const cutoff = last.snapshotMs - 24 * HOUR_MS;
  let ref: OiHourBucket | null = null;
  for (let i = buckets.length - 2; i >= 0; i -= 1) {
    const b = buckets[i];
    if (b.snapshotMs !== null && b.snapshotMs <= cutoff) {
      ref = b;
      break;
    }
  }
  if (!ref || !ref.hasData || ref.oiAmount === null || ref.oiAmount === 0 || ref.snapshotMs === null) {
    return null;
  }

  // 用真实端点时间核对实际跨度：不足 24h 就不算 24h 净变
  const spanHours = (last.snapshotMs - ref.snapshotMs) / HOUR_MS;
  if (spanHours < 24) return null;

  return {
    percent: Number((((last.oiAmount - ref.oiAmount) / ref.oiAmount) * 100).toFixed(2)),
    deltaAmount: last.oiAmount - ref.oiAmount,
    deltaUsd: last.oiUsd !== null && ref.oiUsd !== null ? last.oiUsd - ref.oiUsd : null,
    refLabel: ref.timeLabel,
    spanHours,
  };
}

/**
 * 信号状态：**与后端契约同一枚举**（UI01 修复 2026-10-11）。
 *
 * 旧实现只认识 valid/invalid/forming —— 后端实际返回的
 * candidate/waiting_retest/confirmed/invalidated/expired/undecided 全部映射为 null，
 * 卡片表达式再把 null 渲染成“失效”，于是**合法的 confirmed 被误标失效**。
 * 现在六态原样透传；未知取值回 null（不猜、不默认成失效）。
 */
const SIGNAL_STATUS_MAP: Record<string, CryptoSignal['status']> = {
  candidate: 'candidate',
  waiting_retest: 'waiting_retest',
  confirmed: 'confirmed',
  invalidated: 'invalidated',
  expired: 'expired',
  undecided: 'undecided',
};

/** 真实信号 → 原版 CryptoSignal。后端未计算的字段一律 null（阶段、阈值、价格、评分均无来源）。 */
export function adaptSignal(row: RawSignal, symbolByMarketId: Map<string, string>): CryptoSignal {
  const base = symbolByMarketId.get(`${row.exchange}:${row.market_id}`) ?? row.market_id;
  const direction = row.direction === 'long' || row.direction === 'short' ? row.direction : 'neutral';
  const cur = row.current_direction === 'long' || row.current_direction === 'short'
    ? row.current_direction : null;
  return {
    id: row.signal_id,
    tokenSymbol: stripMultiplierPrefix(base),
    rawSymbol: row.market_id,
    rawExchange: row.exchange,
    exchange: toExchangeLabel(row.exchange),
    // 后端信号没有“策略阶段”字段 → null，不推测。
    stage: null,
    direction,
    timeframe: isTimeframe(row.timeframe) ? row.timeframe : null,
    // UI05 修复：用**确认时刻**（confirmed_at/event_time）而非 created_at 冒充触发时间。
    triggerTime: row.confirmed_at ?? row.event_time ?? row.created_at,
    status: SIGNAL_STATUS_MAP[row.status ?? ''] ?? null,
    triggerPrice: null,
    currentPrice: null,
    conditionThreshold: null,
    conditionActual: null,
    targetPrice: null,
    stopLossPrice: null,
    validityReason: row.actionable_reason ?? null,
    confidenceScore: row.strength,
    confirmedAt: row.confirmed_at ?? null,
    eventTime: row.event_time ?? null,
    strategyKey: row.strategy_key ?? null,
    strategyVersion: row.strategy_version ?? null,
    configHash: row.config_hash ?? null,
    dataVersion: row.data_version ?? null,
    sourceId: row.source_id ?? null,
    sourceMasks: row.source_masks ?? null,
    executionLabel: row.execution_label ?? null,
    actionable: row.actionable ?? null,
    actionableReason: row.actionable_reason ?? null,
    currentDirection: cur,
    // ── 实时信号层扩展（2026-10-13）：原样透传，不做任何推断 ──
    origin: row.origin === 'live' || row.origin === 'historical_research' ? row.origin : null,
    displayState: row.display_state ?? null,
    executableState: row.executable_state ?? null,
    structuralKind: row.structural_kind ?? null,
    barOpenTime: row.bar_open_time ?? null,
    barCloseTime: row.bar_close_time ?? null,
    detectionLow: row.detection_low ?? null,
    detectionAtr: row.detection_atr ?? null,
    admission: row.admission ?? null,
    arm: row.arm ?? null,
    role: row.role ?? null,
    // ── 同一买点多臂合卡（2026-10-14 09 契约 §4）────────────────────────────
    // 后端已在展示层把同买点的多臂行合为一张卡并给出 `arms[]`/`arm_count`/`arms_hit`；
    // 这里**原样透传**，前端不二次合并、不重算臂数（重算会与后端口径分叉）。
    arms: adaptSignalArms(row.arms),
    armCount: row.arm_count ?? (row.arms ? row.arms.length : null),
    armsHit: row.arms_hit ?? null,
  };
}

/** 合卡卡面上各臂的身份与去向：字段原样透传（缺失 ⇒ null，不推断、不补默认）。 */
function adaptSignalArms(arms: RawSignalArm[] | null | undefined): SignalArm[] | null {
  if (!arms || arms.length === 0) return null;
  return arms.map((a) => ({
    arm: a.arm ?? null,
    signalId: a.signal_id ?? null,
    status: SIGNAL_STATUS_MAP[a.status ?? ''] ?? null,
    executableState: a.executable_state ?? null,
    actionable: a.actionable ?? null,
    actionableReason: a.actionable_reason ?? null,
    structuralKind: a.structural_kind ?? null,
    sourceMasks: a.source_masks ?? null,
    executionLabel: a.execution_label ?? null,
  }));
}

export function adaptSignals(env: Envelope<RawSignal> | null, symbolByMarketId: Map<string, string>): CryptoSignal[] {
  return unwrapItems(env).map((r) => adaptSignal(r, symbolByMarketId));
}

/**
 * 从真实 K 线序列取最近一根已收盘 bar 的成交量，用于「成交量动态列」。
 *
 * 量纲必须一致，分子分母同口径：
 *  - bar 的 `base_volume`（标的币） 只能对 `volume_24h_base`（同为标的币）；
 *  - bar 的 `quote_volume`（USDT） 只能对 `volume_24h_quote`（同为 USDT）。
 *
 * 后端 bars[] **确实提供** `quote_volume`，且 `/markets` 的 `volume_24h_quote` 覆盖 526/549，
 * 而 `volume_24h_base` 仅 23/549。因此这里用**美元口径**这一对（覆盖率高得多），
 * 分母缺失时返回 null，不做任何近似换算。
 *
 * 覆盖率须知：原版界面当前没有任何展示位消费 `volumes`，故此处属数据层口径修正，
 * 不影响可见界面。
 */
export function volumesFromCandles(
  candlesByTf: Partial<Record<Timeframe, Candle[]>>,
  volumes: Record<Timeframe, TokenVolumeInfo>,
  vol24hQuoteUsd: number | null,
): Record<Timeframe, TokenVolumeInfo> {
  const out: Record<Timeframe, TokenVolumeInfo> = { ...volumes };
  for (const tf of TIMEFRAMES) {
    const list = candlesByTf[tf];
    if (!list || list.length === 0) continue;
    const closed = [...list].reverse().find((c) => c.isClosed) ?? list[list.length - 1];
    if (!closed || closed.volume === null) continue;
    out[tf] = {
      volume: closed.volume,
      volumeUsd: closed.volumeUsd,
      isSurge: null,
      ratio24hPercent:
        vol24hQuoteUsd !== null &&
        Number.isFinite(vol24hQuoteUsd) &&
        vol24hQuoteUsd !== 0 &&
        closed.volumeUsd !== null
          ? (closed.volumeUsd / vol24hQuoteUsd) * 100 // 分子分母同为计价币(USDT)口径
          : null,
      surgeMultiplierVs24hAvg: null,
    };
  }
  return out;
}

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

export interface CandleGapCheck {
  /** 已加载 bar 数 */
  bars: number;
  /** 相邻 bar 间隔超过一个周期（或时间倒挂）的位置数 */
  gaps: number;
  /**
   * 单处缺口**最多缺多少根**（0 表示无缺口）。
   * 只报「缺口 N 处」会掩盖量级：实测 RLC 5m 在 60 根窗口内只有 1 处缺口，
   * 但那处缺了 384 根（`2026-10-04T23:55Z → 2026-10-06T08:00Z`），必须一并如实标出。
   */
  maxMissing: number;
  /** 数据不足以判断缺口时为 null（不写「无缺口」） */
  ok: boolean | null;
}

/**
 * 用真实 bar 的 open_time 检查时间连续性，供「数据源 / 缺口」展示位使用。
 * 只有拿到 ≥2 根 bar 才能下结论；否则返回 ok=null（未知），不冒充「无缺口」。
 */
export function checkCandleGaps(candles: Candle[], tf: Timeframe): CandleGapCheck {
  const step = TF_MS[tf];
  const ts = (candles || [])
    .map((c) => c.timestamp)
    .filter((t) => Number.isFinite(t) && t > 0)
    .sort((a, b) => a - b);
  if (ts.length < 2 || !step) return { bars: ts.length, gaps: 0, maxMissing: 0, ok: null };
  let gaps = 0;
  let maxMissing = 0;
  for (let i = 1; i < ts.length; i += 1) {
    const delta = ts[i] - ts[i - 1];
    if (delta > step) {
      gaps += 1;
      maxMissing = Math.max(maxMissing, Math.round(delta / step) - 1);
    }
  }
  return { bars: ts.length, gaps, maxMissing, ok: gaps === 0 };
}

/**
 * 「最后一根已闭合 bar」与「未收盘临时层」之间的**真实时间缺口（缺多少根）**。
 *
 * 为什么必须单独算：后端高周期的归档可能仍停在归档日（实测 BTC 30m 闭合末根
 * `2026-10-04T23:30:00Z`），而未收盘层来自当前 WS（`2026-10-06T08:00:00Z`）。
 * 两者在图上**挨着绘制**会造成「数据连续」的错觉 —— 必须把这个真实缺口如实标出。
 *
 * 返回：`null` = 不可判定（缺时点 / 无未收盘层）；`0` = 未收盘层紧接闭合末根（接续）；
 * `>0` = 中间缺了这么多根。
 */
export function tailGapBars(lastClosedTs: number, formingTs: number, tf: Timeframe): number | null {
  const step = TF_MS[tf];
  if (
    !step ||
    !Number.isFinite(lastClosedTs) ||
    !Number.isFinite(formingTs) ||
    lastClosedTs <= 0 ||
    formingTs <= 0
  ) {
    return null;
  }
  const delta = formingTs - lastClosedTs;
  if (delta <= 0) return null;
  return Math.max(0, Math.round(delta / step) - 1);
}

export type { RawBar, RawFundingRow };

/**
 * 币安公开扩展字段 → **既有原版字段**的填充值（只做数值化，不改语义、不补默认）。
 *
 * 映射（一一对应，均可追溯）：
 *  - `oi.longShortRatio` ← `long_short.top_position_long_short_ratio.value`
 *    = 币安 `/futures/data/topLongShortPositionRatio`（**大户持仓量多空比**，仅显示在展开详情的既有相关指标位）。
 *  - `oiAmount` ← `open_interest.amount` = `/futures/data/openInterestHist.sumOpenInterest`
 *  - `oiUsd` ← `open_interest.usd` = 同一行的 `sumOpenInterestValue`（**总持仓名义额 USD**）。
 *    数量单位取市场行自身的 `open_interest_unit`，前端不将“张”与标的币互换。
 *
 * **不**映射：`smart_money`（币安公开 API 无金额/均价/身份）、`orderbook`（「失衡」口径未定）。
 */
export interface PublicExtrasFill {
  /** 持仓量多空比（原始指标，非「聪明钱」） */
  longShortRatio: number | null;
  /** 同一 openInterestHist 行的总持仓数量 */
  oiAmount: number | null;
  /** 同一 openInterestHist 行的总持仓名义额 USD */
  oiUsd: number | null;
}

export function adaptPublicExtras(env: RawPublicExtras | null | undefined): PublicExtrasFill | null {
  if (!env || env.available !== true) return null;
  const pos = env.long_short?.top_position_long_short_ratio;
  const oi = env.open_interest;
  return {
    longShortRatio: pos?.available ? toNum(pos.value) : null,
    oiAmount: oi?.available ? toNum(oi.amount) : null,
    oiUsd: oi?.available ? toNum(oi.usd) : null,
  };
}
