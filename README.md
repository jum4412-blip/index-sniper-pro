# Index Sniper Pro v2.1 — Survival Momentum + Backtest

실전 봇의 철학은 그대로 유지한다.

- 메인: 추세추종 / 타임시리즈 모멘텀
- 진입 방아쇠: 래리 윌리엄스식 변동성 돌파
- 리스크: ATR 손절·익절, 일일 손실 제한
- 운영: SP500/NDX 주말 미보유, BTC는 24시간
- 추가 방어: Anti-Chase Filter, 지수 상관 그룹 제한, Position Manager

## v2.1 추가: Backtest Engine

3년 또는 5년치 과거 차트로 현재 전략의 근사 백테스트를 수행한다.

데이터 기본값:

- BTCUSDT: Yahoo `BTC-USD`
- SP500USDT: Yahoo `ES=F` → `^GSPC`, Stooq `^spx` fallback
- NDX100USDT: Yahoo `NQ=F` → `^NDX`, Stooq `^ndx` fallback

중요한 한계:

- 백테스트는 Bitget 실제 체결·호가·펀딩비를 완벽하게 재현하지 않는다.
- 일봉 OHLC 기반이라 같은 날 TP/SL이 모두 닿으면 보수적으로 SL 먼저로 처리한다.
- SP500/NDX는 Bitget 상품 히스토리가 짧기 때문에 외부 선물/지수 데이터를 proxy로 사용한다.
- 따라서 결과는 “전략 검증용 근사치”이지 수익 보장이 아니다.

## 설치

```bash
cd ~/index-sniper-pro
git pull
bash install.sh
```

## 3년 백테스트

```bash
cd ~/index-sniper-pro
bash run_backtest_3y.sh
```

## 5년 백테스트

```bash
cd ~/index-sniper-pro
bash run_backtest_5y.sh
```

데이터를 다시 받고 싶으면:

```bash
bash run_backtest_5y.sh --refresh
```

## 결과 보기

```bash
bash view_backtest.sh
```

결과 파일:

- `backtests/backtest_summary_latest.txt`
- `backtests/backtest_summary_latest.json`
- `backtests/equity_curve_latest.csv`
- `backtests/trades_latest.csv`
- `backtests/signals_latest.csv`
- `backtests/data/*.csv`

## 실전 봇 점검

```bash
bash status_sniper.sh
bash view_observer.sh
bash run_position_manager.sh
```

## 실전 시작

실전은 백테스트와 별도다. 실전 시작 전에는 반드시 포지션과 미체결 주문을 Bitget 앱에서 확인한다.

```bash
bash stop_sniper.sh
bash run_check.sh
bash run_live_preflight.sh
bash run_weekend_flat_check.sh
bash run_position_manager.sh
bash start_live_guarded.sh
bash status_sniper.sh
```
