#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WyckoffMaster 回测窗口 · 局部隔离适配层（只读复用 wx_execution / ra_engine）。

纪律（红线，违反即拒绝运行）：
  1. **不修改**生产引擎：`后端实现/strategies/wx_execution.py` 必须 SHA == 冻结值，
     且本进程**从不写**该文件或任何 `后端实现/`、`加密货币趋势判断系统/` 下的文件。
  2. **不写生产库**：热库/冷档/paper 账本一律只读（由 wx_* 内部 `mode=ro` + `query_only` 保证）。
  3. **只写** `回测日志/WyckoffMaster/` 下（账本、日志、状态）。
  4. **禁止** norm4h / ext1h 近似模型冒充真实历史资金费；只允许 `historical_events`。
  5. **不抢** 3000 / 8787 端口，不启停生产服务。

本模块只是**薄桥**：把已验证的 RA 输入仓 + 执行引擎按「单段（segment）」封装起来，
供 `run_ladder.py` 顺序实验共用一次 InputStore。不复制任何引擎实现。
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

# ── 路径常量（只读源；本窗口唯一写入根）─────────────────────────────────────
PROJECT = Path("/Users/boos/Desktop/威科夫因子共振趋势")
WRITE_ROOT = PROJECT / "回测日志" / "WyckoffMaster"

BASE = Path("/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究")
BACKEND = BASE / "后端实现"
W549 = BASE / "全池真实费用回测_2026-10-13"
RA = BASE / "回测资源改造_2026-10-14"
RA_SRC = RA / "src"
W549_SRC = W549 / "src"

INPUT_STORE = BACKEND / "历史下载" / "perp_datadir_real" / "data"
DAILY_DOC = W549 / "inputs" / "daily_549_DEV.json"

FROZEN_WX_EXECUTION_SHA = "0ede9ce19c7cec9d18398572e393ef65ef4c04a6aa5ff2b41f8ad022c83dbcf1"
FROZEN_CONFIG_VIEW_SHA = "6d18d222d1991f99dd2ba0cdf80b35fbc57f4f482904c945f43d66ebb9df9501"
ALLOWED_FUNDING_MODEL = "historical_events"
FORBIDDEN_FUNDING_MODELS = ("norm4h", "ext1h")

# 生产只读源（本进程不得写入）
PROTECTED_ROOTS = (
    BACKEND,
    Path("/Users/boos/WorkBuddy AI/加密货币趋势判断系统"),
    W549, RA,
)


class IsolationError(RuntimeError):
    """隔离纪律被违反。"""


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def assert_write_target(p: Path) -> Path:
    """任何写目标必须落在 WRITE_ROOT 内，否则拒绝。"""
    rp = Path(p).resolve()
    if WRITE_ROOT.resolve() not in rp.parents and rp != WRITE_ROOT.resolve():
        raise IsolationError(f"写目标越界（只允许写 {WRITE_ROOT}）：{rp}")
    return rp


def assert_production_untouched() -> dict:
    """核验生产引擎 SHA 未被改动；顺带核验关键源只读存在。"""
    facts = {}
    p = BACKEND / "strategies" / "wx_execution.py"
    if not p.exists():
        raise IsolationError(f"缺生产引擎：{p}")
    got = sha256_file(p)
    facts["wx_execution_sha256"] = got
    facts["wx_execution_sha_ok"] = (got == FROZEN_WX_EXECUTION_SHA)
    if not facts["wx_execution_sha_ok"]:
        raise IsolationError(
            "生产引擎 wx_execution.py SHA 与冻结值不符，拒绝运行（可能被其他窗口改写）：\n"
            f"  expect {FROZEN_WX_EXECUTION_SHA}\n  got    {got}")
    facts["config_view_sha256"] = sha256_file(
        BASE / "最新唐奇安威科夫接通与收尾_2026-10-11" / "5000U收尾与回测条件终验6"
        / "10_研究参数生效视图_v2_2_user5000_shared.json")
    facts["config_view_sha_ok"] = (facts["config_view_sha256"] == FROZEN_CONFIG_VIEW_SHA)
    for q in (INPUT_STORE, DAILY_DOC):
        facts[f"exists::{q.name}"] = q.exists()
    return facts


def assert_funding_model(model: str) -> str:
    m = (model or "").strip()
    if m in FORBIDDEN_FUNDING_MODELS or "norm4h" in m or "ext1h" in m:
        raise IsolationError(
            f"禁止用近似模型 {m!r} 冒充真实历史资金费；只允许 {ALLOWED_FUNDING_MODEL!r}")
    if m != ALLOWED_FUNDING_MODEL:
        raise IsolationError(f"资金费模型必须为 {ALLOWED_FUNDING_MODEL!r}，收到 {m!r}")
    return m


def _ensure_syspath() -> None:
    for p in (str(BACKEND), str(W549_SRC), str(RA_SRC)):
        if p not in sys.path:
            sys.path.insert(0, p)
    sys.dont_write_bytecode = True


def load_engine():
    """加载已验证引擎与输入仓组件（只读复用，不复制实现）。"""
    _ensure_syspath()
    import w549 as W5              # noqa: N811
    import ra_arm as RAARM
    import ra_store as RS
    from strategies import wx_execution as X
    from strategies import wx_ledger as L
    W5.patch_engine()
    return {"W5": W5, "RAARM": RAARM, "RS": RS, "X": X, "L": L}


def make_shared_store(symbols, start_ms: int, end_ms: int, *, data_version: str, log=print,
                      phase: str = "kline"):
    """构建一次共享只读 InputStore（四臂/多实验顺序复用，不复制输入）。

    v3.1 最高红线：
      1. 先过 Holdout 守卫——选型阶段任何与 Holdout 有交集的窗口一律拒绝；
      2. 强制通过 3 条数据卫生规则（同质 526 币池锁定、准 PIT 过滤、长停牌保护）。
    """
    import holdout_guard as HG
    import data_hygiene as DH
    HG.assert_window_allowed(start_ms, end_ms, phase)
    # 强制数据卫生规则 1：锁定 526 同质币池并排除 OKX
    symbols = DH.assert_homogeneous_pool(symbols)
    DH.print_data_hygiene_summary()

    eng = load_engine()
    store = eng["RAARM"].make_store(symbols, start_ms, end_ms,
                                    data_version=data_version, log=log)
    for k in symbols:
        store.s15(k)
        store.smark(k)
    return store, eng


def run_segment_arm(*, eng, store, arm: str, symbols, start_ms: int, end_ms: int,
                    doc_path=DAILY_DOC, run_id: str, ledger_path: Path, log=print,
                    phase: str = "kline"):
    """在**独立账本**上跑一个实验臂的一段（写目标强制落在 WRITE_ROOT 内）。"""
    import sqlite3
    import holdout_guard as HG
    HG.assert_window_allowed(start_ms, end_ms, phase)
    lp = assert_write_target(ledger_path)
    lp.parent.mkdir(parents=True, exist_ok=True)
    if lp.exists():
        lp.unlink()
    con = sqlite3.connect(str(lp))
    con.row_factory = sqlite3.Row
    eng["L"].create_schema(con)
    try:
        out, sd, sig, res = eng["RAARM"].run_actual(
            con=con, run_id=run_id, arm=arm, symbols=symbols, start_ms=start_ms,
            end_ms=end_ms, store=store, doc_path=str(doc_path), log=log)
    finally:
        con.close()
    return {"ledger": str(lp), "run": out, "resource": res}


if __name__ == "__main__":
    import json
    print(json.dumps({"facts": assert_production_untouched(),
                      "write_root": str(WRITE_ROOT),
                      "input_store": str(INPUT_STORE)},
                     ensure_ascii=False, indent=2))
