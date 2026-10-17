#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRECISION-05 工具：独立复核 PRECISION-04 的证据链。

原则：**不读 PRECISION-04 的结论字段当输入**，只读它的原始数据（原始公告 / 存档快照 /
本地快照 / 当前响应），用本脚本自己的解析与自己的分级实现重算一遍，再与
PRECISION-04 的 JSON 逐项比对。对不上的地方逐条列出。

五段：
  A 公告层    —— 327 篇正文里六标的出现几次；4 篇上线规格表到底给了哪几行；
                6 条变更的 before/after 与生效时间能否在原文里找到
  B 存档层    —— 29 次捕获的 serverTime 自洽性重算；usable 数量与剔除原因
  C 时间线层  —— 23 条时间线记录逐条回源文件重读六标的 6 个字段
  D 逐段表    —— 用自建 obs 序列独立重算 20 个分段的等级，与 PRECISION-04 比对
  E 窗口      —— 六标的窗口是否等于各 run inputs.json 里的 time_range_1h

用法：python3 工具_复核PRECISION04.py
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
P04 = os.path.join(ROOT, "evidence", "PRECISION-04")
RUNS = os.path.join(ROOT, "回测日志", "event_trend_v0_1", "runs")
OUT = os.path.join(HERE, "PRECISION04复核结果.json")

SYMBOLS = ["SPACEUSDT", "AIGENSYNUSDT", "TRUTHUSDT", "COTIUSDT", "BNBUSDT", "ORDIUSDT"]
ALIASES = {s: {s, s[:-4] + "/USDT", s[:-4] + "/ USDT"} for s in SYMBOLS}

# PRECISION-04 声称的 6 条命中公告（code → 标的），只用来"按图索骥"回原文核对
CLAIMED = {
    "790cd97ae5d54cd6897ad4fc81ab477b": "BNBUSDT",
    "5b64c7eaf295445584f222adfbf8d723": "ORDIUSDT",
    "5559a6615fd9448bad12a1ceda130428": "TRUTHUSDT",
    "3edd59bc38c147259945b634a1d2c039": "SPACEUSDT",
    "524345e64dd14a23989bfe1e547760b5": "AIGENSYNUSDT",
    "f74b3070321a4cdaaea15f9dd8ceb0e8": "COTIUSDT",
}

# PRECISION-04 报告表里的关键断言（值、生效时间），回原文核对是否真的出现
CLAIMED_FACTS = {
    "BNBUSDT": {"values": ["0.001", "0.01"], "eff": "2021-05-11 03:30"},
    "ORDIUSDT": {"values": ["0.001"], "eff": "2023-11-07 12:30"},
    "TRUTHUSDT": {"values": ["0.000001"], "eff": "2025-10-01 12:00"},
    "SPACEUSDT": {"values": ["0.000001", "5 USDT"], "eff": "2026-01-23 11:45"},
    "AIGENSYNUSDT": {"values": ["0.00001", "5 USDT"], "eff": "2026-04-29 14:15"},
    "COTIUSDT": {"values": ["0.00001", "0.000001"], "eff": "2026-07-21 06:40"},
}

SPEC_KEYS = ["tick", "lot_step", "lot_min", "lot_max", "mkt_max", "min_notional"]

# 时间线/源文件用的字段名 → PRECISION-04 逐段表用的字段名
FIELD_MAP = {"tick": "tick", "lot_step": "qty_step", "lot_min": "min_qty",
             "lot_max": "max_qty", "mkt_max": "mkt_max_qty",
             "min_notional": "min_notional"}


# ══════════════════════════ 通用 ══════════════════════════
def body_text(raw: str) -> str:
    """把公告正文拍平成文本。JSON 节点树取 text 节点；HTML 去标签。

    与 PRECISION-04 的解析是**两套独立实现**（它按 <tr>/tag 抽表格行，
    这里只做全文拍平），所以能互相印证「数字到底在不在原文里」。
    """
    s = (raw or "").strip()
    if s.startswith("{") or s.startswith("["):
        try:
            tree = json.loads(s)
        except Exception:  # noqa: BLE001
            return re.sub(r"<[^>]+>", " ", s)
        buf = []

        def walk(n):
            if isinstance(n, dict):
                if n.get("node") == "text":
                    buf.append(str(n.get("text") or ""))
                for c in n.get("child") or []:
                    walk(c)
                if isinstance(n.get("content"), (dict, list)):
                    walk(n["content"])
            elif isinstance(n, list):
                for c in n:
                    walk(c)

        walk(tree)
        return "\n".join(buf)
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    t = re.sub(r"</(p|li|tr|div|br|h\d)>", "\n", t, flags=re.I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#39;", "'")
         .replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">"))
    return re.sub(r"[ \t]+", " ", t)


def flat(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def same_num(a, b) -> bool:
    """数值等价比较：`0.01` 与 `0.010` 视为相同（只是文本写法不同）。"""
    if a is None or b is None:
        return a is b
    try:
        from decimal import Decimal
        return Decimal(str(a)) == Decimal(str(b))
    except Exception:  # noqa: BLE001
        return str(a) == str(b)


def tables_of(raw: str):
    """独立表格抽取（JSON 节点树 / HTML 两种格式）。与 PRECISION-04 的实现不同源。"""
    s = (raw or "").strip()
    out = []
    if s.startswith("{") or s.startswith("["):
        try:
            tree = json.loads(s)
        except Exception:  # noqa: BLE001
            tree = None
        if tree is not None:
            def cell(n):
                buf = []

                def w(x):
                    if isinstance(x, dict):
                        if x.get("node") == "text":
                            buf.append(str(x.get("text") or ""))
                        for c in x.get("child") or []:
                            w(c)
                    elif isinstance(x, list):
                        for c in x:
                            w(c)
                w(n)
                return re.sub(r"\s+", " ", "".join(buf)).strip()

            def walk(n):
                if isinstance(n, dict):
                    if n.get("tag") == "tr":
                        out.append([cell(c) for c in (n.get("child") or [])])
                        return
                    for c in n.get("child") or []:
                        walk(c)
                elif isinstance(n, list):
                    for c in n:
                        walk(c)

            walk(tree)
            return out
    for tr in re.findall(r"<tr.*?</tr>", s, flags=re.S | re.I):
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, flags=re.S | re.I)]
        cells = [re.sub(r"\s+", " ", c) for c in cells]
        if cells:
            out.append(cells)
    return out


def launch_fields_for(sym, hits):
    """从上线公告的竖排规格表里取 Tick Size / Minimum Trade Amount / Min Notional。"""
    code = next((c for c, s in CLAIMED.items() if s == sym), None)
    if not code or code not in hits:
        return {}
    p = os.path.join(P04, "原始公告", hits[code]["file"])
    raw = json.load(open(p, encoding="utf-8"))["data"].get("body") or ""
    f = {}
    for r in tables_of(raw):
        if len(r) < 2:
            continue
        k = flat(r[0]).lower()
        v = r[1]
        if k == "ticksize":
            f["tick"] = v
        elif k == "minimumtradeamount":
            mm = re.match(r"[\d.]+", v.strip())
            if mm:
                f["lot_min"] = mm.group(0)
        elif k == "minimumnotionalvalue":
            mm = re.match(r"[\d.]+", v.strip())
            if mm:
                f["min_notional"] = mm.group(0)
    return f


def ms(iso: str) -> int:
    import datetime
    return int(datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
               .replace(tzinfo=datetime.UTC).timestamp() * 1000)


# ══════════════════════════ A 公告层 ══════════════════════════
def probe_announcements():
    files = sorted(glob.glob(os.path.join(P04, "原始公告", "*.json")))
    hits = {}
    for p in files:
        j = json.load(open(p, encoding="utf-8"))
        d = j.get("data") or {}
        code = d.get("code") or os.path.basename(p).split("_")[-1].replace(".json", "")
        txt = flat(body_text(d.get("body") or ""))
        found = [s for s in SYMBOLS if any(flat(a) in txt for a in ALIASES[s])]
        if found:
            hits[code] = {"file": os.path.basename(p), "symbols": found,
                          "title": d.get("title")}

    fact_checks = []
    for code, sym in CLAIMED.items():
        rec = {"symbol": sym, "code": code}
        if code not in hits:
            rec["found_in_raw"] = False
            fact_checks.append(rec)
            continue
        p = os.path.join(P04, "原始公告", hits[code]["file"])
        d = json.load(open(p, encoding="utf-8"))["data"]
        txt = body_text(d.get("body") or "")
        f = flat(txt)
        rec["found_in_raw"] = True
        rec["title_matches"] = bool(hits[code]["title"] == d.get("title"))
        rec["values_present"] = {v: (flat(v) in f) for v in CLAIMED_FACTS[sym]["values"]}
        eff = CLAIMED_FACTS[sym]["eff"]
        rec["effective_present"] = flat(eff) in f
        # 该篇公告里出现的全部 "YYYY-MM-DD HH:MM" 时刻（独立提取，用于暴露多合约时间陷阱）
        rec["all_times_in_body"] = sorted(set(
            m.replace("T", " ") for m in re.findall(
                r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}", txt)))
        fact_checks.append(rec)

    # 上线规格表到底给了哪几行（独立抽：把 "Tick Size" 后面 80 字符内的数字抓出来）
    spec_rows = {}
    for sym in ["SPACEUSDT", "AIGENSYNUSDT", "TRUTHUSDT", "ORDIUSDT"]:
        code = next(c for c, s in CLAIMED.items() if s == sym)
        p = os.path.join(P04, "原始公告", hits[code]["file"])
        txt = body_text(json.load(open(p, encoding="utf-8"))["data"].get("body") or "")
        n = flat(txt)
        spec_rows[sym] = {
            "has_TickSize": "Ticksize" in n.lower() or "TickSize" in n,
            "has_MinTradeAmount": "MinimumTradeAmount" in n,
            "has_MinNotional": "MinimumNotionalValue" in n,
        }

    # 「15 分钟更新」免责声明
    provisional = {}
    for sym in SYMBOLS:
        code = next((c for c, s in CLAIMED.items() if s == sym), None)
        if not code or code not in hits:
            provisional[sym] = None
            continue
        p = os.path.join(P04, "原始公告", hits[code]["file"])
        txt = body_text(json.load(open(p, encoding="utf-8"))["data"].get("body") or "")
        provisional[sym] = "updated15minutesbeforethelaunchtime" in flat(txt).lower()

    idx = {}
    for name in ("公告索引.json", "公告索引_48新币上线.json"):
        d = json.load(open(os.path.join(P04, name), encoding="utf-8"))
        idx[name] = {"reported_total": d["reported_total"], "fetched": d["fetched"],
                     "articles": len(d["articles"]), "catalog": d["catalogName"]}

    return {
        "raw_files_scanned": len(files),
        "announcements_containing_any_of_6": len(hits),
        "hit_codes": sorted(hits),
        "hit_titles": {c: hits[c]["title"] for c in hits},
        "claim_vs_raw": fact_checks,
        "launch_spec_table_rows": spec_rows,
        "provisional_disclaimer": provisional,
        "index_totals": idx,
        "index_total_sum": sum(v["reported_total"] for v in idx.values()),
    }


# ══════════════════════════ B 存档层 ══════════════════════════
def probe_archives():
    idx = json.load(open(os.path.join(P04, "存档快照索引.json"), encoding="utf-8"))
    max_skew = idx["max_skew_ms"]
    rows, bad = [], []
    for c in idx["captures"]:
        p = os.path.join(P04, "存档快照", c.get("local_file") or "")
        wb = c.get("wayback_utc")
        if not wb and c.get("wayback_ts"):
            ts = str(c["wayback_ts"])
            wb = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}T{ts[8:10]}:{ts[10:12]}:{ts[12:14]}Z"
        r = {"wayback_utc": wb, "status": c.get("status"),
             "index_usable": c.get("usable"), "index_reason": c.get("reason"),
             "file_exists": os.path.exists(p)}
        if r["file_exists"]:
            try:
                doc = json.load(open(p, encoding="utf-8"))
                st = doc.get("serverTime")
                r["file_server_ms"] = st
                r["index_server_ms"] = c.get("snapshot_server_ms")
                r["server_ms_matches_index"] = (st == c.get("snapshot_server_ms"))
                if st is not None and wb:
                    skew = int(st) - ms(wb)
                    r["recomputed_skew_ms"] = skew
                    r["within_max_skew"] = abs(skew) <= max_skew
                r["symbol_count_recomputed"] = len(doc.get("symbols") or [])
                r["symbol_count_matches_index"] = (
                    r["symbol_count_recomputed"] == c.get("symbol_count"))
            except Exception as e:  # noqa: BLE001
                r["parse_error"] = str(e)
        rows.append(r)
        if c.get("usable"):
            if not r.get("file_exists") or not r.get("within_max_skew") \
                    or not r.get("server_ms_matches_index"):
                bad.append(r)

    usable = [c for c in idx["captures"] if c.get("usable")]
    rej = [c for c in idx["captures"] if not c.get("usable")]
    rej_kind = {}
    for c in rej:
        k = "http_" + str(c.get("status")) if str(c.get("status")) != "200" else "skew"
        rej_kind.setdefault(k, []).append(
            c.get("wayback_utc") or c.get("wayback_ts") or "?")
    return {
        "max_skew_ms": max_skew,
        "captures_total": len(idx["captures"]),
        "usable_count": len(usable),
        "rejected_count": len(rej),
        "rejected_kinds": rej_kind,
        "usable_but_inconsistent": bad,
        "usable_server_utc": sorted(c["snapshot_server_utc"] for c in usable),
        "rows": rows,
    }


# ══════════════════════════ C 时间线层 ══════════════════════════
def _read_spec_from_file(path, sym):
    doc = json.load(open(path, encoding="utf-8"))
    for s in doc.get("symbols") or []:
        if s.get("symbol") != sym:
            continue
        f = {x.get("filterType"): x for x in s.get("filters") or []}
        lot = f.get("LOT_SIZE") or {}
        mkt = f.get("MARKET_LOT_SIZE") or {}
        pf = f.get("PRICE_FILTER") or {}
        mn = f.get("MIN_NOTIONAL") or {}
        return {
            "tick": pf.get("tickSize"),
            "lot_step": lot.get("stepSize"),
            "lot_min": lot.get("minQty"),
            "lot_max": lot.get("maxQty"),
            "mkt_max": mkt.get("maxQty"),
            "min_notional": mn.get("notional"),
        }
    return None


def probe_timeline():
    tl = json.load(open(os.path.join(P04, "规格时间线.json"), encoding="utf-8"))
    rows, mismatch = [], []
    for t in tl["timeline"]:
        src = t.get("file")
        kind = t.get("kind")
        if kind == "archive_capture":
            path = os.path.join(P04, "存档快照", src)
        elif kind == "local_snapshot":
            path = None  # 本地快照：按 basename 在 数据/contracts 下找
            for cand in glob.glob(os.path.join(
                    ROOT, "数据", "contracts", "**", os.path.basename(src or "")),
                    recursive=True):
                path = cand
                break
        else:
            path = os.path.join(P04, src) if src else None
        r = {"server_utc": t["server_utc"], "kind": kind, "file": src,
             "source_found": bool(path and os.path.exists(path))}
        if r["source_found"]:
            diffs = {}
            for sym in SYMBOLS:
                claimed = t["present"].get(sym)
                if not claimed:
                    continue
                actual = _read_spec_from_file(path, sym)
                if actual is None:
                    diffs[sym] = {"claimed": claimed, "actual": "symbol_absent_in_source"}
                    continue
                d = {k: (claimed.get(k), actual.get(k))
                     for k in SPEC_KEYS if not same_num(claimed.get(k), actual.get(k))}
                if d:
                    diffs[sym] = d
            r["field_diffs"] = diffs
            if diffs:
                mismatch.append(r)
        rows.append(r)
    return {
        "timeline_count": len(tl["timeline"]),
        "sources_missing": [r for r in rows if not r["source_found"]],
        "field_mismatches": mismatch,
        "rows": rows,
    }


# ══════════════════════════ D 逐段表 ══════════════════════════
def build_obs(tl):
    """按 serverTime 时刻去重，得到 {sym: [(utc, spec_dict), ...]}。"""
    out = {s: [] for s in SYMBOLS}
    seen = {s: set() for s in SYMBOLS}
    for t in tl["timeline"]:
        for s in SYMBOLS:
            p = t["present"].get(s)
            if not p:
                continue
            key = t["server_utc"]
            if key in seen[s]:
                continue
            seen[s].add(key)
            out[s].append((key, {k: p.get(k) for k in SPEC_KEYS}))
    for s in SYMBOLS:
        out[s].sort(key=lambda x: x[0])
    return out


PROVISIONAL = {"TRUTHUSDT"}


def grade(sym, field, t0, t1, obs, launch_fields):
    in_seg = [x for x in obs if t0 <= x[0] <= t1]
    before = [x for x in obs if x[0] < t0]
    after = [x for x in obs if x[0] > t1]
    if in_seg:
        vals = [x[1][field] for x in in_seg]
        n = sum(1 for a, b in zip(vals, vals[1:]) if not same_num(a, b))
        return "A", vals[-1], n
    if field in ("lot_max", "mkt_max"):
        return "C", None, 0
    lv = launch_fields.get(field)
    if lv is not None and (not before or same_num(before[-1][1][field], lv)):
        if sym in PROVISIONAL:
            return "C", None, 0
        return "B", lv, 0
    if before and after and same_num(before[-1][1][field], after[0][1][field]):
        return "B", before[-1][1][field], 0
    return "C", None, 0


def probe_segments():
    tl = json.load(open(os.path.join(P04, "规格时间线.json"), encoding="utf-8"))
    p04 = json.load(open(os.path.join(P04, "六标的逐时间段证据.json"), encoding="utf-8"))
    obs = build_obs(tl)
    detail = json.load(open(os.path.join(P04, "公告规格变更明细.json"), encoding="utf-8"))

    # 变更点（公告生效时刻），用于切段
    splits = {}
    for r in detail["rows"]:
        sym = r["symbols_in_row"][0]
        eff = r.get("effective_utc_row_iso")
        if eff and "Tick Size" in (r.get("field_hint") or "") and "Updates on Tick" in (
                r.get("title") or ""):
            splits.setdefault(sym, []).append(eff)
        else:
            splits.setdefault(sym, [])

    # 上线时刻与上线公告给出的字段（独立解析）
    hits = {}
    for p in glob.glob(os.path.join(P04, "原始公告", "*.json")):
        j = json.load(open(p, encoding="utf-8"))
        d = j.get("data") or {}
        hits[d.get("code")] = {"file": os.path.basename(p), "title": d.get("title")}

    launch = {}
    for sym in SYMBOLS:
        lf = launch_fields_for(sym, hits)
        eff = None
        code = next((c for c, s in CLAIMED.items() if s == sym), None)
        if code and code in hits:
            raw = json.load(open(os.path.join(P04, "原始公告", hits[code]["file"]),
                                 encoding="utf-8"))["data"].get("body") or ""
            times = sorted(set(re.findall(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}", body_text(raw))))
            eff = times[0].replace(" ", "T") + ":00Z" if times else None
        launch[sym] = (eff, lf)

    report = {}
    diffs = []
    for entry in p04["symbols"]:
        sym = entry["symbol"]
        # 窗口独立取自 run 的 inputs.json
        rid = _run_id_for(sym)
        ws, we = _window_from_run(rid)
        report[sym] = {"window_from_inputs_json": [ws, we],
                       "window_in_p04": entry["window"],
                       "window_matches": [ws, we] == entry["window"]}
        if [ws, we] != entry["window"]:
            diffs.append({"symbol": sym, "kind": "window",
                          "inputs_json": [ws, we], "p04": entry["window"]})
        lf = launch.get(sym, (None, {}))[1]
        for seg in entry["segments"]:
            t0, t1 = seg["segment"].split(" → ")
            for f in SPEC_KEYS:
                g, v, n = grade(sym, f, t0, t1, obs[sym], lf)
                claimed = seg["fields"][FIELD_MAP[f]]
                if g != claimed["grade"] or str(v) != str(claimed["value"]):
                    diffs.append({"symbol": sym, "segment": seg["segment"], "field": f,
                                  "recomputed": [g, v], "p04": [claimed["grade"],
                                                                claimed["value"]]})
            # obs 计数也要对
            n_obs = len([x for x in obs[sym] if t0 <= x[0] <= t1])
            if n_obs != len(seg["in_segment_observations"]):
                diffs.append({"symbol": sym, "segment": seg["segment"],
                              "kind": "obs_count", "recomputed": n_obs,
                              "p04": len(seg["in_segment_observations"])})
    return {"diffs": diffs, "per_symbol": report,
            "obs_counts": {s: len(obs[s]) for s in SYMBOLS}}


def _iso_from_row(r):
    t = r.get("effective_utc_row")
    if not t:
        return None
    return t.replace(" ", "T") + ":00Z"


def _launch_fields_from_row(r):
    """上线公告的规格表是竖排；这里只认 Tick Size 一行（其余字段由 A 段另行核对）。"""
    return {}


def _run_id_for(sym):
    m = {"SPACEUSDT": "1792174451", "AIGENSYNUSDT": "1792196703",
         "TRUTHUSDT": "1792174465", "COTIUSDT": "1792174145",
         "BNBUSDT": "1792172973", "ORDIUSDT": "1792171568"}
    return f"run_WX_EVENT_TREND_V0_1_{sym}_{m[sym]}"


def _window_from_run(run_id):
    d = json.load(open(os.path.join(RUNS, run_id, "inputs.json"), encoding="utf-8"))
    tr = d["data_summary"]["time_range_1h"]
    return (tr["start_utc"].replace(" ", "T") + "Z",
            tr["end_utc"].replace(" ", "T") + "Z")


# ══════════════════════════ main ══════════════════════════
def main():
    import time
    out = {"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    out["A_公告"] = probe_announcements()
    out["B_存档"] = probe_archives()
    out["C_时间线"] = probe_timeline()
    out["D_逐段"] = probe_segments()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    a = out["A_公告"]
    print("=" * 88)
    print(f'A 公告：扫描 {a["raw_files_scanned"]} 篇，含六标的的 {a["announcements_containing_any_of_6"]} 篇')
    print(f'   索引合计 {a["index_total_sum"]} 篇（{", ".join(f"{k}:{v[chr(114)+chr(101)+chr(112)+chr(111)+chr(114)+chr(116)+chr(101)+chr(100)+chr(95)+chr(116)+chr(111)+chr(116)+chr(97)+chr(108)]}" for k,v in a["index_totals"].items())}）')
    for r in a["claim_vs_raw"]:
        ok = r.get("found_in_raw") and all(r.get("values_present", {}).values()) \
            and r.get("effective_present")
        print(f'   [{"OK" if ok else "!!"}] {r["symbol"]:<14} 原文命中={r.get("found_in_raw")} '
              f'值={r.get("values_present")} 生效时间={r.get("effective_present")}')
    print("   上线规格表覆盖:", json.dumps(a["launch_spec_table_rows"], ensure_ascii=False))
    print("   「上线前15分钟更新」免责:", json.dumps(a["provisional_disclaimer"], ensure_ascii=False))

    b = out["B_存档"]
    print("=" * 88)
    print(f'B 存档：{b["captures_total"]} 次捕获，usable {b["usable_count"]}，'
          f'剔除 {b["rejected_count"]} {json.dumps(b["rejected_kinds"], ensure_ascii=False)}')
    print(f'   usable 里 serverTime 与索引不符/超 skew 的：{len(b["usable_but_inconsistent"])}')

    c = out["C_时间线"]
    print("=" * 88)
    print(f'C 时间线：{c["timeline_count"]} 条；源文件缺失 {len(c["sources_missing"])}；'
          f'字段与源文件不符 {len(c["field_mismatches"])}')

    d = out["D_逐段"]
    print("=" * 88)
    print(f'D 逐段：差异 {len(d["diffs"])} 处')
    for x in d["diffs"][:20]:
        print("   ", json.dumps(x, ensure_ascii=False))
    print("   obs 计数:", json.dumps(d["obs_counts"], ensure_ascii=False))
    print("\n写出:", OUT)


if __name__ == "__main__":
    main()
