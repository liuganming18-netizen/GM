#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRECISION-04 工具：把已下载的官方公告正文，汇总成「六标的规格变更明细」。

只做三件事：
1. 从公告正文里取**生效时间**（交易所公布的 effective time，不是发布时间）；
2. 从表格里取与六标的相关的行（Before / After）；
3. 如实标注「表头说明该表在讲什么字段」（Tick Size / Minimum Trade Amount / ...）。

不推断、不补值：公告没写的字段就是没写。
"""
import json
import os
import re
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(HERE, "原始公告")
OUT_JSON = os.path.join(HERE, "公告规格变更明细.json")
OUT_JSONL = os.path.join(HERE, "公告规格变更明细.jsonl")

SYMBOLS = ["SPACEUSDT", "AIGENSYNUSDT", "TRUTHUSDT", "COTIUSDT", "BNBUSDT", "ORDIUSDT"]

DATE_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})\s*(AM|PM)?\s*(?:\(UTC\)|UTC)")


def walk(node, text, tables):
    if not isinstance(node, dict):
        return
    if node.get("node") == "text":
        text.append(node.get("text") or "")
        return
    tag = node.get("tag")
    kids = node.get("child") or []
    if tag == "tr":
        row = []
        for c in kids:
            cell = []
            walk(c, cell, tables)
            row.append("".join(cell).strip())
        tables.append(row)
        return
    if tag == "table":
        tables.append("__S__")
        for c in kids:
            walk(c, text, tables)
        tables.append("__E__")
        return
    if tag in ("p", "li", "br"):
        text.append("\n")
    for c in kids:
        walk(c, text, tables)
    if tag in ("p", "li"):
        text.append("\n")


def group_tables(flat):
    out, cur = [], None
    for t in flat:
        if t == "__S__":
            cur = []
        elif t == "__E__":
            if cur:
                out.append(cur)
            cur = None
        elif cur is not None:
            cur.append(t)
    return out


# ── 旧公告正文是 HTML，新公告是 JSON 节点树；两种都要能读 ──
def _strip_tags(html):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    s = re.sub(r"</(p|li|tr|div|br)>", "\n", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'")
          .replace("&quot;", '"'))
    return re.sub(r"\n{3,}", "\n\n", s)


def _html_tables(html):
    out = []
    for tbl in re.findall(r"<table.*?</table>", html, flags=re.S | re.I):
        rows = []
        for tr in re.findall(r"<tr.*?</tr>", tbl, flags=re.S | re.I):
            cells = [_strip_tags(c).strip() for c in
                     re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, flags=re.S | re.I)]
            cells = [re.sub(r"\s+", " ", c) for c in cells]
            if cells:
                rows.append(cells)
        if rows:
            out.append(rows)
    return out


def parse_body(body):
    """返回 (纯文本, [表格…])。JSON 节点树与 HTML 两种格式统一出口。"""
    b = (body or "").strip()
    if b.startswith("{") or b.startswith("["):
        try:
            tree = json.loads(b)
        except Exception:
            return _strip_tags(b), _html_tables(b)
        text, flat = [], []
        walk(tree, text, flat)
        return "".join(text), group_tables(flat)
    return _strip_tags(b), _html_tables(b)


def field_hint(title, table):
    """这张表在讲哪个字段 —— 只按公告自己的措辞判断，不外推。"""
    t = title.lower()
    if "minimum trade amount" in t:
        return "最小交易额/最小名义额（公告措辞：Minimum Trade Amount）"
    if "price precision" in t or "contract specification" in t:
        return "价格精度（公告措辞：Price Precision）"
    if "tick size" in t:
        return "价格步长 tickSize（公告措辞：Tick Size）"
    hdr = " ".join(table[0]) if table else ""
    return f"未在标题声明字段，表头={hdr}"


def row_effective(header, row):
    """取**这一行自己**的生效时间，并回带它来自哪一列。

    必须按行取：一篇公告常一次改多个合约、每个合约时间不同
    （例：COTI 那篇 12 个合约分布在 06:30/06:31/06:40/06:41/… 共 12 个时刻），
    拿全文第一个时间当该行的生效时间会张冠李戴。
    """
    for i, cell in enumerate(row):
        m = DATE_RE.search(str(cell or ""))
        if m:
            col = header[i] if i < len(header) else f"col{i}"
            return _eff(*m.groups()), col
    return None, None


def _eff_iso(eff):
    """`2026-07-21 06:40` → `2026-07-21T06:40:00Z`（公告原文只到分钟，秒按 00 写并标注）。"""
    if not eff:
        return None
    return eff.replace(" ", "T") + ":00Z"


def main():
    rows = []
    files = sorted(os.listdir(RAW_DIR))
    for fn in files:
        if not fn.endswith(".json"):
            continue
        path = os.path.join(RAW_DIR, fn)
        sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
        j = json.load(open(path, encoding="utf-8"))
        d = j.get("data") or {}
        body, tables = parse_body(d.get("body") or "")
        effs = sorted({_eff(a, b, c, e, f, ap) for a, b, c, e, f, ap in DATE_RE.findall(body)})
        for tb in tables:
            hit = [r for r in tb if any(_norm(c) in SYMBOLS for c in r)]
            if not hit:
                continue
            for r in hit:
                eff_row, eff_col = row_effective(tb[0] if tb else [], r)
                basis = "row" if eff_row else "body_first"
                ambiguous = bool(not eff_row and len(effs) > 1)
                rows.append({
                    "code": d.get("code"),
                    "title": d.get("title"),
                    "publish_ms": d.get("publishDate"),
                    "publish_utc": _iso(d.get("publishDate")),
                    "effective_utc_row": eff_row,
                    "effective_utc_row_iso": _eff_iso(eff_row),
                    "effective_utc_row_column": eff_col,
                    "effective_utc_declared": effs,
                    # 该行的生效时间是怎么来的：row=行内时间列；body_first=正文首个时间（需人工确认）
                    "effective_utc_basis": basis,
                    "effective_utc_ambiguous": ambiguous,
                    "field_hint": field_hint(d.get("title") or "", tb),
                    "header": tb[0] if tb else [],
                    "row": r,
                    "symbols_in_row": [s for s in SYMBOLS if any(_norm(c) == s for c in r)],
                    "url": f"https://www.binance.com/en/support/announcement/{d.get('code')}",
                    "evidence_file": fn,
                    "evidence_sha256": sha,
                })
    rows.sort(key=lambda x: (x["publish_ms"] or 0))
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump({"generated_utc": _now(), "articles_scanned": len([x for x in files if x.endswith('.json')]),
                   "matched_rows": len(rows), "rows": rows}, f, ensure_ascii=False, indent=1)
    with open(OUT_JSONL, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"scanned {len([x for x in files if x.endswith('.json')])} articles, matched {len(rows)} rows")
    for r in rows:
        eff = r["effective_utc_row"] or (r["effective_utc_declared"] or [None])[0]
        print(r["publish_utc"], "|", r["row"], "|", eff, "|", r["title"][:70])


def _iso(ms):
    import datetime
    if not ms:
        return None
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm(cell):
    """表格单元归一：去空格、去斜杠、大写 ⇒ `BNB/USDT` 与 `BNBUSDT` 等价。"""
    return re.sub(r"[\s/]+", "", str(cell or "")).upper()


def _eff(y, mo, d, hh, mm, ampm):
    """把公告里的 `YYYY-MM-DD HH:MM (UTC)` / `... HH:MM AM (UTC)` 归一成 24 小时制。"""
    h = int(hh)
    if ampm == "PM" and h != 12:
        h += 12
    elif ampm == "AM" and h == 12:
        h = 0
    return f"{y}-{mo}-{d} {h:02d}:{mm}"


def _now():
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


if __name__ == "__main__":
    main()
