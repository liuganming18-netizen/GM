#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冻结 IS / Val / Holdout 的确切 UTC 起止（v3.1 正式开工令，覆盖上一版候选）。

v3.1 冻结区间（半开 UTC）：
    IS      = [2022-01-01T00:00:00Z, 2025-01-01T00:00:00Z)
    Val     = [2025-01-01T00:00:00Z, 2026-04-15T00:00:00Z)
    Holdout = [2026-04-15T00:00:00Z, 2026-10-15T00:00:00Z)

v3.1 执行口径（写入本配置，供回测层与核验层共同消费）：
  · 三段各自 5000 USDT、**零仓零挂单**起算；起点前仅指标预热。
  · 段末未平仓用**本段最后 15m 收盘强平**，计不利滑点、费用及应结资金费；**不用下一段开盘**。
  · 段界重置现金、挂单、持仓、执行状态及权益峰值。
  · 仅由预热形成且尚有效的信号锚点可沿用，但必须一致，且**不得产生段前开仓**。

日期只由开工令与数据边界决定，绝不按收益调整。本脚本不读库、不联网、不写行情数据。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path

UTC = dt.timezone.utc

WARMUP_1H_BARS = 240          # 信号层连续有效 1h 预热
WARMUP_ROLL_DAYS = 90         # 滚动分位（M4 OI 90 日）自身此前 90 天
WARMUP_OI_SPAN_BARS = 20      # OI 变化跨度 20 根信号 bar
INITIAL_EQUITY = 5000.0

# v3.1 正式开工令冻结区间
V31 = {
    "IS": ("2022-01-01T00:00:00Z", "2025-01-01T00:00:00Z"),
    "Val": ("2025-01-01T00:00:00Z", "2026-04-15T00:00:00Z"),
    "Holdout": ("2026-04-15T00:00:00Z", "2026-10-15T00:00:00Z"),
}


def _ms(iso: str) -> int:
    return int(dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
               .replace(tzinfo=UTC).timestamp() * 1000)


def _days(a: str, b: str) -> int:
    return (_ms(b) - _ms(a)) // 86_400_000


def build(status: str, notes: str) -> dict:
    segs = []
    for name in ("IS", "Val", "Holdout"):
        s, e = V31[name]
        segs.append({
            "name": name,
            "start_utc": s,
            "end_excl_utc": e,
            "start_ms": _ms(s),
            "end_excl_ms": _ms(e),
            "days": _days(s, e),
            "equity_start": INITIAL_EQUITY,
            "positions_at_start": 0,
            "pending_orders_at_start": 0,
            "warmup_bars_1h": WARMUP_1H_BARS,
            "warmup_roll_days": WARMUP_ROLL_DAYS,
            "warmup_oi_span_bars": WARMUP_OI_SPAN_BARS,
            "warmup_note": "起点前仅指标预热，不产生任何成交",
            "reset_on_segment_boundary": [
                "cash", "pending_orders", "positions", "execution_state", "equity_peak"],
            "carry_positions_across": False,
        })
    doc = {
        "schema": "wyckoffmaster_segments/v2_v31",
        "spec_version": "WyckoffMaster_v3.1",
        "status": status,
        "authority": "docs/WyckoffMaster_v3.1_执行补充与窗口交接_2026-10-15.md（覆盖上一版候选）",
        "rule": {
            "intervals": "半开 UTC 区间 [start, end)",
            "timezone": "UTC",
            "immutable": "日期只由开工令与数据边界决定，绝不按收益调整",
        },
        "data_boundary": {
            "hot_db": "/Users/boos/WorkBuddy AI/加密货币趋势判断系统/data/market_v2.sqlite3",
            "hot_db_note": "只读；**不是** data_hub/market_v2.sqlite3 旧路径，不得创建旧路径库",
            "archive": "/Users/boos/WorkBuddy AI/加密货币趋势判断系统/data/archive/parquet",
            "research_input": "/Users/boos/Desktop/威科夫因子共振趋势/数据/研究输入/WyckoffMaster",
            "holdout_cutoff_utc": V31["Holdout"][1],
            "holdout_cutoff_beijing": "2026-10-15 08:00 (+08)",
            "cutoff_note": ("交接核对时北京时间 01:57，Holdout 截止尚未到；终验须等该段全部 bar 闭合、"
                            "事件材料通过，不得按 01:57 截断冒充完整六个月"),
        },
        "pool": {
            "scope": "crypto_only_v1_549_historical",
            "binance": 526, "okx": 23,
            "excluded": ["ALLUSDT", "BTCDOMUSDT", "DEFIUSDT"],
            "survivor_bias_note": "549 只作库存范围；历史上市/下架身份按 PIT 清单，未知身份单列",
        },
        "segments": segs,
        "segment_end_liquidation": {
            "price": "本段最后 15m 收盘价",
            "costs": ["卖出不利滑点 0.05%", "手续费 0.05%", "应结资金费"],
            "forbidden": "不得用下一段开盘价清算",
            "reason_code": "segment_end_force_close_last_15m_close",
            "accounting": "单独列账（逐笔），不混入正常出场统计",
        },
        "segment_boundary": {
            "reset": ["cash", "pending_orders", "positions", "execution_state", "equity_peak"],
            "anchor_carry_policy": {
                "allowed": "仅由预热形成且尚有效的信号锚点",
                "must_be_consistent": True,
                "forbidden": "不得产生段前开仓（entry_ts 必须落在本段内）",
            },
        },
        "holdout_isolation": {
            "red_line": ("IS/Val 选型锁定前：不读取 Holdout 区间行情/因子数据，不运行该段，"
                         "不计算/查看该段交易、收益、信号表现"),
            "step0_scope": "Step0 只审计 IS/Val 及其预热；Holdout 审计后置到最终候选冻结之后",
            "physical_separation": "研究输入必须物理区分 IS/Val 与 Holdout；跨界月包/混合近期库不得整文件解析后过滤",
            "enforced_by": "adapters/holdout_guard.py（窗口交集即拒绝）",
        },
        "forbidden_models": {
            "norm4h_approx": "禁止作为真实历史资金费",
            "ext1h_approx": "禁止作为真实历史资金费",
            "zero_fill": "缺事件 mark 不得补 0",
            "ordinary_close": "不得用普通成交 close 伪称真实结算 mark",
            "required": "真实结算时刻 / 真实结算费率含正负 / 结算时刻真实 mark",
        },
        "notes": notes,
    }
    canon = json.dumps(doc, ensure_ascii=False, sort_keys=True).encode()
    doc["self_sha256"] = hashlib.sha256(canon).hexdigest()
    return doc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--status", default="FROZEN_V31")
    ap.add_argument("--notes", default="")
    a = ap.parse_args()
    doc = build(a.status, a.notes)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    for s in doc["segments"]:
        print(f"{s['name']:8s} {s['start_utc']} -> {s['end_excl_utc']}  ({s['days']}d)")
    print("self_sha256", doc["self_sha256"])
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
