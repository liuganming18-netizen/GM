#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""范围过滤分析（正确处理 周期根下的散落文件）：白名单=币安526/OKX23；指数排除。
对目标已迁移 raw/1d、raw/12h：区分 币种目录 / 散落文件，列出非白名单，逐文件核验源存在且SHA相同。"""
import json, os, csv, hashlib, re

POOL = "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载/标的池"
ROOT = "/Users/boos/Desktop/威科夫因子共振趋势"
RAW_SRC = "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载/raw"
EVID = ROOT + "/evidence/Work_数据整理迁移_2026-10-15"

def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

def main():
    sc = json.load(open(POOL + "/scope_crypto.json"))
    wl = set(x["symbol"] for x in sc["binance_items"])
    print(f"白名单: 币安={len(wl)}  OKX={len(sc['okx_symbols'])}")
    print(f"排除指数: BTCDOMUSDT, ALLUSDT  (scope_crypto.excluded_indices)\n")
    rows = []
    summary = []
    for tf in ["1d", "12h"]:
        d = f"{ROOT}/数据/raw/{tf}"
        if not os.path.isdir(d):
            continue
        ents = sorted(os.listdir(d))
        dirs = [e for e in ents if os.path.isdir(os.path.join(d, e))]
        loose = [e for e in ents if os.path.isfile(os.path.join(d, e))]
        non_dirs = sorted(set(dirs) - wl)
        # 散落文件的币种
        loose_syms = sorted({re.match(r"([A-Z0-9]+)-" + re.escape(tf) + r"-", e).group(1)
                             for e in loose if re.match(r"([A-Z0-9]+)-" + re.escape(tf) + r"-", e)})
        non_loose = sorted(set(loose_syms) - wl)
        print(f"[{tf}] 顶层条目={len(ents)} 币种目录={len(dirs)} 散落文件={len(loose)}")
        print(f"      非白名单目录({len(non_dirs)}): {non_dirs}")
        print(f"      散落文件涉及币种({len(loose_syms)}): {loose_syms}")
        print(f"      散落文件中非白名单币({len(non_loose)}): {non_loose}")
        summary.append(dict(周期=tf, 顶层条目=len(ents), 币种目录=len(dirs), 散落文件=len(loose),
                            非白名单目录数=len(non_dirs), 非白名单目录=",".join(non_dirs),
                            散落文件币数=len(loose_syms), 散落非白名单币=",".join(non_loose)))
        # 逐文件核验非白名单目录
        for s in non_dirs:
            dd = os.path.join(d, s); sd = os.path.join(RAW_SRC, tf, s)
            for dp, _, fs in os.walk(dd):
                for f in fs:
                    p = os.path.join(dp, f); rel = os.path.relpath(p, dd); sp = os.path.join(sd, rel)
                    sz = os.path.getsize(p)
                    if not os.path.exists(sp):
                        rows.append(dict(周期=tf, 币种=s, 源绝对=sp, 目标绝对=p, 逻辑字节=sz, sha_src="", sha_dst="", 源存在=False, sha相同=False, 判定="源缺失-异常"))
                        continue
                    a, b = sha256(sp), sha256(p)
                    rows.append(dict(周期=tf, 币种=s, 源绝对=sp, 目标绝对=p, 逻辑字节=sz, sha_src=a, sha_dst=b,
                                     源存在=True, sha相同=(a == b),
                                     判定=("指数-排除" if s in ("BTCDOMUSDT", "ALLUSDT") else "非白名单-排除")))
            n = sum(1 for r in rows if r["币种"] == s and r["周期"] == tf)
            b = sum(r["逻辑字节"] for r in rows if r["币种"] == s and r["周期"] == tf)
            ok = sum(1 for r in rows if r["币种"] == s and r["周期"] == tf and r["源存在"])
            sm = sum(1 for r in rows if r["币种"] == s and r["周期"] == tf and r["sha相同"])
            print(f"      -> {s}: 文件={n} 字节={b} 源存在={ok} SHA相同={sm}")
    with open(EVID + "/范围过滤_非白名单副本.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    with open(EVID + "/范围过滤_汇总.json", "w", encoding="utf-8") as f:
        json.dump(dict(白名单币安=len(wl), 白名单OKX=len(sc["okx_symbols"]), 排除指数=["BTCDOMUSDT", "ALLUSDT"],
                       逐周期=summary, 非白名单文件总数=len(rows),
                       非白名单逻辑字节=sum(r["逻辑字节"] for r in rows),
                       源全存在=all(r["源存在"] for r in rows), SHA全相同=all(r["sha相同"] for r in rows)), f, ensure_ascii=False, indent=1)
    print(f"\n写出 范围过滤_非白名单副本.csv 行数={len(rows)}  字节合计={sum(r['逻辑字节'] for r in rows)}")

if __name__ == "__main__":
    main()
