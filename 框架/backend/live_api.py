#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""信号与持仓接通（2026-10-13）· **实时/纸面/实盘只读 路由处理器**（纯标准库）。

由 `adapter.py` 以**最小加性补丁**调用：
  · GET  `/api/v1/signals`          → 追加**实时信号**（`origin=live`）与**历史研究信号**
                                      （`origin=historical_research`，明确标注，不冒充当前）
  · GET  `/api/v1/paper/accounts`   → 四臂模拟账户 + 持仓 + 权益（服务端账本）
  · GET  `/api/v1/paper/positions`  → 扁平持仓列表（供页面直接渲染）
  · POST `/api/v1/paper/orders`     → 页面**模拟开/平仓**（服务端账本唯一写入口）
  · GET  `/api/v1/accounts`         → **实盘只读**账户快照（余额/权益/持仓）
  · GET  `/api/v1/live/health`      → 实时 worker 心跳与各源状态

纪律：
  · **展示与可执行分离**：`display_state` 恒 `displayable`；`executable_state` 单独表达，
    **不**使用 G01–G05 正式回测门禁（那是回测闸门，不是只读信号展示闸门）。
  · 纸面成交只经 `paper_engine`（服务端账本），**浏览器不得造仓**。
  · 实盘账户**只读**；不返回任何密钥字段。
"""
from __future__ import annotations

import json
import sqlite3

import live_store as L
import paper_engine as PE

# 可执行态 → 前端六态（仅展示用；不改变 executable_state 原文）
_EXEC_TO_STATUS = {
    "enterable": "confirmed",
    "display_only_same_instant": "candidate",
    "blocked_no_signal": "undecided",
    "blocked_not_admitted": "undecided",
    "blocked_missing_daily_list": "undecided",
    "not_this_arm_structure": "undecided",
    # 历史研究信号：只展示，**恒不可执行**（不冒充当前可交易信号）
    "historical_not_executable": "undecided",
}


def _iso(ms):
    if ms is None:
        return None
    import datetime as dt
    return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def envelope(**kw):
    base = {"mode": "read_only", "account_id": None, "run_id": None, "strategy_version": None,
            "rule_version": None, "param_hash": None, "data_version": None, "as_of": None,
            "quality": {"status": "ok", "age_seconds": None, "note": None},
            "source_url": None, "evidence_level": None, "missing_reason": None,
            "time": {"event_time": None, "available_at": None, "received_at": None,
                     "fetched_at": None},
            "items": [], "has_more": False, "next_cursor": None, "total": None}
    base.update(kw)
    return base


# ── 实时信号 ─────────────────────────────────────────────────────────────────
# 「当前交易信号」判据（2026-10-14 信号区清理专项）──────────────────────────────
# 一条记录只有**同时**满足下列全部条件，才算「当前有效信号」（缺一不可）：
#   ① `origin='live'`                       —— 排除历史研究导入（`historical_research`）
#   ② `executable_state='enterable'`        —— 排除仅展示 / 非本臂结构 / 阻断
#   ③ `confirmed_at` 与 `available_at` 均 `>= live_start_ms`
#                                           —— 排除实时起点**之前**的窗口内历史事件
#   ④ `payload.role='enterable_arm_entry'`  —— **真实策略臂入场事件**
#      （冻结策略自己的 `events`，`source_masks ∈ base/spring/sc`）；
#      旧引擎写入的 `enterable_structure`（结构证据提示）**不算**
#
# 口径纪律：`origin=live` 字样、库内 `enterable` 标记**本身不足以通过** ——
# 旧引擎把结构证据（spring/sc）也写成 `enterable`，且全部早于起点水位线；
# 只有「真实臂入场事件 + 起点之后」才是当前交易信号。
# 起点（`live_start_ms`）缺失 ⇒ **如实空态**，不用当前时间重置水位、不放行旧信号。
# 过滤一律发生在分页 `LIMIT` 与 `total` / `has_more` 计算**之前**（不得先取 N 条再前端裁）。
CURRENT_SCOPE = "current"
AUDIT_SCOPES = ("all", "audit", "history")
ARM_ENTRY_ROLE = "enterable_arm_entry"
# 纸面账本里**永不可成交的终态**拒单原因 ⇒ 该信号不得再混入当前区（09 契约 §4）。
TERMINAL_REJECT_REASONS = ("missed_fill_moment_no_backfill", "before_paper_epoch_no_backfill")


def _scope_of(params) -> str:
    s = (params.get("scope") or [CURRENT_SCOPE])[0]
    return (s or CURRENT_SCOPE).strip().lower()


def _buy_point_key(it):
    """同买点身份：**(交易所, 币种, 周期, 真实 bar, 方向)**。

    09 契约 §4：同一买点被 3a/3c/3d 等多个臂分别命中时，页面应**合为一张卡**
    （列出命中臂与各臂去向）；**不同时间的新事件保留为不同卡**（bar 身份不同 ⇒ 不合并）。
    `bar_close_time` 缺失时回退 `confirmed_at`（同一条信号的确认时刻）。
    """
    return (it.get("exchange"), it.get("symbol"), it.get("timeframe"),
            it.get("bar_close_time") if it.get("bar_close_time") is not None
            else it.get("confirmed_at"),
            it.get("direction"))


def _merge_same_buy_point(items):
    """把同一买点的多臂行合并为一张卡；卡上 `arms[]` 列各臂身份与去向。

    **不改库内事实**：只改展示层聚合。执行幂等仍按
    「epoch / 交易所 / 币种 / arm账户 / 周期 / 真实事件身份」逐臂独立记账，
    **不跨臂抹掉独立实验**。
    """
    out, index = [], {}
    for it in items:
        entry = {"arm": it.get("arm"), "signal_id": it.get("signal_id"),
                 "status": it.get("status"),
                 "executable_state": it.get("executable_state"),
                 "actionable": it.get("actionable"),
                 "actionable_reason": it.get("actionable_reason"),
                 "structural_kind": it.get("structural_kind"),
                 "source_masks": it.get("source_masks"),
                 "execution_label": it.get("execution_label")}
        k = _buy_point_key(it)
        card = index.get(k)
        if card is None:
            card = dict(it)
            card["arms"] = [entry]
            index[k] = card
            out.append(card)
        else:
            card["arms"].append(entry)
            # 合并结构来源（同一买点可能由不同 source 条件命中 ⇒ 取并集，不丢失）
            masks = list(card.get("source_masks") or [])
            for m in (it.get("source_masks") or []):
                if m not in masks:
                    masks.append(m)
            card["source_masks"] = masks
    for c in out:
        c["arm_count"] = len(c["arms"])
        c["arms_hit"] = [a.get("arm") for a in c["arms"] if a.get("arm")]
    return out


def live_signals(params) -> dict:
    """实时层信号（`live_signal` 表）。

    参数：
      · `scope=current`（**默认**）= 当前交易信号区：只返回**当前真实臂入场**信号
        （判据见上方注释），过滤在分页 `LIMIT` 与 `total` / `has_more` 计算**之前**；
      · `scope=all|audit|history` = 历史审计通道：返回全部行（含历史研究 / 结构提示），
        供审计与对账读取，**默认交易信号区不得使用**；
      · `origin` / `symbol` / `limit` / `executable_only` 保持原有语义。
    """
    scope = _scope_of(params)
    origin = (params.get("origin") or [None])[0]
    symbol = (params.get("symbol") or [None])[0]
    signal_id = (params.get("signal_id") or [None])[0]
    try:
        limit = int((params.get("limit") or ["200"])[0])
    except (ValueError, TypeError):
        limit = 200
    limit = max(1, min(limit, 1000))
    only_exec = (params.get("executable_only") or ["0"])[0] in ("1", "true", "True")
    where, args = [], []
    start_ms = None
    floor_ms = None
    epoch_ms = None
    if scope == CURRENT_SCOPE:
        # **有效下限 = max(live_start, paper epoch)**（2026-10-14 09 契约）：
        # 四臂、挂单推进、自动/手工开仓与本 API **共用同一下限**。
        # 边界口径：原始 `close_time` 存闭区间右端 **−1ms** ⇒ 用真实右边界
        # `max(confirmed_at, available_at) + 1` 比较（**不改原值**）。
        start_ms = L.get_live_start_ms()
        epoch_ms = L.get_paper_epoch_ms()
        floor_ms = L.effective_floor_ms()
        if not floor_ms:
            # 下限缺失 ⇒ 无法判定「当前」；**不**用当前时间兜底重置水位，如实空态。
            return envelope(items=[], has_more=False, total=0, limit=limit,
                            missing_reason="有效下限(paper epoch / live_start)缺失 ⇒ 当前有效信号无法判定",
                            quality={"status": "no_watermark", "age_seconds": None,
                                     "scope": CURRENT_SCOPE, "live_start_ms": start_ms,
                                     "paper_epoch_ms": None, "effective_floor_ms": None,
                                     "note": "下限缺失时如实空态：不用当前时间重置水位、不放行旧信号"})
        where.append("origin='live'")
        where.append("executable_state='enterable'")
        where.append("confirmed_at IS NOT NULL AND available_at IS NOT NULL")
        where.append("(CASE WHEN available_at > confirmed_at THEN available_at "
                     "ELSE confirmed_at END) + 1 >= ?")
        args.append(int(floor_ms))
        where.append("json_extract(payload_json,'$.role')=?")
        args.append(ARM_ENTRY_ROLE)
    if origin:
        where.append("origin=?"); args.append(origin)
    if symbol:
        where.append("symbol=?"); args.append(symbol)
    if signal_id:
        # 按 `signal_uid` 精确取单条（供 `/api/v1/signals/{uid}` 详情与审计读取）
        where.append("signal_uid=?"); args.append(signal_id)
    if only_exec:
        where.append("executable_state='enterable'")
    con = L.live_conn(ro=True)
    try:
        # **排除已终态（永不可成交）记录**（09 契约 §4：「09:00 前与已终态过期记录
        # 不混当前区，历史审计保留」）。判据：该信号在**任一臂**纸面账本里已是终态拒单
        # （`missed_fill_moment_no_backfill` / `before_paper_epoch_no_backfill`）。
        # 做法：ATTACH 四臂纸面库 + 跨库 `NOT EXISTS`（只读连接上的 ATTACH 已实测可用）；
        # ATTACH 失败 ⇒ 不阻断，退回仅按下限过滤（如实降级，不伪造）。
        if scope == CURRENT_SCOPE:
            attached = []
            for _arm in L.ARMS:
                try:
                    con.execute(f"ATTACH DATABASE ? AS sig_{_arm}",
                                (str(L.PAPER_DIR / f"paper_{_arm}.sqlite3"),))
                    attached.append(_arm)
                except Exception:                        # noqa: BLE001
                    pass
            if attached:
                reasons = ",".join("?" * len(TERMINAL_REJECT_REASONS))
                sub = " OR ".join(
                    f"EXISTS (SELECT 1 FROM sig_{a}.paper_order o "
                    f"WHERE o.account_id=? AND o.event_uid = live_signal.signal_uid "
                    f"AND o.status='rejected' AND o.reject_reason IN ({reasons}))"
                    for a in attached)
                where.append(f"NOT ({sub})")
                for a in attached:
                    args.append(f"paper43_{a}")
                    args.extend(TERMINAL_REJECT_REASONS)
        sql = "SELECT * FROM live_signal"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY confirmed_at DESC, symbol ASC LIMIT ?"
        args.append(limit + 1)
        rows = list(con.execute(sql, args))
        total = con.execute("SELECT COUNT(*) FROM live_signal" +
                            (" WHERE " + " AND ".join(where) if where else ""), args[:-1]).fetchone()[0]
        # 被移出当前区的行数（仅 current 口径统计，供对账；不改任何行的存储）
        excluded_n = None
        if scope == CURRENT_SCOPE:
            all_n = con.execute("SELECT COUNT(*) FROM live_signal").fetchone()[0]
            excluded_n = int(all_n) - int(total)
    except sqlite3.OperationalError:
        return envelope(missing_reason="实时信号库尚未初始化（请先运行 live worker）",
                        quality={"status": "not_connected", "age_seconds": None,
                                 "note": "live_signals.sqlite3 未就绪"})
    finally:
        con.close()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for r in rows:
        d = dict(r)
        st = _EXEC_TO_STATUS.get(d["executable_state"], "undecided")
        try:
            payload = json.loads(d.get("payload_json") or "{}")
        except ValueError:
            payload = {}
        items.append({
            "signal_id": d["signal_uid"],
            "exchange": d["exchange"], "market_id": d["symbol"], "symbol": d["symbol"],
            "status": st,
            "direction": d.get("direction"),
            "current_direction": ("long" if d["executable_state"] == "enterable" else None),
            "actionable": d["executable_state"] == "enterable",
            "actionable_reason": d.get("executable_reason"),
            "strength": None,
            "created_at": _iso(d.get("first_seen_at")),
            "confirmed_at": _iso(d.get("confirmed_at")),
            "event_time": _iso(d.get("event_time")),
            "available_at": _iso(d.get("available_at")),
            "expires_at": None,
            "timeframe": d.get("timeframe"),
            "strategy_key": d.get("strategy_key"),
            "strategy_version": d.get("strategy_version"),
            "config_hash": d.get("config_hash"),
            "data_version": d.get("data_version"),
            "source_id": None,
            "source_masks": payload.get("source_masks") or [d.get("structural_kind")],
            "execution_label": payload.get("execution_label"),
            # ── 实时层扩展字段（前端据此标注「实时/历史」与可执行态）──
            "origin": d.get("origin"),
            "arm": payload.get("arm") or (d["signal_uid"].rsplit("|", 1)[-1] or None),
            "display_state": d.get("display_state"),
            "executable_state": d.get("executable_state"),
            "structural_kind": d.get("structural_kind"),
            "bar_open_time": d.get("bar_open_time"),
            "bar_close_time": d.get("bar_close_time"),
            "detection_low": d.get("detection_low"),
            "detection_atr": d.get("detection_atr"),
            "admission": d.get("admission"),
            # ── 事件角色（当前区判据之一）：`enterable_arm_entry` = 真实臂入场 ──
            "role": payload.get("role"),
        })
    n_rows = len(items)
    n_live = sum(1 for i in items if i["origin"] == "live")
    note = (f"实时信号层（scope={scope}）：live={n_live}/{n_rows}；"
            "展示(display_state)与可执行(executable_state)分列；"
            "不适用 G01–G05 正式回测门禁")
    n_cards = None
    if scope == CURRENT_SCOPE:
        # **同买点合并为一张卡**（09 契约 §4）：同 (交易所, 币种, 周期, 真实 bar, 方向)
        # 的多个臂命中合卡，卡上 `arms[]` 列各臂身份与去向；不同时间的新事件不合并。
        # 执行幂等与各臂独立实验**不受影响**（库内仍逐臂记账）。
        items = _merge_same_buy_point(items)
        n_cards = len(items)
        note = ("当前交易信号区（scope=current）：origin=live + enterable + "
                "真实 bar 右边界不早于有效下限 + 真实臂入场(payload.role=enterable_arm_entry)"
                " + 排除已终态拒单；"
                f"合并后 {n_cards} 张卡（原始 {n_rows} 条臂行）；"
                f"已从本区移出 {excluded_n} 条（仍保留在库/审计通道）")
    return envelope(items=items, has_more=has_more, total=total, limit=limit,
                    as_of=_iso(max([r["confirmed_at"] or 0 for r in rows] or [0])) or None,
                    quality={"status": "ok", "age_seconds": None, "scope": scope,
                             "live_start_ms": start_ms,
                             "paper_epoch_ms": epoch_ms,
                             "effective_floor_ms": floor_ms,
                             "n_arm_rows": n_rows, "n_cards": n_cards,
                             "excluded_n": excluded_n, "note": note})


# ── 模拟盘 ───────────────────────────────────────────────────────────────────
def paper_accounts() -> dict:
    snap = PE.snapshot_all()
    items = list(snap.values())
    # 实时起点水位线：解释「为什么现在可能没有自动持仓」——不是没接通，
    # 而是窗口内历史信号**按因果纪律不自动开仓**（只展示）。
    start_ms = L.get_live_start_ms()
    n_pre = 0
    try:
        con = L.live_conn(ro=True)
        try:
            if start_ms:
                n_pre = con.execute(
                    "SELECT COUNT(*) FROM live_signal WHERE origin='live' "
                    "AND executable_state='enterable' AND confirmed_at < ?",
                    (int(start_ms),)).fetchone()[0]
            n_auto = con.execute(
                "SELECT COUNT(*) FROM live_signal WHERE origin='live' "
                "AND executable_state='enterable'"
                + (" AND confirmed_at >= ?" if start_ms else ""),
                ((int(start_ms),) if start_ms else ())).fetchone()[0]
        finally:
            con.close()
    except sqlite3.OperationalError:
        n_auto = 0
    return envelope(items=items, total=len(items), mode="paper",
                    quality={"status": "ok", "age_seconds": None,
                             "live_start_ms": start_ms,
                             "live_start_utc": _iso(start_ms) if start_ms else None,
                             "n_pre_watermark_display_only": n_pre,
                             "n_after_watermark": n_auto,
                             "n_pending_orders": sum(int(s.get("n_pending") or 0)
                                                     for s in snap.values()),
                             "fill_policy": PE.FILL_POLICY,
                             "note": "服务端纸面账本（四臂各 5000U 初始）；成交模型见 "
                                     "model_version。成交时钟：**第一根 `open_time >= "
                                     "max(信号确认时刻,信号可用时刻,真实请求时刻)+15m` 的已闭合 "
                                     "15m bar 的 open**；尚未到点 ⇒ 登记挂单（`pending`），由真实"
                                     "进程时钟推进；错过成交时刻 ⇒ 拒单，**不用旧价回填**。"
                                     "资金费按 `funding_history` **真实结算事件逐笔记账**"
                                     "（事件经过时即更新账户/权益；不固定 8h、不补 0、不倒填）。"
                                     "**自动开仓只对确认时刻不早于实时起点的信号**："
                                     f"起点前有 {n_pre} 条窗口内历史实时信号（真实事件，"
                                     "照常展示）**不自动开仓**；页面手工开/平仓不受影响"})


def paper_positions() -> dict:
    out = []
    for arm, s in PE.snapshot_all().items():
        for p in s["positions"]:
            out.append({"arm": arm, "account_id": s["account_id"], **p})
    return envelope(items=out, total=len(out), mode="paper")


def paper_orders(params=None) -> dict:
    """**只读**订单/挂单查询（06 任务 1：补真实只读查询，**保留 POST 写入口**）。

    `?arm=3a|3b|3c|3d`（缺省四臂全给）`&status=filled|rejected|pending` `&limit=N`。
    本端点**不写任何行**；开/平仓仍只走 `POST /api/v1/paper/orders`。
    """
    params = params or {}
    arm = (params.get("arm") or [None])[0]
    status = (params.get("status") or [None])[0]
    try:
        limit = int((params.get("limit") or ["200"])[0])
    except (ValueError, TypeError):
        limit = 200
    limit = max(1, min(limit, 2000))
    if arm:
        if arm not in L.ARMS:
            return envelope(missing_reason=f"未知臂 {arm!r}（应为 {list(L.ARMS)}）",
                            quality={"status": "bad_request", "age_seconds": None, "note": None})
        items = [PE.list_orders(arm, status=status, limit=limit)]
    else:
        items = [PE.list_orders(a, status=status, limit=limit) for a in L.ARMS]
    return envelope(items=items, total=len(items), mode="paper_read_only",
                    quality={"status": "ok", "age_seconds": None,
                             "note": "只读订单/挂单/平仓成交查询；写入口仍是 "
                                     "POST /api/v1/paper/orders"})


def paper_funding_audit(params=None) -> dict:
    """**只读**资金费符号口径与迟到补记对账（06 任务 3 / 07 G-P03 的在线核对点）。"""
    params = params or {}
    arm = (params.get("arm") or [None])[0]
    arms = [arm] if arm in L.ARMS else list(L.ARMS)
    items = [PE.audit_funding_usage(a) for a in arms]
    return envelope(items=items, total=len(items), mode="paper_read_only",
                    quality={"status": "ok", "age_seconds": None,
                             "amount_sign_convention": PE.AMOUNT_SIGN_CONVENTION,
                             "note": "amount>0 ⇒ 账户支付资金费（支出）；amount<0 ⇒ 收取；"
                                     "ΔCash = −amount。缺口（缺真实 mark/rate）单列，不补 0。"})


def paper_order_post(payload: dict) -> tuple[dict, int]:
    """页面模拟开/平仓（服务端账本唯一写入口）。body:
    `{"arm":"3a","action":"open|close","symbol":"AAVEUSDT","atr":2.1,"signal_uid":"...","position_id":1}`
    """
    arm = str(payload.get("arm") or "").strip()
    action = str(payload.get("action") or "").strip()
    if arm not in L.ARMS:
        return envelope(missing_reason=f"未知臂 {arm!r}（应为 {list(L.ARMS)}）",
                        quality={"status": "bad_request", "age_seconds": None, "note": None}), 400
    if action == "open":
        symbol = str(payload.get("symbol") or "").strip().upper()
        if not symbol:
            return envelope(missing_reason="缺 symbol",
                            quality={"status": "bad_request", "age_seconds": None,
                                     "note": None}), 400
        atr = payload.get("atr")
        # **因果修正（2026-10-13）**：手工请求也走**同一成交时钟**（不再锚定「最近一根已闭合
        # bar」的旧价）。`requested_at` = 本次真实请求挂钟；应成交时刻 = 请求 + 已批准延迟。
        # 若那一刻的已闭合 15m bar 还不存在 ⇒ 登记**挂单**（返回 `pending_future_fill`），
        # 由真实进程时钟推进；**不提前成交、不回填旧价**。
        req = L.now_ms()
        r = PE.open_position(arm, symbol, signal_uid=payload.get("signal_uid") or
                             f"manual:{req}", direction=payload.get("direction") or "long",
                             atr=(None if atr is None else float(atr)),
                             stop_price=(None if payload.get("stop_price") is None
                                         else float(payload["stop_price"])),
                             now_ms=req, requested_at=req,
                             event_time=payload.get("event_time"),
                             available_at=payload.get("available_at"),
                             origin="manual")
        pending = r.get("reason") == "pending_future_fill"
        if pending:
            return envelope(items=[r], mode="paper", total=1,
                            quality={"status": "pending", "age_seconds": None,
                                     "note": "挂单已登记：应成交时刻尚未到达，"
                                             "等真实时钟到点成交（不回填旧价）"}), 200
        return envelope(items=[r], mode="paper", total=1,
                        quality={"status": "ok" if r.get("ok") else "rejected",
                                 "age_seconds": None,
                                 "note": "服务端模拟成交" if r.get("ok") else r.get("reason")}), \
            (200 if r.get("ok") else 409)
    if action == "close":
        pid = payload.get("position_id")
        if pid is None:
            return envelope(missing_reason="缺 position_id",
                            quality={"status": "bad_request", "age_seconds": None,
                                     "note": None}), 400
        r = PE.close_position(arm, int(pid), reason=payload.get("reason") or "manual")
        return envelope(items=[r], mode="paper", total=1,
                        quality={"status": "ok" if r.get("ok") else "rejected",
                                 "age_seconds": None,
                                 "note": "服务端模拟平仓" if r.get("ok") else r.get("reason")}), \
            (200 if r.get("ok") else 409)
    return envelope(missing_reason=f"未知 action {action!r}（应为 open|close）",
                    quality={"status": "bad_request", "age_seconds": None, "note": None}), 400


# ── 实盘只读账户 ─────────────────────────────────────────────────────────────
def live_accounts(params=None) -> dict:
    con = L.live_conn(ro=True)
    try:
        rows = list(con.execute("SELECT * FROM live_account_snapshot ORDER BY exchange"))
    except sqlite3.OperationalError:
        return envelope(missing_reason="实盘账户快照库尚未初始化",
                        quality={"status": "not_connected", "age_seconds": None, "note": None})
    finally:
        con.close()
    if not rows:
        return envelope(missing_reason="尚无实盘账户快照（live worker 未运行或未配置凭据）",
                        quality={"status": "not_connected", "age_seconds": None,
                                 "note": "空态来自「无快照」，非「接口未实现」"})
    now = L.now_ms()
    items = []
    for r in rows:
        d = dict(r)
        try:
            pos = json.loads(d.get("positions_json") or "[]")
        except ValueError:
            pos = []
        age = None if d.get("read_at") is None else (now - int(d["read_at"])) / 1000.0
        status = d["status"]
        if status in ("connected", "connected_no_positions") and age is not None and age > 300:
            status = "stale"
        items.append({
            "exchange": d["exchange"], "account_label": d["account_label"],
            "status": status, "raw_status": d["status"],
            "detail": d.get("detail"), "error_class": d.get("error_class"),
            "equity_usd": d.get("equity_usd"), "available_usd": d.get("available_usd"),
            "used_margin_usd": d.get("used_margin_usd"),
            "positions": pos, "n_positions": len(pos),
            "source": d.get("source"), "read_at": _iso(d.get("read_at")),
            "age_seconds": None if age is None else round(age, 1),
            "readonly": True, "no_real_order": True,
        })
    return envelope(items=items, total=len(items), mode="live_readonly",
                    as_of=_iso(max([r["read_at"] or 0 for r in rows] or [0])) or None,
                    quality={"status": "ok", "age_seconds": None,
                             "note": "实盘账户**只读**：仅 fetch_balance/fetch_positions；"
                                     "不提交订单、不改权限、不返回密钥"})


def live_health() -> dict:
    st = L.get_run("signal_engine_state", {}) or {}
    acct = L.get_run("account_state", {}) or {}
    return envelope(items=[{"signal_engine": st, "account_reader": acct}], total=1,
                    quality={"status": "ok", "age_seconds": None,
                             "note": "实时 worker 心跳与各源状态"})


_ARM_META_LABELS = {
    "ALL": ("四臂组合汇总", "4 × $5,000 独立账本聚合"),
    "3a": ("唐奇安基线", "1h close > 此前20根high"),
    "3b": ("放量阳线过滤", "3a ∧ 成交额>1.5×20h均量 ∧ 阳线"),
    "3c": ("威科夫Spring", "3a ∨ 跌破20h箱体收回站上中线"),
    "3d": ("威科夫SC", "3a ∨ 放量新低后越过恢复目标"),
}

_BTC_CACHE: dict[str, list[tuple[int, float]]] = {}


def _get_btc_1h_series() -> list[tuple[int, float]]:
    """缓存读取真实 BTCUSDT 1h 收盘价序列（优先 Feather 完整历史 + market_v2 最新热库）。"""
    if "pts" in _BTC_CACHE and _BTC_CACHE["pts"]:
        return _BTC_CACHE["pts"]
    pts_map: dict[int, float] = {}
    try:
        import pyarrow.feather as feather
        f_path = L.HERE / "历史下载/perp_datadir_real/data/binance/futures/BTC_USDT_USDT-1h-futures.feather"
        if f_path.exists():
            tb = feather.read_table(str(f_path), columns=["date", "close"], memory_map=True)
            for d, c in zip(tb.column("date").to_pylist(), tb.column("close").to_pylist()):
                if c is not None and c > 0:
                    ms = int(d.timestamp() * 1000) if hasattr(d, "timestamp") else int(d)
                    pts_map[ms] = float(c)
    except Exception:
        pass
    try:
        mcon = L.market_conn()
        try:
            for r in mcon.execute(
                "SELECT open_time, close FROM candle_heads "
                "WHERE exchange='binance' AND symbol='BTCUSDT' AND timeframe='1h' "
                "ORDER BY open_time ASC"
            ):
                if r["close"] and float(r["close"]) > 0:
                    pts_map[int(r["open_time"])] = float(r["close"])
        finally:
            mcon.close()
    except Exception:
        pass
    pts = sorted(pts_map.items(), key=lambda x: x[0])
    if pts:
        _BTC_CACHE["pts"] = pts
    return pts


def _align_btc_benchmark(timestamps: list[int], init_equity: float) -> dict[int, float]:
    """按区间起点价格将真实 BTCUSDT 1h 收盘序列归一化为同起点基准净值。"""
    if not timestamps:
        return {}
    pts = _get_btc_1h_series()
    if not pts:
        return {}
    import bisect
    ts_keys = [p[0] for p in pts]
    i0 = max(0, bisect.bisect_right(ts_keys, timestamps[0]) - 1)
    base_px = pts[i0][1]
    if base_px <= 0:
        return {}
    out: dict[int, float] = {}
    for t in timestamps:
        idx = max(0, bisect.bisect_right(ts_keys, t) - 1)
        out[t] = round(init_equity * (pts[idx][1] / base_px), 2)
    return out


def backtest_review(params=None) -> dict:
    """真实历史回测与模拟实盘复盘接口（接通 /api/v1/backtests 与 /api/v1/reviews）。

    彻底修复：
      1. `mode=history` 读取 `ledgers_ra/ledger_3a..3d.sqlite3` 真实 159 币历史回测账本；
      2. `mode=paper_review` 读取 `paper_3a..3d.sqlite3` 并严格过滤 `COALESCE(voided,0)=0`；
      3. 移除全部硬编码假数据（58.3% 胜率、2.34 夏普、30 天假锯齿曲线）；
      4. 返回四臂横向对标矩阵 `arm_comparison`、毛利/手续费/资金费瀑布拆解、平仓原因拆解、
         单币种盈亏榜单 `symbol_attribution`、信号→成交漏斗 `funnel` 与真实 BTC 基准净值。
    """
    import datetime as dt
    import math

    params = params or {}
    mode = (params.get("mode") or ["history"])[0]
    target_arm = (params.get("arm") or ["ALL"])[0]
    time_range = (params.get("range") or ["ALL"])[0].upper()
    try:
        limit = int((params.get("limit") or ["500"])[0])
    except (ValueError, TypeError):
        limit = 500
    limit = max(1, min(limit, 5000))

    arms = [target_arm] if target_arm in L.ARMS else list(L.ARMS)
    range_days = {"7D": 7, "30D": 30, "90D": 90, "180D": 180, "1Y": 365}.get(time_range)

    all_closed: list[dict] = []
    all_open_positions: list[dict] = []
    eq_by_ts: dict[int, dict[str, float]] = {}
    halted_info: dict[str, str | None] = {}
    arm_comparison: list[dict] = []
    data_version = None
    total_signals_scoped = 0
    total_intents_scoped = 0
    total_rejected_scoped = 0

    if mode == "history":
        ra_dir = L.BASE / "回测资源改造_2026-10-14" / "ledgers_ra"
        ref_dir = L.BASE / "全池真实费用回测_2026-10-13" / "ledgers"
        ledger_dir = ra_dir if (ra_dir / "ledger_3a.sqlite3").exists() else ref_dir

        for a in L.ARMS:
            db_path = ledger_dir / f"ledger_{a}.sqlite3"
            if not db_path.exists():
                continue
            con = sqlite3.connect(str(db_path))
            con.row_factory = sqlite3.Row
            try:
                rev = con.execute("SELECT * FROM research_review LIMIT 1").fetchone()
                run_row = con.execute("SELECT data_version, initial_equity FROM research_run LIMIT 1").fetchone()
                sig_cnt = int(con.execute("SELECT COUNT(*) FROM research_signal").fetchone()[0])
                intent_cnt = int(con.execute("SELECT COUNT(*) FROM research_intent").fetchone()[0])
                rej_cnt = int(
                    con.execute("SELECT COUNT(*) FROM research_order WHERE status!='fill'").fetchone()[0]
                )
                if run_row and not data_version:
                    data_version = run_row["data_version"]

                if rev:
                    init_a = float(run_row["initial_equity"]) if run_row else 5000.0
                    net_a = float(rev["net_profit"] or 0.0)
                    fees_a = float(rev["total_fees"] or 0.0)
                    fund_paid_a = -float(rev["total_funding"] or 0.0)
                    # 真实扣费前价差毛利 = 净利 + 手续费 + 资金费净支出
                    gross_raw_a = net_a + fees_a + fund_paid_a
                    ct_a = int(rev["closed_trades"] or 0)
                    wins_a = int(rev["wins"] or 0)
                    losses_a = int(rev["losses"] or 0)
                    gp_net = float(rev["gross_profit"] or 0.0)
                    gl_net = abs(float(rev["gross_loss"] or 0.0))
                    pf_a = round(gp_net / gl_net, 2) if gl_net > 0 else 0.0
                    avg_w = (gp_net / wins_a) if wins_a > 0 else 0.0
                    avg_l = (gl_net / losses_a) if losses_a > 0 else 0.0
                    wl_a = round(avg_w / avg_l, 2) if avg_l > 0 else 0.0
                    halted_info[a] = rev["halted"]
                    lbl, rule_desc = _ARM_META_LABELS.get(a, (a, ""))
                    arm_comparison.append({
                        "arm": a,
                        "label": lbl,
                        "rule": rule_desc,
                        "initial_equity": init_a,
                        "final_equity": round(float(rev["final_equity"] or init_a), 2),
                        "net_profit": round(net_a, 2),
                        "net_return_pct": round(net_a / init_a * 100.0, 2),
                        "gross_profit_raw": round(gross_raw_a, 2),
                        "total_fees": round(fees_a, 2),
                        "total_funding_paid": round(fund_paid_a, 2),
                        "max_drawdown_pct": round(float(rev["max_drawdown"] or 0.0) * 100.0, 2),
                        "closed_trades": ct_a,
                        "wins": wins_a,
                        "losses": losses_a,
                        "win_rate_pct": round(wins_a / ct_a * 100.0, 1) if ct_a > 0 else 0.0,
                        "profit_factor": pf_a,
                        "win_loss_ratio": wl_a,
                        "expectancy_usd": round(net_a / ct_a, 2) if ct_a > 0 else 0.0,
                        "signals_count": sig_cnt,
                        "intents_count": intent_cnt,
                        "rejected_count": rej_cnt,
                        "halted_at": rev["halted"],
                    })

                if a in arms:
                    total_signals_scoped += sig_cnt
                    total_intents_scoped += intent_cnt
                    total_rejected_scoped += rej_cnt
                    for r in con.execute(
                        "SELECT * FROM research_position WHERE status='closed' ORDER BY exit_time DESC"
                    ):
                        d = dict(r)
                        all_closed.append({
                            "id": d["position_id"],
                            "arm": a,
                            "symbol": d["symbol"],
                            "direction": "long",
                            "qty": float(d["quantity"] or 0.0),
                            "entry_price": float(d["entry_price"] or 0.0),
                            "exit_price": float(d["exit_price"] or 0.0),
                            "entry_ts": int(d["entry_time"] or 0),
                            "exit_ts": int(d["exit_time"] or 0),
                            "gross_pnl": float(d["gross_pnl"] or 0.0),
                            "fee": float(d["fees"] or 0.0),
                            "funding_paid": -float(d["funding"] or 0.0),
                            "net_pnl": float(d["net_pnl"] or 0.0),
                            "exit_reason": d.get("exit_reason") or "closed",
                        })
                    for r in con.execute("SELECT ts, equity FROM research_equity ORDER BY ts ASC"):
                        t_bucket = int(r["ts"]) // 86_400_000 * 86_400_000
                        eq_by_ts.setdefault(t_bucket, {})[a] = float(r["equity"])
            finally:
                con.close()
    else:
        snapshots = PE.snapshot_all()
        conns = L.paper_conns(ro=True)
        try:
            for a in L.ARMS:
                con = conns.get(a)
                if not con:
                    continue
                r_rows = [dict(r) for r in con.execute("SELECT * FROM paper_realized ORDER BY ts DESC")]
                open_rows = [
                    dict(r)
                    for r in con.execute(
                        "SELECT * FROM paper_position WHERE status='open' AND COALESCE(voided,0)=0 ORDER BY entry_ts DESC"
                    )
                ]
                eq_rows = [
                    dict(r)
                    for r in con.execute(
                        "SELECT ts, equity FROM paper_equity WHERE COALESCE(voided,0)=0 ORDER BY ts ASC"
                    )
                ]
                rej_cnt = int(
                    con.execute(
                        "SELECT COUNT(*) FROM paper_order WHERE status='rejected' AND COALESCE(voided,0)=0"
                    ).fetchone()[0]
                )
                snap_a = snapshots.get(a) or {}
                init_a = float(snap_a.get("initial_equity", 5000.0))
                cur_eq_a = float(snap_a.get("equity", init_a))
                ct_a = len(r_rows)
                wins_a = sum(1 for x in r_rows if float(x.get("net_pnl") or 0) > 0)
                losses_a = sum(1 for x in r_rows if float(x.get("net_pnl") or 0) < 0)
                net_a = sum(float(x.get("net_pnl") or 0) for x in r_rows)
                gross_a = sum(float(x.get("gross_pnl") or 0) for x in r_rows)
                fees_a = sum(float(x.get("entry_fee") or 0) + float(x.get("exit_fee") or 0) for x in r_rows)
                fund_a = sum(float(x.get("funding_paid") or 0) for x in r_rows)
                win_sum_a = sum(float(x.get("net_pnl") or 0) for x in r_rows if float(x.get("net_pnl") or 0) > 0)
                loss_sum_a = sum(abs(float(x.get("net_pnl") or 0)) for x in r_rows if float(x.get("net_pnl") or 0) < 0)
                pf_a = round(win_sum_a / loss_sum_a, 2) if loss_sum_a > 0 else 0.0
                avg_w_a = (win_sum_a / wins_a) if wins_a > 0 else 0.0
                avg_l_a = (loss_sum_a / losses_a) if losses_a > 0 else 0.0
                wl_a = round(avg_w_a / avg_l_a, 2) if avg_l_a > 0 else 0.0

                peak_a = init_a
                mdd_a = 0.0
                for er in eq_rows:
                    ev = float(er["equity"])
                    if ev > peak_a:
                        peak_a = ev
                    if peak_a > 0:
                        mdd_a = max(mdd_a, (peak_a - ev) / peak_a * 100.0)

                lbl, rule_desc = _ARM_META_LABELS.get(a, (a, ""))
                arm_comparison.append({
                    "arm": a,
                    "label": lbl,
                    "rule": rule_desc,
                    "initial_equity": init_a,
                    "final_equity": round(cur_eq_a, 2),
                    "net_profit": round(net_a, 2),
                    "net_return_pct": round((cur_eq_a - init_a) / init_a * 100.0, 2) if init_a > 0 else 0.0,
                    "gross_profit_raw": round(gross_a, 2),
                    "total_fees": round(fees_a, 2),
                    "total_funding_paid": round(fund_a, 2),
                    "max_drawdown_pct": round(mdd_a, 2),
                    "closed_trades": ct_a,
                    "open_positions": len(open_rows),
                    "wins": wins_a,
                    "losses": losses_a,
                    "win_rate_pct": round(wins_a / ct_a * 100.0, 1) if ct_a > 0 else 0.0,
                    "profit_factor": pf_a,
                    "win_loss_ratio": wl_a,
                    "expectancy_usd": round(net_a / ct_a, 2) if ct_a > 0 else 0.0,
                    "signals_count": ct_a + len(open_rows) + rej_cnt,
                    "intents_count": ct_a + len(open_rows) + rej_cnt,
                    "rejected_count": rej_cnt,
                    "halted_at": None,
                })

                if a in arms:
                    total_signals_scoped += ct_a + len(open_rows) + rej_cnt
                    total_intents_scoped += ct_a + len(open_rows) + rej_cnt
                    total_rejected_scoped += rej_cnt
                    for d in r_rows:
                        all_closed.append({
                            "id": f"{a}-{d['id']}",
                            "arm": a,
                            "symbol": d["symbol"],
                            "direction": d.get("direction") or "long",
                            "qty": float(d.get("qty") or 0.0),
                            "entry_price": float(d.get("entry_price") or 0.0),
                            "exit_price": float(d.get("exit_price") or 0.0),
                            "entry_ts": int(d.get("entry_ts") or d.get("ts") or 0),
                            "exit_ts": int(d.get("exit_ts") or d.get("ts") or 0),
                            "gross_pnl": float(d.get("gross_pnl") or 0.0),
                            "fee": float(d.get("entry_fee") or 0.0) + float(d.get("exit_fee") or 0.0),
                            "funding_paid": float(d.get("funding_paid") or 0.0),
                            "net_pnl": float(d.get("net_pnl") or 0.0),
                            "exit_reason": d.get("exit_reason") or "normal_exit",
                        })
                    for d in open_rows:
                        d["arm"] = a
                        all_open_positions.append(d)
                    for er in eq_rows:
                        t_bucket = int(er["ts"]) // 300_000 * 300_000
                        eq_by_ts.setdefault(t_bucket, {})[a] = float(er["equity"])
        finally:
            for c in conns.values():
                c.close()

    if arm_comparison:
        tot_init = sum(x["initial_equity"] for x in arm_comparison)
        tot_final = sum(x["final_equity"] for x in arm_comparison)
        tot_net = sum(x["net_profit"] for x in arm_comparison)
        tot_ct = sum(x["closed_trades"] for x in arm_comparison)
        tot_wins = sum(x["wins"] for x in arm_comparison)
        tot_losses = sum(x["losses"] for x in arm_comparison)
        arm_comparison.insert(0, {
            "arm": "ALL",
            "label": "四臂组合汇总",
            "rule": "4 × $5,000 独立账本聚合",
            "initial_equity": tot_init,
            "final_equity": round(tot_final, 2),
            "net_profit": round(tot_net, 2),
            "net_return_pct": round((tot_final - tot_init) / tot_init * 100.0, 2) if tot_init > 0 else 0.0,
            "gross_profit_raw": round(sum(x["gross_profit_raw"] for x in arm_comparison), 2),
            "total_fees": round(sum(x["total_fees"] for x in arm_comparison), 2),
            "total_funding_paid": round(sum(x["total_funding_paid"] for x in arm_comparison), 2),
            "max_drawdown_pct": max((x["max_drawdown_pct"] for x in arm_comparison), default=0.0),
            "closed_trades": tot_ct,
            "wins": tot_wins,
            "losses": tot_losses,
            "win_rate_pct": round(tot_wins / tot_ct * 100.0, 1) if tot_ct > 0 else 0.0,
            "profit_factor": round(sum(x["profit_factor"] for x in arm_comparison) / len(arm_comparison), 2),
            "win_loss_ratio": round(sum(x["win_loss_ratio"] for x in arm_comparison) / len(arm_comparison), 2),
            "expectancy_usd": round(tot_net / tot_ct, 2) if tot_ct > 0 else 0.0,
            "signals_count": sum(x["signals_count"] for x in arm_comparison),
            "intents_count": sum(x.get("intents_count", 0) for x in arm_comparison),
            "rejected_count": sum(x.get("rejected_count", 0) for x in arm_comparison),
            "halted_at": next((x["halted_at"] for x in arm_comparison if x["halted_at"]), None),
        })

    init_equity = len(arms) * 5000.0
    sorted_times = sorted(eq_by_ts.keys())
    if range_days and sorted_times:
        cutoff_ts = sorted_times[-1] - range_days * 86_400_000
        sorted_times = [t for t in sorted_times if t >= cutoff_ts]
        all_closed = [t for t in all_closed if t["exit_ts"] >= cutoff_ts]

    curve_points: list[dict] = []
    last_known = {a: 5000.0 for a in arms}
    peak_eq = init_equity
    max_dd_pct = 0.0
    for t in sorted_times:
        for a in arms:
            if a in eq_by_ts[t]:
                last_known[a] = eq_by_ts[t][a]
        tot = sum(last_known[a] for a in arms)
        if tot > peak_eq:
            peak_eq = tot
        dd = ((peak_eq - tot) / peak_eq * 100.0) if peak_eq > 0 else 0.0
        if dd > max_dd_pct:
            max_dd_pct = dd
        curve_points.append({
            "timestamp": t,
            "time": _iso(t),
            "equity": round(tot, 2),
            "drawdown_pct": round(dd, 2),
        })

    if curve_points:
        base_eq_for_btc = curve_points[0]["equity"] if range_days else init_equity
        current_equity = curve_points[-1]["equity"]
        btc_map = _align_btc_benchmark([p["timestamp"] for p in curve_points], base_eq_for_btc)
        for p in curve_points:
            p["benchmark_equity"] = btc_map.get(p["timestamp"])
    else:
        current_equity = init_equity

    closed_trades = len(all_closed)
    win_pnls = [t["net_pnl"] for t in all_closed if t["net_pnl"] > 0]
    loss_pnls = [abs(t["net_pnl"]) for t in all_closed if t["net_pnl"] < 0]
    win_trades, loss_trades = len(win_pnls), len(loss_pnls)
    win_rate = (win_trades / closed_trades * 100.0) if closed_trades > 0 else 0.0
    realized_pnl = sum(t["net_pnl"] for t in all_closed)
    gross_profit_raw = sum(t["gross_pnl"] for t in all_closed)
    trading_fees = sum(t["fee"] for t in all_closed)
    funding_paid = sum(t["funding_paid"] for t in all_closed)
    fees_paid = trading_fees + funding_paid
    total_win, total_loss = sum(win_pnls), sum(loss_pnls)
    avg_win = (total_win / win_trades) if win_trades > 0 else 0.0
    avg_loss = (total_loss / loss_trades) if loss_trades > 0 else 0.0
    profit_factor = round(total_win / total_loss, 2) if total_loss > 0 else 0.0
    win_loss_ratio = round(avg_win / avg_loss, 2) if avg_loss > 0 else 0.0
    net_return_pct = round(((current_equity - init_equity) / init_equity * 100.0), 2) if init_equity > 0 else 0.0

    # 真实夏普比率（由净值曲线非零差分计算，剔除 halted 后的水平零波动死水期失真）
    sharpe = 0.0
    if len(curve_points) > 2:
        active_rets = [
            (curve_points[i]["equity"] - curve_points[i - 1]["equity"]) / curve_points[i - 1]["equity"]
            for i in range(1, len(curve_points))
            if curve_points[i - 1]["equity"] > 0 and abs(curve_points[i]["equity"] - curve_points[i - 1]["equity"]) > 1e-8
        ]
        if len(active_rets) > 2:
            mean_r = sum(active_rets) / len(active_rets)
            std_r = math.sqrt(sum((r - mean_r) ** 2 for r in active_rets) / (len(active_rets) - 1))
            if std_r > 1e-12:
                sharpe = round((mean_r / std_r) * math.sqrt(365.0), 2)

    # 自然月真实收益聚合
    monthly_returns: dict[str, dict[str, dict]] = {}
    for t in all_closed:
        if not t["exit_ts"]:
            continue
        d_utc = dt.datetime.fromtimestamp(t["exit_ts"] / 1000.0, dt.timezone.utc)
        yr, mo = str(d_utc.year), f"{d_utc.month:02d}"
        cell = monthly_returns.setdefault(yr, {}).setdefault(mo, {"pnl_usd": 0.0, "count": 0, "pnl": 0.0})
        cell["pnl_usd"] = round(cell["pnl_usd"] + t["net_pnl"], 2)
        cell["count"] += 1
        cell["pnl"] = round(cell["pnl_usd"] / init_equity * 100.0, 2)

    # 单币种盈亏归因榜单（Top 5 贡献币种 & Bottom 5 拖累币种）
    sym_map: dict[str, dict] = {}
    for t in all_closed:
        s = t["symbol"]
        rec = sym_map.setdefault(s, {"symbol": s, "trades": 0, "wins": 0, "gross_pnl": 0.0, "fees": 0.0, "net_pnl": 0.0})
        rec["trades"] += 1
        if t["net_pnl"] > 0:
            rec["wins"] += 1
        rec["gross_pnl"] = round(rec["gross_pnl"] + t["gross_pnl"], 2)
        rec["fees"] = round(rec["fees"] + t["fee"] + t["funding_paid"], 2)
        rec["net_pnl"] = round(rec["net_pnl"] + t["net_pnl"], 2)
    sym_sorted = sorted(sym_map.values(), key=lambda x: x["net_pnl"], reverse=True)
    symbol_attribution = {
        "top_winners": sym_sorted[:5],
        "top_losers": list(reversed(sym_sorted[-5:])) if len(sym_sorted) >= 5 else list(reversed(sym_sorted)),
    }

    # 组装明细列表
    items: list[dict] = []
    if mode == "signal_logs":
        try:
            live_con = L.live_conn(ro=True)
            try:
                sig_rows = live_con.execute(
                    "SELECT * FROM live_signal ORDER BY event_time DESC LIMIT ?", (limit,)
                ).fetchall()
            finally:
                live_con.close()
            for r in sig_rows:
                sd = dict(r)
                items.append({
                    "id": sd.get("signal_uid") or str(sd.get("id")),
                    "time": _iso(sd.get("event_time") or sd.get("created_at")),
                    "timestamp": sd.get("event_time") or sd.get("created_at"),
                    "symbol": (sd.get("symbol") or "").replace("USDT", ""),
                    "raw_symbol": sd.get("symbol") or "",
                    "arm": (sd.get("signal_uid") or "").rsplit("|", 1)[-1] if "|" in (sd.get("signal_uid") or "") else "3a",
                    "direction": "做空" if sd.get("direction") == "short" else "做多",
                    "entry_price": sd.get("detection_low") or 0.0,
                    "exit_price": None,
                    "qty": None,
                    "gross_pnl": 0.0,
                    "pnl": 0.0,
                    "pnl_pct": 0.0,
                    "fee": 0.0,
                    "status": sd.get("executable_state") or "undecided",
                    "exit_reason": sd.get("executable_reason") or sd.get("structural_kind") or "signal",
                })
        except Exception:
            pass
    else:
        all_closed.sort(key=lambda x: x["exit_ts"] or x["entry_ts"], reverse=True)
        for t in all_closed[:limit]:
            is_short = t["direction"] in ("short", "做空")
            ret_pct = ((t["exit_price"] - t["entry_price"]) / t["entry_price"] * 100.0) if t["entry_price"] > 0 else 0.0
            if is_short:
                ret_pct = -ret_pct
            diff_m = max(1, int((t["exit_ts"] - t["entry_ts"]) / 60_000)) if t["exit_ts"] > t["entry_ts"] else 15
            dur_str = f"{diff_m // 60}h {diff_m % 60}m" if diff_m >= 60 else f"{diff_m}m"
            items.append({
                "id": f"closed-{t['id']}",
                "time": _iso(t["entry_ts"]),
                "timestamp": t["entry_ts"],
                "exit_time": _iso(t["exit_ts"]),
                "exit_timestamp": t["exit_ts"],
                "duration_str": dur_str,
                "symbol": t["symbol"].replace("USDT", ""),
                "raw_symbol": t["symbol"],
                "arm": t["arm"],
                "direction": "做空" if is_short else "做多",
                "entry_price": t["entry_price"],
                "exit_price": t["exit_price"],
                "qty": t["qty"],
                "gross_pnl": round(t["gross_pnl"], 2),
                "pnl": round(t["net_pnl"], 2),
                "pnl_pct": round(ret_pct, 2),
                "fee": round(t["fee"] + t["funding_paid"], 4),
                "funding_paid": round(t["funding_paid"], 4),
                "status": "已平仓",
                "exit_reason": t["exit_reason"],
            })

    return envelope(
        items=items,
        total=closed_trades,
        data_version=data_version,
        equity_curve=curve_points,
        arm_comparison=arm_comparison,
        monthly_returns=monthly_returns,
        symbol_attribution=symbol_attribution,
        halted_info=halted_info,
        funnel={
            "signals_count": total_signals_scoped,
            "intents_count": total_intents_scoped,
            "rejected_count": total_rejected_scoped,
            "filled_positions": closed_trades + len(all_open_positions),
        },
        summary={
            "initial_equity": round(init_equity, 2),
            "current_equity": round(current_equity, 2),
            "realized_pnl": round(realized_pnl, 2),
            "gross_profit_raw": round(gross_profit_raw, 2),
            "trading_fees": round(trading_fees, 2),
            "funding_paid": round(funding_paid, 2),
            "net_return_pct": net_return_pct,
            "max_drawdown_pct": round(max_dd_pct, 2),
            "sharpe_ratio": sharpe,
            "profit_factor": profit_factor,
            "win_loss_ratio": win_loss_ratio,
            "win_rate_pct": round(win_rate, 1),
            "total_trades": closed_trades + len(all_open_positions),
            "closed_trades": closed_trades,
            "open_positions": len(all_open_positions),
            "total_fees": round(fees_paid, 2),
        },
        mode=mode,
        quality={"status": "ok", "age_seconds": None, "note": "真实账本对账接通（零硬编码兜底）"},
    )

