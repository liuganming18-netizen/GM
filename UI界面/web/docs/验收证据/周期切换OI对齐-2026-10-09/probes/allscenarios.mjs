/**
 * 周期切换 OI/K 线对齐 · 全场景可复查探针（只读，不改工程源码）。
 *
 * 观测：仅在测试浏览器内拦截 Vite 预打包的 lightweight-charts，把 createChart
 * 实例暴露到 window.__LWC__，从而调用官方 API 做**逐点像素级**测量：
 *   timeScale().getVisibleRange()        → 可见真实 UTC 时间范围
 *   timeScale().getVisibleLogicalRange() → 可见逻辑范围（两图点集相同 ⇒ 可直接比较）
 *   timeScale().timeToCoordinate(t)      → 同一 UTC 时间点在两图上的横坐标
 *
 * 用法：OUT_PREFIX=before node allscenarios.mjs
 */
import { open, expandRow, collapseRow } from './harness.mjs';
import fs from 'node:fs';

const TOKEN = process.env.TOKEN || 'binance:BTCUSDT';
const PREFIX = process.env.OUT_PREFIX || 'run';
const SHOT_DIR = `/tmp/oi_fix/shots_${PREFIX}`;
const OUT = `/tmp/oi_fix/${PREFIX}_scenarios.json`;
fs.mkdirSync(SHOT_DIR, { recursive: true });

const INJECT = `
;(function(){
  try {
    const __orig = createChart;
    function __wrapped(container, options){
      const c = __orig(container, options);
      const label = (container && container.getAttribute && container.getAttribute('data-chart')) || 'unknown';
      c.__abLabel = label;
      (globalThis.__LWC__ || (globalThis.__LWC__ = [])).push(c);
      return c;
    }
    createChart = __wrapped;
  } catch (e) { globalThis.__LWC_INJECT_ERR__ = String(e); }
})();
`;

const MEASURE = ({ candidates }) => {
  const labelOf = (c) => { try { return c.__abLabel || 'unknown'; } catch { return 'unknown'; } };
  const charts = (globalThis.__LWC__ || []).filter((c) => {
    try { const el = c.chartElement(); return !!el && document.contains(el); } catch { return false; }
  });
  const rows = [];
  for (const c of charts) {
    let range = null, lr = null, paneW = null, psW = null, bs = null, cw = null;
    try { range = c.timeScale().getVisibleRange(); } catch {}
    try { lr = c.timeScale().getVisibleLogicalRange(); } catch {}
    try { paneW = c.timeScale().width(); } catch {}
    try { psW = c.priceScale('right').width(); } catch {}
    try { bs = c.timeScale().options().barSpacing; } catch {}
    try { cw = c.paneSize().width; } catch {}
    rows.push({ label: labelOf(c), range, logical: lr, paneW, psW, barSpacing: bs, paneWidth: cw });
  }
  const chartC = charts.find((c) => labelOf(c) === 'lightweight-candles');
  const chartO = charts.find((c) => labelOf(c) === 'lightweight-oi');
  let align = null;
  if (chartC && chartO) {
    const xs = [];
    for (const t of candidates) {
      let xc = null, xo = null;
      try { xc = chartC.timeScale().timeToCoordinate(t); } catch {}
      try { xo = chartO.timeScale().timeToCoordinate(t); } catch {}
      if (xc === null || xo === null) continue;
      xs.push({ t, xc: +xc.toFixed(3), xo: +xo.toFixed(3), d: +Math.abs(xc - xo).toFixed(3) });
    }
    const ds = xs.map((r) => r.d);
    const lc = rows.find((r) => r.label === 'lightweight-candles');
    const lo = rows.find((r) => r.label === 'lightweight-oi');
    const rangeEqual = !!(lc && lo && lc.range && lo.range && lc.range.from === lo.range.from && lc.range.to === lo.range.to);
    const logicalEqual = !!(lc && lo && lc.logical && lo.logical &&
      Math.abs(lc.logical.from - lo.logical.from) < 1e-9 && Math.abs(lc.logical.to - lo.logical.to) < 1e-9);
    align = {
      n: xs.length,
      rangeEqual, logicalEqual,
      barSpacingCandle: lc ? lc.barSpacing : null,
      barSpacingOi: lo ? lo.barSpacing : null,
      maxAbsDiff: ds.length ? +Math.max(...ds).toFixed(3) : null,
      meanAbsDiff: ds.length ? +(ds.reduce((a, b) => a + b, 0) / ds.length).toFixed(3) : null,
      over1px: ds.filter((d) => d > 1).length,
      samples: xs.filter((_, i) => i % Math.max(1, Math.floor(xs.length / 6)) === 0).slice(0, 8),
    };
  }
  return { rows, align };
};

async function candidatesFor(tf) {
  const base = 'http://127.0.0.1:8787/api/v1/markets/binance/BTCUSDT';
  const out = new Set();
  try {
    const oi = await (await fetch(base + '/oi')).json();
    for (const it of oi.items || []) if (it.event_time) out.add(Math.floor(Date.parse(it.event_time) / 1000));
  } catch {}
  for (const lim of [60, 500]) {
    try {
      const c = await (await fetch(`${base}/candles?timeframe=${tf}&limit=${lim}&no_rest_tail=1`)).json();
      for (const b of c.bars || []) { const t = b.open_time || b.time; if (t) out.add(Math.floor(Date.parse(t) / 1000)); }
    } catch {}
  }
  return [...out].sort((a, b) => a - b);
}

const results = [];
async function record(label, tf, { shot = true } = {}) {
  await page.waitForTimeout(900);
  const cand = await candidatesFor(tf);
  const m = await page.evaluate(MEASURE, { candidates: cand });
  const dom = await page.evaluate((id) => {
    const row = document.querySelector(`tr[data-token-id="${id}"]`);
    let exp = row && row.nextElementSibling;
    if (!(exp && exp.querySelector('[data-chart="lightweight-candles"]'))) exp = null;
    if (!exp) return { found: false };
    const txt = exp.innerText;
    const g = (re) => { const x = txt.match(re); return x ? x[1].trim() : null; };
    const btns = Array.from(exp.querySelectorAll('button')).filter((b) => /^(1m|5m|15m|30m|1h|4h|8h|12h|1d)$/.test((b.textContent || '').trim()));
    const host = exp.querySelector('[data-chart="lightweight-candles"]');
    const oiHost = exp.querySelector('[data-chart="lightweight-oi"]');
    const rect = (el) => { if (!el) return null; const r = el.getBoundingClientRect(); return { left: Math.round(r.left), top: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) }; };
    return {
      found: true,
      selectedTf: btns.filter((b) => /emerald/.test(b.className)).map((b) => b.textContent.trim())[0] || null,
      viewport: g(/视窗\s*([^·]+)/),
      closedLast: g(/闭合末根\s*([^\s·]+)/),
      forming: g(/未收盘层\s*([^\s·]+)/),
      loaded: g(/本地已载入\s*([^·]+)/) || g(/当前已载入\s*([^·]+)/),
      oiTime: g(/OI时点:\s*([^\s]+)/),
      oiUnitText: g(/总OI:\s*([^\n]+)/),
      candleRect: rect(host),
      oiRect: rect(oiHost),
    };
  }, TOKEN);
  let shotPath = null;
  if (shot) {
    shotPath = `${SHOT_DIR}/${label}.png`;
    await page.screenshot({ path: shotPath });
    const host = await page.$(`[data-chart="lightweight-candles"]`);
    if (host) {
      const box = await host.boundingBox();
      const oiHost = await page.$(`[data-chart="lightweight-oi"]`);
      const oiBox = oiHost ? await oiHost.boundingBox() : null;
      if (box && oiBox) {
        await page.screenshot({ path: `${SHOT_DIR}/${label}__charts.png`, clip: { x: box.x, y: box.y, width: box.width, height: (oiBox.y + oiBox.height) - box.y } });
      }
    }
  }
  const rec = { label, tf, dom, measure: m, shot: shotPath };
  results.push(rec);
  const a = m.align;
  console.log(`[${label}] tf=${dom.selectedTf} viewport=${dom.viewport}`);
  console.log(`   candle range=${JSON.stringify(m.rows.find((r) => r.label === 'lightweight-candles')?.range)} logical=${JSON.stringify(m.rows.find((r) => r.label === 'lightweight-candles')?.logical)} bs=${a?.barSpacingCandle}`);
  console.log(`   oi     range=${JSON.stringify(m.rows.find((r) => r.label === 'lightweight-oi')?.range)} logical=${JSON.stringify(m.rows.find((r) => r.label === 'lightweight-oi')?.logical)} bs=${a?.barSpacingOi}`);
  console.log(`   ALIGN: rangeEqual=${a?.rangeEqual} logicalEqual=${a?.logicalEqual} n=${a?.n} maxDiff=${a?.maxAbsDiff} meanDiff=${a?.meanAbsDiff} over1px=${a?.over1px}`);
  return rec;
}

const clickTf = (tf) => page.evaluate(({ id, tf }) => {
  const row = document.querySelector(`tr[data-token-id="${id}"]`);
  const exp = row && row.nextElementSibling;
  const btn = Array.from(exp.querySelectorAll('button')).find((b) => (b.textContent || '').trim() === tf);
  if (btn) btn.click();
}, { id: TOKEN, tf });

const clickText = (text) => page.evaluate(({ id, text }) => {
  const row = document.querySelector(`tr[data-token-id="${id}"]`);
  const exp = row && row.nextElementSibling;
  const btn = Array.from(exp.querySelectorAll('button')).find((b) => (b.textContent || '').trim().includes(text));
  if (btn) btn.click();
  return !!btn;
}, { id: TOKEN, text });

async function centerOf(sel) {
  const el = await page.$(sel);
  if (!el) return null;
  const b = await el.boundingBox();
  return b ? { x: b.x + b.width / 2, y: b.y + b.height / 2, box: b } : null;
}

const { browser, page, errors } = await open({ navigate: false });
await page.route('**/deps/lightweight-charts.js*', async (route) => {
  const res = await route.fetch();
  const body = await res.text();
  await route.fulfill({ response: res, body: body + INJECT, headers: { ...res.headers(), 'content-type': 'text/javascript' } });
});
await page.goto('http://127.0.0.1:3000/', { waitUntil: 'domcontentloaded', timeout: 60000 });
await page.waitForTimeout(9000);
// 搜索框匹配的是展示简称（搜 BTCUSDT 会 0 结果），故用简称定位目标行
{
  const input = page.locator('input[type="text"]').first();
  await input.waitFor({ state: 'visible', timeout: 20000 });
  await input.click();
  await input.fill('BTC');
  await page.waitForTimeout(1500);
}
await expandRow(page, TOKEN);
await page.waitForTimeout(3000);
console.log('INJECTED=', await page.evaluate(() => (globalThis.__LWC__ || []).length));

await record('01_initial_1m', '1m');

for (const tf of ['1m', '5m', '15m', '30m', '1h', '4h', '8h', '12h', '1d']) {
  await clickTf(tf);
  await record(`02_tf_${tf}`, tf);
}

await clickTf('30m'); await record('03_30m', '30m');
await clickTf('1h'); await record('04_1h', '1h');
await clickTf('30m'); await record('05_back_30m', '30m');

// 快速往返
for (const tf of ['1h', '30m', '1h', '30m', '1h', '30m', '1h', '30m']) { await clickTf(tf); await page.waitForTimeout(120); }
await record('06_rapid_after', '30m');

// 历史分页后切回
const paged = await clickText('加载更早历史');
await page.waitForTimeout(2500);
await record('07_after_pagination_30m', '30m');
await clickTf('1h'); await record('08_page_1h', '1h');
await clickTf('30m'); await record('09_page_back_30m', '30m');

// 两图主动缩放/平移
const cc = await centerOf('[data-chart="lightweight-candles"]');
if (cc) {
  await page.mouse.move(cc.x, cc.y);
  await page.mouse.wheel(0, -240);           // 放大
  await record('10_wheel_zoom_candle', '30m');
  await page.mouse.move(cc.x, cc.y);
  await page.mouse.down();
  await page.mouse.move(cc.x - 180, cc.y, { steps: 12 });
  await page.mouse.up();
  await record('11_drag_candle', '30m');
}
const oc = await centerOf('[data-chart="lightweight-oi"]');
if (oc) {
  await page.mouse.move(oc.x, oc.y);
  await page.mouse.wheel(0, -240);
  await record('12_wheel_zoom_oi', '30m');
  await page.mouse.move(oc.x, oc.y);
  await page.mouse.down();
  await page.mouse.move(oc.x + 140, oc.y, { steps: 12 });
  await page.mouse.up();
  await record('13_drag_oi', '30m');
}

// OI 单位切换
await clickText('名义额 USD');
await record('14_oi_unit_usd', '30m');
await clickText('数量');
await record('15_oi_unit_amount', '30m');

// 收起 → 再展开
const collapsed = await collapseRow(page, TOKEN);
console.log('  collapse clicked =', collapsed);
await page.waitForTimeout(1200);
await expandRow(page, TOKEN);
await record('16_collapse_expand_30m', '30m');

// 刷新页面
await page.reload({ waitUntil: 'domcontentloaded' });
await page.waitForTimeout(9000);
await expandRow(page, TOKEN);
await record('17_after_reload', '30m');

fs.writeFileSync(OUT, JSON.stringify({ token: TOKEN, prefix: PREFIX, errors, results }, null, 2));
console.log('ERRORS', JSON.stringify(errors.slice(0, 6)));
console.log('WROTE', OUT);
await browser.close();
