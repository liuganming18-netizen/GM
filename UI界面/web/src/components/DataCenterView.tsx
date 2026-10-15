/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * 数据中心（按「数据中心全页面草图.svg」渲染）。
 * 原版所有展示位为静态空态；本次仅把**已有展示位**绑定到真实只读接口：
 *   /pool/status、/data/status、/data/import-plan
 * 不新增页面/字段/操作，也不启动任何采集、回测或交易任务。
 * 接口未提供的项保持原版空态 '—'，不补 0、不写假百分比。
 */

import React, { useMemo, useState } from 'react';
import {
  Database,
  Search,
  ChevronDown,
  Layers,
  Clock,
  AlertTriangle,
  RefreshCw,
  Info,
  CheckCircle2,
  FileSpreadsheet
} from 'lucide-react';
import { useEndpoint } from '../api/useEndpoint';
import type { RawDataStatus, RawImportPlan, RawPoolStatus } from '../api/types';

/** 与工作台一致的低频只读刷新：15s 一轮，不对全池扫明细。 */
const REFRESH_MS = 15_000;

/** 表格展示的周期顺序与原版一致（1m 已于 2026-10-11 彻底删除，不再列）。 */
const TF_ROWS: { id: string; name: string }[] = [
  { id: '5m', name: '5分钟' },
  { id: '15m', name: '15分钟' },
  { id: '30m', name: '30分钟' },
  { id: '1h', name: '1小时' },
  { id: '4h', name: '4小时' },
  { id: '8h', name: '8小时' },
  { id: '12h', name: '12小时' },
  { id: '1d', name: '日线' },
];

/** 任务队列顺序取自 /data/import-plan 的真实 order，回退到原版顺序。 */
const QUEUE_LABEL: Record<string, string> = {
  '1d': '日线',
  '12h': '12小时',
  '8h': '8小时',
  '4h': '4小时',
  '1h': '1小时',
  '30m': '30分钟',
  '15m': '15分钟',
  '5m': '5分钟',
};

const fmtInt = (v: number | null | undefined): string =>
  v === null || v === undefined || !Number.isFinite(v) ? '—' : Math.round(v).toLocaleString();

const fmtDay = (ms: number | null | undefined): string => {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return '—';
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return '—';
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}`;
};

/**
 * 真实入库 / 同步时间快照。
 *
 * 来源：复用 App 级已加载的**全池响应**里 `coverage[].source.imported_at`
 * （官方归档文件导入数据库的时刻），**不额外发请求**。
 * 与 `coverage[].latest`（K 线止点 = 交易事件时间）严格区分：
 * `last_close` 不能当同步时间，`imported_at` 才是。
 */
export interface ImportedAtSnapshot {
  latest: string | null;
  byTimeframe: Record<string, string>;
}

interface DataCenterViewProps {
  importedAt?: ImportedAtSnapshot;
  /** App当前工作池身份集合；与行情列表同口径，自动排除加密指数和旧范围残留。 */
  activeMarketKeys?: string[];
}

/**
 * ISO-8601 UTC → 日期 `YYYY-MM-DD`（与原版「实际最早/最新」同为日期粒度，卡片与列宽不变）。
 * 无法解析时 '—'。
 */
const fmtImportedDay = (iso: string | null | undefined): string => {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '—';
  return new Date(t).toISOString().slice(0, 10);
};

/** ISO-8601 UTC → 时刻 `HH:MM`（UTC）。无法解析时 '—'。 */
const fmtImportedClock = (iso: string | null | undefined): string => {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '—';
  return new Date(t).toISOString().slice(11, 16);
};

export const DataCenterView: React.FC<DataCenterViewProps> = ({ importedAt, activeMarketKeys }) => {
  const [activeSubTab, setActiveSubTab] = useState<'kline' | 'oi' | 'funding' | 'smart_money'>('kline');
  const [selectedCycle, setSelectedCycle] = useState<string>('5m');

  const dataStatus = useEndpoint<RawDataStatus>('/data/status', REFRESH_MS);
  const poolStatus = useEndpoint<RawPoolStatus>('/pool/status', REFRESH_MS);
  const importPlan = useEndpoint<RawImportPlan>('/data/import-plan', REFRESH_MS);

  const rawCoverage = dataStatus.data?.coverage ?? null;
  const activeSet = useMemo(() => new Set(activeMarketKeys ?? []), [activeMarketKeys]);
  // App尚未给出币池身份时不回退全库coverage，避免指数或历史旧范围混入。
  const coverage = rawCoverage ? rawCoverage.filter((row) => activeSet.has(`${row.exchange}:${row.symbol}`)) : null;
  const scopeTotal = activeSet.size || null;
  /**
   * 只读接口失败时的**可读原因**。
   *
   * 缺陷背景（本轮修复）：`useEndpoint` 在失败时把原因写进 `.error` 并保留上一次成功数据，
   * 但本页此前**从不读取 `.error`** —— 首次加载失败时表格永久显示「读取中」，
   * 与「正在加载」不可区分（实测：注入 /data/status 故障后 18s+ 仍全表「读取中」，无任何提示）。
   * 修法：只在**既有展示槽**里如实反映失败（状态列文案 + 覆盖卡片说明），
   * 不新增模块/字段/操作，且明确「读取中/读取失败」都**不代表真实为 0**。
   */
  const dataError = dataStatus.error;
  const okxScopeTotal = Array.from(activeSet).filter((key) => key.startsWith('okx:')).length;
  const supportedTotal = (tf: string) => tf === '8h' && scopeTotal !== null ? scopeTotal - okxScopeTotal : scopeTotal;

  /** 按周期聚合真实覆盖记录：实际最早/最新/缺根/来源/状态。 */
  const klineRows = useMemo(() => {
    if (!coverage) return TF_ROWS.map((t) => ({ ...t, target: '—', early: '—', latest: '—', missing: '—', updated: '—', source: '—', status: dataError ? '读取失败' : '读取中' }));
    if (scopeTotal === null) return TF_ROWS.map((t) => ({ ...t, target: '—', early: '—', latest: '—', missing: '—', updated: '—', source: '—', status: dataError ? '读取失败' : '等待当前币池' }));
    const retention = importPlan.data?.plan?.retention ?? null;
    const targetOf = (tf: string): string => {
      if (!retention) return '—';
      if (retention[tf]) return retention[tf];
      return retention['30m及以上'] ?? '—';
    };

    return TF_ROWS.map((t) => {
      const rows = coverage.filter((c) => c.timeframe === t.id);
      if (rows.length === 0) {
        const supported = supportedTotal(t.id);
        const unsupported = t.id === '8h' ? okxScopeTotal : 0;
        return { ...t, target: targetOf(t.id), early: '—', latest: '—', missing: '—', updated: '—', source: '—', status: `已入库 0/${supported ?? '—'}${unsupported ? ` · OKX无原生 ${unsupported}` : ''}` };
      }
      const firsts = rows.map((r) => r.first_open).filter((v): v is number => typeof v === 'number');
      const lasts = rows.map((r) => r.last_close).filter((v): v is number => typeof v === 'number');
      const knownMissing = rows.reduce((a, r) => a + (r.missing_within_observed_range ?? 0), 0);
      const unknownMissing = rows.filter((r) => r.missing_within_observed_range === null || r.missing_within_observed_range === undefined).length;
      const missingText = unknownMissing === rows.length ? `未知 ${unknownMissing} 组合` : `${fmtInt(knownMissing)}${unknownMissing ? `（另 ${unknownMissing} 组合未知）` : ''}`;
      const exchanges = Array.from(new Set(rows.map((r) => r.exchange))).sort();
      const marketCount = new Set(rows.map((r) => `${r.exchange}:${r.symbol}`)).size;
      const latest = lasts.length ? Math.max(...lasts) : null;
      return {
        ...t,
        target: targetOf(t.id),
        early: firsts.length ? fmtDay(Math.min(...firsts)) : '—',
        latest: latest === null ? '—' : fmtDay(latest),
        missing: missingText,
        // 「最后更新」= 该周期的**入库时间**，取自全池响应 coverage[].source.imported_at；
        // coverage.last_close 是**交易事件时间（K 线止点）**，不能拿来冒充同步时间。
        // 该周期没有任何入库记录时保持空态。
        updated: fmtImportedDay(importedAt?.byTimeframe?.[t.id]),
        source: exchanges.length ? exchanges.map((e) => (e === 'binance' ? 'Binance' : e === 'okx' ? 'OKX' : e)).join(' / ') : '—',
        status: `已入库 ${marketCount}/${supportedTotal(t.id) ?? '—'} · 首末范围缺 ${missingText}${t.id === '8h' && okxScopeTotal ? ` · OKX无原生 ${okxScopeTotal}` : ''}`,
      };
    });
  }, [coverage, importPlan.data, importedAt, scopeTotal, okxScopeTotal, dataError]);

  /** 四张状态卡：全部来自真实接口。 */
  const summary = useMemo(() => {
    const combos = scopeTotal === null || !coverage ? null : coverage.length;
    const totalRows = scopeTotal === null || !coverage
      ? null
      : coverage.reduce((a, r) => a + (r.rows ?? 0), 0);
    const knownMissingTotal = coverage
      ? coverage.reduce((a, r) => a + (r.missing_within_observed_range ?? 0), 0)
      : null;
    const unknownMissingTotal = coverage
      ? coverage.filter((r) => r.missing_within_observed_range === null || r.missing_within_observed_range === undefined).length
      : null;
    const missingTotal = scopeTotal === null || coverage === null ? null : unknownMissingTotal === 0
      ? fmtInt(knownMissingTotal)
      : unknownMissingTotal === coverage.length ? `未知 ${unknownMissingTotal} 组合`
      : `${fmtInt(knownMissingTotal)}（另 ${unknownMissingTotal} 组合未知）`;
    // 不再从 coverage.last_close 推导「最后同步时间」：那是交易事件时间，不是入库时间。
    // /data/status 未提供入库/同步时间字段（time 各子字段均为 null）→ 该卡保持空态。

    const runs = poolStatus.data?.recent_runs ?? null;
    const latestRun = runs && runs.length ? runs[0] : null;
    const tasks = poolStatus.data?.download_tasks ?? null;

    return { combos, totalRows, missingTotal, latestRun, tasks };
  }, [coverage, poolStatus.data, scopeTotal]);

  /** 任务队列：按周期聚合真实 download_tasks。 */
  const queueItems = useMemo(() => {
    const order = importPlan.data?.plan?.order ?? ['1d', '12h', '8h', '4h', '1h', '30m', '15m', '5m'];
    const tasks = poolStatus.data?.download_tasks ?? null;
    return order.map((tf) => {
      const label = QUEUE_LABEL[tf] ?? tf;
      if (!tasks) return { tf, period: label, status: '—' };
      const mine = tasks.filter((t) => t.timeframe === tf);
      const parts = mine.map((t) => {
        const name = t.status === 'done' ? 'done日志' : t.status === 'missing_only' ? '补缺日志' : t.status === 'failed' ? '失败日志' : `${t.status}日志`;
        return `${name} ${fmtInt(t.n)}`;
      });
      const loaded = coverage?.filter((row) => row.timeframe === tf).length ?? 0;
      const supported = supportedTotal(tf);
      const unsupported = tf === '8h' ? okxScopeTotal : 0;
      const taskText = parts.length ? `${parts.join(' · ')} · ` : '';
      return { tf, period: label, status: `${taskText}库存 ${loaded}/${supported ?? '—'}${unsupported ? ` · OKX无原生 ${unsupported}` : ''}` };
    });
  }, [coverage, importPlan.data, poolStatus.data, scopeTotal, okxScopeTotal]);

  const selectedRow = klineRows.find((r) => r.id === selectedCycle) ?? null;

  return (
    <div className="flex-1 flex flex-col h-full overflow-hidden bg-neutral-950 p-4 gap-3 select-none text-neutral-200 ai-grid">
      {/* 1. Header Bar matching sketch */}
      <div className="flex flex-wrap items-center justify-between border-b border-neutral-800 pb-3 gap-3">
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            <Database className="w-5 h-5 text-indigo-400" />
            <h1 className="text-xl font-bold text-neutral-100 tracking-wide ai-gradient-text">数据中心</h1>
          </div>
          {/* Sub-tabs matching sketch: K线（当前页）｜OI｜资金费率｜聪明钱与市值 */}
          <div className="flex items-center gap-2 text-xs font-medium bg-neutral-900 p-1 rounded-lg border border-neutral-800">
            <button
              onClick={() => setActiveSubTab('kline')}
              className={`px-2.5 py-1 rounded transition-colors ${
                activeSubTab === 'kline'
                  ? 'bg-neutral-800 text-emerald-400 font-bold border border-neutral-700'
                  : 'text-neutral-400 hover:text-neutral-200'
              }`}
            >
              K线（当前页）
            </button>
            <span className="text-neutral-700">|</span>
            <button
              onClick={() => setActiveSubTab('oi')}
              className={`px-2.5 py-1 rounded transition-colors ${
                activeSubTab === 'oi'
                  ? 'bg-neutral-800 text-emerald-400 font-bold border border-neutral-700'
                  : 'text-neutral-400 hover:text-neutral-200'
              }`}
            >
              OI
            </button>
            <span className="text-neutral-700">|</span>
            <button
              onClick={() => setActiveSubTab('funding')}
              className={`px-2.5 py-1 rounded transition-colors ${
                activeSubTab === 'funding'
                  ? 'bg-neutral-800 text-emerald-400 font-bold border border-neutral-700'
                  : 'text-neutral-400 hover:text-neutral-200'
              }`}
            >
              资金费率
            </button>
            <span className="text-neutral-700">|</span>
            <button
              onClick={() => setActiveSubTab('smart_money')}
              className={`px-2.5 py-1 rounded transition-colors ${
                activeSubTab === 'smart_money'
                  ? 'bg-neutral-800 text-emerald-400 font-bold border border-neutral-700'
                  : 'text-neutral-400 hover:text-neutral-200'
              }`}
            >
              聪明钱与市值
            </button>
          </div>
        </div>

        {/* Right filter options matching sketch: 交易所：全部 ▾　搜索代币　状态筛选 ▾ */}
        <div className="flex items-center gap-2 text-xs">
          <div className="px-2.5 py-1 rounded bg-neutral-900 border border-neutral-800 flex items-center gap-1.5 text-neutral-300">
            <span>交易所：全部</span>
            <ChevronDown className="w-3.5 h-3.5 text-neutral-500" />
          </div>
          <div className="px-2.5 py-1 rounded bg-neutral-900 border border-neutral-800 flex items-center gap-1.5 text-neutral-400">
            <Search className="w-3.5 h-3.5" />
            <input
              type="text"
              placeholder="搜索代币..."
              className="bg-transparent border-0 outline-none text-xs w-24 text-neutral-200 placeholder:text-neutral-600"
              readOnly
            />
          </div>
          <div className="px-2.5 py-1 rounded bg-neutral-900 border border-neutral-800 flex items-center gap-1.5 text-neutral-300">
            <span>状态筛选 ▾</span>
          </div>
        </div>
      </div>

      {/* 2. Four Status Metric Cards matching sketch */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
        <div className="p-3 bg-neutral-900/90 border border-neutral-800 rounded-xl space-y-1">
          <span className="text-xs font-semibold text-neutral-300">实际数据覆盖</span>
          <div className="text-2xl font-bold font-mono text-neutral-400">{fmtInt(summary.combos)}</div>
          <span className="text-[11px] text-neutral-500">
            (标的,周期) 组合 · 共 {fmtInt(summary.totalRows)} 根 K 线 · 当前币池 {scopeTotal ?? '—'}
            {dataError ? <span className="text-rose-400"> · 只读接口读取失败：{dataError}（下方「—」是未取得，不代表真实为 0）</span> : null}
          </span>
        </div>
        <div className="p-3 bg-neutral-900/90 border border-neutral-800 rounded-xl space-y-1">
          <span className="text-xs font-semibold text-neutral-300">缺段与异常</span>
          <div className="text-2xl font-bold font-mono text-neutral-400">{summary.missingTotal ?? '—'}</div>
          <span className="text-[11px] text-neutral-500">已存首末范围内的缺根合计</span>
        </div>
        <div className="p-3 bg-neutral-900/90 border border-neutral-800 rounded-xl space-y-1">
          <span className="text-xs font-semibold text-neutral-300">最后同步时间</span>
          <div className="text-2xl font-bold font-mono text-neutral-400">
            {fmtImportedDay(importedAt?.latest)}
          </div>
          <span className="text-[11px] text-neutral-500">
            {importedAt?.latest
              ? `${fmtImportedClock(importedAt.latest)} UTC 入库`
              : '后端未提供入库/同步时间来源'}
          </span>
        </div>
        <div className="p-3 bg-neutral-900/90 border border-neutral-800 rounded-xl space-y-1">
          <span className="text-xs font-semibold text-neutral-300">独立验收</span>
          <div className="text-2xl font-bold font-mono text-neutral-400">
            {'未接入'}
          </div>
          <span className="text-[11px] text-neutral-500">
            下载日志 ≠ 入库覆盖 ≠ 独立验收
          </span>
        </div>
      </div>

      {/* 3. Main Center Split Layout matching sketch */}
      <div className="flex-1 flex gap-3 overflow-hidden min-h-0">
        {/* Left Column: K线覆盖主表 + 详情 (width ~72%) */}
        <div className="flex-1 flex flex-col gap-3 overflow-hidden min-w-0">
          {/* Table Container */}
          <div className="flex-1 flex flex-col bg-neutral-900/90 border border-neutral-800 rounded-xl overflow-hidden shadow-sm ai-aurora">
            <div className="px-4 py-2.5 border-b border-neutral-800 flex items-center justify-between bg-neutral-950/40">
              <h2 className="text-xs font-bold text-neutral-200">
                K线覆盖主表 · 当前工作池真实入库范围
              </h2>
              <span className="text-[10px] text-neutral-500 font-mono">已入库 / 当前池 {scopeTotal ?? '—'} 标的</span>
            </div>

            <div className="flex-1 overflow-auto">
              <table className="w-full text-left text-xs">
                <thead className="sticky top-0 bg-neutral-900 border-b border-neutral-800 text-neutral-400 font-semibold select-none">
                  <tr>
                    <th className="py-2.5 px-3">周期</th>
                    <th className="py-2.5 px-3">目标范围</th>
                    <th className="py-2.5 px-3">实际最早</th>
                    <th className="py-2.5 px-3">实际最新</th>
                    <th className="py-2.5 px-3">缺段</th>
                    <th className="py-2.5 px-3">最后更新</th>
                    <th className="py-2.5 px-3">来源</th>
                    <th className="py-2.5 px-3">状态</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-neutral-800/60 font-mono text-neutral-300">
                  {klineRows.map((row) => {
                    const isSelected = selectedCycle === row.id;
                    return (
                      <tr
                        key={row.id}
                        onClick={() => setSelectedCycle(row.id)}
                        className={`cursor-pointer transition-colors ${
                          isSelected ? 'bg-neutral-800/90 text-emerald-400 font-medium' : 'hover:bg-neutral-800/40'
                        }`}
                      >
                        <td className="py-2.5 px-3 font-bold font-sans">{row.name}</td>
                        <td className="py-2.5 px-3 text-neutral-400 font-sans">{row.target}</td>
                        <td className="py-2.5 px-3 text-neutral-500">{row.early}</td>
                        <td className="py-2.5 px-3 text-neutral-500">{row.latest}</td>
                        <td className="py-2.5 px-3 text-neutral-500">{row.missing}</td>
                        <td className="py-2.5 px-3 text-neutral-500">{row.updated}</td>
                        <td className="py-2.5 px-3 text-neutral-500">{row.source}</td>
                        <td className="py-2.5 px-3">
                          <span className={`text-[10px] px-1.5 py-0.5 rounded border font-sans ${row.status === '读取失败' ? 'bg-rose-500/10 text-rose-400 border-rose-500/20' : 'bg-amber-500/10 text-amber-400 border-amber-500/20'}`}>
                            {row.status}
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>

          {/* Bottom Record Inspection Panel matching sketch */}
          <div className="bg-neutral-900/90 border border-neutral-800 rounded-xl p-3.5 space-y-1.5 text-xs shrink-0">
            <h3 className="font-bold text-neutral-200 flex items-center gap-1.5">
              <span>选中记录 → 查看来源与缺段</span>
              <span className="text-[10px] text-neutral-500 font-mono font-normal">
                (当前选中: {selectedRow?.name})
              </span>
            </h3>
            <div className="text-neutral-400 space-y-1 text-[11px] leading-relaxed">
              <div>合约 / 原始周期 / 单位 / 来源地址 / 时间：<span className="font-mono text-neutral-500">—</span></div>
              <div>缺段起止与原因：<span className="font-mono text-neutral-500">—</span>；无法访问、未核查、未提供分别显示</div>
              <div className="text-neutral-500 font-sans">
                • 只有 1 分钟按 10 天滚动保留；其他周期与 OI 后续持续保存。
              </div>
            </div>
          </div>
        </div>

        {/* Right Column: 历史导入任务队列 & 任务进度 (width ~28%) */}
        <div className="w-80 shrink-0 flex flex-col gap-3 overflow-hidden">
          {/* Top: 历史导入任务队列 matching sketch */}
          <div className="flex-1 bg-neutral-900/90 border border-neutral-800 rounded-xl p-3.5 flex flex-col overflow-hidden">
            <h2 className="text-xs font-bold text-neutral-200 pb-2 border-b border-neutral-800">
              历史导入任务队列
            </h2>
            <div className="flex-1 overflow-y-auto space-y-2 py-2 text-xs">
              {queueItems.map((item) => (
                <div key={item.tf} className="flex items-center justify-between p-2 rounded bg-neutral-950/60 border border-neutral-850">
                  <div className="flex items-center gap-2">
                    <span className="w-2 h-2 rounded-full border border-neutral-500"></span>
                    <span className="font-medium text-neutral-300">{item.period}</span>
                  </div>
                  <span className="text-[11px] text-neutral-500 font-mono">{item.status}</span>
                </div>
              ))}
              <div className="p-2.5 rounded bg-neutral-950/80 border border-neutral-800/80 space-y-1 mt-2">
                <span className="font-bold text-neutral-300 text-xs block">
                  1分钟：{importPlan.data?.plan?.retention?.['1m'] ?? '—'}
                </span>
                <span className="text-[11px] text-neutral-500 block">
                  {importPlan.data?.plan?.native_skip?.rule ?? '—'}
                </span>
              </div>
            </div>
          </div>

          {/* Bottom: 任务进度与补齐记录 matching sketch */}
          <div className="bg-neutral-900/90 border border-neutral-800 rounded-xl p-3.5 space-y-2 text-xs shrink-0">
            <h3 className="font-bold text-neutral-200">任务进度与补齐记录</h3>
            <div className="space-y-1 text-[11px] text-neutral-400 font-mono">
              <div>
                当前批次 / 已完成量：<span className="text-neutral-500">
                  {summary.latestRun
                    ? `${summary.latestRun.phase ?? '—'} · 任务状态 ${summary.latestRun.status ?? '未知'}（日志记录，不代表全池验收）`
                    : '—'}
                </span>
              </div>
              <div>
                暂停或失败原因：<span className="text-neutral-500">{summary.latestRun?.note ?? '—'}</span>
              </div>
              <div>
                最近任务记录：<span className="text-neutral-500">
                  {summary.latestRun
                    ? `#${summary.latestRun.id} ${summary.latestRun.kind ?? ''} · ${
                        summary.latestRun.started_at ?? '—'
                      } → ${summary.latestRun.finished_at ?? '进行中'}`
                    : '暂无'}
                </span>
              </div>
            </div>
            <div className="pt-1 flex items-center justify-between gap-2">
              <button
                disabled
                className="px-3 py-1.5 rounded bg-neutral-800 text-neutral-500 text-xs font-medium cursor-not-allowed border border-neutral-700/50"
              >
                补齐 / 暂停 (待实现禁用)
              </button>
              <span className="text-[10px] text-neutral-500">旧任务分母不代表当前池；独立验收未接入</span>
            </div>
          </div>
        </div>
      </div>

      {/* 4. Footer Note matching sketch */}
      <div className="text-[11px] text-neutral-500 border-t border-neutral-800 pt-2 flex items-center justify-between">
        <span>数据来自本机只读库存与任务日志；任务日志、入库覆盖、独立验收分开显示。缺根仅统计已存首末范围。</span>
        <span className="font-mono text-neutral-600">数据中心 · 全页面草图 (1800×1180)</span>
      </div>
    </div>
  );
};
