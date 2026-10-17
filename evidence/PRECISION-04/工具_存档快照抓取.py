#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRECISION-04 工具：抓取 Internet Archive 对官方 `fapi/v1/exchangeInfo` 的历史捕获。

为什么用它
──────────
交易所**不提供**历史规格归档：
  · `GET /fapi/v1/exchangeInfo` 只返回当前值；
  · `data.binance.vision` 官方归档只有 K 线/成交/OI 等 10 类，**没有**合约规格
    （见 `官方归档目录清单_data.binance.vision.xml`）。
因此「某历史时点的规格」只能来自：① 官方公告（只覆盖被公告的变更）；
② 当时对同一官方端点的响应副本。

本工具取第 ② 类，但**不是**第三方行情数据：抓的是 `fapi.binance.com/fapi/v1/exchangeInfo`
这个官方端点的**响应原文**，只是被 Internet Archive 在某个时刻保存下来。

三条自检（不通过就不用）
──────────────────────
1. 必须是 200 且能 JSON 解析、含 `symbols`；
2. 响应自带 `serverTime`，必须与捕获时刻相差不超过 24 小时
   （相差过大 ⇒ 捕获到的是上游缓存，不能当该时刻的规格）；
3. 逐份记录 wayback 时间戳、原 URL、CDX digest、本地 sha256，可复核。
"""
import datetime
import hashlib
import json
import os
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "存档快照")
CDX = ("http://web.archive.org/cdx/search/cdx?url=fapi.binance.com/fapi/v1/exchangeInfo"
       "&output=json&collapse=digest")
HDR = {"User-Agent": "Mozilla/5.0 (compatible; PRECISION-04 evidence audit)"}
MAX_SKEW_MS = 24 * 3600 * 1000


def get(url, binary=False, tries=4):
    """走 curl：web.archive.org 的 TLS 握手对 urllib 不稳定（实测 SSL UNEXPECTED_EOF）。"""
    import subprocess
    last = None
    for i in range(tries):
        p = subprocess.run(["curl", "-sL", "--compressed", "-m", "90",
                            "-H", "User-Agent: Mozilla/5.0 (compatible; PRECISION-04)",
                            url], capture_output=True)
        if p.returncode == 0 and p.stdout:
            return p.stdout if binary else json.loads(p.stdout)
        last = p.stderr.decode()[:200] or f"exit {p.returncode}"
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"fetch failed: {last}")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    rows = get(CDX)
    head, body = rows[0], rows[1:]
    idx = []
    for ts, orig, status, digest in [(r[1], r[2], r[4], r[5]) for r in body]:
        if status != "200":
            idx.append({"wayback_ts": ts, "status": status, "usable": False,
                        "reason": f"HTTP {status}，非可解析响应"})
            continue
        path = os.path.join(OUT_DIR, f"exchangeInfo_{ts}Z.json")
        if not os.path.exists(path) or os.path.getsize(path) < 500:
            raw = get(f"https://web.archive.org/web/{ts}id_/{orig}", binary=True)
            with open(path, "wb") as f:
                f.write(raw)
            time.sleep(1.0)
        raw = open(path, "rb").read()
        # 存档里的原始响应体常带 gzip 头但缺少 Content-Encoding，curl 不会自动解压
        if raw[:2] == b"\x1f\x8b":
            import gzip
            try:
                raw = gzip.decompress(raw)
                with open(path, "wb") as f:      # 落盘为解压后的 JSON，便于复核
                    f.write(raw)
            except Exception as e:  # noqa: BLE001
                idx.append({"wayback_ts": ts, "status": status, "usable": False,
                            "reason": f"gzip 解压失败：{e}"})
                continue
        try:
            doc = json.loads(raw)
        except Exception:
            idx.append({"wayback_ts": ts, "status": status, "usable": False,
                        "reason": "无法解析为 JSON"})
            continue
        st = doc.get("serverTime")
        cap_ms = int(datetime.datetime.strptime(ts, "%Y%m%d%H%M%S")
                     .replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
        skew = (cap_ms - st) if st else None
        ok = bool(st) and abs(skew) <= MAX_SKEW_MS
        idx.append({
            "wayback_ts": ts,
            "wayback_utc": datetime.datetime.strptime(ts, "%Y%m%d%H%M%S").strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": status,
            "original_url": orig,
            "wayback_url": f"https://web.archive.org/web/{ts}/{orig}",
            "raw_url": f"https://web.archive.org/web/{ts}id_/{orig}",
            "cdx_digest": digest,
            "local_file": os.path.basename(path),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "snapshot_server_ms": st,
            "snapshot_server_utc": (datetime.datetime.fromtimestamp(st / 1000, datetime.UTC)
                                    .strftime("%Y-%m-%dT%H:%M:%SZ") if st else None),
            "capture_skew_ms": skew,
            "symbol_count": len(doc.get("symbols") or []),
            "usable": ok,
            "reason": ("ok" if ok else
                       f"serverTime 与捕获时刻相差 {skew} ms，超过 24h ⇒ 视为上游缓存副本，"
                       f"不能当作该捕获时刻的规格"),
        })
        print(idx[-1]["wayback_utc"], idx[-1]["snapshot_server_utc"],
              idx[-1]["symbol_count"], "usable" if ok else "REJECT")
    with open(os.path.join(HERE, "存档快照索引.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_utc": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "cdx_query": CDX, "max_skew_ms": MAX_SKEW_MS, "captures": idx},
                  f, ensure_ascii=False, indent=1)
    print("usable:", sum(1 for x in idx if x.get("usable")), "/", len(idx))


if __name__ == "__main__":
    main()
