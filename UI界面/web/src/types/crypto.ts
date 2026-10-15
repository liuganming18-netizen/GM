/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * 周期枚举。
 *
 * **1m 已于 2026-10-11 彻底删除**（库内数据与全部口径一并移除；源头见 53 合同）。
 * 这里不再保留 `'1m'` —— 任何地方传入 `'1m'` 都会在类型层被拒，避免再出现
 * 「前端仍提供 1m 按钮 / 仍请求 1m」这类与后端口径不一致的回归。
 */
export type Timeframe = '5m' | '15m' | '30m' | '1h' | '4h' | '8h' | '12h' | '1d';

export type SignalStage = '冻结区间' | '突破' | '独立回踩' | '止损目标';
export type SignalDirection = 'long' | 'short' | 'neutral';
/**
 * 信号状态：与后端契约**同一枚举**（六态），不再使用本地 valid/invalid/forming。
 * 后端 SIGNAL_STATUSES = candidate/waiting_retest/confirmed/invalidated/expired/undecided。
 * 「形成中」只属于**未收盘 K 线**（Candle.isClosed=false），不是信号状态。
 */
export type SignalStatus =
  | 'candidate'
  | 'waiting_retest'
  | 'confirmed'
  | 'invalidated'
  | 'expired'
  | 'undecided';

// 说明：真实后端对未提供/未计算的字段返回 null。类型统一放行 null，
// 让 UI 走它自带的空态（formatWanYi → '—'、空序列不画线），而不是补 0 或默认值。

export interface CryptoSignal {
  id: string;
  tokenSymbol: string; // e.g. BTC, ETH, BOB
  rawSymbol: string; // 交易所原始 market_id，如 1000000BOBUSDT
  /** 交易所**原始**标识（binance/okx）——精确定位合约用，不用展示名反推 */
  rawExchange?: string;
  exchange: string; // Binance, OKX
  stage: SignalStage | null; // 后端无“策略阶段”字段
  direction: SignalDirection;
  timeframe: Timeframe | null;
  triggerTime: string | null;
  status: SignalStatus | null;
  triggerPrice: number | null;
  currentPrice: number | null;
  conditionThreshold: string | null;
  conditionActual: string | null;
  targetPrice: number | null;
  stopLossPrice: number | null;
  validityReason: string | null;
  confidenceScore: number | null;
  // ── 事件身份（2026-10-11 接线）：原样携带后端真实字段，不用 UI 时间冒充 ──
  /** 确认时刻（事件证据 bar 的收盘时刻）；旧实现用 created_at 冒充触发时间，已纠正 */
  confirmedAt?: string | null;
  /** 事件时刻（= 确认 bar 收盘时刻，UTC） */
  eventTime?: string | null;
  strategyKey?: string | null;
  strategyVersion?: string | null;
  configHash?: string | null;
  dataVersion?: string | null;
  sourceId?: number | null;
  /** 重合时的全部来源掩码（base/spring/sc），执行标签 base 优先 */
  sourceMasks?: string[] | null;
  executionLabel?: string | null;
  /** 当前可执行提示门禁：仅当前启用族 + confirmed + 身份齐全时为 true */
  actionable?: boolean | null;
  actionableReason?: string | null;
  /** 历史事实方向；actionable=false 时 currentDirection 为 null */
  currentDirection?: SignalDirection | null;
  // ── 实时信号层（2026-10-13 信号与持仓接通）──────────────────────────────────
  /**
   * 来源层：`live` = 实时增量生成；`historical_research` = 旧研究窗口导入的历史信号
   * （**明确标注为历史，不冒充当前**）。库内 `signals` 表路径为 undefined。
   */
  origin?: 'live' | 'historical_research' | null;
  /** 展示态（实时层恒 `displayable`）。展示与可执行**严格分列**。 */
  displayState?: string | null;
  /** 可执行态（后端判定，不适用 G01–G05 正式回测门禁） */
  executableState?: string | null;
  /** 结构类型（spring / sc 等） */
  structuralKind?: string | null;
  /** 事件证据 bar 开/收盘时刻（ms 纪元） */
  barOpenTime?: number | null;
  barCloseTime?: number | null;
  /** 结构检测时的低点与 ATR（模拟开仓止损基数） */
  detectionLow?: number | null;
  detectionAtr?: number | null;
  /** 准入状态 */
  admission?: string | null;
  /** 所属启用族（3a/3b/3c/3d）；开仓请求据此路由到对应纸面臂 */
  arm?: string | null;
  /**
   * 事件角色（2026-10-14 信号区清理）：`enterable_arm_entry` = 真实臂入场事件；
   * 结构证据提示（`enterable_structure` 等）不属于当前可交易信号。
   */
  role?: string | null;
  // ── 同一买点多臂合卡（2026-10-14 09 契约 §4）────────────────────────────────
  /**
   * 命中**同一买点**的各臂身份与去向。后端已在展示层把
   * 「同交易所 / 同币种 / 同周期 / 同真实 bar / 同方向」的多臂行**合为一张卡**；
   * 前端只需把 `arms[]` 列出来（各臂名称 + 各臂去向），**不再自行合并**，
   * 也不跨臂抹掉独立实验。不同时间的新事件保留为不同卡。
   */
  arms?: SignalArm[] | null;
  /** 该卡命中的臂数（后端给出，前端不重算） */
  armCount?: number | null;
  /** 命中臂名称列表（如 ["3a","3c","3d"]），顺序与 `arms[]` 一致 */
  armsHit?: string[] | null;
}

/** 合卡卡面上「某一臂」的身份与去向（字段原样来自后端）。 */
export interface SignalArm {
  arm: string | null;
  signalId: string | null;
  status: SignalStatus | null;
  executableState: string | null;
  actionable: boolean | null;
  actionableReason: string | null;
  structuralKind: string | null;
  sourceMasks: string[] | null;
  executionLabel: string | null;
}

/**
 * 聪明钱单侧：三个主展示槽 + 来源附带的身份/巨鲸字段。
 *
 * 真实来源（2026-10-07 实测）：币安**公开无鉴权**接口
 * `/bapi/futures/v1/public/future/smart-money/signal/{overview,details/stats}?symbol=&timeRange=ALL`。
 * 字段语义与取值口径见 `src/api/smartMoneyMapping.ts`（六槽只做字段映射，不重算）。
 *
 * 纪律：来源没给的一律 `null`，不补 0、不用 OI / VWAP / 人数比推算任何一槽。
 */
export interface SmartMoneySide {
  /** 持仓额（USDT）——来源 `longPositions`/`shortPositions`，**已是金额**，不是数量 */
  amountUsd: number | null;
  /** 巨鲸持仓占该侧持仓比例 % ——来源口径「巨鲸仓位 ÷ 该侧仓位」（同一响应内分子分母） */
  ratioPercent: number | null;
  /** 持仓均价 ——来源 `longTradersAvgEntryPrice`/`shortTradersAvgEntryPrice` */
  avgPrice: number | null;
  /** 24小时变化（%）：币安该组接口不提供此口径 ⇒ 恒 `null` */
  change24h: number | null;
  /** 该侧交易员人数 ——来源 `longTraders`/`shortTraders` */
  traders?: number | null;
  /** 该侧巨鲸人数 ——来源 `longWhales`/`shortWhales` */
  whaleTraders?: number | null;
  /** 该侧巨鲸持仓均价 ——来源 `longWhalesAvgEntryPrice`/`shortWhalesAvgEntryPrice` */
  whaleAvgPrice?: number | null;
}

export interface OIData {
  totalUsd: number | null;
  /** 全市场总OI数量；单位保持对应交易所/合约真实口径，不换算。 */
  totalAmount?: number | null;
  quantityUnit?: string | null;
  amountEventTime?: string | null;
  usdEventTime?: string | null;
  source?: string | null;
  changePercent24h: number | null;
  longShortRatio: number | null;
}

export interface Candle {
  time: string;
  timestamp: number;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number | null;
  /** 计价币（USDT）成交额，取自后端 `quote_volume`；后端未给则为 null */
  volumeUsd: number | null;
  isClosed: boolean;
  signalTag?: string;
}

export interface OIPoint {
  time: string;
  timestamp: number;
  oiAmount: number | null; // 总数量
  oiUsd: number | null; // 总名义额
  oiDelta: number | null; // 数量净增减
  oiDeltaUsd: number | null; // 名义额净增减
  deltaPercent: number | null; // 增减百分比
  longOiUsd: number | null; // 多头持仓名义额 (下多)
  shortOiUsd: number | null; // 空头持仓名义额 (上空)
  longOiAmount: number | null; // 多头数量
  shortOiAmount: number | null; // 空头数量
  longRatio: number | null; // 多头占比 %
  shortRatio: number | null; // 空头占比 %
  netOiUsd: number | null; // 多空净差额 (多 - 空)
  hasGap?: boolean;
  /**
   * **显示层「周期对齐」时点（毫秒）** —— 仅当该样本经 `alignOiToTimeframe` 按 K 线周期分桶后才存在。
   *
   * 图表按它落横坐标（让两图共享网格与 K 线网格重合，蜡烛才能等距铺满）；
   * 读数、指标、`timestamp`/`time` 一律仍是**真实快照时点**，不因对齐而改写。
   */
  alignedTimestamp?: number;
}

export interface TokenVolumeInfo {
  volume: number | null;
  volumeUsd: number | null;
  isSurge: boolean | null;
  ratio24hPercent: number | null; // 占24小时总成交量比例 %
  surgeMultiplierVs24hAvg: number | null; // 对比24小时平均速率的量比 (如 2.4x)
}

export interface TokenMarketItem {
  id: string;
  symbol: string; // Display symbol (prefix removed e.g. BOB)
  originalSymbol: string; // 1000000BOB
  fullName: string;
  exchange: string; // e.g. Binance, OKX
  price: number | null;
  change24h: number | null;
  marketCapUsd: number | null; // 流动市值
  /** 市值口径说明（后端如实标注来源；无来源则 null） */
  marketCapNote: string | null;
  /** 流通量**官方原值**（以合约标的币为单位）；无来源则 null */
  circulatingSupply: number | null;
  /** 市值口径的两个时点中**较晚**者（价格时点 vs 流通量时点）；无市值则 null */
  marketCapAsOf: string | null;
  circulatingPercent: number | null; // 流通比例 %
  volume24hUsd: number | null; // 滚动24小时总成交额
  volume24hAmount: number | null; // 滚动24小时总成交量
  volumes: Record<Timeframe, TokenVolumeInfo>;
  /**
   * 各周期「最近一根**已收盘** bar」的计价(USDT)成交额 —— 直接来自后端
   * `/markets` 的 `volume_by_timeframe`（口径：`quote_volume`，只用已收盘 bar）。
   *
   * 为什么需要它：第 3 列每 5 秒在 5m/15m/…/1d 之间轮换，此前只对**当前展开的那一个**
   * 合约有值（只有它才拉 180 根明细 K 线），其余行恒为 `—`。这里让 549 行都有值。
   * 缺该周期 ⇒ 无键（不补 0、不借别的周期顶替）。
   */
  volumeByTimeframe: Partial<Record<Timeframe, number>>;
  smartMoneyLong: SmartMoneySide;
  smartMoneyShort: SmartMoneySide;
  oi: OIData;
  fundingRate: number | null; // 资金费率 (8h)
  predictedFundingRate: number | null;
  sparkline24h: number[]; // 24 hours mini trend（由真实 1h 收盘序列生成；不足则空数组）
  trendJudgment: '强烈看涨' | '温和看涨' | '高位震荡' | '温和看跌' | '突破做空' | null;
  activeSignalCount: number;
  /** 已闭合的历史 K 线序列（后端 `bars[]`）；缺口判定 / 迷你图 / 成交量口径只认这一层。 */
  candles: Record<Timeframe, Candle[]>;
  /**
   * **未收盘临时层**：后端把「当前尚未闭合的那根 bar」单独放在 `forming_bar`，
   * 与 `candles` 严格分离。`null` 表示该周期当前没有未收盘根（如高周期末尾停在归档日）。
   * 该层仅供图表以虚线/空心样式临时叠加，**不得并入 `candles`**，也不得计入缺口、迷你图或成交量口径。
   */
  formingCandles: Record<Timeframe, Candle | null>;
  oiHistory: Record<Timeframe, OIPoint[]> | OIPoint[];
}

export interface Position {
  id: string;
  symbol: string;
  direction: 'long' | 'short';
  amount: number;
  entryPrice: number;
  markPrice: number;
  unrealizedPnl: number;
  unrealizedPnlPercent: number;
  stopLoss: number;
  takeProfit: number;
  riskRatioPercent: number;
  leverage: number;
  openTime: string;
}
