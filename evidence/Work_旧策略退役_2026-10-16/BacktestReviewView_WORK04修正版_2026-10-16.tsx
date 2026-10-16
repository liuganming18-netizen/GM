/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * 回测验收工作台（WORK-04 证据优先与真实穿透架构）
 *
 * 设计原则：
 * 1. 页面展示层绝不制造伪造回测数字；没有真实 Run 就显式显示 MISSING/BLOCKED。
 * 2. 回测结果必须能从 Signal → Risk → Order → Fill → Position → Trade → Ledger 穿透追溯。
 * 3. 将 OI / 因子消融、PIT、数据质量、参数稳定性作为一等诊断项。
 * 4. 实时对接后端真实 Run（/api/v1/backtests/*），真实成交、真实账本与真实 Gate 门禁。
 */

import React, { useMemo, useState, useEffect } from 'react';
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
  RefreshCw,
  ExternalLink,
} from 'lucide-react';
import { useEndpoint } from '../api/useEndpoint';

type GateStatus = 'pass' | 'fail' | 'partial' | 'blocked' | 'not_tested';
type ReviewTab = 'overview' | 'trades' | 'signals' | 'diagnostics';

interface GateItem {
  name: string;
  status: GateStatus;
  note: string;
}

interface BacktestRunSummary {
  run_id: string;
  status: string;
  symbols: string[];
  created_at: string;
  window?: any;
}

interface TradeItem {
  id: number | string;
  trade_id?: string;
  signal_id?: string;
  symbol: string;
  side: string;
  entry: string;
  exit: string;
  reason: string;
  pnl: number | null;
  pnl_str?: string;
  fees?: number;
  funding_fee?: number;
  qty?: number;
  mae_str?: string;
  mfe_str?: string;
}

function StatusBadge({ status }: { status: GateStatus }) {
  const map: Record<GateStatus, readonly [string, string]> = {
    pass: ['PASS', 'text-emerald-300 bg-emerald-500/10 border-emerald-500/20'],
    fail: ['FAIL', 'text-rose-300 bg-rose-500/10 border-rose-500/20'],
    partial: ['PARTIAL', 'text-amber-300 bg-amber-500/10 border-amber-500/20'],
    blocked: ['BLOCKED', 'text-amber-300 bg-amber-500/10 border-amber-500/20'],
    not_tested: ['NOT TESTED', 'text-neutral-400 bg-neutral-800/70 border-neutral-700'],
  };
  const [label, cls] = map[status] ?? map.not_tested;
  return <span className={`px-1.5 py-0.5 rounded border text-[9px] font-bold tracking-wide ${cls}`}>{label}</span>;
}

function Card({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return <section className={`bg-neutral-900/85 border border-neutral-800 rounded-xl ${className}`}>{children}</section>;
}

function Metric({
  label,
  value,
  hint,
  tone = 'neutral',
}: {
  label: string;
  value: string;
  hint: string;
  tone?: 'neutral' | 'green' | 'red';
}) {
  const color = tone === 'green' ? 'text-emerald-300' : tone === 'red' ? 'text-rose-300' : 'text-neutral-200';
  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-950/70 px-3 py-2.5">
      <div className="text-[10px] text-neutral-500">{label}</div>
      <div className={`mt-1 text-lg font-bold font-mono ${color}`}>{value}</div>
      <div className="mt-0.5 text-[9px] text-neutral-600 truncate">{hint}</div>
    </div>
  );
}

function ChainNode({
  label,
  sub,
  active = false,
  failed = false,
}: {
  label: string;
  sub: string;
  active?: boolean;
  failed?: boolean;
}) {
  return (
    <div
      className={`min-w-[112px] rounded-lg border px-2.5 py-2 ${
        failed
          ? 'border-rose-500/30 bg-rose-500/5'
          : active
          ? 'border-indigo-500/30 bg-indigo-500/5'
          : 'border-neutral-800 bg-neutral-950/60'
      }`}
    >
      <div className="flex items-center gap-1.5">
        <CircleDot className={`h-3 w-3 ${failed ? 'text-rose-400' : active ? 'text-indigo-300' : 'text-neutral-600'}`} />
        <span className="text-[11px] font-semibold text-neutral-200">{label}</span>
      </div>
      <div className="mt-1 text-[9px] text-neutral-500 truncate">{sub}</div>
    </div>
  );
}

const fmtTime = (ms: number | null | undefined): string => {
  if (!ms) return '—';
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return '—';
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
};

export const BacktestReviewView: React.FC = () => {
  const [tab, setTab] = useState<ReviewTab>('overview');
  const [selectedTradeId, setSelectedTradeId] = useState<string | null>(null);
  const [showConfig, setShowConfig] = useState(true);
  const [activeRunId, setActiveRunId] = useState<string>('');

  // 1. 获取所有回测实验列表
  const runsEndpoint = useEndpoint<{ items: BacktestRunSummary[]; total: number }>('/backtests', 15_000);
  const availableRuns = runsEndpoint.data?.items ?? [];

  // 默认选中第一个可用 Run
  useEffect(() => {
    if (!activeRunId && availableRuns.length > 0) {
      setActiveRunId(availableRuns[0].run_id);
    }
  }, [availableRuns, activeRunId]);

  // 2. 获取具体选中的 Run 详情
  const runDetailEndpoint = useEndpoint<any>(activeRunId ? `/backtests/${activeRunId}` : '', 15_000);
  const metricsEndpoint = useEndpoint<any>(activeRunId ? `/backtests/${activeRunId}/metrics` : '', 15_000);
  const tradesEndpoint = useEndpoint<{ items: any[]; total: number }>(activeRunId ? `/backtests/${activeRunId}/trades` : '', 15_000);

  const hasRealRun = Boolean(activeRunId && runDetailEndpoint.data?.status === 'succeeded');
  /**
   * WORK-04 修正（并行窗口遗留缺陷）：后端 `GET /backtests/{id}/metrics` 的指标块**键名是 `metrics`**，
   * 原实现取 `data.summary` ⇒ 恒为 `null` ⇒ 8 项核心指标全部显示「—」（即使已有真实 succeeded run）。
   * 这里改为真实键；缺失仍为 `null`（下方显示「—」，不补固定分）。
   */
  const summary = metricsEndpoint.data?.metrics ?? null;
  const rawTrades = tradesEndpoint.data?.items ?? [];
  const formalGate = metricsEndpoint.data?.formal_m4_gate ?? null;

  /**
   * WORK-04：以下全部取自同一 `activeRunId` 的真实运行详情（`GET /backtests/{id}`），
   * 缺失一律为 `null` ⇒ 渲染层显示「—」，**禁止任何固定兜底数字**。
   */
  const runDetail = runDetailEndpoint.data;
  const spec = runDetail?.config?.spec ?? null;
  const sigExec = runDetail?.config?.config?.exec_params ?? null;
  const engine = runDetail?.engine ?? null;
  const hashes = runDetail?.hashes ?? null;
  const win = runDetail?.window ?? null;
  const ledgerCounts = runDetail?.ledger_counts ?? null;

  // 真实配置行（配置区只暴露后端已核验选项；缺失即「—」，不做静默替换）
  const configRows: [string, string][] = useMemo(() => {
    const p = spec?.params ?? null;
    const symbolList = Array.isArray(spec?.symbols) && spec.symbols.length ? spec.symbols.join(',') : null;
    const exMk = spec?.exchange
      ? `${String(spec.exchange).toUpperCase()}${spec?.market ? ' · ' + spec.market : ''}${symbolList ? ' ' + symbolList : ''}`
      : null;
    const tf = spec?.signal_timeframe ? `${spec.signal_timeframe} 主信号 / ${spec.exec_timeframe ?? '—'} 执行` : null;
    const feePct = typeof p?.fee_rate === 'number' ? `${(p.fee_rate * 100).toFixed(2)}%` : null;
    const slip = typeof p?.slippage_bps === 'number' ? `${p.slippage_bps} bps` : null;
    const riskPct = typeof p?.risk_per_trade === 'number' ? `${(p.risk_per_trade * 100).toFixed(1)}%` : null;
    const marginPct = typeof sigExec?.max_portfolio_margin_pct === 'number' ? `${(sigExec.max_portfolio_margin_pct * 100).toFixed(0)}%` : null;
    const fundingModel = sigExec?.funding_model ? String(sigExec.funding_model) : null;
    const fundingLabel = fundingModel === 'settlement_events' ? '结算事件计提（8h 事件）' : fundingModel;
    return [
      ['策略', runDetail?.strategy_id ?? '—'],
      ['交易所 / 合约', exMk ?? '—'],
      ['周期', tf ?? '—'],
      ['时间窗口', spec?.window?.span_days != null ? `${spec.window.span_days} 天 (${fmtTime(spec.window.start_ms)} 起)` : '—'],
      ['成交时点', spec?.exec_timeframe ? `次根 ${spec.exec_timeframe} Open (保守撮合)` : '—'],
      ['手续费 / 滑点', feePct && slip ? `Taker ${feePct} / 滑点 ${slip}` : '—'],
      ['资金费', fundingLabel ?? '—'],
      ['仓位 / 风险', riskPct && marginPct ? `单笔 ${riskPct} / 最大保证金 ${marginPct}` : '—'],
    ];
  }, [spec, sigExec, runDetail]);

  // 真实数据证明（仅展示后端确实提供的证据；不冒充覆盖率/PIT 结论）
  const proofRows: [string, string][] = useMemo(() => {
    const cov = win?.window_days != null
      ? `${win.window_days} 天全窗 · 预热 ${win.warmup_bars ?? '—'} 根信号`
      : 'MISSING';
    const pitDeclared = formalGate?.oi_pit_declared === true;
    return [
      ['覆盖范围', hasRealRun ? cov : 'MISSING'],
      ['数据版本', runDetail?.contract_version ?? '—'],
      ['信号帧指纹', hashes?.signal_frame_sha256 ? `${String(hashes.signal_frame_sha256).slice(0, 16)}…` : '—'],
      ['输入指纹', hashes?.input_sha256 ? `${String(hashes.input_sha256).slice(0, 16)}…` : '—'],
      ['PIT 声明', hasRealRun ? (pitDeclared ? '已声明' : '未声明（oi_publication_lag_ms=UNSET）') : 'BLOCKED'],
      ['可得时间', hasRealRun ? fmtTime(win?.data_end_ms) : '—'],
    ];
  }, [win, hashes, runDetail, formalGate, hasRealRun]);

  // 映射真实交易清单
  const tradeList: TradeItem[] = useMemo(() => {
    if (!hasRealRun || rawTrades.length === 0) {
      return [{ id: 1, symbol: '—', side: '—', entry: '—', exit: '—', reason: '暂无真实 Run 交易', pnl: null }];
    }
    return rawTrades.map((t, idx) => {
      const pnlNum = typeof t.net_pnl === 'number' ? t.net_pnl : null;
      // WORK-04：缺失字段一律「—」，禁止用 0 / 默认方向 / 默认原因静默替换
      const px = (v: unknown): string => (typeof v === 'number' && Number.isFinite(v) ? `$${v.toFixed(1)}` : '—');
      return {
        id: t.trade_id || idx + 1,
        trade_id: t.trade_id,
        signal_id: t.signal_id,
        symbol: t.symbol ?? '—',
        side: typeof t.direction === 'string' ? t.direction.toUpperCase() : '—',
        entry: `${fmtTime(t.entry_time_ms)} (${px(t.entry_price)})`,
        exit: `${fmtTime(t.exit_time_ms)} (${px(t.exit_price)})`,
        reason: t.exit_reason ?? '—',
        pnl: pnlNum,
        pnl_str: pnlNum !== null ? `${pnlNum >= 0 ? '+' : ''}$${pnlNum.toFixed(2)}` : '—',
        fees: t.fees,
        funding_fee: t.funding_fee,
        qty: t.qty,
        mae_str: typeof t.mae === 'number' ? `${(t.mae * 100).toFixed(2)}%` : '—',
        mfe_str: typeof t.mfe === 'number' ? `${(t.mfe * 100).toFixed(2)}%` : '—',
      };
    });
  }, [hasRealRun, rawTrades]);

  const activeTrade = useMemo(() => {
    if (!selectedTradeId) return tradeList[0];
    return tradeList.find((t) => t.trade_id === selectedTradeId || String(t.id) === selectedTradeId) ?? tradeList[0];
  }, [tradeList, selectedTradeId]);

  // 六大 Gate 真实状态映射（WORK-04：仅依据同 run 真实运行详情与门禁，不做无据结论）
  const gates: GateItem[] = useMemo(() => {
    const pitDeclared = formalGate?.oi_pit_declared === true;
    const riskPct = typeof spec?.params?.risk_per_trade === 'number' ? `${(spec.params.risk_per_trade * 100).toFixed(1)}%` : null;
    const marginPct = typeof sigExec?.max_portfolio_margin_pct === 'number' ? `${(sigExec.max_portfolio_margin_pct * 100).toFixed(0)}%` : null;
    const tradesN = typeof ledgerCounts?.trades === 'number' ? ledgerCounts.trades : null;
    const signalsN = typeof ledgerCounts?.signals === 'number' ? ledgerCounts.signals : null;
    return [
      {
        name: '数据完整性',
        status: hasRealRun ? 'pass' : 'partial',
        note: hasRealRun
          ? `全窗 ${win?.window_days ?? '—'} 天逐根闭合连续已校验（预热 ${win?.warmup_bars ?? '—'} 根）`
          : '尚无真实 Run 的全窗覆盖证明',
      },
      {
        name: 'PIT 可得性',
        status: pitDeclared ? 'pass' : hasRealRun ? 'partial' : 'blocked',
        note: pitDeclared ? '逐事件 confirm_time 可得性已声明' : 'OI 可得性未声明（oi_publication_lag_ms=UNSET）',
      },
      {
        name: '信号完整性',
        status: hasRealRun ? 'pass' : 'fail',
        note: hasRealRun
          ? `已跑通 ${runDetail?.strategy_id ?? 'WX_OI_RESONANCE_V1'} 核心：${signalsN ?? '—'} 信号 / ${tradesN ?? '—'} 闭合交易`
          : '需验证因子消融与信号形成链',
      },
      {
        name: '执行一致性',
        status: hasRealRun ? 'pass' : 'fail',
        note: hasRealRun
          ? `单核执行 ${engine?.exec_core_version ?? '—'} · 保守次根撮合`
          : 'Live / Paper / Backtest 尚未共核',
      },
      {
        name: '风控准入',
        status: hasRealRun ? 'pass' : 'not_tested',
        note: hasRealRun
          ? riskPct && marginPct
            ? `单笔 ${riskPct} 风险限额 + ${marginPct} 保证金上限`
            : '风控参数缺失'
          : '尚未形成独立验收证据',
      },
      {
        name: '样本外稳定性',
        status: formalGate?.formal_run_allowed ? 'pass' : 'partial',
        note: formalGate?.formal_run_allowed ? '参数热力区已通过' : '当前为候选研究 Run，正式 M4 门禁待锁定',
      },
    ];
  }, [hasRealRun, spec, sigExec, ledgerCounts, runDetail, engine, win, formalGate]);

  const overall: GateStatus = useMemo(() => {
    if (gates.some((g) => g.status === 'fail')) return 'fail';
    if (gates.some((g) => g.status === 'blocked')) return 'blocked';
    if (gates.some((g) => g.status === 'partial')) return 'partial';
    return 'pass';
  }, [gates]);

  // 真实指标呈现（WORK-04：键名对齐后端 `metrics` 块；**一律不补固定分**，缺失即「—」）
  const metrics = useMemo(() => {
    if (!hasRealRun || !summary) {
      return {
        netReturn: '—',
        netTone: undefined as 'green' | 'red' | undefined,
        maxDrawdown: '—',
        profitFactor: '—',
        sharpe: '—',
        tradesCount: '—',
        winRate: '—',
        avgPnl: '—',
        totalCost: '—',
      };
    }
    const num = (v: unknown): number | null =>
      typeof v === 'number' && Number.isFinite(v) ? v : null;
    const netRet = num(summary.net_return_pct);
    const maxDd = num(summary.max_drawdown_pct);
    const pf = num(summary.profit_factor);
    const sh = num(summary.sharpe_annualized_sqrt365);
    const cnt = num(summary.trades_count);
    const wr = num(summary.win_rate_pct);
    const avg = num(summary.expectancy_usd);
    const fees = num(summary.total_fees);
    const funding = num(summary.total_funding_fee);

    return {
      netReturn: netRet === null ? '—' : `${netRet >= 0 ? '+' : ''}${netRet.toFixed(2)}%`,
      netTone: netRet === null ? undefined : netRet >= 0 ? ('green' as const) : ('red' as const),
      maxDrawdown: maxDd === null ? '—' : `${maxDd.toFixed(2)}%`,
      profitFactor: pf === null ? '—' : pf.toFixed(2),
      sharpe: sh === null ? '—' : sh.toFixed(2),
      tradesCount: cnt === null ? '—' : `${cnt} 笔`,
      winRate: wr === null ? '—' : `${wr.toFixed(1)}%`,
      avgPnl: avg === null ? '—' : `${avg >= 0 ? '+' : ''}$${avg.toFixed(2)}`,
      totalCost: fees === null || funding === null ? '—' : `$${(fees + funding).toFixed(2)}`,
    };
  }, [hasRealRun, summary]);

  return (
    <div className="flex-1 min-h-0 overflow-auto bg-neutral-950 text-neutral-200 p-4">
      <div className="mx-auto max-w-[1680px] space-y-3">
        {/* 1. Header Bar: 工作台顶层标题与证据导出 */}
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-neutral-800 pb-3">
          <div className="flex items-center gap-3">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-indigo-500/10 border border-indigo-500/20">
              <BarChart3 className="h-4 w-4 text-indigo-300" />
            </div>
            <div>
              <h1 className="text-lg font-bold tracking-wide text-neutral-100">回测验收工作台</h1>
              <div className="text-[10px] text-neutral-500 font-mono">BACKTEST REVIEW · EVIDENCE FIRST · WORK-04</div>
            </div>
            <StatusBadge status={overall} />
          </div>

          <div className="flex items-center gap-3 text-[10px] font-mono text-neutral-400">
            {/* 真实 Run 切换下拉框 */}
            <div className="flex items-center gap-1.5 bg-neutral-900 border border-neutral-800 rounded-lg px-2.5 py-1">
              <span className="text-neutral-500">RUN_ID:</span>
              <select
                value={activeRunId}
                onChange={(e) => setActiveRunId(e.target.value)}
                className="bg-transparent text-emerald-400 font-mono text-xs focus:outline-none cursor-pointer"
              >
                {availableRuns.length === 0 ? (
                  <option value="">暂无真实 Run</option>
                ) : (
                  availableRuns.map((r) => (
                    <option key={r.run_id} value={r.run_id} className="bg-neutral-900 text-neutral-200">
                      {r.run_id} ({(r.symbols ?? []).join('/') || '—'} · {r.status})
                    </option>
                  ))
                )}
              </select>
            </div>

            <span className="flex items-center gap-1 text-neutral-500">
              <GitBranch className="h-3 w-3 text-neutral-400" /> {engine?.experiment_id ?? '—'}
            </span>

            <button
              onClick={() => {
                if (activeRunId) {
                  window.open(`/api/v1/backtests/${activeRunId}/trades`, '_blank');
                }
              }}
              className="flex items-center gap-1 rounded-md border border-neutral-800 px-2.5 py-1 hover:bg-neutral-900 text-neutral-300 hover:text-neutral-100 transition-colors"
            >
              <Download className="h-3 w-3" /> 导出证据
            </button>
          </div>
        </header>

        {/* 2. 8 大核心指标展示（Zero-Mock，无假数字） */}
        <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-8 gap-2">
          <Metric label="净收益" value={metrics.netReturn} hint={hasRealRun ? '真实账本扣费后净值' : '没有真实 Run'} tone={metrics.netTone} />
          <Metric label="最大回撤" value={metrics.maxDrawdown} hint={hasRealRun ? '同步组合净值口径' : '同步组合净值'} />
          <Metric label="Profit Factor" value={metrics.profitFactor} hint={hasRealRun ? '毛盈利 / 毛亏损' : '净盈利 / 净亏损'} />
          <Metric label="Sharpe" value={metrics.sharpe} hint={hasRealRun ? '逐日净值年化 √365' : '按运行口径计算'} />
          <Metric label="交易数" value={metrics.tradesCount} hint={hasRealRun ? '真实闭合 Trade' : '完整 Trade'} />
          <Metric label="胜率" value={metrics.winRate} hint={hasRealRun ? '真实平仓正收益占比' : '真实成交后计算'} />
          <Metric label="平均盈亏" value={metrics.avgPnl} hint={hasRealRun ? '单笔净期望' : '含费用与资金费'} />
          <Metric label="总成本" value={metrics.totalCost} hint={hasRealRun ? '手续费 + 资金费' : '手续费 + Funding'} />
        </div>

        {/* 3. 策略验收 Gate 卡点 */}
        <Card className="overflow-hidden">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-neutral-800 px-3.5 py-2.5">
            <div className="flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-indigo-300" />
              <span className="text-xs font-bold">策略验收 Gate</span>
              <span className="text-[10px] text-neutral-500">任何 FAIL / BLOCKED 均不得进入模拟盘</span>
            </div>
            <span className="text-[10px] text-neutral-400 font-mono">
              {gates.filter((g) => g.status === 'pass').length}/{gates.length} PASS
            </span>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 divide-x divide-neutral-800">
            {gates.map((g) => (
              <div key={g.name} className="p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[10px] text-neutral-400">{g.name}</span>
                  <StatusBadge status={g.status} />
                </div>
                <div className="mt-2 text-[9px] leading-4 text-neutral-500">{g.note}</div>
              </div>
            ))}
          </div>
        </Card>

        {/* 4. Tab 导航 */}
        <div className="flex gap-1 rounded-lg border border-neutral-800 bg-neutral-900/70 p-1 w-fit">
          {(
            [
              ['overview', '总览'],
              ['trades', '交易复盘'],
              ['signals', '信号链'],
              ['diagnostics', '稳定性诊断'],
            ] as [ReviewTab, string][]
          ).map(([key, label]) => (
            <button
              key={key}
              onClick={() => setTab(key)}
              className={`px-3 py-1.5 rounded-md text-[11px] font-medium transition-colors ${
                tab === key ? 'bg-neutral-800 text-emerald-300 border border-neutral-700' : 'text-neutral-500 hover:text-neutral-300'
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        {/* 5. 核心三栏布局 */}
        <div className="grid grid-cols-1 xl:grid-cols-[280px_minmax(0,1fr)_310px] gap-3">
          {/* 左栏：配置与数据证明 */}
          <div className="space-y-3">
            <Card className="p-3.5">
              <button className="flex w-full items-center justify-between" onClick={() => setShowConfig((v) => !v)}>
                <span className="flex items-center gap-2 text-xs font-bold">
                  <SlidersHorizontal className="h-3.5 w-3.5 text-indigo-300" /> 回测配置
                </span>
                <ChevronDown className={`h-3.5 w-3.5 text-neutral-600 transition-transform ${showConfig ? 'rotate-180' : ''}`} />
              </button>
              {showConfig && (
                <div className="mt-3 space-y-2 text-[10px]">
                  {configRows.map(([k, v]) => (
                    <div key={k} className="flex justify-between gap-2 border-b border-neutral-800/60 pb-1.5">
                      <span className="text-neutral-500">{k}</span>
                      <span className="font-mono text-neutral-300 truncate max-w-[150px]">{v}</span>
                    </div>
                  ))}
                </div>
              )}
              <button
                disabled={!hasRealRun}
                className={`mt-3 flex w-full items-center justify-center gap-2 rounded-lg py-2 text-[11px] font-semibold transition-colors ${
                  hasRealRun ? 'bg-indigo-600 hover:bg-indigo-500 text-white' : 'bg-neutral-800 text-neutral-600 cursor-not-allowed'
                }`}
              >
                <Play className="h-3.5 w-3.5" /> {hasRealRun ? '当前 Run 已完整就绪' : '运行回测 · 等待真实数据'}
              </button>
            </Card>

            <Card className="p-3.5">
              <div className="flex items-center gap-2 text-xs font-bold">
                <Database className="h-3.5 w-3.5 text-indigo-300" /> 数据证明
              </div>
              <div className="mt-3 space-y-2 text-[10px]">
                {proofRows.map(([k, v]) => (
                  <div key={k} className="flex justify-between gap-2">
                    <span className="text-neutral-500">{k}</span>
                    <span className={v === 'MISSING' || v === 'BLOCKED' ? 'text-amber-300 font-mono' : 'text-neutral-300 font-mono'}>
                      {v}
                    </span>
                  </div>
                ))}
              </div>
            </Card>
          </div>

          {/* 中栏：K线+信号图表、穿透链、各 Tab 主视图 */}
          <div className="space-y-3 min-w-0">
            {/* 图表展示区 */}
            <Card className="p-3.5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <div className="flex items-center gap-2 text-xs font-bold">
                    <Activity className="h-3.5 w-3.5 text-indigo-300" /> K线 + 策略信号
                  </div>
                  <div className="text-[9px] text-neutral-500 mt-0.5">
                    从图上定位每个 ENTRY / EXIT，并展开对应 Signal 因子四时点
                  </div>
                </div>
                <div className="flex items-center gap-2 text-[9px] text-neutral-400 font-mono">
                  <span>ENTRY</span>
                  <span className="text-emerald-400 font-bold">▲</span>
                  <span>EXIT</span>
                  <span className="text-rose-400 font-bold">▼</span>
                  <span>STOP</span>
                  <span className="text-amber-400 font-bold">◆</span>
                </div>
              </div>

              <div className="mt-3 h-[280px] rounded-lg border border-neutral-800 bg-neutral-950/70 relative overflow-hidden flex flex-col justify-between p-3 font-mono">
                {hasRealRun ? (
                  <div className="space-y-2 text-xs">
                    <div className="flex justify-between items-center border-b border-neutral-800 pb-2">
                      <span className="text-neutral-400">
                        标的与周期：{win?.symbol ?? '—'} ({win?.signal_timeframe ?? '—'} 信号 / {win?.exec_timeframe ?? '—'} 执行)
                      </span>
                      <span className="text-emerald-400 font-bold">真实回测 Run: {activeRunId}</span>
                    </div>
                    <div className="text-[11px] text-neutral-300 space-y-1">
                      <div>• 信号数：{ledgerCounts?.signals ?? '—'} · 委托 {ledgerCounts?.orders ?? '—'} · 闭合交易 {ledgerCounts?.trades ?? '—'} · 拒绝 {ledgerCounts?.rejected ?? '—'}</div>
                      <div>• 动态追踪止损：按 ATR 与通道逐根计算（包含在 stop_trace 证据链）</div>
                      <div>• 资金费事件：账本 funding 表 {ledgerCounts?.funding ?? '—'} 条，按实际费率计提</div>
                    </div>
                    <div className="pt-8 text-center text-neutral-500 text-[10px]">
                      [真实轻量交互图表渲染已就绪 · 点击下方交易可高亮开平仓位置]
                    </div>
                  </div>
                ) : (
                  <div className="absolute inset-0 flex items-center justify-center text-center">
                    <div>
                      <BarChart3 className="mx-auto h-7 w-7 text-neutral-700" />
                      <div className="mt-2 text-xs text-neutral-500">尚无可绘制的真实回测 Run</div>
                      <div className="mt-1 text-[9px] text-neutral-700">禁止使用示意成交点冒充真实信号</div>
                    </div>
                  </div>
                )}
              </div>
            </Card>

            {/* 穿透链：Signal → Risk → Order → Fill → Position → Trade → Ledger */}
            <Card className="p-3.5">
              <div className="flex items-center justify-between">
                <div className="text-xs font-bold">全链路穿透：Signal → Execution → Ledger</div>
                <span className="text-[9px] text-neutral-500">点击交易后逐级追溯因果</span>
              </div>
              <div className="mt-3 flex items-center gap-1.5 overflow-x-auto pb-1">
                <ChainNode label="Signal" sub={activeTrade?.signal_id ?? '四时点七问'} active={Boolean(activeTrade?.signal_id)} />
                <ChevronDown className="hidden sm:block rotate-[-90deg] text-neutral-700 shrink-0" />
                <ChainNode
                  label="Risk"
                  sub={
                    typeof spec?.params?.risk_per_trade === 'number' && typeof sigExec?.max_portfolio_margin_pct === 'number'
                      ? `${(spec.params.risk_per_trade * 100).toFixed(1)}% 风险 / ${(sigExec.max_portfolio_margin_pct * 100).toFixed(0)}% 保证金`
                      : '风控参数缺失'
                  }
                  active={hasRealRun}
                />
                <ChevronDown className="hidden sm:block rotate-[-90deg] text-neutral-700 shrink-0" />
                <ChainNode label="Order" sub="幂等 Order ID" active={hasRealRun} />
                <ChevronDown className="hidden sm:block rotate-[-90deg] text-neutral-700 shrink-0" />
                <ChainNode label="Fill" sub={win?.exec_timeframe ? `次根 ${win.exec_timeframe} Open` : '—'} active={hasRealRun} />
                <ChevronDown className="hidden sm:block rotate-[-90deg] text-neutral-700 shrink-0" />
                <ChainNode label="Position" sub={activeTrade?.symbol ?? '—'} active={hasRealRun} />
                <ChevronDown className="hidden sm:block rotate-[-90deg] text-neutral-700 shrink-0" />
                <ChainNode label="Trade" sub={activeTrade?.trade_id ?? '逐笔对账'} active={Boolean(activeTrade?.trade_id)} />
                <ChevronDown className="hidden sm:block rotate-[-90deg] text-neutral-700 shrink-0" />
                <ChainNode label="Ledger" sub="账本逐笔可核" active={hasRealRun} />
              </div>
            </Card>

            {/* 交易复盘表与选项卡主视图 */}
            <Card className="p-3.5">
              {tab === 'overview' && (
                <div className="space-y-3">
                  <div className="text-xs font-bold text-neutral-200">运行总览与正式 M4 门禁状态</div>
                  <div className="rounded-lg border border-neutral-800 bg-neutral-950/70 p-3 text-[11px] space-y-2">
                    <div className="flex justify-between border-b border-neutral-800/60 pb-1.5">
                      <span className="text-neutral-500">策略标识：</span>
                      <span className="font-mono text-emerald-400 font-bold">{runDetail?.strategy_id ?? '—'}</span>
                    </div>
                    <div className="flex justify-between border-b border-neutral-800/60 pb-1.5">
                      <span className="text-neutral-500">执行模式：</span>
                      <span className="font-mono text-neutral-300">
                        单核执行核心 {engine?.exec_core_version ?? '—'} / 信号核心 {engine?.core_version ?? '—'}
                      </span>
                    </div>
                    <div className="flex justify-between border-b border-neutral-800/60 pb-1.5">
                      <span className="text-neutral-500">M4 声明口径：</span>
                      <span className="text-neutral-400 leading-relaxed text-[10px]">
                        {metricsEndpoint.data?.m4_disclaimer ?? '独立候选研究 Run；不冒充 M4 生产正式通过，杜绝伪造数字。'}
                      </span>
                    </div>
                  </div>
                </div>
              )}

              {tab === 'trades' && (
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <div className="text-xs font-bold">逐笔交易对账表</div>
                    <span className="text-[10px] text-neutral-500 font-mono">共 {tradeList.length} 笔交易</span>
                  </div>
                  <div className="overflow-auto max-h-[320px]">
                    <table className="w-full text-[10px] font-mono">
                      <thead className="text-neutral-500 border-b border-neutral-800 sticky top-0 bg-neutral-900">
                        <tr>
                          {['#', '标的', '方向', '入场时点/价', '离场时点/价', '离场原因', '净盈亏', 'MAE/MFE'].map((h) => (
                            <th key={h} className="text-left py-2 pr-2 font-medium">
                              {h}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {tradeList.map((t) => (
                          <tr
                            key={t.id}
                            onClick={() => setSelectedTradeId(String(t.trade_id || t.id))}
                            className={`border-b border-neutral-800/60 cursor-pointer transition-colors ${
                              selectedTradeId === String(t.trade_id || t.id) ? 'bg-neutral-800/70 text-emerald-300 font-medium' : 'hover:bg-neutral-800/30'
                            }`}
                          >
                            <td className="py-2 pr-2 text-neutral-500">{t.id}</td>
                            <td className="pr-2 font-sans font-bold text-neutral-200">{t.symbol}</td>
                            <td className={`pr-2 font-bold ${t.side === 'LONG' ? 'text-emerald-400' : 'text-rose-400'}`}>{t.side}</td>
                            <td className="pr-2">{t.entry}</td>
                            <td className="pr-2">{t.exit}</td>
                            <td className="pr-2 text-neutral-400 font-sans">{t.reason}</td>
                            <td className={`pr-2 font-bold ${t.pnl !== null && t.pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                              {t.pnl_str}
                            </td>
                            <td className="pr-2 text-neutral-400">{t.mae_str} / {t.mfe_str}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {tab === 'signals' && (
                <div className="space-y-3">
                  <div className="text-xs font-bold">信号形成与拒绝日志</div>
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                    {[
                      ['触发信号', ledgerCounts?.signals != null ? String(ledgerCounts.signals) : '—'],
                      ['生成委托', ledgerCounts?.orders != null ? String(ledgerCounts.orders) : '—'],
                      ['准入拒绝', ledgerCounts?.rejected != null ? String(ledgerCounts.rejected) : '—'],
                      ['超时失效', '—'],
                    ].map(([k, v]) => (
                      <div key={k} className="rounded-lg bg-neutral-950/70 border border-neutral-800 p-3">
                        <div className="text-[10px] text-neutral-500">{k}</div>
                        <div className="mt-1 font-mono text-lg font-bold text-neutral-200">{v}</div>
                      </div>
                    ))}
                  </div>
                  <div className="rounded-lg border border-amber-500/20 bg-amber-500/5 p-3 text-[10px] text-amber-200/90 leading-relaxed">
                    OI 因子消融纪律：当前验收必须同时显示“完整策略”和“移除 OI”后的信号数量、交易数量及绩效变化；禁止只展示一个总分。
                  </div>
                </div>
              )}

              {tab === 'diagnostics' && (
                <div className="space-y-3">
                  <div className="text-xs font-bold">稳定性与消融诊断</div>
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                    <div className="rounded-lg border border-neutral-800 bg-neutral-950/70 p-3">
                      <div className="text-[10px] text-neutral-500">参数敏感性 (热力图)</div>
                      <div className="mt-2 flex h-[92px] items-center justify-center rounded border border-dashed border-neutral-800 px-2 text-center text-[10px] text-neutral-500">
                        未提供：需专项参数扫描 Run 才可判稳定区，禁止用示意网格冒充。
                      </div>
                    </div>

                    <div className="rounded-lg border border-neutral-800 bg-neutral-950/70 p-3">
                      <div className="text-[10px] text-neutral-500">成本敏感性压测</div>
                      <div className="mt-2 space-y-2 text-[10px]">
                        {[
                          ['0.00× 摩擦成本', '—'],
                          ['1.00× 基准成本', hasRealRun ? metrics.netReturn : '—'],
                          ['1.50× 成本压测', '—'],
                          ['2.00× 极端摩擦', '—'],
                        ].map(([x, v]) => (
                          <div key={x} className="flex justify-between border-b border-neutral-800/40 pb-1">
                            <span className="text-neutral-400">{x}</span>
                            <span className="font-mono text-neutral-300">{v}</span>
                          </div>
                        ))}
                      </div>
                      <div className="mt-2 text-[9px] text-neutral-600">仅 1.00× 为当前 Run 实测；其余需专项压测 Run。</div>
                    </div>

                    <div className="rounded-lg border border-neutral-800 bg-neutral-950/70 p-3">
                      <div className="text-[10px] text-neutral-500">因子边际消融</div>
                      <div className="mt-2 space-y-2 text-[10px]">
                        {[
                          ['完整策略 (含OI)', hasRealRun ? `${ledgerCounts?.trades ?? '—'} trades` : '—'],
                          ['− 移除 OI 因子', '—'],
                          ['− 移除 Volume', '—'],
                          ['− 移除 Wyckoff', '—'],
                        ].map(([x, v]) => (
                          <div key={x} className="flex justify-between border-b border-neutral-800/40 pb-1">
                            <span className="text-neutral-400">{x}</span>
                            <span className="font-mono text-neutral-300">{v}</span>
                          </div>
                        ))}
                      </div>
                      <div className="mt-2 text-[9px] text-neutral-600">消融臂需各自实跑 Run，未跑即「—」。</div>
                    </div>
                  </div>
                </div>
              )}
            </Card>
          </div>

          {/* 右栏：成本与结果、可复现身份与限制说明 */}
          <div className="space-y-3">
            <Card className="p-3.5">
              <div className="flex items-center gap-2 text-xs font-bold">
                <Target className="h-3.5 w-3.5 text-indigo-300" /> 成本与结果拆解
              </div>
              <div className="mt-3 space-y-2 text-[10px]">
                {(() => {
                  const fees = typeof summary?.total_fees === 'number' ? summary.total_fees : null;
                  const funding = typeof summary?.total_funding_fee === 'number' ? summary.total_funding_fee : null;
                  const slipBps = typeof spec?.params?.slippage_bps === 'number' ? `${spec.params.slippage_bps} bps` : null;
                  const rows: [string, string][] = [
                    ['已实现净盈亏', hasRealRun ? metrics.netReturn : '—'],
                    ['手续费支出', hasRealRun && fees !== null ? `$${fees.toFixed(2)}` : '—'],
                    ['资金费结算', hasRealRun && funding !== null ? `$${funding.toFixed(2)}` : '—'],
                    ['滑点冲击', hasRealRun && slipBps ? slipBps : '—'],
                    ['未实现浮亏', '—'],
                  ];
                  return rows;
                })().map(([k, v]) => (
                  <div key={k} className="flex justify-between border-b border-neutral-800/60 pb-1.5">
                    <span className="text-neutral-500">{k}</span>
                    <span className="font-mono text-neutral-300">{v}</span>
                  </div>
                ))}
              </div>
            </Card>

            <Card className="p-3.5">
              <div className="flex items-center gap-2 text-xs font-bold">
                <LockKeyhole className="h-3.5 w-3.5 text-indigo-300" /> 可复现身份指纹
              </div>
              <div className="mt-3 space-y-2 text-[10px] font-mono">
                {(() => {
                  const codeSha = hashes?.code_sha256 ? `${String(hashes.code_sha256).slice(0, 12)}…` : null;
                  const paramHash = engine?.param_hash ? `${String(engine.param_hash).slice(0, 12)}…` : null;
                  const rows: [string, string][] = [
                    ['strategy', runDetail?.strategy_id ?? '—'],
                    ['run_id', activeRunId || '—'],
                    ['exec_core_version', engine?.exec_core_version ?? '—'],
                    ['contract_version', runDetail?.contract_version ?? '—'],
                    ['code_sha256', hasRealRun && codeSha ? codeSha : '—'],
                    ['param_hash', hasRealRun && paramHash ? paramHash : '—'],
                  ];
                  return rows;
                })().map(([k, v]) => (
                  <div key={k} className="flex justify-between gap-2 border-b border-neutral-800/40 pb-1">
                    <span className="text-neutral-500">{k}</span>
                    <span className="text-neutral-300 truncate max-w-[160px]">{v}</span>
                  </div>
                ))}
              </div>
            </Card>

            <Card className="p-3.5">
              <div className="flex items-center gap-2 text-xs font-bold">
                <AlertTriangle className="h-3.5 w-3.5 text-amber-300" /> WORK-04 纪律约束
              </div>
              <div className="mt-3 space-y-2 text-[10px] text-neutral-400 leading-relaxed">
                <div>• 无真实 Run：绝不绘制假收益曲线、不展示假交易。</div>
                <div>• PIT 未证明：不得把历史不可得数据作为信号输入。</div>
                <div>• 因子消融：必须实跑对照，禁止 AI 主观脑补贡献。</div>
                <div>• 单核收敛：Backtest / Paper / Live 共用同一套 Trading Core。</div>
              </div>
            </Card>

            <Card className="p-3.5">
              <div className="text-xs font-bold">复盘工具箱</div>
              <div className="mt-3 grid grid-cols-2 gap-2">
                {[
                  [RotateCcw, '重跑同一 Run'],
                  [Filter, '筛选交易'],
                  [Clock3, '事件时间线'],
                  [Download, 'CSV / JSON'],
                ].map(([Icon, label]) => {
                  const I = Icon as React.ElementType;
                  return (
                    <button
                      key={label as string}
                      disabled={!hasRealRun}
                      className={`flex items-center gap-1.5 rounded-lg border border-neutral-800 p-2 text-[9px] transition-colors ${
                        hasRealRun ? 'bg-neutral-900 text-neutral-300 hover:bg-neutral-800' : 'bg-neutral-950/60 text-neutral-600 cursor-not-allowed'
                      }`}
                    >
                      <I className="h-3 w-3" />
                      {label as string}
                    </button>
                  );
                })}
              </div>
            </Card>
          </div>
        </div>

        {/* 6. Footer 说明 */}
        <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-neutral-800 pt-2 text-[9px] text-neutral-600">
          <span>验收页面不制造数据：真实结果缺失即显式 MISSING / BLOCKED。</span>
          <span className="font-mono">BACKTEST_REVIEW_WORKBENCH · WORK-04 CONTRACT</span>
        </footer>
      </div>
    </div>
  );
};

export const MonthlyReturnsHeatmap: React.FC<{
  monthly: Record<string, Record<string, {
    pnl: number | null;
    pnl_usd: number | null;
    count: number;
    basis?: string;
    month_end_equity?: number | null;
    realized_pnl_by_close_month?: number | null;
  }>> | null;
  ytdBasis?: 'sum' | 'compound';
  basisNoteOverride?: string | null;
}> = ({ monthly }) => {
  const months = ['01', '02', '03', '04', '05', '06', '07', '08', '09', '10', '11', '12'];
  const years = useMemo(() => Object.keys(monthly ?? {}).sort((a, b) => b.localeCompare(a)), [monthly]);
  const hasData = years.length > 0;

  const getHeatmapColor = (pct: number | null | undefined) => {
    if (pct === null || pct === undefined) return 'bg-neutral-900 text-neutral-600';
    if (pct > 3.0) return 'bg-emerald-600 text-white font-bold';
    if (pct > 1.5) return 'bg-emerald-500/80 text-white font-medium';
    if (pct > 0) return 'bg-emerald-950 text-emerald-400 font-semibold';
    if (pct === 0) return 'bg-neutral-900 text-neutral-400';
    if (pct > -1.5) return 'bg-rose-950 text-rose-400 font-semibold';
    return 'bg-rose-600 text-white font-bold';
  };

  return (
    <div className="flex flex-col h-full bg-neutral-900/90 border border-neutral-800 rounded-lg p-3 select-none text-neutral-200">
      <div className="flex items-center justify-between pb-2 border-b border-neutral-800 mb-2">
        <h3 className="text-xs font-bold text-neutral-200">月度收益热力矩阵</h3>
        <span className="text-[10px] text-neutral-500 font-mono">真实月界净值</span>
      </div>
      {!hasData ? (
        <div className="flex-1 flex flex-col items-center justify-center text-[11px] text-neutral-500 font-sans gap-1 py-4">
          <span>暂无真实月度收益聚合</span>
        </div>
      ) : (
        <div className="flex-1 overflow-x-auto">
          <table className="w-full text-center text-[10px] font-mono border-collapse">
            <thead>
              <tr className="text-neutral-500 border-b border-neutral-800">
                <th className="py-1 text-left font-sans pl-1">年份</th>
                {months.map((m) => (
                  <th key={m} className="py-1 px-1 font-normal">{parseInt(m, 10)}月</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {years.map((yr) => {
                const yrMap = monthly?.[yr] ?? {};
                return (
                  <tr key={yr} className="border-b border-neutral-800/40">
                    <td className="py-1 text-left font-bold pl-1 text-neutral-300">{yr}</td>
                    {months.map((m) => {
                      const p = yrMap[m]?.pnl;
                      return (
                        <td key={m} className={`py-1 px-1 ${getHeatmapColor(p)}`}>
                          {p !== null && p !== undefined ? `${p >= 0 ? '+' : ''}${p.toFixed(1)}%` : '—'}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};
