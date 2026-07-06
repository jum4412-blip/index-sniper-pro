# Index Sniper Pro v3.3 Larry First-Touch + Trend Filter + ATR Exit Backtest

실전 봇 변경 패치가 아니라 **백테스트 전용 패치**입니다.

## 목적
v3.1/v3.2 Larry first-touch 계열이 단독으로는 약했기 때문에, 처음 만들었던 추세추종 아이디어를 **진입 신호가 아니라 방향 필터**로 넣어 검증합니다.

## 규칙

- 기준봉: UTC 00:00 = KST 09:00
- 진입: 1H 캔들 기준 `오늘 시가 ± 전일 변동폭 × K` 중 먼저 닿은 쪽
- 추세필터: 이전에 확정된 추세 캔들만 사용
  - 추세 LONG이면 LONG 돌파만 허용
  - 추세 SHORT이면 SHORT 돌파만 허용
  - 추세 중립이면 거래 안 함
- 청산: ATR 기반 SL/TP
  - 기본 SL = ATR × 1.30
  - 기본 TP = ATR × 2.00
  - 둘 다 안 닿으면 다음 UTC 00:00 / KST 09:00 청산
- 기본 same-candle 처리: `skip`
- 기본 exit same-candle 처리: `stop_first` 보수 가정

## 추세필터 프로필

- `none`: 추세필터 없음. v2 No-MA ATR을 1H first-touch 방식으로 다시 보는 기준선.
- `1H_20_60`
- `1H_50_200`
- `4H_20_60`
- `4H_50_200`
- `1H_20_60+4H_20_60`: 두 추세가 같은 방향일 때만 진입
- `1H_50_200+4H_50_200`: 두 추세가 같은 방향일 때만 진입

## 적용

```bash
cd ~/index-sniper-pro
source .venv/bin/activate

git pull
chmod +x apply_v33_larry_trend_filter_backtest.sh
bash apply_v33_larry_trend_filter_backtest.sh
```

## 실행 순서

### 1) 추세필터 비교

```bash
bash run_v33_larry_trend_filter_btc_trend_sweep.sh
cat backtests/v33_larry_trend_filter/larry_trend_filter_trend_sweep_latest.txt
```

### 2) 선택한 추세필터로 K값 비교

기본은 `4H_20_60`입니다.

```bash
BT_V33_TREND_PROFILE=4H_20_60 \
bash run_v33_larry_trend_filter_btc_k_sweep.sh
cat backtests/v33_larry_trend_filter/larry_trend_filter_k_sweep_latest.txt
```

### 3) 레버리지 비교

```bash
BT_V33_TREND_PROFILE=4H_20_60 BT_V33_K=0.5 \
bash run_v33_larry_trend_filter_btc_sweep.sh
cat backtests/v33_larry_trend_filter/larry_trend_filter_sweep_latest.txt
```

### 4) 시드 30% / 70% / 100% 비교

```bash
BT_V33_TREND_PROFILE=4H_20_60 BT_V33_K=0.5 BT_V33_LEVERAGE=5 \
bash run_v33_larry_trend_filter_btc_capital_sweep.sh
cat backtests/v33_larry_trend_filter/larry_trend_filter_capital_sweep_latest.txt
```

### 5) 상세 결과

```bash
BT_V33_TREND_PROFILE=4H_20_60 BT_V33_K=0.5 BT_V33_LEVERAGE=5 BT_V33_CAPITAL_RATIO=0.30 \
bash run_v33_larry_trend_filter_btc_detail.sh
cat backtests/v33_larry_trend_filter/larry_trend_filter_summary_latest.txt
```

## 중요

- 실전 봇은 켜지 마세요.
- 먼저 `trend_sweep` 결과에서 `none` 대비 추세필터가 실제로 개선되는지 확인해야 합니다.
- `capital_ratio=0.70`, `1.00`은 수익성 검증 후에만 판단하세요. PF가 1 이하인 전략에 시드를 더 태우면 손실만 빨라집니다.
