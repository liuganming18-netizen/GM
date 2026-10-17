# -*- coding: utf-8 -*-
"""WX_EVENT_TREND_V0_1 · 交易所合约规格（tick / step / 最小量 / 最小名义额）。

为什么要这个模块
────────────────
修复前 `engine_adapter.py` 把 `price_step` 写死成 `0.01` 并套到**所有标的**上。
对价格 0.004~0.025 的 SPACE 来说，卖价向更便宜取整会落到 **0.0**（零元卖出）；
AIGENSYN / TRUTH / COTI 同样被砍掉 3~4 位有效小数。这不是「滑点」，是精度错误。

本模块的三条硬边界
──────────────────
1. **只认交易所原始响应**。规格取自 `exchangeInfo` 原始 JSON（`filters` 里的
   `PRICE_FILTER.tickSize` / `LOT_SIZE` / `MARKET_LOT_SIZE` / `MIN_NOTIONAL`），
   并记录：来源 URL、获取时刻、证据文件路径、证据 sha256、合约上市时刻。
2. **当前规格 ≠ 历史规格**。只有当窗口被**两个数值一致、且分别早于窗口开始 /
   晚于窗口结束**的快照夹住时，才算 `verified`；否则一律 `unverified_historical`，
   该样本**不得**作为正式回测通过（`formal_pass_allowed=False`）。
3. **禁止从 K 线小数位推断 tick**。K 线只用于**证伪**：若窗口内的真实价格
   不在该 tick 的整数网格上，则该快照对该窗口**确定不适用** ⇒ `contradicted`。

步长算术全部走 `Decimal`（从原始十进制字符串构造），不用二进制浮点比较，
避免 `100.03/0.01 = 10002.999999999998` 这类误差把价格多取整一档。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ── 适用性结论码 ──
APPLICABILITY_VERIFIED = "verified"                    # 被一致快照包围 ⇒ 可用于正式回测
APPLICABILITY_UNVERIFIED_HISTORICAL = "unverified_historical"  # 有规格但证明不了当时适用
APPLICABILITY_CONTRADICTED = "contradicted"            # 观察价不在 tick 网格 ⇒ 确定不适用
APPLICABILITY_NOT_LISTED = "not_listed"                # 窗口早于合约上市
APPLICABILITY_MISSING = "missing"                      # 快照里没有该合约

# 默认证据目录（可用环境变量 CONTRACTS_DIR 覆盖，便于隔离验证）
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONTRACTS_DIR = _PROJECT_ROOT / "数据" / "contracts" / "旧标的池"

# 浮点→Decimal 的安全入口：只接受有限值
def to_decimal(value: Any, *, field_name: str = "value") -> Decimal:
    """把 float / int / str / Decimal 转成 Decimal。

    浮点走 `str()`（最短往返表示），因此 `float("0.0000010") -> "1e-06" ->
    Decimal("0.000001")` 仍然是精确的十进制值，不会引入二进制噪声。
    """
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, bool):
        raise InvalidOperation(f"{field_name}: 不接受布尔值")
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidOperation(f"{field_name}: 非有限浮点 {value!r}")
        d = Decimal(str(value))
    elif isinstance(value, str):
        d = Decimal(value.strip())
    else:
        raise InvalidOperation(f"{field_name}: 不支持的类型 {type(value).__name__}")
    if not d.is_finite():
        raise InvalidOperation(f"{field_name}: 非有限值 {value!r}")
    return d


def _dec_str(value: Any) -> str:
    """规范化为不带指数的十进制字符串（`1e-06` → `0.000001`）。"""
    d = to_decimal(value)
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


# ══════════════════════════ 步长运算（唯一实现）══════════════════════════

def floor_to_step(value: Any, step: Any) -> Decimal:
    """向下取整到步长（数量用）。`step<=0` ⇒ 抛错，绝不静默跳过。"""
    v = to_decimal(value, field_name="value")
    s = to_decimal(step, field_name="step")
    if s <= 0:
        raise InvalidOperation(f"step_must_be_positive:{step!r}")
    n = (v / s).to_integral_value(rounding="ROUND_FLOOR")
    return (n * s).normalize() if n != 0 else Decimal(0)


def ceil_to_step(value: Any, step: Any) -> Decimal:
    """向上取整到步长（买入价用：向更贵方向，最劣）。"""
    v = to_decimal(value, field_name="value")
    s = to_decimal(step, field_name="step")
    if s <= 0:
        raise InvalidOperation(f"step_must_be_positive:{step!r}")
    n = (v / s).to_integral_value(rounding="ROUND_CEILING")
    return (n * s).normalize() if n != 0 else Decimal(0)


def round_price_to_tick(price: Any, tick: Any, *, is_buy: bool) -> Decimal:
    """按方向取整到 tick：买入向上（更贵），卖出向下（更便宜）。"""
    return ceil_to_step(price, tick) if is_buy else floor_to_step(price, tick)


# ══════════════════════════ 规格对象 ══════════════════════════

@dataclass(frozen=True)
class InstrumentSpec:
    """一个合约在某个**证据时点**的规格 + 它对某个回测窗口的**适用性判定**。"""

    symbol: str
    venue: str
    market: str

    price_step: str            # 精确十进制文本，如 "0.0000010"
    qty_step: str
    min_qty: str
    max_qty: str
    min_notional: str
    price_precision: int
    quantity_precision: int
    order_type: str            # "MARKET" / "LIMIT"（决定用 LOT_SIZE 还是 MARKET_LOT_SIZE）

    source_url: str
    source_kind: str
    fetched_at_ms: Optional[int]
    evidence_path: Optional[str]
    evidence_sha256: Optional[str]
    onboard_ms: Optional[int]
    snapshot_server_ms: Optional[int]

    applicability: str = APPLICABILITY_MISSING
    applicability_note: str = ""
    formal_pass_allowed: bool = False
    window_start_ms: Optional[int] = None
    window_end_ms: Optional[int] = None
    contradiction_samples: Tuple[Dict[str, Any], ...] = ()

    # —— 便捷访问 ——
    @property
    def tick(self) -> Decimal:
        return to_decimal(self.price_step, field_name="price_step")

    @property
    def qty_inc(self) -> Decimal:
        return to_decimal(self.qty_step, field_name="qty_step")

    @property
    def min_qty_dec(self) -> Decimal:
        return to_decimal(self.min_qty, field_name="min_qty")

    @property
    def min_notional_dec(self) -> Decimal:
        return to_decimal(self.min_notional, field_name="min_notional")

    @property
    def blocking(self) -> bool:
        return not self.formal_pass_allowed

    def validate(self) -> None:
        """规格自检：任何一项非法即抛错（不允许「0 步长」蒙混）。"""
        if self.tick <= 0:
            raise InvalidOperation(f"invalid_tick:{self.symbol}:{self.price_step}")
        if self.qty_inc <= 0:
            raise InvalidOperation(f"invalid_qty_step:{self.symbol}:{self.qty_step}")
        if self.min_qty_dec < 0:
            raise InvalidOperation(f"invalid_min_qty:{self.symbol}:{self.min_qty}")
        if self.min_notional_dec < 0:
            raise InvalidOperation(f"invalid_min_notional:{self.symbol}:{self.min_notional}")

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["contradiction_samples"] = [dict(x) for x in self.contradiction_samples]
        d["blocking"] = self.blocking
        return d

    def identity(self) -> Dict[str, Any]:
        """只保留影响撮合数值的字段（用于 run 身份/指纹）。"""
        return {
            "symbol": self.symbol,
            "venue": self.venue,
            "market": self.market,
            "price_step": self.price_step,
            "qty_step": self.qty_step,
            "min_qty": self.min_qty,
            "min_notional": self.min_notional,
            "order_type": self.order_type,
            "evidence_sha256": self.evidence_sha256,
            "applicability": self.applicability,
        }


# ══════════════════════════ 快照加载 ══════════════════════════

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _filters_of(sym_doc: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {f.get("filterType"): f for f in (sym_doc.get("filters") or [])}


def _spec_fields_from_filters(sym_doc: Dict[str, Any], order_type: str) -> Dict[str, Any]:
    """从 exchangeInfo 的 filters 取规格。

    数量侧按**实际订单类型**取：MARKET ⇒ `MARKET_LOT_SIZE`（缺失回落 `LOT_SIZE`），
    并把两侧取更保守的一侧（步长/最小量取 max，最大量取 min）——
    与 `框架/backend/paper_engine.py::_limits_from_spec` 的既有口径一致，不另立第二套。
    """
    fl = _filters_of(sym_doc)
    pf = fl.get("PRICE_FILTER") or {}
    lot = fl.get("LOT_SIZE") or {}
    mkt = fl.get("MARKET_LOT_SIZE") or {}
    mn = fl.get("MIN_NOTIONAL") or {}

    def _num(d: Dict[str, Any], key: str) -> Optional[str]:
        v = d.get(key)
        if v is None or v == "":
            return None
        return _dec_str(v)

    tick = _num(pf, "tickSize")
    if tick is None:
        raise InvalidOperation(f"missing_tick_size:{sym_doc.get('symbol')}")

    base = lot if order_type.upper() != "MARKET" else (mkt or lot)
    step = _num(base, "stepSize") or _num(lot, "stepSize")
    min_qty = _num(base, "minQty") or _num(lot, "minQty")
    max_qty = _num(base, "maxQty") or _num(lot, "maxQty")
    if step is None:
        raise InvalidOperation(f"missing_step_size:{sym_doc.get('symbol')}")

    # 两侧都存在时取更保守的一侧（步长/最小量取大，最大量取小）
    if lot and mkt:
        step = _dec_str(max(to_decimal(step), to_decimal(_num(lot, "stepSize") or "0"),
                            to_decimal(_num(mkt, "stepSize") or "0")))
        min_qty = _dec_str(max(to_decimal(min_qty or "0"),
                               to_decimal(_num(lot, "minQty") or "0"),
                               to_decimal(_num(mkt, "minQty") or "0")))
        mx_vals = [x for x in (_num(lot, "maxQty"), _num(mkt, "maxQty")) if x]
        if mx_vals:
            max_qty = _dec_str(min(to_decimal(x) for x in mx_vals))

    return {
        "price_step": tick,
        "qty_step": step,
        "min_qty": min_qty or "0",
        "max_qty": max_qty or "0",
        "min_notional": _num(mn, "notional") or "0",
        "price_precision": int(sym_doc.get("pricePrecision") or 0),
        "quantity_precision": int(sym_doc.get("quantityPrecision") or 0),
    }


@dataclass(frozen=True)
class _Snapshot:
    path: Path
    sha256: str
    server_ms: int
    saved_utc: str
    url: str
    symbols: Dict[str, Dict[str, Any]] = field(repr=False, default_factory=dict)


class SpecBook:
    """交易所规格簿：装载证据目录下的全部 `exchangeInfo_*.json`。

    只读、确定性：同一目录同一批文件 ⇒ 同一结果。
    """

    def __init__(self, contracts_dir: Optional[Path] = None):
        self.dir = Path(contracts_dir or os.environ.get("CONTRACTS_DIR") or DEFAULT_CONTRACTS_DIR)
        self._snapshots: List[_Snapshot] = []
        self._load()

    def _load(self) -> None:
        if not self.dir.exists():
            return
        for p in sorted(self.dir.glob("exchangeInfo_futures_*.json")):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    doc = json.load(f)
            except Exception:
                continue
            syms = {s.get("symbol"): s for s in (doc.get("symbols") or []) if s.get("symbol")}
            if not syms:
                continue
            self._snapshots.append(_Snapshot(
                path=p, sha256=_sha256_file(p),
                server_ms=int(doc.get("serverTime") or 0),
                saved_utc=str(p.stem).split("_")[-1],
                url="https://fapi.binance.com/fapi/v1/exchangeInfo",
                symbols=syms,
            ))
        self._snapshots.sort(key=lambda s: (s.server_ms, s.path.name))

    # ── 查询 ──
    @property
    def snapshots(self) -> List[_Snapshot]:
        return list(self._snapshots)

    def has_symbol(self, symbol: str) -> bool:
        return any(symbol in s.symbols for s in self._snapshots)

    def resolve(
        self,
        symbol: str,
        *,
        venue: str = "binance",
        market: str = "um",
        order_type: str = "MARKET",
        window_start_ms: Optional[int] = None,
        window_end_ms: Optional[int] = None,
        observed_prices: Optional[Sequence[Any]] = None,
    ) -> InstrumentSpec:
        """给出该合约对**指定窗口**的规格与适用性判定。"""
        if not self._snapshots:
            return self._missing(symbol, venue, market, order_type,
                                 window_start_ms, window_end_ms,
                                 f"证据目录不存在或无 exchangeInfo 快照：{self.dir}")

        hits = [s for s in self._snapshots if symbol in s.symbols]
        if not hits:
            return self._missing(symbol, venue, market, order_type,
                                 window_start_ms, window_end_ms,
                                 f"全部 {len(self._snapshots)} 份 exchangeInfo 快照中均无该合约")

        latest = hits[-1]
        sym_doc = latest.symbols[symbol]
        try:
            fields = _spec_fields_from_filters(sym_doc, order_type)
        except InvalidOperation as exc:
            return self._missing(symbol, venue, market, order_type,
                                 window_start_ms, window_end_ms, f"规格字段非法：{exc}")

        onboard = sym_doc.get("onboardDate")
        onboard_ms = int(onboard) if onboard else None

        spec = InstrumentSpec(
            symbol=symbol, venue=venue, market=market,
            price_step=fields["price_step"],
            qty_step=fields["qty_step"],
            min_qty=fields["min_qty"],
            max_qty=fields["max_qty"],
            min_notional=fields["min_notional"],
            price_precision=fields["price_precision"],
            quantity_precision=fields["quantity_precision"],
            order_type=order_type.upper(),
            source_url=latest.url,
            source_kind="exchange_public_rest:exchangeInfo",
            fetched_at_ms=latest.server_ms,
            evidence_path=str(latest.path),
            evidence_sha256=latest.sha256,
            onboard_ms=onboard_ms,
            snapshot_server_ms=latest.server_ms,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
        )
        spec.validate()

        # ① 合约在该窗口是否存在
        if onboard_ms is not None and window_start_ms is not None and window_start_ms < onboard_ms:
            return _with(spec, APPLICABILITY_NOT_LISTED,
                         f"窗口起点 {window_start_ms} 早于合约上市时刻 {onboard_ms}")

        # ② 观察价证伪（只用真实成交/K 线价，**不用来推断 tick**）
        contra = _contradictions(observed_prices, spec.tick)
        if contra:
            return _with(spec, APPLICABILITY_CONTRADICTED,
                         f"窗口内 {len(contra)} 个真实价格不在 tick={spec.price_step} 网格上 ⇒ "
                         f"该快照不适用于本窗口", contradiction_samples=tuple(contra[:5]))

        # ③ 一致性包围：需要「早于窗口开始」与「晚于窗口结束」两份数值相同的快照
        if window_start_ms is not None and window_end_ms is not None:
            before = [s for s in hits if s.server_ms <= window_start_ms]
            after = [s for s in hits if s.server_ms >= window_end_ms]
            for b in before:
                for a in after:
                    if b is a:
                        continue
                    try:
                        fb = _spec_fields_from_filters(b.symbols[symbol], order_type)
                    except InvalidOperation:
                        continue
                    if _same_spec(fb, fields):
                        return _with(
                            spec, APPLICABILITY_VERIFIED,
                            f"被两份数值一致的快照包围（{b.saved_utc} ≤ 窗口 ≤ {a.saved_utc}）")

        # ④ 单点快照：只能证明「该时点如此」，证明不了「窗口内如此」
        note = (f"仅有单一时点快照（serverTime={latest.saved_utc}），"
                f"窗口 {window_start_ms}~{window_end_ms} 内无同源证据 ⇒ "
                f"不得据此声明历史成交已修正")
        if window_end_ms is not None and latest.server_ms < window_end_ms:
            note = (f"快照时点 {latest.saved_utc} 早于窗口结束 {window_end_ms} ⇒ "
                    f"窗口后段无证据")
        return _with(spec, APPLICABILITY_UNVERIFIED_HISTORICAL, note)

    def _missing(self, symbol, venue, market, order_type,
                 ws, we, note) -> InstrumentSpec:
        return InstrumentSpec(
            symbol=symbol, venue=venue, market=market,
            price_step="0", qty_step="0", min_qty="0", max_qty="0", min_notional="0",
            price_precision=0, quantity_precision=0, order_type=order_type.upper(),
            source_url="", source_kind="", fetched_at_ms=None,
            evidence_path=None, evidence_sha256=None, onboard_ms=None,
            snapshot_server_ms=None,
            applicability=APPLICABILITY_MISSING, applicability_note=note,
            formal_pass_allowed=False, window_start_ms=ws, window_end_ms=we,
        )


def _same_spec(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    keys = ("price_step", "qty_step", "min_qty", "max_qty", "min_notional")
    return all(_dec_str(a.get(k) or "0") == _dec_str(b.get(k) or "0") for k in keys)


def _with(spec: InstrumentSpec, applicability: str, note: str,
          contradiction_samples: Tuple[Dict[str, Any], ...] = ()) -> InstrumentSpec:
    return InstrumentSpec(
        **{**spec.__dict__,
           "applicability": applicability,
           "applicability_note": note,
           "formal_pass_allowed": applicability == APPLICABILITY_VERIFIED,
           "contradiction_samples": contradiction_samples}
    )


def _contradictions(prices: Optional[Sequence[Any]], tick: Decimal) -> List[Dict[str, Any]]:
    """真实价格是否落在 tick 整数网格上（只用于**证伪**）。

    容差取 1e-6 个 tick，仅用于吸收 parquet 里 float 的往返噪声；
    它不是「猜 tick」，而是「给定 tick 是否自洽」的必要条件检验。
    """
    if not prices or tick <= 0:
        return []
    tol = tick * Decimal("0.000001")
    out: List[Dict[str, Any]] = []
    for px in prices:
        try:
            d = to_decimal(px)
        except InvalidOperation:
            continue
        if d <= 0:
            continue
        q = d / tick
        nearest = q.to_integral_value(rounding="ROUND_HALF_UP")
        if abs(q - nearest) * tick > tol:
            out.append({"price": format(d, "f"), "tick": format(tick, "f"),
                        "quotient": format(q, "f")})
    return out


# ══════════════════════════ 单例 ══════════════════════════

_BOOK_CACHE: Dict[str, SpecBook] = {}


def get_spec_book(contracts_dir: Optional[Path] = None) -> SpecBook:
    key = str(Path(contracts_dir or os.environ.get("CONTRACTS_DIR") or DEFAULT_CONTRACTS_DIR))
    if key not in _BOOK_CACHE:
        _BOOK_CACHE[key] = SpecBook(Path(key))
    return _BOOK_CACHE[key]


def resolve_instrument_spec(
    symbol: str,
    *,
    window_start_ms: Optional[int] = None,
    window_end_ms: Optional[int] = None,
    venue: str = "binance",
    market: str = "um",
    order_type: str = "MARKET",
    observed_prices: Optional[Sequence[Any]] = None,
    contracts_dir: Optional[Path] = None,
) -> InstrumentSpec:
    """便捷入口：按标的 + 窗口解析规格。"""
    return get_spec_book(contracts_dir).resolve(
        symbol, venue=venue, market=market, order_type=order_type,
        window_start_ms=window_start_ms, window_end_ms=window_end_ms,
        observed_prices=observed_prices,
    )


__all__ = [
    "APPLICABILITY_VERIFIED", "APPLICABILITY_UNVERIFIED_HISTORICAL",
    "APPLICABILITY_CONTRADICTED", "APPLICABILITY_NOT_LISTED", "APPLICABILITY_MISSING",
    "DEFAULT_CONTRACTS_DIR",
    "to_decimal", "floor_to_step", "ceil_to_step", "round_price_to_tick",
    "InstrumentSpec", "SpecBook", "get_spec_book", "resolve_instrument_spec",
]
