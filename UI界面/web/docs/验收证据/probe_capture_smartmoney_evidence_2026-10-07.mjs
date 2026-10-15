import fs from 'node:fs';
import { execFileSync } from 'node:child_process';

const HOST = 'https://www.binance.bh';
const P = '/bapi/futures/v1/public/future/smart-money/signal';
const OUT = process.argv[2];

const curl = (url) => {
  const out = execFileSync('curl', [
    '-s', '-m', '30',
    // 显式不带任何 cookie / 自定义鉴权头，只留 Accept
    '-H', 'Accept: application/json',
    '-w', '\n@@HTTP=%{http_code}',
    url,
  ], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
  const idx = out.lastIndexOf('\n@@HTTP=');
  const body = idx >= 0 ? out.slice(0, idx) : out;
  const status = idx >= 0 ? Number(out.slice(idx + 8).trim()) : 0;
  let json = null;
  try { json = JSON.parse(body); } catch { /* keep null */ }
  return { status, json, rawLength: body.length };
};

const overviewUrl = (s, tr = 'ALL') => `${HOST}${P}/overview?symbol=${s}&timeRange=${tr}`;
const statsUrl = (s, tr = 'ALL') => `${HOST}${P}/details/stats?symbol=${s}&timeRange=${tr}`;
const listUrl = (s, tr = 'ALL') => `${HOST}${P}/details/list?symbol=${s}&page=1&rows=10&timeRange=${tr}&sortingField=TIME&sortingOrder=DESC`;

const evidence = {
  title: '币安公开聪明钱接口 真实响应与接入条件（只读核查）',
  checkedAt: new Date().toISOString(),
  host: HOST,
  credentialPolicy: '不携带 cookie / Authorization / 交易密钥；不读取任何个人交易账户；不付费。仅 curl 默认头 + Accept: application/json。',
  routes: [
    { name: 'overview', url: `${HOST}${P}/overview?symbol={SYMBOL}&timeRange=ALL`, required: ['symbol'], optional: ['timeRange'], note: 'timeRange 缺省时口径与 ALL 不同（实测 longTraders 172 vs 168），必须显式传 ALL 才与 stats 自洽' },
    { name: 'details/stats', url: `${HOST}${P}/details/stats?symbol={SYMBOL}&timeRange=ALL`, required: ['symbol', 'timeRange'], optional: [], note: '缺 timeRange ⇒ HTTP 400 {"code":"000002","message":"illegal parameter"}' },
    { name: 'details/list', url: `${HOST}${P}/details/list?symbol={SYMBOL}&page=1&rows=10&timeRange=ALL&sortingField=TIME&sortingOrder=DESC`, required: ['symbol', 'timeRange'], optional: ['page', 'rows', 'sortingField', 'sortingOrder'], note: '缺 page/rows/sortingField/sortingOrder 仍 200（有默认值）；缺 timeRange ⇒ 400' },
  ],
  paramTests: [],
  samples: {},
  coverageSample: {},
};

// ── 参数必需性实测 ─────────────────────────────────────
for (const [label, url] of [
  ['overview 缺 symbol', `${HOST}${P}/overview`],
  ['overview 无效 symbol=NOSUCHCOINUSDT', `${HOST}${P}/overview?symbol=NOSUCHCOINUSDT`],
  ['overview 缺 timeRange（默认口径）', `${HOST}${P}/overview?symbol=ARXUSDT`],
  ['overview timeRange=ALL', overviewUrl('ARXUSDT')],
  ['details/stats 缺 timeRange', `${HOST}${P}/details/stats?symbol=ARXUSDT`],
  ['details/stats timeRange=ALL', statsUrl('ARXUSDT')],
  ['details/list 缺 timeRange', `${HOST}${P}/details/list?symbol=ARXUSDT`],
  ['details/list 最小参数(symbol+timeRange)', `${HOST}${P}/details/list?symbol=ARXUSDT&timeRange=ALL`],
]) {
  const r = curl(url);
  evidence.paramTests.push({
    label,
    url,
    httpStatus: r.status,
    code: r.json?.code ?? null,
    message: r.json?.message ?? null,
    success: r.json?.success ?? null,
    dataIsNull: r.json ? r.json.data === null : null,
    // 只留能证明口径的少量字段
    dataKeys: r.json?.data && typeof r.json.data === 'object' && !Array.isArray(r.json.data)
      ? Object.keys(r.json.data)
      : (Array.isArray(r.json?.data) ? `array(${r.json.data.length})` : null),
    longTraders: r.json?.data && !Array.isArray(r.json.data) ? (r.json.data.longTraders ?? null) : null,
  });
}

// ── 真实样本（4 个合约 × 3 条路由）──────────────────────
for (const sym of ['ARXUSDT', 'BTCUSDT', '0GUSDT', 'ETHUSDT']) {
  const ov = curl(overviewUrl(sym));
  const st = curl(statsUrl(sym));
  const ls = curl(listUrl(sym));
  evidence.samples[sym] = {
    overview: { httpStatus: ov.status, code: ov.json?.code ?? null, data: ov.json?.data ?? null },
    stats: { httpStatus: st.status, code: st.json?.code ?? null, data: st.json?.data ?? null },
    list: {
      httpStatus: ls.status,
      code: ls.json?.code ?? null,
      rowCount: Array.isArray(ls.json?.data) ? ls.json.data.length : null,
      // 页面渲染的正是这三列：交易员人数 / 当前仓位(USDT) / 平均开仓价
      rows: Array.isArray(ls.json?.data)
        ? ls.json.data.map((r) => ({
            side: r.side, userType: r.userType, traders: r.traders,
            currentPositions: r.currentPositions, currentQty: r.currentQty,
            avgEntryPrice: r.avgEntryPrice, timeRange: r.timeRange,
          }))
        : null,
    },
  };
}

// ── 覆盖抽样（从本机 /api/v1/markets 的 526 个币安合约里等距抽 30 个）──
const markets = JSON.parse(fs.readFileSync('/tmp/markets.json', 'utf8'));
const binance = markets.items.filter((i) => i.exchange === 'binance').map((i) => i.market_id);
const step = Math.max(1, Math.floor(binance.length / 30));
const sample = binance.filter((_, i) => i % step === 0).slice(0, 30);
const rows = [];
for (const s of sample) {
  const r = curl(overviewUrl(s));
  rows.push({
    symbol: s,
    httpStatus: r.status,
    hasData: !!(r.json && r.json.data),
    totalTraders: r.json?.data?.totalTraders ?? null,
    longTraders: r.json?.data?.longTraders ?? null,
    shortTraders: r.json?.data?.shortTraders ?? null,
  });
}
evidence.coverageSample = {
  poolBinanceCount: binance.length,
  poolOkxCount: markets.items.length - binance.length,
  poolTotal: markets.items.length,
  sampled: rows.length,
  withData: rows.filter((r) => r.hasData).length,
  rows,
};

fs.writeFileSync(OUT, JSON.stringify(evidence, null, 1), 'utf8');
console.log('written', OUT);
console.log('paramTests:', evidence.paramTests.map((t) => `${t.label}=${t.httpStatus}/${t.code}`).join(' | '));
console.log('coverage:', evidence.coverageSample.withData, '/', evidence.coverageSample.sampled);
