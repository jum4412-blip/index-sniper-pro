# Index Sniper Pro v2.6 BTC 5x Whipsaw Guard Patch

BTC 전용 5배 No-MA 실전 후보 패치입니다. 횡보장 휩쏘를 줄이기 위해 아래 방어를 추가합니다.

- BTCUSDT only
- No-MA volatility breakout
- long/short both=stronger
- capital ratio 30%, leverage 5x
- notional cap 2500 USDT
- stronger breakout confirmation: `SURVIVAL_MIN_BREAKOUT_ATR=0.15`
- late entry cap: `MAX_ENTRY_EXTENSION_ATR=0.30`
- anti-chase tightened: 6% / 1.5 ATR
- whipsaw filter: efficiency >= 0.22 and flip ratio <= 0.60 over 10 completed daily candles
- live guard: monthly loss block 8%, MDD block 18%, drawdown warning 10%

This patch does not force live trading on. Preflight first.
