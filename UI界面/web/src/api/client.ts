/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * 真实后端只读客户端。
 * 契约：GET-only；同源前缀 /api/v1（开发期由 vite proxy 转发到 127.0.0.1:8787）。
 * 未接通/缺数据时后端返回 missing_reason 与 null，前端一律保持 null，不补默认值。
 */

import type { Envelope } from './types';

export const API_PREFIX = '/api/v1';

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

function describeStatus(status: number): string {
  if (status === 0) return '无法连接后端（/api/v1 不可达）';
  if (status === 404) return '接口不存在（404）';
  if (status === 400) return '请求参数不合法（400）';
  return `后端返回 HTTP ${status}`;
}

export async function apiGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  const url = `${API_PREFIX}${path}`;
  let res: Response;
  try {
    res = await fetch(url, {
      method: 'GET',
      signal,
      headers: { Accept: 'application/json' },
      cache: 'no-store',
    });
  } catch (err) {
    if ((err as Error)?.name === 'AbortError') throw err;
    throw new ApiError(describeStatus(0), 0);
  }

  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    if (!res.ok) throw new ApiError(describeStatus(res.status), res.status);
    throw new ApiError('后端响应不是合法 JSON', res.status);
  }

  if (body && typeof body === 'object' && !Array.isArray(body) && '错误' in (body as Record<string, unknown>)) {
    throw new ApiError(String((body as Record<string, unknown>)['错误']), res.status);
  }
  if (!res.ok) throw new ApiError(describeStatus(res.status), res.status);
  return body as T;
}

/** 展开信封：返回 items（缺失时为 []），并保留 as_of / quality 供页面显示刷新时间。 */
export function unwrapItems<T>(env: Envelope<T> | null | undefined): T[] {
  const items = env?.items;
  return Array.isArray(items) ? items : [];
}

/**
 * **写入口**（2026-10-13 信号与持仓接通）。
 *
 * 本系统唯一允许的写入是**纸面账本**（`POST /api/v1/paper/orders`）——服务端模拟开/平仓。
 * 它不提交任何真实订单、不接触实盘 API Key。浏览器**不得**自行造仓：持仓只能来自
 * 服务端账本回执。其余接口仍严格 GET-only。
 *
 * 与 `apiGet` 同纪律：非 2xx 一律抛 `ApiError`（含后端 `missing_reason` / `quality.note`），
 * 调用方据此显示真实拒绝原因，不伪造成功。
 */
export async function apiPost<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const url = `${API_PREFIX}${path}`;
  let res: Response;
  try {
    res = await fetch(url, {
      method: 'POST',
      signal,
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      cache: 'no-store',
      body: JSON.stringify(body ?? {}),
    });
  } catch (err) {
    if ((err as Error)?.name === 'AbortError') throw err;
    throw new ApiError(describeStatus(0), 0);
  }

  let parsed: unknown = null;
  try {
    parsed = await res.json();
  } catch {
    if (!res.ok) throw new ApiError(describeStatus(res.status), res.status);
    throw new ApiError('后端响应不是合法 JSON', res.status);
  }

  if (parsed && typeof parsed === 'object' && !Array.isArray(parsed) && '错误' in (parsed as Record<string, unknown>)) {
    throw new ApiError(String((parsed as Record<string, unknown>)['错误']), res.status);
  }
  // 409 = 服务端账本**明确拒绝**（原因在 quality.note / items[0].reason），同样按错误抛出，
  // 让调用方显示真实原因，而不是把拒绝当成功。
  if (!res.ok) {
    const note =
      (parsed as { quality?: { note?: string | null } } | null)?.quality?.note ??
      describeStatus(res.status);
    throw new ApiError(note ?? describeStatus(res.status), res.status);
  }
  return parsed as T;
}

/** 后端 market_id 在 URL 中需要编码（OKX 形如 AEON-USDT-SWAP 含短横线，仍显式编码以免歧义）。 */
export function marketPath(exchange: string, marketId: string, tail?: string): string {
  const base = `/markets/${encodeURIComponent(exchange)}/${encodeURIComponent(marketId)}`;
  return tail ? `${base}/${tail}` : base;
}
