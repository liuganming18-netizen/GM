#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""研究回测 API 验收（只读 + 受控创建）。对指定 base url 逐项断言。

用法：
  python3 api_acceptance.py http://127.0.0.1:8790            # 只读校验（不创建）
  python3 api_acceptance.py http://127.0.0.1:8790 --create   # 另加：创建一次研究回测并轮询
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787"
CREATE = "--create" in sys.argv

ok = 0
fail = 0


def call(method: str, path: str, body=None, timeout=30):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name} :: {detail}")


print(f"=== 验收目标 {BASE} ===")
st, h = call("GET", "/api/health")
check("健康", st == 200, str(st))

st, d = call("GET", "/api/v1/backtests")
check("GET 列表 = 新契约", st == 200 and d.get("contract_version") == "bt_api_v1"
      and d.get("mode") == "research_backtest", json.dumps(d)[:200])
check("列表带注册表单一当前回测策略",
      (d.get("registry_entry") or {}).get("strategy_id") == "WX_OI_RESONANCE_V1"
      and (d.get("registry_entry") or {}).get("available") is True,
      json.dumps(d.get("registry_entry"))[:200])
check("列表不冒称 M4",
      "独立候选" in ((d.get("registry_entry") or {}).get("note") or ""), "")

st, d2 = call("GET", "/api/v1/backtests?mode=history")
check("旧契约 mode=history 保留", st == 200 and d2.get("mode") == "history", str(st))
st, d3 = call("GET", "/api/v1/reviews")
check("旧契约 /reviews 保留", st == 200 and d3.get("mode") == "history", str(st))

st, d4 = call("POST", "/api/v1/backtests", {"strategy_id": "F_WX_3a"})
check("拒绝非法策略(400)", st == 400 and d4.get("errors"), json.dumps(d4)[:200])
st, d5 = call("POST", "/api/v1/backtests", {"signal_timeframe": "4h"})
check("拒绝未验证周期(400)", st == 400 and any("信号周期" in e for e in d5.get("errors", [])),
      json.dumps(d5)[:200])
st, d6 = call("POST", "/api/v1/backtests", {"exec_timeframe": "5m"})
check("拒绝未验证执行周期(400)", st == 400, json.dumps(d6)[:200])
st, d7 = call("POST", "/api/v1/backtests", {"params": {"oi_min_resonance": 0.9, "hack": 1}})
check("拒绝白名单外参数(400)", st == 400 and any("hack" in e for e in d7.get("errors", [])),
      json.dumps(d7)[:200])
st, d8 = call("POST", "/api/v1/backtests", {"whatever": 1})
check("拒绝未知字段(400)", st == 400, json.dumps(d8)[:200])
st, d9 = call("POST", "/api/v1/backtests", {"warmup_bars": 30})
check("拒绝预热不足(400)", st == 400, json.dumps(d9)[:200])
st, d10 = call("POST", "/api/v1/backtests", {"params": {"live_order_enabled": True}})
check("拒绝开启真实下单(400)", st == 400, json.dumps(d10)[:200])

# ── P0-2 新增：多标的 / 窗口 / 成本作用域 ────────────────────────────────
st, dm = call("POST", "/api/v1/backtests", {"symbols": ["BTCUSDT", "ETHUSDT"]})
check("拒绝多标的(400，不静默取第一个)", st == 400
      and any("单标的" in e for e in dm.get("errors", [])), json.dumps(dm)[:200])
st, dg = call("POST", "/api/v1/backtests",
              {"window_start": "2026-10-09T00:07:00Z", "window_end": "2026-10-16T00:00:00Z"})
check("拒绝非网格窗口(400)", st == 400
      and any("网格" in e for e in dg.get("errors", [])), json.dumps(dg)[:200])
st, ds = call("POST", "/api/v1/backtests", {"window_start": "2026-10-09T00:00:00Z"})
check("拒绝只给单个窗口端点(400)", st == 400, json.dumps(ds)[:200])
st, dx = call("POST", "/api/v1/backtests",
              {"window_start": "2026-10-09T00:00:00Z", "window_end": "2026-10-16T00:00:00Z",
               "window_days": 7})
check("拒绝窗口与 window_days 并用(400)", st == 400
      and any("互斥" in e for e in dx.get("errors", [])), json.dumps(dx)[:200])
st, dc = call("POST", "/api/v1/backtests", {"params": {"fee_rate": 2.0}})
check("拒绝越界成本参数(400)", st == 400, json.dumps(dc)[:200])

st, d11 = call("GET", "/api/v1/backtests/bt_19700101T000000Z_deadbeef")
check("未知 run_id → 404", st == 404, str(st))
st, d12 = call("GET", "/api/v1/backtests/not-a-run")
check("非法 run_id → 404", st == 404, str(st))

run_id = None
if CREATE:
    # 显式 UTC 窗口 + 默认成本（与受控实验同规格）
    st, d13 = call("POST", "/api/v1/backtests",
                   {"strategy_id": "WX_OI_RESONANCE_V1", "symbols": ["BTCUSDT"],
                    "window_start": "2026-10-09T00:00:00Z",
                    "window_end": "2026-10-16T00:00:00Z",
                    "note": "api_acceptance explicit window"})
    check("创建研究回测（显式窗口）→ 202", st == 202 and d13.get("run_id"),
          json.dumps(d13)[:300])
    run_id = d13.get("run_id")
    if run_id:
        for _ in range(180):
            st, s = call("GET", f"/api/v1/backtests/{run_id}")
            if s.get("status") in ("succeeded", "failed", "interrupted"):
                break
            time.sleep(1)
        check("运行终态 = succeeded", s.get("status") == "succeeded",
              f"status={s.get('status')} reason={s.get('failure_reason')}")
        check("窗口来源 = 显式 UTC（未被静默替换）",
              ((s.get("window") or {}).get("window_source") == "explicit_utc"),
              json.dumps(s.get("window"))[:200])
        check("run 暴露 chart_file / snapshot_files",
              bool(s.get("chart_file")) and len(s.get("snapshot_files") or []) >= 4,
              json.dumps({"c": s.get("chart_file"), "n": len(s.get("snapshot_files") or [])}))
        for sub in ("metrics", "equity", "trades", "chart", "quality"):
            st, r = call("GET", f"/api/v1/backtests/{run_id}/{sub}")
            check(f"子资源 /{sub}", st == 200 and r.get("run_id") == run_id, str(st))
        st, tr = call("GET", f"/api/v1/backtests/{run_id}/trades")
        items = tr.get("items") or []
        check("交易列表非空", len(items) > 0, f"n={len(items)}")
        if items:
            tid = items[0]["trade_id"]
            st, one = call("GET", f"/api/v1/backtests/{run_id}/trades/{tid}")
            check("单笔可查且含 funding/orders",
                  st == 200 and one.get("trade") and "funding" in one and "orders" in one,
                  str(st))
            check("单笔可联结回原始信号(signal_id)",
                  bool(one.get("signal_id")) and bool(one.get("original_signal")),
                  json.dumps({"sid": one.get("signal_id"),
                              "orig": bool(one.get("original_signal"))}))
        st, mt = call("GET", f"/api/v1/backtests/{run_id}/metrics")
        m = mt.get("metrics") or {}
        check("指标含真实交易数", isinstance(m.get("trades_count"), int),
              json.dumps(m)[:200])
        check("指标明确区分 profit_factor 与平均盈亏比",
              "profit_factor" in m and "avg_win_loss_ratio" in m, "")
        check("指标自带账本重算且一致",
              (m.get("metrics_from_ledger") or {}).get("available") is True
              and m.get("metrics_match") is True, json.dumps(m.get("metrics_match_diffs"))[:200])
        bench = mt.get("benchmark_buy_hold") or {}
        check("基准买入持有存在且为同成本口径",
              bench.get("benchmark_kind") == "perp_buy_and_hold_same_cost"
              and bench.get("symbol") == "BTCUSDT", json.dumps(bench)[:200])
        mth = mt.get("monthly_returns") or {}
        check("月度收益首月给实际区间收益并标 partial",
              bool(mth.get("months")) and mth["months"][0].get("partial_month") is True
              and mth["months"][0].get("return_pct") is not None,
              json.dumps(mth)[:200])
        check("不冒称 M4", "独立候选研究" in (mt.get("m4_disclaimer") or ""), "")
        st, eq = call("GET", f"/api/v1/backtests/{run_id}/equity")
        check("equity 含同成本基准逐点净值（长度 == 策略权益）",
              len(eq.get("benchmark_equity") or []) == len(eq.get("items") or [])
              and len(eq.get("items") or []) > 0,
              json.dumps({"b": len(eq.get("benchmark_equity") or []),
                          "s": len(eq.get("items") or [])}))
        _bmeta = eq.get("benchmark") or {}
        _bseq = eq.get("benchmark_equity") or []
        check("equity 基准末点 == 基准 final_equity（含出场滑点/费）",
              bool(_bseq) and abs(float(_bseq[-1]["equity"])
                                  - float(_bmeta.get("final_equity"))) < 1e-9,
              json.dumps({"last": _bseq[-1]["equity"] if _bseq else None,
                          "final": _bmeta.get("final_equity")}))
        check("equity 基准 net_return_pct 与末点自洽",
              bool(_bseq) and abs(float(_bmeta.get("net_return_pct"))
                                  - (float(_bseq[-1]["equity"]) - float(m["initial_equity"]))
                                  / float(m["initial_equity"]) * 100) < 1e-6,
              json.dumps({"net": _bmeta.get("net_return_pct"),
                          "implied": ((float(_bseq[-1]["equity"]) - float(m["initial_equity"]))
                                      / float(m["initial_equity"]) * 100) if _bseq else None}))
        st, q = call("GET", f"/api/v1/backtests/{run_id}/quality")
        dq = q.get("data_quality") or {}
        check("质量含对账与因子门槛说明",
              dq.get("reconcile_ok") is not None and "oi_gate_note" in dq, "")
        check("质量含资金费频率/覆盖（不假设 8h）",
              bool((dq.get("funding_frequency") or {}).get("source"))
              and "expected_settlements" in json.dumps(dq), json.dumps(dq.get("funding_frequency"))[:200])
        check("质量含结算网格缺格计数（逐网格核对，非仅比次数）",
              "funding_missing_settlements_count" in dq
              and dq.get("funding_match_tolerance_ms") is not None
              and (dq.get("funding_missing_settlements_count") == 0
                   if dq.get("funding_coverage_ok") else True),
              json.dumps({k: dq.get(k) for k in ("funding_missing_settlements_count",
                                                 "funding_expected_settlements",
                                                 "funding_actual_settlements",
                                                 "funding_match_tolerance_ms")}))
        st, ch = call("GET", f"/api/v1/backtests/{run_id}/chart")
        chart = ch.get("chart") or {}
        check("图表含真实K线/信号/开平仓/止损轨迹/无止盈声明",
              chart.get("candles") and "signals" in chart and chart.get("trades")
              and chart.get("take_profit", {}).get("exists") is False, "")
        check("图表信号 confirm 落在窗口内",
              all(int(sig["t"]) >= (s.get("window") or {})["win_start_ms"]
                  and int(sig["t"]) < (s.get("window") or {})["win_end_ms"]
                  for sig in chart.get("signals") or []), "")
        st, again = call("GET", f"/api/v1/backtests/{run_id}")
        check("GET 不触发计算（状态稳定）", again.get("status") == "succeeded", "")

    # ── 失败态：GET metrics/quality 不得报 ok ──
    st, df = call("POST", "/api/v1/backtests",
                  {"strategy_id": "WX_OI_RESONANCE_V1", "symbols": ["BTCUSDT"],
                   "window_start": "2027-01-01T00:00:00Z",
                   "window_end": "2027-01-08T00:00:00Z", "note": "api_acceptance oob"})
    if st == 202 and df.get("run_id"):
        fr = df["run_id"]
        for _ in range(60):
            st, fs = call("GET", f"/api/v1/backtests/{fr}")
            if fs.get("status") in ("succeeded", "failed", "interrupted"):
                break
            time.sleep(1)
        check("越界窗口 → failed（不静默缩窗）", fs.get("status") == "failed",
              f"status={fs.get('status')}")
        st, fm = call("GET", f"/api/v1/backtests/{fr}/metrics")
        check("失败态 metrics.quality != ok",
              (fm.get("quality") or {}).get("status") != "ok", json.dumps(fm.get("quality")))
        st, fq = call("GET", f"/api/v1/backtests/{fr}/quality")
        check("失败态 quality != ok",
              (fq.get("quality") or {}).get("status") != "ok", json.dumps(fq.get("quality")))

print(f"\n=== 结果：{ok} PASS / {fail} FAIL ===")
if run_id:
    print(f"run_id = {run_id}")
sys.exit(0 if fail == 0 else 1)
