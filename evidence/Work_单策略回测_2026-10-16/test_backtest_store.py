# -*- coding: utf-8 -*-
"""研究回测持久化语义测试：原子独占创建 / 重名拒绝 / 重启标 interrupted / 不删旧账本。

运行：
  OI_RESONANCE_RUNS_DIR=/tmp/bt_store_test \
  /Users/boos/.workbuddy-ai/binaries/python/envs/default/bin/python -m pytest \
    evidence/Work_单策略回测_2026-10-16/test_backtest_store.py -q
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势")
sys.path.insert(0, str(ROOT / "框架" / "backend"))
sys.path.insert(0, str(ROOT / "策略"))

TMP = Path(os.environ.get("OI_RESONANCE_RUNS_DIR", "/tmp/bt_store_test"))
os.environ["OI_RESONANCE_RUNS_DIR"] = str(TMP)

S = importlib.import_module("backtest_store")


@pytest.fixture(autouse=True)
def _clean():
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    yield
    shutil.rmtree(TMP, ignore_errors=True)


def test_atomic_exclusive_create_and_duplicate_rejected():
    rid = S.new_run_id("a" * 64)
    S.create_run(rid, {"spec": {}, "status": S.STATUS_QUEUED})
    assert (TMP / rid / "run.json").exists()
    with pytest.raises(S.RunExists):
        S.create_run(rid, {"spec": {}, "status": S.STATUS_QUEUED})


def test_run_id_format_and_path_guard():
    rid = S.new_run_id("b" * 64)
    assert S.assert_run_id(rid) == rid
    for bad in ("../etc", "bt_x", "bt_20260101T000000Z_ZZZZZZZZ", "/abs"):
        with pytest.raises(S.RunNotFound):
            S.assert_run_id(bad)


def test_ledger_never_unlinks_existing():
    rid = S.new_run_id("c" * 64)
    S.create_run(rid, {"spec": {}, "status": S.STATUS_QUEUED})
    with S.Ledger(rid) as lg:
        lg.meta("k", "v")
    p = S.ledger_path(rid)
    assert p.exists()
    before = p.read_bytes()
    with pytest.raises(S.RunExists):
        S.Ledger(rid)                       # 第二次构造必须拒绝（旧实现会 unlink）
    assert p.read_bytes() == before


def test_status_machine_and_orphans_interrupted():
    rid = S.new_run_id("d" * 64)
    S.create_run(rid, {"spec": {}, "status": S.STATUS_QUEUED})
    S.set_status(rid, S.STATUS_RUNNING)
    doc = S.read_run(rid)
    assert doc["status"] == "running" and doc.get("started_at")
    touched = S.mark_orphans_interrupted()
    assert rid in touched
    doc = S.read_run(rid)
    assert doc["status"] == S.STATUS_INTERRUPTED and doc.get("failure_reason")
    # 终态不被再次改写
    assert S.mark_orphans_interrupted() == []


def test_terminal_status_not_overwritten_by_orphan_scan():
    rid = S.new_run_id("e" * 64)
    S.create_run(rid, {"spec": {}, "status": S.STATUS_QUEUED})
    S.set_status(rid, S.STATUS_SUCCEEDED, metrics={"trades_count": 0})
    assert S.mark_orphans_interrupted() == []
    assert S.read_run(rid)["status"] == S.STATUS_SUCCEEDED


def test_list_runs_reads_docs_and_flags_broken():
    rid = S.new_run_id("f" * 64)
    S.create_run(rid, {"spec": {"symbols": ["BTCUSDT"]}, "status": S.STATUS_QUEUED})
    broken = TMP / "bt_20260101T000000Z_00000000"
    broken.mkdir()
    runs = S.list_runs()
    ids = {r["run_id"]: r for r in runs}
    assert rid in ids and ids[rid]["status"] == S.STATUS_QUEUED
    assert ids[broken.name]["status"] == "unknown"      # 不冒充成功
