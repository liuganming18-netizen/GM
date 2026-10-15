/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * 结算周期推断（纯函数，无副作用、无网络）。
 *
 * ## 为什么不能直接用行内两个时间戳相减
 *
 * `/markets` 单条只有两个与结算有关的时间：
 *   - `funding.latest_funding_time`：**最近一次真实结算事件**的时间
 *   - `next_funding_time`：交易所给出的下一次结算时钟
 *
 * 实测（2026-10-06 全池 549 条 + 抽样 200 条费率历史）：
 *   - 两法在 **200/200** 抽样合约上互相矛盾；
 *   - `binance/BTCUSDT`：`latest_funding_time = 2026-10-05T16:00:00Z`、
 *     `next_funding_time = 2026-10-06T08:00:00Z` ⇒ 差 16h；而费率历史相邻间隔为**纯 8h**。
 *
 * 原因：两者来自**不同的历史更新时间**——`next_funding_time` 是按 8h 整点对齐的下一次时钟，
 * `latest_funding_time` 是上一次实际结算。因此这个差值**不是当前结算周期**，
 * 绝不能直接当成 8h 口径的依据。
 *
 * ## 因此只用费率历史
 *
 * 只取费率历史**最近若干次**相邻 `event_time` 的间隔；只有这些间隔**完全一致**
 * 且为**整小时**，才认为该合约的当前结算周期可信。窗口内出现任何不一致
 * （例如交易所调整过结算频率）即判定为「无法确定」，保持空态。
 */

/** 判定所需的**最近**间隔数（取最近 N+1 个点） */
export const FUNDING_RECENT_GAPS = 6;
/** 判定所需的最少间隔数（历史点不足则不判定） */
export const FUNDING_MIN_GAPS = 3;

const HOUR_MS = 3600 * 1000;
/** 间隔一致性容差：1 分钟 */
const TOLERANCE_HOURS = 60 * 1000 / HOUR_MS;

export interface FundingPeriodVerdict {
  /** 可信的当前结算周期（小时）；无法确定时为 null */
  periodHours: number | null;
  /** 实际参与判定的间隔数 */
  gapCount: number;
  /** 去重后的历史点数 */
  sampleCount: number;
  /** 判定用的窗口是否稳定（所有间隔一致且为整小时） */
  stable: boolean;
}

const EMPTY: FundingPeriodVerdict = { periodHours: null, gapCount: 0, sampleCount: 0, stable: false };

function toMs(v: string | number | null | undefined): number | null {
  if (v === null || v === undefined) return null;
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  const t = Date.parse(v);
  return Number.isNaN(t) ? null : t;
}

/**
 * 从费率历史推断当前结算周期。
 *
 * @param rows `/markets/{exchange}/{market_id}/funding` 的 `items[]`（只需 `event_time`）
 */
export function inferFundingPeriod(
  rows: Array<{ event_time?: string | number | null }> | null | undefined,
): FundingPeriodVerdict {
  const all = (rows ?? [])
    .map((r) => toMs(r?.event_time))
    .filter((t): t is number => t !== null)
    .sort((a, b) => a - b);

  const uniq: number[] = [];
  for (const t of all) {
    if (uniq.length === 0 || t !== uniq[uniq.length - 1]) uniq.push(t);
  }
  if (uniq.length < 2) return { ...EMPTY, sampleCount: uniq.length };

  // 只取最近 FUNDING_RECENT_GAPS 个间隔 ⇒ 反映「当前」周期，而非历史上任意时刻的周期
  const tail = uniq.slice(-(FUNDING_RECENT_GAPS + 1));
  const gaps: number[] = [];
  for (let i = 1; i < tail.length; i += 1) gaps.push(tail[i] - tail[i - 1]);
  if (gaps.length < FUNDING_MIN_GAPS) {
    return { periodHours: null, gapCount: gaps.length, sampleCount: uniq.length, stable: false };
  }

  const hours = gaps.map((g) => g / HOUR_MS);
  const first = hours[0];
  const stable = hours.every((h) => Math.abs(h - first) < TOLERANCE_HOURS);
  if (!stable) return { periodHours: null, gapCount: gaps.length, sampleCount: uniq.length, stable: false };
  if (!Number.isInteger(first) || first <= 0) {
    return { periodHours: null, gapCount: gaps.length, sampleCount: uniq.length, stable: false };
  }
  return { periodHours: first, gapCount: gaps.length, sampleCount: uniq.length, stable: true };
}
