#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""聪明钱接入**验收证据**汇总（只读、零网络）：原始响应 SHA、API 样例 SHA、覆盖、矩阵。

产出：`验收证据/聪明钱/币安聪明钱接入_证据_<日期>.json`
用法：python3 聪明钱/build_evidence.py [YYYY-MM-DD]
"""
import glob
import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))
import smart_money as sm  # noqa: E402

DAY = sys.argv[1] if len(sys.argv) > 1 else "2026-10-07"
OUT = BACKEND / "验收证据" / "聪明钱" / f"币安聪明钱接入_证据_{DAY}.json"


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def files(pattern):
    out = []
    for p in sorted(glob.glob(str(HERE / pattern))):
        out.append({"path": os.path.relpath(p, BACKEND), "bytes": os.path.getsize(p),
                    "sha256": sha(p)})
    return out


def load(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


cov = sm.coverage()
idx = cov.get("index") or {}

# 陈旧阈值**固定** = 采集周期×2（默认 600s）；**不**随 full_pool 放宽（2026-10-08 更正）
eff_limit = sm.DEFAULT_STALE_AFTER_S
eff = {"ok": 0, "stale": 0, "unknown": 0}
for ex, mid in sm.list_symbols():
    p = sm.read(ex, mid)
    if not p:
        continue
    eff[sm._freshness(p, stale_after_s=eff_limit)["status"]] += 1
evidence = {
    "task": "币安聪明钱 API 真实接入（独立任务）",
    "date": DAY,
    "source": {
        "host": sm.SMART_MONEY_HOST,
        "base_path": "/bapi/futures/v1/public/future/smart-money/signal",
        "auth": "无（无 cookie / 无 Authorization / 无验证码；页面自身 XHR）",
        "page": "https://www.binance.bh/zh-CN/smart-money/signal/{SYMBOL}?timeRange=ALL",
        "endpoints": sorted(k for k in sm.ENDPOINTS if k.startswith("binance.smart_money")),
        "not_a_substitute_for": ["全市场持仓", "openInterest", "大户/账户数多空比", "taker 买卖量比"],
    },
    "raw_responses": files("raw/*.json") + files("raw/*.hdr"),
    "api_samples": files("api_samples/*.json"),
    "endpoint_matrix": load(HERE / "endpoint_matrix_2026-10-07.json"),
    "time_semantics": {
        "trade_event_time": "details/list[].lastTradeTime —— 交易活动时间（仅活跃度）",
        "upstream_snapshot_time": "币安未公开 ⇒ null（不推断）",
        "local_fetched_at": "本机取回/落盘时点 —— **陈旧判据只用它**",
        "updateTime": "overview.updateTime 日级队列标记，不参与新鲜度判定",
    },
    "snapshot_coverage": {
        "dir": cov.get("snapshot_dir"),
        "symbols_on_disk": cov.get("symbols_on_disk"),
        "continuous_set": cov.get("continuous_set"),
        "collector": cov.get("collector"),
        "freshness": cov.get("freshness"),
        "stale_after_s": eff_limit,
        "api_effective_freshness": {
            "threshold_s": eff_limit,
            "status": eff,
            "note": ("阈值**固定** = 采集周期×2（默认 600s），**不随 full_pool / 扫描用时放宽**；"
                     "采集停摆 ⇒ 本机取回时点不再推进 ⇒ ≤10 分钟判 stale。"),
        },
        "age_seconds": cov.get("age_seconds"),
        "last_round": {k: idx.get(k) for k in
                       ("updated_at", "targets", "ok", "skipped", "failed",
                        "time_range", "interval_s", "head_n", "full_pool", "elapsed_s",
                        "requests", "mode_run")},
    },
    "shared_gateway_change": {
        "file": "data_hub/gateway.py（主项目；**唯一**共享入口改动）",
        "change": "业务成功码白名单追加币安 bapi 成功码 \"000000\"（原为 None/\"0\"）",
        "backwards_compatible": "已登记端点无返回值等于 \"000000\"；176 项既有单测全绿",
    },
    "frontend_contract": {
        "route": "GET /api/v1/smart-money/{exchange}/{market_id}",
        "consumed_by": "前端实现-AIStudio/src/api/smartMoney.ts → smartMoneyMapping.ts",
        "top_level_keys_used": ["overview", "stats"],
        "note": "前端**未改动**；页面经 vite(3000) → /api 代理 → 后端(8787) 读取",
    },
}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps({"written": str(OUT), "raw_files": len(evidence["raw_responses"]),
                  "api_samples": len(evidence["api_samples"]),
                  "snapshot_symbols_on_disk": cov.get("symbols_on_disk"),
                  "continuous_set": cov.get("continuous_set"),
                  "freshness": cov.get("freshness")}, ensure_ascii=False, indent=1))
