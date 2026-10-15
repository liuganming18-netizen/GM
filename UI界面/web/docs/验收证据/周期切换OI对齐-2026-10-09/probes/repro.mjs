/**
 * 周期切换 OI/K 线对齐复现探针（只读，不改工程源码）。
 *
 * 观测手段：仅在**测试浏览器**里用 page.route 拦截 Vite 预打包的
 * `lightweight-charts.js`，在其末尾追加一段包装，把每个 `createChart` 实例
 * 记录到 `window.__LWC__`（连同其容器 DOM）。工程源码零改动。
 * 由此可在页面内调用官方 API：
 *   chart.timeScale().getVisibleRange()
 *   chart.timeScale().timeToCoordinate(time)   ← 精确到像素
 */
import { open, expandRow } from './harness.mjs';

const TOKEN = process.env.TOKEN || 'binance:BTCUSDT';
const TFS = (process.env.TFS || '1m,5m,15m,30m,1h,4h,8h,12h,1d').split(',');
const OUT = process.env.OUT || '/tmp/oi_fix/repro.json';
const SHOT_DIR = process.env.SHOT_DIR || '/tmp/oi_fix/shots';
import fs from 'node:fs';
fs.mkdirSync(SHOT_DIR, { recursive: true });

const INJECT = `
;(function(){
  try {
    const __orig = createChart;
    function __wrapped(container, options){
      const c = __orig(container, options);
      (globalThis.__LWC__ || (globalThis.__LWC__ = [])).push(c);
      return c;
    }
    createChart = __wrapped;
  } catch (e) { globalThis.__LWC_INJECT_ERR__ = String(e); }
})();
`;

const { browser, page, errors } = await open({ navigate: false });
await page.route('**/deps/lightweight-charts.js*', async (route) => {
  const res = await route.fetch();
  const body = await res.text();
  await route.fulfill({ response: res, body: body + INJECT, headers: { ...res.headers(), 'content-type': 'text/javascript' } });
});

await page.goto('http://127.0.0.1:3000/', { waitUntil: 'domcontentloaded', timeout: 60000 });
await page.waitForTimeout(8000);
await expandRow(page, TOKEN);
await page.waitForTimeout(4000);

const injected = await page.evaluate(() => ({ n: (globalThis.__LWC__ || []).length, err: globalThis.__LWC_INJECT_ERR__ || null }));

/** 页面内：读取两图几何 + 可见范围 + 同时间点横坐标差 */
const MEASURE = ({ tokenId, candidates }) => {
  const charts = (globalThis.__LWC__ || []).filter((c) => {
    try { const el = c.chartElement(); return !!el && document.contains(el); } catch { return false; }
  });
  const labelOf = (c) => {
    try {
      let el = c.chartElement();
      while (el) {
        const a = el.getAttribute && el.getAttribute('data-chart');
        if (a) return a;
        el = el.parentElement;
      }
    } catch {}
    return 'unknown';
  };
  const info = [];
  for (const c of charts) {
    let range = null, paneW = null, psW = null, bs = null, lr = null;
    try { range = c.timeScale().getVisibleRange(); } catch {}
    try { paneW = c.timeScale().width(); } catch {}
    try { psW = c.priceScale('right').width(); } catch {}
    try { bs = c.timeScale().options().barSpacing; } catch {}
    try { lr = c.timeScale().getVisibleLogicalRange(); } catch {}
    let el = null;
    try { el = c.chartElement(); } catch {}
    const r = el ? el.getBoundingClientRect() : null;
    info.push({ label: labelOf(c), range, paneW, psW, barSpacing: bs, logicalRange: lr,
      rect: r ? { left: Math.round(r.left), top: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) } : null });
  }
  // 两图配对（按容器标签）
  const pick = (l) => info.filter((i) => i.label === l);
  const candles = pick('lightweight-candles');
  const oi = pick('lightweight-oi');
  let diffs = null, commonCount = 0;
  if (candles.length === 1 && oi.length === 1) {
    const chartC = charts.find((c) => labelOf(c) === 'lightweight-candles');
    const chartO = charts.find((c) => labelOf(c) === 'lightweight-oi');
    const xs = [];
    for (const t of candidates) {
      let xc = null, xo = null;
      try { xc = chartC.timeScale().timeToCoordinate(t); } catch {}
      try { xo = chartO.timeScale().timeToCoordinate(t); } catch {}
      if (xc === null || xo === null) continue;
      commonCount += 1;
      xs.push({ t, xc, xo, d: Math.abs(xc - xo) });
    }
    const ds = xs.map((r) => r.d);
    diffs = {
      count: xs.length,
      maxAbsDiff: ds.length ? Math.max(...ds) : null,
      meanAbsDiff: ds.length ? ds.reduce((a, b) => a + b, 0) / ds.length : null,
      over1px: ds.filter((d) => d > 1).length,
      samples: xs.slice(0, 5).concat(xs.slice(-3)),
    };
  }
  return { info, commonCount, diffs };
};

/** 候选时间点：OI 事件时间 + 各周期 K 线开盘时间（Node 侧从真实 API 取，与页面同源） */
async function candidatesFor(tf) {
  const base = 'http://127.0.0.1:8787/api/v1/markets/binance/BTCUSDT';
  const out = new Set();
  const oi = await (await fetch(base + '/oi')).json();
  for (const it of oi.items || []) {
    if (it.event_time) out.add(Math.floor(Date.parse(it.event_time) / 1000));
  }
  for (const lim of [60, 500]) {
    const c = await (await fetch(`${base}/candles?timeframe=${tf}&limit=${lim}&no_rest_tail=1`)).json();
    for (const b of c.bars || []) {
      const t = b.open_time || b.time;
      if (t) out.add(Math.floor(Date.parse(t) / 1000));
    }
  }
  return [...out].sort((a, b) => a - b);
}

const steps = [];
async function snapshot(label, tf) {
  await page.waitForTimeout(1200);
  const cand = await candidatesFor(tf);
  const m = await page.evaluate(MEASURE, { tokenId: TOKEN, candidates: cand });
  const dom = await page.evaluate((id) => {
    const row = document.querySelector(`tr[data-token-id="${id}"]`);
    let exp = row && row.nextElementSibling;
    if (!(exp && exp.querySelector('[data-chart="lightweight-candles"]'))) exp = null;
    if (!exp) return { found: false };
    const txt = exp.innerText;
    const g = (re) => { const x = txt.match(re); return x ? x[1].trim() : null; };
    const btns = Array.from(exp.querySelectorAll('button')).filter((b) => /^(1m|5m|15m|30m|1h|4h|8h|12h|1d)$/.test((b.textContent || '').trim()));
    return {
      found: true,
      selectedTf: btns.filter((b) => /emerald/.test(b.className)).map((b) => b.textContent.trim())[0] || null,
      viewport: g(/视窗\s*([^·]+)/),
      closedLast: g(/闭合末根\s*([^\s·]+)/),
      forming: g(/未收盘层\s*([^\s·]+)/),
      loaded: g(/本地已载入\s*([^·]+)/) || g(/当前已载入\s*([^·]+)/),
      oiTime: g(/OI时点:\s*([^\s]+)/),
    };
  }, TOKEN);
  const shot = `${SHOT_DIR}/${label}.png`;
  await page.screenshot({ path: shot });
  const rec = { label, dom, measure: m, injected: null, shot };
  steps.push(rec);
  console.log(`[${label}] tf=${dom.selectedTf} viewport=${dom.viewport}`);
  for (const i of m.info) {
    console.log(`   ${i.label}: range=${JSON.stringify(i.range)} logical=${JSON.stringify(i.logicalRange)} paneW=${i.paneW} psW=${i.psW} barSpacing=${i.barSpacing}`);
  }
  if (m.diffs) console.log(`   x-diff: n=${m.diffs.count} max=${m.diffs.maxAbsDiff} mean=${m.diffs.meanAbsDiff} over1px=${m.diffs.over1px}`);
  return rec;
}

async function clickTf(tf) {
  await page.evaluate(({ id, tf }) => {
    const row = document.querySelector(`tr[data-token-id="${id}"]`);
    const exp = row && row.nextElementSibling;
    const btn = Array.from(exp.querySelectorAll('button')).find((b) => (b.textContent || '').trim() === tf);
    if (btn) btn.click();
  }, { id: TOKEN, tf });
}

console.log('INJECTED charts:', JSON.stringify(injected));
await snapshot('00_initial', '1m');

for (const tf of TFS) {
  await clickTf(tf);
  await snapshot(`tf_${tf}`, tf);
}
// 回切序列
await clickTf('30m'); await snapshot('back_30m', '30m');
await clickTf('1h'); await snapshot('back_1h', '1h');
await clickTf('30m'); await snapshot('back_30m_again', '30m');
// 快速往返
for (const tf of ['1h', '30m', '1h', '30m', '1h', '30m']) { await clickTf(tf); await page.waitForTimeout(250); }
await snapshot('rapid_after', '30m');

fs.writeFileSync(OUT, JSON.stringify({ token: TOKEN, errors, steps }, null, 2));
console.log('ERRORS:', JSON.stringify(errors.slice(0, 8)));
console.log('WROTE', OUT);
await browser.close();
