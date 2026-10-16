# Backtest Golden Reference · 2026-10-16

> **本目录是 `WX_OI_RESONANCE_V1` 回测的黄金基准（Golden Reference）。**
> 任何 Trading Core / 策略 / 执行引擎 / 风控重构之后，都必须复跑本案例并与本目录
> 逐项 diff。**diff 不通过 = 重构未完成。**

---

## 1. 为什么它可以作为 Golden Reference

不是因为它「跑通了」，而是因为下面五条**同时成立**：

| # | 条件 | 证据 |
|---|---|---|
| 1 | **可重复运行**，且不依赖任何后端进程 | 直接调 `backtest_api.create_run()`；202 受理 → succeeded，实测 2.0–2.1 秒 |
| 2 | **结果逐位确定** | 独立复跑一次，`final_equity` 等 18 项指标**完全相同**（相对误差 < 1e-9） |
| 3 | **逐笔可核** | 7 笔交易的时点、价格、数量、费用、资金费、净盈亏、退出原因**逐笔完全一致** |
| 4 | **账本自洽** | 独立核验脚本 `verify_run_ledger.py` 对账本**独立重算**：**32 PASS / 0 FAIL** |
| 5 | **输入可追溯** | 输入快照 6 份全部落盘 + SHA256；代码/参数哈希（`code_sha256` 等）已固化 |

> 第 2、3 条是**实测**结果，不是推断。复跑记录见 `reproduce_golden_backtest.py` 的 PASS 输出。

---

## 2. 基准案例（Benchmark Case）

### 2.1 运行身份

| 项 | 值 |
|---|---|
| run_id | `bt_20261016T063427Z_810f1a3b` |
| 运行时刻 | 2026-10-16 14:34（UTC+8） |
| 运行时刻 HEAD | `6220205` |
| 策略 | `WX_OI_RESONANCE_V1`（key `wx_oi_resonance_v1`，族 `F_OI_RES`） |
| signal_version | `oi_res_v1.0.0` |
| core_version | `oi_res_core_v1.0.0` |
| exec_core_version | `oi_res_exec_v1.0.0` |
| **code_sha256** | `364aacfcd65c45e18106b9d2bac3200f21aa1bbdb4750a3f598ad07045fa338a` |
| signal_params_hash | `0ba4b7918356b66415416504ae63f9f99786c73ee410fcc69182e9678aee21db` |
| exec_params_hash | `a92639c0a2b51e3df59e1d7fc78362611fcc91f0a97ba5310c06f0f24e5b99a5` |

> **commit 语义**：本目录建档时刻（15:02）HEAD 已前进到 `801a162`（另一窗口的补缺落库收尾提交，
> 只改 evidence/快照，**未触及 框架/策略/UI 任何代码**）。
> 因此**代码身份以 `code_sha256` 为准**（策略包文件内容哈希），与 git commit 解耦。

### 2.2 市场与窗口

| 项 | 值 |
|---|---|
| exchange / market | `binance` / `um`（USD-M 永续） |
| symbol | `BTCUSDT` |
| signal_timeframe / exec_timeframe | `1h` / `15m` |
| 窗口模式 | 原始 run = `auto_recent_days`（7 天）；Golden 复跑改用**显式窗口** |
| **交易窗口** | `2026-10-09T00:00:00Z` → `2026-10-16T00:00:00Z`（半开 `[start, end)`） |
| win_start_ms / win_end_ms | `1791504000000` / `1792108800000` |
| 预热 | `warmup_bars = 240`（只喂数据，不交易） |
| signal_series_start_ms | `1790640000000`（2026-09-29T00:00:00Z） |

### 2.3 成本模型（币安 VIP0）

| 项 | 值 |
|---|---|
| 初始权益 | `100,000.0` |
| 单笔风险 | `risk_per_trade = 0.008`（0.8%） |
| 最大并发 | `max_concurrent_trades = 3` |
| 单币杠杆上限 | `max_leverage_per_symbol = 2.0`（**实际未用满**，单笔最大名义 ≈ 79,576 ≈ 权益 79.6%） |
| 组合保证金上限 | `max_portfolio_margin_pct = 0.4` |
| Maker / Taker | `0.0002` / `0.0005` |
| 生效费率 | `fee_rate = 0.0005` |
| 滑点 | `slippage_bps = 3.0`（单边 3 bp） |
| 资金费模型 | `settlement_events`（**真实结算事件**，非固定 8h、不补 0） |
| 收尾 | `force_close_at_end = true`（窗口末强制平仓 ⇒ 期末无未平仓） |
| fee_tier | `Binance_VIP0` |

---

## 3. 结果快照

### 3.1 计数

| 项 | 值 |
|---|---|
| signal_count | **30** |
| order_count | **14** |
| fill_count | **14** |
| trade_count | **7** |
| rejected_count | **5** |
| funding_event_count | **7** |
| equity_points | **672** |

### 3.2 绩效

| 项 | 值 |
|---|---|
| 初始权益 | `100,000.0` |
| **最终权益** | `96,918.59176552141` |
| **已实现盈亏** | `-3,081.4082344785857` |
| 净收益率 | `-3.0814082344785856 %` |
| 未实现盈亏（期末） | `0.0`（窗口末强制平仓） |
| 毛盈亏 | `-2,684.8608859243955` |
| 手续费合计 | `389.5084557467661`（入场 `195.42544309486414` + 出场 `194.08301265190198`） |
| 资金费合计 | `7.038892807413944` |
| 交易笔数 | `7` |
| 胜 / 负 / 平 | `0 / 7 / 0` |
| 胜率 | `0.0 %` |
| 盈亏比 | `0.0`（无盈利笔） |
| **最大回撤** | `3.1234770061238812 %` |

> ⚠ **这是研究基准，不是「好策略」证明。** 7 天窗口、7 笔全亏、胜率 0%、
> 且信号层 `oi_quality` 全部为 `partial_insufficient_history`（OI 因子历史不足）。
> 它作为 Golden 的价值在于**可复现、可逐笔核**，而不是绩效好看。

### 3.3 账本分布

```
signals.kind      : donchian_breakout 16 | lps_pullback 8 | sc_reversal 4 | spring 2
signals.in_window : 1 → 12 笔（窗口内）| 0 → 18 笔（预热/窗口外）
orders.phase      : C_entry 7 | D_intrabar 6 | EOD 1
orders.action     : entry 7 | exit 6 | force_close_eod 1
trades.exit_reason: intrabar_stop 6 | segment_end_force_close_last_15m_close 1
rejected.reason   : duplicate_symbol_active ×5
```

---

## 4. 独立核验（32 项）

`verify_run_ledger.py` 对账本做**独立重算**（不复用回测运行器的任何指标/资金费函数）：

```
=== 结果：32 PASS / 0 FAIL ===
```

原始输出：`verify_run_ledger_output.txt`

覆盖的判据（摘要）：逐笔 `net = gross − entry_fee − exit_fee − funding`；
资金费三方一致（账本行 / 原始毫秒事件 / 独立重算）；结算覆盖网格；现金恒等式；
权益恒等式；权益曲线等间隔 15m；指标重算逐项比对；窗口纪律（预热不交易、窗口外信号未交易）；
输入快照 SHA == manifest；代码 SHA == 账本 meta；quality=ok 一致性；同成本买入持有基准自洽。

---

## 5. 文件清单

| 文件 | 内容 |
|---|---|
| `golden_reference.json` | **机器可读全量基准**（身份/窗口/成本/参数/指标/账本分布/7 笔明细/14 单明细/输入 manifest） |
| `ledger_summary.json` | 账本汇总 + 逐笔/逐单**指纹**（供 diff 用） |
| `verify_run_ledger_output.txt` | 32 项独立核验原始输出 |
| `reproduce_golden_backtest.py` | **复跑 + 逐项 diff 脚本**（退出码 0 = 一致，1 = 有差异） |
| `README.md` | 本文件 |

---

## 6. 如何复跑

```bash
cd 框架/backend
PYTHONDONTWRITEBYTECODE=1 \
/Users/boos/.workbuddy-ai/binaries/python/envs/default/bin/python \
  ../../docs/evidence/backtest_golden_2026-10-16/reproduce_golden_backtest.py
echo "exit=$?"
```

**退出码 0 ⇒ 核心判据全部一致；退出码 1 ⇒ 有实质差异（明细打印在 stdout）。**

### 判据分层（脚本内已实现）

**核心判据**（任一不符即 FAIL）：

- 身份：`code_sha256` / `signal_params_hash` / `exec_params_hash` / 三个 version
- 窗口：`win_start_ms` / `win_end_ms` / `signal_series_start_ms` / `warmup_bars`
- 指标：18 项（见 `golden_reference.json` 的 `result.metrics_raw`）
- 计数：signals / orders / trades / funding / equity / rejected
- **逐笔内容**：7 笔的时点、价格、数量、净盈亏、退出原因；14 单的 side/phase/ts/action/reason/price/qty/fee

**已知口径差**（记录但不判失败，原因已写明）：

| 项 | 原因 |
|---|---|
| `trade_id` / `order_id` | 生成式**输入含 `run_id`**（见 `backtest_store.new_trade_id` / `new_order_id`）⇒ 每次运行必然不同，属设计 |
| `input_sha256` / `signal_frame_sha256` | 原 run 用 `auto` 窗口（末端=运行时刻最后已闭合 bar），复跑用**显式窗口**（否则窗口漂移不可复现）。交易窗口相同，但「窗口末端→数据末端」的收尾段长度不同 |
| `data_end_ms` / `sig_end_ms` / `exec_end_ms` | 同上 |

> **为什么必须用显式窗口**：`auto / window_days=7` 按「当前时刻」回推，**每跑一次窗口都漂移**，
> 结果必然不同，根本无法作为基准。显式窗口才是可复现的锚。

---

## 7. 已知限制（不掩盖）

1. **窗口仅 7 天、7 笔交易** —— 样本量不足以支撑任何统计结论，只作工程基准。
2. **OI 因子历史不足** —— 30 条信号的 `oi_quality` 全为 `partial_insufficient_history`。
3. **正式 M4 门禁未过** —— run.json 的 `formal_m4_gate.formal_run_allowed = false`，
   G01–G05 全 `pending`；本基准**不代表** M4 通过。
4. **不继承旧成绩** —— 与 3a–3d / M0–M6 无任何继承关系。
5. **输入快照哈希不是复现判据** —— 见上文「已知口径差」。
