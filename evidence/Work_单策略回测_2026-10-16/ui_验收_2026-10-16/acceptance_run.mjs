/**
 * 真实页面验收（只读 + 一次真实 POST）：
 *   打开 3000 → 回测复盘 → 单策略工作台 → 默认固定窗/默认成本 → 运行回测
 *   → 轮询到终态 → 取新 run_id 与真实指标 → 点击一笔真实交易 → 图表定位 + 同 trade_id 详情
 *   → 保存三张截图（首屏 / 资金分析 / 交易选中定位）并输出 JSON 证据。
 *
 * 不做任何写库/改撮合；POST 只创建一次研究回测（后台线程执行）。
 */
import pw from '/Users/boos/.workbuddy-ai/binaries/node/workspace/node_modules/playwright/index.js';
import fs from 'node:fs';
const { chromium } = pw;

const OUT = process.argv[2] ?? '.';
const W = Number(process.argv[3] ?? 1280);
const H = Number(process.argv[4] ?? 1000);

const ev = { startedAt: new Date().toISOString(), viewport: { W, H }, steps: [] };
const log = (k, v) => {
  ev.steps.push({ step: k, ...v });
  console.log('·', k, JSON.stringify(v).slice(0, 400));
};

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: W, height: H }, deviceScaleFactor: 2 });
const apiCalls = [];
page.on('response', (r) => {
  const u = r.url();
  if (u.includes('/api/v1/')) apiCalls.push(`${r.request().method()} ${r.status()} ${u.split('/api/v1/')[1]}`);
});
page.on('console', (m) => { if (m.type() === 'error') console.log('[console.error]', m.text().slice(0, 200)); });

await page.goto('http://localhost:3000/', { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(2000);
await page.getByText('回测复盘', { exact: true }).first().click();
await page.waitForTimeout(6000);

// ── 1. 默认配置（不继承旧 5000 账户；默认成本）──
const form = await page.evaluate(() => ({
  symbol: document.querySelector('[data-testid="lab-symbol-input"]')?.value ?? null,
  ws: document.querySelector('[data-testid="lab-window-start"]')?.value ?? null,
  we: document.querySelector('[data-testid="lab-window-end"]')?.value ?? null,
  params: Object.fromEntries(
    [...document.querySelectorAll('[data-param-key]')].map((el) => [el.getAttribute('data-param-key'), el.value]),
  ),
  errors: document.querySelector('[data-testid="lab-form-errors"]')?.textContent?.trim() ?? null,
  cfgH: Math.round(document.querySelector('[data-testid="lab-config"]')?.getBoundingClientRect().height ?? -1),
  metricsTop: Math.round(document.querySelector('[data-testid="lab-metrics"]')?.getBoundingClientRect().top ?? -1),
  viewportH: window.innerHeight,
}));
log('form_defaults', form);
await page.screenshot({ path: `${OUT}/01_首屏_配置与指标_${W}.png` });

// ── 2. 真实 POST 运行 ──
const before = await (await fetch('http://127.0.0.1:8787/api/v1/backtests')).json();
const beforeIds = new Set((before.items ?? []).map((r) => r.run_id));
await page.click('[data-testid="lab-run-button"]');
await page.waitForTimeout(1500);
log('post_submitted', { progress: await page.textContent('[data-testid="lab-run-progress"]').catch(() => null) });
// 提交后按钮应立刻禁用（本页 run 未进终态）
log('button_disabled_after_submit', { disabled: await page.isDisabled('[data-testid="lab-run-button"]') });

// ── 3. 轮询到终态 ──
let run = null;
for (let i = 0; i < 90; i++) {
  const j = await (await fetch('http://127.0.0.1:8787/api/v1/backtests')).json();
  run = (j.items ?? []).find((r) => !beforeIds.has(r.run_id)) ?? null;
  if (run && (run.status === 'succeeded' || run.status === 'failed')) break;
  await page.waitForTimeout(2000);
}
log('new_run', run ? { run_id: run.run_id, status: run.status, symbols: run.symbols, window: run.window } : null);
if (!run) { await browser.close(); throw new Error('未拿到新 run_id'); }
ev.new_run_id = run.run_id;

// 等页面把该 run 的结果渲染出来
await page.waitForTimeout(4000);
const metricsTxt = await page.evaluate(() => {
  const card = document.querySelector('[data-testid="lab-metrics"]');
  return card ? card.textContent.replace(/\s+/g, ' ').trim().slice(0, 700) : null;
});
log('metrics_dom', { text: metricsTxt });
ev.run_summary_from_api = {
  net_return_pct: run.net_return_pct,
  max_drawdown_pct: run.max_drawdown_pct,
  trades_count: run.trades_count,
};
await page.screenshot({ path: `${OUT}/01b_首屏_新run结果_${W}.png` });

// ── 4. 资金分析（③）──
const fund = page.locator('[data-testid="lab-funding-panel"]');
await fund.scrollIntoViewIfNeeded();
await page.waitForTimeout(1200);
ev.funding = await page.evaluate(() => {
  const p = document.querySelector('[data-testid="lab-funding-panel"]');
  const pr = document.querySelector('[data-testid="lab-period-return"]');
  return { text: p?.textContent.replace(/\s+/g, ' ').trim().slice(0, 900) ?? null, periodReturn: pr?.textContent.replace(/\s+/g, ' ').trim() ?? null };
});
log('funding', { periodReturn: ev.funding.periodReturn });
await page.screenshot({ path: `${OUT}/02_资金分析_曲线与月度.png` });

// ── 5. 交易选中 → 图表定位 + 同 trade_id 详情（⑤④）──
await page.locator('[data-testid="lab-trades"]').scrollIntoViewIfNeeded();
await page.waitForTimeout(600);
const row = page.locator('tr[data-trade-id]').first();
const tradeId = await row.getAttribute('data-trade-id');
await row.click();
await page.waitForTimeout(1800);
const tradeInfo = await page.evaluate(() => {
  const det = document.querySelector('[data-testid="lab-trade-detail"]');
  const chart = document.querySelector('[data-testid="lab-chart-panel"]');
  return {
    detail: det?.textContent.replace(/\s+/g, ' ').trim().slice(0, 500) ?? null,
    chartFooter: chart?.lastElementChild?.textContent.replace(/\s+/g, ' ').trim().slice(0, 400) ?? null,
  };
});
ev.trade = { trade_id: tradeId, ...tradeInfo };
log('trade_selected', { trade_id: tradeId, chartFooter: tradeInfo.chartFooter });
await page.screenshot({ path: `${OUT}/03_交易选中_图表定位与详情.png` });

// ── 6. 用真实 API 核对页值 ──
const api = async (p) => (await fetch(`http://127.0.0.1:8787/api/v1/backtests/${run.run_id}${p}`)).json();
const [m, eq, tr, q] = await Promise.all([api('/metrics'), api('/equity'), api('/trades'), api('/quality')]);
const tradeRow = (tr.items ?? []).find((t) => t.trade_id === tradeId) ?? null;
ev.cross_check = {
  metrics_net_return_pct: m.metrics?.net_return_pct ?? null,
  metrics_max_drawdown_pct: m.metrics?.max_drawdown_pct ?? null,
  metrics_initial_equity: m.metrics?.initial_equity ?? null,
  monthly_returns: m.monthly_returns ?? null,
  equity_points: (eq.items ?? []).length,
  trades: (tr.items ?? []).length,
  trade_row: tradeRow,
  quality_status: q.quality?.status ?? null,
  data_quality_present: q.data_quality != null,
  hashes_present: q.hashes != null,
};
log('cross_check', { net: ev.cross_check.metrics_net_return_pct, dd: ev.cross_check.metrics_max_drawdown_pct, trades: ev.cross_check.trades, quality: ev.cross_check.quality_status });

ev.apiCalls = apiCalls;
fs.writeFileSync(`${OUT}/acceptance_evidence_${W}.json`, JSON.stringify(ev, null, 2));
console.log('saved:', `${OUT}/acceptance_evidence_${W}.json`);
await browser.close();
