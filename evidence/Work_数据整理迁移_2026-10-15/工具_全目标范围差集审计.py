#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全目标范围差集审计：把「所有已迁目标位置」的符号名与官方白名单做完整差集。
- 白名单：币安 526（underlyingType=COIN，排除指数）+ OKX 23 补充标的。
- 覆盖：raw 各周期(目录+散落文件)、raw_oi_metrics、raw/okx、parquet(candles/candle_heads/oi/sources)、oi_state。
- 对每个「不在范围内」的目标项：逐文件核验「源仍在 + SHA256 相同」，再列清单；--apply 时移除（不动源/不动旧策略输入）。
用法: python 工具_全目标范围差集审计.py [--apply]
"""
import os, sys, json, csv, re, subprocess, hashlib, argparse

POOL = "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载/标的池"
ROOT = "/Users/boos/Desktop/威科夫因子共振趋势"
SRC_A = "/Users/boos/WorkBuddy AI/加密货币趋势判断系统"
DL = "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载"
EVID = ROOT + "/evidence/Work_数据整理迁移_2026-10-15"
FIND, SHASUM = "/usr/bin/find", "/usr/bin/shasum"

def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
    return h.hexdigest()

def sha_files(paths):
    out = {}
    for i in range(0, len(paths), 300):
        ch = [p for p in paths[i:i + 300] if os.path.exists(p)]
        if not ch: continue
        r = subprocess.run([SHASUM, "-a", "256"] + ch, capture_output=True, text=True)
        for ln in r.stdout.splitlines():
            if "  " in ln:
                a, b = ln.split("  ", 1); out[b] = a.lower()
    return out

def rm_rf(p):
    subprocess.run(["/usr/bin/chflags", "-R", "nouchg", p], capture_output=True)
    subprocess.run(["/bin/chmod", "-R", "u+w", p], capture_output=True)
    r = subprocess.run(["/bin/rm", "-rf", p], capture_output=True, text=True)
    return r.returncode

def walk_files(d):
    if os.path.isfile(d): return [d]
    out = []
    for dp, _, fs in os.walk(d):
        for f in fs: out.append(os.path.join(dp, f))
    return out

def sym_of_file(fname, tf):
    m = re.match(r"([A-Z0-9]+)-" + re.escape(tf) + r"-", fname)
    return m.group(1) if m else None

def scan_side(root, wl, kind, tf=None):
    """返回 (在范围符号集, 越界符号集)。"""
    ins, oos = set(), set()
    if not os.path.isdir(root): return ins, oos
    if kind == "rawperiod":
        for e in sorted(os.listdir(root)):
            p = os.path.join(root, e)
            if os.path.isdir(p):
                (ins if e in wl else oos).add(e)
            else:
                s = sym_of_file(e, tf)
                (ins if s in wl else oos).add(s or e)
    elif kind == "symdir":
        for e in sorted(os.listdir(root)):
            (ins if e in wl else oos).add(e)
    elif kind == "symfile":
        for e in sorted(os.listdir(root)):
            s = e[:-5] if e.endswith(".json") else e
            (ins if s in wl else oos).add(s)
    elif kind == "okxfiles":
        for tf2 in sorted(os.listdir(root)):
            d = os.path.join(root, tf2)
            if not os.path.isdir(d): continue
            for e in sorted(os.listdir(d)):
                s = e[:-5] if e.endswith(".json") else e
                (ins if s in wl else oos).add(s)
    return ins, oos

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    sc = json.load(open(POOL + "/scope_crypto.json"))
    WB = set(x["symbol"] for x in sc["binance_items"]); WO = set(sc["okx_symbols"])
    print(f"白名单 币安={len(WB)} OKX={len(WO)}  apply={args.apply}", flush=True)

    # (位置名, 目标根, 源根, 白名单, 类型)
    LOCS = [
        ("raw/1d", f"{ROOT}/数据/raw/1d", f"{DL}/raw/1d", WB, "rawperiod"),
        ("raw/12h", f"{ROOT}/数据/raw/12h", f"{DL}/raw/12h", WB, "rawperiod"),
        ("raw/8h", f"{ROOT}/数据/raw/8h", f"{DL}/raw/8h", WB, "rawperiod"),
        ("raw/4h", f"{ROOT}/数据/raw/4h", f"{DL}/raw/4h", WB, "rawperiod"),
        ("raw/1h", f"{ROOT}/数据/raw/1h", f"{DL}/raw/1h", WB, "rawperiod"),
        ("raw/30m", f"{ROOT}/数据/raw/30m", f"{DL}/raw/30m", WB, "rawperiod"),
        ("raw/15m", f"{ROOT}/数据/raw/15m", f"{DL}/raw/15m", WB, "rawperiod"),
        ("raw/5m", f"{ROOT}/数据/raw/5m", f"{DL}/raw/5m", WB, "rawperiod"),
        ("raw_oi_metrics", f"{ROOT}/数据/raw_oi_metrics", f"{DL}/raw_oi_metrics", WB, "symdir"),
        ("raw/okx", f"{ROOT}/数据/raw/okx", f"{DL}/raw/okx", WO, "okxfiles"),
        ("parquet/candles/binance", f"{ROOT}/数据/archive/parquet/candles/binance/um", f"{SRC_A}/data/archive/parquet/candles/binance/um", WB, "symdir"),
        ("parquet/candles/okx", f"{ROOT}/数据/archive/parquet/candles/okx/swap", f"{SRC_A}/data/archive/parquet/candles/okx/swap", WO, "symdir"),
        ("parquet/candle_heads/binance", f"{ROOT}/数据/archive/parquet/candle_heads/binance/um", f"{SRC_A}/data/archive/parquet/candle_heads/binance/um", WB, "symdir"),
        ("parquet/candle_heads/okx", f"{ROOT}/数据/archive/parquet/candle_heads/okx/swap", f"{SRC_A}/data/archive/parquet/candle_heads/okx/swap", WO, "symdir"),
        ("parquet/oi/binance", f"{ROOT}/数据/archive/parquet/oi/binance/um", f"{SRC_A}/data/archive/parquet/oi/binance/um", WB, "symdir"),
        ("parquet/sources/binance", f"{ROOT}/数据/archive/parquet/sources/binance/um", f"{SRC_A}/data/archive/parquet/sources/binance/um", WB, "symdir"),
        ("parquet/sources/okx", f"{ROOT}/数据/archive/parquet/sources/okx/swap", f"{SRC_A}/data/archive/parquet/sources/okx/swap", WO, "symdir"),
        ("oi_state", f"{ROOT}/回测日志/历史下载状态/oi_state", f"{DL}/oi_state", WB, "symfile"),
    ]

    rows = []; summary = []
    for loc, tdir, sdir, wl, kind in LOCS:
        if not os.path.isdir(tdir):
            summary.append(dict(位置=loc, 目标项=0, 在范围=0, 越界=0, 备注="目标不存在")); continue
        inscope = 0; oos = []
        if kind == "rawperiod":
            tf = loc.split("/")[-1]
            for e in sorted(os.listdir(tdir)):
                p = os.path.join(tdir, e)
                if os.path.isdir(p):
                    if e in wl: inscope += 1
                    else: oos.append((e, p, True))
                else:
                    s = sym_of_file(e, tf)
                    if s in wl: inscope += 1
                    else: oos.append((s or e, p, False))
        elif kind == "symdir":
            for e in sorted(os.listdir(tdir)):
                p = os.path.join(tdir, e)
                if e in wl: inscope += 1
                else: oos.append((e, p, os.path.isdir(p)))
        elif kind == "symfile":
            for e in sorted(os.listdir(tdir)):
                p = os.path.join(tdir, e)
                s = e[:-5] if e.endswith(".json") else e
                if s in wl: inscope += 1
                else: oos.append((s, p, os.path.isdir(p)))
        elif kind == "okxfiles":
            for tf in sorted(os.listdir(tdir)):
                d = os.path.join(tdir, tf)
                if not os.path.isdir(d): continue
                for e in sorted(os.listdir(d)):
                    p = os.path.join(d, e)
                    s = e[:-5] if e.endswith(".json") else e
                    if s in wl: inscope += 1
                    else: oos.append((s, p, os.path.isdir(p)))
        # 越界逐文件核验
        n_oos_files = 0; n_src_missing = 0; n_sha_diff = 0; oos_bytes = 0
        removed = 0
        for sym, tp, isd in oos:
            rel = os.path.relpath(tp, tdir)
            sp = os.path.join(sdir, rel)
            files = walk_files(tp)
            ok_all = True
            srcs = []; dsts = []
            for f in files:
                relf = os.path.relpath(f, tp) if isd else os.path.basename(f)
                sf = os.path.join(sp, relf) if isd else os.path.join(os.path.dirname(sp), relf)
                n_oos_files += 1
                try: oos_bytes += os.path.getsize(f)
                except OSError: pass
                if not os.path.exists(sf):
                    n_src_missing += 1; ok_all = False
                else:
                    srcs.append(sf); dsts.append(f)
            hs = sha_files(srcs); hd = sha_files(dsts)
            same = all(hs.get(s) == hd.get(d) for s, d in zip(srcs, dsts)) if srcs else True
            if not same: n_sha_diff += 1; ok_all = False
            rows.append(dict(位置=loc, 名称=sym, 类型=("目录" if isd else "文件"), 目标绝对=tp, 源绝对=sp,
                             文件数=len(files), 逻辑字节=sum(os.path.getsize(f) for f in files if os.path.exists(f)),
                             源全存在=(n_src_missing == 0), SHA全相同=same,
                             判定=("越界-排除" if sym not in ("BTCDOMUSDT", "ALLUSDT", "DEFIUSDT") else "指数-排除")))
            if ok_all and args.apply:
                rc = rm_rf(tp)
                removed += 1 if (rc == 0 and not os.path.exists(tp)) else 0
        summary.append(dict(位置=loc, 目标项=inscope + len(oos), 在范围=inscope, 越界=len(oos),
                            越界文件=n_oos_files, 越界字节=oos_bytes, 源缺失=n_src_missing, SHA不同=n_sha_diff,
                            已移除=(removed if args.apply else 0)))
        print(f"{loc:32s} 在范围={inscope:>4d} 越界={len(oos):>4d} 越界文件={n_oos_files:>6d} 源缺失={n_src_missing} SHA不同={n_sha_diff}"
              + (f" 已移除={removed}" if args.apply else ""), flush=True)

    # 源侧完整差集（「排除清单」：源里存在但不在白名单的符号）
    src_diff = []
    for loc, tdir, sdir, wl, kind in LOCS:
        tf = loc.split("/")[-1] if kind == "rawperiod" else None
        si, so = scan_side(sdir, wl, kind, tf)
        src_diff.append(dict(位置=loc, 源在范围=len(si), 源越界=len(so), 越界符号=sorted(so)))
        print(f"  源侧 {loc:30s} 在范围={len(si):>4d} 越界={len(so):>4d} {sorted(so)[:6]}", flush=True)

    with open(EVID + "/全目标范围差集.csv", "w", newline="", encoding="utf-8-sig") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    with open(EVID + "/全目标范围差集_汇总.json", "w", encoding="utf-8") as f:
        json.dump(dict(白名单币安=len(WB), 白名单OKX=len(WO), apply=args.apply,
                       目标越界项总数=len(rows),
                       目标越界源全存在=all(r["源全存在"] for r in rows) if rows else None,
                       目标越界SHA全相同=all(r["SHA全相同"] for r in rows) if rows else None,
                       目标逐位置=summary,
                       源侧完整差集=src_diff,
                       源侧越界合计=sum(x["源越界"] for x in src_diff)), f, ensure_ascii=False, indent=1)
    print(f"\n目标越界项={len(rows)}  源侧越界合计={sum(x['源越界'] for x in src_diff)}")
    print(f"目标越界 源全存在={all(r['源全存在'] for r in rows) if rows else 'N/A(无越界)'}  SHA全相同={all(r['SHA全相同'] for r in rows) if rows else 'N/A(无越界)'}")

if __name__ == "__main__":
    main()
