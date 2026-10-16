#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""信号与持仓接通（2026-10-13）· **模拟实盘执行引擎**（纯标准库；不依赖 numpy/ccxt）。

本模块解决第 43 窗口遗留的 `BLOCKED_NO_FILL`：

  原卡点（`03_纸面配置与卡点_43.json`）：
    · `no_verified_paper_execution_model`  —— 「无已核实的现成纸面执行模型」；
    · `no_verified_quote_freshness_config` —— 「无已核实的报价新鲜度阈值配置」。

  解法（**不新造策略、不新造账户体系**）：
    · **成交模型直接由已批准参数视图推导**（`10_研究参数生效视图_v2_2_user5000_shared.json`
      的 `execution`/`stops`/`risk` 段，**运行期只读引用**，不复制数值、不改字节）：
        - 成交标的 bar：`detail_timeframe=15m`；
        - 入场时钟：`signal_execution_delay_minutes=15` ⇒ 确认时刻后**第一根已闭合 15m bar**；
        - 入场价：`entry=market_at_eligible_detail_open` ⇒ 该 bar 的 **open**；
        - 出场时钟：`signal_exit_delay_minutes=15`；手工平仓按**最近一根已闭合 15m bar 收盘**；
        - 手续费 `fee_each_side=0.0005`、滑点 `slippage_each_side=0.0005`（开/平各一次）；
        - 固定止损 `stops.uniform = 入场价 − 3×信号 ATR`；`ROI/trailing=off`；
        - `gap_at_stop = worse_open_with_adverse_slippage`；`max_holding_hours=720`；
        - 风控：`leverage=1`、`max_positions=10`、单名 10% 权益、总多 80%、现金 20%、
          组合初始止损风险 3%、账户回撤 20% 锁止、`short_enabled=false`（只多）。
    · **成交价完全不依赖实时报价**：只用「已闭合 15m bar」——因果、确定、可复现，
      因此**成交不需要报价新鲜度阈值**（原卡点二对「成交」自动失效）。
    · **估值/资金费**用真实数据：权益按 `equity_valuation=15m_contract_last_close` 已批准口径
      （已闭合 15m 收盘）；资金费按 `funding_history` 的**真实结算事件**（真实时刻 + 真实
      结算费率 + 真实 mark_price）记账，**不固定 8h、不补 0、不倒填**。
    · 报价新鲜度配置仍**显式定义**（`QUOTE_FRESHNESS`，用于「实时报价是否可用于估值」，
      不作为成交前置），并**如实报告**这是本窗口新增的显式取值、待人类追认。

纪律：**无真实下单入口**（本模块不含任何交易所写接口）；不改行情库；账本只在既有
`纸面模拟43/paper_{arm}.sqlite3` 内做**加性**写入。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path

import live_store as L
import exec_bridge as EB                     # v3.1 共用执行核心桥（策略/风控/成交唯一来源）

UTC = dt.timezone.utc
H = 3_600_000
MIN = 60_000

MODEL_VERSION = "paper_exec_v1_derived_from_effective_view_20261013"
# 账本口径标识：绑定共用执行核心版本（供任务三核对「回放与实时同口径」）
LEDGER_MODEL_VERSION = (EB.LEDGER_MODEL_VERSION if EB.CORE_AVAILABLE
                        else "paper_exec_v1_core_unavailable")
DETAIL_TF = "15m"

# ── 报价新鲜度配置（本窗口**显式定义**；待人类追认；不用于成交定价）────────────
QUOTE_FRESHNESS = {
    "defined_by": "work_2026-10-13",
    "ratification_status": "pending_human_ratification",
    "last_max_age_s": 300,      # 一个 5m 桶
    "mark_max_age_s": 300,
    "funding_max_age_s": 3600,
    "on_stale": "fallback_to_closed_15m_close_and_label_source",
    "note": "仅决定「实时报价能否用于估值/显示」；**成交价一律取已闭合 15m bar**，"
            "故成交不依赖本配置（原卡点二对成交失效）。",
}

# ── 成交时钟（**因果修正 2026-10-13**）────────────────────────────────────────
# 原缺陷（只读复核 #1，两条）：
#   ① `open_position` 允许「容量拒绝后数小时/数天才用**最早那根** bar 的旧价迟开」；
#   ② 页面手工开仓走 `anchor_to_last_closed`，锚定**已经闭合**的 15m bar 并取其**开盘价**
#      ——那是**事后可得价格**（请求时该价已成历史，回填即前视）。
# 修正后的**唯一成交时钟**：
#   ① 经济请求时刻 `order_request_ms` = max(信号 `event_time`, 信号 `available_at`)；
#      无信号（页面手工请求）⇒ 就是**真实请求挂钟时刻**。
#   ② 最早可成交时刻 `earliest_fill_ms = order_request_ms + 已批准延迟(15m)`。
#      ⇒ **成交绝不早于请求/信号可用时刻**（`anchor_to_last_closed` 已删除）。
#   ③ 成交 bar = 第一根 `open_time >= earliest_fill_ms` 的**已闭合** 15m bar；
#      成交价 = 该 bar 的 `open`（已批准 `market_at_eligible_detail_open`）。
#   ④ 该 bar 尚不存在 ⇒ **挂单 `paper_pending`**（未来待成交），由**真实进程时钟**推进
#      （`advance_pending`）；**不为跑通造历史成交**。
#   ⑤ 发现时已错过成交时刻（`本次尝试时刻 - bar.open_time > max_fill_lag`）⇒
#      **拒单 `missed_fill_moment_no_backfill`**，绝不用旧价回填。
#      这正是「容量拒绝后等数小时/数天再迟开」被堵住的那条路径。
FILL_POLICY = {
    "defined_by": "work_2026-10-13",
    "ratification_status": "pending_human_ratification",
    "fill_price": "open_of_first_closed_15m_bar_at_or_after(order_request_ms + delay)",
    "no_fill_before": "max(signal.event_time, signal.available_at, 真实请求时刻)",
    "future_fill": "paper_pending_advanced_by_real_process_clock",
    "on_missed_moment": "reject_missed_fill_moment_no_backfill",
    "max_fill_lag_seconds": 3600,
    "approved_delay_rule_kept": True,
    "note": "本窗口**显式定义**并如实报告；已批准信号延迟规则（signal_execution_delay_minutes）"
            "逐字保留；无信号的手工请求同样遵守同一延迟，不提前、不回填。",
}
MAX_FILL_LAG_MS = 3600_000
MARKET_SOURCE = "binance.um.candles_15m.is_closed.open"
FUNDING_SOURCE = "funding_history.settled"

# ── 服务端新开仓总闸（2026-10-15 闭环整改·阶段A）────────────────────────────────
# 合同 A 要求：对「尚未通过闭环验收的模拟盘」设置**可回滚**的新开仓阻断，且
# **自动（worker）/手工（POST /api/v1/paper/orders）/挂单推进（advance_pending）**
# 三条路径必须一致；**不得**把「仅禁用 UI 按钮」当服务端门禁。
#
# 设计原则：
#   · **fail-closed**：控制文件缺失 / 不可读 / 未显式放行 ⇒ **一律拒绝新开仓**；
#     仅当文件显式 `{"allow_new_entries": true}` 才放行（供 Codex 运行验收后受控启用）。
#   · **每次调用实时读盘** ⇒ 无需重启进程即可生效 / 回滚（可回滚即改回 false）。
#   · **作用域仅限「新开仓」**：退出（close_*）、费用、资金费（accrue_funding）、
#     健康采集与只读 API **不经过** `open_position`，因此**不受本闸影响**。
#   · 被拦下的信号写 `paper_order(status='rejected')` + `paper_blocked`（幂等、可审计），
#     **不写** `paper_processed` ⇒ 属**可重试**拒单，不会把信号静默丢弃或补开。
GATE_FILE = Path(__file__).resolve().parent / "live" / "paper_new_entry_gate.json"
GATE_REASON = "new_entry_blocked_pending_closure_review"


def new_entry_gate() -> dict:
    """读取服务端新开仓总闸（实时读盘；fail-closed）。"""
    raw = None
    try:
        raw = json.loads(GATE_FILE.read_text(encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        raw = None
    ok = isinstance(raw, dict)
    allow = bool(ok and raw.get("allow_new_entries") is True)
    return {
        "allow_new_entries": allow,
        "gate_reason": GATE_REASON,
        "reason": (raw.get("reason") if ok else "gate_file_missing_or_unreadable"),
        "gate_file": str(GATE_FILE),
        "gate_scope": "server_side_new_entry_only",
        "exits_fees_funding_health_unaffected": True,
        "set_by": (raw.get("set_by") if ok else None),
        "since_ms": (raw.get("since_ms") if ok else None),
    }

# 已批准参数视图（**只读引用**，运行期读取）
EFFECTIVE_VIEW = Path(
    "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/"
    "最新唐奇安威科夫接通与收尾_2026-10-11/5000U收尾与回测条件终验6/"
    "10_研究参数生效视图_v2_2_user5000_shared.json")

# 兜底（仅当视图文件不可读时使用；并在返回值里显式标注 source=fallback）
# **2026-10-15 闭环整改·阶段A**：兜底值**改为冻结视图 v2.2 的保守真值**，
# 不再沿用此前那份「更宽松」的兜底（risk 1%、20 仓、单名 15%、总敞口 90%、
# 现金 10%、组合 5%、short_enabled=True、2×ATR、参与率 2%、delay 0）。
# 依据：`10_研究参数生效视图_v2_2_user5000_shared.json`（spec
# `F_WX_v2.2_project_closure_user_5000_shared`）。**视图不可读时按冻结规则 fail-safe**。
FALLBACK = {
    "fee_each_side": 0.0005, "slippage_each_side": 0.0005,
    "signal_execution_delay_minutes": 15, "signal_exit_delay_minutes": 15,
    "detail_timeframe": "15m",
    "initial_equity_USDT": 5000.0, "risk_per_trade_equity_fraction": 0.005,
    "leverage": 1, "max_positions": 10, "single_name_notional_equity_cap": 0.1,
    "gross_long_notional_equity_cap": 0.8, "cash_or_free_margin_reserve_fraction": 0.2,
    "portfolio_initial_stop_risk_cap": 0.03, "maximum_account_drawdown_fraction": 0.2,
    "short_enabled": False, "max_holding_hours": 720,
    "uniform_stop_atr_mult": 3.0,
    # 已批准参与率上限（**上一根** 15m quote 的 1%）。视图键名逐字：
    # `risk.entry_participation_previous_15m_quote_cap`。缺失 ⇒ 保留此冻结值并标注 fallback。
    "entry_participation_previous_15m_quote_cap": 0.01,
}


def _refresh_config_source() -> dict:
    """加载**共用执行核心**的配置来源（视图核对而非覆盖；缺失只降级不替换）。"""
    global _CONFIG_SOURCE
    if not _CONFIG_SOURCE:
        if EB.CORE_AVAILABLE:
            _CONFIG_SOURCE = EB.load_exec_config()
        else:
            _CONFIG_SOURCE = {"config_source": "exec_core_unavailable", "degraded": True,
                              "errors": [EB.IMPORT_ERROR], "mismatches": []}
    return _CONFIG_SOURCE


_CONFIG_SOURCE: dict = {}


def approved_params() -> dict:
    """只读引用已批准参数（不复制到库、不改字节）。返回扁平 dict + source 标注。

    **2026-10-15 闭环整改·阶段A（无授权 AI 覆盖已删除）**：本函数此前在读取视图后
    **强行覆盖**了四处参数（`uniform_stop_atr_mult=2.0` / `signal_execution_delay_minutes=0`
    / `short_enabled=True` / `max_positions=20`），注释自称「优化」。经核对：
      · 冻结视图 `10_研究参数生效视图_v2_2_user5000_shared.json` 明列
        `execution.signal_execution_delay_minutes=15`、`risk.short_enabled=false`、
        `risk.max_positions=10`、`stops.uniform=actual_entry_minus_3_signal_ATR_fixed`（=3×ATR）；
      · 视图 `user_actual_risk_per_trade_approval=false`、`user_actual_leverage_cap_approval=false`、
        `live_trading_authorized=false`；人类授权原文见
        `evidence/Work_闭环整改_2026-10-15/01_授权来源核对.md`（**无**更新授权可覆盖上述四项）。
    ⇒ 删除四处覆盖，**逐字按冻结视图执行**；同时补齐此前**漏读**的
    `signal_exit_delay_minutes` / `max_positions` / 单名·总敞口·现金·组合风险·单笔风险·
    参与率 / `short_enabled` 等冻结键。返回 `view_sha256` 供逐笔核对绑定。
    """
    p = dict(FALLBACK)
    src = "fallback_frozen_v2_2_conservative"
    try:
        view = json.loads(EFFECTIVE_VIEW.read_text(encoding="utf-8"))
        ex = view.get("execution") or {}
        rk = view.get("risk") or {}
        st = view.get("stops") or {}
        for k in ("fee_each_side", "slippage_each_side", "detail_timeframe",
                  "signal_execution_delay_minutes", "signal_exit_delay_minutes"):
            if k in ex:
                p[k] = ex[k]
        for k in ("initial_equity_USDT", "leverage", "max_positions",
                  "single_name_notional_equity_cap", "gross_long_notional_equity_cap",
                  "cash_or_free_margin_reserve_fraction", "portfolio_initial_stop_risk_cap",
                  "risk_per_trade_equity_fraction",
                  "entry_participation_previous_15m_quote_cap",
                  "maximum_account_drawdown_fraction", "short_enabled"):
            if k in rk:
                p[k] = rk[k]
        if "max_holding_hours" in st:
            p["max_holding_hours"] = st["max_holding_hours"]
        # 固定止损倍数：冻结 `stops.uniform` = `actual_entry_minus_3_signal_ATR_fixed`
        # ⇒ 3×信号 ATR（原实现被无授权「优化」成 2×，本阶段还原）。
        p["uniform_stop_atr_mult"] = 3.0
        p["view_spec_version"] = view.get("spec_version")
        p["view_sha256"] = hashlib.sha256(EFFECTIVE_VIEW.read_bytes()).hexdigest()
        src = "frozen_view_v2_2_user5000_shared"
    except Exception:                                # noqa: BLE001
        pass
    _refresh_config_source()
    p["_source"] = src
    p["_exec_core_crosscheck"] = _crosscheck_with_exec_core(p)
    return p


# 生效视图键 ↔ 共用执行核心冻结键的**逐项对照**（不一致即如实报出，不静默择一）
_CROSSCHECK_MAP = {
    "initial_equity_USDT": "initial_equity",
    "max_positions": "max_positions_total",
    "risk_per_trade_equity_fraction": "risk_per_trade_fraction",
    "single_name_notional_equity_cap": "single_name_notional_cap",
    "gross_long_notional_equity_cap": "gross_long_notional_cap",
    "cash_or_free_margin_reserve_fraction": "cash_reserve_fraction",
    "portfolio_initial_stop_risk_cap": "portfolio_stop_risk_cap",
    "entry_participation_previous_15m_quote_cap": "entry_participation_prev15m_cap",
    "leverage": "leverage",
    "short_enabled": "short_enabled",
    "fee_each_side": "fee_each_side",
    "slippage_each_side": "slippage_each_side",
    "maximum_account_drawdown_fraction": "max_drawdown_fraction",
    "uniform_stop_atr_mult": None,          # 由 core 的 stops 段承载（本模块不参与）
}


def _crosscheck_with_exec_core(params: dict) -> dict:
    if not EB.CORE_AVAILABLE:
        return {"available": False, "import_error": EB.IMPORT_ERROR}
    diffs = []
    for vk, ck in _CROSSCHECK_MAP.items():
        if ck is None or vk not in params:
            continue
        want = getattr(EB.EXEC_FROZEN, ck)
        got = params[vk]
        try:
            same = (bool(got) == want) if isinstance(want, bool) else abs(float(got) - float(want)) < 1e-12
        except (TypeError, ValueError):
            same = False
        if not same:
            diffs.append({"view_key": vk, "exec_core_key": ck,
                          "view_value": got, "exec_core_value": want})
    return {"available": True, "ok": not diffs, "mismatches": diffs,
            "exec_core_config_hash": EB.EXEC_FROZEN.hash()}


# ── 合约规格（已批准**保守执行近似规格**，只读引用）──────────────────────────
# 与冻结回测同源：`strategies/wx_daily_375.py` 的 `SPEC_ROUNDING`
# （`本次离线决策_2026-10-12/07_526币保守执行近似规格.csv`）。此处**直接读同一 CSV**
# （stdlib `csv`），不 `import wx_daily_375`（它模块级 import pyarrow，会让单测静默 skip）。
CONTRACT_SPEC_CSV = Path(
    "/Users/boos/.codex/worktrees/c9d7/加密货币趋势判断系统/本次离线决策_2026-10-12/"
    "07_526币保守执行近似规格.csv")
_SPEC_ROWS: dict | None = None


def contract_limits(symbol: str):
    """返回该币的**有效下单限制**（普通下单 × 市价下单的**交集**，保守）。

    口径与冻结 `wx_execution.effective_order_limits` 逐字一致：
      · `quantity_step` = max(普通步进, 市价步进)（步进取粗 ⇒ 更保守）；
      · `min_quantity`  = max(普通最小量, 市价最小量)；
      · `max_quantity`  = 两侧皆给 ⇒ min；仅一侧 ⇒ 该侧；皆缺 ⇒ 0（不设上限）；
      · `min_notional`  = 普通最小订单金额；`price_step` = 价格最小变动。
    缺该币规格或步进 <=0 ⇒ `None`（调用方**拒单**，不设默认小步进）。
    """
    global _SPEC_ROWS
    if _SPEC_ROWS is None:
        rows = {}
        try:
            import csv as _csv
            with open(CONTRACT_SPEC_CSV, encoding="utf-8-sig", newline="") as f:
                for r in _csv.DictReader(f):
                    key = (r.get("币") or "").strip()
                    if key:
                        rows[key] = r
        except Exception:                                # noqa: BLE001
            rows = {}
        _SPEC_ROWS = rows
    r = _SPEC_ROWS.get(symbol)
    if not r:
        return None

    def _num(col):
        try:
            return float(r.get(col) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    step = max(_num("数量步进"), _num("市价数量步进"))
    if step <= 0:
        return None
    mx_a, mx_b = _num("最大数量"), _num("市价最大数量")
    mx = min(mx_a, mx_b) if (mx_a > 0 and mx_b > 0) else (mx_a or mx_b)
    return {"quantity_step": step,
            "min_quantity": max(_num("最小数量"), _num("市价最小数量")),
            "max_quantity": mx,
            "min_notional": _num("最小订单金额"),
            "price_step": _num("价格步进")}


def _apply_order_rounding(**kw):
    """**惰性**转发冻结的 `wx_execution.apply_order_rounding`（同一函数、同一规格口径）。"""
    from strategies.wx_execution import apply_order_rounding
    return apply_order_rounding(**kw)


def _round_protect_price(px, step):
    """**惰性**转发冻结的保护价取整（多头止损向下=更保守）。"""
    from strategies.wx_execution import round_protect_price
    return round_protect_price(px, step)


# ── 行情读取（只读；已闭合 15m）───────────────────────────────────────────────
def closed_15m(con: sqlite3.Connection, symbol: str, start_ms: int, end_ms: int):
    return list(con.execute(
        "SELECT open_time,close_time,open,high,low,close,quote_volume FROM candles "
        "WHERE exchange='binance' AND market='um' AND symbol=? AND timeframe='15m' "
        "AND is_closed=1 AND open_time>=? AND open_time<? ORDER BY open_time",
        (symbol, int(start_ms), int(end_ms))))


def bar_at_or_after(con: sqlite3.Connection, symbol: str, ts_ms: int):
    """第一根 `open_time >= ts_ms` 的已闭合 15m bar（入场时钟用）。"""
    return con.execute(
        "SELECT open_time,close_time,open,high,low,close,quote_volume FROM candles "
        "WHERE exchange='binance' AND market='um' AND symbol=? AND timeframe='15m' "
        "AND is_closed=1 AND open_time>=? ORDER BY open_time LIMIT 1", (symbol, int(ts_ms))).fetchone()


def last_closed_15m(con: sqlite3.Connection, symbol: str):
    return con.execute(
        "SELECT open_time,close_time,open,high,low,close,quote_volume FROM candles "
        "WHERE exchange='binance' AND market='um' AND symbol=? AND timeframe='15m' "
        "AND is_closed=1 ORDER BY open_time DESC LIMIT 1", (symbol,)).fetchone()


def bar_before(con: sqlite3.Connection, symbol: str, ts_ms: int):
    """**严格早于** `ts_ms` 的最近一根已闭合 15m bar（= 入场 bar 之前那根）。

    为什么必须单独取：已批准参与率规则是 `entry_participation_previous_15m_quote_cap`
    ——「**上一根** 15m 的成交额」，不是**入场 bar 本身**的成交额。入场 bar 的
    `quote_volume` 在它**开盘时刻**（= 我们决策并成交的时刻）**尚不可知**，直接引用
    即构成前视（lookahead）。此函数只返回 `open_time < ts_ms` 的已闭合 bar ⇒
    其 `close_time <= ts_ms - 1ms`，在决策时刻**已经完整可得**。
    """
    return con.execute(
        "SELECT open_time,close_time,open,high,low,close,quote_volume FROM candles "
        "WHERE exchange='binance' AND market='um' AND symbol=? AND timeframe='15m' "
        "AND is_closed=1 AND open_time<? ORDER BY open_time DESC LIMIT 1",
        (symbol, int(ts_ms))).fetchone()


def funding_events(con: sqlite3.Connection, symbol: str, after_ms: int, upto_ms: int):
    """真实结算事件（`rate_type='settled'`），计费边界 (after, upto]。"""
    return list(con.execute(
        "SELECT funding_time,funding_rate,mark_price FROM funding_history "
        "WHERE exchange='binance' AND market='um' AND symbol=? AND rate_type='settled' "
        "AND funding_time>? AND funding_time<=? ORDER BY funding_time",
        (symbol, int(after_ms), int(upto_ms))))


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── 账户态 ───────────────────────────────────────────────────────────────────
def account_view(arm: str, con: sqlite3.Connection) -> dict:
    acc = f"paper43_{arm}"
    st = con.execute("SELECT * FROM paper_state WHERE account_id=?", (acc,)).fetchone()
    cash = float(st["cash"]) if st else L.INIT_EQUITY
    return {"account_id": acc, "arm": arm, "cash": cash,
            "realized_pnl": float(st["realized_pnl"]) if st else 0.0,
            "fees_paid": float(st["fees_paid"]) if st else 0.0,
            "funding_paid": float(st["funding_paid"]) if st else 0.0,
            "initial_equity": L.INIT_EQUITY}


def open_positions(con: sqlite3.Connection, arm: str):
    """当前**有效**未平仓持仓。

    `voided=1` 的行是**定向纠错**标记（不属于本轮 paper epoch，见 `paper_correction`）：
    原审计行保留，但**不参与**估值、退出扫描与容量计算。
    """
    acc = f"paper43_{arm}"
    return list(con.execute(
        "SELECT * FROM paper_position WHERE account_id=? AND status='open' "
        "AND COALESCE(voided,0)=0 ORDER BY position_id",
        (acc,)))


def _set_cash(con, acc, **delta):
    st = con.execute("SELECT * FROM paper_state WHERE account_id=?", (acc,)).fetchone()
    cash = float(st["cash"]) if st else L.INIT_EQUITY
    rp = float(st["realized_pnl"]) if st else 0.0
    fp = float(st["fees_paid"]) if st else 0.0
    fd = float(st["funding_paid"]) if st else 0.0
    cash += delta.get("cash", 0.0)
    rp += delta.get("realized", 0.0)
    fp += delta.get("fees", 0.0)
    fd += delta.get("funding", 0.0)
    con.execute("INSERT INTO paper_state(account_id,cash,realized_pnl,fees_paid,funding_paid,"
                "updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
                "cash=excluded.cash, realized_pnl=excluded.realized_pnl, "
                "fees_paid=excluded.fees_paid, funding_paid=excluded.funding_paid, "
                "updated_at=excluded.updated_at",
                (acc, cash, rp, fp, fd, L.now_ms()))
    return cash


# ── 估值（已批准口径：15m 合约最后收盘）──────────────────────────────────────
def mark_position(con_mkt: sqlite3.Connection, pos) -> dict:
    row = last_closed_15m(con_mkt, pos["symbol"])
    if row is None:
        return {"mark": None, "mark_source": "missing_closed_15m", "mark_time": None}
    mark = _f(row["close"])
    return {"mark": mark, "mark_source": "closed_15m_close", "mark_time": int(row["close_time"])}


def unrealized(con_mkt, pos):
    m = mark_position(con_mkt, pos)
    if m["mark"] is None:
        return None, m
    qty = float(pos["qty"])
    entry = float(pos["entry_price"])
    direction = pos["direction"] if "direction" in pos.keys() else "long"
    if direction == "short":
        upnl = (entry - m["mark"]) * qty
    else:
        upnl = (m["mark"] - entry) * qty
    return upnl, m


# ── 风控闸门 ─────────────────────────────────────────────────────────────────
def pre_open_check(con, arm, symbol, notional, stop_distance, qty, con_mkt, params) -> tuple[bool, str]:
    acc = f"paper43_{arm}"
    if not params.get("short_enabled", False):
        pass  # 只多：调用方只允许 long
    eq = equity(con, arm, con_mkt, params)
    pos = open_positions(con, arm)
    if len(pos) >= int(params["max_positions"]):
        return False, "max_positions_reached"
    if eq["equity"] is None:
        return False, "equity_unavailable"
    e = eq["equity"]
    if notional > float(params["single_name_notional_equity_cap"]) * e + 1e-9:
        return False, "single_name_cap_exceeded"
    gross = eq["gross_notional"] + notional
    if gross > float(params["gross_long_notional_equity_cap"]) * e + 1e-9:
        return False, "gross_long_cap_exceeded"
    free = eq["cash"]
    need_margin = notional / float(params["leverage"])
    if free - need_margin < float(params["cash_or_free_margin_reserve_fraction"]) * e - 1e-9:
        return False, "cash_reserve_breach"
    # 组合初始止损风险：Σ(止损距离×数量) / 权益（与已批准 cap 口径一致）
    risk_frac = (eq["open_risk_abs"] + stop_distance * qty) / max(e, 1e-9)
    if risk_frac > float(params["portfolio_initial_stop_risk_cap"]) + 1e-9:
        return False, "portfolio_stop_risk_cap_exceeded"
    if eq["drawdown_locked"]:
        return False, "account_drawdown_locked"
    # 本轮**持续锁定**（修复 D02）：一旦触发过 20% 回撤，即使权益回升也**不再开仓**
    if drawdown_lock_state(arm).get("locked"):
        return False, "account_drawdown_locked_for_run"
    return True, "ok"


def equity(con, arm, con_mkt, params, peak_ref=None) -> dict:
    av = account_view(arm, con)
    pos = open_positions(con, arm)
    upnl_sum = 0.0
    gross = 0.0
    open_risk_abs = 0.0
    missing = 0
    for p in pos:
        u, m = unrealized(con_mkt, p)
        if u is None:
            missing += 1
        else:
            upnl_sum += u
        notional = float(p["qty"]) * float(p["entry_price"])
        gross += notional
        sp = p["stop_price"]
        if sp is not None:
            # 组合止损风险口径与已批准 `apply_order_rounding` 一致：**止损距离 × 数量**
            dist = abs(float(p["entry_price"]) - float(sp))
            open_risk_abs += dist * float(p["qty"])
    eq = av["cash"] + upnl_sum
    peak = _peak_equity(con, arm, eq)
    dd = 0.0 if peak <= 0 else max(0.0, (peak - eq) / peak)
    return {"equity": eq, "cash": av["cash"], "gross_notional": gross,
            "unrealized": upnl_sum, "open_risk_abs": open_risk_abs,
            "open_risk_frac": (open_risk_abs / eq) if eq > 0 else 0.0,
            "positions": len(pos),
            "positions_missing_mark": missing,
            "peak_equity": peak, "drawdown": dd,
            "drawdown_locked": dd >= float(params["maximum_account_drawdown_fraction"]),
            "initial_equity": av["initial_equity"]}


def _peak_equity(con, arm, current):
    acc = f"paper43_{arm}"
    r = con.execute("SELECT MAX(equity) FROM paper_equity WHERE account_id=?", (acc,)).fetchone()
    peak = float(r[0]) if r and r[0] is not None else L.INIT_EQUITY
    return max(peak, current)


def _record_equity(con, arm, eq_row):
    acc = f"paper43_{arm}"
    ts = L.now_ms()
    con.execute("INSERT OR REPLACE INTO paper_equity(ts,account_id,cash,unrealized,equity) "
                "VALUES(?,?,?,?,?)",
                (ts, acc, eq_row["cash"], eq_row["unrealized"], eq_row["equity"]))


# ── 真实首次请求锚（重试不刷新）──────────────────────────────────────────────
def first_request_anchor(arm: str, signal_uid, default_ms) -> int:
    """返回该信号**真实首次请求墙钟**（毫秒）。

    2026-10-14 09 契约：`order_request_ms = max(信号事件/可用时刻, **真实首次请求**)`；
    **每轮重试不得刷新首次请求**（否则成交 bar 会随重试时钟往前漂、等价于回填旧价）。
    实现：独立表 `paper_request_anchor` 用 `INSERT OR IGNORE` 只落**第一次**的值。
    `signal_uid` 为空（手工请求）⇒ 无稳定事件身份，直接返回 `default_ms`（调用方一次一请求）。
    """
    if not signal_uid:
        return int(default_ms)
    acc = f"paper43_{arm}"
    con = L.paper_conn(arm)
    try:
        con.execute("INSERT OR IGNORE INTO paper_request_anchor"
                    "(account_id,event_uid,first_request_ms,created_at) VALUES(?,?,?,?)",
                    (acc, signal_uid, int(default_ms), L.now_ms()))
        con.commit()
        row = con.execute("SELECT first_request_ms FROM paper_request_anchor "
                          "WHERE account_id=? AND event_uid=?", (acc, signal_uid)).fetchone()
        return int(row[0]) if row and row[0] is not None else int(default_ms)
    finally:
        con.close()


# ── 挂单（未来待成交，由真实进程时钟推进）────────────────────────────────────
def _register_pending(con, *, arm, symbol, signal_uid, direction, atr, stop_price,
                      event_time, available_at, requested_at, earliest_fill_ms, origin,
                      epoch_ms=None, first_request_ms=None):
    """登记/复用挂单。

    **幂等键（2026-10-14 09 契约）**：优先 `(account_id, signal_uid)`；无 signal_uid
    （手工请求）时用 `(account_id, origin, symbol, first_request_ms)`。
    为什么改成按 `first_request_ms` 而不是 `requested_at`：`requested_at` 语义是
    「经济请求时刻」（可能等于信号时刻），而**真实首次请求墙钟**才是「重试不重置」的锚。
    """
    acc = f"paper43_{arm}"
    frm = int(first_request_ms if first_request_ms is not None else requested_at)
    if signal_uid:
        row = con.execute("SELECT pending_id FROM paper_pending WHERE account_id=? AND signal_uid=? "
                          "AND status='pending'", (acc, signal_uid)).fetchone()
    else:
        row = con.execute("SELECT pending_id FROM paper_pending WHERE account_id=? AND origin=? "
                          "AND symbol=? AND COALESCE(first_request_ms, requested_at)=? "
                          "AND status='pending'",
                          (acc, origin, symbol, frm)).fetchone()
    if row:
        return int(row[0])
    cur = con.execute(
        "INSERT INTO paper_pending(account_id,arm,symbol,direction,signal_uid,origin,atr,stop_price,"
        "event_time,available_at,requested_at,earliest_fill_ms,status,reason,created_at,market_source,"
        "first_request_ms,epoch_ms) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending',NULL,?,?,?,?)",
        (acc, arm, symbol, direction, signal_uid, origin,
         (None if atr is None else float(atr)),
         (None if stop_price is None else float(stop_price)),
         (None if event_time is None else int(event_time)),
         (None if available_at is None else int(available_at)),
         int(requested_at), int(earliest_fill_ms), L.now_ms(), MARKET_SOURCE,
         frm, (None if epoch_ms is None else int(epoch_ms))))
    con.commit()
    return int(cur.lastrowid)


def _resolve_pending(con, pending_id, status, reason, *, fill_position_id=None, T=None):
    if pending_id is None:
        return
    con.execute("UPDATE paper_pending SET status=?, reason=?, resolved_at=?, fill_position_id=? "
                "WHERE pending_id=?",
                (status, reason, int(T or L.now_ms()), fill_position_id, int(pending_id)))
    con.commit()


def pending_orders(arm: str, *, status="pending"):
    con = L.paper_conn(arm, ro=True)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM paper_pending WHERE account_id=? AND status=? ORDER BY earliest_fill_ms",
            (f"paper43_{arm}", status))]
    except Exception:                                   # noqa: BLE001
        return []
    finally:
        con.close()


def advance_pending(*, T=None, log=print) -> dict:
    """用**真实进程时钟**推进挂单：到点才成交；没到点继续等；错过了就拒单（不回填）。"""
    T = int(T) if T else L.now_ms()
    out = {"filled": [], "rejected": [], "still_pending": 0, "by_arm": {}}
    for arm in L.ARMS:
        rows = pending_orders(arm)
        n_f = n_r = 0
        for row in rows:
            atr = row.get("atr")
            if atr is None and row.get("stop_price") is None:
                con = L.paper_conn(arm)
                try:
                    _resolve_pending(con, int(row["pending_id"]), "rejected",
                                     "missing_atr_and_stop", T=T)
                finally:
                    con.close()
                n_r += 1
                out["rejected"].append({"pending_id": row["pending_id"],
                                        "reason": "missing_atr_and_stop"})
                continue
            res = open_position(
                arm, row["symbol"], signal_uid=row.get("signal_uid"),
                direction=row.get("direction") or "long",
                atr=(None if atr is None else float(atr)),
                stop_price=(None if row.get("stop_price") is None else float(row["stop_price"])),
                now_ms=T, event_time=row.get("event_time"), available_at=row.get("available_at"),
                requested_at=int(row["requested_at"]), origin=row.get("origin") or "signal",
                pending_id=int(row["pending_id"]),
                # 09 契约：挂单推进必须沿用**登记时**的 epoch 与**真实首次请求墙钟**，
                # 不得因本轮推进时钟而重置（重试不刷新首次请求）。
                epoch_ms=(None if row.get("epoch_ms") is None else int(row["epoch_ms"])),
                first_request_ms=(None if row.get("first_request_ms") is None
                                  else int(row["first_request_ms"])))
            if res.get("ok"):
                n_f += 1
                out["filled"].append(res)
            elif res.get("reason") == "pending_future_fill":
                out["still_pending"] += 1
            else:
                n_r += 1
                out["rejected"].append({"pending_id": row["pending_id"],
                                        "reason": res.get("reason")})
        out["by_arm"][arm] = {"pending_total": len(rows), "filled": n_f, "rejected": n_r,
                              "still_pending": len(rows) - n_f - n_r}
    log(f"[paper] 挂单推进 filled={len(out['filled'])} rejected={len(out['rejected'])} "
        f"still_pending={out['still_pending']} by_arm={out['by_arm']}")
    return out


# ── 开仓 ─────────────────────────────────────────────────────────────────────
def open_position(arm: str, symbol: str, *, signal_uid=None, direction="long",
                  atr=None, stop_price=None, now_ms=None, event_time=None,
                  available_at=None, requested_at=None, signal_payload=None,
                  origin="signal", pending_id=None, epoch_ms=None,
                  first_request_ms=None, entry_mode=None, stop_mode=None,
                  struct_low=None, trailing_mode=None) -> dict:
    """按已批准模型开一笔**模拟多仓**。返回结果 dict（含 ok/reason/明细）。

    成交时钟见模块头 `FILL_POLICY`。要点：
      · 成交价 = **第一根 `open_time >= max(信号 event_time, 信号 available_at, 真实首次请求墙钟)
        + 15m` 的已闭合 15m bar 的 open**；手工请求无信号时刻 ⇒ 以**真实请求挂钟**为经济请求时刻；
      · **成交绝不早于**请求/信号可用时刻（不再锚定「最近一根已闭合 bar」的旧价）；
      · 合格 bar 尚不存在 ⇒ 登记**挂单**（`paper_pending`），等真实时钟到点；
      · 发现时已错过成交时刻（超过 `MAX_FILL_LAG_MS`）⇒ 拒单，**不回填旧价**；
      · **本轮 paper epoch 门禁（2026-10-14 09 契约）**：信号确认 bar 的**真实右边界**
        早于 epoch ⇒ 拒单 `before_paper_epoch_no_backfill`（**不搬到 09:00 补开**）；
        成交 bar 早于 epoch ⇒ 同样拒单（双保险）。
    `now_ms` = **本次尝试时刻**（挂钟）；`requested_at` = **真实首次请求时刻**（挂单重试时沿用原值，
    用于审计与「手工请求」的经济请求时刻；缺省 = 本次尝试时刻）。
    `epoch_ms` = 本轮 paper epoch（缺省取 `live_store.get_paper_epoch_ms()`）。
    """
    params = approved_params()
    # ── 方向归一（任务二重点修复：**绝不**把缺失/非法方向默认成 long）─────────────
    # 旧实现只判 `direction not in ("long","short")` 后直接继续；上游若把 short 写成
    # long（或方向缺失），这里无从发现 ⇒ 变成"静默做多"。现改为：归一失败**显式拒单**，
    # 且 short 在 v3.1 只多口径下**拒单**（不翻转方向）。
    norm_dir = (EB.normalize_direction(direction) if EB.CORE_AVAILABLE
                else (direction if direction in ("long", "short") else None))
    if norm_dir is None:
        return _reject(arm, symbol, signal_uid, f"illegal_direction_{direction}")
    direction = norm_dir
    if direction == "short" and not params.get("short_enabled", False):
        return _reject(arm, symbol, signal_uid, "short_disabled_by_frozen_view_only_long")
    # ── 模式校验 + 结构低点必需性（缺结构低点**不得**退回固定止损）──────────────
    if EB.CORE_AVAILABLE:
        mode_errs = EB.validate_modes(entry_mode=entry_mode or "three_buy_points",
                                      stop_mode=stop_mode or "uniform",
                                      trail_mode=trailing_mode or "none",
                                      direction=direction)
        if mode_errs:
            return _reject(arm, symbol, signal_uid, mode_errs[0])
    if stop_mode in ("structural", "structural_m2b"):
        _sl = None if struct_low is None else _f(struct_low)
        if _sl is None or not (_sl == _sl):            # None 或 NaN
            return _reject(arm, symbol, signal_uid, "missing_struct_low_for_structural_stop")
    # ── 服务端新开仓总闸（阶段A·fail-closed）────────────────────────────────────
    # 覆盖自动 / 手工 / 挂单推进三条路径（它们**都**经由本函数）。
    # 退出、费用、资金费、健康采集与只读 API 不经过此处 ⇒ 不受影响。
    _gate = new_entry_gate()
    if not _gate["allow_new_entries"]:
        if pending_id is not None:
            _con_g = L.paper_conn(arm)
            try:
                _resolve_pending(_con_g, int(pending_id), "rejected", GATE_REASON,
                                 T=int(now_ms or L.now_ms()))
            finally:
                _con_g.close()
        r = _reject(arm, symbol, signal_uid, GATE_REASON)
        r.update({"gate": _gate,
                  "note": "该模拟盘尚未通过闭环验收 ⇒ 服务端拒绝一切新开仓"
                          "（自动/手工/挂单三路一致）。退出、费用、资金费与健康采集不受影响；"
                          "本闸为可回滚控制文件，验收通过后由受控启用时刻放行，绝不补开历史信号。"})
        return r
    con = L.paper_conn(arm)
    try:
        con_mkt = L.market_conn()
    except Exception:                                   # noqa: BLE001
        # **行情库不可用 ⇒ 可重试拒单**（2026-10-14 加固）：不写 `paper_processed`、
        # 不落终态（`market_db_unavailable` 不在终态清单内）⇒ 行情库恢复后下一轮自动重试，
        # 信号**不会**被静默丢弃，也**不会**用任何旧价回填。
        con.close()
        return _reject(arm, symbol, signal_uid, "market_db_unavailable")
    try:
        attempt_ms = int(now_ms or L.now_ms())
        req_ms = int(requested_at) if requested_at is not None else attempt_ms
        epoch = int(epoch_ms if epoch_ms is not None
                    else L.get_paper_epoch_ms())
        # **真实首次请求墙钟**：显式传入优先；否则用**持久锚**（`INSERT OR IGNORE`，
        # 只落第一次）—— 保证**每轮重试不刷新首次请求**（09 契约硬要求）。
        if first_request_ms is not None:
            first_req = int(first_request_ms)
        elif signal_uid:
            con.execute("INSERT OR IGNORE INTO paper_request_anchor"
                        "(account_id,event_uid,first_request_ms,created_at) VALUES(?,?,?,?)",
                        (f"paper43_{arm}", signal_uid, req_ms, L.now_ms()))
            con.commit()
            _arow = con.execute("SELECT first_request_ms FROM paper_request_anchor "
                                "WHERE account_id=? AND event_uid=?",
                                (f"paper43_{arm}", signal_uid)).fetchone()
            first_req = int(_arow[0]) if _arow and _arow[0] is not None else req_ms
        else:
            first_req = req_ms
        # ── epoch 门禁 ①：信号确认 bar 的真实右边界早于 epoch ⇒ 拒单（不补开）────────
        if event_time is not None or available_at is not None:
            sig_right = L.signal_bar_right_ms(
                max(int(x) for x in (event_time, available_at) if x is not None))
            if sig_right < epoch:
                r = _reject(arm, symbol, signal_uid, "before_paper_epoch_no_backfill")
                r.update({"signal_bar_right_ms": sig_right, "epoch_ms": epoch,
                          "note": "该信号确认时刻早于本轮 paper 启用下限 ⇒ 拒单；"
                                  "不搬到启用时刻补开，也不用旧价回填。"})
                return r
        # 幂等：同一 signal_uid 只开一次
        if signal_uid:
            if con.execute("SELECT 1 FROM paper_processed WHERE signal_uid=?", (signal_uid,)).fetchone():
                return {"ok": False, "reason": "duplicate_signal_uid", "signal_uid": signal_uid}
        # 同币已有持仓 → 不加仓（add_positions_enabled=false）
        if con.execute("SELECT 1 FROM paper_position WHERE account_id=? AND symbol=? AND status='open' "
                       "AND COALESCE(voided,0)=0",
                       (f"paper43_{arm}", symbol)).fetchone():
            _resolve_pending(con, pending_id, "rejected", "position_already_open_no_add", T=attempt_ms)
            return _reject(arm, symbol, signal_uid, "position_already_open_no_add")
        delay_ms = int(params["signal_execution_delay_minutes"]) * MIN
        # ① 经济请求时刻 = max(信号事件/可用时刻, **真实首次请求墙钟**)
        #    （2026-10-14 09 契约：不得只用信号时刻 —— 那会让成交 bar 早于系统真实请求时刻）
        eco = [int(x) for x in (event_time, available_at) if x is not None]
        order_request_ms = max(eco + [first_req, req_ms])
        # ② 最早可成交时刻（已批准延迟规则，逐字保留）
        earliest_fill_ms = order_request_ms + delay_ms
        # ③ 第一根合格已闭合 15m bar
        bar = bar_at_or_after(con_mkt, symbol, earliest_fill_ms)
        if bar is None:
            pid = _register_pending(con, arm=arm, symbol=symbol, signal_uid=signal_uid,
                                    direction=direction, atr=atr, stop_price=stop_price,
                                    event_time=event_time, available_at=available_at,
                                    requested_at=req_ms, earliest_fill_ms=earliest_fill_ms,
                                    origin=origin, epoch_ms=epoch, first_request_ms=first_req)
            return {"ok": False, "reason": "pending_future_fill", "pending_id": pid,
                    "symbol": symbol, "signal_uid": signal_uid, "origin": origin,
                    "requested_at": req_ms, "first_request_ms": first_req,
                    "order_request_ms": order_request_ms,
                    "earliest_fill_ms": earliest_fill_ms, "epoch_ms": epoch,
                    "note": "应成交时刻尚未到达（第一根合格已闭合 15m bar 还不存在）"
                            "⇒ 登记挂单，由真实进程时钟推进；不提前、不回填。"}
        bar_open = int(bar["open_time"])
        # ── epoch 门禁 ②：成交 bar 早于 epoch ⇒ 拒单（双保险）────────────────────
        if bar_open < epoch:
            _resolve_pending(con, pending_id, "rejected", "before_paper_epoch_no_backfill",
                             T=attempt_ms)
            r = _reject(arm, symbol, signal_uid, "before_paper_epoch_no_backfill")
            r.update({"bar_open_time": bar_open, "epoch_ms": epoch,
                      "note": "成交 bar 早于本轮 paper 启用下限 ⇒ 拒单；不回填。"})
            return r
        # 时钟自洽：成交 bar 不得晚于**本次尝试时刻**（否则就是用未来价成交 ⇒ 前视）。
        # 生产路径 `attempt_ms = L.now_ms()`，已闭合 bar 必然 ≤ now；此断言防的是
        # 「用模拟/回放的 now 调进来」时的静默前视。
        if bar_open > attempt_ms:
            return _reject(arm, symbol, signal_uid, "bar_after_attempt_clock_no_lookahead")
        # ⑤ 错过成交时刻 ⇒ 拒单（**绝不**用旧价回填）
        lag_ms = attempt_ms - bar_open
        if lag_ms > MAX_FILL_LAG_MS:
            _resolve_pending(con, pending_id, "rejected", "missed_fill_moment_no_backfill",
                             T=attempt_ms)
            r = _reject(arm, symbol, signal_uid, "missed_fill_moment_no_backfill")
            r.update({"bar_open_time": bar_open, "requested_at": req_ms,
                      "order_request_ms": order_request_ms,
                      "earliest_fill_ms": earliest_fill_ms, "lag_ms": lag_ms,
                      "market_source": MARKET_SOURCE,
                      "note": "已错过应成交时刻（滞后超过容忍值）⇒ 拒单；"
                              "该信号仍照常展示，但不自动回填旧价。"})
            return r
        raw = _f(bar["open"])
        if raw is None or raw <= 0:
            return _reject(arm, symbol, signal_uid, "bad_bar_open")
        if atr is None and stop_price is None:
            return _reject(arm, symbol, signal_uid, "missing_atr_and_stop")
        slip = float(params["slippage_each_side"])
        fee_rate = float(params["fee_each_side"])
        stop_mult = float(params["uniform_stop_atr_mult"])
        # ── 仓位规模 + **合约规格实际取整**（2026-10-14 09 契约核对修正）──────────────
        # 缺陷：原实现算完 `qty = min(caps)` 就**直接使用**，从未调用冻结的
        # `apply_order_rounding` ⇒ 「规格 CSV 存在却没被调用」即报通过（禁止项）。
        # 修法：调用**冻结的** `wx_execution.apply_order_rounding`（与回测同一函数、同一规格
        # CSV），一次完成：① 买价按价格步进**向上**取整（更贵=更保守）；② 保护价按步进
        # **向下**取整；③ 数量按步进**向下**取整；④ 最小量/最大量/最小订单金额复核；
        # ⑤ 各风险上限复核（风险预算、单名 10%、总多 80%、现金 20% 预留、参与率、
        # 组合初始止损风险 3%）。任一不满足 ⇒ **整单拒单**（不凑单、不向上加仓到最小单）。
        limits = contract_limits(symbol)
        if limits is None:
            return _reject(arm, symbol, signal_uid, "missing_contract_spec")
        eq = equity(con, arm, con_mkt, params)
        if eq["equity"] is None:
            return _reject(arm, symbol, signal_uid, "equity_unavailable")
        e = eq["equity"]
        # 参与率用**上一根**已闭合 15m 的 quote 成交额（入场 bar 自身在决策时刻尚未收盘，
        # 引用它即前视）。上限从生效视图读，不再硬编码。
        prev_bar = bar_before(con_mkt, symbol, bar_open)
        pqv = _f(prev_bar["quote_volume"]) if (prev_bar is not None
                                               and "quote_volume" in prev_bar.keys()) else None
        part = float(params.get("entry_participation_previous_15m_quote_cap") or 0.0)
        # 无 ATR 但给了显式止损 ⇒ 反推等效 ATR，使冻结取整函数的止损距离与原意一致
        atr_eff = (float(atr) if atr is not None
                   else max(0.0, raw * (1.0 + slip) - float(stop_price)) / max(stop_mult, 1e-9))
        rr = _apply_order_rounding(
            raw_open=raw, atr=atr_eff, limits=limits,
            risk_per_trade_fraction=float(params["risk_per_trade_equity_fraction"]),
            equity=e, stop_atr_mult=stop_mult, slippage_each_side=slip,
            gross_open_notional=eq["gross_notional"], free_cash=eq["cash"],
            leverage=float(params["leverage"]),
            cash_reserve_fraction=float(params["cash_or_free_margin_reserve_fraction"]),
            single_name_notional_cap=float(params["single_name_notional_equity_cap"]),
            gross_long_notional_cap=float(params["gross_long_notional_equity_cap"]),
            prev_quote_volume=pqv, participation_cap=part,
            portfolio_risk_open=eq["open_risk_abs"],
            portfolio_risk_cap=float(params["portfolio_initial_stop_risk_cap"]))
        if rr.get("reject"):
            return _reject(arm, symbol, signal_uid, f"rounding_{rr['reject']}")
        fill = float(rr["entry_price"])
        if direction == "short":
            stop = (float(stop_price) if stop_price is not None
                    else _round_protect_price(fill + stop_mult * float(atr_eff), float(limits["price_step"] or 0.0)))
            stop_dist = stop - fill
        else:
            stop = (float(rr["stop_price"]) if stop_price is None
                    else _round_protect_price(float(stop_price), float(limits["price_step"] or 0.0)))
            stop_dist = fill - stop
        if stop_dist <= 0:
            return _reject(arm, symbol, signal_uid, "invalid_stop_reject_trade")
        qty = float(rr["qty"])
        notional = float(rr["notional"])
        # 附加闸门（`apply_order_rounding` 不含）：**持仓数上限 10** 与 **DD 20% 锁止**，
        # 以及取整后名义的现金预留/单名/总多复核（`pre_open_check` 逐条）。
        ok, why = pre_open_check(con, arm, symbol, notional, stop_dist, qty, con_mkt, params)
        if not ok:
            return _reject(arm, symbol, signal_uid, why)
        fee = notional * fee_rate
        slip_cost = qty * (fill - raw)
        # **经济入场时刻 = 入场 bar 的开盘时刻**（不是挂钟 `attempt_ms`）。
        # 为什么必须这样：① 退出扫描 `closed_15m(sym, entry_ts, T)` 覆盖区间；
        # ② 资金费计费窗 `(entry_ts, exit_ts]` 起点；③ `max_holding_hours=720` 的计时起点。
        # 挂钟记账时刻保留在 `paper_order.ts` / `paper_fill.fill_ts`（审计用）。
        entry_bar_ts = bar_open
        con.execute("INSERT INTO paper_position(account_id,symbol,qty,entry_price,entry_ts,"
                    "status,exit_ts,exit_reason,signal_uid,arm,stop_price,atr_at_entry,"
                    "entry_fee,model_version,event_time,available_at,requested_at,"
                    "earliest_fill_ms,market_source,origin,first_request_ms,epoch_ms) "
                    "VALUES(?,?,?,?,?,'open',NULL,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"paper43_{arm}", symbol, qty, fill, entry_bar_ts, signal_uid, arm, stop, atr,
                     fee, MODEL_VERSION,
                     (None if event_time is None else int(event_time)),
                     (None if available_at is None else int(available_at)),
                     req_ms, earliest_fill_ms, MARKET_SOURCE, origin, first_req, epoch))
        pid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        # `paper_order.event_uid` 是 UNIQUE：同一 signal_uid 若先前被**拒绝**过
        # （`_reject` 写了一条 status='rejected' 的行），此处若用普通 INSERT 会撞唯一约束
        # （实测 `IntegrityError`）。同一 event_uid 的最终事实是「已成交」，
        # 故用 OR REPLACE 覆盖该事件行；拒单审计仍保留在 `paper_blocked`。
        con.execute("INSERT OR REPLACE INTO paper_order(account_id,event_uid,symbol,side,qty,price,"
                    "fee,ts,status,reject_reason,fill_price,notional,bar_open_time,model_version,"
                    "event_time,requested_at,market_source,first_request_ms,epoch_ms) "
                    "VALUES(?,?,?,'buy',?,?,?,?,'filled',NULL,?,?,?,?,?,?,?,?,?)",
                    (f"paper43_{arm}", signal_uid or f"manual:{req_ms}", symbol, qty, fill, fee,
                     attempt_ms, fill, notional, bar_open, MODEL_VERSION,
                     (None if event_time is None else int(event_time)), req_ms, MARKET_SOURCE,
                     first_req, epoch))
        oid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute("INSERT INTO paper_fill(account_id,order_id,position_id,signal_uid,symbol,"
                    "action,qty,raw_price,fill_price,notional,fee,slippage_cost,bar_open_time,"
                    "bar_close_time,fill_ts,model_version,event_time,available_at,requested_at,"
                    "market_source,first_request_ms,epoch_ms) "
                    "VALUES(?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"paper43_{arm}", oid, pid, signal_uid, symbol, qty, raw, fill, notional, fee,
                     slip_cost, bar_open, int(bar["close_time"]), attempt_ms, MODEL_VERSION,
                     (None if event_time is None else int(event_time)),
                     (None if available_at is None else int(available_at)),
                     req_ms, MARKET_SOURCE, first_req, epoch))
        _set_cash(con, f"paper43_{arm}", cash=-fee, fees=fee)
        if signal_uid:
            con.execute("INSERT OR REPLACE INTO paper_processed(signal_uid,arm,action,detail,ts) "
                        "VALUES(?,?,?,?,?)",
                        (signal_uid, arm, "open", json.dumps({"position_id": pid},
                                                             ensure_ascii=False), attempt_ms))
        _resolve_pending(con, pending_id, "filled", None, fill_position_id=pid, T=attempt_ms)
        eq2 = equity(con, arm, con_mkt, params)
        _record_equity(con, arm, eq2)
        con.commit()
        return {"ok": True, "reason": "filled", "position_id": pid, "order_id": oid,
                "qty": qty, "entry_price": fill, "raw_price": raw, "stop_price": stop,
                "notional": notional, "fee": fee, "slippage_cost": slip_cost,
                "bar_open_time": bar_open, "bar_close_time": int(bar["close_time"]),
                "model_version": MODEL_VERSION, "signal_uid": signal_uid,
                "origin": origin, "event_time": event_time, "available_at": available_at,
                "requested_at": req_ms, "earliest_fill_ms": earliest_fill_ms,
                "filled_at": bar_open, "fill_lag_ms": lag_ms, "market_source": MARKET_SOURCE}
    finally:
        con.close()
        con_mkt.close()


def _reject(arm, symbol, signal_uid, reason):
    con = L.paper_conn(arm)
    try:
        # 幂等：同一 event_uid 重复拒单不报错（`event_uid` 有 UNIQUE 约束）
        con.execute("INSERT OR IGNORE INTO paper_order(account_id,event_uid,symbol,side,qty,price,"
                    "fee,ts,status,reject_reason) VALUES(?,?,?,'buy',NULL,NULL,NULL,?,"
                    "'rejected',?)",
                    (f"paper43_{arm}", signal_uid or f"reject:{L.now_ms()}", symbol,
                     L.now_ms(), reason))
        # 拒单去重：常驻 worker 每轮都会重试同一批信号，若不做预检，
        # `paper_blocked` 会随轮次无限增长（86 条/轮 × 288 轮/天）。
        # 同一 (账户, 事件, 原因) 只留**首次**一条；拒单是**可重试**的
        # （如 `max_positions_reached` 会在持仓平掉后可开），故只去重、不落终态。
        # 命中既有重复行时不删除（历史审计原样保留）。
        dup = con.execute("SELECT 1 FROM paper_blocked WHERE account_id=? AND event_uid=? "
                          "AND reason=? LIMIT 1",
                          (f"paper43_{arm}", signal_uid or "", reason)).fetchone()
        if not dup:
            con.execute("INSERT INTO paper_blocked(account_id,event_uid,symbol,reason,ts) "
                        "VALUES(?,?,?,?,?)",
                        (f"paper43_{arm}", signal_uid or "", symbol, reason, L.now_ms()))
        con.commit()
    finally:
        con.close()
    return {"ok": False, "reason": reason, "symbol": symbol, "signal_uid": signal_uid}


# ── 资金费：**结算事件经过时即时记账**（幂等）────────────────────────────────
# 只读复核 #4：原实现只在**平仓时**把整段资金费一次补成总额；「持仓不足一个周期所以 0」
# 并不能证明正负真实费率算对。修正：每个真实结算事件**经过时**（事件时刻 ≤ 真实进程时钟）
# 就逐笔记账并更新现金/权益；平仓只补记 `(上次已记, exit_ts]` 的剩余事件。
#   · 费率/标记价：`funding_history` 真实结算事件（`rate_type='settled'`）的
#     `funding_rate` 与 `mark_price`，**正负原样**；
#   · 基数：**持仓数量 × 该事件真实 mark**（不是 5000U 本金乘费率）；
#   · **不固定 8h/4h、不补 0、不倒填、不除以任何周期**；缺真实事件 ⇒ 如实留缺口。
#
# ── 2026-10-14 模拟实盘闭环专项·订正（06 任务 3 第 3 点）──────────────────────
# 原病灶：`_settle_funding` 用 `lo = MAX(paper_funding.funding_time)` 作**单向游标**，
#   只查 `(lo, upto]`。若某笔更早时刻 `T_early < T_last` 的结算事件因接口延迟
#   **后来才补到行情库**，该游标会把它**永久跳过**；且 `accrue_funding` 只遍历
#   `open` 持仓 ⇒ 已平仓后补到的迟到事件**无任何路径**补记。
# 订正实现：
#   ① 废除单向游标；对每个持仓（**含 open 与 closed**）取**完整存续窗口**
#      `(entry_ts, exit_ts_or_now]` 内的**全部真实结算事件集合 E_truth**，
#      与库内该 `position_id` 已入账集合 E_charged 做**集合差集** `E_missing = E_truth \ E_charged`；
#   ② 按唯一事件身份 `(account_id, position_id, funding_time)` **幂等**补记；
#   ③ 持仓仍 `open` ⇒ 直接补记并同步账户现金；
#      持仓已 `closed` ⇒ 写**迟到补记**行（`late_backfill=1`）并**一致修正**
#      现金 / 资金费累计 / 已实现净盈亏，同时写 `paper_funding_correction` 审计；
#   ④ 缺真实 `mark`/`rate` ⇒ 写 `paper_funding_gap` 留缺口，**不补 0 冒完整**。
#
# ── 金额符号口径（**显式统一，防反扣**）──────────────────────────────────────
#   `amount`（`paper_funding.amount`）= **费用支出口径**：
#       amount > 0 ⇒ 账户**支付**资金费（支出）；amount < 0 ⇒ 账户**收取**资金费。
#       amount = + qty × mark_T × fundingRate      （正负原样，不做任何缩放）
#   账户现金变动：ΔCash = −amount。
#   对账器通过 `amount_sign_convention="expense_positive"` 统一转换，
#   **严禁**把支出正数与现金变动负数混写。
AMOUNT_SIGN_CONVENTION = "expense_positive"


def _funding_truth(con_mkt, symbol, lo_ms, hi_ms):
    """完整窗口 `(lo_ms, hi_ms]` 内的**全部**真实结算事件（无游标截断）。"""
    return funding_events(con_mkt, symbol, lo_ms, hi_ms)


def _charged_funding_times(con, acc, position_id) -> set:
    """库内该持仓已入账的**真实事件身份集合**（`funding_time` 是事件身份的一部分）。"""
    return {int(r[0]) for r in con.execute(
        "SELECT funding_time FROM paper_funding WHERE account_id=? AND position_id=?",
        (acc, int(position_id)))}


def _settle_funding(con, con_mkt, arm, *, position_id, symbol, qty, entry_ts, upto_ms,
                    late=False):
    """把完整存续窗口 `(entry_ts, upto_ms]` 的真实结算事件与已入账集合做**差集**后补记。

    返回 `(新增合计 amount, 新增事件数, 新增明细, 缺口明细)`。
    已记过的事件不重复入账（集合差集 + 唯一索引双保险）。
    """
    acc = f"paper43_{arm}"
    charged = _charged_funding_times(con, acc, position_id)
    new_sum = 0.0
    n_new = 0
    detail, gaps = [], []
    for f in _funding_truth(con_mkt, symbol, int(entry_ts), int(upto_ms)):
        ft = int(f["funding_time"])
        if ft in charged:
            continue                                   # 事件身份已入账 ⇒ 跳过（幂等）
        rate = _f(f["funding_rate"])
        mark = _f(f["mark_price"])
        if rate is None or mark is None:
            # 缺真实费率或真实 mark ⇒ **不补 0 冒完整**，写缺口留痕并跳过
            reason = "missing_real_rate_or_mark"
            con.execute("INSERT OR IGNORE INTO paper_funding_gap(account_id,position_id,"
                        "funding_time,reason,detected_at) VALUES(?,?,?,?,?)",
                        (acc, int(position_id), ft, reason, L.now_ms()))
            gaps.append({"funding_time": ft, "reason": reason,
                         "funding_rate": rate, "mark_price": mark})
            continue
        amt = float(qty) * mark * rate                 # 支出口径：>0 支付、<0 收取
        cur = con.execute(
            "INSERT OR IGNORE INTO paper_funding(account_id,position_id,funding_time,rate,mark,"
            "amount,source,late_backfill,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (acc, int(position_id), ft, rate, mark, amt, FUNDING_SOURCE,
             (1 if late else 0), L.now_ms()))
        if cur.rowcount == 0:                          # 唯一索引挡下 ⇒ 已入账
            continue
        new_sum += amt
        n_new += 1
        detail.append({"funding_time": ft, "funding_rate": rate, "mark_price": mark,
                       "qty": float(qty), "amount": amt, "late_backfill": bool(late),
                       "recorded_at": L.now_ms()})
    return new_sum, n_new, detail, gaps


def accrue_funding(arm: str, *, upto_ms=None, log=None) -> dict:
    """按**真实进程时钟**把已发生的结算事件逐笔记到账户（现金/权益/持仓合计同步）。

    覆盖两类持仓（06 任务 3 第 3 点）：
      · `open` 持仓：完整窗口 `(entry_ts, now]` 差集补记；
      · `closed` 持仓：完整窗口 `(entry_ts, exit_ts]` 差集补记**迟到事件**并一致修正。
    """
    con = L.paper_conn(arm)
    con_mkt = L.market_conn()
    try:
        T = int(upto_ms or L.now_ms())
        acc = f"paper43_{arm}"
        pos = [dict(p) for p in open_positions(con, arm)]
        rows = []
        total_new = 0.0
        for p in pos:
            new_sum, n_new, detail, gaps = _settle_funding(
                con, con_mkt, arm, position_id=int(p["position_id"]), symbol=p["symbol"],
                qty=float(p["qty"]), entry_ts=int(p["entry_ts"]), upto_ms=T, late=False)
            if n_new:
                pos_total = con.execute("SELECT COALESCE(SUM(amount),0) FROM paper_funding "
                                        "WHERE account_id=? AND position_id=?",
                                        (acc, int(p["position_id"]))).fetchone()[0]
                con.execute("UPDATE paper_position SET funding_paid=? WHERE position_id=?",
                            (float(pos_total), int(p["position_id"])))
                # 支出口径 amount>0 ⇒ 现金减少；amount<0 ⇒ 现金增加。ΔCash = −amount。
                _set_cash(con, acc, cash=-new_sum, funding=new_sum)
                total_new += new_sum
            rows.append({"arm": arm, "position_id": int(p["position_id"]), "symbol": p["symbol"],
                         "qty": float(p["qty"]), "entry_ts": int(p["entry_ts"]),
                         "upto_ms": T, "n_new_events": n_new,
                         "new_amount_sum": round(new_sum, 8), "events": detail,
                         "gaps": gaps})
        if rows:
            _record_equity(con, arm, equity(con, arm, con_mkt, approved_params()))
        con.commit()
        # 已平仓持仓的**迟到事件**补记（幂等；与 open 路径分离统计）
        late = backfill_closed_funding(arm, upto_ms=T, con=con, con_mkt=con_mkt)
        out = {"arm": arm, "T": T, "n_open_positions": len(pos),
               "amount_sign_convention": AMOUNT_SIGN_CONVENTION,
               "n_new_events": sum(r["n_new_events"] for r in rows),
               "new_amount_sum": round(total_new, 8), "per_position": rows,
               "closed_late_backfill": late}
        if log and (out["n_new_events"] or late.get("n_new_events")):
            log(f"[paper] 资金费结算入账 arm={arm} 在持事件={out['n_new_events']} "
                f"迟到补记={late.get('n_new_events')} 合计="
                f"{round(total_new + late.get('new_amount_sum', 0.0), 8)}")
        return out
    finally:
        con.close()
        con_mkt.close()


def backfill_closed_funding(arm: str, *, upto_ms=None, lookback_days=60,
                            con=None, con_mkt=None, log=None) -> dict:
    """已平仓持仓的**迟到/补采结算事件**补记 + 一致修正（幂等）。

    为什么必须有：`accrue_funding` 只遍历 `open` 持仓；一笔持仓平仓后，若其存续窗口内
    的某笔结算事件因接口延迟**后来才补到行情库**，原实现**没有任何路径**能补记它。

    一致修正范围（三处必须同改，否则账本自相矛盾）：
      ① 现金：`cash += −amount`；
      ② 持仓与账户的资金费累计：`paper_position.funding_paid += amount`、
         `paper_state.funding_paid += amount`；
      ③ 已实现净盈亏：`paper_realized.net_pnl −= amount`、
         `paper_realized.funding_paid += amount`、`paper_state.realized_pnl −= amount`。
    同时写 `paper_funding_correction` 审计（源事件身份/费率/mark/数量/经济时间/记账时间）。
    """
    T = int(upto_ms or L.now_ms())
    own = con is None
    if own:
        con = L.paper_conn(arm)
    own_mkt = con_mkt is None
    if own_mkt:
        con_mkt = L.market_conn()
    acc = f"paper43_{arm}"
    lo_ms = T - int(lookback_days) * 24 * H
    try:
        closed = [dict(r) for r in con.execute(
            "SELECT position_id,symbol,qty,entry_ts,exit_ts,status,entry_fee FROM paper_position "
            "WHERE account_id=? AND status='closed' AND exit_ts IS NOT NULL AND exit_ts>=? "
            "ORDER BY position_id", (acc, lo_ms))]
        per_pos, n_total, sum_total = [], 0, 0.0
        for p in closed:
            pid = int(p["position_id"])
            new_sum, n_new, detail, gaps = _settle_funding(
                con, con_mkt, arm, position_id=pid, symbol=p["symbol"], qty=float(p["qty"]),
                entry_ts=int(p["entry_ts"]), upto_ms=int(p["exit_ts"]), late=True)
            if n_new:
                # ③ 一致修正已实现净盈亏（先读旧值，再写回）
                real = con.execute("SELECT id,net_pnl,funding_paid FROM paper_realized "
                                   "WHERE account_id=? AND position_id=? ORDER BY id LIMIT 1",
                                   (acc, pid)).fetchone()
                for d in detail:
                    amt = float(d["amount"])
                    con.execute(
                        "INSERT OR IGNORE INTO paper_funding_correction(account_id,position_id,"
                        "symbol,funding_time,rate,mark,qty,amount,cash_delta,realized_delta,"
                        "late,reason,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)",
                        (acc, pid, p["symbol"], int(d["funding_time"]), d["funding_rate"],
                         d["mark_price"], d["qty"], amt, -amt, -amt,
                         "late_settled_event_after_close", L.now_ms()))
                if real is not None:
                    con.execute("UPDATE paper_realized SET net_pnl=net_pnl-?, "
                                "funding_paid=funding_paid+? WHERE id=?",
                                (new_sum, new_sum, int(real["id"])))
                pos_total = con.execute(
                    "SELECT COALESCE(SUM(amount),0) FROM paper_funding WHERE account_id=? "
                    "AND position_id=?", (acc, pid)).fetchone()[0]
                con.execute("UPDATE paper_position SET funding_paid=? WHERE position_id=?",
                            (float(pos_total), pid))
                # ΔCash = −amount；同时把 realized / funding 累计同步到账户态
                _set_cash(con, acc, cash=-new_sum, funding=new_sum, realized=-new_sum)
                n_total += n_new
                sum_total += new_sum
            per_pos.append({"position_id": pid, "symbol": p["symbol"],
                            "entry_ts": int(p["entry_ts"]), "exit_ts": int(p["exit_ts"]),
                            "n_new_events": n_new, "new_amount_sum": round(new_sum, 8),
                            "events": detail, "gaps": gaps})
        if n_total:
            _record_equity(con, arm, equity(con, arm, con_mkt, approved_params()))
        con.commit()
        out = {"arm": arm, "T": T, "lookback_days": int(lookback_days),
               "n_closed_positions_scanned": len(closed),
               "n_new_events": n_total, "new_amount_sum": round(sum_total, 8),
               "per_position": [x for x in per_pos if x["n_new_events"]],
               "amount_sign_convention": AMOUNT_SIGN_CONVENTION}
        if log and n_total:
            log(f"[paper] 迟到资金费补记 arm={arm} 事件={n_total} 合计={round(sum_total, 8)}")
        return out
    finally:
        if own_mkt:
            con_mkt.close()
        if own:
            con.close()


def audit_funding_usage(arm: str, *, amount_sign_convention=AMOUNT_SIGN_CONVENTION) -> dict:
    """资金费**符号口径与记账一致性**对账器（06 任务 3 第 2 点的强制声明点）。

    校验：
      · 声明口径 `expense_positive`（amount>0 支付、<0 收取）；
      · 账户 `funding_paid` 累计 == Σ `paper_funding.amount`（支出口径）；
      · 每条已入账事件的 `amount` 与「数量 × 真实 mark × 真实费率」逐条一致；
      · 现金恒等式 `cash = INIT + Σnet − Σopen_entry_fee − Σopen_funding_paid`；
      · 缺口（`paper_funding_gap`）与迟到补记（`late_backfill=1`）计数单列。
    """
    if amount_sign_convention != "expense_positive":
        return {"ok": False, "reason": "unsupported_amount_sign_convention",
                "got": amount_sign_convention}
    acc = f"paper43_{arm}"
    con = L.paper_conn(arm, ro=True)
    try:
        st = con.execute("SELECT * FROM paper_state WHERE account_id=?", (acc,)).fetchone()
        cash = float(st["cash"]) if st else L.INIT_EQUITY
        funding_state = float(st["funding_paid"]) if st else 0.0
        realized_state = float(st["realized_pnl"]) if st else 0.0
        cols = {r[1] for r in con.execute("PRAGMA table_info(paper_funding)")}
        late_sel = "late_backfill" if "late_backfill" in cols else "0 AS late_backfill"
        rows = [dict(r) for r in con.execute(
            "SELECT position_id,funding_time,rate,mark,amount," + late_sel + " FROM paper_funding "
            "WHERE account_id=?", (acc,))]
        bad = []
        for r in rows:
            exp = None
            q = con.execute("SELECT qty FROM paper_position WHERE position_id=?",
                            (int(r["position_id"]),)).fetchone()
            if q is not None:
                exp = float(q[0]) * float(r["mark"]) * float(r["rate"])
                if abs(exp - float(r["amount"])) > 1e-9:
                    bad.append({"position_id": r["position_id"],
                                "funding_time": r["funding_time"],
                                "expected": exp, "stored": r["amount"]})
        sum_amount = sum(float(r["amount"]) for r in rows)
        n_late = sum(1 for r in rows if int(r["late_backfill"] or 0) == 1)
        try:
            gaps = [dict(r) for r in con.execute(
                "SELECT position_id,funding_time,reason FROM paper_funding_gap WHERE account_id=?",
                (acc,))]
        except sqlite3.OperationalError:                 # 加性表尚未建（未跑过写模式）⇒ 视为 0
            gaps = []
        n_real = con.execute("SELECT COUNT(*) FROM paper_realized WHERE account_id=?",
                             (acc,)).fetchone()[0]
        net = con.execute("SELECT COALESCE(SUM(net_pnl),0) FROM paper_realized WHERE account_id=?",
                          (acc,)).fetchone()[0]
        open_fee = con.execute("SELECT COALESCE(SUM(entry_fee),0) FROM paper_position "
                               "WHERE account_id=? AND status='open'", (acc,)).fetchone()[0]
        open_fund = con.execute("SELECT COALESCE(SUM(funding_paid),0) FROM paper_position "
                                "WHERE account_id=? AND status='open'", (acc,)).fetchone()[0]
        expect_cash = L.INIT_EQUITY + float(net) - float(open_fee) - float(open_fund)
        return {
            "arm": arm, "amount_sign_convention": amount_sign_convention,
            "semantics": "amount>0 ⇒ 账户支付资金费（支出）；amount<0 ⇒ 账户收取；ΔCash = -amount",
            "n_events": len(rows), "n_late_backfill": n_late, "n_gaps": len(gaps),
            "sum_amount_expense": round(sum_amount, 10),
            "funding_paid_state": round(funding_state, 10),
            "state_equals_sum_amount": abs(funding_state - sum_amount) < 1e-9,
            "amount_formula_mismatches": bad[:20], "n_formula_mismatches": len(bad),
            "realized_pnl_state": round(realized_state, 10),
            "realized_net_sum": round(float(net), 10),
            "cash": round(cash, 10), "expected_cash": round(expect_cash, 10),
            "cash_identity_ok": abs(cash - expect_cash) < 1e-6,
            "gaps": gaps[:20], "n_realized_rows": n_real,
            "ok": (not bad) and abs(funding_state - sum_amount) < 1e-9
                  and abs(cash - expect_cash) < 1e-6,
        }
    finally:
        con.close()



# ── 平仓 ─────────────────────────────────────────────────────────────────────
def close_position(arm: str, position_id: int, *, now_ms=None, reason="manual") -> dict:
    """手工平仓：按**最近一根已闭合 15m bar 收盘**（已批准 15m 合约最后收盘口径）。"""
    con = L.paper_conn(arm)
    con_mkt = L.market_conn()
    try:
        row = None
        pos = con.execute("SELECT symbol FROM paper_position WHERE position_id=? AND account_id=?",
                          (position_id, f"paper43_{arm}")).fetchone()
        if pos:
            row = last_closed_15m(con_mkt, pos["symbol"])
    finally:
        con.close()
        con_mkt.close()
    if row is None:
        return {"ok": False, "reason": "no_closed_15m_for_exit"}
    return close_at_price(arm, position_id, raw_price=_f(row["close"]),
                          now_ms=now_ms, reason=reason,
                          bar_open_time=int(row["open_time"]),
                          bar_close_time=int(row["close_time"]),
                          # 手工平仓成交在该 bar 的**收盘** ⇒ 经济出场时刻 = close_time
                          exit_ts=int(row["close_time"]))


def close_at_price(arm: str, position_id: int, *, raw_price, now_ms=None, reason="manual",
                   bar_open_time=None, bar_close_time=None, exit_ts=None,
                   decision_context=None) -> dict:
    """按**给定原始价**平仓（stop/信号退出等确定性触发用；滑点按已批准值不利方向）。

    `exit_ts` = **经济出场时刻**（出场 bar 的 open 或 close，由调用方按口径给）。
    缺省回退到挂钟 `now_ms`。资金费计费窗 = `(entry_ts, exit_ts]`，
    因此 `exit_ts` 必须与 `entry_ts` **同一时间轴**（bar 时刻），否则会漏收/多收结算事件。
    """
    params = approved_params()
    con = L.paper_conn(arm)
    con_mkt = L.market_conn()
    try:
        now_ms = int(now_ms or L.now_ms())
        exit_ms = int(exit_ts) if exit_ts is not None else now_ms
        pos = con.execute("SELECT * FROM paper_position WHERE position_id=? AND account_id=?",
                          (position_id, f"paper43_{arm}")).fetchone()
        if pos is None:
            return {"ok": False, "reason": "position_not_found"}
        if pos["status"] != "open":
            return {"ok": False, "reason": "position_not_open"}
        if raw_price is None or float(raw_price) <= 0:
            return {"ok": False, "reason": "bad_exit_price"}
        raw = float(raw_price)
        slip = float(params["slippage_each_side"])
        direction = pos["direction"] if "direction" in pos.keys() else "long"
        if direction == "short":
            fill = raw * (1.0 + slip)                 # 做空平仓（买入平空）：买入收更高价（不利滑点）
            qty = float(pos["qty"])
            entry = float(pos["entry_price"])
            gross = (entry - fill) * qty
        else:
            fill = raw * (1.0 - slip)                 # 做多平仓（卖出平多）：卖出收更低价（不利滑点）
            qty = float(pos["qty"])
            entry = float(pos["entry_price"])
            gross = (fill - entry) * qty
        fee_rate = float(params["fee_each_side"])
        notional = qty * fill
        fee = notional * fee_rate
        slip_cost = qty * abs(raw - fill)
        # 资金费：真实结算事件，完整窗口 `(entry_ts, exit_ts]` 集合差集（**无游标截断**）。
        # 持仓期间已由 `accrue_funding` 逐笔记过的**不重复入账**；这里只补记剩余事件。
        new_fund, n_new_fund, fund_detail, fund_gaps = _settle_funding(
            con, con_mkt, arm, position_id=position_id, symbol=pos["symbol"], qty=qty,
            entry_ts=int(pos["entry_ts"]), upto_ms=exit_ms, late=False)
        funding_paid = float(con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM paper_funding WHERE account_id=? AND position_id=?",
            (f"paper43_{arm}", int(position_id))).fetchone()[0])
        fund_rows = con.execute("SELECT COUNT(*) FROM paper_funding WHERE account_id=? "
                                "AND position_id=?",
                                (f"paper43_{arm}", int(position_id))).fetchone()[0]
        entry_fee = float(pos["entry_fee"] or 0.0)
        net = gross - entry_fee - fee - funding_paid
        con.execute("UPDATE paper_position SET status='closed', exit_ts=?, exit_reason=?, "
                    "exit_price=?, exit_fee=?, funding_paid=? WHERE position_id=?",
                    (exit_ms, reason, fill, fee, funding_paid, position_id))
        con.execute("INSERT INTO paper_realized(account_id,position_id,symbol,qty,entry_price,"
                    "exit_price,gross_pnl,entry_fee,exit_fee,funding_paid,net_pnl,entry_ts,"
                    "exit_ts,exit_reason,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"paper43_{arm}", position_id, pos["symbol"], qty, entry, fill, gross,
                     entry_fee, fee, funding_paid, net, int(pos["entry_ts"]), exit_ms, reason,
                     now_ms))
        con.execute("INSERT INTO paper_fill(account_id,position_id,signal_uid,symbol,action,qty,"
                    "raw_price,fill_price,notional,fee,slippage_cost,bar_open_time,bar_close_time,"
                    "fill_ts,model_version,event_time,available_at,requested_at,market_source) "
                    "VALUES(?,?,?,?,'close',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"paper43_{arm}", position_id, pos["signal_uid"], pos["symbol"], qty, raw, fill,
                     notional, fee, slip_cost, bar_open_time, bar_close_time,
                     now_ms, MODEL_VERSION,
                     (pos["event_time"] if "event_time" in pos.keys() else None),
                     (pos["available_at"] if "available_at" in pos.keys() else None),
                     now_ms, MARKET_SOURCE))
        # 现金变动：开仓时已扣 entry_fee、持仓期间已逐笔扣过资金费 ⇒
        # 平仓只再扣 exit_fee 与**本次新记**的资金费（避免重复扣）。
        _set_cash(con, f"paper43_{arm}", cash=(gross - fee - new_fund),
                  realized=net, fees=fee, funding=new_fund)
        eq2 = equity(con, arm, con_mkt, params)
        _record_equity(con, arm, eq2)
        con.commit()
        out = {"ok": True, "reason": "closed", "position_id": position_id, "symbol": pos["symbol"],
                "exit_price": fill, "raw_price": raw, "qty": qty, "gross_pnl": gross,
                "entry_fee": entry_fee, "exit_fee": fee, "funding_paid": funding_paid,
                "funding_paid_this_close": new_fund, "n_funding_events": fund_rows,
                "n_funding_events_this_close": n_new_fund, "funding_events": fund_detail,
                "funding_gaps": fund_gaps,
                "amount_sign_convention": AMOUNT_SIGN_CONVENTION,
                "net_pnl": net, "entry_ts": int(pos["entry_ts"]), "exit_ts": exit_ms,
                "exit_reason": reason, "model_version": MODEL_VERSION,
                "ledger_model_version": LEDGER_MODEL_VERSION,
                "market_source": MARKET_SOURCE}
        # 决定记录（条件实际值 / 阈值 / 时间 / 事件身份）：供任务三逐笔核对、任务一展示
        if EB.CORE_AVAILABLE:
            out["decision"] = EB.decision(
                kind="exit", symbol=pos["symbol"], exchange="binance", direction=direction,
                event_identity=f"exit|{arm}|{position_id}|{exit_ms}", reject_reason=None,
                condition_values={"raw_price": raw, "fill_price": fill, "qty": qty,
                                  "entry_price": entry, "gross_pnl": gross,
                                  "stop_price": (pos["stop_price"] if "stop_price" in pos.keys()
                                                 else None)},
                thresholds={"fee_each_side": fee_rate, "slippage_each_side": slip},
                times={"entry_ts": int(pos["entry_ts"]), "exit_ts": exit_ms,
                       "bar_open_time": bar_open_time, "bar_close_time": bar_close_time},
                exit_reason=reason, net_pnl=net, funding_paid=funding_paid,
                detail=decision_context or {})
        return out
    finally:
        con.close()
        con_mkt.close()


# ── 回撤锁定：四动作**完整执行**（停开仓 / 撤挂单 / 下根15m开盘清仓 / 本轮锁定）─────
# 修复 D02：旧实现只有「拒绝新开仓」一条（`pre_open_check` 的 `account_drawdown_locked`），
# 没有「撤未成交挂单、自动清仓、本轮永久锁定」的完整动作，也没有持久化锁定态。
# 本函数把四动作补齐，锁定态持久化到 `live_run:drawdown_lock`（**只允许不解除**）。
DRAWDOWN_LOCK_KEY = "drawdown_lock"


def drawdown_lock_state(arm: str) -> dict:
    """读该臂持久化锁定态（只读，不写库）。"""
    st = L.get_run(DRAWDOWN_LOCK_KEY, {}) or {}
    v = st.get(arm) if isinstance(st, dict) else None
    return v if isinstance(v, dict) else {"locked": False}


def enforce_drawdown(arm: str, *, T=None, log=None) -> dict:
    """按 15m 收盘权益执行回撤四动作（幂等；**只锁不解**）。

    ① 停开仓（持久锁定态，`pre_open_check` 与 `open_position` 均读取）；
    ② 撤未成交挂单（`paper_pending.status='rejected'`，原因 `drawdown_lock_cancel_pending`）；
    ③ 下一根 15m 开盘清仓（按该 bar 的 open + 不利滑点）；
    ④ 本轮持续锁定（`live_run:drawdown_lock` 只增不减）。
    """
    T = int(T) if T else L.now_ms()
    con = L.paper_conn(arm)
    try:
        con_mkt = L.market_conn()
    except Exception:                                       # noqa: BLE001
        con.close()
        return {"arm": arm, "ok": False, "reason": "market_db_unavailable"}
    try:
        params = approved_params()
        eq = equity(con, arm, con_mkt, params)
        prev = drawdown_lock_state(arm)
        st = (EB.drawdown_state(equity=eq["equity"], peak_equity=eq["peak_equity"],
                               locked_before=bool(prev.get("locked")),
                               config=EB.EXEC_FROZEN) if EB.CORE_AVAILABLE else
              {"computable": eq["equity"] is not None, "drawdown": eq["drawdown"],
               "locked": bool(prev.get("locked")) or eq["drawdown_locked"],
               "tripped_now": eq["drawdown_locked"] and not prev.get("locked"),
               "actions": [], "thresholds": {"max_drawdown_fraction":
                                             params["maximum_account_drawdown_fraction"]}})
        out = {"arm": arm, "ok": True, "T": T, "drawdown": st.get("drawdown"),
               "locked": bool(st.get("locked")), "tripped_now": bool(st.get("tripped_now")),
               "actions": list(st.get("actions") or []), "cancelled_pending": 0,
               "liquidated": [], "already_locked": bool(prev.get("locked"))}
        if not st.get("tripped_now"):
            return out
        # ② 撤未成交挂单
        rows = list(con.execute(
            "SELECT pending_id FROM paper_pending WHERE account_id=? AND status='pending'",
            (f"paper43_{arm}",)))
        for r in rows:
            _resolve_pending(con, int(r[0]), "rejected", "drawdown_lock_cancel_pending", T=T)
        out["cancelled_pending"] = len(rows)
        # ④ 持久锁定（只增不减）
        state = dict(L.get_run(DRAWDOWN_LOCK_KEY, {}) or {})
        state[arm] = {"locked": True, "since_ms": T, "drawdown": st.get("drawdown"),
                      "reason": "account_drawdown_fraction_20pct",
                      "actions": ["disable_new_entries", "cancel_pending_orders",
                                  "liquidate_all_next_15m_open", "lock_for_run_no_resume"]}
        L.set_run(DRAWDOWN_LOCK_KEY, state)
        # ③ 下一根 15m 开盘清仓（**因果**：只认 open_time >= 锁定时刻且**已闭合**的 bar；
        #    本轮尚无该 bar ⇒ 记为待清仓，下一轮时钟到达后自动执行，绝不使用未来 bar）
        pending_liquidation = []
        for p in open_positions(con, arm):
            nxt = bar_at_or_after(con_mkt, p["symbol"], T)
            if nxt is None or int(nxt["close_time"]) > T:
                pending_liquidation.append({"position_id": int(p["position_id"]),
                                            "symbol": p["symbol"],
                                            "reason": "awaiting_next_15m_open"})
                continue
            r = close_at_price(arm, int(p["position_id"]), raw_price=_f(nxt["open"]),
                               now_ms=T, reason="drawdown_liquidation_next_15m_open",
                               bar_open_time=int(nxt["open_time"]),
                               bar_close_time=int(nxt["close_time"]),
                               exit_ts=int(nxt["open_time"]),
                               decision_context={"rule": "drawdown_liquidation_next_15m_open",
                                                 "drawdown": st.get("drawdown"),
                                                 "bar_open_time": int(nxt["open_time"])})
            out["liquidated"].append(r)
        out["pending_liquidation"] = pending_liquidation
        if log:
            log(f"[paper] 回撤锁定 arm={arm} dd={st.get('drawdown')} "
                f"撤挂单={out['cancelled_pending']} 清仓={len(out['liquidated'])} "
                f"待清仓={len(pending_liquidation)}")
        return out
    finally:
        con.close()
        con_mkt.close()


def expiry_exit_ts(entry_ts_ms: int) -> int:
    """720h 到期经济出场时刻 = 入场+720h 之后**首根 15m 开盘**（修复 D01）。

    旧实现取「最后一根已闭合 15m 的**收盘**」，与冻结
    `fill_time_plus_720h_at_detail_open_no_added_delay` 不符。
    """
    if EB.CORE_AVAILABLE:
        return EB.expiry_exit_ms(int(entry_ts_ms), int(approved_params()["max_holding_hours"]))
    step = 900_000
    due = int(entry_ts_ms) + int(approved_params()["max_holding_hours"]) * H
    return -(-due // step) * step


def switches() -> dict:
    """**两个独立开关**（供任务一展示、任务三核对）。"""
    g = new_entry_gate()
    return {
        "allow_new_entries": g["allow_new_entries"],
        "new_entry_gate_reason": g["gate_reason"],
        "new_entry_gate_source": g["gate_file"],
        "new_entry_default_blocked": True,
        "maintain_existing": True,
        "maintenance_scope": ["exits", "funding_settlement", "expiry_720h",
                             "drawdown_lock", "trailing_stop"],
        "note": "旧仓维护不受新开仓闸影响；正式候选未选定/未验收 ⇒ 新开仓继续阻断。",
    }


# ── 无持仓信号（record-only，不造仓）────────────────────────────────────────
def log_no_fill(arm: str, symbol: str, signal_uid: str, reason: str) -> None:
    _reject(arm, symbol, signal_uid, reason)


# ── 逐臂快照（供 API 直接返回）───────────────────────────────────────────────
def snapshot(arm: str) -> dict:
    params = approved_params()
    con = L.paper_conn(arm)
    con_mkt = L.market_conn()
    try:
        eq = equity(con, arm, con_mkt, params)
        rows = []
        for p in open_positions(con, arm):
            u, m = unrealized(con_mkt, p)
            entry = float(p["entry_price"])
            mark = m["mark"]
            rows.append({
                "position_id": int(p["position_id"]), "symbol": p["symbol"],
                "direction": p["direction"] if ("direction" in p.keys() and p["direction"]) else "long",
                "qty": float(p["qty"]), "entry_price": entry,
                "mark_price": mark, "mark_source": m["mark_source"], "mark_time": m["mark_time"],
                "unrealized_pnl": None if u is None else round(u, 6),
                "unrealized_pnl_pct": None if (u is None or entry <= 0) else round(u / (entry * float(p["qty"])) * 100, 4),
                "stop_price": p["stop_price"], "atr_at_entry": p["atr_at_entry"],
                "entry_ts": int(p["entry_ts"]), "signal_uid": p["signal_uid"],
                "leverage": float(params["leverage"]),
                "notional": round(float(p["qty"]) * entry, 6),
                "opened_by": p["model_version"],
                "funding_paid": float(p["funding_paid"] or 0.0),
                # 因果审计（可核对：经济时刻/确认可用时刻/请求时刻/成交时刻/行情来源）
                "event_time": p["event_time"], "available_at": p["available_at"],
                "requested_at": p["requested_at"], "earliest_fill_ms": p["earliest_fill_ms"],
                "filled_at": int(p["entry_ts"]), "market_source": p["market_source"],
                "origin": p["origin"],
            })
        pend = [{"pending_id": int(q["pending_id"]), "symbol": q["symbol"], "origin": q["origin"],
                 "signal_uid": q["signal_uid"], "event_time": q["event_time"],
                 "available_at": q["available_at"], "requested_at": q["requested_at"],
                 "earliest_fill_ms": q["earliest_fill_ms"], "created_at": q["created_at"],
                 "status": q["status"], "reason": q["reason"],
                 "note": "未来待成交：由真实进程时钟推进（到点才成交；错过则拒单，不回填）"}
                for q in pending_orders(arm)]
        realized = [dict(r) for r in con.execute(
            "SELECT * FROM paper_realized WHERE account_id=? ORDER BY ts DESC LIMIT 100",
            (f"paper43_{arm}",))]
        funding = [dict(r) for r in con.execute(
            "SELECT * FROM paper_funding WHERE account_id=? ORDER BY funding_time DESC LIMIT 100",
            (f"paper43_{arm}",))]
        n_ord = con.execute("SELECT COUNT(*) FROM paper_order WHERE account_id=?",
                            (f"paper43_{arm}",)).fetchone()[0]
        n_rej = con.execute("SELECT COUNT(*) FROM paper_order WHERE account_id=? AND status='rejected'",
                            (f"paper43_{arm}",)).fetchone()[0]
        n_closed = con.execute("SELECT COUNT(*) FROM paper_realized WHERE account_id=?",
                               (f"paper43_{arm}",)).fetchone()[0]
        return {
            "arm": arm, "account_id": f"paper43_{arm}", "mode": "paper",
            "initial_equity": L.INIT_EQUITY, "cash": round(eq["cash"], 6),
            "equity": None if eq["equity"] is None else round(eq["equity"], 6),
            "unrealized_pnl": round(eq["unrealized"], 6),
            "used_margin": round(eq["gross_notional"] / max(float(params["leverage"]), 1e-9), 6),
            "gross_notional": round(eq["gross_notional"], 6),
            "drawdown": round(eq["drawdown"], 6), "drawdown_locked": eq["drawdown_locked"],
            "peak_equity": round(eq["peak_equity"], 6),
            "realized_pnl_total": round(account_view(arm, con)["realized_pnl"], 6),
            "fees_paid_total": round(account_view(arm, con)["fees_paid"], 6),
            "funding_paid_total": round(account_view(arm, con)["funding_paid"], 6),
            "positions": rows, "pending": pend, "n_pending": len(pend),
            "n_orders": n_ord, "n_rejected": n_rej, "n_closed": n_closed,
            "realized_history": realized, "funding_history": funding,
            "params_source": params["_source"], "model_version": MODEL_VERSION,
            "ledger_model_version": LEDGER_MODEL_VERSION,
            "exec_core": (EB.version_ids() if EB.CORE_AVAILABLE else
                          {"core_available": False, "import_error": EB.IMPORT_ERROR}),
            "config_source": _CONFIG_SOURCE.get("config_source"),
            "config_degraded": _CONFIG_SOURCE.get("degraded"),
            "switches": switches(),
            "quote_freshness": QUOTE_FRESHNESS, "fill_policy": FILL_POLICY,
        }
    finally:
        con.close()
        con_mkt.close()


def snapshot_all() -> dict:
    return {a: snapshot(a) for a in L.ARMS}


# ── 只读订单/挂单视图（2026-10-14 专项；06 任务 1「补真实只读订单/挂单查询」）────────
def list_orders(arm: str, *, status=None, limit=200) -> dict:
    """**只读**订单与挂单查询（不写任何行；`POST /api/v1/paper/orders` 仍是唯一写入口）。

    分三类**分别列出**（06 任务 2 / 派工第 3 点）：
      · `entry_orders`  ：入场订单（`paper_order.side='buy'`），含 `filled|rejected` 当前态；
      · `exit_fills`    ：平仓成交（`paper_fill.action='close'`）；
      · `pending`       ：未来待成交挂单（`paper_pending.status='pending'`）。
    另附「自动策略单 vs 人工/隔离单」分离计数与各状态直方图，供双层对账直接引用。
    """
    con = L.paper_conn(arm, ro=True)
    try:
        acc = f"paper43_{arm}"
        cols = {r[1] for r in con.execute("PRAGMA table_info(paper_order)")}
        extra = "".join(f",{c}" for c in ("fill_price", "notional", "bar_open_time",
                                          "model_version", "event_time", "requested_at",
                                          "market_source") if c in cols)
        sql = ("SELECT order_id,event_uid,symbol,side,qty,price,fee,ts,status,reject_reason"
               + extra + " FROM paper_order WHERE account_id=?")
        args = [acc]
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(int(limit))
        try:
            entries = [dict(r) for r in con.execute(sql, tuple(args))]
        except sqlite3.OperationalError:
            entries = []
        pend = [dict(r) for r in con.execute(
            "SELECT pending_id,symbol,origin,signal_uid,direction,atr,stop_price,event_time,"
            "available_at,requested_at,earliest_fill_ms,status,reason,created_at,resolved_at,"
            "fill_position_id,market_source FROM paper_pending WHERE account_id=? "
            "ORDER BY earliest_fill_ms DESC LIMIT ?", (acc, int(limit)))]
        exits = [dict(r) for r in con.execute(
            "SELECT fill_id,position_id,signal_uid,symbol,action,qty,raw_price,fill_price,"
            "notional,fee,slippage_cost,bar_open_time,bar_close_time,fill_ts,model_version "
            "FROM paper_fill WHERE account_id=? AND action='close' ORDER BY fill_ts DESC LIMIT ?",
            (acc, int(limit)))]
        hist = {}
        for r in con.execute("SELECT status,COUNT(*) n FROM paper_order WHERE account_id=? "
                             "GROUP BY status", (acc,)):
            hist[r[0]] = int(r[1])
        pend_hist = {}
        for r in con.execute("SELECT status,COUNT(*) n FROM paper_pending WHERE account_id=? "
                             "GROUP BY status", (acc,)):
            pend_hist[r[0]] = int(r[1])
        fills_hist = {}
        for r in con.execute("SELECT action,COUNT(*) n FROM paper_fill WHERE account_id=? "
                             "GROUP BY action", (acc,)):
            fills_hist[r[0]] = int(r[1])
        auto = sum(1 for o in entries
                   if not str(o.get("event_uid") or "").startswith(("manual:", "reject:")))
        return {
            "arm": arm, "account_id": acc, "mode": "paper_read_only",
            "entry_orders": entries, "pending": pend, "exit_fills": exits,
            "order_status_histogram": hist,
            "pending_status_histogram": pend_hist,
            "fill_action_histogram": fills_hist,
            "separated": {
                "auto_strategy_orders": auto,
                "manual_or_isolated_orders": len(entries) - auto,
                "entry_orders": sum(1 for o in entries if (o.get("side") or "buy") == "buy"),
                "exit_fills": len(exits),
            },
            "write_entrypoint": "POST /api/v1/paper/orders（唯一写入口；本 GET 只读）",
        }
    finally:
        con.close()


def list_orders_all(*, limit=200) -> dict:
    return {a: list_orders(a, limit=limit) for a in L.ARMS}



# ── 重启恢复自检 ─────────────────────────────────────────────────────────────
def recover_report() -> dict:
    """重启后：账本自洽性 + 持仓/挂单可从库恢复。

    现金恒等式（含**未平仓**）：`cash = 初始 + Σ已实现净额 − Σ未平仓入场手续费
    − Σ未平仓已计资金费`。
    未平仓时入场费与「持仓期间逐笔已扣的资金费」都已从现金扣走但尚未计入已实现
    ⇒ 必须单列，否则误报不自洽。
    """
    out = {}
    for a in L.ARMS:
        con = L.paper_conn(a)
        try:
            st = con.execute("SELECT * FROM paper_state WHERE account_id=?",
                             (f"paper43_{a}",)).fetchone()
            pos = [dict(r) for r in open_positions(con, a)]
            n_fill = con.execute("SELECT COUNT(*) FROM paper_fill WHERE account_id=?",
                                 (f"paper43_{a}",)).fetchone()[0]
            n_real = con.execute("SELECT COUNT(*) FROM paper_realized WHERE account_id=?",
                                 (f"paper43_{a}",)).fetchone()[0]
            n_proc = con.execute("SELECT COUNT(*) FROM paper_processed WHERE arm=?",
                                 (a,)).fetchone()[0]
            n_fund = con.execute("SELECT COUNT(*) FROM paper_funding WHERE account_id=?",
                                 (f"paper43_{a}",)).fetchone()[0]
            pend = pending_orders(a)
            net = con.execute("SELECT COALESCE(SUM(net_pnl),0) FROM paper_realized WHERE account_id=?",
                              (f"paper43_{a}",)).fetchone()[0]
            open_fee = con.execute(
                "SELECT COALESCE(SUM(entry_fee),0) FROM paper_position "
                "WHERE account_id=? AND status='open'", (f"paper43_{a}",)).fetchone()[0]
            open_fund = con.execute(
                "SELECT COALESCE(SUM(funding_paid),0) FROM paper_position "
                "WHERE account_id=? AND status='open'", (f"paper43_{a}",)).fetchone()[0]
            cash = float(st["cash"]) if st else L.INIT_EQUITY
            expect = L.INIT_EQUITY + float(net) - float(open_fee) - float(open_fund)
            out[a] = {"cash": cash, "realized_net_sum": float(net),
                      "open_entry_fees": round(float(open_fee), 6),
                      "open_funding_paid": round(float(open_fund), 6),
                      "expected_cash": round(expect, 6),
                      "cash_equals_init_plus_net": abs(cash - expect) < 1e-6,
                      "open_positions": len(pos), "pending_orders": len(pend),
                      "n_fills": n_fill, "n_realized": n_real, "n_funding_events": n_fund,
                      "n_processed_signals": n_proc}
        finally:
            con.close()
    return out


if __name__ == "__main__":
    L.ensure_live_schema()
    print(json.dumps({"approved_params": approved_params(),
                      "quote_freshness": QUOTE_FRESHNESS,
                      "fill_policy": FILL_POLICY,
                      "snapshot": snapshot_all(),
                      "recover": recover_report()},
                     ensure_ascii=False, indent=1, default=str)[:4000])
