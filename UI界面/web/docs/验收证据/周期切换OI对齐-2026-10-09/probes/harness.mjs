/**
 * 复用测试骨架（只读探针，不改工程源码）。
 * 用 playwright-core + 本机 Chrome for Testing 打开真实页面 3000。
 */
import pw from '/Users/boos/.workbuddy-ai/binaries/node/workspace/node_modules/playwright-core/index.js';

export const EXEC =
  '/Users/boos/.agent-browser/browsers/chrome-154.0.8037.92/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing';

export async function open({ width = 1800, height = 1180, url = 'http://127.0.0.1:3000/', navigate = true } = {}) {
  const browser = await pw.chromium.launch({
    executablePath: EXEC,
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--force-device-scale-factor=1'],
  });
  const ctx = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  const errors = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', (e) => errors.push('PAGEERROR: ' + e.message));
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
  return { browser, ctx, page, errors };
}

/** 展开指定 token 行（若未展开）。注意行内第 0 个按钮是「查看来源与口径」，展开按钮文案是「展开K线」。 */
export async function expandRow(page, tokenId) {
  const row = page.locator(`tr[data-token-id="${tokenId}"]`);
  await row.waitFor({ state: 'attached', timeout: 30000 });
  await row.scrollIntoViewIfNeeded();
  const expanded = await page.evaluate((id) => {
    const r = document.querySelector(`tr[data-token-id="${id}"]`);
    const nx = r && r.nextElementSibling;
    return !!(nx && nx.querySelector('[data-chart="lightweight-candles"]'));
  }, tokenId);
  if (!expanded) {
    const clicked = await page.evaluate((id) => {
      const r = document.querySelector(`tr[data-token-id="${id}"]`);
      const b = r && Array.from(r.querySelectorAll('button')).find((x) => (x.textContent || '').includes('展开K线'));
      if (b) { b.click(); return true; }
      return false;
    }, tokenId);
    if (!clicked) throw new Error(`未找到展开按钮: ${tokenId}`);
    await page.waitForTimeout(2000);
  }
  return tokenId;
}

/** 收起指定 token 行（按钮文案「收起」）。 */
export async function collapseRow(page, tokenId) {
  const clicked = await page.evaluate((id) => {
    const r = document.querySelector(`tr[data-token-id="${id}"]`);
    const b = r && Array.from(r.querySelectorAll('button')).find((x) => (x.textContent || '').trim() === '收起');
    if (b) { b.click(); return true; }
    return false;
  }, tokenId);
  await page.waitForTimeout(1500);
  return clicked;
}

/** 在页面里读取「目标展开行」的全部可读状态。 */
export const READ_STATE = (tokenId) => {
  const row = document.querySelector(`tr[data-token-id="${tokenId}"]`);
  let exp = row && row.nextElementSibling;
  if (!(exp && exp.querySelector('[data-chart="lightweight-candles"]'))) {
    exp = null;
    // 兜底：直接找页面上第一个展开图
    const all = Array.from(document.querySelectorAll('[data-chart="lightweight-candles"]'));
    if (all.length) exp = all[0].closest('div.bg-neutral-950');
  }
  if (!exp) return { found: false };
  const txt = exp.innerText;
  const pick = (re) => { const m = txt.match(re); return m ? m[1].trim() : null; };
  const tfButtons = Array.from(exp.querySelectorAll('button'))
    .filter((b) => /^(1m|5m|15m|30m|1h|4h|8h|12h|1d)$/.test((b.textContent || '').trim()));
  const selectedTf = tfButtons.filter((b) => /emerald/.test(b.className)).map((b) => b.textContent.trim())[0] || null;

  const geom = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const canvases = Array.from(el.querySelectorAll('canvas')).map((c) => {
      const cr = c.getBoundingClientRect();
      return { w: Math.round(cr.width), h: Math.round(cr.height), left: Math.round(cr.left), top: Math.round(cr.top) };
    });
    return { left: Math.round(r.left), top: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height), canvases };
  };
  const candleHost = exp.querySelector('[data-chart="lightweight-candles"]');
  const oiHost = exp.querySelector('[data-chart="lightweight-oi"]');
  return {
    found: true,
    selectedTf,
    timeframes: tfButtons.map((b) => b.textContent.trim()),
    viewport: pick(/视窗\s*([^·]+)/),
    closedLast: pick(/闭合末根\s*([^\s·]+)/),
    forming: pick(/未收盘层\s*([^\s·]+)/),
    loaded: pick(/本地已载入\s*([^·]+)/) || pick(/当前已载入\s*([^·]+)/),
    gapText: pick(/闭合段[^·]*/),
    oiTime: pick(/OI时点:\s*([^\s]+)/),
    totalOi: pick(/总OI:\s*([^\n]+)/),
    source: pick(/数据源:\s*(.*)$/),
    candle: geom(candleHost),
    oi: geom(oiHost),
  };
};

/** 点击周期按钮（真实鼠标点击）。 */
export async function clickTf(page, tokenId, tf) {
  await page.evaluate(({ id, tf }) => {
    const row = document.querySelector(`tr[data-token-id="${id}"]`);
    const exp = row && row.nextElementSibling;
    const btn = Array.from(exp.querySelectorAll('button')).find((b) => (b.textContent || '').trim() === tf);
    if (btn) btn.click();
  }, { id: tokenId, tf });
}
