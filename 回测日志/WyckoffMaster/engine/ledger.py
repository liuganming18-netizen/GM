# -*- coding: utf-8 -*-
"""v3.1 回测账本（SQLite）· 逐笔信号/成交/费用/资金费/权益。

口径（与施工单「交易与资金费执行」逐条对齐）：
  · 毛利 gross_pnl      = (exit_price − entry_price) × qty            （仅价差）
  · 费用 fee            = entry_notional×fee + exit_notional×fee      （双边每边 0.05%）
  · 滑点 slippage_cost  = qty×(raw_price − fill_price) 方向均为不利
  · 资金费 funding      = Σ qty × mark × rate（多头正率支出 ⇒ 负号；负率收入保留）
  · 净利 net_pnl        = gross_pnl − entry_fee − exit_fee + funding  （= 该笔累计现金变动）
四者**分列**，任一可由其余三项独立复算（对账用）。

写纪律：本模块只接受 paths.WRITE_ROOT 内的路径。
性能：`commit_every` 控制提交频率；默认 1（每写一行提交）用于正确性基线，
      >1 时批量提交（优化态），两者必须产出**逐位一致**的账本。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import paths as P

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  run_id TEXT, key TEXT, value TEXT, PRIMARY KEY(run_id, key));
CREATE TABLE IF NOT EXISTS signals (
  run_id TEXT, symbol TEXT, signal_time INTEGER, entry_bar_time INTEGER,
  kind TEXT, atr REAL, struct_low REAL, ref_stop REAL, ref_target REAL,
  decision TEXT, reject_reason TEXT, rank_key TEXT);
CREATE TABLE IF NOT EXISTS orders (
  run_id TEXT, intent_id TEXT, symbol TEXT, side TEXT, status TEXT, ts INTEGER,
  raw_price REAL, fill_price REAL, qty REAL, fee REAL, slippage REAL,
  notional REAL, reason TEXT, position_id TEXT, stage TEXT);
CREATE TABLE IF NOT EXISTS positions (
  run_id TEXT, position_id TEXT, symbol TEXT,
  entry_time INTEGER, entry_raw REAL, entry_price REAL, quantity REAL, notional REAL,
  stop_price REAL, stop_atr_mult REAL, entry_fee REAL, entry_slippage_cost REAL,
  equity_at_entry REAL, rank_key TEXT,
  funding REAL, funding_events INTEGER,
  exit_time INTEGER, exit_raw REAL, exit_price REAL, exit_reason TEXT,
  exit_fee REAL, exit_slippage_cost REAL,
  gross_pnl REAL, net_pnl REAL, holding_hours REAL);
CREATE TABLE IF NOT EXISTS funding (
  run_id TEXT, position_id TEXT, funding_time INTEGER, rate REAL, mark REAL,
  mark_source TEXT, quantity REAL, amount REAL);
CREATE TABLE IF NOT EXISTS equity (
  run_id TEXT, ts INTEGER, cash REAL, committed REAL, gross_long_notional REAL,
  realized REAL, unrealized REAL, equity REAL, peak REAL, drawdown REAL,
  halted INTEGER, n_positions INTEGER);
CREATE TABLE IF NOT EXISTS review (
  run_id TEXT, segment TEXT, level TEXT, timeframe TEXT,
  initial_equity REAL, final_cash REAL, final_equity REAL,
  trades INTEGER, wins INTEGER, losses INTEGER, gross_pnl REAL, fees REAL,
  funding_net REAL, net_pnl REAL, max_drawdown REAL, halted INTEGER,
  code_sha TEXT, config_sha TEXT, input_sha TEXT, extra_json TEXT);
CREATE INDEX IF NOT EXISTS ix_sig ON signals(run_id, symbol, signal_time);
CREATE INDEX IF NOT EXISTS ix_pos ON positions(run_id, position_id);
CREATE INDEX IF NOT EXISTS ix_fund ON funding(run_id, position_id);
CREATE INDEX IF NOT EXISTS ix_eq ON equity(run_id, ts);
"""


class Ledger:
    def __init__(self, path: Path, *, commit_every: int = 1):
        self.path = P.assert_write_target(Path(path))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()
        self.con = sqlite3.connect(str(self.path))
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=OFF")
        self.con.executescript(SCHEMA)
        self.commit_every = max(1, int(commit_every))
        self._pending = 0
        self.counts = {}

    def _maybe_commit(self):
        self._pending += 1
        if self._pending >= self.commit_every:
            self.con.commit()
            self._pending = 0

    def _ins(self, table, cols, row):
        self.con.execute(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", row)
        self.counts[table] = self.counts.get(table, 0) + 1
        self._maybe_commit()

    def meta(self, run_id, key, value):
        self.con.execute("INSERT OR REPLACE INTO meta VALUES (?,?,?)", (run_id, key, str(value)))
        self._maybe_commit()

    def signal(self, run_id, symbol, signal_time, entry_bar_time, kind, atr, struct_low,
               ref_stop, ref_target, decision, reject_reason, rank_key):
        self._ins("signals",
                  ["run_id", "symbol", "signal_time", "entry_bar_time", "kind", "atr",
                   "struct_low", "ref_stop", "ref_target", "decision", "reject_reason", "rank_key"],
                  [run_id, symbol, int(signal_time), entry_bar_time, kind, atr, struct_low,
                   ref_stop, ref_target, decision, reject_reason, rank_key])

    def order(self, run_id, intent_id, symbol, side, status, ts, raw_price, fill_price, qty,
              fee, slippage, notional, reason, position_id, stage):
        self._ins("orders",
                  ["run_id", "intent_id", "symbol", "side", "status", "ts", "raw_price",
                   "fill_price", "qty", "fee", "slippage", "notional", "reason", "position_id",
                   "stage"],
                  [run_id, intent_id, symbol, side, status, int(ts), raw_price, fill_price, qty,
                   fee, slippage, notional, reason, position_id, stage])

    def position(self, run_id, **kw):
        cols = ["run_id", "position_id", "symbol", "entry_time", "entry_raw", "entry_price",
                "quantity", "notional", "stop_price", "stop_atr_mult", "entry_fee",
                "entry_slippage_cost", "equity_at_entry", "rank_key", "funding", "funding_events",
                "exit_time", "exit_raw", "exit_price", "exit_reason", "exit_fee",
                "exit_slippage_cost", "gross_pnl", "net_pnl", "holding_hours"]
        self._ins("positions", cols, [run_id] + [kw.get(c) for c in cols[1:]])

    def funding(self, run_id, position_id, funding_time, rate, mark, mark_source, quantity, amount):
        self._ins("funding",
                  ["run_id", "position_id", "funding_time", "rate", "mark", "mark_source",
                   "quantity", "amount"],
                  [run_id, position_id, int(funding_time), rate, mark, mark_source, quantity, amount])

    def equity(self, run_id, ts, cash, committed, gross_long_notional, realized, unrealized,
               equity, peak, drawdown, halted, n_positions):
        self._ins("equity",
                  ["run_id", "ts", "cash", "committed", "gross_long_notional", "realized",
                   "unrealized", "equity", "peak", "drawdown", "halted", "n_positions"],
                  [run_id, int(ts), cash, committed, gross_long_notional, realized, unrealized,
                   equity, peak, drawdown, int(halted), n_positions])

    def review(self, run_id, **kw):
        cols = ["run_id", "segment", "level", "timeframe", "initial_equity", "final_cash",
                "final_equity", "trades", "wins", "losses", "gross_pnl", "fees", "funding_net",
                "net_pnl", "max_drawdown", "halted", "code_sha", "config_sha", "input_sha",
                "extra_json"]
        self._ins("review", cols, [run_id] + [kw.get(c) for c in cols[1:]])

    def flush(self):
        self.con.commit()
        self._pending = 0

    def close(self):
        self.con.commit()
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
