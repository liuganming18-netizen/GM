import fs from 'node:fs';
function load(f){ return JSON.parse(fs.readFileSync('/tmp/oi_fix/'+f+'.json','utf8')); }
function report(j, tag){
  const rs = j.results;
  let n=0, ok=0, worstMax=-1, worstName='', worstOver=-1, worstOverName='', over1Total=0;
  const lines=[];
  for (const r of rs){
    const a = r.measure && r.measure.align;
    if(!a){ continue; }
    n++;
    if(a.maxAbsDiff===0 && a.over1px===0) ok++;
    over1Total += a.over1px;
    if(a.maxAbsDiff>worstMax){ worstMax=a.maxAbsDiff; worstName=r.label; }
    if(a.over1px>worstOver){ worstOver=a.over1px; worstOverName=r.label; }
    lines.push(`${r.label.padEnd(26)} n=${String(a.n).padStart(4)} rangeEq=${a.rangeEqual} logicalEq=${a.logicalEqual} bsC=${a.barSpacingCandle.toFixed(6)} bsO=${a.barSpacingOi.toFixed(6)} max=${String(a.maxAbsDiff).padStart(7)} over1px=${String(a.over1px).padStart(4)}`);
  }
  console.log(`\n===== ${tag} ===== 场景数=${n} 完全对齐=${ok}/${n} 超1px点合计=${over1Total}`);
  console.log(`最差: max=${worstMax}px @${worstName} ; 最多超1px=${worstOver} @${worstOverName}`);
  console.log(`errors=${JSON.stringify(j.errors)}`);
  return lines;
}
const lb = report(load('before_scenarios'),'BEFORE');
const la = report(load('after2_scenarios'),'AFTER');
console.log('\n--- BEFORE 明细 ---'); lb.forEach(l=>console.log('  '+l));
console.log('\n--- AFTER 明细 ---'); la.forEach(l=>console.log('  '+l));
