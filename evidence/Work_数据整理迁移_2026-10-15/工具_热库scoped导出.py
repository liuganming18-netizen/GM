#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""活动热库 scoped 导出（只读源 + 一致读事务 + **逐表行级白名单过滤**）— 修正版2 2026-10-15

相对上一版的修正：
 ① 不用 PRAGMA query_only（连接级只读会连 ATTACH 目标的写一起拒）；源 URI mode=ro 只读，目标 ATTACH 可写，仅写 o.*。
 ② CREATE INDEX 重写：索引名限定为 o."idx"，ON 后表名**不带** schema；失败**记录**到证据，绝不 except pass 静默吞。
 ③ **逐表行级过滤**（不是只选表）：
      - 有 symbol 列的数据表：Binance 仅 526、OKX 仅 23，其他非白名单 symbol（含指数 BTCDOMUSDT/ALLUSDT/DEFIUSDT）一律排除。
      - kline_stream_state：无 symbol 列，但有 exchange/market；按 (binance/um, okx/swap) 行级过滤，并说明其字段为聚合运行计数。
      - api_scope_state：无 symbol/exchange，按 API host 计的运行/限流状态；逐列核对并标注为“运行状态”（非分类状态）。
 ④ 记录：逐表 源/目标/排除/指数/越界 行数；逐币目标行数 CSV；排除符号 CSV；WAL 前后；读取时点与时长。
 ⑤ 起始空间 < floor(12.2GiB) 中止。
用法：python 工具_热库scoped导出.py [--dry-run] [--floor-gib 12.2]
"""
import os, sys, sqlite3, re, json, hashlib, datetime, argparse, csv

ROOT = "/Users/boos/Desktop/威科夫因子共振趋势"
SRC = "/Users/boos/WorkBuddy AI/加密货币趋势判断系统/data/market_v2.sqlite3"
POOL = "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现/历史下载/标的池"
EVID = ROOT + "/evidence/Work_数据整理迁移_2026-10-15"
DST = ROOT + "/数据/hot/market_v2_scoped_kline_oi.sqlite3"
DRY = "/tmp/_scoped_dryrun.sqlite3"

IDX = ["BTCDOMUSDT", "ALLUSDT", "DEFIUSDT"]

# 有 symbol 列 -> 逐 symbol 行级过滤
SYM_FILTERED = ["candle_versions", "candle_heads", "oi_history", "sources",
                "symbol_pool", "symbol_supply", "realtime_klines",
                "candle_field_notes", "coverage_cache", "symbol_classification"]
# 无 symbol，有 exchange/market -> 按 (exchange,market) 行级过滤
EM_FILTERED = ["kline_stream_state"]
# 无 symbol/exchange -> API host 运行状态（逐列核对后保留，非分类状态）
HOST_STATE = ["api_scope_state"]
VIEWS = ["candles"]
ALL_TABLES = SYM_FILTERED + EM_FILTERED + HOST_STATE

NOTE = {
    "kline_stream_state": "无 symbol 列；按 (exchange,market) 行级过滤；字段为每(exchange,market,timeframe)聚合运行计数；symbols_planned=528 反映采集器实时计划(含2指数)，原样保留的运行计数，非逐币数据",
    "api_scope_state": "无 symbol/exchange；键为 API host；列=scope/cooldown_until/blocked/reason，属**运行/限流状态**（非分类状态），无符号维度故不适用白名单过滤",
    "symbol_classification": "有 symbol/exchange；本表为范围分类证据，目标仅保留白名单符号行，被排除行及原因另存 热库_symbol_classification_范围判定全表.csv",
}


def free_bytes(p):
    st = os.statvfs(p)
    return st.f_bavail * st.f_frsize


def rm_target(p):
    for suf in ("", "-journal", "-wal", "-shm"):
        if os.path.exists(p + suf):
            os.remove(p + suf)


def rewrite_object(sql, name, alias):
    pat = re.compile(r'(?is)^(\s*CREATE\s+(?:UNIQUE\s+)?(?:TABLE|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?)(["\[`]?)'
                     + re.escape(name) + r'\2')
    new, n = pat.subn(lambda m: m.group(1) + alias + '."' + name + '"', sql, count=1)
    return new if n else None


def rewrite_index(sql, alias):
    """CREATE [UNIQUE] INDEX [IF NOT EXISTS] [schema.]idx ON [schema.]tbl(...) [WHERE ...]
    -> index 名限定到目标 schema，ON 后表名去掉 schema。"""
    m = re.match(r'(?is)^(\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?)(.+?)(\s+ON\s+)(.+)$', sql)
    if not m:
        return None
    head, idxname, onkw, rest = m.group(1), m.group(2).strip(), m.group(3), m.group(4)
    idxbase = idxname.split('.')[-1].strip().strip('"[]`')
    m2 = re.match(r'(?is)^\s*([^\s(]+)\s*(.*)$', rest)
    if not m2:
        return None
    tblbase = m2.group(1).split('.')[-1].strip().strip('"[]`')
    return f'{head}{alias}."{idxbase}"{onkw}"{tblbase}"{m2.group(2)}'


def src_indexes(con, t):
    return con.execute("SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL ORDER BY name", (t,)).fetchall()


def build_group(con, alias, tables, pred, params, rows_out, idx_res, do_data=True):
    for t in tables:
        r = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
        if not r or not r[0]:
            rows_out.append({"表": t, "状态": "源无此表"}); continue
        obj = rewrite_object(r[0], t, alias)
        if not obj:
            raise RuntimeError(f"无法重写建表语句: {t}")
        con.execute(obj)
        n = con.execute(f'SELECT count(*) FROM main."{t}"').fetchone()[0]
        if do_data:
            if pred:
                con.execute(f'INSERT INTO {alias}."{t}" SELECT * FROM main."{t}" WHERE {pred}', params)
            else:
                con.execute(f'INSERT INTO {alias}."{t}" SELECT * FROM main."{t}"')
            m = con.execute(f'SELECT count(*) FROM {alias}."{t}"').fetchone()[0]
            rows_out.append({"表": t, "源行数": n, "目标行数": m, "排除行数": n - m,
                             "一致": (m <= n if pred else n == m), "备注": NOTE.get(t, "")})
            print(f"  {t}: 源={n} 目标={m} 排除={n-m}", flush=True)
        res = []
        for iname, isql in src_indexes(con, t):
            ns = rewrite_index(isql, alias)
            if not ns:
                res.append([iname, "无法重写"]); continue
            try:
                con.execute(ns); res.append([iname, "已建"])
            except sqlite3.Error as e:
                res.append([iname, f"失败:{e}"])
        idx_res[t] = res
        if not do_data:
            print(f"  {t}: schema-only 索引={res}", flush=True)


def make_preds(wb, wo):
    bin_ph = ",".join("?" * len(wb))
    okx_ph = ",".join("?" * len(wo))
    sym_pred = f"((exchange='binance' AND symbol IN ({bin_ph})) OR (exchange='okx' AND symbol IN ({okx_ph})))"
    sym_params = tuple(sorted(wb)) + tuple(sorted(wo))
    em_pred = "((exchange='binance' AND market='um') OR (exchange='okx' AND market='swap'))"
    return sym_pred, sym_params, em_pred, bin_ph, okx_ph


def dry_run(floor):
    rm_target(DRY)
    wal = SRC + "-wal"
    src = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True, isolation_level=None)
    src.execute("ATTACH DATABASE ? AS o", (DRY,))
    t0 = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        src.execute("BEGIN")
        idx_res = {}
        build_group(src, "o", ALL_TABLES, None, (), [], idx_res, do_data=False)
        for v in VIEWS:
            r = src.execute("SELECT sql FROM sqlite_master WHERE type='view' AND name=?", (v,)).fetchone()
            if r and r[0]:
                src.execute(rewrite_object(r[0], v, "o"))
        src.execute('INSERT INTO o."api_scope_state" SELECT * FROM main."api_scope_state"')
        src.execute("COMMIT")
    finally:
        try:
            src.execute("COMMIT")
        except Exception:
            pass
    t1 = datetime.datetime.now(datetime.timezone.utc).isoformat()
    src.close()
    ck = sqlite3.connect(DRY)
    tables = [r[0] for r in ck.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    views = [r[0] for r in ck.execute("SELECT name FROM sqlite_master WHERE type='view' ORDER BY name")]
    idx = ck.execute("SELECT tbl_name, name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY tbl_name, name").fetchall()
    api_rows = ck.execute('SELECT count(*) FROM "api_scope_state"').fetchone()[0]
    bad_on = [s for _, _, s in idx if re.search(r'(?is)\bON\s+o\s*\.', s or "")]
    ck.close()
    c2 = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
    src_idx = {t: [n for n, s in src_indexes(c2, t)] for t in ALL_TABLES}
    c2.close()
    miss = {t: [n for n in names if n not in [x[1] for x in idx if x[0] == t]] for t, names in src_idx.items()}
    miss = {k: v for k, v in miss.items() if v}
    rep = {"模式": "dry-run(schema-only)", "目标": DRY, "读取起": t0, "读取止": t1,
           "源WAL字节": os.path.getsize(wal) if os.path.exists(wal) else 0,
           "目标表": tables, "目标视图": views,
           "目标显式索引": [{"表": t, "索引": n, "SQL": s} for t, n, s in idx],
           "源在范围显式索引": {t: v for t, v in src_idx.items() if v},
           "缺失索引": miss, "非法ON_o索引": bad_on,
           "ATTACH写验证_api_scope_state行数": api_rows,
           "索引建立结果": {t: r for t, r in idx_res.items() if r},
           "结论": ("PASS" if not miss and not bad_on and api_rows == 5 else "FAIL")}
    with open(EVID + "/热库_scoped导出_dryrun.json", "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print(json.dumps({"结论": rep["结论"], "目标表数": len(tables), "目标视图数": len(views),
                      "目标显式索引数": len(idx), "缺失索引": miss, "非法ON_o": bad_on,
                      "ATTACH写验证行数": api_rows}, ensure_ascii=False, indent=1))
    rm_target(DRY)
    return 0 if rep["结论"] == "PASS" else 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--floor-gib", type=float, default=12.2)
    args = ap.parse_args()
    floor = int(args.floor_gib * (1 << 30))
    if args.dry_run:
        sys.exit(dry_run(floor))

    sc = json.load(open(POOL + "/scope_crypto.json"))
    wb = sorted(x["symbol"] for x in sc["binance_items"])
    wo = sorted(sc["okx_symbols"])
    sym_pred, sym_params, em_pred, bin_ph, okx_ph = make_preds(wb, wo)

    fr0 = free_bytes(ROOT)
    if fr0 < floor:
        print(f"[中止] 起始空闲 {fr0/(1<<30):.2f}GiB < floor {args.floor_gib}GiB，不写任何文件")
        sys.exit(3)

    rm_target(DST)
    os.makedirs(os.path.dirname(DST), exist_ok=True)
    wal = SRC + "-wal"
    wal_before = os.path.getsize(wal) if os.path.exists(wal) else 0
    db_size = os.path.getsize(SRC)
    t_start = datetime.datetime.now(datetime.timezone.utc).isoformat()
    ts0 = datetime.datetime.now()

    rows = []
    idx_res = {}
    src = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True, isolation_level=None)
    src.execute("ATTACH DATABASE ? AS o", (DST,))
    try:
        src.execute("BEGIN")  # 一致读事务（WAL 固定快照），仅导出期间持有
        build_group(src, "o", SYM_FILTERED, sym_pred, sym_params, rows, idx_res)
        build_group(src, "o", EM_FILTERED, em_pred, (), rows, idx_res)
        build_group(src, "o", HOST_STATE, None, (), rows, idx_res)
        for v in VIEWS:
            r = src.execute("SELECT sql FROM sqlite_master WHERE type='view' AND name=?", (v,)).fetchone()
            if r and r[0]:
                src.execute(rewrite_object(r[0], v, "o"))
        src.execute("COMMIT")
    finally:
        try:
            src.execute("COMMIT")
        except Exception:
            pass
        src.close()  # 立即释放读事务
    t_end = datetime.datetime.now(datetime.timezone.utc).isoformat()
    dur = round((datetime.datetime.now() - ts0).total_seconds(), 1)
    wal_after = os.path.getsize(wal) if os.path.exists(wal) else 0
    fr1 = free_bytes(ROOT)

    # ---- 目标校验 + 目标侧逐表行数/指数残留 ----
    ck = sqlite3.connect(DST)
    qc = ck.execute("PRAGMA quick_check(1)").fetchone()[0]
    integ = ck.execute("PRAGMA integrity_check(1)").fetchone()[0]
    tables = [r[0] for r in ck.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    views = [r[0] for r in ck.execute("SELECT name FROM sqlite_master WHERE type='view' ORDER BY name")]
    tgt_idx = [{"表": t, "索引": n} for t, n, s in ck.execute("SELECT tbl_name,name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY tbl_name,name")]
    idx_ph = ",".join("?" * len(IDX))
    tgt_index_rows = {}
    tgt_oob_rows = {}
    for t in SYM_FILTERED:
        if t not in tables:
            continue
        tgt_index_rows[t] = ck.execute(f'SELECT count(*) FROM "{t}" WHERE symbol IN ({idx_ph})', IDX).fetchone()[0]
        tgt_oob_rows[t] = ck.execute(f'SELECT count(*) FROM "{t}" WHERE NOT ({sym_pred})', sym_params).fetchone()[0]
    # 逐币目标行数
    with open(EVID + "/热库_scoped导出_逐币行数.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(["表", "exchange", "market", "symbol", "目标行数"])
        for t in SYM_FILTERED:
            if t not in tables:
                continue
            for ex, mk, sym, c in ck.execute(f'SELECT exchange,market,symbol,count(*) FROM "{t}" GROUP BY exchange,market,symbol ORDER BY exchange,market,symbol'):
                w.writerow([t, ex, mk, sym, c])
    ck.close()

    # ---- 源侧逐表 指数/越界 分解 + 排除符号 CSV ----
    c2 = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
    src_detail = {}
    with open(EVID + "/热库_scoped导出_排除符号.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(["表", "exchange", "market", "symbol", "源行数"])
        for t in SYM_FILTERED:
            n = c2.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
            idxn = c2.execute(f'SELECT count(*) FROM "{t}" WHERE symbol IN ({idx_ph})', IDX).fetchone()[0]
            excl = c2.execute(f'SELECT count(*) FROM "{t}" WHERE NOT ({sym_pred})', sym_params).fetchone()[0]
            src_detail[t] = {"源行数": n, "白名单内": n - excl, "排除行数": excl, "指数行数": idxn, "越界行数": excl - idxn}
            for ex, mk, sym, c in c2.execute(f'SELECT exchange,market,symbol,count(*) FROM "{t}" WHERE NOT ({sym_pred}) GROUP BY exchange,market,symbol ORDER BY 4 DESC', sym_params):
                w.writerow([t, ex, mk, sym, c])
    # symbol_classification 全表（范围判定证据，另存）
    scols = [r[1] for r in c2.execute('PRAGMA table_info("symbol_classification")')]
    with open(EVID + "/热库_symbol_classification_范围判定全表.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(scols)
        for r in c2.execute('SELECT * FROM symbol_classification'):
            w.writerow(r)
    c2.close()

    src_idx = {}
    c3 = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
    for t in ALL_TABLES:
        src_idx[t] = [n for n, s in src_indexes(c3, t)]
    c3.close()
    tgt_idx_names = {t: [x["索引"] for x in tgt_idx if x["表"] == t] for t in ALL_TABLES}
    idx_missing = {t: [n for n in src_idx[t] if n not in tgt_idx_names.get(t, [])] for t in ALL_TABLES}
    idx_missing = {k: v for k, v in idx_missing.items() if v}

    dst_size = os.path.getsize(DST)
    h = hashlib.sha256()
    with open(DST, "rb") as f:
        while True:
            b = f.read(1 << 22)
            if not b:
                break
            h.update(b)

    # 逐表行数合并（源分解 + 目标行数）
    tbl_rows = []
    tgt_map = {r["表"]: r for r in rows}
    for t in ALL_TABLES:
        rec = dict(tgt_map.get(t, {"表": t}))
        if t in src_detail:
            rec.update({"指数行数": src_detail[t]["指数行数"], "越界行数": src_detail[t]["越界行数"]})
            rec["目标指数残留"] = tgt_index_rows.get(t, 0)
            rec["目标越界残留"] = tgt_oob_rows.get(t, 0)
        rec["备注"] = NOTE.get(t, "")
        tbl_rows.append(rec)

    rep = {"源库": SRC, "源库字节": db_size,
           "源WAL字节_导出前": wal_before, "源WAL字节_导出后": wal_after, "WAL增量字节": wal_after - wal_before,
           "读取起": t_start, "读取止": t_end, "导出时长s": dur,
           "起始空闲GiB": round(fr0 / (1 << 30), 3), "结束空闲GiB": round(fr1 / (1 << 30), 3), "floorGiB": args.floor_gib,
           "白名单币安": len(wb), "白名单OKX": len(wo), "排除指数": IDX,
           "过滤方式": "逐表行级：有symbol的表按 exchange+symbol 白名单；kline_stream_state 按 (exchange,market)；api_scope_state 无符号维度",
           "目标": DST, "目标字节": dst_size, "目标GiB": round(dst_size / (1 << 30), 4),
           "目标SHA256": h.hexdigest(), "quick_check": qc, "integrity_check": integ,
           "含表": tables, "含视图": views, "逐表行数": tbl_rows,
           "目标指数残留合计": sum(tgt_index_rows.values()),
           "目标越界残留合计": sum(tgt_oob_rows.values()),
           "目标显式索引": tgt_idx, "源在范围显式索引": {t: v for t, v in src_idx.items() if v},
           "索引缺失": idx_missing, "索引建立结果": {t: r for t, r in idx_res.items() if r},
           "处理方式": "只读源(mode=ro,不用query_only)+ATTACH可写目标+BEGIN一致读事务；逐表行级白名单过滤；索引DDL正确重写；未复制活动库文件",
           "WAL钉住说明": f"读取事务仅持有 {dur}s（导出期间），COMMIT 后立即 close 释放；期间采集写入使 WAL 变化 {wal_after - wal_before} 字节，属一致快照固有代价"}
    with open(EVID + "/热库_scoped导出_证据.json", "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print(json.dumps({"目标字节": dst_size, "目标GiB": rep["目标GiB"], "目标SHA256": h.hexdigest(),
                      "quick_check": qc, "integrity_check": integ,
                      "目标指数残留合计": rep["目标指数残留合计"], "目标越界残留合计": rep["目标越界残留合计"],
                      "索引缺失": idx_missing, "WAL增量": wal_after - wal_before, "时长s": dur,
                      "空闲GiB": rep["结束空闲GiB"]}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
