# v5.2 Top10 VWAP Scalp Backtest Patch

Backtests the v5.2 Top10 VWAP rubber-band scalp idea with candle approximation.

Rules approximated:
- Top 10 symbols default: BTC, ETH, BNB, XRP, SOL, TRX, HYPE, DOGE, ZEC, ADA.
- VWAP bands using OHLCV candles.
- ADX <= 20 for range filter.
- Maker entry at lower/upper VWAP band.
- TP +0.6%, SL -0.3%, VWAP-touch exit.
- Maker fee 0.02%, taker fee 0.05%.
- Shock cooldown if one-bar move exceeds 0.15%.
- Default backtest: 30 days of 1m candles, leverage 1x~20x.

Caution: this is not tick-perfect. It uses 1m/5m candles and conservative intrabar assumptions.
