# Index Sniper Pro v2.2 — Backtest Decomposition Matrix

이 버전은 v2.1 백테스트 엔진에 **분해 백테스트**를 추가한 버전이다.

목표는 단순히 “전체 수익률”을 보는 것이 아니라, 어떤 조합이 돈을 벌고 어떤 조합이 손실을 만드는지 분리해서 보는 것이다.

## 추가 기능

- BTC only
- SP500 only
- NDX100 only
- BTC + SP500
- BTC + NDX100
- SP500 + NDX100
- BTC + SP500 + NDX100
- SP500/NDX long-only
- BTC long/short + indices long-only
- BTC long-only
- BTC short-only

각 조합별로 아래 지표를 비교한다.

- return_pct
- max_drawdown_pct
- return_over_mdd
- trade_count
- win_rate_pct
- profit_factor
- avg_net_pnl
- max_win_streak
- max_loss_streak
- symbol별 net_pnl

## 중요한 한계

- SP500/NDX는 Bitget 과거 데이터가 짧기 때문에 Yahoo/Stooq 외부 차트를 proxy로 사용한다.
- 일봉 OHLC 기반이라 같은 날 TP/SL이 모두 닿으면 보수적으로 SL 먼저 처리한다.
- 펀딩비, 실제 Bitget 호가, 체결 지연, 실시간 스프레드는 완벽히 재현하지 않는다.
- 이 결과는 수익 보장이 아니라 전략 선별용 근사치다.

## 설치

```bash
cd ~/index-sniper-pro
git pull
bash install.sh
```

## 백테스트 전용 30% 설정 적용

실전 설정과 백테스트 설정은 분리한다. 백테스트만 30%로 돌리고 싶으면:

```bash
bash apply_backtest_30pct.sh
```

확인:

```bash
grep -E 'BT_INITIAL_EQUITY|BT_CAPITAL_RATIO|BT_LEVERAGE|BT_MAX_ORDER_NOTIONAL_USDT|BT_K_VALUE' .env
```

정상 예시:

```text
BT_INITIAL_EQUITY=1374
BT_CAPITAL_RATIO=0.30
BT_LEVERAGE=5
BT_MAX_ORDER_NOTIONAL_USDT=1000
BT_K_VALUE=0.50
```

## 일반 백테스트

```bash
bash run_backtest_3y.sh --refresh
bash run_backtest_5y.sh --refresh
bash view_backtest.sh
```

## 분해 백테스트

3년:

```bash
bash run_backtest_matrix_3y.sh --refresh
```

5년:

```bash
bash run_backtest_matrix_5y.sh --refresh
```

결과 보기:

```bash
bash view_backtest_matrix.sh
```

## 특정 시나리오만 실행

예: BTC only, BTC + SP500, 전체 index long-only만 실행:

```bash
bash run_backtest_matrix_5y.sh --scenarios btc_only_ls,btc_sp500_ls,all_index_long_only
```

## 결과 파일

```text
backtests/backtest_matrix_latest.txt
backtests/backtest_matrix_latest.csv
backtests/backtest_matrix_latest.json
backtests/matrix_runs/<label>/
```

## 실전 봇과 분리

분해 백테스트는 실주문을 전혀 넣지 않는다. 실전 봇은 별도로 `start_live_guarded.sh`로만 실행된다.

실전 봇 상태 확인:

```bash
bash status_sniper.sh
```
