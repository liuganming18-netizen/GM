/**
 * 逐点数据完整性核对（只读探针）。
 *
 * 与 verify2.mjs 的区别：
 *  - 成交量按后端真实字段 `base_volume` 比对（前端 adapters.ts:324 `volume: b.base_volume`）；
 *  - 把「闭合蜡烛序列 / 未收盘临时序列 / 成交量柱 / OI 柱」全部 dump 出来分别核对；
 *  - 打印每一处不一致的 (time, 前端值, 后端值)，用于定位而非只给计数。
 *
 * 判据（用户口径）：保留原始数值/时间戳/单位与真实缺口，无补零、无插值、无伪造、无下标错配。
 */
import { open, expandRow } from './harness.mjs';
import fs from 'node:fs';

const PREFIX = process.env.OUT_PREFIX || 'datacheck';
const OUT = `/tmp/oi_fix/${PREFIX}.json`;
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

const DUMP_ALL = () => {
  const out = {};
  for (const c of (globalThis.__LWC__ || [])) {
    let attached = false;
    try { const el = c.chartElement(); attached = !!el && document.contains(el); } catch {}
    if (!attached) continue;
    const label = c.__abLabel || 'unknown';
    out[label] = (c.__abSeries || []).map((s, i) => {
      let type = '?'; try { type = s.seriesType(); } catch {}
      let data = []; try { data = s.data(); } catch {}
      let opts = null; try { opts = s.options(); } catch {}
      return { i, type, n: data.length, priceScaleId: opts && opts.priceScaleId, data };
    });
  }
  return out;
};

async function api(path) {
  const r = await fetch('http://127.0.0.1:8787/api/v1' + path);
  return r.json();
}

const sec = (t) => Math.floor(Date.parse(t) / 1000);

const { browser, page, errors } = await open({ navigate: false });
await page.route('**/deps/lightweight-charts.js*', async (route) => {
  const res = await route.fetch();
  const body = await res.text();
  await route.fulfill({ response: res, body: body + INJECT, headers: { ...res.headers(), 'content-type': 'text/javascript' } });
});
await page.goto('http://127.0.0.1:3000/', { waitUntil: 'domcontentloaded', timeout: 60000 });
await page.waitForTimeout(9000);

async function search(sym) {
  const keyword = sym.includes('-') ? sym.split('-')[0] : sym.replace(/USDT$/, '');
  await page.keyboard.press('Escape').catch(() => {});
  await page.waitForTimeout(250);
  await page.evaluate(() => { const d = document.querySelector('dialog.market-source-dialog'); if (d && d.open) d.close(); });
  await page.evaluate((kw) => {
    const input = document.querySelector('input[type="text"]');
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(input, ''); input.dispatchEvent(new Event('input', { bubbles: true }));
    setter.call(input, kw); input.dispatchEvent(new Event('input', { bubbles: true }));
  }, keyword);
  await page.waitForTimeout(1600);
}

async function findRowId(sym) {
  return page.evaluate((s) => {
    const rows = Array.from(document.querySelectorAll('tr[data-token-id]'));
    const hit = rows.find((r) => (r.getAttribute('data-token-id') || '').toUpperCase().includes(s.toUpperCase()));
    return hit ? hit.getAttribute('data-token-id') : null;
  }, sym);
}

const results = [];
// 默认：BTC 全 9 周期（定位 1d 差异）+ ETH/0G/OKX 各代表周期
const SPECS = (process.env.SPECS || 'BTCUSDT:1m,5m,15m,30m,1h,4h,8h,12h,1d|ETHUSDT:30m,1d|0GUSDT:30m,1d|AEON-USDT-SWAP:30m,1d').split('|');

for (const spec of SPECS) {
  const [sym, tfList] = spec.split(':');
  const tfs = tfList.split(',');
  await search(sym);
  const id = await findRowId(sym);
  if (!id) { results.push({ sym, error: 'row not found' }); continue; }
  await expandRow(page, id);
  await page.waitForTimeout(3000);

  const tfButtons = await page.evaluate((tokenId) => {
    const row = document.querySelector(`tr[data-token-id="${tokenId}"]`);
    const exp = row && row.nextElementSibling;
    if (!exp) return [];
    return Array.from(exp.querySelectorAll('button')).map((b) => (b.textContent || '').trim())
      .filter((t) => /^(1m|5m|15m|30m|1h|4h|8h|12h|1d)$/.test(t));
  }, id);

  const [ex, ...rest] = id.split(':');
  const mid = rest.join(':');
  const base = `/markets/${encodeURIComponent(ex)}/${encodeURIComponent(mid)}`;

  const entry = { sym, id, ex, mid, tfButtons, tfs: [] };

  for (const tf of tfs) {
    if (!tfButtons.includes(tf)) { entry.tfs.push({ tf, skipped: 'no button' }); continue; }
    await page.evaluate(({ tokenId, tf }) => {
      const row = document.querySelector(`tr[data-token-id="${tokenId}"]`);
      const exp = row && row.nextElementSibling;
      const btn = Array.from(exp.querySelectorAll('button')).find((b) => (b.textContent || '').trim() === tf);
      if (btn) btn.click();
    }, { tokenId: id, tf });
    await page.waitForTimeout(1400);

    const series = await page.evaluate(DUMP_ALL);
    const candleEnv = await api(`${base}/candles?timeframe=${tf}&limit=500&no_rest_tail=1`);
    const oiEnv = await api(base + '/oi');

    const barBySec = new Map();
    for (const b of candleEnv.bars || []) barBySec.set(sec(b.open_time), b);
    const forming = candleEnv.forming_bar || null;
    const formingSec = forming ? sec(forming.open_time) : null;

    const oiBySec = new Map();
    for (const r of oiEnv.items || []) { if (r && r.event_time) oiBySec.set(sec(r.event_time), r); }

    const cSers = (series['lightweight-candles'] || []).filter((s) => s.type === 'Candlestick');
    const vSer = (series['lightweight-candles'] || []).find((s) => s.type === 'Histogram');
    const oSer = (series['lightweight-oi'] || []).find((s) => s.type === 'Histogram');

    const detail = { tf, candleSeriesCount: cSers.length, candles: [], volume: null, oi: null };

    for (const cs of cSers) {
      const rec = { n: cs.n, match: 0, mismatch: 0, unknown: 0, dup: 0, isForming: cs.n <= 2, bad: [] };
      const seen = new Set();
      for (const d of cs.data) {
        if (seen.has(d.time)) rec.dup++;
        seen.add(d.time);
        const b = barBySec.get(d.time);
        if (!b) { rec.unknown++; if (rec.bad.length < 5) rec.bad.push({ t: d.time, why: 'no backend bar' }); continue; }
        const ok = ['open', 'high', 'low', 'close'].every((k) => Math.abs(Number(b[k]) - Number(d[k])) < 1e-9);
        if (ok) rec.match++; else { rec.mismatch++; if (rec.bad.length < 5) rec.bad.push({ t: d.time, front: { o: d.open, h: d.high, l: d.low, c: d.close }, back: { o: b.open, h: b.high, l: b.low, c: b.close }, isFormingBar: d.time === formingSec }); }
      }
      rec.headTime = cs.data[0] && cs.data[0].time;
      rec.tailTime = cs.data[cs.data.length - 1] && cs.data[cs.data.length - 1].time;
      detail.candles.push(rec);
    }

    if (vSer) {
      const rec = { n: vSer.n, match: 0, mismatch: 0, unknown: 0, zero: 0, bad: [] };
      for (const d of vSer.data) {
        const b = barBySec.get(d.time);
        if (!b) { rec.unknown++; continue; }
        const bv = Number(b.base_volume);
        if (!Number.isFinite(bv)) { rec.unknown++; continue; }
        if (Math.abs(bv - Number(d.value)) < 1e-6) rec.match++;
        else { rec.mismatch++; if (rec.bad.length < 5) rec.bad.push({ t: d.time, front: d.value, back: bv }); }
      }
      detail.volume = rec;
    }

    if (oSer) {
      const rec = { n: oSer.n, match: 0, mismatch: 0, unknown: 0, dup: 0, apiRows: (oiEnv.items || []).length, bad: [] };
      const seen = new Set();
      for (const d of oSer.data) {
        if (seen.has(d.time)) rec.dup++;
        seen.add(d.time);
        const r = oiBySec.get(d.time);
        if (!r) { rec.unknown++; if (rec.bad.length < 5) rec.bad.push({ t: d.time, why: 'no oi row' }); continue; }
        if (Math.abs(Number(r.open_interest) - Number(d.value)) < 1e-6) rec.match++;
        else { rec.mismatch++; if (rec.bad.length < 5) rec.bad.push({ t: d.time, front: d.value, back: r.open_interest }); }
      }
      rec.headTime = oSer.data[0] && oSer.data[0].time;
      rec.tailTime = oSer.data[oSer.data.length - 1] && oSer.data[oSer.data.length - 1].time;
      detail.oi = rec;
    }

    entry.tfs.push(detail);
    const c0 = detail.candles[0] || {};
    console.log(`[${sym} ${tf}] candleSeries=${detail.candleSeriesCount} candle(m=${c0.match},x=${c0.mismatch},u=${c0.unknown},dup=${c0.dup},n=${c0.n}) vol(m=${detail.volume && detail.volume.match},x=${detail.volume && detail.volume.mismatch},u=${detail.volume && detail.volume.unknown}) oi(m=${detail.oi && detail.oi.match},x=${detail.oi && detail.oi.mismatch},u=${detail.oi && detail.oi.unknown},dup=${detail.oi && detail.oi.dup},api=${detail.oi && detail.oi.apiRows})`);
  }

  await page.screenshot({ path: `${SHOT_DIR}/${sym.replace(/[^A-Za-z0-9]/g, '_')}.png` });
  results.push(entry);
}

fs.writeFileSync(OUT, JSON.stringify({ prefix: PREFIX, errors, results }, null, 2));
console.log('ERRORS', JSON.stringify(errors.slice(0, 6)));
console.log('WROTE', OUT);
await browser.close();
