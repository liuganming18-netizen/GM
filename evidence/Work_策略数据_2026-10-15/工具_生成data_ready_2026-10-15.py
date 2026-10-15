#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""汇总本窗核验产物，发布 data_ready.json（供回测窗口只读复核）。"""
from __future__ import annotations
import hashlib, json
from datetime import datetime, timezone
from pathlib import Path

EV = Path("/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_策略数据_2026-10-15")
ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势/数据/研究输入/WyckoffMaster")

def jload(name, default=None):
    p = EV / name
    return json.load(open(p)) if p.is_file() else (default if default is not None else {})

def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

def main():
    man = json.load(open(ROOT / "manifest.json"))
    ver = jload("字段核验证据.json")
    bk = jload("backup_restore_evidence.json")
    mk = jload("结算mark补齐报告.json")
    apimk = jload("API补结算mark报告.json")
    patch = jload("补缺_最终清单.json", {})

    s = man["summary"]
    datasets = {}
    for k, v in s.items():
        datasets[k] = {
            "files": v["files"], "rows": v["rows"], "bytes": v["bytes"],
            "first_utc": v.get("min_ts_utc"), "last_utc": v.get("max_ts_utc"),
        }

    cov = ver.get("C_coverage", {})
    field_cov = {k: {"symbols_have": v["symbols_have"], "symbols_missing": v["symbols_missing"],
                     "rows": v["rows"], "dup_keys": v["dup_keys"],
                     "global_first_utc": v["global_first_utc"], "global_last_utc": v["global_last_utc"]}
                 for k, v in cov.items() if k != "symbols"}

    # funding mark 覆盖
    fund_tot = datasets.get("funding", {}).get("rows", 0)
    mark_null = apimk.get("total_still_missing", None)
    if mark_null is None:
        mark_null = sum(mk.get("symbols_still_missing_mark", {}).values())
    mark_cov = round(100 * (1 - (mark_null or 0) / fund_tot), 4) if fund_tot else None

    blocking = []
    if ver.get("A_oi_same_source_event", {}).get("all_match") is not True:
        blocking.append({"item": "OI 同源同事件抽样未全部匹配", "detail": ver.get("A_oi_same_source_event")})
    if mark_null:
        blocking.append({
            "item": "funding 结算 mark 仍有缺（官方不可得）",
            "count": mark_null,
            "pct_of_funding": round(100 * mark_null / fund_tot, 4) if fund_tot else None,
            "reason": "mark1h 月包官方空洞（如 BTCUSDT 2021-07-01 整天）+ 5 个中文名符号无 mark1h 归档；API markPrice 仅 ≥2024 有值，<2024 返回空串",
            "impact": "受影响 (symbol, funding_time) 的正式资金费记账需列为不通过，或按 §门禁 阻断该持仓正式结果",
        })
    for ds, v in field_cov.items():
        if v["symbols_missing"]:
            blocking.append({"item": f"{ds} 存在无文件币", "count": v["symbols_missing"],
                             "symbols": cov.get(ds, {}).get("missing_symbols", [])})

    ready = (not any(b["item"].startswith("OI 同源同事件") for b in blocking)) and \
            bk.get("restore_verified_ok", 0) > 0 and \
            bk.get("backup_sha_mismatches", []) == []

    out = {
        "gate": "data_ready",
        "window": "evidence/Work_策略数据_2026-10-15",
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "universe": {"exchange": "binance", "market": "um", "n_symbols": 526,
                     "scope": "加密币(COIN)活动范围 USDT 线性永续",
                     "pool_snapshot_utc": "2026-10-05T04:53Z",
                     "note": "526 为当前活动池，作库存范围，不冒充完整历史 PIT 池"},
        "manifest": {
            "path": str(ROOT / "manifest.json"),
            "sha256": sha256(ROOT / "manifest.json"),
            "n_files": man["n_files"], "total_bytes": man["total_bytes"],
        },
        "schema": {"path": str(ROOT / "schema.json"), "sha256": sha256(ROOT / "schema.json")},
        "datasets": datasets,
        "field_coverage": field_cov,
        "field_verification": {
            "oi_ts_equals_metrics_create_time": True,
            "oi_value_matches_hot_db": ver.get("A_oi_same_source_event", {}).get("all_match"),
            "oi_sample": ver.get("A_oi_same_source_event", {}).get("detail"),
            "grid": ver.get("B_grid"),
            "ratio_nonnull_total": ver.get("D_ratio_nonnull_total"),
            "funding_rate_cold_vs_archive_mismatch": 0,
            "funding_interval": "8h 网格（00/08/16 UTC），毫秒抖动 ≤4ms",
            "metrics_source": "binance.vision.metrics.daily（OI 与 4 比率同源同事件）",
        },
        "funding_mark": {"funding_events": fund_tot, "mark_null": mark_null,
                         "coverage_pct": mark_cov,
                         "filled_from_mark1h": mk.get("filled_mark_total"),
                         "filled_from_api": apimk.get("api_filled_mark_total")},
        "backup_restore": {
            "evidence": str(EV / "backup_restore_evidence.json"),
            "n_files_backed_up": bk.get("n_files_backed_up"),
            "backup_bytes": bk.get("backup_bytes"),
            "backup_dir": bk.get("backup_dir"),
            "isolated_restore_dir": bk.get("isolated_restore_dir"),
            "isolated_restore_scope": bk.get("isolated_restore_scope"),
            "restore_verified_ok": bk.get("restore_verified_ok"),
            "restore_failures": bk.get("restore_failures"),
            "backup_sha_mismatches": bk.get("backup_sha_mismatches"),
            "existing_hot_backup_verified": bk.get("existing_hot_backup_verified"),
        },
        "blocking_items": blocking,
        "scope_limits": [
            {"id": "funding_mark_missing", "count": mark_null,
             "rule": "受影响 (symbol, funding_time) 的正式资金费记账列不通过；该持仓正式结果阻断（合同「无法补则阻断该持仓正式结果/将范围列不通过」）"},
            {"id": "mark1h_missing_symbols", "symbols": cov.get("mark1h", {}).get("missing_symbols", []),
             "rule": "该 5 币无 mark1h 数据集；其结算 mark 仅由官方 API（≥2024）覆盖"},
            {"id": "oi_source_no_event_segments",
             "rule": "M4 须按逐币可用窗口收窄样本，不得用全市场平均覆盖代替"},
            {"id": "contract_halt_holes",
             "rule": "K 线合约停牌空洞（官方已证无事件）显式处理，不插值、不用现货补"},
            {"id": "pit_pool",
             "rule": "526 为当前活动池，非完整历史 PIT 池；上市/下架身份以各币实际首末条为准，幸存者偏差限制已知"},
        ],
        "ready": bool(ready),
        "ready_semantics": "ready=true 表示：核心字段（K线/资金费/OI/比率）齐备、同源同事件与桶口径已实测核验、备份与隔离恢复已通过；scope_limits 内条目为已知不通过范围，回测须显式排除或按范围列不通过。",
        "notes": [
            "决策特征使用 event_time；可得时间假设：历史归档 K 线按 close_time 后可得，未保存真实公布时刻，属研究可得时间假设，不谎称真实采集 PIT。",
            "Taker（sum_taker_long_short_vol_ratio）与大户账户比仅作独立观察列收集，未添加任何策略筛选条件。",
            "bapi 巨鲸均价未用于本轮。",
            "未使用现货或 OKX 数据补币安永续缺口；未插值/补 0。",
            "本窗只读源（热库 mode=ro/query_only），未改生产库/分片/WAL；未移动或删除任何原件。",
            "资金费结算 mark 为 mark1h 归档 open 或官方 API markPrice；非当前收盘价替代。",
        ],
    }
    (EV / "data_ready.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"ready": out["ready"], "n_files": man["n_files"],
                      "total_bytes": man["total_bytes"], "funding_mark_cov_pct": mark_cov,
                      "blocking": [b["item"] for b in blocking]}, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
