import fs from 'node:fs';
const BASE = 'http://127.0.0.1:8787/api/v1';
const j = async (u) => {
  const r = await fetch(u, { headers: { Accept: 'application/json' }, cache: 'no-store' });
  return { status: r.status, body: await r.json().catch(() => null) };
};
const mk = await j(`${BASE}/markets?limit=600`);
const rows = mk.body.items;
fs.writeFileSync('/tmp/markets_fresh.json', JSON.stringify(mk.body, null, 1), 'utf8');

const hasUsd = (r) => typeof r.open_interest_usd === 'number' && Number.isFinite(r.open_interest_usd);
const byEx = {};
const srcCount = {};
for (const r of rows) {
  byEx[r.exchange] = byEx[r.exchange] ?? { total: 0, usd: 0 };
  byEx[r.exchange].total++;
  if (hasUsd(r)) {
    byEx[r.exchange].usd++;
    const s = r.oi_source ?? '(null)';
    srcCount[s] = (srcCount[s] ?? 0) + 1;
  }
}
console.log('as_of', mk.body.as_of);
console.log('markets usd non-null:', rows.filter(hasUsd).length, '/', rows.length);
console.log('byExchange:', JSON.stringify(byEx));
console.log('oi_source histogram:', JSON.stringify(srcCount, null, 1));
console.log('sample binance rows with usd:', JSON.stringify(rows.filter((r) => r.exchange === 'binance' && hasUsd(r)).slice(0, 3).map((r) => ({ id: r.market_id, usd: r.open_interest_usd, src: r.oi_source, t: r.oi_time, unit: r.open_interest_unit, amt: r.open_interest_total })), null, 1));
console.log('sample binance rows WITHOUT usd:', JSON.stringify(rows.filter((r) => r.exchange === 'binance' && !hasUsd(r)).slice(0, 3).map((r) => ({ id: r.market_id, usd: r.open_interest_usd, src: r.oi_source, t: r.oi_time, amt: r.open_interest_total })), null, 1));

// public-extras 全池（本机只读快照，不触发上游）
const extras = new Map();
let cursor = 0;
const list = rows.map((r) => ({ ex: r.exchange, mid: r.market_id }));
const q = async (r) => {
  const res = await j(`${BASE}/markets/${encodeURIComponent(r.ex)}/${encodeURIComponent(r.mid)}/public-extras?period=1h&fields=ratio,oi`);
  const b = res.body;
  return {
    id: `${r.ex}:${r.mid}`,
    status: res.status,
    available: b?.available ?? null,
    oiUsd: typeof b?.open_interest?.usd === 'number' ? b.open_interest.usd : null,
    oiAmount: typeof b?.open_interest?.amount === 'number' ? b.open_interest.amount : null,
    oiUsdEventTime: b?.open_interest?.usd_event_time ?? null,
    missingReason: (b?.missing_reason ?? '').slice(0, 60),
  };
};
const out = [];
await Promise.all(Array.from({ length: 8 }, async () => {
  while (cursor < list.length) {
    const r = list[cursor++];
    out.push(await q(r));
  }
}));
for (const x of out) extras.set(x.id, x);
const exUsd = out.filter((x) => x.oiUsd !== null);
console.log('public-extras sampled all:', out.length, '| with oi.usd:', exUsd.length);
const exByEx = {};
for (const x of out) {
  const ex = x.id.split(':')[0];
  exByEx[ex] = exByEx[ex] ?? { total: 0, usd: 0 };
  exByEx[ex].total++;
  if (x.oiUsd !== null) exByEx[ex].usd++;
}
console.log('public-extras byExchange:', JSON.stringify(exByEx));
console.log('extras sample with usd:', JSON.stringify(exUsd.slice(0, 5), null, 1));

// union
let union = 0;
const unionByEx = {};
const onlyExtras = [];
const onlyMarkets = [];
for (const r of rows) {
  const id = `${r.exchange}:${r.market_id}`;
  const e = extras.get(id);
  const m = hasUsd(r);
  const x = e && e.oiUsd !== null;
  const any = m || x;
  unionByEx[r.exchange] = unionByEx[r.exchange] ?? { total: 0, union: 0, markets: 0, extras: 0 };
  unionByEx[r.exchange].total++;
  if (any) unionByEx[r.exchange].union++;
  if (m) unionByEx[r.exchange].markets++;
  if (x) unionByEx[r.exchange].extras++;
  if (any) union++;
  if (x && !m) onlyExtras.push(id);
  if (m && !x) onlyMarkets.push(id);
}
console.log('UNION oi amount coverage:', union, '/', rows.length);
console.log('unionByExchange:', JSON.stringify(unionByEx, null, 1));
console.log('only in public-extras (not in markets):', onlyExtras.length, onlyExtras.slice(0, 10));
console.log('only in markets (not in public-extras):', onlyMarkets.length);

// 新鲜度
const now = Date.now();
const ageOf = (t) => (t ? Math.round((now - Date.parse(t)) / 1000) : null);
const fresh = [], stale = [], unknown = [];
for (const r of rows) {
  const id = `${r.exchange}:${r.market_id}`;
  const e = extras.get(id);
  const m = hasUsd(r);
  const x = e && e.oiUsd !== null;
  if (!m && !x) continue;
  const ages = [];
  if (m) ages.push(ageOf(r.oi_time));
  if (x) ages.push(ageOf(e.oiUsdEventTime));
  const a = Math.min(...ages.filter((v) => v !== null).concat([Number.POSITIVE_INFINITY]));
  if (!Number.isFinite(a)) unknown.push(id);
  else if (a <= 900) fresh.push(id);
  else stale.push(id);
}
console.log('freshness(<=900s fresh):', JSON.stringify({ fresh: fresh.length, stale: stale.length, unknown: unknown.length }));

fs.writeFileSync('/tmp/oi_coverage_full.json', JSON.stringify({
  checkedAt: new Date().toISOString(),
  marketsAsOf: mk.body.as_of,
  pool: { total: rows.length },
  markets: { nonNull: rows.filter(hasUsd).length, byExchange: byEx, oiSourceHistogram: srcCount },
  publicExtras: { queried: out.length, withOiUsd: exUsd.length, byExchange: exByEx, rows: out },
  union: { coverage: union, total: rows.length, byExchange: unionByEx, onlyExtras: onlyExtras.length, onlyMarkets: onlyMarkets.length },
  freshness: { fresh: fresh.length, stale: stale.length, unknown: unknown.length },
}, null, 1), 'utf8');
