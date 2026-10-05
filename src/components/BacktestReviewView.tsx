/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import {
  History,
  TrendingUp,
  BarChart3,
  Calendar,
  Layers,
  Sliders,
  Filter,
  AlertCircle
} from 'lucide-react';

export const BacktestReviewView: React.FC = () => {
  const [activeTab, setActiveTab] = useState<'history' | 'paper_review' | 'live_review' | 'signal_logs'>('history');

  const topMetrics = [
    { title: '净收益', value: '—', sub: '账户成本后收益' },
    { title: '最大回撤', value: '—', sub: '含未平仓权益' },
    { title: '夏普比率', value: '—', sub: '按日净收益计算' },
    { title: '收益因子', value: '—', sub: '净盈利 / 净亏损' },
    { title: '成交笔数', value: '—', sub: '完整交易数' },
    { title: '胜率', value: '—', sub: '尚无运行结果' },
    { title: '盈亏比', value: '—', sub: '平均盈利 / 平均亏损' },
    { title: '总费用', value: '—', sub: '手续费与资金费' },
  ];

  return (
    <div className="flex-1 flex flex-col h-full overflow-hidden bg-neutral-950 p-4 gap-3 select-none text-neutral-200">
      {/* 1. Header Bar matching sketch */}
      <div className="flex flex-wrap items-center justify-between border-b border-neutral-800 pb-3 gap-3">
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            <History className="w-5 h-5 text-indigo-400" />
            <h1 className="text-xl font-bold text-neutral-100 tracking-wide">回测复盘</h1>
          </div>
          {/* Sub-tabs matching sketch */}
          <div className="flex items-center gap-2 text-xs font-medium bg-neutral-900 p-1 rounded-lg border border-neutral-800">
            <button
              onClick={() => setActiveTab('history')}
              className={`px-2.5 py-1 rounded transition-colors ${
                activeTab === 'history'
                  ? 'bg-neutral-800 text-emerald-400 font-bold border border-neutral-700'
                  : 'text-neutral-400 hover:text-neutral-200'
              }`}
            >
              历史回测（当前页）
            </button>
            <span className="text-neutral-700">|</span>
            <button
              onClick={() => setActiveTab('paper_review')}
              className={`px-2.5 py-1 rounded transition-colors ${
                activeTab === 'paper_review'
                  ? 'bg-neutral-800 text-emerald-400 font-bold border border-neutral-700'
                  : 'text-neutral-400 hover:text-neutral-200'
              }`}
            >
              模拟实盘复盘
            </button>
            <span className="text-neutral-700">|</span>
            <button
              onClick={() => setActiveTab('live_review')}
              className={`px-2.5 py-1 rounded transition-colors ${
                activeTab === 'live_review'
                  ? 'bg-neutral-800 text-emerald-400 font-bold border border-neutral-700'
                  : 'text-neutral-400 hover:text-neutral-200'
              }`}
            >
              实盘仓位复盘
            </button>
            <span className="text-neutral-700">|</span>
            <button
              onClick={() => setActiveTab('signal_logs')}
              className={`px-2.5 py-1 rounded transition-colors ${
                activeTab === 'signal_logs'
                  ? 'bg-neutral-800 text-emerald-400 font-bold border border-neutral-700'
                  : 'text-neutral-400 hover:text-neutral-200'
              }`}
            >
              信号日志
            </button>
          </div>
        </div>

        <div className="text-xs text-neutral-400 font-mono">
          回测 / 模拟 / 实盘只读记录分别查看
        </div>
      </div>

      {/* 2. Top 8 Metrics Row matching sketch */}
      <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 gap-2">
        {topMetrics.map((m, idx) => (
          <div key={idx} className="p-2.5 bg-neutral-900/90 border border-neutral-800 rounded-xl space-y-1">
            <span className="text-xs font-semibold text-neutral-300 block truncate">{m.title}</span>
            <div className="text-xl font-bold font-mono text-neutral-400">{m.value}</div>
            <span className="text-[10px] text-neutral-500 block truncate">{m.sub}</span>
          </div>
        ))}
      </div>

      {/* 3. Three Columns Body Layout matching sketch */}
      <div className="flex-1 flex gap-3 overflow-hidden min-h-0">
        {/* Left Column: 历史回测配置 + 留存与复盘筛选 (width ~18%) */}
        <div className="w-64 shrink-0 flex flex-col gap-3 overflow-y-auto">
          {/* 历史回测配置 */}
          <div className="bg-neutral-900/90 border border-neutral-800 rounded-xl p-3.5 space-y-2 text-xs">
            <h2 className="font-bold text-neutral-200 border-b border-neutral-800 pb-1.5">历史回测配置</h2>
            <div className="space-y-1.5 text-[11px] text-neutral-400 font-mono">
              <div>交易所：<span className="text-neutral-500">—</span></div>
              <div>币池 / 合约：<span className="text-neutral-500">—</span></div>
              <div>开始日期 / 结束日期：<span className="text-neutral-500">—</span></div>
              <div>策略版本：<span className="text-neutral-500">—</span></div>
              <div>执行精度：<span className="text-neutral-500">—</span></div>
              <div className="text-neutral-300 font-sans">每策略周期：2000U（口径待定）</div>
              <div>实际覆盖与缺口：<span className="text-neutral-500">—</span></div>
              <div className="text-neutral-500 font-sans">• 1分钟精度仅近10天覆盖</div>
            </div>

            <div className="pt-1">
              <button
                disabled
                className="w-full py-1.5 rounded bg-neutral-800 text-neutral-500 text-xs font-medium cursor-not-allowed border border-neutral-700/50"
              >
                运行回测 (待实现禁用)
              </button>
            </div>

            <div className="border-t border-neutral-800 pt-2 text-[11px] text-neutral-500 space-y-1 font-mono">
              <div>占用 / 预留 / 可用：—</div>
              <div className="font-sans">• 开仓占用，平仓释放</div>
              <div className="font-sans">• 盈亏扣成本，全部留存</div>
            </div>
          </div>

          {/* 留存与复盘筛选 */}
          <div className="bg-neutral-900/90 border border-neutral-800 rounded-xl p-3.5 space-y-1.5 text-xs flex-1">
            <h2 className="font-bold text-neutral-200 border-b border-neutral-800 pb-1.5">留存与复盘筛选</h2>
            <div className="space-y-1 text-[11px] text-neutral-400">
              <div>模拟 / 实盘 / 信号：<span className="font-mono text-neutral-500">—</span></div>
              <div>币种 / 时间 / 策略版本</div>
              <div>持仓变化 / 成交 / 信号日志</div>
              <div>有效 / 失效 / 拒绝原因</div>
              <div className="border-t border-neutral-800 pt-1.5 text-neutral-500 space-y-1">
                <div>• 各模式不合计收益</div>
                <div>• 持仓归零不删除历史记录</div>
              </div>
            </div>
          </div>
        </div>

        {/* Center Column: 净值与回撤曲线 + 留存记录清单 + 单条记录回放 (width ~58%) */}
        <div className="flex-1 flex flex-col gap-3 overflow-hidden min-w-0">
          {/* Top Curves Row (净值曲线 + 回撤曲线) */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 h-44 shrink-0">
            {/* 净值曲线 */}
            <div className="bg-neutral-900/90 border border-neutral-800 rounded-xl p-3 flex flex-col">
              <h3 className="text-xs font-bold text-neutral-200 mb-1">净值曲线</h3>
              <div className="flex-1 relative flex items-center justify-center border border-dashed border-neutral-800 rounded-lg bg-neutral-950/60 p-2">
                {/* SVG coordinate axis */}
                <svg className="absolute inset-0 w-full h-full p-3 pointer-events-none" preserveAspectRatio="none">
                  <line x1="20" y1="10" x2="20" y2="90%" stroke="#333" strokeWidth="1.5" />
                  <line x1="20" y1="90%" x2="95%" y2="90%" stroke="#333" strokeWidth="1.5" />
                </svg>
                <div className="text-center space-y-1 z-10">
                  <span className="text-xs text-neutral-400 font-sans block">尚无回测结果，不绘制曲线</span>
                  <span className="text-[10px] text-neutral-600 block">所选回测日期窗口</span>
                </div>
              </div>
            </div>

            {/* 回撤曲线 */}
            <div className="bg-neutral-900/90 border border-neutral-800 rounded-xl p-3 flex flex-col">
              <h3 className="text-xs font-bold text-neutral-200 mb-1">回撤曲线</h3>
              <div className="flex-1 relative flex items-center justify-center border border-dashed border-neutral-800 rounded-lg bg-neutral-950/60 p-2">
                <svg className="absolute inset-0 w-full h-full p-3 pointer-events-none" preserveAspectRatio="none">
                  <line x1="20" y1="10" x2="20" y2="90%" stroke="#333" strokeWidth="1.5" />
                  <line x1="20" y1="90%" x2="95%" y2="90%" stroke="#333" strokeWidth="1.5" />
                </svg>
                <div className="text-center space-y-1 z-10">
                  <span className="text-xs text-neutral-400 font-sans block">尚无回测结果，不绘制曲线</span>
                  <span className="text-[10px] text-neutral-600 block">所选回测日期窗口</span>
                </div>
              </div>
            </div>
          </div>

          {/* Middle Table: 留存记录清单 · 按当前页签显示 */}
          <div className="flex-1 flex flex-col bg-neutral-900/90 border border-neutral-800 rounded-xl overflow-hidden shadow-sm">
            <div className="px-4 py-2 border-b border-neutral-800 flex items-center justify-between bg-neutral-950/40">
              <h3 className="text-xs font-bold text-neutral-200">
                留存记录清单 · 按当前页签显示
              </h3>
              <span className="text-[10px] text-neutral-500 font-mono">0 条记录</span>
            </div>

            <div className="flex-1 overflow-auto flex flex-col">
              <table className="w-full text-left text-xs">
                <thead className="bg-neutral-900 border-b border-neutral-800 text-neutral-400 font-semibold sticky top-0 select-none">
                  <tr>
                    <th className="py-2.5 px-3">时间</th>
                    <th className="py-2.5 px-3">代币</th>
                    <th className="py-2.5 px-3">方向</th>
                    <th className="py-2.5 px-3">事件 / 价位</th>
                    <th className="py-2.5 px-3">净盈亏</th>
                    <th className="py-2.5 px-3">状态 / 原因</th>
                  </tr>
                </thead>
              </table>
              <div className="flex-1 flex items-center justify-center p-6 text-center text-xs text-neutral-500">
                尚无记录；持仓和信号的完整过程在此留存查看
              </div>
            </div>
          </div>

          {/* Bottom Container: 单条记录回放 matching sketch */}
          <div className="bg-neutral-900/90 border border-neutral-800 rounded-xl p-3.5 space-y-1 text-xs shrink-0">
            <h3 className="font-bold text-neutral-200">单条记录回放 · 信号 ↔ 持仓变化 ↔ 成交与退出</h3>
            <div className="text-[11px] text-neutral-400 space-y-0.5 leading-relaxed">
              <div>• 当时 K 线、信号条件、持仓/成交时间线；原始证据长期留存。</div>
              <div>• 1 分钟原始 K 线仅近 10 天；更早案例按真实可得周期查看。</div>
              <div>• 中文复盘与事后标签单独记录，不改写当时证据。</div>
            </div>
          </div>
        </div>

        {/* Right Column: 成本与结果明细 + 数据与回放限制 + 复盘与样本外检查 (width ~24%) */}
        <div className="w-72 shrink-0 flex flex-col gap-3 overflow-y-auto">
          {/* 成本与结果明细 */}
          <div className="bg-neutral-900/90 border border-neutral-800 rounded-xl p-3.5 space-y-2 text-xs">
            <h3 className="font-bold text-neutral-200 border-b border-neutral-800 pb-1">成本与结果明细</h3>
            <div className="space-y-1 text-[11px] text-neutral-400">
              <div>手续费：<span className="font-mono text-neutral-500">—</span></div>
              <div>资金费：<span className="font-mono text-neutral-500">—</span></div>
              <div>滑点影响：<span className="font-mono text-neutral-500">—（成交价计入不重扣）</span></div>
              <div>已实现净盈亏：<span className="font-mono text-neutral-500">—</span></div>
              <div className="text-neutral-500 pt-0.5">• 资金费缺失则标结果限制</div>
            </div>
          </div>

          {/* 数据与回放限制 */}
          <div className="bg-neutral-900/90 border border-neutral-800 rounded-xl p-3.5 space-y-2 text-xs">
            <h3 className="font-bold text-neutral-200 border-b border-neutral-800 pb-1">数据与回放限制</h3>
            <div className="space-y-1 text-[11px] text-neutral-400">
              <div>实际覆盖：<span className="font-mono text-neutral-500">—</span></div>
              <div>缺段 / 未决样本：<span className="font-mono text-neutral-500">—</span></div>
              <div>同根止损止盈顺序：<span className="font-mono text-neutral-500">—</span></div>
              <div>结果精度与数据版本：<span className="font-mono text-neutral-500">—</span></div>
              <div className="text-neutral-500 pt-0.5">• 未来数据检查：未执行</div>
            </div>
          </div>

          {/* 复盘与样本外检查 */}
          <div className="bg-neutral-900/90 border border-neutral-800 rounded-xl p-3.5 space-y-2 text-xs flex-1">
            <h3 className="font-bold text-neutral-200 border-b border-neutral-800 pb-1">复盘与样本外检查</h3>
            <div className="space-y-1 text-[11px] text-neutral-400">
              <div>• 选中记录的原因与证据</div>
              <div>• 触发 / 失效 / 拒绝 / 未成交均保留</div>
              <div>• 人工操作、模拟与实际成交分别标明</div>
              <div className="border-t border-neutral-800/80 pt-1 text-neutral-500 space-y-0.5">
                <div>中文备注 / 标签：待记录</div>
                <div>样本外 / 参数稳定性：未检查</div>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* 4. Footer Note matching sketch */}
      <div className="text-[11px] text-neutral-500 border-t border-neutral-800 pt-2 flex items-center justify-between">
        <span>仅沟通草图与需求；未启动采集、回测或交易，按钮与曲线均为设计槽位。</span>
        <span className="font-mono text-neutral-600">回测复盘 · 全页面草图 (1800×1180)</span>
      </div>
    </div>
  );
};
