#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段A·旧账本与旧信号**只读**违规审计（不改任何历史记录）。

合同依据：`docs/Codex_项目闭环复审与整改合同_2026-10-15.md` §A。

纪律：
  · **只读**打开纸面账本与实时信号库（`mode=ro`），不 UPDATE/DELETE/INSERT 任何原表；
  · 违规清单写入**独立的新旁表** `paper_legacy_audit.sqlite3`（追加式，不在原账本内）；
  · 同时导出 JSON 证据，供页面/Codex 读取。

输出：
  · evidence/Work_闭环整改_2026-10-15/old_ledger_violation_audit.json
  · <后端实现>/live/paper_legacy_audit.sqlite3（旁表 legacy_position_audit / legacy_signal_audit）
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

UTC = dt.timezone.utc
MIN = 60_000
DELAY_MS = 15 * MIN                      # 冻结规则：入场 = max(event_time, available_at, 真实首次请求) + 15m
PAPER_DIR = Path("/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/"
                 "最新唐奇安威科夫接通与收尾_2026-10-11/量价信号接入与模拟历史并行43/纸面模拟43")
LIVE_DB = Path("/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/"
               "后端实现/live/live_signals.sqlite3")
AUDIT_DB = Path("/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/"
                "后端实现/live/paper_legacy_audit.sqlite3")
OUT_JSON = Path("/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_闭环整改_2026-10-15/"
                "old_ledger_violation_audit.json")
ARMS = ("3a", "3b", "3c", "3d")


def _ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=1")
    return con


def audit_positions() -> dict:
    rows = []
    per_arm = {}
    for arm in ARMS:
        p = PAPER_DIR / f"paper_{arm}.sqlite3"
        if not p.exists():
            continue
        con = _ro(p)
        try:
            anchors = {r["event_uid"]: int(r["first_request_ms"])
                       for r in con.execute("SELECT event_uid, first_request_ms "
                                            "FROM paper_request_anchor")}
            cur = con.execute(
                "SELECT position_id, symbol, entry_ts, entry_price, qty, stop_price, atr_at_entry,"
                " signal_uid, event_time, available_at, requested_at, earliest_fill_ms,"
                " first_request_ms, epoch_ms, origin, model_version "
                "FROM paper_position WHERE status='open' AND COALESCE(voided,0)=0 "
                "ORDER BY entry_ts")
            n_open = 0
            for r in cur:
                d = dict(r)
                n_open += 1
                eco = [int(x) for x in (d.get("event_time"), d.get("available_at")) if x is not None]
                frm = d.get("first_request_ms")
                if frm is None:
                    frm = anchors.get(d.get("signal_uid"))
                req = d.get("requested_at")
                cand = eco + [int(x) for x in (frm, req) if x is not None]
                order_request_ms = max(cand) if cand else None
                entry_ts = int(d["entry_ts"]) if d.get("entry_ts") is not None else None
                delay_ms = (entry_ts - order_request_ms) if (entry_ts and order_request_ms) else None
                epoch = int(d["epoch_ms"]) if d.get("epoch_ms") is not None else None
                v = []
                if delay_ms is not None and delay_ms < DELAY_MS:
                    v.append("entry_delay_lt_15m")
                if entry_ts is not None and order_request_ms is not None and entry_ts < order_request_ms:
                    v.append("entry_before_request")
                if entry_ts is not None and epoch is not None and entry_ts < epoch:
                    v.append("entry_before_paper_epoch")
                if not str(d.get("signal_uid") or "").strip():
                    v.append("missing_signal_uid_no_event_identity")
                rows.append({
                    "arm": arm, "position_id": d["position_id"], "symbol": d["symbol"],
                    "entry_ts": entry_ts, "entry_price": d.get("entry_price"),
                    "qty": d.get("qty"), "stop_price": d.get("stop_price"),
                    "atr_at_entry": d.get("atr_at_entry"), "signal_uid": d.get("signal_uid"),
                    "event_time": d.get("event_time"), "available_at": d.get("available_at"),
                    "requested_at": d.get("requested_at"), "first_request_ms": frm,
                    "order_request_ms": order_request_ms, "delay_ms": delay_ms,
                    "epoch_ms": epoch, "origin": d.get("origin"),
                    "model_version": d.get("model_version"),
                    "violations": v,
                    "verification_status": "legacy_unverified_rule_violation" if v
                                           else "legacy_unverified",
                    "label": "旧版/未验收/规则违规" if v else "旧版/未验收",
                })
            per_arm[arm] = n_open
        finally:
            con.close()
    return {"rows": rows, "per_arm": per_arm, "n_open": sum(per_arm.values())}


def audit_signals() -> dict:
    con = _ro(LIVE_DB)
    try:
        total = con.execute("SELECT COUNT(*) FROM live_signal").fetchone()[0]
        by_dir = [dict(r) for r in con.execute(
            "SELECT direction, COUNT(*) n FROM live_signal GROUP BY direction")]
        # base_short 掩码被写为 long：structural_kind='sc' 或 payload 含 base_short
        n_sc = con.execute("SELECT COUNT(*) FROM live_signal WHERE structural_kind='sc'").fetchone()[0]
        n_mask = con.execute("SELECT COUNT(*) FROM live_signal "
                             "WHERE payload_json LIKE '%base_short%'").fetchone()[0]
        n_as_long = con.execute(
            "SELECT COUNT(*) FROM live_signal WHERE direction='long' "
            "AND (structural_kind='sc' OR payload_json LIKE '%base_short%')").fetchone()[0]
        samples = [dict(r) for r in con.execute(
            "SELECT signal_uid, symbol, direction, structural_kind, event_time, confirmed_at,"
            " executable_state FROM live_signal WHERE direction='long' AND structural_kind='sc' "
            "ORDER BY confirmed_at DESC LIMIT 5")]
        state = {}
        for r in con.execute("SELECT key, value_json, updated_at FROM live_run"):
            try:
                state[r["key"]] = {"value": json.loads(r["value_json"]), "updated_at": r["updated_at"]}
            except Exception:  # noqa: BLE001
                state[r["key"]] = {"value": r["value_json"], "updated_at": r["updated_at"]}
        return {
            "total_signals": total, "by_direction": by_dir,
            "structural_kind_sc": n_sc, "payload_base_short": n_mask,
            "short_mask_written_as_long": n_as_long,
            "violation": "base_short/sc 结构被写为 direction=long（丢失真实方向）",
            "samples": samples, "live_run": state,
        }
    finally:
        con.close()


def write_side_table(pos: dict, sig: dict, now_ms: int) -> None:
    AUDIT_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(AUDIT_DB))
    try:
        con.execute("CREATE TABLE IF NOT EXISTS legacy_position_audit("
                    "audit_run_ms INTEGER, arm TEXT, position_id INTEGER, symbol TEXT,"
                    "entry_ts INTEGER, order_request_ms INTEGER, delay_ms INTEGER,"
                    "violations_json TEXT, verification_status TEXT, label TEXT,"
                    "PRIMARY KEY(audit_run_ms, arm, position_id))")
        con.execute("CREATE TABLE IF NOT EXISTS legacy_signal_audit("
                    "audit_run_ms INTEGER, kind TEXT, n INTEGER, detail_json TEXT,"
                    "PRIMARY KEY(audit_run_ms, kind))")
        for r in pos["rows"]:
            con.execute("INSERT OR REPLACE INTO legacy_position_audit VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (now_ms, r["arm"], r["position_id"], r["symbol"], r["entry_ts"],
                         r["order_request_ms"], r["delay_ms"],
                         json.dumps(r["violations"], ensure_ascii=False),
                         r["verification_status"], r["label"]))
        con.execute("INSERT OR REPLACE INTO legacy_signal_audit VALUES(?,?,?,?)",
                    (now_ms, "short_mask_written_as_long", sig["short_mask_written_as_long"],
                     json.dumps({"structural_kind_sc": sig["structural_kind_sc"],
                                 "payload_base_short": sig["payload_base_short"]},
                                ensure_ascii=False)))
        con.execute("INSERT OR REPLACE INTO legacy_signal_audit VALUES(?,?,?,?)",
                    (now_ms, "total_live_signals", sig["total_signals"],
                     json.dumps(sig["by_direction"], ensure_ascii=False)))
        con.commit()
    finally:
        con.close()


def main() -> int:
    now_ms = int(dt.datetime.now(UTC).timestamp() * 1000)
    pos = audit_positions()
    sig = audit_signals()
    write_side_table(pos, sig, now_ms)
    n_viol_delay = sum(1 for r in pos["rows"] if "entry_delay_lt_15m" in r["violations"])
    n_viol_any = sum(1 for r in pos["rows"] if r["violations"])
    out = {
        "generated_utc": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_ms": now_ms,
        "contract": "docs/Codex_项目闭环复审与整改合同_2026-10-15.md",
        "mode": "read_only_no_mutation_of_original_ledgers",
        "frozen_rule": {"entry_delay_minutes": 15,
                        "entry_ts_floor": "max(event_time, available_at, 真实首次请求) + 15m",
                        "direction": "long_only（short_enabled=false）",
                        "max_positions": 10, "initial_equity_USDT": 5000},
        "original_ledgers_preserved": True,
        "side_table": str(AUDIT_DB),
        "positions": {
            "n_open": pos["n_open"], "per_arm": pos["per_arm"],
            "n_violation_any": n_viol_any, "n_violation_entry_delay_lt_15m": n_viol_delay,
            "rows": pos["rows"],
        },
        "signals": sig,
        "summary": {
            "n_open_positions": pos["n_open"],
            "n_positions_with_violation": n_viol_any,
            "n_entry_delay_lt_15m": n_viol_delay,
            "n_short_mask_written_as_long": sig["short_mask_written_as_long"],
            "excluded_from_official_strategy_performance": True,
            "verdict": "旧账本与旧信号保留原样，标记为 旧版/未验收/规则违规，排除正式策略表现",
        },
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out["summary"], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
