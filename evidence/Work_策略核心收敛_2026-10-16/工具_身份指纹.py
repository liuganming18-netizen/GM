#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WORK-02 · 身份指纹清单（Hash 证据）。

一次输出全部**身份类**指纹，供交付报告与后续 Work 逐位比对：
  策略版本 / 核心版本 / 执行核心版本 / signal_params_hash / exec_params_hash /
  统一 param_hash（W2-2）及其覆盖面 / 契约版本（trading_core / signal / order /
  fill / policy / risk / position / ledger / funding / pit）/ code_sha256（六维绑定源码包）。

用法（从工程根）：
  PYTHONDONTWRITEBYTECODE=1 <py> evidence/Work_策略核心收敛_2026-10-16/工具_身份指纹.py [--json 路径]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BE = ROOT / "框架" / "backend"
for p in (str(ROOT / "策略"), str(BE), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, str(p))


def collect() -> dict:
    from WyckoffMaster.oi_resonance_v1 import config as C
    from WyckoffMaster.oi_resonance_v1 import execution as EXEC
    from WyckoffMaster.oi_resonance_v1 import registry_entry as REG
    from WyckoffMaster.trading_core import contracts as CT
    from WyckoffMaster.trading_core import fill as FILL
    from WyckoffMaster.trading_core import funding as FUND
    from WyckoffMaster.trading_core import ledger as LED
    from WyckoffMaster.trading_core import order as ORD
    from WyckoffMaster.trading_core import pit as PIT
    from WyckoffMaster.trading_core import policy as POL
    from WyckoffMaster.trading_core import position as POS
    from WyckoffMaster.trading_core import risk as RISK
    from WyckoffMaster.trading_core import signal as SIG
    from WyckoffMaster.trading_core import engine as ENG

    out = {
        "strategy": {
            "strategy_id": C.STRATEGY_ID,
            "strategy_key": C.STRATEGY_KEY,
            "signal_version": C.SIGNAL_VERSION,
            "core_version": C.CORE_VERSION,
            "exec_core_version": C.EXEC_CORE_VERSION,
            "experiment_id": C.EXPERIMENT_ID,
            "signal_params_hash": C.SIGNAL_FROZEN.hash(),
            "exec_params_hash": C.EXEC_FROZEN.hash(),
            "param_hash": C.param_hash(),
            "param_hash_basis": list(C.PARAM_HASH_BASIS),
            "oi_min_resonance": C.SIGNAL_FROZEN.oi_min_resonance,
            "oi_min_resonance_status": C.oi_min_resonance_status(),
            "oi_gate_enabled": C.oi_gate_enabled(),
            "oi_publication_lag_ms": C.SIGNAL_FROZEN.oi_publication_lag_ms,
            "signal_params_field_count": len(C.SIGNAL_FROZEN.identity()),
            "exec_params_field_count": len(C.EXEC_FROZEN.identity()),
        },
        "contracts": {
            "trading_core": CT.CORE_CONTRACT_VERSION,
            "signal": SIG.SIGNAL_CONTRACT_VERSION,
            "order": ORD.ORDER_CONTRACT_VERSION,
            "fill": FILL.FILL_CONTRACT_VERSION,
            "policy": POL.POLICY_CONTRACT_VERSION,
            "risk": RISK.RISK_CONTRACT_VERSION,
            "position": POS.POSITION_CONTRACT_VERSION,
            "ledger": LED.LEDGER_CONTRACT_VERSION,
            "funding": FUND.FUNDING_CONTRACT_VERSION,
            "pit": PIT.PIT_CONTRACT_VERSION,
            "engine": ENG.ENGINE_CONTRACT_VERSION,
            "order_status_enum": [s.value for s in CT.OrderStatus],
            "ledger_event_types": list(LED.LEDGER_EVENT_TYPES),
        },
        "formal_run_gate": REG.formal_run_gate(),
    }
    try:
        import oi_resonance_bridge as BR                    # noqa: WPS433
        out["code_sha256"] = BR.code_hash()
        out["bridge_version_block"] = BR.version_block()
    except Exception as exc:                             # noqa: BLE001
        out["code_sha256"] = f"<unavailable: {type(exc).__name__}: {exc}>"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    data = collect()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"\n（已写入 {args.json}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
