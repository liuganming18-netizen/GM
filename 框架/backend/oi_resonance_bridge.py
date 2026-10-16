# -*- coding: utf-8 -*-
"""WX_OI_RESONANCE_V1 候选包 ⇄ 后端 桥（只读加载 + 契约常量 + 参数白名单）。

纪律
────
· 本文件**不含任何策略/风控数值**——阈值唯一来源是候选包 `config.SIGNAL_FROZEN` /
  `config.EXEC_FROZEN`。
· 候选包或 numpy/pandas 不可用 ⇒ **不静默降级**：`AVAILABLE=False` + `IMPORT_ERROR`
  如实暴露，调用方据此把回测置 `failed` 并留原因，绝不退回「某套默认参数」。
· 只读引用：不写任何库、不下单、不联网。

身份隔离（不可回退）
────────────────────
本桥**只**加载 `策略/WyckoffMaster/oi_resonance_v1`（新候选），**不**加载
`策略/WyckoffMaster/execution.py`（旧 v3.1 共用核心，`exec_bridge` 的职责）。
旧 3a–3d / M0–M6 成绩一律不继承。
"""
from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(__file__).resolve().parent
STRATEGY_DIR = PROJECT_ROOT / "策略"
PACKAGE_DIR = STRATEGY_DIR / "WyckoffMaster" / "oi_resonance_v1"
if str(STRATEGY_DIR) not in sys.path:
    sys.path.insert(0, str(STRATEGY_DIR))

# 代码哈希覆盖范围：**候选包全部 .py** + **影响产出的后端文件**
# （桥/运行器/存储/API 的改动同样会改变结果与账本结构 ⇒ 必须计入同一哈希）。
BACKEND_CODE_FILES = (
    "oi_resonance_bridge.py",
    "backtest_runner.py",
    "backtest_store.py",
    "backtest_api.py",
)

# ── 契约常量（与候选包身份一致；写死在桥里，不从别处推导）──────────────────────
STRATEGY_ID = "WX_OI_RESONANCE_V1"
STRATEGY_KEY = "wx_oi_resonance_v1"
FAMILY = "F_OI_RES"

# 当前**仅确认**「1h 信号 + 15m 执行」。其他周期未验证 ⇒ **禁用**，不静默改配置。
ALLOWED_SIGNAL_TF = ("1h",)
ALLOWED_EXEC_TF = ("15m",)
ALLOWED_EXCHANGE = ("binance",)
ALLOWED_MARKET = ("um",)

# 运行规格（POST body）允许的顶层键——白名单外一律拒绝（不静默忽略）。
SPEC_KEYS = (
    "strategy_id", "exchange", "market", "symbols",
    "signal_timeframe", "exec_timeframe",
    "window_start", "window_end", "window_days", "warmup_bars",
    "params", "note",
)

CONTRACT_VERSION = "bt_api_v1"

AVAILABLE = False
IMPORT_ERROR: Optional[str] = None

C = CORE = EXEC = FACT = REG = None            # noqa: E305
try:                                                       # noqa: SIM105
    from WyckoffMaster.oi_resonance_v1 import config as C          # type: ignore
    from WyckoffMaster.oi_resonance_v1 import core as CORE         # type: ignore
    from WyckoffMaster.oi_resonance_v1 import execution as EXEC    # type: ignore
    from WyckoffMaster.oi_resonance_v1 import factors as FACT      # type: ignore
    from WyckoffMaster.oi_resonance_v1 import registry_entry as REG  # type: ignore
    AVAILABLE = True
except Exception as exc:                                   # noqa: BLE001
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def _require() -> None:
    if not AVAILABLE:
        raise RuntimeError(f"oi_resonance_unavailable: {IMPORT_ERROR}")


# ══════════════════════════ 版本 / 哈希 ══════════════════════════

def code_hash() -> str:
    """**影响产出的全部源码**的聚合 SHA256（按相对路径排序，含文件名与作用域标签）。

    覆盖：候选包 `oi_resonance_v1/*.py` **+** 后端 `BACKEND_CODE_FILES`
    （桥 / 运行器 / 存储 / API）。只哈希候选包会漏掉「后端改动改变结果」的情形。
    """
    _require()
    h = hashlib.sha256()
    for p in sorted(PACKAGE_DIR.glob("*.py")):
        h.update(b"strategy:")
        h.update(p.name.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    for name in BACKEND_CODE_FILES:
        p = BACKEND_DIR / name
        if not p.exists():
            h.update(b"backend-missing:")
            h.update(name.encode("utf-8"))
            h.update(b"\0")
            continue
        h.update(b"backend:")
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def code_hash_manifest() -> Dict[str, str]:
    """逐文件 SHA256（供快照/manifest 定位「同一份代码」）。"""
    _require()
    out: Dict[str, str] = {}
    for p in sorted(PACKAGE_DIR.glob("*.py")):
        out[f"strategy/{p.name}"] = hashlib.sha256(p.read_bytes()).hexdigest()
    for name in BACKEND_CODE_FILES:
        p = BACKEND_DIR / name
        out[f"backend/{name}"] = (hashlib.sha256(p.read_bytes()).hexdigest()
                                  if p.exists() else "MISSING")
    return out


def version_block() -> Dict[str, Any]:
    _require()
    return {
        "strategy_id": STRATEGY_ID,
        "strategy_key": STRATEGY_KEY,
        "family": FAMILY,
        "package_dir": str(PACKAGE_DIR),
        "code_sha256": code_hash(),
        "signal_version": C.SIGNAL_VERSION,
        "core_version": C.CORE_VERSION,
        "exec_core_version": C.EXEC_CORE_VERSION,
        "experiment_id": C.EXPERIMENT_ID,
        "signal_params_hash": C.SIGNAL_FROZEN.hash(),
        "exec_params_hash": C.EXEC_FROZEN.hash(),
        "contract_version": CONTRACT_VERSION,
        "core_available": AVAILABLE,
        "import_error": IMPORT_ERROR,
    }


def formal_run_gate() -> Dict[str, Any]:
    """正式 M4 门禁（只读）。**独立候选研究**不得冒称 M4 已通过。"""
    _require()
    return REG.formal_run_gate()


# ══════════════════════════ 参数白名单 ══════════════════════════

def parameter_whitelist() -> Dict[str, Dict[str, Any]]:
    """允许在 POST 覆盖的参数键（信号 + 执行）。白名单外一律拒绝。"""
    _require()
    out: Dict[str, Dict[str, Any]] = {}
    for k, (lo, hi, kind) in C.SIGNAL_VALIDATION.items():
        out[k] = {"scope": "signal", "min": lo, "max": hi,
                  "type": "int" if kind is int else "float"}
    for k, (lo, hi, kind) in C.EXEC_VALIDATION.items():
        out[k] = {"scope": "exec", "min": lo, "max": hi,
                  "type": "int" if kind is int else "float"}
    return out


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_params(params: Optional[dict]) -> Tuple[Dict[str, Any], List[str]]:
    """按白名单 + 类型/范围校验参数覆盖。返回 `(clean, errors)`；errors 非空 ⇒ 拒绝。"""
    _require()
    wl = parameter_whitelist()
    errs: List[str] = []
    clean: Dict[str, Any] = {}
    if params is None:
        return clean, errs
    if not isinstance(params, dict):
        return {}, ["params 必须是 JSON 对象"]
    for k, v in params.items():
        if k not in wl:
            errs.append(f"参数不在白名单：{k}")
            continue
        spec = wl[k]
        if spec["type"] == "int":
            if not isinstance(v, int) or isinstance(v, bool):
                errs.append(f"{k} 必须为整数（得到 {v!r}）")
                continue
        elif not _num(v):
            errs.append(f"{k} 必须为数值（得到 {v!r}）")
            continue
        fv = float(v)
        if not (fv == fv and fv not in (float("inf"), float("-inf"))):
            errs.append(f"{k} 必须有限（得到 {v!r}）")
            continue
        if spec["min"] is not None and fv < spec["min"]:
            errs.append(f"{k} 必须 >= {spec['min']}（得到 {fv}）")
        if spec["max"] is not None and fv > spec["max"]:
            errs.append(f"{k} 必须 <= {spec['max']}（得到 {fv}）")
        clean[k] = v
    return clean, errs


def split_params(params: Optional[dict]) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """按白名单**作用域**把覆盖参数拆成 `(signal_overrides, exec_overrides, errors)`。

    这一步是「请求配置真正生效」的关键：`fee_rate` / `slippage_bps` / `initial_equity`
    属 exec 作用域，**不得**混进 signal 参数（否则 `signal_params(**all)` 会直接抛错，
    或更糟——被静默丢弃而请求看似成功但实际未生效）。
    """
    _require()
    wl = parameter_whitelist()
    sig: Dict[str, Any] = {}
    ex: Dict[str, Any] = {}
    errs: List[str] = []
    for k, v in (params or {}).items():
        scope = (wl.get(k) or {}).get("scope")
        if scope == "signal":
            sig[k] = v
        elif scope == "exec":
            ex[k] = v
        else:
            errs.append(f"参数不在白名单：{k}")
    return sig, ex, errs


def signal_params(**overrides):
    """在冻结信号参数上做**受控**覆盖（覆盖值必须先过白名单校验）。"""
    _require()
    if not overrides:
        return C.SIGNAL_FROZEN
    bad = [k for k in overrides if k not in C.SIGNAL_VALIDATION]
    if bad:
        raise ValueError(f"signal 参数不在白名单：{bad}")
    return type(C.SIGNAL_FROZEN)(**{**C.SIGNAL_FROZEN.identity(), **overrides})


def exec_params(**overrides):
    """在冻结执行参数上做**受控**覆盖。`live_order_enabled` 恒不可被打开。"""
    _require()
    if not overrides:
        return C.EXEC_FROZEN
    bad = [k for k in overrides if k not in C.EXEC_VALIDATION]
    if bad:
        raise ValueError(f"exec 参数不在白名单：{bad}")
    if overrides.get("live_order_enabled"):
        raise ValueError("live_order_enabled 恒为 False，不得开启")
    return type(C.EXEC_FROZEN)(**{**C.EXEC_FROZEN.identity(), **overrides})


def exec_core_fingerprint() -> Dict[str, Any]:
    """执行核心指纹（供运行规格落盘，保证「读回账本时能定位同一核心」）。"""
    _require()
    return EXEC.version_block()


# ══════════════════════════ 运行规格校验 ══════════════════════════

# 显式窗口须对齐**执行周期网格**（15m）——不静默对齐、不静默取整。
WINDOW_GRID_MS = 900_000
MIN_WINDOW_MS = 86_400_000            # 至少 1 天


def parse_utc_ms(v, *, name: str) -> Tuple[Optional[int], Optional[str]]:
    """把显式窗口端点解析为 **UTC 毫秒**。

    接受：① ISO 8601 字符串（`Z` / `+00:00`；无时区按 UTC 处理）；
          ② epoch 毫秒（int/float，必须整毫秒）。
    返回 `(ms, err)`；`err is None` ⇒ 合法。
    """
    if v is None:
        return None, f"{name} 缺失"
    if isinstance(v, bool):
        return None, f"{name} 非法（bool）"
    if isinstance(v, (int, float)):
        f = float(v)
        if not (f == f and abs(f) != float("inf")) or f != int(f):
            return None, f"{name} 必须为整毫秒 epoch（得到 {v!r}）"
        return int(f), None
    if not isinstance(v, str):
        return None, f"{name} 必须为 ISO8601 字符串或 epoch 毫秒（得到 {type(v).__name__}）"
    s = v.strip()
    if not s:
        return None, f"{name} 为空字符串"
    txt = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError as exc:
        return None, f"{name} 无法解析为 ISO8601：{s!r}（{exc}）"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    if dt.microsecond != 0:
        return None, f"{name} 含亚秒（{dt.microsecond}µs）：窗口须对齐 15m 网格"
    return int(dt.timestamp()) * 1000, None


def validate_window(spec: dict) -> Tuple[Dict[str, Any], List[str]]:
    """校验窗口规格 → `(window_clean, errors)`。

    · 未给 `window_start`/`window_end` ⇒ 自动模式（近期 `window_days` 天，默认 7）；
    · 两者必须**同时**给出；解析为 UTC 毫秒；半开区间 `[start, end)`；
    · 两端须对齐 15m 网格、跨度 >= 1 天；
    · 显式窗口与 `window_days` **互斥**（不静默择一）。
    """
    errs: List[str] = []
    ws_raw, we_raw = spec.get("window_start"), spec.get("window_end")
    has_ws, has_we = ws_raw is not None, we_raw is not None
    if has_ws != has_we:
        errs.append("window_start / window_end 必须同时给出（半开区间 [start, end)）")
        return {"mode": "invalid"}, errs
    if not has_ws:
        wd = spec.get("window_days")
        if wd is not None and (not isinstance(wd, int) or isinstance(wd, bool) or wd < 1):
            errs.append(f"window_days 必须为 >=1 的整数（得到 {wd!r}）")
        return {"mode": "auto", "window_days": wd}, errs

    if spec.get("window_days") is not None:
        errs.append("显式窗口（window_start/window_end）与 window_days 互斥，请只给其一")

    ws, e1 = parse_utc_ms(ws_raw, name="window_start")
    we, e2 = parse_utc_ms(we_raw, name="window_end")
    errs += [e for e in (e1, e2) if e]
    if e1 or e2:
        return {"mode": "invalid"}, errs

    if we <= ws:
        errs.append(f"window_end({we}) 必须晚于 window_start({ws})")
    if ws % WINDOW_GRID_MS != 0 or we % WINDOW_GRID_MS != 0:
        errs.append(f"窗口端点须对齐 {WINDOW_GRID_MS}ms（15m）网格："
                    f"start%grid={ws % WINDOW_GRID_MS} end%grid={we % WINDOW_GRID_MS}")
    if we - ws < MIN_WINDOW_MS:
        errs.append(f"窗口跨度不足 1 天（{(we - ws) / 1000:.0f}s）")

    return {"mode": "explicit", "start_ms": ws, "end_ms": we,
            "span_days": (we - ws) / MIN_WINDOW_MS}, errs


def validate_run_spec(spec: Optional[dict]) -> Tuple[Dict[str, Any], List[str]]:
    """校验 POST 的运行规格（顶层键白名单 + 周期/交易所/市场白名单 + 参数白名单 + 窗口）。

    返回 `(clean_spec, errors)`；errors 非空 ⇒ 调用方以 400 拒绝，**不静默改配置**。
    `clean_spec` 携带 `signal_overrides` / `exec_overrides`（按白名单作用域拆好）
    与 `window`（解析后的 UTC 毫秒）⇒ 请求值**真正进入**运行，且原样落盘快照。
    """
    _require()
    errs: List[str] = []
    spec = spec if isinstance(spec, dict) else {}
    unknown = [k for k in spec if k not in SPEC_KEYS]
    if unknown:
        errs.append(f"未知的运行规格字段：{unknown}")

    sid = spec.get("strategy_id", STRATEGY_ID)
    if sid != STRATEGY_ID:
        errs.append(f"仅支持 {STRATEGY_ID}（收到 {sid!r}）")

    exchange = str(spec.get("exchange", "binance")).lower()
    if exchange not in ALLOWED_EXCHANGE:
        errs.append(f"exchange 仅支持 {ALLOWED_EXCHANGE}（收到 {exchange!r}）")
    market = str(spec.get("market", "um")).lower()
    if market not in ALLOWED_MARKET:
        errs.append(f"market 仅支持 {ALLOWED_MARKET}（收到 {market!r}）")

    sig_tf = str(spec.get("signal_timeframe", "1h")).lower()
    if sig_tf not in ALLOWED_SIGNAL_TF:
        errs.append(f"信号周期仅确认 {ALLOWED_SIGNAL_TF}，其他周期未验证 ⇒ 禁用"
                    f"（收到 {sig_tf!r}）")
    ex_tf = str(spec.get("exec_timeframe", "15m")).lower()
    if ex_tf not in ALLOWED_EXEC_TF:
        errs.append(f"执行周期仅确认 {ALLOWED_EXEC_TF}，其他周期未验证 ⇒ 禁用"
                    f"（收到 {ex_tf!r}）")

    # symbols：当前**只支持单标的**。多标的必须**显式 400**，不得取 [0] 静默丢弃其余。
    syms = spec.get("symbols", ["BTCUSDT"])
    if not isinstance(syms, list) or not all(isinstance(s, str) and s for s in syms):
        errs.append("symbols 必须为非空字符串数组")
        syms = []
    elif len(syms) != 1:
        errs.append(f"当前仅支持单标的（symbols 长度必须为 1，收到 {len(syms)} 个：{syms}）；"
                    "多标的组合尚未验证 ⇒ 拒绝，不静默只取第一个")

    params, perrs = validate_params(spec.get("params"))
    errs.extend(perrs)
    sig_ov, exec_ov, serrs = split_params(params)
    errs.extend(serrs)

    # 窗口：解析 + 网格/跨度校验
    win, werrs = validate_window(spec)
    errs.extend(werrs)

    # 预热：显式有界。预热不得少于指标起点（donchian_entry_window / atr_period），
    # 且建议 >= OI 因子 min_periods（bars_per_day × oi_rank_min_days）。
    wb = spec.get("warmup_bars")
    if wb is not None:
        if not isinstance(wb, int) or isinstance(wb, bool) or wb < 20:
            errs.append(f"warmup_bars 必须为 >=20 的整数（得到 {wb!r}）")
        else:
            need = max(20, int(C.SIGNAL_FROZEN.bars_per_day)
                       * int(C.SIGNAL_FROZEN.oi_rank_min_days))
            if wb < need:
                errs.append(f"warmup_bars={wb} 小于 OI 因子 min_periods={need}"
                            "（窗口起点因子将不可用；如确需请显式接受，本接口拒绝静默降级）")

    # 覆盖参数**实际构造**后由候选包自身校验（如 exit_window<=entry_window）⇒ 400 前置。
    try:
        sp_try = signal_params(**sig_ov)
        errs += [f"signal 参数非法：{e}" for e in C.validate_signal_params(sp_try)]
    except Exception as exc:                                  # noqa: BLE001
        errs.append(f"signal 覆盖参数构造失败：{type(exc).__name__}: {exc}")
    try:
        ep_try = exec_params(**exec_ov)
        errs += [f"exec 参数非法：{e}" for e in C.validate_exec_params(ep_try)]
    except Exception as exc:                                  # noqa: BLE001
        errs.append(f"exec 覆盖参数构造失败：{type(exc).__name__}: {exc}")

    clean = {
        "strategy_id": STRATEGY_ID,
        "exchange": exchange,
        "market": market,
        "symbols": list(syms),
        "signal_timeframe": sig_tf,
        "exec_timeframe": ex_tf,
        "window": win,
        "warmup_bars": spec.get("warmup_bars"),
        "params": params,
        "signal_overrides": sig_ov,
        "exec_overrides": exec_ov,
        "note": spec.get("note"),
    }
    return clean, errs


__all__ = [
    "PROJECT_ROOT", "STRATEGY_DIR", "PACKAGE_DIR", "BACKEND_DIR", "BACKEND_CODE_FILES",
    "STRATEGY_ID", "STRATEGY_KEY", "FAMILY", "CONTRACT_VERSION",
    "ALLOWED_SIGNAL_TF", "ALLOWED_EXEC_TF", "ALLOWED_EXCHANGE", "ALLOWED_MARKET",
    "SPEC_KEYS", "AVAILABLE", "IMPORT_ERROR",
    "WINDOW_GRID_MS", "MIN_WINDOW_MS", "parse_utc_ms", "validate_window",
    "code_hash", "code_hash_manifest", "version_block", "formal_run_gate",
    "parameter_whitelist", "validate_params", "split_params", "validate_run_spec",
    "signal_params", "exec_params", "exec_core_fingerprint",
    "C", "CORE", "EXEC", "FACT", "REG",
]
