#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「四类汇总」：① 目标原有（本任务之前已存在）② 本任务新增 ③ 重复跳过 ④ 未迁/范围外排除。
输出：迁移_四类汇总.json（机读） + 迁移_四类汇总.csv（机读逐组） 。
文件数/字节用原生 find/du（快）。
"""
import os, json, csv, gzip, subprocess

ROOT = "/Users/boos/Desktop/威科夫因子共振趋势"
EVID = ROOT + "/evidence/Work_数据整理迁移_2026-10-15"

def nfiles(p):
    if not os.path.exists(p): return 0
    if os.path.isfile(p): return 1
    r = subprocess.run(["/usr/bin/find", p, "-type", "f"], capture_output=True, text=True)
    return len(r.stdout.splitlines())

def nbytes(p):
    if not os.path.exists(p): return 0
    if os.path.isfile(p): return os.path.getsize(p)
    r = subprocess.run(["/usr/bin/du", "-sk", p], capture_output=True, text=True)
    try: return int(r.stdout.split()[0]) * 1024
    except Exception: return 0

def load_json(p):
    try:
        with open(p, encoding="utf-8") as f: return json.load(f)
    except Exception: return None

def main():
    exec_sum = load_json(EVID + "/过滤迁移_执行汇总.json") or {}
    grp = {g["组号"]: g for g in exec_sum.get("逐组", [])}
    dry = load_json(EVID + "/过滤迁移_分组_dry.csv") if False else None
    # 分组 dry 表
    grpmeta = {}
    dp = EVID + "/过滤迁移_分组_exec.csv"
    if os.path.exists(dp):
        with open(dp, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f): grpmeta[row["组号"]] = row
    # 排除副本移除证据
    src_excl = load_json(EVID + "/sources非白名单_移除汇总.json") or {}
    raw_excl = load_json(EVID + "/范围过滤_汇总.json") or {}

    # ① 目标原有
    pre = []
    for rel in ["数据/研究输入/WyckoffMaster", "数据/研究输入/WyckoffMaster_v31"]:
        p = os.path.join(ROOT, rel)
        if os.path.exists(p):
            pre.append(dict(路径=rel, 文件数=nfiles(p), 字节=nbytes(p), 说明="本任务开始前已存在；未复制/未覆盖/未改"))
    # ② 本任务新增（逐组）
    added = []; skipped = []; excl = []
    for gid, g in grp.items():
        m = grpmeta.get(gid, {})
        added.append(dict(组号=gid, 新增项=g.get("新增项", 0), 用时s=g.get("用时s", 0), df消耗MiB=g.get("df消耗MiB", 0)))
        skipped.append(dict(组号=gid, 跳过项=g.get("跳过项", 0), 允许项=int(m.get("允许项", 0) or 0), 目标已存在项=int(m.get("目标已存在项", 0) or 0)))
        if int(g.get("排除项", 0) or 0):
            excl.append(dict(组号=gid, 排除项=g.get("排除项", 0), 排除文件=int(m.get("排除文件", 0) or 0), 排除GiB=float(m.get("排除GiB", 0) or 0), 样例=m.get("排除样例", "")))
    # ④ 额外排除：raw 指数副本 / sources 非白名单（本任务误拷后移除）
    extra_excl = []
    if raw_excl:
        extra_excl.append(dict(位置="数据/raw/{1d,12h}", 对象="指数副本 ALLUSDT/BTCDOMUSDT",
                               文件数=raw_excl.get("非白名单文件总数"), 说明="本任务整周期ditto误拷入→逐文件核验源存在且SHA相同后移除；源未动"))
    if src_excl:
        extra_excl.append(dict(位置="数据/archive/parquet/sources", 对象="指数/非白名单OKX",
                               文件数=src_excl.get("核验文件总数"), 说明="本任务目录级ditto误拷入→核验后移除；保留白名单549目录"))

    # 清单统计（新增行 / 跳过行）
    man = EVID + "/过滤迁移_逐文件清单.csv.gz"
    man_stats = {}
    if os.path.exists(man):
        n_add = n_skip = 0; byt = 0
        with gzip.open(man, "rt", encoding="utf-8-sig") as f:
            rd = csv.reader(f); hdr = next(rd)
            i_st = hdr.index("状态"); i_sz = hdr.index("逻辑字节")
            for r in rd:
                if r[i_st] == "新增":
                    n_add += 1
                    try: byt += int(r[i_sz] or 0)
                    except Exception: pass
                elif r[i_st] == "已存在-跳过": n_skip += 1
        man_stats = dict(逐文件清单_新增行=n_add, 逐文件清单_跳过行=n_skip, 新增逻辑字节=byt,
                         新增逻辑GiB=round(byt / (1 << 30), 4))

    out = {
        "生成说明": "K线+OI 迁移四类汇总（只列实测证据）",
        "迁移后空闲GiB": exec_sum.get("迁移后空闲GiB"),
        "中止": exec_sum.get("中止"),
        "①_目标原有": pre,
        "②_本任务新增_逐组": added,
        "③_重复跳过_逐组": skipped,
        "④_未迁范围外排除_逐组": excl,
        "④_额外排除_误拷已移除": extra_excl,
        "逐文件清单统计": man_stats,
        "排除副本证据": {"raw指数副本": raw_excl.get("非白名单文件总数") if raw_excl else None,
                    "sources非白名单": src_excl.get("核验文件总数") if src_excl else None},
    }
    with open(EVID + "/迁移_四类汇总.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open(EVID + "/迁移_四类汇总.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(["类别", "组号", "项", "文件数/说明"])
        for r in pre: w.writerow(["①目标原有", r["路径"], r["文件数"], f'{r["字节"]}字节'])
        for r in added: w.writerow(["②本任务新增", r["组号"], r["新增项"], f'df消耗{r["df消耗MiB"]}MiB'])
        for r in skipped: w.writerow(["③重复跳过", r["组号"], r["跳过项"], f'允许{r["允许项"]}'])
        for r in excl: w.writerow(["④未迁/排除", r["组号"], r["排除项"], f'{r["排除文件"]}文件 {r["排除GiB"]}GiB'])
        for r in extra_excl: w.writerow(["④误拷已移除", r["位置"], r["文件数"], r["对象"]])
    print(json.dumps(out, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
