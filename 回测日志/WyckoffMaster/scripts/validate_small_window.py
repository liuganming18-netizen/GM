#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小型 IS/Val 窗口验证（完整阶梯前的准入验证）。

内容：
  · 在小型 IS / Val 窗口上跑 M0→M1→M2→(条件)M2b→M3_BE/M3_Step→M4
    （真数据、任务二共用执行核心、**跨 IS+Val 晋级驱动**的配置继承）；
  · 每个账本做**逐笔对账**（毛利/费用/资金费/净利/现金流 + 汇总一致性）；
  · **因果时钟不变量**：每笔入场 = 信号收盘 + 1ms + 15m 的首个 15m 开盘；
  · **段边界不变量**：无段外持仓；**资金费来源不变量**：全部真实 `event_mark`；
  · 配置继承 + 消费证明（每层实际使用的 entry/stop/trail/rank 与买点计数）；
  · M2 逐类型超限拒单统计 + M2b 条件触发（IS+Val 合并，容量无关固定分母）；
  · 全部结果绑定 代码/配置/数据/核心 版本 SHA。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

WM = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WM))
sys.path.insert(0, str(WM.parent.parent / "策略"))

from engine import paths as P             # noqa: E402
from engine import loader as LD           # noqa: E402
from engine import ladder as LAD          # noqa: E402
from engine import reconcile as RC        # noqa: E402
from engine.m4 import make_m4_rank_provider  # noqa: E402
from WyckoffMaster import experiments as EX  # noqa: E402
from WyckoffMaster import execution as X  # noqa: E402
from WyckoffMaster.params import FROZEN   # noqa: E402

UTC = dt.timezone.utc
EV = WM.parent.parent / "evidence" / "Work_阶梯回测_2026-10-15"


def ms(s):
    return int(dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
               .timestamp() * 1000)


def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def bundle_sha(files):
    h = hashlib.sha256()
    for p in sorted(files):
        h.update(p.name.encode())
        h.update(sha_file(p).encode())
    return h.hexdigest()


def clock_and_funding_invariants(path, seg_start_ms, seg_end_ms, trail_mode="none"):
    """因果时钟 + 段边界 + 资金费来源 + 逐笔复算 不变量。

    `stop_not_below_entry`：仅对**未启用台阶锁利**的层检查（初始止损必在入场价下方）。
    启用台阶/保本的层（naive_be / step_swing）止损会被**上移**到入场价之上（这正是锁利目的），
    故不作为违规。
    """
    con = sqlite3.connect(str(path)); con.row_factory = sqlite3.Row
    sig = {(r["symbol"], r["entry_bar_time"]): int(r["signal_time"])
           for r in con.execute("SELECT symbol, entry_bar_time, signal_time FROM signals")}
    pos = con.execute("SELECT * FROM positions").fetchall()
    fund_src = {}
    for r in con.execute("SELECT mark_source, COUNT(*) c FROM funding GROUP BY mark_source"):
        fund_src[r["mark_source"]] = r["c"]
    n = len(pos)
    clock_bad = 0; bounds_bad = 0; recompute_bad = 0; stop_bad = 0
    for p in pos:
        st = sig.get((p["symbol"], p["entry_time"]))
        if st is None or X.next_15m_open_at_or_after(st + 1 + 900_000) != int(p["entry_time"]):
            clock_bad += 1
        if not (seg_start_ms <= int(p["entry_time"]) < seg_end_ms):
            bounds_bad += 1
        q = float(p["quantity"]); ep = float(p["entry_price"]); xp = float(p["exit_price"])
        g = (xp - ep) * q
        if abs(g - float(p["gross_pnl"])) > 1e-9 * max(1.0, abs(g)):
            recompute_bad += 1
        net = g - float(p["entry_fee"]) - float(p["exit_fee"]) + float(p["funding"])
        if abs(net - float(p["net_pnl"])) > 1e-9 * max(1.0, abs(net)):
            recompute_bad += 1
        if trail_mode == "none" and float(p["stop_price"]) >= float(p["entry_price"]):
            stop_bad += 1
    con.close()
    return {"n_positions": n, "trail_mode": trail_mode,
            "entry_clock_violations": clock_bad,
            "segment_bounds_violations": bounds_bad,
            "per_position_recompute_bad": recompute_bad,
            "stop_not_below_entry": stop_bad,
            "stop_check_applicable": trail_mode == "none",
            "funding_mark_source": fund_src,
            "funding_missing_rows": fund_src.get("missing", 0),
            "ok": clock_bad == 0 and bounds_bad == 0 and recompute_bad == 0 and stop_bad == 0}


def main():
    t0 = time.time()
    windows = {"IS": (ms("2022-01-01T00:00:00Z"), ms("2022-04-01T00:00:00Z")),
               "Val": (ms("2025-01-01T00:00:00Z"), ms("2025-04-01T00:00:00Z"))}
    tf = "1h"

    engine_files = list((WM / "engine").glob("*.py"))
    strat_files = list((WM.parent.parent / "策略" / "WyckoffMaster").glob("*.py"))
    code_sha = bundle_sha(engine_files + strat_files)
    config_sha = EX.experiment_config_hash()
    input_sha = P.input_manifest_sha()
    iface = X.interface()

    led_dir = WM / "ledgers"
    led_dir.mkdir(parents=True, exist_ok=True)
    out = {"schema": "wyckoffmaster_small_window_validation/v3",
           "generated_utc": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "timeframe": tf, "params_identity": FROZEN.identity(),
           "code_sha256": code_sha, "config_sha256": config_sha, "input_manifest_sha256": input_sha,
           "core_version": X.CORE_VERSION, "strategy_version": X.STRATEGY_VERSION,
           "ledger_model_version": X.LEDGER_MODEL_VERSION,
           "exec_config_hash": iface["config_hash"],
           "strategy_params_hash": iface["strategy_params_hash"],
           "execution_core_module": "策略/WyckoffMaster/execution.py",
           "production": P.assert_production_untouched(), "segments": {}}

    segments, rank_by_seg = {}, {}
    for seg, (s_ms, e_ms) in windows.items():
        sd = LD.SegmentData(seg)
        segments[seg] = (sd, s_ms, e_ms)
        rank_by_seg[seg] = make_m4_rank_provider(sd, tf)

    res = LAD.run_ladder(segments=segments, tf=tf, run_tag="small", led_dir=led_dir,
                         code_sha=code_sha, config_sha=config_sha, input_sha=input_sha,
                         m2_collect=True, rank_provider_by_seg=rank_by_seg)

    recon, inv = {}, {}
    for seg in windows:
        s_ms, e_ms = windows[seg]
        recon[seg], inv[seg] = {}, {}
        for lv in res["results"]:
            lp = led_dir / f"ladder_small_{tf}_{seg}_{lv}.sqlite3"
            recon[seg][lv] = RC.reconcile_ledger(lp)
            inv[seg][lv] = clock_and_funding_invariants(
                lp, s_ms, e_ms, trail_mode=res["specs"][lv]["trail_mode"])
        out["segments"][seg] = {
            "window": [s_ms, e_ms],
            "window_utc": [dt.datetime.fromtimestamp(s_ms / 1000, UTC).isoformat(),
                           dt.datetime.fromtimestamp(e_ms / 1000, UTC).isoformat()],
            "n_symbols_used": res["n_symbols"][seg],
            "results": {lv: {"trades": res["results"][lv][seg]["trades"],
                             "net": res["results"][lv][seg]["net"],
                             "gross": res["results"][lv][seg]["gross"],
                             "fees": res["results"][lv][seg]["fees"],
                             "funding": res["results"][lv][seg]["funding"],
                             "max_dd": res["results"][lv][seg]["max_dd"],
                             "halted": res["results"][lv][seg]["halted"],
                             "rejects": res["results"][lv][seg]["rejects"],
                             "kinds": res["results"][lv][seg]["consumed"]["kinds"],
                             "counts": res["results"][lv][seg]["counts"]}
                        for lv in res["results"]},
            "reconcile": {lv: {"ok": recon[seg][lv]["ok"],
                               "n_positions": recon[seg][lv]["n_positions"],
                               "n_mismatch": recon[seg][lv]["n_mismatch"],
                               "n_funding_rows": recon[seg][lv]["n_funding_rows"],
                               "funding_blocked_rows": recon[seg][lv]["funding_blocked_rows"]}
                          for lv in recon[seg]},
            "invariants": inv[seg],
        }
        print(f"[{seg}] done")

    out["specs"] = res["specs"]
    out["inheritance"] = res["inheritance"]
    out["consumed"] = res["consumed"]
    out["decisions"] = res["decisions"]
    out["levels_run"] = res["levels_run"]
    out["m2_over_limit"] = res["m2_over_limit"]
    out["m2_candidates_n"] = res["m2_candidates_n"]

    out["elapsed_sec"] = round(time.time() - t0, 1)
    out["all_reconcile_ok"] = all(v["ok"] for s in out["segments"].values()
                                  for v in s["reconcile"].values())
    out["all_invariants_ok"] = all(v["ok"] for s in out["segments"].values()
                                   for v in s["invariants"].values())
    out["all_inheritance_ok"] = all(v["ok"] for v in out["inheritance"].values())
    out["all_ok"] = (out["all_reconcile_ok"] and out["all_invariants_ok"]
                     and out["all_inheritance_ok"])
    EV.mkdir(parents=True, exist_ok=True)
    (WM / "state" / "small_window_validation.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    (EV / "20_小型窗口验证_v31.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("reconcile =", out["all_reconcile_ok"], "invariants =", out["all_invariants_ok"],
          "inheritance =", out["all_inheritance_ok"], "ALL =", out["all_ok"],
          "elapsed", out["elapsed_sec"])


if __name__ == "__main__":
    main()
