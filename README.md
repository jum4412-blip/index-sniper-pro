# v4.2 BTC Quant Observer Upgrade

Observation-only upgrade for BTC Quant v4.1.

Changes:
- Dynamic trend score haircut when short-term momentum contradicts the old trend score.
- Short-pressure logic: price down + positive funding + OI increase can push score lower.
- Lower observation thresholds for short side: WEAK_SHORT -30, STRONG_SHORT -55.
- 2-run confirmation before Telegram alert.
- Signal forward-performance tracking at 1h/4h/12h/24h.
- No orders. No live execution changes.

Commands:

```bash
chmod +x apply_v42_quant_observer.sh
bash apply_v42_quant_observer.sh
python -m py_compile index_sniper/v42_quant_observer.py
bash run_quant_v42_once.sh
bash start_quant_v42.sh
bash status_quant_v42.sh
bash view_quant_v42_log.sh
bash summarize_quant_v42.sh
```
