# Index Sniper Pro v2.4 — BTC No-MA Optimizer

v2.4 adds a BTC-only optimizer that can compare the existing EMA trend-filter strategy against a pure volatility-breakout strategy with the moving-average filter removed.

## What this version tests

- BTCUSDT only
- Backtest capital ratio: 30%
- Leverage sweep: 1x through 10x
- Side modes:
  - `ls` = long/short
  - `long` = long-only
  - `short` = short-only
- MA modes:
  - `ema` = existing EMA trend filter
  - `none` = no moving-average trend filter, pure volatility breakout
- No-MA both-breakout handling:
  - `skip` = conservative, skip days where both long and short targets are touched
  - `stronger` = choose the side with the larger ATR-normalized follow-through
  - `candle` = choose long on green candle, short on red candle
- K value, ATR stop/take-profit, entry extension, and anti-chase parameters

## First-time EC2 setup

If Ubuntu blocks `pip install` globally, use the project virtual environment:

```bash
cd ~/index-sniper-pro
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python - <<'PY'
from dotenv import load_dotenv
print('dotenv ok')
PY
```

For later runs:

```bash
cd ~/index-sniper-pro
source .venv/bin/activate
```

## Apply BTC optimizer settings

This is backtest-only and does not change live trading keys such as `CAPITAL_RATIO`.

```bash
bash clean_dotenv_parse_errors.sh
bash apply_backtest_btc30_optimizer.sh
```

## 1) No-MA leverage sweep only

```bash
bash run_btc_no_ma_leverage_sweep_5y.sh --refresh
bash run_btc_no_ma_leverage_sweep_3y.sh --refresh
bash view_btc_optimizer.sh
```

## 2) No-MA quick optimizer and 3y/5y comparison

```bash
PRESET=no_ma_quick bash run_btc_no_ma_optimizer_compare_3y_5y.sh --refresh --top 40 --top-detail 10
cat backtests/btc_optimizer_compare_latest.txt
```

## 3) EMA vs No-MA combined comparison

This compares the existing EMA filter against the no-MA version in the same optimizer grid.

```bash
PRESET=ma_mix bash run_btc_ma_mix_compare_3y_5y.sh --refresh --top 50 --top-detail 10
cat backtests/btc_optimizer_compare_latest.txt
```

## 4) Wider no-MA search

This can take much longer. Run it after the quick run gives a promising range.

```bash
PRESET=no_ma_wide bash run_btc_no_ma_optimizer_compare_3y_5y.sh --refresh --top 80 --top-detail 15
cat backtests/btc_optimizer_compare_latest.txt
```

## Output files

- `backtests/btc_optimizer_latest.txt`
- `backtests/btc_optimizer_latest.csv`
- `backtests/btc_optimizer_5y_latest.txt`
- `backtests/btc_optimizer_3y_latest.txt`
- `backtests/btc_optimizer_compare_latest.txt`
- `backtests/btc_optimizer_compare_latest.csv`
- `backtests/btc_optimizer_runs/`

## Interpretation guide

Do not select by total return alone. Prefer candidates that pass both windows:

- 5y return is strong
- 3y return stays positive
- max drawdown is survivable
- Profit Factor is not fragile
- Calmar and robust score rank near the top

A high-return 5x–10x setting with 70–95% drawdown should remain research-only.
