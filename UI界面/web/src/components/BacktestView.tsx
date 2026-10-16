/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * 回测复盘页容器（WORK-05）。
 *
 * 该页提供**真正独立的完整回测窗口**，两个视图共存、互不覆盖：
 *  · `完整回测工作台`（默认）= `BacktestWorkbench` —— 配置/提交/运行/失败 → run_id 固定的结果总览
 *    → K 线叠加信号/开平仓/止损轨迹 → 权益与回撤 → 交易/订单/资金费/拒单 → 数据覆盖/PIT/成本/
 *    参数/代码与输入哈希 → 运行历史 + 质量报告。全部数值来自同一 `run_id` 的真实 API/账本。
 *  · `验收复盘` = `BacktestReviewView`（WORK-04 交付的 Gate / 穿透链证据视图），原样保留。
 *
 * 纪律：本容器只做视图切换与导航，不计算任何交易/止损/收益，不引入第二回测引擎或第二图表框架。
 *
 * 设计规范（2026-10-16）：本页为**设计规范系统 v1.0** 的首个落地页。
 *   · 外层 `.ds-scope` 把子树内的 Tailwind 色阶重映射到新色板（不影响其它页面）。
 *   · 页头承载「我是谁 + 主操作」，单行、sticky 常驻。
 *
 * 2026-10-16 第三轮：用户要求「一眼全部能看」⇒ 工作台改为**单屏网格**（一屏不滚动）。
 *   因此**移除区块导航** —— 导航是长页（>3 屏）的补偿件，页面能一屏看完时它只剩噪音。
 *   页头随之从两行（89px）压到一行。
 */

import React, { useState } from 'react';
import { BacktestWorkbench } from './BacktestWorkbench';
import { BacktestReviewView } from './BacktestReviewView';

type BacktestPane = 'workbench' | 'review';

export const BacktestView: React.FC = () => {
  const [pane, setPane] = useState<BacktestPane>('workbench');

  return (
    <div className="ds-scope flex-1 min-h-0 flex flex-col">
      {/* ── 页头：单行，sticky 常驻 ── */}
      <header className="ds-pagehead shrink-0">
        <div className="ds-pagehead__row">
          <div className="ds-pagehead__title">
            <h1>回测复盘</h1>
            <span className="ds-pagehead__sub">单策略独立研究 · WX_OI_RESONANCE_V1</span>
          </div>
          <div className="ds-pagehead__spacer" />
          <div className="ds-seg" role="tablist" aria-label="回测视图">
            {(
              [
                ['workbench', '完整回测工作台'],
                ['review', '验收复盘'],
              ] as [BacktestPane, string][]
            ).map(([key, label]) => (
              <button
                key={key}
                type="button"
                role="tab"
                aria-selected={pane === key}
                onClick={() => setPane(key)}
                className="ds-seg__item"
                data-testid={`backtest-pane-${key}`}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      </header>

      {/* `data-backtest-scroll` 供「回到顶部」等操作定位滚动容器，不依赖 DOM 层级假设 */}
      <div className="flex-1 min-h-0 overflow-auto" data-backtest-scroll>
        {pane === 'workbench' ? <BacktestWorkbench /> : <BacktestReviewView />}
      </div>
    </div>
  );
};

export default BacktestView;
