# Live 断点诊断（只做诊断，不伪造冻结模块）

**诊断时间**：2026-10-16 14:58–15:20（UTC+8）
**诊断范围**：`live_worker.py`、`live_pool.py`、`live_signal_engine.py`、`live_api.py`、`live_store.py`、`strategies/wx_*`
**本阶段约束（已遵守）**：未创建任何同名空模块或兼容壳、未修改任何业务代码、未删除任何文件。

---

## 0. 结论速览

| 问题 | 结论 |
|---|---|
| Live 当前能否启动 | **不能**。`live_worker.py` L42 顶层 `import _warmup43` 直接抛 `ModuleNotFoundError` |
| 缺失模块的真实功能 | **实时链路的「数据装配 + 结构确认 + 提示装配」三件事** —— 详见 §1 |
| Live 应恢复旧模块还是迁移 | **迁移到正式架构**（§6 给出逐模块迁移目标） |
| 冻结模块能否靠"补文件"解决 | ❌ **不能**。它们是旧研究窗口的产物，路径已随工作区删除；重建即伪造 |

---

## 1. 五个缺失模块逐个分析

### 1.1 `_warmup43` —— 影响面最大，**Live 的硬阻塞**

| 问题 | 回答 |
|---|---|
| **谁 import？** | ① `live_worker.py` **L42（顶层，裸 import，无 try/except）**；② `live_signal_engine.py` L60（`try/except` 包裹，失败置 `W = None`） |
| **谁调用？** | `live_signal_engine.py`：L71–73（`W.MARKET_DB` 路径对齐）、**L430 `W.open_ro()`**、L442 `W.open_ro()`、**L453 `W.assemble(sym, T, warm_hours, window_hours, lookback_hours, con)`**、**L456 `W.structural_confirms_live(sym, T, bars_pack=pack)`**、L516 `_sha256_file(W43 / '_warmup43.py')`（规则身份哈希） |
| **调用目的** | ① 打开行情库**只读**连接；② 为每个币装配「1h 信号 bar 包」（预热段 + 窗口段）；③ 跑 **39 原型冻结状态机**做结构确认（Spring / SC）；④ 参与规则身份哈希 |
| **输入** | symbol、当前时刻 T、`warm_hours`（= `ASSEMBLY_LOOKBACK_HOURS`）、`window_hours`（=168）、`lookback_hours`、只读连接 |
| **输出** | ① 1h bar 包（`pack`，`pack[0]` 是 bars）；② 结构确认列表 `conf` + 诊断 `sdiag`（含 `bars`、`warmup_satisfied`） |
| **是否属于正式生产逻辑？** | **是**。它是实时信号引擎的**数据装配 + 结构识别**核心，不是辅助工具 |
| **是否只是旧研究阶段辅助模块？** | 否。功能是生产的，**只是实现载体（模块文件）来自旧研究窗口** |
| **真实功能应归属哪一层？** | **Data 层（装配）+ Strategy 层（结构确认）**：<br>· `W.open_ro()` → 已被 `MarketDataRepository` / `live_store.market_conn()` 覆盖<br>· `W.assemble()` → 应迁到 **Data 层**（统一 bar 装配器，供 Backtest/Paper/Live 共用）<br>· `W.structural_confirms_live()` → 应迁到 **Strategy 层**（`oi_resonance_v1` 或 `trading_core` 的结构确认器） |

**注意**：`live_signal_engine.py` 用 `try/except` 把 import 失败降级成 `W = None`，
但 **L430 起的 `W.open_ro()` 没有任何 `None` 保护** ⇒ 调用时必然
`AttributeError: 'NoneType' object has no attribute 'open_ro'`。
即 `live_signal_engine.run_cycle()` **实际不可用**，只是"导入不报错"而已。

### 1.2 `_接入43` —— 提示装配

| 问题 | 回答 |
|---|---|
| **谁 import？** | `live_signal_engine.py` L59（`try/except` 包裹，失败置 `I = None`） |
| **谁调用？** | L537 `I.make_prompts(...)`、L602 `I.summarize_prompts(prompts_all)` |
| **调用目的** | 把结构确认装配成**可执行提示**（绑定 signal UID / 准入状态 / 版本），并做汇总 |
| **输入** | 结构确认列表 + 币种 + 准入结果 + 版本标识 |
| **输出** | 提示列表（展示态与可执行态分列）+ 汇总统计 |
| **是否属于正式生产逻辑？** | **是**（它是信号到执行的桥） |
| **是否只是旧研究阶段辅助模块？** | 否（功能是生产的） |
| **真实功能应归属哪一层？** | **Signal 层**：应迁为 `signal_engine` 的「信号事件规范化」——把结构确认转成统一信号事件（标的/方向/强度/时戳），与回测 `oi_resonance_v1.evaluate()` 输出同构 |
| **额外发现** | L195 注释记录了 `_接入43.make_prompts` 的一个**已核实缺陷**：其循环 `for c in struct_confirms:` 逻辑有问题。⇒ **即使找回该模块，这段也要重写**，不能原样复用 |

### 1.3 `_pool_scope` —— 币池口径（**有 fallback，实际可用**）

| 问题 | 回答 |
|---|---|
| **谁 import？** | `live_pool.py` L79（`try/except` + **`_FallbackPoolScope` 兜底类**） |
| **谁调用？** | `live_pool.py`：L234 `PS.binance_scope_symbols()`、L235 `PS.scope_meta()`、L239/L275/L277 `PS.SCOPE_JSON` |
| **调用目的** | 提供币安加密币**候选集合（526 个）**与范围元数据 |
| **输入** | 无（读 `SCOPE_JSON` 文件） |
| **输出** | `symbols` 列表 + `meta`（`pool_source` / `n` / 排除的指数） |
| **是否属于正式生产逻辑？** | **是**（币池口径是准入的前提） |
| **是否只是旧研究阶段辅助模块？** | 否（功能是生产的） |
| **真实功能应归属哪一层？** | **Data 层**（标的池口径） |
| **重要：实际状态是"已降级可用"** | `live_pool.py` L81–L95 的 fallback 指向 `数据/contracts/旧标的池/scope_crypto.json`，**该文件存在**（219,604 字节，2026-10-08）。⇒ 币池功能**当前不阻塞**，但 `pool_source` 会标成 `scope_crypto_fallback` 而非 `scope_crypto_v1` |

### 1.4 `_pool_restfill` —— 历史 K 线 REST 补缺导入器（**非生产路径**）

| 问题 | 回答 |
|---|---|
| **谁 import？** | 仅**测试**：`tests/test_history_integrity.py` L36、`tests/test_rest_import_contract.py` L41、`tests/test_okx_volume_mapping.py` L46 |
| **谁调用？** | 测试内以子进程方式跑 `logs/_pool_restfill.py --tf 12h --exchange binance --local-only`（`test_history_integrity.py` L244、`test_pool_gate.py` L376）；核心函数 `import_rest`（`test_okx_volume_mapping.py` L5、`test_rest_import_contract.py` L5） |
| **调用目的** | 把交易所 REST 拉到的 K 线**幂等导入**行情库；提供 `--local-only` 覆盖度门禁返回码 |
| **输入** | `--tf` / `--exchange` / `--local-only` 等参数；REST 行数据 |
| **输出** | 落库行 + 返回码（0=本地已齐 / 4=待范围证据 / 非零=缺行） |
| **是否属于正式生产逻辑？** | **否**。它是**离线数据补缺工具**，不参与交易链路 |
| **是否只是旧研究阶段辅助模块？** | 是（数据工程工具） |
| **真实功能应归属哪一层？** | **Data 层（离线工具）**。当前工程已有等价物：`evidence/Work_补缺落库_2026-10-16/工具_冷层追加补缺.py`、`工具_官方归档下载.py`、`工具_缺口复检.py` ⇒ **不需要恢复旧模块**，把测试重定向到新工具即可 |

### 1.5 `_pool_gate` —— 池覆盖门禁（**非生产路径**）

| 问题 | 回答 |
|---|---|
| **谁 import？** | 仅**测试**：`tests/test_pool_gate.py` L37 |
| **谁调用？** | `test_pool_gate.py` L178 `load_exemptions()`、L179 `load_identity()`、L184 `evaluate_tf()` |
| **调用目的** | 判定「某周期 × 某币池」的 K 线覆盖是否达标（头段/内部/尾段三段判据 + 周期绑定豁免） |
| **输入** | 只读连接、时间周期、目标币列表、豁免表、身份表 |
| **输出** | 逐周期通过/阻塞结论（未决 ⇒ 返回码 4） |
| **是否属于正式生产逻辑？** | **否**（验收门禁，非交易链路） |
| **是否只是旧研究阶段辅助模块？** | 是（验收工具） |
| **真实功能应归属哪一层？** | **Data 层（验收器）**。当前工程已有等价物：`evidence/Work_补缺落库_2026-10-16/工具_缺口复检.py` + `官方缺席白名单_2026-10-16.csv`（据用户口径已"升级验收工具实现白名单自动排除"）⇒ **不需要恢复旧模块** |

### 1.6 另外两个（测试中出现的缺失模块）

| 模块 | 引用位置 | 功能 | 归属 | 是否生产 |
|---|---|---|---|---|
| `bulk_import` | `tests/test_business.py` L1261/1485/1513/1519/1558/1570 | `import_zip_streaming()`：官方归档 ZIP 流式两遍导入（低内存） | Data 层（离线导入器） | ❌ 非生产 |
| `okx_download` | `tests/test_business.py` L701 | `pool_items()` / `pool_sync()`：欧易标的池同步 | Data 层（离线工具） | ❌ 非生产 |

---

## 2. 五个模块的共性判断

| 模块 | 阻塞 Live？ | 是否生产逻辑？ | 当前是否可用 | 应恢复？ | 应迁移到 |
|---|---|---|---|---|---|
| `_warmup43` | **✅ 硬阻塞** | ✅ 是 | ❌ 不可用 | ❌ 不恢复 | Data 层（装配）+ Strategy 层（结构确认） |
| `_接入43` | 🟡 阻塞（被上一条挡住） | ✅ 是 | ❌ 不可用 | ❌ 不恢复（**且有已知缺陷，须重写**） | Signal 层（信号事件规范化） |
| `_pool_scope` | ❌ 不阻塞 | ✅ 是 | ✅ **降级可用**（fallback 文件存在） | ❌ 不恢复 | Data 层（标的池口径） |
| `_pool_restfill` | ❌ 不阻塞 | ❌ 否 | — | ❌ 不恢复 | Data 层离线工具（已有新实现） |
| `_pool_gate` | ❌ 不阻塞 | ❌ 否 | — | ❌ 不恢复 | Data 层验收器（已有新实现） |

> **一句话**：真正卡住 Live 的只有 **`_warmup43`（+ 顺带的 `_接入43`）**，
> 而这两个的功能**都属于正式生产逻辑**，只是实现载体来自旧研究窗口。
> 另外三个是**离线数据工具**，与交易链路无关，**不应为了"让 import 成功"而恢复**。

---

## 3. 为什么不能靠"补同名文件"解决

1. **文件已不存在**：全盘搜索（工程内 + `/Users/boos` 深度 9）**未找到**
   `_warmup43.py` / `_接入43.py` / `_pool_scope.py` / `_pool_restfill.py` / `_pool_gate.py`
   任何一个。它们原本在 `/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/`
   下（`live_worker.py` L27–L31 的 `BASE`/`W43`/`W37`），该目录**已整体删除**。
2. **造一个空壳 = 伪造**：`_warmup43.structural_confirms_live` 是 39 原型冻结状态机，
   其行为直接决定信号是否成立。空壳会让**所有币都"无结构确认"或"乱确认"**，
   产生看似正常实则全错的信号。用户明令禁止。
3. **官方核验脚本已如实报缺**：`verify_exec_wiring.py` 输出
   `status: "blocked_missing_frozen_modules"`，并明确「本脚本不伪造替身」。

---

## 4. 外部依赖最终定位

扫描范围：`框架/`、`策略/`、`UI界面/web/src/`。

### 4.1 `WorkBuddy` / `/Users/boos/WorkBuddy`（外部旧工作区）

| 当前文件 | 行号 | 用途 | 是否生产路径 | 最终处理方案 |
|---|---|---|---|---|
| `live_worker.py` | **L27–L31** | `BASE`/`BACKEND`/`ROOT`/`W37`/`W43` → `sys.path` 注入 | ✅ **生产**（启动即用） | 删除；冻结件功能迁入正式组件（§6） |
| `live_signal_engine.py` | **L43–L50** | 同上 `sys.path` 注入 | ✅ 生产 | 同上 |
| `live_signal_engine.py` | L516 | `_sha256_file(W43 / '_warmup43.py')` | ✅ 生产（规则身份） | 改为对**迁移后的正式模块**取哈希 |
| `live_signal_engine.py` | L646 | `W43 / "12_实时结构提示_43.jsonl"` | 🟡 CLI 调试入口 | 删除或改为本地路径 |
| `live_pool.py` | L72–L75 | `BACKEND = .../网站逆向研究/后端实现` → `sys.path` | ✅ 生产 | 删除（工程内已有等价模块） |
| `live_pool.py` | **L114–L117** | `FROZEN_VIEW` = `…/10_研究参数生效视图_v2_2_user5000_shared.json` | ✅ 生产（**冻结参数唯一来源**） | **已失效** → 现走 `_VIEW_FALLBACK` 并标注；应迁入 `数据/` 或策略包 profiles |
| `paper_engine.py` | **L138–L141** | `EFFECTIVE_VIEW` = 同上视图 | ✅ 生产 | **已失效** → 现走 `FALLBACK`（保守真值）；同上 |
| `paper_engine.py` | **L274–L276** | `CONTRACT_SPEC_CSV` = `…/07_526币保守执行近似规格.csv` | ✅ 生产 | ⚠ **该路径当前仍可达**（`.codex/worktrees/c9d7` 未被删）→ 高危；应迁入 `数据/contracts/` |
| `strategies/wx_donchian_wyckoff.py` | L42 | `EFFECTIVE_VIEW_PATH` | ✅ 生产 | 同上 |
| `strategies/wx_daily_375.py` | L55 | `BACKEND = …/后端实现` | ✅ 生产 | 删除 |
| `strategies/wx_config.py` | L264 | `"source"` 字段填旧路径 | ✅ 生产（仅标注） | 改为相对/环境变量 |
| `strategy_runner.py` | L17, L38, L39 | `PY`（外部解释器）、`DEFAULT_DB`（旧 `market.sqlite3`）、`PROJECT` | 🟡 未接线 | 改用 `sys.executable` + `MARKET_DB` |
| `adapter.py` | L60, L67 | 旧库/旧工程**候选回退路径** | 🟡 回退项 | 删除回退项 |
| `migrate.py` | L51 | `DEFAULT_DB`（旧工程路径） | 🟡 迁移工具 | 改环境变量 |
| `public_market_read.py` | L32 | `DATA_HUB_PROJECT` 默认值 | 🟡 兜底 | 改 `Path(__file__)` 推导 |
| `smart_money.py` | L70 | 同上 | 🟡 兜底 | 同上 |
| `live_api.py` | L82, L108 | **仅注释文本**（说明视图随旧工作区丢失） | ⚪ 非路径 | 保留（是如实说明） |
| `tests/` 共 **9 个文件** | L26–L45 | `PROJ` / `PROJECT` / `FIXDIR` 夹具指向旧工程 | 🔴 测试 | 重定向到本工程 |

### 4.2 `.codex` / `worktree`

| 当前文件 | 行号 | 用途 | 是否生产路径 | 最终处理方案 |
|---|---|---|---|---|
| `paper_engine.py` | **L274–L276** | 合约规格 CSV（`.codex/worktrees/c9d7/…`） | ✅ 生产（**仍可达**） | **优先处理**：迁入 `数据/contracts/` |
| `strategies/wx_donchian_wyckoff.py` | L46, L48 | `SPEC_V2_PATH` / `PARAM_V2_PATH` | ✅ 生产 | 迁入工程内 `数据/` |
| `strategies/wx_daily_375.py` | L40, L42 | `DECISION_DIR` / `AUDIT_DIR` | ✅ 生产 | 迁入工程内 |
| `strategies/wx_config.py` | L286 | `APPROX_375_DECISION_DIR` | ✅ 生产 | 同上 |

> **实测**：`/Users/boos/.codex/worktrees/` 目录**仍然存在**（含 `c9d7` 等多个 worktree），
> 所以这几处**目前不报错**——是**静默生效的高危幽灵依赖**（一旦 worktree 被清理即全断）。

### 4.3 旧 `market.sqlite3`

| 当前文件 | 行号 | 用途 | 是否生产路径 | 最终处理方案 |
|---|---|---|---|---|
| `strategy_runner.py` | **L38** | `DEFAULT_DB = "…/data/market.sqlite3"` | 🟡 未接线 | 改为 `MARKET_DB` 环境变量 |
| `migrate.py` | L51 | 已是 `market_v2.sqlite3`（旧库已退役） | ⚪ 注释说明 | 无需改 |
| `history_lifecycle.py` | L13 | 注释 | ⚪ | 无需改 |
| `public_snapshot.py` | L10 | 注释 | ⚪ | 无需改 |
| `README.md` | L10, L132 | 文档 | ⚪ | 文档更新 |
| `交接状态_2026-10-05.md` | 多处 | 历史交接文档 | ⚪ | 保留（历史留档） |

> 结论：旧 `market.sqlite3` **只在 `strategy_runner.py` L38 一处是活代码**，其余都是注释/文档。
> 现役库是 `数据/市场数据/db/market_v2.sqlite3`（已由 `start.sh` / `collector.sh` / `live_store.py` 统一走 `MARKET_DB`）。

### 4.4 `exec_bridge`

| 当前文件 | 行号 | 用途 | 是否生产路径 | 最终处理方案 |
|---|---|---|---|---|
| `paper_engine.py` | **L43** | `import exec_bridge as EB` → `EXEC_FROZEN` / `plan_entry` / `decision` / `drawdown_state` / `expiry_exit_ms` | ✅ 生产 | **保留并升级**为 Shared Trading Core 的桥（§6） |
| `live_worker.py` | **L39** | 同上 | ✅ 生产 | 同上 |
| `live_signal_engine.py` | **L55** | 同上 | ✅ 生产 | 同上 |
| `verify_exec_wiring.py` | L81 | 接线核验 | ⚪ 工具 | 保留 |

> **重要**：`exec_bridge` **不是**要清除的幽灵依赖，它是**当前唯一已经共享的执行核心桥**
> （`exec_core_v3.1.0`，`config_hash=fb3e617f…`，实测可用）。施工蓝图把它列为「待废除」，
> 但正确做法是**把它升级为 Shared Trading Core 的正式入口**，而不是删掉重写。

### 4.5 `wx_execution`

| 当前文件 | 行号 | 用途 | 是否生产路径 | 最终处理方案 |
|---|---|---|---|---|
| `strategies/wx_gate.py` | L1266 | 延迟导入 `wx_execution` | ✅ 生产（旧四臂链） | 随四臂降为 Legacy 后停用 |
| `strategies/wx_config.py` | L493, L599 | 延迟导入 | ✅ 生产（旧四臂链） | 同上 |
| `tests/test_funding_audit_counterexamples.py` | L45 | 测试导入 | 🔴 测试 | 随 Legacy 归档 |

---

## 5. 数据工作区污染诊断

### 5.1 为什么会产生大量工作区改动

**根因**：`框架/backend/快照/` 下的**运行时快照 JSON 被纳入了版本控制**，
而这些文件由采集/监控进程**持续覆写**（`public_snapshot.py` 的「公开扩展字段本机可读快照」机制，
以及 `smart_money.py` 的聪明钱快照）。

进程每次刷新 → 文件内容变化 → `git status` 立刻出现几十上百条 `M`。**与代码无关。**

### 5.2 实测构成（2026-10-16 15:20）

```
工作区改动总计 129 项 = 125 M + 4 ??

按目录：
  框架/backend/快照/smart_money/*.json          62   ← 运行时快照（被跟踪）
  框架/backend/快照/public_extras/*.json        61   ← 运行时快照（被跟踪）
  框架/backend/adapter.py                        1   ← ⚠ 见 §5.4
  框架/backend/datacenter_service.py             1   ← ⚠ 见 §5.4
  docs/evidence/backtest_golden_.../*            3   ← 本阶段产物
  docs/evidence/paper_gap_analysis_...md         1   ← 本阶段产物

按扩展名：.json 124 / .py 3 / .md 2
```

**其中 123 项（95.3%）是运行时快照数据，与源码无关。**

### 5.3 分层归类与处置建议

| 类别 | 实例 | 是否应入库 | 建议 |
|---|---|---|---|
| **source code** | `框架/backend/*.py`、`策略/**/*.py`、`UI界面/web/src/**` | ✅ 必须入库 | **保留** |
| **runtime data**（账本/状态库） | `框架/backend/live/*.sqlite3`（89 MB）、`live/*.json` 闸门 | ❌ 不入库 | `.gitignore` 已覆盖 `*.sqlite3*`；闸门 JSON 建议保留（是**配置**不是数据） |
| **market snapshots** | `框架/backend/快照/public_extras/*.json`（248 KB）<br>`框架/backend/快照/smart_money/*.json`（6.1 MB） | ❌ **不该入库** | **新增 ignore 规则**（见下） |
| **logs** | `框架/backend/logs/*.log`、`*.pid` | ❌ 不入库 | ✅ 已忽略（`*.log` / `*.pid` / `logs/`） |
| **runs**（回测产物） | `回测日志/oi_resonance_v1/runs/**`（961 MB） | ❌ 不入库 | ✅ **已忽略**（`.gitignore` L80，实测 `git ls-files` = 0） |
| **evidence** | `evidence/**`、`docs/evidence/**` | ✅ 入库（结论性证据） | **保留** |

### 5.4 ⚠ 核查期间发现并行写者（必须报告）

本次诊断进行中（15:01–15:07），检测到**另一个窗口正在修改业务代码**：

| 文件 | 状态 | 内容 | 时间 |
|---|---|---|---|
| `框架/backend/adapter.py` | **M**（+27 行） | 新增 `import datacenter_service`；新增 `/api/v1/data/oi`、`/api/v1/data/funding`、`/api/v1/data/smart-money` 三条路由；`v1_data_status()` 加 `metrics_summary` | 15:0x |
| `框架/backend/datacenter_service.py` | **??**（新文件，11,372 字节） | 「数据中心多维数据服务模块」（OI / 资金费 / 聪明钱 / 覆盖统计） | 15:06 |
| git 提交 `801a162` | 已提交 | 「完成历史冷层K线补缺落库收尾，升级验收工具实现白名单自动排除」；**151 文件**，且把本阶段已生成的 golden 产物一并提交 | 15:01:07 |

**对本次验收的影响**：
- 我本人**未修改任何业务代码**（本阶段改动仅 4 个 docs/evidence 产物）。
- 但仓库里确实出现了 2 处业务代码改动（`adapter.py` + `datacenter_service.py`），**来源是并行窗口**。
- 因此「业务代码修改 = 0」这条，**需要按「本窗口」口径验收**，不能按「仓库整体」口径。

### 5.5 建议的 `.gitignore` 增补（本阶段不执行）

```gitignore
# 运行时快照（由采集/监控进程持续覆写，非源码）
框架/backend/快照/
```

配套动作（需用户批准后另行执行）：
1. `git rm -r --cached 框架/backend/快照/` —— 从索引移除但**保留本地文件**；
2. 快照继续正常写入（代码路径不变）；
3. 若需保留历史，把当前快照复制到 `数据/快照归档/<日期>/`（该目录已被 `数据/` 规则忽略）。

> **本阶段不执行任何删除或 ignore 修改**，仅提出建议。

---

## 6. 最终结论（七问）

### 6.1 Backtest 当前为什么可以作为 Golden Reference？

因为它同时满足**可重复、可逐位核、账本自洽、输入可追溯**四条，且**已被实测验证**：

1. **可重复**：直接调 `backtest_api.create_run()`，不依赖后端进程，2.0–2.1 秒完成；
2. **可逐位核**：独立复跑一次，18 项指标**完全相同**（相对误差 < 1e-9）；
3. **可逐笔核**：7 笔交易的时点、价格、数量、费用、资金费、净盈亏、退出原因**逐笔完全一致**；
4. **账本自洽**：`verify_run_ledger.py` 独立重算 **32 PASS / 0 FAIL**；
5. **输入可追溯**：6 份输入快照 + SHA256 + 代码/参数哈希全部固化。

配套交付：`docs/evidence/backtest_golden_2026-10-16/`
（`golden_reference.json`、`ledger_summary.json`、`verify_run_ledger_output.txt`、
`reproduce_golden_backtest.py`、`README.md`），复跑脚本**实测 PASS**。

> 它的价值在**工程可复现性**，不在绩效（7 天 / 7 笔 / 全亏 / 胜率 0%）。

### 6.2 Paper 真正缺的是「账本」还是「整套旧账户模型」？

**缺的是「整套旧账户模型」，账本文件只是它的物理载体。**

- **账本文件**（`paper_{3a..3d}.sqlite3`）确实不存在 —— 这是**表面症状**；
- **但即使把四个库文件放回来，Paper 也不能接进新架构**，因为：
  - 四臂独立 5000 U ⇒ 合计 20,000 U，与执行核心的**共享 5000 U** 冲突；
  - 逐臂独立回撤锁 ⇒ 共享账户下必须全局锁；
  - 逐臂独立幂等水位 ⇒ 同一信号会在四臂各开一次；
  - 逐臂独立现金恒等式 ⇒ 整体核算必须重写。

⇒ **正确做法**：账本文件按 M002–M005 迁入**仅作历史归档（只读）**；
新 Paper 用**单一共享账户**重建，直接复用 `exec_bridge` 的冻结风控口径。

### 6.3 3a/3b/3c/3d 最终应该：保留 / 迁移 / Legacy / 删除？

| 对象 | 归宿 | 说明 |
|---|---|---|
| **代码**（`wx_donchian_wyckoff.py` 四臂分派、`ARMS` 常量、四臂循环） | **Legacy** | 保留可读、可查询、可追溯；**禁止进入新链路**。注册表已降为 `LEGACY_KEYS`，与之一致 |
| **账本行数据**（`paper_fill` / `paper_realized` / `paper_position` / `paper_funding` / `paper_equity` / `paper_order` / `paper_processed`） | **迁移**（只读归档） | 按 M002–M005 迁入 `数据/paper/`，仅作历史追溯 |
| **账户模型**（四臂独立 5000 U、`paper43_{arm}`、逐臂锁/水位） | **废弃** | 与共享账户口径冲突，迁移即放大 4 倍敞口 |
| **`exec_bridge → EXEC_FROZEN` 调用** | **迁移并升级** | 这是唯一已共享且正确的层，作为 Shared Trading Core 的种子 |
| **删除** | **无** | 本阶段不删任何东西；旧代码与旧数据都保留 |

### 6.4 `_warmup43` 等缺失模块真正承担什么功能？

**三件事，全部属于正式生产逻辑：**

| 模块 | 承担的功能 | 应归属层级 |
|---|---|---|
| `_warmup43` | ① 行情库只读连接；② **1h bar 包装配**（预热 + 窗口）；③ **39 原型冻结状态机做结构确认**（Spring / SC） | ② → **Data 层**；③ → **Strategy 层** |
| `_接入43` | **提示装配**：把结构确认转成可执行提示（绑定 UID / 准入 / 版本） | **Signal 层** |
| `_pool_scope` | **币池口径**（币安 526 加密币 + 排除 2 个指数） | **Data 层**（已有 fallback，当前可用） |
| `_pool_restfill` | 历史 K 线 **REST 补缺导入** | Data 层**离线工具**（非生产；已有新实现） |
| `_pool_gate` | K 线**覆盖门禁** | Data 层**验收器**（非生产；已有新实现） |

> `_接入43.make_prompts` 另有一个**已被本工程注释记录的逻辑缺陷**（`live_signal_engine.py` L195），
> 即使找回也不能原样复用。

### 6.5 Live 应该恢复这些旧模块，还是把功能迁移到正式架构？

**迁移。不恢复。**

理由三条：

1. **物理上不可恢复**：文件已随旧工作区整体删除，全盘搜索无结果；重建即伪造。
2. **功能归属清楚**：真正卡住 Live 的 `_warmup43` / `_接入43` 的功能分别属于
   **Data / Strategy / Signal 三层**，都有明确的正式落点（见 §6.4）。
3. **另三个模块本就不该恢复**：`_pool_restfill` / `_pool_gate` / `bulk_import` / `okx_download`
   是**离线数据工具**，只在测试与验收脚本里出现，工程内**已有等价新实现**
   （`evidence/Work_补缺落库_2026-10-16/` 下的四个工具脚本）。

**唯一需要注意的**：`_pool_scope` 的功能**当前通过 fallback 仍在工作**
（`数据/contracts/旧标的池/scope_crypto.json` 存在），所以币池不是阻塞项，
但 `pool_source` 会标成 fallback，**验收时必须如实报告**。

### 6.6 Shared Trading Core 应该从哪些现有代码抽取，而不是重新发明？

| 目标组件 | 抽取来源（现成、已验证） |
|---|---|
| **风控纯函数** | `策略/WyckoffMaster/execution.py` 的 `EXEC_FROZEN` + `plan_entry` / `validate_modes` / `normalize_direction`（已被 `exec_bridge` 桥接，`config_hash=fb3e617f…`） |
| **成交时钟（因果）** | `paper_engine.py` 的 `FILL_POLICY`（L84–L96）+ `order_request_ms` / `earliest_fill_ms` / `max_fill_lag`。**回测侧目前没有等价实现 ⇒ 应以 Paper 这套为准** |
| **资金费结算** | `execution.settle_funding_events` + `paper_engine._settle_funding`（事件身份幂等、缺口不补 0） |
| **回撤锁定四动作** | `paper_engine.enforce_drawdown` / `drawdown_state`（L1440 / L1433） |
| **账本算术恒等式** | `evidence/Work_单策略回测_2026-10-16/verify_run_ledger.py` 的 **32 项独立重算判据** |
| **回测驱动与指标** | `backtest_runner.py` + `oi_resonance_bridge.py`（已通过 Golden Reference 验证） |
| **账本 fail-closed 纪律** | `live_store.paper_conn` 的缺库/身份核对（L592–L637） |
| **幂等与水位** | `paper_processed` / `live_run` 机制（共享账户下需从「按臂」改「全局」） |
| **信号事件规范化** | `oi_resonance_v1` 的 `evaluate()` 输出格式（回测侧已定） |

**应丢弃而非抽取的**：四臂分派、`paper43_{arm}` 账户 ID、逐臂独立账本/锁/水位、
`paper_engine` 里与回测重复的第二套成交/持仓实现。

### 6.7 下一阶段最小修改集合是什么？

**按依赖顺序，最小集 = 6 项**（本阶段只报告，不执行）：

| # | 动作 | 目标文件 | 为什么必须 |
|---|---|---|---|
| 1 | **把 `_warmup43` 的「bar 装配」迁为正式 Data 层组件** | 新建 `框架/backend/data_hub/bar_assembler.py`（或等价）；改 `live_signal_engine.py` L430/L442/L453 | 解除 Live 硬阻塞的第 1 步 |
| 2 | **把「39 原型结构确认」迁为 Strategy 层组件** | 落到 `策略/WyckoffMaster/oi_resonance_v1/` 或 `trading_core/`；改 `live_signal_engine.py` L456 | 解除 Live 硬阻塞的第 2 步 |
| 3 | **信号事件规范化迁为 Signal 层组件**（重写，不复用有缺陷的 `make_prompts`） | 新建 `框架/backend/signal_engine.py`；改 `live_signal_engine.py` L537/L602 | 解除 Live 硬阻塞的第 3 步 |
| 4 | **删除 `live_worker.py` / `live_signal_engine.py` 的外部 `sys.path` 注入与旧路径** | `live_worker.py` L27–L35、`live_signal_engine.py` L43–L51、`live_pool.py` L72–L76 | 幽灵依赖清零的前提 |
| 5 | **`paper_engine.py` 两条外部路径迁入工程内** | L138–L141（`EFFECTIVE_VIEW`）、**L274–L276（`CONTRACT_SPEC_CSV`，目前仍可达）** | 消除静默高危依赖 |
| 6 | **`.gitignore` 增补 `框架/backend/快照/`** + `git rm -r --cached` | `.gitignore` | 消除 95% 的假性工作区改动 |

**明确不在最小集内**（属后续阶段）：
四臂下线、账本迁移（M002–M005）、Shared Trading Core 抽象、前端策略标签收敛、
`adapter.py` / `live_api.py` 路由重构。

---

## 7. 本阶段未做（如实声明）

- ❌ 未创建任何同名空模块或兼容壳（**0**）
- ❌ 未修改任何业务代码（**0**，本窗口口径）
- ❌ 未删除任何文件（**0**）
- ❌ 未启动 Live / Paper / 8787 后端
- ✅ 仅新增 3 份报告 + 4 个 Golden 配套文件（均在 `docs/evidence/` 下）

> ⚠ **须提醒**：核查期间（15:01–15:07）检测到**并行窗口**提交了 `801a162`
> 并修改了 `adapter.py` / 新增 `datacenter_service.py`。本窗口未参与这些改动，
> 但它们确实存在于工作区中，验收时请按窗口区分。
