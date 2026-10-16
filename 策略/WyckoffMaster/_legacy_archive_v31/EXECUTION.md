# 共用执行核心（exec_core v3.1）—— 接口与版本标识

> 本文件是**任务二**交付说明。核心代码：`策略/WyckoffMaster/execution.py`；
> 后端桥：`框架/backend/exec_bridge.py`；核验：`策略/WyckoffMaster/test_execution.py`、
> `框架/backend/verify_exec_wiring.py`。**本文件不新增任何阈值**，阈值唯一来源是
> `execution.EXEC_FROZEN`（其哈希即 `config_hash`）。

## 1. 为什么需要它

历史回放（任务三）与实时模拟（任务二）若各写一套成交/风控，会出现「同一根 bar、
同一份配置，两处得到不同决定」。本核心把**决策**收敛为纯函数，两侧都调它，
才谈得上「同一策略、风控与成交逻辑」。

## 2. 版本标识（任务三绑定 / 任务一展示）

| 标识 | 值 | 含义 |
|---|---|---|
| `CORE_VERSION` | `exec_core_v3.1.0` | 执行核心版本 |
| `STRATEGY_VERSION` | `3.1` | 策略冻结版本（与任务一一致） |
| `LEDGER_MODEL_VERSION` | `paper_exec_v2_shared_5000` | 账本口径（共享 5000U） |
| `EXEC_FROZEN.hash()` | 见 `interface()["config_hash"]` | 执行/风控冻结值指纹 |
| `_params_hash()` | 见 `interface()["strategy_params_hash"]` | 策略阈值指纹 |

```python
from WyckoffMaster import execution as X
X.interface()       # 稳定接口清单 + 冻结约束 + 版本标识
X.version_block()   # 版本 + 冻结配置全量
```
后端：`import exec_bridge as EB; EB.version_ids()`。

## 3. 冻结约束（共享账户）

共享 **5000 USDT**、同时持仓**合计最多 10**、只多（`short_enabled=False`）、
单笔止损风险 0.5% 权益、单币名义 ≤10%、总多头 ≤80%、现金预留 ≥20%、
组合初始止损风险 ≤3%、参与率 ≤上一根 15m quote 的 1%、双边各 0.05% 费 + 0.05% 滑点。
旧四账户历史**不由本模块读写**（本模块无 IO）。

## 4. 开仓：唯一入口

```python
d = X.plan_entry(symbol=..., direction="long", signal_kind="spring",
                 signal_bar_close_time=..., available_at=..., request_clock_ms=...,
                 open_15m=..., atr_val=..., struct_low=..., stop_mode="structural",
                 limits=..., equity=..., free_cash=..., gross_open_notional=...,
                 open_risk_abs=..., prev15m_quote_volume=..., open_positions=...,
                 attempt_clock_ms=...)
```
返回**决定信封**（`reject_reason` 为 `None` 即可开）：
`condition_values`（条件实际值）、`thresholds`（阈值）、`times`（各时刻）、
`reject_reason` / `reject_detail`、`event_identity`、`core_version`、`config_hash`。

拒绝原因（节选，均为**显式**拒单，绝不静默改写）：
`illegal_direction:*`、`illegal_entry_mode:*`、`illegal_stop_mode:*`、
`short_disabled_by_frozen_view_only_long`、`max_positions_total_reached`、
`missing_struct_low_for_structural_stop`、`entry_below_struct_low`、
`pending_future_fill`、`missed_fill_moment_no_backfill`、`open_already_through_stop`、
`single_name_cap_exceeded`、`gross_long_cap_exceeded`、`cash_reserve_breach`、
`portfolio_stop_risk_cap_exceeded`、`participation_cap_exceeded`、`missing_contract_spec`。

## 5. 持仓维护（旧仓）与两个开关

```python
X.apply_trail(...)            # 台阶锁利：返回 effective_from_ms = 下一根 15m 开盘
X.expiry_exit_ms(entry_ts)    # 720h 到期经济时刻 = 入场+720h 后首根 15m 开盘
X.drawdown_state(...)         # 回撤 ≥20% 四动作（停开仓/撤挂单/下根开盘清仓/本轮锁定）
X.settle_funding_events(...)  # 资金费：窗口 (entry,exit]、qty×mark×rate、负费率保留
X.order_bar_events(...)       # 同根 15m 固定 4 相位顺序
X.stop_hit_for_bar(...)       # bar 内止损（开盘穿 stop ⇒ 更差 open）
```

**两个独立开关**（实时侧 `paper_engine.switches()`）：
* `allow_new_entries` —— 服务端控制文件 `live/paper_new_entry_gate.json`（fail-closed）。
  **正式候选未选定、未经最终验收 ⇒ 继续阻断（默认 False）。**
* `maintain_existing` —— 旧仓维护（退出 / 资金费 / 到期 / 回撤 / 台阶），常开，
  **不受**新开仓闸影响。

## 6. 确定性引擎

```python
eng = X.ReplayEngine(bars_signal=..., bars_15m=..., funding_events_by_symbol=...,
                     allow_new_entries=False, maintain_existing=True)
r = eng.run(upto_ms=...)      # 截断运行
eng.decisions                 # 逐决定（前缀可比对）
```
· 只处理 `open_time <= now` 的已闭合 bar ⇒ **追加未来数据不改变过去决定**（构造保证）；
· 同输入同配置 ⇒ 逐位相同决定。

## 7. 十项重点修复落点

| 项 | 落点 |
|---|---|
| short 不被改写为 long | `normalize_direction` / `plan_entry`；`live_signal_engine._arm_entry_prompts` + `upsert_prompts`（真实方向落库）；`live_worker` 传真实方向并显式拒单 |
| 参数不被默认值覆盖 | `EXEC_FROZEN` 唯一真值；`resolve_config` 拒绝冻结键覆盖；`load_exec_config` 视图核对（缺失只降级不替换） |
| 缺结构低点不退回固定止损 | `plan_entry`：`missing_struct_low_for_structural_stop` |
| 非法模式被拒绝 | `validate_modes`（entry/stop/trail/rank/factor/direction） |
| 入场 15 分钟因果时钟 | `plan_entry` 的 `order_request_ms` / `earliest_fill_ms`；只用该时刻 open |
| 退出事件顺序 | `order_bar_events`（4 相位）；`live_worker._scan_position_exit`（同刻顺序） |
| 台阶止损生效时间 | `apply_trail` 的 `effective_from_ms`；`live_worker._trail_schedule` |
| 720 小时到期 | `expiry_exit_ms` / `expiry_exit_from_bars`；`paper_engine.expiry_exit_ts`（修复 D01） |
| 回撤锁定 | `drawdown_state`；`paper_engine.enforce_drawdown`（修复 D02） |
| 资金费结算 | `settle_funding_events`（事件身份幂等、缺口不补 0） |

## 8. 边界与未决

* 本核心**不下载数据、不写生产库、不下单**；旧四账户历史原样保留。
* `正式候选` 尚未选定：`allow_new_entries` 默认阻断，未经最终验收不得放行。
* 实时链路可运行性依赖冻结研究件（`_pool_scope` / `_warmup43` / `_接入43`）可达；
  当前本机不可达，`verify_exec_wiring.py` 会如实报 `blocked_missing_frozen_modules`。
  该阻塞属任务一启动路径/迁移范围，本核心**不伪造替身**。
