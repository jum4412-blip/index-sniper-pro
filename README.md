# v2.7 Daily Target Alert Patch

Adds Telegram alerts for the current daily volatility breakout targets.

- Manual send: `bash run_daily_targets.sh --force`
- Once-per-day loop: `bash start_daily_target_alerts.sh`
- Stop loop: `bash stop_daily_target_alerts.sh`

The alert shows:
- current price
- long target and distance
- short target and distance
- previous high/low/range
- current day open
- ATR
- expected SL/TP if entry triggers

Default state file: `data/daily_target_alert_state.json`
