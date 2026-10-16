/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useMemo, useState } from 'react';
import { CryptoSignal, SignalArm, TokenMarketItem } from '../types/crypto';
import { TokenIcon } from './TokenIcon';
import type { PaperPendingView, PaperPositionView } from '../api/liveTrading';
import {
  Target,
  ArrowRight,
  ShieldCheck,
  Search,
  Layers,
  Activity,
  Clock,
} from 'lucide-react';

interface SignalListPanelProps {
  signals: CryptoSignal[];
  tokens?: TokenMarketItem[];
  selectedSignalId: string | null;
  onSelectSignal: (signal: CryptoSignal) => void;
  /**
   * 服务端账本未平仓持仓（用于按 `signal_uid` 关联「实际成交」）。
   * **「已买入」标记只能来自这里**——浏览器不得自行造仓。
   */
  paperPositions?: PaperPositionView[];
  /** 服务端登记的未成交挂单（用于「计划下单」层）。 */
  paperPending?: PaperPendingView[];
  /** 服务端新开仓总闸（fail-closed）；用于如实说明「新开仓已被阻断」。 */
  newEntryGate?: { allowNewEntries: boolean; reason: string | null } | null;
}

/** 数字格式化：null / 非有限值一律 '—'，绝不用 0 冒充真实值。 */
const fmtPrice = (v: number | null | undefined): string => {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  const abs = Math.abs(v);
  const digits = abs >= 1000 ? 2 : abs >= 1 ? 4 : abs >= 0.01 ? 5 : 6;
  return v.toLocaleString(undefined, {
    minimumFractionDigits: Math.min(2, digits),
    maximumFractionDigits: digits,
  });
};

/** 将 ISO/UTC 时间字符串格式化为紧凑可读的 `MM-DD HH:mm UTC`，无法解析则原样返回。 */
const fmtCompactUtc = (raw: string | null | undefined): string => {
  if (!raw) return '—';
  const ms = Date.parse(raw);
  if (Number.isNaN(ms)) return raw;
  const d = new Date(ms);
  const mo = String(d.getUTCMonth() + 1).padStart(2, '0');
  const da = String(d.getUTCDate()).padStart(2, '0');
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mm = String(d.getUTCMinutes()).padStart(2, '0');
  return `${mo}-${da} ${hh}:${mm} UTC`;
};

/** 结构类型与掩码 → 可读中文标签与色彩样式（白底浅色） */
const kindBadge = (
  kind: string | null | undefined,
  masks?: string[] | null,
): { label: string; cls: string } | null => {
  const maskList = (masks ?? []).filter((m): m is string => !!m);
  if (maskList.length > 1) {
    const names = maskList.map((m) =>
      m === 'base' ? '突破' : m === 'spring' ? 'Spring' : m === 'sc' ? 'SC' : m,
    );
    return {
      label: names.join('+'),
      cls: 'text-indigo-700 bg-indigo-50 border-indigo-200',
    };
  }
  const k = (kind ?? maskList[0] ?? '').toLowerCase();
  if (k === 'base' || k === 'donchian') {
    return {
      label: '突破',
      cls: 'text-sky-700 bg-sky-50 border-sky-200',
    };
  }
  if (k === 'spring') {
    return {
      label: 'Spring',
      cls: 'text-amber-700 bg-amber-50 border-amber-200',
    };
  }
  if (k === 'sc') {
    return {
      label: '恐慌SC',
      cls: 'text-purple-700 bg-purple-50 border-purple-200',
    };
  }
  if (k) {
    return {
      label: k,
      cls: 'text-gray-600 bg-gray-50 border-gray-200',
    };
  }
  return null;
};

/**
 * 格式化信号时间：`MM-DD HH:mm (周期)`（无年份，紧凑专业）。
 *
 * **2026-10-15 任务四**：删除「无时间就用浏览器当前时刻」的兜底（那是**伪造的确认时刻**）。
 * 无真实时间 ⇒ 返回 `'—'`；无周期 ⇒ 不补默认 `1h`。
 */
const fmtSignalTime = (raw: string | null | undefined, timeframe: string | null | undefined): string => {
  if (!raw) return '—';
  const ms = Date.parse(raw);
  if (Number.isNaN(ms)) return '—';
  const d = new Date(ms);
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  const tf = timeframe ? ` (${timeframe})` : '';
  return `${m}-${day} ${hh}:${mm}${tf}`;
};

/**
 * 解析信号展示价格：触发价 / 现价 / 止损价 / 目标价。
 *
 * **2026-10-15 闭环整改·阶段A（假展示清理）**：
 *   原实现含大量**凭空价格**：ARKM 硬编码 1.425 / 1.420 / 1.350 / 1.575；
 *   其余币种缺价时用 `trigger*1.003`、`entry±5%`、`entry+1.5R` 等**推导**补出，
 *   并把 `currentPrice` 直接当作「入场价」展示（**用现价冒充入场价**）。
 *   这些都属「无账本成交却画出买卖点」的假展示。
 *   现改为：**只用信号自带的真实字段**；缺失 ⇒ 返回 `null`，页面显示 `—`。
 *   另：**入场/成交价只由账本 fill 产生**，本面板不再以任何形式代填入场价；
 *   现价列如实标注为「现价」（取信号自带现价，或该币真实行情价）。
 */
const resolveSignalPrices = (
  signal: CryptoSignal,
  tokenPrice?: number | null,
): {
  triggerPrice: number | null;
  currentPrice: number | null;
  stopLossPrice: number | null;
  targetPrice: number | null;
} => {
  const fin = (v: number | null | undefined): number | null =>
    v !== null && v !== undefined && Number.isFinite(v) ? v : null;
  return {
    triggerPrice: fin(signal.triggerPrice ?? signal.detectionLow),
    currentPrice: fin(signal.currentPrice ?? tokenPrice),
    stopLossPrice: fin(signal.stopLossPrice),
    targetPrice: fin(signal.targetPrice),
  };
};

/**
 * 解析交易理由文本。
 *
 * **2026-10-15 闭环整改·阶段A**：删除全部**编造理由**（原实现在缺理由时拼出
 * 「大户持仓做多(1.62)背离散户(0.81) · OI增24% · 现价贴近巨鲸均价(1.40) · 插针收回」，
 * 其中 1.62 / 0.81 / 24% / 1.40 均为**常量兜底**，并非真实证据）。
 * 现只**原样透出后端结构化证据**；无结构化证据 ⇒ 返回 `null`，页面显示
 * 「—（无结构化理由，缺失即未知）」。AI 只解释已有结构化证据，不补造。
 */
const resolveReason = (signal: CryptoSignal): string | null => {
  const raw = (signal.actionableReason ?? signal.validityReason ?? '').trim();
  if (!raw) return null;
  if (raw === 'enterable' || raw === 'base') return null; // 状态代号不是理由
  return raw;
};

/**
 * 信号区（**当前交易信号**，2026-10-14 信号区清理与信息展示深化）。
 *
 * 本面板只呈现「当前有效信号」：
 *   · 数据入口已按 `scope=current` 请求（后端在分页 LIMIT 与 total/has_more **之前**过滤）；
 *   · 这里再保留一层**防御性判定** `isCurrentSignal` —— 即使后端口径回归，
 *     历史研究 / 启动前仅展示 / 结构提示（display_only / not_this_arm_structure 等）
 *     也不会重新出现在本区（含刷新、轮询与任何筛选切换）。
 */
export const SignalListPanel: React.FC<SignalListPanelProps> = ({
  signals,
  tokens = [],
  selectedSignalId,
  onSelectSignal,
  paperPositions = [],
  paperPending = [],
  newEntryGate = null,
}) => {
  const [armFilter, setArmFilter] = useState<'ALL' | '3a' | '3b' | '3c' | '3d'>('ALL');
  const [kindFilter, setKindFilter] = useState<'ALL' | 'base' | 'spring' | 'sc'>('ALL');
  const [searchQuery, setSearchQuery] = useState('');

  /**
   * 当前有效信号判定（与后端 `scope=current` 同一口径，客户端复算一遍作防线）：
   *   ① origin='live'                     —— 非历史研究
   *   ② executableState='enterable'       —— 非仅展示 / 非本臂结构 / 非阻断
   *   ③ role='enterable_arm_entry'        —— **真实策略臂入场事件**（结构证据提示不算）
   */
  const isCurrentSignal = (s: CryptoSignal): boolean =>
    s.origin === 'live' &&
    s.executableState === 'enterable' &&
    s.role === 'enterable_arm_entry';

  /**
   * 账本联动索引：`signal_uid → 持仓 / 挂单`（2026-10-15 任务四）。
   *
   * 「已买入」标记只能由**服务端账本成交**（`paperPositions`）产生；
   * 「计划下单」只能由**服务端登记的挂单**（`paperPending`）产生。前端不造任何一条。
   */
  const posBySignalUid = useMemo(() => {
    const m = new Map<string, PaperPositionView[]>();
    for (const p of paperPositions) {
      if (!p.signalUid) continue;
      const arr = m.get(p.signalUid) ?? [];
      arr.push(p);
      m.set(p.signalUid, arr);
    }
    return m;
  }, [paperPositions]);

  const pendBySignalUid = useMemo(() => {
    const m = new Map<string, PaperPendingView[]>();
    for (const p of paperPending) {
      if (!p.signalUid) continue;
      const arr = m.get(p.signalUid) ?? [];
      arr.push(p);
      m.set(p.signalUid, arr);
    }
    return m;
  }, [paperPending]);

  const tokenMap = useMemo(() => {
    const map = new Map<string, TokenMarketItem>();
    tokens.forEach((t) => {
      map.set(t.symbol.toUpperCase(), t);
      if (t.originalSymbol) map.set(t.originalSymbol.toUpperCase(), t);
    });
    return map;
  }, [tokens]);

  const currentSignals = useMemo(() => signals.filter(isCurrentSignal), [signals]);

  /** 同买点合卡：一张卡列全部命中臂与各臂去向。 */
  const armList = (s: CryptoSignal): SignalArm[] => {
    if (s.arms && s.arms.length > 0) return s.arms;
    if (s.arm) {
      return [
        {
          arm: s.arm,
          signalId: s.id,
          status: s.status,
          executableState: s.executableState ?? null,
          actionable: s.actionable ?? null,
          actionableReason: s.actionableReason ?? null,
          structuralKind: s.structuralKind ?? null,
          sourceMasks: s.sourceMasks ?? null,
          executionLabel: s.executionLabel ?? null,
        },
      ];
    }
    return [];
  };

  /** 卡上臂数合计（用于标题「N 张卡 · M 臂」）。 */
  const totalArms = useMemo(
    () =>
      currentSignals.reduce(
        (n, s) => n + (s.armCount ?? (s.arms ? s.arms.length : s.arm ? 1 : 0)),
        0,
      ),
    [currentSignals],
  );

  /** 各臂命中卡数统计（供顶部快筛标签展示真实计数）。 */
  const armCounts = useMemo(() => {
    const counts: Record<'3a' | '3b' | '3c' | '3d', number> = {
      '3a': 0,
      '3b': 0,
      '3c': 0,
      '3d': 0,
    };
    for (const s of currentSignals) {
      const hitSet = new Set<string>(
        s.armsHit && s.armsHit.length > 0
          ? s.armsHit
          : armList(s)
              .map((a) => a.arm)
              .filter((a): a is string => !!a),
      );
      if (hitSet.has('3a')) counts['3a'] += 1;
      if (hitSet.has('3b')) counts['3b'] += 1;
      if (hitSet.has('3c')) counts['3c'] += 1;
      if (hitSet.has('3d')) counts['3d'] += 1;
    }
    return counts;
  }, [currentSignals]);

  /** 按用户选择的策略臂、结构类型与搜索词过滤当前有效信号。 */
  const filteredSignals = useMemo(() => {
    const q = searchQuery.trim().toUpperCase();
    return currentSignals.filter((s) => {
      const arms = armList(s);
      if (armFilter !== 'ALL') {
        const hit =
          (s.armsHit && s.armsHit.includes(armFilter)) ||
          arms.some((a) => a.arm === armFilter);
        if (!hit) return false;
      }
      if (kindFilter !== 'ALL') {
        const k = (s.structuralKind ?? '').toLowerCase();
        const masks = (s.sourceMasks ?? []).map((m) => m.toLowerCase());
        const armMatch = arms.some(
          (a) =>
            (a.structuralKind ?? '').toLowerCase() === kindFilter ||
            (a.sourceMasks ?? []).some((m) => m.toLowerCase() === kindFilter),
        );
        if (k !== kindFilter && !masks.includes(kindFilter) && !armMatch) {
          return false;
        }
      }
      if (q) {
        const sym = (s.tokenSymbol ?? '').toUpperCase();
        const rawSym = (s.rawSymbol ?? '').toUpperCase();
        const ex = (s.exchange ?? '').toUpperCase();
        if (!sym.includes(q) && !rawSym.includes(q) && !ex.includes(q)) {
          return false;
        }
      }
      return true;
    });
  }, [currentSignals, armFilter, kindFilter, searchQuery]);

  /** 六态 → 展示标签与配色（未知/null 显式标「未决」，不冒充失效）。 */
  const statusLabel = (s: CryptoSignal): { text: string; cls: string } => {
    switch (s.status) {
      case 'confirmed':
        return { text: '已确认', cls: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30' };
      case 'candidate':
        return { text: '候选', cls: 'bg-sky-500/20 text-sky-400 border-sky-500/30' };
      case 'waiting_retest':
        return { text: '待回踩', cls: 'bg-amber-500/20 text-amber-400 border-amber-500/30' };
      case 'invalidated':
        return { text: '已失效', cls: 'bg-rose-500/20 text-rose-400 border-rose-500/30' };
      case 'expired':
        return { text: '已过期', cls: 'bg-rose-500/20 text-rose-400 border-rose-500/30' };
      default:
        return { text: '未决', cls: 'bg-neutral-800 text-neutral-300 border-neutral-700' };
    }
  };

  /** 执行状态（后端判定；用可读中文，不暴露内部代号）。 */
  const execLabel = (s: CryptoSignal): string => {
    if (!s.executableState) return '—';
    if (s.executableState === 'enterable') return '可执行（满足入场门禁）';
    if (s.executableState.startsWith('blocked')) return '阻断（不可开仓）';
    return '不可执行';
  };

  /** 某一臂的去向（合卡卡面上的短标签）。 */
  const armOutcome = (a: SignalArm): { text: string; textCls: string } => {
    if (a.executableState === 'enterable') return { text: '可开仓', textCls: 'text-emerald-300' };
    if (a.executableState && a.executableState.startsWith('blocked')) {
      return { text: '阻断', textCls: 'text-rose-300' };
    }
    return { text: '不可执行', textCls: 'text-neutral-400' };
  };

  const armShortKind = (a: SignalArm): string => {
    const k = (a.structuralKind ?? '').toLowerCase();
    if (k === 'spring') return '·Spring';
    if (k === 'sc') return '·SC';
    if (k === 'base') return '·突破';
    return '';
  };

  /** 结构类型可读名（结构发现阶段）。 */
  const kindName = (s: CryptoSignal): string => {
    const k = (s.structuralKind ?? '').toLowerCase();
    if (k === 'spring') return 'Spring（威科夫弹簧）';
    if (k === 'sc') return 'SC（恐慌抛售）';
    if (k === 'base' || k === 'donchian') return '突破（唐奇安）';
    return s.structuralKind ?? '—';
  };

  /**
   * **四阶段事件链**（2026-10-15 任务四）——逐阶段只引用真实字段：
   *   ① 结构发现：结构类型 + 结构证据 bar（`barOpenTime`/`barCloseTime`，ms 纪元）
   *   ② 信号确认：`confirmedAt`/`eventTime` + 六态
   *   ③ 计划下单：服务端登记的**未成交挂单**（按 `signal_uid` 关联；无 ⇒ 未登记）
   *   ④ 实际成交：服务端账本**已成交持仓**（按 `signal_uid` 关联；无 ⇒ 未成交）
   * 任何阶段无真实数据一律显示 `—`，绝不推断下一阶段。
   */
  const eventChain = (s: CryptoSignal) => {
    const pos = posBySignalUid.get(s.id) ?? [];
    const pend = pendBySignalUid.get(s.id) ?? [];
    const barOpen = s.barOpenTime ? new Date(s.barOpenTime).toISOString().slice(0, 16).replace('T', ' ') : null;
    const barClose = s.barCloseTime ? new Date(s.barCloseTime).toISOString().slice(0, 16).replace('T', ' ') : null;
    return {
      structure: {
        text: kindName(s),
        bar: barOpen || barClose ? `结构bar ${barOpen ?? '—'}→${barClose ?? '—'} UTC` : '结构bar 无记录',
      },
      confirm: {
        text: s.confirmedAt ?? s.eventTime ?? null,
        status: statusLabel(s).text,
      },
      plan: pend.length > 0
        ? { filled: true, text: `已登记挂单 ${pend.length} 笔（未成交）`, ids: pend.map((p) => `#${p.pendingId}`).join(' ') }
        : { filled: false, text: '未登记挂单', ids: '' },
      fill: pos.length > 0
        ? { filled: true, text: `已成交（账本）`, ids: pos.map((p) => `${p.arm}#${p.positionId}`).join(' ') }
        : { filled: false, text: '未成交', ids: '' },
    };
  };

  const gateBlocked = !!newEntryGate && !newEntryGate.allowNewEntries;

  return (
    <div
      className="workbench-side-panel workbench-signal-panel bg-white border-r border-gray-200 flex flex-col justify-between shrink-0 select-none overflow-hidden"
      data-testid="signal-panel"
      data-signal-count={currentSignals.length}
      data-signal-scope="current"
    >
      <div className="workbench-signal-content flex flex-col flex-1 overflow-hidden">
        {/* Header + 极简搜索区（白底无特效） */}
        <div className="p-3 border-b border-gray-200 space-y-2 bg-white">
          <div className="flex items-center justify-between">
            <h3 className="text-xs font-bold text-gray-900 flex items-center gap-1.5 uppercase tracking-wider">
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>
              <span>交易信号</span>
            </h3>
            <span className="text-[10px] text-gray-500 font-mono">
              {filteredSignals.length} 笔信号
            </span>
          </div>

          {/* 币种搜索 + 结构类型快筛 */}
          <div className="flex items-center gap-1.5">
            <div className="relative flex-1">
              <Search className="w-3.5 h-3.5 text-gray-400 absolute left-2.5 top-1/2 -translate-y-1/2 pointer-events-none" />
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="搜索代币..."
                className="w-full bg-gray-50 border border-gray-200 focus:bg-white focus:border-gray-300 rounded pl-7 pr-2 py-1 text-[11px] text-gray-800 placeholder-gray-400 outline-none font-mono"
              />
            </div>
            <select
              value={kindFilter}
              onChange={(e) => setKindFilter(e.target.value as 'ALL' | 'base' | 'spring' | 'sc')}
              className="bg-gray-50 border border-gray-200 rounded px-2 py-1 text-[11px] text-gray-700 outline-none cursor-pointer"
            >
              <option value="ALL">全部形态</option>
              <option value="base">突破</option>
              <option value="spring">Spring</option>
              <option value="sc">恐慌SC</option>
            </select>
          </div>
        </div>

        {/* Signals List */}
        <div className="flex-1 overflow-y-auto p-2.5 space-y-2.5 bg-gray-50/50">
          {currentSignals.length === 0 ? (
            <div
              data-testid="signal-empty"
              className="mt-6 mx-1 p-4 rounded-lg border border-dashed border-gray-200 bg-white text-center space-y-1.5"
            >
              <p className="text-[12px] font-medium text-gray-700">暂无符合规则的实时信号</p>
              <p className="text-[10px] leading-relaxed text-gray-400">
                当前交易信号区只显示实时真实入场的信号，不使用任何演示/兜底样本。
              </p>
              {gateBlocked && (
                <p className="text-[10px] leading-relaxed text-amber-600">
                  服务端已阻断全部新开仓（总闸 fail-closed；原因：{newEntryGate?.reason ?? '未标注'}）；
                  退出/费用/资金费照常处理。
                </p>
              )}
            </div>
          ) : filteredSignals.length === 0 ? (
            <div className="mt-6 mx-1 p-4 rounded-lg border border-dashed border-gray-200 bg-white text-center space-y-1.5">
              <p className="text-[11px] text-gray-500">当前筛选条件下无匹配信号</p>
              <button
                type="button"
                onClick={() => {
                  setKindFilter('ALL');
                  setSearchQuery('');
                }}
                className="text-[10px] text-emerald-600 hover:underline"
              >
                重置筛选条件
              </button>
            </div>
          ) : (
            filteredSignals.map((signal) => {
              const isSelected = selectedSignalId === signal.id;
              const isLong = signal.direction === 'long';
              const isShort = signal.direction === 'short';
              const kBadge = kindBadge(signal.structuralKind, signal.sourceMasks);

              const matchedToken =
                tokenMap.get((signal.tokenSymbol || '').toUpperCase()) ||
                tokenMap.get((signal.rawSymbol || '').toUpperCase());

              const prices = resolveSignalPrices(signal, matchedToken?.price);
              const reasonText = resolveReason(signal);
              const chain = eventChain(signal);
              const filled = chain.fill.filled;
              const timeDisplay = fmtSignalTime(
                signal.confirmedAt ?? signal.eventTime ?? signal.triggerTime,
                signal.timeframe,
              );

              return (
                <div
                  key={signal.id}
                  onClick={() => onSelectSignal(signal)}
                  data-testid="signal-row"
                  data-origin={signal.origin ?? ''}
                  data-executable-state={signal.executableState ?? ''}
                  data-symbol={signal.tokenSymbol}
                  data-signal-uid={signal.id}
                  data-filled={filled ? '1' : '0'}
                  className={`p-3 rounded-lg border cursor-pointer transition-colors space-y-2.5 ${
                    isSelected
                      ? 'bg-emerald-50/20 border-2 border-emerald-600'
                      : 'bg-white border border-gray-200 hover:border-gray-300'
                  }`}
                >
                  {/* 第 1 行：纯粹代币图标 + 代币名称，右侧「已买入（账本）」+ 做多标签 */}
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2 min-w-0">
                      <TokenIcon symbol={signal.tokenSymbol} size="sm" />
                      <span className="font-bold text-gray-900 text-[14px] font-mono tracking-tight shrink-0">
                        {signal.tokenSymbol}
                      </span>
                      {/* 「已买入」只能来自服务端账本成交；无成交不显示 */}
                      {filled && (
                        <span
                          className="text-[10px] px-1.5 py-0.5 rounded border border-emerald-300 bg-emerald-50 text-emerald-700 font-sans font-bold shrink-0"
                          title={chain.fill.ids}
                        >
                          已买入（账本）
                        </span>
                      )}
                    </div>
                    <span
                      className={`text-[11px] px-2 py-0.5 rounded font-medium border ${
                        isLong
                          ? 'text-emerald-700 bg-emerald-50 border-emerald-200'
                          : isShort
                            ? 'text-rose-700 bg-rose-50 border-rose-200'
                            : 'text-gray-600 bg-gray-100 border-gray-200'
                      }`}
                    >
                      {isLong ? '做多' : isShort ? '做空' : '观望'}
                    </span>
                  </div>

                  {/* 第 2 行：无年份时间 + 周期，右侧精简形态标签 */}
                  <div className="flex items-center justify-between text-[11px] font-mono text-gray-500">
                    <span>{timeDisplay}</span>
                    {kBadge && (
                      <span
                        className={`text-[10px] px-1.5 py-0.2 rounded border font-sans font-medium ${kBadge.cls}`}
                      >
                        {kBadge.label}
                      </span>
                    )}
                  </div>

                  {/* 第 3 行：理由（只透出后端结构化证据；缺失即未知，不编造） */}
                  <div className="text-[11px] leading-relaxed text-gray-700 bg-gray-50 p-2 rounded border border-gray-100 font-sans">
                    <span className="text-gray-500 font-semibold mr-1">理由：</span>
                    <span className={reasonText ? '' : 'text-gray-400'}>
                      {reasonText ?? '—（无结构化理由，缺失即未知）'}
                    </span>
                  </div>

                  {/* 第 3.5 行：四阶段事件链（结构发现 → 信号确认 → 计划下单 → 实际成交） */}
                  <div className="text-[10px] bg-gray-50/70 rounded border border-gray-100 p-1.5 space-y-0.5 font-sans">
                    <div className="flex items-center justify-between">
                      <span className="text-gray-400">① 结构发现</span>
                      <span className="text-gray-700 truncate max-w-[68%] text-right">{chain.structure.text}</span>
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="text-gray-400">② 信号确认</span>
                      <span className="text-gray-700">{chain.confirm.status}{chain.confirm.text ? ` · ${chain.confirm.text.replace('T', ' ').replace('Z', '')}` : ''}</span>
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="text-gray-400">③ 计划下单</span>
                      <span className={chain.plan.filled ? 'text-sky-700' : 'text-gray-400'}>{chain.plan.text}{chain.plan.ids ? ` ${chain.plan.ids}` : ''}</span>
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="text-gray-400">④ 实际成交</span>
                      <span className={chain.fill.filled ? 'text-emerald-700 font-bold' : 'text-gray-400'}>{chain.fill.text}{chain.fill.ids ? ` ${chain.fill.ids}` : ''}</span>
                    </div>
                  </div>

                  {/* 第 4 行：触发价 现价 止损价 目标价
                      —— 仅展示信号自带真实字段；缺失显示 `—`。
                      **入场/成交价不在此展示**（只由服务端账本 fill 产生，杜绝现价冒充入场价）。 */}
                  <div className="grid grid-cols-4 gap-1.5 pt-1.5 text-[11px] font-mono border-t border-gray-100">
                    <div>
                      <div className="text-[10px] text-gray-400 font-sans">触发价</div>
                      <div className="text-gray-800 font-medium mt-0.5">
                        {fmtPrice(prices.triggerPrice)}
                      </div>
                    </div>
                    <div>
                      <div className="text-[10px] text-gray-400 font-sans">现价</div>
                      <div className="text-emerald-600 font-bold mt-0.5">
                        {fmtPrice(prices.currentPrice)}
                      </div>
                    </div>
                    <div>
                      <div className="text-[10px] text-gray-400 font-sans">止损价</div>
                      <div className="text-rose-600 font-medium mt-0.5">
                        {fmtPrice(prices.stopLossPrice)}
                      </div>
                    </div>
                    <div>
                      <div className="text-[10px] text-gray-400 font-sans">目标价</div>
                      <div className="text-sky-600 font-medium mt-0.5">
                        {fmtPrice(prices.targetPrice)}
                      </div>
                    </div>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>

      {/* 底部紧凑状态 */}
      <div className="px-3 py-2 border-t border-gray-200 bg-white text-[10px] text-gray-500 flex items-center justify-between">
        <span className="flex items-center gap-1.5 text-gray-600">
          <ShieldCheck className="w-3.5 h-3.5 text-emerald-600" />
          <span>点击信号联动定位行情与开仓</span>
        </span>
        <span className="font-mono text-gray-400">实时推送</span>
      </div>
    </div>
  );
};
