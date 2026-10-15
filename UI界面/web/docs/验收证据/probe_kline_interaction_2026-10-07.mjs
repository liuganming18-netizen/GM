/**
 * 前端 K 线交互实测探针（只读，不改工程、不发上游请求）
 *
 * 连接既有 3000 页面（经 CDP），用**真实鼠标手势**验证：
 *   0) 自带状态准备：清空搜索 → 搜索 0G → 确保 0G 行展开（不依赖进入时的页面状态）
 *   1) 滚轮缩放：K 线视窗收窄/放宽，且页面不被滚动（证明非被动监听 preventDefault 生效）
 *   2) K 线 / 成交量 / OI 共用同一时间窗
 *   3) 拖动平移：视窗按真实像素位移
 *   4) 搜索 + 周期选择在 ≥95 秒（含 5 秒列表刷新）后仍保持
 *
 * 用法：
 *   CDP_URL=ws://127.0.0.1:PORT/devtools/browser/... \
 *   node probe_kline_interaction_2026-10-07.mjs <outJsonPath> [holdSeconds]
 */
import fs from 'node:fs';

/**
 * playwright-core 来自**既有托管 Node 工作区**（不往本前端工程安装依赖）。
 * 可用 PLAYWRIGHT_CORE 覆盖其入口路径。
 */
const PW_ENTRY =
  process.env.PLAYWRIGHT_CORE ??
  '/Users/boos/.workbuddy-ai/binaries/node/workspace/node_modules/playwright-core/index.js';
const pw = await import(PW_ENTRY);
const chromium = pw.chromium ?? pw.default?.chromium;

const CDP_URL = process.env.CDP_URL;
if (!CDP_URL) throw new Error('缺少 CDP_URL');
const OUT = process.argv[2] ?? '/tmp/kline-interaction-result.json';
const HOLD_S = Number(process.argv[3] ?? 0);
const SHOT_DIR = process.env.SCREENSHOT_DIR ?? null;
/**
 * 标准化视口。默认 1800x1180：
 * 展开图整体高约 900px，若窗口过矮（如 1280x577，表格滚动区仅 ~294px），
 * K 线画布会落在视口之外，`page.mouse.wheel` 的落点无效、收不到任何 wheel 事件。
 * 用 CDP Emulation 覆盖，保证「真实手势」打在画布上且多次测量可比。
 */
const VIEWPORT = process.env.VIEWPORT ?? '1800x1180';

const TARGET = 'binance:0GUSDT';
const CANDLE_PANE = 'svg[viewBox="0 0 1000 200"]';
const OI_PANE = 'svg[viewBox="0 0 1000 110"]';

/**
 * 读取状态：**严格限定在 0G 行的展开图表行内**（`tr[data-token-id] + tr`），
 * 不能扫整个 tbody —— 否则其它标的的展开图按钮会被误当成目标行。
 */
function readState() {
  const row = document.querySelector('tr[data-token-id="binance:0GUSDT"]');
  const expandTr = row && row.nextElementSibling && row.nextElementSibling.querySelector('svg[viewBox="0 0 1000 200"]')
    ? row.nextElementSibling
    : null;
  const candleSvg = expandTr ? expandTr.querySelector('svg[viewBox="0 0 1000 200"]') : null;
  const oiSvg = expandTr ? expandTr.querySelector('svg[viewBox="0 0 1000 110"]') : null;
  const viewMatch = expandTr ? expandTr.innerText.match(/视窗\s*([^·]+)/) : null;
  let scroller = candleSvg ? candleSvg.parentElement : null;
  while (scroller && !(scroller.scrollHeight > scroller.clientHeight)) scroller = scroller.parentElement;
  const tfButtons = expandTr
    ? Array.from(expandTr.querySelectorAll('button')).filter((b) =>
        /^(1m|5m|15m|30m|1h|4h|8h|12h|1d)$/.test((b.textContent || '').trim()),
      )
    : [];
  const selected = tfButtons.find((b) => /bg-emerald-600/.test(b.className));
  return {
    expanded: !!expandTr,
    view: viewMatch ? viewMatch[1].trim() : null,
    bars: candleSvg ? candleSvg.querySelectorAll('g').length : 0,
    oiCircles: oiSvg ? oiSvg.querySelectorAll('circle').length : 0,
    oiCx: oiSvg
      ? Array.from(oiSvg.querySelectorAll('circle')).map((c) => Number(c.getAttribute('cx')))
      : [],
    selectedTf: selected ? selected.textContent.trim() : null,
    winScrollY: Math.round(window.scrollY),
    innerScrollTop: scroller ? Math.round(scroller.scrollTop) : null,
    searchValue: document.querySelector('input[placeholder*="搜索代币"]')?.value ?? null,
    expandedRowIds: Array.from(document.querySelectorAll('tr[data-token-id]'))
      .filter((r) => r.nextElementSibling && r.nextElementSibling.querySelector('svg[viewBox="0 0 1000 200"]'))
      .map((r) => r.dataset.tokenId),
  };
}

const spanMinutes = (view) => {
  if (!view) return null;
  const [a, b] = view.split('–').map((s) => Date.parse(s));
  if (!Number.isFinite(a) || !Number.isFinite(b)) return null;
  return Number(((b - a) / 60000).toFixed(3));
};

const browser = await chromium.connectOverCDP(CDP_URL);
const ctx = browser.contexts()[0];
const page = ctx.pages().find((p) => p.url().includes('127.0.0.1:3000'));
if (!page) {
  throw new Error(
    `未找到 3000 页面（当前标签：${ctx.pages().map((p) => p.url()).join(' | ')}）。请先 agent-browser open http://127.0.0.1:3000`,
  );
}
await page.bringToFront();

const emuClient = await page.context().newCDPSession(page);
{
  const [w, h] = VIEWPORT.split('x').map(Number);
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

const result = { cdpUrl: CDP_URL, startedAt: new Date().toISOString(), emulatedViewport: VIEWPORT, steps: {} };

/** 记录主框架导航次数：用于判断长等待期间页面是否被整页刷新（会清空搜索/展开态）。 */
let navigations = 0;
page.on('framenavigated', (f) => {
  if (f === page.mainFrame()) navigations += 1;
});

// 在 document 冒泡阶段读取 defaultPrevented：只有非被动监听才能真正 preventDefault
await page.evaluate(() => {
  window.__wheelProbe = [];
  document.addEventListener(
    'wheel',
    (e) => {
      window.__wheelProbe.push({
        target: (e.target && e.target.tagName) || null,
        deltaY: e.deltaY,
        deltaX: e.deltaX,
        cancelable: e.cancelable,
        defaultPrevented: e.defaultPrevented,
      });
    },
    false,
  );
});

const paneBox = (selector) =>
  page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {
      x: r.left + r.width / 2,
      y: r.top + r.height / 2,
      w: r.width,
      h: r.height,
      inViewport: r.top >= 0 && r.bottom <= window.innerHeight && r.left >= 0 && r.right <= window.innerWidth,
      winH: window.innerHeight,
    };
  }, selector);

/**
 * 把目标画布滚到表格可视区中央，再取中心坐标。
 * 必要性：展开图整体高约 900px；若不做这一步，画布可能落在视口之外，
 * `page.mouse.wheel` / `mouse.move` 的落点无效（一个事件都收不到），
 * 会被误判成「缩放无效」。
 */
const ensurePaneVisible = async (selector) => {
  await page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (!el) return;
    let sc = el.parentElement;
    while (sc && !(sc.scrollHeight > sc.clientHeight + 4)) sc = sc.parentElement;
    if (!sc) return;
    const er = el.getBoundingClientRect();
    const sr = sc.getBoundingClientRect();
    sc.scrollTop += er.top - sr.top - (sc.clientHeight - er.height) / 2;
  }, selector);
  await page.waitForTimeout(300);
  return paneBox(selector);
};

// ---- 0. 状态准备：清空搜索 → 搜索 0G → 确保 0G 行展开 ----
await page.evaluate(() => {
  const i = document.querySelector('input[placeholder*="搜索代币"]');
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(i, '');
  i.dispatchEvent(new Event('input', { bubbles: true }));
});
await page.waitForTimeout(800);
await page.fill('input[placeholder*="搜索代币"]', '0G');
await page.waitForTimeout(2000);
const preExpanded = await page.evaluate(
  () => {
    const row = document.querySelector('tr[data-token-id="binance:0GUSDT"]');
    return !!(row && row.nextElementSibling && row.nextElementSibling.querySelector('svg[viewBox="0 0 1000 200"]'));
  },
);
if (!preExpanded) {
  await page.click('tr[data-token-id="binance:0GUSDT"] button');
  await page.waitForTimeout(1500);
}
/**
 * 等 K 线**真正画出来**（最多 30s）。
 * 固定等待会有两个坑：等待不足时画布是空态、高度不同，后续手势落点错位；
 * 而空态下的「缩放无效」会被误读成功能回归。
 */
await page
  .waitForFunction(
    () => {
      const row = document.querySelector('tr[data-token-id="binance:0GUSDT"]');
      const next = row && row.nextElementSibling;
      const svg = next && next.querySelector('svg[viewBox="0 0 1000 200"]');
      return !!svg && svg.querySelectorAll('g').length > 0;
    },
    null,
    { timeout: 30_000 },
  )
  .catch(() => undefined);
result.steps.setup = { preExpanded, afterSetup: await page.evaluate(readState) };

// ---- 1. 滚轮缩放（K 线画布，真实 wheel） ----
const cb = await ensurePaneVisible(CANDLE_PANE);
result.steps.paneBox = cb;
const before = await page.evaluate(readState);
await page.mouse.move(cb.x, cb.y);
await page.mouse.wheel(0, -240);
await page.waitForTimeout(400);
const afterZoomIn = await page.evaluate(readState);
await page.mouse.wheel(0, 240);
await page.waitForTimeout(400);
const afterZoomOut = await page.evaluate(readState);
result.steps.zoom = {
  before,
  afterZoomIn,
  afterZoomOut,
  spanBefore: spanMinutes(before.view),
  spanZoomIn: spanMinutes(afterZoomIn.view),
  spanZoomOut: spanMinutes(afterZoomOut.view),
  wheelProbe: await page.evaluate(() => window.__wheelProbe),
};
result.steps.zoom.screenshot = await shot('K线交互-滚轮缩放后-1800x1180.png');

// ---- 2. OI 画布滚轮 → 与 K 线共用同一视窗 ----
const ob = await ensurePaneVisible(OI_PANE);
result.steps.oiPaneBox = ob;
const beforeOi = await page.evaluate(readState);
await page.mouse.move(ob.x, ob.y);
await page.mouse.wheel(0, -240);
await page.waitForTimeout(400);
const afterOiZoom = await page.evaluate(readState);
result.steps.oiZoom = {
  before: beforeOi,
  after: afterOiZoom,
  spanBefore: spanMinutes(beforeOi.view),
  spanAfter: spanMinutes(afterOiZoom.view),
  candleViewChanged: beforeOi.view !== afterOiZoom.view,
  oiCxChanged: JSON.stringify(beforeOi.oiCx) !== JSON.stringify(afterOiZoom.oiCx),
};

// ---- 3. 拖动平移（真实 pointer 手势） ----
const db = (await ensurePaneVisible(CANDLE_PANE)) ?? cb;
result.steps.dragPaneBox = db;
// 注意：必须在 ensurePaneVisible **之后**读基线，否则滚动位置的差异会被误判成「页面被滚动」
const beforeDrag = await page.evaluate(readState);
await page.mouse.move(db.x, db.y);
await page.mouse.down();
for (let i = 1; i <= 6; i += 1) await page.mouse.move(db.x + (i * 180) / 6, db.y);
await page.mouse.up();
await page.waitForTimeout(400);
const afterDrag = await page.evaluate(readState);
result.steps.drag = {
  before: beforeDrag,
  after: afterDrag,
  viewChanged: beforeDrag.view !== afterDrag.view,
  spanKept: spanMinutes(beforeDrag.view) === spanMinutes(afterDrag.view),
  pageNotScrolled:
    beforeDrag.winScrollY === afterDrag.winScrollY && beforeDrag.innerScrollTop === afterDrag.innerScrollTop,
};

// ---- 4. 搜索 + 周期选择保持（≥95 秒） ----
if (HOLD_S > 0) {
  const expandedRow = page.locator('tr[data-token-id="binance:0GUSDT"] + tr');
  await expandedRow.locator('button', { hasText: /^1h$/ }).click();
  await page.fill('input[placeholder*="搜索代币"]', '0G');
  await page.waitForTimeout(2500);
  const holdStart = await page.evaluate(readState);
  const holdStartAt = new Date().toISOString();
  const navBefore = navigations;
  const shotStart = await shot('K线保持-95秒前-1h与搜索0G-1800x1180.png');
  await page.waitForTimeout(HOLD_S * 1000);
  const holdEnd = await page.evaluate(readState);
  const shotEnd = await shot('K线保持-95秒后-1h与搜索0G-1800x1180.png');
  result.steps.hold = {
    seconds: HOLD_S,
    holdStartAt,
    holdEndAt: new Date().toISOString(),
    pageNavigationsDuringHold: navigations - navBefore,
    shotStart,
    shotEnd,
    holdStart,
    holdEnd,
    searchKept: holdStart.searchValue === '0G' && holdEnd.searchValue === '0G',
    timeframeKept: holdStart.selectedTf === '1h' && holdEnd.selectedTf === '1h',
    stillExpanded: holdEnd.expandedRowIds.includes('binance:0GUSDT'),
    viewKept: holdStart.view === holdEnd.view,
  };
}

result.endedAt = new Date().toISOString();
fs.writeFileSync(OUT, JSON.stringify(result, null, 2));
console.log(JSON.stringify(result, null, 2));
// **不** browser.close()：CDP 连接下 close() 会关掉整个浏览器实例（会连带干掉其它任务的页面）。
await emuClient.send('Emulation.clearDeviceMetricsOverride').catch(() => undefined);
process.exit(0);
