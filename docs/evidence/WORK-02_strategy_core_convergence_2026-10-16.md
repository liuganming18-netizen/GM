# WORK-02｜新策略净核 + Shared Trading Core 收敛 · 交付报告

- 施工单：`WORK-02｜新策略净核 + Shared Trading Core 前置施工单`
- 交付日期：2026-10-16
- 交付窗口：`evidence/Work_策略核心收敛_2026-10-16/`
- 唯一生产候选策略：`WX_OI_RESONANCE_V1`（注册表键 `wx_oi_resonance_v1`）
- 结论一句话：**把策略/风控/撮合/账本收敛成 Backtest / Paper / Live 三端共用的同一份代码，
  行为逐位不变（0 行为差异），身份指纹按设计变更（已逐位归因），正式生产仍被门禁拒绝。**

---

## 1. 任务定位与边界

把 `WX_OI_RESONANCE_V1` 从「一份 787 行的执行单体」整理为**正式核心契约**：
策略信号 → 风控 → 订单 → 执行策略 → 成交适配器 → 成交 → 持仓 → 成交记录 → 账本事件，
每一层都有唯一一份、三端共用。

**本次做的**：建立共享核心包；修复 OI 质量列广播缺陷；锁定未批准参数为 UNSET；
建统一参数指纹；建四时点 Signal 契约；建 PIT 可得性标注；拆开「决策」与「成交」；
建 Order 模型与状态机；把幂等键从随机改为确定性哈希；定义 Ledger 事件接口；
处理 Golden 降级；补 15 个指定测试。

**本次明确没做的**（施工单禁止项，逐条自检见第 12 节）：
恢复 `_warmup43`、恢复旧 `3a/3b/3c/3d`、改旧 `exec_bridge`、接真实下单、启动 Live、
改 React UI、重写数据层、造兼容假模块、伪造历史账本、改策略规则不留证据、
删失败测试、改 Golden 使其 PASS、为测试造 fake engine。

---

## 2. 第一原则与身份声明

- 新策略 `WX_OI_RESONANCE_V1` 是**唯一生产候选**；旧 `F_WX 3a/3b/3c/3d` 与
  `WyckoffMaster v3.1` 的成绩**不继承**（`config.NOT_INHERITED` 逐条对外披露）。
- 本次改造**不改策略规则**：所有阈值、窗口、权重、相位顺序逐式搬移；
  唯一「新规则」是 OI 门槛由 `0.0` 变为 `None`（UNSET）—— 这不是改规则，是**停止冒充**
  （旧 `0.0` 让「未批准的门槛」看起来像「已配置但很宽松」）。
- 真实下单**结构性不存在**：`ExchangeFillAdapter` 的 `quote_*` 一律抛
  `LiveExecutionForbidden`；`assert_live_disabled()` 是硬闸。

---

## 3. 改动清单（W2-0 ～ W2-11 逐条）

| 项 | 内容 | 落点 | 状态 |
|---|---|---|---|
| **W2-0** | `oi_resonance_quality` 由**标量整列广播**改为**逐行判定** | `oi_resonance_v1/factors.py` | ✅ 四类标签共存（见第 6 节） |
| **W2-1** | `oi_min_resonance` 由 `0.0` 改为 `None`（UNSET）；无正式批准值不得发明 | `config.py` | ✅ 正式运行门禁恒拒（第 1 条阻塞原因） |
| **W2-2** | 新增统一 `param_hash`，覆盖身份/周期/两层全部参数/权重 | `config.py`（`PARAM_HASH_BASIS` 14 项） | ✅ 33 项参数逐一验证「改即变」 |
| **W2-3** | `close_open_positions_at_end` 显式开关 + 来源标注 | `trading_core/policy.py`、`engine.py` | ✅ `frozen_config` / `runtime_override` 可区分 |
| **W2-4** | 正式 Signal 契约，四时点分离 + 七问齐备 | `trading_core/signal.py` | ✅ 已入注册表 `run()`；**尚未入 `backtest_runner` 信号表**（见第 11 节） |
| **W2-5** | PIT 可得性分层标注（数值可得 ≠ 决策可见） | `trading_core/pit.py`、`core.py` | ✅ 未声明延迟 ⇒ 一律 `pit_proven=False` |
| **W2-6** | `ExecutionPolicy`（只决策）与 `FillAdapter`（成交结果）分离 | `trading_core/policy.py`、`fill.py` | ✅ 三端注入同一份 Policy |
| **W2-7** | 统一 Order 模型 + 7 态状态机 | `trading_core/order.py`、`contracts.py` | ✅ 非法跳转硬拒 |
| **W2-8** | 确定性幂等键（禁 uuid4 / random / 时间+随机） | `contracts.stable_hash` | ✅ 跨进程一致（子进程验证） |
| **W2-9** | Position/Trade/Ledger 接口边界 | `trading_core/position.py` | ✅ `Fill→Position→Trade→LedgerEvent` |
| **W2-10** | Ledger 只定义 12 类事件接口，**不删旧账本** | `trading_core/ledger.py` | ✅ `paper_engine.py` 未被删除 |
| **W2-11** | 旧 Golden 标记 `PREVIOUS_GOLDEN` + 评估是否重生成 | `docs/evidence/backtest_golden_2026-10-16/` | ✅ 已标记；判定「暂不重生成」（第 8 节） |

### 3.1 修改文件（7 个，`+421 / −743`）

```
框架/backend/backtest_runner.py                     |  20 +-
框架/backend/oi_resonance_bridge.py                 |  13 +
策略/WyckoffMaster/oi_resonance_v1/config.py        | 112 +-
策略/WyckoffMaster/oi_resonance_v1/core.py          |  47 +-
策略/WyckoffMaster/oi_resonance_v1/execution.py     | 857 +---（单体 → 驱动层）
策略/WyckoffMaster/oi_resonance_v1/factors.py       |  34 +-
策略/WyckoffMaster/oi_resonance_v1/registry_entry.py|  81 +-
```

> 说明：`factors.py` 的逐行质量修复由**并行施工窗口**（15:43:25）先落地，本窗口保留其实现
> 并逐项复核（未回退）；其余 6 个文件为本窗口改动。并行写者的原始版本已备份为
> `并行写者_execution_20261016_1543.py` / `并行写者_factors_20261016_1543.py`（含 SHA256）。

### 3.2 关键设计决定（有证据、可回退）

1. **执行策略不对数量取整**：既有引擎相位 C 直接使用风控 `qty`，未做 `price_step` 取整
   ⇒ `ExecutionPolicy.decide_entry` 保持不取整，避免引入行为变化。
2. **费率取 `taker_fee_rate`**：既有引擎用 taker ⇒ adapter 与平仓记账均用 taker。
3. **`OpenPosition = Position` 是别名不是副本**：保证既有调用方零改动，不产生第二套持仓模型。
4. **`ExecutionPolicy` 不给成交价**：成交价一律来自注入的 `FillAdapter`（`FillQuote`）。

---

## 4. 新增组件清单

### 4.1 共享核心包 `策略/WyckoffMaster/trading_core/`（12 个模块）

| 模块 | 契约版本 | 职责 |
|---|---|---|
| `contracts.py` | `trading_core_v1.0.0` | 枚举（OrderStatus 7 态 / Side / PositionSide / OrderType / ExecPhase / Liquidity）、错误类型、确定性哈希 |
| `signal.py` | `signal_contract_v1.0.0` | `SignalEvent` 四时点 + 七问；`resolve_trade_time` 与引擎同一规则 |
| `order.py` | `order_contract_v1.0.0` | `OrderIntent` / `Order` / 状态机 / `OrderRegistry`（幂等） |
| `position.py` | `position_contract_v1.0.0` | `Position`（= 原 `OpenPosition`）/ `Trade` / `PositionBook` |
| `risk.py` | `risk_contract_v1.0.0` | `RiskConstraints` / `RiskDecision` / `portfolio_open_risk` / `check_entry` |
| `fill.py` | `fill_contract_v1.0.0` | `FillQuote` / `Fill` / `FillAdapter` / `SimulatedBarFillAdapter` / `ExchangeFillAdapter` |
| `funding.py` | `funding_contract_v1.0.0` | 毫秒口径资金费结算（`settlement_events` / `assumption_fixed_8h`） |
| `ledger.py` | `ledger_contract_v1.0.0` | 12 类事件接口 + 确定性 `event_id` + `InMemoryLedger` |
| `policy.py` | `execution_policy_v1.0.0` | `ExecutionPolicy`（只决策：发不发/何时/多少/类型/退出） |
| `pit.py` | `pit_contract_v1.0.0` | PIT 可得性标注与摘要 |
| `engine.py` | `trading_engine_v1.0.0` | 主循环（相位 A→B→C→D→E→EOD）+ `prep_symbol` |
| `__init__.py` | — | 统一导出 |

### 4.2 测试与证据工具

| 文件 | 作用 |
|---|---|
| `策略/WyckoffMaster/test_work02_core_contract.py` | **15 个指定测试**（第 6 节） |
| `evidence/…/工具_合成夹具.py` | 确定性夹具（seed=20261016） |
| `evidence/…/工具_基线捕获.py` | 改造前完整结果哈希 |
| `evidence/…/工具_行为一致性核验.py` | 剥离新增键后与改造前逐位比对 |
| `evidence/…/工具_Golden差异分类.py` | **W2-11 闸门**：行为差异 vs 身份差异分类 |
| `evidence/…/工具_身份指纹.py` | 全部身份指纹清单（第 7 节） |

---

## 5. 删除 / 废弃清单

| 对象 | 处理 | 说明 |
|---|---|---|
| `oi_resonance_v1/execution.py` 的执行算术 | **搬移**，非删除 | 逐式搬到 `trading_core/*`，原文件保留为驱动层，对外 API 逐字不变 |
| `OpenPosition` | 降为**别名** | `execution.OpenPosition is trading_core.position.Position` |
| `_settle_funding` / `_funding_events_ms` / `_funding_events_by_sec` / `_prep_symbol` / `_close_position` / `_max_dd` / `_apply_trail` / `_empty_result` | **保留兼容入口** | 既有回归测试按这些名字调用，全部仍在 |
| `paper_engine.py` 旧账本 | **一个字节都没删** | W2-10 明文要求；本次只定义事件接口 |
| 旧 `WyckoffMaster v3.1` 核心 | 未触碰 | 仍在 `_legacy_archive_v31/` 逐字节封存 |
| 旧 `3a/3b/3c/3d` | 未恢复、未删除 | `框架/backend/strategies/` 内容未因本次改动增减 |

**没有任何测试被删除、跳过或改成 `skip`。**

---

## 6. 测试结果（命令 / 总数 / PASS / FAIL / ERROR）

解释器：`/Users/boos/.workbuddy-ai/binaries/python/envs/default/bin/python`
（含 duckdb / pyarrow），`PYTHONDONTWRITEBYTECODE=1`。

### 6.1 本次新增：15 个指定用例 —— 全绿

```sh
cd /Users/boos/Desktop/威科夫因子共振趋势
PYTHONDONTWRITEBYTECODE=1 <py> -m pytest 策略/WyckoffMaster/test_work02_core_contract.py -q
# 15 passed in 1.16s
```

| 分组 | 用例 | 结果 |
|---|---|---|
| Strategy | `test_oi_quality_is_row_aligned` | PASS |
| Strategy | `test_oi_threshold_behavior` | PASS |
| Strategy | `test_parameter_hash_changes` | PASS |
| Strategy | `test_future_data_prefix_invariance` | PASS |
| Strategy | `test_pit_timestamp` | PASS |
| Signal | `test_signal_contract_complete` | PASS |
| Signal | `test_signal_available_at` | PASS |
| Signal | `test_signal_deterministic` | PASS |
| Execution | `test_execution_policy_deterministic` | PASS |
| Execution | `test_next_open_execution` | PASS |
| Execution | `test_gap_behavior` | PASS |
| Execution | `test_fee_once` | PASS |
| Execution | `test_slippage_once` | PASS |
| Order | `test_order_idempotency` | PASS |
| Order | `test_duplicate_order_rejected` | PASS |

**合计：15 PASS / 0 FAIL / 0 ERROR**

### 6.2 既有回归套件 —— 零回归（87 PASS / 0 FAIL / 0 ERROR）

命令按 `test_backtest_store.py` **自身 docstring 规定**的方式给出（该测试的
`RUNS_ROOT` 在模块导入期读环境变量，故必须随命令一起提供）：

```sh
cd /Users/boos/Desktop/威科夫因子共振趋势
OI_RESONANCE_RUNS_DIR=/tmp/bt_store_test \
PYTHONDONTWRITEBYTECODE=1 <py> -m pytest \
  evidence/Work_策略替换_2026-10-16/test_oi_resonance_v1_patches.py \
  evidence/Work_单策略回测_2026-10-16/test_oi_resonance_v1_backend_fixes.py \
  evidence/Work_单策略回测_2026-10-16/test_oi_resonance_v1_backend_p0p1.py \
  evidence/Work_单策略回测_2026-10-16/test_backtest_store.py -q
# 87 passed in 1.18s
```

| 套件 | 结果 |
|---|---|
| `test_oi_resonance_v1_patches.py` | 35 passed |
| `test_oi_resonance_v1_backend_fixes.py` | 13 passed |
| `test_oi_resonance_v1_backend_p0p1.py` | 33 passed |
| `test_backtest_store.py` | 6 passed |
| **小计** | **87 PASS / 0 FAIL / 0 ERROR** |

**顺序无关性核验**（不设环境变量，仅把 `test_backtest_store.py` 置首）：
`87 passed in 1.11s` —— 与上式**同为 87/87**，证明结论不依赖命令细节。

**本次两组合计：102 PASS / 0 FAIL / 0 ERROR。**

### 6.2.1 裸命令会得到 `2 failed / 85 passed` —— 已归因，非 WORK-02 引入

不设环境变量、按上表原文件顺序执行时，`test_backtest_store.py` 有 2 项失败：

```
FAILED test_backtest_store.py::test_atomic_exclusive_create_and_duplicate_rejected
FAILED test_backtest_store.py::test_list_runs_reads_docs_and_flags_broken
# 2 failed, 85 passed in 2.04s
```

**成因（既有测试隔离缺陷，与本次改动零因果关系）**：
`backtest_store.py:41` 在**模块导入期**读 `OI_RESONANCE_RUNS_DIR`；
而 `test_oi_resonance_v1_backend_p0p1.py:31` 在模块级先一步 `import backtest_store`，
使 `RUNS_ROOT` 冻结为项目默认目录；随后 `test_backtest_store.py:25` 的
`importlib.import_module("backtest_store")` 命中缓存返回同一对象，其环境变量赋值完全失效
⇒ `RUNS_ROOT != TMP`，两条断言必然失败（测试产物被写进了**生产**回测根）。

**证据链**（完整版：`evidence/Work_策略核心收敛_2026-10-16/验收_既有回归2项失败_归因_2026-10-16.md`）：

| # | 环境变量 | 文件顺序 | 结果 |
|---|---|---|---|
| V1 | 不设 | 原顺序 | 2 failed, 85 passed |
| V2 | 不设 | **store 置首** | **87 passed** |
| V3 | `OI_RESONANCE_RUNS_DIR` 已设 | 原顺序 | **87 passed** |
| V4 | 不设 | 仅 `p0p1` + `store` | 2 failed, 37 passed |

同一批 87 个用例，**只改顺序或只补环境变量**即可 0 失败 ⇒ 与代码状态无关。
且触发链上三个文件（`backtest_store.py` / `p0p1.py` / `test_backtest_store.py`）**相对 HEAD 零改动**
（`git status` 为空），WORK-02 改动模块（`trading_core/*`、`oi_resonance_v1/execution.py`）
**从不导入 `backtest_store`**（仅注释提及），最小复现也**只用未改动模块**即可成立。

**附带损害已封住**：V1/V4 每次会往生产 `回测日志/oi_resonance_v1/runs/` 写 2 个测试目录；
已**原样移入**同级隔离目录 `回测日志/oi_resonance_v1/_隔离_误落测试产物_2026-10-16/`（共 10 个，
可 `mv` 回退，未删除任何文件），生产 runs 根残留 **0**，10:22–10:55 的真实运行记录未触碰。

**建议修复未实施**：改法（在 `importlib.import_module` 后补 `S.RUNS_ROOT = TMP`）属
**他人工作包**文件，且该目录内有并行写者，施工单亦禁止「改测试使其 PASS」⇒ 本窗口不越界改动，
仅登记为遗留项（见第 11 节）。

### 6.3 W2-0 缺陷「现形 → 修复」实证

同一夹具（`工具_合成夹具.py`，`fixture_hash = 57e3003df1c8620f7dd95fb8de7e0263a23ae413bfc661aae929c1abeb0baf03`，
900 根 1h，OI 三段质量）—— **前后对同一夹具**：

| 阶段 | `oi_quality_counts` | 证据文件 |
|---|---|---|
| **改造前**（标量广播缺陷） | `{"partial_insufficient_history": 900}` ← **整列同一个标签** | `基线_改造前.json` |
| **W2-0 修复后** | `{"missing_open_interest": 347, "ok": 281, "insufficient_history": 167, "partial_insufficient_history": 105}` | `基线_W20后.json` |
| **共享核心改造后** | 同上（逐行判定未被重构破坏） | `基线_共享核心后.json` |

四类标签共存 ⇒ 证明「因子可用行 / 历史不足行 / OI 缺失行」不再互相冒充。
（修复前后 `result_sha256` 必然不同，因为**结果体本身包含质量列**；数值行为由
`工具_行为一致性核验.py` 在 W2-0 之后的代码上单独核验，见第 7.1 节。）

新增测试用的是**独立夹具**（`long_frame(600)`，OI 仅前 100 行为 NaN），实测：
`{"missing_open_interest": 100, "insufficient_history": 167, "partial_insufficient_history": 48, "ok": 285}`，
断言「四类共存 + 逐行与可观测事实一致 + 缺失段恰为前 100 行」。

### 6.4 后端全量隔离单测（272 项）—— 33 项非绿，**全部为既有环境问题**

```sh
cd 框架/backend && POOL_RUNTIME_PY=<py> sh run_tests.sh
# Ran 272 tests — FAILED (failures=1, errors=32, skipped=33)
```

逐项归因（**无一项与本次改动相关**）：

| 根因 | 项数 | 性质 |
|---|---|---|
| `FileNotFoundError: 数据/研究输入/外部迁移_2026-10-16/OKX成交量…` | 16 | 该批 OKX 修复脚本**未随工程迁移** |
| `ModuleNotFoundError: bulk_import` | 5 | 未随工程迁移的模块 |
| `ModuleNotFoundError: _pool_restfill` / `_pool_gate` / `okx_download` | 6 | 同上 |
| `sqlite3.IntegrityError: signals.strategy_version` | 2 | 冻结生效视图丢失 ⇒ 版本号取不到（项目已知丢失项） |
| `KeyError: 'required_config'` | 1 | 同上 |
| `test_strategies_pending_config_and_no_third` | 1 | 断言注册表**不含** `wx_oi_resonance_v1`，但该键已在 HEAD 提交中注册（`strategies/registry.py` 未改动） |
| 其余导入失败 | 2 | 依赖缺失 |

**证明**：失败测试模块**无一** `import` `oi_resonance_v1` / `trading_core` / `WyckoffMaster`；
`strategies/registry.py` 相对 HEAD **零改动**（新策略注册是既有提交内容，非本次引入）。

### 6.5 旧 v3.1 验收套件 —— 13/14，E11 为**既有失败**

```sh
cd 策略 && PYTHONDONTWRITEBYTECODE=1 <py> -m WyckoffMaster.test_execution
# all_passed = False（仅 E11_append_future_data_keeps_past）
```

- **2026-10-15 记录**（`evidence/Work_执行核心_2026-10-15/01_execution_core_checks.json`）：
  `E11 → true`。
- **2026-10-16 状态核查报告**（`docs/系统状态核查报告_2026-10-16.md` L105 / L221）已记为失败，
  诊断结论：**「用例需把『段末强制平仓』决定排除在前缀比对之外」**。
- 本次复核：E11 仍失败，`n_past_full=2, n_prefix=2`，差异发生在**段末强平**那一笔。
- 该套件只调用 `WyckoffMaster.execution`（v3.1）+ `core` + `params`，
  与本次改动的模块**无 import 关系** ⇒ **不是本次引入**。

**关键**：该报告指出的修法，正是 **W2-3** 提供的机制 —— 显式
`close_open_positions_at_end=False`。本次新增的
`test_future_data_prefix_invariance` **就是按此机制**做的因果比对，结果 **0 漂移**。
即：新核心已具备该能力；旧 v3.1 套件仍用它自己的老口径（不在本次施工范围内，未改）。

---

## 7. Hash 证据

```sh
PYTHONDONTWRITEBYTECODE=1 <py> \
  evidence/Work_策略核心收敛_2026-10-16/工具_身份指纹.py \
  --json evidence/Work_策略核心收敛_2026-10-16/身份指纹_2026-10-16.json
```

| 指纹 | 值 |
|---|---|
| `strategy_id` / `strategy_key` | `WX_OI_RESONANCE_V1` / `wx_oi_resonance_v1` |
| `signal_version` | `oi_res_v1.0.0` |
| `core_version` | `oi_res_core_v1.0.0` |
| `exec_core_version` | `oi_res_exec_v1.0.0` |
| `experiment_id` | `exp_oi_resonance_v1_2026-10-16` |
| `signal_params_hash`（24 字段） | `5cc0457b7d0913bf7bb896021276369895fe3eef944702852f542917b72c12db` |
| `exec_params_hash`（15 字段） | `a92639c0a2b51e3df59e1d7fc78362611fcc91f0a97ba5310c06f0f24e5b99a5` |
| **`param_hash`（W2-2）** | `91390a66484495b81098d3963722c6d6534266e5c28b8c512931835638d9edc3` |
| **`code_sha256`**（六维绑定源码包） | `a40299a39ef2e26312766ce9a76e6d9c0b2a111a5a6f4665fb52812320523f01` |

`param_hash_basis`（14 项，逐项对号）：`strategy_key` / `strategy_id` /
`strategy_version(signal_version)` / `core_version` / `exec_core_version` /
`experiment_id` / `timeframe` / `exec_timeframe` / `exchange` / `market` / `direction` /
`signal_params.*` / `exec_params.*` / `oi_weights`。

**契约版本**：`trading_core_v1.0.0`、`signal_contract_v1.0.0`、`order_contract_v1.0.0`、
`fill_contract_v1.0.0`、`execution_policy_v1.0.0`、`risk_contract_v1.0.0`、
`position_contract_v1.0.0`、`ledger_contract_v1.0.0`、`funding_contract_v1.0.0`、
`pit_contract_v1.0.0`、`trading_engine_v1.0.0`。

### 7.1 行为保持验证（改造前 vs 改造后）

```sh
cd evidence/Work_策略核心收敛_2026-10-16 && PYTHONDONTWRITEBYTECODE=1 <py> 工具_行为一致性核验.py
```

```
改造前 result_sha256（完整结果体）: ed62645f623faa1250477472b41e718fbd7f5897e7a31ff04a4f76647790bbfe
改造后 剥离新增键后 hash         : ed62645f623faa1250477472b41e718fbd7f5897e7a31ff04a4f76647790bbfe
剥离掉的新增键                   : ['close_open_positions_at_end', 'close_open_positions_at_end_source', 'trading_core']
结果: PASS —— 剥离新增键后与改造前逐位一致
```

关键指标（同一合成夹具）：trades 50 / net_return 8.354876% / final_equity 108354.876 /
maxDD 10.0405% / fees 4062.99 / funding 340.13 / decisions 100 / rejected 68 / equity 3600。

**契约产物（新增，不参与行为判据）**：`orders_count=100`、`registry_unique_keys=100`、
`ledger_events_total=660`，分布
`{FEE_CHARGED:100, FUNDING_FEE:41, GENESIS_INIT:1, ORDER_CREATED:100, ORDER_FILLED:100,
ORDER_SUBMITTED:100, POSITION_CLOSED:50, POSITION_OPENED:50, SIGNAL_ACCEPTED:50,
SIGNAL_REJECTED:68}`，`fill_adapter.kind=simulated_bar`。

---

## 8. Golden 影响（W2-11）

### 8.1 结论

```sh
PYTHONDONTWRITEBYTECODE=1 <py> evidence/Work_策略核心收敛_2026-10-16/工具_Golden差异分类.py
# 结果: PASS —— 行为判据零差异；差异**全部**为身份哈希
```

| 判据 | 结果 |
|---|---|
| 核心判据 DIFF 总数 | 4 项（去重后 **2 项**） |
| **行为类差异（必须 0）** | **0 项** ← 窗口/权益/费用/资金费/回撤/计数/逐笔内容全一致 |
| 身份类差异 | 2 项：`code_sha256`、`signal_params_hash` |
| 已知口径差 NOTE | 27 项（run-scoped ID / 窗口末端，不判失败） |

### 8.2 `signal_params_hash` 变化的**逐位归因证明**

把当前身份块**去掉** `oi_publication_lag_ms`、并把 `oi_min_resonance` 改回 `0.0`，
用同一个 `SignalParams.hash()` 算法重算：

```
⇒ 0ba4b7918356b66415416504ae63f9f99786c73ee410fcc69182e9678aee21db
   == golden_reference.json 的 strategy.signal_params_hash（逐位相同）
```

⇒ 该哈希的变化**唯一**来自 W2-1（`0.0 → None`）与 W2-5（新增 `oi_publication_lag_ms` 字段），
**无第三处参数漂移**。

**为什么这不改变行为**（三态语义等价）：

| 配置 | `oi_gate_enabled` | 是否影响回测信号 |
|---|---|---|
| 旧 `oi_min_resonance = 0.0` | `False` | 否 |
| 新 `oi_min_resonance = None`（UNSET） | `False` | 否 |
| 新 `oi_publication_lag_ms = None`（未声明） | 逐 bar 不改规则 | 否（仅运行级拒绝正式生产） |

### 8.3 处理：降级标记，暂不重生成

- 已写 `docs/evidence/backtest_golden_2026-10-16/PREVIOUS_GOLDEN.md`：
  **加性标记**，`golden_reference.json` / `ledger_summary.json` **一个字节都没改**。
- **判定：暂不重新生成。** 理由：① 收敛系列未结束，现在生成会立刻再次失效（移动靶）；
  ② 旧基线的**行为判据仍完全有效**，已由分类闸门转成可执行的「行为 0 差异」绿灯；
  ③ 生成新 Golden 属**控制基线重置**，须由施工方批准，不得由施工窗口自行宣布。
- 推荐时机与步骤（已写入 `PREVIOUS_GOLDEN.md` 第 4 节）：收敛系列**最后一个 Work** 收尾时，
  一次性生成 + 用同一闸门做「身份 0 差异 + 行为 0 差异」双零核验。

---

## 9. 行为保持与因果性的独立验证

除第 7.1 节的整跑比对，另有两条独立证据：

1. **信号层前缀不变性**（`test_future_data_prefix_invariance`）：
   600 根 → 追加 300 根**不同**未来数据后，18 个因果列（含 `oi_resonance_score`、
   三个分位、`dc_*_prev`、`exit_*_curr/prev`、`confirm_time`）前 600 行**逐位相同**。
2. **执行层前缀不变性**：`close_open_positions_at_end=False` 下，2400 点权益曲线
   （`cash_equity` / `unrealized_pnl` / `total_equity` / `open_risk_pct` /
   `open_positions_count`）**逐点相同**，成交明细为前缀关系。

> 夹具自证：测试先断言 600 行夹具与 900 行夹具的**前 600 行逐位相同**，
> 否则比对无鉴别力（防止「夹具本身就不同」造成的假绿）。

---

## 10. 决策点（需施工方拍板，本窗口不自行裁决）

### D-1｜PIT 未声明时，逐 bar 是否也要 fail-closed？

- **施工单原文（W2-5）**：「无法证明则**标记**不可用」—— 是**标记**，不是**改策略规则**。
- **本窗口选择**：逐 bar **只标记**（`oi_pit_proven=False` / `oi_decision_usable=False` /
  `oi_pit_reason`），**运行级**由 `formal_run_gate()` 拒绝正式生产。
  理由：① 忠实于原文；② 不在未经批准时收紧策略规则；③ 保持既有 35 项回归全绿
  （曾有「逐 bar 一律 fail-closed」的版本，撞破既有用例
  `TestP2FreshBreakoutAnchor::test_fresh_breakout_registered_before_oi_gate_and_pullback_fires`，
  已按纪律回退，未改测试）。
- **备选**：逐 bar 也 fail-closed（更严格，但属**策略规则变更**，且会让
  「门槛已设 + 延迟未声明」的配置**产出 0 信号**）。
- **需拍板**：是否要把逐 bar 也收紧？若收紧，属改策略规则，需单独施工单 + 证据。

### D-2｜`oi_min_resonance` 的正式批准值（生产阻塞项）

当前 `UNSET` ⇒ `formal_run_allowed = False`（施工单 W2-1 要求）。
**这不是缺陷，是等待批准。** 需业务方给出经批准的生产阈值，否则正式生产永远不启动。

### D-3｜Golden 重新生成的时机与批准

见第 8.3 节。建议收敛系列末次 Work 统一执行；需施工方批准。

### D-4｜`available_at` / `trade_time` 是否本轮就写入 `backtest_runner` 信号表？

当前 Signal 契约已在注册表 `run()` 产出；`backtest_runner` 的信号表**尚未**写
`available_at`。本轮未改（避免扩大改动面 + 影响 Golden 输入快照）。

---

## 11. 已知未完成项 / 遗留缺口（如实披露）

| 项 | 现状 | 影响 |
|---|---|---|
| W2-4 `available_at` / `trade_time` 入账 | `backtest_runner` 信号表未写 | 回测产物里四时点只有 `confirm_ms`；契约层已齐备 |
| W2-5 `oi_observed_at` 列 | `annotate_oi_pit` 支持，但 `load_inputs` 尚未产出 | 当前退回 `timestamp + bar_seconds`（最乐观假设），故**必须**声明延迟才放行 |
| W2-10 Ledger 落盘迁移 | 只定义事件接口 + `InMemoryLedger` | 明文要求「本阶段不做大规模迁移」 |
| Live 端接线 | `ExchangeFillAdapter` 已就位，无交易所客户端 | 结构性禁止按 K 线假算；`LiveExecutionForbidden` 硬闸 |
| Paper 端接线 | `SimulatedBarFillAdapter` 可复用 | 下一施工单 |
| 后端套件 33 项非绿 | 既有环境问题（缺外部模块/目录、视图丢失） | 属 W1 路径迁移范围，非本次 |
| 旧 v3.1 E11 | 既有失败，2026-10-16 报告已记录 | 新核心已用 W2-3 机制正确实现该性质 |
| `test_backtest_store.py` **顺序依赖**（2 项） | 既有测试隔离缺陷：`RUNS_ROOT` 在导入期读 env，`importlib` 命中缓存 ⇒ 原顺序 + 不设 env 时 2 项失败 | **非本次引入**（见第 6.2.1 节归因证据）；按该测试 docstring 命令或任意「store 置首」顺序均 87/87。改法属他人工作包，未越界实施 |
| 误落生产 runs 根的测试产物 | 已原样移入同级隔离目录（10 个，可 `mv` 回退） | 生产 `runs/` 残留 0；真实运行记录未触碰 |

---

## 12. 禁止事项合规自检（逐条）

| 禁止项 | 自检 |
|---|---|
| 恢复 `_warmup43` | ✅ 未恢复（`git status` 无该文件） |
| 恢复旧 `3a/3b/3c/3d` | ✅ 未恢复；`框架/backend/strategies/` 内容未因本次增减 |
| 改旧 `exec_bridge` | ✅ `git diff --quiet 框架/backend/exec_bridge.py` ⇒ 未改动 |
| 接真实 Binance 下单 | ✅ 无任何下单/联网路径；`assert_live_disabled()` 硬闸 |
| 启动 Live | ✅ 未启动；`live_order_enabled` 恒 `False` 且校验强制 |
| 改 React UI | ✅ 未触碰 `UI界面/` |
| 重写数据层 | ✅ 未触碰 `数据/`、`market_data` 读取逻辑 |
| 制造兼容假模块 | ✅ `trading_core` 全部为正式实现；`OpenPosition` 是**别名**（同一类对象），非副本 |
| 伪造历史账本 | ✅ 未生成任何账本；旧账本文件未创建/未修改 |
| 改策略规则不留证据 | ✅ 唯一规则语义变化（UNSET）已在第 3 节 + 第 8.2 节逐位举证 |
| 删除失败测试 | ✅ 零删除、零 skip |
| 修改 Golden 使其 PASS | ✅ `golden_reference.json` 字节未变（仅新增 `PREVIOUS_GOLDEN.md` 标记） |
| 为测试制造 fake / mock engine | ✅ 无 mock；测试用真实 `TradingCore` + 真实 `SimulatedBarFillAdapter` + 合成行情 |

**附带**：`paper_engine.py` 与 `strategies/wx_donchian_wyckoff.py` 相对 HEAD 有改动，
但均为**并行窗口 W1 的外部路径改写**（diff 内含 `# W1（2026-10-16）` 注释），
非本次 WORK-02 所为，且**未删除任何账本代码**。

---

## 13. 目标状态图（当前 vs 完成后）

```
                    ┌──────────────────────────────────────────────┐
                    │  Strategy  WX_OI_RESONANCE_V1                │
                    │  core.compute_signals（唯一一份）             │
                    └───────────────┬──────────────────────────────┘
                                    │ SignalEvent（四时点 + 七问）
                                    ▼
                    ┌──────────────────────────────────────────────┐
                    │  Risk  check_entry / portfolio_open_risk      │
                    └───────────────┬──────────────────────────────┘
                                    │ RiskDecision
                                    ▼
                    ┌──────────────────────────────────────────────┐
                    │  Order  OrderIntent → Order（7 态状态机）      │
                    │         确定性 idempotency_key（禁 uuid/random）│
                    └───────────────┬──────────────────────────────┘
                                    │
                    ┌───────────────▼──────────────────────────────┐
                    │  ExecutionPolicy（只决策，三端同一份）         │
                    └───────────────┬──────────────────────────────┘
                                    │
              ┌─────────────────────┴─────────────────────┐
              ▼                                           ▼
   SimulatedBarFillAdapter                     ExchangeFillAdapter
   （Backtest / Paper）                        （Live；quote_* 抛硬闸）
              │                                           │
              └─────────────────────┬─────────────────────┘
                                    │ Fill
                                    ▼
                    ┌──────────────────────────────────────────────┐
                    │  Position → Trade → LedgerEvent（12 类）      │
                    └──────────────────────────────────────────────┘
```

**当前**：Backtest 端已完整走通上图（102 项测试全绿，行为逐位保持）。
**Paper / Live 端**：核心件已就位（同一 Policy + 可注入 Adapter），**接线未做**（下一施工单）。

---

## 14. 停工声明

本次 WORK-02 到此**停止施工**，等待下一施工单。未获批准不做：
Golden 重生成、Paper/Live 接线、`available_at` 入账、逐 bar PIT fail-closed、
任何策略规则变更。

**复跑全部证据的命令**（一次性全量复验的实跑输出已落盘：
`evidence/Work_策略核心收敛_2026-10-16/验收_最终全量复验_2026-10-16.txt`）：

```sh
cd /Users/boos/Desktop/威科夫因子共振趋势
PY=/Users/boos/.workbuddy-ai/binaries/python/envs/default/bin/python
export PYTHONDONTWRITEBYTECODE=1

# 1) 15 个指定测试                                      → 15 passed
$PY -m pytest 策略/WyckoffMaster/test_work02_core_contract.py -q

# 2) 既有回归（零回归）                                  → 87 passed
#    注意：必须带 OI_RESONANCE_RUNS_DIR（该测试 docstring 已规定），
#    否则 test_backtest_store.py 会因既有「导入期读 env」缺陷报 2 项失败
#    —— 归因见第 6.2.1 节，与本次改动无关。
OI_RESONANCE_RUNS_DIR=/tmp/bt_store_test \
$PY -m pytest evidence/Work_策略替换_2026-10-16/test_oi_resonance_v1_patches.py \
               evidence/Work_单策略回测_2026-10-16/test_oi_resonance_v1_backend_fixes.py \
               evidence/Work_单策略回测_2026-10-16/test_oi_resonance_v1_backend_p0p1.py \
               evidence/Work_单策略回测_2026-10-16/test_backtest_store.py -q

# 2b) 顺序无关性核验（不设 env，store 置首）             → 87 passed
$PY -m pytest evidence/Work_单策略回测_2026-10-16/test_backtest_store.py \
               evidence/Work_策略替换_2026-10-16/test_oi_resonance_v1_patches.py \
               evidence/Work_单策略回测_2026-10-16/test_oi_resonance_v1_backend_fixes.py \
               evidence/Work_单策略回测_2026-10-16/test_oi_resonance_v1_backend_p0p1.py -q

# 3) 行为保持                                            → PASS（逐位一致）
$PY evidence/Work_策略核心收敛_2026-10-16/工具_行为一致性核验.py

# 4) Golden 差异分类（退出码 0 = 行为零差异）            → PASS，行为类 0 项
$PY evidence/Work_策略核心收敛_2026-10-16/工具_Golden差异分类.py; echo "exit=$?"

# 5) 身份指纹
$PY evidence/Work_策略核心收敛_2026-10-16/工具_身份指纹.py
```
