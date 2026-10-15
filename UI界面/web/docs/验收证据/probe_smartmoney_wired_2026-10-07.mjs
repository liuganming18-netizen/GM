/**
 * 聪明钱**接线**端到端验证（只读，不改工程；只访问本机 3000 与经其转发的币安公开端点）
 *
 * 验证目标（对应主助手验收第 1、2 条）：
 *   §1 列表列文案已更正：不再出现「内部占比 / 无公开源」；未采集到的行显示「暂无数据」
 *   §2 展开行时**真的发出**聪明钱请求（CDP Network 抓包，记录 URL / 状态码 / 命中路径）
 *   §3 展开面板里出现**真实值**（巨鲸占多/空持仓比、多空均价），且与同刻 bapi 响应一致
 *   §4 悬浮说明（title）已改为「来源 / 口径 / 纪律」表述，不再宣称「无公开源」
 *   §5 币安接口只覆盖币安合约 ⇒ OKX 行不请求、保持空态（不猜、不替代）
 *
 * 用法：
 *   CDP_URL=ws://... VIEWPORT=1800x1180 LABEL=聪明钱接线 \
 *   node probe_smartmoney_wired_2026-10-07.mjs <outJson>
 */
import fs from 'node:fs';

const PW_ENTRY =
  process.env.PLAYWRIGHT_CORE ??
  '/Users/boos/.workbuddy-ai/binaries/node/workspace/node_modules/playwright-core/index.js';
const pw = await import(PW_ENTRY);
const chromium = pw.chromium ?? pw.default?.chromium;

const CDP_URL = process.env.CDP_URL;
if (!CDP_URL) throw new Error('缺少 CDP_URL');
const OUT = process.argv[2] ?? '/tmp/smartmoney-wired.json';
const VIEWPORT = process.env.VIEWPORT ?? '1800x1180';
const LABEL = process.env.LABEL ?? '聪明钱接线';
const SHOT_DIR = process.env.SCREENSHOT_DIR ?? null;

const browser = await chromium.connectOverCDP(CDP_URL);
const ctx = browser.contexts()[0];
// 优先复用已打开的 3000 页面；若没有则在**同一上下文新开一页**。
// 注意：只新增标签页，**不关闭浏览器、不动其它标签**（connectOverCDP 下 close() 会关掉整个实例）。
let page = ctx.pages().find((p) => p.url().includes('127.0.0.1:3000'));
if (!page) {
  const before = ctx.pages().map((p) => p.url());
  page = await ctx.newPage();
  await page.goto('http://127.0.0.1:3000/', { waitUntil: 'domcontentloaded', timeout: 60000 });
  console.log(`[probe] 未找到 3000 页面（现有：${before.join(' | ')}），已在同一上下文新开一页`);
}
await page.bringToFront();

const [VW, VH] = VIEWPORT.split('x').map(Number);
const emu = await page.context().newCDPSession(page);
await emu.send('Emulation.setDeviceMetricsOverride', { width: VW, height: VH, deviceScaleFactor: 1, mobile: false });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const shot = async (name) => {
  if (!SHOT_DIR) return null;
  const p = `${SHOT_DIR}/${LABEL}-${name}-${VIEWPORT}.png`;
  await page.screenshot({ path: p });
  return p;
};

// ── 网络抓包：只记录聪明钱相关请求（不落任何凭据/响应体）────────────
// netLog 会在 §5 前清空以只统计「点开 OKX 行之后」的请求；
// allNetLog **从不清空**，用于最终留档（否则 §network 恒为空）。
const netLog = [];
const allNetLog = [];
const cdp = await page.context().newCDPSession(page);
await cdp.send('Network.enable');
cdp.on('Network.requestWillBeSent', (e) => {
  const u = e.request?.url ?? '';
  if (/smart-money|\/bapi\//.test(u)) {
    const rec = {
      phase: 'request',
      requestId: e.requestId,
      method: e.request?.method ?? null,
      url: u,
      // 证明没有携带任何凭据
      hasCookieHeader: !!e.request?.headers?.Cookie,
      hasAuthorizationHeader: !!e.request?.headers?.Authorization,
      requestHeaders: e.request?.headers ?? null,
    };
    netLog.push(rec);
    allNetLog.push(rec);
  }
});
cdp.on('Network.responseReceived', (e) => {
  const hits = allNetLog.filter((r) => r.requestId === e.requestId && r.phase === 'request');
  for (const hit of hits) {
    hit.status = e.response?.status ?? null;
    hit.mimeType = e.response?.mimeType ?? null;
  }
});

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

const result = {
  startedAt: new Date().toISOString(),
  emulatedViewport: VIEWPORT,
  label: LABEL,
  sections: {},
};

// ── §0 就绪 ─────────────────────────────────────────────
await page.reload({ waitUntil: 'domcontentloaded' });
await page.waitForSelector('tbody tr[data-token-id]', { timeout: 60000 });
await sleep(1500);

/** 读列表前 N 行的聪明钱两列文本 + title（**按内容定位列**，不写死下标） */
const readListSmartMoney = () =>
  page.evaluate(() => {
    const rows = Array.from(document.querySelectorAll('tbody tr[data-token-id]')).slice(0, 12);
    return rows.map((tr) => {
      const tds = Array.from(tr.querySelectorAll('td'));
      // 聪明钱列的唯一特征：单元格文本含「持仓额:」
      const smCells = tds.filter((td) => /持仓额:/.test(td.innerText || ''));
      const long = smCells[0] ?? null;
      const short = smCells[1] ?? null;
      return {
        id: tr.getAttribute('data-token-id'),
        tdCount: tds.length,
        smartMoneyCellCount: smCells.length,
        longText: long ? long.innerText.replace(/\s+/g, ' ').trim() : null,
        shortText: short ? short.innerText.replace(/\s+/g, ' ').trim() : null,
        longTitle: long ? (long.getAttribute('title') || '') : '',
      };
    });
  });

result.sections.listSmartMoney = await readListSmartMoney();
const allListText = result.sections.listSmartMoney
  .flatMap((r) => [r.longText ?? '', r.shortText ?? '', r.longTitle ?? ''])
  .join('\n');
result.sections.listLabelAudit = {
  mentionsWrongLabel内部占比: /内部占比/.test(allListText),
  mentionsWrongLabel无公开源: /无公开源/.test(allListText),
  mentions巨鲸占比: /巨鲸占比/.test(allListText),
  mentions暂无数据: /暂无数据/.test(allListText),
  titleMentionsBinanceBapi: /bapi\/futures\/v1\/public\/future\/smart-money\/signal/.test(allListText),
  titleMentions60sUpdate: /60\s*秒/.test(allListText),
};
result.sections.listScreenshot = await shot('列表聪明钱列');

// ── §2/§3 展开币安合约 → 抓包 + 读真实值 ─────────────────
await setSearch('0G');
await sleep(900);
const targetId = await page.evaluate(
  () => document.querySelector('tbody tr[data-token-id]')?.getAttribute('data-token-id') ?? null,
);
await page.evaluate(() => {
  const tr = document.querySelector('tbody tr[data-token-id]');
  tr?.querySelector('button')?.click();
});
await page.waitForFunction(() => !!document.querySelector('svg[viewBox="0 0 1000 200"]'), { timeout: 30000 }).catch(() => {});
// 币安 details/stats 实测可达 3.7s（且先要等一次 404 的 /api/v1/smart-money 探测），
// 留足时间，避免把「还没回来」误判成「没接线」。
await sleep(8000);

const expandedText = await page.evaluate(() => {
  const svg = document.querySelector('svg[viewBox="0 0 1000 200"]');
  const host = svg?.closest('div');
  // 展开面板整体（向上找足够大的容器）
  let node = svg;
  for (let i = 0; i < 12 && node?.parentElement; i++) {
    node = node.parentElement;
    if (node.innerText && node.innerText.length > 400) break;
  }
  const text = (node?.innerText ?? '').replace(/\s+/g, ' ').trim();
  const avgSpan = Array.from(node?.querySelectorAll('span') ?? []).find((s) =>
    /多空均价/.test(s.innerText || ''),
  );
  const ratioSpan = Array.from(node?.querySelectorAll('span') ?? []).find((s) =>
    /巨鲸占多\/空持仓比|聪明钱多空持仓/.test(s.innerText || ''),
  );
  return {
    ratioLine: ratioSpan ? ratioSpan.innerText.replace(/\s+/g, ' ').trim() : null,
    ratioTitle: ratioSpan ? (ratioSpan.getAttribute('title') || '') : '',
    avgLine: avgSpan ? avgSpan.innerText.replace(/\s+/g, ' ').trim() : null,
    avgTitle: avgSpan ? (avgSpan.getAttribute('title') || '') : '',
    panelExcerpt: text.slice(0, 700),
  };
});

result.sections.expandedTarget = targetId;
result.sections.expandedSmartMoney = {
  ratioLine: expandedText.ratioLine,
  avgLine: expandedText.avgLine,
  panelExcerpt: expandedText.panelExcerpt,
  ratioTitleHasBapiRoute: /bapi\/futures\/v1\/public\/future\/smart-money\/signal/.test(expandedText.ratioTitle),
  ratioTitleHas60sUpdate: /60\s*秒/.test(expandedText.ratioTitle),
  ratioTitleStillClaimsNoPublicSource: /无公开源|无公开聪明钱/.test(expandedText.ratioTitle),
  ratioTitleHasDisciplineLine: /不用 OI \/ VWAP \/ 人数比 \/ taker 量推算任何一槽/.test(expandedText.ratioTitle),
  avgTitleHasPerSideRealValues: /本行真实值/.test(expandedText.avgTitle),
  avgTitleHasBapiRoute: /bapi\/futures\/v1\/public\/future\/smart-money\/signal/.test(expandedText.avgTitle),
  ratioShowsRealPercent: /巨鲸占多\/空持仓比:\s*[\d.]+%/.test(expandedText.ratioLine ?? ''),
  avgShowsRealPrice: /多空均价:\s*\$[\d.]+/.test(expandedText.avgLine ?? ''),
};
result.sections.expandedScreenshot = await shot('展开聪明钱');

// ── §4 同刻对照：直接从同源 /bapi 取一次，与页面显示比对 ──────────
const live = await page.evaluate(async () => {
  const base = '/bapi/futures/v1/public/future/smart-money/signal';
  const get = async (u) => {
    const r = await fetch(u, { credentials: 'omit', cache: 'no-store' });
    return { status: r.status, body: await r.json().catch(() => null) };
  };
  const [ov, st] = await Promise.all([
    get(`${base}/overview?symbol=0GUSDT&timeRange=ALL`),
    get(`${base}/details/stats?symbol=0GUSDT&timeRange=ALL`),
  ]);
  return { ovStatus: ov.status, stStatus: st.status, ov: ov.body?.data ?? null, st: st.body?.data ?? null };
});
const pct = (a, b) => (typeof a === 'number' && typeof b === 'number' && b !== 0 ? Math.round((a / b) * 1000) / 10 : null);
result.sections.sameInstantReference = {
  via: '同源 /bapi（经 vite dev proxy → https://www.binance.bh，credentials:omit）',
  overviewHttpStatus: live.ovStatus,
  statsHttpStatus: live.stStatus,
  longPositionsUsd: live.st?.longPositions ?? null,
  shortPositionsUsd: live.st?.shortPositions ?? null,
  longWhaleSharePercent: pct(live.st?.longWhalePositions, live.st?.longPositions),
  shortWhaleSharePercent: pct(live.st?.shortWhalePositions, live.st?.shortPositions),
  longAvgEntryPrice: live.ov?.longTradersAvgEntryPrice ?? null,
  shortAvgEntryPrice: live.ov?.shortTradersAvgEntryPrice ?? null,
  longTraders: live.st?.longTraders ?? null,
  shortTraders: live.st?.shortTraders ?? null,
};
{
  const ref = result.sections.sameInstantReference;
  const rl = result.sections.expandedSmartMoney.ratioLine ?? '';
  const al = result.sections.expandedSmartMoney.avgLine ?? '';
  const nums = (s) => (s.match(/[\d]+\.?[\d]*/g) ?? []).map(Number);
  const rn = nums(rl);
  const an = nums(al);
  result.sections.crossCheck = {
    ratioLineNumbers: rn,
    avgLineNumbers: an,
    ratioMatchesReference:
      ref.longWhaleSharePercent !== null && rn.includes(ref.longWhaleSharePercent) &&
      ref.shortWhaleSharePercent !== null && rn.includes(ref.shortWhaleSharePercent),
    avgMatchesReferenceApprox:
      an.length >= 2 &&
      an.some((v) => Math.abs(v - ref.longAvgEntryPrice) / ref.longAvgEntryPrice < 0.02) &&
      an.some((v) => Math.abs(v - ref.shortAvgEntryPrice) / ref.shortAvgEntryPrice < 0.02),
    note: '页面值取自展开那一刻的请求；参考值取自随后同源再取一次，价格/占比会小幅漂移，故均价用 2% 相对容差、占比用精确匹配（占比在小数点后 1 位稳定）。',
  };
}

// ── §5 OKX 行不应发聪明钱请求 ────────────────────────────
// ⚠️ 不能按「名字里含某串」挑行：搜 'CRO' 会命中币安的 CROSSUSDT（≠ OKX 的 CRO-USDT-SWAP）。
// 改为直接按 `data-token-id` 前缀 `okx:` 定位，找不到就显式记 null（不假装通过）。
netLog.length = 0;
await setSearch('');
await sleep(800);
await page.evaluate(() => {
  const svg = document.querySelector('svg[viewBox="0 0 1000 200"]');
  const tr = svg?.closest('tr')?.previousElementSibling;
  tr?.querySelector('button')?.click();
});
await sleep(900);

// 依次试几个候选，直到渲染出的首行确实是 okx 行
let okxTarget = null;
for (const q of ['XDP', 'CNPY', 'AEON', 'BLEND']) {
  await setSearch(q);
  await sleep(800);
  const id = await page.evaluate(
    () => document.querySelector('tbody tr[data-token-id]')?.getAttribute('data-token-id') ?? null,
  );
  if (id && id.startsWith('okx:')) {
    okxTarget = id;
    break;
  }
}

let okxCell = null;
if (okxTarget) {
  netLog.length = 0; // 只统计「点开 OKX 行之后」发出的请求
  await page.evaluate((tid) => {
    document.querySelector(`tbody tr[data-token-id="${tid}"]`)?.querySelector('button')?.click();
  }, okxTarget);
  await sleep(6000);
  okxCell = await page.evaluate((tid) => {
    const tr = document.querySelector(`tbody tr[data-token-id="${tid}"]`);
    if (!tr) return null;
    const tds = Array.from(tr.querySelectorAll('td'));
    const sm = tds.find((td) => /持仓额:/.test(td.innerText || ''));
    return { id: tr.getAttribute('data-token-id'), longText: sm ? sm.innerText.replace(/\s+/g, ' ').trim() : null };
  }, okxTarget);
}
result.sections.okxRow = {
  targetId: okxTarget,
  foundOkxRow: !!okxTarget,
  cellText: okxCell,
  smartMoneyRequestsAfterOkxExpand: netLog.map((r) => r.url),
  noRequestForOkx: okxTarget ? netLog.length === 0 : null,
  screenshot: okxTarget ? await shot('OKX行无聪明钱请求') : null,
};

result.sections.network = {
  note: '页面自身发出的聪明钱请求（含 Cookie/Authorization 头探测，用于证明未携带凭据）。取自 allNetLog，不因 §5 清空 netLog 而丢失。',
  requests: allNetLog.map((r) => ({
    method: r.method,
    url: r.url,
    status: r.status ?? null,
    hasCookieHeader: r.hasCookieHeader,
    hasAuthorizationHeader: r.hasAuthorizationHeader,
  })),
};

result.endedAt = new Date().toISOString();
fs.writeFileSync(OUT, JSON.stringify(result, null, 1), 'utf8');
await emu.send('Emulation.clearDeviceMetricsOverride').catch(() => {});
console.log(JSON.stringify(result, null, 1));
console.log('\nJSON →', OUT);
process.exit(0);
