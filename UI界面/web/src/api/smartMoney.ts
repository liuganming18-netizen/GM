/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * 聪明钱取数客户端（**只读**，纯 GET）。
 *
 * 真实来源（2026-10-07 实测，页面实际请求捕获 + curl 双重验证，均 **无鉴权 200**）：
 *   GET https://www.binance.bh/bapi/futures/v1/public/future/smart-money/signal/overview?symbol={S}&timeRange=ALL
 *   GET https://www.binance.bh/bapi/futures/v1/public/future/smart-money/signal/details/stats?symbol={S}&timeRange=ALL
 *   GET https://www.binance.bh/bapi/futures/v1/public/future/smart-money/signal/details/list?symbol={S}&timeRange=ALL&page=1&rows=10&sortingField=TIME&sortingOrder=DESC
 * 路径含 `/public/`，请求不带 cookie / Authorization 即 200；`timeRange` 为**必填**（缺则 400 illegal parameter）。
 *
 * 两条取数路径，按优先级：
 *  ① **后端代理（durable，生产走这条）**
 *     GET {API_PREFIX}/smart-money/{exchange}/{market_id}
 *     后端把币安原始响应原样透传；未实现时返回 404 ⇒ 自动降级到 ②。
 *  ② **开发期同源直连（verification）**
 *     GET /bapi/futures/v1/public/future/smart-money/signal/*
 *     由 vite dev proxy 转发到 https://www.binance.bh，因此不受浏览器 CORS 限制。
 *     仅对 `exchange === 'binance'` 的行可用；生产构建下该路径不存在 ⇒ 自动降级为空态。
 *
 * 纪律：本模块**只做取数与字段映射**，一律经 `mapBinanceSmartMoney`，
 * 不重算、不用 OI/VWAP/人数比推算、缺值保留 null。
 */

import { API_PREFIX } from './client';
import {
  mapBinanceSmartMoney,
  type BinanceSmartMoneyOverview,
  type BinanceSmartMoneyStats,
  type SmartMoneyMapped,
} from './smartMoneyMapping';

/** 币安公开 bapi 主机（仅用于文档与证据标注；实际请求走同源代理）。 */
export const SMART_MONEY_BAPI_HOST = 'https://www.binance.bh';
/** 同源前缀：由 vite dev proxy `/bapi` 转发到 `SMART_MONEY_BAPI_HOST`。 */
export const SMART_MONEY_BAPI_PREFIX = '/bapi/futures/v1/public/future/smart-money/signal';
/** 页面 URL 使用的口径（`?timeRange=ALL`），overview 与 stats 必须同口径才自洽。 */
export const SMART_MONEY_TIME_RANGE = 'ALL';

interface BapiEnvelope<T> {
  code?: string | null;
  message?: string | null;
  data?: T | null;
  success?: boolean | null;
}

/** 后端代理 `GET /api/v1/smart-money/{exchange}/{market_id}` 的响应体。 */
export interface BackendSmartMoneyPayload {
  available?: boolean | null;
  reason?: string | null;
  source?: string | null;
  symbol?: string | null;
  /** 币安 `…/overview` 原始 `data`（原样透传，前端不重算） */
  overview?: BinanceSmartMoneyOverview | null;
  /** 币安 `…/details/stats` 原始 `data`（原样透传） */
  stats?: BinanceSmartMoneyStats | null;
  update_time_ms?: number | null;
  fetched_at?: string | null;
}

export interface SmartMoneyFetchResult {
  mapped: SmartMoneyMapped;
  /** 实际命中的取数路径 */
  via: 'backend' | 'bapi';
  symbol: string;
  fetchedAt: string;
}

async function getJson<T>(url: string, signal?: AbortSignal): Promise<T | null> {
  try {
    const res = await fetch(url, {
      method: 'GET',
      signal,
      headers: { Accept: 'application/json' },
      cache: 'no-store',
      // 币安该组端点为公开接口：显式不携带凭据，避免误用任何已登录会话。
      credentials: 'omit',
    });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

/** 币安 bapi 单请求：解出 `data`（`data:null` 表示该合约无聪明钱数据，不是错误）。 */
async function bapiData<T>(path: string, symbol: string, signal?: AbortSignal): Promise<T | null> {
  const url = `${SMART_MONEY_BAPI_PREFIX}${path}?symbol=${encodeURIComponent(symbol)}&timeRange=${SMART_MONEY_TIME_RANGE}`;
  const env = await getJson<BapiEnvelope<T>>(url, signal);
  return env?.data ?? null;
}

/**
 * 取某一合约的聪明钱。
 * @returns 命中数据时返回映射结果；两条路径都没拿到 ⇒ `null`（调用方保持空态，不补值）。
 */
export async function fetchSmartMoney(
  exchange: string,
  marketId: string,
  signal?: AbortSignal,
): Promise<SmartMoneyFetchResult | null> {
  // 币安该组接口只覆盖币安 USD-M 合约；OKX 行没有对应来源，直接返回 null（不猜、不替代）。
  if (exchange !== 'binance') return null;
  const symbol = marketId;

  // ① 后端代理（durable）
  const be = await getJson<BackendSmartMoneyPayload>(
    `${API_PREFIX}/smart-money/${encodeURIComponent(exchange)}/${encodeURIComponent(marketId)}`,
    signal,
  );
  if (be && (be.overview || be.stats)) {
    return {
      mapped: mapBinanceSmartMoney(be.overview ?? null, be.stats ?? null),
      via: 'backend',
      symbol,
      fetchedAt: new Date().toISOString(),
    };
  }

  // ② 开发期同源直连（verification）
  const [overview, stats] = await Promise.all([
    bapiData<BinanceSmartMoneyOverview>('/overview', symbol, signal),
    bapiData<BinanceSmartMoneyStats>('/details/stats', symbol, signal),
  ]);
  if (!overview && !stats) return null;
  return {
    mapped: mapBinanceSmartMoney(overview, stats),
    via: 'bapi',
    symbol,
    fetchedAt: new Date().toISOString(),
  };
}
