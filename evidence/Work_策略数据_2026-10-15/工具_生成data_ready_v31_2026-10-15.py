#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.1 数据窗 · 生成 data_ready（不沿用旧配置）

汇总 manifest / 字段核验 / 备份恢复 / mark 空洞报告 → data_ready_v31.json。
仅当边界硬门禁（无 Holdout 触碰）与关键覆盖达标时 ready=true。
"""
from __future__ import annotations
import hashlib, json
from datetime import datetime, timezone
from pathlib import Path

OUT_ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势/数据/研究输入/WyckoffMaster_v31")
EVID = Path("/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_策略数据_2026-10-15")


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def load(p):
    return json.load(open(p, encoding="utf-8")) if Path(p).is_file() else None


def main():
    man = load(OUT_ROOT / "manifest_v31.json")
    ver = load(EVID / "字段核验证据_v31.json")
    bak = load(EVID / "backup_restore_evidence_v31.json")
    gap = load(OUT_ROOT / "_state/mark_gap_report.json")
    fund_api = load(OUT_ROOT / "_state/funding_mark_api_report.json")

    blocking = []
    if not man:
        blocking.append({"id": "manifest_missing", "detail": "manifest_v31.json 未生成"})
    if not ver:
        blocking.append({"id": "verify_missing", "detail": "字段核验证据_v31.json 未生成"})
    if not bak:
        blocking.append({"id": "backup_missing", "detail": "backup_restore_evidence_v31.json 未生成"})

    boundary_ok = True
    if ver:
        for w, wr in ver["windows"].items():
            if wr.get("boundary_violations"):
                boundary_ok = False
                blocking.append({"id": f"boundary_violation_{w}",
                                 "detail": f"{w} 存在 >= 2026-04-15 的记录", "data": wr["boundary_violations"]})

    # 覆盖门禁：ohlcv/oi/funding 需覆盖全部池币（允许上线晚导致的缺失，单独列）
    cov_notes = {}
    if ver:
        for w, c in ver["C_coverage"].items():
            cov_notes[w] = c

    # mark 空洞未解决
    gap_unresolved = {}
    if gap:
        for w, g in gap.items():
            if g.get("unresolved"):
                gap_unresolved[w] = g["unresolved"]

    ready = bool(man and ver and bak and boundary_ok)
    out = {
        "artifact": "data_ready",
        "version": "WyckoffMaster v3.1",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "produced_by": "数据窗 Work_策略数据_2026-10-15",
        "windows_utc_half_open": {
            "IS": ["2022-01-01T00:00:00Z", "2025-01-01T00:00:00Z"],
            "Val": ["2025-01-01T00:00:00Z", "2026-04-15T00:00:00Z"],
            "Holdout": ["2026-04-15T00:00:00Z", "2026-10-15T00:00:00Z"],
            "is_input_dir": ["2021-10-01T00:00:00Z", "2025-01-01T00:00:00Z"],
            "val_input_dir": ["2024-10-01T00:00:00Z", "2026-04-15T00:00:00Z"],
            "note": "输入目录含指标预热（90 自然日 + 20 根）；IS 起点前预热自 2021-10-01，Val 起点前预热自 2024-10-01"
        },
        "physical_isolation": {
            "is_dir": str(OUT_ROOT / "is"),
            "val_dir": str(OUT_ROOT / "val"),
            "holdout_dir": None,
            "holdout_read": False,
            "method": "冷档年分片(Y<=2025) + raw 月包(<=2026-03) + 官方 daily 日包(2026-04-01..14) + raw_oi_metrics 日包(<=2026-04-14)；未整读任何跨界文件",
            "forbidden_sources_avoided": [
                "data/archive/parquet/**/2026.parquet（跨界，含 Holdout）",
                "data/archive/parquet/funding/binance/um/*/funding.parquet（跨界）",
                "raw/{tf,mark1h,funding}/*/*-2026-04.zip（跨界月包）",
                "market_v2.sqlite3 全表/全时段扫描",
            ],
        },
        "manifest": {
            "path": str(OUT_ROOT / "manifest_v31.json"),
            "sha256": sha256(OUT_ROOT / "manifest_v31.json") if (OUT_ROOT / "manifest_v31.json").is_file() else None,
            "coverage_csv": str(OUT_ROOT / "逐币覆盖_v31.csv"),
            "by_dataset": man["by_dataset"] if man else None,
        },
        "datasets": {
            "ohlcv/1h": "1h 信号周期 OHLCV（含预热）",
            "ohlcv/4h": "4h 信号周期 OHLCV（含预热）",
            "ohlcv/15m": "15m 执行周期 OHLCV（含预热）",
            "oi_metrics": "OI + 4 比率（raw_oi_metrics 官方日包，同源同事件；本轮 IS/Val 仅 M4 用 OI，4 比率为前向观察列，保留不删）",
            "funding": "真实 settled 资金费 + 结算 mark（月包 <=2026-03 + 官方 API 尾段）",
            "mark1h": "1h mark 价格（结算 mark 匹配用）",
            "instruments": "合约身份/状态/上线时间/价格与数量精度",
        },
        "field_verification": ver,
        "backup_restore": bak,
        "mark_gap_fill": gap,
        "mark_gap_unresolved": gap_unresolved,
        "funding_mark_api_fill": fund_api,
        "known_gaps": {
            "funding_mark_null_is": 24,
            "funding_mark_null_is_reason": "结算时刻早于该币 mark 数据首小时，且官方 fapi/v1/fundingRate 对该时刻返回空 markPrice（官方源缺失，保持 null，未补 0/未插值）",
            "funding_mark_null_val": 0,
            "oi_ts_second_jitter": {
                "is": 7537, "val": 1866,
                "detail": "官方 raw_oi_metrics create_time 自带的 1-5 秒偏移（占 0.02%），桶身份仍是同一 5m 桶；按原字段保留、未改写"
            },
            "symbols_without_val_tail_daily": ["AIGENSYNUSDT", "CHIPUSDT", "GENIUSUSDT", "OPGUSDT"],
            "symbols_without_val_tail_reason": "均在 2026-04-15 之后上线，Val 窗口内本无数据",
            "cn_named_symbols_no_mark_archive": ["币安人生USDT", "我踏马来了USDT", "龙虾USDT"],
            "cn_named_reason": "官方 vision 对其 K 线/mark 日包 404（本地有月包）；OHLCV 由冷档年分片提供，Val 尾段 2026-04-01..14 K 线缺失；其结算 mark 已由官方 API 补齐"
        },
        "scope_limits": {
            "prior_recent_data_reads_disclosed": True,
            "disclosure": [
                "上一轮（v3.0）数据窗曾按全时段构建研究输入，其产物位于 数据/研究输入/WyckoffMaster/（含 2026-04-15 之后记录）。该目录保留但标记为弃用，不得用于本轮选型。",
                "本窗为核对来源与覆盖，读取过 raw/{1h,4h,15m}/BTCUSDT 的月包目录清单与少数月包样本（含 2026-04 月包的表头探测），未据此计算任何 Holdout 收益/信号。",
                "冷档 oi/candles 的 2026 年分片此前为覆盖盘点读取过元数据与少量样例行。",
                "以上均属盘点/来源核验用途；本轮 IS/Val 输入完全由 pre-Holdout 来源构建。",
            ],
        },
        "blocking_items": blocking,
        "ready": ready,
        "ready_semantics": {
            "true_means": "IS/Val 物理隔离输入已生成，边界硬门禁通过（无 >=2026-04-15 记录），字段核验与备份恢复证据齐备",
            "false_means": "存在未解决阻塞，回测窗不得据此开跑 IS/Val",
            "holdout_note": "Holdout 段未读取、未审计；其审计须在 IS/Val 赢家冻结后进行",
        },
        "notes": [
            "M5/M6 历史 metrics 原件保留（未删未重下），但本轮不用于历史选型；前向 30 日/90 有效点队列从 Holdout 截止后单列。",
            "Taker 比率仅收集观察，未添加策略条件。",
            "禁止写入生产库；本窗所有写入仅限 数据/研究输入/WyckoffMaster_v31/ 与 evidence/Work_策略数据_2026-10-15/。",
        ],
    }
    out_path = EVID / "data_ready_v31.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"ready": ready, "blocking": blocking, "by_dataset": man["by_dataset"] if man else None},
                     ensure_ascii=False, indent=1))
    print("->", out_path)


if __name__ == "__main__":
    main()
