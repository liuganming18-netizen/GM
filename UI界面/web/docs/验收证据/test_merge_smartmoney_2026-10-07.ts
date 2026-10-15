/**
 * 回归测试：`mergeMarketRow` 不得用「列表空值」抹掉「按需取到的聪明钱真值」。
 *
 * 缺陷背景（2026-10-07 实测）：
 *   列表每 5s 刷新走 `/api/v1/markets`，该接口当前**不含**聪明钱原始字段
 *   ⇒ `adaptMarket` 给出空态；而展开行是按需从币安公开接口取到的**真值**。
 *   若刷新时无条件用列表值覆盖，**每 5 秒就会把真值抹成空**
 *   （端到端探针实测：修复前面板恒为「— / 暂无数据」，修复后为 86.3% / 83.6%）。
 *
 * 运行：
 *   node_modules/tsx/dist/cli.mjs docs/验收证据/test_merge_smartmoney_2026-10-07.ts
 */
import { adaptMarket, mergeMarketRow, EMPTY_SMART_MONEY_SIDE } from '../../src/api/adapters';
import type { RawMarket } from '../../src/api/types';
import type { TokenMarketItem } from '../../src/types/crypto';

const cases: Array<{ name: string; ok: boolean; detail: string }> = [];
const push = (name: string, ok: boolean, detail = '') => cases.push({ name, ok, detail });

/** 最小可用的 /markets 行（只填 adaptMarket 会读到的字段） */
function rawRow(over: Partial<RawMarket> = {}): RawMarket {
  return {
    exchange: 'binance',
    market_id: 'ARXUSDT',
    base: 'ARX',
    symbol: 'ARXUSDT',
    last_price: 0.23,
    price_change_ratio_24h: 0.01,
    market_cap_usd: null,
    volume_24h_quote: null,
    volume_24h_base: null,
    open_interest_usd: null,
    open_interest_total: null,
    open_interest_unit: null,
    oi_time: null,
    oi_source: null,
    funding_rate: null,
    smart_money: null,
    oi: null,
    summary: null,
    mark_price: null,
    ...over,
  } as unknown as RawMarket;
}

// ① 列表无聪明钱 → 空态
const base = adaptMarket(rawRow());
push('列表未带聪明钱 ⇒ 六槽空态', base.smartMoneyLong === EMPTY_SMART_MONEY_SIDE, 'shared EMPTY object');

// ② 列表带聪明钱原始响应 → 六槽被填
const withSm = adaptMarket(
  rawRow({
    smart_money: {
      available: true,
      reason: null,
      overview: { longTradersAvgEntryPrice: 0.2303209, shortTradersAvgEntryPrice: 0.25706929 },
      stats: { longPositions: 8176229.67218994, longWhalePositions: 8084914.59929724, shortPositions: 807278.43447855, shortWhalePositions: 681513.74056035 },
    },
  }),
);
push(
  '列表带 overview/stats ⇒ 持仓额=真实 USDT 金额',
  withSm.smartMoneyLong.amountUsd === 8176229.67218994,
  `amountUsd=${withSm.smartMoneyLong.amountUsd}`,
);
push('列表带 overview/stats ⇒ 巨鲸占比=同源分子分母', withSm.smartMoneyLong.ratioPercent === 98.9, `${withSm.smartMoneyLong.ratioPercent}`);
push('列表带 overview/stats ⇒ 均价=overview 真实字段', withSm.smartMoneyShort.avgPrice === 0.25706929, `${withSm.smartMoneyShort.avgPrice}`);

// ③ 关键回归：prev 有真值、next 为空态 ⇒ 必须保留真值
const merged = mergeMarketRow(withSm, base);
push(
  '★ 刷新（列表空态）不得抹掉已取到的真值',
  merged.smartMoneyLong.amountUsd === 8176229.67218994 &&
    merged.smartMoneyShort.avgPrice === 0.25706929,
  `long.amountUsd=${merged.smartMoneyLong.amountUsd}, short.avgPrice=${merged.smartMoneyShort.avgPrice}`,
);
push('★ 空态刷新后引用保持（仍指向同一真值对象）', merged.smartMoneyLong === withSm.smartMoneyLong, '');

// ④ 列表确实带了新值 ⇒ 必须替换
const newer = adaptMarket(
  rawRow({
    smart_money: {
      available: true,
      reason: null,
      overview: { longTradersAvgEntryPrice: 0.25, shortTradersAvgEntryPrice: 0.26 },
      stats: { longPositions: 1000, longWhalePositions: 900, shortPositions: 500, shortWhalePositions: 400 },
    },
  }),
);
const merged2 = mergeMarketRow(withSm, newer);
push('列表带来新值 ⇒ 覆盖为最新', merged2.smartMoneyLong.amountUsd === 1000, `amountUsd=${merged2.smartMoneyLong.amountUsd}`);

// ⑤ 值完全相同的两次刷新 ⇒ 保持身份（性能整改约定）
const sameAgain = adaptMarket(
  rawRow({
    smart_money: {
      available: true,
      reason: null,
      overview: { longTradersAvgEntryPrice: 0.2303209, shortTradersAvgEntryPrice: 0.25706929 },
      stats: { longPositions: 8176229.67218994, longWhalePositions: 8084914.59929724, shortPositions: 807278.43447855, shortWhalePositions: 681513.74056035 },
    },
  }),
);
const merged3 = mergeMarketRow(withSm, sameAgain);
push('值未变的刷新 ⇒ 返回 prev（跳过重渲染）', merged3 === withSm, merged3 === withSm ? 'identity kept' : 'new object created');

// ⑥ 双空态 ⇒ 早退返回 prev（列表刷新主路径）
const t0 = adaptMarket(rawRow()) as TokenMarketItem;
const t1 = adaptMarket(rawRow());
push('双空态刷新 ⇒ 返回 prev', mergeMarketRow(t0, t1) === t0, '');

const failed = cases.filter((c) => !c.ok);
console.log(JSON.stringify({ pass: failed.length === 0, cases }, null, 1));
console.log(`\nPASS=${failed.length === 0} cases=${cases.length} failed=${failed.length}`);
process.exit(failed.length === 0 ? 0 : 1);
