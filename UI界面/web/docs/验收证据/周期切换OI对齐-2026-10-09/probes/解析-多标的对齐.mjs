import fs from 'node:fs';
const j = JSON.parse(fs.readFileSync('/tmp/oi_fix/after3_verify.json', 'utf8'));
console.log('prefix=', j.prefix, 'errors=', JSON.stringify(j.errors));
for (const r of j.results) {
  if (r.error) { console.log(`!! ${r.sym} ERROR ${r.error}`); continue; }
  console.log(`\n=== ${r.sym} (${r.id}) isOkx=${r.isOkx} ===`);
  console.log(`  tfButtons=${JSON.stringify(r.tfButtons)}`);
  console.log(`  okx8h.has8hButton=${r.okx8h && r.okx8h.has8hButton}`);
  for (const s of r.steps) {
    if (s.skipped) { console.log(`   - ${s.tf}: SKIPPED(${s.skipped})`); continue; }
    const a = s.measure.align;
    const bs = s.measure.rows.map((x) => `${x.label}:bs=${x.barSpacing && x.barSpacing.toFixed(6)} psW=${x.psW}`).join(' | ');
    console.log(`   - ${s.tf}: n=${a && a.n} max=${a && a.maxAbsDiff} over1px=${a && a.over1px}  [${bs}]`);
  }
  const d = r.dataCheck;
  if (d) {
    console.log(`  dataCheck @ ${d.lastTf}:`);
    for (const k of ['candle','volume','oi']) {
      const c = d[k];
      console.log(`    ${k}: ${c ? JSON.stringify(c) : 'null'}`);
    }
  }
}
