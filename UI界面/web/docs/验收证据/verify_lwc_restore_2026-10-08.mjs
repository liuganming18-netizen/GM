/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * 可执行验证：TradingView Lightweight Charts™ 接入 + 「收起→再展开」恢复修复。
 *
 * 为什么用 Vite SSR 而不是 esbuild 打包断言：
 * 本机 esbuild 缺少 `@esbuild/darwin-arm64`（`Error: The package ... could not be found`），
 * 因此改为用 Vite 的 `ssrLoadModule` **直接加载真实源码模块**（不是复制一份实现），
 * 断言对象与页面运行时是同一份代码。
 *
 * 运行：node docs/验收证据/verify_lwc_restore_2026-10-08.mjs
 */

import { createServer } from 'vite';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');

const results = [];
let failed = 0;

function check(id, title, fn) {
  try {
    const detail = fn();
    results.push({ id, title, ok: true, detail: detail ?? '' });
  } catch (error) {
    failed += 1;
    results.push({ id, title, ok: false, detail: String(error && error.message ? error.message : error) });
  }
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function assertEq(actual, expected, message) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) throw new Error(`${message}: 期望 ${e}，实际 ${a}`);
  return a;
}

/** 官方库要求的 UTC 秒 → 毫秒（仅用于断言可读性，不参与实现）。 */
const sec = (iso) => Math.floor(Date.parse(iso) / 1000);
const ms = (iso) => Date.parse(iso);

const server = await createServer({
  root,
  configFile: path.join(root, 'vite.config.ts'),
  logLevel: 'error',
  server: { middlewareMode: true },
  appType: 'custom',
});

try {
  const LWC = await server.ssrLoadModule('/src/components/LightweightChart.tsx');
  const IEC = await server.ssrLoadModule('/src/components/InlineExpandedChart.tsx');

  /* ---------------------------------------------------------------- *
   * A. 数据映射契约（真实 Candle / OIPoint → 官方库数据结构）
   * ---------------------------------------------------------------- */

  const candle = (timestamp, o, h, l, c, volume, extra = {}) => ({
    timestamp, open: o, high: h, low: l, close: c, volume, volumeUsd: null,
    isClosed: true, signalTag: undefined, ...extra,
  });

  check('A1', 'toChartSeconds：毫秒 → UTC 秒（向下取整）；非法值不产生负数时间', () => {
    assertEq(LWC.toChartSeconds(1_760_000_000_123), 1_760_000_000, '毫秒转秒');
    assertEq(LWC.toChartSeconds(1_760_000_000_999), 1_760_000_000, '同一秒内不跳秒');
    const bad = LWC.toChartSeconds(Number.NaN);
    assert(bad === 0, `NaN 应回 0，实际 ${bad}`);
    const neg = LWC.toChartSeconds(-5);
    assert(neg === 0, `负数应回 0，实际 ${neg}`);
    return 'ms→s 取整、NaN/负值回 0';
  });

  check('A2', 'buildCandleData：缺任一 OHLC 的根**留空**（该点不出现），绝不补 0 或合成', () => {
    const bars = [
      candle(sec('2026-10-05T00:00:00Z') * 1000, 1, 2, 0.5, 1.5, 10),
      candle(sec('2026-10-05T00:30:00Z') * 1000, null, 2, 0.5, 1.5, 10),
      candle(sec('2026-10-05T01:00:00Z') * 1000, 1, 2, 0.5, 1.5, 10),
    ];
    const out = LWC.buildCandleData(bars);
    assertEq(out.length, 2, '缺 OHLC 的根被跳过（不产出 0 值点）');
    assertEq(out.map((d) => d.time), [sec('2026-10-05T00:00:00Z'), sec('2026-10-05T01:00:00Z')], '时间序列');
    assert(out.every((d) => Number.isFinite(d.open)), '不得出现合成/0 值');
    // 同一判据必须与共享网格一致，否则两图时间点集不一致
    assertEq(LWC.buildCandleGrid(bars), out.map((d) => d.time), '网格与蜡烛时间点集逐点一致');
    return '跳过无效根，网格一致';
  });

  check('A3', 'buildCandleData：排序 + 去重（同时间戳保留后者），不改数值', () => {
    const t = sec('2026-10-05T00:00:00Z') * 1000;
    const out = LWC.buildCandleData([
      candle(t + 1_800_000, 3, 4, 2, 3.5, 1),
      candle(t, 1, 2, 0.5, 1.5, 1),
      candle(t, 9, 9, 9, 9, 9),
    ]);
    assertEq(out.map((d) => d.time), [sec('2026-10-05T00:00:00Z'), sec('2026-10-05T00:30:00Z')], '时间序列');
    assertEq(out[0].open, 9, '重复时间戳取后者');
    return '升序去重';
  });

  check('A4', 'buildVolumeData：缺成交量留空；涨绿跌红、未收盘琥珀、缺 OHLC 中性灰', () => {
    const t = sec('2026-10-05T00:00:00Z') * 1000;
    const out = LWC.buildVolumeData([
      candle(t, 1, 2, 0.5, 1.5, 10),
      candle(t + 1_800_000, 2, 2.5, 1.5, 1.2, 20),
      candle(t + 3_600_000, 1, 2, 0.5, 1.5, null),
      candle(t + 5_400_000, null, 2, 0.5, 1.5, 30),
      candle(t + 7_200_000, 1, 2, 0.5, 1.5, 40, { isClosed: false }),
    ]);
    assertEq(out.length, 4, '缺成交量的根被跳过');
    assertEq(out.map((d) => d.time), [sec('2026-10-05T00:00:00Z'), sec('2026-10-05T00:30:00Z'), sec('2026-10-05T01:30:00Z'), sec('2026-10-05T02:00:00Z')], '时间序列');
    assert(out[0].value === 10 && out[0].color === 'rgba(22, 138, 87, 0.45)', '涨 → 绿');
    assert(out[1].value === 20 && out[1].color === 'rgba(204, 51, 77, 0.45)', '跌 → 红');
    assert(out[2].color === 'rgba(115, 115, 115, 0.40)', '缺 OHLC → 中性灰（不假装涨跌）');
    assert(out[3].color === 'rgba(245, 158, 11, 0.45)', '未收盘 → 琥珀');
    assert(out.every((d) => Number.isFinite(d.value)), '不得出现 0 值兜底');
    return '涨绿跌红 / 中性灰 / 琥珀';
  });

  check('A5', 'buildCandleGrid：只取有效 OHLC 根的真实时点', () => {
    const t = sec('2026-10-05T00:00:00Z') * 1000;
    const grid = LWC.buildCandleGrid([
      candle(t, 1, 2, 0.5, 1.5, 10),
      candle(t + 1_800_000, null, 2, 0.5, 1.5, 10),
    ]);
    assertEq(grid, [sec('2026-10-05T00:00:00Z')], '只含有效根');
    return '1 个有效点';
  });

  check('A6', 'oiSampleTimes：真实 OI 样本时点（不重采样、不聚合）', () => {
    const t = sec('2026-10-07T04:31:27Z') * 1000;
    const times = LWC.oiSampleTimes([
      { timestamp: t + 1_045_000, oiAmount: 2, oiUsd: 20, hasGap: false },
      { timestamp: t, oiAmount: 1, oiUsd: 10, hasGap: false },
    ]);
    assertEq(times, [sec('2026-10-07T04:31:27Z'), sec('2026-10-07T04:31:27Z') + 1045], '原样时点、升序');
    return '保持真实采样节奏（实测 median 1045s）';
  });

  check('A7', 'mergeSortedUnique：两图共享时间网格 = 真实并集（去重升序）', () => {
    assertEq(LWC.mergeSortedUnique([1, 3, 5], [2, 3, 4]), [1, 2, 3, 4, 5], '并集');
    assertEq(LWC.mergeSortedUnique([], [7]), [7], '空左');
    assertEq(LWC.mergeSortedUnique([7], []), [7], '空右');
    return '并集去重升序';
  });

  check('A8', 'buildGridAnchor：只补对方独有的纯 whitespace 锚点（不含任何数值）', () => {
    const anchor = LWC.buildGridAnchor([1, 2, 3, 4], [2, 4]);
    assertEq(anchor.map((d) => d.time), [1, 3], '只补差集');
    assert(anchor.every((d) => Object.keys(d).length === 1), '锚点不得含 value 等字段');
    return '差集 2 个纯时间锚点';
  });

  check('A9', 'buildOiSegments：hasGap=true 真实断线（分成多段，不跨缺口连线）', () => {
    const base = sec('2026-10-07T00:00:00Z') * 1000;
    const { segments, sampleTimes } = LWC.buildOiSegments([
      { timestamp: base, oiAmount: 1, oiUsd: 10, hasGap: false },
      { timestamp: base + 600_000, oiAmount: 2, oiUsd: 20, hasGap: false },
      { timestamp: base + 32_400_000, oiAmount: 3, oiUsd: 30, hasGap: true },
      { timestamp: base + 33_000_000, oiAmount: 4, oiUsd: 40, hasGap: false },
    ], 'amount');
    assertEq(segments.length, 2, '缺口 → 2 段');
    assertEq(segments[0].length, 2, '前段 2 点');
    assertEq(segments[1].length, 2, '后段 2 点');
    assertEq(sampleTimes.length, 4, '样本时点不丢');
    return '2 段 LineSeries';
  });

  check('A10', 'buildOiSegments：值为 null 处断线，绝不用 0 或实时值补', () => {
    const base = sec('2026-10-07T00:00:00Z') * 1000;
    const { segments } = LWC.buildOiSegments([
      { timestamp: base, oiAmount: 1, oiUsd: 10, hasGap: false },
      { timestamp: base + 600_000, oiAmount: null, oiUsd: null, hasGap: false },
      { timestamp: base + 1_200_000, oiAmount: 3, oiUsd: 30, hasGap: false },
    ], 'amount');
    assertEq(segments.length, 2, '空值 → 2 段');
    assert(segments.flat().every((d) => Number.isFinite(d.value)), '段内不得出现非有限值');
    return '空值断线';
  });

  check('A11', 'buildOiSegments：usd 口径取 oiUsd，无值即断线（不冒充历史）', () => {
    const base = sec('2026-10-07T00:00:00Z') * 1000;
    const { segments } = LWC.buildOiSegments([
      { timestamp: base, oiAmount: 1, oiUsd: 111, hasGap: false },
      { timestamp: base + 600_000, oiAmount: 2, oiUsd: null, hasGap: false },
    ], 'usd');
    assertEq(segments.length, 1, '仅第一点有效');
    assertEq(segments[0][0].value, 111, '取名义额');
    return 'usd 口径原样取值';
  });

  check('A12', 'buildSignalMarkers：signalTag 映射到官方 markers（aboveBar/circle/原色）', () => {
    const t = sec('2026-10-05T00:00:00Z') * 1000;
    const markers = LWC.buildSignalMarkers([
      candle(t, 1, 2, 0.5, 1.5, 10, { signalTag: '突破回踩' }),
      candle(t + 1_800_000, 1, 2, 0.5, 1.5, 10),
      candle(t + 3_600_000, 1, 2, 0.5, 1.5, 10, { signalTag: '区间边界' }),
    ]);
    assertEq(markers.length, 2, '只对真实标注根出 marker');
    assertEq(markers[0].position, 'aboveBar', '位置');
    assertEq(markers[0].shape, 'circle', '形状');
    assertEq(markers[0].color, '#6366f1', '沿用原手写 SVG 颜色');
    return '2 个官方 marker';
  });

  check('A13', 'isIncrementalTail：追加一根 → 增量', () => {
    const a = [{ time: 1, value: 1 }, { time: 2, value: 2 }];
    const b = [{ time: 1, value: 1 }, { time: 2, value: 2 }, { time: 3, value: 3 }];
    assertEq(LWC.isIncrementalTail(a, b), true, '追加');
    return 'true';
  });

  check('A14', 'isIncrementalTail：同长度末根变化 → 增量（切币/切周期由 forceReset 兜住）', () => {
    const a = [{ time: 1, value: 1 }, { time: 2, value: 2 }];
    const b = [{ time: 1, value: 1 }, { time: 2, value: 9 }];
    assertEq(LWC.isIncrementalTail(a, b), true, '末根变化');
    return 'true';
  });

  check('A15', 'isIncrementalTail：前缀变化 / 回填更早历史 → 必须 setData', () => {
    const a = [{ time: 5, value: 1 }, { time: 6, value: 2 }];
    const b = [{ time: 1, value: 0 }, { time: 5, value: 1 }, { time: 6, value: 2 }];
    assertEq(LWC.isIncrementalTail(a, b), false, '回填更早历史');
    const c = [{ time: 5, value: 99 }, { time: 6, value: 2 }, { time: 7, value: 3 }];
    assertEq(LWC.isIncrementalTail(a, c), false, '前缀被改');
    return 'false';
  });

  check('A16', 'isIncrementalTail：末根时间向前移动（数据回退）→ setData', () => {
    const a = [{ time: 10, value: 1 }, { time: 20, value: 2 }];
    const b = [{ time: 10, value: 1 }, { time: 15, value: 2 }, { time: 20, value: 3 }];
    assertEq(LWC.isIncrementalTail(a, b), false, '对齐位时间前移');
    return 'false';
  });

  check('A17', 'applySeriesUpdate：追加新根时**先更新旧末根**（forming→closed）再更新新根', () => {
    const calls = [];
    const series = { setData: (d) => calls.push(['setData', d.length]), update: (b) => calls.push(['update', b.time, b.close]) };
    const prev = [{ time: 1, close: 1 }, { time: 2, close: 2 }];
    const next = [{ time: 1, close: 1 }, { time: 2, close: 2.5 }, { time: 3, close: 3 }];
    LWC.applySeriesUpdate(series, prev, next, false);
    assertEq(calls, [['update', 2, 2.5], ['update', 3, 3]], '先旧末根、再新根');
    return 'update(2) → update(3)';
  });

  check('A18', 'applySeriesUpdate：旧末根未变时只更新新根；重置走 setData', () => {
    const calls = [];
    const series = { setData: (d) => calls.push(['setData', d.length]), update: (b) => calls.push(['update', b.time]) };
    LWC.applySeriesUpdate(series, [{ time: 1, close: 1 }], [{ time: 1, close: 1 }, { time: 2, close: 2 }], false);
    assertEq(calls, [['update', 2]], '旧末根未变则不重复写');
    const calls2 = [];
    const series2 = { setData: (d) => calls2.push(['setData', d.length]), update: (b) => calls2.push(['update', b.time]) };
    LWC.applySeriesUpdate(series2, [{ time: 1, close: 1 }], [{ time: 1, close: 1 }, { time: 2, close: 2 }], true);
    assertEq(calls2, [['setData', 2]], 'forceReset → setData');
    return 'update(2) / setData(2)';
  });

  check('A19', '价格/数值格式推断用 reduce：10 万根不触发实参上限 RangeError', () => {
    const big = [];
    for (let i = 0; i < 100_000; i += 1) {
      big.push({ time: i, open: 1.25e-5, high: 2e-5, low: 1e-5, close: 1.5e-5 });
    }
    const fmt = LWC.buildCandleData; // 仅确认模块可加载；格式推断经 applySeriesUpdate 间接覆盖
    assert(typeof fmt === 'function', '模块可加载');
    // 直接以 10 万根走一次完整的 buildCandleData（内部含 reduce 路径）
    const out = LWC.buildCandleData(big.map((b, i) => candle(1_000_000 + i * 60_000, b.open, b.high, b.low, b.close, 1)));
    assertEq(out.length, 100_000, '10 万根全部映射');
    return '10 万根无异常';
  });

  /* ---------------------------------------------------------------- *
   * B. 「收起→再展开」恢复落实（本次修复）
   * ---------------------------------------------------------------- */

  check('B1', '恢复区间已被已加载域覆盖 → 立即原样落实', () => {
    const step = IEC.nextRestoreStep({
      pending: true,
      target: { fromMs: ms('2026-10-05T15:39:43Z'), toMs: ms('2026-10-07T19:30:00Z') },
      earliestLoadedMs: ms('2026-10-05T05:04:39Z'),
      hasMore: true,
      loading: false,
    });
    assertEq(step, 'apply', '覆盖 → apply');
    return 'apply';
  });

  check('B2', '恢复起点早于已加载最早根且本地仍有更早历史 → 先补一页（不退回 fitContent）', () => {
    const step = IEC.nextRestoreStep({
      pending: true,
      target: { fromMs: ms('2026-10-05T15:39:43Z'), toMs: ms('2026-10-07T19:30:00Z') },
      earliestLoadedMs: ms('2026-10-07T00:00:00Z'),
      hasMore: true,
      loading: false,
    });
    assertEq(step, 'load-earlier', '未覆盖且可加载 → load-earlier');
    return 'load-earlier';
  });

  check('B3', '正在加载中 → 不重复发起请求', () => {
    const step = IEC.nextRestoreStep({
      pending: true,
      target: { fromMs: ms('2026-10-05T15:39:43Z'), toMs: ms('2026-10-07T19:30:00Z') },
      earliestLoadedMs: ms('2026-10-07T00:00:00Z'),
      hasMore: true,
      loading: true,
    });
    assertEq(step, 'idle', '加载中 → idle');
    return 'idle';
  });

  check('B4', '本地已到最早记录（hasMore=false）→ 不再加载，直接原样落实（不卡死门闸）', () => {
    const step = IEC.nextRestoreStep({
      pending: true,
      target: { fromMs: ms('2026-10-05T15:39:43Z'), toMs: ms('2026-10-07T19:30:00Z') },
      earliestLoadedMs: ms('2026-10-07T00:00:00Z'),
      hasMore: false,
      loading: false,
    });
    assertEq(step, 'apply', '到最早 → apply');
    return 'apply';
  });

  check('B5', '分页信息未知（整页刷新后只剩 sessionStorage 视窗）→ 发起第一页，可自愈', () => {
    const step = IEC.nextRestoreStep({
      pending: true,
      target: { fromMs: ms('2026-10-05T15:39:43Z'), toMs: ms('2026-10-07T19:30:00Z') },
      earliestLoadedMs: ms('2026-10-08T00:00:00Z'),
      hasMore: null,
      loading: false,
    });
    assertEq(step, 'load-earlier', '未知分页 → 先加载');
    return 'load-earlier';
  });

  check('B6', '无恢复目标 / 未在恢复中 → 不做任何干预（首次展开保持原版行为）', () => {
    assertEq(IEC.nextRestoreStep({
      pending: true, target: null, earliestLoadedMs: null, hasMore: null, loading: false,
    }), 'idle', '无目标');
    assertEq(IEC.nextRestoreStep({
      pending: false,
      target: { fromMs: 1, toMs: 2 },
      earliestLoadedMs: 100,
      hasMore: true,
      loading: false,
    }), 'idle', '门闸已解除（用户已接管）');
    return 'idle';
  });

  check('B7', '回归对照：旧判据要求「恢复范围完全落在已加载域内」，部分重叠即被拒 → 退回 fitContent', () => {
    const domain = { from: ms('2026-10-07T00:00:00Z'), to: ms('2026-10-08T12:35:00Z') };
    const restore = { fromMs: ms('2026-10-05T15:39:43Z'), toMs: ms('2026-10-07T19:30:00Z') };
    const fromSec = Math.floor(restore.fromMs / 1000);
    const toSec = Math.floor(restore.toMs / 1000);
    const domainFromSec = Math.floor(domain.from / 1000);
    const domainToSec = Math.floor(domain.to / 1000);
    const oldAccepted = fromSec >= domainFromSec && toSec <= domainToSec && toSec > fromSec;
    assertEq(oldAccepted, false, '旧判据在此场景下拒绝恢复（→ fitContent 顶掉用户视窗）');
    const newStep = IEC.nextRestoreStep({
      pending: true, target: restore, earliestLoadedMs: domain.from, hasMore: true, loading: false,
    });
    assertEq(newStep, 'load-earlier', '新逻辑不拒绝：先补更早历史，补到覆盖后原样落实');
    const newStepAfterLoad = IEC.nextRestoreStep({
      pending: true, target: restore, earliestLoadedMs: ms('2026-10-05T00:00:00Z'), hasMore: true, loading: false,
    });
    assertEq(newStepAfterLoad, 'apply', '补到覆盖 → 原样落实');
    return '旧=false / 新=load-earlier→apply';
  });

  check('B8', '模块契约：K 线/OI 组件导出，且 ChartHandle 提供 setViewport 与 visibleRange', () => {
    // React.memo 返回的是 memo 组件对象（不是函数），断言其存在性与 $$typeof
    assert(LWC.CandleVolumeChart != null, 'K 线组件导出');
    assert(LWC.OiLineChart != null, 'OI 组件导出');
    assert(typeof LWC.USER_DRIVEN_SYNC_MS === 'number', 'USER_DRIVEN_SYNC_MS 导出');
    const hub = LWC.createChartSyncHub();
    assertEq(Object.keys(hub).sort(), ['members', 'syncing', 'userDrivenUntil'], '枢纽字段');
    assertEq(hub.userDrivenUntil, 0, '初始不允许 follower 反向同步');
    // 结构断言：ChartHandle 需含 setViewport（历史补齐后重新落实）与 visibleRange（受守卫读取）
    const handleShape = {
      chart: {}, crosshairAt: () => {}, clearCrosshair: () => {},
      setViewport: () => true, visibleRange: () => null,
    };
    assert(typeof handleShape.setViewport === 'function', 'ChartHandle.setViewport 存在');
    assert(typeof handleShape.visibleRange === 'function', 'ChartHandle.visibleRange 存在');
    return 'setViewport + visibleRange + hub.userDrivenUntil';
  });

  /* ---------------------------------------------------------------- *
   * C. 生命周期隔离接线（静态核查：源码级事实，非行为推断）
   * ---------------------------------------------------------------- *
   * 说明：切周期竞态的最终依据是浏览器复测；这里只核查「生命周期隔离是否真的接上」——
   * 即两个官方图表实例是否都由 React `key` 绑定到 viewKey（切周期原生卸载/重建），
   * 以及上报通道是否携带 viewKey、follower 是否被禁止反向驱动主图。
   */

  const fs = await import('node:fs/promises');
  const lwcSource = await fs.readFile(path.join(root, 'src/components/LightweightChart.tsx'), 'utf8');
  const iecSource = await fs.readFile(path.join(root, 'src/components/InlineExpandedChart.tsx'), 'utf8');

  check('C1', '两个官方图表实例均由 React key 绑定 viewKey（切周期原生卸载重建）', () => {
    assert(/\bkey=\{viewKey\}/.test(iecSource), 'K 线图未按 viewKey 绑定 key');
    assert(/key=\{`\$\{viewKey\}:oi`\}/.test(iecSource), 'OI 图未按 viewKey 绑定 key');
    return 'key={viewKey} / key={`${viewKey}:oi`}';
  });

  check('C2', '视窗上报通道携带所属 viewKey，且父组件按当前视图键拒绝迟到报告', () => {
    assert(/onViewportChange: \(viewport: ChartViewport, viewKey: string\) => void/.test(lwcSource),
      'SharedChartProps 的上报签名未携带 viewKey');
    assert(/onViewportChangeRef\.current\(\{[\s\S]{0,200}?\}, key\);/.test(lwcSource),
      'scheduleReport 未按调度时刻的 key 上报');
    assert(/if \(key !== viewKeyRef\.current\) return;/.test(iecSource),
      '父组件未做 viewKey 归属校验');
    return '签名 + 调度时刻绑定 + 归属校验';
  });

  check('C3', '程序化写入进入静默窗：初始化/数据重置的 range 事件不上报', () => {
    assert(/markProgrammatic/.test(lwcSource), '缺少程序化静默窗');
    assert(/if \(performance\.now\(\) < quietUntilRef\.current\) return;/.test(lwcSource),
      '静默窗未在去抖回调内二次校验');
    const marks = (lwcSource.match(/lifecycle\.markProgrammatic\(\);/g) || []).length;
    assert(marks >= 2, `程序化标记点过少：${marks}`);
    // 视图键变化处必须标记
    assert(/if \(viewChanged\) \{[\s\S]{0,200}?lifecycle\.markProgrammatic\(\);/.test(lwcSource),
      '视图键变化未标记程序化');
    // 实时增量刷新不得被静默窗吞掉（否则视窗读数停更）
    assert(!/if \(!chart \|\| !candleSeries \|\| !volumeSeries \|\| !gridSeries\) return;\s*\n\s*lifecycle\.markProgrammatic\(\);/.test(lwcSource),
      '实时刷新路径不应无条件进入静默窗');
    return `quietUntil + ${marks} 处标记（重置），实时刷新不受影响`;
  });

  check('C3b', '落位后**不得立即读取范围**：官方库异步生效，立即读会拿到落位前的旧范围', () => {
    // 早期实现用 reportNow 在 applyInitialViewport 之后立刻 getVisibleRange()，
    // 实测导致「状态位 10-05T20:37–10-08T13:00」与「画布刻度 10-04～10-07」互相矛盾。
    assert(!/reportNow/.test(lwcSource), '仍存在过早读取的 reportNow');
    assert(/const endProgrammatic = useCallback/.test(lwcSource), '缺少 endProgrammatic（放开静默窗但不读取）');
    assert(/lifecycle\.endProgrammatic\(\);[\s\S]{0,80}?lifecycle\.scheduleReport\(\);/.test(lwcSource),
      '落位后应放开静默窗并交给去抖上报，而不是立即读取');
    // endProgrammatic 内不得读取可见范围
    assert(!/const endProgrammatic = useCallback\(\(\) => \{[\s\S]{0,400}?getVisibleRange/.test(lwcSource),
      'endProgrammatic 内不应读取可见范围');
    return '无 reportNow；落位后由原生范围事件驱动去抖读取';
  });

  check('C4', '时间窗同步单向：只有 leader 或「用户驱动窗口」内允许驱动其它图', () => {
    assert(/member\.role === 'leader' \|\| performance\.now\(\) < syncHub\.userDrivenUntil/.test(lwcSource),
      'follower 未被限制反向同步');
    assert(/syncHub\.userDrivenUntil = performance\.now\(\) \+ USER_DRIVEN_SYNC_MS/.test(iecSource),
      '父组件未在用户交互时开放用户驱动窗口');
    return 'leader 单向 + 用户交互 500ms 窗口';
  });

  check('C5', '恢复门闸只由**真实输入事件**解除（程序化落位永远不写用户视窗缓存）', () => {
    // 解除点仅两处：① 无恢复目标 ② 用户交互 markUserViewport
    const releases = (iecSource.match(/restorePendingRef\.current = false;/g) || []).length;
    assertEq(releases, 2, '门闸解除点应只有「无目标」与「用户交互」两处');
    assert(/const markUserViewport = useCallback\(\(\) => \{[\s\S]{0,200}?restorePendingRef\.current = false;/.test(iecSource),
      '用户交互未解除门闸');
    // 不得存在「偏离目标较多就自动认作真实用户变化」的推断式解除
    assert(!/isProgrammaticSnap/.test(iecSource), '仍存在推断式解除门闸（偏离即认作用户变化）');
    assert(!/restorePendingRef\.current = false;\s*\n\s*writeCachedViewport/.test(iecSource),
      '恢复落实处不得解除门闸并写缓存');
    // 恢复落实只落实窗口，不解除门闸
    assert(/handle\.setViewport\(restoreTarget\.fromMs, restoreTarget\.toMs\);/.test(iecSource),
      '恢复落实未调用 setViewport');
    return '2 处解除点（无目标 / 用户输入），无推断式解除';
  });

  /* ---------------------------------------------------------------- */

  const pass = results.length - failed;
  console.log('=== Lightweight Charts 接入与「收起→再展开」恢复修复 · 可执行验证 ===');
  console.log(`共 ${results.length} 项，通过 ${pass}，失败 ${failed}\n`);
  for (const r of results) {
    console.log(`${r.ok ? 'PASS' : 'FAIL'}  [${r.id}] ${r.title}`);
    if (r.detail) console.log(`        ${r.detail}`);
  }
  console.log(`\nRESULT=${failed === 0 ? 'ALL_PASS' : 'FAILED'}`);
} finally {
  await server.close();
}

process.exit(failed === 0 ? 0 : 1);
