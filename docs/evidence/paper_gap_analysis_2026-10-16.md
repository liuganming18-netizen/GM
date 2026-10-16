# Paper 断点诊断（只做诊断，不做恢复）

**诊断时间**：2026-10-16 14:58–15:15（UTC+8）
**诊断范围**：`框架/backend/paper_engine.py`、`live_store.py`、`strategies/registry.py`、`strategies/wx_donchian_wyckoff.py`
**本阶段约束（已遵守）**：未创建任何账本、未恢复 3a–3d 生产逻辑、未修改 Paper 业务逻辑、未删除任何旧代码。

---

## 0. 结论速览

| 问题 | 结论 |
|---|---|
| Paper 当前能否启动 | **不能**。纸面账本目录 `数据/paper/` 不存在，引擎按设计 fail-closed |
| 真正缺的是什么 | **不是「四个账本文件」，而是「整套旧账户模型」** —— 详见 §5 |
| 3a/3b/3c/3d 最终归宿 | **代码：Legacy（保留可读，禁止进生产）**；**数据：可迁移（仅作历史追溯）**；**账户模型：必须废弃** —— 详见 §8 |

---

## 1. 3a / 3b / 3c / 3d 的业务意义

四臂**不是四个策略**，而是**同一个确定性核心的四个信号变体**。

核心实现在 `strategies/wx_donchian_wyckoff.py`（`ARM_IDS = ("3a","3b","3c","3d")`，L38），
四个臂共用同一套指标与出场逻辑，只在「入场触发条件」上不同：

| 臂 | 源码原文定义（`wx_donchian_wyckoff.py` L19–22、L624–628） |
|---|---|
| **3a** | 唐奇安基线：1h 收盘**严格突破**此前 20 根最高价 |
| **3b** | `3a` **AND** 放量（quote 成交额 > 此前 20 根均量 × 1.5）**AND** 阳线（close > open） |
| **3c** | `3a` **OR** Spring 确认（跌破箱体后收回、第 1–3 根小时收盘站上冻结中线） |
| **3d** | `3a` **OR** SC 确认（放量新低后第 1–5 根小时收盘越过恢复目标） |

**它们的经济含义**：把「唐奇安通道突破」这一个基线，分别叠加「成交量确认（3b）」
「威科夫 Spring（3c）」「威科夫 SC（3d）」，形成四条入场规则，用于对比哪一类确认更有效。

**账户含义**：每臂一个独立 5000 USDT 账本（`live_store.INIT_EQUITY = 5000.0`，
账户 ID `paper43_{arm}`），四臂合计 20,000 USDT。这是**旧研究阶段的对照实验设计**，
不是生产账户结构。

**当前注册表状态（关键）**：`strategies/registry.py` L44 已经把「当前启用族」切成新策略：

```python
CURRENT_RUN_KEYS = (CURRENT_BACKTEST_KEY,)          # = ("wx_oi_resonance_v1",)
LEGACY_KEYS      = tuple(wx.ARM_IDS) + (strategy_a.KEY, strategy_b.KEY)
```

即：**注册表层面 3a–3d 已经降级为 legacy 只读**（L45 注释：「旧 F_WX（3a–3d）降为 legacy 只读，
与旧 A/B 共同保留为历史追溯，不改名冒充新策略」）。

> ⚠ **但 `paper_engine` / `live_worker` / `live_store` / `live_signal_engine` 四处的
> `ARMS = ("3a","3b","3c","3d")` 仍然是硬编码的四臂**（`live_store.py:67`、
> `live_signal_engine.py:79`、`live_worker.py:49` 取 `L.ARMS`）。
> **注册表已切换、执行侧没切换** —— 这就是「双轨」在 Paper 上的具体表现。

---

## 2. 哪些代码只属于 Legacy

### 2.1 纯 Legacy（只服务 3a–3d 四臂，与 `WX_OI_RESONANCE_V1` 无任何关系）

| 文件 | 行 / 位置 | Legacy 内容 |
|---|---|---|
| `strategies/wx_donchian_wyckoff.py` | L38 `ARM_IDS`；L509–L540 | 四臂入场分派（`if arm == "3b"/"3c"/"3d"`） |
| `strategies/registry.py` | L46 `LEGACY_KEYS`；L105–L137 `describe()` | 四臂元数据与 legacy 只读标记 |
| `live_store.py` | L67 `ARMS`；L450 `ledger_path()`；L457 `expected_account_id()` | 四库路径与 `paper43_{arm}` 账户身份 |
| `live_signal_engine.py` | L79 `ARMS`；L90 `STRUCT_ARMS`；L146 `LEGACY_ARMS` | 四臂信号写入与结构臂映射 |
| `live_worker.py` | L49 `ARMS = L.ARMS` | 四臂循环推进 |
| `paper_engine.py` | L630 / L1630 / L1705 / L1719 | 四臂遍历（`for arm in L.ARMS`） |
| `paper_engine.py` | L1590 / L1594 / L1598 / L1722 等 | 所有 SQL 都带 `account_id = f"paper43_{arm}"` |
| `strategies/wx_daily_375.py`、`wx_config.py`、`wx_gate.py`、`wx_ledger.py`、`wx_execution.py` | 全文 | 375 名单 / 冻结参数视图 / 门禁 / 账本 / 执行（旧研究链） |
| `adapter.py` | L83 `CURRENT_RUN_KEYS`、L2850 | 后端仍以「当前启用族」名义暴露 3a–3d 的路由（与注册表已不同步） |
| `UI界面/web/src` | 策略选择器 | 旧四臂标签残留 |

### 2.2 混合（骨架可留、四臂分支要拆）

| 文件 | 可复用骨架 | 必须剥离的四臂耦合 |
|---|---|---|
| `paper_engine.py` | 成交时钟（`FILL_POLICY`）、因果修正、挂单推进、回撤四动作、资金费结算、`switches()` | `arm` 参数贯穿全文、`paper43_{arm}` 账户 ID、逐臂独立账本 |
| `live_store.py` | `paper_conn()` 的 fail-closed 纪律、身份核对、加性 schema 升级 | `ARMS` 常量、四库路径、每臂 `expected_account_id` |
| `live_worker.py` | 一轮编排（信号→paper→账户→心跳） | `for arm in ARMS` 四臂循环 |
| `live_signal_engine.py` | 币池准入（`live_pool`）、增量水位 | `ARMS` 分派、`STRUCT_ARMS` 映射 |

---

## 3. 哪些数据可以迁移

**可迁移（有历史追溯价值，建议只读归档）**：

| 数据 | 位置（迁移目标） | 价值 |
|---|---|---|
| 四臂历史成交 | `paper_{arm}.sqlite3` → `paper_fill` | 旧研究的逐笔成交记录 |
| 四臂已实现盈亏 | → `paper_realized` | 旧研究绩效 |
| 四臂历史持仓 | → `paper_position` | 含 `entry_ts` / `exit_ts` / `stop_price` / `funding_paid` |
| 四臂资金费流水 | → `paper_funding`、`paper_funding_gap`、`paper_funding_correction` | 真实结算事件记录 |
| 四臂权益曲线 | → `paper_equity` | 逐 15m 权益 |
| 四臂订单流水 | → `paper_order` | 含拒单原因 |
| 四臂幂等水位 | → `paper_processed` | 已处理信号去重 |
| 账户身份声明 | `数据/paper/ledger_identity.json`（`live_store.LEDGER_MANIFEST`） | 库身份交叉核对依据 |

**迁移纪律（继承既有约定，不得放松）**：

- 按迁移清单 **M002 / M003 / M004 / M005** 逐项受控迁入（`live_store.py` L607 的报错文案已指明）。
- 库文件必须与 `ledger_identity.json` **成对**迁入，否则 `verify_ledger_identity()` 抛
  `LedgerIdentityMismatch`（L510–L524）。
- **不得**用空库冒充、**不得**用 `PAPER_LEDGER_ALLOW_INIT=1` 建档（L602–L608 明确禁止）。

---

## 4. 哪些逻辑不能迁移

| # | 不可迁移项 | 位置 | 为什么不能迁移 |
|---|---|---|---|
| 1 | **四臂独立 5000U 账户模型** | `live_store.INIT_EQUITY=5000` × `ARMS` 四臂 | 与执行核心的**共享账户**口径**直接冲突**。`exec_bridge.EXEC_FROZEN` 明确是 `shared_account=True`、`initial_equity=5000`、`max_positions_total=10`。四臂各 5000 ⇒ 合计 20,000 U、合计最多 40 仓 —— **敞口放大 4 倍** |
| 2 | `paper43_{arm}` 账户 ID 体系 | `live_store.expected_account_id()` L457 | 新架构是单一账户；四臂 ID 会让「共享 10 仓上限」永远无法全局生效 |
| 3 | **逐臂独立回撤锁定** | `paper_engine.drawdown_lock_state()` L1433、`enforce_drawdown()` L1440 | 锁定态按 `arm` 分键（`live_run:drawdown_lock` 的 `st[arm]`）。共享账户必须**全局锁**，否则一臂锁死另三臂照常开仓 |
| 4 | **逐臂独立幂等水位** | `paper_processed` 按 `arm` 分行（L1729 `WHERE arm=?`） | 共享账户必须**统一信号去重**，否则同一信号会在四臂各开一次 |
| 5 | 四臂入场分派逻辑 | `wx_donchian_wyckoff.py` L509–L540 | 新策略 `WX_OI_RESONANCE_V1` 的入场是 OI 共振 + 唐奇安/Spring/SC，**不是**「3a OR ...」的四臂择一 |
| 6 | 逐臂权益/现金独立核算 | `paper_engine.equity()` / `recover_report()` L1742–L1743 | 现金恒等式以**单臂 5000** 为基（`expect = L.INIT_EQUITY + net - ...`）；共享账户下这个恒等式必须整体重写 |

> **一句话**：四臂账本的**行数据**可以搬，**账户模型和风控口径**不能搬。
> 搬行数据 = 保留历史；搬账户模型 = 把 4 倍敞口和 4 套风控锁带进新架构。

---

## 5. Paper 当前为什么拒绝启动

### 5.1 直接原因：账本目录不存在（fail-closed，设计如此）

```
PAPER_LEDGER_DIR      = /Users/boos/Desktop/威科夫因子共振趋势/数据/paper
该目录存在？          否
PAPER_LEDGER_ALLOW_INIT = 0（禁止自动建档）
```

四个臂全部被拒：

```
3a: LedgerUnavailable —— 纸面账本缺失：…/数据/paper/paper_3a.sqlite3（请按迁移清单 M002 受控迁入）
3b: LedgerUnavailable —— …（M003）
3c: LedgerUnavailable —— …（M004）
3d: LedgerUnavailable —— …（M005）
```

`paper_engine.snapshot_all()` 实测抛 `LedgerUnavailable`。

**这是**正确**行为，不是 bug**：源码 L594–L608 明确「缺库即报错，绝不自动创建」，
理由是历史缺陷 —— 曾因相对路径推导 + `create=True` 凭空建出 4 份空账本，
被当成原账户读（等同新开四个 5000U 假账户）。

### 5.2 附加原因一：新开仓总闸关闭

`live/paper_new_entry_gate.json`：

```json
{ "allow_new_entries": false,
  "reason": "stage_A_closure_review_pending",
  "set_by": "work_2026-10-15_stageA" }
```

即使账本存在，**新开仓仍被阻断**；但退出 / 费用 / 资金费 / 健康采集 / 只读 API **不受影响**
（`paper_engine.switches()` L1530–L1542 实测输出 `maintain_existing: true`）。

### 5.3 附加原因二：Live 侧入口先崩（更靠前）

`live_worker.py` L42 顶层 `import _warmup43` 直接 `ModuleNotFoundError`
⇒ 即使账本齐备，**worker 也起不来**（详见 `live_gap_analysis_2026-10-16.md`）。

---

## 6. `数据/paper/` 缺失时的完整调用链

### 6.1 读路径（HTTP GET）—— 降级为 `unavailable`，**不返回 500**

```
GET /api/v1/paper/accounts
 └─ adapter.route(...)                                    [adapter.py]
     └─ live_api.paper_accounts()                         [live_api.py:600]
         └─ paper_engine.snapshot_all()                   [paper_engine.py:1629]
             └─ for arm in L.ARMS:  snapshot(arm)         [paper_engine.py:1630 / 1551]
                 └─ L.paper_conn(arm)                     [live_store.py:592]
                     └─ path = ledger_path(arm)  → 不存在 [live_store.py:450]
                         └─ allow_create = (create is True) or (create is None and LEDGER_ALLOW_INIT)
                            → False                          [live_store.py:602]
                             └─ raise LedgerUnavailable   [live_store.py:605]
         └─ except (L.LedgerUnavailable, L.LedgerIdentityMismatch)
             → 返回 quality.status = "unavailable"        [live_api.py:605-609 / 692-697]
```

同样降级的端点：`paper_positions`(L658)、`paper_orders`(L703)、`paper_funding_audit`(L738)。

### 6.2 写路径（HTTP POST）—— 降级为 **503**

```
POST /api/v1/paper/orders
 └─ live_api.paper_order_post(payload)                    [live_api.py:757]
     └─ paper_engine.open_position(arm, symbol, ...)      [paper_engine.py:676]
         └─ L.paper_conn(arm)  →  LedgerUnavailable
     └─ except ... → _ledger_unavailable(...), 503        [live_api.py:790-792 / 814-815]
```

### 6.3 后台路径（worker 轮）—— **未捕获，直接崩**

```
live_worker.main()                                        [live_worker.py:521]
 └─ L.ensure_live_schema()      ← 建 live_signals 表（OK，此库不依赖 paper）
 └─ for arm in ARMS:  L.paper_conn(arm).close()           [live_worker.py:527-528]
     └─ LedgerUnavailable   ← **此处无 try/except**
```

> **注意**：这条路径在实际运行时**到不了**，因为 `live_worker` 在
> L42 的 `import _warmup43` 就已经抛 `ModuleNotFoundError` 退出了。
> 即：**Paper 的账本缺失被 Live 的模块缺失「挡住」了** —— 修复顺序上，
> 应先解决 Live 的模块缺失，Paper 的账本缺失才会显形。

### 6.4 降级设计评价（如实说明）

| 方面 | 评价 |
|---|---|
| 是否 fail-closed | ✅ 是。缺库不会静默造假账户、不会返回空列表冒充「无订单」 |
| 是否可诊断 | ✅ 报错文案含具体路径 + 迁移清单编号 + 解除方法 |
| 是否影响其他功能 | ✅ 不影响。行情库只读、健康端点、回测端点均正常 |
| 唯一瑕疵 | ⚠ `live_worker` L527 的 `paper_conn` 未包 try/except，账本缺失时会以裸异常终止整轮（可接受，但不够友好） |

---

## 7. 当前 Paper 使用的各层实现

| 层 | 实现 | 位置 | 与 `WX_OI_RESONANCE_V1` 的关系 |
|---|---|---|---|
| **Strategy** | `strategies/wx_donchian_wyckoff.py`（四臂冻结核心） | `ARM_IDS = ("3a","3b","3c","3d")` | 🔴 **完全不同**。Paper 跑的是旧唐奇安+Spring/SC 四臂，不是 OI 共振 |
| **Signal** | `live_signal_engine.py` | L79 `ARMS`、L90 `STRUCT_ARMS` | 🔴 旧四臂信号；且依赖缺失的 `_接入43` / `_warmup43` ⇒ 实际不可用 |
| **Risk** | `exec_bridge` → `策略/WyckoffMaster/execution.py` 的 `EXEC_FROZEN` | `paper_engine.py:43` 导入；L1408 `EB.decision()`；L1459 `EB.drawdown_state()` | 🟡 **同一执行核心**（`exec_core_v3.1.0`，`config_hash=fb3e617f…`）。这是唯一已经共享的一层 |
| **Order** | `paper_engine` + `live_store.paper_order` / `paper_pending` | 表定义 `live_store.py:200 / 326` | 🟡 字段体系自建，与回测的 `orders` 表**不互通** |
| **Execution** | `paper_engine.open_position()` / `close_position()` | L676 / L1290 附近 | 🔴 成交价 = 已闭合 15m bar 的 open；与回测的 `SimulatedExecutionEngine` **各写一套** |
| **Position** | `live_store.paper_position` | 表定义 L204 | 🟡 有 `stop_price` / `funding_paid` / `voided`，但无保证金/强平价概念 |
| **Trade** | `live_store.paper_fill` / `paper_realized` | 表定义 L230 / L251 | 🟡 与回测 `trades` 表字段不同（回测有 `mfe`/`mae`/`holding_hours`/`funding_gaps_json`） |
| **Ledger** | `数据/paper/paper_{3a..3d}.sqlite3`（**每臂一个库**） | `live_store.py:450` | 🔴 **物理分裂**：4 个库、4 套账户、4 套权益曲线 |

### 7.1 关键口径对照（Paper vs Backtest）

| 口径 | Paper（现状） | Backtest（Golden） | 是否一致 |
|---|---|---|---|
| 账户结构 | 四臂各 5000 U（合计 20,000） | 单账户 100,000 U | 🔴 不可比 |
| 初始权益 | 5000 / 臂 | 100,000 | 🔴 |
| 并发上限 | 10 / 臂（合计 40） | 3 | 🔴 |
| 手续费 | `fee_each_side=0.0005` | maker 0.0002 / taker 0.0005 | 🟡 近似 |
| 滑点 | `slippage_each_side=0.0005`（5 bp） | `slippage_bps=3.0`（3 bp） | 🔴 **不同** |
| 资金费 | 真实结算事件（`funding_history.settled`） | `settlement_events`（真实结算事件） | ✅ 同口径 |
| 成交价 | 已闭合 15m bar 的 open + 15m 延迟 | 下一根 15m open（Conservative Next-Open） | 🟡 概念相近，实现不同 |
| 止损 | 入场价 − 3×信号 ATR（固定） | 结构止损（`struct_low`）+ ATR | 🔴 **不同** |
| 账本载体 | SQLite × 4 | `ledger.sqlite3` × 1 + CSV 快照 | 🔴 |

> 这张表就是**关卡 2（Paper ≈ Backtest 逐笔等价）在物理上无法对齐**的根因。

---

## 8. 3a / 3b / 3c / 3d 最终归宿（明确回答）

| 维度 | 归宿 | 理由 |
|---|---|---|
| **代码** | **Legacy（保留、可读、只读）** | 注册表已降为 `LEGACY_KEYS`；作为旧研究基线可追溯、可比对。**不删除**（删了就没有对照基线），**不进生产**（禁止出现在新链路） |
| **数据** | **迁移（只读归档）** | 按 M002–M005 迁入 `数据/paper/`，仅作历史追溯；**不参与新账户核算** |
| **账户模型** | **废弃（不迁移）** | 四臂独立 5000U 与共享账户口径冲突（§4 #1–#4）；迁移即放大 4 倍敞口 |
| **入场分派逻辑** | **Legacy** | 新策略入场规则完全不同（§4 #5） |
| **执行核心调用** | **迁移到 Shared Trading Core** | `exec_bridge → EXEC_FROZEN` 这部分是**唯一已共享且正确**的层，应作为新核心的种子（见 §9） |

---

## 9. Shared Trading Core 应从哪些现有代码抽取

**不要重新发明。** 现有代码里已经有可直接抽取的成熟件：

| 目标组件 | 抽取来源 | 说明 |
|---|---|---|
| **风控纯函数** | `策略/WyckoffMaster/execution.py` 的 `EXEC_FROZEN` + `plan_entry` / `validate_modes` / `normalize_direction` | 已经是纯函数、已有冻结哈希、已被 `exec_bridge` 桥接。**这是最该直接复用的一块** |
| **成交时钟（因果）** | `paper_engine.py` 的 `FILL_POLICY`（L84–L96）+ `order_request_ms` / `earliest_fill_ms` / `max_fill_lag` | 已经过因果修正（禁止回填旧价、禁止锚定已闭合 bar）。**回测侧目前没有等价实现** ⇒ 应以 Paper 这套为准 |
| **资金费结算** | `execution.settle_funding_events` + `paper_engine._settle_funding` | 事件身份幂等、缺口不补 0、迟到差集补记 |
| **回撤锁定四动作** | `paper_engine.enforce_drawdown` / `drawdown_state`（L1440 / L1433） | 已补齐「停开仓 / 撤挂单 / 下根开盘清仓 / 本轮锁定」 |
| **账本算术恒等式** | `evidence/Work_单策略回测_2026-10-16/verify_run_ledger.py` 的 32 项判据 | 已经是**独立重算**（不复用运行器函数）⇒ 可直接作为新账本的自检器 |
| **回测驱动与指标** | `backtest_runner.py` + `oi_resonance_bridge.py` | 已通过 Golden Reference 验证 |
| **账本 fail-closed 纪律** | `live_store.paper_conn` 的缺库/身份核对 | 迁移到统一账本时**必须保留**这套纪律 |
| **幂等与水位** | `paper_processed` / `live_run` 机制 | 共享账户下需从「按臂」改为「全局」 |

**应丢弃而非抽取的**：四臂分派、`paper43_{arm}` 账户 ID、逐臂独立账本/锁/水位、
`paper_engine` 里与回测重复的第二套成交/持仓实现。

---

## 10. 本阶段未做（如实声明）

- 未创建任何账本（含隔离测试账本）
- 未恢复 3a/3b/3c/3d 任何生产逻辑
- 未修改 `paper_engine.py` / `live_store.py` 任何一行
- 未删除任何旧代码
- 未启动 Paper（8787 后端未启、`live_worker` 未启）
