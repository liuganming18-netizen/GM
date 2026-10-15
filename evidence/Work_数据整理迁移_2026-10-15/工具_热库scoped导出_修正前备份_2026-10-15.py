#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""活动热库 scoped 导出（只读源 + 一致读事务 + 范围过滤）：
- 源库以 URI mode=ro 打开（保护源）；目标以 ATTACH 可写；只 INSERT 到 o.*，绝不写 main.*。
- 表范围：仅 K线+OI+来源/分类/状态；排除 funding/signals/collector/api 等。
- 符号范围：数据表按正式白名单过滤（币安526 / OKX 23），排除指数(BTCDOMUSDT/ALLUSDT/DEFIUSDT)与非白名单 OKX 币。
- symbol_classification 整表保留（范围判定证据，含 excluded_reason）。
- 记录：读取时点、源WAL、逐表源/目标/排除行数、目标SHA/大小、quick_check。
不直接复制活动库文件。
"""
import os, sys, sqlite3, re, json, hashlib, datetime

ROOT = "/Users/boos/Desktop/威科夫因子共振趋势"
SRC = "/Users/boos/WorkBuddy AI/加密货币趋势判断系统/data/market_v2.sqlite3"
POOL = "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载/标的池"
EVID = ROOT + "/evidence/Work_数据整理迁移_2026-10-15"
DST = ROOT + "/数据/hot/market_v2_scoped_kline_oi.sqlite3"

# 逐符号数据表（按白名单过滤）
SYM_FILTERED = ["candle_versions", "candle_heads", "oi_history", "sources",
                "symbol_pool", "symbol_supply", "realtime_klines",
                "candle_field_notes", "coverage_cache"]
# 整表保留（无 symbol 列 / 范围判定证据）
WHOLE = ["symbol_classification", "api_scope_state", "kline_stream_state"]
VIEWS = ["candles"]

def prefixed(sql, name, alias):
    """把 CREATE TABLE/VIEW 的对象名（可带引号）加 schema 前缀；CREATE INDEX 则替换 ON 后的表名。"""
    pat = re.compile(r'(?is)^(\s*CREATE\s+(?:UNIQUE\s+)?(?:TABLE|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?)(["\[`]?)' + re.escape(name) + r'\2')
    new, n = pat.subn(lambda m: m.group(1) + alias + '."' + name + '"', sql, count=1)
    if n:
        return new
    # 回退：INDEX / 其它 -> 替换第一处出现的表名 token
    return re.sub(r'(?i)\b' + re.escape(name) + r'\b', alias + '."' + name + '"', sql, count=1)

def main():
    sc = json.load(open(POOL + "/scope_crypto.json"))
    wl_bin = sorted(x["symbol"] for x in sc["binance_items"])
    wl_okx = sorted(sc["okx_symbols"])
    if os.path.exists(DST):
        os.remove(DST)
    for suf in ("-journal", "-wal", "-shm"):
        if os.path.exists(DST + suf):
            os.remove(DST + suf)
    os.makedirs(os.path.dirname(DST), exist_ok=True)
    t_start = datetime.datetime.now(datetime.timezone.utc).isoformat()
    wal = SRC + "-wal"
    wal_size = os.path.getsize(wal) if os.path.exists(wal) else 0
    db_size = os.path.getsize(SRC)

    # 源只读；目标 ATTACH 可写
    src = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True, isolation_level=None)
    src.execute("ATTACH DATABASE ? AS o", (DST,))
    bin_ph = ",".join("?" * len(wl_bin))
    okx_ph = ",".join("?" * len(wl_okx))
    pred = f"((exchange='binance' AND symbol IN ({bin_ph})) OR (exchange='okx' AND symbol IN ({okx_ph})))"
    params = tuple(wl_bin + wl_okx)
    rows = []
    try:
        src.execute("BEGIN")  # 一致读事务（WAL 固定快照）
        for t in SYM_FILTERED:
            r = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
            if not r or not r[0]:
                rows.append({"表": t, "状态": "源无此表"}); continue
            src.execute(prefixed(r[0], t, "o"))
            n = src.execute(f'SELECT count(*) FROM main."{t}"').fetchone()[0]
            src.execute(f'INSERT INTO o."{t}" SELECT * FROM main."{t}" WHERE {pred}', params)
            idx = src.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (t,)).fetchall()
            for (isql,) in idx:
                try: src.execute(prefixed(isql, t, "o"))
                except sqlite3.Error: pass
            m = src.execute(f'SELECT count(*) FROM o."{t}"').fetchone()[0]
            rows.append({"表": t, "源行数": n, "目标行数": m, "排除行数": n - m, "一致": m <= n})
            print(f"  {t}: 源={n} 目标={m} 排除={n-m}", flush=True)
        for t in WHOLE:
            r = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
            if not r or not r[0]:
                rows.append({"表": t, "状态": "源无此表"}); continue
            src.execute(prefixed(r[0], t, "o"))
            n = src.execute(f'SELECT count(*) FROM main."{t}"').fetchone()[0]
            src.execute(f'INSERT INTO o."{t}" SELECT * FROM main."{t}"')
            idx = src.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (t,)).fetchall()
            for (isql,) in idx:
                try: src.execute(prefixed(isql, t, "o"))
                except sqlite3.Error: pass
            m = src.execute(f'SELECT count(*) FROM o."{t}"').fetchone()[0]
            rows.append({"表": t, "源行数": n, "目标行数": m, "排除行数": 0, "一致": n == m, "说明": "整表保留(范围判定证据)"})
            print(f"  {t}: 源={n} 目标={m} (整表)", flush=True)
        for v in VIEWS:
            r = src.execute("SELECT sql FROM sqlite_master WHERE type='view' AND name=?", (v,)).fetchone()
            if r and r[0]:
                src.execute(prefixed(r[0], v, "o"))
        src.execute("COMMIT")
    finally:
        try: src.execute("COMMIT")
        except Exception: pass
    t_end = datetime.datetime.now(datetime.timezone.utc).isoformat()
    src.close()

    # 目标校验 + 范围自检
    ck = sqlite3.connect(DST)
    qc = ck.execute("PRAGMA quick_check(1)").fetchone()[0]
    integ = ck.execute("PRAGMA integrity_check(1)").fetchone()[0]
    tables = [r[0] for r in ck.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    views = [r[0] for r in ck.execute("SELECT name FROM sqlite_master WHERE type='view' ORDER BY name")]
    # 自检：数据表是否仍有非白名单符号
    leaks = {}
    for t in ["candle_versions", "candle_heads", "oi_history", "sources", "symbol_pool", "symbol_supply", "realtime_klines", "candle_field_notes", "coverage_cache"]:
        if t not in tables: continue
        bad = ck.execute(f"SELECT DISTINCT exchange||':'||symbol FROM \"{t}\" WHERE (exchange='binance' AND symbol NOT IN ({bin_ph})) OR (exchange='okx' AND symbol NOT IN ({okx_ph}))", params).fetchall()
        if bad: leaks[t] = [x[0] for x in bad]
    ck.close()
    dst_size = os.path.getsize(DST)
    h = hashlib.sha256()
    with open(DST, "rb") as f:
        while True:
            b = f.read(1 << 22)
            if not b: break
            h.update(b)
    rep = {"源库": SRC, "源库字节": db_size, "源WAL字节": wal_size,
           "读取起": t_start, "读取止": t_end,
           "白名单币安": len(wl_bin), "白名单OKX": len(wl_okx),
           "排除指数": ["BTCDOMUSDT", "ALLUSDT", "DEFIUSDT"],
           "目标": DST, "目标字节": dst_size, "目标GiB": round(dst_size / (1 << 30), 4),
           "目标SHA256": h.hexdigest(), "quick_check": qc, "integrity_check": integ,
           "含表": tables, "含视图": views, "逐表行数": rows,
           "范围自检_非白名单残留": leaks,
           "处理方式": "只读源(mode=ro)+ATTACH可写目标+BEGIN一致读事务；数据表按白名单过滤；symbol_classification整表保留；未复制活动库文件"}
    with open(EVID + "/热库_scoped导出_证据.json", "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print(json.dumps({"目标字节": dst_size, "目标GiB": rep["目标GiB"], "目标SHA256": h.hexdigest(),
                      "quick_check": qc, "integrity_check": integ, "非白名单残留": leaks}, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
