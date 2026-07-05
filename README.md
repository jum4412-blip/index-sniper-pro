# v2.8 BTC UTC 00:00 / KST 09:00 Daily Candle Patch

이 패치는 BTCUSDT 일봉 기준을 `UTC 00:00 = KST 09:00`으로 고정합니다.

반영 내용:
- BTCUSDT 실전 신호 계산에서 거래소 1D 봉 대신 1H 봉을 UTC 00:00 기준으로 재집계
- daily target alert의 기준봉 표시를 `UTC / KST` 둘 다 표시
- No-MA 변동성 돌파 타겟이 KST 오전 9시 기준으로 계산됨

업로드 후 EC2:

```bash
cd ~/index-sniper-pro
git pull
source .venv/bin/activate
chmod +x apply_btc_utc_day_kst09.sh
bash apply_btc_utc_day_kst09.sh
python -m py_compile index_sniper/strategy/utc_daily.py index_sniper/strategy_executor.py index_sniper/daily_targets.py
bash run_daily_targets.sh --force
```

정상 알림 예시:

```text
일봉 기준: UTC 00:00 = KST 09:00
BTCUSDT 기준봉: 2026-07-05 00:00 UTC / 2026-07-05 09:00 KST
```
