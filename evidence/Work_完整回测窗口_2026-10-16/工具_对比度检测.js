// 检测低对比度（近乎不可见）文字：color 与最近不透明祖先背景对比度 < 1.8
const { chromium } = require('playwright');
const BASE = process.env.BASE_URL || 'http://127.0.0.1:3010';

function parseRGB(s) {
  const m = s && s.match(/rgba?\(([^)]+)\)/);
  if (!m) return null;
  const p = m[1].split(',').map((x) => parseFloat(x.trim()));
  return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
}
function lum({ r, g, b }) {
  const f = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}
function ratio(a, b) { const l1 = lum(a), l2 = lum(b); const hi = Math.max(l1, l2), lo = Math.min(l1, l2); return (hi + 0.05) / (lo + 0.05); }

(async () => {
  const browser = await chromium.launch();
  const page = await (await browser.newContext({ viewport: { width: 1680, height: 1050 } })).newPage();
  await page.goto(BASE + '/', { waitUntil: 'load', timeout: 60000 });
  await page.waitForTimeout(3500);
  const nav = page.getByText('回测复盘', { exact: true }).first();
  if (await nav.count() > 0) { await nav.click(); await page.waitForTimeout(8000); }

  const bad = await page.evaluate(() => {
    const cvs = document.createElement('canvas').getContext('2d');
    function toRGBA(css) {
      if (!css) return null;
      cvs.fillStyle = '#000';
      cvs.fillStyle = css;
      const s = cvs.fillStyle; // normalized: '#rrggbb' or 'rgba(r,g,b,a)'
      if (s.startsWith('#')) {
        const h = s.slice(1);
        return { r: parseInt(h.slice(0, 2), 16), g: parseInt(h.slice(2, 4), 16), b: parseInt(h.slice(4, 6), 16), a: 1 };
      }
      const m = s.match(/rgba?\(([^)]+)\)/);
      if (!m) return null;
      const p = m[1].split(',').map((x) => parseFloat(x.trim()));
      return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
    }
    function parseRGB(s) { return toRGBA(s); }
    function lum({ r, g, b }) { const f = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); }
    function ratio(a, b) { const l1 = lum(a), l2 = lum(b); const hi = Math.max(l1, l2), lo = Math.min(l1, l2); return (hi + 0.05) / (lo + 0.05); }
    function effBg(el) {
      let n = el;
      while (n) {
        const bg = parseRGB(getComputedStyle(n).backgroundColor);
        if (bg && bg.a > 0.55) return bg;
        n = n.parentElement;
      }
      return { r: 255, g: 255, b: 255, a: 1 };
    }
    const out = [];
    const all = document.querySelectorAll('*');
    for (const el of all) {
      if (!el.childNodes || el.childNodes.length !== 1) continue;
      const tn = el.childNodes[0];
      if (tn.nodeType !== 3) continue;
      const txt = (tn.textContent || '').trim();
      if (!txt || txt.length < 2) continue;
      const st = getComputedStyle(el);
      if (st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity) < 0.4) continue;
      const fg = parseRGB(st.color); if (!fg) continue;
      const bg = effBg(el);
      const r = ratio(fg, bg);
      if (r < 1.8) out.push({ txt: txt.slice(0, 40), cls: el.className, color: st.color, bg: `rgb(${bg.r},${bg.g},${bg.b})`, ratio: +r.toFixed(2) });
    }
    return out;
  });
  console.log('低对比度文本数：', bad.length);
  const seen = new Set();
  for (const b of bad) { const k = b.cls + '|' + b.color + '|' + b.bg; if (seen.has(k)) continue; seen.add(k); console.log(`  ratio=${b.ratio} color=${b.color} bg=${b.bg} :: "${b.txt}" :: ${b.cls}`); }
  await browser.close();
})().catch((e) => { console.error('FAIL:', e); process.exit(1); });
