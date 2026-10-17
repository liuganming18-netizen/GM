# -*- coding: utf-8 -*-
"""WX_EVENT_TREND_V0_1 · 交易所合约规格（tick / step / 最小量 / 最小名义额）。

为什么要这个模块
────────────────
修复前 `engine_adapter.py` 把 `price_step` 写死成 `0.01` 并套到**所有标的**上。
对价格 0.004~0.025 的 SPACE 来说，卖价向更便宜取整会落到 **0.0**（零元卖出）；
AIGENSYN / TRUTH / COTI 同样被砍掉 3~4 位有效小数。这不是「滑点」，是精度错误。

本模块的五条硬边界
──────────────────
1. **只认交易所原始响应**。规格取自 `exchangeInfo` 原始 JSON（`filters` 里的
   `PRICE_FILTER.tickSize` / `LOT_SIZE` / `MARKET_LOT_SIZE` / `MIN_NOTIONAL` /
   `NOTIONAL`），并记录：来源 URL、证据文件路径、证据 sha256、合约上市时刻。
2. **四类时间互不代替**（PRECISION-01 起）。请求获取时间 `request_fetched_ms`、
   文件保存时间 `file_saved_ms`、交易所响应时间 `snapshot_server_ms`、
   规格生效时间 `spec_effective_ms` 各自独立保存；**未知即 `None`**，
   绝不拿 serverTime 冒充 fetched_at，也不拿文件名时间冒充生效时间。
3. **当前规格 ≠ 历史规格**。判定历史窗口适用性时：
   · 需要一份「早于窗口开始」和一份「晚于窗口结束」的可定位快照把窗口夹住；
   · 夹住的区间内**每一份**快照都必须逐一比较过（两端相同不构成证明）；
   · 区间内不得有快照缺该合约（那是缺口，不是「没变」）；
   · **还必须有覆盖该窗口的规格变更证据**（`spec_changes_*.json`）。
   四条全满足才是 `verified`；任何一条不满足 ⇒ `unverified_historical`，
   `formal_pass_allowed=False`。**不得为凑通过而把未知历史规格升级为已验证。**
4. **禁止从 K 线小数位推断 tick**。K 线只用于**证伪**：若窗口内的真实价格
   不在该 tick 的整数网格上，则该快照对该窗口**确定不适用** ⇒ `contradicted`。
5. **数量侧按订单类型取全部适用过滤器，并验证结果满足每一条**。
   `LOT_SIZE` 恒适用；`MARKET_LOT_SIZE` 仅对 MARKET 单适用。
   多条步长同时适用时，合法数量必须同时是各步长的整数倍 ⇒ 有效步长取
   **最小公倍数**，而不是「取较大值」——`LOT=2 / MARKET=3` 取 3 会放过
   qty=3（不是 2 的倍数），这是本窗口修掉的第二个真实缺陷。
   `InstrumentSpec.check_quantity()` 逐条复核，不用「取更严的那一条」蒙混。

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
APPLICABILITY_VERIFIED = "verified"                    # 证据链完整 ⇒ 可用于正式回测
APPLICABILITY_UNVERIFIED_HISTORICAL = "unverified_historical"  # 有规格但证明不了当时适用
APPLICABILITY_CONTRADICTED = "contradicted"            # 观察价不在 tick 网格 ⇒ 确定不适用
APPLICABILITY_NOT_LISTED = "not_listed"                # 窗口早于合约上市
APPLICABILITY_MISSING = "missing"                      # 快照里没有该合约

# 参与「规格是否变化」比较的字段（也是变更证据必须覆盖的字段）
COMPARED_SPEC_FIELDS: Tuple[str, ...] = (
    "price_step", "qty_step", "min_qty", "max_qty", "min_notional",
)

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


def decimal_lcm(values: Sequence[Any]) -> Decimal:
    """十进制最小公倍数：同时满足多条步长约束的**最小**合法步长。

    为什么不是 max：`LOT_SIZE.stepSize=2` 与 `MARKET_LOT_SIZE.stepSize=3`
    同时适用时，合法数量必须同时是 2 和 3 的倍数 ⇒ 只能是 6 的倍数。
    取 max=3 会把 qty=3 当成合法，而 3 不是 2 的倍数 ⇒ 下单必被交易所拒绝。
    """
    ds = []
    for v in values:
        d = to_decimal(v).normalize()
        if d <= 0:
            raise InvalidOperation(f"step_must_be_positive:{v!r}")
        ds.append(d)
    if not ds:
        raise InvalidOperation("step_must_be_positive:empty")
    scale = 0
    for d in ds:
        e = d.as_tuple().exponent
        if isinstance(e, int) and e < 0:
            scale = max(scale, -e)
    ints = [int(d.scaleb(scale).to_integral_value()) for d in ds]
    l = 1
    for i in ints:
        l = l * i // math.gcd(l, i)
        if l > 10 ** 15:
            raise InvalidOperation("qty_constraints_incompatible:lcm_overflow")
    return Decimal(l).scaleb(-scale).normalize()


# ══════════════════════════ 规格对象 ══════════════════════════

@dataclass(frozen=True)
class InstrumentSpec:
    """一个合约在某个**证据时点**的规格 + 它对某个回测窗口的**适用性判定**。"""

    symbol: str
    venue: str
    market: str

    price_step: str            # 精确十进制文本，如 "0.0000010"
    qty_step: str              # **有效**数量步长 = 全部适用约束的最小公倍数
    min_qty: str
    max_qty: str
    min_notional: str
    price_precision: int
    quantity_precision: int
    order_type: str            # "MARKET" / "LIMIT"（决定 MARKET_LOT_SIZE 是否适用）

    source_url: str
    source_kind: str
    fetched_at_ms: Optional[int]      # 请求获取时间（= request_fetched_ms 的旧名，禁止填 serverTime）
    evidence_path: Optional[str]
    evidence_sha256: Optional[str]
    onboard_ms: Optional[int]
    snapshot_server_ms: Optional[int]  # 交易所响应时间（JSON 的 serverTime）

    applicability: str = APPLICABILITY_MISSING
    applicability_note: str = ""
    formal_pass_allowed: bool = False
    window_start_ms: Optional[int] = None
    window_end_ms: Optional[int] = None
    contradiction_samples: Tuple[Dict[str, Any], ...] = ()

    # ── PRECISION-01 新增：四类时间分开存（未知即 None，互不代替）──
    request_fetched_ms: Optional[int] = None   # 请求获取时间（meta.fetched_at_ms）
    file_saved_ms: Optional[int] = None        # 文件保存时间（文件名/meta.saved_utc）
    file_mtime_ms: Optional[int] = None        # 文件系统 mtime（诊断用，非证据）
    spec_effective_ms: Optional[int] = None    # 规格生效时间（变更记录给出；无则 None）

    # ── PRECISION-01 新增：证据链与判定依据 ──
    qty_constraints: Tuple[Dict[str, Any], ...] = ()   # 每条适用过滤器（含来源 filterType）
    coverage: Dict[str, Any] = field(default_factory=dict)
    evidence_chain: Tuple[Dict[str, Any], ...] = ()     # 每份被用到的快照 + 它在判定里的角色
    change_evidence: Tuple[Dict[str, Any], ...] = ()    # 变更证据（每条含 source_url/sha256）

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

    # ── 数量约束逐条复核（要求 4）──
    def applicable_qty_constraints(self) -> List[Dict[str, Any]]:
        """返回该订单类型下**同时适用**的数量约束。

        没有 `qty_constraints` 明细时（旧构造路径/测试夹具），退化为一条合成约束，
        保证 `check_quantity` 永远可调用。
        """
        if self.qty_constraints:
            return [dict(c) for c in self.qty_constraints]
        return [{
            "filter": "EFFECTIVE", "applies": "always",
            "step": self.qty_step, "min": self.min_qty, "max": self.max_qty,
            "notional": self.min_notional,
        }]

    def check_quantity(self, qty: Any, price: Any = None) -> Dict[str, Any]:
        """**逐条**验证数量是否满足全部适用约束（不是「取最严一条」）。

        返回 `{"ok": bool, "qty": str, "checks": [...], "violations": [...]}`，
        `checks` 里每一项都标明是哪条 filterType 的哪一项要求、通过与否。
        """
        q = to_decimal(qty, field_name="qty")
        checks: List[Dict[str, Any]] = []
        violations: List[Dict[str, Any]] = []

        def _push(c: Dict[str, Any]) -> None:
            checks.append(c)
            if not c["ok"]:
                violations.append(c)

        for con in self.applicable_qty_constraints():
            name = str(con.get("filter") or "UNKNOWN")
            step_raw = con.get("step") or ""
            if step_raw:
                step = to_decimal(step_raw, field_name="qty_step")
                if step > 0:
                    ratio = q / step
                    ok = ratio == ratio.to_integral_value()
                    _push({"filter": name, "requirement": "stepSize", "step": _dec_str(step),
                           "ok": ok, "quotient": _dec_str(ratio)})
            min_raw = con.get("min") or ""
            if min_raw:
                mn = to_decimal(min_raw, field_name="min_qty")
                _push({"filter": name, "requirement": "minQty", "min": _dec_str(mn),
                       "ok": q >= mn})
            max_raw = con.get("max") or ""
            if max_raw:
                mx = to_decimal(max_raw, field_name="max_qty")
                if mx > 0:
                    _push({"filter": name, "requirement": "maxQty", "max": _dec_str(mx),
                           "ok": q <= mx})
            notional_raw = con.get("notional") or ""
            if notional_raw and price is not None:
                mn_n = to_decimal(notional_raw, field_name="min_notional")
                if mn_n > 0:
                    p = to_decimal(price, field_name="price")
                    _push({"filter": name, "requirement": "minNotional",
                           "min_notional": _dec_str(mn_n),
                           "notional": _dec_str(q * p), "ok": q * p >= mn_n})

        return {"ok": not violations, "qty": _dec_str(q),
                "checks": checks, "violations": violations}

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["contradiction_samples"] = [dict(x) for x in self.contradiction_samples]
        d["qty_constraints"] = [dict(x) for x in self.qty_constraints]
        d["evidence_chain"] = [dict(x) for x in self.evidence_chain]
        d["change_evidence"] = [dict(x) for x in self.change_evidence]
        d["coverage"] = dict(self.coverage or {})
        d["blocking"] = self.blocking
        return d

    def identity(self) -> Dict[str, Any]:
        """只保留影响撮合数值的字段（用于 run 身份/指纹）。

        **故意不含** `evidence_chain` / `coverage` / `change_evidence`：
        它们是判定过程，不是撮合数值；把它们塞进指纹会让 375 个历史 run
        的指纹全部漂移，且不改变任何成交结果。
        """
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


def _qty_constraints_of(sym_doc: Dict[str, Any], order_type: str) -> List[Dict[str, Any]]:
    """列出该订单类型下**同时适用**的数量约束（要求 4）。

    · `LOT_SIZE` 对任何订单都适用（交易所对所有单校验）；
    · `MARKET_LOT_SIZE` **只对 MARKET 单**适用 —— LIMIT 单套用它会平白收紧上限；
    · `MIN_NOTIONAL` / `NOTIONAL` 取两者中更严的一条（新 API 用 NOTIONAL）。
    """
    fl = _filters_of(sym_doc)
    ot = (order_type or "MARKET").upper()

    def _num(d: Dict[str, Any], key: str) -> Optional[str]:
        v = d.get(key)
        if v is None or v == "":
            return None
        return _dec_str(v)

    out: List[Dict[str, Any]] = []
    lot = fl.get("LOT_SIZE") or {}
    if lot:
        out.append({"filter": "LOT_SIZE", "applies": "always",
                    "step": _num(lot, "stepSize") or "",
                    "min": _num(lot, "minQty") or "",
                    "max": _num(lot, "maxQty") or ""})
    mkt = fl.get("MARKET_LOT_SIZE") or {}
    if mkt and ot == "MARKET":
        out.append({"filter": "MARKET_LOT_SIZE", "applies": "market_only",
                    "step": _num(mkt, "stepSize") or "",
                    "min": _num(mkt, "minQty") or "",
                    "max": _num(mkt, "maxQty") or ""})

    notionals: List[Tuple[str, str]] = []
    for fname, key in (("MIN_NOTIONAL", "notional"), ("NOTIONAL", "notional")):
        f = fl.get(fname) or {}
        v = _num(f, key)
        if v:
            notionals.append((fname, v))
    if notionals:
        # 两条同时存在时，合法订单必须同时满足 ⇒ 取较大者（这里取大是**对的**：
        # 「≥a 且 ≥b」等价于「≥max(a,b)」，与步长取 lcm 不同）
        fname, v = max(notionals, key=lambda x: to_decimal(x[1]))
        out.append({"filter": fname, "applies": "always", "notional": v})
    return out


def _spec_fields_from_filters(sym_doc: Dict[str, Any], order_type: str) -> Dict[str, Any]:
    """从 exchangeInfo 的 filters 取规格 + 每条约束的来源。

    数量侧的 `qty_step` 是**全部适用约束的最小公倍数**（见 `decimal_lcm`），
    不是「取较大值」；`min_qty` 取各约束最大值后向上对齐到有效步长；
    `max_qty` 取各约束最小值后向下对齐到有效步长（0 表示无上限）。
    """
    fl = _filters_of(sym_doc)
    pf = fl.get("PRICE_FILTER") or {}

    def _num(d: Dict[str, Any], key: str) -> Optional[str]:
        v = d.get(key)
        if v is None or v == "":
            return None
        return _dec_str(v)

    tick = _num(pf, "tickSize")
    if tick is None:
        raise InvalidOperation(f"missing_tick_size:{sym_doc.get('symbol')}")

    cons = _qty_constraints_of(sym_doc, order_type)
    steps = [c["step"] for c in cons if c.get("step")]
    if not steps:
        raise InvalidOperation(f"missing_step_size:{sym_doc.get('symbol')}")
    step_eff = decimal_lcm(steps)

    mins = [to_decimal(c["min"]) for c in cons if c.get("min")]
    min_eff = ceil_to_step(max(mins), step_eff) if mins else Decimal(0)

    maxs = [to_decimal(c["max"]) for c in cons if c.get("max") and to_decimal(c["max"]) > 0]
    if maxs:
        max_eff = floor_to_step(min(maxs), step_eff)
        if max_eff <= 0:
            raise InvalidOperation(
                f"qty_constraints_incompatible:{sym_doc.get('symbol')}:"
                f"step={_dec_str(step_eff)}>max={_dec_str(min(maxs))}")
    else:
        max_eff = Decimal(0)

    if max_eff > 0 and min_eff > max_eff:
        raise InvalidOperation(
            f"qty_constraints_incompatible:{sym_doc.get('symbol')}:"
            f"min={_dec_str(min_eff)}>max={_dec_str(max_eff)}")

    notionals = [c["notional"] for c in cons if c.get("notional")]
    min_notional = _dec_str(max(to_decimal(x) for x in notionals)) if notionals else "0"

    return {
        "price_step": tick,
        "qty_step": _dec_str(step_eff),
        "min_qty": _dec_str(min_eff),
        "max_qty": _dec_str(max_eff),
        "min_notional": min_notional,
        "price_precision": int(sym_doc.get("pricePrecision") or 0),
        "quantity_precision": int(sym_doc.get("quantityPrecision") or 0),
        # 来源与签名（用于跨快照比较；provenance 不同也算「规格变了」）
        "filter_sources": ";".join(
            f"{c['filter']}:{c.get('step') or c.get('notional') or ''}" for c in cons),
        "qty_constraints": tuple(dict(c) for c in cons),
    }


def _parse_iso_ms(text: str) -> Optional[int]:
    """`2026-10-05T045328Z` / `2026-10-05T04:53:28Z` → 毫秒。失败返回 None（不猜）。"""
    import datetime as _dt
    s = str(text or "").strip()
    if not s:
        return None
    s = s.rstrip("Z")
    for fmt in ("%Y-%m-%dT%H%M%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%d_%H%M%S", "%Y-%m-%d"):
        try:
            dt = _dt.datetime.strptime(s, fmt).replace(tzinfo=_dt.timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None


def _load_meta_index(directory: Path) -> Dict[str, Dict[str, Any]]:
    """扫目录里的 `*meta*.json`，建 `文件名 → {fetched_at_ms, saved_utc}` 索引。

    这是「请求获取时间」的**唯一**来源。没有 meta 就是 None —— 绝不用
    文件名时间或 serverTime 顶替。
    """
    idx: Dict[str, Dict[str, Any]] = {}

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            fname = node.get("file")
            if isinstance(fname, str) and fname:
                slot = idx.setdefault(fname, {})
                for k in ("fetched_at_ms", "saved_utc", "request_id", "source", "url"):
                    if node.get(k) not in (None, "") and slot.get(k) in (None, ""):
                        slot[k] = node[k]
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    for p in sorted(directory.glob("*meta*.json")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                _walk(json.load(f))
        except Exception:
            continue
    return idx


@dataclass(frozen=True)
class _Snapshot:
    path: Path
    sha256: str
    server_ms: Optional[int]        # 交易所响应时间；None ⇒ 无法定位，不能参与包围
    saved_ms: Optional[int]         # 文件保存时间（文件名/meta）
    request_ms: Optional[int]       # 请求获取时间（meta）
    mtime_ms: Optional[int]         # 文件系统 mtime（诊断）
    url: str
    symbols: Dict[str, Dict[str, Any]] = field(repr=False, default_factory=dict)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def positioned(self) -> bool:
        return self.server_ms is not None

    def time_record(self) -> Dict[str, Any]:
        return {
            "path": str(self.path), "sha256": self.sha256,
            "request_fetched_ms": self.request_ms,
            "file_saved_ms": self.saved_ms,
            "snapshot_server_ms": self.server_ms,
            "file_mtime_ms": self.mtime_ms,
        }


@dataclass(frozen=True)
class SpecChangeRecord:
    """规格变更证据（交易所公告 / 人工核验记录）。

    `kind="change"`      ⇒ 该合约在 `effective_ms` 生效了一次变更；
    `kind="no_change"`   ⇒ 明确记载 `[from_ms, to_ms]` 内**没有**变更。
    没有 `no_change` 记录，就无法排除「窗口内改过又改回来」⇒ 不能 verified。

    三个时间刻意分开：`announced_ms`（公告/获取时间）、`effective_ms`（交易所
    公布的生效时间）、`from_ms/to_ms`（无变更区间的边界）。**公告时间不能代替
    生效时间**：公告早于窗口、生效落在窗口内的情况是真实存在的。
    """
    symbol: str
    kind: str
    effective_ms: Optional[int]
    from_ms: Optional[int]
    to_ms: Optional[int]
    fields: Tuple[str, ...]
    source_url: str
    evidence_path: Optional[str]
    evidence_sha256: Optional[str]
    note: str = ""
    announced_ms: Optional[int] = None

    def affects(self, fields: Iterable[str]) -> bool:
        if not self.fields:
            return True
        return bool(set(self.fields) & set(fields))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "kind": self.kind,
            "effective_ms": self.effective_ms,
            "announced_ms": self.announced_ms,
            "from_ms": self.from_ms, "to_ms": self.to_ms,
            "fields": list(self.fields), "source_url": self.source_url,
            "evidence_path": self.evidence_path,
            "evidence_sha256": self.evidence_sha256, "note": self.note,
        }


class SpecBook:
    """交易所规格簿：装载证据目录下的全部 `exchangeInfo_*.json` + 变更证据。

    只读、确定性：同一目录同一批文件 ⇒ 同一结果（与文件枚举顺序无关）。
    """

    def __init__(self, contracts_dir: Optional[Path] = None):
        self.dir = Path(contracts_dir or os.environ.get("CONTRACTS_DIR") or DEFAULT_CONTRACTS_DIR)
        self._snapshots: List[_Snapshot] = []
        self._changes: List[SpecChangeRecord] = []
        self._meta_index: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.dir.exists():
            return
        self._meta_index = _load_meta_index(self.dir)
        for p in sorted(self.dir.glob("exchangeInfo_futures_*.json")):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    doc = json.load(f)
            except Exception:
                continue
            syms = {s.get("symbol"): s for s in (doc.get("symbols") or []) if s.get("symbol")}
            if not syms:
                continue
            meta = self._meta_index.get(p.name) or {}
            raw_server = doc.get("serverTime")
            try:
                server_ms = int(raw_server) if raw_server not in (None, "", 0) else None
            except (TypeError, ValueError):
                server_ms = None
            saved_ms = _parse_iso_ms(str(p.stem).split("_")[-1])
            meta_saved = _parse_iso_ms(str(meta.get("saved_utc") or ""))
            if saved_ms is None:
                saved_ms = meta_saved
            request_ms = meta.get("fetched_at_ms")
            request_ms = int(request_ms) if isinstance(request_ms, (int, float)) else None
            try:
                mtime_ms = int(p.stat().st_mtime * 1000)
            except OSError:
                mtime_ms = None
            self._snapshots.append(_Snapshot(
                path=p, sha256=_sha256_file(p),
                server_ms=server_ms, saved_ms=saved_ms, request_ms=request_ms,
                mtime_ms=mtime_ms,
                url="https://fapi.binance.com/fapi/v1/exchangeInfo",
                symbols=syms,
            ))
        # 排序键只用「交易所响应时间 + sha256」：与文件名/枚举顺序无关，
        # 因此「快照顺序扰动」不改变判定结果。
        self._snapshots.sort(key=lambda s: (s.server_ms is None, s.server_ms or 0, s.sha256))
        self._changes = self._load_changes()

    def _load_changes(self) -> List[SpecChangeRecord]:
        out: List[SpecChangeRecord] = []
        for p in sorted(self.dir.glob("spec_changes_*.json")):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    doc = json.load(f)
            except Exception:
                continue
            sha = _sha256_file(p)
            for rec in (doc.get("records") or []):
                kind = str(rec.get("kind") or "").strip()
                if kind not in ("change", "no_change"):
                    continue
                fields = tuple(rec.get("fields") or ())
                out.append(SpecChangeRecord(
                    symbol=str(rec.get("symbol") or ""),
                    kind=kind,
                    effective_ms=rec.get("effective_ms"),
                    from_ms=rec.get("from_ms"),
                    to_ms=rec.get("to_ms"),
                    fields=fields,
                    source_url=str(rec.get("source_url") or ""),
                    evidence_path=str(p),
                    evidence_sha256=str(rec.get("evidence_sha256") or sha),
                    note=str(rec.get("note") or ""),
                    announced_ms=rec.get("announced_ms"),
                ))
        return out

    # ── 查询 ──
    @property
    def snapshots(self) -> List[_Snapshot]:
        return list(self._snapshots)

    @property
    def change_records(self) -> List[SpecChangeRecord]:
        return list(self._changes)

    def has_symbol(self, symbol: str) -> bool:
        return any(symbol in s.symbols for s in self._snapshots)

    # ══════════════ 主判定 ══════════════
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

        positioned = [s for s in self._snapshots if s.positioned]
        hits = [s for s in positioned if symbol in s.symbols]
        if not hits:
            note = (f"全部 {len(self._snapshots)} 份 exchangeInfo 快照中均无该合约"
                    if positioned else
                    f"{len(self._snapshots)} 份快照均缺 serverTime，无法定位证据时点")
            return self._missing(symbol, venue, market, order_type,
                                 window_start_ms, window_end_ms, note)

        anchor, anchor_note = self._select_anchor(hits, window_end_ms)
        sym_doc = anchor.symbols[symbol]
        try:
            fields = _spec_fields_from_filters(sym_doc, order_type)
        except InvalidOperation as exc:
            return self._missing(symbol, venue, market, order_type,
                                 window_start_ms, window_end_ms, f"规格字段非法：{exc}")

        onboard = sym_doc.get("onboardDate")
        try:
            onboard_ms = int(onboard) if onboard else None
        except (TypeError, ValueError):
            onboard_ms = None

        spec = _make_spec(anchor, fields, symbol=symbol, venue=venue, market=market,
                          order_type=order_type, onboard_ms=onboard_ms,
                          window_start_ms=window_start_ms, window_end_ms=window_end_ms)
        spec.validate()

        # ① 合约在该窗口是否存在
        if onboard_ms is not None and window_start_ms is not None and window_start_ms < onboard_ms:
            return _with(spec, APPLICABILITY_NOT_LISTED,
                         f"窗口起点 {window_start_ms} 早于合约上市时刻 {onboard_ms}",
                         evidence_chain=(_chain_entry(anchor, "anchor", fields),))

        # ② 观察价证伪（只用真实成交/K 线价，**不用来推断 tick**）
        contra = _contradictions(observed_prices, spec.tick)
        if contra:
            return _with(spec, APPLICABILITY_CONTRADICTED,
                         f"窗口内 {len(contra)} 个真实价格不在 tick={spec.price_step} 网格上 ⇒ "
                         f"该快照不适用于本窗口", contradiction_samples=tuple(contra[:5]),
                         evidence_chain=(_chain_entry(anchor, "anchor", fields),))

        # ③ 证据链判定（包围 + 逐份比较 + 变更证据）
        cov = self._coverage_verdict(
            symbol=symbol, order_type=order_type,
            window_start_ms=window_start_ms, window_end_ms=window_end_ms,
            anchor=anchor, anchor_fields=fields)

        # 窗口内规格不唯一时，数值改取「窗口起点时的规格」：
        # 报告锚点（窗口结束前最近）会把窗口后半段的临时规格当成整窗规格，
        # 比报告起点规格更误导。两种情形都 blocked，这里只影响诊断数值。
        src = cov.get("numeric_source")
        if src is not None and src.sha256 != anchor.sha256:
            try:
                fields2 = _spec_fields_from_filters(src.symbols[symbol], order_type)
                spec2 = _make_spec(src, fields2, symbol=symbol, venue=venue, market=market,
                                   order_type=order_type, onboard_ms=onboard_ms,
                                   window_start_ms=window_start_ms, window_end_ms=window_end_ms)
                spec2.validate()
                spec, fields = spec2, fields2
            except InvalidOperation:
                pass      # 取不到就保留原锚点数值，note 里已说明窗口内规格不唯一

        eff = self._spec_effective_ms(symbol, window_start_ms)
        note = anchor_note + cov["note"] if anchor_note else cov["note"]
        if cov["verdict"] == APPLICABILITY_VERIFIED:
            return _with(spec, APPLICABILITY_VERIFIED, note,
                         evidence_chain=tuple(cov["chain"]), coverage=cov,
                         change_evidence=tuple(cov.get("change_evidence") or ()),
                         spec_effective_ms=eff)
        return _with(spec, APPLICABILITY_UNVERIFIED_HISTORICAL, note,
                     evidence_chain=tuple(cov["chain"]), coverage=cov,
                     change_evidence=tuple(cov.get("change_evidence") or ()),
                     spec_effective_ms=eff)

    # ── 锚点选择 ──
    @staticmethod
    def _select_anchor(hits: List[_Snapshot],
                       window_end_ms: Optional[int]) -> Tuple[_Snapshot, str]:
        """选「最贴近窗口」的那份快照作为规格数值来源。

        规则：取 `serverTime <= 窗口结束` 中**最晚**的一份（窗口结束时已知的规格）；
        没有则取最早的一份（未来证据，弱），并在 note 里写明。
        旧实现直接取「全局最新」，会把窗口之后才改的规格套到历史窗口上。
        """
        if window_end_ms is None:
            return hits[-1], ""
        past = [s for s in hits if s.server_ms is not None and s.server_ms <= window_end_ms]
        if past:
            a = past[-1]
            if a.server_ms != hits[-1].server_ms:
                return a, (f"规格数值取自窗口结束前最近的快照 {a.name}"
                           f"（serverTime={a.server_ms}），不是全局最新快照。")
            return a, ""
        a = hits[0]
        return a, (f"窗口结束早于全部快照 ⇒ 规格数值取自最早的未来快照 {a.name}"
                   f"（serverTime={a.server_ms}），仅作参考，不构成窗口内证据。")

    # ── 变更证据 ──
    def _spec_effective_ms(self, symbol: str, window_start_ms: Optional[int]) -> Optional[int]:
        if window_start_ms is None:
            return None
        cands = [r for r in self._changes
                 if r.symbol == symbol and r.kind == "change"
                 and r.effective_ms is not None and r.effective_ms <= window_start_ms]
        if not cands:
            return None
        return max(int(r.effective_ms) for r in cands)

    def _coverage_verdict(self, *, symbol: str, order_type: str,
                          window_start_ms: Optional[int], window_end_ms: Optional[int],
                          anchor: _Snapshot, anchor_fields: Dict[str, Any]) -> Dict[str, Any]:
        """历史窗口适用性判定（要求 1/2/3）。

        返回 `{"verdict","reason","note","chain","change_evidence"}`。
        `chain` 逐条列出「哪份快照、扮演什么角色、和锚点比是否相同」。
        """
        chain: List[Dict[str, Any]] = []
        cache: Dict[str, Optional[Dict[str, Any]]] = {}

        def fields_of(s: _Snapshot) -> Optional[Dict[str, Any]]:
            key = s.sha256
            if key not in cache:
                try:
                    cache[key] = _spec_fields_from_filters(s.symbols[symbol], order_type)
                except (InvalidOperation, KeyError):
                    cache[key] = None
            return cache[key]

        def fail(code: str, note: str, numeric_source=None) -> Dict[str, Any]:
            return {"verdict": APPLICABILITY_UNVERIFIED_HISTORICAL, "reason": code,
                    "note": note, "chain": chain, "change_evidence": [],
                    "numeric_source": numeric_source}

        chain.append(_chain_entry(anchor, "anchor", anchor_fields, same=True))

        if window_start_ms is None or window_end_ms is None:
            return fail("window_bounds_missing",
                        "未给出窗口起止 ⇒ 无法证明窗口内规格，保持 unverified_historical")
        if window_start_ms >= window_end_ms:
            return fail("window_bounds_invalid",
                        f"窗口非法（start={window_start_ms} >= end={window_end_ms}）")

        positioned = [s for s in self._snapshots if s.positioned]
        unpositioned = [s for s in self._snapshots if not s.positioned]
        for s in unpositioned:
            chain.append(_chain_entry(s, "unpositioned", None, same=None,
                                      note="缺 serverTime ⇒ 无法定位，不能用于排除窗口内变更"))
        if unpositioned:
            return fail("snapshot_without_server_time",
                        f"{len(unpositioned)} 份快照缺交易所响应时间（serverTime），"
                        f"无法定位其证据时点 ⇒ 不能声称窗口内未变更")

        # 窗口内缺该合约 ⇒ 是缺口，不是「没变」
        absent = [s for s in positioned
                  if symbol not in s.symbols
                  and window_start_ms <= s.server_ms <= window_end_ms]
        for s in absent:
            chain.append(_chain_entry(s, "absent", None, same=None,
                                      note="该时点快照无此合约"))
        if absent:
            return fail("symbol_absent_inside_window",
                        f"窗口内有 {len(absent)} 份快照缺该合约（如 {absent[0].name}）⇒ "
                        f"规格链断裂，不能声称整窗适用")

        hits = [s for s in positioned if symbol in s.symbols]
        before = [s for s in hits if s.server_ms <= window_start_ms]
        after = [s for s in hits if s.server_ms >= window_end_ms]
        if not before:
            return fail("no_snapshot_before_window_start",
                        f"没有任何快照的 serverTime 早于窗口开始 {window_start_ms} ⇒ "
                        f"窗口起点之前无证据")
        if not after:
            return fail("no_snapshot_after_window_end",
                        f"没有任何快照的 serverTime 晚于窗口结束 {window_end_ms} ⇒ "
                        f"窗口后段无证据（现有快照最早 serverTime={hits[0].server_ms}）")

        b, a = before[-1], after[0]
        sig_anchor = _spec_signature(anchor_fields)
        fb, fa = fields_of(b), fields_of(a)
        chain.append(_chain_entry(
            b, "bracket_before", fb,
            same=(None if fb is None else _spec_signature(fb) == sig_anchor),
            note="窗口开始之前最近的证据"))
        chain.append(_chain_entry(
            a, "bracket_after", fa,
            same=(None if fa is None else _spec_signature(fa) == sig_anchor),
            note="窗口结束之后最早的证据"))
        if fb is None or fa is None:
            return fail("spec_unreadable_at_endpoint",
                        "夹住窗口的快照里规格字段缺失/非法 ⇒ 无法比较")
        if _spec_signature(fb) != _spec_signature(fa):
            return fail("spec_changed_between_endpoints",
                        f"窗口两端规格不同（{b.name} vs {a.name}）⇒ 窗口内发生过变更，"
                        f"单一规格不能代表整窗")

        # 锚点（窗口结束前最近）与窗口两端不一致 ⇒ 窗口内规格不唯一。
        # 数值改取「窗口起点时的规格」，并把这件事写进 reason。
        numeric_source = None
        if sig_anchor != _spec_signature(fa):
            numeric_source = b

        # 逐份比较 [b, a] 内的**每一份**快照（两端相同不构成证明）
        span = [s for s in hits if b.server_ms <= s.server_ms <= a.server_ms]
        seen_sha = set()
        for s in span:
            if s.sha256 in seen_sha:
                continue
            seen_sha.add(s.sha256)
            if s.sha256 in (b.sha256, a.sha256):
                continue          # 两端已在 chain 里作为 bracket 记录，且已比较过
            fs = fields_of(s)
            if fs is None:
                chain.append(_chain_entry(s, "intermediate", None, same=None,
                                          note="规格字段缺失/非法"))
                return fail("spec_unreadable_inside_span",
                            f"夹住区间的快照 {s.name} 规格字段缺失/非法 ⇒ 无法排除变更",
                            numeric_source=numeric_source)
            same = _spec_signature(fs) == _spec_signature(fa)
            role = "anchor_in_span" if s.sha256 == anchor.sha256 else "intermediate"
            chain.append(_chain_entry(s, role, fs, same=same,
                                      note="夹住区间内的中间快照，逐一与两端比较"))
            if not same:
                return fail("spec_changed_inside_span",
                            f"夹住区间内的快照 {s.name} 规格与两端不同 ⇒ "
                            f"「两端相同」不能证明中间未变更；"
                            f"窗口内规格不唯一，数值取窗口起点时的规格（{b.name}），"
                            f"仅作诊断，不得用于正式回测",
                            numeric_source=b)

        # 重复响应时点提示：同一 serverTime 的多个文件不是两个时点
        dup_groups: Dict[int, List[str]] = {}
        for s in span:
            dup_groups.setdefault(int(s.server_ms), []).append(s.name)
        dup_note = ""
        dups = {k: v for k, v in dup_groups.items() if len(v) > 1}
        if dups:
            dup_note = ("；注意 " + "、".join(
                f"{len(v)} 份快照 serverTime 同为 {k}（{'/'.join(v)}）"
                for k, v in dups.items()) + " —— 同一响应存多份，不构成两个时点")

        # ── 变更证据（要求 2：没有变更证据就不能 verified）──
        recs = [r for r in self._changes if r.symbol == symbol]
        valid = [r for r in recs if r.source_url and r.evidence_sha256
                 and len(str(r.evidence_sha256)) == 64]
        dropped = [r for r in recs if r not in valid]
        if dropped:
            chain.append({"role": "change_evidence_dropped",
                          "note": f"{len(dropped)} 条变更记录缺 source_url/evidence_sha256，"
                                  f"不作为证据", "path": dropped[0].evidence_path})
        if not valid:
            return fail("no_change_evidence",
                        f"缺少覆盖窗口的规格变更证据（spec_changes_*.json 无有效记录）⇒ "
                        f"两端相同仍可能是「改过又改回」，不能 verified{dup_note}")

        unknown_eff = [r for r in valid
                       if r.kind == "change" and r.effective_ms is None
                       and r.affects(COMPARED_SPEC_FIELDS)]
        if unknown_eff:
            return fail("change_effective_time_unknown",
                        f"{len(unknown_eff)} 条变更记录只有公告时间"
                        f"（announced_ms={unknown_eff[0].announced_ms}）、没有生效时间 ⇒ "
                        f"无法排除其落在窗口内（公告/获取时间不能代替生效时间）{dup_note}")

        inside = [r for r in valid
                  if r.kind == "change" and r.effective_ms is not None
                  and window_start_ms <= r.effective_ms <= window_end_ms
                  and r.affects(COMPARED_SPEC_FIELDS)]
        if inside:
            return fail("change_inside_window",
                        f"窗口内有 {len(inside)} 次规格变更（最早生效 {inside[0].effective_ms}）⇒ "
                        f"单一规格不能代表整窗{dup_note}")

        covering = [r for r in valid
                    if r.kind == "no_change"
                    and r.from_ms is not None and r.to_ms is not None
                    and r.from_ms <= window_start_ms and r.to_ms >= window_end_ms
                    and r.affects(COMPARED_SPEC_FIELDS)]
        if not covering:
            return fail("change_evidence_does_not_cover_window",
                        f"变更证据没有覆盖整个窗口 "
                        f"[{window_start_ms}, {window_end_ms}] ⇒ 窗口内仍可能变更{dup_note}")

        ev = [r.to_dict() for r in (covering + inside)]
        return {
            "verdict": APPLICABILITY_VERIFIED,
            "reason": "bracketed_and_change_evidence_covers_window",
            "note": (f"被两份一致快照夹住（{b.name} ≤ 窗口 ≤ {a.name}），"
                     f"区间内 {len(seen_sha)} 份快照逐一比较一致，"
                     f"且有变更证据覆盖整窗{dup_note}"),
            "chain": chain,
            "change_evidence": ev,
            "bracket": {"before": b.time_record(), "after": a.time_record()},
            "compared_snapshots": sorted(seen_sha),
            "numeric_source": None,
        }

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


def _make_spec(snap: _Snapshot, fields: Dict[str, Any], *, symbol: str, venue: str,
               market: str, order_type: str, onboard_ms: Optional[int],
               window_start_ms: Optional[int],
               window_end_ms: Optional[int]) -> InstrumentSpec:
    """按某份快照装配规格对象（四类时间全部来自该快照，不跨快照拼）。"""
    return InstrumentSpec(
        symbol=symbol, venue=venue, market=market,
        price_step=fields["price_step"],
        qty_step=fields["qty_step"],
        min_qty=fields["min_qty"],
        max_qty=fields["max_qty"],
        min_notional=fields["min_notional"],
        price_precision=fields["price_precision"],
        quantity_precision=fields["quantity_precision"],
        order_type=order_type.upper(),
        source_url=snap.url,
        source_kind="exchange_public_rest:exchangeInfo",
        fetched_at_ms=snap.request_ms,        # 请求获取时间；无 meta ⇒ None
        evidence_path=str(snap.path),
        evidence_sha256=snap.sha256,
        onboard_ms=onboard_ms,
        snapshot_server_ms=snap.server_ms,
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
        request_fetched_ms=snap.request_ms,
        file_saved_ms=snap.saved_ms,
        file_mtime_ms=snap.mtime_ms,
        qty_constraints=tuple(fields["qty_constraints"]),
    )


def _spec_signature(fields: Optional[Dict[str, Any]]) -> str:
    """跨快照比较用的规格签名（数值 + 约束来源都要一致）。"""
    if not fields:
        return "<none>"
    body = "|".join(f"{k}={_dec_str(fields.get(k) or '0')}" for k in COMPARED_SPEC_FIELDS)
    return f"{body}|src={fields.get('filter_sources') or ''}"


def _same_spec(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """保留的旧接口：按签名比较两份规格字段是否等价。"""
    return _spec_signature(a) == _spec_signature(b)


def _chain_entry(snap: _Snapshot, role: str, fields: Optional[Dict[str, Any]],
                 *, same: Optional[bool] = None, note: str = "") -> Dict[str, Any]:
    d = snap.time_record()
    d.update({
        "role": role,
        "spec_signature": _spec_signature(fields) if fields is not None else None,
        "same_as_anchor": same,
    })
    if note:
        d["note"] = note
    return d


def _with(spec: InstrumentSpec, applicability: str, note: str,
          contradiction_samples: Tuple[Dict[str, Any], ...] = (),
          evidence_chain: Tuple[Dict[str, Any], ...] = (),
          coverage: Optional[Dict[str, Any]] = None,
          change_evidence: Tuple[Dict[str, Any], ...] = (),
          spec_effective_ms: Optional[int] = None) -> InstrumentSpec:
    return InstrumentSpec(
        **{**spec.__dict__,
           "applicability": applicability,
           "applicability_note": note,
           "formal_pass_allowed": applicability == APPLICABILITY_VERIFIED,
           "contradiction_samples": contradiction_samples,
           "evidence_chain": tuple(evidence_chain),
           "coverage": dict(coverage or {}),
           "change_evidence": tuple(change_evidence),
           "spec_effective_ms": spec_effective_ms}
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
    """便捷入口：按标的 + 窗口解析规格（签名与旧版逐字一致，未改调用接口）。"""
    return get_spec_book(contracts_dir).resolve(
        symbol, venue=venue, market=market, order_type=order_type,
        window_start_ms=window_start_ms, window_end_ms=window_end_ms,
        observed_prices=observed_prices,
    )


__all__ = [
    "APPLICABILITY_VERIFIED", "APPLICABILITY_UNVERIFIED_HISTORICAL",
    "APPLICABILITY_CONTRADICTED", "APPLICABILITY_NOT_LISTED", "APPLICABILITY_MISSING",
    "COMPARED_SPEC_FIELDS",
    "DEFAULT_CONTRACTS_DIR",
    "to_decimal", "floor_to_step", "ceil_to_step", "round_price_to_tick",
    "decimal_lcm",
    "InstrumentSpec", "SpecBook", "SpecChangeRecord",
    "get_spec_book", "resolve_instrument_spec",
]
