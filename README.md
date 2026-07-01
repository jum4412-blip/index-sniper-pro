# index-sniper-pro v1.1 SURVIVAL

Bitget UTA 전용 자동매매 프로젝트입니다. 이 버전은 “1등보다 생존”을 선택한 운용 규칙을 기본값으로 둡니다.

## v1.1 핵심 변경점

- SP500USDT / NDX100USDT / BTCUSDT 고정 운용
- Larry Williams 변동성 돌파 + EMA 추세 필터 + ATR TP/SL
- SURVIVAL risk profile 기본 적용
- SP500USDT와 NDX100USDT를 같은 미국지수 위험 버킷으로 취급
- 미국지수 버킷은 동시에 1개 포지션만 허용
- 전체 오픈 포지션 최대 2개
- 한 사이클 신규 진입 최대 1개
- 하루 심볼당 신규 진입 1회
- 돌파선 1틱 터치가 아니라 ATR 0.05 이상 추가 돌파 확인
- 여러 신호가 동시에 나오면 survival score가 가장 높은 1개만 선택
- Daily equity loss guard 기본 -1.00%
- 기본값은 DRY_RUN=true로 실주문 없음

## 설치

```bash
bash install.sh
```

## 기본 확인

```bash
bash run_check.sh
bash run_preflight.sh
bash run_strategy_exec_dry.sh
```

## 드라이런 루프

```bash
bash start_exec_dry.sh
bash status_sniper.sh
```

## 실전 전용 프리플라이트

실주문 없이 전략 엔진을 강제로 드라이런으로 점검합니다.

```bash
bash run_live_preflight.sh
```

## 실전 자동매매 시작 전 필수 .env

처음 실전은 아래처럼 보수적으로 시작합니다.

```env
DRY_RUN=false
LIVE_TRADING_ENABLED=true
LEVERAGE=5
CAPITAL_RATIO=0.10
RISK_PROFILE=SURVIVAL
MAX_OPEN_POSITIONS=2
MAX_NEW_POSITIONS_PER_CYCLE=1
MAX_DAILY_ENTRIES_PER_SYMBOL=1
SURVIVAL_CORRELATED_GROUP=SP500USDT,NDX100USDT
SURVIVAL_MAX_CORRELATED_OPEN=1
SURVIVAL_MAX_LIVE_OPEN_POSITIONS=2
SURVIVAL_SELECT_BEST_SIGNAL=true
SURVIVAL_MIN_BREAKOUT_ATR=0.05
MAX_LIVE_CAPITAL_RATIO=0.10
MAX_ORDER_NOTIONAL_USDT=250
MAX_DAILY_LOSS_PCT=1.00
SYMBOLS=SP500USDT,NDX100USDT,BTCUSDT
STRATEGY_LIVE_CONFIRM=I_UNDERSTAND_AUTO_TRADING
LIVE_START_CONFIRM=START_LIVE_INDEX_SNIPER
```

실전 시작:

```bash
bash stop_sniper.sh
bash reset_equity_guard.sh
bash run_live_preflight.sh
bash start_live_guarded.sh
bash status_sniper.sh
```

중지:

```bash
bash stop_sniper.sh
```

주의: stop_sniper.sh는 봇 프로세스만 멈춥니다. 거래소에 이미 열린 포지션이 있으면 Bitget 앱/웹에서 직접 확인해야 합니다.


## v1.2 HOTFIX: Bitget UTA hedge-mode close fix

Bitget UTA hedge-mode에서는 청산 주문에 `posSide`와 `reduceOnly`를 동시에 보내면 오류 25238이 발생할 수 있다.
따라서 v1.2부터 hedge-mode 청산은 다음 형태로 보낸다.

- Long 청산: `side=sell`, `posSide=long`
- Short 청산: `side=buy`, `posSide=short`
- `reduceOnly`는 hedge-mode 청산 payload에서 제외

마이크로 테스트 중 청산 실패가 있었으면 먼저 Bitget 앱에서 포지션을 확인한다.
포지션이 남아 있으면 아래 명령으로 BTCUSDT 긴급 청산을 시도할 수 있다.

```bash
DRY_RUN=false EMERGENCY_CLOSE_CONFIRM=I_UNDERSTAND_CLOSE_POSITION EMERGENCY_CLOSE_SYMBOL=BTCUSDT bash run_emergency_close.sh
```

그 다음 마이크로 테스트를 다시 실행한다.

```bash
DRY_RUN=false LIVE_TEST_CONFIRM=I_UNDERSTAND_REAL_ORDER LIVE_TEST_SYMBOL=BTCUSDT bash run_micro_live_test.sh
```
