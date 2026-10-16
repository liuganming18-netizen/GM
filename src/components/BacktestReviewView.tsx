/**
 * 回测验收工作台
 *
 * 设计原则：
 * 1. 页面展示层不制造回测数字；没有真实 Run 就显示 MISSING/BLOCKED。
 * 2. 回测结果必须能从 Signal → Risk → Order → Fill → Position → Trade → Ledger 追溯。
 * 3. 将 OI / 因子消融、PIT、数据质量、参数稳定性作为一等诊断项。
 * 4. 当前组件先完成验收工作台 UI；真实结果接入后只替换数据适配层，不重写页面。
 */
import React, { useMemo, useState } from 'react';
import {
  Activity,
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  ChevronDown,
  CircleDot,
  Clock3,
  Database,
  Download,
  Filter,
  GitBranch,
  Layers3,
  LockKeyhole,
  Play,
  RotateCcw,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  Target,
  TrendingDown,
  TrendingUp,
  XCircle,
} from 'lucide-react';

type GateStatus = 'pass' | 'fail' | 'partial' | 'blocked' | 'not_tested';
type ReviewTab = 'overview' | 'trades' | 'signals' | 'diagnostics';

const gates: { name: string; status: GateStatus; note: string }[] = [
  { name: '数据完整性', status: 'partial', note: '当前运行缺少完整覆盖证明' },
  { name: 'PIT 可得性', status: 'blocked', note: '未提供逐事件可得性证明' },
  { name: '信号完整性', status: 'fail', note: '需验证因子消融与信号形成链' },
  { name: '执行一致性', status: 'fail', note: 'Live / Paper / Backtest 尚未共核' },
  { name: '风控', status: 'not_tested', note: '尚未形成独立验收证据' },
  { name: '样本外稳定性', status: 'fail', note: '参数稳定性尚未通过' },
];

const trades = [
  { id: 1, symbol: '—', side: '—', entry: '—', exit: '—', reason: '暂无真实 Run', pnl: null },
];

const heatmap = [
  [-2.1, 0.8, 1.9, 1.2, -0.5],
  [0.4, 3.1, 5.2, 3.0, 0.6],
  [1.2, 5.8, 8.6, 5.1, -1.4],
  [0.9, 4.2, 6.3, 2.2, -3.2],
  [-1.8, 0.3, 1.1, -2.6, -4.9],
];

function StatusBadge({ status }: { status: GateStatus }) {
  const map = {
    pass: ['PASS', 'text-emerald-300 bg-emerald-500/10 border-emerald-500/20'],
    fail: ['FAIL', 'text-rose-300 bg-rose-500/10 border-rose-500/20'],
    partial: ['PARTIAL', 'text-amber-300 bg-amber-500/10 border-amber-500/20'],
    blocked: ['BLOCKED', 'text-amber-300 bg-amber-500/10 border-amber-500/20'],
    not_tested: ['NOT TESTED', 'text-neutral-400 bg-neutral-800/70 border-neutral-700'],
  } as const;
  const [label, cls] = map[status];
  return <span className={`px-1.5 py-0.5 rounded border text-[9px] font-bold tracking-wide ${cls}`}>{label}</span>;
}

function Card({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return <section className={`bg-neutral-900/85 border border-neutral-800 rounded-xl ${className}`}>{children}</section>;
}

function Metric({ label, value, hint, tone = 'neutral' }: { label: string; value: string; hint: string; tone?: 'neutral' | 'green' | 'red' }) {
  const color = tone === 'green' ? 'text-emerald-300' : tone === 'red' ? 'text-rose-300' : 'text-neutral-200';
  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-950/70 px-3 py-2.5">
      <div className="text-[10px] text-neutral-500">{label}</div>
      <div className={`mt-1 text-lg font-bold font-mono ${color}`}>{value}</div>
      <div className="mt-0.5 text-[9px] text-neutral-600 truncate">{hint}</div>
    </div>
  );
}

function ChainNode({ label, sub, active = false, failed = false }: { label: string; sub: string; active?: boolean; failed?: boolean }) {
  return (
    <div className={`min-w-[112px] rounded-lg border px-2.5 py-2 ${failed ? 'border-rose-500/30 bg-rose-500/5' : active ? 'border-indigo-500/30 bg-indigo-500/5' : 'border-neutral-800 bg-neutral-950/60'}`}>
      <div className="flex items-center gap-1.5">
        <CircleDot className={`h-3 w-3 ${failed ? 'text-rose-400' : active ? 'text-indigo-300' : 'text-neutral-600'}`} />
        <span className="text-[11px] font-semibold text-neutral-200">{label}</span>
      </div>
      <div className="mt-1 text-[9px] text-neutral-600">{sub}</div>
    </div>
  );
}

export const BacktestReviewView: React.FC = () => {
  const [tab, setTab] = useState<ReviewTab>('overview');
  const [selectedTrade, setSelectedTrade] = useState<number | null>(null);
  const [showConfig, setShowConfig] = useState(true);

  const overall: GateStatus = useMemo(() => {
    if (gates.some(g => g.status === 'fail')) return 'fail';
    if (gates.some(g => g.status === 'blocked')) return 'blocked';
    if (gates.some(g => g.status === 'partial')) return 'partial';
    return 'pass';
  }, []);

  return (
    <div className="flex-1 min-h-0 overflow-auto bg-neutral-950 text-neutral-200 p-4">
      <div className="mx-auto max-w-[1680px] space-y-3">
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-neutral-800 pb-3">
          <div className="flex items-center gap-3">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-indigo-500/10 border border-indigo-500/20">
              <BarChart3 className="h-4 w-4 text-indigo-300" />
            </div>
            <div>
              <h1 className="text-lg font-bold tracking-wide text-neutral-100">回测验收工作台</h1>
              <div className="text-[10px] text-neutral-500 font-mono">BACKTEST REVIEW · EVIDENCE FIRST</div>
            </div>
            <StatusBadge status={overall} />
          </div>
          <div className="flex items-center gap-2 text-[10px] font-mono text-neutral-500">
            <span className="flex items-center gap-1"><GitBranch className="h-3 w-3" /> wyckoff-v3.1-main</span>
            <span>·</span><span>RUN_ID —</span>
            <button className="flex items-center gap-1 rounded-md border border-neutral-800 px-2 py-1 hover:bg-neutral-900 text-neutral-400">
              <Download className="h-3 w-3" /> 导出证据
            </button>
          </div>
        </header>

        <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-8 gap-2">
          <Metric label="净收益" value="—" hint="没有真实 Run" />
          <Metric label="最大回撤" value="—" hint="含未平仓权益" />
          <Metric label="Profit Factor" value="—" hint="净盈利 / 净亏损" />
          <Metric label="Sharpe" value="—" hint="按运行口径计算" />
          <Metric label="交易数" value="—" hint="完整 Trade" />
          <Metric label="胜率" value="—" hint="真实成交后计算" />
          <Metric label="平均盈亏" value="—" hint="含费用与资金费" />
          <Metric label="总成本" value="—" hint="手续费 + Funding + 滑点" />
        </div>

        <Card className="overflow-hidden">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-neutral-800 px-3.5 py-2.5">
            <div className="flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-indigo-300" />
              <span className="text-xs font-bold">策略验收 Gate</span>
              <span className="text-[10px] text-neutral-600">任何 FAIL / BLOCKED 均不得进入模拟盘</span>
            </div>
            <span className="text-[10px] text-neutral-500 font-mono">{gates.filter(g => g.status === 'pass').length}/{gates.length} PASS</span>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 divide-x divide-neutral-800">
            {gates.map(g => (
              <div key={g.name} className="p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[10px] text-neutral-400">{g.name}</span>
                  <StatusBadge status={g.status} />
                </div>
                <div className="mt-2 text-[9px] leading-4 text-neutral-600">{g.note}</div>
              </div>
            ))}
          </div>
        </Card>

        <div className="flex gap-1 rounded-lg border border-neutral-800 bg-neutral-900/70 p-1 w-fit">
          {([
            ['overview', '总览'],
            ['trades', '交易复盘'],
            ['signals', '信号链'],
            ['diagnostics', '稳定性诊断'],
          ] as [ReviewTab, string][]).map(([key, label]) => (
            <button
              key={key}
              onClick={() => setTab(key)}
              className={`px-3 py-1.5 rounded-md text-[11px] font-medium transition-colors ${tab === key ? 'bg-neutral-800 text-emerald-300 border border-neutral-700' : 'text-neutral-500 hover:text-neutral-300'}`}
            >
              {label}
            </button>
          ))}
        </div>

        <div className="grid grid-cols-1 xl:grid-cols-[260px_minmax(0,1fr)_310px] gap-3">
          <div className="space-y-3">
            <Card className="p-3.5">
              <button className="flex w-full items-center justify-between" onClick={() => setShowConfig(v => !v)}>
                <span className="flex items-center gap-2 text-xs font-bold"><SlidersHorizontal className="h-3.5 w-3.5 text-indigo-300" /> 回测配置</span>
                <ChevronDown className={`h-3.5 w-3.5 text-neutral-600 transition-transform ${showConfig ? 'rotate-180' : ''}`} />
              </button>
              {showConfig && (
                <div className="mt-3 space-y-2 text-[10px]">
                  {[
                    ['策略', 'WX_OI_RESONANCE_V1'],
                    ['交易所 / 合约', '—'],
                    ['周期', '—'],
                    ['时间窗口', '—'],
                    ['成交时点', '—'],
                    ['手续费 / 滑点', '—'],
                    ['资金费', '—'],
                    ['仓位 / 风险', '—'],
                  ].map(([k, v]) => (
                    <div key={k} className="flex justify-between gap-2 border-b border-neutral-800/60 pb-1.5">
                      <span className="text-neutral-500">{k}</span><span className="font-mono text-neutral-400">{v}</span>
                    </div>
                  ))}
                </div>
              )}
              <button disabled className="mt-3 flex w-full items-center justify-center gap-2 rounded-lg bg-neutral-800 py-2 text-[11px] font-semibold text-neutral-600 cursor-not-allowed">
                <Play className="h-3.5 w-3.5" /> 运行回测 · 等待真实参数/数据
              </button>
            </Card>

            <Card className="p-3.5">
              <div className="flex items-center gap-2 text-xs font-bold"><Database className="h-3.5 w-3.5 text-indigo-300" /> 数据证明</div>
              <div className="mt-3 space-y-2 text-[10px]">
                {[
                  ['覆盖范围', 'MISSING'],
                  ['缺口 / 未决样本', 'MISSING'],
                  ['PIT', 'BLOCKED'],
                  ['数据版本', '—'],
                  ['可得时间', '—'],
                ].map(([k, v]) => (
                  <div key={k} className="flex justify-between gap-2">
                    <span className="text-neutral-500">{k}</span>
                    <span className={v === 'MISSING' || v === 'BLOCKED' ? 'text-amber-300 font-mono' : 'text-neutral-400 font-mono'}>{v}</span>
                  </div>
                ))}
              </div>
            </Card>
          </div>

          <div className="space-y-3 min-w-0">
            <Card className="p-3.5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <div className="flex items-center gap-2 text-xs font-bold"><Activity className="h-3.5 w-3.5 text-indigo-300" /> K线 + 策略信号</div>
                  <div className="text-[9px] text-neutral-600 mt-0.5">必须能从图上定位每个 ENTRY / EXIT，并打开对应 Signal</div>
                </div>
                <div className="flex items-center gap-2 text-[9px] text-neutral-600 font-mono">
                  <span>ENTRY</span><span className="text-emerald-400">▲</span>
                  <span>EXIT</span><span className="text-rose-400">▼</span>
                  <span>STOP</span><span className="text-amber-400">◆</span>
                </div>
              </div>
              <div className="mt-3 h-[300px] rounded-lg border border-dashed border-neutral-800 bg-neutral-950/70 relative overflow-hidden">
                <div className="absolute inset-0 opacity-40" style={{ backgroundImage: 'linear-gradient(#20262f 1px, transparent 1px), linear-gradient(90deg, #20262f 1px, transparent 1px)', backgroundSize: '54px 42px' }} />
                <div className="absolute inset-x-5 top-1/2 border-t border-neutral-800" />
                <div className="absolute left-1/2 top-0 bottom-0 border-l border-neutral-800" />
                <div className="absolute inset-0 flex items-center justify-center text-center">
                  <div>
                    <BarChart3 className="mx-auto h-7 w-7 text-neutral-700" />
                    <div className="mt-2 text-xs text-neutral-500">尚无可绘制的真实回测 Run</div>
                    <div className="mt-1 text-[9px] text-neutral-700">禁止使用示意成交点冒充真实信号</div>
                  </div>
                </div>
              </div>
            </Card>

            <Card className="p-3.5">
              <div className="flex items-center justify-between">
                <div className="text-xs font-bold">Signal → Execution → Ledger</div>
                <span className="text-[9px] text-neutral-600">点击交易后必须可逐级追溯</span>
              </div>
              <div className="mt-3 flex items-center gap-1 overflow-x-auto pb-1">
                <ChainNode label="Signal" sub="四时点 + 七问" active />
                <ChevronDown className="hidden sm:block rotate-[-90deg] text-neutral-700" />
                <ChainNode label="Risk" sub="entry / portfolio" />
                <ChevronDown className="hidden sm:block rotate-[-90deg] text-neutral-700" />
                <ChainNode label="Order" sub="幂等 ID" />
                <ChevronDown className="hidden sm:block rotate-[-90deg] text-neutral-700" />
                <ChainNode label="Fill" sub="simulated / exchange" failed />
                <ChevronDown className="hidden sm:block rotate-[-90deg] text-neutral-700" />
                <ChainNode label="Position" sub="持仓状态" />
                <ChevronDown className="hidden sm:block rotate-[-90deg] text-neutral-700" />
                <ChainNode label="Trade" sub="净盈亏" />
                <ChevronDown className="hidden sm:block rotate-[-90deg] text-neutral-700" />
                <ChainNode label="Ledger" sub="事件账本" />
              </div>
            </Card>

            {tab !== 'overview' && (
              <Card className="p-3.5">
                {tab === 'trades' && (
                  <>
                    <div className="flex items-center justify-between mb-2">
                      <div className="text-xs font-bold">交易明细</div>
                      <div className="flex items-center gap-2 text-[9px] text-neutral-600"><Search className="h-3 w-3" /> 可按币种 / 方向 / 原因筛选</div>
                    </div>
                    <div className="overflow-auto">
                      <table className="w-full text-[10px]">
                        <thead className="text-neutral-600 border-b border-neutral-800">
                          <tr>{['#', '标的', '方向', '入场', '离场', '原因', '净盈亏', 'MAE/MFE'].map(h => <th key={h} className="text-left py-2 pr-3 font-medium">{h}</th>)}</tr>
                        </thead>
                        <tbody>
                          {trades.map(t => (
                            <tr key={t.id} onClick={() => setSelectedTrade(t.id)} className={`border-b border-neutral-800/60 cursor-pointer ${selectedTrade === t.id ? 'bg-neutral-800/50' : ''}`}>
                              <td className="py-2 pr-3 text-neutral-500">{t.id}</td><td className="pr-3">{t.symbol}</td><td className="pr-3">{t.side}</td><td className="pr-3 font-mono">{t.entry}</td><td className="pr-3 font-mono">{t.exit}</td><td className="pr-3 text-neutral-500">{t.reason}</td><td className="pr-3">—</td><td>— / —</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </>
                )}

                {tab === 'signals' && (
                  <div>
                    <div className="text-xs font-bold">信号形成与拒绝日志</div>
                    <div className="mt-3 grid grid-cols-2 md:grid-cols-4 gap-2">
                      {['触发', '接受', '拒绝', '失效'].map(x => (
                        <div key={x} className="rounded-lg bg-neutral-950/70 border border-neutral-800 p-3">
                          <div className="text-[10px] text-neutral-500">{x}</div><div className="mt-1 font-mono text-lg">—</div>
                        </div>
                      ))}
                    </div>
                    <div className="mt-3 rounded-lg border border-amber-500/20 bg-amber-500/5 p-3 text-[10px] text-amber-200/80">
                      OI 消融：当前验收必须同时显示“完整策略”和“移除 OI”后的信号数量、交易数量及绩效变化；禁止只展示一个总分。
                    </div>
                  </div>
                )}

                {tab === 'diagnostics' && (
                  <div>
                    <div className="text-xs font-bold">稳定性与消融</div>
                    <div className="mt-3 grid grid-cols-1 md:grid-cols-3 gap-3">
                      <div className="rounded-lg border border-neutral-800 bg-neutral-950/70 p-3">
                        <div className="text-[10px] text-neutral-500">参数敏感性</div>
                        <div className="mt-2 grid grid-cols-5 gap-1">
                          {heatmap.flat().map((v, i) => <div key={i} className={`rounded h-8 flex items-center justify-center text-[9px] font-mono ${v >= 0 ? 'bg-emerald-500/15 text-emerald-300' : 'bg-rose-500/15 text-rose-300'}`}>{v.toFixed(1)}</div>)}
                        </div>
                        <div className="mt-2 text-[9px] text-neutral-600">仅有真实运行数据后才可判稳定区。</div>
                      </div>
                      <div className="rounded-lg border border-neutral-800 bg-neutral-950/70 p-3">
                        <div className="text-[10px] text-neutral-500">成本敏感性</div>
                        <div className="mt-2 space-y-2">
                          {['0.00× 成本', '1.00× 基准成本', '1.50× 成本', '2.00× 成本'].map(x => <div key={x} className="flex justify-between text-[10px]"><span>{x}</span><span className="font-mono text-neutral-600">—</span></div>)}
                        </div>
                      </div>
                      <div className="rounded-lg border border-neutral-800 bg-neutral-950/70 p-3">
                        <div className="text-[10px] text-neutral-500">因子消融</div>
                        <div className="mt-2 space-y-2">
                          {['完整策略', '− OI', '− Volume', '− Wyckoff', '− Momentum'].map(x => <div key={x} className="flex justify-between text-[10px]"><span>{x}</span><span className="font-mono text-neutral-600">— signals</span></div>)}
                        </div>
                      </div>
                    </div>
                  </div>
                )}
              </Card>
            )}
          </div>

          <div className="space-y-3">
            <Card className="p-3.5">
              <div className="flex items-center gap-2 text-xs font-bold"><Target className="h-3.5 w-3.5 text-indigo-300" /> 成本与结果</div>
              <div className="mt-3 space-y-2 text-[10px]">
                {[
                  ['手续费', '—'],
                  ['资金费', '—'],
                  ['滑点影响', '—'],
                  ['已实现净盈亏', '—'],
                  ['未实现盈亏', '—'],
                ].map(([k, v]) => <div key={k} className="flex justify-between border-b border-neutral-800/60 pb-1.5"><span className="text-neutral-500">{k}</span><span className="font-mono text-neutral-400">{v}</span></div>)}
              </div>
            </Card>

            <Card className="p-3.5">
              <div className="flex items-center gap-2 text-xs font-bold"><LockKeyhole className="h-3.5 w-3.5 text-indigo-300" /> 可复现身份</div>
              <div className="mt-3 space-y-2 text-[10px] font-mono">
                {[
                  ['strategy', 'WX_OI_RESONANCE_V1'],
                  ['code_sha256', '—'],
                  ['param_hash', '—'],
                  ['data_version', '—'],
                  ['rule_version', '—'],
                  ['engine_version', '—'],
                  ['run_id', '—'],
                ].map(([k, v]) => <div key={k} className="flex justify-between gap-2"><span className="text-neutral-600">{k}</span><span className="text-neutral-400 truncate max-w-[170px]">{v}</span></div>)}
              </div>
            </Card>

            <Card className="p-3.5">
              <div className="flex items-center gap-2 text-xs font-bold"><AlertTriangle className="h-3.5 w-3.5 text-amber-300" /> 当前限制</div>
              <div className="mt-3 space-y-2 text-[10px] text-neutral-500 leading-4">
                <div>• 无真实 Run：不绘制收益曲线、不展示虚构交易。</div>
                <div>• PIT 未证明：不得把历史数据当作可得数据。</div>
                <div>• OI 消融必须独立运行，不能由 AI 主观解释“应该有贡献”。</div>
                <div>• Backtest / Paper / Live 的执行链差异必须显式显示。</div>
              </div>
            </Card>

            <Card className="p-3.5">
              <div className="text-xs font-bold">复盘工具</div>
              <div className="mt-3 grid grid-cols-2 gap-2">
                {[
                  [RotateCcw, '重跑同一 Run'],
                  [Filter, '筛选交易'],
                  [Clock3, '事件时间线'],
                  [Download, 'CSV / JSON'],
                ].map(([Icon, label]) => {
                  const I = Icon as React.ElementType;
                  return <button key={label as string} disabled className="flex items-center gap-1.5 rounded-lg border border-neutral-800 bg-neutral-950/60 p-2 text-[9px] text-neutral-600 cursor-not-allowed"><I className="h-3 w-3" />{label as string}</button>;
                })}
              </div>
            </Card>
          </div>
        </div>

        <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-neutral-800 pt-2 text-[9px] text-neutral-600">
          <span>验收页面不制造数据：真实结果缺失即 MISSING / BLOCKED。</span>
          <span className="font-mono">BACKTEST_REVIEW_WORKBENCH · v1</span>
        </footer>
      </div>
    </div>
  );
};
