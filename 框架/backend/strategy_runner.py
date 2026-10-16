#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线策略运行器（读取已入库已收盘 K 线 → 产出事件 → 可选写入信号日志）。

只读行情库的 K 线；写入仅限业务表 signals / signal_conditions（可重复、幂等）。
不触发任何外部请求；不下单；不写合成数据到共享库。

纪律：
  - **不静默截断**：按显式时间范围分页读取全部已收盘 bar；超过 --max-bars 直接拒绝。
  - **数据版本绑定实际消费数据**：data_version 由本次消费的所有 bar 的来源集合计算，
    不使用“最新来源”冒充。
  - **未冻结配置只允许显式研究试运行**：未冻结时必须加 --research-trial（只读）；
    --write 仅允许在配置已冻结（frozen=true）时使用，拒绝写生产确认日志。
  - 事件唯一键含策略版本与数据版本；重复写入以 skipped 计数显式报告，不静默吞掉。

用法（Python 3.13；勿用系统 3.9，本包使用 PEP 604 类型语法）：
  PY=/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3
  $PY strategy_runner.py --strategy A                       # 只读干跑（默认）
  $PY strategy_runner.py --strategy B --research-trial      # 未冻结配置的研究试运行
  $PY strategy_runner.py --strategy A --start 2024-01-01T00:00:00Z --end 2024-12-31T23:59:59Z
  $PY strategy_runner.py --strategy A --write               # 仅配置已冻结时可用
"""
import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from strategies import common, registry, wx_gate  # noqa: E402
import history_lifecycle  # noqa: E402

# W1（2026-10-16）：默认库 / 工程根改为**工程内推导**（原写死 旧工作区路径（已删），
# 该目录已删除）。显式环境变量仍可覆盖，缺省落到本工程热库 market_v2.sqlite3。
PROJECT = str(HERE.parent.parent)
DEFAULT_DB = str(Path(PROJECT) / "数据" / "市场数据" / "db" / "market_v2.sqlite3")


def connect(db):
    con = sqlite3.connect(db, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=10000")
    return con


def parse_time(s):
    """接受 ISO8601（含 Z）或毫秒时间戳；None/空返回 None。"""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def iso(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# OKX 12h 的 UTC 对齐周期（12h = 43_200_000 ms）。用于读侧隔离旧 `candle12H`
# （UTC+8 锚点 04:00/16:00Z）遗留的非对齐行，与 adapter._utc_boundary_filter 同一判据。
_UTC_ALIGN_MS = {"12h": 12 * 60 * 60 * 1000}


def read_bars(con, exchange, market, symbol, timeframe, start=None, end=None,
              chunk=5000, max_bars=None, apply_lifecycle=True,
              allow_cross_generation=False, utc_aligned_only=True,
              closed_only_end=True):
    """分页读取时间范围内**全部**已收盘 bar。返回 (bars, 范围内总数, lifecycle_meta)。

    超过 max_bars 时抛 SystemExit 拒绝，绝不静默截断。

    因果可用性（2026-10-11 独立反例 `end_cutoff_reads_future_close` 整改）：
      - `end` 默认按**闭合可用时间**过滤：只消费 `close_time <= end` 的 bar。
        一根 1h bar 只有到 `close_time` 才「已闭合、可用」；旧实现按 `open_time <= end`
        过滤，于是请求 `--end` 落在某小时中点时会读到该小时的**未来收盘价**。
      - `closed_only_end=False` 可显式恢复旧口径（仅供审计对比，不作为默认）。

    读侧语义隔离（与 adapter.v1_candles 同一判据，2026-10-07 12h 任务）：
      - **官方归档零量占位行**（volume==0 且 OHLC 四值相同）**不计入成交样本**，
        从返回 bars 中剔除并如实计数（`excluded_archive_padding`）。
      - **同一符号多代合约**（旧合约退市→新合约上市）**不拼成连续可交易历史**：
        默认只保留**最新一代**（当前可交易合约），被丢弃的旧代根数如实计数；
        研究场景可显式 `allow_cross_generation=True` 放开。
      - **OKX 12h 旧混边界行**（`candle12H`，UTC+8 锚点 04:00/16:00Z）不并入 UTC 序列：
        仅 `exchange=="okx" and timeframe=="12h"` 时，剔除 `open_time` 未对齐 00:00/12:00Z
        的旧行并如实计数（`excluded_non_utc_12h`）；其他交易所/周期原样读取，**不改其他周期语义**。
      - 库内原始行**不删不改**；本函数只影响本次返回。
    """
    where = "exchange=? AND market=? AND symbol=? AND timeframe=? AND is_closed=1"
    args = [exchange, market, symbol, timeframe]
    if start is not None:
        where += " AND open_time>=?"
        args.append(start)
    if end is not None:
        if closed_only_end:
            where += " AND close_time<=?"      # 因果：只消费截止前已闭合可用的 bar
        else:
            where += " AND open_time<=?"
        args.append(end)
    total = con.execute(f"SELECT COUNT(*) FROM candles WHERE {where}", args).fetchone()[0]
    if max_bars is not None and total > max_bars:
        raise SystemExit(
            f"时间范围内有 {total} 根已收盘 bar，超过 --max-bars={max_bars}；"
            f"请缩小 --start/--end 或提高 --max-bars。拒绝静默截断。")
    # 读侧 UTC 边界隔离判据（仅 OKX 12h 生效，与 adapter._utc_boundary_filter 一致；
    # 库内原行保留、不删）。分页游标始终按**库内原始行数**推进，避免漏读/重读。
    align_ms = _UTC_ALIGN_MS.get(timeframe) if exchange == "okx" else None
    apply_utc = bool(utc_aligned_only and align_ms)
    non_utc_n = 0
    out, off = [], 0
    while off < total:
        rows = con.execute(
            f"SELECT * FROM candles WHERE {where} ORDER BY open_time ASC LIMIT ? OFFSET ?",
            args + [chunk, off]).fetchall()
        if not rows:
            break
        off += len(rows)  # 游标按原始行数推进（含被隔离行）
        if apply_utc:
            aligned = [r for r in rows if not int(r["open_time"]) % align_ms]
            non_utc_n += len(rows) - len(aligned)
            rows = aligned
        out.extend(common.from_row(r) for r in rows)

    life = {"apply_lifecycle": bool(apply_lifecycle and timeframe == history_lifecycle.SCOPE_TF),
            "excluded_archive_padding": 0, "excluded_non_tradeable": 0,
            "excluded_non_utc_12h": non_utc_n,
            "generations_present": [], "cross_generation": False,
            "restricted_to_generation": None, "dropped_prior_generation_bars": 0,
            "note": None}
    if life["apply_lifecycle"]:
        anns = [history_lifecycle.classify_values(
                    exchange, symbol, timeframe, b.open_time,
                    b.open, b.high, b.low, b.close, b.volume) for b in out]
        gens = []
        for a in anns:
            g = a["generation"]
            if g is not None and g not in gens:
                gens.append(g)
        life["generations_present"] = gens
        life["cross_generation"] = len([g for g in gens if g != "gap"]) > 1
        kept, pad_n, non_tr_n = [], 0, 0
        for b, a in zip(out, anns):
            if a["row_kind"] == "archive_padding":
                pad_n += 1
            if not a["tradeable"]:
                non_tr_n += 1
                continue
            kept.append((b, a))
        life["excluded_archive_padding"] = pad_n
        life["excluded_non_tradeable"] = non_tr_n
        if life["cross_generation"] and not allow_cross_generation:
            latest = history_lifecycle.latest_generation(exchange, symbol, timeframe)
            life["restricted_to_generation"] = latest
            before = len(kept)
            kept = [(b, a) for (b, a) in kept if a["generation"] == latest]
            life["dropped_prior_generation_bars"] = before - len(kept)
        out = [b for (b, _a) in kept]
        notes = []
        if pad_n:
            notes.append(f"剔除官方归档零量占位 {pad_n} 根（非成交样本）")
        if non_utc_n:
            notes.append(f"读侧隔离 OKX 12h 非 UTC 边界旧行 {non_utc_n} 根"
                         "（旧 candle12H 混边界；库内原行保留）")
        if life["cross_generation"]:
            if life["restricted_to_generation"]:
                notes.append(f"跨代际已按官方生命周期限用最新代 "
                             f"{life['restricted_to_generation']}，丢弃旧代 "
                             f"{life['dropped_prior_generation_bars']} 根（不拼接旧新合约）")
            else:
                notes.append("跨代际（--allow-cross-generation 已放开，仅研究用）")
        life["note"] = "；".join(notes) or "无占位/无跨代际，未剔除任何根"
    return out, total, life


def _bar_consumed_identity(b):
    """一根 bar 的**实际消费值**规范串（含来源 id）。

    2026-10-11 独立反例 `consumed_close_changed_data_version_unchanged` 整改：
    旧 `data_version_for` 只 hash「来源集合」，于是被消费的 close 从 101.5 改成 101.6
    数据版本**不变** —— 版本没有绑定实际消费值。现把每根被消费 bar 的
    open/close 时点、OHLC、量、主动买量与来源 id 全部纳入身份。
    """
    def g(v):
        return "N" if v is None else repr(float(v))
    return "|".join([
        str(int(b.open_time)), str(int(b.close_time)),
        g(b.open), g(b.high), g(b.low), g(b.close),
        g(b.volume), g(b.quote_volume), str(int(b.trades or 0)),
        g(b.taker_buy_volume), g(b.taker_buy_quote_volume),
        ("N" if b.source_id is None else str(int(b.source_id))),
    ])


def data_version_for(con, bars, upto_close_time=None, identity=None):
    """由本次**实际消费**的 bars 计算可复现数据版本。返回 (dv, sources, unknown)。

    - `upto_close_time` 给定时，只把 `close_time <= upto_close_time` 的 bar 计入 ——
      这使**同一事件的数据版本对其自身前缀稳定**（「允许快照且前缀不变」）：
      追加未来来源不会改变既有事件的数据版本，因而不会二次写入（幂等消费）。
    - `identity` 可再绑定策略/配置/标的身份，使版本同时含完整身份。
    - `dv = "dv:" + sha256(消费值规范串 + 来源修订 + 身份)[:16]`。
    """
    used = bars if upto_close_time is None else \
        [b for b in bars if int(b.close_time) <= int(upto_close_time)]
    ids = sorted({b.source_id for b in used if b.source_id is not None})
    unknown = any(b.source_id is None for b in used)
    pairs = []
    if ids:
        qs = ",".join("?" * len(ids))
        rows = con.execute(f"SELECT id, sha256 FROM sources WHERE id IN ({qs})", ids).fetchall()
        pairs = sorted((r["id"], r["sha256"] or "") for r in rows)
    body = "BARS:" + str(len(used)) + "\n" + "\n".join(_bar_consumed_identity(b) for b in used)
    src = "SRC:" + "|".join(f"{i}:{s}" for i, s in pairs)
    canon = body + "\n" + src
    if identity:
        canon += "\nID:" + str(identity)
    h = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    if not used:
        return None, [{"source_id": i, "sha256": s} for i, s in pairs], unknown
    return f"dv:{h[:16]}", [{"source_id": i, "sha256": s} for i, s in pairs], unknown


# 事件身份键（**不含 data_version**）：同一策略/版本/配置/标的/确认时点 = 同一事件。
# 这是「幂等消费」的判据：数据版本变化只允许**修订**同一事件，不允许新增重复事件。
EVENT_KEY_COLS = ("strategy_key", "strategy_version", "config_hash",
                  "exchange", "market", "symbol", "timeframe", "confirmed_at")


def _event_key(ev, exchange, market, symbol, timeframe):
    return (ev.strategy_key, ev.strategy_version, ev.config_hash,
            exchange, market, symbol, timeframe, int(ev.confirmed_at))


def write_events(con, events, exchange, market, symbol, timeframe, dv, sources,
                 dv_for=None):
    """写入信号日志。返回 {"written","skipped","revised","refused","refused_events"}。

    2026-10-11 独立反例整改：
      · **SIG02 保留**：`signals` 唯一键含可空 data_version ⇒ 正式写入仍拒绝缺
        data_version / 缺来源身份的事件（不写、不猜、不补空值）。
      · **不再用其他行来源补填**（反例 `partly_unknown_source_written_and_fabricated`）：
        事件自身 `source_id is None` 即拒绝该事件，绝不取 `sources[0]` 冒充。
      · **幂等消费 + 修订**（反例 `future_source_extension_duplicates_same_event`）：
        以 `EVENT_KEY_COLS`（不含 data_version）判同一事件：
          - 已存在且 data_version 相同 ⇒ `skipped`（重跑不重复提示）；
          - 已存在但 data_version 不同 ⇒ **UPDATE 原地修订**该行（不新增行），计 `revised`；
          - 不存在 ⇒ INSERT，计 `written`。
        因此追加未来来源 / 修订过去数据都**不会**产生第二条同事件行。
      · `dv_for(ev)` 可按事件给前缀数据版本；缺省用统一 `dv`。
    """
    written = skipped = revised = 0
    refused_events = []
    if not sources:
        return {"written": 0, "skipped": 0, "revised": 0,
                "refused": "缺来源身份（sources 为空）：拒绝写入正式信号日志",
                "refused_events": []}
    for ev in events:
        ev.exchange, ev.market, ev.symbol = exchange, market, symbol
        ev_dv = dv_for(ev) if dv_for else dv
        if not ev_dv:
            refused_events.append({"event_uid": (ev.evidence or {}).get("event_uid"),
                                   "reason": "缺 data_version（该事件前缀来源集合为空）"})
            continue
        if ev.source_id is None:
            refused_events.append({"event_uid": (ev.evidence or {}).get("event_uid"),
                                   "reason": "事件自身 source_id=None：拒绝写入，"
                                             "不用其他行来源补填"})
            continue
        ev.data_version = ev_dv
        evidence = dict(ev.evidence or {})
        evidence["data_sources"] = sources
        ev_json = json.dumps(evidence, ensure_ascii=False)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        key = _event_key(ev, exchange, market, symbol, timeframe)
        where = " AND ".join(f"{c}=?" for c in EVENT_KEY_COLS)
        try:
            existing = con.execute(
                f"SELECT id, data_version FROM signals WHERE {where}", key).fetchall()
        except sqlite3.OperationalError as exc:
            raise SystemExit("业务表未迁移，请先运行 migrate.py：" + str(exc))
        if existing:
            if any(r["data_version"] == ev_dv for r in existing):
                skipped += 1
                continue
            # 修订：原地更新第一条；多余的历史重复行如实计数但不删（保留证据）
            con.execute(
                "UPDATE signals SET data_version=?, source_id=?, evidence_json=? "
                "WHERE id=?",
                (ev_dv, ev.source_id, ev_json, existing[0]["id"]))
            con.execute("DELETE FROM signal_conditions WHERE signal_id=?", (existing[0]["id"],))
            _insert_conditions(con, existing[0]["id"], ev)
            revised += 1
            continue
        try:
            cur = con.execute(
                "INSERT OR IGNORE INTO signals(strategy_key,strategy_version,config_hash,exchange,"
                "market,symbol,timeframe,status,direction,event_time,confirmed_at,data_version,"
                "source_id,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ev.strategy_key, ev.strategy_version, ev.config_hash, exchange, market, symbol,
                 timeframe, ev.status, ev.direction, ev.event_time, ev.confirmed_at, ev_dv,
                 ev.source_id, ev_json, now_ms))
        except sqlite3.OperationalError as exc:
            raise SystemExit("业务表未迁移，请先运行 migrate.py：" + str(exc))
        if cur.rowcount == 0:
            skipped += 1
            continue
        _insert_conditions(con, cur.lastrowid, ev)
        written += 1
    con.commit()
    return {"written": written, "skipped": skipped, "revised": revised,
            "refused": None, "refused_events": refused_events}


def _insert_conditions(con, signal_id, ev):
    for c in ev.conditions:
        con.execute("INSERT INTO signal_conditions(signal_id,name,actual,threshold,unit,"
                    "satisfied,note) VALUES (?,?,?,?,?,?,?)",
                    (signal_id, c.name, None if c.actual is None else str(c.actual),
                     None if c.threshold is None else str(c.threshold), c.unit,
                     None if c.satisfied is None else int(bool(c.satisfied)), c.note))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--strategy", required=True, choices=sorted(registry.REGISTRY))
    p.add_argument("--config-id", default=None,
                   help="按**预登记配置 id**（含 3b_vol_minus03 / 3a_cost2x / 3a_delay30 等）"
                        "解析配置身份；缺省=策略臂本身")
    p.add_argument("--version", default=None)
    p.add_argument("--exchange", default="binance")
    p.add_argument("--market", default="um")
    p.add_argument("--symbol", default="1000PEPEUSDT")
    p.add_argument("--start", default=None, help="时间范围起（ISO8601 或毫秒时间戳）")
    p.add_argument("--end", default=None, help="时间范围止（ISO8601 或毫秒时间戳）")
    p.add_argument("--max-bars", type=int, default=200000, help="超过则拒绝，不静默截断")
    p.add_argument("--research-trial", action="store_true",
                   help="显式研究试运行：允许使用未冻结配置（只读，不写生产日志）")
    p.add_argument("--write", action="store_true",
                   help="写入信号日志（要求：身份一致 ∧ 视图 run_frozen ∧ G01–G05 全 pass）")
    p.add_argument("--allow-cross-generation", action="store_true",
                   help="研究用：放开跨代际限制（默认只保留最新一代合约，不拼接旧新合约）")
    p.add_argument("--end-open-inclusive", action="store_true",
                   help="审计用：--end 按 open_time 过滤（旧口径；默认按 close_time 闭合可用过滤）")
    a = p.parse_args()

    mod = registry.get(a.strategy)
    con = connect(a.db)
    try:
        cfg_id = a.config_id or a.strategy
        resolved = registry.resolved_config(con, cfg_id, a.version)
        status, missing, errors = common.validate_config(
            resolved, mod.REQUIRED_CONFIG, mod.VALIDATION, mod.RELATIONS)
        if resolved and resolved.get("hash_check") == "drift":
            raise SystemExit("身份漂移（视图 SHA / 登记表 / 逐配置生效身份 / DB 参数不一致）："
                             "拒绝运行。" + "；".join(resolved.get("identity_problems") or []))
        identity_ok = bool(resolved and resolved.get("identity_ok"))
        # 第17：运行门禁**始终**从真实来源重读（不信任 resolved["run_binding"] 缓存）。
        # 缓存只作**声明核对**（source+sha256 须与真实门禁一致）；伪路径/全零 SHA/自填布尔 ⇒ 拒绝运行。
        bind = wx_gate.binding()
        declared_bind = (resolved or {}).get("run_binding")
        if isinstance(declared_bind, dict) and (
                declared_bind.get("sha256") != bind.get("sha256")
                or declared_bind.get("source") != bind.get("source")):
            raise SystemExit(
                "run_binding 声明与真实门禁来源不符（缓存 source/sha256 ≠ wx_gate.binding()）："
                "拒绝运行。")
        frozen = bool(resolved and resolved.get("frozen"))
        gate_pass = bool(bind.get("all_pass"))

        base = {"strategy": a.strategy, "name": mod.NAME, "timeframe": mod.TIMEFRAME,
                "family": getattr(mod, "FAMILY", "legacy"),
                "exchange": a.exchange, "market": a.market, "symbol": a.symbol,
                "config_id": cfg_id,
                "config_version": resolved["version"] if resolved else None,
                "config_hash": resolved["config_hash"] if resolved else None,
                "config_source": resolved.get("source") if resolved else None,
                "config_exec": resolved.get("exec") if resolved else None,
                "config_frozen": frozen,
                "identity_ok": identity_ok,
                "view_sha256": resolved.get("view_sha256") if resolved else None,
                "run_binding": {"gates": bind.get("gates"), "all_pass": gate_pass,
                                "source": bind.get("source"),
                                "self_check": bind.get("self_check"),
                                "reason": bind.get("reason")},
                "bars_used": 0, "events": []}

        if status == common.STATUS_PENDING:
            base["status"] = common.STATUS_PENDING
            base["diagnostics"] = {"strategy": a.strategy, "missing_config": missing,
                                   "note": "参数未冻结；不采用任何默认阈值"}
            print(json.dumps(base, ensure_ascii=False, indent=2))
            return 0
        if status == common.STATUS_INVALID:
            base["status"] = common.STATUS_INVALID
            base["config_errors"] = errors
            print(json.dumps(base, ensure_ascii=False, indent=2))
            return 2

        # 运行放行 = 身份一致 ∧ 视图 run_frozen ∧ G01–G05 全 pass（三者缺一不可）。
        if a.write and not frozen:
            raise SystemExit(
                "运行未放行（配置未冻结 / 运行门禁未通过）：拒绝写入生产确认日志。"
                f"（身份一致={identity_ok}；G01–G05 全 pass={gate_pass}；"
                f"run_frozen={resolved.get('policy_run_frozen') if resolved else None}；"
                f"原因：{bind.get('reason')}）"
                "未放行只允许显式研究试运行（--research-trial，只读）。")
        if not frozen and not a.research_trial:
            raise SystemExit(
                "运行未放行（配置未冻结 / G01–G05 未全通过）：默认不运行。"
                f"（G01–G05 全 pass={gate_pass}；run_frozen="
                f"{resolved.get('policy_run_frozen') if resolved else None}）"
                "研究试运行请显式加 --research-trial（只读，不写日志）。")

        start, end = parse_time(a.start), parse_time(a.end)
        bars, total, life = read_bars(con, a.exchange, a.market, a.symbol, mod.TIMEFRAME,
                                      start, end, max_bars=a.max_bars,
                                      allow_cross_generation=a.allow_cross_generation,
                                      closed_only_end=not a.end_open_inclusive)
        # 第15：F_WX 臂**显式**传 (exchange, symbol) 身份，使事件/结构锚点 event_uid 含币/所
        # （跨币不串、跨币同刻同类型各留一条）；legacy A/B 无 identity 形参，保持原调用。
        if getattr(mod, "FAMILY", None) == "F_WX":
            result = mod.run(bars, resolved, identity=(a.exchange, a.symbol))
        else:
            result = mod.run(bars, resolved)
        dv, sources, unknown_src = data_version_for(con, bars)

        ident_str = f"{a.exchange}:{a.market}:{a.symbol}:{mod.TIMEFRAME}:{cfg_id}"
        _dv_cache = {}

        def dv_for(ev):
            ct = int(ev.event_time)
            if ct not in _dv_cache:
                _dv_cache[ct] = data_version_for(
                    con, bars, upto_close_time=ct,
                    identity=f"{ident_str}:{ev.strategy_key}:{ev.config_hash}")[0]
            return _dv_cache[ct]

        events = []
        for e in result["events"]:
            d = e.to_dict()
            d["data_version"] = dv_for(e)
            ev_evidence = dict(d.get("evidence") or {})
            ev_evidence["data_sources"] = sources
            if unknown_src:
                ev_evidence["data_sources_unknown"] = True
            d["evidence"] = ev_evidence
            events.append(d)

        base.update({
            "mode": "confirmed" if frozen else "research_trial",
            "status": result["status"],
            "bars_used": len(bars),
            "bars_available_in_range": total,
            "end_cutoff_semantics": ("open_time<=end（审计旧口径）" if a.end_open_inclusive
                                     else "close_time<=end（闭合可用，因果）"),
            "data_lifecycle": life,
            "range": {"start": iso(start), "end": iso(end),
                      "first_open": iso(bars[0].open_time) if bars else None,
                      "last_close": iso(bars[-1].close_time) if bars else None},
            "data_version": dv,
            "data_version_binding": "每事件按自身前缀（close_time<=event_time）的实际消费值+来源修订+身份计算",
            "data_sources": sources,
            "data_sources_unknown": unknown_src,
            "diagnostics": result["diagnostics"],
            "events": events,
        })
        if a.write:
            if unknown_src:
                raise SystemExit(
                    "本次消费范围内存在缺来源（source_id=None）的 bar：拒绝写入正式信号日志"
                    "（不用其他行来源补填）。")
            base["write"] = write_events(con, result["events"], a.exchange, a.market,
                                         a.symbol, mod.TIMEFRAME, dv, sources, dv_for=dv_for)
        print(json.dumps(base, ensure_ascii=False, indent=2))
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
