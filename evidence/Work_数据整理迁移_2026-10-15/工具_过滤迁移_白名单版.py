#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""K线+OI 迁移（白名单过滤版 + 原生批量校验）：
- 白名单：币安526（underlyingType=COIN，排除指数 BTCDOMUSDT/ALLUSDT）；OKX 23。
- 逐"币种子目录"克隆（ditto/clonefile），散落文件按文件名币种判定；不对整周期父目录全拷。
- 目标冲突不覆盖；空间守卫 floor=12.2GiB；保留源原件。
- 逐文件校验改用原生批量：find -exec stat / shasum / cat（Python 逐文件 I/O 在本沙箱约 6.5ms/文件，原生约 0.02~0.6ms/文件）。
用法: python 工具_过滤迁移_白名单版.py [--dry-run] [--only gid,...] [--floor-gib 12.2]
"""
import os, sys, json, csv, re, subprocess, time, hashlib, argparse

POOL = "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载/标的池"
ROOT = "/Users/boos/Desktop/威科夫因子共振趋势"
SRC_A = "/Users/boos/WorkBuddy AI/加密货币趋势判断系统"
DL = "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载"
EVID = ROOT + "/evidence/Work_数据整理迁移_2026-10-15"
FIND, STAT, SHASUM, CAT = "/usr/bin/find", "/usr/bin/stat", "/usr/bin/shasum", "/bin/cat"

def load_wl():
    sc = json.load(open(POOL + "/scope_crypto.json"))
    return set(x["symbol"] for x in sc["binance_items"]), set(sc["okx_symbols"])

GROUPS = [
    ("G-RESTGAP",   "API补缺原始响应",        "K线",  "API原始响应", ("plain", "raw_rest_gap", "数据/raw_rest_gap", None)),
    ("G-POOL",      "标的池/范围分类",         "控制", "分类",        ("plain", "标的池", "数据/contracts/旧标的池", None)),
    ("G-LOGS",      "下载任务日志",           "控制", "日志",        ("plain", "logs", "回测日志/历史下载状态/logs", None)),
    ("G-STATE-OKX", "batch_state_okx 断点",   "控制", "状态",        ("plain", "batch_state_okx", "回测日志/历史下载状态/batch_state_okx", None)),
    ("G-STATE",     "batch_state 断点",        "控制", "状态",        ("plain", "batch_state", "回测日志/历史下载状态/batch_state", None)),
    ("G-CATALOG",   "冷档catalog/state",      "控制", "清单",        ("files", "data/archive/parquet", "数据/archive/manifests/旧清单截点", ["_catalog.json", "_state.json", "_rebuild_progress.json"])),
    ("G-OISTATE",   "oi_state 逐文件核验",     "控制", "状态(OI)",    ("symdir", "oi_state", "回测日志/历史下载状态/oi_state", "binance")),
    ("G-SRC-PROV",  "来源凭证 sources",        "控制", "已结构化",    ("symtree", "data/archive/parquet/sources", "数据/archive/parquet/sources", [("binance", "um", "binance"), ("okx", "swap", "okx")])),
    ("G-RAW-1d",    "raw 1d K线ZIP",          "K线",  "下载(ZIP)",   ("rawperiod", "raw/1d", "数据/raw/1d", "binance")),
    ("G-RAW-12h",   "raw 12h K线ZIP",         "K线",  "下载(ZIP)",   ("rawperiod", "raw/12h", "数据/raw/12h", "binance")),
    ("G-RAW-8h",    "raw 8h K线ZIP",          "K线",  "下载(ZIP)",   ("rawperiod", "raw/8h", "数据/raw/8h", "binance")),
    ("G-RAW-4h",    "raw 4h K线ZIP",          "K线",  "下载(ZIP)",   ("rawperiod", "raw/4h", "数据/raw/4h", "binance")),
    ("G-RAW-1h",    "raw 1h K线ZIP",          "K线",  "下载(ZIP)",   ("rawperiod", "raw/1h", "数据/raw/1h", "binance")),
    ("G-RAW-30m",   "raw 30m K线ZIP",         "K线",  "下载(ZIP)",   ("rawperiod", "raw/30m", "数据/raw/30m", "binance")),
    ("G-RAW-15m",   "raw 15m K线ZIP",         "K线",  "下载(ZIP)",   ("rawperiod", "raw/15m", "数据/raw/15m", "binance")),
    ("G-RAW-5m",    "raw 5m K线ZIP",          "K线",  "下载(ZIP)",   ("rawperiod", "raw/5m", "数据/raw/5m", "binance")),
    ("G-RAW-OKX",   "raw OKX官方历史原件",     "K线",  "下载(JSON)",  ("rawokx", "raw/okx", "数据/raw/okx", "okx")),
    ("G-OI-COLD",   "OI正式冷档",             "OI",   "已结构化",    ("symtree", "data/archive/parquet/oi", "数据/archive/parquet/oi", [("binance", "um", "binance")])),
    ("G-K-CANDLES", "K线冷档 candles",        "K线",  "已结构化",    ("symtree", "data/archive/parquet/candles", "数据/archive/parquet/candles", [("binance", "um", "binance"), ("okx", "swap", "okx")])),
    ("G-K-HEADS",   "K线头版本 candle_heads", "K线",  "已结构化",    ("symtree", "data/archive/parquet/candle_heads", "数据/archive/parquet/candle_heads", [("binance", "um", "binance"), ("okx", "swap", "okx")])),
    ("G-RAW-OI",    "raw_oi_metrics OI ZIP",  "OI",   "下载(ZIP)",   ("symdir", "raw_oi_metrics", "数据/raw_oi_metrics", "binance")),
]

def free_bytes(p):
    st = os.statvfs(p); return st.f_bavail * st.f_frsize

def sym_of_file(fname, tf):
    m = re.match(r"([A-Z0-9]+)-" + re.escape(tf) + r"-", fname)
    return m.group(1) if m else None

def count_tree(p):
    if os.path.isfile(p):
        return 1, os.path.getsize(p)
    n = 0; b = 0
    for dp, _, fs in os.walk(p):
        for f in fs:
            n += 1
            try: b += os.path.getsize(os.path.join(dp, f))
            except OSError: pass
    return n, b

# ---------- 原生批量校验 ----------
def native_stat_tree(root):
    """{abspath: (size, inode, nlink)}"""
    r = subprocess.run([FIND, root, "-type", "f", "-exec", STAT, "-f", "%z|%i|%l|%N", "{}", "+"],
                       capture_output=True, text=True)
    out = {}
    for ln in r.stdout.splitlines():
        pr = ln.split("|", 3)
        if len(pr) == 4:
            try: out[pr[3]] = (int(pr[0]), int(pr[1]), int(pr[2]))
            except ValueError: pass
    return out

def native_sha_tree(root):
    """{abspath: sha256hex}"""
    r = subprocess.run([FIND, root, "-type", "f", "-exec", SHASUM, "-a", "256", "{}", "+"],
                       capture_output=True, text=True)
    out = {}
    for ln in r.stdout.splitlines():
        if "  " in ln:
            sha, path = ln.split("  ", 1); out[path] = sha.lower()
        else:
            pr = ln.split(None, 1)
            if len(pr) == 2: out[pr[1].lstrip("*")] = pr[0].lower()
    return out

def native_sha_files(paths):
    out = {}
    for i in range(0, len(paths), 400):
        chunk = paths[i:i + 400]
        if not chunk: continue
        r = subprocess.run([SHASUM, "-a", "256"] + chunk, capture_output=True, text=True)
        for ln in r.stdout.splitlines():
            if "  " in ln:
                sha, path = ln.split("  ", 1); out[path] = sha.lower()
            else:
                pr = ln.split(None, 1)
                if len(pr) == 2: out[pr[1].lstrip("*")] = pr[0].lower()
    return out

def native_cks_tree(root):
    """{zipbasename: expected_sha256}"""
    r = subprocess.run([FIND, root, "-name", "*.CHECKSUM", "-exec", CAT, "{}", "+"], capture_output=True, text=True)
    m = {}
    for ln in r.stdout.splitlines():
        pr = ln.split()
        if len(pr) >= 2:
            m[pr[-1].lstrip("*")] = pr[0].lower()
    return m

def collect_manifest_rows(dst, isd, src):
    """返回 [(srcpath, dstpath, size, ino, nlink, sha, src_sha_same, ck)]"""
    if isd:
        stt = native_stat_tree(dst)
        shas = native_sha_tree(dst)
        cks = native_cks_tree(dst)
    else:
        st = os.stat(dst)
        stt = {dst: (st.st_size, st.st_ino, st.st_nlink)}
        shas = native_sha_files([dst])
        cks = native_cks_tree(os.path.dirname(dst)) if dst.endswith(".zip") else {}
    # 非 zip/CHECKSUM 文件补算源 SHA（数量少），zip 用官方 CHECKSUM
    nonzip = [(p, os.path.join(src, os.path.relpath(p, dst)) if isd else src)
              for p in shas if not (p.endswith(".zip") or p.endswith(".CHECKSUM"))]
    src_sha = native_sha_files([s for _, s in nonzip]) if nonzip else {}
    rows = []
    for p in sorted(stt.keys()):
        sz, ino, nlink = stt[p]
        sha = shas.get(p, "")
        rel = os.path.relpath(p, dst) if isd else os.path.basename(p)
        sp = os.path.join(src, rel) if isd else src
        same = ""
        if not p.endswith(".zip"):
            ss = src_sha.get(sp)
            if ss: same = "True" if ss == sha else "False"
        ck = ""
        if p.endswith(".zip"):
            exp = cks.get(os.path.basename(p))
            if exp: ck = "官方CHECKSUM=MATCH" if exp == sha else "官方CHECKSUM=MISMATCH"
        rows.append((sp, p, sz, ino, nlink, sha, same, ck))
    return rows

def plan(group, WL):
    gid, name, cat, kind, spec = group
    sk = spec[0]
    items = []       # (src, dst, sym, is_dir)
    excl = []        # (src, sym)
    if sk == "plain":
        src = os.path.join(SRC_A, spec[1]) if spec[1].startswith("data/") else os.path.join(DL, spec[1])
        dst = os.path.join(ROOT, spec[2])
        if os.path.exists(src):
            items.append((src, dst, "-", True))
    elif sk == "files":
        base = os.path.join(SRC_A, spec[1]); dstb = os.path.join(ROOT, spec[2])
        for f in sorted(os.listdir(base)):
            if os.path.isfile(os.path.join(base, f)) and any(f.startswith(x) for x in spec[3]):
                items.append((os.path.join(base, f), os.path.join(dstb, f), f, False))
    elif sk == "rawperiod":
        base = os.path.join(DL, spec[1]); dstb = os.path.join(ROOT, spec[2]); tf = spec[1].split("/")[-1]
        wl = WL[spec[3]]
        if os.path.isdir(base):
            for e in sorted(os.listdir(base)):
                p = os.path.join(base, e)
                if os.path.isdir(p):
                    (items if e in wl else excl).append((p, os.path.join(dstb, e), e, True) if e in wl else (p, e))
                else:
                    s = sym_of_file(e, tf)
                    if s in wl: items.append((p, os.path.join(dstb, e), s, False))
                    else: excl.append((p, s or e))
    elif sk == "rawokx":
        base = os.path.join(DL, spec[1]); dstb = os.path.join(ROOT, spec[2]); wl = WL[spec[3]]
        if os.path.isdir(base):
            for tf in sorted(os.listdir(base)):
                d = os.path.join(base, tf)
                if not os.path.isdir(d): continue
                for ent in sorted(os.listdir(d)):
                    p = os.path.join(d, ent)
                    s = ent[:-5] if ent.endswith(".json") else ent
                    if s in wl: items.append((p, os.path.join(dstb, tf, ent), s, os.path.isdir(p)))
                    else: excl.append((p, s))
    elif sk == "symdir":
        base = os.path.join(DL, spec[1]) if not spec[1].startswith("data/") else os.path.join(SRC_A, spec[1])
        dstb = os.path.join(ROOT, spec[2]); wl = WL[spec[3]]
        if os.path.isdir(base):
            for e in sorted(os.listdir(base)):
                p = os.path.join(base, e)
                s = e[:-5] if e.endswith(".json") else e
                if s in wl: items.append((p, os.path.join(dstb, e), s, os.path.isdir(p)))
                else: excl.append((p, s))
    elif sk == "symtree":
        base = os.path.join(SRC_A, spec[1]); dstb = os.path.join(ROOT, spec[2])
        for ex, mkt, wlk in spec[3]:
            d = os.path.join(base, ex, mkt)
            if not os.path.isdir(d): continue
            wl = WL[wlk]
            for sym in sorted(os.listdir(d)):
                p = os.path.join(d, sym)
                if sym in wl: items.append((p, os.path.join(dstb, ex, mkt, sym), sym, True))
                else: excl.append((p, sym))
    return items, excl

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--floor-gib", type=float, default=12.2)
    args = ap.parse_args()
    only = set(x for x in args.only.split(",") if x)
    floor = int(args.floor_gib * (1 << 30))
    wb, wo = load_wl()
    WL = {"binance": wb, "okx": wo}
    print(f"白名单 币安={len(wb)} OKX={len(wo)}  排除指数=BTCDOMUSDT,ALLUSDT  floor={args.floor_gib}GiB", flush=True)

    summary = []
    item_rows = []
    for group in GROUPS:
        gid = group[0]
        if only and gid not in only: continue
        items, excl = plan(group, WL)
        af = ab = 0
        for src, dst, sym, isd in items:
            n, b = count_tree(src); af += n; ab += b
        ef = eb = 0
        for src, sym in excl:
            n, b = count_tree(src); ef += n; eb += b
        tgt = 0
        for src, dst, sym, isd in items:
            if os.path.exists(dst): tgt += 1
        rec = dict(组号=gid, 名称=group[1], 类别=group[2], 来源类型=group[3],
                   允许项=len(items), 允许文件=af, 允许GiB=round(ab / (1 << 30), 4),
                   排除项=len(excl), 排除文件=ef, 排除GiB=round(eb / (1 << 30), 4),
                   目标已存在项=tgt, 排除样例=",".join(s for _, s in excl[:8]))
        summary.append(rec)
        print(f"{gid:13s} 允许项={len(items):>4d} 文件={af:>7d} {ab/(1<<20):9.2f}MiB | 排除项={len(excl):>4d} 文件={ef:>6d} {eb/(1<<20):8.2f}MiB | 目标已存在={tgt}", flush=True)
        if args.dry_run:
            for src, dst, sym, isd in items[:5]:
                item_rows.append(dict(组号=gid, 符号=sym, 源=src, 目标=dst, 类型="允许"))
            for src, sym in excl[:50]:
                item_rows.append(dict(组号=gid, 符号=sym, 源=src, 目标="", 类型="排除"))

    tot = dict(允许项=sum(r["允许项"] for r in summary), 允许文件=sum(r["允许文件"] for r in summary),
               允许GiB=round(sum(r["允许GiB"] for r in summary), 3),
               排除项=sum(r["排除项"] for r in summary), 排除文件=sum(r["排除文件"] for r in summary),
               排除GiB=round(sum(r["排除GiB"] for r in summary), 3))
    print("\n合计:", json.dumps(tot, ensure_ascii=False))
    tag = "dry" if args.dry_run else "exec"
    with open(f"{EVID}/过滤迁移_分组_{tag}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(summary[0].keys())); w.writeheader(); w.writerows(summary)
    if args.dry_run:
        with open(f"{EVID}/过滤迁移_明细样例_dry.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["组号", "符号", "源", "目标", "类型"]); w.writeheader(); w.writerows(item_rows)
    else:
        exec_all(WL, floor, args)

def exec_all(WL, floor, args):
    grp_res = []; man_path = EVID + "/过滤迁移_逐文件清单.csv"
    man = open(man_path, "w", newline="", encoding="utf-8-sig")
    mw = csv.writer(man)
    mw.writerow(["组号","类别","来源类型","交易所","币种","周期或字段","源绝对路径","目标绝对路径","逻辑字节","sha256","源SHA相同","官方校验","目标inode","目标links","状态","备注"])
    aborted = False
    for group in GROUPS:
        gid = group[0]
        if args.only and gid not in set(x for x in args.only.split(",") if x): continue
        items, excl = plan(group, WL)
        n_files = 0
        for s, _, _, _ in items:
            n_files += count_tree(s)[0]
        est_meta = n_files * 1024
        fr = free_bytes(ROOT)
        if fr - est_meta < floor:
            print(f"[停止] {gid} 空间不足(元数据≈{est_meta/1048576:.1f}MiB 空闲{fr/(1<<30):.2f}GiB)")
            grp_res.append(dict(组号=gid, 状态="未迁(空间不足)")); aborted = True; break
        t0 = time.time(); f0 = free_bytes(ROOT)
        n_new = n_skip = n_err = 0
        for idx, (src, dst, sym, isd) in enumerate(items):
            if idx % 300 == 0 and free_bytes(ROOT) < floor:
                print(f"[停止] {gid} 实时空间跌破 floor，已迁 {n_new} 项"); aborted = True; break
            if os.path.exists(dst):
                n_skip += 1
                mw.writerow([gid, group[2], group[3], "", sym, "", src, dst, "", "", "", "", "", "", "已存在-跳过", "目标已有"])
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                if isd:
                    r = subprocess.run(["/usr/bin/ditto", src, dst], capture_output=True, text=True)
                else:
                    r = subprocess.run(["/bin/cp", "-c", src, dst], capture_output=True, text=True)
                if r.returncode != 0:
                    n_err += 1; mw.writerow([gid, group[2], group[3], "", sym, "", src, dst, "", "", "", "", "", "", "失败", r.stderr[:200]]); continue
            except Exception as e:
                n_err += 1; mw.writerow([gid, group[2], group[3], "", sym, "", src, dst, "", "", "", "", "", "", "失败", repr(e)[:200]]); continue
            n_new += 1
            try:
                for sp, p, sz, ino, nlink, sha, same, ck in collect_manifest_rows(dst, isd, src):
                    mw.writerow([gid, group[2], group[3], "", sym, "", sp, p, sz, sha, same, ck, ino, nlink, "新增", ""])
            except Exception as e:
                mw.writerow([gid, group[2], group[3], "", sym, "", src, dst, "", "", "", "", "", "", "新增(校验异常)", repr(e)[:150]])
        dt = time.time() - t0; f1 = free_bytes(ROOT)
        grp_res.append(dict(组号=gid, 状态="已迁", 新增项=n_new, 跳过项=n_skip, 失败项=n_err, 排除项=len(excl),
                            用时s=round(dt), df消耗MiB=round((f0 - f1) / 1048576, 1)))
        print(f"[完成] {gid:13s} 新增={n_new} 跳过={n_skip} 失败={n_err} 排除项={len(excl)} {dt:.0f}s df={(f0-f1)/1048576:.1f}MiB 空闲={free_bytes(ROOT)/(1<<30):.2f}GiB", flush=True)
        if aborted: break
    man.close()
    with open(EVID + "/过滤迁移_执行汇总.json", "w", encoding="utf-8") as f:
        json.dump(dict(迁移后空闲GiB=round(free_bytes(ROOT)/(1<<30), 3), 中止=aborted, 逐组=grp_res), f, ensure_ascii=False, indent=1)
    try:
        import gzip, shutil as _sh
        with open(man_path, "rb") as fi, gzip.open(man_path + ".gz", "wb", compresslevel=6) as fo:
            _sh.copyfileobj(fi, fo, 1 << 20)
        os.remove(man_path)
        print(f"[清单] 已压缩 {man_path}.gz  大小={os.path.getsize(man_path+'.gz')/1048576:.1f}MiB", flush=True)
    except Exception as e:
        print(f"[清单] 压缩跳过: {e!r}", flush=True)

if __name__ == "__main__":
    main()
