#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PRECISION-04 工具：抓取币安官方公告（只读公开接口），提取与六标的相关的时间段规格证据。

设计约束（与任务书一致）：
- 只读、只写本目录；不修改规格缓存/行情库/生产代码。
- 只抓官方来源（www.binance.com 公共 CMS 接口），不引入第三方数据。
- 保留原始响应字节，便于复核；提取结果另存，不改原始件。
- 抓取失败/被限流如实记录，不用空结果冒充「无公告」。

用法：
  python 工具_公告抓取.py index     # 抓公告索引（catalog 49 = Latest Binance News）
  python 工具_公告抓取.py detail    # 抓候选公告详情（可重入，已下载的跳过）
  python 工具_公告抓取.py extract   # 抽取正文文本+表格
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(HERE, "原始公告")
IDX_PATH = os.path.join(HERE, "公告索引.json")

HDR = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "clienttype": "web",
    "lang": "en",
    "Accept": "application/json",
}

# composite 与 apex 两个网关返回同一份 CMS 内容；composite 对高频请求返回 429，
# 故统一走 apex（同一官方域名、同一数据源）。
LIST_URL = ("https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query"
            "?type=1&pageNo={page}&pageSize=50&catalogId={cid}")
IDX48_PATH = os.path.join(HERE, "公告索引_48新币上线.json")
DETAIL_URL = ("https://www.binance.com/bapi/apex/v1/public/apex/cms/article/detail/query"
              "?articleCode={code}")

SYMBOLS = ["SPACEUSDT", "AIGENSYNUSDT", "TRUTHUSDT", "COTIUSDT", "BNBUSDT", "ORDIUSDT"]
BASES = ["SPACE", "AIGENSYN", "TRUTH", "COTI", "BNB", "ORDI"]

FUT_KW = ("Tick Size", "Price Precision", "Minimum Trade Amount",
          "Contract Specification", "Step Size", "Precision")


def fetch(url, tries=6, base_sleep=2.0):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=HDR)
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
            if not body:
                raise RuntimeError("empty body")
            return json.loads(body)
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code == 429:
                time.sleep(base_sleep * (2 ** i))
                continue
            time.sleep(base_sleep)
        except Exception as e:  # noqa: BLE001
            last = repr(e)
            time.sleep(base_sleep)
    raise RuntimeError(f"fetch failed: {last}")


def _dump_index(cid, pages, path, name):
    arts = []
    total = None
    for p in range(1, pages + 1):
        j = fetch(LIST_URL.format(page=p, cid=cid))
        cat = j["data"]["catalogs"][0]
        total = cat["total"]
        arts.extend(cat["articles"])
        if p % 10 == 0:
            print(f"  page {p}/{pages} total={len(arts)}", flush=True)
        time.sleep(0.6)
    out = {"catalogId": cid, "catalogName": name,
           "reported_total": total, "fetched": len(arts),
           "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "articles": arts}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"index saved: {path} {len(arts)} / reported {total}")


def cmd_index():
    _dump_index(49, 90, IDX_PATH, "Latest Binance News")


def cmd_index48():
    _dump_index(48, 46, IDX48_PATH, "New Cryptocurrency Listing")


def candidates():
    picked = {}
    idx = json.load(open(IDX_PATH, encoding="utf-8"))
    for a in idx["articles"]:
        t = a["title"]
        fut = any(k in t for k in FUT_KW) and ("Perpetual" in t or "Futures" in t or "USDⓈ-M" in t)
        # 标的专属：必须同时出现「合约类」词，避免把 BNB 燃烧/质押等新闻混进来
        sym = any(s in t for s in SYMBOLS) or (
            any(b in t for b in BASES)
            and any(w in t for w in ("Futures", "Perpetual", "Contract", "Listing", "Launch")))
        if fut or sym:
            picked[a["code"]] = dict(a, src_catalog=49)
    if os.path.exists(IDX48_PATH):
        idx48 = json.load(open(IDX48_PATH, encoding="utf-8"))
        for a in idx48["articles"]:
            t = a["title"]
            if any(b in t for b in BASES):
                picked.setdefault(a["code"], dict(a, src_catalog=48))
    return sorted(picked.values(), key=lambda a: a["releaseDate"])


def cmd_detail():
    os.makedirs(RAW_DIR, exist_ok=True)
    cands = candidates()
    # 倒序抓：优先取最近（2023 以后）的公告，早期噪音放最后。
    # 抓取可重入，已下载的会跳过，所以顺序不影响最终完整性。
    cands = list(reversed(cands))
    print(f"candidates: {len(cands)}")
    ok = skip = fail = 0
    for i, a in enumerate(cands, 1):
        path = os.path.join(RAW_DIR, f"{a['releaseDate']}_{a['code']}.json")
        if os.path.exists(path) and os.path.getsize(path) > 200:
            skip += 1
            continue
        try:
            j = fetch(DETAIL_URL.format(code=a["code"]))
            if not (j.get("data") or {}).get("body"):
                raise RuntimeError("no body")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(j, f, ensure_ascii=False)
            ok += 1
            if i % 10 == 0:
                print(f"  {i}/{len(cands)} ok={ok} skip={skip} fail={fail}", flush=True)
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"  FAIL {a['code']} {a['title'][:60]} :: {e}", flush=True)
            with open(os.path.join(HERE, "抓取失败记录.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"code": a["code"], "title": a["title"],
                                    "releaseDate": a["releaseDate"], "error": str(e)},
                                   ensure_ascii=False) + "\n")
        time.sleep(0.9)
    print(f"done ok={ok} skip={skip} fail={fail}")


def _walk(node, out, tables, cur_row=None):
    if not isinstance(node, dict):
        return
    if node.get("node") == "text":
        out.append(node.get("text") or "")
        return
    tag = node.get("tag")
    kids = node.get("child") or []
    if tag == "tr":
        row = []
        for c in kids:
            cell = []
            _walk(c, cell, tables)
            row.append("".join(cell).strip())
        tables.append(row)
        return
    if tag == "table":
        tables.append("__TABLE_START__")
        for c in kids:
            _walk(c, out, tables)
        tables.append("__TABLE_END__")
        return
    if tag in ("p", "li"):
        out.append("\n")
    for c in kids:
        _walk(c, out, tables)
    if tag in ("p", "li"):
        out.append("\n")


def cmd_extract():
    rows = []
    for fn in sorted(os.listdir(RAW_DIR)):
        if not fn.endswith(".json"):
            continue
        j = json.load(open(os.path.join(RAW_DIR, fn), encoding="utf-8"))
        d = j["data"]
        try:
            tree = json.loads(d["body"])
        except Exception:  # noqa: BLE001
            continue
        text_parts, tables = [], []
        _walk(tree, text_parts, tables)
        text = "".join(text_parts)
        # 表格按行分组
        tbl_rows, cur = [], []
        for t in tables:
            if t == "__TABLE_START__":
                cur = []
            elif t == "__TABLE_END__":
                tbl_rows.append(cur)
                cur = []
            else:
                cur.append(t)
        rows.append({"code": d["code"], "title": d["title"],
                     "publishDate": d["publishDate"],
                     "firstCatalogName": d.get("firstCatalogName"),
                     "text": text,
                     "tables": tbl_rows})
    with open(os.path.join(HERE, "公告正文提取.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"extracted {len(rows)} articles -> 公告正文提取.jsonl")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"index": cmd_index, "index48": cmd_index48, "detail": cmd_detail,
     "extract": cmd_extract}.get(cmd, lambda: print(__doc__))()
