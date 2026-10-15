"""SQLite单机存储；历史下载时间与策略可用时间分别保存。"""

import csv
import hashlib
import io
import json
import sqlite3
import time
import zipfile
from contextlib import contextmanager, closing
from decimal import Decimal, InvalidOperation
from pathlib import Path

INTERVAL_MS = {"1m": 60000, "5m": 300000, "15m": 900000, "30m": 1800000,
               "1h": 3600000, "4h": 14400000, "8h": 28800000,
               "12h": 43200000, "1d": 86400000}


def now_ms():
    return int(time.time() * 1000)


class MarketStore:
    # P3 切流（2026-10-13）：默认库改为**新热库** market_v2.sqlite3。
    # 旧 market.sqlite3 已退役；此处若仍留旧默认，任何一次裸构造都会
    # `mkdir -p` + 建 WAL 把旧库**静默重建**成空库，破坏退役前提。
    def __init__(self, path="data/market_v2.sqlite3", clock=now_ms):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        with closing(self.connect()) as con, con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(Path(__file__).with_name("schema.sql").read_text())
            con.execute("INSERT OR IGNORE INTO schema_versions VALUES (1, ?)", (clock(),))

    def connect(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=10000")
        return con

    @contextmanager
    def transaction(self):
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def import_binance_zip(self, path, checksum_path, symbol, timeframe, source_url,
                           downloaded_at=None):
        """只导入校验通过的币安原生合约归档，保留每个文件版本。"""
        if timeframe not in INTERVAL_MS:
            raise ValueError("未配置的原生周期，不进行聚合")
        path = Path(path)
        # 一个文件一个批次，不加载整个币池；大文件后续用流式导入适配器。
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("此样例导入器限32MB，较大文件须分批导入")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        expected = Path(checksum_path).read_text().split()[0]
        if digest != expected:
            raise ValueError("官方校验和不匹配，拒绝入库")
        duration = INTERVAL_MS[timeframe]
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            if len(names) != 1 or not names[0].endswith(".csv"):
                raise ValueError("归档应包含唯一CSV")
            if archive.getinfo(names[0]).file_size > 128 * 1024 * 1024:
                raise ValueError("解压后超过样例导入器容量")
            expected_name = f"{symbol}-{timeframe}-"
            if not Path(names[0]).name.startswith(expected_name):
                raise ValueError("CSV标的或周期与入库参数不符")
            rows = list(csv.reader(io.StringIO(archive.read(names[0]).decode("utf-8-sig"))))
        if rows and rows[0][0] == "open_time":
            rows = rows[1:]
        if not rows:
            raise ValueError("CSV没有实际K线")
        previous = None
        for r in rows:
            if len(r) != 12:
                raise ValueError("K线字段应为12列")
            opened, closed = int(r[0]), int(r[6])
            if opened % duration or closed != opened + duration - 1 or closed >= self.clock():
                raise ValueError("时间边界不正确或K线尚未收盘")
            if previous is not None and opened <= previous:
                raise ValueError("开盘时间重复或未按升序排列")
            previous = opened
            try:
                values = [Decimal(r[i]) for i in (1, 2, 3, 4, 5, 7, 9, 10)]
            except InvalidOperation as exc:
                raise ValueError("行情数值格式不正确") from exc
            if not all(v.is_finite() and v >= 0 for v in values):
                raise ValueError("行情必须为有限非负数值")
            op, hi, lo, cl = values[:4]
            if lo > min(op, cl) or hi < max(op, cl) or lo > hi or min(op, cl) <= 0:
                raise ValueError("OHLC价格关系不正确")
            if int(r[8]) < 0:
                raise ValueError("成交笔数不能为负数")
        key = ("binance", "um", symbol, timeframe)
        with self.transaction() as con:
            old = con.execute("SELECT id FROM sources WHERE exchange=? AND market=? AND symbol=? "
                              "AND timeframe=? AND sha256=?", (*key, digest)).fetchone()
            if old:
                return {"来源编号": old["id"], "新增版本数": 0, "状态": "已导入，不重复写入"}
            cursor = con.execute("INSERT INTO sources(exchange,market,symbol,timeframe,source_url,"
                                 "file_path,sha256,imported_at,downloaded_at,first_open,last_close,row_count)"
                                 " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (*key, source_url, str(path.resolve()), digest, self.clock(),
                                  downloaded_at, int(rows[0][0]), int(rows[-1][6]), len(rows)))
            source_id = cursor.lastrowid
            for r in rows:
                con.execute("INSERT INTO candle_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (*key, int(r[0]), int(r[6]), *r[1:6], r[7], int(r[8]), r[9], r[10],
                             source_id, None, 1))
                # 归档只补缺，不得把已有实时/REST head 改回 available_at=NULL 的历史版本。
                con.execute("INSERT INTO candle_heads VALUES (?,?,?,?,?,?) "
                            "ON CONFLICT(exchange,market,symbol,timeframe,open_time) DO NOTHING",
                            (*key, int(r[0]), source_id))
        return {"来源编号": source_id, "新增版本数": len(rows), "状态": "已入库"}

    def candles(self, exchange, market, symbol, timeframe, start=0, end=None, limit=500,
                source_id=None):
        if not 1 <= limit <= 2000:
            raise ValueError("每次读取限制为1至2000根")
        end = self.clock() if end is None else end
        table = "candles" if source_id is None else "candle_versions"
        sql = f"SELECT * FROM {table} WHERE exchange=? AND market=? AND symbol=? AND timeframe=? "
        params = [exchange, market, symbol, timeframe, start, end]
        sql += "AND open_time>=? AND open_time<? "
        if source_id is not None:
            sql += "AND source_id=? "
            params.append(source_id)
        sql += "ORDER BY open_time DESC LIMIT ?"
        params.append(limit)
        with closing(self.connect()) as con:
            result = [dict(r) for r in con.execute(sql, params)]
        return list(reversed(result))

    def status(self):
        with closing(self.connect()) as con:
            coverage = [dict(r) for r in con.execute(
                "SELECT exchange,market,symbol,timeframe,MIN(open_time) AS first_open,"
                "MAX(close_time) AS last_close,COUNT(*) AS rows FROM candles "
                "GROUP BY exchange,market,symbol,timeframe")]
            for c in coverage:
                duration = INTERVAL_MS[c["timeframe"]]
                expected = (c["last_close"] + 1 - c["first_open"]) // duration
                c["missing_within_observed_range"] = expected - c["rows"]
            sources = con.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
            states = [dict(r) for r in con.execute("SELECT * FROM api_scope_state")]
            calls = con.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0]
        return {"数据库": str(self.path.resolve()), "来源文件数": sources,
                "已入库覆盖": coverage, "外部请求记录数": calls, "接口冷却或暂停状态": states}
