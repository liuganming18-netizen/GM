# WORK-04B 阶段 1：删除前全库依赖深度审计报告

- **审计日期**：2026-10-16
- **审计范围**：`框架/`、`策略/`、`UI界面/web/`、`docs/`、`tests/`、`scripts/`、`配置/`
- **审计目标**：全面摸排旧策略代码与旧执行桥在系统各层的引用关系，按五大类别精确定性，指导阶段 2 依赖解除与阶段 4 安全删除。

---

## 一、旧策略资产清单（待退役/清理对象）

| 资产名称 | 所在路径 | 角色定性 | 阶段4处置方案 |
|:---|:---|:---|:---|
| **旧核心逻辑** | `策略/WyckoffMaster/core.py` | 旧 v3.1 核心算法 | 阶段 4 物理删除 |
| **旧执行流** | `策略/WyckoffMaster/execution.py` | 旧 3a~3d 执行实现 | 阶段 4 物理删除 |
| **旧参数定义** | `策略/WyckoffMaster/params.py` | 旧策略硬编码参数 | 阶段 4 物理删除 |
| **旧因子/片段** | `策略/WyckoffMaster/factors.py`<br>`segments.py`<br>`series.py` | 旧特征提取工具 | 阶段 4 物理删除 |
| **旧测试/验证** | `策略/WyckoffMaster/test_execution.py`<br>`verify.py`<br>`experiments.py` | 旧执行测试夹具 | 阶段 4 物理删除 |
| **旧执行桥** | `框架/backend/exec_bridge.py` | 旧执行桥接层 | 阶段 4 物理删除 |
| **旧策略实现** | `框架/backend/strategies/wx_donchian_wyckoff.py`<br>`strategy_a.py`<br>`strategy_b.py`<br>`wx_config.py` | 旧 3a~3d 及 A/B 策略 | 阶段 4 物理删除 |

> **严禁删除的受保护资产（永久保留）**：
> - `策略/WyckoffMaster/_legacy_archive_v31/`（不可篡改的历史封存归档，供历史追溯）
> - `docs/evidence/`（所有历史审计报告、WORK-02、WORK-03、WORK-04 证据链）
> - `策略/WyckoffMaster/trading_core/`（Shared Trading Core 唯一共享交易核心）
> - `策略/WyckoffMaster/oi_resonance_v1/`（新策略 WX_OI_RESONANCE_V1 唯一实现）

---

## 二、全库引用五类定性分析

### 1. 生产运行引用 (Production Runtime)
在尚未解除依赖前，以下 7 个后端文件存在对待删模块的显式 `import`，必须在阶段 2 进行迁移与依赖切断：
- `框架/backend/strategies/registry.py`：导入了 `wx_donchian_wyckoff`, `strategy_a`, `strategy_b`
- `框架/backend/strategies/wx_config.py`：导入了 `wx_donchian_wyckoff`
- `框架/backend/exec_bridge.py`：旧桥自身
- `框架/backend/paper_engine.py`：导入了 `exec_bridge`
- `框架/backend/live_worker.py`：导入了 `exec_bridge`, `wx_donchian_wyckoff`
- `框架/backend/live_signal_engine.py`：导入了 `exec_bridge`, `wx_donchian_wyckoff`
- `框架/backend/verify_exec_wiring.py`：导入了 `exec_bridge`

**处置**：阶段 2 彻底解除上述文件的 import 依赖，使新回测主链路（`Data → WX_OI_RESONANCE_V1 → Shared Trading Core → Backtest API → UI`）达到 100% 独立运行。

### 2. 测试引用 (Test Suites)
- `框架/backend/tests/test_business.py`：包含对已退役旧策略 `strategy_a` / `strategy_b` 的单元测试。
**处置**：标记为历史测试，剥离旧测试用例或归档，保留针对新核心与新策略的测试。

### 3. UI 展示引用 (UI / Frontend)
- `UI界面/web/src/App.tsx`、`PositionPanel.tsx`、`useLiveTrading.ts`：在代码注释和只读免责声明中提到了 `exec_bridge`（说明旧通道已封堵，不回退）。
**定性**：无实质模块 import，仅为防呆提示文本，符合规范。

### 4. 历史文档引用 (Documentation / Evidence)
- `docs/evidence/*` 下各文档记录了系统架构演化历史、WORK-03 审计报告等。
**定性**：作为司法级审计证据保留，不得被生产代码加载。

### 5. 历史归档引用 (Archive / Legacy Archive)
- `策略/WyckoffMaster/_legacy_archive_v31/` 下包含完整的历史代码副本与清单。
**定性**：完全物理隔离，不被系统 sys.path 自动引入，纯归档。
