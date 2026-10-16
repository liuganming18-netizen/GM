#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后端适配层（第一/二批）。

复用统一数据层 MarketStore（只读行情库），在 127.0.0.1:8787 提供：

  A) 数据层已有只读接口（原样保留）：
       GET /api/health   GET /api/data/status   GET /api/candles?...
  B) 前端 /api/v1 只读契约（最小字段 + 缺失显式状态）：
       /api/v1/health  /api/v1/overview  /api/v1/markets  /api/v1/markets/{ex}/{mid}
       /api/v1/markets/{ex}/{mid}/candles|oi|funding|rule-snapshots|public-extras
       /api/v1/pool    /api/v1/pool/status               （数据总池快照与下载进度）
       /api/v1/data/status  /api/v1/data/coverage  /api/v1/data/import-plan
       /api/v1/strategies  /api/v1/strategies/{key}   （注册表；含配置状态）
       /api/v1/strategy                                （旧单数端点，前端 client.ts 仍调用）
       /api/v1/signals     /api/v1/signals/{id}

已接 / 未接（诚实口径）：
  - annotations：已由**真实已存信号**构建；无信号时为 null 并给出说明，不生成推测标注。
  - rule-snapshots：按 marker_kind+marker_time **精确匹配**；查不到返回缺失，不退回最新信号。
  - 指标(metrics)、账户、回测、复盘：**未接通**，返回 not_connected。
  - OI/资金费历史：表结构已就绪但**未采集**，空表返回 missing。
  - 库存：优先读 coverage_cache（批量下载器维护）；为空时回退全表统计。
  - 数据总池(pool)：来自真实 exchangeInfo 快照，**不等于**可交易清单；
    交易资格规则未冻结 → eligibility 一律 null 并给出原因。

纪律：
  - 只读行情库 + **只读本机快照文件**（`快照/public_extras/`，由**既有采集工作器**
    `realtime_collector.public_extras_loop` 周期写入）；**仍不发起任何交易所请求**
    （前端读取不触发外部采集 —— 采集与读取分离）。
  - 不造假：取不到 → null 或显式缺失状态（missing / not_connected / 待配置 / 数据不足）。
  - 聪明钱多空金额/内部占比/均价无真实来源 → 一律缺失，不用人数多空比或 taker 买量冒充。
  - 仅监听 127.0.0.1，不开放局域网。
"""
import hashlib
import json
import os
import socket
import sqlite3
import sys
import threading
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
FRAMEWORK_ROOT = os.path.dirname(HERE)
DESKTOP_ROOT = os.path.dirname(FRAMEWORK_ROOT)

# 自动优先探测真实存在的本地项目目录与数据库文件。
# W1（2026-10-16）：**删除**工程外兜底 旧工作区路径（已删）——该目录已随旧工作区
# 删除；保留它会让「找不到本工程库」时静默落到一个不存在的路径上。现在候选**只**来自
# 显式环境变量与工程内相对位置（`MARKET_DB` 由 `start.sh` / `启动系统.command` 注入）。
_CANDIDATE_DBS = [
    os.environ.get("MARKET_DB"),
    os.path.join(HERE, "data", "market_v2.sqlite3"),
    os.path.join(FRAMEWORK_ROOT, "data_hub", "market_v2.sqlite3"),
    os.path.join(DESKTOP_ROOT, "数据", "市场数据", "db", "market_v2.sqlite3"),
    os.path.join(DESKTOP_ROOT, "data", "market_v2.sqlite3"),
]
DB_PATH = next((p for p in _CANDIDATE_DBS if p and os.path.exists(p)), _CANDIDATE_DBS[1])

_CANDIDATE_PROJECTS = [
    os.environ.get("DATA_HUB_PROJECT"),
    DESKTOP_ROOT,
]
PROJECT_ROOT = next((p for p in _CANDIDATE_PROJECTS if p and os.path.exists(p)), DESKTOP_ROOT)
PORT = int(os.environ.get("PORT", "8787"))

if FRAMEWORK_ROOT not in sys.path:
    sys.path.insert(0, FRAMEWORK_ROOT)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from data_hub.store import INTERVAL_MS, MarketStore, now_ms  # noqa: E402
from data_hub.http_api import read_route             # noqa: E402
from strategies import common, registry              # noqa: E402
from strategies import wx_config, wx_gate            # noqa: E402
CURRENT_RUN_KEYS = set(registry.CURRENT_RUN_KEYS)    # 当前启用族 F_WX：3a/3b/3c/3d
LEGACY_RUN_KEYS = set(registry.LEGACY_KEYS)          # 旧 A/B：保留可读，不进入当前启用集
# 本机公开扩展字段快照的**只读**访问（纯文件 I/O，无网络、无库依赖）。
import public_snapshot                               # noqa: E402
# 币安**聪明钱（Smart Signal）**本机快照的**只读**访问（纯文件 I/O，无网络）；
# 采集由既有采集工作器的 `smart_money_loop` 周期写入，本机 API 只读快照。
import smart_money                                   # noqa: E402
# 历史 K 线**读侧语义隔离**（只读、纯函数）：官方归档零量占位 + 多代合约生命周期分段。
# 范围限 12h；不改库、不删行、不改既有字段语义，只在返回里新增标签与计数。
import history_lifecycle as lifecycle                # noqa: E402
# ── 信号与持仓接通（2026-10-13，**加性**）──────────────────────────────────────
# 独立模块：实时信号 / 模拟盘账本 / 实盘只读账户。**全部纯标准库**（本进程 3.13.12 无 numpy/ccxt）；
# 实时信号与实盘快照由独立 live worker（freqtrade 解释器）写入 live 库，本层只读。
# 导入失败 ⇒ 相关端点如实降级为 not_connected，**不影响**既有行情接口。
try:                                                 # noqa: E402
    import live_api as live                          # noqa: E402
    _LIVE_IMPORT_ERROR = None
except Exception as _exc:                            # noqa: BLE001
    live = None
    _LIVE_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"

# ── 单一策略真实回测接入（2026-10-16，**加性**）──────────────────────────────
# 独立模块：研究回测任务/持久化/API 适配。**不改** live_signal_engine / paper_engine，
# 不启 worker、不写 paper 账户、不碰旧 ledger/state/manifest。
# 依赖 numpy/pandas（候选包执行核心）⇒ 导入失败只影响 `/api/v1/backtests` 新通道，
# 不影响既有行情/信号/纸面端点。
try:                                                 # noqa: E402
    import backtest_api as backtest                 # noqa: E402
    _BACKTEST_IMPORT_ERROR = None
except Exception as _exc:                            # noqa: BLE001
    backtest = None
    _BACKTEST_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"

try:                                                 # noqa: E402
    import datacenter_service as datacenter         # noqa: E402
    _DATACENTER_IMPORT_ERROR = None
except Exception as _exc:                            # noqa: BLE001
    datacenter = None
    _DATACENTER_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"



def _backtest_missing(reason):
    return missing(reason, "not_connected", items=[], has_more=False, next_cursor=None,
                   total=None)


def _live_missing(reason):
    return missing(reason, "not_connected", items=[], has_more=False, next_cursor=None,
                   total=None)


class _ReadOnlyStore(MarketStore):
    """只读行情库包装：**不执行任何 DDL / 写入**，连接以 `mode=ro` 打开。

    本窗修复（BUG-003 前置 + OBS-004）：
      · 原实现 `MarketStore(DB_PATH)` 在导入期执行 `schema.sql`
        （`CREATE ... IF NOT EXISTS`）并 `INSERT OR IGNORE INTO schema_versions`
        —— 对 76GB 生产库是多余写事务，也让「只读服务」的只读性只靠约定；
        重载时会触碰正在写库的 15m 写者。此处改为**只读初始化 + 只读连接**，
        重载不再产生任何 DDL / 写事务。
      · 原 `status()` 对 `candles` 视图做 1.28 亿行 `GROUP BY`（无界，实测 >3s）；
        此处改为读 `coverage_cache`（有界，由下载器增量维护）+ 非阻塞重统计，
        「已入库覆盖」口径不变。
    """

    def __init__(self, path, clock=now_ms):        # 覆盖：不建目录、不跑 schema、不写版本表
        self.path = Path(path)
        self.clock = clock

    def connect(self):
        con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        return con

    def status(self):
        with closing(self.connect()) as con:
            try:
                coverage = [dict(r) for r in con.execute(
                    "SELECT exchange,market,symbol,timeframe,rows,first_open,last_close,"
                    "missing AS missing_within_observed_range FROM coverage_cache")]
            except sqlite3.OperationalError:
                coverage = []
            if not coverage:
                # coverage_cache 缺失/为空（如离线测试库）→ 回退 candles 视图，
                # 保持「已入库覆盖」口径不变；但带**硬预算**，避免在 1.28 亿行
                # 生产库上无界扫描（生产库 coverage_cache 非空，不会走到此分支）。
                coverage = self._coverage_from_candles(con)
            try:
                states = [dict(r) for r in con.execute("SELECT * FROM api_scope_state")]
            except sqlite3.OperationalError:
                states = []
        st = api_stats()
        return {"数据库": str(Path(self.path).resolve()), "来源文件数": st["sources"],
                "已入库覆盖": coverage, "外部请求记录数": st["calls"],
                "接口冷却或暂停状态": states}

    @staticmethod
    def _coverage_from_candles(con, budget_s=5.0):
        """回退口径：按 (标的,周期) 聚合 `candles` 视图；超预算返回 []（不无界扫描）。"""
        t0 = time.monotonic()
        con.set_progress_handler(
            lambda: 1 if (time.monotonic() - t0) > budget_s else 0, 20000)
        try:
            rows = [dict(r) for r in con.execute(
                "SELECT exchange,market,symbol,timeframe,MIN(open_time) AS first_open,"
                "MAX(close_time) AS last_close,COUNT(*) AS rows FROM candles "
                "GROUP BY exchange,market,symbol,timeframe")]
        except sqlite3.OperationalError:
            rows = []
        finally:
            con.set_progress_handler(None, 0)
        for c in rows:
            duration = INTERVAL_MS.get(c["timeframe"])
            if duration and c["first_open"] is not None and c["last_close"] is not None:
                c["missing_within_observed_range"] = (
                    (c["last_close"] + 1 - c["first_open"]) // duration) - c["rows"]
            else:
                c["missing_within_observed_range"] = None
        return rows

    def candles(self, exchange, market, symbol, timeframe, start=0, end=None, limit=500,
                source_id=None):
        """点查 K 线：**热层（新热库近 30 天）+ 冷层（Parquet 全历史）统一路由**。

        未配置 `MARKET_ARCHIVE` ⇒ 回退到原 SQLite 单库路径（与迁移前逐字一致，零回归）。
        """
        if _REPO is not None:
            return _REPO.candles(exchange, market, symbol, timeframe, start=start, end=end,
                                 limit=limit, source_id=source_id, prefer="hot")
        return super().candles(exchange, market, symbol, timeframe, start=start, end=end,
                               limit=limit, source_id=source_id)


STORE = _ReadOnlyStore(DB_PATH)

# ── P3 冷热仓储接入（2026-10-13 数据迁移）──────────────────────────────────────
# 热层 = 本进程的 `MARKET_DB`（切流后指向 `market_v2.sqlite3`：近 30 天 + 每周期
#        latest200）；冷层 = `MARKET_ARCHIVE` 指向的 Parquet 全历史归档。
# 二者由**同一个** `MarketDataRepository` 统一路由（不另写第二套语义）。
# 未设置 `MARKET_ARCHIVE` ⇒ 本块整体跳过，行为与迁移前**逐字一致**（零回归、可回退）。
ARCHIVE_ROOT = os.environ.get("MARKET_ARCHIVE") or ""
REPO_WATERMARK_MS = int(os.environ.get("MARKET_WATERMARK_MS") or "0")
REPO_RETENTION_DAYS = int(os.environ.get("MARKET_RETENTION_DAYS") or "30")
REPO_ERROR = None
REPO_WATERMARK_SRC = None
_REPO = None
if ARCHIVE_ROOT:
    try:
        from market_repository import MarketDataRepository as _MDR   # noqa: E402
        # 水位**以归档 `_state.json` 为单一事实源**（由归档器校验通过后推进）。
        # `MARKET_WATERMARK_MS` 只在状态文件缺失时兜底 ⇒ 不再出现
        # 「读侧用旧 env 水位 / 归档器已推新水位」两套语义并存（用户 P3-3 明令禁止）。
        _st = _MDR.read_state(ARCHIVE_ROOT)
        _wm_state = _st.get("watermark_ms") or _st.get("reconcile_watermark_ms")
        if _wm_state:
            REPO_WATERMARK_MS = int(_wm_state)
            REPO_WATERMARK_SRC = "state"
        else:
            REPO_WATERMARK_SRC = "env" if REPO_WATERMARK_MS else "none"
        _REPO = _MDR(DB_PATH, ARCHIVE_ROOT, watermark_ms=REPO_WATERMARK_MS,
                     retention_days=REPO_RETENTION_DAYS)
    except Exception as _exc:                            # noqa: BLE001
        _REPO = None
        REPO_ERROR = f"{type(_exc).__name__}: {_exc}"


def repo_status():
    """冷热仓储接入状态（只读，供 /api/data/status 或诊断用）。"""
    return {"接入": _REPO is not None, "热层": DB_PATH, "冷层": ARCHIVE_ROOT or None,
            "水位ms": REPO_WATERMARK_MS or None, "水位来源": REPO_WATERMARK_SRC,
            "热层保留天数": REPO_RETENTION_DAYS,
            "错误": REPO_ERROR}


def _market_for(exchange, symbol):
    """解析该 (exchange,symbol) 的 market —— 冷档路径按 (exchange,market,symbol) 存放。"""
    try:
        for c in covered():
            if c["exchange"] == exchange and c["symbol"] == symbol:
                return c.get("market") or ("um" if exchange == "binance" else "swap")
    except Exception:                                    # noqa: BLE001
        pass
    return "um" if exchange == "binance" else "swap"


def _cold_flat_rows(table, exchange, mid, ts_col, period_val, frm, to, want_n):
    """冷档平铺历史行（dict 列表，时间**降序**）。未接入仓储 ⇒ 返回 []。"""
    if _REPO is None:
        return []
    market = _market_for(exchange, mid)
    if table == "oi_history":
        return _REPO.oi(exchange, market, mid, period=period_val, start=frm, end=to,
                        limit=want_n, prefer="cold")
    if table == "funding_history":
        return _REPO.funding(exchange, market, mid, start=frm, end=to,
                             limit=want_n, prefer="cold")
    return []

TF_ORDER = ["1d", "12h", "8h", "4h", "1h", "30m", "15m", "5m"]
# 2026-10-11 用户指令：1m **彻底删除**（库内数据 + 全部口径一起摘）。
# 此前 `TF_SET` 仍并入 {"1m"}（53 合同只改了 TF_ORDER，这里漏了），会让 `?timeframe=1m`
# 与 `signals.timeframe` 校验继续认这个已不存在的周期。现与 TF_ORDER 完全一致。
TF_SET = set(TF_ORDER)
NO_NATIVE = {"okx": {"8h"}}          # 欧易无 8h 原生周期，不合成
UNIT = {"klines": "OHLCV(价格=计价币, 量=标的币, 成交额=计价币)",
        "oi": "open_interest=标的币/张, oi_usd=USD", "funding": "rate=小数/期"}
SMART_MONEY_REASON = ("无真实来源：不使用交易所人数多空比或 taker 买量冒充"
                      "「聪明钱多空金额/内部占比/均价」")
SIGNAL_STATUSES = {"candidate", "waiting_retest", "confirmed", "invalidated",
                   "expired", "undecided"}
# 方向字段的前端契约取值。**只认这两种**；其余（含 null）一律回 null。
# 纪律：方向只能来自 `signals` 表的真实信号（A/B 策略事件），
# **不得**用 24h 涨跌、taker 买卖比、人数多空比等派生量冒充策略方向。
SIGNAL_DIRECTIONS = {"long", "short"}
# 折叠行摘要选哪一档周期：**由粗到细**（优先日线）。
# 理由：MarketRowSummary.change_ratio 在界面上只显示为「涨跌幅」，用日线口径最不易被误读成
# 实时价或短线涨跌；且周期由 summary.timeframe 明示，前端展开行会显示该 chip。
SUMMARY_TF_PREF = ["1d", "12h", "8h", "4h", "1h", "30m", "15m", "5m"]
# symbol_pool 存的现货状态是**交易所原始状态串**，需按前端契约映射为 listed/not_listed/unknown。
# 现货快照是按 symbolStatus=TRADING 过滤取的：未命中 = **未知**，不等于未挂牌。
SPOT_LISTED_STATUS = "TRADING"
# 永续合约的 contract_type 取值：**各所原始语义**不同，不能只认币安那一套。
#   · 币安 USD-M：contractType = "PERPETUAL"
#   · 欧易 SWAP：instType      = "SWAP"（同为永续，只是字段口径不同）
# 只按 "PERPETUAL" 判定会让欧易全部被判成「非永续」→ 549 里的欧易标的一致为空。
PERP_CONTRACT_TYPES = {"PERPETUAL", "SWAP"}
# 标记类型 → 事件证据里的时间字段（仅登记当前模块**真实产出**的标记）
MARKER_EVIDENCE = {"frozen_range": "frozen_at", "breakout": "breakout_close_time",
                   "retest": "retest_close_time"}
IMPORT_PLAN = {
    "order": ["1d", "12h", "8h", "4h", "1h", "30m", "15m", "5m"],
    "retention": {"5m": "首次1年", "15m": "首次2年",
                  "30m及以上": "各原生周期全部可得历史"},
    "native_skip": {"okx": ["8h"], "rule": "无原生周期跳过，不合成、不跨所拼接",
                    "evidence": "实测订阅欧易 candle8H 返回 code 60018（频道不存在）；"
                                "币安 8h 为原生周期，正常采集"},
    "realtime": {
        "source": "交易所**原生** WS K 线流（币安 <sym>@kline_<interval>；欧易 candle<bar>）",
        "closed_rule": "bar 收盘后按 (exchange,market,symbol,timeframe,open_time) 幂等写入"
                       " candle_versions/candle_heads，且**不覆盖已有行**",
        "forming_rule": "未收盘 bar 只写独立临时层 realtime_klines，供图表；"
                        "不混入已收盘记录、不作回测输入",
        "api_rule": "浏览器只读同源 /api/v1，**不直连交易所**",
    },
    "chart_default": "默认周期为 5m；1m 已于 2026-10-11 彻底删除（库内数据 + 全部口径）",
}
# 运行状态**一律现读库内真实记录**（不写死文案）。
# 旧文案「配置已保留；本轮未启动全池下载或持续采集」在下载与采集都已运行时是过时的，
# 会让人误判"什么都没在跑"，故改为由 `v1_import_plan()` 实时汇总。


def _realtime_kline_state():
    """逐 (交易所,周期) 的实时 K 线链路状态（读 kline_stream_state，真实消息时点）。"""
    with use_db() as con:
        try:
            rows = con.execute(
                "SELECT exchange,market,timeframe,symbols_planned,symbols_seen,last_msg_at,"
                "last_forming_open_time,last_closed_open_time,last_closed_written_at,"
                "closed_written,gapfill_calls,gapfill_filled,gapfill_skipped,reconnects,"
                "backoff_s,status,detail,updated_at FROM kline_stream_state "
                "ORDER BY exchange, timeframe").fetchall()
        except sqlite3.OperationalError:
            return None
    now = _now_ms()
    out = []
    for r in rows:
        d = dict(r)
        age = ((now - r["last_msg_at"]) / 1000.0) if r["last_msg_at"] else None
        out.append({
            "exchange": r["exchange"], "timeframe": r["timeframe"],
            "symbols_planned": r["symbols_planned"], "symbols_seen": r["symbols_seen"],
            "last_msg_at": iso(r["last_msg_at"]),
            "last_msg_age_seconds": round(age, 1) if age is not None else None,
            "last_forming_open_time": iso(r["last_forming_open_time"]),
            "last_closed_open_time": iso(r["last_closed_open_time"]),
            "last_closed_written_at": iso(r["last_closed_written_at"]),
            "closed_written": r["closed_written"],
            "gapfill_calls": r["gapfill_calls"], "gapfill_filled": r["gapfill_filled"],
            "gapfill_skipped": r["gapfill_skipped"],
            "reconnects": r["reconnects"], "backoff_s": r["backoff_s"],
            "status": r["status"], "detail": r["detail"],
            "updated_at": iso(r["updated_at"]),
        })
    return out


def _collector_health():
    """采集进程**心跳**（不是 run 记录）：只有心跳时间足够新才算活着。"""
    with use_db() as con:
        try:
            r = con.execute("SELECT name,pid,started_at,heartbeat_at,status,detail,"
                            "counters_json FROM collector_state WHERE name='realtime'").fetchone()
        except sqlite3.OperationalError:
            return None
    if not r:
        return None
    age = (_now_ms() - r["heartbeat_at"]) / 1000.0 if r["heartbeat_at"] else None
    return {"pid": r["pid"], "started_at": iso(r["started_at"]),
            "heartbeat_at": iso(r["heartbeat_at"]),
            "heartbeat_age_seconds": round(age, 1) if age is not None else None,
            "alive": bool(age is not None and age < 120),
            "recorded_status": r["status"], "detail": r["detail"]}


def _history_runs():
    """历史下载 run 的**记录**（如实呈现，但**不等于**进程仍健康或已补齐全池）。"""
    with use_db() as con:
        try:
            rows = con.execute("SELECT id,kind,phase,started_at,finished_at,status,note "
                               "FROM download_runs ORDER BY id DESC LIMIT 3").fetchall()
        except sqlite3.OperationalError:
            return None
    return [{"run_id": r["id"], "kind": r["kind"], "phase": r["phase"],
             "started_at": iso(r["started_at"]), "finished_at": iso(r["finished_at"]),
             "recorded_status": r["status"], "note": r["note"]} for r in rows]


def iso(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def num(txt):
    try:
        return float(txt)
    except (TypeError, ValueError):
        return None


def _now_ms():
    return int(time.time() * 1000)


# 实时新鲜度阈值：REST 批量快照一轮 300s（WS 可用时）→ 取 2 倍余量。
# 只用**价格事件/接收时间**判断，不因进程存在就断言 ok（契约明确要求）。
RT_STALE_MS = 600_000


def _src_label(rt):
    """realtime_source：交易所来源 + 传输方式，可追溯。"""
    src = rt.get("price_source")
    if not src:
        return None
    tr = rt.get("price_transport")
    return f"{src}/{tr}" if tr else src


def _rt_status(rt, now):
    """ok / stale / missing —— 按真正价格时点判断。"""
    if not rt or rt.get("last_price") in (None, ""):
        return "missing"
    t = rt.get("price_event_time") or rt.get("price_received_at")
    if not t:
        return "stale"                     # 有价但无时点：不能断言新鲜
    try:
        return "ok" if (now - int(t)) <= RT_STALE_MS else "stale"
    except (TypeError, ValueError):
        return "stale"


# ── SQLite 连接池 ───────────────────────────────────────────────────────────
# 为什么必须有：`/api/v1/markets` 一次请求要为 **549 个标的**各取一次摘要，
# 原先每个调用点都 `closing(db())` 新建连接 —— **实测单次请求新建 557 个连接**。
# 当 WAL 因持续写入而膨胀时，每个新连接都要读 WAL 索引，单请求耗时升到 12.6 秒；
# 前端每 5 秒轮询一次 → 请求到达快于完成 → 处理线程无界堆积
# （实测 35363 进程积到 1971 个 socket + 1742 个 DB fd）→ 连接被重置、页面只剩骨架。
# 复用后稳态新建连接数为 0。
_DB_POOL = []
_DB_POOL_LOCK = threading.Lock()
_DB_POOL_MAX = 24
DB_STATS = {"opened": 0, "reused": 0}


def _new_db_conn():
    # check_same_thread=False 是**连接池的前提**：池会把同一连接先后交给不同请求线程。
    # 安全性由池保证 —— 一个连接同一时刻只被一个线程持有（借出后归还才可再借）。
    # 若不关掉该检查，跨线程复用会抛 ProgrammingError 并让接口 500。
    con = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=10000")
    # 只读侧页缓存：默认 2MB/连接。历史下载并发写、库已近 20GB 时，
    # 反复读 coverage_cache / sources / realtime_quotes 等小表也会被磁盘 I/O 拖慢，
    # 调大到 32MB/连接（池上限 24 ⇒ 峰值约 768MB 上限，实际按需占用）显著减少重复读。
    con.execute("PRAGMA cache_size=-32000")
    # 只读连接不走 mmap 写回；mmap 让热点页直接映射，避免 read() 系统调用。
    con.execute("PRAGMA mmap_size=268435456")
    DB_STATS["opened"] += 1
    return con


def db():
    """取一个 SQLite 连接：优先复用池中空闲连接，池空才新建。"""
    with _DB_POOL_LOCK:
        if _DB_POOL:
            DB_STATS["reused"] += 1
            return _DB_POOL.pop()
    return _new_db_conn()


def release_db(con):
    """把连接**归还池**（而不是关闭）；池满才真正关闭。"""
    if con is None:
        return
    with _DB_POOL_LOCK:
        if len(_DB_POOL) < _DB_POOL_MAX:
            _DB_POOL.append(con)
            return
    try:
        con.close()
    except Exception:                                        # noqa: BLE001
        pass


@contextmanager
def use_db():
    """请求内连接上下文：用完归还池。语义与 `closing(db())` 等价但**不关闭**。"""
    con = db()
    try:
        yield con
    finally:
        release_db(con)


# ── 极短 TTL 缓存 ───────────────────────────────────────────────────────────
# 库级索引（sources / coverage / pool / 实时快照）在每次 `/api/v1/markets` 都要重算。
# 实测 `sources_index()` 单条查询在 WAL 1.85GB 时要 1.8~11.6 秒。
# 这些数据变化很慢（仅在新数据入库时变），按 TTL 摊薄即可，不改变任何字段语义。
_TTL_CACHE = {}
_TTL_LOCK = threading.Lock()
# 单飞（single-flight）：同一 key 重算期间，其余请求等待**同一次**结果，
# 而不是各自重算一遍。必要性：库级索引函数（如 sources_index）在库变大后
# 单次可达 1.6~3.6 秒；若无单飞，N 个并发请求会做 N 次相同重算，
# 在 GIL 下串行成 N×耗时（实测 8 并发 /api/v1/markets 由 0.5s 放大到 9.5s）。
_TTL_INFLIGHT = {}
TTL_WAIT_MAX = 30.0        # 跟随者等待上限；超时则自行重算，避免被卡死的 leader 拖住
# 缓存开关：生产默认开启；测试用 `ADAPTER_CACHE=0` 关闭，
# 因为测试会在同一进程内改库后立刻再读，必须拿到最新结果。
CACHE_ENABLED = os.environ.get("ADAPTER_CACHE", "1") != "0"


def _ttl_run(key, producer, flight):
    """执行一次重算并广播结果（leader 路径）。"""
    try:
        val = producer()
    except BaseException as exc:               # noqa: BLE001
        with _TTL_LOCK:
            _TTL_INFLIGHT.pop(key, None)
        flight["error"] = exc
        flight["event"].set()
        raise
    # 关键：缓存时点取**完成时刻**，不是开始时刻。
    # 旧写法用开始时刻，一旦生产者耗时 > TTL，条目写入即已过期，
    # 缓存等于永久失效（实测 v1_markets 生产者 2.14s > TTL 2.0s，每个请求都重算）。
    with _TTL_LOCK:
        _TTL_CACHE[key] = (time.monotonic(), val)
        _TTL_INFLIGHT.pop(key, None)
    flight["value"] = val
    flight["event"].set()
    return val


def _ttl(key, ttl_s, producer):
    """带 TTL 的取值；过期时**先用旧值返回、后台刷新**（stale-while-revalidate）。

    为什么需要「先用旧值」：`/api/v1/markets` 的重算成本随库增长而上升
    （`sources_index()` 在历史下载并发写、磁盘 I/O 饱和时实测 3.6 秒），
    超过 TTL 后缓存条目一写入即过期，等于每请求都重算 → 前端 5 秒轮询下
    请求堆积、代理后超时、页面空表。改为：命中过期值时**立即返回上一次结果**，
    同时只让一个后台线程去重算。HTTP 请求因此**永不阻塞在重算上**；
    代价是 `as_of` 最多滞后「TTL + 一次重算」，字段与语义不变。
    仅当该 key **从未**算过（进程刚启动）时才同步计算。
    """
    if not CACHE_ENABLED:
        return producer()
    now = time.monotonic()
    with _TTL_LOCK:
        hit = _TTL_CACHE.get(key)
        if hit is not None and (now - hit[0]) < ttl_s:
            return hit[1]
        stale = hit[1] if hit is not None else None
        flight = _TTL_INFLIGHT.get(key)
        if flight is None:
            flight = {"event": threading.Event(), "value": None, "error": None}
            _TTL_INFLIGHT[key] = flight
            leader = True
        else:
            leader = False
    if hit is not None:
        if leader:
            threading.Thread(target=_ttl_run, args=(key, producer, flight),
                             daemon=True).start()
        return stale
    if not leader:
        if flight["event"].wait(TTL_WAIT_MAX):
            if flight["error"] is not None:
                raise flight["error"]
            return flight["value"]
        return producer()                      # 等不到就自己算，不无限等待
    return _ttl_run(key, producer, flight)


# ── 有界、非阻塞的库级重统计（BUG-003 修复）─────────────────────────────────
# 背景：`oi_history` 1.27 亿行的逐交易所聚合、`sources` 约 4.1e6 行与
# `api_calls` 约 7.3e5 行的全表 COUNT，都无法在请求路径内完成
# （本窗只读实测：coverage 的 OI 聚合 >90s；sources COUNT 1.4~7.6s；
#  api_calls status COUNT >3s）。这些统计把只读请求拖进写压下的 I/O 竞争。
# 处理原则：
#   · 请求线程**永不**执行重统计：命中缓存直接返回；缓存过期先返回旧值（stale）；
#     从未算过 → 触发后台单飞刷新并返回 None（unknown），**不阻塞、不填 0**。
#   · 后台刷新带**硬墙钟预算**（STATS_BUDGET_S），超预算即中止，保留上一轮结果。
#   · 上层把 None 显式标注为「暂不可得」，不冒充精确计数。
#   · 根治（增量汇总表 / `api_calls(status)` 索引）属数据层 schema 决策，另单列候选。
_STATS_CACHE = {}
_STATS_INFLIGHT = set()
_STATS_ATTEMPT = {}
_STATS_LOCK = threading.Lock()
STATS_TTL_S = 300.0        # 重统计缓存 TTL（库级计数变化很慢）
STATS_RETRY_S = 60.0       # 失败/超预算后的最短重试间隔
STATS_BUDGET_S = 8.0       # 单次刷新硬预算（秒）


def _bounded_scalar(sql):
    """在**独立只读连接**上取一个标量；超过 `STATS_BUDGET_S` 即中止，返回 None。

    用独立连接（不入连接池）：`set_progress_handler` 是**连接级**状态，
    若设在池连接上会泄漏到后续其它请求。中止返回 None（unknown），不填 0。
    """
    con = sqlite3.connect(DB_PATH, timeout=10)
    try:
        con.execute("PRAGMA query_only=1")
        t0 = time.monotonic()
        con.set_progress_handler(
            lambda: 1 if (time.monotonic() - t0) > STATS_BUDGET_S else 0, 20000)
        row = con.execute(sql).fetchone()
        return row[0] if row is not None else None
    except sqlite3.OperationalError:
        return None
    finally:
        con.set_progress_handler(None, 0)
        con.close()


def _heavy_refresh(key, producer):
    try:
        val = producer()
    except BaseException:                          # noqa: BLE001
        val = None
    finally:
        with _STATS_LOCK:
            _STATS_INFLIGHT.discard(key)
            if val is not None:                    # 失败/超预算不覆盖旧值、不写 0
                _STATS_CACHE[key] = (time.monotonic(), val)


def _heavy_stats(key, producer, ttl_s=STATS_TTL_S):
    """非阻塞取重统计：命中即返回；否则调度后台刷新并返回 None（unknown）。

    `ADAPTER_CACHE=0`（测试/显式关闭缓存）时**同步**计算：测试在同一进程内改库后
    立即读，必须拿到最新结果；测试库很小，同步成本可忽略。
    """
    if not CACHE_ENABLED:
        return producer()
    now = time.monotonic()
    with _STATS_LOCK:
        hit = _STATS_CACHE.get(key)
        if hit is not None and (now - hit[0]) < ttl_s:
            return hit[1]
        stale = hit[1] if hit is not None else None
        due = (now - _STATS_ATTEMPT.get(key, 0.0)) >= STATS_RETRY_S
        if key not in _STATS_INFLIGHT and due:
            _STATS_ATTEMPT[key] = now
            _STATS_INFLIGHT.add(key)
            threading.Thread(target=_heavy_refresh, args=(key, producer), daemon=True).start()
        return stale


def _count_sources():
    n = _bounded_scalar("SELECT COUNT(*) FROM sources")
    return n if isinstance(n, int) else None


def _api_calls_counts():
    ok = _bounded_scalar("SELECT COUNT(*) FROM api_calls WHERE status='成功'")
    bad = _bounded_scalar("SELECT COUNT(*) FROM api_calls WHERE status<>'成功'")
    if isinstance(ok, int) and isinstance(bad, int):
        return (ok, bad)
    return None


def _exchange_span_stats(table, ts_col):
    """逐交易所的入库行数与时间跨度（**有界**）。

    · `oi_history`：只统计 `period='snapshot'`（实时快照分区），走部分索引
      `ix_oi_hist_snapshot`（本窗实测 0.18s）；**不**统计 5m 等归档历史
      （逐交易所聚合需全表扫描 1.27 亿行）。归档口径由调用方显式标注暂不可得。
    · `funding_history`：表约十万行，主键覆盖索引扫描成本可忽略。
    """
    where = " WHERE period='snapshot'" if table == "oi_history" else ""
    with use_db() as con:
        try:
            rows = con.execute(f"SELECT exchange, COUNT(*) n, MIN({ts_col}) f, MAX({ts_col}) l "
                               f"FROM {table}{where} GROUP BY exchange").fetchall()
        except sqlite3.OperationalError:
            return {}
    return {r["exchange"]: dict(r) for r in rows}


def envelope(**kw):
    base = {"mode": None, "account_id": None, "run_id": None, "strategy_version": None,
            "rule_version": None, "param_hash": None, "data_version": None,
            "as_of": iso(STORE.clock()), "quality": None, "source_url": None,
            "evidence_level": None, "missing_reason": None,
            "time": {"event_time": None, "available_at": None, "received_at": None,
                     "fetched_at": None}}
    base.update(kw)
    return base


def missing(reason, status="missing", **kw):
    return envelope(quality={"status": status, "age_seconds": None, "note": reason},
                    missing_reason=reason, **kw)


def covered():
    """真实库存摘要。

    优先读 `coverage_cache`（由批量下载器在每完成一个 (标的,周期) 后增量维护）；
    该表不存在或为空时回退原全表统计。全池上亿行后，全表 GROUP BY 不能放进每次请求。
    """
    with use_db() as con:
        try:
            rows = con.execute(
                "SELECT exchange,market,symbol,timeframe,rows,first_open,last_close,"
                "expected,missing FROM coverage_cache").fetchall()
        except sqlite3.OperationalError:
            rows = None
    if rows:
        return [{"exchange": r["exchange"], "market": r["market"], "symbol": r["symbol"],
                 "timeframe": r["timeframe"], "first_open": r["first_open"],
                 "last_close": r["last_close"], "rows": r["rows"],
                 "expected": r["expected"],
                 "missing_within_observed_range": r["missing"]} for r in rows]
    return STORE.status()["已入库覆盖"]


def sources_index():
    """一次取回全部来源，避免逐 (标的,周期) 查询（N+1）。

    只取每个 (exchange,market,symbol,timeframe) 的**最新一条**（imported_at 最大），
    由 SQL 的 GROUP BY 在库内归约，而不是把全表读进 Python 再 `setdefault` 去重。
    全表已超 12 万行：旧写法要把**每一行**都算一次 `iso()`（时间戳格式化），
    实测 1.6~2.3 秒，且超过上层 2 秒 TTL → 缓存条目写入即过期。
    改写后只返回约 3.8 千行，实测 0.1 秒级。**结果口径不变**（仍是最新导入记录）。
    """
    with use_db() as con:
        try:
            rows = con.execute(
                "SELECT s.id,s.exchange,s.market,s.symbol,s.timeframe,s.sha256,"
                "s.source_url,s.imported_at FROM sources s JOIN ("
                "  SELECT exchange,market,symbol,timeframe,MAX(imported_at) AS mi"
                "  FROM sources GROUP BY exchange,market,symbol,timeframe) g"
                " ON s.exchange=g.exchange AND s.market=g.market AND s.symbol=g.symbol"
                " AND s.timeframe=g.timeframe AND s.imported_at=g.mi").fetchall()
        except sqlite3.OperationalError:
            return {}
    idx = {}
    for r in rows:
        idx.setdefault((r["exchange"], r["market"], r["symbol"], r["timeframe"]),
                       {"source_id": r["id"], "sha256": r["sha256"],
                        "source_url": r["source_url"], "imported_at": iso(r["imported_at"])})
    return idx


def source_of(exchange, market, symbol, timeframe, src_idx=None):
    if src_idx is not None:
        return src_idx.get((exchange, market, symbol, timeframe))
    with use_db() as con:
        r = con.execute("SELECT id,sha256,source_url,imported_at FROM sources WHERE exchange=? "
                        "AND market=? AND symbol=? AND timeframe=? ORDER BY imported_at DESC LIMIT 1",
                        (exchange, market, symbol, timeframe)).fetchone()
    if not r:
        return None
    return {"source_id": r["id"], "sha256": r["sha256"], "source_url": r["source_url"],
            "imported_at": iso(r["imported_at"])}


def cells_for(exchange, symbol, cov=None, src_idx=None):
    out = []
    for c in (covered() if cov is None else cov):
        if c["exchange"] != exchange or c["symbol"] != symbol:
            continue
        # 保留 NULL：计数未知与「缺 0 根」是两回事，不得把未知当成 0
        miss = c.get("missing_within_observed_range")
        exp = c.get("expected")
        src = source_of(exchange, c["market"], c["symbol"], c["timeframe"], src_idx)
        out.append({
            "timeframe": c["timeframe"], "rows": c["rows"],
            "market": c["market"],
            "expected": exp,
            "missing_within_observed_range": miss,
            "earliest": iso(c["first_open"]), "latest": iso(c["last_close"]),
            # 逐段缺口区间未入库（下载器只落「缺根计数」）。**不猜区间、不断言成因**：
            # 逐段定位需按标的扫描 candles 视图，全池下每次请求不可承受；且仅凭计数
            # 无法区分「官方归档空洞」与「本地丢弃」，故只给计数与说明。
            "gaps": [],
            "gaps_note": ("缺根计数未知（未逐段定位）" if miss is None else
                          (f"已存首末范围内缺 {miss} 根（仅计数，未逐段定位区间；成因未核验）"
                           if miss else "已存首末范围内按开盘时间与周期推算无缺根")),
            "unit": UNIT["klines"],
            "source": src, "available_at": None, "available_at_known": False,
            "evidence_level": "样例已实取",
        })
    out.sort(key=lambda x: TF_ORDER.index(x["timeframe"]) if x["timeframe"] in TF_ORDER else 99)
    return out


def spot_status_of(p):
    """把 symbol_pool 的**现货原始状态**映射到前端契约的 listed/not_listed/unknown。

    现货快照是按 `symbolStatus=TRADING` 过滤取的，因此：
      · 命中且为 TRADING  → listed
      · 命中但非 TRADING  → not_listed（快照内存在但已停牌/退市）
      · 未命中（无原始状态）→ **unknown**，不得当作未挂牌

    始终返回契约枚举值，不返回 None：无法判定时就是显式的 unknown。
    """
    raw = (p or {}).get("spot_status")
    if raw == SPOT_LISTED_STATUS:
        return "listed"
    if raw:
        return "not_listed"
    return "unknown"


def market_summary(exchange, symbol, cells, market=None):
    """折叠行摘要（**后端计算**，前端只显示）。

    取该标的**已存数据中周期最粗**的一档（优先 1d，见 SUMMARY_TF_PREF）的最近一根已收盘 bar：
      · latest_close  —— 该 bar 的收盘价（**历史归档的本地最后收盘，不是实时价**）
      · change_ratio  —— 相邻两根收盘的变化比例（后端算，前端不算）
      · quote_volume  —— **该 bar 的**计价成交额（**不是**滚动 24h 成交额）
      · event_time    —— 该 bar 的收盘事件时间
    返回 (summary | None, 该周期对应的 coverage cell | None)。取不到就返回 (None, ...)，
    不用 0 或占位值冒充。

    市场标识取值顺序（不依赖偶然键，避免 KeyError）：
      ① coverage cell 的 `market`（来自 `covered()` 原始记录）
      ② 调用方传入的明确映射（`symbol_pool.market`，如 binance=um / okx=swap）
      ③ 两者都取不到 → 不猜，返回空摘要
    """
    by_tf = {c["timeframe"]: c for c in cells}
    tf = next((t for t in SUMMARY_TF_PREF if t in by_tf), None)
    if tf is None:
        return None, None
    cell = by_tf[tf]
    mk = cell.get("market") or market
    if not mk:
        return None, cell
    with use_db() as con:
        rows = con.execute(
            "SELECT close_time,close,quote_volume FROM candles WHERE exchange=? AND market=? "
            "AND symbol=? AND timeframe=? ORDER BY open_time DESC LIMIT 2",
            (exchange, mk, symbol, tf)).fetchall()
    if not rows:
        return None, cell
    last = rows[0]
    prev_close = num(rows[1]["close"]) if len(rows) > 1 else None
    close_v = num(last["close"])
    ratio = None
    if close_v is not None and prev_close not in (None, 0):
        ratio = (close_v - prev_close) / prev_close
    return ({"timeframe": tf, "latest_close": close_v, "change_ratio": ratio,
             "quote_volume": num(last["quote_volume"]),
             "event_time": iso(last["close_time"])}, cell)


def pool_index():
    """合约目录快照（真实 exchangeInfo 来源）。缺失/未同步时返回 {}，不编造。"""
    with use_db() as con:
        try:
            rows = con.execute("SELECT * FROM symbol_pool").fetchall()
        except sqlite3.OperationalError:
            return {}
    return {(r["exchange"], r["symbol"]): dict(r) for r in rows}


# 标的池分类产物目录（可用环境变量覆盖；测试指向空目录 ⇒ 降级为旧口径）
POOL_DIR = os.environ.get("MARKET_POOL_DIR", os.path.join(HERE, "历史下载", "标的池"))
_JSON_CACHE = {}
_JSON_LOCK = threading.Lock()


def _json_cached(name):
    """读取标的池分类产物（按 mtime 缓存，避免每次请求重解析大 JSON）。"""
    path = os.path.join(POOL_DIR, name)
    try:
        mt = os.stat(path).st_mtime_ns
    except OSError:
        return None
    with _JSON_LOCK:
        hit = _JSON_CACHE.get(path)
        if hit and hit[0] == mt:
            return hit[1]
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    with _JSON_LOCK:
        _JSON_CACHE[path] = (mt, data)
    return data


def cls_index():
    """标的分类索引（symbol_classification）→ {(exchange, symbol): row}。"""
    with use_db() as con:
        try:
            return {(r["exchange"], r["symbol"]): dict(r)
                    for r in con.execute("SELECT * FROM symbol_classification")}
        except sqlite3.OperationalError:
            return {}


def workbench_scope():
    """工作台活动池口径：**币安 526 COIN + 欧易仅补 23**；INDEX 另列，旧数据不删。

    返回 (active_keys, index_items, meta)：
      · active_keys —— 工作台主列表口径（币安 COIN ∪ 欧易补充候选）
      · index_items —— 加密指数（另列，不冒充币）
      · meta        —— 计数与「映射不明待核」说明（含 LUNA 等）
    """
    b = _json_cached("binance_classified.json") or {}
    x = _json_cached("cross_exchange_supplement.json") or {}
    coins = b.get("coins") or []
    idx = b.get("crypto_index") or []
    supp = x.get("supplement") or []
    active = {("binance", c["symbol"]) for c in coins}
    active |= {("okx", s["inst_id"]) for s in supp}
    index_items = [{"exchange": "binance", "market": "um", "symbol": c["symbol"],
                    "base": c.get("base_asset"), "instrument_category": "Crypto Index",
                    "list_section": "index", "note": "加密指数：另列，不冒充币"}
                   for c in idx]
    unverified = [{"exchange": "okx", "symbol": s["inst_id"], "base": s.get("base"),
                   "naming_risk": s.get("naming_risk")}
                  for s in supp if s.get("naming_risk")]
    okx_total = x.get("okx_crypto_live_usdt_linear")
    meta = {
        "binance_coins": len(coins), "binance_index": len(idx),
        "okx_crypto_total": okx_total, "okx_supplement": len(supp),
        "okx_duplicated_excluded": ((okx_total - len(supp))
                                    if isinstance(okx_total, int) else None),
        "active_total": len(active),
        "index_note": "币安加密指数另列，不计入币池",
        "mapping_unverified": unverified,
        "mapping_unverified_note": ("跨所同名未确认/命名相近的补充标的保留待核说明；"
                                    "不靠名字猜、不盲删数字倍数"),
    }
    return active, index_items, meta


def realtime_index():
    """一次性读取全部**实时最新快照**（realtime_quotes）。

    实时表与已收盘 K 线**分开**：本表只放"当前最新值"，不参与 K 线覆盖统计，
    也不冒充已收盘 bar。表不存在（未迁移）时返回空 dict，不报错。
    """
    with use_db() as con:
        try:
            return {(r["exchange"], r["symbol"]): dict(r)
                    for r in con.execute("SELECT * FROM realtime_quotes")}
        except sqlite3.OperationalError:
            return {}


def oi_latest_index():
    """每个标的的**最新一条 OI 快照**（`oi_history`，仅 `period='snapshot'`）。

    必须限定 `period='snapshot'`：`oi_history` 现已同时存放 5m 历史归档行，
    若不限定，`MAX(ts)` 聚合要扫全表（历史行数达亿级），且同 ts 的 5m 行会与
    快照行**同时命中**导致取值歧义。限定后语义与修复前一致（取实时快照）。
    """
    with use_db() as con:
        try:
            return {(r["exchange"], r["symbol"]): dict(r) for r in con.execute(
                "SELECT o.* FROM oi_history o JOIN (SELECT exchange,market,symbol,MAX(ts) ts "
                "FROM oi_history WHERE period='snapshot' GROUP BY exchange,market,symbol) m "
                "ON o.exchange=m.exchange AND o.market=m.market AND o.symbol=m.symbol "
                "AND o.ts=m.ts AND o.period='snapshot'")}
        except sqlite3.OperationalError:
            return {}


def funding_latest_index():
    """每个标的的**最近一次真实结算事件**（funding_history）；不插值。"""
    with use_db() as con:
        try:
            return {(r["exchange"], r["symbol"]): dict(r) for r in con.execute(
                "SELECT f.* FROM funding_history f JOIN (SELECT exchange,market,symbol,"
                "MAX(funding_time) ft FROM funding_history GROUP BY exchange,market,symbol) m "
                "ON f.exchange=m.exchange AND f.market=m.market AND f.symbol=m.symbol "
                "AND f.funding_time=m.ft")}
        except sqlite3.OperationalError:
            return {}


def supply_index():
    """流通量索引（`symbol_supply`，每合约一行，约 5 百行）。

    来源 = 币安公开 `openInterestHist` 同一行的 `CMCCirculatingSupply`
    （采集器 `oi_sweep` 本来就取回该行，只是此前被丢弃，2026-10-11 起落库）。
    返回 `((exchange, symbol), row)`；表不存在（未迁移）⇒ 空 dict，不报错、不补值。
    """
    with use_db() as con:
        try:
            return {(r["exchange"], r["symbol"]): dict(r) for r in con.execute(
                "SELECT exchange,market,symbol,base,circulating_supply,unit_note,source,"
                "supply_event_time,fetched_at FROM symbol_supply")}
        except sqlite3.OperationalError:
            return {}


# 各周期的**毫秒时长**：由「最近一根已收盘 bar 的 close_time」反推它的 open_time，
# 从而按主键 `(exchange,market,symbol,timeframe,open_time)` 精确命中那一行。
# 为什么不在 SQL 里直接按 close_time 关联：`candle_versions` 只有主键索引
# `(exchange,market,symbol,timeframe,open_time)`，按 close_time 关联实测**退化为全表扫描**
# （1.3 亿行，>120 秒未返回）。改用主键后实测 3,926 次命中仅 **0.63 秒**。
TF_DURATION_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
                  "1h": 3_600_000, "4h": 14_400_000, "8h": 28_800_000,
                  "12h": 43_200_000, "1d": 86_400_000}


def last_closed_volume_index():
    """各 (exchange, market, symbol, timeframe) **最近一根已收盘 bar** 的成交额。

    为什么需要它：界面第 3 列的「周期滚动成交额」（5m/15m/30m/1h/… 每 5 秒轮换）
    此前只对**当前展开的那一个合约**有值（因为只有它才会去拉 180 根明细 K 线），
    其余 548 行恒为 `—` —— 这正是用户反馈的「周期滚动成交额没有接通」。

    口径：**只取已收盘 bar**（`candle_versions` 的 `is_closed` 有 `CHECK(is_closed = 1)`，
    未收盘 bar 另存在 `realtime_klines` 临时层）。金额一律取 `quote_volume`
    （计价币 / USDT 口径），与 `/markets` 的 `volume_24h_quote` 同口径，
    **不拿标的币量按价格折算**。

    返回 `((exchange, symbol), {timeframe: {"quote_volume","base_volume","close","close_time"}})`；
    任一步取不到就跳过该 (标的,周期)，**不补 0、不用别的周期顶替**。
    """
    with use_db() as con:
        try:
            cov = con.execute("SELECT exchange,market,symbol,timeframe,last_close "
                              "FROM coverage_cache WHERE last_close IS NOT NULL").fetchall()
        except sqlite3.OperationalError:
            return {}
        out = {}
        q = ("SELECT quote_volume,volume,close,close_time FROM candle_versions "
             "WHERE exchange=? AND market=? AND symbol=? AND timeframe=? AND open_time=?")
        for r in cov:
            dur = TF_DURATION_MS.get(r["timeframe"])
            if not dur:
                continue
            hit = con.execute(q, (r["exchange"], r["market"], r["symbol"],
                                  r["timeframe"], int(r["last_close"]) - dur + 1)).fetchone()
            if not hit:
                continue
            out.setdefault((r["exchange"], r["symbol"]), {})[r["timeframe"]] = {
                "quote_volume": num(hit["quote_volume"]),
                "base_volume": num(hit["volume"]),
                "close": num(hit["close"]),
                "close_time": iso(hit["close_time"]),
            }
        return out


def signal_latest_index():
    """每个 (exchange, symbol) 的**最近一条真实信号** → {(ex, sym): row}。

    取值口径（与 `/api/v1/signals` 的排序一致）：
      · 按 `confirmed_at DESC, id DESC` 取**第一行**，即最近被确认的那条；
        `confirmed_at` 是**确认时点**，不是 event_time，也不回填。
      · 窗口函数取每标的 top-1，避免全表读入。

    SIG07（2026-10-11）：**按当前版本/状态过滤方向**。只接受
      当前启用族（F_WX）+ `status='confirmed'` + 身份非空 + `strategy_version` 等于
      当前生效版本 + `config_hash` 等于该臂当前生效配置的信号；
      任一不符 ⇒ 该标的**不给出方向**（fail closed，不用历史/旧版本事件冒充当前）。

    诚实边界：
      · `signals` 表不存在 / 为空 → 返回 `{}`，**绝不**用 24h 涨跌、taker 买卖比、
        人数多空比等派生量编造方向。
      · 方向只认 `long` / `short`；状态只认 `SIGNAL_STATUSES`，其余回 null。
    """
    ctx = _current_run_ctx()
    ph = ",".join("?" * len(CURRENT_RUN_KEYS))
    with use_db() as con:
        try:
            rows = con.execute(
                "SELECT * FROM signals WHERE strategy_key IN (" + ph + ") "
                "AND strategy_version IS NOT NULL AND config_hash IS NOT NULL "
                "AND data_version IS NOT NULL "
                "ORDER BY confirmed_at DESC, id DESC LIMIT 5000",
                sorted(CURRENT_RUN_KEYS)).fetchall()
        except sqlite3.OperationalError:
            return {}
    arms = ctx.get("arms", {})
    out = {}
    for r in rows:                       # 已按 confirmed_at DESC 排序 ⇒ 每个标的首见即最新
        if r["strategy_version"] != ctx.get("version"):
            continue
        if arms.get(r["strategy_key"]) != r["config_hash"]:
            continue
        key = (r["exchange"], r["symbol"])
        if key not in out:
            out[key] = dict(r)
    return out


# 列表行聪明钱快照缓存：(exchange, mid) -> (mtime_ns, 精简 payload)。
# 文件未变零成本；采集器每轮重写约 500 个小 JSON，均摊每秒 ~1 次重解析。
_SM_ROW_CACHE = {}


def _smart_money_row(exchange, symbol):
    """列表行的聪明钱位：**只读本机快照**（mtime 缓存；不联网、不触发任何交易所请求）。

    2026-10-08 修复「列表恒显空态」：`/markets` 行内嵌入快照的 overview/stats
    （页面六槽的真实来源）+ quality（新鲜/陈旧）+ 快照时间；列表与展开行从此
    读同一份后端数据。快照缺失/非币安返回 None（调用方落回显式缺失原因）。
    """
    try:
        p = smart_money._snap_path(exchange, symbol)
        mt = p.stat().st_mtime_ns
    except (OSError, ValueError):
        return None
    key = (exchange, symbol)
    hit = _SM_ROW_CACHE.get(key)
    if hit and hit[0] == mt:
        return hit[1]
    snap = smart_money.read(exchange, symbol)
    if not isinstance(snap, dict):
        return None
    fr = smart_money._freshness(snap)
    out = {
        "available": bool(snap.get("available")),
        "overview": snap.get("overview"),
        "stats": snap.get("stats"),
        "quality": {"status": fr.get("status"), "age_seconds": fr.get("age_seconds"),
                    "stale_after_s": fr.get("stale_after_s"),
                    "local_fetched_at": fr.get("local_fetched_at"),
                    "note": ((fr.get("note") or "")[:200] or None)},
        "snapshot": {"refreshed_at": snap.get("snapshot_refreshed_at")},
        "source": snap.get("source"),
    }
    _SM_ROW_CACHE[key] = (mt, out)
    return out


def market_item(exchange, symbol, cells=None, pool=None, rt=None, oi=None, fund=None, cls=None,
                sig=None, sup=None, sup_by_base=None, vol=None):
    cells = cells_for(exchange, symbol) if cells is None else cells
    p = pool or {}
    c = cls or {}
    is_perp = (p.get("contract_type") in PERP_CONTRACT_TYPES
               and p.get("quote_asset") == "USDT") if p else None
    summary, sum_cell = market_summary(exchange, symbol, cells, market=p.get("market"))
    # 数据时点：摘要那一档的**收盘事件时间**（不是抓取时间，也不是实时时间）
    data_time = summary["event_time"] if summary else None
    # 缺口：只用 covered()/coverage_cache 的**精确缺根计数**。
    #   · 计数已知 → 明确写出根数（含 0 根），界面不显示「—」
    #   · 计数未知 → 留空（不猜）
    #   · **不断言成因**：计数无法区分「官方归档空洞」与「本地丢弃」，故不写任何成因结论
    data_missing_reason = None
    miss = (sum_cell or {}).get("missing_within_observed_range")
    if miss is not None:
        data_missing_reason = f"{sum_cell['timeframe']} 该已存范围缺 {miss} 根"
        if miss:
            data_missing_reason += "（仅计数，未逐段定位区间）"

    # ── 实时字段：只接**真实来源**的值；没有就留空，绝不用历史收盘或日线量冒充 ──
    # 数据来源：realtime_quotes（价格/24h/标记价/当前费率）、oi_history（最新 OI）、
    #          funding_history（最近一次真实结算事件）。
    oi_row, fund_row = oi or {}, fund or {}
    oi_total = oi_row.get("open_interest") or oi_row.get("oi_ccy")
    oi_unit = None
    if oi_row:
        oi_unit = "张/标的币" if exchange == "binance" else "张(oiCcy 另存)"
    # SIG07：行内「当前可执行」标记与全局运行门禁同源（缓存 10s）。
    sig_brief = _signal_brief(sig, _current_run_ctx()) if sig else {}

    # ── 流动市值（2026-10-11 接通）────────────────────────────────────────────
    # 口径：`market_cap_usd = 最新价 × CMC 流通量`，分子分母**同为合约标的币单位**
    #      （币安 `CMCCirculatingSupply` 就是以合约标的币计，如 1000PEPE 合约给的是
    #       以 1000PEPE 为单位的流通量，故与合约价直接相乘即得美元市值）。
    # 来源：币安公开 `openInterestHist` 同一行（采集器 oi_sweep 本来就取回，此前被丢弃）。
    # 纪律：两者缺一 ⇒ 市值留空（**不补值、不用现价乘历史供应量、不跨币顶替**）；
    #      `market_cap_as_of` 取「价格时点」与「供应量时点」中**较晚**者，
    #      绝不把「快照何时写」当成「指标何时发生」。
    #      OKX 公开端点无流通量字段 ⇒ 仅当**标的币同名**时复用币安同名币的流通量，
    #      并由 `market_cap_note` 如实标注；匹配不到就留空。
    sup_row = (sup or {}).get((exchange, symbol))
    sup_note = None
    if sup_row is not None:
        sup_note = "流通量取自币安公开 openInterestHist 的 CMCCirculatingSupply（同合约）"
    else:
        base_asset = p.get("base_asset")
        cand = (sup_by_base or {}).get(base_asset) if base_asset else None
        if cand is not None:
            sup_row = cand
            sup_note = (f"本所公开端点无流通量字段；按**标的币 {base_asset}** 复用"
                        f"币安同名合约的 CMCCirculatingSupply（非本所来源，如实标注）")
    price_now = num((rt or {}).get("last_price"))
    supply_now = num(sup_row.get("circulating_supply")) if sup_row else None
    market_cap = (price_now * supply_now
                  if price_now not in (None, 0) and supply_now not in (None, 0) else None)
    mc_times = [t for t in ((rt or {}).get("price_event_time"),
                            (sup_row or {}).get("supply_event_time")) if t]
    market_cap_as_of = iso(max(mc_times)) if (market_cap is not None and mc_times) else None

    # ── 周期成交额（2026-10-11 接通）──────────────────────────────────────────
    # 第 3 列每 5 秒在 5m/15m/…/1d 之间轮换；此前只有**展开的那一个合约**有值
    # （只有它才会拉 180 根明细 K 线），其余 548 行恒为 `—`。
    # 现由后端直接给出**各周期最近一根已收盘 bar** 的计价成交额（口径见
    # `last_closed_volume_index`），全部 549 行都有值，且与展开图同口径（只用已收盘 bar）。
    vol_by_tf = (vol or {}).get((exchange, symbol)) or {}

    return {
        "exchange": exchange, "market_id": symbol, "symbol": symbol,
        "base": p.get("base_asset"), "quote": p.get("quote_asset"),
        "contract_type": p.get("contract_type"), "is_usdt_perp": is_perp,
        # 现货状态按前端契约映射（listed/not_listed/unknown）；无来源 = unknown
        "spot_status": spot_status_of(p),
        # 数据总池 ≠ 交易资格：资格规则未冻结 → 一律不下结论
        "eligibility": None,
        "eligibility_reason": ("交易资格未定：中小市值门槛与筛选规则尚未冻结"
                               "（本列表是数据总池，不是可交易清单）"),
        "market_cap_usd": market_cap,
        "market_cap_as_of": market_cap_as_of,
        "market_cap_note": sup_note,
        "circulating_supply": supply_now,
        "liquidity_status": None,
        "quote_volume_median_30d": None,
        "volume_by_timeframe": vol_by_tf,
        "available_timeframes": [c["timeframe"] for c in cells],
        "coverage": cells,
        "oi": ({"available": True, "rows": 1, "latest_ts": oi_row.get("ts"),
                "open_interest": oi_total, "oi_ccy": oi_row.get("oi_ccy"),
                "oi_usd": oi_row.get("oi_usd"), "source": oi_row.get("source"),
                "note": "最新一条 OI 快照；全池 OI 为**轮询**，新鲜度见 collector_rounds"}
               if oi_row else
               {"available": False, "reason": "OI 历史表暂无该标的记录；不自动下载"}),
        "funding": ({"available": True, "latest_funding_time": fund_row.get("funding_time"),
                     "funding_rate": fund_row.get("funding_rate"),
                     "rate_type": fund_row.get("rate_type"), "source": fund_row.get("source"),
                     "note": "最近一次**真实结算事件**；不插值、不冒充每小时事件"}
                    if fund_row else
                    {"available": False, "reason": "资金费历史表暂无该标的记录；不自动下载"}),
        # 聪明钱：读本机快照（realtime_collector.smart_money_loop 周期写入），
        # 带 quality（新鲜/陈旧/缺失）与快照时间；无快照保持显式缺失，不补值。
        "smart_money": (_smart_money_row(exchange, symbol)
                        or {"available": False, "reason": SMART_MONEY_REASON}),
        # 2026-10-11：`one_minute` 字段已随 1m 彻底删除一并摘除（前端零消费，无兼容期）。
        # 折叠行摘要：真实已存最后收盘 + 该周期 + 事件时间（前端只显示，不计算）
        "summary": summary,
        "data_time": data_time,
        "data_missing_reason": data_missing_reason,
        # ── 实时 flat 字段（契约字段；真实来源，无行即 null，不用历史收盘/日线量冒充）──
        # **所有价格/成交量/OI/费率一律经 num() 转 JSON number**：库里存 TEXT，
        # 直接透传会让前端 fmtPrice 收到字符串而显示「—」。
        "last_price": num((rt or {}).get("last_price")),
        "price_change_ratio_24h": num((rt or {}).get("price_change_pct_24h")),
        "realtime_time": iso((rt or {}).get("price_event_time")),
        "realtime_received_at": iso((rt or {}).get("price_received_at")),
        "realtime_source": (_src_label(rt) if rt else None),
        "realtime_status": _rt_status(rt, _now_ms()),
        # 成交量：明确是**计价额**还是**标的量**，两者不混用；缺源即 null
        "volume_24h_quote": num((rt or {}).get("quote_volume_24h")),
        "volume_24h_base": num((rt or {}).get("volume_24h_base")),
        "volume_source": ((rt or {}).get("volume_source") if rt else None),
        "volume_24h_quote_note": ((rt or {}).get("unit_note") if rt else None),
        "volume_by_timeframe_note": ("各周期取**最近一根已收盘 bar** 的计价(USDT)成交额"
                                     "（quote_volume）；未收盘 bar 另存实时层，不参与本字段"),
        # 标记价 / 当前费率：各自独立时点与来源，不共用价格时点
        "mark_price": num((rt or {}).get("mark_price")),
        "mark_time": iso((rt or {}).get("mark_event_time")),
        "mark_source": ((rt or {}).get("mark_source") if rt else None),
        "funding_rate": (num((rt or {}).get("funding_rate"))
                         if (rt or {}).get("funding_rate") not in (None, "")
                         else num(fund_row.get("funding_rate"))),
        "next_funding_time": iso((rt or {}).get("next_funding_time")),
        "funding_time": iso((rt or {}).get("funding_event_time")),
        "funding_source": ((rt or {}).get("funding_source") if rt else None),
        # OI：独立时间与来源，不拿价格时点掩盖全池扫池延迟
        "open_interest_total": num(oi_total),
        "open_interest_unit": oi_unit,
        "open_interest_usd": num(oi_row.get("oi_usd")),
        "oi_time": iso(oi_row.get("ts")),
        "oi_source": oi_row.get("source"),
        # 分类（工作台活动口径）
        "instrument_category": c.get("instrument_category"),
        "category_name": c.get("category_name"),
        "in_active_range": c.get("in_active_range"),
        "excluded_reason": c.get("excluded_reason"),
        "list_section": "active" if c.get("in_active_range") == 1 else "index",
        # 向后兼容的嵌套对象（字段名同步为独立时点）
        "realtime": ({"last_price": num(rt.get("last_price")),
                      "price_change_ratio_24h": num(rt.get("price_change_pct_24h")),
                      "event_time": iso(rt.get("price_event_time")),
                      "received_at": iso(rt.get("price_received_at")),
                      "source": rt.get("price_source"), "transport": rt.get("price_transport")}
                     if rt else None),
        "realtime_note": ("实时最新价与已收盘 K 线分开存放；未收盘 bar 只作图表临时层，"
                          "不写 candle_* 表，也不与历史最后收盘混用"),
        # ── 方向字段：只来自 `signals` 表的**当前启用族真实信号**（F_WX 3a–3d）──
        # 无信号 → 两个字段都为 null（前端显示「—」/未知），**不造方向**：
        # 不用 24h 涨跌、taker 买卖比、人数多空比等派生量冒充策略方向。
        # SIG07：`signal_latest_index` 已按**当前版本/配置**过滤来源；此处再按**状态**过滤方向
        # （仅 confirmed 才给出方向；失效/过期/候选/未决一律 null），并给出「当前可执行」标记
        # （运行未放行时 current_direction 为 null，避免把历史事件当当前提示）。
        "signal_status": (sig.get("status")
                          if sig and sig.get("status") in SIGNAL_STATUSES else None),
        "signal_direction": (sig.get("direction")
                             if (sig and sig.get("status") == "confirmed"
                                 and sig.get("direction") in SIGNAL_DIRECTIONS) else None),
        "signal_actionable": (sig_brief.get("actionable") if sig else None),
        "signal_current_direction": (sig_brief.get("current_direction") if sig else None),
        "signal_actionable_reason": (sig_brief.get("actionable_reason") if sig else None),
        # 方向来源链（仅在有真实信号时出现；供核对「标的/策略/周期/时点」）
        "signal_ref": ({"signal_id": str(sig["id"]), "strategy_key": sig.get("strategy_key"),
                        "strategy_version": sig.get("strategy_version"),
                        "config_hash": sig.get("config_hash"),
                        "timeframe": sig.get("timeframe") if sig.get("timeframe") in TF_SET else None,
                        "status": sig.get("status"),
                        "direction": sig.get("direction"),
                        "event_time": iso(sig.get("event_time")),
                        "confirmed_at": iso(sig.get("confirmed_at")),
                        "data_version": sig.get("data_version")} if sig else None),
        "note": "仅列出统一数据库中真实存在的标的；不造全市场币池",
    }


def api_stats():
    """来源数 / **公共 REST** 调用计数（成功与失败分开）/ 冷却状态。

    边界说明：`api_calls` 只记**公共 REST 网关**的调用（币安 exchangeInfo 等目录端点）。
    官方归档是公开静态资源，由 `历史下载/batch_download.py` 的**独立归档下载器**直连
    `data.binance.vision` / S3 列举端点下载，**不经 REST 网关、不计入 api_calls**。
    因此本函数返回的计数**不能**用来断言「归档下载已连通」。

    性能（BUG-003 修复）：`sources`/`api_calls` 的精确计数是**无界全表 COUNT**
    （本窗只读实测：sources 1.4~7.6s、api_calls status >3s），已**移出请求路径**：
    走 `_heavy_stats` 非阻塞缓存；尚未就绪时返回 None（暂不可得），
    **不填 0、不冒充精确**。计数语义不变。
    """
    nsrc = _heavy_stats("stats:sources", _count_sources)
    counts = _heavy_stats("stats:api_calls", _api_calls_counts)
    ok_calls = counts[0] if counts else None
    failed_calls = counts[1] if counts else None
    calls = ((ok_calls + failed_calls)
             if (ok_calls is not None and failed_calls is not None) else None)
    states = []
    with use_db() as con:
        try:
            states = [dict(r) for r in con.execute("SELECT * FROM api_scope_state")]
        except sqlite3.OperationalError:
            pass
    return {"sources": nsrc, "ok_calls": ok_calls, "failed_calls": failed_calls,
            "calls": calls, "scope_state": states}


# 采集进程心跳失效阈值：超过 2 分钟未更新视为失效（采集器每 15s 写一次心跳）。
RT_HEARTBEAT_STALE_MS = 120_000


def _rt_oi_snapshot_rows():
    """实时 OI 快照行数（`period='snapshot'`，走部分索引 ix_oi_hist_snapshot，有界）。"""
    with use_db() as con:
        try:
            return con.execute("SELECT COUNT(*) FROM oi_history "
                               "WHERE period='snapshot'").fetchone()[0]
        except sqlite3.OperationalError:
            return None


def _rt_funding_rows():
    """资金费历史行数（表约十万行，主键覆盖索引，成本可忽略）。"""
    with use_db() as con:
        try:
            return con.execute("SELECT COUNT(*) FROM funding_history").fetchone()[0]
        except sqlite3.OperationalError:
            return None


def realtime_health():
    """实时采集健康度：**新鲜真实价格时点 且 有效心跳** 才判 ok。

    判定规则（两条必须同时成立，缺一不可）：
      · `price_fresh`  —— 最新 `price_event_time`（无则退 `price_received_at`）距今 ≤ RT_STALE_MS
      · `hb_valid`     —— `collector_state.heartbeat_at` 存在且距今 ≤ RT_HEARTBEAT_STALE_MS
    因此：
      · 有数据行但**心跳缺失/过期** → `stale`（进程可能已死，数据不再更新）
      · 心跳活跃但**没有新鲜价格消息** → `stale`（进程活着但没收到行情）
      · 两者皆无 → `not_connected`
    绝不"仅凭 n>0 或心跳存在"就判 ok。
    """
    now = _now_ms()
    st, counters = {}, {}
    n = newest_px = newest_recv = 0
    with use_db() as con:
        try:
            r = con.execute("SELECT * FROM collector_state WHERE name='realtime'").fetchone()
            st = dict(r) if r else {}
        except sqlite3.OperationalError:
            st = {}
        try:
            row = con.execute("SELECT COUNT(*), MAX(price_event_time), MAX(price_received_at) "
                              "FROM realtime_quotes").fetchone()
            n, newest_px, newest_recv = (row[0] or 0), row[1], row[2]
        except sqlite3.OperationalError:
            pass
    # oi_rows / funding_rows 是**信息性计数**：走 30s TTL 缓存，避免每轮轮询重扫
    # 部分索引/主键覆盖索引；取不到时如实为 None，**不填 0**。
    # （OI 只统计实时快照分区 period='snapshot'，走部分索引 ix_oi_hist_snapshot，
    #  与「实时 OI 快照行数」语义一致；5m 历史行数见离线清单，不在此全表 COUNT。）
    oi_n = _ttl("rt:oi_snapshot_rows", 30.0, _rt_oi_snapshot_rows)
    fu_n = _ttl("rt:funding_rows", 30.0, _rt_funding_rows)
    if st.get("counters_json"):
        try:
            counters = json.loads(st["counters_json"])
        except ValueError:
            counters = {}
    hb = st.get("heartbeat_at")
    hb_age = (now - int(hb)) if hb else None
    # 价格时点：优先 price_event_time（上游事件时间），缺失才退 price_received_at
    px_ts = newest_px or newest_recv
    px_age = (now - int(px_ts)) if px_ts else None
    price_fresh = px_age is not None and px_age <= RT_STALE_MS
    hb_valid = hb_age is not None and hb_age <= RT_HEARTBEAT_STALE_MS
    if not n and hb_age is None:
        status = "not_connected"               # 既无数据也无心跳 → 未接通
    elif not hb_valid:
        status = "stale"                       # 缺心跳 / 心跳过期 → 不能判 ok
    elif not price_fresh:
        status = "stale"                       # 心跳活跃但无新鲜价格消息 → stale
    else:
        status = "ok"
    return {"status": status, "rows": n, "newest_price_time": newest_px,
            "newest_received_at": newest_recv, "price_age_s": (round(px_age / 1000)
                                                              if px_age is not None else None),
            "price_fresh": price_fresh,
            "heartbeat_at": hb, "heartbeat_valid": hb_valid,
            "heartbeat_age_s": (round(hb_age / 1000) if hb_age is not None else None),
            "pid": st.get("pid"), "collector_status": st.get("status"),
            "counters": counters, "oi_rows": oi_n, "funding_rows": fu_n,
            "detail": st.get("detail")}


def health_items():
    cov = covered()
    rows = sum(c["rows"] for c in cov)
    last_close = max((c["last_close"] for c in cov if c["last_close"]), default=None)
    st = api_stats()
    rh = realtime_health()
    ok_calls, failed_calls = st["ok_calls"], st["failed_calls"]
    if ok_calls is None and failed_calls is None:
        # 重统计尚未就绪：显式「暂不可得」，**不填 0**（BUG-003 修复后非阻塞返回）
        rest_detail = "成功公共 REST 请求计数**暂不可得**（无界全表 COUNT 已移出请求路径，重统计未就绪）"
    else:
        rest_detail = f"成功公共 REST 请求 {ok_calls} 次"
        if failed_calls:
            rest_detail += f"，失败 {failed_calls} 次（失败不计作连通）"
    rest_detail += ("：币安 USD-M exchangeInfo 合约目录、币安现货目录；"
                    "经统一网关（限频 + 缓存 + 并发租约）。前端读取不触发外部采集。")
    return [
        {"name": "统一行情数据库", "status": "ok" if rows else "missing",
         "last_event_time": iso(last_close), "last_received_at": None, "age_seconds": None,
         "detail": f"{DB_PATH}；已入库 {len(cov)} 个（标的,周期）组合，共 {rows} 根真实K线"},
        # ① 公共 REST 网关：**只按成功请求留证**，失败请求单独计数，不当作已连通
        {"name": "公共 REST 网关（目录/端点）",
         "status": ("missing" if ok_calls is None else
                    ("ok" if ok_calls else ("not_connected" if not failed_calls else "missing"))),
         "last_event_time": None, "last_received_at": None, "age_seconds": None,
         "detail": rest_detail},
        # ② 官方归档：独立下载器直连静态资源，**不经 REST 网关** —— 与 ① 分开陈述
        {"name": "官方归档下载（独立下载器）",
         "status": "ok" if rows else "missing",
         "last_event_time": iso(last_close), "last_received_at": None, "age_seconds": None,
         "detail": (f"由 `历史下载/batch_download.py` 直连 data.binance.vision / S3 列举端点"
                    f"下载，**不经 REST 网关、不计入 api_calls**；每包核验官方 CHECKSUM。"
                    f"来源归档 {(st['sources'] if st['sources'] is not None else '暂不可得')} 个")},
        {"name": "实时行情采集（价格/OI/费率）",
         "status": rh["status"],
         "last_event_time": iso(rh["newest_price_time"]),
         "last_received_at": iso(rh["newest_received_at"]),
         "age_seconds": rh["heartbeat_age_s"],
         "detail": (f"realtime_quotes 覆盖 {rh['rows']} 标的；"
                    f"最新价格事件 {iso(rh['newest_price_time'])}"
                    f"（{rh['price_age_s']}s 前，新鲜={rh['price_fresh']}）；"
                    f"心跳 {iso(rh['heartbeat_at'])}（{rh['heartbeat_age_s']}s 前，"
                    f"有效={rh['heartbeat_valid']}，pid={rh['pid']}）；"
                    f"WS 消息 币安 {rh['counters'].get('ws_binance', 0)} / "
                    f"欧易 {rh['counters'].get('ws_okx', 0)}；"
                    f"OI {(rh['oi_rows'] if rh['oi_rows'] is not None else '暂不可得')} 行，"
                    f"费率结算 {(rh['funding_rows'] if rh['funding_rows'] is not None else '暂不可得')} 行。"
                    f"判定要求**新鲜价格时点与有效心跳同时成立**，缺一即 stale"
                    if rh["rows"] else
                    "realtime_quotes 无数据：实时采集未运行或尚未写入")},
        {"name": "账户/交易", "status": "not_connected", "last_event_time": None,
         "last_received_at": None, "age_seconds": None, "detail": "未配置账户，不读取凭据"},
    ]


def v1_health():
    return envelope(items=health_items())


def v1_overview():
    cov = covered()
    active, _idx, _meta = workbench_scope()
    markets = set(active) if active else {(c["exchange"], c["symbol"]) for c in cov}
    return envelope(counts={"markets_total": len(markets), "markets_eligible": None,
                            "signals_open": None, "signals_today": None},
                    account_summary=None, alerts=[], data_health=health_items())


def v1_markets():
    """工作台市场列表（**带 2 秒 TTL 缓存**）。

    为什么缓存：本函数要为 549 个标的各查一次摘要（实测单次计算 2.6~16 秒，
    随库内 WAL 膨胀与写入竞争波动）。前端每 5 秒轮询，多客户端叠加时
    计算量会超过轮询周期 → 请求堆积 → 线程/fd 撑爆。
    2 秒 TTL 把「N 个客户端 × 549 次查询」塌缩成「每 2 秒 1 次」，
    远小于前端轮询周期，不改变任何字段与语义（`as_of` 最多滞后 2 秒）。
    """
    return _ttl("v1_markets", 2.0, _v1_markets_compute)


def _v1_markets_compute():
    """工作台市场列表：**活动池口径 = 币安 526 COIN + 欧易仅补 23**。

    收窄原因：上一版把「K线覆盖 ∪ 实时快照 ∪ 数据总池」全部列出（814 行），
    把币安被排除的股票/商品/外汇合约、以及欧易与币安**重复**的加密币也列进了工作台。
    现按最新分类口径过滤：INDEX 另列（crypto_index 字段），重复的欧易币不出现，
    映射不明（如 LUNA）保留待核说明。**旧数据不删**，只是活动范围过滤。
    """
    cov = _ttl("covered", 10.0, covered)
    # 来源归档元数据（文件级 imported_at / sha256 / source_url）：只在历史下载入库时变，
    # 与价格无关。历史下载并发写时全表扫 `sources` 实测 3.6 秒，故给较长 TTL（5 分钟），
    # 配合 `_ttl` 的「先用旧值、后台刷新」，既不阻塞请求也不会陈旧到不可用。
    src_idx = _ttl("sources", 300.0, sources_index)
    pools = _ttl("pool", 30.0, pool_index)
    rt_idx = _ttl("realtime", 3.0, realtime_index)
    oi_idx = _ttl("oi", 3.0, oi_latest_index)
    fund_idx = _ttl("funding", 3.0, funding_latest_index)
    sig_idx = _ttl("signal", 5.0, signal_latest_index)
    cls_idx = _ttl("cls", 60.0, cls_index)
    # 流通量（symbol_supply，约 5 百行）与「最近已收盘 bar 成交额」（实测 0.63s）
    # 都放在 TTL 缓存里：两者都随库增长而变贵，但都**不参与**每请求热路径。
    sup_idx = _ttl("supply", 60.0, supply_index)
    sup_by_base = {}
    for row in sup_idx.values():
        b = row.get("base")
        if b and b not in sup_by_base:
            sup_by_base[b] = row
    vol_idx = _ttl("lastvol", 30.0, last_closed_volume_index)
    groups = {}
    for c in cov:
        groups.setdefault((c["exchange"], c["symbol"]), []).append(c)
    active, index_items, meta = _ttl("scope", 30.0, workbench_scope)
    if active:
        keys = set(active)
    else:
        # 分类产物缺失时**降级**为原「K线覆盖 ∪ 实时快照 ∪ 数据总池」，不返回空列表
        keys = set(groups) | set(rt_idx) | set(pools)
    items = [market_item(e, s, cells=cells_for(e, s, cov=groups.get((e, s), []), src_idx=src_idx),
                         pool=pools.get((e, s)), rt=rt_idx.get((e, s)),
                         oi=oi_idx.get((e, s)), fund=fund_idx.get((e, s)),
                         cls=cls_idx.get((e, s)), sig=sig_idx.get((e, s)),
                         sup=sup_idx, sup_by_base=sup_by_base, vol=vol_idx)
             for (e, s) in sorted(keys)]
    return envelope(items=items, has_more=False, next_cursor=None, total=len(items),
                    crypto_index=index_items, active_scope=meta,
                    quality={"status": "ok", "age_seconds": None,
                             "note": "工作台活动池 = 币安 COIN ∪ 欧易仅补；加密指数另列；"
                                     "总池 ≠ 交易资格；被排除目录与旧数据仍保留在库"})


def v1_market(exchange, mid):
    if mid.startswith(exchange + ":"):
        mid = mid[len(exchange) + 1:]
    rt, oi, fund = (realtime_index().get((exchange, mid)), oi_latest_index().get((exchange, mid)),
                    funding_latest_index().get((exchange, mid)))
    sup_idx = _ttl("supply", 60.0, supply_index)
    sup_by_base = {}
    for row in sup_idx.values():
        b = row.get("base")
        if b and b not in sup_by_base:
            sup_by_base[b] = row
    known = any(c["exchange"] == exchange and c["symbol"] == mid for c in covered())
    if known or rt or (exchange, mid) in pool_index():
        return market_item(exchange, mid, pool=pool_index().get((exchange, mid)),
                           rt=rt, oi=oi, fund=fund, cls=cls_index().get((exchange, mid)),
                           sig=signal_latest_index().get((exchange, mid)),
                           sup=sup_idx, sup_by_base=sup_by_base,
                           vol=_ttl("lastvol", 30.0, last_closed_volume_index))
    return missing(f"统一数据库中无 {exchange}:{mid} 的已入库或实时数据")


def v1_pool():
    """工作台数据池（真实 exchangeInfo 快照，**与 markets 同口径**）。**不是**可交易清单。

    与 markets 一致：只列币安 COIN ∪ 欧易补充；被排除目录（股票/商品/外汇/指数）与
    欧易重复币仍留在库中，但**不在工作台列表**。`pool_all_total` 给出未过滤总数以便核对。
    """
    pools = pool_index()
    if not pools:
        return missing("symbol_pool 为空或未同步；请先运行 历史下载/symbol_pool.py 与 batch_download.py")
    active, index_items, meta = workbench_scope()
    all_total = len(pools)
    shown = {k: p for k, p in pools.items() if (not active) or (k in active)}
    items = []
    for (ex, sym), p in sorted(shown.items()):
        items.append({
            "exchange": ex, "market_id": sym, "symbol": sym,
            "base": p.get("base_asset"), "quote": p.get("quote_asset"),
            "contract_type": p.get("contract_type"), "margin_asset": p.get("margin_asset"),
            "status": p.get("status"), "onboard_time": iso(p.get("onboard_time")),
            "spot_symbol": p.get("spot_symbol"), "spot_status": p.get("spot_status"),
            "spot_match_method": p.get("spot_match_method"),
            "priority_hint": p.get("priority_hint"),
            # 池 ≠ 资格
            "eligibility": None,
            "eligibility_reason": "交易资格未定：中小市值门槛与筛选规则尚未冻结",
        })
    snap = max((p.get("snapshot_utc") or 0) for p in pools.values())
    return envelope(items=items, total=len(items), has_more=False, next_cursor=None,
                    pool_all_total=all_total, crypto_index=index_items, active_scope=meta,
                    quality={"status": "ok", "age_seconds": None,
                             "note": "工作台口径 = 币安 COIN ∪ 欧易仅补（与 markets 同口径）；"
                                     "被排除目录与欧易重复币仍在库但不列出；"
                                     "当前交易中 USDT 线性永续快照；现货挂牌仅作优先级提示，"
                                     "未知即未知"})


def v1_pool_status():
    """数据总池 × 已入库覆盖 × 下载任务状态（进度用真实计数，不推断）。"""
    pools = pool_index()
    cov = covered()
    have = {(c["exchange"], c["symbol"]) for c in cov}
    with use_db() as con:
        try:
            tasks = [dict(r) for r in con.execute(
                "SELECT timeframe,status,COUNT(*) AS n FROM download_tasks "
                "GROUP BY timeframe,status ORDER BY timeframe")]
        except sqlite3.OperationalError:
            tasks = []
        try:
            runs = [dict(r) for r in con.execute(
                "SELECT id,kind,phase,started_at,finished_at,status,note FROM download_runs "
                "ORDER BY id DESC LIMIT 5")]
        except sqlite3.OperationalError:
            runs = []
    active, index_items, meta = workbench_scope()
    scope = set(active) if active else set(pools)
    return envelope(
        pool_total=len(scope), pool_all_total=len(pools),
        pool_with_candles=len(have & scope),
        pool_without_candles=len(scope - have),
        crypto_index=index_items, active_scope=meta,
        covered_combos=len(cov), candles=sum(c["rows"] for c in cov),
        download_tasks=tasks,
        recent_runs=[{**r, "started_at": iso(r["started_at"]),
                      "finished_at": iso(r["finished_at"])} for r in runs],
        quality={"status": "ok", "age_seconds": None,
                 "note": "数据总池与交易资格分开；资格规则未冻结，故不给可交易清单"})


ANNOT_LOOKBACK_MS = 30 * 24 * 3600 * 1000   # 标注回溯缓冲：A/B 的 frozen_at 早于 confirmed_at


def annotations_for(exchange, mid, tf, window_start, window_end, limit=2000):
    """从**真实已存信号**构建 K 线标注。无信号 → (None, 说明)。不生成推测标注。

    SIG06 修复（2026-10-11）：旧实现先取**最早 500 行**再按图表窗口过滤 ——
    窗口靠后时，前面的旧信号会占满 500 行，导致窗口内的标注被挤掉、且不披露。
    现改为**先按时间窗口过滤**（`confirmed_at` 是必要下界：所有标记时刻 ≤ 确认时刻），
    再加 30 天回溯缓冲覆盖 A/B 的 frozen_at，最后逐标记落窗；截断如实披露。
    """
    lo = int(window_start)
    hi = int(window_end) + ANNOT_LOOKBACK_MS
    with use_db() as con:
        try:
            rows = con.execute(
                "SELECT * FROM signals WHERE exchange=? AND symbol=? AND timeframe=? "
                "AND confirmed_at >= ? AND confirmed_at <= ? "
                "ORDER BY confirmed_at ASC LIMIT ?",
                (exchange, mid, tf, lo, hi, limit + 1)).fetchall()
        except sqlite3.OperationalError:
            return None, "signals 表不存在；annotations 未接入"
    truncated = len(rows) > limit
    rows = rows[:limit]
    if not rows:
        return None, "该窗口内无已存信号；annotations 为空（不生成推测标注）"
    frozen, breakouts, retests, confirms = [], [], [], []
    for r in rows:
        ev = _ev_of(r)
        sid = str(r["id"])
        ft = ev.get("frozen_at")
        if ft is not None and window_start <= ft <= window_end:
            frozen.append({"start_time": None, "end_time": iso(ft),
                           "upper": ev.get("range_upper"), "lower": ev.get("range_lower"),
                           "bar_count": None, "note": "区间根数未随事件留存；不推测",
                           "signal_id": sid})
        bt = ev.get("breakout_close_time")
        if bt is not None and window_start <= bt <= window_end:
            breakouts.append({"time": iso(bt), "price": None, "note": "突破收盘确认",
                              "signal_id": sid})
        rt = ev.get("retest_close_time")
        if rt is not None and window_start <= rt <= window_end:
            retests.append({"time": iso(rt), "price": None, "note": "独立回踩确认",
                            "signal_id": sid})
        ca = r["confirmed_at"]
        if ca is not None and window_start <= ca <= window_end:
            confirms.append({"time": iso(ca), "price": None, "note": "信号确认",
                             "signal_id": sid, "status": r["status"],
                             "strategy_key": r["strategy_key"],
                             "source_masks": ev.get("source_masks"),
                             "execution_label": ev.get("execution_label")})
    if not (frozen or breakouts or retests or confirms):
        return None, "该窗口内无标注"
    note = (f"候选信号超过 {limit} 条，已按 confirmed_at 升序截断（如实披露）"
            if truncated else None)
    return ({"frozen_ranges": frozen, "breakouts": breakouts, "retests": retests,
             "stops": [], "targets": [], "signals": confirms}, note)


# ══════════════ 选中合约的「有限近期原生尾段」（按需、经统一网关）══════════════
# 目的：用户点开任一周期时**不必等全池多年下载完成**，即可看到该合约最近的
#       原生尾段 + 当前未收盘 bar。
# 纪律：
#   · 一律经 `gateway_ext.gateway`（共享缓存 / 租约 / 预算 / 429 冷却 / 403 封禁）；
#     **只有已收盘 bar 入库**，未收盘只作为 `forming_bar` 临时返回，绝不进历史。
#   · **优先读实时采集写入的临时层 `realtime_klines`**（WS 原生 K 线流，最真实、零额外
#     请求）；仅当临时层无该 (标的,周期) 或已陈旧时，才回退到 REST 尾段。
#     这样既有实时未收盘 bar，又不会把 REST 预算打满。
#   · 夹具/离线测试用 `MARKET_TAIL_FETCH=0` 关闭，不触发真实外部采集。
TAIL_TIMEFRAMES = ("1d", "12h", "8h", "4h", "1h", "30m", "15m", "5m")
TAIL_LIMIT = {"binance": 1000, "okx": 300}
TAIL_MIN_INTERVAL_MS = 60_000                 # 入库尝试节流（拉取仍走网关缓存）
TAIL_FETCH_ENABLED = os.environ.get("MARKET_TAIL_FETCH", "1") != "0"
_OKX_BAR = {"5m": "5m", "15m": "15m", "30m": "30m",
            "1h": "1H", "4h": "4H", "12h": "12Hutc", "1d": "1Dutc"}
# 2026-10-07 修正（12h UTC 边界一致性）：12h 原用 "12H"（OKX 的 UTC+8 开盘 kline，
# 锚点 04:00/16:00 UTC），与币安 12h / 历史补齐 "12Hutc"（UTC 00:00/12:00）边界不一致，
# 且 `data_hub/gateway.py` 的 `okx.candles` 白名单只接受 {"1m","5m","15m","30m","1H",
# "4H","12Hutc","1Dutc"} —— 传 "12H" 会被 RejectedRequest 拒绝，导致 OKX 12h 尾段
# 补缺**静默失败**。改用官方 UTC+0 口径 "12Hutc"，与币安/历史补齐同一边界。**其他周期不变**。
# 实测（2026-10-06）：欧易 K 线频道/接口**没有 8H**（订阅 candle8H 返回 code 60018）。
# 因此 `_OKX_BAR` 不含 8h；`v1_candles` 对欧易 8h 走 `NO_NATIVE` 分支如实说明，不合成。
# 实时临时层的"陈旧"判据：超过该周期若干倍仍未收到新消息即视为陈旧，回退 REST 尾段。
_RT_KLINE_STALE_MS = {"5m": 300_000, "15m": 600_000, "30m": 900_000,
                      "1h": 1_800_000, "4h": 3_600_000, "8h": 7_200_000,
                      "12h": 10_800_000, "1d": 21_600_000}
_TAIL_LOCK = threading.Lock()
_TAIL_LAST = {}
_TAIL_GW = None


def _tail_gateway():
    global _TAIL_GW
    if _TAIL_GW is None:
        import gateway_ext
        _TAIL_GW = gateway_ext.gateway(STORE)
    return _TAIL_GW


def _gw_get_retry(gw, name, params, attempts=2):
    """尾段拉取的**短重试**：预算推迟（DeferredRequest）或瞬时抖动时等一小会儿再试。

    仍**不绕过**网关的任何拒绝：重试失败就如实报错，由前端下一轮刷新自然重试。
    """
    last = None
    for i in range(attempts):
        try:
            return gw.get(name, params)
        except Exception as exc:                                   # noqa: BLE001
            last = exc
            if i == attempts - 1:
                break
            ra = getattr(exc, "retry_at", None)
            wait = min(2.5, max(0.3, (ra - _now_ms()) / 1000.0)) if ra else 1.0
            time.sleep(wait)
    raise last


def _tail_fetch(exchange, symbol, tf):
    """经统一网关拉取**有限近期原生尾段**；返回 (bars, source_url)。"""
    gw = _tail_gateway()
    fetched_at = _now_ms()
    if exchange == "binance":
        r = _gw_get_retry(gw, "binance.candles",
                          {"symbol": symbol, "interval": tf, "limit": TAIL_LIMIT["binance"]})
        out = [{"open_time": int(k[0]), "close_time": int(k[6]),
                "open": k[1], "high": k[2], "low": k[3], "close": k[4],
                "volume": k[5], "quote_volume": k[7], "trade_count": int(k[8]),
                "taker_buy_volume": k[9], "taker_buy_quote_volume": k[10],
                "is_closed": int(k[6]) < fetched_at, "available_at": fetched_at}
               for k in (r["数据"] or [])]
        src = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={tf}"
    else:
        r = _gw_get_retry(gw, "okx.candles_recent",
                          {"instId": symbol, "bar": _OKX_BAR.get(tf, tf),
                           "limit": TAIL_LIMIT["okx"]})
        iv = INTERVAL_MS[tf]
        out = [{"open_time": int(c[0]), "close_time": int(c[0]) + iv - 1,
                "open": c[1], "high": c[2], "low": c[3], "close": c[4],
                # 欧易无 taker / trade_count 字段：占位并另行标注，不冒充真值
                "volume": c[6], "quote_volume": c[7], "trade_count": 0,
                "taker_buy_volume": "", "taker_buy_quote_volume": "",
                "is_closed": c[8] == "1", "available_at": fetched_at}
               for c in (r["数据"].get("data") or [])]
        src = "https://www.okx.com/api/v5/market/candles"
    out.sort(key=lambda b: b["open_time"])
    return out, src


def _tail_store(exchange, symbol, tf, bars, src_url):
    """把**已收盘**尾段入库（幂等：同内容 sha256 只入一次）。返回新增根数。"""
    closed = sorted((b for b in bars if b["is_closed"]), key=lambda b: b["open_time"])
    if not closed:
        return 0
    # 节流键必须含**周期**：否则同一标的先请求 1d 再请求 1m 时，第二次会被误判为
    # 「同一标的刚写过」而整批丢弃（多周期尾段只有第一个周期能入库）。
    key = (exchange, symbol, tf)
    with _TAIL_LOCK:
        now = _now_ms()
        if now - _TAIL_LAST.get(key, 0) < TAIL_MIN_INTERVAL_MS:
            return 0
        _TAIL_LAST[key] = now
    market = "um" if exchange == "binance" else "swap"
    with use_db() as con:
        # REST 尾段只用于补缺；先剔除已有 candle_heads，避免新 REST source 抢走更新的
        # WS/归档 head，也避免重复造 inactive candle_versions。
        have = {r[0] for r in con.execute(
            "SELECT open_time FROM candle_heads WHERE exchange=? AND market=? AND symbol=? "
            "AND timeframe=? AND open_time BETWEEN ? AND ?",
            (exchange, market, symbol, tf, closed[0]["open_time"], closed[-1]["open_time"]))}
        todo = [b for b in closed if b["open_time"] not in have]
        if not todo:
            return 0
        text = "\n".join("|".join(str(b[k]) for k in
                                  ("open_time", "open", "high", "low", "close",
                                   "volume", "quote_volume")) for b in todo)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        old = con.execute("SELECT id FROM sources WHERE exchange=? AND market=? AND symbol=? "
                          "AND timeframe=? AND sha256=?",
                          (exchange, market, symbol, tf, digest)).fetchone()
        if old:
            return 0
        cur = con.execute(
            "INSERT INTO sources(exchange,market,symbol,timeframe,source_url,file_path,sha256,"
            "imported_at,downloaded_at,first_open,last_close,row_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (exchange, market, symbol, tf, src_url, f"{exchange}:rest:tail",
             digest, now, now, todo[0]["open_time"], todo[-1]["close_time"], len(todo)))
        sid = cur.lastrowid
        added = 0
        for b in todo:
            inserted = con.execute(
                "INSERT OR IGNORE INTO candle_versions(exchange,market,symbol,timeframe,open_time,"
                "close_time,open,high,low,close,volume,quote_volume,trade_count,taker_buy_volume,"
                "taker_buy_quote_volume,source_id,available_at,is_closed) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (exchange, market, symbol, tf, b["open_time"], b["close_time"],
                 b["open"], b["high"], b["low"], b["close"], b["volume"], b["quote_volume"],
                 b["trade_count"], b["taker_buy_volume"], b["taker_buy_quote_volume"], sid,
                 b.get("available_at")))
            if not inserted.rowcount:
                continue
            # 最近尾段 REST 不能因同 open_time 重新指向新来源：
            # 已有的实时/归档 head 保持原值与原 source（尤其 available_at 语义不能被抹掉）。
            head = con.execute("INSERT INTO candle_heads(exchange,market,symbol,timeframe,open_time,source_id)"
                               " VALUES(?,?,?,?,?,?) ON CONFLICT(exchange,market,symbol,timeframe,open_time)"
                               " DO NOTHING", (exchange, market, symbol, tf, b["open_time"], sid))
            if not head.rowcount:
                con.execute("DELETE FROM candle_versions WHERE exchange=? AND market=? AND symbol=? "
                            "AND timeframe=? AND open_time=? AND source_id=?",
                            (exchange, market, symbol, tf, b["open_time"], sid))
                continue
            added += 1
        if added:
            cov = con.execute("SELECT rows,first_open,last_close FROM coverage_cache WHERE "
                              "exchange=? AND market=? AND symbol=? AND timeframe=?",
                              (exchange, market, symbol, tf)).fetchone()
            if cov:
                first = min(int(cov["first_open"]) if cov["first_open"] is not None
                            else todo[0]["open_time"], min(b["open_time"] for b in todo))
                last = max(int(cov["last_close"]) if cov["last_close"] is not None
                           else todo[-1]["close_time"], max(b["close_time"] for b in todo))
                rows = int(cov["rows"] or 0) + added
                expected = max(0, (last + 1 - first) // INTERVAL_MS[tf])
                con.execute("UPDATE coverage_cache SET rows=?,first_open=?,last_close=?,expected=?,"
                            "missing=?,source_count=source_count+?,updated_at=? WHERE exchange=? "
                            "AND market=? AND symbol=? AND timeframe=?",
                            (rows, first, last, expected, max(0, expected - rows),
                             int(cur.rowcount), now, exchange, market, symbol, tf))
        if exchange == "okx":
            for f, stored, reason, impact in (
                    ("taker_buy_volume", "", "欧易 K 线响应无该字段（币安有）",
                     "B 策略数据不足"),
                    ("trade_count", "0", "欧易 K 线响应无 trade_count 字段；本列 NOT NULL，"
                                          "写 0 为占位符，非真值", "成交笔数类指标不可用")):
                con.execute("INSERT OR REPLACE INTO candle_field_notes(exchange,market,symbol,"
                            "timeframe,field,stored_as,reason,impact,source,updated_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (exchange, market, symbol, tf, f, stored, reason, impact,
                             "okx.v5/market-candles", now))
        con.commit()
    return added


def _tail_bars(exchange, symbol, tf):
    """编排：拉尾段 → 已收盘入库 → 返回 (新增根数, forming_bar|None, 说明)。"""
    try:
        bars, src = _tail_fetch(exchange, symbol, tf)
    except Exception as exc:                                       # noqa: BLE001
        return 0, None, f"近期尾段拉取未成功（{type(exc).__name__}）：不影响已入库历史"
    added = 0
    try:
        added = _tail_store(exchange, symbol, tf, bars, src)
    except Exception as exc:                                       # noqa: BLE001
        return 0, _forming_of(bars), f"尾段已收盘部分入库未成功（{type(exc).__name__}）"
    return added, _forming_of(bars), None


def _forming_of(bars):
    """未收盘 bar（可空）：单独返回，**不入已收盘历史**。"""
    for b in reversed(bars):
        if not b["is_closed"]:
            return {"open_time": iso(b["open_time"]), "close_time": iso(b["close_time"]),
                    "open": num(b["open"]), "high": num(b["high"]), "low": num(b["low"]),
                    "close": num(b["close"]), "base_volume": num(b["volume"]),
                    "contract_volume": None, "quote_volume": num(b["quote_volume"]),
                    "trades": None, "is_closed": False,
                    "note": "未收盘临时层：不入已收盘历史、不作回测输入"}
    return None


def _forming_from_temp(exchange, market, symbol, tf):
    """从**实时采集写入的临时层** `realtime_klines` 读当前未收盘 bar。

    这是首选来源：由统一采集进程的原生 WS K 线流写入，**零额外外部请求**，
    且带真实来源/传输/事件/接收时点。返回 (forming_bar|None, meta|None)。
    """
    with use_db() as con:
        try:
            r = con.execute(
                "SELECT open_time,close_time,open,high,low,close,volume,quote_volume,"
                "trade_count,event_time,received_at,source,transport FROM realtime_klines "
                "WHERE exchange=? AND market=? AND symbol=? AND timeframe=?",
                (exchange, market, symbol, tf)).fetchone()
        except sqlite3.OperationalError:
            return None, None          # 迁移未执行 → 如实退化为"无临时层"
    if not r:
        return None, None
    age_s = ((_now_ms() - r["received_at"]) / 1000.0) if r["received_at"] else None
    stale = age_s is None or age_s * 1000 > _RT_KLINE_STALE_MS.get(tf, 300_000)
    src = f"{r['source']}/{r['transport']}" if r["transport"] else r["source"]
    bar = {"open_time": iso(r["open_time"]), "close_time": iso(r["close_time"]),
           "open": num(r["open"]), "high": num(r["high"]), "low": num(r["low"]),
           "close": num(r["close"]), "base_volume": num(r["volume"]),
           "contract_volume": None, "quote_volume": num(r["quote_volume"]),
           "trades": r["trade_count"], "is_closed": False,
           "event_time": iso(r["event_time"]), "received_at": iso(r["received_at"]),
           "source": src, "age_seconds": round(age_s, 1) if age_s is not None else None,
           "note": "未收盘临时层（realtime_klines）：不入已收盘历史、不作回测输入"}
    meta = {"source": src, "transport": r["transport"],
            "event_time": iso(r["event_time"]), "received_at": iso(r["received_at"]),
            "age_seconds": bar["age_seconds"], "stale": stale,
            "origin": "realtime_klines（统一采集进程原生 WS K 线流）"}
    return bar, meta


def _utc_boundary_filter(rows, exchange, tf):
    """读侧隔离：OKX 12h 的**旧混边界行**（UTC+8 锚点 04:00/16:00Z）不进 UTC 序列。

    用户约束（2026-10-07）：**保留原行、不删历史**，但 UTC 读侧不得把非 UTC 00:00/12:00
    对齐的旧行与对齐行并列，冒充同一 UTC 12h 序列。仅当 `exchange=="okx" and tf=="12h"`
    时生效；其他交易所/周期原样返回（**不改其他周期语义**）。

    返回 `(kept_rows, isolated_n)`：`kept_rows` 为 UTC 对齐行（保持原顺序），
    `isolated_n` 为被隔离的非对齐旧行根数（如实上报，库内原行不动）。
    """
    if not (exchange == "okx" and tf == "12h"):
        return list(rows), 0
    iv = INTERVAL_MS[tf]
    kept, isolated = [], 0
    for r in rows:
        if int(r["open_time"]) % iv:
            isolated += 1
        else:
            kept.append(r)
    return kept, isolated


def v1_candles(exchange, mid, params):
    if mid.startswith(exchange + ":"):
        mid = mid[len(exchange) + 1:]
    tf = (params.get("timeframe") or ["1d"])[0]
    history_only = str((params.get("history_only") or ["0"])[0]).lower() in ("1", "true", "yes")
    no_rest_tail = str((params.get("no_rest_tail") or ["0"])[0]).lower() in ("1", "true", "yes")
    limit = int((params.get("limit") or ["500"])[0])
    # 历史分页每页最多1999根，额外读1根仅用于准确判断 has_more；Store 的硬上限为2000。
    limit = min(max(limit, 1), 1999 if history_only else 2000)
    before_raw = (params.get("before") or [None])[0] if history_only else None
    before = int(before_raw) if before_raw not in (None, "") else None
    if tf not in INTERVAL_MS:
        raise ValueError("未配置的原生周期")
    is_native = tf not in NO_NATIVE.get(exchange, set())
    market = (next((c["market"] for c in covered()
                    if c["exchange"] == exchange and c["symbol"] == mid), None)
              or ("um" if exchange == "binance" else "swap"))
    if exchange == "okx":
        # 当前K线工作台仅允许OKX提供币安缺失的补充集合；币安已有合约不因历史/尾段缺口转查OKX。
        active, _indices, _meta = workbench_scope()
        if ("okx", mid) not in active:
            note = "OKX仅接入币安缺失的补充标的；此合约不在补充集合中，不从OKX取K线"
            if not is_native:
                note += f"；OKX无{tf}原生周期：不合成、不跨所拼接"
            return envelope(
                exchange=exchange, market_id=mid, timeframe=tf, timezone="UTC", bars=[],
                forming_bar=None, realtime=None, has_more=False, next_cursor=None,
                is_native=is_native, annotations=None, source_url=None,
                time={"event_time": None, "available_at": None, "available_at_known": False,
                      "received_at": None, "fetched_at": None},
                evidence_level="不在当前OKX补充K线范围", missing_reason="不属于币安缺失补充集合",
                quality={"status": "out_of_scope", "age_seconds": None, "note": note})
    # ── 未收盘 bar：**优先读实时临时层**（统一采集进程的原生 WS K 线流）──────────
    # 目的：不必等全池多年下载完成，选中合约即可看到各周期当前未收盘 bar；
    # 临时层无数据或已陈旧时，才经统一网关回退到 REST 有限尾段（已收盘才入库）。
    # 未收盘 bar 只作为 forming_bar 临时层返回，**不入已收盘历史**。
    forming_bar, tail_added, tail_note, rt_meta = None, 0, None, None
    # history_only 是严格本地库存读取：不读临时层、不触发 REST 尾段，也不声称当前实时状态。
    if not history_only and is_native and exchange in ("binance", "okx"):
        forming_bar, rt_meta = _forming_from_temp(exchange, market, mid, tf)
        if TAIL_FETCH_ENABLED and not no_rest_tail and (rt_meta is None or rt_meta.get("stale")):
            tail_added, rest_forming, tail_note = _tail_bars(exchange, mid, tf)
            if rest_forming:
                rest_forming["source"] = "rest_tail"
                forming_bar = rest_forming
                rt_meta = {"source": "rest_tail（统一网关近期尾段）", "transport": "rest",
                           "event_time": None, "received_at": None, "age_seconds": 0.0,
                           "stale": False,
                           "origin": "REST 有限尾段回退（临时层无数据或已陈旧）"}
    # ── 读侧隔离：OKX 12h 的**旧混边界行**（UTC+8 锚点）不得并入 UTC 序列 ─────────
    # 背景：2026-10-07 之前 OKX 12h 采集用 `candle12H`（UTC+8 开盘，锚点 04:00/16:00 UTC），
    # 库内遗留非 UTC 00:00/12:00 对齐的 12h 行。按用户约束「**保留原行、不删历史**」，
    # 仅在本读侧把非对齐行**隔离出本次返回**，不与 UTC 对齐行并列冒充同一 UTC 12h 序列；
    # 隔离根数如实写进 note（残存事实可见），库内原始行原样保留。
    isolate_off = (exchange == "okx" and tf == "12h")
    fetch_n = min(limit + (1 if history_only else 0) + (256 if isolate_off else 0), 2000)
    raw_rows = STORE.candles(exchange, market, mid, tf, 0, before, fetch_n)
    has_more = history_only and len(raw_rows) > limit
    # 游标在原始库行上定位，过滤12h旧边界或成交样本后仍不会跨页重复/跳过。
    # 普通实时详情仍沿用既有「过滤后最多 limit 根」口径。
    page_rows = raw_rows[-limit:] if history_only and has_more else raw_rows
    next_cursor = str(page_rows[0]["open_time"]) if has_more and page_rows else None
    rows, off_isolated = _utc_boundary_filter(page_rows, exchange, tf)
    if not history_only:
        rows = rows[-limit:]
    # ── 读侧语义隔离（只打标签/计数，不改库、不删行）─────────────────────────────
    # ① 官方归档**零量占位行**（volume==0 且 OHLC 四值相同）：是归档证据，
    #    **不是有成交趋势样本** → `tradeable=False`。
    # ② **多代合约**（同一符号旧合约退市→新合约上市）：按官方生命周期分段，
    #    打 `generation` 标签，**不拼成一条连续可交易历史**。
    # 仅 12h 生效（本任务边界）；其他周期 `annotate_rows` 原样返回。
    # 默认**保留**占位行（作为归档证据，附加标签），加 `tradeable_only=1` 只取成交样本。
    life_anns, life_meta = lifecycle.annotate_rows(exchange, mid, tf, rows)
    tradeable_only = str((params.get("tradeable_only") or ["0"])[0]).lower() in ("1", "true", "yes")
    bars = []
    raw_open, raw_close, raw_available = [], [], []
    for r, ann in zip(rows, life_anns):
        if tradeable_only and not ann["tradeable"]:
            continue
        raw_open.append(r["open_time"])
        raw_close.append(r["close_time"])
        raw_available.append(r["available_at"])
        bars.append({"open_time": iso(r["open_time"]), "close_time": iso(r["close_time"]),
                     "open": num(r["open"]), "high": num(r["high"]), "low": num(r["low"]),
                     "close": num(r["close"]), "base_volume": num(r["volume"]),
                     "contract_volume": None, "quote_volume": num(r["quote_volume"]),
                     "trades": r["trade_count"], "is_closed": bool(r["is_closed"]),
                     # 读侧语义标签：归档占位 / 成交样本；代际分段（未注册为 null）。
                     "row_kind": ann["row_kind"],
                     "generation": ann["generation"],
                     "tradeable": ann["tradeable"],
                     # NULL 表示历史归档没有「当时可得」证据；REST 尾段/实时闭合 bar
                     # 按实际接收时间入库，明确区分，不把当前查询时点冒充接收时点。
                     "available_at": iso(r["available_at"]) if r["available_at"] is not None else None,
                     "available_at_known": r["available_at"] is not None})
    ok = bool(bars)
    ann, ann_note = (None, "无已入库K线，不生成标注")
    if ok:
        ann, ann_note = annotations_for(exchange, mid, tf, min(raw_open), max(raw_close))
    note = None if ok else "该标的/周期无已入库K线；接口不自动下载、不生成替代数据"
    if off_isolated:
        note = ((note + "；" if note else "")
                + f"读侧已隔离 {off_isolated} 根非UTC边界旧行（OKX 12h 旧 candle12H 混边界；"
                  "**库内原行保留、不删除**），本序列只含 UTC 00:00/12:00 对齐行")
    # 读侧语义隔离说明（仅 12h 有注册时出现）：占位根数与代际分段如实上报。
    if life_meta["archive_padding_rows"] or life_meta["cross_generation"]:
        bits = []
        if life_meta["archive_padding_rows"]:
            bits.append(f"含官方归档零量占位 {life_meta['archive_padding_rows']} 根"
                        "（`row_kind=archive_padding`、`tradeable=false`，非有成交趋势样本；"
                        "库内保留为归档证据）")
        if life_meta["cross_generation"]:
            bits.append("本序列跨代际（" + "/".join(life_meta["generations"])
                        + "）：按官方生命周期分段，**不拼成连续可交易历史**")
        if tradeable_only:
            bits.append("已按 `tradeable_only=1` 只返回成交样本")
        note = (note + "；" if note else "") + "；".join(bits)
    if ann_note:
        note = (note + "；" if note else "") + f"annotations：{ann_note}"
    if exchange in NO_NATIVE and tf in NO_NATIVE[exchange]:
        note = ((note + "；" if note else "")
                + f"{exchange} 无 {tf} 原生周期：如实置空，不合成、不跨所拼接")
    if forming_bar is not None:
        tail_msg = (f"未收盘临时层：来源 {rt_meta.get('source') if rt_meta else '未知'}"
                    f"（{rt_meta.get('origin') if rt_meta else ''}）")
        if rt_meta and rt_meta.get("age_seconds") is not None:
            tail_msg += f"，新鲜度 {rt_meta['age_seconds']}s"
        if rt_meta and rt_meta.get("stale"):
            tail_msg += "；**该临时层已陈旧**"
        if tail_added:
            tail_msg += f"；本次另补已收盘 {tail_added} 根"
        if tail_note:
            tail_msg += f"；{tail_note}"
        note = (note + "；" if note else "") + tail_msg
    # 来源与时间语义：source_url = 最近入库来源；fetched_at = 来源记录入库时点。
    # `available_at` 只在原始导入/REST尾段/实时闭合时确有接收记录时返回；历史归档保持 NULL，
    # 不冒充「当时可得/当时收到」。forming_bar 的 event_time/received_at 单独来自临时层。
    src = source_of(exchange, market, mid, tf) if market is not None else None
    return envelope(
        exchange=exchange, market_id=mid, timeframe=tf, timezone="UTC", bars=bars,
        # 读侧语义隔离元数据（占位根数 / 代际分段 / 是否跨代）；未注册标的全为 0/空。
        lifecycle=life_meta,
        # 未收盘 bar 单独字段（可空）；bars 恒为**已收盘**
        forming_bar=forming_bar,
        # 实时未收盘的最小必要新鲜度字段（来源/传输/事件/接收/年龄/是否陈旧）。
        # 只描述**真实来源**，不因进程存在就断言 ok。
        realtime=rt_meta,
        has_more=bool(has_more), next_cursor=next_cursor, is_native=is_native, annotations=ann,
        source_url=(src or {}).get("source_url"),
        time={"event_time": iso(max(raw_close)) if raw_close else None,
              "available_at": (iso(raw_available[-1]) if raw_available and raw_available[-1] is not None
                               else None),
              "available_at_known": bool(raw_available and raw_available[-1] is not None),
              "received_at": None, "fetched_at": (src or {}).get("imported_at")},
        evidence_level="样例已实取" if ok else "缺失",
        missing_reason=None if ok else "无已入库K线",
        quality={"status": "ok" if ok else "missing", "age_seconds": None, "note": note})


HISTORY_LIMIT_DEFAULT = 2000
HISTORY_LIMIT_MAX = 20000

# `period=auto` 的「自适应金字塔」分桶：**近期细、远期粗**。
# 6 年 5m 归档约 64 万行/标的，若一律 5m 直读，2000 条只够 6.9 天；
# 若一律取粗桶，近端又会被抹平。因此按「距现在的年龄」选桶宽。
# 每一档只做**抽样选择**（取该桶内时间最晚的一条真实观测）：不插值、不改数值、不改时间戳。
OI_SPREAD_STEPS = (
    (6 * 24 * 3600 * 1000, 5 * 60 * 1000),        # 近 6 天    → 5m
    (30 * 24 * 3600 * 1000, 60 * 60 * 1000),      # 6–30 天    → 1h
    (365 * 24 * 3600 * 1000, 24 * 3600 * 1000),   # 30–365 天  → 1d
    (None, 7 * 24 * 3600 * 1000),                 # 更早       → 1w
)
# 为什么近端 5m 档取 **6 天**（2026-10-11 由 2 天放宽）：
#   展开图的 5m 首屏 = 本地库存一页 1500 根 + 实时尾段 180 根 ≈ **5.2 天**。
#   旧档位只保近 2 天 5m，于是「首屏左侧 3 天被降成 1 小时桶、右侧 2 天是 5 分钟桶」，
#   同一张柱形图里出现 4 倍的密度突变 —— 实测 10-06/07/08 每天只有 24 根柱，
#   而 10-10/11 每天 75~81 根。放宽到 6 天后首屏整段同为 5m 档，密度一致。
#   总点数上界 ≈ 6×288 + 24×24 + 335 + 287 ≈ 2,950（前端 OI_HISTORY_LIMIT = 4000）。


def _qint(params, name, default=None, lo=None, hi=None):
    """从 query 取整数参数；非法值**静默回落默认**（只读接口不因坏参数报错）。"""
    raw = (params.get(name) or [None])[0] if params is not None else None
    if raw is None or raw == "":
        return default
    try:
        val = int(str(raw))
    except (TypeError, ValueError):
        return default
    if lo is not None:
        val = max(lo, val)
    if hi is not None:
        val = min(hi, val)
    return val


def _history(table, exchange, mid, ts_col, extra_cols, params=None, period_col=None,
             default_period=None, want=None, bucket_ms=None, limit=None,
             from_ms=None, to_ms=None):
    """历史序列（**只读**）。

    `oi_history` 自 2026-10-08 起同时存放**两种采样周期**：
      · `period='snapshot'` —— 实时快照（既有，由采集器周期写入）；
      · `period='5m'`       —— 官方日度 metrics 归档历史（原生 5m，本机离线导入）。
    因此必须**按周期隔离读取**，否则多周期混读会把「最新/近端」读数顶掉：
      · 未指定 `?period=` 时**默认只读 `snapshot`**（与修复前逐字一致，零回归）；
      · 指定 `?period=5m` 时按该周期取**最近** N 条（再按时间升序返回）。
    不带 `period_col` 的表（如 `funding_history`）行为完全不变。

    **加性**查询参数（全部可选；缺省时与旧行为逐字一致）：
      · `limit`       —— 返回条数上限（默认 2000，硬上限 20000）；
      · `from` / `to` —— 时间闭区间（毫秒）；
      · `bucket_ms`   —— >0 时按该毫秒数分桶，每桶只取**桶内时间最晚的一条真实观测**
                         （抽样选择，不插值、不改数值、不改时间戳）。
    """
    if mid.startswith(exchange + ":"):
        mid = mid[len(exchange) + 1:]
    lim = limit if limit is not None else _qint(params, "limit", HISTORY_LIMIT_DEFAULT,
                                                1, HISTORY_LIMIT_MAX)
    frm = from_ms if from_ms is not None else _qint(params, "from", None, 0, None)
    to = to_ms if to_ms is not None else _qint(params, "to", None, 0, None)
    bms = bucket_ms if bucket_ms is not None else _qint(params, "bucket_ms", None, 1000, None)

    where, args = "exchange=? AND symbol=?", [exchange, mid]
    want_period = None
    if period_col:
        if want is not None:
            want_period = want
        elif params is not None:
            want_period = (params.get("period") or [None])[0] or None
        where += f" AND {period_col}=?"
        args.append(want_period or default_period)
    if frm is not None:
        where += f" AND {ts_col}>=?"
        args.append(frm)
    if to is not None:
        where += f" AND {ts_col}<=?"
        args.append(to)

    descending = bool(want_period) or bool(bms)
    if bms:
        # 分桶：`MAX(ts)` 是**唯一**聚合 ⇒ 命中 SQLite 的 bare-column 规则，
        # 其余列取「该最大值所在行」的值 ⇒ 每桶返回一条**真实观测**（非聚合均值）。
        sql = (f"SELECT *, MAX({ts_col}) AS _bucket_ts FROM {table} WHERE {where} "
               f"GROUP BY {ts_col}/? ORDER BY {ts_col} DESC LIMIT ?")
        sel_args = args + [bms, lim]
    else:
        sql = (f"SELECT * FROM {table} WHERE {where} "
               f"ORDER BY {ts_col} {'DESC' if descending else 'ASC'} LIMIT ?")
        sel_args = args + [lim]
    with use_db() as con:
        try:
            rows = [dict(r) for r in con.execute(sql, sel_args).fetchall()]
        except sqlite3.OperationalError:
            return missing(f"{table} 表不存在；请先执行 migrate.py", "not_connected")
    if descending:
        rows = rows[::-1]              # 取最近 N 条后恢复时间升序
    # ── 冷层补齐（P3 冷热迁移）────────────────────────────────────────────────
    # 热层只保留近 30 天；全历史在冷档 Parquet。仅当**接入仓储**时启用；
    # 未接入（未设 MARKET_ARCHIVE）⇒ 此段整体跳过，行为与迁移前逐字一致。
    if _REPO is not None:
        want_n = (max(lim * 64, 20000) if bms else max(lim * 4, 20000))
        cold = _cold_flat_rows(table, exchange, mid, ts_col,
                               (want_period or default_period) if period_col else None,
                               frm, to, want_n)
        if cold:
            if bms:   # 与热层 SQL 的 bare-column 口径等价：每桶取桶内 ts 最大的一条真实观测
                best = {}
                for r in cold:
                    k = r[ts_col] // bms
                    if k not in best or r[ts_col] > best[k][ts_col]:
                        best[k] = r
                cold = sorted(best.values(), key=lambda r: r[ts_col])
            merged = {}
            for r in rows + cold:
                merged[r[ts_col]] = r      # 同 ts 以热层为准（热层更贴近实时）
            rows = sorted(merged.values(), key=lambda r: r[ts_col])
            rows = rows[-lim:] if descending else rows[:lim]
    if not rows:
        return missing(f"{table} 无已入库数据；本接口只读，不自动下载")
    items = []
    for r in rows:
        it = {"event_time": iso(r[ts_col])}
        for c in extra_cols:
            it[c] = r[c] if c in r.keys() else None
        items.append(it)
    return envelope(items=items, total=len(items),
                    quality={"status": "ok", "age_seconds": None, "note": None})


def _oi_spread_expr(params):
    """`period=auto` 的 5m 分桶表达式 → `(group_expr, extra_args)`。

    · 显式给了 `bucket_ms` ⇒ 用固定桶宽（调用方自己决定粒度）；
    · 否则（`spread` 默认 1）⇒ 按 `OI_SPREAD_STEPS` 的年龄分档自适应金字塔；
    · `spread=0` ⇒ 不分桶，直接取最近 N 条原生 5m。
    """
    bms = _qint(params, "bucket_ms", None, 1000, None)
    if bms:
        return f"ts/{bms}", []
    if not _qint(params, "spread", 1, 0, 1):
        return None, []
    now = now_ms()
    parts, args = [], []
    for age, ms in OI_SPREAD_STEPS[:-1]:
        parts.append(f"WHEN ts>=? THEN ts/{ms}")
        args.append(now - age)
    return ("CASE " + " ".join(parts) + f" ELSE ts/{OI_SPREAD_STEPS[-1][1]} END"), args


OI_COLS = ["period", "open_interest", "open_interest_value", "oi_ccy", "oi_usd",
           "source", "fetched_at"]


def _pyramid_size(ts, now):
    """OI 自适应金字塔：按年龄返回桶宽（毫秒）。与 `OI_SPREAD_STEPS` 逐档一致。"""
    for age, ms in OI_SPREAD_STEPS[:-1]:
        if ts >= now - age:
            return ms
    return OI_SPREAD_STEPS[-1][1]


def _oi_envelope(rows, note):
    """把 OI 行（dict，升序）打包成既有 envelope（口径与热层路径逐字一致）。"""
    if not rows:
        return missing("oi_history 无已入库数据；本接口只读，不自动下载")
    items = []
    for r in rows:
        it = {"event_time": iso(r["ts"])}
        for c in OI_COLS:
            it[c] = r[c] if c in r.keys() else None
        items.append(it)
    return envelope(items=items, total=len(items),
                    quality={"status": "ok", "age_seconds": None, "note": note})


def _oi_auto_cold(exchange, mid, params):
    """`period=auto`（**冷热仓储版**）：冷档 5m 全历史 + 热层 5m/snapshot 近端。

    与热层 SQL 版语义一致：5m 归档按 `OI_SPREAD_STEPS` 年龄分档分桶，每桶取桶内
    `ts` 最大的一条**真实观测**（不插值、不改数值）；snapshot 段只取归档末点之后
    （两段零重叠）；近端 snapshot 优先占满 limit。
    """
    lim = _qint(params, "limit", HISTORY_LIMIT_DEFAULT, 1, HISTORY_LIMIT_MAX)
    frm = _qint(params, "from", None, 0, None)
    to = _qint(params, "to", None, 0, None)
    spread = _qint(params, "spread", 1, 0, 1)
    bms = _qint(params, "bucket_ms", None, 1000, None)
    market = _market_for(exchange, mid)
    cold5 = _REPO.oi(exchange, market, mid, period="5m", start=frm, end=to,
                     limit=300000, prefer="cold")
    where5, a5 = "exchange=? AND symbol=? AND period='5m'", [exchange, mid]
    if frm is not None:
        where5 += " AND ts>=?"; a5.append(frm)
    if to is not None:
        where5 += " AND ts<=?"; a5.append(to)
    wheres, as_ = "exchange=? AND symbol=? AND period='snapshot'", [exchange, mid]
    if frm is not None:
        wheres += " AND ts>=?"; as_.append(frm)
    if to is not None:
        wheres += " AND ts<=?"; as_.append(to)
    with use_db() as con:
        try:
            hot5 = [dict(r) for r in con.execute(
                f"SELECT * FROM oi_history WHERE {where5}", a5).fetchall()]
        except sqlite3.OperationalError:
            return missing("oi_history 表不存在；请先执行 migrate.py", "not_connected")
        all5 = {}
        for r in hot5 + cold5:
            all5[r["ts"]] = r
        last5 = max(all5) if all5 else None
        if last5 is not None:
            rows_s = [dict(r) for r in con.execute(
                f"SELECT * FROM oi_history WHERE {wheres} AND ts>? ORDER BY ts DESC LIMIT ?",
                as_ + [last5, lim]).fetchall()]
        else:
            rows_s = [dict(r) for r in con.execute(
                f"SELECT * FROM oi_history WHERE {wheres} ORDER BY ts DESC LIMIT ?",
                as_ + [lim]).fetchall()]
    budget5 = max(1, lim - len(rows_s))
    now = now_ms()
    arch = list(all5.values())
    if bms:
        bucket_of = lambda t: t // bms                     # noqa: E731
    elif not spread:
        bucket_of = None
    else:
        bucket_of = lambda t: t // _pyramid_size(t, now)   # noqa: E731
    if bucket_of is None:
        rows5 = sorted(arch, key=lambda r: r["ts"], reverse=True)[:budget5]
    else:
        best = {}
        for r in arch:
            k = bucket_of(r["ts"])
            if k not in best or r["ts"] > best[k]["ts"]:
                best[k] = r
        rows5 = sorted(best.values(), key=lambda r: r["ts"], reverse=True)[:budget5]
    rows = list(rows5[::-1]) + list(rows_s[::-1])
    note = ("period=auto（冷热仓储）：5m 归档取自冷档 Parquet 全历史 + 热层近端，"
            "按 OI_SPREAD_STEPS 自适应金字塔分桶（每桶取桶内最晚真实观测）；"
            "归档末点之后的 snapshot 近端优先占满 limit，两段零重叠，每行 period 自证口径")
    return _oi_envelope(rows, note)


def _oi_auto(exchange, mid, params):
    """`period=auto`：**5m 归档（自适应金字塔） + 该 5m 末点之后的 snapshot 近端**。

    为什么需要拼接：5m 归档的尾部天然滞后于「现在」（最后一次离线下载的时间），
    近端只能由采集器写入的 snapshot 补足。拼接**不跨周期替代**：
    snapshot 行只取 `ts > 5m 末点`，与 5m 行**零重叠**；每行仍带自己的 `period` 字段，
    读的人一眼能分清哪段是归档、哪段是实时快照。
    """
    if _REPO is not None:      # 冷热仓储版（冷档承载 5m 全历史）
        return _oi_auto_cold(exchange, mid, params)
    lim = _qint(params, "limit", HISTORY_LIMIT_DEFAULT, 1, HISTORY_LIMIT_MAX)
    frm = _qint(params, "from", None, 0, None)
    to = _qint(params, "to", None, 0, None)
    expr, expr_args = _oi_spread_expr(params)

    where5, a5 = "exchange=? AND symbol=? AND period='5m'", [exchange, mid]
    if frm is not None:
        where5 += " AND ts>=?"
        a5.append(frm)
    if to is not None:
        where5 += " AND ts<=?"
        a5.append(to)

    wheres, as_ = "exchange=? AND symbol=? AND period='snapshot'", [exchange, mid]
    if frm is not None:
        wheres += " AND ts>=?"
        as_.append(frm)
    if to is not None:
        wheres += " AND ts<=?"
        as_.append(to)

    with use_db() as con:
        try:
            # ① 先定归档末点（`MAX(ts)` 走索引，不读全表）。
            last5 = con.execute(f"SELECT MAX(ts) FROM oi_history WHERE {where5}",
                                a5).fetchone()[0]
            # ② 先取**归档末点之后**的 snapshot 近端，且**优先占满预算**。
            #    顺序很关键：这一段是展开图真正看得见的那几天，必须先保完整；
            #    若反过来（先让金字塔吃满 limit）会把近端截断，直接在归档末点处留一个洞。
            if last5 is not None:
                where_s = wheres + " AND ts>?"
                args_s = as_ + [last5]
            else:
                where_s, args_s = wheres, list(as_)
            rows_s = con.execute(f"SELECT * FROM oi_history WHERE {where_s} "
                                 f"ORDER BY ts DESC LIMIT ?", args_s + [lim]).fetchall()
            # ③ 剩余预算给归档金字塔（按 `ts DESC` 截断 ⇒ 优先丢**最久远**的那一头）。
            budget5 = max(1, lim - len(rows_s))
            if expr:
                sql5 = (f"SELECT *, MAX(ts) AS _bucket_ts FROM oi_history WHERE {where5} "
                        f"GROUP BY {expr} ORDER BY ts DESC LIMIT ?")
                sel5 = a5 + expr_args + [budget5]
            else:
                sql5 = (f"SELECT * FROM oi_history WHERE {where5} "
                        f"ORDER BY ts DESC LIMIT ?")
                sel5 = a5 + [budget5]
            rows5 = con.execute(sql5, sel5).fetchall()
        except sqlite3.OperationalError:
            return missing("oi_history 表不存在；请先执行 migrate.py", "not_connected")

    rows = list(rows5[::-1]) + list(rows_s[::-1])          # 两段各自恢复时间升序后拼接
    if not rows:
        return missing("oi_history 无已入库数据；本接口只读，不自动下载")
    items = []
    for r in rows:
        it = {"event_time": iso(r["ts"])}
        for c in OI_COLS:
            it[c] = r[c] if c in r.keys() else None
        items.append(it)
    note = ("period=auto：5m 归档（自适应金字塔，近期细/远期粗） + 该归档末点之后的 "
            "snapshot 近端；两段零重叠，每行 period 字段自证口径。"
            "近端 snapshot 段优先占满 limit（保证可见区间不留洞），"
            "归档金字塔用剩余预算，超限时优先丢最久远的一头")
    return envelope(items=items, total=len(items),
                    quality={"status": "ok", "age_seconds": None, "note": note})


def v1_oi(exchange, mid, params):
    """`/oi` —— 只读本机 `oi_history`（**总量口径**，无多空拆分）。

    查询参数：
      · `period=snapshot`（默认）| `5m` | `auto`
        - `auto`：5m 归档（自适应金字塔） + 该 5m 末点之后的 snapshot 近端，
          按时间升序返回，两段零重叠；见 `_oi_auto`。
      · `limit` / `from` / `to` / `bucket_ms` / `spread`：见 `_history` / `_oi_spread_expr`。
    未给 `period` 时**与修复前逐字一致**（只读 snapshot），零回归。
    """
    want = None
    if params is not None:
        want = (params.get("period") or [None])[0] or None
    if want == "auto":
        return _oi_auto(exchange, mid, params)
    return _history("oi_history", exchange, mid, "ts", OI_COLS,
                    params=params, period_col="period", default_period="snapshot")


def v1_funding(exchange, mid, params):
    return _history("funding_history", exchange, mid, "funding_time",
                    ["funding_rate", "mark_price", "rate_type", "source", "fetched_at"])


def v1_public_extras(exchange, mid, params):
    """`/api/v1/markets/{ex}/{mid}/public-extras` —— 币安公开扩展字段（**只读本机快照**）。

    为什么只读快照：用户口径要求「**维持前端读取不触发外部采集**，不要仅把页面请求
    直接变成交易所请求」。故**采集**由既有采集工作器
    （`realtime_collector.public_extras_loop` → `public_market_read.refresh_snapshot`）
    按周期把**页首有界集合**写入本机可读快照（`快照/public_extras/*.json`），
    本接口**只读文件**，**不发起任何交易所请求**、不落库、不写历史。

    填入原版字段的**两项真实数值**（其余保持缺失，不冒充）：
      · `long_short.top_position_long_short_ratio.value`
        = 币安 `/futures/data/topLongShortPositionRatio`（**大户持仓量多空比**，聚合比率，
          非「聪明钱」、无身份/无金额/无均价）；
      · `open_interest.usd` = 币安 `/futures/data/openInterestHist.sumOpenInterestValue`
        （**全市场总持仓名义额 USD**，无多空拆分）。
    `smart_money` 恒 `available:false`（币安公开 API 无金额/均价/身份）。

    `quality.age_seconds` 按**快照写入时点**现算，`status` 为 `ok`/`stale`（过期），
    `note` 另附**指标自身的数据时点**（`event_time` / `usd_event_time`）——
    不把「快照何时写」误当成「指标何时发生」。
    """
    if mid.startswith(exchange + ":"):
        mid = mid[len(exchange) + 1:]
    want_period = (params.get("period") or ["1h"])[0] or "1h"
    snap = public_snapshot.read(exchange, mid)
    idx = public_snapshot.read_index()
    idx_brief = {"updated_at": idx.get("updated_at"), "targets": idx.get("targets"),
                 "ok": idx.get("ok"), "skipped": idx.get("skipped"),
                 "failed": idx.get("failed"), "interval_s": idx.get("interval_s")}
    if snap is None:
        note = (f"本机快照未覆盖 {exchange}:{mid}：采集器只按**页首有界集合**周期刷新"
                f"（最近一轮目标 {idx.get('targets', '未知')} 个）；"
                f"本接口**只读快照、不触发任何交易所请求**。")
        return {"available": False, "exchange": exchange, "market_id": mid,
                "as_of": iso(_now_ms()), "missing_reason": note,
                "open_interest": None, "long_short": None, "orderbook": None,
                "smart_money": {"available": False, "reason": SMART_MONEY_REASON},
                "source_url": None, "snapshot": {"refreshed_at": None, "refreshed_at_ms": None,
                                                 "period": None, "index": idx_brief},
                "quality": {"status": "missing", "age_seconds": None, "note": note}}
    out = dict(snap)
    out["quality"] = public_snapshot.freshness(snap)
    snap_period = (snap.get("long_short") or {}).get("period")
    notes = ["本接口**只读本机快照**（既有采集工作器周期写入）；前端读取**不触发**交易所请求"]
    if snap_period and want_period != snap_period:
        notes.append(f"请求 period={want_period}，快照固定口径为 period={snap_period}")
    out["snapshot"] = {
        "refreshed_at": snap.get("snapshot_refreshed_at"),
        "refreshed_at_ms": snap.get("snapshot_refreshed_at_ms"),
        "period": snap_period, "index": idx_brief,
        "written_by": "realtime_collector.public_extras_loop → public_market_read.refresh_snapshot",
        "note": "；".join(notes),
    }
    return out


def v1_coverage():
    cov = covered()
    cells = []
    for c in cov:
        # 保留 NULL：计数未知 ≠ 缺 0 根
        miss = c.get("missing_within_observed_range")
        cells.append({"exchange": c["exchange"], "market_id": c["symbol"], "field": "klines",
                      "granularity": c["timeframe"], "earliest": iso(c["first_open"]),
                      "latest": iso(c["last_close"]), "gaps": [], "unit": UNIT["klines"],
                      "expected": c.get("expected"),
                      "missing_within_observed_range": miss,
                      "evidence_level": "样例已实取", "source_url": None,
                      "available_at": None, "available_at_known": False,
                      "free": True, "auth_required": False,
                      "note": ("缺根计数未知（未逐段定位）" if miss is None else
                               (f"已存首末范围内缺 {miss} 根（仅计数，未逐段定位区间；成因未核验）"
                                if miss else "已存首末范围内按开盘时间与周期推算无缺根"))})
    # OI / 费率：**统一采集进程确实在采**（写 oi_history / funding_history）。
    # 旧文案「未采集；本轮不启动采集」在采集已运行时是过时的、会误导。
    # 这里改为按交易所**如实报已入库行数与时间跨度**，但不逐标的伪造 granularity/earliest。
    #
    # 性能（BUG-003 修复）：`oi_history` 现已 1.27 亿行，`GROUP BY exchange` 的全表
    # （覆盖索引）扫描实测 >90s —— 不能放进请求路径。处理：
    #   · OI **只统计实时快照分区** `period='snapshot'`（走部分索引 ix_oi_hist_snapshot，
    #     本窗实测 0.18s）；这与「本机统一采集进程写入」的口径一致。
    #   · 归档历史（5m 等）的逐交易所聚合**显式标注暂不可得**（需全表扫描 1.27 亿行），
    #     **不填 0、不冒充全历史、不暗换为最近窗口**。
    #   · 费率表约十万行，逐交易所聚合成本可忽略，保留。
    OI_ARCHIVE_NOTE = ("归档历史（5m 等）逐交易所聚合需全表扫描 1.27 亿行，"
                       "已移出请求路径 → 该口径暂不可得，不填 0、不冒充")
    for field, table, ts_col, unit in (("oi", "oi_history", "ts", UNIT["oi"]),
                                       ("funding", "funding_history", "funding_time",
                                        UNIT["funding"])):
        stats = _ttl(f"covstats:{table}", 30.0, lambda t=table, c=ts_col: _exchange_span_stats(t, c))
        for c in cov:
            s = stats.get(c["exchange"])
            if s and s["n"]:
                note = (f"{table} 已入库 {s['n']} 行（本机统一采集进程写入）；"
                        f"本端点**不逐标的枚举**该字段覆盖，故不下逐标的结论")
                if field == "oi":
                    note += f"；口径=实时快照分区 period='snapshot'（有界）；{OI_ARCHIVE_NOTE}"
                cells.append({"exchange": c["exchange"], "market_id": c["symbol"], "field": field,
                              "granularity": ("snapshot" if field == "oi" else None),
                              "earliest": iso(s["f"]), "latest": iso(s["l"]),
                              "gaps": [], "unit": unit, "evidence_level": "样例已实取",
                              "source_url": None, "available_at": None, "available_at_known": False,
                              "free": True, "auth_required": False, "note": note})
            else:
                note = (f"{table} 实时快照分区（period='snapshot'）暂无该交易所数据；"
                        f"{OI_ARCHIVE_NOTE}" if field == "oi" else
                        f"{table} 暂无该交易所数据（如实置空，不冒充）")
                cells.append({"exchange": c["exchange"], "market_id": c["symbol"], "field": field,
                              "granularity": ("snapshot" if field == "oi" else None),
                              "earliest": None, "latest": None, "gaps": [],
                              "unit": unit, "evidence_level": "缺失", "source_url": None,
                              "available_at": None, "available_at_known": False, "free": True,
                              "auth_required": False, "note": note})
    return envelope(cells=cells, coverage_ratio=None, total_cells=None, covered_cells=len(cells),
                    quality={"status": "ok", "age_seconds": None,
                             "note": "仅统计已入库组合；全市场宇宙未知，故覆盖率不下结论"})


def v1_data_status():
    st = api_stats()
    metrics_summary = datacenter.get_datacenter_metrics_summary() if datacenter else None
    feeds = datacenter.get_data_feeds_status() if datacenter else None
    return envelope(database=DB_PATH, source_files=st["sources"],
                    coverage=covered(), external_calls=st["calls"],
                    metrics_summary=metrics_summary,
                    feeds=feeds,
                    external_calls_ok=st["ok_calls"],
                    external_calls_failed=st["failed_calls"],
                    scope_state=st["scope_state"],
                    note=("本机只读。官方归档由**独立归档下载器**直连静态资源下载、不经 REST 网关，"
                          "故 external_calls 仅统计公共 REST 调用，且成功/失败分开计数；"
                          "失败请求不计作已连通。"
                          "计数为**非阻塞重统计**：无界全表 COUNT 已移出请求路径，"
                          "尚未就绪时 external_calls* / source_files 返回 null（暂不可得），不填 0。"))



def v1_import_plan():
    """导入计划 + **现读**的真实运行状态。

    诚实边界（契约明确要求）：
      · `history.runs` 只是**下载 run 的记录**；记录为 running **不代表**该进程此刻仍健康，
        更不代表全池已补齐。进程是否活着看 `realtime_collector.heartbeat_age_seconds`。
      · `realtime_klines` 逐周期给出**真实 WS 消息时点**、未收盘/最近闭合时点、闭合入库根数、
        断线次数与补缺结果，不用 running 记录冒充健康。
    """
    kst = _realtime_kline_state()
    hist = _history_runs()
    return envelope(
        plan=IMPORT_PLAN,
        realtime_klines=kst,
        realtime_klines_note=("每 (交易所,周期) 一条：last_msg_at 为**真实 WS 消息**的本机"
                              "接收时点；last_forming_open_time 为当前未收盘 bar 的 open_time；"
                              "closed_written 为累计闭合入库根数。"
                              "欧易无 8h 原生周期，故只有 8 行（币安 9 行）。"),
        collector=_collector_health(),
        history={"runs": hist,
                 "note": "run 记录仅供参考，**不等于**进程健康或全池补齐；"
                         "本轮不新建第二个历史 run。"},
        quality={"status": "ok", "age_seconds": None,
                 "note": "计划与运行状态均现读库内真实记录，非写死文案"})


def _strategy_shape(item):
    """把注册表条目映射为前端 src/api/types.ts 的 Strategy 结构。

    研究未冻结时：param_hash=null、status=research_candidate、params.decided=false；
    不填占位串冒充已冻结。
    """
    cfg = item.get("config") or {}
    frozen = bool(item.get("frozen"))
    ver = item.get("config_version") or "未冻结"
    return {
        "strategy_key": item["strategy_key"],
        "strategy_version": ver,
        "rule_version": ver,
        "param_hash": item.get("config_hash"),
        "name": item["name"],
        "status": "research_candidate",
        "summary": f"{item['timeframe']} / {item['direction']}；{item['note']}",
        "params": [{"key": k, "label": k, "value": cfg.get(k), "unit": None,
                    "decided": frozen, "note": None} for k in item["required_config"]],
        "data_dependencies": [f"K线:{item['timeframe']}", "仅已收盘 bar",
                              "历史分位基线仅用当前 bar 之前"],
        "source_url": None,
        "config_status": item["config_status"],
        "missing_config": item.get("missing_config"),
        "config_errors": item.get("config_errors"),
        # 三层真实状态（2026-10-11）：policy / run / implementation 互不冒充
        "family": item.get("family"),
        "enabled_for_current_run": item.get("enabled_for_current_run"),
        "policy_status": item.get("policy_status"),
        "run_status": item.get("run_status"),
        "implementation_status": item.get("implementation_status"),
        "config_source": item.get("config_source"),
        "gate_status": item.get("gate_status"),
        "identity_ok": item.get("identity_ok"),
        "view_sha256": item.get("view_sha256"),
        "run_binding": item.get("run_binding"),
        "exec": item.get("exec"),
    }


def v1_strategies():
    """复数端点：当前可运行策略（新 WX_OI_RESONANCE_V1）+ 历史只读族（旧 F_WX 3a–3d / 旧 A/B）。

    WORK-04（2026-10-16）：旧策略**已退役**（`runnable=False`），只列在 `legacy` 里作历史追溯；
    `current` 只含新策略。旧件源码/旧 runs 以可恢复只读归档保留，不进入任何可运行入口。
    """
    with use_db() as con:
        current = registry.list_current(con)
        legacy = registry.list_legacy(con)
    ctx = _current_run_ctx()
    try:
        current_backtest = registry.describe_current_backtest(None)
    except Exception as _exc:                            # noqa: BLE001
        current_backtest = {"available": False,
                            "import_error": f"{type(_exc).__name__}: {_exc}"}
    return envelope(items=current + legacy, current=current, legacy=legacy,
                    current_run_keys=sorted(CURRENT_RUN_KEYS),
                    legacy_keys=sorted(LEGACY_RUN_KEYS),
                    current_backtest=current_backtest,
                    retirement={
                        **registry.retirement_block(),
                        "note": "旧策略（3a/3b/3c/3d/A/B）已退役：仅保留历史只读追溯，"
                                "不进入任何可运行入口；唯一可运行策略为 "
                                f"{registry.CURRENT_BACKTEST_ID}。",
                    },
                    run_binding={"gates": ctx.get("gates"),
                                 "all_pass": ctx.get("gates_all_pass"),
                                 "source": ctx.get("gates_source")},
                    run_context={"run_frozen": ctx.get("run_frozen"),
                                 "policy_run_frozen": ctx.get("policy_run_frozen"),
                                 "identity_ok": ctx.get("identity_ok"),
                                 "version": ctx.get("version")},
                    total=len(current) + len(legacy),
                    quality={"status": "ok", "age_seconds": None,
                             "note": "当前可运行策略仅 WX_OI_RESONANCE_V1（注册表键 "
                                     "wx_oi_resonance_v1）；旧 F_WX 3a/3b/3c/3d 与旧 A/B 已退役"
                                     "（runnable=False），仅列 legacy 供历史追溯；"
                                     "运行放行 = 视图 run_frozen ∧ 身份一致 ∧ G01–G05 全 pass"})


def v1_registered_configs():
    """**加性只读端点** `/api/v1/strategies/registered`（2026-10-11 接线路整改2）。

    如实给出：18 预登记配置的**生效身份**（信号参数 + 执行覆盖）、共享 5000 USDT 资金模型、
    G01–G05 运行门禁（pending 就写 pending，**不冒充放行**）。
    不改变任何既有端点行为；不读生产行情库（只用生效参数视图 + 门禁台账）。
    """
    view, vsha = wx_config.load_view()
    if view is None:
        return missing("生效参数视图缺失", "pending",
                       identity=None, registered_configs=[], registered_count=0)
    ident = wx_config.identity_check(view, vsha)
    eff = wx_config.effective_configs_check(view)
    bind = wx_gate.binding()
    risk = view.get("risk") or {}
    return envelope(
        identity={
            "view_sha256": vsha, "expected_view_sha256": wx_config.EXPECTED_VIEW_SHA256,
            "view_sha_ok": ident["view_sha_ok"],
            "registered_sha256": ident["registered"]["sha256"],
            "expected_registered_sha256": wx_config.EXPECTED_REGISTERED_CONFIGS_SHA256,
            "registered_ok": ident["registered"]["status"] == "ok",
            "effective_sha256": eff["sha256"],
            "expected_effective_sha256": wx_config.EXPECTED_EFFECTIVE_CONFIGS_SHA256,
            "effective_ok": eff["status"] == "ok",
            "identity_ok": ident["ok"], "problems": ident["problems"]},
        registered_configs=eff["rows"], registered_count=eff["count"],
        # 2026-10-11 合入 5000U 窗口受控补丁（adapter.py.5000.diff）：
        # 兜底默认值与 note 文案统一为 5000，与 risk 视图实际值一致（此前文案写 10000 与数值不符）。
        capital={"initial_equity_USDT": risk.get("initial_equity_USDT", 5000),
                 "shared_across_exchanges": ["binance", "okx"],
                 "per_arm_independent_replay": True,
                 "note": "跨所共用一个 5000 USDT 虚拟研究账户；各研究臂独立回放同一本金，不叠加"},
        gate_binding={"gates": bind.get("gates"), "all_pass": bind.get("all_pass"),
                      "self_check": bind.get("self_check"), "reason": bind.get("reason")},
        run_frozen=bool(view.get("run_frozen")),
        quality={"status": "ok", "age_seconds": None,
                 "note": "18 预登记配置生效身份（信号参数+执行覆盖）；G01–G05 待独立接受，不冒充放行"})


def v1_strategy():
    """单数端点 /strategy：保持前端 client.ts 既有契约兼容。

    顶层为当前可运行策略结构（`WX_OI_RESONANCE_V1`），另以 `registry` 给出全部注册条目
    （含已退役的旧 F_WX 3a–3d / 旧 A/B，`runnable=False`，仅历史追溯）。
    """
    with use_db() as con:
        current = registry.list_current(con)
        legacy = registry.list_legacy(con)
    by_key = {i["strategy_key"]: i for i in current + legacy}
    head = next((k for k in registry.CURRENT_RUN_KEYS if k in by_key), None)
    primary = _strategy_shape(by_key[head]) if head else {}
    return envelope(registry=[_strategy_shape(i) for i in current + legacy],
                    current=[_strategy_shape(i) for i in current],
                    legacy=[_strategy_shape(i) for i in legacy],
                    current_run_keys=sorted(CURRENT_RUN_KEYS),
                    retirement=registry.retirement_block(),
                    quality={"status": "ok", "age_seconds": None,
                             "note": "顶层为当前可运行策略；registry 含全部注册条目"
                                     "（旧 3a–3d / A/B 已退役，runnable=False）"},
                    **primary)


def v1_strategy_one(key):
    with use_db() as con:
        d = registry.describe(con, key)
    if d is None:
        raise LookupError("策略不存在")
    return envelope(strategy=d)


def _current_run_ctx():
    """**当前运行上下文**（只读、缓存 10s）：决定一个信号能否作为「当前可执行」。

    SIG07 修复（2026-10-11）：旧实现只判「当前启用族 + confirmed + 非空身份」，
    于是 2020 年的历史合成事件在 `run_frozen=false` 时仍 `actionable=true`。
    现在 `actionable` 必须同时满足：
      · 当前启用族（F_WX 3a–3d）；
      · 状态 confirmed（候选/失效/过期/未决一律 false）；
      · 身份非空（strategy_version / config_hash / data_version）；
      · 策略版本 == 当前生效视图版本；
      · config_hash == 该臂**当前生效**配置 canonical hash；
      · **运行放行**：视图 run_frozen ∧ 身份一致 ∧ G01–G05 全 pass（三者缺一不可）。
    任一不成立 ⇒ actionable=false 并给出原因。**历史事实仍可读**（status/direction 原样）。
    """
    def build():
        try:
            view, vsha = wx_config.load_view()
        except Exception as exc:  # noqa: BLE001
            view, vsha = None, None
        ident = (wx_config.identity_check(view, vsha) if view
                 else {"ok": False, "problems": ["生效视图缺失"]})
        bind = wx_gate.binding()
        arms = {}
        if view:
            for a in registry.CURRENT_RUN_KEYS:
                arms[a] = common.config_hash(wx_config.config_for_registered(view, a))
        run_frozen = bool(view and view.get("run_frozen") and ident["ok"]
                          and bind.get("all_pass"))
        return {"run_frozen": run_frozen,
                "policy_run_frozen": bool(view and view.get("run_frozen")),
                "identity_ok": bool(ident["ok"]),
                "identity_problems": ident.get("problems"),
                "gates": bind.get("gates"), "gates_all_pass": bool(bind.get("all_pass")),
                "gates_source": bind.get("source"),
                "version": (view or {}).get("spec_version"),
                "arms": arms}
    return _ttl("wx_run_ctx", 10.0, build)


def _ev_of(r):
    try:
        return json.loads(r["evidence_json"]) if r["evidence_json"] else {}
    except (ValueError, TypeError):
        return {}


def _signal_brief(r, ctx=None):
    """映射为 src/api/types.ts 的 Signal。未计算字段一律 null，不冒充。

    SIG04/SIG07 修复（2026-10-11）：**历史事实**与**当前可执行提示**分开。
      · `status`/`direction`            = 事件的历史事实（原样）
      · `current_direction`             = 仅在「当前启用族 + confirmed + 身份非空 +
        版本==当前 + config_hash==当前生效 + 运行放行」时给出
      · `actionable`                    = 上述门禁的布尔结果；任一不成立一律 false
    同时补齐事件身份（SIG03/SIG05）：策略键/版本、config_hash、data_version、
    confirmed_at/event_time、source_id、source_masks，并暴露逐项门禁布尔与原因。
    """
    ctx = ctx if ctx is not None else _current_run_ctx()
    st = r["status"] if r["status"] in SIGNAL_STATUSES else "undecided"
    direction = r["direction"] if r["direction"] in ("long", "short") else None
    keys = r.keys()
    skey = r["strategy_key"] if "strategy_key" in keys else None
    cver = r["strategy_version"] if "strategy_version" in keys else None
    chash = r["config_hash"] if "config_hash" in keys else None
    dv = r["data_version"] if "data_version" in keys else None
    ev = _ev_of(r)
    is_current_family = skey in CURRENT_RUN_KEYS
    identity_ok = bool(chash) and bool(dv) and bool(cver)
    version_ok = bool(cver) and cver == ctx.get("version")
    config_ok = bool(chash) and ctx.get("arms", {}).get(skey) == chash
    state_ok = (st == "confirmed")
    run_ok = bool(ctx.get("run_frozen"))
    reasons = []
    if not is_current_family:
        reasons.append("非当前启用族（历史策略）")
    if not state_ok:
        reasons.append("状态非 confirmed")
    if not identity_ok:
        reasons.append("缺策略版本/config_hash/data_version 身份，不作当前提示")
    elif not version_ok:
        reasons.append("策略版本非当前生效版本")
    elif not config_ok:
        reasons.append("config_hash 与当前生效配置不一致")
    if not run_ok:
        reasons.append("运行未放行（run_frozen=false 或 G01–G05 未全通过）")
    actionable = not reasons
    return {
        "signal_id": str(r["id"]),
        "exchange": r["exchange"],
        "market_id": r["symbol"],
        "symbol": r["symbol"],
        "status": st,
        "direction": direction,
        "current_direction": direction if actionable else None,
        "actionable": actionable,
        "actionable_reason": None if actionable else "；".join(reasons),
        "gates": {"current_family": is_current_family, "state_confirmed": state_ok,
                  "identity_ok": identity_ok, "version_current": version_ok,
                  "config_current": config_ok, "run_frozen": run_ok},
        "strength": None,            # 未计算；不是胜率概率
        "created_at": iso(r["created_at"]),
        "confirmed_at": iso(r["confirmed_at"]) if "confirmed_at" in keys else None,
        "event_time": iso(r["event_time"]) if "event_time" in keys else None,
        "expires_at": None,          # 未定义有效期，不推测
        "timeframe": r["timeframe"] if r["timeframe"] in TF_SET else None,
        "strategy_key": skey,
        "strategy_version": cver,
        "config_hash": chash,
        "data_version": dv,
        "source_id": r["source_id"] if "source_id" in keys else None,
        "source_masks": ev.get("source_masks"),
        "execution_label": ev.get("execution_label"),
    }


def _cond_detail(c):
    parts = []
    if c["actual"] is not None:
        parts.append(f"实际 {c['actual']}{c['unit'] or ''}")
    if c["threshold"] is not None:
        parts.append(f"阈值 {c['threshold']}{c['unit'] or ''}")
    if c["note"]:
        parts.append(c["note"])
    return "；".join(parts) or None


def _frozen_range_from(ev):
    if ev.get("frozen_at") is None:
        return None
    return {"start_time": None, "end_time": iso(ev.get("frozen_at")),
            "upper": ev.get("range_upper"), "lower": ev.get("range_lower"),
            "bar_count": None, "note": "区间根数未随事件留存；不推测"}


def _marker_obj(ev, key, note):
    t = ev.get(key)
    if t is None:
        return None
    return {"time": iso(t), "price": None, "note": note}


def _signal_detail(r, con, ctx=None):
    """映射为 src/api/types.ts 的 SignalDetail（除信封外的字段）。"""
    try:
        ev = json.loads(r["evidence_json"]) if r["evidence_json"] else {}
    except (ValueError, TypeError):
        ev = {}
    conds = con.execute("SELECT name,actual,threshold,unit,satisfied,note FROM signal_conditions "
                        "WHERE signal_id=? ORDER BY id", (r["id"],)).fetchall()
    return {
        "signal": _signal_brief(r, ctx),
        "frozen_range": _frozen_range_from(ev),
        "conditions": [{"name": c["name"],
                        "satisfied": None if c["satisfied"] is None else bool(c["satisfied"]),
                        "detail": _cond_detail(c)} for c in conds],
        "invalidation": None,        # 失效判定未接通
        "risk": None,                # 风控未接通；不造止损/目标
        "orders": [],                # 信号 != 下单
        "evidence": ev,
        "strategy_key": r["strategy_key"],
        "strategy_version": r["strategy_version"],
        "config_hash": r["config_hash"],
        "data_version": r["data_version"],
    }


def _parse_cursor(cur):
    """游标 = "confirmed_at:id"（与排序键一致）。非法游标 → None（从头开始）。"""
    if not cur:
        return None
    try:
        a, b = str(cur).split(":", 1)
        return int(a), int(b)
    except (ValueError, TypeError):
        return None


def v1_signals(params):
    """信号日志分页（SIG06 修复 2026-10-11）。

    旧实现固定 `LIMIT 500` 且 `has_more=false`、`total=len(items)` —— 第 500 条之后
    的记录被静默截断。现改为：
      · `limit`（默认 200，上限 1000）+ 稳定游标 `confirmed_at:id`（与排序键一致）；
      · `total` = 过滤条件下的**真实计数**；`has_more` 按实际是否还有下一页给出。
    """
    where, args = [], []
    f = {"exchange": "exchange", "status": "status", "timeframe": "timeframe",
         "strategy": "strategy_key", "market_id": "symbol"}
    for k, col in f.items():
        if k in params and params[k][0]:
            where.append(f"{col}=?"), args.append(params[k][0])
    if (params.get("current_only") or ["0"])[0] in ("1", "true", "True"):
        ph = ",".join("?" * len(CURRENT_RUN_KEYS))
        where.append(f"strategy_key IN ({ph})")
        args.extend(sorted(CURRENT_RUN_KEYS))
    try:
        limit = int((params.get("limit") or ["200"])[0])
    except (ValueError, TypeError):
        limit = 200
    limit = max(1, min(limit, 1000))
    cur = _parse_cursor((params.get("cursor") or [None])[0])

    base = " FROM signals"
    if where:
        base += " WHERE " + " AND ".join(where)
    with use_db() as con:
        try:
            total = con.execute("SELECT COUNT(*)" + base, args).fetchone()[0]
            sql = "SELECT *" + base
            page_args = list(args)
            if cur is not None:
                sql += (" AND (confirmed_at < ? OR (confirmed_at = ? AND id < ?))"
                        if where else
                        " WHERE (confirmed_at < ? OR (confirmed_at = ? AND id < ?))")
                page_args += [cur[0], cur[0], cur[1]]
            sql += " ORDER BY confirmed_at DESC, id DESC LIMIT ?"
            page_args.append(limit + 1)
            rows = con.execute(sql, page_args).fetchall()
        except sqlite3.OperationalError:
            return missing("signals 表不存在；请先执行 migrate.py", "not_connected",
                           items=[], has_more=False, next_cursor=None, total=None)
        has_more = len(rows) > limit
        rows = rows[:limit]
        ctx = _current_run_ctx()
        items = [_signal_brief(r, ctx) for r in rows]
        next_cursor = (f"{rows[-1]['confirmed_at']}:{rows[-1]['id']}"
                       if has_more and rows else None)
    return envelope(items=items, has_more=has_more, next_cursor=next_cursor,
                    total=total, limit=limit,
                    quality={"status": "ok", "age_seconds": None,
                             "note": "游标分页：confirmed_at:id（与排序键一致）；"
                                     "total 为过滤后真实计数；信号输出 != 下单；"
                                     "actionable 需当前族+confirmed+身份+当前版本/config+运行放行"})


def v1_signal(sid):
    with use_db() as con:
        try:
            r = con.execute("SELECT * FROM signals WHERE id=?", (sid,)).fetchone()
        except sqlite3.OperationalError:
            return missing("signals 表不存在；请先执行 migrate.py", "not_connected")
        if not r:
            return missing(f"无 signal_id={sid}")
        detail = _signal_detail(r, con, _current_run_ctx())
    return envelope(**detail)


def v1_rule_snapshot(exchange, mid, params):
    """规则快照：**精确匹配** 指定策略/周期/事件标记。

    必须同时给出 marker_kind 与 marker_time；按证据里的对应时间字段精确匹配，
    查不到即返回缺失，**不退回最新信号**。策略 B 不产出 retest/breakout/frozen_range
    标记，故对 B 一律不匹配（不得默认贴 retest 标签）。
    """
    if mid.startswith(exchange + ":"):
        mid = mid[len(exchange) + 1:]
    kind = (params.get("marker_kind") or [None])[0]
    mtime = (params.get("marker_time") or [None])[0]
    strategy = (params.get("strategy") or [None])[0]
    timeframe = (params.get("timeframe") or [None])[0]

    if not kind or not mtime:
        return missing("必须同时指定 marker_kind 与 marker_time；不退回最新信号", "undecided")
    if kind not in MARKER_EVIDENCE:
        return missing(f"marker_kind={kind} 无对应事件类型（当前仅 "
                       f"{sorted(MARKER_EVIDENCE)}；stop/target 未产出）", "not_connected")

    ev_key = MARKER_EVIDENCE[kind]
    with use_db() as con:
        try:
            sql = "SELECT * FROM signals WHERE exchange=? AND symbol=?"
            args = [exchange, mid]
            if strategy:
                sql += " AND strategy_key=?"
                args.append(strategy)
            if timeframe:
                sql += " AND timeframe=?"
                args.append(timeframe)
            rows = con.execute(sql + " ORDER BY confirmed_at DESC LIMIT 1000", args).fetchall()
        except sqlite3.OperationalError:
            return missing("signals 表不存在；请先执行 migrate.py", "not_connected")
        pick = None
        for r in rows:
            try:
                ev = json.loads(r["evidence_json"]) if r["evidence_json"] else {}
            except (ValueError, TypeError):
                ev = {}
            t = ev.get(ev_key)
            if t is not None and iso(t) == mtime:
                pick = (r, ev)
                break
        if pick is None:
            return missing(f"未找到匹配 marker_kind={kind} marker_time={mtime} 的事件；"
                           f"不退回最新信号", "missing")
        r, ev = pick
        detail = _signal_detail(r, con)
    return envelope(
        exchange=exchange, market_id=mid, timeframe=r["timeframe"],
        marker_kind=kind, marker_time=iso(ev.get(ev_key)),
        marker_price=None,
        conditions=detail["conditions"], frozen_range=detail["frozen_range"],
        breakout=_marker_obj(ev, "breakout_close_time", "突破收盘确认"),
        retest=_marker_obj(ev, "retest_close_time", "独立回踩确认"),
        invalidation=None, signal_id=str(r["id"]),
        quality={"status": "ok", "age_seconds": None,
                 "note": "条件实际值/阈值均由后端给出，前端不做比较推断；按标记精确匹配"})


def v1_stub_list(reason):
    return missing(reason, "not_connected", items=[], has_more=False, next_cursor=None, total=None)


def route_v1(path, params):
    seg = [unquote(p) for p in path.split("/") if p][2:]
    if seg == ["health"]:
        return v1_health(), 200
    if seg == ["overview"]:
        return v1_overview(), 200
    if seg == ["markets"]:
        return v1_markets(), 200
    if seg == ["pool"]:
        return v1_pool(), 200
    if seg == ["pool", "status"]:
        return v1_pool_status(), 200
    if seg == ["strategies"]:
        return v1_strategies(), 200
    if seg == ["strategies", "registered"]:
        # 加性只读端点（整改2）：18 配置生效身份 + 共享资金模型 + 门禁 pending。
        return v1_registered_configs(), 200
    if seg == ["strategy"]:
        return v1_strategy(), 200
    if len(seg) == 2 and seg[0] == "strategies":
        return v1_strategy_one(seg[1]), 200
    if seg == ["signals"]:
        return _v1_signals_merged(params), 200
    if len(seg) == 3 and seg[0] == "smart-money":
        # 币安聪明钱：**只读本机快照**（采集器周期写入），不触发任何交易所请求。
        # 兼容前端既有契约 `GET /api/v1/smart-money/{exchange}/{market_id}`。
        return smart_money.v1_smart_money(seg[1], seg[2], params), 200
    if len(seg) == 2 and seg[0] == "signals":
        # 实时层 uid 优先（`live_signal.signal_uid`）；否则回落既有库内整数 id 路径
        # **详情/审计读取走 `scope=all`**：按 uid 精确取单条，不受当前交易信号区默认口径限制
        # （被移出当前区的历史/结构记录仍可按 uid 审计读取）。
        if live is not None:
            got = live.live_signals({"signal_id": [seg[1]], "scope": ["all"]})
            items = got.get("items") or []
            if items:
                return envelope(signal=items[0], source="live_signal",
                                quality={"status": "ok", "age_seconds": None,
                                         "note": "实时信号层（展示与可执行分列；审计口径 scope=all）"}), 200
        return v1_signal(seg[1]), 200
    # ── 信号与持仓接通（2026-10-13，加性）────────────────────────────────────
    if seg and seg[0] == "paper":
        if live is None:
            return _live_missing(f"实时/纸面模块导入失败：{_LIVE_IMPORT_ERROR}"), 200
        if seg == ["paper", "accounts"]:
            return live.paper_accounts(), 200
        if seg == ["paper", "positions"]:
            return live.paper_positions(), 200
        if seg == ["paper", "orders"]:
            # 2026-10-14 专项：GET 改为**真实只读**订单/挂单/平仓成交查询
            # （原实现只返回「请用 POST」的说明，等于该只读能力缺失）。
            # **写入口仍然是 POST**（下方 do_POST），GET 绝不隐式开平仓。
            return live.paper_orders(params), 200
        if seg == ["paper", "funding-audit"]:
            # 资金费符号口径 + 迟到补记缺口（只读）
            return live.paper_funding_audit(params), 200
        if seg == ["paper", "legacy-audit"]:
            # 旧版/未验收/规则违规 只读审计清单（2026-10-15 闭环整改·阶段A）
            return live.legacy_audit(), 200
    if seg == ["live", "health"]:
        if live is None:
            return _live_missing(f"实时模块导入失败：{_LIVE_IMPORT_ERROR}"), 200
        return live.live_health(), 200
    if seg == ["accounts"]:
        if live is None:
            return _live_missing(f"实盘只读模块导入失败：{_LIVE_IMPORT_ERROR}"), 200
        return live.live_accounts(params), 200
    if seg and seg[0] == "backtests":
        # 新通道（研究回测）：**不带 mode** 时走 backtest_api（实验列表 / {id} / 子资源）。
        # 带 mode（history / paper_review）时**保持旧契约**，仍走 live.backtest_review。
        mode = (params.get("mode") or [None])[0]
        if mode is None and backtest is not None:
            return backtest.route(seg[1:], params)
        if live is None:
            return _live_missing(f"回测复盘模块导入失败：{_LIVE_IMPORT_ERROR}"), 200
        return live.backtest_review(params), 200
    if seg and seg[0] == "reviews":
        if live is None:
            return _live_missing(f"回测复盘模块导入失败：{_LIVE_IMPORT_ERROR}"), 200
        return live.backtest_review(params), 200
    if seg == ["data", "status"]:
        return v1_data_status(), 200
    if seg == ["data", "coverage"]:
        return v1_coverage(), 200
    if seg == ["data", "import-plan"]:
        return v1_import_plan(), 200
    if seg == ["data", "oi"]:
        limit = int((params.get("limit") or [150])[0])
        q = (params.get("q") or [None])[0]
        items = datacenter.get_oi_items(limit, q) if datacenter else []
        return envelope(items=items, total=len(items)), 200
    if seg == ["data", "funding"]:
        limit = int((params.get("limit") or [150])[0])
        q = (params.get("q") or [None])[0]
        items = datacenter.get_funding_items(limit, q) if datacenter else []
        return envelope(items=items, total=len(items)), 200
    if seg == ["data", "smart-money"]:
        limit = int((params.get("limit") or [150])[0])
        q = (params.get("q") or [None])[0]
        items = datacenter.get_smart_money_items(limit, q) if datacenter else []
        return envelope(items=items, total=len(items)), 200
    if seg == ["data", "all-in-one"]:
        limit = int((params.get("limit") or [150])[0])
        q = (params.get("q") or [None])[0]
        items = datacenter.get_all_in_one_table(limit, q) if datacenter else []
        return envelope(items=items, total=len(items)), 200
    if seg == ["data", "feeds"]:
        feeds = datacenter.get_data_feeds_status() if datacenter else []
        return envelope(feeds=feeds, total=len(feeds)), 200
    if seg in (["data", "whitelist"], ["data", "backfill"]):
        summary = datacenter.get_backfill_and_whitelist_summary() if datacenter else {}
        return envelope(**summary), 200
    if len(seg) == 3 and seg[0] == "markets":

        return v1_market(seg[1], seg[2]), 200
    if len(seg) == 4 and seg[0] == "markets":
        tail = seg[3]
        if tail == "candles":
            return v1_candles(seg[1], seg[2], params), 200
        if tail == "oi":
            return v1_oi(seg[1], seg[2], params), 200
        if tail == "funding":
            return v1_funding(seg[1], seg[2], params), 200
        if tail == "rule-snapshots":
            return v1_rule_snapshot(seg[1], seg[2], params), 200
        if tail == "public-extras":
            # 币安公开扩展字段：**只读本机快照**（采集器周期写入），不触发交易所请求。
            return v1_public_extras(seg[1], seg[2], params), 200
        if tail == "metrics":
            return missing("指标模块尚未接通；本轮仅数据契约与策略模块", "not_connected"), 200
    raise LookupError("接口不存在")


def _v1_signals_merged(params):
    """`/api/v1/signals` = **实时层** + 库内 `signals` 表（历史研究信号）。

    · 实时层来自 `live_signal`（`origin=live` / `historical_research` 分列）；
    · **默认 `scope=current`（当前交易信号区）**：只保留实时层的**当前真实臂入场**信号
      （过滤在 `live_api.live_signals` 内、分页 LIMIT 与 total/has_more 之前完成），
      并**丢弃库内 `signals` 表项** —— 那是历史研究路径，不得混入当前交易信号区。
    · `scope=all|audit|history`：历史审计通道，保留实时层全量与库内表项（只读对账用）。
    · **展示与可执行分离**：实时层 `display_state` 恒 `displayable`；`executable_state`
      单独表达，**不**使用 G01–G05 正式回测门禁（那是回测闸门，不是只读信号展示闸门）。
    · 任一源不可用 ⇒ 如实降级并给出 `missing_reason`，不伪造条数。
    """
    base = v1_signals(params)
    if live is None:
        base["quality"] = {"status": (base.get("quality") or {}).get("status", "ok"),
                           "age_seconds": None,
                           "note": f"实时信号层不可用：{_LIVE_IMPORT_ERROR}"}
        return base
    scope = live._scope_of(params)
    is_current = scope == live.CURRENT_SCOPE
    # **运行身份 / 账本来源 / 数据版本透传**（2026-10-15 身份统一）：
    # 合并层此前只搬 items/total/has_more/quality，把实时层的身份字段整块丢掉
    # ⇒ 信号接口无法追溯「哪个策略版本、哪套规则、哪个库、哪份数据」。
    _ident_keys = ("strategy_version", "rule_version", "param_hash", "param_hash_basis",
                   "params_source", "rule_view_available", "rule_view_spec_version",
                   "rule_view_note", "exec_core_crosscheck", "data_version",
                   "daily_list_version", "pool_rule_identity", "ledger_source", "as_of")
    try:
        _id = live._runtime_identity()
        for _k, _v in _id.items():
            if _k in _ident_keys and _v is not None:
                base[_k] = _v
    except Exception:                                 # noqa: BLE001
        pass
    try:
        lv = live.live_signals(params)
    except Exception as exc:                          # noqa: BLE001
        base["quality"] = {"status": (base.get("quality") or {}).get("status", "ok"),
                           "age_seconds": None,
                           "note": f"实时信号层读取失败：{type(exc).__name__}: {exc}"}
        return base
    live_items = lv.get("items") or []
    # 实时层自报的身份**优先**（它绑定的是这次真实读库所用的口径）
    for _k in _ident_keys:
        if lv.get(_k) is not None:
            base[_k] = lv[_k]
    # 当前交易信号区**不含**库内 `signals` 表（历史研究路径）；审计通道才合并。
    db_items = [] if is_current else (base.get("items") or [])
    merged = live_items + db_items
    n_live = sum(1 for i in live_items if i.get("origin") == "live")
    n_hist = sum(1 for i in live_items if i.get("origin") == "historical_research")
    base["items"] = merged
    base["total"] = (lv.get("total") if is_current else len(merged))
    base["has_more"] = bool(lv.get("has_more")) if is_current else (
        bool(base.get("has_more")) or bool(lv.get("has_more")))
    if is_current:
        base["missing_reason"] = lv.get("missing_reason")
        base["quality"] = lv.get("quality") or {"status": "ok", "age_seconds": None}
        return base
    base["quality"] = {
        "status": "ok", "age_seconds": None, "scope": scope,
        "note": f"审计通道(scope={scope})：实时层 live={n_live}、历史研究={n_hist}；"
                f"库内 signals 表={len(db_items)}。展示(display_state)与可执行(executable_state)"
                "分列；不适用 G01–G05 正式回测门禁；信号输出 != 下单"}
    return base


# ── 请求并发上限 ────────────────────────────────────────────────────────────
# `ThreadingHTTPServer` 默认**每来一个连接就新开一个线程，且不设上限**。
# 一旦单请求耗时（实测最坏 12.6 秒）超过前端轮询周期（5 秒），请求到达快于完成，
# 线程与连接就会无界堆积 —— 实测 35363 进程堆积到 1971 个 socket + 1742 个 DB fd，
# 随后连接被服务端重置、页面只剩骨架。这里把「同时在处理请求的线程数」硬性封顶：
# 多余的连接留在内核 backlog 排队，而不是无限开线程。
# 2026-10-14 专项：默认由 24 提到 64（重/轻再各自细分，见 `Handler` 的容量预留）。
REQ_CONCURRENCY = int(os.environ.get("ADAPTER_MAX_CONCURRENCY", "64"))
REQ_BACKLOG = int(os.environ.get("ADAPTER_BACKLOG", "128"))


class Handler(BaseHTTPRequestHandler):
    server_version = "MarketDataHubAdapter/0.2"

    # ── 连接语义（2026-10-14 模拟实盘闭环专项·任务 1 根因修复）───────────────────
    # 原实现**未设置** `protocol_version` ⇒ 默认 HTTP/1.0（实测 `r.version == 10`），
    # 每个响应后无条件关连接。表现：客户端连接池复用已被服务端关闭的连接 ⇒
    # `http.client.RemoteDisconnected`；且连接反复重建放大了并发压力。
    # 改为 HTTP/1.1：响应恒带 `Content-Length`（下方 `_emit`），客户端可安全复用连接；
    # 需要关闭时显式发 `Connection: close`。**不改变任何接口契约与响应内容**。
    protocol_version = "HTTP/1.1"

    # 连接级读超时（秒）。**必须设置**：BaseHTTPRequestHandler 默认无超时，
    # 一旦客户端（如前端 dev proxy 的 keep-alive 连接）不再发送数据，
    # 处理线程会永久阻塞在 rfile.readline() 上，导致 socket fd 与线程无界累积
    # （实测曾累积到 1197 个 socket / 1168 个 SQLite 连接，使 /api/v1/markets 从
    #  6.8s 退化到 >30s 超时）。超时后 handle_one_request 会置 close_connection
    # 并正常收尾，不再泄漏。仅影响空闲连接，不改变任何接口契约与响应内容。
    timeout = 30

    # ── 轻量接口**容量预留**（2026-10-14 专项·任务 1 根因修复）───────────────────
    # 复现证据：50 并发**重**请求（`/markets` 5.85MB、`/data/coverage` 7.20MB、
    # `/signals?limit=1000` 1.09MB）+ 8 个轻量探针（客户端超时 3s）⇒ 轻量接口
    # **56 次客户端 TimeoutError**（探针 p50 由 7ms 恶化到 >3s）。
    # 机制：处理线程总量封顶后，重请求把槽位占满，轻量请求滞留内核 backlog，
    # 客户端自身超时先到 ⇒ 客户端断开；服务端随后才写响应 ⇒ 日志里 2671 条
    # `BrokenPipeError`/`ConnectionResetError`（**全在响应写出阶段**）。
    # 修法：把「重/轻」拆成**两个独立信号量**——重请求最多占 `_HEAVY_SLOTS`，
    # 轻量接口恒有 `_LIGHT_SLOTS` 可用，**结构上不可能被重请求饿死**。
    _LIGHT_SLOTS = int(os.environ.get("ADAPTER_LIGHT_CONCURRENCY", "12"))
    _HEAVY_SLOTS = max(4, REQ_CONCURRENCY - _LIGHT_SLOTS)
    _LIGHT_SEM = threading.BoundedSemaphore(_LIGHT_SLOTS)
    _HEAVY_SEM = threading.BoundedSemaphore(_HEAVY_SLOTS)
    # 轻量（实时/纸面/健康）路径前缀：这些接口是「页面轮询 + 验收探针」的主路径
    _LIGHT_PREFIXES = ("/api/v1/health", "/api/v1/live/", "/api/v1/paper/",
                       "/api/v1/accounts", "/api/v1/backtests", "/api/health", "/health")
    # 观测计数（供 /api/v1/health 暴露；只增不改任何业务语义）
    CLIENT_ABORTS = 0
    REQUESTS = 0

    # ── 大响应**字节缓存**（2026-10-14 专项·任务 1 根因修复第二段）────────────────
    # 复现：30 个**独立进程**客户端各以紧循环反复取 `/markets`(5.85MB) /
    # `/data/coverage`(7.20MB) / `/signals?limit=1000`(1.09MB) 时，轻量探针
    # （`/live/health`、`/paper/accounts`）在 3s 客户端超时下**35–55 次超时**
    # （p50 1.5s）。根因不是槽位，而是 **CPython GIL 下的 `json.dumps`**：
    # 每个请求都要把 MB 级对象重新序列化一次，30 个并发请求把进程打成 CPU 饱和，
    # 轻量请求即使拿到线程也排不到 GIL ⇒ 客户端先超时（服务端随后写响应即
    # 日志里的 BrokenPipe/ConnectionReset）。
    # 修法：对**最大的只读端点**缓存**已序列化字节**（短 TTL，默认 3s），
    # 把「每请求一次 MB 级序列化」降为「每 TTL 一次」。**只缓存字节，不改内容**；
    # TTL 可由 `ADAPTER_RESP_CACHE_TTL` 覆盖，设为 0 即完全关闭（回到原行为）。
    _RESP_TTL = float(os.environ.get("ADAPTER_RESP_CACHE_TTL", "3"))
    # 过期后仍可继续服务的倍数（保留期 = `_RESP_TTL * _RESP_STALE_FACTOR`）。
    # 过期瞬间**不阻塞任何客户端**：前台先回旧字节，同时只允许**一个**线程后台重算
    # （消除「30 个并发在 TTL 到期同一刻一起重算 MB 级 `json.dumps`」的惊群，
    #  实测该惊群正是轻量探针 p95 从 136ms 恶化到 2172ms 的直接原因）。
    _RESP_STALE_FACTOR = float(os.environ.get("ADAPTER_RESP_STALE_FACTOR", "10"))
    _RESP_CACHE_PREFIXES = ("/api/v1/markets", "/api/v1/data/coverage",
                            "/api/v1/signals", "/api/v1/data/status")
    _RESP_CACHE = {}
    _RESP_CACHE_LOCK = threading.Lock()
    _REFRESHING = set()          # 正在后台重算的 key（同一 key 同时只允许一个）
    RESP_CACHE_HITS = 0          # 命中**新鲜**条目
    RESP_CACHE_STALE = 0         # 命中**过期但仍在保留期**的条目
    RESP_CACHE_REFRESH = 0       # 触发的后台重算次数

    @classmethod
    def _cacheable(cls, path):
        return cls._RESP_TTL > 0 and any(path.startswith(p)
                                         for p in cls._RESP_CACHE_PREFIXES)

    @classmethod
    def _cache_get(cls, key):
        """返回 `(status, raw, fresh)`；未命中或超出保留期返回 `None`。"""
        now = time.time()
        with cls._RESP_CACHE_LOCK:
            hit = cls._RESP_CACHE.get(key)
            if not hit:
                return None
            if hit[0] > now:
                cls.RESP_CACHE_HITS += 1
                return hit[1], hit[2], True
            if hit[0] + cls._RESP_TTL * cls._RESP_STALE_FACTOR > now:
                cls.RESP_CACHE_STALE += 1
                return hit[1], hit[2], False
        return None

    @classmethod
    def _begin_refresh(cls, key):
        """抢占某 key 的后台重算权；返回 True 表示调用方负责重算。"""
        with cls._RESP_CACHE_LOCK:
            if key in cls._REFRESHING:
                return False
            cls._REFRESHING.add(key)
            cls.RESP_CACHE_REFRESH += 1
            return True

    @classmethod
    def _end_refresh(cls, key):
        with cls._RESP_CACHE_LOCK:
            cls._REFRESHING.discard(key)

    def _refresh_async(self, key, path, params):
        """后台重算并覆盖缓存。**不阻塞任何客户端**，失败也不影响前台。"""
        try:
            if path.startswith("/api/v1"):
                body, status = route_v1(path, params)
            else:
                body, status = read_route(STORE, path), 200
            if status == 200:
                self._cache_put(key, status,
                                json.dumps(body, ensure_ascii=False).encode())
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._end_refresh(key)

    @classmethod
    def _cache_put(cls, key, status, raw):
        with cls._RESP_CACHE_LOCK:
            cls._RESP_CACHE[key] = (time.time() + cls._RESP_TTL, status, raw)

    def _is_light(self, path):
        return any(path.startswith(p) for p in self._LIGHT_PREFIXES)

    def _emit_raw(self, status, raw):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            type(self).CLIENT_ABORTS += 1
            self.close_connection = True

    def _emit(self, body, status):
        """统一响应写出：**恒带 Content-Length**；客户端提前断开不抛栈。

        客户端在服务端写出前/中关闭连接（超时放弃、页面切走、验收脚本提前退出）
        属**连接级事件**，不是服务端错误。原实现让它冒泡到 socketserver 打印整段
        traceback（实测 2671 条），既污染日志也会在并发下放大抖动。
        """
        self._emit_raw(status, json.dumps(body, ensure_ascii=False).encode())

    def do_GET(self):  # noqa: N802
        parsed = urlsplit(self.path)
        params = parse_qs(parsed.query, keep_blank_values=True)
        type(self).REQUESTS += 1
        cache_key = parsed.path + "?" + parsed.query
        if self._cacheable(parsed.path):
            got = self._cache_get(cache_key)
            if got is not None:
                status, raw, fresh = got
                if not fresh and self._begin_refresh(cache_key):
                    threading.Thread(
                        target=self._refresh_async,
                        args=(cache_key, parsed.path, params),
                        daemon=True).start()
                self._emit_raw(status, raw)
                return
        sem = self._LIGHT_SEM if self._is_light(parsed.path) else self._HEAVY_SEM
        sem.acquire()
        try:
            try:
                if parsed.path.startswith("/api/v1"):
                    body, status = route_v1(parsed.path, params)
                else:
                    body, status = read_route(STORE, self.path), 200
            except LookupError as exc:
                body, status = {"错误": str(exc)}, 404
            except (ValueError, TypeError) as exc:
                body, status = {"错误": str(exc)}, 400
            except Exception as exc:  # noqa: BLE001
                body, status = {"错误": "读取失败", "类型": type(exc).__name__}, 500
            raw = json.dumps(body, ensure_ascii=False).encode()
            if self._cacheable(parsed.path) and status == 200:
                self._cache_put(cache_key, status, raw)
            self._emit_raw(status, raw)
        finally:
            sem.release()

    def _send_json(self, body, status):
        self._emit(body, status)

    def do_POST(self):  # noqa: N802
        """写入口（**仅两处**）：

        · `POST /api/v1/paper/orders` → 服务端**纸面账本**开/平仓（`paper_engine`），
          不触碰实盘、不提交任何真实订单；
        · `POST /api/v1/backtests`    → **创建研究回测任务**（2026-10-16 新增）。
          只写 `runs/<run_id>/`（原子独占目录），**不写 paper 账户、不启 worker**、
          不碰旧 ledger/state/manifest；策略为纯合成模拟，`live_order_enabled` 恒 False。

        浏览器不得自行造仓；除上述两路外一律 404。
        """
        parsed = urlsplit(self.path)
        type(self).REQUESTS += 1
        sem = self._LIGHT_SEM if self._is_light(parsed.path) else self._HEAVY_SEM
        sem.acquire()
        try:
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw_req = self.rfile.read(n) if n > 0 else b""
                payload = json.loads(raw_req.decode("utf-8")) if raw_req else {}
                if not isinstance(payload, dict):
                    raise ValueError("请求体必须是 JSON 对象")
            except (ValueError, UnicodeDecodeError) as exc:
                self._emit({"错误": f"请求体解析失败：{exc}"}, 400)
                return
            try:
                bt_res = (backtest.route_post(parsed.path, payload)
                          if backtest is not None else None)
                if bt_res is not None:
                    body, status = bt_res
                elif parsed.path == "/api/v1/paper/orders":
                    if live is None:
                        body, status = _live_missing(
                            f"纸面模块导入失败：{_LIVE_IMPORT_ERROR}"), 200
                    else:
                        body, status = live.paper_order_post(payload)
                else:
                    body, status = {"错误": "接口不存在"}, 404
            except LookupError as exc:
                body, status = {"错误": str(exc)}, 404
            except (ValueError, TypeError) as exc:
                body, status = {"错误": str(exc)}, 400
            except Exception as exc:  # noqa: BLE001
                body, status = {"错误": "写入失败", "类型": type(exc).__name__}, 500
            self._emit(body, status)
        finally:
            sem.release()

    def log_message(self, fmt, *args):
        pass


# （`REQ_CONCURRENCY` / `REQ_BACKLOG` 已上移到 `class Handler` 之前定义：
#   `Handler` 的容量预留需要在类体求值时就能读到这两个常量。）


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """带并发上限的线程化 HTTP 服务：防止请求堆积撑爆线程与 fd。

    2026-10-14 专项补充：
      · `daemon_threads = True` ⇒ 进程退出不被残留连接线程拖住；
      · 覆盖 `handle_error`：客户端提前断开（BrokenPipe/ConnectionReset）是**连接级事件**，
        不再打印整段 traceback（原实现实测 2671 条，污染日志并在并发下放大抖动）。
    """

    request_queue_size = REQ_BACKLOG
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(REQ_CONCURRENCY)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        self._slots.acquire()          # 满员时在此背压，不再新开线程
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                            TimeoutError)):
            Handler.CLIENT_ABORTS += 1
            return                      # 客户端提前断开：静默，不打印 traceback
        super().handle_error(request, client_address)


def _loopback_server(host, family):
    """按地址族构造回环监听器；只绑本机回环，不开放局域网。"""
    cls = type("AdapterHTTPServer", (_BoundedThreadingHTTPServer,),
               {"address_family": family})
    return cls((host, PORT), Handler)


# ── 启动预热 ────────────────────────────────────────────────────────────────
# 冷启动时第一次 `/api/v1/markets` 要为 549 个标的建全套库级索引，
# 在库近 20GB、历史下载并发写时实测 15 秒以上。若此刻前端正在 5 秒轮询，
# 请求会堆在 24 个并发槽位之后形成**自持拥塞**：实测重启后
# 直连 markets 从 0.02s 恶化到 7~12s、health 到 23s，且关掉轮询后立刻恢复 0.02s。
# 故在开始服务的同时，用后台线程先把最贵的库级索引算进 TTL 缓存；
# 首个真实请求即可命中（或只等一次已在进行的重算），不再触发堆积。
PREWARM_KEYS = (("covered", 10.0, "covered"), ("sources", 300.0, "sources_index"),
                ("pool", 30.0, "pool_index"), ("cls", 60.0, "cls_index"),
                ("scope", 30.0, "workbench_scope"), ("realtime", 3.0, "realtime_index"),
                ("oi", 3.0, "oi_latest_index"), ("funding", 3.0, "funding_latest_index"))


def _prewarm():
    g = globals()
    t0 = time.time()
    # BUG-003：无界重统计（sources/api_calls 全表 COUNT）也在此**后台**预触发，
    # 让首个真实请求就命中缓存，而不是等到第一次 `/data/status` 才启动重统计。
    # 注意：`_heavy_stats` 立即返回（不阻塞），真正计算在后台线程里做。
    try:
        _heavy_stats("stats:sources", _count_sources)
        _heavy_stats("stats:api_calls", _api_calls_counts)
    except Exception:                                  # noqa: BLE001
        pass
    for key, ttl_s, fname in PREWARM_KEYS:
        try:
            _ttl(key, ttl_s, g[fname])
        except Exception:                                  # noqa: BLE001
            pass
    print(f"[adapter] 预热完成（库级索引已进 TTL 缓存）用时 {time.time()-t0:.2f}s",
          flush=True)


def main():
    """同时监听 IPv4 127.0.0.1 与 IPv6 ::1。

    macOS 上 `localhost` 先解析到 `::1`；只绑 IPv4 时，按 `localhost`/`[::1]`
    访问的客户端会直接 ECONNREFUSED。两个监听器都只在回环地址上，不暴露局域网。
    """
    specs = [("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)]
    servers, bound = [], []
    for host, family in specs:
        try:
            servers.append(_loopback_server(host, family))
            bound.append(host)
        except OSError as exc:
            print(f"[adapter] 绑定 {host}:{PORT} 失败（跳过）：{exc}", flush=True)
    if not servers:
        raise SystemExit(f"[adapter] 无法绑定任何回环地址，端口 {PORT}")
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    # 后台预热最贵的库级索引，避免重启后首轮请求触发前端轮询堆积（见 _prewarm 注释）
    threading.Thread(target=_prewarm, daemon=True).start()
    # 回测任务启动钩子：把遗留 queued/running 标 interrupted（**不冒充成功**，保留原因）
    if backtest is not None:
        try:
            _touched = backtest.startup()
            if _touched:
                print(f"[adapter] 回测遗留运行标 interrupted：{_touched}", flush=True)
        except Exception as _exc:                     # noqa: BLE001
            print(f"[adapter] 回测启动钩子失败（不影响服务）：{_exc}", flush=True)
    print(f"[adapter] {','.join(bound)}:{PORT} db={DB_PATH}", flush=True)
    servers[0].serve_forever()


if __name__ == "__main__":
    main()
