/**
 * 客观可读性审计（只读）：遍历 Lab 页每个**有可见文字**的元素，
 * 用「文字色 vs 最近不透明祖先背景」算 WCAG 对比度，列出 < 2.0（近乎不可见）与 < 3.0 的项。
 * 目的：把「白字落白底」这类问题从主观判断变成可复核数字。
 */
import pw from '/Users/boos/.workbuddy-ai/binaries/node/workspace/node_modules/playwright/index.js';
import fs from 'node:fs';
const { chromium } = pw;

const OUT = process.argv[2] ?? '.';
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 984, height: 900 } });
await page.goto('http://localhost:3000/', { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(1800);
await page.getByText('回测复盘', { exact: true }).first().click();
await page.waitForFunction(() => {
  const m = document.querySelector('[data-testid="lab-metrics"]')?.textContent ?? '';
  return m.includes('净收益率')
    && document.querySelector('[data-testid="lab-funding-panel"]')
    && document.querySelector('[data-testid="lab-funding-chart"] canvas')
    && document.querySelector('[data-testid="lab-chart-panel"] canvas')
    && document.querySelector('tr[data-trade-id]')
    && document.querySelector('[data-testid="lab-quality-panel"]');
}, null, { timeout: 90000 });
await page.waitForTimeout(2000);

await page.getByTestId('review-tab-legacy').click();
await page.waitForTimeout(3500);
const report = await page.evaluate(() => {
  const parse = (s) => {
    const m = s.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(',').map((x) => parseFloat(x));
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };
  const lum = ({ r, g, b }) => {
    const f = (c) => { const v = c / 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((m, n) => n - m); return (x + 0.05) / (y + 0.05); };
  const opaqueBg = (el) => {
    let p = el;
    while (p && p !== document.documentElement) {
      const c = parse(getComputedStyle(p).backgroundColor);
      if (c && c.a > 0.5) return c;
      p = p.parentElement;
    }
    return { r: 255, g: 255, b: 255, a: 1 };
  };
  const bad = [];
  let checked = 0;
  for (const el of document.querySelectorAll('body *')) {
    const own = [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim().length > 0);
    if (!own) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity) < 0.3) continue;
    const fg = parse(cs.color);
    if (!fg) continue;
    const bg = opaqueBg(el);
    const r = ratio(fg, bg);
    checked++;
    if (r < 3.0) {
      bad.push({
        ratio: Math.round(r * 100) / 100,
        text: (el.textContent || '').trim().slice(0, 40),
        fg: cs.color,
        bg: `rgb(${Math.round(bg.r)}, ${Math.round(bg.g)}, ${Math.round(bg.b)})`,
        cls: el.className.toString().slice(0, 70),
      });
    }
  }
  bad.sort((a, b) => a.ratio - b.ratio);
  return { checked, badCount: bad.length, worst: bad.slice(0, 25) };
});

console.log(JSON.stringify(report, null, 1));
fs.writeFileSync(`${OUT}/legacy_readability_audit.json`, JSON.stringify(report, null, 2));
await browser.close();
