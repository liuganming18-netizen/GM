/**
 * 列表渲染性能实测探针（只读，不改工程、不发上游请求）
 *
 * 连接既有 3000 真实页面（经 CDP），用**可复算的机器指标**回答「一次性渲染太多」是否成立：
 *   1) 冷启动：reload 后首行出现 / 全池 549 行到位 / 计数稳定 三个时点
 *   2) DOM 规模：总元素数、行数、SVG/polyline/path/circle 等图元数、已挂载展开图数
 *   3) 刷新代价：12s（覆盖 2 个 5s 刷新周期）内的
 *        · CDP CPU Profiler 采样 → 按函数名聚合自耗时（直接看是不是 TokenTable 渲染在烧 CPU）
 *        · longtask 长任务条数 / 总阻塞时长 / 最长一条
 *        · MutationObserver 记录的 DOM 变更（属性写 + 子树变更）
 *   4) 交互耗时：搜索逐字 / 清空 / 展开 / 收起
 *   5) 滚动：程序化滚到底再滚回顶，rAF 帧间隔的均值 / p95 / 最长 / >50ms 掉帧数
 *
 * 用法：
 *   CDP_URL=ws://127.0.0.1:PORT/devtools/browser/... \
 *   node probe_list_perf_2026-10-07.mjs <outJsonPath> [profileSeconds]
 */
import fs from 'node:fs';

const PW_ENTRY =
  process.env.PLAYWRIGHT_CORE ??
  '/Users/boos/.workbuddy-ai/binaries/node/workspace/node_modules/playwright-core/index.js';
const pw = await import(PW_ENTRY);
const chromium = pw.chromium ?? pw.default?.chromium;

const CDP_URL = process.env.CDP_URL;
if (!CDP_URL) throw new Error('缺少 CDP_URL');
const OUT = process.argv[2] ?? '/tmp/list-perf.json';
const PROFILE_S = Number(process.argv[3] ?? 12);
const SHOT_DIR = process.env.SCREENSHOT_DIR ?? null;
/** 可选：标准化视口，如 VIEWPORT=1800x1180（经 CDP Emulation 覆盖，保证多次测量可比） */
const VIEWPORT = process.env.VIEWPORT ?? null;
/** 截图标签，如 LABEL=改前 / LABEL=改后 */
const LABEL = process.env.LABEL ?? '改后';

const browser = await chromium.connectOverCDP(CDP_URL);
const ctx = browser.contexts()[0];
const page = ctx.pages().find((p) => p.url().includes('127.0.0.1:3000'));
if (!page) {
  throw new Error(
    `未找到 3000 页面（当前标签：${ctx.pages().map((p) => p.url()).join(' | ')}）。请先 agent-browser open http://127.0.0.1:3000`,
  );
}
await page.bringToFront();

/** 标准化视口：用 CDP Emulation 覆盖，不改窗口本身；测量结束后清除。 */
let emuClient = null;
if (VIEWPORT) {
  const [w, h] = VIEWPORT.split('x').map(Number);
  emuClient = await page.context().newCDPSession(page);
  await emuClient.send('Emulation.setDeviceMetricsOverride', {
    width: w,
    height: h,
    deviceScaleFactor: 1,
    mobile: false,
  });
}

const shot = async (name) => {
  if (!SHOT_DIR) return null;
  const p = `${SHOT_DIR}/${name}`;
  await page.screenshot({ path: p });
  return p;
};

const result = { cdpUrl: CDP_URL, startedAt: new Date().toISOString(), steps: {} };

/** 页面内通用工具：表格滚动容器 / 计数元素 / 展开图计数 */
const PAGE_HELPERS = () => {
  window.__t = {
    scroller() {
      const tr = document.querySelector('tr[data-token-id]');
      let el = tr && tr.parentElement;
      while (el && !(el.scrollHeight > el.clientHeight + 4)) el = el.parentElement;
      return el;
    },
    countEl() {
      return Array.from(document.querySelectorAll('div')).find((d) =>
        /^共 \d+ 个标的$/.test((d.textContent || '').trim()),
      );
    },
    domCounts() {
      const q = (s) => document.querySelectorAll(s).length;
      const sc = window.__t.scroller();
      const rows = Array.from(document.querySelectorAll('tr[data-token-id]'));
      const firstTr = rows[0];
      return {
        totalElements: q('*'),
        rows: q('tr[data-token-id]'),
        tbodyRows: q('tbody tr'),
        svg: q('svg'),
        polyline: q('polyline'),
        path: q('path'),
        circle: q('circle'),
        line: q('line'),
        rect: q('rect'),
        canvas: q('canvas'),
        buttons: q('button'),
        expandedCharts: q('svg[viewBox="0 0 1000 200"]'),
        oiCharts: q('svg[viewBox="0 0 1000 110"]'),
        spacerRows: q('tbody tr[aria-hidden]'),
        rowHeight: firstTr ? firstTr.offsetHeight : null,
        scroller: sc
          ? { clientH: sc.clientHeight, scrollH: sc.scrollHeight, scrollTop: Math.round(sc.scrollTop) }
          : null,
        renderedFirstId: rows.length ? rows[0].dataset.tokenId : null,
        renderedLastId: rows.length ? rows[rows.length - 1].dataset.tokenId : null,
        visibleRows: sc
          ? rows.filter((r) => {
              const b = r.getBoundingClientRect();
              const sb = sc.getBoundingClientRect();
              return b.bottom > sb.top && b.top < sb.bottom;
            }).length
          : 0,
      };
    },
    setSearch(v) {
      const i = document.querySelector('input[placeholder*="搜索代币"]');
      if (!i) throw new Error('搜索框不存在（页面可能未渲染或已导航）');
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(i, v);
      i.dispatchEvent(new Event('input', { bubbles: true }));
    },
    searchValue() {
      return document.querySelector('input[placeholder*="搜索代币"]')?.value ?? null;
    },
    countText() {
      return window.__t.countEl()?.textContent.trim() ?? null;
    },
  };
};

// ---- 1. 冷启动：reload 后测首行 / 全池 / 稳定 ----
await page.reload({ waitUntil: 'commit' });
const tReload = Date.now();
await page.waitForSelector('input[placeholder*="搜索代币"]', { timeout: 30_000 });
let firstRowMs = null;
let fullPoolMs = null;
let stableMs = null;
let lastCount = 0;
let stableSince = null;
for (let i = 0; i < 600; i += 1) {
  const n = await page.evaluate(() => document.querySelectorAll('tr[data-token-id]').length).catch(() => 0);
  const el = Date.now() - tReload;
  if (firstRowMs === null && n > 0) firstRowMs = el;
  if (fullPoolMs === null && n >= 500) fullPoolMs = el;
  if (n !== lastCount) {
    lastCount = n;
    stableSince = Date.now();
  } else if (stableSince && n > 0 && Date.now() - stableSince > 2500) {
    stableMs = Date.now() - tReload;
    break;
  }
  await page.waitForTimeout(100);
}
await page.evaluate(PAGE_HELPERS);
const vpTag = await page.evaluate(() => `${window.innerWidth}x${window.innerHeight}`);
result.steps.coldStart = {
  reloadAt: new Date(tReload).toISOString(),
  firstRowMs,
  fullPoolMs,
  stableMs,
  finalRowCount: lastCount,
  viewport: await page.evaluate(() => ({
    w: window.innerWidth,
    h: window.innerHeight,
    dpr: window.devicePixelRatio,
  })),
  emulatedViewport: VIEWPORT,
  note: 'Vite dev(HMR) 服务；首次编译已缓存，数值为运行时渲染耗时',
};
result.steps.domBaseline = await page.evaluate(() => window.__t.domCounts());
result.steps.domBaselineScreenshot = await shot(`${LABEL}-全列表-${vpTag}.png`);

// ---- 2. 安装观测器：longtask + MutationObserver ----
await page.evaluate(() => {
  window.__perf = { longtasks: [], mutations: { attr: 0, child: 0, character: 0 }, mutStart: performance.now() };
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        window.__perf.longtasks.push({ start: Math.round(e.startTime), dur: Math.round(e.duration) });
      }
    }).observe({ entryTypes: ['longtask'] });
  } catch {
    window.__perf.longtaskUnsupported = true;
  }
  const tb = document.querySelector('tbody');
  if (tb) {
    new MutationObserver((records) => {
      for (const r of records) {
        if (r.type === 'attributes') window.__perf.mutations.attr += 1;
        else if (r.type === 'childList') window.__perf.mutations.child += 1;
        else if (r.type === 'characterData') window.__perf.mutations.character += 1;
      }
    }).observe(tb, { attributes: true, childList: true, characterData: true, subtree: true });
  }
});

// ---- 3. 交互耗时：搜索逐字 / 清空 ----
const measureSearch = (v) =>
  page.evaluate(async (val) => {
    const t0 = performance.now();
    const before = window.__t.countText();
    window.__t.setSearch(val);
    let changedMs = null;
    for (let i = 0; i < 60; i += 1) {
      if (window.__t.countText() !== before) {
        changedMs = performance.now() - t0;
        break;
      }
      await new Promise((r) => requestAnimationFrame(r));
    }
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    return {
      input: val,
      countChangedMs: changedMs === null ? null : Number(changedMs.toFixed(1)),
      paintedMs: Number((performance.now() - t0).toFixed(1)),
      countText: window.__t.countText(),
      rowsAfter: document.querySelectorAll('tr[data-token-id]').length,
    };
  }, v);

result.steps.search = [];
for (const q of ['B', 'BT', 'BTC']) result.steps.search.push(await measureSearch(q));
result.steps.search.push(await measureSearch(''));

// ---- 4. 交互耗时：展开 / 收起 ----
const measureExpand = (tokenId) =>
  page.evaluate(async (id) => {
    const row = document.querySelector(`tr[data-token-id="${id}"]`);
    if (!row) return { tokenId: id, ok: false, reason: 'row-not-found' };
    const t0 = performance.now();
    row.click();
    let appearedMs = null;
    for (let i = 0; i < 240; i += 1) {
      const tr = document.querySelector(`tr[data-token-id="${id}"]`);
      const next = tr && tr.nextElementSibling;
      if (next && next.querySelector('svg[viewBox="0 0 1000 200"]')) {
        appearedMs = performance.now() - t0;
        break;
      }
      await new Promise((r) => requestAnimationFrame(r));
    }
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    return {
      tokenId: id,
      ok: appearedMs !== null,
      svgAppearedMs: appearedMs === null ? null : Number(appearedMs.toFixed(1)),
      paintedMs: Number((performance.now() - t0).toFixed(1)),
      domAfter: window.__t.domCounts(),
    };
  }, tokenId);

result.steps.expand = await measureExpand('binance:0GUSDT');
result.steps.expandScreenshot = await shot(`${LABEL}-展开0G-${vpTag}.png`);
result.steps.collapse = await measureExpand('binance:0GUSDT'); // 再点一次=收起
result.steps.collapse.domAfter = await page.evaluate(() => window.__t.domCounts());

// ---- 5. 滚动：滚到底再滚回顶，采 rAF 帧间隔 ----
await page.evaluate(() => {
  const el = window.__t.scroller();
  el.scrollTop = 0;
});
await page.waitForTimeout(300);
await page.evaluate(async () => {
  const el = window.__t.scroller();
  const frames = [];
  let last = performance.now();
  let stop = false;
  const loop = () => {
    const now = performance.now();
    frames.push(now - last);
    last = now;
    if (!stop) requestAnimationFrame(loop);
  };
  requestAnimationFrame(loop);
  const target = el.scrollHeight - el.clientHeight;
  const steps = 40;
  const per = 50;
  for (let i = 1; i <= steps; i += 1) {
    el.scrollTop = (target * i) / steps;
    await new Promise((r) => setTimeout(r, per));
  }
  for (let i = steps; i >= 0; i -= 1) {
    el.scrollTop = (target * i) / steps;
    await new Promise((r) => setTimeout(r, per));
  }
  await new Promise((r) => setTimeout(r, 300));
  stop = true;
  window.__scrollFrames = frames;
  window.__scrollRange = { max: target, reached: el.scrollTop };
});
const frames = await page.evaluate(() => window.__scrollFrames || []);
const sorted = [...frames].sort((a, b) => a - b);
const pct = (p) => (sorted.length ? sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * p))] : null);
result.steps.scroll = {
  frameCount: frames.length,
  avgMs: frames.length ? Number((frames.reduce((a, b) => a + b, 0) / frames.length).toFixed(2)) : null,
  p95Ms: pct(0.95) === null ? null : Number(pct(0.95).toFixed(2)),
  maxMs: frames.length ? Number(Math.max(...frames).toFixed(2)) : null,
  droppedOver50ms: frames.filter((f) => f > 50).length,
  scrollRange: await page.evaluate(() => window.__scrollRange || null),
  domAfterScroll: await page.evaluate(() => window.__t.domCounts()),
};
result.steps.scrollScreenshot = await shot(`${LABEL}-滚动到底-${vpTag}.png`);

// ---- 6. 刷新代价：CPU Profiler + longtask + MutationObserver ----
await page.evaluate(() => {
  window.__perf.mutations = { attr: 0, child: 0, character: 0 };
  window.__perf.longtasks = [];
  window.__perf.mutStart = performance.now();
});
await page.evaluate(() => {
  const el = window.__t.scroller();
  el.scrollTop = 0;
});
const client = await page.context().newCDPSession(page);
await client.send('Profiler.enable');
await client.send('Profiler.setSamplingInterval', { interval: 200 });
await client.send('Profiler.start');
await page.waitForTimeout(PROFILE_S * 1000);
const { profile } = await client.send('Profiler.stop');
await client.send('Profiler.disable');

const byFn = new Map();
for (const node of profile.nodes) {
  const cf = node.callFrame || {};
  const hits = node.hitCount || 0;
  if (!hits) continue;
  const key = `${cf.functionName || '(anonymous)'} @ ${(cf.url || '').split('/').pop() || '?'}:${(cf.lineNumber ?? -1) + 1}`;
  byFn.set(key, (byFn.get(key) || 0) + hits);
}
const totalHits = Array.from(byFn.values()).reduce((a, b) => a + b, 0);
result.steps.refreshCost = {
  profileSeconds: PROFILE_S,
  samplingIntervalUs: 200,
  totalSamples: totalHits,
  topFunctions: Array.from(byFn.entries())
    .sort((a, b) => b[1] - a[1])
    .slice(0, 18)
    .map(([fn, hits]) => ({ fn, hits, pct: Number(((hits / totalHits) * 100).toFixed(2)) })),
  longtasks: await page.evaluate(() => window.__perf.longtasks || []),
  longtaskUnsupported: await page.evaluate(() => !!window.__perf.longtaskUnsupported),
  mutations: await page.evaluate(() => window.__perf.mutations),
};
result.steps.refreshCost.longtaskSummary = {
  count: result.steps.refreshCost.longtasks.length,
  totalBlockingMs: result.steps.refreshCost.longtasks.reduce((a, b) => a + b.dur, 0),
  maxMs: result.steps.refreshCost.longtasks.reduce((a, b) => Math.max(a, b.dur), 0),
};
result.steps.refreshScreenshot = await shot(`${LABEL}-刷新窗口-${vpTag}.png`);

// ---- 7. 展开后滚离屏再回来：展开态与重图表应恢复（虚拟化的正确性验证） ----
const TARGET_ID = 'binance:0GUSDT';
const probeExpanded = (id) =>
  page.evaluate((tid) => {
    const row = document.querySelector(`tr[data-token-id="${tid}"]`);
    const next = row && row.nextElementSibling;
    const sc = window.__t.scroller();
    const rows = document.querySelectorAll('tr[data-token-id]');
    return {
      rowRendered: !!row,
      chartMounted: !!(next && next.querySelector('svg[viewBox="0 0 1000 200"]')),
      chevronDown: !!(row && row.querySelector('svg.lucide-chevron-down')),
      renderedRows: rows.length,
      firstRenderedId: rows.length ? rows[0].dataset.tokenId : null,
      scrollTop: sc ? Math.round(sc.scrollTop) : null,
      scrollH: sc ? sc.scrollHeight : null,
      clientH: sc ? sc.clientHeight : null,
    };
  }, id);

// 确保 0G 行已展开
await page.evaluate((id) => {
  const row = document.querySelector(`tr[data-token-id="${id}"]`);
  if (!row) return;
  const next = row.nextElementSibling;
  const expanded = !!(next && next.querySelector('svg[viewBox="0 0 1000 200"]'));
  if (!expanded) row.click();
}, TARGET_ID);
await page.waitForTimeout(2500);
const beforeAway = await probeExpanded(TARGET_ID);

// 滚到列表底部（展开行离开渲染窗口）
await page.evaluate(() => {
  const sc = window.__t.scroller();
  sc.scrollTop = sc.scrollHeight;
});
await page.waitForTimeout(1200);
const atBottom = await probeExpanded(TARGET_ID);

// 滚回顶部（展开行重新进入渲染窗口）
await page.evaluate(() => {
  window.__t.scroller().scrollTop = 0;
});
await page.waitForTimeout(1500);
const backTop = await probeExpanded(TARGET_ID);

result.steps.offscreenRemount = {
  targetId: TARGET_ID,
  beforeAway,
  atBottom,
  backTop,
  chartUnmountedWhenOffscreen: beforeAway.chartMounted && !atBottom.chartMounted,
  chartRestoredAfterScrollBack: backTop.chartMounted && backTop.chevronDown,
  scrollReturnedToTop: backTop.scrollTop === 0,
};
result.steps.offscreenScreenshot = await shot(`${LABEL}-滚回顶部展开恢复-${vpTag}.png`);

result.endedAt = new Date().toISOString();
fs.writeFileSync(OUT, JSON.stringify(result, null, 2));
console.log(JSON.stringify(result, null, 2));
// 清除视口覆盖并断开 CDP 会话；**不** browser.close()：避免关掉其它任务的浏览器实例。
if (emuClient) await emuClient.send('Emulation.clearDeviceMetricsOverride').catch(() => undefined);
await client.detach().catch(() => undefined);
// playwright 的 CDP 连接会挂住事件循环，显式退出（结果已落盘）。
process.exit(0);
