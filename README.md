# Index Sniper Pro v3.1 Larry Pure First-Touch Backtest Patch

## 목적

v3.0 일봉 백테스트는 `both_mode=skip`으로 보수 검증을 했지만, 하루 안에서 롱타겟과 숏타겟 중 어느 쪽이 먼저 닿았는지는 일봉만으로는 알 수 없습니다.

v3.1은 Bitget UTA 1H 캔들을 받아서 다음 구조로 다시 검증합니다.

```text
기준봉 = UTC 00:00 / KST 09:00
진입 = 오늘 시가 ± 전일 변동폭 × K 중 먼저 닿은 쪽
지표 = 없음
고정 TP = 없음
고정 SL = 없음
청산 = 다음 UTC 00:00 / KST 09:00 시가 전량 청산
```

## 모호한 경우

1H 캔들 하나 안에서 롱타겟과 숏타겟이 둘 다 닿으면 순서를 알 수 없습니다. 기본값은 보수적으로 `skip`입니다.

```text
BT_FT_SAME_CANDLE_MODE=skip
```

선택지는 `skip`, `open_distance`, `candle`입니다. 실전 후보 판단은 먼저 `skip`만 보세요.

## 적용

GitHub에 ZIP 내용물을 업로드/커밋한 뒤 EC2에서:

```bash
cd ~/index-sniper-pro
source .venv/bin/activate

git pull
chmod +x apply_v31_larry_first_touch_backtest.sh
bash apply_v31_larry_first_touch_backtest.sh

python -m py_compile index_sniper/backtest/larry_first_touch.py
```

## K값 스윕

```bash
bash run_v31_larry_first_touch_btc_k_sweep.sh
cat backtests/v31_larry_first_touch/larry_first_touch_k_sweep_latest.txt
```

처음 실행은 Bitget 1H 히스토리를 받기 때문에 시간이 걸릴 수 있습니다. 이후에는 CSV 캐시를 사용합니다.

## K=0.5 상세

```bash
BT_FT_K=0.5 BT_FT_SAME_CANDLE_MODE=skip bash run_v31_larry_first_touch_btc_detail.sh
cat backtests/v31_larry_first_touch/larry_first_touch_summary_latest.txt
```

## 5배 K=0.5 레버리지 스윕

```bash
BT_FT_K=0.5 BT_FT_SAME_CANDLE_MODE=skip bash run_v31_larry_first_touch_btc_sweep.sh
cat backtests/v31_larry_first_touch/larry_first_touch_sweep_latest.txt
```

## 출력 파일

```text
backtests/v31_larry_first_touch/larry_first_touch_k_sweep_latest.txt
backtests/v31_larry_first_touch/larry_first_touch_sweep_latest.txt
backtests/v31_larry_first_touch/larry_first_touch_summary_latest.txt
backtests/v31_larry_first_touch/larry_first_touch_trades_latest.csv
backtests/v31_larry_first_touch/larry_first_touch_signals_latest.csv
backtests/v31_larry_first_touch/larry_first_touch_equity_latest.csv
```
