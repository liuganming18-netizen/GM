/**
 * 回测复盘页 · 三项改造验收（只读，不改任何状态）
 *   ① 执行视图周期选择（日线 – 5分线）
 *   ② 逐笔明细 → ③ 资金分析（水下回撤）联动定位
 *   ③ 一屏布局 ratio + 失败请求清点
 *
 * 用法：node scripts/verify_backtest_link.mjs <输出目录> [runId]
 * 不传 runId ⇒ 自动从 `/backtests?limit=50` 里挑「有真实成交」的最新一条
 * （⑥ 列表是实时数据，并行窗口会持续新增 run ⇒ 不能写死 run_id）。
 */
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';

const OUT = process.argv[2] || '/tmp/bt_verify';
let RUN_ID = process.argv[3] || null;
const API = process.env.API_BASE || 'http://127.0.0.1:8787/api/v1';
fs.mkdirSync(OUT, { recursive: true });

/* ── 选一条「有真实成交」且落在列表前 50 的 run ── */
const report = { consoleErrors: [], failedResponses: [], checks: {}, shots: [] };
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1680, height: 1050 }, deviceScaleFactor: 1 });
page.on('console', (m) => {
  if (m.type() === 'error') report.consoleErrors.push(m.text().slice(0, 300));
});
page.on('response', (r) => {
  if (r.status() >= 400) report.failedResponses.push(`${r.status()} ${r.url().slice(0, 180)}`);
});

const shot = async (name) => {
  const p = path.join(OUT, `${name}.png`);
  await page.screenshot({ path: p, fullPage: false });
  report.shots.push(p);
  console.log('saved', p);
};

await page.goto('http://127.0.0.1:3000/', { waitUntil: 'networkidle' });
await page.waitForTimeout(1500);
await page.getByRole('button', { name: '回测复盘' }).click();
// ⑥ 实验记录列表要等 50 条 run + hash 补齐，不能只等固定毫秒
await page.waitForSelector('[data-run-id]', { timeout: 40000 });

/* ── 选一条「有真实成交」且**当前出现在页面上**的 run ──
 * ⚠️ ⑥ 列表是实时数据（并行窗口持续新建 run，且同批 run 的 created_at 相同 ⇒ 排序不稳定），
 * 前 50 条每次请求可能不同 ⇒ 必须「先拿候选集，再等它出现在 DOM」，不能写死也不能只查一次。
 */
async function candidatesWithTrades() {
  const r = await fetch(`${API}/backtests?limit=200`);
  const d = await r.json();
  const items = d.items ?? [];
  return {
    listCount: items.length,
    ids: items.filter((x) => (x.trades_count ?? 0) > 0).map((x) => x.run_id),
  };
}

if (!RUN_ID) {
  for (let attempt = 0; attempt < 10 && !RUN_ID; attempt++) {
    const c = await candidatesWithTrades();
    report.checks.candidates = { listCount: c.listCount, withTrades: c.ids.length };
    const present = await page.$$eval('[data-run-id]', (els, ids) => {
      const have = els.map((e) => e.getAttribute('data-run-id'));
      return have.filter((x) => ids.includes(x));
    }, c.ids);
    if (present.length > 0) {
      RUN_ID = present[0];
      break;
    }
    await page.waitForTimeout(3000);
  }
}
report.runId = RUN_ID;
if (!RUN_ID) {
  console.log('!! 页面上（前 50 条）没有「有真实成交」的 run，无法验收联动');
  fs.writeFileSync(path.join(OUT, 'report.json'), JSON.stringify(report, null, 2));
  await browser.close();
  process.exit(3);
}

await page.locator(`[data-run-id="${RUN_ID}"]`).click();
await page.waitForTimeout(3500);
await page.evaluate(() => document.querySelector('[data-backtest-scroll]')?.scrollTo({ top: 0 }));

/* ── ① 周期选择 ── */
report.checks.tfGroupFound = await page.locator('[data-testid="lab-chart-tf"]').count();
report.checks.tfButtons = await page.$$eval('[data-testid="lab-chart-tf"] button', (els) =>
  els.map((e) => ({
    tf: e.getAttribute('data-tf'),
    label: e.textContent.trim(),
    disabled: e.disabled,
    pressed: e.getAttribute('aria-pressed') === 'true',
  })),
);

const barCount = async () => {
  const t = await page.locator('[data-testid="lab-chart-panel"]').innerText();
  const m = t.match(/(\d[\d,]*)\s*根/);
  return m ? Number(m[1].replace(/,/g, '')) : null;
};
report.checks.barsAt15m = await barCount();
await shot('01_chart_15m');

const clickTf = async (tf) => {
  await page.locator(`[data-testid="lab-chart-tf"] button[data-tf="${tf}"]`).click();
  await page.waitForTimeout(1300);
};
await clickTf('1d');
report.checks.barsAt1d = await barCount();
report.checks.dayPressed = await page.locator('[data-testid="lab-chart-tf"] button[data-tf="1d"]').getAttribute('aria-pressed');
await shot('02_chart_1d');

await clickTf('4h');
report.checks.barsAt4h = await barCount();
await shot('03_chart_4h');

await clickTf('1h');
report.checks.barsAt1h = await barCount();

await clickTf('15m');
report.checks.barsBackTo15m = await barCount();

const m5 = page.locator('[data-testid="lab-chart-tf"] button[data-tf="5m"]');
report.checks.m5Disabled = await m5.isDisabled();
report.checks.m5Title = await m5.getAttribute('title');

/* ── ② 逐笔明细 → 资金分析联动 ── */
const vp = page.locator('[data-testid="lab-funding-viewport"]');
report.checks.fundingViewportFound = await vp.count();
report.checks.fundingViewportBefore = (await vp.count()) ? (await vp.innerText()).trim() : null;
report.checks.underwaterActiveBefore = await page.evaluate(() => {
  const btns = [...document.querySelectorAll('[data-testid="lab-funding-panel"] button')];
  const b = btns.find((x) => x.textContent.trim() === '水下回撤');
  return b ? b.className.includes('ds-accent') : null;
});
await shot('04_funding_before');

const tradeRow = page.locator('[data-trade-id]').first();
report.checks.tradeRowCount = await tradeRow.count();
if (report.checks.tradeRowCount > 0) {
  report.checks.tradeId = await tradeRow.getAttribute('data-trade-id');
  report.checks.tradeRowText = (await tradeRow.innerText()).replace(/\s+/g, ' ').trim().slice(0, 200);
  await tradeRow.scrollIntoViewIfNeeded();
  await tradeRow.click();
  await page.waitForTimeout(2200);

  report.checks.underwaterActiveAfter = await page.evaluate(() => {
    const btns = [...document.querySelectorAll('[data-testid="lab-funding-panel"] button')];
    const b = btns.find((x) => x.textContent.trim() === '水下回撤');
    return b ? b.className.includes('ds-accent') : null;
  });
  const focus = page.locator('[data-testid="lab-funding-focus"]');
  report.checks.focusBadgeFound = await focus.count();
  report.checks.focusBadgeText = (await focus.count()) ? (await focus.innerText()).trim() : null;
  report.checks.fundingViewportAfter = (await vp.innerText()).trim();
  report.checks.fundingReadout = (await page.locator('[data-testid="lab-funding-panel"]').innerText())
    .replace(/\s+/g, ' ')
    .slice(0, 320);
  // ④ 执行视图是否也认出了这一笔（图上高亮 + 底部选中读数）
  const chartText = (await page.locator('[data-testid="lab-chart-panel"]').innerText()).replace(/\s+/g, ' ');
  report.checks.chartPanelHasSelected = chartText.includes('已选中');
  report.checks.chartPanelSelectedLine = (chartText.match(/已选中[^|]{0,140}/) ?? [''])[0];

  await page.evaluate(() => document.querySelector('[data-backtest-scroll]')?.scrollTo({ top: 0 }));
  await page.waitForTimeout(400);
  await shot('05_linked_underwater');

  // 点「净值对比」应能切回，且定位徽章被清掉
  await page.locator('[data-testid="lab-funding-panel"] button:text-is("净值对比")').click();
  await page.waitForTimeout(700);
  await page.locator('[data-testid="lab-funding-panel"] button:text-is("全部")').click();
  await page.waitForTimeout(700);
  report.checks.focusBadgeAfterReset = await focus.count();
  await shot('06_after_reset');
}

/* ── ③ 一屏 ratio ── */
report.checks.scrollRatio = await page.evaluate(() => {
  const s = document.querySelector('[data-backtest-scroll]');
  return s ? { scrollHeight: s.scrollHeight, clientHeight: s.clientHeight, ratio: +(s.scrollHeight / s.clientHeight).toFixed(3) } : null;
});
report.checks.docRatio = await page.evaluate(() => {
  const d = document.scrollingElement;
  return d ? +(d.scrollHeight / d.clientHeight).toFixed(3) : null;
});
await shot('07_final');

await browser.close();
report.failedResponses = [...new Set(report.failedResponses)];
fs.writeFileSync(path.join(OUT, 'report.json'), JSON.stringify(report, null, 2));
console.log(JSON.stringify(report, null, 2));
