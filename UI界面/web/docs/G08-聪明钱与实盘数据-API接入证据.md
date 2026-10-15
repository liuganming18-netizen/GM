# G08 · 聪明钱与实盘数据：来源核查 + 实际只读 API 取样证据

> **版本：v3（2026-10-06 22:3x 北京时间）** —— 用户纠正来源后，**撤回 Hyperliquid 候选**，改为**严格以币安公开 API 为主**；本轮交付**独立只读模块 + 原版字段接线**（不再只交取样报告）。见「§零·A 来源纠正」。
> **版本：v2** —— 新增「§零·B 范围核对」：用户确认**实盘数据是公开的**，取消个人账户方向。
> 依据：《工程施工表》G08（`/Users/boos/.codex/worktrees/7943/加密货币趋势判断系统/文档/工程施工表.md`）。
> 归属：G08 由**原前端任务（本任务）**执行；**不碰历史下载/补缺/采集服务**，不与历史侧同时改同一后端文件。
> 原始证据：`docs/验收证据/G08-范围核对取样_2026-10-06.json`（v2）、`G08-币安官方端点逐字段核对_2026-10-06.json`（v3）、`G08-独立模块运行证据_2026-10-06.json`（v3）。
> **未新建端口 / 未新建库 / 未建第二个采集器 / 未做策略执行·交易·转账·上传部署**；未输出任何密钥、未购买任何付费服务。

---

## 零·A 来源纠正：撤回 Hyperliquid，严格币安公开 API 为主（v3 本轮）

> 用户明确质疑：**"怎么又是hype了，Api不是调用币安？"** 并纠正：
> 来源**始终**以**币安公开 API 为主**，**只有币安没有该代币/合约时才补 OKX**；
> **不能因为币安缺聪明钱字段而改接 Hyperliquid 或 OKX**。

### 0A.1 撤回清单（立即生效）

| 项 | 处置 |
|---|---|
| Hyperliquid 作为**候选来源** | ❌ **撤回**（不再等待用户接受，不再讨论是否采用） |
| Hyperliquid 的**调用** | ❌ **停止**（不再取样、不再翻页、不再聚合 Top-N 队列） |
| Hyperliquid 的**接线** | ❌ **不做**（不得进入任何字段映射） |
| 原报告 §三 / §四 的 Hyperliquid 结论 | ⚠️ **已作废**（保留原文仅作留痕，不得作为依据引用） |
| 原取样文件 `G08-Hyperliquid*.json` | 📦 移入 `docs/验收证据/已撤回_非币安来源/`（附 README 说明，保留仅作历史留痕） |
| 原 §一 摘要中「免费可用的真实聪明钱来源 = Hyperliquid」 | ❌ **作废**（见下方修正后的结论） |

### 0A.2 修正后的结论（v3）

| 项 | 结论 |
|---|---|
| 聪明钱**金额 / 均价 / 身份** | ❌ **币安公开 API 没有**（无此字段）⇒ **如实列缺项**，**不承诺公开 API 能提供不存在的字段**，**不改接** Hyperliquid/OKX |
| 大户**持仓量**多空比 | ✅ 币安 `/futures/data/topLongShortPositionRatio`（参考产品同名指标「持仓量多空比」） |
| 大户**账户数**多空比 | ✅ 币安 `/futures/data/topLongShortAccountRatio`（参考产品同名指标「账户数多空比」） |
| 全局账户多空比 | ✅ 币安 `/futures/data/globalLongShortAccountRatio` |
| taker 主动买卖量比 | ✅ 币安 `/futures/data/takerlongshortRatio`（**不是**持仓、**不是**身份） |
| **总持仓名义额(USD)** | ✅ 币安 `/futures/data/openInterestHist.sumOpenInterestValue`（**唯一**公开来源，无多空拆分） |
| 公开行情 / 盘口 / 逐笔 | ✅ 币安全部可得（主源）；OKX 仅在币安无该合约时补 |
| 参考产品私有后端 | ❌ **HTTP 401**，且**队列/阈值定义未公开**，不可复刻 |

> **纪律（不变）**：`大户持仓量多空比 / 账户数多空比 / 全局账户多空比 / taker 买卖量比` 是**各自独立命名的原始指标**，**不得**改名成「聪明钱金额 / 均价 / 身份」。

### 0A.3 本轮实际交付（不再只交报告）

| # | 交付物 | 说明 |
|---|---|---|
| 1 | **`后端实现/public_market_read.py`**（新文件，独立） | 币安公开数据只读模块：复用统一网关（共享缓存·租约·预算·429 冷却·403/418 封禁）+ **严格标的身份** + **「币安没有才补 OKX」闸门**；不落库、不当作历史 |
| 2 | **前端原版字段接线**（`oi.longShortRatio`、`oi.totalUsd` 兜底） | 有界可见行补扫 + **路由未接线（404）自动熔断**；未取到一律保持原版 `—` |
| 3 | **最小路由接线需求**（见 §八·0A.3） | `adapter.py` 只需 **1 行分派**；**本任务不改** `adapter.py` / `realtime_collector.py` |
| 4 | 币安官方端点**逐字段**核对证据 | `docs/验收证据/G08-币安官方端点逐字段核对_2026-10-06.json` |
| 5 | 独立模块**运行**证据（真实响应 + 时点） | `docs/验收证据/G08-独立模块运行证据_2026-10-06.json` |

**验证边界**：只跑了 `npx tsc --noEmit`（通过）；**未重跑全套 build**；未起新端口/采集器。

---

## 零·B 范围核对（用户已确认口径 → 与已有 G08 结果对齐）

> ⚠️ **本节为 v2 时点快照**；其中**涉及 Hyperliquid 的行已由 §零·A 撤回**（不再作为来源）。

> 触发：用户已答复「**实盘数据是公开的**」，取消个人账户方向；**主接币安，币安没有的币才补 OKX**；聪明钱同样只核查**公开可取得**的真实来源，**不得**把人数比或全部成交量冒充聪明钱。
> 本轮动作：**只做范围核对 + 复核性实际取样**（1 次），**复用已有符合数据**，**不重跑无关测试、不另开端口/采集器、不动另一边日线全池历史任务**。
> 复核取样时间：`2026-10-06T13:31:39Z`（北京时间 21:31）。原始证据：`docs/验收证据/G08-范围核对取样_2026-10-06.json`。

### 0B.1 口径对照（用户口径 → 实测 → 结论）

| 用户已确认口径 | 本次实测（含时点） | 结论 |
|---|---|---|
| 实盘=**公开**市场数据；不读私人持仓/订单/成交、不索取交易账户密钥 | 后端 `/api/v1/health` → `账户/交易 not_connected`「未配置账户，不读取凭据」；本轮**未发起任何私人账户请求** | ✅ **口径一致**，个人账户方向已取消 |
| 主接**币安** | 币安公开 5 类端点全部 `http=200`（depth / aggTrades / ticker24h / premiumIndex / openInterest） | ✅ 币安为主源可行 |
| 币安没有的币才补 **OKX** | OKX `books` / `trades` 均 `http=200`、`code=0` | ✅ 补充源可行（**仅补位**，不因币安历史缺口切换） |
| 保持**市场/合约/时间/单位**真实可辨 | 币安 `symbol=BTCUSDT`（USD-M 永续）、OKX `instId=BTC-USDT-SWAP`；时间 `E/T`、`ts`；单位：depth 数量=币、`lastFundingRate` 小数、`nextFundingTime` 毫秒纪元 | ✅ 已逐字段记录（见 §七 与证据 JSON） |
| 聪明钱=**公开可取得**的真实来源；不得用人数比/全部成交量冒充 | 参考产品私有后端仍 `http=401`；**币安公开 API 无「聪明钱金额/均价/身份」字段**；大户/全局**账户数比**与 taker 量虽 `http=200`，但**只能按原名作原始指标**、**不得改名冒充** | ✅ 口径一致，见 §五 / §六 |

### 0B.2 复用清单（已有符合数据，**不重复施工**）

| 已有资产 | 内容 | 为何符合新口径 |
|---|---|---|
| 同源实时链路（后端 `realtime_quotes` + WS） | 公开**行情**：价格 / OI / 费率；实测 `realtime_status=ok`、`last_price=86116.5`、`funding_rate=5.56e-06`、`next_funding_time=2026-10-06T16:00:00Z`、`open_interest_total=94292.872` | 均为**交易所公开**市场数据，非私人账户 |
| 同源 K 线链路 | 各周期闭合 `bars[]` + 独立 `forming_bar`（`is_closed=false`）+ `realtime` 块（`source=binance.fstream/ws`） | 公开市场数据；G05 已接线 |
| 币安公开目录 / 网关 | `/api/v1/health` → `公共 REST 网关（目录/端点） ok`（成功 93,097 次） | 币安为主源的既有通道 |
| 币安公开端点（v3 已实现为独立模块） | `/futures/data/topLongShortPositionRatio` 等 4 个原始指标 + `openInterestHist`（总持仓名义额） | **公开**、无需密钥；主源口径 |

⇒ 上述**不需要重做**；本轮只做 1 次复核取样，不重建、不重跑。

### 0B.3 仅需补充的必要项（本轮识别，**均附实际响应**）

| # | 必要项 | 提供方 / 端点 | 本轮实测（时点） | 说明 |
|---|---|---|---|---|
| A1 | 公开**盘口**（币安主源） | `GET fapi.binance.com/fapi/v1/depth?symbol=BTCUSDT&limit=5` | `http=200`，`lastUpdateId=11748018797798`，买一 `86049.40/2.723`、卖一 `86049.50/8.856`（`13:31:39Z`） | 唯一有原版落点的公开新维度（见 0B.5） |
| A2 | 公开**逐笔成交**（币安主源） | `GET fapi.binance.com/fapi/v1/aggTrades?symbol=BTCUSDT&limit=3` | `http=200`，`a=3476570424 p=86062.20 q=0.063 T=1791293501813 m=false` | **原版无逐笔模块**（见 0B.4） |
| A3 | 公开**盘口/成交**（OKX 补充源） | `GET www.okx.com/api/v5/market/books?instId=BTC-USDT-SWAP&sz=5`、`…/market/trades?limit=3` | 均 `http=200`、`code=0`；`tradeId=2976486538 px=86110.8 side=buy ts=1791293506948` | 仅在币安无该合约时使用 |
| A4 | 大户**持仓量**多空比（币安官方） | `fapi.binance.com/futures/data/topLongShortPositionRatio?symbol=BTCUSDT&period=1h` | `http=200`，`longShortRatio=1.7012`、`longAccount=0.6298`、`shortAccount=0.3702`（`14:24Z`） | ✅ **可作明确原始指标**（不得改名成聪明钱） |
| A5 | 总持仓名义额（币安官方） | `fapi.binance.com/futures/data/openInterestHist?symbol=BTCUSDT&period=5m&limit=1` | `http=200`，`sumOpenInterest=94673.59`、`sumOpenInterestValue=8,168,077,235.68` | ✅ **唯一公开的 USD 名义额**（无多空拆分） |
| A6 | 聪明钱币种域 | `POST …/info {"type":"meta"}` | `http=200`，`universe=234` | 与币安/OKX 标的域**不对齐** |
| A7 | 同源只读端点（**v3 已实现模块，待 1 行分派**） | `/api/v1/markets/{ex}/{mid}/public-extras`（新）；`/api/v1/smart-money`、`…/depth`（v2 旧提议，**已收敛，不再采用**） | `public-extras` 需 `adapter.py` 加 1 行；旧提议的路径现均 `http=404` | 见 **§8.2** |

### 0B.4 无法接线项：原版**没有**盘口/逐笔模块（关键发现）

- 全量核对 `src/`：**不存在**订单簿 / 盘口 / 逐笔成交的组件或字段。检索命中仅 3 处，全部是**注释 + 一个既有占位标签**：
  - `InlineExpandedChart.tsx:174`（说明：后端无订单簿深度，不再声称「聚合深度」）
  - `InlineExpandedChart.tsx:686`（注释：四项指标后端无来源）
  - `InlineExpandedChart.tsx:696`（**原版**标签「订单簿流动性失衡」）
- 基线比对：`git show baseline-8edb395:src/components/InlineExpandedChart.tsx` 第 **530** 行**就是** `<span className="text-neutral-400">订单簿流动性失衡:</span>` ⇒ 该标签是**原版字段**，不是新增。
- ⇒ 「公开盘口 / 逐笔成交」**没有可接的原版模块**。要显示必须先**新增模块** —— 而用户明确「不新增删除模块」，故**本轮不接线**，如实记录，不用空态当已接通。

### 0B.5 唯一可接的原版字段：「订单簿流动性失衡」（含真实算例）

| 项 | 内容 |
|---|---|
| 原版位置 | `InlineExpandedChart.tsx` → 折叠详情「相关指标 (随当前周期展示)」→ `订单簿流动性失衡:`（原版恒 `—`） |
| 数据需求 | 公开盘口前 N 档买/卖量（币安 `depth` / OKX `books`） |
| 候选口径（**需业务确认**） | `(Σ买量 − Σ卖量) / (Σ买量 + Σ卖量)`，取前 5 档 |
| 本轮真实算例 | 币安 BTCUSDT 前 5 档：买量合计 `2.756`、卖量合计 `9.292` ⇒ 失衡 **`−0.5425`**（`13:31:39Z`） |
| 状态 | ⚠️ **口径未定**（档位数 / 是否按距离加权 / 是否用合约张数）⇒ 在业务确认前**保持原版 `—`**，不自行拍板 |
| 依赖 | 需后端**同源**代理（项目规约：浏览器不直连交易所）⇒ 见 0B.7 |

### 0B.6 本轮复核取样时点表

| 端点 | http | 实测时点 |
|---|---|---|
| 币安 depth / aggTrades / ticker24h / premiumIndex / openInterest | 200 ×5 | `13:31:39Z` |
| OKX books / trades | 200 ×2 | `13:31:46Z` |
| 币安官方 4 个原始指标 + `openInterestHist`（v3 实测） | 200 ×5 | `14:24:08Z` |
| 禁用源（币安大户比 / taker 比、OKX 账户比） | 200 ×3（**仅记录，不接入**） | `13:32:0xZ` |
| 参考产品 `smart-money-signal/symbols` | **401** | `13:32:07Z` |
| 同源 `markets/binance/BTCUSDT/candles` / `…/BTCUSDT` | 200 ×2 | `13:32:46Z` |
| 后端待建路由 `smart-money`、`…/depth`、`…/orderbook`、`…/trades` | **404 ×4** | `13:32:07Z` |

### 0B.7 后端必要变更（**仍交主助手协调归属，本任务不直接改**）

> ⚠️ **v3 已收敛本节**：不再走 `v1_smart_money`（币安无该字段，Hyperliquid 已撤回）。
> 现行方案是**一个**独立模块 + **一行分派**的 `public-extras` 路由，见 **§8.2**。本节保留为 v2 过程留痕。

> 原有 §八 第 1 项（`v1_smart_money`）不变；本轮**新增第 2 项**（公开盘口代理），仅用于填 0B.5 的既有字段。

**2. 新增只读函数（建议名）`v1_depth(exchange, market_id, params)`：**
   - 主源：币安 `fapi/v1/depth`；**仅当**该标的币安无合约时才走 OKX `/api/v5/market/books`
   - 输出（建议契约）：`{available, exchange, market_id, as_of, bids:[[px,sz],…], asks:[[px,sz],…], unit, source_url, missing_reason}`
   - 分派：`route_v1()` 中 `if len(seg) == 4 and seg[0] == "markets" and seg[3] == "depth": return v1_depth(seg[1], seg[2], params), 200`
   - 纪律：**不落库、不当作历史**（仅实时透传 + 短缓存），**不影响历史侧采集**

**3. 前端侧（待后端就绪后）**：`InlineExpandedChart.tsx` 仅把该行 `—` 换成由新字段派生的数值；**UI 不增删**；口径未定前继续 `—`。

### 0B.8 缺口与边界（本轮）

| # | 项 | 状态 |
|---|---|---|
| 1 | 公开盘口/逐笔 → 原版**无模块**可接 | ❌ 不接线（不新增模块） |
| 2 | 「订单簿流动性失衡」口径（档位/加权/单位） | ⚠️ 待业务确认 |
| 3 | 后端同源 `depth` / `smart-money` 端点 | ❌ 待主助手协调归属 |
| 4 | 聪明钱 `占比/多空比/24h变化` | ⚠️ 需业务定义队列（§六） |
| 5 | 后端 `public-extras` 路由未接线 / 与「前端读取不触发外部采集」纪律冲突 | 需 1 行分派 + 裁定 | **主助手协调**（v3 已给最小需求，见 §8.2） |
| 6 | 同源实时链路新鲜度（观测，**非本任务**） | ⚠️ `/api/v1/health` 报 `实时行情采集 stale`（age 219s，心跳 `pid=29827` 有效=False）；同源 `realtime_status=ok` 但 `age_seconds≈42s`；BTC 1m 闭合序列实测存在**中段缺口**（`13:20` → `13:31`）。属采集/补缺侧，本任务不动 |

边界：✅ 只读、✅ 未新端口/库/采集器、✅ 未改后端文件、✅ 未输出密钥、✅ 未购买、✅ 未用人数比/taker 量冒充、✅ 未重跑无关构建/全套测试。

---

## 一、结论摘要（先给结论）

| 项 | 结论 |
|---|---|
| 原项目「聪明钱」的来源 | **参考产品（tradeflow.club）自己的私有后端** `/api/analytics/smart-money-signal/*`；本次实测 **HTTP 401 Unauthorized** |
| 该私有后端是否可用 | ❌ **不可用**（无凭据）。且其**样本/队列/阈值定义未公开**，无法复刻 |
| 免费可用的**真实**聪明钱来源 | ❌ **币安公开 API 没有**「聪明钱金额/均价/身份」字段 ⇒ **如实列缺项**（v3 修正：**已撤回** Hyperliquid 候选，见 §零·A） |
| 币安公开 API **能**提供的原始指标 | ✅ 大户**持仓量**多空比 / 大户**账户数**多空比 / 全局账户多空比 / taker 买卖量比 / **总持仓名义额(USD)**（见 §零·A.2） |
| 交易所「人数多空比 / taker 量」 | ⚠️ **可作为独立命名的原始指标**；但**禁止**改名成聪明钱身份/金额/均价（见 §五） |
| 第三方聪明钱平台 | ❌ 均需**付费密钥**或被拦（Nansen 401 / Arkham 需密钥 / GMGN 403 / CoinGlass 不可达） |
| 实盘数据（公开行情/成交/盘口） | ✅ **已实际取样 + 已实现同源读取模块**（币安主源；OKX 仅补位） |
| 实盘数据（个人账户持仓/订单/成交） | ✅ **已取消该方向**（用户确认实盘=公开）；后端 `/api/v1/health` 亦为 `账户/交易 not_connected`，本轮未发起任何私人账户请求 |
| 可落地的接入 | **原版既有字段** `oi.longShortRatio`（持仓量多空比）、`oi.totalUsd`（总持仓名义额兜底）——已接线（§零·A.3 / §八） |
| 必要后端变更 | 仅需 `adapter.py` **1 行分派**到独立模块；**交主助手协调归属后实施**（§八） |

---

## 二、原项目「聪明钱」定义与原版字段

### 2.1 参考产品的真实端点（本次实测）

| 端点 | 方法 | 本次实测 |
|---|---|---|
| `https://www.tradeflow.club/api/analytics/smart-money-signal/symbols` | GET | **HTTP 401** `{"statusCode":401,...,"message":"Unauthorized"}` |
| `https://www.tradeflow.club/api/analytics/smart-money-signal/rankings` | GET | **HTTP 401** 同上 |
| `https://www.tradeflow.club/api/analytics/coin-list` | GET | **HTTP 401** 同上 |

⇒ **参考产品的聪明钱数据源是其自有鉴权后端**，不是公开第三方 API。我们**没有该凭据**，故**不能**以「复刻参考产品」为路径。

### 2.2 参考产品的字段名（来自公开 JS 逆向文档，用于对齐「原版字段」）

- 摘要字段：`lastPrice, priceChangePercent, fundingRate, openInterestValue, smartMoneyLongShortRatio, smartMoneyLongOiRatio, smartMoneyShortOiRatio, topPositionRatio, smartMoneyLongTraders, smartMoneyShortTraders`
- 时序字段：`longShortRatioArr, topPositionLongShortRatioArr, topAccountLongShortRatioArr, topAveragePositionConcentrationArr, longPosArr, shortPosArr, longWhalePosArr, shortWhalePosArr, longAvgPriceArr, longWhalesAvgPriceArr, shortAvgPriceArr, shortWhalesAvgPriceArr, longTradersArr, shortTradersArr, longWhalesArr, shortWhalesArr`
- 展示派生：`longWhalePos/longPos*100`、`shortWhalePos/shortPos*100`

> 关键：文档已确认**参考产品自身的「聪明钱/鲸鱼」样本、阈值、账户范围、OI 分母均未公开**。即使用户给到该产品账号，**其定义仍不可复刻**。

### 2.3 本项目原版 UI 字段（需要被填的位）

| 位置 | 字段 | 含义 |
|---|---|---|
| `TokenTable` 第 4/5 列「聪明钱多头 / 空头」 | `smartMoneyLong/Short.{amountUsd, ratioPercent, avgPrice}` | 持仓额 / 占比% / 均价 |
| `TopStatusBar`「聪明钱多空比」 | `smartMoneyRatio` | 多空比 |
| `InlineExpandedChart` 折叠详情 | `smartMoneyLong/Short.ratioPercent`、`avgPrice` | 多空持仓占比 / 多空均价 |
| `DataCenterView` 子页签 | 「聪明钱与市值」 | 占位页签 |
| 后端契约 | `smart_money: {available, reason}` | 目前恒 `available:false` |

**后端现状**（只读核对）：`后端实现/adapter.py:752`
`"smart_money": {"available": False, "reason": SMART_MONEY_REASON}`
其中 `SMART_MONEY_REASON`（`adapter.py:67`）=「无真实来源：不使用交易所人数多空比或 taker 买量冒充『聪明钱多空金额/内部占比/均价』」。

---

## 三、~~聪明钱：可用的真实来源（Hyperliquid 公开 API）~~ —— ❌ **本节已撤回（v3）**

> ⚠️ **本节结论作废**。用户明确纠正来源：**以币安公开 API 为主**，Hyperliquid **不是**已批准的替代来源。
> 本节保留**仅作历史留痕**：**不得**作为接入依据、**不得**出现在任何字段映射里。
> 取样文件已移入 `docs/验收证据/已撤回_非币安来源/`。
> 现行结论见 **§零·A.2**（币安官方端点逐字段核对）。

<details>
<summary>（历史留痕）v1/v2 对 Hyperliquid 的核查内容，已作废</summary>



### 3.1 提供方与端点

- **提供方**：Hyperliquid（去中心化永续交易所，官方公开 API）
- **端点 1（榜单）**：`GET https://stats-data.hyperliquid.xyz/Mainnet/leaderboard` —— **无需密钥**
- **端点 2（账户状态）**：`POST https://api.hyperliquid.xyz/info`，body `{"type":"clearinghouseState","user":"0x…"}` —— **无需密钥**
- **端点 3（成交历史）**：`POST https://api.hyperliquid.xyz/info`，body `{"type":"userFills","user":"0x…"}`
- **端点 4（合约域）**：`POST https://api.hyperliquid.xyz/info`，body `{"type":"meta"}`

### 3.2 真实字段（实测返回）

| 端点 | 顶层字段 | 关键字段 |
|---|---|---|
| leaderboard | `leaderboardRows[]` | `ethAddress`、`accountValue`、`windowPerformances[][day/week/month]{pnl, roi, vlm}` |
| clearinghouseState | `marginSummary, crossMarginSummary, crossMaintenanceMarginUsed, withdrawable, assetPositions[], time` | `assetPositions[].position`：**`coin`、`szi`(数量，正多负空)、`entryPx`(均价)、`positionValue`(名义额)、`unrealizedPnl`、`leverage`、`liquidationPx`、`marginUsed`** |
| userFills | 数组 | `coin, px, sz, side, time, startPosition, dir, closedPnl, hash, oid, crossed, fee, tid, cloid` |
| meta | `universe[]` | `name, szDecimals, maxLeverage` |

### 3.3 覆盖范围

- **榜单账户数**：**47,446** 条
- **可查询永续币种**：**234** 个（`meta.universe`）
- **实测队列**：账户价值 Top-30 中 **30/30 成功取到持仓**，共覆盖 **89 个币种**（耗时 34 s）
- 币种口径：Hyperliquid 自有永续（**≠ 币安/OKX 标的域**）

### 3.4 数据时点

- `clearinghouseState.time` = `2026-10-06T13:25:47Z`（快照时间，实时）
- `userFills[].time` 为毫秒事件时间（最近成交）

### 3.5 是否有历史

- ✅ **有**：`userFills` 单次最多返回 **2000** 条近期成交（实测返回 2000 条），可按 `user` 持续翻页
- ⚠️ 榜单 `leaderboardRows` 只给 `day/week/month` 三个窗口的聚合 PnL/ROI/量，**不是逐日序列**
- ❌ **没有**「聪明钱多空比」的历史序列端点（参考产品的 `*Arr` 时序字段在 Hyperliquid 无对应）

### 3.6 实测样例（真实数据，可复核）

账户 `0x5b5d51203a0f9079f8aeb098a6523a13f298c060`，账户价值 ≈ **1.55 亿 USD**：

| 币 | 数量 szi | **均价 entryPx** | **名义额 positionValue** | 未实现盈亏 |
|---|---|---|---|---|
| BTC | -2788.13357 | 75,949.4 | 239,971,868 | -28,214,796 |
| ETH | -104550.8054 | 2,304.1 | 283,573,149 | -42,677,065 |
| SOL | -742153.46 | 94.3122 | 89,307,037 | -19,312,895 |

Top-30 队列聚合（示例口径，**非业务定义**）：ETH 空头名义 5.52 亿 / 5 个账户；BTC 空头 4.64 亿 / 5 个账户；共 89 币。

⇒ **身份（地址）+ 金额（positionValue）+ 均价（entryPx）三者齐备**，满足「聪明钱」的硬要求。

---

</details>

---

## 四、聪明钱：第三方平台核查（均不可直接使用）

| 提供方 | 端点 | 实测 | 缺口 |
|---|---|---|---|
| Nansen | `https://api.nansen.ai/api/v1/smart-money` | **HTTP 401** `"API key required"` | **需付费 API 密钥** |
| Arkham | `https://api.arkhamintelligence.com/transfers` | **HTTP 307** → Cloudflare | 需密钥 / 需授权 |
| GMGN | `https://gmgn.ai/api/v1/smartmoney/sol/walletNew` | **HTTP 403**（Cloudflare 拦截） | 被拦；未获授权 |
| CoinGlass | `https://api.coinglass.com/...` | **HTTP 000**（不可达/被拦） | 网络不可达；付费 |
| 参考产品 | `www.tradeflow.club/api/analytics/...` | **HTTP 401** | 无凭据；定义未公开 |

---

## 五、币安官方「原始指标」端点（**可用**，但禁止改名冒充聪明钱）

> v3 修正：用户明确「**大户持仓多空比和账户多空比可作为明确原始指标核查**」，
> 但「**不得拿人数比/taker量假造聪明钱金额/均价/身份**」。
> 因此下表的正确用法是：**以各自的名字**（持仓量多空比 / 账户数多空比 / …）呈现，
> **不得**改名成「聪明钱」。

| 端点 | 实测返回字段 | 正确用途 | 禁止用途 |
|---|---|---|---|
| `fapi.binance.com/futures/data/topLongShortPositionRatio` | `longAccount, shortAccount, longShortRatio, timestamp` | ✅ **大户持仓量多空比**（参考产品同名指标） | ❌ 不得当作聪明钱金额/均价/身份 |
| `fapi.binance.com/futures/data/topLongShortAccountRatio` | 同上 | ✅ **大户账户数多空比**（参考产品同名指标） | ❌ 同上 |
| `fapi.binance.com/futures/data/globalLongShortAccountRatio` | 同上 | ✅ **全局账户数多空比** | ❌ 同上 |
| `fapi.binance.com/futures/data/takerlongshortRatio` | `buySellRatio, buyVol, sellVol, timestamp` | ✅ **taker 主动买卖量比** | ❌ 非持仓、非身份 |
| `fapi.binance.com/futures/data/openInterestHist` | `sumOpenInterest, sumOpenInterestValue, CMCCirculatingSupply, timestamp` | ✅ **总持仓量 + 总持仓名义额(USD)**（无多空拆分） | ❌ 不得当作「聪明钱持仓额」 |

**本轮实测（2026-10-06T14:24Z，`period=1h`）**：

| 指标 | BTCUSDT 实测值 | 事件时点 |
|---|---|---|
| 大户持仓量多空比 | `1.6974` → **`1.7012`** | `14:00:00Z` |
| 大户账户数多空比 | `1.0606` → **`1.0333`** | `14:00:00Z` |
| 全局账户数多空比 | `0.9956` → **`0.9654`** | `14:00:00Z` |
| taker 买卖量比 | `0.9187` → **`1.1400`** | `13:00:00Z` |
| 总持仓量 | **`94598.720`**（标的币） | `14:24:12Z` |
| 总持仓名义额 | **`8,145,237,433.35` USD** | `14:20:00Z` |
| `/fapi/v1/positionRisk`（账户身份） | **HTTP 404**（需签名） | — |

> **这些数字与「聪明钱多头持仓额 / 均价 / 身份」不是一回事**：币安公开 API **没有**后者。
> 本模块把它们**按原名**暴露（`top_position_long_short_ratio` 等），**不接入**任何「聪明钱」字段。

---

## 六、原版字段 → 可得来源 映射与缺口（v3 修正）

### 6.1 原版**聪明钱**字段（`SmartMoneySide`）

| 原版 UI 字段 | 参考产品来源 | 币安公开 API | 状态 |
|---|---|---|---|
| 持仓额 `amountUsd` | 站内 API（401） | **无此字段** | ❌ **缺**（币安公开 API 不提供） |
| 均价 `avgPrice` | 站内 API（401） | **无此字段** | ❌ **缺** |
| 身份/账户 | 站内 API（401） | **无此字段**（`positionRisk` 需签名） | ❌ **缺** |
| 占比 `ratioPercent` | 站内 API（401） | 需「多空拆分持仓」——**无** | ❌ **缺** |
| 多空比 `smartMoneyRatio` | 站内 API（401） | 只有大户/全局**账户数比**，**不得改名** | ❌ **缺**（不冒充） |
| 24h 变化 `change24h` | 站内 API（401） | **无此字段** | ❌ **缺** |

⇒ **结论**：币安公开 API **不能**提供原版「聪明钱金额/均价/身份」。**如实列缺项**，
**不承诺**公开 API 能提供不存在的字段，**不因缺字段改接** Hyperliquid / OKX。

### 6.2 原版**其他**字段（本轮已接 / 可接）

| 原版 UI 字段 | 原版位置 | 币安公开来源 | 状态 |
|---|---|---|---|
| `oi.longShortRatio`（**多空比**） | `TokenTable` OI 列 `(多空比 —)` | `/futures/data/topLongShortPositionRatio.longShortRatio`（持仓量多空比） | ✅ **已接线**（§零·A.3 / §八） |
| `oi.totalUsd`（**持仓名义额**） | `TokenTable` OI 列主数值 | `/futures/data/openInterestHist.sumOpenInterestValue` | ✅ **已接线**（仅原值为 null 时兜底） |
| `订单簿流动性失衡` | `InlineExpandedChart` 相关指标 | `/fapi/v1/depth`（原始档位） | ⚠️ 数据已备（模块返回原始 bids/asks）；**口径未定**（档位/加权/单位）⇒ 保持 `—` |
| 逐笔成交 / 盘口列表 | **原版无此模块** | `/fapi/v1/aggTrades`、`/fapi/v1/depth` | ❌ **无位可接**（不新增模块） |
| `smartMoneyLong/Short` | `TokenTable` 第 4/5 列 | **无** | ❌ 保持原版 `—` |

> **纪律**：`占比 / 多空比 / 24h 变化` 若要用作「聪明钱」口径，**必须有业务方给出的队列定义**；**在定义到位前保持原版空态 `—`**，不自行发明队列。

---

## 七、实盘数据（用户已确认：**公开**）

> 用户已答复「实盘数据是**公开的**」⇒ 本节按此口径收口：**只**读交易所公开实时行情 / 成交 / 盘口；**不**读个人账户持仓/订单/成交，**不**索取交易账户密钥。范围核对见 §零·B。

### 7.1 公开实时行情 / 成交 / 盘口 —— ✅ **已实际取样**

| 提供方 | 端点 | 实测返回字段 | 实测时点 |
|---|---|---|---|
| 币安 | `fapi.binance.com/fapi/v1/depth?symbol=BTCUSDT&limit=5` | `lastUpdateId, E, T, bids[][], asks[][]`（买一 `86,056.00/7.032`、卖一 `86,056.10/5.796`） | `13:25:49Z` |
| 币安 | `fapi.binance.com/fapi/v1/aggTrades?symbol=BTCUSDT&limit=2` | `a, p, q, nq, f, l, T, m` | 实时 |
| OKX | `www.okx.com/api/v5/market/books?instId=BTC-USDT-SWAP&sz=2` | `asks[][], bids[][], ts, seqId` | 实时 |
| OKX | `www.okx.com/api/v5/market/trades?instId=BTC-USDT-SWAP&limit=2` | `instId, tradeId, px, sz, side, ts, source` | `13:25:48Z` |

> 注意：本项目**已有一条同源实时链路**（后端 `realtime_quotes` + WS，覆盖价格/OI/费率）。**公开盘口/逐笔**按新口径**属于「实盘数据」范围**，已实测可得；但**原版没有盘口/逐笔模块**，唯一可落的是既有「订单簿流动性失衡」字段（见 §零·B 0B.4 / 0B.5）。
> v2 复核取样：币安 `depth` 买一 `86049.40/2.723`、卖一 `86049.50/8.856`；`aggTrades` 最新 `p=86062.20 q=0.063`；OKX `books` 买一 `86099.9`、卖一 `86100`（`13:31:39Z`–`13:31:46Z`）。

### 7.2 个人账户 持仓/订单/成交 —— ✅ **已取消（用户确认实盘=公开）**

- 用户明确：实盘数据**只**含公开市场数据 ⇒ **不读**私人持仓/订单/成交，**不索取**交易账户密钥
- 后端 `/api/v1/health`：**`账户/交易 not_connected`**「未配置账户，不读取凭据」——与本口径一致
- 本轮**未读取任何私人账户**、**未请求任何密钥**（符合指令）
- 如未来改口径，需先由用户明确交易所与只读凭据存放位置；按 §九 报告来源与权限，**不输出密钥、不购买**

---

## 八、后端接入：独立模块已实现 + **最小路由接线需求**（v3）

> 原因：前端只能读**同源** `/api/v1`（项目规约「浏览器不直连交易所」）；
> 而 `adapter.py` / `realtime_collector.py` **正被历史侧（日线全池补缺）使用**，**不得同时编辑**。
> 故按用户授权：**先在独立新模块实现公开数据获取/规范化**，`adapter.py` 只留**一行分派**给主助手。

### 8.1 已实现的独立模块（**本任务已交付，可直接运行**）

**文件**：`后端实现/public_market_read.py`（**新文件**，不碰 `adapter.py` / `realtime_collector.py`）

| 能力 | 实现 |
|---|---|
| 复用统一网关 | `from gateway_ext import gateway` → 共享缓存 · 并发租约 · 本地预算 · 429 冷却 · 403/418 封禁 · 不跟随重定向 · 不接收认证头 |
| 遵守网关节流 | 遇 `DeferredRequest` 按网关给出的**最早可重试时间**等待后重试（上限 4s × 4 次），**不绕过**任何限频 |
| 严格标的身份 | `resolve_identity()`：`(exchange, symbol)` 必须命中 `symbol_pool`；交易所白名单仅 `binance`/`okx`；非法代号直接拒绝 |
| **「币安没有才补 OKX」闸门** | 请求 `okx:*` 时先查币安是否有该 base 的 USDT 永续；**有则拒绝**（含"历史缺口不构成切换理由"文案） |
| 有界取数 | `fields=ratio,oi`（3 次上游，列表页用）/ `orderbook`（1 次）/ 缺省 `all`（7 次） |
| 不落库 | 只走网关缓存，**不写行情库**、不当作历史 |

**实测（2026-10-06T14:25–14:26Z）**：

| 用例 | 结果 |
|---|---|
| `binance:BTCUSDT` | `available=true`；持仓量多空比 `1.7012`、账户数多空比 `1.0333`、全局 `0.9654`、taker `1.1400`；OI 名义额 `8,145,237,433.35` |
| `binance:ETHUSDT` | `available=true`；持仓量多空比 `1.5708`；OI 名义额 `6,239,585,412.61` |
| `okx:BTC-USDT-SWAP` | **拒绝**：「币安已有 BTC 的 USDT 永续 ⇒ 按口径只用币安，不切 OKX（历史缺口不构成切换理由）」 |
| `okx:AEON-USDT-SWAP` | 身份通过（币安无 AEON）但 **`available=false`**：OKX 公开扩展字段未实现，保持缺失（不跨所拼接） |
| `binance:NOTACOINZZZ` | **拒绝**：「symbol_pool 中无 … 标的身份未确认，不对外取数」 |
| `kraken:BTCUSDT` | **拒绝**：「不支持的交易所」 |

### 8.2 给主助手的**最小**接线需求（1 行分派）

**目标文件**：`后端实现/adapter.py`（**只加 2 处**，不改任何既有路由行为）

```python
# 1) 文件顶部（与既有 import 同处，约第 55 行）：
import public_market_read

# 2) route_v1() 中，既有 `if len(seg) == 4 and seg[0] == "markets":` 块内，
#    与 `if tail == "candles":` 并列（约第 1878 行）：
        if tail == "public-extras":
            return public_market_read.handle_market_tail(seg[1], seg[2], params), 200
```

**路由契约**：`GET /api/v1/markets/{exchange}/{market_id}/public-extras?period=1h&fields=ratio,oi`

**响应契约**（节选，全部可为 `null`）：
```json
{
  "available": true, "exchange": "binance", "market_id": "BTCUSDT",
  "as_of": "2026-10-06T14:26:00Z", "fields": ["oi","ratio"],
  "open_interest": { "available": true, "amount": 94601.268,
                     "usd": 8134822919.715, "usd_event_time": "2026-10-06T14:20:00Z",
                     "source": "币安 fapi/v1/openInterest + /futures/data/openInterestHist",
                     "note": "**全市场总量**，无多空拆分、无身份；不是「聪明钱持仓额」" },
  "long_short": {
    "period": "1h",
    "top_position_long_short_ratio": { "available": true, "value": "1.7012",
        "long_account": "0.6298", "short_account": "0.3702",
        "event_time": "2026-10-06T14:00:00Z" },
    "top_account_long_short_ratio": { "…": "…" },
    "global_account_long_short_ratio": { "…": "…" },
    "taker_buy_sell_ratio": { "…": "…" },
    "note": "原始指标；**不得**改名成「聪明钱金额/均价/身份」" },
  "orderbook": { "available": true, "unit": "标的币", "bids": [["85986.70","6.219"]],
                 "asks": [["85986.80","3.153"]], "note": "只给原始档位；失衡口径未定" },
  "smart_money": { "available": false,
                   "reason": "币安公开 API 无「聪明钱金额/均价/身份」字段：…" },
  "missing_reason": null
}
```

**注意（需主助手裁定）**：该路由会触发**对外取数**，与 `adapter.py` 自述的
「**不发起任何交易所请求**（前端读取不触发外部采集）」纪律**相冲突**。
本模块已用**统一网关 + 共享缓存 + 有界 fields + 前端熔断**把负载压到 ≤18 次/分钟，
但**是否豁免该纪律由主助手裁定**。若不豁免，可改为**仅由采集侧周期性落库、前端只读库**（那需要历史侧配合，属另一次协调）。

### 8.3 前端侧（**本任务已接线**，路由就绪即自动生效）

| 文件 | 改动 |
|---|---|
| `src/api/types.ts` | 新增 `RawPublicExtras` / `RawRatioBlock`（可选字段，不影响既有契约） |
| `src/api/adapters.ts` | 新增 `adaptPublicExtras()`：只做数值化，**不改语义、不补默认** |
| `src/api/useMarketData.ts` | 新增**有界可见行补扫**（≤6 行/轮、并发 2、节拍 60s、TTL 5min、退避 60s→30min）；**404 立即熔断**；把结果并入既有 `oi.longShortRatio` / `oi.totalUsd`（后者仅原值 null 时兜底） |

**UI 不增删**：只填既有字段；未取到 / 未接线 / OKX 补充位一律保持原版 `—`。

**页面实测（2026-10-06T14:3xZ，`http://127.0.0.1:3000/`，1800×1180）**：
549 行渲染正常；`多空比 —` 保持原版空态；控制台 `{"errors":[]}`；
`public-extras` 请求 **2 次后 75 秒内不再增长**（熔断生效）。
截图：`docs/验收截图/G08-接线就绪-币安公开-路由未接线时多空比保持原版空态-1800x1180.png`。

**真实载荷映射验证**（用模块真实输出喂前端适配器）：
```
真实后端载荷 → adaptPublicExtras() = {"longShortRatio":1.7012,"oiUsd":8134822919.715}
原版 oi 字段   = {"totalUsd":null,"changePercent24h":-1.23,"longShortRatio":null}
接线后 oi      = {"totalUsd":8134822919.715,"changePercent24h":-1.23,"longShortRatio":1.7012}
available=false → null（不填）；null → null（不填）
```

---

## 九、缺口清单（如实列，不用空态当已接通）

| # | 缺口 | 类型 | 归属 |
|---|---|---|---|
| 1 | 参考产品聪明钱 API **401**，且**定义/队列未公开** | 无凭据 + 定义缺失 | 需用户提供凭据**且**给定义；否则不可复刻 |
| 2 | 聪明钱**队列定义**（谁算聪明钱/阈值/分母） | 业务定义缺失 | **需用户/主助手裁定** |
| 3 | **币安公开 API 无**聪明钱 `金额/均价/身份/占比/24h变化` | **字段不存在** | 如实缺失；**不承诺公开 API 能提供**；**不改接** Hyperliquid/OKX |
| 4 | 币安/OKX **标的域**的「聪明钱」 | 无来源 | 只有大户/全局**账户数比**（**不得改名**） |
| 5 | Nansen / Arkham / GMGN / CoinGlass | **需付费密钥/被拦** | 需用户授权与预算（本任务不购买） |
| 6 | 个人账户持仓/订单/成交 | **已取消方向** | 用户确认实盘=公开 |
| 7 | 后端 `adapter.py` **未接线** `public-extras` 路由 | 需 1 行分派 | **主助手协调归属后实施**（§8.2） |
| 8 | 该路由与「前端读取不触发外部采集」纪律冲突 | **需裁定** | **主助手裁定是否豁免**（§8.2） |
| 9 | 「订单簿流动性失衡」口径（档位/加权/单位） | 业务定义缺失 | **需用户/主助手裁定**（数据已备，保持 `—`） |
| 10 | 逐笔成交 / 盘口列表 **原版无模块** | 无位可接 | 不新增模块（用户明确） |
| 11 | OKX 补充位的公开扩展字段未实现 | 未实现 | 需要时再补（**只在币安无该合约时才用**） |

---

## 十、边界与纪律（本轮遵守）

- ✅ 只做了**只读**外部 API 调用（GET 查询类），**未下单、未转账、未授权扩展**
- ✅ **未**新建端口 / 数据库 / 第二个采集器；`:3000` 与 `:8787` 未新增（`:3100`/`:5173`/`:8788`/`:8080` 全程未监听）
- ✅ **未**改后端 `adapter.py` / `realtime_collector.py`（避免与历史侧冲突）；新代码只落在**独立新文件** `public_market_read.py`
- ✅ **未**输出任何密钥；**未**购买任何付费服务
- ✅ **未**把大户/全局账户数比或 taker 量**改名**成聪明钱金额/均价/身份
- ✅ **已撤回** Hyperliquid 候选，**未**再调用、**未**接线
- ✅ 新增模块的对外取数**全部经统一网关**（共享缓存·租约·预算·冷却·封禁），并**遵守网关节流**（`DeferredRequest` 按网关给出的时间等待重试，不绕过）
- ✅ 前端补扫**有界**（≤6 行/轮、并发 2、节拍 60s、TTL 5min）+ **404 熔断**；页面实测控制台 `{"errors":[]}`
- ✅ 只跑了 `npx tsc --noEmit`（通过）；**未**重跑全套 build / 无关测试
- ✅ **未**新增 UI 模块、**未**增删任何原版 UI 元素

## 十一、下一步（等指令，不自行推进）

**v3 本轮已交付**：撤回 Hyperliquid（§零·A）→ 独立只读模块 `public_market_read.py`（§8.1）→ 前端既有字段接线（§8.3）→ 最小路由需求（§8.2）。剩余待办**全部卡在外部**：

1. **等主助手协调归属**：`adapter.py` 加 **1 行分派**接通 `public-extras`（§8.2）——`adapter.py` 正被历史侧使用，**不得同时编辑**
2. **等主助手裁定**：该路由触发对外取数，是否豁免 `adapter.py` 自述的「前端读取不触发外部采集」纪律（§8.2 末段）
3. **等业务裁定**：「订单簿流动性失衡」口径（档位数/加权/单位）；以及若要用「聪明钱」口径，需业务给出**队列定义**（§六）
4. 上述明确后：前端**无需再改**（接线已在位，路由一通即自动生效）；若口径裁定，仅调整派生公式
5. **不做**：改接 Hyperliquid/OKX 替代币安、新增盘口/逐笔模块（原版无位）、新建端口/库/采集器、读取私人账户、输出密钥、购买付费源

> 本任务同时保留 **G05 未通过**状态：前端侧就绪，待后端补 OKX 1d/12h/4h 闭合尾段与 OKX/RLC 中段缺口后**只复验未通过格**，G05 整体未通过前不进入 G06。
