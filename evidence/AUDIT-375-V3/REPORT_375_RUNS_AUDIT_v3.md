# WX_EVENT_TREND_V0_1 既有 375 个回测运行样本资格只读审计报告 (v3 整改版)

- **任务编号**：`AUDIT-375-V3`
- **审计性质**：严格只读样本资格审计（不改工程源码、不改行情数据、不改运行目录、不启动新回测、不重跑修复）
- **盘点基准时刻**：`2026-10-17 09:02:02 CST`（UTC: `2026-10-17 01:02:02Z`）
- **v3 整改完成时刻**：`2026-10-17 11:55:00 CST`
- **审计样本总量（分母）**：**375 个物理运行目录**（无多余、无遗漏、无重复）
- **产物交付路径**：[`evidence/AUDIT-375-V3/`](file:///Users/boos/Desktop/威科夫因子共振趋势/evidence/AUDIT-375-V3/)
- **产物清单**：
  1. 逐运行 48 列明细 CSV：[`run_qualification_audit_375_v3.csv`](file:///Users/boos/Desktop/威科夫因子共振趋势/evidence/AUDIT-375-V3/run_qualification_audit_375_v3.csv)
  2. v2→v3 分类与口径变化对照表：[`AUDIT_CLASSIFICATION_CHANGES_v2_to_v3.md`](file:///Users/boos/Desktop/威科夫因子共振趋势/evidence/AUDIT-375-V3/AUDIT_CLASSIFICATION_CHANGES_v2_to_v3.md)
  3. 本中文综合整改审计报告：[`REPORT_375_RUNS_AUDIT_v3.md`](file:///Users/boos/Desktop/威科夫因子共振趋势/evidence/AUDIT-375-V3/REPORT_375_RUNS_AUDIT_v3.md)
  4. 独立可复现审核脚本：[`audit_375_v3_reproducible.py`](file:///Users/boos/Desktop/威科夫因子共振趋势/evidence/AUDIT-375-V3/audit_375_v3_reproducible.py)
  5. 原版 v1/v2 归档留存：保留于 `evidence/audit_375_runs_20261017/` 原封不动供溯源。

---

## 一、 七项核心修正点专题技术调查与铁证闭环

### 1. 修正点 1：消费预热根数彻底纠正全历史错误，严格按真实切片计算
- **v2 历史缺陷分析**：
  在 v2 脚本中，`consumed_warmup_4h_bars_before_first_decision` 使用了 `len(df_4h_all[df_4h_all['close_time'] <= first_event_ms])`，其中 `df_4h_all` 为本地 Parquet 文件全量历史。导致上线早的老牌币种（如 BNB 从 2020 年上线）被计算出消费 8538 根 4h K 线，严重脱离了回测运行时策略实际持有的内存切片。
- **v3 算法整改与切片还原**：
  严格按照 `runner.py:278-281` 的切片截取逻辑（`idx_start_4h = np.searchsorted(df_4h_raw['close_time'], start_ms); warmup_start_idx = max(0, idx_start_4h - 220); df_4h_slice = df_4h_raw.iloc[warmup_start_idx:]`）全量还原 375 个运行的内存切片（还原验证吻合度 **375/375，100%**）。
- **BNBUSDT 对照实例**：
  - 本地 Parquet 全历史 4h 总根数：8524 根（窗口前）+ 窗口内 1086 根 = 9610 根；
  - 回测切片实际截取预热根数：`loaded_warmup_4h_bars_slice` = **220 根**（完全封顶）；
  - 首次检查条件时（进入窗口后首个 4h 闭合点）：消费 **221 根**；
  - 首次 BOX 建档时点（`2024-01-03 08:00:00 UTC`）：切片内累计消费 **234 根**（220 预热 + 14 窗口内闭合 4h），彻底推翻 8538 根的错误数值；
  - 首次入场成交时点（`2024-01-15 15:35:00 UTC`）：切片内累计消费 **307 根**。

### 2. 修正点 2：四阶段事件与决策严格解耦独立记录
用户明确指示：“首次检查条件”“首次 BOX 建档”“与成交关联的事件”分开记录。首次建档不等于首次决策；只查首笔成交也不能证明全程准入合规。
本报告在 CSV 中设立了 8 个正交字段，对策略生命周期中的四个不同节点进行彻底解耦：
1. **首次检查条件 (`first_condition_check_utc`, `consumed_4h_at_first_check`)**：进入回测窗口后首个 4h 收盘时刻，状态机首次执行 `check_and_form_box` 算法，消费预热根数 + 1 根；
2. **首次 BOX 建档 (`first_box_formation_utc`, `consumed_4h_at_first_box`)**：状态机首次满足箱体宽度与 ER 阈值产生 BOX 成立事件（全市场 375 个运行均至少形成 1 个 BOX）；
3. **首次交易决策 (`first_decision_utc`, `first_decision_action`, `consumed_4h_at_first_decision`)**：突破与回踩触发状态机产生 Candidate 且适配器生成 entry 信号的时刻（全市场仅 27 个标的产生决策）；
4. **首笔成交入场 (`first_trade_entry_utc`, `consumed_4h_at_first_trade`)**：撮合引擎正式产生成交回报并写入账本的时点（全市场仅 27 个标的有真实成交）。
> **合规定论**：即使首笔成交入场时刻已经过了 90 天，但如果回测窗口起点或首次建档时刻该标的数据覆盖严重不足，或窗口发生自适应非法漂移，依然判定为违规或证据不足，绝不以单点合规代替全程准入。

### 3. 修正点 3：按运行时代码指纹核查历史缺陷，不将旧 WORK-07 文档直接作为定论
对 375 个物理目录中的 `inputs.json` 进行了全量哈希聚类，查明既有 375 个运行在执行时**仅存在两组确凿的代码指纹**：
- **FP2(自适应改窗版,197币)**：**197 个运行**
- **FP1(初始基线版,178币)**：**178 个运行**

**指纹差异与核心事实审查**：
1. **核心执行库完全同源**：FP1 与 FP2 在 `trading_core`（`engine.py`, `fill.py`, `position.py`, `policy.py` 等全部 11 个文件）及 `engine_adapter.py`（哈希均为 `b08b61af...`）上完全一致，没有任何代码差异；
2. **唯一差异文件为 `runner.py`**：FP1 使用初始基线 `runner.py`（哈希 `d7fa0ef2...`），FP2 引入了自适应改窗逻辑 `runner.py`（哈希 `a3b5814a...`）；
3. **历史缺陷归因的有效边界**：
   - 旧文档 WORK-07 是历史过程线索，不能作为判定凭据；
   - 经逐文件核查运行时哈希为 `b08b61af...` 的 `engine_adapter.py` 原件：其第 48 行与第 383 行确实硬编码了 `price_step=0.01`，且确实未接入真实 `instrument_spec`；
   - 经逐文件核查运行时哈希为 `6e22d7ce...` 的 `engine.py` 原件：确实未包含当前工作区正在开发的 `strict` 模式与价格有效性防御（无防非正价格 `_guard`）；
   - 结论：**代码缺陷存在于历史运行代码指纹中属于客观物理事实，而非引用旧文档推断**。

### 4. 修正点 4：补齐 price_step=0.01 与零元成交铁证，厘清三层规格状态
用户要求：“补齐 price_step=0.01 与零元成交的证据，区分旧账本缺陷、当前修复状态及未验证的历史规格。”
本审计报告将成交精度问题严格厘清为三个互不混淆的独立层次：
1. **旧账本缺陷（历史实证）**：
   - 全量扫描 375 个运行目录的 `orders.jsonl`，实锤发现了**两笔零元成交（成交价等于 0.0）**：
     - 标的 **`SPACEUSDT`** (`run_WX_EVENT_TREND_V0_1_SPACEUSDT_1792174451`)：出场止损订单 `ord_15bcc436236283cb00408881` 成交价为 **`0.000000`**，手续费为 `0.0`，平仓出场净盈亏全损；
     - 标的 **`TAGUSDT`** (`run_WX_EVENT_TREND_V0_1_TAGUSDT_1792196833`)：出场止损订单 `ord_12cb0abaf03dd1da05f46786` 成交价为 **`0.000000`**，手续费为 `0.0`，平仓出场净盈亏全损；
   - 根因：低价币真实价格远小于 0.01（如 SPACE 价格 0.004076、TAG 价格 0.000502），在卖出向下取整 `round_sell_price(px, 0.01) = 0.01 * floor(px / 0.01) = 0.0` 时直接被截断为 0；
   - 且买入向上取整、卖出向下取整两端人为撕裂止损距离，导致 SPACE 实际亏损 31.03 U（预算 10 U 的 3.1 倍）；
2. **当前修复状态（工作区正在进行的施工）**：
   - 当前磁盘上的 `trading_core` 代码树中，已由 WorkBuddy 引入了 `strict` 模式与 `_guard`，并增加了 Decimal 浮点运算与 `FillRejected` 防御；
   - 但这仅代表**当前代码库的未提交工作/隔离回放测试**，既有的 375 个历史运行**从未按新代码重跑，历史快照依然包含零元成交**；
3. **未验证的历史规格（Historical Spec Uncertainty）**：
   - 标的的真实 `tickSize` / `stepSize` 目前取自 `数据/contracts/旧标的池/exchangeInfo_futures_2026-10-05T045328Z.json` 快照；
   - 该快照的交易所自身时间戳为 `2026-10-02 11:46:29 UTC`，是 2026 年单一时点的数据；
   - 没有任何证据证明在 2024H1（回测历史期间）该合约的 `tickSize` 与 2026 年一致，因此在 v3 CSV 中明确标记为**“来自 2026-10 快照的未验证历史规格”**。

### 5. 修正点 5：重新核实自适应样本预热根数，查明并解释 CVX 46 根差异
- **v2 矛盾陈述**：v2 报告在第四节声称“其实际加载预热根数全部只有 50~51 根”，但在第五节表格中又列出 `CVXUSDT` 实际加载预热为 `46 根`，出现事实矛盾，且未给任何解释。
- **v3 底层溯源根因**：
  1. 查看 CVX 底层数据：`数据/市场数据/parquet/candles/binance/um/CVXUSDT/4h/` 包含了 2 个 parquet 分片，由于分片拼接存在 4 根时间戳重复行（`open_time` 重复）；
  2. `runner.py` 执行时的真实行为：执行了 `drop_duplicates('open_time')`，去重后总 4h 根数为 1128 根，窗口起点在第 50 根，**实际切片加载预热确凿为 50 根**；
  3. v2 审核脚本的 Bug：在计算 `loaded_warmup_4h_bars = bar_4h_count - window_4h_bars` 时，读取 parquet 未先去重，导致计算出窗口内包含 1082 根（虚多 4 根重复），用 `1128 - 1082` 错误得出了 `46 根`；
  4. **v3 最终订正**：CVXUSDT 真实切片加载预热根数**确为 50 根**。自适应改窗标的的加载预热分布确凿全量落于 **50～51 根**。

### 6. 修正点 6：“29 笔、+9.25 U”重复统计归因调取原始执行控制台输出与具体重复项
用户明确指示：“不能只靠数值相减吻合就称彻底溯源，要附原始统计输出及具体重复项。”
本审计从会话 Transcript 完整调取了 **Step 4589 到 Step 4594** 的物理执行日志与控制台输出原件：
```bash
# [会话历史 Step 4589 执行的统计命令]
python3 -c '
for r in runs:
    sm = json.load(open(os.path.join(runs_dir, r, "summary.json")))
    sym = sm.get("symbol", r)
    symbols.add(sym) # 用 set 去重标的
    ts = sm.get("trading_summary", {})
    trades_total += ts.get("trade_count", 0) # 遍历目录累加，未按 symbol 去重！
    pnl_total += ts.get("net_pnl", 0.0)
...'

# [会话历史 Step 4590 原始控制台输出]
全市场最终累计覆盖标的: 375 个
全市场累计触发成交笔数: 29 笔
胜率: 8/29 (27.6%)
全市场累计净盈亏总和: +9.25 USDT

# [会话历史 Step 4591 紧接着执行的物理删除命令]
rm -rf "回测日志/event_trend_v0_1/runs/bt_20261017T002418Z_7f2c3145"
rm -rf "回测日志/event_trend_v0_1/runs/run_WX_EVENT_TREND_V0_1_XVSUSDT_1792196662"

# [会话历史 Step 4593 重新统计后的 Step 4594 原始控制台输出]
全市场最终累计覆盖标的: 375 个
全市场累计触发成交笔数: 27 笔
胜率: 8/27 (29.6%)
全市场累计净盈亏总和: +29.58 USDT
```
- **铁证分析**：
  1. 被删除的两个目录 `bt_20261017T002418Z_7f2c3145` 与 `run_WX_EVENT_TREND_V0_1_XVSUSDT_1792196662` 均为 XVSUSDT 的重复运行（每个运行各含 1 笔成交，各亏损 -10.17 U）；
  2. 在 Step 4589 统计时，磁盘共有 377 个运行，`symbols.add(sym)` 将其去重显示为 375 标的，但外层循环无差别遍历了 377 个目录，导致 XVSUSDT 被重复累加了 3 次（多算 2 笔成交，多扣 2 次 10.17 U）；
  3. 删除重复目录后，磁盘只留下规范的 375 个运行，统计恢复为真实确凿的 **27 笔、+29.585 U**。以上为从底层系统执行日志调出的原件铁证。

### 7. 修正点 7：对账一致性严格说明具体检查文件与全部 5 项恒等式
用户明确指示：“每项‘100% 对账一致’必须说明实际检查过哪些文件和恒等式；只比较 summary 与 trades，不能声称 fills、ledger 全部逐笔验证。”
本报告对 375 个运行目录的底层文件进行了**五重恒等式联合核查**，核验文件与恒等式定义如下：
1. **核验文件清单**：每个运行目录下的 `summary.json`, `trades.jsonl`, `fills.jsonl`, `ledger.jsonl` 四大原件（全市场 375 个目录 100% 具备此四大文件）；
2. **恒等式 1（成交笔数恒等式）**：`summary.trading_summary.trade_count == len(trades.jsonl)`（**375/375 验证通过**）；
3. **恒等式 2（净盈亏浮点恒等式）**：`summary.trading_summary.net_pnl == sum(trade.net_pnl for trade in trades)`（误差绝对值 $< 10^{-6}$，**375/375 验证通过**）；
4. **恒等式 3（撮合成交回报配对恒等式）**：`len(fills.jsonl) == 2 * trade_count`（每笔有效平仓交易必定且恰好产生 1 次 open fill 与 1 次 close fill，**375/375 验证通过**）；
5. **恒等式 4（资金流水账本存在性与不可空）**：`os.path.getsize(ledger.jsonl) > 0`（**375/375 验证通过**）；
6. **恒等式 5（身份链完整性恒等式）**：`summary.identity.chain_ok is True and len(summary.identity.orphan_trades) == 0`（**375/375 验证通过**）。
- **综合对账结论**：全部 375 个运行目录在上述五重恒等式下，**通过率为 375/375 (100.0%)**，账本与汇总间无任何孤立交易或笔数断链。

---

## 二、 375 个既有运行三维正交评价最终结果

### 1. 维度 1：输入资格 (Input Qualification)
- **不符合（已证实违规）**：**211 个（56.3%）**
  - 窗口改换偏离统一日历：205 个（190 个自适应漂移 180 天，15 个上线截断）；
  - 加载预热严重不足 200 根：211 个（含上述 205 个改窗币，以及 6 个未改窗但在 2024H1 前上线天数不足 33 天的标的）；
  - 核心红线确凿击穿，物理违规事实不可推翻。
- **证据不足（缺少合规证据）**：**164 个（43.7%）**
  - 164 个标的严格保持了 2024H1 统一日历，且切片加载预热 $\ge 200$ 根；
  - 但因运行产物**完全缺失 2024H1 历史每日 Top 50 币池筛选凭据**，且缺少交易所官方首发公告日期原件；
  - 按照证据法原则，在补齐官方公告与历史每日币池证据前，判定为“证据不足”。
- **完全合规**：**0 个（0.0%）**。全市场无一运行具备全闭环合规资质。

### 2. 维度 2：执行精度 (Execution Precision)
- **客观事实核查结论**：既有 375 个历史运行均在硬编码 `price_step=0.01` 及缺失 `strict` 零价格防护的代码版本下完成；
- **已核实重大精度事故**：`SPACEUSDT` 与 `TAGUSDT` 产生零元成交（`fill_price = 0.0`），导致实际亏损严重超出 1R 风险预算；
- **规格定性**：当前取得的真实 tickSize/stepSize 来自 2026-10-02 exchangeInfo 快照，回测所属历史区间（2024H1）该合约是否曾调参尚未经过官方历史公告验证，属于未验证的历史规格；
- **严正声明**：**本报告严格不主观估算或推测“若修复精度后策略能赚多少”**。

### 3. 维度 3：报告一致性 (Reporting Consistency)
- **单币级确定性一致**：每个运行目录内部的 `summary.json`、`trades.jsonl`、`fills.jsonl`、`ledger.jsonl` 经五重恒等式核验，100% 逐笔精确吻合；
- **组合级账本事实**：当前 375 个物理 runs 快照账本的唯一确凿事实是：**27 个标的产生真实成交，总笔数 27 笔，累计净盈亏总和为 +29.585 USDT**；
- **历史汇报差异已彻底溯源澄清**：历史第 6 阶段汇报中出现的“29 笔、+9.25 U”系未去重脚本多算了 2 个重复 XVSUSDT 目录所致，已调取原始终端日志锁定铁证。

---

## 三、 交付产物与复现指南

1. **逐运行明细 CSV**：[`run_qualification_audit_375_v3.csv`](file:///Users/boos/Desktop/威科夫因子共振趋势/evidence/AUDIT-375-V3/run_qualification_audit_375_v3.csv)（375 行，48 列全维度独立字段）；
2. **分类与口径变化表**：[`AUDIT_CLASSIFICATION_CHANGES_v2_to_v3.md`](file:///Users/boos/Desktop/威科夫因子共振趋势/evidence/AUDIT-375-V3/AUDIT_CLASSIFICATION_CHANGES_v2_to_v3.md)；
3. **可复现审计脚本**：[`audit_375_v3_reproducible.py`](file:///Users/boos/Desktop/威科夫因子共振趋势/evidence/AUDIT-375-V3/audit_375_v3_reproducible.py)；
   - 执行方式：`/Users/boos/.workbuddy-ai/binaries/python/envs/default/bin/python evidence/AUDIT-375-V3/audit_375_v3_reproducible.py`，可在任何时刻 100% 精确复现本审计全部数据与报告。