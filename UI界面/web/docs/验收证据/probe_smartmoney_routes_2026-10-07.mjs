/**
 * 聪明钱「真实 API 主机 / 路由 / 参数」只读核查探针
 * ------------------------------------------------------------------
 * 目的：
 *   1) 用 bundle 里**实际声明**的路由（不是猜的路径）逐个发**匿名只读 GET**，
 *      记录真实 HTTP 状态码与响应体片段 ⇒ 区分「404 路由不存在」与「401 需登录」。
 *   2) 复现上一份报告里 `tradeflow.club/api/analytics/smart-money-signal/BTCUSDT`
 *      的「301 → 404」，判断它是**地址/入口写错**还是**路由真的不存在**。
 *   3) 对照 `https://tradeflow.club`（无 www）与 `https://www.tradeflow.club`，
 *      说明 301 属于**主机名归一化**，与 API 本身无关。
 *
 * 纪律（对齐用户指令）：
 *   - 只读：全部 `method=GET`，`credentials:'omit'`（**不发送、不读取、不输出任何 cookie/token/密钥**）
 *   - 不改账户、不改权限、不付费、不购买数据
 *   - 只记录**无凭据部分**的 URL、状态码、响应体前若干字符
 *
 * 运行： node docs/验收证据/probe_smartmoney_routes_2026-10-07.mjs
 * 输出： docs/验收证据/聪明钱真实路由与状态码只读核查_2026-10-07.json
 */

import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT = join(__dirname, '聪明钱真实路由与状态码只读核查_2026-10-07.json');

const WWW = 'https://www.tradeflow.club';
const APEX = 'https://tradeflow.club';

/** bundle 里实际声明的服务方法 → 路由模板（见 tradeflow_index-27SKEJ8j.js offset 749001~751480） */
const DECLARED = [
  ['ST()',  '/api/analytics/smart-money-signal/symbols'],
  ['Sie()', '/api/analytics/smart-money-signal/BTCUSDT/kline?includeTopTraderRatios=true'],
  ['Aie()', '/api/analytics/smart-money-signal/rankings'],
  ['jie()', '/api/analytics/smart-money-signal/volatility-rankings'],
  ['Hie()', '/api/analytics/smart-money-signal/BTCUSDT/active-signal'],
  ['Mie()', '/api/analytics/coin-list'],
  ['Uie()', '/api/analytics/coin-list/BTCUSDT'],
  ['Nie()', '/api/analytics/funding-rates'],
  ['TT()',  '/api/analytics/feed'],
  ['ET()',  '/api/analytics/signals'],
  ['Vie()', '/api/analytics/signals/1'],
];

/** 上一份报告实际打过的路径（**不是** bundle 声明的任何一条） */
const PREVIOUS_WRONG = '/api/analytics/smart-money-signal/BTCUSDT';

const CLIP = 400;

async function probe(url, { method = 'GET' } = {}) {
  const startedAt = new Date().toISOString();
  const t0 = Date.now();
  try {
    const res = await fetch(url, {
      method,
      redirect: 'manual',          // 不跟随跳转：要如实记录 301 本身
      credentials: 'omit',         // 明确不带任何凭据
      headers: { 'Content-Type': 'application/json' },
    });
    const text = await res.text();
    return {
      url,
      request_method: method,
      http: res.status,
      status_text: res.statusText,
      location: res.headers.get('location'),
      content_type: res.headers.get('content-type'),
      set_cookie_present: res.headers.has('set-cookie'), // 只记录“是否存在”，不读取值
      body_clip: text.slice(0, CLIP),
      elapsed_ms: Date.now() - t0,
      started_at_utc: startedAt,
    };
  } catch (err) {
    return {
      url,
      request_method: method,
      error: String(err && err.message ? err.message : err),
      elapsed_ms: Date.now() - t0,
      started_at_utc: startedAt,
    };
  }
}

const result = {
  checked_at_utc: new Date().toISOString(),
  purpose:
    '聪明钱真实 API 主机/路由/参数只读核查：用 bundle 实际声明的路由区分 404(路由不存在) 与 401(需登录)；复核历史 301→404 是否地址/入口写错',
  discipline: {
    method: 'GET',
    credentials: 'omit',
    follows_redirect: false,
    reads_or_outputs_cookies: false,
    reads_or_outputs_tokens: false,
    changes_account: false,
    pays: false,
  },
  api_host_finding: {
    helper: 'xT(e,t,n,r) 用相对路径 fetch；headers 仅 {"Content-Type":"application/json"}；**无 Authorization 头**',
    implication:
      '所有 /api/analytics/* 均为**同源相对路径** ⇒ 真实 API 主机 = 前端主机 = https://www.tradeflow.club；鉴权走浏览器同源 cookie 会话，故匿名访问应得 401',
  },
  host_normalization: {},
  declared_routes: [],
  previously_probed_path: {},
};

// ── 1) 主机归一化：无 www 的 apex 域 → 301
result.host_normalization.apex = await probe(`${APEX}/api/analytics/smart-money-signal/symbols`);
result.host_normalization.www_root = await probe(`${WWW}/`);

// ── 2) bundle 实际声明的路由逐个探测（匿名）
for (const [fn, path] of DECLARED) {
  const r = await probe(`${WWW}${path}`);
  r.declared_by = fn;
  r.path_template = path;
  result.declared_routes.push(r);
}

// ── 3) 复现上一份报告打过的路径（不在声明清单里）
result.previously_probed_path = await probe(`${WWW}${PREVIOUS_WRONG}`);
result.previously_probed_path.note =
  '该路径不是 bundle 声明的任何路由：声明的是 /symbols、/{symbol}/kline、/rankings、/volatility-rankings、/{symbol}/active-signal；/api/analytics/smart-money-signal/BTCUSDT 把 {symbol} 直接当成了资源根，故 404 是路径写错的结果，不能据此判定「原站来源不可得」';

writeFileSync(OUT, JSON.stringify(result, null, 1), 'utf8');

// 控制台摘要
const pad = (s, n) => String(s).padEnd(n);
console.log(pad('HTTP', 6), pad('声明方法', 10), '路径');
console.log('-'.repeat(90));
console.log(pad(result.host_normalization.apex.http, 6), pad('(apex 无www)', 10), `${APEX}/api/analytics/smart-money-signal/symbols`, '→', result.host_normalization.apex.location || '');
for (const r of result.declared_routes) {
  console.log(pad(r.http ?? r.error, 6), pad(r.declared_by, 10), r.path_template);
}
console.log(pad(result.previously_probed_path.http ?? result.previously_probed_path.error, 6), pad('(历史路径)', 10), PREVIOUS_WRONG);
console.log('\nJSON →', OUT);
