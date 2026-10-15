# WyckoffMaster 回测窗口 · 状态与操作说明（v3.1）

**当前阶段：v3.1 准备与自核验完成 / 等待两个 v3.1 就绪交付。尚未开始任何回测，未读取 Holdout。**

> 本窗口是**唯一回测调度窗口**。策略代码与研究数据在各自新窗口施工；本窗口不重写信号策略、
> 不做全池下载、不改他窗产物、不碰生产服务与真实账本。

---

## 一、v3.1 冻结区间（覆盖上一版）

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

## 三、Holdout 最高红线（已物理强制）

`adapters/holdout_guard.py` 把红线做成代码级拒绝：选型阶段任何与 Holdout 有交集的窗口**直接抛错**；
`--phase holdout` 需 `state/winner_lock.json` 才放行；截止未到做终验也拒绝。
`run_ladder.py` 在**任何数据读取之前**先做红线检查。此前读取披露见
`evidence/Work_阶梯回测_2026-10-15/10_此前Holdout区间读取披露_2026-10-15.md`。

## 四、发现的真实引擎差异（已写局部适配，未改生产引擎）

冻结引擎 `wx_execution.run_arm` 期末清算用**最后一根 15m 开盘**（`last_executable_open`），
v3.1 要求用**本段最后 15m 收盘**。`adapters/segment_end_liquidation.py` 在独立账本副本上定点替换
（`segment_end_force_close_last_15m_close`），并留逐笔差异。生产引擎 SHA 未变。

## 五、现在卡在哪

1. `evidence/Work_策略优化_2026-10-15/strategy_ready.json` —— 未发布（须绑定 **v3.1**）。
2. `evidence/Work_策略数据_2026-10-15/data_ready.json` —— 未发布（须绑定 **v3.1**）。
3. **数据窗研究输入缺 IS/Val 与 Holdout 物理分区**（自核验唯一未过项）。

## 六、就绪后怎么启动（用户已授权本轮隔离回测，无须再问）

```sh
cd /Users/boos/Desktop/威科夫因子共振趋势/回测日志/WyckoffMaster
python3 scripts/check_gates_once.py         # 有界探测
python3 scripts/verify_gates.py             # 独立核查（v3.1 + SHA + 覆盖 + 备份 + 分区）
python3 scripts/verify_v31_prep.py          # 准备件自核验（36 项）
python3 scripts/run_ladder.py --phase kline --dry-run
sh adapters/launch_isolated.sh logs/ladder_kline.pid logs/ladder_kline.log \
   scripts/run_ladder.py --phase kline
python3 scripts/run_ladder.py --phase factor
# 锁唯一模型 → state/winner_lock.json → 等截止+闭合 → 仅一次：
sh adapters/launch_isolated.sh logs/ladder_holdout.pid logs/ladder_holdout.log \
   scripts/run_ladder.py --phase holdout
```

## 七、纪律红线（写死在适配层里）

- 只写 `回测日志/WyckoffMaster/`；写目标越界即拒绝。
- 生产引擎 `wx_execution.py` SHA 不符即拒绝运行（防他窗改写）。
- 资金费只允许 `historical_events`；`norm4h` / `ext1h` / 补 0 / 普通 close 冒充一律拒绝。
- 不抢 3000 / 8787，不启停生产服务，不动真实账本。
