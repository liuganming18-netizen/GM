# TradeFlow公开前端结构与数据来源 R2

**研究日期：**2026-10-03（北京时间）。页面样例取自授权输入资料所记17:57–17:59左右的正常会话快照；本轮源码GET/静态读取约18:32–18:43。  
**范围：**仅按`网站研究协作范围.md`和集中输入资料、既有本任务公开研究产物、用户给定的一个准确公开JS URL静态检查。没有重开登录页，没有读取Cookie/令牌/LocalStorage、没有执行脚本、没有调用JS中发现的任何`/api/...`端点、没有检查请求头或后端响应；没有安装工具、没有写入memory。全部新文件只写入本任务`outputs/网站逆向研究/`。

## 1. 本轮实际资源取得

用户给定准确资源：`https://www.tradeflow.club/assets/index-27SKEJ8j.js`。

- 请求：匿名只读HTTP GET；UTC `2026-10-03T10:32:27.186314Z`至`10:32:28.952394Z`；HTTP 200；`Content-Type: text/javascript; charset=utf-8`；响应/保存长度2,158,821 bytes；SHA-256 `80dac5a26b62e83b86a822af8d6973c1c2342a6faca01181fab337de27cfa7d4`。
- 源码原件：[`tradeflow_index-27SKEJ8j.js`](tradeflow_index-27SKEJ8j.js)。请求证据：[`tradeflow_public_js_request.json`](tradeflow_public_js_request.json)。源码摘录、offset与搜索计数：[`tradeflow_public_js_source_evidence_R2.json`](tradeflow_public_js_source_evidence_R2.json)。
- `网站实际公开资源清单.json`还记录页面引用的同一首方CSS `https://www.tradeflow.club/assets/index-Dzyh-iof.css`、第三方脚本Cloudflare Insights，以及三个页面DOM里同一资源集合；本轮只GET用户给定的首方JS，没有GET CSS/第三方脚本。
- 源码是minified JS，证据位置按文件从0开始的字符offset，不是源码行号。以下均为**静态字符串/代码片段读取**，未运行bundle，也未调用bundle声明的服务端接口。

## 2. 导航与实际客户端路由

JS中的`zT.menus`列出8类分析导航（offset约753,762），并列出角色标签；路由表`Q9=Gr([...])`实际声明对应组件（offset约2,048,812）。这比仅凭页面文字多了一层客户端实现证据，但依然不能证明服务端权限配置与实时可达性。

| 类目 | 代码中的菜单/路由 | 菜单角色标签/路由守卫 | 页面组件和可确认内容 |
|---|---|---|---|
| 聪明钱明细 | `/analytics/smart-money-signal` | 菜单`User`；路由loader `LT()` | `yme`组件，读symbols并显示摘要/时序图字段 |
| 多头共振信号 | `/analytics/confluence-signals` | 菜单`VIP`；路由`LT([PT.VIP])` | `ahe`组件；列表/当前活动信号/选中信号分别取signals相关服务 |
| 币种列表 | `/analytics/coin-list` | 菜单`VIP`；路由`LT([PT.VIP])` | `jme`组件；API返回后有客户端筛选 |
| 自选币种 | `/analytics/favorite-symbols` | 菜单`VIP`；路由`LT()` | 页面/账户相关代码在JS中，但本轮不读私有数据 |
| 多空比波动分析 | `/analytics/smart-money-volatility` | 菜单`User`；路由`LT()` | `Cme`组件，显示变化字段和排序 |
| 交易数据 | `/analytics/trading-data` | 菜单`User`；路由`LT()` | `lhe`组件，symbol selector和TradingView widget配置 |
| 资金费率 | `/analytics/funding-rates` | 菜单`User`；路由`LT()` | `Ime`组件，费率筛选及格式化 |
| 快讯 | `/analytics/market-feed` | 菜单`User`；路由`LT()` | `Zme`/`Rme`，调用`/api/analytics/feed`；与共振信号专页的`/api/analytics/signals`不是同一接口 |

`LT()`静态实现要求已登录；若传入role数组还作角色检查。菜单和路由guard是客户端代码证据，**不等于**我方已打开所有路由，也不等于服务端API实际允许的权限。

### 两项权益的R2核查

- **多空比排行榜：**本轮静态代码发现客户端路由`/analytics/smart-money-rankings`（route-table offset 2,049,385），绑定组件`xme`；组件从`Aie()`读取`top20/bottom20`，其服务声明为GET `/api/analytics/smart-money-signal/rankings`（service offset约749,896），页面文案写“多空比=聪明钱多头持仓量/空头持仓量”、显示多头最强/空头最强Top20及账户/鲸鱼数。**真实客户端route已在源码找到，但`zT.menus`的8项中没有这个菜单入口，本轮没有正常打开该路由，也没有调用其API；故计“代码路由1项、正常可见菜单0项、正常页面功能复核0项”。** pricing文案说VIP功能；router loader代码没有显式传VIP role（`LT()`），与会员文案/菜单缺席的关系仍未知，可能由服务端或其他页面控制，不据此猜测。
- **指数信息监控：**pricing plan对象中可见权益字符串`指数信息监控`（offset约1,970,039）；未找到独立`/analytics/...index...` route或`/api/...index...`服务字符串（对该bundle的静态文本搜索结果0条）。但JS有feed事件类型`index_adjust`、标签“指数调整”，`qme`组件可显示added/removed成分、weightChanges（offset约1,828,736及1,837,717）。这是市场事件feed的一个事件渲染类型，不等于独立的“指数信息监控”页。两者区分后：**独立页面/API未发现；feed内index_adjust事件类型有代码证据；正常页面入口/数据仍待主助手复核。**

## 3. JS声明的数据服务端点（仅代码证据，不调用）

共享`xT`封装（offset 747,625）用`fetch`访问相对路径、GET/params构造query string、检查JSON `success`并返回`data`；没有在该封装中明写Authorization header。浏览器同源默认行为和服务端访问控制本轮未检查，因此不能把这些端点称为匿名公开API。本轮**未对以下任何端点发请求**。

| 页面用途 | JS中方法/路径模板 | 代码位置（约，字符offset） | 静态上可确认什么 | 服务端仍未知 |
|---|---|---:|---|---|
| 聪明钱可选币种 | `ST()` GET `/api/analytics/smart-money-signal/symbols` | 749,001 | selector用返回symbols列表 | 币种来源、筛选、会员、历史覆盖 |
| 聪明钱/K线指标 | `Sie(symbol)` GET `/api/analytics/smart-money-signal/${symbol}/kline?includeTopTraderRatios=true` | 749,087 | 查询参数含`includeTopTraderRatios=true`；图表映射若干时间序列字段 | 响应体、字段单位/来源、计算和历史范围 |
| 聪明钱排名 | `Aie()` GET `/api/analytics/smart-money-signal/rankings` | 749,896 | 组件字段期望`top20,bottom20,updatedAt` | 样本、比率源、排序和权限 |
| 多空比波动榜 | `jie()` GET `/api/analytics/smart-money-signal/volatility-rankings` | 749,984 | 页面读`data,updatedAt` | 各窗口如何计算、分母、聚合对象/权限 |
| 币种列表 | `Mie()` GET `/api/analytics/coin-list` | 750,030 | 返回数据赋给`rows=e.data`，客户端字段筛选 | 原始数据源/计算、完整币池、权限 |
| 费率表 | `Nie()` GET `/api/analytics/funding-rates` | 750,100 | 返回`rows=e.data, updatedAt` | 预测/结算口径，source/timezone/覆盖 |
| 快讯/事件feed | `TT(params)` GET `/api/analytics/feed` | 750,854 | Feed组件会传事件type、symbol等参数 | `index_adjust`等事件数据何处产生、历史/权限 |
| 共振/信号列表 | `ET(params)` GET `/api/analytics/signals` | 750,930 | 页面传`symbol,status,limit=20,cursor`，读取`data,nextCursor,hasMore`；load more沿cursor继续 | 后台筛选/排序/完整历史/每个status定义 |
| 信号详情 | `Vie(encodeURIComponent(id))` GET `/api/analytics/signals/{id}` | 750,972 | 选中信号按ID取详情 | 内容schema、规则来源、版本/可用时间 |
| 当前活动信号 | `Hie(encodeURIComponent(symbol))` GET `/api/analytics/smart-money-signal/{symbol}/active-signal` | 751,103 | 详情会并行取active signal | 触发算法/当前服务端状态依据 |
| 单symbol列表详情 | `Uie(symbol)` GET `/api/analytics/coin-list/{symbol}` | 751,222 | 静态服务声明 | 响应/使用页和字段未知 |
| TradingData symbol list与K线图 | 复用`ST()`；图表直接用外部函数`Qme`请求Binance K线 | `ST`约749,001；`Qme`约1,841,499；widget约1,869,005 | JS用symbol组`BINANCE:${normalizedSymbol}.P`；Qme通过`https://fapi.binance.com/fapi/v1/klines`取`interval=1m`、limit、可选endTime，并校验OHLC范围/时间；widget静态配置`interval=D`,`timezone=Asia/Shanghai`,`range=1M`,`studies=[Volume,Cumulative%1Volume%1Delta]` | 此为bundle声明的公开K线请求/TradingView配置，不是本轮实际调用，也不证明TradeFlow TradingView图已成功呈现；CVD系TradingView study字符串，未发现站内CVD计算函数 |

共 **11条与核心分析相关的第一方相对路径GET声明**（上表），另有 **1条第三方Binance公开K线URL构造**。bundle中还含账户/复制交易等其它API字符串，不在本轮调用或分析范围内。所有`/api/...`均仅为前端代码声明，URL为相对路径；helper调用`fetch`但没有显式写Authorization header，实际权限/是否依赖浏览器同源会话、返回字段和服务端数据源均未核。本轮对这些声明端点的请求次数为0。

## 4. 六个核心研究卡：实际字段映射与显示计算

> 下述为已下载公开bundle的客户端代码实现证据，定位采用`tradeflow_index-27SKEJ8j.js#字符offset`；不是服务端schema，不代表响应已获取。源代码由本轮匿名HTTP GET保存；没有执行JS。

### 4.1 币种列表

- 页面组件：`jme()`约offset 1,823,892；数据由`Mie()`调用上表`GET /api/analytics/coin-list`，消费`e.data`和`e.updatedAt`。表格字段映射包括`symbol,lastPrice,priceChangePercent,fundingRate,openInterestValue,smartMoneyLongShortRatio,smartMoneyLongOiRatio,smartMoneyShortOiRatio,smartMoneyLongTraders,smartMoneyLongWhales,smartMoneyShortTraders,smartMoneyShortWhales,topAveragePositionConcentration,topPositionRatio,topAccountRatio,globalAccountRatio,dataAt`；部分列由render函数格式化。
- 客户端明确的筛选换算（offset 1,823,892附近）：
  - symbol：trim后大写子串匹配。
  - `smartMoneyLongShortRatio`：直接与min/max输入值比较，不乘100。
  - `smartMoneyLongOiRatio`、`smartMoneyShortOiRatio`：源字段乘100后与页面百分数阈值比较；据此可确认UI把这两个字段当比例小数来展示/比较，但API原值的单位定义仍需服务端说明。
  - `openInterestValue`与总OI输入值×1,000,000比较；页面输入标签为总OI（百万USDT），这段代码证明客户端换算，不证明服务端返回值的精确定义/计价币范围。
  - 标题行显示`筛选后 filteredRows / rows`；updatedAt通过`Asia/Shanghai`时间格式化helper显示。
- 页面列名“聪明钱/大户/鲸鱼”不等于代码解释了账户集合或统计构造。全量币池、退市合约、响应授权与原始source仍未知。
- 输入样例关系复核（仅数字显示、非原始响应）：0GUSDT页面`long OI%=36.70`、`short OI%=16.37`，两者相除≈2.2419，与页面`smart money ratio=2.2419`相合；AWEUSDT显示57.93%、1.74%、比值33.3592，显示值相除≈33.2931，不完全相等。舍入/时差/分母均未确认，因此这是一行吻合、一行差异的关系线索，不认定精确公式，也不把这几个相关字段当多条独立证据。对应页面关系审阅在`输入资料/页面指标关系审阅.md`。

### 4.2 交易数据

- 组件`lhe()`约offset 1,869,005；币种selector调用`ST()`读`/api/analytics/smart-money-signal/symbols`。选择后symbol文本形成`BINANCE:${hT(symbol)}.P`。
- TradingView widget配置中可见`interval=D`、`timezone=Asia/Shanghai`、`range=1M`、`studies=["Volume@tv-basicstudies","STD;Cumulative%1Volume%1Delta"]`（offset 1,869,005–1,870,100左右）。配置声明了内嵌图表和CVD study，不是成功加载/实际数据证据，也不是TradeFlow实现的CVD算法。
- 自定义K线取数函数`Qme()`约offset 1,841,499直接构造`https://fapi.binance.com/fapi/v1/klines`，参数为选中symbol、`interval=1m`、limit、可选endTime；option `credentials:"omit"`；代码检查返回数组并过滤非安全时间戳、非正价格及OHLC不满足high/low关系的数据。这证明客户端**代码意图/配置**使用币安1分钟K线；不证明请求被执行、所有市场指标也来自币安，亦不改变页面当时图表连接失败的正常DOM观察。
- 此JS没有发现明文“CVD”单词，但配置字符串中有URL编码样式的`Cumulative%1Volume%1Delta`。未发现自定义CVD累计算法/原始主动方成交输入代码，因此CVD公式仍未知。

### 4.3 聪明钱明细

- 静态页面摘要使用的字段键包括`lastPrice,priceChangePercent,fundingRate,openInterestValue,smartMoneyLongShortRatio,smartMoneyLongOiRatio,smartMoneyShortOiRatio,topPositionRatio,smartMoneyLongTraders,smartMoneyShortTraders`（offset约1,795,380起）。页面摘要formatter将两侧OI share通过`P5(e)=(e*100).toFixed(2)+"%"`展示；ratio以数值格式展示；OI值formatter按K/M/B缩写。
- chart cache映射的时序字段名（offset约1,799,748附近）：`longShortRatioArr,topPositionLongShortRatioArr,topAccountLongShortRatioArr,topAveragePositionConcentrationArr,longPosArr,shortPosArr,longWhalePosArr,shortWhalePosArr,longAvgPriceArr,longWhalesAvgPriceArr,shortAvgPriceArr,shortWhalesAvgPriceArr,longTradersArr,shortTradersArr,longWhalesArr,shortWhalesArr`。这些可确认客户端期望并映射这些属性；不证明服务端一定长期提供所有列。
- `B5()`代码（约offset 1,797,254）明确显示鲸鱼持仓占多头/空头持仓比例分别按`longWhalePos/longPos*100`和`shortWhalePos/shortPos*100`格式化为一位小数。这是一个展示派生量；raw长/短仓位、鲸鱼筛选和账户样本定义仍是服务端/规则未知。
- symbols API与`/smart-money-signal/{symbol}/kline?includeTopTraderRatios=true`在bundle中有方法声明；本轮未调用。页面文字中的“Binance直连”只明确对应TradingView chart，不应外推到该API数据源。

### 4.4 多空比波动分析

- 组件`Cme()`约offset 1,813,402；取`jie()`返回`data,updatedAt`。页面显示字段键包括`currentRatio,changePercent2min,change2min,changePercent30min,change30min,changePercent4h,change4h,longTraders,longWhales,shortTraders,shortWhales,totalPositions`（offset约1,815,083–1,817,263）。
- 列排序读取`changePercent2min/30min/4h`；页面分别显示百分比字段和括号中的绝对变化字段。该代码只确认客户端使用/显示哪些值，不显示计算的时间锚点/公式，也不证明多空比的账户口径。
- 网站说明的“情绪增强/减弱”是页面注释。客户端直接显示API返回变化字段；未找到本地计算窗口差值的代码。
- 页面快照关系审阅的KNCUSDT示例为currentRatio 1.6550、2m delta +0.0591/percent +3.70%、30m +0.0747/+4.73%、4h +0.1852/+12.60%。用`delta/(currentRatio-delta)`算得约3.7032%、4.7276%、12.6004%，与页面百分比接近；只能作为旧值分母的客户端/页面关系线索，不证明服务器对齐窗口、原始采样时刻或变化百分比实现。

### 4.5 资金费率

- `Mme` store约offset 1,825,227通过`Nie()`读`/api/analytics/funding-rates`，直接消费`e.data`和`e.updatedAt`。
- `applyFilter()`（offset约1,825,700）执行`Math.abs(fundingRate)*100 >= minAbsRatePct`及symbol子串筛选；默认`minAbsRatePct=.05`。formatter `Pme(rate)=(rate*100).toFixed(4)+"%"`。表列键是`symbol,fundingRate,fundingIntervalHours,markPrice,nextFundingTime,dataAt`；`nextFundingTime`由`gT`以Asia/Shanghai格式显示，`fundingIntervalHours`原样显示为小时。
- 这些实现证明前端将API `fundingRate`按小数比例×100呈现/过滤；网页表本身没有说明它是预测、当前还是已结算费率、使用哪个交易所或是否可追溯完整历史；backend unknown。

### 4.6 多头共振信号

- route/component：`Q9` router表offset约2,048,812含`analytics/confluence-signals`→`ahe`，loader`LT([PT.VIP])`；具体是否实际可访问未在本轮导航验证，正常菜单也标VIP。
- 服务声明：`ET(params)`→`GET /api/analytics/signals`；`Vie(id)`→`GET /api/analytics/signals/{id}`；`Hie(symbol)`→`GET /api/analytics/smart-money-signal/{symbol}/active-signal`。`querySignals(symbol)`在`nhe`附近(offset约1,851,300–1,853,900)并行调用信号列表、active signal、所选signal detail；列表参数含symbol/status/limit=20/cursor，更早条目继续传cursor。三个端点均只记录于公开JS，没有对其发请求。
- 前端显示字段包含status、watch/ready/ended anchors、readyAt、endReason、触发价、lastEvaluatedAt、expiresAt、cooldownUntil、dataStaleSince等。`READY`显示“核心条件已触发”；payload highlights显示`concentrationChange,longWhaleQtyChange,longEntryPriceChange,oiChange,priceToEntry,fundingRate`，generic `Z5`按`value*100`转百分比显示。它是字段显示和单位格式代码，不是触发算法本身。
- 代码字符串里的事件feed另有`index_adjust/oi_anomaly/smart_money_confluence`；`Rme`默认过滤types为`oi_anomaly,smart_money_confluence`，feed是`/api/analytics/feed`，不同于上面的confluence signals API。不要混为同一个信号系统。

## 5. 两项权益与聪明钱/CVD的R2结论

### 多空比排行榜

- 新发现的客户端route是`/analytics/smart-money-rankings`，在router配置offset 2,049,385绑定`xme`组件；`bme/xme`（offset约1,809,694/1,811,742）读取`Aie()`→GET `/api/analytics/smart-money-signal/rankings`的`top20,bottom20,updatedAt`，页面文字称多空比是“聪明钱多头持仓量/空头持仓量”，分别呈现多头/空头最强TOP20及long/short trader、whale计数字段。
- JS sidebar `zT.menus`八项没有该route的link；所以实际计数：**权益文案1项、源码route声明1项、菜单链接0项、本轮正常页面打开0项、接口请求0项**。路由loader调用`LT()`但没有显式传VIP角色，而pricing静态配置把该权益放VIP，权限/页面能否由正常账号访问仍以主助手随后既有浏览器核验为准；未去试探访问。

### 指数信息监控

- pricing静态plan数据含权益文字“指数信息监控”（字符约offset 1,970,039）；在bundle里静态检索专属`/analytics/...index...` route匹配0、`/api/...index...`路径匹配0。
- bundle里有`index_adjust`事件类型及`qme()`展示新增/移除成分与weightChanges（offset约1,828,736和1,837,717），但该事件出现在feed renderer；feed默认筛选集合是OI异动和聪明钱共振。**不能据此认定存在专属指数监控页或已经定位权益入口**。
- 计数：权益文案1项、独立源码route 0、专属API路径字符串0、正常页面打开0；feed内有1种`index_adjust`事件渲染类型。此状态仍是“实现线索/功能入口待主助手正常浏览器核验”。

### 聪明钱与鲸鱼

- JS确有summary/chart字段键及显示百分比转换，`B5()`把long/short whale position占相应long/short position的比值展示为百分比；`Ume`等映射有鲸鱼账户数量、多空持仓、平均价格、集中度、smartMoneyRatio等名称。
- 这些只揭示客户端属性名和显示关系，不揭示账户池、鲸鱼阈值、交易所/币种覆盖、分子分母原始数据口径、服务端计算、去重和历史版本。不能由一条数值关系反推“聪明钱”后台算法。

## 6. 完成度计数（R2）

| 任务项 | 证据数量 | 完成边界 |
|---|---:|---|
| 用户给定公开JS资源 | 1个URL，1次匿名GET，HTTP200，2,158,821B，SHA-256已记 | 成功获取的是静态bundle；未执行JS |
| 同DOM资源清单 | 1个首方JS、1个首方CSS、1个第三方Cloudflare脚本；另有扩展注入脚本被排除 | 本轮实际GET：首方JS 1；CSS 0；第三方0；extension0 |
| 菜单导航类目 | 8/8在JS menu配置中有对应项目和path/role | 当前client menu/route code；非后端全站能力 |
| 核心研究用途卡 | 6/6有至少一条前端service/fetch声明或读字段映射 | 不是6/6字段口径闭合；服务端来源、历史和样例需分开 |
| 核心分析API GET声明 | 11条静态analytics path模板；另外1条直接Binance K线fetch构造 | 本轮实际请求JS中API路径：0条；未知后端response schema不计已取 |
| 多空比排行榜 | 文案1；编译bundle route 1；菜单link 0；本轮正常打开0；排名API调用0 | route/组件/service code存在，访问和服务端权限未验证 |
| 指数信息监控 | pricing文案1；专属route 0；专属API path 0；feed内`index_adjust` renderer 1；正常打开0 | renderer是index composition event，不等于监控页 |
| 页面当前/历史样例 | 依据输入资料：8导航记录，6核心页记录，至少5页有市场字段，交易图实际值0；资金费率较晚快照13/923且输入文件一条行样例；币种列表610数据行节点但未全量解析；共振至少4个命名token来自不同时间记录，1个加载更多旧条目窗口 | snapshot行数不代表历史库；不同观察时间点计数不合并成一张“总表” |
| JS源码确认的本地显示计算 | funding率×100格式/绝对值筛选；coin list OI百分比字段×100筛选、总OI阈值×1e6；smart-money whale share分子/分母比例显示；TradingData K线参数校验 | 这些是客户端展示/过滤，不证明输入单位和服务端值准确；CVD只见TradingView配置，无自有计算 |

## 7. 证据源与可复核路径

- JS原件：`outputs/网站逆向研究/tradeflow_index-27SKEJ8j.js`
- GET请求清单：`outputs/网站逆向研究/tradeflow_public_js_request.json`
- 有字符偏移及代码上下文：`outputs/网站逆向研究/tradeflow_public_js_source_evidence_R2.json`
- 搜索/字符串匹配索引：`outputs/网站逆向研究/tradeflow_public_js_route_field_index_R2.json`
- 所有6核心页的先前正常DOM/字段/样例观察来自协作目录`输入资料/网站逐类拆分与逆向证据.md`、`网站字段脱敏样例.json`、`页面指标关系审阅.md`；这些输入已注明各自观察时间，不能把本轮GET JS等同于本轮重新登录或页面更新。
- 本轮API使用结论仅为“JS静态声明”。不打开静态代码里出现的`/api/...`端点、不读取response body、不检查认证信息。客户端的fetch wrapper没有显式加Authorization header，但同源浏览器默认credential行为与服务端控制未核，不推断匿名访问。

**R2结论：**现在可以确认一份公开JS里存在菜单/路由、6核心页面的数据服务调用声明、客户端字段映射/部分展示公式，以及多空比排行榜的隐藏菜单路由代码；指数信息监控只见权益文字和feed事件类型。服务端API实际数据、权限、字段口径、历史覆盖、算法阈值仍未知。本轮不声称完成网站复制，不声称拿齐网站数据。