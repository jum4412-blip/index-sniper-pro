# index-sniper-pro v1.0

Bitget UTA 전용 자동매매 프로젝트입니다.

## v1.0 변경점

- SP500USDT / NDX100USDT / BTCUSDT 고정 운용
- Larry Williams 변동성 돌파 + EMA 추세 필터 + ATR TP/SL
- 기본값은 `DRY_RUN=true`로 실주문 없음
- 실제 자동매매는 4중 안전문구가 모두 있어야 실행
- HOLD 반복 알림 없음, 신호/주문/오류/heartbeat만 텔레그램 전송
- Daily equity loss guard: 하루 손실 한도 도달 시 신규 진입 차단
- 실전 시작용 `start_live_guarded.sh` 추가

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

처음 실전은 `CAPITAL_RATIO=0.01` 또는 `0.03`처럼 작게 시작하는 것을 권장합니다.

```env
DRY_RUN=false
LIVE_TRADING_ENABLED=true
LEVERAGE=5
CAPITAL_RATIO=0.01
MAX_LIVE_CAPITAL_RATIO=0.10
MAX_ORDER_NOTIONAL_USDT=250
MAX_DAILY_LOSS_PCT=1.50
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

주의: `stop_sniper.sh`는 봇 프로세스만 멈춥니다. 거래소에 이미 열린 포지션이 있으면 Bitget 앱/웹에서 직접 확인해야 합니다.
