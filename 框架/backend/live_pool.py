#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""信号与持仓接通（2026-10-13）· **在线候选入口 = 当前币池口径**（只读；需 numpy）。

## 为什么要单独成模块（**不是**另造一套策略/准入体系）

* `strategies/wx_daily_375.py`（sha `45e46301…` 家族）与 `01_规则37.py` 都是**冻结**文件，
  其 sha 就是已注册 run 的身份 ⇒ **不得修改**。
* 用户 2026-10-13 明确：**在线候选入口必须覆盖当前币池（549 加密币；币安 526）**，
  并**去掉「上市 ≥90 日」与「此前 30 完整 UTC 日成交额中位数 ≥500 万 U」两条准入硬门槛**，
  **保留短历史与极端价格**；**指数仅保留存储、不纳入候选**。
* ⇒ 本模块只做两件事：
    1. **口径解析**：币安加密币集合直接取唯一事实源 `logs/_pool_scope.py`
       （`历史下载/标的池/scope_crypto.json`，`crypto_only_v1`，`counts={binance:526, okx:23,
       total:549}`，`excluded_indices=[BTCDOMUSDT, ALLUSDT]`）；
    2. **37 规则的参数化放宽副本**：`qualify_symbol_live` 与冻结 `qualify_symbol_37`
       **逐字同构**，只把 `min_listed_days` / `min_median` / `prior_full_utc_days` 变成参数。
       在线取值 `min_listed_days=0, min_median=None, prior_full_utc_days=0`。

## 2026-10-13 第 2 轮订正（人类「补充复核」指令）

人类指出：把「**连续 240 根有效 1h**」当**准入条件**就是**已撤回的人为 240h 门槛**，
**不得把它改名成「策略预热」后恢复**。本模块据此改为：

  · **准入下限 = 指标各自所需的真实最小历史的最大值**（`INDICATOR_MIN_HISTORY`，从冻结
    参数生效视图逐项推导）⇒ 当前取值 **26 根**连续有效 1h（**不是 240**）；
  · **逐时点（逐 UTC 日）列出缺项**：每个日界列出该时点**尚不可算的指标名**，而不是整币一句
    「不足 240 根」；
  · **短历史币保留可用时段**：某日不达标只影响该日，**不整币拒绝**；
  · `240` 只以 **`STRATEGY_SELF_GATE`** 出现，明确标注为**冻结策略自身参数**
    （`wx_donchian_wyckoff.run()` 内 `len(seg) <= warmup_contiguous_bars` 跳过，sha 绑定不可改），
    **不是准入判据**，也不参与 `rule_identity()`。

## 逐指标真实最小历史（推导依据 = 冻结源码 + 冻结参数生效视图，逐条可核）

| 指标 | 冻结参数 | 需要的「更早的 bar」 | 最小 bar 数 |
|---|---|---|---|
| 唐奇安入场通道 `_donchian_flags` | `don_entry_bars=20` | 20 | 21 |
| 成交量基线 `_donchian_flags` | `vol_n=20` | 20 | 21 |
| ATR（`ewm_atr` `min_periods=20`） | `atr_n=20` | 19 | 20 |
| 检测用 ATR 锚 = 上一根完整 bar | `atr_n=20` | 20 | 21 |
| 唐奇安退出通道 `_exits` | `don_exit_bars=10` | 10 | 11 |
| Spring 箱体 `_spring_detect` | `don_entry_bars=20` | 20 | 21 |
| Spring 确认窗 | `spring_confirm_bars=3` | 20 + 3 | 24 |
| SC 箱体 `_sc_detect` | `don_entry_bars=20` | 20 | 21 |
| SC 前 10 根腿 `leg` | `sc_leg_bars=10` | 10 | 11 |
| SC 含当前 bar 的 20 均价 | `vol_n=20` | 19 | 20 |
| SC 确认窗 | `sc_confirm_bars=5` | 20 + 5 | 26 |

⇒ **准入下限 = max(以上) = 26 根**（连续、有效、原生 1h）。

## 如实说明（不美化）

* 本引擎读的是**币安 USD-M** 1h/15m K 线；池内 **23 个欧易仅补币**（`okx_in_binance=[]`）
  在币安无对应合约 ⇒ **不进**币安引擎候选，**不是**被静默丢掉，是市场不存在。
* 冻结研究件（375 名单、37 隔离副本、历史回测输入）**保持原样**，本模块不改写、不覆盖。
* 逐币「缺什么字段 / 哪个时点缺哪个指标」由 `missing_fields()` / `diagnostics()` /
  `run_cycle` 的 `pool_missing` 显式列明。
* 「连续有效」沿用冻结 37 口径（OHLC/量非空且为正、整点对齐、逐根 +1h）；
  冻结策略另有更严的 `bar_reject_reason`（额外校验 `close_time == open+1h−1ms` 与 OHLC 越界），
  **未**在本准入层重复实现（保持与冻结 37 逐字同构），故准入下限是**下界**而非上界。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np

BACKEND = Path("/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/后端实现")
for _p in (str(BACKEND), str(BACKEND / "logs")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
sys.dont_write_bytecode = True

try:
    import _pool_scope as PS                                  # noqa: E402
except Exception:                                             # noqa: BLE001
    class _FallbackPoolScope:
        SCOPE_JSON = Path(__file__).resolve().parents[2] / "数据" / "contracts" / "旧标的池" / "scope_crypto.json"
        @staticmethod
        def binance_scope_symbols():
            if _FallbackPoolScope.SCOPE_JSON.exists():
                try:
                    data = json.loads(_FallbackPoolScope.SCOPE_JSON.read_text(encoding="utf-8"))
                    return data.get("symbols") or data.get("binance") or []
                except Exception:
                    pass
            return []
        @staticmethod
        def scope_meta():
            return {"pool_source": "scope_crypto_fallback", "n": len(_FallbackPoolScope.binance_scope_symbols())}
    PS = _FallbackPoolScope


def _d():
    """惰性导入冻结研究件 `strategies.wx_daily_375`。

    该模块在**模块级** `import numpy, pyarrow`；而门禁默认解释器可能**无 pyarrow**
    ⇒ 若在模块级导入，会把本模块（以及依赖它的准入单测）一起拖成 skip。
    本模块只用到它的 3 个只读工具（`load_pool` / `_read_csv` / `_ms`），故改为**用到时才导入**。
    """
    from strategies import wx_daily_375 as D              # noqa: N814
    return D

DAY = 24 * 3_600_000
H = 3_600_000
EXCHANGE = "binance"
MARKET = "um"

# 冻结参数生效视图（只读引用；唯一参数来源，禁手抄数值）
FROZEN_VIEW = Path(
    "/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/"
    "最新唐奇安威科夫接通与收尾_2026-10-11/5000U收尾与回测条件终验6/"
    "10_研究参数生效视图_v2_2_user5000_shared.json")
_VIEW_PARAM_KEYS = ("don_entry_bars", "don_exit_bars", "atr_n", "vol_n",
                    "warmup_contiguous_bars", "spring_confirm_bars",
                    "sc_leg_bars", "sc_confirm_bars")
_VIEW_FALLBACK = {"don_entry_bars": 20, "don_exit_bars": 10, "atr_n": 20, "vol_n": 20,
                  "warmup_contiguous_bars": 240, "spring_confirm_bars": 3,
                  "sc_leg_bars": 10, "sc_confirm_bars": 5}


def frozen_signal_params() -> dict:
    """从冻结生效视图读 `signals.*`（只读）。视图不可读 ⇒ 用已核对的回退值并**显式标注**。"""
    try:
        doc = json.loads(FROZEN_VIEW.read_text(encoding="utf-8"))
        sig = doc["signals"]
        out = {k: sig[k] for k in _VIEW_PARAM_KEYS}
        out["_source"] = str(FROZEN_VIEW)
        out["_sha256"] = _sha256_file(FROZEN_VIEW)
        out["_fallback"] = False
        return out
    except Exception:                                        # noqa: BLE001
        out = dict(_VIEW_FALLBACK)
        out.update({"_source": str(FROZEN_VIEW), "_sha256": None, "_fallback": True})
        return out


def indicator_min_history(p: dict | None = None) -> dict:
    """**逐指标**真实最小历史（连续有效 1h bar 数）。全部由冻结参数推导，不手抄。"""
    p = p or frozen_signal_params()
    de, dx = int(p["don_entry_bars"]), int(p["don_exit_bars"])
    an, vn = int(p["atr_n"]), int(p["vol_n"])
    sl = int(p["sc_leg_bars"])
    sp_cb, sc_cb = int(p["spring_confirm_bars"]), int(p["sc_confirm_bars"])
    detect_start = max(de, vn)          # _donchian_flags / _scan_structure 的起点
    return {
        "donchian_entry_channel":   {"param": "don_entry_bars", "prior_bars": de,
                                     "min_bars": de + 1},
        "volume_baseline":          {"param": "vol_n", "prior_bars": vn, "min_bars": vn + 1},
        "atr_ewm":                  {"param": "atr_n", "prior_bars": an - 1, "min_bars": an},
        "atr_previous_bar_anchor":  {"param": "atr_n", "prior_bars": an, "min_bars": an + 1},
        "donchian_exit_channel":    {"param": "don_exit_bars", "prior_bars": dx,
                                     "min_bars": dx + 1},
        "spring_box":               {"param": "don_entry_bars", "prior_bars": de,
                                     "min_bars": de + 1},
        "spring_confirm_window":    {"param": "spring_confirm_bars", "prior_bars": detect_start,
                                     "extra_bars": sp_cb, "min_bars": detect_start + 1 + sp_cb},
        "sc_box":                   {"param": "don_entry_bars", "prior_bars": de,
                                     "min_bars": de + 1},
        "sc_leg":                   {"param": "sc_leg_bars", "prior_bars": sl, "min_bars": sl + 1},
        "sc_close_mean_incl_current": {"param": "vol_n", "prior_bars": vn - 1,
                                       "min_bars": vn},
        "sc_confirm_window":        {"param": "sc_confirm_bars", "prior_bars": detect_start,
                                     "extra_bars": sc_cb, "min_bars": detect_start + 1 + sc_cb},
    }


INDICATOR_MIN_HISTORY = indicator_min_history()
# 准入下限 = 所有指标/确认窗各自所需最小 bar 数的**最大值**（当前 = 26）
ADMISSION_MIN_BARS = max(v["min_bars"] for v in INDICATOR_MIN_HISTORY.values())

# ── 冻结策略**自身**参数（sha 绑定、不可改）——**不是准入判据**，也不进 rule_identity ──
STRATEGY_SELF_GATE = {
    "note": "冻结策略 wx_donchian_wyckoff.run() 内部参数；`len(seg) <= warmup_contiguous_bars` "
            "的段不产生任何事件。属冻结策略自带行为（sha 即已注册 run 身份），**本层准入不使"
            "用它**，仅用于解释「更短段为何不产生事件」与决定喂给策略的数据装配长度。",
    "param": "warmup_contiguous_bars",
    "value": int(frozen_signal_params()["warmup_contiguous_bars"]),
    "used_for_admission": False,
}

# 在线候选口径身份（改任何取值即变 ⇒ 供报告注明）
POOL_RULE_LIVE = {
    "schema": "online_candidate_pool/v2",
    "rule_id": "live_pool.scope_crypto_v1_noagefloor_per_indicator_min_history",
    "human_authorization": "用户 2026-10-13 指令：549 加密币候选、去掉 90 天/500 万 U、"
                           "保留短历史与极端价格；指数仅保留存储不纳入候选",
    "human_authorization_round2": "用户 2026-10-13 补充复核：不得把「连续 240 根有效 1h」"
                                  "当准入条件，也不得改名成「策略预热」恢复；改按指标各自所需"
                                  "真实最小历史逐时点判定，短历史币保留可用时段",
    "scope_source": "logs/_pool_scope.py → 历史下载/标的池/scope_crypto.json",
    "scope_version": "crypto_only_v1",
    "counts_expected": {"binance": 526, "okx": 23, "total": 549},
    "excluded_indices": ["BTCDOMUSDT", "ALLUSDT"],
    "engine_market": {"exchange": "binance", "market": "um"},
    "min_listed_days": 0,
    "minimum_previous30_full_UTC_day_median_quote_USDT": None,
    "prior_full_utc_days": 0,
    "quote_hard_threshold_disabled": True,
    "listed_age_threshold_disabled": True,
    # ── 本轮核心：准入下限来自指标真实最小历史，**不是** 240 ──
    "admission_min_bars": ADMISSION_MIN_BARS,
    "admission_min_bars_basis": "max(INDICATOR_MIN_HISTORY[*].min_bars)",
    "indicator_min_history": {k: v["min_bars"] for k, v in INDICATOR_MIN_HISTORY.items()},
    "uniform_240h_gate_removed": True,
    "per_timepoint_missing_listing": True,
    "short_history_coins_keep_usable_window": True,
    "unchanged": {
        "volume_price_confirmation": "保留（威科夫量价确认，冻结策略内）",
        "missing_input_rule": "缺数据即缺数据；不补 0 冒完整；连续有效 bar 不足该指标最小值即列缺",
        "capital_risk_fee": "与已批准 5000U 口径一致（见 paper_engine.approved_params）",
    },
}


def rule_identity() -> str:
    payload = json.dumps(POOL_RULE_LIVE, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"))
    return "pool:scope_crypto_v1_per_indicator_min_history:" + hashlib.sha256(
        payload.encode("utf-8")).hexdigest()[:32]


# ── 口径解析（唯一事实源：`logs/_pool_scope.py`）──────────────────────────────
def scope_symbols():
    """币安加密币候选（526）。范围文件缺失 ⇒ 回退冻结 375 名单并**显式标注**。

    返回 `(symbols, meta)`；`meta["pool_source"]` ∈
    `{"scope_crypto_v1", "fallback_frozen_375"}`，报告必须照抄，不得含糊。
    """
    syms = PS.binance_scope_symbols()
    meta = PS.scope_meta()
    if syms:
        return sorted(syms), {"pool_source": "scope_crypto_v1", "n": len(syms),
                              "scope_meta": meta,
                              "scope_sha256": _sha256_file(PS.SCOPE_JSON),
                              "rule_identity": rule_identity()}
    frozen = _d().load_pool()
    return sorted(frozen["symbols"]), {
        "pool_source": "fallback_frozen_375", "n": len(frozen["symbols"]),
        "scope_meta": meta, "frozen_pool_path": frozen["path"],
        "frozen_pool_sha256": frozen["sha256"], "rule_identity": rule_identity(),
        "warning": "范围文件不可读 ⇒ 已回退冻结 375 名单（**未**覆盖当前币池，须修范围文件）",
    }


def _sha256_file(p) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 16), b""):
            h.update(c)
    return h.hexdigest()


def listing_map():
    """每币**官方上市时刻**（ms）。

    来源优先级：`05_526币身份可证明点与未证明时段.csv`（526/526 有值，与
    `scope_crypto.json` 的 `binance_items[].onboard_time` **逐字节一致**，已实测 526/526 相同）
    → 缺失时回退范围文件 `onboard_time`。两者皆缺 ⇒ `None`（并逐币列明）。
    """
    out = {}
    try:
        D = _d()
        for r in D._read_csv(D.IDENT05):
            raw = r.get("当前官方上市时间")
            if raw:
                out[r["币"]] = D._ms(raw)
    except OSError:
        pass
    try:
        doc = json.loads(PS.SCOPE_JSON.read_text(encoding="utf-8"))
        for it in doc.get("binance_items", []):
            s = it.get("symbol")
            if s and s not in out and it.get("onboard_time"):
                out[s] = int(it["onboard_time"])
    except Exception:                                        # noqa: BLE001
        pass
    return out


# ── 37 规则的**参数化放宽副本**（与冻结版逐字同构）─────────────────────────────
def qualify_symbol_live(ts, o, hi, lo, cl, vol, q, listed_ms, panel_start_ms, panel_end_ms,
                        min_listed_days=0, min_median=None, prior_full_utc_days=0,
                        min_history=ADMISSION_MIN_BARS):
    """返回 `[(day_ms, prior_median, listed_days, min_history)]`（升序）。

    与 `01_规则37.qualify_symbol_37` **逐字同构**，唯一差异：四个门槛参数化。
      · `min_listed_days=0`   ⇒ **取消「上市 ≥90 日」硬门槛**（保留短历史）；
      · `min_median=None`     ⇒ **取消「30 日成交额中位数 ≥500 万 U」硬门槛**；
      · `prior_full_utc_days=0` ⇒ 不再要求「此前 30 个完整 UTC 日每日 24 有效小时」；
      · `min_history=26`      ⇒ **准入下限 = 指标真实最小历史的最大值**（2026-10-13 第 2 轮订正，
        **取代**原先写死的 240）；短历史币只要在该日界前达到该值即准入该日，**不整币拒绝**。
    仍然强制：`ts[j] == dayms - H` 且 `runs[j] >= min_history` 且 `full_ok[j]`。
    **不含段首禁入**（禁入另作 `ban_mask` 分离，与 37 一致）。
    """
    full_ok = (np.isfinite(o) & np.isfinite(hi) & np.isfinite(lo) & np.isfinite(cl)
               & np.isfinite(vol) & np.isfinite(q)
               & (o > 0) & (hi > 0) & (lo > 0) & (cl > 0) & (vol >= 0) & (q >= 0))
    if ts.size == 0 or int(ts[0] // DAY) > int((panel_end_ms - 1) // DAY):
        return []
    dmin = int(ts[0] // DAY)
    dmax = int((panel_end_ms - 1) // DAY)
    n = dmax - dmin + 1
    di = (ts // DAY - dmin)
    valid = (ts < panel_end_ms) & full_ok & (ts % H == 0)
    counts = np.bincount(di[valid], minlength=n)[:n]
    tot = np.bincount(di[valid], weights=np.asarray(q)[valid], minlength=n)[:n]
    runs = _contiguous_runs(ts, full_ok)
    out = []
    d_start = max(int(prior_full_utc_days), int(panel_start_ms // DAY) - dmin)
    for d in range(d_start, n):
        dayms = (d + dmin) * DAY
        if dayms < listed_ms + int(min_listed_days) * DAY or dayms >= panel_end_ms:
            continue
        if prior_full_utc_days and not np.all(counts[d - int(prior_full_utc_days):d] == 24):
            continue
        med = float(np.median(tot[d - 30:d])) if d >= 30 else None
        if min_median is not None and (med is None or med < min_median):
            continue
        j = int(np.searchsorted(ts, dayms) - 1)
        if j < 0 or ts[j] != dayms - H or runs[j] < int(min_history) or not full_ok[j]:
            continue
        out.append((int(dayms), med, (dayms - listed_ms) / DAY, int(min_history)))
    return out


def _contiguous_runs(ts, full_ok):
    """连续有效 1h 游程长度（含当前位置）。与冻结 37 的 `resets` 口径逐字一致。"""
    resets = np.zeros(len(ts), dtype=int)
    if len(ts):
        resets[0] = 1
        resets[1:] = (~full_ok[1:]) | (~full_ok[:-1]) | (np.diff(ts) != H)
    latest = np.maximum.accumulate(np.where(resets, np.arange(len(ts)), 0))
    return np.arange(len(ts)) - latest + 1


def per_day_missing(ts, o, hi, lo, cl, vol, q, days, listed_ms,
                    min_history_map=None, panel_end_ms=None):
    """**逐时点（逐 UTC 日）列出缺项**：每个日界 → 该时点尚不可算的指标名。

    返回 `[{day_ms, day_utc, contiguous_valid_hours_before_day, admitted,
             missing_indicators: [...], per_indicator_bars: {...}}, ...]`（升序）。
    `admitted` = `missing_indicators` 为空（即全部指标可算）。
    日界口径与 `qualify_symbol_live` 一致：取 `dayms - H` 那根 bar 的游程。
    """
    min_history_map = min_history_map or INDICATOR_MIN_HISTORY
    panel_end_ms = int(panel_end_ms) if panel_end_ms is not None else (
        int(ts[-1]) + H if ts.size else 0)
    if ts.size == 0:
        return [{"day_ms": int(d), "day_utc": _iso_day(int(d)),
                 "contiguous_valid_hours_before_day": 0, "admitted": False,
                 "missing_indicators": sorted(min_history_map),
                 "per_indicator_bars": {k: v["min_bars"] for k, v in min_history_map.items()},
                 "reason": "no_1h_bars"} for d in days]
    full_ok = (np.isfinite(o) & np.isfinite(hi) & np.isfinite(lo) & np.isfinite(cl)
               & np.isfinite(vol) & np.isfinite(q)
               & (o > 0) & (hi > 0) & (lo > 0) & (cl > 0) & (vol >= 0) & (q >= 0))
    runs = _contiguous_runs(ts, full_ok)
    out = []
    for d in days:
        d = int(d)
        j = int(np.searchsorted(ts, d) - 1)
        if j < 0:
            run = 0
            has_prev = False
        else:
            has_prev = bool(ts[j] == d - H)
            run = int(runs[j]) if has_prev else 0
        missing = sorted(k for k, v in min_history_map.items() if run < int(v["min_bars"]))
        row = {"day_ms": d, "day_utc": _iso_day(d),
               "contiguous_valid_hours_before_day": run,
               "admitted": (not missing) and has_prev,
               "missing_indicators": missing,
               "per_indicator_bars": {k: v["min_bars"] for k, v in min_history_map.items()}}
        if not has_prev:
            row["reason"] = "no_bar_at_day_minus_1h"
        if listed_ms is not None and d < int(listed_ms):
            row["reason"] = "before_official_listing"
        out.append(row)
    return out


def _iso_day(ms) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(int(ms) / 1000, _dt.timezone.utc).strftime("%Y-%m-%d")


def missing_fields(con, sym, listed_ms, T_ms, adm_start_ms, days=None):
    """逐币「缺什么 / 哪个时点缺哪个指标」——**如实列明**，不猜、不补。

    返回 `{"missing": [...], "facts": {...}, "per_day": [...]}`；
    `missing` 为空 ⇒ 该币在候选内。
    """
    missing = []
    facts = {"symbol": sym, "listing_ms": listed_ms}
    r = con.execute("SELECT MAX(open_time), COUNT(*) FROM candle_heads "
                    "WHERE exchange=? AND market=? AND symbol=? AND timeframe='1h'",
                    (EXCHANGE, MARKET, sym)).fetchone()
    facts["last_1h_open_time"] = int(r[0]) if r and r[0] is not None else None
    facts["n_1h_bars_total"] = int(r[1]) if r else 0
    if facts["n_1h_bars_total"] == 0:
        missing.append("no_1h_candles_in_market_db")
    if listed_ms is None:
        missing.append("no_official_listing_time")
    # 读窗：覆盖「待评估日」+ 足够裕量（> 准入下限，避免游程被窗口截断而低估）
    if days:
        start = min(int(d) for d in days) - H - max(240, ADMISSION_MIN_BARS + 2) * H
    else:
        start = int(T_ms) - (ADMISSION_MIN_BARS + 240) * H
    rows = con.execute(
        "SELECT open_time,open,high,low,close,volume,quote_volume FROM candles "
        "WHERE exchange=? AND market=? AND symbol=? AND timeframe='1h' "
        "AND open_time>=? AND open_time<? ORDER BY open_time",
        (EXCHANGE, MARKET, sym, int(start), int(T_ms))).fetchall()
    ts = np.array([int(x[0]) for x in rows], dtype=np.int64)
    facts["n_1h_bars_in_window"] = int(ts.size)
    if ts.size == 0:
        facts["max_consecutive_valid_hours_to_now"] = 0
        facts["admission_min_bars"] = ADMISSION_MIN_BARS
        facts["per_day"] = []
        return {"missing": missing, "facts": facts, "per_day": []}
    o = np.array([float(x[1]) for x in rows]); hi = np.array([float(x[2]) for x in rows])
    lo = np.array([float(x[3]) for x in rows]); cl = np.array([float(x[4]) for x in rows])
    vol = np.array([float(x[5]) for x in rows]); q = np.array([float(x[6]) for x in rows])
    full_ok = (np.isfinite(o) & np.isfinite(hi) & np.isfinite(lo) & np.isfinite(cl)
               & np.isfinite(vol) & np.isfinite(q)
               & (o > 0) & (hi > 0) & (lo > 0) & (cl > 0) & (vol >= 0) & (q >= 0))
    runs = _contiguous_runs(ts, full_ok)
    facts["max_consecutive_valid_hours_to_now"] = int(runs[-1])
    facts["admission_min_bars"] = ADMISSION_MIN_BARS
    facts["contiguous_valid_hours_at_T"] = int(runs[-1])
    if int(runs[-1]) < ADMISSION_MIN_BARS:
        missing.append("contiguous_valid_1h_bars_below_per_indicator_min")
    pdays = per_day_missing(ts, o, hi, lo, cl, vol, q, days or [], listed_ms,
                            panel_end_ms=int(T_ms))
    facts["per_day_admitted_n"] = sum(1 for x in pdays if x["admitted"])
    facts["per_day_total_n"] = len(pdays)
    if pdays and facts["per_day_admitted_n"] == 0:
        missing.append("no_admitted_utc_day_in_window")
    return {"missing": missing, "facts": facts, "per_day": pdays}


def diagnostics(con, symbols, T_ms, days, listing):
    """全池逐币诊断（只读）。返回 `(per_symbol, summary)`。

    `days` = 需要逐时点列缺项的 UTC 日界列表（ms）。
    """
    per = {}
    summary = {}
    for sym in symbols:
        d = missing_fields(con, sym, listing.get(sym), T_ms, None, days)
        per[sym] = d
        key = "+".join(d["missing"]) if d["missing"] else "ok"
        summary[key] = summary.get(key, 0) + 1
    return per, dict(sorted(summary.items(), key=lambda kv: -kv[1]))
