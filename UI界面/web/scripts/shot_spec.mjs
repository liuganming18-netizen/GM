/**
 * 设计规范页截图（本地 file:// 打开，验证 tokens.css + components.css 渲染正常）。
 * 用法：node scripts/shot_spec.mjs <输出目录>
 */
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';

const OUT = process.argv[2] || '/tmp/ds_spec';
fs.mkdirSync(OUT, { recursive: true });
const url = 'file://' + path.resolve(process.cwd(), '设计规范.html');

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
await page.goto(url, { waitUntil: 'load' });
await page.waitForTimeout(600);

// 校验关键令牌真的生效（而不是 fallback 成黑色）
const probe = await page.evaluate(() => {
  const cs = getComputedStyle(document.documentElement);
  const card = document.querySelector('.ds-card');
  const btn = document.querySelector('.ds-btn--primary');
  return {
    accent: cs.getPropertyValue('--ds-accent').trim(),
    text: cs.getPropertyValue('--ds-text').trim(),
    cardBg: card ? getComputedStyle(card).backgroundColor : null,
    cardBorder: card ? getComputedStyle(card).borderTopColor : null,
    btnBg: btn ? getComputedStyle(btn).backgroundColor : null,
    bodyBg: getComputedStyle(document.body).backgroundColor,
    swatchCount: document.querySelectorAll('.sw').length,
    sectionCount: document.querySelectorAll('section').length,
  };
});
console.log('令牌探测:', JSON.stringify(probe));

await page.screenshot({ path: path.join(OUT, 'spec_01_top.png') });
await page.screenshot({ path: path.join(OUT, 'spec_02_full.png'), fullPage: true });

for (const [name, sel] of [
  ['color', '#color'],
  ['type', '#type'],
  ['stat', '#stat'],
  ['table', '#table'],
  ['grid', '#grid'],
  ['pagehead', '#pagehead'],
  ['usage', '#usage'],
]) {
  await page.evaluate((s) => document.querySelector(s)?.scrollIntoView(), sel);
  await page.waitForTimeout(250);
  await page.screenshot({ path: path.join(OUT, `spec_${name}.png`) });
  console.log('saved', `spec_${name}.png`);
}

await browser.close();
console.log('done');
