/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useLayoutEffect, useRef, useCallback, useMemo } from 'react';
import { TokenMarketItem, CryptoSignal, SmartMoneySide } from '../types/crypto';
import {
  PUBLIC_EXTRAS_OI_STALE_AFTER_S,
  PUBLIC_EXTRAS_SNAPSHOT_STALE_AFTER_S,
} from '../api/useMarketData';
import type { PublicExtrasView } from '../api/useMarketData';
import { InlineExpandedChart } from './InlineExpandedChart';
import { TokenIcon } from './TokenIcon';
import { formatWanYi, formatAmountWanYi, formatOiQuantityUnit, formatWanU } from '../utils/formatters';
import {
  Search,
  Filter,
  ArrowUpDown,
  ChevronRight,
  ChevronDown,
  RefreshCw,
  Zap,
  Sparkles,
  Info
} from 'lucide-react';

interface MarketSourceDetails {
  symbol: string;
  smartMoneyLong: string;
  smartMoneyShort: string;
  oi: string;
}

interface TokenTableProps {
  tokens: TokenMarketItem[];
  expandedTokenId: string | null;
  onToggleExpand: (tokenId: string) => void;
  activeSignal?: CryptoSignal;
  /**
   * 上报「当前视口内可见的行 id」。仅用于让数据层对**可见行**做有界取数
   * （迷你图 / 费率周期证据），不对全池扫明细；不产生任何界面控件。
   */
  onVisibleTokenIdsChange?: (ids: string[]) => void;
  /** 当前标的 public-extras 本机快照的原始口径、来源、指标时点与新鲜度 */
  publicExtras?: Record<string, PublicExtrasView>;
}

/**
 * 第 7 列「资金费率」：**按交易所实际费率显示**。
 *
 * 2026-10-12 口径调整（用户指令「资金费率按实际算」）：取消此前「仅当结算周期核对为
 * 8h 才输出、其余一律空态」的闸门，直接展示后端 `/markets` 行内 `funding_rate`
 * 的**真实值**（交易所对**该合约实际结算周期**给出的当期费率），不再把它当成 8h 口径
 * 归一化或换算；`funding_rate` 缺失才显示空态 '—'，不补 0、不推算。
 *
 * 说明：结算周期核对（`fundingPeriodHours`）仍由数据层计算，仅服务于顶部总览的
 * 8h 均值聚合，不再参与本列显示，故本组件不再接收该 prop。
 */

/** 真实后端未提供的字段为 null；此处只做空态呈现，不补 0、不补默认值。 */
const isNum = (v: number | null | undefined): v is number =>
  v !== null && v !== undefined && Number.isFinite(v);

/**
 * 价格显示：真实池价格跨 1.25e-05 ~ 85340，固定小数位会把小价四舍五入成 0，
 * 故按量级自适应位数（不改变列宽与样式）。
 */
const fmtPrice = (v: number | null | undefined): string => {
  if (!isNum(v)) return '—';
  const abs = Math.abs(v);
  if (abs === 0) return '0';
  if (abs >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
  if (abs >= 1) return v.toLocaleString(undefined, { maximumFractionDigits: 4 });
  if (abs >= 0.01) return v.toLocaleString(undefined, { maximumFractionDigits: 6 });
  return Number(v.toPrecision(4)).toString();
};

/** 原版风格：数值原样输出（均价等）。 */
const fmtRaw = (v: number | null | undefined): string => (isNum(v) ? String(v) : '—');

const fmtSignedPercent = (v: number | null | undefined, digits = 2): string =>
  isNum(v) ? `${v >= 0 ? '+' : ''}${v.toFixed(digits)}%` : '—';

const fmtPercent = (v: number | null | undefined, digits = 2): string =>
  isNum(v) ? `${v.toFixed(digits)}%` : '—';

type IndicatorState = 'fresh' | 'stale' | 'missing' | 'error' | 'unknown' | 'pending';

const publicExtrasStatusLabel = (status: IndicatorState): string => {
  switch (status) {
    case 'fresh': return '时效内';
    case 'stale': return '陈旧';
    case 'missing': return '未覆盖';
    case 'error': return '读取失败';
    case 'unknown': return '时间未知';
    default: return '等待本机快照';
  }
};

const publicExtrasStatusClass = (status: IndicatorState): string => {
  if (status === 'fresh') return 'text-emerald-400';
  if (status === 'stale') return 'text-amber-400';
  if (status === 'error') return 'text-rose-400';
  return 'text-neutral-500';
};

const ageSecondsFrom = (value: string | null | undefined, nowMs: number): number | null => {
  if (!value) return null;
  const ts = Date.parse(value);
  return Number.isFinite(ts) ? Math.max(0, (nowMs - ts) / 1000) : null;
};

const fmtAgeShort = (seconds: number | null): string => {
  if (seconds === null) return '年龄未知';
  if (seconds < 60) return `${Math.floor(seconds)}秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}分`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}小时`;
  return `${(seconds / 86400).toFixed(1)}天`;
};

const snapshotStateAt = (extra: PublicExtrasView | undefined, age: number | null): IndicatorState => {
  if (!extra) return 'pending';
  if (extra.status === 'missing' || extra.status === 'error' || extra.status === 'unknown') return extra.status;
  if (extra.status === 'stale') return 'stale';
  if (age === null) return 'unknown';
  return age > PUBLIC_EXTRAS_SNAPSHOT_STALE_AFTER_S ? 'stale' : 'fresh';
};

const metricStateAt = (
  extra: PublicExtrasView | undefined,
  available: boolean,
  eventAge: number | null,
  staleAfterSeconds: number,
): IndicatorState => {
  if (!extra) return 'pending';
  if (extra.status === 'missing' || extra.status === 'error' || extra.status === 'unknown') return extra.status;
  if (!available) return 'missing';
  if (eventAge === null) return 'unknown';
  return eventAge > staleAfterSeconds ? 'stale' : 'fresh';
};

const rawMetricStateAt = (available: boolean, eventAge: number | null, staleAfterSeconds: number): IndicatorState => {
  if (!available) return 'missing';
  if (eventAge === null) return 'unknown';
  return eventAge > staleAfterSeconds ? 'stale' : 'fresh';
};

/**
 * 聪明钱六槽（多/空 × 持仓额 / 巨鲸占比 / 均价）的**来源、口径与当前状态**。
 *
 * 2026-10-07 更正：此前这里写「币安公开 API 结构上没有这些字段」——
 * 该判断**只对 `/fapi/v1/futures/data/*` 那几个聚合比率端点成立**。
 * 币安另有**公开无鉴权**聪明钱接口（路径含 `/public/`，不带 cookie/Authorization 即 200，
 * `timeRange` 必填）：
 *   GET /bapi/futures/v1/public/future/smart-money/signal/overview?symbol={S}&timeRange=ALL
 *   GET /bapi/futures/v1/public/future/smart-money/signal/details/stats?symbol={S}&timeRange=ALL
 * 页面 https://www.binance.bh/zh-CN/smart-money/signal/{SYMBOL} 即渲染这些数据，自述每 60 秒更新。
 * 实测证据见 docs/验收证据/币安聪明钱真实响应_2026-10-07.json。
 *
 * 六槽取值（全部为来源真实字段，**无任何推算**）：
 *   持仓额   ← stats.longPositions / shortPositions（**已是 USDT 金额**，不是数量）
 *   巨鲸占比 ← stats.longWhalePositions ÷ stats.longPositions（同一响应内分子分母）
 *   均价     ← overview.longTradersAvgEntryPrice / shortTradersAvgEntryPrice
 * 来源另给（本行悬浮说明里列出）：交易员人数、巨鲸人数、巨鲸均价、总持仓/总人数/名义多空比。
 *
 * 当前状态：列表列显示**列表接口透传**的聪明钱；后端尚未采集到的行显示「暂无数据」，
 * 展开该行时会按需直接取该合约（取到即填）。币安接口只覆盖币安 USD-M 合约 ⇒
 * OKX 行没有对应来源，保持空态。
 * 纪律：不用 OI、VWAP、人数比、taker 量推算任何一槽；缺值保留空，不填 0。
 */
const SMART_MONEY_MISSING_TITLE = [
  '来源：币安公开无鉴权聪明钱接口（路径含 /public/，无 cookie/Authorization 即 200，timeRange 必填）。',
  '  · GET /bapi/futures/v1/public/future/smart-money/signal/overview?symbol={S}&timeRange=ALL',
  '  · GET /bapi/futures/v1/public/future/smart-money/signal/details/stats?symbol={S}&timeRange=ALL',
  '  页面 https://www.binance.bh/zh-CN/smart-money/signal/{SYMBOL} 渲染同一组数据，自述每 60 秒更新。',
  '六槽口径（全部真实字段，无推算）：',
  '  持仓额 = stats.longPositions / shortPositions（已是 USDT 金额）；',
  '  巨鲸占比 = stats.longWhalePositions ÷ stats.longPositions（同源同一响应）；',
  '  均价 = overview.longTradersAvgEntryPrice / shortTradersAvgEntryPrice。',
  '来源另提供：交易员人数(longTraders/shortTraders)、巨鲸人数(longWhales/shortWhales)、',
  '  巨鲸均价(longWhalesAvgEntryPrice/shortWhalesAvgEntryPrice)、总持仓 totalPositions、',
  '  交易者总数 totalTraders、名义多空比 longShortRatio。',
  '新鲜度（2026-10-07 实测更正）：overview.updateTime **不随数据推进**——间隔 73s 两次采样，',
  '  业务字段全变而 updateTime 恒为 1791353989563（age ≈ 9.6h）。故本表**不用 updateTime 判新鲜**；',
  '  真实新鲜度字段是 details/list[].lastTradeTime（同刻实测 age ≈ 59s，与页面自述 60 秒更新吻合）。',
  '  · GET /bapi/futures/v1/public/future/smart-money/signal/details/list?symbol={S}&timeRange=ALL',
  '当前状态：列表列显示列表接口透传值；未采集到的行显示「暂无数据」，',
  '  展开该行时会按需直接取该合约（取到即填）。币安接口只覆盖币安 USD-M 合约，',
  '  故 OKX 行没有对应来源，保持空态。',
  '纪律：不用 OI / VWAP / 人数比 / taker 量推算任何一槽；缺值保留空，不填 0。',
].join('\n');

/** 单侧聪明钱悬浮说明：静态来源说明 + **该行真实值**（无值时不追加）。 */
function smartMoneySideTitle(side: SmartMoneySide, label: string): string {
  if (!isNum(side.amountUsd) && !isNum(side.ratioPercent) && !isNum(side.avgPrice)) {
    return SMART_MONEY_MISSING_TITLE;
  }
  const parts = [
    `仓位 ${isNum(side.amountUsd) ? `${formatWanYi(side.amountUsd, '')} USDT` : '暂无'}`,
    `巨鲸占比 ${isNum(side.ratioPercent) ? `${side.ratioPercent.toFixed(1)}%` : '暂无'}`,
    `均价 ${isNum(side.avgPrice) ? `${fmtRaw(side.avgPrice)} USDT` : '暂无'}`,
    `交易员 ${isNum(side.traders) ? side.traders : '暂无'} 人`,
    `其中巨鲸 ${isNum(side.whaleTraders) ? side.whaleTraders : '暂无'} 人`,
    `巨鲸均价 ${isNum(side.whaleAvgPrice) ? `${fmtRaw(side.whaleAvgPrice)} USDT` : '暂无'}`,
  ];
  return `${SMART_MONEY_MISSING_TITLE}\n\n本行${label}：${parts.join(' · ')}`;
}

/** 无来源时的占位文案（不是 0，也不是"永久静默空白"）。 */
const SMART_MONEY_EMPTY_TEXT = '暂无数据';

/* ------------------------------------------------------------------ *
 * 列表渲染性能整改（2026-10-07）
 *
 * 问题：tbody 原先对 `filteredTokens` 全量 `map`，549 行 + 549 个迷你图 SVG
 * （实测总 DOM 22,761 元素 / 1,135 个 SVG）在**同一帧**全部挂载；且全工程无 memo，
 * 每 5s 列表刷新与每 10s 新鲜度 tick 都会让 549 行全量重渲染、重建每行约 20 行的
 * 悬浮说明字符串。实测代价：清空搜索绘制 362.8ms；滚动平均帧 27ms / 最长 800.5ms；
 * 12s 窗口长任务 13 条、总阻塞 1287ms。
 *
 * 整改（只改渲染方式，不改数据与语义）：
 *  1. **视口内渲染**：只渲染滚动窗口内的行 + 上下缓冲，其余用占位行补齐总高，
 *     保证滚动条长度/位置与全量渲染一致；币池、排序、筛选、搜索仍对全池生效。
 *  2. **展开行高度**：实测行高 + ResizeObserver 量测展开块高度，代入占位换算；
 *     展开态由 `expandedTokenId` 保持，滚出窗口即卸载重图表、滚回即恢复。
 *  3. **行级 memo**：`TokenRow` / `Sparkline` 用 `React.memo`，回调引用稳定，
 *     未变化的行跨刷新跳过重渲染。
 *  4. **可见行上报**：由「549 个 IntersectionObserver」改为按滚动位置直接计算
 *     可见 id（等价语义、成本 O(可见行)）。
 * ------------------------------------------------------------------ */
const DEFAULT_ROW_H = 49;
/** 渲染缓冲（像素）：上下各多渲染约这么多高度，避免快速滚动出现空白。 */
const OVERSCAN_PX = 600;
const INITIAL_WINDOW_ROWS = 40;
const DEFAULT_EXPANDED_H = 660;
const VISIBLE_MARGIN_PX = 300;

/**
 * 由滚动位置推出应渲染的行区间。
 * `expandedH` 是展开块（K线图那一行）的高度：当展开行位于视口**上方**时，
 * 需要把它占的高度从 scrollTop 里扣掉，才能得到「按行计」的等效位置。
 * 缓冲按**像素**换算成行数——行高在不同窗口宽度下差异很大（窄窗口下
 * 「聪明钱」列换行会让行高从 ~49px 涨到 ~284px），按行数缓冲会过度渲染。
 */
function computeRange(
  scrollTop: number,
  viewportH: number,
  count: number,
  expandedIndex: number,
  rowH: number,
  expandedH: number,
  overscanPx: number,
): { start: number; end: number } {
  if (count <= 0) return { start: 0, end: -1 };
  const overscan = Math.max(1, Math.ceil(overscanPx / rowH));
  let eff = scrollTop;
  if (expandedIndex >= 0 && scrollTop > expandedIndex * rowH) {
    eff = Math.max(0, scrollTop - expandedH);
  }
  const firstVisible = Math.min(count - 1, Math.max(0, Math.floor(eff / rowH)));
  const visibleCount = Math.ceil(viewportH / rowH) + 1;
  const start = Math.max(0, firstVisible - overscan);
  const end = Math.min(count - 1, firstVisible + visibleCount + overscan);
  return { start, end };
}

/** 迷你图：独立 memo，避免行重渲染时重算点串。DOM 结构与原版一致。 */
const Sparkline = React.memo(function Sparkline({
  values,
  positive,
}: {
  values: number[] | null | undefined;
  positive: boolean;
}) {
  return (
    <div className="w-24 h-7 mx-auto">
      <svg className="w-full h-full" viewBox="0 0 100 30">
        {(() => {
          // 真实 1h 序列不足 2 点（无该周期数据）→ 保持原版空态，不画线、不补点
          if (!values || values.length < 2) return null;
          const min = Math.min(...values);
          const max = Math.max(...values);
          const range = max - min || 1;
          const pts = values
            .map((val, idx) => {
              const x = (idx / (values.length - 1)) * 96 + 2;
              const y = 28 - ((val - min) / range) * 24;
              return `${x},${y}`;
            })
            .join(' ');
          return (
            <polyline
              points={pts}
              fill="none"
              stroke={positive ? '#0b8a5e' : '#d92d48'}
              strokeWidth="2"
              strokeLinecap="round"
            />
          );
        })()}
      </svg>
    </div>
  );
});

interface TokenRowProps {
  token: TokenMarketItem;
  isExpanded: boolean;
  publicExtra?: PublicExtrasView;
  freshnessNowMs: number;
  onToggleExpand: (tokenId: string) => void;
  onOpenSourceDetails: (details: MarketSourceDetails) => void;
}

/**
 * 单行渲染。**memo 化**：只有 token 身份/展开态/快照/新鲜度变化时才重渲染。
 * 悬浮说明（oiTitle）在本组件内构建，因此未变化行不会重复拼串。
 */
const TokenRow = React.memo(function TokenRow({
  token,
  isExpanded,
  publicExtra,
  freshnessNowMs,
  onToggleExpand,
  onOpenSourceDetails,
}: TokenRowProps) {
  const handleToggle = useCallback(() => onToggleExpand(token.id), [onToggleExpand, token.id]);

  const snapshotAgeSeconds = ageSecondsFrom(publicExtra?.snapshotTime, freshnessNowMs);
  const snapshotState = snapshotStateAt(publicExtra, snapshotAgeSeconds);
  /**
   * 快照与列表行的「指标时点」比较 —— 决定**谁更权威**。
   *
   * 缺陷背景（本轮修正）：旧判定 `publicOiPair` 只看「有值 + 数量/名义额同一行时点」，
   * **不看年龄**。实测 0GUSDT：`/markets` 行 `oi_time=12:18:57Z`（较新，采集器轮询），
   * public-extras 快照 `amount/usd @12:05:00Z`（较旧，5m 边界采样，快照 age 1420s > 900s）。
   * 旧逻辑会让**较旧的快照**覆盖**较新的列表值**，属陈旧优先。
   *
   * 现规则：
   *   · **数量**取「较新的一侧」——快照成对（同一 openInterestHist 行）且其指标时点
   *     **不早于**列表行 OI 时点时，才用快照数量；否则用列表数量。
   *   · **名义额**列表行通常没有（币安 526 行 `open_interest_usd` 全为 null），
   *     故只能来自 public-extras 快照；它**单独**标注自身指标时点与陈旧状态，
   *     不与数量拼接、不换算、不倒填。
   */
  const listOiTime = token.oi.amountEventTime ?? token.oi.usdEventTime ?? null;
  const listOiMs = listOiTime ? Date.parse(listOiTime) : Number.NaN;
  const pubOiMs = publicExtra?.oiAmountEventTime
    ? Date.parse(publicExtra.oiAmountEventTime)
    : Number.NaN;
  const publicPairSameRow =
    publicExtra?.oiAvailable === true &&
    isNum(publicExtra.oiAmount) &&
    isNum(publicExtra.oiUsd) &&
    !!publicExtra.oiAmountEventTime &&
    publicExtra.oiAmountEventTime === publicExtra.oiUsdEventTime;
  const snapshotNotOlderThanList =
    Number.isFinite(pubOiMs) && (!Number.isFinite(listOiMs) || pubOiMs >= listOiMs);
  const publicOiPair = publicPairSameRow && snapshotNotOlderThanList;
  /** 快照成对但**早于**列表行 → 陈旧优先已阻止，需在悬浮说明里如实写出。 */
  const snapshotOlderThanList = publicPairSameRow && !snapshotNotOlderThanList;
  const oiAmount = publicOiPair ? publicExtra?.oiAmount ?? null : token.oi.totalAmount ?? null;
  const oiUnit = publicOiPair
    ? publicExtra?.oiUnit ?? token.oi.quantityUnit ?? null
    : token.oi.quantityUnit ?? null;
  const oiUnitLabel = formatOiQuantityUnit(oiUnit);
  const oiAmountTime = publicOiPair
    ? publicExtra?.oiAmountEventTime ?? null
    : token.oi.amountEventTime ?? null;
  /**
   * 金额（本列**主显示**）：只放真实金额，÷10000 → 「万U」。
   *
   * 来源判定必须显式：数据层在快照「成对且较新」时会把快照金额并入
   * `token.oi.totalUsd`，此时若仍按「列表行金额」标注，会把快照的指标时点
   * 错标成列表行的 oi_time。故这里用「快照金额可用且与 token 值同值」判定
   * 金额来自快照，并据此取**快照自己的**指标时点与状态。
   */
  const snapshotUsdUsable = publicPairSameRow;
  const snapshotUsd = snapshotUsdUsable ? publicExtra?.oiUsd ?? null : null;
  const listUsdRaw = isNum(token.oi.totalUsd) ? token.oi.totalUsd : null;
  const usdFromSnapshot =
    snapshotUsd !== null && (listUsdRaw === null || listUsdRaw === snapshotUsd);
  const oiUsd = usdFromSnapshot ? snapshotUsd : listUsdRaw;
  const oiUsdTime = usdFromSnapshot
    ? publicExtra?.oiUsdEventTime ?? null
    : listUsdRaw !== null
      ? token.oi.usdEventTime ?? token.oi.amountEventTime ?? null
      : null;
  const oiAmountAge = ageSecondsFrom(oiAmountTime, freshnessNowMs);
  const oiUsdAge = ageSecondsFrom(oiUsdTime, freshnessNowMs);
  const oiAmountState = publicOiPair
    ? metricStateAt(publicExtra, isNum(oiAmount), oiAmountAge, PUBLIC_EXTRAS_OI_STALE_AFTER_S)
    : rawMetricStateAt(isNum(oiAmount), oiAmountAge, PUBLIC_EXTRAS_OI_STALE_AFTER_S);
  const oiUsdState = usdFromSnapshot
    ? metricStateAt(publicExtra, isNum(oiUsd), oiUsdAge, PUBLIC_EXTRAS_OI_STALE_AFTER_S)
    : rawMetricStateAt(isNum(oiUsd), oiUsdAge, PUBLIC_EXTRAS_OI_STALE_AFTER_S);
  const oiSource = publicOiPair
    ? publicExtra?.oiSource ?? '币安 public-extras 快照'
    : usdFromSnapshot
      ? `${token.oi.source ?? '列表 OI 字段'}（数量）+ ${publicExtra?.oiSource ?? '币安 public-extras 快照'}（金额）`
      : token.oi.source ?? '列表 OI 字段';
  const usdBasisText = usdFromSnapshot
    ? '币安 /futures/data/openInterestHist 的 sumOpenInterestValue（合约计价币 USDT 名义额）'
    : listUsdRaw !== null
      ? token.exchange === 'OKX'
        ? 'OKX /api/v5/public/open-interest 的 oiUsd（官方字段定义为 USD），经本机 /markets 的 open_interest_usd 透出'
        : '列表行 /api/v1/markets 的 open_interest_usd（本机采集字段）'
      : '无来源';
  const oiTitle = [
    `合约：${token.exchange}/${token.originalSymbol}（${token.id}）`,
    `总持仓金额（本列主显示）：${formatWanU(oiUsd)}`,
    `金额口径：${usdBasisText}；仅 ÷10000 显示为「万U」，未做任何汇率/币种换算。`,
    `金额来源：${usdFromSnapshot ? '本机 public-extras 快照' : listUsdRaw !== null ? '列表行' : '无'}；指标时点 ${oiUsdTime ?? '未知'}；年龄 ${fmtAgeShort(oiUsdAge)}；状态 ${publicExtrasStatusLabel(oiUsdState)}`,
    `金额覆盖：本机 public-extras 快照只覆盖采集器页首有界集合（前端仅读当前视口行）；未覆盖或确实无金额的标的显示「暂无金额」，**不用数量或推算值顶替**。`,
    `数量（仅供核对，非本列主显示）：${isNum(oiAmount) ? formatAmountWanYi(oiAmount) : '—'} ${oiUnitLabel}；来源 ${publicOiPair ? 'public-extras 快照（较新）' : '列表行 /api/v1/markets'}；指标时点 ${oiAmountTime ?? '未知'}；年龄 ${fmtAgeShort(oiAmountAge)}；状态 ${publicExtrasStatusLabel(oiAmountState)}`,
    `来源：${oiSource}`,
    publicOiPair ? `API：${publicExtra?.oiSourceUrl ?? '币安 /futures/data/openInterestHist'}` : '列表来源 API：/api/v1/markets；数量按该响应 oi_time 展示，不与其他时间点拼接。',
    snapshotOlderThanList
      ? `陈旧优先已阻止：本机快照的数量/名义额指标时点 ${publicExtra?.oiAmountEventTime ?? '未知'} 早于列表行 OI 时点 ${listOiTime ?? '未知'}，故**数量取较新的列表值**；快照名义额仅按自身时点独立展示，未与数量拼接、未倒填。`
      : '',
    snapshotUsdUsable && !publicOiPair && isNum(oiUsd)
      ? `注意：数量的指标时点（${oiAmountTime ?? '未知'}）与金额的指标时点（${oiUsdTime ?? '未知'}）不同源不同时，二者未作拼接或换算。`
      : '',
    `快照写入时点：${publicExtra?.snapshotTime ?? '未使用 public-extras 快照'}；当前年龄 ${fmtAgeShort(snapshotAgeSeconds)}；快照状态 ${publicExtrasStatusLabel(snapshotState)}`,
    publicOiPair ? '数量与名义额来自 public-extras 同一 openInterestHist 行；总 OI 不包含多空拆分，不代表聪明钱金额。' : '',
    oiUnitLabel === '单位待核验' ? '现有单位字段不是逐合约已核验单位；缺少可用的合约单位/倍率元数据，数量保持原值，不作换算。' : '',
    oiUnitLabel === '单位待核验' && token.exchange === 'Binance'
      ? '口径一致性核对（2026-10-07，只读）：币安 openInterestHist 的 sumOpenInterestValue / sumOpenInterest 与合约最新价一致（0G −0.17% / BTC +0.20% / ETH +0.19% / 1000PEPE +0.18% / 1000BONK +0.66%），支持「数量以合约标的币计（含 1000 等倍数前缀）」。这是口径一致性核对，不是官方逐合约 ctVal/ctMult 文档级确认，故仍显示「单位待核验」且不做张↔币换算。'
      : '',
    publicExtra?.missingReason ?? '',
    publicExtra?.qualityNote ?? '',
  ].filter(Boolean).join('\n');

  /**
   * 第 3 列「日总成交额」的取值。
   *
   * 2026-10-12 口径调整（用户指令「成交额就显示日总成交额，去除滚动周期成交额」）：
   * 取消此前每 5 秒在 5m/15m/…/12h 之间轮换的「周期滚动成交额」，本列**只**显示
   * 后端 `/markets` 的 `volume_24h_quote`（日总成交额，计价币 USDT）。
   * 缺失 ⇒ `—`；不补 0、不用标的币量按价格折算、不借其他周期顶替。
   */
  const dailyVolumeTitle = [
    `日总成交额：${formatWanYi(token.volume24hUsd)}`,
    '来源：/api/v1/markets 的 volume_24h_quote（币安 quoteVolume / 欧易 volCcyQuote，计价币 USDT 口径），即最近 24 小时滚动总成交额。',
    `标的币成交量（仅供核对，多数合约后端未给）：${token.volume24hAmount === null ? '—' : formatAmountWanYi(token.volume24hAmount)}`,
    '本列主显示一律为**成交额(USDT)**，不使用标的币量按价格折算。',
  ].join('\n');

  return (
    <tr
      data-token-id={token.id}
      onClick={handleToggle}
      className={`cursor-pointer transition-colors ${
        isExpanded ? 'bg-neutral-850/90 font-medium' : 'hover:bg-neutral-850/50 bg-neutral-900/30'
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
          <TokenIcon symbol={token.symbol} base={token.originalSymbol} size="sm" />
          <div>
            <div className="font-bold text-neutral-100 flex items-center gap-1.5">
              <span>{token.symbol}</span>
              {token.activeSignalCount > 0 && (
                <span className="w-1.5 h-1.5 rounded-full bg-indigo-500 animate-ping"></span>
              )}
            </div>
            <div className="font-mono text-[11px] text-neutral-400 flex items-center gap-1.5">
              <span>${fmtPrice(token.price)}</span>
              <span className={(token.change24h ?? 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'}>
                {fmtSignedPercent(token.change24h)}
              </span>
            </div>
          </div>
        </div>
      </td>

      {/* Col 2: Circulating Market Cap (流动市值) —— 2026-10-12 移除「流通率」子行 */}
      <td className="py-3 px-3 font-mono">
        <div
          className="text-neutral-100 font-bold text-xs"
          title={[
            `流动市值：${formatWanYi(token.marketCapUsd)}`,
            '口径：最新价 × CMC 流通量（分子分母同为**合约标的币**单位），故直接相乘即得美元市值。',
            `流通量（官方原值）：${token.circulatingSupply === null ? '—' : formatAmountWanYi(token.circulatingSupply)}（以合约标的币为单位）`,
            `市值口径时点：${token.marketCapAsOf ?? '—'}（取「价格时点」与「流通量时点」中较晚者）`,
            `来源：${token.marketCapNote ?? '无来源（本合约无流通量数据）'}`,
            '纪律：价格或流通量缺一 ⇒ 留空，不补值、不用现价乘历史供应量、不跨币顶替。',
          ].join('\n')}
        >
          {formatWanYi(token.marketCapUsd)}
        </div>
      </td>

      {/* Col 3: 日总成交额（USDT）—— 2026-10-12 去除周期滚动成交额 */}
      <td className="py-3 px-3 font-mono">
        <div className="text-neutral-100 font-bold text-xs" title={dailyVolumeTitle}>
          {formatWanYi(token.volume24hUsd)}
        </div>
      </td>

      {/* Col 4: Smart Money Long —— 三个真实槽位（持仓额 / 巨鲸占比 / 均价），空值显式占位 */}
      <td className="py-3 px-3 font-mono">
        <div className="text-emerald-400 font-bold">
          持仓额: {isNum(token.smartMoneyLong.amountUsd) ? `${formatWanYi(token.smartMoneyLong.amountUsd, '')} USD` : SMART_MONEY_EMPTY_TEXT}
        </div>
        <div className="text-[10px] text-neutral-400">
          巨鲸占比: {fmtPercent(token.smartMoneyLong.ratioPercent, 1)}
        </div>
        <div className="text-[10px] text-neutral-400">
          均价: {isNum(token.smartMoneyLong.avgPrice) ? `${fmtRaw(token.smartMoneyLong.avgPrice)} USDT` : SMART_MONEY_EMPTY_TEXT}
        </div>
      </td>

      {/* Col 5: Smart Money Short —— 同上 */}
      <td className="py-3 px-3 font-mono">
        <div className="text-rose-400 font-bold">
          持仓额: {isNum(token.smartMoneyShort.amountUsd) ? `${formatWanYi(token.smartMoneyShort.amountUsd, '')} USD` : SMART_MONEY_EMPTY_TEXT}
        </div>
        <div className="text-[10px] text-neutral-400">
          巨鲸占比: {fmtPercent(token.smartMoneyShort.ratioPercent, 1)}
        </div>
        <div className="text-[10px] text-neutral-400">
          均价: {isNum(token.smartMoneyShort.avgPrice) ? `${fmtRaw(token.smartMoneyShort.avgPrice)} USDT` : SMART_MONEY_EMPTY_TEXT}
        </div>
      </td>

      {/* Col 6: 总持仓 —— 主显示只放真实金额（万U）；
          数量/单位/来源/更新时间/覆盖全部移入原悬停说明，未新增任何模块。
          2026-10-12：金额配色由「近白/灰」改为**绿色**（有值即 emerald，仅「陈旧」保留琥珀告警；
          无值仍是中性灰空态，不用绿色冒充有数据）。 */}
      <td className="py-3 px-3 font-mono">
        <div
          className={`font-bold text-sm ${
            oiUsdState === 'stale'
              ? 'text-amber-400'
              : isNum(oiUsd)
                ? 'text-emerald-400'
                : 'text-neutral-500'
          }`}
        >
          {formatWanU(oiUsd)}
        </div>
        <div className={`text-[10px] ${publicExtrasStatusClass(oiUsdState)}`}>
          {publicExtrasStatusLabel(oiUsdState)}
        </div>
      </td>

      {/* Col 7: Funding Rate — 按交易所实际费率显示（2026-10-12 取消 8h 口径闸门） */}
      <td className="py-3 px-3 font-mono">
        <span
          className={`font-semibold px-2 py-0.5 rounded text-[11px] ${
            (token.fundingRate ?? 0) >= 0
              ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
              : 'bg-rose-500/10 text-rose-400 border border-rose-500/20'
          }`}
        >
          {isNum(token.fundingRate) ? fmtSignedPercent(token.fundingRate * 100, 4) : '—'}
        </span>
      </td>

      {/* Col 8: Sparkline mini chart */}
      <td className="py-3 px-3">
        <Sparkline values={token.sparkline24h} positive={(token.change24h ?? 0) >= 0} />
      </td>

      {/* Action Column */}
      <td className="py-3 px-3 text-right">
        <div className="flex items-center justify-end gap-1.5">
          <button
            type="button"
            aria-label={`打开 ${token.symbol} 的来源与口径详情`}
            aria-haspopup="dialog"
            title="查看来源与口径"
            onClick={(event) => {
              event.stopPropagation();
              onOpenSourceDetails({
                symbol: token.symbol,
                smartMoneyLong: smartMoneySideTitle(token.smartMoneyLong, '多头'),
                smartMoneyShort: smartMoneySideTitle(token.smartMoneyShort, '空头'),
                oi: oiTitle,
              });
            }}
            className="inline-flex h-7 w-7 items-center justify-center rounded border border-neutral-700 bg-neutral-800 text-neutral-300 transition-colors hover:border-neutral-600 hover:text-emerald-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-emerald-400"
          >
            <Info className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              handleToggle();
            }}
            className="px-2.5 py-1 rounded bg-neutral-800 hover:bg-neutral-700 text-neutral-300 text-[11px] font-medium transition-colors"
          >
            {isExpanded ? '收起' : '展开K线'}
          </button>
        </div>
      </td>
    </tr>
  );
});

export const TokenTable: React.FC<TokenTableProps> = ({
  tokens,
  expandedTokenId,
  onToggleExpand,
  activeSignal,
  onVisibleTokenIdsChange,
  publicExtras,
}) => {
  const [searchQuery, setSearchQuery] = useState('');
  const [freshnessNowMs, setFreshnessNowMs] = useState(() => Date.now());
  const [sourceDetails, setSourceDetails] = useState<MarketSourceDetails | null>(null);
  const sourceDialogRef = useRef<HTMLDialogElement | null>(null);
  const handleOpenSourceDetails = useCallback((details: MarketSourceDetails) => {
    setSourceDetails(details);
  }, []);

  /** 指标年龄按本机时钟推进，不把 API 响应里的 age_seconds 冻结在获取瞬间。 */
  useEffect(() => {
    const tick = () => setFreshnessNowMs(Date.now());
    const timer = setInterval(tick, 10_000);
    document.addEventListener('visibilitychange', tick);
    window.addEventListener('focus', tick);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', tick);
      window.removeEventListener('focus', tick);
    };
  }, []);

  useEffect(() => {
    const dialog = sourceDialogRef.current;
    if (!dialog) return;
    if (sourceDetails && !dialog.open) dialog.showModal();
    if (!sourceDetails && dialog.open) dialog.close();
  }, [sourceDetails]);

  /** 表格滚动容器：既是视口（虚拟化窗口来源），也是可见行上报的 root */
  const scrollRef = useRef<HTMLDivElement | null>(null);

  const filteredTokens = useMemo(() => {
    if (!searchQuery) return tokens;
    const q = searchQuery.toLowerCase();
    return tokens.filter(
      (t) =>
        t.symbol.toLowerCase().includes(q) ||
        t.originalSymbol.toLowerCase().includes(q) ||
        t.fullName.toLowerCase().includes(q),
    );
  }, [tokens, searchQuery]);

  const expandedIndex = useMemo(
    () => (expandedTokenId ? filteredTokens.findIndex((t) => t.id === expandedTokenId) : -1),
    [filteredTokens, expandedTokenId],
  );

  // ---- 虚拟化窗口状态 ----
  const [range, setRange] = useState<{ start: number; end: number }>({
    start: 0,
    end: INITIAL_WINDOW_ROWS,
  });
  const [rowH, setRowH] = useState(DEFAULT_ROW_H);
  const [expandedH, setExpandedH] = useState(DEFAULT_EXPANDED_H);

  /**
   * 横向滚动容器的可视宽度（clientWidth），用于把展开图约束在可视区内。
   *
   * 表格是 `table-layout: fixed` + colgroup 合计 **1260px**，而滚动容器可视宽仅 ~1096px
   * （1800 视口实测：scroll clientWidth 1096 / scrollWidth 1260，横向溢出 164px）。
   * 展开行是 `<td colSpan={10}>`，其宽度 = 整表宽 1260px，因此展开图被拉成 1208px，
   * 右侧 78px 价格轴画布落在 x 1573–1651，而滚动视口右缘只有 1524 ——
   * **刻度整段被裁在可视区外**，必须横向滚到底才能看到 OI / K 线的价格读数。
   *
   * 修法见下方展开行 JSX：包一层 `sticky left-0` 的壳并取其宽度，
   * 图表与价格轴因此始终完整可见，横向滚动时壳吸附左缘。不改列宽、不改行结构。
   */
  const [scrollViewportW, setScrollViewportW] = useState(0);

  const countRef = useRef(filteredTokens.length);
  const itemsRef = useRef(filteredTokens);
  const expIndexRef = useRef(expandedIndex);
  const rowHRef = useRef(rowH);
  const expHRef = useRef(expandedH);
  const rangeRef = useRef(range);
  const notifyRef = useRef(onVisibleTokenIdsChange);
  const visibleIdsRef = useRef<string[]>([]);
  const expandedRoRef = useRef<ResizeObserver | null>(null);

  countRef.current = filteredTokens.length;
  itemsRef.current = filteredTokens;
  expIndexRef.current = expandedIndex;
  rowHRef.current = rowH;
  expHRef.current = expandedH;
  rangeRef.current = range;
  notifyRef.current = onVisibleTokenIdsChange;

  /** 依据当前滚动位置重算窗口；区间未变则不触发渲染。 */
  const recompute = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const r = computeRange(
      el.scrollTop,
      el.clientHeight,
      countRef.current,
      expIndexRef.current,
      rowHRef.current,
      expHRef.current,
      OVERSCAN_PX,
    );
    if (r.start !== rangeRef.current.start || r.end !== rangeRef.current.end) {
      rangeRef.current = r;
      setRange(r);
    }
  }, []);

  /**
   * 可见行上报（替代原「549 个 IntersectionObserver」）：
   * 按滚动位置直接算出「视口 ± 300px」覆盖的行 id，集合变化时才回调。
   */
  const reportVisible = useCallback(() => {
    const el = scrollRef.current;
    const n = countRef.current;
    if (!el || n === 0) {
      if (visibleIdsRef.current.length > 0) {
        visibleIdsRef.current = [];
        notifyRef.current?.([]);
      }
      return;
    }
    const rowHv = rowHRef.current;
    const marginRows = Math.ceil(VISIBLE_MARGIN_PX / rowHv);
    const st = el.scrollTop;
    const vh = el.clientHeight;
    const e = expIndexRef.current;
    const eff = e >= 0 && st > e * rowHv ? Math.max(0, st - expHRef.current) : st;
    const firstVisible = Math.floor(eff / rowHv);
    const first = Math.max(0, firstVisible - marginRows);
    const last = Math.min(n - 1, firstVisible + Math.ceil(vh / rowHv) + marginRows);
    const ids = itemsRef.current.slice(first, last + 1).map((t) => t.id);
    const prev = visibleIdsRef.current;
    if (prev.length === ids.length && ids.every((id, i) => prev[i] === id)) return;
    visibleIdsRef.current = ids;
    notifyRef.current?.(ids);
  }, []);

  /** 滚动/尺寸变化 → 重算窗口与可见行（rAF 节流）。 */
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    let raf = 0;
    const onChange = () => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        recompute();
        reportVisible();
      });
    };
    el.addEventListener('scroll', onChange, { passive: true });
    const ro = new ResizeObserver(onChange);
    ro.observe(el);
    return () => {
      el.removeEventListener('scroll', onChange);
      if (raf) cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, [recompute, reportVisible]);

  /**
   * 量测滚动容器可视宽度（供展开图「贴合可视区」使用）。
   *
   * 独立于上面的滚动监听 effect：那里是 rAF 节流 + 每次滚动都跑，
   * 若把 setState 混进去会在滚动时造成重渲染风暴；此处只随尺寸变化更新，
   * 且差值 ≤0.5px 时直接沿用旧值（滚动条出现/消失会带来 1px 级抖动）。
   */
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const sync = () => {
      const w = el.clientWidth;
      setScrollViewportW((prev) => (Math.abs(prev - w) > 0.5 ? w : prev));
    };
    sync();
    const ro = new ResizeObserver(sync);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  /** 过滤结果 / 展开项 / 行高 / 展开块高度变化 → 重算窗口与可见行。 */
  useEffect(() => {
    recompute();
    reportVisible();
  }, [filteredTokens, expandedIndex, rowH, expandedH, recompute, reportVisible]);

  /**
   * 量测真实行高（首行），代入占位行换算；与常量差异 >0.5px 才更新。
   *
   * 2026-10-07 余项验收注记：曾怀疑「首屏 rowH 偏大导致占位总高多 660px」，
   * 实测排除 —— 那 660px 是 `App.tsx` 挂载时**自动展开 BTC 行**的合法预留
   * （BTC 在 549 行池中位于当前窗口之外，`offsetOf(count)` 含一次 `expandedH`，
   * 而窗口末端不含），并非测量偏差。故此处保持原来的单次量测，不加延迟复测。
   */
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const tr = el.querySelector<HTMLElement>('tr[data-token-id]');
    if (!tr) return;
    const h = tr.offsetHeight;
    if (h > 0 && Math.abs(h - rowHRef.current) > 0.5) {
      rowHRef.current = h;
      setRowH(h);
    }
  }, [range, filteredTokens.length]);

  /** 展开块高度：ResizeObserver 量测，用于占位换算（收起后沿用上次测量值）。 */
  const expandedRowRef = useCallback((node: HTMLTableRowElement | null) => {
    if (expandedRoRef.current) {
      expandedRoRef.current.disconnect();
      expandedRoRef.current = null;
    }
    if (!node) return;
    const measure = () => {
      const h = node.offsetHeight;
      if (h > 0 && Math.abs(h - expHRef.current) > 1) {
        expHRef.current = h;
        setExpandedH(h);
      }
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(node);
    expandedRoRef.current = ro;
  }, []);

  /** 关闭当前展开行（仅一个展开项，引用稳定，避免展开图被无谓重渲染）。 */
  const handleCloseExpanded = useCallback(() => {
    if (expandedTokenId) onToggleExpand(expandedTokenId);
  }, [expandedTokenId, onToggleExpand]);

  const count = filteredTokens.length;
  const clampedStart = count > 0 ? Math.max(0, Math.min(range.start, count - 1)) : 0;
  const clampedEnd = count > 0 ? Math.max(clampedStart - 1, Math.min(range.end, count - 1)) : -1;
  const windowTokens = count > 0 ? filteredTokens.slice(clampedStart, clampedEnd + 1) : [];

  /** 占位高度换算：展开块位于 index 之前时，其高度计入该 index 的偏移。 */
  const offsetOf = useCallback(
    (index: number) => index * rowH + (expandedIndex >= 0 && index > expandedIndex ? expandedH : 0),
    [rowH, expandedH, expandedIndex],
  );
  /** 外部选择信号/持仓时，把目标展开行滚入视口，确保虚拟化列表实际挂载其 K 线。 */
  useEffect(() => {
    if (!expandedTokenId || expandedIndex < 0) return;
    const el = scrollRef.current;
    if (!el) return;
    const rowTop = offsetOf(expandedIndex);
    const rowBottom = rowTop + rowH + expandedH;
    const viewportTop = el.scrollTop;
    const viewportBottom = viewportTop + el.clientHeight;
    if (rowTop < viewportTop || rowBottom > viewportBottom) {
      el.scrollTo({ top: Math.max(0, rowTop - 24), behavior: 'smooth' });
    }
  }, [expandedTokenId, expandedIndex, offsetOf, rowH, expandedH]);
  const topPad = count > 0 ? offsetOf(clampedStart) : 0;
  const bottomPad = count > 0 ? Math.max(0, offsetOf(count) - offsetOf(clampedEnd + 1)) : 0;

  return (
    <div className="workbench-market-panel flex-1 flex flex-col overflow-hidden bg-neutral-950 p-4 space-y-3 min-w-0 min-h-0 ai-grid">
      {/* Top Header of Table matching sketch */}
      <div className="flex flex-wrap items-center justify-between gap-3 select-none">
        <div>
          <h2 className="text-base font-bold text-neutral-100 flex items-center gap-2">
            <span className="ai-gradient-text">全市场代币排序</span>
            <span className="text-xs px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-normal ai-shimmer">
              7 组标准列结构
            </span>
          </h2>
          <p className="text-[11px] text-neutral-400">
            代币简称已完成倍率映射去前缀；成交额按日总成交额展示
          </p>
        </div>

        {/* Sort Indicator matching sketch: 按信号逻辑排序 ↓ */}
        <div className="flex items-center gap-2 text-xs font-semibold text-emerald-400 bg-neutral-900 px-3 py-1.5 rounded-lg border border-neutral-800 ai-aurora">
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
          共 {count} 个标的
        </div>
      </div>

      {/* Main Table Container */}
      <div ref={scrollRef} className="market-table-scroll rounded-xl border border-neutral-800 bg-neutral-900/60 shadow-lg">
        <table className="workbench-market-table w-full text-left text-xs">
          <colgroup>
            <col style={{ width: 40 }} />
            <col style={{ width: 158 }} />
            <col style={{ width: 106 }} />
            <col style={{ width: 168 }} />
            <col style={{ width: 156 }} />
            <col style={{ width: 156 }} />
            <col style={{ width: 124 }} />
            <col style={{ width: 104 }} />
            <col style={{ width: 120 }} />
            <col style={{ width: 128 }} />
          </colgroup>
          {/* 7 Columns Table Header matching sketch */}
          <thead className="sticky top-0 z-20 bg-neutral-900 border-b border-neutral-800 text-neutral-300 font-semibold select-none shadow-sm">
            <tr>
              <th className="py-3 px-3 w-8"></th>
              {/* Col 1 */}
              <th className="py-3 px-3">1. 代币 (简称)</th>
              {/* Col 2: Circulating Market Cap (流动市值) */}
              <th className="py-3 px-3">2. 流动市值</th>
              {/* Col 3: 日总成交额（USDT） */}
              <th className="py-3 px-3">3. 日总成交额</th>
              {/* Col 4 */}
              <th className="py-3 px-3 text-emerald-400">4. 聪明钱多头</th>
              {/* Col 5 */}
              <th className="py-3 px-3 text-rose-400">5. 聪明钱空头</th>
              {/* Col 6 */}
              <th className="py-3 px-3">6. 总OI持仓</th>
              {/* Col 7 */}
              <th className="py-3 px-3">7. 资金费率</th>
              {/* Col 8 */}
              <th className="py-3 px-3 text-center">8. 近24h小时图</th>
              <th className="py-3 px-3 text-right">展开详情</th>
            </tr>
          </thead>

          <tbody className="divide-y divide-neutral-800/60">
            {/* 上方占位行：补齐窗口前所有行的高度（含已滚出视口的展开块） */}
            {topPad > 0 && (
              <tr aria-hidden="true" style={{ borderTop: 'none' }}>
                <td
                  colSpan={10}
                  style={{ height: topPad, padding: 0, border: 0, lineHeight: 0 }}
                />
              </tr>
            )}

            {windowTokens.map((token) => (
              <React.Fragment key={token.id}>
                <TokenRow
                  token={token}
                  isExpanded={expandedTokenId === token.id}
                  publicExtra={publicExtras?.[token.id]}
                  freshnessNowMs={freshnessNowMs}
                  onToggleExpand={onToggleExpand}
                  onOpenSourceDetails={handleOpenSourceDetails}
                />

                {/* Inline Expanded Area Row：仅当展开行落在渲染窗口内才挂载，
                    滚出窗口即卸载重图表、滚回即恢复（展开态由 expandedTokenId 保持）。 */}
                {expandedTokenId === token.id && (
                  <tr ref={expandedRowRef}>
                    <td colSpan={10} className="p-0 border-b border-neutral-700/80 bg-neutral-950">
                      {/*
                        展开图外壳：`sticky left-0` + 滚动容器可视宽。
                        展开行是 colSpan=10 的 td（宽 1260px），直接放图表会把右侧
                        78px 价格轴推到可视区外（详见 scrollViewportW 注释）。
                        外壳宽度取 clientWidth 后，图表 + 价格轴完整可见；
                        横向滚动时外壳吸附在滚动视口左缘，不会跟着滑走。
                        scrollViewportW 首帧为 0（未量测）→ 退化为 auto，与旧行为一致。
                      */}
                      <div
                        className="sticky left-0"
                        style={scrollViewportW > 0 ? { width: scrollViewportW } : undefined}
                      >
                        <InlineExpandedChart
                          token={token}
                          activeSignal={activeSignal}
                          publicExtras={publicExtras?.[token.id]}
                          onClose={handleCloseExpanded}
                        />
                      </div>
                    </td>
                  </tr>
                )}
              </React.Fragment>
            ))}

            {/* 下方占位行：补齐窗口后所有行的高度 */}
            {bottomPad > 0 && (
              <tr aria-hidden="true" style={{ borderTop: 'none' }}>
                <td
                  colSpan={10}
                  style={{ height: bottomPad, padding: 0, border: 0, lineHeight: 0 }}
                />
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Table Footer Notes matching sketch */}
      <div className="flex flex-wrap items-center justify-between text-[11px] text-neutral-500 pt-1 select-none">
        <span>
          代币仅简称：确认倍率映射后去前缀（如 1000000BOB→BOB），0G、1INCH等固有数字保留；方向绿多红空、K线涨绿跌红。
        </span>
        <span className="font-mono text-neutral-400">
          第2列：流动市值（万/亿）；第3列：日总成交额（USDT 计价）
        </span>
      </div>

      <dialog
        ref={sourceDialogRef}
        className="market-source-dialog"
        aria-labelledby="market-source-dialog-title"
        onCancel={(event) => {
          event.preventDefault();
          setSourceDetails(null);
        }}
        onClose={() => setSourceDetails(null)}
      >
        <div className="mb-4 flex items-start justify-between gap-4">
          <div>
            <p className="mb-1 text-[11px] font-medium uppercase tracking-wide text-emerald-400">
              {sourceDetails?.symbol ?? ''} · 完整来源与口径
            </p>
            <h2 id="market-source-dialog-title" className="text-lg font-bold text-neutral-100">
              行情数据说明
            </h2>
          </div>
          <button
            type="button"
            onClick={() => setSourceDetails(null)}
            className="rounded-lg border border-neutral-700 px-3 py-1.5 text-xs text-neutral-300 hover:bg-neutral-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-emerald-400"
          >
            关闭
          </button>
        </div>
        {sourceDetails && (
          <div className="space-y-4">
            <section aria-labelledby="source-smart-long-title">
              <h3 id="source-smart-long-title" className="mb-2 text-sm font-semibold text-emerald-400">聪明钱多头</h3>
              <pre className="rounded-lg border border-neutral-800 bg-neutral-950 p-3 text-xs leading-relaxed text-neutral-300">{sourceDetails.smartMoneyLong}</pre>
            </section>
            <section aria-labelledby="source-smart-short-title">
              <h3 id="source-smart-short-title" className="mb-2 text-sm font-semibold text-rose-400">聪明钱空头</h3>
              <pre className="rounded-lg border border-neutral-800 bg-neutral-950 p-3 text-xs leading-relaxed text-neutral-300">{sourceDetails.smartMoneyShort}</pre>
            </section>
            <section aria-labelledby="source-oi-title">
              <h3 id="source-oi-title" className="mb-2 text-sm font-semibold text-neutral-200">总OI持仓</h3>
              <pre className="rounded-lg border border-neutral-800 bg-neutral-950 p-3 text-xs leading-relaxed text-neutral-300">{sourceDetails.oi}</pre>
            </section>
          </div>
        )}
      </dialog>
    </div>
  );
};
