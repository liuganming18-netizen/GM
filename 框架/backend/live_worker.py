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


def _now():
    return int(time.time() * 1000)


def _arm_exits(sym, arm, resolved, T, window_hours=SE.WINDOW_HOURS):
    """冻结策略在该币上的 Donchian 退出事件（1h open_time）。

    装配长度用 `SE.ASSEMBLY_LOOKBACK_HOURS`（= 冻结策略自带 `warmup_contiguous_bars`，
    **不是准入门槛**），否则冻结策略算不出状态。
    """
    pack = W.assemble(sym, T, warm_hours=SE.ASSEMBLY_LOOKBACK_HOURS, window_hours=window_hours,
                      lookback_hours=SE.ASSEMBLY_LOOKBACK_HOURS + window_hours + 24)
    bars = pack[0]
    r = WX.run(bars, resolved, arm_id=resolved.get("registered_config_id"),
               identity=("binance", sym), require_identity=True)
    return r.get("exits") or []


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

    opened, rejected = [], []
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
                                  direction="long", atr=float(atr),
                                  now_ms=T,
                                  event_time=int(d["confirmed_at"]),
                                  available_at=int(d.get("available_at") or d["confirmed_at"]),
                                  requested_at=anchor, origin="live",
                                  epoch_ms=epoch, first_request_ms=anchor)
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

    # 退出管理
    exits = []
    for arm in ARMS:
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
                exit_map[sym] = []
        for sym, plist in by_sym.items():
            # **行情库不可用时只跳过该币的退出判定**，不让整轮 `advance_paper` 抛崩
            # （2026-10-14 加固；与上面 snapshot 同一原则）。
            try:
                mkt = L.market_conn()
            except Exception as exc:                        # noqa: BLE001
                log(f"[paper] exits {arm} {sym} 跳过（行情库不可用）：{type(exc).__name__}: {exc}")
                continue
            try:
                for p in plist:
                    entry_ts = int(p["entry_ts"])
                    stop = p["stop_price"]
                    direction = p.get("direction") or "long"
                    bars = PE.closed_15m(mkt, sym, entry_ts, T)
                    hit = None
                    for b in bars:
                        lo = PE._f(b["low"]); hi = PE._f(b["high"]); op = PE._f(b["open"])
                        if direction == "short":
                            if stop is not None and hi is not None and hi >= float(stop):
                                # 做空缺口：开盘已涨破 ⇒ 按更差开盘成交；否则按止损价
                                raw = op if (op is not None and op >= float(stop)) else float(stop)
                                hit = (raw, int(b["open_time"]), int(b["close_time"]), "stop_loss")
                                break
                        else:
                            if stop is not None and lo is not None and lo <= float(stop):
                                # 做多缺口：开盘已跌破 ⇒ 按更差开盘成交；否则按止损价
                                raw = op if (op is not None and op <= float(stop)) else float(stop)
                                hit = (raw, int(b["open_time"]), int(b["close_time"]), "stop_loss")
                                break
                    if hit is None:
                        # 信号退出：冻结 Donchian 退出事件（1h open_time）+ 已批准 15m 延迟
                        for ex in exit_map.get(sym, []):
                            ex_ot = int(ex["open_time"])
                            if ex_ot + int(params["signal_exit_delay_minutes"]) * MIN <= entry_ts:
                                continue
                            eb = PE.bar_at_or_after(mkt, sym,
                                                    ex_ot + int(params["signal_exit_delay_minutes"]) * MIN)
                            if eb is None:
                                continue
                            hit = (PE._f(eb["open"]), int(eb["open_time"]), int(eb["close_time"]),
                                   "signal_exit_donchian")
                            break
                    if hit is None:
                        held_h = (T - entry_ts) / H
                        if held_h >= float(params["max_holding_hours"]):
                            last = PE.last_closed_15m(mkt, sym)
                            if last is not None:
                                hit = (PE._f(last["close"]), int(last["open_time"]),
                                       int(last["close_time"]), "max_holding")
                    if hit is None:
                        continue
                    raw, bot, bct, why = hit
                    # 经济出场时刻与入场同轴（bar 时刻）：
                    #   · 止损/信号退出成交在该 bar 的**开盘** ⇒ 用 bar_open_time；
                    #   · 最长持仓成交在最后一根已闭合 bar 的**收盘** ⇒ 用 bar_close_time。
                    exit_ts = bct if why == "max_holding" else bot
                    r = PE.close_at_price(arm, int(p["position_id"]), raw_price=raw, now_ms=T,
                                          reason=why, bar_open_time=bot, bar_close_time=bct,
                                          exit_ts=exit_ts)
                    exits.append(r)
            finally:
                mkt.close()
    log(f"[paper] exits={len(exits)}")
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
            "pending": pend_res, "funding": funding,
            "live_start_ms": L.get_live_start_ms(), "paper_epoch_ms": epoch,
            "effective_floor_ms": floor_ms, "n_pre_watermark_display_only": n_pre,
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
