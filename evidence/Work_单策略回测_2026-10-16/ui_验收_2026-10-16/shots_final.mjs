/**
 * 最终三张截图（不新建 run，直接看已提交的 run 结果）：
 *   01 首屏（配置压缩后 + 收益指标可见）
 *   02 资金分析（官方 LineSeries/AreaSeries 曲线 + 复合 YTD 月度）
 *   03 交易选中（图表 setViewport 定位 + 同 trade_id 详情）
 * 同时输出定位范围 / trade_id / 复合口径核对文本作为证据。
 */
import pw from '/Users/boos/.workbuddy-ai/binaries/node/workspace/node_modules/playwright/index.js';
import fs from 'node:fs';
const { chromium } = pw;

const OUT = process.argv[2] ?? '.';
const ev = { at: new Date().toISOString(), shots: [] };
const browser = await chromium.launch();

async function gotoWorkbench(page) {
  await page.goto('http://localhost:3000/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1800);
  await page.getByText('回测复盘', { exact: true }).first().click();
  // 等指标真正落数（loading 占位消失），避免把「读取中」拍成「—」
  await page.waitForFunction(
    () => document.querySelector('[data-testid="lab-metrics-loading"]') === null
      && (document.querySelector('[data-testid="lab-metrics"]')?.textContent ?? '').includes('净收益率'),
    null,
    { timeout: 60000 },
  );
  await page.waitForTimeout(1500);
}

/* ── 1280 宽：三张主图 ── */
{
  const page = await browser.newPage({ viewport: { width: 1280, height: 1000 }, deviceScaleFactor: 2 });
  await gotoWorkbench(page);
  ev.run_id = await page.textContent('[data-testid="lab-metrics"] .font-mono').catch(() => null);
  ev.metrics = await page.evaluate(() => {
    const cards = [...document.querySelectorAll('[data-testid="lab-metrics"] .grid > div')].map((d) => d.textContent.replace(/\s+/g, ' ').trim());
    return { cards, raw: document.querySelector('[data-testid="lab-metrics"]')?.textContent.replace(/\s+/g, ' ').trim().slice(0, 800) };
  });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${OUT}/01_首屏_配置与指标.png` });
  ev.shots.push('01_首屏_配置与指标.png');

  const fund = page.locator('[data-testid="lab-funding-panel"]');
  await fund.scrollIntoViewIfNeeded();
  await page.waitForTimeout(1500);
  ev.funding = await page.evaluate(() => ({
    periodReturn: document.querySelector('[data-testid="lab-period-return"]')?.textContent.replace(/\s+/g, ' ').trim() ?? null,
    canvases: document.querySelectorAll('[data-testid="lab-funding-chart"] canvas').length,
    monthly: document.querySelector('[data-testid="lab-funding-panel"]')?.textContent.replace(/\s+/g, ' ').match(/月度收益热力矩阵[\s\S]{0,320}/)?.[0] ?? null,
  }));
  await page.screenshot({ path: `${OUT}/02_资金分析_曲线与月度.png` });
  ev.shots.push('02_资金分析_曲线与月度.png');

  await page.locator('[data-testid="lab-trades"]').scrollIntoViewIfNeeded();
  await page.waitForTimeout(500);
  const row = page.locator('tr[data-trade-id]').first();
  const tid = await row.getAttribute('data-trade-id');
  await row.click();
  await page.waitForTimeout(2000);
  ev.trade = await page.evaluate((tid) => {
    const chart = document.querySelector('[data-testid="lab-chart-panel"]');
    const det = document.querySelector('[data-testid="lab-trade-detail"]');
    return {
      clicked_trade_id: tid,
      chartFooter: chart?.lastElementChild?.textContent.replace(/\s+/g, ' ').trim() ?? null,
      detailHead: det?.textContent.replace(/\s+/g, ' ').trim().slice(0, 320) ?? null,
      chartCanvas: chart?.querySelectorAll('canvas').length ?? 0,
    };
  }, tid);
  await page.locator('[data-testid="lab-chart-panel"]').scrollIntoViewIfNeeded();
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${OUT}/03_交易选中_图表定位与详情.png` });
  ev.shots.push('03_交易选中_图表定位与详情.png');
  await page.close();
}

/* ── 984 宽（用户报告的问题宽度）：首屏 ── */
{
  const page = await browser.newPage({ viewport: { width: 984, height: 900 }, deviceScaleFactor: 2 });
  await gotoWorkbench(page);
  ev.at984 = await page.evaluate(() => {
    const r = (s) => { const e = document.querySelector(s); return e ? { top: Math.round(e.getBoundingClientRect().top), h: Math.round(e.getBoundingClientRect().height) } : null; };
    return { cfg: r('[data-testid="lab-config"]'), metrics: r('[data-testid="lab-metrics"]'), vh: window.innerHeight };
  });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${OUT}/01b_首屏_984宽.png` });
  ev.shots.push('01b_首屏_984宽.png');
  await page.close();
}

fs.writeFileSync(`${OUT}/screenshots_evidence.json`, JSON.stringify(ev, null, 2));
console.log(JSON.stringify({ at984: ev.at984, periodReturn: ev.funding?.periodReturn, canvases: ev.funding?.canvases, trade: ev.trade?.clicked_trade_id, chartFooter: ev.trade?.chartFooter }, null, 1));
await browser.close();
