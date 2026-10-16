#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WORK-02 · 行为一致性核验（确定性，不读库、不联网）。

判据
────
改造前（单体 `execution.py`）的完整结果体哈希 = `BASELINE_BEFORE_SHA`
（记录于 `基线_改造前.json`，由 `工具_基线捕获.py` 在**任何改动之前**产出）。

改造后（`execution.py` 变成 `TradingCore` 驱动）重新跑同一夹具，**剥离已知新增键**后
再哈希，必须与 `BASELINE_BEFORE_SHA` **逐位相同**：

    剥离的键 = 新增契约产物，不是行为：
      · close_open_positions_at_end / close_open_positions_at_end_source   （W2-3 显式开关）
      · trading_core                                                       （W2-6..10 契约块）

任一不符 ⇒ 说明「抽 Shared Core」改变了数值行为 ⇒ FAIL。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for p in (str(ROOT), str(ROOT / "策略")):
    if p not in sys.path:
        sys.path.insert(0, p)

_spec = importlib.util.spec_from_file_location("_w2fx", HERE / "工具_合成夹具.py")
_w2fx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_w2fx)

from WyckoffMaster.oi_resonance_v1 import config as C   # noqa: E402
from WyckoffMaster.oi_resonance_v1 import execution as EXEC  # noqa: E402

NEW_KEYS = ("close_open_positions_at_end", "close_open_positions_at_end_source",
            "trading_core")


def main() -> int:
    before = json.loads((HERE / "基线_改造前.json").read_text(encoding="utf-8"))
    baseline_sha = before["result_sha256"]

    fx = _w2fx.build_fixture()
    res = EXEC.run_portfolio_execution(_w2fx.market_data(fx),
                                       strategy_params=C.SIGNAL_FROZEN,
                                       exec_cfg=C.EXEC_FROZEN, time_unit="ms")
    present_new = [k for k in NEW_KEYS if k in res]
    stripped = {k: v for k, v in res.items() if k not in NEW_KEYS}
    now_sha = _w2fx.stable_json_hash(stripped)

    print("=" * 72)
    print("WORK-02 行为一致性核验（抽 Shared Trading Core）")
    print("=" * 72)
    print(f"改造前 result_sha256（完整结果体）: {baseline_sha}")
    print(f"改造后 剥离新增键后 hash         : {now_sha}")
    print(f"剥离掉的新增键                   : {present_new}")
    print()

    # 逐笔/逐决策的独立对照（不依赖哈希）
    checks = []
    for k in ("trades_count", "net_return_pct", "final_equity", "max_drawdown_pct"):
        checks.append((k, res.get(k)))
    checks.append(("reconcile.sum_fees", res["reconcile"].get("sum_fees")))
    checks.append(("reconcile.sum_funding_fee", res["reconcile"].get("sum_funding_fee")))
    checks.append(("reconcile.cash_identity_ok", res["reconcile"].get("cash_identity_ok")))
    checks.append(("reconcile.equity_identity_ok", res["reconcile"].get("equity_identity_ok")))
    checks.append(("decisions", len(res["decisions"])))
    checks.append(("rejected_orders", len(res["rejected_orders"])))
    checks.append(("equity_points", len(res["equity_curve"])))
    for k, v in checks:
        print(f"  {k:<34}: {v}")

    # 契约产物存在性（Gate W2-E/F/G/H 的证据）
    tc = res.get("trading_core") or {}
    print()
    print("契约产物（新增，不参与行为判据）:")
    print(f"  orders_count                     : {tc.get('orders_count')}")
    print(f"  registry_unique_keys             : {tc.get('registry_unique_keys')}")
    print(f"  ledger_events_total              : {tc.get('ledger_events_total')}")
    print(f"  ledger_event_counts              : {tc.get('ledger_event_counts')}")
    print(f"  fill_adapter.kind                : {(tc.get('fill_adapter') or {}).get('kind')}")
    print(f"  policy.close_open_positions_at_end: "
          f"{(tc.get('policy') or {}).get('close_open_positions_at_end')}")

    ok = (now_sha == baseline_sha)
    print()
    print("结果:", "PASS —— 剥离新增键后与改造前逐位一致" if ok
          else "FAIL —— 数值行为发生了变化（必须定位）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
