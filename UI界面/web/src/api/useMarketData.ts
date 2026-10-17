/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * 真实数据编排：首次全量拉取 → 周期刷新 → 展开时按需加载明细。
 * 只做“取数 + 映射 + 合并”，不含任何推测、补值或算法。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Candle, CryptoSignal, OIPoint, Timeframe, TokenMarketItem } from '../types/crypto';
import type {
  Envelope,
  RawCandlesEnvelope,
  RawFundingRow,
  RawHealthEnvelope,
  RawMarket,
  RawOiRow,
  RawPublicExtras,
  RawSignal,
} from './types';
import { ApiError, apiGet, marketPath, unwrapItems } from './client';
import { fetchSmartMoney } from './smartMoney';
import { inferFundingPeriod } from './fundingPeriod';
import {
  TIMEFRAMES,
  adaptCandles,
  adaptFormingBar,
  adaptMarket,
  adaptOiHistory,
  adaptPublicExtras,
  adaptSignals,
  emptyFormingMap,
  mergeMarketRow,
  oiNet24h,
  bucketOiByHour,
  sparklineFromCandles,
  tokenId,
  volumesFromCandles,
} from './adapters';

/** 刷新周期：与原版页面自述的“每 5 秒轮换”一致。 */
export const REFRESH_MS = 5000;

/**
 * 展开 K 线取数条数。
 *
 * 取值依据**原版实测口径**：原版 TradeFlow 的 K 线组件 `the({symbol,markers,anchorTime})`
 * 首屏调 `Qme(symbol, 1500)`（`fapi/v1/klines?interval=1m&limit=1500`），此后每 60s 用
 * `limit=50` 合并；其图表是 `lightweight-charts` 且**不设任何显式可视范围**，即用库默认
 * `barSpacing = 6px` / `rightOffset = 0`。本项目绘图区实测 1124px（主画布）+ 84px（价格轴），
 * 故原版首屏实际可见根数 ≈ 1124 ÷ 6 ≈ **187 根**，蜡烛正好铺满绘图区。
 *
 * 旧值 60 会让首屏只画 1 小时（1m）且两侧留白，观感即「K 线没有完整导入」。
 * 这里取 180（≈ 原版 187 的整数化），**只决定首屏可见根数**；更早历史仍由
 * `InlineExpandedChart` 的分页历史（`history_only=1`，首屏 1500 根）向左延伸。
 */
const DETAIL_BAR_LIMIT = 180;

/**
 * OI 历史取数上限（点数，不是天数）。
 *
 * 后端 `/oi?period=auto` 会把它当**总点数预算**：
 *   近 6 天按 5m、6–30 天按 1h、30–365 天按 1d、更早按 1d 分桶
 *   （每桶只取桶内时间最晚的一条**真实观测**），
 * 因此能覆盖 BTCUSDT 自 2020-09 起的全部归档，而不是 5m 粒度下的几天。
 *
 * 为什么是 5000（2026-10-11 由 2000 提到 4000；2026-10-17 由 4000 提到 5000）：
 *   · 展开图 5m 首屏 = 本地库存一页 1500 根 + 实时尾段 180 根 ≈ **5.2 天**；
 *   · 近端 5m 档放宽到 6 天后，最外档由 1w 收紧到 1d，总点数上界
 *     ≈ 6×288 + 24×24 + 335 + (2237−365) ≈ **4,511**；
 *   · 因此 4000 会把最久远约 1.2 年（2020-09 → 2022-01）截掉。虽然 1d 周期首屏
 *     只看最近 1500 天，但点一次「加载更早历史」就会露出那段空白 ⇒ 提到 5000 留足余量。
 * 后端把**近端 snapshot 段优先占满预算**（保证可见区间不留洞），
 * 归档金字塔用剩余预算，超限时优先丢最久远的一头。
 */
const OI_HISTORY_LIMIT = 5000;

/**
 * 迷你图（近24h小时图）的有界更新参数。
 *
 * 规则：**只对当前真正渲染在列表视口内的行**取 1h 序列（由 TokenTable 的
 * IntersectionObserver 上报），因此工作集天然被限制在「一屏行数」，绝不对全池 549 行扫明细。
 * - 失败不写「已请求」标记：按 10s 起、逐次翻倍、封顶 5min 的退避重试；
 * - 已成功取到的按 TTL 10min 过期后重取，保证与接口同步；
 * - 轮询节拍 20s（不是列表的 5s），且每轮最多处理 SPARKLINE_MAX_PER_PUMP 行。
 */
const SPARKLINE_CONCURRENCY = 6;
const SPARKLINE_TTL_MS = 10 * 60_000;
const SPARKLINE_RETRY_BASE_MS = 10_000;
const SPARKLINE_RETRY_MAX_MS = 5 * 60_000;
const SPARKLINE_TICK_MS = 20_000;
const SPARKLINE_MAX_PER_PUMP = 40;

/**
 * 展开合约明细的有界刷新周期：只对「当前展开的那一个合约」重取，
 * 不对全池扫明细。30s 一次，量级远低于列表的 5s。
 */
export const DETAIL_REFRESH_MS = 30_000;

/** 明细请求失败后的退避：5s 起，逐次翻倍，封顶 60s。成功即清零。 */
const DETAIL_RETRY_BASE_MS = 5_000;
const DETAIL_RETRY_MAX_MS = 60_000;

/** 实时采集新鲜度阈值：超过该秒数视为不新鲜，状态位不再显示“正常”。 */
const REALTIME_FRESH_SECONDS = 60;

/**
 * 结算周期核对（**有界、低频**，复用现有只读 funding 接口）。
 *
 * 依据：
 *  1. `/markets` 单条**没有任何结算周期字段**（全池响应里唯一的入库时间是
 *     `coverage[].source.imported_at`，与结算周期无关）；
 *  2. 行内 `next_funding_time − funding.latest_funding_time` **不是**当前结算周期——
 *     两者来自不同的历史更新时间（详见 `fundingPeriod.ts`），实测 200/200 抽样合约上
 *     该差值与费率历史真实间隔互相矛盾。
 * 因此只能用费率历史相邻 `event_time` 间隔核对，且必须**有界**：
 *  - 可见行优先（列表上报），保证当前屏幕上的费率位先得到真实口径；
 *  - 其余按池序低速补扫，用于顶栏「平均费率 (8h)」口径收敛；
 *  - 节拍 15s、每轮最多 20 条、并发 3 ⇒ 一次全池约 7 分钟，成功后按 TTL 6h 缓存；
 *  - 失败按 2min 起指数退避，**不写「已核对」标记**；
 *  - 列表全池请求在途时跳过本轮，避免与 4.3MB 的 `/markets` 争抢后端。
 */
const FUNDING_PROBE_TICK_MS = 15_000;
const FUNDING_PROBE_MAX_PER_PUMP = 20;
const FUNDING_PROBE_CONCURRENCY = 3;
const FUNDING_PERIOD_TTL_MS = 6 * 3600_000;
const FUNDING_PROBE_RETRY_BASE_MS = 2 * 60_000;
const FUNDING_PROBE_RETRY_MAX_MS = 30 * 60_000;
/** 费率历史请求条数（后端实际返回其全部可用历史） */
const FUNDING_HISTORY_LIMIT = 60;
/** 「资金费率 (8h)」这一列只输出 8h 口径 */
const FUNDING_TARGET_PERIOD_HOURS = 8;

/**
 * 币安公开扩展字段（`/markets/{ex}/{mid}/public-extras`）的有界补扫。
 *
 * 目的：把**币安官方公开**原始值接入现有字段：
 *   - `oi.longShortRatio` ← `/futures/data/topLongShortPositionRatio`，在展开详情的既有「相关指标」位按“大户持仓量多空比”显示；
 *   - 同一 `/futures/data/openInterestHist` 行的 `sumOpenInterest` / `sumOpenInterestValue`
 *     分别作为全市场总OI数量 / 名义额；不拆分多空，不用于推算聪明钱。
 *
 * 有界本机读取（后端路由**只读快照文件，不请求交易所**）：
 *   - **只对当前视口内可见行**读取本机 API（TokenTable 的 IntersectionObserver 上报），
 *     不对全池 549 行扫描；
 *   - 每轮 ≤6 行、并发 2、节拍 60s；每轮最多 6 次 localhost API GET，**不会增加上游预算**；
 *   - 快照成功后按 60s TTL 重读；切回前台立即重读可见行，不等待下一轮定时器；
 *   - 失败按 60s 起指数退避、封顶 30min，**不写「已取到」标记**；
 *   - 列表全池请求在途时让路（`refreshBusy`）；
 *   - **public-extras 端点不可用（404）时立即熔断并停止本会话探测** —— 原版 `—` 保持，
 *     不产生控制台错误、不反复打不存在的接口。
 */
const PUBLIC_EXTRAS_TICK_MS = 60_000;
const PUBLIC_EXTRAS_MAX_PER_PUMP = 6;
const PUBLIC_EXTRAS_CONCURRENCY = 2;
const PUBLIC_EXTRAS_TTL_MS = 60_000;
const PUBLIC_EXTRAS_RETRY_BASE_MS = 60_000;
const PUBLIC_EXTRAS_RETRY_MAX_MS = 30 * 60_000;
/** 列表页只需要「多空比 + OI 名义额」：3 次上游，不是全量 7 次 */
const PUBLIC_EXTRAS_FIELDS = 'ratio,oi';
const PUBLIC_EXTRAS_PERIOD = '1h';

export interface TopAggregates {
  /**
   * 平均费率 (8h)：**只对结算周期已被核对为 8h 的合约**取 `funding_rate` 均值。
   *
   * 为什么不能直接平均：`/markets` 无结算周期字段，行内 `next_funding_time −
   * funding.latest_funding_time` 来自不同的历史更新时间、**不是**当前结算周期
   * （实测 200/200 抽样合约上与费率历史真实间隔互相矛盾）。因此改为按
   * 「费率历史相邻 `event_time` 间隔」逐条核对（见 `fundingPeriod.ts`），
   * 只有窗口内间隔完全一致且为 8h 的合约才计入；一条都没核对出来时为 null（显示 '—'）。
   */
  avgFundingRate: number | null;
  /** 已核对出结算周期的合约数 / 已核对为 8h 的合约数（仅用于判断口径是否成立） */
  fundingPeriodResolved: number;
  fundingPeriod8h: number;
  /** 全网 OI 名义额：后端无全量 USD 口径，恒为 null */
  networkOiUsd: number | null;
  /** 聪明钱多空比：无真实来源，恒为 null */
  smartMoneyRatio: number | null;
  /** 活跃池规模（真实接口口径） */
  poolTotal: number | null;
  /** 实时价正常的市场数 / 总数 */
  realtimeOk: number;
  realtimeTotal: number;
}

const EMPTY_AGGREGATES: TopAggregates = {
  avgFundingRate: null,
  fundingPeriodResolved: 0,
  fundingPeriod8h: 0,
  networkOiUsd: null,
  smartMoneyRatio: null,
  poolTotal: null,
  realtimeOk: 0,
  realtimeTotal: 0,
};

/**
 * 真实入库 / 同步时间：复用本次全池响应里 `coverage[].source.imported_at`
 * （官方归档文件导入数据库的时刻），**不额外发请求**。
 * 与 `coverage[].latest`（K 线止点 = 交易事件时间）严格区分，不混用。
 */
export interface ImportedAtSnapshot {
  /** 全池最新的入库时间 */
  latest: string | null;
  /** 按 K 线周期聚合的最新入库时间 */
  byTimeframe: Record<string, string>;
}

const EMPTY_IMPORTED_AT: ImportedAtSnapshot = { latest: null, byTimeframe: {} };

/** 固定状态栏位可绑定的真实证据（来自 /health），未知一律 null。 */
export interface HealthSnapshot {
  /** 实时行情采集链路：最新价格事件距今秒数 */
  realtimeAgeSeconds: number | null;
  /** 实时行情采集链路状态（ok / stale / ...） */
  realtimeStatus: string | null;
  /** 账户/交易模块状态（not_connected / ok / ...） */
  accountStatus: string | null;
}

const EMPTY_HEALTH: HealthSnapshot = {
  realtimeAgeSeconds: null,
  realtimeStatus: null,
  accountStatus: null,
};

export const PUBLIC_EXTRAS_SNAPSHOT_STALE_AFTER_S = 900;
/** 保守展示限值：1h 持仓量多空比超过两个指标周期即标记陈旧。 */
export const PUBLIC_EXTRAS_RATIO_STALE_AFTER_S = 2 * 60 * 60;
/** 保守展示限值：5m OI 名义额超过三个数据周期即标记陈旧。 */
export const PUBLIC_EXTRAS_OI_STALE_AFTER_S = 3 * 5 * 60;

export interface PublicExtrasView {
  status: 'pending' | 'fresh' | 'stale' | 'missing' | 'error' | 'unknown';
  ratioAvailable: boolean;
  longShortRatio: number | null;
  oiAvailable: boolean;
  /** 快照原始值保留，陈旧时由页面明确标注，不冒充当前值。 */
  oiAmount?: number | null;
  oiUsd: number | null;
  oiAmountEventTime?: string | null;
  oiUsdEventTime?: string | null;
  oiEventTime: string | null;
  oiUnit?: string | null;
  oiSource: string | null;
  oiSourceUrl: string | null;
  period: string | null;
  eventTime: string | null;
  snapshotTime: string | null;
  snapshotAgeSeconds: number | null;
  source: string | null;
  sourceUrl: string | null;
  qualityNote: string | null;
  missingReason: string | null;
}

export interface MarketData {
  tokens: TokenMarketItem[];
  publicExtras: Record<string, PublicExtrasView>;
  signals: CryptoSignal[];
  loading: boolean;
  refreshing: boolean;
  error: string | null;
  asOf: string | null;
  aggregates: TopAggregates;
  /** /health 真实证据 */
  health: HealthSnapshot;
  /**
   * 展开某行时调用。默认只补齐「尚未成功加载的周期/OI」；
   * `force=true` 时对当前可得周期整体重取（有界刷新用）。
   */
  ensureDetail: (id: string, force?: boolean) => void;
  /** 声明当前展开的合约，用于有界刷新；传 null 表示无展开项。 */
  setActiveDetailId: (id: string | null) => void;
  /**
   * 由列表组件上报「当前视口内可见的行 id」。
   * 迷你图只对可见行做有界更新与失败退避重试；上报内容变化时才触发一轮。
   */
  noteVisibleRows: (ids: string[]) => void;
  /**
   * 逐合约的**已核对结算周期（小时）**快照，只包含已得出结论的合约。
   * `8` ⇒ 可输出「资金费率 (8h)」口径；其他值或缺失 ⇒ 该位空态。
   */
  fundingPeriodHours: Record<string, number | null>;
  /** 复用全池响应得到的真实入库时间（不额外发请求） */
  importedAt: ImportedAtSnapshot;
}

export function useMarketData(): MarketData {
  const [tokens, setTokens] = useState<TokenMarketItem[]>([]);
  const [signals, setSignals] = useState<CryptoSignal[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [asOf, setAsOf] = useState<string | null>(null);
  const [aggregates, setAggregates] = useState<TopAggregates>(EMPTY_AGGREGATES);
  const [health, setHealth] = useState<HealthSnapshot>(EMPTY_HEALTH);
  const [activeDetailId, setActiveDetailId] = useState<string | null>(null);
  const [fundingPeriodHours, setFundingPeriodHours] = useState<Record<string, number | null>>({});
  /** 本机 public-extras 快照值与来源/时点/新鲜度，按合约 id；不生成默认数值 */
  const [publicExtras, setPublicExtras] = useState<Record<string, PublicExtrasView>>({});
  const [importedAt, setImportedAt] = useState<ImportedAtSnapshot>(EMPTY_IMPORTED_AT);

  /** 原始行情行，供按需明细取 exchange / market_id / 可得周期 */
  const rawById = useRef<Map<string, RawMarket>>(new Map());
  const refreshBusy = useRef(false);
  const alive = useRef(true);

  /**
   * 迷你图状态：按合约记录「上次成功时间 / 连续失败次数 / 下次可尝试时间」。
   * 关键点：**不在请求前写「已请求」**，失败必须能重试；成功后才记录 loadedAt。
   */
  const sparkState = useRef<Map<string, { loadedAt: number; failStreak: number; nextAttemptAt: number }>>(
    new Map(),
  );
  /** 当前视口内可见的行（由 TokenTable 上报）；version用于识别请求在途时的切换，不另增Hook。 */
  type VisibleRowSet = Set<string> & { version: number };
  const visibleRowIds = useRef<VisibleRowSet>(Object.assign(new Set<string>(), { version: 0 }));
  const sparkPumping = useRef(false);

  /**
   * 结算周期核对状态：按合约记录「核对结论 / 核对时间 / 连续失败次数 / 下次可尝试时间」。
   * 与迷你图同规则：**失败不写「已核对」**，退避后仍会重试。
   */
  const fundingState = useRef<
    Map<
      string,
      {
        checkedAt: number;
        periodHours: number | null;
        resolved: boolean;
        failStreak: number;
        nextAttemptAt: number;
      }
    >
  >(new Map());
  const fundingPumping = useRef(false);

  /**
   * 币安公开扩展字段的取数状态：按合约记录「上次成功时间 / 连续失败次数 / 下次可尝试时间」。
   * `routeAbsent`：一旦后端返回 404（路由尚未接线）就熔断，本会话不再探测 ——
   * 保持原版 `—`，不刷控制台错误、不反复打不存在的接口。
   */
  const extrasState = useRef<Map<string, { loadedAt: number; failStreak: number; nextAttemptAt: number }>>(
    new Map(),
  );
  const extrasPumping = useRef(false);
  const extrasRouteAbsent = useRef(false);

  /**
   * 明细状态：按合约记录「已成功加载的周期 / OI 是否成功 / 失败退避」。
   * 关键点：失败**不**标记为已加载，退避后仍会重试；成功过的时间用于 TTL 刷新。
   */
  interface DetailState {
    loadedAt: number;
    loadedTfs: Set<Timeframe>;
    oiLoaded: boolean;
    /**
     * 聪明钱是否已成功取到（币安公开接口，按展开合约取，O(1)，不扫全池）。
     * 与 OI 同理：失败**不**置位，退避后仍会重试。
     */
    smartMoneyLoaded: boolean;
    failStreak: number;
    nextAttemptAt: number;
  }
  const detailState = useRef<Map<string, DetailState>>(new Map());
  const detailInflight = useRef<Set<string>>(new Set());

  const stateOf = useCallback((id: string): DetailState => {
    let st = detailState.current.get(id);
    if (!st) {
      st = { loadedAt: 0, loadedTfs: new Set(), oiLoaded: false, smartMoneyLoaded: false, failStreak: 0, nextAttemptAt: 0 };
      detailState.current.set(id, st);
    }
    return st;
  }, []);

  const ensureDetail = useCallback(
    (id: string, force = false) => {
      if (!id) return;
      if (detailInflight.current.has(id)) return;
      const raw = rawById.current.get(id);
      if (!raw) return;

      const st = stateOf(id);
      const now = Date.now();
      if (now < st.nextAttemptAt) return; // 失败退避中

      /**
       * 取数周期 = 原版全部 9 个周期（1m→1d 顺序），**不只**用 `available_timeframes`。
       *
       * 为什么不能只信 `available_timeframes`：实测 `/markets` 里 binance/BTCUSDT 的该字段为
       * `['1d','12h','8h','4h','1h','30m','15m','5m']`（**没有 1m**），但后端同源
       * `/candles?timeframe=1m` 能返回 60 根实时闭合 K 线 + forming_bar。该字段只反映
       * 「归档已入库周期」，不反映「网关可服务周期」，用它做闸门会导致 1m 实时尾段永远不显示。
       * 因此改为对**当前展开的这一个合约**按原版 9 周期逐一取数（≤9 个 GET，30s TTL，
       * detailInflight 去重，绝不对全池扫描）；缺数据的周期由后端返回 `bars: []` +
       * `missing_reason`，前端如实空态，不合成、不借别的周期替代。
       */
      const tfs = TIMEFRAMES;
      if (!force) {
        // 只补「没成功过的」：新入库的可用周期会自动被带进来
        const missingTfs = tfs.filter((tf) => !st.loadedTfs.has(tf));
        if (missingTfs.length === 0 && st.oiLoaded && st.smartMoneyLoaded) return;
      }

      detailInflight.current.add(id);

      const candleJobs = tfs.map((tf) =>
        apiGet<RawCandlesEnvelope>(
          `${marketPath(raw.exchange, raw.market_id, 'candles')}?timeframe=${encodeURIComponent(tf)}&limit=${DETAIL_BAR_LIMIT}&no_rest_tail=1`,
        )
          .then((env) => ({
            tf,
            // 已闭合历史序列：只进 candles
            list: adaptCandles(env, tf),
            // 未收盘临时层：单独取出，绝不并入 candles（不当作历史闭合记录）
            forming: adaptFormingBar(env, tf),
            ok: true as const,
          }))
          .catch(() => ({ tf, list: [] as Candle[], forming: null, ok: false as const })),
      );
      /**
       * OI：显式请求 `period=auto`（后端加性能力，2026-10-11 接入）。
       *
       * 为什么必须显式要 `auto` —— 这就是「OI 总持仓量就几天」的**唯一**原因：
       *   · `/oi` 不带 `period` ⇒ 后端只读 `period='snapshot'`（实时快照），
       *     本机 snapshot 只有 2026-10-05 起约 6 天 ⇒ 图上就「几天」；
       *   · `?period=5m` ⇒ 归档覆盖 2020-09 起约 6 年（`oi_history` 1.271 亿行 / 526 标的），
       *     但旧实现硬编码 `LIMIT 2000`，5m 粒度下只够 6.9 天。
       * `auto` = 5m 归档（近期细、远期粗的自适应金字塔，总点数 ≤ `limit`）
       *        + 该 5m 末点**之后**的 snapshot 近端（两段**零重叠**，每行 `period` 字段自证口径）。
       * 不聚合求均值、不插值、不改时间戳；OKX 无 5m 归档 ⇒ 自动退化为纯 snapshot。
       * 请求失败 ⇒ 保持空态（不造数据、不借别的周期替代）。
       */
      const oiJob = apiGet<Envelope<RawOiRow>>(
        `${marketPath(raw.exchange, raw.market_id, 'oi')}?period=auto&limit=${OI_HISTORY_LIMIT}`,
      )
        .then((env) => ({ rows: unwrapItems(env), ok: true as const }))
        .catch(() => ({ rows: [] as RawOiRow[], ok: false as const }));

      /**
       * 聪明钱：只对**当前展开的这一个合约**取数（币安公开接口，O(1)，不扫全池）。
       * 两条路径见 `src/api/smartMoney.ts`：优先后端代理，未实现则降级到同源 `/bapi`。
       * 拿不到 ⇒ 保持空态（六槽 null），不补值、不用 OI/人数比推算。
       */
      const smartMoneyJob = fetchSmartMoney(raw.exchange, raw.market_id)
        .then((res) => ({ res, ok: !!res }))
        .catch(() => ({ res: null, ok: false as const }));

      Promise.all([Promise.all(candleJobs), oiJob, smartMoneyJob])
        .then(([candleRes, oiRes, smRes]) => {
          if (!alive.current) return;
          const okTfs = candleRes.filter((r) => r.ok).map((r) => r.tf);

          setTokens((prev) =>
            prev.map((t) => {
              if (t.id !== id) return t;
              const candles: Record<Timeframe, Candle[]> = { ...t.candles };
              const formingCandles: Record<Timeframe, Candle | null> = {
                ...emptyFormingMap(),
                ...(t.formingCandles ?? {}),
              };
              // 只有成功的周期才覆盖；失败的保留旧数据，避免一次抖动把已有图清空
              for (const r of candleRes) {
                if (r.ok) {
                  candles[r.tf] = r.list;
                  // 未收盘层同样只在成功时覆盖：接口返回 forming_bar=null 时如实写 null（空态）
                  formingCandles[r.tf] = r.forming;
                }
              }
              const prevOi: OIPoint[] = Array.isArray(t.oiHistory) ? t.oiHistory : [];
              const oiPoints: OIPoint[] = oiRes.ok ? adaptOiHistory(oiRes.rows) : prevOi;
              const net = oiNet24h(bucketOiByHour(oiPoints));
              const next: TokenMarketItem = {
                ...t,
                candles,
                formingCandles,
                oiHistory: oiPoints,
                oi: { ...t.oi, changePercent24h: net ? net.percent : null },
                // 量纲一致：分子为 bar 的 quote_volume（USDT），分母为同为计价币口径的 24h 成交额
                // 口径只用闭合序列（candles），未收盘根不参与
                volumes: volumesFromCandles(candles, t.volumes, t.volume24hUsd),
              };
              // 聪明钱：只在真正取到时覆盖；取不到保留上一轮（可能是列表透传来的）值。
              if (smRes.res) {
                next.smartMoneyLong = {
                  amountUsd: smRes.res.mapped.long.amountUsd,
                  ratioPercent: smRes.res.mapped.long.ratioPercent,
                  avgPrice: smRes.res.mapped.long.avgPrice,
                  change24h: smRes.res.mapped.long.change24h,
                  traders: smRes.res.mapped.long.traders,
                  whaleTraders: smRes.res.mapped.long.whaleTraders,
                  whaleAvgPrice: smRes.res.mapped.long.whaleAvgPrice,
                };
                next.smartMoneyShort = {
                  amountUsd: smRes.res.mapped.short.amountUsd,
                  ratioPercent: smRes.res.mapped.short.ratioPercent,
                  avgPrice: smRes.res.mapped.short.avgPrice,
                  change24h: smRes.res.mapped.short.change24h,
                  traders: smRes.res.mapped.short.traders,
                  whaleTraders: smRes.res.mapped.short.whaleTraders,
                  whaleAvgPrice: smRes.res.mapped.short.whaleAvgPrice,
                };
              }
              const spark = sparklineFromCandles(candles['1h'] ?? []);
              if (spark.length >= 2) next.sparkline24h = spark;
              return next;
            }),
          );

          const st2 = stateOf(id);
          for (const tf of okTfs) st2.loadedTfs.add(tf);
          if (oiRes.ok) st2.oiLoaded = true;
          if (smRes.ok) st2.smartMoneyLoaded = true;

          // 逐资源判定：只要还有任一周期 / OI / 聪明钱没成功，就不算加载完成，按退避重试。
          const failed =
            candleRes.filter((r) => !r.ok).length + (oiRes.ok ? 0 : 1) + (smRes.ok ? 0 : 1);
          if (failed === 0) {
            st2.loadedAt = Date.now();
            st2.failStreak = 0;
            st2.nextAttemptAt = 0;
          } else {
            st2.failStreak += 1;
            st2.nextAttemptAt =
              Date.now() + Math.min(DETAIL_RETRY_BASE_MS * 2 ** (st2.failStreak - 1), DETAIL_RETRY_MAX_MS);
          }
        })
        .finally(() => {
          detailInflight.current.delete(id);
        });
    },
    [stateOf],
  );

  /** 首次加载与周期刷新 */
  useEffect(() => {
    alive.current = true;
    const ctrl = new AbortController();

    async function pull(first: boolean) {
      if (refreshBusy.current) return;
      refreshBusy.current = true;
      if (!first) setRefreshing(true);
      try {
        const marketsEnv = await apiGet<Envelope<RawMarket>>('/markets', ctrl.signal);
        if (!alive.current) return;
        const rows = unwrapItems(marketsEnv);

        const nextRaw = new Map<string, RawMarket>();
        const adapted: TokenMarketItem[] = [];
        for (const r of rows) {
          if (!r || !r.market_id || !r.exchange) continue;
          const id = tokenId(r.exchange, r.market_id);
          nextRaw.set(id, r);
          adapted.push(adaptMarket(r));
        }
        rawById.current = nextRaw;

        setTokens((prev) => {
          if (prev.length === 0) return adapted;
          const prevById = new Map(prev.map((t) => [t.id, t]));
          return adapted.map((n) => {
            const p = prevById.get(n.id);
            return p ? mergeMarketRow(p, n) : n;
          });
        });

        // 顶部总览聚合：只用真实字段
        setAggregates({
          // 结算周期逐条核对后由 useMemo 覆盖（见 avgFundingRate 注释）→ 此处先置 null
          avgFundingRate: null,
          fundingPeriodResolved: 0,
          fundingPeriod8h: 0,
          // 全量 USD OI 后端未提供（仅 23/549 有 open_interest_usd）→ 不给“全网”口径
          networkOiUsd: null,
          smartMoneyRatio: null,
          poolTotal: marketsEnv.total ?? rows.length,
          realtimeOk: rows.filter((r) => r.realtime_status === 'ok').length,
          realtimeTotal: rows.length,
        });

        // 复用本次全池响应里的真实入库时间（coverage[].source.imported_at），不额外发请求。
        // 注意：这是**入库时间**，与 coverage[].latest（K 线止点 = 交易事件时间）不是一回事。
        // ISO-8601 UTC 字符串可直接按字典序比较，无需 Date.parse。
        const byTf: Record<string, string> = {};
        let latestImported: string | null = null;
        for (const r of rows) {
          const cov = r.coverage;
          if (!Array.isArray(cov)) continue;
          for (const c of cov) {
            const imp = c?.source?.imported_at;
            if (typeof imp !== 'string' || imp.length === 0) continue;
            const tf = c.timeframe;
            if (tf && (byTf[tf] === undefined || imp > byTf[tf])) byTf[tf] = imp;
            if (latestImported === null || imp > latestImported) latestImported = imp;
          }
        }
        setImportedAt({ latest: latestImported, byTimeframe: byTf });

        setAsOf(marketsEnv.as_of ?? null);
        setError(null);
      } catch (err) {
        if ((err as Error)?.name === 'AbortError') return;
        if (alive.current) setError((err as Error)?.message ?? '拉取真实行情失败');
      } finally {
        refreshBusy.current = false;
        if (alive.current) {
          setLoading(false);
          setRefreshing(false);
        }
      }
    }

    async function pullHealth() {
      try {
        const env = await apiGet<RawHealthEnvelope>('/health', ctrl.signal);
        if (!alive.current) return;
        const items = unwrapItems(env);
        const rt = items.find((i) => (i.name ?? '').includes('实时行情采集')) ?? null;
        const acct = items.find((i) => (i.name ?? '').includes('账户')) ?? null;
        setHealth({
          realtimeAgeSeconds: rt?.age_seconds ?? null,
          realtimeStatus: rt?.status ?? null,
          accountStatus: acct?.status ?? null,
        });
      } catch {
        // 健康接口不可用时状态位保持未知（显示空态），不沿用旧值冒充实时
        if (alive.current) setHealth(EMPTY_HEALTH);
      }
    }

    async function pullSignals() {
      try {
        /**
         * **当前交易信号区**（2026-10-14 信号区清理专项）：
         * 显式请求 `scope=current` —— 后端只返回「实时真实臂入场 + 起点之后」的信号，
         * 且过滤发生在**分页 LIMIT 与 total/has_more 之前**（不会「先取 500 再前端裁掉」
         * 而漏掉后面的合法新信号）。
         * 历史研究 / 启动前仅展示 / 结构提示 / 失效过期等记录**不再进入本入口**；
         * 审计读取走 `scope=all`（不在本数据层请求，避免重新混入交易信号区）。
         */
        const env = await apiGet<Envelope<RawSignal>>('/signals?scope=current&limit=500', ctrl.signal);
        if (!alive.current) return;
        const map = new Map<string, string>();
        rawById.current.forEach((r, id) => {
          if (r.base) map.set(id, r.base);
        });
        setSignals(adaptSignals(env, map));
      } catch {
        // 信号接口不可用时保持空列表（原版空态），不补造信号
        if (alive.current) setSignals([]);
      }
    }

    pull(true);
    pullSignals();
    pullHealth();
    const timer = setInterval(() => {
      pull(false);
      pullSignals();
      pullHealth();
    }, REFRESH_MS);

    return () => {
      alive.current = false;
      clearInterval(timer);
      ctrl.abort();
    };
  }, []);

  /**
   * 有界明细刷新：只针对「当前展开的那一个合约」，全池明细不做任何扫描。
   * - 5s tick：只补「尚未成功加载」的周期 / OI（含失败重试，内部有 5s→60s 退避，空跑几乎无成本）；
   *   采集新入库的可用周期会在下一 tick 被自动带上，不需要等 TTL。
   * - 30s tick：对当前可得周期整体重取一次，保持展开图与接口同步。
   */
  useEffect(() => {
    if (!activeDetailId) return;
    ensureDetail(activeDetailId);
    const retryTimer = setInterval(() => ensureDetail(activeDetailId), REFRESH_MS);
    const ttlTimer = setInterval(() => ensureDetail(activeDetailId, true), DETAIL_REFRESH_MS);
    return () => {
      clearInterval(retryTimer);
      clearInterval(ttlTimer);
    };
  }, [activeDetailId, ensureDetail]);

  /**
   * 迷你图有界更新：只处理「当前视口内可见」的行（由列表上报）。
   * - 从未成功 / 失败且已过退避 / 已成功但超过 TTL → 需要处理；
   * - 失败按指数退避重试（成功即清零），不再出现「失败一次就永久不再更新」；
   * - 新行（含同长度池替换）只要进入视口就会被带上；
   * - 每轮最多 SPARKLINE_MAX_PER_PUMP 行、节拍 20s，绝不对全池扫明细。
   */
  const pumpSparklines = useCallback(() => {
    if (sparkPumping.current) return;
    const now = Date.now();
    const todo = Array.from(visibleRowIds.current)
      .filter((id) => {
        const st = sparkState.current.get(id);
        if (!st) return true; // 从未取过
        if (now < st.nextAttemptAt) return false; // 退避中
        if (st.loadedAt === 0) return true; // 取过但从未成功
        return now - st.loadedAt > SPARKLINE_TTL_MS; // 已过期
      })
      .slice(0, SPARKLINE_MAX_PER_PUMP);
    if (todo.length === 0) return;

    sparkPumping.current = true;
    let cursor = 0;

    const bumpFail = (id: string) => {
      const st = sparkState.current.get(id) ?? { loadedAt: 0, failStreak: 0, nextAttemptAt: 0 };
      st.failStreak += 1;
      st.nextAttemptAt =
        Date.now() + Math.min(SPARKLINE_RETRY_BASE_MS * 2 ** (st.failStreak - 1), SPARKLINE_RETRY_MAX_MS);
      sparkState.current.set(id, st);
    };

    const worker = async () => {
      while (cursor < todo.length) {
        const id = todo[cursor];
        cursor += 1;
        const raw = rawById.current.get(id);
        if (!raw) {
          bumpFail(id);
          continue;
        }
        try {
          const env = await apiGet<RawCandlesEnvelope>(
            `${marketPath(raw.exchange, raw.market_id, 'candles')}?timeframe=1h&limit=24`,
          );
          const spark = sparklineFromCandles(adaptCandles(env, '1h'));
          if (spark.length >= 2) {
            if (alive.current) {
              setTokens((prev) => prev.map((x) => (x.id === id ? { ...x, sparkline24h: spark } : x)));
            }
            sparkState.current.set(id, { loadedAt: Date.now(), failStreak: 0, nextAttemptAt: 0 });
          } else {
            // 请求成功但该合约确实没有足够 1h 点 → 也按退避重试（采集可能随后补齐）
            bumpFail(id);
          }
        } catch {
          // 失败**不**标记为「已加载」，退避后仍会重试
          bumpFail(id);
        }
      }
    };

    Promise.all(Array.from({ length: SPARKLINE_CONCURRENCY }, () => worker()))
      .catch(() => undefined)
      .finally(() => {
        sparkPumping.current = false;
      });
  }, []);

  /** 把内部核对状态发布成对外快照（只含已得出结论的合约）。 */
  const publishFundingPeriods = useCallback(() => {
    const out: Record<string, number | null> = {};
    fundingState.current.forEach((st, id) => {
      if (st.resolved) out[id] = st.periodHours;
    });
    setFundingPeriodHours(out);
  }, []);

  /**
   * 结算周期核对：复用现有只读 funding 接口，逐条用**费率历史相邻 event_time 间隔**判定。
   * 可见行优先；其余按池序低速补扫。有界（每轮 ≤20 条）、低频（15s 节拍、成功 TTL 6h）。
   * 不做全池高频扫描：一轮补扫 20 条 × 3 并发，全池约 7 分钟走完，之后 6 小时内不再请求。
   */
  const pumpFundingPeriods = useCallback(() => {
    if (fundingPumping.current) return;
    // 全池列表请求在途时让路，避免与 4.3MB 的 /markets 争抢单线程后端
    if (refreshBusy.current) return;
    const now = Date.now();
    const allIds = Array.from(rawById.current.keys());
    if (allIds.length === 0) return;

    const need = (id: string): boolean => {
      const st = fundingState.current.get(id);
      if (!st) return true; // 从未核对
      if (now < st.nextAttemptAt) return false; // 退避中
      if (!st.resolved) return true; // 核对过但未得结论
      return now - st.checkedAt > FUNDING_PERIOD_TTL_MS; // 结论已过期
    };

    // 可见行优先，保证当前屏幕上的费率位先拿到真实口径
    const visibleFirst: string[] = [];
    const rest: string[] = [];
    for (const id of visibleRowIds.current) if (need(id)) visibleFirst.push(id);
    for (const id of allIds) {
      if (!visibleRowIds.current.has(id) && need(id)) rest.push(id);
    }
    const todo = visibleFirst.concat(rest).slice(0, FUNDING_PROBE_MAX_PER_PUMP);
    if (todo.length === 0) return;

    fundingPumping.current = true;
    let cursor = 0;

    const bumpFail = (id: string) => {
      const st = fundingState.current.get(id) ?? {
        checkedAt: 0,
        periodHours: null,
        resolved: false,
        failStreak: 0,
        nextAttemptAt: 0,
      };
      st.failStreak += 1;
      st.nextAttemptAt =
        Date.now() + Math.min(FUNDING_PROBE_RETRY_BASE_MS * 2 ** (st.failStreak - 1), FUNDING_PROBE_RETRY_MAX_MS);
      fundingState.current.set(id, st);
    };

    const worker = async () => {
      while (cursor < todo.length) {
        const id = todo[cursor];
        cursor += 1;
        const raw = rawById.current.get(id);
        if (!raw) {
          bumpFail(id);
          continue;
        }
        try {
          const env = await apiGet<Envelope<RawFundingRow>>(
            `${marketPath(raw.exchange, raw.market_id, 'funding')}?limit=${FUNDING_HISTORY_LIMIT}`,
          );
          const verdict = inferFundingPeriod(unwrapItems(env));
          if (verdict.periodHours !== null) {
            fundingState.current.set(id, {
              checkedAt: Date.now(),
              periodHours: verdict.periodHours,
              resolved: true,
              failStreak: 0,
              nextAttemptAt: 0,
            });
          } else if (verdict.sampleCount >= 2) {
            // 历史确实存在但窗口内间隔不一致/非整小时 ⇒ 终态「无法确定」，
            // 不反复请求；该合约的费率位保持空态。
            fundingState.current.set(id, {
              checkedAt: Date.now(),
              periodHours: null,
              resolved: true,
              failStreak: 0,
              nextAttemptAt: 0,
            });
          } else {
            // 历史点不足（采集可能随后补齐）⇒ 退避重试，不写「已核对」
            bumpFail(id);
          }
        } catch {
          bumpFail(id);
        }
      }
    };

    Promise.all(Array.from({ length: FUNDING_PROBE_CONCURRENCY }, () => worker()))
      .catch(() => undefined)
      .finally(() => {
        fundingPumping.current = false;
        if (alive.current) publishFundingPeriods();
      });
  }, [publishFundingPeriods]);

  /**
   * 币安公开扩展字段补扫：**只对可见行**、有界、低频。
   *
   * 与 `pumpFundingPeriods` 同一套纪律（可见行优先、有界、TTL、退避、让路），
   * 但**不**做「其余按池序低速补扫」—— 该接口每行 3 次上游，全池扫会与历史侧抢预算。
   * 因此只覆盖当前屏幕上的行；滚动到新行时由 `noteVisibleRows` 触发。
   */
  const pumpPublicExtras = useCallback((force = false): void => {
    if (extrasPumping.current) return;
    if (extrasRouteAbsent.current) return; // 路由未接线：本会话不再探测
    if (refreshBusy.current) return; // 全池列表请求在途时让路
    const now = Date.now();

    const todo: string[] = [];
    for (const id of visibleRowIds.current) {
      const st = extrasState.current.get(id);
      if (st) {
        if (now < st.nextAttemptAt) continue; // 退避中
        if (!force && now - st.loadedAt < PUBLIC_EXTRAS_TTL_MS) continue; // 本机快照 TTL
      }
      todo.push(id);
    }
    const batch = todo.slice(0, PUBLIC_EXTRAS_MAX_PER_PUMP);
    if (batch.length === 0) return;
    const startedVisibleVersion = visibleRowIds.current.version;

    extrasPumping.current = true;
    let cursor = 0;
    const got: Record<string, PublicExtrasView> = {};

    const worker = async () => {
      while (cursor < batch.length) {
        const id = batch[cursor];
        cursor += 1;
        const raw = rawById.current.get(id);
        if (!raw) continue;
        try {
          const env = await apiGet<RawPublicExtras>(
            `${marketPath(raw.exchange, raw.market_id, 'public-extras')}` +
              `?period=${PUBLIC_EXTRAS_PERIOD}&fields=${PUBLIC_EXTRAS_FIELDS}`,
          );
          const fill = adaptPublicExtras(env);
          const pos = env.long_short?.top_position_long_short_ratio;
          const qualityStatus = env.quality?.status;
          const status: PublicExtrasView['status'] = !env.available
            ? 'missing'
            : qualityStatus === 'stale'
              ? 'stale'
              : qualityStatus === 'ok'
                ? 'fresh'
                : qualityStatus === 'unknown'
                  ? 'unknown'
                  : 'unknown';
          const ratioValue = env.available && pos?.available ? fill?.longShortRatio ?? null : null;
          got[id] = {
            status,
            ratioAvailable: env.available && pos?.available === true,
            longShortRatio: ratioValue,
            oiAvailable: env.available && env.open_interest?.available === true,
            oiAmount: env.open_interest?.available ? fill?.oiAmount ?? null : null,
            oiUsd: env.open_interest?.available ? fill?.oiUsd ?? null : null,
            oiAmountEventTime: env.open_interest?.amount_event_time ?? null,
            oiUsdEventTime: env.open_interest?.usd_event_time ?? null,
            oiEventTime: env.open_interest?.usd_event_time ?? null,
            oiUnit: raw.open_interest_unit ?? null,
            oiSource: env.open_interest?.source ?? null,
            oiSourceUrl: env.source_urls?.find((url) => url.includes('openInterestHist')) ?? null,
            period: pos?.period ?? env.long_short?.period ?? null,
            eventTime: pos?.event_time ?? null,
            snapshotTime: env.snapshot?.refreshed_at ?? null,
            snapshotAgeSeconds: env.quality?.age_seconds ?? null,
            source: env.long_short?.source ?? null,
            sourceUrl: env.source_urls?.find((url) => url.includes('topLongShortPositionRatio')) ?? null,
            qualityNote: env.quality?.note ?? env.snapshot?.note ?? null,
            missingReason: env.available
              ? (pos?.available ? null : pos?.reason ?? '快照中没有大户持仓量多空比')
              : env.missing_reason ?? '本机快照未覆盖',
          };
          extrasState.current.set(id, { loadedAt: Date.now(), failStreak: 0, nextAttemptAt: 0 });
        } catch (err) {
          if ((err as Error)?.name === 'AbortError') return;
          const reason = err instanceof Error ? err.message : '本机快照读取失败';
          // 404 = 后端路由不可用 ⇒ 熔断，停止本会话全部探测（不刷错误、不反复请求）
          if (err instanceof ApiError && err.status === 404) {
            got[id] = {
              status: 'error', ratioAvailable: false, longShortRatio: null, oiAvailable: false,
              oiUsd: null, oiEventTime: null, oiSource: null,
              oiSourceUrl: null, period: null, eventTime: null,
              snapshotTime: null, snapshotAgeSeconds: null, source: null, sourceUrl: null,
              qualityNote: null, missingReason: reason,
            };
            extrasRouteAbsent.current = true;
            return;
          }
          got[id] = {
            status: 'error', ratioAvailable: false, longShortRatio: null, oiAvailable: false,
            oiUsd: null, oiEventTime: null, oiSource: null,
            oiSourceUrl: null, period: null, eventTime: null,
            snapshotTime: null, snapshotAgeSeconds: null, source: null, sourceUrl: null,
            qualityNote: null, missingReason: reason,
          };
          const st = extrasState.current.get(id) ?? { loadedAt: 0, failStreak: 0, nextAttemptAt: 0 };
          st.failStreak += 1;
          st.nextAttemptAt =
            Date.now() +
            Math.min(PUBLIC_EXTRAS_RETRY_BASE_MS * 2 ** (st.failStreak - 1), PUBLIC_EXTRAS_RETRY_MAX_MS);
          extrasState.current.set(id, st);
        }
      }
    };

    Promise.all(Array.from({ length: PUBLIC_EXTRAS_CONCURRENCY }, () => worker()))
      .catch(() => undefined)
      .finally(() => {
        extrasPumping.current = false;
        if (alive.current && Object.keys(got).length > 0) {
          setPublicExtras((prev) => ({ ...prev, ...got }));
        }
        // 若当前批次在途时用户切换了可见标的，只为最新工作集再补一轮；每轮仍限 6 条，TTL/退避不变。
        if (alive.current && visibleRowIds.current.version !== startedVisibleVersion) {
          setTimeout(() => {
            if (alive.current) pumpPublicExtras();
          }, 0);
        }
      });
  }, []);

  /** 定时补扫：节拍 15s（不是列表的 5s）。 */
  useEffect(() => {
    const timer = setInterval(pumpFundingPeriods, FUNDING_PROBE_TICK_MS);
    return () => clearInterval(timer);
  }, [pumpFundingPeriods]);

  /** 本机快照读取：可见行每 60s 最多重读一次；只访问 8787 本机 API，不触发上游采集。 */
  useEffect(() => {
    const timer = setInterval(pumpPublicExtras, PUBLIC_EXTRAS_TICK_MS);
    return () => clearInterval(timer);
  }, [pumpPublicExtras]);

  /** 回到前台 / 窗口重新聚焦时强制读取当前可见行的最新本机快照。 */
  useEffect(() => {
    const refreshOnResume = () => {
      if (!document.hidden) pumpPublicExtras(true);
    };
    document.addEventListener('visibilitychange', refreshOnResume);
    window.addEventListener('focus', refreshOnResume);
    return () => {
      document.removeEventListener('visibilitychange', refreshOnResume);
      window.removeEventListener('focus', refreshOnResume);
    };
  }, [pumpPublicExtras]);

  /** 全池首次到位后立刻开跑一轮，不必等第一个 15s 节拍。 */
  useEffect(() => {
    if (tokens.length === 0) return;
    pumpFundingPeriods();
  }, [tokens.length, pumpFundingPeriods]);

  /** 可见行到位后同样立刻开跑一轮（可见集合在首屏渲染后才有内容）。 */
  useEffect(() => {
    if (tokens.length === 0) return;
    pumpPublicExtras();
  }, [tokens.length, pumpPublicExtras]);

  /**
   * 平均费率 (8h)：只对结算周期已核对为 8h 的合约取真实 `funding_rate` 均值。
   * 一条都没核对出来时为 null（顶栏显示 '—'），绝不把周期未知的费率当 8h 平均。
   */
  const fundingAggregates = useMemo(() => {
    let resolved = 0;
    let eightH = 0;
    const rates: number[] = [];
    for (const t of tokens) {
      const p = fundingPeriodHours[t.id];
      if (p === undefined) continue; // 未核对出结论 → 不参与
      resolved += 1;
      if (p === FUNDING_TARGET_PERIOD_HOURS && t.fundingRate !== null && Number.isFinite(t.fundingRate)) {
        eightH += 1;
        rates.push(t.fundingRate);
      }
    }
    const avg = rates.length > 0 ? rates.reduce((a, b) => a + b, 0) / rates.length : null;
    return { avgFundingRate: avg, fundingPeriodResolved: resolved, fundingPeriod8h: eightH };
  }, [tokens, fundingPeriodHours]);

  const mergedAggregates = useMemo<TopAggregates>(
    () => ({ ...aggregates, ...fundingAggregates }),
    [aggregates, fundingAggregates],
  );

  /** 列表上报视口内可见行；内容变化才触发一轮（内容相同不重复请求）。 */
  const noteVisibleRows = useCallback(
    (ids: string[]) => {
      const previous = visibleRowIds.current;
      const next = Object.assign(new Set(ids), { version: previous.version });
      const same =
        next.size === previous.size &&
        Array.from(next).every((id) => previous.has(id));
      if (!same) next.version += 1;
      visibleRowIds.current = next;
      if (!same) {
        pumpSparklines();
        pumpFundingPeriods();
        pumpPublicExtras();
      }
    },
    [pumpSparklines, pumpFundingPeriods, pumpPublicExtras],
  );

  /** 定时重跑：覆盖 TTL 过期与退避到期的重试；节拍 20s，不是列表的 5s。 */
  useEffect(() => {
    const timer = setInterval(pumpSparklines, SPARKLINE_TICK_MS);
    return () => clearInterval(timer);
  }, [pumpSparklines]);

  /**
   * 把可追溯的币安原始指标并入原有字段：大户持仓量多空比仍保持原始含义；
   * OI 数量与名义额只在同一条新鲜 public-extras 行、且指标时点一致时成对兜底。
   * 页面仍保留原始来源/时间并独立判断陈旧；没有覆盖或失败不补 0。
   */
  const tokensWithExtras = useMemo<TokenMarketItem[]>(() => {
    if (Object.keys(publicExtras).length === 0) return tokens;
    const now = Date.now();
    return tokens.map((t) => {
      const e = publicExtras[t.id];
      if (!e) return t;
      const ratioReadable = e.status === 'fresh' || e.status === 'stale' || e.status === 'unknown';
      const snapshotMs = e.snapshotTime ? Date.parse(e.snapshotTime) : Number.NaN;
      const oiEventMs = e.oiUsdEventTime ? Date.parse(e.oiUsdEventTime) : Number.NaN;
      const snapshotAge = Number.isFinite(snapshotMs) ? Math.max(0, (now - snapshotMs) / 1000) : null;
      const oiAge = Number.isFinite(oiEventMs) ? Math.max(0, (now - oiEventMs) / 1000) : null;
      const oiFresh = e.status === 'fresh' && snapshotAge !== null &&
        snapshotAge <= PUBLIC_EXTRAS_SNAPSHOT_STALE_AFTER_S && oiAge !== null &&
        oiAge <= PUBLIC_EXTRAS_OI_STALE_AFTER_S;
      const extrasPairFresh = oiFresh && typeof e.oiAmount === 'number' && Number.isFinite(e.oiAmount) &&
        typeof e.oiUsd === 'number' && Number.isFinite(e.oiUsd) &&
        !!e.oiAmountEventTime && e.oiAmountEventTime === e.oiUsdEventTime;
      const longShortRatio = ratioReadable ? e.longShortRatio : null;
      /**
       * 陈旧优先修正（与 TokenTable 同一规则）：快照成对且新鲜时，还必须**不早于**列表行
       * 的 OI 指标时点，才允许覆盖列表值；否则保留较新的列表值（列表缺失时才回落到快照）。
       */
      const listOiMs = t.oi.amountEventTime ? Date.parse(t.oi.amountEventTime) : Number.NaN;
      const pubOiMs = e.oiAmountEventTime ? Date.parse(e.oiAmountEventTime) : Number.NaN;
      const extrasNewerThanList = extrasPairFresh && Number.isFinite(pubOiMs) &&
        (!Number.isFinite(listOiMs) || pubOiMs > listOiMs);
      const listAmount = t.oi.totalAmount ?? null;
      const listUsd = t.oi.totalUsd ?? null;
      const totalAmount = extrasNewerThanList
        ? (e.oiAmount ?? listAmount)
        : (listAmount ?? (extrasPairFresh ? e.oiAmount ?? null : null));
      const totalUsd = extrasNewerThanList
        ? (e.oiUsd ?? listUsd)
        : (listUsd ?? (extrasPairFresh ? e.oiUsd ?? null : null));
      if (
        longShortRatio === t.oi.longShortRatio &&
        totalAmount === t.oi.totalAmount &&
        totalUsd === t.oi.totalUsd
      ) return t;
      return { ...t, oi: { ...t.oi, longShortRatio, totalAmount, totalUsd } };
    });
  }, [tokens, publicExtras]);

  return {
    tokens: tokensWithExtras,
    publicExtras,
    signals,
    loading,
    refreshing,
    error,
    asOf,
    aggregates: mergedAggregates,
    health,
    ensureDetail,
    setActiveDetailId,
    noteVisibleRows,
    fundingPeriodHours,
    importedAt,
  };
}

export { TIMEFRAMES };
export type { Timeframe };
