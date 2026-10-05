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
