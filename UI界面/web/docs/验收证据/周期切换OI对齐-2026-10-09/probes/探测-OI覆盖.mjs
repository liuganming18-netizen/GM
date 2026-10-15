const api = async (p) => (await fetch('http://127.0.0.1:8787/api/v1' + p)).json();
for (const [ex, mid] of [['binance','BTCUSDT'],['binance','ETHUSDT'],['binance','0GUSDT'],['okx','AEON-USDT-SWAP']]) {
  const base = `/markets/${encodeURIComponent(ex)}/${encodeURIComponent(mid)}`;
  let oi = null, cd = null;
  try { oi = await api(base + '/oi'); } catch (e) { oi = { err: String(e) }; }
  try { cd = await api(base + '/candles?timeframe=30m&limit=5&no_rest_tail=1'); } catch (e) { cd = { err: String(e) }; }
  const oiN = (oi && oi.items || []).length;
  const last = (oi && oi.items || [])[oiN - 1];
  console.log(`${ex}:${mid} oiItems=${oiN} lastEvent=${last && last.event_time} oi=${last && last.open_interest} | bars=${(cd.bars||[]).length} is_native=${cd.is_native} tfs=${cd.timeframe}`);
}
