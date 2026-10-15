#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""APFS clonefile 可行性小样本测试（可逆、只读源、副本清理）。
证据：源/目标 SHA 相同、inode 不同、links=1、df 前后空间。
"""
import os, sys, ctypes, ctypes.util, hashlib, shutil, json, subprocess, time

ROOT = "/Users/boos/Desktop/威科夫因子共振趋势"
EVID = ROOT + "/evidence/Work_数据整理迁移_2026-10-15"
TMP = EVID + "/_clone_test"
DL = "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载"

libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
libc.clonefile.restype = ctypes.c_int
libc.clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]

def df_kib(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize // 1024, st.f_bavail * st.f_frsize

def sha256(path, limit=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def info(path):
    st = os.lstat(path)
    return dict(path=path, inode=st.st_ino, links=st.st_nlink, size=st.st_size, blocks=st.st_blocks)

def do_clone(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    ctypes.set_errno(0)
    rc = libc.clonefile(src.encode(), dst.encode(), 0)
    err = ctypes.get_errno()
    return rc, err, (os.strerror(err) if rc != 0 else "OK")

def cp_c_clone(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    p = subprocess.run(["/bin/cp", "-c", src, dst], capture_output=True, text=True)
    return p.returncode, p.stderr.strip()

def rm_rf(path):
    """清除不可变标志后删除（源数据带 uchg，副本会继承）"""
    subprocess.run(["/usr/bin/chflags", "-R", "nouchg", path], capture_output=True)
    subprocess.run(["/bin/chmod", "-R", "u+w", path], capture_output=True)
    subprocess.run(["/bin/rm", "-rf", path], capture_output=True)

def main():
    if os.path.exists(TMP):
        rm_rf(TMP)
    os.makedirs(TMP, exist_ok=True)
    res = {"steps": [], "df_before": {}, "df_after": {}}
    avail_kib0, avail_b0 = df_kib(ROOT)
    res["df_before"] = {"avail_KiB": avail_kib0, "avail_GiB": round(avail_b0 / (1 << 30), 4)}

    # ---- 样本A：很小的 raw_rest_gap 文件（用户指定） ----
    srcA = DL + "/raw_rest_gap/1d/BTCUSDT/BTCUSDT-1d-1567900800000.meta.json"
    dstA = TMP + "/A.meta.json"
    rc, err, msg = do_clone(srcA, dstA)
    res["steps"].append({"样本": "A-small-rest_gap", "src": srcA, "dst": dstA,
                         "clonefile_rc": rc, "errno": err, "msg": msg})
    if rc == 0:
        i_s, i_d = info(srcA), info(dstA)
        res["steps"][-1].update({"src_info": i_s, "dst_info": i_d,
                                 "sha_src": sha256(srcA), "sha_dst": sha256(dstA),
                                 "inode_diff": i_s["inode"] != i_d["inode"], "dst_links": i_d["links"],
                                 "sha_equal": sha256(srcA) == sha256(dstA)})

    # ---- 样本B：较大的真实文件（证明大文件 clone 也 ≈0 空间） ----
    # 找一个 ~100-300MB 的冷档 parquet
    big = None
    for dp, dns, fns in os.walk("/Users/boos/WorkBuddy AI/加密货币趋势判断系统/data/archive/parquet/candles"):
        for f in fns:
            p = os.path.join(dp, f)
            if 80_000_000 < os.path.getsize(p) < 300_000_000:
                big = p; break
        if big:
            break
    if big is None:
        big = DL + "/raw/1h/0GUSDT/0GUSDT-1h-2025-09.zip"
    dstB = TMP + "/B." + os.path.basename(big)
    rcB, errB, msgB = do_clone(big, dstB)
    res["steps"].append({"样本": "B-large", "src": big, "dst": dstB,
                         "src_size": os.path.getsize(big), "clonefile_rc": rcB, "errno": errB, "msg": msgB})
    if rcB == 0:
        i_s, i_d = info(big), info(dstB)
        res["steps"][-1].update({"src_info": i_s, "dst_info": i_d,
                                 "sha_src": sha256(big), "sha_dst": sha256(dstB),
                                 "inode_diff": i_s["inode"] != i_d["inode"], "dst_links": i_d["links"],
                                 "sha_equal": sha256(big) == sha256(dstB)})

    avail_kib1, avail_b1 = df_kib(ROOT)
    res["df_after"] = {"avail_KiB": avail_kib1, "avail_GiB": round(avail_b1 / (1 << 30), 4),
                       "delta_KiB": avail_kib0 - avail_kib1,
                       "delta_MiB": round((avail_kib0 - avail_kib1) / 1024, 3)}

    # ---- COW 独立性：改副本，源应不变 ----
    if rcB == 0:
        try:
            before_src = sha256(big)
            subprocess.run(["/usr/bin/chflags", "nouchg", dstB], capture_output=True)
            subprocess.run(["/bin/chmod", "u+w", dstB], capture_output=True)
            with open(dstB, "ab") as f:
                f.write(b"\x00COW-TEST")
            after_src = sha256(big); after_dst = sha256(dstB)
            res["cow_test"] = {"src_unchanged": before_src == after_src,
                               "dst_changed": after_dst != before_src,
                               "src_sha_before": before_src, "src_sha_after": after_src}
        except Exception as e:
            res["cow_test"] = {"skipped": repr(e)}

    # ---- /bin/cp -c 交叉验证 ----
    dstC = TMP + "/C.meta.json"
    rcc, errc = cp_c_clone(srcA, dstC)
    res["bin_cp_c"] = {"rc": rcc, "stderr": errc,
                       "exists": os.path.exists(dstC),
                       "sha_equal": (sha256(dstC) == sha256(srcA)) if os.path.exists(dstC) else None,
                       "inode_diff": (os.lstat(dstC).st_ino != os.lstat(srcA).st_ino) if os.path.exists(dstC) else None,
                       "links": os.lstat(dstC).st_nlink if os.path.exists(dstC) else None}

    # ---- 清理测试副本 ----
    rm_rf(TMP)
    avail_kib2, avail_b2 = df_kib(ROOT)
    res["df_final_after_cleanup"] = {"avail_KiB": avail_kib2, "avail_GiB": round(avail_b2 / (1 << 30), 4)}
    res["tmp_removed"] = not os.path.exists(TMP)

    with open(EVID + "/clonefile测试_证据.json", "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(json.dumps(res, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
