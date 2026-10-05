/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

export type Timeframe = '1m' | '5m' | '15m' | '30m' | '1h' | '4h' | '8h' | '12h' | '1d';

export type SignalStage = '冻结区间' | '突破' | '独立回踩' | '止损目标';
export type SignalDirection = 'long' | 'short' | 'neutral';
export type SignalStatus = 'valid' | 'invalid' | 'forming';

export interface CryptoSignal {
  id: string;
  tokenSymbol: string; // e.g. BTC, ETH, BOB
  rawSymbol: string; // e.g. 1000000BOB -> BOB
  exchange: string; // Binance, Bybit, OKX
  stage: SignalStage;
  direction: SignalDirection;
  timeframe: Timeframe;
  triggerTime: string;
  status: SignalStatus;
  triggerPrice: number;
  currentPrice: number;
  conditionThreshold: string;
  conditionActual: string;
  targetPrice: number;
  stopLossPrice: number;
  validityReason: string;
  confidenceScore: number;
}

export interface SmartMoneySide {
  amountUsd: number; // 持仓额
  ratioPercent: number; // 占比 %
  avgPrice: number; // 均价
  change24h: number; // 24小时变化
}

export interface OIData {
  totalUsd: number;
  changePercent24h: number;
  longShortRatio: number;
}

export interface Candle {
  time: string;
  timestamp: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  isClosed: boolean;
  signalTag?: string;
}

export interface OIPoint {
  time: string;
  timestamp: number;
  oiAmount: number; // 总数量
  oiUsd: number; // 总名义额
  oiDelta: number; // 数量净增减
  oiDeltaUsd: number; // 名义额净增减
  deltaPercent: number; // 增减百分比
  longOiUsd: number; // 多头持仓名义额 (下多)
  shortOiUsd: number; // 空头持仓名义额 (上空)
  longOiAmount: number; // 多头数量
  shortOiAmount: number; // 空头数量
  longRatio: number; // 多头占比 %
  shortRatio: number; // 空头占比 %
  netOiUsd: number; // 多空净差额 (多 - 空)
  hasGap?: boolean;
}

export interface TokenVolumeInfo {
  volume: number;
  volumeUsd: number;
  isSurge: boolean;
  ratio24hPercent: number; // 占24小时总成交量比例 %
  surgeMultiplierVs24hAvg: number; // 对比24小时平均速率的量比 (如 2.4x)
}

export interface TokenMarketItem {
  id: string;
  symbol: string; // Display symbol (prefix removed e.g. BOB)
  originalSymbol: string; // 1000000BOB
  fullName: string;
  exchange: string; // e.g. Binance, OKX, Bybit
  price: number;
  change24h: number;
  marketCapUsd: number; // 流动市值
  circulatingPercent: number; // 流通比例 %
  volume24hUsd: number; // 滚动24小时总成交额
  volume24hAmount: number; // 滚动24小时总成交量
  volumes: Record<Timeframe, TokenVolumeInfo>;
  smartMoneyLong: SmartMoneySide;
  smartMoneyShort: SmartMoneySide;
  oi: OIData;
  fundingRate: number; // 资金费率 (8h)
  predictedFundingRate: number;
  sparkline24h: number[]; // 24 hours mini trend
  trendJudgment: '强烈看涨' | '温和看涨' | '高位震荡' | '温和看跌' | '突破做空';
  activeSignalCount: number;
  candles: Record<Timeframe, Candle[]>;
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
