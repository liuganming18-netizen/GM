# WyckoffMaster 回测窗口 · 状态与操作说明（v3.1）

**当前阶段：v3.1 接入完成 + 小型 IS/Val 窗口验证通过（`all_ok=True`）。**
**完整阶梯尚未启动**——门禁未 `BOTH_PASS`（`strategy_ready=FAIL`，由策略窗维护），
`run_ladder.py` 在此之前一律 `REFUSED_NOT_READY`。**全程未读取 Holdout。**

> 本窗口是**唯一回测调度窗口**。策略代码与研究数据在各自新窗口施工；本窗口不重写信号策略、
> 不做全池下载、不改他窗产物、不碰生产服务与真实账本。
> 详细证据见 `evidence/Work_阶梯回测_2026-10-15/24_引擎与数据接入_v31.md`。

---

## 一、v3.1 冻结区间

| 段 | 起（UTC 含） | 止（UTC 不含） | 天数 |
|---|---|---|---|
| **IS** | 2022-01-01T00:00:00Z | 2025-01-01T00:00:00Z | 1096 |
| **Val** | 2025-01-01T00:00:00Z | 2026-04-15T00:00:00Z | 469 |
| **Holdout** | 2026-04-15T00:00:00Z | 2026-10-15T00:00:00Z | 183 |

- 三段各自 **5000 USDT / 零仓 / 零挂单** 起算；段界重置 `cash / pending_orders / positions /
  execution_state / equity_peak`；起点前仅指标预热。
- 段末未平仓用**本段最后 15m 收盘强平**（+不利滑点/费用/应结资金费），**不用下一段开盘**。
- Holdout 截止 = **北京时间 2026-10-15 08:00**；终验须等截止后数据闭合，不得提前截断。

## 二、本轮历史只跑 M0–M4 + 条件 M2b

- **M0 → M1 → M2（+ 条件 M2b）→ M3_BE / M3_Step → M4**，全部只在 **IS / Val**。
- **M5 / M6 本轮不跑**，改前向验证（30 日分位 / 30 日 Z 排序，每币 ≥30 自然日且 ≥90 有效快照才激活）。
- 顺序：**先定 M2/M2b 及 M3 父配置，再 M4**；不同时启动多套全池。
- 父层**不硬编码**：由 `engine/ladder.py::promote()` 按 `config/ladder.json` 的 `promotion_rules`
  跨 IS+Val 判定后动态选择（阈值冻结在 `ladder.PROMOTION`，调用处不得改）。

## 三、Holdout 最高红线（已物理强制）

`adapters/holdout_guard.py` 把红线做成代码级拒绝：选型阶段任何与 Holdout 有交集的窗口**直接抛错**；
`--phase holdout` 需 `state/winner_lock.json` 才放行；截止未到做终验也拒绝。
`run_ladder.py` 在**任何数据读取之前**先做红线检查。此前读取披露见
`evidence/Work_阶梯回测_2026-10-15/10_此前Holdout区间读取披露_2026-10-15.md`。

## 四、执行唯一口径（不再自写成交/风控）

`engine/engine.py` 只做「多标的组合编排 + 排名 + 仓位上限 + 逐段窗口 + 账本」；
**所有成交与风控决定都调任务二共用核心** `策略/WyckoffMaster/execution.py`（`exec_core v3.1.0`）：
`plan_entry` / `size_position` / `stop_hit_for_bar` / `apply_trail` / `expiry_exit_ms` /
`drawdown_state` / `settle_funding_events` / `order_bar_events` / `round_*_price`。

- **段末强平**由引擎直接按「本段最后 15m 收盘」结算（`segment_end_force_close_last_15m_close`），
  原先的定点替换适配器 `adapters/segment_end_liquidation.py` **已不再参与主链路**（保留作历史对照）。
- **旧研究目录依赖已移除**：`adapters/ra_bridge.py` 重写为项目内闭环（研究输入 →
  `数据/研究输入/WyckoffMaster_v31/{is,val}`；不再引用已消失的 `outputs/网站逆向研究/...`）。
- **15m 入场时钟修正**：`entry = X.next_15m_open_at_or_after(信号收盘 + 1ms + 15m)`；
  旧实现等价零延迟、每笔早 15 分钟，已修正并逐笔验证（`entry_clock_violations=0`）。

## 五、现在卡在哪（门禁独立核查，实际复算而非信 `ready=true`）

| 项 | 结论 | 说明 |
|---|---|---|
| `data_ready.json`（v3.1） | **PASS** | manifest SHA 复算一致；`is/`+`val/` 存在；**无 holdout 目录**；blocking 为空 |
| 生产执行引擎 SHA | **OK** | `wx_execution.py` == `0ede9ce1…dbcf1` |
| `strategy_ready.json`（v3.1） | **FAIL** | ① 逐文件 SHA 出现 extra：`execution.py`、`test_execution.py`（任务二新增，声明未更新）；② `bundle_sha256` 不一致；③ `blocking_items` 非空 |

`blocking_items` 中 **B2 =「执行层按 SHA 绑定既有引擎（15m 成交/滑点/费用/真实资金费/回撤停机/
段末最后 15m 收盘强平/三段独立账户）」—— owner 标注为「回测窗口」**，本轮交付即为 B2 的完成证据。
`strategy_ready.json` 由**策略窗**维护，本窗口不改他窗文件。

## 六、就绪后怎么启动（用户已授权本轮隔离回测，无须再问）

```sh
cd /Users/boos/Desktop/威科夫因子共振趋势/回测日志/WyckoffMaster
python3 scripts/check_gates_once.py          # 有界探测
python3 scripts/verify_gates.py              # 独立核查（v3.1 + SHA + 覆盖 + 备份 + 分区）
python3 scripts/verify_v31_prep.py           # 准备件自核验（36 项）
python3 scripts/run_ladder.py --phase kline --dry-run
sh adapters/launch_isolated.sh logs/ladder_kline.pid logs/ladder_kline.log \
   scripts/run_ladder.py --phase kline
python3 scripts/run_ladder.py --phase factor
# 锁唯一模型 → state/winner_lock.json → 等截止+闭合 → 仅一次：
sh adapters/launch_isolated.sh logs/ladder_holdout.pid logs/ladder_holdout.log \
   scripts/run_ladder.py --phase holdout
```

完整阶梯前已通过的小型验证（可随时复跑）：

```sh
python3 scripts/validate_small_window.py       # 小型 IS/Val：逐笔对账 + 不变量 + 继承（~350s）
python3 scripts/no_lookahead.py                # 无前视：未来置毒 + 截断不变
python3 scripts/perf_equiv.py                  # 性能三态等价性
python3 scripts/stream_vs_batch_reconcile.py 3 # 历史回放 vs 流式推进 逐笔对账（~12s）
python3 scripts/smoke_engine.py                # 冒烟
```

## 六之二、历史回放 vs 流式推进：对账结果（重要）

见 `evidence/Work_阶梯回测_2026-10-15/26_历史回放与流式推进对账_v31.md`。结论分两层：

- **决策层（信号/开仓决定）完全一致**：19 笔共同信号，成交价/止损/数量/拒单原因 0 差异；
- **结果层全路径不等价**：首个分歧之前逐笔一致、权益差异全 0，之后分叉。
  根因是**共用核心的流式引擎 `ReplayEngine` 能力缺口**（非本窗口缺陷）：
  **G1** 缺唐奇安预约出场、**G2** 缺台阶/保本锁利、**G4** 缺同标的并发去重、
  **G5** 出场价未按 tick 取整；**G3** 段末强平属本窗口职责（已在组合引擎内实现）。
- 流式引擎自身「追加未来数据不改变过去决定」**成立**（前缀逐字一致）。

⇒ **G1/G2/G4/G5 需任务二/策略窗确认**：实时链路是靠 `live_worker._scan_position_exit`
补齐（则该链路需拿来做同样对账，当前本机不可达），还是直接跑 `ReplayEngine`（则必须补齐）。

## 七、目录说明

| 路径 | 作用 |
|---|---|
| `engine/` | `engine.py` 组合编排；`ladder.py` 跨段晋级驱动；`m4.py` OI 分位排序；`loader.py`/`paths.py`/`ledger.py`/`reconcile.py` |
| `adapters/` | `holdout_guard.py`（红线）、`data_hygiene.py`（池与卫生）、`ra_bridge.py`（项目内隔离桥）、`launch_isolated.sh` |
| `config/` | `ladder.json`（层级定义 + **冻结晋级规则**）、`frozen_segments.json`（三段与段末口径）、`m2_rejection.json` |
| `state/` | 运行状态与核验产物（`gate_verify.json`、`small_window_validation.json`、`no_lookahead.json`、`perf_equiv.json`、`phase.json`、`winner_lock.json`(未生成)） |
| `ledgers/` | 逐层逐段账本 `ladder_<tag>_<tf>_<seg>_<level>.sqlite3` |
| `logs/` | 运行日志（`small_validate.log`、`final_evidence.log` 等） |
| `scripts/` | 门禁核查、阶梯调度、小型验证、无前视、性能等价、冒烟 |

## 八、纪律红线（写死在适配层里）

- 只写 `回测日志/WyckoffMaster/`；写目标越界即拒绝。
- 生产引擎 `wx_execution.py` SHA 不符即拒绝运行（防他窗改写）。
- 资金费只允许 `historical_events`；`norm4h` / `ext1h` / 补 0 / 普通 close 冒充一律拒绝。
- 不抢 3000 / 8787，不启停生产服务，不动真实账本。
