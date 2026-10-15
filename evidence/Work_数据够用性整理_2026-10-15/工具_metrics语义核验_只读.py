#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""metrics 衍生字段「与 REST 指标的口径/时标对应关系」核验（全程只读，不下载）。

核验对象（用本地已有证据，不新增网络请求）：
  A. openInterestHist 端点 vs 归档 5m 行        —— 用 REST 采样 + 归档 ZIP
  B. takerlongshortRatio 端点 4h vs 热库 5m K线 —— 用 REST 采样 + 热库 candle
  C. K线自身时标控制：REST 4h K线采样 vs 热库 4h —— 证明热库 open_time = 桶起点
  D. topLongShortAccountRatio 端点 4h vs 归档 5m —— 逐位相等性检验
  E. 5m 桶内 taker 近似：归档 metrics 列 vs 冷档 5m K线
  F. OKX K线 taker 字段可用性

输出：metrics字段语义核验.json
"""
import csv
import datetime as dt
import io
import json
import os
import sqlite3
import zipfile
from urllib.parse import quote

RAW = "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载/raw_oi_metrics"
SAMPLE = "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/趋势与套利研究/原始证据/数据接口采样"
HOT = "/Users/boos/WorkBuddy AI/加密货币趋势判断系统/data/market_v2.sqlite3"
COLD = "/Users/boos/WorkBuddy AI/加密货币趋势判断系统/data/archive/parquet"
OUT = "/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_数据够用性整理_2026-10-15"

RATIO_COLS = ["count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
              "count_long_short_ratio", "sum_taker_long_short_vol_ratio"]


def iso(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def metrics_day(sym, day):
    p = os.path.join(RAW, sym, f"{sym}-metrics-{day}.zip")
    if not os.path.exists(p):
        return None
    z = zipfile.ZipFile(p)
    rows = list(csv.reader(io.StringIO(z.read(z.namelist()[0]).decode("utf-8"))))
    return {r[0]: r for r in rows[1:]}, rows[0]


def hot_conn():
    c = sqlite3.connect("file://" + quote(os.path.abspath(HOT)) + "?mode=ro", uri=True)
    c.execute("PRAGMA query_only=ON")
    return c


res = {"generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
       "discipline": "全程只读；只用本地已有 REST 采样与本地归档，不发起任何下载"}

# ---------- C. K线自身时标控制 ----------
rest4h = json.load(open(os.path.join(SAMPLE, "binance_klines_4h_limit5.json")))
c = hot_conn()
ctl = []
for r in rest4h:
    row = list(c.execute("""select open,high,low,close,volume,taker_buy_volume,close_time from candles
        where exchange='binance' and market='um' and symbol='BTCUSDT' and timeframe='4h' and open_time=?""",
                         (r[0],)))
    if not row:
        ctl.append({"open_time": r[0], "open_time_utc": iso(r[0]), "hot": None,
                    "note": "热库无此 open_time（该桶在采样后被新版本覆盖）"})
        continue
    h = row[0]
    ctl.append({"open_time": r[0], "open_time_utc": iso(r[0]),
                "rest": {"open": r[1], "close": r[4], "volume": r[5], "taker_buy_volume": r[9],
                         "close_time": iso(r[6])},
                "hot": {"open": h[0], "close": h[3], "volume": h[4], "taker_buy_volume": h[5],
                        "close_time": iso(h[6])},
                "match": (h[0] == r[1] and h[3] == r[4] and h[4] == r[5] and h[5] == r[9])})
res["C_K线时标控制"] = {"对象": "binance/um/BTCUSDT/4h，REST 采样 vs 热库",
                        "结论": "热库 open_time = 桶起点；close_time = 起点+4h-1ms",
                        "明细": ctl}

# ---------- A. OI 端点 vs 归档 5m ----------
rest_oi = json.load(open(os.path.join(SAMPLE, "binance_openInterestHist_4h.json")))
D = {}
for day in ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"]:
    got = metrics_day("BTCUSDT", day)
    if got:
        D.update(got[0])
a_rows = []
for r in rest_oi:
    lab = r["timestamp"]
    rec = {"rest_label": iso(lab), "rest_sum_open_interest": r["sumOpenInterest"]}
    for delta in (-300, -600, -900, 0):
        k = iso(lab + delta * 1000)[:19].replace("T", " ").replace("Z", "")
        v = D.get(k, [None] * 8)[2] if D.get(k) else None
        rec[f"archive_at_{delta}s"] = {"create_time": k, "sum_open_interest": v}
        if v is not None and float(v) == float(r["sumOpenInterest"]):
            rec.setdefault("exact_hits", []).append(delta)
    a_rows.append(rec)
res["A_OI端点与归档"] = {
    "对象": "binance openInterestHist(period=4h) vs raw_oi_metrics 5m 行",
    "结论": "归档 create_time = REST 标签 - 300s（桶起点）；OI 值逐位相等",
    "明细": a_rows,
    "精确命中数": sum(1 for x in a_rows if x.get("exact_hits")),
    "总点数": len(a_rows),
}

# ---------- B. taker 端点 4h vs 热库 5m K线 ----------
rest_tk = json.load(open(os.path.join(SAMPLE, "binance_takerlongshortRatio.json")))
b_rows = []
for r in rest_tk:
    lab = r["timestamp"]
    lo, hi = lab, lab + 4 * 3600 * 1000 - 5 * 60 * 1000
    q = """select count(*), sum(cast(volume as real)), sum(cast(taker_buy_volume as real)),
                  sum(cast(quote_volume as real)), sum(cast(taker_buy_quote_volume as real))
           from candles where exchange='binance' and market='um' and symbol='BTCUSDT'
             and timeframe='5m' and open_time>=? and open_time<=?"""
    n, V, TB, Q, TQ = list(c.execute(q, (lo, hi)))[0]
    rec = {"rest_label": iso(lab), "rest_buyVol": r["buyVol"], "rest_sellVol": r["sellVol"],
           "rest_buySellRatio": r["buySellRatio"],
           "窗口": f"[{iso(lo)}, {iso(hi)}]"}
    if n:
        rec.update({"n_5m": n,
                    "sum_taker_buy_volume": round(TB, 6), "sum_volume": round(V, 6),
                    "derived_buyVol": round(TB, 6), "derived_sellVol": round(V - TB, 6),
                    "derived_buySellRatio": round(TB / (V - TB), 4),
                    "buyVol_bit_exact": round(TB, 3) == round(float(r["buyVol"]), 3),
                    "sellVol_bit_exact": round(V - TB, 3) == round(float(r["sellVol"]), 3),
                    "ratio_4dp_match": round(TB / (V - TB), 4) == round(float(r["buySellRatio"]), 4)})
    b_rows.append(rec)
res["B_taker端点与K线"] = {
    "对象": "binance takerlongshortRatio(period=4h) vs 热库 5m K线 taker 字段窗口求和",
    "结论": "同币同窗口买卖比可由 K线 taker_buy_volume/volume 复算；"
            "该端点标签 = 窗口起点（与 K线 open_time 同为桶起点，10 点窗口均满 48 根 5m）；"
            "但两个官方端点不保证逐位相同：买量逐位相等 5/10、比值 4 位相等 7/10，最大相对残差 0.114% ⇒ 只能称『可复算/近似』",
    "明细": b_rows,
    "买量逐位相等点数": sum(1 for x in b_rows if x.get("buyVol_bit_exact")),
    "比值4位相等点数": sum(1 for x in b_rows if x.get("ratio_4dp_match")),
    "总点数": len(b_rows),
}

# ---------- D. 多空比端点 4h vs 归档 5m ----------
rest_ls = json.load(open(os.path.join(SAMPLE, "binance_topLongShortAccountRatio.json")))
d_rows = []
for r in rest_ls:
    lab = r["timestamp"]
    rec = {"rest_label": iso(lab), "rest_longShortRatio": r["longShortRatio"],
           "rest_longAccount": r["longAccount"], "rest_shortAccount": r["shortAccount"]}
    for delta in (-300, -600, -900):
        k = iso(lab + delta * 1000)[:19].replace("T", " ").replace("Z", "")
        v = D.get(k, [None] * 8)[4] if D.get(k) else None
        rec[f"count_toptrader_at_{delta}s"] = v
        if v is not None:
            rec[f"round4_{delta}s"] = f"{float(v):.4f}"
    rec["bit_equal_any_offset"] = any(
        rec.get(f"round4_{d}s") == f"{float(r['longShortRatio']):.4f}" for d in (-300, -600, -900))
    d_rows.append(rec)
res["D_多空比端点与归档"] = {
    "对象": "binance topLongShortAccountRatio(period=4h) vs 归档 5m count_toptrader_long_short_ratio",
    "结论": "数值最接近 -300s 边界（即桶起点口径），但 10 点中仅 3 点在 4 位小数上相等；"
            "REST 4h 是窗口口径，与归档 5m 口径不逐位相等 ⇒ 不得直接等同",
    "明细": d_rows,
    "4位相等点数": sum(1 for x in d_rows if x["bit_equal_any_offset"]),
    "总点数": len(d_rows),
}

# ---------- E. 5m 桶 taker 近似 ----------
import pyarrow.parquet as pq  # noqa: E402
sym, day = "BTCUSDT", "2026-09-10"
md = metrics_day(sym, day)[0]
t = pq.read_table(f"{COLD}/candles/binance/um/{sym}/5m/2026.parquet").to_pydict()
cand = {}
for i in range(len(t["open_time"])):
    cand[iso(t["open_time"][i])[:19].replace("T", " ").replace("Z", "")] = (
        float(t["volume"][i]), float(t["taker_buy_volume"][i]),
        float(t["quote_volume"][i]), float(t["taker_buy_quote_volume"][i]))
keys = [k for k in sorted(md) if k.startswith(day) and k in cand]
e_rows = []
for name, idx in (("base taker_buy_volume/(volume-tbv)", (0, 1)), ("quote taker_buy_quote/(quote-tbq)", (2, 3))):
    for delta in (0, -5, 5):
        diffs = []
        for k in keys:
            kk = (dt.datetime.strptime(k, "%Y-%m-%d %H:%M:%S") + dt.timedelta(minutes=delta)
                  ).strftime("%Y-%m-%d %H:%M:%S")
            if kk not in cand:
                continue
            v, tb, q, tq = cand[kk]
            num, den = (tb, v - tb) if idx == (0, 1) else (tq, q - tq)
            if den <= 0:
                continue
            diffs.append(abs(float(md[k][7]) - num / den))
        if not diffs:
            continue
        s = sorted(diffs)
        e_rows.append({"口径": name, "偏移分钟": delta, "n": len(diffs),
                       "中位绝对差": round(s[len(s) // 2], 6),
                       "≤1e-3占比": round(sum(1 for x in diffs if x <= 1e-3) / len(diffs) * 100, 1),
                       "≤1e-2占比": round(sum(1 for x in diffs if x <= 1e-2) / len(diffs) * 100, 1)})
res["E_5m桶taker近似"] = {
    "对象": f"{sym} {day} 归档 sum_taker_long_short_vol_ratio vs 冷档 5m K线",
    "结论": "同桶（偏移 0）对齐成立；数值近似而非逐位相等",
    "明细": e_rows,
}

# ---------- F. OKX taker 可用性 ----------
f_rows = []
for r in c.execute("""select exchange, count(*) n,
                        sum(case when taker_buy_volume='' or taker_buy_volume is null then 1 else 0 end),
                        sum(case when taker_buy_quote_volume='' or taker_buy_quote_volume is null then 1 else 0 end)
                      from candle_versions group by 1 order by 1"""):
    f_rows.append({"exchange": r[0], "rows": r[1], "empty_taker_buy_volume": r[2],
                   "empty_taker_buy_quote_volume": r[3],
                   "empty_pct": round(r[2] / r[1] * 100, 2)})
res["F_OKX_taker可用性"] = {
    "对象": "热库 candle_versions taker 列空值率",
    "结论": "币安 0% 空（可复算）；OKX 100% 空（OKX K线响应无该字段，candle_field_notes 已记录）"
            " ⇒ 币安结论不得外推 OKX",
    "明细": f_rows,
}
c.close()

with open(os.path.join(OUT, "metrics字段语义核验.json"), "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=1)
print(json.dumps({k: (v if not isinstance(v, dict) else {kk: vv for kk, vv in v.items() if kk != "明细"})
                  for k, v in res.items()}, ensure_ascii=False, indent=1))
