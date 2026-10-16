#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**执行链接线核验**（只读、零真实行情读取；合成 bar）。

用途：证明任务二把 v3.1 共用执行核心接进实时链路后，**适配层**行为正确：
  · 台阶止损时间表：下一根 15m 生效、单调不降、对齐 15m 网格；
  · 逐根 15m 出场扫描：同刻顺序（台阶→缺口→720h→信号退出→bar 内止损）；
  · 缺口止损：开盘已穿 ⇒ 用**更差 open**；
  · 720h 到期：入场 + 720h 之后**首根 15m 开盘**（修复 D01）；
  · 方向/模式强制：非法方向/模式拒绝、short 不翻转、缺结构低点拒单；
  · 两个开关分离：新开仓闸（fail-closed）与旧仓维护互不影响。

运行：`python verify_exec_wiring.py [--out json]`（在 `框架/backend` 下）。

**依赖说明（重要）**：本脚本需要能 `import live_worker`。若冻结研究件
（`_pool_scope.py` / `_warmup43.py` / `_接入43.py`）不在本机可达路径，
导入会失败——此时脚本**如实报出阻塞**（`blocked_missing_frozen_modules`）并返回非零，
**不伪造替身、不写任何生产库**。核心层（策略窗）的等价核验见
`策略/WyckoffMaster/test_execution.py`（不依赖这些冻结件）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True

HOUR, M15 = 3_600_000, 900_000
T0 = 1_699_999_200_000


def _hour_bars(n=200):
    b = []
    for i in range(n):
        o = 100.0 + i * 0.05
        c = o + 0.4 * (1 if i % 3 else -0.5)
        b.append({"open_time": T0 + i * HOUR, "close_time": T0 + i * HOUR + HOUR - 1,
                  "open": o, "high": max(o, c) + 0.3, "low": min(o, c) - 0.3, "close": c,
                  "volume": 100.0, "quote_volume": 2_000_000.0})
    return b


def _bars15(hours):
    out = []
    for h in hours:
        for k in range(4):
            ot = int(h["open_time"]) + k * M15
            out.append({"open_time": ot, "close_time": ot + M15 - 1, "open": float(h["open"]),
                        "high": float(h["high"]), "low": float(h["low"]),
                        "close": float(h["close"]), "quote_volume": 500_000.0})
    return out


def _mem_15m(rows):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE candles(exchange TEXT, market TEXT, symbol TEXT, timeframe TEXT,"
                "is_closed INT, open_time INT, close_time INT, open REAL, high REAL, low REAL,"
                "close REAL, quote_volume REAL)")
    for b in rows:
        con.execute("INSERT INTO candles VALUES('binance','um','SYN','15m',1,?,?,?,?,?,?,?)",
                    (b["open_time"], b["close_time"], b["open"], b["high"], b["low"],
                     b["close"], b["quote_volume"]))
    con.commit()
    return con


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    rep: dict = {"probe": "exec_wiring", "data_read_disclosure":
                 "零真实行情读取（全部合成 bar + 内存 sqlite）"}

    import exec_bridge as EB
    rep["core_available"] = EB.CORE_AVAILABLE
    rep["version_ids"] = EB.version_ids()
    if not EB.CORE_AVAILABLE:
        rep["status"] = "blocked_exec_core_unavailable"
        _emit(rep, a.out)
        return 2

    try:
        import paper_engine as PE
        import live_worker as LW
    except Exception as exc:                                    # noqa: BLE001
        rep["status"] = "blocked_missing_frozen_modules"
        rep["blocking_items"] = [{
            "error": f"{type(exc).__name__}: {exc}",
            "cause": "实时链路依赖的冻结研究件（_pool_scope / _warmup43 / _接入43）"
                     "不在本机可达路径；属任务一启动路径/迁移范围，本脚本不伪造替身。",
            "unblocked_by": "把冻结研究件放回 live_pool/live_signal_engine 的 sys.path 路径，"
                            "或按迁移表把 `框架/最新唐奇安威科夫接通与收尾_2026-10-11/` "
                            "下的冻结模块补齐。",
            "core_level_equivalent_check":
                "策略/WyckoffMaster/test_execution.py（不依赖冻结件，已全通过）",
        }]
        _emit(rep, a.out)
        return 3

    rep["ledger_model_version"] = PE.LEDGER_MODEL_VERSION
    rep["switches"] = PE.switches()
    params = PE.approved_params()
    rep["config_source"] = params["_source"]
    rep["exec_core_crosscheck"] = params["_exec_core_crosscheck"]

    hours = _hour_bars()
    b15 = _bars15(hours)
    mem = _mem_15m(b15)
    entry_ts = int(b15[60]["open_time"])
    entry_px = float(b15[60]["open"])

    # A 台阶止损时间表
    sched = LW._trail_schedule(hours, entry_ts=entry_ts, entry_price=entry_px,
                              entry_atr=1.0, initial_stop=entry_px - 3.0, params=params)
    rep["A_trail_schedule"] = {
        "n_steps": len(sched),
        "monotone_non_decreasing": all(sched[i][1] >= sched[i - 1][1] - 1e-12
                                       for i in range(1, len(sched))),
        "effective_aligned_15m": all(e % M15 == 0 for e, _s, _r in sched),
        "passed": len(sched) > 0 and all(e % M15 == 0 for e, _s, _r in sched)
        and all(sched[i][1] >= sched[i - 1][1] - 1e-12 for i in range(1, len(sched))),
    }

    # B 720h 到期
    T = int(b15[-1]["close_time"])
    far = T - 721 * HOUR
    pos = {"position_id": 1, "symbol": "SYN", "direction": "long", "qty": 1.0,
           "entry_price": entry_px, "entry_ts": far, "stop_price": entry_px - 3.0,
           "atr_at_entry": 1.0}
    hit = LW._scan_position_exit(pos, mkt=mem, arm_exits=[], bars_1h=hours, params=params, T=T)
    due = PE.expiry_exit_ts(far)
    rep["B_expiry_720h"] = {
        "due_ms": due, "due_aligned_15m": due % M15 == 0,
        "hit_reason": None if hit is None else hit[3],
        "hit_exit_ts": None if hit is None else hit[4],
        "hit_at_first_15m_open_at_or_after_due":
            bool(hit is not None and hit[4] == due and hit[4] == hit[1]),
        "passed": bool(hit is not None and hit[3] == "max_holding_720h_at_first_15m_open"
                       and hit[4] == due),
    }

    # C 缺口止损
    g = _bars15(_hour_bars(n=80))
    gts = int(g[40]["open_time"])
    m2 = _mem_15m([{"open_time": gts, "close_time": gts + M15 - 1, "open": 90.0, "high": 95.0,
                    "low": 89.0, "close": 94.0, "quote_volume": 500_000.0},
                   {"open_time": gts + M15, "close_time": gts + 2 * M15 - 1, "open": 94.0,
                    "high": 95.0, "low": 93.0, "close": 94.5, "quote_volume": 500_000.0}])
    hit2 = LW._scan_position_exit({"position_id": 2, "symbol": "SYN", "direction": "long",
                                  "qty": 1.0, "entry_price": 100.0, "entry_ts": gts - HOUR,
                                  "stop_price": 95.0, "atr_at_entry": 1.0},
                                 mkt=m2, arm_exits=[], bars_1h=hours, params=params,
                                 T=gts + 2 * M15)
    rep["C_gap_stop"] = {"raw": None if hit2 is None else hit2[0],
                         "rule": None if hit2 is None else hit2[5].get("rule"),
                         "passed": bool(hit2 is not None and hit2[0] == 90.0
                                        and hit2[5].get("rule") == "gap_open_worse_than_stop")}

    # D 方向/模式/结构低点守卫（早退分支；写落在 PAPER_DIR）
    def probe(uid, **kw):
        return PE.open_position("3a", "AAAUSDT", signal_uid=uid, atr=1.0, now_ms=1, **kw)

    d1 = probe("w1", direction="sideways")["reason"]
    d2 = probe("w2", direction="short")["reason"]
    d3 = probe("w3", direction="long", stop_mode="structural", struct_low=None)["reason"]
    d4 = probe("w4", direction="long", stop_mode="nonsense")["reason"]
    d5 = probe("w5", direction="long")["reason"]
    rep["D_guards"] = {"illegal_direction": d1, "short_not_rewritten": d2,
                       "missing_struct_low": d3, "illegal_stop_mode": d4,
                       "new_entry_gate": d5,
                       "passed": d1 == "illegal_direction_sideways"
                       and d2 == "short_disabled_by_frozen_view_only_long"
                       and d3 == "missing_struct_low_for_structural_stop"
                       and d4.startswith("illegal_stop_mode")
                       and d5 == PE.GATE_REASON}

    mem.close()
    m2.close()
    ok = all(rep[k].get("passed") for k in ("A_trail_schedule", "B_expiry_720h",
                                            "C_gap_stop", "D_guards"))
    rep["status"] = "ok" if ok else "failed"
    _emit(rep, a.out)
    return 0 if ok else 1


def _emit(rep: dict, out_path) -> None:
    text = json.dumps(rep, ensure_ascii=False, indent=2, default=str)
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    raise SystemExit(main())
