/**
 * 回测页「一屏」验收截图（只读）。
 * 输出：01_一屏总览 / 02_展开口径与审计 / 03_展开订单 / 04_验收复盘回归
 * 同时打印各表可见列数、页面滚动比（ratio 必须 = 1，即一屏不滚动）。
 *
 * 注：④ 执行视图 K 线图自 2026-10-16 起**常驻页面上部**，不再是可折叠面板，
 *     因此本脚本不再有「展开执行视图」这一步。
 */
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';

const OUT = process.argv[2] || '/tmp/bt_onescreen';
const TAG = process.argv[3] || 'one';
fs.mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1680, height: 1050 } });
page.on('console', (m) => {
  if (m.type() === 'error') console.log('[console.error]', m.text().slice(0, 200));
});

await page.goto('http://127.0.0.1:3000/', { waitUntil: 'networkidle' });
await page.waitForTimeout(1400);
await page.getByRole('button', { name: '回测复盘' }).click();
await page.waitForTimeout(1000);

const firstRun = page.locator('[data-run-id]').first();
if (await firstRun.count()) {
  await firstRun.scrollIntoViewIfNeeded();
  await firstRun.click();
  await page.waitForTimeout(2800);
} else {
  console.log('no run rows');
}
await page.evaluate(() => {
  const s = document.querySelector('[data-backtest-scroll]');
  if (s) s.scrollTo({ top: 0 });
});
await page.waitForTimeout(500);

const shot = async (name) => {
  const p = path.join(OUT, `${TAG}_${name}.png`);
  await page.screenshot({ path: p, fullPage: false });
  console.log('saved', p);
};

await shot('01_onescreen');

// 顶部「更多指标」开关（次要指标展开后仍须一行不换行）
const moreBtn = page.locator('[data-testid="lab-metrics-more"]');
let metricsMore = null;
if (await moreBtn.count()) {
  await moreBtn.click();
  await page.waitForTimeout(600);
  metricsMore = await page.evaluate(() => {
    const el = document.querySelector('[data-testid="lab-run-context"]');
    if (!el) return null;
    const kids = Array.from(el.children);
    const cs = getComputedStyle(el);
    const padY = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
    const childMax = Math.max(...kids.map((c) => c.getBoundingClientRect().height));
    const contentRight = Math.max(...kids.map((c) => c.getBoundingClientRect().right));
    return {
      h: Math.round(el.getBoundingClientRect().height),
      wrapped: el.getBoundingClientRect().height > childMax + padY + 6,
      free: Math.round(el.getBoundingClientRect().right - contentRight - parseFloat(cs.paddingRight)),
      aria: document.querySelector('[data-testid="lab-metrics-more"]')?.getAttribute('aria-pressed'),
    };
  });
  await shot('02_expand_metrics');
  await moreBtn.click();
  await page.waitForTimeout(400);
}

const expanded = [];
for (const [key, name] of [
  ['audit', '03_expand_audit'],
  ['orders', '04_expand_orders'],
]) {
  const btn = page.locator(`[data-testid="lab-panel-${key}"]`);
  if (await btn.count()) {
    await btn.click();
    await page.waitForTimeout(1400); // 等平滑滚动落定
    const vis = await page.evaluate((k) => {
      const map = { audit: 'lab-audit', orders: 'lab-orders' };
      const el = document.querySelector(`[data-testid="${map[k]}"]`);
      const s = document.querySelector('[data-backtest-scroll]');
      if (!el || !s) return null;
      const r = el.getBoundingClientRect();
      const sr = s.getBoundingClientRect();
      return { top: Math.round(r.top - sr.top), visiblePx: Math.round(Math.min(r.bottom, sr.bottom) - Math.max(r.top, sr.top)) };
    }, key);
    expanded.push([key, vis]);
    await shot(name);
    await btn.click(); // 收起
    await page.waitForTimeout(400);
  }
}

// 回归：验收复盘 tab
await page.getByRole('tab', { name: '验收复盘' }).click();
await page.waitForTimeout(1600);
await shot('05_review_regression');

// 测量（先把滚动归零、并重新选中一条 run —— 切 tab 会重挂载组件、清空选择）
await page.getByRole('tab', { name: '完整回测工作台' }).click();
await page.waitForTimeout(1200);
const reRun = page.locator('[data-run-id]').first();
if (await reRun.count()) {
  await reRun.scrollIntoViewIfNeeded();
  await reRun.click();
  await page.waitForTimeout(2800);
}
await page.evaluate(() => document.querySelector('[data-backtest-scroll]')?.scrollTo({ top: 0 }));
await page.waitForTimeout(400);
const info = await page.evaluate(() => {
  const s = document.querySelector('[data-backtest-scroll]');
  const vis = (sel) => {
    const t = document.querySelector(sel);
    if (!t) return null;
    const ths = Array.from(t.querySelectorAll('thead th'));
    return { total: ths.length, shown: ths.filter((x) => getComputedStyle(x).display !== 'none').length };
  };
  const secs = Array.from(s.querySelectorAll('[data-lab-section]')).map((e) => [
    e.getAttribute('data-lab-section'),
    Math.round(e.getBoundingClientRect().height),
  ]);
  const chart = document.querySelector('[data-lab-section="lab-chart"]');
  const canvas = chart ? chart.querySelector('canvas') : null;
  const wb = s.firstElementChild;
  const wbTop = wb.getBoundingClientRect().top;
  const contentBottom = Math.max(
    ...Array.from(wb.children).map((c) => c.getBoundingClientRect().bottom - wbTop),
  );
  return {
    scrollH: s.scrollHeight,
    clientH: s.clientHeight,
    ratio: +(s.scrollHeight / s.clientHeight).toFixed(2),
    /* scrollHeight 下限就是 clientHeight ⇒ 真实余量必须量「内容底边 − 容器高」 */
    contentH: Math.round(contentBottom),
    slack: Math.round(s.clientHeight - contentBottom),
    sections: secs,
    chartTop: chart ? Math.round(chart.getBoundingClientRect().top - s.getBoundingClientRect().top) : null,
    chartInView: chart ? chart.getBoundingClientRect().bottom <= s.getBoundingClientRect().bottom + 1 : null,
    chartCanvas: canvas ? { w: canvas.clientWidth, h: canvas.clientHeight } : null,
    cols: {
      trades: vis('[data-testid="lab-trades"] table'),
      runs: vis('[data-testid="lab-runs"] table'),
    },
    openPanels: Array.from(document.querySelectorAll('[data-testid^="lab-panel-"]')).map((b) => [
      b.getAttribute('data-testid'),
      b.getAttribute('aria-pressed'),
    ]),
  };
});
console.log(JSON.stringify({ ...info, metricsMore, expandedPanels: expanded }, null, 1));
await browser.close();
