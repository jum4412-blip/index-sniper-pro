# Index Sniper Pro v3.2 Larry First-Touch + Fail-Exit Backtest Patch

백테스트 전용 패치입니다. 실전 봇 주문 로직은 바꾸지 않습니다.

## 목적

v3.1 Larry Pure first-touch가 `TP/SL 없음 + 다음날 09:00 청산`에서 무너졌기 때문에, 가짜 돌파를 조기에 정리하는 가격 기반 청산을 검증합니다.

## 규칙

- 진입: UTC 00:00 / KST 09:00 일봉 기준 `오늘 시가 ± 전일 변동폭 × K`
- 체결: 1H 캔들 기준 먼저 닿은 방향만 진입
- 지표: 없음
- 고정 TP: 없음
- 시간청산: 다음 UTC 00:00 / KST 09:00
- 실패청산:
  - `target_reclaim_close`: 롱은 1H 종가가 롱타겟 아래로 복귀하면 청산, 숏은 1H 종가가 숏타겟 위로 복귀하면 청산
  - `day_open_reclaim_close`: 롱은 1H 종가가 당일 시가 아래로 복귀하면 청산, 숏은 1H 종가가 당일 시가 위로 복귀하면 청산
  - `next_open`: 실패청산 없음, v3.1과 같은 기준 비교용

## 적용

```bash
cd ~/index-sniper-pro
source .venv/bin/activate

git pull
chmod +x apply_v32_larry_fail_exit_backtest.sh
bash apply_v32_larry_fail_exit_backtest.sh
```

## 실행 순서

### 1) 실패청산 방식 비교

```bash
bash run_v32_larry_fail_exit_btc_exit_sweep.sh
cat backtests/v32_larry_fail_exit/larry_fail_exit_exit_sweep_latest.txt
```

### 2) K값 비교

```bash
bash run_v32_larry_fail_exit_btc_k_sweep.sh
cat backtests/v32_larry_fail_exit/larry_fail_exit_k_sweep_latest.txt
```

### 3) 레버리지 비교

```bash
bash run_v32_larry_fail_exit_btc_sweep.sh
cat backtests/v32_larry_fail_exit/larry_fail_exit_sweep_latest.txt
```

### 4) 시드 투입비율 30% / 70% / 100% 비교

```bash
bash run_v32_larry_fail_exit_btc_capital_sweep.sh
cat backtests/v32_larry_fail_exit/larry_fail_exit_capital_sweep_latest.txt
```

### 5) 상세 결과

```bash
bash run_v32_larry_fail_exit_btc_detail.sh
cat backtests/v32_larry_fail_exit/larry_fail_exit_summary_latest.txt
```

## 자주 바꾸는 환경변수

```bash
BT_V32_K=0.5
BT_V32_LEVERAGE=5
BT_V32_CAPITAL_RATIO=0.30
BT_V32_EXIT_MODE=target_reclaim_close
BT_V32_SAME_CANDLE_MODE=skip
BT_V32_CAPITAL_RATIOS=0.30,0.70,1.00
```

예시:

```bash
BT_V32_K=0.35 BT_V32_EXIT_MODE=target_reclaim_close bash run_v32_larry_fail_exit_btc_capital_sweep.sh
```
