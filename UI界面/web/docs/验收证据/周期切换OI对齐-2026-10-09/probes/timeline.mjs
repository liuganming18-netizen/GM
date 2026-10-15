/**
 * 时序探针：记录两图的时间轴调用与可见范围事件（只读，不改工程源码）。
 */
import { open, expandRow } from './harness.mjs';
import fs from 'node:fs';

const TOKEN = 'binance:BTCUSDT';
const OUT = process.env.OUT || '/tmp/oi_fix/timeline.json';

const INJECT = `
;(function(){
  try {
    const __orig = createChart;
    function __wrapped(container, options){
      const c = __orig(container, options);
      const label = (container && container.getAttribute && container.getAttribute('data-chart')) || 'unknown';
      (globalThis.__LWC__ || (globalThis.__LWC__ = [])).push(c);
      const ts = c.timeScale();
      c.__label = label;
      try {
        ts.subscribeVisibleLogicalRangeChange((r) => {
          (globalThis.__LWC_EV__ || (globalThis.__LWC_EV__ = [])).push({
            t: Math.round(performance.now()), kind: 'rangeEvent', label,
            lr: r ? { from: +r.from.toFixed(3), to: +r.to.toFixed(3) } : null,
            vr: (() => { try { const v = ts.getVisibleRange(); return v ? {from:v.from,to:v.to} : null; } catch { return null; } })(),
          });
        });
      } catch (e) {}
      for (const m of ['setVisibleRange','fitContent','scrollToRealTime','setVisibleLogicalRange','applyOptions']) {
        try {
          const o = ts[m].bind(ts);
          ts[m] = (...a) => {
            (globalThis.__LWC_CALL__ || (globalThis.__LWC_CALL__ = [])).push({
              t: Math.round(performance.now()), kind: 'call', label, m,
              a: a.map((x) => (x && typeof x === 'object') ? JSON.parse(JSON.stringify(x)) : x),
            });
            return o(...a);
          };
        } catch (e) {}
      }
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
await page.waitForTimeout(9000);
await expandRow(page, TOKEN);
await page.waitForTimeout(3000);

const dump = async (mark) => page.evaluate((m) => {
  const ev = (globalThis.__LWC_EV__ || []).slice();
  const calls = (globalThis.__LWC_CALL__ || []).slice();
  globalThis.__LWC_EV__ = [];
  globalThis.__LWC_CALL__ = [];
  return { mark: m, calls, ev };
}, mark);

const clickTf = (tf) => page.evaluate(({ id, tf }) => {
  const row = document.querySelector(`tr[data-token-id="${id}"]`);
  const exp = row && row.nextElementSibling;
  const btn = Array.from(exp.querySelectorAll('button')).find((b) => (b.textContent || '').trim() === tf);
  if (btn) btn.click();
}, { id: TOKEN, tf });

const log = [];
const step = async (mark, action) => {
  if (action) await action();
  await page.waitForTimeout(1500);
  const d = await dump(mark);
  log.push(d);
  console.log(`=== ${mark} ===`);
  for (const c of d.calls) console.log(`   CALL ${c.label}.${c.m}(${JSON.stringify(c.a)}) @${c.t}`);
  for (const e of d.ev) console.log(`   EV   ${e.label} lr=${JSON.stringify(e.lr)} vr=${JSON.stringify(e.vr)} @${e.t}`);
};

await step('initial-1m');
await step('click-1m(load history)', () => clickTf('1m'));
await step('click-30m', () => clickTf('30m'));
await step('click-1h', () => clickTf('1h'));
await step('click-30m-again', () => clickTf('30m'));

fs.writeFileSync(OUT, JSON.stringify({ errors, log }, null, 2));
console.log('ERRORS', JSON.stringify(errors.slice(0, 5)));
await browser.close();
