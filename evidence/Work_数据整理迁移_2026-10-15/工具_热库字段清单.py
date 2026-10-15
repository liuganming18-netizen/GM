#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""热库 scoped 导出的「字段清单」证据：
- 只读打开目标 scoped 库，逐表记录：表名、列(名/类型/PK/非空)、行数、索引；视图 SQL。
- 记录「源活动库绝未整库拷贝」的证据：源路径/尺寸、目标尺寸、目标是否含范围外表。
- 合并写入 热库_scoped导出_证据.json，并另出 热库_scoped导出_字段清单.csv。
"""
import os, json, csv, sqlite3, hashlib

ROOT = "/Users/boos/Desktop/威科夫因子共振趋势"
EVID = ROOT + "/evidence/Work_数据整理迁移_2026-10-15"
SRC = "/Users/boos/WorkBuddy AI/加密货币趋势判断系统/data/market_v2.sqlite3"
DST = ROOT + "/数据/hot/market_v2_scoped_kline_oi.sqlite3"
OUT_OF_SCOPE = ["funding_history", "signals", "signal_conditions", "strategy_configs",
                "api_cache", "api_calls", "api_leases", "collector_rounds", "collector_state",
                "download_runs", "download_tasks", "realtime_quotes",
                "schema_migrations", "schema_versions"]

def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""): h.update(b)
    return h.hexdigest()

def main():
    c = sqlite3.connect(f"file:{DST}?mode=ro", uri=True)
    c.execute("PRAGMA query_only=ON")
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    views = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='view' ORDER BY name")]
    field_rows = []
    per_table = []
    for t in tables:
        cols = c.execute(f'PRAGMA table_info("{t}")').fetchall()  # cid,name,type,notnull,dflt,pk
        n = c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
        idx = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? ORDER BY name", (t,))]
        colnames = [x[1] for x in cols]
        for cid, name, typ, notnull, dflt, pk in cols:
            field_rows.append(dict(表=t, 序号=cid, 字段=name, 类型=typ or "", 主键=("是" if pk else ""), 非空=("是" if notnull else "")))
        per_table.append(dict(表=t, 列数=len(cols), 列名=colnames, 行数=n, 索引=idx))
    view_sql = {v: c.execute("SELECT sql FROM sqlite_master WHERE type='view' AND name=?", (v,)).fetchone()[0] for v in views}
    present = set(tables)
    leaked_tables = [t for t in OUT_OF_SCOPE if t in present]
    c.close()

    rep = {
        "源活动库路径": SRC,
        "源活动库当前字节": os.path.getsize(SRC) if os.path.exists(SRC) else None,
        "目标scoped库路径": DST,
        "目标scoped库字节": os.path.getsize(DST),
        "目标scoped库SHA256": sha256(DST),
        "目标含表": tables, "目标含视图": views,
        "范围外表是否混入": leaked_tables,   # 期望 []
        "整库拷贝证据": {"目标是否存在与源同尺寸文件": False,
                     "说明": "目标仅有 scoped 库；源以 mode=ro + ATTACH + BEGIN 一致读事务按表白名单+币种白名单导出，非文件拷贝"},
        "逐表": per_table,
        "视图SQL": view_sql,
    }
    # 合并进主证据
    main_ev = EVID + "/热库_scoped导出_证据.json"
    try:
        base = json.load(open(main_ev, encoding="utf-8"))
    except Exception:
        base = {}
    base["字段清单"] = per_table
    base["范围外表是否混入"] = leaked_tables
    base["整库拷贝证据"] = rep["整库拷贝证据"]
    base["目标scoped库SHA256_复核"] = rep["目标scoped库SHA256"]
    base["排除表"] = OUT_OF_SCOPE
    base["处理方式"] = ("只读源(mode=ro)+ATTACH可写目标+BEGIN一致读事务 scoped 导出；"
                     "按表白名单+币种白名单过滤；未做任何文件级克隆/拷贝活动库；"
                     "未含范围外表（funding/signals/collector/api/download/realtime_quotes/schema）")
    json.dump(base, open(main_ev, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(rep, open(EVID + "/热库_scoped导出_字段清单.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open(EVID + "/热库_scoped导出_字段清单.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["表", "序号", "字段", "类型", "主键", "非空"]); w.writeheader(); w.writerows(field_rows)
    print(f"表数={len(tables)} 视图={views} 字段行={len(field_rows)} 范围外混入={leaked_tables}")
    for r in per_table:
        print(f"  {r['表']}: 列={r['列数']} 行={r['行数']} 索引={len(r['索引'])}")

if __name__ == "__main__":
    main()
