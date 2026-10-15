# -*- coding: utf-8 -*-
"""F_WX 研究账本（S3a 持久化层）：signal→intent→fill/reject→成本→退出→净权益→review。

纪律：
  · 只在**隔离研究库**建表（`CREATE TABLE IF NOT EXISTS`），不碰生产行情库、不碰 `signals`。
  · 全部金额/数量按「同一事件时点、同一来源」记录；缺值写 NULL，**不用 0 冒充**。
  · 逐笔可对账：`reconcile()` 从 orders/funding/positions **重算**净额并与留存值比对，
    使用生效视图声明的容差（价格/数量相对 1e-8；权益相对 1e-6；成本/总盈亏绝对 0.01）。
  · 初始权益 5000（用户确认「共用5000u」）；同一 run 内跨所共享现金，各臂独立回放。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

# 生效视图 reconciliation 段（只读引用，不复制语义）。
TOL_PRICE_QTY_REL = 1e-8
TOL_EQUITY_REL = 1e-6
TOL_COST_ABS = 0.01

SCHEMA = """
CREATE TABLE IF NOT EXISTS research_run(
    run_id TEXT PRIMARY KEY, arm TEXT NOT NULL, config_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL, config_hash TEXT NOT NULL, data_version TEXT NOT NULL,
    engine TEXT NOT NULL, engine_version TEXT, adapter_version TEXT, code_sha256 TEXT,
    initial_equity REAL NOT NULL, created_at INTEGER NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS research_reservation(
    reservation_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL, position_id TEXT NOT NULL, amount REAL NOT NULL,
    created_at INTEGER NOT NULL, released_at INTEGER, UNIQUE(run_id, position_id));
CREATE TABLE IF NOT EXISTS research_signal(
    run_id TEXT NOT NULL, event_uid TEXT NOT NULL, exchange TEXT NOT NULL, symbol TEXT NOT NULL,
    event_time INTEGER NOT NULL, confirmed_at INTEGER NOT NULL, direction TEXT,
    atr_signal_bar REAL, execution_label TEXT, source_masks TEXT, data_version TEXT,
    payload_json TEXT, PRIMARY KEY(run_id, event_uid));
CREATE TABLE IF NOT EXISTS research_intent(
    run_id TEXT NOT NULL, intent_id TEXT PRIMARY KEY, event_uid TEXT,
    exchange TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
    created_at INTEGER NOT NULL, eligible_open_time INTEGER, reason TEXT, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_order(
    run_id TEXT NOT NULL, order_id INTEGER PRIMARY KEY, intent_id TEXT, status TEXT NOT NULL,
    reject_reason TEXT, fill_time INTEGER, fill_price REAL, quantity REAL,
    fee REAL, slippage REAL, notional REAL, source TEXT,
    -- 第24 加性：保守取整/最小单可追溯（legacy 路径留 NULL）
    qty_raw REAL, qty_step_delta REAL, price_raw REAL, price_step_delta REAL,
    min_orderable_qty REAL, limits_json TEXT, rounding_spec_sha TEXT);
CREATE TABLE IF NOT EXISTS research_position(
    run_id TEXT NOT NULL, position_id TEXT PRIMARY KEY, exchange TEXT NOT NULL,
    symbol TEXT NOT NULL, entry_time INTEGER, entry_price REAL, quantity REAL, notional REAL,
    stop_price REAL, safety_exit_at INTEGER, exit_time INTEGER, exit_price REAL,
    exit_reason TEXT, gross_pnl REAL, fees REAL, funding REAL, net_pnl REAL,
    status TEXT NOT NULL, reservation_amount REAL);
CREATE TABLE IF NOT EXISTS research_funding(
    run_id TEXT NOT NULL, position_id TEXT NOT NULL, funding_time INTEGER NOT NULL,
    rate REAL, amount REAL, source TEXT, mark REAL, mark_source TEXT,
    available_at INTEGER, contract_unit TEXT, generation TEXT,
    -- 第24 加性：相邻不利费率替代可追溯（legacy 路径留 NULL）
    rate_source TEXT, rate_side TEXT, rate_alt REAL, rate_alt_time INTEGER,
    rate_selection TEXT, rate_substituted INTEGER,
    -- 第33 加性：该笔实际计费数量（实际使用凭证/逐笔审查用；legacy 路径留 NULL）
    qty REAL,
    PRIMARY KEY(run_id, position_id, funding_time));
-- 第33 新增：**逐笔双向审查**裁决（实际使用凭证 + 正向逐行 + 反向应收付；证据留档）
CREATE TABLE IF NOT EXISTS research_funding_audit(
    run_id TEXT PRIMARY KEY, ok INTEGER NOT NULL, n_expected INTEGER, n_charged INTEGER,
    voucher_id TEXT, complete_input_identity TEXT, model TEXT, problems_json TEXT,
    detail_json TEXT, created_at INTEGER);
-- 第24 新增：全损压力（**与实际成交分列**；is_actual_sell=0，绝不并入真实现金/权益）
CREATE TABLE IF NOT EXISTS research_stress_full_loss(
    run_id TEXT NOT NULL, position_id TEXT NOT NULL, exchange TEXT, symbol TEXT,
    basis TEXT NOT NULL, entry_notional REAL, known_fees REAL, known_funding REAL,
    stress_loss REAL, is_actual_sell INTEGER NOT NULL DEFAULT 0, note TEXT,
    created_at INTEGER, PRIMARY KEY(run_id, position_id, basis));
-- 第24 新增：缺口阻断记录（价格/成交额/必要标价缺口 ⇒ 禁开/停算，逐条留因）
CREATE TABLE IF NOT EXISTS research_gap_block(
    run_id TEXT NOT NULL, exchange TEXT NOT NULL, symbol TEXT NOT NULL, ts INTEGER NOT NULL,
    kind TEXT NOT NULL, detail TEXT, created_at INTEGER,
    PRIMARY KEY(run_id, exchange, symbol, ts, kind));
-- 第26 新增：严格停算「无法计算」持仓（首个缺时点起共享账户冻结；**不**写真实平仓/费用/盈亏）
CREATE TABLE IF NOT EXISTS research_uncomputable(
    run_id TEXT NOT NULL, position_id TEXT NOT NULL, exchange TEXT, symbol TEXT,
    entry_time INTEGER, gap_ts INTEGER NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
    detail TEXT, created_at INTEGER, PRIMARY KEY(run_id, position_id, gap_ts, kind));
CREATE TABLE IF NOT EXISTS research_equity(
    run_id TEXT NOT NULL, ts INTEGER NOT NULL, cash REAL, committed_margin REAL,
    unrealized REAL, equity REAL, positions INTEGER, PRIMARY KEY(run_id, ts));
CREATE TABLE IF NOT EXISTS research_review(
    run_id TEXT PRIMARY KEY, closed_trades INTEGER, wins INTEGER, losses INTEGER,
    gross_profit REAL, gross_loss REAL, total_fees REAL, total_funding REAL,
    net_profit REAL, final_equity REAL, max_drawdown REAL, halted TEXT,
    metrics_json TEXT, created_at INTEGER);
"""


def now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def create_schema(con):
    con.executescript(SCHEMA)
    # 加性迁移：旧研究库补 funding 可追溯列（整改3）。幂等，仅本地隔离库使用。
    have = {r[1] for r in con.execute("PRAGMA table_info(research_funding)")}
    for col, typ in (("mark", "REAL"), ("mark_source", "TEXT"),
                     ("available_at", "INTEGER"), ("contract_unit", "TEXT"),
                     ("generation", "TEXT"),
                     # 第24：相邻不利费率替代可追溯列
                     ("rate_source", "TEXT"), ("rate_side", "TEXT"), ("rate_alt", "REAL"),
                     ("rate_alt_time", "INTEGER"), ("rate_selection", "TEXT"),
                     ("rate_substituted", "INTEGER"),
                     # 第33：实际计费数量（凭证/逐笔审查）
                     ("qty", "REAL")):
        if col not in have:
            con.execute(f"ALTER TABLE research_funding ADD COLUMN {col} {typ}")
    # 第24：订单取整/最小单可追溯列（加性迁移，旧库补列）
    have_o = {r[1] for r in con.execute("PRAGMA table_info(research_order)")}
    for col, typ in (("qty_raw", "REAL"), ("qty_step_delta", "REAL"), ("price_raw", "REAL"),
                     ("price_step_delta", "REAL"), ("min_orderable_qty", "REAL"),
                     ("limits_json", "TEXT"), ("rounding_spec_sha", "TEXT")):
        if col not in have_o:
            con.execute(f"ALTER TABLE research_order ADD COLUMN {col} {typ}")
    con.commit()


def iso(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Ledger:
    """研究账本写入器。所有方法显式、可追溯；不隐式补值。"""

    def __init__(self, con, run_id):
        self.con = con
        self.run_id = run_id
        # order_id 是表级 INTEGER PRIMARY KEY（rowid 别名，全局唯一，非 AUTOINCREMENT）。
        # 不自行编号，交由 SQLite 分配，避免不同 run 之间重号（曾触发 UNIQUE 冲突）。
        self._order_seq = None

    # ── run / signal / intent ────────────────────────────────────────────
    def open_run(self, *, arm, config_id, strategy_version, config_hash, data_version,
                 engine, engine_version=None, adapter_version=None, code_sha256=None,
                 initial_equity=5000.0, note=None):
        self.con.execute(
            "INSERT OR REPLACE INTO research_run(run_id,arm,config_id,strategy_version,"
            "config_hash,data_version,engine,engine_version,adapter_version,code_sha256,"
            "initial_equity,created_at,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, arm, config_id, strategy_version, config_hash, data_version, engine,
             engine_version, adapter_version, code_sha256, float(initial_equity), now_ms(), note))
        self.con.commit()

    def add_signal(self, ev):
        evd = ev.get("evidence") or {}
        self.con.execute(
            "INSERT OR REPLACE INTO research_signal(run_id,event_uid,exchange,symbol,event_time,"
            "confirmed_at,direction,atr_signal_bar,execution_label,source_masks,data_version,"
            "payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, evd.get("event_uid") or f"{ev.get('symbol')}:{ev.get('event_time')}",
             ev.get("exchange"), ev.get("symbol"), ev.get("event_time"), ev.get("confirmed_at"),
             ev.get("direction"), evd.get("atr_signal_bar"), evd.get("execution_label"),
             json.dumps(evd.get("source_masks"), ensure_ascii=False),
             ev.get("data_version"), json.dumps(evd, ensure_ascii=False)))
        self.con.commit()

    def add_intent(self, *, intent_id, event_uid, exchange, symbol, side, created_at,
                   eligible_open_time, reason, status):
        self.con.execute(
            "INSERT OR REPLACE INTO research_intent(run_id,intent_id,event_uid,exchange,symbol,"
            "side,created_at,eligible_open_time,reason,status) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, intent_id, event_uid, exchange, symbol, side, created_at,
             eligible_open_time, reason, status))
        self.con.commit()

    def add_order(self, *, intent_id, status, reject_reason=None, fill_time=None,
                  fill_price=None, quantity=None, fee=None, slippage=None, notional=None,
                  source="market", qty_raw=None, qty_step_delta=None, price_raw=None,
                  price_step_delta=None, min_orderable_qty=None, limits=None,
                  rounding_spec_sha=None):
        # 不显式传 order_id：由 SQLite 作为 rowid 分配，跨 run 全局唯一。
        # 第24 加性：qty_raw/price_raw 等取整明细可选；legacy 调用留 NULL。
        limits_json = (json.dumps(limits, ensure_ascii=False, sort_keys=True)
                       if limits is not None else None)
        cur = self.con.execute(
            "INSERT INTO research_order(run_id,intent_id,status,reject_reason,fill_time,"
            "fill_price,quantity,fee,slippage,notional,source,qty_raw,qty_step_delta,"
            "price_raw,price_step_delta,min_orderable_qty,limits_json,rounding_spec_sha) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, intent_id, status, reject_reason, fill_time,
             fill_price, quantity, fee, slippage, notional, source,
             qty_raw, qty_step_delta, price_raw, price_step_delta, min_orderable_qty,
             limits_json, rounding_spec_sha))
        self.con.commit()
        return cur.lastrowid

    def add_position(self, p):
        self.con.execute(
            "INSERT OR REPLACE INTO research_position(run_id,position_id,exchange,symbol,"
            "entry_time,entry_price,quantity,notional,stop_price,safety_exit_at,exit_time,"
            "exit_price,exit_reason,gross_pnl,fees,funding,net_pnl,status,reservation_amount) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, p["position_id"], p["exchange"], p["symbol"], p.get("entry_time"),
             p.get("entry_price"), p.get("quantity"), p.get("notional"), p.get("stop_price"),
             p.get("safety_exit_at"), p.get("exit_time"), p.get("exit_price"),
             p.get("exit_reason"), p.get("gross_pnl"), p.get("fees"), p.get("funding"),
             p.get("net_pnl"), p["status"], p.get("reservation_amount")))
        self.con.commit()

    def add_funding(self, *, position_id, funding_time, rate, amount, source,
                    mark=None, mark_source=None, available_at=None,
                    contract_unit=None, generation=None,
                    rate_source=None, rate_side=None, rate_alt=None, rate_alt_time=None,
                    rate_selection=None, rate_substituted=None, qty=None):
        # 第24 加性：相邻不利费率替代的追溯列可选；legacy 调用留 NULL。
        # 第33 加性：`qty` = 该笔实际计费数量（实际使用凭证/逐笔审查用；legacy 调用留 NULL）。
        self.con.execute(
            "INSERT OR REPLACE INTO research_funding(run_id,position_id,funding_time,rate,amount,"
            "source,mark,mark_source,available_at,contract_unit,generation,rate_source,rate_side,"
            "rate_alt,rate_alt_time,rate_selection,rate_substituted,qty) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, position_id, int(funding_time), rate, amount, source,
             mark, mark_source, available_at, contract_unit, generation,
             rate_source, rate_side, rate_alt, rate_alt_time, rate_selection,
             None if rate_substituted is None else int(bool(rate_substituted)), qty))
        self.con.commit()

    def add_funding_audit(self, *, ok, n_expected=None, n_charged=None, voucher_id=None,
                          complete_input_identity=None, model=None, problems=None, detail=None):
        """第33：**逐笔双向审查**裁决持久化（证据留档；run 级一行）。

        `ok` = 正向逐行 ∧ 反向应收付集合 ∧ 凭证可用；`problems` / `detail` 为 JSON 文本。
        """
        self.con.execute(
            "INSERT OR REPLACE INTO research_funding_audit(run_id,ok,n_expected,n_charged,"
            "voucher_id,complete_input_identity,model,problems_json,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, 1 if ok else 0, n_expected, n_charged, voucher_id,
             complete_input_identity, model,
             json.dumps(problems, ensure_ascii=False) if problems is not None else None,
             detail, now_ms()))
        self.con.commit()

    def add_stress_full_loss(self, *, position_id, basis, exchange=None, symbol=None,
                             entry_notional=None, known_fees=None, known_funding=None,
                             stress_loss=None, note=None):
        """第24：全损压力**独立入账**（与实际成交分列；`is_actual_sell` 恒 0）。

        绝不并入 `research_position` / `research_equity` / `reconcile` 的真实现金与盈亏。
        """
        self.con.execute(
            "INSERT OR REPLACE INTO research_stress_full_loss(run_id,position_id,exchange,symbol,"
            "basis,entry_notional,known_fees,known_funding,stress_loss,is_actual_sell,note,"
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, position_id, exchange, symbol, basis, entry_notional, known_fees,
             known_funding, stress_loss, 0, note, now_ms()))
        self.con.commit()

    def add_gap_block(self, *, exchange, symbol, ts, kind, detail=None):
        """第24：缺口阻断记录（价格/成交额/必要标价缺口 ⇒ 禁开/停算，逐条留因）。"""
        self.con.execute(
            "INSERT OR REPLACE INTO research_gap_block(run_id,exchange,symbol,ts,kind,detail,"
            "created_at) VALUES(?,?,?,?,?,?,?)",
            (self.run_id, exchange, symbol, int(ts), kind, detail, now_ms()))
        self.con.commit()

    def add_uncomputable(self, *, position_id, exchange=None, symbol=None, entry_time=None,
                         gap_ts=None, kind=None, status="uncomputable", detail=None):
        """第26：严格停算「无法计算」持仓记录（首个缺时点起共享账户冻结）。

        **不**写真实平仓成交 / 费用 / 盈亏；`status` 为「不可算」状态字段（恒 "uncomputable"）。
        """
        self.con.execute(
            "INSERT OR REPLACE INTO research_uncomputable(run_id,position_id,exchange,symbol,"
            "entry_time,gap_ts,kind,status,detail,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, position_id, exchange, symbol, entry_time, int(gap_ts), kind,
             status, detail, now_ms()))
        self.con.commit()

    def add_equity(self, *, ts, cash, committed_margin, unrealized, equity, positions):
        self.con.execute(
            "INSERT OR REPLACE INTO research_equity(run_id,ts,cash,committed_margin,unrealized,"
            "equity,positions) VALUES(?,?,?,?,?,?,?)",
            (self.run_id, int(ts), cash, committed_margin, unrealized, equity, positions))
        self.con.commit()

    def add_review(self, *, closed_trades, wins, losses, gross_profit, gross_loss,
                   total_fees, total_funding, net_profit, final_equity, max_drawdown,
                   halted, metrics):
        self.con.execute(
            "INSERT OR REPLACE INTO research_review(run_id,closed_trades,wins,losses,gross_profit,"
            "gross_loss,total_fees,total_funding,net_profit,final_equity,max_drawdown,halted,"
            "metrics_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, closed_trades, wins, losses, gross_profit, gross_loss, total_fees,
             total_funding, net_profit, final_equity, max_drawdown, halted,
             json.dumps(metrics, ensure_ascii=False), now_ms()))
        self.con.commit()

    # ── 原子预占（共享资金协调）─────────────────────────────────────────
    def try_reserve(self, *, position_id, amount, cash_available, created_at, total_cash=None):
        """原子预占：`BEGIN IMMEDIATE` 下核对可用现金并写入唯一预占行。

        **现金口径（E06 修复，只扣一次 held）**：
          · `cash_available` = 调用方口径的**可用（free）现金**，即**已扣除当前 held** 的余额。
            ⇒ 本函数**不再**二次扣减 held（旧实现 `held + amount > cash_available` 把 held
            扣了两次 ⇒ held6000、真实 free4000、新增1000 被误拒）。
          · 若显式给出 `total_cash`（**总现金**），则在**事务内**重算 `free = total_cash - held`
            并以之为准（不依赖调用方陈旧余额）。
          · 事务内重读持久化预占（`held`）、复核唯一性；并发/重复 position_id ⇒ 唯一约束 ⇒ 拒绝。

        返回 (ok, reason)。`reason` 明确区分 `duplicate_reservation` / `insufficient_free_cash`。
        """
        try:
            self.con.execute("BEGIN IMMEDIATE")
            dup = self.con.execute(
                "SELECT 1 FROM research_reservation WHERE run_id=? AND position_id=?",
                (self.run_id, position_id)).fetchone()
            if dup:
                self.con.execute("ROLLBACK")
                return False, "duplicate_reservation"
            held = self.con.execute(
                "SELECT COALESCE(SUM(amount),0) FROM research_reservation WHERE run_id=? "
                "AND released_at IS NULL", (self.run_id,)).fetchone()[0]
            if total_cash is not None:
                free = float(total_cash) - float(held)     # 事务内重算，不依赖陈旧传入余额
                basis = "total_cash_minus_held"
            else:
                free = float(cash_available)               # 已是 free，held 只扣一次
                basis = "free_cash_as_given"
            if amount > free + 1e-9:
                self.con.execute("ROLLBACK")
                return False, "insufficient_free_cash"
            self.con.execute(
                "INSERT INTO research_reservation(run_id,position_id,amount,created_at) "
                "VALUES(?,?,?,?)", (self.run_id, position_id, float(amount), int(created_at)))
            self.con.execute("COMMIT")
            self._last_reserve_basis = {"basis": basis, "held_before": float(held),
                                        "free_used": float(free)}
            return True, None
        except sqlite3.Error as exc:  # noqa: BLE001
            try:
                self.con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return False, f"reservation_error:{exc}"

    def release(self, *, position_id, released_at):
        self.con.execute(
            "UPDATE research_reservation SET released_at=? WHERE run_id=? AND position_id=?",
            (int(released_at), self.run_id, position_id))
        self.con.commit()

    def held_amount(self):
        return self.con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM research_reservation WHERE run_id=? "
            "AND released_at IS NULL", (self.run_id,)).fetchone()[0]


def reconcile(con, run_id):
    """**独立逐笔对账**：从**实际成交（orders.fill_price/quantity）**独立重算 gross/net。

    E08 修复（2026-10-11）：旧实现直接信任 `research_position.gross_pnl`（stored），
    因此「orders 价格/数量不变、只把 stored gross/net 与末日 equity 各 +100」也能全绿。
    现在：
      · **gross 只由成交重算**：取该 position 的入场/出场 `research_order`（status='fill'）
        的 `fill_price` 与 `quantity` 重算 `(exit_fill_price - entry_fill_price) * qty`；
        缺失任一成交 ⇒ `gross_recomputed=None` ⇒ fail closed。
      · stored `gross_pnl` 只作为**被比对值**，绝不作为输入。
      · `net = gross_recomputed - Σfee(fill) + Σfunding`；与 stored net 比对。
      · 总账：`final_equity - initial == Σ gross_recomputed - Σfee + Σfunding`。
    返回 {run_id, trades:[...], totals:{...}, checks:{...}, ok}。不使用任何留存汇总值作为输入。
    """
    pos = con.execute("SELECT * FROM research_position WHERE run_id=?", (run_id,)).fetchall()
    ords = con.execute("SELECT * FROM research_order WHERE run_id=? ORDER BY order_id",
                       (run_id,)).fetchall()
    fnd = con.execute("SELECT * FROM research_funding WHERE run_id=?", (run_id,)).fetchall()
    eq = con.execute("SELECT * FROM research_equity WHERE run_id=? ORDER BY ts",
                     (run_id,)).fetchall()
    run = con.execute("SELECT * FROM research_run WHERE run_id=?", (run_id,)).fetchone()

    fills_by_intent, fees_by_intent = {}, {}
    for o in ords:
        if o["status"] == "fill":
            fills_by_intent.setdefault(o["intent_id"], []).append(o)
            if o["fee"] is not None:
                fees_by_intent[o["intent_id"]] = fees_by_intent.get(o["intent_id"], 0.0) + o["fee"]
    funding_by_pos = {}
    for f in fnd:
        funding_by_pos[f["position_id"]] = funding_by_pos.get(f["position_id"], 0.0) + (f["amount"] or 0.0)
    # 缺 event mark 被阻断时写 amount=NULL：**fail closed**（不得当 0 混入汇总）
    null_funding = [{"position_id": f["position_id"], "funding_time": f["funding_time"],
                     "source": f["source"]} for f in fnd if f["amount"] is None]

    trades, tot_fee, tot_fund, gross = [], 0.0, 0.0, 0.0
    all_gross_ok, all_net_ok = True, True
    for p in pos:
        if p["status"] != "closed":
            continue
        pid = p["position_id"]
        fl = fills_by_intent.get(pid, [])
        fsum = funding_by_pos.get(pid, 0.0)
        osum = fees_by_intent.get(pid, 0.0)
        gross_recomputed, entry_fill, exit_fill = None, None, None
        if len(fl) >= 2:
            entry_fill, exit_fill = fl[0], fl[-1]
            qty = entry_fill["quantity"] if entry_fill["quantity"] is not None else exit_fill["quantity"]
            if (entry_fill["fill_price"] is not None and exit_fill["fill_price"] is not None
                    and qty):
                gross_recomputed = (float(exit_fill["fill_price"])
                                    - float(entry_fill["fill_price"])) * float(qty)
        gross_ok = (gross_recomputed is not None and abs(
            gross_recomputed - (p["gross_pnl"] or 0.0)) <= max(
            TOL_COST_ABS, abs(gross_recomputed) * TOL_PRICE_QTY_REL))
        net_recomputed = (gross_recomputed - osum + fsum) if gross_recomputed is not None else None
        net_ok = (net_recomputed is not None and abs(
            net_recomputed - (p["net_pnl"] or 0.0)) <= max(
            TOL_COST_ABS, abs(net_recomputed) * TOL_PRICE_QTY_REL))
        all_gross_ok = all_gross_ok and gross_ok
        all_net_ok = all_net_ok and net_ok
        trades.append({"position_id": pid, "symbol": p["symbol"],
                       "entry_fill_price": (entry_fill["fill_price"] if entry_fill else None),
                       "exit_fill_price": (exit_fill["fill_price"] if exit_fill else None),
                       "quantity": (entry_fill["quantity"] if entry_fill else None),
                       "gross_pnl_stored": p["gross_pnl"],
                       "gross_pnl_recomputed": gross_recomputed, "gross_ok": gross_ok,
                       "fees_recomputed": osum, "funding_recomputed": fsum,
                       "net_pnl_stored": p["net_pnl"], "net_pnl_recomputed": net_recomputed,
                       "net_ok": net_ok, "exit_reason": p["exit_reason"],
                       "recompute_source": "orders_fill_price_x_quantity"})
        if gross_recomputed is not None:
            gross += gross_recomputed
        tot_fee += osum
        tot_fund += fsum

    init = run["initial_equity"] if run else None
    final_eq = eq[-1]["equity"] if eq else None
    net_profit = (final_eq - init) if (final_eq is not None and init is not None) else None
    net_from_components = gross - tot_fee + tot_fund
    net_profit_check = None
    if net_profit is not None:
        net_profit_check = abs(net_profit - net_from_components) <= TOL_COST_ABS
    peak, mdd = None, 0.0
    for e in eq:
        v = e["equity"]
        if v is None:
            continue
        peak = v if peak is None else max(peak, v)
        if peak and peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    ok = bool(all_gross_ok and all_net_ok and not null_funding
              and (net_profit_check is not False)
              and (net_profit_check is True or not trades))
    return {
        "run_id": run_id,
        "initial_equity": init,
        "final_equity": final_eq,
        "trades": trades,
        "totals": {"closed_trades": len(trades), "gross_pnl": gross,
                   "fees_recomputed": tot_fee, "funding_recomputed": tot_fund,
                   "net_profit": net_profit, "net_profit_from_components": net_from_components,
                   "max_drawdown_recomputed": mdd},
        "checks": {"per_trade_gross_ok": all_gross_ok, "per_trade_net_ok": all_net_ok,
                   "net_profit_ok": net_profit_check,
                   "null_funding_amounts": null_funding,
                   "gross_source": "independent_recompute_from_orders",
                   "tolerances": {"price_qty_rel": TOL_PRICE_QTY_REL,
                                  "equity_rel": TOL_EQUITY_REL, "cost_abs": TOL_COST_ABS}},
        "ok": ok,
    }
