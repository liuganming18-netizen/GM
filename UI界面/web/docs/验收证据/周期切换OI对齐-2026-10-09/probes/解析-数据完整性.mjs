import fs from 'node:fs';
const j = JSON.parse(fs.readFileSync('/tmp/oi_fix/dc1.json','utf8'));
const api = async (p) => (await fetch('http://127.0.0.1:8787/api/v1'+p)).json();
console.log('errors=', JSON.stringify(j.errors));
let badTotal = 0, formingChecked = 0, formingOk = 0;
for (const r of j.results) {
  console.log(`\n=== ${r.sym} (${r.id}) tfButtons=${JSON.stringify(r.tfButtons)} ===`);
  for (const s of r.tfs) {
    if (s.skipped) { console.log(`  - ${s.tf}: SKIPPED(${s.skipped})`); continue; }
    for (const c of s.candles) {
      if (c.bad.length) { badTotal += c.bad.length; console.log(`  - ${s.tf} candle BAD:`, JSON.stringify(c.bad)); }
    }
    if (s.volume && s.volume.bad.length) { badTotal += s.volume.bad.length; console.log(`  - ${s.tf} vol BAD:`, JSON.stringify(s.volume.bad)); }
    if (s.oi && s.oi.bad.length) { badTotal += s.oi.bad.length; console.log(`  - ${s.tf} oi BAD:`, JSON.stringify(s.oi.bad)); }
    // 未收盘根核对
    const c0 = s.candles[0];
    if (c0 && c0.unknown === 1) {
      const env = await api(`/markets/${encodeURIComponent(r.ex)}/${encodeURIComponent(r.mid)}/candles?timeframe=${s.tf}&limit=500&no_rest_tail=1`);
      const fb = env.forming_bar;
      if (fb) {
        formingChecked++;
        const ts = Math.floor(Date.parse(fb.open_time)/1000);
        console.log(`  - ${s.tf} forming: backend=${ts} seriesTail=${c0.tailTime} ${ts===c0.tailTime?'MATCH':'DIFF'}`);
        if (ts === c0.tailTime) formingOk++;
      } else {
        console.log(`  - ${s.tf} forming: 后端无 forming_bar 但序列有 1 未知点`);
      }
    }
    if (s.oi) console.log(`  - ${s.tf} oi: n=${s.oi.n} match=${s.oi.match} apiRows=${s.oi.apiRows} head=${s.oi.headTime} tail=${s.oi.tailTime}`);
  }
}
console.log(`\nTOTAL_BAD=${badTotal} formingChecked=${formingChecked} formingOk=${formingOk}`);
