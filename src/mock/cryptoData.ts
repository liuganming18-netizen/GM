/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import { CryptoSignal, Position, Timeframe, TokenMarketItem, Candle, OIPoint } from '../types/crypto';

// Helper to generate simulated candles with timeframe-accurate interval
function generateCandles(basePrice: number, volatility: number, count: number = 32, tf: Timeframe = '1m'): Candle[] {
  const candles: Candle[] = [];
  let currentPrice = basePrice;
  const now = Date.now();

  const stepMsMap: Record<Timeframe, number> = {
    '1m': 60 * 1000,
    '5m': 5 * 60 * 1000,
    '15m': 15 * 60 * 1000,
    '30m': 30 * 60 * 1000,
    '1h': 60 * 60 * 1000,
    '4h': 4 * 60 * 60 * 1000,
    '8h': 8 * 60 * 60 * 1000,
    '12h': 12 * 60 * 60 * 1000,
    '1d': 24 * 60 * 60 * 1000,
  };

  const stepMs = stepMsMap[tf] || 60 * 1000;

  for (let i = count; i >= 0; i--) {
    const timestamp = now - i * stepMs;
    const dateObj = new Date(timestamp);
    let timeStr = dateObj.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
    if (tf === '4h' || tf === '8h' || tf === '12h') {
      timeStr = `${dateObj.getMonth() + 1}/${dateObj.getDate()} ${dateObj.getHours()}:00`;
    } else if (tf === '1d') {
      timeStr = `${dateObj.getMonth() + 1}-${dateObj.getDate()}`;
    }

    const change = (Math.random() - 0.48) * volatility * basePrice;
    const open = currentPrice;
    const close = Number((open + change).toFixed(2));
    const high = Number((Math.max(open, close) + Math.random() * volatility * basePrice * 0.8).toFixed(2));
    const low = Number((Math.min(open, close) - Math.random() * volatility * basePrice * 0.8).toFixed(2));
    const volume = Math.floor(Math.random() * 500000 + 100000);
    const isClosed = i > 0;

    let signalTag: string | undefined = undefined;
    if (i === 12) signalTag = '突破点 (Breakout)';
    if (i === 5) signalTag = '独立回踩 (Pullback)';

    candles.push({
      time: timeStr,
      timestamp,
      open,
      high,
      low,
      close,
      volume,
      isClosed,
      signalTag,
    });

    currentPrice = close;
  }
  return candles;
}

// Dynamically generate Upper-Short / Lower-Long OI points for any specific candle array
export function generateOIPointsForCandles(baseOI: number, candles: Candle[]): OIPoint[] {
  let curOI = baseOI;

  return candles.map((candle, i) => {
    const priceChange = candle.close - candle.open;
    const baseDelta = Math.round((Math.random() - 0.46) * 35000);
    const delta = priceChange >= 0 ? Math.abs(baseDelta) : -Math.abs(baseDelta);
    curOI = Math.max(curOI + delta, baseOI * 0.7);

    const totalUsd = Math.round(curOI * 66.5);
    const deltaUsd = Math.round(delta * 66.5);
    const deltaPercent = Number(((delta / (curOI - delta || 1)) * 100).toFixed(2));

    const longRatio = Number((50 + (Math.sin(i / 3) * 8 + (Math.random() - 0.4) * 4)).toFixed(1));
    const shortRatio = Number((100 - longRatio).toFixed(1));
    const longOiUsd = Math.round(totalUsd * (longRatio / 100));
    const shortOiUsd = Math.round(totalUsd * (shortRatio / 100));
    const longOiAmount = Math.round(curOI * (longRatio / 100));
    const shortOiAmount = Math.round(curOI * (shortRatio / 100));
    const netOiUsd = longOiUsd - shortOiUsd;

    return {
      time: candle.time,
      timestamp: candle.timestamp,
      oiAmount: Math.round(curOI),
      oiUsd: totalUsd,
      oiDelta: delta,
      oiDeltaUsd: deltaUsd,
      deltaPercent,
      longOiUsd,
      shortOiUsd,
      longOiAmount,
      shortOiAmount,
      longRatio,
      shortRatio,
      netOiUsd,
      hasGap: i === 18 ? true : false,
    };
  });
}

// Timeframe divisors for 24-hour baseline comparison
const tfDivisors: Record<Timeframe, number> = {
  '1m': 1440,
  '5m': 288,
  '15m': 96,
  '30m': 48,
  '1h': 24,
  '4h': 6,
  '8h': 3,
  '12h': 2,
  '1d': 1,
};

export function createTokenVolumes(
  vol24hUsd: number,
  vol24hAmount: number,
  surges: Partial<Record<Timeframe, boolean>> = {}
): Record<Timeframe, TokenVolumeInfo> {
  const timeframes: Timeframe[] = ['1m', '5m', '15m', '30m', '1h', '4h', '8h', '12h', '1d'];
  const res: Partial<Record<Timeframe, TokenVolumeInfo>> = {};

  timeframes.forEach((tf) => {
    const divisor = tfDivisors[tf];
    const isSurge = !!surges[tf];
    const multiplier = isSurge ? 1.42 : 0.97;
    const volumeUsd = Math.round((vol24hUsd / divisor) * multiplier);
    const volume = Number(((vol24hAmount / divisor) * multiplier).toFixed(1));
    const ratio24hPercent = Number(((volumeUsd / vol24hUsd) * 100).toFixed(2));
    const surgeMultiplierVs24hAvg = Number(multiplier.toFixed(2));

    res[tf] = {
      volume,
      volumeUsd,
      isSurge,
      ratio24hPercent,
      surgeMultiplierVs24hAvg,
    };
  });

  return res as Record<Timeframe, TokenVolumeInfo>;
}

// Helper to generate Binance-style OI histogram points with upper-short / lower-long
function generateOIPoints(baseOI: number, count: number = 35): OIPoint[] {
  const points: OIPoint[] = [];
  let curOI = baseOI;
  const now = Date.now();
  const stepMs = 60 * 1000;

  for (let i = count; i >= 0; i--) {
    const time = new Date(now - i * stepMs).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    const delta = Math.round((Math.random() - 0.46) * 45000);
    curOI += delta;
    const totalUsd = Math.round(curOI * 66.5);
    const deltaUsd = Math.round(delta * 66.5);
    const deltaPercent = Number(((delta / (curOI - delta || 1)) * 100).toFixed(2));

    // Long ratio between 45% and 65%
    const longRatio = Number((50 + (Math.sin(i / 3) * 8 + (Math.random() - 0.4) * 4)).toFixed(1));
    const shortRatio = Number((100 - longRatio).toFixed(1));
    const longOiUsd = Math.round(totalUsd * (longRatio / 100));
    const shortOiUsd = Math.round(totalUsd * (shortRatio / 100));
    const longOiAmount = Math.round(curOI * (longRatio / 100));
    const shortOiAmount = Math.round(curOI * (shortRatio / 100));
    const netOiUsd = longOiUsd - shortOiUsd;

    points.push({
      time,
      timestamp: now - i * stepMs,
      oiAmount: Math.round(curOI),
      oiUsd: totalUsd,
      oiDelta: delta,
      oiDeltaUsd: deltaUsd,
      deltaPercent,
      longOiUsd,
      shortOiUsd,
      longOiAmount,
      shortOiAmount,
      longRatio,
      shortRatio,
      netOiUsd,
      hasGap: i === 18 ? true : false,
    });
  }
  return points;
}

export const initialSignals: CryptoSignal[] = [
  {
    id: 'sig-1',
    tokenSymbol: 'BTC',
    rawSymbol: 'BTCUSDT',
    exchange: 'Binance',
    stage: '突破',
    direction: 'long',
    timeframe: '15m',
    triggerTime: '14:28:10',
    status: 'valid',
    triggerPrice: 66850,
    currentPrice: 67120,
    conditionThreshold: '突破上沿 $66,800 + OI增幅 > 3.5%',
    conditionActual: '现价 $67,120 + 实际OI增幅 +4.82%',
    targetPrice: 68500,
    stopLossPrice: 66200,
    validityReason: '高位放量真突破，聪明钱多头流入净额超 38M USD',
    confidenceScore: 92,
  },
  {
    id: 'sig-2',
    tokenSymbol: 'SOL',
    rawSymbol: 'SOLUSDT',
    exchange: 'Binance',
    stage: '独立回踩',
    direction: 'long',
    timeframe: '5m',
    triggerTime: '14:15:02',
    status: 'valid',
    triggerPrice: 154.2,
    currentPrice: 155.8,
    conditionThreshold: '回踩均线支撑 $153.80 不破 + 缩量整理',
    conditionActual: '触及 $154.10 快速反弹，聪明钱买单挂满',
    targetPrice: 162.0,
    stopLossPrice: 151.5,
    validityReason: '回踩测试有效，空头主动抛压已被全部吸收',
    confidenceScore: 88,
  },
  {
    id: 'sig-3',
    tokenSymbol: 'ETH',
    rawSymbol: 'ETHUSDT',
    exchange: 'Bybit',
    stage: '冻结区间',
    direction: 'neutral',
    timeframe: '1h',
    triggerTime: '13:50:40',
    status: 'forming',
    triggerPrice: 2640,
    currentPrice: 2638,
    conditionThreshold: '波动率挤压带 < 1.2% + 筹码密集分布',
    conditionActual: '振幅 0.88%，即将迎来变盘选择',
    targetPrice: 2750,
    stopLossPrice: 2580,
    validityReason: '区间横盘吸筹阶段，建议观察边界突破情况',
    confidenceScore: 76,
  },
  {
    id: 'sig-4',
    tokenSymbol: 'BOB',
    rawSymbol: '1000000BOB',
    exchange: 'Binance',
    stage: '突破',
    direction: 'short',
    timeframe: '5m',
    triggerTime: '13:12:00',
    status: 'invalid',
    triggerPrice: 0.0384,
    currentPrice: 0.0392,
    conditionThreshold: '跌破下轨 0.0380 伴随持仓量流失',
    conditionActual: '假跌破快速收回，空头动能不足',
    targetPrice: 0.035,
    stopLossPrice: 0.0405,
    validityReason: '价格重回震荡中枢，信号已自动失效作废',
    confidenceScore: 35,
  },
];

export const initialTokens: TokenMarketItem[] = [
  {
    id: 'btc',
    symbol: 'BTC',
    originalSymbol: 'BTCUSDT',
    fullName: 'Bitcoin',
    exchange: 'Binance',
    price: 67120.5,
    change24h: 3.42,
    marketCapUsd: 1326300000000, // 1.33万亿
    circulatingPercent: 94.1,
    volume24hUsd: 59736800000,
    volume24hAmount: 890000,
    volumes: {
      '1m': { volume: 842.1, volumeUsd: 56521660, isSurge: true, ratio24hPercent: 0.09, surgeMultiplierVs24hAvg: 1.36 },
      '5m': { volume: 3820.5, volumeUsd: 256431960, isSurge: true, ratio24hPercent: 0.43, surgeMultiplierVs24hAvg: 1.24 },
      '15m': { volume: 11450.2, volumeUsd: 768537424, isSurge: true, ratio24hPercent: 1.29, surgeMultiplierVs24hAvg: 1.24 },
      '30m': { volume: 22800.0, volumeUsd: 1530345600, isSurge: false, ratio24hPercent: 2.56, surgeMultiplierVs24hAvg: 1.02 },
      '1h': { volume: 46200.4, volumeUsd: 3101000000, isSurge: true, ratio24hPercent: 5.19, surgeMultiplierVs24hAvg: 1.25 },
      '4h': { volume: 168000.0, volumeUsd: 11276160000, isSurge: false, ratio24hPercent: 18.88, surgeMultiplierVs24hAvg: 1.13 },
      '8h': { volume: 312000.0, volumeUsd: 20941440000, isSurge: false, ratio24hPercent: 35.06, surgeMultiplierVs24hAvg: 1.05 },
      '12h': { volume: 450000.0, volumeUsd: 30204000000, isSurge: false, ratio24hPercent: 50.56, surgeMultiplierVs24hAvg: 1.01 },
      '1d': { volume: 890000.0, volumeUsd: 59736800000, isSurge: false, ratio24hPercent: 100.0, surgeMultiplierVs24hAvg: 1.0 },
    },
    smartMoneyLong: {
      amountUsd: 482500000,
      ratioPercent: 64.2,
      avgPrice: 65420,
      change24h: 8.6,
    },
    smartMoneyShort: {
      amountUsd: 269000000,
      ratioPercent: 35.8,
      avgPrice: 68100,
      change24h: -5.4,
    },
    oi: {
      totalUsd: 14200000000,
      changePercent24h: 6.84,
      longShortRatio: 1.79,
    },
    fundingRate: 0.0125,
    predictedFundingRate: 0.014,
    sparkline24h: [64800, 65100, 64900, 65400, 65800, 66200, 66000, 66450, 66800, 67120],
    trendJudgment: '强烈看涨',
    activeSignalCount: 2,
    candles: {
      '1m': generateCandles(67120, 0.0015, 35),
      '5m': generateCandles(67000, 0.0035, 35),
      '15m': generateCandles(66800, 0.006, 35),
      '30m': generateCandles(66400, 0.009, 35),
      '1h': generateCandles(66000, 0.012, 35),
      '4h': generateCandles(65200, 0.02, 35),
      '8h': generateCandles(64500, 0.025, 35),
      '12h': generateCandles(64000, 0.03, 35),
      '1d': generateCandles(63000, 0.04, 35),
    },
    oiHistory: generateOIPoints(215000, 35),
  },
  {
    id: 'eth',
    symbol: 'ETH',
    originalSymbol: 'ETHUSDT',
    fullName: 'Ethereum',
    exchange: 'Bybit',
    price: 2638.4,
    change24h: 1.85,
    marketCapUsd: 317660000000, // 3176.60亿
    circulatingPercent: 100.0,
    volume24hUsd: 16885760000,
    volume24hAmount: 6400000,
    volumes: createTokenVolumes(16885760000, 6400000, { '1h': true }),
    smartMoneyLong: {
      amountUsd: 184000000,
      ratioPercent: 53.4,
      avgPrice: 2590,
      change24h: 3.2,
    },
    smartMoneyShort: {
      amountUsd: 160500000,
      ratioPercent: 46.6,
      avgPrice: 2685,
      change24h: -1.8,
    },
    oi: {
      totalUsd: 6850000000,
      changePercent24h: 2.15,
      longShortRatio: 1.15,
    },
    fundingRate: 0.0082,
    predictedFundingRate: 0.009,
    sparkline24h: [2580, 2595, 2610, 2605, 2620, 2645, 2630, 2625, 2635, 2638],
    trendJudgment: '高位震荡',
    activeSignalCount: 1,
    candles: {
      '1m': generateCandles(2638, 0.002, 35),
      '5m': generateCandles(2630, 0.004, 35),
      '15m': generateCandles(2620, 0.007, 35),
      '30m': generateCandles(2610, 0.01, 35),
      '1h': generateCandles(2590, 0.014, 35),
      '4h': generateCandles(2560, 0.022, 35),
      '8h': generateCandles(2540, 0.028, 35),
      '12h': generateCandles(2520, 0.032, 35),
      '1d': generateCandles(2500, 0.045, 35),
    },
    oiHistory: generateOIPoints(2580000, 35),
  },
  {
    id: 'sol',
    symbol: 'SOL',
    originalSymbol: 'SOLUSDT',
    fullName: 'Solana',
    exchange: 'Binance',
    price: 155.8,
    change24h: 5.62,
    marketCapUsd: 73226000000, // 732.26亿
    circulatingPercent: 81.5,
    volume24hUsd: 6543600000,
    volume24hAmount: 42000000,
    volumes: createTokenVolumes(6543600000, 42000000, { '1m': true, '5m': true, '15m': true }),
    smartMoneyLong: {
      amountUsd: 96400000,
      ratioPercent: 71.8,
      avgPrice: 148.5,
      change24h: 14.2,
    },
    smartMoneyShort: {
      amountUsd: 37800000,
      ratioPercent: 28.2,
      avgPrice: 158.2,
      change24h: -11.5,
    },
    oi: {
      totalUsd: 2840000000,
      changePercent24h: 9.42,
      longShortRatio: 2.55,
    },
    fundingRate: 0.0215,
    predictedFundingRate: 0.025,
    sparkline24h: [146, 147.5, 149, 148.2, 151, 153.4, 152.8, 154.2, 155.0, 155.8],
    trendJudgment: '强烈看涨',
    activeSignalCount: 2,
    candles: {
      '1m': generateCandles(155.8, 0.003, 35),
      '5m': generateCandles(154.5, 0.006, 35),
      '15m': generateCandles(152.0, 0.01, 35),
      '30m': generateCandles(150.0, 0.015, 35),
      '1h': generateCandles(148.0, 0.02, 35),
      '4h': generateCandles(145.0, 0.03, 35),
      '8h': generateCandles(142.0, 0.035, 35),
      '12h': generateCandles(140.0, 0.04, 35),
      '1d': generateCandles(138.0, 0.05, 35),
    },
    oiHistory: generateOIPoints(18200000, 35),
  },
  {
    id: 'bob',
    symbol: 'BOB', // 去前缀 1000000BOB -> BOB
    originalSymbol: '1000000BOB',
    fullName: 'BOB (1M)',
    exchange: 'Binance',
    price: 0.0392,
    change24h: -3.45,
    marketCapUsd: 39200000, // 3920.00万
    circulatingPercent: 78.4,
    volume24hUsd: 82320000,
    volume24hAmount: 2100000000,
    volumes: createTokenVolumes(82320000, 2100000000),
    smartMoneyLong: {
      amountUsd: 3200000,
      ratioPercent: 41.5,
      avgPrice: 0.0415,
      change24h: -4.8,
    },
    smartMoneyShort: {
      amountUsd: 4510000,
      ratioPercent: 58.5,
      avgPrice: 0.0388,
      change24h: 6.2,
    },
    oi: {
      totalUsd: 34500000,
      changePercent24h: -4.12,
      longShortRatio: 0.71,
    },
    fundingRate: -0.0042,
    predictedFundingRate: -0.005,
    sparkline24h: [0.041, 0.0408, 0.0405, 0.0398, 0.0395, 0.0392, 0.0394, 0.0389, 0.0391, 0.0392],
    trendJudgment: '温和看跌',
    activeSignalCount: 1,
    candles: {
      '1m': generateCandles(0.0392, 0.004, 35),
      '5m': generateCandles(0.0398, 0.008, 35),
      '15m': generateCandles(0.0405, 0.012, 35),
      '30m': generateCandles(0.041, 0.018, 35),
      '1h': generateCandles(0.0415, 0.025, 35),
      '4h': generateCandles(0.042, 0.035, 35),
      '8h': generateCandles(0.043, 0.045, 35),
      '12h': generateCandles(0.044, 0.055, 35),
      '1d': generateCandles(0.045, 0.065, 35),
    },
    oiHistory: generateOIPoints(880000000, 35),
  },
  {
    id: '1inch',
    symbol: '1INCH', // 固有数字保留
    originalSymbol: '1INCH',
    fullName: '1inch Network',
    exchange: 'OKX',
    price: 0.324,
    change24h: 2.12,
    marketCapUsd: 421200000, // 4.21亿
    circulatingPercent: 85.0,
    volume24hUsd: 24300000,
    volume24hAmount: 75000000,
    volumes: createTokenVolumes(24300000, 75000000),
    smartMoneyLong: {
      amountUsd: 8400000,
      ratioPercent: 61.2,
      avgPrice: 0.312,
      change24h: 5.1,
    },
    smartMoneyShort: {
      amountUsd: 5320000,
      ratioPercent: 38.8,
      avgPrice: 0.331,
      change24h: -2.3,
    },
    oi: {
      totalUsd: 42100000,
      changePercent24h: 3.45,
      longShortRatio: 1.58,
    },
    fundingRate: 0.0094,
    predictedFundingRate: 0.011,
    sparkline24h: [0.315, 0.318, 0.317, 0.32, 0.322, 0.325, 0.323, 0.322, 0.323, 0.324],
    trendJudgment: '温和看涨',
    activeSignalCount: 0,
    candles: {
      '1m': generateCandles(0.324, 0.003, 35),
      '5m': generateCandles(0.322, 0.006, 35),
      '15m': generateCandles(0.32, 0.01, 35),
      '30m': generateCandles(0.318, 0.015, 35),
      '1h': generateCandles(0.315, 0.02, 35),
      '4h': generateCandles(0.31, 0.03, 35),
      '8h': generateCandles(0.305, 0.035, 35),
      '12h': generateCandles(0.3, 0.04, 35),
      '1d': generateCandles(0.295, 0.05, 35),
    },
    oiHistory: generateOIPoints(130000000, 35),
  },
  {
    id: '0g',
    symbol: '0G', // 固有数字保留
    originalSymbol: '0G',
    fullName: '0G AI Network',
    exchange: 'Binance',
    price: 1.48,
    change24h: 7.82,
    marketCapUsd: 355200000, // 3.55亿
    circulatingPercent: 24.0,
    volume24hUsd: 68080000,
    volume24hAmount: 46000000,
    volumes: createTokenVolumes(68080000, 46000000, { '1m': true, '5m': true, '1h': true }),
    smartMoneyLong: {
      amountUsd: 12500000,
      ratioPercent: 68.5,
      avgPrice: 1.36,
      change24h: 11.2,
    },
    smartMoneyShort: {
      amountUsd: 5750000,
      ratioPercent: 31.5,
      avgPrice: 1.52,
      change24h: -8.1,
    },
    oi: {
      totalUsd: 68000000,
      changePercent24h: 12.4,
      longShortRatio: 2.17,
    },
    fundingRate: 0.0185,
    predictedFundingRate: 0.022,
    sparkline24h: [1.34, 1.36, 1.39, 1.41, 1.4, 1.44, 1.45, 1.46, 1.47, 1.48],
    trendJudgment: '强烈看涨',
    activeSignalCount: 1,
    candles: {
      '1m': generateCandles(1.48, 0.004, 35),
      '5m': generateCandles(1.45, 0.008, 35),
      '15m': generateCandles(1.42, 0.012, 35),
      '30m': generateCandles(1.4, 0.016, 35),
      '1h': generateCandles(1.37, 0.022, 35),
      '4h': generateCandles(1.33, 0.03, 35),
      '8h': generateCandles(1.3, 0.038, 35),
      '12h': generateCandles(1.27, 0.045, 35),
      '1d': generateCandles(1.22, 0.055, 35),
    },
    oiHistory: generateOIPoints(46000000, 35),
  },
];

export const initialPaperPositions: Position[] = [
  {
    id: 'pos-1',
    symbol: 'BTCUSDT',
    direction: 'long',
    amount: 0.25,
    entryPrice: 65800.0,
    markPrice: 67120.5,
    unrealizedPnl: 330.12,
    unrealizedPnlPercent: 2.01,
    stopLoss: 65200.0,
    takeProfit: 68800.0,
    riskRatioPercent: 18.5,
    leverage: 10,
    openTime: '13:20:45',
  },
  {
    id: 'pos-2',
    symbol: 'SOLUSDT',
    direction: 'long',
    amount: 15.0,
    entryPrice: 151.2,
    markPrice: 155.8,
    unrealizedPnl: 69.0,
    unrealizedPnlPercent: 3.04,
    stopLoss: 149.0,
    takeProfit: 165.0,
    riskRatioPercent: 22.4,
    leverage: 5,
    openTime: '14:02:11',
  },
];
