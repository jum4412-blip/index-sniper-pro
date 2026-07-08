# Index Sniper Pro v6.0 BTC Quant Signal Lab

Paper-only signal laboratory. It never sends real orders.

## What it records

- 5m/15m/1H/4H OHLCV features
- Trend, momentum, volume, funding, OI, liquidation proxy, risk scores
- Long/short scores every loop
- Paper entries when scores pass threshold
- Multiple TP/SL paper exit plans simultaneously
- Closed paper-trade outcomes for later analysis

## Files

- `research/signal_lab_snapshots.csv`: every observation
- `research/signal_lab_signals.csv`: paper signal events
- `research/signal_lab_paper_trades.csv`: closed paper trades
- `research/signal_lab_events.jsonl`: full event log
- `research/signal_lab_report_latest.txt`: performance report
- `data/signal_lab_state.json`: active paper trades and funding/OI history

## Commands

```bash
bash apply_v60_signal_lab.sh
python -m py_compile index_sniper/signal_lab.py
bash run_v60_signal_lab_once.sh
bash start_v60_signal_lab.sh
bash status_v60_signal_lab.sh
bash run_v60_signal_lab_report.sh
bash view_v60_signal_lab_files.sh
bash stop_v60_signal_lab.sh
```

## Minimum sample before judging

Do not convert to live trading until at least 50-100 closed paper trades are recorded.
