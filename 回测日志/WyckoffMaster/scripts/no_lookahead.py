#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无未来信息核验（引擎级）。

两项独立证据：
  A. **未来置毒**：把 poison 时刻之后的行情置毒（价格→1e9、量→1e12），
     重跑同一配置；凡 exit_time <= poison 的成交必须与基线**逐字段一致**。
     若前视存在，未来毒值必然改变过去的成交/拒单 ⇒ 该测试会失败。
  B. **截断不变**：对每个信号 bar i，用前缀 bars[:i+1] 重算信号，
     master_entry/kind/atr/struct_low 必须与全历史计算**逐位一致**。
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

WM = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WM))
sys.path.insert(0, str(WM.parent.parent / "策略"))

from engine import paths as P            # noqa: E402
from engine import loader as LD          # noqa: E402
from engine.engine import Backtest, LevelSpec  # noqa: E402
from WyckoffMaster import core as WC     # noqa: E402
from WyckoffMaster.params import FROZEN  # noqa: E402

UTC = dt.timezone.utc


def ms(s):
    return int(dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
               .timestamp() * 1000)


def read_positions(path):
    con = sqlite3.connect(str(path)); con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT position_id,symbol,entry_time,entry_price,quantity,exit_time,exit_price,"
        "exit_reason,entry_fee,exit_fee,funding,gross_pnl,net_pnl FROM positions").fetchall()]
    con.close()
    return rows


def future_poison(seg, tf, start, end, symbols, poison, outdir):
    spec = LevelSpec("M2", "three_buy_points", "structural", "none", "quote_vol_30d_median", "M1")
    sd = LD.SegmentData(seg)
    outdir.mkdir(parents=True, exist_ok=True)
    base = outdir / "poison_base.sqlite3"
    pois = outdir / "poison_test.sqlite3"
    bt = Backtest(seg_data=sd, spec=spec, timeframe=tf, start_ms=start, end_ms=end,
                  run_id="nl_base", ledger_path=base, symbols=symbols)
    bt.run(); bt.led.close()
    bt2 = Backtest(seg_data=sd, spec=spec, timeframe=tf, start_ms=start, end_ms=end,
                   run_id="nl_poison", ledger_path=pois, symbols=symbols, poison_after_ms=poison)
    bt2.run(); bt2.led.close()
    b = read_positions(base); p = read_positions(pois)
    b_cut = {r["position_id"]: r for r in b if r["exit_time"] and r["exit_time"] <= poison}
    p_cut = {r["position_id"]: r for r in p if r["exit_time"] and r["exit_time"] <= poison}
    mismatch = []
    for pid, rb in b_cut.items():
        rp = p_cut.get(pid)
        if rp is None:
            mismatch.append({"position_id": pid, "issue": "missing_in_poisoned"})
            continue
        for k in ("symbol", "entry_time", "entry_price", "quantity", "exit_time", "exit_price",
                  "exit_reason", "entry_fee", "exit_fee", "funding", "gross_pnl", "net_pnl"):
            if rb[k] != rp[k]:
                mismatch.append({"position_id": pid, "field": k, "base": rb[k], "poison": rp[k]})
    return {"poison_after_utc": dt.datetime.fromtimestamp(poison / 1000, UTC).isoformat(),
            "n_base_total": len(b), "n_poison_total": len(p),
            "n_compared": len(b_cut), "n_mismatch": len(mismatch),
            "ok": len(mismatch) == 0, "mismatches": mismatch[:10]}


def truncation_invariance(seg, tf, symbols, sample=6, idx_step=137):
    sd = LD.SegmentData(seg)
    bad = []
    checked = 0
    for s in symbols[:sample]:
        sig = sd.ohlcv(s, tf)
        if sig is None:
            continue
        bars = [{"open_time": int(sig["open_time"][i]), "close_time": int(sig["close_time"][i]),
                 "open": float(sig["open"][i]), "high": float(sig["high"][i]),
                 "low": float(sig["low"][i]), "close": float(sig["close"][i]),
                 "volume": float(sig["volume"][i]), "quote_volume": float(sig["quote_volume"][i])}
                for i in range(len(sig["open_time"]))]
        inst = sd.instrument(s)
        full = WC.compute_signals(bars, FROZEN, entry_mode="three_buy_points",
                                  stop_mode="structural", price_step=inst.tick_size)
        n = len(bars)
        for i in range(60, n, idx_step):
            pre = WC.compute_signals(bars[:i + 1], FROZEN, entry_mode="three_buy_points",
                                     stop_mode="structural", price_step=inst.tick_size)
            checked += 1
            for k in ("master_entry",):
                if bool(pre.__getattribute__(k)[i]) != bool(full.__getattribute__(k)[i]):
                    bad.append({"symbol": s, "bar": i, "field": k})
            for k in ("atr", "struct_low", "ref_stop_price"):
                a = pre.__getattribute__(k)[i]; b = full.__getattribute__(k)[i]
                if not (np.isnan(a) and np.isnan(b)) and a != b:
                    bad.append({"symbol": s, "bar": i, "field": k, "pre": float(a), "full": float(b)})
            if pre.signal_kind[i] != full.signal_kind[i]:
                bad.append({"symbol": s, "bar": i, "field": "kind"})
    return {"checked_points": checked, "n_mismatch": len(bad), "ok": len(bad) == 0,
            "mismatches": bad[:10]}


def main():
    seg = "Val"; tf = "1h"
    start = ms("2025-01-01T00:00:00Z"); end = ms("2025-04-01T00:00:00Z")
    poison = ms("2025-02-01T00:00:00Z")
    sd = LD.SegmentData(seg)
    symbols = sd.symbols(tf)[:30]
    outdir = WM / "ledgers" / "noloookahead"
    A = future_poison(seg, tf, start, end, symbols, poison, outdir)
    B = truncation_invariance(seg, tf, symbols)
    rep = {"schema": "wyckoffmaster_no_lookahead/v1",
           "segment": seg, "timeframe": tf, "window": [start, end],
           "A_future_poison": A, "B_truncation_invariance": B,
           "overall_ok": A["ok"] and B["ok"]}
    (WM / "state" / "no_lookahead.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                                                    encoding="utf-8")
    print(json.dumps({k: (v if k != "A_future_poison" else {kk: v[kk] for kk in
                                                           ("n_compared", "n_mismatch", "ok")})
                      for k, v in rep.items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
