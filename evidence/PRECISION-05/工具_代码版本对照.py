#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRECISION-05 工具：把「run 时的代码」与「当前代码」「git HEAD」三方对照。

目的：回答一个问题 —— 这 6 个 run 用的到底是哪一版成交代码，现在能不能复原？
  · run 时指纹：来自各 run 的 inputs.json → fingerprints.code.files（每个文件一个 sha256）
  · 当前磁盘：现场算 sha256
  · git HEAD：`git show HEAD:<path>` 的 sha256（只有被跟踪的文件才有）

三类结论：
  same_as_current —— 跑完没改过
  recoverable     —— 与 HEAD 一致，可以用 git 取回当时的字节
  unrecoverable   —— 既不是当前版本也不是 HEAD（未跟踪 / 中间态），字节已丢失

用法：python3 工具_代码版本对照.py
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
RUNS = os.path.join(ROOT, "回测日志", "event_trend_v0_1", "runs")
OUT = os.path.join(HERE, "运行时代码", "运行时代码指纹对照.json")

RUN_IDS = [
    "run_WX_EVENT_TREND_V0_1_SPACEUSDT_1792174451",
    "run_WX_EVENT_TREND_V0_1_AIGENSYNUSDT_1792196703",
    "run_WX_EVENT_TREND_V0_1_TRUTHUSDT_1792174465",
    "run_WX_EVENT_TREND_V0_1_COTIUSDT_1792174145",
    "run_WX_EVENT_TREND_V0_1_BNBUSY_1792172973".replace("BNBUSY", "BNBUSDT"),
    "run_WX_EVENT_TREND_V0_1_ORDIUSDT_1792171568",
]


def sha_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha_file(p):
    return sha_bytes(open(p, "rb").read())


def git_head_sha(rel):
    try:
        r = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=ROOT,
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            return None
        return sha_bytes(r.stdout)
    except Exception:  # noqa: BLE001
        return None


def tracked(rel):
    r = subprocess.run(["git", "ls-files", "--error-unmatch", rel], cwd=ROOT,
                       capture_output=True, timeout=60)
    return r.returncode == 0


def main():
    out = {"generated_utc": None, "runs": {}}
    import time
    out["generated_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    for rid in RUN_IDS:
        p = os.path.join(RUNS, rid, "inputs.json")
        d = json.load(open(p, encoding="utf-8"))
        files = d["fingerprints"]["code"]["files"]
        rows = {}
        for rel, run_sha in files.items():
            cur = os.path.join(ROOT, rel)
            cur_sha = sha_file(cur) if os.path.exists(cur) else None
            head = git_head_sha(rel) if tracked(rel) else None
            if cur_sha == run_sha:
                verdict = "same_as_current"
            elif head == run_sha:
                verdict = "recoverable_from_git_HEAD"
            else:
                verdict = "unrecoverable"
            rows[rel] = {"run_sha256": run_sha, "current_sha256": cur_sha,
                         "git_head_sha256": head, "git_tracked": tracked(rel),
                         "verdict": verdict}
        out["runs"][rid] = {
            "symbol": d.get("symbol"),
            "combined_sha256": d["fingerprints"]["code"]["combined_sha256"],
            "files": rows,
            "differs_from_current": [k for k, v in rows.items()
                                     if v["verdict"] != "same_as_current"],
        }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    first = next(iter(out["runs"].values()))
    print("run 时 combined sha256:", first["combined_sha256"])
    print(f'与当前不一致的文件 {len(first["differs_from_current"])} 个：')
    for rel in first["differs_from_current"]:
        v = first["files"][rel]
        print(f'  {rel}\n     run  : {v["run_sha256"]}\n     当前 : {v["current_sha256"]}'
              f'\n     HEAD : {v["git_head_sha256"]}\n     结论 : {v["verdict"]}')
    # 六个 run 的指纹是否完全一致
    uniq = {v["combined_sha256"] for v in out["runs"].values()}
    print("六个 run 的代码指纹是否同一版:", len(uniq) == 1, uniq)
    print("写出:", OUT)


if __name__ == "__main__":
    main()
