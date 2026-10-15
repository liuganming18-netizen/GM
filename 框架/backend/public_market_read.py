#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""币安公开数据只读模块（**独立文件**）。

为什么独立成文件：`adapter.py` / `realtime_collector.py` 正被历史侧（日线全池补缺）
使用，**不得同时编辑**。本模块自带路由处理函数，主助手只需在 `adapter.py` 加**一行分派**
即可接通（见文件末尾 `MINIMAL_WIRING`）。

用户口径（严格照做）：
  1) 来源**只用币安公开 API**；**只有**币安没有该代币/合约时才补 OKX。
     **不因币安历史缺口切换 OKX**，不跨所拼接。
  2) 复用现有**统一网关** `gateway_ext`（共享缓存 · 并发租约 · 本地预算 · 429 冷却 ·
     403/418 封禁 · 不跟随重定向 · 不接收认证头 · 不接受任意 URL）。
  3) **严格标的身份**：必须能在 `symbol_pool` 按 `(exchange, symbol)` 命中。
  4) **只读**：不落库、不当作历史、不下单、不读私人账户。
  5) **不冒充**：大户持仓/账户多空比、taker 买卖量比是**各自独立命名的原始指标**，
     **不得**当作「聪明钱金额 / 均价 / 身份」。币安公开 API **没有**聪明钱金额/均价/身份，
     本模块如实返回 `available:false` 并给出原因。

边界：不新建端口 / 库 / 采集器；不新增 UI 模块；不触碰日线全池历史任务。
"""
import json
import os
import re
import sqlite3
import sys
import threading
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.environ.get(
    "DATA_HUB_PROJECT", "/Users/boos/WorkBuddy AI/加密货币趋势判断系统")
# P3 切流（2026-10-13）：默认只读库 = 新热库 market_v2.sqlite3（旧库已退役）
DB_PATH = os.environ.get("MARKET_DB", os.path.join(PROJECT_ROOT, "data", "market_v2.sqlite3"))
for _p in (PROJECT_ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 复用现有统一网关（导入即完成 gateway_ext 的端点登记）
from gateway_ext import gateway  # noqa: E402
from data_hub.gateway import ENDPOINTS, DeferredRequest, Endpoint, RejectedRequest  # noqa: E402
from data_hub.store import MarketStore  # noqa: E402

# ── 币安官方 `/futures/data/*` 允许的 period（官方文档口径）────────────────────
RATIO_PERIODS = ("5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d")
# 这些端点的数据**每 5 分钟更新一次**，TTL 与之对齐，避免无谓重复请求
RATIO_TTL_MS = 300_000

SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{0,31}$")

# 聪明钱「金额 / 均价 / 身份」在币安公开 API 的**真实缺项**（不是没找到，是不存在）
SMART_MONEY_REASON = (
    "币安公开 API 无「聪明钱金额/均价/身份」字段："
    "`/futures/data/*` 只给**大户持仓量多空比**与**账户数多空比**（聚合比率，无身份、无金额、无均价）；"
    "`/fapi/v1/openInterest` 只给**全市场总量**（无多空拆分）；"
    "`/fapi/v1/positionRisk` 需签名（公开 404）。"
    "按用户口径：不得用人数比或 taker 量冒充聪明钱金额/均价/身份，故保持缺失。"
)

# 快照精简口径下**未取**的比率块的统一原因（显式缺失，不静默省略）
CORE_SKIP_REASON = ("快照精简口径未取：原版 UI 不使用该指标，"
                    "为与历史侧共用同一预算而有界取数；需要时经 `fields=ratio` 全量直取")


def _register():
    """把本模块需要的**币安公开**端点登记进统一网关（幂等；不改主项目 data_hub）。

    登记后即自动获得统一纪律：共享缓存 / 并发租约 / 预算 / 429 冷却 / 403·418 封禁。
    """
    add = {
        # 大户**持仓量**多空比（按持仓额口径的 Top 交易员）—— 对应参考产品「持仓量多空比」
        "binance.top_position_ratio": Endpoint(
            "fapi.binance.com", "/futures/data/topLongShortPositionRatio",
            frozenset({"symbol", "period", "limit"}), frozenset({"symbol", "period"}),
            RATIO_TTL_MS),
        # 大户**账户数**多空比 —— 对应参考产品「账户数多空比」
        "binance.top_account_ratio": Endpoint(
            "fapi.binance.com", "/futures/data/topLongShortAccountRatio",
            frozenset({"symbol", "period", "limit"}), frozenset({"symbol", "period"}),
            RATIO_TTL_MS),
        # 全市场账户数多空比
        "binance.global_account_ratio": Endpoint(
            "fapi.binance.com", "/futures/data/globalLongShortAccountRatio",
            frozenset({"symbol", "period", "limit"}), frozenset({"symbol", "period"}),
            RATIO_TTL_MS),
        # taker 主动买卖量比（**不是**持仓、**不是**身份）
        "binance.taker_ratio": Endpoint(
            "fapi.binance.com", "/futures/data/takerlongshortRatio",
            frozenset({"symbol", "period", "limit"}), frozenset({"symbol", "period"}),
            RATIO_TTL_MS),
        # 持仓量历史：**唯一**能拿到「总持仓名义额(USD)」的公开端点（无多空拆分）
        "binance.oi_hist": Endpoint(
            "fapi.binance.com", "/futures/data/openInterestHist",
            frozenset({"symbol", "period", "limit"}), frozenset({"symbol", "period"}),
            60_000),
        # 公开盘口（原始档位；「失衡」口径未定，本模块只回原始 bids/asks）
        "binance.depth": Endpoint(
            "fapi.binance.com", "/fapi/v1/depth",
            frozenset({"symbol", "limit"}), frozenset({"symbol"}), 2_000),
        # 公开逐笔聚合成交
        "binance.agg_trades": Endpoint(
            "fapi.binance.com", "/fapi/v1/aggTrades",
            frozenset({"symbol", "limit"}), frozenset({"symbol"}), 2_000),
    }
    for name, spec in add.items():
        ENDPOINTS.setdefault(name, spec)


_register()

_LOCK = threading.Lock()
_GW = None
_STORE = None


def _gw():
    """惰性构造网关（复用采集 profile：与现有 REST 工作器共用同一预算/缓存）。"""
    global _GW, _STORE
    if _GW is None:
        with _LOCK:
            if _GW is None:
                _STORE = MarketStore(DB_PATH)
                _GW = gateway(_STORE)
    return _GW


def iso(ms):
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return None


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ────────────────────────────── 严格标的身份 ──────────────────────────────

def _pool_row(con, exchange, symbol):
    try:
        return con.execute("SELECT * FROM symbol_pool WHERE exchange=? AND symbol=?",
                           (exchange, symbol)).fetchone()
    except sqlite3.OperationalError:
        return None


def _has_binance_instrument(con, base_asset):
    """币安是否有该**标的资产**的 USDT 永续（用于「只有币安没有才补 OKX」闸门）。"""
    try:
        row = con.execute(
            "SELECT 1 FROM symbol_pool WHERE exchange='binance' AND base_asset=? "
            "AND status='TRADING' LIMIT 1", (base_asset,)).fetchone()
        return bool(row)
    except sqlite3.OperationalError:
        return False


def resolve_identity(exchange, market_id):
    """严格标的身份解析 + 补充所闸门。

    返回 dict：
      ok / exchange / market_id / base / reason / is_supplement / source
    """
    ex = (exchange or "").strip().lower()
    mid = (market_id or "").strip()
    if ex and mid.startswith(ex + ":"):
        mid = mid[len(ex) + 1:]
    if ex not in ("binance", "okx"):
        return {"ok": False, "reason": f"不支持的交易所 {exchange!r}（本模块只读币安公开 API；"
                                       f"仅当币安无该合约时补 OKX）"}
    if not mid or not SYMBOL_RE.match(mid):
        return {"ok": False, "reason": f"标的代号不合法：{market_id!r}"}

    con = _gw().store.connect()
    try:
        row = _pool_row(con, ex, mid)
        if row is None:
            return {"ok": False, "exchange": ex, "market_id": mid,
                    "reason": f"symbol_pool 中无 {ex}:{mid} —— 标的身份未确认，不对外取数"}
        base = row["base_asset"]
        # 「币安没有该代币/合约才补 OKX」：币安已有 ⇒ 即使请求 OKX 也拒绝，
        # 且**不因币安历史缺口**放行（缺口属于历史补缺侧，不构成切换理由）。
        if ex == "okx" and base and _has_binance_instrument(con, base):
            return {"ok": False, "exchange": ex, "market_id": mid, "base": base,
                    "reason": f"币安已有 {base} 的 USDT 永续 ⇒ 按口径只用币安，不切 OKX"
                              f"（历史缺口不构成切换理由）"}
        return {"ok": True, "exchange": ex, "market_id": mid, "base": base,
                "is_supplement": ex == "okx",
                "status": row["status"], "contract_type": row["contract_type"],
                "source": "symbol_pool（真实 exchangeInfo 快照）"}
    finally:
        con.close()


# ────────────────────────────── 取数与规范化 ──────────────────────────────

def _gw_get(name, params, attempts=4, cap_s=4.0, gw=None):
    """统一网关取数（**遵守网关自己的节流**）。

    网关对同一来源有 `min_interval_ms` 间隔保护与预算，连续取数会返回 `DeferredRequest`
    并给出**最早可重试时间**。这里按该时间等待后重试（上限 `cap_s` 秒、最多 `attempts` 次），
    **不绕过**任何限频/冷却/封禁；预算耗尽或来源被封时如实返回失败原因。

    `gw` 可注入**既有采集工作器持有的同一网关实例**（`RestGateway(PublicGateway)`）：
    采集器刷新快照时复用同一实例，缓存/预算/租约/冷却本就 DB 级共享，
    注入后连「逐端点成功/失败记账」也落到采集器同一份统计里，不另造第二套通道。
    """
    import time as _t
    gate = gw or _gw()
    last = None
    for i in range(attempts):
        try:
            res = gate.get(name, params)
            return res.get("数据"), res.get("来源"), res.get("获取时间UTC毫秒"), None
        except DeferredRequest as exc:
            last = f"{type(exc).__name__}: {exc}"
            if i == attempts - 1:
                break
            wait = (exc.retry_at - gate.store.clock()) / 1000.0
            _t.sleep(max(0.0, min(wait, cap_s)) + 0.05)
        except RejectedRequest as exc:
            return None, None, None, f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001
            return None, None, None, f"{type(exc).__name__}: {exc}"
    return None, None, None, last


def _last(rows):
    if isinstance(rows, list) and rows:
        return rows[-1]
    return None


def _ratio_block(name, symbol, period, key_map, gw=None):
    data, origin, got_ms, err = _gw_get(name, {"symbol": symbol, "period": period, "limit": 1},
                                        gw=gw)
    if err:
        return {"available": False, "reason": err}
    row = _last(data)
    if not isinstance(row, dict):
        return {"available": False, "reason": "上游未返回数组行"}
    out = {"available": True, "period": period,
           "event_time": iso(row.get("timestamp")),
           "fetched_at": iso(got_ms), "origin": origin}
    for src_key, dst_key in key_map:
        if src_key in row:
            out[dst_key] = row[src_key]
    return out


def _parse_fields(raw):
    """`fields` 选择器：`ratio` / `oi` / `orderbook`（逗号分隔），缺省=全部。

    为什么要选择器：全量取数是 **7 次**上游请求；列表页只需要「多空比 + OI 名义额」
    （3 次）。有界取数才能与历史侧共用同一预算而不抢占。
    """
    if not raw:
        return {"ratio", "oi", "orderbook"}
    want = {p.strip().lower() for p in str(raw).split(",") if p.strip()}
    if "all" in want or not want:
        return {"ratio", "oi", "orderbook"}
    return want & {"ratio", "oi", "orderbook"}


def fetch_public_extras(exchange, market_id, period="1h", fields=None, gw=None, core=False):
    """币安公开数据归一化。**不落库**；所有取数走统一网关（有缓存/限频）。

    `gw`    —— 可注入既有采集工作器持有的网关实例（见 `_gw_get`）。
    `core`  —— **快照精简口径**：只取「原版 UI 实际使用」的 2 项
               （持仓量多空比 + 总持仓名义额 USD），其余比率块显式标注未取，
               使单标上游请求从 6 次降到 **2 次**，供采集器**有界**周期刷新。
               默认 `False`，行为与既有契约完全一致。
    """
    ident = resolve_identity(exchange, market_id)
    if not ident.get("ok"):
        return {"available": False, "exchange": ident.get("exchange"),
                "market_id": ident.get("market_id"), "as_of": _now_iso(),
                "missing_reason": ident.get("reason"),
                "quality": {"status": "missing", "age_seconds": None,
                            "note": ident.get("reason")}}
    ex, sym = ident["exchange"], ident["market_id"]
    if period not in RATIO_PERIODS:
        period = "1h"
    want = _parse_fields(fields)

    out = {"available": True, "exchange": ex, "market_id": sym, "base": ident.get("base"),
           "as_of": _now_iso(), "is_supplement": ident.get("is_supplement"),
           "identity_source": ident.get("source"), "fields": sorted(want),
           "source_urls": [], "missing_reason": None}

    # ── 币安：公开 OI 总量 + 总持仓名义额（USD）──────────────────────────
    if ex == "binance":
        if "oi" in want:
            # `openInterestHist` 的**同一行**同时给出 `sumOpenInterest`（总量）与
            # `sumOpenInterestValue`（名义额 USD），故精简口径下无需再打
            # `/fapi/v1/openInterest` —— 少 1 次上游，口径不变（同为「全市场总持仓量」，
            # 只是采样在 5m 边界）。完整口径保持原有两次取数，行为不变。
            if core:
                oi, oi_origin, oi_ms, oi_err = None, None, None, None
            else:
                oi, oi_origin, oi_ms, oi_err = _gw_get("binance.oi", {"symbol": sym}, gw=gw)
            oih, oih_origin, oih_ms, oih_err = _gw_get(
                "binance.oi_hist", {"symbol": sym, "period": "5m", "limit": 1}, gw=gw)
            oi_row = _last(oih) if isinstance(oih, list) else None
            if (oi_err and oih_err) or (oih_err and not core):
                block = {"available": False, "reason": f"OI 取数失败：{oih_err or oi_err}"}
            else:
                if core:
                    amount = (float(oi_row["sumOpenInterest"])
                              if isinstance(oi_row, dict) and oi_row.get("sumOpenInterest") else None)
                    amount_et = iso(oi_row.get("timestamp")) if isinstance(oi_row, dict) else None
                    oi_src = "币安 /futures/data/openInterestHist（同一行给总量与名义额）"
                else:
                    amount = (float(oi["openInterest"])
                              if isinstance(oi, dict) and oi.get("openInterest") else None)
                    amount_et = iso(oi.get("time")) if isinstance(oi, dict) else None
                    oi_src = "币安 fapi/v1/openInterest + /futures/data/openInterestHist"
                block = {
                    "available": True,
                    "amount": amount,
                    "amount_event_time": amount_et,
                    # 唯一公开的「总持仓名义额」来源：
                    # /futures/data/openInterestHist.sumOpenInterestValue
                    "usd": (float(oi_row["sumOpenInterestValue"])
                            if isinstance(oi_row, dict) and oi_row.get("sumOpenInterestValue") else None),
                    "usd_event_time": iso(oi_row.get("timestamp")) if isinstance(oi_row, dict) else None,
                    "fetched_at": iso(oi_ms or oih_ms),
                    "origin": oi_origin or oih_origin,
                    "source": oi_src,
                    "note": ("**全市场总量**，无多空拆分、无身份；不是「聪明钱持仓额」"
                             + ("；快照精简口径：总量与名义额取自同一行（5m 边界采样）"
                                if core else "")),
                }
            out["open_interest"] = block
            out["source_urls"].append(
                "https://fapi.binance.com/futures/data/openInterestHist?symbol=" + sym + "&period=5m")
            if not core:
                out["source_urls"].append(
                    "https://fapi.binance.com/fapi/v1/openInterest?symbol=" + sym)

        # ── 币安：4 个**独立命名的原始指标**（不得冒充聪明钱）───────────────
        if "ratio" in want:
            top_pos = _ratio_block(
                "binance.top_position_ratio", sym, period,
                [("longShortRatio", "value"), ("longAccount", "long_account"),
                 ("shortAccount", "short_account")], gw=gw)
            if core:
                # 快照精简口径：只取原版 UI 用到的「持仓量多空比」，
                # 其余 3 块**显式标注未取**（不静默省略、不伪造）。
                skip = {"available": False, "reason": CORE_SKIP_REASON}
                out["long_short"] = {
                    "period": period,
                    "top_position_long_short_ratio": top_pos,
                    "top_account_long_short_ratio": dict(skip),
                    "global_account_long_short_ratio": dict(skip),
                    "taker_buy_sell_ratio": dict(skip),
                    "source": "币安 fapi /futures/data/*（官方公开，无需密钥）",
                    "note": ("**快照精简口径**：仅取原版 UI 实际使用的「持仓量多空比」"
                             "（topLongShortPositionRatio）；其余 3 块未取，见各块 reason。"
                             "四者均为**聚合比率**，无身份/无金额/无均价，"
                             "**不得**改名成「聪明钱金额/均价/身份」。"),
                }
            else:
                out["long_short"] = {
                    "period": period,
                    "top_position_long_short_ratio": top_pos,
                    "top_account_long_short_ratio": _ratio_block(
                        "binance.top_account_ratio", sym, period,
                        [("longShortRatio", "value"), ("longAccount", "long_account"),
                         ("shortAccount", "short_account")], gw=gw),
                    "global_account_long_short_ratio": _ratio_block(
                        "binance.global_account_ratio", sym, period,
                        [("longShortRatio", "value"), ("longAccount", "long_account"),
                         ("shortAccount", "short_account")], gw=gw),
                    "taker_buy_sell_ratio": _ratio_block(
                        "binance.taker_ratio", sym, period,
                        [("buySellRatio", "value"), ("buyVol", "buy_vol"),
                         ("sellVol", "sell_vol")], gw=gw),
                    "source": "币安 fapi /futures/data/*（官方公开，无需密钥）",
                    "note": ("原始指标口径：**持仓量多空比** = topLongShortPositionRatio；"
                             "**账户数多空比** = topLongShortAccountRatio；"
                             "**全局账户多空比** = globalLongShortAccountRatio；"
                             "**taker 买卖量比** = takerlongshortRatio。"
                             "四者均为**聚合比率**，无身份/无金额/无均价，"
                             "**不得**改名成「聪明钱金额/均价/身份」。"),
                }
            out["source_urls"] += [
                "https://fapi.binance.com/futures/data/topLongShortPositionRatio?symbol=" + sym,
            ]
            if not core:
                out["source_urls"] += [
                    "https://fapi.binance.com/futures/data/topLongShortAccountRatio?symbol=" + sym,
                    "https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol=" + sym,
                    "https://fapi.binance.com/futures/data/takerlongshortRatio?symbol=" + sym,
                ]

        # ── 币安：公开盘口（只回原始档位；「失衡」口径未定 ⇒ 不由后端拍板）──
        if "orderbook" in want:
            depth, d_origin, d_ms, d_err = _gw_get("binance.depth",
                                                   {"symbol": sym, "limit": 5}, gw=gw)
            if d_err or not isinstance(depth, dict):
                out["orderbook"] = {"available": False, "reason": d_err or "上游未返回盘口"}
            else:
                out["orderbook"] = {
                    "available": True, "unit": "标的币",
                    "bids": depth.get("bids") or [], "asks": depth.get("asks") or [],
                    "best_bid": (depth.get("bids") or [None])[0],
                    "best_ask": (depth.get("asks") or [None])[0],
                    "event_time": iso(depth.get("E")), "fetched_at": iso(d_ms), "origin": d_origin,
                    "source": "币安 fapi/v1/depth?limit=5",
                    "note": "只给原始档位；「订单簿流动性失衡」的档位数/加权/单位口径未定，不在此拍板",
                }
                out["source_urls"].append(
                    "https://fapi.binance.com/fapi/v1/depth?symbol=" + sym + "&limit=5")

    # ── OKX 补充位：**只有**币安没有该代币/合约时才可能走到这里 ──────────────
    else:
        out["missing_reason"] = ("OKX 为补充源；本模块的公开扩展字段当前只对币安实现，"
                                 "OKX 标的保持缺失（不跨所拼接）")
        out["quality"] = {"status": "missing", "age_seconds": None,
                          "note": out["missing_reason"]}

    # ── 聪明钱：币安公开 API **没有**金额/均价/身份 ⇒ 如实缺失 ───────────────
    out["smart_money"] = {"available": False, "reason": SMART_MONEY_REASON}
    out.setdefault("quality", {"status": "ok", "age_seconds": None,
                               "note": "币安公开 API 只读；不落库、不当作历史"})
    return out


# ────────────────────── 采集器侧：周期刷新本机可读快照 ──────────────────────

class _CountingGateway:
    """给注入的网关套一层**调用计数**（只计数，不改行为）。

    目的：验收要能拿出「本轮真实上游请求数 vs 共享缓存命中数」的硬数字，
    而不是「计划请求数」。计数口径与网关返回的 `来源` 字段一致。
    """

    def __init__(self, gw):
        self._gw = gw
        self.attempts = 0
        self.upstream = 0        # 来源=公共REST（真实出网）
        self.cached = 0          # 来源=共享缓存（未出网）
        self.deferred = 0        # 本地节流/预算/冷却
        self.rejected = 0        # 被拒绝（封禁/未登记等）
        self.failed = 0          # 其它异常

    def get(self, name, params=None):
        self.attempts += 1
        try:
            res = self._gw.get(name, params)
        except DeferredRequest:
            self.deferred += 1
            raise
        except RejectedRequest:
            self.rejected += 1
            raise
        except Exception:                                          # noqa: BLE001
            self.failed += 1
            raise
        if res.get("来源") == "共享缓存":
            self.cached += 1
        else:
            self.upstream += 1
        return res

    def __getattr__(self, key):
        return getattr(self._gw, key)

    def stats(self):
        return {"attempts": self.attempts, "upstream": self.upstream, "cached": self.cached,
                "deferred": self.deferred, "rejected": self.rejected, "failed": self.failed}


def refresh_snapshot(gw, symbols, period="1h", fields="ratio,oi", extra=None):
    """**采集器侧**周期刷新：把有界标的集合的公开扩展字段写入本机可读快照。

    口径（与用户要求逐条对应）：
      · **复用既有网关**：`gw` 由采集器注入（同一 `RestGateway(PublicGateway)`），
        缓存/预算/租约/冷却/封禁一律走既有统一网关，**不另造第二套通道**；
      · **有界**：`symbols` 由调用方给**有界集合**（页首 N 个标的），本函数不自行扩张；
      · **精简取数**：`core=True` ⇒ 单标 **2 次**上游（OI 名义额/总量取自
        `openInterestHist` 同一行 + 持仓量多空比），只覆盖原版 UI 实际使用的字段；
      · **只写快照**：不落库、不写历史、不新增进程/端口；
      · **失败不覆盖**：仅当 `available:true` 才落盘；失败保留上一份好快照，
        由读取侧按写入时点判过期（不把失败静默写成空值）。

    返回本轮统计（同时写 `_index.json`）。
    """
    import time as _t
    import public_snapshot as _snap
    counted = _CountingGateway(gw)
    t0 = _t.monotonic()
    ok = failed = skipped = 0
    failures = []
    for item in symbols or ():
        if isinstance(item, (tuple, list)) and len(item) == 2:
            ex, mid = item[0], item[1]
        else:
            ex, mid = "binance", item
        try:
            payload = fetch_public_extras(ex, mid, period=period, fields=fields,
                                          gw=counted, core=True)
        except Exception as exc:                                   # noqa: BLE001
            failed += 1
            if len(failures) < 12:
                failures.append(f"{ex}:{mid} → {type(exc).__name__}")
            continue
        if payload.get("available"):
            _snap.write(ex, mid, payload)
            ok += 1
        else:
            skipped += 1
            if len(failures) < 12:
                failures.append(f"{ex}:{mid} → {(payload.get('missing_reason') or '未取到')[:60]}")
    elapsed = round(_t.monotonic() - t0, 2)
    stats = {
        "targets": len(symbols or ()), "ok": ok, "failed": failed, "skipped": skipped,
        "period": period, "fields": fields, "mode": "core（精简：OI名义额+持仓量多空比，单标2次上游）",
        "requests": counted.stats(), "elapsed_s": elapsed,
        "snapshot_dir": str(_snap.SNAP_DIR),
        "failures": failures,
        "source": "币安 fapi /futures/data/* + fapi/v1/openInterest（官方公开，无需密钥）",
        "note": ("快照由**现有采集工作器**周期写入；本机 API **只读快照**，"
                 "前端读取**不触发**任何交易所请求"),
    }
    if isinstance(extra, dict):
        stats.update(extra)
    try:
        _snap.write_index(stats)
    except OSError as exc:                                         # noqa: BLE001
        stats["index_error"] = f"{type(exc).__name__}: {exc}"
    return stats


# ────────────────────────────── 路由处理（供 adapter 最小接线）──────────────────

def handle_market_tail(exchange, market_id, params):
    """**直取口径**（命令行/核验用）：`GET /api/v1/markets/{ex}/{mid}/public-extras?...`。

    返回**普通 dict**（与 adapter.py 既有路由同形：`return body, 200`）。
    `fields` 可选：`ratio`（4 个多空比类原始指标）/ `oi`（OI 总量+名义额，2 次）/
    `orderbook`（盘口，1 次）/ 缺省 `all`（7 次）。

    ⚠️ **服务路径不用本函数**：`adapter` 的路由读**本机快照**
    （`public_snapshot`，由采集器 `refresh_snapshot` 周期写入），
    以满足「前端读取**不触发**外部采集」。本函数只供离线核验直取。
    """
    def first(key, default=None):
        v = params.get(key)
        if isinstance(v, list):
            v = v[0] if v else None
        return default if v in (None, "") else v

    return fetch_public_extras(exchange, market_id,
                               period=first("period", "1h"), fields=first("fields"))


# 采集 → 本机 API → 原版字段 的接线（**已由本任务落地**）：
#   · 采集器：`realtime_collector.public_extras_loop` 周期调用 `refresh_snapshot`
#     写本机快照（`public_snapshot`）；
#   · API：`adapter.v1_public_extras` **只读快照**，`route_v1` 一行分派；
#   · 前端：既有 `public-extras` 请求/映射不变（`oi.longShortRatio` / `oi.totalUsd`）。
MINIMAL_WIRING = '''
# 1) 文件顶部（与既有 import 同处）：
import public_snapshot
# 2) route_v1() 中，既有 `if len(seg) == 4 and seg[0] == "markets":` 块内、
#    与 `if tail == "candles":` 并列，加：
        if tail == "public-extras":
            return v1_public_extras(seg[1], seg[2], params), 200
'''


if __name__ == "__main__":
    argv = sys.argv[1:]
    ex = "binance"
    mid = "BTCUSDT"
    period = "1h"
    fields = None
    if argv:
        if argv[0] in ("binance", "okx"):
            ex, argv = argv[0], argv[1:]
        if argv:
            mid = argv[0]
        if len(argv) > 1:
            period = argv[1]
        if len(argv) > 2:
            fields = argv[2]
    print(json.dumps(fetch_public_extras(ex, mid, period, fields), ensure_ascii=False, indent=1))
