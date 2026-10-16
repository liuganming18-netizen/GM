# 阶段 A 独立复审退回及 Gemini 意见核验补充

本文件承接《Codex_项目闭环复审与整改合同_2026-10-15.md》，不替换已冻结策略规则。用户本轮确认：反重力在做隔离删除；本任务在 `/Users/boos/Desktop/威科夫因子共振趋势` 内工作。Work 实施，Codex 审核。旧目录只读取证，不同步写回，不碰反重力删除范围。若必要源文件处于删除/移动中，先报告实际路径与冲突，不自行删除、覆盖或清理。

## 当前判定：阶段 A 未通过，先修运行回归

核验时后端 PID 87244、前端 PID 87486 已运行于桌面 `框架/backend` 和 `UI界面/web`。Work 回执仍写旧目录 PID 86003/80976，不能作为当前运行证明。paper_engine/live_api 文件仍匹配回执 SHA；adapter 和总闸文件 SHA 已变化，须重新记录当前状态及改动来源，不能覆盖后续改动。

1. **P0 账本绑定回归**：`live_store.BASE=HERE.parent` 移至桌面后，默认 PAPER_DIR 变为 `框架/最新唐奇安威科夫接通与收尾_2026-10-11/量价信号接入与模拟历史并行43/纸面模拟43`。该路径四个库已存在，但各自 paper_position=0。旧库仍有 50/35/46/46 条记录、未作废在持仓 7/4/7/7。`/api/v1/paper/positions` 当前 total=0，属于读错账本，不能算清理成功。证据 `evidence/Codex_闭环复审_2026-10-15/migration_ledger_path_audit.json`。
   - 明确配置正式账本身份和路径；在桌面项目保全接通既有账本，使用 SQLite 一致性备份等现有能力处理可能的 WAL，不能裸拷贝活库主文件。原件只读、保留；不要将四账户合并为新账户，不重置现金。保留误建空库作为回归证据，不删除。
   - 正式服务库缺失/身份不符时必须报 unavailable，不得自动初始化四份5000U账户冒充原账户。证明旧记录逐表数量、关键金额及原行内容保持一致，再核对 API/UI。
   - Codex 已生成 `legacy_ledger_preservation_baseline.json`（同证据目录）：四臂 position/fill/funding/equity/realized/order/state 的逐行及整表SHA。迁移前先对照基线，迁移后证明旧行保全；若之后允许旧仓正常退出，须把迁移保全时刻与后续新增执行/状态更新分别列出，不能用行数相同代替内容守恒。
2. **P0 退出实际未获证明**：worker 仍未运行。代码未阻断 close/funding，只能说明路径允许，不能写“退出/资金费照常处理”。不冻结旧仓退出，不改历史；须证明真实旧仓维护进程、处理水位、资金费和时间顺序，闭环前新开仓持续阻断。不要为证明退出而人工平掉旧仓或回填价格。
3. **P1 审计不完整**：Work 仅列当前25仓/17延迟违规，Codex 已核全部161条未作废记录：89条关联 base_short 却按多仓、97条不足15m延迟。两类有重叠，不能相加。读 `ledger_causal_audit.json`，补全开/平历史、关联信号及审计旁表。优先追加旁表，不把全部违规记录改 voided，不改变已发生现金/费用。已 voided 的历史继续单列。
4. **P1 UI 不消费审计状态**：API 增加 excluded_from_official_strategy_performance 不等于页面实际剔除。PositionPanel 尚未按该状态标注/隔离；应明确旧版未验收账户与当前正式策略暂无合格表现，保留旧账户真实权益和持仓。不要简单删掉违规交易后伪造剩余净值。
5. **P1 去伪未完成**：桌面 BacktestReviewView.tsx 仍含 Math.sin BTC 基准、默认夏普2.34/胜率58.3、固定月度矩阵及默认2ATR调参状态。PositionPanel 仍自行算 suggestedStop。删除虚构数据与不起作用的调参入口；当前止损读账本/规则状态，缺值明确缺失。信号和成交不可混画。
6. **P1 健康假绿灯**：live/health 仍 quality=ok、age=null。状态过期/worker停运/读错账本必须准确暴露；市场 WebSocket 活跃不代表策略 worker 活跃。TTL 需绑定实际调度频率，不能只凭 PID 或一条200判健康。

总闸已观察为 false，保留。自动/手工/pending 在隔离库验证三路拒绝，严禁在生产切 true 做验证。参数还原为旧冻结保守值只属于止血，不等于 v3.1 同核验收；后续必需字段缺失应拒绝，不能长期使用 fallback。

## 对 Gemini 意见的核验与修订

已在源码独立确认：假BTC曲线/默认指标/月度矩阵；wx_execution.py:2538 每时间点扫描 entries；wx_execution.py:2984-2985 gross_profit/gross_loss 实际汇总 net_pnl；wx_ledger 每次写入 commit；`.gitignore:59 数据/` 导致 pool/schema 例外不生效（git check-ignore 已证实）。

以下不得照抄：

- Gemini 所称“回测死锁”和“115亿次”没有本轮日志/测量证明。性能整改须先记录真实窗口、事件数、bar数、耗时；复用按eligible分组字典，保留组内冻结排序、去重与同时间事件先后。批量事务需原子性、断点与崩溃恢复验证，不能只追求速度。
- 后端 live_api 已有区分 history/paper_review 的改动，不能重复恢复旧版本。当前 history 仍指向 L.BASE 下旧159币实验目录，必须核准真实 run_id/strategy_version/data_version/实验层；旧研究结果只标旧版，不能冒充 v3.1。
- 不能向所有表机械加 `COALESCE(voided,0)=0`。先检查 schema；paper_realized/order/equity 的过滤按身份和有效关系实施。正式净值若无法可靠剥离违规交易影响，显示未验收，不拼接一条貌似合规曲线。
- gross_profit/gross_loss 按 gross_pnl 重算时，同时明确胜负按毛利还是净利，收益因子/盈亏比的分子分母同口径；手续费、资金费符号保留，净利逐笔守恒。已有账本字段错误不直接重写历史，可在审计派生结果中纠正并保留原值。
- 月度收益应以同一账户的月界净值及资金流口径计算；按平仓时间汇总只能称“月度已实现损益”。未平浮盈/费用/资金费不得被漏掉。DD20%是相对历史峰值的回撤，不能画成固定初值80%线；在回撤图画阈值并标真实触发事件。
- range 切换要同时裁剪权益、指标、基准、明细及对比口径；保留窗口起点估值。缺行情/不足统计样本显示未知，禁止0/默认夏普代替。BTC基准只用所选窗口当时可得行情，未来追加不改变过去值。
- 后续 C 阶段补充的源码实证：live_api 的夏普主动剔除所有零收益日再乘 sqrt(365)，需改为完整等间隔有效净值收益；停机后的统计区间应明确，不能按收益是否为零来筛选。ALL 的收益因子/盈亏比当前为各臂比值算术平均、最大回撤为各臂最大值，均不能冒充合并组合指标；应按合并明细和同步净值重算。BTC基准将1h收盘值标在open_time并且 bisect 结果为负时强制取第一条，会把未来首条价格回填到更早时刻；应绑定真实close/available时间，早于首个可用点返回缺失，缓存也需失效更新。
- Gemini 的30天预热、停牌与退出枚举不能自动成为新规则；以已批准阈值/实际阶梯为准。M2/M2b结构止损和M3台阶退出不可统称3ATR。数据断档不等于官方停牌，不按未来缺口倒填强平。
- `.gitignore` 需逐级放行父目录并精确白名单化小型清单。仅把 `数据/` 改 `/数据/*` 仍可能屏蔽更深父目录。用 git check-ignore 和 staged清单验证；不得把大型行情/账本/缓存、凭据加入Git，本次不提交不推送。
- 实际前端根是 `UI界面/web`。视觉建议可在事实接通后复用现有图表与组件改善密度；不以整页视觉重构作为阻塞修复前置，不引入新框架，不复制“工业级”宣传口号。

## 本轮执行顺序与交付

独立补核策略模块：自带12项合成核验本轮重跑全部通过（`strategy_synthetic_verify.json`，bundle SHA `4cb4eff0772ac8b617393b52f0f9739718b371da6b7ce7cad5c1a8898ba3091c`）。但独立缺输入反例 `strategy_missing_input_repro.json` 发现 `evaluate_entry_fill` 在 structural/structural_m2b 缺 struct_low 时退回固定3ATR，并接受未定义 stop_mode；B阶段需按冻结规则拒绝缺结构/非法模式，除非有明确批准的回退口径。不能把这些缺失当 uniform 实验。

阶梯接线不得只补 levels 字段：run_ladder 读了 signal_module/config_sha，却没有实际把该模块/实验配置传到 RA 执行；ra_bridge 仍直接调用旧 RAARM.run_actual。需证明 M0/M1/M2/M2b/M3/M4 各自真实生效。M3 必须继承已选 M2/M2b 的止损模式，M4 继承已选父实验，仅改变排序，不能根据 experiments 中写死默认值擅自切回 structural/step_swing。选型标准沿用批准来源，未知不自定。

先完成 A 退回项：账本保全与身份绑定 → 保持服务端禁新仓 → 全量审计/真实状态展示/去伪 → 真实退出维护证据。阶段 A 合格后再推进原合同 B 同一v3.1核心、C事件链、D回放/流式逐笔一致性。无已选定正式策略与完整验收前，新epoch不开启。

本次新 Work 窗口，桌面目录唯一代码写入范围；先核对其他写者，不覆盖未归属改动。每阶段写 stage_status.json 但保留旧回执副本；记录真实当前 PID/cwd/SHA/账本realpath与身份、API body及刷新后页面证据。Codex 只独立核验和派工，不接管工程实施。
