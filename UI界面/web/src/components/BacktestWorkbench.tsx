/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * **单策略回测工作台**（`WX_OI_RESONANCE_V1`）—— 真实 API 接线版。
 *
 * 六个屏（全部接真数据，缺失一律 `—` 并说明，绝不造数）
 * ──────────────────────────────────────────────────
 * ① 配置与运行：唯一策略 + 版本、交易对（真实池）、交易所/市场/周期（未支持项**禁用并解释**）、
 *    UTC 起止、初始资金、单边手续费、滑点 bps、风险/仓位、策略参数（白名单校验 + 后台快照）。
 * ② 收益指标：收益率 / 最大回撤 / 胜率 / 平均盈亏比 / profit factor / 交易次数 / 手续费 / 资金费，
 *    并展示**所选 run 的实际时间/配置/版本**（表单未运行的新值绝不冒充结果配置）。
 * ③ 资金分析：策略净值 vs 买入持有基准、回撤曲线、月度热力图（见 `LabFundingPanel`）。
 * ④ 执行视图：真实 15m K 线 / 原始成交量 / 信号 / 开平仓 / 保护止损轨迹 + 选中定位（见 `LabChartPanel`）。
 * ⑤ 明细表 + 单笔详情（signal_id / 原始规则 / ATR / 结构证据 / 订单 / 止损轨迹 / 成本 / 资金费）。
 * ⑥ 实验记录 + 质量报告（expected/actual/缺口、OI 门禁、资金费覆盖、对账、快照、hash）。
 *
 * 纪律
 * ────
 * · **不在浏览器重算交易与止损**；所有成交/止损/成本只来自后台账本与 chart 载荷。
 * · 运行完成的「执行状态」与「正式策略放行 / M4 验收」**分开显示**（后者来自 `formal_m4_gate`）。
 * · 非 `succeeded` 的运行**一律不得显示为绿色成功**（即便 HTTP 200）。
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle,
  CheckCircle2,
  Clock,
  FlaskConical,
  Info,
  Play,
  RefreshCw,
  XCircle,
} from 'lucide-react';
import {
  LAB_EXCHANGE_OPTIONS,
  LAB_EXEC_TF_OPTIONS,
  LAB_MARKET_OPTIONS,
  LAB_MIN_WINDOW_MS,
  LAB_PARAM_SPECS,
  LAB_SIGNAL_TF_OPTIONS,
  LAB_STRATEGY_ID,
  LAB_UNIT_LABEL,
  LAB_WINDOW_GRID_MS,
  labCreateRun,
  labErrorsOf,
  labFetchRunHashMap,
  labFromWire,
  labGetChart,
  labGetEquity,
  labGetMetrics,
  labGetPool,
  labGetQuality,
  labGetRun,
  labGetTrade,
  labGetTrades,
  labListRuns,
  labLoadDefaults,
  labToWire,
  labValidateParams,
  type LabChart,
  type LabChartResponse,
  type LabDefaults,
  type LabEquityPoint,
  type LabEquityResponse,
  type LabMetricsResponse,
  type LabParamSpec,
  type LabPoolItem,
  type LabQualityResponse,
  type LabRegistryEntry,
  type LabRunDetail,
  type LabRunHashes,
  type LabRunSummary,
  type LabTradeDetail,
  type LabTradeRow,
  type LabTradesResponse,
} from '../api/labBacktest';
import { LabChartPanel } from './LabChartPanel';
import { LabFundingPanel } from './LabFundingPanel';

/* ══════════════════════════ 格式化（未知一律 '—'）══════════════════════════ */

const fmtNum = (v: number | null | undefined, digits = 2): string =>
  v === null || v === undefined || !Number.isFinite(v)
    ? '—'
    : v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });

const fmtUsd = (v: number | null | undefined, digits = 2): string =>
  v === null || v === undefined || !Number.isFinite(v) ? '—' : `$${fmtNum(v, digits)}`;

const fmtSignedUsd = (v: number | null | undefined, digits = 2): string => {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  return `${v > 0 ? '+' : v < 0 ? '-' : ''}$${fmtNum(Math.abs(v), digits)}`;
};

const fmtPct = (v: number | null | undefined, digits = 4): string =>
  v === null || v === undefined || !Number.isFinite(v) ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(digits)}%`;

const fmtUtc = (ms: number | null | undefined): string => {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms <= 0) return '—';
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return '—';
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
};

const fmtIsoUtc = (iso: string | null | undefined): string => {
  if (!iso) return '—';
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? `${fmtUtc(ms)} UTC` : iso;
};

const shortHash = (h: string | null | undefined, n = 12): string =>
  !h ? '—' : h.length <= n ? h : `${h.slice(0, n)}…`;

/** `YYYY-MM-DDTHH:mm`（按 **UTC** 解释）→ 毫秒；非法 ⇒ null。 */
function parseUtcInput(raw: string): number | null {
  const s = raw.trim().replace(' ', 'T');
  if (!s) return null;
  const norm = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(s) ? `${s}:00Z` : /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(s) ? `${s}Z` : s;
  if (!/(Z|[+-]\d{2}:\d{2})$/.test(norm)) return null;
  const ms = Date.parse(norm);
  return Number.isFinite(ms) ? ms : null;
}

function toUtcInput(ms: number | null): string {
  if (ms === null || !Number.isFinite(ms)) return '';
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}T${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
}

function toIsoUtcZ(ms: number): string {
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}T${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}Z`;
}

const STATUS_STYLE: Record<string, string> = {
  succeeded: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  queued: 'bg-sky-50 text-sky-700 border-sky-200',
  running: 'bg-sky-50 text-sky-700 border-sky-200',
  failed: 'bg-rose-50 text-rose-700 border-rose-200',
  interrupted: 'bg-amber-50 text-amber-800 border-amber-200',
};

const statusStyle = (s: string | null | undefined): string =>
  STATUS_STYLE[s ?? ''] ?? 'bg-neutral-100 text-neutral-700 border-neutral-200';

/* ══════════════════════════ 只读取数 hook ══════════════════════════ */

interface AsyncState<T> {
  /** **本状态所属的请求键**。返回时只把 `key` 与当前请求键一致的状态交出去。 */
  key: string | null;
  data: T | null;
  error: string | null;
  loading: boolean;
}

/**
 * 只读取数 hook。**身份安全**：状态自带 `key`，返回值只在 `state.key === key` 时给出，
 * 否则一律返回「空数据 + loading」—— 这样**切换 run / 参数时不会有一帧读到旧结果**，
 * 也不会出现 A run 的指标挂到 B run 标题下的串身份显示。
 *
 * 失败语义：取数失败 ⇒ `data = null` + `error`（**清掉旧结果**，不把上一份数据顶上来冒充本次结果）；
 * 运行自身的失败（`status=failed`）不在这里处理，由运行状态块按 `failure_reason` 如实显示。
 */
function useLabFetch<T>(key: string | null, loader: () => Promise<T>): AsyncState<T> & { reload: () => void } {
  const [state, setState] = useState<AsyncState<T>>({ key: null, data: null, error: null, loading: false });
  const [tick, setTick] = useState(0);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;
  /** 请求在途标记：轮询节拍据此跳过，避免堆叠请求 */
  const inflightRef = useRef(false);

  useEffect(() => {
    if (key === null) {
      inflightRef.current = false;
      setState({ key: null, data: null, error: null, loading: false });
      return;
    }
    let alive = true;
    inflightRef.current = true;
    // 立刻清掉上一份结果（不保留旧值），保证「切换即清」
    setState({ key, data: null, error: null, loading: true });
    loaderRef
      .current()
      .then((d) => {
        if (alive) {
          inflightRef.current = false;
          setState({ key, data: d, error: null, loading: false });
        }
      })
      .catch((e: unknown) => {
        if (alive) {
          inflightRef.current = false;
          setState({ key, data: null, error: labErrorsOf(e)[0] ?? '取数失败', loading: false });
        }
      });
    return () => {
      alive = false;
      inflightRef.current = false;
    };
  }, [key, tick]);

  const reload = useCallback(() => {
    if (inflightRef.current) return; // 在途则跳过，防止轮询堆叠
    setTick((t) => t + 1);
  }, []);

  const current: AsyncState<T> =
    state.key === key
      ? state
      : { key, data: null, error: null, loading: key !== null };

  return { ...current, reload };
}

/**
 * 响应身份核对：详情 / 指标 / 图表 / 交易等响应必须与**所选 run_id** 一致。
 * 不一致 ⇒ 抛错（页面显示为取数错误），绝不把别的 run 的数字挂到当前标题下。
 */
function expectRunId<T extends { run_id?: string | null }>(env: T, expected: string): T {
  const got = env && typeof env === 'object' ? env.run_id : null;
  if (got && got !== expected) {
    throw new Error(`响应身份不符：run_id=${got} ≠ 所选 ${expected}`);
  }
  return env;
}

/* ══════════════════════════ 小构件 ══════════════════════════ */

const Chip: React.FC<{ children: React.ReactNode; tone?: 'neutral' | 'ok' | 'warn' | 'bad' | 'info' }> = ({
  children,
  tone = 'neutral',
}) => {
  const map: Record<string, string> = {
    neutral: 'bg-neutral-100 text-neutral-700 border-neutral-200',
    ok: 'bg-emerald-50 text-emerald-700 border-emerald-200',
    warn: 'bg-amber-50 text-amber-800 border-amber-200',
    bad: 'bg-rose-50 text-rose-700 border-rose-200',
    info: 'bg-sky-50 text-sky-700 border-sky-200',
  };
  return <span className={`px-1.5 py-0.5 rounded border text-[10px] font-mono whitespace-nowrap ${map[tone]}`}>{children}</span>;
};

const KV: React.FC<{ k: string; v: React.ReactNode; mono?: boolean }> = ({ k, v, mono = true }) => (
  <div className="flex items-baseline gap-1.5 min-w-0">
    <span className="text-neutral-500 shrink-0">{k}</span>
    <span className={`text-neutral-200 truncate ${mono ? 'font-mono' : ''}`} title={typeof v === 'string' ? v : undefined}>
      {v}
    </span>
  </div>
);

const SectionCard: React.FC<{ title: string; right?: React.ReactNode; children: React.ReactNode; testid?: string }> = ({
  title,
  right,
  children,
  testid,
}) => (
  /**
   * 卡片容器**显式**声明深色文字：Lab 根容器是深色画布（`bg-neutral-100`）且带浅色文字，
   * 若卡内不覆盖就会继承成「近白字落白底」。本项目 index.css 把中性色阶整体反转，
   * 因此「浅底上的深字」对应 `text-neutral-100/200/300/400`。
   */
  <div
    className="bg-white border border-neutral-200 rounded-lg shadow-sm overflow-hidden text-neutral-100"
    data-testid={testid}
  >
    <div className="px-3 py-2 border-b border-neutral-200 bg-neutral-50 flex flex-wrap items-center justify-between gap-2">
      <h3 className="text-xs font-bold text-neutral-100">{title}</h3>
      {right}
    </div>
    <div className="p-3">{children}</div>
  </div>
);

/* ══════════════════════════ 配置表单 ══════════════════════════ */

interface LabForm {
  symbol: string;
  exchange: string;
  market: string;
  signalTf: string;
  execTf: string;
  windowStart: string;
  windowEnd: string;
  warmup: string;
  /** 参数输入（**显示值**字符串；空 = 沿用后台冻结默认，不发送覆盖） */
  params: Record<string, string>;
}

const EMPTY_FORM: LabForm = {
  symbol: 'BTCUSDT',
  exchange: 'binance',
  market: 'um',
  signalTf: '1h',
  execTf: '15m',
  windowStart: '',
  windowEnd: '',
  warmup: '',
  params: {},
};

/**
 * 紧凑分段选择：标签在左、按钮在右（一行内），未支持项 `disabled` + **简短 tooltip**。
 * 不再为每个禁用项重复打印 ALLOWED_* 长说明（原因统一收进 title，保持首屏紧凑）。
 */
const OptionRow: React.FC<{
  label: string;
  value: string;
  options: { value: string; label: string; enabled: boolean; reason?: string }[];
  onChange: (v: string) => void;
  /** 紧凑模式：标签与按钮同一行（默认 true） */
  inline?: boolean;
}> = ({ label, value, options, onChange, inline = true }) => (
  <div className={inline ? 'flex items-center gap-1.5 min-w-0' : 'space-y-0.5'}>
    <div className="text-[10px] text-neutral-500 shrink-0">{label}</div>
    <div className="flex flex-wrap items-center gap-1">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          disabled={!o.enabled}
          title={o.enabled ? `${label} = ${o.value}` : o.reason}
          onClick={() => o.enabled && onChange(o.value)}
          className={`px-1.5 py-0.5 rounded border text-[10px] font-mono transition-colors ${
            !o.enabled
              ? 'bg-neutral-50 text-neutral-300 border-neutral-200 cursor-not-allowed line-through'
              : value === o.value
                ? 'bg-emerald-600 text-white border-emerald-600 font-bold'
                : 'bg-white text-neutral-300 border-neutral-200 hover:bg-neutral-50'
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  </div>
);

/** 参数输入：清楚标出单位（小数 / 百分数 / bps / USD / 根 / 倍）与等价换算；白名单代码名收进 tooltip。 */
const ParamField: React.FC<{
  spec: LabParamSpec;
  value: string;
  onChange: (v: string) => void;
}> = ({ spec, value, onChange }) => {
  const shown = value.trim() === '' ? null : Number(value);
  const wire = shown !== null && Number.isFinite(shown) ? labToWire(spec, shown) : null;
  let equiv: string | null = null;
  if (wire !== null) {
    if (spec.key === 'fee_rate') equiv = `= ${(wire * 10000).toFixed(2)} bps / ${(wire * 100).toFixed(4)}%`;
    else if (spec.unit === 'percent') equiv = `= ${wire}（小数）`;
    else if (spec.unit === 'bps') equiv = `= ${(wire / 10000).toFixed(6)}（小数）`;
    else if (spec.unit === 'usd') equiv = `= ${wire.toLocaleString()}`;
  }
  const bounds =
    spec.min !== null || spec.max !== null ? `[${spec.min ?? '-∞'}, ${spec.max ?? '+∞'}]` : null;
  return (
    <label className="block space-y-0.5 min-w-0" title={`${spec.key}${spec.hint ? ` · ${spec.hint}` : ''}`}>
      <span className="text-[10px] text-neutral-400 flex items-center gap-1">
        <span className="truncate">{spec.label}</span>
        <Chip tone={spec.unit === 'percent' ? 'info' : 'neutral'}>{LAB_UNIT_LABEL[spec.unit]}</Chip>
      </span>
      <input
        type="text"
        inputMode="decimal"
        value={value}
        placeholder={spec.type === 'int' ? '整数' : '数值'}
        onChange={(e) => onChange(e.target.value)}
        className="w-full px-1.5 py-1 text-xs rounded border border-neutral-200 bg-white text-neutral-200 focus:outline-none focus:border-emerald-500 font-mono"
        data-param-key={spec.key}
      />
      {(equiv || bounds) && (
        <span className="text-[10px] text-neutral-400 block truncate">
          {equiv}
          {equiv && bounds ? ' · ' : ''}
          {bounds}
        </span>
      )}
    </label>
  );
};

/* ══════════════════════════ 单笔详情 ══════════════════════════ */

interface StopTracePoint {
  ts: number;
  stop: number;
  reason: string;
}

function parseStopTrace(raw: string | null | undefined): StopTracePoint[] {
  if (!raw) return [];
  try {
    const arr = JSON.parse(raw);
    if (!Array.isArray(arr)) return [];
    return arr
      .filter((p) => p && Number.isFinite(p.ts) && Number.isFinite(p.stop))
      .map((p) => ({ ts: p.ts as number, stop: p.stop as number, reason: String(p.reason ?? '') }));
  } catch {
    return [];
  }
}

const LabTradeDetailPanel: React.FC<{ detail: LabTradeDetail | null; loading: boolean; error: string | null }> = ({
  detail,
  loading,
  error,
}) => {
  if (loading && !detail) return <div className="text-[11px] text-neutral-500 p-3">正在读取单笔详情…</div>;
  if (error) return <div className="text-[11px] text-rose-600 p-3">单笔详情取数失败：{error}</div>;
  if (!detail?.trade) return <div className="text-[11px] text-neutral-400 p-3">点击明细行查看单笔详情</div>;

  const t = detail.trade;
  const sig = detail.original_signal;
  const orders = detail.orders ?? [];
  const funding = detail.funding ?? [];
  const trace = parseStopTrace(t.stop_trace_json);

  return (
    <div className="space-y-2 text-[11px]" data-testid="lab-trade-detail">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-bold text-neutral-100">trade_id = {t.trade_id}</span>
        <Chip tone="info">signal_id = {detail.signal_id ?? '—'}</Chip>
        <Chip>{t.symbol}</Chip>
        <Chip tone={t.net_pnl >= 0 ? 'ok' : 'bad'}>{t.direction}</Chip>
        <Chip>{t.exit_reason}</Chip>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-1 font-mono text-[11px]">
        <KV k="进场" v={`${fmtUtc(t.entry_time_ms)} @ ${fmtNum(t.entry_price, 4)}`} />
        <KV k="出场" v={`${fmtUtc(t.exit_time_ms)} @ ${fmtNum(t.exit_price, 4)}`} />
        <KV k="数量" v={fmtNum(t.qty, 6)} />
        <KV k="持仓小时" v={fmtNum(t.holding_hours, 2)} />
        <KV k="毛盈亏" v={fmtSignedUsd(t.gross_pnl, 2)} />
        <KV k="手续费" v={`${fmtUsd(t.fees, 2)}（入 ${fmtNum(t.entry_fee, 2)} / 出 ${fmtNum(t.exit_fee, 2)}）`} />
        <KV k="资金费" v={`${fmtUsd(t.funding_fee, 2)}（${t.funding_events ?? 0} 事件）`} />
        <KV k="净盈亏" v={fmtSignedUsd(t.net_pnl, 2)} />
        <KV k="MFE / MAE" v={`${fmtNum(t.mfe, 6)} / ${fmtNum(t.mae, 6)}`} />
        <KV k="funding_gaps" v={t.funding_gaps_json ?? '—'} />
      </div>

      <div className="border-t border-neutral-100 pt-2">
        <div className="font-bold text-neutral-200 mb-1">原始信号（signal_id 规则 / ATR / 结构证据）</div>
        {sig ? (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-1 font-mono">
            <KV k="kind" v={sig.kind} />
            <KV k="direction" v={sig.direction} />
            <KV k="signal_bar_open" v={fmtUtc(sig.signal_bar_open_ms)} />
            <KV k="confirm" v={fmtUtc(sig.confirm_ms)} />
            <KV k="stop_price" v={fmtNum(sig.stop_price, 4)} />
            <KV k="atr" v={fmtNum(sig.atr, 6)} />
            <KV k="oi_score" v={fmtNum(sig.oi_score, 6)} />
            <KV k="oi_available" v={String(sig.oi_available ?? '—')} />
            <KV k="oi_quality" v={sig.oi_quality ?? '—'} />
            <KV k="rank_key" v={sig.rank_key ?? '—'} />
            <KV k="in_window" v={String(sig.in_window ?? '—')} />
          </div>
        ) : (
          <div className="text-neutral-400">后台未返回该 signal_id 的原始信号行（不编造）</div>
        )}
      </div>

      <div className="border-t border-neutral-100 pt-2">
        <div className="font-bold text-neutral-200 mb-1">订单与成交（账本 orders）</div>
        {orders.length === 0 ? (
          <div className="text-neutral-400">无订单行</div>
        ) : (
          <table className="w-full text-left font-mono">
            <thead className="text-neutral-500 border-b border-neutral-200">
              <tr>
                <th className="py-1 pr-2">时点</th>
                <th className="py-1 px-2">phase</th>
                <th className="py-1 px-2">side</th>
                <th className="py-1 px-2">action</th>
                <th className="py-1 px-2 text-right">价格</th>
                <th className="py-1 px-2 text-right">数量</th>
                <th className="py-1 px-2 text-right">手续费</th>
                <th className="py-1 px-2 text-right">止损价</th>
                <th className="py-1 px-2">原因</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-neutral-100">
              {orders.map((o) => (
                <tr key={o.order_id}>
                  <td className="py-1 pr-2 text-neutral-500">{fmtUtc(o.ts_ms)}</td>
                  <td className="py-1 px-2">{o.phase}</td>
                  <td className="py-1 px-2">{o.side}</td>
                  <td className="py-1 px-2">{o.action}</td>
                  <td className="py-1 px-2 text-right">{fmtNum(o.price, 4)}</td>
                  <td className="py-1 px-2 text-right">{fmtNum(o.qty, 6)}</td>
                  <td className="py-1 px-2 text-right">{fmtNum(o.fee, 6)}</td>
                  <td className="py-1 px-2 text-right">{fmtNum(o.stop_price, 4)}</td>
                  <td className="py-1 px-2 text-neutral-500">{o.reason || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="border-t border-neutral-100 pt-2 grid grid-cols-1 md:grid-cols-2 gap-3">
        <div>
          <div className="font-bold text-neutral-200 mb-1">保护止损轨迹（引擎实况 {trace.length} 点）</div>
          {trace.length === 0 ? (
            <div className="text-neutral-400">无止损轨迹记录</div>
          ) : (
            <div className="max-h-40 overflow-auto">
              <table className="w-full text-left font-mono">
                <thead className="text-neutral-500 border-b border-neutral-200">
                  <tr>
                    <th className="py-1 pr-2">时点</th>
                    <th className="py-1 px-2 text-right">止损价</th>
                    <th className="py-1 px-2">原因</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-neutral-100">
                  {trace.map((p, i) => (
                    <tr key={`${p.ts}-${i}`}>
                      <td className="py-1 pr-2 text-neutral-500">{fmtUtc(p.ts)}</td>
                      <td className="py-1 px-2 text-right">{fmtNum(p.stop, 4)}</td>
                      <td className="py-1 px-2 text-neutral-500">{p.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
        <div>
          <div className="font-bold text-neutral-200 mb-1">资金费事件（{funding.length} 条）</div>
          {funding.length === 0 ? (
            <div className="text-neutral-400">该笔无资金费事件</div>
          ) : (
            <table className="w-full text-left font-mono">
              <thead className="text-neutral-500 border-b border-neutral-200">
                <tr>
                  <th className="py-1 pr-2">结算时点</th>
                  <th className="py-1 px-2 text-right">rate</th>
                  <th className="py-1 px-2 text-right">mark</th>
                  <th className="py-1 px-2 text-right">数量</th>
                  <th className="py-1 px-2 text-right">金额</th>
                  <th className="py-1 px-2">来源</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-neutral-100">
                {funding.map((f) => (
                  <tr key={`${f.funding_time_ms}-${f.amount}`}>
                    <td className="py-1 pr-2 text-neutral-500">{fmtUtc(f.funding_time_ms)}</td>
                    <td className="py-1 px-2 text-right">{f.rate}</td>
                    <td className="py-1 px-2 text-right">{fmtNum(f.mark, 2)}</td>
                    <td className="py-1 px-2 text-right">{fmtNum(f.qty, 6)}</td>
                    <td className="py-1 px-2 text-right">{fmtNum(f.amount, 6)}</td>
                    <td className="py-1 px-2 text-neutral-500">{f.source}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
};

/* ══════════════════════════ 质量报告 ══════════════════════════ */

const BoolFlag: React.FC<{ label: string; v: boolean | null | undefined; good?: boolean }> = ({ label, v, good = true }) => {
  if (v === null || v === undefined) return <KV k={label} v="—" />;
  const ok = good ? v : !v;
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="text-neutral-500">{label}</span>
      <span className={`font-mono font-semibold ${ok ? 'text-emerald-700' : 'text-rose-600'}`}>{v ? 'true' : 'false'}</span>
    </div>
  );
};

const LabQualityPanel: React.FC<{ q: LabQualityResponse | null; loading: boolean; error: string | null }> = ({
  q,
  loading,
  error,
}) => {
  const dq = q?.data_quality ?? null;
  const hashes = q?.hashes ?? null;
  const envStatus = q?.status ?? null;
  const qualityStatus = q?.quality?.status ?? null;
  /**
   * 绿色「通过」的**全部**前提（缺一不可）：
   *   ① 运行 status === 'succeeded'；
   *   ② 信封 quality.status === 'ok'（**不接受 null**：质量未知不得当成功）；
   *   ③ `data_quality` 实际存在（没有质量明细就无法断言通过）；
   *   ④ 本次质量请求没有取数错误。
   * 任何一条不满足 ⇒ 一律按异常显示（HTTP 200 不等于成功）。
   */
  const qualityOk = !error && envStatus === 'succeeded' && qualityStatus === 'ok' && dq !== null;
  const verdict = error
    ? `取数失败（不得视为成功）：${error}`
    : envStatus !== 'succeeded'
      ? `运行未成功（status=${envStatus ?? '—'}）`
      : qualityStatus !== 'ok'
        ? `质量未通过（quality.status=${qualityStatus ?? 'null'}）`
        : dq === null
          ? '缺 data_quality 明细 ⇒ 不能断言通过'
          : '通过（运行成功 + 质量 ok + 有质量明细）';

  if (loading && !q) return <div className="text-[11px] text-neutral-500">正在读取质量报告…</div>;
  if (error && !q) return <div className="text-[11px] text-rose-600">质量报告取数失败：{error}</div>;
  if (!q) return <div className="text-[11px] text-neutral-400">选择一次运行查看质量报告</div>;

  return (
    <div className="space-y-2 text-[11px]" data-testid="lab-quality-panel">
      <div
        className={`rounded px-2 py-1.5 border flex flex-wrap items-center gap-2 ${
          qualityOk ? 'bg-emerald-50 border-emerald-200 text-emerald-800' : 'bg-rose-50 border-rose-200 text-rose-800'
        }`}
        data-testid="lab-quality-verdict"
      >
        <span className="font-bold flex items-center gap-1">
          {qualityOk ? <CheckCircle2 className="w-3.5 h-3.5" /> : <XCircle className="w-3.5 h-3.5" />}
          质量结论：{verdict}
        </span>
        <span className="font-mono">
          运行 status {envStatus ?? '—'} · 信封 quality.status {qualityStatus ?? 'null'} · data_quality {dq ? '存在' : '缺失'}
        </span>
        {!qualityOk && (
          <span>（HTTP 200 不等于成功：运行未成功 / 质量非 ok / 缺质量明细一律按异常显示，不得当绿色成功）</span>
        )}
        {q.quality?.note && <span className="text-neutral-300">说明：{q.quality.note}</span>}
        {q.missing_reason && <span className="text-neutral-300">缺失原因：{q.missing_reason}</span>}
      </div>

      {dq ? (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-1 font-mono">
            <BoolFlag label="信号全窗覆盖" v={dq.signal_full_window_ok} />
            <BoolFlag label="执行全窗覆盖" v={dq.exec_full_window_ok} />
            <BoolFlag label="信号连续" v={dq.signal_contiguous} />
            <BoolFlag label="执行连续" v={dq.exec_contiguous} />
            <BoolFlag label="全部已收盘" v={dq.all_closed} />
            <KV k="信号 bar expected/actual" v={`${dq.signal_expected_bars ?? '—'} / ${dq.signal_actual_bars ?? '—'}`} />
            <KV k="执行 bar expected/actual" v={`${dq.exec_expected_bars ?? '—'} / ${dq.exec_actual_bars ?? '—'}`} />
            <KV k="OI 缺根" v={String(dq.oi_bars_missing ?? '—')} />
            <KV k="OI 假填充" v={dq.oi_fake_fill ?? '—'} />
            <KV k="资金费模型" v={dq.funding_model ?? '—'} />
            <BoolFlag label="资金费为假设值" v={dq.funding_is_assumption} good={false} />
            <KV k="资金费 expected/actual" v={`${dq.funding_expected_settlements ?? '—'} / ${dq.funding_actual_settlements ?? '—'}`} />
            <KV k="资金费缺事件" v={String(dq.funding_missing_settlements_count ?? '—')} />
            <BoolFlag label="资金费覆盖 ok" v={dq.funding_coverage_ok} />
            <KV k="资金费 used/recomputed" v={`${dq.funding_events_used ?? '—'} / ${dq.funding_events_recomputed ?? '—'}`} />
            <BoolFlag label="OI 门禁启用" v={dq.oi_gate_enabled} good={false} />
            <KV k="OI 门槛值" v={String(dq.oi_min_resonance ?? '—')} />
            <BoolFlag label="对账 ok" v={dq.reconcile_ok} />
            <BoolFlag label="指标与账本一致" v={dq.metrics_match} />
            <KV k="指标差异" v={dq.metrics_match_diffs && dq.metrics_match_diffs.length ? dq.metrics_match_diffs.join(' | ') : '无'} />
          </div>

          {dq.oi_gate_note && <div className="text-[10px] text-amber-800 bg-amber-50 border border-amber-200 rounded px-2 py-1">OI 门禁：{dq.oi_gate_note}</div>}
          {dq.funding_evidence_reconcile && (
            <div className="text-[10px] text-neutral-400">资金费证据对账：{dq.funding_evidence_reconcile}</div>
          )}
          {dq.time_precision_note && <div className="text-[10px] text-neutral-500">时间精度：{dq.time_precision_note}</div>}

          {dq.reconcile && (
            <div className="border-t border-neutral-100 pt-2">
              <div className="font-bold text-neutral-200 mb-1">现金恒等式对账（reconcile）</div>
              <div className="grid grid-cols-2 gap-x-4 gap-y-1 font-mono">
                {Object.entries(dq.reconcile).map(([k, v]) => (
                  <KV key={k} k={k} v={typeof v === 'boolean' ? String(v) : fmtNum(v as number, 6)} />
                ))}
              </div>
            </div>
          )}

          {dq.ledger_counts && (
            <div className="font-mono text-[10px] text-neutral-400">
              账本行数：{Object.entries(dq.ledger_counts).map(([k, v]) => `${k}=${v}`).join(' · ')}
            </div>
          )}
        </>
      ) : (
        <div className="text-neutral-400">后台未返回 data_quality（不编造缺口数字）</div>
      )}

      <div className="border-t border-neutral-100 pt-2 grid grid-cols-1 md:grid-cols-2 gap-3">
        <div>
          <div className="font-bold text-neutral-200 mb-1">代码 / 输入 hash</div>
          {hashes ? (
            <div className="space-y-0.5 font-mono">
              {Object.entries(hashes).map(([k, v]) => (
                <KV key={k} k={k} v={v} />
              ))}
            </div>
          ) : (
            <div className="text-neutral-400">无 hash</div>
          )}
        </div>
        <div>
          <div className="font-bold text-neutral-200 mb-1">输入快照（snapshot_files）</div>
          {q.window ? (
            <div className="space-y-0.5 font-mono text-[10px]">
              <KV k="窗口" v={`${fmtUtc(q.window.win_start_ms)} → ${fmtUtc(q.window.win_end_ms)} UTC（${q.window.window_days} 天）`} />
              <KV k="预热" v={`${q.window.warmup_bars ?? '—'} 根信号 bar`} />
              <KV k="约定" v={q.window.convention} mono={false} />
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
};

/* ══════════════════════════ 主组件 ══════════════════════════ */

export const BacktestWorkbench: React.FC = () => {
  const [form, setForm] = useState<LabForm>(EMPTY_FORM);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [selectedTradeId, setSelectedTradeId] = useState<string | null>(null);
  const [showSignalParams, setShowSignalParams] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createNotice, setCreateNotice] = useState<string | null>(null);
  const [createErrors, setCreateErrors] = useState<string[]>([]);
  const [defaultsNote, setDefaultsNote] = useState<string | null>(null);
  /** **本页提交**的 run（用于「运行按钮在该 run 进入终态前保持禁用」）。 */
  const [submittedRunId, setSubmittedRunId] = useState<string | null>(null);

  /* ── 列表 / 注册表 ── */
  const listKey = 'list';
  const list = useLabFetch<Awaited<ReturnType<typeof labListRuns>>>(listKey, () => labListRuns(50));
  const runs: LabRunSummary[] = useMemo(() => list.data?.items ?? [], [list.data]);
  const registry: LabRegistryEntry | null = list.data?.registry_entry ?? null;

  /* ── 默认值（来自后台真实 run）── */
  const defaults = useLabFetch<LabDefaults>('defaults', () => labLoadDefaults());
  const didInitForm = useRef(false);
  useEffect(() => {
    if (didInitForm.current || !defaults.data) return;
    const d = defaults.data;
    const next: LabForm = { ...EMPTY_FORM, params: {} };
    if (d.symbol) next.symbol = d.symbol;
    if (d.exchange) next.exchange = d.exchange;
    if (d.market) next.market = d.market;
    if (d.signal_timeframe) next.signalTf = d.signal_timeframe;
    if (d.exec_timeframe) next.execTf = d.exec_timeframe;
    next.windowStart = toUtcInput(d.window_start_ms);
    next.windowEnd = toUtcInput(d.window_end_ms);
    const params: Record<string, string> = {};
    for (const spec of LAB_PARAM_SPECS) {
      const src = spec.scope === 'signal' ? d.signal_params : d.exec_params;
      const v = src?.[spec.key];
      if (v === undefined || v === null || !Number.isFinite(v)) continue;
      params[spec.key] = String(labFromWire(spec, v));
    }
    next.params = params;
    setForm(next);
    didInitForm.current = true;
    if (!d.signal_params || !d.exec_params) {
      setDefaultsNote(
        '未能从后台取到冻结参数默认值（最近的成功运行均含参数覆盖）⇒ 参数输入框留空表示「沿用后台冻结值」；如需覆盖请显式填写。',
      );
    } else {
      setDefaultsNote(`参数默认值取自后台 run ${d.params_from_run}（该 run 无参数覆盖 ⇒ 即候选包冻结参数）。`);
    }
  }, [defaults.data]);

  /* ── 标的池（真实工作台活动池）── */
  const pool = useLabFetch<Awaited<ReturnType<typeof labGetPool>>>('pool', () => labGetPool());
  const poolSymbols: LabPoolItem[] = useMemo(
    () => (pool.data?.items ?? []).filter((i) => i.exchange === 'binance'),
    [pool.data],
  );

  /* ── 运行 hash 补全（列表响应不含 hash）── */
  const [hashMap, setHashMap] = useState<Record<string, LabRunHashes>>({});
  const hashedRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    const todo = runs.map((r) => r.run_id).filter((id) => !hashedRef.current.has(id)).slice(0, 30);
    if (todo.length === 0) return;
    todo.forEach((id) => hashedRef.current.add(id));
    let alive = true;
    void labFetchRunHashMap(todo, 4).then((m) => {
      if (alive) setHashMap((prev) => ({ ...prev, ...m }));
    });
    return () => {
      alive = false;
    };
  }, [runs]);

  /* ── 轮询：只要还有 queued/running 的 run，就**持续**2s 拉列表直到全部进入终态 ── */
  const activeRun = useMemo(
    () => runs.find((r) => r.status === 'queued' || r.status === 'running') ?? null,
    [runs],
  );
  useEffect(() => {
    if (!activeRun) return;
    // 以 run_id 为依赖用 interval（而非一次性 timeout）：状态字符串在 queued→running 变化时
    // 若只调度一次就会漏掉后续轮询。interval 持续到该 run 离开非终态、activeRun 变 null 为止。
    const timer = window.setInterval(() => void list.reload(), 2000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeRun?.run_id]);

  /* 自动选中最新 run（仅首次） */
  const didPick = useRef(false);
  useEffect(() => {
    if (didPick.current) return;
    if (runs.length === 0) return;
    didPick.current = true;
    setSelectedRunId((prev) => prev ?? runs[0].run_id);
  }, [runs]);

  /* ── 所选 run 的详情 / 指标 / 曲线 / 逐笔 / 图表 / 质量（响应身份一律核对 run_id）── */
  const detail = useLabFetch<LabRunDetail>(
    selectedRunId ? `detail:${selectedRunId}` : null,
    () => labGetRun(selectedRunId as string).then((d) => expectRunId(d, selectedRunId as string)),
  );
  const detailStatus = detail.data?.status ?? null;
  const isPending = detailStatus === 'queued' || detailStatus === 'running';

  // 运行未完成时**持续**轮询详情（interval 依赖 isPending，queued→running 期间不会漏拍）
  useEffect(() => {
    if (!selectedRunId || !isPending) return;
    const timer = window.setInterval(() => void detail.reload(), 2000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedRunId, isPending]);

  const done = detailStatus === 'succeeded';
  const subKey = selectedRunId && done ? `${selectedRunId}:done` : null;

  const metrics = useLabFetch<LabMetricsResponse>(subKey, () =>
    labGetMetrics(selectedRunId as string).then((d) => expectRunId(d, selectedRunId as string)),
  );
  const equity = useLabFetch<LabEquityResponse>(subKey, () =>
    labGetEquity(selectedRunId as string).then((d) => expectRunId(d, selectedRunId as string)),
  );
  const trades = useLabFetch<LabTradesResponse>(subKey, () =>
    labGetTrades(selectedRunId as string).then((d) => expectRunId(d, selectedRunId as string)),
  );
  const chart = useLabFetch<LabChartResponse>(subKey, () =>
    labGetChart(selectedRunId as string).then((d) => expectRunId(d, selectedRunId as string)),
  );
  const quality = useLabFetch<LabQualityResponse>(
    selectedRunId ? `quality:${selectedRunId}:${detailStatus ?? '?'}` : null,
    () => labGetQuality(selectedRunId as string).then((d) => expectRunId(d, selectedRunId as string)),
  );

  // 切换 run ⇒ 清空选中交易
  useEffect(() => {
    setSelectedTradeId(null);
  }, [selectedRunId]);

  const tradeDetail = useLabFetch<LabTradeDetail>(
    selectedRunId && selectedTradeId ? `trade:${selectedRunId}:${selectedTradeId}` : null,
    () =>
      labGetTrade(selectedRunId as string, selectedTradeId as string).then((d) =>
        expectRunId(d, selectedRunId as string),
      ),
  );

  /* ── 表单校验与提交 ── */
  const windowStartMs = parseUtcInput(form.windowStart);
  const windowEndMs = parseUtcInput(form.windowEnd);

  const windowIssues = useMemo(() => {
    const errs: string[] = [];
    if (!form.windowStart.trim()) errs.push('窗口起点（UTC）未填写');
    else if (windowStartMs === null) errs.push('窗口起点无法解析为 UTC 时间（示例 2026-10-09T00:00）');
    if (!form.windowEnd.trim()) errs.push('窗口终点（UTC）未填写');
    else if (windowEndMs === null) errs.push('窗口终点无法解析为 UTC 时间（示例 2026-10-16T00:00）');
    if (windowStartMs !== null && windowEndMs !== null) {
      if (windowEndMs <= windowStartMs) errs.push('窗口终点必须晚于起点（后台为半开区间 [start, end)）');
      if (windowStartMs % LAB_WINDOW_GRID_MS !== 0) errs.push('窗口起点须对齐 15m 网格（分钟为 00/15/30/45）');
      if (windowEndMs % LAB_WINDOW_GRID_MS !== 0) errs.push('窗口终点须对齐 15m 网格（分钟为 00/15/30/45）');
      if (windowEndMs - windowStartMs < LAB_MIN_WINDOW_MS) errs.push('窗口跨度不足 1 天');
    }
    if (form.warmup.trim() !== '') {
      const wb = Number(form.warmup);
      if (!Number.isInteger(wb) || wb < 20) errs.push('预热根数须为 >=20 的整数（留空 = 后台自动）');
    }
    if (!form.symbol.trim()) errs.push('交易对未填写');
    return errs;
  }, [form.windowStart, form.windowEnd, form.warmup, form.symbol, windowStartMs, windowEndMs]);

  /** 解析表单参数（显示值 → 数值；空 = 沿用后台默认）。 */
  const parsedParams = useMemo(() => {
    const shown: Record<string, number> = {};
    const errs: string[] = [];
    for (const spec of LAB_PARAM_SPECS) {
      const raw = (form.params[spec.key] ?? '').trim();
      if (raw === '') continue;
      const n = Number(raw);
      if (!Number.isFinite(n)) {
        errs.push(`${spec.label}（${spec.key}）不是合法数值：${raw}`);
        continue;
      }
      shown[spec.key] = n;
    }
    errs.push(...labValidateParams(shown));
    return { shown, errs };
  }, [form.params]);

  /** 只发送**相对冻结默认有变化**的参数（真实覆盖）；全部参数的实际生效值由后台快照。 */
  const wireParams = useMemo(() => {
    const out: Record<string, number> = {};
    const overridden: string[] = [];
    for (const spec of LAB_PARAM_SPECS) {
      const n = parsedParams.shown[spec.key];
      if (n === undefined) continue;
      const wire = labToWire(spec, n);
      const def = (spec.scope === 'signal' ? defaults.data?.signal_params : defaults.data?.exec_params)?.[spec.key];
      if (def !== undefined && Math.abs(def - wire) < 1e-12) continue;
      out[spec.key] = wire;
      overridden.push(spec.key);
    }
    return { out, overridden };
  }, [parsedParams, defaults.data]);

  const blockingErrors = useMemo(
    () => [...windowIssues, ...parsedParams.errs],
    [windowIssues, parsedParams.errs],
  );

  const symbolInPool = useMemo(
    () => poolSymbols.some((i) => i.symbol === form.symbol.trim().toUpperCase()),
    [poolSymbols, form.symbol],
  );

  const setParam = useCallback((key: string, v: string) => {
    setForm((f) => ({ ...f, params: { ...f.params, [key]: v } }));
  }, []);

  /**
   * 本页提交的 run 的当前状态。
   * 列表还没刷新到该 run 时按 `queued` 计 —— 否则提交瞬间按钮会短暂解禁，可能重复启动。
   */
  const submittedStatus = useMemo(() => {
    if (!submittedRunId) return null;
    return runs.find((r) => r.run_id === submittedRunId)?.status ?? 'queued';
  }, [submittedRunId, runs]);
  const submittedPending = submittedStatus === 'queued' || submittedStatus === 'running';
  const runLocked = creating || submittedPending;

  const onSubmit = useCallback(async () => {
    if (runLocked) return; // 禁重复点击：本页 run 进入终态前不再受理
    setCreateErrors([]);
    setCreateNotice(null);
    if (blockingErrors.length > 0) {
      setCreateErrors(blockingErrors);
      return;
    }
    if (windowStartMs === null || windowEndMs === null) return;
    setCreating(true);
    try {
      const res = await labCreateRun({
        strategy_id: LAB_STRATEGY_ID,
        exchange: form.exchange,
        market: form.market,
        symbols: [form.symbol.trim().toUpperCase()],
        signal_timeframe: form.signalTf,
        exec_timeframe: form.execTf,
        window_start: toIsoUtcZ(windowStartMs),
        window_end: toIsoUtcZ(windowEndMs),
        params: wireParams.out,
        note: 'GM 单策略工作台',
      });
      const rid = res.run_id;
      setCreateNotice(
        `已受理：run_id = ${rid} · status = ${res.status}（后台线程执行中；GET 只读不触发计算）。` +
          `请求覆盖 ${wireParams.overridden.length} 项参数：${wireParams.overridden.length ? wireParams.overridden.join(', ') : '无（全部沿用冻结默认）'}。`,
      );
      if (rid) {
        setSubmittedRunId(rid);
        setSelectedRunId(rid);
      }
      didPick.current = true;
      void list.reload();
      void defaults.reload();
    } catch (e: unknown) {
      setCreateErrors(labErrorsOf(e));
    } finally {
      setCreating(false);
    }
  }, [runLocked, blockingErrors, windowStartMs, windowEndMs, form, wireParams, list, defaults]);

  /* ── 指标卡 ── */
  const m = metrics.data?.metrics ?? null;
  const wins = m?.wins ?? null;
  const metricCards = useMemo(() => {
    const noWin = wins === 0;
    return [
      {
        title: '净收益率',
        value: fmtPct(m?.net_return_pct, 4),
        sub: `净盈亏 ${fmtSignedUsd(m?.net_pnl, 2)} / 初始 ${fmtUsd(m?.initial_equity, 2)}`,
        color: m?.net_return_pct === null || m?.net_return_pct === undefined ? 'text-neutral-400' : m.net_return_pct >= 0 ? 'text-emerald-600' : 'text-rose-600',
      },
      {
        title: '最大回撤',
        value: m?.max_drawdown_pct === null || m?.max_drawdown_pct === undefined ? '—' : `-${m.max_drawdown_pct.toFixed(4)}%`,
        sub: '峰值动态水线（后台 /metrics）',
        color: m?.max_drawdown_pct === null || m?.max_drawdown_pct === undefined ? 'text-neutral-400' : 'text-rose-600',
      },
      {
        title: '胜率',
        value: m?.win_rate_pct === null || m?.win_rate_pct === undefined ? '—' : `${m.win_rate_pct.toFixed(2)}%`,
        sub: m ? `盈 ${m.wins ?? '—'} / 亏 ${m.losses ?? '—'} / 平 ${m.flats ?? '—'}` : '后台未返回',
        color: 'text-neutral-100',
      },
      {
        title: '平均盈亏比',
        value: m?.avg_win_loss_ratio === null || m?.avg_win_loss_ratio === undefined ? '—' : `${m.avg_win_loss_ratio}`,
        sub:
          m?.avg_win_loss_ratio === null || m?.avg_win_loss_ratio === undefined
            ? noWin
              ? '无盈利单 ⇒ 未定义（不填假盈利比）'
              : '后台未给出'
            : '平均盈利 / 平均亏损（均值口径）',
        color: 'text-neutral-100',
      },
      {
        title: 'Profit Factor',
        value: m?.profit_factor === null || m?.profit_factor === undefined ? '—' : `${m.profit_factor}`,
        sub: noWin ? '0：无盈利单；Σ盈 / |Σ亏|（聚合口径）' : 'Σ盈 / |Σ亏|（聚合口径）',
        color: 'text-neutral-100',
      },
      {
        title: '交易次数',
        value: m?.trades_count === null || m?.trades_count === undefined ? '—' : String(m.trades_count),
        sub: m?.open_positions_remaining && m.open_positions_remaining.length > 0
          ? `期末仍有未平仓 ${m.open_positions_remaining.length} 笔`
          : '已平仓笔数',
        color: 'text-neutral-100',
      },
      {
        title: '手续费合计',
        value: fmtUsd(m?.total_fees, 2),
        sub: `入场 ${fmtNum(m?.total_entry_fee, 2)} + 出场 ${fmtNum(m?.total_exit_fee, 2)}`,
        color: 'text-neutral-300',
      },
      {
        title: '资金费合计',
        value: fmtUsd(m?.total_funding_fee, 2),
        sub: `${m?.funding_events_total ?? '—'} 笔真实结算事件（settled）`,
        color: 'text-neutral-300',
      },
    ];
  }, [m, wins]);

  /** 「质量通过」的判定（与质量报告面板同一口径，缺一不可）。 */
  const qualityStatusOfRun = quality.data?.quality?.status ?? null;
  const qualityOkOfRun =
    !quality.error &&
    detailStatus === 'succeeded' &&
    qualityStatusOfRun === 'ok' &&
    quality.data?.data_quality != null;

  const tradeRows: LabTradeRow[] = useMemo(() => trades.data?.items ?? [], [trades.data]);
  const equityPoints: LabEquityPoint[] = useMemo(() => equity.data?.items ?? [], [equity.data]);
  const chartPayload: LabChart | null = chart.data?.chart ?? null;

  /** 该月平仓笔数（按**真实**平仓时间；仅作旧热力图的「补充事实」列，非收益口径）。 */
  const monthCloseCounts = useMemo(() => {
    const out: Record<string, number> = {};
    for (const t of tradeRows) {
      if (!Number.isFinite(t.exit_time_ms)) continue;
      const d = new Date(t.exit_time_ms);
      const key = `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}`;
      out[key] = (out[key] ?? 0) + 1;
    }
    return out;
  }, [tradeRows]);

  const formVersions = registry?.signal_versions ?? null;

  return (
    <div
      className="flex flex-col min-h-full gap-2.5 p-3 bg-neutral-100 text-neutral-800"
      data-testid="lab-workbench"
    >
      {/* ══ ① 配置与运行 ══ */}
      <SectionCard
        title="① 运行配置（唯一策略 · 唯一标的 · UTC 半开区间）"
        testid="lab-config"
        right={
          <div className="flex flex-wrap items-center gap-2">
            <Chip tone="info">{LAB_STRATEGY_ID}</Chip>
            {formVersions && (
              <Chip>
                信号 {formVersions.signal ?? '—'} · 核心 {formVersions.core ?? '—'} · 执行 {formVersions.exec_core ?? '—'}
              </Chip>
            )}
            <button
              type="button"
              onClick={() => void list.reload()}
              className="flex items-center gap-1 px-2 py-1 rounded border border-neutral-200 bg-white text-[11px] text-neutral-300 hover:bg-neutral-50"
            >
              <RefreshCw className={`w-3 h-3 ${list.loading ? 'animate-spin' : ''}`} /> 刷新实验列表
            </button>
          </div>
        }
      >
        {/* ── 第 1 行：交易对 / 交易所 / 市场 / 周期 ── */}
        <div className="flex flex-wrap items-end gap-x-3 gap-y-2">
          <label
            className="block space-y-0.5 w-32"
            title="单标的；建议取自工作台活动池，也可手工输入（后台按真实数据独立校验）"
          >
            <span className="text-[10px] text-neutral-500 flex items-center gap-1 whitespace-nowrap">
              交易对
              <span className={`font-mono ${symbolInPool ? 'text-emerald-600' : 'text-amber-700'}`}>
                {pool.loading
                  ? '池读取中'
                  : poolSymbols.length > 0
                    ? symbolInPool
                      ? '在池内'
                      : '不在池内'
                    : '池未取到'}
              </span>
            </span>
            <input
              list="lab-symbol-options"
              value={form.symbol}
              onChange={(e) => setForm((f) => ({ ...f, symbol: e.target.value.toUpperCase() }))}
              className="w-full px-1.5 py-1 text-xs rounded border border-neutral-200 bg-white text-neutral-200 focus:outline-none focus:border-emerald-500 font-mono"
              data-testid="lab-symbol-input"
            />
            <datalist id="lab-symbol-options">
              {poolSymbols.map((i) => (
                <option key={`${i.exchange}:${i.market_id}`} value={i.symbol} />
              ))}
            </datalist>
          </label>

          <OptionRow
            label="交易所"
            value={form.exchange}
            options={LAB_EXCHANGE_OPTIONS}
            onChange={(v) => setForm((f) => ({ ...f, exchange: v }))}
          />
          <OptionRow
            label="市场"
            value={form.market}
            options={LAB_MARKET_OPTIONS}
            onChange={(v) => setForm((f) => ({ ...f, market: v }))}
          />
          <OptionRow
            label="信号周期"
            value={form.signalTf}
            options={LAB_SIGNAL_TF_OPTIONS}
            onChange={(v) => setForm((f) => ({ ...f, signalTf: v }))}
          />
          <OptionRow
            label="执行周期"
            value={form.execTf}
            options={LAB_EXEC_TF_OPTIONS}
            onChange={(v) => setForm((f) => ({ ...f, execTf: v }))}
          />
        </div>

        {/* ── 第 2 行：UTC 起止 / 预热 / 运行 ── */}
        <div className="flex flex-wrap items-end gap-x-3 gap-y-2 mt-2.5">
          <label className="block space-y-0.5 w-40" title="半开区间左端（含）；须对齐 15m 网格（分钟 00/15/30/45）">
            <span className="text-[10px] text-neutral-500">窗口起点 UTC</span>
            <input
              value={form.windowStart}
              onChange={(e) => setForm((f) => ({ ...f, windowStart: e.target.value }))}
              placeholder="2026-10-09T00:00"
              className="w-full px-1.5 py-1 text-xs rounded border border-neutral-200 bg-white text-neutral-200 focus:outline-none focus:border-emerald-500 font-mono"
              data-testid="lab-window-start"
            />
            <span className="text-[10px] text-neutral-400 block truncate">{fmtUtc(windowStartMs)} UTC</span>
          </label>
          <label className="block space-y-0.5 w-40" title="半开区间右端（不含）；须对齐 15m 网格；跨度至少 1 天">
            <span className="text-[10px] text-neutral-500">窗口终点 UTC</span>
            <input
              value={form.windowEnd}
              onChange={(e) => setForm((f) => ({ ...f, windowEnd: e.target.value }))}
              placeholder="2026-10-16T00:00"
              className="w-full px-1.5 py-1 text-xs rounded border border-neutral-200 bg-white text-neutral-200 focus:outline-none focus:border-emerald-500 font-mono"
              data-testid="lab-window-end"
            />
            <span className="text-[10px] text-neutral-400 block truncate">{fmtUtc(windowEndMs)} UTC</span>
          </label>
          <label className="block space-y-0.5 w-24" title="信号 bar 根数；留空 = 后台自动。预热段只喂数据不交易，后台会拒绝静默降级">
            <span className="text-[10px] text-neutral-500">预热根数</span>
            <input
              value={form.warmup}
              onChange={(e) => setForm((f) => ({ ...f, warmup: e.target.value }))}
              placeholder="自动"
              className="w-full px-1.5 py-1 text-xs rounded border border-neutral-200 bg-white text-neutral-200 focus:outline-none focus:border-emerald-500 font-mono"
            />
          </label>
          <span className="text-[10px] text-neutral-400 pb-1.5 whitespace-nowrap" title="执行周期明确为 15m bar（相位 A→E，见执行视图口径）">
            执行按 15m bar 级
          </span>
          <div className="flex flex-col gap-1">
            <button
              type="button"
              onClick={() => void onSubmit()}
              disabled={runLocked || blockingErrors.length > 0}
              className={`flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-bold border transition-colors ${
                runLocked || blockingErrors.length > 0
                  ? 'bg-neutral-50 text-neutral-400 border-neutral-200 cursor-not-allowed'
                  : 'bg-emerald-600 text-white border-emerald-600 hover:bg-emerald-700'
              }`}
              data-testid="lab-run-button"
            >
              <Play className="w-3.5 h-3.5" />
              {creating ? '提交中…' : submittedPending ? `运行中（${submittedStatus}）…` : '运行回测'}
            </button>
            {submittedRunId ? (
              <div
                className={`text-[10px] rounded border px-1.5 py-1 max-w-[22rem] ${
                  submittedPending
                    ? 'bg-sky-50 border-sky-200 text-sky-800'
                    : 'bg-neutral-50 border-neutral-200 text-neutral-400'
                }`}
                data-testid="lab-run-progress"
              >
                本页提交：<span className="font-mono">{submittedRunId}</span> · 状态 <b>{submittedStatus ?? '—'}</b>
                {submittedPending ? '（轮询中；进入终态前按钮保持禁用）' : '（已进入终态，可再次运行）'}
              </div>
            ) : (
              <span className="text-[10px] text-neutral-400">结果永久保存为实验记录</span>
            )}
          </div>
        </div>

        {/* ── 第 3 行：成本（一行，单位与等价换算已标注）── */}
        <div className="mt-2.5 pt-2.5 border-t border-neutral-100 flex flex-wrap items-end gap-x-3 gap-y-2">
          <span className="text-[10px] font-bold text-neutral-300 pb-1 shrink-0" title="单位已标注；参数留空 = 沿用后台冻结默认值">
            成本
          </span>
          {LAB_PARAM_SPECS.filter((s) => s.group === 'exec_cost').map((spec) => (
            <div key={spec.key} className="w-48">
              <ParamField spec={spec} value={form.params[spec.key] ?? ''} onChange={(v) => setParam(spec.key, v)} />
            </div>
          ))}
        </div>

        {/* ── 风险与策略参数（默认折叠；标题行保留风险边界摘要）── */}
        <div className="mt-2.5 pt-2.5 border-t border-neutral-100">
          <button
            type="button"
            onClick={() => setShowSignalParams((s) => !s)}
            className="w-full text-left text-[11px] font-bold text-neutral-200 hover:text-emerald-700"
            data-testid="lab-params-toggle"
          >
            {showSignalParams ? '▾' : '▸'} 风险与策略参数（白名单校验）
            <span className="ml-2 font-normal text-neutral-500">
              单笔风险 {form.params['risk_per_trade'] || '—'}% · 组合上限 {form.params['max_portfolio_risk'] || '—'}% ·
              杠杆上限 {form.params['max_leverage_per_symbol'] || '—'}× · 本次覆盖 {wireParams.overridden.length} 项
            </span>
          </button>
          {showSignalParams && (
            <div className="mt-2 space-y-3">
              <div>
                <div className="text-[11px] text-neutral-400 mb-1.5">风险 / 仓位（exec）</div>
                <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
                  {LAB_PARAM_SPECS.filter((s) => s.group === 'exec_risk').map((spec) => (
                    <ParamField key={spec.key} spec={spec} value={form.params[spec.key] ?? ''} onChange={(v) => setParam(spec.key, v)} />
                  ))}
                </div>
              </div>
              {(['sig_structure', 'sig_oi'] as const).map((g) => (
                <div key={g}>
                  <div className="text-[11px] text-neutral-400 mb-1.5">
                    {g === 'sig_structure' ? '结构参数（唐奇安 / ATR / 回踩 / SC）' : 'OI 因子参数'}
                  </div>
                  <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
                    {LAB_PARAM_SPECS.filter((s) => s.group === g).map((spec) => (
                      <ParamField key={spec.key} spec={spec} value={form.params[spec.key] ?? ''} onChange={(v) => setParam(spec.key, v)} />
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* ── 运行配置与审计详情（默认折叠：不挡指标与曲线）── */}
        <details className="mt-2.5 border border-neutral-200 rounded bg-neutral-50" data-testid="lab-audit-details">
          <summary className="cursor-pointer select-none px-2 py-1 text-[11px] font-bold text-neutral-300">
            运行配置与审计详情（API 路径 / 请求体 / 后台线程 / 参数代码名 / 原始 JSON）
          </summary>
          <div className="px-2 pb-2 pt-1 space-y-1 text-[10px] font-mono text-neutral-400 border-t border-neutral-200">
            <div>
              列表 / 详情：<span className="text-neutral-200">GET /api/v1/backtests</span>、
              <span className="text-neutral-200">GET /api/v1/backtests/{'{id}'}</span>（只读，不触发计算）
            </div>
            <div>
              创建：<span className="text-neutral-200">POST /api/v1/backtests</span> ⇒ 202 + run_id，由后台线程执行
            </div>
            <div>
              窗口端点须对齐 15m 网格（{LAB_WINDOW_GRID_MS} ms），最短 {LAB_MIN_WINDOW_MS} ms（1 天）
            </div>
            <div>epoch 毫秒：起点 {windowStartMs ?? '—'} · 终点 {windowEndMs ?? '—'}</div>
            <div className="break-all">
              本次将发送 payload：
              {JSON.stringify({
                strategy_id: LAB_STRATEGY_ID,
                exchange: form.exchange,
                market: form.market,
                symbols: [form.symbol.trim().toUpperCase()],
                signal_timeframe: form.signalTf,
                exec_timeframe: form.execTf,
                window_start: windowStartMs === null ? null : toIsoUtcZ(windowStartMs),
                window_end: windowEndMs === null ? null : toIsoUtcZ(windowEndMs),
                params: wireParams.out,
              })}
            </div>
            <div className="break-all">
              白名单参数代码名（{LAB_PARAM_SPECS.length} 项）：{LAB_PARAM_SPECS.map((s) => s.key).join(', ')}
            </div>
            {defaultsNote && (
              <div className="flex items-start gap-1 text-neutral-500">
                <Info className="w-3 h-3 mt-0.5 shrink-0" />
                <span>{defaultsNote}</span>
              </div>
            )}
          </div>
        </details>

        {blockingErrors.length > 0 && (
          <div className="mt-2 text-[11px] text-rose-700 bg-rose-50 border border-rose-200 rounded px-2 py-1.5" data-testid="lab-form-errors">
            <div className="font-bold">提交前校验未通过（后台仍会独立校验）：</div>
            <ul className="list-disc pl-4">
              {blockingErrors.map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ul>
          </div>
        )}

        {createErrors.length > 0 && (
          <div className="mt-2 text-[11px] text-rose-700 bg-rose-50 border border-rose-200 rounded px-2 py-1.5" data-testid="lab-create-errors">
            <div className="font-bold">后台拒绝创建（原样显示后台原因，失败保留）：</div>
            <ul className="list-disc pl-4">
              {createErrors.map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ul>
          </div>
        )}

        {createNotice && (
          <div className="mt-2 text-[11px] text-sky-800 bg-sky-50 border border-sky-200 rounded px-2 py-1.5" data-testid="lab-create-notice">
            {createNotice}
          </div>
        )}
      </SectionCard>

      {/* ══ ② 指标 ══ */}
      <SectionCard
        title="② 收益指标（所选 run 的真实结果）"
        testid="lab-metrics"
        right={
          <div className="flex flex-wrap items-center gap-2 text-[10px] font-mono">
            {selectedRunId ? (
              <>
                <Chip tone={detailStatus === 'succeeded' ? 'ok' : detailStatus === 'failed' ? 'bad' : 'warn'}>
                  run_id {selectedRunId}
                </Chip>
                <span className={`px-1.5 py-0.5 rounded border ${statusStyle(detailStatus)}`}>
                  执行：{detailStatus === 'succeeded' ? '已完成' : (detailStatus ?? '—')}
                </span>
                {/* 「执行完成」与「质量通过」分开标注：HTTP 200 的执行完成不代表质量通过 */}
                <span
                  className={`px-1.5 py-0.5 rounded border ${
                    qualityOkOfRun
                      ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
                      : 'bg-rose-50 text-rose-700 border-rose-200'
                  }`}
                  title="质量通过需同时满足：运行 succeeded + quality.status=ok + 有 data_quality 明细"
                >
                  质量：{qualityOkOfRun ? '通过' : (qualityStatusOfRun ?? (quality.loading ? '读取中' : '未通过'))}
                </span>
              </>
            ) : (
              <span className="text-neutral-400">未选择运行</span>
            )}
            {metrics.loading && <span className="text-neutral-400">读取中…</span>}
            {metrics.error && <span className="text-rose-600">指标取数错误：{metrics.error}</span>}
          </div>
        }
      >
        {!selectedRunId ? (
          <div className="text-[11px] text-neutral-500">先在下方「实验记录」选择一次运行，或在上方点击「运行回测」。</div>
        ) : detailStatus !== 'succeeded' ? (
          <div className="text-[11px] text-amber-800 bg-amber-50 border border-amber-200 rounded px-2 py-1.5">
            该运行状态为 <b>{detailStatus ?? '—'}</b>，结果尚未就绪（GET 只读、不触发计算）。
            {detail.data?.failure_reason && <div className="mt-0.5 text-rose-700">失败原因：{detail.data.failure_reason}</div>}
            {(detailStatus === 'queued' || detailStatus === 'running') && <div className="mt-0.5">正在轮询状态…</div>}
          </div>
        ) : metrics.loading && !metrics.data ? (
          <div className="text-[11px] text-neutral-500" data-testid="lab-metrics-loading">
            正在读取该 run 的真实指标…（未取到才显示 —；此处不预填任何 0 / 假值）
          </div>
        ) : metrics.error ? (
          <div className="text-[11px] text-rose-700 bg-rose-50 border border-rose-200 rounded px-2 py-1.5">
            指标取数失败（不得视为成绩）：{metrics.error}
          </div>
        ) : (
          <div className="space-y-2">
            <div className="grid grid-cols-2 sm:grid-cols-4 xl:grid-cols-8 gap-2">
              {metricCards.map((c) => (
                <div key={c.title} className="p-2.5 bg-white border border-neutral-200 rounded-lg space-y-1 shadow-sm">
                  <span className="text-[11px] font-medium text-neutral-500 block truncate" title={c.title}>{c.title}</span>
                  <div className={`text-lg font-bold font-mono tracking-tight ${c.color}`}>{c.value}</div>
                  <span className="text-[10px] text-neutral-400 block" title={c.sub}>{c.sub}</span>
                </div>
              ))}
            </div>
            {m?.note && <div className="text-[10px] text-neutral-500">口径：{m.note}</div>}
            {m?.sharpe_basis && (
              <div className="text-[10px] text-neutral-500 font-mono">
                夏普 {fmtNum(m.sharpe_annualized_sqrt365, 4)} · 基准：{m.sharpe_basis}
              </div>
            )}
          </div>
        )}
      </SectionCard>

      {/* ══ 结果身份：所选 run 的实际时间 / 配置 / 版本（不是表单未运行的值）══ */}
      {selectedRunId && detail.data && (
        <SectionCard
          title="结果身份 · 所选 run 的实际时间 / 配置 / 版本（不用表单未运行的新值冒充结果配置）"
          testid="lab-run-identity"
        >
          <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-x-4 gap-y-1 font-mono text-[11px]">
            <KV k="run_id" v={selectedRunId} />
            <KV k="status" v={detail.data.status ?? '—'} />
            <KV k="created_at" v={fmtIsoUtc(detail.data.created_at)} />
            <KV k="finished_at" v={fmtIsoUtc(detail.data.finished_at)} />
            <KV
              k="标的 / 所 / 市场"
              v={`${detail.data.window?.symbol ?? '—'} · ${detail.data.window?.exchange ?? '—'} / ${detail.data.window?.market ?? '—'}`}
            />
            <KV
              k="周期（信号 / 执行）"
              v={`${detail.data.window?.signal_timeframe ?? '—'} / ${detail.data.window?.exec_timeframe ?? '—'}`}
            />
            <KV k="窗口（UTC）" v={`${fmtUtc(detail.data.window?.win_start_ms)} → ${fmtUtc(detail.data.window?.win_end_ms)}`} />
            <KV
              k="窗口天数 / 预热"
              v={`${fmtNum(detail.data.window?.window_days, 2)} 天 / ${detail.data.window?.warmup_bars ?? '—'} 根`}
            />
            <KV k="初始资金（生效）" v={fmtUsd((detail.data.config?.actual_config?.['initial_equity'] as number) ?? null, 2)} />
            <KV k="手续费率（生效）" v={String((detail.data.config?.actual_config?.['fee_rate'] as number) ?? '—')} />
            <KV k="滑点 bps（生效）" v={String((detail.data.config?.actual_config?.['slippage_bps'] as number) ?? '—')} />
            <KV
              k="engine 版本"
              v={`${(detail.data.engine?.['signal_version'] as string) ?? '—'} / ${(detail.data.engine?.['core_version'] as string) ?? '—'} / ${(detail.data.engine?.['exec_core_version'] as string) ?? '—'}`}
            />
          </div>

          {/* 审计明细：长 hash / 账本行数 / 原始参数 JSON / 输入快照 —— 默认折叠，不挡指标与曲线 */}
          <details className="mt-2 border border-neutral-200 rounded bg-neutral-50" data-testid="lab-run-audit">
            <summary className="cursor-pointer select-none px-2 py-1 text-[11px] font-bold text-neutral-300">
              代码 / 输入 hash · 账本行数 · 生效参数原始 JSON · 输入快照
            </summary>
            <div className="px-2 pb-2 pt-1 space-y-1 border-t border-neutral-200">
              <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-x-4 gap-y-1 font-mono text-[10px]">
                <KV k="signal_params_hash" v={detail.data.hashes?.['signal_params_hash'] ?? '—'} />
                <KV k="exec_params_hash" v={detail.data.hashes?.['exec_params_hash'] ?? '—'} />
                <KV k="spec_sha256" v={detail.data.hashes?.['spec_sha256'] ?? '—'} />
                <KV k="code_sha256" v={detail.data.hashes?.['code_sha256'] ?? '—'} />
                <KV k="input_sha256" v={detail.data.hashes?.['input_sha256'] ?? '—'} />
                <KV k="signal_frame_sha256" v={detail.data.hashes?.['signal_frame_sha256'] ?? '—'} />
                <KV k="experiment_id" v={(detail.data.engine?.['experiment_id'] as string) ?? '—'} />
                <KV
                  k="账本行数"
                  v={detail.data.ledger_counts ? Object.entries(detail.data.ledger_counts).map(([k, v]) => `${k}=${v}`).join(' ') : '—'}
                />
              </div>
              {detail.data.config?.config?.signal_params && (
                <div className="text-[10px] text-neutral-400 font-mono break-all">
                  生效 signal_params：{JSON.stringify(detail.data.config.config.signal_params)}
                </div>
              )}
              {detail.data.config?.config?.exec_params && (
                <div className="text-[10px] text-neutral-400 font-mono break-all">
                  生效 exec_params：{JSON.stringify(detail.data.config.config.exec_params)}
                </div>
              )}
              {detail.data.snapshot_files && detail.data.snapshot_files.length > 0 && (
                <div className="text-[10px] text-neutral-400 font-mono break-all">
                  输入快照：{detail.data.snapshot_files.join(' · ')}
                </div>
              )}
            </div>
          </details>
        </SectionCard>
      )}

      {/* ══ ③ 资金分析 ══ */}
      {selectedRunId && detailStatus === 'succeeded' && (
        <LabFundingPanel
          points={equityPoints}
          benchmarkSeries={equity.data?.benchmark_equity ?? null}
          benchmarkMeta={equity.data?.benchmark ?? metrics.data?.benchmark_buy_hold ?? null}
          metrics={m}
          monthly={metrics.data?.monthly_returns ?? null}
          loading={equity.loading}
          error={equity.error}
          monthCloseCounts={monthCloseCounts}
          windowStartMs={detail.data?.window?.win_start_ms ?? null}
          windowEndMs={detail.data?.window?.win_end_ms ?? null}
        />
      )}

      {/* ══ ④ 执行视图 ══ */}
      {selectedRunId && detailStatus === 'succeeded' && (
        <LabChartPanel
          chart={chartPayload}
          loading={chart.loading}
          error={chart.error}
          missingReason={chart.data?.missing_reason ?? null}
          selectedTradeId={selectedTradeId}
          onSelectTrade={setSelectedTradeId}
        />
      )}

      {/* ══ ⑤ 明细表 + 单笔详情 ══ */}
      {selectedRunId && detailStatus === 'succeeded' && (
        <SectionCard
          title="⑤ 逐笔明细（点击任意行 ⇒ 图表定位该 trade_id + 显示单笔详情）"
          testid="lab-trades"
          right={
            <div className="flex items-center gap-2 text-[10px] font-mono">
              <Chip>共 {tradeRows.length} 笔</Chip>
              {trades.error && <span className="text-rose-600">逐笔取数错误：{trades.error}</span>}
            </div>
          }
        >
          <div className="overflow-auto max-h-72 border border-neutral-100 rounded">
            <table className="w-full text-left text-[11px] font-mono">
              <thead className="bg-neutral-50 text-neutral-400 border-b border-neutral-200 sticky top-0">
                <tr>
                  <th className="py-1.5 px-2">进场时间</th>
                  <th className="py-1.5 px-2">出场时间</th>
                  <th className="py-1.5 px-2">标的</th>
                  <th className="py-1.5 px-2">方向</th>
                  <th className="py-1.5 px-2 text-right">开仓价</th>
                  <th className="py-1.5 px-2 text-right">出场价</th>
                  <th className="py-1.5 px-2 text-right">数量</th>
                  <th className="py-1.5 px-2 text-right">毛盈亏</th>
                  <th className="py-1.5 px-2 text-right">双边手续费</th>
                  <th className="py-1.5 px-2 text-right">资金费</th>
                  <th className="py-1.5 px-2 text-right">净盈亏</th>
                  <th className="py-1.5 px-2">退出原因</th>
                  <th className="py-1.5 px-2">trade_id</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-neutral-100">
                {tradeRows.length === 0 ? (
                  <tr>
                    <td colSpan={13} className="py-6 text-center text-neutral-400 font-sans">
                      {trades.loading ? '读取中…' : '无真实成交记录（后台账本为空）——不显示任何演示成绩'}
                    </td>
                  </tr>
                ) : (
                  tradeRows.map((t) => {
                    const sel = t.trade_id === selectedTradeId;
                    return (
                      <tr
                        key={t.trade_id}
                        data-trade-id={t.trade_id}
                        onClick={() => setSelectedTradeId(t.trade_id)}
                        className={`cursor-pointer transition-colors ${sel ? 'bg-emerald-50' : 'hover:bg-neutral-50'}`}
                      >
                        <td className="py-1.5 px-2 text-neutral-500 whitespace-nowrap">{fmtUtc(t.entry_time_ms)}</td>
                        <td className="py-1.5 px-2 text-neutral-500 whitespace-nowrap">{fmtUtc(t.exit_time_ms)}</td>
                        <td className="py-1.5 px-2 font-bold text-neutral-100">{t.symbol}</td>
                        <td className="py-1.5 px-2">
                          <span className={`px-1.5 py-0.5 rounded border text-[10px] font-bold ${t.direction === 'long' ? 'bg-emerald-50 text-emerald-700 border-emerald-200' : 'bg-rose-50 text-rose-700 border-rose-200'}`}>
                            {t.direction === 'long' ? '做多' : t.direction === 'short' ? '做空' : t.direction}
                          </span>
                        </td>
                        <td className="py-1.5 px-2 text-right text-neutral-300">{fmtNum(t.entry_price, 4)}</td>
                        <td className="py-1.5 px-2 text-right text-neutral-100 font-semibold">{fmtNum(t.exit_price, 4)}</td>
                        <td className="py-1.5 px-2 text-right">{fmtNum(t.qty, 6)}</td>
                        <td className="py-1.5 px-2 text-right">{fmtSignedUsd(t.gross_pnl, 2)}</td>
                        <td className="py-1.5 px-2 text-right text-neutral-400">{fmtUsd(t.fees, 2)}</td>
                        <td className="py-1.5 px-2 text-right text-neutral-400">{fmtUsd(t.funding_fee, 2)}</td>
                        <td className={`py-1.5 px-2 text-right font-bold ${t.net_pnl >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
                          {fmtSignedUsd(t.net_pnl, 2)}
                        </td>
                        <td className="py-1.5 px-2 text-neutral-400">{t.exit_reason}</td>
                        <td className="py-1.5 px-2 text-neutral-400">{t.trade_id}</td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>

          <div className="mt-3 border-t border-neutral-100 pt-3">
            <LabTradeDetailPanel
              detail={tradeDetail.data}
              loading={tradeDetail.loading}
              error={tradeDetail.error}
            />
          </div>
        </SectionCard>
      )}

      {/* ══ ⑥ 实验记录 + 质量报告 ══ */}
      <SectionCard
        title="⑥ 实验记录（永久保存 · 可选择历史 run）与质量报告"
        testid="lab-runs"
        right={
          <div className="flex items-center gap-2 text-[10px] font-mono">
            <Chip>共 {runs.length} 条</Chip>
            {activeRun && <Chip tone="info">轮询中：{activeRun.run_id} · {activeRun.status}</Chip>}
            {list.error && <span className="text-rose-600">列表取数错误：{list.error}</span>}
          </div>
        }
      >
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
          <div className="overflow-auto max-h-80 border border-neutral-100 rounded">
            <table className="w-full text-left text-[11px] font-mono">
              <thead className="bg-neutral-50 text-neutral-400 border-b border-neutral-200 sticky top-0">
                <tr>
                  <th className="py-1.5 px-2">run_id</th>
                  <th className="py-1.5 px-2">状态</th>
                  <th className="py-1.5 px-2">创建时间（UTC）</th>
                  <th className="py-1.5 px-2">标的 / 窗口</th>
                  <th className="py-1.5 px-2 text-right">净收益</th>
                  <th className="py-1.5 px-2 text-right">回撤</th>
                  <th className="py-1.5 px-2 text-right">笔数</th>
                  <th className="py-1.5 px-2">配置 hash</th>
                  <th className="py-1.5 px-2">失败原因</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-neutral-100">
                {runs.length === 0 ? (
                  <tr>
                    <td colSpan={9} className="py-6 text-center text-neutral-400 font-sans">
                      {list.loading ? '读取中…' : '暂无实验记录'}
                    </td>
                  </tr>
                ) : (
                  runs.map((r) => {
                    const sel = r.run_id === selectedRunId;
                    const h = hashMap[r.run_id];
                    return (
                      <tr
                        key={r.run_id}
                        data-run-id={r.run_id}
                        onClick={() => {
                          didPick.current = true;
                          setSelectedRunId(r.run_id);
                        }}
                        className={`cursor-pointer transition-colors ${sel ? 'bg-emerald-50' : 'hover:bg-neutral-50'}`}
                      >
                        <td className="py-1.5 px-2 text-neutral-200 whitespace-nowrap">{r.run_id}</td>
                        <td className="py-1.5 px-2">
                          <span className={`px-1.5 py-0.5 rounded border text-[10px] ${statusStyle(r.status)}`}>{r.status}</span>
                        </td>
                        <td className="py-1.5 px-2 text-neutral-500 whitespace-nowrap">{fmtIsoUtc(r.created_at)}</td>
                        <td className="py-1.5 px-2 text-neutral-400 whitespace-nowrap">
                          {(r.symbols ?? []).join(',') || '—'}
                          {r.window ? ` · ${fmtUtc(r.window.win_start_ms)}→${fmtUtc(r.window.win_end_ms)}` : ''}
                        </td>
                        <td className={`py-1.5 px-2 text-right ${(r.net_return_pct ?? 0) >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
                          {fmtPct(r.net_return_pct, 4)}
                        </td>
                        <td className="py-1.5 px-2 text-right text-rose-600">
                          {r.max_drawdown_pct === null || r.max_drawdown_pct === undefined ? '—' : `-${r.max_drawdown_pct.toFixed(4)}%`}
                        </td>
                        <td className="py-1.5 px-2 text-right">{r.trades_count ?? '—'}</td>
                        <td className="py-1.5 px-2 text-neutral-500" title={h?.spec_sha256 ?? '未取到'}>
                          {h ? shortHash(h.spec_sha256, 10) : '—'}
                        </td>
                        <td className="py-1.5 px-2 text-rose-600 max-w-[16rem] truncate" title={r.failure_reason ?? undefined}>
                          {r.failure_reason ?? '—'}
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
            <div className="px-2 py-1 text-[10px] text-neutral-400 border-t border-neutral-100">
              列表端点 `GET /backtests` 的 summary **不含配置 hash**（精确缺口：`backtest_api._summary()` 未带
              `spec_sha256`/`*_params_hash`）⇒ 这里用只读 `GET /backtests/{'{id}'}` 补齐（并发上限 4，最多 30 条）；
              未取到的显示 —，不伪造。
            </div>
          </div>

          <LabQualityPanel q={quality.data} loading={quality.loading} error={quality.error} />
        </div>

        {/* 执行完成 ≠ 正式策略放行 */}
        <div className="mt-3 border-t border-neutral-100 pt-2 space-y-1">
          <div className="text-[11px] font-bold text-neutral-200 flex items-center gap-1">
            <FlaskConical className="w-3.5 h-3.5 text-amber-600" />
            执行完成 ≠ 正式策略放行 / M4 验收
          </div>
          {metrics.data?.m4_disclaimer && (
            <div className="text-[10px] text-amber-900 bg-amber-50 border border-amber-200 rounded px-2 py-1">
              {metrics.data.m4_disclaimer}
            </div>
          )}
          {metrics.data?.formal_m4_gate ? (
            <div className="text-[10px] text-neutral-400 font-mono break-all">
              formal_m4_gate：{JSON.stringify(metrics.data.formal_m4_gate)}
            </div>
          ) : (
            <div className="text-[10px] text-neutral-400">
              未取到 formal_m4_gate（运行未成功或该 run 无该字段）⇒ 不推断「已放行」。
            </div>
          )}
          <div className="text-[10px] text-neutral-500 flex items-center gap-1">
            <Clock className="w-3 h-3" />
            本工作台只做**独立候选研究**；在线策略仍为 3a–d，不修改在线/模拟账户。
            <AlertCircle className="w-3 h-3 text-neutral-400" />
            页面数值全部来自服务端账本；缺失显示 —，不造数。
          </div>
        </div>
      </SectionCard>
    </div>
  );
};

export default BacktestWorkbench;
