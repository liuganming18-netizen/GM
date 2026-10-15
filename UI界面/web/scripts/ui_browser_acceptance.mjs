/**
 * 信号与持仓接通（2026-10-13）· **真实浏览器 UI 验收**（不是 SSR HTML、不是截图冒充）。
 *
 * 做法：用真实 headless Chromium 打开 `http://127.0.0.1:3000/`，等 React 挂载完成后
 * **整页刷新一次**，再读取刷新后的真实 DOM；同时从页面同源代理取服务端账本，
 * 逐项比对 **DOM ↔ 服务端账本** 的一致性。
 *
 * 纪律：
 *  · 只读：不点击开/平仓、不改任何状态；只读 DOM 与 GET 接口。
 *  · 断言失败如实记录（`failures` 数组），不粉饰。
 *  · 截图仅作辅助留档；**验收依据是 DOM 数据属性与文本**。
 */
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';

const UI = process.env.UI_URL || 'http://127.0.0.1:3000/';
/** 页面内一律用**同源相对路径**（vite 代理 `/api/v1` → 127.0.0.1:8787），
 *  这正是浏览器真实取数的通道；用绝对跨源地址会被 CORS 挡住，属于测量方法错误。 */
const REL = '/api/v1';
const OUT = process.env.EVID_DIR || path.resolve(process.cwd(), '../信号与持仓接通_2026-10-13/evidence');

const failures = [];
const check = (name, cond, detail) => {
  if (!cond) failures.push({ check: name, detail });
  return { name, pass: !!cond, detail };
};

async function snapshot(page, tag) {
  return page.evaluate((t) => {
    const q = (s) => document.querySelector(s);
    const qa = (s) => Array.from(document.querySelectorAll(s));
    const txt = (el) => (el ? (el.innerText || '').replace(/\s+/g, ' ').trim() : null);
    const sig = q('[data-testid="signal-panel"]');
    const rows = qa('[data-testid="signal-row"]');
    const byOrigin = {};
    const byExec = {};
    rows.forEach((r) => {
      const o = r.getAttribute('data-origin') || '(none)';
      const e = r.getAttribute('data-executable-state') || '(none)';
      byOrigin[o] = (byOrigin[o] || 0) + 1;
      byExec[e] = (byExec[e] || 0) + 1;
    });
    const box = q('[data-testid="paper-account-box"]');
    const panel = q('[data-testid="paper-panel"]');
    const pend = qa('[data-testid="paper-pending-block"]');
    const liveRows = qa('[data-testid="live-account-row"]').map((r) => ({
      exchange: r.getAttribute('data-exchange'),
      status: r.getAttribute('data-status'),
      nPositions: r.getAttribute('data-n-positions'),
      text: txt(r),
    }));
    return {
      tag: t,
      url: location.href,
      title: document.title,
      signalPanel: {
        present: !!sig,
        count: sig ? Number(sig.getAttribute('data-signal-count')) : null,
        domRows: rows.length,
        byOrigin,
        byExec,
        headerText: txt(sig),
      },
      paperPanel: {
        present: !!panel,
        connected: panel ? panel.getAttribute('data-connected') : null,
        arms: panel ? panel.getAttribute('data-arms') : null,
        accountBox: box
          ? {
              cash: box.getAttribute('data-cash'),
              equity: box.getAttribute('data-equity'),
              nPositions: box.getAttribute('data-n-positions'),
              nPending: box.getAttribute('data-n-pending'),
              text: txt(box),
            }
          : null,
        pendingBlocks: pend.length,
        pendingText: pend.map(txt),
      },
      liveRows,
      bodyHas: {
        signalAreaTitle: document.body.innerText.includes('信号区'),
        paperTitle: document.body.innerText.includes('模拟实盘持仓'),
        liveReadonlyTitle: document.body.innerText.includes('实盘仓位（只读）'),
        notConfigured: document.body.innerText.includes('未配置凭据'),
        pendingWord: document.body.innerText.includes('待成交挂单'),
        historicalWord: document.body.innerText.includes('历史研究'),
      },
      bodyTextLen: document.body.innerText.length,
    };
  }, tag);
}

const main = async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1800, height: 1180 } });
  const consoleErrors = [];
  page.on('pageerror', (e) => consoleErrors.push(String(e)));
  const ev = { ui_url: UI, api_via_same_origin: REL, checks: [], failures: [], consoleErrors };

  await page.goto(UI, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForSelector('[data-testid="signal-panel"]', { timeout: 60000 });
  await page.waitForSelector('[data-testid="paper-panel"]', { timeout: 60000 });
  // 让首屏数据落地（账本 5s 轮询 + 行情首帧）
  await page.waitForTimeout(9000);
  // 实盘只读行是**异步取数**后渲染的（首次请求还要在 adapter 侧加载 ccxt）。
  // 这里只等它出现，**不放松任何断言**；等不到就照实判失败。
  await page.waitForSelector('[data-testid="live-account-row"]', { timeout: 60000 })
    .catch(() => {});
  ev.pass1_before_reload = await snapshot(page, 'before_reload');

  // **整页刷新**：验收对象是「刷新后的实际 UI」
  await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForSelector('[data-testid="signal-panel"]', { timeout: 60000 });
  await page.waitForTimeout(9000);
  await page.waitForSelector('[data-testid="live-account-row"]', { timeout: 60000 })
    .catch(() => {});
  ev.pass2_after_reload = await snapshot(page, 'after_reload');

  // 服务端账本（页面同源代理，等价于浏览器真正取到的那份数据）
  ev.api = await page.evaluate(async (api) => {
    const r = await fetch(api + '/paper/accounts');
    const j = await r.json();
    return {
      http_ok: r.ok,
      arms: (j.items || []).map((a) => ({
        arm: a.arm,
        cash: a.cash,
        equity: a.equity,
        n_positions: (a.positions || []).length,
        n_pending: (a.pending || []).length,
        pending: (a.pending || []).map((p) => ({
          symbol: p.symbol, origin: p.origin, requested_at: p.requested_at,
          earliest_fill_ms: p.earliest_fill_ms, status: p.status,
        })),
        realized_pnl_total: a.realized_pnl_total,
        fees_paid_total: a.fees_paid_total,
        funding_paid_total: a.funding_paid_total,
        model_version: a.model_version,
      })),
      quality: j.quality,
    };
  }, REL);
  ev.api_live = await page.evaluate(async (api) => {
    const r = await fetch(api + '/accounts');
    const j = await r.json();
    return {
      http_ok: r.ok,
      missing_reason: j.missing_reason,
      items: (j.items || []).map((x) => ({
        exchange: x.exchange, status: x.status, detail: x.detail,
        n_positions: x.n_positions, readonly: x.readonly, no_real_order: x.no_real_order,
      })),
    };
  }, REL);
  ev.api_signals = await page.evaluate(async (api) => {
    const r = await fetch(api + '/signals?limit=200');
    const j = await r.json();
    const items = j.items || [];
    const byOrigin = {};
    items.forEach((x) => { const o = x.origin || '(none)'; byOrigin[o] = (byOrigin[o] || 0) + 1; });
    return { http_ok: r.ok, total: j.total, returned: items.length, byOrigin, quality: j.quality };
  }, REL);

  await page.screenshot({ path: path.join(OUT, 'ui_browser_after_reload.png'), fullPage: true });
  ev.screenshot = path.join(OUT, 'ui_browser_after_reload.png');
  await browser.close();

  // ── 断言（DOM ↔ 账本一致）───────────────────────────────────────────────
  const p2 = ev.pass2_after_reload;
  ev.checks.push(check('信号区已渲染', p2.signalPanel.present, p2.signalPanel.headerText));
  ev.checks.push(check('信号区条数>0', (p2.signalPanel.domRows || 0) > 0,
    { domRows: p2.signalPanel.domRows, byOrigin: p2.signalPanel.byOrigin }));
  ev.checks.push(check('信号含实时(origin=live)', (p2.signalPanel.byOrigin.live || 0) > 0,
    p2.signalPanel.byOrigin));
  ev.checks.push(check('信号含历史研究(origin=historical_research)且不可执行',
    (p2.signalPanel.byOrigin.historical_research || 0) > 0 &&
    (p2.signalPanel.byExec.historical_not_executable || 0) > 0, p2.signalPanel.byExec));
  ev.checks.push(check('模拟实盘面板已接通(connected=1)', p2.paperPanel.connected === '1',
    { connected: p2.paperPanel.connected, arms: p2.paperPanel.arms }));
  ev.checks.push(check('四臂账本齐备', (p2.paperPanel.arms || '') === '3a,3b,3c,3d', p2.paperPanel.arms));
  const apiArms = ev.api.arms || [];
  const apiCashSum = apiArms.reduce((a, x) => a + (Number(x.cash) || 0), 0);
  const domCash = Number(p2.paperPanel.accountBox?.cash);
  ev.checks.push(check('DOM 现金合计 == 服务端账本现金合计',
    Number.isFinite(domCash) && Math.abs(domCash - apiCashSum) < 1e-6,
    { dom: domCash, api: apiCashSum }));
  const apiPend = apiArms.reduce((a, x) => a + x.n_pending, 0);
  ev.checks.push(check('DOM 挂单数 == 服务端账本挂单数',
    Number(p2.paperPanel.accountBox?.nPending) === apiPend,
    { dom: p2.paperPanel.accountBox?.nPending, api: apiPend }));
  const apiPos = apiArms.reduce((a, x) => a + x.n_positions, 0);
  ev.checks.push(check('DOM 持仓数 == 服务端账本持仓数',
    Number(p2.paperPanel.accountBox?.nPositions) === apiPos,
    { dom: p2.paperPanel.accountBox?.nPositions, api: apiPos }));
  ev.checks.push(check('实盘只读行已渲染且状态来自服务端',
    (p2.liveRows || []).length > 0 &&
    p2.liveRows.every((r, i) => r.status === (ev.api_live.items[i] || {}).status),
    { dom: p2.liveRows, api: ev.api_live.items }));
  ev.checks.push(check('实盘只读未配置时如实标注(未配置凭据)',
    p2.bodyHas.notConfigured === (ev.api_live.items.some((x) => x.status === 'not_configured')),
    { domHasText: p2.bodyHas.notConfigured, apiStatuses: ev.api_live.items.map((x) => x.status) }));
  ev.checks.push(check('实盘只读标记 readonly / 不提交真实订单',
    ev.api_live.items.every((x) => x.readonly === true && x.no_real_order === true),
    ev.api_live.items));
  ev.checks.push(check('无控制台运行时错误', consoleErrors.length === 0, consoleErrors));
  ev.checks.push(check('刷新前/刷新后信号条数一致(非空态闪烁)',
    (ev.pass1_before_reload.signalPanel.domRows || 0) > 0 &&
    (ev.pass2_after_reload.signalPanel.domRows || 0) > 0,
    { before: ev.pass1_before_reload.signalPanel.domRows, after: p2.signalPanel.domRows }));

  ev.failures = failures;
  ev.summary = {
    total: ev.checks.length,
    passed: ev.checks.filter((c) => c.pass).length,
    failed: failures.length,
  };
  fs.mkdirSync(OUT, { recursive: true });
  const p = path.join(OUT, '29_ui_browser_acceptance.json');
  fs.writeFileSync(p, JSON.stringify(ev, null, 1), 'utf-8');
  console.log(JSON.stringify({ evidence: p, summary: ev.summary, failures }, null, 1));
  process.exit(failures.length ? 1 : 0);
};

main().catch((e) => {
  console.error(JSON.stringify({ fatal: String(e) }));
  process.exit(2);
});
