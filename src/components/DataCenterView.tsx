/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
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

export const DataCenterView: React.FC = () => {
  const [activeSubTab, setActiveSubTab] = useState<'kline' | 'oi' | 'funding' | 'smart_money'>('kline');
  const [selectedCycle, setSelectedCycle] = useState<string>('1m');

  const klineTemplates = [
    { id: '1m', name: '1分钟', target: '滚动10天', early: '—', latest: '—', missing: '—', updated: '—', source: '—', status: '待核查' },
    { id: '5m', name: '5分钟', target: '首次1年', early: '—', latest: '—', missing: '—', updated: '—', source: '—', status: '待核查' },
    { id: '15m', name: '15分钟', target: '首次2年', early: '—', latest: '—', missing: '—', updated: '—', source: '—', status: '待核查' },
    { id: '30m', name: '30分钟', target: '全部可得历史', early: '—', latest: '—', missing: '—', updated: '—', source: '—', status: '待核查' },
    { id: '1h', name: '1小时', target: '全部可得历史', early: '—', latest: '—', missing: '—', updated: '—', source: '—', status: '待核查' },
    { id: '4h', name: '4小时', target: '全部可得历史', early: '—', latest: '—', missing: '—', updated: '—', source: '—', status: '待核查' },
    { id: '8h', name: '8小时', target: '全部可得历史', early: '—', latest: '—', missing: '—', updated: '—', source: '—', status: '待核查' },
    { id: '12h', name: '12小时', target: '全部可得历史', early: '—', latest: '—', missing: '—', updated: '—', source: '—', status: '待核查' },
    { id: '1d', name: '日线', target: '全部可得历史', early: '—', latest: '—', missing: '—', updated: '—', source: '—', status: '待核查' },
  ];

  const queueItems = [
    { period: '日线', status: '尚未启动' },
    { period: '12小时', status: '尚未启动' },
    { period: '8小时', status: '尚未启动' },
    { period: '4小时', status: '尚未启动' },
    { period: '1小时', status: '尚未启动' },
    { period: '30分钟', status: '尚未启动' },
    { period: '15分钟', status: '尚未启动' },
    { period: '5分钟', status: '尚未启动' },
  ];

  return (
    <div className="flex-1 flex flex-col h-full overflow-hidden bg-neutral-950 p-4 gap-3 select-none text-neutral-200">
      {/* 1. Header Bar matching sketch */}
      <div className="flex flex-wrap items-center justify-between border-b border-neutral-800 pb-3 gap-3">
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            <Database className="w-5 h-5 text-indigo-400" />
            <h1 className="text-xl font-bold text-neutral-100 tracking-wide">数据中心</h1>
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
          <div className="text-2xl font-bold font-mono text-neutral-400">—</div>
          <span className="text-[11px] text-neutral-500">以真实覆盖记录为准</span>
        </div>
        <div className="p-3 bg-neutral-900/90 border border-neutral-800 rounded-xl space-y-1">
          <span className="text-xs font-semibold text-neutral-300">缺段与异常</span>
          <div className="text-2xl font-bold font-mono text-neutral-400">—</div>
          <span className="text-[11px] text-neutral-500">未接通，尚无缺口统计</span>
        </div>
        <div className="p-3 bg-neutral-900/90 border border-neutral-800 rounded-xl space-y-1">
          <span className="text-xs font-semibold text-neutral-300">最后同步时间</span>
          <div className="text-2xl font-bold font-mono text-neutral-400">—</div>
          <span className="text-[11px] text-neutral-500">来源与时间逐项保留</span>
        </div>
        <div className="p-3 bg-neutral-900/90 border border-neutral-800 rounded-xl space-y-1">
          <span className="text-xs font-semibold text-neutral-300">导入任务状态</span>
          <div className="text-2xl font-bold font-mono text-neutral-400">—</div>
          <span className="text-[11px] text-neutral-500">尚未启动下载任务</span>
        </div>
      </div>

      {/* 3. Main Center Split Layout matching sketch */}
      <div className="flex-1 flex gap-3 overflow-hidden min-h-0">
        {/* Left Column: K线覆盖主表 + 详情 (width ~72%) */}
        <div className="flex-1 flex flex-col gap-3 overflow-hidden min-w-0">
          {/* Table Container */}
          <div className="flex-1 flex flex-col bg-neutral-900/90 border border-neutral-800 rounded-xl overflow-hidden shadow-sm">
            <div className="px-4 py-2.5 border-b border-neutral-800 flex items-center justify-between bg-neutral-950/40">
              <h2 className="text-xs font-bold text-neutral-200">
                K线覆盖主表 · 周期模板，不代表已有数据
              </h2>
              <span className="text-[10px] text-neutral-500 font-mono">空态线框规约</span>
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
                  {klineTemplates.map((row) => {
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
                          <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-400 border border-amber-500/20 font-sans">
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
                (当前选中: {klineTemplates.find((t) => t.id === selectedCycle)?.name})
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
              {queueItems.map((item, i) => (
                <div key={i} className="flex items-center justify-between p-2 rounded bg-neutral-950/60 border border-neutral-850">
                  <div className="flex items-center gap-2">
                    <span className="w-2 h-2 rounded-full border border-neutral-500"></span>
                    <span className="font-medium text-neutral-300">{item.period}</span>
                  </div>
                  <span className="text-[11px] text-neutral-500 font-mono">{item.status}</span>
                </div>
              ))}
              <div className="p-2.5 rounded bg-neutral-950/80 border border-neutral-800/80 space-y-1 mt-2">
                <span className="font-bold text-neutral-300 text-xs block">1分钟：独立近10天补充</span>
                <span className="text-[11px] text-neutral-500 block">原生周期没有就跳过，并记录原因</span>
              </div>
            </div>
          </div>

          {/* Bottom: 任务进度与补齐记录 matching sketch */}
          <div className="bg-neutral-900/90 border border-neutral-800 rounded-xl p-3.5 space-y-2 text-xs shrink-0">
            <h3 className="font-bold text-neutral-200">任务进度与补齐记录</h3>
            <div className="space-y-1 text-[11px] text-neutral-400 font-mono">
              <div>当前批次 / 已完成量：<span className="text-neutral-500">—</span></div>
              <div>暂停或失败原因：<span className="text-neutral-500">—</span></div>
              <div>最近任务记录：<span className="text-neutral-500">暂无</span></div>
            </div>
            <div className="pt-1 flex items-center justify-between gap-2">
              <button
                disabled
                className="px-3 py-1.5 rounded bg-neutral-800 text-neutral-500 text-xs font-medium cursor-not-allowed border border-neutral-700/50"
              >
                补齐 / 暂停 (待实现禁用)
              </button>
              <span className="text-[10px] text-neutral-500">进度按任务记录，不填写假百分比</span>
            </div>
          </div>
        </div>
      </div>

      {/* 4. Footer Note matching sketch */}
      <div className="text-[11px] text-neutral-500 border-t border-neutral-800 pt-2 flex items-center justify-between">
        <span>仅沟通草图与需求；未启动采集、回测或交易，按钮与曲线均为设计槽位。</span>
        <span className="font-mono text-neutral-600">数据中心 · 全页面草图 (1800×1180)</span>
      </div>
    </div>
  );
};
