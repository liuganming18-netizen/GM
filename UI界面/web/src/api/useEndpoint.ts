/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * 只读 GET 轮询 Hook：数据中心 / 策略管理 / 回测复盘三个大类页共用。
 *
 * 纪律：接口不可用或字段缺失时保持 null（原版空态），绝不用旧值或默认值顶替；
 * 失败时保留上一次成功的数据（只读展示，不闪空），但不会伪造任何新值。
 */

import { useEffect, useRef, useState } from 'react';
import { apiGet } from './client';

export interface EndpointState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** 最近一次成功取数时间（毫秒），用于展示“最后同步” */
  fetchedAt: number | null;
  /** 手动触发立即刷新数据 */
  refresh: () => Promise<void>;
}

export function useEndpoint<T>(path: string, refreshMs: number): EndpointState<T> {
  const [state, setState] = useState<Omit<EndpointState<T>, 'refresh'>>({
    data: null,
    error: null,
    loading: true,
    fetchedAt: null,
  });
  const alive = useRef(true);
  const ctrlRef = useRef<AbortController | null>(null);

  const pull = async () => {
    ctrlRef.current?.abort();
    const ctrl = new AbortController();
    ctrlRef.current = ctrl;
    try {
      const d = await apiGet<T>(path, ctrl.signal);
      if (alive.current) setState({ data: d, error: null, loading: false, fetchedAt: Date.now() });
    } catch (e) {
      if ((e as Error)?.name === 'AbortError') return;
      if (alive.current) {
        setState((s) => ({
          data: s.data,
          error: (e as Error)?.message ?? '取数失败',
          loading: false,
          fetchedAt: s.fetchedAt,
        }));
      }
    }
  };

  useEffect(() => {
    alive.current = true;
    void pull();
    const timer = setInterval(() => {
      void pull();
    }, refreshMs);
    return () => {
      alive.current = false;
      clearInterval(timer);
      ctrlRef.current?.abort();
    };
  }, [path, refreshMs]);

  return {
    ...state,
    refresh: pull,
  };
}
