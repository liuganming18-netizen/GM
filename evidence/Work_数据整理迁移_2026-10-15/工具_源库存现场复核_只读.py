#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""源库存现场复核（只读）——复核旧清单过时项，产出分组清单。
不写源、不写目标，仅读取 stat 与文件名。"""
import os, sys, json, csv, re, datetime

SRC_A = "/Users/boos/WorkBuddy AI/加密货币趋势判断系统"
SRC_B = "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现"
DL = SRC_B + "/历史下载"

# 组定义：(组号, 名称, 类别, 根, 相对路径, 来源类型, 是否在范围)
GROUPS = [
    # 源A 正式冷档
    ("G-K-CANDLES", "K线冷档 candles+candle_heads", "K线", SRC_A, "data/archive/parquet/candles", "已结构化(parquet)", True),
    ("G-K-HEADS", "K线头版本 candle_heads", "K线", SRC_A, "data/archive/parquet/candle_heads", "已结构化(parquet)", True),
    ("G-OI-COLD", "OI正式冷档 oi", "OI", SRC_A, "data/archive/parquet/oi", "已结构化(parquet)", True),
    ("G-SRC-PROV", "来源凭证 sources", "控制/来源", SRC_A, "data/archive/parquet/sources", "已结构化(parquet)", True),
    ("G-CATALOG", "冷档catalog/state/检查点", "控制/清单", SRC_A, "data/archive/parquet", "清单/状态", True),  # 仅直属文件
    ("G-HOTDB", "正式热库 market_v2.sqlite3", "K线+OI(混)", SRC_A, "data", "活动SQLite", True),  # 仅 market_v2*
    # 源B 原始归档
    ("G-RAW-1d", "raw 1d K线ZIP+CHECKSUM", "K线", DL, "raw/1d", "下载(ZIP)", True),
    ("G-RAW-12h", "raw 12h K线ZIP+CHECKSUM", "K线", DL, "raw/12h", "下载(ZIP)", True),
    ("G-RAW-8h", "raw 8h K线ZIP+CHECKSUM", "K线", DL, "raw/8h", "下载(ZIP)", True),
    ("G-RAW-4h", "raw 4h K线ZIP+CHECKSUM", "K线", DL, "raw/4h", "下载(ZIP)", True),
    ("G-RAW-1h", "raw 1h K线ZIP+CHECKSUM", "K线", DL, "raw/1h", "下载(ZIP)", True),
    ("G-RAW-30m", "raw 30m K线ZIP+CHECKSUM", "K线", DL, "raw/30m", "下载(ZIP)", True),
    ("G-RAW-15m", "raw 15m K线ZIP+CHECKSUM", "K线", DL, "raw/15m", "下载(ZIP)", True),
    ("G-RAW-5m", "raw 5m K线ZIP+CHECKSUM(冻结冷备)", "K线", DL, "raw/5m", "下载(ZIP)", True),
    ("G-RAW-OKX", "raw OKX官方历史原件", "K线", DL, "raw/okx", "下载(JSON)", True),
    ("G-RAW-OI", "raw_oi_metrics 币安日度OI ZIP+CHECKSUM", "OI", DL, "raw_oi_metrics", "下载(ZIP)", True),
    ("G-RESTGAP", "raw_rest_gap API补缺原始响应", "K线(API补缺)", DL, "raw_rest_gap", "API原始响应", True),
    ("G-POOL", "标的池/范围分类", "控制/分类", DL, "标的池", "分类(JSON)", True),
    ("G-STATE", "batch_state/oi_state/logs 断点状态", "控制/状态", DL, "batch_state", "状态/断点", True),
    ("G-STATE-OKX", "batch_state_okx 断点状态", "控制/状态", DL, "batch_state_okx", "状态/断点", True),
    ("G-OISTATE", "oi_state 逐文件核验记录", "控制/状态(OI)", DL, "oi_state", "状态/核验", True),
    ("G-LOGS", "下载任务日志", "控制/日志", DL, "logs", "日志", True),
    # 范围外(仅登记，不迁)
    ("X-RAW-FUNDING", "raw/funding 资金费原始归档[范围外]", "资金费", DL, "raw/funding", "下载(ZIP)", False),
    ("X-RAW-MARK1H", "raw/mark1h 标记价[范围外]", "mark", DL, "raw/mark1h", "下载(ZIP)", False),
    ("X-RAW-MARK1D", "raw/mark1d 标记价[范围外]", "mark", DL, "raw/mark1d", "下载(ZIP)", False),
    ("X-COLD-FUNDING", "冷档 funding[范围外]", "资金费", SRC_A, "data/archive/parquet/funding", "已结构化", False),
    ("X-PERP-REAL", "perp_datadir_real Freqtrade转换[混mark/funding]", "混合", DL, "perp_datadir_real", "Feather/JSON", False),
    ("X-PERP", "perp_datadir 旧转换试验[范围外]", "混合", DL, "perp_datadir", "试验", False),
]

def scan_dir(root, rel):
    base = os.path.join(root, rel)
    n = 0; logical = 0; alloc = 0; names = []
    if not os.path.isdir(base):
        return None
    for dp, dns, fns in os.walk(base):
        for f in fns:
            p = os.path.join(dp, f)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            n += 1; logical += st.st_size; alloc += st.st_blocks * 512
            if len(names) < 400000:
                names.append(f)
    return n, logical, alloc, names

def scan_files(root, rel, patterns):
    base = os.path.join(root, rel)
    n = 0; logical = 0; alloc = 0; names = []
    if not os.path.isdir(base):
        return None
    for f in sorted(os.listdir(base)):
        p = os.path.join(base, f)
        if not os.path.isfile(p):
            continue
        if patterns and not any(f.startswith(x) for x in patterns):
            continue
        st = os.lstat(p)
        n += 1; logical += st.st_size; alloc += st.st_blocks * 512; names.append(f)
    return n, logical, alloc, names

def timerange(names):
    """从文件名推断覆盖起止（年-月 / 年-月-日）"""
    months = set(); days = set(); years = set()
    for f in names:
        m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", f)
        if m:
            days.add(m.group(0)); continue
        m = re.search(r"(20\d{2})-(\d{2})", f)
        if m:
            months.add(m.group(0)); continue
        m = re.search(r"(20\d{2})", f)
        if m:
            years.add(m.group(0))
    if days:
        return min(days), max(days), "日"
    if months:
        return min(months)+"-01", max(months)+"-31", "月"
    if years:
        return min(years)+"-01-01", max(years)+"-12-31", "年"
    return "", "", ""

def main():
    out = []
    for gid, name, cat, root, rel, kind, inscope in GROUPS:
        if gid == "G-CATALOG":
            r = scan_files(root, rel, ["_catalog.json", "_state.json", "_rebuild_progress.json"])
        elif gid == "G-HOTDB":
            r = scan_files(root, rel, ["market_v2.sqlite3"])
        else:
            r = scan_dir(root, rel)
        if r is None:
            out.append(dict(组号=gid, 名称=name, 类别=cat, 源根=root, 源相对=rel, 来源类型=kind,
                            在范围=inscope, 存在=False, 文件数=0, 逻辑字节=0, GiB=0.0,
                            分配字节=0, 分配GiB=0.0, 时间起="", 时间止="", 时间粒度=""))
            continue
        n, logical, alloc, names = r
        t0, t1, tg = timerange(names)
        out.append(dict(组号=gid, 名称=name, 类别=cat, 源根=root, 源相对=rel, 来源类型=kind,
                        在范围=inscope, 存在=True, 文件数=n, 逻辑字节=logical,
                        GiB=round(logical/1073741824, 4), 分配字节=alloc,
                        分配GiB=round(alloc/1073741824, 4), 时间起=t0, 时间止=t1, 时间粒度=tg))
        print(f"{gid:14s} files={n:>8d} logical={logical/1073741824:8.3f}GiB alloc={alloc/1073741824:8.3f}GiB range=[{t0}~{t1}]", flush=True)

    outp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "源库存_分组清单.csv")
    with open(outp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader(); w.writerows(out)
    # 汇总
    ins = [o for o in out if o["在范围"] and o["存在"]]
    outs = [o for o in out if not o["在范围"] and o["存在"]]
    tot_in = sum(o["分配字节"] for o in ins)
    tot_out = sum(o["分配字节"] for o in outs)
    print(f"\n[在范围] 组数={len(ins)} 分配合计={tot_in/1073741824:.3f}GiB")
    print(f"[范围外] 组数={len(outs)} 分配合计={tot_out/1073741824:.3f}GiB")
    print("写出:", outp)

if __name__ == "__main__":
    main()
