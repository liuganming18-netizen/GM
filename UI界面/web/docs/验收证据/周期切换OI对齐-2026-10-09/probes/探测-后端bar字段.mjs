const r = await fetch('http://127.0.0.1:8787/api/v1/markets/binance/BTCUSDT/candles?timeframe=1d&limit=3&no_rest_tail=1');
const j = await r.json();
console.log('bar keys:', Object.keys(j.bars[0]));
console.log(JSON.stringify(j.bars[0], null, 1));
