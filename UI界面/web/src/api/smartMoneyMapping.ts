/**
 * 聪明钱「币安公开接口 → 本工程六槽」映射（**已接入**）
 * ------------------------------------------------------------------
 * 真实入口（**页面实际请求捕获，非猜测**，2026-10-07 23:2x 实测）：
 *   GET https://www.binance.bh/bapi/futures/v1/public/future/smart-money/signal/overview?symbol={SYMBOL}
 *   GET https://www.binance.bh/bapi/futures/v1/public/future/smart-money/signal/details/stats?symbol={SYMBOL}&timeRange=ALL
 *   GET https://www.binance.bh/bapi/futures/v1/public/future/smart-money/signal/details/list?symbol={SYMBOL}&page=1&rows=10&timeRange=ALL&sortingField=TIME&sortingOrder=DESC
 * 路径含 `/public/`，实测 **无 cookie / 无凭据** 直连 200（`credentials:'omit'` + curl 双重验证）。
 * 页面声明「每 60 秒更新」。
 *
 * 六槽来源（**均为真实字段，无推算**）：
 *   持仓额(USDT) ← `longPositions` / `shortPositions`（stats 接口；**已是 USDT 金额**）
 *   巨鲸占比(%)  ← `longWhalePositions ÷ longPositions × 100`（stats 接口，同源同一响应）
 *   均价         ← `longTradersAvgEntryPrice` / `shortTradersAvgEntryPrice`（overview 接口）
 *
 * 纪律（硬约束）：
 *  1. **不猜字段**：字段名全部来自实测响应体。
 *  2. **不推算**：不用 OI、VWAP、entryPrice 编造；比例只用同一响应内的分子分母。
 *  3. **缺值保留 null**：不填 0、不用其它字段顶替；分母缺失或为 0 ⇒ null。
 *  4. 原 Tradeflow 字段名（longPos/shortPos 等）作为**兼容分支**保留，但**不再作为主来源**。
 *
 * 注意：本模块**不发起任何请求**，纯函数。
 */

/** 币安 `…/signal/overview` 响应 `data`（实测字段，2026-10-07） */
export interface BinanceSmartMoneyOverview {
  symbol?: string | null;
  /** 交易员总持仓（USDT） */
  totalPositions?: number | null;
  totalTraders?: number | null;
  /** 名义多空比率（页面显示为百分比，如 10.1541 → 1015.40%） */
  longShortRatio?: number | null;
  longTraders?: number | null;
  longTradersQty?: number | null;
  /** 多头交易员平均开仓价 */
  longTradersAvgEntryPrice?: number | null;
  longWhales?: number | null;
  longWhalesQty?: number | null;
  longWhalesAvgEntryPrice?: number | null;
  shortTraders?: number | null;
  shortTradersQty?: number | null;
  shortTradersAvgEntryPrice?: number | null;
  shortWhales?: number | null;
  shortWhalesQty?: number | null;
  shortWhalesAvgEntryPrice?: number | null;
  longProfitTraders?: number | null;
  shortProfitTraders?: number | null;
  longProfitWhales?: number | null;
  shortProfitWhales?: number | null;
  /** 数据时点（毫秒纪元） */
  updateTime?: number | null;
}

/** 币安 `…/signal/details/stats` 响应 `data`（实测字段，2026-10-07） */
export interface BinanceSmartMoneyStats {
  longTraders?: number | null;
  longWhales?: number | null;
  /** 多头当前仓位（USDT）——**六槽「持仓额」的来源** */
  longPositions?: number | null;
  longQty?: number | null;
  /** 多头巨鲸当前仓位（USDT） */
  longWhalePositions?: number | null;
  longWhaleQty?: number | null;
  shortTraders?: number | null;
  shortWhales?: number | null;
  /** 空头当前仓位（USDT） */
  shortPositions?: number | null;
  shortQty?: number | null;
  shortWhalePositions?: number | null;
  shortWhaleQty?: number | null;
}

/** 兼容分支：原 Tradeflow 字段名（保留但非主来源） */
export interface LegacyTradeflowPayload {
  longPos?: number | null;
  shortPos?: number | null;
  longWhalePos?: number | null;
  shortWhalePos?: number | null;
  longAvgPrice?: number | null;
  shortAvgPrice?: number | null;
  longWhalesAvgPrice?: number | null;
  shortWhalesAvgPrice?: number | null;
  shortgWhalesAvgPrice?: number | null;
  longTraders?: number | null;
  shortTraders?: number | null;
  longWhales?: number | null;
  shortWhales?: number | null;
}

/** 单侧六槽映射结果 */
export interface SmartMoneySideMapped {
  /** 持仓额（USDT）——真实金额，来源 `longPositions`/`shortPositions` */
  amountUsd: number | null;
  /** 巨鲸持仓占该侧持仓比例（%），1 位小数 */
  ratioPercent: number | null;
  /** 持仓均价——来源 `longTradersAvgEntryPrice`/`shortTradersAvgEntryPrice` */
  avgPrice: number | null;
  /** 巨鲸均价 */
  whaleAvgPrice: number | null;
  /** 交易员人数 / 其中巨鲸人数 */
  traders: number | null;
  whaleTraders: number | null;
  /** 24h 变化：币安该组接口不提供此口径 ⇒ 恒 null */
  change24h: null;
}

export interface SmartMoneyMapped {
  long: SmartMoneySideMapped;
  short: SmartMoneySideMapped;
  /** 总览：交易员总持仓(USDT)、交易者总数、名义多空比率、来源 updateTime */
  totals: {
    totalPositionsUsd: number | null;
    totalTraders: number | null;
    longShortRatio: number | null;
    /**
     * 来源 `overview.updateTime`。⚠️ **不随数据推进，不得当新鲜度用**：
     * 2026-10-07 间隔 73s 两次采样，业务字段全变而该值恒为 1791353989563（age ≈ 9.6h）。
     * 保留仅作原始字段留档；真实新鲜度取 `details/list[].lastTradeTime`。
     */
    updateTime: number | null;
  };
}

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const keep = (v: unknown): number | null => (isNum(v) ? v : null);

/**
 * 巨鲸持仓占该侧持仓比例（%）。
 * 口径：分子 = 该侧巨鲸仓位(USDT)，分母 = 该侧总仓位(USDT)，同源同一响应，保留 1 位小数。
 * 分母缺失或为 0 ⇒ **null**（不显示，不填 0）。
 */
export function whaleSharePercent(
  whalePositions: number | null | undefined,
  sidePositions: number | null | undefined,
): number | null {
  if (!isNum(whalePositions) || !isNum(sidePositions) || sidePositions === 0) return null;
  return Math.round((whalePositions / sidePositions) * 100 * 10) / 10;
}

function mapSide(
  positionsUsd: unknown,
  whalePositionsUsd: unknown,
  avgPrice: unknown,
  whaleAvgPrice: unknown,
  traders: unknown,
  whales: unknown,
): SmartMoneySideMapped {
  const pos = keep(positionsUsd);
  const wpos = keep(whalePositionsUsd);
  return {
    amountUsd: pos,
    ratioPercent: whaleSharePercent(wpos, pos),
    avgPrice: keep(avgPrice),
    whaleAvgPrice: keep(whaleAvgPrice),
    traders: keep(traders),
    whaleTraders: keep(whales),
    change24h: null,
  };
}

/** 币安主来源映射（overview + stats 合并） */
export function mapBinanceSmartMoney(
  overview: BinanceSmartMoneyOverview | null | undefined,
  stats: BinanceSmartMoneyStats | null | undefined,
): SmartMoneyMapped {
  const o = overview ?? {};
  const s = stats ?? {};
  return {
    long: mapSide(
      s.longPositions,
      s.longWhalePositions,
      o.longTradersAvgEntryPrice,
      o.longWhalesAvgEntryPrice,
      s.longTraders ?? o.longTraders,
      s.longWhales ?? o.longWhales,
    ),
    short: mapSide(
      s.shortPositions,
      s.shortWhalePositions,
      o.shortTradersAvgEntryPrice,
      o.shortWhalesAvgEntryPrice,
      s.shortTraders ?? o.shortTraders,
      s.shortWhales ?? o.shortWhales,
    ),
    totals: {
      totalPositionsUsd: keep(o.totalPositions),
      totalTraders: keep(o.totalTraders),
      longShortRatio: keep(o.longShortRatio),
      updateTime: keep(o.updateTime),
    },
  };
}

/** 兼容分支：原 Tradeflow 字段名映射（非主来源，保留以免历史证据失联） */
export function mapLegacySmartMoney(raw: LegacyTradeflowPayload | null | undefined): SmartMoneyMapped {
  const r = raw ?? {};
  const side = (
    p: unknown,
    wp: unknown,
    ap: unknown,
    wap: unknown,
    tr: unknown,
    wh: unknown,
  ): SmartMoneySideMapped => {
    const pos = keep(p);
    const wpos = keep(wp);
    return {
      // 原站 longPos/shortPos 是**数量**，不是金额 ⇒ 不写入 amountUsd
      amountUsd: null,
      ratioPercent: whaleSharePercent(wpos, pos),
      avgPrice: keep(ap),
      whaleAvgPrice: keep(wap),
      traders: keep(tr),
      whaleTraders: keep(wh),
      change24h: null,
    };
  };
  return {
    long: side(r.longPos, r.longWhalePos, r.longAvgPrice, r.longWhalesAvgPrice, r.longTraders, r.longWhales),
    short: side(
      r.shortPos,
      r.shortWhalePos,
      r.shortAvgPrice,
      r.shortWhalesAvgPrice ?? r.shortgWhalesAvgPrice,
      r.shortTraders,
      r.shortWhales,
    ),
    totals: { totalPositionsUsd: null, totalTraders: null, longShortRatio: null, updateTime: null },
  };
}

/* ------------------------------------------------------------------ *
 * 离线自测： node --experimental-strip-types <runner.mjs>
 * 夹具 = **真实捕获的 ARXUSDT 响应**，逐字取自
 *   docs/验收证据/币安聪明钱真实响应_2026-10-07.json
 *   （samples.ARXUSDT.overview.data / samples.ARXUSDT.stats.data，checkedAt 2026-10-07T15:40:22Z）
 * 断言值 = 该证据文件里的原始数值，因此自测失败即可直接与证据对账。
 * ------------------------------------------------------------------ */
export function selfTest(): { pass: boolean; cases: Array<{ name: string; ok: boolean; detail: string }> } {
  const cases: Array<{ name: string; ok: boolean; detail: string }> = [];
  const push = (name: string, ok: boolean, detail: string) => cases.push({ name, ok, detail });

  // 真实捕获值（ARXUSDT，2026-10-07T15:40:22Z）
  const overview: BinanceSmartMoneyOverview = {
    symbol: 'ARXUSDT',
    totalPositions: 8957987.2376,
    totalTraders: 270,
    longShortRatio: 10.1314,
    longTraders: 169,
    longTradersQty: 30343275,
    longTradersAvgEntryPrice: 0.2303209,
    longWhales: 102,
    longWhalesQty: 30004417,
    longWhalesAvgEntryPrice: 0.22993993,
    shortTraders: 101,
    shortTradersQty: 2994973,
    shortTradersAvgEntryPrice: 0.25706929,
    shortWhales: 68,
    shortWhalesQty: 2528233,
    shortWhalesAvgEntryPrice: 0.25853704,
    updateTime: 1791353989563,
  };
  const stats: BinanceSmartMoneyStats = {
    longTraders: 169,
    longWhales: 102,
    longPositions: 8176229.67218994,
    longQty: 30343758,
    longWhalePositions: 8084914.59929724,
    longWhaleQty: 30004868,
    shortTraders: 100,
    shortWhales: 67,
    shortPositions: 807278.43447855,
    shortQty: 2995985,
    shortWhalePositions: 681513.74056035,
    shortWhaleQty: 2529245,
  };
  const m = mapBinanceSmartMoney(overview, stats);

  push('多头持仓额取真实 longPositions(USDT)', m.long.amountUsd === 8176229.67218994, `amountUsd=${m.long.amountUsd}`);
  push('空头持仓额取真实 shortPositions(USDT)', m.short.amountUsd === 807278.43447855, `amountUsd=${m.short.amountUsd}`);
  push('多头均价取 longTradersAvgEntryPrice', m.long.avgPrice === 0.2303209, `avgPrice=${m.long.avgPrice}`);
  push('空头均价取 shortTradersAvgEntryPrice', m.short.avgPrice === 0.25706929, `avgPrice=${m.short.avgPrice}`);
  push('多头巨鲸占比 = 8084914.59929724/8176229.67218994 ≈ 98.9%', m.long.ratioPercent === 98.9, `ratioPercent=${m.long.ratioPercent}`);
  push('空头巨鲸占比 = 681513.74056035/807278.43447855 ≈ 84.4%', m.short.ratioPercent === 84.4, `ratioPercent=${m.short.ratioPercent}`);
  push('交易员人数取 stats.longTraders/shortTraders', m.long.traders === 169 && m.short.traders === 100, `${m.long.traders}/${m.short.traders}`);
  push('巨鲸人数取 stats.longWhales/shortWhales', m.long.whaleTraders === 102 && m.short.whaleTraders === 67, `${m.long.whaleTraders}/${m.short.whaleTraders}`);
  push('巨鲸均价取 overview.longWhalesAvgEntryPrice', m.long.whaleAvgPrice === 0.22993993, `whaleAvgPrice=${m.long.whaleAvgPrice}`);
  push('总览：总持仓(USDT)', m.totals.totalPositionsUsd === 8957987.2376, `${m.totals.totalPositionsUsd}`);
  push('总览：交易者总数', m.totals.totalTraders === 270, `${m.totals.totalTraders}`);
  push('总览：名义多空比率', m.totals.longShortRatio === 10.1314, `${m.totals.longShortRatio}`);
  push('总览：数据时点（来源原样保留，不当新鲜度用）', m.totals.updateTime === 1791353989563, `${m.totals.updateTime}`);
  push(
    '★ *Qty(数量字段) 绝不写入 amountUsd',
    m.long.amountUsd !== overview.longTradersQty &&
      m.long.amountUsd !== stats.longQty &&
      m.short.amountUsd !== overview.shortTradersQty &&
      m.short.amountUsd !== stats.shortQty,
    `amountUsd=${m.long.amountUsd} vs longQty=${stats.longQty} / longTradersQty=${overview.longTradersQty}`,
  );
  push(
    '★ 只有 *Qty、无 *Positions 时 amountUsd 必须为 null',
    mapBinanceSmartMoney({ longTradersQty: 999 }, { longQty: 999 }).long.amountUsd === null,
    'ok',
  );

  const empty = mapBinanceSmartMoney(null, null);
  push('空输入 → 六槽全 null', empty.long.amountUsd === null && empty.long.avgPrice === null && empty.long.ratioPercent === null, 'ok');
  push('空输入 → 空头同样 null', empty.short.amountUsd === null && empty.short.avgPrice === null, 'ok');
  push('分母缺失 → ratioPercent=null', mapBinanceSmartMoney(null, { longWhalePositions: 100 }).long.ratioPercent === null, 'ok');
  push('分母为 0 → ratioPercent=null', mapBinanceSmartMoney(null, { longWhalePositions: 100, longPositions: 0 }).long.ratioPercent === null, 'ok');
  push('只有仓位 → avgPrice=null（不顶替）', mapBinanceSmartMoney(null, { longPositions: 123 }).long.avgPrice === null, 'ok');

  const legacy = mapLegacySmartMoney({ longPos: 1000, longWhalePos: 400, longAvgPrice: 62000 });
  push('兼容分支：原站数量不写入 amountUsd', legacy.long.amountUsd === null, `amountUsd=${legacy.long.amountUsd}`);
  push('兼容分支：比例仍按来源口径 40%', legacy.long.ratioPercent === 40, `ratioPercent=${legacy.long.ratioPercent}`);
  push('兼容分支：均价取 longAvgPrice', legacy.long.avgPrice === 62000, `avgPrice=${legacy.long.avgPrice}`);

  return { pass: cases.every((c) => c.ok), cases };
}
