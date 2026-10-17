# -*- coding: utf-8 -*-
"""PRECISION-01 · 历史规格适用性判定（verified 放行逻辑）回归测试。

这一组测试盯的是**「能不能把未知历史规格升级成已验证」**这条红线，
不是「功能跑通」。覆盖任务书要求的全部场景：

  1. 单快照；
  2. 前后快照不同；
  3. 两端相同但中间存在变化（含「改过又改回」）；
  4. 窗口内规格缺口（无窗口前证据 / 中间缺该合约 / 中间规格不可读）；
  5. 快照顺序扰动（文件名顺序、枚举顺序）；
  6. 获取时间与生效时间不同（四类时间不得互相代替）。

另外覆盖数量侧要求：
  7. 订单类型对应的数量过滤器（MARKET 才用 MARKET_LOT_SIZE）；
  8. 多条步长同时适用 ⇒ 取最小公倍数而不是较大值；
  9. `check_quantity` 逐条复核，不靠「取最严一条」蒙混；
 10. 约束不相容时必须显式失败，不得静默放过。

以及「不许改调用接口」与「真实证据目录不得被凑成 verified」两条护栏。
"""
import datetime
import hashlib
import inspect
import json
import os
import sys
from pathlib import Path

import pytest

base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
sys.path.insert(0, os.path.join(base_dir, "策略"))

from WyckoffMaster.event_trend_v0_1.instrument_spec import (  # noqa: E402
    APPLICABILITY_MISSING,
    APPLICABILITY_UNVERIFIED_HISTORICAL,
    APPLICABILITY_VERIFIED,
    COMPARED_SPEC_FIELDS,
    InstrumentSpec,
    SpecBook,
    decimal_lcm,
    resolve_instrument_spec,
)

# 与六笔样本 run 完全相同的窗口（inputs.json → data_summary.time_range_1h）
WS = 1704067200000          # 2024-01-01T00:00:00Z
WE = 1719705600000          # 2024-06-30T00:00:00Z

T_BEFORE = 1701388800000    # 2023-12-01
T_MID1 = 1709251200000      # 2024-03-01
T_MID2 = 1714521600000      # 2024-05-01
T_AFTER = 1722470400000     # 2024-08-01

SYM = "ACEUSDT"
ANNOUNCE = "https://www.binance.com/zh-CN/support/announcement/detail/test-prc01"


# ══════════════════════════ 夹具 ══════════════════════════

def _iso(ms: int) -> str:
    dt = datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H%M%SZ")


def _sym_doc(symbol: str = SYM, *, tick: str = "0.000010", lot_step: str = "0.01",
             lot_min: str = "0.01", lot_max: str = "2000000",
             mkt_step=None, mkt_min=None, mkt_max=None,
             notional: str = "5", onboard_ms=None,
             drop_filters=()) -> dict:
    filters = [
        {"filterType": "PRICE_FILTER", "tickSize": tick,
         "minPrice": "0.000010", "maxPrice": "2000"},
        {"filterType": "LOT_SIZE", "stepSize": lot_step,
         "minQty": lot_min, "maxQty": lot_max},
    ]
    if mkt_step is not None or mkt_max is not None or mkt_min is not None:
        filters.append({"filterType": "MARKET_LOT_SIZE",
                        "stepSize": mkt_step or lot_step,
                        "minQty": mkt_min or lot_min,
                        "maxQty": mkt_max or lot_max})
    if notional is not None:
        filters.append({"filterType": "MIN_NOTIONAL", "notional": notional})
    filters = [f for f in filters if f["filterType"] not in drop_filters]
    doc = {"symbol": symbol, "pricePrecision": 6, "quantityPrecision": 2,
           "filters": filters}
    if onboard_ms is not None:
        doc["onboardDate"] = str(onboard_ms)
    return doc


def _write_snapshot(d: Path, *, tag: str, server_ms, symbols,
                    request_ms=None, saved_tag=None) -> Path:
    """写一份 exchangeInfo 快照（`serverTime` 是唯一证据时点）。

    `tag` 只用于文件名；`saved_tag` 可单独指定「文件保存时间」，
    用于构造「文件名时间 ≠ 交易所响应时间」的场景。
    """
    p = d / f"exchangeInfo_futures_{tag}.json"
    doc = {"symbols": symbols}
    if server_ms is not None:
        doc["serverTime"] = server_ms
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    if request_ms is not None or saved_tag is not None:
        meta = {"futures": {
            "file": p.name,
            "fetched_at_ms": request_ms,
            "saved_utc": saved_tag or tag,
            "url": "https://fapi.binance.com/fapi/v1/exchangeInfo",
        }}
        (d / f"meta_{tag}.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return p


def _write_changes(d: Path, records) -> Path:
    p = d / "spec_changes_2026-10-17.json"
    p.write_text(json.dumps({"records": list(records)}, ensure_ascii=False),
                 encoding="utf-8")
    return p


def _announcement_sha() -> str:
    """变更记录必须带 64 位 sha256，否则不算证据。"""
    return hashlib.sha256(b"prc01-announcement").hexdigest()


def _no_change_record(symbol=SYM, frm=WS, to=WE, **over) -> dict:
    rec = {"kind": "no_change", "symbol": symbol,
           "from_ms": frm, "to_ms": to,
           "fields": list(COMPARED_SPEC_FIELDS),
           "source_url": ANNOUNCE,
           "evidence_sha256": _announcement_sha(),
           "note": "交易所公告与合约详情页核对：该区间无 tick/step/minQty 变更"}
    rec.update(over)
    return rec


def _change_record(symbol=SYM, effective_ms=None, announced_ms=None, **over) -> dict:
    rec = {"kind": "change", "symbol": symbol,
           "effective_ms": effective_ms,
           "announced_ms": announced_ms,
           "fields": ["price_step"],
           "source_url": ANNOUNCE,
           "evidence_sha256": _announcement_sha(),
           "note": "tickSize 变更公告"}
    rec.update(over)
    return rec


def _book(tmp_path: Path) -> SpecBook:
    return SpecBook(tmp_path)


def _baseline_dir(tmp_path: Path, *, extra_mid=None, changes=True) -> Path:
    """标准三段证据：窗口前 / 窗口中 / 窗口后，规格一致。

    `extra_mid` 可插入一份中间快照（用于构造「中间变了」的场景）。
    """
    d = tmp_path / "contracts"
    d.mkdir(parents=True, exist_ok=True)
    _write_snapshot(d, tag=_iso(T_BEFORE), server_ms=T_BEFORE, symbols=[_sym_doc()])
    if extra_mid is not None:
        _write_snapshot(d, tag=_iso(extra_mid[0]), server_ms=extra_mid[0],
                        symbols=[extra_mid[1]])
    _write_snapshot(d, tag=_iso(T_AFTER), server_ms=T_AFTER, symbols=[_sym_doc()])
    if changes:
        _write_changes(d, [_no_change_record()])
    return d


# ══════════════════════════ 1. 单快照 ══════════════════════════

def test_single_snapshot_cannot_verify_history(tmp_path):
    """只有一份快照 ⇒ 连「窗口后段」都没证据，必须 unverified。"""
    d = tmp_path / "contracts"
    d.mkdir()
    _write_snapshot(d, tag=_iso(T_BEFORE), server_ms=T_BEFORE, symbols=[_sym_doc()])
    _write_changes(d, [_no_change_record()])       # 即使给了变更证据也不行

    sp = _book(d).resolve(SYM, window_start_ms=WS, window_end_ms=WE)

    assert sp.applicability == APPLICABILITY_UNVERIFIED_HISTORICAL
    assert sp.formal_pass_allowed is False
    assert sp.coverage["reason"] == "no_snapshot_after_window_end"
    assert sp.price_step == "0.00001"               # 规格本身是真的，只是证明不了整窗


def test_no_snapshot_at_all_is_missing(tmp_path):
    d = tmp_path / "contracts"
    d.mkdir()
    sp = _book(d).resolve(SYM, window_start_ms=WS, window_end_ms=WE)
    assert sp.applicability == APPLICABILITY_MISSING
    assert sp.formal_pass_allowed is False


# ══════════════════════════ 2. 前后快照不同 ══════════════════════════

def test_endpoints_differ_is_unverified(tmp_path):
    """窗口前 tick=1e-5、窗口后 tick=1e-3 ⇒ 窗口内必然变过，单一规格不能代表整窗。"""
    d = tmp_path / "contracts"
    d.mkdir()
    _write_snapshot(d, tag=_iso(T_BEFORE), server_ms=T_BEFORE,
                    symbols=[_sym_doc(tick="0.000010")])
    _write_snapshot(d, tag=_iso(T_AFTER), server_ms=T_AFTER,
                    symbols=[_sym_doc(tick="0.001000")])
    _write_changes(d, [_no_change_record()])

    sp = _book(d).resolve(SYM, window_start_ms=WS, window_end_ms=WE)

    assert sp.applicability == APPLICABILITY_UNVERIFIED_HISTORICAL
    assert sp.coverage["reason"] in {"spec_changed_between_endpoints",
                                     "anchor_spec_differs_from_window_spec"}
    assert sp.formal_pass_allowed is False


# ══════════════════════════ 3. 两端相同但中间变了 ══════════════════════════

def test_identical_endpoints_with_middle_change_is_unverified(tmp_path):
    """两端一模一样、中间改过又改回来 —— 旧实现只看两端就会误判 verified。

    窗口前 1e-5 → 2024-03 变成 1e-3 → 2024-05 又改回 1e-5 → 2024-08 仍是 1e-5。
    两端相同，但窗口内确实变过 ⇒ 必须 unverified。
    """
    d = tmp_path / "contracts"
    d.mkdir()
    _write_snapshot(d, tag=_iso(T_BEFORE), server_ms=T_BEFORE,
                    symbols=[_sym_doc(tick="0.000010")])
    _write_snapshot(d, tag=_iso(T_MID1), server_ms=T_MID1,
                    symbols=[_sym_doc(tick="0.001000")])   # 中间变了
    _write_snapshot(d, tag=_iso(T_MID2), server_ms=T_MID2,
                    symbols=[_sym_doc(tick="0.000010")])   # 又改回来
    _write_snapshot(d, tag=_iso(T_AFTER), server_ms=T_AFTER,
                    symbols=[_sym_doc(tick="0.000010")])
    _write_changes(d, [_no_change_record()])

    sp = _book(d).resolve(SYM, window_start_ms=WS, window_end_ms=WE)

    assert sp.applicability == APPLICABILITY_UNVERIFIED_HISTORICAL
    assert sp.coverage["reason"] == "spec_changed_inside_span"
    # 证据链必须点名是哪一份快照不同（要求 1：说清用了哪些原始证据）
    bad = [c for c in sp.evidence_chain
           if c.get("role") == "intermediate" and c.get("same_as_anchor") is False]
    assert len(bad) == 1
    assert bad[0]["snapshot_server_ms"] == T_MID1
    assert len(bad[0]["sha256"]) == 64
    assert bad[0]["path"].endswith(".json")


def test_window_spec_not_unique_reports_window_start_spec(tmp_path):
    """窗口内规格不唯一（没有「改回来」那一份）⇒ 数值取窗口起点规格，不是锚点。

    这里 2024-03 改成 1e-3 后直到窗口结束都是 1e-3（2024-08 才改回），
    锚点（窗口结束前最近）是 1e-3；但整窗多数时间是 1e-5，报告锚点会误导。
    """
    d = tmp_path / "contracts"
    d.mkdir()
    _write_snapshot(d, tag=_iso(T_BEFORE), server_ms=T_BEFORE,
                    symbols=[_sym_doc(tick="0.000010")])
    _write_snapshot(d, tag=_iso(T_MID1), server_ms=T_MID1,
                    symbols=[_sym_doc(tick="0.001000")])
    _write_snapshot(d, tag=_iso(T_AFTER), server_ms=T_AFTER,
                    symbols=[_sym_doc(tick="0.000010")])

    sp = _book(d).resolve(SYM, window_start_ms=WS, window_end_ms=WE)
    assert sp.applicability == APPLICABILITY_UNVERIFIED_HISTORICAL
    assert sp.coverage["reason"] == "spec_changed_inside_span"
    assert sp.price_step == "0.00001"          # 窗口起点时的规格
    assert sp.formal_pass_allowed is False


def test_change_only_visible_in_change_record_blocks_verification(tmp_path):
    """快照全一致，但变更证据记载窗口内发生过变更 ⇒ 仍不能 verified。"""
    d = _baseline_dir(tmp_path)
    _write_changes(d, [
        _no_change_record(from_ms=T_BEFORE, to_ms=WS),
        _change_record(effective_ms=T_MID1, announced_ms=T_MID1 - 86400_000),
    ])
    sp = _book(d).resolve(SYM, window_start_ms=WS, window_end_ms=WE)
    assert sp.applicability == APPLICABILITY_UNVERIFIED_HISTORICAL
    assert sp.coverage["reason"] == "change_inside_window"


# ══════════════════════════ 4. 窗口内规格缺口 ══════════════════════════

def test_no_snapshot_before_window_start_is_gap(tmp_path):
    """只有窗口之后的快照 ⇒ 窗口起点之前无证据，属于缺口。"""
    d = tmp_path / "contracts"
    d.mkdir()
    _write_snapshot(d, tag=_iso(T_MID2), server_ms=T_MID2, symbols=[_sym_doc()])
    _write_snapshot(d, tag=_iso(T_AFTER), server_ms=T_AFTER, symbols=[_sym_doc()])
    _write_changes(d, [_no_change_record()])

    sp = _book(d).resolve(SYM, window_start_ms=WS, window_end_ms=WE)
    assert sp.applicability == APPLICABILITY_UNVERIFIED_HISTORICAL
    assert sp.coverage["reason"] == "no_snapshot_before_window_start"


def test_symbol_absent_inside_window_is_gap_not_no_change(tmp_path):
    """窗口中间某份快照里没有该合约 ⇒ 规格链断裂，不是「没变」。"""
    d = tmp_path / "contracts"
    d.mkdir()
    _write_snapshot(d, tag=_iso(T_BEFORE), server_ms=T_BEFORE, symbols=[_sym_doc()])
    _write_snapshot(d, tag=_iso(T_MID1), server_ms=T_MID1,
                    symbols=[_sym_doc(symbol="OTHERUSDT")])   # 缺本合约
    _write_snapshot(d, tag=_iso(T_AFTER), server_ms=T_AFTER, symbols=[_sym_doc()])
    _write_changes(d, [_no_change_record()])

    sp = _book(d).resolve(SYM, window_start_ms=WS, window_end_ms=WE)
    assert sp.applicability == APPLICABILITY_UNVERIFIED_HISTORICAL
    assert sp.coverage["reason"] == "symbol_absent_inside_window"
    assert any(c.get("role") == "absent" for c in sp.evidence_chain)


def test_unreadable_spec_inside_span_is_gap(tmp_path):
    """夹住区间内某份快照缺 tickSize ⇒ 无法比较 ⇒ 不能当「一致」。"""
    d = tmp_path / "contracts"
    d.mkdir()
    _write_snapshot(d, tag=_iso(T_BEFORE), server_ms=T_BEFORE, symbols=[_sym_doc()])
    _write_snapshot(d, tag=_iso(T_MID1), server_ms=T_MID1,
                    symbols=[_sym_doc(drop_filters=("PRICE_FILTER",))])   # 不可读
    _write_snapshot(d, tag=_iso(T_MID2), server_ms=T_MID2, symbols=[_sym_doc()])
    _write_snapshot(d, tag=_iso(T_AFTER), server_ms=T_AFTER, symbols=[_sym_doc()])
    _write_changes(d, [_no_change_record()])

    sp = _book(d).resolve(SYM, window_start_ms=WS, window_end_ms=WE)
    assert sp.applicability == APPLICABILITY_UNVERIFIED_HISTORICAL
    assert sp.coverage["reason"] == "spec_unreadable_inside_span"


def test_snapshot_without_server_time_blocks_verification(tmp_path):
    """有一份快照缺 serverTime ⇒ 无法定位 ⇒ 不能用来排除窗口内变更。"""
    d = _baseline_dir(tmp_path)
    _write_snapshot(d, tag="no_server_time", server_ms=None, symbols=[_sym_doc()])

    sp = _book(d).resolve(SYM, window_start_ms=WS, window_end_ms=WE)
    assert sp.applicability == APPLICABILITY_UNVERIFIED_HISTORICAL
    assert sp.coverage["reason"] == "snapshot_without_server_time"
    assert any(c.get("role") == "unpositioned" for c in sp.evidence_chain)


# ══════════════════════════ 5. 快照顺序扰动 ══════════════════════════

def _fingerprint(sp):
    return {
        "applicability": sp.applicability,
        "reason": (sp.coverage or {}).get("reason"),
        "anchor": sp.evidence_sha256,
        "price_step": sp.price_step,
        "qty_step": sp.qty_step,
        "min_qty": sp.min_qty,
        "max_qty": sp.max_qty,
        "min_notional": sp.min_notional,
        "chain": sorted((c.get("role"), c.get("sha256")) for c in sp.evidence_chain
                        if c.get("sha256")),
    }


def test_snapshot_order_perturbation_does_not_change_verdict(tmp_path):
    """文件名顺序与 serverTime 顺序相反 ⇒ 判定必须逐字相同。"""
    normal = _baseline_dir(tmp_path / "normal")
    # 反向：文件名时间戳与 serverTime 故意颠倒（内容不变）
    rev = tmp_path / "reversed" / "contracts"
    rev.mkdir(parents=True)
    _write_snapshot(rev, tag=_iso(T_AFTER), server_ms=T_BEFORE, symbols=[_sym_doc()])
    _write_snapshot(rev, tag=_iso(T_BEFORE), server_ms=T_AFTER, symbols=[_sym_doc()])
    _write_changes(rev, [_no_change_record()])

    a = _book(normal).resolve(SYM, window_start_ms=WS, window_end_ms=WE)
    b = _book(rev).resolve(SYM, window_start_ms=WS, window_end_ms=WE)

    assert _fingerprint(a) == _fingerprint(b)
    assert a.applicability == b.applicability == APPLICABILITY_VERIFIED


def test_filename_time_never_positions_a_snapshot(tmp_path):
    """文件名写 2024-08，但 serverTime 是 2023-12 ⇒ 必须按 serverTime 判成「窗口前」。"""
    d = tmp_path / "contracts"
    d.mkdir()
    _write_snapshot(d, tag=_iso(T_AFTER), server_ms=T_BEFORE, symbols=[_sym_doc()])
    _write_changes(d, [_no_change_record()])

    sp = _book(d).resolve(SYM, window_start_ms=WS, window_end_ms=WE)
    assert sp.applicability == APPLICABILITY_UNVERIFIED_HISTORICAL
    assert sp.coverage["reason"] == "no_snapshot_after_window_end"
    # 文件保存时间与交易所响应时间分别保存，且不相等
    assert sp.file_saved_ms == T_AFTER
    assert sp.snapshot_server_ms == T_BEFORE


def test_duplicate_server_time_is_one_time_point(tmp_path):
    """同一响应存两份（serverTime 相同）不构成两个时点 ⇒ 夹不住窗口。"""
    d = tmp_path / "contracts"
    d.mkdir()
    _write_snapshot(d, tag=_iso(T_BEFORE), server_ms=T_BEFORE, symbols=[_sym_doc()])
    _write_snapshot(d, tag="copy_of_before", server_ms=T_BEFORE, symbols=[_sym_doc()])
    _write_changes(d, [_no_change_record()])

    sp = _book(d).resolve(SYM, window_start_ms=WS, window_end_ms=WE)
    assert sp.applicability == APPLICABILITY_UNVERIFIED_HISTORICAL
    assert sp.coverage["reason"] == "no_snapshot_after_window_end"


# ══════════════════════════ 6. 时间互不代替 ══════════════════════════

def test_four_times_are_kept_apart(tmp_path):
    """请求获取 / 文件保存 / 交易所响应 / 规格生效 四个时间各归各位。"""
    d = tmp_path / "contracts"
    d.mkdir()
    fetch = T_AFTER + 3 * 86400_000          # 缓存：三天后才取到 T_MID1 的响应
    _write_snapshot(d, tag=_iso(T_BEFORE), server_ms=T_BEFORE, symbols=[_sym_doc()])
    _write_snapshot(d, tag=_iso(T_MID2), server_ms=T_MID1,      # 文件名时间 ≠ 响应时间
                    symbols=[_sym_doc()], request_ms=fetch)
    _write_snapshot(d, tag=_iso(T_AFTER), server_ms=T_AFTER, symbols=[_sym_doc()])
    _write_changes(d, [
        _no_change_record(),
        _change_record(effective_ms=T_BEFORE - 86400_000, announced_ms=T_BEFORE - 2 * 86400_000),
    ])

    sp = _book(d).resolve(SYM, window_start_ms=WS, window_end_ms=WE)

    assert sp.applicability == APPLICABILITY_VERIFIED
    assert sp.request_fetched_ms == fetch
    assert sp.file_saved_ms == T_MID2            # 文件名（保存时间）
    assert sp.snapshot_server_ms == T_MID1       # 交易所响应时间
    assert sp.spec_effective_ms == T_BEFORE - 86400_000
    # 四个值互不相等，任何两个都不能互相顶替
    assert len({sp.request_fetched_ms, sp.file_saved_ms,
                sp.snapshot_server_ms, sp.spec_effective_ms}) == 4
    # 旧字段 fetched_at_ms 明确等于「请求获取时间」，不是 serverTime
    assert sp.fetched_at_ms == fetch
    assert sp.fetched_at_ms != sp.snapshot_server_ms


def test_missing_request_time_stays_none_not_server_time(tmp_path):
    """没有 meta 文件 ⇒ 请求获取时间保持 None，禁止拿 serverTime 顶上。"""
    d = tmp_path / "contracts"
    d.mkdir()
    _write_snapshot(d, tag=_iso(T_BEFORE), server_ms=T_BEFORE, symbols=[_sym_doc()])
    _write_snapshot(d, tag=_iso(T_AFTER), server_ms=T_AFTER, symbols=[_sym_doc()])
    _write_changes(d, [_no_change_record()])

    sp = _book(d).resolve(SYM, window_start_ms=WS, window_end_ms=WE)
    assert sp.request_fetched_ms is None
    assert sp.fetched_at_ms is None
    assert sp.snapshot_server_ms == T_BEFORE     # 锚点=窗口结束前最近的快照
    assert sp.file_saved_ms == T_BEFORE
    assert sp.spec_effective_ms is None           # 没有变更记录 ⇒ 生效时间未知


def test_change_without_effective_time_blocks(tmp_path):
    """公告早于窗口、但只有公告时间没有生效时间 ⇒ 不能假定它已在窗口前生效。"""
    d = _baseline_dir(tmp_path)
    _write_changes(d, [
        _no_change_record(),
        _change_record(effective_ms=None, announced_ms=T_BEFORE - 86400_000),
    ])
    sp = _book(d).resolve(SYM, window_start_ms=WS, window_end_ms=WE)
    assert sp.applicability == APPLICABILITY_UNVERIFIED_HISTORICAL
    assert sp.coverage["reason"] == "change_effective_time_unknown"
    assert str(T_BEFORE - 86400_000) in sp.applicability_note


# ══════════════════════════ 7. 完整证据链 ⇒ verified（正例）══════════════════════════

def test_full_evidence_chain_is_verified(tmp_path):
    """包围 + 中间逐份一致 + 变更证据覆盖整窗 ⇒ 才允许 verified。"""
    d = _baseline_dir(tmp_path, extra_mid=(T_MID1, _sym_doc()))
    _write_snapshot(d, tag=_iso(T_MID2), server_ms=T_MID2, symbols=[_sym_doc()])

    sp = _book(d).resolve(SYM, window_start_ms=WS, window_end_ms=WE)

    assert sp.applicability == APPLICABILITY_VERIFIED
    assert sp.formal_pass_allowed is True
    assert sp.coverage["reason"] == "bracketed_and_change_evidence_covers_window"

    roles = [c["role"] for c in sp.evidence_chain]
    assert roles[0] == "anchor"
    assert "bracket_before" in roles and "bracket_after" in roles
    assert "intermediate" in roles

    # 每项判断都挂着原始证据（路径 + sha256 + 三类时间）
    for c in sp.evidence_chain:
        assert c["path"] and len(c["sha256"]) == 64
        assert "request_fetched_ms" in c and "file_saved_ms" in c
        assert "snapshot_server_ms" in c
    # 变更证据也带出处
    assert sp.change_evidence
    for r in sp.change_evidence:
        assert r["source_url"] and len(r["evidence_sha256"]) == 64
        assert r["evidence_path"]


def test_no_change_evidence_keeps_unverified(tmp_path):
    """证据链完美但没有任何变更证据 ⇒ 仍不得 verified（不得凑通过）。"""
    d = _baseline_dir(tmp_path, changes=False)
    sp = _book(d).resolve(SYM, window_start_ms=WS, window_end_ms=WE)
    assert sp.applicability == APPLICABILITY_UNVERIFIED_HISTORICAL
    assert sp.coverage["reason"] == "no_change_evidence"
    assert sp.formal_pass_allowed is False


def test_change_evidence_without_provenance_is_dropped(tmp_path):
    """变更记录缺 source_url / sha256 ⇒ 不算证据。"""
    d = _baseline_dir(tmp_path, changes=False)
    _write_changes(d, [_no_change_record(source_url="", evidence_sha256="")])
    sp = _book(d).resolve(SYM, window_start_ms=WS, window_end_ms=WE)
    assert sp.applicability == APPLICABILITY_UNVERIFIED_HISTORICAL
    assert sp.coverage["reason"] == "no_change_evidence"
    assert any(c.get("role") == "change_evidence_dropped" for c in sp.evidence_chain)


def test_change_evidence_must_cover_whole_window(tmp_path):
    """变更证据只覆盖窗口一半 ⇒ 不能 verified。"""
    d = _baseline_dir(tmp_path, changes=False)
    _write_changes(d, [_no_change_record(frm=WS, to=WS + 86400_000)])
    sp = _book(d).resolve(SYM, window_start_ms=WS, window_end_ms=WE)
    assert sp.applicability == APPLICABILITY_UNVERIFIED_HISTORICAL
    assert sp.coverage["reason"] == "change_evidence_does_not_cover_window"


# ══════════════════════════ 8. 数量过滤器（要求 4）══════════════════════════

def _spec_from(tmp_path, *, order_type="MARKET", **sym_kw):
    d = tmp_path / "contracts"
    d.mkdir(exist_ok=True)
    _write_snapshot(d, tag=_iso(T_BEFORE), server_ms=T_BEFORE,
                    symbols=[_sym_doc(**sym_kw)])
    return _book(d).resolve(SYM, order_type=order_type,
                            window_start_ms=WS, window_end_ms=WE)


def test_market_order_applies_market_lot_size(tmp_path):
    """MARKET 单必须同时满足 LOT_SIZE 与 MARKET_LOT_SIZE（上限取更小者）。"""
    sp = _spec_from(tmp_path, order_type="MARKET",
                    lot_max="2000000", mkt_max="600000")
    filters = {c["filter"] for c in sp.qty_constraints}
    assert {"LOT_SIZE", "MARKET_LOT_SIZE"} <= filters
    assert sp.max_qty == "600000"


def test_limit_order_does_not_apply_market_lot_size(tmp_path):
    """LIMIT 单不得套用 MARKET_LOT_SIZE（那会平白收紧上限）。"""
    sp = _spec_from(tmp_path, order_type="LIMIT",
                    lot_max="2000000", mkt_max="600000")
    filters = {c["filter"] for c in sp.qty_constraints}
    assert "MARKET_LOT_SIZE" not in filters
    assert "LOT_SIZE" in filters
    assert sp.max_qty == "2000000"


def test_multiple_steps_use_lcm_not_max(tmp_path):
    """LOT=2 / MARKET=3 同时适用 ⇒ 有效步长 6，不是「取较大值」3。

    这是本窗口修掉的第二个真实缺陷：取 3 会放过 qty=3，而 3 不是 2 的倍数，
    下单必被交易所拒绝。
    """
    sp = _spec_from(tmp_path, order_type="MARKET",
                    lot_step="2", lot_min="0", lot_max="1000",
                    mkt_step="3", mkt_min="0", mkt_max="900")
    assert sp.qty_step == "6"

    ok = sp.check_quantity("6")
    assert ok["ok"] is True
    bad = sp.check_quantity("3")
    assert bad["ok"] is False
    assert {v["filter"] for v in bad["violations"]} == {"LOT_SIZE"}


def test_check_quantity_reports_every_applicable_constraint(tmp_path):
    """逐条复核：每条过滤器的 step/min/max/notional 都要单独给出通过与否。"""
    sp = _spec_from(tmp_path, order_type="MARKET",
                    lot_step="0.01", lot_min="0.01", lot_max="2000000",
                    mkt_step="0.01", mkt_min="0.01", mkt_max="600000",
                    notional="5")
    res = sp.check_quantity("0.5", price="100")
    assert res["ok"] is True
    pairs = {(c["filter"], c["requirement"]) for c in res["checks"]}
    assert ("LOT_SIZE", "stepSize") in pairs
    assert ("LOT_SIZE", "maxQty") in pairs
    assert ("MARKET_LOT_SIZE", "maxQty") in pairs
    assert ("MIN_NOTIONAL", "minNotional") in pairs

    # 超出 MARKET_LOT_SIZE 上限 ⇒ 只这一条不通过，其余仍然记录
    over = sp.check_quantity("700000", price="100")
    assert over["ok"] is False
    assert {v["filter"] for v in over["violations"]} == {"MARKET_LOT_SIZE"}


def test_incompatible_qty_constraints_are_missing_not_silently_passed(tmp_path):
    """步长 2 而上限 1 ⇒ 无合法数量，必须显式失败，不得静默放过。"""
    sp = _spec_from(tmp_path, order_type="MARKET",
                    lot_step="2", lot_min="0", lot_max="1",
                    mkt_step="2", mkt_min="0", mkt_max="1")
    assert sp.applicability == APPLICABILITY_MISSING
    assert sp.formal_pass_allowed is False
    assert "qty_constraints_incompatible" in sp.applicability_note


def test_legacy_constructed_spec_check_quantity_still_works():
    """旧构造路径（无 qty_constraints）仍可逐条复核，接口未变。"""
    sp = InstrumentSpec(
        symbol="SOLUSDT", venue="binance", market="um",
        price_step="0.01", qty_step="0.01", min_qty="0.01", max_qty="1000",
        min_notional="5", price_precision=2, quantity_precision=2,
        order_type="MARKET", source_url="test", source_kind="test",
        fetched_at_ms=None, evidence_path=None, evidence_sha256=None,
        onboard_ms=None, snapshot_server_ms=None,
    )
    assert sp.check_quantity("0.01", price="600")["ok"] is True
    assert sp.check_quantity("0.005", price="600")["ok"] is False


def test_decimal_lcm_handles_decimals():
    def _s(v):
        return format(decimal_lcm(v), "f")
    assert _s(["0.01", "0.02"]) == "0.02"
    assert _s(["0.3", "0.7"]) == "2.1"
    assert _s(["1", "3", "5"]) == "15"
    assert _s(["0.0000100", "0.000010"]) == "0.00001"


# ══════════════════════════ 9. 接口未变 ══════════════════════════

def test_public_entry_signature_unchanged():
    """`resolve_instrument_spec` 的关键字与默认值逐字未变。"""
    params = inspect.signature(resolve_instrument_spec).parameters
    assert list(params) == ["symbol", "window_start_ms", "window_end_ms", "venue",
                            "market", "order_type", "observed_prices", "contracts_dir"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY
               for n, p in params.items() if n != "symbol")
    assert params["order_type"].default == "MARKET"
    assert params["venue"].default == "binance"
    assert params["market"].default == "um"


def test_public_entry_still_works_through_contracts_dir(tmp_path):
    d = _baseline_dir(tmp_path, extra_mid=(T_MID1, _sym_doc()))
    sp = resolve_instrument_spec(SYM, window_start_ms=WS, window_end_ms=WE,
                                 contracts_dir=d)
    assert sp.applicability == APPLICABILITY_VERIFIED
    assert sp.symbol == SYM


# ══════════════════════════ 10. 真实证据目录护栏 ══════════════════════════

REAL_SIX = ["LPTUSDT", "OPUSDT", "ORDIUSDT", "ACEUSDT", "BNBUSDT", "MOVRUSDT"]


@pytest.mark.parametrize("symbol", REAL_SIX)
def test_real_evidence_dir_never_yields_verified(symbol):
    """现有两份快照是同一响应的两次保存，且没有变更证据 ⇒ 六标的必须全部 unverified。

    这条是防「为凑通过而升级」的护栏：一旦有人放宽判定，它立刻红。
    """
    sp = resolve_instrument_spec(symbol, window_start_ms=WS, window_end_ms=WE)
    assert sp.applicability == APPLICABILITY_UNVERIFIED_HISTORICAL
    assert sp.formal_pass_allowed is False
    assert sp.blocking is True
    assert sp.coverage["reason"] in {"no_snapshot_before_window_start",
                                     "no_snapshot_after_window_end"}
    # 规格数值本身必须是真实抓到的，不是 0
    assert sp.tick > 0
