# -*- coding: utf-8 -*-
"""资金费周期元数据 · **消费侧适配层**（独立只读伴生库 → `wx_daily_375.interval_meta` 契约）。

本模块做什么
------------
把**独立只读伴生库** `funding_interval_meta.sqlite3`（401 MB，始终 `mode=ro`）里的
「历史有效结算周期」喂给既有消费契约 —— `strategies/wx_daily_375.py` 的
`_approx_interval_hours(interval_meta, t)` / `build_norm4h_approx_events(...)`。

本模块**不做什么**（红线）
--------------------------
1. **不修改** `strategies/wx_daily_375.py`（sha `45e46301554ead88…`）与
   `strategies/wx_execution.py`（sha `0ede9ce19c7cec9d…`）—— 二者 sha 与已注册的
   run 身份一致，改动即作废身份。本模块是**新增的、非冻结的适配层**，不触碰它们。
2. **不联网、不下载、不写任何库**（伴生库只以 `mode=ro` 打开）。
3. **不默认 8h、不补 0、不推断**：拿不到就让它显式失败（见下）。

消费契约（`wx_daily_375._approx_interval_hours` 的入参形态）
-----------------------------------------------------------
`list[dict]`，每条：
    {"effective_from_ms": ms, "interval_hours": h, "available_at_ms": ms,
     "nominal_anchor_ms": ms?}
该函数第 41b 硬约束：记录在 t 可用 ⟺ `effective_from_ms <= t` **且** `available_at_ms`
显式存在且 `<= t`；**缺 `available_at_ms` ⇒ 视为 UNKNOWN 不可用**。

因果口径 = **HUMAN_RATIFIED_SETTLEMENT_CAUSAL_V1**（人类 2026-10-13 批准）
--------------------------------------------------------------------------
`effective_from_ms = available_at_ms = calc_time_ms`（资金费**结算时刻**）。
领域依据：币安 fapi 的结算事件与「当前生效周期」在 `calc_time` 即为市场公开的确定性
事实；归档 ZIP 的落盘时间只代表离线冷备媒介生成时间。详见 `_fim_query.py` 模块 docstring。

覆盖期之外**绝不外推**（规则 2 / 3 落地）
-----------------------------------------
`_approx_interval_hours` 取「`effective_from_ms <= t` 的最大记录」⇒ 天然会**无限期沿用**
最后一条。为防「归档止于 2026-09-30、查询落在 2026-10 却盲目跨月外推」，本适配层在覆盖
末端注入一条**哨兵记录**：

    {"effective_from_ms": S, "available_at_ms": S, "interval_hours": None, ...}
    S = last_available_ms + (max_staleness_ms or 0) + 1

`t >= S` 时哨兵成为最大可用记录 ⇒ `interval_hours=None` ⇒ `_approx_interval_hours` 返回
None ⇒ `build_norm4h_approx_events` 抛 `FundingIntervalUnknownError`。**这是显式失败，
不是静默按 8h 计费**。哨兵只在 `diag` 里如实登记（`sentinel_at_ms`），不虚构任何数值。

使用
----
    import funding_interval_meta_source as SRC
    records, diag = SRC.load_interval_meta("BTCUSDT", start_ms, end_ms)
    # diag["status"] ∈ {"OK","TAIL_BEYOND_COVERAGE","NO_COVERAGE"}
    funding = W375.build_norm4h_approx_events(raw, marks, start_ms, end_ms, records)
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# 交付目录（伴生库与查询 API 的所在地）。生产代码绑定交付件路径的先例见
# `strategies/wx_gate.DATA_MANIFEST`（绑定「正式回测输入补齐与终验8/07_正式研究manifest.json」）。
SITE_ROOT = Path(__file__).resolve().parents[2]          # outputs/网站逆向研究
DEFAULT_DELIVERY_DIR = SITE_ROOT / "周期元数据表修复与5m冷备_2026-10-13"

# 人类 2026-10-13 正式批准的因果口径
RATIFIED_RULE = "HUMAN_RATIFIED_SETTLEMENT_CAUSAL_V1"
RATIFIED_STATUS = "RATIFIED_BY_HUMAN_2026_10_13"

# 消费侧显式「不可判定」标记
UNVERIFIABLE_NO_INTERVAL_META = "UNVERIFIABLE_NO_INTERVAL_META"

SOURCE_KIND = "funding_interval_meta_sidecar"
SENTINEL_KIND = "coverage_end_sentinel"

_STATUS_OK = "OK"
_STATUS_TAIL = "TAIL_BEYOND_COVERAGE"
_STATUS_NONE = "NO_COVERAGE"

_FIM = None


def delivery_dir():
    return Path(os.environ.get("FUNDING_INTERVAL_META_DIR") or DEFAULT_DELIVERY_DIR)


def sidecar_path():
    return Path(os.environ.get("FUNDING_INTERVAL_META_DB")
                or (delivery_dir() / "周期元数据表" / "funding_interval_meta.sqlite3"))


def query_module():
    """按文件路径加载查询 API（**唯一事实源**，不复制一份查询逻辑）。"""
    global _FIM
    if _FIM is not None:
        return _FIM
    path = delivery_dir() / "_fim_query.py"
    spec = importlib.util.spec_from_file_location("_fim_query_sidecar", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载周期元数据查询 API：{path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fim_query_sidecar"] = mod
    spec.loader.exec_module(mod)
    _FIM = mod
    return _FIM


def open_sidecar(db=None):
    """以 **只读** 打开伴生库（`mode=ro` + `PRAGMA query_only=1`）。"""
    return query_module().connect(db or sidecar_path(), readonly=True)


def coverage(symbol, *, basis="settlement", db=None):
    return query_module().coverage(symbol, basis=basis, db=db or sidecar_path())


def _record(calc_ms, hours, avail_ms):
    rec = {"effective_from_ms": int(calc_ms), "interval_hours": hours,
           "available_at_ms": int(avail_ms), "nominal_anchor_ms": int(calc_ms),
           "source_kind": SOURCE_KIND, "ratified_rule": RATIFIED_RULE}
    return rec


def _sentinel(at_ms):
    return {"effective_from_ms": int(at_ms), "interval_hours": None,
            "available_at_ms": int(at_ms), "nominal_anchor_ms": None,
            "source_kind": SENTINEL_KIND}


def load_interval_meta(symbol, start_ms, end_ms, *, basis="settlement",
                       max_staleness_ms=None, stop_extrapolation=True, db=None):
    """产出 `wx_daily_375` 可直接消费的 `interval_meta`（只读材料）。

    返回 `(records, diag)`：

    · `records`：`list[dict]`，按 `effective_from_ms` 升序；末位可能含一条**哨兵**
      （`interval_hours=None`，`source_kind=coverage_end_sentinel`）用于**阻断覆盖期外外推**。
    · `diag`：
        `status`             OK / TAIL_BEYOND_COVERAGE / NO_COVERAGE
        `coverage`           {first_available_ms, last_available_ms, n}
        `tail_beyond_ms`     请求窗口末端超出覆盖期的毫秒数（0 表示未超）
        `sentinel_at_ms`     哨兵注入点（未注入为 None）
        `max_staleness_ms`   生效的陈旧度上限（None 表示「不额外放宽」）
        `unverifiable_marker` 需要时 = `UNVERIFIABLE_NO_INTERVAL_META`
        `reason`             人可读原因（区分「归档未覆盖」与「费率本身错误」）
    """
    fim = query_module()
    con = fim.connect(db or sidecar_path(), readonly=True)
    try:
        cov = fim.coverage(symbol, basis=basis, con=con)
        rows = con.execute(
            f"SELECT calc_time_ms, funding_interval_hours, "
            f"{'avail_settlement_ms' if basis == 'settlement' else 'avail_archive_publish_ms'} "
            f"FROM funding_interval_meta WHERE exchange=? AND market=? AND symbol=? "
            f"ORDER BY calc_time_ms ASC",
            (fim.EXCHANGE, fim.MARKET, symbol)).fetchall()
    finally:
        con.close()

    diag = {"symbol": symbol, "basis": basis, "status": _STATUS_OK, "coverage": cov,
            "tail_beyond_ms": 0, "sentinel_at_ms": None,
            "max_staleness_ms": (None if max_staleness_ms is None else int(max_staleness_ms)),
            "unverifiable_marker": None, "reason": None,
            "ratified_rule": RATIFIED_RULE, "ratified_status": RATIFIED_STATUS,
            "sidecar": str(db or sidecar_path()), "n_records": 0}

    if not rows:
        diag["status"] = _STATUS_NONE
        diag["unverifiable_marker"] = UNVERIFIABLE_NO_INTERVAL_META
        diag["reason"] = "该币在伴生库中无任何周期元数据记录（覆盖期为零）"
        return [], diag

    records = [_record(calc, h, avail) for calc, h, avail in rows]

    last_avail = max(int(r["available_at_ms"]) for r in records)
    if int(end_ms) > last_avail:
        diag["status"] = _STATUS_TAIL
        diag["tail_beyond_ms"] = int(end_ms) - last_avail
        diag["unverifiable_marker"] = UNVERIFIABLE_NO_INTERVAL_META
        diag["reason"] = ("请求窗口末端超出归档周期元数据覆盖期（归档止于 %d）⇒ "
                          "该时段周期不可判定，**不得静默按 8h 计费**，亦非费率本身错误"
                          % last_avail)
    if stop_extrapolation:
        at = last_avail + (0 if max_staleness_ms is None else int(max_staleness_ms)) + 1
        records.append(_sentinel(at))
        diag["sentinel_at_ms"] = at

    diag["n_records"] = len(records)
    return records, diag


def load_for_symbols(symbols, start_ms, end_ms, **kw):
    """批量版：返回 `{symbol: (records, diag)}`（只读；缺失币同样给出 diag）。"""
    out = {}
    for s in symbols:
        out[s] = load_interval_meta(s, start_ms, end_ms, **kw)
    return out


def assert_covered_or_mark(diag, *, allow_tail=False):
    """消费侧入口的显式检查：覆盖不完整时返回标记（或抛错），**绝不静默继续**。

    `allow_tail=False`（默认）⇒ 只要窗口末端超出覆盖期就抛 `UnverifiableIntervalMetaError`。
    `allow_tail=True` ⇒ 返回 `UNVERIFIABLE_NO_INTERVAL_META` 供调用方显式登记。
    """
    if diag.get("status") == _STATUS_OK:
        return None
    msg = (f"[{UNVERIFIABLE_NO_INTERVAL_META}] {diag.get('symbol')} "
           f"status={diag.get('status')} tail_beyond_ms={diag.get('tail_beyond_ms')} "
           f"reason={diag.get('reason')}")
    if allow_tail:
        return UNVERIFIABLE_NO_INTERVAL_META
    raise UnverifiableIntervalMetaError(msg)


class UnverifiableIntervalMetaError(RuntimeError):
    """周期元数据不可判定（覆盖期外 / 无记录）⇒ 显式失败，不静默按 8h 计费。"""
