# Index Sniper Pro v4.0 BTC OHLCV Multi-Alpha Quant Backtest Patch

실전 봇 파일은 변경하지 않는 백테스트/리서치 전용 패치입니다.

## 핵심 아이디어

v4.0은 Larry/No-MA 단일 규칙을 버리고, 1H OHLCV만으로 여러 작은 알파를 점수화합니다.

- Multi-horizon momentum: 4H, 12H, 24H, 72H 수익률을 변동성으로 정규화
- EMA regime: 빠른 EMA/느린 EMA로 큰 방향 필터
- Volume confirmation: 강한 방향 이동 + 거래량 증가 확인
- Liquidation-like reversal proxy: 급격한 1H 충격 + 거래량 폭증 후 되돌림 점수
- Volatility risk scaling: 변동성 과열 구간에서 점수 축소

펀딩비/OI는 이 버전에 포함하지 않습니다. 즉 5년 OHLCV 차트만으로 살아남는 후보를 먼저 찾는 용도입니다.

## 적용

```bash
cd ~/index-sniper-pro
source .venv/bin/activate

git pull
chmod +x apply_v40_quant_multi_alpha_backtest.sh
bash apply_v40_quant_multi_alpha_backtest.sh
```

## 1. 5년 양수 후보만 탐색

기본값: BTCUSDT / 5년 / capital 30% / leverage 3x / positive-only

```bash
bash run_v40_quant_multi_alpha_btc_search.sh
cat backtests/v40_quant_multi_alpha/quant_multi_alpha_search_latest.txt
```

더 공격적으로 5배도 볼 수 있습니다.

```bash
BT_V40_LEVERAGE=5 bash run_v40_quant_multi_alpha_btc_search.sh
cat backtests/v40_quant_multi_alpha/quant_multi_alpha_search_latest.txt
```

## 2. 후보 하나를 골라 1/2/3/5년 모두 확인

검색 결과 1위의 profile/gate/E/X/H/SL/TP를 환경변수에 넣어서 실행합니다.

예시:

```bash
BT_V40_PROFILE=trend_volume \
BT_V40_TREND_GATE=ema80_240 \
BT_V40_ENTRY_THRESHOLD=55 \
BT_V40_EXIT_THRESHOLD=15 \
BT_V40_MAX_HOLD_BARS=24 \
BT_V40_ATR_STOP_MULT=1.5 \
BT_V40_ATR_TP_MULT=3.0 \
BT_V40_LEVERAGE=3 \
bash run_v40_quant_multi_alpha_btc_robust.sh

cat backtests/v40_quant_multi_alpha/quant_multi_alpha_robust_latest.txt
```

## 3. 30% / 70% / 100% 시드 투입 비교

```bash
BT_V40_PROFILE=trend_volume \
BT_V40_TREND_GATE=ema80_240 \
BT_V40_ENTRY_THRESHOLD=55 \
BT_V40_EXIT_THRESHOLD=15 \
BT_V40_MAX_HOLD_BARS=24 \
BT_V40_ATR_STOP_MULT=1.5 \
BT_V40_ATR_TP_MULT=3.0 \
BT_V40_LEVERAGE=3 \
bash run_v40_quant_multi_alpha_btc_capital_sweep.sh

cat backtests/v40_quant_multi_alpha/quant_multi_alpha_capital_sweep_latest.txt
```

## 4. 상세 결과와 거래내역

```bash
BT_V40_PROFILE=trend_volume \
BT_V40_TREND_GATE=ema80_240 \
BT_V40_ENTRY_THRESHOLD=55 \
BT_V40_EXIT_THRESHOLD=15 \
BT_V40_MAX_HOLD_BARS=24 \
BT_V40_ATR_STOP_MULT=1.5 \
BT_V40_ATR_TP_MULT=3.0 \
BT_V40_LEVERAGE=3 \
BT_V40_CAPITAL_RATIO=0.30 \
bash run_v40_quant_multi_alpha_btc_detail.sh

cat backtests/v40_quant_multi_alpha/quant_multi_alpha_summary_latest.txt
```

## 출력 파일

- `backtests/v40_quant_multi_alpha/quant_multi_alpha_search_latest.txt`
- `backtests/v40_quant_multi_alpha/quant_multi_alpha_search_latest.csv`
- `backtests/v40_quant_multi_alpha/quant_multi_alpha_robust_latest.txt`
- `backtests/v40_quant_multi_alpha/quant_multi_alpha_capital_sweep_latest.txt`
- `backtests/v40_quant_multi_alpha/quant_multi_alpha_summary_latest.txt`
- `backtests/v40_quant_multi_alpha/quant_multi_alpha_trades_latest.csv`
- `backtests/v40_quant_multi_alpha/quant_multi_alpha_equity_latest.csv`

## 주의

`positive-only`는 과거 5년 OHLCV에서 양수였다는 뜻이지 미래 양수를 보장하지 않습니다. 펀딩비/OI가 없는 버전이므로, 이 결과가 살아남으면 다음 단계에서 펀딩비/OI 실시간 관찰 알파를 추가하는 것이 맞습니다.
