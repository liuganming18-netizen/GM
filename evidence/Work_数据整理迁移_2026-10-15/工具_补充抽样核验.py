#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补充抽样核验：从逐文件清单的「新增」行里按组抽样，原生批量计算 源SHA 与 目标SHA 并比对；
另对含 .CHECKSUM 的组做「排序后拼接内容的聚合SHA」源/目标比对。"""
import gzip, csv, json, os, subprocess
from collections import defaultdict

EVID = "/Users/boos/Desktop/威科夫因子共振趋势/evidence/Work_数据整理迁移_2026-10-15"
SHASUM = "/usr/bin/shasum"

def native_sha_files(paths):
    out = {}
    for i in range(0, len(paths), 300):
        chunk = [p for p in paths[i:i + 300] if os.path.exists(p)]
        if not chunk: continue
        r = subprocess.run([SHASUM, "-a", "256"] + chunk, capture_output=True, text=True)
        for ln in r.stdout.splitlines():
            if "  " in ln:
                sha, path = ln.split("  ", 1); out[path] = sha.lower()
    return out

def agg_sidecar_hash(root):
    """排序后拼接所有 *.CHECKSUM 内容 -> sha256"""
    r = subprocess.run(["sh", "-c",
        f"/usr/bin/find '{root}' -name '*.CHECKSUM' -print0 | sort -z | xargs -0 /bin/cat 2>/dev/null | {SHASUM} -a 256"],
        capture_output=True, text=True)
    return r.stdout.split()[0] if r.stdout.strip() else ""

def main():
    rows = defaultdict(list)
    with gzip.open(EVID + "/过滤迁移_逐文件清单.csv.gz", "rt", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        for r in rd:
            if r["状态"] == "新增":
                rows[r["组号"]].append((r["源绝对路径"], r["目标绝对路径"]))
    sample = []
    for gid, lst in rows.items():
        if len(lst) <= 60:
            sample += [(gid, s, d) for s, d in lst]
        else:
            step = len(lst) // 60
            sample += [(gid,) + lst[i] for i in range(0, len(lst), step)][:60]
    srcs = [s for _, s, _ in sample]; dsts = [d for _, _, d in sample]
    sh_src = native_sha_files(srcs); sh_dst = native_sha_files(dsts)
    ok = bad = miss = 0; details = []
    for gid, s, d in sample:
        a, b = sh_src.get(s), sh_dst.get(d)
        if a is None or b is None: miss += 1; continue
        if a == b: ok += 1
        else:
            bad += 1; details.append(dict(组号=gid, 源=s, 目标=d, sha_src=a, sha_dst=b))
    # sidecar 聚合
    agg = []
    srcA = "/Users/boos/WorkBuddy AI/加密货币趋势判断系统"
    DL = "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载"
    ROOT = "/Users/boos/Desktop/威科夫因子共振趋势"
    pairs = [(DL + "/raw/" + tf, ROOT + "/数据/raw/" + tf) for tf in ["1d", "12h", "8h", "4h", "1h", "30m", "15m", "5m"]]
    pairs += [(DL + "/raw_oi_metrics", ROOT + "/数据/raw_oi_metrics")]
    for s, d in pairs:
        if os.path.isdir(s) and os.path.isdir(d):
            hs, hd = agg_sidecar_hash(s), agg_sidecar_hash(d)
            agg.append(dict(源=s, 目标=d, 聚合SHA_源=hs[:16], 聚合SHA_目标=hd[:16], 相同=(hs == hd and hs != "")))
    rep = dict(抽样总数=len(sample), 抽样SHA相同=ok, 抽样SHA不同=bad, 抽样缺失=miss,
               不同明细=details, sidecar聚合比对=agg)
    with open(EVID + "/补充抽样核验.json", "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print(json.dumps(dict(抽样总数=len(sample), 抽样SHA相同=ok, 抽样SHA不同=bad, 抽样缺失=miss), ensure_ascii=False))
    for a in agg:
        print(f"  sidecar聚合 {os.path.basename(a['源'])}: 相同={a['相同']}")

if __name__ == "__main__":
    main()
