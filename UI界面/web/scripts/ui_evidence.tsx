/**
 * 界面证据生成器（2026-10-13 信号与持仓接通）。
 *
 * 为什么不用浏览器截图：本机无 Chromium（磁盘高占用，不新装 ~500MB 浏览器）。
 * 改为**服务端渲染真实组件**：直接调用前端生产代码路径
 * （`client.ts: apiGet/apiPost` → `liveTrading.ts: fetchPaperLedger/fetchLiveAccounts/
 * postPaperOpen` → 组件）取**真实后端数据**，再用**生产构建产出的 CSS** 渲染成自包含 HTML。
 *
 * 因此本文件产出的是「真实组件 + 真实数据 + 真实样式」的渲染结果，而非示意图；
 * DOM 断言一并写入，便于逐条复核。
 *
 * 为保证「持仓渲染 / 平仓按钮」类断言可复核，脚本会**先确保账本里有一笔真实持仓**：
 * 若当前无持仓，就按真实的 `POST /api/v1/paper/orders` 走一遍开仓（与页面按钮同一条代码路径）。
 * 这属于本窗口允许的「页面模拟开仓」，不是伪造数据。
 *
 * 运行：
 *   EVIDENCE_API=http://127.0.0.1:8787 EVIDENCE_OUT=../信号与持仓接通_2026-10-13/evidence \
 *   node node_modules/tsx/dist/cli.mjs scripts/ui_evidence.tsx
 */
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { readFileSync, writeFileSync, readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..');
const BASE = process.env.EVIDENCE_API || 'http://127.0.0.1:8799';

// 把相对路径的 fetch 指到真实后端（浏览器里由 vite proxy 承担，Node 里由这里承担）
const origFetch = globalThis.fetch;
globalThis.fetch = ((input: any, init?: any) => {
  const url = typeof input === 'string' && input.startsWith('/') ? BASE + input : input;
  return origFetch(url, init);
}) as typeof fetch;

import { PositionPanel } from '../src/components/PositionPanel';
import { SignalListPanel } from '../src/components/SignalListPanel';
import {
  fetchLiveAccounts,
  fetchPaperLedger,
  postPaperOpen,
  type PaperLedger,
} from '../src/api/liveTrading';
import { adaptSignals } from '../src/api/adapters';
import { apiGet } from '../src/api/client';
import type { Envelope, RawSignal } from '../src/api/types';
import type { CryptoSignal } from '../src/types/crypto';

function renderPanel(ledger: PaperLedger, live: any, positions: PaperLedger['positions'], note: string | null) {
  return renderToStaticMarkup(
    React.createElement(PositionPanel, {
      paperAccounts: ledger.accounts,
      paperPositions: positions,
      paperMissingReason: ledger.missingReason,
      paperNote: note,
      liveAccounts: live.accounts,
      liveMissingReason: live.missingReason,
      orderBusy: false,
      onSelectTokenSymbol: () => {},
      onClosePosition: () => {},
      onOpenQuickPosition: () => {},
      orderNotice: null,
    }),
  );
}

async function ensureOnePosition(ledger: PaperLedger, signals: CryptoSignal[]) {
  if (ledger.positions.length > 0) {
    return { opened: null as string | null, note: '账本已有真实持仓，未新建' };
  }
  const openUids = new Set(ledger.positions.map((p) => p.signalUid).filter(Boolean) as string[]);
  const tried: string[] = [];
  for (const arm of ['3d', '3c']) {
    const acc = ledger.accounts.find((a) => a.arm === arm);
    if (!acc || acc.positions.length >= 10) continue; // 已批准 max_positions=10
    const cands = signals.filter(
      (s) => s.arm === arm && s.executableState === 'enterable' && s.detectionAtr && !openUids.has(s.id),
    );
    for (const cand of cands.slice(0, 12)) {
      // 已被处理过的 signal_uid 会被服务端以 409 duplicate_signal_uid 拒绝 ⇒ 换下一个
      try {
        const r = await postPaperOpen({
          arm,
          symbol: cand.rawSymbol,
          atr: cand.detectionAtr,
          signalUid: cand.id,
        });
        if (r.ok) return { opened: `${arm} ${cand.rawSymbol}`, note: '按服务端账本真实开仓一笔' };
        tried.push(`${arm}:${r.reason}`);
      } catch (e) {
        tried.push(`${arm}:${e instanceof Error ? e.message : String(e)}`);
      }
    }
  }
  return { opened: null, note: `未找到可开仓信号（不伪造持仓）；已试 ${tried.length} 个：${tried.slice(0, 4).join(', ')}` };
}

async function main() {
  let ledger = await fetchPaperLedger();
  const live = await fetchLiveAccounts();
  const sigEnv = await apiGet<Envelope<RawSignal>>('/signals?limit=500');
  const signals: CryptoSignal[] = adaptSignals(sigEnv, new Map());

  const ensured = await ensureOnePosition(ledger, signals);
  if (ensured.opened) ledger = await fetchPaperLedger();

  const panel = renderPanel(ledger, live, ledger.positions, ledger.qualityNote);
  // 空持仓态**单独渲染**（用真实账本 + 真实 note），以便断言「空态必须解释原因」
  const panelEmpty = renderPanel(ledger, live, [], ledger.qualityNote);

  const sigPanel = renderToStaticMarkup(
    React.createElement(SignalListPanel, {
      signals,
      selectedSignalId: null,
      onSelectSignal: () => {},
    }),
  );

  const cssFiles = readdirSync(join(ROOT, 'dist', 'assets')).filter((f) => f.endsWith('.css'));
  const css = cssFiles.map((f) => readFileSync(join(ROOT, 'dist', 'assets', f), 'utf8')).join('\n');

  // ── DOM 断言：逐条可复核 ────────────────────────────────────────────────
  const has = (s: string) => panel.includes(s) || sigPanel.includes(s);
  const assertions: Array<[string, boolean]> = [
    ['持仓面板出现「模拟实盘持仓」', panel.includes('模拟实盘持仓')],
    ['持仓面板出现「实盘仓位（只读）」', panel.includes('实盘仓位（只读）')],
    ['模拟持仓来自服务端账本（出现臂标签 3a/3b/3c/3d）', />(3a|3b|3c|3d)</.test(panel)],
    ['持仓卡片显示来源 signal_uid（可回溯到信号）', panel.includes('信号: structure_prototype_39|')],
    ['存在「平仓」按钮', panel.includes('平仓')],
    ['开仓按钮文案已改为「按信号开多」（不再本地造仓）', panel.includes('按信号开多')],
    ['做空按钮显式禁用', panel.includes('做空（已禁用）')],
    ['空持仓态解释原因（不让人误读成「没接通」）', panelEmpty.includes('为什么现在没有自动持仓')],
    ['空持仓态带出实时起点水位线口径', panelEmpty.includes('实时起点')],
    ['实盘只读不再是硬编码「未连接API」', !panel.includes('未连接 API')],
    ['实盘只读按真实状态渲染（出现已连接/未配置之一）', has('已连接') || has('未配置凭据') || has('尚无实盘账户快照')],
    ['信号区出现来源层徽标「实时」', />实时</.test(sigPanel)],
    ['信号区出现来源层徽标「历史研究」', />历史研究</.test(sigPanel)],
    ['信号区出现「展示 / 可执行」分列行', sigPanel.includes('展示 / 可执行')],
    ['信号区不再把 created_at 当触发时间（用确认时刻）', sigPanel.includes('周期 / 确认时刻')],
    ['历史信号被标为不可执行', sigPanel.includes('历史研究（不可执行）')],
  ];

  const html = `<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<title>信号与持仓接通 · 界面渲染证据（真实组件 + 真实数据）</title>
<style>${css}</style>
<style>
  body{margin:0;background:#0a0a0a;color:#e5e5e5;font-family:ui-sans-serif,system-ui,"PingFang SC",sans-serif}
  .wrap{display:flex;gap:16px;padding:16px;align-items:flex-start;flex-wrap:wrap}
  .box{height:860px;display:flex;flex-direction:column}
  .cap{font-size:12px;color:#a3a3a3;padding:6px 2px}
  .probe{background:#171717;border:1px solid #333;border-radius:8px;padding:10px 12px;font-size:11px;margin:0 16px 16px}
  .ok{color:#34d399}.bad{color:#fb7185}
  code{color:#93c5fd}
</style></head>
<body>
<div class="wrap">
  <div class="box"><div class="cap">① 信号区（SignalListPanel）— 实时/历史来源 + 展示态/可执行态分列</div>${sigPanel}</div>
  <div class="box"><div class="cap">② 模拟实盘持仓 + 实盘只读（PositionPanel）— 全部来自服务端账本</div>${panel}</div>
  <div class="box"><div class="cap">③ 空持仓态（同一组件、真实账本口径说明）— 解释「为什么现在没有自动持仓」</div>${panelEmpty}</div>
</div>
<div class="probe">
  <div><b>数据源</b>：<code>${BASE}/api/v1</code></div>
  <div>信号 ${signals.length} 条（实时 ${signals.filter((s) => s.origin === 'live').length}）｜纸面持仓 ${ledger.positions.length} 条｜实盘账户 ${live.accounts.length} 个</div>
  <div>本次开仓动作：${ensured.opened ?? '无'}（${ensured.note}）</div>
  <div style="margin-top:6px"><b>DOM 断言</b></div>
  ${assertions.map(([n, ok]) => `<div class="${ok ? 'ok' : 'bad'}">${ok ? '✓' : '✗'} ${n}</div>`).join('')}
</div>
</body></html>`;

  const outDir = process.env.EVIDENCE_OUT || join(ROOT, 'ui_evidence');
  const out = join(outDir, 'ui_render_evidence.html');
  writeFileSync(out, html, 'utf8');

  const failed = assertions.filter(([, ok]) => !ok);
  console.log(JSON.stringify({
    out,
    base: BASE,
    signals: signals.length,
    signals_live: signals.filter((s) => s.origin === 'live').length,
    paper_positions: ledger.positions.length,
    opened_by_harness: ensured.opened,
    live_accounts: live.accounts.length,
    live_statuses: live.accounts.map((a) => `${a.exchange}:${a.status}`),
    assertions_total: assertions.length,
    assertions_failed: failed.map(([n]) => n),
  }, null, 1));
  if (failed.length) process.exitCode = 2;
}

main().catch((e) => { console.error('RENDER_FAIL', e); process.exit(1); });
