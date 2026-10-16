# PREVIOUS_GOLDEN 标记（W2-11）

> **本目录的 `golden_reference.json` 已降级为 `PREVIOUS_GOLDEN`（历史对照），不再是
> 「字节级必须一致」的活动基线。** 本文件是**加性标记**，不修改、不覆盖、不删除
> `golden_reference.json` / `ledger_summary.json` 的任何一个字节。

- 标记日期：2026-10-16
- 标记施工单：`WORK-02｜新策略净核 + Shared Trading Core 前置施工单`（第 W2-11 节）
- 标记窗口：`evidence/Work_策略核心收敛_2026-10-16/`

---

## 1. 为什么降级

WORK-02 把 `WX_OI_RESONANCE_V1` 的 `Strategy → Signal → Execution` 收敛为
Backtest / Paper / Live 三端共用的正式核心契约（`策略/WyckoffMaster/trading_core/`）。
这必然改变两项**身份指纹**：

| 指纹 | 为什么会变 |
|---|---|
| `code_sha256` | 执行核心代码整体重构（`execution.py` 由单体改为驱动层 + 新增共享核心包） |
| `signal_params_hash` | W2-1：`oi_min_resonance` 由 `0.0` 改为 `None`（UNSET）；W2-5：`SignalParams` 新增 `oi_publication_lag_ms` 字段 |

`signal_params_hash` 的变化**已被逐位证明只来自这两处**（见第 3 节）。

## 2. 行为是否变过？没有

`reproduce_golden_backtest.py` 报 FAIL，但其**核心判据**差异只有 4 项，
**全部是身份哈希**；窗口、权益、费用、资金费、回撤、各类计数、
以及**逐笔内容**（entry/exit 时间与价格、qty、net_pnl、exit_reason）**零差异**。

分类闸门（可复跑）：

```sh
cd /Users/boos/Desktop/威科夫因子共振趋势
PYTHONDONTWRITEBYTECODE=1 <py> \
  evidence/Work_策略核心收敛_2026-10-16/工具_Golden差异分类.py \
  --json evidence/Work_策略核心收敛_2026-10-16/Golden差异分类_2026-10-16.json
# 退出码 0 = 行为类差异 0 项
```

结论（2026-10-16 实跑）：**行为类 0 项 ⇒ PASS**。

## 3. `signal_params_hash` 变化的逐位复原证明

把当前 `SignalParams` 身份块**去掉** `oi_publication_lag_ms`、并把
`oi_min_resonance` 改回 `0.0`，用**同一个** `SignalParams.hash()` 算法重算：

```
hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
  ⇒ 0ba4b7918356b66415416504ae63f9f99786c73ee410fcc69182e9678aee21db
  == golden_reference.json 的 strategy.signal_params_hash（逐位相同）
```

⇒ 该哈希的变化**唯一**来自 W2-1 与 W2-5 两处字段变更，无任何第三处参数漂移。

**语义等价说明**（为什么这不改变行为）：

- 旧值 `oi_min_resonance = 0.0` ⇒ `oi_gate_enabled = False`（0 不构成门槛）；
- 新值 `oi_min_resonance = None`（UNSET）⇒ `oi_gate_enabled = False`；
- 新字段 `oi_publication_lag_ms = None` ⇒ PIT 标记 `pit_proven=False`，
  但**未声明延迟时逐 bar 不改变策略规则**（由 `formal_run_gate()` 在**运行级**拒绝正式生产）。

三者在**回测信号产出**上完全等价 ⇒ 逐笔结果不变。

## 4. 是否必须重新生成？—— 现在不生成，收敛系列结束后生成

**判断：当前不重新生成。** 理由：

1. **活动基线不能是移动靶。** 本 Work 之后仍待办的事项会再次改变身份：
   - W2-4 的 `available_at` / `trade_time` 尚未写入 `backtest_runner` 的信号表；
   - 策略核心仍在收敛（Paper / Live 端接线未完成）。
   现在生成 ⇒ 下次改动又要重置一次控制基线，等于**反复清零**。
2. **旧基线的行为判据仍然完全有效**，且已由分类闸门转成「行为 0 差异」的可执行判据
   （见第 2 节），不需要靠「重新生成」来获得绿灯。
3. 生成新 Golden 属**控制基线重置**，须由施工方批准后执行，不得由施工窗口自行宣布 PASS。

**推荐执行时机**：核心收敛系列的**最后一个 Work** 收尾时，一次性生成并独立核验。

**届时执行步骤（已就绪，勿在未经批准时运行）**：

```sh
cd /Users/boos/Desktop/威科夫因子共振趋势/框架/backend
# 1) 跑一次回测，拿到新 run 的完整结果（脚本会写入一个新 run 目录）
PYTHONDONTWRITEBYTECODE=1 <py> ../../docs/evidence/backtest_golden_2026-10-16/reproduce_golden_backtest.py
# 2) 以该 run 的结果体替换 golden_reference.json（**新文件名，不覆盖旧文件**），
#    旧文件改名为 golden_reference_PREVIOUS_GOLDEN_2026-10-16.json
# 3) 用同一个分类闸门对新 golden 复跑一次，要求「身份 0 差异 + 行为 0 差异」双零
```

## 5. 本标记的边界

- 本文件**只做标记与举证**；`golden_reference.json` 保持原样，仍可被任何工具读取比对。
- 任何「把 golden 改成 PASS」的行为（改数值、改哈希、删判据）均被禁止。
- 旧基线的**行为判据**继续作为活动控制使用，直到新基线生成并被批准。
