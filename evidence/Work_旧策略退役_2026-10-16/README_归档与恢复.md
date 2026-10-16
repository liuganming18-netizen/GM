# 旧策略退役 · 可恢复只读归档（WORK-04 · 2026-10-16）

> 一句话：**旧策略（3a–3d / A / B）的「可运行入口」已下线，但旧代码、旧账本、旧证据一个字节都没删。**
> 本目录记录它们**在哪里**、**指纹是什么**，将来需要时可逐字节核对恢复。

---

## 1. 退役了什么（只退「可运行入口」）

| 入口 | 退役前 | 退役后 |
|---|---|---|
| 注册表 Registry | `3a/3b/3c/3d/A/B` 可运行 | `retired=true`、`runnable=false`，`is_runnable()` 恒 False |
| 回测 Backtest | 旧 `3a–3d` runs 混在列表里 | 新策略运行列表**只含** `wx_oi_resonance_v1`；旧 runs 单列 `excluded_legacy` 并标注归属 |
| 旧「回测复盘」通道 | 读旧 `3a–3d` / 模拟盘 / 信号审计账本 | `GET /api/v1/backtests?mode=...` 返回 `retired` 态，**不读、不算、不返任何旧数字** |
| Paper（模拟实盘） | 四臂纸面账户 / 持仓 / 下单 | **`NOT_CONNECTED`**：不返回旧账户，`POST /paper/orders` 恒 `503`，**禁止新开仓** |
| Live（实盘） | 只读账户 + 真实下单通道 | **`NOT_CONNECTED`**：禁止真实下单 |
| UI | 旧臂筛选 / 开仓按钮可用 | 旧臂筛选下线；开/平仓按钮**禁用**并显示 `NOT_CONNECTED` |

**唯一生产候选**：`WX_OI_RESONANCE_V1`（注册表键 `wx_oi_resonance_v1`）。

**为什么 Paper / Live 是 `NOT_CONNECTED` 而不是"暂时空仓"**：新共享核心目前只完成了 Backtest 一侧；
Paper / Live 的接线属后续施工单。为避免"看起来接通了"的假象，一律 fail-closed ——
**不回退旧 `exec_bridge`、不回退旧 3a–3d 账本模型**。

---

## 2. 归档内容（原件**未删、未改、未移动**）

清单：`归档清单_旧策略_2026-10-16.json`（逐文件 `path` / `bytes` / `mtime_utc` / `sha256`）

| 分类 | 文件数 | 说明 |
|---|---|---|
| `legacy_ab_source` | 2 | 旧 A/B 源码（`strategy_a.py` / `strategy_b.py`） |
| `legacy_fwx_source` | 7 | 旧 F_WX 3a–3d 源码（`wx_*.py`） |
| `legacy_shared_util` | 3 | 旧件共享工具（`common.py` 等，**未退役**，仍被 import） |
| `current_registry` | 1 | 当前注册表 `registry.py`（**未退役**；登记指纹用于核对退役标记） |
| `legacy_v31_core_archive` | 13 | 旧 v3.1 核心逐字节封存（`策略/WyckoffMaster/_legacy_archive_v31/`） |
| `legacy_run_dir` | 108 | 旧回测/复盘 runs（`回测日志/WyckoffMaster/`，只读保留） |
| **合计** | **134** | 约 256 MB |

> ⚠ `框架/backend/strategies/` 里的旧件**仍被当前 `registry.py` 在模块级 import**
> （`strategy_a` / `strategy_b` / `wx_donchian_wyckoff` / `wx_config` / `common`）。
> 所以它们**只能退役入口、不能删文件**，否则当前后端直接 import 失败。

---

## 3. 如何恢复（按需、逐字节可核对）

恢复**不需要**从本目录拷回任何东西 —— 原件本来就在原位。本目录提供的是**指纹**：

```bash
# 1) 逐文件核对指纹（原件是否被改动）
cd "/Users/boos/Desktop/威科夫因子共振趋势"
/Users/boos/.workbuddy-ai/binaries/python/envs/default/bin/python \
  evidence/Work_旧策略退役_2026-10-16/工具_校验归档清单.py
```

- **若只想恢复"可运行入口"**：把 `框架/backend/legacy_retirement.py` 里对应键从
  `RETIRED_*_KEYS` 移出，并回滚 `live_api.py` / `backtest_api.py` / `registry.py` 的退役补丁
  （见 `git diff`）。
- **若某个旧件被误改/误删**：用版本库或外部备份按清单 SHA256 还原后，重跑上面的校验脚本应为全绿。

---

## 4. 未退役的边界（避免误伤）

- `_pool_scope` **不是**旧策略代码：它是 `live_pool` 的**币池依赖**（历史下载口径），
  有工程内替代来源 `数据/contracts/旧标的池/scope_crypto.json`，且被
  `tests/test_history_integrity.py` / `tests/test_pool_gate.py` 直接引用 ⇒ **保留**。
  详见 `框架/backend/legacy_retirement.py::_POOL_SCOPE_DECISION`。
- 通用 K 线 / OI / Funding / Mark 数据、Golden、既有测试、历史证据**一律未动**。

---

## 5. 工具

| 文件 | 作用 |
|---|---|
| `工具_生成归档清单.py` | 只读：扫描旧件 → 写 `归档清单_旧策略_2026-10-16.json`（含 SHA256） |
| `工具_校验归档清单.py` | 只读：按清单逐文件核对 SHA256（全绿=原件未被改动） |
| `工具_引用分类扫描.py` | 只读：符号级统计旧件引用分布（供分类扫描复核） |
| `工具_截图_隔离实例.js` | Playwright：对隔离实例（前端 3010 → 后端 8799）截全页面 |
| `工具_截图_诊断页签.js` | Playwright：截「信号链 / 稳定性诊断 / 交易复盘 / 总览」四页签 |
| `引用分类扫描_2026-10-16.md` | 旧策略引用的分类扫描（保留 / 退役 / 替代来源） |
| `验收回执_WORK-04_2026-10-16.md` | 本窗口验收回执（含 before/after、逐笔对账、测试归因） |
| `BacktestReviewView_WORK04修正版_2026-10-16.tsx` | 回测工作台修正版快照（sha256 `4e599f521a94599f…`） |
| `截图/` | 全页面 + 各页签验收截图 |

---

## 6. ⚠ 并行写者与冲突施工单（务必先读）

本目录记录的是**本窗口（WORK-04）**的施工结果。施工期间检测到**另一并行窗口**正在写同一批文件，
且其施工单 **WORK-04B**（`docs/evidence/WORK-04B_final_acceptance_2026-10-16/`）的**阶段 4 要求物理删除**
旧策略源码与旧执行桥 —— 与本窗口「只退入口、绝不删除」的要求**互斥**。

- 本窗口**未删除任何文件**（归档清单可逐字节核对）。
- 两单裁决前**不应提交**。详见 `验收回执_WORK-04_2026-10-16.md` §0。
