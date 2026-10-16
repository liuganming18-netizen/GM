#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""币安**聪明钱（Smart Signal）**公开只读接入（**独立文件**）。

为什么独立成文件：`adapter.py` / `realtime_collector.py` 正被历史侧（日线/12h 全池补缺）
与 OI 任务使用，**不得同时编辑**；本模块自带取数、规范化、快照读写与路由处理函数，
主助手只需在既有分派处加**一行**即可接通（见文件末尾 `MINIMAL_WIRING`）。

═══════════════════════════════════════════════════════════════════════════════
真实数据源（**实测捕获，非猜测**；2026-10-07，无 cookie / 无凭据 直连 200）
═══════════════════════════════════════════════════════════════════════════════
来源 = **用户给定的币安公开页面自身 XHR**：
  `https://www.binance.bh/zh-CN/smart-money/signal/{SYMBOL}?timeRange=ALL&side=BOTH&...`

  GET https://www.binance.bh/bapi/futures/v1/public/future/smart-money/signal/overview
        ?symbol={S}[&timeRange=ALL]
  GET https://www.binance.bh/bapi/futures/v1/public/future/smart-money/signal/details/stats
        ?symbol={S}&timeRange=ALL
  GET https://www.binance.bh/bapi/futures/v1/public/future/smart-money/signal/details/list
        ?symbol={S}&timeRange=ALL&page=1&rows=10&sortingField=TIME&sortingOrder=DESC

  · 路径含 `/public/`，**无需登录、无鉴权、无验证码**；实测 `credentials:'omit'` 与 curl
    双重 200。**不读取**用户私有账户/持仓/订单，**不提取**任何凭证，**不绕过**任何访问控制。
  · `timeRange` 对 `details/*` 为**必填**（缺 ⇒ `code=000002` 参数非法）；页面口径为 `ALL`。
  · 主机：本机实测 `www.binance.com` 该 bapi **HTTP 000（不可达）**，`www.binance.bh` 200；
    两者同属币安官方域族，本模块固定使用**页面自身所用**的 `www.binance.bh`。

═══════════════════════════════════════════════════════════════════════════════
语义边界（**必须逐条如实标注，不得冒充**）
═══════════════════════════════════════════════════════════════════════════════
  1) 这是币安**自定义口径的「聪明钱 / Smart Signal」交易员队列**的**聚合持仓**，
     **不是全市场持仓**，**不是 OI**，**不是**大户多空比 / 账户数多空比 / taker 量比。
     队列成员如何界定（谁算「聪明钱」、鲸鱼门槛）**币安未公开** ⇒ 本模块标 `cohort_definition:"undisclosed"`。
  2) 「鲸鱼」= 该队列内**按保证金余额 Top 20%** 的子集（社区实现口径，**币安未官方确认**）
     ⇒ 标 `whale_definition:"community_claim_unverified"`。
  3) **三种「时间」严格分开，不得混用**（2026-10-08 更正）：
     · `trade_event_time`（源字段 `details/list[].lastTradeTime`）= **交易活动时间**，
       只说明该队列最近的成交/持仓变动活跃度；**不能**当作上游数据快照时点，
       也**不能**因某标的活动少（该值旧）就判它「数据陈旧」。
     · `upstream_snapshot_time` = 上游（币安）数据快照时点：币安**未公开** ⇒ 一律 `null`+原因，
       **不推断**。
     · `local_fetched_at` / `snapshot_refreshed_at` = 本机取回 / 落盘时点。
     · **陈旧（stale）判据只看本机取回时点**：采集停摆 ⇒ 该时点不再推进 ⇒ 判 stale；
       「交易少但本机刚成功取回」**不会**被误判陈旧。
  4) `updateTime`（overview）是**日级队列标记**（全标的共享、不随分钟推进）
     ⇒ 既非交易事件时间、也非上游快照时点，**不参与新鲜度判定**，仅原样留档。
  5) 金额口径：`*Positions` 为**按当前标记价折算的持仓市值（USDT）**，
     = `*Qty × markPrice`（本机实测：ARXUSDT 30325969 × 0.26904770 = 8,159,557 ≈ 8,159,781）
     ⇒ **不是**开仓成本额；开仓成本口径只有 `*AvgEntryPrice`（均价）。
  6) `longShortRatio` 是**数量比** `longQty / shortQty`（实测 30325969/2986022=10.156 ≈ 10.1574），
     **不是**人数比（166/102=1.63）⇒ 标 `basis:"qty"` + 显式分子分母。
  7) 缺字段一律 `null` + 具体原因（`null_reasons`），**不填 0、不用其它指标顶替**。

边界：不新建端口 / 库 / 表 / 采集进程；不落历史、不下单、不扩大到策略/回测；
不触碰历史全池下载与其断点/配置文件。
"""
import argparse
import json
import math
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
# W1（2026-10-16）：默认工程根改为**本工程推导**（原兜底 旧工作区路径（已删） 已删除）。
PROJECT_ROOT = os.environ.get("DATA_HUB_PROJECT", str(HERE.parent.parent))
# P3 切流（2026-10-13）：默认只读库 = 新热库 market_v2.sqlite3（旧库已退役）
DB_PATH = os.environ.get("MARKET_DB",
                         os.path.join(PROJECT_ROOT, "数据", "市场数据", "db",
                                      "market_v2.sqlite3"))
for _p in (str(PROJECT_ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 复用现有统一网关（导入即完成 gateway_ext 的端点登记）
from gateway_ext import gateway as _make_gateway          # noqa: E402
from gateway_ext import SMART_MONEY_HOST, SMART_MONEY_TTL_MS  # noqa: E402
from data_hub.gateway import (DeferredRequest, ENDPOINTS, Endpoint,  # noqa: E402
                              RejectedRequest)
from data_hub.store import MarketStore                     # noqa: E402

SNAP_DIR = Path(os.environ.get("SMART_MONEY_SNAPSHOT_DIR")
                or (HERE / "快照" / "smart_money"))
INDEX_NAME = "_index.json"
# 陈旧判据 = **本机取回时点**（`snapshot_refreshed_at`）距当前时刻超过阈值。
#   阈值 = 采集器聪明钱周期（`SMART_MONEY_INTERVAL_S`，默认 300s）× 2 = 600s。
#   ⚠ **不由「扫描用时」推导、不随 full_pool 放宽**：采集停摆后 ≤10 分钟即判 stale，
#     不会被大阈值掩盖（2026-10-08 更正：删除旧的 FULL_POOL 1800s 放宽）。
DEFAULT_STALE_AFTER_S = int(os.environ.get("SMART_MONEY_STALE_AFTER_S", "600"))
DEFAULT_TIME_RANGE = os.environ.get("SMART_MONEY_TIME_RANGE", "ALL")
DEFAULT_ROWS = int(os.environ.get("SMART_MONEY_ROWS", "10"))

# ── 轮内进度心跳（2026-10-08）─────────────────────────────────────────────────
# 修复「collector_liveness 只用整轮完成时间 ⇒ 长轮仍在推进却被报 stalled」：
# 采集器逐标推进时刷新 `_progress.json`；健康判据优先看它（区分「推进中 running」
# 与「真停摆 stalled」），索引 `updated_at` 只作为空闲期的回退信号。
PROGRESS_NAME = "_progress.json"
PROGRESS_STALE_S = int(os.environ.get("SMART_MONEY_PROGRESS_STALE_S", "300"))

# ── 页面「正在看」标的登记（2026-10-08）───────────────────────────────────────
# `v1_smart_money` 被前端读取时写入 `_wanted.json`（有界 + TTL，只写本机小文件，
# **不触发任何交易所请求**）；采集器每轮优先刷新这些标的（可见/展开币优先更新），
# 其余标的按持久游标公平轮询。两个集合都复用同一共享网关限频。
WANTED_NAME = "_wanted.json"
WANTED_MAX = int(os.environ.get("SMART_MONEY_WANTED_MAX", "48"))
WANTED_TTL_S = int(os.environ.get("SMART_MONEY_WANTED_TTL_S", "600"))

# `details/*` 允许的 timeRange（实测：ALL / 7D / 3D / 30D / 90D 通过；
# 1H/4H/12H/1D/14D/1W/1M 返回 code=000002 参数异常）
TIME_RANGES = ("ALL", "7D", "3D", "30D", "90D")
# 标的标识校验：**币安存在中文名合约**（实测 `哈基米USDT` / `币安人生USDT` / `牛来USDT` /
# `龙虾USDT` / `我踏马来了USDT` 该 bapi **均返回真实数据**），故**不能**限定 ASCII。
# 改为**黑名单**：拒绝路径分隔符、URL 保留字符、空白与控制字符（长度 ≤32）。
SYMBOL_BAD_RE = re.compile(r"[/\\.?&#%:*\"<>|\s\x00-\x1f\x7f]")


def valid_symbol(sym):
    s = str(sym or "")
    return bool(s) and len(s) <= 32 and not SYMBOL_BAD_RE.search(s)


# 兼容旧名（既有调用方可能引用）
def _valid_symbol(sym):
    return valid_symbol(sym)


SYMBOL_RE = re.compile(r".+", re.S)          # 仅占位，实际校验走 `valid_symbol()`

SOURCE_LABEL = ("币安公开页面 bapi（www.binance.bh/bapi/futures/v1/public/future/"
                "smart-money/signal/*，无鉴权只读）")

_SMART_MONEY_ADD = {
    "binance.smart_money_overview": Endpoint(
        SMART_MONEY_HOST, "/bapi/futures/v1/public/future/smart-money/signal/overview",
        frozenset({"symbol", "timeRange"}), frozenset({"symbol"}), SMART_MONEY_TTL_MS),
    "binance.smart_money_stats": Endpoint(
        SMART_MONEY_HOST, "/bapi/futures/v1/public/future/smart-money/signal/details/stats",
        frozenset({"symbol", "timeRange"}), frozenset({"symbol", "timeRange"}),
        SMART_MONEY_TTL_MS),
    "binance.smart_money_details": Endpoint(
        SMART_MONEY_HOST, "/bapi/futures/v1/public/future/smart-money/signal/details/list",
        frozenset({"symbol", "timeRange", "page", "rows", "sortingField", "sortingOrder"}),
        frozenset({"symbol", "timeRange"}), SMART_MONEY_TTL_MS),
    "binance.smart_money_list": Endpoint(
        SMART_MONEY_HOST, "/bapi/futures/v1/public/future/smart-money/signal/list",
        frozenset({"symbol", "timeRange", "side", "sortBy", "sortOrder", "page"}),
        frozenset({"symbol", "timeRange"}), SMART_MONEY_TTL_MS),
}
for _name, _spec in _SMART_MONEY_ADD.items():
    ENDPOINTS.setdefault(_name, _spec)

_LOCK = threading.Lock()
_GW = None
_STORE = None


def _gw():
    """惰性构造网关（与现有 REST 工作器共用同一 store/预算/冷却/封禁纪律）。"""
    global _GW, _STORE
    if _GW is None:
        with _LOCK:
            if _GW is None:
                _STORE = MarketStore(DB_PATH)
                _GW = _make_gateway(_STORE)
    return _GW


def iso(ms):
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return None


def now_ms():
    return int(time.time() * 1000)


def _num(v):
    """严格数值化：非有限数一律 None（**不把脏值当 0**）。"""
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pick(*vals):
    for v in vals:
        if v is not None:
            return v
    return None


# 文件名安全化：**只**替换路径/控制字符，保留 Unicode（否则中文名合约会互相覆盖）
_FILE_BAD_RE = re.compile(r"[/\\\x00-\x1f\x7f]")


def _safe(text):
    return _FILE_BAD_RE.sub("_", str(text or ""))[:64]


# ── 数值字段白名单（统一网关用 `parse_float=str` 保留**精确十进制文本**，
#    这里按字段名**显式**转成 JSON number，供前端直接消费）。
#    ⚠ 2026-10-13 变更（候选 A 瘦身）：`api_calls.response_json` 已对 939,328 行置 NULL，
#    整行、`response_sha256`、`response_headers_json`、时间线与状态**全部保留**，
#    并新增 `response_bytes` = 原始正文的**字节数**（供 `_call_sha()` 一类校验）。
#    因此**精确原始文本不再在库内**。需要时取回滚备份归档，按 `raw_refs[].call_id`
#    对应备份内的 id：
#      `网站逆向研究/K线瘦身盘点_2026-10-12/候选A_清空response_json/A1_backup.jsonl.gz`
_INT_FIELDS = frozenset({
    "totalTraders", "longTraders", "longWhales", "shortTraders", "shortWhales",
    "longProfitTraders", "shortProfitTraders", "longProfitWhales", "shortProfitWhales",
    "updateTime", "lastTradeTime", "userType", "traders", "total",
})
_OVERVIEW_NUM = frozenset({
    "totalPositions", "totalTraders", "longShortRatio", "longTraders", "longTradersQty",
    "longTradersAvgEntryPrice", "longWhales", "longWhalesQty", "longWhalesAvgEntryPrice",
    "shortTraders", "shortTradersQty", "shortTradersAvgEntryPrice", "shortWhales",
    "shortWhalesQty", "shortWhalesAvgEntryPrice", "longProfitTraders", "shortProfitTraders",
    "longProfitWhales", "shortProfitWhales", "updateTime",
})
_STATS_NUM = frozenset({
    "longTraders", "longWhales", "longPositions", "longQty", "longWhalePositions",
    "longWhaleQty", "shortTraders", "shortWhales", "shortPositions", "shortQty",
    "shortWhalePositions", "shortWhaleQty",
})
_DETAIL_NUM = frozenset({
    "lastTradeTime", "userType", "traders", "currentPositions", "currentQty", "avgEntryPrice",
})
# 身份类字段**必须保持字符串**（大整数 id 超 JSON 安全整数范围，转 float 会失真）
_STRICT_STR = frozenset({"topTraderId", "traderName", "symbol", "side", "timeRange"})


def _coerce(rec, num_fields):
    """按字段名把精确十进制文本转成 JSON number；未列出的字段原样保留。"""
    if not isinstance(rec, dict):
        return rec
    out = {}
    for k, v in rec.items():
        if k in num_fields and k not in _STRICT_STR:
            n = _num(v)
            if n is not None:
                out[k] = int(n) if (k in _INT_FIELDS and float(n).is_integer()) else n
                continue
        out[k] = v
    return out


# ────────────────────────────── 取数（经统一网关）──────────────────────────────

def _gw_get(name, params, attempts=3, cap_s=3.0, gw=None):
    """统一网关取数（**遵守网关自己的限频/预算/冷却/封禁**，不绕过任何保护）。

    与 `public_market_read._gw_get` 同语义：`DeferredRequest` 按网关给出的最早重试时间等待，
    上限 `cap_s` 秒、最多 `attempts` 次；预算耗尽或来源被封时如实返回失败原因。
    返回 `(data, origin, got_ms, call_id, err)`。
    """
    gate = gw or _gw()
    last = None
    for i in range(attempts):
        try:
            res = gate.get(name, params)
            return (res.get("数据"), res.get("来源"), res.get("获取时间UTC毫秒"),
                    res.get("请求编号"), None)
        except DeferredRequest as exc:
            last = f"{type(exc).__name__}: {exc}"
            if i == attempts - 1:
                break
            wait = (exc.retry_at - gate.store.clock()) / 1000.0
            time.sleep(max(0.0, min(wait, cap_s)) + 0.05)
        except RejectedRequest as exc:
            return None, None, None, None, f"{type(exc).__name__}: {exc}"
        except Exception as exc:                                   # noqa: BLE001
            return None, None, None, None, f"{type(exc).__name__}: {exc}"
    return None, None, None, None, last


def _envelope_data(body):
    """解币安 bapi 信封：成功 `code=000000`；`data:null` = 该合约**无覆盖**（不是错误）。"""
    if not isinstance(body, dict):
        return None, "上游未返回 JSON 对象"
    code = body.get("code")
    if code not in (None, "000000", "0"):
        return None, f"上游业务码 {code}：{body.get('message') or '未给出原因'}"
    return body.get("data"), None


def _call_sha(store, call_id):
    """读该次请求在**共享账本**中记录的正文 SHA256（取证用；只读）。"""
    if not call_id:
        return None
    try:
        with store.transaction() as con:
            row = con.execute("SELECT response_sha256, response_json, http_status "
                              "FROM api_calls WHERE id=?", (call_id,)).fetchone()
    except Exception:                                              # noqa: BLE001
        return None
    if not row:
        return None
    raw = row["response_json"]
    return {"call_id": call_id, "http_status": row["http_status"],
            "sha256": row["response_sha256"], "bytes": (len(raw.encode("utf-8")) if raw else None)}


def fetch_smart_money(exchange, market_id, time_range=DEFAULT_TIME_RANGE, gw=None,
                      rows=DEFAULT_ROWS, with_details=True, attempts=3, cap_s=3.0):
    """取某一合约的币安聪明钱并**逐字段**规范化。**只读、不落库**。

    返回体同时兼容：① 前端既有契约（`overview` / `stats` 原样透传）；
    ② 逐字段核验槽（`long` / `short` / `totals` / `ratios` + `null_reasons`）。

    `attempts` / `cap_s` 透传给统一网关取数：采集器长轮用**更耐心的等待**
    （本地节流 DeferredRequest 变成等待而非失败），页面侧路径不受影响。
    """
    out = {
        "available": False, "reason": None, "source": SOURCE_LABEL,
        "exchange": exchange, "market_id": market_id, "symbol": market_id,
        "time_range": time_range,
        "overview": None, "stats": None, "details": None, "details_total": None,
        "update_time_ms": None, "update_time": None,
        # ① 交易活动时间（源字段 lastTradeTime）——只说明活跃度，非陈旧判据
        "last_trade_time_ms": None, "last_trade_time": None,
        "trade_event_time_ms": None, "trade_event_time": None,
        # ② 上游数据快照时点（币安未公开 ⇒ null + 原因，不推断）
        "upstream_snapshot_time_ms": None, "upstream_snapshot_time": None,
        # ③ 本机取回 / 落盘时点（**陈旧判据只用这一组**）
        "local_fetched_at": None, "local_fetched_at_ms": None,
        "fetched_at": None, "fetched_at_ms": None,
        "time_semantics": None,
        "long": None, "short": None, "totals": None, "ratios": None,
        "null_reasons": {}, "raw_refs": [], "source_urls": [],
        "cohort_definition": "undisclosed（币安未公开「聪明钱」队列的成员判定规则）",
        "whale_definition": "community_claim_unverified（社区实现称「保证金余额 Top 20%」，币安未确认）",
        "not_a_substitute_for": ["全市场持仓", "openInterest", "大户/账户数多空比", "taker 买卖量比"],
    }
    if str(exchange).lower() != "binance":
        out["reason"] = ("币安聪明钱接口只覆盖**币安 USD-M 合约**；"
                         "该行非币安标的，无对应来源 ⇒ 保持缺失（不猜、不以其它指标替代）")
        out["quality"] = {"status": "missing", "age_seconds": None, "note": out["reason"]}
        return out
    if not valid_symbol(market_id):
        out["reason"] = (f"标的标识不合法：{market_id!r}"
                         "（拒绝路径分隔符/URL 保留字符/空白，长度 ≤32；"
                         "币安中文名合约**允许**）")
        out["quality"] = {"status": "missing", "age_seconds": None, "note": out["reason"]}
        return out
    if time_range not in TIME_RANGES:
        out["reason"] = (f"timeRange={time_range!r} 非实测可用取值 {TIME_RANGES}"
                         "（其余取值币安返回 code=000002 参数非法）")
        out["quality"] = {"status": "missing", "age_seconds": None, "note": out["reason"]}
        return out

    base = "https://" + SMART_MONEY_HOST + "/bapi/futures/v1/public/future/smart-money/signal"
    sym = str(market_id)

    # ① overview：17 字段（人数 / 数量 / 均价 / 鲸鱼 / 浮盈数 / updateTime）
    ov_body, ov_origin, ov_ms, ov_id, ov_err = _gw_get(
        "binance.smart_money_overview", {"symbol": sym, "timeRange": time_range},
        gw=gw, attempts=attempts, cap_s=cap_s)
    ov_data, ov_env_err = (None, ov_err) if ov_err else _envelope_data(ov_body)
    if ov_id:
        out["raw_refs"].append({"endpoint": "overview", "call_id": ov_id,
                                "origin": ov_origin, "fetched_at": iso(ov_ms)})
    out["source_urls"].append(f"{base}/overview?symbol={sym}&timeRange={time_range}")

    # ② details/stats：逐侧**当前持仓额(USDT)** + 数量 + 鲸鱼仓位（六槽「持仓额/占比」真源）
    st_body, st_origin, st_ms, st_id, st_err = _gw_get(
        "binance.smart_money_stats", {"symbol": sym, "timeRange": time_range},
        gw=gw, attempts=attempts, cap_s=cap_s)
    st_data, st_env_err = (None, st_err) if st_err else _envelope_data(st_body)
    if st_id:
        out["raw_refs"].append({"endpoint": "details/stats", "call_id": st_id,
                                "origin": st_origin, "fetched_at": iso(st_ms)})
    out["source_urls"].append(f"{base}/details/stats?symbol={sym}&timeRange={time_range}")

    # ③ details/list：聚合行 + 具名交易员行；`lastTradeTime` = **真实新鲜度**
    dl_data, dl_total = None, None
    if with_details:
        dl_body, dl_origin, dl_ms, dl_id, dl_err = _gw_get(
            "binance.smart_money_details",
            {"symbol": sym, "timeRange": time_range, "page": 1, "rows": rows,
             "sortingField": "TIME", "sortingOrder": "DESC"},
            gw=gw, attempts=attempts, cap_s=cap_s)
        dl_data, dl_env_err = (None, dl_err) if dl_err else _envelope_data(dl_body)
        if dl_id:
            out["raw_refs"].append({"endpoint": "details/list", "call_id": dl_id,
                                    "origin": dl_origin, "fetched_at": iso(dl_ms)})
        out["source_urls"].append(
            f"{base}/details/list?symbol={sym}&timeRange={time_range}&page=1&rows={rows}"
            "&sortingField=TIME&sortingOrder=DESC")
        if isinstance(dl_body, dict) and isinstance(dl_body.get("total"), int):
            dl_total = dl_body["total"]

    out["overview"] = _coerce(ov_data, _OVERVIEW_NUM) if isinstance(ov_data, dict) else None
    out["stats"] = _coerce(st_data, _STATS_NUM) if isinstance(st_data, dict) else None
    out["details"] = ([_coerce(r, _DETAIL_NUM) for r in dl_data]
                      if isinstance(dl_data, list) else None)
    out["details_total"] = dl_total
    out["fetched_at_ms"] = now_ms()
    out["fetched_at"] = iso(out["fetched_at_ms"])
    out["local_fetched_at_ms"] = out["fetched_at_ms"]
    out["local_fetched_at"] = out["fetched_at"]
    out["time_semantics"] = {
        "trade_event_time": ("details/list[].lastTradeTime —— **交易活动时间**"
                             "（仅说明该队列交易/持仓变动活跃度）"),
        "upstream_snapshot_time": "币安**未公开**上游数据快照时点 ⇒ 恒 null（不推断）",
        "local_fetched_at": "本机取回该合约数据的时刻（**陈旧判据只用本机取回时点**）",
        "updateTime": "overview.updateTime —— 日级队列标记，**不参与**新鲜度判定",
    }

    ov, st = out["overview"], out["stats"]
    if ov is None and st is None:
        out["reason"] = ("币安聪明钱接口对该合约返回 `data:null`（**该合约无聪明钱覆盖**，"
                         "不是错误）；不填 0、不用其它指标顶替。"
                         + (f"（overview: {ov_env_err}）" if ov_env_err else "")
                         + (f"（stats: {st_env_err}）" if st_env_err else ""))
        out["quality"] = {"status": "missing", "age_seconds": None, "note": out["reason"]}
        return out

    # ── 更新时间与新鲜度 ────────────────────────────────────────────────────
    if isinstance(ov, dict) and _num(ov.get("updateTime")) is not None:
        out["update_time_ms"] = int(_num(ov["updateTime"]))
        out["update_time"] = iso(out["update_time_ms"])
    agg = [r for r in (out["details"] or [])
           if isinstance(r, dict) and r.get("userType") == 2]
    named = [r for r in (out["details"] or [])
             if isinstance(r, dict) and r.get("userType") != 2]
    pool = agg or [r for r in (out["details"] or []) if isinstance(r, dict)]
    times = [int(_num(r.get("lastTradeTime"))) for r in pool
             if _num(r.get("lastTradeTime")) is not None]
    if times:
        out["last_trade_time_ms"] = max(times)
        out["last_trade_time"] = iso(out["last_trade_time_ms"])
        # 交易活动时间 = 源字段 lastTradeTime（**不是**上游快照时点，不用于陈旧判定）
        out["trade_event_time_ms"] = out["last_trade_time_ms"]
        out["trade_event_time"] = out["last_trade_time"]
    out["details_split"] = {
        "aggregate_rows": len(agg), "named_trader_rows": len(named),
        "note": ("聚合行 userType=2 = 该队列整侧汇总；具名行 userType=0 = 单个交易员"
                 "（含 traderName / topTraderId）。具名行是**样本个体**，不代表全市场。"),
    }

    out["long"] = _side_block("long", ov, st)
    out["short"] = _side_block("short", ov, st)
    out["totals"] = {
        "positions_usd": _pick(_num((st or {}).get("longPositions")), _num((st or {}).get("shortPositions"))),
        "total_positions_usd": _num((ov or {}).get("totalPositions")),
        "total_traders": _num((ov or {}).get("totalTraders")),
        "positions_ccy": "USDT",
        "positions_basis": ("按当前标记价折算的持仓市值 = *Qty × markPrice（非开仓成本额）"),
    }
    out["totals"]["positions_usd"] = _pick(
        _sum2(_num((st or {}).get("longPositions")), _num((st or {}).get("shortPositions"))),
        _num((ov or {}).get("totalPositions")))
    out["ratios"] = _ratios(ov, st, out["long"], out["short"])
    out["available"] = True
    out["reason"] = None
    out["quality"] = _freshness(out)
    out["null_reasons"] = _null_reasons(out)
    return out


def _sum2(a, b):
    if a is None or b is None:
        return None
    return a + b


def _side_block(side, ov, st):
    """逐侧逐字段：持仓额及币种 / 内部占比及分母 / 均价及口径 / 身份 / 浮盈。"""
    pre = "long" if side == "long" else "short"
    lbl = "多" if side == "long" else "空"
    ov = ov or {}
    st = st or {}
    pos = _num(st.get(pre + "Positions"))
    wpos = _num(st.get(pre + "WhalePositions"))
    qty = _pick(_num(st.get(pre + "Qty")), _num(ov.get(pre + "TradersQty")))
    wqty = _pick(_num(st.get(pre + "WhaleQty")), _num(ov.get(pre + "WhalesQty")))
    traders = _pick(_num(st.get(pre + "Traders")), _num(ov.get(pre + "Traders")))
    whales = _pick(_num(st.get(pre + "Whales")), _num(ov.get(pre + "Whales")))
    avg = _num(ov.get(pre + "TradersAvgEntryPrice"))
    wavg = _num(ov.get(pre + "WhalesAvgEntryPrice"))
    profit_t = _num(ov.get(pre + "ProfitTraders"))
    profit_w = _num(ov.get(pre + "ProfitWhales"))
    missing = []
    if pos is None:
        missing.append("stats." + pre + "Positions")
    if avg is None:
        missing.append("overview." + pre + "TradersAvgEntryPrice")
    return {
        "side": side, "label": lbl,
        # ① 持仓金额及币种（真实金额，非数量）
        "positions_usd": pos, "positions_ccy": "USDT",
        "positions_basis": ("当前持仓市值 = Qty × 当前标记价（USDT）；非开仓成本额"),
        "positions_source": "details/stats." + pre + "Positions",
        # 数量（标的币）—— **绝不写入金额槽**
        "qty": qty, "qty_ccy": (str(ov.get("symbol") or "")[: -len("USDT")] or None),
        "qty_source": "details/stats." + pre + "Qty（回退 overview." + pre + "TradersQty）",
        # ② 内部占比及分母
        "whale_positions_usd": wpos,
        "whale_share_percent": _pct(wpos, pos),
        "whale_share_denominator": {"field": "details/stats." + pre + "Positions",
                                    "value": pos, "ccy": "USDT"},
        "whale_traders": whales,
        "whale_traders_share_percent": _pct(whales, traders),
        "whale_traders_share_denominator": {"field": "details/stats." + pre + "Traders",
                                            "value": traders, "unit": "人"},
        # ③ 持仓均价及计算口径/时间
        "avg_entry_price": avg, "avg_entry_price_ccy": "USDT/" + (str(ov.get("symbol") or "")[: -len("USDT")] or "标的币"),
        "avg_entry_price_basis": ("该侧**交易员**（含鲸鱼）的平均开仓价；币安未公开加权方式"
                                  "（简单/数量加权未知）"),
        "avg_entry_price_source": "overview." + pre + "TradersAvgEntryPrice",
        "whale_avg_entry_price": wavg,
        "whale_avg_entry_price_source": "overview." + pre + "WhalesAvgEntryPrice",
        # 浮盈（币安给出的真实计数，非推算）
        "profit_traders": profit_t, "profit_traders_share_percent": _pct(profit_t, traders),
        "profit_whales": profit_w, "profit_whales_share_percent": _pct(profit_w, whales),
        # 身份
        "traders": traders, "whales": whales,
        "identity_note": ("队列成员身份**币安未公开**；具名个体见 details 中 userType=0 行"),
        "missing_fields": missing,
    }


def _pct(num, den):
    """占比：分母缺失或为 0 ⇒ **null**（不显示、不填 0）。"""
    if num is None or den is None or den == 0:
        return None
    return round(num / den * 100, 1)


def _ratios(ov, st, lng, sht):
    ov = ov or {}
    lq, sq = lng.get("qty"), sht.get("qty")
    lsr = _num(ov.get("longShortRatio"))
    recomputed = None
    if lq is not None and sq:
        recomputed = round(lq / sq, 4)
    return {
        "long_short_ratio": {
            "value": lsr, "basis": "qty",
            "numerator": {"field": "overview.longTradersQty", "value": lq},
            "denominator": {"field": "overview.shortTradersQty", "value": sq},
            "recomputed_from_qty": recomputed,
            "note": ("币安字段 `longShortRatio` 实测 = **多头数量 / 空头数量**（非人数比）。"
                     "ARXUSDT 实测 30326158/2985634=10.157 ≈ 字段 10.1574；"
                     "而人数比 167/101=1.65，故**不是**人数比。"),
        },
        "traders_long_short_ratio": {
            "value": (round(lng["traders"] / sht["traders"], 4)
                      if lng.get("traders") and sht.get("traders") else None),
            "basis": "count",
            "numerator": {"field": "overview.longTraders", "value": lng.get("traders")},
            "denominator": {"field": "overview.shortTraders", "value": sht.get("traders")},
            "note": "本模块**自行命名**的人数比（与币安 longShortRatio 不同口径，不混用）",
        },
    }


def _null_reasons(out):
    """逐条列出为 null 的关键字段及**具体原因**（不静默省略）。"""
    reasons = {}
    for side in ("long", "short"):
        blk = out.get(side) or {}
        lbl = blk.get("label") or side
        if blk.get("positions_usd") is None:
            reasons[f"{side}.positions_usd"] = (
                "details/stats 未返回该侧 *Positions（该合约无聪明钱覆盖时 data 为 null）")
        if blk.get("avg_entry_price") is None:
            reasons[f"{side}.avg_entry_price"] = (
                "overview 未返回该侧 *TradersAvgEntryPrice")
        if blk.get("whale_share_percent") is None and blk.get("positions_usd") is not None:
            reasons[f"{side}.whale_share_percent"] = (
                "分母（该侧总持仓额）缺失或为 0 ⇒ 不显示占比、不填 0")
        if blk.get("qty") is None:
            reasons[f"{side}.qty"] = "details/stats 与 overview 均未返回该侧数量"
        _ = lbl
    if out.get("update_time_ms") is None:
        reasons["update_time_ms"] = "overview 未返回 updateTime"
    if out.get("trade_event_time_ms") is None:
        reasons["trade_event_time_ms"] = ("details/list 未返回可用 lastTradeTime"
                                         "（该合约无明细行）⇒ 交易活动时间未知"
                                         "（**不影响**以本机取回时点判定的新鲜度）")
    return reasons


def _freshness(payload, now=None, stale_after_s=None):
    """按**本机取回时点**判定新鲜度（**不用** lastTradeTime / updateTime）。

    · 判据 = `snapshot_refreshed_at_ms`（落盘时点）→ 回退 `local_fetched_at_ms` / `fetched_at_ms`；
      ⇒ 采集停摆时该时点不再推进 ⇒ 判 `stale`；
      「交易活动少（lastTradeTime 旧）但本机刚成功取回」判 `ok`（**不误判**）。
    · `trade_event_time`（lastTradeTime）与 `updateTime` 只随附展示，**不参与**判定。
    """
    now = now_ms() if now is None else now
    limit = DEFAULT_STALE_AFTER_S if stale_after_s is None else stale_after_s
    ts = (payload.get("snapshot_refreshed_at_ms")
          or payload.get("local_fetched_at_ms")
          or payload.get("fetched_at_ms"))
    basis = ("本机快照落盘时点 snapshot_refreshed_at"
             if payload.get("snapshot_refreshed_at_ms") else "本机取回时点 local_fetched_at")
    trade_ev = payload.get("trade_event_time_ms") or payload.get("last_trade_time_ms")
    base = {
        "basis": basis, "stale_after_s": limit,
        "local_fetched_at": iso(ts),
        "trade_event_time": iso(trade_ev),
        "upstream_snapshot_time": payload.get("upstream_snapshot_time"),
        "upstream_snapshot_time_note": ("币安未公开上游数据快照时点 ⇒ null；"
                                        "本模块不据此判断新鲜度"),
    }
    if ts is None:
        base.update({"status": "unknown", "age_seconds": None,
                     "note": "无本机取回时点，无法判定新鲜度"})
        return base
    age = max(0.0, round((now - int(ts)) / 1000.0, 1))
    status = "ok" if age <= limit else "stale"
    note = (f"本机取回 {iso(ts)}，{age:.0f}s 前，阈值 {limit}s"
            f"（=采集周期×2，**不随扫描用时放宽**）"
            + (f"；交易活动时间 {iso(trade_ev)}（仅活跃度，**非**陈旧判据）" if trade_ev else "")
            + ("；`overview.updateTime` 为日级队列标记，不参与新鲜度判定"
               if payload.get("update_time_ms") is not None else ""))
    base.update({"status": status, "age_seconds": age, "note": note,
                 "event_time": iso(ts)})
    return base


def _collector_liveness(idx, now=None):
    """采集器**推进信号**（与逐标的新鲜度**分开报告**）。

    2026-10-08 修复「长轮仍在推进却被报 stalled」：优先读**轮内进度心跳**
    `_progress.json`（逐标推进时刷新，含 phase/done/total/current）——
    心跳新鲜 ⇒ `running`（本轮正在推进，**不是**停摆）；心跳缺失（旧版采集器）
    或已停 ⇒ 回退「最近一轮索引 `updated_at` 年龄 ≤600s ⇒ `ok`」；
    两者都超阈值才判 `stalled`（真停摆）。进程存活、轮次推进、单币快照时效
    三个信号从此分开。
    """
    now = now_ms() if now is None else now
    prog = read_progress()
    hb_ms = int(prog.get("updated_at_ms") or 0) if prog else 0
    hb_age = (max(0.0, round((now - hb_ms) / 1000.0, 1)) if hb_ms else None)
    ts = idx.get("updated_at_ms")
    round_age = (max(0.0, round((now - int(ts)) / 1000.0, 1)) if ts else None)
    prog_brief = ({"phase": prog.get("phase"), "done": prog.get("done"),
                   "total": prog.get("total"), "current": prog.get("current"),
                   "pid": prog.get("pid")} if prog else None)
    if hb_age is not None and hb_age <= PROGRESS_STALE_S:
        status = "running"
        note = (f"采集轮内推进中：心跳 {hb_age:.0f}s 前"
                f"（{prog.get('phase')} {prog.get('done')}/{prog.get('total')}，"
                f"当前 {prog.get('current')}）；进程存活与轮次推进分开报告")
    elif round_age is not None and round_age <= DEFAULT_STALE_AFTER_S:
        status = "ok"
        note = ("最近一轮采集完成时点在阈值内（当前处于轮间休息）；"
                "用于区分「采集停摆」与「该标的未被覆盖」")
    elif hb_age is not None or round_age is not None:
        status = "stalled"
        note = (f"心跳{'缺失' if hb_age is None else f'{hb_age:.0f}s 前'}且"
                f"最近一轮{'未知' if round_age is None else f'{round_age:.0f}s 前'}——"
                f"两者均超阈值，判定采集推进停摆")
    else:
        status = "unknown"
        note = "无进度心跳且索引无 updated_at ⇒ 未知"
    return {"status": status, "heartbeat_age_seconds": hb_age,
            "progress": prog_brief,
            "last_round_at": idx.get("updated_at"),
            "last_round_age_seconds": round_age,
            "progress_stale_after_s": PROGRESS_STALE_S,
            "note": note}


# ────────────────────────────── 本机快照（纯文件，无网络）──────────────────────

def _snap_path(exchange, market_id):
    return SNAP_DIR / f"{_safe(exchange)}__{_safe(market_id)}.json"


def _atomic_write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def write(exchange, market_id, payload, refreshed_at_ms=None):
    ts = int(refreshed_at_ms if refreshed_at_ms is not None else now_ms())
    body = dict(payload or {})
    body["snapshot_refreshed_at_ms"] = ts
    body["snapshot_refreshed_at"] = iso(ts)
    body["snapshot_exchange"] = exchange
    body["snapshot_market_id"] = market_id
    path = _snap_path(exchange, market_id)
    _atomic_write(path, body)
    return path


def read(exchange, market_id):
    try:
        with open(_snap_path(exchange, market_id), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def list_symbols():
    out = []
    try:
        names = sorted(os.listdir(SNAP_DIR))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json") or name == INDEX_NAME or "__" not in name[:-5]:
            continue
        ex, mid = name[:-5].split("__", 1)
        out.append((ex, mid))
    return out


def write_index(stats):
    body = dict(stats or {})
    body.setdefault("updated_at_ms", now_ms())
    body.setdefault("updated_at", iso(body["updated_at_ms"]))
    _atomic_write(SNAP_DIR / INDEX_NAME, body)
    return body


def read_index():
    try:
        with open(SNAP_DIR / INDEX_NAME, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ── 轮内进度心跳与「页面正在看」登记（本机小文件，均有界）─────────────────────

def write_progress(phase, done, total, current=None, pid=None):
    """采集器轮内**逐标推进心跳**（`_collector_liveness` 由此区分 running/stalled）。"""
    ts = now_ms()
    body = {"phase": phase, "done": int(done), "total": int(total),
            "current": current, "pid": int(pid or os.getpid()),
            "updated_at_ms": ts, "updated_at": iso(ts)}
    try:
        _atomic_write(SNAP_DIR / PROGRESS_NAME, body)
    except OSError:
        pass
    return body


def read_progress():
    try:
        with open(SNAP_DIR / PROGRESS_NAME, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def note_wanted(exchange, market_id):
    """登记「页面正在看」的标的（`v1_smart_money` 读取时调用；**不触发任何交易所请求**）。

    有界（FIFO ≤ WANTED_MAX）+ TTL（WANTED_TTL_S）+ 去重；采集器每轮读它做
    **优先刷新**（可见/展开币优先更新），其余标的仍按公平轮询推进。
    """
    ts = now_ms()
    with _LOCK:
        try:
            with open(SNAP_DIR / WANTED_NAME, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        items = [x for x in (data.get("items") or [])
                 if isinstance(x, dict) and x.get("ex") and x.get("mid")
                 and ts - int(x.get("at") or 0) < WANTED_TTL_S * 1000]
        items = [x for x in items
                 if not (x.get("ex") == exchange and x.get("mid") == market_id)]
        items.append({"ex": str(exchange), "mid": str(market_id), "at": ts})
        items = items[-WANTED_MAX:]
        try:
            _atomic_write(SNAP_DIR / WANTED_NAME, {"items": items})
        except OSError:
            pass


def read_wanted():
    """采集器侧读取优先集合（过期项在 `note_wanted` 写入时自然淘汰）。"""
    try:
        with open(SNAP_DIR / WANTED_NAME, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    cutoff = now_ms() - WANTED_TTL_S * 1000
    out = []
    for x in (data.get("items") or []):
        if isinstance(x, dict) and int(x.get("at") or 0) >= cutoff:
            out.append((str(x.get("ex")), str(x.get("mid"))))
    return out


class _CountingGateway:
    """给注入的网关套一层**调用计数**（只计数，不改行为）——供验收给真实上游/缓存数。

    2026-10-08：`refresh_snapshot` 支持有界并发后，计数加锁（网关本身线程安全，
    这里只保证计数不丢）。
    """

    def __init__(self, gw):
        self._gw = gw
        self._lock = threading.Lock()
        self.attempts = self.upstream = self.cached = 0
        self.deferred = self.rejected = self.failed = 0

    def get(self, name, params=None):
        with self._lock:
            self.attempts += 1
        try:
            res = self._gw.get(name, params)
        except DeferredRequest:
            with self._lock:
                self.deferred += 1
            raise
        except RejectedRequest:
            with self._lock:
                self.rejected += 1
            raise
        except Exception:
            with self._lock:
                self.failed += 1
            raise
        origin = (res or {}).get("来源")
        with self._lock:
            if origin == "公共REST":
                self.upstream += 1
            elif origin == "共享缓存":
                self.cached += 1
        return res

    def __getattr__(self, key):
        return getattr(self._gw, key)

    def stats(self):
        return {"attempts": self.attempts, "upstream": self.upstream, "cached": self.cached,
                "deferred": self.deferred, "rejected": self.rejected, "failed": self.failed}


def _merge_preserve_details(payload, prev):
    """主槽 `with_details=False` 复刷时，**保留上一份快照的明细与交易活动时间**。

    明细（details/list）降频后不再随每轮覆盖；旧明细必须显式带上「截至时点」，
    不能让降频后的 null 冒充「该合约无明细」，也不能让旧明细冒充新取。
    """
    if not isinstance(prev, dict):
        return
    for key in ("details", "details_total", "details_split"):
        if payload.get(key) is None and prev.get(key) is not None:
            payload[key] = prev.get(key)
            payload["details_as_of"] = prev.get("snapshot_refreshed_at")
    if payload.get("last_trade_time_ms") is None and prev.get("last_trade_time_ms") is not None:
        payload["last_trade_time_ms"] = prev.get("last_trade_time_ms")
        payload["last_trade_time"] = prev.get("last_trade_time")
        payload["trade_event_time_ms"] = prev.get("trade_event_time_ms")
        payload["trade_event_time"] = prev.get("trade_event_time")
        payload["details_as_of"] = prev.get("snapshot_refreshed_at")


def refresh_snapshot(gw, symbols, time_range=DEFAULT_TIME_RANGE, extra=None,
                     with_details=True, max_workers=1, progress_cb=None,
                     deadline_s=None, attempts=3, cap_s=3.0):
    """**采集器侧**周期刷新：把有界标的集合的聪明钱写入本机可读快照。

    · **复用既有网关**（同一 `RestGateway(PublicGateway)`）⇒ 缓存/预算/租约/冷却/封禁共享；
    · **有界**：`symbols` 由调用方给有界集合，本函数不自行扩张；
    · 单标上游：`with_details=True` ≤3 次（overview+stats+details/list），
      `False` ≤2 次（overview+stats）——页面六槽只依赖前两者，明细降频（2026-10-08）；
    · `max_workers`：有界并发（共享同一网关，预算/冷却/封禁仍由网关统一执行）；
    · `deadline_s`：时间预算——超时后不再开新标的，`completed_all=False`、
      `completed_n` 返回实际推进数（调用方据此推进公平轮询游标，不饿死全池）；
    · `progress_cb(done, total, current, phase)`：逐标进度心跳（**真实线程推进**，
      修复「长轮被整轮完成时间误报 stalled」）；回调内部自行节流；
    · `attempts` / `cap_s`：网关取数耐心（采集长轮调大，DeferredRequest=等待而非失败）；
    · **只写快照**：不落库、不写历史、不新增进程/端口；
    · **失败不覆盖**：仅 `available:true` 才落盘；失败保留上一份好快照，
      由读取侧按写入时点判过期（不把失败静默写成空值）。
    """
    counted = _CountingGateway(gw)
    t0 = time.monotonic()
    deadline = (t0 + float(deadline_s)) if deadline_s else None
    items = []
    for item in symbols or ():
        if isinstance(item, (tuple, list)) and len(item) == 2:
            items.append((item[0], item[1]))
        else:
            items.append(("binance", item))
    total = len(items)
    phase = "details" if with_details else "main"
    ok = failed = skipped = 0
    failures = []
    skipped_symbols = []
    done_n = 0
    state_lock = threading.Lock()

    def _bump(item, payload=None, exc=None):
        nonlocal ok, failed, skipped, done_n
        ex, mid = item
        with state_lock:
            done_n += 1
            if exc is not None:
                failed += 1
                if len(failures) < 12:
                    failures.append(f"{ex}:{mid} → {type(exc).__name__}")
            elif payload is not None and payload.get("available"):
                ok += 1
            else:
                skipped += 1
                skipped_symbols.append(f"{ex}:{mid}")
                if len(failures) < 12:
                    failures.append(f"{ex}:{mid} → {((payload or {}).get('reason') or '未取到')[:60]}")
            n_done, n_total = done_n, total
        if progress_cb:
            try:
                progress_cb(n_done, n_total, f"{ex}:{mid}", phase)
            except Exception:                                      # noqa: BLE001
                pass

    def _work(item):
        ex, mid = item
        try:
            payload = fetch_smart_money(ex, mid, time_range=time_range, gw=counted,
                                        with_details=with_details,
                                        attempts=attempts, cap_s=cap_s)
        except Exception as exc:                                   # noqa: BLE001
            _bump(item, exc=exc)
            return
        # 只写快照、失败不覆盖；主槽降频时保留上一份明细（带截至时点，不冒充新取）。
        # write() 原子替换且每标的一个文件 ⇒ 并发安全。
        if payload.get("available"):
            if not with_details:
                _merge_preserve_details(payload, read(ex, mid))
            try:
                write(ex, mid, payload)
            except OSError as wexc:                                # noqa: PERF203
                _bump(item, exc=wexc)
                return
        _bump(item, payload=payload)

    def _pending():
        return (deadline is None) or (time.monotonic() < deadline)

    jobs = iter(items)
    if max_workers and max_workers > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = set()
            while True:
                nxt = next(jobs, None) if _pending() else None
                if nxt is None:
                    break
                futures.add(pool.submit(_work, nxt))
            for f in futures:
                f.result()
    else:
        while True:
            item = next(jobs, None) if _pending() else None
            if item is None:
                break
            _work(item)

    # 超时未开跑的标的如实报剩余（completed_all=False），游标推进由调用方用 completed_n。
    remaining = total - done_n
    elapsed = round(time.monotonic() - t0, 2)
    stats = {
        "targets": total, "ok": ok, "failed": failed, "skipped": skipped,
        # 本轮**已查询但上游 data:null** 的标的（当次无覆盖证据；不是永久结论）
        "skipped_symbols": skipped_symbols,
        "completed_n": done_n, "remaining_n": remaining,
        "completed_all": remaining == 0,
        "time_range": time_range,
        "mode": ("bapi（overview+details/stats+details/list，单标≤3次）" if with_details
                 else "bapi（overview+details/stats，单标≤2次；明细降频保留旧明细+截至时点）"),
        "max_workers": max(1, int(max_workers or 1)),
        "requests": counted.stats(), "elapsed_s": elapsed,
        "snapshot_dir": str(SNAP_DIR), "failures": failures,
        "source": SOURCE_LABEL,
        "note": ("快照由**现有采集工作器**周期写入；本机 API **只读快照**，"
                 "前端读取**不触发**任何交易所请求"),
    }
    if isinstance(extra, dict):
        stats.update(extra)
    try:
        write_index(stats)
    except OSError as exc:                                         # noqa: BLE001
        stats["index_error"] = f"{type(exc).__name__}: {exc}"
    return stats


def coverage():
    """本机快照覆盖（**不联网**）：落盘标的数 + 新鲜度分布 + 最近一轮采集索引。

    显式**分开**报告两个集合（口径不得混用）：
      · `symbols_on_disk`  = 快照目录内已落盘标的（含**历史**一次性刷新留下的）；
      · `continuous_set`   = **最近一轮采集实际刷新的集合**（`index.targets`），
        只有它才是「持续刷新集合」。
    """
    syms = list_symbols()
    ok = stale = unknown = 0
    ages = []
    for ex, mid in syms:
        p = read(ex, mid)
        if not p:
            continue
        fr = _freshness(p)
        if fr["status"] == "ok":
            ok += 1
        elif fr["status"] == "stale":
            stale += 1
        else:
            unknown += 1
        if fr["age_seconds"] is not None:
            ages.append(fr["age_seconds"])
    idx = read_index()
    return {"snapshot_dir": str(SNAP_DIR),
            "symbols_on_disk": len(syms),
            "freshness": {"ok": ok, "stale": stale, "unknown": unknown},
            "age_seconds": {"min": (min(ages) if ages else None),
                            "max": (max(ages) if ages else None)},
            "stale_after_s": DEFAULT_STALE_AFTER_S,
            "continuous_set": {
                "targets": idx.get("targets"), "ok": idx.get("ok"),
                "skipped": idx.get("skipped"), "failed": idx.get("failed"),
                "full_pool": bool(idx.get("full_pool")), "head_n": idx.get("head_n"),
                "interval_s": idx.get("interval_s"), "updated_at": idx.get("updated_at"),
                "elapsed_s": idx.get("elapsed_s"), "mode_run": idx.get("mode_run"),
            },
            "collector": _collector_liveness(idx),
            "index": idx}


# ─────────────────────── 路由处理（供 adapter 最小接线）───────────────────────

def v1_smart_money(exchange, mid, params=None):
    """`GET /api/v1/smart-money/{exchange}/{market_id}` —— **只读本机快照**。

    与 `public-extras` 同纪律：**不触发任何交易所请求**；快照缺失时如实返回 `available:false`
    与原因（并附采集器覆盖情况），由采集器周期刷新。
    响应体兼容前端既有契约：顶层 `overview` / `stats` 为**币安原始 `data` 原样透传**。

    2026-10-08 新增：
    · `missing_kind`（机器可读缺失分类：unsupported_exchange / invalid_symbol /
      no_coverage_null / not_covered_yet）——前端据此呈现「缺失/无覆盖/不支持」，
      不再把后端的明确结论当「取数失败」去走旁路；
    · 读取即登记 `note_wanted`（有界 TTL 队列），采集器对本标的**优先刷新**。
    """
    params = params or {}
    note_wanted(exchange, mid)
    idx = read_index()
    idx_brief = {"updated_at": idx.get("updated_at"), "updated_at_ms": idx.get("updated_at_ms"),
                 "targets": idx.get("targets"), "ok": idx.get("ok"),
                 "skipped": idx.get("skipped"), "failed": idx.get("failed"),
                 "time_range": idx.get("time_range"), "interval_s": idx.get("interval_s"),
                 "head_n": idx.get("head_n"),
                 "full_pool": bool(idx.get("full_pool")), "elapsed_s": idx.get("elapsed_s"),
                 "mode_run": idx.get("mode_run"),
                 "skipped_symbols": idx.get("skipped_symbols"),
                 "failures": idx.get("failures")}
    snap = read(exchange, mid)
    if str(exchange).lower() != "binance":
        reason = ("币安聪明钱接口只覆盖**币安 USD-M 合约**；该行非币安标的，"
                  "无对应来源 ⇒ 保持缺失（不猜、不以大户多空比/taker 量替代）")
        return {"available": False, "reason": reason, "exchange": exchange,
                "market_id": mid, "symbol": mid, "source": SOURCE_LABEL,
                "missing_kind": "unsupported_exchange",
                "overview": None, "stats": None, "details": None,
                "update_time_ms": None, "last_trade_time_ms": None, "fetched_at": None,
                "snapshot": {"refreshed_at": None, "refreshed_at_ms": None, "index": idx_brief},
                "quality": {"status": "missing", "age_seconds": None, "note": reason}}
    if snap is None:
        mode = "全池" if idx.get("full_pool") else "有界集合"
        skipped_hit = next((f for f in (idx.get("failures") or [])
                            if f.startswith(f"{exchange}:{mid} ")), None)
        if skipped_hit:
            missing_kind = "no_coverage_null"
            reason = (f"本轮已查询 {exchange}:{mid}，币安返回 `data:null`（**该次无覆盖**，"
                      f"不是永久结论、也不是错误）；不填 0、不用其它指标顶替。"
                      f"最近一轮目标 {idx.get('targets', '未知')} 个（{mode}）。")
        else:
            missing_kind = "not_covered_yet"
            reason = (f"本机聪明钱快照未覆盖 {exchange}:{mid}：采集器按**{mode}**周期刷新"
                      f"（最近一轮目标 {idx.get('targets', '未知')} 个）；本接口**只读快照、"
                      f"不触发任何交易所请求**。")
        return {"available": False, "reason": reason, "exchange": exchange,
                "market_id": mid, "symbol": mid, "source": SOURCE_LABEL,
                "missing_kind": missing_kind,
                "overview": None, "stats": None, "details": None,
                "update_time_ms": None, "last_trade_time_ms": None,
                "fetched_at": None,
                "snapshot": {"refreshed_at": None, "refreshed_at_ms": None,
                             "index": idx_brief, "collector": _collector_liveness(idx)},
                "quality": {"status": "missing", "age_seconds": None, "note": reason}}
    out = dict(snap)
    # 陈旧阈值**固定**（=采集周期×2，默认 600s），**不**随 full_pool / 扫描用时放宽。
    out["quality"] = _freshness(snap, stale_after_s=DEFAULT_STALE_AFTER_S)
    out["snapshot"] = {
        "refreshed_at": snap.get("snapshot_refreshed_at"),
        "refreshed_at_ms": snap.get("snapshot_refreshed_at_ms"),
        "index": idx_brief,
        "collector": _collector_liveness(idx),
        "written_by": "realtime_collector.smart_money_loop → smart_money.refresh_snapshot",
        "note": "本接口**只读本机快照**；前端读取**不触发**交易所请求",
    }
    return out


MINIMAL_WIRING = '''
# adapter.py 接线（唯一必要改动，共 2 行）：
# 1) 文件顶部（与既有 import 同处）：
import smart_money
# 2) route_v1() 中，与 `if seg == ["signals"]:` 并列加：
    if len(seg) == 3 and seg[0] == "smart-money":
        return smart_money.v1_smart_money(seg[1], seg[2], params), 200
'''


def _default_symbols(n):
    """命令行默认标的：币安页首字母序前 n（与工作台列表字典序一致）。"""
    try:
        pool = json.loads((HERE / "历史下载" / "标的池" / "binance_classified.json")
                          .read_text(encoding="utf-8"))
        coins = sorted(c["symbol"] for c in pool["coins"])
    except (OSError, ValueError, KeyError):
        coins = ["BTCUSDT", "ETHUSDT"]
    return coins[:max(1, n)]


def main(argv=None):
    ap = argparse.ArgumentParser(description="币安聪明钱公开只读接入（只写本机快照）")
    ap.add_argument("--symbols", default="", help="逗号分隔标的；缺省=页首前 N")
    ap.add_argument("--head-n", type=int, default=8, help="缺省标的数（页首字母序）")
    ap.add_argument("--time-range", default=DEFAULT_TIME_RANGE, choices=list(TIME_RANGES))
    ap.add_argument("--refresh", action="store_true", help="抓取并写本机快照")
    ap.add_argument("--once", action="store_true", help="单标的取数并打印（不写快照）")
    ap.add_argument("--coverage", action="store_true", help="打印本机快照覆盖（不联网）")
    ap.add_argument("--no-details", action="store_true", help="跳过 details/list（省 1 次上游）")
    a = ap.parse_args(argv)

    if a.coverage:
        print(json.dumps(coverage(), ensure_ascii=False, indent=1))
        return 0
    syms = ([s.strip().upper() for s in a.symbols.split(",") if s.strip()]
            or _default_symbols(a.head_n))
    if a.once:
        out = []
        for s in syms:
            out.append(fetch_smart_money("binance", s, time_range=a.time_range,
                                         with_details=not a.no_details))
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    stats = refresh_snapshot(_gw(), syms, time_range=a.time_range,
                             extra={"mode_run": "cli", "head_n": len(syms)},
                             with_details=not a.no_details)
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
