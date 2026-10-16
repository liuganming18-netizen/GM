/**
 * 回测页截图工具（只读，不改任何状态）。
 * 用法：node scripts/shot_backtest.mjs <输出目录> <标签>
 */
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';

const OUT = process.argv[2] || '/tmp/bt_shots';
const TAG = process.argv[3] || 'shot';
fs.mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1680, height: 1050 }, deviceScaleFactor: 1 });
page.on('console', (m) => {
  if (m.type() === 'error') console.log('[console.error]', m.text().slice(0, 300));
});

await page.goto('http://127.0.0.1:3000/', { waitUntil: 'networkidle' });
await page.waitForTimeout(1500);

// 进入「回测复盘」
await page.getByRole('button', { name: '回测复盘' }).click();
await page.waitForTimeout(1200);

const shot = async (name, opts = {}) => {
  const p = path.join(OUT, `${TAG}_${name}.png`);
  await page.screenshot({ path: p, fullPage: false, ...opts });
  console.log('saved', p);
};

await shot('01_initial');

// 选中第一条 run（若有）
const firstRun = page.locator('[data-run-id]').first();
if (await firstRun.count()) {
  await firstRun.scrollIntoViewIfNeeded();
  await firstRun.click();
  await page.waitForTimeout(2500);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(600);
  await shot('02_run_selected');
  await shot('03_fullpage', { fullPage: true });
  // 逐屏
  const n = await page.evaluate(() => {
    const s = document.querySelector('[data-backtest-scroll]');
    return Math.min(9, Math.ceil(s.scrollHeight / s.clientHeight));
  });
  for (let i = 1; i < n; i++) {
    await page.evaluate((i) => {
      const s = document.querySelector('[data-backtest-scroll]');
      s.scrollTo({ top: i * (s.clientHeight - 60) });
    }, i);
    await page.waitForTimeout(450);
    await shot(`04_scroll_${String(i).padStart(2, '0')}`);
  }
} else {
  console.log('no run rows found');
}

// 验收复盘 pane
await page.evaluate(() => window.scrollTo(0, 0));
await page.getByRole('tab', { name: '验收复盘' }).click();
await page.waitForTimeout(1500);
await shot('05_review');
await shot('06_review_full', { fullPage: true });

await browser.close();
console.log('done');
