/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import {
  Sliders,
  Bell,
  Layers,
  ShieldAlert,
  GitBranch,
  FileCheck,
  CheckCircle2,
  AlertCircle
} from 'lucide-react';

export const StrategyManagementView: React.FC = () => {
  const [activeTab, setActiveTab] = useState<'rules' | 'deps' | 'versions'>('rules');
  const [selectedStep, setSelectedStep] = useState<number>(1);

  const steps = [
    {
      step: '01',
      title: '区间形成与冻结',
      desc: '区间边界、观察窗口、冻结时点；使用周期待规则确认',
    },
    {
      step: '02',
      title: '放量突破',
      desc: '突破方向、成交量基线、比较条件与阈值来源',
    },
    {
      step: '03',
      title: '后续独立回踩',
      desc: '发生在突破之后；回踩区域、确认与过期条件',
    },
    {
      step: '04',
      title: '入场与模拟风控',
      desc: '条件满足、价格限制、风险预算与接受/拒绝原因',
    },
    {
      step: '05',
      title: '止损与退出',
      desc: '保护价、退出条件、费用与资金费处理',
    },
    {
      step: '06',
      title: '失效与缺数据处理',
      desc: '超时、结构失效、输入缺失；保留原因和当时快照',
    },
  ];

  return (
    <div className="flex-1 flex flex-col h-full overflow-hidden bg-neutral-950 p-4 gap-3 select-none text-neutral-200">
      {/* 1. Top Header Bar matching sketch */}
      <div className="flex flex-wrap items-center justify-between border-b border-neutral-800 pb-3 gap-3">
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            <Sliders className="w-5 h-5 text-emerald-400" />
            <h1 className="text-xl font-bold text-neutral-100 tracking-wide">策略管理</h1>
          </div>
          {/* Sub-tabs matching sketch: 规则说明（当前页）｜数据依赖｜版本对比 */}
          <div className="flex items-center gap-2 text-xs font-medium bg-neutral-900 p-1 rounded-lg border border-neutral-800">
            <button
              onClick={() => setActiveTab('rules')}
              className={`px-2.5 py-1 rounded transition-colors ${
                activeTab === 'rules'
                  ? 'bg-neutral-800 text-emerald-400 font-bold border border-neutral-700'
                  : 'text-neutral-400 hover:text-neutral-200'
              }`}
            >
              规则说明（当前页）
            </button>
            <span className="text-neutral-700">|</span>
            <button
              onClick={() => setActiveTab('deps')}
              className={`px-2.5 py-1 rounded transition-colors ${
                activeTab === 'deps'
                  ? 'bg-neutral-800 text-emerald-400 font-bold border border-neutral-700'
                  : 'text-neutral-400 hover:text-neutral-200'
              }`}
            >
              数据依赖
            </button>
            <span className="text-neutral-700">|</span>
            <button
              onClick={() => setActiveTab('versions')}
              className={`px-2.5 py-1 rounded transition-colors ${
                activeTab === 'versions'
                  ? 'bg-neutral-800 text-emerald-400 font-bold border border-neutral-700'
                  : 'text-neutral-400 hover:text-neutral-200'
              }`}
            >
              版本对比
            </button>
          </div>
        </div>

        <div className="text-xs text-neutral-400 font-mono">
          当前先只读查看；未确定参数明确标注
        </div>
      </div>

      {/* 2. Four Status Metric Cards matching sketch */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
        <div className="p-3 bg-neutral-900/90 border border-neutral-800 rounded-xl space-y-1">
          <span className="text-xs font-semibold text-neutral-300">策略状态</span>
          <div className="text-2xl font-bold font-sans text-amber-400">待确认</div>
          <span className="text-[11px] text-neutral-500">研究草案，尚未定稿</span>
        </div>
        <div className="p-3 bg-neutral-900/90 border border-neutral-800 rounded-xl space-y-1">
          <span className="text-xs font-semibold text-neutral-300">规则版本</span>
          <div className="text-2xl font-bold font-mono text-neutral-400">—</div>
          <span className="text-[11px] text-neutral-500">修改形成新版本</span>
        </div>
        <div className="p-3 bg-neutral-900/90 border border-neutral-800 rounded-xl space-y-1">
          <span className="text-xs font-semibold text-neutral-300">策略计算周期</span>
          <div className="text-2xl font-bold font-sans text-amber-400">待确认</div>
          <span className="text-[11px] text-neutral-500">展示默认1分钟不改计算周期</span>
        </div>
        <div className="p-3 bg-neutral-900/90 border border-neutral-800 rounded-xl space-y-1">
          <span className="text-xs font-semibold text-neutral-300">数据依赖状态</span>
          <div className="text-2xl font-bold font-mono text-neutral-400">—</div>
          <span className="text-[11px] text-neutral-500">字段来源与缺口单独核查</span>
        </div>
      </div>

      {/* 3. Three Columns Body Layout matching sketch */}
      <div className="flex-1 flex gap-3 overflow-hidden min-h-0">
        {/* Left Column: 策略列表 + 查看范围 + 手机提醒 (width ~20%) */}
        <div className="w-56 shrink-0 flex flex-col gap-3 overflow-y-auto">
          {/* 策略列表 */}
          <div className="bg-neutral-900/90 border border-neutral-800 rounded-xl p-3.5 space-y-2 text-xs">
            <h2 className="font-bold text-neutral-200 border-b border-neutral-800 pb-1.5">策略列表</h2>
            <div className="p-2.5 rounded bg-neutral-950 border border-emerald-500/30 space-y-1">
              <div className="font-bold text-emerald-400">区间突破与回踩</div>
              <div className="text-[11px] text-neutral-400">规则草案</div>
              <div className="text-[11px] font-mono text-neutral-500">版本：—</div>
            </div>
            <div className="text-[11px] text-neutral-500 pt-1 leading-relaxed">
              <div>• 选中策略查看完整逻辑</div>
              <div>• 其他策略版本：未列入</div>
            </div>
          </div>

          {/* 查看范围 */}
          <div className="bg-neutral-900/90 border border-neutral-800 rounded-xl p-3.5 space-y-2 text-xs flex-1">
            <h2 className="font-bold text-neutral-200 border-b border-neutral-800 pb-1.5">查看范围</h2>
            <div className="space-y-1.5 text-neutral-300 text-xs">
              <div className="flex items-center gap-1.5 text-emerald-400 font-medium">
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
                <span>触发条件</span>
              </div>
              <div className="pl-3 text-neutral-400">指标字段与阈值</div>
              <div className="pl-3 text-neutral-400">标的筛选</div>
              <div className="pl-3 text-neutral-400">止损、退出与失效</div>
              <div className="pl-3 text-neutral-400">模拟风控</div>
              <div className="pl-3 text-neutral-400">修改历史</div>
            </div>
            <div className="border-t border-neutral-800 pt-2 text-[11px] text-neutral-500 space-y-1">
              <div>• 已确认与待确认分开</div>
              <div>• 当前不提供实盘启用</div>
            </div>
          </div>

          {/* 手机提醒设置 matching sketch */}
          <div className="bg-neutral-900/90 border border-neutral-800 rounded-xl p-3.5 space-y-1.5 text-xs shrink-0">
            <div className="flex items-center gap-1.5 font-bold text-neutral-200">
              <Bell className="w-3.5 h-3.5 text-neutral-400" />
              <span>手机提醒设置</span>
            </div>
            <div className="text-[11px] text-neutral-400 space-y-0.5 font-mono">
              <div>渠道：Telegram</div>
              <div>提醒范围 / 去重 / 时段</div>
              <div className="text-amber-500/80 font-sans">未配置，不发送通知</div>
            </div>
          </div>
        </div>

        {/* Center Column: 策略条件序列 + 字段与计算口径 (width ~55%) */}
        <div className="flex-1 flex flex-col gap-3 overflow-hidden min-w-0">
          {/* Main Condition Sequence Card matching sketch */}
          <div className="flex-1 flex flex-col bg-neutral-900/90 border border-neutral-800 rounded-xl overflow-hidden shadow-sm">
            <div className="px-4 py-2.5 border-b border-neutral-800 flex items-center justify-between bg-neutral-950/40">
              <h2 className="text-xs font-bold text-neutral-200">
                策略条件序列 · 阈值不杜撰
              </h2>
              <span className="text-[10px] text-neutral-500 font-mono">6 阶段完整逻辑链</span>
            </div>

            <div className="flex-1 overflow-y-auto p-3 space-y-2">
              {steps.map((item, idx) => {
                const isSelected = selectedStep === idx + 1;
                return (
                  <div
                    key={item.step}
                    onClick={() => setSelectedStep(idx + 1)}
                    className={`p-3 rounded-lg border transition-all cursor-pointer ${
                      isSelected
                        ? 'bg-neutral-800 border-emerald-500/50 shadow-md'
                        : 'bg-neutral-950/60 border-neutral-850 hover:bg-neutral-850'
                    }`}
                  >
                    <div className="flex items-center gap-2 mb-1">
                      <span className="font-mono font-bold text-xs text-emerald-400">{item.step}</span>
                      <h3 className="font-bold text-xs text-neutral-100">{item.title}</h3>
                    </div>
                    <p className="text-xs text-neutral-400 pl-6 leading-relaxed">
                      {item.desc}
                    </p>
                  </div>
                );
              })}
            </div>
          </div>

          {/* Bottom Box: 选中条件 → 字段与计算口径 matching sketch */}
          <div className="bg-neutral-900/90 border border-neutral-800 rounded-xl p-3.5 space-y-1.5 text-xs shrink-0">
            <h3 className="font-bold text-neutral-200 flex items-center gap-2">
              <span>选中条件 → 字段与计算口径</span>
              <span className="text-[10px] text-neutral-500 font-mono font-normal">
                (当前阶段: {steps[selectedStep - 1]?.title})
              </span>
            </h3>
            <div className="text-[11px] text-neutral-400 space-y-1 leading-relaxed">
              <div className="font-mono text-neutral-300">
                字段名（中文说明）｜单位｜周期｜比较方向｜阈值｜来源｜确认状态
              </div>
              <div className="text-neutral-500 font-sans">
                • 缺数据处理：按规则拒绝或标未知，不自动视为满足。
              </div>
              <div className="text-neutral-500 font-sans">
                • 历史信号保留当时规则与条件快照，不随新参数改写。
              </div>
            </div>
          </div>
        </div>

        {/* Right Column: 规则版本与变更 + 标的与模拟风控 + 数据依赖与来源 (width ~25%) */}
        <div className="w-72 shrink-0 flex flex-col gap-3 overflow-y-auto">
          {/* 规则版本与变更 */}
          <div className="bg-neutral-900/90 border border-neutral-800 rounded-xl p-3.5 space-y-2 text-xs">
            <h3 className="font-bold text-neutral-200 border-b border-neutral-800 pb-1">规则版本与变更</h3>
            <div className="space-y-1 text-[11px] text-neutral-400">
              <div>当前 / 对照版本：<span className="font-mono text-neutral-500">—</span></div>
              <div>差异与修改原因：<span className="font-mono text-neutral-500">—</span></div>
              <div>参数来源：<span className="text-amber-400">待核查</span></div>
              <div className="text-neutral-500 pt-0.5">• 候选、已确认状态分开</div>
            </div>
          </div>

          {/* 标的与模拟风控 */}
          <div className="bg-neutral-900/90 border border-neutral-800 rounded-xl p-3.5 space-y-2 text-xs">
            <h3 className="font-bold text-neutral-200 border-b border-neutral-800 pb-1">标的与模拟风控</h3>
            <div className="space-y-1 text-[11px] text-neutral-400">
              <div>交易所、现货资格等条件</div>
              <div>单笔与总风险限制</div>
              <div className="text-neutral-300 font-mono">每策略周期 2000U（口径待定）</div>
              <div className="text-neutral-500 pt-0.5">• 待确认数值不填造</div>
              <div className="text-neutral-500">• 开仓占用 / 平仓释放 / 扣成本</div>
            </div>
          </div>

          {/* 数据依赖与来源 */}
          <div className="bg-neutral-900/90 border border-neutral-800 rounded-xl p-3.5 space-y-2 text-xs flex-1">
            <h3 className="font-bold text-neutral-200 border-b border-neutral-800 pb-1">数据依赖与来源</h3>
            <div className="space-y-1 text-[11px] text-neutral-400">
              <div>• K线 / 成交量</div>
              <div>• OI / 费率等增强字段</div>
              <div>• 聪明钱定义与成本来源</div>
              <div className="border-t border-neutral-800/80 pt-1 text-neutral-500 space-y-0.5">
                <div>每项显示实际覆盖和缺口</div>
                <div>不把显示字段自动设为阈值</div>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* 4. Footer Note matching sketch */}
      <div className="text-[11px] text-neutral-500 border-t border-neutral-800 pt-2 flex items-center justify-between">
        <span>仅沟通草图与需求；未启动采集、回测或交易，按钮与曲线均为设计槽位。</span>
        <span className="font-mono text-neutral-600">策略管理 · 全页面草图 (1800×1180)</span>
      </div>
    </div>
  );
};
