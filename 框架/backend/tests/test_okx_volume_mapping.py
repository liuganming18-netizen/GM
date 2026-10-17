# -*- coding: utf-8 -*-
"""OKX REST 补口入库器 `import_rest` 的 **volume 字段映射契约**回归测试。

背景（2026-10-11 实测缺陷）：
  `logs/_pool_restfill.py::import_rest` 的 OKX 分支把 `r[5]`（`vol`，**张数**）写进了
  `candle_versions.volume`；而库内既有约定 / `09_来源抽样逐字段对账` 映射 /
  `okx_raw_json` 导入器均为 `volume ← r[6]`（`volCcy`，**基础币**）。二者相差合约 `ctVal` 倍。

本测试锁定契约（**离线、零网络、临时库**）：
  · OKX 行 `[ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]` ⇒
      `volume ← r[6]`（volCcy）、`quote_volume ← r[7]`（volCcyQuote）、
      `trade_count=0`、`taker_buy_volume=''`、`taker_buy_quote_volume=''`、
      `close_time = open_time + iv - 1`、`available_at=NULL`、`is_closed=1`；
  · OHLC 与其余列**逐字段不变**（防止「顺手改坏」）；
  · `ctVal=1`（`vol == volCcy`）与零量（`vol == volCcy == 0`）**数值不变**；
  · Binance 分支映射（`volume ← r[5]`）**不受影响**；
  · 十进制字符串**原样保留**（不经 float 往返，无精度损失）；
  · 同一批根**重复导入幂等**（不新增重复版本行）。

运行（必须 Python 3.13；无 pytest）：
  PY=/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3
  cd 后端实现 && PYTHONDONTWRITEBYTECODE=1 $PY -m unittest tests.test_okx_volume_mapping -v
"""
# ── W1-GUARD-BEGIN（2026-10-16）遗留组件守卫（显式 SKIP，非 PASS）──
# 本模块回归的是**旧工作区遗留件**（未随工程迁移，且属施工单禁止恢复项）。
# 组件缺失 ⇒ **显式 SKIP**：既不 PASS、也不静默 fallback（fail-closed 纪律）。
# 若遗留件将来被正式迁入，守卫自动失效，测试照常运行。
import importlib.util as _ilu_w1
from pathlib import Path as _PathW1

MIG_W1 = _PathW1(__file__).resolve().parents[3].joinpath('数据', '研究输入', '外部迁移_2026-10-16')
_missing_w1 = [str(p) for p in () if not p.exists()]
_missing_w1 += [m for m in ('_pool_restfill',) if _ilu_w1.find_spec(m) is None]
if _missing_w1:
    # 统一用模块级 `unittest.SkipTest`（2026-10-17 实测）：
    #   · `unittest discover` ⇒ `OK (skipped=1)`、退出码 0；
    #   · `pytest`           ⇒ `1 skipped`、退出码 0。
    # 旧写法 `pytest.skip(allow_module_level=True)` 抛的是 `Skipped`，**不是**
    # `unittest.SkipTest` 子类 ⇒ `unittest discover` 把它记成 **ERROR**（而非 skipped），
    # 令 `run_tests.sh` 门禁**永远非 0**。改用 `SkipTest` 后两种 runner 语义一致，
    # 仍是「显式 SKIP，非 PASS」。
    import unittest as _ut_w1
    raise _ut_w1.SkipTest(
        "W1: 旧工作区遗留组件未迁移且禁止恢复 ⇒ 显式 SKIP（非 PASS）: "
        + ", ".join(_missing_w1)
    )
# ── W1-GUARD-END ──

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent          # 后端实现/
# W1（2026-10-16）：原指向工程外路径（已删）。现按工程布局推导：
#   HERE = 框架/backend；FRAMEWORK = 框架/（含 `data_hub` 包）；PROJECT_ROOT = 工程根。
FRAMEWORK = HERE.parent
PROJECT_ROOT = HERE.parent.parent
PROJ = FRAMEWORK
sys.path.insert(0, str(FRAMEWORK))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "logs"))

_TMP = tempfile.TemporaryDirectory(prefix="okx_volume_mapping_")
TMP = Path(_TMP.name)
# 必须在 import _pool_restfill **之前**重定向，避免触碰正式库/断点/缓存目录
os.environ["MARKET_DB"] = str(TMP / "market.sqlite3")
os.environ["POOL_STATE_DIR"] = str(TMP / "state")
os.environ["POOL_CACHE_DIR"] = str(TMP / "cache")
os.environ["POOL_RAW_DIR"] = str(TMP / "raw")
os.environ["POOL_READONLY"] = "1"

import _pool_restfill as pr                                        # noqa: E402

TF = "15m"
IV = 900_000
SYM = "CRO-USDT-SWAP"


def _schema(con):
    con.executescript((PROJ / "data_hub" / "schema.sql").read_text(encoding="utf-8"))


def _okx_row(open_ms, vol="2", volccy="20", volccyquote="3.942", o="0.08062", h="0.08063",
             lo="0.08042", c="0.08045"):
    """OKX history-candles 原始行：[ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]。"""
    return [str(open_ms), o, h, lo, c, vol, volccy, volccyquote, "1"]


def _bn_row(open_ms):
    """Binance klines 原始行（12 列）。"""
    return [open_ms, "1.0", "2.0", "0.5", "1.5", "10.0", open_ms + IV - 1, "15.0", 3, "4.0", "6.0", "0"]


class OkxVolumeMapping(unittest.TestCase):
    def setUp(self):
        self.db = TMP / f"{self._testMethodName}.sqlite3"
        if self.db.exists():
            self.db.unlink()
        self.con = sqlite3.connect(str(self.db))
        _schema(self.con)
        self.con.commit()
        self.body = TMP / f"{self._testMethodName}.body"
        self.body.write_bytes(b'{"code":"0","data":[]}')
        self.ev = [{"url": "https://www.okx.com/api/v5/market/history-candles?instId=" + SYM,
                    "host": "www.okx.com", "http_code": 200, "body_path": str(self.body),
                    "body_bytes": 24, "body_sha256": "0" * 64, "sha_kind": "test",
                    "fetched_at": "2026-10-11T00:00:00Z", "phase": "fetch"}]

    def tearDown(self):
        self.con.close()

    # ── 工具 ──────────────────────────────────────────────────────────────
    def _import_okx(self, rows, symbol=SYM, tf=TF):
        return pr.import_rest(self.con, "okx", "swap", symbol, tf, rows,
                              "https://www.okx.com/api/v5/market/history-candles",
                              evidence=self.ev, link_path=TMP / f"{self._testMethodName}.link.json",
                              require_evidence=True)

    def _versions(self, sid):
        return [dict(zip(
            ("exchange", "market", "symbol", "timeframe", "open_time", "close_time", "open",
             "high", "low", "close", "volume", "quote_volume", "trade_count",
             "taker_buy_volume", "taker_buy_quote_volume", "source_id", "available_at",
             "is_closed"), r)) for r in self.con.execute(
            "SELECT * FROM candle_versions WHERE source_id=? ORDER BY open_time", (sid,))]

    # ── 核心：volume 取 r[6]=volCcy ────────────────────────────────────────
    def test_okx_volume_is_volccy_not_vol(self):
        """ctVal=10：vol=2 / volCcy=20 ⇒ 库内 volume 必须是 '20'（不是 '2'）。"""
        o = 1617871500000
        added, sid, ev = self._import_okx([(o, _okx_row(o, vol="2", volccy="20"))])
        self.assertEqual((added, ev), (1, "linked"))
        v = self._versions(sid)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0]["volume"], "20", "volume 必须取 r[6]=volCcy（基础币）")
        self.assertNotEqual(v[0]["volume"], "2", "不得写 r[5]=vol（张数）")

    def test_okx_other_columns_unchanged(self):
        """OHLC / quote_volume / 占位列 / 时间边界 / available_at / is_closed 逐字段不变。"""
        o = 1617871500000
        self._import_okx([(o, _okx_row(o))])
        sid = self.con.execute("SELECT id FROM sources ORDER BY id DESC LIMIT 1").fetchone()[0]
        v = self._versions(sid)[0]
        self.assertEqual(v["open"], "0.08062")
        self.assertEqual(v["high"], "0.08063")
        self.assertEqual(v["low"], "0.08042")
        self.assertEqual(v["close"], "0.08045")
        self.assertEqual(v["quote_volume"], "3.942")        # r[7] 未变
        self.assertEqual(v["close_time"], o + IV - 1)
        self.assertEqual(v["trade_count"], 0)               # OKX 无该字段 ⇒ 0
        self.assertEqual(v["taker_buy_volume"], "")         # 占位空串
        self.assertEqual(v["taker_buy_quote_volume"], "")
        self.assertIsNone(v["available_at"])
        self.assertEqual(v["is_closed"], 1)

    def test_okx_ctval_one_value_unchanged(self):
        """ctVal=1：vol == volCcy ⇒ 修复前后数值相同（LUNA/CORE/RAY/PI 等）。"""
        o = 1726574400000
        self._import_okx([(o, _okx_row(o, vol="1700", volccy="1700", volccyquote="15300.0"))])
        sid = self.con.execute("SELECT id FROM sources ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertEqual(self._versions(sid)[0]["volume"], "1700")

    def test_okx_ctval_lt_one_value_corrected(self):
        """ctVal=0.01（OKB）：vol=12345 / volCcy=123.45 ⇒ volume 必须是 '123.45'。"""
        o = 1756944000000
        self._import_okx([(o, _okx_row(o, vol="12345", volccy="123.45", volccyquote="7890.1"))],
                         symbol="OKB-USDT-SWAP")
        sid = self.con.execute("SELECT id FROM sources ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertEqual(self._versions(sid)[0]["volume"], "123.45")

    def test_okx_zero_volume(self):
        """零量根：vol == volCcy == '0' ⇒ 仍写 '0'，且不报错。"""
        o = 1617872400000
        added, sid, _ = self._import_okx([(o, _okx_row(o, vol="0", volccy="0", volccyquote="0"))])
        self.assertEqual(added, 1)
        self.assertEqual(self._versions(sid)[0]["volume"], "0")

    def test_okx_decimal_string_preserved_exactly(self):
        """十进制字符串**原样**落库（无 float 往返 ⇒ 无精度损失）。"""
        o = 1617873300000
        long_volccy = "123456789.123456789012345678901234567890"
        self._import_okx([(o, _okx_row(o, vol="12345678", volccy=long_volccy))])
        sid = self.con.execute("SELECT id FROM sources ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertEqual(self._versions(sid)[0]["volume"], long_volccy)

    def test_okx_second_run_is_idempotent(self):
        """同批根重复导入 ⇒ added=0 / no_rows，且库内不出现重复版本行。"""
        o = 1617871500000
        self._import_okx([(o, _okx_row(o))])
        added2, sid2, ev2 = self._import_okx([(o, _okx_row(o))])
        self.assertEqual((added2, sid2, ev2), (0, None, "no_rows"))
        n = self.con.execute("SELECT COUNT(*) FROM candle_versions").fetchone()[0]
        self.assertEqual(n, 1, "幂等：不得新增重复版本行")

    def test_binance_branch_unchanged(self):
        """Binance 分支映射不受影响：volume ← r[5]、quote ← r[7]、trade_count ← r[8]。"""
        o = 1567900800000
        added, sid, ev = pr.import_rest(
            self.con, "binance", "um", "TESTUSDT", "15m", [(o, _bn_row(o))],
            "https://fapi.binance.com/fapi/v1/klines",
            evidence=self.ev, link_path=TMP / f"{self._testMethodName}.link.json",
            require_evidence=True)
        self.assertEqual((added, ev), (1, "linked"))
        v = self._versions(sid)[0]
        self.assertEqual(v["volume"], "10.0")
        self.assertEqual(v["quote_volume"], "15.0")
        self.assertEqual(v["trade_count"], 3)
        self.assertEqual(v["taker_buy_volume"], "4.0")
        self.assertEqual(v["taker_buy_quote_volume"], "6.0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
