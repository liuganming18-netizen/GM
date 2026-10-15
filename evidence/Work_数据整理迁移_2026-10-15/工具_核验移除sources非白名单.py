#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核验并移除「本任务」拷入目标的 sources 非白名单副本。
- 目标 sources 树 birth=2026-10-15 06:32-06:33（本任务创建），只处理其中非白名单目录。
- 逐文件核验：源存在 + SHA256 相同；全部通过才移除该目录（chflags nouchg + chmod u+w + rm -rf）。
- 不动源、不动旧策略输入（数据/研究输入）、不动其他目标已有文件。
用法: python 工具_核验移除sources非白名单.py [--dry-run]
"""
import os, sys, json, csv, hashlib, subprocess, time, argparse

POOL = "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载/标的池"
ROOT = "/Users/boos/Desktop/威科夫因子共振趋势"
SRC_A = "/Users/boos/WorkBuddy AI/加密货币趋势判断系统"
TGT = ROOT + "/数据/archive/parquet/sources"
EVID = ROOT + "/evidence/Work_数据整理迁移_2026-10-15"
EXCL_IDX = {"BTCDOMUSDT", "ALLUSDT"}

def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

def rm_rf(p):
    subprocess.run(["/usr/bin/chflags", "-R", "nouchg", p], capture_output=True)
    subprocess.run(["/bin/chmod", "-R", "u+w", p], capture_output=True)
    r = subprocess.run(["/bin/rm", "-rf", p], capture_output=True, text=True)
    return r.returncode, r.stderr.strip()

def walk_files(d):
    out = []
    for dp, _, fs in os.walk(d):
        for f in fs:
            out.append(os.path.join(dp, f))
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sc = json.load(open(POOL + "/scope_crypto.json"))
    wl_bin = set(x["symbol"] for x in sc["binance_items"])
    wl_okx = set(sc["okx_symbols"])
    print(f"白名单 币安={len(wl_bin)} OKX={len(wl_okx)} 排除指数={sorted(EXCL_IDX)}  dry_run={args.dry_run}", flush=True)

    targets = [
        ("binance", "um", TGT + "/binance/um", SRC_A + "/data/archive/parquet/sources/binance/um", wl_bin),
        ("okx", "swap", TGT + "/okx/swap", SRC_A + "/data/archive/parquet/sources/okx/swap", wl_okx),
    ]

    rows = []
    removed = []
    kept = []
    for ex, mkt, tdir, sdir, wl in targets:
        if not os.path.isdir(tdir):
            continue
        for e in sorted(os.listdir(tdir)):
            tp = os.path.join(tdir, e)
            if not os.path.isdir(tp):
                continue
            if e in wl:
                kept.append((ex, mkt, e))
                continue
            # 非白名单 -> 核验
            sp = os.path.join(sdir, e)
            files = walk_files(tp)
            ok_all = True
            n_src_missing = n_sha_diff = 0
            total_bytes = 0
            for f in files:
                rel = os.path.relpath(f, tp)
                sf = os.path.join(sp, rel)
                sz = os.path.getsize(f)
                total_bytes += sz
                if not os.path.exists(sf):
                    n_src_missing += 1; ok_all = False
                    rows.append(dict(交易所=ex, 市场=mkt, 币种=e, 源绝对=sf, 目标绝对=f, 逻辑字节=sz,
                                     sha_src="", sha_dst="", 源存在=False, sha相同=False,
                                     判定=("指数-排除" if e in EXCL_IDX else "非白名单-排除")))
                    continue
                a, b = sha256(sf), sha256(f)
                same = (a == b)
                if not same:
                    n_sha_diff += 1; ok_all = False
                rows.append(dict(交易所=ex, 市场=mkt, 币种=e, 源绝对=sf, 目标绝对=f, 逻辑字节=sz,
                                 sha_src=a, sha_dst=b, 源存在=True, sha相同=same,
                                 判定=("指数-排除" if e in EXCL_IDX else "非白名单-排除")))
            # 仅当全部核验通过才移除
            action = "保留(核验未过)" if not ok_all else ("将移除" if args.dry_run else "已移除")
            rc = ""
            if ok_all and not args.dry_run:
                rc, err = rm_rf(tp)
                if rc != 0 or os.path.exists(tp):
                    action = f"移除失败 rc={rc} {err[:80]}"
            removed.append(dict(交易所=ex, 市场=mkt, 币种=e, 文件数=len(files), 逻辑字节=total_bytes,
                                源缺失=n_src_missing, SHA不同=n_sha_diff, 全部核验通过=ok_all,
                                动作=action, rm_rc=rc))
            print(f"  {ex}/{mkt}/{e}: 文件={len(files)} 字节={total_bytes} 源缺失={n_src_missing} SHA不同={n_sha_diff} -> {action}", flush=True)

    with open(EVID + "/sources非白名单_逐文件核验.csv", "w", newline="", encoding="utf-8-sig") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    with open(EVID + "/sources非白名单_移除汇总.json", "w", encoding="utf-8") as f:
        json.dump(dict(dry_run=args.dry_run,
                       目标sources根=TGT,
                       核验文件总数=len(rows),
                       核验逻辑字节=sum(r["逻辑字节"] for r in rows),
                       源全存在=all(r["源存在"] for r in rows) if rows else None,
                       SHA全相同=all(r["sha相同"] for r in rows) if rows else None,
                       移除目录=removed,
                       保留白名单目录数=len(kept)), f, ensure_ascii=False, indent=1)
    print(f"\n核验文件={len(rows)} 字节={sum(r['逻辑字节'] for r in rows)} 源全存在={all(r['源存在'] for r in rows) if rows else None} SHA全相同={all(r['sha相同'] for r in rows) if rows else None}", flush=True)
    print(f"非白名单目录={len(removed)} 保留白名单目录={len(kept)}", flush=True)

if __name__ == "__main__":
    main()
