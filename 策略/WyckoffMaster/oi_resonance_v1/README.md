# WX_OI_RESONANCE_V1（新候选策略）

> 落地日期：2026-10-16 · 分支 `wyckoff-v3.1-main` · 基线 HEAD `e1d38d6`

## 这是什么

按用户 2026-10-16 新附件落地的一份**新候选规则**，并**退出**旧的当前启用来源。

| 项 | 值 |
|---|---|
| 策略 ID | `WX_OI_RESONANCE_V1` |
| 注册表键 | `wx_oi_resonance_v1` |
| 族 | `F_OI_RES` |
| 实验 ID | `exp_oi_resonance_v1_2026-10-16` |
| 信号版本 | `oi_res_v1.0.0` |
| 核心版本 | `oi_res_core_v1.0.0` |
| 执行核心版本 | `oi_res_exec_v1.0.0` |
| 周期 / 方向 | 1h / long（`enable_short=False` 默认） |
| 时间单位 | **显式**（注册表入口声明 `ms`；未声明 ⇒ 拒绝） |

## 它不是什么

- **不是**历史 `WyckoffMaster v3.1` 的同一策略；**不继承** `F_WX 3a/3b/3c/3d` 与历史
  `M0/M1/M2/M2b/M3/M4`、前向 `M5/M6` 的任何成绩（见 `config.NOT_INHERITED`）。
- **不复用**旧 `structural_m2b` 这个名字：旧 M2b 是**条件距离实验**（绕过 3.5ATR 超限拒单的
  止损距离变体）；本包的回踩是威科夫 **LPS**（Last Point of Support），故用新名
  `lps_pullback` + 新止损模式 `lps_structural`。
- **不覆盖**旧账户冻结参数（`../params.FROZEN`、`../execution.EXEC_FROZEN` 原样保留）。

## 信号

| id | 名 | 含义 |
|---|---|---|
| 0 | `none` | 无信号 |
| 1 | `spring` | 跌破前低后当根收回且收阳 |
| 2 | `sc_reversal` | 放量长下影、振幅显著的下插反转 |
| 3 | `donchian_breakout` | 收盘严格突破此前 N 根最高 |
| 4 | `lps_pullback` | 突破后回踩不破、重新站上突破位（LPS） |

同根优先级固定（2026-10-16 订正）：
`fresh breakout 登记 → 因子门禁 → sc_reversal → spring → donchian_breakout → lps_pullback →（可选）short`
—— **SC 先于 Spring**；突破锚点只在 **fresh breakout** 当根登记，且**先于**因子门禁。

## 相对附件稿修掉的实现缺陷

1. **数值校验**：`compute_stop` 逐参做类型 + 有限性检查；bool/str/None/NaN/±inf、
   非法 `stop_mode`、非法 `direction`、非法极值（≤0/非有限）一律拒绝。
2. **时间单位显式**：不再用全量 `max/median` 推断（会让同一序列的前缀与全量判成不同单位）。
   逐值区间校验；多币单位不一致 ⇒ 拒绝，不静默归一。
3. **开盘先于 bar 内事件**：`A 开盘（锁利生效→资金费）→ B 开盘跳空退出 → C 开盘新开仓 →
   D bar 内止损（含当根新开仓）→ E 收盘盯市`。
4. **跳空退出不用未来 low**：相位 B 只读 `open`；bar 内止损在开仓之后发生，其释放的现金
   不可能被同一根 bar 的开仓使用。
5. **真实成交价后再算风险/止损**：先取整成交，再由成交价推止损与单位风险。
6. **`force_close_at_end=False` 仍返回真实 MTM 权益**，且恒等于权益曲线末点。
7. **入场费用只记一次**；未实现盈亏只估出场费用。
8. **费用率不硬编码**：附件 `_compute_unrealized_pnl` 写死的 `0.0005` 改为 `cfg.fee_rate`。
9. **期末对账**：现金 / 毛盈亏 / 净盈亏 / 资金费 / 权益四项恒等式（`result["reconcile"]`）。
10. **资金费模型显式**：默认 `settlement_events`（消费真实 `(time, rate, mark)`，缺字段记
    `funding_gap`，**不补 0**）；`assumption_fixed_8h` 是**显式假设模型**，结果恒带
    `funding_is_assumption=True`，不得冒充真实历史结算。
11. **缺 OI / 预热不足不伪造 0.5**：`oi_resonance_score = NaN` + `available=False` +
    `quality=<原因>`；仅在显式设门槛（`oi_min_resonance > 0`）时 fail-closed 拒单。
12. **加权因子保留原始分项 / 权重 / available / quality**，不冒充旧 M4/M5/M6。

### 2026-10-16 补丁轮（第二轮）

13. **锁利通道加当期列**：`core` 输出 `exit_low_curr` / `exit_high_curr`（当期 rolling，**不 shift**），
    与 `*_prev` 并存；执行层**优先 curr、缺列回退 prev**，按「信号 bar 收盘确认 → **下一根 15m**
    才生效」映射。
14. **fresh breakout 先于因子门禁登记**：`is_fresh = (c[i] > dc_high_prev[i]) ∧
    (c[i-1] <= dc_high_prev[i-1] ∨ dc_high_prev[i-1] 非有限)`；仅 fresh 更新
    `last_bo_idx/level/low`，突破分支**不再**重复覆盖锚点 ⇒ 低 OI 不阻断锚点、连续突破不重锚。
15. **SC 先于 Spring**（同根同时满足选 SC）。
16. **组合风险改为入场成本本金口径**：`long exp_exit=stop×(1−slip), loss=(entry−exp_exit)×qty`；
    `short exp_exit=stop×(1+slip), loss=(exp_exit−entry)×qty`；**删除** `max/min(entry, last_mark)`
    基准 ⇒ 浮盈不再放大风险预算，锁利后本金风险为 0。
17. **因子分项按附件定义**：0.50 分项 = **原始 `open_interest`** 的
    `rolling(window, min_periods).rank(pct=True)`，命名 `f_oi_level_rank`（**不是** OI 变化率分位，
    **不** shift1）；div/flux 亦取各自当期 rolling 分位；`oi_chg_20` 仅作**独立观察列**。
18. **去掉 LPS 的额外过滤**：删除附件未要求的 `close_pos >= 0.5`。
19. **入场费用只记一次**：`sum_entry_fee` 在开仓时计入，平仓分支不再重复累加（该缺陷由
    权益恒等式测试实际捕获并修复）。
20. **`pct_change(fill_method=None)`**：缺值不 pad，如实留 NaN。

## 门禁

`registry_entry.formal_run_gate()`：`formal_run_allowed=False`。正式 M4 仍需
「唯一合格父赢家 + 共同样本 + 同样本父级对照」，G01–G05 未过 ⇒ **禁止正式运行**。
AI 只生成候选；确定性风控有最终权限。真实下单**恒禁用**（本包无任何下单路径，
`execution.assert_live_disabled()` 为硬闸）。

## 旧实现的封存位置

`../_legacy_archive_v31/`（12 个文件，逐字节一致，含 4 个替换前未提交文件），
证据清单：`../_legacy_archive_v31/ARCHIVE_MANIFEST.json`。
