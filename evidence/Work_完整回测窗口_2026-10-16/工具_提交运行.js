// WORK-05：驱动 UI 提交一次真实回测，验证「提交/运行/失败」路径并截图。
const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

const OUT = process.env.OUT_DIR || '/tmp/wb05_shots';
const BASE = process.env.BASE_URL || 'http://127.0.0.1:3010';
const SYMBOL = process.env.SYMBOL || 'BTCUSDT';
const WSTART = process.env.WSTART || '2026-10-01T00:00';
const WEND = process.env.WEND || '2026-10-08T00:00';
fs.mkdirSync(OUT, { recursive: true });

(async () => {
  const browser = await chromium.launch();
  const page = await (await browser.newContext({ viewport: { width: 1680, height: 1050 } })).newPage();
  page.on('response', (r) => {
    const u = r.url();
    if (u.includes('/api/v1/backtests') && r.request().method() !== 'GET') console.log(`  [net] ${r.request().method()} ${r.status()} ${u.slice(-60)}`);
  });
  await page.goto(BASE + '/', { waitUntil: 'load', timeout: 60000 });
  await page.waitForTimeout(5000);
  await page.getByText('回测复盘', { exact: true }).first().click();
  await page.waitForTimeout(13000);

  // 填写表单（真实控件）
  const sym = page.locator('[data-testid="lab-symbol-input"]').first();
  await sym.fill(SYMBOL);
  await page.locator('[data-testid="lab-window-start"]').first().fill(WSTART);
  await page.locator('[data-testid="lab-window-end"]').first().fill(WEND);
  await page.waitForTimeout(800);
  await page.screenshot({ path: path.join(OUT, 'submit_01_form.png'), fullPage: false });

  // 记录提交前的实验条数
  const beforeText = await page.locator('[data-testid="lab-runs"]').first().innerText().catch(() => '');
  const beforeN = (beforeText.match(/共\s*(\d+)\s*条/) || [])[1] ?? '?';

  // 点击运行
  await page.locator('[data-testid="lab-run-button"]').first().click();
  await page.waitForTimeout(1500);
  await page.screenshot({ path: path.join(OUT, 'submit_02_submitted.png'), fullPage: false });

  // 轮询直到进入终态（按钮文字从「运行中」变回「运行回测」）
  let status = '';
  for (let i = 0; i < 40; i++) {
    await page.waitForTimeout(3000);
    const prog = await page.locator('[data-testid="lab-run-progress"]').first().innerText().catch(() => '');
    status = prog;
    console.log(`  [poll ${i}] ${prog.replace(/\s+/g, ' ').slice(0, 120)}`);
    if (prog.includes('已进入终态')) break;
  }
  const runId = (status.match(/(bt_[0-9A-Za-z_]+)/) || [])[1] || null;
  console.log('RUN_ID =', runId, '| before count =', beforeN);
  await page.waitForTimeout(9000);
  await page.screenshot({ path: path.join(OUT, 'submit_03_result.png') });
  await page.screenshot({ path: path.join(OUT, 'submit_03_result_vp.png'), fullPage: false });

  // 失败路径：窗口跨度 < 1 天 ⇒ 前端拦截（不提交）
  await page.locator('[data-testid="lab-window-end"]').first().fill('2026-10-01T06:00');
  await page.waitForTimeout(600);
  await page.screenshot({ path: path.join(OUT, 'submit_04_form_blocked.png'), fullPage: false });
  const errBox = await page.locator('[data-testid="lab-form-errors"]').first().innerText().catch(() => '(无)');
  console.log('前端校验提示：', errBox.replace(/\s+/g, ' ').slice(0, 200));

  console.log('DONE runId=' + runId);
  await browser.close();
})().catch((e) => { console.error('FAIL:', e); process.exit(1); });
