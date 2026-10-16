#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""信号与持仓接通（2026-10-13）· **实时 worker**（需 numpy+ccxt ⇒ 用 freqtrade 解释器运行）。

一轮做的事（幂等、可重启）：

  1. **实时信号增量**：`live_signal_engine.run_cycle` → 写 `live_signal`（真实结构/臂事件）。
  2. **模拟盘推进**（`paper_engine`）：
       · 新 `executable_state='enterable'` 信号 → 按已批准模型**模拟开仓**（服务端账本）；
       · 已开持仓 → 逐根已闭合 15m bar 检查**固定止损**（入场价−3×信号 ATR，缺口按更差开盘 + 不利滑点）、
         **信号退出**（冻结 `wx_donchian_wyckoff` 的 Donchian 退出事件 + 已批准 15m 延迟）、
         **最长持仓 720h** 安全退出。
  3. **实盘只读账户**：`live_account.read_and_store` → 写 `live_account_snapshot`。
  4. **心跳**：`live_run:account_state`（含各源状态、耗时）。

只读行情库；不写行情库；不提交任何真实订单；不打印任何密钥。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

BASE = Path("/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究")
BACKEND = BASE / "后端实现"
ROOT = BASE / "最新唐奇安威科夫接通与收尾_2026-10-11"
W37 = ROOT / "取消成交额硬门槛与375新准入37"
W43 = ROOT / "量价信号接入与模拟历史并行43"
for _p in (str(W43), str(W37), str(BACKEND)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
sys.dont_write_bytecode = True

import live_store as L                                     # noqa: E402
import paper_engine as PE                                  # noqa: E402
import exec_bridge as EB                                   # noqa: E402（v3.1 共用执行核心桥）
import live_signal_engine as SE                            # noqa: E402
import live_account as LA                                  # noqa: E402
import _warmup43 as W                                      # noqa: E402
from strategies import wx_config as CFG                    # noqa: E402
from strategies import wx_donchian_wyckoff as WX           # noqa: E402

UTC = dt.timezone.utc
H = 3_600_000
DAY = 24 * H
ARMS = L.ARMS
MIN = 60_000
M15 = 900_000

# ── 两个**独立**开关（任务二验收重点）────────────────────────────────────────
# · `MAINTAIN_EXISTING`：旧仓维护（退出 / 资金费 / 到期 / 回撤 / 台阶）—— 常开，
#   **不受**新开仓闸影响；
# · 新开仓闸：读服务端控制文件（`PE.new_entry_gate()`，fail-closed）——正式候选未选定/
#   未经最终验收 ⇒ 继续阻断（默认 False）。
MAINTAIN_EXISTING = True
TRAIL_MODE = "step_swing"          # 台阶锁利（下一根 15m 生效；单调不降、不回填）


def _now():
    return int(time.time() * 1000)


def _arm_exits(sym, arm, resolved, T, window_hours=SE.WINDOW_HOURS):
    """冻结策略在该币上的 Donchian 退出事件（1h open_time）+ 装配好的 1h bar（供台阶锁利）。

    装配长度用 `SE.ASSEMBLY_LOOKBACK_HOURS`（= 冻结策略自带 `warmup_contiguous_bars`，
    **不是准入门槛**），否则冻结策略算不出状态。
    """
    pack = W.assemble(sym, T, warm_hours=SE.ASSEMBLY_LOOKBACK_HOURS, window_hours=window_hours,
                      lookback_hours=SE.ASSEMBLY_LOOKBACK_HOURS + window_hours + 24)
    bars = pack[0]
    r = WX.run(bars, resolved, arm_id=resolved.get("registered_config_id"),
               identity=("binance", sym), require_identity=True)
    return (r.get("exits") or []), bars


def _trail_schedule(bars_1h, *, entry_ts, entry_price, entry_atr, initial_stop,
                    params, trail_mode=TRAIL_MODE):
    """台阶锁利时间表：[(effective_from_ms, stop)]（下一根 15m 生效；单调不降、不回填）。

    评估点 = 入场后的**信号周期（1h）收盘**；生效点 = 紧随其后的 15m 开盘。
    回看低点/新高只用**已收盘** 1h bar（`core.evaluate_trail_stop` 保证无前视）。
    """
    if not EB.CORE_AVAILABLE or trail_mode == "none" or entry_atr is None or entry_atr <= 0:
        return []
    # 入场信号 bar（含）起的 1h 序列
    seq = [b for b in bars_1h if int(b["open_time"]) + H - 1 >= int(entry_ts)]
    if not seq:
        return []
    closes = [float(b["close"]) for b in seq]
    lows = [float(b["low"]) for b in seq]
    out = []
    stop = float(initial_stop)
    for k in range(1, len(seq) + 1):
        close_t = int(seq[k - 1]["close_time"])
        eff = EB.next_15m_open_at_or_after(close_t + 1)
        res = EB.apply_trail(closes=closes[:k], lows=lows[:k], current_stop=stop,
                             entry_price=float(entry_price), entry_atr=float(entry_atr),
                             evaluate_close_time=close_t, next_15m_open_time=eff,
                             mode=trail_mode)
        if res.get("updated"):
            stop = float(res["stop"])
            out.append((int(eff), stop, res.get("reason")))
    return out


def _scan_position_exit(p, *, mkt, arm_exits, bars_1h, params, T, trail_mode=TRAIL_MODE):
    """逐根已闭合 15m 扫描一笔持仓的出场（**同刻顺序固定**，全部只用已闭合 bar）。

    同一根 15m 内的判定顺序（与共用执行核心的相位顺序一致）：
      ① 台阶止损在**本根开盘**生效（`effective_from_ms <= bar.open_time`，单调不降、不回填）；
      ② **开盘已穿 stop** ⇒ 按更差 open 成交（止损优先，缺口）；
      ③ 到期 720h ⇒ 本根开盘（= 入场+720h 后首根 15m 开盘，修复 D01）；
      ④ 预约信号退出 ⇒ 本根开盘（冻结 Donchian 退出事件 + 已批准 15m 延迟）；
      ⑤ bar 内触 stop ⇒ 按 stop 价成交（止损优先于同根其它价量事件）。

    返回 `(raw_price, bar_open_time, bar_close_time, reason, exit_ts, decision_ctx)`；无出场 ⇒ None。
    """
    entry_ts = int(p["entry_ts"])
    direction = (p.get("direction") or "long")
    base_stop = p["stop_price"]
    stop = None if base_stop is None else float(base_stop)
    entry_price = float(p["entry_price"])
    entry_atr = p.get("atr_at_entry")
    # ① 台阶止损时间表（下一根 15m 生效）
    trail = _trail_schedule(bars_1h, entry_ts=entry_ts, entry_price=entry_price,
                            entry_atr=(float(entry_atr) if entry_atr else None),
                            initial_stop=(stop if stop is not None else entry_price),
                            params=params, trail_mode=trail_mode)
    trail = sorted(trail, key=lambda x: x[0])
    trail_i = 0
    due_expiry = PE.expiry_exit_ts(entry_ts) if EB.CORE_AVAILABLE else \
        ((entry_ts + int(params["max_holding_hours"]) * H + M15 - 1) // M15) * M15
    delay_ms = int(params["signal_exit_delay_minutes"]) * MIN
    bars = PE.closed_15m(mkt, p["symbol"], entry_ts, T)
    for b in bars:
        ot, ct = int(b["open_time"]), int(b["close_time"])
        # ① 台阶止损：本根开盘起生效（取所有 effective<=ot 的最大值 ⇒ 单调不降）
        while trail_i < len(trail) and trail[trail_i][0] <= ot:
            if stop is None or trail[trail_i][1] > stop:
                stop = trail[trail_i][1]
            trail_i += 1
        op = PE._f(b["open"]); hi = PE._f(b["high"]); lo = PE._f(b["low"])
        # ② 开盘已穿 stop（止损优先，缺口用更差 open）
        if stop is not None and op is not None:
            if direction == "long" and op <= stop:
                return (op, ot, ct, "stop_loss", ot,
                        {"rule": "gap_open_worse_than_stop", "stop": stop, "bar_open": op})
            if direction == "short" and op >= stop:
                return (op, ot, ct, "stop_loss", ot,
                        {"rule": "gap_open_worse_than_stop", "stop": stop, "bar_open": op})
        # ③ 720h 到期：本根开盘
        if ot >= due_expiry:
            return (op, ot, ct, "max_holding_720h_at_first_15m_open", ot,
                    {"due_ms": due_expiry, "rule": "entry_plus_720h_first_15m_open"})
        # ④ 预约信号退出：本根开盘（信号事件 + 延迟 <= 本根开盘）
        for ex in arm_exits:
            ex_ot = int(ex["open_time"])
            if ex_ot + delay_ms <= entry_ts:
                continue
            if ex_ot + delay_ms <= ot:
                return (op, ot, ct, "signal_exit_donchian", ot,
                        {"signal_event_open_time": ex_ot, "delay_ms": delay_ms,
                         "rule": "signal_exit_at_15m_open"})
        # ⑤ bar 内触 stop
        if stop is not None:
            if direction == "long" and lo is not None and lo <= stop:
                return (stop, ot, ct, "stop_loss", ot,
                        {"rule": "stop_price_touched_intrabar", "stop": stop, "bar_low": lo})
            if direction == "short" and hi is not None and hi >= stop:
                return (stop, ot, ct, "stop_loss", ot,
                        {"rule": "stop_price_touched_intrabar", "stop": stop, "bar_high": hi})
    return None


def advance_paper(*, T=None, log=print) -> dict:
    """把 enterable 实时信号落成纸面持仓，并管理已开持仓的退出。

    **因果闸门**：只自动开 `confirmed_at >= 实时起点水位线` 的信号。
    水位线之前的实时信号照常展示，但不自动开仓（详见 `live_store` 水位线注释）。
    """
    T = int(T) if T else _now()
    params = PE.approved_params()
    resolved = {a: CFG.resolve(None, a) for a in ARMS}
    # **固定启用下限（2026-10-14 09 契约）**：`epoch` 由 `live_store` 持久化到
    # `live_run.paper_epoch_ms`（**只允许不降**，重启从库读回**不改值**）；
    # 有效下限 = `max(live_start, epoch)`。四臂、挂单推进、当前信号 API 共用同一下限。
    epoch = L.ensure_paper_epoch_ms()
    floor_ms = L.effective_floor_ms()
    con = L.live_conn(ro=True)
    try:
        # 待开仓：enterable 且未处理，且**信号确认 bar 的真实右边界不早于有效下限**。
        # **2026-10-14 生产接通修正**：原查询是
        #   `… WHERE … origin='live' ORDER BY confirmed_at ASC LIMIT 200`
        # —— `LIMIT` 作用在**未按下限过滤**的结果上，只取每臂**最旧的 200 条**（4 臂共 800）。
        # 一旦某臂 enterable 总数超过 200（本次生产实测 3a=2766），**下限之后的真实臂入场
        # 永远进不了 `todo`** ⇒ 每轮 `todo=0`、纸面链路空转（真实缺陷，非"无信号"）。
        # 修法：把下限条件**下推到 SQL**，先过滤再取；下限之前的部分单独计数
        # （语义不变：仍**只展示、不自动开仓**）。
        # **边界口径**：原始 `close_time` 存的是闭区间右端 **−1ms**（如 09:00 收盘存
        # `1791939599999`）⇒ 判定用真实右边界 `confirmed_at + 1`；**不改原值**。
        todo = []
        n_pre = 0
        for arm in ARMS:
            # 信号确认 bar 的**真实右边界** = `max(confirmed_at, available_at) + 1`
            # （原始 `close_time` 存闭区间右端 −1ms）。判定下限用**同一表达式**，
            # 与 `open_position` 的 epoch 门禁逐字一致。
            right_expr = ("(CASE WHEN available_at > confirmed_at THEN available_at "
                          "ELSE confirmed_at END) + 1")
            n_pre += int(con.execute(
                f"SELECT COUNT(*) FROM live_signal WHERE strategy_key=? "
                f"AND executable_state='enterable' AND origin='live' "
                f"AND {right_expr} < ?",
                (arm, floor_ms)).fetchone()[0])
            # **把「已处理 / 已挂单 / 已终态」全部下推到 SQL**（2026-10-14 09 契约）：
            # 原实现先取 200 条再在 Python 里逐个剔除 ⇒ 旧行会**永久占满 LIMIT 名额**，
            # 新信号被饿死。改为 `ATTACH` 该臂纸面库 + 跨库 `NOT EXISTS`，先排除再取。
            # （只读连接上的 ATTACH 与跨库 NOT EXISTS 均已实测可用；TEMPORARY 表不可用。）
            # ① 已成交/已处理（`paper_processed`）；② 已登记挂单（`paper_pending.status='pending'`，
            #    交 `advance_pending` 处理，避免孤儿挂单）；③ 已终态「错过成交时刻不回填」
            #    （`paper_order` 中该原因且 status='rejected'）。
            alias = f"p_{arm}"
            pdb = L.PAPER_DIR / f"paper_{arm}.sqlite3"
            attached = False
            try:
                con.execute(f"ATTACH DATABASE ? AS {alias}", (str(pdb),))
                attached = True
            except Exception:                                # noqa: BLE001
                attached = False
            if attached:
                excl = (f"AND NOT EXISTS (SELECT 1 FROM {alias}.paper_processed p "
                        f"WHERE p.signal_uid = live_signal.signal_uid) "
                        f"AND NOT EXISTS (SELECT 1 FROM {alias}.paper_pending q "
                        f"WHERE q.account_id=? AND q.signal_uid = live_signal.signal_uid "
                        f"AND q.status='pending') "
                        f"AND NOT EXISTS (SELECT 1 FROM {alias}.paper_order o "
                        f"WHERE o.account_id=? AND o.event_uid = live_signal.signal_uid "
                        f"AND o.status='rejected' "
                        f"AND o.reject_reason='missed_fill_moment_no_backfill')")
                args = [arm, floor_ms, f"paper43_{arm}", f"paper43_{arm}"]
            else:
                # 兜底（ATTACH 不可用）：退回 Python 侧剔除，语义不变但会占名额。
                excl = ""
                args = [arm, floor_ms]
            # **同刻竞争按既有 daily rank 排序**（2026-10-14 09 契约核对修正）：
            # 原实现按 `confirmed_at ASC, symbol` 处理，**未按每日排名**。冻结回测的口径是
            # `due.sort(key=lambda e: (-rank, exchange, symbol))`（`wx_execution`）——
            # 排名 = **前 30 日成交额中位**，**降序**（越大越优先），并列以
            # `(exchange, symbol)` 升序**稳定决胜**。此处逐字同序（`rank` 为 NULL 时
            # SQLite 在 DESC 下排最后，与冻结 `-rk.get(key, 0.0)` 的兜底次序一致）。
            rows = con.execute(
                f"SELECT * FROM live_signal WHERE strategy_key=? "
                f"AND executable_state='enterable' AND origin='live' "
                f"AND {right_expr} >= ? {excl} "
                f"ORDER BY rank DESC, exchange ASC, symbol ASC LIMIT 200", args).fetchall()
            for r in rows:
                d = dict(r)
                if not attached and con_processed(arm, d["signal_uid"]):
                    continue
                todo.append(d)
            if attached:
                try:
                    con.execute(f"DETACH DATABASE {alias}")
                except Exception:                            # noqa: BLE001
                    pass
    finally:
        con.close()

    # ── 新开仓闸（**独立开关**；fail-closed）──────────────────────────────────
    # 旧仓维护（下面的挂单推进/资金费/退出/回撤）**不经过**本闸。
    gate = PE.new_entry_gate()
    opened: list = []
    rejected: list = []
    if not gate["allow_new_entries"]:
        log(f"[paper] 新开仓已阻断（{gate['gate_reason']}，来源 {gate['gate_file']}）："
            f"候选 {len(todo)} 条仅登记为阻断，不写 paper_processed ⇒ 可重试；"
            f"旧仓维护（退出/资金费/到期/回撤）继续执行。")
        rejected = [{"signal_uid": d["signal_uid"], "symbol": d["symbol"],
                     "reason": PE.GATE_REASON} for d in todo]
        todo = []

    for d in todo:
        try:
            payload = json.loads(d.get("payload_json") or "{}")
        except ValueError:
            payload = {}
        atr = payload.get("atr_signal") or d.get("detection_atr")
        if not atr:
            rejected.append({"signal_uid": d["signal_uid"], "reason": "missing_atr"})
            PE.log_no_fill(d["strategy_key"], d["symbol"], d["signal_uid"], "missing_atr")
            continue
        # **方向原样传递，绝不改写**（任务二重点修复：short 曾被写成 long 并做多）。
        # v3.1 只多 ⇒ 非 long 显式拒单（不翻转、不静默丢弃）。
        raw_dir = payload.get("direction") if payload.get("direction") is not None \
            else d.get("direction")
        direction = (EB.normalize_direction(raw_dir) if EB.CORE_AVAILABLE
                     else (raw_dir if raw_dir in ("long", "short") else None))
        if direction is None:
            rejected.append({"signal_uid": d["signal_uid"], "symbol": d["symbol"],
                             "reason": "illegal_direction_missing_or_unknown",
                             "raw_direction": raw_dir})
            PE.log_no_fill(d["strategy_key"], d["symbol"], d["signal_uid"],
                           "illegal_direction_missing_or_unknown")
            continue
        if direction == "short":
            rejected.append({"signal_uid": d["signal_uid"], "symbol": d["symbol"],
                             "reason": "short_disabled_by_frozen_view_only_long",
                             "direction": direction})
            PE.log_no_fill(d["strategy_key"], d["symbol"], d["signal_uid"],
                           "short_disabled_by_frozen_view_only_long")
            continue
        # **因果修正（2026-10-13 / 2026-10-14 09 契约）**：把信号的**经济时刻 / 可用时刻 /
        # 真实首次请求墙钟**全部交给 `open_position`；成交绝不早于这些时刻，错过的成交时刻
        # **不回填**（见 FILL_POLICY）。
        # **关键（循环第 2 次·请求前旧价回填）**：`requested_at` **必须**是**真实首次请求**
        # 而非本轮重试时钟 —— 原实现传 `T`，每轮刷新 ⇒ `order_request_ms` 随轮次前移，
        # 成交 bar 反而落在**请求之前**（实测 16 笔 `entry_ts < requested_at`）。
        # 现由 `paper_request_anchor`（持久锚，`INSERT OR IGNORE` 只落第一次）钉住。
        try:
            anchor = PE.first_request_anchor(d["strategy_key"], d["signal_uid"], T)
            res = PE.open_position(d["strategy_key"], d["symbol"], signal_uid=d["signal_uid"],
                                  direction=direction, atr=float(atr),
                                  now_ms=T,
                                  event_time=int(d["confirmed_at"]),
                                  available_at=int(d.get("available_at") or d["confirmed_at"]),
                                  requested_at=anchor, origin="live",
                                  epoch_ms=epoch, first_request_ms=anchor,
                                  stop_mode=payload.get("stop_mode"),
                                  entry_mode=payload.get("entry_mode"),
                                  struct_low=payload.get("struct_low"),
                                  trailing_mode=payload.get("trailing_mode"))
        except Exception as exc:                            # noqa: BLE001
            # **单笔异常不拖垮整轮**（2026-10-14 加固）：行情库/账本临时不可用时
            # 该信号下一轮仍会重试（异常路径**不写** `paper_processed`）。
            res = {"ok": False, "reason": f"exception_{type(exc).__name__}",
                   "signal_uid": d["signal_uid"], "symbol": d["symbol"],
                   "detail": str(exc)}
        (opened if res.get("ok") else rejected).append(res)
    log(f"[paper] 下限(epoch={epoch}, floor={floor_ms}) 下限前(仅展示、不自动开仓)={n_pre} "
        f"待开仓 todo={len(todo)} ok={len(opened)} rejected={len(rejected)} "
        f"reasons={_reason_hist(rejected)}")

    # 未来待成交挂单：**用真实进程时钟推进**（到点才成交；错过则拒单，不回填）
    try:
        pend_res = PE.advance_pending(T=T, log=log)
    except Exception as exc:                                # noqa: BLE001
        # 行情库不可用时挂单推进失败 ⇒ 只降级本轮结果，挂单**保持 pending**，
        # 下一轮继续推进（不丢单、不回填）。（2026-10-14 加固）
        log(f"[paper] 挂单推进失败（行情库不可用？）：{type(exc).__name__}: {exc}")
        pend_res = {"error": f"{type(exc).__name__}: {exc}"}

    # 资金费：真实结算事件**经过时**即时逐笔记账（幂等），不是只在平仓时补总额
    funding = {}
    for arm in ARMS:
        try:
            funding[arm] = PE.accrue_funding(arm, upto_ms=T, log=log)
        except Exception as exc:                        # noqa: BLE001
            log(f"[paper] 资金费入账 {arm} 失败：{type(exc).__name__}: {exc}")
            funding[arm] = {"error": f"{type(exc).__name__}: {exc}"}

    # ── 退出管理（旧仓维护；**不受**新开仓闸影响）──────────────────────────────
    # 相位顺序（与共用执行核心一致）：①资金费已在上方逐笔记账 →
    # ②回撤锁定（撤挂单/下一根 15m 开盘清仓/本轮锁定）→ 到期 720h / 台阶止损 / 信号退出 / 固定止损。
    # 止损逐根 15m 检查、止损优先；开盘穿 stop 用**更差 open**；台阶止损在**下一根 15m 开盘**生效。
    exits = []
    drawdown = {}
    for arm in ARMS:
        # ② 回撤锁定（先做，清仓后下面的止损扫描自然无仓可扫）
        try:
            drawdown[arm] = PE.enforce_drawdown(arm, T=T, log=log)
        except Exception as exc:                            # noqa: BLE001
            log(f"[paper] 回撤检查 {arm} 失败：{type(exc).__name__}: {exc}")
            drawdown[arm] = {"error": f"{type(exc).__name__}: {exc}"}
        con = L.paper_conn(arm)
        try:
            positions = [dict(p) for p in PE.open_positions(con, arm)]
        finally:
            con.close()
        if not positions:
            continue
        by_sym = {}
        for p in positions:
            by_sym.setdefault(p["symbol"], []).append(p)
        exit_map = {}
        for sym in by_sym:
            try:
                exit_map[sym] = _arm_exits(sym, arm, resolved[arm], T)
            except Exception as exc:                    # noqa: BLE001
                log(f"[paper] exits {arm} {sym} 失败：{type(exc).__name__}: {exc}")
                exit_map[sym] = ([], [])
        for sym, plist in by_sym.items():
            # **行情库不可用时只跳过该币的退出判定**，不让整轮 `advance_paper` 抛崩
            # （2026-10-14 加固；与上面 snapshot 同一原则）。
            try:
                mkt = L.market_conn()
            except Exception as exc:                        # noqa: BLE001
                log(f"[paper] exits {arm} {sym} 跳过（行情库不可用）：{type(exc).__name__}: {exc}")
                continue
            try:
                arm_exits, bars_1h = exit_map.get(sym) or ([], [])
                for p in plist:
                    hit = _scan_position_exit(p, mkt=mkt, arm_exits=arm_exits,
                                              bars_1h=bars_1h, params=params, T=T)
                    if hit is None:
                        continue
                    raw, bot, bct, why, exit_ts, dctx = hit
                    r = PE.close_at_price(arm, int(p["position_id"]), raw_price=raw, now_ms=T,
                                          reason=why, bar_open_time=bot, bar_close_time=bct,
                                          exit_ts=exit_ts, decision_context=dctx)
                    exits.append(r)
            finally:
                mkt.close()
    log(f"[paper] exits={len(exits)} drawdown={ {a: (v or {}).get('actions') for a, v in drawdown.items()} }")
    # **逐臂单独 try/except（2026-10-14 加固）**：`PE.snapshot` 需要读行情库估市值。
    # 行情库缺失/不可打开时**只让该臂快照降级**，绝不能把整轮 `advance_paper` 抛崩
    # （否则开仓/挂单/资金费/退出的既有结果全部丢失，且 worker 无法常驻）。
    snap = {}
    for a in ARMS:
        try:
            snap[a] = {k: v for k, v in PE.snapshot(a).items()
                       if k in ("equity", "cash", "positions", "n_pending",
                                "realized_pnl_total", "fees_paid_total",
                                "funding_paid_total")}
        except Exception as exc:                            # noqa: BLE001
            log(f"[paper] snapshot {a} 降级（行情库不可用？）：{type(exc).__name__}: {exc}")
            snap[a] = {"error": f"{type(exc).__name__}: {exc}"}
    return {"opened": opened, "rejected": rejected, "exits": exits,
            "pending": pend_res, "funding": funding, "drawdown": drawdown,
            "live_start_ms": L.get_live_start_ms(), "paper_epoch_ms": epoch,
            "effective_floor_ms": floor_ms, "n_pre_watermark_display_only": n_pre,
            "switches": PE.switches(),
            "maintain_existing": MAINTAIN_EXISTING,
            "allow_new_entries": gate["allow_new_entries"],
            "exec_core": (EB.version_ids() if EB.CORE_AVAILABLE
                          else {"core_available": False, "import_error": EB.IMPORT_ERROR}),
            "snapshot": snap}


_PROC_CACHE = {}


def _reason_hist(rejected) -> dict:
    """拒单原因直方图（常驻日志可读性；不改变任何账本事实）。"""
    hist = {}
    for r in rejected or []:
        k = str(r.get("reason") or "unknown")
        hist[k] = hist.get(k, 0) + 1
    return dict(sorted(hist.items(), key=lambda kv: -kv[1]))


def con_processed(arm, uid) -> bool:
    key = (arm, uid)
    if key in _PROC_CACHE:
        return _PROC_CACHE[key]
    con = L.paper_conn(arm, ro=True)
    try:
        r = con.execute("SELECT 1 FROM paper_processed WHERE signal_uid=?", (uid,)).fetchone()
        v = bool(r)
    except Exception:                                   # noqa: BLE001
        v = False
    finally:
        con.close()
    _PROC_CACHE[key] = v
    return v


def run_once(*, T=None, limit=0, force=False, with_signals=True, with_accounts=True,
             with_paper=True, log=print) -> dict:
    t0 = time.time()
    out = {}
    if with_signals:
        out["signals"] = SE.run_cycle(T=T, limit=limit, force=force, log=log)
    if with_paper:
        out["paper"] = advance_paper(T=T, log=log)
    if with_accounts:
        try:
            out["accounts"] = LA.read_and_store()
        except Exception as exc:                        # noqa: BLE001
            out["accounts"] = {"error": f"{type(exc).__name__}: {exc}"}
    out["elapsed_s"] = round(time.time() - t0, 1)
    L.set_run("account_state", {"real_utc": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "elapsed_s": out["elapsed_s"],
                                "signals": out.get("signals", {}).get("write") if with_signals else None,
                                "symbols_total": (out.get("signals") or {}).get("symbols_total"),
                                "pool_source": (out.get("signals") or {}).get("pool_source"),
                                "pool_n": (out.get("signals") or {}).get("pool_n"),
                                "admission_min_bars": (out.get("signals") or {}).get(
                                    "admission_min_bars"),
                                "assembly_lookback_hours": (out.get("signals") or {}).get(
                                    "assembly_lookback_hours"),
                                "n_opened": len(out.get("paper", {}).get("opened", [])),
                                "n_exits": len(out.get("paper", {}).get("exits", [])),
                                "allow_new_entries": (out.get("paper", {}) or {}).get(
                                    "allow_new_entries"),
                                "maintain_existing": MAINTAIN_EXISTING,
                                "exec_core": (out.get("paper", {}) or {}).get("exec_core"),
                                "n_pending": (out.get("paper", {}).get("pending") or {}).get(
                                    "still_pending"),
                                "funding_events_this_cycle": sum(
                                    int((v or {}).get("n_new_events") or 0)
                                    for v in (out.get("paper", {}).get("funding") or {}).values()
                                    if isinstance(v, dict)),
                                "accounts": (out.get("accounts") or {}).get("results")})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--T", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-signals", action="store_true")
    ap.add_argument("--no-accounts", action="store_true")
    ap.add_argument("--no-paper", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=900)
    a = ap.parse_args()
    L.ensure_live_schema()
    for arm in ARMS:
        L.paper_conn(arm).close()
    kw = dict(T=a.T, limit=a.limit, force=a.force, with_signals=not a.no_signals,
              with_accounts=not a.no_accounts, with_paper=not a.no_paper)
    if a.loop:
        while True:
            try:
                r = run_once(**kw)
                print(json.dumps({k: v for k, v in r.items() if k != "paper"},
                                 ensure_ascii=False, default=str)[:1200], flush=True)
            except Exception as exc:                    # noqa: BLE001
                print(json.dumps({"error": type(exc).__name__, "detail": str(exc)},
                                 ensure_ascii=False), flush=True)
            time.sleep(max(60, a.interval))
    else:
        r = run_once(**kw)
        print(json.dumps(r, ensure_ascii=False, indent=1, default=str)[:6000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
