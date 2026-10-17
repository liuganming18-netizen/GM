# -*- coding: utf-8 -*-
"""账本**真实标识**关联（只读 / 确定性 / 不猜配）。

背景（2026-10-17 查实）
======================
`回测日志/event_trend_v0_1/runs/*/` 的账本被落成了 **JSONL**（没有 `ledger.sqlite3`），
而 `backtest_store.read_ledger_rows()` 的 JSONL 兼容分支里写的是：

    r.setdefault("trade_id", r.get("trade_id") or f"trd_{len(rows)+1}")
    r.setdefault("order_id", r.get("order_id") or f"ord_{len(rows)+1}")

`trades.jsonl` / `orders.jsonl` **本身根本没有 `trade_id` 字段**（`orders.jsonl` 有真
`order_id`），于是这段兜底**必然**触发 ⇒ 对外一律返回 `trd_1`/`trd_2`…
这是**按行号猜配**，正是本项目明令禁止的做法：它会让「图上选中的那一笔」与
「账本里真正的那一笔」失去可核对性，且 `trd_1` 与账本真实标识
（`positions.jsonl` 的 `trd_<16hex>`）对不上。

本模块给出**三条只用真实标识的链路**；任一环节不唯一 ⇒ 返回 `None` + 缺失原因，
绝不用行号 / 顺序 / 邻近 / 复算哈希顶替。

链路 ①：交易 ↔ `trade_id`
-------------------------
源：`positions.jsonl` 的 `POSITION_CLOSED.payload.trade_id`（形如 `trd_<16hex>`）。
匹配键：`(symbol, exit_time)`。

* 为什么不调 `backtest_store.new_trade_id()` 复算：该函数以 **run_id** 为种子，
  而批量脚本是「引擎在自己的 run 目录里跑 → 再把产物复制到脚本的 run 目录」，
  两个 run_id 不同 ⇒ 用目标 run_id 复算**必然对不上**（LPTUSDT 实测：复算得
  `trd_9d6019a5e43ab100`，账本真值 `trd_eb941d2defdde27285ef06e2`）。
* 唯一性：全量 375 个 run 中 27 个有成交，`(symbol, exit_time)` **无重复**；
  `POSITION_CLOSED` 条数与 `trades.jsonl` 行数**逐 run 相等**。

链路 ②：交易 ↔ 订单
-------------------
源：`trades.jsonl.order_id`（开仓）/ `trades.jsonl.exit_order_id`（平仓）——真实订单标识，
直连 `orders.jsonl.order_id`。
交叉校验：`positions.jsonl` 的 `POSITION_OPENED.payload.order_id` 必须与之一致。

链路 ③：交易 ↔ 资金费
---------------------
源：`funding.jsonl` 的 `FUNDING_FEE` 事件**不带任何 trade 标识**（只有 `event_time`、
`payload.delta`、`payload.cum_funding_fee`、`sequence`）。因此只能按**时间窗归属**：
事件时间落在 `[entry_time, exit_time]` 内且**恰好一笔**包含 ⇒ 归属该笔；
零笔包含（事件在持仓之外）或多笔包含（窗口重叠）⇒ `None` + 歧义原因。
交叉校验：`Σ(−payload.delta)` 必须等于该笔 `funding_fee`（符号约定：正数=成本）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "read_jsonl",
    "load_ledger_facts",
    "LedgerFacts",
]


def read_jsonl(p: Path) -> List[dict]:
    """逐行 JSON 读取；单行坏掉只跳过该行，不因一行毁掉整个载荷。"""
    out: List[dict] = []
    try:
        if not Path(p).exists():
            return out
        with Path(p).open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:                              # noqa: BLE001
                    continue
    except Exception:                                          # noqa: BLE001
        return []
    return out


def _int_or_none(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


class LedgerFacts:
    """一个 run 目录的账本事实（一次读盘，多处复用）。

    所有映射都是「唯一才收录」：`None` 表示**账本无法唯一确定**，调用方必须如实
    展示缺失，不得回退到顺序 / 行号。
    """

    def __init__(self, run_dir: Path, run_id: str):
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.notes: List[str] = []

        self.trades: List[dict] = read_jsonl(self.run_dir / "trades.jsonl")
        self.orders: List[dict] = read_jsonl(self.run_dir / "orders.jsonl")
        self.funding: List[dict] = read_jsonl(self.run_dir / "funding.jsonl")
        self.positions: List[dict] = read_jsonl(self.run_dir / "positions.jsonl")
        self.candidates: List[dict] = read_jsonl(self.run_dir / "candidates.jsonl")
        self.equity: List[dict] = read_jsonl(self.run_dir / "equity_curve.jsonl")

        self._build()

    # ── 链路 ①：交易 ↔ trade_id ────────────────────────────────────────────
    def _build(self) -> None:
        closed = [p for p in self.positions if p.get("event_type") == "POSITION_CLOSED"]
        opened = [p for p in self.positions if p.get("event_type") == "POSITION_OPENED"]

        # (symbol, exit_time) → [trade_id, ...]（收集全部，>1 即歧义）
        by_exit: Dict[Tuple[str, int], List[str]] = {}
        for ev in closed:
            pl = ev.get("payload") or {}
            tid = pl.get("trade_id")
            ex = _int_or_none(ev.get("event_time"))
            sym = ev.get("symbol")
            if not tid or ex is None or sym is None:
                continue
            by_exit.setdefault((str(sym), ex), []).append(str(tid))
        self._trade_id_by_exit: Dict[Tuple[str, int], Optional[str]] = {}
        for k, v in by_exit.items():
            self._trade_id_by_exit[k] = v[0] if len(v) == 1 else None

        # (symbol, entry_time) → order_id（开仓订单，来自 POSITION_OPENED）
        by_entry: Dict[Tuple[str, int], List[str]] = {}
        for ev in opened:
            pl = ev.get("payload") or {}
            oid = pl.get("order_id")
            et = _int_or_none(ev.get("event_time"))
            sym = ev.get("symbol")
            if not oid or et is None or sym is None:
                continue
            by_entry.setdefault((str(sym), et), []).append(str(oid))
        self._entry_order_by_entry: Dict[Tuple[str, int], Optional[str]] = {
            k: (v[0] if len(v) == 1 else None) for k, v in by_entry.items()
        }

        # orders.jsonl：真实 order_id → 行
        self._order_by_id: Dict[str, dict] = {}
        for o in self.orders:
            oid = o.get("order_id")
            if oid:
                self._order_by_id.setdefault(str(oid), o)

        # ── 逐笔标注 ──────────────────────────────────────────────────────
        self.trade_rows: List[dict] = []
        for i, t in enumerate(self.trades):
            row = dict(t)
            row.setdefault("run_id", self.run_id)
            sym = str(row.get("symbol") or "")
            et = _int_or_none(row.get("entry_time") or row.get("entry_time_ms"))
            xt = _int_or_none(row.get("exit_time") or row.get("exit_time_ms"))
            row["entry_time_ms"] = et
            row["exit_time_ms"] = xt
            row["symbol"] = sym or None

            # ① trade_id：账本真值，唯一才给
            tid = self._trade_id_by_exit.get((sym, xt)) if (sym and xt is not None) else None
            row["trade_id"] = tid
            row["trade_id_source"] = ("ledger:positions.POSITION_CLOSED" if tid
                                      else "unresolved")

            # ② 订单：真实 order_id / exit_order_id 直连
            entry_oid = row.get("order_id")
            exit_oid = row.get("exit_order_id")
            # 交叉校验：POSITION_OPENED 的 order_id 必须一致，不一致则记为冲突（仍以 trades 为准并标注）
            po_oid = self._entry_order_by_entry.get((sym, et)) if (sym and et is not None) else None
            if po_oid and entry_oid and po_oid != entry_oid:
                self.notes.append(
                    f"trade#{i + 1} 开仓订单冲突：trades.jsonl={entry_oid} "
                    f"vs positions.POSITION_OPENED={po_oid}")
            row["entry_order_id"] = str(entry_oid) if entry_oid else None
            row["exit_order_id"] = str(exit_oid) if exit_oid else None
            row["entry_order_found"] = bool(entry_oid and str(entry_oid) in self._order_by_id)
            row["exit_order_found"] = bool(exit_oid and str(exit_oid) in self._order_by_id)
            row["linked_order_count"] = int(row["entry_order_found"]) + int(row["exit_order_found"])

            # stop_trace：统一成 `t`（官方契约），保留 `ts` 原样以便核对
            st = []
            for p in (row.get("stop_trace") or []):
                if not isinstance(p, dict):
                    continue
                ts = _int_or_none(p.get("t", p.get("ts")))
                if ts is None:
                    continue
                st.append({"t": ts, "stop": p.get("stop"), "reason": p.get("reason"),
                           "ts": ts})
            row["stop_trace"] = st
            row["stop_trace_json"] = json.dumps(
                [{"t": x["t"], "stop": x["stop"], "reason": x["reason"]} for x in st],
                ensure_ascii=False)
            row["stop_trace_points"] = len(st)

            # 口径统一：net_pnl 必须扣费后
            row["fees"] = (row.get("fees")
                           if row.get("fees") is not None
                           else (float(row.get("entry_fee") or 0.0)
                                 + float(row.get("exit_fee") or 0.0)))
            row["holding_hours"] = (round((xt - et) / 3_600_000.0, 6)
                                    if (et is not None and xt is not None and xt >= et) else None)
            self.trade_rows.append(row)

        # 订单 → 交易 反向索引（**只用真实 order_id**，供全局 `/orders` 标注归属）。
        # 一个订单若同时关联两笔**不同**交易 ⇒ 冲突，记 note 并标为不可归属（不猜）。
        self._trade_by_order: Dict[str, Optional[str]] = {}
        for row in self.trade_rows:
            tid = row.get("trade_id")
            if not tid:
                continue
            for oid in (row.get("entry_order_id"), row.get("exit_order_id")):
                if not oid:
                    continue
                prev = self._trade_by_order.get(oid, tid)
                if prev is not None and prev != tid:
                    self.notes.append(
                        f"订单 {oid} 同时关联多笔交易（{prev} / {tid}）⇒ 归属不唯一，标为未关联")
                    self._trade_by_order[oid] = None
                elif oid not in self._trade_by_order:
                    self._trade_by_order[oid] = tid

        # ③ 资金费归属
        self.funding_rows: List[dict] = self._link_funding()

        # 逐笔的资金费事件计数（用于 LabTradeRow.funding_events）
        cnt: Dict[str, int] = {}
        for f in self.funding_rows:
            if f.get("trade_id"):
                cnt[f["trade_id"]] = cnt.get(f["trade_id"], 0) + 1
        for row in self.trade_rows:
            tid = row.get("trade_id")
            row["funding_events"] = cnt.get(tid, 0) if tid else None
            row["funding_events_linked"] = (row["funding_events"] is not None)

        # ④ 候选信号（供 /trades/{id} 的 original_signal 用；键是 candidate_id）
        self.signal_by_id: Dict[str, dict] = {}
        for c in self.candidates:
            cid = c.get("candidate_id")
            if cid:
                self.signal_by_id[str(cid)] = c

    # ── 链路 ③ 实现 ───────────────────────────────────────────────────────
    def _link_funding(self) -> List[dict]:
        wins = []
        for row in self.trade_rows:
            et, xt, tid = row.get("entry_time_ms"), row.get("exit_time_ms"), row.get("trade_id")
            if et is not None and xt is not None:
                wins.append((et, xt, tid))
        out: List[dict] = []
        for e in self.funding:
            t_ms = _int_or_none(e.get("event_time"))
            if t_ms is None:
                continue
            pl = e.get("payload") or {}
            delta = pl.get("delta")
            hit = [(tid) for (a, b, tid) in wins if a <= t_ms <= b]
            tid: Optional[str]
            if len(hit) == 1:
                tid = hit[0]
            else:
                tid = None
                if len(hit) > 1:
                    self.notes.append(
                        f"资金费事件 {t_ms} 落在 {len(hit)} 笔交易窗口内 ⇒ 归属不唯一，记为缺失")
            out.append({
                "run_id": self.run_id,
                "trade_id": tid,
                "trade_link": ("time_window_unique" if tid else
                               ("ambiguous" if len(hit) > 1 else "outside_any_trade")),
                "funding_time_ms": t_ms,
                "event_time": t_ms,
                "rate": None,                 # 账本只落 delta，未落 rate/mark ⇒ 如实为 None
                "mark": None,
                "qty": None,
                "amount": (float(delta) if delta is not None else None),
                "cum_funding_fee": pl.get("cum_funding_fee"),
                "event_id": e.get("event_id"),
                "sequence": e.get("sequence"),
                "symbol": e.get("symbol"),
                "source": "ledger_funding_event",
            })
        return out

    # ── 对外查询 ──────────────────────────────────────────────────────────
    def trade_by_id(self, trade_id: str) -> Optional[dict]:
        for r in self.trade_rows:
            if r.get("trade_id") == trade_id:
                return r
        return None

    def orders_of(self, trade_id: str) -> List[dict]:
        """该笔交易关联的订单（真实 order_id 直连；不用行号）。"""
        out: List[dict] = []
        for r in self.trade_rows:
            if r.get("trade_id") != trade_id:
                continue
            for oid in (r.get("entry_order_id"), r.get("exit_order_id")):
                o = self._order_by_id.get(str(oid)) if oid else None
                if o is not None:
                    row = dict(o)
                    row.setdefault("run_id", self.run_id)
                    row["trade_id"] = trade_id
                    row["trade_link"] = "trades.order_id"
                    row["ts_ms"] = _int_or_none(
                        o.get("created_at") or o.get("submitted_at")
                        or o.get("filled_at") or o.get("ts_ms"))
                    row.setdefault("price", o.get("fill_price"))
                    row.setdefault("qty", o.get("quantity"))
                    out.append(row)
        return out

    def funding_of(self, trade_id: str) -> List[dict]:
        return [f for f in self.funding_rows if f.get("trade_id") == trade_id]

    def all_orders(self) -> List[dict]:
        """**全局**订单明细（真实 `order_id` + 真实归属 `trade_id`）。

        `orders.jsonl` 本身不带 `trade_id`，也**不带 `ts_ms`**（真实时刻在
        `created_at`/`submitted_at`/`filled_at`）。这里按真实 `order_id` 反查归属，
        并补齐 `ts_ms`；关联不上 ⇒ `trade_id=None` + `trade_link="unlinked"`，
        **绝不按顺序套用第 N 笔**。
        """
        out: List[dict] = []
        for o in self.orders:
            oid = o.get("order_id")
            row = dict(o)
            row.setdefault("run_id", self.run_id)
            tid = self._trade_by_order.get(str(oid)) if oid else None
            row["trade_id"] = tid
            row["trade_link"] = ("trades.order_id" if tid else "unlinked")
            row["ts_ms"] = _int_or_none(
                o.get("created_at") or o.get("submitted_at")
                or o.get("filled_at") or o.get("ts_ms"))
            row.setdefault("price", o.get("fill_price"))
            row.setdefault("qty", o.get("quantity"))
            out.append(row)
        return out

    def funding_cross_check(self, trade_id: str) -> dict:
        """交叉校验：Σ(−delta) 是否等于该笔 `funding_fee`。

        符号约定：账本 `funding_fee` 正数 = 成本；`funding.jsonl` 的 `delta` 负数 = 扣款。
        """
        row = self.trade_by_id(trade_id) or {}
        rows = self.funding_of(trade_id)
        got = -sum(float(f["amount"]) for f in rows if f.get("amount") is not None)
        want = row.get("funding_fee")
        ok = (want is not None and abs(got - float(want)) <= 1e-9)
        return {"trade_id": trade_id, "events": len(rows),
                "sum_neg_delta": got,
                "ledger_funding_fee": (float(want) if want is not None else None),
                "match": ok}


def load_ledger_facts(run_dir, run_id: str) -> LedgerFacts:
    return LedgerFacts(Path(run_dir), run_id)
