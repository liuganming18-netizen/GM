# 验收证据 · 测试套件「改造前 vs 改造后」逐条对照

- 日期：2026-10-16
- 解释器：`/Users/boos/.workbuddy-ai/binaries/python/envs/default/bin/python`（duckdb 1.5.6 / pyarrow 26.0.0）
- 命令（两侧相同）：
  ```sh
  <py> -m pytest 框架/backend/tests -q -p no:cacheprovider --no-header --continue-on-collection-errors -rfE
  ```
- 「改造前」= 在 `BASE_COMMIT`（`64a447f433963ad917b4bb464bcece7e4805d1a4`）上新建的**临时 git worktree**（`git worktree add --detach /tmp/wk_base_probe <BASE_COMMIT>`，跑完即 `git worktree remove` + `prune`，未污染主工作区）。

---

## 1. 总量对照

| 阶段 | passed | failed | skipped | errors |
|---|---|---|---|---|
| **改造前**（BASE_COMMIT，裸树） | **212** | 25 | 47 | 10 |
| **改造后**（当前工作区） | **212** | **9** | 54 | **4** |

- **passed 完全相同（212 / 212）⇒ 零回归**：改造前通过的用例，改造后**无一转失败**。
- failed 25 → 9（−16）、errors 10 → 4（−6）、skipped 47 → 54（+7）。

---

## 2. 逐条归因（改造前 35 项非绿 → 改造后 13 项）

### 2.1 已消除的 22 项 —— 全部是「遗留组件缺失」类，非真实缺陷

| 组 | 改造前 | 改造后 | 原因 |
|---|---|---|---|
| `test_okx_volume_exec_v3.py`（16 个 FAILED） | 16 failed | **1 模块 skip** | 该模块回归的是旧工作区 OKX 修复脚本（`OKX成交量执行验收整改_2026-10-11/04_okx_volume_fix_v3.py`），**未随工程迁移**且属施工单**禁止恢复**项 |
| `test_okx_volume_fix_isolation.py` | 1 ERROR（收集） | **1 模块 skip** | 同上（`OKX成交量离线修复_2026-10-11/04_okx_volume_fix.py`） |
| `test_okx_volume_fix_rectification.py` | 1 ERROR（收集） | **1 模块 skip** | 同上（`OKX成交量离线修复整改_2026-10-11/04_okx_volume_fix_v2.py`） |
| `test_okx_volume_mapping.py` | 1 ERROR（收集） | **1 模块 skip** | `import _pool_restfill` —— 该模块全盘不存在 |
| `test_rest_import_contract.py` | 1 ERROR（收集） | **1 模块 skip** | 同上 |
| `test_pool_gate.py` | 1 ERROR（收集） | **1 模块 skip** | `import _pool_gate` —— 全盘不存在 |
| `test_history_integrity.py` | 1 ERROR（收集） | **1 模块 skip** | `import _pool_restfill` + `import _pool_pipeline` —— 全盘不存在 |

处理方式（W1 纪律：**fail-closed，不静默兜底**）：
在 docstring / `__future__` 之后插入**显式守卫**（`pytest.skip(..., allow_module_level=True)`），
条件 = 遗留件确实缺失。**显式 SKIP ≠ PASS**，原因写入测试报告；
若遗留件将来被正式迁入，守卫自动失效、测试照常运行。
工具：`工具_W1_遗留测试守卫.py`（带 `W1-GUARD-BEGIN/END` 标记，可幂等重跑）。

> 这 22 项**不是**「修好了」，而是**从「收集期崩溃 / 误报失败」改为「可读的显式跳过」**。
> 本质缺陷（遗留件不在工程内）依旧存在 —— 属施工单「旧四臂处理：生产调用 → 0」的预期结果。

### 2.2 改造后仍存在的 13 项 —— **与改造前逐条同名，零新增**

`框架/backend/tests/test_business.py`：**9 failed + 4 errors**，改造前后**完全同名同因**：

| # | 用例 | 根因 | 性质 |
|---|---|---|---|
| 1 | `BulkImportTests::test_archive_importers_preserve_existing_live_head` | `No module named 'bulk_import'` | 遗留模块，**从未在 git 仓库中**（`git ls-files \| grep bulk` 为空） |
| 2 | `BulkImportTests::test_streaming_matches_official_importer_row_by_row` | 同上 | 同上 |
| 3 | `BulkImportTests::test_streaming_rejects_checksum_mismatch` | 同上 | 同上 |
| 4 | `BulkImportTests::test_streaming_rejects_unclosed_and_unknown_timeframe` | 同上 | 同上 |
| 5 | `PoolAndCoverageTests::test_market_item_maps_stored_close_gaps_and_spot_status` | `No module named 'bulk_import'` | 同上 |
| 6 | `OkxImportTests::test_sha256_changes_with_content` | `No module named 'okx_download'` | 遗留模块，不在工程内 |
| 7 | `OkxImportTests::test_sha256_real_rows_and_raw_and_notes` | 同上 | 同上 |
| 8 | `ContractEmptyTests::test_strategies_pending_config_and_no_third` | 断言注册表**不含** `wx_oi_resonance_v1`，实际含 | **既有**：2026-10-16 策略替换已注册新键，测试未同步（`strategies/registry.py` 未被本 Work 改动） |
| 9 | `ContractEmptyTests::test_strategy_singular_backward_compat` | `KeyError: 'required_config'`（`adapter.py`） | **既有**（改造前同样失败） |
| 10-13 | `MarketDirectionTests::*` ×3 + `MarketDirectionGuardTests::*` ×1 | `sqlite3.IntegrityError: NOT NULL constraint failed: signals.strategy_version` | **既有**：测试的 `insert_signal` 助手未提供 `strategy_version`，而 schema 已要求 NOT NULL |

**证明「非本次引入」的硬证据**：以上 13 项在 `BASE_COMMIT` 裸树上**逐条同名同样失败**（见 `/tmp/base_fail.txt` 与第 1 节对照表）。

---

## 3. 策略侧（Shared Trading Core）测试

| 套件 | 命令 | 结果 |
|---|---|---|
| `策略/WyckoffMaster/test_work02_core_contract.py` | `<py> -m pytest 策略/WyckoffMaster/test_work02_core_contract.py -q`（cwd=`策略/`） | **15 passed** |
| `策略/WyckoffMaster/test_execution.py`（旧 v3.1 自检） | `cd 策略 && <py> -m WyckoffMaster.test_execution` | 退出码 0（自检脚本，输出 JSON） |

---

## 4. 结论

1. **零回归**：`passed` 212 → 212，无一条原有通过用例转为失败。
2. 改造后 13 项非绿**全部**为既有环境/既有断言问题（缺遗留模块、schema 与测试不同步、注册表断言过期），
   与 W1 路径改写、W2-0 质量列修复、Shared Core 抽取**均无因果关系**。
3. 新增 7 个显式 SKIP，原因是**遗留件确实不在工程内且禁止恢复**，属预期结果而非缺陷。
