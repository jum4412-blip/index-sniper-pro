# Index Sniper Pro v3.0 Larry Pure No-MA Daily Reset Backtest Patch

## 목적

v2.x의 진입은 No-MA 변동성 돌파로 단순해졌지만, 청산은 ATR TP/SL이라서 전날 포지션과 다음날 반대 신호가 충돌할 수 있었습니다.

v3.0 백테스트는 래리 윌리엄스식으로 더 단순하게 봅니다.

```text
진입 = 오늘 시가 ± 전일 변동폭 × K
이동평균 = 사용 안 함
ATR TP/SL = 사용 안 함
익절 = 다음날 UTC 00:00 / KST 09:00 시가 시간청산
손절 후보 = 당일 시가 복귀 방식 비교
```

## 백테스트에 들어간 3가지 청산 모드

1. `next_open`
   - 가장 순수한 래리식 daily reset 버전입니다.
   - 고정 손절/익절 없이 다음날 KST 09:00 시가에 전량 청산합니다.

2. `open_stop_conservative`
   - 롱은 당일 시가 아래 터치, 숏은 당일 시가 위 터치 시 당일 시가에서 손절한 것으로 봅니다.
   - 일봉 데이터만으로는 진입 후에 시가를 터치했는지, 진입 전에 터치했는지 알 수 없어서 매우 보수적인 가정입니다.

3. `close_fail`
   - 롱은 종가가 당일 시가 아래면 실패 청산, 숏은 종가가 당일 시가 위면 실패 청산합니다.
   - 일봉 데이터의 순서 문제를 줄인 절충형입니다.

## 적용 방법

GitHub에 ZIP 내용물을 업로드/커밋한 뒤 EC2에서:

```bash
cd ~/index-sniper-pro
source .venv/bin/activate

git pull
chmod +x apply_v30_larry_pure_backtest.sh
bash apply_v30_larry_pure_backtest.sh

python -m py_compile index_sniper/backtest/larry_pure.py
```

## 기본 백테스트 실행

```bash
bash run_v30_larry_pure_btc_sweep.sh
```

결과 보기:

```bash
cat backtests/v30_larry_pure/larry_pure_sweep_latest.txt
```

## K값 비교

```bash
bash run_v30_larry_pure_btc_k_sweep.sh
```

결과 보기:

```bash
cat backtests/v30_larry_pure/larry_pure_k_sweep_latest.txt
```

## 상세 5년 / 5배 / K=0.5 결과

```bash
bash run_v30_larry_pure_btc_detail.sh
```

결과 파일:

```text
backtests/v30_larry_pure/larry_pure_summary_latest.txt
backtests/v30_larry_pure/larry_pure_trades_latest.csv
backtests/v30_larry_pure/larry_pure_equity_latest.csv
backtests/v30_larry_pure/larry_pure_signals_latest.csv
```

## 주의

이 패치는 실전 자동매매 로직을 바로 바꾸지 않습니다. 먼저 백테스트 전용으로 v3.0 구조를 검증하기 위한 패치입니다.

백테스트가 v2.x보다 낫거나, 최소한 MDD/손실연속/월별 손실이 더 납득 가능할 때 실전 엔진에 v3.0 exit mode를 붙이는 순서가 안전합니다.
