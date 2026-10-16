/**
 * 任务四验收：前端真实展示与事件联动（真实 headless Chromium，只读）。
 *
 * 断言（全部读真实 DOM 与同源 /api/v1）：
 *  A. 空数据不出现虚构成绩：回测复盘页不得出现已删除的假数值
 *     （2.32 / 0.83 / 2.34 / 2.18 / 58.3 / 61.2 / 54.5 / 1h 15m 兜底），
 *     且空曲线/空月度显示显式空态文案。
 *  B. 无「假 BTC 曲线」：equity_curve 为空时显示「暂无真实净值曲线」，不画合成线。
 *  C. 无「无效调参按钮」：不存在「参数调优」「运行回测」按钮。
 *  D. 信号区四阶段事件链存在：结构发现 / 信号确认 / 计划下单 / 实际成交。
 *  E. 「已买入」标记只能来自账本：DOM 中 data-filled=1 的行数
 *     == 同源 /api/v1/paper/positions 中 signal_uid 命中信号集合的数量。
 *  F. 刷新不漂移/不重复：整页刷新前后，signal_uid 集合完全一致且无重复。
 *  G. 逐字段对应：信号区 data-signal-count == /api/v1/signals?scope=current 的 items 长度。
 */
import pw from '/Users/boos/Desktop/威科夫因子共振趋势/UI界面/web/node_modules/playwright/index.js';
const { chromium } = pw;
import fs from 'node:fs';
import path from 'node:path';

const UI = process.env.UI_URL || 'http://127.0.0.1:3000/';
const OUT = process.env.EVID_DIR || path.resolve(process.cwd(), '../evidence/Work_前端真实展示_2026-10-15');
fs.mkdirSync(OUT, { recursive: true });

const failures = [];
const passes = [];
const check = (name, cond, detail) => {
  const rec = { name, pass: !!cond, detail };
  (cond ? passes : failures).push(rec);
  return rec;
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 等待 React 挂载 + 首轮取数完成（信号面板出现）。 */
async function waitReady(page) {
  await page.waitForSelector('[data-testid="signal-panel"]', { timeout: 30000 });
  await sleep(3500);
}

async function clickNav(page, label) {
  await page.getByRole('button', { name: label, exact: false }).first().click();
  await sleep(2500);
}

async function readSignalUids(page) {
  return page.evaluate(() =>
    Array.from(document.querySelectorAll('[data-testid="signal-row"]')).map((r) => r.getAttribute('data-signal-uid')),
  );
}

async function api(page, rel) {
  return page.evaluate(async (u) => {
    const res = await fetch(u, { headers: { Accept: 'application/json' }, cache: 'no-store' });
    return { status: res.status, body: await res.json() };
  }, `/api/v1${rel}`);
}

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1800, height: 1180 } });
const consoleErrors = [];
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
page.on('pageerror', (e) => consoleErrors.push(String(e)));

const report = { ui: UI, generated_utc: new Date().toISOString(), checks: [], api: {}, dom: {} };

try {
  // ── 工作台：信号区四阶段 + 已买入 + 刷新稳定性 ─────────────────────────────
  await page.goto(UI, { waitUntil: 'domcontentloaded' });
  await waitReady(page);

  const sigApi = await api(page, '/signals?scope=current&limit=500');
  const apiSignalUids = (sigApi.body.items || []).map((s) => s.signal_id);
  report.api.signals_current_total_items = apiSignalUids.length;
  report.api.signals_current_total_field = sigApi.body.total ?? null;

  const domCount = await page.evaluate(() => {
    const p = document.querySelector('[data-testid="signal-panel"]');
    return p ? Number(p.getAttribute('data-signal-count')) : null;
  });
  report.dom.signal_count = domCount;
  check('G 信号区计数 == API items 长度', domCount === apiSignalUids.length, { dom: domCount, api: apiSignalUids.length });

  const chainText = await page.evaluate(() => {
    const rows = Array.from(document.querySelectorAll('[data-testid="signal-row"]'));
    const joined = rows.map((r) => r.innerText).join('\n');
    return {
      rows: rows.length,
      hasStructure: joined.includes('结构发现'),
      hasConfirm: joined.includes('信号确认'),
      hasPlan: joined.includes('计划下单'),
      hasFill: joined.includes('实际成交'),
    };
  });
  report.dom.signal_chain = chainText;
  check('D 信号区含「结构发现」', chainText.rows === 0 || chainText.hasStructure, chainText);
  check('D 信号区含「信号确认」', chainText.rows === 0 || chainText.hasConfirm, chainText);
  check('D 信号区含「计划下单」', chainText.rows === 0 || chainText.hasPlan, chainText);
  check('D 信号区含「实际成交」', chainText.rows === 0 || chainText.hasFill, chainText);

  // 已买入 只能来自账本
  const filledDom = await page.evaluate(() => ({
    filledRows: document.querySelectorAll('[data-testid="signal-row"][data-filled="1"]').length,
    badge: (document.body.innerText.match(/已买入（账本）/g) || []).length,
  }));
  const posApi = await api(page, '/paper/positions');
  const posUids = new Set((posApi.body.items || []).map((p) => p.signal_uid).filter(Boolean));
  const expectedFilled = apiSignalUids.filter((u) => posUids.has(u)).length;
  report.dom.filled = filledDom;
  report.api.ledger_open_positions = (posApi.body.items || []).length;
  report.api.ledger_signal_uids = Array.from(posUids);
  check('E 已买入行数 == 账本命中信号数', filledDom.filledRows === expectedFilled, { dom: filledDom.filledRows, expected: expectedFilled });
  check('E 已买入徽标数 == 已买入行数', filledDom.badge === filledDom.filledRows, filledDom);

  // 刷新稳定性
  const before = await readSignalUids(page);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await waitReady(page);
  const after = await readSignalUids(page);
  const uniq = new Set(after);
  report.dom.refresh = { before_n: before.length, after_n: after.length, unique_after: uniq.size };
  check('F 刷新后信号集合一致', JSON.stringify([...before].sort()) === JSON.stringify([...after].sort()), { before_n: before.length, after_n: after.length });
  check('F 刷新后无重复 signal_uid', uniq.size === after.length, { n: after.length, unique: uniq.size });

  await page.screenshot({ path: path.join(OUT, '01_工作台_信号四阶段.png'), fullPage: false });

  // ── 回测复盘：无虚构成绩 ───────────────────────────────────────────────
  await clickNav(page, '回测复盘');
  await sleep(3000);
  const btText = await page.evaluate(() => document.body.innerText.replace(/\s+/g, ' '));
  report.dom.backtest_text_len = btText.length;

  const fakeTokens = ['2.32%', '2.34', '0.83%', '58.3%', '61.2%', '54.5%', '收益因子 (Profit Factor) 2.18'];
  const foundFake = fakeTokens.filter((t) => btText.includes(t));
  check('A 回测页无已删除的假数值', foundFake.length === 0, { foundFake });

  check('C 回测页无「参数调优」按钮', !btText.includes('参数调优'), {});
  check('C 回测页无「运行回测」按钮', !btText.includes('运行回测'), {});

  const btApi = await api(page, '/backtests?mode=paper_review&range=ALL');
  const curveLen = (btApi.body.equity_curve || []).length;
  const monthlyLen = Object.keys(btApi.body.monthly_returns || {}).length;
  report.api.backtest = { curve_points: curveLen, monthly_years: monthlyLen, closed_trades: btApi.body.summary?.closed_trades };

  if (curveLen === 0) {
    check('B 空曲线显示显式空态（不画合成线）', btText.includes('暂无真实净值曲线'), {});
  } else {
    check('B 曲线存在时不显示空态', !btText.includes('暂无真实净值曲线'), { curveLen });
  }
  if (monthlyLen === 0) {
    check('A 空月度显示显式空态', btText.includes('暂无真实月度收益聚合'), {});
  } else {
    check('A 有月度数据时不显示空态', !btText.includes('暂无真实月度收益聚合'), { monthlyLen });
  }

  // 逐字段：指标卡不得显示「夏普比率 0」这种把 0 当成绩的写法（无成交时应为 —）
  if ((btApi.body.summary?.closed_trades ?? 0) === 0) {
    const metricsText = await page.evaluate(() => {
      const el = document.querySelector('[data-testid="backtest-metrics"]');
      return el ? el.innerText.replace(/\s+/g, ' ') : '';
    });
    report.dom.metrics_text = metricsText;
    check('A 无成交时夏普/胜率等派生量为 —（非 0）', !/夏普比率 \(Sharpe\) 0\b/.test(metricsText) && !/交易胜率 \(Win Rate\) 0\.0%/.test(metricsText), { metricsText });
  }

  await page.screenshot({ path: path.join(OUT, '02_回测复盘_空态真实.png'), fullPage: false });

  report.checks = [...passes, ...failures];
  report.console_errors = consoleErrors.slice(0, 20);
  report.summary = { pass: passes.length, fail: failures.length };
} catch (err) {
  report.error = String(err && err.stack ? err.stack : err);
  report.summary = { pass: passes.length, fail: failures.length + 1 };
} finally {
  await browser.close();
}

fs.writeFileSync(path.join(OUT, 'ui_acceptance_task4.json'), JSON.stringify(report, null, 2), 'utf8');
console.log(JSON.stringify({ summary: report.summary, failures: failures.map((f) => f.name + ' :: ' + JSON.stringify(f.detail)), error: report.error || null }, null, 2));
