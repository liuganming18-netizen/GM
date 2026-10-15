# -*- coding: utf-8 -*-
"""策略公共件：bar 模型、分位、配置校验、事件结构。

因果性硬约束：
  - 只用已收盘 bar（is_closed=1）。
  - 历史分位/均值基线只取**当前 bar 之前**的 bars，绝不含当前或未来 bar。
  - 事件确认时间 = 证据 bar 的收盘时间，不回填到更早的 bar。

配置纪律：
  - 参数未配置 → 状态“待配置”。
  - 参数已给但类型/有限性/范围/窗口关系不合法 → 状态“配置无效”（不当 ok，不抛异常）。
  - 只有全部合法才进入运行；“配置无效”不采用任何默认值兜底。
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, asdict

STATUS_OK = "ok"
STATUS_PENDING = "待配置"
STATUS_INSUFFICIENT = "数据不足"
STATUS_INVALID = "配置无效"


@dataclass(frozen=True)
class Bar:
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trades: int
    taker_buy_volume: float
    taker_buy_quote_volume: float
    source_id: int | None = None

    @property
    def buy_ratio(self):
        """主动买入占比（计价口径）。总量为 0/缺失时为 None，不用 0 冒充。"""
        if not self.quote_volume:
            return None
        if self.taker_buy_quote_volume is None:
            return None
        return self.taker_buy_quote_volume / self.quote_volume

    @property
    def buy_sell_diff(self):
        """同一成交量单位下：买卖差 = 2×主动买入量 − 总量。

        仅作展示，**不**作为独立加分条件（避免把主动买入量重复计入两次证据）。
        """
        if self.taker_buy_volume is None or self.volume is None:
            return None
        return 2 * self.taker_buy_volume - self.volume


def from_row(row):
    def f(key):
        v = row[key]
        # 缺值语义：NULL 与空字符串都视为「无该字段数据」，返回 None，
        # 绝不用 0 冒充缺值（部分 OKX 12h 行的 taker_buy_volume 为空字符串）。
        if v is None:
            return None
        if isinstance(v, str) and v.strip() == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    return Bar(
        open_time=int(row["open_time"]), close_time=int(row["close_time"]),
        open=f("open"), high=f("high"), low=f("low"), close=f("close"),
        volume=f("volume"), quote_volume=f("quote_volume"),
        trades=int(row["trade_count"] or 0),
        taker_buy_volume=f("taker_buy_volume"),
        taker_buy_quote_volume=f("taker_buy_quote_volume"),
        source_id=row["source_id"] if "source_id" in row.keys() else None,
    )


def quantile(values, q):
    """线性插值分位（与 numpy.quantile 默认一致）。values 为空返回 None。

    q 非法（非数值/非有限/超出 0–1）时返回 None，**不抛异常**。
    合法区间内按 0–1 截断，避免越界索引。
    """
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    try:
        qf = float(q)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(qf):
        return None
    qf = min(1.0, max(0.0, qf))
    if len(xs) == 1:
        return xs[0]
    pos = qf * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] * (1 - frac) + xs[hi] * frac


def mean(values):
    xs = [v for v in values if v is not None]
    return sum(xs) / len(xs) if xs else None


def config_hash(config):
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_config(con, strategy_key, version=None):
    """从 strategy_configs 读取显式配置；无则 None。"""
    try:
        if version:
            row = con.execute("SELECT * FROM strategy_configs WHERE strategy_key=? AND version=?",
                              (strategy_key, version)).fetchone()
        else:
            row = con.execute("SELECT * FROM strategy_configs WHERE strategy_key=? "
                              "ORDER BY created_at DESC LIMIT 1", (strategy_key,)).fetchone()
    except Exception:  # 表不存在
        return None
    if not row:
        return None
    return {"version": row["version"], "config": json.loads(row["config_json"]),
            "config_hash": row["config_hash"], "frozen": bool(row["frozen"])}


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _finite(v):
    return _is_number(v) and math.isfinite(float(v))


def validate_config(resolved, required, spec=None, relations=None):
    """校验配置：键齐全 + 类型/有限性/范围 + 窗口关系。

    返回 (status, missing, errors)：
      - 未提供配置         → (STATUS_PENDING, list(required), [])
      - 缺键 / 值为 None   → (STATUS_PENDING, missing, [])
      - 类型/范围/关系非法 → (STATUS_INVALID, [], errors)
      - 全部合法           → (STATUS_OK, [], [])

    spec 为 {key: 规则}，规则字段：
      type: "int" | "float"
      min / max: 数值边界；exclusive_min / exclusive_max: 是否开区间
    relations 为 [callable(cfg) -> list[str]]，用于窗口关系等跨键约束。
    """
    if not resolved:
        return STATUS_PENDING, list(required), []
    cfg = resolved.get("config") or {}
    missing = [k for k in required if k not in cfg or cfg[k] is None]
    if missing:
        return STATUS_PENDING, missing, []

    errors = []
    for key, rule in (spec or {}).items():
        v = cfg.get(key)
        if not _finite(v):
            errors.append(f"{key} 不是有限数值（得到 {v!r}）")
            continue
        fv = float(v)
        kind = rule.get("type")
        if kind == "int":
            if fv != int(fv):
                errors.append(f"{key} 必须为整数（得到 {v!r}）")
                continue
            iv = int(fv)
            if "min" in rule and iv < rule["min"]:
                errors.append(f"{key} 必须 ≥ {rule['min']}（得到 {iv}）")
            if "max" in rule and iv > rule["max"]:
                errors.append(f"{key} 必须 ≤ {rule['max']}（得到 {iv}）")
        elif kind == "float":
            if "min" in rule:
                if rule.get("exclusive_min"):
                    if fv <= rule["min"]:
                        errors.append(f"{key} 必须 > {rule['min']}（得到 {fv}）")
                elif fv < rule["min"]:
                    errors.append(f"{key} 必须 ≥ {rule['min']}（得到 {fv}）")
            if "max" in rule:
                if rule.get("exclusive_max"):
                    if fv >= rule["max"]:
                        errors.append(f"{key} 必须 < {rule['max']}（得到 {fv}）")
                elif fv > rule["max"]:
                    errors.append(f"{key} 必须 ≤ {rule['max']}（得到 {fv}）")

    for rel in (relations or []):
        try:
            errors.extend(rel(cfg))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"窗口关系校验失败：{exc}")

    if errors:
        return STATUS_INVALID, [], errors
    return STATUS_OK, [], []


def check_config(resolved, required):
    """兼容旧调用：仅检查键是否齐全（不含类型/范围校验）。"""
    status, missing, _ = validate_config(resolved, required)
    return status != STATUS_PENDING, missing


@dataclass
class Condition:
    name: str
    actual: object = None
    threshold: object = None
    unit: str = None
    satisfied: bool = None
    note: str = None


@dataclass
class Event:
    strategy_key: str
    strategy_version: str
    config_hash: str
    exchange: str
    market: str
    symbol: str
    timeframe: str
    status: str
    direction: str
    event_time: int
    confirmed_at: int
    evidence: dict = field(default_factory=dict)
    conditions: list = field(default_factory=list)
    data_version: str = None
    source_id: int = None

    def to_dict(self):
        d = asdict(self)
        d["conditions"] = [asdict(c) if isinstance(c, Condition) else c for c in self.conditions]
        return d


def pending_result(strategy_key, missing, timeframe=None):
    return {"status": STATUS_PENDING, "events": [],
            "diagnostics": {"strategy": strategy_key, "missing_config": list(missing),
                            "timeframe": timeframe,
                            "note": "参数未冻结，必须显式配置并版本化；不采用任何默认阈值"}}


def invalid_result(strategy_key, errors, timeframe=None):
    return {"status": STATUS_INVALID, "events": [],
            "diagnostics": {"strategy": strategy_key, "config_errors": list(errors),
                            "timeframe": timeframe,
                            "note": "配置非法：类型/范围/窗口关系校验未通过；不采用任何默认值兜底"}}


def insufficient_result(strategy_key, timeframe, got_bars, need_bars):
    return {"status": STATUS_INSUFFICIENT, "events": [],
            "diagnostics": {"strategy": strategy_key, "timeframe": timeframe,
                            "available_bars": got_bars, "required_bars": need_bars,
                            "note": "该周期无足够已收盘数据；不合成、不用其他周期冒充"}}
