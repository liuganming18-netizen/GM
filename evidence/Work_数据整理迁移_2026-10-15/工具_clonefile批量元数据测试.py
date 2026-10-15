#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""clonefile 批量元数据开销测试：克隆 N 个 raw/1d 文件到临时目录，测 df 前后，估每文件元数据成本。"""
import os, ctypes, ctypes.util, json, subprocess, time

ROOT = "/Users/boos/Desktop/威科夫因子共振趋势"
EVID = ROOT + "/evidence/Work_数据整理迁移_2026-10-15"
TMP = EVID + "/_meta_test"
SRC = "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载/raw/1d"

libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
libc.clonefile.restype = ctypes.c_int
libc.clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]

def avail():
    st = os.statvfs(ROOT); return st.f_bavail * st.f_frsize

def rm_rf(p):
    subprocess.run(["/usr/bin/chflags", "-R", "nouchg", p], capture_output=True)
    subprocess.run(["/bin/chmod", "-R", "u+w", p], capture_output=True)
    subprocess.run(["/bin/rm", "-rf", p], capture_output=True)

def main():
    if os.path.exists(TMP):
        rm_rf(TMP)
    os.makedirs(TMP, exist_ok=True)
    files = []
    for dp, dns, fns in os.walk(SRC):
        for f in fns:
            files.append(os.path.join(dp, f))
    files.sort()
    N = min(10000, len(files))
    sel = files[:N]
    total_logical = sum(os.path.getsize(p) for p in sel)
    a0 = avail()
    t0 = time.time()
    ok = 0; fail = 0
    for p in sel:
        rel = os.path.relpath(p, SRC)
        dst = os.path.join(TMP, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        ctypes.set_errno(0)
        rc = libc.clonefile(p.encode(), dst.encode(), 0)
        if rc == 0:
            ok += 1
        else:
            fail += 1
    dt = time.time() - t0
    a1 = avail()
    delta = a0 - a1
    res = dict(N=N, ok=ok, fail=fail, logical_bytes=total_logical,
               logical_MiB=round(total_logical / 1048576, 3),
               df_delta_bytes=delta, df_delta_MiB=round(delta / 1048576, 3),
               per_file_meta_bytes=round(delta / ok, 1) if ok else None,
               seconds=round(dt, 2), files_per_sec=round(ok / dt, 1) if dt else None)
    rm_rf(TMP)
    res["tmp_removed"] = not os.path.exists(TMP)
    with open(EVID + "/clonefile批量元数据_证据.json", "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(json.dumps(res, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
