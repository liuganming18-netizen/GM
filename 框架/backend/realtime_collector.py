#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一实时采集进程（唯一对外采集者）。

职责边界（用户授权范围）：
  ✅ 公共行情**只读**采集：最新价 / 24h 涨跌与计价成交额 / 标记价 / 当前费率 /
     持仓量(OI) / 费率结算历史 / **各周期原生 K 线流（含未收盘 bar 与闭合入库）**。
  ❌ 不接账户、不下单、不做策略/回测/Telegram；不读任何凭据。

设计要点：
  1. **REST 一律经统一 PublicGateway**（共享缓存/租约/预算/429 冷却/403 封禁），
     使用保守统一 profile（budget_units=1200, min_interval_ms=250 ⇒ ≤120 请求/分钟）。
  2. **WS 优先**：币安组合流按 100 标的/连接批量订阅；欧易按 instId 分组订阅。
     断线指数退避重连，不绕过任何拒绝。
  3. **价格实时最新表与已收盘 K 线分开**：写 `realtime_quotes`；
     **未收盘 bar 只写独立临时层 `realtime_klines`**（供图表），
     bar 收盘后才按 (exchange,market,symbol,timeframe,open_time) 幂等写入
     `candle_versions` + `candle_heads`，且**不覆盖已有行**（历史回填不覆盖更新的实时值）。
  4. **单位统一**：欧易张数按 `ctVal`/`ctMult` 记录，OI 同时保留 `oiCcy`/`oiUsd`；
     成交额统一为计价(USDT)口径；所有行保留 事件/接收/获取 时间与来源。
  5. **诚实边界**：不虚称"所有币每秒更新"。OI 全池扫描一轮需数分钟，
     新鲜度由 `collector_rounds` 的 newest/oldest 事件时间如实记录。
  6. 不插值冒充费率结算事件：只写交易所真实返回的结算点。

用法：
  PY=/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3
  $PY realtime_collector.py --once          # 只跑一轮 REST 快照（用于核验）
  $PY realtime_collector.py                 # 常驻：WS + 周期 OI/费率
"""
import argparse
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import sys
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote as _urlquote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
PROJECT = os.environ.get("DATA_HUB_PROJECT", str(HERE.parent.parent))
# P3 切流（2026-10-13）：默认写库 = 新热库 market_v2.sqlite3（旧 market.sqlite3 已退役）
DEFAULT_MARKET_DB = str(HERE.parent.parent / "数据" / "市场数据" / "db" / "market_v2.sqlite3")
DB = os.environ.get("MARKET_DB", DEFAULT_MARKET_DB if Path(DEFAULT_MARKET_DB).exists() else PROJECT + "/data/market_v2.sqlite3")
POOL_DIR = HERE / "历史下载" / "标的池" if (HERE / "历史下载" / "标的池").exists() else HERE.parent.parent / "数据" / "contracts" / "旧标的池"
LOGS = HERE / "logs"
LOGS.mkdir(parents=True, exist_ok=True)
# 进程内控制入口：写入该文件即让**运行中的生产采集进程**对真实 WS 连接做一次强制
# 断开，用于在不另起进程、复用同一采集服务的前提下留证「重连 + 退避 + 补缺」。
#   文件内容为空  → 只断开一次（正常重连）；
#   文件内容为 N  → 断开并**抑制重连 N 秒**（受控断线，用于制造真实缺口）。
FORCE_RECONNECT_FILE = LOGS / ".force_reconnect"
_WS_HOLD_UNTIL = 0   # ms；> now 时抑制 WS 重连（受控断线验收用）
_WS_HOLD_LOCK = threading.Lock()


def _ws_hold_wait(stop):
    """若处于受控断线窗口，阻塞到窗口结束（可被 stop 中断）。"""
    while not stop.is_set():
        with _WS_HOLD_LOCK:
            remain = (_WS_HOLD_UNTIL - now_ms()) / 1000.0
        if remain <= 0:
            return
        stop.wait(min(remain, 1.0))

BINANCE = ("binance", "um")
OKX = ("okx", "swap")
# 币安 USD-M 组合流。**必须用 `/market/stream`**：官方新市场数据路径。
# 旧地址 `wss://fstream.binance.com/stream` 在本机实测为「握手 101 但无任何数据帧」，
# 而 `/market/stream` 实测 1.88s 即收到真实 `24hrTicker`（E=1791174689617、c=85721.50）。
WS_URL_BINANCE = "wss://fstream.binance.com/market/stream?streams="
# 不带 `?streams=` 的基址：K 线流改用**连接后 SUBSCRIBE 方法**订阅（见下）。
# 为什么必须这样：实测把流名拼进 URL 时，**请求行超过约 16KB 即被 414 拒绝**
#   shard0 URL 16,187 字符 → 101 OK；shard1 URL 16,403 字符 → 414 Request-URI Too Large
#   （16,187 与 16,403 之间就是硬上限）。900 条流（100 标的×9 周期）已 16KB+。
#   改为 SUBSCRIBE 后 URL 恒为 39 字符，单连接可承载 ≤1024 条流（官方上限）。
WS_URL_BINANCE_BASE = "wss://fstream.binance.com/market/stream"
WS_URL_OKX = "wss://ws.okx.com:8443/ws/v5/public"
# 欧易 **K 线频道在 business 端点**，不在 public 端点。
# 实测（2026-10-06，本机）：对 `public` 端点订阅 `candle1m` 全部返回
# `event:error code:60018 ... channel:candle1m doesn't exist`；
# 换 `business` 端点后 1m/5m/15m/30m/1H/4H/12H/1Dutc **均收到真实数据帧**。
WS_URL_OKX_BUSINESS = "wss://ws.okx.com:8443/ws/v5/business"
# 2026-10-13（03 施工单·动作1）：本阈值**只是心跳里的展示标签**
# （`history_space` = "ok" / "paused_no_space"，见 heartbeat_loop 第 2050 行），
# **不拦任何写入** —— `free_gb()` 在本文件内仅此一处调用，无第二处门禁。
# 且 UI 侧完全不消费它：`adapter.py` 只读 `collector_state` 的心跳有效性/counters，
# 前端 `TopStatusBar` 无任何磁盘相关字段。故「UI 启动要求 30GB」并非本常量所致。
# 按用户指令把**展示门槛**由 30.0 降为 1.0，并开放环境变量覆盖（不改代码即可调回）。
# 注意：本常量是模块级硬编码、无热加载入口 ⇒ 生效需一次受控重载（见 collector.sh）。
MIN_FREE_GB = float(os.environ.get("RT_MIN_FREE_GB", "1.0"))
# 币安组合流单连接订阅上限（官方建议 ≤ 200 条流）；每标的占 2 条流。
WS_SYMBOLS_PER_CONN = 100

# ── 实时 K 线（交易所**原生** WS 流）────────────────────────────────────────
# 用户指定顺序（与历史下载外循环一致）：粗 → 细。
# 2026-10-10 用户指令（53 合同）：**删除 1m K 线**（不下载、不订阅、不写库）。
# 1m 从实时 K 线订阅中移除；OI 的 5 分钟 metrics 与**其他所有 K 线周期保持不变**。
# 生效方式：本常量为模块级硬编码、无热加载入口 ⇒ 需一次受控重载（见 collector.sh）。
TF_ORDER_RT = ["1d", "12h", "8h", "4h", "1h", "30m", "15m", "5m"]
# 2026-10-11 用户指令：1m 已**彻底删除**（库内数据 + 全部口径一并摘）。
# 53 合同只改了订阅列表 `TF_ORDER_RT`，本文件仍残留 4 处**可被查表重新启用**的 1m 入口：
#   `TF_MS["1m"]` / `OKX_CANDLE_CHANNEL["1m"]` / `KLINE_WRITE_MIN_INTERVAL_MS["1m"]`
#   / `GAPFILL_1M_WINDOW_MS` 与 `kline_gap_fill` 的 `tf == "1m"` 分支。
# 现已全部移除，使 1m 在本文件内**无任何**查表命中点（既不可订阅、不可写库、不可补缺）。
TF_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000,
         "1h": 3_600_000, "4h": 14_400_000, "8h": 28_800_000,
         "12h": 43_200_000, "1d": 86_400_000}
# 币安 USD-M `<symbol>@kline_<interval>`：1m/5m/15m/30m/1h/4h/8h/12h/1d **均为原生**。
# 实测：`/market/stream` 同时收到 kline_1m / kline_1d / kline_8h / kline_12h（e 均为 "kline"）。
BINANCE_KLINE_INTERVAL = {tf: tf for tf in TF_ORDER_RT}
# 欧易 `candle<bar>` 频道。**8H 实测不存在**（code 60018），故不在表中 ——
# 按用户要求「没有原生 8h 就记原因跳过，不合成、不跨所拼接」。
#
# 2026-10-07 修正（12h UTC 边界一致性，用户独占授权）：
#   12h 原用 `candle12H`，那是 **UTC+8 开盘**（UTC 锚点 04:00/16:00），与币安 12h
#   （UTC 00:00/12:00）以及历史补齐脚本用的 `12Hutc` **边界不一致**，会持续产生
#   混边界旧行。官方同时提供 `candle12Hutc`（WS）与 `bar=12Hutc`（REST，UTC+0 开盘）。
#   官方文档（中文镜像 offline/zh.html）原文：
#     WS channel: "... candle12Hutc candle6Hutc"
#     REST /api/v5/market/candles bar: "UTC+8开盘价k线：[6H/12H/1D/...]
#                                       UTC+0开盘价k线：[6Hutc/12Hutc/1Dutc/...]"
#   故 12h 改用 `candle12Hutc`，与币安/历史补齐同一边界；**仅改 12h，不动其他周期**。
OKX_CANDLE_CHANNEL = {"1d": "candle1Dutc", "12h": "candle12Hutc", "4h": "candle4H",
                      "1h": "candle1H", "30m": "candle30m", "15m": "candle15m",
                      "5m": "candle5m"}
OKX_CANDLE_TF = {v: k for k, v in OKX_CANDLE_CHANNEL.items()}
OKX_KLINE_TFS = [tf for tf in TF_ORDER_RT if tf in OKX_CANDLE_CHANNEL]
# ⚠ WS 频道名带 `candle` 前缀（candle1m），但 **REST `/market/candles` 的 `bar` 参数不带**
#   （bar=1m）。实测把 `bar=candle1m` 传给 REST 会被欧易判为非法参数、响应非成功
#   （网关抛 RejectedRequest）—— 曾导致欧易断线补缺**静默失败**。故单列 REST 口径。
OKX_REST_BAR = {tf: ch[len("candle"):] for tf, ch in OKX_CANDLE_CHANNEL.items()}
# 官方无原生周期的显式记录（跳过，不合成）
NO_NATIVE_TF = {"okx": {"8h": "实测 candle8H 订阅返回 code 60018（频道不存在），"
                              "故跳过；不合成、不跨所拼接"}}
# 单连接覆盖的标的数（K 线流）：币安 100 标的 × 8 周期 = 800 条流（< 官方 1024 上限）。
WS_KLINE_SYMBOLS_PER_CONN = 100
KLINE_FLUSH_INTERVAL = 3.0            # 未收盘临时层落库间隔（秒）
# **按周期分级的写库节流**（秒）。为什么必需：实测币安**每笔成交都推一次当前 K 线**
#   （1000 条流 ≈ 285 msg/s ⇒ 每流约 0.28 msg/s，与周期粗细无关）。
#   全量 4752(币安)+2288(欧易) 条流若每 3 秒全量落库 ≈ 2300 行/秒，
#   会与正在跑的历史批量下载抢写锁。按周期分级后稳态约 600 行/秒，
#   与既有 realtime_quotes 落库（约 814 行/秒）同量级。
#   语义影响：只影响**未收盘临时层**的落库频率（图表新鲜度），不影响已收盘历史。
KLINE_WRITE_MIN_INTERVAL_MS = {"5m": 5_000, "15m": 15_000, "30m": 15_000,
                               "1h": 30_000, "4h": 60_000, "8h": 60_000,
                               "12h": 60_000, "1d": 60_000}
# 归档→实时尾段补缺：所有 REST 经统一网关；每 (exchange,tf) 每轮最多 10 个合约，
# 5 分钟冷却避免币安 6 分片重连时抢占 REST/OI 预算；**所有周期**统一近 7 日桥接窗口，
# 更旧的多年历史仍交给 run11。（1m 已于 2026-10-11 彻底删除，不再有独立 10 日窗口。）
GAPFILL_MAX_CALLS = 10
GAPFILL_COOLDOWN_S = 300
GAPFILL_MAX_WINDOW_MS = 7 * 24 * 60 * 60 * 1000

# ── 公开扩展字段（大户持仓量多空比 + 总持仓名义额）本机快照 ───────────────────
# 用户口径：**采集器**按合理周期抓取并保存**可读快照**，本机 API **只读快照**；
# 前端读取**不触发**任何交易所请求。快照刷新**有界**（只刷页首 N 个标的 + 少量主流），
# 单标上游 **2 次**（core 精简口径：OI 名义额/总量同一行 + 持仓量多空比），
# 与既有 OI/费率/快照工作器**共用同一网关预算**。
# 预算核对（币安 fapi 宿主，网关 budget_units=1200 / cost=10 ⇒ 120 请求/分钟）：
#   既有 OI 全池 528 次/600s ≈ 52.8 请求/分钟；费率历史 ≈ 35；K 线尾段 ≈ 8；
#   本刷新 ≤60 标 × 2 次 / 300s ≤ 24 请求/分钟（**占预算上限 20%**）；合计 ≈ 115/120。
PUBLIC_EXTRAS_HEAD_N = int(os.environ.get("PUBLIC_EXTRAS_HEAD_N", "48"))
# 页首字母序只覆盖列表最前面几屏；主流标的（BTC/ETH…）字母序靠后（实测 BTCUSDT 第 107 位），
# 人工核验最常直接查它们，故**额外**纳入一个有界固定清单（可经环境变量覆盖）。
PUBLIC_EXTRAS_MAJORS = tuple(
    s.strip().upper() for s in os.environ.get(
        "PUBLIC_EXTRAS_MAJORS",
        "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,"
        "TRXUSDT,LTCUSDT,LINKUSDT,AVAXUSDT,DOTUSDT,SUIUSDT").split(",") if s.strip())
PUBLIC_EXTRAS_INTERVAL_S = int(os.environ.get("PUBLIC_EXTRAS_INTERVAL_S", "300"))
PUBLIC_EXTRAS_PERIOD = os.environ.get("PUBLIC_EXTRAS_PERIOD", "1h")
# ── 币安**聪明钱（Smart Signal）**本机快照 ────────────────────────────────────
# 来源 = 币安公开页面自身 bapi（`www.binance.bh/bapi/.../smart-money/signal/*`，无鉴权只读）。
# **独立 host 作用域**：网关按 host 记账，`www.binance.bh` 与 `fapi.binance.com` **互不占预算**
#   ⇒ 聪明钱刷新**不会**抢占 OI/费率/K 线的既有预算（同一 store/租约/冷却/封禁纪律不变）。
# 预算核对（www.binance.bh，budget_units=1200 / cost=10 ⇒ 120 请求/分钟）：
#   单标 ≤3 次上游；默认有界集合 60 标 × 3 = 180 次 / 300s = 36 请求/分钟（**≤30% 上限**）。
SMART_MONEY_INTERVAL_S = int(os.environ.get("SMART_MONEY_INTERVAL_S", "300"))
SMART_MONEY_HEAD_N = int(os.environ.get("SMART_MONEY_HEAD_N", str(PUBLIC_EXTRAS_HEAD_N)))
SMART_MONEY_TIME_RANGE = os.environ.get("SMART_MONEY_TIME_RANGE", "ALL")
# 全池刷新（可选）：`SMART_MONEY_FULL_POOL=1` 时改用**币安全部活动合约**（约 5xx 标）。
SMART_MONEY_FULL_POOL = os.environ.get("SMART_MONEY_FULL_POOL", "0") not in ("0", "", "false")
# 2026-10-08 重构：分频 + 优先 + 心跳 + 有界并发（详见 smart_money_loop docstring）。
#   PRIORITY_MAX / PRIORITY_AGE_S：「页面正在看」标的的优先刷新上限与「够新则跳过」阈值；
#   POOL_BUDGET_S：全池公平轮询的**单轮时间预算**（超时下轮接续，游标不重置）；
#   CONCURRENCY：共享同一网关的工作线程数（预算/限频仍由网关统一执行）；
#   GW_ATTEMPTS / GW_CAP_S：DeferredRequest（本地节流）的耐心等待，避免把节流当失败。
SMART_MONEY_PRIORITY_MAX = int(os.environ.get("SMART_MONEY_PRIORITY_MAX", "48"))
SMART_MONEY_PRIORITY_AGE_S = int(os.environ.get("SMART_MONEY_PRIORITY_AGE_S", "300"))
SMART_MONEY_POOL_BUDGET_S = float(os.environ.get("SMART_MONEY_POOL_BUDGET_S", "420"))
SMART_MONEY_CONCURRENCY = int(os.environ.get("SMART_MONEY_CONCURRENCY", "3"))
SMART_MONEY_GW_ATTEMPTS = int(os.environ.get("SMART_MONEY_GW_ATTEMPTS", "6"))
SMART_MONEY_GW_CAP_S = float(os.environ.get("SMART_MONEY_GW_CAP_S", "30"))
GAPFILL_LIMIT = {"binance": 1000, "okx": 300}

sys.path.insert(0, PROJECT)
from data_hub.gateway import DeferredRequest, ENDPOINTS, Endpoint      # noqa: E402

# ── 币安公开 OI 历史端点：**唯一**能在**同一行**同时给出「总持仓量」与「总持仓名义额(USD)」
# 的公开端点（`/futures/data/openInterestHist` → `sumOpenInterest` + `sumOpenInterestValue`）。
# 与 `public_market_read._register()` 的登记**同口径、幂等**（setdefault，不覆盖既有）。
# 用途：全池 OI 快照的**美元金额**来源；不再逐币只用 `/fapi/v1/openInterest`（那个**没有**美元额）。
ENDPOINTS.setdefault("binance.oi_hist", Endpoint(
    "fapi.binance.com", "/futures/data/openInterestHist",
    frozenset({"symbol", "period", "limit"}), frozenset({"symbol", "period"}), 60_000))

# ── 近端 OI 采样密度（2026-10-11 用户裁决「全池真 5m」） ─────────────────────────
#
# 缺陷与根因（实测，2026-10-11）：
#   · K 线轴是 5m 一格，而 OI 近端只有采集器每轮落 **1** 行；
#     一轮 528 标的实测约 9~10 分钟，加 interval 600s ⇒ 相邻两行中位间隔 **15 分钟**，
#     于是「最近 181 个 5m 槽位里只有 50 根 OI 柱（72% 空槽）」，柱形图看起来就是缺的。
#   · 5m 归档（离线导入）尾部停在 2026-10-09 14:50（逐标的略有差异），之后只能靠 snapshot。
#
# 处置：**同一次请求**多取几根 5m 桶（请求数不变、单请求 payload 仍是十几条小 JSON），
# 把每轮返回的**每一行**都按官方 `timestamp`（5m 桶边界）落库。因为 PK 是
# (exchange,market,symbol,period,ts)，重复桶走 INSERT OR REPLACE，**每天行数上界 =
# 288 桶/标的**（不随 limit 增大而增长）；limit 取 12（覆盖 60 分钟）是为了让
# 「某一轮被限速/抖动整轮跳过」也能自愈——实测确实出现过 60~110 分钟的空档。
#
# 口径纪律（**两种索引口径并存，不是跨周期替代**）：
#   · **已收盘**的 5m 桶 → `period='5m'`。它们与离线归档**同源同周期**
#     （同一个 `/futures/data/openInterestHist?period=5m`），落成 `'5m'` 是**如实标注**，
#     不是冒充；这样 `/oi?period=auto` 的「归档末点」才会持续向前推进，
#     近端不再需要靠 snapshot 段兜底，金字塔预算也不会被 snapshot 段吃光。
#   · **最新那一根**（仍在形成中，未收盘）→ 只写 `period='snapshot'`，**不写 `'5m'`**
#     —— 未收盘的值还会变，不能冒充「已收盘的 5m 归档」。
#   · 同一轮里**最新那一根同时再写一行 `'snapshot'`**（即使它已收盘）：`period='snapshot'`
#     分区是 `/markets`「最新 OI」的取数口径，走部分索引 `ix_oi_hist_snapshot`；
#     若停止写它，`/markets` 的 OI 会冻结，且该查询会退化为 1.27 亿行全表扫描。
#     两行同值、同 `ts`，只是两种**索引口径**并存。
OI_HIST_BUCKETS = 12
OI_5M_MS = 5 * 60 * 1000
_QUOTE_SUFFIXES = ("USDT", "USDC", "BUSD", "FDUSD", "TUSD", "DAI", "BTC", "ETH")


def base_asset_of(symbol):
    """从合约代号粗取标的币（仅供 `symbol_supply.base` 回退匹配用，不做任何换算）。

    交易所侧的真实 base 以 `symbol_pool.base_asset` 为准（读取侧优先用它）；
    这里只是给流通量表留一个可索引的辅助键，取不到就返回原串，不编造。
    """
    for q in _QUOTE_SUFFIXES:
        if symbol.endswith(q) and len(symbol) > len(q):
            return symbol[: -len(q)]
    return symbol



def now_ms():
    return int(time.time() * 1000)


def iso(msv):
    if not msv:
        return None
    return datetime.fromtimestamp(msv / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    # 只写 stdout：脱离会话启动时 stdout 已被重定向到 logs/realtime.log，
    # 再显式追加会**重复一行**（曾出现日志每行两遍）。
    print(f"[{iso(now_ms())}] {msg}", flush=True)


def free_gb():
    st = os.statvfs(str(HERE))
    return st.f_bavail * st.f_frsize / (1024 ** 3)


# ══════════════════════════ 最小 WebSocket 客户端 ══════════════════════════
class WSClient:
    """无第三方依赖的 RFC6455 客户端（仅用 ssl/socket）。

    只做客户端必需的部分：握手、文本帧收发、ping→pong、分片重组、正常关闭。
    """

    def __init__(self, url, timeout=25):
        assert url.startswith("wss://")
        rest = url[len("wss://"):]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.host, self.port = host, int(port or 443)
        self.path = "/" + path
        self.timeout = timeout
        self.sock = None
        self.buf = b""

    def connect(self):
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        ctx = ssl.create_default_context()
        self.sock = ctx.wrap_socket(raw, server_hostname=self.host)
        self.sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {self.path} HTTP/1.1\r\nHost: {self.host}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               f"Sec-WebSocket-Version: 13\r\nUser-Agent: MarketDataHub-Realtime/0.1\r\n\r\n")
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WS 握手被关闭")
            head += chunk
        header, _, rest = head.partition(b"\r\n\r\n")
        if b" 101 " not in header.split(b"\r\n")[0]:
            raise ConnectionError("WS 握手失败：" + header.split(b"\r\n")[0].decode("latin1"))
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if accept.encode() not in header:
            raise ConnectionError("WS 握手校验失败")
        self.buf = rest
        return self

    # ── 帧 ──
    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("WS 连接被对端关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send_text(self, text):
        payload = text.encode()
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _send_control(self, opcode, payload=b""):
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode, 0x80 | len(payload)]) + mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv_text(self):
        """返回一条完整文本消息；控制帧内部处理。超时抛 socket.timeout。"""
        data = b""
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            masked, ln = b2 & 0x80, b2 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if masked else None
            payload = self._read(ln) if ln else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:                       # ping → pong
                self._send_control(0xA, payload)
                continue
            if opcode == 0xA:                       # pong
                continue
            if opcode == 0x8:                       # close
                raise ConnectionError("WS 对端要求关闭")
            data += payload
            if fin:
                return data.decode("utf-8", errors="replace")

    def close(self):
        try:
            if self.sock:
                self._send_control(0x8)
        except Exception:                                          # noqa: BLE001
            pass
        try:
            if self.sock:
                self.sock.close()
        except Exception:                                          # noqa: BLE001
            pass


# ══════════════════════════ 数据库写入 ══════════════════════════
def db():
    import sqlite3
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=15000")
    return con


# ── 按字段分组的写入语义 ────────────────────────────────────────────────────
# 价格 / 标记价 / 费率来自**不同的上游消息**，各有各的事件时间与来源。
# 若共用一个 event_time，mark 或 funding 的局部更新会把价格的时点推新，
# 从而伪造"价格刚刚更新过"。因此：
#   · 每组字段配一个"守卫时间列"（该组的事件时间）；
#   · 只有本行**带该组守卫时间**时才更新该组；
#   · 且**乱序不覆盖**：来older的数据不得覆盖更新的数据（`>=` 才写）。
_GROUPS = [
    ("price", ["last_price", "price_change_pct_24h", "quote_volume_24h", "high_24h", "low_24h",
               "volume_24h_base", "volume_24h_contract", "volume_source", "open24h"],
     "price_event_time", ["price_event_time", "price_received_at", "price_source",
                          "price_transport"]),
    ("mark", ["mark_price"], "mark_event_time", ["mark_event_time", "mark_source"]),
    ("funding", ["funding_rate", "next_funding_time"], "funding_event_time",
     ["funding_event_time", "funding_source"]),
]

_ALL_COLS = ["exchange", "market", "symbol", "unit_note", "updated_at"]
for _n, _f, _g, _t in _GROUPS:
    _ALL_COLS += _f + _t


def _build_quote_sql():
    parts = []
    for _n, fields, guard, times in _GROUPS:
        cond = (f"(excluded.{guard} IS NOT NULL AND "
                f"({guard} IS NULL OR excluded.{guard} >= {guard}))")
        for c in fields + times:
            parts.append(f"  {c}=CASE WHEN {cond} THEN excluded.{c} ELSE {c} END")
    return ("INSERT INTO realtime_quotes(" + ",".join(_ALL_COLS) + ") VALUES("
            + ",".join("?" * len(_ALL_COLS)) + ")"
            " ON CONFLICT(exchange,market,symbol) DO UPDATE SET "
            + ",\n".join(parts)
            + ",\n  unit_note=COALESCE(excluded.unit_note,unit_note),"
              "\n  updated_at=excluded.updated_at")


QUOTE_SQL = _build_quote_sql()


def upsert_quotes(rows):
    """rows: dict 列表，键为 realtime_quotes 列名（缺失列按 NULL）。"""
    if not rows:
        return 0
    with closing(db()) as con:
        con.executemany(QUOTE_SQL, [tuple(r.get(c) for c in _ALL_COLS) for r in rows])
        con.commit()
    return len(rows)


# WS/高频轮询每秒可达上千条消息；逐条落库会把磁盘打满、也会拖垮写入线程。
# 用「按 标的 合并 + 定期批量提交」：同一标的在一个 flush 周期内**合并字段**
# （而不是整行覆盖），否则 mark 消息会把同周期的 price 字段抹掉。
_BUF_LOCK = threading.Lock()
_BUF = {}


def queue_quote(row):
    key = (row["exchange"], row["market"], row["symbol"])
    with _BUF_LOCK:
        cur = _BUF.setdefault(key, {"exchange": row["exchange"], "market": row["market"],
                                    "symbol": row["symbol"]})
        for k, v in row.items():
            if v is not None:              # 只合并有值的字段，避免互相抹除
                cur[k] = v


def flush_quotes():
    with _BUF_LOCK:
        rows = list(_BUF.values())
        _BUF.clear()
    if rows:
        upsert_quotes(rows)
    return len(rows)


def flush_loop(stop, interval=1.0):
    while not stop.is_set():
        try:
            n = flush_quotes()
            if n:
                COUNTERS.add("ws_flushed", n)
        except Exception as exc:                                   # noqa: BLE001
            log(f"WS 缓冲落库失败：{type(exc).__name__}: {str(exc)[:100]}")
        stop.wait(interval)
    flush_quotes()


def upsert_oi(rows):
    if not rows:
        return 0
    with closing(db()) as con:
        con.executemany(
            "INSERT OR REPLACE INTO oi_history(exchange,market,symbol,period,ts,"
            "open_interest,open_interest_value,oi_ccy,oi_usd,source,fetched_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [(r["exchange"], r["market"], r["symbol"], r.get("period") or "snapshot",
              r["ts"], r.get("open_interest"), r.get("open_interest_value"),
              r.get("oi_ccy"), r.get("oi_usd"), r.get("source"), r.get("fetched_at"))
             for r in rows])
        con.commit()
    return len(rows)


def upsert_supply(rows):
    """流通量（`symbol_supply`）UPSERT：每 (交易所,市场,合约) 只保留**当前一行**。

    来源 = 币安 `/futures/data/openInterestHist` 同一行的 `CMCCirculatingSupply`
    （CMC 流通量，**以合约标的币为单位**）。这一行**本来就已经被 `oi_sweep` 取回**，
    此前只是被丢弃；本轮把它如实落库，供「流动市值」列现算，不做任何换算与猜测。

    写库前**丢弃空值**：官方没给就整条不写，绝不用 0 或上一轮值冒充。
    """
    rows = [r for r in rows if r.get("circulating_supply") not in (None, "", "0", "0.0")]
    if not rows:
        return 0
    with closing(db()) as con:
        con.executemany(
            "INSERT OR REPLACE INTO symbol_supply(exchange,market,symbol,base,"
            "circulating_supply,unit_note,source,supply_event_time,fetched_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            [(r["exchange"], r["market"], r["symbol"], r.get("base"),
              r["circulating_supply"], r.get("unit_note"), r.get("source"),
              r.get("supply_event_time"), r.get("fetched_at"), now_ms())
             for r in rows])
        con.commit()
    return len(rows)


def upsert_funding(rows):
    if not rows:
        return 0
    with closing(db()) as con:
        con.executemany(
            "INSERT OR REPLACE INTO funding_history(exchange,market,symbol,funding_time,"
            "funding_rate,mark_price,rate_type,source,fetched_at) VALUES(?,?,?,?,?,?,?,?,?)",
            [(r["exchange"], r["market"], r["symbol"], r["funding_time"], r.get("funding_rate"),
              r.get("mark_price"), r.get("rate_type"), r.get("source"), r.get("fetched_at"))
             for r in rows])
        con.commit()
    return len(rows)


def start_round(kind, exchange=None, market=None, detail=None):
    with closing(db()) as con:
        cur = con.execute("INSERT INTO collector_rounds(kind,exchange,market,started_at,detail)"
                          " VALUES(?,?,?,?,?)", (kind, exchange, market, now_ms(), detail))
        con.commit()
        return cur.lastrowid


def finish_round(rid, total=None, ok=None, failed=None, newest=None, oldest=None, detail=None):
    with closing(db()) as con:
        con.execute("UPDATE collector_rounds SET finished_at=?,symbols_total=?,symbols_ok=?,"
                    "symbols_failed=?,newest_event_time=?,oldest_event_time=?,"
                    "detail=COALESCE(?,detail) WHERE id=?",
                    (now_ms(), total, ok, failed, newest, oldest, detail, rid))
        con.commit()


class Counters:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {"ws_msgs": 0, "ws_reconnects": 0, "rest_quotes": 0, "oi_rows": 0,
                     "funding_rows": 0, "ws_binance": 0, "ws_okx": 0, "errors": 0,
                     "last_error": None}

    def add(self, key, n=1):
        with self.lock:
            self.data[key] = self.data.get(key, 0) + n

    def set(self, key, value):
        with self.lock:
            self.data[key] = value

    def snapshot(self):
        with self.lock:
            return dict(self.data)


COUNTERS = Counters()


# ══════════════════════ REST 通道逐端点记账（健康判据 / 验收取证）══════════════
# 为什么需要：健康检查原先只看「进程存活 + 心跳新鲜」，覆盖不到 **REST 出网通道**。
# 实测教训（2026-10-06）：沙箱会话轮换后，长驻进程持有的 HTTP 代理端口消失，
# 全部 REST 工作器 `URLError`（OI 0/528），而 WS 正常、心跳照写 → 健康检查仍报「健康」。
# 这里把**逐端点成功/失败与最近成功时点**记下来，写进心跳，并纳入 `--health` 判据。
# 只记账，不改任何调用行为（缓存/租约/预算/冷却/封禁仍由 PublicGateway 原样执行）。
REST_STALE_S = 300          # 最近一次成功 REST 超过该秒数即判「REST 通道失效」
REST_STATS_LOCK = threading.Lock()
REST_STATS = {"attempts": 0, "ok": 0, "fail": 0, "deferred": 0, "endpoints": {},
              "last_ok_at": None, "last_ok_endpoint": None,
              "last_error": None, "last_error_at": None, "last_error_endpoint": None}
_REST_WARN_AT = [0]


def _rest_note(name, ok, exc=None):
    """按端点记账。**本地节流不算失败**：
      · `DeferredRequest`（共享预算/最小间隔/同请求租约/来源冷却）→ 计入 `deferred`；
      · 其余异常（网络/HTTP/业务码）→ 计入 `fail`，并记录错误文本。
    这条区分很重要：用户口径「**失败请求不是不可得**」，本地节流更不是通道故障。
    """
    now = now_ms()
    deferred = exc is not None and type(exc).__name__ == "DeferredRequest"
    with REST_STATS_LOCK:
        REST_STATS["attempts"] += 1
        ep = REST_STATS["endpoints"].setdefault(
            name, {"ok": 0, "fail": 0, "deferred": 0, "last_ok_at": None, "last_error": None})
        if ok:
            REST_STATS["ok"] += 1
            REST_STATS["last_ok_at"] = now
            REST_STATS["last_ok_endpoint"] = name
            ep["ok"] += 1
            ep["last_ok_at"] = now
        elif deferred:
            REST_STATS["deferred"] += 1
            ep["deferred"] += 1
        else:
            REST_STATS["fail"] += 1
            text = f"{type(exc).__name__}: {str(exc)[:120]}"
            REST_STATS["last_error"] = f"{name} → {text}"
            REST_STATS["last_error_at"] = now
            REST_STATS["last_error_endpoint"] = name
            ep["fail"] += 1
            ep["last_error"] = text


def _proxy_label():
    """当前传输层实际使用的代理（供取证）：环境代理地址，或「直连（无存活代理）」。"""
    try:
        import gateway_ext
        return gateway_ext.resolve_proxy() or "直连（无存活代理端点）"
    except Exception:                                              # noqa: BLE001
        return None


def rest_stats_snapshot():
    with REST_STATS_LOCK:
        data = json.loads(json.dumps(REST_STATS))
    data["proxy"] = _proxy_label()
    data["last_ok_age_s"] = (round((now_ms() - data["last_ok_at"]) / 1000.0, 1)
                             if data["last_ok_at"] else None)
    return data


class RestGateway:
    """给统一网关套一层**逐端点成功/失败**记账；只记账，不改行为。"""

    def __init__(self, gw):
        self._gw = gw

    def get(self, name, params=None):
        try:
            result = self._gw.get(name, params)
        except Exception as exc:                                   # noqa: BLE001
            _rest_note(name, False, exc)
            raise
        _rest_note(name, True)
        return result

    def __getattr__(self, key):
        return getattr(self._gw, key)


# ══════════════════════════ 标的清单 ══════════════════════════
def load_universe():
    """从目录分类产物读取采集范围。缺文件则明确报错，不静默空跑。"""
    b = json.loads((POOL_DIR / "binance_classified.json").read_text(encoding="utf-8"))
    x = json.loads((POOL_DIR / "cross_exchange_supplement.json").read_text(encoding="utf-8"))
    o = json.loads((POOL_DIR / "okx_classified.json").read_text(encoding="utf-8"))
    return {
        "binance_coins": [c["symbol"] for c in b["coins"]],
        "binance_index": [c["symbol"] for c in b["crypto_index"]],
        "okx_crypto": [c["inst_id"] for c in o["crypto_live_usdt_linear_items"]],
        "okx_supplement": [s["inst_id"] for s in x["supplement"]],
    }


def sync_classification():
    """把目录分类写入 symbol_classification（幂等覆盖）。"""
    b = json.loads((POOL_DIR / "binance_classified.json").read_text(encoding="utf-8"))
    o = json.loads((POOL_DIR / "okx_classified.json").read_text(encoding="utf-8"))
    snap = now_ms()
    rows = []
    for c in b["coins"]:
        rows.append(("binance", "um", c["symbol"], c["underlying_type"], "Crypto",
                     "underlyingType", c["contract_type"], 1, None, c, snap))
    for c in b["crypto_index"]:
        rows.append(("binance", "um", c["symbol"], c["underlying_type"], "Crypto Index",
                     "underlyingType", c["contract_type"], 0,
                     "加密指数：另列指数，不冒充币", c, snap))
    for c in b["excluded"]:
        rows.append(("binance", "um", c["symbol"], c["underlying_type"],
                     {"EQUITY": "Stocks", "KR_EQUITY": "Stocks", "HK_EQUITY": "Stocks",
                      "CN_EQUITY": "Stocks", "PREMARKET": "Stocks",
                      "COMMODITY": "Commodities", "FX": "Forex"}.get(c["underlying_type"], "TradFi"),
                     "underlyingType", c["contract_type"], 0, c.get("excluded_reason"), c, snap))
    for c in b["other"]:
        rows.append(("binance", "um", c["symbol"], c["underlying_type"], "非活动范围",
                     "underlyingType", c["contract_type"], 0, c.get("excluded_reason"), c, snap))
    for c in o["crypto_live_usdt_linear_items"]:
        rows.append(("okx", "swap", c["inst_id"], c["inst_category"], c["inst_category_name"],
                     "instCategory", c["ct_type"], 1, None, c, snap))
    # 欧易**被排除**的目录同样留证（股票/商品/外汇/债券），不只留加密
    live_ids = {c["inst_id"] for c in o["crypto_live_usdt_linear_items"]}
    for c in o["crypto_all"]:
        if c["inst_id"] in live_ids:
            continue
        rows.append(("okx", "swap", c["inst_id"], c["inst_category"], c["inst_category_name"],
                     "instCategory", c["ct_type"], 0,
                     "加密但非 USDT 线性 live（不在活动范围）", c, snap))
    for c in o.get("non_crypto_all") or []:
        rows.append(("okx", "swap", c["inst_id"], c["inst_category"], c["inst_category_name"],
                     "instCategory", c["ct_type"], 0,
                     f"instCategory={c['inst_category']}（{c['inst_category_name']}）："
                     f"按官方分类排除，非加密", c, snap))
    for k, items in (o.get("待核查分类") or {}).items():
        for iid in items:
            rows.append(("okx", "swap", iid, k, "未知", "instCategory", None, 0,
                         f"instCategory={k} 不明分类，列待核查", {}, snap))
    with closing(db()) as con:
        con.executemany(
            "INSERT OR REPLACE INTO symbol_classification(exchange,market,symbol,"
            "instrument_category,category_name,category_field,contract_type,in_active_range,"
            "excluded_reason,raw_json,snapshot_utc,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8],
              json.dumps(r[9], ensure_ascii=False), r[10], snap) for r in rows])
        con.commit()
    return len(rows)


# ══════════════════════════ REST 快照 ══════════════════════════
def gw_get(gw, name, params, attempts=3):
    """带退避重试的网关调用。

    本机出网路径对 `fapi.binance.com` 存在**间歇性连接失败**（实测同一时刻
    `/fapi/v1/ping` 返回 000，而 `/fapi/v1/premiumIndex` 正常返回真实数据）。
    这类失败是连接级抖动，**不是**交易所拒绝，因此：
      · 重试（指数退避），不当作"数据缺失"写库；
      · 只在**真实**收到 403/418/429 时才让网关的封禁/冷却逻辑生效（不由本函数绕过）。
    """
    last = None
    for attempt in range(attempts):
        try:
            return gw.get(name, params)
        except DeferredRequest as exc:
            last = exc
            if attempt == attempts - 1:
                raise
            time.sleep(min(6.0, max(0.5, (exc.retry_at - now_ms()) / 1000.0)))
        except Exception as exc:                                   # noqa: BLE001
            last = exc
            if attempt == attempts - 1:
                raise
            time.sleep(1.0 + attempt)
    raise last


def snapshot_quotes(gw, universe):
    """全市场快照：**5 次批量 REST 覆盖 814 标的**（币安 528 + 欧易 286）。

    最新价 / 24h 涨跌 / 24h 计价成交额 / 标记价 / 当前费率。
    比逐币轮询省 100+ 倍请求，是 WS 不可用时的高频主通道。
    """
    ok_units = {"okx": set(universe["okx_crypto"])}

    # ① 币安 24h ticker（1 次请求覆盖全市场）
    rid = start_round("ticker_rest", "binance", "um", "fapi/v1/ticker/24hr 全市场")
    try:
        r = gw_get(gw, "binance.ticker24h", {})
        want = set(universe["binance_coins"]) | set(universe["binance_index"])
        rows, newest, oldest = [], None, None
        for t in r["数据"]:
            if t.get("symbol") not in want:
                continue
            et = t.get("closeTime")
            newest = max(newest or 0, et or 0)
            oldest = min(oldest or et or 0, et or 0)
            # 币安 priceChangePercent 是**百分数**（"1.23" = 1.23%）；统一存为**比例小数**，
            # 与欧易派生口径一致（adapter 直接作为 price_change_ratio_24h 输出）。
            pcp = t.get("priceChangePercent")
            try:
                pcp = None if pcp in (None, "") else str(float(pcp) / 100.0)
            except (TypeError, ValueError):
                pcp = None
            rows.append(dict(exchange="binance", market="um", symbol=t["symbol"],
                             last_price=t.get("lastPrice"),
                             price_change_pct_24h=pcp,
                             quote_volume_24h=t.get("quoteVolume"),
                             high_24h=t.get("highPrice"), low_24h=t.get("lowPrice"),
                             open24h=t.get("openPrice"),
                             volume_source="quoteVolume（USDT 计价）",
                             price_event_time=et, price_received_at=now_ms(),
                             price_source="binance.fapi", price_transport="rest",
                             unit_note="quote_volume_24h 为计价(USDT)滚动24h成交额",
                             updated_at=now_ms()))
        n = upsert_quotes(rows)
        COUNTERS.add("rest_quotes", n)
        finish_round(rid, len(rows), n, 0, newest, oldest, f"覆盖 {n} 标的")
        log(f"REST 币安 ticker24h：写入 {n} 标的（含 24h 涨跌与计价成交额）")
    except Exception as exc:                                       # noqa: BLE001
        finish_round(rid, detail=f"失败 {type(exc).__name__}: {str(exc)[:120]}")
        COUNTERS.add("errors")
        COUNTERS.set("last_error", f"binance.ticker24h {type(exc).__name__}: {str(exc)[:120]}")
        log(f"REST 币安 ticker24h 失败：{type(exc).__name__}: {str(exc)[:140]}")

    # ② 币安 premiumIndex（1 次请求：标记价 + 当前费率 + 下次结算时间）
    rid = start_round("mark_rest", "binance", "um", "fapi/v1/premiumIndex 全市场")
    try:
        r = gw_get(gw, "binance.premium_index", {})
        want = set(universe["binance_coins"]) | set(universe["binance_index"])
        rows = []
        for t in r["数据"]:
            if t.get("symbol") not in want:
                continue
            # premiumIndex 一次给 标记价 + 当前费率 + 下次结算时间，共用上游 `time`；
            # 但它们是**两组独立字段**，各写各的守卫时点列，互不推进对方时点。
            pt = int(t.get("time") or 0) or None
            rows.append(dict(exchange="binance", market="um", symbol=t["symbol"],
                             mark_price=t.get("markPrice"),
                             mark_event_time=pt, mark_source="binance.fapi",
                             funding_rate=t.get("lastFundingRate"),
                             next_funding_time=t.get("nextFundingTime"),
                             funding_event_time=pt, funding_source="binance.fapi",
                             updated_at=now_ms()))
        n = upsert_quotes(rows)
        finish_round(rid, len(rows), n, 0, None, None, f"标记价/费率覆盖 {n} 标的")
        log(f"REST 币安 premiumIndex：写入 {n} 标的（标记价 + 当前费率）")
    except Exception as exc:                                       # noqa: BLE001
        finish_round(rid, detail=f"失败 {type(exc).__name__}: {str(exc)[:120]}")
        COUNTERS.add("errors")
        log(f"REST 币安 premiumIndex 失败：{type(exc).__name__}: {str(exc)[:140]}")

    # ③ 欧易 全 SWAP tickers（1 次请求）
    rid = start_round("ticker_rest", "okx", "swap", "v5/market/tickers 全 SWAP")
    try:
        r = gw_get(gw, "okx.tickers", {"instType": "SWAP"})
        want = ok_units["okx"]
        rows, newest, oldest = [], None, None
        for t in (r["数据"].get("data") or []):
            if t.get("instId") not in want:
                continue
            et = int(t.get("ts") or 0) or None
            newest = max(newest or 0, et or 0)
            oldest = min(oldest or et or 0, et or 0)
            # 欧易 SWAP ticker.volCcy24h 是**基础币 24h 成交量**，不是 USDT 计价额。
            # 精确计价额欧易此端点不提供 → quote_volume_24h 留 NULL，只存基础币量。
            # 24h 涨跌：欧易不给字段，但可用 (last-open24h)/open24h **有来源地派生**并注明。
            last_v, open_v = t.get("last"), t.get("open24h")
            pct = None
            try:
                if last_v is not None and open_v not in (None, "", "0", 0):
                    pct = str((float(last_v) - float(open_v)) / float(open_v))
            except (TypeError, ValueError):
                pct = None
            rows.append(dict(exchange="okx", market="swap", symbol=t["instId"],
                             last_price=last_v,
                             price_change_pct_24h=pct,
                             high_24h=t.get("high24h"), low_24h=t.get("low24h"),
                             open24h=open_v,
                             volume_24h_base=t.get("volCcy24h"),
                             volume_source="volCcy24h（基础币量）",
                             price_event_time=et, price_received_at=now_ms(),
                             price_source="okx.v5", price_transport="rest",
                             unit_note="volCcy24h 为基础币 24h 成交量（**非** USDT 计价额，"
                                       "精确计价额该端点缺源故留空）；"
                                       "price_change_pct_24h 由 last/open24h 派生（来源 okx tickers）",
                             updated_at=now_ms()))
        n = upsert_quotes(rows)
        COUNTERS.add("rest_quotes", n)
        finish_round(rid, len(rows), n, 0, newest, oldest, f"覆盖 {n} 标的")
        log(f"REST 欧易 tickers：写入 {n} 标的")
    except Exception as exc:                                       # noqa: BLE001
        finish_round(rid, detail=f"失败 {type(exc).__name__}: {str(exc)[:120]}")
        COUNTERS.add("errors")
        log(f"REST 欧易 tickers 失败：{type(exc).__name__}: {str(exc)[:140]}")

    # ④ 欧易 标记价（1 次请求）
    rid = start_round("mark_rest", "okx", "swap", "v5/public/mark-price 全 SWAP")
    try:
        r = gw_get(gw, "okx.mark_price", {"instType": "SWAP"})
        want = ok_units["okx"]
        rows = []
        for t in (r["数据"].get("data") or []):
            if t.get("instId") not in want:
                continue
            rows.append(dict(exchange="okx", market="swap", symbol=t["instId"],
                             mark_price=t.get("markPx"),
                             mark_event_time=int(t.get("ts") or 0) or None,
                             mark_source="okx.v5", updated_at=now_ms()))
        n = upsert_quotes(rows)
        finish_round(rid, len(rows), n, 0, None, None, f"标记价覆盖 {n} 标的")
        log(f"REST 欧易 mark-price：写入 {n} 标的")
    except Exception as exc:                                       # noqa: BLE001
        finish_round(rid, detail=f"失败 {type(exc).__name__}: {str(exc)[:120]}")
        COUNTERS.add("errors")
        log(f"REST 欧易 mark-price 失败：{type(exc).__name__}: {str(exc)[:140]}")

    # ⑤ 欧易 当前费率（1 次请求，instId=ANY）
    rid = start_round("funding_current", "okx", "swap", "v5/public/funding-rate instId=ANY")
    try:
        r = gw_get(gw, "okx.funding_all", {"instId": "ANY"})
        want = ok_units["okx"]
        rows = []
        for t in (r["数据"].get("data") or []):
            if t.get("instId") not in want:
                continue
            rows.append(dict(exchange="okx", market="swap", symbol=t["instId"],
                             funding_rate=t.get("fundingRate"),
                             next_funding_time=int(t.get("nextFundingTime") or 0) or None,
                             funding_event_time=int(t.get("ts") or 0) or None,
                             funding_source="okx.v5", updated_at=now_ms()))
        n = upsert_quotes(rows)
        finish_round(rid, len(rows), n, 0, None, None, f"当前费率覆盖 {n} 标的")
        log(f"REST 欧易 funding-rate：写入 {n} 标的")
    except Exception as exc:                                       # noqa: BLE001
        finish_round(rid, detail=f"失败 {type(exc).__name__}: {str(exc)[:120]}")
        COUNTERS.add("errors")
        log(f"REST 欧易 funding-rate 失败：{type(exc).__name__}: {str(exc)[:140]}")


def rest_snapshot(gw, universe, with_oi=True, with_funding=False):
    """一轮完整 REST：快照 + （可选）OI 与费率历史。"""
    snapshot_quotes(gw, universe)
    if with_oi:
        oi_sweep(gw, universe)
    if with_funding:
        funding_history_sweep(gw, universe)


def ticker_loop(gw, universe, stop, interval_s=30):
    """高频批量快照：默认 30 秒一轮 = 5 次请求 ⇒ 约 10 请求/分钟。

    诚实说明：这是**轮询**，不是推送。真实新鲜度以每行的 event_time 为准，
    不虚称"所有币每秒更新"。OI 全池扫描更慢，其新鲜度由 collector_rounds 单独记录。
    """
    while not stop.is_set():
        try:
            snapshot_quotes(gw, universe)
        except Exception as exc:                                   # noqa: BLE001
            log(f"快照轮询异常：{type(exc).__name__}: {str(exc)[:120]}")
        stop.wait(interval_s)


def ws_probe(universe):
    """探测各交易所 WS 可用性并把结论留证（不静默假装 WS 在跑）。

    实测（2026-10-05，本机出网路径）：
      - **币安** `wss://fstream.binance.com/market/stream?streams=...`：握手 101 后
        约 1.9 秒即收到真实 `24hrTicker`（`E=1791174689617`、`c=85721.50`）→ 可用。
        旧地址 `wss://fstream.binance.com/stream` 在**同一环境**下握手 101 但无数据帧，
        说明是**端点路径**问题，不是环境阻断；已改用官方 `/market/stream`。
      - **欧易** `wss://ws.okx.com:8443/ws/v5/public`：握手 101 后**能收到真实数据帧** → 可用。
    返回 {exchange: {...}}，调用方按所分别决策。
    """
    ev = {}
    for name, url in (("binance", WS_URL_BINANCE + "btcusdt@ticker"),
                      ("okx", WS_URL_OKX)):
        try:
            c = WSClient(url, timeout=8).connect()
            try:
                if name == "okx":
                    c.send_text(json.dumps({"op": "subscribe", "args": [
                        {"channel": "tickers", "instId": "BTC-USDT-SWAP"}]}))
                c.sock.settimeout(8)
                c.recv_text()
                ev[name] = {"handshake": "ok(101)", "frames": "ok", "usable": True}
            except socket.timeout:
                ev[name] = {"handshake": "ok(101)", "frames": "无数据帧（超时）", "usable": False}
            c.close()
        except Exception as exc:                                   # noqa: BLE001
            ev[name] = {"handshake": f"失败 {type(exc).__name__}",
                        "frames": str(exc)[:80], "usable": False}
    rid = start_round("ws_snapshot", detail=json.dumps(ev, ensure_ascii=False))
    finish_round(rid, detail=json.dumps(ev, ensure_ascii=False))
    log(f"WS 可用性探测：{json.dumps(ev, ensure_ascii=False)}")
    return ev


def oi_bucket_rows(symbol, arr, now, fetched_at=None):
    """把一次 `openInterestHist` 的返回切成**两类落库行**（纯函数，便于回归）。

    规则（见 `OI_HIST_BUCKETS` 上方注释的「口径纪律」）：

    · **已收盘**的 5m 桶（判据：`桶起点 + 5m <= now`，即桶的**结束时刻**已经过去）
      → `period='5m'`。它们与离线归档**同源同周期**（同一个
      `/futures/data/openInterestHist?period=5m`），落成 `'5m'` 是如实标注。
    · **最新那一根**（无论是否已收盘）→ **追加**一行 `period='snapshot'`，
      保住 `/markets`「最新 OI」的取数口径（走部分索引 `ix_oi_hist_snapshot`；
      停止写它会让该查询退化为 1.27 亿行全表扫描）。
      两行同值、同 `ts`，只是两种**索引口径**并存，**不是**跨周期替代。
    · 未收盘的桶**不**落 `'5m'`：值还会变，不能冒充「已收盘的归档」。

    返回 `(rows, newest_ts, oldest_ts)`。

    ⚠ 判据必须直接与 `now` 比。曾误写成 `now - OI_5M_MS`，等价于要求
      `桶起点 + 10m <= now`，会把**最近一根已收盘桶**也排除掉
      （靠 60 分钟重叠窗口被下一轮补回，所以不显形，但仍是错的）。
      `test_oi_bucket_rows_*` 三条回归锁死这个边界。
    """
    rows = []
    tss = []
    for row in arr:
        ts = int(row.get("timestamp") or 0) or now
        tss.append(ts)
        if ts + OI_5M_MS > now:
            continue                      # 未收盘：不落 '5m' 归档口径
        qty = row.get("sumOpenInterest")            # 币/张口径总量
        usd = row.get("sumOpenInterestValue")       # 名义额 USD（同一行、同一时点）
        rows.append(dict(exchange="binance", market="um", symbol=symbol, period="5m",
                         ts=ts, open_interest=qty, open_interest_value=usd,
                         oi_ccy=qty, oi_usd=usd, source="binance.fapi.openInterestHist",
                         fetched_at=fetched_at))
    last = arr[-1]
    ts_last = int(last.get("timestamp") or 0) or now
    rows.append(dict(exchange="binance", market="um", symbol=symbol, period="snapshot",
                     ts=ts_last, open_interest=last.get("sumOpenInterest"),
                     open_interest_value=last.get("sumOpenInterestValue"),
                     oi_ccy=last.get("sumOpenInterest"),
                     oi_usd=last.get("sumOpenInterestValue"),
                     source="binance.fapi.openInterestHist", fetched_at=fetched_at))
    return rows, (max(tss) if tss else None), (min(tss) if tss else None)


def oi_sweep(gw, universe):
    """OI 轮询：欧易批量（1 次）；币安逐币（官方无全市场 OI 端点）。"""
    rid = start_round("oi_sweep", "okx", "swap", "v5/public/open-interest 全 SWAP")
    try:
        r = gw_get(gw, "okx.open_interest", {"instType": "SWAP"})
        want = set(universe["okx_crypto"])
        rows, ok_n, newest, oldest = [], 0, None, None
        for t in (r["数据"].get("data") or []):
            if t.get("instId") not in want:
                continue
            ts = int(t.get("ts") or 0) or now_ms()
            newest = max(newest or 0, ts)
            oldest = min(oldest or ts, ts)
            rows.append(dict(exchange="okx", market="swap", symbol=t["instId"],
                             period="snapshot", ts=ts, open_interest=t.get("oi"),
                             open_interest_value=t.get("oiUsd"),
                             oi_ccy=t.get("oiCcy"), oi_usd=t.get("oiUsd"),
                             source="okx.v5", fetched_at=r.get("获取时间UTC毫秒")))
        ok_n = upsert_oi(rows)
        COUNTERS.add("oi_rows", ok_n)
        finish_round(rid, len(rows), ok_n, 0, newest, oldest,
                     "oiCcy 为币口径、oiUsd 为美元口径，两者分别保留")
        log(f"OI 欧易批量：写入 {ok_n} 行（oiCcy/oiUsd 分列）")
    except Exception as exc:                                       # noqa: BLE001
        finish_round(rid, detail=f"失败 {type(exc).__name__}: {str(exc)[:120]}")
        COUNTERS.add("errors")
        log(f"OI 欧易批量失败：{type(exc).__name__}: {str(exc)[:140]}")

    # 币安逐币：预算内串行，失败不伪造。
    # 数据源：`/futures/data/openInterestHist`（period=5m, limit=OI_HIST_BUCKETS）——
    # 官方**唯一**公开的「总持仓名义额(USD)」来源；**同一行**同时给
    # `sumOpenInterest`（总量）、`sumOpenInterestValue`（名义额 USD）与
    # `CMCCirculatingSupply`（CMC 流通量，以合约标的币为单位）。
    # 故数量与金额共用同一行、同一时点，不再出现「有数量、金额为 null」。
    # 失败不写 0、不用数量×价格替代，保留上一轮陈旧值。
    # 每轮取回的**每一根 5m 桶**都落库（见 `OI_HIST_BUCKETS` 注释），使近端 OI 成为真 5m。
    rid = start_round("oi_sweep", "binance", "um",
                      "futures/data/openInterestHist 逐币（同一行给总量+名义额USD+CMC流通量）")
    syms = universe["binance_coins"] + universe["binance_index"]
    rows, supply_rows, ok_n, fail_n, newest, oldest = [], [], 0, 0, None, None
    for i, s in enumerate(syms, 1):
        # 预算被推迟（DeferredRequest）或瞬时网络错误时**等待重试**，不把限额/抖动当成数据缺失
        for attempt in range(3):
            try:
                r = gw.get("binance.oi_hist",
                           {"symbol": s, "period": "5m", "limit": OI_HIST_BUCKETS})
                arr = r["数据"] if isinstance(r["数据"], list) else []
                arr = [x for x in arr if isinstance(x, dict)]
                if not arr:
                    raise ValueError("openInterestHist 空返回")
                fetched = r.get("获取时间UTC毫秒")
                # 切桶逻辑抽成纯函数（见 `oi_bucket_rows`）：这里只负责拼装与累计。
                rows_sym, newest_s, oldest_s = oi_bucket_rows(s, arr, now_ms(), fetched)
                rows.extend(rows_sym)
                if newest_s is not None:
                    newest = max(newest or 0, newest_s)
                if oldest_s is not None:
                    oldest = min(oldest or oldest_s, oldest_s)
                last = arr[-1]
                supply_rows.append(dict(
                    exchange="binance", market="um", symbol=s, base=base_asset_of(s),
                    circulating_supply=last.get("CMCCirculatingSupply"),
                    unit_note="CMCCirculatingSupply（以合约标的币为单位，官方原值）",
                    source="binance.fapi.openInterestHist",
                    supply_event_time=int(last.get("timestamp") or 0) or None,
                    fetched_at=r.get("获取时间UTC毫秒")))
                ok_n += 1
                break
            except DeferredRequest as exc:
                if attempt == 2:
                    fail_n += 1
                    if fail_n <= 3:
                        log(f"OI 币安 {s} 预算推迟未成功：{str(exc)[:80]}")
                else:
                    time.sleep(min(5.0, max(0.5, (exc.retry_at - now_ms()) / 1000.0)))
            except Exception as exc:                               # noqa: BLE001
                if attempt == 2:
                    fail_n += 1
                    if fail_n <= 3:
                        log(f"OI 币安 {s} 失败（已重试 3 次）：{type(exc).__name__}: {str(exc)[:80]}")
                else:
                    time.sleep(1.0 + attempt)                      # 瞬时网络抖动退避
        if i % 50 == 0:
            upsert_oi(rows)
            upsert_supply(supply_rows)
            COUNTERS.add("oi_rows", len(rows))
            log(f"OI 币安进度 {i}/{len(syms)}（已入库 {ok_n}，失败 {fail_n}）")
            rows, supply_rows = [], []
    if rows:
        upsert_oi(rows)
        COUNTERS.add("oi_rows", len(rows))
    if supply_rows:
        n_sup = upsert_supply(supply_rows)
        log(f"流通量（CMCCirculatingSupply）写入 {n_sup} 条")
    finish_round(rid, len(syms), ok_n, fail_n, newest, oldest,
                 "总量(sumOpenInterest)与名义额(sumOpenInterestValue)取自同一行、同一时点；"
                 "失败不写 0、不用数量×价格替代")
    log(f"OI 币安逐币完成：成功 {ok_n}，失败 {fail_n}（来源 openInterestHist，含美元名义额）")


def funding_history_sweep(gw, universe):
    """费率**结算事件**历史（不插值）。币安逐币、欧易逐 instId。"""
    rid = start_round("funding_history", "binance", "um", "fapi/v1/fundingRate 逐币")
    ok_n = fail_n = rows_n = 0
    for s in universe["binance_coins"] + universe["binance_index"]:
        try:
            r = gw_get(gw, "binance.funding_history", {"symbol": s, "limit": 100})
            rows = [dict(exchange="binance", market="um", symbol=s,
                         funding_time=int(t["fundingTime"]), funding_rate=t.get("fundingRate"),
                         mark_price=t.get("markPrice"), rate_type="settled",
                         source="binance.fapi", fetched_at=r.get("获取时间UTC毫秒"))
                    for t in r["数据"] if t.get("fundingTime")]
            rows_n += upsert_funding(rows)
            ok_n += 1
        except Exception as exc:                                   # noqa: BLE001
            fail_n += 1
            if fail_n <= 3:
                log(f"费率历史 币安 {s} 失败：{type(exc).__name__}: {str(exc)[:80]}")
    COUNTERS.add("funding_rows", rows_n)
    finish_round(rid, len(universe["binance_coins"]), ok_n, fail_n, None, None,
                 f"结算事件入库 {rows_n} 行；只写交易所真实返回的结算点，不插值")
    log(f"费率历史 币安完成：成功 {ok_n}，失败 {fail_n}，入库 {rows_n} 行")

    rid = start_round("funding_history", "okx", "swap", "v5/public/funding-rate-history 逐 instId")
    ok_n = fail_n = rows_n = 0
    for s in universe["okx_crypto"]:
        try:
            r = gw_get(gw, "okx.funding_history", {"instId": s, "limit": 100})
            # 语义（2026-10-10 官方实测更正）：`/api/v5/public/funding-rate-history` 是**历史**接口，
            # 其 `fundingRate` 与 `realizedRate` 实测**逐行相等**（返回的都是已结算周期）。
            # 防御性口径：优先取 `realizedRate`（官方定义的**实际**结算费率），缺失才回退
            # `fundingRate`；两者相等时行为与旧实现完全一致（零回归），避免未来接口语义变化时
            # 把**预测**费率当实际结算入库。
            rows = [dict(exchange="okx", market="swap", symbol=s,
                         funding_time=int(t["fundingTime"]),
                         funding_rate=(t.get("realizedRate") if t.get("realizedRate") not in (None, "")
                                       else t.get("fundingRate")),
                         mark_price=None, rate_type="settled", source="okx.v5",
                         fetched_at=r.get("获取时间UTC毫秒"))
                    for t in (r["数据"].get("data") or []) if t.get("fundingTime")]
            rows_n += upsert_funding(rows)
            ok_n += 1
        except Exception as exc:                                   # noqa: BLE001
            fail_n += 1
            if fail_n <= 3:
                log(f"费率历史 欧易 {s} 失败：{type(exc).__name__}: {str(exc)[:80]}")
    COUNTERS.add("funding_rows", rows_n)
    finish_round(rid, len(universe["okx_crypto"]), ok_n, fail_n, None, None,
                 f"结算事件入库 {rows_n} 行")
    log(f"费率历史 欧易完成：成功 {ok_n}，失败 {fail_n}，入库 {rows_n} 行")


# ══════════════════════════ WS 工作线程 ══════════════════════════
def _backoff(attempt):
    return min(60, 2 ** min(attempt, 6))


def _stream_name(sym, suffix):
    """组合流 stream 名：`<小写 symbol><suffix>`。

    真实存在 5 个**中文名合约**（哈基米USDT / 币安人生USDT / 我踏马来了USDT /
    牛来USDT / 龙虾USDT），必须做百分号编码，否则 URL 非法（曾导致下载器
    `UnicodeEncodeError` 崩溃）。纯 ASCII 符号编码后**原样**，行为不变。
    """
    return _urlquote(sym.lower(), safe="") + suffix


def ws_binance_worker(symbols, stop):
    """币安组合流：`/market/stream` 批量订阅 `<sym>@ticker`。

    单个连接只订阅本分片（由 main 按 `WS_SYMBOLS_PER_CONN` 切分），
    避免"只订了前 200 条流、其余标的永远没有 WS"的静默缺口。

    只订 `@ticker`（24hrTicker）：标记价 / 费率由 REST `premiumIndex` 每轮快照覆盖，
    两者**各有独立时点列**，互不推进。组合流消息形如
    `{"stream": "...", "data": {...}}`，按 `data.e` 分派。
    """
    streams = [_stream_name(s, "@ticker") for s in symbols]
    attempt = 0
    while not stop.is_set():
        try:
            client = WSClient(WS_URL_BINANCE + "/".join(streams)).connect()
            log(f"WS 币安已连接（{len(symbols)} 标的 / {len(streams)} 流，/market/stream）")
            attempt = 0
            while not stop.is_set():
                msg = client.recv_text()
                d = json.loads(msg).get("data") or {}
                e = d.get("e")
                if e == "24hrTicker":
                    # `P` 为百分数 → 统一转比例小数（与 REST 口径一致）
                    try:
                        _p = None if d.get("P") in (None, "") else str(float(d["P"]) / 100.0)
                    except (TypeError, ValueError):
                        _p = None
                    queue_quote(dict(exchange="binance", market="um", symbol=d.get("s"),
                                        last_price=d.get("c"),
                                        price_change_pct_24h=_p,
                                        quote_volume_24h=d.get("q"),
                                        high_24h=d.get("h"), low_24h=d.get("l"),
                                        open24h=d.get("o"),
                                        volume_source="quoteVolume（USDT 计价）",
                                        price_event_time=d.get("E"), price_received_at=now_ms(),
                                        price_source="binance.fstream", price_transport="ws",
                                        unit_note="quote_volume_24h 为计价(USDT)滚动24h成交额",
                                        updated_at=now_ms()))
                    COUNTERS.add("ws_msgs")
                    COUNTERS.add("ws_binance")
        except Exception as exc:                                   # noqa: BLE001
            COUNTERS.add("errors")
            COUNTERS.set("last_error", f"binance ws {type(exc).__name__}: {str(exc)[:100]}")
            log(f"WS 币安断开：{type(exc).__name__}: {str(exc)[:100]}；{_backoff(attempt)}s 后重连")
            COUNTERS.add("ws_reconnects")
            attempt += 1
            time.sleep(_backoff(attempt))


def ws_okx_worker(inst_ids, stop):
    """欧易公共频道：tickers + funding-rate，按 instId 分组订阅。"""
    batch = 40
    attempt = 0
    while not stop.is_set():
        try:
            client = WSClient(WS_URL_OKX).connect()
            for i in range(0, len(inst_ids), batch):
                args = ([{"channel": "tickers", "instId": x} for x in inst_ids[i:i + batch]]
                        + [{"channel": "funding-rate", "instId": x} for x in inst_ids[i:i + batch]])
                client.send_text(json.dumps({"op": "subscribe", "args": args}))
                time.sleep(0.25)
            log(f"WS 欧易已连接（{len(inst_ids)} instId × 2 频道）")
            attempt = 0
            last_ping = time.time()
            while not stop.is_set():
                if time.time() - last_ping > 20:
                    client.send_text("ping")
                    last_ping = time.time()
                try:
                    msg = client.recv_text()
                except socket.timeout:
                    continue
                if msg == "pong":
                    continue
                d = json.loads(msg)
                arg = d.get("arg") or {}
                ch = arg.get("channel")
                for t in (d.get("data") or []):
                    if ch == "tickers":
                        # volCcy24h = 基础币量（非 USDT 计价额）→ 只写 volume_24h_base。
                        last_v, open_v = t.get("last"), t.get("open24h")
                        pct = None
                        try:
                            if last_v is not None and open_v not in (None, "", "0", 0):
                                pct = str((float(last_v) - float(open_v)) / float(open_v))
                        except (TypeError, ValueError):
                            pct = None
                        queue_quote(dict(exchange="okx", market="swap",
                                            symbol=t.get("instId"), last_price=last_v,
                                            price_change_pct_24h=pct,
                                            high_24h=t.get("high24h"), low_24h=t.get("low24h"),
                                            open24h=open_v,
                                            volume_24h_base=t.get("volCcy24h"),
                                            volume_source="volCcy24h（基础币量）",
                                            price_event_time=int(t.get("ts") or 0) or None,
                                            price_received_at=now_ms(),
                                            price_source="okx.ws", price_transport="ws",
                                            unit_note="volCcy24h 为基础币 24h 成交量（非 USDT 计价额）；"
                                                      "price_change_pct_24h 由 last/open24h 派生",
                                            updated_at=now_ms()))
                        COUNTERS.add("ws_msgs")
                        COUNTERS.add("ws_okx")
                    elif ch == "funding-rate":
                        queue_quote(dict(exchange="okx", market="swap",
                                            symbol=t.get("instId"),
                                            funding_rate=t.get("fundingRate"),
                                            next_funding_time=int(t.get("nextFundingTime") or 0) or None,
                                            funding_event_time=int(t.get("ts") or 0) or None,
                                            funding_source="okx.ws",
                                            updated_at=now_ms()))
                        COUNTERS.add("ws_msgs")
                        COUNTERS.add("ws_okx")
        except Exception as exc:                                   # noqa: BLE001
            COUNTERS.add("errors")
            COUNTERS.set("last_error", f"okx ws {type(exc).__name__}: {str(exc)[:100]}")
            log(f"WS 欧易断开：{type(exc).__name__}: {str(exc)[:100]}；{_backoff(attempt)}s 后重连")
            COUNTERS.add("ws_reconnects")
            attempt += 1
            time.sleep(_backoff(attempt))


# ══════════════════════════ 实时 K 线：临时层 + 闭合入库 ══════════════════════════
# 纪律（对应用户要求）：
#   · 未收盘 bar 只写 `realtime_klines`（**独立临时层**，供图表），绝不混入已收盘记录。
#   · bar 收盘（币安 k.x=true / 欧易 confirm="1"）后才写 `candle_versions`+`candle_heads`，
#     按 (exchange,market,symbol,timeframe,open_time) **幂等**，且**不覆盖已有行**
#     （已存在的行可能是历史归档或更早的实时值；一律不动）。
#   · 三时点齐全：event_time（交易所事件）/ received_at（本机收到）/ updated_at（落库），
#     另有真实来源 source + transport —— **不能只更新时间戳**。
_KLINE_SQL = (
    "INSERT INTO realtime_klines(exchange,market,symbol,timeframe,open_time,close_time,"
    "open,high,low,close,volume,quote_volume,trade_count,taker_buy_volume,"
    "taker_buy_quote_volume,is_closed,event_time,received_at,source,transport,updated_at) "
    "VALUES(:exchange,:market,:symbol,:timeframe,:open_time,:close_time,:open,:high,:low,"
    ":close,:volume,:quote_volume,:trade_count,:taker_buy_volume,:taker_buy_quote_volume,"
    "0,:event_time,:received_at,:source,:transport,:updated_at) "
    "ON CONFLICT(exchange,market,symbol,timeframe) DO UPDATE SET "
    "open_time=excluded.open_time, close_time=excluded.close_time, open=excluded.open, "
    "high=excluded.high, low=excluded.low, close=excluded.close, volume=excluded.volume, "
    "quote_volume=excluded.quote_volume, trade_count=excluded.trade_count, "
    "taker_buy_volume=excluded.taker_buy_volume, "
    "taker_buy_quote_volume=excluded.taker_buy_quote_volume, is_closed=0, "
    "event_time=excluded.event_time, received_at=excluded.received_at, "
    "source=excluded.source, transport=excluded.transport, updated_at=excluded.updated_at "
    # 乱序不覆盖：更旧的一根不得覆盖更新的未收盘 bar
    "WHERE excluded.open_time >= realtime_klines.open_time")

_KBUF_LOCK = threading.Lock()
_KBUF = {}          # (exchange,market,symbol,tf) -> 未收盘 bar 行
_KCLOSED = {}       # (exchange,market,symbol,tf,open_time) -> 已收盘 bar 行
_KLAST_SIG = {}     # (exchange,market,symbol,tf) -> 上次落库的 OHLCV 签名（变化检测）
_KLAST_WRITE_AT = {}  # (exchange,market,symbol,tf) -> 上次写入临时层的本机时间（节流用）
_KLAST_OPEN = {}    # (exchange,market,symbol,tf) -> 最近一次见到的 open_time（补缺判据）
_KLAST_CLOSED = {}  # (exchange,market,symbol,tf) -> 最近已收盘 bar 的 open_time
_WS_REGISTRY = []   # 活动 WS 连接（仅用于自检时强制断开，触发真实重连/补缺）
_WS_REG_LOCK = threading.Lock()
# 补缺去重：多分片会**同时**重连，若每个分片都对全池做一遍补缺，会互相抢网关预算
# （实测触发 DeferredRequest「本地请求预算已用尽」）。按 (exchange,market,tf) 加冷却，
# 同一周期在冷却窗口内只由第一个分片执行一次，其余直接跳过。
_GAPFILL_LAST = {}
_GAPFILL_LOCK = threading.Lock()
# 从既有 universe 维护补缺候选；避免只补「本进程收到过 WS 帧」的合约。
_KLINE_GAPFILL_SYMBOLS = {"binance": (), "okx": ()}

# 每 (exchange,market,tf) 的链路状态（写 kline_stream_state，供验收逐周期列出）
_KSTATE = {}
_KSTATE_LOCK = threading.Lock()


def _kstate(exchange, market, tf):
    key = (exchange, market, tf)
    with _KSTATE_LOCK:
        st = _KSTATE.get(key)
        if st is None:
            st = {"exchange": exchange, "market": market, "timeframe": tf,
                  "symbols_planned": 0, "symbols_seen": set(), "last_msg_at": None,
                  "last_closed_open_time": None, "last_closed_written_at": None,
                  "last_forming_open_time": None, "closed_written": 0,
                  "gapfill_calls": 0, "gapfill_filled": 0, "gapfill_skipped": 0,
                  "reconnects": 0, "backoff_s": 0, "status": "ok", "detail": None}
            _KSTATE[key] = st
        return st


def _sig_of(row):
    return (row["open_time"], row["open"], row["high"], row["low"], row["close"],
            row["volume"], row["quote_volume"], row["trade_count"])


def queue_kline(row):
    """把**未收盘** bar 放入待落库缓冲（同一 key 只留最新一条）。

    两道乱序防护：
      ① 内容与上次落库相同 → 不重复写；
      ② 该 bar 的 open_time **不大于**已知的最近闭合 bar → 说明它已经收盘
         （或本来就是更旧的一根），**不得**作为"当前未收盘 bar"回灌临时层。
         这一条是必需的：闭合时临时层那一行会被删除，此后若收到乱序的旧帧，
         没有第②道防护就会把一根早已收盘的旧 bar 重新写成"当前未收盘"。
    """
    key = (row["exchange"], row["market"], row["symbol"], row["timeframe"])
    with _KBUF_LOCK:
        if row["open_time"] <= _KLAST_CLOSED.get(key, 0):
            return False
        if _KLAST_SIG.get(key) == _sig_of(row):
            return False                 # 内容无变化 → 不重复写库
        _KBUF[key] = row
    return True


def queue_closed(row):
    """把**已收盘** bar 放入待入库缓冲（幂等去重）。"""
    key = (row["exchange"], row["market"], row["symbol"], row["timeframe"], row["open_time"])
    with _KBUF_LOCK:
        _KCLOSED[key] = row
    return True


def _update_coverage_cache_delta(con, exchange, market, symbol, tf, added_bars, source_added=0):
    """只对已存在的 coverage_cache 单元增量同步闭合入库，不扫大表、不伪造全池覆盖。"""
    if not added_bars:
        return 0
    cached = con.execute(
        "SELECT rows,first_open,last_close,source_count FROM coverage_cache "
        "WHERE exchange=? AND market=? AND symbol=? AND timeframe=?",
        (exchange, market, symbol, tf)).fetchone()
    if not cached:
        return 0       # 无缓存单元时让库存 API 的既有回退路径处理，不用样本造覆盖
    rows = int(cached["rows"] or 0) + len(added_bars)
    first_values = ([int(cached["first_open"])] if cached["first_open"] is not None else [])
    first_values.extend(b["open_time"] for b in added_bars)
    last_values = ([int(cached["last_close"])] if cached["last_close"] is not None else [])
    last_values.extend(b["close_time"] for b in added_bars)
    first, last = min(first_values), max(last_values)
    expected = max(0, (last + 1 - first) // TF_MS[tf])
    con.execute(
        "UPDATE coverage_cache SET rows=?,first_open=?,last_close=?,expected=?,missing=?,"
        "source_count=source_count+?,updated_at=? WHERE exchange=? AND market=? AND symbol=? "
        "AND timeframe=?",
        (rows, first, last, expected, max(0, expected - rows), int(source_added), now_ms(),
         exchange, market, symbol, tf))
    return 1


def _store_closed_bars(con, exchange, market, symbol, tf, bars, src_url, transport):
    """把**已收盘** bar 幂等写入 candle_versions + candle_heads。

    返回 (写入根数, 因已存在而跳过根数)。**绝不覆盖已有行**：
    先查 candle_heads（该 (标的,周期,open_time) 是否已有 head），已有的直接跳过。
    这样历史回填与实时闭合互不覆盖 —— 先到者为准，后到者不动它。
    """
    if not bars:
        return 0, 0
    bars = sorted(bars, key=lambda b: b["open_time"])
    lo, hi = bars[0]["open_time"], bars[-1]["open_time"]
    have = {r[0] for r in con.execute(
        "SELECT open_time FROM candle_heads WHERE exchange=? AND market=? AND symbol=? "
        "AND timeframe=? AND open_time BETWEEN ? AND ?",
        (exchange, market, symbol, tf, lo, hi))}
    todo = [b for b in bars if b["open_time"] not in have]
    skipped = len(bars) - len(todo)
    if not todo:
        return 0, skipped
    now = now_ms()
    text = "\n".join("|".join(str(b[k]) for k in
                              ("open_time", "open", "high", "low", "close",
                               "volume", "quote_volume")) for b in todo)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    source_cur = con.execute(
        "INSERT OR IGNORE INTO sources(exchange,market,symbol,timeframe,source_url,file_path,"
        "sha256,imported_at,downloaded_at,first_open,last_close,row_count) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (exchange, market, symbol, tf, src_url, f"{exchange}:{transport}:kline",
         digest, now, now, todo[0]["open_time"], todo[-1]["close_time"], len(todo)))
    sid = con.execute(
        "SELECT id FROM sources WHERE exchange=? AND market=? AND symbol=? AND timeframe=? "
        "AND sha256=?", (exchange, market, symbol, tf, digest)).fetchone()[0]
    written = 0
    written_bars = []
    for b in todo:
        cur = con.execute(
            "INSERT OR IGNORE INTO candle_versions(exchange,market,symbol,timeframe,open_time,"
            "close_time,open,high,low,close,volume,quote_volume,trade_count,taker_buy_volume,"
            "taker_buy_quote_volume,source_id,available_at,is_closed) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (exchange, market, symbol, tf, b["open_time"], b["close_time"],
             b["open"], b["high"], b["low"], b["close"], b["volume"], b["quote_volume"],
             b["trade_count"], b["taker_buy_volume"], b["taker_buy_quote_volume"], sid,
             # 实时闭合 bar 的 available_at = 本机收到该闭合帧的时间（**当时确实可得**）；
             # 历史回填写 NULL —— 两者语义不同，不互相冒充。
             b.get("available_at")))
        if not cur.rowcount:
            continue                       # 已有同 (key, source_id) 行：不重复插入
        head_cur = con.execute(
            "INSERT INTO candle_heads(exchange,market,symbol,timeframe,open_time,source_id)"
            " VALUES(?,?,?,?,?,?) ON CONFLICT(exchange,market,symbol,timeframe,open_time)"
            " DO NOTHING", (exchange, market, symbol, tf, b["open_time"], sid))
        if not head_cur.rowcount:
            # 并发 writer 先建立了 head；删除本次孤立版本，不改变当前来源。
            con.execute("DELETE FROM candle_versions WHERE exchange=? AND market=? AND symbol=? "
                        "AND timeframe=? AND open_time=? AND source_id=?",
                        (exchange, market, symbol, tf, b["open_time"], sid))
            continue
        written += 1
        written_bars.append(b)
        # 收盘后把该 bar 从临时层移除（它已是已收盘历史）
        con.execute("DELETE FROM realtime_klines WHERE exchange=? AND market=? AND symbol=? "
                    "AND timeframe=? AND open_time=?",
                    (exchange, market, symbol, tf, b["open_time"]))
        with _KBUF_LOCK:
            k = (exchange, market, symbol, tf)
            _KLAST_OPEN[k] = b["open_time"]
            _KLAST_CLOSED[k] = max(_KLAST_CLOSED.get(k, 0), b["open_time"])
    if written_bars:
        _update_coverage_cache_delta(con, exchange, market, symbol, tf, written_bars,
                                     source_added=source_cur.rowcount)
    if exchange == "okx": 
        # 欧易 K 线响应无 taker / trade_count 字段：显式标注缺失，不用 0 冒充真值
        for f, stored, reason, impact in (
                ("taker_buy_volume", "", "欧易 K 线响应无该字段（币安有）", "B 策略数据不足"),
                ("trade_count", "0", "欧易 K 线响应无 trade_count 字段；本列 NOT NULL，"
                                      "写 0 为占位符，非真值", "成交笔数类指标不可用")):
            con.execute("INSERT OR REPLACE INTO candle_field_notes(exchange,market,symbol,"
                        "timeframe,field,stored_as,reason,impact,source,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (exchange, market, symbol, tf, f, stored, reason, impact,
                         "okx.v5/ws-candle", now))
    return written, skipped


def flush_klines():
    """把缓冲落库：① 未收盘 → 临时层；② 已收盘 → candle_*（幂等、不覆盖）。

    未收盘部分按 `KLINE_WRITE_MIN_INTERVAL_MS[timeframe]` 节流：未到间隔的**放回缓冲**
    下次再写（同一 key 只保留最新一条）。这样既保证图表新鲜度，又不让写库速率失控。
    """
    with _KBUF_LOCK:
        open_rows = list(_KBUF.values())
        _KBUF.clear()
        closed_rows = list(_KCLOSED.values())
        _KCLOSED.clear()
    n_open = n_closed = n_skip = 0
    if not open_rows and not closed_rows:
        return 0, 0, 0
    now = now_ms()
    deferred = {}
    with closing(db()) as con:
        for r in open_rows:
            key = (r["exchange"], r["market"], r["symbol"], r["timeframe"])
            if _KLAST_SIG.get(key) == _sig_of(r):
                continue
            miniv = KLINE_WRITE_MIN_INTERVAL_MS.get(r["timeframe"], 3_000)
            if now - _KLAST_WRITE_AT.get(key, 0) < miniv:
                deferred[key] = r
                continue
            con.execute(_KLINE_SQL, r)
            _KLAST_SIG[key] = _sig_of(r)
            _KLAST_WRITE_AT[key] = now
            n_open += 1
        grouped = {}
        for r in closed_rows:
            grouped.setdefault((r["exchange"], r["market"], r["symbol"], r["timeframe"]), []).append(r)
        for (ex, mk, sym, tf), bars in grouped.items():
            src_url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval={tf}"
                       if ex == "binance" else "https://www.okx.com/api/v5/market/candles")
            w, s = _store_closed_bars(con, ex, mk, sym, tf, bars, src_url, "ws")
            n_closed += w
            n_skip += s
            if w:
                st = _kstate(ex, mk, tf)
                st["closed_written"] += w
                st["last_closed_open_time"] = max(b["open_time"] for b in bars)
                st["last_closed_written_at"] = now_ms()
        con.commit()
    # 未到节流间隔的行放回缓冲（不覆盖期间到达的更新行；已收盘的 key 不再回灌）
    with _KBUF_LOCK:
        for key, r in deferred.items():
            if r["open_time"] <= _KLAST_CLOSED.get(key, 0):
                continue
            _KBUF.setdefault(key, r)
    return n_open, n_closed, n_skip


def kline_flush_loop(stop, interval=KLINE_FLUSH_INTERVAL):
    while not stop.is_set():
        try:
            n_open, n_closed, n_skip = flush_klines()
            if n_open:
                COUNTERS.add("kline_open_flushed", n_open)
            if n_closed:
                COUNTERS.add("kline_closed_written", n_closed)
        except Exception as exc:                                   # noqa: BLE001
            COUNTERS.add("errors")
            COUNTERS.set("last_error", f"kline flush {type(exc).__name__}: {str(exc)[:100]}")
            log(f"K 线缓冲落库失败：{type(exc).__name__}: {str(exc)[:120]}")
        stop.wait(interval)
    try:
        flush_klines()
    except Exception:                                              # noqa: BLE001
        pass


def kline_state_loop(stop, interval=30):
    """把每 (exchange,market,timeframe) 的**真实**链路状态周期写入 kline_stream_state。

    不靠 running 记录冒充健康：这里写的是真实 WS 消息时点、闭合入库计数、
    重连次数、退避秒数与补缺结果。
    """
    while not stop.is_set():
        # 进程内控制入口：控制文件出现即对生产 WS 连接做一次真实强制断开。
        try:
            if FORCE_RECONNECT_FILE.exists():
                raw = FORCE_RECONNECT_FILE.read_text().strip()
                FORCE_RECONNECT_FILE.unlink()
                hold = int(raw) if raw.isdigit() else 0
                if hold > 0:
                    global _WS_HOLD_UNTIL
                    with _WS_HOLD_LOCK:
                        _WS_HOLD_UNTIL = now_ms() + hold * 1000
                n = drop_all_ws()
                log(f"收到强制重连指令：已关闭 {n} 条 WS 连接"
                    + (f"，并抑制重连 {hold}s（受控断线）" if hold else "")
                    + "，观察重连与补缺")
        except Exception as exc:                                   # noqa: BLE001
            log(f"强制重连处理失败：{type(exc).__name__}: {str(exc)[:100]}")
        try:
            write_kline_state()
        except Exception as exc:                                   # noqa: BLE001
            log(f"K 线状态写入失败：{type(exc).__name__}: {str(exc)[:100]}")
        stop.wait(interval)


def _register_ws(client):
    with _WS_REG_LOCK:
        _WS_REGISTRY.append(client)


def _unregister_ws(client):
    with _WS_REG_LOCK:
        try:
            _WS_REGISTRY.remove(client)
        except ValueError:
            pass


def drop_all_ws():
    """强制关闭所有活动 WS 连接（仅自检用，用于真实触发重连与补缺）。"""
    with _WS_REG_LOCK:
        clients = list(_WS_REGISTRY)
    for c in clients:
        try:
            c.close()
        except Exception:                                          # noqa: BLE001
            pass
    return len(clients)


def _expected_closed_open(now, tf, exchange="binance"):
    """按交易所原生时区边界推算最近已闭合 bar 的 open_time。

    2026-10-07 修正（12h UTC 边界一致性）：OKX 12h 改用官方 `candle12Hutc`
    （UTC 00:00/12:00 边界），不再使用 UTC+8 开盘的 `candle12H`（旧锚点 04:00）。
    因此 12h 与币安同锚点，无需再对 OKX 12h 加 4 小时偏移；**其他周期不变**。
    """
    iv = TF_MS[tf]
    return (now // iv - 1) * iv


def _last_closed_open(con, exchange, market, symbol, tf):
    """按 candle_heads 主键索引读取该合约/周期最近一根已闭合 bar。"""
    row = con.execute(
        "SELECT open_time FROM candle_heads WHERE exchange=? AND market=? AND symbol=? "
        "AND timeframe=? ORDER BY open_time DESC LIMIT 1",
        (exchange, market, symbol, tf)).fetchone()
    return int(row[0]) if row else None


def kline_gap_fill(exchange, market, tf, gw, max_calls=GAPFILL_MAX_CALLS,
                   src_label="gapfill", symbols=None):
    """把归档尾点与**当前已闭合实时尾段**之间的有限缺口经统一 REST 网关衔接。

    本函数从最新 Binance 官方归档 last_close 之后（统一近 7 日桥接窗口）扫描已存 open_time，
    找到**第一个内部缺口**；不以 MAX(open_time) 误判中间已经连续。更旧的多年历史仍交给
    run11，不在实时网关重做。币安用 startTime/endTime 定位；欧易以 before 游标分页，
    超出 recent 1440 根窗口时切 `history-candles`。REST 回复只按连续已闭合前缀入库；
    官方未返回的时间桶不合成。复用幂等 `_store_closed_bars`，只插库中不存在的 open_time，
    绝不覆盖已有实时/历史 head。
    """
    if tf not in TF_MS or exchange not in ("binance", "okx"):
        return 0, 0, 0
    now = now_ms()
    gkey = (exchange, market, tf)
    with _GAPFILL_LOCK:
        if now - _GAPFILL_LAST.get(gkey, 0) < GAPFILL_COOLDOWN_S * 1000:
            return 0, 0, 0
        _GAPFILL_LAST[gkey] = now

    want = _expected_closed_open(now, tf, exchange)
    iv = TF_MS[tf]
    pool = list(symbols if symbols is not None else _KLINE_GAPFILL_SYMBOLS.get(exchange, ()))
    if not pool:
        # 自检/小工具调用兜底；生产路径由 main() 传入当前真实 universe。
        with _KBUF_LOCK:
            pool = sorted({k[2] for k in _KLAST_OPEN if k[0] == exchange and k[1] == market})
    if not pool:
        return 0, 0, 0

    candidates, old_gap = [], 0
    anchor = 4 * 60 * 60 * 1000 if exchange == "okx" and tf == "12h" else 0
    window_ms = GAPFILL_MAX_WINDOW_MS
    window_start = want - window_ms
    with closing(db()) as con:
        for sym in pool:
            first = con.execute(
                "SELECT open_time FROM candle_heads WHERE exchange=? AND market=? AND symbol=? "
                "AND timeframe=? ORDER BY open_time ASC LIMIT 1",
                (exchange, market, sym, tf)).fetchone()
            onboard_row = con.execute(
                "SELECT onboard_time FROM symbol_pool WHERE exchange=? AND market=? AND symbol=?",
                (exchange, market, sym)).fetchone()
            onboard = int(onboard_row[0]) if onboard_row and onboard_row[0] is not None else None
            if first is None and onboard is None:
                continue       # 无历史/上架时间锚点的合约归历史任务，不在实时桥接中推测上市时间
            archive = None
            if exchange == "binance":
                ar = con.execute(
                    "SELECT MAX(last_close) FROM sources WHERE exchange=? AND market=? AND symbol=? "
                    "AND timeframe=? AND source_url LIKE '%data.binance.vision%'",
                    (exchange, market, sym, tf)).fetchone()
                archive = int(ar[0]) if ar and ar[0] is not None else None
            if archive is not None:
                archive_next = archive + 1
                if archive_next < window_start:
                    old_gap += 1    # 超出实时桥接窗的旧段由 run11 负责
                start_raw = max(window_start, archive_next, onboard or 0)
            else:
                first_open = int(first[0]) if first is not None else 0
                start_raw = max(window_start, onboard or 0, first_open)
            start = ((start_raw - anchor + iv - 1) // iv) * iv + anchor
            if start > want:
                continue
            # 不是只看 MAX(open_time)：Binance 从归档尾端向前找第一个缺口；
            # OKX 按官方 after 分页语义，从最新闭合端向后找最近缺口，避免以 MAX 误判。
            if exchange == "binance":
                have = con.execute(
                    "SELECT open_time FROM candle_heads WHERE exchange=? AND market=? AND symbol=? "
                    "AND timeframe=? AND open_time BETWEEN ? AND ? ORDER BY open_time ASC",
                    (exchange, market, sym, tf, start, want))
                first_gap = start
                for (op,) in have:
                    op = int(op)
                    if op < first_gap:
                        continue
                    if op > first_gap:
                        break
                    first_gap += iv
                if first_gap <= want:
                    candidates.append((want - first_gap, sym, first_gap))
            else:
                have = con.execute(
                    "SELECT open_time FROM candle_heads WHERE exchange=? AND market=? AND symbol=? "
                    "AND timeframe=? AND open_time BETWEEN ? AND ? ORDER BY open_time DESC",
                    (exchange, market, sym, tf, start, want))
                first_gap, expected_open = None, want
                for (op,) in have:
                    op = int(op)
                    if op > expected_open:
                        continue
                    if op < expected_open:
                        first_gap = expected_open
                        break
                    expected_open -= iv
                if first_gap is None and expected_open >= start:
                    first_gap = expected_open
                if first_gap is not None and first_gap >= start:
                    candidates.append((want - first_gap, sym, first_gap))
    candidates.sort(reverse=True)
    selected = candidates[:max(0, int(max_calls))]
    skipped = old_gap + max(0, len(candidates) - len(selected))
    st = _kstate(exchange, market, tf)
    calls = filled = 0
    fetched_at = now_ms()

    for lag_ms, sym, last_open in selected:
        missing_n = max(1, (want - last_open) // iv + 1)
        limit = min(missing_n, GAPFILL_LIMIT[exchange])
        try:
            if exchange == "binance":
                start_ms = last_open
                end_ms = min(want, last_open + (limit - 1) * iv) + iv - 1
                r = gw_get(gw, "binance.candles", {
                    "symbol": sym, "interval": BINANCE_KLINE_INTERVAL[tf],
                    "startTime": start_ms, "endTime": end_ms, "limit": limit})
                raw = r["数据"] or []
                bars = [{"open_time": int(k[0]), "close_time": int(k[6]), "open": k[1],
                         "high": k[2], "low": k[3], "close": k[4], "volume": k[5],
                         "quote_volume": k[7], "trade_count": int(k[8]),
                         "taker_buy_volume": k[9], "taker_buy_quote_volume": k[10],
                         "is_closed": int(k[6]) < fetched_at, "available_at": fetched_at}
                        for k in raw]
                src_url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}"
                           f"&interval={tf}&startTime={start_ms}&endTime={end_ms}")
            else:
                # /market/candles 只覆盖最新 1,440 根；更老但仍在 7 日实时补缺窗内时，
                # 使用同一官方网关登记的 /market/history-candles（limit 上限 300）。
                endpoint = "okx.candles_recent" if missing_n <= 1440 else "okx.candles"
                after_ms = last_open + iv
                params = {"instId": sym, "bar": OKX_REST_BAR[tf],
                          "after": after_ms, "limit": limit}
                r = gw_get(gw, endpoint, params)
                raw = (r["数据"].get("data") or [])
                bars = [{"open_time": int(c[0]), "close_time": int(c[0]) + iv - 1,
                         "open": c[1], "high": c[2], "low": c[3], "close": c[4],
                         "volume": c[6], "quote_volume": c[7], "trade_count": 0,
                         "taker_buy_volume": "", "taker_buy_quote_volume": "",
                         "is_closed": c[8] == "1", "available_at": fetched_at}
                        for c in raw]
                path = "/api/v5/market/candles" if endpoint.endswith("recent") else "/api/v5/market/history-candles"
                src_url = f"https://www.okx.com{path}?instId={sym}&bar={OKX_REST_BAR[tf]}&after={after_ms}"
        except Exception as exc:                                   # noqa: BLE001
            calls += 1
            if calls <= 5:
                log(f"K 线补缺 {exchange}/{tf}/{sym} 拉取失败：{type(exc).__name__}: {str(exc)[:100]}")
            continue
        calls += 1
        # Binance 的 startTime 给出从第一个洞向后的连续片段；OKX after 返回游标之前记录，
        # 从「最新缺洞」向后分页。两者均只取目标闭合窗内、明确已收盘的真实记录，不造空桶。
        if exchange == "binance":
            todo = []
            expected_open = last_open
            for b in sorted(bars, key=lambda item: item["open_time"]):
                if b["open_time"] < expected_open:
                    continue
                if b["open_time"] > expected_open or not b["is_closed"]:
                    break
                if b["open_time"] <= want:
                    todo.append(b)
                    expected_open += iv
        else:
            todo = [b for b in bars if b["is_closed"]
                    and window_start <= b["open_time"] <= last_open]
        if not todo:
            continue
        try:
            with closing(db()) as con:
                w, _s = _store_closed_bars(con, exchange, market, sym, tf, todo, src_url, "rest")
                con.commit()
                latest_after = _last_closed_open(con, exchange, market, sym, tf)
            filled += w
            if latest_after is not None:
                st["closed_written"] += w
                st["last_closed_open_time"] = max(st["last_closed_open_time"] or 0, latest_after)
                if w:
                    st["last_closed_written_at"] = fetched_at
                with _KBUF_LOCK:
                    k = (exchange, market, sym, tf)
                    _KLAST_CLOSED[k] = max(_KLAST_CLOSED.get(k, 0), latest_after)
                    _KLAST_OPEN[k] = max(_KLAST_OPEN.get(k, 0), latest_after)
        except Exception as exc:                                   # noqa: BLE001
            log(f"K 线补缺 {exchange}/{tf}/{sym} 入库失败：{type(exc).__name__}: {str(exc)[:100]}")

    st["gapfill_calls"] += calls
    st["gapfill_filled"] += filled
    st["gapfill_skipped"] += skipped
    if calls or skipped:
        log(f"K 线补缺 {exchange}/{tf}（{src_label}）：REST {calls} 次，写入闭合 {filled} 根，"
            f"预算/旧历史跳过 {skipped} 个合约（统一近7天窗口）")
    return calls, filled, skipped


def _handle_kline(exchange, market, symbol, tf, o, h, l, c, v, q, n, V, Q,
                  open_time, close_time, is_closed, event_time, source):
    """一条原生 K 线消息的统一入口（币安/欧易共用）。"""
    now = now_ms()
    row = dict(exchange=exchange, market=market, symbol=symbol, timeframe=tf,
               open_time=open_time, close_time=close_time, open=o, high=h, low=l, close=c,
               volume=v, quote_volume=q, trade_count=n, taker_buy_volume=V,
               taker_buy_quote_volume=Q, event_time=event_time, received_at=now,
               source=source, transport="ws", updated_at=now, available_at=now)
    st = _kstate(exchange, market, tf)
    st["last_msg_at"] = now
    st["symbols_seen"].add(symbol)
    with _KBUF_LOCK:
        _KLAST_OPEN[(exchange, market, symbol, tf)] = open_time
    if is_closed:
        st["last_closed_open_time"] = max(st["last_closed_open_time"] or 0, open_time)
        with _KBUF_LOCK:
            _KLAST_CLOSED[(exchange, market, symbol, tf)] = max(
                _KLAST_CLOSED.get((exchange, market, symbol, tf), 0), open_time)
        queue_closed(row)
    else:
        st["last_forming_open_time"] = open_time
        queue_kline(row)


def ws_binance_kline_worker(symbols, timeframes, stop, gw=None):
    """币安 USD-M K 线流：`<sym>@kline_<interval>`，**连接后用 SUBSCRIBE 方法**订阅。

    官方约束（本地工程预算内遵守）：
      · 单连接最多 1024 条流 —— 本分片 = 100 标的 × 9 周期 = 900 条流。
      · 单连接入站 10 条/秒 —— 订阅走 5 条 SUBSCRIBE 消息（每 200 条流一条），
        间隔 0.35s，远低于 10 条/秒。
      · 服务端每 3 分钟发 ping，须在 10 分钟内回 pong（WSClient 自动回）。
    """
    streams = []
    for tf in timeframes:
        streams += [_stream_name(s, "@kline_" + BINANCE_KLINE_INTERVAL[tf]) for s in symbols]
    attempt = 0
    while not stop.is_set():
        client = None
        try:
            client = WSClient(WS_URL_BINANCE_BASE).connect()
            _register_ws(client)
            for i in range(0, len(streams), 200):
                client.send_text(json.dumps({"method": "SUBSCRIBE",
                                             "params": streams[i:i + 200],
                                             "id": i // 200 + 1}))
                time.sleep(0.35)
            log(f"WS 币安 K 线已连接（{len(symbols)} 标的 × {len(timeframes)} 周期"
                f" = {len(streams)} 条流，SUBSCRIBE 方法）")
            if attempt:
                for tf in timeframes:
                    st = _kstate("binance", "um", tf)
                    st["reconnects"] += 1
                    st["backoff_s"] = 0
                if gw is not None:
                    for tf in timeframes:
                        kline_gap_fill("binance", "um", tf, gw, src_label="ws_reconnect")
            attempt = 0
            while not stop.is_set():
                msg = client.recv_text()
                d = json.loads(msg).get("data") or {}
                if d.get("e") != "kline":
                    continue
                k = d.get("k") or {}
                tf, sym = k.get("i"), k.get("s")
                if tf not in TF_MS or not sym:
                    continue
                _handle_kline("binance", "um", sym, tf,
                              k.get("o"), k.get("h"), k.get("l"), k.get("c"),
                              k.get("v"), k.get("q"), k.get("n"), k.get("V"), k.get("Q"),
                              int(k.get("t") or 0), int(k.get("T") or 0),
                              bool(k.get("x")), d.get("E"), "binance.fstream")
                COUNTERS.add("ws_msgs")
                COUNTERS.add("ws_binance_kline")
        except Exception as exc:                                   # noqa: BLE001
            COUNTERS.add("errors")
            COUNTERS.set("last_error", f"binance kline ws {type(exc).__name__}: {str(exc)[:100]}")
            log(f"WS 币安 K 线断开：{type(exc).__name__}: {str(exc)[:100]}；"
                f"{_backoff(attempt)}s 后重连")
            COUNTERS.add("ws_reconnects")
            attempt += 1
            for tf in timeframes:
                _kstate("binance", "um", tf)["backoff_s"] = _backoff(attempt)
            time.sleep(_backoff(attempt))
            _ws_hold_wait(stop)          # 受控断线窗口内不重连（默认窗口为 0，立即重连）
        finally:
            if client is not None:
                _unregister_ws(client)
                client.close()


def ws_okx_kline_worker(inst_ids, stop, gw=None):
    """欧易公共 K 线频道（**business 端点**）：`candle<bar>`，按 instId 分组订阅。

    官方约束：
      · 连接建立 3 次/秒（按 IP）——本工作器单连接、退避重连，远低于上限。
      · 单连接 subscribe/unsubscribe/login 合计 480 次/小时 —— 因此**批量**订阅
        （每请求 chunk×周期 条 args），整轮重订阅仅需个位数请求。
      · 单请求频道总长度 ≤ 64 KB —— 本工作器每请求约 40×8=320 条 args（≈16KB），安全。
      · 30 秒无订阅/无数据会被断开 —— 与 tickers 工作器一致，20s 主动 ping。
    欧易**无 8H 原生周期**（实测 code 60018），故不订阅、不合成。
    """
    batch = 40
    attempt = 0
    while not stop.is_set():
        client = None
        try:
            client = WSClient(WS_URL_OKX_BUSINESS).connect()
            _register_ws(client)
            for i in range(0, len(inst_ids), batch):
                chunk = inst_ids[i:i + batch]
                args = [{"channel": OKX_CANDLE_CHANNEL[tf], "instId": x}
                        for x in chunk for tf in OKX_KLINE_TFS]
                client.send_text(json.dumps({"op": "subscribe", "args": args}))
                time.sleep(0.25)
            log(f"WS 欧易 K 线已连接（{len(inst_ids)} instId × {len(OKX_KLINE_TFS)} 周期；"
                f"8h 无原生频道已跳过）")
            if attempt:
                for tf in OKX_KLINE_TFS:
                    _kstate("okx", "swap", tf)["reconnects"] += 1
                    _kstate("okx", "swap", tf)["backoff_s"] = 0
                    if gw is not None:
                        kline_gap_fill("okx", "swap", tf, gw, src_label="ws_reconnect")
            attempt = 0
            last_ping = time.time()
            while not stop.is_set():
                if time.time() - last_ping > 20:
                    client.send_text("ping")
                    last_ping = time.time()
                try:
                    msg = client.recv_text()
                except socket.timeout:
                    continue
                if msg == "pong":
                    continue
                d = json.loads(msg)
                ch = (d.get("arg") or {}).get("channel")
                tf = OKX_CANDLE_TF.get(ch)
                if not tf:
                    continue
                for c in (d.get("data") or []):
                    _handle_kline("okx", "swap", (d.get("arg") or {}).get("instId"), tf,
                                  c[1], c[2], c[3], c[4], c[6], c[7], 0, "", "",
                                  int(c[0]), int(c[0]) + TF_MS[tf] - 1,
                                  c[8] == "1", int(c[0]), "okx.ws")
                    COUNTERS.add("ws_msgs")
                    COUNTERS.add("ws_okx_kline")
        except Exception as exc:                                   # noqa: BLE001
            COUNTERS.add("errors")
            COUNTERS.set("last_error", f"okx kline ws {type(exc).__name__}: {str(exc)[:100]}")
            log(f"WS 欧易 K 线断开：{type(exc).__name__}: {str(exc)[:100]}；"
                f"{_backoff(attempt)}s 后重连")
            COUNTERS.add("ws_reconnects")
            attempt += 1
            for tf in OKX_KLINE_TFS:
                _kstate("okx", "swap", tf)["backoff_s"] = _backoff(attempt)
            time.sleep(_backoff(attempt))
            _ws_hold_wait(stop)          # 受控断线窗口内不重连（默认窗口为 0，立即重连）
        finally:
            if client is not None:
                _unregister_ws(client)
                client.close()


def kline_gapfill_loop(gw, stop, interval_s=300):
    """周期性补缺：把「最近已知 open_time 落后于应闭合时点」的 (标的,周期) 经统一网关补上。

    为什么需要它（不只是重连时补）：重连补缺可能**恰好撞上网关限频**而失败
    （实测欧易 REST 在并发采集下返回非成功响应 → RejectedRequest）。
    失败时 `_KLAST_OPEN` 不会被推进，因此下一轮会**自动重试**，缺口自愈。
    每轮按周期各自最多 `GAPFILL_MAX_CALLS` 次 REST，绝不打满预算、绝不停用其他工作器。
    """
    while not stop.is_set():
        if stop.wait(interval_s):
            return
        for exchange, market, tfs in (("binance", "um", TF_ORDER_RT),
                                      ("okx", "swap", OKX_KLINE_TFS)):
            for tf in tfs:
                if stop.is_set():
                    return
                try:
                    kline_gap_fill(exchange, market, tf, gw, max_calls=10,
                                   src_label="周期补缺")
                except Exception as exc:                           # noqa: BLE001
                    log(f"K 线周期补缺 {exchange}/{tf} 异常：{type(exc).__name__}: {str(exc)[:100]}")


def heartbeat_loop(stop, interval=15):
    while not stop.is_set():
        try:
            snap = COUNTERS.snapshot()
            fg = free_gb()
            hist = "ok" if fg >= MIN_FREE_GB else "paused_no_space"
            snap["free_gb"] = round(fg, 1)
            snap["history_space"] = hist
            # REST 通道逐端点成功/失败与最近成功时点：随心跳落库，供 --health 与验收取证读取
            rest = rest_stats_snapshot()
            snap["rest"] = rest
            age = rest.get("last_ok_age_s")
            if age is not None and age > REST_STALE_S and now_ms() - _REST_WARN_AT[0] > 120000:
                _REST_WARN_AT[0] = now_ms()
                log(f"⚠ REST 通道告警：最近成功已 {age:.0f}s 前（阈值 {REST_STALE_S}s）；"
                    f"传输代理={rest.get('proxy')}；最近错误 {rest.get('last_error')}")
            with closing(db()) as con:
                con.execute(
                    "INSERT INTO collector_state(name,pid,started_at,heartbeat_at,status,"
                    "detail,counters_json,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(name) DO UPDATE SET pid=excluded.pid,heartbeat_at=excluded.heartbeat_at,"
                    "status=excluded.status,detail=excluded.detail,counters_json=excluded.counters_json,"
                    "updated_at=excluded.updated_at",
                    ("realtime", os.getpid(), START_AT, now_ms(), "running",
                     f"历史空间保护：{hist}（剩余 {fg:.1f}GB，阈值 {MIN_FREE_GB}GB）；"
                     f"实时采集不受空间阈值影响，仍报告状态",
                     json.dumps(snap, ensure_ascii=False), now_ms()))
                con.commit()
        except Exception as exc:                                   # noqa: BLE001
            log(f"心跳写入失败：{type(exc).__name__}: {str(exc)[:100]}")
        stop.wait(interval)


def oi_loop(gw, universe, stop, interval_s=600):
    """OI 轮询：**首轮立即执行**，之后按 interval 循环。"""
    while not stop.is_set():
        try:
            oi_sweep(gw, universe)
        except Exception as exc:                                   # noqa: BLE001
            log(f"OI 轮询异常：{type(exc).__name__}: {str(exc)[:120]}")
        if stop.wait(interval_s):
            return


def funding_loop(gw, universe, stop, interval_s=8 * 3600):
    """费率结算事件历史：**首轮立即执行**，之后按 interval 循环。"""
    while not stop.is_set():
        try:
            funding_history_sweep(gw, universe)
        except Exception as exc:                                   # noqa: BLE001
            log(f"费率历史异常：{type(exc).__name__}: {str(exc)[:120]}")
        if stop.wait(interval_s):
            return


def public_extras_head_symbols(universe, n=None):
    """**页首有界集合 ∪ 少量主流标的**（去重、稳定序）。

    工作台列表按 `(exchange, symbol)` 升序，币安全部在前 ⇒ 「首屏/前几屏」
    = 币安标的按 **ASCII 升序**的前 N 个（与 `adapter._v1_markets_compute` 的
    `sorted(keys)` 同一字典序）。再并入 `PUBLIC_EXTRAS_MAJORS`（有界固定清单），
    使人工直接核验 BTC/ETH 等主流标的时也有快照。
    """
    n = PUBLIC_EXTRAS_HEAD_N if n is None else n
    coins = set(universe.get("binance_coins") or ())
    head = sorted(coins)[:max(0, n)]
    seen, out = set(), []
    for s in list(head) + [m for m in PUBLIC_EXTRAS_MAJORS if m in coins]:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def public_extras_loop(gw, universe, stop, interval_s=None):
    """公开扩展字段**本机快照刷新**：**首轮立即执行**，之后按 interval 循环。

    · 复用**同一** `RestGateway`（缓存/预算/租约/冷却/封禁均与既有工作器共享）；
    · 只刷**页首有界集合**（`public_extras_head_symbols`），不扩张到全池；
    · 只写本机可读快照（`public_snapshot`），**不落库、不写历史**；
    · 异常只记日志、不中断其它工作器；`stop` 可即时退出。
    """
    interval = PUBLIC_EXTRAS_INTERVAL_S if interval_s is None else interval_s
    symbols = public_extras_head_symbols(universe)
    log(f"公开扩展快照：目标 {len(symbols)} 个（页首 {PUBLIC_EXTRAS_HEAD_N} ∪ 主流 "
        f"{len(PUBLIC_EXTRAS_MAJORS)}），周期 {interval}s，"
        f"period={PUBLIC_EXTRAS_PERIOD}（单标 2 次上游，core 精简口径）")
    while not stop.is_set():
        try:
            import public_market_read
            stats = public_market_read.refresh_snapshot(
                gw, symbols, period=PUBLIC_EXTRAS_PERIOD,
                extra={"interval_s": interval, "head_n": len(symbols),
                       "head_symbols": symbols[:8]})
            req = stats.get("requests") or {}
            log(f"公开扩展快照刷新：目标 {stats['targets']}，成功 {stats['ok']}，"
                f"跳过 {stats['skipped']}，失败 {stats['failed']}；"
                f"上游请求 {req.get('upstream', 0)}（缓存 {req.get('cached', 0)}，"
                f"本地节流 {req.get('deferred', 0)}），用时 {stats['elapsed_s']}s")
            if stats.get("failures"):
                log(f"公开扩展快照失败样例：{'；'.join(stats['failures'][:5])}")
        except Exception as exc:                                   # noqa: BLE001
            log(f"公开扩展快照刷新异常：{type(exc).__name__}: {str(exc)[:120]}")
        if stop.wait(interval):
            return


def smart_money_symbols(universe, n=None):
    """聪明钱刷新目标：默认**有界**（复用页首 N ∪ 主流）；`SMART_MONEY_FULL_POOL=1` 时全池。"""
    if SMART_MONEY_FULL_POOL:
        return sorted(set(universe.get("binance_coins") or ()))
    return public_extras_head_symbols(universe, n=n)


def smart_money_loop(gw, universe, stop, interval_s=None):
    """币安聪明钱**本机快照刷新**（2026-10-08 重构：分频 + 优先 + 心跳 + 有界并发）。

    · 复用**同一** `RestGateway`（缓存/租约/冷却/封禁与既有工作器共享；
      `www.binance.bh` 为**独立 host 作用域**，不占 fapi 预算）；
    · **分频**（复用 `with_details=False`）：主槽 = overview+stats（≤2 次上游/标），
      是页面六槽的全部依赖；明细 details/list 只对「页面正在看」的标的取（≤1 次/标），
      主槽复刷时旧明细保留并带「截至时点」，不冒充新取；
    · **优先**：`_wanted.json`（页面读取时登记，有界+TTL）里的标的优先刷新
      （快照够新则跳过）；其余全池按**持久游标**公平轮询，时间预算内推进，不饿死；
    · **心跳**：轮内逐标写 `_progress.json` 进度心跳——健康判据据此区分
      「推进中 running」与「真停摆 stalled」，不再用整轮完成时间误报；
    · **有界并发**：`SMART_MONEY_CONCURRENCY` 个工作线程共享同一网关，
      预算/限频/冷却仍由网关统一执行；DeferredRequest 用更耐心等待（不把本地节流
      当失败）；异常只记日志、不中断其它工作器；`stop` 可即时退出。
    """
    interval = SMART_MONEY_INTERVAL_S if interval_s is None else interval_s
    symbols = smart_money_symbols(universe, n=SMART_MONEY_HEAD_N)
    workers = max(1, SMART_MONEY_CONCURRENCY)
    log(f"聪明钱快照：目标 {len(symbols)} 个（"
        f"{'全池' if SMART_MONEY_FULL_POOL else f'页首 {SMART_MONEY_HEAD_N} ∪ 主流'}），"
        f"周期 {interval}s，主槽 overview+stats（单标≤2次上游）、明细仅优先标的，"
        f"并发 {workers}，timeRange={SMART_MONEY_TIME_RANGE}")
    cursor = 0

    def _priority_items():
        """优先集合：页面正在看且快照不够新的标的（有界）。"""
        try:
            import smart_money
            wanted = smart_money.read_wanted()
        except Exception:                                          # noqa: BLE001
            return []
        picked = []
        for ex, mid in wanted:
            if (ex, mid) not in picked:
                picked.append((ex, mid))
        picked = picked[:SMART_MONEY_PRIORITY_MAX]
        keep = []
        for item in picked:
            try:
                import smart_money
                snap = smart_money.read(item[0], item[1])
                ts = int(snap.get("snapshot_refreshed_at_ms") or 0) if snap else 0
            except Exception:                                      # noqa: BLE001
                ts = 0
            age = (now_ms() - ts) / 1000.0 if ts else 1e9
            if age > SMART_MONEY_PRIORITY_AGE_S:
                keep.append(item)
        return keep

    def _progress_cb(done, total, current, phase, last=[0.0]):
        # 心跳节流 ≥2s（轮内持续推进的**真实**信号；线程安全：本回调只在采集线程调）
        if time.monotonic() - last[0] < 2.0:
            return
        last[0] = time.monotonic()
        try:
            import smart_money
            smart_money.write_progress(phase, done, total, current)
        except Exception:                                          # noqa: BLE001
            pass

    while not stop.is_set():
        cycle_t0 = time.monotonic()
        try:
            import smart_money
            pool_budget = SMART_MONEY_POOL_BUDGET_S
            # ① 优先刷新：页面正在看的标的（带明细；快照够新的已被过滤）
            prio = _priority_items()
            if prio:
                smart_money.refresh_snapshot(
                    gw, prio, time_range=SMART_MONEY_TIME_RANGE,
                    with_details=True, max_workers=workers,
                    progress_cb=_progress_cb, attempts=SMART_MONEY_GW_ATTEMPTS,
                    cap_s=SMART_MONEY_GW_CAP_S,
                    extra={"phase": "priority", "priority_n": len(prio)})
            # ② 全池公平轮询：持久游标 + 时间预算，主槽 overview+stats（明细降频）
            rotated = symbols[cursor:] + symbols[:cursor]
            budget = max(30.0, pool_budget)
            stats = smart_money.refresh_snapshot(
                gw, rotated, time_range=SMART_MONEY_TIME_RANGE,
                with_details=False, max_workers=workers,
                progress_cb=_progress_cb, deadline_s=budget,
                attempts=SMART_MONEY_GW_ATTEMPTS, cap_s=SMART_MONEY_GW_CAP_S,
                extra={"phase": "main", "interval_s": interval,
                       "full_pool": SMART_MONEY_FULL_POOL,
                       "head_symbols": symbols[:8]})
            advanced = int(stats.get("completed_n") or 0)
            cursor = (cursor + max(1, advanced)) % max(1, len(symbols))
            req = stats.get("requests") or {}
            log(f"聪明钱快照刷新：本轮推进 {stats.get('completed_n')}/{stats.get('targets')}，"
                f"成功 {stats['ok']}，跳过 {stats['skipped']}，失败 {stats['failed']}；"
                f"上游请求 {req.get('upstream', 0)}（缓存 {req.get('cached', 0)}，"
                f"本地节流 {req.get('deferred', 0)}），用时 {stats['elapsed_s']}s；"
                f"游标推进至 {cursor}/{len(symbols)}"
                + (f"；本轮未扫完，剩余 {stats.get('remaining_n')} 个由下轮接续"
                   if not stats.get("completed_all") else ""))
            if stats.get("failures"):
                log(f"聪明钱快照失败样例：{'；'.join(stats['failures'][:5])}")
            # 轮末心跳：进入休息期（健康判据此间回退「最近一轮完成时点」信号）
            try:
                smart_money.write_progress("idle", len(symbols), len(symbols), None)
            except Exception:                                      # noqa: BLE001
                pass
        except Exception as exc:                                   # noqa: BLE001
            log(f"聪明钱快照刷新异常：{type(exc).__name__}: {str(exc)[:120]}")
        # 休息 = 周期 − 本轮实际用时（下限 20s）：全池轮转不被固定长睡拖慢
        elapsed = time.monotonic() - cycle_t0
        stop.wait(max(20.0, min(interval, interval - elapsed)))


START_AT = now_ms()


def _rest_health(rest):
    """REST 通道健康：无记录=未知（不判失败）；有记录则按最近成功时点判新鲜度。

    注意：`deferred`（本地节流/预算/冷却）**不算通道失败**，只在明细里如实列出。
    """
    if not isinstance(rest, dict) or not rest.get("attempts"):
        return True, "REST 通道：尚无调用记录"
    eps = rest.get("endpoints") or {}
    brief = "；".join(
        f"{k} ok={v.get('ok', 0)}/fail={v.get('fail', 0)}/deferred={v.get('deferred', 0)}"
        for k, v in sorted(eps.items()))
    age = rest.get("last_ok_age_s")
    if age is None:
        return False, (f"REST 通道失效：{rest['attempts']} 次调用**全部失败**；"
                       f"最近错误 {rest.get('last_error')}")
    if age > REST_STALE_S:
        return False, (f"REST 通道失效：最近成功已 {age:.0f}s 前（阈值 {REST_STALE_S}s）；"
                       f"{brief}；最近错误 {rest.get('last_error')}")
    return True, (f"REST 通道正常：最近成功 {age:.0f}s 前；"
                  f"成功 {rest.get('ok', 0)}/失败 {rest.get('fail', 0)}/"
                  f"本地节流 {rest.get('deferred', 0)}；{brief}")


def health_check():
    """采集进程**真实健康**判据：(pid 文件进程存活) 且 (心跳新鲜) 且 (REST 通道新鲜)。

    为什么要三个条件：
      · `realtime.pid` 里的进程可能已死；
      · `collector_state.status='running'` 只是**上次心跳时写的记录**，进程卡死/被杀后它仍然是 running；
      · **心跳本身覆盖不到 REST 出网通道**——实测出现过「WS 正常、心跳照写，但全部 REST
        打到已失效的沙箱代理端口而 `URLError`」的情形，只看前两条会误判为健康。
    返回 (ok, detail)。
    """
    pidf = LOGS / "realtime.pid"
    pid = None
    try:
        pid = int(pidf.read_text().strip())
    except Exception:                                              # noqa: BLE001
        pid = None
    alive = False
    if pid:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            alive = False
    hb = None
    rest = None
    with closing(db()) as con:
        try:
            r = con.execute("SELECT heartbeat_at,counters_json FROM collector_state "
                            "WHERE name='realtime'").fetchone()
            hb = r[0] if r else None
            if r and r[1]:
                rest = (json.loads(r[1]) or {}).get("rest")
        except Exception:                                          # noqa: BLE001
            hb = None
    age = (now_ms() - hb) / 1000.0 if hb else None
    fresh = age is not None and age < 120
    rest_ok, rest_detail = _rest_health(rest)
    detail = (f"pid={pid} 存活={alive}；心跳={iso(hb)}"
              f"（{('%.0fs 前' % age) if age is not None else '无'}）；新鲜={fresh}；{rest_detail}")
    return (alive and fresh and rest_ok), detail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只跑一轮 REST 快照后退出")
    ap.add_argument("--no-ws", action="store_true", help="不启 WS（仅 REST 轮询）")
    ap.add_argument("--no-oi", action="store_true", help="跳过 OI 轮询（快速核验用）")
    ap.add_argument("--no-klines", action="store_true", help="不启实时 K 线 WS 流")
    ap.add_argument("--kline-smoke", type=int, default=0, metavar="SECONDS",
                    help="自检：只跑 K 线链路 SECONDS 秒（含中途强制断线以验证重连与补缺）后退出")
    ap.add_argument("--kline-report", action="store_true",
                    help="只读打印实时 K 线逐周期报告（覆盖/未收盘时点/来源/闭合入库）后退出")
    ap.add_argument("--health", action="store_true",
                    help="只检查采集进程真实健康（进程存活 + 心跳新鲜）后退出：0=健康 1=不健康")
    ap.add_argument("--with-funding-history", action="store_true")
    ap.add_argument("--no-public-extras", action="store_true",
                    help="不启公开扩展字段（大户持仓量多空比 + 总持仓名义额）本机快照刷新")
    ap.add_argument("--public-extras-once", action="store_true",
                    help="只刷一轮公开扩展字段本机快照（受控核验用）后退出")
    ap.add_argument("--no-smart-money", action="store_true",
                    help="不启币安聪明钱本机快照刷新")
    ap.add_argument("--smart-money-once", action="store_true",
                    help="只刷一轮币安聪明钱本机快照（受控核验用）后退出")
    ap.add_argument("--ticker-interval", type=int, default=0,
                    help="REST 批量快照间隔秒；0=自动（WS 可用 300s，否则 30s）")
    ap.add_argument("--oi-interval", type=int, default=600)
    a = ap.parse_args()

    if a.health:
        ok, detail = health_check()
        print(("健康：" if ok else "不健康：") + detail)
        return 0 if ok else 1

    import gateway_ext
    from data_hub.store import MarketStore
    store = MarketStore(DB)
    # 套一层逐端点成功/失败记账（只记账，不改行为），供 --health 与验收取证读取。
    gw = RestGateway(gateway_ext.gateway(store))

    n = sync_classification()
    log(f"目录分类已同步：{n} 行")
    universe = load_universe()
    _KLINE_GAPFILL_SYMBOLS["binance"] = tuple(universe["binance_coins"] + universe["binance_index"])
    # K 线 OKX 严格只采集币安差集；其余 OKX 行情/OI/费率现有采集范围保持不变。
    _KLINE_GAPFILL_SYMBOLS["okx"] = tuple(universe["okx_supplement"])
    log(f"采集范围：币安 {len(universe['binance_coins'])} 币 + {len(universe['binance_index'])} 指数；"
        f"欧易加密 {len(universe['okx_crypto'])}（其中补币安 {len(universe['okx_supplement'])}）")

    if a.kline_report:
        return kline_report()

    if a.smart_money_once:
        import smart_money
        syms = smart_money_symbols(universe, n=SMART_MONEY_HEAD_N)
        stats = smart_money.refresh_snapshot(
            gw, syms, time_range=SMART_MONEY_TIME_RANGE,
            extra={"interval_s": SMART_MONEY_INTERVAL_S, "head_n": len(syms),
                   "full_pool": SMART_MONEY_FULL_POOL, "head_symbols": syms[:8],
                   "mode_run": "once"})
        print(json.dumps(stats, ensure_ascii=False, indent=1))
        return 0

    if a.public_extras_once:
        import public_market_read
        syms = public_extras_head_symbols(universe)
        stats = public_market_read.refresh_snapshot(
            gw, syms, period=PUBLIC_EXTRAS_PERIOD,
            extra={"interval_s": PUBLIC_EXTRAS_INTERVAL_S, "head_n": len(syms),
                   "head_symbols": syms[:8], "mode_run": "once"})
        print(json.dumps(stats, ensure_ascii=False, indent=1))
        return 0

    if a.once:
        rest_snapshot(gw, universe, with_oi=not a.no_oi, with_funding=a.with_funding_history)
        log("单轮 REST 快照完成")
        return 0

    if a.kline_smoke:
        return _kline_smoke(gw, universe, a.kline_smoke)

    rest_snapshot(gw, universe, with_oi=False, with_funding=False)
    stop = threading.Event()
    threads = []
    ws_ev = {"binance": {"usable": False}, "okx": {"usable": False}}
    if not a.no_ws:
        ws_ev = ws_probe(universe)
    if ws_ev.get("binance", {}).get("usable"):
        syms = universe["binance_coins"] + universe["binance_index"]
        for i in range(0, len(syms), WS_SYMBOLS_PER_CONN):
            threads.append(threading.Thread(
                target=ws_binance_worker, args=(syms[i:i + WS_SYMBOLS_PER_CONN], stop),
                daemon=True))
    if ws_ev.get("okx", {}).get("usable"):
        threads.append(threading.Thread(target=ws_okx_worker, args=(universe["okx_crypto"], stop),
                                        daemon=True))
    # ── 实时 K 线（原生 WS 流）：币安全周期分片 + 欧易全周期 ────────────────
    if not a.no_klines:
        bsyms = universe["binance_coins"] + universe["binance_index"]
        # 单连接流数上限 1024：100 标的 × 9 周期 = 900 条流，安全。
        per = WS_KLINE_SYMBOLS_PER_CONN
        for i in range(0, len(bsyms), per):
            threads.append(threading.Thread(
                target=ws_binance_kline_worker,
                args=(bsyms[i:i + per], TF_ORDER_RT, stop, gw), daemon=True))
        for tf in TF_ORDER_RT:
            _kstate("binance", "um", tf)["symbols_planned"] = len(bsyms)
        okx_ksyms = universe["okx_supplement"]
        if okx_ksyms:
            threads.append(threading.Thread(
                target=ws_okx_kline_worker, args=(okx_ksyms, stop, gw), daemon=True))
        for tf in OKX_KLINE_TFS:
            _kstate("okx", "swap", tf)["symbols_planned"] = len(okx_ksyms)
        log(f"实时 K 线流：币安 {len(bsyms)} 标的 × {len(TF_ORDER_RT)} 周期"
            f"（每连接 {per} 标的）；欧易仅补币安缺失的 {len(okx_ksyms)} instId × "
            f"{len(OKX_KLINE_TFS)} 原生周期（8h 无原生频道，跳过）")
    # REST 批量快照：币安 WS 不可用时是**主通道**（30s），可用时降为缺口补齐（300s）。
    # 欧易 WS 已覆盖 tickers/费率，REST 快照仍保留以便与 WS 交叉校验与补缺。
    if a.ticker_interval:
        ticker_interval = a.ticker_interval
    elif ws_ev.get("binance", {}).get("usable"):
        ticker_interval = 300
    else:
        ticker_interval = 30
    log(f"WS 判定：币安={'可用' if ws_ev.get('binance', {}).get('usable') else '不可用(环境限制)'}，"
        f"欧易={'可用' if ws_ev.get('okx', {}).get('usable') else '不可用'}；"
        f"REST 批量快照间隔 {ticker_interval}s（5 次请求覆盖 814 标的）")
    threads.append(threading.Thread(target=ticker_loop,
                                    args=(gw, universe, stop, ticker_interval), daemon=True))
    threads.append(threading.Thread(target=oi_loop, args=(gw, universe, stop, a.oi_interval),
                                    daemon=True))
    threads.append(threading.Thread(target=funding_loop, args=(gw, universe, stop), daemon=True))
    # 公开扩展字段本机快照：只刷页首有界集合，供 API 只读（前端读取不触发外部采集）。
    if not a.no_public_extras:
        threads.append(threading.Thread(target=public_extras_loop, args=(gw, universe, stop),
                                        daemon=True))
    if not a.no_smart_money:
        threads.append(threading.Thread(target=smart_money_loop, args=(gw, universe, stop),
                                        daemon=True))
    threads.append(threading.Thread(target=heartbeat_loop, args=(stop,), daemon=True))
    threads.append(threading.Thread(target=flush_loop, args=(stop,), daemon=True))
    if not a.no_klines:
        threads.append(threading.Thread(target=kline_flush_loop, args=(stop,), daemon=True))
        threads.append(threading.Thread(target=kline_state_loop, args=(stop,), daemon=True))
        threads.append(threading.Thread(target=kline_gapfill_loop, args=(gw, stop), daemon=True))
    for t in threads:
        t.start()
    log(f"实时采集常驻启动：pid={os.getpid()}，线程 {len(threads)} 个")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop.set()
        log("收到中断，退出")
    return 0


def _kline_smoke(gw, universe, seconds):
    """K 线链路自检：跑 `seconds` 秒，中途**强制断开**一次以验证重连+退避+补缺。"""
    stop = threading.Event()
    bsyms = universe["binance_coins"] + universe["binance_index"]
    per = WS_KLINE_SYMBOLS_PER_CONN
    threads = []
    for i in range(0, len(bsyms), per):
        threads.append(threading.Thread(
            target=ws_binance_kline_worker, args=(bsyms[i:i + per], TF_ORDER_RT, stop, gw),
            daemon=True))
    for tf in TF_ORDER_RT:
        _kstate("binance", "um", tf)["symbols_planned"] = len(bsyms)
    okx_ksyms = universe["okx_supplement"]
    if okx_ksyms:
        threads.append(threading.Thread(target=ws_okx_kline_worker,
                                        args=(okx_ksyms, stop, gw), daemon=True))
    for tf in OKX_KLINE_TFS:
        _kstate("okx", "swap", tf)["symbols_planned"] = len(okx_ksyms)
    threads.append(threading.Thread(target=kline_flush_loop, args=(stop,), daemon=True))
    threads.append(threading.Thread(target=kline_state_loop, args=(stop,), daemon=True))
    for t in threads:
        t.start()
    log(f"K 线自检启动：币安 {len(bsyms)} 标的 / 欧易补充集 {len(okx_ksyms)} instId，"
        f"跑 {seconds}s（中途强制断线 1 次）")
    third = max(6, seconds // 3)
    stop.wait(third)
    n = drop_all_ws()
    log(f"K 线自检：已强制关闭 {n} 条 WS 连接，观察重连与补缺")
    stop.wait(seconds - third)
    time.sleep(KLINE_FLUSH_INTERVAL + 1)
    flush_klines()
    write_kline_state()
    log(f"K 线自检结束，计数：{json.dumps(COUNTERS.snapshot(), ensure_ascii=False)}")
    kline_report()
    return 0


def write_kline_state():
    """把内存中的链路状态立即写入 kline_stream_state（供报告/验收读取）。"""
    now = now_ms()
    with _KSTATE_LOCK:
        snaps = [dict(v) for v in _KSTATE.values()]
    if not snaps:
        return 0
    with closing(db()) as con:
        for st in snaps:
            planned, seen = st["symbols_planned"], len(st["symbols_seen"])
            if st["last_msg_at"] is None:
                status = "no_messages"
            elif now - st["last_msg_at"] > 5 * TF_MS[st["timeframe"]] + 120_000:
                status = "stale"
            else:
                status = "ok"
            detail = (f"计划 {planned} 标的 / 已见 {seen}；闭合入库 {st['closed_written']} 根；"
                      f"断线 {st['reconnects']} 次；补缺调用 {st['gapfill_calls']} "
                      f"（成功 {st['gapfill_filled']}，预算上限跳过 {st['gapfill_skipped']}）")
            con.execute(
                "INSERT INTO kline_stream_state(exchange,market,timeframe,symbols_planned,"
                "symbols_seen,last_msg_at,last_closed_open_time,last_closed_written_at,"
                "last_forming_open_time,closed_written,gapfill_calls,gapfill_filled,"
                "gapfill_skipped,reconnects,backoff_s,status,detail,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(exchange,market,timeframe) DO UPDATE SET "
                "symbols_planned=excluded.symbols_planned,"
                "symbols_seen=excluded.symbols_seen,last_msg_at=excluded.last_msg_at,"
                "last_closed_open_time=excluded.last_closed_open_time,"
                "last_closed_written_at=excluded.last_closed_written_at,"
                "last_forming_open_time=excluded.last_forming_open_time,"
                "closed_written=excluded.closed_written,"
                "gapfill_calls=excluded.gapfill_calls,"
                "gapfill_filled=excluded.gapfill_filled,"
                "gapfill_skipped=excluded.gapfill_skipped,"
                "reconnects=excluded.reconnects,backoff_s=excluded.backoff_s,"
                "status=excluded.status,detail=excluded.detail,updated_at=excluded.updated_at",
                (st["exchange"], st["market"], st["timeframe"], planned, seen,
                 st["last_msg_at"], st["last_closed_open_time"], st["last_closed_written_at"],
                 st["last_forming_open_time"], st["closed_written"], st["gapfill_calls"],
                 st["gapfill_filled"], st["gapfill_skipped"], st["reconnects"],
                 st["backoff_s"], status, detail, now))
        con.commit()
    return len(snaps)


def kline_report(symbols=("BTCUSDT", "RLCUSDT", "CRO-USDT-SWAP")):
    """只读报告：逐周期列出**真实**采集覆盖 / 未收盘时点与来源 / 已收盘时点。

    data 全部来自库内真实记录（realtime_klines / candle_heads / kline_stream_state），
    不靠 running 记录冒充健康。
    """
    import sqlite3 as _sq
    now = now_ms()
    with closing(db()) as con:
        con.row_factory = _sq.Row
        log("── 实时 K 线逐周期报告（真实库内记录）──")
        for r in con.execute(
                "SELECT exchange,timeframe,symbols_planned,symbols_seen,last_msg_at,"
                "last_forming_open_time,last_closed_open_time,closed_written,reconnects,"
                "gapfill_calls,gapfill_filled,gapfill_skipped,status "
                "FROM kline_stream_state ORDER BY exchange,timeframe"):
            age = (now - r["last_msg_at"]) / 1000.0 if r["last_msg_at"] else None
            log(f"  {r['exchange']}/{r['timeframe']}：计划 {r['symbols_planned']} 已见 "
                f"{r['symbols_seen']}｜未收盘 open={iso(r['last_forming_open_time'])}"
                f"｜最近闭合 open={iso(r['last_closed_open_time'])}（累计入库 "
                f"{r['closed_written']}）｜消息 {age:.0f}s 前｜断线 {r['reconnects']} "
                f"补缺 {r['gapfill_calls']}/{r['gapfill_filled']}(跳过 {r['gapfill_skipped']})"
                f"｜{r['status']}")
        log("── 临时层实际行数 / 未收盘时点 / 来源 ──")
        for r in con.execute(
                "SELECT exchange,timeframe,COUNT(*) n,MAX(open_time) mx,MAX(received_at) rcv,"
                "MAX(source) src,MAX(transport) tr FROM realtime_klines "
                "GROUP BY exchange,timeframe ORDER BY exchange,timeframe"):
            log(f"  {r['exchange']}/{r['timeframe']}：{r['n']} 行｜最新未收盘 open="
                f"{iso(r['mx'])}｜接收={iso(r['rcv'])}｜来源={r['src']}/{r['tr']}")
        log("── 指定合约的未收盘 bar（真实字段）──")
        for s in symbols:
            for r in con.execute(
                    "SELECT exchange,timeframe,open_time,close_time,open,high,low,close,volume,"
                    "quote_volume,event_time,received_at,source,transport FROM realtime_klines "
                    "WHERE symbol=? ORDER BY timeframe", (s,)):
                log(f"  {s} {r['exchange']}/{r['timeframe']}：open={iso(r['open_time'])} "
                    f"close_time={iso(r['close_time'])} O={r['open']} H={r['high']} "
                    f"L={r['low']} C={r['close']} V={r['volume']}｜事件={iso(r['event_time'])} "
                    f"接收={iso(r['received_at'])}｜{r['source']}/{r['transport']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
