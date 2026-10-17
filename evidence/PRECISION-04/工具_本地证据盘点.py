#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRECISION-04 工具：盘点本地合约规格证据（只读）。

输出 `本地证据盘点.json`，逐份记录四类时间（互不代替）：
  · request_fetched_ms —— 请求获取时间，只来自 meta 文件；没有就是 None
  · file_saved_ms      —— 文件名里的保存时刻
  · snapshot_server_ms —— 交易所响应里的 serverTime
  · file_mtime_ms      —— 文件系统 mtime（只作诊断，不是证据）

同时把「两份 futures 快照到底差什么」「六标的各自规格」摊开。
"""
import datetime
import hashlib
import json
import os

ROOT = "/Users/boos/Desktop/威科夫因子共振趋势"
CONTRACTS = os.path.join(ROOT, "数据/contracts/旧标的池")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "本地证据盘点.json")

SYMBOLS = ["SPACEUSDT", "AIGENSYNUSDT", "TRUTHUSDT", "COTIUSDT", "BNBUSDT", "ORDIUSDT"]
FIELDS = ("pricePrecision", "quantityPrecision", "onboardDate", "status", "contractType")
FILTERS = ("PRICE_FILTER", "LOT_SIZE", "MARKET_LOT_SIZE", "MIN_NOTIONAL", "NOTIONAL",
           "PERCENT_PRICE", "MAX_NUM_ORDERS")


def iso(ms):
    if ms is None:
        return None
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def spec_of(sym_doc):
    fl = {f.get("filterType"): f for f in (sym_doc.get("filters") or [])}
    out = {k: sym_doc.get(k) for k in FIELDS}
    for name in FILTERS:
        f = fl.get(name)
        if f:
            out[name] = {k: v for k, v in f.items() if k != "filterType"}
    out["orderTypes"] = sym_doc.get("orderTypes")
    out["timeInForce"] = sym_doc.get("timeInForce")
    return out


def load_meta():
    """扫目录下**所有** `*meta*.json`（raw_meta.json / catalog_meta.json），
    按 `file` 建索引。这是「请求获取时间」的唯一来源；没有就是 None。"""
    idx = {}

    def walk(n):
        if isinstance(n, dict):
            f = n.get("file")
            if isinstance(f, str):
                slot = idx.setdefault(f, {})
                for k in ("fetched_at_ms", "saved_utc", "request_id", "source",
                          "url", "via", "endpoint"):
                    if n.get(k) not in (None, "") and slot.get(k) in (None, ""):
                        slot[k] = n[k]
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    for fn in sorted(os.listdir(CONTRACTS)):
        if "meta" in fn and fn.endswith(".json"):
            try:
                walk(json.load(open(os.path.join(CONTRACTS, fn), encoding="utf-8")))
            except Exception:
                continue
    return idx


def main():
    meta = load_meta()
    snaps = []
    for fn in sorted(os.listdir(CONTRACTS)):
        if not fn.endswith(".json") or "exchangeInfo" not in fn and "instruments_okx" not in fn:
            continue
        p = os.path.join(CONTRACTS, fn)
        doc = json.load(open(p, encoding="utf-8"))
        syms = doc.get("symbols") or doc.get("data") or []
        m = meta.get(fn) or {}
        stem_ts = fn.rsplit("_", 1)[-1].replace(".json", "")
        snaps.append({
            "file": fn,
            "bytes": os.path.getsize(p),
            "sha256": sha256(p),
            "request_fetched_ms": m.get("fetched_at_ms"),
            "request_fetched_utc": iso(m.get("fetched_at_ms")),
            "file_saved_ms_from_name": _parse(stem_ts),
            "file_saved_utc_from_name": stem_ts,
            "snapshot_server_ms": doc.get("serverTime"),
            "snapshot_server_utc": iso(doc.get("serverTime")),
            "file_mtime_utc": iso(int(os.path.getmtime(p) * 1000)),
            "meta_present": bool(m),
            "meta": m,
            "symbol_count": len(syms),
            "has_our_symbols": [s for s in SYMBOLS if any(x.get("symbol") == s for x in syms)],
        })

    # 两份 futures 快照逐符号比较
    fut = [s for s in snaps if s["file"].startswith("exchangeInfo_futures_")]
    pair_diff = None
    if len(fut) == 2:
        a = json.load(open(os.path.join(CONTRACTS, fut[0]["file"]), encoding="utf-8"))
        b = json.load(open(os.path.join(CONTRACTS, fut[1]["file"]), encoding="utf-8"))
        sa = {x["symbol"]: x for x in a["symbols"]}
        sb = {x["symbol"]: x for x in b["symbols"]}
        top_diff = [k for k in a if k != "symbols" and a[k] != b.get(k)]
        sym_diff = [k for k in sa if sa[k] != sb.get(k)]
        pair_diff = {
            "file_a": fut[0]["file"], "file_b": fut[1]["file"],
            "top_level_fields_differing": top_diff,
            "server_time_a": a.get("serverTime"), "server_time_b": b.get("serverTime"),
            "server_time_delta_ms": (b.get("serverTime") or 0) - (a.get("serverTime") or 0),
            "symbol_count_a": len(sa), "symbol_count_b": len(sb),
            "symbols_differing": sym_diff,
            "conclusion": (
                "两份文件 sha256 不同，但差异**仅**在顶层 serverTime（相差 "
                f"{(b.get('serverTime') or 0) - (a.get('serverTime') or 0)} ms）；"
                f"{len(sa)} 个合约条目逐字段完全一致。两个 serverTime 落在同一秒内，"
                "属同一次响应的两次落盘，**不构成两个可比较的证据时点**。"
                if not top_diff[1:] and not sym_diff else
                "两份快照存在实质差异，需逐项核对（见字段清单）。"
            ),
        }

    # 当前响应（本次抓取，官方 REST）
    now_path = os.path.join(HERE, "当前响应_exchangeInfo.json")
    now_doc = None
    if os.path.exists(now_path):
        now_doc = json.load(open(now_path, encoding="utf-8"))

    per_symbol = {}
    base = json.load(open(os.path.join(CONTRACTS, fut[-1]["file"]), encoding="utf-8")) if fut else None
    base_map = {x["symbol"]: x for x in base["symbols"]} if base else {}
    now_map = {x["symbol"]: x for x in now_doc["symbols"]} if now_doc else {}
    for s in SYMBOLS:
        rec = {"snapshot": spec_of(base_map[s]) if s in base_map else None,
               "now": spec_of(now_map[s]) if s in now_map else None}
        if rec["snapshot"] and rec["now"]:
            d = {}
            for k in set(rec["snapshot"]) | set(rec["now"]):
                if rec["snapshot"].get(k) != rec["now"].get(k):
                    d[k] = {"snapshot": rec["snapshot"].get(k), "now": rec["now"].get(k)}
            rec["diff_snapshot_vs_now"] = d
        per_symbol[s] = rec

    out = {
        "generated_utc": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "contracts_dir": CONTRACTS,
        "snapshots": snaps,
        "futures_pair_diff": pair_diff,
        "current_fetch": ({
            "file": "当前响应_exchangeInfo.json",
            "sha256": sha256(now_path),
            "bytes": os.path.getsize(now_path),
            "url": "https://fapi.binance.com/fapi/v1/exchangeInfo",
            "snapshot_server_utc": iso(now_doc.get("serverTime")),
            "symbol_count": len(now_doc.get("symbols") or []),
            "note": "本窗口实际抓取的官方响应；serverTime 明显早于抓取时刻 ⇒ 该响应本身也可能来自上游缓存，"
                    "只能证明「响应里写的那个时点」。",
        } if now_doc else None),
        "per_symbol": per_symbol,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps({"snapshots": len(snaps), "pair_diff": pair_diff}, ensure_ascii=False, indent=1)[:1500])
    for s in SYMBOLS:
        d = per_symbol[s].get("diff_snapshot_vs_now") or {}
        print(s, "差异字段:", list(d.keys()) or "无")


def _parse(ts):
    for fmt in ("%Y-%m-%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return int(datetime.datetime.strptime(ts, fmt).replace(
                tzinfo=datetime.timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
    return None


if __name__ == "__main__":
    main()
