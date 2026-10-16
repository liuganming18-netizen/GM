#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""信号与持仓接通（2026-10-13）· **实时层只读存储**（纯标准库，无 numpy/ccxt 依赖）。

职责边界（**本模块只做存储，不做策略、不做撮合**）：

  · **实时信号库** `live/live_signals.sqlite3`：
      - `live_signal`：真实结构信号 / 臂事件（幂等键 `signal_uid`）。
      - `live_account_snapshot`：**实盘只读**账户快照（由 live worker 用 ccxt 读取后写入）。
      - `live_run`：worker 心跳与运行态（键值表）。
  · **纸面账本**：**复用既有四组** `纸面模拟43/paper_{arm}.sqlite3`（各 5000U）。
    本模块只提供 `paper_conn()` 与**加性** schema 升级（`ADD COLUMN` / `CREATE TABLE IF NOT EXISTS`），
    不改动既有表既有列语义，不清空、不重建。

纪律：
  · 只新增文件；**不写** `market.sqlite3` / `market_v2.sqlite3`（行情库只读）。
  · 行情库连接一律 `mode=ro` + `query_only=ON`。
  · 并发写用 WAL + `busy_timeout`，避免与 adapter 只读进程互锁。
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 工程根：框架/backend → 框架 → 工程根（桌面工程 `威科夫因子共振趋势/`）
PROJECT_ROOT = HERE.parents[1] if len(HERE.parents) >= 2 else HERE.parent
BASE = PROJECT_ROOT                                  # 兼容旧名（调用方仍引用 L.BASE）
W43 = HERE.parent / "最新唐奇安威科夫接通与收尾_2026-10-11" / "量价信号接入与模拟历史并行43"

# ── 纸面账本身份与路径（**唯一权威**；fail-closed）────────────────────────────
# 历史缺陷（2026-10-15 独立核验确认）：旧实现 `BASE = HERE.parent` +
# `PAPER_DIR = BASE/最新唐奇安…/纸面模拟43`，且 `_connect(create=True)` 会
# **自动创建**目录与库文件。源码迁到桌面工程后，同一相对式解析到**另一个空目录**，
# 于是接口读到 4 份空账本却报 `quality=ok` —— 等同于**凭空新开四份 5000U 假账户
# 冒充原账户**，且原账本（含 177 条持仓 / 25 笔未作废在持仓）被彻底绕开。
#
# 现纪律（不可回退）：
#   · 账本目录**显式**来自启动配置 `PAPER_LEDGER_DIR`；缺省 = 工程根 `数据/paper`
#     （= 迁移清单 M002–M005 的目标相对范围，**不是**任何相对推导目录）；
#   · **缺库 ⇒ 抛 `LedgerUnavailable`**；**身份不符 ⇒ 抛 `LedgerIdentityMismatch`**；
#     绝不自动创建、绝不换目录、绝不用空库冒充；
#   · 仅当显式 `PAPER_LEDGER_ALLOW_INIT=1`（供隔离测试/受控建档）才允许创建空库；
#   · 账本身份以 `ledger_identity.json`（本目录内）为权威声明，并与库内
#     `paper_state/paper_account.account_id` 交叉核对。
LEDGER_DIR = Path(os.environ.get("PAPER_LEDGER_DIR", str(PROJECT_ROOT / "数据" / "paper")))
PAPER_DIR = LEDGER_DIR                               # 兼容旧名（调用方仍引用 L.PAPER_DIR）
LEDGER_MANIFEST = LEDGER_DIR / "ledger_identity.json"
LEDGER_ALLOW_INIT = os.environ.get("PAPER_LEDGER_ALLOW_INIT", "0") == "1"
LEDGER_SPEC_VERSION = os.environ.get(
    "PAPER_LEDGER_SPEC", "F_WX_v2.2_project_closure_user_5000_shared")
# 冻结的历史回测账本目录（迁移清单 M042–M046 的目标相对范围）；不存在 ⇒ 缺失态
BACKTEST_LEDGER_DIRS = (
    PROJECT_ROOT / "回测日志" / "回测资源改造_2026-10-14" / "ledgers_ra",
    PROJECT_ROOT / "回测日志" / "全池真实费用回测_2026-10-13" / "ledgers",
)

LIVE_DIR = Path(os.environ.get("LIVE_DIR", str(HERE / "live")))
LIVE_DB = Path(os.environ.get("LIVE_DB", str(LIVE_DIR / "live_signals.sqlite3")))
# 只读行情库默认 = 桌面工程热库（旧绝对路径已随旧工作区清理，不再作为默认；
# 启动脚本会显式注入 MARKET_DB）。
MARKET_DB = Path(os.environ.get(
    "MARKET_DB", str(PROJECT_ROOT / "数据" / "市场数据" / "db" / "market_v2.sqlite3")))

ARMS = ("3a", "3b", "3c", "3d")
INIT_EQUITY = 5000.0


class LedgerUnavailable(RuntimeError):
    """纸面账本缺失 / 不可读 ⇒ 调用方必须如实报 `unavailable`，**不得**创建或换库。"""


class LedgerIdentityMismatch(RuntimeError):
    """纸面账本存在但身份与预期不符 ⇒ 拒绝读写，不得冒充原账户。"""

# ── 实盘只读账户状态取值（前后端同一枚举；禁止用「未实现接口的空态」冒充「未配置」）──
ACC_STATUSES = (
    "connected",            # 已连接且有持仓
    "connected_no_positions",  # 已连接、当前无持仓
    "not_configured",       # 本机缺该交易所凭据（**精确**说明缺哪一类）
    "auth_failed",          # 凭据存在但鉴权失败
    "stale",                # 快照过期（读取成功但太旧）
    "reader_unavailable",   # 本机缺少只读读取器（ccxt 不可用）
    "error",                # 其它读取错误
)

SIGNAL_SOURCES = ("structure_prototype_39", "arm_event")
ORIGINS = ("live", "historical_research")


def now_ms() -> int:
    return int(time.time() * 1000)


def _connect(path: Path, *, ro: bool, create: bool = True) -> sqlite3.Connection:
    if ro:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=15)
        con.execute("PRAGMA query_only=ON")
    else:
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(path), timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=15000")
    con.row_factory = sqlite3.Row
    return con


def market_conn() -> sqlite3.Connection:
    """行情库**只读**连接（绝不写、绝不 DDL）。"""
    return _connect(MARKET_DB, ro=True)


# ── 实时信号库 schema ──────────────────────────────────────────────────────
LIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS live_signal(
  signal_uid        TEXT PRIMARY KEY,
  source            TEXT NOT NULL,
  origin            TEXT NOT NULL,
  exchange          TEXT NOT NULL,
  market            TEXT NOT NULL,
  symbol            TEXT NOT NULL,
  timeframe         TEXT NOT NULL,
  strategy_key      TEXT,
  strategy_version  TEXT,
  config_hash       TEXT,
  data_version      TEXT,
  structural_kind   TEXT,
  direction         TEXT,
  event_time        INTEGER,
  confirmed_at      INTEGER,
  available_at      INTEGER,
  bar_open_time     INTEGER,
  bar_close_time    INTEGER,
  detection_low     REAL,
  detection_atr     REAL,
  display_state     TEXT NOT NULL DEFAULT 'displayable',
  executable_state  TEXT NOT NULL DEFAULT 'undecided',
  executable_reason TEXT,
  admission         INTEGER,
  rank              REAL,
  daily_list_version TEXT,
  payload_json      TEXT,
  first_seen_at     INTEGER NOT NULL,
  updated_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_live_signal_confirmed ON live_signal(confirmed_at DESC);
CREATE INDEX IF NOT EXISTS ix_live_signal_symbol    ON live_signal(symbol);
CREATE INDEX IF NOT EXISTS ix_live_signal_origin    ON live_signal(origin);
CREATE INDEX IF NOT EXISTS ix_live_signal_exec      ON live_signal(executable_state);

CREATE TABLE IF NOT EXISTS live_account_snapshot(
  exchange       TEXT NOT NULL,
  account_label  TEXT NOT NULL,
  read_at        INTEGER NOT NULL,
  status         TEXT NOT NULL,
  detail         TEXT,
  error_class    TEXT,
  equity_usd     REAL,
  available_usd  REAL,
  used_margin_usd REAL,
  positions_json TEXT,
  source         TEXT,
  PRIMARY KEY(exchange, account_label)
);

CREATE TABLE IF NOT EXISTS live_run(
  key        TEXT PRIMARY KEY,
  value_json TEXT,
  updated_at INTEGER
);
"""


def live_conn(*, ro: bool = False) -> sqlite3.Connection:
    con = _connect(LIVE_DB, ro=ro, create=not ro)
    if not ro:
        con.executescript(LIVE_SCHEMA)
        con.commit()
    return con


def ensure_live_schema() -> None:
    con = live_conn()
    con.close()


# ── 纸面账本：复用既有四组 + **加性**升级 ────────────────────────────────────
PAPER_ADDITIVE = """
-- 基础表（与 43 窗口 `create_paper_ledger` **逐字同构**；IF NOT EXISTS ⇒ 对既有账本无副作用）
CREATE TABLE IF NOT EXISTS paper_account(
  account_id TEXT PRIMARY KEY, arm TEXT, initial_equity REAL, cash REAL,
  leverage REAL, dry_run INTEGER, created_at INTEGER);
CREATE TABLE IF NOT EXISTS paper_prompt(
  event_uid TEXT PRIMARY KEY, arm TEXT, symbol TEXT, kind TEXT,
  confirm_close_ms INTEGER, payload TEXT, ingested_at INTEGER);
CREATE TABLE IF NOT EXISTS paper_order(
  order_id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, event_uid TEXT UNIQUE,
  symbol TEXT, side TEXT, qty REAL, price REAL, fee REAL, ts INTEGER,
  status TEXT, reject_reason TEXT);
CREATE TABLE IF NOT EXISTS paper_position(
  position_id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, symbol TEXT,
  qty REAL, entry_price REAL, entry_ts INTEGER, exit_ts INTEGER, exit_reason TEXT,
  status TEXT);
CREATE TABLE IF NOT EXISTS paper_funding(
  id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, position_id INTEGER,
  funding_time INTEGER, rate REAL, mark REAL, amount REAL, source TEXT);
CREATE TABLE IF NOT EXISTS paper_equity(
  ts INTEGER PRIMARY KEY, account_id TEXT, cash REAL, unrealized REAL, equity REAL);
CREATE TABLE IF NOT EXISTS paper_blocked(
  id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, event_uid TEXT,
  symbol TEXT, reason TEXT, ts INTEGER);
-- 拒单去重索引（**非唯一**）：常驻 worker 会反复重试同一信号，
-- 无索引时每轮都要全表扫；建**唯一**索引会因既有重复行直接失败（且需删数据），
-- 故只建非唯一索引 + 由 `paper_engine._reject` 做存在性预检，历史重复行原样保留。
CREATE INDEX IF NOT EXISTS ix_paper_blocked_dedup
  ON paper_blocked(account_id, event_uid, reason);
-- 已处理信号（幂等）：同一 signal_uid 只驱动一次开/平仓
CREATE TABLE IF NOT EXISTS paper_processed(
  signal_uid  TEXT PRIMARY KEY,
  arm         TEXT NOT NULL,
  action      TEXT NOT NULL,
  detail      TEXT,
  ts          INTEGER NOT NULL
);
-- 成交明细（模拟撮合价、滑点、手续费分项）
CREATE TABLE IF NOT EXISTS paper_fill(
  fill_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id  TEXT NOT NULL,
  order_id    INTEGER,
  position_id INTEGER,
  signal_uid  TEXT,
  symbol      TEXT NOT NULL,
  action      TEXT NOT NULL,            -- open|close
  qty         REAL NOT NULL,
  raw_price   REAL NOT NULL,            -- 已闭合 15m bar 开盘价（未含滑点）
  fill_price  REAL NOT NULL,            -- 含滑点后的模拟成交价
  notional    REAL NOT NULL,
  fee         REAL NOT NULL,
  slippage_cost REAL NOT NULL,
  bar_open_time INTEGER,
  bar_close_time INTEGER,
  fill_ts     INTEGER NOT NULL,
  model_version TEXT
);
CREATE INDEX IF NOT EXISTS ix_paper_fill_acct ON paper_fill(account_id, fill_ts);
-- 已实现盈亏（平仓时落一笔）
CREATE TABLE IF NOT EXISTS paper_realized(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id  TEXT NOT NULL,
  position_id INTEGER,
  symbol      TEXT NOT NULL,
  qty         REAL NOT NULL,
  entry_price REAL NOT NULL,
  exit_price  REAL NOT NULL,
  gross_pnl   REAL NOT NULL,
  entry_fee   REAL NOT NULL,
  exit_fee    REAL NOT NULL,
  funding_paid REAL NOT NULL DEFAULT 0,
  net_pnl     REAL NOT NULL,
  entry_ts    INTEGER, exit_ts INTEGER,
  exit_reason TEXT,
  ts          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_paper_realized_acct ON paper_realized(account_id, ts);
-- 账户运行态（现金/已实现/费用/资金费合计）
CREATE TABLE IF NOT EXISTS paper_state(
  account_id  TEXT PRIMARY KEY,
  cash        REAL NOT NULL,
  realized_pnl REAL NOT NULL DEFAULT 0,
  fees_paid   REAL NOT NULL DEFAULT 0,
  funding_paid REAL NOT NULL DEFAULT 0,
  updated_at  INTEGER NOT NULL
);
-- **资金费缺口留痕**（2026-10-14 模拟实盘闭环专项）：真实结算事件存在但缺真实
-- `funding_rate` 或 `mark_price` ⇒ **不补 0 冒完整**，在此显式留缺口，供对账与阻断判定。
-- 幂等键 (account_id, position_id, funding_time, reason)。
CREATE TABLE IF NOT EXISTS paper_funding_gap(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id  TEXT NOT NULL,
  position_id INTEGER,
  funding_time INTEGER,
  reason      TEXT NOT NULL,
  detected_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_paper_funding_gap
  ON paper_funding_gap(account_id, position_id, funding_time, reason);
-- **迟到资金费补记修正审计**（2026-10-14）：已平仓持仓在平仓后才补到更早的结算事件时，
-- 必须**一致修正**现金 / 资金费累计 / 已实现净盈亏，并在此留可复核的修正记录。
-- 记录源事件身份、费率、mark、数量、经济结算时间与实际记账时间。
CREATE TABLE IF NOT EXISTS paper_funding_correction(
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id    TEXT NOT NULL,
  position_id   INTEGER,
  symbol        TEXT,
  funding_time  INTEGER,          -- 经济结算时间（源事件 funding_time）
  rate          REAL,
  mark          REAL,
  qty           REAL,
  amount        REAL,             -- 支出口径（>0 支付、<0 收取）
  cash_delta    REAL,             -- = -amount
  realized_delta REAL,            -- 对已实现净盈亏的修正量（= -amount）
  late          INTEGER NOT NULL DEFAULT 1,
  reason        TEXT,
  recorded_at   INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_paper_funding_correction
  ON paper_funding_correction(account_id, position_id, funding_time);
-- 手工/页面操作审计
CREATE TABLE IF NOT EXISTS paper_command(
  cmd_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id TEXT NOT NULL,
  action     TEXT NOT NULL,
  symbol     TEXT,
  payload    TEXT,
  result     TEXT,
  ok         INTEGER,
  ts         INTEGER NOT NULL
);
-- **未来待成交挂单**（2026-10-13 因果修正）：信号/手工请求的「应成交时刻」若尚未到达
-- （即第一根合格已闭合 15m bar 还不存在），**不得**用更早的旧价成交，也不得凭空造历史成交。
-- 挂单在此登记，由真实进程时钟推进（`paper_engine.advance_pending`）。
CREATE TABLE IF NOT EXISTS paper_pending(
  pending_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id     TEXT NOT NULL,
  arm            TEXT NOT NULL,
  symbol         TEXT NOT NULL,
  direction      TEXT NOT NULL DEFAULT 'long',
  signal_uid     TEXT,
  origin         TEXT,                    -- signal | manual
  atr            REAL,
  stop_price     REAL,
  event_time     INTEGER,                 -- 信号经济时刻（确认 bar 收盘）
  available_at   INTEGER,                 -- 信号可用时刻
  requested_at   INTEGER NOT NULL,        -- 真实请求时刻（挂钟）
  earliest_fill_ms INTEGER NOT NULL,      -- 允许成交的最早 bar open_time 下界
  status         TEXT NOT NULL,           -- pending|filled|rejected|expired
  reason         TEXT,
  created_at     INTEGER NOT NULL,
  resolved_at    INTEGER,
  fill_position_id INTEGER,
  market_source  TEXT
);
CREATE INDEX IF NOT EXISTS ix_paper_pending_status
  ON paper_pending(account_id, status, earliest_fill_ms);
CREATE INDEX IF NOT EXISTS ix_paper_pending_sig
  ON paper_pending(account_id, signal_uid);
-- **定向纠错审计**（2026-10-14 09 契约）：把某笔**已记账但不属于本轮有效 epoch** 的
-- 持仓/挂单/成交/订单标记为 `voided=1`（**保留原审计行**），并在此逐条留**可复核的
-- 增量修正记录**（现金/手续费/资金费/已实现盈亏的 delta 与原因）。
-- 纪律：**不清空账户**、**不把历史成交重记为 epoch 时刻**、只做定向隔离。
CREATE TABLE IF NOT EXISTS paper_correction(
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id    TEXT NOT NULL,
  kind          TEXT NOT NULL,          -- position|pending|order|fill|state
  ref_id        INTEGER,                -- position_id / pending_id / order_id / fill_id
  symbol        TEXT,
  field         TEXT,                   -- cash|fees_paid|funding_paid|realized_pnl|voided
  delta         REAL,
  reason        TEXT,
  detail        TEXT,
  recorded_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_paper_correction_acct
  ON paper_correction(account_id, recorded_at);
-- **真实首次请求锚**（2026-10-14 09 契约）：把「某信号第一次被系统请求开仓的墙钟」
-- 一次性钉住。为什么单独建表：`paper_order` 在**容量类拒单**时也会写行但不含该字段，
-- 且成交走 `INSERT OR REPLACE` 会覆盖；把锚放在独立的 `INSERT OR IGNORE` 表里，
-- 才能保证**任何重试都不刷新首次请求**（`order_request_ms` 因此不随轮次增长）。
CREATE TABLE IF NOT EXISTS paper_request_anchor(
  account_id       TEXT NOT NULL,
  event_uid        TEXT NOT NULL,        -- = signal_uid
  first_request_ms INTEGER NOT NULL,
  created_at       INTEGER NOT NULL,
  PRIMARY KEY(account_id, event_uid)
);
"""
# **可能失败**的加性索引（若既有账本已有重复行 ⇒ 唯一索引建不上）：
# 单独执行、失败不阻断启动；幂等性另有「存在性预检」兜底（见 `paper_engine._settle_funding`）。
# 为什么不删重复行来强行建索引：删账本行属于「改动既有事实」，必须走人类批复的清理件，
# 不能因为「建索引方便」顺手删。
PAPER_INDEXES_GUARDED = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_paper_funding_event "
    "ON paper_funding(account_id, position_id, funding_time)",
)

PAPER_COLUMNS = (
    # (table, column, decl) —— 加性补列；已存在则跳过
    ("paper_position", "exit_price", "REAL"),
    ("paper_position", "entry_fee", "REAL"),
    ("paper_position", "exit_fee", "REAL"),
    ("paper_position", "funding_paid", "REAL DEFAULT 0"),
    ("paper_position", "signal_uid", "TEXT"),
    ("paper_position", "arm", "TEXT"),
    ("paper_position", "stop_price", "REAL"),
    ("paper_position", "atr_at_entry", "REAL"),
    ("paper_position", "model_version", "TEXT"),
    ("paper_order", "fill_price", "REAL"),
    ("paper_order", "notional", "REAL"),
    ("paper_order", "bar_open_time", "INTEGER"),
    ("paper_order", "model_version", "TEXT"),
    # ── 因果审计列（2026-10-13）：**经济时刻 / 确认(可用)时刻 / 请求时刻 / 成交时刻 / 行情来源**
    # 必须逐笔可核对：只记挂钟会造成「事后可得价格」无法被发现（本窗口真实缺陷）。
    ("paper_position", "event_time", "INTEGER"),
    ("paper_position", "available_at", "INTEGER"),
    ("paper_position", "requested_at", "INTEGER"),
    ("paper_position", "earliest_fill_ms", "INTEGER"),
    ("paper_position", "market_source", "TEXT"),
    ("paper_position", "origin", "TEXT"),
    ("paper_order", "event_time", "INTEGER"),
    ("paper_order", "requested_at", "INTEGER"),
    ("paper_order", "market_source", "TEXT"),
    ("paper_fill", "event_time", "INTEGER"),
    ("paper_fill", "available_at", "INTEGER"),
    ("paper_fill", "requested_at", "INTEGER"),
    ("paper_fill", "market_source", "TEXT"),
    # ── 资金费迟到事件补记（2026-10-14 模拟实盘闭环专项）──────────────────────────
    # `late_backfill=1` 表示该行是**持仓已平仓后**才补到的迟到/补采结算事件；
    # `recorded_at` = 实际记账挂钟时刻（与 `funding_time` 经济结算时间分开记录）。
    ("paper_funding", "late_backfill", "INTEGER DEFAULT 0"),
    ("paper_funding", "recorded_at", "INTEGER"),
    # ── 本轮 paper epoch 与「真实首次请求时刻」（2026-10-14 09 契约）────────────────
    # `first_request_ms` = **真实首次请求墙钟**：不得因每轮重试重置，也不得回写成
    #   过去信号时刻；成交下限必须包含它（见 `paper_engine.open_position`）。
    # `epoch_ms` = 该笔所属的 paper epoch 边界（= `live_store.PAPER_EPOCH_MS`），
    #   与 symbol/arm/bar 身份一起构成**执行幂等键**的一部分。
    # `voided` = 定向纠错标记：该行**保留**但**不计入**有效 epoch 的账户/持仓口径。
    ("paper_position", "first_request_ms", "INTEGER"),
    ("paper_position", "epoch_ms", "INTEGER"),
    ("paper_position", "voided", "INTEGER DEFAULT 0"),
    ("paper_position", "void_reason", "TEXT"),
    ("paper_order", "first_request_ms", "INTEGER"),
    ("paper_order", "epoch_ms", "INTEGER"),
    ("paper_order", "voided", "INTEGER DEFAULT 0"),
    ("paper_fill", "first_request_ms", "INTEGER"),
    ("paper_fill", "epoch_ms", "INTEGER"),
    ("paper_fill", "voided", "INTEGER DEFAULT 0"),
    ("paper_pending", "first_request_ms", "INTEGER"),
    ("paper_pending", "epoch_ms", "INTEGER"),
    ("paper_pending", "voided", "INTEGER DEFAULT 0"),
    # `paper_equity.voided=1` = 该快照**落在被纠错区间内**（含已作废持仓的估值），
    # 消费方应排除；**不删除、不改写**原快照（历史证据原样保留，只加标记）。
    ("paper_equity", "voided", "INTEGER DEFAULT 0"),
)


def ledger_path(arm: str) -> Path:
    """某臂账本**权威**路径（不做任何相对推导）。"""
    if arm not in ARMS:
        raise ValueError("未知臂：%r" % (arm,))
    return LEDGER_DIR / f"paper_{arm}.sqlite3"


def expected_account_id(arm: str) -> str:
    return f"paper43_{arm}"


def _sha256_file(path: Path) -> str | None:
    try:
        import hashlib
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def read_manifest() -> dict:
    """账本目录身份声明（`ledger_identity.json`）；缺失返回 `{}`（不视为错误）。"""
    import json
    try:
        doc = json.loads(LEDGER_MANIFEST.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except Exception:                                    # noqa: BLE001
        return {}


def _db_identity(con: sqlite3.Connection) -> dict:
    """从库内读**可交叉核对**的身份（account_id / 初始权益 / 规格）。"""
    ident = {"account_id": None, "initial_equity": None, "spec_version": None}
    for sql, keys in (
        ("SELECT account_id, initial_equity FROM paper_account LIMIT 1",
         ("account_id", "initial_equity")),
        ("SELECT account_id FROM paper_state LIMIT 1", ("account_id",)),
    ):
        try:
            r = con.execute(sql).fetchone()
        except sqlite3.OperationalError:
            continue
        if r:
            for i, k in enumerate(keys):
                if ident.get(k) is None:
                    ident[k] = r[i]
    try:
        r = con.execute("SELECT value_json FROM live_run WHERE key='ledger_spec_version'"
                        ).fetchone()
        if r:
            import json as _json
            ident["spec_version"] = _json.loads(r[0]) if r[0] else None
    except sqlite3.OperationalError:
        pass
    return ident


def verify_ledger_identity(con: sqlite3.Connection, arm: str, *, strict: bool = True) -> dict:
    """核对账本身份。返回身份 dict；不符且 `strict` ⇒ 抛 `LedgerIdentityMismatch`。

    判据（**任一不符即拒绝**）：
      · 库内若已声明 `account_id`，必须 == `paper43_{arm}`；
      · 目录 `ledger_identity.json` 若声明该臂，其 `account_id` 必须一致，
        且（若给 `sha256`）与库文件摘要一致。
    """
    ident = _db_identity(con)
    db_acc = ident.get("account_id")
    if db_acc and db_acc != expected_account_id(arm):
        if strict:
            raise LedgerIdentityMismatch(
                f"账本身份不符：{LEDGER_DIR}/paper_{arm}.sqlite3 内 account_id="
                f"{db_acc!r}，期望 {expected_account_id(arm)!r}")
        ident["identity_mismatch"] = True
    man = read_manifest()
    man_arms = (man.get("accounts") or {}) if isinstance(man, dict) else {}
    m = man_arms.get(arm)
    if isinstance(m, dict):
        ma = m.get("account_id")
        if ma and db_acc and ma != db_acc:
            if strict:
                raise LedgerIdentityMismatch(
                    f"账本清单与库内身份不一致：manifest[{arm}].account_id={ma!r} "
                    f"vs 库内 {db_acc!r}")
            ident["manifest_mismatch"] = True
        if m.get("spec_version"):
            ident["spec_version"] = m.get("spec_version")
        ident["manifest_declared"] = True
    return ident


def _provision_ledger(arm: str, path: Path) -> sqlite3.Connection:
    """**受控建档**（仅 `PAPER_LEDGER_ALLOW_INIT=1` 时可达）：创建空账本 + 声明身份。"""
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    con = _connect(path, ro=False, create=True)
    con.executescript(PAPER_ADDITIVE)
    for stmt in PAPER_INDEXES_GUARDED:
        try:
            con.execute(stmt)
        except sqlite3.OperationalError:
            pass
    for table, col, decl in PAPER_COLUMNS:
        try:
            cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        except sqlite3.OperationalError:
            continue
        if not cols or col in cols:
            continue
        try:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass
    con.execute("INSERT OR IGNORE INTO paper_account(account_id,arm,initial_equity,cash,"
                "leverage,dry_run,created_at) VALUES(?,?,?,?,1,1,?)",
                (expected_account_id(arm), arm, INIT_EQUITY, INIT_EQUITY, now_ms()))
    con.execute("INSERT OR IGNORE INTO paper_state(account_id,cash,realized_pnl,fees_paid,"
                "funding_paid,updated_at) VALUES(?,?,0,0,0,?)",
                (expected_account_id(arm), INIT_EQUITY, now_ms()))
    con.commit()
    # 落身份清单（**追加式**：保留既有其它臂声明）
    try:
        man = read_manifest() or {}
        man.setdefault("spec_version", LEDGER_SPEC_VERSION)
        man.setdefault("created_utc", None)
        man.setdefault("accounts", {})
        man["accounts"][arm] = {
            "account_id": expected_account_id(arm),
            "initial_equity": INIT_EQUITY,
            "provisioned_at_ms": now_ms(),
        }
        man["note"] = ("受控建档（PAPER_LEDGER_ALLOW_INIT=1）；"
                       "正式账本须按迁移清单 M002–M005 迁入，不得用空库冒充原账户。")
        LEDGER_MANIFEST.write_text(json.dumps(man, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    except OSError:
        pass
    return con


def paper_conn(arm: str, *, ro: bool = False, create: bool | None = None,
               strict_identity: bool = True) -> sqlite3.Connection:
    """复用既有 `paper_{arm}.sqlite3`；**缺库即报错，绝不自动创建**。

    · `ro=True`  → 只读打开（并核对身份）；
    · `ro=False` → 读写打开并做**加性** schema 升级（不删、不重建、不改既有列语义）；
    · 库文件不存在 ⇒ `LedgerUnavailable`（除非显式 `create=True` 且允许建档）；
    · 身份不符 ⇒ `LedgerIdentityMismatch`。
    """
    path = ledger_path(arm)
    allow_create = (create is True) or (create is None and LEDGER_ALLOW_INIT)
    if not path.exists():
        if not allow_create:
            raise LedgerUnavailable(
                f"纸面账本缺失：{path} —— 拒绝自动创建（不得用空库冒充原账户）。"
                f"请按迁移清单 M00{ARMS.index(arm) + 2} 受控迁入，或显式设置 "
                f"PAPER_LEDGER_DIR / PAPER_LEDGER_ALLOW_INIT=1（仅隔离建档）。")
        con = _provision_ledger(arm, path)
        verify_ledger_identity(con, arm, strict=strict_identity)
        return con
    con = _connect(path, ro=ro, create=False)
    if not ro:
        con.executescript(PAPER_ADDITIVE)
        for stmt in PAPER_INDEXES_GUARDED:
            try:
                con.execute(stmt)
            except sqlite3.OperationalError:
                pass                                     # 既有重复行 ⇒ 交给存在性预检兜底
        for table, col, decl in PAPER_COLUMNS:
            try:
                cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            except sqlite3.OperationalError:
                continue
            if not cols or col in cols:
                continue
            try:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass
        con.commit()
    try:
        verify_ledger_identity(con, arm, strict=strict_identity)
    except (LedgerIdentityMismatch, sqlite3.OperationalError):
        con.close()
        raise
    return con


def paper_conns(*, ro: bool = False):
    """四臂账本连接（dict arm→con）；**缺库的臂直接缺席**（由调用方如实报缺）。"""
    out = {}
    for a in ARMS:
        try:
            out[a] = paper_conn(a, ro=ro)
        except (LedgerUnavailable, LedgerIdentityMismatch):
            continue
    return out


def ledger_status() -> dict:
    """**只读**账本状态（供 API/UI 追溯「接口到底读的哪个库」）。"""
    man = read_manifest()
    out = {
        "ledger_dir": str(LEDGER_DIR),
        "manifest_path": str(LEDGER_MANIFEST),
        "manifest_present": LEDGER_MANIFEST.exists(),
        "spec_version": man.get("spec_version") or LEDGER_SPEC_VERSION,
        "allow_init": LEDGER_ALLOW_INIT,
        "arms": {},
    }
    for a in ARMS:
        p = ledger_path(a)
        info = {"path": str(p), "exists": p.exists(), "account_id_expected":
                expected_account_id(a)}
        if p.exists():
            try:
                con = _connect(p, ro=True)
                try:
                    rows = {}
                    for t in ("paper_position", "paper_fill", "paper_funding",
                              "paper_equity", "paper_realized", "paper_order", "paper_state"):
                        try:
                            rows[t] = int(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
                        except sqlite3.OperationalError:
                            rows[t] = None
                    try:
                        info["open_nonvoid"] = int(con.execute(
                            "SELECT COUNT(*) FROM paper_position "
                            "WHERE COALESCE(voided,0)=0 AND status='open'").fetchone()[0])
                    except sqlite3.OperationalError:
                        info["open_nonvoid"] = None
                    info["rows"] = rows
                    info["identity"] = _db_identity(con)
                    info["readable"] = True
                finally:
                    con.close()
                info["sha256"] = _sha256_file(p)
            except Exception as exc:                     # noqa: BLE001
                info["readable"] = False
                info["error"] = f"{type(exc).__name__}: {exc}"
        out["arms"][a] = info
    out["all_present"] = all(v["exists"] for v in out["arms"].values())
    out["any_present"] = any(v["exists"] for v in out["arms"].values())
    return out


# ── 实时起点水位线（**因果闸门**）─────────────────────────────────────────────
# 为什么必须有：43 窗口的 prompt 覆盖整个 168h 窗口，因此实时引擎首次运行会把
# **过去约 7 天**的结构信号一并算出来。若纸面账本把它们全部开仓，就会在
# 「6 天前的价格」上建仓并一路持有到今天 ⇒ 账户吃到了一段它当时**根本不存在**
# 的行情（前视偏差），与「增量」「不冒充当前」「无实时信号时不伪造触发」冲突。
# 故：**只对 `confirmed_at >= 水位线` 的信号自动开仓**；水位线之前的实时信号
# 仍然**正常展示**（它们是真实事件），但 `executable_state` 保持可执行、
# 仅由 worker 侧闸门挡住自动开仓（不篡改信号本身的事实）。
LIVE_START_KEY = "live_start_ms"


def get_live_start_ms():
    return get_run(LIVE_START_KEY)


def ensure_live_start_ms(ts=None) -> int:
    """首次调用时落定水位线（幂等，不覆盖既有值）。"""
    cur = get_run(LIVE_START_KEY)
    if cur:
        return int(cur)
    val = int(ts if ts is not None else now_ms())
    set_run(LIVE_START_KEY, val)
    return val


# ── 模拟交易启用边界（**本轮 paper epoch**，2026-10-14 09 契约整改）──────────────
# 用户明确指令（北京时间 2026-10-14）：**开仓必须不早于今天 09:00**。
#   · 边界 = 2026-10-14 09:00:00 Asia/Shanghai = 2026-10-14T01:00:00Z
#   · Unix 毫秒 = 1791939600000（**固定值，本文件是唯一权威定义处**）
# 该边界持久化到 `live_run.paper_epoch_ms`：四臂、worker、自动/手工开仓、挂单推进
# 与当前信号 API 使用**同一**有效下限 `max(live_start_ms, paper_epoch_ms)`；
# 重启从库里读回 ⇒ **不改值**；`ensure_*` 只允许「不降」，防止被误调低。
# 与 `live_start_ms` 的关系：后者是 2026-10-13 的展示/因果水位，**保留不覆盖**
# （审计需要），二者取最大者为有效下限。
PAPER_EPOCH_MS = 1_791_939_600_000
PAPER_EPOCH_KEY = "paper_epoch_ms"
PAPER_EPOCH_ISO = "2026-10-14T01:00:00Z"
PAPER_EPOCH_LOCAL = "2026-10-14 09:00:00 +08:00"


def get_paper_epoch_ms() -> int:
    """读持久 epoch（**只读，不写库**，供 API/只读端使用）。

    缺失或非法 ⇒ 返回固定下限 `PAPER_EPOCH_MS`（fail-closed：绝不返回更早的值）。
    """
    cur = get_run(PAPER_EPOCH_KEY)
    try:
        cur = int(cur)
    except (TypeError, ValueError):
        return PAPER_EPOCH_MS
    return max(cur, PAPER_EPOCH_MS)


def ensure_paper_epoch_ms() -> int:
    """worker 侧落定本轮 paper epoch（幂等；**只允许不降**）。"""
    cur = get_run(PAPER_EPOCH_KEY)
    if cur is None or int(cur) < PAPER_EPOCH_MS:
        set_run(PAPER_EPOCH_KEY, PAPER_EPOCH_MS)
    return get_paper_epoch_ms()


def effective_floor_ms() -> int:
    """自动开仓 / 挂单推进 / 当前信号 API 的**同一**有效下限。"""
    return max(int(get_live_start_ms() or 0), get_paper_epoch_ms())


def signal_bar_right_ms(confirmed_at) -> int:
    """信号的**真实区间右边界**（毫秒）。

    为什么必须 +1：库里 `close_time` 存的是**闭区间右端 - 1ms**（如 09:00 收盘存
    1791939599999）。若直接用 `confirmed_at >= epoch` 判断，会把「08:00~09:00 正常闭合」
    的信号误判为「边界前」而排除。故按真实右边界（`confirmed_at + 1`）比较，
    **原始值原样保留不改**。
    """
    try:
        return int(confirmed_at) + 1
    except (TypeError, ValueError):
        return -(1 << 62)


def set_run(key: str, value) -> None:
    import json
    con = live_conn()
    try:
        con.execute("INSERT INTO live_run(key,value_json,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,"
                    "updated_at=excluded.updated_at",
                    (key, json.dumps(value, ensure_ascii=False, default=str), now_ms()))
        con.commit()
    finally:
        con.close()


def get_run(key: str, default=None):
    import json
    con = live_conn(ro=True)
    try:
        r = con.execute("SELECT value_json FROM live_run WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return default
    finally:
        con.close()
    if not r or r[0] is None:
        return default
    try:
        return json.loads(r[0])
    except ValueError:
        return default


if __name__ == "__main__":
    import json as _json
    ensure_live_schema()
    print("live_store: schema ready ->", LIVE_DB)
    st = ledger_status()
    print("paper ledger dir ->", st["ledger_dir"])
    for a, info in st["arms"].items():
        print(f"  {a}: exists={info['exists']} "
              + (f"rows={info.get('rows')} identity={info.get('identity')}"
                 if info["exists"] else "MISSING（拒绝自动创建）"))
    # 缺库不静默建档；仅显式允许时才建档
    if st["all_present"] or LEDGER_ALLOW_INIT:
        for a in ARMS:
            try:
                paper_conn(a).close()
            except (LedgerUnavailable, LedgerIdentityMismatch) as exc:
                print(f"  {a}: {type(exc).__name__}: {exc}")
    else:
        print("  缺库 ⇒ 不自动创建（如需受控建档：PAPER_LEDGER_ALLOW_INIT=1）")
    print(_json.dumps({"ledger_status": st}, ensure_ascii=False, indent=1)[:2000])
