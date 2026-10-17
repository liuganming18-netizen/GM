// WORK-05 最终截图：桌面完整 / 窄屏 / 缺失状态（验收复盘 pane）。
const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

const OUT = process.env.OUT_DIR || '/tmp/wb05_shots';
const BASE = process.env.BASE_URL || 'http://127.0.0.1:3010';
fs.mkdirSync(OUT, { recursive: true });

async function open(page, w, h) {
  await page.setViewportSize({ width: w, height: h });
  await page.goto(BASE + '/', { waitUntil: 'load', timeout: 60000 });
  await page.waitForTimeout(4000);
  await page.getByText('回测复盘', { exact: true }).first().click();
  await page.waitForTimeout(13000);
}

(async () => {
  const browser = await chromium.launch();
  const page = await (await browser.newContext({ viewport: { width: 1680, height: 1050 } })).newPage();

  // ① 桌面完整
  await open(page, 1680, 1050);
  await page.screenshot({ path: path.join(OUT, 'final_01_workbench_desktop_full.png') });
  await page.screenshot({ path: path.join(OUT, 'final_01b_workbench_desktop_viewport.png'), fullPage: false });
  console.log('shot desktop');

  // ② 验收复盘 pane（缺失状态：参数热力图/成本压测/因子消融 均显式「—」）
  const review = page.locator('[data-testid="backtest-pane-review"]').first();
  if (await review.count() > 0) {
    await review.click();
    await page.waitForTimeout(4000);
    await page.screenshot({ path: path.join(OUT, 'final_03_review_pane.png') });
    console.log('shot review pane');
  }

  // ③ 窄屏 1024
  const page2 = await (await browser.newContext({ viewport: { width: 1024, height: 800 } })).newPage();
  await open(page2, 1024, 800);
  await page2.screenshot({ path: path.join(OUT, 'final_04_narrow_1024.png') });
  console.log('shot narrow 1024');

  // ④ 更窄 768
  const page3 = await (await browser.newContext({ viewport: { width: 768, height: 900 } })).newPage();
  await open(page3, 768, 900);
  await page3.screenshot({ path: path.join(OUT, 'final_05_narrow_768.png') });
  console.log('shot narrow 768');

  await browser.close();
  console.log('done');
})().catch((e) => { console.error('FAIL:', e); process.exit(1); });
