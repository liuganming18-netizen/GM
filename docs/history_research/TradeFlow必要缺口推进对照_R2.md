# TradeFlow必要缺口推进对照 R2

范围：依《网站研究协作范围.md》新增条款和输入资料，静态检查公开bundle；不读任何memory、不访问认证存储、不请求bundle里声明的API。仅输出到本任务`outputs/网站逆向研究/`。

| 研究项 | R1状态 | R2新证据 | R2数量/状态 | 仍需我方正常登录页面核验/未知 |
|---|---|---|---|---|
| 8类导航及客户端路由 | 依先前页面观察8类；未读公开JS | `zT.menus`在`tradeflow_index-27SKEJ8j.js`约753,762处列出8类菜单、路径及角色；router数组约2,048,812列出相应组件 | **8类菜单/8类route代码路径**；client access labels：User/VIP | 服务端路由/权限、是否所有等级用户实际可达；不得以JS menu角色推断后端许可 |
| 六核心用途卡数据映射 | 页面层观察6卡；无代码 | 找到Mie/Nie/ST/Sie/Aie/jie/TT/ET/Vie/Hie/Uie等调用声明；TradingData的Qme直接构造Binance K线URL | **6/6卡都有客户端service/fetch线索；11条核心相对API GET声明+1条外部Binance K线fetch**；实际API调用0次、response schema验证0次 | 服务端各端点权限、数据源、完整schema、历史、字段单位和数据质量 |
| 公开资源 | R1报告说bundle未取得 | 匿名GET精确首方bundle成功；资源清单还含一个首方CSS、一个Cloudflare第三方script | **JS URL 1/1 GET成功**；2,158,821B；CSS GET0、Cloudflare GET0、扩展注入排除 | JS当前版本只代表URL取回时；未验证和页面实时加载版本相同/缓存版本 |
| 交易数据/CVD | 错误页配置字符串，CVD实现未知 | 源码里的TradingView widget config确有`BINANCE:{symbol}.P`、`D`,`Asia/Shanghai`,`1M`、Volume和CVD study；另有1m Binance Kline fetch函数及数据校验 | **客户端行情源配置代码1组**；直接Binance GET函数定义1个，但本轮调用0 | 页面实际嵌入是否成功（既有DOM记录为失败），CVD的TradingView外部study算法/返回值，使用页面的时点和数据范围 |
| 币种列表 | 页面字段及筛选仅DOM证据 | Mie→相对API；jme按symbol、smart L/S ratio、long/short OI share和OI value在前端过滤；字段dataIndex可见 | 源码确认字段键和筛选数量转换 | 服务端原始字段单位仍未知；610/611 DOM count差异、全量结果及历史保存 |
| 资金费率 | 页面表列可见、结算/预测口径未知 | Nie→相对API；filter absolute rate×100；Pme以rate×100四位小数显示；列含rate、interval、mark、next funding、dataAt | 源码确认前端显示缩放/过滤公式1套 | 费率是预测/结算、交易所和时区、币安/OKX对应方式、更新延迟及历史长度 |
| 聪明钱与鲸鱼 | 只有字段线索/两行代数关系 | chart API路径含`includeTopTraderRatios=true`；data key映射多空仓位/鲸鱼仓/均价/账户数/ratio；tooltip显示whalePos/pos百分比 | 源码确认属性键≥16组，显示派生式至少1种 | “聪明钱/巨鲸”账户筛选/地址身份/金额门槛、数据来源、分母、重叠列关系及服务端生成逻辑 |
| 多空比波动 | 只有页面解释 | jie→相对API；组件显示currentRatio、changePercent和absolute change的2m/30m/4h字段，排序引用API返回的changePercent | 源码确认展示字段组1套，服务端返回变化序列 | 时间窗口锚点、百分比计算、值延迟、统计样本范围 |
| 多头共振 | 页面状态字段；阈值未知 | route组件ahe；ET signals列表+Hie active+Vie detail并行取数；列表请求symbol/status/cursor/limit=20；detail显示WATCH/READY/endedAt/endReason/highlights | 源码确认3条client API方法、1条cursor列表路径、1组状态展示 | 后台共振规则、阈值、版本、失败/终止定义、历史完整性。JS展示highlights不证明用这些值触发 |
| 多空比排行榜权益 | R1权益文字有，未定位入口 | router有`/analytics/smart-money-rankings`→xme组件，Aie GET rankings，top20/bottom20；zT菜单无该入口 | 文案1、JS route1、侧栏入口0、正常打开0、API请求0 | 我方正常浏览器验证该client route能否在正常会员级打开；VIP页面声明与route loader未显式VIP门槛存在差异；backend授权仍未知 |
| 指数信息监控权益 | 仅权益文案、无入口 | JS pricing plan含权益字样；feed代码有`index_adjust` event renderer（components/weights变化），但无专属index-monitor route或API字符串；feed默认过滤列表未含此type | 文案1、独立route0、独立API path0、事件renderer1、正常页面打开0 | 正常浏览器查pricing链接、菜单、已有页面子功能；不猜路由。index_adjust feed不能直接认作权益监控 |
| 快讯与事件 feed | 旧输入认为快讯页空列表 | JS TT→`/api/analytics/feed`；Feed支持type/symbol参数及分页；展示事件类型含OI异常、聪明钱共振、index_adjust | API声明1、前端事件types至少3种，默认列表只有其中2类 | 端点响应、每个事件类型实际是否下发、历史/授权和指数监控区别 |
| 页面样例数量 | R1以旧快照为基 | 更新引用较晚的正常页面快照：coin list 610/610标题但DOM虚拟化611；volatility标题728、虚拟表729；funding 13/923，样例文件保存1行；smart-money页字段样例；confluence ONEUSDT细节及此前旧分页记录 | 8导航；6核心卡；5/6有可读实时字段；交易数据图实际数据0；基金费最新样例1行本地JSON；币种list全字段保存0；权益独立入口0/2；完整历史0/6 | 多次页面时点不混成统一快照；样例/页面计数不是后台全量数据或历史行数 |
| 服务端/API真实性 | 一律未知 | 公开JS声明相对`/api/...` paths和共用fetch wrapper；没有显式auth header | endpoints code strings 11项，不代表匿名API；本轮未访问任何一项 | 后端、会话要求、字段源、真实HTTP状态/返回值、数据保留时间均待另有授权后核验 |

## 资源与交付目录

- JS原件：`outputs/网站逆向研究/tradeflow_index-27SKEJ8j.js`
- 请求元数据：`outputs/网站逆向研究/tradeflow_public_js_request.json`
- 公开JS细节摘录/offset：`outputs/网站逆向研究/tradeflow_public_js_source_evidence_R2.json`
- 静态路由/字段搜索索引：`outputs/网站逆向研究/tradeflow_public_js_route_field_index_R2.json`
- 结构总报告：`outputs/网站逆向研究/TradeFlow公开前端结构与数据来源_R2.md`

## R2关闭与未关闭判定

**已关闭的前端公开证据项：**指定公开JS已真实匿名GET并保存；菜单和客户端路由、核心卡服务声明、若干前端字段key、百分比/过滤显示公式、CVD widget配置、排名route和feed事件类型均可定位到JS字符offset；接口访问次数为0。  
**未关闭的产品/数据项：**后台原始返回及schema、站内字段data provider、历史全量、站内算法和信号生成、正常页面验证新发现rank route、指数监控实际独立入口。  
**网站整体验收：**不能称“网站逆向完成”或“数据都能拿到”；R2只完成公开前端静态源码辅助、功能结构证据和缺口分层。