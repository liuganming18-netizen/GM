// WORK-04 验收截图：对隔离实例（前端 3010 → 后端 8799）截取全页面。
const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

const OUT = process.env.OUT_DIR || '/tmp/wb04_shots';
const BASE = process.env.BASE_URL || 'http://127.0.0.1:3010';
const RUN_ID = process.env.RUN_ID || '';

fs.mkdirSync(OUT, { recursive: true });

async function shot(page, name, opts = {}) {
  const p = path.join(OUT, name);
  await page.screenshot({ path: p, fullPage: opts.fullPage !== false });
  console.log('shot:', p);
}

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1680, height: 1050 }, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  page.on('console', (m) => { if (m.type() === 'error') console.log('  [page error]', String(m.text()).slice(0, 300)); });
  page.on('response', (r) => {
    const u = r.url();
    if (u.includes('/api/v1/backtests')) console.log(`  [net] ${r.status()} ${u.replace(BASE, '')}`);
  });

  await page.goto(BASE + '/', { waitUntil: 'load', timeout: 60000 });
  await page.waitForTimeout(4500);

  // ① 交易工作台：信号区 + 持仓区 的 NOT_CONNECTED
  await shot(page, '01_workbench_not_connected.png');
  const sigPanel = page.locator('[data-testid="signal-not-connected"]');
  const posPanel = page.locator('[data-testid="paper-not-connected"]');
  console.log('signal-not-connected 可见:', await sigPanel.count() > 0);
  console.log('paper-not-connected 可见:', await posPanel.count() > 0);
  if (await posPanel.count() > 0) await posPanel.first().scrollIntoViewIfNeeded();
  await page.waitForTimeout(400);
  await shot(page, '02_paper_not_connected_banner.png', { fullPage: false });
  if (await sigPanel.count() > 0) await sigPanel.first().scrollIntoViewIfNeeded();
  await page.waitForTimeout(400);
  await shot(page, '03_signal_not_connected_banner.png', { fullPage: false });

  // ② 回测复盘 → 单策略工作台
  await page.getByText('回测复盘', { exact: true }).first().click();
  await page.waitForTimeout(5000);
  await shot(page, '04_backtest_workbench_page.png');

  // 选中本次 run：run 选择器是 <select>
  if (RUN_ID) {
    const sel = page.locator('select').filter({ has: page.locator(`option[value="${RUN_ID}"]`) }).first();
    if (await sel.count() > 0) {
      await sel.selectOption(RUN_ID);
      console.log('已选择 run:', RUN_ID);
      await page.waitForTimeout(9000);
      await shot(page, '05_run_selected_full.png');
    } else {
      console.log('未找到含该 run 的 select');
    }
  }

  // ③ 历史阅读（旧通道）→ 退役横幅
  const legacyTab = page.locator('[data-testid="review-tab-legacy"]');
  if (await legacyTab.count() > 0) {
    await legacyTab.first().click();
    await page.waitForTimeout(3500);
    await shot(page, '06_legacy_channel_retired.png');
  }

  // ④ 策略管理页（注册表新旧归属）
  await page.getByText('策略管理', { exact: true }).first().click();
  await page.waitForTimeout(3000);
  await shot(page, '07_strategy_management.png');

  await browser.close();
  console.log('done');
})().catch((e) => { console.error('FAIL:', e); process.exit(1); });
