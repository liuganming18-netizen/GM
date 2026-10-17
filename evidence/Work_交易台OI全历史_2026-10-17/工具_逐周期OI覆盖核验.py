#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐周期 OI 可见覆盖核验（只读；只打 HTTP，不写库、不改服务）。

复刻前端两段口径（与源码逐行一致）：
  · `oiWindowedPoints`（`InlineExpandedChart.tsx`）：把 OI 样本裁剪到**当前周期已加载
    K 线的时点区间** [min,max] —— 因为两图共享同一时间轴，窗口外的 OI 不会被画出来；
  · `alignOiToTimeframe`（`adapters.ts`）：按 tfMs 分桶（`floor(ts/tfMs)*tfMs`），
    每桶只留最新一条真实样本。
据此算出「K 线窗口内有多少根蜡烛拿得到 OI 样本」——这才是用户眼睛看到的覆盖。

判据：
  · `覆盖` = 有 OI 样本的桶数 / 窗口内 K 线根数；
  · `最长无OI连续` = 连续多少根 K 线在窗口内没有任何 OI 样本（肉眼看到的空洞宽度）。

用法：
    python3 工具_逐周期OI覆盖核验.py [exchange] [symbol]
    默认 binance BTCUSDT；结果写入 `对账_逐周期OI覆盖_<ex>_<sym>_2026-10-17.json`。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

BASE = os.environ.get("OI_PROBE_BASE", "http://127.0.0.1:8787")
HERE = os.path.dirname(os.path.abspath(__file__))
OI_LIMIT = 5000          # = 前端 OI_HISTORY_LIMIT
PAGE_LIMIT = 1500        # = 前端 HISTORY_PAGE_LIMIT（首屏一页）
DETAIL_LIMIT = 180       # = 前端 DETAIL_BAR_LIMIT（实时尾段）
TF_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
         "4h": 14_400_000, "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000}
TFS = ["5m", "15m", "30m", "1h", "4h", "8h", "12h", "1d"]


def get(path):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(BASE + path, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def iso2ms(s):
    if not s:
        return None
    return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


def bar_ms(b):
    v = b.get("open_time") or b.get("timestamp") or b.get("time")
    if v is None:
        return None
    if isinstance(v, str) and v[:2] == "20":
        return iso2ms(v if v.endswith("Z") else v + "Z")
    return int(v)


def day(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def main():
    ex = sys.argv[1] if len(sys.argv) > 1 else "binance"
    sym = sys.argv[2] if len(sys.argv) > 2 else "BTCUSDT"

    env = get(f"/api/v1/markets/{ex}/{sym}/oi?period=auto&limit={OI_LIMIT}")
    oi = [iso2ms(i["event_time"]) for i in (env.get("items") or [])]
    oi = [t for t in oi if t]
    note = ((env.get("quality") or {}).get("note") or "")
    span = (day(min(oi)), day(max(oi))) if oi else (None, None)
    print(f"{ex}/{sym}  OI 样本 = {len(oi)} 点 | {span[0]} → {span[1]} | "
          f"冷档={'是' if '冷热仓储' in note else '否'}")
    print()

    hdr = (f"{'周期':<5}{'K线根数':>8}{'K线跨度':>10}{'OI窗口点':>9}"
           f"{'对齐桶':>7}{'覆盖':>8}{'最长无OI连续':>13}")
    print(hdr)
    print("-" * 78)

    rows = []
    for tf in TFS:
        ts = set()
        for q in (f"?timeframe={tf}&limit={PAGE_LIMIT}&history_only=1",
                  f"?timeframe={tf}&limit={DETAIL_LIMIT}&no_rest_tail=1"):
            e = get(f"/api/v1/markets/{ex}/{sym}/candles{q}")
            for b in (e.get("bars") or []):
                v = bar_ms(b)
                if v:
                    ts.add(v)
        ts = sorted(ts)
        if not ts:
            print(f"{tf:<5}{'无K线':>8}")
            rows.append({"周期": tf, "K线根数": 0, "错误": "无已入库K线"})
            continue
        lo, hi = ts[0], ts[-1]
        win = [t for t in oi if lo <= t <= hi]
        tfms = TF_MS[tf]
        have = {t // tfms for t in win}
        cov = len(have) / len(ts) * 100
        run = best = 0
        for t in ts:
            if (t // tfms) in have:
                run = 0
            else:
                run += 1
                best = max(best, run)
        span_d = (hi - lo) / 86_400_000
        print(f"{tf:<5}{len(ts):>8}{span_d:>9.1f}d{len(win):>9}{len(have):>7}"
              f"{cov:>7.1f}%{best:>13}")
        rows.append({"周期": tf, "K线根数": len(ts), "K线跨度天": round(span_d, 1),
                     "OI窗口点": len(win), "对齐桶": len(have),
                     "覆盖率": round(cov, 1), "最长无OI连续": best})

    out = {
        "生成时间": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "接口": f"{BASE}/api/v1/markets/{ex}/{sym}/oi?period=auto&limit={OI_LIMIT}",
        "标的": f"{ex}/{sym}",
        "OI点数": len(oi),
        "OI跨度": {"起": span[0], "止": span[1]},
        "冷档接入": "冷热仓储" in note,
        "逐周期": rows,
    }
    p = os.path.join(HERE, f"对账_逐周期OI覆盖_{ex}_{sym.replace('/', '_')}_2026-10-17.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print()
    print("写入:", os.path.basename(p))


if __name__ == "__main__":
    main()
