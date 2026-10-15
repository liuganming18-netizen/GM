const PW = '/Users/boos/.workbuddy-ai/binaries/node/workspace/node_modules/playwright-core/index.js';
const pw = await import(PW);
const chromium = pw.chromium ?? pw.default?.chromium;
const browser = await chromium.connectOverCDP(process.env.CDP_URL);
const ctx = browser.contexts()[0];
const page = ctx.pages().find((p) => p.url().includes('127.0.0.1:3000'));
await page.bringToFront();
const cdp = await page.context().newCDPSession(page);
await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1800, height: 1180, deviceScaleFactor: 1, mobile: false });
const S = 'div.overflow-auto, div[class*="overflow-y"]';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const spacers = () =>
  page.evaluate((sel) => {
    const a = [...document.querySelectorAll(sel)];
    const e = a.find((x) => x.scrollHeight > x.clientHeight + 50) || a[0];
    const trs = [...document.querySelectorAll('tbody tr')];
    const sp = trs
      .filter((tr) => tr.getAttribute('aria-hidden') === 'true')
      .map((tr) => {
        const td = tr.querySelector('td');
        return { inline: td?.style?.height || null, rect: Math.round(tr.getBoundingClientRect().height) };
      });
    const dataRows = trs.filter((tr) => tr.getAttribute('data-token-id'));
    const realH = dataRows.reduce((s, tr) => s + tr.getBoundingClientRect().height, 0);
    return {
      scrollH: e.scrollHeight,
      clientH: e.clientHeight,
      tbodyRows: trs.length,
      spacerCount: sp.length,
      spacers: sp,
      spacerSumRect: sp.reduce((s, x) => s + x.rect, 0),
      dataRowCount: dataRows.length,
      dataRowSumRect: Math.round(realH),
      tbodyRect: Math.round(document.querySelector('tbody').getBoundingClientRect().height),
    };
  }, S);

const clickExpand = (id) => page.evaluate((i) => { const tr = document.querySelector(`tbody tr[data-token-id="${i}"]`); tr?.querySelector('button')?.click(); }, id);
const first = () => page.evaluate(() => document.querySelector('tbody tr[data-token-id]')?.getAttribute('data-token-id') ?? null);

const out = {};
await page.reload({ waitUntil: 'domcontentloaded' });
await page.waitForSelector('tbody tr[data-token-id]', { timeout: 60000 });
await sleep(2500);
out.A_初始_顶部 = await spacers();
await page.evaluate((sel) => { const a = [...document.querySelectorAll(sel)]; const e = a.find((x) => x.scrollHeight > x.clientHeight + 50) || a[0]; e.scrollTop = 1e9; }, S);
await sleep(700);
out.A2_初始_底部 = await spacers();
await page.evaluate((sel) => { const a = [...document.querySelectorAll(sel)]; const e = a.find((x) => x.scrollHeight > x.clientHeight + 50) || a[0]; e.scrollTop = 0; }, S);
await sleep(600);
const id = await first();
await clickExpand(id);
await sleep(2500);
out.B_展开后 = await spacers();
await clickExpand(id);
await sleep(2500);
out.C_收起后 = await spacers();
await cdp.send('Emulation.clearDeviceMetricsOverride').catch(() => {});
console.log(JSON.stringify(out, null, 1));
process.exit(0);
