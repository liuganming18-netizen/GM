# WORK-03 证据索引

审计目标：`/Users/boos/Desktop/威科夫因子共振趋势`，HEAD `64a447f433963ad917b4bb464bcece7e4805d1a4`。本目录文件由审计方编写，不复用施工方 PASS 结论。

## 取证命令与结果

| 类别 | 命令/输入 | 本次结果 |
|---|---|---|
| 工作区 | `git status --short; git branch --show-current; git rev-parse HEAD; git log --oneline -10` | `wyckoff-v3.1-main`；125 项预存变化：123 个快照 JSON，2 个未跟踪项；已跟踪业务源码修改 0 |
| 调用链 | `rg -n 'oi_resonance_events_for|arm_events_for|WX.run|run_cycle' 框架/backend/live_signal_engine.py 框架/backend/live_worker.py` | `run_cycle:463` 调 `arm_events_for`，后者 `169` 调 `WX.run`；新函数 `149` 未在主循环调用 |
| 共核 | `rg -n 'trading_core' 框架/backend 策略/WyckoffMaster -g '*.py'` | 仅 `trading_core/contracts.py` 自身命中；三个生产入口没有引用 |
| 幽灵依赖 | `rg -n 'WorkBuddy|worktree|/Users/boos/' 框架/backend 策略/WyckoffMaster -g '*.py'` | 生产源码存在多处绝对路径；测试、Legacy、注释已与生产调用区分 |
| OI A/B | 项目 Python `-B`，`core.compute_signals`，同一 `signal_bars.csv` 正常与去掉 `open_interest` | 414 根；各 30 个信号；signal/direction/stop_price/reject_reason/confirm_time 各 0 差异 |
| 信号前缀 | 同一 CSV 前 366 根对比完整 414 根的前 366 根 | 上述五列各 0 差异；仅信号层 |
| Golden 独立算术 | Python 仅用 `golden_reference.json` 导出交易行及标准算术公式 | 7 笔，gross -2684.8608859243955，fees 389.5084557467661，funding 7.038892807413944，final cash 96918.59176552143；逐笔最大误差 0 |
| UI 真值 | `DataCenterView.tsx:144-167,410` | API 无 feeds 时落入写死绿色健康记录；全库笔数亦有固定兜底 |
| Golden checks | `docs/evidence/backtest_golden_2026-10-16/verify_run_ledger_output.txt` | 自报 32 PASS；本审计未把它当成独立证明 |

## 输入与限制

- A/B 与前缀输入：`回测日志/oi_resonance_v1/runs/bt_20261016T063427Z_810f1a3b/inputs/signal_bars.csv`。只读运行，未改 CSV。
- 数学输入：`docs/evidence/backtest_golden_2026-10-16/golden_reference.json` 的 `trades`、`equity.last`、`result`。由项目导出的数据，独立复算公式，不保证原始来源真实性。
- 未运行真实订单、服务重启、kill -9、持久账本双投、完整 Order/Position/Trade 前缀比对。对应 Gate 维持 FAIL/PARTIAL/NOT TESTED，不默认为 PASS。
