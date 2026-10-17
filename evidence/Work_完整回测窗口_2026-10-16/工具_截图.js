// WORK-05 截图工具：对给定 BASE_URL 截取回测窗口各状态。
// 用法: BASE_URL=http://127.0.0.1:3000 OUT_DIR=/tmp/x RUN_ID=bt_... node 工具_截图.js
const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

const OUT = process.env.OUT_DIR || '/tmp/wb05_shots';
const BASE = process.env.BASE_URL || 'http://127.0.0.1:3000';
const RUN_ID = process.env.RUN_ID || '';
const PREFIX = process.env.PREFIX || '';
const W = parseInt(process.env.VW || '1680', 10);
const H = parseInt(process.env.VH || '1050', 10);

fs.mkdirSync(OUT, { recursive: true });
const shot = async (page, name, fullPage = true) => {
  const p = path.join(OUT, PREFIX + name);
  await page.screenshot({ path: p, fullPage });
  console.log('shot:', p);
};

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  page.on('console', (m) => { if (m.type() === 'error') console.log('  [page error]', String(m.text()).slice(0, 300)); });
  page.on('response', (r) => {
    const u = r.url();
    if (u.includes('/api/v1/backtests')) console.log(`  [net] ${r.status()} ${u.replace(BASE, '').slice(0, 160)}`);
  });

  await page.goto(BASE + '/', { waitUntil: 'load', timeout: 60000 });
  await page.waitForTimeout(6000);

  // 进入「回测复盘」页
  const nav = page.getByText('回测复盘', { exact: true }).first();
  if (await nav.count() > 0) { await nav.click(); await page.waitForTimeout(14000); }
  else console.log('!! 未找到「回测复盘」导航');
  await shot(page, '01_backtest_page.png');

  // 选择 run
  if (RUN_ID) {
    const sel = page.locator('select').filter({ has: page.locator(`option[value="${RUN_ID}"]`) }).first();
    if (await sel.count() > 0) {
      await sel.selectOption(RUN_ID);
      console.log('已选择 run:', RUN_ID);
      await page.waitForTimeout(10000);
      await shot(page, '02_run_selected_full.png');
    } else console.log('!! 未找到含该 run 的 select');
  }

  await browser.close();
  console.log('done');
})().catch((e) => { console.error('FAIL:', e); process.exit(1); });
