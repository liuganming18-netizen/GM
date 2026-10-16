# -*- coding: utf-8 -*-
"""WyckoffMaster v3.1 阶梯回测引擎（15m 逐根 · 多标的组合回放）。

执行/风控**唯一口径** = 任务二交付的共用执行核心
`策略/WyckoffMaster/execution.py`（exec_core v3.1.0）。本引擎只做
「多标的组合编排」（标的池 / 排名 / 仓位上限 / 逐段窗口 / 无前视核验 / 账本），
**所有成交与风控决定**都调用共用核心的纯函数，不另写策略：

  · 开仓唯一入口  `X.plan_entry`            —— 因果时钟（收盘+1ms+15m）、
                                               结构低点必需（不退回固定止损）、
                                               0.5% 权益 / 单币 / 总多头 / 现金 / 参与率 / 组合风险
                                               各上限整单拒单、穿底拒单。
  · 数量          `X.size_position`         —— 由 `plan_entry` 内部调用。
  · bar 内止损    `X.stop_hit_for_bar`      —— 开盘穿 stop 用更差 open；只读该 bar OHLC。
  · 台阶锁利      `X.apply_trail`           —— 下一根 15m 生效、单调不降、不回填。
  · 720h 到期     `X.expiry_exit_ms`        —— 入场 + 720h 后首根 15m 开盘。
  · 回撤锁定      `X.drawdown_state`        —— ≥20% 四动作、本轮持续锁定。
  · 资金费结算    `X.settle_funding_events` —— 窗口 (entry, exit]、缺 mark 记缺口不补 0。
  · 同刻顺序      `X.order_bar_events`      —— ①资金费 → ②到期/回撤/预约出场 → ③新入场
                                               → ④bar 内止损。
  · 取整/数量     `X.round_buy_price` / `X.round_protect_price` / `X.round_qty_down`。

信号与参考止损/目标 = 策略共用核心 `策略/WyckoffMaster/core.py`（任务二交付）。

v3.1 执行语义（与施工单「交易与资金费执行」逐条对齐）：
  · 最早入场 = 信号 bar 收盘 + 1ms + 15m，成交价 = 该时刻**首个已闭合 15m bar 的 open**
    （+ 不利滑点、按 tick 向上取整）；不用同根 low/close。
  · 止损逐 15m 检查、止损优先；开盘穿 stop 用更差 open + 卖出不利滑点。
  · 台阶/保本在信号周期收盘判定、**下一根 15m** 起生效且单调上移，不回填。
  · 真实结算资金费按 (entry_bar, exit_bar] 的结算事件记账；缺 event mark ⇒ 记缺口不补 0。
  · 段末未平仓用**本段最后 15m 收盘**强平（+不利滑点/费用/应结资金费），不用下一段开盘。
  · 20% 峰值回撤 ⇒ 锁止新入场，下一 15m 开盘清仓，本次 run 不恢复。
  · 同刻顺序：资金费 → 到期/DD 清仓 → 预约出场 → 新入场 → bar 内 stop → 权益。

性能（不改变任何决定）：资金费按**单调游标**只结算新增事件（等价于全量窗口过滤）；
标记价每根只算一次；入场候选按 (symbol, bar) 建索引避免重复扫描。
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import math
import sys
from pathlib import Path

import numpy as np

from . import paths as P
from . import loader as LD
from .ledger import Ledger

if str(P.STRATEGY_ROOT) not in sys.path:
    sys.path.insert(0, str(P.STRATEGY_ROOT))
from WyckoffMaster import execution as X      # noqa: E402  ← 共用执行核心（唯一口径）
from WyckoffMaster import core as WC           # noqa: E402
from WyckoffMaster import factors as WF        # noqa: E402
from WyckoffMaster.params import FROZEN        # noqa: E402

UTC = dt.timezone.utc
DETAIL_MS = P.DETAIL_MS
HOUR_MS = P.HOUR_MS
EPS = 1e-9
FIELDS = {"open": "b15_o", "high": "b15_h", "low": "b15_l", "close": "b15_c"}

EXEC = X.EXEC_FROZEN                    # 冻结执行/风控配置（唯一真值）
FEE_SIDE = EXEC.fee_each_side
SLIP_SIDE = EXEC.slippage_each_side
ENTRY_DELAY_MS = EXEC.entry_delay_minutes * 60_000
EXIT_DELAY_MS = EXEC.exit_delay_minutes * 60_000


def _utc(ms):
    return dt.datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _next_15m_after(ts_ms):
    """给定时刻之后（不含）对齐到的 15m 开盘边界。"""
    return (ts_ms // DETAIL_MS + 1) * DETAIL_MS


def _bar_close(T):
    """15m bar 的收盘时刻（数据约定：close_time = open_time + 15m − 1）。"""
    return T + DETAIL_MS - 1


def _daily_quote_volume(ot, qv):
    d = {}
    for i in range(len(ot)):
        day = int(ot[i]) // P.DAY_MS * P.DAY_MS
        d[day] = d.get(day, 0.0) + float(qv[i])
    return d


def median_qv_30d(daily_qv, t_ms):
    """过去 30 个**完整 UTC 日**的日成交额中位数（严格 as-of t，不用未来）。"""
    cur_day = t_ms // P.DAY_MS * P.DAY_MS
    days = sorted(d for d in daily_qv if d < cur_day)[-30:]
    if len(days) < 30:
        return 0.0
    vals = sorted(daily_qv[d] for d in days)
    n = len(vals)
    return 0.5 * (vals[n // 2] + vals[(n - 1) // 2])


def _funding_slice(sd, lo, hi):
    """资金费事件 [lo, hi) 的切片（真实结算事件：funding_time / funding_rate / mark_price）。"""
    out = []
    for j in range(lo, hi):
        mk = sd.fund_m[j]
        out.append({"funding_time": int(sd.fund_t[j]),
                    "funding_rate": float(sd.fund_r[j]),
                    "mark_price": float(mk) if np.isfinite(mk) else None})
    return out


class SymData:
    """单标的预计算（紧凑）：候选入场/出场索引 + 执行 bar + 资金费 + OI。

    `entry_bar[i]` / `exit_bar[i]` 按**共用核心因果时钟**推导：
      成交 bar = `next_15m_open_at_or_after(信号收盘 + 1ms + 延迟)`。
    """

    __slots__ = ("symbol", "tf", "entry_mode", "sig_close_time", "sig_close", "sig_low",
                 "don_exit_low", "entry_idx", "e_pos", "e_atr", "e_struct", "e_kind",
                 "entry_bar", "exit_idx", "exit_bar", "close_idx",
                 "b15_ot", "b15_o", "b15_h", "b15_l", "b15_c", "b15_qv", "b15_pos",
                 "fund_t", "fund_r", "fund_m", "oi_t", "oi_v", "daily_qv",
                 "tick", "step", "gap48")

    def __init__(self, symbol, tf, entry_mode, sig, b15, fund, oi, inst, stop_mode):
        self.symbol = symbol; self.tf = tf; self.entry_mode = entry_mode
        self.tick = inst.tick_size if inst else 0.0
        self.step = inst.step_size if inst else 0.0
        self.sig_close_time = sig["close_time"]
        self.sig_close = sig["close"]
        self.sig_low = sig["low"]
        bars = [{"open_time": int(sig["open_time"][i]), "close_time": int(sig["close_time"][i]),
                 "open": float(sig["open"][i]), "high": float(sig["high"][i]),
                 "low": float(sig["low"][i]), "close": float(sig["close"][i]),
                 "volume": float(sig["volume"][i]),
                 "quote_volume": float(sig["quote_volume"][i])}
                for i in range(len(sig["open_time"]))]
        fr = WC.compute_signals(bars, FROZEN, entry_mode=entry_mode, stop_mode=stop_mode,
                                price_step=self.tick)
        self.don_exit_low = fr.don_exit_low
        self.entry_idx = [i for i in range(len(self.sig_close_time)) if bool(fr.master_entry[i])]
        self.e_pos = {i: k for k, i in enumerate(self.entry_idx)}
        self.e_atr = [float(fr.atr[i]) if np.isfinite(fr.atr[i]) else None for i in self.entry_idx]
        self.e_struct = [float(fr.struct_low[i]) if np.isfinite(fr.struct_low[i]) else None
                         for i in self.entry_idx]
        self.e_kind = [fr.signal_kind[i] for i in self.entry_idx]
        # 因果成交 bar：收盘 + 1ms + 15m → 首个已闭合 15m 开盘
        self.entry_bar = [X.next_15m_open_at_or_after(int(self.sig_close_time[i]) + 1 + ENTRY_DELAY_MS)
                          for i in self.entry_idx]
        self.exit_idx = [i for i in range(len(self.sig_close_time))
                         if math.isfinite(self.don_exit_low[i])
                         and self.sig_close[i] < self.don_exit_low[i]]
        self.exit_bar = [X.next_15m_open_at_or_after(int(self.sig_close_time[i]) + 1 + EXIT_DELAY_MS)
                         for i in self.exit_idx]
        self.close_idx = {int(self.sig_close_time[i]): i
                          for i in range(len(self.sig_close_time))}
        self.b15_ot = b15["open_time"]; self.b15_o = b15["open"]; self.b15_h = b15["high"]
        self.b15_l = b15["low"]; self.b15_c = b15["close"]; self.b15_qv = b15["quote_volume"]
        self.b15_pos = {int(t): i for i, t in enumerate(self.b15_ot)}
        self.fund_t = fund["funding_time"] if fund else np.array([], np.int64)
        self.fund_r = fund["funding_rate"] if fund else np.array([], float)
        self.fund_m = fund["mark_price"] if fund else np.array([], float)
        self.oi_t = oi["ts"] if oi else np.array([], np.int64)
        self.oi_v = oi["open_interest"] if oi else np.array([], float)
        self.daily_qv = _daily_quote_volume(self.b15_ot, self.b15_qv)
        d = np.diff(self.b15_ot) if len(self.b15_ot) > 1 else np.array([])
        self.gap48 = bool((d > 48 * HOUR_MS).any()) if len(d) else False

    def bar(self, T, field):
        i = self.b15_pos.get(int(T))
        if i is None:
            return None
        return float(getattr(self, FIELDS[field])[i])

    def mark(self, T):
        """T 时刻（含）最后已知 15m 收盘（缺口用最后已知值，不补 0）。"""
        i = self.b15_pos.get(int(T))
        if i is not None:
            return float(self.b15_c[i])
        j = bisect.bisect_right(self.b15_ot, int(T)) - 1
        return float(self.b15_c[j]) if j >= 0 else None


class Position:
    __slots__ = ("pid", "sym", "entry_bar", "entry_sig_idx", "entry_price", "entry_raw",
                 "qty", "notional", "stop", "entry_atr", "entry_fee", "entry_slip",
                 "equity_at_entry", "rank_key", "funding_expense", "funding_n",
                 "funding_blocked", "fund_from", "fund_settled", "open")

    def __init__(self, pid, sym, entry_bar, entry_sig_idx, entry_price, entry_raw, qty,
                 notional, stop, entry_atr, entry_fee, entry_slip, equity_at_entry, rank_key,
                 fund_from):
        self.pid = pid; self.sym = sym; self.entry_bar = entry_bar
        self.entry_sig_idx = entry_sig_idx; self.entry_price = entry_price
        self.entry_raw = entry_raw; self.qty = qty; self.notional = notional
        self.stop = stop; self.entry_atr = entry_atr; self.entry_fee = entry_fee
        self.entry_slip = entry_slip; self.equity_at_entry = equity_at_entry
        self.rank_key = rank_key
        self.funding_expense = 0.0; self.funding_n = 0     # 支出口径（>0 = 账户支付）
        self.funding_blocked = False; self.fund_from = fund_from
        self.fund_settled = set()                          # 全量重扫模式的幂等键
        self.open = True


class LevelSpec:
    def __init__(self, level, entry_mode, stop_mode, trail_mode, rank_mode, parent=""):
        self.level = level; self.entry_mode = entry_mode; self.stop_mode = stop_mode
        self.trail_mode = trail_mode; self.rank_mode = rank_mode; self.parent = parent

    def as_dict(self):
        return {"level": self.level, "entry_mode": self.entry_mode, "stop_mode": self.stop_mode,
                "trail_mode": self.trail_mode, "rank_mode": self.rank_mode, "parent": self.parent}


class Backtest:
    def __init__(self, *, seg_data, spec, timeframe, start_ms, end_ms, run_id, ledger_path,
                 code_sha="", config_sha="", input_sha="", commit_every=1, symbols=None,
                 initial_equity=None, rank_provider=None, max_bars=None,
                 collect_candidates=False, poison_after_ms=None, funding_rescan=False):
        P.assert_segment_not_holdout(seg_data.seg)
        self.SD = seg_data; self.spec = spec; self.tf = timeframe
        self.start_ms = start_ms; self.end_ms = end_ms
        self.run_id = run_id; self.code_sha = code_sha; self.config_sha = config_sha
        self.input_sha = input_sha
        self.initial_equity = float(initial_equity if initial_equity is not None
                                    else EXEC.initial_equity)
        self.max_positions = EXEC.max_positions_total
        self.symbols = list(symbols) if symbols else seg_data.symbols(timeframe)
        self.rank_provider = rank_provider; self.max_bars = max_bars
        self.collect_candidates = collect_candidates
        self.poison_after_ms = poison_after_ms   # 无前视核验：该时刻之后的数据置毒
        self.funding_rescan = funding_rescan     # 性能基线：每根全量重扫资金费（等价性对照）
        self.candidates = []          # M2 超限拒单分母（容量无关的唯一候选）
        self.led = Ledger(ledger_path, commit_every=commit_every)
        self._syms = {}
        # 账户态（共享 5000U / 最多 10 仓 / 只多）
        self.cash = self.initial_equity
        self.peak = self.initial_equity
        self.locked = False
        self.dd_pending = False
        self.positions = []
        self.pid_seq = 0

    # ── 无前视核验用置毒 ──────────────────────────────────────────────────
    def _poison(self, sig, b15):
        c = self.poison_after_ms
        for d in (sig, b15):
            if d is None:
                continue
            m = d["open_time"] > c
            for k in ("open", "high", "low", "close"):
                d[k] = np.where(m, 1e9, d[k])
            d["quote_volume"] = np.where(m, 1e12, d["quote_volume"])
            d["volume"] = np.where(m, 1e12, d["volume"])

    def _prepare(self):
        for s in self.symbols:
            sig = self.SD.ohlcv(s, self.tf)
            b15 = self.SD.ohlcv(s, "15m")
            if sig is None or b15 is None or len(sig["open_time"]) < 60 or len(b15["open_time"]) < 10:
                continue
            if self.poison_after_ms is not None:
                sig = {k: (v.copy() if isinstance(v, np.ndarray) else v) for k, v in sig.items()}
                b15 = {k: (v.copy() if isinstance(v, np.ndarray) else v) for k, v in b15.items()}
                self._poison(sig, b15)
            self._syms[s] = SymData(s, self.tf, self.spec.entry_mode, sig, b15,
                                    self.SD.funding(s), self.SD.oi(s), self.SD.instrument(s),
                                    self.spec.stop_mode)
        return len(self._syms)

    # ── M2 超限拒单分母（容量无关；只取到达结构止损超限判断的唯一候选）──
    def m2_candidates(self):
        out = []
        for s, sd in self._syms.items():
            for pos, i in enumerate(sd.entry_idx):
                k = sd.e_pos[i]
                struct_low = sd.e_struct[k]; atr = sd.e_atr[k]
                if struct_low is None or atr is None or not (atr > 0):
                    continue
                o = sd.bar(sd.entry_bar[pos], "open")
                if o is None or not math.isfinite(o) or o <= 0:
                    continue
                entry = X.round_buy_price(o * (1.0 + SLIP_SIDE), sd.tick)
                if entry <= struct_low:
                    continue                      # 穿底 ⇒ 未到达超限判断（不计入分母）
                out.append({"kind": sd.e_kind[k], "entry_price": entry,
                            "struct_low": struct_low, "atr": atr})
        return out

    # ── 主回放 ────────────────────────────────────────────────────────────
    def run(self):
        n = self._prepare()
        led = self.led; run_id = self.run_id
        for k, v in (("spec", json.dumps(self.spec.as_dict(), ensure_ascii=False)),
                     ("timeframe", self.tf), ("segment", self.SD.seg),
                     ("code_sha", self.code_sha), ("config_sha", self.config_sha),
                     ("input_sha", self.input_sha), ("n_symbols", n),
                     ("core_version", X.CORE_VERSION),
                     ("exec_config_hash", EXEC.hash()),
                     ("strategy_version", X.STRATEGY_VERSION),
                     ("ledger_model_version", X.LEDGER_MODEL_VERSION),
                     ("execution_core", "策略/WyckoffMaster/execution.py")):
            led.meta(run_id, k, v)

        if self.collect_candidates:
            self.candidates = self.m2_candidates()

        start_ms = self.start_ms; end_ms = self.end_ms
        entries_by_bar, exits_by_bar = {}, {}
        for s, sd in self._syms.items():
            for pos, i in enumerate(sd.entry_idx):
                t = sd.entry_bar[pos]
                if start_ms <= t < end_ms:
                    entries_by_bar.setdefault(t, []).append((s, i))
            for pos, i in enumerate(sd.exit_idx):
                t = sd.exit_bar[pos]
                if start_ms <= t < end_ms:
                    exits_by_bar.setdefault(t, []).append((s, i))

        trades = wins = losses = 0
        sum_gross = sum_fees = sum_fund = 0.0
        max_dd = 0.0; gap_events = 0; rej_counts = {}
        consumed = {"level": self.spec.as_dict(), "kinds": {}}
        nbars = 0

        def record(pnl, w, l):
            return (w + 1, l) if pnl > 0 else (w, l + 1)

        for T in LD.grid15(start_ms, end_ms):
            nbars += 1
            if self.max_bars and nbars > self.max_bars:
                break
            ct = _bar_close(T)
            # ⓪ 台阶锁利：信号周期在上一根 15m 收盘 ⇒ 本根开盘起生效（单调不降、不回填）
            for p in self.positions:
                if not p.open:
                    continue
                sd = self._syms[p.sym]
                ci = sd.close_idx.get(T - 1)
                if ci is not None and ci >= p.entry_sig_idx:
                    p.stop = self._trail(p, sd, ci, T)
            # ① 真实资金费结算（结算至本 bar 收盘；位置仍持有）
            for p in self.positions:
                if p.open:
                    self._settle_funding(p, self._syms[p.sym], ct, led)
            # ② 到期 / 回撤清仓 / 预约出场
            for p in list(self.positions):
                if not p.open:
                    continue
                due = X.expiry_exit_ms(p.entry_bar, EXEC.max_holding_hours)
                if T >= due:
                    g, f, fu = self._close(p, T, self._px(p, T, "open") or p.entry_price,
                                           "expiry_720h", led)
                    sum_gross += g; sum_fees += f; sum_fund += fu; trades += 1
                    wins, losses = record(g - f - fu, wins, losses)
            if self.dd_pending:                      # 回撤锁定 → 本根开盘清仓
                for p in list(self.positions):
                    if p.open:
                        g, f, fu = self._close(p, T, self._px(p, T, "open") or p.entry_price,
                                               "dd_lock_liquidation", led)
                        sum_gross += g; sum_fees += f; sum_fund += fu; trades += 1
                        wins, losses = record(g - f - fu, wins, losses)
                self.dd_pending = False
            for (s, i) in exits_by_bar.get(T, []):
                for p in self.positions:
                    if p.open and p.sym == s:
                        px = self._px(p, T, "open")
                        if px is None:
                            gap_events += 1; continue
                        g, f, fu = self._close(p, T, px, "donchian_exit", led)
                        sum_gross += g; sum_fees += f; sum_fund += fu; trades += 1
                        wins, losses = record(g - f - fu, wins, losses)
            # ③ 新入场（受锁定与仓位上限控制；plan_entry 内部再校验）
            cands = entries_by_bar.get(T, [])
            nopen = sum(1 for p in self.positions if p.open)
            if cands and not self.locked and nopen < self.max_positions:
                open_syms = {p.sym for p in self.positions if p.open}
                for (s, i) in self._rank(cands, T):
                    nopen = sum(1 for p in self.positions if p.open)
                    if self.locked or nopen >= self.max_positions:
                        break
                    if s in open_syms:
                        continue
                    _sd = self._syms[s]
                    _kind = _sd.e_kind[_sd.e_pos[i]]
                    consumed["kinds"][_kind] = consumed["kinds"].get(_kind, 0) + 1
                    ok = self._try_enter(s, i, T, led, rej_counts)
                    if ok:
                        nopen += 1
                        open_syms.add(s)
            # ④ bar 内止损（止损优先；只读该 bar OHLC）
            for p in list(self.positions):
                if not p.open:
                    continue
                sd = self._syms[p.sym]
                i = sd.b15_pos.get(int(T))
                if i is None:
                    gap_events += 1; continue
                hit = X.stop_hit_for_bar(direction="long", stop_price=p.stop,
                                         bar_open=float(sd.b15_o[i]), bar_high=float(sd.b15_h[i]),
                                         bar_low=float(sd.b15_l[i]))
                if hit is not None:
                    g, f, fu = self._close(p, T, hit["raw_price"], "stop_loss", led)
                    sum_gross += g; sum_fees += f; sum_fund += fu; trades += 1
                    wins, losses = record(g - f - fu, wins, losses)
            # ⑤ 权益 / 回撤（15m 收盘，唯一一次判定）
            mv = self._mark_all(T)
            eq = self.cash + mv
            if eq > self.peak:
                self.peak = eq
            dd = (self.peak - eq) / self.peak if self.peak > 0 else 0.0
            max_dd = max(max_dd, dd)
            st = X.drawdown_state(equity=eq, peak_equity=self.peak, locked_before=self.locked)
            if st["tripped_now"]:
                self.locked = True
                self.dd_pending = True
                led.order(run_id, f"dd:{T}", "", "none", "lock", T, None, None, None, None,
                          None, None, "drawdown_lock_20pct", None, "risk")
            nopen = sum(1 for p in self.positions if p.open)
            if nopen or T % (24 * 4) == 0:
                led.equity(run_id, T, self.cash,
                           sum(p.notional for p in self.positions if p.open), mv,
                           self.cash - self.initial_equity, mv, eq, self.peak, dd,
                           self.locked, nopen)

        # ⑥ 段末强平（本段最后 15m 收盘）
        last_bar = (end_ms // DETAIL_MS - 1) * DETAIL_MS
        for p in list(self.positions):
            if not p.open:
                continue
            sd = self._syms[p.sym]
            self._settle_funding(p, sd, _bar_close(last_bar), led)
            c = sd.mark(last_bar)
            if c is None:
                continue
            g, f, fu = self._close(p, last_bar, c,
                                   "segment_end_force_close_last_15m_close", led,
                                   exit_ts=last_bar)
            sum_gross += g; sum_fees += f; sum_fund += fu; trades += 1
            wins, losses = record(g - f - fu, wins, losses)

        led.review(run_id, segment=self.SD.seg, level=self.spec.level, timeframe=self.tf,
                   initial_equity=self.initial_equity, final_cash=self.cash,
                   final_equity=self.cash, trades=trades, wins=wins, losses=losses,
                   gross_pnl=sum_gross, fees=sum_fees, funding_net=-sum_fund,
                   net_pnl=sum_gross - sum_fees - sum_fund, max_drawdown=max_dd,
                   halted=int(self.locked), code_sha=self.code_sha,
                   config_sha=self.config_sha, input_sha=self.input_sha,
                   extra_json=json.dumps({"rejects": rej_counts, "gap_events": gap_events,
                                          "n_bars": nbars, "n_symbols": n,
                                          "consumed": consumed,
                                          "funding_rows": led.counts.get("funding", 0),
                                          "core_version": X.CORE_VERSION,
                                          "exec_config_hash": EXEC.hash()},
                                         ensure_ascii=False))
        led.flush()
        return {"run_id": run_id, "trades": trades, "final_equity": self.cash,
                "gross": sum_gross, "fees": sum_fees, "funding": -sum_fund,
                "net": sum_gross - sum_fees - sum_fund, "max_dd": max_dd,
                "halted": self.locked, "counts": dict(led.counts), "rejects": rej_counts,
                "gap_events": gap_events, "consumed": consumed, "n_symbols": n,
                "core_version": X.CORE_VERSION, "exec_config_hash": EXEC.hash()}

    # ── 工具 ──────────────────────────────────────────────────────────────
    def _px(self, p, T, field):
        return self._syms[p.sym].bar(T, field)

    def _mark_all(self, T):
        v = 0.0
        for p in self.positions:
            if p.open:
                m = self._syms[p.sym].mark(T)
                v += p.qty * (m if m is not None else p.entry_price)
        return v

    def _settle_funding(self, p, sd, exit_ts, led):
        """按共用核心结算 (entry_ts, exit_ts] 的资金费事件。

        · 默认（游标）：只对**新增**事件调用 `X.settle_funding_events`，游标单调推进。
        · `funding_rescan=True`（性能基线）：每根对**全量**事件调用同一函数，
          用 `event_identity` 幂等去重 —— 与共用核心 `ReplayEngine` 的写法一致。
        两种写法必须产出**逐位一致**的账本（见 scripts/perf_equiv.py）。
        """
        n = len(sd.fund_t)
        if n == 0:
            return
        hi = bisect.bisect_right(sd.fund_t, int(exit_ts))
        if self.funding_rescan:
            lo = 0
        else:
            if hi <= p.fund_from:
                return
            lo = p.fund_from
        evs = _funding_slice(sd, lo, hi)
        r = X.settle_funding_events(position_id=p.pid, qty=p.qty, entry_ts=p.entry_bar,
                                    exit_ts=int(exit_ts), events=evs)
        for c in r["charges"]:
            ident = c["event_identity"]
            if ident in p.fund_settled:            # 幂等：同一 (position, funding_time) 只记一次
                continue
            p.fund_settled.add(ident)
            p.funding_expense += c["amount"]; p.funding_n += 1
            self.cash -= c["amount"]                       # ΔCash = −支出
            led.funding(self.run_id, p.pid, c["funding_time"], c["rate"], c["mark"],
                        "event_mark", p.qty, -c["amount"])
        for g in r["gaps"]:
            if g["funding_time"] in p.fund_settled:
                continue
            p.fund_settled.add(g["funding_time"])
            p.funding_blocked = True
            led.funding(self.run_id, p.pid, g["funding_time"], g.get("funding_rate"), None,
                        "missing", p.qty, None)
        p.fund_from = hi

    def _close(self, p, T, px_raw, reason, led, exit_ts=None):
        """平仓（卖出）：不利滑点 + 按 tick 向下取整；资金费结算至 exit_ts。"""
        sd = self._syms[p.sym]
        xts = int(T if exit_ts is None else exit_ts)
        self._settle_funding(p, sd, xts, led)
        exit_price = X.round_protect_price(px_raw * (1 - SLIP_SIDE), sd.tick)
        exit_slip = p.qty * (px_raw - exit_price)
        exit_fee = exit_price * p.qty * FEE_SIDE
        gross = (exit_price - p.entry_price) * p.qty
        net = gross - p.entry_fee - exit_fee - p.funding_expense
        self.cash += exit_price * p.qty - exit_fee
        led.position(self.run_id, position_id=p.pid, symbol=p.sym, entry_time=p.entry_bar,
                     entry_raw=p.entry_raw, entry_price=p.entry_price, quantity=p.qty,
                     notional=p.notional, stop_price=p.stop,
                     stop_atr_mult=((p.entry_price - p.stop) / p.entry_atr) if p.entry_atr else None,
                     entry_fee=p.entry_fee, entry_slippage_cost=p.entry_slip,
                     equity_at_entry=p.equity_at_entry, rank_key=p.rank_key,
                     funding=-p.funding_expense, funding_events=p.funding_n,
                     exit_time=xts, exit_raw=px_raw, exit_price=exit_price, exit_reason=reason,
                     exit_fee=exit_fee, exit_slippage_cost=exit_slip, gross_pnl=gross,
                     net_pnl=net, holding_hours=(xts - p.entry_bar) / HOUR_MS)
        led.order(self.run_id, f"{p.pid}:exit", p.sym, "sell", "fill", xts, px_raw, exit_price,
                  p.qty, exit_fee, exit_slip, exit_price * p.qty, reason, p.pid, "exit")
        p.open = False
        return gross, p.entry_fee + exit_fee, p.funding_expense

    def _rank(self, cands, T):
        """同刻排序：**按本层 rank_mode 分派**（实验差异必须真实生效）。

        · `quote_vol_30d_median`（M0–M3）：30 日成交额中位数降序。
        · `oi_change_90d_pct`（M4）：OI 变化自身 90 日分位降序（由 rank_provider 提供）。
        """
        if self.spec.rank_mode == "oi_change_90d_pct" and self.rank_provider is not None:
            return self.rank_provider(cands, T, self)
        return sorted(cands, key=lambda c: -median_qv_30d(self._syms[c[0]].daily_qv, T))

    def _try_enter(self, s, i, T, led, rej_counts):
        """开仓：**唯一入口** `X.plan_entry`（含因果时钟/结构低点/各上限/拒单原因）。"""
        sd = self._syms[s]
        k = sd.e_pos[i]
        atr = sd.e_atr[k]; struct_low = sd.e_struct[k]; kind = sd.e_kind[k]
        sig_ct = int(sd.sig_close_time[i])
        ident = f"binance|{s}|{kind}|{sig_ct}"
        j = sd.b15_pos.get(int(T))
        if j is None:
            rej_counts["missing_exec_bar"] = rej_counts.get("missing_exec_bar", 0) + 1
            led.signal(self.run_id, s, sig_ct, T, kind, atr, struct_low, None, None,
                       "reject", "missing_exec_bar", self.spec.rank_mode)
            return False
        open15 = float(sd.b15_o[j])
        prev_qv = float(sd.b15_qv[j - 1]) if j - 1 >= 0 else None
        gross_open = self._mark_all(T)
        eq = self.cash + gross_open
        port_risk = sum(((p.entry_price - p.stop) * p.qty) for p in self.positions if p.open)
        limits = {"quantity_step": sd.step, "min_quantity": sd.step, "max_quantity": 0.0,
                  "min_notional": 0.0, "price_step": sd.tick}
        d = X.plan_entry(
            symbol=s, direction="long", signal_kind=kind, signal_bar_close_time=sig_ct,
            available_at=sig_ct, request_clock_ms=sig_ct, open_15m=open15, atr_val=atr,
            struct_low=struct_low, entry_mode=self.spec.entry_mode, stop_mode=self.spec.stop_mode,
            trail_mode=self.spec.trail_mode, price_step=sd.tick, limits=limits,
            equity=eq, free_cash=self.cash, gross_open_notional=gross_open,
            open_risk_abs=port_risk, prev15m_quote_volume=prev_qv,
            open_positions=sum(1 for p in self.positions if p.open), attempt_clock_ms=T,
            exchange="binance", event_identity=ident)
        if d.get("reject_reason"):
            rej = d["reject_reason"]
            rej_counts[rej] = rej_counts.get(rej, 0) + 1
            led.signal(self.run_id, s, sig_ct, T, kind, atr, struct_low,
                       d.get("stop_price"), d.get("target_price"), "reject", rej,
                       self.spec.rank_mode)
            led.order(self.run_id, f"{s}:{i}:entry", s, "buy", "reject", T, open15,
                      d.get("entry_price"), None, None, None, None, rej, None, "entry")
            return False
        entry_price = d["entry_price"]; stop = d["stop_price"]; qty = d["qty"]
        notional = d["notional"]
        self.pid_seq += 1; pid = f"P{self.pid_seq:06d}"
        entry_fee = notional * FEE_SIDE
        entry_slip = qty * (entry_price - open15)
        self.positions.append(Position(
            pid, s, int(T), i, entry_price, d.get("entry_raw"), qty, notional, stop, atr,
            entry_fee, entry_slip, eq, self.spec.rank_mode,
            bisect.bisect_right(sd.fund_t, int(T))))     # 窗口 (entry, ...]
        self.cash -= notional + entry_fee
        led.signal(self.run_id, s, sig_ct, T, kind, atr, struct_low, stop,
                   d.get("target_price"), "enter", "", self.spec.rank_mode)
        led.order(self.run_id, f"{s}:{i}:entry", s, "buy", "fill", T, open15, entry_price, qty,
                  entry_fee, entry_slip, notional, "", pid, "entry")
        return True

    # ── 台阶锁利（在信号周期收盘判定，下一根 15m 生效）──────────────────
    def _trail(self, p, sd, ci, T):
        if self.spec.trail_mode == "none":
            return p.stop
        r = X.apply_trail(closes=sd.sig_close[p.entry_sig_idx:ci + 1],
                          lows=sd.sig_low[p.entry_sig_idx:ci + 1],
                          current_stop=p.stop, entry_price=p.entry_price,
                          entry_atr=p.entry_atr,
                          evaluate_close_time=int(sd.sig_close_time[ci]),
                          next_15m_open_time=int(T), mode=self.spec.trail_mode)
        return float(r["stop"]) if r["updated"] else p.stop


if __name__ == "__main__":
    print(json.dumps(P.assert_production_untouched(), ensure_ascii=False, indent=2))
