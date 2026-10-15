# -*- coding: utf-8 -*-
"""F_WX S3a **项目局部执行适配器**（隔离研究用；不触生产、不下单、不改全局 vendor）。

复用已安装 Freqtrade 2026.9 的 **订单 / Trade 骨架语义**（市价单、单一持仓、固定止损、
资金费按结算事件计入、按 15m 执行时钟推进），在项目内实现确定性回放：

  信号 → 意图 → 成交/拒单 → 手续费/滑点/实际资金费 → 固定止损/720h/DD20% 退出
       → 账户净权益 → 复盘

规则来源（只读引用，不复制数值）：
  · `10_研究参数生效视图_v2_1.json` 的 execution / stops / risk / reconciliation 段。
  · 用户确认「共用5000u」：**单一跨所共享研究账户**（5000 USDT），
    同一 run 内 Binance/OKX 共享现金/保证金/风险预算；**每个研究臂独立回放**。

**事件顺序（2026-10-11 E04 修复后，T 开盘先于盘中）**：
  ① T⁻ 旧仓资金费结算（用 T 实际 event mark）→ ② 开盘缺口保护 → ③ DD 锁止的下一开盘清仓
  → ④ 开盘到期的信号/安全退出（donchian / safety_720h）→ ⑤ 排名入场
  → ⑥ 盘中 high/low（固定止损 / 入场后盘中止损）→ ⑦ 权益 + DD 检测。
  关键：**T 之后的本根 future low 不得改变已在 T 开盘完成的成交**（E04）。

**E01–E06 修复要点**：
  · E01 初始固定止损锚点 = **实际含滑点成交价 − 3×信号/确认 bar ATR**；数量按该实际止损距离
    与历史数量步长计算。结构失效线（detection_low）、ATR 检测条件与止损锚点**分开**。
  · E02 终止按**已冻结的可执行终止开盘**清算；终止退出费/滑点/资金费**全部入账**后才写
    最终现金、权益、review；每条退出路径都参与核账（不只核 reason）。
  · E03 资金费用 **T 实际 event mark × 实际 rate × 实际数量**；**普通 15m 交易 bar 开盘价
    不得替代 event mark**（整改3）：只接受事件自带 mark 或真实 `marks` 序列；二者皆缺显式
    **阻断**（写 amount=NULL + blocked 记录），**不补 0、不用入场价、不用未来 close、不用
    trade bar open 回退**；T⁻ 旧仓先结算、T 新仓不结算、负率保留；T 开盘风险只用**当时因果
    可得**的价格/权益。funding 行留存 mark/mark_source/available_at/合约单位/历史代际可追溯。
  · E05 收盘权益触发 20% DD ⇒ 取消 pending、**锁止全 run 新入场**，**下一 15m 开盘**清仓且
    不恢复；每条入场路径与预占都检查 `halted`。
  · E06 预占只扣一次 held（见 `wx_ledger.try_reserve`）。

现金口径（单一共享账户）：
  · `cash` = 账户总现金；`committed` = Σ(名义/杠杆) 为已占用保证金；`free = cash - committed`。
  · 入场：`cash -= 入场费`（费即时入账），预占保证金（唯一约束，防双花）。
  · 资金费：`cash += amount`（多头付正费率；**负费率保留**）。
  · 退出：`cash += 毛盈亏 − 出场费`（资金费已在持有期逐次入账，不重复计）。
  · 单笔净盈亏 = 毛盈亏 − 双边费 + 资金费 = 该笔的累计现金变动。

诚实边界：
  · 缺 funding / 缺 event mark / 缺合约精度 / 缺身份 ⇒ **拒单或阻断**，绝不用默认钱包或 0 充数。
  · 部分成交不建模：容量不足即整单拒绝（`whole_order_reject_when_capacity_fails`）。
  · 不声称历史交易所精确强平；研究模型用 15m 合约最新价估值。
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
import re
from dataclasses import dataclass

from . import wx_ledger as L

DETAIL_MS = 15 * 60 * 1000
HOUR_MS = 3600 * 1000
DAY_MS = 24 * HOUR_MS                    # 第15：UTC00 日边界（准入/竞争刷新节拍）

# 第15：新正式运行**必须**绑定的六个维度（缺任一 ⇒ 不得作为正式用途放行）。
# 这是「缺绑定必须阻断新正式用途、不再默填『未指定』」的判据（见 validate_run_binding）。
REQUIRED_RUN_BINDING = (
    "scope",              # 范围：如 "binance_um_526"（币安 526 合约）
    "daily_list_version", # 逐日名单/排序映射版本（sha256 或版本串）
    "input_snapshot",     # 输入快照身份（逐币 data_version 汇总 + manifest sha）
    "code_sha256",        # 源码包 sha（策略/执行/账本/配置/引擎）
    "strategy_config",    # 策略/18 配置身份（视图 sha + 逐配置生效身份 sha）
    "funding_model",      # 资金费模型身份（正常4h / 极端1h·2% 等，分别登记）
)


def validate_run_binding(binding):
    """正式运行绑定**完备性**校验（fail-closed）。返回 (ok, missing)。

    `binding` 为 dict；缺键 / 空值一律计为缺失。`None`/空 dict ⇒ 六维全缺。
    本函数**只判完备性**，不做证据实核（实核属 `wx_gate` 的 G01–G05，本轮不声称通过）。
    """
    if not binding:
        return False, list(REQUIRED_RUN_BINDING)
    missing = [k for k in REQUIRED_RUN_BINDING if not binding.get(k)]
    return (len(missing) == 0), missing


def binding_sha256(binding):
    """运行绑定的 canonical sha256（用于 run 记录/追溯）。"""
    return hashlib.sha256(
        json.dumps(binding or {}, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8")).hexdigest()


# ── 第16：正式就绪与「工程绑定完整 / 局部对账」**分别表达** ─────────────────────
# 六维绑定的**本地可实算**分量（范围/资金费模型身份）与源码包口径。
AUTHORIZED_SCOPE = "binance_um_526"          # 本轮唯一授权范围（仅币安 526）
# 第17：资金费模型身份**严格登记匹配**（不再用 startswith 判前缀）。
# 已登记模型名（互斥、分别登记、绝不相加）；完整身份串 = `fundmodel:<model>:<32hex>`
# （口径同第15 `_fundmodel15.funding_model_identity`：sha256 前 32 位小写十六进制）。
REGISTERED_FUNDING_MODELS = ("norm4h", "ext1h")
_FUNDING_MODEL_RE = re.compile(r"^fundmodel:(norm4h|ext1h):[0-9a-f]{32}$")

# 第18：费用模型**实值核验**所需的两情景规则定义。与第15 `_fundmodel15.MODEL_FEE_RULE`
# **逐字节同源**（本窗口只抽出兼容共享纯函数，**不修改**第15 原件、不改费率规则）。
# `norm4h` 正常 4h / `ext1h` 极端 1h·2%；两情景**互斥、分别登记、绝不相加**。
FUNDING_MODEL_FEE_RULE = {
    "norm4h": "固定每4h(UTC00/04/08/12/16/20)；费率=严格早于T的最近真实结算费率向后沿用"
              "(last-known)；不除8/除2、不补0、不用T之后事件；符号原样（负率=收款）",
    "ext1h": "固定每1h整点；幅度0.02(2%)；明示额外单边不利压力（在册持仓一律按名义扣2%）；"
             "不冒作官方多空都付2%；基数=持仓名义×T时刻真实1h mark",
}
# 六维绑定 code_sha256 覆盖的源码文件（相对 `后端实现/`）。与第15 `_binding15.CODE_FILES` 同源，
# 使「实际源码包 sha」可与声明值**逐字节**比对（不是字段凑齐）。
BINDING_CODE_FILES = (
    "strategies/wx_donchian_wyckoff.py", "strategies/wx_execution.py",
    "strategies/wx_ledger.py", "strategies/wx_config.py",
    "strategies/wx_engine.py", "strategies/wx_gate.py",
    "strategies/common.py", "strategies/registry.py",
    "strategy_runner.py",
)
# 独立接受门禁（wx_gate）文档的必备字段：用于拒绝「任意传 True 即放行」的伪门禁对象。
# 注：`synthetic` 只在 wx_gate 读到真实台账时出现（`_pending` 无该键）⇒ 用 `.get` 可选判定。
_GATE_FLAG_FIELDS = ("all_pass", "research_pass", "isolated")
_GATE_PROOF_FIELDS = ("structure_ok", "bindings_concrete", "real_bindings_ok",
                      "run_binding_ok", "evidence_verified")


def _default_backend_dir():
    import os
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _sha256_file_local(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 16), b""):
            h.update(c)
    return h.hexdigest()


def compute_code_bundle_sha(backend_dir=None):
    """实算源码包 sha（`code:<sha>`），口径与第15 `_binding15.code_bundle_sha` 一致。

    缺任一文件 ⇒ 该文件记 None（**不静默跳过**），包 sha 随之为该「含缺项」集合的哈希
    ⇒ 与任何完整声明的 code_sha256 都不同 ⇒ 一致性核验 fail closed。
    """
    import os
    backend = backend_dir or _default_backend_dir()
    per = {}
    for rel in BINDING_CODE_FILES:
        fp = os.path.join(backend, rel)
        per[rel] = _sha256_file_local(fp) if os.path.exists(fp) else None
    bundle = hashlib.sha256(
        json.dumps(per, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"code:{bundle}", per


def compute_strategy_config_identity():
    """实算策略/18 配置身份（视图 sha + 18 生效身份 sha）。返回 dict（含 value/identity_ok）。"""
    from . import wx_config
    view, vsha = wx_config.load_view()
    if view is None:
        return {"value": None, "identity_ok": False, "view_sha256": None,
                "effective_configs_sha256": None,
                "problems": ["effective_view_missing"]}
    ident = wx_config.identity_check(view, vsha)
    eff = ident["effective"]["sha256"] if ident.get("effective") else None
    return {"value": f"cfg:{vsha}:{eff}", "identity_ok": ident["ok"],
            "view_sha256": vsha, "effective_configs_sha256": eff,
            "problems": ident["problems"]}


def funding_model_registered(value):
    """资金费模型身份是否**完整匹配已登记模型**（第17：不再用 startswith 判前缀）。

    已登记身份 = `fundmodel:<model>:<32hex>`，`model ∈ REGISTERED_FUNDING_MODELS`
    （`norm4h` / `ext1h`），后缀须为 32 位小写十六进制。任意自定义后缀
    （如 `fundmodel:norm4h:FAKE_UNREGISTERED_SUFFIX`）、未登记模型名（如 `ext8h`）、
    长度不符 ⇒ 不成立（fail closed）。
    """
    return bool(isinstance(value, str) and _FUNDING_MODEL_RE.match(value))


def parse_funding_model(value):
    """拆解身份串 → (model, hex32)；非登记格式 ⇒ (None, None)。**纯函数**。"""
    if not funding_model_registered(value):
        return None, None
    _, model, hx = value.split(":")
    return model, hx


def funding_model_identity(model, events, marks_consumed=None):
    """费用情景身份 sha256（**与第15 `_fundmodel15.funding_model_identity` 字节同源**）。

    把「模型名 + 规则定义文本 + 事件时间/费率序列 + 实际消费 mark 集合」写入 sha256 前 32 位
    （小写十六进制），返回 `fundmodel:<model>:<32hex>`。⇒ 只换模型名 / 费率 / 时点 / mark /
    **定义版本（规则文本）** 任一不一致，身份即不同。本函数为**共享纯函数**，不改第15 原件。
    """
    h = hashlib.sha256()
    h.update(str(model).encode())
    h.update(json.dumps(FUNDING_MODEL_FEE_RULE.get(model, ""), ensure_ascii=False).encode())
    h.update(json.dumps([[int(t), float(r)] for t, r in events]).encode())
    h.update(json.dumps(sorted((marks_consumed or {}).items())).encode())
    return f"fundmodel:{model}:" + h.hexdigest()[:32]


# ── 第19：**实际**资金费材料（结构化、带版本、逐 (所,币) 分列）────────────────────────
# 目的：把「费用模型实值核验」的输入从**调用方字典**改为**本次真实执行输入**（`run_arm` 的
# `symbol_data` 规范化资金费序列 + 真正消费 mark），并可与隔离账本**实际入账**行交叉核对。
# 多币/多所**分列**（键含 exchange+symbol）⇒ 同小时跨币 mark/费率**不互相覆盖**。
# 复用 `_normalize_funding_events` / `resolve_funding_mark` / 账本现有字段，**不造第二执行引擎**。
#
# ── 第33：**完整输入版本身份**与「实际使用凭证」**分开登记**（人类 134 批准口径）────────────
#   · 完整输入版本身份 = 本次 `symbol_data` 规范化**完整**费用事件（含缺率边界/来源毫秒/真实 mark
#     或缺）—— **稳定**，执行前后一致，**绝不**被事后持仓使用子集覆盖/替换；
#   · 实际使用凭证 = 同一真实账本（run/position/order/funding）逐笔派生，**指向**完整输入版本。
FUNDING_MATERIALS_SCHEMA = "funding_materials/v2"          # 第33：完整输入版本材料（v2）
FUNDING_MATERIALS_SCHEMA_V1 = "funding_materials/v1"       # 兼容旧 v1 声明（只读解析）
FUNDING_USAGE_VOUCHER_SCHEMA = "funding_usage_voucher/v1"  # 第33：实际使用凭证
FUNDING_USAGE_AUDIT_SCHEMA = "funding_usage_audit/v1"      # 第33：逐笔双向审查


def complete_funding_materials(symbol_data, model=None):
    """**完整输入版本材料**（v2）：本次实际 `symbol_data` 的规范化**完整**费用事件。

    逐 `(exchange, symbol)` 一个 source：
      · `events` = **全部**规范化事件 `[T, rate|None, kind|None, source_ms|None, mark|None]`
        —— 含**缺率边界**（`kind="rate_missing"`，rate=None）与**来源原始毫秒** `source_ms`；
      · `marks` = `{T: mark|None}`（真实 event mark：事件自带或 `symbol_data["marks"]` 序列；缺 ⇒ None）。
    覆盖「费率或 None/缺标签、来源原始时刻、真实 mark 或缺、规则/模型、所/币」。
    **执行前后稳定**；**不**受 `ledger_rows`（实际使用子集）影响。
    """
    sources = []
    keys = [k for k in (symbol_data or {}) if isinstance(k, tuple) and len(k) == 2]
    for key in sorted(keys):
        exchange, symbol = key
        sd = symbol_data.get(key) or {}
        events, marks = [], {}
        for ev in _normalize_funding_events(sd.get("funding", [])):
            ft, rate, emark, avail, kind = ev
            mark, _msrc = resolve_funding_mark(sd, None, exchange, symbol, ft, emark)
            events.append([int(ft),
                           (float(rate) if rate is not None else None),
                           (kind if kind is not None else None),
                           (int(avail) if avail is not None else None),
                           (float(mark) if mark is not None else None)])
            marks[int(ft)] = (float(mark) if mark is not None else None)
        sources.append({"exchange": exchange, "symbol": symbol, "events": events,
                        "marks": marks, "n_events": len(events),
                        "n_marks": sum(1 for v in marks.values() if v is not None),
                        "marks_source": ("event_or_series"
                                         if any(v is not None for v in marks.values())
                                         else None)})
    return {"schema": FUNDING_MATERIALS_SCHEMA, "model": model,
            "n_sources": len(sources), "sources": sources}


def actual_funding_materials(symbol_data, ledger_rows=None):
    """兼容入口：返回**完整输入版本材料（v2）**（`marks` 为**完整**真实 mark）。

    第33 修复：旧实现用 `ledger_rows`（实际使用 mark 子集）**覆盖** `marks_consumed`
    ⇒ 与执行前全量 marks 声明口径不同 ⇒ 形式一致性恒 False（133a 根因）。现：
      · 完整材料**始终**来自 `symbol_data`（完整 marks，**不被覆盖**）；
      · `ledger_rows` 的实际使用 mark 只登记为**独立字段** `marks_used_ledger`（信息项，供凭证/审查）。
    """
    mats = complete_funding_materials(symbol_data)
    used = {}
    for row in (ledger_rows or []):
        if row.get("mark") is None or row.get("amount") is None:
            continue
        used.setdefault((row["exchange"], row["symbol"]), {})[
            int(row["funding_time"])] = float(row["mark"])
    for src in mats["sources"]:
        ck = used.get((src["exchange"], src["symbol"]))
        if ck:
            src["marks_used_ledger"] = dict(ck)
            src["n_marks_used_ledger"] = len(ck)
    return mats


def _events_canon(events):
    """v2 5 元组事件 → 规范化 5 元组（用于身份/比对；容忍 2/3/4/5 元组输入）。"""
    out = []
    for e in (events or []):
        out.append([int(e[0]),
                    (None if len(e) < 2 or e[1] is None else float(e[1])),
                    (e[2] if len(e) > 2 else None),
                    (None if len(e) < 4 or e[3] is None else int(e[3])),
                    (None if len(e) < 5 or e[4] is None else float(e[4]))])
    return out


def funding_model_identity_v2(model, events, marks):
    """完整输入版本身份（v2，**逐源**）：模型名 + 规则文本 + 事件[时点/率/缺标签/来源毫秒/mark] + marks。"""
    h = hashlib.sha256()
    h.update(FUNDING_MATERIALS_SCHEMA.encode())
    h.update(str(model).encode())
    h.update(json.dumps(FUNDING_MODEL_FEE_RULE.get(model, ""), ensure_ascii=False).encode())
    h.update(json.dumps(_events_canon(events), ensure_ascii=False).encode())
    h.update(json.dumps(sorted((int(k), (None if v is None else float(v)))
                               for k, v in (marks or {}).items())).encode())
    return f"fundmodel:{model}:" + h.hexdigest()[:32]


def funding_materials_identity(model, materials):
    """资金费模型身份 `fundmodel:<model>:<32hex>`。

    · **v2**（`FUNDING_MATERIALS_SCHEMA`，完整输入版本）：逐 `(所,币)` 用 `funding_model_identity_v2`
      （覆盖 时点/率/缺标签/来源毫秒/mark），再以显式版本 + 含所/币键聚合 ⇒ 模型名/规则/费率/
      时点/mark/来源毫秒/缺标签/币/所 任一不同 ⇒ 身份不同；跨币同刻不互相覆盖。
    · **v1**（旧 dict）：走既有 `funding_model_identity`（第15 算法），**不**因升级而崩。
    """
    srcs = (materials or {}).get("sources") if isinstance(materials, dict) else None
    schema = (materials or {}).get("schema") if isinstance(materials, dict) else None
    if schema == FUNDING_MATERIALS_SCHEMA:
        per = {}
        for s in (srcs or []):
            key = "%s:%s" % (s.get("exchange"), s.get("symbol"))
            per[key] = funding_model_identity_v2(model, s.get("events") or [],
                                                 s.get("marks") or {})
        h = hashlib.sha256()
        h.update(FUNDING_MATERIALS_SCHEMA.encode())
        h.update(str(model).encode())
        h.update(json.dumps(sorted(per.items()), ensure_ascii=False).encode())
        return "fundmodel:%s:%s" % (model, h.hexdigest()[:32])
    # ── 兼容 v1（旧 dict；events=[[t,rate]]，marks_consumed={t:mark}）────────────────────
    per = {}
    for s in (srcs or []):
        key = "%s:%s" % (s.get("exchange"), s.get("symbol"))
        ev1 = [[int(e[0]), float(e[1])] for e in (s.get("events") or [])
               if len(e) >= 2 and e[1] is not None]
        mk = s.get("marks_consumed") or s.get("marks") or {}
        per[key] = funding_model_identity(model, ev1,
                                          {k: v for k, v in mk.items() if v is not None})
    h = hashlib.sha256()
    h.update(FUNDING_MATERIALS_SCHEMA_V1.encode())
    h.update(str(model).encode())
    h.update(json.dumps(sorted(per.items()), ensure_ascii=False).encode())
    return "fundmodel:%s:%s" % (model, h.hexdigest()[:32])


def _cross_check_materials_vs_ledger(materials, ledger_rows):
    """材料（完整输入）vs 隔离账本**实际入账**行：逐 (所,币,时点) 核 rate 与 mark。

    第33：材料为 v2 完整事件（`events` 5 元组 / `marks` 含 None）；缺率边界（rate=None）行的
    rate 由相邻不利**估计**，不参与 rate 相等比较（由双向审查按 `rate_source` 判定）；mark 仍须
    与完整输入的真实 mark 一致。无账本行 ⇒ 不构成核对失败（材料仍来自实际输入）。
    """
    per = {}
    for s in ((materials or {}).get("sources") or []):
        per[(s.get("exchange"), s.get("symbol"))] = s
    diffs, n = [], 0
    for row in (ledger_rows or []):
        n += 1
        key = (row["exchange"], row["symbol"])
        src = per.get(key)
        label = "%s:%s@%s" % (key[0], key[1], row["funding_time"])
        if src is None:
            diffs.append({"at": label, "why": "source_not_in_materials"})
            continue
        ev = {int(e[0]): (None if len(e) < 2 else e[1]) for e in (src.get("events") or [])}
        ft = int(row["funding_time"])
        if ft not in ev:
            diffs.append({"at": label, "why": "time_not_in_complete_events"})
        elif ev[ft] is not None and row.get("rate") is not None and \
                abs(float(ev[ft]) - float(row["rate"])) > 1e-15:
            diffs.append({"at": label, "why": "rate_mismatch",
                          "materials": ev[ft], "ledger": float(row["rate"])})
        mk = src.get("marks") or src.get("marks_consumed") or {}
        if row.get("mark") is not None and (
                ft not in mk or mk[ft] is None
                or abs(float(mk[ft]) - float(row["mark"])) > 1e-12):
            diffs.append({"at": label, "why": "mark_mismatch",
                          "materials": mk.get(ft), "ledger": float(row["mark"])})
    return {"ok": not diffs, "n_ledger_rows": n, "diffs": diffs}


def _declaration_matches_materials(declaration, materials):
    """调用方**材料声明** vs **实际材料**（材料是声明、不是可信实际证据）。

    结构一致才通过：v2/v1 形态逐源比对（所/币/事件 5 元组/消费 mark）；legacy 平铺形态仅单源可比。
    返回 (ok, why)。
    """
    if declaration is None:
        return True, "no_declaration"
    if not isinstance(declaration, dict):
        return False, "declaration_not_object"
    srcs = (materials or {}).get("sources") or []

    def _norm(ss):
        out = []
        for s in ss:
            mk = s.get("marks_consumed") or s.get("marks") or {}
            out.append((s.get("exchange"), s.get("symbol"),
                        _events_canon(s.get("events") or []),
                        sorted((int(k), (None if v is None else float(v)))
                               for k, v in mk.items())))
        return sorted(out, key=lambda x: (str(x[0]), str(x[1])))

    if isinstance(declaration.get("sources"), list):
        same = (_norm(declaration["sources"]) == _norm(srcs))
        return same, ("matches" if same else "sources_mismatch")
    if len(srcs) != 1:
        return False, "legacy_declaration_ambiguous_multisource"
    s = srcs[0]
    d_ev = _events_canon(declaration.get("events") or [])
    a_ev = _events_canon(s.get("events") or [])
    if d_ev != a_ev:
        return False, "events_mismatch"
    dmk = declaration.get("marks_consumed") or declaration.get("marks") or {}
    amk = s.get("marks_consumed") or s.get("marks") or {}
    d_mk = sorted((int(k), (None if v is None else float(v))) for k, v in dmk.items())
    a_mk = sorted((int(k), (None if v is None else float(v))) for k, v in amk.items())
    if d_mk != a_mk:
        return False, "marks_mismatch"
    return True, "matches"


def _table_columns(con, table):
    """表列名集合（容忍旧库缺列 ⇒ 只读旧账本不报错、不迁移）。"""
    try:
        return {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}
    except Exception:  # noqa: BLE001
        return set()


def _ledger_funding_rows(con, run_id):
    """隔离账本**实际**资金费入账行（含 position→exchange/symbol、rate、mark、amount、run_id）。

    第33 加性：`qty` / `rate_source` / `rate_side` / `rate_substituted` 列**若存在**则一并读出
    （旧库缺列 ⇒ 不 SELECT、不报错、不迁移），供**实际使用凭证**与逐笔审查。
    """
    have = _table_columns(con, "research_funding")
    extra = [c for c in ("rate_source", "rate_side", "rate_substituted", "qty") if c in have]
    cols = ", ".join(["f.run_id AS run_id", "f.position_id AS position_id",
                      "p.exchange AS exchange", "p.symbol AS symbol",
                      "f.funding_time AS funding_time", "f.rate AS rate", "f.mark AS mark",
                      "f.amount AS amount", "f.mark_source AS mark_source"]
                     + ["f.%s AS %s" % (c, c) for c in extra])
    rows = con.execute(
        "SELECT " + cols + " FROM research_funding f JOIN research_position p "
        "ON f.run_id = p.run_id AND f.position_id = p.position_id "
        "WHERE f.run_id = ?", (run_id,)).fetchall()
    out = []
    for r in rows:
        try:
            keys = list(r.keys())
        except AttributeError:
            keys = ["run_id", "position_id", "exchange", "symbol", "funding_time", "rate",
                    "mark", "amount", "mark_source"] + extra
        out.append({k: r[k] for k in keys})
    return out


def _ledger_position_rows(con, run_id):
    """隔离账本**实际**持仓行（含 entry_time/exit_time/status/quantity ⇒ 存续期与数量）。"""
    rows = con.execute("SELECT * FROM research_position WHERE run_id=?", (run_id,)).fetchall()
    out = []
    for r in rows:
        try:
            keys = list(r.keys())
        except AttributeError:
            keys = ["run_id", "position_id", "exchange", "symbol", "entry_time", "entry_price",
                    "quantity", "notional", "stop_price", "safety_exit_at", "exit_time",
                    "exit_price", "exit_reason", "gross_pnl", "fees", "funding", "net_pnl",
                    "status", "reservation_amount"]
        out.append({k: r[k] for k in keys})
    return out


def _ledger_order_rows(con, run_id):
    """隔离账本**实际**订单行（成交单 fill_price×quantity ⇒ 数量独立来源）。"""
    rows = con.execute("SELECT * FROM research_order WHERE run_id=? ORDER BY order_id",
                       (run_id,)).fetchall()
    out = []
    for r in rows:
        try:
            keys = list(r.keys())
        except AttributeError:
            keys = ["run_id", "order_id", "intent_id", "status", "reject_reason", "fill_time",
                    "fill_price", "quantity", "fee", "slippage", "notional", "source"]
        out.append({k: r[k] for k in keys})
    return out


# ── 第40 加性：真实成交单方向/净数量独立重建（用于费用审计的数量强勾稽）────────────
TRUE_HISTORICAL_MODELS = ("historical_events", "historical", "historical_events:v38",
                          "HistoricalFundingModel")


def _norm_fill_side(raw_side):
    """成交单方向归一（仅接受显式买/卖语义；`long/short` 是**持仓方向**不当作成交方向）。"""
    if raw_side is None:
        return None
    s = str(raw_side).strip().upper()
    if s in ("BUY", "B", "ENTRY", "OPEN", "LONG_OPEN", "BUY_OPEN"):
        return "BUY"
    if s in ("SELL", "S", "EXIT", "CLOSE", "LONG_CLOSE", "SELL_CLOSE"):
        return "SELL"
    return None


def _fill_direction(fill, pos):
    """单笔成交单方向：优先显式 `side` 字段；否则按 `fill_time` 与持仓 entry/exit 对齐推断。

    `fill_time == entry_time` ⇒ 买入（开仓）；`fill_time == exit_time` ⇒ 卖出（平仓）。
    无法判定 ⇒ None（调用方 fail-closed 记 `invalid_order_fill_provenance`）。
    """
    for k in ("side", "fill_side", "order_side", "direction"):
        if k in (fill or {}):
            d = _norm_fill_side(fill.get(k))
            if d:
                return d
    ft = fill.get("effective_fill_time")
    if ft is None:
        ft = fill.get("fill_time")
    if ft is None or pos is None:
        return None
    et, xt = pos.get("entry_time"), pos.get("exit_time")
    if et is not None and int(ft) == int(et):
        return "BUY"
    if xt is not None and int(ft) == int(xt):
        return "SELL"
    return None


def _finite_num(v):
    """有效有限数字（int/float/可解析数字串）⇒ float；None/非数字/NaN/Inf ⇒ None。

    第41：金额 / 数量 / 费率 / 标价出现**非有效数字**时，审查器必须**拒绝**（不得让 NaN
    在 `nan > tol` 恒 False 的比较中逃逸）。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _fill_scope_mismatch(fill, pos):
    """成交单**显式声明**的所/币/身份段与持仓不符 ⇒ True（用于逐订单所/币核对）。

    第41：同一持仓 ID 借**别的交易所/别的币/别的身份段**的成交单凑数量必须被拒。仅当成交单
    **声明了**某维度且与持仓不等时判不符；字段缺失（真实 `research_order` 无 exchange/symbol
    列，由 `position_id` 唯一绑定）不判不符，避免把合法旧账本误杀。
    """
    for fk, pk in (("exchange", "exchange"), ("symbol", "symbol"),
                   ("contract_generation", "contract_generation")):
        fv = fill.get(fk)
        if fv is None:
            continue
        pv = pos.get(pk)
        if pv is not None and str(fv) != str(pv):
            return True
    return False


def _net_qty_before(pos, fills, T):
    """`effective_fill_time < T` 的**净持仓数量**（多头 = 买入累加 − 卖出累加）。

    严格 **`< T`**：同 T 买入不计入、同 T 卖出在资金费结算完成后才生效 ⇒ **不前瞻减仓**。
    任一成交单缺时刻/方向/数量，或数量为**非有效数字**（NaN/Inf/非数字）⇒ `(None, False)`
    （调用方 fail-closed；第41b：真实成交源数量也必须有效有限，不得让 NaN 逃逸）。
    """
    net = 0.0
    for o in (fills or []):
        ft = o.get("effective_fill_time")
        if ft is None:
            ft = o.get("fill_time")
        if ft is None or o.get("quantity") is None:
            return None, False
        q = _finite_num(o.get("quantity"))
        if q is None:
            return None, False
        d = _fill_direction(o, pos)
        if d is None:
            return None, False
        if int(ft) < int(T):
            net += q if d == "BUY" else -q
    return net, True


def funding_usage_voucher(con, run_id, *, complete_identity=None, model=None):
    """第33：**实际使用凭证**（从**同一真实账本**派生，**指向完整输入版本**）。

    来源 = 真实账本 `research_run` + `research_position` + `research_order` + `research_funding`
    （**只读**；无 `research_run` 行 ⇒ `available=False`，调用方字典/另一本账/无真实 run 记录
    **不构成凭证**）。逐行记 position / 所 / 币 / T / rate / mark / amount / 数量(+来源) /
    费率来源标签。`voucher_id` 因持仓集合变化而不同，**不与完整输入 ID 强行比较**。
    """
    run = con.execute("SELECT * FROM research_run WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        return {"schema": FUNDING_USAGE_VOUCHER_SCHEMA, "run_id": run_id, "available": False,
                "reason": "no_research_run_row", "record_source": "ledger:research_run",
                "complete_input_identity": complete_identity, "model": model,
                "rows": [], "n_rows": 0, "voucher_id": None}
    positions = _ledger_position_rows(con, run_id)
    orders = _ledger_order_rows(con, run_id)
    fund = _ledger_funding_rows(con, run_id)
    pos_by_id = {p["position_id"]: p for p in positions}
    qty_by_pos = {}
    for o in orders:
        if o.get("status") == "fill" and o.get("quantity") is not None:
            qty_by_pos.setdefault(o.get("intent_id"), []).append(float(o["quantity"]))
    rows = []
    for f in fund:
        pid = f["position_id"]
        p = pos_by_id.get(pid) or {}
        fill_q = qty_by_pos.get(pid) or []
        fq = f.get("qty")
        if fq is not None:
            qty, qsrc = float(fq), "funding.qty"
        elif p.get("quantity") is not None:
            qty, qsrc = float(p["quantity"]), "position.quantity"
        elif fill_q:
            qty, qsrc = float(fill_q[0]), "order.fill_quantity"
        else:
            qty, qsrc = None, None
        rows.append({"position_id": pid, "exchange": f.get("exchange"), "symbol": f.get("symbol"),
                     "funding_time": (int(f["funding_time"])
                                      if f.get("funding_time") is not None else None),
                     "rate": f.get("rate"), "mark": f.get("mark"), "amount": f.get("amount"),
                     "mark_source": f.get("mark_source"), "rate_source": f.get("rate_source"),
                     "rate_substituted": f.get("rate_substituted"),
                     "qty": qty, "qty_source": qsrc})
    run_rec = {"arm": run["arm"], "config_id": run["config_id"],
               "code_sha256": run["code_sha256"], "data_version": run["data_version"],
               "initial_equity": run["initial_equity"]}
    h = hashlib.sha256()
    h.update(FUNDING_USAGE_VOUCHER_SCHEMA.encode())
    h.update(str(run_id).encode())
    h.update(json.dumps([complete_identity, model, run_rec], ensure_ascii=False).encode())
    h.update(json.dumps([[r["position_id"], r["exchange"], r["symbol"], r["funding_time"],
                          r["rate"], r["mark"], r["amount"]] for r in rows],
                        ensure_ascii=False).encode())
    return {"schema": FUNDING_USAGE_VOUCHER_SCHEMA, "run_id": run_id, "available": True,
            "reason": None, "record_source": "ledger:research_run+position+order+funding",
            "complete_input_identity": complete_identity, "model": model,
            "run": run_rec, "rows": rows, "n_rows": len(rows),
            "voucher_id": "fu:" + h.hexdigest()[:32]}


def audit_funding_usage(complete_materials, *, con=None, run_id=None, ledger_rows=None,
                        position_rows=None, order_rows=None, freeze_ts=None, model=None,
                        tol_abs=0.01, tol_total_abs=0.50, complete_identity=None,
                        rate_series_by_sym=None, account=None):
    """第33/40：**逐笔双向审查**（正向逐行 + 反向应收付集合；二者都必须有）。

    · **正向**：每笔已入账费用行 ↔ 固定模型事件（`(所,币,T)` 命中）、正确持仓、真实 mark、
      **数量（独立来源）**、`amount == -qty*mark*rate`、费率幅度符号保留；估算行须带标签且**真缺项**。
    · **反向**：由**真实持仓存续**（`entry_time < T <= 真实 exit_time`）+ **固定费用事件**
      推**应收付集合**，对 actual 逐 `(position_id,T)` 查 **缺行 / 多扣 / 重复 / 出持仓外 /
      错币或 run / 另一账本**；**空账本但有实际持仓与应扣事件 ⇒ 不 PASS**。
    · **边界**：严格冻结 `T_freeze` 及以后无费用/平仓/有效权益 ⇒ 只审 `T < T_freeze` 有效前缀；
      `exit_time==freeze` 是停算元数据**不是实际平仓**。

    ── 第40 整改（闭环 141a 两处盲点 + 数量时间截点 + 双重容差）─────────────────────────
    ① **数量强勾稽**：`r["qty"]` 必须同时等于 `p["quantity"]` 且等于**同 run/所/币/持仓**的真实成交
       记录在 **`effective_fill_time < T`** 下的**净数量**（多头 = 买入累加 − 卖出累加）；
       **同 T 先结旧仓再更新成交**（同 T 买入不计、同 T 卖出先收）；**严禁**把 `fill_time > T`
       的未来平仓提前扣减 ⇒ `quantity_mismatch_with_order_fills_before_T`。
       数量无任何金额容差。
    ② **来源缺失 fail-closed**：`qty`/`p`/`amount`/`mark`/订单 fill 的**时刻/方向/数量**任一缺失 ⇒
       **结构化 reject**（`fee_row_quantity_null` / `position_not_in_ledger` / `amount_null` /
       `mark_missing_on_charged_row` / `invalid_order_fill_provenance` /
       `no_valid_order_fills_before_T`），**先校验再时序过滤**，**绝不静默跳过、绝不默认无仓**。
    ③ **估计标签严格验伪**：真实历史模型（`TRUE_HISTORICAL_MODELS`）**严禁**任何估计标签；
       允许估计的场景必须**确有真实缺项**（完整材料该 T 真实率为 None/缺标签）且（给
       `rate_series_by_sym` 时）数值**独立重算**等于相邻不利费率 ⇒ 否则
       `unauthorized_estimate_tag` / `estimate_rate_mismatch`。**原真实 rate 存在不准贴估计绕过**。
    ④ **双重容差**：单笔 `|want - amount| <= tol_abs`(0.01U)；全账本累计绝对误差
       `<= tol_total_abs`(0.50U) ⇒ `total_accumulated_error_exceeded`。数量/身份/行数**不适用**容差。
    ⑤ **空集合不得 PASS**：`auditable = bool(expected or charged)`；二者皆空 ⇒ `empty_audit_scope`。

    ── 第41 整改（闭环 Codex 独立核验的 5 处审查器漏洞）─────────────────────────────
    ⑥ **材料缺真实费率不得放行**：`ft` 命中固定事件但 `ev[ft] is None` 且**未标估计** ⇒
       `missing_rate_no_estimate`（不得填任意费率后放行）；**费用行费率空** ⇒
       `missing_rate_on_charged_row`（不得以「金额 0」冒充合法费用）。
    ⑦ **相邻不利估计必须独立可重算**：标估计但**拿不出相邻原始费率序列** ⇒
       `no_adjacent_adverse_rate_source`（不信任调用方自报估计值；有序列则从**原始序列独立重算**
       相邻不利并与行值严格比对）。
    ⑧ **非有效数字拒绝**：金额/数量/费率/标价任一为 NaN/Inf/非数字 ⇒
       `invalid_{amount,qty,mark,rate}_not_finite`（NaN 不参与公式、不污染累计绝对误差）。
    ⑨ **成交单逐订单所/币/身份段核对**：同一 `position_id` 借**别的所/币/身份段**成交单凑数量 ⇒
       `order_fill_scope_mismatch`。

    返回 `{"schema", "ok", "auditable", "forward", "reverse", "voucher", "problems", ...}`。
    """
    problems = []
    # 真实账本行（优先用连接派生；另可显式给行）
    if con is not None and run_id is not None:
        if ledger_rows is None:
            ledger_rows = _ledger_funding_rows(con, run_id)
        if position_rows is None:
            position_rows = _ledger_position_rows(con, run_id)
        if order_rows is None:
            order_rows = _ledger_order_rows(con, run_id)
    ledger_rows = ledger_rows or []
    position_rows = position_rows or []
    order_rows = order_rows or []

    voucher = (funding_usage_voucher(con, run_id, complete_identity=complete_identity, model=model)
               if (con is not None and run_id is not None) else
               {"schema": FUNDING_USAGE_VOUCHER_SCHEMA, "run_id": run_id, "available": False,
                "reason": "no_ledger_context", "rows": [], "n_rows": 0, "voucher_id": None})
    if not voucher.get("available"):
        problems.append("voucher_unavailable:" + str(voucher.get("reason")))

    # 完整输入：逐 (所,币) → {T: rate_or_None} / {T: mark_or_None}
    ev_by_sym, mk_by_sym = {}, {}
    for s in ((complete_materials or {}).get("sources") or []):
        key = (s.get("exchange"), s.get("symbol"))
        ev_by_sym[key] = {int(e[0]): (None if len(e) < 2 else e[1]) for e in (s.get("events") or [])}
        mk_by_sym[key] = {int(k): v for k, v in (s.get("marks") or {}).items()}
    pos_by_id = {p["position_id"]: p for p in position_rows}

    # ── 第40：真实成交单按持仓分组（同 run/所/币/持仓；由真实订单行派生，不由费用行反推）──
    fills_by_pos = {}
    for o in order_rows:
        if o.get("status") != "fill":
            continue
        if run_id is not None and o.get("run_id") is not None and o.get("run_id") != run_id:
            continue
        pid = o.get("intent_id") if o.get("intent_id") is not None else o.get("position_id")
        if pid is None:
            continue
        fills_by_pos.setdefault(pid, []).append(o)

    # ── 正向：每笔已入账费用行 ────────────────────────────────────────────────
    charged, blocked_rows, fwd_fail = set(), set(), []
    total_err = 0.0
    n_formula = 0
    for r in ledger_rows:
        pid = r.get("position_id")
        p = pos_by_id.get(pid)
        ft = int(r["funding_time"]) if r.get("funding_time") is not None else None
        key = (r.get("exchange"), r.get("symbol"))
        reasons = []
        # ① 结构化存在性（**先校验字段，再时序过滤**；缺项即 reject，不静默跳过）
        if p is None:
            reasons.append("position_not_in_ledger")
        if ft is None:
            reasons.append("funding_time_null")
        if r.get("qty") is None:
            reasons.append("fee_row_quantity_null")
        if r.get("amount") is None:
            reasons.append("amount_null")
        if r.get("mark") is None:
            reasons.append("mark_missing_on_charged_row")
        if r.get("rate") is None:
            # 第41：费用行费率为空 ⇒ 拒绝；不得以「金额 0」冒充合法费用（原率存在时更须拒）
            reasons.append("missing_rate_on_charged_row")
        # ①b 数值有效性：金额/数量/费率/标价任一为**非有效数字**（NaN/Inf/非数字）⇒ 拒绝
        amt_v = _finite_num(r.get("amount"))
        if r.get("amount") is not None and amt_v is None:
            reasons.append("invalid_amount_not_finite")
        qty_v = _finite_num(r.get("qty"))
        if r.get("qty") is not None and qty_v is None:
            reasons.append("invalid_qty_not_finite")
        mark_v = _finite_num(r.get("mark"))
        if r.get("mark") is not None and mark_v is None:
            reasons.append("invalid_mark_not_finite")
        rate_v = _finite_num(r.get("rate"))
        if r.get("rate") is not None and rate_v is None:
            reasons.append("invalid_rate_not_finite")
        fills = fills_by_pos.get(pid) or []
        if p is not None and not fills:
            reasons.append("no_valid_order_fills_before_T")
        # ② 订单来源完整性（时刻/方向/数量任一缺 ⇒ 结构化 reject）+ 逐订单所/币/身份段核对
        if fills:
            prov_bad = False
            qty_bad = False
            for o in fills:
                ft_fill = (o.get("effective_fill_time")
                           if o.get("effective_fill_time") is not None else o.get("fill_time"))
                if ft_fill is None or o.get("quantity") is None or _fill_direction(o, p) is None:
                    prov_bad = True
                    break
                # 第41b：真实成交数量必须为**有效有限数值**（NaN/Inf/非数字 ⇒ 结构化拒绝，
                #        不得仅检查「非 None」后让净量 NaN 在差异比较中逃逸）
                if _finite_num(o.get("quantity")) is None:
                    qty_bad = True
                    break
            if prov_bad:
                reasons.append("invalid_order_fill_provenance")
            if qty_bad:
                reasons.append("invalid_order_fill_quantity")
            # 第41：借别的所/币/身份段的成交单凑数量 ⇒ 拒绝（逐订单核对）
            if p is not None and any(_fill_scope_mismatch(o, p) for o in fills):
                reasons.append("order_fill_scope_mismatch")
        # ③ 存续区间（严格 (entry, exit]；不可算持仓按冻结前缀）
        if p is not None and ft is not None:
            if (p.get("exchange"), p.get("symbol")) != key:
                reasons.append("coin_exchange_mismatch")
            et, xt = p.get("entry_time"), p.get("exit_time")
            if p.get("status") == "uncomputable":
                eff = freeze_ts if freeze_ts is not None else xt
                if not (et is not None and eff is not None and et < ft < eff):
                    reasons.append("out_of_position_lifetime")
            elif not (et is not None and ft is not None and xt is not None and et < ft <= xt):
                reasons.append("out_of_position_lifetime")
        ev = ev_by_sym.get(key, {})
        mk = mk_by_sym.get(key, {})
        if ft not in ev:
            reasons.append("time_not_fixed_model_event")
        row_rate = rate_v  # 第41：用**已校验有限**的费率（None 表示缺失/非法）
        # 第41b：**源费率/标价**必须为有效有限数值（NaN/Inf/非数字 ⇒ 结构化拒绝，
        #        否则 `abs(nan - x) > tol` 恒 False，比较被静默逃逸）。
        src_rate_v = _finite_num(ev[ft]) if (ft in ev and ev[ft] is not None) else None
        if ft in ev and ev[ft] is not None and src_rate_v is None:
            reasons.append("invalid_source_rate")
        src_mark_v = _finite_num(mk[ft]) if (ft in mk and mk[ft] is not None) else None
        if ft in mk and mk[ft] is not None and src_mark_v is None:
            reasons.append("invalid_source_mark")
        est = (r.get("rate_substituted") == 1
               or r.get("rate_source") == "adjacent_adverse_estimate")
        # ④ 估计标签严格验伪（141a 盲点2）
        if est:
            if model in TRUE_HISTORICAL_MODELS:
                reasons.append("estimate_forbidden_in_true_historical")
            else:
                if not r.get("rate_source"):
                    reasons.append("estimate_without_source_label")
                if ft in ev and ev[ft] is not None:
                    reasons.append("unauthorized_estimate_tag")
                series = (rate_series_by_sym or {}).get(key)
                # 第41：标「相邻不利估计」却**拿不出相邻原始费率材料** ⇒ 直接拒绝（不得任意费率放行）；
                #       相邻不利必须**从原始费率序列独立重算**，不信任调用方自报估计值。
                if series is None:
                    reasons.append("no_adjacent_adverse_rate_source")
                elif ft is not None:
                    sel = select_adverse_funding_rate(series, ft, side="long")
                    adv_v = _finite_num(sel.get("rate")) if sel.get("ok") else None
                    if not sel.get("ok"):
                        reasons.append("estimate_rate_not_recomputable")
                    elif adv_v is None:
                        # 第41b：相邻不利源费率为非有效数字 ⇒ 拒绝（不得让 NaN 比较逃逸）
                        reasons.append("invalid_adverse_rate_source")
                    elif row_rate is None or abs(adv_v - row_rate) > 1e-15:
                        reasons.append("estimate_rate_mismatch")
        elif src_rate_v is not None and row_rate is not None:
            if abs(src_rate_v - row_rate) > 1e-15:
                reasons.append("rate_not_fixed_input")
        elif ft in ev and ev[ft] is None:
            # 第41：原材料该时点**缺真实费率**，费用行却填了费率且未标估计 ⇒ 拒绝（不得任意费率放行）
            reasons.append("missing_rate_no_estimate")
        if mark_v is not None:
            if ft in mk and mk[ft] is not None:
                if src_mark_v is None:
                    pass    # 已记 invalid_source_mark
                elif abs(src_mark_v - mark_v) > 1e-12:
                    reasons.append("mark_not_complete_input")
            else:
                reasons.append("mark_not_complete_input")
        # ⑤ 数量强勾稽（独立来源；无容差）
        row_qty = None
        if "fee_row_quantity_null" not in reasons and "position_not_in_ledger" not in reasons:
            row_qty = qty_v
            if row_qty is None:
                reasons.append("fee_row_quantity_null")
            else:
                # 第41b：持仓数量也必须为有效有限数值
                pos_qty = _finite_num(p.get("quantity"))
                if p.get("quantity") is None:
                    reasons.append("position_quantity_null")
                elif pos_qty is None:
                    reasons.append("invalid_position_quantity")
                elif abs(row_qty - pos_qty) > 1e-12:
                    reasons.append("quantity_mismatch_with_position")
                if (ft is not None and "invalid_order_fill_provenance" not in reasons
                        and "invalid_order_fill_quantity" not in reasons
                        and "no_valid_order_fills_before_T" not in reasons):
                    net, ok = _net_qty_before(p, fills, ft)
                    if not ok:
                        reasons.append("invalid_order_fill_provenance")
                    elif abs(row_qty - float(net)) > 1e-12:
                        reasons.append("quantity_mismatch_with_order_fills_before_T")
        # ⑥ 金额公式 + 双重容差（仅当四个分量**皆有效有限**时核；NaN/Inf 不参与、不污染累计）
        if amt_v is not None and row_qty is not None and mark_v is not None and row_rate is not None:
            want = -row_qty * mark_v * row_rate
            err = abs(want - amt_v)
            total_err += err
            n_formula += 1
            if err > max(tol_abs, abs(want) * 1e-9):
                reasons.append("amount_formula_mismatch")
        if (not est and src_rate_v is not None and row_rate is not None
                and src_rate_v != 0 and row_rate != 0
                and (src_rate_v > 0) != (row_rate > 0)):
            reasons.append("rate_sign_flipped")
        if amt_v is not None:
            charged.add((pid, ft))
        else:
            blocked_rows.add((pid, ft))
        if reasons:
            fwd_fail.append({"position_id": pid, "exchange": key[0], "symbol": key[1],
                             "funding_time": ft, "reasons": reasons})

    # ── 反向：由持仓存续 + 固定事件推应收付集合 ───────────────────────────────
    expected = set()
    for p in position_rows:
        pid = p["position_id"]
        key = (p.get("exchange"), p.get("symbol"))
        ev = ev_by_sym.get(key, {})
        et, xt = p.get("entry_time"), p.get("exit_time")
        if p.get("status") == "uncomputable":
            eff = freeze_ts if freeze_ts is not None else xt
            ts = ([t for t in ev if et is not None and t is not None and et < t < eff]
                  if eff is not None else [])
        else:
            ts = ([t for t in ev if et is not None and t is not None and et < t <= xt]
                  if xt is not None else [])
        for t in ts:
            expected.add((pid, int(t)))
    missing = sorted(expected - charged)                 # 应扣未扣（含被阻断）
    extra = sorted(charged - expected)                   # 多扣 / 出持仓外
    seen, dup = set(), []
    for r in ledger_rows:
        k = (r.get("run_id"), r.get("position_id"),
             int(r["funding_time"]) if r.get("funding_time") is not None else None)
        if k in seen:
            dup.append(k)
        seen.add(k)
    blocked_in_expected = sorted(blocked_rows & expected)
    wrong_run = sorted({r.get("run_id") for r in ledger_rows
                        if r.get("run_id") is not None and run_id is not None
                        and r.get("run_id") != run_id})

    auditable = bool(expected or charged)
    if fwd_fail:
        problems.append("forward_row_fail")
    if missing:
        problems.append("reverse_missing_charge")
    if extra:
        problems.append("reverse_extra_charge")
    if dup:
        problems.append("reverse_duplicate_row")
    if blocked_in_expected:
        problems.append("reverse_blocked_expected")
    if wrong_run:
        problems.append("ledger_run_mismatch")
    if not charged and expected:
        problems.append("empty_ledger_with_expected_charges")
    if not auditable:
        problems.append("empty_audit_scope")
    if total_err > tol_total_abs:
        problems.append("total_accumulated_error_exceeded")
    return {"schema": FUNDING_USAGE_AUDIT_SCHEMA, "ok": (not problems) and auditable,
            "auditable": auditable,
            "n_ledger_rows": len(ledger_rows), "n_charged": len(charged),
            "n_expected": len(expected),
            "total_abs_error": total_err, "n_formula_rows": n_formula,
            "tol_abs": tol_abs, "tol_total_abs": tol_total_abs,
            "forward": {"ok": not fwd_fail, "fail": fwd_fail[:50]},
            "reverse": {"ok": not (missing or extra or dup or blocked_in_expected),
                        "n_expected": len(expected), "n_charged": len(charged),
                        "missing": missing[:50], "extra": extra[:50],
                        "duplicate": dup[:50], "blocked_in_expected": blocked_in_expected[:50],
                        "wrong_run": wrong_run},
            "voucher": voucher, "problems": problems}


def child_run_identity(con, run_id, *, run_binding=None, data_version=None,
                       code_sha256=None, parent_batch_run_id=None):
    """第21：从**隔离账本实际记录**（`research_run` 行）构造**当前执行子运行**身份记录。

    记录**只来自真实账本行**（`record_source="ledger:research_run"`）：行不存在 ⇒ `None`
    （不可核 ⇒ 门禁三维 `run_match=None`，**不得**凭调用方字典冒充）。除账本记录字段外，附
    **本次实际消费**维度（`data_version` / `daily_list_version` / `funding_model`，取自
    `run_binding`）供门禁做**内容对应**：子运行**实际消费**值 == 真实解析来源值 ⇒ 当前运行匹配。
    `record_sha256` 覆盖账本记录字段（`wx_gate.child_run_record_sha256`，与门禁同源，防记录内篡改）。

    `parent_batch_run_id`（可选）：门禁**批次级** run id（由 `wx_gate.binding()` 实算身份派生）；
    门禁侧若声明须与批次 run 相符（不符 ⇒ `parent_batch_run_mismatch`）。
    """
    row = con.execute("SELECT * FROM research_run WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return None
    try:
        keys = list(row.keys())
    except AttributeError:
        keys = ["run_id", "arm", "config_id", "strategy_version", "config_hash", "data_version",
                "engine", "engine_version", "adapter_version", "code_sha256",
                "initial_equity", "created_at", "note"]
    rec = {k: row[k] for k in keys}
    out = {
        "record_source": "ledger:research_run",
        "run_id": run_id,
        "arm": rec.get("arm"),
        "config_id": rec.get("config_id"),
        "data_version": data_version if data_version is not None else rec.get("data_version"),
        "code_sha256": code_sha256 if code_sha256 is not None else rec.get("code_sha256"),
        "config_hash": rec.get("config_hash"),
        "initial_equity": rec.get("initial_equity"),
        "created_at": rec.get("created_at"),
    }
    from . import wx_gate as G           # 延迟导入（wx_gate 亦延迟导入本模块）
    out["record_sha256"] = G.child_run_record_sha256(out)
    # 本次**实际消费**维度（用于门禁**内容对应**；非账本行字段）
    rb = run_binding if isinstance(run_binding, dict) else {}
    if rb.get("daily_list_version") is not None:
        out["daily_list_version"] = rb.get("daily_list_version")
    if rb.get("funding_model") is not None:
        out["funding_model"] = rb.get("funding_model")
    if parent_batch_run_id is not None:
        out["parent_batch_run_id"] = parent_batch_run_id
    return out


def daily_list_doc_from_maps(rank_keys_by_day, admission_by_day, *, scope, usage_mask):
    """第21：把 `run_arm` **本次实际使用**的逐日排序/准入映射序列化为规范化日名单文档。

    与 `wx_gate.DAILY_LIST_MAP_SCHEMA` 形状一致（`days[*].order` / `admission`，键 = `exchange:symbol`），
    使 `daily_list_version` 可从**当前运行真实使用内容**解析（**不**硬编码一个不存在的默认文件路径）。
    """
    from . import wx_gate as G
    days = sorted(set(rank_keys_by_day or {}) | set(admission_by_day or {}))
    out_days = []
    for day in days:
        rk = (rank_keys_by_day or {}).get(day) or {}
        adm = (admission_by_day or {}).get(day) or {}
        order = [f"{ex}:{sym}" for (ex, sym) in
                 sorted(rk, key=lambda k: (-float(rk.get(k, 0.0)), str(k[0]), str(k[1])))]
        out_days.append({"day_ms": int(day), "order": order,
                         "admission": {f"{ex}:{sym}": bool(v) for (ex, sym), v in adm.items()}})
    return {"schema": G.DAILY_LIST_MAP_SCHEMA, "refresh": "UTC00",
            "window": "prior 30 complete UTC days", "scope": scope, "usage_mask": usage_mask,
            "days": out_days}


def declaration_matches_source(declared, real_source, fields=("sha256", "source")):
    """**纯函数**：调用方声明与真实来源逐字段比对（声明**只能核对、不得覆盖**）。

    返回 (ok, diffs)。`declared is None` ⇒ 「无声明」⇒ (True, {})（不构成错误，但不提供凭据）；
    非 dict 声明 ⇒ (False, ...)；任一字段不等 ⇒ (False, diffs)。
    """
    if declared is None:
        return True, {}
    if not isinstance(declared, dict):
        return False, {"_declared": {"declared": type(declared).__name__, "real": "object"}}
    src = real_source if isinstance(real_source, dict) else {}
    diffs = {f: {"declared": declared.get(f), "real": src.get(f)}
             for f in fields if declared.get(f) != src.get(f)}
    return (not diffs), diffs


def _structured_match(declared, source):
    """声明与**已核来源**的结构化比对（source 为空/None ⇒ 不可核）。**纯函数**。

    · 双方 dict：整体相等，或来源的输入关键 sha 字段（`snapshot_sha256` / `manifest_sha256`）
      在声明中**同值** ⇒ 一致；
    · 双方标量（str/int）：相等即一致；
    · 类型不符 / 声明为空 ⇒ 不一致。
    """
    if source in (None, "", {}, []):
        return False
    if isinstance(declared, dict) and isinstance(source, dict):
        if declared == source:
            return True
        keys = [k for k in ("snapshot_sha256", "manifest_sha256") if k in source]
        return bool(keys) and all(declared.get(k) == source.get(k) for k in keys)
    if isinstance(declared, (str, int)) and isinstance(source, (str, int)):
        return declared == source
    return False


def _extract_gate_sources_fallback(rg):
    """`wx_gate` 不可用时的**只读兜底**：不猜、不从不存在键派生；三维记 `gate_module_unavailable`。"""
    vp = rg.get("verified_payload") if isinstance(rg.get("verified_payload"), dict) else None
    blocked = {d: {"dimension": d, "status": "blocked", "reason": "gate_module_unavailable",
                   "value": None, "scope": None, "run_id": None, "run_match": None,
                   "run_status": "undeterminable", "run_reason": "gate_module_unavailable",
                   "formal_ok": False, "formal_ready": False}
               for d in ("daily_list_version", "funding_model", "data_version")}
    cds = {"schema": None, "run_id": None, "batch_run_id": None, "child_run_id": None,
           "requested_child_run_id": None,
           "child_run_state": "invalid", "child_run_reason": "gate_module_unavailable",
           "dimensions": blocked, "resolved": [], "source_ok": [],
           "blocked": list(blocked), "formal_ok": [], "formal_ready": False,
           "reason": "gate_module_unavailable"}
    common = {"daily_list_version": None, "input_snapshot": None, "funding_model": None,
              "data_version": None, "consumer_dim_sources": cds,
              "input_snapshot_candidate_dim": "data",
              "interface_missing": [], "blocked_dimensions": list(blocked),
              "batch_run_id": None, "child_run_id": None, "requested_child_run_id": None,
              "child_run_state": "invalid", "child_run_reason": "gate_module_unavailable",
              "parent_batch_run_declared": None, "parent_batch_run_ok": None,
              "source": rg.get("source"), "sha256": rg.get("sha256")}
    if vp is None:
        return {"schema": None, "available": False, "accepted": False,
                "reason": "verified_payload_absent", "run_identity": {}, "run_id": None,
                "run_binding_ok": bool(rg.get("run_binding_ok")),
                "real_bindings_ok": bool(rg.get("real_bindings_ok")),
                "verified_bindings": {}, **common}
    ri = vp.get("run_identity") if isinstance(vp.get("run_identity"), dict) else {}
    return {"schema": vp.get("schema"), "available": bool(vp.get("accepted")),
            "accepted": bool(vp.get("accepted")), "reason": vp.get("reason"),
            "run_identity": dict(ri), "run_id": vp.get("run_id"),
            "run_binding_ok": bool(rg.get("run_binding_ok")),
            "real_bindings_ok": bool(rg.get("real_bindings_ok")),
            "verified_bindings": dict(vp.get("verified_bindings") or {}), **common}


def extract_gate_sources(real_gate, *, funding_actual=None, funding_ledger=None, model=None,
                         daily_list_path=None, manifest_path=None, daily_list_usage=None,
                         daily_list_doc=None, daily_list_source=None, child_run=None, run=None,
                         isolated=False, ledger=None):
    """从 `wx_gate.binding()` 的**真实返回结构**提取已核来源（第19 接线；第20 扩展；第21 贯通）。

    第17 执行器读 `real_gate["bindings"]`（`daily_list_version`/`input_snapshot`）与顶层
    `real_gate["data_version"]`，但真实返回（含 `_pending`）**没有**这些键 ⇒ 恒 None。本函数经
    `wx_gate.real_sources` 读取**真实返回恒带的** `verified_payload`（第19 真实验证链产出）与
    **第20 消费者三维真实来源契约** `consumer_dim_sources`。`wx_gate` 不可用时按**同一真实字段**
    直接读取（不读不存在键、三维记 `gate_module_unavailable`，**不恒 None**）。

    返回：
      · `available` / `accepted` / `reason` / `schema`：真实门禁是否给出**已核**结构及原因；
      · `run_id` / `run_identity`：本次 run 身份（真实键）；
      · `verified_bindings`：经真实验证链核验的**台账声明值**（来源=独立台账，非调用方）；
      · `daily_list_version` / `funding_model` / `data_version`：第20 由**真实来源契约**解析
        （未解析 ⇒ None，但 `consumer_dim_sources[dim].reason` 给出**具体原因**，非恒 None）；
      · `consumer_dim_sources`：三维完整契约（含 `run_correspondence` 当前 run 对应）。

    **第22**：`ledger`（本次**真实账本连接**）一并贯通到 `wx_gate.real_sources` ⇒ 子运行记录须与
    真实 `research_run` 行逐字段一致；无账本上下文 / 无该 run 行 / 行与记录不符 ⇒ 子运行 `invalid`
    （`run_match` 不为 True），**声明字典/自算 hash 不构成来源**。
    """
    rg = real_gate if isinstance(real_gate, dict) else {}
    try:
        from . import wx_gate
        # 第21：把**本次实际材料/账本/模型、实际使用名单内容、当前执行子运行**一并贯通到
        # 真实生产来源接口（第20 只传 `real_gate` ⇒ 新来源接口仍错报缺费用材料；104a 缺陷1）。
        return wx_gate.real_sources(
            rg, funding_actual=funding_actual, funding_ledger=funding_ledger, model=model,
            daily_list_path=daily_list_path, manifest_path=manifest_path,
            daily_list_usage=daily_list_usage, daily_list_doc=daily_list_doc,
            daily_list_source=daily_list_source, child_run=child_run, run=run, isolated=isolated,
            ledger=ledger)
    except Exception:  # noqa: BLE001 —— 门禁不可用时按**同一真实字段**直接读取（不读不存在键）
        return _extract_gate_sources_fallback(rg)


def binding_actual_consistency(binding, backend_dir=None, real_gate=None, data_version=None,
                               funding_actual=None, funding_ledger=None,
                               funding_declaration=None, *, child_run=None, run=None,
                               daily_list_path=None, manifest_path=None, daily_list_usage=None,
                               daily_list_doc=None, daily_list_source=None, model=None,
                               ledger=None, funding_usage=None):
    """核验六维绑定是否与**本轮实际来源**一致（不只是字段非空 / 前缀 / 格式）。

    · `code_sha256`      ↔ 实算源码包 sha（`compute_code_bundle_sha`）**逐字节**比对；
    · `strategy_config`  ↔ 实算 `cfg:{视图sha}:{18生效身份sha}`（并与门禁 `configs_18_sha256` 交叉）；
    · `scope`            ↔ 本轮唯一授权范围 `binance_um_526`（**不**用门禁 549/pilot 范围冒充）；
    · `funding_model`    ↔ **第19 实值核验（对实际材料）**：语法 `fundmodel:(norm4h|ext1h):<32hex>`
      **且** 对 `funding_actual`（**实际材料**，`FUNDING_MATERIALS_SCHEMA`；由 `run_arm` 从本次
      真实执行输入 `symbol_data` + 真正消费 mark 派生，**非调用方字典**）用
      `funding_materials_identity` 重算并**逐字节**比对；同时
        - `funding_ledger`（隔离账本**实际入账**行）逐 (所,币,时点) 交叉核对 rate/mark；
        - `funding_declaration`（调用方**材料声明**，可选）与实际材料结构核对（材料是声明）。
      任一不一致 ⇒ 分量失败并列原因；无可核材料 ⇒ `funding_model_not_verified`；
    · `daily_list_version` / `input_snapshot` / `data_version`：本地**不可独立实算**，由
      **真实独立接受来源**（`real_gate`，即 `wx_gate.binding()`）按**真实返回结构**核对。
      `input_snapshot` 的已核来源 = 门禁七维中的 `data`（输入文件集合 + manifest sha）；
      **第20 起** `daily_list_version` / `data_version` 由 `wx_gate.real_sources` 的
      `consumer_dim_sources` 契约解析（逐日映射**已存文件或本次实际使用内容**规范化指纹 /
      正式 manifest **仅币安 526** 子集指纹）⇒ **可核则核**；**缺材料/错范围/run 不符 ⇒ 记具体原因**
      `*_not_independently_verified`（**不再**恒「接口缺项」）。本次 run 身份须具体（run_id 非空）。
      **第21**：三维各自**分列** `source_resolved`（源解析）/ `run_matched`（**当前执行子运行**内容
      对应）/ `independently_verified`（= 源解析 ∧ 当前运行匹配）。`child_run`（当前执行子运行记录，
      来自真实账本 `research_run` 行）经 `extract_gate_sources` **贯通**到真实来源接口；**无**子运行 ⇒
      `run_match=None` ⇒ 不构成已核（**版本值相等不得冒当前运行绑定**，104a 缺陷2）。

    返回 {"ok", "problems", "checks"}；`ok=False` ⇒ 正式就绪 fail closed。
    """
    if not binding:
        return {"ok": False, "problems": ["no_binding"], "checks": {}}
    problems, checks = [], {}

    want_code, _per = compute_code_bundle_sha(backend_dir)
    got_code = binding.get("code_sha256")
    code_ok = (got_code == want_code)
    checks["code_sha256"] = {"declared": got_code, "actual": want_code, "ok": code_ok}
    if not code_ok:
        problems.append("code_sha256_not_actual_source")

    want_cfg = compute_strategy_config_identity()
    got_cfg = binding.get("strategy_config")
    cfg_ok = (want_cfg["value"] is not None and got_cfg == want_cfg["value"])
    checks["strategy_config"] = {"declared": got_cfg, "actual": want_cfg["value"],
                                 "actual_identity_ok": want_cfg["identity_ok"], "ok": cfg_ok}
    if not cfg_ok:
        problems.append("strategy_config_not_actual")
    if not want_cfg["identity_ok"]:
        problems.append("actual_config_identity_drift")

    scope = binding.get("scope")
    scope_ok = (scope == AUTHORIZED_SCOPE)
    checks["scope"] = {"declared": scope, "actual": AUTHORIZED_SCOPE, "ok": scope_ok}
    if not scope_ok:
        problems.append("scope_not_authorized_binance_um_526")

    # ── 资金费模型：语法 + **实值**（对**本次实际材料**重算并逐字节比对 + 账本交叉核对）────────
    fm = binding.get("funding_model")
    fm_fmt_ok = funding_model_registered(fm)          # 仅语法
    fm_chk = {"declared": fm, "registered_models": list(REGISTERED_FUNDING_MODELS),
              "format_ok": fm_fmt_ok, "content_verified": False, "ok": False,
              "materials_schema": FUNDING_MATERIALS_SCHEMA}
    if not fm_fmt_ok:
        fm_chk["reason"] = "format_invalid"
        problems.append("funding_model_not_registered")
    else:
        model, _hx = parse_funding_model(fm)
        mats = funding_actual if isinstance(funding_actual, dict) else None
        n_src = len((mats or {}).get("sources") or []) if mats else 0
        n_ev = sum(len(s.get("events") or []) for s in (mats or {}).get("sources") or []) \
            if mats else 0
        if (mats is None or mats.get("schema") != FUNDING_MATERIALS_SCHEMA
                or n_src == 0 or n_ev == 0):
            # 无可核**实际**材料（缺材料 / 非本 schema / 无任何资金费事件）⇒ 未证并阻断
            # （不误认已登记、不因调用方内部自洽而通过、不把「空事件」当已核）
            fm_chk["reason"] = "no_actual_model_materials"
            fm_chk["n_sources"] = n_src
            fm_chk["n_events"] = n_ev
            problems.append("funding_model_not_verified")
        else:
            recomputed = funding_materials_identity(model, mats)
            decl_ok, decl_why = _declaration_matches_materials(funding_declaration, mats)
            ledger_x = _cross_check_materials_vs_ledger(mats, funding_ledger)
            fm_chk.update({
                "content_verified": True, "recomputed": recomputed, "n_sources": n_src,
                "n_events": n_ev,
                "sources": ["%s:%s" % (s.get("exchange"), s.get("symbol"))
                            for s in mats["sources"]],
                "materials_from": "run_arm_symbol_data+ledger_consumed_marks",
                "declaration_check": {"ok": decl_ok, "why": decl_why},
                "ledger_cross_check": ledger_x,
                "ok": (recomputed == fm) and decl_ok and ledger_x["ok"]})
            if recomputed != fm:
                fm_chk["reason"] = "identity_mismatch"
                problems.append("funding_model_identity_mismatch")
            if not decl_ok:
                fm_chk["reason"] = fm_chk.get("reason") or "declaration_not_actual"
                problems.append("funding_declaration_not_actual")
            if not ledger_x["ok"]:
                fm_chk["reason"] = fm_chk.get("reason") or "ledger_mismatch"
                problems.append("funding_materials_ledger_mismatch")
    checks["funding_model"] = fm_chk

    # ── 门禁真实结构提取（不读不存在的 `bindings` / `data_version`）────────────────────
    # 第21：**贯通**本次实际材料/账本/模型、实际使用名单内容、当前执行子运行（第20 只传 real_gate）。
    # 第22：`ledger`（本次真实账本连接）一并贯通 ⇒ 子运行记录须对真实 `research_run` 行逐字段核验。
    gs = extract_gate_sources(
        real_gate, funding_actual=funding_actual, funding_ledger=funding_ledger, model=model,
        daily_list_path=daily_list_path, manifest_path=manifest_path,
        daily_list_usage=daily_list_usage, daily_list_doc=daily_list_doc,
        daily_list_source=daily_list_source, child_run=child_run, run=run, ledger=ledger)
    cds = gs.get("consumer_dim_sources") if isinstance(gs.get("consumer_dim_sources"), dict) else {}
    cdims = cds.get("dimensions") if isinstance(cds.get("dimensions"), dict) else {}
    checks["gate_sources"] = {
        "available": gs.get("available"), "run_id": gs.get("run_id"),
        "run_binding_ok": gs.get("run_binding_ok"), "real_bindings_ok": gs.get("real_bindings_ok"),
        "interface_missing": gs.get("interface_missing"),
        "source": gs.get("source"), "sha256": gs.get("sha256"),
        # 第20/21：消费者三维真实来源契约（源解析/当前运行对应/正式资格分列）
        "consumer_dim_sources": {
            "schema": cds.get("schema"), "resolved": cds.get("resolved"),
            "source_ok": cds.get("source_ok"),
            "blocked": cds.get("blocked"), "formal_ok": cds.get("formal_ok"),
            "batch_run_id": cds.get("batch_run_id"), "child_run_id": cds.get("child_run_id"),
            "requested_child_run_id": cds.get("requested_child_run_id"),
            "child_run_state": cds.get("child_run_state"),
            "child_run_reason": cds.get("child_run_reason"),
            "child_run_ledger_checked": cds.get("child_run_ledger_checked"),
            "parent_batch_run_declared": cds.get("parent_batch_run_declared"),
            "parent_batch_run_ok": cds.get("parent_batch_run_ok"),
            "run_correspondence": cds.get("run_correspondence")},
        "note": "按 wx_gate 真实返回键 run_identity/real_bindings_detail/run_binding_detail 提取；"
                "第21 贯通实际材料/实际名单内容/当前执行子运行"}

    # 本次 run 身份：门禁须给出具体 run_id **且** run 绑定核验通过（run_binding_ok），否则不可核。
    run_id = gs.get("run_id")
    run_ok_gate = bool(gs.get("run_binding_ok"))
    run_verified = bool(run_id) and run_ok_gate
    checks["run_identity"] = {
        "run_id": run_id, "gate_run_binding_ok": run_ok_gate,
        "independently_verified": run_verified,
        "reason": (None if run_verified else
                   ("gate_no_concrete_run_identity" if not run_id
                    else "gate_run_binding_mismatch"))}
    if not run_verified:
        problems.append("run_identity_not_independently_verified")

    # 本地实算的 18 配置生效身份与门禁已核身份交叉（门禁给值时必须一致）。
    cfg_gate = (gs.get("run_identity") or {}).get("configs_18_sha256")
    if cfg_gate is not None:
        cross_ok = (want_cfg["effective_configs_sha256"] is not None
                    and cfg_gate == want_cfg["effective_configs_sha256"])
        checks["strategy_config"]["gate_configs_18_sha256"] = cfg_gate
        checks["strategy_config"]["gate_cross_ok"] = cross_ok
        if not cross_ok:
            problems.append("strategy_config_gate_mismatch")

    # daily_list_version：第20 由**真实来源契约**解析（逐日映射**已存文件**或**本次实际使用内容**）。
    # 第21：**分列** 源解析（source_resolved）／当前运行匹配（run_matched）／正式独立核验
    # （independently_verified = 源解析 ∧ 当前运行匹配）。缺来源/错范围/用途不符/无当前子运行/
    # 子运行内容不符 ⇒ 具体原因阻断（**不再**把「版本值相等」当成「当前运行已独立核验」）。
    dl_src = cdims.get("daily_list_version") or {}
    dlv = dl_src.get("value") if dl_src.get("source_ok") else None
    dl_source_ok = bool(dlv) and binding.get("daily_list_version") == dlv
    dl_run_match = dl_src.get("run_match")
    dlv_ok = dl_source_ok and dl_run_match is True
    if dlv_ok:
        dl_reason = None
    elif not dl_source_ok:
        dl_reason = (dl_src.get("reason") if dl_src.get("status") != "resolved"
                     else ("scope_not_formal_526" if not dl_src.get("scope_ok")
                           else ("usage_not_formal" if not dl_src.get("usage_ok")
                                 else "declared_daily_list_version_mismatch")))
    else:
        dl_reason = (dl_src.get("run_reason")
                     or ("no_current_child_run" if dl_run_match is None
                         else "child_run_unmatched"))
    checks["daily_list_version"] = {
        "declared": binding.get("daily_list_version"), "real_source": dlv,
        "source_resolved": dl_source_ok, "run_matched": dl_run_match is True,
        "run_match": dl_run_match, "run_status": dl_src.get("run_status"),
        "run_reason": dl_src.get("run_reason"),
        "independently_verified": dlv_ok, "locally_verifiable": False,
        "real_source_status": dl_src.get("status"), "real_source_scope": dl_src.get("scope"),
        "reason": dl_reason,
        "note": "逐日名单版本由真实已存映射文件/本次实际使用内容规范化指纹解析（第20/21 接口）；"
                "independently_verified 还须**当前执行子运行内容对应**"}
    if not dlv_ok:
        problems.append("daily_list_version_not_independently_verified")

    # input_snapshot：门禁七维 `data`（输入文件集合 snapshot_sha256 + manifest_sha256）为已核来源。
    data_bind = (gs.get("verified_bindings") or {}).get("data")
    is_ok = _structured_match(binding.get("input_snapshot"), data_bind)
    checks["input_snapshot"] = {
        "declared": binding.get("input_snapshot"), "real_source": data_bind,
        "independently_verified": is_ok, "locally_verifiable": False,
        "reason": (None if is_ok else ("gate_no_verified_input_binding" if not data_bind
                                       else "declared_input_snapshot_mismatch")),
        "note": "门禁 `data` 维（输入文件集合 + manifest sha）为输入快照的已核来源"}
    if not is_ok:
        problems.append("input_snapshot_not_independently_verified")

    # data_version：第20 由**真实来源契约**解析（正式 manifest **仅币安 526** 子集内容指纹）。
    # 第21：**分列** 源解析（source_resolved）／当前运行匹配（run_matched）／正式独立核验
    # （independently_verified = 源解析 ∧ 当前运行匹配）。**版本值相等 ≠ 当前运行已独立核验**：
    # 无当前子运行 ⇒ `run_match=None` ⇒ 不构成已核（104a 缺陷2）。
    dv_src = cdims.get("data_version") or {}
    dvg = dv_src.get("value") if dv_src.get("source_ok") else None
    dv_source_ok = bool(dvg) and data_version == dvg
    dv_run_match = dv_src.get("run_match")
    dv_ok = dv_source_ok and dv_run_match is True
    if dv_ok:
        dv_reason = None
    elif not dv_source_ok:
        dv_reason = (dv_src.get("reason") if dv_src.get("status") != "resolved"
                     else ("scope_not_formal_526" if not dv_src.get("scope_ok")
                           else ("usage_not_formal" if not dv_src.get("usage_ok")
                                 else "declared_data_version_mismatch")))
    else:
        dv_reason = (dv_src.get("run_reason")
                     or ("no_current_child_run" if dv_run_match is None
                         else "child_run_unmatched"))
    checks["data_version"] = {
        "declared": data_version, "real_source": dvg,
        "source_resolved": dv_source_ok, "run_matched": dv_run_match is True,
        "run_match": dv_run_match, "run_status": dv_src.get("run_status"),
        "run_reason": dv_src.get("run_reason"),
        "independently_verified": dv_ok, "locally_verifiable": False,
        "real_source_status": dv_src.get("status"), "real_source_scope": dv_src.get("scope"),
        "reason": dv_reason,
        "note": "数据版本由正式 manifest 仅币安 526 子集内容指纹解析（第20/21 接口）；"
                "independently_verified 还须**当前执行子运行内容对应**"}
    if not dv_ok:
        problems.append("data_version_not_independently_verified")

    # ── 第33：**逐笔双向审查**（实际使用凭证 + 正向逐行 + 反向应收付集合）────────────────
    # 无账本上下文（纯声明路径）⇒ `provided=False`，不构成失败；给出且不 ok ⇒ fail closed。
    usage = funding_usage if isinstance(funding_usage, dict) else None
    checks["funding_usage"] = {
        "provided": usage is not None,
        "ok": (None if usage is None else bool(usage.get("ok"))),
        "schema": (usage or {}).get("schema"),
        "problems": (usage or {}).get("problems"),
        "n_expected": ((usage or {}).get("reverse") or {}).get("n_expected"),
        "n_charged": ((usage or {}).get("reverse") or {}).get("n_charged"),
        "voucher_available": ((usage or {}).get("voucher") or {}).get("available"),
        "voucher_id": ((usage or {}).get("voucher") or {}).get("voucher_id"),
        "complete_input_identity": ((usage or {}).get("voucher") or {}).get("complete_input_identity"),
        "note": "逐笔双向审查（正向逐行核对 + 反向由持仓存续推应收付）；"
                "无账本上下文 ⇒ not provided（纯声明路径，不构成失败）"}
    if usage is not None and not usage.get("ok"):
        problems.append("funding_usage_audit_failed")
    return {"ok": not problems, "problems": problems, "checks": checks}


def gate_document_valid(doc):
    """校验 `formal_gate` 是**结构自洽**的 wx_gate 门禁文档（拒绝布尔/伪凭据）。

    · 非 dict / 缺关键字段 ⇒ 不成立（`formal_gate_not_gate_document`）；
    · `research_pass=True` 必须同时 `all_pass` 且全部证明位（structure/concrete/
      real_bindings/run_binding/evidence）为真、且非 synthetic/isolated
      ⇒ 否则判自相矛盾（`formal_gate_inconsistent_research_pass`）。
    ⇒ 不能靠「传一个 `{"research_pass": True}`」或任意布尔放行。
    返回 (ok, error)。
    """
    if not isinstance(doc, dict):
        return False, "formal_gate_not_gate_document"
    for k in _GATE_FLAG_FIELDS:
        if k not in doc:
            return False, f"formal_gate_missing_field:{k}"
    if doc.get("synthetic") or doc.get("isolated"):
        return True, None                     # 合法文档，但 research_pass 恒 False
    if doc.get("research_pass"):
        bad = [k for k in _GATE_PROOF_FIELDS if not doc.get(k)]
        if bad or not doc.get("all_pass"):
            return False, "formal_gate_inconsistent_research_pass:" + ",".join(bad or ["all_pass"])
    return True, None


def resolve_formal_gate(resolved, formal_gate=None):
    """取**真实独立接受门禁**文档（`wx_gate.binding()` 结果）。返回 (doc|None, error|None)。

    **第17 收口**：`resolved["run_binding"]`（`wx_config.resolve` 的缓存）与显式 `formal_gate`
    **都只是声明**，**绝不**作为独立接受来源。决策**始终**从真实已存来源重读：
      · 实调 `wx_gate.binding()` 取**当前**接受状态与凭据指纹（只读；不读环境变量、不造凭据）；
      · 真实来源缺失 / 读取异常 / 结构不符 ⇒ (None, error) ⇒ 正式就绪 fail closed；
      · 调用方声明（缓存或显式）**只能与真实来源核对**（`sha256`+`source` 一致）；伪路径、
        全零 SHA、自填布尔 ⇒ 声明不符 ⇒ 记错并**保留真实 `research_pass`**（不覆盖、不放行）。
    """
    # 1) 真实来源：**始终实调**（不信任调用方缓存/声明）
    try:
        from . import wx_gate
        real = wx_gate.binding()
    except Exception as exc:  # noqa: BLE001
        return None, f"formal_gate_unavailable:{type(exc).__name__}"
    ok, err = gate_document_valid(real)
    if not ok:
        return None, err

    # 2) 调用方声明核对（缓存 / 显式）—— 只可核对，不可覆盖真实 research_pass
    declarations = (("resolved_run_binding", (resolved or {}).get("run_binding")),
                    ("formal_gate", formal_gate))
    for name, decl in declarations:
        if decl is None:
            continue
        if not isinstance(decl, dict):
            return real, f"{name}_declaration_not_document"
        match, _diffs = declaration_matches_source(decl, real)
        if not match:
            return real, f"{name}_declaration_mismatch"
    return real, None


def compute_formal_ready(*, binding_ok, binding, data_version, daily_enforced,
                         input_nonempty, blocked, recon_ok, consistency,
                         gate_doc, gate_error=None):
    """**正式就绪判据**（fail-closed）。返回 (formal_ready, missing[])。

    **正式就绪 ≠ 工程绑定完整 ≠ 局部对账**：三者分别表达。正式就绪要求全部成立：
      ① 六维工程绑定完整；② 非合成/非旧兼容；③ 逐日名单强制（rank+admission 均给出）；
      ④ 输入非空；⑤ 无缺 mark/资金费阻断；⑥ 局部逐笔对账通过；
      ⑦ 绑定值与本轮实际源/代码/配置/费用/范围一致（不可实核项**逐项阻断**）；
      ⑧ **独立接受门禁研究放行**（wx_gate `research_pass`）。
    本轮无真实独立接受凭据 ⇒ 恒缺 ⑧ ⇒ `formal_ready=False`（并列出全部缺项）。
    """
    missing = []
    if not binding_ok:
        missing.append("run_binding_incomplete_or_legacy")
    if isinstance(binding, dict) and binding.get("_synthetic"):
        missing.append("synthetic_binding")
    if not daily_enforced:
        missing.append("daily_list_not_enforced")
    if not input_nonempty:
        missing.append("empty_input")
    if blocked:
        missing.append("blocked_missing_event_mark")
    if not recon_ok:
        missing.append("local_reconcile_failed")
    if consistency is None:
        missing.append("binding_consistency_not_evaluated")
    elif not consistency.get("ok"):
        missing.append("binding_values_inconsistent_with_actual")
        for p in (consistency.get("problems") or []):      # 第17：逐项说明
            missing.append(f"binding_inconsistent:{p}")
    if gate_error:
        missing.append(gate_error)
    if gate_doc is None:
        missing.append("no_independent_acceptance_credential")
    elif not gate_doc.get("research_pass"):
        missing.append("independent_acceptance_not_passed")
    return (len(missing) == 0), missing



def _resolve_day_map(day_map, day, default=None):
    """取**恰好命中 `day`（UTC00 日边界）**的刷新值；该日无刷新 ⇒ `default`（**不回退昨日**）。

    第16 修复（89a 独立复现缺陷1）：旧实现取「≤ day 的最近一个」⇒ 昨日有、今日缺时
    **沿用昨日**名单/排序并下单，违反 87「缺当天名单必须拒绝、不沿用陈旧排序」。
    现改为**精确命中 UTC 交易日**：`day` 不在映射中 ⇒ `default`（None）⇒ 调用方
    **拒单入账**（`daily_rank_unavailable` / `daily_admission_unavailable`）。

    第15 复用第10：仅当调用方显式传入 `rank_keys_by_day` / `admission_by_day` 时被使用；
    两参数均为 None 时 `run_arm` 行为与冻结版**逐字节等价**。
    **因果**：`day` 当日的映射只由「此前 30 个完整 UTC 日」算得，未来日/未来成交额
    不会改变当日（未来映射 key ≠ day，精确匹配取不到）——因果性不因改精确匹配而放宽。
    """
    if not day_map:
        return default
    return day_map.get(day, default)


def _record_gate_reject(led, e, ts, reason):
    """逐日门禁（准入/排序缺失或未准入）**入账拒单**，不静默丢弃闭环。

    position_id 口径与 `_try_enter` 完全一致（含 exchange/symbol），故同一事件不会双记账。
    拒单后信号记录（`research_signal`）仍完整保留（信号在更早的遍历中已写入）。
    """
    exchange, symbol = e["exchange"], e["symbol"]
    pid = hashlib.sha256(
        f"{led.run_id}|{exchange}|{symbol}|{e['event_time']}".encode()).hexdigest()[:16]
    led.add_intent(intent_id=pid, event_uid=e.get("event_uid"), exchange=exchange,
                   symbol=symbol, side="long", created_at=ts, eligible_open_time=ts,
                   reason=reason, status="rejected")
    led.add_order(intent_id=pid, status="reject", reject_reason=reason, source="market")
    return 0.0


@dataclass(frozen=True)
class Contract:
    """合约精度（缺失即拒单）。`quantity_step` 与 `min_notional` 必须显式给出。

    `price_step` 为**历史价格最小变动**（可选，0=不取整）；止损锚点按它向**更保守**
    （对多头更低）方向取整，避免用当前合约精度替代历史身份时放大风险。

    第24 加性（可选，默认 0/缺省 ⇒ legacy 逐字节等价）：`min_quantity`/`max_quantity`
    为**普通下单**数量限制；`market_*` 为**市价下单**数量限制。启用保守取整规格时按
    「普通 × 市价」有效约束**交集**（不择宽者）处理，见 `effective_order_limits`。
    """
    quantity_step: float
    min_notional: float
    price_step: float = 0.0
    min_quantity: float = 0.0
    max_quantity: float = 0.0
    market_quantity_step: float = 0.0
    market_min_quantity: float = 0.0
    market_max_quantity: float = 0.0


@dataclass
class ExecConfig:
    detail_timeframe: str = "15m"
    signal_execution_delay_minutes: int = 15
    signal_exit_delay_minutes: int = 15
    fee_each_side: float = 0.0005
    slippage_each_side: float = 0.0005
    max_holding_hours: int = 720
    stop_atr_mult: float = 3.0
    entry_participation_prev15m_quote_cap: float = 0.01

    @classmethod
    def from_resolved(cls, resolved, view):
        ex = (view or {}).get("execution") or {}
        st = (view or {}).get("stops") or {}
        cfg = resolved.get("exec") or {}
        return cls(
            signal_execution_delay_minutes=int(
                cfg.get("signal_execution_delay_minutes",
                        ex.get("signal_execution_delay_minutes", 15))),
            signal_exit_delay_minutes=int(
                cfg.get("signal_exit_delay_minutes", ex.get("signal_exit_delay_minutes", 15))),
            fee_each_side=float(cfg.get("fee_each_side", ex.get("fee_each_side", 0.0005))),
            slippage_each_side=float(cfg.get("slippage_each_side",
                                             ex.get("slippage_each_side", 0.0005))),
            max_holding_hours=int(st.get("max_holding_hours", 720)),
            stop_atr_mult=3.0,
        )


@dataclass
class RiskConfig:
    initial_equity: float = 5000.0
    risk_per_trade_fraction: float = 0.005
    leverage: float = 1.0
    max_positions: int = 10
    single_name_notional_cap: float = 0.10
    gross_long_notional_cap: float = 0.80
    cash_reserve_fraction: float = 0.20
    portfolio_risk_cap: float = 0.03
    max_drawdown_fraction: float = 0.20

    @classmethod
    def from_view(cls, view):
        r = (view or {}).get("risk") or {}
        return cls(
            initial_equity=float(r.get("initial_equity_USDT", 5000)),
            risk_per_trade_fraction=float(r.get("risk_per_trade_equity_fraction", 0.005)),
            leverage=float(r.get("leverage", 1)),
            max_positions=int(r.get("max_positions", 10)),
            single_name_notional_cap=float(r.get("single_name_notional_equity_cap", 0.10)),
            gross_long_notional_cap=float(r.get("gross_long_notional_equity_cap", 0.80)),
            cash_reserve_fraction=float(r.get("cash_or_free_margin_reserve_fraction", 0.20)),
            portfolio_risk_cap=float(r.get("portfolio_initial_stop_risk_cap", 0.03)),
            max_drawdown_fraction=float(r.get("maximum_account_drawdown_fraction", 0.20)),
        )


@dataclass
class Position:
    position_id: str
    exchange: str
    symbol: str
    entry_time: int
    entry_price: float
    quantity: float
    notional: float
    stop_price: float
    safety_exit_at: int
    reservation: float
    entry_fee: float
    entry_slippage_cost: float
    funding: float = 0.0
    funding_applied_to: int = 0
    state: str = "open"
    exit_time: int = None
    exit_price: float = None
    exit_reason: str = None
    exit_fee: float = 0.0
    gross_pnl: float = None
    net_pnl: float = None


def _step_floor(qty, step):
    if step <= 0:
        return None
    return math.floor(qty / step) * step


def _round_price_step(px, step):
    """价格最小变动取整（多头止损向更低=更保守方向）。step<=0 表示不取整。"""
    if not step or step <= 0:
        return px
    return math.floor(px / step) * step


# ── 第24：保守执行近似规格（取整 / 最小单 / 缺口 / 相邻不利费率 / 压力分列）────────────
# 用途固定「近似回测，不是精确实盘还原」。全部为**加性**纯函数；不启用（approx=None）时
# 执行路径与改前逐字节等价。
_EPS = 1e-9


def _step_ceil(qty, step):
    """数量向上取整到步进（用于**最小可下单**，不用于实际下单量）。step<=0 ⇒ None。"""
    if step <= 0:
        return None
    return step * math.ceil(qty / step - _EPS)


def round_buy_price(px, step):
    """买价：先施加原登记不利滑点后，按价格步进**向上**取整（更贵=更保守）。step<=0 ⇒ 不取整。"""
    if not step or step <= 0:
        return px
    return step * math.ceil(px / step - _EPS)


def round_sell_price(px, step):
    """卖价：先施加原登记不利滑点后，按价格步进**向下**取整（更便宜=更保守）。step<=0 ⇒ 不取整。"""
    if not step or step <= 0:
        return px
    return step * math.floor(px / step + _EPS)


def round_protect_price(px, step):
    """保护价（多头止损）：按价格步进**向下**取整（更低=更保守）。step<=0 ⇒ 不取整。"""
    if not step or step <= 0:
        return px
    return step * math.floor(px / step + _EPS)


def effective_order_limits(contract):
    """「普通下单 × 市价下单」有效约束**交集**（保守；**不择宽者**）。

    · `quantity_step` = max(普通步进, 市价步进)（步进取粗 ⇒ 可选数量更少 ⇒ 更保守）；
    · `min_quantity`  = max(普通最小量, 市价最小量)；
    · `max_quantity`  = 两侧皆给 ⇒ min；仅一侧给 ⇒ 该侧；皆缺 ⇒ 0（不设上限）；
    · `min_notional`  = 普通最小订单金额（缺失/0 ⇒ 0，表示不约束）。
    缺关键字段（步进 <=0）⇒ 返回 `None`（调用方拒单，不设默认小步进）。
    """
    step = max(float(contract.quantity_step or 0.0),
               float(getattr(contract, "market_quantity_step", 0.0) or 0.0))
    if step <= 0:
        return None
    mn = max(float(getattr(contract, "min_quantity", 0.0) or 0.0),
             float(getattr(contract, "market_min_quantity", 0.0) or 0.0))
    mx_a = float(getattr(contract, "max_quantity", 0.0) or 0.0)
    mx_b = float(getattr(contract, "market_max_quantity", 0.0) or 0.0)
    if mx_a > 0 and mx_b > 0:
        mx = min(mx_a, mx_b)
    else:
        mx = mx_a or mx_b
    return {"quantity_step": step, "min_quantity": mn, "max_quantity": mx,
            "min_notional": float(contract.min_notional or 0.0),
            "price_step": float(getattr(contract, "price_step", 0.0) or 0.0)}


def min_orderable_qty(limits, rounded_buy_price):
    """最小可下数量 = 步进 × ceil(max(最小量, 最小金额/取整后买价) / 步进)。

    禁止为了达到最小单而向上加仓（调用方只**拒单**，不补量）。字段缺/价格非正 ⇒ None。
    """
    if limits is None or limits["quantity_step"] <= 0:
        return None
    if rounded_buy_price is None or rounded_buy_price <= 0:
        return None
    need = limits["min_quantity"]
    if limits["min_notional"] > 0:
        need = max(need, limits["min_notional"] / rounded_buy_price)
    return _step_ceil(need, limits["quantity_step"])


def apply_order_rounding(*, raw_open, atr, limits, risk_per_trade_fraction, equity,
                         stop_atr_mult, slippage_each_side, gross_open_notional,
                         free_cash, leverage,
                         cash_reserve_fraction, single_name_notional_cap,
                         gross_long_notional_cap, prev_quote_volume,
                         participation_cap, portfolio_risk_open, portfolio_risk_cap):
    """统一「保守取整 + 最小单 + 风险重核」，返回取整明细或拒单原因。

    顺序（照 08）：
      ① 买价 = round_up(开盘 × (1+滑点))；② 保护价 = round_down(买价 − 3×ATR)；
      ③ 由风险/资金/参与率各上限求 q 上限 ⇒ q = 步进 × floor(q上限/步进)；
      ④ 最小可下数量 / 最大量 / 取整后上限复核 ⇒ 任一不满足整单拒绝（不凑单）。
    返回 dict 含 `reject`（None=通过）与取整明细（原值/取整值/差额/最小可下/上限快照）。
    """
    step = limits["quantity_step"]
    ps = limits["price_step"]
    entry_raw = raw_open * (1.0 + slippage_each_side)
    entry_price = round_buy_price(entry_raw, ps)
    stop_price = round_protect_price(entry_price - stop_atr_mult * float(atr), ps)
    out = {"entry_price_raw": entry_raw, "entry_price": entry_price,
           "price_raw": entry_raw, "price_step_delta": entry_price - entry_raw,
           "stop_price": stop_price, "qty": None, "qty_raw": None, "qty_step_delta": None,
           "notional": None, "min_orderable_qty": None, "limits": dict(limits),
           "reject": None}
    if entry_price <= stop_price:
        out["reject"] = "nonpositive_stop_distance"
        return out
    stop_dist = entry_price - stop_price
    # 各上限蕴含的数量上限（只用**当时因果可得**的价格/权益）
    caps = [equity * risk_per_trade_fraction / stop_dist]
    if single_name_notional_cap > 0:
        caps.append(equity * single_name_notional_cap / entry_price)
    if gross_long_notional_cap > 0:
        caps.append(max(0.0, equity * gross_long_notional_cap - gross_open_notional) / entry_price)
    if leverage and leverage > 0:
        margin_per_unit = entry_price / leverage
        caps.append(max(0.0, free_cash - equity * cash_reserve_fraction) / margin_per_unit)
    if participation_cap > 0 and prev_quote_volume is not None:
        caps.append(float(prev_quote_volume) * participation_cap / entry_price)
    q_upper = min(caps) if caps else 0.0
    qty_raw = q_upper
    qty = _step_floor(q_upper, step)
    out["qty_raw"], out["qty"] = qty_raw, qty
    if qty is None or qty <= 0:
        out["reject"] = "quantity_below_step"
        return out
    out["qty_step_delta"] = qty - qty_raw
    mn = min_orderable_qty(limits, entry_price)
    out["min_orderable_qty"] = mn
    if mn is None:
        out["reject"] = "missing_min_orderable_inputs"
        return out
    if qty < mn - _EPS:
        out["reject"] = "below_min_orderable_qty"
        return out
    if limits["max_quantity"] > 0 and qty > limits["max_quantity"] + _EPS:
        out["reject"] = "above_max_quantity"
        return out
    notional = qty * entry_price
    out["notional"] = notional
    # 取整后上限复核（价格上取整会抬高名义 ⇒ 必须重核，违反即整单拒绝）
    if limits["min_notional"] > 0 and notional < limits["min_notional"] - _EPS:
        out["reject"] = "below_min_notional"
        return out
    if single_name_notional_cap > 0 and notional > equity * single_name_notional_cap + _EPS:
        out["reject"] = "single_name_notional_cap"
        return out
    if gross_long_notional_cap > 0 and gross_open_notional + notional > equity * gross_long_notional_cap + _EPS:
        out["reject"] = "gross_long_notional_cap"
        return out
    if participation_cap > 0 and prev_quote_volume is not None and \
            notional > float(prev_quote_volume) * participation_cap + _EPS:
        out["reject"] = "entry_participation_cap"
        return out
    if leverage and leverage > 0 and \
            free_cash - notional / leverage < equity * cash_reserve_fraction - _EPS:
        out["reject"] = "cash_reserve_cap"
        return out
    if portfolio_risk_cap > 0 and portfolio_risk_open + stop_dist * qty > equity * portfolio_risk_cap + _EPS:
        out["reject"] = "portfolio_risk_cap"
        return out
    return out


def detect_input_gap(bar):
    """必要输入缺口判定：执行 bar 缺失 / 价格字段缺 / 成交额缺 ⇒ 返回缺口类别，否则 None。

    用于「缺口禁开新仓」与「已有持仓缺必需输入严格停算」。**不**用 0 补价。
    """
    if bar is None:
        return "missing_exec_bar"
    for idx, name in ((1, "open"), (2, "high"), (3, "low"), (4, "close")):
        v = bar[idx]
        if v is None:
            return f"missing_price_{name}"
    if len(bar) < 6 or bar[5] is None:
        return "missing_quote_volume"
    return None


# ── 第26：严格停算「任一整点小时真实 mark」覆盖（含**非 4h 结算小时**）──────────────
HOUR_MS_EXEC = 3600_000


def hour_floor(ts):
    """向下取整到整点小时边界（UTC 毫秒）。"""
    return (int(ts) // HOUR_MS_EXEC) * HOUR_MS_EXEC


def build_mark_hour_index(sd):
    """真实 mark 的「整点小时」索引：合并 ①事件自带 mark ②`marks` 序列。

    仅接受**真实 mark 来源**（funding 事件 3/4/5 元组第 3 项、或 `symbol_data["marks"]`
    dict/序列）。**明确禁止**回退普通 15m 交易 bar 开盘价、入场价、未来 close、0。
    用于「持仓缺任一整点小时真实 mark ⇒ 严格停算」。
    """
    idx = {}
    marks = sd.get("marks")
    if isinstance(marks, dict):
        for k, v in marks.items():
            if v is not None:
                idx[int(k)] = float(v)
    elif marks:
        for m_ts, m_px in marks:
            if m_px is not None:
                idx[int(m_ts)] = float(m_px)
    for ev in sd.get("funding", []) or []:
        if len(ev) >= 3 and ev[2] is not None:
            idx[int(ev[0])] = float(ev[2])
    return idx


def hourly_mark_gap_hours(entry_time, checked_to, upto_ts, mark_index):
    """持仓期内**每一个整点小时**的真实 mark 覆盖检查（含**非 4h 结算小时**）。

    返回 `(first_missing_hour|None, last_hour_checked)`：从 `checked_to` 之后逐小时向后推进到
    `upto_ts`；遇到第一个缺真实 mark 的整点小时即返回它（严格停算的首个缺时点），否则返回
    `(None, 已核到的小时)`。`mark_index` 由 `build_mark_hour_index` 给出（仅真实来源）。
    """
    last = int(checked_to)
    h = hour_floor(max(int(entry_time), int(checked_to))) + HOUR_MS_EXEC
    while h <= int(upto_ts):
        if mark_index.get(h) is None:
            return h, h
        last = h
        h += HOUR_MS_EXEC
    return None, last


def select_adverse_funding_rate(series, ft, side="long"):
    """缺费率时取**相邻不利**真实费率（用户指定口径）。

    做多取前后最近真实观测费率的**较大值**；两者都负时取较不负者（=较大值，少收款），
    **不自动变 0**。返回 `{ok, rate, before, after, selection, single_side, uses_future}`；
    单侧材料 ⇒ `single_side=True`（调用方标「仅单侧」）；两侧皆无 ⇒ `ok=False`（停算）。
    `series` = 升序 [(ms, rate), ...] 的真实费率序列（不限窗口，供因果/后侧查找）。
    """
    before = after = None
    for t, r in series or []:
        ti = int(t)
        if ti < int(ft):
            before = (ti, float(r))
        elif ti >= int(ft) and after is None:
            after = (ti, float(r))
    cands = [c for c in (before, after) if c is not None]
    if not cands:
        return {"ok": False, "reason": "no_adjacent_rate", "before": before, "after": after,
                "rate": None, "selection": None, "single_side": None, "uses_future": None}
    if len(cands) == 1:
        sel, basis = cands[0], "single_side"
    else:
        pick = (max if side == "long" else min)(cands, key=lambda c: c[1])
        sel, basis = pick, "adverse_max" if side == "long" else "adverse_min"
    return {"ok": True, "reason": None, "rate": sel[1], "before": before, "after": after,
            "selection": basis, "side": side, "single_side": (len(cands) == 1),
            "uses_future": (after is not None and sel == after)}


def compute_full_loss_stress(positions):
    """全损压力（**多头退出价 0** 的价差冲击 + 已知费用），**与实际成交分列**。

    这不是实际卖出，也不是「无法估计资金费」的数学上界：`stress_loss = 初始名义金额
    （= 数量 × 含滑点入场价）+ 已知费用`。无受影响单 ⇒ 返回空列表（明确 0，不伪造单）。
    """
    out = []
    for p in positions:
        entry_notional = float(p.notional or 0.0)
        known_fees = float(p.entry_fee or 0.0) + float(p.exit_fee or 0.0)
        known_funding = float(p.funding or 0.0)
        out.append({"position_id": p.position_id, "exchange": p.exchange, "symbol": p.symbol,
                    "basis": "long_exit_price_zero", "entry_notional": entry_notional,
                    "known_fees": known_fees, "known_funding": known_funding,
                    "stress_loss": entry_notional + known_fees, "is_actual_sell": 0,
                    "note": "全损压力情景（非实际卖出；不并入真实现金/权益）"})
    return out


def _normalize_funding_events(raw):
    """把 funding 事件规范为 [(ft_ms, rate_or_None, mark_or_None, available_at, kind_or_None), ...]。

    支持 2/3/4/5 元组：
      · 2 元组 `(ft, rate)`：**无** mark（须由 `marks` 序列供给，否则阻断）。
      · 3 元组 `(ft, rate, mark)`：事件自带**真实 event mark**。
      · 4 元组 `(ft, rate, mark, available_at)`：附带可得时刻（缺则 None=unknown）。
      · 5 元组 `(ft, rate, mark, avail, kind)`：第24 加性，`rate=None` + `kind="rate_missing"`
        表示该边界缺真实费率 ⇒ 由 `select_adverse_funding_rate` 取相邻不利费率替代。
    """
    out = []
    for ev in raw or []:
        ft = int(ev[0])
        rate = float(ev[1]) if len(ev) >= 2 and ev[1] is not None else None
        mark = float(ev[2]) if len(ev) >= 3 and ev[2] is not None else None
        avail = int(ev[3]) if len(ev) >= 4 and ev[3] is not None else None
        kind = ev[4] if len(ev) >= 5 and ev[4] is not None else None
        out.append((ft, rate, mark, avail, kind))
    return out


def resolve_funding_mark(sd, bar_by, exchange, symbol, ft, explicit_mark):
    """T 实际 event mark 解析（整改3：**普通交易 bar 开盘价不得替代 event mark**）。

    返回 (mark|None, source)。**只接受真实 mark 来源**：
      ① 事件自带 mark（funding 事件 3/4 元组第 3 项）→ `"event_mark"`；
      ② `symbol_data["marks"]` 的真实 event mark 序列（dict{ts:mark} 或 [(ts,mark)]）
         → `"marks_series"`。
    二者皆缺 ⇒ `(None, "missing")` ⇒ 调用方**阻断**（写 amount=NULL + blocked 记录）。

    明确**禁止**回退：普通 15m 交易 bar 开盘价（旧 `bar_open_at_T`）、入场价、本根未来
    close、0。`bar_by` 仅为保持既有调用签名兼容而保留，**不再参与** mark 解析。
    """
    if explicit_mark is not None:
        return float(explicit_mark), "event_mark"
    marks = sd.get("marks")
    if marks:
        v = None
        if isinstance(marks, dict):
            v = marks.get(int(ft))
        else:
            for m_ts, m_px in marks:
                if int(m_ts) == int(ft):
                    v = m_px
                    break
        if v is not None:
            return float(v), "marks_series"
    return None, "missing"


def run_arm(*, con, run_id, arm, config_id, resolved, view, symbol_data,
            rank_keys=None, code_sha256=None, engine="wx_local_replay",
            engine_version=None, ft_engine=None,
            data_version=None, rank_keys_by_day=None, admission_by_day=None,
            run_binding=None, require_run_binding=False, formal_gate=None,
            funding_actual=None, approx_spec=None, run_mode="version_a"):
    """回放一个研究臂（共享 5000 现金，跨所共享，各臂独立）。

    **第15/16 逐日准入（复用第10/12）**：给出 `rank_keys_by_day` / `admission_by_day`
    时，入场阶段按 **UTC00 每日刷新** 的「准入（此前30完整UTC日成交额中位 ≥ 5e6 ∧
    上市≥90d）」与「竞争排序（中位降序→(exchange,symbol) 字典序）」处理；**fail-closed**：
    **当日（精确 UTC 交易日）**缺映射 / 币未准入 / 排序依据缺失 ⇒ 写 `research_intent`+
    `research_order` 拒单（原因 `daily_rank_unavailable` / `daily_admission_unavailable` /
    `daily_not_admitted` / `daily_rank_missing_for_symbol`），**绝不回退固定起点顺序、
    绝不沿用昨日名单、绝不默认排名 0、绝不静默丢信号**。两参数均为 None 时与冻结版
    逐字节等价。**退出/风险/资金费规则不受准入影响**（今日缺名单/未准入均不删既有持仓）。

    **第15/16 run 绑定**：`run_binding` 六维（scope/daily_list_version/input_snapshot/
    code_sha256/strategy_config/funding_model）。给出但**不完整** ⇒ `ValueError` 阻断；
    `require_run_binding=True` 且缺失/不完整 / `data_version` 为空 /
    **未同时给出逐日名单与准入映射** ⇒ `ValueError` 阻断（正式新入口强制每日名单与版本检查，
    不得默认关闭检查后声称正式生效）。不给出且不要求 ⇒ legacy 兼容路径，run note 显式标注
    `legacy_no_run_binding`（不得作为正式通过入口）。

    **第16 正式就绪分解**：返回值把「工程绑定完整（`binding_ok`）」「局部逐笔对账
    （`local_reconcile_ok`）」「正式就绪（`formal_ready`）」**分别表达**。`formal_ready`
    还须：非合成 ∧ 逐日名单强制 ∧ 输入非空 ∧ 绑定值与本轮实际源/代码/配置/费用/范围一致
    ∧ **独立接受门禁（`wx_gate`）研究放行**。

    **第17 真实来源收口**：`resolved["run_binding"]`（`wx_config.resolve` 的缓存）与显式
    `formal_gate` **都只是声明**，**不构成**独立接受来源。决策**始终**实调 `wx_gate.binding()`
    取当前接受状态；声明只可与真实来源 `sha256`+`source` **核对**（不符 ⇒ 记
    `*_declaration_mismatch`，但**保留**真实 `research_pass`）⇒ 伪路径/全零 SHA/自填布尔
    **不能**放行。无真实接受凭据 ⇒ `formal_ready=False` 且 `formal_missing` 逐项列出。
    合成/旧兼容/空输入**永不**冒正式就绪。

    **第18 模型实值核验与门禁字段衔接**：① `funding_actual`（可选，**本次实际**模型材料）
    = `{"model": "norm4h"|"ext1h", "events": [(t_ms, rate), ...], "marks_consumed": {...}}`；
    给出时对「模型名 + 规则定义文本 + 事件时间/费率 + 消费 mark」用 `funding_model_identity`
    重算并与声明 `funding_model` **逐字节**比对 ⇒ 全零/任意合法 hex/换模型名/费率/时点/mark/
    定义版本任一不一致都失败；**不给**（无实际可核材料）⇒ `funding_model_not_verified` 阻断
    （不再仅凭格式/前缀通过）。② 六维一致性改为按 `wx_gate.binding()` **真实返回结构**
    （`run_identity`/`real_bindings_detail`/`run_binding_detail`）提取，**不读**不存在的
    `bindings`/`data_version`；`input_snapshot` 的已核来源 = 门禁 `data` 维；
    `daily_list_version`/`data_version` **第20 起**由 `consumer_dim_sources` 契约解析（可核则核；
    缺材料/错范围/run 不符 ⇒ 记具体原因并阻断，**不再**恒「接口缺项」）。

    **第19 执行材料实核与真门禁载荷接通**：① 资金费维**不再**以调用方 `funding_actual` 字典为
    实际证据：`run_arm` 由本次**真实执行输入**（`symbol_data` 规范化的逐 (所,币) 资金费序列 +
    `resolve_funding_mark` 真正消费 mark + 隔离账本**实际入账**行）派生 `funding_materials`
    （`FUNDING_MATERIALS_SCHEMA`），再对**实际材料**用 `funding_materials_identity`（逐源复用
    第15 算法后带版本聚合）重算并与声明 `funding_model` **逐字节**比对；费率/时点/mark/**币/所**/
    模型定义/当前 run 任一不同 ⇒ 分量失败并列原因；调用方 `funding_actual` 仅作**声明**与实际材料
    交叉核对（`funding_declaration_not_actual`）。② 门禁载荷改读真实返回恒带的 `verified_payload`。
    实际材料/账本行随返回值暴露（`funding_materials` / `funding_ledger_rows`）供核验与追溯。

    **第21 实际消费上下文与子运行闭环**：① `binding_actual_consistency` **贯通**本次实际材料/
    账本、**实际使用**逐日映射内容（`rank_keys_by_day`/`admission_by_day` → `daily_list_doc`）与
    **当前执行子运行**记录到真实生产来源接口（第20 只传 `real_gate` ⇒ 新来源接口仍错报缺费用材料）；
    ② 子运行身份 `child_run` **只**来自真实账本 `research_run` 行（`record_source="ledger:research_run"`，
    `record_sha256` 防篡改）；门禁侧把**门禁批次 run** 与**子臂 run** 分列，`run_match` 仅表示
    子运行**实际消费**维度 == 真实解析来源（无子运行 ⇒ `None`，无关/不符 ⇒ `False`）；
    ③ 三维各自**分列** `source_resolved`（源解析）/ `run_matched`（当前运行匹配）/
    `independently_verified`（= 两者皆真）⇒ **版本值相等不得冒当前运行已独立核验**。

    `symbol_data`: {(exchange, symbol): {
        "events": [{"event_time","direction","atr","execution_label","source_masks",
                    "event_uid","detection_low"}],
        "detail": [(open_time, open, high, low, close, quote_volume), ...],   # 15m，升序
        "funding": [(funding_time, rate)]                      # 无 mark（须由 marks 供给）
                 | [(funding_time, rate, event_mark)]          # 事件自带真实 mark
                 | [(funding_time, rate, event_mark, available_at)],  # +可得时刻
                 # 第24 加性：5 元组 (ft, None, mark, avail, "rate_missing") 表示该边界缺真实
                 # 费率 ⇒ 由 funding_rate_series 取相邻不利费率替代（仅 approx 启用）。
        "funding_rate_series": [(ms, rate), ...]  # 第24：升序真实费率序列（相邻不利替代用）
        "funding_available": bool,
        "marks": {ts_ms: mark} 或 [(ts_ms, mark), ...]（真实 event mark 序列；缺则阻断）,
        "contract_unit": str（可选：合约数量单位/乘数口径，用于 funding 可追溯）,
        "generation": str（可选：历史合约代际标识，用于 funding 可追溯）,
        "donchian_exits": [exit_open_time_1h, ...],
        "contract": Contract(...),
    }}

    `ft_engine`（可选）：真实 Freqtrade Order/Trade 持久化适配器（见 `wx_engine`）。
    给出时每笔成交会**同时**写入隔离的 Freqtrade `trades`/`orders` 表；不给则仅本地账本。
    """
    # ── 第16：逐日名单是否被强制（rank+admission 均显式给出）──────────────────────
    daily_enforced = (rank_keys_by_day is not None and admission_by_day is not None)

    # ── 第15：run 绑定 fail-closed（缺绑定 ⇒ 阻断新正式用途，不默填「未指定」）──
    binding_ok, binding_missing = validate_run_binding(run_binding)
    if run_binding is not None and not binding_ok:
        raise ValueError(
            "run_binding 不完整，拒绝新正式用途（缺 "
            f"{binding_missing}）；六维=" + "/".join(REQUIRED_RUN_BINDING))
    if require_run_binding and not binding_ok:
        raise ValueError(
            "require_run_binding=True 但 run_binding 缺失/不完整（缺 "
            f"{binding_missing}）：新正式用途必须显式绑定 "
            "币安526范围/逐日名单版本/输入快照/源码/策略18配置/资金费模型身份")
    if require_run_binding and not data_version:
        raise ValueError(
            "require_run_binding=True 时 data_version 不得为空（拒绝默填『未指定』）")
    # 第16：正式新入口**强制逐日名单**（不得默认关闭检查后声称正式生效）。
    if require_run_binding and not daily_enforced:
        raise ValueError(
            "require_run_binding=True 时必须同时提供 rank_keys_by_day 与 admission_by_day："
            "正式新入口强制逐日名单与版本检查（缺当日映射须拒单，不得沿用昨日/关闭检查）")

    risk = RiskConfig.from_view(view)
    ex = ExecConfig.from_resolved(resolved, view)
    led = L.Ledger(con, run_id)
    # ── 第24：保守执行近似规格开关（None ⇒ legacy 逐字节等价路径）──────────────────────
    # `approx_spec` 为**隔离工程控制载荷**（非正式接受来源）：给出即启用「买价上取整/卖价
    # 下取整/最小可下单/缺口禁开与停算/相邻不利费率替代/全损压力分列」；spec_sha 用于追溯。
    approx = None
    if approx_spec is not None:
        approx = {"enabled": True,
                  "spec": approx_spec,
                  "spec_sha": hashlib.sha256(json.dumps(
                      approx_spec, sort_keys=True, ensure_ascii=False,
                      default=str).encode("utf-8")).hexdigest()}
    dv = data_version or resolved.get("data_version")
    if binding_ok:
        bind_note = f"；run_binding_sha256={binding_sha256(run_binding)}"
    else:
        bind_note = ("；legacy_no_run_binding（未提供 run 绑定：不得作正式通过入口）")
    if not dv:
        bind_note += "；data_version=未指定（legacy 兼容）"
    led.open_run(arm=arm, config_id=config_id,
                 strategy_version=resolved.get("version"), config_hash=resolved["config_hash"],
                 data_version=dv or "dv:unspecified",
                 engine=engine, engine_version=engine_version,
                 adapter_version="wx_execution/4", code_sha256=code_sha256,
                 initial_equity=risk.initial_equity,
                 note="隔离研究回放；共享5000跨所；各臂独立" + bind_note)

    cash = risk.initial_equity
    positions = {}
    peak_equity = risk.initial_equity
    halted = None
    dd_breached = False
    blocked = []
    entries = []
    # 第24：缺口阻断状态（仅 approx 启用时填充；legacy 恒空 ⇒ 逐字节等价）
    gap_blocks = []
    gap_pos_ids = set()
    gap_stopped = False
    # 第26：严格停算（首个缺时点起共享账户冻结；受影响持仓标「无法计算」）
    frozen_at = None
    uncomputable = []
    mark_checked = {}
    # 第40：A/B 双轨（A 严格截断原样；B 存量结清 run-off，**必须新版本明确启用**）
    if run_mode not in ("version_a", "version_b"):
        raise ValueError("未知 run_mode=%r（仅 version_a / version_b）" % (run_mode,))
    wind_down = False
    wind_down_ids = set()
    first_anomaly_ts = None

    def _first_ts_at_or_after(grid, t):
        for g in grid:
            if g >= t:
                return g
        return None

    grids, exit_ts_by_symbol = {}, {}
    exit_delay_ms = ex.signal_exit_delay_minutes * 60 * 1000
    for key, sd in symbol_data.items():
        grids[key] = sorted(int(b[0]) for b in sd.get("detail", []))
        ts_set = set()
        for e in sd.get("donchian_exits") or []:
            t = _first_ts_at_or_after(grids[key], int(e) + HOUR_MS + exit_delay_ms)
            if t is not None:
                ts_set.add(t)
        exit_ts_by_symbol[key] = ts_set
    # 第26：每币「整点小时真实 mark」索引（仅真实来源；缺口 ⇒ 持仓严格停算）
    mark_hour_by_symbol = {k: build_mark_hour_index(sd) for k, sd in symbol_data.items()}

    for (exchange, symbol), sd in symbol_data.items():
        grid = grids[(exchange, symbol)]
        for ev in sd.get("events", []):
            led.add_signal({"exchange": exchange, "symbol": symbol,
                            "event_time": ev["event_time"],
                            "confirmed_at": ev.get("confirmed_at", ev["event_time"]),
                            "direction": ev.get("direction", "long"),
                            "data_version": ev.get("data_version"),
                            "evidence": {"event_uid": ev.get("event_uid"),
                                         "atr_signal_bar": ev.get("atr"),
                                         "execution_label": ev.get("execution_label"),
                                         "source_masks": ev.get("source_masks")}})
            elig = _first_ts_at_or_after(
                grid, int(ev["event_time"]) + ex.signal_execution_delay_minutes * 60 * 1000)
            if elig is None:
                continue
            entries.append({"exchange": exchange, "symbol": symbol, "eligible": elig, **ev})

    all_ts = sorted({int(b[0]) for sd in symbol_data.values() for b in sd.get("detail", [])})
    if approx is not None:
        # 第24：把每币**期望的完整 15m 网格**并入时间轴 ⇒ 中段缺 bar 的 ts 也会被访问，
        # 从而能被缺口检测捕获（legacy 不并入 ⇒ 时间轴与改前逐字节一致）。
        exp = set()
        for _key, _g in grids.items():
            if _g:
                exp.update(range(_g[0], _g[-1] + DETAIL_MS, DETAIL_MS))
        all_ts = sorted(set(all_ts) | exp)
    bar_by = {}
    for (exchange, symbol), sd in symbol_data.items():
        for b in sd.get("detail", []):
            bar_by[(exchange, symbol, int(b[0]))] = b

    def committed_now():
        return sum(p.notional / risk.leverage for p in positions.values() if p.state == "open")

    def unreal_mark(ts):
        """按当根 15m **收盘** mark-to-market（用于权益行/DD；不含未来信息，仅记录本根结果）。"""
        u = 0.0
        for p in positions.values():
            if p.state != "open":
                continue
            b = bar_by.get((p.exchange, p.symbol, ts))
            if b is not None:
                u += (float(b[4]) - p.entry_price) * p.quantity
        return u

    def unreal_causal(ts):
        """**T 开盘风险**专用：只用当时因果可得的最后一根已闭合 bar 收盘（E03）。

        取 `ts - 15m` 的收盘（上一根已闭合 bar）；无则回退到**本根开盘**（T 时刻价格），
        **绝不**使用本根未来 close。
        """
        u = 0.0
        for p in positions.values():
            if p.state != "open":
                continue
            b = bar_by.get((p.exchange, p.symbol, ts - DETAIL_MS))
            if b is not None:
                px = float(b[4])
            else:
                b2 = bar_by.get((p.exchange, p.symbol, ts))
                px = float(b2[1]) if b2 is not None else None
            if px is not None:
                u += (px - p.entry_price) * p.quantity
        return u

    def portfolio_risk_now():
        return sum(max(0.0, (p.entry_price - p.stop_price) * p.quantity)
                   for p in positions.values() if p.state == "open")

    def close_pos(p, ts, px, reason):
        nonlocal cash
        exit_price = px * (1.0 - ex.slippage_each_side)
        if approx is not None:
            # 第24：卖价先滑点后按价格步进**向下**取整（更便宜=更保守）
            c = symbol_data[(p.exchange, p.symbol)].get("contract")
            ps = float(getattr(c, "price_step", 0.0) or 0.0) if c is not None else 0.0
            exit_price = round_sell_price(exit_price, ps)
        exit_notional = exit_price * p.quantity
        exit_fee = exit_notional * ex.fee_each_side
        slip_cost = p.quantity * (px - exit_price)
        gross = (exit_price - p.entry_price) * p.quantity
        p.state = "closed"
        p.exit_time, p.exit_price, p.exit_reason, p.exit_fee = ts, exit_price, reason, exit_fee
        p.gross_pnl = gross
        p.net_pnl = gross - p.entry_fee - exit_fee + p.funding
        led.add_order(intent_id=p.position_id, status="fill", fill_time=ts,
                      fill_price=exit_price, quantity=p.quantity, fee=exit_fee,
                      slippage=slip_cost, notional=exit_notional, source="market")
        led.add_position(_pos_row(p, "closed"))
        led.release(position_id=p.position_id, released_at=ts)
        cash += gross - exit_fee          # 资金费已在持有期入账
        if ft_engine is not None:
            ft_engine.close_position(p, ts, exit_price, exit_fee, slip_cost, reason)

    def _freeze(p, gts, kind):
        """第26 严格停算：把受影响持仓标「无法计算」并**冻结共享账户**（首个缺时点）。

        - 不写真实平仓成交 / 费用 / 盈亏（不是写 NULL 费用行冒有效）；**不**补 0。
        - 记 `research_uncomputable`（不可算 status）+ `research_gap_block` + `blocked`。
        - 冻结后主循环 break：不再记权益、不再开新仓、不做 terminal/DD 虚拟卖出。
        """
        nonlocal gap_stopped, frozen_at
        gap_stopped = True
        if frozen_at is None or int(gts) < frozen_at:
            frozen_at = int(gts)
        gap_pos_ids.add(p.position_id)
        gap_blocks.append({"position_id": p.position_id, "exchange": p.exchange,
                           "symbol": p.symbol, "ts": int(gts), "kind": kind})
        blocked.append({"position_id": p.position_id, "symbol": p.symbol,
                        "funding_time": None, "rate": None,
                        "reason": "input_gap_stop", "kind": kind, "ts": int(gts)})
        led.add_gap_block(exchange=p.exchange, symbol=p.symbol, ts=int(gts), kind=kind,
                          detail=json.dumps({"position_id": p.position_id,
                                             "entry_time": p.entry_time,
                                             "status": "uncomputable"}, ensure_ascii=False))
        # 持仓标「无法计算」：清空未来真实平仓/费用/盈亏，仅保留此前已证事实
        p.state = "uncomputable"
        p.exit_time = int(gts)
        p.exit_reason = f"uncomputable_{kind}"
        p.exit_price = None
        p.exit_fee = 0.0
        p.gross_pnl = None
        p.net_pnl = None
        led.add_position(_pos_row(p, "uncomputable"))
        led.add_uncomputable(position_id=p.position_id, exchange=p.exchange, symbol=p.symbol,
                             entry_time=p.entry_time, gap_ts=int(gts), kind=kind,
                             status="uncomputable",
                             detail=json.dumps({"note": "严格停算：首个缺时点起无有效后续账本",
                                                "entry_price": p.entry_price,
                                                "quantity": p.quantity},
                                               ensure_ascii=False))
        uncomputable.append({"position_id": p.position_id, "exchange": p.exchange,
                             "symbol": p.symbol, "entry_time": p.entry_time,
                             "gap_ts": int(gts), "kind": kind, "status": "uncomputable"})
        # 共享账户冻结 ⇒ 其余仍开仓的持仓同样**无有效后续账本**（标「无法计算」，不写假平仓）
        for q in positions.values():
            if q.state != "open" or q.position_id in gap_pos_ids:
                continue
            q.state = "uncomputable"
            q.exit_time = int(gts)
            q.exit_reason = "uncomputable_shared_account_frozen"
            q.exit_price = None
            q.exit_fee = 0.0
            q.gross_pnl = None
            q.net_pnl = None
            led.add_position(_pos_row(q, "uncomputable"))
            led.add_uncomputable(
                position_id=q.position_id, exchange=q.exchange, symbol=q.symbol,
                entry_time=q.entry_time, gap_ts=int(gts), kind="shared_account_frozen",
                status="uncomputable",
                detail=json.dumps({"trigger_position_id": p.position_id,
                                   "note": "共享5000账户冻结：无有效后续账本"},
                                  ensure_ascii=False))
            uncomputable.append({"position_id": q.position_id, "exchange": q.exchange,
                                 "symbol": q.symbol, "entry_time": q.entry_time,
                                 "gap_ts": int(gts), "kind": "shared_account_frozen",
                                 "status": "uncomputable"})

    def _wind_down(p, gts, kind):
        """第40 版本 B：**存量结清 run-off**（不冻结共享账户、不 break）。

        - 仅把**受影响持仓**标「无法计算」：NULL 退出价/盈亏，**不写假平仓、不释放其未知资金**；
        - **全账户永久禁开新仓**（总权益未知 ⇒ 无法计算 10% 仓位/80% 敞口上限）；
        - 其余**资料完整**存量持仓**继续**自身真实费用/止损/信号/时间退出；
        - 账户总收益/权益/可用资金/回撤自首异常 T 起 UNKNOWN；**停用回撤锁止**。
        """
        nonlocal wind_down, first_anomaly_ts, gap_stopped
        wind_down = True
        if first_anomaly_ts is None or int(gts) < first_anomaly_ts:
            first_anomaly_ts = int(gts)
        wind_down_ids.add(p.position_id)
        gap_pos_ids.add(p.position_id)
        gap_blocks.append({"position_id": p.position_id, "exchange": p.exchange,
                           "symbol": p.symbol, "ts": int(gts), "kind": kind,
                           "mode": "version_b_wind_down"})
        blocked.append({"position_id": p.position_id, "symbol": p.symbol,
                        "funding_time": None, "rate": None,
                        "reason": "wind_down_uncomputable", "kind": kind, "ts": int(gts)})
        led.add_gap_block(exchange=p.exchange, symbol=p.symbol, ts=int(gts), kind=kind,
                          detail=json.dumps({"position_id": p.position_id,
                                             "entry_time": p.entry_time,
                                             "mode": "version_b_wind_down",
                                             "status": "uncomputable"}, ensure_ascii=False))
        p.state = "uncomputable"
        p.exit_time = int(gts)
        p.exit_reason = f"uncomputable_{kind}"
        p.exit_price = None
        p.exit_fee = 0.0
        p.gross_pnl = None
        p.net_pnl = None
        led.add_position(_pos_row(p, "uncomputable"))
        led.add_uncomputable(position_id=p.position_id, exchange=p.exchange, symbol=p.symbol,
                             entry_time=p.entry_time, gap_ts=int(gts), kind=kind,
                             status="uncomputable",
                             detail=json.dumps({"note": "版本B存量结清：受影响单停算未决，不假平；"
                                                        "全账户禁新仓、账户指标UNKNOWN",
                                                "entry_price": p.entry_price,
                                                "quantity": p.quantity},
                                               ensure_ascii=False))
        uncomputable.append({"position_id": p.position_id, "exchange": p.exchange,
                             "symbol": p.symbol, "entry_time": p.entry_time,
                             "gap_ts": int(gts), "kind": kind, "status": "uncomputable",
                             "mode": "version_b_wind_down"})

    for ts in all_ts:
        # ⓪ 第26：缺口检测 —— (c)执行价格/成交额缺、(a)任一整点小时真实 mark 缺（含非 4h
        #    结算小时）、(b)待结算资金费缺真实 mark/缺率且前后皆无 ⇒ 统一严格停算。
        #    首个缺时点起不再记录该持仓真实平仓/费用/盈亏；共享账户冻结并停止一切后续真实记录。
        if approx is not None:
            # 第41：同刻**全量**检查所有开仓持仓缺口（不再「遇首币 break」），
            #       避免第二受影响持仓在**同一时点**继续被止损/平仓（违反「受影响之后不记平仓」）。
            hits = []
            for p in list(positions.values()):
                if p.state != "open" or p.position_id in gap_pos_ids:
                    continue
                if ts <= p.entry_time:
                    continue
                sym_grid = grids.get((p.exchange, p.symbol)) or []
                if not sym_grid or ts >= sym_grid[-1]:
                    continue    # 数据末端（无更晚 bar）不计中段缺口
                # (c) 执行价格/成交额缺口
                g = detect_input_gap(bar_by.get((p.exchange, p.symbol, ts)))
                if g:
                    hits.append((p, ts, g))
                    continue
                # (a) 任一整点小时真实 mark 缺口（**特测非 4h 结算小时**）
                mi = mark_hour_by_symbol.get((p.exchange, p.symbol)) or {}
                miss_h, last_ok = hourly_mark_gap_hours(
                    p.entry_time, mark_checked.get(p.position_id, p.entry_time),
                    min(int(ts), int(sym_grid[-1])), mi)
                if miss_h is not None:
                    hits.append((p, miss_h, "missing_hour_mark"))
                    continue
                mark_checked[p.position_id] = last_ok
            # (b) 待结算资金费：缺真实 mark 或 缺率且相邻前后皆无 ⇒ 严格停算（先于入账）
            #     **全量**扫描（同一持仓已命中 (a)/(c) 则不重复计入）。
            for p in list(positions.values()):
                if p.state != "open" or p.position_id in gap_pos_ids:
                    continue
                if any(h[0].position_id == p.position_id for h in hits):
                    continue
                sd_p = symbol_data[(p.exchange, p.symbol)]
                for ft, rate, emark, _avail, fkind in _normalize_funding_events(
                        sd_p.get("funding", [])):
                    if not (p.entry_time < int(ft) <= ts
                            and int(ft) > p.funding_applied_to):
                        continue
                    mark, _msrc = resolve_funding_mark(
                        sd_p, bar_by, p.exchange, p.symbol, ft, emark)
                    if mark is None:
                        hits.append((p, int(ft), "missing_event_mark"))
                        break
                    if rate is None or fkind == "rate_missing":
                        sel = select_adverse_funding_rate(
                            sd_p.get("funding_rate_series"), ft, side="long")
                        if not sel["ok"]:
                            hits.append((p, int(ft), "missing_rate_no_adjacent"))
                            break
            if hits:
                # 第40/41：版本B 对**标价/执行缺口**走存量结清（不冻结共享账户、不 break；
                #       本 ts 继续处理其余持仓的既有流程）；真实费率缺失（无相邻不利）属
                #       数据阻断 ⇒ A/B 均严格停算该场景。
                #       同刻**全部**受影响持仓统一处理：版本B 逐笔 `_wind_down`（不冻结、不 break），
                #       版本A/含费率阻断 ⇒ `_freeze`（共享账户冻结，立即截断）并 break。
                rate_block = any(h[2] == "missing_rate_no_adjacent" for h in hits)
                if run_mode == "version_b" and not rate_block:
                    for p_h, gts_h, kind_h in hits:
                        _wind_down(p_h, gts_h, kind_h)
                else:
                    _freeze(hits[0][0], hits[0][1], hits[0][2])
                    break    # 共享账户冻结：不再记权益、不开新仓、不做 terminal/DD 虚拟卖出

        # ① 资金费（既有仓）：真实结算事件 + T 实际 event mark；缺 mark ⇒ 阻断（不补 0/不回退）
        for p in list(positions.values()):
            if p.state != "open":
                continue
            sd_p = symbol_data[(p.exchange, p.symbol)]
            unit = sd_p.get("contract_unit")
            gen = sd_p.get("generation")
            for ft, rate, emark, avail, kind in _normalize_funding_events(sd_p.get("funding", [])):
                if p.entry_time < int(ft) <= ts and int(ft) > p.funding_applied_to:
                    mark, msrc = resolve_funding_mark(
                        sd_p, bar_by, p.exchange, p.symbol, ft, emark)
                    # ── 第24：缺真实费率 ⇒ 相邻不利费率替代（仅 approx 启用；legacy 恒有 rate）──
                    rate_used, rsub, rsrc, rside, ralt, ralt_t, rsel = (
                        rate, None, None, None, None, None, None)
                    if (rate is None or kind == "rate_missing"):
                        if approx is None:
                            blocked.append({"position_id": p.position_id, "symbol": p.symbol,
                                            "funding_time": int(ft), "rate": None,
                                            "reason": "missing_funding_rate"})
                            led.add_funding(position_id=p.position_id, funding_time=ft,
                                            rate=None, amount=None,
                                            source="blocked:missing_funding_rate",
                                            mark=mark, mark_source=msrc, available_at=avail,
                                            contract_unit=unit, generation=gen, qty=p.quantity)
                            p.funding_applied_to = int(ft)
                            continue
                        sel = select_adverse_funding_rate(
                            sd_p.get("funding_rate_series"), ft, side="long")
                        if not sel["ok"]:
                            blocked.append({"position_id": p.position_id, "symbol": p.symbol,
                                            "funding_time": int(ft), "rate": None,
                                            "reason": "missing_rate_no_adjacent"})
                            led.add_funding(position_id=p.position_id, funding_time=ft,
                                            rate=None, amount=None,
                                            source="blocked:missing_rate_no_adjacent",
                                            mark=mark, mark_source=msrc, available_at=avail,
                                            contract_unit=unit, generation=gen,
                                            rate_source="adjacent_adverse_estimate",
                                            rate_selection="no_adjacent_rate",
                                            rate_substituted=0, qty=p.quantity)
                            p.funding_applied_to = int(ft)
                            continue
                        rate_used = sel["rate"]
                        rsub = 1
                        rsrc = "adjacent_adverse_estimate"
                        rside = ("single_side" if sel["single_side"]
                                 else ("after" if sel["uses_future"] else "before"))
                        other = sel["after"] if sel["uses_future"] else sel["before"]
                        if other is not None:
                            ralt, ralt_t = other[1], other[0]
                        rsel = (f"{sel['selection']}(long);"
                                f"before={sel['before']};after={sel['after']};"
                                f"single_side={sel['single_side']};"
                                f"uses_future={sel['uses_future']}"
                                + (";事后成本假设（用后侧费率）" if sel["uses_future"] else ""))
                    if mark is None:
                        blocked.append({"position_id": p.position_id, "symbol": p.symbol,
                                        "funding_time": int(ft), "rate": rate_used,
                                        "reason": "missing_event_mark"})
                        led.add_funding(position_id=p.position_id, funding_time=ft,
                                        rate=rate_used, amount=None,
                                        source="blocked:missing_event_mark",
                                        mark=None, mark_source="missing",
                                        available_at=avail, contract_unit=unit,
                                        generation=gen, rate_source=rsrc, rate_side=rside,
                                        rate_alt=ralt, rate_alt_time=ralt_t,
                                        rate_selection=rsel, rate_substituted=rsub, qty=p.quantity)
                        p.funding_applied_to = int(ft)
                        continue
                    amount = -p.quantity * mark * float(rate_used)
                    p.funding += amount
                    cash += amount
                    led.add_funding(position_id=p.position_id, funding_time=ft,
                                    rate=float(rate_used), amount=amount,
                                    source=(f"{p.exchange}:funding_history:{msrc}"
                                            if rsub is None else
                                            f"{p.exchange}:funding_adjacent_adverse:{msrc}"),
                                    mark=mark, mark_source=msrc, available_at=avail,
                                    contract_unit=unit, generation=gen,
                                    rate_source=rsrc, rate_side=rside, rate_alt=ralt,
                                    rate_alt_time=ralt_t, rate_selection=rsel,
                                    rate_substituted=rsub, qty=p.quantity)
                    p.funding_applied_to = int(ft)

        # ② 开盘缺口保护：开盘跳空穿过止损 ⇒ 更差开盘价成交
        for p in list(positions.values()):
            if p.state != "open" or p.position_id in gap_pos_ids:
                continue
            b = bar_by.get((p.exchange, p.symbol, ts))
            if b is not None and float(b[1]) <= p.stop_price:
                close_pos(p, ts, float(b[1]), "gap_through_stop")

        # ③ DD 锁止：**下一 15m 开盘**清仓（E05；不是触发当根 close）
        if dd_breached:
            for p in list(positions.values()):
                if p.state != "open" or p.position_id in gap_pos_ids:
                    continue
                b = bar_by.get((p.exchange, p.symbol, ts))
                close_pos(p, ts, float(b[1]) if b else p.entry_price, "drawdown_lock")

        # ④ 开盘到期的信号/安全退出（先于盘中 high/low —— E04）
        for p in list(positions.values()):
            if p.state != "open" or p.position_id in gap_pos_ids:
                continue
            b = bar_by.get((p.exchange, p.symbol, ts))
            if b is None:
                continue
            op = float(b[1])
            if p.safety_exit_at is not None and ts >= p.safety_exit_at:
                close_pos(p, ts, op, "safety_720h")
                continue
            de = exit_ts_by_symbol.get((p.exchange, p.symbol)) or set()
            if ts in de:
                close_pos(p, ts, op, "donchian_exit")

        # ⑤ 排名入场（halted 时全部拒绝；第40 版本B 存量结清 ⇒ 全账户永久禁新仓）
        due = [e for e in entries if e["eligible"] == ts]
        if due and wind_down:
            for e in due:
                _record_gate_reject(led, e, ts, "wind_down_no_new_entries")
            due = []
        if due:
            rk = rank_keys or {}
            # 第15/16 加性（复用第10/12）：UTC00 每日刷新「准入(500万)」与「竞争排序
            # (prior30 成交额中位)」。两个 day 参数都为 None 时本段与冻结版逐字节等价。
            # **fail-closed**：当日（精确 UTC 交易日）缺准入/排序 ⇒ 拒单入账，**绝不回退固定
            # 起点顺序、绝不沿用昨日名单、绝不默认排名 0、绝不静默丢信号**；未准入/缺排名依据亦明确拒绝。
            # 只用**当日已可知**资料：`_resolve_day_map` **精确命中** day（未来映射不参与、昨日不回退）。
            if rank_keys_by_day is not None or admission_by_day is not None:
                day = (ts // DAY_MS) * DAY_MS
                rk_day = _resolve_day_map(rank_keys_by_day, day, None)
                adm_day = _resolve_day_map(admission_by_day, day, None)
                if rk_day is None:
                    for e in due:
                        _record_gate_reject(led, e, ts, "daily_rank_unavailable")
                    due = []
                elif adm_day is None:
                    for e in due:
                        _record_gate_reject(led, e, ts, "daily_admission_unavailable")
                    due = []
                else:
                    rk = rk_day
                    keep = []
                    for e in due:
                        key = (e["exchange"], e["symbol"])
                        if not adm_day.get(key, False):
                            _record_gate_reject(led, e, ts, "daily_not_admitted")
                        elif key not in rk:
                            _record_gate_reject(led, e, ts, "daily_rank_missing_for_symbol")
                        else:
                            keep.append(e)
                    due = keep
            due.sort(key=lambda e: (-rk.get((e["exchange"], e["symbol"]), 0.0),
                                    e["exchange"], e["symbol"]))
            for e in due:
                cash += _try_enter(led, positions, e, ts, risk, ex, symbol_data, bar_by,
                                   cash, committed_now(), unreal_causal(ts), portfolio_risk_now(),
                                   halted, ft_engine, approx=approx)

        # ⑥ 盘中 high/low：固定止损（保守先）；本 bar 刚入场者亦保守先
        for p in list(positions.values()):
            if p.state != "open" or p.position_id in gap_pos_ids:
                continue
            b = bar_by.get((p.exchange, p.symbol, ts))
            if b is None:
                continue
            lo, op = float(b[3]), float(b[1])
            if lo <= p.stop_price:
                reason = ("intrabar_stop_after_entry" if p.entry_time == ts
                          else "fixed_atr_stop")
                close_pos(p, ts, op if op < p.stop_price else p.stop_price, reason)

        # ⑦ 权益 + DD20% 锁止检测
        if wind_down:
            # 第40 版本B：账户总权益/现金/回撤自首异常 T 起 UNKNOWN（显式写 NULL，不静默缺失）
            #             且**停用回撤锁止**（不以未知回撤作退出判据）。
            led.add_equity(ts=ts, cash=None, committed_margin=None, unrealized=None,
                           equity=None,
                           positions=sum(1 for p in positions.values() if p.state == "open"))
        else:
            equity = cash + unreal_mark(ts)
            led.add_equity(ts=ts, cash=cash, committed_margin=committed_now(),
                           unrealized=unreal_mark(ts), equity=equity,
                           positions=sum(1 for p in positions.values() if p.state == "open"))
            peak_equity = max(peak_equity, equity)
            if (peak_equity > 0 and not dd_breached
                    and (peak_equity - equity) / peak_equity >= risk.max_drawdown_fraction):
                dd_breached = True
                halted = L.iso(ts)

    # 期末（E02）：按**已冻结的可执行终止开盘**清算；费用/滑点/资金费入账后才写最终权益
    # 第26：**严格停算时取消期末 terminal 虚拟实际卖出**（受影响持仓已标「无法计算」，
    #       共享账户冻结 ⇒ 不再输出有效后续权益绩效路径）。
    # 第40 版本B：存量结清 ⇒ **不末尾假价卖出**（可结清仓自然结束或数据到尾即止）。
    terminal = None
    last_ts = all_ts[-1] if all_ts else None
    if not gap_stopped and not wind_down:
        for p in list(positions.values()):
            if p.state != "open":
                continue
            bars = symbol_data[(p.exchange, p.symbol)].get("detail", [])
            t = max((int(b[0]) for b in bars), default=p.entry_time)
            b = bar_by.get((p.exchange, p.symbol, t))
            px = float(b[1]) if b is not None else p.entry_price     # 最后一根的可执行**开盘**
            close_pos(p, t, px, "terminal_liquidation")
            terminal = {"time": t, "price_rule": "last_executable_open"}
        if last_ts is not None:
            led.add_equity(ts=last_ts, cash=cash, committed_margin=committed_now(),
                           unrealized=unreal_mark(last_ts) if positions else 0.0, equity=cash,
                           positions=sum(1 for p in positions.values() if p.state == "open"))

    review = _review(con, run_id, risk, halted, cash)
    # 第24：缺口停算 ⇒ 受影响共享账户路径不得输出**有效全段结果**（分列标记）
    # 第26：严格停算 ⇒ 共享账户冻结，`uncomputable` 持仓无有效后续账本
    review["metrics"]["gap_stopped"] = gap_stopped
    review["metrics"]["valid_full_segment"] = (not gap_stopped and not uncomputable)
    review["metrics"]["gap_affected_positions"] = sorted(gap_pos_ids)
    review["metrics"]["uncomputable_positions"] = [u["position_id"] for u in uncomputable]
    review["metrics"]["frozen_at"] = frozen_at
    # 第40：A/B 版本标签（旧运行不换标签；A 与改前逐字节等价，仅新增键）
    review["metrics"]["run_mode"] = ("version_b_wind_down" if wind_down else "version_a_strict")
    review["metrics"]["wind_down"] = wind_down
    review["metrics"]["first_anomaly_ts"] = first_anomaly_ts
    if wind_down:
        # 版本B：账户总收益/权益/可用资金/回撤自首异常 T 起 UNKNOWN（不借旧权益、不重新入金）
        review["metrics"]["account_metrics_valid"] = False
        review["metrics"]["account_metrics"] = "UNKNOWN"
        review["metrics"]["equity"] = None
        review["metrics"]["final_equity"] = None
        review["metrics"]["final_cash"] = None
        review["metrics"]["equity_row_final"] = None
        review["metrics"]["net_profit"] = None
        review["metrics"]["max_drawdown_fraction"] = None
        review["record"]["final_equity"] = None
        review["record"]["net_profit"] = None
        review["record"]["max_drawdown"] = None
    else:
        review["metrics"]["account_metrics_valid"] = (not gap_stopped)
        review["metrics"]["account_metrics"] = ("computed_prefix_only" if gap_stopped
                                                else "computed")
    review["record"]["metrics"] = review["metrics"]
    led.add_review(**review["record"])
    recon = L.reconcile(con, run_id)

    # ── 第24：全损压力**分列**（仅受影响单；与实际成交分列，不并入现金/权益）──────────
    # 第26：受影响单 = 严格停算的「无法计算」持仓（含共享账户冻结波及的开仓持仓）
    stress_records = []
    if approx is not None and uncomputable:
        affected = [positions[u["position_id"]] for u in uncomputable
                    if u["position_id"] in positions]
        stress_records = compute_full_loss_stress(affected)
        for rec in stress_records:
            led.add_stress_full_loss(position_id=rec["position_id"], basis=rec["basis"],
                                     exchange=rec["exchange"], symbol=rec["symbol"],
                                     entry_notional=rec["entry_notional"],
                                     known_fees=rec["known_fees"],
                                     known_funding=rec["known_funding"],
                                     stress_loss=rec["stress_loss"], note=rec["note"])

    # ── 第16：正式就绪**分别表达**（工程绑定完整 / 局部对账 / 正式就绪）────────────
    # 89a 缺陷2：旧式 `formal_ready = binding_ok ∧ dv ∧ ¬blocked ∧ reconcile.ok` 在「六维全填
    # 未核验合成串 + _synthetic + 空行情」时也为 True。现把三者拆开，且正式就绪额外要求：
    # 非合成 ∧ 逐日名单强制 ∧ 输入非空 ∧ 绑定值与本轮实际源/代码/配置/费用/范围一致
    # ∧ **独立接受门禁（wx_gate）研究放行**。本轮无真实接受凭据 ⇒ formal_ready 恒 False。
    recon_ok = bool(recon.get("ok"))
    input_nonempty = bool(symbol_data) and bool(all_ts)
    # 第17：**先**从真实来源解析门禁（不信任 resolved["run_binding"] 缓存），再据真实源核六维一致性。
    # 第19：资金费维改**对实际材料实核** —— 实际材料由本次**真实执行输入**（`symbol_data`）规范化
    # 资金费序列 + 真正消费 mark（隔离账本**实际入账**行）派生；调用方 `funding_actual` **仅作声明**
    # 与实际材料交叉核对（材料是声明、不是可信实际证据）。
    gate_doc, gate_err = resolve_formal_gate(resolved, formal_gate)
    ledger_funding = _ledger_funding_rows(con, run_id)
    # 第33：**完整输入版本材料**（完整 marks，**不**被账本使用子集覆盖）—— 声明与执行器**同源**。
    actual_funding = actual_funding_materials(symbol_data)
    # 第33：**逐笔双向审查**（实际使用凭证 + 正向逐行 + 反向应收付集合；冻结前缀边界）。
    _usage_model = parse_funding_model(run_binding.get("funding_model"))[0] if binding_ok else None
    funding_usage = audit_funding_usage(
        actual_funding, con=con, run_id=run_id, model=_usage_model,
        freeze_ts=frozen_at,
        rate_series_by_sym={k: sd.get("funding_rate_series")
                            for k, sd in symbol_data.items()},
        complete_identity=funding_materials_identity(_usage_model, actual_funding)
        if _usage_model else None)
    # ── 第21：**当前执行子运行**身份（真实账本记录）+ **实际使用**逐日映射内容 ──────────────
    # 批次级 run（门禁实算身份）与子臂 run 分列；子运行记录来自 `research_run` 行（非调用方字典）。
    batch_run_id = ((gate_doc or {}).get("run_identity") or {}).get("run_id")
    daily_doc = (daily_list_doc_from_maps(
        rank_keys_by_day, admission_by_day,
        scope=(run_binding or {}).get("scope") or AUTHORIZED_SCOPE,
        usage_mask="run_actual_daily_map") if daily_enforced else None)
    child_run = child_run_identity(
        con, run_id, run_binding=run_binding, data_version=dv, code_sha256=code_sha256,
        parent_batch_run_id=batch_run_id)
    # 第21：**贯通**本次实际材料/账本、实际使用名单内容、当前执行子运行到真实来源接口。
    # 第22：一并传**本次真实账本连接** `con` ⇒ 子运行记录对 `research_run` 行逐字段核验（缺口修复）。
    # 第33：`funding_usage`（逐笔双向审查）一并贯通 ⇒ 门禁/审查可核「实际使用凭证」。
    consistency = binding_actual_consistency(
        run_binding, None, real_gate=gate_doc, data_version=dv,
        funding_actual=actual_funding, funding_ledger=ledger_funding,
        funding_declaration=funding_actual, child_run=child_run,
        daily_list_doc=daily_doc, funding_usage=funding_usage,
        daily_list_source="run_arm_actual_daily_map", ledger=con) if binding_ok else None
    formal_ready, formal_missing = compute_formal_ready(
        binding_ok=binding_ok, binding=run_binding, data_version=dv,
        daily_enforced=daily_enforced, input_nonempty=input_nonempty, blocked=blocked,
        recon_ok=recon_ok, consistency=consistency, gate_doc=gate_doc, gate_error=gate_err)
    # 第33：把**逐笔双向审查**裁决持久化入账本（证据；`research_funding_audit`）。
    try:
        led.add_funding_audit(
            ok=bool(funding_usage.get("ok")), n_expected=funding_usage.get("n_expected"),
            n_charged=funding_usage.get("n_charged"),
            voucher_id=((funding_usage.get("voucher") or {}).get("voucher_id")),
            complete_input_identity=funding_usage.get("complete_input_identity")
            or ((funding_usage.get("voucher") or {}).get("complete_input_identity")),
            model=_usage_model, problems=funding_usage.get("problems"),
            detail=json.dumps({"forward": funding_usage.get("forward"),
                               "reverse": funding_usage.get("reverse")},
                              ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001 —— 旧账本无该表等 ⇒ 不阻断主流程（审查结果仍随返回值暴露）
        pass
    return {"run_id": run_id, "review": review, "reconciliation": recon, "halted": halted,
            "blocked": blocked, "blocked_count": len(blocked), "terminal": terminal,
            "final_cash": cash,
            # 第19：本次**完整输入版本**资金费材料（由真实执行输入派生，**不被使用子集覆盖**）
            # + 账本实际入账行（供核验/追溯）
            "funding_materials": actual_funding, "funding_ledger_rows": ledger_funding,
            # 第33：**实际使用凭证**（从同一真实账本派生，指向完整输入版本）+ **逐笔双向审查**
            "funding_usage": funding_usage,
            "funding_usage_voucher": funding_usage.get("voucher"),
            # 工程绑定完整（≠ 正式就绪；兼容第15 字段名 run_binding_ok）
            "binding_ok": binding_ok, "run_binding_ok": binding_ok,
            "run_binding_missing": binding_missing,
            "binding_consistency": consistency,
            # 第21：当前执行子运行身份记录（来自真实账本 `research_run` 行）+ 实际使用逐日映射内容
            "child_run": child_run, "actual_daily_list_doc": daily_doc,
            "data_version": dv,
            # 局部逐笔对账（独立重算）
            "local_reconcile_ok": recon_ok,
            "daily_enforced": daily_enforced,
            # 第24：保守近似规格身份 + 缺口停算 + 全损压力（与实际成交**分列**）
            "approx_spec_sha": (approx["spec_sha"] if approx else None),
            "approx_enabled": (approx is not None),
            "gap_blocks": gap_blocks, "gap_stopped": gap_stopped,
            "valid_full_segment": (not gap_stopped and not uncomputable),
            "gap_affected_positions": sorted(gap_pos_ids),
            "uncomputable_positions": [u["position_id"] for u in uncomputable],
            "uncomputable": uncomputable, "frozen_at": frozen_at,
            "stress_full_loss": stress_records,
            # 第40：A/B 双轨执行状态（B 的账户级指标 UNKNOWN；停用回撤锁止；不末尾假平）
            "run_mode": ("version_b_wind_down" if wind_down else "version_a_strict"),
            "wind_down": wind_down, "first_anomaly_ts": first_anomaly_ts,
            "wind_down_positions": sorted(wind_down_ids),
            "wind_down_completed": bool(wind_down),
            "open_positions_at_end": sorted(pid for pid, p in positions.items()
                                            if p.state == "open"),
            "account_metrics_valid": (not wind_down and not gap_stopped),
            "account_metrics": ("UNKNOWN" if wind_down
                                else ("computed_prefix_only" if gap_stopped else "computed")),
            "drawdown_lock_enabled": (not wind_down),
            # 正式就绪（含独立接受门禁）
            "formal_ready": formal_ready, "formal_missing": formal_missing,
            "formal_gate": {
                "research_pass": bool(gate_doc.get("research_pass")) if gate_doc else False,
                "reason": (gate_doc or {}).get("reason") or gate_err,
                "source": (gate_doc or {}).get("source"),
                "sha256": (gate_doc or {}).get("sha256"),
                "isolated": bool((gate_doc or {}).get("isolated")),
                "synthetic": bool((gate_doc or {}).get("synthetic")),
                # 第17：调用方声明（缓存/显式）与真实来源的核对错误（None = 无声明或一致）
                "declaration_error": gate_err,
            }}



def _try_enter(led, positions, e, ts, risk, ex, symbol_data, bar_by, cash, committed,
               unreal, portfolio_risk, halted, ft_engine=None, approx=None):
    """返回**现金变动**（入场费，负值）；被拒返回 0.0。

    `approx`（第24，可选）：给出时启用**保守取整/最小单/缺口禁开**新规格（买价上取整、
    保护价下取整、最小可下数量、取整后风险重核）；为 None 时与改前逐字节等价。
    """
    exchange, symbol = e["exchange"], e["symbol"]
    sd = symbol_data[(exchange, symbol)]
    b = bar_by.get((exchange, symbol, ts))
    # 完整身份含 exchange：**同名跨所**不得共用 position_id（整改3 修复跨所串单）。
    pid = hashlib.sha256(
        f"{led.run_id}|{exchange}|{symbol}|{e['event_time']}".encode()).hexdigest()[:16]

    def reject(reason, rounding=None):
        led.add_intent(intent_id=pid, event_uid=e.get("event_uid"), exchange=exchange,
                       symbol=symbol, side="long", created_at=ts, eligible_open_time=ts,
                       reason=reason, status="rejected")
        if rounding is None:
            led.add_order(intent_id=pid, status="reject", reject_reason=reason, source="market")
        else:
            led.add_order(intent_id=pid, status="reject", reject_reason=reason, source="market",
                          qty_raw=rounding.get("qty_raw"),
                          qty_step_delta=rounding.get("qty_step_delta"),
                          price_raw=rounding.get("price_raw"),
                          price_step_delta=rounding.get("price_step_delta"),
                          min_orderable_qty=rounding.get("min_orderable_qty"),
                          limits=rounding.get("limits"),
                          rounding_spec_sha=(approx or {}).get("spec_sha"))
        return 0.0

    # E05：每条入场路径都先检查 run 是否已锁止
    if halted is not None:
        return reject("run_halted_drawdown_lock")
    if b is None:
        return reject("no_execution_bar_at_eligible_open")
    if sum(1 for p in positions.values() if p.state == "open") >= risk.max_positions:
        return reject("max_positions")
    contract = sd.get("contract")
    if contract is None or not contract.quantity_step or contract.quantity_step <= 0:
        return reject("missing_contract_precision")
    if not sd.get("funding_available", False):
        return reject("missing_funding_coverage")
    atr = e.get("atr")
    if atr is None or not math.isfinite(float(atr)) or float(atr) <= 0:
        return reject("missing_or_invalid_signal_atr")

    # ── 第24：保守取整 / 最小单 / 缺口禁开（仅 approx 启用；legacy 走下方原路径）──────────
    if approx is not None:
        limits = effective_order_limits(contract)
        if limits is None:
            return reject("missing_contract_precision")
        gap = detect_input_gap(b)
        if gap:
            led.add_gap_block(exchange=exchange, symbol=symbol, ts=ts, kind=gap,
                              detail="new_entry_blocked")
            return reject(f"input_gap:{gap}")
        raw_open = float(b[1])
        # 结构失效线（与止损锚点**分开**的独立条件）
        det_low = e.get("detection_low")
        if det_low is not None and raw_open <= float(det_low):
            return reject("structure_pre_fill_open_below_detection_low")
        equity = cash + unreal
        gross_open = sum(p.notional for p in positions.values() if p.state == "open")
        free_cash = cash - committed
        prev = bar_by.get((exchange, symbol, ts - DETAIL_MS))
        prev_qv = float(prev[5]) if (prev is not None and prev[5] is not None) else None
        res = apply_order_rounding(
            raw_open=raw_open, atr=float(atr), limits=limits,
            risk_per_trade_fraction=risk.risk_per_trade_fraction, equity=equity,
            stop_atr_mult=ex.stop_atr_mult, slippage_each_side=ex.slippage_each_side,
            gross_open_notional=gross_open, free_cash=free_cash, leverage=risk.leverage,
            cash_reserve_fraction=risk.cash_reserve_fraction,
            single_name_notional_cap=risk.single_name_notional_cap,
            gross_long_notional_cap=risk.gross_long_notional_cap,
            prev_quote_volume=prev_qv,
            participation_cap=ex.entry_participation_prev15m_quote_cap,
            portfolio_risk_open=portfolio_risk, portfolio_risk_cap=risk.portfolio_risk_cap)
        if res["reject"]:
            return reject(res["reject"], rounding=res)
        entry_price, stop_price = res["entry_price"], res["stop_price"]
        qty, notional = res["qty"], res["notional"]
        margin = notional / risk.leverage
        ok, why = led.try_reserve(position_id=pid, amount=margin, cash_available=free_cash,
                                  created_at=ts, total_cash=cash)
        if not ok:
            return reject(f"reservation:{why}", rounding=res)
        fee = notional * ex.fee_each_side
        slip_cost = qty * (entry_price - raw_open)
        positions[pid] = Position(position_id=pid, exchange=exchange, symbol=symbol,
                                  entry_time=ts, entry_price=entry_price, quantity=qty,
                                  notional=notional, stop_price=stop_price,
                                  safety_exit_at=ts + ex.max_holding_hours * HOUR_MS,
                                  reservation=margin, entry_fee=fee,
                                  entry_slippage_cost=slip_cost)
        led.add_intent(intent_id=pid, event_uid=e.get("event_uid"), exchange=exchange,
                       symbol=symbol, side="long", created_at=ts, eligible_open_time=ts,
                       reason="ok", status="filled")
        led.add_order(intent_id=pid, status="fill", fill_time=ts, fill_price=entry_price,
                      quantity=qty, fee=fee, slippage=slip_cost, notional=notional,
                      source="market", qty_raw=res["qty_raw"],
                      qty_step_delta=res["qty_step_delta"], price_raw=res["price_raw"],
                      price_step_delta=res["price_step_delta"],
                      min_orderable_qty=res["min_orderable_qty"], limits=res["limits"],
                      rounding_spec_sha=approx.get("spec_sha"))
        led.add_position(_pos_row(positions[pid], "open"))
        if ft_engine is not None:
            ft_engine.open_position(positions[pid], ts, entry_price, fee, slip_cost)
        return -fee

    raw_open = float(b[1])
    # E01：先算**实际含滑点成交价**，止损锚点 = 成交价 − 3×信号 bar ATR
    entry_price = raw_open * (1.0 + ex.slippage_each_side)
    stop_price = _round_price_step(entry_price - ex.stop_atr_mult * float(atr),
                                   getattr(contract, "price_step", 0.0))
    if entry_price <= stop_price:
        return reject("nonpositive_stop_distance")
    # 结构失效线（与止损锚点**分开**的独立条件）
    det_low = e.get("detection_low")
    if det_low is not None and raw_open <= float(det_low):
        return reject("structure_pre_fill_open_below_detection_low")

    equity = cash + unreal
    stop_dist = entry_price - stop_price
    if stop_dist <= 0:
        return reject("nonpositive_stop_distance")
    qty = _step_floor(equity * risk.risk_per_trade_fraction / stop_dist, contract.quantity_step)
    if qty is None or qty <= 0:
        return reject("quantity_below_step")
    notional = qty * entry_price
    if notional < contract.min_notional:
        return reject("below_min_notional")
    if notional > equity * risk.single_name_notional_cap:
        return reject("single_name_notional_cap")
    gross = sum(p.notional for p in positions.values() if p.state == "open") + notional
    if gross > equity * risk.gross_long_notional_cap:
        return reject("gross_long_notional_cap")
    margin = notional / risk.leverage
    free_cash = cash - committed
    if free_cash - margin < equity * risk.cash_reserve_fraction:
        return reject("cash_reserve_cap")
    prev = bar_by.get((exchange, symbol, ts - DETAIL_MS))
    if prev is not None and notional > float(prev[5]) * ex.entry_participation_prev15m_quote_cap:
        return reject("entry_participation_cap")
    if portfolio_risk + stop_dist * qty > equity * risk.portfolio_risk_cap:
        return reject("portfolio_risk_cap")

    # E06：预占只扣一次 held —— 传 total_cash 让账本在事务内重算 free = cash − held
    ok, why = led.try_reserve(position_id=pid, amount=margin, cash_available=free_cash,
                              created_at=ts, total_cash=cash)
    if not ok:
        return reject(f"reservation:{why}")

    fee = notional * ex.fee_each_side
    slip_cost = qty * (entry_price - raw_open)
    positions[pid] = Position(position_id=pid, exchange=exchange, symbol=symbol, entry_time=ts,
                              entry_price=entry_price, quantity=qty, notional=notional,
                              stop_price=stop_price,
                              safety_exit_at=ts + ex.max_holding_hours * HOUR_MS,
                              reservation=margin, entry_fee=fee, entry_slippage_cost=slip_cost)
    led.add_intent(intent_id=pid, event_uid=e.get("event_uid"), exchange=exchange, symbol=symbol,
                   side="long", created_at=ts, eligible_open_time=ts, reason="ok", status="filled")
    led.add_order(intent_id=pid, status="fill", fill_time=ts, fill_price=entry_price,
                  quantity=qty, fee=fee, slippage=slip_cost, notional=notional, source="market")
    led.add_position(_pos_row(positions[pid], "open"))
    if ft_engine is not None:
        ft_engine.open_position(positions[pid], ts, entry_price, fee, slip_cost)
    return -fee


def _pos_row(p, status):
    return {"position_id": p.position_id, "exchange": p.exchange, "symbol": p.symbol,
            "entry_time": p.entry_time, "entry_price": p.entry_price, "quantity": p.quantity,
            "notional": p.notional, "stop_price": p.stop_price,
            "safety_exit_at": p.safety_exit_at, "exit_time": p.exit_time,
            "exit_price": p.exit_price, "exit_reason": p.exit_reason,
            "gross_pnl": p.gross_pnl, "fees": p.entry_fee + p.exit_fee, "funding": p.funding,
            "net_pnl": p.net_pnl, "status": status, "reservation_amount": p.reservation}


def _review(con, run_id, risk, halted, final_cash):
    pos = con.execute("SELECT * FROM research_position WHERE run_id=? AND status='closed'",
                      (run_id,)).fetchall()
    eq = con.execute("SELECT * FROM research_equity WHERE run_id=? ORDER BY ts",
                     (run_id,)).fetchall()
    closed = len(pos)
    wins = sum(1 for p in pos if (p["net_pnl"] or 0) > 0)
    losses = sum(1 for p in pos if (p["net_pnl"] or 0) < 0)
    gross_profit = sum((p["net_pnl"] or 0) for p in pos if (p["net_pnl"] or 0) > 0)
    gross_loss = sum((p["net_pnl"] or 0) for p in pos if (p["net_pnl"] or 0) < 0)
    total_fees = sum((p["fees"] or 0) for p in pos)
    total_funding = sum((p["funding"] or 0) for p in pos)
    # E02：最终权益 = **成本后最终现金**（终止退出费/滑点/资金费均已入账），并与末条权益行核对
    final_eq = final_cash
    peak, mdd = None, 0.0
    for e in eq:
        v = e["equity"]
        if v is None:
            continue
        peak = v if peak is None else max(peak, v)
        if peak and peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    net_profit = final_eq - risk.initial_equity
    equity_row_final = eq[-1]["equity"] if eq else None
    equity_matches_cash = (equity_row_final is not None
                           and abs(equity_row_final - final_cash) <= 0.01)
    metrics = {"final_equity": final_eq, "final_cash": final_cash,
               "equity_row_final": equity_row_final,
               "equity_matches_cash": equity_matches_cash,
               "net_profit": net_profit,
               "max_drawdown_fraction": mdd, "win_rate": (wins / closed) if closed else None,
               "trades": closed, "halted": halted,
               "note": "研究模型；未执行完整回测前不得称收益通过"}
    return {"record": {"closed_trades": closed, "wins": wins, "losses": losses,
                       "gross_profit": gross_profit, "gross_loss": gross_loss,
                       "total_fees": total_fees, "total_funding": total_funding,
                       "net_profit": net_profit, "final_equity": final_eq,
                       "max_drawdown": mdd, "halted": halted, "metrics": metrics},
            "metrics": metrics}
