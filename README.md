# v4.1 Quant Data Observer

Research/observer patch only. It does **not** place orders.

Features:
- Fetches BTCUSDT 1H OHLCV
- Fetches Bitget UTA funding history/current funding
- Fetches Bitget current open interest
- Appends snapshots to `data/quant_v41/BTCUSDT_snapshots.csv`
- Computes a simple multi-alpha state score
- Optional Telegram notification using `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`

Apply:

```bash
cd ~/index-sniper-pro
source .venv/bin/activate
chmod +x apply_v41_quant_data_observer.sh
bash apply_v41_quant_data_observer.sh
```

Run once:

```bash
bash run_v41_quant_snapshot.sh
```

Start observer loop:

```bash
BT_V41_LOOP_MINUTES=15 bash start_v41_quant_observer.sh
```

Stop:

```bash
bash stop_v41_quant_observer.sh
```

View snapshots:

```bash
bash view_v41_quant_state.sh
```
