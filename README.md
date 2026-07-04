# Index Sniper Pro v2.3 BTC Optimizer

This version keeps the v2.2 decomposed backtest tools and adds a **BTC-only optimizer**.

Main purpose:

- Backtest **BTCUSDT only**.
- Keep **BT_CAPITAL_RATIO=0.30** for backtests.
- Sweep leverage **1x through 10x**.
- Test multiple parameter combinations: K value, EMA pair, ATR stop/take-profit, extension filter, anti-chase filter, and side mode.
- Compare 3-year and 5-year results so a setting that only worked in one window does not fool us.

Important: this is a **backtest research upgrade**. It does not automatically change live trading unless you deliberately edit live keys such as `SYMBOLS`, `CAPITAL_RATIO`, and `LEVERAGE`.

---

## Apply BTC-only backtest settings

```bash
cd ~/index-sniper-pro
bash apply_backtest_btc30_optimizer.sh
```

This sets:

```text
BT_SYMBOLS=BTCUSDT
BT_INITIAL_EQUITY=1374
BT_CAPITAL_RATIO=0.30
BT_MAX_ORDER_NOTIONAL_USDT=1000
BT_OPT_MAX_ORDER_NOTIONAL_USDT=999999
```

The regular backtest cap stays at `BT_MAX_ORDER_NOTIONAL_USDT=1000`, but the BTC optimizer uses `BT_OPT_MAX_ORDER_NOTIONAL_USDT=999999`. This separate optimizer cap is intentional. If the optimizer stayed capped at `1000`, leverage above about 2.4x would be flattened and the 1x~10x sweep would not be meaningful.

---

## First: leverage-only BTC test

Fastest check.

```bash
bash run_btc_leverage_sweep_5y.sh --refresh
bash run_btc_leverage_sweep_3y.sh --refresh
```

It tests:

```text
BTC long/short
BTC long-only
BTC short-only
leverage 1x~10x
current base strategy parameters
```

---

## Second: BTC optimizer quick run

Recommended first optimizer run.

```bash
bash run_btc_optimizer_quick_5y.sh --refresh
bash run_btc_optimizer_quick_3y.sh --refresh
```

Or run 5y + 3y + comparison in one command:

```bash
PRESET=quick bash run_btc_optimizer_compare_3y_5y.sh --refresh
```

---

## Third: default optimizer

Larger grid.

```bash
bash run_btc_optimizer_5y.sh --refresh
bash run_btc_optimizer_3y.sh --refresh
python3 -m index_sniper.backtest.btc_optimizer_compare
```

Or:

```bash
PRESET=default bash run_btc_optimizer_compare_3y_5y.sh --refresh
```

---

## Wide optimizer

This is much heavier. Use only after quick/default results look promising.

```bash
PRESET=wide bash run_btc_optimizer_compare_3y_5y.sh --refresh --top 50 --top-detail 20
```

---

## View results

```bash
bash view_btc_optimizer.sh
```

Main files:

```text
backtests/btc_optimizer_latest.txt
backtests/btc_optimizer_latest.csv
backtests/btc_optimizer_5y_latest.txt
backtests/btc_optimizer_5y_latest.csv
backtests/btc_optimizer_3y_latest.txt
backtests/btc_optimizer_3y_latest.csv
backtests/btc_optimizer_compare_latest.txt
backtests/btc_optimizer_compare_latest.csv
backtests/btc_optimizer_runs/
```

---

## Custom examples

Only test leverage 1x~10x, long/short only:

```bash
python3 -m index_sniper.backtest.btc_optimizer --years 5 --preset leverage --side-modes ls --refresh
```

Test custom K and EMA combinations:

```bash
python3 -m index_sniper.backtest.btc_optimizer \
  --years 5 \
  --preset default \
  --leverages 1-10 \
  --k-values 0.25,0.35,0.45,0.55,0.65 \
  --ema-pairs 10/40,20/60,30/90 \
  --stop-values 1.0,1.3,1.6 \
  --tp-values 1.8,2.2,2.8 \
  --side-modes ls,long,short \
  --refresh
```

---

## Environment cleanup

If you see warnings like:

```text
Python-dotenv could not parse statement starting at line ...
```

run:

```bash
bash clean_dotenv_parse_errors.sh
```

It backs up `.env`, removes invalid pasted lines, and saves the removed lines separately.

---

## Interpretation rule

Do not pick the strategy with the highest 5-year return alone. Prefer a setting that survives both 5y and 3y:

- 5y return strong
- 3y return positive
- MDD not insane
- Profit factor above 1.15 if possible
- Calmar better than the current baseline
- No extreme dependence on only one market regime

