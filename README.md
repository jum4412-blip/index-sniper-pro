# index-sniper-pro v0.7

고정 프로젝트: `index-sniper-pro`

## v0.7 목표

v0.7은 **전략 신호를 실제 주문 payload로 변환하는 실행 엔진** 단계입니다. 기본값은 `DRY_RUN=true`라서 실주문은 없습니다.

v0.7에서 추가된 것:

- SP500USDT / NDX100USDT / BTCUSDT 신호 계산
- 1D EMA60 부족 심볼은 4H EMA50/200 warm-up 사용
- 계좌 10% / 3개 종목 분산 / 5배 레버리지 기준 수량 계산
- 현재 레버리지/마진모드/기존 포지션 확인
- 거래 허용 여부 체크
- 실주문용 payload 생성
- Bitget UTA preset `takeProfit`, `stopLoss` 포함
- 로그 저장: `logs/events.jsonl`, `logs/trades.csv`
- 상태 저장: `data/strategy_state.json`

## 전략 기준

### 돌파 가격

```text
전일 Range = 전일 고가 - 전일 저가
롱 기준가 = 오늘 시가 + 전일 Range × K_VALUE
숏 기준가 = 오늘 시가 - 전일 Range × K_VALUE
```

기본값은 `K_VALUE=0.50`입니다.

### 추세 필터

- BTCUSDT: 1D EMA20 / EMA60
- SP500USDT, NDX100USDT: 1D EMA60이 부족하면 4H EMA50 / EMA200 warm-up

롱은 상승 추세에서 상단 돌파가 필요하고, 숏은 하락 추세에서 하단 돌파가 필요합니다.

### 손절 / 익절

```text
롱 손절 = 현재가 - ATR × ATR_STOP_MULT
롱 익절 = 현재가 + ATR × ATR_TAKE_PROFIT_MULT

숏 손절 = 현재가 + ATR × ATR_STOP_MULT
숏 익절 = 현재가 - ATR × ATR_TAKE_PROFIT_MULT
```

기본값:

```text
ATR_STOP_MULT=1.30
ATR_TAKE_PROFIT_MULT=2.00
```

v0.7은 Bitget UTA `place-order` 요청에 `takeProfit`, `stopLoss` preset 값을 함께 넣습니다.

## 설치

```bash
cd ~/index-sniper-pro
git pull
bash install.sh
```

## 기본 점검

```bash
bash run_check.sh
bash run_preflight.sh
bash run_strategy_dry.sh
```

## v0.7 실행 드라이런

```bash
bash run_strategy_exec_dry.sh
```

루프 실행:

```bash
screen -S sniper-exec-dry
bash run_strategy_exec_loop.sh
```

빠져나오기:

```text
Ctrl + A, D
```

다시 보기:

```bash
screen -r sniper-exec-dry
```

## 실제 전략 자동매매 시작 조건

실제 자동매매는 아래 2개가 `.env`에 있어야만 실행됩니다.

```env
DRY_RUN=false
STRATEGY_LIVE_CONFIRM=I_UNDERSTAND_AUTO_TRADING
```

실제 루프 시작:

```bash
screen -S sniper-live
bash run_strategy_live_loop.sh
```

## 안전장치

- 기본은 `DRY_RUN=true`
- 레버리지 5배가 아니면 주문 차단
- crossed가 아니면 주문 차단
- 해당 심볼 포지션이 있으면 신규 주문 차단
- 하루 심볼당 기본 1회만 진입
- 한 사이클 신규 진입 기본 1개
- 전체 오픈 포지션 최대 3개
- `.env`는 GitHub에 올리지 않음
