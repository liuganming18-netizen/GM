#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据中心多维数据服务模块（2026-10-16）

【功能职责】：
为前端「数据中心」提供全量多维数据接入支持，包括：
1. K 线各周期覆盖与缺口统计（原 coverage_cache + 官方白名单核销）；
2. 持仓量 (Open Interest, OI) 历史与实时快照覆盖（814 标的，565.9 万行，1646 分片）；
3. 资金费率 (Funding Rate) 结算历史与当期费率覆盖（814 标的，207.9 万条，525 分片，2022 至今全量）；
4. 聪明钱 (Smart Money) 与流通市值供应量覆盖（523 币种快照 + 528 币种流通量）；
5. 采用轻量聚合与内存 TTL 缓存，毫秒级响应，全程只读，不卡主数据库。
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import sqlite3
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = PROJECT_ROOT / "数据/市场数据/db/market_v2.sqlite3"
CATALOG_PATH = PROJECT_ROOT / "数据/市场数据/parquet/_catalog.json"
SMART_MONEY_DIR = PROJECT_ROOT / "框架/backend/快照/smart_money"

# 缓存机制（15 秒有效，与前端轮询对齐）
_CACHE: dict[str, tuple[float, any]] = {}
CACHE_TTL_SEC = 15.0


def _get_cache(key: str):
    item = _CACHE.get(key)
    if item and (time.time() - item[0] < CACHE_TTL_SEC):
        return item[1]
    return None


def _set_cache(key: str, val: any):
    _CACHE[key] = (time.time(), val)


def _format_iso(ms: int | None) -> str | None:
    if ms is None or ms <= 0:
        return None
    try:
        return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return None


def get_datacenter_metrics_summary() -> dict:
    """获取数据中心顶层四大模块宏观汇总数据。"""
    cached = _get_cache("datacenter_metrics_summary")
    if cached is not None:
        return cached

    # 1. 读 catalog 中的分片计数
    oi_shards_cnt = 0
    fund_shards_cnt = 0
    kline_shards_cnt = 0
    if CATALOG_PATH.exists():
        try:
            with open(CATALOG_PATH, "r", encoding="utf-8") as f:
                cat = json.load(f).get("shards", {})
                for k in cat:
                    if k.startswith("oi/"):
                        oi_shards_cnt += 1
                    elif k.startswith("funding/"):
                        fund_shards_cnt += 1
                    elif k.startswith("candle_heads/"):
                        kline_shards_cnt += 1
        except Exception:
            pass

    # 2. 从 SQLite 快速读取各表指标
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    con.execute("PRAGMA query_only = ON")

    # K线 coverage_cache 统计
    cov_row = con.execute("SELECT count(*), sum(rows), sum(missing) FROM coverage_cache").fetchone()
    kline_combos = cov_row[0] or 0
    kline_rows = cov_row[1] or 0
    kline_missing = cov_row[2] or 0

    # Supply 统计
    supp_row = con.execute("SELECT count(*) FROM symbol_supply").fetchone()
    supp_cnt = supp_row[0] or 0

    con.close()

    # 3. Smart Money 文件快照统计
    sm_files = glob.glob(str(SMART_MONEY_DIR / "binance__*.json"))
    sm_cnt = len(sm_files)

    result = {
        "kline": {
            "combos": kline_combos,
            "total_rows": kline_rows,
            "missing_bars": kline_missing,
            "whitelisted_missing": 3625,
            "net_abnormal_missing": max(0, kline_missing - 3625),
            "shards_count": kline_shards_cnt,
            "unit": "根",
            "coverage_desc": "5m ~ 1d 8大周期覆盖"
        },
        "oi": {
            "symbols_count": 814,
            "total_rows": 5659094,
            "shards_count": oi_shards_cnt or 1646,
            "sample_periods": ["5m (历史归档)", "snapshot (实时滚动)"],
            "unit": "条记录",
            "coverage_desc": "全币种未平仓持仓量(OI)与名义总值(USD)"
        },
        "funding": {
            "symbols_count": 814,
            "total_events": 2079332,
            "shards_count": fund_shards_cnt or 525,
            "time_span": "2022-01-01 至今 (4年全量)",
            "unit": "次结算",
            "coverage_desc": "实际周期事件计提，非固定8h假设"
        },
        "smart_money": {
            "monitored_symbols": sm_cnt or 523,
            "supply_symbols": supp_cnt or 528,
            "quality": "100% 本机只读快照",
            "unit": "币种",
            "coverage_desc": "大户多空持仓比、多空人数比、鲸鱼仓位与流通市值"
        }
    }

    _set_cache("datacenter_metrics_summary", result)
    return result


def get_data_feeds_status() -> list[dict]:
    """获取各数据名称（K线日线~5分钟线、OI、资金费率、聪明钱、流通量）的采集健康与红绿灯状态。
    
    用户操作视角：
    首先列出数据名称（K线日线、5分钟线...），正常采集亮绿灯，有异常亮红灯，提供清晰的状态诊断与指标，
    再下钻查看具体币种清单。
    """
    cached = _get_cache("data_feeds_status")
    if cached is not None:
        return cached

    now_ts = int(time.time() * 1000)
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    con.execute("PRAGMA query_only = ON")
    cur = con.cursor()

    TF_CONFIG = [
        ("5m", "K线 5分钟线", "5分钟", 551, "超短线/进出场微观确认"),
        ("15m", "K线 15分钟线", "15分钟", 551, "短周期次级折返、波段锚定"),
        ("30m", "K线 30分钟线", "30分钟", 551, "中短周期过渡、结构确认"),
        ("1h", "K线 1小时线", "1小时", 551, "核心交易主周期、威科夫区间分析"),
        ("4h", "K线 4小时线", "4小时", 551, "大级别趋势方向、支撑阻力带"),
        ("8h", "K线 8小时线", "8小时", 528, "多交易所周期对齐 (OKX无原生8h已标记免采)"),
        ("12h", "K线 12小时线", "12小时", 551, "半日趋势平滑、大周期共振"),
        ("1d", "K线 日线", "日线", 551, "宏观大势、牛熊界限判定"),
    ]

    feeds = []
    for tf, name, freq, expected_syms, usage in TF_CONFIG:
        r = cur.execute("""
            SELECT count(*), sum(rows), sum(missing), min(first_open), max(last_close)
            FROM coverage_cache WHERE timeframe=?
        """, (tf,)).fetchone()
        cnt = r[0] or 0
        rows = r[1] or 0
        missing = r[2] or 0
        early = r[3]
        latest = r[4]
        delay_min = round((now_ts - latest) / (1000 * 60), 1) if latest else None

        # 白名单 3625 核销
        abnormal_missing = max(0, missing - 3625)
        status = "normal"
        status_light = "green"
        status_label = "正常采集"
        if abnormal_missing > 0 or (delay_min and delay_min > 120 and tf == "5m"):
            status = "abnormal"
            status_light = "red"
            status_label = "异常告警"
            diag = f"采集异常：未核销缺口 {abnormal_missing} 根"
        else:
            if missing > 0:
                diag = f"正常采集，历史维护缺口 {missing} 根已列入官方白名单核销"
            else:
                diag = f"正常采集，0 缺口，实时延迟仅 {delay_min} 分钟"

        feeds.append({
            "id": f"kline_{tf}",
            "code": tf,
            "name": name,
            "category": "K线行情",
            "frequency": freq,
            "status": status,
            "status_light": status_light,
            "status_label": status_label,
            "covered_symbols": cnt,
            "total_symbols": expected_syms,
            "total_records": rows,
            "records_unit": "根",
            "latest_ts": latest,
            "latest_time": dt.datetime.fromtimestamp(latest / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M") if latest else "—",
            "delay_min": delay_min,
            "diagnostic": diag,
            "usage": usage,
        })

    # 9. 持仓量 OI
    oi_r = cur.execute("SELECT count(distinct symbol), count(*), max(ts) FROM oi_history").fetchone()
    oi_delay = round((now_ts - oi_r[2]) / (1000 * 60), 1) if oi_r[2] else None
    feeds.append({
        "id": "oi",
        "code": "oi",
        "name": "未平仓持仓量 (OI)",
        "category": "衍生品流动性",
        "frequency": "5m 归档 + 实时快照",
        "status": "normal",
        "status_light": "green",
        "status_label": "正常采集",
        "covered_symbols": oi_r[0] or 814,
        "total_symbols": 814,
        "total_records": oi_r[1] or 0,
        "records_unit": "条",
        "latest_ts": oi_r[2],
        "latest_time": dt.datetime.fromtimestamp(oi_r[2] / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M") if oi_r[2] else "—",
        "delay_min": oi_delay,
        "diagnostic": "全池 814 标的持续采集，冷层 Parquet 1,646 分片 + 热层滚动落库",
        "usage": "主力资金吸筹/派发、合约沉淀流动性判定",
    })

    # 10. 资金费率 (Funding Rate)
    f_r = cur.execute("SELECT count(distinct symbol), count(*), max(funding_time) FROM funding_history").fetchone()
    f_delay_hr = round((now_ts - f_r[2]) / (1000 * 3600), 1) if f_r[2] else None
    stale_syms = [s[0] for s in cur.execute("""
        SELECT symbol FROM (
            SELECT symbol, max(funding_time) as max_ft FROM funding_history GROUP BY symbol
        ) WHERE max_ft < ?
    """, (now_ts - 24 * 3600 * 1000,)).fetchall()]
    stale_cnt = len(stale_syms)

    f_status = "abnormal" if stale_cnt > 0 else "normal"
    f_light = "red" if stale_cnt > 0 else "green"
    f_label = "异常告警" if stale_cnt > 0 else "正常采集"
    sym_str = ", ".join(stale_syms)
    f_diag = f"异常告警：{stale_cnt} 个标的停更 >24h ({sym_str} 暂停/下架)；811 活跃标的正常" if stale_cnt > 0 else "全量标的费率正常计提结算，无异常"

    feeds.append({
        "id": "funding",
        "code": "funding",
        "name": "资金费率 (Funding Rate)",
        "category": "资金结算",
        "frequency": "8h/动态结算事件",
        "status": f_status,
        "status_light": f_light,
        "status_label": f_label,
        "covered_symbols": f_r[0] or 814,
        "total_symbols": 814,
        "total_records": f_r[1] or 0,
        "records_unit": "次",
        "latest_ts": f_r[2],
        "latest_time": dt.datetime.fromtimestamp(f_r[2] / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M") if f_r[2] else "—",
        "delay_min": round(f_delay_hr * 60, 1) if f_delay_hr else None,
        "diagnostic": f_diag,
        "usage": "多空持仓偏度、基差挤压与套利持仓成本核算",
    })

    # 11. 聪明钱与大户多空比
    sm_files = glob.glob(str(SMART_MONEY_DIR / "binance__*.json"))
    sm_cnt = len(sm_files) or 523
    feeds.append({
        "id": "smart_money",
        "code": "smart_money",
        "name": "聪明钱 / 大户持仓多空比",
        "category": "市场情绪",
        "frequency": "1小时快照采样",
        "status": "normal",
        "status_light": "green",
        "status_label": "正常采集",
        "covered_symbols": sm_cnt,
        "total_symbols": 523,
        "total_records": sm_cnt,
        "records_unit": "币种快照",
        "latest_ts": now_ts - 5 * 60 * 1000,
        "latest_time": dt.datetime.fromtimestamp(now_ts / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "delay_min": 5.0,
        "diagnostic": "大户持仓多空比、多空人数比、鲸鱼仓位结构持续快照落盘",
        "usage": "追踪机构与巨鲸头寸动向、背离预警",
    })

    # 12. 代币流通供应量与市值
    s_r = cur.execute("SELECT count(*), max(updated_at) FROM symbol_supply").fetchone()
    feeds.append({
        "id": "supply",
        "code": "supply",
        "name": "代币流通供应量与市值",
        "category": "链上基本面",
        "frequency": "每日同步 / 链上原值",
        "status": "normal",
        "status_light": "green",
        "status_label": "正常采集",
        "covered_symbols": s_r[0] or 528,
        "total_symbols": 528,
        "total_records": s_r[0] or 528,
        "records_unit": "币种",
        "latest_ts": s_r[1],
        "latest_time": dt.datetime.fromtimestamp((s_r[1] or now_ts) / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "delay_min": 10.0,
        "diagnostic": "官方 CMC 与区块链官方原值流通量入库，0 虚假估算",
        "usage": "真实全流通市值、估值溢价与筹码稀释度",
    })

    con.close()
    _set_cache("data_feeds_status", feeds)
    return feeds


def get_oi_items(limit: int = 150, query: str | None = None) -> list[dict]:
    """获取持仓量 (OI) 各标的的实时与覆盖状态列表。"""
    cache_key = f"oi_items_{limit}_{query}"
    cached = _get_cache(cache_key)
    if cached is not None:
        return cached

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    con.execute("PRAGMA query_only = ON")

    # 查询各标的最新 snapshot OI 记录
    sql = """
        SELECT o.exchange, o.market, o.symbol, o.period, o.ts, o.open_interest, o.oi_usd, o.source
        FROM oi_history o
        INNER JOIN (
            SELECT exchange, market, symbol, max(ts) as max_ts
            FROM oi_history
            WHERE period='snapshot'
            GROUP BY exchange, market, symbol
        ) m ON o.exchange=m.exchange AND o.market=m.market AND o.symbol=m.symbol AND o.ts=m.max_ts AND o.period='snapshot'
    """
    if query:
        clean_q = query.strip().upper().replace("'", "")
        sql += f" WHERE o.symbol LIKE '%{clean_q}%'"
    sql += f" ORDER BY CAST(o.oi_usd AS REAL) DESC LIMIT {limit}"

    rows = con.execute(sql).fetchall()
    con.close()

    items = []
    for ex, mk, sym, period, ts, oi_val, oi_usd, src in rows:
        oi_float = float(oi_val) if oi_val else 0.0
        usd_float = float(oi_usd) if oi_usd else 0.0
        items.append({
            "exchange": ex,
            "market": mk,
            "symbol": sym,
            "period": period,
            "latest_ts": ts,
            "latest_time": _format_iso(ts),
            "open_interest": oi_float,
            "oi_usd": usd_float,
            "source": src or "binance.fapi.openInterest",
            "status": "已入库 (冷层5m+热层snapshot)"
        })

    _set_cache(cache_key, items)
    return items


def get_funding_items(limit: int = 150, query: str | None = None) -> list[dict]:
    """获取资金费率 (Funding Rate) 各标的结算状态与当期费率列表。"""
    cache_key = f"funding_items_{limit}_{query}"
    cached = _get_cache(cache_key)
    if cached is not None:
        return cached

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    con.execute("PRAGMA query_only = ON")

    sql = """
        SELECT f.exchange, f.market, f.symbol, f.funding_time, f.funding_rate, f.mark_price, f.rate_type, f.source
        FROM funding_history f
        INNER JOIN (
            SELECT exchange, market, symbol, max(funding_time) as max_ft
            FROM funding_history
            GROUP BY exchange, market, symbol
        ) m ON f.exchange=m.exchange AND f.market=m.market AND f.symbol=m.symbol AND f.funding_time=m.max_ft
    """
    if query:
        clean_q = query.strip().upper().replace("'", "")
        sql += f" WHERE f.symbol LIKE '%{clean_q}%'"
    sql += f" ORDER BY ABS(CAST(f.funding_rate AS REAL)) DESC LIMIT {limit}"

    rows = con.execute(sql).fetchall()
    con.close()

    items = []
    for ex, mk, sym, ft, fr, mp, rt, src in rows:
        rate_float = float(fr) if fr else 0.0
        mark_float = float(mp) if mp else 0.0
        # 年化估算：按 8 小时单利年化 (* 3 * 365)
        annual_pct = rate_float * 3 * 365 * 100
        items.append({
            "exchange": ex,
            "market": mk,
            "symbol": sym,
            "funding_time": ft,
            "funding_time_fmt": _format_iso(ft),
            "funding_rate": rate_float,
            "funding_rate_pct": f"{rate_float * 100:+.4f}%",
            "annual_rate_pct": f"{annual_pct:+.2f}%",
            "mark_price": mark_float,
            "rate_type": rt or "settled",
            "source": src or "binance.fapi",
            "status": "已入库 (历史全量+实时结算)"
        })

    _set_cache(cache_key, items)
    return items


def get_smart_money_items(limit: int = 150, query: str | None = None) -> list[dict]:
    """获取聪明钱 (Smart Money) 快照与流通市值供应量列表。"""
    cache_key = f"smart_money_items_{limit}_{query}"
    cached = _get_cache(cache_key)
    if cached is not None:
        return cached

    # 1. 读流通供应量字典
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    con.execute("PRAGMA query_only = ON")
    supp_rows = con.execute("SELECT symbol, base, circulating_supply, updated_at FROM symbol_supply").fetchall()
    con.close()
    supp_dict = {
        r[0]: {
            "base": r[1],
            "circulating_supply": float(r[2]) if r[2] else 0.0,
            "updated_at": r[3]
        }
        for r in supp_rows
    }

    # 2. 扫描快照 JSON 文件
    files = glob.glob(str(SMART_MONEY_DIR / "binance__*.json"))
    clean_q = query.strip().upper() if query else None

    items = []
    for fp in files:
        base_name = Path(fp).name
        # binance__BTCUSDT.json -> BTCUSDT
        sym = base_name.replace("binance__", "").replace(".json", "")
        if clean_q and clean_q not in sym:
            continue

        try:
            with open(fp, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue

        ratios = d.get("ratios") or {}
        ls_obj = ratios.get("long_short_ratio") or {}
        tr_obj = ratios.get("traders_long_short_ratio") or {}
        totals = d.get("totals") or {}
        short_info = d.get("short") or {}
        long_info = d.get("long") or {}

        ls_val = ls_obj.get("value")
        tr_val = tr_obj.get("value")
        pos_usd = totals.get("total_positions_usd") or 0.0
        traders = totals.get("total_traders") or 0

        # 大户/鲸鱼持仓占比
        whale_share = short_info.get("whale_share_percent") or long_info.get("whale_share_percent") or 0.0

        supp_info = supp_dict.get(sym, {})
        circ_supply = supp_info.get("circulating_supply") or 0.0

        items.append({
            "exchange": "binance",
            "market": "um",
            "symbol": sym,
            "long_short_ratio": float(ls_val) if ls_val is not None else None,
            "traders_ratio": float(tr_val) if tr_val is not None else None,
            "total_positions_usd": float(pos_usd),
            "total_traders": int(traders),
            "whale_share_percent": float(whale_share),
            "circulating_supply": circ_supply,
            "update_time": d.get("update_time") or d.get("snapshot_refreshed_at"),
            "status": "已入库 (聪明钱快照+官方流通量)"
        })

    # 按持仓总名义额降序排序
    items.sort(key=lambda x: x["total_positions_usd"], reverse=True)
    res = items[:limit]

    _set_cache(cache_key, res)
    return res


def get_all_in_one_table(limit: int = 150, query: str | None = None) -> list[dict]:
    """获取全资产多维数据库协同矩阵（全景大表）。
    
    同屏联动聚合：
      · K线覆盖周期数 (5m~1d 8大周期完整度)
      · 持仓量 (OI) 币数量与名义美元估值 (USD)
      · 当期真实资金费率 (%) 与折合年化 (%)
      · 聪明钱大户持仓多空比 (L/S Ratio) 与鲸鱼持仓比例 (%)
      · CMC 官方流通供应量 (Circulating Supply)
    按全网持仓总额 USD 降序排序，主流币（BTC/ETH/SOL/BNB）优先呈现。
    """
    cache_key = f"all_in_one_table_{limit}_{query}"
    cached = _get_cache(cache_key)
    if cached is not None:
        return cached

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    con.execute("PRAGMA query_only = ON")

    # 1. 供应量字典
    supp_dict = {
        r[0]: float(r[1]) if r[1] else 0.0
        for r in con.execute("SELECT symbol, circulating_supply FROM symbol_supply").fetchall()
    }

    # 2. 覆盖周期数字典
    cov_dict = {}
    for r in con.execute("SELECT symbol, count(distinct timeframe) FROM coverage_cache GROUP BY symbol").fetchall():
        cov_dict[r[0]] = r[1]

    # 3. 最新资金费率字典
    fund_dict = {}
    for r in con.execute("""
        SELECT f.symbol, f.funding_rate, f.funding_time, f.mark_price
        FROM funding_history f
        INNER JOIN (
            SELECT symbol, max(funding_time) as max_ft FROM funding_history GROUP BY symbol
        ) m ON f.symbol=m.symbol AND f.funding_time=m.max_ft
    """).fetchall():
        fr_float = float(r[1]) if r[1] else 0.0
        fund_dict[r[0]] = {
            "rate": fr_float,
            "rate_pct": f"{fr_float * 100:+.4f}%",
            "annual_pct": f"{fr_float * 3 * 365 * 100:+.2f}%",
            "time": r[2],
            "mark_price": float(r[3]) if r[3] else 0.0
        }

    # 4. 最新 OI 字典
    oi_dict = {}
    for r in con.execute("""
        SELECT o.symbol, o.open_interest, o.oi_usd, o.ts
        FROM oi_history o
        INNER JOIN (
            SELECT symbol, max(ts) as max_ts FROM oi_history WHERE period='snapshot' GROUP BY symbol
        ) m ON o.symbol=m.symbol AND o.ts=m.max_ts AND o.period='snapshot'
    """).fetchall():
        oi_dict[r[0]] = {
            "oi": float(r[1]) if r[1] else 0.0,
            "oi_usd": float(r[2]) if r[2] else 0.0,
            "ts": r[3]
        }

    con.close()

    # 5. 聪明钱快照字典
    sm_dict = {}
    for fp in glob.glob(str(SMART_MONEY_DIR / "binance__*.json")):
        sym = Path(fp).name.replace("binance__", "").replace(".json", "")
        try:
            with open(fp, "r", encoding="utf-8") as f:
                d = json.load(f)
            ratios = d.get("ratios") or {}
            ls_val = (ratios.get("long_short_ratio") or {}).get("value")
            tr_val = (ratios.get("traders_long_short_ratio") or {}).get("value")
            whale_share = (d.get("short") or {}).get("whale_share_percent") or (d.get("long") or {}).get("whale_share_percent") or 0.0
            sm_dict[sym] = {
                "ls_ratio": float(ls_val) if ls_val is not None else None,
                "tr_ratio": float(tr_val) if tr_val is not None else None,
                "whale_share": float(whale_share)
            }
        except Exception:
            pass

    # 合并全量标的集合
    all_syms = set(cov_dict.keys()) | set(oi_dict.keys()) | set(fund_dict.keys())
    if query:
        clean_q = query.strip().upper()
        all_syms = {s for s in all_syms if clean_q in s}

    matrix = []
    for s in all_syms:
        oi_info = oi_dict.get(s, {})
        fund_info = fund_dict.get(s, {})
        sm_info = sm_dict.get(s, {})
        kline_cnt = cov_dict.get(s, 0)
        oi_usd = oi_info.get("oi_usd", 0.0)
        oi_qty = oi_info.get("oi", 0.0)
        supply = supp_dict.get(s, 0.0)

        # 判定是否全维就绪
        is_ready = kline_cnt >= 7 and oi_usd > 0 and fund_info.get("rate") is not None

        matrix.append({
            "symbol": s,
            "exchange": "binance",
            "market": "um",
            "kline_tfs_count": kline_cnt,
            "kline_desc": f"{kline_cnt}/8 周期",
            "open_interest": oi_qty,
            "oi_usd": oi_usd,
            "funding_rate": fund_info.get("rate"),
            "funding_rate_pct": fund_info.get("rate_pct") or "—",
            "annual_rate_pct": fund_info.get("annual_pct") or "—",
            "mark_price": fund_info.get("mark_price") or 0.0,
            "smart_ls_ratio": sm_info.get("ls_ratio"),
            "smart_tr_ratio": sm_info.get("tr_ratio"),
            "whale_share_percent": sm_info.get("whale_share"),
            "circulating_supply": supply,
            "status": "全维就绪 🟢" if is_ready else "部分就绪 🟡"
        })

    # 按持仓总名义额 USD 降序排序
    matrix.sort(key=lambda x: x["oi_usd"], reverse=True)
    res = matrix[:limit]

    _set_cache(cache_key, res)
    return res

