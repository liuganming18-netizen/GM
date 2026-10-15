# 后端实现 · 第二批补丁（核查发现逐项修复）

**时间：**2026-10-05（北京时间）
**范围：**只修主助手核查发现的 5 项 + 附带项。**不扩功能、不改前端、不启动采集或交易。**

| 项 | 值 |
|---|---|
| 监听 | `http://127.0.0.1:8787` **与** `http://[::1]:8787`（双栈，均仅本机回环） |
| 进程 PID | 以**端口监听者**为准：`lsof -nP -iTCP:8787 -sTCP:LISTEN`（`backend.pid` 可能写的是包装进程号，不可信） |
| 唯一行情库 | `/Users/boos/.codex/worktrees/90b1/加密货币趋势判断系统/data/market.sqlite3` |
| 启动 / 停止 | `sh start.sh` / `sh stop.sh`（身份校验后仅停自有服务） |
| 验证 | `curl -s --noproxy '*' http://127.0.0.1:8787/api/health` |
| **Python** | **`/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3`** |

> ⚠️ **勿用系统 Python 3.9 复跑**：本包使用 PEP 604（`int | None`）类型语法，3.9 会在导入期报错，属环境不兼容而非测试失败。

---

## 一、逐项修复

**1. 配置校验（`strategies/common.py` + A/B + `registry.py`）**
新增 `validate_config`：类型、有限性、范围、窗口关系。
- 分位限 0–1；窗口/持续根数须**正整数**；容差 ≥0；放量倍数 **>0**；B 的 `min_history_bars ≤ lookback_bars`。
- 非法 → 新状态 **`配置无效`**（返回 `config_errors`，不当 ok、不抛 500/IndexError）。
- `quantile()` 防御性：非有限/越界 q 返回 None 或截断，不再 `IndexError`（复现 `buy_ratio_quantile=2` 已不抛异常）。
- 未冻结配置**只能显式研究试运行**；`--write` 仅限 `frozen=true`，拒绝写生产确认日志。

**2. 策略 A 回填风险（`strategy_a.py`）**
`_need_bars` 由 `rl+vl+2` 改为**必要下界** `max(rl,vl)+2`。旧值偏高，导致 6 根时误报“数据不足”、加到第 7 根却补出 `confirmed_at` 为第 6 根的事件。新增**逐前缀一致性**测试：`events(prefix) == events(full)` 中 `confirmed_at ≤ 前缀末根` 者。

**3. 策略 B 持平基线（`strategy_b.py`）**
异常改为“两维不低于基线 **且至少一维严格增强**”。常量序列（无增强）不再判异常；未引入任何未确认数值阈值。另：时间缺口（`open_time != 上一根 close_time+1`）断开异常段；主动买入字段缺失即断开。补常量/缺口/缺失字段测试。

**4. adapter 契约（`adapter.py`）**
- `rule-snapshots`：**精确匹配** `marker_kind`+`marker_time`（按证据时间字段）；查不到返回 `missing`，**不退回最新信号**；B 不默认贴 `retest`；`stop/target` 返回 `not_connected`；缺 `marker_time` 返回 `undecided`。
- `candles.annotations`：已接**真实已存信号**（无信号 → null + 说明），不再恒为 None。
- `signals` 列表/详情对齐 `src/api/types.ts`：列表为 `Signal`，详情含 `signal/frozen_range/conditions[{name,satisfied,detail}]/invalidation/risk/orders`；未计算字段一律 null。
- 新增**旧单数** `/api/v1/strategy`（前端 `client.ts` 仍调用），顶层为策略 A 的 `Strategy`，并带 `registry` 两套注册表。

**5. 运行器（`strategy_runner.py`）**
- 按 `--start/--end` **分页读取全部**已收盘 bar；超 `--max-bars` **拒绝并报错**，不静默截断。
- `data_version` 由**实际消费 bars 的来源集合**计算（`dv:<sha256[:16]>`），不再取“最新来源”；事件证据带 `data_sources`。
- `--write` 要求配置已冻结；未冻结须显式 `--research-trial`（只读）。

**附带**：迁移 v2 扩展 `signals` 唯一键（纳入 `strategy_version` + `data_version`），不同版本事件不再被静默吞掉；`--write` 重复以 `skipped` 显式报告。修复 `with sqlite3.connect()` 不关闭连接导致的 `ResourceWarning`（改 `contextlib.closing`）。

---

## 二、CLI（**纠正 README 旧写法**）

旧的 `--dry-run` **从未定义**。实际用法：

```sh
PY=/Users/boos/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3
$PY strategy_runner.py --strategy A                          # 默认只读干跑
$PY strategy_runner.py --strategy B --research-trial         # 未冻结配置：显式研究试运行
$PY strategy_runner.py --strategy A --start 2024-01-01T00:00:00Z --end 2024-12-31T23:59:59Z
$PY strategy_runner.py --strategy A --write                  # 仅配置已冻结时可用
```

---

## 三、测试与验收（按实测限定结论）

- **61 项离线测试全部通过**（`-W error::ResourceWarning` 下亦通过，无 ResourceWarning）。
  覆盖：迁移幂等/非破坏、唯一键含版本、配置非法/待配置、**逐前缀一致（A/B）**、常量与缺口与缺失字段、规则快照精确匹配与不退回、非空信号结构契约、annotations 接线、runner CLI（未冻结拒绝、拒绝截断、数据版本绑定、重复 skipped）。
- 共享库迁移 **31→31，`K线未丢失=true`**；`signals` 唯一键含 `strategy_version`+`data_version`；`integrity_check=ok`；`signals` 0 行。
- 旧 API `/api/health`、`/api/data/status`、`/api/candles` 正常；`/api/v1/strategy` 与 `/strategies` 均可用。
- 无 `__pycache__`/`.pyc`（工作区与只读包）；外部请求 0。

**限定结论（不夸大）**：
- 因果性只在**测试覆盖的场景**内验证（分位基线仅用先前 bars、确认时间取证据 bar、逐前缀一致）；**未**对所有可能参数组合做形式化证明，因此**不声称“无未来函数”**。
- 已接：只读行情契约、市场/覆盖/导入计划、策略注册表与状态、信号日志与条件快照、annotations、离线运行器、迁移。
- **未接**：指标(metrics)、账户、回测、复盘、OI/资金费**实采**、持续采集、认证、Telegram、下单。**不得称“全套已接通”。**

---

## 四、剩余问题

1. 前端 `client.ts` 仍调 `/api/v1/strategy`（已兼容），`SettingsPage` 两候选仍为静态名称——**需 Luna 做最小适配**改读 `registry`。
2. `signals.strength/expires_at`、`risk`、`invalidation` 未计算 → 恒 null（未接通，非缺失）。
3. 策略 A/B 参数**仍未冻结**：运行器对共享库一律返回“待配置”，**不会**用 31 根日线造 4h/15m 信号。
4. 下一项：模拟成本账本＋最小回测骨架（每策略周期 2000U、开仓占用平仓释放、手续费/滑点/资金费）；**杠杆、分配、2000U 口径待定**。

---

## 五、官方归档历史 K 线下载（本轮追加，见 `历史下载/`）

用户授权「K线直接下载」后执行。来源：币安官方 `data.binance.vision` 的
`futures/um/{monthly|daily}/klines`；逐文件比对官方 `.CHECKSUM`，流式下载、并发≤2、
断点续传、哈希去重，复用 `MarketStore.import_binance_zip` 入唯一主项目库。

- 已入库 `binance/um/1000PEPEUSDT` **294376 根 / 370 个官方归档**，9 个周期**全部 0 缺口、0 未取得文件**。
- 顺序与范围：1d→12h→8h→4h→1h→30m→15m→5m（30m 及以上取该合约全部可得原生历史；
  15m 近 2 年；5m 近 1 年），1m 最后单独补近 10 个已收盘 UTC 日。
- 边界按本轮北京时间（2026-10-05）推算，未硬编码；末根为最后已收盘 UTC 日 `2026-10-03`。
- `available_at` 全部 NULL（历史可得时间未知，未编造）；taker 字段完整（0 条为空）。
- 无未归档缺段，故**未调用** PublicGateway REST，`api_calls` 外部请求记录数 0。
- 明细见 `历史下载/下载清单.md`、`历史下载/manifest.json`、`历史下载/logs/verify.json`。

## 五之二、全池批量下载 + 真实标的池（本轮追加，见 `历史下载/`）

用户确认「那就去做」后执行：把已验收的单币下载器扩成**可恢复全池批量任务**并实际启动。

- **真实标的池**：经统一 `PublicGateway` 取 `binance.instruments`（`/fapi/v1/exchangeInfo`）
  与现货目录（`binance.spot_instruments`，**在本后端模块登记**、复用网关限频/缓存），
  原始响应 + sha256 + 请求编号 + 获取时刻存 `历史下载/标的池/`。
  当前交易中 USDT 线性永续 **528** 个；归档目录中存在但非当前的 **529** 个另列。
- **口径**：当前币池 ≠ 历史币池（标注快照时刻）；中小市值门槛未定 → 不淘汰任何标的；
  现货挂牌只作优先级（无源 = 未知，**不等于未上现货**）；**数据总池 ≠ 交易资格**
  （`/api/v1/pool` 的 `eligibility` 一律 null 并给出原因）。
- **顺序**：全池按周期外循环（所有币 1d → 12h → 8h → 4h → 1h → 30m → 15m → 5m），
  1m 近 10 天为独立最低优先队列。
- **纪律**：官方 `.CHECKSUM` 逐文件校验、失配即丢弃；429/403/418 立即停止不绕过；
  下载并发硬上限 2、入库串行低内存（`bulk_import.py` 流式两遍，与主项目导入器逐行等价）；
  空间低于 30GB 自动暂停并可恢复；`available_at` 未知保持 NULL。
- **估算**（`标的池/估算.json`）：196,825 个归档文件、约 8.0GB 原始包、
  约 1.27 亿行、库增量约 35.6GB；实际剩余磁盘 147.6GB → 估算后约 104GB。
- **性能**：改用按线程长连接后 830ms → **366ms/请求（2.3×）**，实测约 2.7 文件/秒。
- **数据库增量迁移 v3/v4**（非破坏、可重复，先临时库验证再共享库）：
  `download_runs` / `download_tasks` / `symbol_pool` / `coverage_cache`；
  既有 294,376 根 K 线未丢。
- **库存读取**：适配层优先读 `coverage_cache`（批量器增量维护），为空才回退全表统计；
  `v1_markets` 改为单次遍历（避免全池下 O(n²) 查询）。新增 `/api/v1/pool`、`/api/v1/pool/status`。
- **首批证据（1d，9 个标的）**：计划 858 文件 / 已入库 759 / 缺失 99 / **失败 0** /
  22,118 根 / 缺口 20。缺失=99 已逐一定位（`2026-10-04` 日归档官方未发布 + 归档起点前的日探测）；
  缺口 20 根为**官方归档自身空洞**（XRP/LTC/TRX/XLM 的 2022-02-26~28 与 2022-04-01~02 完全一致）。
- 明细见 `历史下载/批量下载清单.md`、`历史下载/README.md`、`历史下载/batch_state/`。

## 六、边界与合规

只读：`data_hub/`、前端 `src/api/{client,types}.ts`、5 份 `文档/`。唯一写入目录：本目录。行情库只读写 `data/market.sqlite3`，未复制生产库；`PYTHONDONTWRITEBYTECODE=1`。未扫描私库/家目录/memory/凭据；未绕过限频。

## 七、服务可用性修复（2026-10-05，主助手核查「Connection refused」后）

**现象**：主助手用本机直连 `127.0.0.1:8787` 的 health / pool/status / data/status 均 Connection refused。

**实测根因（两条，均已修）**：

1. **监听只绑 IPv4**。原 `main()` 为 `ThreadingHTTPServer(("127.0.0.1", PORT), Handler)`，只监听 `127.0.0.1`。macOS 上 `localhost` 先解析到 `::1`，按 `localhost` / `[::1]` 访问的客户端会直接 `ECONNREFUSED`。
   - 修法：`_loopback_server()` 按地址族分别构造监听器，**同时**绑 `127.0.0.1`（AF_INET）与 `::1`（AF_INET6），任一绑定失败只跳过不致命。两个地址都只在回环，**不暴露局域网**。
2. **进程被会话回收**。`start.sh` 用 `nohup ... &` 分离子进程，在本机工具环境下该子进程会在启动它的那次命令结束后被回收，服务随之消失（实测：启动后 1–2 分钟内端口即无人监听）。
   - 修法：本环境下**必须用受管后台任务启动** `adapter.py`（长驻进程），并在 `start.sh` 中导出 `NO_PROXY/no_proxy=127.0.0.1,localhost,::1`，避免 `http_proxy` 把回环请求转给代理导致连接被拒。

**实测验证（全部 `--noproxy '*'`，均 HTTP 200）**：

| URL | 结果 |
|---|---|
| `http://127.0.0.1:8787/api/health` | 200 |
| `http://127.0.0.1:8787/api/v1/health` | 200 |
| `http://127.0.0.1:8787/api/v1/pool/status` | 200 |
| `http://127.0.0.1:8787/api/v1/data/status` | 200 |
| `http://localhost:8787/api/v1/data/status` | 200 |
| `http://[::1]:8787/api/v1/data/status` | 200 |

`lsof -nP -iTCP:8787 -sTCP:LISTEN` 可见同一 PID 同时持有 IPv4 与 IPv6 两个监听套接字。

**回退方式**：`sh stop.sh`（按端口监听者 + 工作目录 + 命令行三重校验，只停自有服务），再以受管后台任务重启 `adapter.py`。`sh start.sh` 现在把「端口已监听」视为已运行并直接返回 0（不再误报占用错误）。

**注意**：`/health` 与 `/pool/status`、`/data/status` 的正确路径是 **`/api/health`**、**`/api/v1/pool/status`**、**`/api/v1/data/status`**（裸 `/health` 为 404，非连接问题）。

## 八、只读契约接线修复（2026-10-05，UI 几乎全空）

**现象**：5173 同源 `/api/v1/markets` 能返回标的，但页面「最后收盘（本地）」「涨跌幅」「数据时点 / 缺口」全空、现货状态恒显示「未知」。**根因**：`adapter.market_item` 把 `summary` 硬编码为 `None`，`data_time` / `data_missing_reason` 未返回，`spot_status` 直传交易所原始状态串（`TRADING`）而前端契约要 `listed`。

**只改后端，未改任何前端文件。** 改动全部在 `adapter.py`（+ 测试）：

| 字段 | 修复前 | 修复后 | 数据来源 |
|---|---|---|---|
| `summary` | `null` | `{timeframe, latest_close, change_ratio, quote_volume, event_time}` | `candles` 视图按主键索引取最近 2 根（**0.01 ms/标的**） |
| `data_time` | 未返回 | = `summary.event_time` | 同上 |
| `data_missing_reason` | 未返回 | 计数已知：`"1d 该已存范围缺 5 根（仅计数，未逐段定位区间）"`；缺 0 根：`"1d 该已存范围缺 0 根"`；**计数未知 → 留空** | `coverage_cache.missing`（下载器维护的**精确计数**） |
| `spot_status` | `"TRADING"`（前端认不出→恒「未知」） | `listed` / `not_listed` / `unknown` | `symbol_pool.spot_status` + `spot_match_method` |
| `coverage[].expected` / `missing_within_observed_range` | 未返回 | 真实数字 | `coverage_cache` |
| `v1_candles.source_url` | `null` | 官方归档 URL | `sources.source_url` |
| `v1_candles.time` | 全 `null` | `event_time`=末根收盘时间；`fetched_at`=归档入库时点；`available_at`/`received_at` 恒 `null` | `candles` + `sources.imported_at` |
| `health` 网关项 | 一律 `not_connected` | **只按成功请求留证**：`公共 REST 网关`（成功 N 次 / 失败 M 次）与 `官方归档下载（独立下载器）` **分两条陈述** + 实时订阅单列 `not_connected` | `api_calls.status` 分类计数 |

### 8.1 复核修正（主助手代码核查后）

1. **`market_summary` 不再依赖偶然键**。原写法直接取 `cell["market"]`。现按顺序显式取值：① coverage cell 的 `market`（来自 `covered()` 原始记录）→ ② 调用方传入的明确映射（`symbol_pool.market`，binance=`um` / okx=`swap`）→ ③ 两者都取不到则**不猜，返回空摘要**。已用真实 `v1_markets()`（254 标的）与逐个 `v1_market()` 调用验证，并**额外验证 `coverage_cache` 为空时的回退路径**（`STORE.status()["已入库覆盖"]`），均无 `KeyError`。
2. **`health_items` 不再把失败请求当连通**。实测 `api_calls` 为 **4 条：3 成功 + 1 失败**（`okx.instruments` 失败）；原写法用「总数 > 0」判 `ok` 属不实。现按 `status='成功'` 单独计数，失败次数显式列出且**不计作连通**；只有失败请求时状态为 `missing` 而非 `ok`。同时把**官方归档下载器**（直连 `data.binance.vision`/S3，**不经 REST 网关、不计入 `api_calls`**）拆成独立一条，不再与 REST 目录混说。
3. **缺口文案不再断言成因**。原 `"非本地丢弃"` 是仅凭计数就下的结论，已删除。现口径：计数已知 → 写明根数（**含 0 根**，如 `"1d 该已存范围缺 0 根"`）；计数未知（`missing IS NULL`）→ **留空**，不猜。`cells_for` / `v1_coverage` 同样**保留 NULL 语义**，不再把「未知」当成「缺 0 根」。

**摘要选哪一档周期**：`SUMMARY_TF_PREF = [1d, 12h, 8h, 4h, 1h, 30m, 15m, 5m, 1m]` —— **由粗到细，优先日线**。
理由：`MarketRowSummary.change_ratio` 在界面上只显示为「涨跌幅」，日线口径最不易被误读成实时价或短线涨跌；且周期由 `summary.timeframe` 明示（前端展开行显示该 chip）。

**三条禁止（已用测试守住）**：
1. **历史 close 不冒充实时价** —— 字段名是「最后收盘（本地）」，并给出 `event_time` 与周期。
2. **该 bar 的 `quote_volume` 不冒充滚动 24h / 5m 成交量** —— `volume_24h_quote`、`volume_by_timeframe` 一律 `null`。
3. **无原生短周期 / 无真实来源的字段留空** —— `open_interest_total`、`funding_rate`、`signal_status` 等恒 `null`；`spot_status` 无来源时为显式 `unknown`，**不得判为未挂牌**。

**为什么 `gaps` 仍是 `[]`（不猜区间）**：`coverage_cache` 只落「缺根计数」，**没有逐段区间**。逐段定位要按标的扫描 `candles` 视图，实测 `candles` 视图按 `symbol` 过滤走不到主键前缀 → **0.19 s/标的 × 246 = 44 s**，每次请求不可承受。故按既有契约给**精确计数 + 明确 note**，不编造 `from/to`。

**实测**：`v1_markets()` 254 个标的全部有 `summary`，总耗时 **0.479 s**；`summary.timeframe=1d`、`event_time=2026-10-03T23:59:59Z`、`change_ratio` 与 `quote_volume` 均与库内一致。回归测试 **74 项全绿**（含 3 项针对上述复核的契约测试）。

**未接入范围（本轮不启动）**：实时价格 / OI / 资金费采集（`oi_history`、`funding_history`、`signals` 三表均为 0 行）、WebSocket 订阅、策略 / 回测 / 交易 / 账户认证 / Telegram。欧易 K 线仍未下载。
