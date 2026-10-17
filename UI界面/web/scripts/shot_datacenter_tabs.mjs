/**
 * 数据中心各 tab 巡检截图（一次性）。
 * 用法：node scripts/_dc_tabs.mjs <输出目录>
 */
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';

const OUT = process.argv[2] || '/tmp/dc';
fs.mkdirSync(OUT, { recursive: true });

const TABS = [
  ['feeds', '全域数据名称与红绿灯看板'],
  ['backfill', '待补数据与核销清单'],
  ['matrix', '全资产协同矩阵'],
  ['kline', 'K线 (8周期)'],
  ['oi', 'OI'],
  ['funding', '费率'],
  ['smart_money', '聪明钱'],
];

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1680, height: 1050 } });
await page.goto('http://localhost:3000', { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(2500);
await page.getByText('数据中心', { exact: true }).first().click();
await page.waitForTimeout(4000);

for (const [id, label] of TABS) {
  try {
    await page.getByText(label, { exact: true }).first().click({ timeout: 4000 });
  } catch {
    await page.getByText(label).first().click({ timeout: 4000 });
  }
  await page.waitForTimeout(2500);
  const info = await page.evaluate(() => {
    const se = document.scrollingElement;
    const err = document.body.innerText.includes('Uncaught') || document.body.innerText.includes('is not defined');
    const ds = document.querySelectorAll('[class*="ds-card"]').length;
    return { docRatio: +(se.scrollHeight / se.clientHeight).toFixed(2), dsCards: ds, maybeErr: err };
  });
  console.log(id, JSON.stringify(info));
  await page.screenshot({ path: path.join(OUT, `tab_${id}.png`) });
}

await browser.close();
console.log('done');
