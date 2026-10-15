# TradeFlow 公开JS源码证据位置 R2

**原件：**`tradeflow_index-27SKEJ8j.js`  
**准确来源URL：**`https://www.tradeflow.club/assets/index-27SKEJ8j.js`  
**取回：**2026-10-03T10:32:27.186314Z，匿名GET，HTTP 200，2,158,821B，`text/javascript; charset=utf-8`。SHA-256：`80dac5a26b62e83b86a822af8d6973c1c2342a6faca01181fab337de27cfa7d4`。  
**取回请求记录：**`tradeflow_public_js_request.json`。所有位置是minified单文件的从0起始字符offset（非行号）。文件未执行；所引relative `/api/...`只是代码中声明的调用目标，均未实际请求。

| 字符范围/位置 | 代码事实 | 对应类目/用途 | 不可推导的结论 |
|---:|---|---|---|
| 747,625–约748,050 | 公共前端`xT` helper组装method/JSON params、fetch相对路径，校验`success`后返回`data`；无显式Authorization header | 所有第一方相对API | 无显式Authorization不等于匿名接口；同源浏览器credentials及服务端会话要求本轮没查 |
| 749,001–约751,480 | 分析服务method/path声明（symbols、kline、rankings、volatility rankings、coin-list、funding、feed、signals、signal detail、active signal、单symbol详情） | 六核心卡的服务映射 | 未发请求，不知道后端schema、认证、source provider、历史保留和数据完整性 |
| 753,571–约754,700 | `LT()`身份/可选role loader；`zT.menus`8条分析菜单及Role | 8导航、VIP/普通菜单条件 | 前端menu/guard不是服务端权限最终证据 |
| 758,995–约759,420 | `HT/UT`菜单分类和8页文案 | 导航说明 | 文案只说明用途，不等于策略公式 |
| 1,797,254起 | `B5(data)`显示long/short positions、whale position/count和平均开仓价；占比显示用`whalePos/pos*100` | 聪明钱明细chart tooltip | 只证明页面展示换算式；底层whale定义、用户样本、原始单位未公开 |
| 1,795,380附近 | 智能资金摘要字段：lastPrice、priceChangePercent、fundingRate、openInterestValue、smartMoneyLongShortRatio、long/short OI ratio、topPositionRatio、long/short trader counts | 聪明钱明细 | 字段生产方、时间同步、是否合约/跨交易所聚合未知 |
| 1,799,748附近 | Chart-cache字段数组名：longShortRatioArr、topPosition/topAccount ratios、topAveragePositionConcentration、long/short positions、whales、average prices、trader counts | 聪明钱明细图表 | JS接收/映射字段不能证明服务端实际返回完整历史 |
| 1,809,694–约1,813,549 | `bme/xme/H5`榜单逻辑；读取Aie返回top20/bottom20，列longShortRatio、trader/whale counts、totalPositions；页面文字定义比值为聪明钱多头持仓量/空头持仓量 | 多空比排行榜权益 | 页面定义和字段展示存在，但无数据响应/样本筛选公式；菜单入口缺 |
| 1,813,049–约1,817,263 | `Sme/Cme`多空比波动榜读取jie返回data/updatedAt；显示currentRatio、2m/30m/4h百分比与绝对变化、多空账户/鲸鱼数、totalPositions | 多空比波动 | JS显示两个变化字段，不说明其采样基准和计算方式 |
| 1,817,264–约1,825,050 | `U5/wme/Tme/Ame/jme`币种列表默认过滤器、Mie取数、表格字段和筛选 | 币种列表 | 只揭露前端字段/过滤，不揭露币池来源/数值生成方式 |
| 1,823,892–约1,825,100 | symbol子串匹配；smartMoneyLongShortRatio按输入直接比较；long/short OI ratios×100后与百分数输入比较；openInterestValue和“百万”输入×1e6比较 | 币种列表过滤 | 只能确认客户端假设字段尺度，不能作为后台单位文档 |
| 1,825,227–约1,828,760 | `Mme/Nme/Ime`：Nie取数据，abs(fundingRate)×100筛选，Pme以rate×100四位小数显示；列symbol/rate/interval/mark/next time/dataAt | 资金费率 | 预测/结算、费率来源、周期换算、收费成本未定义 |
| 1,828,736–约1,831,300 | Feed事件类型`index_adjust,oi_anomaly,smart_money_confluence`；Rme缺省类型排除index_adjust、30秒刷新；分页调用feed | 快讯/指数权益线索 | `index_adjust`事件显示不等于独立指数信息监控产品页；feed源/覆盖未知 |
| 1,809,385左右和1,839,025–约1,840,200 | qme展示index调整事件的added/removed成分和weightChanges；Yme渲染feed event types | index_adjust feed事件 | 不说明监控哪些指数、完整成分source或历史 |
| 1,849,000–约1,855,608 | `nhe/ahe`共振列表、cursor加载、并行请求signals list/current active/signal detail；detail显示WATCH/READY、endReason和highlights | 多头共振信号 | `READY`标签/显示highlights不等于真实规则定义；服务端公式/阈值未知 |
| 1,841,499附近 | `Qme`构造Binance `/fapi/v1/klines`、1m、limit/endTime，校验K线输入并截取最近1500bar | 交易数据/嵌入行情 | 本轮只读bundle，不证明函数已运行或真实请求成功 |
| 1,869,005附近 | `lhe`组symbol为`BINANCE:${...}.P`；TradingView widget配置日线、Asia/Shanghai、1个月范围、Volume及Cumulative Volume Delta study | 交易数据图 | 外部widget study配置不等于可读K线/CVD，也不是TradeFlow内部CVD实现 |
| 2,048,812起；2,049,385附近 | React router表将`analytics/smart-money-rankings`映射到xme；同时列出其余分析route | 多空比排行榜入口线索 | JS声明的route未经正常页面打开、数据未取；pricing计划文字与菜单缺项关系未核 |
| 1,970,039附近 | 静态pricing对象列User/VIP权益，含指数信息监控、多空比排行榜 | 两项权益文案 | 不是实际运营方案/会员权限响应、也不证明功能已开放 |

## 字段与公式补充

- 费率页渲染函数`Pme(rate)=(rate*100).toFixed(4)+"%"`；筛选条件为`Math.abs(rate)*100>=minAbsRatePct`。这证明客户端的比例缩放与筛选表达式，不验证API字段的真实单位或已结算/预测属性。
- 币种列表过滤：ratio上下限直接比较`smartMoneyLongShortRatio`；OI share字段先乘100再与百分数阈值比较；总OI筛选把输入的“百万USDT”阈值乘`1e6`。
- 图表tooltip：巨鲸多头/多头持仓`longWhalePos/longPos×100%`，空头对应`shortWhalePos/shortPos×100%`。
- 共振高亮将多个highlight字段乘100显示百分数（含fundingRate另取4位小数）。不能因此认定服务端触发阈值同这些高亮字段。
- 多空波动榜以API返回的`changePercent2min/30min/4h`做排序并直接格式化显示；当前bundle片段没有计算这几个窗口变化值的公式。
- TradingData widget配置用TradingView CVD study；代码中没有发现TradeFlow自定义CVD累计公式/买卖分类来源。