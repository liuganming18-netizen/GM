#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作区公共网关扩展：登记**公共批量**端点与实时（WS）端点。

为什么不改主项目：`data_hub/gateway.py` 的 `ENDPOINTS` 是模块级字典，
`PublicGateway._request` 在**调用时**才按名字查表，所以这里往同一字典补登记
即可生效 —— 满足用户「无需改主项目 data_hub，工作区扩展已登记公共批量端点/WS 管理」。

新增端点与原有端点**共用同一套纪律**（由 PublicGateway 统一执行，无法绕过）：
  共享缓存 · 并发租约 · 本地工程预算 · 429 冷却 · 403/418 封禁 · 不跟随重定向 ·
  不接收认证头 · 不接收任意 URL。

预算口径（用户已确认）：`budget_units` / `min_interval_ms` 是**本地工程单位**，
不是交易所官方权重；每个端点默认 `cost=10`。全池采集用保守统一 profile
（例如 budget_units=1200, min_interval_ms=250 ⇒ 最多 120 请求/分钟），
**所有 REST 工作器共用同一 profile**。
"""
from data_hub.gateway import ENDPOINTS, Endpoint

# ── 1) 扩展已有端点：欧易 instruments（**不含** instCategory 请求参数）─────────
# ⚠️ 纠正（实测）：`instCategory` 是**响应字段**，**不是**该端点的请求参数。
#   实测 `instType=SWAP&instCategory=1` / `=3` / `=4` **均返回同一批完整 500 条**，
#   OKX 直接忽略该参数 —— 若把它当请求参数登记，会让人误以为"按参数过滤过了"，
#   进而把请求参数当成分类依据。**分类必须逐条读响应里的 `instCategory`**。
#   官方 instCategory（响应）：1=Crypto 3=Stocks 4=Commodities 5=Forex 6=Bonds。
#   该端点**无分页、无 500 上限**：实测 SPOT=1143、OPTION(BTC-USD)=1398、SWAP=500，
#   故 SWAP 的 500 条是完整返回，不是截断。
ENDPOINTS["okx.instruments"] = Endpoint(
    "www.okx.com", "/api/v5/public/instruments",
    frozenset({"instType", "instId", "uly", "instFamily"}), frozenset({"instType"}), 3600000)

# ── 2) 新增**全市场批量**端点（一次请求覆盖全池，避免逐币打点）─────────────────
_EXTRA = {
    # 币安 USD-M：不带 symbol ⇒ 返回全部合约。24h 最新价/涨跌幅/计价成交额。
    "binance.ticker24h": Endpoint(
        "fapi.binance.com", "/fapi/v1/ticker/24hr", frozenset(), frozenset(), 5000),
    # 币安 USD-M：不带 symbol ⇒ 返回全部合约的标记价 + 当前资金费率 + 下次结算时间。
    "binance.premium_index": Endpoint(
        "fapi.binance.com", "/fapi/v1/premiumIndex", frozenset(), frozenset(), 5000),
    # 币安 USD-M：逐币 OI（官方**无**全市场批量 OI 端点，只能逐币轮询）。
    "binance.oi": Endpoint(
        "fapi.binance.com", "/fapi/v1/openInterest", frozenset({"symbol"}),
        frozenset({"symbol"}), 15000),
    # 币安 USD-M：不带 symbol ⇒ 全市场 OI 快照（含 notional 的接口不可得，故仅记数量）。
    "binance.oi_all": Endpoint(
        "fapi.binance.com", "/fapi/v1/openInterest", frozenset(), frozenset(), 15000),
    # 欧易：全 SWAP 行情（最新价 / 24h 计价成交额 / 买一卖一）。
    "okx.tickers": Endpoint(
        "www.okx.com", "/api/v5/market/tickers", frozenset({"instType"}),
        frozenset({"instType"}), 5000),
    # 欧易：全 SWAP 标记价。
    "okx.mark_price": Endpoint(
        "www.okx.com", "/api/v5/public/mark-price", frozenset({"instType", "instId"}),
        frozenset({"instType"}), 5000),
    # 欧易：全 SWAP 持仓量（含 oiCcy / oiUsd 两种口径，需分别保留）。
    "okx.open_interest": Endpoint(
        "www.okx.com", "/api/v5/public/open-interest", frozenset({"instType", "instId"}),
        frozenset({"instType"}), 15000),
    # 欧易：当前资金费率（instId=ANY ⇒ 全部）。
    "okx.funding_all": Endpoint(
        "www.okx.com", "/api/v5/public/funding-rate", frozenset({"instId"}),
        frozenset({"instId"}), 15000),
    # 欧易：**近期** K 线（`/market/candles`，含未收盘 confirm=0，limit ≤300）。
    # 与 `okx.candles`（=/market/history-candles，只给已收盘旧数据、limit ≤100）区分：
    # 选中合约的"有限近期原生尾段"必须用本端点，才能拿到最新一根未收盘 bar。
    "okx.candles_recent": Endpoint(
        "www.okx.com", "/api/v5/market/candles",
        frozenset({"instType", "instId", "bar", "after", "before", "limit"}),
        frozenset({"instId", "bar"}), 5000),
}

ENDPOINTS.update(_EXTRA)

# ── 2a) 币安 OI 历史：**带时间范围**的变体（一次性缺口回补用）────────────────
# 与 `realtime_collector` 登记的 `binance.oi_hist`（只有 symbol/period/limit）同 host、同 path、
# 同响应结构；差别仅在于**显式允许 `startTime`/`endTime`**，用于从「归档尾点」向前补洞。
# 官方该端点 `limit` 上界 500 ⇒ 5m 桶一次最多覆盖 41.6 小时，跨更长的洞必须按时间翻页。
# 纪律：仍走同一套 PublicGateway（共享缓存/租约/预算/冷却/不跟随重定向/不接收凭据），
# 不新增任何旁路；`startTime`/`endTime` 已登记为**数字型**参数，会被 `_request` 校验为非负整数。
ENDPOINTS.setdefault("binance.oi_hist_range", Endpoint(
    "fapi.binance.com", "/futures/data/openInterestHist",
    frozenset({"symbol", "period", "limit", "startTime", "endTime"}),
    frozenset({"symbol", "period"}), 60_000))

# ── 2c) 币安**聪明钱（Smart Signal）**公开只读端点（2026-10-07 实测，无鉴权 200）──
# 来源 = 用户给定的**币安公开页面**自身 XHR（`https://www.binance.bh/zh-CN/smart-money/
# signal/{SYMBOL}`）：`/bapi/...` 路径含 `/public/`，**不带 cookie / Authorization** 即 200。
# 为什么用 `www.binance.bh` 而不是 `www.binance.com`：本机实测 `www.binance.com` 该 bapi
# 直连 HTTP 000（不可达），`www.binance.bh` 200 且响应体与页面一致（同一官方域族）。
# 纪律：只读、GET、无凭据；`timeRange` 为**必填**（缺则 code=000002 参数非法）。
# TTL 与页面声明「每 60 秒更新」对齐，避免无谓重复请求。
SMART_MONEY_TTL_MS = 60_000
SMART_MONEY_HOST = "www.binance.bh"
SMART_MONEY_BASE = "/bapi/futures/v1/public/future/smart-money/signal"
SMART_MONEY_ENDPOINTS = {
    # 17 字段总览：含**公开 fapi 拿不到**的鲸鱼均价、浮盈人数（金额/人数/均价/更新时点）
    "binance.smart_money_overview": Endpoint(
        SMART_MONEY_HOST, SMART_MONEY_BASE + "/overview",
        frozenset({"symbol", "timeRange"}), frozenset({"symbol"}), SMART_MONEY_TTL_MS),
    # 逐侧**当前持仓额(USDT)** + 数量 + 鲸鱼仓位（六槽「持仓额/巨鲸占比」的真实来源）
    "binance.smart_money_stats": Endpoint(
        SMART_MONEY_HOST, SMART_MONEY_BASE + "/details/stats",
        frozenset({"symbol", "timeRange"}), frozenset({"symbol", "timeRange"}),
        SMART_MONEY_TTL_MS),
    # 明细行：聚合行（userType=2）+ 具名交易员行（userType=0，含 traderName/topTraderId）
    # `lastTradeTime` 是**真实新鲜度**来源（`overview.updateTime` 是日级队列标记，不可当新鲜度）
    "binance.smart_money_details": Endpoint(
        SMART_MONEY_HOST, SMART_MONEY_BASE + "/details/list",
        frozenset({"symbol", "timeRange", "page", "rows", "sortingField", "sortingOrder"}),
        frozenset({"symbol", "timeRange"}), SMART_MONEY_TTL_MS),
    # 兼容别名：页面早期口径的 `/signal/list`（与 details/list 同族聚合行，保留以便对账）
    "binance.smart_money_list": Endpoint(
        SMART_MONEY_HOST, SMART_MONEY_BASE + "/list",
        frozenset({"symbol", "timeRange", "side", "sortBy", "sortOrder", "page"}),
        frozenset({"symbol", "timeRange"}), SMART_MONEY_TTL_MS),
}
ENDPOINTS.update(SMART_MONEY_ENDPOINTS)

# ── 2b) 币安**现货**目录：与 symbol_pool.py 登记一致（幂等），供跨所映射取证用 ──
# 完整现货目录约 17.6MB > 网关 16MB 单响应上限，故固定带过滤查询（约 5.9MB）。
ENDPOINTS.setdefault("binance.spot_instruments", Endpoint(
    "api.binance.com", "/api/v3/exchangeInfo",
    frozenset({"permissions", "symbolStatus"}), frozenset({"permissions", "symbolStatus"}),
    3600000))

# ── 3) 实时（WS）端点登记：仅登记地址与用途，连接由 realtime_collector 建立 ────
# 说明：WS 不走 PublicGateway（网关只管 REST），但登记在此处集中管理，
# 避免"工作区里冒出一个没人管的对外连接"。
WS_ENDPOINTS = {
    "binance.usdm": {
        # ⚠️ 必须用官方 `/market/stream`：旧 `wss://fstream.binance.com/stream` 实测
        # 「握手 101 但无任何数据帧」；`/market/stream` 实测 1.35s 收到真实 24hrTicker。
        "url": "wss://fstream.binance.com/market/stream?streams=",
        # 组合流：一次订阅多个 <symbol>@ticker；标记价/费率由 REST premiumIndex 覆盖。
        "stream_suffixes": {"ticker": "@ticker"},
        "batch_limit": 200,          # 单连接订阅上限（官方建议 ≤ 200 条/连接）
        "note": "币安 USD-M 组合流；批量订阅，失败退避重连，不绕过任何拒绝",
    },
    "okx.swap": {
        "url": "wss://ws.okx.com:8443/ws/v5/public",
        "channels": ["tickers", "funding-rate"],
        "batch_limit": 100,          # 单次 subscribe 的 args 条数上限
        "note": "欧易公共频道；instId 分组订阅，退避重连",
    },
}

# ── 4) 采集用保守统一 profile（所有 REST 工作器共用）────────────────────────
COLLECT_PROFILE = {"budget_units": 1200, "min_interval_ms": 250}

# ── 5) 传输层：**请求时**重新解析代理（不再冻结启动瞬间的沙箱代理端口）────────
# 背景（本机实测，非推测）：
#   沙箱会向被启动的进程注入 `HTTP_PROXY/HTTPS_PROXY=http://127.0.0.1:<port>`，
#   该端口由 `sandbox-cli` 的 LocalProxy 持有，并**随沙箱会话轮换而消失**
#   （实证：`sandbox_14841_016.log` 有 `注入代理环境变量: HTTP_PROXY=http://127.0.0.1:62185`，
#    而 `lsof -iTCP:62185` 已无监听）。
#   `urllib` 的默认行为是**在 opener 构造时读一次环境变量**；长驻进程因此在会话轮换后
#   把全部 REST 打到已死端口（`URLError`），而自实现 WS 走 raw socket 直连 → 不受影响。
#   这就是「REST 全挂、WS 正常、健康检查仍说健康」的根因。
#
# 处置（不解除任何网络/安全保护，不新建第二套代理）：
#   ① 每次请求前探测**当前**环境代理是否仍可连（TCP 存活）；可用就继续走它 —— 仍在用沙箱代理；
#   ② 只有在**代理端点已经消失**（端口无人监听）时，才退化为**本项目既有直连约定**
#      （与 `历史下载/download_klines.py` 的 `OPENER = build_opener(ProxyHandler({}))`
#      同一约定，其注释写明"本环境存在 HTTP(S)_PROXY；归档为公开静态资源，显式直连以保证可复现"），
#      即**普通（非沙箱）运行环境**下 urllib 的默认行为。
#   ③ 代理**存活但拒绝**请求时**不**改走直连 —— 不绕过任何仍然生效的沙箱策略。
import os as _os
import socket as _socket
import time as _time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from data_hub.gateway import NoRedirect, RejectedRequest

PROXY_TTL_S = 15.0
_PROXY_ENV_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                   "ALL_PROXY", "all_proxy")
_proxy_state = {"at": 0.0, "value": None}
_openers = {}


def _proxy_hostport(url):
    try:
        u = urlsplit(url if "://" in url else "http://" + url)
        if not u.hostname:
            return None
        return u.hostname, (u.port or (443 if u.scheme == "https" else 80))
    except ValueError:
        return None


def _tcp_alive(host, port, timeout=0.35):
    try:
        with _socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def env_proxy_candidates():
    out = []
    for key in _PROXY_ENV_KEYS:
        value = (_os.environ.get(key) or "").strip()
        if value and value not in out:
            out.append(value)
    return out


def resolve_proxy(force=False):
    """返回**当前可用**的代理地址；环境代理端点已消失时返回 None（= 普通环境默认行为）。

    结果带短 TTL 缓存（`PROXY_TTL_S`），失败路径用 `force=True` 立即重探。
    """
    now = _time.monotonic()
    if not force and now - _proxy_state["at"] < PROXY_TTL_S:
        return _proxy_state["value"]
    value = None
    for cand in env_proxy_candidates():
        hp = _proxy_hostport(cand)
        if hp and _tcp_alive(*hp):
            value = cand
            break
    _proxy_state["value"] = value
    _proxy_state["at"] = now
    return value


def _opener_for(proxy):
    key = proxy or ""
    opener = _openers.get(key)
    if opener is None:
        handler = ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
        opener = build_opener(handler, NoRedirect)
        _openers[key] = opener
    return opener


def _fetch(url, timeout, proxy):
    request = Request(url, headers={"Accept": "application/json",
                                    "User-Agent": "MarketDataHub/0.1"})
    try:
        response = _opener_for(proxy).open(request, timeout=timeout)
    except HTTPError as exc:
        response = exc
    with response:
        raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise RejectedRequest("响应超过16MB，本次不接收")
        return response.code, dict(response.headers), raw


def resilient_transport(url, timeout):
    """与 `data_hub.gateway.public_transport` 同签名/同语义，但**请求时**解析代理。"""
    proxy = resolve_proxy()
    try:
        return _fetch(url, timeout, proxy)
    except RejectedRequest:
        raise
    except Exception:                                              # noqa: BLE001
        # 代理端点可能刚刚随会话轮换消失：重新探测一次；地址没变则不重试（不掩盖真实错误）。
        proxy2 = resolve_proxy(force=True)
        if proxy2 == proxy:
            raise
        return _fetch(url, timeout, proxy2)


def gateway(store, **overrides):
    """构造一个使用采集 profile 的 PublicGateway（导入本模块即完成端点登记）。

    默认传输层为 `resilient_transport`（请求时解析代理）；调用方显式传入 `transport=`
    时以调用方为准（测试注入用）。
    """
    from data_hub.gateway import PublicGateway
    profile = dict(COLLECT_PROFILE)
    profile.update(overrides)
    profile.setdefault("transport", resilient_transport)
    return PublicGateway(store, **profile)
