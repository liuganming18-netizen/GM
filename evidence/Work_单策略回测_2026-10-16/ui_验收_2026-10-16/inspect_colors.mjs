/**
 * 只读探针：打开 3000 → 回测复盘 → 单策略工作台，导出关键元素的**计算后颜色**与截图。
 * 目的：确认调色板反转（index.css 未分层 :root）之后，哪些文字真的落在同色背景上不可读。
 */
import pw from '/Users/boos/.workbuddy-ai/binaries/node/workspace/node_modules/playwright/index.js';
const { chromium } = pw;

const OUT = process.argv[2] ?? '.';
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 984, height: 900 }, deviceScaleFactor: 2 });
page.on('console', (m) => {
  if (m.type() === 'error') console.log('[console.error]', m.text().slice(0, 200));
});

await page.goto('http://localhost:3000/', { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(2500);

// 进入「回测复盘」
const nav = page.getByText('回测复盘', { exact: true }).first();
if (await nav.count()) {
  await nav.click().catch(() => {});
  await page.waitForTimeout(1500);
}

const dump = await page.evaluate(() => {
  const rgb = (s) => s;
  const pick = (el) => {
    if (!el) return null;
    const cs = getComputedStyle(el);
    return { tag: el.tagName, cls: (el.className || '').toString().slice(0, 110), color: cs.color, bg: cs.backgroundColor };
  };
  const out = { probes: [], body: pick(document.body) };
  // 1) 关键 testid 容器
  for (const id of ['lab-workbench', 'lab-config', 'lab-metrics', 'lab-run-identity']) {
    const el = document.querySelector(`[data-testid="${id}"]`);
    if (el) {
      const cs = getComputedStyle(el);
      out.probes.push({ id, ...pick(el), parentBg: getComputedStyle(el.parentElement).backgroundColor, h: Math.round(el.getBoundingClientRect().height), top: Math.round(el.getBoundingClientRect().top) });
    }
  }
  // 2) 所有 text-neutral-900 / 800 / 700 元素：文字色 vs 最近有背景的祖先
  const near = (el) => {
    let p = el;
    while (p && p !== document.documentElement) {
      const bg = getComputedStyle(p).backgroundColor;
      if (bg && bg !== 'rgba(0, 0, 0, 0)') return bg;
      p = p.parentElement;
    }
    return null;
  };
  const tally = {};
  for (const el of document.querySelectorAll('[class*="text-neutral-"]')) {
    const cls = el.className.toString();
    const m = cls.match(/text-neutral-(900|800|700|600|500|400|300|200|100)/);
    if (!m) continue;
    const key = `text-neutral-${m[1]}`;
    const cs = getComputedStyle(el);
    const bg = near(el);
    tally[key] = tally[key] ?? { count: 0, color: cs.color, samples: [] };
    tally[key].count++;
    if (tally[key].samples.length < 3) {
      tally[key].samples.push({ text: (el.textContent || '').trim().slice(0, 28), color: cs.color, bgOn: bg });
    }
  }
  out.tally = tally;
  // 3) 首屏可见性：指标卡是否在第一屏
  const metric = document.querySelector('[data-testid="lab-metrics"]');
  out.metricsRect = metric ? { top: Math.round(metric.getBoundingClientRect().top), h: Math.round(metric.getBoundingClientRect().height) } : null;
  const cfg = document.querySelector('[data-testid="lab-config"]');
  out.configRect = cfg ? { top: Math.round(cfg.getBoundingClientRect().top), h: Math.round(cfg.getBoundingClientRect().height) } : null;
  out.viewportH = window.innerHeight;
  return out;
});

console.log(JSON.stringify(dump, null, 2));
await page.screenshot({ path: `${OUT}/inspect_984.png`, fullPage: false });
await page.screenshot({ path: `${OUT}/inspect_984_full.png`, fullPage: true });
await browser.close();
