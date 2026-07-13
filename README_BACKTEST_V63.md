# BTC/ETH Quant v6.3.2 백테스트 추가본

이 추가본은 Bitget 공개 과거 캔들을 내려받아 v6.3.2의 BTCUSDT/ETHUSDT TREND·IMPULSE 진입, 5배 위험 크기, 손절·익절, no-followthrough, time stop, ATR trailing, 신호 무효화와 계좌 손실 제한을 5분 단위로 재생합니다.

## 중요한 한계

이 결과는 LIVE 승인서가 아니라 **거절 필터**입니다.

- 과거 뉴스·이벤트 상태는 중립으로 처리합니다.
- 실제 과거 OI 시계열은 재생하지 않습니다.
  - `conservative`: OI 점수 0, IMPULSE는 exceptional tape만 허용
  - `proxy`: 가격·거래량 일치로 약한 OI 대용 점수 사용
- 주문은 미래 정보 사용을 막기 위해 신호 다음 5분봉 시가에 체결합니다.
- 같은 봉에서 TP와 SL이 모두 닿으면 손절이 먼저였다고 가정합니다.
- 기본 비용 가정은 진입·청산 각각 수수료 6bp, 불리한 슬리피지 3bp입니다. 실제 계정 비용에 맞게 바꿀 수 있습니다.
- 펀딩 기록은 혼잡도 점수에만 사용하고 실제 펀딩비 현금흐름은 차감하지 않습니다.
- 실제 주문 API, 서버측 TP/SL 부착, 재시작 복구는 백테스트로 검증되지 않습니다.

## 설치

v6.3.2 live 패치를 먼저 SHADOW 상태로 설치한 다음 프로젝트 루트에서 실행합니다.

```bash
cd ~/index-sniper-pro
bash /path/to/v63_backtest_addon_6.3.2/apply_v63_backtest_addon.sh
```

## 권장 실행

60일, 시드 1,000 USDT 가정, 3개 시나리오:

```bash
cd ~/index-sniper-pro
bash run_v63_backtest.sh 60 1000 suite
```

실행되는 시나리오:

1. `conservative / 1-bar confirmation / fee 6bp / slip 3bp`
2. `proxy / 1-bar confirmation / fee 6bp / slip 3bp`
3. `conservative / 2-bar confirmation / fee 8bp / slip 5bp` 스트레스

처음 데이터 다운로드에는 시간이 걸리지만 이후 실행은 캐시를 사용합니다.

개별 실행:

```bash
bash run_v63_backtest.sh 90 1000 conservative
bash run_v63_backtest.sh 90 1000 proxy
bash run_v63_backtest.sh 90 1000 stress
```

날짜를 직접 정하려면:

```bash
.venv/bin/python -m index_sniper.backtest_v63 \
  --start 2026-04-01 \
  --end 2026-07-01 \
  --initial-equity 1000 \
  --oi-mode conservative \
  --impulse-confirm-bars 1 \
  --fee-bps 6 \
  --slippage-bps 3
```

## 결과

```text
reports/v63_backtest/<scenario>/summary.txt
reports/v63_backtest/<scenario>/summary.json
reports/v63_backtest/<scenario>/trades.csv
reports/v63_backtest/<scenario>/equity_curve.csv
```

최근 결과 모아보기:

```bash
bash report_v63_backtest.sh
```

## 실전 전 판단 기준

백테스트 하나만으로 LIVE를 승인하지 않습니다. 최소한 아래를 먼저 봅니다.

- 60~90일 거래 수가 너무 적지 않은가: 대략 30건 미만이면 판단 보류
- 기본 시나리오 Profit Factor가 1.15 이상인가
- 평균 R이 비용 차감 후 양수인가
- 최대 낙폭이 6% 이내인가
- 스트레스 비용 시나리오가 완전히 붕괴하지 않는가
- BTC·ETH, LONG·SHORT, TREND·IMPULSE 중 한 구간의 우연한 수익에만 의존하지 않는가

통과하더라도 먼저 SHADOW와 소액 Live Canary로 주문 체결·TP/SL 부착·슬리피지를 검증합니다.
