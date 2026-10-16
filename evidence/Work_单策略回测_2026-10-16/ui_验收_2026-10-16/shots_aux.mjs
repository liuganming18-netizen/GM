/** 辅助证据：审计折叠区展开后、⑥ 实验记录 + 质量报告 的可读性与内容。 */
import pw from '/Users/boos/.workbuddy-ai/binaries/node/workspace/node_modules/playwright/index.js';
const { chromium } = pw;
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 1100 }, deviceScaleFactor: 2 });
await page.goto('http://localhost:3000/', { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(1800);
await page.getByText('回测复盘', { exact: true }).first().click();
await page.waitForFunction(() => document.querySelector('[data-testid="lab-metrics-loading"]') === null, null, { timeout: 60000 });
await page.waitForTimeout(1500);

// 展开 ① 的运行配置与审计详情
await page.locator('[data-testid="lab-audit-details"] summary').click();
await page.waitForTimeout(500);
await page.locator('[data-testid="lab-config"]').screenshot({ path: './04_审计详情_展开.png' });

// 展开 结果身份 的 hash 明细
await page.locator('[data-testid="lab-run-audit"] summary').click();
await page.waitForTimeout(500);
await page.locator('[data-testid="lab-run-identity"]').screenshot({ path: './05_结果身份_hash展开.png' });

// ⑥ 实验记录 + 质量报告
const runs = page.locator('[data-testid="lab-runs"]');
await runs.scrollIntoViewIfNeeded();
await page.waitForTimeout(1200);
await runs.screenshot({ path: './06_实验记录与质量报告.png' });
console.log(await page.evaluate(() => {
  const q = document.querySelector('[data-testid="lab-quality-verdict"]');
  const runs = document.querySelector('[data-testid="lab-runs"]');
  return JSON.stringify({ verdict: q?.textContent.replace(/\s+/g, ' ').trim().slice(0, 300), runsHead: runs?.textContent.replace(/\s+/g, ' ').trim().slice(0, 260) });
}));
await browser.close();
