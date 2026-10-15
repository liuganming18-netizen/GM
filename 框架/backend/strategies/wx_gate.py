# -*- coding: utf-8 -*-
"""F_WX 运行绑定门禁（G01–G05）：**配置冻结 ≠ 运行放行**；**标签 ≠ 证据**；**存在 id ≠ 接受**。

背景（2026-10-11 独立反例，逐轮收紧）：
  · `db_override_frozen_bypasses_pending_view`：DB 行 `frozen=1` 曾绕过视图 `run_frozen=false`。
  · `unhashed_gate_labels_accepted`（E07）：旧实现只写 `{"gates":{G:"pass"}}` 也 `all_pass=true`。
  · 第二轮严格反例：把各 `bindings` 置 `"invented"`、证据 `sha256="invented"`、
    `accepted_by="work"` 再自校验，旧实现仍 `all_pass=true`。
  · **收口4 独立反例（本轮修复）**：把「真实身份实算 + 接受记录校验」写在**交付目录的
    并行包装器**里，而真实消费者（`wx_config.resolve` / `strategy_runner` / `adapter`）
    仍调用本模块 ⇒ 真实放行链**未接入**。同时包装器：允许 `isolated=False` 下自定义
    合成接受索引、允许 `run=None`、比较字段不含 `run_id`。故真实放行链存在漏检。

本模块是**运行是否放行的唯一事实源**（唯一真实消费门禁入口）。真实身份重算与接受记录校验
**内联在此**，不再另设只供测试调用的并行包装器；`wx_config` / `strategy_runner` / `adapter`
/ 测试**走同一实现**。逐层 fail-closed：

  ① `sha256` 自校验：对「除 sha256 外全部内容」canonical 哈希，读回**重算**比对。
  ② `structure_ok`：`bindings` 七个维度齐备、`evidence` 每门禁含 sha256+accepted_by。
  ③ `bindings_concrete`：每个绑定必须是**结构化对象**且含**具体字段值**（64-hex / 18 配置
     生效身份 / 5000 共享资本 / 执行成本与延迟），不是任意非空标签。
  ④ `real_bindings_ok`（**真实身份实算**，本轮接入）：七个维度按**实际产物**重算并与台账
     声明**逐字段比对** ——
       · `data` = **正式全范围 manifest**（终验8）列出的**实际输入文件**（549 范围：Binance
         526 + OKX 23 的 1h/15m 行情 + funding + **1h mark** + quote sidecar）{路径: sha256}
         canonical 哈希 + manifest 自身哈希（钉住 usage_mask / generation_pit / revision /
         aux 绑定）；**不再 pilot 3 币**（显式 `pilot_root` 仅隔离漂移测试用）；
       · `code` = 实际策略/运行器源码文件逐文件 sha 汇总（**不**照抄旧证据）；
       · `engine` = **实际安装引擎源码**（freqtrade `trade_model.py`/`models.py`/`__init__.py`）
         重算（**不**照抄旧引擎证据 JSON）；
       · `configs_18` / `holdout` / `execution_cost` / `capital` = 从生效视图实算。
       任一必需文件缺失 ⇒ 该维度 `None` ⇒ 与声明不符 ⇒ 拒绝（**不**「部分非空就算成功」）。
  ⑤ `run_binding_ok`（**具体 run**，本轮接入）：本次被评估的 run 身份（`run_id` + code/data/
     engine/configs_18/holdout/execution_cost/capital）必须与**实算身份**一致；未显式给出时由
     实算身份派生（生产消费入口即如此）。
  ⑥ `evidence_verified`（**接受记录实核**）：每条证据必须
       · `file` 为真实存在文件且**实际内容 sha256 == 声明值**；
       · `accepted_by` 为**冻结 Codex 接受记录 id**（`codex_acceptance_registry.json`，其自身
         sha256 在代码中**钉死**）；
       · 该记录 `scope` **覆盖**对应门禁；
       · 该记录 `path`+`sha256` 钉住**真正审查文件**（真实存在且内容 sha 相符）；
       · 该记录 `evidence[G]` 的 `file`+`sha256` **等于**台账该门禁证据（接受记录钉住实际证据）；
       · 该记录 `run_id` == 本次 run 的 `run_id`，且 `run_sha256` == canonical(run)
         （**具体 run + 完整身份**；缺 run 绑定 ⇒ 拒绝；旧 run / 只换 run_id ⇒ 拒绝）。
     任何伪 SHA、未知路径、`accepted_by=work`、自称 codex、身份 hash 不匹配/旧 run 套用 ⇒ 不成立。
  ⑦ `synthetic=true`（测试用合成台账）⇒ **永不**研究放行。
  ⑧ **已核结构载荷**（第19 加性）：`verified_payload` 由**真实验证链**（①–⑤）结果产出，
     **真实返回 schema 恒带**（`_pending` 与成功返回均带）；未接受 ⇒ 空载荷 + 明确原因。
     消费者 `real_sources` **只读该真实字段**，不再读可能不存在的 `bindings`/`data_version`。

放行判据：
  · `all_pass`（机制层）= structure ∧ concrete ∧ evidence ∧ real_bindings ∧ run ∧ ¬synthetic
    ∧ G01–G05 全 pass（隔离正例可为 True，仅证明机制有效）。
  · `research_pass`（**研究放行**）= `all_pass` ∧ ¬isolated ∧ ¬normal_mode_custom_index_refused。
  · `run_frozen(...)` 以 `research_pass` 为准 —— 隔离 / 自定义索引**永不**研究放行。

正常模式（`isolated=False`）**只接受 Codex 冻结索引**：显式传入的 `acceptance_path`/
`acceptance_sha` 一律忽略并记录 `normal_mode_custom_index_refused=True`；本模块**不读任何
环境变量**作为索引/台账来源。合成接受索引只能由**显式 `isolated=True`** 传入，且**永不**研究放行。

纪律：本模块**只读**台账/证据/实际产物，**不推断**、**不把标签当证据**、**不自写研究 PASS**。
现阶段 G01–G05 真实 pending（冻结 Codex 接受记录 `scope` 均为空）⇒ 任何真实台账都不
`all_pass`；正例只能在**内存/临时目录**用**合成验收记录**测试，且与生产放行严格隔离。
"""
from __future__ import annotations

import hashlib
import json
import os
import re

GATE_IDS = ("G01", "G02", "G03", "G04", "G05")

# ── 路径锚点（绝对；不依赖 cwd，也不读环境变量）──────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))           # .../后端实现/strategies
BACKEND_DIR = os.path.dirname(_HERE)                         # .../后端实现
RESEARCH_ROOT = os.path.dirname(BACKEND_DIR)                 # .../网站逆向研究
SUITE_DIR = os.path.join(RESEARCH_ROOT, "最新唐奇安威科夫接通与收尾_2026-10-11")

# 默认台账路径：由 Codex 独立接受后写入；不存在即视为全 pending。
DEFAULT_BINDING_PATH = os.path.join(SUITE_DIR, "接线路整改3", "运行绑定_G01_G05.json")

# 冻结的 Codex 接受记录索引。其自身 sha256 在下方钉死；
# 每条记录再以 path+sha256 钉住对应 Codex 审查产物，scope 列出**可被接受为 PASS** 的门禁。
DEFAULT_CODEX_ACCEPTANCE_PATH = os.path.join(
    SUITE_DIR, "接线路整改3", "codex_acceptance_registry.json")
EXPECTED_CODEX_ACCEPTANCE_SHA256 = (
    "b4e0cf58333bd43b1324c996eb21b6e2fad44984f7f15cefddb3b6449379567a")

VALID_STATUS = ("pending", "pass", "fail")

# 必须绑定的身份维度（缺任一 ⇒ fail closed）。
REQUIRED_BINDINGS = ("data", "code", "engine", "configs_18", "holdout",
                     "execution_cost", "capital")

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

# 每个绑定的**具体字段**要求（`sha_fields` 须为 64-hex；`equals` 为硬等值）。
BINDING_SPEC = {
    # `snapshot_sha256` 钉住实际输入文件集合与逐文件内容；`manifest_sha256` 钉住 manifest
    # 内嵌的用途掩码/代际/PIT/修订与 aux 绑定（二者缺一 ⇒ 不放行）。
    "data": {"required": ("snapshot_sha256", "manifest_sha256", "as_of", "scope"),
             "sha_fields": ("snapshot_sha256", "manifest_sha256")},
    "code": {"required": ("source_root", "source_sha256"),
             "sha_fields": ("source_sha256",)},
    "engine": {"required": ("name", "version", "sha256"), "sha_fields": ("sha256",)},
    "configs_18": {"required": ("effective_sha256", "count"),
                   "sha_fields": ("effective_sha256",),
                   "equals": {"effective_sha256": "__EXPECTED_EFFECTIVE__", "count": 18}},
    "holdout": {"required": ("frozen_sha256", "window"), "sha_fields": ("frozen_sha256",)},
    # 执行成本**与延迟**都必须进入身份（压力组 cost2x / delay30 不得被丢弃）。
    "execution_cost": {"required": ("fee_each_side", "slippage_each_side",
                                    "signal_execution_delay_minutes",
                                    "signal_exit_delay_minutes", "sha256"),
                       "sha_fields": ("sha256",)},
    "capital": {"required": ("initial_equity_USDT", "shared_across", "leverage"),
                "equals": {"initial_equity_USDT": 5000}},
}

# ── 真实身份实算所用的**实际产物**（只读；全部相对 BACKEND_DIR / SUITE_DIR）──────
CODE_SOURCE_FILES = (
    "strategies/wx_config.py", "strategies/wx_donchian_wyckoff.py",
    "strategies/wx_engine.py", "strategies/wx_execution.py",
    "strategies/wx_gate.py", "strategies/wx_ledger.py", "strategy_runner.py",
)
_PILOT = os.path.join(SUITE_DIR, "数据终验与输入试点2")

# ── 正式全范围输入 manifest（终验8；**不再 pilot_3coins**）───────────────────────
# 由 `正式回测输入补齐与终验8/07_正式研究manifest.py` 从**实际输入文件**逐文件实算 sha256
# 生成，覆盖冻结 549 范围（Binance 526 + OKX 23）的全部行情 / aux（funding、1h mark）/
# quote sidecar，并内嵌 usage_mask、generation_pit、revision 与 aux 绑定。生产 `data`
# 维度 = 该 manifest 的**实际输入文件哈希集合**（`snapshot_sha256`）+ manifest 自身哈希
# （`manifest_sha256`）。`pilot_datadir`/`DATA_EXPECTED_FILES` 仅在显式 `pilot_root` 时使用。
FORMAL_DIR = os.path.join(SUITE_DIR, "正式回测输入补齐与终验8")
DATA_MANIFEST = os.path.join(FORMAL_DIR, "07_正式研究manifest.json")
# 隔离漂移测试用 pilot 目录（生产**不使用**）。
DATA_INPUT_DIRS = (os.path.join(_PILOT, "pilot_datadir"),
                   os.path.join(_PILOT, "pilot_sidecar"))
# pilot 必需输入文件（仅 `pilot_root` 覆盖路径使用；缺任一 ⇒ data 维度 None）。
DATA_EXPECTED_FILES = tuple(sorted([
    "pilot_datadir/futures/BTC_USDT_USDT-15m-futures.feather",
    "pilot_datadir/futures/BTC_USDT_USDT-1h-futures.feather",
    "pilot_datadir/futures/CRO_USDT_USDT-15m-futures.feather",
    "pilot_datadir/futures/CRO_USDT_USDT-1h-futures.feather",
    "pilot_datadir/futures/ETH_USDT_USDT-15m-futures.feather",
    "pilot_datadir/futures/ETH_USDT_USDT-1h-futures.feather",
    "pilot_sidecar/BTC_USDT_USDT-15m-sidecar.json",
    "pilot_sidecar/BTC_USDT_USDT-1h-sidecar.json",
    "pilot_sidecar/CRO_USDT_USDT-15m-sidecar.json",
    "pilot_sidecar/CRO_USDT_USDT-1h-sidecar.json",
    "pilot_sidecar/ETH_USDT_USDT-15m-sidecar.json",
    "pilot_sidecar/ETH_USDT_USDT-1h-sidecar.json",
]))

# 实际安装引擎相关源码（**重算**，不照抄旧证据 JSON）。
FREQTRADE_DIR = ("/Users/boos/.workbuddy-ai/binaries/python/envs/freqtrade/"
                 "lib/python3.13/site-packages/freqtrade")
ENGINE_SOURCE_FILES = ("persistence/trade_model.py", "persistence/models.py", "__init__.py")

# run 身份字段（code/data/engine/configs18/holdout/执行费用及延迟/共享 5000）。
RUN_IDENTITY_FIELDS = ("code_sha256", "data_snapshot_sha256", "engine_sha256",
                       "configs_18_sha256", "holdout_sha256", "execution_cost_sha256",
                       "capital_USDT")


def _canon(obj):
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8")).hexdigest()


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _payload_for_hash(doc):
    """台账哈希口径 = 除 `sha256` 字段外的**全部内容**（gates+bindings+evidence+meta）。"""
    if not isinstance(doc, dict):
        return doc
    return {k: v for k, v in doc.items() if k != "sha256"}


def _pending(reason, self_check="no_ledger", source=None, approx=None):
    res = {"gates": {g: "pending" for g in GATE_IDS},
           "all_pass": False, "research_pass": False, "source": source,
           "sha256": None, "self_check": self_check, "structure_ok": False,
           "bindings_concrete": False, "evidence_verified": False,
           "real_bindings_ok": False, "real_bindings_detail": {},
           "run_binding_ok": False, "run_binding_detail": {},
           "run_identity": None, "isolated": False,
           "normal_mode_custom_index_refused": False,
           "codex_acceptance": {"status": "not_loaded", "detail": {}},
           # 第23 加性：375 近似用途元数据（**不**参与放行判据；由调用方传入或 None）。
           "approximate_375": approx,
           # 第27/30 加性：375 逐日名单 loader 与实际运行身份（**不**参与放行判据）。
           # 修正 124 第4点：早返回/待接受分支**同样恒带** `daily_loader_375`（此前缺 ⇒ 字段声明不实）。
           "daily_loader_375": _daily_loader_375_safe(),
           "approximate_375_actual_run": _actual_run_375_safe(),
           # 第34 加性：375 完整输入版本与实际运行接通身份（完整输入与使用凭证分列；不入放行判据）。
           "approximate_375_complete_input_actual_run": _complete_input_actual_run_375_safe(),
           "reason": reason}
    # 第19：**真实返回 schema 恒带** `verified_payload`（此处未接受 ⇒ 空载荷 + 明确原因）。
    res["verified_payload"] = _verified_payload(
        run_identity=None, bindings=None, concrete_detail={}, real_detail={},
        run_ok=False, structure_ok=False, self_check=self_check, reason=reason,
        source=source, sha256=None, synthetic=False)
    return res


# ══════════════════════════════════════════════════════════════════════════
# ① 真实身份实算（按**实际产物**重算；缺失 ⇒ 该维度 None ⇒ fail closed）
# ══════════════════════════════════════════════════════════════════════════
def _engine_version(freqtrade_dir=None):
    d = freqtrade_dir or FREQTRADE_DIR
    init = os.path.join(d, "__init__.py")
    if not os.path.exists(init):
        return None
    try:
        with open(init, encoding="utf-8") as f:
            for line in f:
                m = re.match(r'^__version__\s*=\s*["\']([^"\']+)["\']', line.strip())
                if m:
                    return m.group(1)
    except OSError:
        return None
    return None


def _formal_data_binding():
    """正式全范围 manifest → `data` 维度（只读）。

    `snapshot_sha256` = 对 manifest 列出的**全部实际输入文件** {绝对路径: sha256} 的 canonical
    哈希（钉住输入文件集合与逐文件内容）；`manifest_sha256` = manifest 文件自身哈希（钉住
    usage_mask / generation_pit / revision / aux 绑定与 availability_mask）；`scope` = 范围、
    文件数与**官方不可得**计数。manifest 缺失、格式非法、任一 sha 非 64-hex、**任一 manifest
    项在磁盘缺失**、或掩码不完备（present + official_unavailable ≠ total）⇒ 返回 None
    （fail closed；不「部分非空就算成功」）。`availability == "official_unavailable"` 的项
    必须是**官方不发布**且**带证据 sha256 引用**（真实 HTTP 404 探测），否则同样 fail closed。
    """
    if not os.path.exists(DATA_MANIFEST):
        return None
    try:
        with open(DATA_MANIFEST, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    files = doc.get("files") if isinstance(doc, dict) else None
    if not isinstance(files, list) or not files:
        return None
    hashes = {}
    n_official_unavailable = 0
    for rec in files:
        if not isinstance(rec, dict):
            return None
        rel, sha = rec.get("abs"), rec.get("sha256")
        if rec.get("availability") == "official_unavailable":
            # 官方**不发布**该子数据集（真实 HTTP 404 探测证据，见 manifest.availability_mask
            # 与 12b_缺失mark官方证据与补缺.json）：不计入「必需输入文件集合」，也不伪造 sha。
            # 但**必须**带证据引用，否则 fail closed（不得凭一个标签就豁免）。
            ev = rec.get("evidence_sha256")
            if not isinstance(ev, str) or not _HEX64.match(ev):
                return None
            n_official_unavailable += 1
            continue
        if not rel or not isinstance(sha, str) or not _HEX64.match(sha):
            return None
        if not os.path.isfile(rel):
            return None                       # 任一 manifest 项缺失 ⇒ fail closed
        hashes[rel] = sha
    if len(hashes) + n_official_unavailable != len(files):
        return None                           # 掩码必须**完备**：present+official_unavailable==total
    sc = doc.get("scope") or {}
    return {
        "snapshot_sha256": _canon(hashes),
        "manifest_sha256": _sha256_file(DATA_MANIFEST),
        "as_of": doc.get("cutoff"),
        "scope": "formal_%s_symbols_%d_files_%d_official_unavailable" % (
            sc.get("total", 0), len(hashes), n_official_unavailable),
        "n_files": len(hashes),
        "n_official_unavailable": n_official_unavailable,
    }


def compute_real_bindings(backend=None, freqtrade_dir=None, pilot_root=None):
    """从**实际产物**实算七个身份维度（只读）。必需文件缺失 ⇒ 对应项 None（fail closed）。

    与交付目录无关：本函数位于真实门禁模块内，`wx_config`/`strategy_runner`/`adapter`/测试
    全部经此同一实现取实算身份。`backend` / `freqtrade_dir` / `pilot_root` 仅供**隔离漂移测试**
    指向临时副本；生产默认全部指向固定真实产物（`data` = 正式全范围 manifest，非 pilot 3 币）。
    """
    backend = backend or BACKEND_DIR
    out = {"code": None, "configs_18": None, "engine": None, "execution_cost": None,
           "capital": None, "data": None, "holdout": None, "provenance": {}}

    # ── code：逐文件实算 sha，再对 {相对名: sha} canonical 汇总（全在 ⇒ 才有效）────
    files = {}
    for rel in CODE_SOURCE_FILES:
        fp = os.path.join(backend, rel)
        if os.path.exists(fp):
            files[rel] = _sha256_file(fp)
    if len(files) == len(CODE_SOURCE_FILES):
        out["code"] = {"source_root": backend, "source_sha256": _canon(files)}
        out["provenance"]["code_files"] = {rel: files[rel] for rel in sorted(files)}

    # ── data：生产 = 正式全范围 manifest（549 范围实际输入文件哈希集合 + manifest 哈希）；
    #          显式 `pilot_root` = 旧 pilot 12 文件语义（仅隔离漂移测试）────────────────
    if pilot_root is not None:
        data_files = {}
        for rel in DATA_EXPECTED_FILES:
            fp = os.path.join(pilot_root, rel)
            if os.path.exists(fp):
                data_files[rel] = _sha256_file(fp)
        if len(data_files) == len(DATA_EXPECTED_FILES):
            as_of, scope = None, "pilot_%d_files" % len(DATA_EXPECTED_FILES)
            manifest = os.path.join(pilot_root, "02_pilot_export_readin.json")
            if os.path.exists(manifest):
                try:
                    with open(manifest, encoding="utf-8") as f:
                        md = json.load(f)
                    as_of = md.get("cutoff")
                    scope = "pilot_" + str(len(md.get("samples_requested") or [])) + "coins"
                except (OSError, ValueError):
                    as_of = None
            out["data"] = {"snapshot_sha256": _canon(data_files), "as_of": as_of, "scope": scope}
            out["provenance"]["data_files"] = {rel: data_files[rel]
                                               for rel in sorted(data_files)}
            out["provenance"]["data_manifest"] = manifest
    else:
        formal = _formal_data_binding()
        if formal is not None:
            out["data"] = formal
            out["provenance"]["data_manifest"] = DATA_MANIFEST
            out["provenance"]["data_files_count"] = formal["n_files"]

    # ── configs_18 / execution_cost / capital / holdout：从生效视图**重算** ────────
    from . import wx_config  # 延迟导入避免循环；单一事实源（18 生效身份 hash）
    view, vsha = wx_config.load_view()
    if view is not None:
        eff = wx_config.effective_configs_check(view)
        out["configs_18"] = {"effective_sha256": eff["sha256"], "count": eff["count"]}
        out["provenance"]["view_sha256"] = vsha
        ex = view.get("execution") or {}
        fee = ex.get("fee_each_side")
        slip = ex.get("slippage_each_side")
        ed = ex.get("signal_execution_delay_minutes")
        xd = ex.get("signal_exit_delay_minutes")
        out["execution_cost"] = {
            "fee_each_side": fee, "slippage_each_side": slip,
            "signal_execution_delay_minutes": ed, "signal_exit_delay_minutes": xd,
            "sha256": _canon({"fee_each_side": fee, "slippage_each_side": slip,
                              "signal_execution_delay_minutes": ed,
                              "signal_exit_delay_minutes": xd})}
        cm = view.get("capital_model") or {}
        out["capital"] = {
            "initial_equity_USDT": cm.get("total_initial_equity_USDT_per_research_arm"),
            "shared_across": "binance,okx",
            "leverage": (view.get("risk") or {}).get("leverage", 1)}
        sample = view.get("sample") or {}
        out["holdout"] = {
            "frozen_sha256": _canon(sample),
            "window": "DEV/VAL/FINAL split " + "/".join(str(x) for x in sample.get("split", []))}

    # ── engine：**实际安装引擎源码**重算（不照抄旧引擎证据 JSON）─────────────────
    ft = freqtrade_dir or FREQTRADE_DIR
    eng_files = {}
    for rel in ENGINE_SOURCE_FILES:
        fp = os.path.join(ft, rel)
        if os.path.exists(fp):
            eng_files[rel] = _sha256_file(fp)
    if len(eng_files) == len(ENGINE_SOURCE_FILES):
        out["engine"] = {"name": "freqtrade", "version": _engine_version(ft),
                         "sha256": _canon(eng_files)}
        out["provenance"]["engine_files"] = {rel: eng_files[rel] for rel in sorted(eng_files)}
    return out


def current_run_identity(real=None, backend=None, freqtrade_dir=None, pilot_root=None):
    """本次研究 run 的**具体身份**（run_id 内容绑定 + 七维身份）。

    `run_id` 由实算身份内容派生 ⇒ 任一实际产物漂移都会改变 run_id（旧 run 无法套用）。
    """
    real = real if real is not None else compute_real_bindings(backend, freqtrade_dir, pilot_root)
    if not all(isinstance(real.get(k), dict) for k in REQUIRED_BINDINGS):
        return None
    run = {
        "run_id": None,
        "code_sha256": (real["code"] or {}).get("source_sha256"),
        "data_snapshot_sha256": (real["data"] or {}).get("snapshot_sha256"),
        "engine_sha256": (real["engine"] or {}).get("sha256"),
        "configs_18_sha256": (real["configs_18"] or {}).get("effective_sha256"),
        "holdout_sha256": (real["holdout"] or {}).get("frozen_sha256"),
        "execution_cost_sha256": (real["execution_cost"] or {}).get("sha256"),
        "capital_USDT": (real["capital"] or {}).get("initial_equity_USDT"),
    }
    run["run_id"] = "wx_research:" + _canon({k: run[k] for k in RUN_IDENTITY_FIELDS})[:16]
    return run


def verify_bindings(declared, real):
    """逐维比对**声明值**与**实算值**；仅比对门禁规格要求的具体字段。"""
    per = {}
    ok = True
    for key in REQUIRED_BINDINGS:
        dv = (declared or {}).get(key)
        rv = (real or {}).get(key)
        if not isinstance(dv, dict) or not isinstance(rv, dict):
            per[key] = {"ok": False, "why": "missing_declared_or_real"}
            ok = False
            continue
        spec = BINDING_SPEC.get(key, {})
        fields = tuple(spec.get("required", ())) + tuple((spec.get("equals") or {}).keys())
        diffs = {f: {"declared": dv.get(f), "real": rv.get(f)}
                 for f in fields if f in rv and dv.get(f) != rv.get(f)}
        per[key] = {"ok": not diffs, "diffs": diffs}
        ok = ok and not diffs
    return ok, per


def verify_run_binding(run, real, declared):
    """校验**本次 run** 与实算身份、台账声明**逐字段一致**（禁止旧 run / 只换 run_id 套用）。"""
    if not isinstance(run, dict) or not run.get("run_id"):
        return False, {"error": "run_missing"}
    detail = {"run_id": run.get("run_id"), "fields": {}}
    ok = True
    real_run = current_run_identity(real=real)
    for f in RUN_IDENTITY_FIELDS:
        rv = (real_run or {}).get(f)
        dv = (declared or {}).get(_field_to_binding(f), {})
        dv = dv.get(_field_to_binding_field(f)) if isinstance(dv, dict) else None
        agree_real = (run.get(f) == rv)
        agree_decl = (run.get(f) == dv)
        detail["fields"][f] = {"run": run.get(f), "real": rv, "declared": dv,
                               "agree_real": agree_real, "agree_declared": agree_decl}
        ok = ok and agree_real and agree_decl
    return ok, detail


_FIELD_TO_BINDING = {
    "code_sha256": ("code", "source_sha256"),
    "data_snapshot_sha256": ("data", "snapshot_sha256"),
    "engine_sha256": ("engine", "sha256"),
    "configs_18_sha256": ("configs_18", "effective_sha256"),
    "holdout_sha256": ("holdout", "frozen_sha256"),
    "execution_cost_sha256": ("execution_cost", "sha256"),
    "capital_USDT": ("capital", "initial_equity_USDT"),
}


def _field_to_binding(f):
    return _FIELD_TO_BINDING[f][0]


def _field_to_binding_field(f):
    return _FIELD_TO_BINDING[f][1]


# ══════════════════════════════════════════════════════════════════════════
# ② 接受记录索引（正常模式**只**接受冻结索引；隔离模式才允许自定义）
# ══════════════════════════════════════════════════════════════════════════
def _load_codex_acceptance(path=None, expected_sha=None, *, isolated=False):
    """读取并校验冻结的 Codex 接受记录索引。返回 (records_by_id, status, detail)。

    · **正常模式（isolated=False）**：忽略显式 `path`/`expected_sha`，恒用冻结索引与其钉死 sha；
      **不读任何环境变量**。⇒ 不存在「用显式参数或环境把合成索引接进正常研究」的旁路。
    · **隔离模式（isolated=True）**：允许显式 `path`+`expected_sha`（合成接受记录）；该模式下
      调用方必须显式标记隔离，且研究放行恒为 False。
    · 索引文件 sha256 必须等于**钉死值**（否则 drift ⇒ 空 records ⇒ 任何证据不成立）。
    """
    if isolated:
        p = path or DEFAULT_CODEX_ACCEPTANCE_PATH
        pinned = expected_sha or EXPECTED_CODEX_ACCEPTANCE_SHA256
    else:
        p = DEFAULT_CODEX_ACCEPTANCE_PATH
        pinned = EXPECTED_CODEX_ACCEPTANCE_SHA256
    if not os.path.exists(p):
        return {}, "missing", {"path": p}
    try:
        actual = _sha256_file(p)
    except OSError as exc:
        return {}, "unreadable", {"path": p, "error": str(exc)}
    if actual != pinned:
        return {}, "drift", {"path": p, "actual": actual, "expected": pinned}
    try:
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as exc:
        return {}, "unreadable", {"path": p, "error": str(exc)}
    by_id = {}
    for rec in (doc.get("records") or []):
        rid = rec.get("id")
        if not rid:
            continue
        by_id[rid] = rec
    return by_id, "ok", {"path": p, "sha256": actual, "count": len(by_id)}


def _verify_evidence_file(ev):
    """校验单条证据：文件存在 + 实际内容 sha256 == 声明值。返回 (ok, detail)。"""
    if not isinstance(ev, dict):
        return False, {"error": "evidence_not_object"}
    fp = ev.get("file")
    declared = ev.get("sha256")
    if not fp or not isinstance(fp, str) or not os.path.isabs(fp):
        return False, {"error": "evidence_file_path_missing_or_not_absolute", "file": fp}
    if not os.path.exists(fp) or not os.path.isfile(fp):
        return False, {"error": "evidence_file_not_found", "file": fp}
    try:
        actual = _sha256_file(fp)
    except OSError as exc:
        return False, {"error": "evidence_file_unreadable", "file": fp, "detail": str(exc)}
    if declared != actual:
        return False, {"error": "evidence_sha_mismatch", "file": fp,
                       "declared": declared, "actual": actual}
    return True, {"file": fp, "sha256": actual}


def _verify_gate_evidence(ev, rec, gate, run):
    """单门禁的**接受记录实核**：真实证据文件 ∧ 记录 scope ∧ 记录钉住证据 ∧ 记录钉住 run。

    返回 (ok, detail)。任一不成立 ⇒ 该证据不成立（fail closed）。
    """
    ok_file, det = _verify_evidence_file(ev)
    if not ok_file:
        return False, {"ok": False, "stage": "evidence_file", "detail": det}
    if not isinstance(rec, dict):
        return False, {"ok": False, "stage": "record_missing"}
    if gate not in (rec.get("scope") or []):
        return False, {"ok": False, "stage": "record_scope_miss",
                       "record_scope": rec.get("scope")}
    # 记录必须钉住**实际证据** path+SHA（不能只凭存在的 id/scope 背书）。
    rec_ev = (rec.get("evidence") or {}).get(gate)
    if not isinstance(rec_ev, dict):
        return False, {"ok": False, "stage": "record_evidence_missing"}
    if rec_ev.get("file") != ev.get("file") or rec_ev.get("sha256") != ev.get("sha256"):
        return False, {"ok": False, "stage": "record_evidence_not_pinned",
                       "record_evidence": rec_ev, "ledger_evidence": {
                           "file": ev.get("file"), "sha256": ev.get("sha256")}}
    # 记录必须钉住**真正审查文件**（path+sha 真实可核）。
    rp, rs = rec.get("path"), rec.get("sha256")
    if not rp or not isinstance(rp, str) or not os.path.isfile(rp):
        return False, {"ok": False, "stage": "reviewed_artifact_missing", "path": rp}
    try:
        rsha = _sha256_file(rp)
    except OSError as exc:
        return False, {"ok": False, "stage": "reviewed_artifact_unreadable",
                       "path": rp, "detail": str(exc)}
    if rs != rsha:
        return False, {"ok": False, "stage": "reviewed_artifact_sha_mismatch",
                       "declared": rs, "actual": rsha}
    # 记录必须钉住**具体 run + 完整身份**（缺 run ⇒ 拒绝；旧 run / 只换 run_id ⇒ 拒绝）。
    if not isinstance(run, dict) or not run.get("run_id"):
        return False, {"ok": False, "stage": "run_missing"}
    if rec.get("run_id") != run.get("run_id"):
        return False, {"ok": False, "stage": "record_run_id_mismatch",
                       "record_run_id": rec.get("run_id"), "run_id": run.get("run_id")}
    if rec.get("run_sha256") != _canon(run):
        return False, {"ok": False, "stage": "record_run_sha_mismatch",
                       "record_run_sha256": rec.get("run_sha256"),
                       "expected_run_sha256": _canon(run)}
    return True, {"ok": True, "stage": "verified", "file_check": det,
                  "record_id": rec.get("id"), "run_id": run.get("run_id")}


def _bindings_concrete(bindings):
    """身份**实核**：每维度结构化且含具体值；返回 (ok, detail_by_key, missing_list)。"""
    from . import wx_config  # 延迟导入避免循环；单一事实源（18 生效身份 hash）
    expected_eff = wx_config.EXPECTED_EFFECTIVE_CONFIGS_SHA256
    detail, bad = {}, []
    for key in REQUIRED_BINDINGS:
        spec = BINDING_SPEC[key]
        obj = bindings.get(key) if isinstance(bindings, dict) else None
        problems = []
        if not isinstance(obj, dict):
            problems.append("not_a_structured_object")
        else:
            for fld in spec.get("required", ()):
                if obj.get(fld) in (None, "", {}, []):
                    problems.append(f"missing_field:{fld}")
            for fld in spec.get("sha_fields", ()):
                v = obj.get(fld)
                if isinstance(v, str) and not _HEX64.match(v):
                    problems.append(f"not_64hex:{fld}")
            for fld, want in (spec.get("equals") or {}).items():
                if want == "__EXPECTED_EFFECTIVE__":
                    want = expected_eff
                if obj.get(fld) != want:
                    problems.append(f"not_equal:{fld}(got={obj.get(fld)!r},want={want!r})")
        detail[key] = {"ok": not problems, "problems": problems}
        if problems:
            bad.append(key)
    return (not bad), detail, bad


# ══════════════════════════════════════════════════════════════════════════
# ②b 已核结构载荷（第19 加性；真实返回 schema 恒带，消费者只读此字段）
# ══════════════════════════════════════════════════════════════════════════
VERIFIED_PAYLOAD_SCHEMA = "wx_gate.verified_payload/v1"


def _verified_payload(*, run_identity, bindings, concrete_detail, real_detail, run_ok,
                      structure_ok, self_check, reason, source, sha256, synthetic):
    """第19 **加性只读**：由**真实验证链**结果产出「已核结构载荷」。

    内容来源明确 = 本次 `binding()` 实际完成「① sha256 指纹自校验 ∧ ② 结构 ∧ ③ 身份具体
    （concrete）∧ ④ 实算身份一致（real）∧ ⑤ 当前 run 一致」后，**台账文件**（独立来源，
    **非调用方**）声明的七维绑定值。任一未过 ⇒ 载荷为空并给出**原因**（fail closed）；
    `synthetic=true` 永不作为已核载荷。

    **真实返回 schema 恒带**该键（`_pending` 与成功返回均带）⇒ 消费者（`real_sources` /
    `wx_execution`）只读该真实字段，**不再依赖可能不存在的 `bindings`/`data_version` 键**。
    本函数不改既有返回键与判据（纯加性）。
    """
    concrete_ok = {k: bool((v or {}).get("ok")) for k, v in (concrete_detail or {}).items()
                   if isinstance(v, dict)}
    real_ok = {k: bool((v or {}).get("ok")) for k, v in (real_detail or {}).items()
               if isinstance(v, dict)}
    accepted = bool(structure_ok and run_ok and not synthetic
                    and all(concrete_ok.get(d) for d in REQUIRED_BINDINGS)
                    and all(real_ok.get(d) for d in REQUIRED_BINDINGS))
    verified = {}
    if accepted:
        for d in REQUIRED_BINDINGS:
            if isinstance((bindings or {}).get(d), dict):
                verified[d] = (bindings or {})[d]
        accepted = (len(verified) == len(REQUIRED_BINDINGS))
    if accepted:
        why = None
    elif synthetic:
        why = "synthetic_ledger"
    elif not structure_ok:
        why = "structure_incomplete:" + str(self_check or "unknown")
    elif not run_ok:
        why = "run_binding_mismatch"
    else:
        bad = [d for d in REQUIRED_BINDINGS
               if not (concrete_ok.get(d) and real_ok.get(d))]
        why = "content_not_verified:" + ",".join(bad or ["unknown"])
    return {
        "schema": VERIFIED_PAYLOAD_SCHEMA,
        "accepted": accepted,
        "reason": why,
        "self_check": self_check,
        "run_id": (run_identity or {}).get("run_id"),
        "run_identity": dict(run_identity or {}),
        "verified_bindings": verified,
        "verified_dimensions": sorted(verified),
        "unverified_dimensions": [d for d in REQUIRED_BINDINGS if d not in verified],
        "consumer_dims_without_gate_dimension": list(CONSUMER_DIMS_NO_GATE_DIMENSION),
        "source": source, "sha256": sha256,
    }


# ══════════════════════════════════════════════════════════════════════════
# ③ 唯一门禁入口
# ══════════════════════════════════════════════════════════════════════════
def binding(path=None, *, acceptance_path=None, acceptance_sha=None, run=None,
            isolated=False, real_bindings=None, backend=None, freqtrade_dir=None,
            pilot_root=None):
    """读取并**逐层校验**运行绑定台账。返回 dict（含各层判定与理由）。

    台账格式（严格）：
      {
        "gates":     {"G01":"pass", ...},
        "bindings":  {"data": {"snapshot_sha256":"<64hex>","as_of":"...","scope":"..."},
                      "code": {"source_root":"...","source_sha256":"<64hex>"},
                      "engine": {"name":"freqtrade","version":"2026.9","sha256":"<64hex>"},
                      "configs_18": {"effective_sha256":"<64hex>","count":18},
                      "holdout": {"frozen_sha256":"<64hex>","window":"..."},
                      "execution_cost": {"fee_each_side":0.0005,"slippage_each_side":0.0005,
                                         "signal_execution_delay_minutes":0,
                                         "signal_exit_delay_minutes":0,"sha256":"<64hex>"},
                      "capital": {"initial_equity_USDT":5000,"shared_across":"binance,okx",
                                  "leverage":1}},
        "evidence":  {"G01": {"file":"<abs path>","sha256":"<实际文件 sha>",
                              "accepted_by":"<codex record id>"}, ...},
        "synthetic": false,
        "sha256":    "<canon(除 sha256 外全部内容)>"
      }

    接受记录（`codex_acceptance_registry.json` 的 records[]）需含：
      id / path（真正审查文件）/ sha256 / scope / run_id / run_sha256
      / evidence: {G: {file, sha256}}（钉住实际证据）。
    当前冻结索引 scope 全空 ⇒ 任何真实台账都**不放行**（G01–G05 pending）。

    `run`：本次被评估的 run 身份；缺省由实算身份派生（生产消费入口即如此）。
    `isolated`：显式隔离模式（测试用合成接受索引）；**永不**研究放行。

    **第19 加性返回**：返回 dict **恒带** `verified_payload`（见 `_verified_payload`）——由真实
    验证链产出、来源明确（独立台账）的已核结构载荷；未接受 ⇒ 空载荷 + `reason`。既有返回键与
    判据**不变**。
    """
    # 正常模式：台账路径不读环境变量；自定义接受索引被忽略并如实记录。
    p = path or DEFAULT_BINDING_PATH
    custom_index_requested = (acceptance_path is not None) or (acceptance_sha is not None)
    normal_mode_custom_index_refused = bool(custom_index_requested and not isolated)
    # 第23 加性：375 近似用途元数据（与 G01–G05 台账**无关**，故所有返回路径都带；失败 ⇒ None）。
    # 只在真正返回的那条路径上算一次（避免重复 `compute_real_bindings`）。
    if not os.path.exists(p):
        res = _pending(f"运行绑定台账不存在：{p}（视为 G01–G05 全 pending，不放行）",
                       approx=_approx_375_safe(real=real_bindings))
        res["isolated"] = bool(isolated)
        res["normal_mode_custom_index_refused"] = normal_mode_custom_index_refused
        return res
    try:
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as exc:
        res = _pending(f"运行绑定台账不可读/非法 JSON：{exc}", self_check="unreadable",
                       approx=_approx_375_safe(real=real_bindings))
        res["isolated"] = bool(isolated)
        res["normal_mode_custom_index_refused"] = normal_mode_custom_index_refused
        return res

    gates_raw = doc.get("gates") if isinstance(doc, dict) else None
    if not isinstance(gates_raw, dict):
        res = _pending("台账缺少 gates 映射", self_check="structure_incomplete",
                       approx=_approx_375_safe(real=real_bindings))
        res["isolated"] = bool(isolated)
        res["normal_mode_custom_index_refused"] = normal_mode_custom_index_refused
        return res
    gates = {g: (gates_raw.get(g) if gates_raw.get(g) in VALID_STATUS else "pending")
             for g in GATE_IDS}

    # ① sha256 自校验（缺失即 fail closed）
    recomputed = _canon(_payload_for_hash(doc))
    declared = doc.get("sha256")
    drift = (declared is None) or (declared != recomputed)

    # ② 结构完整（绑定/证据字段非空即可）
    bindings = doc.get("bindings")
    missing_bind = [k for k in REQUIRED_BINDINGS
                    if not isinstance(bindings, dict)
                    or bindings.get(k) in (None, "", {}, [])]
    bindings_ok = not missing_bind
    evidence = doc.get("evidence")
    ev_missing = []
    for g in GATE_IDS:
        ev = evidence.get(g) if isinstance(evidence, dict) else None
        if not (isinstance(ev, dict) and ev.get("sha256") and ev.get("accepted_by")):
            ev_missing.append(g)
    evidence_ok = not ev_missing
    synthetic = bool(doc.get("synthetic"))
    structure_ok = (not drift) and bindings_ok and evidence_ok

    # ③ 身份实核（具体字段值）
    concrete_ok, concrete_detail, bad_bind = _bindings_concrete(bindings or {})

    # ④ 真实身份实算（按实际产物重算并与声明逐字段比对）
    real = real_bindings if real_bindings is not None else \
        compute_real_bindings(backend, freqtrade_dir, pilot_root)
    real_ok, real_detail = verify_bindings(bindings or {}, real)

    # ⑤ 具体 run 绑定（与实算身份、台账声明一致）
    run_eff = run if run is not None else current_run_identity(real=real)
    run_ok, run_detail = verify_run_binding(run_eff, real, bindings or {})

    # ⑥ 接受证据实核（真实文件 SHA + 冻结 Codex 记录 + 门禁范围 + 钉住证据 + 钉住 run）
    records, reg_status, reg_detail = _load_codex_acceptance(
        acceptance_path, acceptance_sha, isolated=isolated)
    ev_verified, ev_detail = True, {}
    for g in GATE_IDS:
        if g in ev_missing:
            ev_detail[g] = {"ok": False, "error": "evidence_missing"}
            ev_verified = False
            continue
        ev = evidence[g]
        rid = ev.get("accepted_by")
        rec = records.get(rid)
        ok, det = _verify_gate_evidence(ev, rec, g, run_eff)
        det = dict(det)
        det["accepted_by"] = rid
        det["codex_record_found"] = bool(rec)
        ev_detail[g] = det
        ev_verified = ev_verified and ok
    evidence_verified = ev_verified and (reg_status == "ok")

    if declared is None:
        self_check = "unhashed"
    elif drift:
        self_check = "drift"
    elif not structure_ok:
        self_check = "structure_incomplete"
    elif synthetic:
        self_check = "synthetic"
    elif not concrete_ok:
        self_check = "bindings_not_concrete"
    elif not real_ok:
        self_check = "real_bindings_mismatch"
    elif not run_ok:
        self_check = "run_binding_mismatch"
    elif not evidence_verified:
        self_check = "evidence_unverified"
    else:
        self_check = "ok"

    all_pass = bool(structure_ok and concrete_ok and real_ok and run_ok
                    and evidence_verified and not synthetic
                    and all(gates[g] == "pass" for g in GATE_IDS))
    research_pass = bool(all_pass and not isolated
                         and not normal_mode_custom_index_refused)

    if all_pass and not research_pass:
        if isolated:
            reason = "隔离模式（合成接受记录）：机制有效但不构成研究放行"
        else:
            reason = "正常模式拒绝自定义接受索引：不构成研究放行"
    elif all_pass:
        reason = None
    elif declared is None:
        reason = "台账缺 sha256（仅标签不构成证据）：不放行"
    elif drift:
        reason = "台账自校验漂移（sha256 与内容不一致）：不放行"
    elif not bindings_ok:
        reason = "台账缺身份绑定：" + ",".join(missing_bind) + "：不放行"
    elif not evidence_ok:
        reason = "台账缺证据/独立接受方：" + ",".join(ev_missing) + "：不放行"
    elif not concrete_ok:
        reason = "身份绑定非具体值（未实核）：" + ",".join(bad_bind) + "：不放行"
    elif not real_ok:
        bad = [k for k, v in real_detail.items() if not v.get("ok")]
        reason = "身份声明与**实际产物实算**不符：" + ",".join(bad) + "：不放行"
    elif not run_ok:
        reason = "具体 run 与实算身份/台账不一致：" + str(run_detail.get("error") or
                                                       run_detail.get("fields")) + "：不放行"
    elif not evidence_verified:
        reason = ("接受证据未通过实核（真实文件 SHA / 冻结 Codex 记录 / 门禁范围 / 钉住证据与 "
                  f"具体 run）：registry={reg_status}：不放行")
    elif synthetic:
        reason = "合成测试台账（synthetic=true）永不作为研究放行：不放行"
    else:
        reason = "存在非 pass 门禁：" + ",".join(g for g in GATE_IDS if gates[g] != "pass")

    return {
        "gates": gates, "all_pass": all_pass, "research_pass": research_pass,
        "structure_ok": structure_ok, "source": p, "sha256": recomputed,
        "declared_sha256": declared, "self_check": self_check,
        "bindings_ok": bindings_ok, "missing_bindings": missing_bind,
        # 第18 加性：暴露台账**声明**的七维绑定（仅当 `structure_ok` 且为 dict）——供消费者按
        # 真实结构提取「已实核来源」；**声明本身不等于已核**（须结合 real_bindings_detail/run_binding）。
        "bindings": bindings if (structure_ok and isinstance(bindings, dict)) else None,
        # 第19 加性：**真实验证链**产出的已核结构载荷（恒存在；未接受 ⇒ 空 + 原因）。消费者
        # （`real_sources`/`wx_execution`）只读此字段，不再读可能不存在的 `bindings`。
        "verified_payload": _verified_payload(
            run_identity=run_eff, bindings=bindings if isinstance(bindings, dict) else None,
            concrete_detail=concrete_detail, real_detail=real_detail, run_ok=run_ok,
            structure_ok=structure_ok, self_check=self_check, reason=reason,
            source=p, sha256=recomputed, synthetic=synthetic),
        "evidence_ok": evidence_ok, "missing_evidence": ev_missing,
        "synthetic": synthetic, "reason": reason,
        "bindings_concrete": concrete_ok, "bindings_concrete_detail": concrete_detail,
        "real_bindings_ok": real_ok, "real_bindings_detail": real_detail,
        "run_binding_ok": run_ok, "run_binding_detail": run_detail,
        "run_identity": run_eff, "isolated": bool(isolated),
        "normal_mode_custom_index_refused": normal_mode_custom_index_refused,
        "evidence_verified": evidence_verified, "evidence_verified_detail": ev_detail,
        "codex_acceptance": {"status": reg_status, "detail": reg_detail},
        # 第23 加性只读：375 近似研究用途元数据（**不**参与上面任何放行判据）。
        "approximate_375": _approx_375_safe(real=real),
        # 第27 加性只读：375 逐日名单与实际输入 loader（三掩码分离；**不**参与放行判据）。
        "daily_loader_375": _daily_loader_375_safe(),
        # 第30 加性只读：375 实际运行接通身份（**不**参与放行判据）。
        "approximate_375_actual_run": _actual_run_375_safe(),
        # 第34 加性只读：375 完整输入版本与实际运行接通身份（完整输入与使用凭证分列；**不**参与放行判据）。
        "approximate_375_complete_input_actual_run": _complete_input_actual_run_375_safe(),
    }


# ══════════════════════════════════════════════════════════════════════════
# ②c 消费者三维**真实来源契约**（第20 加性）
# ══════════════════════════════════════════════════════════════════════════
# 背景：第18 起 `real_sources` 把 `daily_list_version` / `funding_model` / `data_version`
# **恒置 None** 并列入 `interface_missing`（「接口缺项」）。这是**接口未实现**，**不是**「用户
# 未批下载」。第20 起在**既有真实元数据/已核载荷接口**上**加性**定义三维的**可核来源**与
# **当前 run 对应**：**缺材料 ⇒ 具体原因阻断**（不是恒 None）。
#   · `data_version`       ← 正式输入 manifest（终验8）的**仅币安 526** 子集内容指纹（不沿 549/pilot）；
#   · `daily_list_version` ← 逐日准入/排名映射的**实际已存文件**规范化内容指纹（UTC/可得性/用途掩码既定）；
#   · `funding_model`      ← 本次采用的**既定模型规则** + **实际消费材料**身份（复用第19 `wx_execution`）。
# 纪律：**只读真实文件/实际材料**；**不接受调用方字符串/已核布尔**作证明；合成/隔离控制**永不**
# `formal_ready`；既有返回键与放行判据**不变**（纯加性）。
CONSUMER_DIM_SOURCE_SCHEMA = "wx_gate.consumer_dim_sources/v1"
REQUIRED_FORMAL_SCOPE = "binance_um_526"

# 第18 起的历史事实：这三维在本门禁七维里**没有**独立维度（保留常量供追溯）。第20 起它们
# **不再是**「接口缺项」（已有独立可核来源）⇒ `real_sources.interface_missing` 置空。
CONSUMER_DIMS_NO_GATE_DIMENSION = ("daily_list_version", "funding_model", "data_version")
# 消费者三维（与 `wx_execution.REQUIRED_RUN_BINDING` 中同名字段对应）。
CONSUMER_DIMS = ("daily_list_version", "funding_model", "data_version")

# 逐日名单映射的**实际已存文件**（正式 526）。当前未产出 ⇒ 解析为 blocked（带具体原因）；
# 契约已定义，文件出现即按 `DAILY_LIST_MAP_SCHEMA` 解析。
FORMAL_DAILY_LIST_DIR = os.path.join(SUITE_DIR, "正式信号与逐日准入闭环15")
FORMAL_DAILY_LIST_FILE = "04_逐日名单映射_526.json"
DAILY_LIST_MAP_SCHEMA = "daily_list_map/v1"

_DATA_USAGE_FORMAL = "formal_input_snapshot_526"
_DAILY_LIST_USAGE_FORMAL = "formal_daily_list_526"
_FUNDING_USAGE_FORMAL = "formal_funding_model"
# 三维各自**正式用途**标签（解析结果的 `usage` 必须等于它，否则记 `usage_not_formal`）。
_EXPECTED_USAGE = {"data_version": _DATA_USAGE_FORMAL,
                   "daily_list_version": _DAILY_LIST_USAGE_FORMAL,
                   "funding_model": _FUNDING_USAGE_FORMAL}

_JSON_CACHE = {}

# ── 第21：**当前执行子运行**（策略臂 `run_arm`）身份记录 ────────────────────────────────
# 背景：第20 的 `run_match` 只做了「消费者派生来源 == 门禁批次已核来源」的**批次内部一致**，
# 并把「无关子运行」静默换成门禁批次 run ⇒ `run_match=True` 仅表示版本值相等，**不表示当前
# 运行已匹配**（104a 缺陷2）。第21 起把「源可解析 / 当前运行匹配 / 正式资格」**分列**：
#   · `source_ok`   ：源可核（resolved ∧ 范围 ∧ 用途）；
#   · `run_match`   ：**当前执行子运行** ↔ 本批次的**内容对应**（True/False/**None**）；
#   · `formal_ok`   ：`source_ok` ∧ `run_match is True`（**不含**独立接受凭据）。
# 子运行身份**只**来自真实账本记录（`record_source="ledger:research_run"`），由
# `wx_execution.child_run_identity` 从 `research_run` 行构造；**不接受**调用方任意父 run 字符串
# 或裸布尔。`run_match` 的判据 = 子运行**实际消费**的维度值 == 真实解析出的来源值。
# **第22（缺口修复）**：`record_source` 字符串 + 调用方自算 `record_sha256` **不是**账本来源凭证
# （107/107a：伪造记录在实际账本 0 行仍判 `ok`）。故 `_verify_child_run` **还须**用**本次真实账本
# 连接**查询 `research_run` 行并**逐字段**核对（`verify_child_run_against_ledger`）；无账本上下文 /
# 无该 run 行 / 行与记录不符 ⇒ `invalid` + 具体原因，**绝不**返回 `ok`。
CHILD_RUN_RECORD_SOURCE = "ledger:research_run"
CHILD_RUN_RECORD_SOURCES = (CHILD_RUN_RECORD_SOURCE,)
# `record_sha256` 覆盖的**账本记录字段**（防记录内篡改；不含 declared 维度与哈希自身）。
CHILD_RUN_RECORD_FIELDS = ("record_source", "run_id", "arm", "config_id", "data_version",
                           "code_sha256", "config_hash", "initial_equity", "created_at")
# **第22**：记录里**必须与真实账本 `research_run` 行逐字段一致**的列（`record_source` 非列、
# `record_sha256` 为派生，均不参与）。声明字典/自算 hash **不是**账本来源凭证（107/108 缺口）。
CHILD_RUN_LEDGER_FIELDS = ("run_id", "arm", "config_id", "data_version", "code_sha256",
                           "config_hash", "initial_equity", "created_at")


def child_run_record_sha256(rec):
    """子运行**记录字段**的规范哈希（`wx_execution.child_run_identity` 与门禁同源）。"""
    return _canon({k: (rec or {}).get(k) for k in CHILD_RUN_RECORD_FIELDS})


def read_research_run_row(ledger, run_id):
    """**第22**：从**本次真实账本连接**只读 `research_run` 行。返回 dict|None（不可读 ⇒ None）。

    `ledger` 为 sqlite3.Connection（`wx_execution.run_arm` 的隔离研究库）或具备 `.execute` 的
    最小只读连接；**不**访问生产行情库。行不存在 / 无该列 / 读取异常 ⇒ `None`（不可核）。
    """
    if ledger is None or not run_id:
        return None
    try:
        row = ledger.execute("SELECT * FROM research_run WHERE run_id=?", (run_id,)).fetchone()
    except Exception:  # noqa: BLE001 —— 不可读即不可核（不猜、不从调用方字典派生）
        return None
    if row is None:
        return None
    try:
        keys = list(row.keys())
    except AttributeError:
        return None
    return {k: row[k] for k in keys}


def verify_child_run_against_ledger(child_run, ledger=None):
    """**第22**：核验子运行记录是否来自**本次真实账本 `research_run` 行**（逐字段）。

    返回 `(state, reason, row)`：`state ∈ {"ok","invalid"}`；`row` 为真实行 dict（无 ⇒ None）。
    判据（缺一即 `invalid`，**不**返回 `ok`）：
      · 无账本上下文（`ledger is None`）⇒ `child_run_no_ledger_context`（声明字典不可当来源）；
      · 该 `run_id` 在本账本 `research_run` **无行** ⇒ `child_run_no_ledger_row`；
      · 记录字段与真实行**逐字段**不符 ⇒ `child_run_record_row_mismatch:<字段…>`（含调用方以
        可选参数覆盖已存真实行后仍称「来自账本」的情形）。
    """
    if ledger is None:
        return "invalid", "child_run_no_ledger_context", None
    rec = child_run if isinstance(child_run, dict) else {}
    row = read_research_run_row(ledger, rec.get("run_id"))
    if row is None:
        return "invalid", "child_run_no_ledger_row", None
    mismatch = [f for f in CHILD_RUN_LEDGER_FIELDS
                if f in row and rec.get(f) != row.get(f)]
    if mismatch:
        return "invalid", "child_run_record_row_mismatch:" + ",".join(mismatch), row
    return "ok", None, row


def _verify_child_run(child_run, ledger=None):
    """校验**当前执行子运行**记录：须为真实账本记录且字段自洽。返回 (state, reason)。

    state ∈ `"absent"`（未提供 ⇒ 不可核）/ `"invalid"`（记录不符/无真实行 ⇒ 不匹配）/ `"ok"`。
    **第22**：`record_source` 字符串 + 调用方自算 `record_sha256` **只是**「记录自洽」检查，**不**
    构成账本来源凭证；`ok` **还须**经 `verify_child_run_against_ledger` 对**本次真实账本行**逐字段
    核验通过（无账本上下文/无行/字段不符 ⇒ `invalid` + 具体原因）。
    """
    if child_run is None:
        return "absent", "no_current_child_run"
    if not isinstance(child_run, dict):
        return "invalid", "child_run_not_record"
    if child_run.get("record_source") not in CHILD_RUN_RECORD_SOURCES:
        return "invalid", "child_run_unrecorded_source"
    if not child_run.get("run_id"):
        return "invalid", "child_run_missing_run_id"
    declared = child_run.get("record_sha256")
    if not isinstance(declared, str) or declared != child_run_record_sha256(child_run):
        return "invalid", "child_run_record_tampered"
    # **第22**：记录自洽 ≠ 来自账本；须对**本次真实账本行**逐字段核验（无则 fail-closed）。
    state, reason, _row = verify_child_run_against_ledger(child_run, ledger)
    return state, reason


def _child_dim_correspondence(child_run, batch_run_id, dims, requested_run=None, ledger=None):
    """**当前执行子运行** ↔ 本批次三维来源的**内容对应**（不只查「有 run_id 且为 True」）。

    · 未提供子运行 ⇒ 三维 `current_run_match=None`（`undeterminable`，**不**冒已匹配）；
    · 子运行记录不符（非账本记录 / 篡改 / 无 run_id）⇒ `False`（`unmatched` + 具体原因）；
    · **第22**：子运行记录须与**本次真实账本 `research_run` 行**逐字段一致（`ledger` 传入真实连接）；
      无账本上下文 / 无该 run 行 / 行与记录不符 ⇒ `False`（`unmatched` + 具体原因）——
      声明字典/自算 hash **不**构成来源；
    · 子运行声明了父批次 run_id 且 ≠ 门禁批次 run_id ⇒ `False`（`parent_batch_run_mismatch`）；
    · 逐维：子运行**实际消费**值 == 真实解析来源值 ⇒ `True`；子运行未给该维 ⇒ `None`（不可核）；
      不等 ⇒ `False`。**门禁批次 run 绝不覆盖**调用方请求的当前 run（第20 缺陷）：
      `requested_run` 原样回显为 `requested_child_run_id`；未给可核记录 ⇒ 记
      `requested_child_run_has_no_record`（**不**把它换成批次 run）。
    """
    state, reason = _verify_child_run(child_run, ledger)
    rec = child_run if isinstance(child_run, dict) else {}
    if state == "absent" and requested_run is not None:
        reason = "requested_child_run_has_no_record"
    if state == "ok" and requested_run is not None and requested_run != rec.get("run_id"):
        state, reason = "invalid", "child_run_id_mismatch"
    parent_decl = rec.get("parent_batch_run_id")
    parent_ok = None
    if state == "ok" and parent_decl is not None:
        parent_ok = (batch_run_id is not None and parent_decl == batch_run_id)
        if not parent_ok:
            state, reason = "invalid", "parent_batch_run_mismatch"
    out = {"child_run_state": state, "child_run_reason": reason,
           "child_run_id": rec.get("run_id"), "batch_run_id": batch_run_id,
           "requested_child_run_id": requested_run,
           "parent_batch_run_declared": parent_decl, "parent_batch_run_ok": parent_ok,
           "dims": {}}
    for dim in CONSUMER_DIMS:
        d = dims.get(dim) or {}
        val = d.get("value")
        declared = rec.get(dim)
        if state == "absent":
            m, st, rsn = None, "undeterminable", (reason or "no_current_child_run")
        elif state == "invalid":
            m, st, rsn = False, "unmatched", reason
        elif val is None:
            m, st, rsn = None, "undeterminable", "source_not_resolved"
        elif declared is None:
            m, st, rsn = None, "undeterminable", "child_declared_%s_absent" % dim
        elif declared == val:
            m, st, rsn = True, "matched", None
        else:
            m, st, rsn = False, "unmatched", "child_%s_mismatch" % dim
        out["dims"][dim] = {"current_run_match": m, "run_status": st, "run_reason": rsn,
                            "child_declared": declared, "resolved": val}
    return out


def _read_json_cached(path):
    """只读 JSON + 文件 sha256（按 mtime/size 缓存，文件一变即重读）。返回 (doc|None, sha|None)。"""
    try:
        st = os.stat(path)
    except OSError:
        return None, None
    key = (path, st.st_mtime_ns, st.st_size)
    hit = _JSON_CACHE.get(key)
    if hit is None:
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            return None, None
        hit = (doc, _sha256_file(path))
        _JSON_CACHE.clear()
        _JSON_CACHE[key] = hit
    return hit


def _dim_blocked(base, reason, **extra):
    """构造**阻断**维结果（有定义、有具体原因；**不是**恒 None 的「接口缺项」）。"""
    d = dict(base)
    d.update({"status": "blocked", "reason": reason, "value": None,
              "identity_sha256": None, "source_sha256": None, "scope": None, "as_of": None,
              "scope_ok": False, "usage_ok": False, "source_ok": False,
              "run_match": None, "formal_ok": False, "formal_ready": False})
    d.update(extra)
    return d


def _dim_finalize(d):
    """计算 `scope_ok` / `usage_ok` / `source_ok` / `formal_ok`。

    · `source_ok` = 真实来源**已解析** ∧ 范围 == 授权范围 ∧ 用途 == 正式用途（**可核来源**成立）；
    · `formal_ok` = `source_ok` ∧ **当前 run 对应**为真（研究批次级 ↔ 子运行一致）；
    · `formal_ready` **恒 False**：三维可核 ≠ 正式接受凭据（G01–G05 / 独立接受记录）成立。
    """
    if d.get("status") != "resolved":
        d["scope_ok"] = d["usage_ok"] = d["source_ok"] = False
        d["formal_ok"] = d["formal_ready"] = False
        return d
    d["scope_ok"] = (d.get("scope") == REQUIRED_FORMAL_SCOPE)
    d["usage_ok"] = (d.get("usage") == _EXPECTED_USAGE.get(d.get("dimension")))
    d["source_ok"] = bool(d["scope_ok"] and d["usage_ok"])
    d["formal_ok"] = bool(d["source_ok"] and d.get("run_match") is True)
    d["formal_ready"] = False
    return d


def _dim_source_reason(d):
    """可核来源不成立时的**具体原因**（供消费者在 `independently_verified=False` 时说明）。"""
    if d.get("status") != "resolved":
        return d.get("reason") or "source_unavailable"
    if not d.get("scope_ok", True):
        return "scope_not_formal_526"
    if not d.get("usage_ok", True):
        return "usage_not_formal"
    return None


def _resolve_data_version(path=None, run_id=None, usage=_DATA_USAGE_FORMAL):
    """`data_version` ← 正式 manifest 的**仅币安 526** 子集（不沿旧 549/pilot）。

    子集 = manifest `files[]` 中 `exchange=="binance"` 的记录（OHLCV 1h/15m + funding + 1h mark
    + quote sidecar），`official_unavailable` 项须自带 64-hex 证据 sha（否则 fail closed）。
    任一文件在磁盘缺失 / 记录非法 / 币数 ≠ 526 / manifest 的 `scope.binance` ≠ 526 ⇒ blocked。
    """
    path = path or DATA_MANIFEST
    base = {"dimension": "data_version", "source": path, "usage": usage,
            "scope_required": REQUIRED_FORMAL_SCOPE, "run_id": run_id}
    doc, fsha = _read_json_cached(path)
    if doc is None:
        return _dim_blocked(base, "data_manifest_missing_or_unreadable")
    files = doc.get("files") if isinstance(doc, dict) else None
    if not isinstance(files, list) or not files:
        return _dim_blocked(base, "data_manifest_no_files")
    sc = doc.get("scope") or {}
    if sc.get("binance") != 526:
        return _dim_blocked(base, "data_manifest_binance_scope_not_526", declared_scope=sc)
    hashes, symbols, n_off = {}, set(), 0
    for rec in files:
        if not isinstance(rec, dict) or rec.get("exchange") != "binance":
            continue
        sym = rec.get("symbol")
        if rec.get("availability") == "official_unavailable":
            ev = rec.get("evidence_sha256")
            if not (isinstance(ev, str) and _HEX64.match(ev)):
                return _dim_blocked(base, "binance_official_unavailable_without_evidence")
            n_off += 1
            symbols.add(sym)
            continue
        rel, sha = rec.get("abs"), rec.get("sha256")
        if not rel or not (isinstance(sha, str) and _HEX64.match(sha)):
            return _dim_blocked(base, "binance_file_record_invalid")
        if not os.path.isfile(rel):
            return _dim_blocked(base, "binance_input_file_missing_on_disk", detail=rel)
        hashes[rel] = sha
        symbols.add(sym)
    if not hashes:
        return _dim_blocked(base, "binance_526_subset_empty")
    if len(symbols) != 526:
        return _dim_blocked(base, "binance_symbol_count_not_526", n_symbols=len(symbols))
    snap = _canon(hashes)
    ident = _canon({"schema": CONSUMER_DIM_SOURCE_SCHEMA, "dim": "data_version",
                    "scope": REQUIRED_FORMAL_SCOPE, "snapshot_sha256_526": snap,
                    "manifest_sha256": fsha})
    return _dim_finalize({
        **base, "status": "resolved", "reason": None,
        "value": "dv:formal526:" + ident[:32], "identity_sha256": ident,
        "source_sha256": fsha, "manifest_sha256": fsha, "snapshot_sha256_526": snap,
        "scope": REQUIRED_FORMAL_SCOPE, "as_of": doc.get("cutoff"),
        "n_files_526": len(hashes), "n_symbols_526": len(symbols),
        "n_official_unavailable": n_off, "run_match": None,
    })


def _normalize_daily_list(doc):
    """规范化逐日名单映射（UTC00 日边界 / 来源可得性 / 用途掩码既定）。非法 ⇒ None。"""
    if not isinstance(doc, dict) or doc.get("schema") != DAILY_LIST_MAP_SCHEMA:
        return None
    days = doc.get("days")
    if not isinstance(days, list) or not days:
        return None
    import datetime as _dt
    norm_days = []
    for d in days:
        if not isinstance(d, dict):
            return None
        dms = d.get("day_ms")
        if dms is None:
            du = d.get("day_utc")
            if not isinstance(du, str):
                return None
            try:
                dms = int(_dt.datetime.strptime(du, "%Y-%m-%d")
                          .replace(tzinfo=_dt.timezone.utc).timestamp() * 1000)
            except ValueError:
                return None
        dms = int(dms)
        if dms % 86400000 != 0:
            return None                      # 必须落在 UTC00 日边界（既定口径）
        order, adm = d.get("order"), d.get("admission")
        if not isinstance(order, list) or not isinstance(adm, dict):
            return None
        norm_days.append({"day_ms": dms, "order": [str(s) for s in order],
                          "admission": sorted((str(k), bool(v)) for k, v in adm.items())})
    norm_days.sort(key=lambda x: x["day_ms"])
    return {"schema": DAILY_LIST_MAP_SCHEMA, "refresh": "UTC00",
            "window": "prior 30 complete UTC days",
            "scope": doc.get("scope"), "usage_mask": doc.get("usage_mask"),
            "as_of": doc.get("as_of"), "days": norm_days,
            "n_days": len(norm_days), "n_symbols": len(norm_days[0]["admission"])}


def _resolve_daily_list_version(path=None, run_id=None, usage=_DAILY_LIST_USAGE_FORMAL,
                                doc=None, source=None):
    """`daily_list_version` ← 逐日准入/排名映射的**实际已存文件**规范化内容指纹。

    文件缺失 / 非法 / schema 不符 / 日边界非 UTC00 / 文件声明 run_id 与当前 run 不符 ⇒ blocked。

    **第21 加性**：`doc`（可选）= 本次运行**实际使用**的规范化逐日映射内容（`run_arm` 的
    `rank_keys_by_day`/`admission_by_day` 序列化而来）⇒ 直接按内容解析，**不**依赖默认文件路径；
    此时 `source` 记「实际使用内容」标签、`source_sha256=None`（内容指纹仍在 `identity_sha256`）。
    `doc` 与 `path` 互斥：给 `doc` 即按实际使用内容解析（可核当前运行真实消费的名单）。
    """
    p = path or os.path.join(FORMAL_DAILY_LIST_DIR, FORMAL_DAILY_LIST_FILE)
    if doc is not None:
        base = {"dimension": "daily_list_version",
                "source": source or "run_arm_actual_daily_map", "usage": usage,
                "scope_required": REQUIRED_FORMAL_SCOPE, "run_id": run_id}
        fsha = None
    else:
        base = {"dimension": "daily_list_version", "source": p, "usage": usage,
                "scope_required": REQUIRED_FORMAL_SCOPE, "run_id": run_id}
        doc, fsha = _read_json_cached(p)
    if doc is None:
        return _dim_blocked(base, "daily_list_source_missing_or_unreadable")
    norm = _normalize_daily_list(doc)
    if norm is None:
        return _dim_blocked(base, "daily_list_schema_invalid", declared_schema=(
            doc.get("schema") if isinstance(doc, dict) else None))
    # **用途来自文件本身**（用途掩码既定）：文件声明的 `usage_mask` 覆盖调用方默认；
    # 非正式用途（如工程控制）⇒ `usage_ok=False` ⇒ `source_ok=False` ⇒ 阻断（不冒正式用途）。
    base["usage"] = doc.get("usage_mask") or usage
    declared_run = doc.get("run_id")
    if declared_run is not None and run_id is not None and declared_run != run_id:
        return _dim_blocked(base, "daily_list_run_id_mismatch",
                            declared_run_id=declared_run, current_run_id=run_id)
    ident = _canon({"schema": CONSUMER_DIM_SOURCE_SCHEMA, "dim": "daily_list_version",
                    "normalized": norm, "source_sha256": fsha})
    return _dim_finalize({
        **base, "status": "resolved", "reason": None,
        "value": "dl:" + ident[:32], "identity_sha256": ident, "source_sha256": fsha,
        "scope": norm["scope"], "as_of": norm.get("as_of"),
        "usage_mask": norm.get("usage_mask"),
        "utc_day_boundary": True, "n_days": norm["n_days"], "n_symbols": norm["n_symbols"],
        "declared_run_id": declared_run, "run_match": None,
    })


def _resolve_funding_model(funding_actual=None, funding_ledger=None, model=None,
                           run_id=None, usage=_FUNDING_USAGE_FORMAL):
    """`funding_model` ← 既定模型规则 + **实际消费材料**身份（复用第19 `wx_execution`）。

    无可核**实际**材料 / 模型未登记 / 材料与账本交叉核对不符 ⇒ blocked（不补 0、不冒作已核）。
    """
    from . import wx_execution as X       # 延迟导入（wx_execution 亦延迟导入本模块）
    base = {"dimension": "funding_model", "source": "run_arm_actual_materials",
            "usage": usage, "scope_required": REQUIRED_FORMAL_SCOPE, "run_id": run_id,
            "rule_registry": list(X.REGISTERED_FUNDING_MODELS)}
    mats = funding_actual if isinstance(funding_actual, dict) else None
    if (mats is None or mats.get("schema") != X.FUNDING_MATERIALS_SCHEMA
            or not (mats.get("sources") or [])
            or not any((s.get("events") or []) for s in mats["sources"])):
        return _dim_blocked(base, "funding_materials_missing")
    mdl = model or "norm4h"
    if mdl not in X.REGISTERED_FUNDING_MODELS:
        return _dim_blocked(base, "funding_model_unregistered", model=mdl)
    xcheck = (X._cross_check_materials_vs_ledger(mats, funding_ledger) if funding_ledger
              else {"ok": True, "n_ledger_rows": 0, "diffs": []})
    if not xcheck.get("ok"):
        return _dim_blocked(base, "funding_materials_ledger_mismatch", ledger_cross_check=xcheck)
    identity = X.funding_materials_identity(mdl, mats)
    exchanges = sorted({s.get("exchange") for s in mats["sources"]})
    scope = REQUIRED_FORMAL_SCOPE if exchanges == ["binance"] else "mixed_or_non_binance"
    ident = _canon({"schema": CONSUMER_DIM_SOURCE_SCHEMA, "dim": "funding_model",
                    "identity": identity, "n_sources": mats.get("n_sources")})
    return _dim_finalize({
        **base, "status": "resolved", "reason": None,
        "value": identity, "identity_sha256": ident, "source_sha256": None,
        "scope": scope, "as_of": None, "model": mdl,
        "n_sources": mats.get("n_sources"),
        "sources": ["%s:%s" % (s.get("exchange"), s.get("symbol"))
                    for s in mats["sources"]],
        "ledger_cross_check": xcheck, "run_match": None,
    })


def resolve_consumer_dim_sources(b=None, *, run=None, child_run=None, funding_actual=None,
                                 funding_ledger=None, model=None, daily_list_path=None,
                                 manifest_path=None, daily_list_usage=None, daily_list_doc=None,
                                 daily_list_source=None, isolated=False, ledger=None):
    """第20/21 **加性只读**：把消费者三维解析为**可核来源 + 当前运行对应**（缺材料 ⇒ 具体原因）。

    · 三维来源：`data_version`←正式 manifest 526 子集；`daily_list_version`←逐日映射（已存文件
      **或本次实际使用内容** `daily_list_doc`）；`funding_model`←既定模型规则 + 实际材料（复用第19）。
    · **层级分列（第21 关键修正）**：
        ① `source_ok`：源可核（resolved ∧ 范围 ∧ 用途）——**源解析**；
        ② `run_match`：**当前执行子运行**（`run_arm`）↔ 本批次三维的**内容对应**——**当前运行匹配**；
        ③ `formal_ok` = `source_ok` ∧ `run_match is True`——**正式资格**（仍**不含**独立接受凭据）。
      `run_match` 判据 = 子运行**实际消费**维度值 == 真实解析来源值；**无**子运行 ⇒ `None`（不可核），
      记录不符/无关父批次/内容不符 ⇒ `False`，**绝不**把门禁批次 run 当成子运行（第20 缺陷）。
    · `child_run`：当前执行子运行记录（**只**来自真实账本 `research_run` 行，由
      `wx_execution.child_run_identity` 构造）；`parent_batch_run_id` 若声明须 == 门禁批次 run。
      **第22**：`ledger`（本次真实账本连接）**必传**才能核验记录确来自该账本行；无则 `run_match`
      fail-closed 为 `False` + 具体原因（声明字典/自算 hash **不**构成来源）。
    · **只读真实文件/实际材料**；**不**接受调用方字符串/布尔；合成/隔离控制**永不** `formal_ready`。
    """
    b = b if isinstance(b, dict) else {}
    vp = b.get("verified_payload") if isinstance(b.get("verified_payload"), dict) else None
    ri = (vp or {}).get("run_identity") if isinstance((vp or {}).get("run_identity"), dict) else {}
    batch_run_id = (vp or {}).get("run_id") or (ri or {}).get("run_id")
    # **第21**：门禁批次 run 与 `run`（当前执行子运行 run id）**分列**；批次 run **绝不**覆盖子运行。
    run_id = batch_run_id if batch_run_id is not None else run
    verified = (vp or {}).get("verified_bindings") or {}
    data_dim = verified.get("data") if isinstance(verified.get("data"), dict) else None
    gate_run_binding_ok = bool(b.get("run_binding_ok"))

    dims = {
        "data_version": _resolve_data_version(manifest_path, run_id=run_id),
        "daily_list_version": _resolve_daily_list_version(
            daily_list_path, run_id=run_id,
            usage=daily_list_usage or _DAILY_LIST_USAGE_FORMAL,
            doc=daily_list_doc, source=daily_list_source),
        "funding_model": _resolve_funding_model(
            funding_actual, funding_ledger, model=model, run_id=run_id),
    }

    # ── ① 批次内部一致：三维来源 vs 门禁**批次**已核来源（**不等于**当前运行匹配）──────────
    dv = dims["data_version"]
    dl = dims["daily_list_version"]
    fm = dims["funding_model"]
    batch_consistent = {
        "data_version": ((dv.get("manifest_sha256") == data_dim.get("manifest_sha256"))
                         if (dv.get("source_ok") and data_dim is not None) else None),
        "daily_list_version": ((dl.get("declared_run_id") is None
                                or dl.get("declared_run_id") == run_id)
                               if dl.get("source_ok") else None),
        "funding_model": (bool(fm.get("model") in (fm.get("rule_registry") or []))
                          if fm.get("source_ok") else None),
    }

    # ── ② 当前执行子运行 ↔ 本批次三维的**内容对应**（第21；无子运行 ⇒ None，无关 ⇒ False）──
    # 调用方请求的当前 run（`run`）**原样回显**，门禁批次 run **不覆盖**它（第20 缺陷2）。
    # **第22**：`ledger`（本次真实账本连接）贯通到 `_verify_child_run` ⇒ 记录须与真实行逐字段一致。
    child_corr = _child_dim_correspondence(child_run, batch_run_id, dims, requested_run=run,
                                           ledger=ledger)

    corr = {}
    for dim in CONSUMER_DIMS:
        d = dims[dim]
        cc = child_corr["dims"][dim]
        d["run_match"] = cc["current_run_match"]        # 严格：仅内容对应为真才 True
        d["run_status"] = cc["run_status"]
        d["run_reason"] = cc["run_reason"]
        d["batch_consistent"] = batch_consistent[dim]
        corr[dim] = {
            "source_status": d.get("status"), "source_ok": d.get("source_ok"),
            "batch_consistent": batch_consistent[dim], "run_id": run_id,
            "batch_run_id": batch_run_id, "child_run_id": child_corr["child_run_id"],
            "requested_child_run_id": child_corr["requested_child_run_id"],
            "current_run_match": cc["current_run_match"], "run_status": cc["run_status"],
            "run_reason": cc["run_reason"],
            "child_declared": cc["child_declared"], "resolved": cc["resolved"],
            "formal_ok": None,                          # _dim_finalize 后回填
        }
    for d in dims.values():
        _dim_finalize(d)
    for dim in CONSUMER_DIMS:
        corr[dim]["formal_ok"] = dims[dim].get("formal_ok")
    return {
        "schema": CONSUMER_DIM_SOURCE_SCHEMA,
        "run_id": run_id, "batch_run_id": batch_run_id,
        "child_run_id": child_corr["child_run_id"],
        "requested_child_run_id": child_corr["requested_child_run_id"],
        "child_run_state": child_corr["child_run_state"],
        "child_run_reason": child_corr["child_run_reason"],
        # **第22**：是否已用**本次真实账本连接**核对子运行记录（False ⇒ 记录不可核，run_match 不为 True）
        "child_run_ledger_checked": ledger is not None,
        "parent_batch_run_declared": child_corr["parent_batch_run_declared"],
        "parent_batch_run_ok": child_corr["parent_batch_run_ok"],
        "gate_run_binding_ok": gate_run_binding_ok,
        "isolated": bool(isolated),
        "dimensions": dims, "run_correspondence": corr,
        "resolved": [k for k, v in dims.items() if v.get("status") == "resolved"],
        "source_ok": [k for k, v in dims.items() if v.get("source_ok")],
        "blocked": [k for k, v in dims.items() if v.get("status") == "blocked"],
        "formal_ok": [k for k, v in dims.items() if v.get("formal_ok")],
        # 正式就绪**恒 False**：三维可核 ≠ 正式接受凭据（G01–G05 / 独立接受记录）成立。
        "formal_ready": False,
        "note": ("三维可核来源已定义并可解析；`run_match` 仅表示**当前执行子运行内容对应**，"
                 "formal_ready 仍由门禁 research_pass（独立接受凭据）决定，本函数**不**放行"),
    }


def real_sources(b=None, *, funding_actual=None, funding_ledger=None, model=None,
                 daily_list_path=None, manifest_path=None, daily_list_usage=None,
                 daily_list_doc=None, daily_list_source=None, child_run=None, run=None,
                 isolated=False, ledger=None):
    """第20 **加性只读**接口：读取门禁**真实返回结构**的已核载荷，并**解析消费者三维真实来源**。

    真实返回 schema **恒带** `verified_payload`（`_pending` 与成功返回均带；见
    `_verified_payload`）。本函数**只读该真实字段**，**不再**读 `b.get("bindings")` 之类
    可能不存在的键（第18 缺陷：`_pending` 无 `bindings` ⇒ 已核载荷永远空）。

    **第20 加性**：`daily_list_version` / `funding_model` / `data_version` **不再恒 None**——
    改由 `resolve_consumer_dim_sources` 从**真实已存材料**解析（见其 docstring）。三维各自带
    `value` / `source` / `scope` / `as_of` / `status` / `reason` / `run_match`；**缺材料 ⇒
    `status="blocked"` + 具体 `reason`**（不是恒 None，也**不是**调用方字符串/布尔）。完整契约在
    返回的 `consumer_dim_sources`；`interface_missing` 置空（接口已实现），`blocked_dimensions`
    列出被材料/范围/run 阻断的维。

    返回（消费者 `wx_execution` 可直接使用的结构）：
      · `available` / `accepted`：真实门禁是否给出**已核**结构（未接受 ⇒ False + `reason`）；
      · `schema` / `reason`：载荷契约版本与未接受原因（未接受时明确原因，非恒 None）；
      · `run_identity` / `run_id`：本次 run 身份（真实键）；
      · `run_binding_ok` / `real_bindings_ok`：门禁实核结论（真实返回键）；
      · `verified_bindings`：七维中经真实验证链核验的**台账声明值**（来源=独立台账，**非调用方**）；
      · `daily_list_version` / `funding_model` / `data_version`：由**真实来源契约**解析的值
        （未解析 ⇒ None，但 `consumer_dim_sources[dim].reason` 给出**具体原因**）；
      · `consumer_dim_sources`：三维完整来源契约（含 `run_correspondence`）。

    旧对象（无 `verified_payload` 键）⇒ `available=False`、`reason="verified_payload_absent"`，
    **不猜、不从不存在键派生**（但三维仍从**真实文件**独立解析，故不恒 None）。
    """
    b = b if isinstance(b, dict) else binding()
    cds = resolve_consumer_dim_sources(
        b, run=run, child_run=child_run, funding_actual=funding_actual,
        funding_ledger=funding_ledger, model=model,
        daily_list_path=daily_list_path, manifest_path=manifest_path,
        daily_list_usage=daily_list_usage, daily_list_doc=daily_list_doc,
        daily_list_source=daily_list_source, isolated=isolated, ledger=ledger)
    dims = cds["dimensions"]
    dv, dl, fm = dims["data_version"], dims["daily_list_version"], dims["funding_model"]
    vp = b.get("verified_payload") if isinstance(b.get("verified_payload"), dict) else None
    if vp is None:
        return {
            "schema": None, "available": False, "accepted": False,
            "reason": "verified_payload_absent",
            "run_identity": {}, "run_id": cds.get("run_id"),
            "run_binding_ok": bool(b.get("run_binding_ok")),
            "real_bindings_ok": bool(b.get("real_bindings_ok")),
            "verified_bindings": {}, "verified_dimensions": [],
            "unverified_dimensions": list(REQUIRED_BINDINGS),
            "daily_list_version": dl.get("value"), "input_snapshot": None,
            "funding_model": fm.get("value"), "data_version": dv.get("value"),
            "consumer_dim_sources": cds,
            "input_snapshot_candidate_dim": "data",
            "interface_missing": [],
            "blocked_dimensions": cds.get("blocked"),
            "batch_run_id": cds.get("batch_run_id"), "child_run_id": cds.get("child_run_id"),
            "requested_child_run_id": cds.get("requested_child_run_id"),
            "child_run_state": cds.get("child_run_state"),
            "child_run_reason": cds.get("child_run_reason"),
            "child_run_ledger_checked": cds.get("child_run_ledger_checked"),
            "parent_batch_run_declared": cds.get("parent_batch_run_declared"),
            "parent_batch_run_ok": cds.get("parent_batch_run_ok"),
            "source": b.get("source"), "sha256": b.get("sha256"),
        }
    ri = vp.get("run_identity") if isinstance(vp.get("run_identity"), dict) else {}
    verified = vp.get("verified_bindings") if isinstance(vp.get("verified_bindings"), dict) else {}
    return {
        "schema": vp.get("schema"),
        "available": bool(vp.get("accepted")), "accepted": bool(vp.get("accepted")),
        "reason": vp.get("reason"),
        "run_identity": dict(ri), "run_id": vp.get("run_id"),
        "run_binding_ok": bool(b.get("run_binding_ok")),
        "real_bindings_ok": bool(b.get("real_bindings_ok")),
        "verified_bindings": dict(verified),
        "verified_dimensions": list(vp.get("verified_dimensions") or sorted(verified)),
        "unverified_dimensions": list(vp.get("unverified_dimensions")
                                     or [d for d in REQUIRED_BINDINGS if d not in verified]),
        "daily_list_version": dl.get("value"), "input_snapshot": None,
        "funding_model": fm.get("value"), "data_version": dv.get("value"),
        "consumer_dim_sources": cds,
        "input_snapshot_candidate_dim": "data",
        "interface_missing": [],
        "blocked_dimensions": cds.get("blocked"),
        # 第21：批次 run 与**当前执行子运行**分列（run_match 只表示子运行内容对应）
        "batch_run_id": cds.get("batch_run_id"), "child_run_id": cds.get("child_run_id"),
        "requested_child_run_id": cds.get("requested_child_run_id"),
        "child_run_state": cds.get("child_run_state"),
        "child_run_reason": cds.get("child_run_reason"),
        "child_run_ledger_checked": cds.get("child_run_ledger_checked"),
        "parent_batch_run_declared": cds.get("parent_batch_run_declared"),
        "parent_batch_run_ok": cds.get("parent_batch_run_ok"),
        "historical_consumer_dims_without_gate_dimension":
            list(vp.get("consumer_dims_without_gate_dimension")
                 or CONSUMER_DIMS_NO_GATE_DIMENSION),
        "source": b.get("source"), "sha256": b.get("sha256"),
    }


# ══════════════════════════════════════════════════════════════════════════
# ④ 375 币近似研究用途（第23 加性只读；**不**参与 all_pass / research_pass）
# ══════════════════════════════════════════════════════════════════════════
APPROX_375_PURPOSE = "approximate_375_research"


def approximate_375(*, real=None, with_calendar=True):
    """第23 **加性只读**：375 近似研究用途的实际输入/版本/资本/风险/费用模型元数据。

    复用 `wx_config.approximate_375_registration`（单一事实源）；缺材料 ⇒ `status="blocked"` +
    具体原因。**恒不构成研究放行**：`formal_ready=False`、`research_pass=False`；**不**进入
    `binding()` 的 `all_pass` / `research_pass` 判据（纯加性；近似用途须 Codex 独立接受）。
    """
    from . import wx_config          # 延迟导入（wx_config 顶层导入本模块）
    return wx_config.approximate_375_registration(real=real, with_calendar=with_calendar)


def _approx_375_safe(real=None):
    """安全包装：任何异常 ⇒ None（**绝不**影响既有门禁判据与返回结构）。"""
    try:
        return approximate_375(real=real)
    except Exception:  # noqa: BLE001 —— 加性元数据不得影响放行链
        return None


def daily_loader_375():
    """第27 **加性只读**：375 逐日名单与实际输入 loader 登记（三掩码分离）。

    复用 `wx_config.approximate_375_daily_loader`（单一事实源）。**恒不构成研究放行**：
    `formal_ready=False`、`research_pass=False`；**不**进入 `all_pass` / `research_pass` 判据。
    """
    from . import wx_config          # 延迟导入（wx_config 顶层导入本模块）
    return wx_config.approximate_375_daily_loader()


def _daily_loader_375_safe():
    """安全包装：任何异常 ⇒ None（**绝不**影响既有门禁判据与返回结构）。"""
    try:
        return daily_loader_375()
    except Exception:  # noqa: BLE001 —— 加性元数据不得影响放行链
        return None


def actual_run_375():
    """第30 **加性只读**：375 **实际运行**接通身份（入口/池/日名单/模型/费用材料/数据版本/源码/
    引擎/18 配置/资本/父子 run）。

    复用 `wx_config.approximate_375_actual_run_registration`（单一事实源；读第30 实际交付材料）。
    **恒不构成研究放行**：`formal_ready=False`、`research_pass=False`；**不**进入 `all_pass` /
    `research_pass` 判据（近似用途须 Codex 独立接受；30 不自写接受）。
    """
    from . import wx_config          # 延迟导入（wx_config 顶层导入本模块）
    return wx_config.approximate_375_actual_run_registration()


def _actual_run_375_safe():
    """安全包装：任何异常 ⇒ None（**绝不**影响既有门禁判据与返回结构）。"""
    try:
        return actual_run_375()
    except Exception:  # noqa: BLE001 —— 加性元数据不得影响放行链
        return None


def complete_input_actual_run_375():
    """第34 **加性只读**：375 **完整输入版本与实际运行**接通身份（完整输入与使用凭证分列 + 真实引用链）。

    复用 `wx_config.approximate_375_complete_input_actual_run_registration`（单一事实源）。
    **恒不构成研究放行**：`formal_ready=False`、`research_pass=False`；**不**进入 `all_pass` /
    `research_pass` 判据（近似用途须 Codex 独立接受；34 不自写接受）。
    """
    from . import wx_config          # 延迟导入（wx_config 顶层导入本模块）
    return wx_config.approximate_375_complete_input_actual_run_registration()


def _complete_input_actual_run_375_safe():
    """安全包装：任何异常 ⇒ None（**绝不**影响既有门禁判据与返回结构）。"""
    try:
        return complete_input_actual_run_375()
    except Exception:  # noqa: BLE001 —— 加性元数据不得影响放行链
        return None


def run_frozen(policy_run_frozen, identity_ok, binding_doc=None):
    """运行放行的**唯一合成判据**：策略视图 run_frozen ∧ 身份一致 ∧ 研究放行。

    以 `research_pass` 为准（隔离 / 自定义索引 / 合成台账永不研究放行）。任何单表
    （如 DB 行 frozen=1）或标签都不能单独放行。
    """
    b = binding_doc or binding()
    release = b.get("research_pass")
    if release is None:                       # 兼容极旧对象（无 research_pass 字段）
        release = b.get("all_pass")
    return bool(policy_run_frozen) and bool(identity_ok) and bool(release)
