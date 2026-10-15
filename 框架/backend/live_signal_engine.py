#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""信号与持仓接通（2026-10-13）· **实时信号增量引擎**（需 numpy ⇒ 用 freqtrade 解释器运行）。

职责：**复用既有真实结构计算**，持续增量产出信号并**持久化**，供同源只读 API 与界面消费。

复用（**不另造策略**）：
  · 读侧 1h 拼接：`量价信号接入与模拟历史并行43/_warmup43.py`
      （视图 `candles`；`is_closed=1 AND close_time<=T`；`available_at` 非空 ⇒ 真实收到时刻，
       为空 ⇒ 显式假设「官方 K 线于收盘可得」）。
  · 结构确认（arm 无关 Spring/SC）：`_warmup43.structural_confirms_live` → 39 原型冻结状态机。
  · 臂事件（3a/3b/3c/3d）：冻结 `strategies.wx_donchian_wyckoff.run`（入场条件**逐字不变**）。
  · 实时准入（在线候选入口）：`live_pool.qualify_symbol_live`（37 规则的**参数化放宽副本**，
    去掉 90 天上市龄 / 500 万 U 成交额门槛）；**准入下限 = 指标真实最小历史**
    （`live_pool.ADMISSION_MIN_BARS`，当前 26 根），**逐 UTC 日、逐时点列缺项**，
    短历史币保留可用时段。**不再**使用写死的「连续 240 根」准入门槛（2026-10-13 第 2 轮订正）。
    `ASSEMBLY_LOOKBACK_HOURS`（= 冻结策略自带 `warmup_contiguous_bars`）只决定**读多少历史**，
    **不是**准入判据。
  · 提示装配：`_接入43.make_prompts`（绑定 UID/准入/版本；展示与可执行分列）。

持久化：`live/live_signals.sqlite3:live_signal`（幂等键 `signal_uid`）。
  · `origin='live'`：实时窗口内新产生的真实事件；
  · `origin='historical_research'`：**仅**由显式导入器写入的旧研究信号（本引擎不写该值）。

增量策略：按币记录「上次处理到的最后一根已闭合 1h bar 时间」；无新根即跳过
（`live_run:signal_engine_state`）。`--force` 忽略该缓存。

纪律：只读行情库；**不写** `market.sqlite3`/`market_v2.sqlite3`；不下载、不联网、不改策略参数、
不新造臂；G01–G05 正式回测门禁**不参与**本层展示（展示与可执行分列，见 `executable_state`）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

BASE = Path("/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究")
BACKEND = BASE / "后端实现"
ROOT = BASE / "最新唐奇安威科夫接通与收尾_2026-10-11"
W37 = ROOT / "取消成交额硬门槛与375新准入37"
W43 = ROOT / "量价信号接入与模拟历史并行43"
for _p in (str(W43), str(W37), str(BACKEND)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
sys.dont_write_bytecode = True

import live_store as L                                   # noqa: E402
import paper_engine as PE                                # noqa: E402
import live_pool as LP                                   # noqa: E402（在线候选入口 = 当前币池）
import _接入43 as I                                       # noqa: E402
import _warmup43 as W                                    # noqa: E402
from strategies import wx_daily_375 as D                 # noqa: E402
from strategies import wx_config as CFG                  # noqa: E402
from strategies import wx_donchian_wyckoff as WX         # noqa: E402

# ── P3 冷热切流对齐（2026-10-14 生产接通专项）────────────────────────────────
# `_warmup43` 是**冻结模块**，其 `MARKET_DB` 硬编码为**已退役旧库**
# `data/market.sqlite3`（该库 1h 已冻结在 2026-10-13T15:00:00Z）。实时引擎的 1h 读路径
# 全部经 `_warmup43.open_ro()`，于是每轮 `_latest_closed_1h` 都返回同一个 15:00:00Z，
# 与增量水位 `signal_engine_state.last_bar_by_symbol` 相等 ⇒ **526 全跳过、零信号**。
# 按既有纪律「改冻结工具用 importlib + **运行期覆盖常量**」，在此把读库指向
# `live_store.MARKET_DB`（P3 切流后的新热库 `market_v2.sqlite3`），**不编辑冻结文件**。
# 该覆盖对进程内所有 `_warmup43` 使用者生效（含 `live_worker._arm_exits`），因为
# `live_worker` 在 `import live_signal_engine`（本行）之后才使用 `_warmup43`。
if Path(W.MARKET_DB).resolve() != Path(L.MARKET_DB).resolve():
    W.MARKET_DB = Path(L.MARKET_DB)

UTC = dt.timezone.utc
H = 3_600_000
DAY = 24 * H
EXCHANGE = "binance"
ARMS = ("3a", "3b", "3c", "3d")

# ── 数据装配长度（**不是准入门槛**）────────────────────────────────────────────
# 冻结策略 `wx_donchian_wyckoff.run()` 内 `len(seg) <= warmup_contiguous_bars` 的段不产生
# 任何事件 ⇒ 必须至少喂「该值 + 展示窗」根连续有效 1h，否则策略算不出状态。
# 该值是**冻结策略自带参数**（sha 即已注册 run 身份，不可改），只决定「读多少历史」。
# **准入**（谁能进候选）在 `live_pool`，按指标真实最小历史（`ADMISSION_MIN_BARS`）逐日判定。
ASSEMBLY_LOOKBACK_HOURS = int(LP.frozen_signal_params()["warmup_contiguous_bars"])
ADMISSION_MIN_BARS = LP.ADMISSION_MIN_BARS
WINDOW_HOURS = 168
ADMISSION_LOOKBACK_DAYS = 60
STRUCT_ARMS = {"3c": "spring", "3d": "sc"}
STATE_KEY = "signal_engine_state"


def _load_rule37():
    """**仅作参考/等价性对照**加载冻结的 37 规则（在线入口已改用 `live_pool`）。

    保留它是因为「在线放宽」必须能被复核：用 `min_median=5_000_000` 调冻结版可复现旧 375，
    用 `live_pool.qualify_symbol_live` 的放宽参数即当前在线口径（**含**逐指标最小历史准入）。
    """
    spec = importlib.util.spec_from_file_location("rule37", str(W37 / "01_规则37.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _latest_closed_1h(con, symbols) -> dict:
    """每币最后一根已闭合 1h bar 的 open_time（用于增量跳过）。

    **性能**：必须**逐币**走 `candle_heads` 主键前缀（1 ms/币）；对 1.28 亿行做
    `GROUP BY symbol` 实测 42 s（且 `candles` 视图 join 更慢）⇒ 禁止全表聚合。
    """
    out = {}
    for sym in symbols:
        r = con.execute(
            "SELECT MAX(open_time) FROM candle_heads WHERE exchange='binance' AND market='um' "
            "AND symbol=? AND timeframe='1h'", (sym,)).fetchone()
        if r and r[0] is not None:
            out[sym] = int(r[0])
    return out


def read_admission_arrays(con, sym, start_ms, end_ms):
    import numpy as np
    rows = list(con.execute(
        "SELECT open_time,open,high,low,close,volume,quote_volume FROM candles "
        "WHERE exchange='binance' AND market='um' AND symbol=? AND timeframe='1h' "
        "AND open_time>=? AND open_time<? ORDER BY open_time", (sym, int(start_ms), int(end_ms))))
    if not rows:
        return None
    ts = np.array([int(r[0]) for r in rows], dtype=np.int64)
    o = np.array([float(r[1]) for r in rows]); hi = np.array([float(r[2]) for r in rows])
    lo = np.array([float(r[3]) for r in rows]); cl = np.array([float(r[4]) for r in rows])
    vol = np.array([float(r[5]) for r in rows]); q = np.array([float(r[6]) for r in rows])
    return ts, o, hi, lo, cl, vol, q


def arm_events_for(sym, bars, resolved, window_start_ms, T_ms):
    r = WX.run(bars, resolved, arm_id=resolved.get("registered_config_id"),
               identity=(EXCHANGE, sym), require_identity=True)
    out = []
    for e in r["events"]:
        et = int(e.event_time)
        if not (window_start_ms <= et <= T_ms):
            continue
        evd = e.evidence
        out.append({"event_time": et, "confirmed_at": int(e.confirmed_at),
                    "direction": e.direction, "atr": evd.get("atr_signal_bar"),
                    "execution_label": evd.get("execution_label"),
                    "source_masks": evd.get("source_masks"),
                    "event_uid": evd.get("event_uid"),
                    "signal_bar_open_time": evd.get("signal_bar_open_time"),
                    "signal_bar_close_time": evd.get("signal_bar_close_time"),
                    "detection_atr": ((evd.get("spring") or {}).get("detection_atr")
                                      or (evd.get("sc") or {}).get("detection_atr")),
                    "detection_low": (evd.get("spring") or {}).get("detection_low")
                                     or (evd.get("sc") or {}).get("detection_low")})
    return out, {"status": r.get("status"), "events_total": len(r["events"]),
                 "events_in_window": len(out)}


# ── 臂**自身入场事件** → 可执行提示（2026-10-14 专项·任务 2b）─────────────────────
# 缺陷（本窗口核实，真实数据佐证）：`_接入43.make_prompts` 的循环是 `for c in struct_confirms:`
# —— 只遍历**结构证据**（spring/sc）。于是：
#   · 没有 spring/sc 的币 ⇒ **一个提示都不产生**，与臂无关；
#   · 臂的 `base`（**纯唐奇安突破**）入场事件在结构证据里**永不存在** ⇒
#     3a/3b 恒 `display_only`（真实库：3a/3b 各 62 spring + 88 sc，全部 display_only），
#     3c/3d 也丢掉**全部** base 入场（真实库：3c 仅 62 spring 可进场、3d 仅 88 sc）。
# 修法：入场事件以**冻结策略自己的** `events`（`source_masks` ∈ base/spring/sc，
# `execution_label` base 优先）为唯一 `enterable` 来源。**不改任何策略参数、不新增准入门槛**；
# 「不补开启动前历史信号」由 `live_worker` 的水位线保证，本层只如实表达可执行态。
ARM_ENTRY_ROLE = "enterable_arm_entry"


def _arm_entry_prompts(sym, arm, events, *, daily_list_version, data_version,
                       arm_config_version, arm_config_hash, admission_by_day, rank_by_day):
    """把臂的真实入场事件装配成提示（可执行态由准入决定，缺当日名单则显式阻断）。"""
    key = (EXCHANGE, sym)
    out = []
    for e in events:
        masks = list(e.get("source_masks") or [])
        if not masks:
            continue                                   # 无掩码 ⇒ 非入场事件，如实跳过
        # 结构种类**沿用冻结策略自己的执行标签**（base 优先，与 A-12 一致），不另立名目
        kind = e.get("execution_label") or ("base" if "base" in masks
                                            else ("spring" if "spring" in masks else "sc"))
        close_ms = int(e["event_time"])
        open_ms = int(e.get("signal_bar_open_time") or (close_ms - H + 1))
        day = (close_ms // DAY) * DAY
        admitted = (admission_by_day.get(day) or {}).get(key)
        rank = (rank_by_day.get(day) or {}).get(key)
        status, block = "prompt_enterable", None
        if admitted is False:
            status, block = "prompt_blocked_not_admitted", "daily_not_admitted"
        elif admitted is None:
            status, block = "prompt_blocked_missing_daily_list", "missing_daily_list"
        out.append({
            "schema": "w43_accumulation_prompt/v1",
            "symbol": sym, "exchange": EXCHANGE, "timeframe": "1h",
            "structural_kind": kind,
            "structural_event_uid": e.get("event_uid"),
            "detect_open_time": None, "detect_open_time_utc": None,
            "confirm_bar_open_time": open_ms,
            "confirm_bar_close_time": close_ms,
            "confirm_bar_close_utc": _iso(close_ms),
            "effective_at": "confirm_bar_close",
            "backfilled_to_detect_bar": False,
            "detection_low": e.get("detection_low"),
            "detection_atr": e.get("detection_atr"),
            "arm": arm,
            "arm_config_version": arm_config_version, "arm_config_hash": arm_config_hash,
            "signal_present": True,
            "signal_event_uid": e.get("event_uid"),
            "matched_signal_event_uid": e.get("event_uid"),
            "execution_label": kind,
            "source_masks": masks,
            "admission": admitted, "rank": rank,
            "daily_list_version": daily_list_version, "data_version": data_version,
            "stage": "live",
            "role": ARM_ENTRY_ROLE,
            "status": status,
            "paper_fill_policy": "paper_engine_v1",
            "paper_fill": None, "paper_block_reason": block,
            # 已批准止损 = 实际入场价 − 3×信号 ATR（锚 = 信号 bar 的 ATR）
            "atr_signal": e.get("atr"),
        })
    return out


def _iso(t):
    if t is None:
        return None
    return dt.datetime.fromtimestamp(int(t) / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 16), b""):
            h.update(c)
    return h.hexdigest()


# ── 写库（幂等 upsert）────────────────────────────────────────────────────────
def upsert_prompts(prompts, *, data_version, daily_list_version) -> dict:
    con = L.live_conn()
    now = L.now_ms()
    n_new = n_upd = 0
    try:
        for p in prompts:
            kind = p["structural_kind"]
            arm = p["arm"]
            sig_uid = f"structure_prototype_39|{kind}|{p['structural_event_uid']}|{arm}"
            status = p["status"]
            # 展示态：**恒可展示**（真实事件即展示）；可执行态：单独表达，不受正式回测门禁影响
            display_state = "displayable"
            if status == "prompt_enterable":
                exec_state, exec_reason = "enterable", None
            elif status == "prompt_display_only":
                exec_state, exec_reason = "display_only_same_instant", "该臂结构仅展示，不改变入场条件"
            elif status == "prompt_blocked_no_signal":
                exec_state, exec_reason = "blocked_no_signal", "同刻无该臂信号"
            elif status == "prompt_blocked_not_admitted":
                exec_state, exec_reason = "blocked_not_admitted", "当日未准入"
            elif status == "prompt_blocked_missing_daily_list":
                exec_state, exec_reason = "blocked_missing_daily_list", "缺当日名单"
            else:
                exec_state, exec_reason = "not_this_arm_structure", "非该臂结构"
            row = (
                sig_uid, "structure_prototype_39", "live", EXCHANGE, "um", p["symbol"], "1h",
                arm, p.get("arm_config_version"), p.get("arm_config_hash"), data_version,
                kind, "long",                                  # 已批准只多
                int(p["confirm_bar_close_time"]),              # event_time = 确认 bar 收盘
                int(p["confirm_bar_close_time"]),              # confirmed_at
                int(p["confirm_bar_close_time"]),              # available_at（生效即收盘时刻）
                int(p["confirm_bar_open_time"]), int(p["confirm_bar_close_time"]),
                p.get("detection_low"), p.get("detection_atr"),
                display_state, exec_state, exec_reason,
                (None if p.get("admission") is None else int(bool(p["admission"]))),
                p.get("rank"), daily_list_version,
                json.dumps(p, ensure_ascii=False, default=str), now, now)
            exists = con.execute("SELECT 1 FROM live_signal WHERE signal_uid=?", (sig_uid,)).fetchone()
            con.execute(
                "INSERT INTO live_signal(signal_uid,source,origin,exchange,market,symbol,timeframe,"
                "strategy_key,strategy_version,config_hash,data_version,structural_kind,direction,"
                "event_time,confirmed_at,available_at,bar_open_time,bar_close_time,detection_low,"
                "detection_atr,display_state,executable_state,executable_reason,admission,rank,"
                "daily_list_version,payload_json,first_seen_at,updated_at) "
                "VALUES(" + ",".join("?" * 29) + ") "
                "ON CONFLICT(signal_uid) DO UPDATE SET "
                "executable_state=excluded.executable_state, executable_reason=excluded.executable_reason,"
                "admission=excluded.admission, rank=excluded.rank, "
                "daily_list_version=excluded.daily_list_version, payload_json=excluded.payload_json,"
                "data_version=excluded.data_version, updated_at=excluded.updated_at", row)
            if exists:
                n_upd += 1
            else:
                n_new += 1
        con.commit()
    finally:
        con.close()
    return {"new": n_new, "updated": n_upd, "total_written": n_new + n_upd}


def import_historical_research(path: Path, *, limit: int = 0) -> dict:
    """把旧研究提示流**显式**导入为 `origin='historical_research'`（不冒充当前）。"""
    if not path.exists():
        return {"imported": 0, "reason": "file_missing", "path": str(path)}
    con = L.live_conn()
    now = L.now_ms()
    n = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            p = json.loads(line)
            kind = p["structural_kind"]; arm = p["arm"]
            uid = f"historical|{kind}|{p['structural_event_uid']}|{arm}"
            # **历史研究信号恒不可执行**：它们是 43 窗口的历史事实，不是当前可交易信号。
            # 若沿用 `prompt_enterable → enterable`，会与实时信号混淆（并可能被页面误开仓）。
            # 故统一置为专用态 `historical_not_executable`，只展示、不参与任何下单判定。
            exec_state = "historical_not_executable"
            con.execute(
                "INSERT OR IGNORE INTO live_signal(signal_uid,source,origin,exchange,market,symbol,"
                "timeframe,strategy_key,strategy_version,config_hash,data_version,structural_kind,"
                "direction,event_time,confirmed_at,available_at,bar_open_time,bar_close_time,"
                "detection_low,detection_atr,display_state,executable_state,executable_reason,"
                "admission,rank,daily_list_version,payload_json,first_seen_at,updated_at) "
                "VALUES(" + ",".join("?" * 29) + ")",
                (uid, "structure_prototype_39", "historical_research", p["exchange"], "um",
                 p["symbol"], "1h", arm, p.get("arm_config_version"), p.get("arm_config_hash"),
                 p.get("data_version"), kind, "long",
                 int(p["confirm_bar_close_time"]), int(p["confirm_bar_close_time"]),
                 int(p["confirm_bar_close_time"]), int(p["confirm_bar_open_time"]),
                 int(p["confirm_bar_close_time"]), p.get("detection_low"), p.get("detection_atr"),
                 "displayable", exec_state, "历史研究窗口（43 窗口 2026-10-12T21:42Z）",
                 (None if p.get("admission") is None else int(bool(p["admission"]))),
                 p.get("rank"), p.get("daily_list_version"),
                 json.dumps(p, ensure_ascii=False, default=str), now, now))
            n += 1
            if limit and n >= limit:
                break
        con.commit()
    finally:
        con.close()
    return {"imported": n, "path": str(path)}


# ── 主循环 ───────────────────────────────────────────────────────────────────
def run_cycle(*, T=None, limit=0, force=False, assembly_hours=ASSEMBLY_LOOKBACK_HOURS,
              window_hours=WINDOW_HOURS, log=print, arm_events=True) -> dict:
    t0 = time.time()
    T = int(T) if T else int(time.time() * 1000)
    day = (T // DAY) * DAY
    wstart = T - window_hours * H
    wdays = list(range((wstart // DAY) * DAY, day + 1, DAY))
    # ── 在线候选入口 = **当前币池口径**（2026-10-13 修正；第 2 轮改逐指标最小历史）─────
    # 原缺陷：这里调 `D.load_pool()` ⇒ 只用**冻结 375 子集**，静默丢掉 151 个加密币。
    # 现改为唯一事实源 `logs/_pool_scope.py`（`scope_crypto_v1`，币安 526；指数 2 个已排除），
    # 并用**放宽版**资格规则（`live_pool.qualify_symbol_live`，去掉 90 天上市龄 / 500 万 U
    # 成交额门槛）；**准入下限 = 指标真实最小历史**（`ADMISSION_MIN_BARS`，逐日逐时点列缺项），
    # **不再**是写死的 240。冻结研究件原样保留、不改写。
    symbols, pool_meta = LP.scope_symbols()
    listing = LP.listing_map()
    if limit:
        symbols = symbols[:limit]

    resolved = {a: CFG.resolve(None, a) for a in ARMS}
    for a, r in resolved.items():
        if r is None or not r.get("identity_ok"):
            raise SystemExit(f"冻结配置身份异常 arm={a}: "
                             f"{None if r is None else r.get('identity_problems')}")

    con = W.open_ro()
    try:
        last_bar = _latest_closed_1h(con, symbols)
    finally:
        con.close()
    prev_state = L.get_run(STATE_KEY, {}) or {}
    prev_seen = prev_state.get("last_bar_by_symbol", {}) if not force else {}

    lookback = assembly_hours + window_hours + 24
    adm_start = T - ADMISSION_LOOKBACK_DAYS * DAY
    adm_end = day + DAY

    con = W.open_ro()
    per_sym, adm_by_day, rank_by_day = {}, {}, {}
    payload = {}
    skipped = 0
    n_conf_total = n_ev_total = 0
    try:
        for i, sym in enumerate(symbols):
            lb = last_bar.get(sym)
            if (not force) and lb is not None and prev_seen.get(sym) == lb:
                skipped += 1
                continue
            pack = W.assemble(sym, T, warm_hours=assembly_hours, window_hours=window_hours,
                              lookback_hours=lookback, con=con)
            bars = pack[0]
            conf, sdiag = W.structural_confirms_live(sym, T, bars_pack=pack)
            ev_by_arm = {a: [] for a in ARMS}
            # **必须无条件计算臂事件**：臂的入场条件（base / base_3b / base|spring / base|sc）
            # 与「是否有 spring/sc 结构证据」无关。原实现 `if arm_events and conf:` 使
            # 无结构证据的币连臂事件都不算 ⇒ 纯唐奇安突破入场在源头即被丢弃（任务 2b 缺陷）。
            if arm_events:
                for a in ARMS:
                    evs, _ed = arm_events_for(sym, bars, resolved[a], wstart, T)
                    ev_by_arm[a] = evs
            arr = read_admission_arrays(con, sym, adm_start, adm_end)
            listed = listing.get(sym)
            qual_days = {}
            if arr is not None and listed:
                ts, o, hi, lo, cl, vol, q = arr
                # 准入下限 = 指标真实最小历史（**不是** 240）；短历史币按日界逐日判定
                quals = LP.qualify_symbol_live(ts, o, hi, lo, cl, vol, q, listed,
                                               int(ts[0]), int(adm_end),
                                               min_history=ADMISSION_MIN_BARS)
                qual_days = {int(d): m for (d, m, _ld, _mh) in quals}
            for d in wdays:
                if d in qual_days:
                    adm_by_day.setdefault(d, {})[(EXCHANGE, sym)] = True
                    rank_by_day.setdefault(d, {})[(EXCHANGE, sym)] = qual_days[d]
            n_conf_total += len(conf)
            n_ev_total += sum(len(v) for v in ev_by_arm.values())
            per_sym[sym] = {"bars": sdiag["bars"],
                            "strategy_segment_satisfied": sdiag["warmup_satisfied"],
                            "admission_min_bars": ADMISSION_MIN_BARS,
                            "n_confirms_in_window": len(conf),
                            "n_arm_events_in_window": {k: len(v) for k, v in ev_by_arm.items()},
                            "last_bar_open_time": lb}
            payload[sym] = (conf, ev_by_arm)
            if (i + 1) % 50 == 0:
                log(f"[live] {i+1}/{len(symbols)} {sym} conf={len(conf)} "
                    f"strategy_seg_ok={sdiag['warmup_satisfied']}")
        # 逐币「缺什么 / 哪个时点缺哪个指标」——只对**本轮实际跑到**的币做，如实列明
        pool_diag, pool_missing = LP.diagnostics(con, sorted(payload), T, wdays, listing)
    finally:
        con.close()

    adm_doc = {"schema": "live_admission/v2", "T_utc": _iso(T), "day_utc": _iso(day),
               "rule": "live_pool.qualify_symbol_live(min_listed_days=0, min_median=None, "
                       "prior_full_utc_days=0, min_history=ADMISSION_MIN_BARS) —— "
                       "在线候选入口 = 当前币池；准入下限 = 指标真实最小历史（非 240）",
               "admission_min_bars": ADMISSION_MIN_BARS,
               "indicator_min_history": {k: v["min_bars"]
                                         for k, v in LP.INDICATOR_MIN_HISTORY.items()},
               "strategy_self_gate": LP.STRATEGY_SELF_GATE,
               "rule_identity": LP.rule_identity(),
               "pool_source": pool_meta.get("pool_source"),
               "pool_n": pool_meta.get("n"),
               "scope_meta": pool_meta.get("scope_meta"),
               "segment_head_ban_applied": False,
               "admission_by_day": {_iso(d): {f"{ex}|{s}": v for (ex, s), v in sorted(m.items())}
                                    for d, m in sorted(adm_by_day.items())},
               "rank_by_day": {_iso(d): {f"{ex}|{s}": v for (ex, s), v in sorted(m.items())}
                               for d, m in sorted(rank_by_day.items())}}
    daily_list_version = "live:" + hashlib.sha256(
        json.dumps(adm_doc, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:32]
    data_version = "live:" + hashlib.sha256(
        f"{T}|{_sha256_file(W43 / '_warmup43.py')}|{pool_meta.get('pool_source')}|"
        f"{pool_meta.get('n')}|a{assembly_hours}|m{ADMISSION_MIN_BARS}|n{window_hours}"
        .encode()).hexdigest()[:32]

    prompts_all = []
    n_struct_prompts = n_arm_entries = n_struct_suppressed = 0
    for sym, (conf, ev_by_arm) in payload.items():
        for a in ARMS:
            # ① **臂自身入场事件**（冻结策略 `events`）⇒ 唯一 `enterable` 来源，**含纯唐奇安突破**
            eps = _arm_entry_prompts(
                sym, a, ev_by_arm[a], daily_list_version=daily_list_version,
                data_version=data_version, arm_config_version=resolved[a].get("version"),
                arm_config_hash=resolved[a].get("config_hash"),
                admission_by_day=adm_by_day, rank_by_day=rank_by_day)
            n_arm_entries += len(eps)
            covered = {(a, p["structural_kind"], int(p["confirm_bar_close_time"])) for p in eps}
            # ② **吸筹结构提示**（spring/sc）：恒为**展示**，不改变任何臂的开仓条件。
            #    原实现把 3c/3d 的结构证据直接判为 `prompt_enterable`；若保留，会与 ① 的
            #    臂自身入场事件在**同一经济时刻**各产生一条 `enterable` ⇒ 同刻开两仓。
            #    故统一下调为展示态；且当 ① 已有**同臂同 kind 同 bar** 的入场提示时，
            #    该结构展示行被抑制（避免同一根 bar 上出现两条同 kind 记录）。
            ps = I.make_prompts(
                sym, a, conf, ev_by_arm[a], daily_list_version=daily_list_version,
                data_version=data_version, arm_config_version=resolved[a].get("version"),
                arm_config_hash=resolved[a].get("config_hash"),
                admission_by_day=adm_by_day, rank_by_day=rank_by_day,
                paper_fill_policy="paper_engine_v1")
            # 附加**信号 ATR**（臂事件 `atr_signal_bar`）：已批准止损 = 入场价 − 3×信号 ATR
            atr_by_close = {}
            for e in ev_by_arm[a]:
                if e.get("atr") is not None:
                    atr_by_close[int(e["event_time"])] = e["atr"]
            for p in ps:
                p["atr_signal"] = atr_by_close.get(int(p["confirm_bar_close_time"]))
                if p["status"] == "prompt_enterable":
                    p["status"] = "prompt_display_only"
                    p["role"] = "display_only_same_instant"
                    p["paper_block_reason"] = None
                if (a, p["structural_kind"], int(p["confirm_bar_close_time"])) in covered:
                    n_struct_suppressed += 1
                    continue
                n_struct_prompts += 1
                prompts_all.append(p)
            prompts_all.extend(eps)
    wr = upsert_prompts(prompts_all, data_version=data_version,
                        daily_list_version=daily_list_version)

    # 更新增量状态
    new_seen = dict(prev_seen)
    for sym, d in per_sym.items():
        if d["last_bar_open_time"] is not None:
            new_seen[sym] = d["last_bar_open_time"]
    L.set_run(STATE_KEY, {"last_bar_by_symbol": new_seen, "T_utc": _iso(T),
                          "assembly_lookback_hours": assembly_hours,
                          "admission_min_bars": ADMISSION_MIN_BARS,
                          "indicator_min_history": {k: v["min_bars"]
                                                    for k, v in LP.INDICATOR_MIN_HISTORY.items()},
                          "strategy_self_gate": LP.STRATEGY_SELF_GATE,
                          "window_hours": window_hours,
                          "symbols_run": len(payload), "symbols_skipped": skipped,
                          "pool_source": pool_meta.get("pool_source"),
                          "pool_n": pool_meta.get("n"),
                          "pool_rule_identity": LP.rule_identity(),
                          "pool_missing": pool_missing,
                          "data_version": data_version, "daily_list_version": daily_list_version,
                          "updated_real_utc": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")})
    _write_pool_diagnostics(pool_meta, pool_missing, pool_diag, T)
    out = {"T_utc": _iso(T), "symbols_total": len(symbols), "symbols_run": len(payload),
           "symbols_skipped": skipped, "pool_source": pool_meta.get("pool_source"),
           "pool_n": pool_meta.get("n"), "pool_rule_identity": LP.rule_identity(),
           "pool_missing": pool_missing,
           "admission_min_bars": ADMISSION_MIN_BARS,
           "indicator_min_history": {k: v["min_bars"]
                                     for k, v in LP.INDICATOR_MIN_HISTORY.items()},
           "assembly_lookback_hours": assembly_hours,
           "strategy_self_gate": LP.STRATEGY_SELF_GATE,
           "strategy_segment_satisfied": sum(
               1 for v in per_sym.values() if v["strategy_segment_satisfied"]),
           "n_confirms": n_conf_total, "n_arm_events": n_ev_total,
           "n_structure_prompts": n_struct_prompts,
           "n_structure_prompts_suppressed": n_struct_suppressed,
           "n_arm_entry_prompts": n_arm_entries,
           "prompts": I.summarize_prompts(prompts_all), "write": wr,
           "data_version": data_version, "daily_list_version": daily_list_version,
           "elapsed_s": round(time.time() - t0, 1)}
    return out


def _write_pool_diagnostics(pool_meta, pool_missing, pool_diag, T=None) -> None:
    """逐币「缺什么 / 哪个时点缺哪个指标」落盘（供报告直接引用；不写行情库）。"""
    try:
        p = L.LIVE_DIR / "pool_diagnostics.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(
            {"pool_meta": pool_meta, "missing_summary": pool_missing,
             "admission_min_bars": ADMISSION_MIN_BARS,
             "indicator_min_history": {k: v["min_bars"]
                                       for k, v in LP.INDICATOR_MIN_HISTORY.items()},
             "indicator_min_history_detail": LP.INDICATOR_MIN_HISTORY,
             "uniform_240h_gate_removed": True,
             "strategy_self_gate": LP.STRATEGY_SELF_GATE,
             "assembly_lookback_hours": ASSEMBLY_LOOKBACK_HOURS,
             "T_utc": _iso(T) if T else None,
             "per_symbol": pool_diag,
             "generated_real_utc": dt.datetime.now(UTC).strftime(
                 "%Y-%m-%dT%H:%M:%SZ")},
            ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    except Exception:                                        # noqa: BLE001
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--T", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-arm-events", action="store_true")
    ap.add_argument("--import-historical", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=1800, help="循环间隔秒（默认 1800）")
    a = ap.parse_args()
    L.ensure_live_schema()
    for arm in ARMS:
        L.paper_conn(arm).close()
    if a.import_historical:
        print(json.dumps(import_historical_research(W43 / "12_实时结构提示_43.jsonl"),
                         ensure_ascii=False))
    if a.loop:
        while True:
            try:
                r = run_cycle(T=a.T, limit=a.limit, force=a.force, arm_events=not a.no_arm_events)
                print(json.dumps(r, ensure_ascii=False, default=str), flush=True)
            except Exception as exc:                      # noqa: BLE001
                print(json.dumps({"error": type(exc).__name__, "detail": str(exc)},
                                 ensure_ascii=False), flush=True)
            time.sleep(max(60, a.interval))
    else:
        r = run_cycle(T=a.T, limit=a.limit, force=a.force, arm_events=not a.no_arm_events)
        print(json.dumps(r, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
