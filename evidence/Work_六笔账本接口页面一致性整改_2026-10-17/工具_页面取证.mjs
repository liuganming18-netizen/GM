/**
 * 回测复盘页 · 六笔交易页面级验收取证（Playwright，真实浏览器）。
 *
 * 用 `?run=<run_id>` 深链直接打开指定 run（⑥ 列表只取最近 50 条且排序不稳定，
 * 指定 run 可能不在列表里 —— 深链是确定性入口）。
 *
 * 每个 run 采集：
 *   ① 页面截图（整页）
 *   ② DOM 断言：K 线 canvas 存在 / 逐笔行可点 / 单笔详情可读（订单 2 条 + 止损轨迹 + 资金费）
 *   ③ 选中交易后：止损轨迹读数（应为「仅初始止损 …，无后续调整 ⇒ 不合成轨迹」）
 *   ④ ③ 资金分析面板切「水下回撤」并给出「该笔区间」
 *   ⑤ ⑤/⑥ 面板文本快照（逐笔表 + 订单表）
 *
 * 用法：node 工具_页面取证.mjs
 */
import { chromium } from '/Users/boos/Desktop/威科夫因子共振趋势/UI界面/web/node_modules/playwright/index.mjs';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// ⚠ 必须用 fileURLToPath：路径含中文，`new URL(...).pathname` 是百分号编码串，
// 直接 join 会在 Desktop 下建出一个 `%E5%A8%81...` 的错位目录（实测踩过）。
const HERE = path.dirname(fileURLToPath(import.meta.url));
const SHOT = path.join(HERE, '页面截图');
fs.mkdirSync(SHOT, { recursive: true });

const RUNS = [
  ['LPTUSDT', 'run_WX_EVENT_TREND_V0_1_LPTUSDT_1792173591', 'trd_eb941d2defdde27285ef06e2'],
  ['OPUSDT', 'run_WX_EVENT_TREND_V0_1_OPUSDT_1792172950', 'trd_a01814754027b85386a054ca'],
  ['ORDIUSDT', 'run_WX_EVENT_TREND_V0_1_ORDIUSDT_1792171568', 'trd_4b7e88ebe45d27376fb18ca0'],
  ['ACEUSDT', 'run_WX_EVENT_TREND_V0_1_ACEUSDT_1792171577', 'trd_d574e8cd27bd34df692444e9'],
  ['BNBUSDT', 'run_WX_EVENT_TREND_V0_1_BNBUSDT_1792172973', 'trd_ae5861c5781bd75236471b3e'],
  ['MOVRUSDT', 'run_WX_EVENT_TREND_V0_1_MOVRUSDT_1792171582', 'trd_6d6be42bdeed0ceb56044e2c'],
];

const problems = [];
const out = [];

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1680, height: 1000 }, deviceScaleFactor: 2 });

try {
  for (const [sym, rid, tid] of RUNS) {
    const rec = { symbol: sym, run_id: rid, trade_id: tid };
    await page.goto(`http://127.0.0.1:3000/?run=${rid}`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('[data-testid="lab-workbench"]', { timeout: 60000 });
    await page.waitForSelector('[data-testid="lab-chart-panel"]', { timeout: 60000 });
    // 等 K 线 canvas 与逐笔行就绪
    await page.waitForSelector('[data-testid="lab-chart-panel"] canvas', { timeout: 60000 });
    await page.waitForSelector(`tr[data-trade-id="${tid}"]`, { timeout: 60000 });
    await page.waitForTimeout(2500);

    const canvasCount = await page.locator('[data-testid="lab-chart-panel"] canvas').count();
    rec.chart_canvas = canvasCount;
    if (canvasCount === 0) problems.push(`${sym}: 执行视图无 canvas（K 线未渲染）`);

    // ④ 面板读数（未选中交易时不应出现「已选中」）
    const panelBefore = await page.locator('[data-testid="lab-chart-panel"]').innerText();
    rec.panel_before_has_selected = panelBefore.includes('已选中');
    if (rec.panel_before_has_selected) problems.push(`${sym}: 未点选即显示「已选中」`);

    await page.screenshot({ path: path.join(SHOT, `页面_${sym}_01_总览.png`), fullPage: true });

    // 点选逐笔行 ⇒ 图表定位 + 单笔详情
    await page.locator(`tr[data-trade-id="${tid}"]`).click();
    await page.waitForTimeout(2000);

    const panelAfter = await page.locator('[data-testid="lab-chart-panel"]').innerText();
    rec.panel_after = panelAfter.replace(/\s+/g, ' ').slice(0, 600);
    if (!panelAfter.includes(tid)) problems.push(`${sym}: 选中后未显示真实 trade_id`);
    if (!panelAfter.includes('仅初始止损')) {
      problems.push(`${sym}: 未如实标注「仅初始止损…无后续调整 ⇒ 不合成轨迹」`);
    }

    // ③ 资金分析联动（「该笔区间」在 ③ 面板，不在 ④）
    const fundSec = page.locator('[data-lab-section="lab-funding"]');
    if (!(await fundSec.count())) {
      problems.push(`${sym}: 未找到 ③ 资金分析面板`);
    } else {
      const fTxt = (await fundSec.innerText()).replace(/\s+/g, ' ');
      rec.funding_panel = fTxt.slice(0, 900);
      if (!fTxt.includes('该笔区间')) problems.push(`${sym}: ③ 面板未给出「该笔区间」`);
      const uw = fundSec.locator('button', { hasText: '水下回撤' }).first();
      const uwCls = (await uw.count()) ? ((await uw.getAttribute('class')) || '') : '';
      rec.funding_mode_underwater = uwCls.includes('--ds-accent');
      if (!rec.funding_mode_underwater) {
        problems.push(`${sym}: ③ 面板未切到「水下回撤」（按钮未高亮）`);
      }
    }

    // 单笔详情（折叠卡）展开
    const fold = page.locator('[data-testid="lab-trade-detail-fold"]');
    await fold.locator('summary').click();
    await page.waitForSelector('[data-testid="lab-trade-detail"]', { timeout: 30000 });
    await page.waitForTimeout(1200);
    const detailTxt = (await page.locator('[data-testid="lab-trade-detail"]').innerText()).replace(/\s+/g, ' ');
    rec.trade_detail = detailTxt.slice(0, 1400);
    if (!detailTxt.includes(tid)) problems.push(`${sym}: 单笔详情 trade_id 不符`);
    const orderRows = await page.locator('[data-testid="lab-trade-detail"] tbody tr').count();
    rec.detail_order_rows = orderRows;
    if (orderRows < 2) problems.push(`${sym}: 单笔详情订单行 ${orderRows} < 2`);
    if (!detailTxt.includes('保护止损轨迹')) problems.push(`${sym}: 单笔详情无止损轨迹区块`);
    if (!detailTxt.includes('资金费事件')) problems.push(`${sym}: 单笔详情无资金费区块`);

    await page.screenshot({ path: path.join(SHOT, `页面_${sym}_02_选中与单笔详情.png`), fullPage: true });
    // 关键区块单拍（整页截图受单屏仪表盘内滚动限制，看不到下方区块）
    await page.locator('[data-testid="lab-trade-detail"]').screenshot({
      path: path.join(SHOT, `区块_${sym}_03_单笔详情.png`) });
    await page.locator('[data-lab-section="lab-funding"]').screenshot({
      path: path.join(SHOT, `区块_${sym}_04_资金分析联动.png`) });

    // ⑤ 逐笔表快照
    // 毛盈亏/手续费/资金费/trade_id 是**次要列**（渐进披露：默认隐藏）⇒ 先点「更多列」。
    const tradesPanel = page.locator('[data-testid="lab-trades"]');
    await tradesPanel.locator('button', { hasText: '更多列' }).first().click();
    await page.waitForTimeout(600);
    const tradesTxt = (await tradesPanel.innerText()).replace(/\s+/g, ' ');
    rec.trades_panel = tradesTxt.slice(0, 800);
    if (!tradesTxt.includes(tid)) problems.push(`${sym}: ⑤ 逐笔表未显示真实 trade_id`);

    // ⑤ 逐笔表首行的**单元格原值**（页面侧三方对照用，直接取 DOM 文本，不重算）
    const cells = await page.locator(`tr[data-trade-id="${tid}"] td`).allInnerTexts();
    rec.page_row_cells = cells.map((x) => x.replace(/\s+/g, ' ').trim());

    // ⑦ 订单面板（默认折叠在「更多视图 → 订单明细」，点开取证）
    await page.locator('[data-testid="lab-panel-orders"]').click();
    await page.waitForTimeout(1200);
    const ordersPanel = page.locator('[data-testid="lab-orders"]');
    if (await ordersPanel.count()) {
      // `trade_id` 是**次要列**（渐进披露：默认隐藏）⇒ 点「更多列」展开后再取证。
      await ordersPanel.locator('button', { hasText: '更多列' }).first().click();
      await page.waitForTimeout(800);
      const oTxt = (await ordersPanel.innerText()).replace(/\s+/g, ' ');
      rec.orders_panel = oTxt.slice(0, 900);
      if (!oTxt.includes(tid)) problems.push(`${sym}: ⑦ 订单面板未显示关联 trade_id`);
      await ordersPanel.screenshot({ path: path.join(SHOT, `区块_${sym}_05_订单明细.png`) });
    } else {
      problems.push(`${sym}: ⑦ 订单面板未出现`);
    }

    out.push(rec);
    console.log(`  ✓ ${sym}: canvas=${canvasCount} 订单行=${orderRows}`);
  }
} finally {
  await browser.close();
}

fs.writeFileSync(path.join(HERE, '页面验收_2026-10-17.json'),
  JSON.stringify({ runs: out, problems }, null, 2), 'utf-8');
console.log(`problems=${problems.length}`);
for (const p of problems) console.log('  ✗', p);
process.exit(problems.length ? 1 : 0);
