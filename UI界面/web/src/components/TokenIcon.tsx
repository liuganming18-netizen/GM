/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useEffect, useMemo, useState } from 'react';
import { stripMultiplierPrefix } from '../api/adapters';

interface TokenIconProps {
  /** 展示用代币简称（已去倍率前缀，如 BOB）。 */
  symbol: string;
  /**
   * 交易所原始基础资产代号（含倍率前缀，如 1000000BOB）。
   * 真实 logo 源按「原始名 → 去前缀名」逐个探测；缺省时只用去前缀名。
   */
  base?: string | null;
  size?: 'xs' | 'sm' | 'md' | 'lg';
  className?: string;
}

/**
 * 真实代币 logo 源 —— 公开静态资源，不新增任何依赖、不新增进程/端口。
 *
 * 币安官方 logo CDN 对**非币安 Referer** 返回 403（2026-10-09 实测：
 * 无 Referer 200 / `Referer: http://localhost:3000/` 403 / `Referer: https://www.binance.com/` 200），
 * 故所有 <img> 必须带 `referrerPolicy="no-referrer"`，浏览器不发送 Referer 才可取到图。
 * 另注意该 CDN 对部分不存在的资产返回 **200 + XML `<Error>AccessDenied</Error>`**（如 MOG），
 * 不是 404 —— 浏览器按「非图片」触发 onError，回退链据此自然前进，无需额外判定。
 */
const BINANCE_LOGO = (name: string) => `https://bin.bnbstatic.com/static/assets/logos/${name}.png`;
const COINCAP_LOGO = (name: string) => `https://assets.coincap.io/assets/icons/${name.toLowerCase()}@2x.png`;

/**
 * 候选 URL（按序探测，命中即停）。
 *
 * 覆盖实测（2026-10-09，对活动池 549 个合约逐个按 content-type 校验，非只看状态码）：
 *   币安 CDN（原始名）    385/549
 *   币安 CDN（去前缀名）  390/549
 *   CoinCap（去前缀名）   255/549
 *   三者合并去重          425/549 ≈ 77.4%
 * 其余 124 个多为新上市/低市值标的（TAIKO、ZETA、SONIC、DRIFT、SWARMS…）
 * 与中文名合约（哈基米、币安人生…），按纪律保留字母占位，**不用近似图顶替**。
 */
function buildCandidates(baseName: string): string[] {
  const raw = baseName.toUpperCase();
  const stripped = stripMultiplierPrefix(raw);
  const urls = [BINANCE_LOGO(raw)];
  if (stripped !== raw) urls.push(BINANCE_LOGO(stripped));
  urls.push(COINCAP_LOGO(stripped));
  return urls;
}

/**
 * 已解析结果缓存（模块级，会话内有效）：
 *   string ⇒ 该标的可用的 logo URL；null ⇒ 全部候选失败，直接用内置占位。
 * 虚拟列表滚动会反复挂载/卸载同一行，缓存避免重复探测与重复请求。
 */
const resolved = new Map<string, string | null>();

function cachedAttempt(key: string, candidates: string[]): number {
  if (!resolved.has(key)) return 0;
  const hit = resolved.get(key);
  if (hit == null) return candidates.length;
  const index = candidates.indexOf(hit);
  return index >= 0 ? index : 0;
}

export const TokenIcon: React.FC<TokenIconProps> = ({
  symbol,
  base,
  size = 'sm',
  className = '',
}) => {
  const cleanSymbol = stripMultiplierPrefix(symbol.toUpperCase().replace(/USDT$/, ''));
  const candidates = useMemo(() => buildCandidates(base || cleanSymbol), [base, cleanSymbol]);
  const cacheKey = useMemo(() => candidates.join('|'), [candidates]);

  /** 当前正在探测的候选下标；等于 candidates.length ⇒ 全部失败。 */
  const [attempt, setAttempt] = useState(() => cachedAttempt(cacheKey, candidates));
  /** 已成功加载的图源；非空时隐藏内置兜底层（避免透明 PNG 透出占位字）。 */
  const [loadedSrc, setLoadedSrc] = useState<string | null>(null);

  // 标的身份变化时重置探测状态（命中缓存则同步到已解析项）。
  useEffect(() => {
    setAttempt(cachedAttempt(cacheKey, candidates));
    setLoadedSrc(null);
  }, [cacheKey, candidates]);

  const currentSrc = attempt < candidates.length ? candidates[attempt] : null;

  const handleError = () => {
    setAttempt((prev) => {
      const next = prev + 1;
      if (next >= candidates.length) resolved.set(cacheKey, null);
      return next;
    });
  };

  const handleLoad = () => {
    if (!currentSrc) return;
    resolved.set(cacheKey, currentSrc);
    setLoadedSrc(currentSrc);
  };

  const sizeClasses = {
    xs: 'w-5 h-5 text-[10px]',
    sm: 'w-7 h-7 text-xs',
    md: 'w-9 h-9 text-sm',
    lg: 'w-11 h-11 text-base',
  };

  /**
   * 内置兜底：无网络 / 未覆盖 / 图源失败时使用。
   * 主流币为手绘 SVG（离线可用），其余为字母占位 —— 不编造、不近似。
   */
  const renderFallback = () => {
    switch (cleanSymbol) {
      case 'BTC':
        return (
          <div className="w-full h-full rounded-full bg-[#F7931A] flex items-center justify-center shadow-sm">
            <svg viewBox="0 0 32 32" className="w-[62%] h-[62%] fill-white" xmlns="http://www.w3.org/2000/svg">
              <path d="M23.189 14.02c.314-2.096-1.283-3.223-3.465-3.975l.708-2.84-1.728-.43-.69 2.765c-.454-.114-.92-.22-1.385-.326l.695-2.783L15.596 6l-.708 2.839c-.376-.086-.746-.17-1.104-.26l.002-.009-2.384-.595-.46 1.846s1.283.294 1.256.312c.7.175.826.638.805 1.006l-.806 3.235c.048.012.11.03.18.057l-.183-.045-1.13 4.532c-.086.212-.303.53-.792.409.017.024-1.256-.314-1.256-.314L8 20.316l2.25.561c.418.105.828.215 1.231.318l-.715 2.872 1.727.43.708-2.84c.472.127.93.245 1.378.357l-.705 2.828 1.728.43.715-2.866c2.948.558 5.164.333 6.097-2.333.752-2.146-.037-3.385-1.588-4.192 1.13-.26 1.98-1.003 2.208-2.535zm-3.95 5.545c-.535 2.146-4.152.986-5.325.694l.95-3.81c1.172.293 4.929.872 4.375 3.116zm.536-5.578c-.488 1.954-3.502.962-4.48.718l.86-3.45c.978.243 4.13.7 3.62 2.732z" />
            </svg>
          </div>
        );

      case 'ETH':
        return (
          <div className="w-full h-full rounded-full bg-[#627EEA] flex items-center justify-center shadow-sm">
            <svg viewBox="0 0 32 32" className="w-[60%] h-[60%] fill-white" xmlns="http://www.w3.org/2000/svg">
              <path opacity="0.6" d="M16 4v8.87l7.49 3.35L16 4z" />
              <path d="M16 4L8.51 16.22l7.49-3.35V4z" />
              <path opacity="0.6" d="M16 21.97v6.03l7.5-10.37L16 21.97z" />
              <path d="M16 28V21.97l-7.49-4.32L16 28z" />
              <path opacity="0.2" d="M16 20.57l7.49-4.35L16 12.87v7.7z" />
              <path opacity="0.6" d="M8.51 16.22l7.49 4.35v-7.7l-7.49 3.35z" />
            </svg>
          </div>
        );

      case 'SOL':
        return (
          <div className="w-full h-full rounded-full bg-gradient-to-tr from-[#00FFA3] via-[#03E1FF] to-[#DC1FFF] p-[1.5px] shadow-sm flex items-center justify-center">
            <div className="w-full h-full rounded-full bg-neutral-950 flex items-center justify-center p-1">
              <svg viewBox="0 0 394 316" className="w-[85%] h-[85%]" xmlns="http://www.w3.org/2000/svg">
                <path
                  d="M65.1 237.9c2.4-2.4 5.7-3.8 9.2-3.8h313.4c5.7 0 8.6 6.9 4.6 10.9l-58 58c-2.4 2.4-5.7 3.8-9.2 3.8H11.7c-5.7 0-8.6-6.9-4.6-10.9l58-58z"
                  fill="url(#sol-grad-1)"
                />
                <path
                  d="M65.1 8.9C67.5 6.5 70.8 5.1 74.3 5.1h313.4c5.7 0 8.6 6.9 4.6 10.9l-58 58c-2.4 2.4-5.7 3.8-9.2 3.8H11.7c-5.7 0-8.6-6.9-4.6-10.9l58-58z"
                  fill="url(#sol-grad-2)"
                />
                <path
                  d="M328.9 123.4c-2.4-2.4-5.7-3.8-9.2-3.8H6.3c-5.7 0-8.6 6.9-4.6 10.9l58 58c2.4 2.4 5.7 3.8 9.2 3.8h313.4c5.7 0 8.6-6.9 4.6-10.9l-58-58z"
                  fill="url(#sol-grad-3)"
                />
                <defs>
                  <linearGradient id="sol-grad-1" x1="394" y1="316" x2="6.3" y2="234.1" gradientUnits="userSpaceOnUse">
                    <stop stopColor="#00FFA3" />
                    <stop offset="1" stopColor="#DC1FFF" />
                  </linearGradient>
                  <linearGradient id="sol-grad-2" x1="394" y1="87.9" x2="6.3" y2="5.1" gradientUnits="userSpaceOnUse">
                    <stop stopColor="#00FFA3" />
                    <stop offset="1" stopColor="#DC1FFF" />
                  </linearGradient>
                  <linearGradient id="sol-grad-3" x1="6.3" y1="123.4" x2="394" y2="202.3" gradientUnits="userSpaceOnUse">
                    <stop stopColor="#DC1FFF" />
                    <stop offset="1" stopColor="#00FFA3" />
                  </linearGradient>
                </defs>
              </svg>
            </div>
          </div>
        );

      case 'BOB':
        return (
          <div className="w-full h-full rounded-full bg-gradient-to-br from-[#F59E0B] via-[#D97706] to-[#78350F] flex items-center justify-center shadow-sm p-0.5 border border-amber-400/40">
            {/* BOB Stylized Coin Logo with Builder Hardhat / Mascot Glyph */}
            <svg viewBox="0 0 32 32" className="w-[75%] h-[75%] fill-white" xmlns="http://www.w3.org/2000/svg">
              <circle cx="16" cy="16" r="14" fill="#FBBF24" opacity="0.3" />
              <path d="M16 6c-4.4 0-8 2.7-8 6.5 0 1.2.4 2.4 1.2 3.4L8 23l6-2.5c.6.2 1.3.3 2 .3 4.4 0 8-2.7 8-6.5S20.4 6 16 6zm-3.5 6.5c-.8 0-1.5-.7-1.5-1.5s.7-1.5 1.5-1.5 1.5.7 1.5 1.5-.7 1.5-1.5 1.5zm7 0c-.8 0-1.5-.7-1.5-1.5s.7-1.5 1.5-1.5 1.5.7 1.5 1.5-.7 1.5-1.5 1.5z" />
            </svg>
          </div>
        );

      case '1INCH':
        return (
          <div className="w-full h-full rounded-full bg-[#1B1E2B] flex items-center justify-center shadow-sm border border-neutral-700/80 p-1">
            {/* 1INCH Unicorn Logo */}
            <svg viewBox="0 0 32 32" className="w-[80%] h-[80%]" xmlns="http://www.w3.org/2000/svg">
              <path
                d="M23.5 7.5L16 2.5 13 8l3 3.5-5.5 1.5L9 20.5l5 2 2.5 7 5.5-5 5-2-3.5-15z"
                fill="#DC2626"
                opacity="0.85"
              />
              <path
                d="M17 5l-2.5 4.5 4 4.5-6.5 1.5L10 21l6 2.5 3 6 4-4.5 3-2.5-3-17.5z"
                fill="#FFFFFF"
              />
            </svg>
          </div>
        );

      case '0G':
        return (
          <div className="w-full h-full rounded-full bg-gradient-to-br from-[#4F46E5] via-[#7C3AED] to-[#06B6D4] flex items-center justify-center shadow-sm p-0.5 border border-indigo-400/40">
            {/* 0G AI Network Zero-G Orbit Glyph */}
            <svg viewBox="0 0 32 32" className="w-[75%] h-[75%] fill-white" xmlns="http://www.w3.org/2000/svg">
              <circle cx="16" cy="16" r="10" stroke="white" strokeWidth="2.5" fill="none" opacity="0.9" />
              <path d="M16 6a10 10 0 0 1 10 10" stroke="#38BDF8" strokeWidth="3" fill="none" strokeLinecap="round" />
              <circle cx="16" cy="16" r="3.5" fill="white" />
            </svg>
          </div>
        );

      default:
        return (
          <div className="w-full h-full rounded-full bg-gradient-to-br from-neutral-700 to-neutral-900 border border-neutral-600 flex items-center justify-center font-bold text-neutral-200">
            {cleanSymbol.substring(0, 3)}
          </div>
        );
    }
  };

  return (
    <div
      className={`relative rounded-full shrink-0 overflow-hidden select-none transition-transform hover:scale-105 ${sizeClasses[size]} ${className}`}
    >
      {/* 内置兜底：探测中作为底板，成功加载真实 logo 后隐藏（透明 PNG 才不会透出占位字）。 */}
      {!loadedSrc && (
        <div className="absolute inset-0 flex items-center justify-center">{renderFallback()}</div>
      )}

      {currentSrc && (
        <img
          src={currentSrc}
          alt=""
          aria-hidden="true"
          loading="lazy"
          decoding="async"
          referrerPolicy="no-referrer"
          onError={handleError}
          onLoad={handleLoad}
          className="absolute inset-0 h-full w-full object-cover"
        />
      )}
    </div>
  );
};
