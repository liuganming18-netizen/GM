/**
 * 数据完整性 + 多标的验证（只读，不改工程源码）。
 *
 * 除对齐测量外，还通过拦截官方 `addSeries` 拿到**真实序列句柄**，
 * 用 `series.data()` 逐点核对：页面上的 K 线 / 成交量 / OI 柱
 * 是否与后端真实数据逐点一致（时间戳、数值、缺口、无补零/无插值/无伪造）。
 */
import { open, expandRow } from './harness.mjs';
import fs from 'node:fs';

const PREFIX = process.env.OUT_PREFIX || 'v2';
const OUT = `/tmp/oi_fix/${PREFIX}_verify.json`;
const SHOT_DIR = `/tmp/oi_fix/shots_${PREFIX}`;
fs.mkdirSync(SHOT_DIR, { recursive: true });

const INJECT = `
;(function(){
  try {
    const __orig = createChart;
    function __wrapped(container, options){
      const c = __orig(container, options);
      const label = (container && container.getAttribute && container.getAttribute('data-chart')) || 'unknown';
      c.__abLabel = label;
      const origAdd = c.addSeries.bind(c);
      c.addSeries = function(def, opts, paneIndex){
        const s = origAdd(def, opts, paneIndex);
        (c.__abSeries || (c.__abSeries = [])).push(s);
        return s;
      };
      (globalThis.__LWC__ || (globalThis.__LWC__ = [])).push(c);
      return c;
    }
    createChart = __wrapped;
  } catch (e) { globalThis.__LWC_INJECT_ERR__ = String(e); }
})();
`;

const DUMP_SERIES = () => {
  const out = {};
  for (const c of (globalThis.__LWC__ || [])) {
    let attached = false;
    try { const el = c.chartElement(); attached = !!el && document.contains(el); } catch {}
    if (!attached) continue;
    const label = c.__abLabel || 'unknown';
    out[label] = (c.__abSeries || []).map((s) => {
      let type = '?'; try { type = s.seriesType(); } catch {}
      let data = []; try { data = s.data(); } catch {}
      return { type, n: data.length, data };
    });
  }
  return out;
};

const MEASURE = ({ candidates }) => {
  const labelOf = (c) => c.__abLabel || 'unknown';
  const charts = (globalThis.__LWC__ || []).filter((c) => { try { const el = c.chartElement(); return !!el && document.contains(el); } catch { return false; } });
  const rows = [];
  for (const c of charts) {
    let range = null, lr = null, paneW = null, psW = null, bs = null;
    try { range = c.timeScale().getVisibleRange(); } catch {}
    try { lr = c.timeScale().getVisibleLogicalRange(); } catch {}
    try { paneW = c.timeScale().width(); } catch {}
    try { psW = c.priceScale('right').width(); } catch {}
    try { bs = c.timeScale().options().barSpacing; } catch {}
    rows.push({ label: labelOf(c), range, logical: lr, paneW, psW, barSpacing: bs });
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
      xs.push({ t, d: +Math.abs(xc - xo).toFixed(3) });
    }
    const ds = xs.map((r) => r.d);
    align = { n: xs.length, maxAbsDiff: ds.length ? +Math.max(...ds).toFixed(3) : null, over1px: ds.filter((d) => d > 1).length };
  }
  return { rows, align };
};

async function api(path) {
  const r = await fetch('http://127.0.0.1:8787/api/v1' + path);
  return r.json();
}

async function candidatesFor(ex, mid, tf) {
  const base = `/markets/${encodeURIComponent(ex)}/${encodeURIComponent(mid)}`;
  const out = new Set();
  try { const oi = await api(base + '/oi'); for (const it of oi.items || []) if (it.event_time) out.add(Math.floor(Date.parse(it.event_time) / 1000)); } catch {}
  for (const lim of [60, 500]) {
    try { const c = await api(`${base}/candles?timeframe=${tf}&limit=${lim}&no_rest_tail=1`); for (const b of c.bars || []) { const t = b.open_time || b.time; if (t) out.add(Math.floor(Date.parse(t) / 1000)); } } catch {}
  }
  return [...out].sort((a, b) => a - b);
}

const { browser, page, errors } = await open({ navigate: false });
await page.route('**/deps/lightweight-charts.js*', async (route) => {
  const res = await route.fetch();
  const body = await res.text();
  await route.fulfill({ response: res, body: body + INJECT, headers: { ...res.headers(), 'content-type': 'text/javascript' } });
});
await page.goto('http://127.0.0.1:3000/', { waitUntil: 'domcontentloaded', timeout: 60000 });
await page.waitForTimeout(9000);

async function search(sym) {
  // 搜索框匹配的是「展示简称 / 名称」，不是原始 market_id（实测搜 BTCUSDT → 0 结果）
  const keyword = sym.includes('-') ? sym.split('-')[0] : sym.replace(/USDT$/, '');
  // 先关闭可能残留的原生 <dialog>（showModal 会进入 top-layer 遮挡一切点击）
  await page.keyboard.press('Escape').catch(() => {});
  await page.waitForTimeout(300);
  await page.evaluate(() => {
    const d = document.querySelector('dialog.market-source-dialog');
    if (d && d.open) d.close();
  });
  await page.waitForTimeout(200);
  // 用 React 兼容方式写入（原生 setter + input 事件），不依赖元素可点性
  await page.evaluate((kw) => {
    const input = document.querySelector('input[type="text"]');
    if (!input) throw new Error('no text input');
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(input, '');
    input.dispatchEvent(new Event('input', { bubbles: true }));
    setter.call(input, kw);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }, keyword);
  await page.waitForTimeout(1600);
  const rows = await page.evaluate(() => Array.from(document.querySelectorAll('tr[data-token-id]')).map((r) => r.getAttribute('data-token-id')));
  console.log(`  search(${sym} as "${keyword}") -> rows=${JSON.stringify(rows)}`);
}

async function findRowId(sym) {
  return page.evaluate((s) => {
    const rows = Array.from(document.querySelectorAll('tr[data-token-id]'));
    const hit = rows.find((r) => (r.getAttribute('data-token-id') || '').toUpperCase().includes(s.toUpperCase()));
    return hit ? hit.getAttribute('data-token-id') : null;
  }, sym);
}

const results = [];
const TOKENS = (process.env.TOKENS || 'BTCUSDT:1m,30m,1h,4h,8h,1d|ETHUSDT:1m,30m,1h,4h,1d|0GUSDT:1m,30m,1h,1d|AEON-USDT-SWAP:1m,30m,1h,4h,1d').split('|');

for (const spec of TOKENS) {
  const [sym, tfList] = spec.split(':');
  const tfs = tfList.split(',');
  await search(sym);
  let id = await findRowId(sym);
  if (!id) { results.push({ sym, error: 'row not found' }); continue; }
  const isOkx = /okx/i.test(id);
  await expandRow(page, id);
  await page.waitForTimeout(3500);

  const entry = { sym, id, isOkx, tfButtons: null, steps: [], dataCheck: null, okx8h: null };

  entry.tfButtons = await page.evaluate((tokenId) => {
    const row = document.querySelector(`tr[data-token-id="${tokenId}"]`);
    const exp = row && row.nextElementSibling;
    if (!exp) return null;
    return Array.from(exp.querySelectorAll('button')).map((b) => (b.textContent || '').trim())
      .filter((t) => /^(1m|5m|15m|30m|1h|4h|8h|12h|1d)$/.test(t));
  }, id);
  entry.okx8h = { has8hButton: !!(entry.tfButtons || []).includes('8h') };

  const clickTf = (tf) => page.evaluate(({ tokenId, tf }) => {
    const row = document.querySelector(`tr[data-token-id="${tokenId}"]`);
    const exp = row && row.nextElementSibling;
    const btn = Array.from(exp.querySelectorAll('button')).find((b) => (b.textContent || '').trim() === tf);
    if (btn) btn.click();
  }, { tokenId: id, tf });

  for (const tf of tfs) {
    if (!(entry.tfButtons || []).includes(tf)) { entry.steps.push({ tf, skipped: 'no button' }); continue; }
    await clickTf(tf);
    await page.waitForTimeout(1300);
    const [ex, ...rest] = id.split(':');
    const mid = rest.join(':');
    const cand = await candidatesFor(ex, mid, tf);
    const m = await page.evaluate(MEASURE, { candidates: cand });
    const dom = await page.evaluate((tokenId) => {
      const row = document.querySelector(`tr[data-token-id="${tokenId}"]`);
      const exp = row && row.nextElementSibling;
      if (!exp) return null;
      const t = exp.innerText;
      const g = (re) => { const x = t.match(re); return x ? x[1].trim() : null; };
      return { viewport: g(/视窗\s*([^·]+)/), closedLast: g(/闭合末根\s*([^\s·]+)/), forming: g(/未收盘层\s*([^\s·]+)/), loaded: g(/本地已载入\s*([^·]+)/) || g(/当前已载入\s*([^·]+)/) };
    }, id);
    entry.steps.push({ tf, dom, measure: m });
    console.log(`  [${sym} ${tf}] ${dom && dom.viewport} | align n=${m.align && m.align.n} max=${m.align && m.align.maxAbsDiff} over1px=${m.align && m.align.over1px} | ${dom && dom.loaded}`);
  }

  // 数据完整性：把页面上的序列与后端真实数据逐点核对（当前停在最后一个 tf）
  const lastTf = tfs.filter((t) => (entry.tfButtons || []).includes(t)).slice(-1)[0];
  const series = await page.evaluate(DUMP_SERIES);
  const [ex, ...rest] = id.split(':');
  const mid = rest.join(':');
  const base = `/markets/${encodeURIComponent(ex)}/${encodeURIComponent(mid)}`;
  const oiEnv = await api(base + '/oi');
  const oiRows = (oiEnv.items || []).filter((r) => r && r.event_time);
  const oiBySec = new Map();
  for (const r of oiRows) {
    const ts = Date.parse(r.event_time);
    if (!Number.isFinite(ts)) continue;
    oiBySec.set(Math.floor(ts / 1000), r);
  }
  const candleEnv = await api(`${base}/candles?timeframe=${lastTf}&limit=500&no_rest_tail=1`);
  const barBySec = new Map();
  for (const b of candleEnv.bars || []) {
    const t = b.open_time || b.time;
    const ts = Date.parse(t);
    if (Number.isFinite(ts)) barBySec.set(Math.floor(ts / 1000), b);
  }
  const forming = candleEnv.forming_bar;
  if (forming) { const ts = Date.parse(forming.open_time || forming.time); if (Number.isFinite(ts)) barBySec.set(Math.floor(ts / 1000), forming); }

  const oiSeries = (series['lightweight-oi'] || []).find((s) => s.type === 'Histogram');
  const candleSeries = (series['lightweight-candles'] || []).find((s) => s.type === 'Candlestick');
  const volSeries = (series['lightweight-candles'] || []).find((s) => s.type === 'Histogram');

  const check = { lastTf, candle: null, volume: null, oi: null };
  if (candleSeries) {
    let match = 0, mismatch = 0, unknown = 0, dup = 0;
    const seen = new Set();
    for (const d of candleSeries.data) {
      if (seen.has(d.time)) dup += 1;
      seen.add(d.time);
      const b = barBySec.get(d.time);
      if (!b) { unknown += 1; continue; }
      const eq = b.open === undefined || (Math.abs(Number(b.open) - d.open) < 1e-9 && Math.abs(Number(b.high) - d.high) < 1e-9 && Math.abs(Number(b.low) - d.low) < 1e-9 && Math.abs(Number(b.close) - d.close) < 1e-9);
      if (eq) match += 1; else mismatch += 1;
    }
    check.candle = { n: candleSeries.data.length, match, mismatch, unknown, dup };
  }
  if (volSeries) {
    let match = 0, mismatch = 0, unknown = 0;
    for (const d of volSeries.data) {
      const b = barBySec.get(d.time);
      if (!b) { unknown += 1; continue; }
      if (Math.abs(Number(b.volume) - d.value) < 1e-6) match += 1; else mismatch += 1;
    }
    check.volume = { n: volSeries.data.length, match, mismatch, unknown };
  }
  if (oiSeries) {
    let match = 0, mismatch = 0, unknown = 0, dup = 0;
    const seen = new Set();
    for (const d of oiSeries.data) {
      if (seen.has(d.time)) dup += 1;
      seen.add(d.time);
      const r = oiBySec.get(d.time);
      if (!r) { unknown += 1; continue; }
      if (Math.abs(Number(r.open_interest) - d.value) < 1e-6) match += 1; else mismatch += 1;
    }
    check.oi = { n: oiSeries.data.length, match, mismatch, unknown, dup, apiRows: oiRows.length };
  }
  entry.dataCheck = check;

  await page.screenshot({ path: `${SHOT_DIR}/${sym.replace(/[^A-Za-z0-9]/g, '_')}.png` });
  results.push(entry);
}

fs.writeFileSync(OUT, JSON.stringify({ prefix: PREFIX, errors, results }, null, 2));
console.log('ERRORS', JSON.stringify(errors.slice(0, 6)));
console.log('WROTE', OUT);
await browser.close();
