# 任务二交付回执：唯一策略核心与实时模拟执行

日期：2026-10-15 ｜ 范围：`策略/WyckoffMaster/`、`paper_engine.py`、`live_signal_engine.py`、
`live_worker.py`、共用执行核心。**未修改任务一的接口与启动文件。**

## 一、实际产物

| # | 文件 | 性质 |
|---|---|---|
| 1 | `策略/WyckoffMaster/execution.py` | **新增**：v3.1 共用执行核心（唯一策略/风控/成交口径） |
| 2 | `策略/WyckoffMaster/test_execution.py` | **新增**：合成边界 + 确定性 + 前缀不敏感 + 开关分离（13 项） |
| 3 | `策略/WyckoffMaster/EXECUTION.md` | **新增**：稳定接口与版本标识说明 |
| 4 | `框架/backend/exec_bridge.py` | **新增**：后端桥（唯一暴露点，不含任何阈值） |
| 5 | `框架/backend/verify_exec_wiring.py` | **新增**：执行链接线核验（只读、合成 bar） |
| 6 | `框架/backend/paper_engine.py` | **修改**：方向/模式/结构低点强制；回撤四动作；720h 口径；配置来源 |
| 7 | `框架/backend/live_signal_engine.py` | **修改**：真实方向落库（不再恒写 long）；版本标识入状态 |
| 8 | `框架/backend/live_worker.py` | **修改**：真实方向 + 非 long 显式拒单；两开关分离；退出顺序/台阶/720h/回撤 |

## 二、验收重点逐项

| 验收项 | 结论 | 证据 |
|---|---|---|
| 合成边界案例通过 | **PASS** | `01_execution_core_checks.json`（E1–E13 全 true） |
| 相同输入与配置得到确定结果 | **PASS** | E10：两次运行决定逐位相同、现金相同 |
| 追加未来数据不改变过去决定 | **PASS** | E11：截止时刻前决定逐位不变 |
| 旧仓维护与新开仓开关分开控制 | **PASS** | E12（4 个子情形）+ `switches()` |
| 共享 5000U / 最多 10 仓 / 只多 | **PASS** | E13；`interface()["frozen_constraints"]` |
| 任务一接口/核验无回归 | **PASS** | `02_strategy_verify_v3_1.json`（v3.1 十二项全过 + code_sha） |
| 实时链路可导入运行 | **BLOCKED（环境）** | `03_wiring_verification.json`：`blocked_missing_frozen_modules` |

## 三、十项重点修复

1. **short 被改写为 long** —— 已修。信号层此前恒写 `direction='long'`（`upsert_prompts`），
   臂真实方向（`base_short` 等）在入库时被抹掉，worker 又硬编码 `direction="long"`。
   现：方向原样落库；非 long 一律 `blocked_short_disabled_only_long`（仅展示）；
   worker 传真实方向，short 显式拒单 `short_disabled_by_frozen_view_only_long`，**不翻转**。
2. **参数被默认值覆盖** —— 已修。`EXEC_FROZEN` 为唯一真值；`resolve_config` 对任何改动
   冻结键的尝试报 `frozen_key_override_rejected`；视图不可读时标 `degraded=True` 并
   逐项对照（`_exec_core_crosscheck`），**不静默替换**。
3. **缺结构低点却退回固定止损** —— 已修。`plan_entry` 在 structural/structural_m2b 下
   缺结构低点即拒单 `missing_struct_low_for_structural_stop`（原 `core.compute_stop` 会
   悄悄降级为 3ATR；核心层不改任务一代码，改由执行入口把关）。
4. **非法模式未拒绝** —— 已修。`validate_modes` 覆盖 entry/stop/trail/rank/factor/direction；
   `open_position` 新增 `entry_mode/stop_mode/trailing_mode` 入参并前置校验。
5. **入场 15 分钟因果时钟** —— 已固化。`order_request_ms = max(收盘+1ms, available_at, 首次请求墙钟)`；
   `earliest_fill_ms = +15m`；只用该时刻 bar 的 **open**（E4 断言接口不含 low/close 入参）。
6. **退出事件顺序** —— 已固化。`order_bar_events` 四相位；worker 逐根 15m 同刻顺序：
   台阶生效 → 缺口止损 → 720h 到期 → 预约信号退出 → bar 内止损。
7. **台阶止损生效时间** —— 已修。`apply_trail` 返回 `effective_from_ms`（下一根 15m 开盘），
   单调不降、不回填；`_trail_schedule` 真实合成数据 50 步全部对齐 15m 网格且单调。
8. **720 小时到期** —— 已修（D01）。`expiry_exit_ms` = 入场+720h 之后**首根 15m 开盘**；
   离线核验 `hit_at_due_bar_open = true`（原实现取最后一根已闭合 bar 的**收盘**）。
9. **回撤锁定** —— 已补（D02）。`enforce_drawdown`：停开仓（持久锁定，只锁不解）+
   撤未成交挂单 + 下一根 15m 开盘清仓（因果：只认已闭合 bar）+ 本轮不恢复。
10. **资金费结算** —— 已固化。窗口 `(entry_ts, exit_ts]`、`qty×真实 mark×真实 rate`、
    负费率收入保留、事件身份 `(position, funding_time)` 幂等、缺 rate/mark 记
    `paper_funding_gap` 不补 0。

## 四、未通过项 / 阻塞（如实）

1. **实时链路无法在本机导入运行**：`live_signal_engine` / `live_worker` 依赖的冻结研究件
   `_pool_scope.py`、`_warmup43.py`、`_接入43.py`（原在
   `/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/…`）**已不在本机**，
   项目内 `框架/最新唐奇安威科夫接通与收尾_2026-10-11/` 只迁移了 `纸面模拟43` 账本。
   该阻塞属**任务一启动路径/迁移范围**，本任务**不伪造替身、不改任务一启动文件**。
   解除方式：把冻结研究件放回 `live_pool.py:70-78` / `live_signal_engine.py:43-50`
   所声明的 `sys.path` 路径（或按迁移表补齐 `框架/最新…/` 下的冻结模块）。
   为不阻塞验收，本任务以**替身模块 + 合成 bar** 完成了适配层核验
   （`04_wiring_offline_standin_smoke.json`，替身只补缺失模块，不进入项目、不写生产库）。
2. **冻结参数视图缺失**：`10_研究参数生效视图_v2_2_user5000_shared.json` 原路径已不存在
   ⇒ `paper_engine.approved_params()` 走 `fallback_frozen_v2_2_conservative`。
   已逐项对照共用核心冻结值，**零不一致**（`_exec_core_crosscheck.ok = true`）。
3. **未做**：全池回测、Holdout 运行、生产切流、放行新开仓、修改任务一启动文件。
4. **Holdout 红线**：本任务核验全部使用**合成 bar**；未读取真实行情（当前热库数据
   全部落在 Holdout 区间，按红线不读）。

## 五、给任务三 / 任务一的交接

* 任务三：`from WyckoffMaster import execution as X`；用 `X.plan_entry` 开仓、
  `X.apply_trail`/`X.expiry_exit_ms`/`X.drawdown_state`/`X.settle_funding_events`/
  `X.order_bar_events` 维护持仓、`X.ReplayEngine` 跑确定性回放；绑定
  `interface()["config_hash"]` 与 `strategy_params_hash`。
* 任务一：`框架/backend/exec_bridge.version_ids()` 与
  `paper_engine.snapshot()["exec_core" | "config_source" | "switches"]` 可直接展示。
* 新开仓默认仍**阻断**：`live/paper_new_entry_gate.json` 的 `allow_new_entries=false`；
  放行须经正式候选选定 + 最终验收后受控启用，且**不补开历史信号**。
