// WORK-04 验收截图（补充）：回测工作台「信号链 / 稳定性诊断」页签（真实值 / 缺失即「—」）。
const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

const OUT = process.env.OUT_DIR || '/tmp/wb04_tabs';
const BASE = process.env.BASE_URL || 'http://127.0.0.1:3010';
const RUN_ID = process.env.RUN_ID || '';

fs.mkdirSync(OUT, { recursive: true });

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1680, height: 1050 }, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  await page.goto(BASE + '/', { waitUntil: 'load', timeout: 60000 });
  await page.waitForTimeout(4500);

  await page.getByText('回测复盘', { exact: true }).first().click();
  await page.waitForTimeout(4000);

  if (RUN_ID) {
    const sel = page.locator('select').filter({ has: page.locator(`option[value="${RUN_ID}"]`) }).first();
    if (await sel.count() > 0) { await sel.selectOption(RUN_ID); await page.waitForTimeout(7000); }
  }

  for (const [tab, name] of [['信号链', '08_tab_signals.png'], ['稳定性诊断', '09_tab_diagnostics.png'], ['交易复盘', '10_tab_trades.png'], ['总览', '11_tab_overview.png']]) {
    const btn = page.getByText(tab, { exact: true }).first();
    if (await btn.count() > 0) {
      await btn.click();
      await page.waitForTimeout(1200);
      await page.screenshot({ path: path.join(OUT, name), fullPage: true });
      console.log('shot:', name);
    } else {
      console.log('tab not found:', tab);
    }
  }
  await browser.close();
  console.log('done');
})().catch((e) => { console.error('FAIL:', e); process.exit(1); });
