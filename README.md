# Index Sniper Pro v2.5 Partial Exit Patch

Adds BTC No-MA partial-exit backtest tools.

Default profile: `p50_be_25`

- 50% take profit at +1.0 ATR
- Move remaining stop to breakeven after first TP
- Remaining 50% take profit at +2.5 ATR
- BTC only / No-MA / long-short / both=stronger / 30% capital / leverage 1x-10x

## Run

```bash
cd ~/index-sniper-pro
source .venv/bin/activate
bash run_btc_partial_exit_final_1y_5y.sh
bash run_btc_partial_exit_risk_1y_3y.sh
bash view_btc_partial_exit.sh
```

## Other profiles

```bash
PARTIAL_PROFILE=p40_30_30_be bash run_btc_partial_exit_final_1y_5y.sh
PARTIAL_PROFILE=p50_be_20 bash run_btc_partial_exit_final_1y_5y.sh
```

## Output files

- `backtests/btc_partial_final_latest.txt`
- `backtests/btc_partial_risk_latest.txt`
- `backtests/btc_partial_monthly_pnl_1y_<profile>_ls_stronger.txt`
- `backtests/btc_partial_monthly_pnl_3y_<profile>_ls_stronger.txt`
- `backtests/btc_partial_worst10_trades_<profile>_ls_stronger.txt`
