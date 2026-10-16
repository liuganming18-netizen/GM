# WORK-04：旧策略隔离 → 新策略回测链跑通 → 旧策略及其回测数据清除施工总合同

- **施工编号**：WORK-04
- **制定日期**：2026-10-16
- **执行主体**：Antigravity Agentic Pair
- **唯一事实源**：本地 `wyckoff-v3.1-main` 最新代码与真实数据库
- **验收依据**：以 WORK-03 独立审计发现的 FAIL 事实为驱动，绝不继续在旧链打补丁，锁死单核架构收敛

---

## 一、铁律与执行顺序

```text
阶段 1：旧策略/旧执行链
        ↓
    【完全隔离，禁止删除，不再被新系统调用】
        ↓
阶段 2：新 WX_OI_RESONANCE_V1 + Shared Trading Core
        ↓
    端到端跑通真实 Backtest
        ↓
    新回测验收页面正确接入前端
        ↓
    真实 Run 生成 (run_id / Signal / Risk / Order / Fill / Position / Trade / Ledger)
        ↓
阶段 3：六大验收 Gate 完整审查 (PASS 才允许进入阶段 4)
        ↓
阶段 4：列出清单核实后，安全删除旧策略、旧执行、旧回测产物
        ↓
阶段 5：全仓库扫描确认：旧引用 = 0，外部绝对路径 = 0，Worktree 硬编码 = 0
        ↓
阶段 6：终极复核：删除后重启服务，重跑同一回测，UI 与结果必须 100% 一致
```

---

## 二、阶段任务与操作规范

### 阶段 1：旧策略与旧执行链完全隔离（先隔离，严禁提前删除）
1. **隔离范围**：
   - 旧执行桥接：`策略/WyckoffMaster/execution.py`（旧 3a/3b/3c/3d 执行流）
   - 旧策略核心：`策略/WyckoffMaster/core.py`、`factors.py`、`params.py`、`segments.py`、`series.py`
   - 旧验证脚本：`策略/WyckoffMaster/verify.py`、`experiments.py`
   - 旧纸面引擎执行混用点：`paper_engine.py` / `exec_bridge.py` 中对旧组件的引用
2. **隔离动作**：
   - 策略注册表（Registry）中移除旧策略，仅注册 `WX_OI_RESONANCE_V1` 作为生产唯一策略；
   - 任何新回测、模拟盘、实盘主流程，禁止以任何方式 import 或调用旧组件；
   - 先保留实体文件，作为后续比对与安全回滚证据，严禁在阶段 1 提前删代码。

### 阶段 2：新回测页面迁移与新链路全线跑通
1. **前端回测页面迁移**：
   - 将 GitHub `origin/work04-backtest-workbench` 提交中的 `src/components/BacktestReviewView.tsx` 迁移并标准化到本地前端标准目录：
     `UI界面/web/src/components/BacktestReviewView.tsx`
   - 遵循 Zero-Mock 原则：页面展示层绝不编造虚假数字，无真实 Run 显式显示 `MISSING` / `BLOCKED` / `NOT TESTED`。
2. **后端 Shared Trading Core 端到端串联**：
   - 真实数据输入：从本地 Parquet 冷层 + SQLite 热层读取历史行与 OI / 资金费率；
   - 策略驱动：`WX_OI_RESONANCE_V1` 产生原生信号；
   - 统一内核（`Shared Trading Core`）：
     - `signal.py` → 信号时间戳规范（四时点七问）
     - `risk.py` → 仓位风控与准入
     - `order.py` → 幂等 Order 生成
     - `fill.py` → 保守成交与撮合仿真（严格考虑滑点、手续费）
     - `position.py` → 严格持仓生命周期状态机
     - `trade.py` → 开平闭合 Trade
     - `ledger.py` → 现金流水与 Funding 计提账本
   - 运行一次真实回测，生成具体 `run_id`。

### 阶段 3：六大验收 Gate 严格通关
| Gate 编号 | 门禁名称 | 验收标准 | 状态 |
|:---|:---|:---|:---:|
| Gate 1 | 数据完整性 | 冷热层 0 异常空洞，白名单核销排除 | 待测 |
| Gate 2 | PIT 可得性 | 无未来函数，事件按生成时点可得性对齐 | 待测 |
| Gate 3 | 信号与消融 | 信号链完整，提供带 OI 与无 OI 因子消融对照 | 待测 |
| Gate 4 | 执行一致性 | 核心 Trading Core 为统一实现，无多轨代码 | 待测 |
| Gate 5 | 风险与风控 | 仓位准入、止损收敛与最大杠杆合规 | 待测 |
| Gate 6 | 账本与幂等 | Order/Trade/Ledger 资金流水一分不差，支持断点重启 | 待测 |

### 阶段 4：旧资产安全清理清单（先报备证据，再执行）
必须先扫描、列出以下完整清单并记录校验和后方可删除：
1. 旧策略代码文件
2. 旧 Registry 注册项
3. 旧 execution bridge
4. 旧回测缓存与废弃日志
5. 旧 API 废弃端点与 UI 旧导入

### 阶段 5：全仓库扫描确认 0 悬挂依赖
- `grep -r "wyckoff_v31"` 扫描
- `grep -r "/Users/boos/WorkBuddy AI/"` 绝对路径扫描
- `grep -r ".codex/worktrees/"` 扫描
- 确保所有生产依赖均归集于 `Shared Trading Core` 与标准工程根相对路径。

### 阶段 6：终极验证（删除后独立重测）
- 清理旧系统后，重启后端 API 与前端；
- 重新提交同一参数的真实回测任务；
- 验证新系统在**完全脱离旧代码**的情况下，回测结果、账本流水与删除前 100% 吻合，宣告收敛圆满达成。
