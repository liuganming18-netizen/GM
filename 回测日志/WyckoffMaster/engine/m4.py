# -*- coding: utf-8 -*-
"""M4 排序：OI 变化自身 90 日分位（降序），平手用 30 日成交额 rank → exchange → symbol。

复用 `策略/WyckoffMaster/factors.py`：oi_change_20 / rolling_pct_rank / m4_rank_key。
滚动分位严格取**自身此前** 90 日窗口（不含当前点），不用未来。

性能：`change` 每标的只算一次（O(n)）；**分位按需惰性计算并缓存**
（只为真正成为候选的 bar 计算，避免对全部信号 bar 做 O(n²) 预计算）。
"""
from __future__ import annotations

import sys

import numpy as np

from . import paths as P
from .engine import median_qv_30d

if str(P.STRATEGY_ROOT) not in sys.path:
    sys.path.insert(0, str(P.STRATEGY_ROOT))
from WyckoffMaster import factors as WF   # noqa: E402

DAY_MS = 86_400_000
W90 = 90 * DAY_MS


def make_m4_rank_provider(seg_data, tf):
    """返回 provider(cands, T, bt) -> 排序后的 cands（越小越优先）。"""
    cache = {}          # symbol -> {"change": list|None, "times": list|None, "pct": {i: pct}}

    def _entry(sd):
        ent = cache.get(sd.symbol)
        if ent is None:
            if len(sd.oi_t) == 0:
                ent = {"change": None, "times": None, "pct": {}}
            else:
                times = list(sd.sig_close_time)
                change = WF.oi_change_20(list(sd.oi_t), list(sd.oi_v), times, 20)
                ent = {"change": change, "times": times, "pct": {}}
            cache[sd.symbol] = ent
        return ent

    def _pct(sd, i):
        ent = _entry(sd)
        if ent["change"] is None:
            return None
        pc = ent["pct"]
        if i not in pc:
            pc[i] = WF.rolling_pct_rank(ent["change"], ent["times"], i,
                                        window_ms=W90, min_samples=30)
        return pc[i]

    def provider(cands, T, bt):
        rows = []
        for (s, i) in cands:
            sd = bt._syms[s]
            p = _pct(sd, i)
            qv = median_qv_30d(sd.daily_qv, T)
            rows.append((WF.m4_rank_key(p, qv, "binance", s), (s, i)))
        rows.sort(key=lambda r: r[0])
        return [r[1] for r in rows]

    return provider
