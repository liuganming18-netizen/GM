#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成交付 CSV：逐币逐周期可用范围 / 仍缺与处理建议 / 文件清单及SHA。只读。"""
import csv, json, os, hashlib, datetime as dt
from collections import defaultdict

BASE = "/Users/boos/Desktop/威科夫因子共振趋势"
CODEX = f"{BASE}/evidence/数据完整度验收_2026-10-14"
PREV = f"{BASE}/evidence/Work_补缺_2026-10-14"
OUT = f"{BASE}/evidence/Work_数据够用性整理_2026-10-15"
ARCH = "/Users/boos/WorkBuddy AI/加密货币趋势判断系统/data/archive/parquet"
DB = "/Users/boos/WorkBuddy AI/加密货币趋势判断系统/data/market_v2.sqlite3"
RAW = "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载"

MAINLINE = ["1d", "12h", "8h", "4h", "1h", "30m", "15m"]
ALLTF = MAINLINE + ["5m"]

def iso(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def sha256(path, buf=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(buf)
            if not b: break
            h.update(b)
    return h.hexdigest()

# ---------- 1. 读补件，按 symbol|period 归集 ----------
patch_rows = defaultdict(int)      # (ex,mkt,sym,tf) -> rows
patch_keys = set()
for fn in ("补缺_K线_api_import.csv", "补缺_K线_okx补齐_import.csv",
           "补缺_K线_okx前端_import.csv", "补缺_K线_官方日包_import.csv",
           "补缺_K线_local_import.csv"):
    with open(os.path.join(PREV, fn), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            k = (r["exchange"], r["market"], r["symbol"], r["timeframe"])
            tk = k + (r["open_time"],)
            if tk in patch_keys:
                continue
            patch_keys.add(tk)
            patch_rows[k] += 1

# ---------- 2. 读 Codex 冷热覆盖 + 内部缺槽 ----------
cov = []
with open(os.path.join(CODEX, "逐币逐周期冷热覆盖.csv"), encoding="utf-8-sig") as f:
    for r in csv.DictReader(f):
        if r["metric"] == "candle_heads":
            cov.append(r)

runs = defaultdict(list)   # (ex,sym,tf) -> list of (prev_ts,next_ts)
with open(os.path.join(CODEX, "冷热合并内部缺槽.csv"), encoding="utf-8-sig") as f:
    for r in csv.DictReader(f):
        if r["metric"] == "candle_heads":
            runs[(r["exchange"], r["symbol"], r["period"])].append(
                (int(r["previous_ts"]), int(r["next_ts"])))

def parse_utc(s):
    if not s: return None
    return int(dt.datetime.fromisoformat(s).timestamp() * 1000)

# ---------- 3. 逐币逐周期可用范围.csv ----------
rows_out = []
summary = defaultdict(lambda: {"n": 0, "merged": 0, "patch": 0, "gaps": 0, "slots": 0})
for r in cov:
    ex, mkt, sym, tf = r["exchange"], r["market"], r["symbol"], r["period"]
    merged = int(r["merged_unique_timestamps"])
    pr = patch_rows.get((ex, mkt, sym, tf), 0)
    first, last = parse_utc(r["first_utc"]), parse_utc(r["last_utc"])
    segs = sorted(runs.get((ex, sym, tf), []))
    longest = 0; longest_from = first; longest_to = last
    if first is not None and last is not None:
        cur = first
        for pv, nx in segs:
            if pv - cur > longest:
                longest = pv - cur; longest_from, longest_to = cur, pv
            cur = max(cur, nx)
        if last - cur > longest:
            longest = last - cur; longest_from, longest_to = cur, last
    days = round(longest / 86400000, 1) if longest else 0
    rows_out.append({
        "exchange": ex, "market": mkt, "symbol": sym, "period": tf,
        "cold_rows": r["cold_rows"], "hot_rows": r["hot_rows"],
        "merged_unique": merged, "patch_rows": pr, "merged_plus_patch": merged + pr,
        "first_utc": r["first_utc"], "last_utc": r["last_utc"],
        "internal_gap_segments": r["internal_gap_segments"],
        "missing_grid_slots": r["missing_grid_slots_candidate"],
        "longest_continuous_days": days,
        "longest_run_from_utc": iso(longest_from) if longest_from else "",
        "longest_run_to_utc": iso(longest_to) if longest_to else "",
        "offgrid_timestamps": r["offgrid_timestamps"],
    })
    s = summary[(ex, tf)]
    s["n"] += 1; s["merged"] += merged; s["patch"] += pr
    s["gaps"] += int(r["internal_gap_segments"]); s["slots"] += int(r["missing_grid_slots_candidate"])

with open(os.path.join(OUT, "逐币逐周期可用范围.csv"), "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
    w.writeheader(); w.writerows(rows_out)

with open(os.path.join(OUT, "逐币逐周期可用范围_汇总.json"), "w", encoding="utf-8") as f:
    json.dump({f"{k[0]}|{k[1]}": v for k, v in sorted(summary.items())}, f, ensure_ascii=False, indent=1)

# ---------- 4. 仍缺与处理建议.csv ----------
G = []
# 4.1 可补未补（补件已备，未落库）
G.append({"分类": "可补未补（补件已备未落库）", "对象": "K线补件 484,907 唯一行 / 162 键",
          "交易所": "binance+okx", "周期": "1d/12h/8h/4h/1h/30m/15m", "规模": 484907,
          "证据": "evidence/Work_补缺_2026-10-14/补缺_K线_*_import.csv；校验_全轴覆盖汇总.json",
          "处理建议": "按第5节顺序：补件验收→用户审核→正式入库→独立覆盖验收→备份核验",
          "是否阻塞策略": "是（M0-M3 若要求历史完整）"})
G.append({"分类": "可补未补（补件已备未落库）", "对象": "已结算资金费补件 11,773 条 / 420 币",
          "交易所": "binance", "周期": "结算事件", "规模": 11773,
          "证据": "evidence/Work_补缺_2026-10-14/补缺_资金费_import.csv",
          "处理建议": "同上；注意该补件无 mark_price，落库后结算mark仍缺",
          "是否阻塞策略": "是"})
G.append({"分类": "可补未补（补件已备未落库）", "对象": "OI补件 13,776 条（API 13,745 + 归档 31）",
          "交易所": "binance", "周期": "5m", "规模": 13776,
          "证据": "补缺_OI_API_import.csv / 补缺_OI_1008_1013_官方归档_import.csv",
          "处理建议": "落库前先统一归档/采集器 ts 桶口径（建议统一桶起点），否则同区间双标",
          "是否阻塞策略": "否（仅 2026-10-14 内部空档）"})
# 4.2 官方已证无事件
G.append({"分类": "官方已证无事件（合约停牌，官方确实没有）", "对象": "K线主线残差 1,609 槽 / 48 段 / 9 币",
          "交易所": "binance", "周期": "15m/30m/1h/4h/8h/12h/1d", "规模": 1609,
          "证据": "校验_官方包停牌证据_2026-10-15.csv；校验_官方逐笔成交停牌证据_2026-10-15.csv",
          "处理建议": "不补。落库时按停牌空洞显式标注，禁止用现货或插值填充",
          "是否阻塞策略": "否（需在回测中显式处理空洞）"})
G.append({"分类": "官方已证无事件（上市后前端无更早数据）", "对象": "上市后→首条数据前端空档 345 项 / 66 币",
          "交易所": "binance+okx", "周期": "多周期", "规模": 345,
          "证据": "校验_前端归档复核_2026-10-15.csv / …汇总.json（官方月包全量复核）",
          "处理建议": "不补。以各币实际首条数据时间为可用起点，前端空档不判缺失",
          "是否阻塞策略": "否"})
G.append({"分类": "官方已证无事件（源包无事件）", "对象": "OI 源包无事件段 7,869 段 / 676,673 槽 / 526 币",
          "交易所": "binance", "周期": "5m", "规模": 676673,
          "证据": "补缺_OI官方归档可用天_2026-10-15.json；校验_全轴覆盖汇总.json C_oi_by_status",
          "处理建议": "不补。M4 OI 因子必须按币分别核算可用窗口，不得用全市场平均覆盖代替",
          "是否阻塞策略": "是（M4 逐币可用窗口收窄）"})
# 4.3 网格/桶边界假缺口
G.append({"分类": "网格/桶边界假缺口（数据已在库，仅标签口径差一格）", "对象": "OI 接缝假缺口 545 槽（522 在 2026-10-08T00:00 切源处）",
          "交易所": "binance", "周期": "5m", "规模": 545,
          "证据": "补缺_OI接缝假缺口.csv / 补缺_OI接缝假缺口统计.json（实测 288/288 归档[t]=API[t+5m]）",
          "处理建议": "不补数据；入库前统一桶口径。OI 桶标签的解释须有逐事件时间字段证据，禁止随意移时",
          "是否阻塞策略": "是（口径不统一会双记同一 5m 区间）"})
# 4.4 原因未确认
G.append({"分类": "原因未确认（行数差，非逐时刻证明的删除事件）", "对象": "≤2025 旧年份 K线清单差 449,048 行 / 184 分片",
          "交易所": "binance+okx", "周期": "多周期", "规模": 449048,
          "证据": "校验_旧年份覆盖证明_2026-10-15.csv / …汇总.json（|A∪P|≥old_rows 且 A∩P=∅ ⇒ 184/184 covered）",
          "处理建议": "集合层已证被现有补件 100% 覆盖；仍缺“旧时间键集合”原始快照，无法逐键证明原键恢复 ⇒ 披露限制",
          "是否阻塞策略": "否（集合层已覆盖）"})
G.append({"分类": "原因未确认（无本地原件）", "对象": "OKX 资金费 / OI 冷档", "交易所": "okx",
          "周期": "结算/OI", "规模": 0,
          "证据": "验收报告 §3.6：raw/okx 仅 K 线 JSON，无资金费/OI 原件",
          "处理建议": "热库 OKX 资金费最早 2026-09-02、OI 快照最早 2026-10-05；如需长历史须另立官方 API 采集，不在本轮",
          "是否阻塞策略": "是（M0-M3 若含 OKX 资金费长历史）"})
G.append({"分类": "原因未确认（源目录缺失，未迁空壳）", "对象": "M019 OI 正式冷档 / M020 实际资金费正式冷档",
          "交易所": "-", "周期": "-", "规模": 0,
          "证据": "docs/项目逐项转移与人工审核清单_2026-10-14.md 第76-77行（源目录缺失）",
          "处理建议": "登记缺口，不迁空壳；与冷档 parquet 中 oi/ funding/ 目录的实际存在分别披露",
          "是否阻塞策略": "否"})
# 4.5 结构性不足
G.append({"分类": "结构性不足（无表无列无采集）", "对象": "历史多空比（long/short ratio）", "交易所": "binance",
          "周期": "5m", "规模": "未知",
          "证据": "生产库 26 表 + 1 视图全库检索无 ratio/long_short 列；raw_oi_metrics 原件含 4 个多空比列",
          "处理建议": "M5/M6 历史回测判定为不够；须新增 schema + 入库接线后方可研究，不以缺失默认放行",
          "是否阻塞策略": "是（M5/M6 直接阻塞）"})
G.append({"分类": "结构性不足（仅近 6 周）", "对象": "资金费结算 mark（funding_history.mark_price）",
          "交易所": "binance", "周期": "结算事件", "规模": 79363,
          "证据": "热库 mark 非空 79,363（2026-09-02~2026-10-14）；冷档 funding mark 抽样 0/259,202 非空；OKX mark 0/42,303",
          "处理建议": "历史结算 mark 需从 raw/mark1h（21,121 ZIP）+ raw/mark1d（21,078 ZIP）原件另立入库接线；本轮不实施",
          "是否阻塞策略": "是（若策略按结算 mark 计费/定价）"})

with open(os.path.join(OUT, "仍缺与处理建议.csv"), "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["分类", "对象", "交易所", "周期", "规模", "证据", "处理建议", "是否阻塞策略"])
    w.writeheader(); w.writerows(G)

# ---------- 5. 文件清单及SHA.csv ----------
F = []
def add(path, cat, note, do_sha=True, sha=""):
    if not os.path.exists(path):
        F.append({"类别": cat, "绝对路径": path, "bytes": "", "GiB": "", "sha256": "文件不存在", "说明": note})
        return
    b = os.path.getsize(path) if os.path.isfile(path) else 0
    if sha:
        hv = sha
    elif do_sha and os.path.isfile(path):
        hv = sha256(path)
    else:
        hv = "见 _catalog.json 逐分片sha256"
    F.append({"类别": cat, "绝对路径": path, "bytes": b,
              "GiB": round(b / 1024 ** 3, 4) if b else "",
              "sha256": hv, "说明": note})

# 正式热库
add(DB, "正式热库（生产，只读引用）", "SQLite 主文件；WAL 46,720,832 B 另行存在，不作为独立迁移文件",
    do_sha=False, sha="见 文件清单_SHA_大文件.json（后台单独计算）")
add(DB + "-wal", "正式热库（生产，只读引用）", "WAL 活动状态", do_sha=False)
# 正式冷档
add(os.path.join(ARCH, "_catalog.json"), "正式冷档（parquet 清单）", "32,785 分片清单，逐分片 sha256 内嵌")
add(os.path.join(ARCH, "_state.json"), "正式冷档（状态）", "冷热边界水位", do_sha=False)
# 独立补件
for fn in sorted(os.listdir(PREV)):
    if fn.startswith("补缺_") and fn.endswith(".csv"):
        add(os.path.join(PREV, fn), "独立补件（未落库）", "补缺任务交付；本轮只读复核")
# 本轮证据
for fn in sorted(os.listdir(OUT)):
    if fn.endswith((".json", ".csv", ".txt", ".md")):
        add(os.path.join(OUT, fn), "本轮证据", "本轮只读盘点/汇总产物")
# 原始归档（目录级，SHA 用官方 CHECKSUM）
for sub, note in (("raw", "官方K线ZIP（按周期分目录）"), ("raw_oi_metrics", "官方日度 metrics ZIP（OI+多空比列）"),
                  ("raw/funding", "官方资金费ZIP"), ("raw/mark1h", "官方1h标记价ZIP"),
                  ("raw/mark1d", "官方1d标记价ZIP"), ("raw/okx", "OKX JSON原件"),
                  ("raw_rest_gap", "API补缺原始响应")):
    p = os.path.join(RAW, sub)
    if os.path.isdir(p):
        n = int(os.popen(f'find "{p}" -type f 2>/dev/null | wc -l').read().strip() or 0)
        kb = int(os.popen(f'du -sk "{p}" 2>/dev/null | cut -f1').read().strip() or 0) * 1024
        F.append({"类别": "原始归档（ZIP+CHECKSUM，未入库）", "绝对路径": p, "bytes": kb,
                  "GiB": round(kb / 1024 ** 3, 4), "sha256": "逐文件官方 .CHECKSUM（未重算全量）", "说明": f"{note}；文件数 {n}"})

with open(os.path.join(OUT, "文件清单及SHA.csv"), "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["类别", "绝对路径", "bytes", "GiB", "sha256", "说明"])
    w.writeheader(); w.writerows(F)

print("rows_out", len(rows_out), "gaps", len(G), "files", len(F))
print("summary:", json.dumps({f"{k[0]}|{k[1]}": v for k, v in sorted(summary.items())}, ensure_ascii=False))
