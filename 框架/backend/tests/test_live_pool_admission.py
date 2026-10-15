# -*- coding: utf-8 -*-
"""在线准入 · **逐指标真实最小历史**回归测试（离线、零网络、零写库）。

被测对象（**测谁就 import 谁**）
------------------------------
`后端实现/live_pool.py` —— 在线候选入口（本轮由「整币统一 240h」改为
「逐指标真实最小历史 + 逐时点列缺项」）。

为什么要锁这些用例（人类 2026-10-13 补充复核）
--------------------------------------------
人类明确：把「连续 240 根有效 1h」当**准入条件**就是**已撤回的人为 240h 门槛**，
**不得改名成「策略预热」后恢复**。故本测试把以下三件事钉死：

  A. 准入下限 = `max(逐指标 min_bars)` = **26**（且 **≠ 240**），且各项数值与冻结源码推导一致；
  B. `240` **不出现**在准入规则载荷里（`POOL_RULE_LIVE`），也**不参与** `rule_identity()`；
     它只以 `STRATEGY_SELF_GATE`（`used_for_admission=False`）身份出现；
  C. 新规则相对旧 240 规则**只放宽不变严**（`{240 准入日} ⊆ {26 准入日}`），
     且 `per_day_missing` 会**逐个写出缺哪个指标**（不是整币一句「不足 240 根」）。

依赖：`live_pool` 在模块级 `import numpy`。门禁解释器若缺 numpy ⇒ 本文件**整体 skip**
（不伪装通过）；跑通它请用 `envs/freqtrade/bin/python` 或任何含 numpy 的解释器。
"""
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
for _p in (str(BACKEND), str(BACKEND / "logs")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_ERR = None
try:
    import numpy as np                                   # noqa: F401
    import live_pool as LP
except Exception as exc:                                 # noqa: BLE001
    LP = None
    _ERR = f"{type(exc).__name__}: {exc}"

DAY = 24 * 3_600_000
H = 3_600_000


def _synthetic(n_bars, *, start_ms=1_800_000_000_000, close_last=None):
    """构造 `n_bars` 根**连续、原生、合法**的 1h bar（从整点日界开始）。"""
    start = (start_ms // DAY) * DAY
    ts = np.arange(n_bars, dtype=np.int64) * H + start
    o = np.full(n_bars, 100.0)
    hi = np.full(n_bars, 101.0)
    lo = np.full(n_bars, 99.0)
    cl = np.full(n_bars, 100.5)
    vol = np.full(n_bars, 10.0)
    q = np.full(n_bars, 1000.0)
    if close_last is not None:                           # 制造一根「非法」bar 以切断游程
        idx = int(close_last)
        cl[idx] = -1.0
    return ts, o, hi, lo, cl, vol, q


@unittest.skipIf(LP is None, f"numpy / live_pool 不可用 ⇒ 跳过（不伪装通过）：{_ERR}")
class TestPerIndicatorMinHistory(unittest.TestCase):

    def test_admission_min_bars_is_26_and_not_240(self):
        self.assertEqual(LP.ADMISSION_MIN_BARS, 26)
        self.assertNotEqual(LP.ADMISSION_MIN_BARS, 240)

    def test_each_indicator_min_bars_matches_frozen_source(self):
        m = {k: v["min_bars"] for k, v in LP.INDICATOR_MIN_HISTORY.items()}
        # 与冻结源码逐项对照（don_entry_bars=20 / vol_n=20 / atr_n=20 /
        # don_exit_bars=10 / sc_leg_bars=10 / spring_confirm_bars=3 / sc_confirm_bars=5）
        self.assertEqual(m["donchian_entry_channel"], 21)
        self.assertEqual(m["volume_baseline"], 21)
        self.assertEqual(m["atr_ewm"], 20)
        self.assertEqual(m["atr_previous_bar_anchor"], 21)
        self.assertEqual(m["donchian_exit_channel"], 11)
        self.assertEqual(m["spring_box"], 21)
        self.assertEqual(m["spring_confirm_window"], 24)
        self.assertEqual(m["sc_box"], 21)
        self.assertEqual(m["sc_leg"], 11)
        self.assertEqual(m["sc_close_mean_incl_current"], 20)
        self.assertEqual(m["sc_confirm_window"], 26)
        # 准入下限必须正好是各项最大值（改任何一项都会改下限）
        self.assertEqual(LP.ADMISSION_MIN_BARS, max(m.values()))

    def test_240_is_not_in_admission_rule_payload(self):
        """准入规则里**不得出现数值 240**（原文引用人类指令的字符串不算判据，故只查数值）。"""
        def numeric_240(o):
            if isinstance(o, dict):
                return any(numeric_240(v) for v in o.values())
            if isinstance(o, list):
                return any(numeric_240(v) for v in o)
            return isinstance(o, (int, float)) and not isinstance(o, bool) and o == 240
        self.assertFalse(numeric_240(LP.POOL_RULE_LIVE),
                         "准入规则载荷里出现了数值 240 ⇒ 人为门槛可能被改名恢复")
        self.assertEqual(LP.POOL_RULE_LIVE["admission_min_bars"], 26)
        self.assertEqual(max(LP.POOL_RULE_LIVE["indicator_min_history"].values()), 26)
        self.assertTrue(LP.POOL_RULE_LIVE["uniform_240h_gate_removed"])
        self.assertTrue(LP.POOL_RULE_LIVE["per_timepoint_missing_listing"])
        self.assertTrue(LP.POOL_RULE_LIVE["short_history_coins_keep_usable_window"])

    def test_strategy_self_gate_is_not_admission(self):
        self.assertFalse(LP.STRATEGY_SELF_GATE["used_for_admission"])
        self.assertEqual(LP.STRATEGY_SELF_GATE["param"], "warmup_contiguous_bars")

    def test_rule_identity_changes_when_admission_floor_changes(self):
        """身份哈希必须覆盖准入下限 ⇒ 改了准入就换身份（可复核）。"""
        base = LP.rule_identity()
        saved = LP.POOL_RULE_LIVE["admission_min_bars"]
        try:
            LP.POOL_RULE_LIVE["admission_min_bars"] = 240
            self.assertNotEqual(LP.rule_identity(), base)
        finally:
            LP.POOL_RULE_LIVE["admission_min_bars"] = saved
        self.assertEqual(LP.rule_identity(), base)

    def test_new_rule_is_pure_relaxation_over_240(self):
        """{旧 240 准入日} ⊆ {新 26 准入日}，且 100 根连续时旧规则零准入、新规则有准入。"""
        ts, o, hi, lo, cl, vol, q = _synthetic(100)
        listed = int(ts[0])
        end = int(ts[-1]) + 2 * H
        old = LP.qualify_symbol_live(ts, o, hi, lo, cl, vol, q, listed, int(ts[0]), end,
                                     min_history=240)
        new = LP.qualify_symbol_live(ts, o, hi, lo, cl, vol, q, listed, int(ts[0]), end,
                                     min_history=LP.ADMISSION_MIN_BARS)
        self.assertEqual(len(old), 0, "100 根连续时旧 240 规则不应有任何准入日")
        self.assertGreater(len(new), 0, "100 根连续时新规则应保留可用时段")
        # 300 根连续：两者都有准入日，且旧的是新的子集
        ts2, o2, hi2, lo2, cl2, vol2, q2 = _synthetic(300)
        end2 = int(ts2[-1]) + 2 * H
        old2 = {d for (d, *_r) in LP.qualify_symbol_live(
            ts2, o2, hi2, lo2, cl2, vol2, q2, int(ts2[0]), int(ts2[0]), end2, min_history=240)}
        new2 = {d for (d, *_r) in LP.qualify_symbol_live(
            ts2, o2, hi2, lo2, cl2, vol2, q2, int(ts2[0]), int(ts2[0]), end2,
            min_history=LP.ADMISSION_MIN_BARS)}
        self.assertTrue(old2)
        self.assertTrue(new2)
        self.assertTrue(old2.issubset(new2), "新规则不得比旧规则更严")
        self.assertTrue(new2 - old2, "新规则应多出「旧规则被挡掉」的准入日")

    def test_per_day_missing_names_the_indicator(self):
        """逐时点缺项必须写**指标名**，而不是整币一句「不足 240 根」。"""
        ts, o, hi, lo, cl, vol, q = _synthetic(80)
        listed = int(ts[0])
        days = [int(ts[0]) + k * DAY for k in (1, 2, 3)]
        rows = LP.per_day_missing(ts, o, hi, lo, cl, vol, q, days, listed,
                                 panel_end_ms=int(ts[-1]) + H)
        by_day = {r["day_utc"]: r for r in rows}
        # 第 1 个日界：其前一根 = 起始日 23:00 ⇒ 游程 24 ⇒ 只差 sc_confirm_window(26)
        r1 = by_day[sorted(by_day)[0]]
        self.assertEqual(r1["contiguous_valid_hours_before_day"], 24)
        self.assertEqual(r1["missing_indicators"], ["sc_confirm_window"])
        self.assertFalse(r1["admitted"])
        # 第 2 个日界：游程 48 ⇒ 全部可算
        r2 = by_day[sorted(by_day)[1]]
        self.assertEqual(r2["missing_indicators"], [])
        self.assertTrue(r2["admitted"])
        # 每行都带「各指标所需 bar 数」，可直接核对
        self.assertEqual(r2["per_indicator_bars"]["sc_confirm_window"], 26)

    def test_per_day_missing_reports_gap_reset(self):
        """断一根（非法 bar）⇒ 游程重置，缺项重新出现（不跨段拼接）。"""
        ts, o, hi, lo, cl, vol, q = _synthetic(200)
        cl[100] = -1.0                                       # 第 100 根非法
        days = [int(ts[0]) + k * DAY for k in (5, 6)]
        rows = LP.per_day_missing(ts, o, hi, lo, cl, vol, q, days, int(ts[0]),
                                  panel_end_ms=int(ts[-1]) + H)
        # 断点之后游程被重置 ⇒ 后面的日界必然缺项
        self.assertTrue(any(not r["admitted"] for r in rows))

    def test_per_day_missing_before_listing_flagged(self):
        ts, o, hi, lo, cl, vol, q = _synthetic(80)
        listed = int(ts[0]) + 10 * DAY
        days = [int(ts[0]) + 3 * DAY]
        rows = LP.per_day_missing(ts, o, hi, lo, cl, vol, q, days, listed,
                                  panel_end_ms=int(ts[-1]) + H)
        self.assertEqual(rows[0].get("reason"), "before_official_listing")


if __name__ == "__main__":
    unittest.main()
