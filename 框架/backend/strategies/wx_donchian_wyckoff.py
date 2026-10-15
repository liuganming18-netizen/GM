# -*- coding: utf-8 -*-
"""F_WX：唐奇安基线 + 威科夫（Spring / SC）确定性纯核心（v2 / v2.1）。

范围与纪律（来源：策略与回测规则_2026-10-11/01、02；项目闭环审查_2026-10-11/05、06、10）：

  · 只做 1h、只做多（long_only）。不接 OI、旧 A/B、结构止损 _S、做空、指数。
  · 纯函数：输入 = 已收盘 1h bar 序列 + 冻结配置；输出 = 事件。**不读库、不联网、不下单**。
  · 只消费**已收盘** bar；所有基线（唐奇安通道、量均量、结构箱体）一律**排除当前 bar**，
    只用严格更早的 bar。检测用 ATR 取**上一完整 bar**（detection_atr_anchor=previous_closed_bar）。
  · 240 根**连续有效**小时预热；原生 1h 网格**缺一根**即按身份重置事件与预热（A-09）。
  · 全部不等号**严格执行**：等号不算突破 / 不算确认。
  · 同 bar 检测**不得**确认；确认**只发一次**；因仓位 / 资金被拒**仍消费**该事件。
  · 每类（Spring / SC）只保留**首个活动事件**；同类新检测不覆盖锚点（另记 overlap）。
    事件被确认 / 失效 / 到期后的**下一根 bar** 才允许建新事件。
  · base 与结构重合时执行标签 **base 优先**，并保留**全部** source_masks（A-12）。

四组臂（只差入场条件，其余全部相同，保证可比）：

  3a  唐奇安基线：close > 此前 20 根最高价（严格）
  3b  3a AND quote成交额 > 此前 20 根均量×1.5 AND close > open
  3c  3a OR Spring 确认
  3d  3a OR SC 确认

止损 / 执行时钟 / 成本 / 资金费**不在本模块**（属执行层 G03）；本模块只给出
信号 bar 的 ATR，供执行层按 `S = 实际入场价 − 3×信号bar ATR` 固定止损（A-12）。
"""
from __future__ import annotations

import hashlib
import math

from . import common
from .common import Condition, Event

TIMEFRAME = "1h"
DIRECTION = "long"
HOUR_MS = 3600_000
ARM_IDS = ("3a", "3b", "3c", "3d")

# 生效参数视图（唯一合成参数视图）。只读引用，不复制数值、不改字节。
# 2026-10-11 用户改本金 10000→5000：指向 v2.2 用户 5000 共享模型视图；v2.1 原字节保留。
EFFECTIVE_VIEW_PATH = ("/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/"
                       "最新唐奇安威科夫接通与收尾_2026-10-11/5000U收尾与回测条件终验6/"
                       "10_研究参数生效视图_v2_2_user5000_shared.json")
# 父规格绑定（仅记录，供身份核对）。
SPEC_V2_PATH = ("/Users/boos/.codex/worktrees/c9d7/加密货币趋势判断系统/"
                "策略与回测规则_2026-10-11/01_策略优化与回测规则_v2.md")
PARAM_V2_PATH = ("/Users/boos/.codex/worktrees/c9d7/加密货币趋势判断系统/"
                 "策略与回测规则_2026-10-11/02_研究参数_v2.json")

REQUIRED_CONFIG = (
    "don_entry_bars", "don_exit_bars", "atr_n", "vol_n", "warmup_contiguous_bars",
    "atr_method", "ice_vol_mult", "tr_atr_mult", "spring_vol_mult", "spring_close_pos",
    "spring_depth_atr", "spring_confirm_bars", "sc_vol_mult", "sc_close_pos",
    "sc_newlow_pct", "sc_leg_bars", "sc_recover", "sc_confirm_bars",
)

VALIDATION = {
    "don_entry_bars": {"type": "int", "min": 1},
    "don_exit_bars": {"type": "int", "min": 1},
    "atr_n": {"type": "int", "min": 2},
    "vol_n": {"type": "int", "min": 2},
    "warmup_contiguous_bars": {"type": "int", "min": 0},
    "ice_vol_mult": {"type": "float", "min": 0.0, "exclusive_min": True},
    "tr_atr_mult": {"type": "float", "min": 0.0, "exclusive_min": True},
    "spring_vol_mult": {"type": "float", "min": 0.0, "exclusive_min": True},
    "spring_close_pos": {"type": "float", "min": 0.0, "max": 1.0, "exclusive_max": True},
    "spring_depth_atr": {"type": "float", "min": 0.0, "exclusive_min": True},
    "spring_confirm_bars": {"type": "int", "min": 1},
    "sc_vol_mult": {"type": "float", "min": 0.0, "exclusive_min": True},
    "sc_close_pos": {"type": "float", "min": 0.0, "max": 1.0, "exclusive_max": True},
    "sc_newlow_pct": {"type": "float", "min": 0.0, "max": 1.0, "exclusive_max": True},
    "sc_leg_bars": {"type": "int", "min": 1},
    "sc_recover": {"type": "float", "min": 0.0, "exclusive_min": True},
    "sc_confirm_bars": {"type": "int", "min": 1},
}


def _rel_atr_method(cfg):
    if cfg.get("atr_method") != "EWM_alpha_1_over_20_adjust_false_min_periods20":
        return [f"atr_method 必须为 EWM_alpha_1_over_20_adjust_false_min_periods20"
                f"（得到 {cfg.get('atr_method')!r}）"]
    return []


def _rel_window(cfg):
    """预热必须不短于任何单根基线窗口（否则基线在预热内不可用）。"""
    need = max(int(cfg["atr_n"]), int(cfg["don_entry_bars"]),
               int(cfg["vol_n"]), int(cfg["sc_leg_bars"]))
    if int(cfg["warmup_contiguous_bars"]) < need:
        return [f"warmup_contiguous_bars({cfg['warmup_contiguous_bars']}) "
                f"必须 ≥ max(atr_n,don_entry_bars,vol_n,sc_leg_bars)={need}"]
    return []


RELATIONS = [_rel_atr_method, _rel_window]


# ────────────────────────────── 纯计算工具 ──────────────────────────────

def _finite(v):
    return v is not None and isinstance(v, (int, float)) and math.isfinite(float(v))


def bar_reject_reason(b):
    """单根 bar 的**合法性拒绝原因**（None = 合法可参与计算）。

    2026-10-11 独立反例整改（`non_native_hour_grid_produces_signal` /
    `illegal_close_above_high_and_negative_quote_produces_signal`）：
      · **严格 1h UTC 原生网格**：`open_time` 必须整点对齐（整体偏移 15m 的序列
        旧实现仍算「连续」并产事件）；`close_time` 必须 == open_time + 1h − 1ms
        （与库内实测口径一致：真实 1h bar close−open = 3599999）。
      · **非法 OHLC 拒绝**：high<low、close/high/low 越界、open 越界。
      · **负成交量拒绝**：quote_volume/volume < 0 非法；**真实 0 合法**（不填 0、不删行）。
      · 缺字段 / 非有限一律拒绝（不补造值）。
    """
    if not all(_finite(x) for x in (b.open, b.high, b.low, b.close, b.quote_volume)):
        return "missing_or_nonfinite_ohlc_quote"
    ot = int(b.open_time)
    if ot % HOUR_MS != 0:
        return "non_native_hour_grid_open_time"
    if b.close_time is None or not _finite(b.close_time) or \
            int(b.close_time) != ot + HOUR_MS - 1:
        return "non_native_bar_close_time"
    o, h, l, c = float(b.open), float(b.high), float(b.low), float(b.close)
    if h < l:
        return "high_below_low"
    if c > h or c < l or o > h or o < l:
        return "ohlc_out_of_range"
    if float(b.quote_volume) < 0:
        return "negative_quote_volume"
    if b.volume is not None and _finite(b.volume) and float(b.volume) < 0:
        return "negative_volume"
    return None


def _bar_valid(b):
    """OHLC/成交额/网格 全部合法才可参与计算；缺字段/非有限/非法一律视为无效 bar。"""
    return bar_reject_reason(b) is None


def _segments(bars):
    """按**原生 1h UTC 网格连续性**切段：缺一根 / 非法 / 身份不连续 → 新段。

    新段意味着事件与 240 根预热一起重置（A-09），绝不补零、不缩窗口、不跨代拼接。
    返回 ([(start_idx, end_idx_exclusive), ...], reject_counts)。
    """
    segs = []
    rejects = {}
    i = 0
    n = len(bars)
    while i < n:
        reason = bar_reject_reason(bars[i])
        if reason is not None:
            rejects[reason] = rejects.get(reason, 0) + 1   # 每根被拒 bar 只计一次
            i += 1
            continue
        j = i + 1
        while j < n:
            if bar_reject_reason(bars[j]) is not None:
                break                                      # 交给外层计数，避免重复
            if int(bars[j].open_time) - int(bars[j - 1].open_time) != HOUR_MS:
                break
            j += 1
        segs.append((i, j))
        i = j
    return segs, rejects


def ewm_atr(trs, alpha, min_periods):
    """pandas `Series.ewm(alpha=alpha, adjust=False, min_periods=n).mean()` 的等价实现。

    adjust=False ⇒ 递归 y_i = (1−alpha)·y_{i−1} + alpha·x_i，且 y_0 = x_0；
    min_periods ⇒ 前 n−1 个位置返回 None（不足则不算出 ATR，不猜）。
    """
    out = [None] * len(trs)
    if not trs:
        return out
    y = float(trs[0])
    for i, x in enumerate(trs):
        if i > 0:
            y = (1.0 - alpha) * y + alpha * float(x)
        if i + 1 >= min_periods:
            out[i] = y
    return out


def true_ranges(seg):
    """段内 TR 序列：TR[0] = high−low；其后 = max(H−L, |H−prevC|, |L−prevC|)。"""
    trs = []
    for k, b in enumerate(seg):
        hl = float(b.high) - float(b.low)
        if k == 0:
            trs.append(hl)
        else:
            pc = float(seg[k - 1].close)
            trs.append(max(hl, abs(float(b.high) - pc), abs(float(b.low) - pc)))
    return trs


def _close_pos(b):
    """收盘位置 (close−low)/(high−low)。零振幅/非有限 → None（拒绝检测，不猜测）。"""
    hi, lo, c = float(b.high), float(b.low), float(b.close)
    rng = hi - lo
    if not math.isfinite(rng) or rng <= 0:
        return None
    pos = (c - lo) / rng
    return pos if math.isfinite(pos) else None


def _uid(identity, *parts):
    """事件/信号身份哈希。**身份显式前置**：`identity=(exchange, symbol)`。

    第15 修复（68 缺陷 D2）：冻结版 `_uid(*parts)` 只有 `(arm, open_time, masks)` ⇒
    不同币、同一时刻、同类型事件生成**同一个** `event_uid`，撞 `research_signal` 主键
    `(run_id, event_uid)` 被 `INSERT OR REPLACE` 覆盖（**跨币丢行**）。现在把
    `(exchange, symbol)` 显式前置进 canonical 串。

    **不使用模块全局变量**：第12 隔离副本用模块级 `_IDENTITY`（`run()` 内赋值），
    交错/嵌套调用会共享错误状态（串币风险）；本实现改为**逐调用显式传参**。
    `identity=None` 时与冻结版**逐字节一致**（legacy 兼容路径，不改变旧运行身份）。
    """
    prefix = tuple(str(x) for x in (identity or ()))
    canon = "|".join(str(p) for p in (*prefix, *parts))
    return "wx:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:20]


# ────────────────────────────── 事件状态机 ──────────────────────────────

class _StructureState:
    """Spring / SC 共用的「首个活动事件」状态机。

    idle  → 检测命中即建锚点（active），锚点在 confirm_bars 根内逐 bar 判定：
              · 先判失效（invalidation_before_confirmation）→ invalidated
              · 再判确认（严格不等号）→ confirmed 并**发一次**
              · 窗口耗尽仍无二者 → expired
            任一解析后，`next_allowed` = 解析 bar 下标 + 1（下一根 bar 才可建新事件）。
    active 期间的同类新检测**不覆盖锚点**，只计入 `overlaps`（另记重叠）。
    """

    __slots__ = ("kind", "confirm_bars", "anchor", "next_allowed", "overlaps", "resolved")

    def __init__(self, kind, confirm_bars):
        self.kind = kind
        self.confirm_bars = int(confirm_bars)
        self.anchor = None
        self.next_allowed = 0
        self.overlaps = 0
        self.resolved = []      # [(detect_i, resolve_i, outcome)]


def _spring_detect(seg, i, cfg, atr_prev, box, identity=None):
    """Spring 检测（只用 i 之前 20 根箱体 + 当前 bar 自身）。命中返回冻结锚点 dict。

    `identity=(exchange, symbol)` **显式**参与结构事件 `event_uid`（第15 修复）。
    """
    b = seg[i]
    if atr_prev is None or not _finite(atr_prev) or atr_prev <= 0:
        return None
    box_high = max(float(x.high) for x in box)
    box_low = min(float(x.low) for x in box)
    if box_high - box_low >= float(cfg["tr_atr_mult"]) * atr_prev:
        return None                                    # 箱体过宽，不算 Spring 箱体
    l20 = box_low                                      # L20 = 此前 20 根最低
    if not (float(b.low) < l20):                       # 严格跌破
        return None
    if not (float(b.close) > l20):                     # 收回箱体内
        return None
    pos = _close_pos(b)
    if pos is None or not (pos > float(cfg["spring_close_pos"])):
        return None
    vbase = common.mean(x.quote_volume for x in box)
    if vbase is None or vbase <= 0:
        return None
    if not (float(b.quote_volume) > float(cfg["spring_vol_mult"]) * vbase):
        return None
    depth = l20 - float(b.low)
    if not (depth < float(cfg["spring_depth_atr"]) * atr_prev):
        return None
    return {
        "kind": "spring", "detect_i": i, "detect_open_time": int(b.open_time),
        "box_high": box_high, "box_low": box_low, "support": l20,
        "midline": (box_high + box_low) / 2.0, "detection_low": float(b.low),
        "detection_atr": float(atr_prev), "vol_baseline": vbase,
        "quote_volume": float(b.quote_volume), "close_pos": pos,
        "event_uid": _uid(identity, "spring", b.open_time, round(l20, 12), round(depth, 12)),
    }


def _spring_invalidated(seg, j, anchor, cfg):
    b = seg[j]
    if float(b.low) < anchor["detection_low"]:
        return "low_below_detection_low"
    if float(b.close) <= anchor["support"]:
        return "close_at_or_below_frozen_support"
    return None


def _spring_confirmed(seg, j, anchor, cfg):
    return float(seg[j].close) > anchor["midline"]


def _sc_detect(seg, i, cfg, atr_prev, box, leg, identity=None):
    """SC 检测（close 对**含当前 bar**的 20 均价；量对**此前** 20 均量）。

    `identity=(exchange, symbol)` **显式**参与结构事件 `event_uid`（第15 修复）。
    """
    b = seg[i]
    if atr_prev is None or not _finite(atr_prev) or atr_prev <= 0:
        return None
    n = int(cfg["vol_n"])
    incl = seg[i - n + 1:i + 1]                        # 含当前 bar 的 n 根
    cmean = common.mean(float(x.close) for x in incl)
    if cmean is None or not (float(b.close) < cmean):
        return None
    vbase = common.mean(x.quote_volume for x in box)
    if vbase is None or vbase <= 0:
        return None
    if not (float(b.quote_volume) > float(cfg["sc_vol_mult"]) * vbase):
        return None
    pos = _close_pos(b)
    if pos is None or not (pos > float(cfg["sc_close_pos"])):
        return None
    l20 = min(float(x.low) for x in box)
    if not (float(b.low) < l20 * (1.0 - float(cfg["sc_newlow_pct"]))):
        return None
    prior10_high = max(float(x.high) for x in leg)
    detection_low = float(b.low)
    target = detection_low + float(cfg["sc_recover"]) * (prior10_high - detection_low)
    return {
        "kind": "sc", "detect_i": i, "detect_open_time": int(b.open_time),
        "detection_low": detection_low, "prior10_high": prior10_high,
        "target": target, "detection_atr": float(atr_prev),
        "vol_baseline": vbase, "quote_volume": float(b.quote_volume),
        "close_mean_incl": cmean, "close_pos": pos,
        "event_uid": _uid(identity, "sc", b.open_time, round(detection_low, 12),
                          round(target, 12)),
    }


def _sc_invalidated(seg, j, anchor, cfg):
    if float(seg[j].low) < anchor["detection_low"]:
        return "low_below_detection_low"
    return None


def _sc_confirmed(seg, j, anchor, cfg):
    return float(seg[j].close) > anchor["target"]


_STRUCTURES = {
    "spring": (_spring_detect, _spring_invalidated, _spring_confirmed),
    "sc": (_sc_detect, _sc_invalidated, _sc_confirmed),
}


def _scan_structure(seg, cfg, kind, atr_prev_by_i, warm_start, identity=None):
    """扫描单个段内的结构事件（Spring / SC）。返回 (confirms, anchors, state)。

    confirms: {bar_i: anchor}  在该 bar **收盘**确认的结构事件
    anchors:  {detect_i: anchor} 全部检测锚点（含最终失效/到期的）

    `identity=(exchange, symbol)` 显式下传到检测函数，参与结构事件身份（第15 修复）。
    """
    detect_fn, invalid_fn, confirm_fn = _STRUCTURES[kind]
    confirm_bars = int(cfg["spring_confirm_bars"] if kind == "spring" else cfg["sc_confirm_bars"])
    st = _StructureState(kind, confirm_bars)
    confirms, anchors = {}, {}
    n = len(seg)
    nb = int(cfg["don_entry_bars"])
    nl = int(cfg["sc_leg_bars"])
    i = max(warm_start, max(nb, int(cfg["vol_n"]), nl))
    while i < n:
        if st.anchor is not None:
            i += 1
            continue
        if i < st.next_allowed:
            i += 1
            continue
        box = seg[i - nb:i]
        leg = seg[i - nl:i]
        if len(box) < nb or len(leg) < nl:
            i += 1
            continue
        a = detect_fn(seg, i, cfg, atr_prev_by_i[i], box, leg, identity) if kind == "sc" \
            else detect_fn(seg, i, cfg, atr_prev_by_i[i], box, identity)
        if a is None:
            i += 1
            continue
        # 锚点建立；从下一根 bar 起判定（同 bar 不得确认）
        st.anchor = a
        anchors[i] = a
        outcome = None
        for k in range(1, confirm_bars + 1):
            j = i + k
            if j >= n:
                outcome = ("expired", j - 1, "segment_end")
                break
            bad = invalid_fn(seg, j, a, cfg)
            if bad:
                outcome = ("invalidated", j, bad)
                break
            if confirm_fn(seg, j, a, cfg):
                outcome = ("confirmed", j, None)
                break
        else:
            outcome = ("expired", i + confirm_bars, "confirm_window_exhausted")
        kind_, j, reason = outcome
        st.resolved.append((i, j, kind_))
        st.anchor = None
        st.next_allowed = j + 1
        if kind_ == "confirmed":
            confirms[j] = a
        i = st.next_allowed
    return confirms, anchors, st


# ────────────────────────────── 主运行入口 ──────────────────────────────

def _donchian_flags(seg, cfg):
    """逐 bar 计算 3a / 3b 多空双向基线标志与随附量值。全部基线排除当前 bar。"""
    nb = int(cfg["don_entry_bars"])
    nv = int(cfg["vol_n"])
    ice = float(cfg["ice_vol_mult"])
    flags = {}
    for i in range(max(nb, nv), len(seg)):
        box = seg[i - nb:i]
        upper = max(float(x.high) for x in box)
        lower = min(float(x.low) for x in box)
        b = seg[i]
        brk_long = float(b.close) > upper                      # 严格多头向上突破
        brk_short = float(b.close) < lower                     # 严格空头向下跌破
        vbase = common.mean(x.quote_volume for x in seg[i - nv:i])
        vol_ok = (vbase is not None and vbase > 0 and
                  float(b.quote_volume) > ice * vbase)
        bull = float(b.close) > float(b.open)
        bear = float(b.close) < float(b.open)
        flags[i] = {"don_upper": upper, "don_lower": lower, "vol_baseline": vbase,
                    "base_3a": bool(brk_long),
                    "base_3b": bool(brk_long and vol_ok and bull),
                    "base_3a_short": bool(brk_short),
                    "base_3b_short": bool(brk_short and vol_ok and bear),
                    "vol_ok": bool(vol_ok), "bull": bool(bull), "bear": bool(bear),
                    "quote_volume": float(b.quote_volume)}
    return flags


def _exits(seg, cfg):
    """唐奇安退出（小时收盘产生；执行时钟由执行层按 15m 开盘处理）。"""
    ne = int(cfg["don_exit_bars"])
    out = []
    for i in range(ne, len(seg)):
        lower = min(float(x.low) for x in seg[i - ne:i])
        if float(seg[i].close) < lower:
            out.append({"exit_i": i, "exit_open_time": int(seg[i].open_time),
                        "don_lower": lower})
    return out


def run(bars, resolved, arm_id=None, identity=None, require_identity=False):
    """纯核心入口。`resolved` = {"version","config","config_hash","frozen"}。

    `identity=(exchange, symbol)`（第15 修复）：**显式**并入每个事件/结构锚点的
    `event_uid` 与 `Event.exchange/symbol`。缺省 `None` 时与冻结版逐字节一致（legacy）。
    `require_identity=True` 且未给 identity ⇒ 直接 `配置无效`（新正式用途 fail-closed，
    不允许「无币/所身份」的信号进入正式研究链）。
    """
    arm = arm_id or getattr(run, "_arm_id", None) or "3a"
    if require_identity and not identity:
        return common.invalid_result(
            arm, ["require_identity=True 但未提供 identity=(exchange, symbol)："
                  "正式信号必须含交易所/币身份（拒绝无身份信号）"], TIMEFRAME)
    status, missing, errors = common.validate_config(
        resolved, REQUIRED_CONFIG, VALIDATION, RELATIONS)
    if status == common.STATUS_PENDING:
        return common.pending_result(arm, missing, TIMEFRAME)
    if status == common.STATUS_INVALID:
        return common.invalid_result(arm, errors, TIMEFRAME)
    cfg = resolved["config"]
    version = resolved.get("version")
    chash = resolved.get("config_hash")

    segs, rejects = _segments(bars)
    events = []
    exits = []
    seg_diag = []
    for (s, e) in segs:
        seg = bars[s:e]
        warm = int(cfg["warmup_contiguous_bars"])
        if len(seg) <= warm:
            seg_diag.append({"start_open": int(seg[0].open_time), "bars": len(seg),
                             "warmup": warm, "decided": 0,
                             "note": "段内不足 240 根连续有效预热，不产生任何事件"})
            continue
        trs = true_ranges(seg)
        atr = ewm_atr(trs, 1.0 / float(cfg["atr_n"]), int(cfg["atr_n"]))
        atr_prev = [None] + atr[:-1]                       # 检测 ATR = 上一完整 bar
        flags = _donchian_flags(seg, cfg)
        spring_c, spring_a, spring_st = _scan_structure(seg, cfg, "spring", atr_prev, warm,
                                                        identity)
        sc_c, sc_a, sc_st = _scan_structure(seg, cfg, "sc", atr_prev, warm, identity)
        seg_exits = _exits(seg, cfg)
        exits.extend({"open_time": x["exit_open_time"], "don_lower": x["don_lower"]}
                     for x in seg_exits)

        for i in range(warm, len(seg)):
            f = flags.get(i)
            # ── 多头方向 ──
            in_base_long = bool(f and f["base_3a"])
            in_base3b_long = bool(f and f["base_3b"])
            in_spring = i in spring_c
            in_sc = i in sc_c
            take_long = False
            if arm == "3b":
                take_long = in_base3b_long
            elif arm == "3c":
                take_long = in_base_long or in_spring
            elif arm == "3d":
                take_long = in_base_long or in_sc
            else:
                take_long = in_base_long

            if take_long:
                masks = []
                if in_base_long:
                    masks.append("base")
                if in_spring:
                    masks.append("spring")
                if in_sc:
                    masks.append("sc")
                b = seg[i]
                ev_long = _make_event(arm, version, chash, b, masks, f,
                                      spring_c.get(i), sc_c.get(i), atr[i], identity,
                                      direction="long")
                events.append(ev_long)

            # ── 空头方向 ──
            in_base_short = bool(f and f.get("base_3a_short"))
            in_base3b_short = bool(f and f.get("base_3b_short"))
            take_short = in_base3b_short if arm == "3b" else in_base_short
            if take_short:
                b = seg[i]
                ev_short = _make_event(arm, version, chash, b, ["base_short"], f,
                                       None, None, atr[i], identity,
                                       direction="short")
                events.append(ev_short)
        seg_diag.append({
            "start_open": int(seg[0].open_time), "bars": len(seg), "warmup": warm,
            "decided": max(0, len(seg) - warm),
            "spring_detections": len(spring_a), "spring_confirms": len(spring_c),
            "sc_detections": len(sc_a), "sc_confirms": len(sc_c),
            "donchian_breakouts": sum(1 for v in flags.values() if v["base_3a"]),
            "exits": len(seg_exits),
        })

    return {"status": common.STATUS_OK, "events": events,
            "exits": exits,
            "diagnostics": {
                "strategy": arm, "timeframe": TIMEFRAME,
                "bars": len(bars), "segments": len(segs), "segment_detail": seg_diag,
                "rejected_bars": rejects,
                "rejected_bars_total": sum(rejects.values()),
                "grid_rule": "严格 1h UTC 原生网格：open_time 整点对齐且 close_time=open+1h−1ms",
                "gap_reset_rule": "原生 1h 网格缺一根即重置事件与 240 根连续预热（A-09）",
                "note": "只消费已收盘 1h；基线排除当前 bar；检测 ATR 取上一完整 bar；"
                        "同 bar 不确认；确认只发一次；结构失效先于确认；base 优先且保留全部 source_masks；"
                        "非法 OHLC/负量/非原生网格/错周期时间戳一律拒绝（真零量合法）"}}


def _make_event(arm, version, chash, b, masks, f, spring_anchor, sc_anchor, atr_i,
                identity=None, direction="long"):
    ex_, sym_ = (tuple(identity) + ("", ""))[:2] if identity else ("", "")
    evd = {
        "source_masks": masks,
        "direction": direction,
        "execution_label": "base" if "base" in masks else ("spring" if "spring" in masks else "sc"),
        "atr_signal_bar": atr_i,                    # 执行层止损锚：S = 实际入场价 ∓ 2×ATR
        "don_upper_prev20": (f or {}).get("don_upper"),
        "don_lower_prev20": (f or {}).get("don_lower"),
        "vol_baseline_prev20": (f or {}).get("vol_baseline"),
        "quote_volume": (f or {}).get("quote_volume"),
        "close_pos": _close_pos(b),
        "signal_close": float(b.close),
        "signal_open": float(b.open),
        "signal_bar_open_time": int(b.open_time),
        "signal_bar_close_time": int(b.close_time),
        "event_uid": _uid(identity, arm, b.open_time, direction, ",".join(masks)),
    }
    if spring_anchor:
        evd["spring"] = {k: spring_anchor[k] for k in
                         ("box_high", "box_low", "support", "midline", "detection_low",
                          "detection_atr", "detect_open_time", "event_uid")}
        evd["spring_confirm_time"] = int(b.close_time)
    if sc_anchor:
        evd["sc"] = {k: sc_anchor[k] for k in
                     ("detection_low", "prior10_high", "target", "detection_atr",
                      "detect_open_time", "event_uid")}
        evd["sc_confirm_time"] = int(b.close_time)
    brk_text = "唐奇安向上突破（>20根最高）" if direction == "long" else "唐奇安向下跌破（<20根最低）"
    ref_level = (f or {}).get("don_upper") if direction == "long" else (f or {}).get("don_lower")
    conds = [Condition(brk_text,
                       round(float(b.close), 10), round(ref_level, 10)
                       if ref_level is not None else None,
                       "price", bool((f or {}).get("base_3a" if direction == "long" else "base_3a_short")),
                       "基线与当前 bar 严格比较，等号不算突破")]
    if spring_anchor:
        conds.append(Condition("Spring 确认（close > 冻结中线）",
                               round(float(b.close), 10), round(spring_anchor["midline"], 10),
                               "price", True, f"检测低点 {spring_anchor['detection_low']}"))
    if sc_anchor:
        conds.append(Condition("SC 确认（close > 恢复目标）",
                               round(float(b.close), 10), round(sc_anchor["target"], 10),
                               "price", True, f"检测低点 {sc_anchor['detection_low']}"))
    return Event(
        strategy_key=arm, strategy_version=version, config_hash=chash,
        exchange=ex_, market="", symbol=sym_, timeframe=TIMEFRAME,
        status="confirmed", direction=direction,
        event_time=int(b.close_time), confirmed_at=int(b.close_time),
        source_id=b.source_id, evidence=evd, conditions=conds)


# ────────────────────────────── 四组臂模块对象 ──────────────────────────────

_ARM_META = {
    "3a": "唐奇安基线：1h 收盘严格突破此前 20 根最高价",
    "3b": "3a AND 放量（quote成交额 > 此前20均量×1.5）AND 阳线（close>open）",
    "3c": "3a OR Spring 确认（跌破箱体后收回、第1–3根小时收盘站上冻结中线）",
    "3d": "3a OR SC 确认（放量新低后第1–5根小时收盘越过恢复目标）",
}


class WxArm:
    """把「同一确定性核心 + 不同入场条件」暴露成注册表可识别的模块对象。"""

    def __init__(self, arm_id):
        self.KEY = arm_id
        self.NAME = _ARM_META[arm_id]
        self.TIMEFRAME = TIMEFRAME
        self.DIRECTION = DIRECTION
        self.FAMILY = "F_WX"
        self.REQUIRED_CONFIG = REQUIRED_CONFIG
        self.VALIDATION = VALIDATION
        self.RELATIONS = RELATIONS

    def run(self, bars, resolved, identity=None, require_identity=False):
        return run(bars, resolved, arm_id=self.KEY, identity=identity,
                   require_identity=require_identity)


REGISTRY = {a: WxArm(a) for a in ARM_IDS}


def get(key):
    return REGISTRY.get(key)
