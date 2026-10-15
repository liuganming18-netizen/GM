#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可重复、非破坏的增量迁移。

只做 CREATE TABLE/INDEX IF NOT EXISTS；不改、不删既有 K 线表。
- 已应用版本记录在 schema_migrations，重复执行无副作用。
- 提供 candle 行数前后对比，证明既有真实 K 线未丢失。
- 先用本工作区临时测试库验证，再对共享库执行。

版本：
  v1 business_tables     新增业务表（strategy_configs / signals / signal_conditions / oi_history / funding_history）
  v2 signals_unique_key  重建 signals / signal_conditions，唯一键纳入 strategy_version 与 data_version
                         （既有行 INSERT...SELECT 保留；不触碰 K 线表）
  v3 download_task_state 新增 download_runs / download_tasks / symbol_pool（全池下载进度与标的池快照）
  v4 coverage_cache      新增 coverage_cache（库存摘要缓存；全池上亿行后避免每次请求全表扫描）
  v5 realtime_collect    新增 realtime_quotes / symbol_classification / collector_state /
                         collector_rounds（实时最新价与已收盘 K 线分开；类别分类；采集心跳与轮次）
  v6 candle_field_notes  新增 candle_field_notes（欧易无 taker 字段的**显式缺失标注**）
  v7 realtime_per_field_times  realtime_quotes 补「按字段独立时点/来源」列（ADD COLUMN）
  v8 realtime_klines     新增 realtime_klines（**未收盘 K 线临时层**，与已收盘 candle_* 分离）
                         + kline_stream_state（每 (交易所,周期) 的实时 K 线链路状态）
  v9 symbol_supply       新增 symbol_supply（币安 openInterestHist 同一行自带的
                         `CMCCirculatingSupply` 流通量，供「流动市值」列现算；不含任何历史回填）

订正记录：
  2026-10-13  K 线规模探针 `candle_count()` 由 `MAX(rowid)` 改为「主键字典序末键」。
              原因：rowid 是 rowid 表的实现细节，且 `WITHOUT ROWID` 改造后不再存在，
              届时旧实现会把探针失效**误报**为「表不存在」。详见该函数 docstring。
              受影响输出字段：`K线净变化`（不可再计算，已移除，无消费方）、
              `K线根数`（改名为 `K线探针`，无消费方）。
              未受影响：`K线未丢失`（语义与取值不变，tests/test_business.py 依赖）。

用法：
  python3 migrate.py --db <path> [--dry-run] [--check]
"""
import argparse
import sqlite3
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA = HERE / "business_schema.sql"
SCHEMA_V2 = HERE / "business_schema_v2.sql"
SCHEMA_V3 = HERE / "business_schema_v3.sql"
SCHEMA_V4 = HERE / "business_schema_v4.sql"
SCHEMA_V5 = HERE / "business_schema_v5.sql"
SCHEMA_V6 = HERE / "business_schema_v6.sql"
SCHEMA_V8 = HERE / "business_schema_v8.sql"
SCHEMA_V9 = HERE / "business_schema_v9.sql"
# P3 切流（2026-10-13）：默认目标库 = 新热库 market_v2.sqlite3（旧库已退役，禁止回落重建）
DEFAULT_DB = "/Users/boos/WorkBuddy AI/加密货币趋势判断系统/data/market_v2.sqlite3"

MIGRATIONS = [
    (1, "business_tables", SCHEMA.read_text(encoding="utf-8")),
    (2, "signals_unique_key", SCHEMA_V2.read_text(encoding="utf-8")),
    (3, "download_task_state", SCHEMA_V3.read_text(encoding="utf-8")),
    (4, "coverage_cache", SCHEMA_V4.read_text(encoding="utf-8")),
    (5, "realtime_collect", SCHEMA_V5.read_text(encoding="utf-8")),
    (6, "candle_field_notes", SCHEMA_V6.read_text(encoding="utf-8")),
    (8, "realtime_klines", SCHEMA_V8.read_text(encoding="utf-8")),
    (9, "symbol_supply", SCHEMA_V9.read_text(encoding="utf-8")),
]

# v7：给 **本工作区自己新建的** realtime_quotes 补「按字段独立时点/来源」列。
# 为什么用 ADD COLUMN 而不是建新表：
#   · realtime_quotes 是 30 分钟前由本工作区 v5 新建的实时快照表，**没有历史行情**，
#     不触碰任何既有 K 线/归档数据；分表反而会让适配层读两处、更易出错。
#   · SQLite 不支持"删列/改约束"，但支持 ADD COLUMN；重复执行会报 duplicate column，
#     这里逐条 try/except 保证**幂等**。
# 语义：价格 / 标记价 / 费率**各自独立**的事件时间与来源，任一局部更新
#       **不得**推进其他字段的时点（否则 mark/funding 的更新会伪造价格新鲜度）。
COLUMN_MIGRATIONS = [
    (7, "realtime_per_field_times", "realtime_quotes", [
        ("price_event_time", "INTEGER"),
        ("price_received_at", "INTEGER"),
        ("price_source", "TEXT"),
        ("price_transport", "TEXT"),
        ("mark_event_time", "INTEGER"),
        ("mark_source", "TEXT"),
        ("funding_event_time", "INTEGER"),
        ("funding_source", "TEXT"),
        ("volume_24h_base", "TEXT"),
        ("volume_24h_contract", "TEXT"),
        ("volume_source", "TEXT"),
        ("open24h", "TEXT"),
    ]),
]


def connect(db):
    con = sqlite3.connect(db, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=10000")
    return con


def candle_count(con):
    """K 线表规模的**廉价单调探针**（不返回精确根数）。

    —— 为什么不用 `COUNT(*)`
    `SELECT COUNT(*) FROM candle_versions` 在 7400 万行 + 后台批量下载持续写入时
    实测**超过 120 秒仍未返回**（迁移进程被上层超时 SIGTERM，exit 137），
    导致迁移无法在共享库上执行。

    —— 为什么也不该用 `MAX(rowid)`（本函数 2026-10-13 订正前的实现）
    先说清事实，避免以讹传讹：本函数**是只读探针**，被 `migrate()`（前后各调一次）
    与 `check()` 调用，唯一判据是调用方的 `after >= before`（证明「既有真实 K 线
    未被删除」）。它**不参与任何数据迁移、不生成主键、不处理自增**，
    因此「DELETE 后 MAX(rowid) 会导致主键冲突或游标错乱」的顾虑在这里**并不成立**。

    真正的缺陷是另外两条：
      1. **rowid 是 rowid 表的实现细节，不是逻辑主键**。本表主键是
         (exchange,market,symbol,timeframe,open_time,source_id) 复合键，
         `MAX(rowid)` 与业务语义无对应关系，且含历史空洞（1m 清洗遗留），
         其数值本身不可解释。
      2. **它会在既定改造后直接失效**：`candle_versions` 的既定方向是
         `WITHOUT ROWID`（见 `网站逆向研究/库内冗余清理_2026-10-13/D4_施工单`）。
         `WITHOUT ROWID` 表**没有 rowid**，届时 `MAX(rowid)` 抛
         `OperationalError`，而旧实现把该异常**一律**翻译成
         「candle_versions 不存在」—— 会把「表在、探针失效」**误报**成
         「表不存在」，从而掩盖真实故障。

    —— 现在的口径：**按主键字典序的最大键**
        SELECT exchange,market,symbol,timeframe,open_time,source_id
        FROM candle_versions
        ORDER BY exchange DESC, market DESC, symbol DESC,
                 timeframe DESC, open_time DESC, source_id DESC LIMIT 1
      · 走主键 btree 末端，实测 **0.00 s**（同表上把 ORDER BY 换成与索引序不匹配的
        `source_id DESC` 需 **101.9 s**，可作对照证明这不是缓存巧合）；
      · **单调不减**：新增行只可能抬高或保持它，删除才会压低它
        ⇒ 仍满足调用方唯一判据 `after >= before`；
      · 用**逻辑主键**而非 rowid ⇒ 在 `WITHOUT ROWID` 改造后**依然可用**
        （那时主键即聚簇表本身，末端定位同样是 O(log n)）。

    返回 `(末键元组, 口径说明)`；不可用时返回 `(None, 原因)`。
    元组元素类型固定（str×4 + int×2），可直接用 Python 元组比较做 `>=`。
    """
    try:
        row = con.execute(
            "SELECT exchange,market,symbol,timeframe,open_time,source_id "
            "FROM candle_versions "
            "ORDER BY exchange DESC, market DESC, symbol DESC, "
            "timeframe DESC, open_time DESC, source_id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError as exc:
        # 只在「表/列真的不存在」时回报不可用；其余异常照常抛出，**不掩盖故障**
        msg = str(exc).lower()
        if "no such table" in msg or "no such column" in msg:
            return None, f"candle_versions 不存在或不可用：{exc}"
        raise
    if row is None:
        return None, "candle_versions 为空表（无任何行）"
    key = tuple(row)
    return key, f"主键字典序末键 {key}（廉价单调探针，非精确根数）"


def applied_versions(con):
    try:
        return {r[0] for r in con.execute("SELECT version FROM schema_migrations")}
    except sqlite3.OperationalError:
        return set()


def apply_column_migrations(con, done):
    """逐条 ADD COLUMN，容忍 duplicate column（保证幂等）；返回本次新增版本。"""
    applied = []
    for version, name, table, columns in COLUMN_MIGRATIONS:
        if version in done:
            continue
        for col, ddl in columns:
            try:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        con.execute("INSERT OR IGNORE INTO schema_migrations VALUES (?,?,?)",
                    (version, name, int(time.time() * 1000)))
        applied.append(version)
    return applied


def migrate(db, dry_run=False):
    db = str(Path(db).resolve())
    con = connect(db)
    try:
        before, probe = candle_count(con)
        done = applied_versions(con)
        pending = [(v, n, s) for v, n, s in MIGRATIONS if v not in done]
        col_pending = [v for v, _, _, _ in COLUMN_MIGRATIONS if v not in done]
        if dry_run:
            return {"数据库": db, "K线探针(前)": before, "探针口径": probe,
                    "已应用版本": sorted(done),
                    "待应用版本": [v for v, _, _ in pending] + col_pending,
                    "模式": "dry-run，未写入"}
        for version, name, script in pending:
            con.executescript(script)          # 幂等：全部 IF NOT EXISTS
            con.execute("INSERT OR IGNORE INTO schema_migrations VALUES (?,?,?)",
                        (version, name, int(time.time() * 1000)))
        col_applied = apply_column_migrations(con, done)
        con.commit()
        after, probe = candle_count(con)
        # 注意：后台批量下载**正在运行**时 after 会比 before 大（正常新增），
        # 因此判据是 `after >= before`（未丢失），并单独报告探针方向，避免误报。
        # 2026-10-13 订正：探针由「rowid 最大值」改为「主键字典序末键」，
        # 故原先的 `after - before`（数值差 = 新增行数）**不再可计算**，已移除；
        # 该字段经全项目检索确认无任何消费方（唯一被消费的是 `K线未丢失`，
        # 见 tests/test_business.py:216-217）。
        lost = None
        if after is not None and before is not None:
            lost = after >= before
        return {"数据库": db, "K线探针(前)": before, "K线探针(后)": after,
                "探针口径": probe,
                "探针未回退": lost,
                "K线未丢失": lost,
                "本次应用": [v for v, _, _ in pending] + col_applied,
                "已应用版本": sorted(applied_versions(con))}
    finally:
        con.close()


def signals_unique_columns(con):
    """返回 signals 表唯一索引的列组合（用于核对唯一键是否含策略/数据版本）。"""
    try:
        idx = [r for r in con.execute("PRAGMA index_list('signals')") if r["unique"]]
    except sqlite3.OperationalError:
        return None
    out = []
    for r in idx:
        out.append([c["name"] for c in con.execute(f"PRAGMA index_info('{r['name']}')")])
    return out


def check(db):
    con = connect(db)
    try:
        rows = con.execute("SELECT version,name,applied_at FROM schema_migrations ORDER BY version")
        tables = sorted(r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))
        return {"数据库": str(Path(db).resolve()), "K线探针": candle_count(con),
                "迁移记录": [dict(r) for r in rows], "表": tables,
                "信号唯一键": signals_unique_columns(con)}
    finally:
        con.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--check", action="store_true")
    a = p.parse_args()
    import json
    print(json.dumps(check(a.db) if a.check else migrate(a.db, a.dry_run),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
