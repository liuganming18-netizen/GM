import pw from '/Users/boos/.workbuddy-ai/binaries/node/workspace/node_modules/playwright/index.js';
const { chromium } = pw;
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 1000 }, deviceScaleFactor: 1 });
await page.goto('http://localhost:3000/', { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(1800);
await page.getByText('回测复盘', { exact: true }).first().click();
await page.waitForFunction(() => document.querySelector('[data-testid="lab-metrics-loading"]') === null, null, { timeout: 60000 });
await page.waitForTimeout(6000);
console.log(await page.evaluate(() => {
  const ids = ['lab-workbench','lab-config','lab-metrics','lab-run-identity','lab-funding-panel','lab-funding-chart','lab-chart-panel','lab-trades','lab-runs','lab-quality-panel'];
  const o = {};
  for (const id of ids) { const e = document.querySelector(`[data-testid="${id}"]`); o[id] = e ? Math.round(e.getBoundingClientRect().height) : 'MISSING'; }
  o.textEls = [...document.querySelectorAll('[data-testid="lab-workbench"] *')].filter((el) => [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())).length;
  o.tradeRows = document.querySelectorAll('tr[data-trade-id]').length;
  o.runRows = document.querySelectorAll('tr[data-run-id]').length;
  o.fundCanvas = document.querySelectorAll('[data-testid="lab-funding-chart"] canvas').length;
  o.chartCanvas = document.querySelectorAll('[data-testid="lab-chart-panel"] canvas').length;
  return JSON.stringify(o);
}));
await browser.close();
