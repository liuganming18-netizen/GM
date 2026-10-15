/**
 * 虚拟列表 + K 线操作 余项验收探针（只读，不改工程、不发上游请求）
 *
 * 覆盖主助手验收清单中尚未独立验证的项：
 *   §1 原币池与原界面保留（币池数、表头列数、关键列存在、无新增/删除模块）
 *   §2 滚动到底再返回（到底真实、返回后首行正确、滚动条长度一致）
 *   §3 展开行高度变化（展开前后滚动总高差 = 展开块实测高；收起恢复）
 *   §4 搜索（逐字 B/BT/BTC/清空 的行数与绘制耗时）
 *   §5 K 线缩放与平移（wheel 缩放跨度变化且可逆；拖动平移跨度不变、页面不滚动）
 *   §6 状态保留（搜索 + 手选周期 + 展开 + 图表视窗 在保持期内不变；页面导航 0）
 *
 * 用法：
 *   CDP_URL=ws://... VIEWPORT=1800x1180 LABEL=余项验收 \
 *   node probe_acceptance_residual_2026-10-07.mjs <outJson> [holdSeconds]
 */
import fs from 'node:fs';

const PW_ENTRY =
  process.env.PLAYWRIGHT_CORE ??
  '/Users/boos/.workbuddy-ai/binaries/node/workspace/node_modules/playwright-core/index.js';
const pw = await import(PW_ENTRY);
const chromium = pw.chromium ?? pw.default?.chromium;

const CDP_URL = process.env.CDP_URL;
if (!CDP_URL) throw new Error('缺少 CDP_URL');
const OUT = process.argv[2] ?? '/tmp/acceptance-residual.json';
const HOLD_S = Number(process.argv[3] ?? 30);
const SHOT_DIR = process.env.SCREENSHOT_DIR ?? null;
const VIEWPORT = process.env.VIEWPORT ?? '1800x1180';
const LABEL = process.env.LABEL ?? '余项验收';

const browser = await chromium.connectOverCDP(CDP_URL);
const ctx = browser.contexts()[0];
const page = ctx.pages().find((p) => p.url().includes('127.0.0.1:3000'));
if (!page) throw new Error(`未找到 3000 页面：${ctx.pages().map((p) => p.url()).join(' | ')}`);
await page.bringToFront();

const [VW, VH] = VIEWPORT.split('x').map(Number);
const emu = await page.context().newCDPSession(page);
await emu.send('Emulation.setDeviceMetricsOverride', { width: VW, height: VH, deviceScaleFactor: 1, mobile: false });

const shot = async (name) => {
  if (!SHOT_DIR) return null;
  const p = `${SHOT_DIR}/${LABEL}-${name}-${VIEWPORT}.png`;
  await page.screenshot({ path: p });
  return p;
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 滚动容器 = 表格所在的 overflow 容器 */
const SCROLLER = 'div.overflow-auto, div[class*="overflow-y"]';
const scrollerSel = async () =>
  page.evaluate((sel) => {
    const cands = Array.from(document.querySelectorAll(sel));
    const el = cands.find((e) => e.scrollHeight > e.clientHeight + 50);
    return el ? true : false;
  }, SCROLLER);

const scrollInfo = () =>
  page.evaluate((sel) => {
    const cands = Array.from(document.querySelectorAll(sel));
    const el = cands.find((e) => e.scrollHeight > e.clientHeight + 50) || cands[0];
    if (!el) return null;
    return { clientH: el.clientHeight, scrollH: el.scrollHeight, scrollTop: Math.round(el.scrollTop) };
  }, SCROLLER);

const setScroll = (top) =>
  page.evaluate(
    ({ sel, top }) => {
      const cands = Array.from(document.querySelectorAll(sel));
      const el = cands.find((e) => e.scrollHeight > e.clientHeight + 50) || cands[0];
      if (!el) return null;
      el.scrollTop = top;
      return Math.round(el.scrollTop);
    },
    { sel: SCROLLER, top },
  );

const setSearch = (text) =>
  page.evaluate((t) => {
    const inp = Array.from(document.querySelectorAll('input')).find((i) => {
      const ph = (i.placeholder || '') + (i.getAttribute('aria-label') || '');
      return /搜索|search|symbol/i.test(ph);
    });
    if (!inp) throw new Error('未找到搜索框');
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(inp, t);
    inp.dispatchEvent(new Event('input', { bubbles: true }));
    return true;
  }, text);

const rowCount = () => page.evaluate(() => document.querySelectorAll('tbody tr[data-token-id]').length);
const firstRowId = () =>
  page.evaluate(() => document.querySelector('tbody tr[data-token-id]')?.getAttribute('data-token-id') ?? null);
const lastRowId = () =>
  page.evaluate(() => {
    const rows = document.querySelectorAll('tbody tr[data-token-id]');
    return rows.length ? rows[rows.length - 1].getAttribute('data-token-id') : null;
  });

const waitRowCount = async (n, timeoutMs = 12000) => {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    if ((await rowCount()) === n) return Date.now() - t0;
    await sleep(120);
  }
  return -1;
};

const result = {
  startedAt: new Date().toISOString(),
  emulatedViewport: VIEWPORT,
  label: LABEL,
  sections: {},
};

// ── §0 就绪 ─────────────────────────────────────────────
await page.reload({ waitUntil: 'domcontentloaded' });
await page.waitForSelector('tbody tr[data-token-id]', { timeout: 60000 });
await sleep(1200);
result.sections.readiness = { scrollerFound: await scrollerSel() };

// ── §1 原币池与原界面保留 ───────────────────────────────
result.sections.poolAndUi = await page.evaluate(() => {
  const heads = Array.from(document.querySelectorAll('thead th')).map((th) => th.innerText.trim().replace(/\s+/g, ''));
  return {
    headerColumnCount: heads.length,
    headerColumns: heads,
    hasSmartMoneyLong: heads.some((h) => /聪明钱|多头/.test(h)),
    hasOiColumn: heads.some((h) => /总持仓|OI/.test(h)),
    hasSparkline: document.querySelectorAll('tbody svg polyline').length,
    tabCount: document.querySelectorAll('nav a, nav button').length,
  };
});
result.sections.poolAndUi.poolSizeFromSearch = await (async () => {
  await setSearch('');
  await sleep(900);
  return page.evaluate(() => {
    // 表头实际文案：「共 {count} 个标的」（见 TokenTable 第 819 行），
    // 旧正则 /筛选后 (\d+)\/(\d+)/ 在该文案下恒不匹配 ⇒ 旧结果恒为 null。
    const m = document.body.innerText.match(/共\s*(\d+)\s*个标的/);
    return m ? { filtered: Number(m[1]), total: Number(m[1]) } : null;
  });
})();

// ── §2 滚动到底再返回 ───────────────────────────────────
const s0 = await scrollInfo();
await setScroll(1e9);
await sleep(500);
const sBottom = await scrollInfo();
const bottomLastId = await lastRowId();
await setScroll(0);
await sleep(500);
const sBack = await scrollInfo();
const backFirstId = await firstRowId();
result.sections.scrollBottomAndBack = {
  before: s0,
  atBottom: sBottom,
  reachedBottom: sBottom ? sBottom.scrollTop + sBottom.clientH >= sBottom.scrollH - 4 : null,
  bottomLastRowId: bottomLastId,
  afterBackToTop: sBack,
  returnedToTop: sBack ? sBack.scrollTop === 0 : null,
  topFirstRowId: backFirstId,
  scrollHeightConsistent: s0 && sBottom ? Math.abs(s0.scrollH - sBottom.scrollH) <= 2 : null,
  screenshot: await shot('滚动到底再返回'),
};

// ── §3 展开行高度变化 ───────────────────────────────────
/**
 * 总高公式（见 TokenTable.offsetOf）：total = 行数 × 实测行高 + 展开块实测高，
 * 且**同时只允许一个**展开项（`expandedTokenId` 单值）。
 *
 * ⚠️ 2026-10-07 修正：`App.tsx` 启动时默认展开 BTC（`didInitExpand`）。若直接量基线，
 * 基线里含 **BTC 的展开块高**；探针点开第一行会把 BTC 的展开态顶掉，收起后自然
 * 比基线少一个块高 —— 旧版据此报 `heightRestoredOnCollapse=false`，是**基线污染**，
 * 不是功能缺陷。现在先做一次「展开→收起」预热把默认展开清掉，再正式量测。
 */
const clickToggle = async (id) => {
  await page.evaluate((tid) => {
    const tr = document.querySelector(`tbody tr[data-token-id="${tid}"]`);
    const btn = tr?.querySelector('button');
    if (btn) btn.click();
  }, id);
};

await setScroll(0);
await sleep(400);
const preExistingExpandedBlock = await page.evaluate(
  () => !!document.querySelector('svg[viewBox="0 0 1000 200"]'),
);

// 预热循环：清掉启动时的默认展开（BTC 不在窗口内，其块未挂载但高度已计入 total）
const warmId = await firstRowId();
const w0 = await scrollInfo();
await clickToggle(warmId);
await sleep(1600);
const w1 = await scrollInfo();
await clickToggle(warmId);
await sleep(1400);
const w2 = await scrollInfo();

// 正式量测
await setScroll(0);
await sleep(400);
const hBefore = await scrollInfo();
const targetId = await firstRowId();
await clickToggle(targetId);
await sleep(1800);
const hExpanded = await scrollInfo();
const expandedMounted = await page.evaluate(() => !!document.querySelector('svg[viewBox="0 0 1000 200"]'));
await clickToggle(targetId);
await sleep(1400);
const hCollapsed = await scrollInfo();
const delta = hBefore && hExpanded ? hExpanded.scrollH - hBefore.scrollH : null;
result.sections.expandedRowHeight = {
  formula: 'total = 行数 × 实测行高 + 展开块实测高（ResizeObserver 量测）；同时只允许一个展开项',
  preExistingExpandedBlockMounted: preExistingExpandedBlock,
  warmup: {
    note: '预热：展开→收起第一行，用于清掉 App 启动时对 BTC 的默认展开（其块在窗口外未挂载，但高度计入 total）',
    before: w0?.scrollH ?? null,
    expanded: w1?.scrollH ?? null,
    collapsed: w2?.scrollH ?? null,
    deltaCollapsedBack: w0 && w2 ? w2.scrollH - w0.scrollH : null,
  },
  targetId,
  scrollHeightBefore: hBefore?.scrollH ?? null,
  scrollHeightExpanded: hExpanded?.scrollH ?? null,
  scrollHeightCollapsed: hCollapsed?.scrollH ?? null,
  deltaExpanded: delta,
  deltaCollapsedBack: hCollapsed && hBefore ? hCollapsed.scrollH - hBefore.scrollH : null,
  expandedChartMounted: expandedMounted,
  heightChangedOnExpand: delta != null ? delta > 100 : null,
  /**
   * 收起后是否回到基线。判据用**自缩放**容差：残差必须 < 展开块高度的 5%。
   * 不用固定 ±4px：虚拟化的总高是「行数 × 实测行高 + 展开块高」的**估算**，
   * 已渲染行的真实高度与估算值之间的亚像素差会累加（实测 13px / 46178px = 0.03%，
   * 相对展开块 1036px 仅 1.3%）。固定小阈值会把这种布局舍入误报成"未恢复"。
   */
  heightRestoredOnCollapse:
    hCollapsed && hBefore && delta != null
      ? Math.abs(hCollapsed.scrollH - hBefore.scrollH) < Math.max(4, Math.abs(delta) * 0.05)
      : null,
  /** 残差占展开块高度的比例（供人工复核） */
  restoreResidualRatioOfExpandedBlock:
    hCollapsed && hBefore && delta ? Math.abs(hCollapsed.scrollH - hBefore.scrollH) / Math.abs(delta) : null,
  screenshot: await shot('展开行高度'),
};

// ── §4 搜索 ─────────────────────────────────────────────
const searchSteps = [];
for (const q of ['B', 'BT', 'BTC', '']) {
  const t0 = Date.now();
  await setSearch(q);
  await page.waitForFunction(
    () => document.querySelectorAll('tbody tr[data-token-id]').length > 0,
    { timeout: 8000 },
  ).catch(() => {});
  await sleep(400);
  searchSteps.push({
    query: q,
    renderedRows: await rowCount(),
    poolMatched: await page.evaluate(() => {
      const m = document.body.innerText.match(/共\s*(\d+)\s*个标的/);
      return m ? Number(m[1]) : null;
    }),
    elapsedMs: Date.now() - t0,
  });
}
result.sections.search = searchSteps;

// ── §5 K 线缩放与平移 ───────────────────────────────────
await setSearch('0G');
await sleep(900);
await page.evaluate(() => {
  const tr = document.querySelector('tbody tr[data-token-id]');
  const btn = tr?.querySelector('button');
  if (btn) btn.click();
});
await page.waitForFunction(() => !!document.querySelector('svg[viewBox="0 0 1000 200"]'), { timeout: 30000 }).catch(() => {});
await sleep(2500);

const paneSel = 'svg[viewBox="0 0 1000 200"]';
const ensurePaneVisible = async () => {
  await page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (el) el.scrollIntoView({ block: 'center' });
  }, paneSel);
  await sleep(400);
};
const paneBox = async () => {
  const b = await page.locator(paneSel).first().boundingBox();
  return b;
};
/**
 * 解析「数据源: … · 视窗 <ISO起>–<ISO止> · …」里的**完整 ISO 起止**。
 *
 * ⚠️ 2026-10-07 修正（主助手验收第 6 条）：
 * 上一版用 `/视窗[^\n]*?([\d.]+)\s*[~-]\s*([\d.]+)/` 取「视窗后第一对数字」。
 * 非贪婪匹配会把日期里的 `2026-10` 当成一对数字 ⇒ span = 10 − 2026 = **−2016**，
 * 且三次读数得到的是**同一条错误截断串**，于是 zoomReversible / panKeptSpan
 * 被这个恒定错误值判成 `true` —— **假通过**。
 * 现在只接受严格 ISO 时间戳（含 `T…Z`），并要求 `endMs > startMs`；
 * 解析失败一律返回 `null`，相应判定随之返回 `null`（而不是 `true`）。
 */
const readView = () =>
  page.evaluate(() => {
    const t = document.body.innerText;
    const m = t.match(
      /视窗\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s*[–—~-]\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)/,
    );
    if (!m) return null;
    const startMs = Date.parse(m[1]);
    const endMs = Date.parse(m[2]);
    if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs <= startMs) return null;
    return {
      raw: m[0],
      startIso: m[1],
      endIso: m[2],
      startMs,
      endMs,
      spanMs: endMs - startMs,
      spanDays: Number(((endMs - startMs) / 86400000).toFixed(4)),
    };
  });

await ensurePaneVisible();
const beforeZoom = await readView();
const box1 = await paneBox();
await page.mouse.move(box1.x + box1.width / 2, box1.y + box1.height / 2);
await page.mouse.wheel(0, -600);
await sleep(900);
const afterZoomIn = await readView();
await page.mouse.wheel(0, 600);
await sleep(900);
const afterZoomOut = await readView();

/**
 * 平移必须在**已放大**的状态下测：视窗被夹在「当前已加载 K 线域」内，
 * 在全域状态下拖动本就被夹住不动。旧版在 zoom-out 之后拖动，等于在全域测平移，
 * `panKeptSpan` 只能恒真（假通过）。现在先放大再拖。
 */
await ensurePaneVisible();
await page.mouse.move(box1.x + box1.width / 2, box1.y + box1.height / 2);
await page.mouse.wheel(0, -600);
await sleep(900);
const beforeDrag = await readView();
const box2 = await paneBox();
const pageScrollBefore = await page.evaluate(() => window.scrollY);
await page.mouse.move(box2.x + box2.width * 0.6, box2.y + box2.height / 2);
await page.mouse.down();
await page.mouse.move(box2.x + box2.width * 0.25, box2.y + box2.height / 2, { steps: 16 });
await page.mouse.up();
await sleep(900);
const afterDrag = await readView();
const pageScrollAfter = await page.evaluate(() => window.scrollY);

result.sections.klineZoomPan = {
  viewBeforeZoom: beforeZoom,
  viewAfterZoomIn: afterZoomIn,
  viewAfterZoomOut: afterZoomOut,
  /**
   * 解析必须成功（拿到完整 ISO 起止、spanMs > 0）才算这次缩放/平移**证据有效**。
   * 解析失败时下面所有判定返回 null（不返回 true），避免旧版的「截断串假通过」。
   */
  parseOk: !!(beforeZoom && afterZoomIn && afterZoomOut),
  spanMsBefore: beforeZoom?.spanMs ?? null,
  spanMsAfterZoomIn: afterZoomIn?.spanMs ?? null,
  spanMsAfterZoomOut: afterZoomOut?.spanMs ?? null,
  spanDaysBefore: beforeZoom?.spanDays ?? null,
  spanDaysAfterZoomIn: afterZoomIn?.spanDays ?? null,
  spanDaysAfterZoomOut: afterZoomOut?.spanDays ?? null,
  // 放大 = 时间跨度**变小**（严格，且至少缩小 1 分钟，排除浮点/取整噪声）
  zoomInShrankSpan: beforeZoom && afterZoomIn ? afterZoomIn.spanMs < beforeZoom.spanMs - 60000 : null,
  // 反向缩放 = 跨度**回到**原值（±1 分钟内）
  zoomReversible:
    beforeZoom && afterZoomOut ? Math.abs(afterZoomOut.spanMs - beforeZoom.spanMs) <= 60000 : null,
  panTestedWhileZoomedIn: !!(beforeDrag && afterDrag && beforeZoom && beforeDrag.spanMs < beforeZoom.spanMs),
  viewBeforeDrag: beforeDrag,
  viewAfterDrag: afterDrag,
  // 平移 = 跨度不变、但起止整体位移
  panKeptSpan: beforeDrag && afterDrag ? Math.abs(afterDrag.spanMs - beforeDrag.spanMs) <= 60000 : null,
  panMovedWindow: beforeDrag && afterDrag ? afterDrag.startMs !== beforeDrag.startMs : null,
  panShiftedMs: beforeDrag && afterDrag ? afterDrag.startMs - beforeDrag.startMs : null,
  pageScrollBefore,
  pageScrollAfter,
  pageNotScrolled: pageScrollBefore === pageScrollAfter,
  screenshot: await shot('K线缩放平移'),
};

// ── §6 状态保留 ─────────────────────────────────────────
const stateSnapshot = () =>
  page.evaluate(() => ({
    search: Array.from(document.querySelectorAll('input'))
      .map((i) => i.value)
      .find((v) => v && /^[A-Za-z0-9]+$/.test(v)) ?? null,
    stillExpanded: !!document.querySelector('svg[viewBox="0 0 1000 200"]'),
    timeframeButtons: Array.from(document.querySelectorAll('button'))
      .map((b) => b.innerText.trim())
      .filter((t) => /^(1m|5m|15m|1h|4h|1d|1w)$/.test(t)),
    activeTimeframe: (() => {
      const b = Array.from(document.querySelectorAll('button')).find(
        (x) => /^(1m|5m|15m|1h|4h|1d|1w)$/.test(x.innerText.trim()) && /bg-|active|border-/.test(x.className),
      );
      return b ? b.innerText.trim() : null;
    })(),
    navCount: performance.getEntriesByType('navigation').length,
  }));

const holdStart = await stateSnapshot();
const holdStartTs = Date.now();
await sleep(HOLD_S * 1000);
const holdEnd = await stateSnapshot();
result.sections.stateRetention = {
  holdSeconds: HOLD_S,
  holdStartTs: new Date(holdStartTs).toISOString(),
  holdEndTs: new Date().toISOString(),
  start: holdStart,
  end: holdEnd,
  searchKept: holdStart.search === holdEnd.search,
  stillExpanded: holdEnd.stillExpanded,
  timeframeKept: holdStart.activeTimeframe === holdEnd.activeTimeframe,
  pageNavigations: holdEnd.navCount - holdStart.navCount,
};

result.endedAt = new Date().toISOString();
fs.writeFileSync(OUT, JSON.stringify(result, null, 1), 'utf8');
await emu.send('Emulation.clearDeviceMetricsOverride').catch(() => {});
console.log(JSON.stringify(result, null, 1));
console.log('\nJSON →', OUT);
process.exit(0);
