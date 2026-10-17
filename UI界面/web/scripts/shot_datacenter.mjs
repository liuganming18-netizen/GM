/**
 * 数据中心页评审探针（一次性）：
 *  - 实测 feed 网格列数、各区块几何（高度占比）
 *  - 实测日志终端底色与文字色的对比度（找「浅色主题里嵌黑块」的可读性问题）
 *  - 截图：整屏 + 日志区裁剪
 * 用法：node scripts/_dc_review.mjs <输出目录> [标签]
 */
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';

const OUT = process.argv[2] || '/tmp/dc';
const TAG = process.argv[3] || 'x';
fs.mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1680, height: 1050 } });
await page.goto('http://localhost:3000', { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(2500);
await page.getByText('数据中心', { exact: true }).first().click();
await page.waitForTimeout(4500);

const r = await page.evaluate(() => {
  const q = (sel) => document.querySelector(sel);
  const box = (el) => {
    if (!el) return null;
    const b = el.getBoundingClientRect();
    return { x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height) };
  };
  const cls = (el) => (el && el.className && typeof el.className === 'string') ? el.className : '';

  // 找 feed 网格：className 同时含 grid-cols-2 与 sm:grid-cols-3
  let grid = null;
  for (const el of document.querySelectorAll('div')) {
    const c = cls(el);
    if (c.includes('grid-cols-2') && c.includes('sm:grid-cols-3')) { grid = el; break; }
  }
  const gridCols = grid ? getComputedStyle(grid).gridTemplateColumns.split(' ').length : null;
  const feedCards = grid ? Array.from(grid.children) : [];

  // 日志终端：className 含 bg-black
  let term = null;
  for (const el of document.querySelectorAll('div')) {
    const c = cls(el);
    if (c.includes('ds-surface-3')) { term = el; break; }
  }

  // 页面根
  const root = q('.ai-grid');

  // 字号分布
  const sizes = {};
  let tiny = 0, total = 0;
  for (const el of document.querySelectorAll('span,div,td,th,p,a,button')) {
    if (!el.textContent.trim() || el.children.length > 0) continue;
    const fs = parseFloat(getComputedStyle(el).fontSize);
    sizes[fs + 'px'] = (sizes[fs + 'px'] || 0) + 1; total++;
    if (fs < 11) tiny++;
  }

  // 背景色分布
  const bg = {};
  for (const el of document.querySelectorAll('*')) {
    const cs = getComputedStyle(el);
    if (cs.backgroundColor !== 'rgba(0, 0, 0, 0)') bg[cs.backgroundColor] = (bg[cs.backgroundColor] || 0) + 1;
  }

  // 日志终端内文字颜色采样
  const termTexts = [];
  if (term) {
    for (const el of term.querySelectorAll('span')) {
      const t = el.textContent.trim();
      if (!t || el.children.length > 0) continue;
      const cs = getComputedStyle(el);
      termTexts.push({ t: t.slice(0, 22), color: cs.color, fs: cs.fontSize });
      if (termTexts.length >= 14) break;
    }
  }

  // 绿色徽章计数
  const greenBadges = document.querySelectorAll('[class*="emerald-500/10"], [class*="emerald-500/20"]').length;

  return {
    gridCols,
    feedCount: feedCards.length,
    feedCardBox: box(feedCards[0]),
    feedGridBox: box(grid),
    termBox: box(term),
    termBg: term ? getComputedStyle(term).backgroundColor : null,
    termScroll: term ? { sh: term.scrollHeight, ch: term.clientHeight } : null,
    rootBox: box(root),
    docScroll: (() => { const se = document.scrollingElement; return { h: se.scrollHeight, ch: se.clientHeight }; })(),
    fontSizeDist: Object.fromEntries(Object.entries(sizes).sort((a, b) => parseFloat(a) - parseFloat(b))),
    tinyRatio: total ? +(tiny / total * 100).toFixed(1) : 0,
    topBg: Object.entries(bg).sort((a, b) => b[1] - a[1]).slice(0, 8),
    dsClassCount: document.querySelectorAll('[class*="ds-"]').length,
    twColorClassCount: document.querySelectorAll('[class*="neutral-"], [class*="emerald-"], [class*="rose-"], [class*="amber-"]').length,
    greenBadges,
    termTexts,
  };
});

console.log(JSON.stringify(r, null, 2));
await page.screenshot({ path: path.join(OUT, `${TAG}_full.png`) });
if (r.termBox) {
  const b = r.termBox;
  await page.screenshot({
    path: path.join(OUT, `${TAG}_term.png`),
    clip: { x: b.x, y: Math.max(0, b.y), width: b.w, height: Math.min(b.h, 1050 - b.y) },
  });
}
await browser.close();
console.log('done', OUT);
