/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * 格式化数字为中文金融单位：万 (10^4) 与 亿 (10^8)
 * @param num 要格式化的数值
 * @param prefix 前缀符号，默认为 '$'，如果不带货币符号可传 ''
 * @param decimals 保留小数位数，默认 2 位
 */
export function formatWanYi(num: number | null | undefined, prefix: string = '$', decimals: number = 2): string {
  if (num === null || num === undefined || isNaN(num)) return '—';
  const abs = Math.abs(num);
  const sign = num < 0 ? '-' : '';

  if (abs >= 1000000000000) {
    // 1万亿及以上 (10^12)
    const wyVal = abs / 1000000000000;
    return `${sign}${prefix}${wyVal.toFixed(decimals)}万亿`;
  } else if (abs >= 100000000) {
    // 1亿及以上 (100,000,000)
    const yiVal = abs / 100000000;
    return `${sign}${prefix}${yiVal.toFixed(decimals)}亿`;
  } else if (abs >= 10000) {
    // 1万及以上 (10,000)
    const wanVal = abs / 10000;
    return `${sign}${prefix}${wanVal.toFixed(decimals)}万`;
  } else {
    return `${sign}${prefix}${abs.toFixed(decimals)}`;
  }
}

/**
 * 格式化币种数量为 万 / 亿
 */
export function formatAmountWanYi(num: number | null | undefined, decimals: number = 2): string {
  return formatWanYi(num, '', decimals);
}

/**
 * OI 数量单位只有在来源给出可辨识的逐合约单位时才直接展示。
 * 历史通用值“张/标的币”“张(oiCcy 另存)”未提供具体合约倍率，不能作为已核验单位。
 */
export function formatOiQuantityUnit(unit: string | null | undefined): string {
  const compact = (unit ?? '').replace(/\\s/g, '').toLowerCase();
  if (!compact || compact === '张/标的币' || compact === '张(oiccy另存)') return '单位待核验';
  return unit?.trim() || '单位待核验';
}

/**
 * OI「总持仓」主显示：**只放真实金额**，金额 ÷ 10000 → 「万U」。
 *
 * 产品口径（2026-10-07）：如 `11104100` → `1,110.41万U`。
 * 金额来源与币种口径（均已核实，不换算、不改口径）：
 *   - 币安 `/futures/data/openInterestHist.sumOpenInterestValue`：
 *     合约**计价币（USDT）名义额**。核实依据：该合约 `quoteAsset=USDT`，
 *     且 5/5 抽样 `sumOpenInterestValue / sumOpenInterest` 与合约最新价一致（偏差 ≤0.66%）。
 *   - OKX `/api/v5/public/open-interest.oiUsd`：官方字段定义为 **USD**。
 * 两者均为「U/美元」量级的计价币名义额，本函数只做 ÷10000 与千分位格式化，
 * **不做任何汇率或币种换算**。
 *
 * 缺失（null / 非有限）→ `暂无金额`。**绝不**用数量、推算值或其它口径顶替。
 */
export function formatWanU(num: number | null | undefined, decimals: number = 2): string {
  if (num === null || num === undefined || !Number.isFinite(num)) return '暂无金额';
  const wan = num / 10000;
  return `${wan.toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })}万U`;
}
