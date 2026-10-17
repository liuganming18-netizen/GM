// 分区截图：按 data-testid 截取工作台各屏
const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

const OUT = process.env.OUT_DIR || '/tmp/wb05_shots';
const BASE = process.env.BASE_URL || 'http://127.0.0.1:3010';
const PREFIX = process.env.PREFIX || 'sec_';
fs.mkdirSync(OUT, { recursive: true });

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1680, height: 1050 } });
  const page = await ctx.newPage();
  await page.goto(BASE + '/', { waitUntil: 'load', timeout: 60000 });
  await page.waitForTimeout(3500);
  const nav = page.getByText('回测复盘', { exact: true }).first();
  if (await nav.count() > 0) { await nav.click(); await page.waitForTimeout(14000); }

  const ids = ['lab-config', 'lab-metrics', 'lab-run-identity', 'lab-trades', 'lab-orders', 'lab-rejected', 'lab-funding', 'lab-runs'];
  for (const id of ids) {
    const el = page.locator(`[data-testid="${id}"]`).first();
    if (await el.count() === 0) { console.log('missing', id); continue; }
    try {
      await el.scrollIntoViewIfNeeded();
      await page.waitForTimeout(500);
      await el.screenshot({ path: path.join(OUT, PREFIX + id + '.png') });
      console.log('shot', id);
    } catch (e) { console.log('fail', id, String(e).slice(0, 120)); }
  }
  // 资金分析面板（无 testid，用标题定位）
  const fund = page.getByText('资金分析', { exact: false }).first();
  if (await fund.count() > 0) {
    try { await fund.scrollIntoViewIfNeeded(); await page.waitForTimeout(600); await page.screenshot({ path: path.join(OUT, PREFIX + 'funding.png'), fullPage: false }); console.log('shot funding'); } catch (e) { console.log('fail funding', String(e).slice(0,120)); }
  }
  const chartPanel = page.locator('[data-testid="lab-chart-panel"]').first();
  if (await chartPanel.count() > 0) {
    try { await chartPanel.scrollIntoViewIfNeeded(); await page.waitForTimeout(1200); await chartPanel.screenshot({ path: path.join(OUT, PREFIX + 'chart.png') }); console.log('shot chart'); } catch (e) { console.log('fail chart', String(e).slice(0,120)); }
  }
  await browser.close();
  console.log('done');
})().catch((e) => { console.error('FAIL:', e); process.exit(1); });
