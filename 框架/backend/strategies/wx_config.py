# -*- coding: utf-8 -*-
"""F_WX 参数绑定：把**生效参数视图 v2.2（用户 5000 共享模型）**（唯一合成参数视图）绑到四组臂上。

纪律（2026-10-11 独立反例整改后收紧，fail closed）：
  · 数值**只读引用**生效视图，不复制、不改、不按收益调参。
  · 读回后**重算 canonical hash**（`json.dumps(sort_keys=True,separators=(',',':'),
    ensure_ascii=False)` 的 sha256）；与视图内登记值不一致 ⇒ 判定身份漂移。
  · **必须同时**核对三件事，任一不成立即 `identity_ok=False`（不得只报 registered 漂移
    却在 resolve 里给 `hash_check="ok"` —— 那是反例 `view_registry_and_parameter_drift`）：
      ① 生效视图**文件** sha256 == 固定接受值；
      ② `registered_configs` 的 canonical sha256 == 固定接受值（18 条，含 id+arm+kind）；
      ③ 18 条**逐配置生效参数**（信号参数 + **执行覆盖**：费率/滑点/入场与信号退出延迟）
         的 canonical sha256 == 固定接受值。
  · 库内 `strategy_configs` 只有在**与登记 canonical 参数逐字节一致**时才被接受；
    DB 行的 `frozen` **绝不**参与运行放行（反例 `db_override_frozen_bypasses_pending_view`：
    DB frozen=1 + warmup=20 曾绕过 run_frozen=false）。
  · 运行放行 = 视图 `run_frozen` ∧ 身份一致 ∧ G01–G05 全 pass（见 `wx_gate`）。
    `run_frozen=false` ⇒ 本模块只提供**配置绑定**，不构成运行放行。
"""
from __future__ import annotations

import hashlib
import json
import os

from . import common
from . import wx_donchian_wyckoff as wx
from . import wx_gate

# ── 固定接受身份（施工前由生效视图实算并冻结；不是运行放行凭据）──────────────
# 生效视图 10_研究参数生效视图_v2_2_user5000_shared.json 的文件 sha256。
# v2.1（用户 10000）原字节保留但已 superseded；旧 10000/100000 run 拒绝。
EXPECTED_VIEW_SHA256 = \
    "6d18d222d1991f99dd2ba0cdf80b35fbc57f4f482904c945f43d66ebb9df9501"
# registered_configs（18 条：id/kind/arm/changes）的 canonical sha256。
EXPECTED_REGISTERED_CONFIGS_SHA256 = \
    "194d8915f041bbf0b4df3de50b97c8c7775cc178a6c9db8de10db0b0fc4494d8"
# 18 条**生效配置**（信号参数 + 执行覆盖）逐条 canonical sha256 汇总后的 canonical sha256。
EXPECTED_EFFECTIVE_CONFIGS_SHA256 = \
    "1e1737d1900d4645246da3ec0b601e12e5ff0ed5e0225b57b3d3559f65f7b3cc"

# 执行覆盖键：压力组（cost2x / delay30）**真实绑定**到配置身份，而非被丢弃。
EXEC_KEYS = ("fee_each_side", "slippage_each_side",
             "signal_execution_delay_minutes", "signal_exit_delay_minutes")
# 视图 execution 段的默认执行参数（逐配置可被 changes 覆盖）。
EXEC_DEFAULTS_FROM = {
    "fee_each_side": "fee_each_side",
    "slippage_each_side": "slippage_each_side",
    "signal_execution_delay_minutes": "signal_execution_delay_minutes",
    "signal_exit_delay_minutes": "signal_exit_delay_minutes",
}


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def load_view(path=None):
    """读取生效参数视图。返回 (view, file_sha256)；文件缺失返回 (None, None)。

    路径优先级：显式 `path` > 环境变量 `WX_EFFECTIVE_VIEW`（隔离反例用）> 固定视图。
    """
    p = path or os.environ.get("WX_EFFECTIVE_VIEW") or wx.EFFECTIVE_VIEW_PATH
    if not os.path.exists(p):
        return None, None
    with open(p, encoding="utf-8") as f:
        view = json.load(f)
    return view, _sha256_file(p)


def canonical_sha256(obj):
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8")).hexdigest()


def registered_configs_check(view):
    """读回自检：registered_configs 的 canonical hash 是否与登记值一致。"""
    rc = view.get("registered_configs")
    if rc is None:
        return {"status": "missing", "sha256": None}
    got = canonical_sha256(rc)
    return {"status": "ok" if got == EXPECTED_REGISTERED_CONFIGS_SHA256 else "drift",
            "sha256": got, "expected": EXPECTED_REGISTERED_CONFIGS_SHA256,
            "count": len(rc)}


def _exec_defaults(view):
    ex = view.get("execution") or {}
    return {k: ex.get(src) for k, src in EXEC_DEFAULTS_FROM.items()}


def effective_config(view, item):
    """某条登记配置的**生效参数** = 信号参数(REQUIRED_CONFIG) + 执行覆盖 + 该条 changes。

    执行覆盖（费率/滑点/入场与信号退出延迟）必须真实进入身份，否则压力组身份丢失。
    """
    sig = view.get("signals") or {}
    cfg = {k: sig.get(k) for k in wx.REQUIRED_CONFIG}
    cfg.update(_exec_defaults(view))
    for k, v in (item.get("changes") or {}).items():
        if k in cfg:
            cfg[k] = v
    return cfg


def signal_config_for(view, arm_id):
    """取该臂**主配置**（id == arm_id）的生效参数（含执行覆盖）。"""
    for item in view.get("registered_configs") or []:
        if item.get("id") == arm_id:
            return effective_config(view, item)
    # 未登记该 arm：回落为视图 signals + 默认执行（仍标记为未登记）
    cfg = {k: (view.get("signals") or {}).get(k) for k in wx.REQUIRED_CONFIG}
    cfg.update(_exec_defaults(view))
    return cfg


def config_for_registered(view, config_id):
    """按**预登记配置 id**（3a/3b/…/3d_delay30）取完整生效参数（含执行覆盖）。"""
    for item in view.get("registered_configs") or []:
        if item.get("id") == config_id:
            return effective_config(view, item)
    return signal_config_for(view, config_id.split("_")[0])


def registered_configs_effective(view):
    """18 个预登记配置的**生效参数 hash**（逐配置 canonical hash，含执行覆盖）。"""
    out = []
    for item in view.get("registered_configs") or []:
        cfg = effective_config(view, item)
        out.append({"id": item["id"], "kind": item.get("kind"), "arm": item.get("arm"),
                    "config_sha256": common.config_hash(cfg)})
    return out


def effective_configs_check(view):
    """18 条生效配置身份的 canonical 汇总校验（含执行覆盖）。"""
    rows = registered_configs_effective(view)
    got = canonical_sha256(rows)
    return {"status": "ok" if got == EXPECTED_EFFECTIVE_CONFIGS_SHA256 else "drift",
            "sha256": got, "expected": EXPECTED_EFFECTIVE_CONFIGS_SHA256,
            "count": len(rows), "rows": rows}


def identity_check(view, vsha):
    """三件事一次性核对（视图 SHA / 登记表 / 逐配置生效身份）。"""
    rc = registered_configs_check(view)
    ec = effective_configs_check(view)
    view_ok = (vsha == EXPECTED_VIEW_SHA256)
    problems = []
    if not view_ok:
        problems.append(f"生效视图 SHA 与固定接受值不一致（得到 {vsha}）")
    if rc["status"] != "ok":
        problems.append("registered_configs canonical hash 漂移")
    if ec["status"] != "ok":
        problems.append("18 配置生效身份 canonical hash 漂移")
    return {"ok": not problems, "problems": problems, "view_sha_ok": view_ok,
            "registered": rc, "effective": ec}


def resolve(con, arm_id, version=None):
    """解析某臂的生效配置。

    返回 {version, config, config_hash, frozen, run_frozen, source, view_sha256,
          hash_check, identity_ok, identity_problems, run_binding, exec, ...}。
    视图缺失 → None（待配置）。
    """
    if arm_id not in wx.ARM_IDS:
        # 允许按**预登记配置 id**（含敏感性/压力组）解析，便于逐配置身份核验；
        # 仍拒绝任何未登记键。
        view_probe, _ = load_view()
        known = {it.get("id") for it in (view_probe or {}).get("registered_configs") or []}
        if arm_id not in known:
            return None
    view, vsha = load_view()
    if view is None:
        return None

    ident = identity_check(view, vsha)
    cfg = config_for_registered(view, arm_id)
    chash = common.config_hash(cfg)

    row = common.load_config(con, arm_id, version) if con is not None else None
    db_info = None
    if row:
        row_hash = common.config_hash(row["config"])
        agrees = (row_hash == chash)
        db_info = {"db_config_hash": row_hash, "registered_config_hash": chash,
                   "db_frozen": bool(row["frozen"]), "db_agrees_with_registered": agrees}
        if not agrees:
            ident["ok"] = False
            ident["problems"] = list(ident["problems"]) + \
                ["库内 strategy_configs 参数与登记 canonical 不一致（DB 自洽不能替代登记身份）"]
        if row["frozen"] and not bool(view.get("run_frozen")):
            db_info["db_frozen_override_refused"] = True

    bind = wx_gate.binding()
    policy_run_frozen = bool(view.get("run_frozen"))
    frozen = wx_gate.run_frozen(policy_run_frozen, ident["ok"], bind)

    return {
        "version": view.get("spec_version"),
        "config": cfg, "config_hash": chash,
        "registered_config_id": arm_id,
        "arm": next((it.get("arm") for it in view.get("registered_configs") or []
                     if it.get("id") == arm_id), arm_id),
        "kind": next((it.get("kind") for it in view.get("registered_configs") or []
                      if it.get("id") == arm_id), None),
        "exec": {k: cfg.get(k) for k in EXEC_KEYS},
        "stored_hash": (row["config_hash"] if row else None),
        "frozen": frozen, "run_frozen": frozen,
        "policy_run_frozen": policy_run_frozen,
        "source": ("db:strategy_configs" if row and db_info and
                   db_info["db_agrees_with_registered"] else "bound_effective_view_v2_2"),
        "view_sha256": vsha, "view_sha_ok": ident["view_sha_ok"],
        "hash_check": "ok" if ident["ok"] else "drift",
        "identity_ok": ident["ok"], "identity_problems": ident["problems"],
        "registered": ident["registered"], "effective": ident["effective"],
        "db": db_info,
        "run_binding": bind,
    }


# ══════════════════════════════════════════════════════════════════════════
# 第23：375 币**近似研究用途**登记（加性；不改既有判据、不自写接受凭据）
# ══════════════════════════════════════════════════════════════════════════
# 背景（用户 2026-10-12 授权，见 `Work派工_2026-10-11/111..115`）：442「研究期间视为同一
# 合约」假设币经**全历史质量筛选**（相邻完整小时价格 ≥2 倍跳变剔除 36、单位突变 0）得 406，
# 再经原规则（90 日上市龄 / 前 30 完整日成交额中位 ≥500 万 U / 连续 240 小时预热 / 段首 30 日
# 禁入）实际有过准入资格者 375。**375 不是官方历史身份证明、不是 G01–G05 已通过**，只是
# 「同一合约假设 + 保守规格取整」的**近似研究**范围；用途固定
# `近似回测，不是精确实盘还原`。本登记**不关闭**任何既有门禁，也**不自写**接受记录。
PURPOSE_APPROXIMATE_375 = "approximate_375_research"
APPROX_375_SCOPE = "binance_um_375_approx"
APPROX_375_EXCHANGE = "binance"                 # 本轮仅币安；欧易原件保留不参与
APPROX_375_ASSUMPTION = "研究期间视为同一合约；非已证明历史事实"
APPROX_375_PURPOSE_LABEL = "近似回测，不是精确实盘还原"
APPROX_375_READY_FOR = "READY_FOR_CODEX_ACCEPTANCE"
APPROX_375_POOL_COUNT = 375
# 候选日历（**继承既有**；若新池面板起点条件不满足则报冲突、**不**自行改日期）。
APPROX_375_CALENDAR = {
    "panel_start_inherited": "2020-12-07T00:00:00Z",
    "panel_end": "2026-10-09T00:00:00Z",
    "DEV": "2020-12-07..2023-11-07",
    "VAL": "2023-11-08..2025-01-07",
    "FINAL": "2025-01-08..2026-10-09",
    "segment_head_ban_days": 30,
    "warmup_hours": 240,
    "min_listed_days": 90,
    "min_prior30_median_quote_USDT": 5000000,
    "panel_start_rule": "first_accepted_UTC_day_with_rolling30day_median_eligible_count_at_least30",
    "cutoff_rule": "parsed_from_manifest_never_hardcoded",
}

# ── 118 人类确认日历（2026-10-12 确认；**覆盖** 上方 inherited 候选，仅记回执不擅改）──────
# 用户 2026-10-12 明确确认：面板 2020-12-15→2026-10-09；DEV 至 2023-11-11；段首 30 日禁开，
# DEV 首开 2021-01-14（=面板起+30d，正好给足「此前 30 完整 UTC 日」窗口）。
APPROX_375_CALENDAR_STATUS = "human_confirmed"
APPROX_375_CALENDAR_CONFIRMED = {
    "source": ("/Users/boos/.codex/worktrees/c9d7/加密货币趋势判断系统/"
               "Work派工_2026-10-11/118_20201215新候选日历_待用户确认.json"),
    "human_confirmed": True,
    "confirmed_at_utc": "2026-10-12T04:43:15.294862+00:00",
    "timezone": "UTC",
    "dates_inclusive": True,
    "total_days": 2125,
    "overall": ["2020-12-15", "2026-10-09"],
    "DEV": ["2020-12-15", "2023-11-11"],
    "VAL": ["2023-11-12", "2025-01-09"],
    "FINAL": ["2025-01-10", "2026-10-09"],
    "first_entry_days": {"DEV": "2021-01-14", "VAL": "2023-12-12", "FINAL": "2025-02-09"},
    "boundaries_cumulative": [1062, 1487],
    "segment_head_ban_days": 30,
    "cutoff": "2026-10-10T07:00:00Z",
}
# 继承候选（APPROX_375_CALENDAR）已被上方确认值**取代**；保留仅作 23 回归兼容，不参与放行。
APPROX_375_CALENDAR_SUPERSEDED_NOTE = (
    "APPROX_375_CALENDAR（inherited 2020-12-07）已被 APPROX_375_CALENDAR_CONFIRMED（118）取代；"
    "新输入/回测一律用确认值，inherited 仅作 23 回归兼容。")

# 本次离线决策目录（用户本轮授权的 375 名单 / 逐日资格 / 规格 / 费用规则来源；**只读**）。
APPROX_375_DECISION_DIR = ("/Users/boos/.codex/worktrees/c9d7/加密货币趋势判断系统/"
                           "本次离线决策_2026-10-12")
APPROX_375_POOL_LIST = os.path.join(APPROX_375_DECISION_DIR, "19_最终375币假设研究池.csv")
APPROX_375_DAILY_ELIGIBILITY = os.path.join(APPROX_375_DECISION_DIR,
                                            "12_通过连续性币的逐日准入资格.csv")
APPROX_375_POOL_EVIDENCE = os.path.join(APPROX_375_DECISION_DIR,
                                        "13_最终假设研究币池与审查证据.json")
APPROX_375_SPEC_ROUNDING = os.path.join(APPROX_375_DECISION_DIR,
                                        "07_526币保守执行近似规格.csv")
APPROX_375_MODEL_DOC = os.path.join(APPROX_375_DECISION_DIR,
                                    "08_具体近似模型与缺口持仓两方案.md")
APPROX_375_FEE_DOC = os.path.join(APPROX_375_DECISION_DIR,
                                  "20_用户新假设与缺口费用压力规则.md")
# 本窗口（23）产出的日历冲突检查（只读引用；缺则 `calendar.conflict_check.status=missing`）。
APPROX_375_CALENDAR_CHECK = os.path.join(
    wx_gate.SUITE_DIR, "375近似规则与输入冻结23", "02_日历冲突检查.json")

# 第27：375 逐日名单与实际输入（**本窗口只读登记**；doc = run_arm 直接消费的日名单）。
APPROX_375_DIR27 = os.path.join(wx_gate.SUITE_DIR, "375逐日名单与实际输入接通27")
APPROX_375_DAILY_MAP_DOC = os.path.join(APPROX_375_DIR27, "02_375逐日名单_DEV.json")
APPROX_375_EVIDENCE_CSV = os.path.join(APPROX_375_DIR27, "03_375逐日资格证据.csv")
APPROX_375_COVERAGE = os.path.join(APPROX_375_DIR27, "07_实际输入覆盖与来源.json")
APPROX_375_CROSS_CHECK = os.path.join(APPROX_375_DIR27, "04_交叉核对_12CSV.json")

# 规格取整假设（**保守取整**方向；来源见 07/08，不在本模块重定义数值）。
APPROX_375_ROUNDING = {
    "purpose_label": APPROX_375_PURPOSE_LABEL,
    "quantity": "q=s*floor(q_upper/s)；不为凑最小单向上加仓",
    "buy_price": "先施不利滑点，再按价格步进 δ 向上取整",
    "sell_price": "先施不利滑点，再按价格步进 δ 向下取整",
    "protective_stop": "按 δ 向下取整后重核风险上限",
    "reject": "低于最小可下量/超最大量/取整后越界 ⇒ 整单拒绝并逐条留因",
    "source": APPROX_375_SPEC_ROUNDING,
}

_APPROX_SHA_CACHE = {}
_APPROX_MANIFEST_CACHE = {}


def _sha_cached(path):
    """按 (path, mtime_ns, size) 缓存的文件 sha256；缺失 ⇒ None（不抛）。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime_ns, st.st_size)
    hit = _APPROX_SHA_CACHE.get(key)
    if hit is None:
        try:
            hit = _sha256_file(path)
        except OSError:
            return None
        _APPROX_SHA_CACHE.clear()
        _APPROX_SHA_CACHE[key] = hit
    return hit


def _read_json_cached(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime_ns, st.st_size)
    hit = _APPROX_MANIFEST_CACHE.get(key)
    if hit is None:
        try:
            with open(path, encoding="utf-8") as f:
                hit = json.load(f)
        except (OSError, ValueError):
            return None
        _APPROX_MANIFEST_CACHE.clear()
        _APPROX_MANIFEST_CACHE[key] = hit
    return hit


def approximate_375_pool():
    """读 375 清单（19 CSV 第一列）→ {path, sha256, count, symbols}。缺失/非法 ⇒ symbols None。"""
    p = APPROX_375_POOL_LIST
    if not os.path.exists(p):
        return {"path": p, "sha256": None, "count": 0, "symbols": None}
    import csv as _csv
    syms = []
    try:
        with open(p, encoding="utf-8-sig") as f:
            for row in _csv.DictReader(f):
                s = row.get("币")
                if s:
                    syms.append(s)
    except (OSError, ValueError):
        return {"path": p, "sha256": _sha_cached(p), "count": 0, "symbols": None}
    return {"path": p, "sha256": _sha_cached(p), "count": len(syms), "symbols": syms}


def _manifest_binance_symbols():
    """正式 manifest 的**币安 symbol 集合** + 官方不可得 symbol 集合（只读；缓存）。"""
    doc = _read_json_cached(wx_gate.DATA_MANIFEST)
    if not isinstance(doc, dict):
        return None, None, None
    files = doc.get("files")
    if not isinstance(files, list):
        return None, None, None
    present, off = set(), set()
    for r in files:
        if not isinstance(r, dict) or r.get("exchange") != "binance":
            continue
        s = r.get("symbol")
        if r.get("availability") == "official_unavailable":
            off.add(s)
        else:
            present.add(s)
    return present | off, off, doc


def approximate_375_daily_loader():
    """第27：375 **逐日名单与实际输入**只读登记（三掩码分离；不构成研究放行）。

    · `daily_list_doc`：`daily_list_map/v1`（`days[*].order/admission`，键 `exchange:symbol`），
      `run_arm` 直接消费；`daily_list_version` 由此文件规范化内容解析。
    · 掩码**分离**：`orig_qual`（原资格）/ `ban`（新段首 30 日禁入）/ `tradeable`（价格/mark 可交易）。
    · `coverage`：375 池实际输入覆盖；3 币（币安人生/我踏马来了/龙虾）1h mark 本地缺。
    · `cross_check_12`：与 12 CSV 重叠日逐值一致（证明重算=既有原资格）。
    缺任一材料 ⇒ `status="blocked"` + 具体原因；**不**自写接受。
    """
    blocked = []
    doc_sha = _sha_cached(APPROX_375_DAILY_MAP_DOC)
    if not doc_sha:
        blocked.append("daily_map_doc_missing:" + APPROX_375_DAILY_MAP_DOC)
    ev_sha = _sha_cached(APPROX_375_EVIDENCE_CSV)
    if not ev_sha:
        blocked.append("evidence_csv_missing:" + APPROX_375_EVIDENCE_CSV)
    cov = _read_json_cached(APPROX_375_COVERAGE)
    if not isinstance(cov, dict):
        blocked.append("coverage_missing:" + APPROX_375_COVERAGE)
    cross = _read_json_cached(APPROX_375_CROSS_CHECK)
    if not isinstance(cross, dict):
        blocked.append("cross_check_missing:" + APPROX_375_CROSS_CHECK)
    elif cross.get("verdict") != "IDENTICAL_ON_OVERLAP":
        blocked.append("cross_check_not_identical:%r" % cross.get("verdict"))
    doc = _read_json_cached(APPROX_375_DAILY_MAP_DOC)
    days = len((doc or {}).get("days") or []) if isinstance(doc, dict) else None
    mark_missing = (cov or {}).get("mark_missing_symbols")
    return {
        "schema": "wx_config.approximate_375_daily_loader/v1",
        "purpose": PURPOSE_APPROXIMATE_375,
        "scope": APPROX_375_SCOPE,
        "exchange": APPROX_375_EXCHANGE,
        "calendar": APPROX_375_CALENDAR_CONFIRMED,
        "calendar_status": APPROX_375_CALENDAR_STATUS,
        "daily_list_doc": {"path": APPROX_375_DAILY_MAP_DOC, "sha256": doc_sha, "days": days},
        "evidence_csv": {"path": APPROX_375_EVIDENCE_CSV, "sha256": ev_sha},
        "coverage": cov,
        "cross_check_12": ({"verdict": cross.get("verdict"),
                            "overlap_matched": cross.get("overlap_matched"),
                            "median_mismatch_count": cross.get("median_mismatch_count"),
                            "recomputed_but_not_in_12_count": cross.get("recomputed_but_not_in_12_count"),
                            "in_12_but_not_recomputed": cross.get("in_12_but_not_recomputed")}
                           if isinstance(cross, dict) else None),
        "masks_separated": ["orig_qual", "ban", "tradeable"],
        "mark_missing_symbols": mark_missing,
        "pool_shrunk": False,
        "status": "blocked" if blocked else "resolved",
        "blocked_reasons": blocked,
        "formal_ready": False,
        "research_pass": False,
        "self_written_acceptance": False,
        "ready_for": APPROX_375_READY_FOR,
        "note": ("逐日准入/排名由 run_arm 经 rank_keys_by_day/admission_by_day 实际消费；"
                 "三掩码分离、不静默缩 375 池；3 币 mark 本地缺按可交易掩码 False + 执行期严格停算。"),
    }


# ── 第30：375 **实际运行**接通身份（**只读登记**；读 30 实际交付材料）──────────────────
# 与 23/27 的分工：23 登记「近似用途/输入/资本/费用模型」；27 登记「逐日名单/实际输入 loader」；
# 30 登记「**实际运行入口**及其真实消费身份」（池/日名单/模型/费用材料/数据版本/源码/引擎/
# 18 配置/资本/父子 run），供 31 独立审查按**真实来源**核对。**不**参与任何放行判据，**不**自写接受。
APPROX_375_DIR30 = os.path.join(wx_gate.SUITE_DIR, "375实际运行接通30")
APPROX_375_ACTUAL_ENTRY = os.path.join(APPROX_375_DIR30, "_entry30.py")
APPROX_375_ACTUAL_MATERIALS = os.path.join(APPROX_375_DIR30, "05_实际运行材料与身份.json")
APPROX_375_ACTUAL_BNB = os.path.join(APPROX_375_DIR30, "06_BNB准入日正向对账.json")
APPROX_375_ACTUAL_CONTROLS = os.path.join(APPROX_375_DIR30, "07_控制与拒绝用例.json")
APPROX_375_ACTUAL_ALL375 = os.path.join(APPROX_375_DIR30, "08_全375候选版本可生成.json")
APPROX_375_HANDOFF30 = os.path.join(APPROX_375_DIR30, "handoff_ready30.json")


def approximate_375_actual_run_registration():
    """第30：375 **实际运行**接通身份（只读真实文件；**不**构成研究放行）。

    · `entry`：30 薄回放入口路径 + SHA（供 31 复用）。
    · `materials`：30 实际运行材料/身份 JSON（读其摘要；**不**信调用方字符串）。
    · `identity`：复用实算身份（pool/daily_list/code/engine/configs_18/capital/child_run）。
    缺任一材料 ⇒ `status="blocked"` + 具体原因；`formal_ready=False`、`research_pass=False`。
    """
    blocked = []
    entry_sha = _sha_cached(APPROX_375_ACTUAL_ENTRY)
    if not entry_sha:
        blocked.append("entry_missing:" + APPROX_375_ACTUAL_ENTRY)
    mats = _read_json_cached(APPROX_375_ACTUAL_MATERIALS)
    if not isinstance(mats, dict):
        blocked.append("materials_missing:" + APPROX_375_ACTUAL_MATERIALS)

    pool = approximate_375_pool()
    if not pool["sha256"]:
        blocked.append("pool_list_missing_or_unreadable:" + APPROX_375_POOL_LIST)
    daily_sha = _sha_cached(APPROX_375_DAILY_MAP_DOC)
    if not daily_sha:
        blocked.append("daily_map_doc_missing:" + APPROX_375_DAILY_MAP_DOC)

    from . import wx_execution as X          # 延迟导入（wx_execution 亦延迟导入本模块）
    code, _per = X.compute_code_bundle_sha()
    cfg = X.compute_strategy_config_identity()
    view, vsha = load_view()
    ident = identity_check(view, vsha) if view is not None else {"ok": False,
                                                                "problems": ["view_missing"]}
    if not ident.get("ok"):
        blocked.append("view_identity_drift:" + ";".join(ident.get("problems") or []))
    fm_models = list(X.REGISTERED_FUNDING_MODELS)
    if fm_models != ["norm4h", "ext1h"]:
        blocked.append("funding_model_registry_changed:%r" % (fm_models,))
    capital = None
    if view is not None:
        cm = view.get("capital_model") or {}
        capital = {
            "initial_equity_USDT": cm.get("total_initial_equity_USDT_per_research_arm"),
            "initial_capital_credit_count": cm.get("initial_capital_credit_count"),
            "shared_across": "binance",
            "leverage": (view.get("risk") or {}).get("leverage", 1),
            "one_deposit_per_arm": True,
            "no_cross_arm_pooling": True,
        }

    return {
        "schema": "wx_config.approximate_375_actual_run/v1",
        "purpose": PURPOSE_APPROXIMATE_375,
        "scope": APPROX_375_SCOPE,
        "exchange": APPROX_375_EXCHANGE,
        "assumption": APPROX_375_ASSUMPTION,
        "purpose_label": APPROX_375_PURPOSE_LABEL,
        "entry": {"path": APPROX_375_ACTUAL_ENTRY, "sha256": entry_sha},
        "materials": {
            "path": APPROX_375_ACTUAL_MATERIALS, "sha256": _sha_cached(APPROX_375_ACTUAL_MATERIALS),
            "present": isinstance(mats, dict),
            "summary": ({k: mats.get(k) for k in (
                "generated_at_utc", "purpose", "scope", "model", "fee_materials_schema",
                "data_version", "signal_source", "signals_nonempty", "child_run",
                "entry_functions", "blocked_reasons", "status")} if isinstance(mats, dict) else None),
        },
        "companion_artifacts": {
            "bnb_admission_positive": {"path": APPROX_375_ACTUAL_BNB,
                                       "sha256": _sha_cached(APPROX_375_ACTUAL_BNB)},
            "controls_and_rejects": {"path": APPROX_375_ACTUAL_CONTROLS,
                                     "sha256": _sha_cached(APPROX_375_ACTUAL_CONTROLS)},
            "all_375_version_generation": {"path": APPROX_375_ACTUAL_ALL375,
                                           "sha256": _sha_cached(APPROX_375_ACTUAL_ALL375)},
            "handoff_ready30": {"path": APPROX_375_HANDOFF30,
                                "sha256": _sha_cached(APPROX_375_HANDOFF30)},
        },
        "identity": {
            "pool_list": {"path": pool["path"], "sha256": pool["sha256"], "count": pool["count"]},
            "daily_list_doc": {"path": APPROX_375_DAILY_MAP_DOC, "sha256": daily_sha},
            "code_bundle_sha256": code,
            "strategy_config": cfg.get("value"),
            "configs_18_sha256": cfg.get("effective_configs_sha256"),
            "view_sha256": vsha,
            "engine": (wx_gate.compute_real_bindings() or {}).get("engine"),
            "capital": capital,
            "funding_models": fm_models,
        },
        "child_run_fields": {"record_source": wx_gate.CHILD_RUN_RECORD_SOURCE,
                             "ledger_fields": list(wx_gate.CHILD_RUN_LEDGER_FIELDS)},
        "status": "blocked" if blocked else "resolved",
        "blocked_reasons": blocked,
        "formal_ready": False,
        "research_pass": False,
        "self_written_acceptance": False,
        "not_participating_in_gates": ["all_pass", "research_pass"],
        "ready_for": APPROX_375_READY_FOR,
        "note": ("30 只登记**实际运行入口与真实消费身份**；近似用途须 Codex 独立接受，"
                 "本登记不关闭任何门禁、不伪改 526 官方历史。"),
    }


# ── 第34：375 **完整输入版本与实际运行**接通身份（**只读登记**；读 34 实际交付材料）──────────
# 与 23/27/30 的分工：23 登记「近似用途/输入/资本/费用模型」；27 登记「逐日名单/实际输入 loader」；
# 30 登记「**实际运行入口**及其真实消费身份」；34 登记「**完整输入版本**与**实际使用凭证**的分列
# 口径**及真实引用链**」——完整输入版本身份**稳定**，实际使用凭证从**同一真实账本**派生并**指向**
# 完整输入版本。**近似就绪**与 **strict 官方门禁**分列：后者恒 False。**不**入放行判据、**不**自写接受。
APPROX_375_DIR34 = os.path.join(wx_gate.SUITE_DIR, "完整输入版本与实际运行接通34")
APPROX_375_COMPLETE_INPUT_ENTRY = os.path.join(APPROX_375_DIR34, "_entry34.py")
APPROX_375_COMPLETE_INPUT_POSITIVE = os.path.join(APPROX_375_DIR34, "01_BNB完整输入与使用凭证.json")
APPROX_375_COMPLETE_INPUT_NEGATIVE = os.path.join(APPROX_375_DIR34, "02_反例_缺整笔应扣费用.json")
APPROX_375_COMPLETE_INPUT_HANDOFF = os.path.join(APPROX_375_DIR34, "handoff_ready34.json")

COMPLETE_INPUT_ACTUAL_RUN_SCHEMA = "wx_config.approximate_375_complete_input_actual_run/v1"


def approximate_375_complete_input_actual_run_registration():
    """第34：375 **完整输入版本与实际运行**接通身份（**只读真实文件/实算源 SHA**；**不**构成研究放行）。

    · `entry`：34 薄入口路径 + SHA（供后续独立审查复用）。
    · `dependency_33`：并行 33 的 `wx_execution`/`wx_ledger` **实算** sha + 关键 schema/表存在性
      （v2 完整材料 / 实际使用凭证 / 逐笔双向审查）——**只读**，**不**改 33 文件。
    · `identity_reference_chain`：完整输入版本身份与**实际使用凭证**ID **分列**引用
      （读 34 正向材料摘要；**不**信调用方字符串）。
    · `approximate_375_ready`：34 实际入口已接线且 33 v2 接口就绪（**近似用途**层面）。
    · `strict_official_gate`：**恒 False**（近似 scope ≠ 授权 scope `binance_um_526`；须 Codex 独立接受）。
    缺任一材料 ⇒ `status="blocked"` + 具体原因；`formal_ready=False`、`research_pass=False`。
    """
    blocked = []
    entry_sha = _sha_cached(APPROX_375_COMPLETE_INPUT_ENTRY)
    if not entry_sha:
        blocked.append("entry_missing:" + APPROX_375_COMPLETE_INPUT_ENTRY)

    # 33 依赖：只读实算源 SHA + 关键接口存在性（**不**改 33 文件）
    from . import wx_execution as X          # 延迟导入（wx_execution 亦延迟导入本模块）
    dep = {}
    try:
        dep["wx_execution.py"] = {"sha256": _sha_cached(X.__file__),
                                  "materials_schema": X.FUNDING_MATERIALS_SCHEMA,
                                  "voucher_schema": X.FUNDING_USAGE_VOUCHER_SCHEMA,
                                  "audit_schema": X.FUNDING_USAGE_AUDIT_SCHEMA,
                                  "has_complete_materials": hasattr(X, "complete_funding_materials"),
                                  "has_usage_voucher": hasattr(X, "funding_usage_voucher"),
                                  "has_usage_audit": hasattr(X, "audit_funding_usage")}
        from . import wx_ledger as L
        dep["wx_ledger.py"] = {"sha256": _sha_cached(L.__file__),
                               "has_funding_audit_table": hasattr(L.Ledger, "add_funding_audit")}
        if not dep["wx_execution.py"]["has_complete_materials"]:
            blocked.append("dep33_complete_funding_materials_missing")
        if not dep["wx_execution.py"]["has_usage_voucher"]:
            blocked.append("dep33_funding_usage_voucher_missing")
        if not dep["wx_execution.py"]["has_usage_audit"]:
            blocked.append("dep33_audit_funding_usage_missing")
        if not dep["wx_ledger.py"]["has_funding_audit_table"]:
            blocked.append("dep33_funding_audit_table_missing")
    except Exception as exc:  # noqa: BLE001 —— 依赖不可读须如实登记，不冒完成
        blocked.append("dep33_unreadable:%s: %s" % (type(exc).__name__, exc))

    pos = _read_json_cached(APPROX_375_COMPLETE_INPUT_POSITIVE)
    neg = _read_json_cached(APPROX_375_COMPLETE_INPUT_NEGATIVE)
    if not isinstance(pos, dict):
        blocked.append("positive_artifact_missing:" + APPROX_375_COMPLETE_INPUT_POSITIVE)
    if not isinstance(neg, dict):
        blocked.append("negative_artifact_missing:" + APPROX_375_COMPLETE_INPUT_NEGATIVE)

    chain = None
    if isinstance(pos, dict):
        # 正例材料形如 {"results": {"<run_id>": {"complete_input":…, "usage_credential":…, …}}}
        results = pos.get("results") if isinstance(pos.get("results"), dict) else {}
        pick = None
        for rid, rr in results.items():
            if isinstance(rr, dict) and "norm4h" in str(rid):
                pick = rr
                break
        if pick is None and results:
            pick = next((v for v in results.values() if isinstance(v, dict)), None)
        pick = pick or {}
        ci = pick.get("complete_input") or {}
        vc = pick.get("usage_credential") or {}
        au = pick.get("usage_audit") or {}
        chain = {
            "source_run_id": pick.get("run_id"),
            "complete_input_identity": ci.get("identity"),
            "complete_input_n_events": ci.get("n_events"),
            "complete_input_n_marks": ci.get("n_marks"),
            "usage_voucher_id": vc.get("voucher_id"),
            "usage_voucher_n_rows": vc.get("n_rows"),
            "usage_voucher_points_to": vc.get("complete_input_identity"),
            "usage_audit_ok": au.get("ok"),
            "usage_audit_n_expected": au.get("n_expected"),
            "usage_audit_n_charged": au.get("n_charged"),
            "declared_equals_complete_input": (pick.get("checks") or {}).get(
                "declaration_equals_complete_input"),
            "voucher_id_differs_from_complete_input": (pick.get("checks") or {}).get(
                "voucher_id_differs_from_complete_input"),
            "separate_not_forced_equal": True,
            "note": "完整输入 ID 稳定；使用凭证 ID 因持仓集合变化而不同 ⇒ 两者**不**强行同码，仅引用。",
        }
        if not chain.get("complete_input_identity"):
            blocked.append("positive_artifact_has_no_complete_input_identity")

    negative_ok = None
    if isinstance(neg, dict):
        cases = neg.get("cases") or {}
        negative_ok = bool(neg.get("all_cases_failed_as_expected")) and all(
            (v or {}).get("failed") is True for v in cases.values()) if cases else None

    approximate_ready = (not blocked)
    return {
        "schema": COMPLETE_INPUT_ACTUAL_RUN_SCHEMA,
        "purpose": PURPOSE_APPROXIMATE_375,
        "scope": APPROX_375_SCOPE,
        "exchange": APPROX_375_EXCHANGE,
        "assumption": APPROX_375_ASSUMPTION,
        "purpose_label": APPROX_375_PURPOSE_LABEL,
        "entry": {"path": APPROX_375_COMPLETE_INPUT_ENTRY, "sha256": entry_sha},
        "dependency_33": dep,
        "companion_artifacts": {
            "positive": {"path": APPROX_375_COMPLETE_INPUT_POSITIVE,
                         "sha256": _sha_cached(APPROX_375_COMPLETE_INPUT_POSITIVE),
                         "present": isinstance(pos, dict)},
            "negative": {"path": APPROX_375_COMPLETE_INPUT_NEGATIVE,
                         "sha256": _sha_cached(APPROX_375_COMPLETE_INPUT_NEGATIVE),
                         "present": isinstance(neg, dict),
                         "all_cases_failed_as_expected": negative_ok},
            "handoff_ready34": {"path": APPROX_375_COMPLETE_INPUT_HANDOFF,
                                "sha256": _sha_cached(APPROX_375_COMPLETE_INPUT_HANDOFF)},
        },
        "identity_reference_chain": chain,
        "complete_input_caliber": {
            "schema": "complete_input_version/v1",
            "stable": True,
            "overwritten_by_usage_subset": False,
            "source": "wx_daily_375.complete_input_version（复用 33 complete_funding_materials + funding_materials_identity）",
        },
        "usage_credential_caliber": {
            "schema": "funding_usage_voucher/v1",
            "derived_from": "同一真实账本 research_run+position+order+funding",
            "points_to": "complete_input_identity",
            "not_forced_equal_to_complete_input": True,
            "source": "wx_daily_375.usage_credential（薄包装 33 funding_usage_voucher）",
        },
        "approximate_375_ready": approximate_ready,
        "strict_official_gate": {
            "formal_ready": False,
            "research_pass": False,
            "authorized_scope": "binance_um_526",
            "approx_scope": APPROX_375_SCOPE,
            "approx_scope_is_not_authorized_scope": True,
            "requires_codex_independent_acceptance": True,
        },
        "status": "blocked" if blocked else "resolved",
        "blocked_reasons": blocked,
        "formal_ready": False,
        "research_pass": False,
        "self_written_acceptance": False,
        "not_participating_in_gates": ["all_pass", "research_pass"],
        "ready_for": APPROX_375_READY_FOR,
        "note": ("34 只登记**完整输入版本身份**与**实际使用凭证**的**分列**口径与真实引用链；"
                 "近似用途须 Codex 独立接受；本登记不关闭任何门禁、不伪改 526 官方历史。"),
    }


def approximate_375_registration(real=None, with_calendar=True):
    """第23：375 近似研究用途的**实际输入 / 版本 / 资本 / 风险 / 费用模型元数据**。

    **只读真实文件**；缺材料/覆盖不符 ⇒ `status="blocked"` + 具体 `blocked_reasons`。
    **恒不构成研究放行**：`formal_ready=False`、`research_pass=False`、`self_written_acceptance=False`。
    本函数供 `wx_gate.binding()` 加性引用与 23 交接材料使用；**不**改任何既有放行判据。
    """
    blocked = []
    pool = approximate_375_pool()
    if not pool["sha256"]:
        blocked.append("pool_list_missing_or_unreadable:" + APPROX_375_POOL_LIST)
    if not pool["symbols"]:
        blocked.append("pool_list_empty_or_unparsable")
    elif pool["count"] != APPROX_375_POOL_COUNT:
        blocked.append("pool_count_not_375:%d" % pool["count"])

    daily_sha = _sha_cached(APPROX_375_DAILY_ELIGIBILITY)
    if not daily_sha:
        blocked.append("daily_eligibility_missing_or_unreadable:" + APPROX_375_DAILY_ELIGIBILITY)
    ev_sha = _sha_cached(APPROX_375_POOL_EVIDENCE)
    if not ev_sha:
        blocked.append("pool_evidence_missing_or_unreadable:" + APPROX_375_POOL_EVIDENCE)

    # data 维度：复用门禁的**正式全范围 manifest** 绑定（只查存在性，不重读字节）。
    formal = wx_gate._formal_data_binding()
    if formal is None:
        blocked.append("formal_manifest_binding_unavailable:" + wx_gate.DATA_MANIFEST)

    # 覆盖：375 ⊆ manifest 币安 symbol 集合。
    man_syms, man_off, man_doc = _manifest_binance_symbols()
    coverage = {"pool_count": pool["count"], "covered": None, "missing": None,
                "pool_symbols_with_official_unavailable_mark": None}
    if man_syms is not None and pool["symbols"]:
        covered = sorted(set(pool["symbols"]) & man_syms)
        missing = sorted(set(pool["symbols"]) - man_syms)
        coverage = {
            "pool_count": pool["count"], "covered": len(covered), "missing": missing,
            "pool_symbols_with_official_unavailable_mark":
                sorted(set(pool["symbols"]) & (man_off or set())),
        }
        if missing:
            blocked.append("pool_symbols_absent_from_manifest:%d" % len(missing))

    cutoff = (man_doc or {}).get("cutoff") if isinstance(man_doc, dict) else None
    if cutoff is None and formal is not None:
        cutoff = formal.get("as_of")
    if cutoff is None:
        blocked.append("cutoff_unresolvable_from_manifest")

    # 版本 / 18 配置 / 引擎 / 代码 / 资本 / 风险：复用实算身份（缺则由调用方传入或现算）。
    real = real if real is not None else wx_gate.compute_real_bindings()
    view, vsha = load_view()
    ident = identity_check(view, vsha) if view is not None else {"ok": False,
                                                                "problems": ["view_missing"]}
    capital, risk, spec_version = None, None, None
    if view is not None:
        cm = view.get("capital_model") or {}
        capital = {
            "initial_equity_USDT": cm.get("total_initial_equity_USDT_per_research_arm"),
            "initial_capital_credit_count": cm.get("initial_capital_credit_count"),
            "shared_across": "binance",       # 本轮仅币安
            "leverage": (view.get("risk") or {}).get("leverage", 1),
            "one_deposit_per_arm": True,
            "no_cross_arm_pooling": True,
        }
        rk = view.get("risk") or {}
        risk = {k: rk.get(k) for k in (
            "initial_equity_USDT", "risk_per_trade_equity_fraction", "leverage",
            "max_positions", "single_name_notional_equity_cap",
            "gross_long_notional_equity_cap", "cash_or_free_margin_reserve_fraction",
            "portfolio_initial_stop_risk_cap", "entry_participation_previous_15m_quote_cap",
            "maximum_account_drawdown_fraction")}
        spec_version = view.get("spec_version")
    else:
        blocked.append("effective_view_missing:" + str(wx.EFFECTIVE_VIEW_PATH))
    if not ident.get("ok"):
        blocked.append("view_identity_drift:" + ";".join(ident.get("problems") or []))

    # 费用模型：模型名**严格登记匹配**（复用 wx_execution 唯一登记表，不重定义费率规则）。
    from . import wx_execution as X          # 延迟导入（wx_execution 亦延迟导入本模块）
    models = list(X.REGISTERED_FUNDING_MODELS)
    if models != ["norm4h", "ext1h"]:
        blocked.append("funding_model_registry_changed:%r" % (models,))
    fee = {
        "registered_models": models,
        "mutually_exclusive_never_summed": True,
        "normal_4h": "每4h(UTC00/04/08/12/16/20)；费率=严格早于T的最近真实结算费率(last-known)；"
                     "不除8/除2、不补0；符号原样",
        "extreme_1h_2pct": "每1h整点；幅度0.02 单边不利压力；基数=持仓名义×T时刻真实1h mark",
        "gap_adjacent_adverse": "缺真实费率取相邻不利者（做多取较大；皆负取较不负）；标"
                                "「相邻不利费率估计」+ 后侧来源；单侧标「仅单侧」；双侧皆无或 mark 缺 ⇒ 停算",
        "missing_mark_stops": True,
        "rule_source": {"fee_rule": APPROX_375_FEE_DOC, "sha256": _sha_cached(APPROX_375_FEE_DOC)},
    }

    spec_docs = {
        "rounding_csv": {"path": APPROX_375_SPEC_ROUNDING,
                         "sha256": _sha_cached(APPROX_375_SPEC_ROUNDING)},
        "model_doc": {"path": APPROX_375_MODEL_DOC, "sha256": _sha_cached(APPROX_375_MODEL_DOC)},
        "fee_rule_doc": {"path": APPROX_375_FEE_DOC, "sha256": _sha_cached(APPROX_375_FEE_DOC)},
    }

    cal = None
    if with_calendar:
        chk = _read_json_cached(APPROX_375_CALENDAR_CHECK)
        cal = {"inherited": dict(APPROX_375_CALENDAR),
               "conflict_check": {"path": APPROX_375_CALENDAR_CHECK,
                                  "sha256": _sha_cached(APPROX_375_CALENDAR_CHECK),
                                  "status": "available" if isinstance(chk, dict) else "missing"},
               "dates_not_modified_by_23": True}
        if isinstance(chk, dict):
            cal["conflict_check"]["summary"] = chk.get("summary")

    return {
        "schema": "wx_config.approximate_375_registration/v1",
        "purpose": PURPOSE_APPROXIMATE_375,
        "purpose_label": APPROX_375_PURPOSE_LABEL,
        "assumption": APPROX_375_ASSUMPTION,
        "scope": APPROX_375_SCOPE,
        "exchange": APPROX_375_EXCHANGE,
        "okx_participates": False,
        "pool_list": {"path": pool["path"], "sha256": pool["sha256"], "count": pool["count"]},
        "daily_eligibility": {"path": APPROX_375_DAILY_ELIGIBILITY, "sha256": daily_sha},
        "pool_evidence": {"path": APPROX_375_POOL_EVIDENCE, "sha256": ev_sha},
        "cutoff": cutoff,
        "cutoff_source": wx_gate.DATA_MANIFEST,
        "manifest": {"path": wx_gate.DATA_MANIFEST,
                     "manifest_sha256": (formal or {}).get("manifest_sha256"),
                     "snapshot_sha256": (formal or {}).get("snapshot_sha256"),
                     "scope": (formal or {}).get("scope")},
        "coverage": coverage,
        "spec_version": spec_version,
        "view": {"path": wx.EFFECTIVE_VIEW_PATH, "sha256": vsha, "identity_ok": ident.get("ok"),
                 "identity_problems": ident.get("problems")},
        "configs_18": (real or {}).get("configs_18"),
        "engine": (real or {}).get("engine"),
        "code": (real or {}).get("code"),
        "holdout": (real or {}).get("holdout"),
        "capital": capital,
        "risk": risk,
        "rounding": dict(APPROX_375_ROUNDING),
        "fee_model": fee,
        "spec_assumptions": spec_docs,
        "calendar": cal,
        "calendar_confirmed": dict(APPROX_375_CALENDAR_CONFIRMED),
        "calendar_status": APPROX_375_CALENDAR_STATUS,
        "calendar_superseded_note": APPROX_375_CALENDAR_SUPERSEDED_NOTE,
        "daily_loader": approximate_375_daily_loader(),
        "batch": {
            "reuse": "第22 真实账本读取链（wx_execution.run_arm → child_run_identity → "
                     "wx_gate.verify_child_run_against_ledger）",
            "child_run_record_source": wx_gate.CHILD_RUN_RECORD_SOURCE,
            "child_run_ledger_fields": list(wx_gate.CHILD_RUN_LEDGER_FIELDS),
            "sub_run_source_is_ledger_row": True,
        },
        "status": "blocked" if blocked else "resolved",
        "blocked_reasons": blocked,
        "formal_ready": False,
        "research_pass": False,
        "self_written_acceptance": False,
        "ready_for": APPROX_375_READY_FOR,
        "note": ("近似用途须由 Codex **独立接受**；本登记不构成 G01–G05 通过、不关闭任何门禁、"
                 "不伪改 526 官方历史。"),
    }


def arm_status(con, arm_id):
    """三态真实状态：policy（研究政策）/ run（运行冻结）/ implementation（实现）。"""
    view, vsha = load_view()
    resolved = resolve(con, arm_id)
    policy = "frozen" if (view and view.get("research_policy_frozen")) else "unknown"
    run = "frozen" if (resolved and resolved.get("run_frozen")) else "pending"
    return {
        "strategy_key": arm_id, "family": "F_WX",
        "policy_status": policy,
        "run_status": run,
        "implementation_status": "implemented",
        "config_status": "已配置" if resolved else "待配置",
        "config_source": resolved["source"] if resolved else None,
        "config_version": resolved["version"] if resolved else None,
        "config_hash": resolved["config_hash"] if resolved else None,
        "config_hash_check": resolved["hash_check"] if resolved else None,
        "identity_ok": resolved["identity_ok"] if resolved else None,
        "view_sha256": vsha,
        "view_sha_ok": resolved["view_sha_ok"] if resolved else None,
        "run_frozen": bool(resolved and resolved["run_frozen"]),
        "run_binding": resolved["run_binding"] if resolved else None,
        "note": "实现已就绪；run_frozen=false ⇒ 未运行放行（G01–G05 待 Codex 独立接受）",
    }
