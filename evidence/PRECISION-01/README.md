# PRECISION-01 · 修复历史规格放行逻辑（证据目录）

窗口：Work 窗口 1 ｜ 任务编号：PRECISION-01 ｜ 日期：2026-10-17

## 一句话结论

**没有把任何未知历史规格升级成已验证。** 六个样本标的修复后仍全部
`unverified_historical` / `formal_pass_allowed=false`；同时修掉了旧实现里
「只看两端快照就判 verified」和「把窗口之后的规格套到历史窗口上」两个真实错误，
并让每条结论都能指着具体文件、sha256 和四类时间复核。

## 改了什么（只动这两个文件）

| 文件 | 改动 |
| --- | --- |
| `策略/WyckoffMaster/event_trend_v0_1/instrument_spec.py` | 重写历史适用性判定 + 数量约束合成 + 四类时间分离 |
| `策略/WyckoffMaster/event_trend_v0_1/tests/test_spec_historical_applicability.py` | 新增 35 个测试 |

未改：`engine_adapter.py` / `runner.py` / `trading_core/*` / 任何其他窗口负责的文件。
`resolve_instrument_spec()` 的签名与关键字默认值**逐字未变**（有测试守着）。

## 交付物

| 文件 | 内容 |
| --- | --- |
| `git_diff.patch` | 两个文件的完整 diff（含未跟踪文件） |
| `测试结果_门禁.txt` | 门禁测试完整输出：**93 passed**（原有 58 + 新增 35） |
| `判定规则表.md` | R0–R15 判定顺序、数值来源规则、四类时间规则、数量约束规则 |
| `六标的规格状态变化.md` / `.json` | LPT/OP/ORDI/ACE/BNB/MOVR 修复前后逐项对比 |
| `旧实现误判复现.md` / `.json` | 用合成证据证明旧实现会误判 verified（A/B/C 三场景） |
| `工具_六标的状态对比.py` | 可复跑：真实证据目录 + 真实窗口 |
| `工具_旧实现误判复现.py` | 可复跑：合成证据三场景，新旧实现对照 |
| `before/instrument_spec_orig.py` | 改动前逐字节副本（diff 基线） |
| `合成证据/` | 三个合成夹具目录（A 两端相同中间变更 / B 窗口后改规格 / C 证据齐备） |

复跑：

```bash
PY=/Users/boos/.workbuddy-ai/binaries/python/envs/default/bin/python
$PY -m pytest 策略/WyckoffMaster/event_trend_v0_1/tests/ \
    策略/WyckoffMaster/test_work02_core_contract.py --basetemp=.pytest_tmp_prc01 -q
$PY evidence/PRECISION-01/工具_六标的状态对比.py
$PY evidence/PRECISION-01/工具_旧实现误判复现.py
```

> 注：`--basetemp` 是因为本机沙箱不允许 pytest 在 `/private/var/folders` 下新建
> `pytest-of-root`。测试本身用的是标准 `tmp_path`，普通机器上不需要这个参数。

## 修掉的三个具体错误

### 1. verified 分支没有逐一比较「所选后侧快照」

旧代码：

```python
fb = _spec_fields_from_filters(b.symbols[symbol], order_type)
if _same_spec(fb, fields):        # fields 来自**全局最新**快照，不是 a
    return _with(spec, VERIFIED, ...)
```

- 比较对象是「窗口前快照 `b`」vs「**全局最新**快照」，而不是选中的后侧快照 `a`；
- `[b, a]` 之间的中间快照**一份都没看**。两端相同完全可以对应
  `1e-5 → 1e-3 → 1e-5`，旧代码会判 verified。

修复后：`b`、`a` 与区间内**每一份**快照逐一比较，任何一份不同即
`spec_changed_inside_span`。

### 2. 快照选择取「全局最新」

历史窗口的规格数值被 2026 年的快照覆盖（`旧实现误判复现.md` 的 B 场景：
2024 年窗口报出 2025 年才生效的 `tick=0.001`）。修复后取「窗口结束前最近的一份」；
窗口内规格不唯一时改报「窗口起点时的规格」，并注明仅作诊断。

### 3. 数量步长「取较大值」当作同时满足

`LOT_SIZE.step=2` 与 `MARKET_LOT_SIZE.step=3` 同时适用时，合法数量必须同时是
2 和 3 的倍数。旧代码 `max(2,3)=3` 会放过 qty=3（不是 2 的倍数）⇒ 下单必被拒。
修复后取**最小公倍数** 6，并提供 `check_quantity()` 逐条复核每条过滤器的
`stepSize / minQty / maxQty / minNotional`。

> 现有 920 个合约两种 stepSize **全部相同**，所以这一项对当前数据零数值影响；
> 是为将来不同步长时不留口子。

## 为什么 `verified` 现在几乎不可达 —— 这是对的

`verified` 需要「覆盖窗口的**变更证据**」（`数据/contracts/旧标的池/spec_changes_*.json`）。
该文件目前**不存在**，因此所有历史窗口都是 `unverified_historical`。

这不是新引入的保守，而是把旧代码里「隐式的、错误的 verified」换成了「显式的、可补齐的
缺证据」。补齐路径很明确：抓交易所合约变更公告 → 生成 `spec_changes_*.json`
（`kind=no_change` 或 `kind=change` + `effective_ms` + `source_url` + `evidence_sha256`）。

## 最小变更建议（未实施，属于其他窗口的文件）

1. `策略/WyckoffMaster/trading_core/engine.py`（约 527–553 行）：入场只校验
   `qty_step` / `min_qty` / `min_notional`，**未校验 `max_qty`**。
   建议改为调用 `spec.check_quantity(qty_rounded, entry_fill)`，超上限时以
   `qty_above_max_step` 拒绝。约 3 行。
2. `框架/backend/strategies/wx_execution.py`（1662/1669 行）：第二套取整实现且
   `price_step` 默认 `0.0`（0 = 不取整），与 `trading_core/fill.py` 的唯一实现重复。
   建议收口到 `round_price_to_tick`。
3. 变更证据采集脚本（新文件）：从交易所公告接口抓取并落 `spec_changes_*.json`，
   否则 `verified` 永远不可达。

以上均**未修改**，因为不在本窗口的允许改动范围内。

## 风险与边界（必须一起读）

- 本次没有改变任何成交结果：六标的的五个数值字段前后完全一致（`六标的规格状态变化.md`）；
  `qty_step` 语义由 max 改为 lcm，对现有 920 个合约数值等价。
- 本次**没有**声称「真实历史盈亏已修正」。`formal_pass_allowed` 依旧全为 `false`。
- `identity()` 故意不含 `evidence_chain` / `coverage` / `change_evidence`：
  它们是判定过程，不是撮合数值；塞进指纹会让 375 个历史 run 的指纹全部漂移，
  却不改变任何成交结果。
- 8787 服务**未重启**（按约定本窗口不动）。本模块的改动要等下次重载才在 API 侧生效。
