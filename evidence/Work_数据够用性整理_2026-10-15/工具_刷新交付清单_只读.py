#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""刷新本窗交付清单与 SHA（文件清单及SHA.csv / 交付清单_SHA.json）。

不重跑会覆盖手工编辑的旧工具；只做：
  1. 保留既有「外部对象」行（源库/冷档/补件/原始归档，路径不在本窗目录内）原样；
  2. 按本窗目录实际文件重建「交付物」行（报告 + 目录内 .json/.csv/.txt/.md/.py，排除两个清单自身）；
  3. 重算每个交付物 SHA256 并自检。
"""
import csv
import hashlib
import json
import os

BASE = "/Users/boos/Desktop/威科夫因子共振趋势"
OUT = f"{BASE}/evidence/Work_数据够用性整理_2026-10-15"
REPORT = f"{BASE}/docs/数据够用性结论与整理清单_2026-10-15.md"
SELF = {"文件清单及SHA.csv", "交付清单_SHA.json"}


def sha256(path, buf=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(buf)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def row(cat, path, note, sha=None, do_sha=True):
    if not os.path.exists(path):
        return {"类别": cat, "绝对路径": path, "bytes": "", "GiB": "", "sha256": "文件不存在", "说明": note}
    b = os.path.getsize(path) if os.path.isfile(path) else 0
    if sha is not None:
        hv = sha
    elif do_sha and os.path.isfile(path):
        hv = sha256(path)
    else:
        hv = "见 _catalog.json 逐分片sha256"
    return {"类别": cat, "绝对路径": path, "bytes": b,
            "GiB": round(b / 1024 ** 3, 6) if b else "", "sha256": hv, "说明": note}


def main():
    old = list(csv.DictReader(open(os.path.join(OUT, "文件清单及SHA.csv"), encoding="utf-8-sig")))
    # 外部对象：路径不在本窗目录内、且不是本报告的行，原样保留（去重）
    seen = set()
    external = []
    for r in old:
        p = r["绝对路径"]
        if p.startswith(OUT + "/") or p == REPORT:
            continue
        if p in seen:
            continue
        seen.add(p)
        external.append(r)

    F = list(external)
    # 报告（单行）
    F.append(row("本轮交付报告", REPORT, "本报告（更正版）"))
    # 本窗目录内产物
    for fn in sorted(os.listdir(OUT)):
        if fn in SELF or not os.path.isfile(os.path.join(OUT, fn)):
            continue
        if not fn.endswith((".json", ".csv", ".txt", ".md", ".py")):
            continue
        F.append(row("本轮证据产物", os.path.join(OUT, fn), fn))

    with open(os.path.join(OUT, "文件清单及SHA.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["类别", "绝对路径", "bytes", "GiB", "sha256", "说明"])
        w.writeheader(); w.writerows(F)
        w.writerow({"类别": "本轮证据产物", "绝对路径": os.path.join(OUT, "文件清单及SHA.csv"),
                    "bytes": "", "GiB": "", "sha256": "自引用不列（本清单自身）", "说明": "文件清单及SHA.csv（本清单）"})
        w.writerow({"类别": "本轮证据产物", "绝对路径": os.path.join(OUT, "交付清单_SHA.json"),
                    "bytes": "", "GiB": "", "sha256": "自引用不列（同内容 JSON 版）", "说明": "交付清单_SHA.json（同内容 JSON 版）"})

    ext_json = [r for r in F if not r["绝对路径"].startswith(OUT + "/")]
    del_json = [r for r in F if r["绝对路径"].startswith(OUT + "/") or r["绝对路径"] == REPORT]
    json.dump({
        "generated_at_utc": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(),
        "note": "本轮交付物清单（CSV 版见 文件清单及SHA.csv）；两个清单文件不列自身 SHA。"
                "本版新增 v3.1 固定 IS/Val 记录与既往读取披露。",
        "external_objects": ext_json,
        "deliverables": del_json,
    }, open(os.path.join(OUT, "交付清单_SHA.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # 自检
    bad = 0
    for r in F:
        p = r["绝对路径"]
        if not os.path.isfile(p):
            continue
        if r["sha256"] in ("文件不存在",) or "（" in str(r["sha256"]) or r["sha256"] == "自引用不列":
            continue
        if r["sha256"] == "见 _catalog.json 逐分片sha256":
            continue
        if sha256(p) != r["sha256"]:
            bad += 1
            print("SHA 不符:", p)
    print(f"清单 {len(F)} 行（外部 {len(ext_json)} + 交付 {len(del_json)}）；SHA 问题 {bad} 项")


if __name__ == "__main__":
    main()
