/**
 * 用**前端自己的映射代码**（src/api/smartMoneyMapping.ts）验证后端代理响应可被页面读取。
 * 零依赖、零网络：读取本地保存的后端响应样例，跑 mapBinanceSmartMoney，打印六槽。
 * 运行： node --experimental-strip-types 前端契约读取验证.mjs <后端响应.json>
 */
import { readFileSync } from 'node:fs';
import { mapBinanceSmartMoney } from '../../前端实现-AIStudio/src/api/smartMoneyMapping.ts';

const file = process.argv[2];
const be = JSON.parse(readFileSync(file, 'utf8'));
const mapped = mapBinanceSmartMoney(be.overview ?? null, be.stats ?? null);
const ok = (v) => v !== null && v !== undefined;
const rows = [
  ['后端 available', String(be.available)],
  ['标的身份 symbol', String(be.symbol)],
  ['多头 持仓额(USDT)', String(mapped.long.amountUsd)],
  ['空头 持仓额(USDT)', String(mapped.short.amountUsd)],
  ['多头 巨鲸占比(%)', String(mapped.long.ratioPercent)],
  ['空头 巨鲸占比(%)', String(mapped.short.ratioPercent)],
  ['多头 均价', String(mapped.long.avgPrice)],
  ['空头 均价', String(mapped.short.avgPrice)],
  ['多头 交易员/巨鲸', `${mapped.long.traders}/${mapped.long.whaleTraders}`],
  ['总持仓(USDT)', String(mapped.totals.totalPositionsUsd)],
  ['名义多空比率', String(mapped.totals.longShortRatio)],
  ['24h 变化（币安不提供）', String(mapped.long.change24h)],
];
for (const [k, v] of rows) console.log(`  ${k.padEnd(22, ' ')} = ${v}`);
const filled = [mapped.long.amountUsd, mapped.short.amountUsd, mapped.long.avgPrice,
  mapped.short.avgPrice, mapped.long.ratioPercent, mapped.short.ratioPercent,
  mapped.totals.totalPositionsUsd, mapped.totals.longShortRatio].filter(ok).length;
console.log(`\n  PASS(可读槽位)=${filled}/8  ${filled === 8 ? 'OK' : 'FAIL'}`);
process.exit(filled === 8 ? 0 : 1);
