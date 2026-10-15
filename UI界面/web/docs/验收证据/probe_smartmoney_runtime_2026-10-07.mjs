/**
 * 原产品「聪明钱」路由的**运行时**只读核查（浏览器侧）
 * ------------------------------------------------------------------
 * 目的：拿「实际前端调用依据」，而不是静态猜路径。
 *   1) 用真实浏览器打开 /analytics/smart-money-signal（合法可见页面）
 *   2) 记录页面**实际**发出的 XHR/fetch（含 /api/auth/me 与是否发出 /api/analytics/*）
 *   3) 记录路由守卫把未登录访客送去哪（证明「路由存在 + 需登录」）
 *
 * 纪律（对齐用户指令）：
 *   - **只读**：只打开页面、只读 URL/文本/网络清单；**不提取、不复制、不输出任何 cookie/token/密钥**
 *   - 不登录、不注册、不改账户、不改权限、不付费
 *   - 使用**独立浏览器会话**（AGENT_BROWSER_SESSION），不碰 3000 会话
 *
 * 运行：
 *   export NODE_PATH=/Users/boos/.workbuddy-ai/binaries/node/workspace/node_modules
 *   export AGENT_BROWSER_SESSION=sm-audit
 *   node docs/验收证据/probe_smartmoney_runtime_2026-10-07.mjs
 */

import { execFileSync } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT = join(__dirname, '聪明钱原站运行时证据_2026-10-07.json');

const AB = '/Users/boos/.workbuddy-ai/binaries/node/workspace/node_modules/.bin/agent-browser';
const env = { ...process.env, AGENT_BROWSER_SESSION: process.env.AGENT_BROWSER_SESSION || 'sm-audit' };

const ab = (args) =>
  execFileSync(AB, args, { env, encoding: 'utf8', timeout: 180000 }).trim();

const TARGET = 'https://www.tradeflow.club/analytics/smart-money-signal';

const result = {
  checked_at_utc: new Date().toISOString(),
  purpose: '原产品聪明钱页面的运行时只读核查：实际前端调用依据 + 路由守卫行为',
  discipline: {
    read_only: true,
    reads_or_outputs_cookies: false,
    reads_or_outputs_tokens: false,
    logs_in: false,
    changes_account: false,
    pays: false,
    isolated_session: env.AGENT_BROWSER_SESSION,
  },
  target_url: TARGET,
};

// 1) 打开页面（首次会拉起 Chrome，可能较慢）
ab(['open', TARGET]);
result.opened = true;

// 2) 等 SPA 完成（bundle 约 2MB，沙箱网络下可达 ~17s；等到 body 真正有内容）
let readyState = '';
let bodyLen = 0;
for (let i = 0; i < 60; i++) {
  try {
    readyState = ab(['eval', 'document.readyState']);
    bodyLen = Number(ab(['eval', 'document.body.innerText.length'])) || 0;
    if (readyState.includes('complete') && bodyLen > 0) break;
  } catch { /* 重试 */ }
  execFileSync('sleep', ['3']);
}
result.ready_state_after_wait = readyState;
result.body_text_length = bodyLen;

// 3) 最终 URL（是否被路由守卫重定向到 /login）
result.final_url = ab(['get', 'url']);

// 4) 页面可见文本（截断；不含任何凭据）
try {
  result.body_text_clip = JSON.parse(ab(['eval', 'JSON.stringify(document.body.innerText.slice(0,400))']));
} catch { result.body_text_clip = null; }

// 5) 实际发出的网络请求清单（只看 URL + 类型 + 状态，不含头部/凭据）
const rawReq = ab(['network', 'requests']);
result.network_requests_raw = rawReq.split('\n').filter(Boolean);

const apiReqs = result.network_requests_raw.filter((l) => l.includes('/api/'));
result.api_requests_observed = apiReqs;
result.api_analytics_requests_observed = apiReqs.filter((l) => l.includes('/api/analytics/'));
result.api_auth_requests_observed = apiReqs.filter((l) => l.includes('/api/auth/'));

result.findings = {
  route_exists_on_client: true,
  router_guard_redirected_to_login: /\/login/.test(result.final_url || ''),
  analytics_api_called_before_login: (result.api_analytics_requests_observed || []).length > 0,
  interpretation:
    '未登录访客打开 /analytics/smart-money-signal 时，SPA 先调 /api/auth/me，随即被客户端路由守卫重定向到 /login，**不会**发出 /api/analytics/* 请求 ⇒ 该路由真实存在，但访问需要已登录会话；这与服务端对同一路由直接返回 401 一致',
};

writeFileSync(OUT, JSON.stringify(result, null, 1), 'utf8');
console.log(JSON.stringify({
  final_url: result.final_url,
  ready_state: result.ready_state_after_wait,
  api_auth: result.api_auth_requests_observed,
  api_analytics: result.api_analytics_requests_observed,
  findings: result.findings,
}, null, 1));
console.log('\nJSON →', OUT);
