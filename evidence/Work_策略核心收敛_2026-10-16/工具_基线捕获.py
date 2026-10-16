#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WORK-02 行为基线捕获 / 复核（确定性，不读库、不联网）。

用法
────
    # 捕获（改造前）：
    python 工具_基线捕获.py --label before --out 基线_改造前.json
    # 复核（改造后）：
    python 工具_基线捕获.py --label after --out 基线_改造后.json
    python 工具_基线比较.py 基线_改造前.json 基线_改造后.json

判据
────
`result_sha256` = 对 `run_portfolio_execution` **完整返回体**（trades / decisions /
rejected / equity_curve / reconcile / funding_gaps 全含）做稳定哈希。
逐位一致 ⇒ 架构拆分未改变任何数值行为。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for p in (str(ROOT), str(ROOT / "策略")):
    if p not in sys.path:
        sys.path.insert(0, p)

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("_w2fx", HERE / "工具_合成夹具.py")
_w2fx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_w2fx)                       # noqa: E402

from WyckoffMaster.oi_resonance_v1 import config as C   # noqa: E402
from WyckoffMaster.oi_resonance_v1 import core as CORE  # noqa: E402
from WyckoffMaster.oi_resonance_v1 import execution as EXEC  # noqa: E402


def _json_default(o):
    import numpy as _np
    if isinstance(o, (_np.integer,)):
        return int(o)
    if isinstance(o, (_np.floating,)):
        return float(o)
    if isinstance(o, (_np.bool_,)):
        return bool(o)
    raise TypeError(f"not serializable: {type(o).__name__}")


def capture() -> dict:
    fx = _w2fx.build_fixture()
    md = _w2fx.market_data(fx)
    res = EXEC.run_portfolio_execution(md, strategy_params=C.SIGNAL_FROZEN,
                                       exec_cfg=C.EXEC_FROZEN, time_unit="ms")
    sig = CORE.compute_signals(fx["signal_bars"], C.SIGNAL_FROZEN, "ms")
    out = {
        "fixture_hash": _w2fx.stable_json_hash({
            "signal_bars": fx["signal_bars"].to_dict("list"),
            "intrabar_bars": fx["intrabar_bars"].to_dict("list"),
            "funding_events": fx["funding_events"]}),
        "signal_counts": dict(sig.attrs.get("signal_counts") or {}),
        "reject_counts": dict(sig.attrs.get("reject_counts") or {}),
        "oi_quality_counts": (dict(sig["oi_resonance_quality"].value_counts())
                              if "oi_resonance_quality" in sig.columns else {}),
        "result_sha256": _w2fx.stable_json_hash(res),
        "summary": {
            "trades_count": res["trades_count"],
            "net_return_pct": res["net_return_pct"],
            "final_equity": res["final_equity"],
            "sum_fees": res["reconcile"].get("sum_fees"),
            "sum_funding_fee": res["reconcile"].get("sum_funding_fee"),
            "max_drawdown_pct": res["max_drawdown_pct"],
            "rejected_orders": len(res["rejected_orders"]),
            "decisions": len(res["decisions"]),
            "equity_points": len(res["equity_curve"]),
            "cash_identity_ok": res["reconcile"].get("cash_identity_ok"),
            "equity_identity_ok": res["reconcile"].get("equity_identity_ok"),
        },
        "identity": {
            "signal_params_hash": C.SIGNAL_FROZEN.hash(),
            "exec_params_hash": C.EXEC_FROZEN.hash(),
        },
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    doc = capture()
    doc["label"] = a.label
    p = HERE / a.out if not Path(a.out).is_absolute() else Path(a.out)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2, default=_json_default),
                 encoding="utf-8")
    print(json.dumps(doc, ensure_ascii=False, indent=2, default=_json_default))
    print("\nwritten:", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
