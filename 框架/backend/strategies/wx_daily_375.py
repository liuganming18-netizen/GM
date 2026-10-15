#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第27窗口：375 逐日名单与实际输入**薄适配**（只读真实源；零网络；不写生产库）。

职责（见 `Work派工_2026-10-11/121_Work新窗口_375逐日名单与实际输入接通27.md`）：
  · 复用 19 名单 + 10/12 逐日资料 + **原资格算法**（`身份假设连续性审查.py`），
    在新日历（118 人类确认）下**重算** 375 池逐日原资格（90 上市龄 / 此前 30 完整 UTC 日
    成交额中位 ≥500 万 U / 连续 240 有效小时预热）。
  · **掩码分离**：`orig_qual`（原资格，无禁入）∧ `ban_mask`（新段首 30 日禁入）∧
    `tradeable_mask`（价格 / 全小时 mark 可交易）——三者分别登记，不静默缩 375 池。
  · 产出 `run_arm` 直接消费的 `rank_keys_by_day`（此前 30 日成交额中位，降序）
    与 `admission_by_day`（原资格 ∧ ¬禁入），键 = `(exchange, symbol)`，日 = UTC00 ms。
  · 与 12 CSV（旧段首禁入已过滤）**交叉核对**（重叠日应逐值一致）。
  · 每笔开仓/拒单可关联当日完整资格证据（日期 / 上市龄 / 此前 30 日完整度 / 中位 /
    240h / 禁入 / 排序依据 / 源路径 / SHA / 版本）。

**不**下载、**不**联网、**不**写生产行情库、**不**改共享执行器/账本、**不**跑整段收益。
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.feather as feather

H = 3_600_000
FOURH_MS = 4 * H
DAY = 24 * H
UTC = dt.timezone.utc
EXTREME_RATE = 0.02                      # 极端情景：每整点 1h、单边不利 2%
NORM4H_KIND = "norm4h_fixed_last_known"  # 正常情景：固定 4h 整点 + 严格过去 last-known 费率

# ── 只读真实来源（第27窗口固定）────────────────────────────────────────────────
DECISION_DIR = Path("/Users/boos/.codex/worktrees/c9d7/加密货币趋势判断系统/"
                    "本次离线决策_2026-10-12")
AUDIT_DIR = Path("/Users/boos/.codex/worktrees/c9d7/加密货币趋势判断系统/"
                 "Work派工_2026-10-11")
POOL_LIST = DECISION_DIR / "19_最终375币假设研究池.csv"
SRC10 = DECISION_DIR / "10_442币连续性与实际准入汇总.csv"      # 价格文件 / 成交额文件
DAILY12 = DECISION_DIR / "12_通过连续性币的逐日准入资格.csv"    # 交叉核对
IDENT05 = DECISION_DIR / "05_526币身份可证明点与未证明时段.csv"  # 当前官方上市时间
POOL_EVIDENCE = DECISION_DIR / "13_最终假设研究币池与审查证据.json"
FEE_RULE = DECISION_DIR / "20_用户新假设与缺口费用压力规则.md"
SPEC_ROUNDING = DECISION_DIR / "07_526币保守执行近似规格.csv"
MODEL_DOC = DECISION_DIR / "08_具体近似模型与缺口持仓两方案.md"

CALENDAR_118 = AUDIT_DIR / "118_20201215新候选日历_待用户确认.json"

BACKEND = Path("/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现")
BN_DIR = BACKEND / "历史下载" / "perp_datadir_real" / "data" / "binance" / "futures"
SIDECAR = (BACKEND.parent / "最新唐奇安威科夫接通与收尾_2026-10-11" /
           "5000U收尾与回测条件终验6" / "回测输入收口" / "quote_sidecar")

CUTOFF_MS = int(dt.datetime(2026, 10, 10, 7, tzinfo=UTC).timestamp() * 1000)
PANEL_END_DAY_MS = int(dt.datetime(2026, 10, 10, 0, 0, tzinfo=UTC).timestamp() * 1000)

# ── 118 人类确认日历（**唯一事实源**；cutoff 从 manifest 解析，禁手写）──────────────
# 面板 2020-12-15→2026-10-09；DEV 至 2023-11-11；段首 30 日禁开，DEV 首开 2021-01-14。
CALENDAR = {
    "source": str(CALENDAR_118),
    "human_confirmed": True,
    "confirmed_at_utc": "2026-10-12T04:43:15.294862+00:00",
    "timezone": "UTC",
    "overall": ("2020-12-15", "2026-10-09"),
    "DEV": ("2020-12-15", "2023-11-11"),
    "VAL": ("2023-11-12", "2025-01-09"),
    "FINAL": ("2025-01-10", "2026-10-09"),
    "first_entry_days": {"DEV": "2021-01-14", "VAL": "2023-12-12", "FINAL": "2025-02-09"},
    "segment_head_ban_days": 30,
    "boundaries_cumulative": (1062, 1487),
    "total_days": 2125,
}

EXCHANGE = "binance"
ASSUMPTION = "研究期间视为同一合约；非已证明历史事实"
PURPOSE_LABEL = "近似回测，不是精确实盘还原"


# ── 基础工具 ────────────────────────────────────────────────────────────────
def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ms(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


def iso(ms):
    return dt.datetime.fromtimestamp(int(ms) / 1000, UTC).isoformat().replace("+00:00", "Z")


def _read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _base_of(symbol):
    """'AAVEUSDT'→'AAVE'；'币安人生USDT'→'币安人生'。"""
    return symbol[:-4] if symbol.endswith("USDT") else symbol


# ── 只读装载 ────────────────────────────────────────────────────────────────
def load_pool():
    syms = []
    with open(POOL_LIST, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            s = row.get("币")
            if s:
                syms.append(s)
    return {"path": str(POOL_LIST), "sha256": _sha256_file(POOL_LIST),
            "count": len(syms), "symbols": syms}


def source_map():
    """375 池每币的真实源（价格 1h feather / 成交额 1h npy / 15m / mark / funding）+ SHA。"""
    rows = {r["币"]: r for r in _read_csv(SRC10)}
    pool = load_pool()["symbols"]
    out = {}
    for sym in pool:
        r = rows[sym]
        price = Path(r["价格文件"])
        quote = Path(r["成交额文件"])
        base = price.name.split("_USDT_USDT-")[0]
        out[sym] = {
            "base": base,
            "price_1h": str(price), "quote_1h": str(quote),
            "price_1h_sha256": _sha256_file(price), "quote_1h_sha256": _sha256_file(quote),
            "price_15m": str(BN_DIR / f"{base}_USDT_USDT-15m-futures.feather"),
            "quote_15m": str(SIDECAR / f"{base}_USDT_USDT-15m-quote_volume.npy"),
            "mark_1h": str(BN_DIR / f"{base}_USDT_USDT-1h-mark.feather"),
            "funding_1h": str(BN_DIR / f"{base}_USDT_USDT-1h-funding_rate.feather"),
        }
    return out


def listing_map():
    """当前官方上市时间（ms）——`05` 只读；375 池必备。"""
    rows = {r["币"]: r for r in _read_csv(IDENT05)}
    return {s: _ms(rows[s]["当前官方上市时间"]) for s in load_pool()["symbols"]}


# ── 原资格算法（严格复用 `身份假设连续性审查.py`；**无禁入**）────────────────────
def _qualify_symbol(ts, o, hi, lo, cl, vol, q, listed_ms, panel_start_ms, panel_end_ms):
    """返回 [(day_ms, prior30_median, listed_days, warmup_hours)]（升序）。

    条件（与既有原规则一致）：上市 ≥90 日 ∧ 此前 30 个完整 UTC 日每日 24 有效小时 ∧
    此前 30 日成交额中位 ≥5e6 ∧ 该日 00:00 前连续 240 有效小时（末根为 dayms-H）。
    **不含段首禁入**（禁入另作 `ban_mask` 分离）。
    """
    full_ok = (np.isfinite(o) & np.isfinite(hi) & np.isfinite(lo) & np.isfinite(cl)
               & np.isfinite(vol) & np.isfinite(q)
               & (o > 0) & (hi > 0) & (lo > 0) & (cl > 0) & (vol >= 0) & (q >= 0))
    if ts.size == 0 or int(ts[0] // DAY) > int((panel_end_ms - 1) // DAY):
        return []                                   # 该币数据起点晚于面板 ⇒ 无资格日
    dmin = int(ts[0] // DAY)
    dmax = int((panel_end_ms - 1) // DAY)
    n = dmax - dmin + 1
    di = (ts // DAY - dmin)
    valid = (ts < panel_end_ms) & full_ok & (ts % H == 0)
    counts = np.bincount(di[valid], minlength=n)[:n]
    tot = np.bincount(di[valid], weights=np.asarray(q)[valid], minlength=n)[:n]
    resets = np.zeros(len(ts), dtype=int)
    resets[0] = 1
    resets[1:] = (~full_ok[1:]) | (~full_ok[:-1]) | (np.diff(ts) != H)
    latest = np.maximum.accumulate(np.where(resets, np.arange(len(ts)), 0))
    runs = np.arange(len(ts)) - latest + 1
    out = []
    d_start = max(30, int(panel_start_ms // DAY) - dmin)
    for d in range(d_start, n):
        dayms = (d + dmin) * DAY
        if dayms < listed_ms + 90 * DAY or dayms >= panel_end_ms:
            continue
        if not np.all(counts[d - 30:d] == 24):
            continue
        med = float(np.median(tot[d - 30:d]))
        if med < 5_000_000:
            continue
        j = int(np.searchsorted(ts, dayms) - 1)
        if j < 0 or ts[j] != dayms - H or runs[j] < 240 or not full_ok[j]:
            continue
        out.append((int(dayms), med, (dayms - listed_ms) / DAY, 240))
    return out


def _mark_days_complete(mark_path, panel_start_ms, panel_end_ms):
    """该币 `mark_1h` 中**每日 24/24 整点 mark 齐全**的 UTC 日集合。缺文件 ⇒ 空集。"""
    if not os.path.exists(mark_path):
        return set()
    t = feather.read_table(mark_path, columns=["date"], memory_map=True)
    a = _dates_ms(t)
    a = a[(a >= panel_start_ms) & (a < panel_end_ms) & (a % H == 0)]
    if a.size == 0:
        return set()
    cnt = np.bincount((a // DAY).astype(np.int64))
    return {int(d) * DAY for d, c in enumerate(cnt) if c >= 24}


# ── 逐日名单构建（三掩码分离）─────────────────────────────────────────────────
def build_daily_maps(*, panel="DEV", do_cross_check=True, verbose=False):
    """构建 375 逐日名单（`rank_keys_by_day` / `admission_by_day`）+ 三掩码 + 证据。

    `panel` ∈ {DEV, VAL, FINAL, overall}；默认 DEV（本窗口只消费开发及以前必要预热）。
    """
    pool = load_pool()
    syms = pool["symbols"]
    src = source_map()
    listed = listing_map()
    if panel == "overall":
        p_start, p_end = CALENDAR["overall"]
    else:
        p_start, p_end = CALENDAR[panel]
    panel_start_ms = _ms(p_start + "T00:00:00Z")
    panel_end_ms = _ms(p_end + "T00:00:00Z") + DAY          # 含末日
    ban_end_ms = panel_start_ms + CALENDAR["segment_head_ban_days"] * DAY

    rank_keys_by_day, admission_by_day = {}, {}
    orig_by_sym, ban_by_sym, trade_by_sym = {}, {}, {}
    evidence = []
    mark_missing_syms = []
    stats = {"panel": panel, "panel_start": p_start, "panel_end": p_end,
             "panel_days": (panel_end_ms - panel_start_ms) // DAY,
             "ban_window": [iso(panel_start_ms), iso(ban_end_ms)],
             "pool_count": len(syms), "orig_qual_symbols": 0, "admitted_symbols": 0,
             "orig_qual_rows": 0, "admitted_rows": 0, "banned_rows": 0,
             "mark_missing_symbols": 0, "non_tradeable_rows": 0}

    for sym in syms:
        s = src[sym]
        tab = feather.read_table(s["price_1h"],
                                 columns=["date", "open", "high", "low", "close", "volume"],
                                 memory_map=True)
        ts = _dates_ms(tab)
        o = tab["open"].to_numpy(); hi = tab["high"].to_numpy()
        lo = tab["low"].to_numpy(); cl = tab["close"].to_numpy()
        vol = tab["volume"].to_numpy()
        q = np.load(s["quote_1h"], mmap_mode="r")
        if len(ts) != len(q):
            raise ValueError(f"{sym}: 量价不对齐 {len(ts)}!={len(q)}")
        qual = _qualify_symbol(ts, o, hi, lo, cl, vol, q, listed[sym],
                               panel_start_ms, panel_end_ms)
        mk_days = _mark_days_complete(s["mark_1h"], panel_start_ms, panel_end_ms)
        if not os.path.exists(s["mark_1h"]):
            mark_missing_syms.append(sym)
        orig_by_sym[sym] = qual
        ban_by_sym[sym] = {}
        trade_by_sym[sym] = {}
        if qual:
            stats["orig_qual_symbols"] += 1
        for (dayms, med, ldays, warm) in qual:
            banned = dayms < ban_end_ms
            tradeable = dayms in mk_days
            ban_by_sym[sym][dayms] = banned
            trade_by_sym[sym][dayms] = tradeable
            stats["orig_qual_rows"] += 1
            if banned:
                stats["banned_rows"] += 1
            if not tradeable:
                stats["non_tradeable_rows"] += 1
            key = f"{EXCHANGE}:{sym}"
            rank_keys_by_day.setdefault(dayms, {})[key] = med
            admitted = (not banned)
            admission_by_day.setdefault(dayms, {})[key] = admitted
            evidence.append({
                "币": sym, "资格日": iso(dayms), "原资格": True, "新禁入": banned,
                "可交易mark": tradeable,
                "准入": admitted,
                "此前30日成交额中位U": med, "上市日数": round(ldays, 4),
                "前240小时连续有效": True, "排序依据": "此前30日成交额中位降序→(exchange,symbol)字典序",
                "源路径_价格": s["price_1h"], "源SHA_价格": s["price_1h_sha256"],
                "源路径_成交额": s["quote_1h"], "源SHA_成交额": s["quote_1h_sha256"],
                "源路径_mark": s["mark_1h"],
                "身份性质": ASSUMPTION, "用途": PURPOSE_LABEL,
            })
        if verbose:
            print(f"  {sym}: orig={len(qual)} mark_days={len(mk_days)}")
    stats["mark_missing_symbols"] = len(mark_missing_syms)
    stats["mark_missing_list"] = sorted(mark_missing_syms)
    stats["admitted_symbols"] = len({e["币"] for e in evidence if e["准入"]})
    stats["admitted_rows"] = sum(1 for e in evidence if e["准入"])
    stats["days_with_admission"] = len(rank_keys_by_day)
    stats["pool_list"] = {"path": pool["path"], "sha256": pool["sha256"], "count": pool["count"]}
    out = {
        "schema": "daily_list_map/v1",
        "purpose": "approximate_375_research",
        "scope": "binance_um_375_approx",
        "exchange": EXCHANGE,
        "calendar": CALENDAR,
        "panel": panel,
        "rank_keys_by_day": rank_keys_by_day,
        "admission_by_day": admission_by_day,
        "masks": {
            "orig_qual": {s: sorted(d for (d, *_ ) in orig_by_sym[s]) for s in syms},
            "ban": {s: sorted(d for d, v in ban_by_sym[s].items() if v) for s in syms},
            "tradeable": {s: sorted(d for d, v in trade_by_sym[s].items() if v) for s in syms},
        },
        "stats": stats,
        "evidence": evidence,
    }
    if do_cross_check:
        out["cross_check_12"] = cross_check_12(out, panel_start_ms, panel_end_ms)
    return out


def cross_check_12(maps, panel_start_ms, panel_end_ms):
    """与 12 CSV（旧段首禁入已过滤）重叠日交叉核对：应逐值一致。

    12 CSV 由**旧日历**（面板 2020-12-07、旧段首 30 日禁入）生成 ⇒ 其缺日包含旧禁入窗口。
    反向核对时须排除旧禁入窗口内的日子，否则会把「旧禁入造成的缺行」误判为差异。
    """
    OLD_EMBARGO = [(2020, 12, 7), (2023, 11, 8), (2025, 1, 8)]
    old_ban = [(_ms(f"{y:04d}-{m:02d}-{d:02d}T00:00:00Z"),
                _ms(f"{y:04d}-{m:02d}-{d:02d}T00:00:00Z") + 30 * DAY)
               for (y, m, d) in OLD_EMBARGO]

    def in_old_ban(x):
        return any(a <= x < b for a, b in old_ban)

    orig = {}
    for e in maps["evidence"]:
        if e["原资格"]:
            orig.setdefault(e["币"], {})[_ms(e["资格日"])] = e["此前30日成交额中位U"]
    rows12 = _read_csv(DAILY12)
    mism_med, missing_in_orig, extra = [], [], []
    matched = 0
    for r in rows12:
        sym = r["币"]; d = _ms(r["资格日"])
        if d < panel_start_ms or d >= panel_end_ms:
            continue
        med12 = float(r["此前30日成交额中位U"])
        got = orig.get(sym, {}).get(d)
        if got is None:
            missing_in_orig.append((sym, iso(d)))
        elif abs(got - med12) > 1e-6:
            mism_med.append((sym, iso(d), med12, got))
        else:
            matched += 1
    d12_min = min(_ms(r["资格日"]) for r in rows12)
    keys12 = {(r["币"], _ms(r["资格日"])) for r in rows12}
    for sym, dd in orig.items():
        for d in dd:
            if d >= d12_min and not in_old_ban(d) and (sym, d) not in keys12:
                extra.append((sym, iso(d)))
    return {
        "rows12": len(rows12), "d12_day_min": iso(d12_min),
        "overlap_matched": matched,
        "median_mismatch_count": len(mism_med), "median_mismatch_sample": mism_med[:20],
        "in_12_but_not_recomputed": len(missing_in_orig),
        "in_12_but_not_recomputed_sample": missing_in_orig[:20],
        "recomputed_but_not_in_12_count": len(extra),
        "recomputed_but_not_in_12_sample": extra[:20],
        "old_ban_windows": [[iso(a), iso(b)] for a, b in old_ban],
        "verdict": ("IDENTICAL_ON_OVERLAP"
                    if not mism_med and not missing_in_orig and not extra
                    else "DIFFERS"),
        "note": "12 CSV 旧段首禁入已过滤；本窗口重算**不含禁入**，重叠日（≥2021-01-06 且非旧禁入）应逐值一致。",
    }


def write_daily_map_doc(maps, out_path):
    """落盘规范日名单文档（`daily_list_map/v1`；供 run_arm/门禁 `daily_list_version` 解析）。"""
    days = sorted(maps["rank_keys_by_day"])
    doc = {
        "schema": "daily_list_map/v1",
        "purpose": maps["purpose"], "scope": maps["scope"], "exchange": maps["exchange"],
        "panel": maps["panel"], "calendar": maps["calendar"],
        "days": [{"day": d,
                  "order": maps["rank_keys_by_day"].get(d, {}),
                  "admission": maps["admission_by_day"].get(d, {})} for d in days],
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"path": str(out_path), "sha256": _sha256_file(out_path), "days": len(days)}


def write_evidence_csv(maps, out_path):
    rows = maps["evidence"]
    if not rows:
        Path(out_path).write_text("币,资格日\n", encoding="utf-8-sig")
        return {"path": str(out_path), "rows": 0}
    cols = list(rows[0].keys())
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader(); w.writerows(rows)
    return {"path": str(out_path), "rows": len(rows)}


def coverage_report():
    """375 池实际输入覆盖（价格 / 成交额 / 15m / mark / funding）+ 3 币本地缺 mark。"""
    src = source_map()
    rep = {"pool": 375, "missing": {}}
    for kind, key in (("price_1h", "price_1h"), ("quote_1h", "quote_1h"),
                      ("price_15m", "price_15m"), ("quote_15m", "quote_15m"),
                      ("mark_1h", "mark_1h"), ("funding_1h", "funding_1h")):
        miss = [s for s, v in src.items() if not os.path.exists(v[key])]
        rep["missing"][kind] = miss
    rep["mark_missing_symbols"] = rep["missing"]["mark_1h"]
    rep["note"] = ("mark_1h 本地缺 = 该币整份 1h mark 不可得 ⇒ 该币可交易掩码 False、"
                   "资金费缺 mark 严格停算（不补 0）；**不称所有官方渠道不可得**，**不缩 375 池**。")
    return rep


# ── symbol_data 组装（复用 12 真实装载口径；供 run_arm）────────────────────────
def _dates_ms(tab):
    """feather date 列 → int64 毫秒 numpy（兼容 int / datetime / 各精度 timestamp）。"""
    col = tab["date"]
    if pa.types.is_timestamp(col.type):
        unit = col.type.unit
        vals = col.cast(pa.int64()).to_numpy()
        if unit == "s":
            return vals * 1000
        if unit == "ms":
            return vals
        if unit == "us":
            return vals // 1000
        if unit == "ns":
            return vals // 1_000_000
    if pa.types.is_integer(col.type):
        return col.cast(pa.int64()).to_numpy()
    return np.array([_to_ms(x) for x in col.to_pylist()], dtype=np.int64)


def _load_1h(base, sym, start_ms, end_ms):
    fp = BN_DIR / f"{base}_USDT_USDT-1h-futures.feather"
    qp = SIDECAR / f"{base}_USDT_USDT-1h-quote_volume.npy"
    tab = feather.read_table(fp, columns=["date", "open", "high", "low", "close", "volume"],
                             memory_map=True)
    ts = _dates_ms(tab)
    q = np.load(qp, mmap_mode="r")
    if len(ts) != len(q):
        raise ValueError(f"{sym}: 1h 量价不对齐")
    sel = (ts >= start_ms) & (ts < end_ms)
    return [(int(ts[i]), float(tab["open"][i]), float(tab["high"][i]), float(tab["low"][i]),
             float(tab["close"][i]), float(tab["volume"][i]), float(q[i]))
            for i in np.flatnonzero(sel)]


def _load_15m(base, sym, start_ms, end_ms):
    fp = BN_DIR / f"{base}_USDT_USDT-15m-futures.feather"
    qp = SIDECAR / f"{base}_USDT_USDT-15m-quote_volume.npy"
    tab = feather.read_table(fp, columns=["date", "open", "high", "low", "close"],
                             memory_map=True)
    ts = _dates_ms(tab)
    q = np.load(qp, mmap_mode="r")
    if len(ts) != len(q):
        raise ValueError(f"{sym}: 15m 量价不对齐")
    sel = (ts >= start_ms - 900_000) & (ts < end_ms)
    return [(int(ts[i]), float(tab["open"][i]), float(tab["high"][i]), float(tab["low"][i]),
             float(tab["close"][i]), float(q[i])) for i in np.flatnonzero(sel)]


def _load_marks(base, start_ms, end_ms):
    mp = BN_DIR / f"{base}_USDT_USDT-1h-mark.feather"
    if not os.path.exists(mp):
        return {}
    tab = feather.read_table(mp, columns=["date", "open"], memory_map=True)
    a = _dates_ms(tab)
    op = tab["open"].to_numpy()
    return {int(a[i]): float(op[i]) for i in np.flatnonzero((a >= start_ms) & (a < end_ms))}


def _to_ms(x):
    """把 feather 的 date（datetime/str/int）统一为毫秒。"""
    if isinstance(x, dt.datetime):
        return int(x.timestamp() * 1000)
    if isinstance(x, str):
        return _ms(x)
    return int(x)


def _load_funding_raw(base):
    fp = BN_DIR / f"{base}_USDT_USDT-1h-funding_rate.feather"
    tab = feather.read_table(fp, memory_map=True)
    d = tab["date"].to_pylist(); r = tab["funding_rate"].to_pylist()
    return sorted((_to_ms(x), float(y)) for x, y in zip(d, r))


def _first_data_ms(raw, marks, win_start_ms):
    """该币在**窗口内**首个可用数据时刻（真实费率首笔与整点 mark 首笔的较早者）；无 ⇒ None。

    用于把费用事件起点**不早于**首个可用数据 ⇒ 不伪造上市前「应结算」缺口；
    窗口内**完全无数据**（空 / 上市前）⇒ 返回 None（调用方不造任何事件）。
    """
    cands = []
    for t, _r in (raw or []):
        if int(t) >= win_start_ms:
            cands.append(int(t))
            break
    if marks:
        m = [int(t) for t in marks if int(t) >= win_start_ms]
        if m:
            cands.append(min(m))
    return min(cands) if cands else None


def build_norm4h_events(raw_all, marks, win_start_ms, win_end_ms):
    """正常 4h：固定 UTC 00/04/08/12/16/20 整点事件；费率 = **严格早于 T** 的最近真实费率。

    复用第12 `_inputs12.build_norm_4h_events` 的固定 4h 整点 / 严格过去 last-known 算法：
      · 事件 T 落在 4h 网格（00/04/08/12/16/20），**不是**把原始 8h 序列改名；
      · rate = `raw_ms < T` 的最近一笔真实费率（**不除8/不除2、不补0、不用未来 T 之后事件**）；
      · **保留来源原始毫秒时刻** `source_ms`（如 `2021-01-14T08:00:00.012Z` ⇒ 12ms 不取整、不冒 T）；
      · 起点不早于该币**首个可用数据时刻**（不伪造上市前应结算）。

    返回 5 元组列表 `(T, rate, mark_T, source_ms, kind)`（升序）：
      · 已知率 ⇒ `kind="norm4h_fixed_last_known"`；
      · T 前无任何已知真实费率 ⇒ `(T, None, mark_T, None, "rate_missing")`（由执行器相邻不利替代）。
    """
    raw = sorted((int(t), float(r)) for t, r in (raw_all or []))
    out = []
    j, n = 0, len(raw)
    start = int(win_start_ms)
    fd = _first_data_ms(raw, marks, start)
    if fd is None:
        return out                       # 窗口内无任何数据（空 / 上市前）⇒ 不造事件
    if fd > start:
        start = fd
    t = (start // FOURH_MS) * FOURH_MS
    if t < start:
        t += FOURH_MS
    while t < win_end_ms:
        while j < n and raw[j][0] < t:          # 推进到最后一个 raw_ms < T（严格早于 T）
            j += 1
        if j == 0:
            out.append((t, None, marks.get(t), None, "rate_missing"))
        else:
            src_ms, rate = raw[j - 1]
            out.append((t, float(rate), marks.get(t), int(src_ms), NORM4H_KIND))
        t += FOURH_MS
    return out


def build_ext1h_events(marks, win_start_ms, win_end_ms, rate=EXTREME_RATE):
    """极端 1h：每个整点事件，幅度 `rate`（默认 0.02）单边不利；起点不早于首个可用数据。

    返回 3 元组列表 `(T, rate, mark_T)`（升序）。
    """
    out = []
    start = int(win_start_ms)
    fd = _first_data_ms(None, marks, start)
    if fd is None:
        return out                       # 窗口内无 mark 数据（空 / 上市前）⇒ 不造事件
    if fd > start:
        start = fd
    t = (start // H) * H
    if t < start:
        t += H
    while t < win_end_ms:
        out.append((t, float(rate), marks.get(t)))
        t += H
    return out


# ── 第40 加性：norm4h_approx 动态折算（因果近似；非成本严格等价）─────────────────────
NORM4H_APPROX_KIND = "norm4h_approx_dynamic_interval"   # 新近似事件 kind
NORM4H_APPROX_MODEL = "norm4h_approx"
APPROX_MAX_DRIFT_MS = 5000                               # 名义结算点漂移上限（>5s 终止该场景）


class FundingIntervalUnknownError(RuntimeError):
    """该时点**历史已知有效结算周期元数据**缺失 ⇒ 严格阻断（不默认 8h、不向前看下一事件）。"""


class FundingDriftExceededError(RuntimeError):
    """名义结算点与可核规则/元数据锚点漂移 > 5s ⇒ 终止该臂该场景（不 round 造合法名义时间）。"""


def _approx_interval_hours(interval_meta, t):
    """在时点 `t` **因果可知**的历史有效结算周期（小时）；缺 ⇒ None（调用方阻断）。

    `interval_meta` 支持两种形态（**只读材料，不由本函数联网/推断**）：
      · list[dict]：`{"effective_from_ms": ms, "interval_hours": h, "available_at_ms": ms,
        "nominal_anchor_ms": ms?, "nominal_settlement_ms": [ms,...]?}` ⇒ 取满足下条**可得性**
        约束中 `effective_from_ms` 最大的记录；
      · dict：`{from_ms: {"effective_from_ms": ms?, "available_at_ms": ms, "interval_hours": h}}`
        ⇒ 取满足**可得性**约束的最大键。**键必须配完整记录（含 `available_at_ms`）**。

    第41b **可得性硬约束（所有输入形态一律适用）**：记录在 `t` 可用，当且仅当
      ① 已生效：`effective_from_ms <= t`；**且**
      ② 当时已公开可得：`available_at_ms` **显式存在且** `<= t`。
    记录**缺 `available_at_ms`** ⇒ 无法证明「当时已公开可得」⇒ 视为 UNKNOWN **不可用**。
    **字典简写 `{生效时刻: h}`（裸数字值）不携带可得时刻 ⇒ 一律不可用**（已删除「生效即可得」
    的推定，不得自动放行）；须改用完整记录形态显式给出 `available_at_ms`。
    **不**默认 8h、**不**用当前静态周期反推历史、**不**用未来生效/未来可得记录。
    返回 `(interval_hours|None, nominal_anchor_ms|None, nominal_settlement_ms|None)`。
    """
    if not interval_meta:
        return None, None, None

    def _usable(frm, avail):
        """记录在 `t` 是否**已生效且当时已可得**（缺可得时刻 ⇒ 不可用）。"""
        if frm is None or int(frm) > int(t):
            return False
        if avail is None or int(avail) > int(t):
            return False
        return True

    if isinstance(interval_meta, dict):
        cand = []
        for k, v in interval_meta.items():
            kk = int(k)
            if isinstance(v, dict):
                frm = v.get("effective_from_ms", kk)
                avail = v.get("available_at_ms")
            else:
                # 第41b：裸数字简写 `{生效时刻: h}` **不含可得时刻** ⇒ 不得推定「生效即可得」，
                #        视为 UNKNOWN 不可用（不自动放行）；须用完整记录形态显式给 available_at_ms。
                frm, avail = kk, None
            if _usable(frm, avail):
                cand.append((kk, v))
        if not cand:
            return None, None, None
        k, v = max(cand, key=lambda kv: kv[0])
        if not isinstance(v, dict):
            return float(v), None, None
        h = v.get("interval_hours")
        anchor = v.get("nominal_anchor_ms")
        nom = v.get("nominal_settlement_ms")
        return (None if h is None else float(h),
                (int(anchor) if anchor is not None else None),
                ([int(x) for x in nom] if nom else None))
    best = None
    for rec in interval_meta:
        frm = rec.get("effective_from_ms")
        avail = rec.get("available_at_ms")
        if not _usable(frm, avail):
            continue
        if best is None or int(frm) > int(best.get("effective_from_ms")):
            best = rec
    if best is None:
        return None, None, None
    h = best.get("interval_hours")
    anchor = best.get("nominal_anchor_ms")
    nom = best.get("nominal_settlement_ms")
    return (None if h is None else float(h),
            (int(anchor) if anchor is not None else None),
            ([int(x) for x in nom] if nom else None))


def _approx_nominal_drift(t, h, anchor, nominal_list, max_drift_ms):
    """名义结算点漂移（ms）：**只用可核规则/元数据**，不 round 造合法名义时间。

    · 元数据给出显式 `nominal_settlement_ms` ⇒ 取最近名义点求漂移；
    · 否则用锚点 + 周期点阵；**仅当周期整除 4h**（1/2/4h）时 4h 网格点本身即结算点，
      可核；否则（如 8h）4h 网格是模型自身名义点，无元数据整点可核 ⇒ 漂移记 0（并披露）。
    """
    if nominal_list:
        nearest = min(nominal_list, key=lambda x: abs(int(x) - int(t)))
        return abs(int(t) - int(nearest))
    if anchor is None or not h or h <= 0:
        return 0
    ratio = 4.0 / float(h)
    if abs(ratio - round(ratio)) > 1e-9:
        return 0
    h_ms = int(round(float(h) * H))
    if h_ms <= 0:
        return 0
    k = round((int(t) - int(anchor)) / h_ms)
    return abs(int(t) - (int(anchor) + k * h_ms))


def build_norm4h_approx_events(raw_all, marks, win_start_ms, win_end_ms, interval_meta,
                               *, max_drift_ms=APPROX_MAX_DRIFT_MS):
    """`norm4h_approx`：4h 整点事件，费率 = `(4.0 / 当时已知有效 interval_hours) * 最近已知有效 rate`。

    **因果近似**（不是成本严格等价）：只用 `raw_ms < T` 的最近一笔**真实**费率，**不除8/不补0/
    不用未来事件倒填**；折算系数只用**该时点因果可知**的历史有效结算周期元数据。

    严格阻断（**不默认、不取整、不下载**）：
      · 该时点缺有效周期元数据（**已生效 ∧ 当时已公开可得**；缺 `available_at_ms` 亦视为不可用）
        ⇒ `FundingIntervalUnknownError`（结束该臂该近似场景）；
      · 名义结算点相对元数据锚点/显式名义点漂移 > `max_drift_ms` ⇒ `FundingDriftExceededError`。

    返回 5 元组列表 `(T, rate, mark_T, source_ms, kind)`（升序；`kind=NORM4H_APPROX_KIND`）；
    `T` 前无任何已知真实费率 ⇒ `(T, None, mark_T, None, "rate_missing")`（由执行器相邻不利替代）。
    """
    raw = sorted((int(t), float(r)) for t, r in (raw_all or []))
    out = []
    j, n = 0, len(raw)
    start = int(win_start_ms)
    fd = _first_data_ms(raw, marks, start)
    if fd is None:
        return out
    if fd > start:
        start = fd
    t = (start // FOURH_MS) * FOURH_MS
    if t < start:
        t += FOURH_MS
    while t < win_end_ms:
        h, anchor, nom = _approx_interval_hours(interval_meta, t)
        if h is None or h <= 0:
            raise FundingIntervalUnknownError(
                "norm4h_approx：时点 %d 缺历史已知有效结算周期元数据（不默认8h/不取下一事件）" % t)
        drift = _approx_nominal_drift(t, h, anchor, nom, max_drift_ms)
        if drift > int(max_drift_ms):
            raise FundingDriftExceededError(
                "norm4h_approx：名义结算点 %d 相对元数据漂移 %dms > %dms"
                % (t, drift, int(max_drift_ms)))
        while j < n and raw[j][0] < t:
            j += 1
        if j == 0:
            out.append((t, None, marks.get(t), None, "rate_missing"))
        else:
            src_ms, rate = raw[j - 1]
            scaled = (4.0 / float(h)) * float(rate)
            out.append((t, float(scaled), marks.get(t), int(src_ms), NORM4H_APPROX_KIND))
        t += FOURH_MS
    return out


def build_symbol_data(symbols, start_ms, end_ms, *, model="norm4h", interval_meta=None):
    """为给定币集组装 `run_arm` 的 `symbol_data`（价量/成交额/全小时 mark/资金费/费率序列）。

    `model` ∈ {norm4h, norm4h_approx, ext1h}：
      · `norm4h`：**真构造固定 4h 整点**事件（00/04/08/12/16/20）+ 严格过去 last-known 费率，
        保留来源原始毫秒时刻；缺率边界记 5 元组 `rate_missing`（不补 0）。**原版保留，供旧基线重核**。
      · `norm4h_approx`（第40 加性）：同 4h 整点，费率 = `(4.0/当时已知有效interval_hours)*最近已知
        有效rate`；`interval_meta` 缺有效周期 ⇒ `FundingIntervalUnknownError` 终止该臂该场景
        （**不默认8h/不取下一事件/不下载**）；名义点漂移 >5s ⇒ `FundingDriftExceededError`。
      · `ext1h`：每整点 3 元组 `(T, 0.02, mark_T)`。**原版保留**。
    统一 2/3/4/5 元素口径（见 `compute_data_version`），不改原件、不下载、不写生产库。
    """
    src = source_map()
    out = {}
    for sym in symbols:
        s = src[sym]; base = s["base"]
        d15 = _load_15m(base, sym, start_ms, end_ms)
        marks = _load_marks(base, start_ms, end_ms)
        raw = _load_funding_raw(base)
        series = [(int(t), float(r)) for (t, r) in raw]
        if model == "norm4h":
            funding = build_norm4h_events(raw, marks, start_ms, end_ms)
        elif model == "norm4h_approx":
            # 第40 加性：动态折算（严格因果；缺周期元数据/漂移超限 ⇒ 抛错终止该臂该场景）
            funding = build_norm4h_approx_events(raw, marks, start_ms, end_ms, interval_meta)
        elif model == "ext1h":
            funding = build_ext1h_events(marks, start_ms, end_ms)
        else:
            raise ValueError(f"未知费用模型: {model!r}")
        contract = _contract_for(sym)
        out[(EXCHANGE, sym)] = {
            "detail": d15, "marks": marks, "funding": funding,
            "funding_rate_series": series,
            "funding_available": bool(funding),
            "events": [], "donchian_exits": [], "contract": contract,
        }
    return out


def _contract_for(sym):
    """从 07 保守执行近似规格读当前合约精度（价格步进/数量步进/最小量/最大量/最小金额）。

    历史变化未证明（07 `历史变化已证明=False`）⇒ 固定当前规格作**近似**，用途
    「近似回测，不是精确实盘还原」。
    """
    from .wx_execution import Contract
    for r in _read_csv(SPEC_ROUNDING):
        if r["币"] == sym:
            return Contract(
                quantity_step=float(r["数量步进"]),
                min_notional=float(r["最小订单金额"]),
                price_step=float(r["价格步进"]),
                min_quantity=float(r["最小数量"]),
                max_quantity=float(r["最大数量"]),
                market_quantity_step=float(r["市价数量步进"]),
                market_min_quantity=float(r["市价最小数量"]),
                market_max_quantity=float(r["市价最大数量"]),
            )
    return None


# ── 日名单文档加载（供 run_arm 消费；键 (exchange, symbol)）─────────────────────
def daily_doc_path(panel="DEV"):
    return Path(__file__).resolve().parent.parent.parent / "最新唐奇安威科夫接通与收尾_2026-10-11" \
        / "375逐日名单与实际输入接通27" / f"02_375逐日名单_{panel}.json"


def load_daily_maps(doc_path=None, panel="DEV"):
    """加载规范日名单文档 → (rank_keys_by_day, admission_by_day, meta)。

    键还原为 `(exchange, symbol)` 元组（run_arm 口径）；`day` 为 UTC00 ms。
    """
    p = Path(doc_path) if doc_path else daily_doc_path(panel)
    with open(p, encoding="utf-8") as f:
        doc = json.load(f)
    rk, adm = {}, {}
    for d in doc["days"]:
        day = int(d["day"])
        rk[day] = {tuple(k.split(":", 1)): float(v) for k, v in (d.get("order") or {}).items()}
        adm[day] = {tuple(k.split(":", 1)): bool(v) for k, v in (d.get("admission") or {}).items()}
    return rk, adm, {"path": str(p), "sha256": _sha256_file(p), "days": len(doc["days"]),
                     "schema": doc.get("schema")}


def approx_spec_375():
    """当前保守执行近似规格（07/08/20；用途固定「近似回测，不是精确实盘还原」）。"""
    return {
        "purpose": PURPOSE_LABEL,
        "rounding": "买价先滑点后按价格步进向上取整；卖价先滑点后向下取整；保护价向下取整后重核风险",
        "quantity": "按合约报价数量单位；q=步进×floor(风险/资金/参与率交集上限/步进)；不向上加仓凑最小单",
        "reject": "低于最小可下量/超最大量/取整后违反资金/组合风险/参与上限 ⇒ 整单拒绝并逐条留因",
        "gap": "价格/成交额/必要标价缺 ⇒ 禁开新仓；持仓缺必需输入严格停算（不补0）",
        "funding": "正常4h(每4h整点,last-known真实费率)/极端1h·2%(固定幅度) 互斥分别登记；缺率取相邻不利",
        "spec_source": str(SPEC_ROUNDING), "model_doc": str(MODEL_DOC), "fee_rule": str(FEE_RULE),
        "spec_source_sha256": _sha256_file(SPEC_ROUNDING) if SPEC_ROUNDING.exists() else None,
        "model_doc_sha256": _sha256_file(MODEL_DOC) if MODEL_DOC.exists() else None,
        "fee_rule_sha256": _sha256_file(FEE_RULE) if FEE_RULE.exists() else None,
        "approximate_not_exact": True,
    }


def normalize_funding_events(raw):
    """把 funding 事件**统一为 5 元组** `(ft, rate, mark, avail, kind)`（rate/mark/avail/kind 可 None）。

    复用执行器 `wx_execution._normalize_funding_events` 的**同一口径**（单一事实源），
    使「数据版本」与「实际消费」规范化内容一致；支持 2/3/4/5 元素，**不跳过**任何行。
    """
    from .wx_execution import _normalize_funding_events
    return list(_normalize_funding_events(raw or []))


def compute_data_version(symbol_data, model):
    """本次**实际消费输入**的 canonical 摘要（逐 (所,币) 价量/成交额/费率/消费 mark）。

    **统一 2/3/4/5 元素**资金费元组（复用执行器同口径归一化），逐行写入
    `[ft, rate|None, mark|None, avail|None, kind|None]` ⇒ **缺率/None/缺标签不被忽略**（不逃 hash）。
    另含 ①邻接来源序列 `funding_rate_series` ②实际消费整点 `marks`。
    ⇒ 模型名 / 事件时间 / 费率 / mark / 缺标签 / 来源原始时刻 / 邻接来源 / 消费 mark 任一变化
    都改变版本（`dv:<64hex>`）。
    """
    h = hashlib.sha256()
    h.update(str(model).encode())
    for key in sorted(symbol_data):
        sd = symbol_data[key]
        h.update(json.dumps([key[0], key[1]]).encode())
        h.update(json.dumps(sd.get("detail") or []).encode())
        ev = [[ft, rate, mark, avail, kind]
              for (ft, rate, mark, avail, kind) in normalize_funding_events(sd.get("funding") or [])]
        h.update(json.dumps(ev).encode())
        h.update(json.dumps([[int(t), float(r)]
                             for t, r in (sd.get("funding_rate_series") or [])]).encode())
        h.update(json.dumps(sorted((sd.get("marks") or {}).items())).encode())
    return "dv:" + h.hexdigest()


def build_run_binding(daily_meta, data_version, model, *, scope="binance_um_375_approx",
                      symbol_data=None):
    """组装 run_arm 六维绑定（scope/daily_list_version/input_snapshot/code_sha256/
    strategy_config/funding_model）。**声明**，实核由门禁负责。

    第29 加性：给出 `symbol_data` 时，`funding_model` 由**本次实际资金费材料**
    （`wx_execution.actual_funding_materials` → `funding_materials_identity`）派生 ⇒ 声明
    与门禁重算的**实际材料身份**同源，不再用空事件占位。未给 `symbol_data` ⇒ 退回空事件占位
    （**明确「无实际材料」，不得冒已核**）。
    """
    from . import wx_execution as X
    code, _per = X.compute_code_bundle_sha()
    cfg = X.compute_strategy_config_identity()
    if symbol_data:
        mats = X.actual_funding_materials(symbol_data)
        fm = X.funding_materials_identity(model, mats)
    else:
        fm = X.funding_model_identity(model, [], None)
    from . import wx_gate as G
    formal = G._formal_data_binding() or {}
    return {
        "scope": scope,
        "daily_list_version": daily_meta["sha256"],
        "input_snapshot": formal.get("snapshot_sha256") or data_version,
        "code_sha256": code,
        "strategy_config": cfg.get("value"),
        "funding_model": fm,
    }


# ══════════════════════════════════════════════════════════════════════════
# 第34：**完整输入版本**与**实际使用凭证**分开登记（人类 134 批准口径；加性，不改 29/30 口径）
# ══════════════════════════════════════════════════════════════════════════
# 背景（133/133a）：32 账本「完整输入 42 标价时点」≠「实际使用 22 标价时点」只说明**子集关系**，
# **不**说明金额错。旧口径用账本使用子集**覆盖**全事件 marks 再与执行前全量声明比对 ⇒ 恒 False。
# 本窗口**只接线**：完整输入版本身份**稳定**（执行前后同一 `symbol_data` 不变），实际使用凭证
# 从**同一真实账本**派生并**指向**完整输入版本；两者**分列**，**不**强行同码比较。
# 复用 33 的 `wx_execution`（`complete_funding_materials` / `funding_materials_identity` /
# `funding_usage_voucher` / `audit_funding_usage`）——**不另造引擎、不造第二执行器**。
COMPLETE_INPUT_VERSION_SCHEMA = "complete_input_version/v1"


def complete_input_version(symbol_data, model):
    """**完整输入版本/费用模型材料身份**（稳定；**不**被事后持仓使用子集覆盖）。

    返回 `{"schema":"complete_input_version/v1", "model", "identity":"fundmodel:<model>:<32hex>",
    "materials_schema", "n_sources", "n_events", "n_marks"}`。

    · `identity` = 本次 `symbol_data` 的**规范化完整**费用事件（含缺率边界/来源原始毫秒/真实 mark 或缺）
      + 模型名 + 规则文本，逐 (所,币) 聚合的 v2 身份（复用 33 `funding_materials_identity`）；
    · 执行前后同一 `symbol_data` ⇒ 身份**不变**；篡改 时点/率/mark/模型/来源毫秒/缺标签 ⇒ 身份变化；
    · **本函数不含任何账本上下文** ⇒ 与「本次实际使用了哪些标价」**无关**（子集不改变本身份）。
    """
    from . import wx_execution as X
    mats = X.complete_funding_materials(symbol_data, model)
    n_ev = sum(len(s.get("events") or []) for s in (mats.get("sources") or []))
    n_mk = sum(int(s.get("n_marks") or 0) for s in (mats.get("sources") or []))
    return {
        "schema": COMPLETE_INPUT_VERSION_SCHEMA,
        "model": model,
        "identity": X.funding_materials_identity(model, mats),
        "materials_schema": mats.get("schema"),
        "n_sources": mats.get("n_sources"),
        "n_events": n_ev,
        "n_marks": n_mk,
        "sources": ["%s:%s" % (s.get("exchange"), s.get("symbol"))
                    for s in (mats.get("sources") or [])],
        "stable_under_usage_subset": True,
    }


def usage_credential(con, run_id, *, model=None, complete_identity=None):
    """**实际使用凭证**（从**同一真实账本** run/position/order/funding 派生；**指向**完整输入版本）。

    薄包装 33 `wx_execution.funding_usage_voucher`（**不**复制执行器）。无 `research_run` 行 /
    无账本上下文 / 调用方虚构字典 ⇒ `available=False`（**不构成**凭证）。
    `voucher_id` 因持仓集合变化而不同 ⇒ **不**与 `complete_identity` 强行比较，仅**引用**之。
    """
    from . import wx_execution as X
    return X.funding_usage_voucher(con, run_id, complete_identity=complete_identity, model=model)


def funding_usage_audit(complete_materials, **kwargs):
    """**逐笔双向审查**（正向逐行 + 反向应收付集合）——薄包装 33 `wx_execution.audit_funding_usage`。

    透传 33 的全部关键字参数（`con`/`run_id`/`ledger_rows`/`position_rows`/`order_rows`/
    `freeze_ts`/`model`/`tol_abs`/`complete_identity`），**不**复制执行器、**不**改写判据。
    缺整笔应扣 / 多扣 / 重复 / 出持仓外 / 错币或 run / 另一账本 ⇒ `ok=False` 且列出具体 `problems`
    （**真实 FAIL**，**不**是 `counted=False` 或默认 False）。空账本但有实际持仓与应扣事件 ⇒ 不 PASS。
    """
    from . import wx_execution as X
    return X.audit_funding_usage(complete_materials, **kwargs)


def complete_input_and_usage(symbol_data, model, *, con=None, run_id=None, freeze_ts=None):
    """一次给出**完整输入版本**与**实际使用凭证**（**分列**；两者**不**强行同码）。

    返回 `{"complete_input": {...}, "usage_credential": {...}, "usage_audit": {...}|None}`。
    仅当给出 `con`/`run_id` 时才派生凭证与逐笔审查（无账本 ⇒ `usage_credential.available=False`）。
    """
    ci = complete_input_version(symbol_data, model)
    voucher, audit = None, None
    if con is not None and run_id is not None:
        voucher = usage_credential(con, run_id, model=model, complete_identity=ci["identity"])
        mats = None
        from . import wx_execution as X
        mats = X.complete_funding_materials(symbol_data, model)
        audit = funding_usage_audit(mats, con=con, run_id=run_id, freeze_ts=freeze_ts,
                                    model=model, complete_identity=ci["identity"])
    return {"complete_input": ci, "usage_credential": voucher, "usage_audit": audit}


def run_arm_375(*, con, run_id, arm, config_id, resolved, view, symbols, start_ms, end_ms,
                model="norm4h", ft_engine=None, panel="DEV", doc_path=None, verbose=False):
    """375 逐日准入/排名**真正进 run_arm** 的实际入口（供 28 复用）。

    组装：真实 `symbol_data`（价量/成交额/全小时 mark/资金费）+ 日名单映射（`rank_keys_by_day`/
    `admission_by_day`）+ 六维 run 绑定 + 保守近似规格，随后调用 `wx_execution.run_arm`
    （`require_run_binding=True`）。**不跑整段收益**——由调用方（28 获通过后）决定窗口与臂。
    """
    from . import wx_execution as X
    rk, adm, meta = load_daily_maps(doc_path=doc_path, panel=panel)
    sd = build_symbol_data(symbols, start_ms, end_ms, model=model)
    dv = compute_data_version(sd, model)
    binding = build_run_binding(meta, dv, model, symbol_data=sd)
    if verbose:
        print("  daily_doc", meta["path"], meta["sha256"][:16], "days", meta["days"])
        print("  binding", json.dumps(binding, ensure_ascii=False)[:200])
    return X.run_arm(
        con=con, run_id=run_id, arm=arm, config_id=config_id, resolved=resolved, view=view,
        symbol_data=sd, rank_keys_by_day=rk, admission_by_day=adm, data_version=dv,
        run_binding=binding, require_run_binding=True, approx_spec=approx_spec_375(),
        ft_engine=ft_engine)

