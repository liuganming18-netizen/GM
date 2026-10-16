# -*- coding: utf-8 -*-
"""WX_OI_RESONANCE_V1 —— **新候选策略**的独立身份与实验配置（2026-10-16 落地）。

⚠️ 身份声明（必须原样保留，不得改写）
────────────────────────────────────
本包是**新候选规则**，**不是**历史 v3.1 策略的同一实现，**不继承**其任何成绩：

  · 与 `F_WX 3a/3b/3c/3d`（唐奇安基线 + 威科夫 Spring/SC，1h、只多、240 根预热、
    共享 5000U、单笔 0.5%、组合 3%）**不是同一策略**；
  · 与 `WyckoffMaster v3.1` 的历史阶梯 **M0–M6 成绩完全无关**：本包不引用、不加载、
    不重算、不继承任何历史实验指标；旧成绩只在 `_legacy_archive_v31/` 里作为历史事实保留。
  · 旧 `M2b` 是**条件距离实验**（`D = max(P_entry − (L_struct − 0.8ATR), 1.5ATR)`，
    为绕过 M2 的 3.5ATR 超限拒单而预注册的**止损距离**变体）；新稿把回踩当 **LPS**
    （Last Point of Support，威科夫回踩确认买点）使用。二者**语义不同**，故本包一律用
    新信号名 `lps_pullback` / 新止损模式 `lps_structural`，**不复用** `structural_m2b`
    这个名字，也不把 M2b 的历史统计套到 LPS 上。

参数归属
────────
附件给出的 ATR14 / buffer 0.2 / 窗口 8 / 资金 100000 / 单笔 1.5% / 组合 8% / 滑点 3bps
**属于本份新实验配置**，独立版本化（`EXPERIMENT_ID`）；**不覆盖、不改写**旧账户的冻结参数
（`WyckoffMaster.params.FROZEN` / `execution.EXEC_FROZEN`）——旧参数仍在原处、仍是旧策略身份。

纪律
────
纯常量与校验，不含计算逻辑；所有数值只此一处，调用方不得就地覆盖。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from typing import Any, Dict, Optional

# ══════════════════════════ 身份标识 ══════════════════════════

FAMILY = "F_OI_RES"
STRATEGY_ID = "WX_OI_RESONANCE_V1"
STRATEGY_KEY = "wx_oi_resonance_v1"          # 注册表键
SIGNAL_VERSION = "oi_res_v1.0.0"
CORE_VERSION = "oi_res_core_v1.0.0"
EXEC_CORE_VERSION = "oi_res_exec_v1.0.0"
EXPERIMENT_ID = "exp_oi_resonance_v1_2026-10-16"

SUPERSEDES = "F_WX(3a/3b/3c/3d) + WyckoffMaster v3.1 core"
SUPERSEDED_BY_ARCHIVE = "策略/WyckoffMaster/_legacy_archive_v31"

# 明确「不继承」清单（对外回执与注册表描述逐条披露）
NOT_INHERITED = (
    "F_WX 3a/3b/3c/3d 的入场事件与任何历史净值/夏普/回撤",
    "WyckoffMaster v3.1 历史阶梯 M0/M1/M2/M2b/M3/M4 的任何成绩",
    "v3.1 前向验证 M5/M6 的任何统计量",
    "旧共享账户 5000U / 单笔 0.5% / 组合 3% / 名义 10% 等冻结参数",
    "旧 M2b 条件距离实验的任何计数与比例",
)

# 门禁：未通过前**禁止正式运行**（run_frozen 恒 False）
GATE_IDS = ("G01", "G02", "G03", "G04", "G05")
GATE_STATUS = {g: "pending" for g in GATE_IDS}
RUN_FROZEN = False

# ══════════════════════════ 时间单位（必须显式）══════════════════════════
# 附件用「全量 max/median」推断单位 ⇒ 同一序列的前缀与全量可能被判成不同单位
# （前缀不变性被破坏）。本包改为**显式声明 + 逐值范围校验**：
#   · 声明值权威（"s" / "ms" / "us"）；未声明 ⇒ 直接拒绝 `time_unit_undeclared`；
#   · 每个时间戳都必须落在该单位的合理区间内，否则拒绝（可捕捉声明错单位）；
#   · 多币声明不一致 ⇒ 拒绝（不得静默归一）。
TIME_UNITS = ("s", "ms", "us")
UNIT_SCALE = {"s": 1, "ms": 1_000, "us": 1_000_000}
# 合理 epoch 秒区间：2009-01-03T18:15:05Z .. 2100-01-01T00:00:00Z
EPOCH_SEC_MIN = 1_231_468_800
EPOCH_SEC_MAX = 4_102_444_800


# ══════════════════════════ 信号参数（新实验配置）══════════════════════════

@dataclass(frozen=True)
class SignalParams:
    """信号层配置。`bars_per_day` **必须显式**（附件按 max/median 推断 ⇒ 前缀不稳定）。"""

    # —— 唐奇安 / ATR ——
    donchian_entry_window: int = 20
    donchian_exit_window: int = 10
    atr_period: int = 14
    atr_mult: float = 3.0

    # —— 结构止损 ——
    structural_buffer_atr: float = 0.2

    # —— 回踩（**新信号名 lps_pullback**，与旧 M2b 条件距离实验分离）——
    # 附件对回踩只要求「触及突破位附近 + 不破前低 + 重新站上突破位且收阳」；
    # **不**含任何 close_pos 阈值（此前的 `pullback_min_close_pos` 已按附件删除）。
    pullback_window: int = 8
    pullback_proximity_atr: float = 0.5      # 回踩须触及 breakout 位 + 0.5ATR 以内

    # —— Spring / SC ——
    enable_sc: bool = True
    sc_vol_mult: float = 2.0
    sc_range_atr_mult: float = 1.5
    sc_lower_wick_frac: float = 0.50

    # —— 方向 / 因子门槛 ——
    enable_short: bool = False               # 默认只多（与旧策略一致，但为**本包**默认值）

    # **OI 共振门槛：当前 UNSET（未批准生产阈值）**
    # ────────────────────────────────────────
    # 来源追查（2026-10-16，WORK-02）：
    #   · 附件（`已粘贴的文本.txt` L205）只给出
    #     `oi_min_resonance = float(cfg.get("oi_min_resonance", 0.0))` 的**默认值**，
    #     并未给出任何**经批准的生产阈值**；
    #   · 全工程无第二处正式来源（无生效视图、无登记表、无实验记录给出该值）。
    # 因此按施工单 W2-1：**没有正式批准值 ⇒ 不得擅自发明**，置 `None`（= UNSET）。
    # 语义：`None` ⇒ 因子门槛**未配置**（OI 仍照实计算并逐行标注 quality / pit_proven，
    # 但不作为拒单依据）；数值 ⇒ 显式门槛，必须 `0 < v <= 1` 才真正参与决策。
    # 后果：`formal_run_allowed` 在 UNSET 期间**恒 False** ⇒ 阻止正式生产运行，
    # 只允许独立候选研究（回测）运行，且运行记录必须带 `oi_min_resonance_status=UNSET`。
    oi_min_resonance: Optional[float] = None

    # —— 时间网格（显式，禁止推断）——
    bars_per_day: int = 24                   # 信号周期 1h ⇒ 24
    signal_bar_seconds: int = 3600           # 确认时刻 = 信号 bar 收盘 + 该值

    # —— OI 因子窗口 ——
    oi_chg_window: int = 20
    oi_rank_days: int = 90
    oi_rank_min_days: int = 7                # min_periods = max(20, bars_per_day × 该值)
    oi_price_div_window: int = 12
    oi_mom_flux_window: int = 48
    oi_vol_ma_window: int = 48
    oi_vol_ratio_clip: float = 5.0

    # —— OI 数据可得性（PIT，施工单 W2-5）——
    # `None` = **未声明发布延迟** ⇒ 无法证明 PIT ⇒ OI 标记 `pit_proven=False`，
    # 不得作为决策依据（"宁可标记不可用，也不乐观使用"）。
    # 声明为非负毫秒数后，只有 `bucket_time + lag <= bar_close` 的观测才可用于决策。
    oi_publication_lag_ms: Optional[int] = None

    # —— 价格最小变动（0 = 不取整）——
    price_step: float = 0.0

    def identity(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def hash(self) -> str:
        return hashlib.sha256(json.dumps(self.identity(), sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()


SIGNAL_FROZEN = SignalParams()

# OI 共振权重（**新因子名**，不冒充旧 M4/M5/M6）
# 0.50 分项 = **原始 open_interest 的当期 rolling 分位**（附件第 72 行定义；当根收盘已可得，
# 不是未来函数），命名 `f_oi_level_rank`，**不叫 m4**、也不改写成「OI 变化率分位」。
OI_WEIGHTS = {
    "f_oi_level_rank": 0.50,
    "f_oi_price_div_rank": 0.25,
    "f_oi_mom_flux_rank": 0.25,
}


# ══════════════════════════ 执行参数（新实验配置）══════════════════════════

@dataclass(frozen=True)
class ExecParams:
    """合成执行配置。**真实下单恒禁用**（本包无任何下单/写接口）。"""

    initial_equity: float = 100_000.0
    risk_per_trade: float = 0.008            # 单笔最大亏损 0.8% (0.5%~1.0%)
    max_portfolio_risk: float = 0.04         # 组合总风险上限 4%
    max_portfolio_margin_pct: float = 0.40   # 总名义仓位/保证金占用上限 40% (30%~50%)
    max_concurrent_trades: int = 3           # 最大并发持仓笔数 3 笔 (3~5笔)
    max_leverage_per_symbol: float = 2.0     # 单标的杠杆上限 2.0 倍 (1~2倍)

    # 币安合约普通用户 (VIP 0) 标准费率：Maker 0.02%, Taker 0.05%
    fee_rate: float = 0.0005                 # 兼容旧字段：默认按 Taker 0.05%
    maker_fee_rate: float = 0.0002           # 币安普通用户 Maker 万2
    taker_fee_rate: float = 0.0005           # 币安普通用户 Taker 万5
    slippage_bps: float = 3.0                # 默认滑点 3 bps (万3)

    enable_trailing_exit: bool = True
    force_close_at_end: bool = True

    # 资金费：严格按实际发生周期结算 (settlement_events)，绝不写死8小时
    funding_model: str = "settlement_events"

    # 安全开关（两道）
    # `allow_new_entries` 只作用于**本模块的合成模拟**（纯函数、无 IO、无账户）；
    # 它**不是**实盘开仓开关 —— 实盘开关在 `exec_bridge.new_entry_switch_default()`，恒 False。
    allow_new_entries: bool = True
    live_order_enabled: bool = False         # 真实下单：恒 False，本包不实现该路径

    def identity(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def hash(self) -> str:
        return hashlib.sha256(json.dumps(self.identity(), sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()


EXEC_FROZEN = ExecParams()
FUNDING_MODELS = ("settlement_events", "assumption_fixed_8h")


# ══════════════════════════ 校验 ══════════════════════════

SIGNAL_VALIDATION = {
    "donchian_entry_window": (1, None, int),
    "donchian_exit_window": (1, None, int),
    "atr_period": (2, None, int),
    "atr_mult": (0.0, None, float),
    "structural_buffer_atr": (0.0, None, float),
    "pullback_window": (1, None, int),
    "pullback_proximity_atr": (0.0, None, float),
    "sc_vol_mult": (0.0, None, float),
    "sc_range_atr_mult": (0.0, None, float),
    "sc_lower_wick_frac": (0.0, 1.0, float),
    # `None` = UNSET（未批准生产阈值）；数值时须落在 [0, 1]（见 validate_signal_params）
    "oi_min_resonance": (0.0, 1.0, float),
    "bars_per_day": (1, None, int),
    "signal_bar_seconds": (1, None, int),
    "oi_chg_window": (1, None, int),
    "oi_rank_days": (1, None, int),
    "oi_rank_min_days": (1, None, int),
    "oi_price_div_window": (1, None, int),
    "oi_mom_flux_window": (1, None, int),
    "oi_vol_ma_window": (1, None, int),
    "oi_vol_ratio_clip": (0.0, None, float),
    "price_step": (0.0, None, float),
    # PIT：OI 发布延迟（毫秒）。None = 未声明（⇒ pit_proven=False）
    "oi_publication_lag_ms": (0, None, int),
}

EXEC_VALIDATION = {
    "initial_equity": (0.0, None, float),
    "risk_per_trade": (0.0, 1.0, float),
    "max_portfolio_risk": (0.0, 1.0, float),
    "max_portfolio_margin_pct": (0.0, 1.0, float),
    "max_concurrent_trades": (1, None, int),
    "max_leverage_per_symbol": (0.0, None, float),
    "fee_rate": (0.0, 1.0, float),
    "maker_fee_rate": (0.0, 1.0, float),
    "taker_fee_rate": (0.0, 1.0, float),
    "slippage_bps": (0.0, None, float),
}


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# `oi_min_resonance` 是**唯一**允许为 None 的参数（None = UNSET）
OPTIONAL_NUMERIC_FIELDS = ("oi_min_resonance", "oi_publication_lag_ms")


def validate_signal_params(p: SignalParams = SIGNAL_FROZEN) -> list[str]:
    errs: list[str] = []
    for name, (lo, hi, kind) in SIGNAL_VALIDATION.items():
        v = getattr(p, name)
        # UNSET 语义：仅 `oi_min_resonance` 允许 None（未批准生产阈值）
        if v is None and name in OPTIONAL_NUMERIC_FIELDS:
            continue
        if kind is int:
            if not isinstance(v, int) or isinstance(v, bool):
                errs.append(f"{name} 必须为 int（得到 {v!r}）")
                continue
        else:
            if not _is_num(v):
                errs.append(f"{name} 必须为数值（得到 {v!r}）")
                continue
        fv = float(v)
        if not (fv == fv and fv not in (float("inf"), float("-inf"))):
            errs.append(f"{name} 必须有限（得到 {v!r}）")
            continue
        if lo is not None and fv < lo:
            errs.append(f"{name} 必须 >= {lo}（得到 {fv}）")
        if hi is not None and fv > hi:
            errs.append(f"{name} 必须 <= {hi}（得到 {fv}）")
    if p.donchian_exit_window > p.donchian_entry_window:
        errs.append("donchian_exit_window 不得大于 donchian_entry_window")
    if p.oi_rank_min_days > p.oi_rank_days:
        errs.append("oi_rank_min_days 不得大于 oi_rank_days")
    return errs


def validate_exec_params(e: ExecParams = EXEC_FROZEN) -> list[str]:
    errs: list[str] = []
    for name, (lo, hi, kind) in EXEC_VALIDATION.items():
        v = getattr(e, name)
        if kind is int:
            if not isinstance(v, int) or isinstance(v, bool):
                errs.append(f"{name} 必须为 int（得到 {v!r}）")
                continue
        else:
            if not _is_num(v):
                errs.append(f"{name} 必须为数值（得到 {v!r}）")
                continue
        fv = float(v)
        if not (fv == fv and fv not in (float("inf"), float("-inf"))):
            errs.append(f"{name} 必须有限（得到 {v!r}）")
            continue
        if lo is not None and fv < lo:
            errs.append(f"{name} 必须 >= {lo}（得到 {fv}）")
        if hi is not None and fv > hi:
            errs.append(f"{name} 必须 <= {hi}（得到 {fv}）")
    if e.funding_model not in FUNDING_MODELS:
        errs.append(f"illegal_funding_model:{e.funding_model}")
    if e.live_order_enabled:
        errs.append("live_order_must_stay_disabled")
    return errs


def config_identity() -> dict:
    """对外身份块（每响应必备）。"""
    return {
        "strategy_id": STRATEGY_ID,
        "strategy_key": STRATEGY_KEY,
        "family": FAMILY,
        "signal_version": SIGNAL_VERSION,
        "core_version": CORE_VERSION,
        "exec_core_version": EXEC_CORE_VERSION,
        "experiment_id": EXPERIMENT_ID,
        "signal_params_hash": SIGNAL_FROZEN.hash(),
        "exec_params_hash": EXEC_FROZEN.hash(),
        "param_hash": param_hash(),
        "param_hash_basis": list(PARAM_HASH_BASIS),
        "oi_min_resonance_status": oi_min_resonance_status(),
        "run_frozen": RUN_FROZEN,
        "gate_status": dict(GATE_STATUS),
        "supersedes": SUPERSEDES,
        "legacy_archive": SUPERSEDED_BY_ARCHIVE,
        "not_inherited": list(NOT_INHERITED),
        "live_order_enabled": False,
        "allow_new_entries": EXEC_FROZEN.allow_new_entries,
    }


# ══════════════════════════ OI 门槛状态（W2-1）══════════════════════════

OI_MIN_RESONANCE_UNSET = "UNSET"
OI_MIN_RESONANCE_SET = "SET"
OI_MIN_RESONANCE_DISABLED_ZERO = "SET_ZERO_NO_GATE"


def oi_min_resonance_status(p: SignalParams = SIGNAL_FROZEN) -> str:
    """`UNSET`（未批准生产阈值）/ `SET_ZERO_NO_GATE`（显式 0，门槛不生效）/ `SET`。"""
    v = p.oi_min_resonance
    if v is None:
        return OI_MIN_RESONANCE_UNSET
    if float(v) <= 0.0:
        return OI_MIN_RESONANCE_DISABLED_ZERO
    return OI_MIN_RESONANCE_SET


def oi_gate_enabled(p: SignalParams = SIGNAL_FROZEN) -> bool:
    """门槛是否**真正**参与拒单。UNSET 或 <=0 ⇒ 不参与。"""
    return p.oi_min_resonance is not None and float(p.oi_min_resonance) > 0.0


# ══════════════════════════ 统一 param_hash（W2-2）══════════════════════════

# 覆盖面（施工单 W2-2 逐项对号）
PARAM_HASH_BASIS = (
    "strategy_key", "strategy_id", "strategy_version(signal_version)",
    "core_version", "exec_core_version", "experiment_id",
    "timeframe", "exec_timeframe", "exchange", "market", "direction",
    "signal_params.*",   # 含 OI 参数 / Wyckoff 参数 / ATR / Donchian / price_step /
                         # bars_per_day / signal_bar_seconds（执行时点）/ oi_publication_lag_ms
    "exec_params.*",     # 含 position sizing / leverage / risk limit / fee profile /
                         # slippage profile / exit policy（trailing、期末平仓）/ funding_model
    "oi_weights",
)


def param_hash(signal: Optional[SignalParams] = None,
               exec_cfg: Optional[ExecParams] = None,
               *,
               timeframe: str = "1h",
               exec_timeframe: str = "15m",
               exchange: str = "binance",
               market: str = "um",
               direction: str = "long") -> str:
    """**统一参数指纹**：覆盖一切能改变订单结果的身份与参数。

    与 `SignalParams.hash()` / `ExecParams.hash()` 的关系：
      · 两个分项哈希各自只覆盖本层字段，**不含**策略身份与周期 ⇒ 不足以单独作为
        「同一份规则」的指纹（改 `timeframe` 不会让它们变）；
      · `param_hash` = 身份 + 周期 + 两层全部字段 + 权重，一次算全。
    验收：改任一影响行为的参数 ⇒ `param_hash` 必变。
    """
    p = signal or SIGNAL_FROZEN
    e = exec_cfg or EXEC_FROZEN
    payload: Dict[str, Any] = {
        "strategy_key": STRATEGY_KEY,
        "strategy_id": STRATEGY_ID,
        "strategy_version": SIGNAL_VERSION,
        "core_version": CORE_VERSION,
        "exec_core_version": EXEC_CORE_VERSION,
        "experiment_id": EXPERIMENT_ID,
        "timeframe": str(timeframe),
        "exec_timeframe": str(exec_timeframe),
        "exchange": str(exchange),
        "market": str(market),
        "direction": str(direction),
        "signal_params": p.identity(),
        "exec_params": e.identity(),
        "oi_weights": dict(OI_WEIGHTS),
        "oi_min_resonance_status": oi_min_resonance_status(p),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
                                     separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def signal_params_dict(p: SignalParams = SIGNAL_FROZEN) -> dict:
    return asdict(p)


def exec_params_dict(e: ExecParams = EXEC_FROZEN) -> dict:
    return asdict(e)
