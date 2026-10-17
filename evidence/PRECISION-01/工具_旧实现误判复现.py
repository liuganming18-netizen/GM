# -*- coding: utf-8 -*-
"""PRECISION-01 · 用合成证据复现「旧实现的 verified 放行错误」。

为什么要有这个脚本
──────────────────
真实证据目录里只有两份 serverTime 几乎相同的快照，旧实现的 `verified` 分支
根本走不到 —— 于是「修好了没」无法用真实数据证明。这里用三组**可控**的合成
快照，把旧实现的三种错误摆出来，同一批文件喂给新实现做对照。

三组场景
────────
A 两端相同、中间改过又改回：
   旧实现只比较「窗口前快照」与「全局最新快照」，直接判 verified（错）。
B 窗口之后又改了一次规格：
   旧实现把 2025 年的规格数值套到 2024 年的窗口上（数值错，结论偶然对）。
C 证据齐备（包围 + 中间一致 + 变更证据覆盖整窗）：
   新旧都应判 verified —— 用来证明 verified 没有被改成死代码。

输出：evidence/PRECISION-01/旧实现误判复现.md / .json
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVID = ROOT / "evidence" / "PRECISION-01"
FIX = EVID / "合成证据"

WS = 1704067200000      # 2024-01-01
WE = 1719705600000      # 2024-06-30
T_BEFORE = 1701388800000    # 2023-12-01
T_MID = 1709251200000       # 2024-03-01
T_AFTER = 1722470400000     # 2024-08-01
T_LATER = 1735689600000     # 2025-01-01
SYM = "ACEUSDT"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _sym(tick: str) -> dict:
    return {"symbol": SYM, "pricePrecision": 6, "quantityPrecision": 2, "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": tick,
         "minPrice": "0.000010", "maxPrice": "2000"},
        {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01",
         "maxQty": "2000000"},
        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.01", "minQty": "0.01",
         "maxQty": "600000"},
        {"filterType": "MIN_NOTIONAL", "notional": "5"},
    ]}


def _iso(ms: int) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(
        ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def _write(d: Path, ms: int, tick: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    doc = {"serverTime": ms, "symbols": [_sym(tick)]}
    (d / f"exchangeInfo_futures_{_iso(ms)}.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def _build() -> dict:
    for p in (FIX,):
        if p.exists():
            for f in p.rglob("*.json"):
                f.unlink()
    # A：两端相同，中间改过又改回
    a = FIX / "A_两端相同中间变更"
    _write(a, T_BEFORE, "0.000010")
    _write(a, T_MID, "0.001000")
    _write(a, T_AFTER, "0.000010")
    # B：窗口后（2025-01）又改了一次
    b = FIX / "B_窗口后改规格"
    _write(b, T_BEFORE, "0.000010")
    _write(b, T_AFTER, "0.000010")
    _write(b, T_LATER, "0.001000")
    # C：证据齐备
    c = FIX / "C_证据齐备"
    _write(c, T_BEFORE, "0.000010")
    _write(c, T_MID, "0.000010")
    _write(c, T_AFTER, "0.000010")
    import hashlib
    sha = hashlib.sha256(b"prc01-fixture-announcement").hexdigest()
    (c / "spec_changes_fixture.json").write_text(json.dumps({"records": [{
        "kind": "no_change", "symbol": SYM, "from_ms": WS, "to_ms": WE,
        "fields": ["price_step", "qty_step", "min_qty", "max_qty", "min_notional"],
        "source_url": "https://www.binance.com/zh-CN/support/announcement/detail/fixture",
        "evidence_sha256": sha,
        "note": "合成夹具：用于验证 verified 分支仍可达",
    }]}, ensure_ascii=False), encoding="utf-8")
    return {"A_两端相同中间变更": a, "B_窗口后改规格": b, "C_证据齐备": c}


def main() -> int:
    old = _load(EVID / "before" / "instrument_spec_orig.py", "prc01_old_spec2")
    new = _load(ROOT / "策略" / "WyckoffMaster" / "event_trend_v0_1" / "instrument_spec.py",
                "prc01_new_spec2")
    dirs = _build()

    rows = []
    for name, d in dirs.items():
        o = old.resolve_instrument_spec(SYM, window_start_ms=WS, window_end_ms=WE,
                                        contracts_dir=d)
        n = new.resolve_instrument_spec(SYM, window_start_ms=WS, window_end_ms=WE,
                                        contracts_dir=d)
        rows.append({
            "scenario": name,
            "dir": str(d.relative_to(ROOT)),
            "old": {"applicability": o.applicability,
                    "formal_pass_allowed": o.formal_pass_allowed,
                    "price_step": o.price_step, "note": o.applicability_note},
            "new": {"applicability": n.applicability,
                    "formal_pass_allowed": n.formal_pass_allowed,
                    "price_step": n.price_step,
                    "reason": (n.coverage or {}).get("reason"),
                    "note": n.applicability_note},
            "verdict": ("旧误判已被拦住"
                        if o.formal_pass_allowed and not n.formal_pass_allowed
                        else "数值被修正" if o.price_step != n.price_step
                        else "一致"),
        })

    (EVID / "旧实现误判复现.json").write_text(
        json.dumps({"rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# PRECISION-01 · 旧实现 verified 放行错误复现",
        "",
        "同一批合成快照分别喂给「修复前」与「修复后」实现，窗口固定 "
        f"`[{WS}, {WE}]`（2024-01-01 → 2024-06-30），标 `{SYM}`。",
        "夹具可复跑：`工具_旧实现误判复现.py`（会重建 `合成证据/`）。",
        "",
        "| 场景 | 旧：结论 | 旧：正式通过 | 旧：price_step | 新：结论 | 新：正式通过 | 新：理由码 | 新：price_step | 判定 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        lines.append(
            f"| {r['scenario']} | {r['old']['applicability']} | {r['old']['formal_pass_allowed']} | "
            f"{r['old']['price_step']} | {r['new']['applicability']} | "
            f"{r['new']['formal_pass_allowed']} | `{r['new']['reason']}` | "
            f"{r['new']['price_step']} | {r['verdict']} |")
    lines += ["", "## 逐条说明", ""]
    for r in rows:
        lines += [f"### {r['scenario']}", "",
                  f"- 旧：{r['old']['note']}",
                  f"- 新：{r['new']['note']}", ""]
    lines += [
        "## 读法",
        "",
        "- **A 场景**是这次修的核心：两端快照一模一样，但 2024-03 确实把 tick 改成过 1e-3 又改回。"
        "旧实现只看「窗口前快照 vs 全局最新快照」，于是判 `verified` —— 等于宣称"
        "「整个窗口都用 1e-5」，而事实是窗口里有一段用的是 1e-3。修复后该场景被 "
        "`spec_changed_inside_span` 拦住。",
        "- **B 场景**说明「快照选择」也是错的：旧实现把 2025 年才生效的 tick 套到 2024 年窗口上，"
        "修复后锚点取「窗口结束前最近的一份」，数值回到 1e-5。",
        "- **C 场景**是反证：证据真的齐备时，新实现照样判 `verified` —— 不是把 verified 变成死代码。",
        "",
    ]
    (EVID / "旧实现误判复现.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(rows, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
