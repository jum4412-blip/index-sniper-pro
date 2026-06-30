# index-sniper-pro

고정 프로젝트: `index-sniper-pro`

## v0.5 목표

- v0.4까지 검증한 Bitget UTA 연결 / 주문 payload / micro live test 유지
- SP500USDT / NDX100USDT / BTCUSDT 대상 고정
- 래리 윌리엄스식 변동성 돌파 신호 계산
- EMA20/EMA60 추세 필터
- ATR14 기반 손절/익절 가격 계산
- `DRY_RUN=true` 상태에서만 전략 신호와 주문 예정 payload 생성
- 실전 자동매매 주문은 아직 넣지 않음

## 설치

```bash
cd ~/index-sniper-pro
bash install.sh
```

## 기본 체크

```bash
bash run_check.sh
bash run_dry_order.sh
bash run_preflight.sh
```

## 전략 드라이런

```bash
bash run_strategy_dry.sh
```

실주문 없음. 3개 심볼에 대해 현재가, 돌파 목표가, EMA, ATR, 손절/익절 예정가를 계산하고 텔레그램으로 요약한다.

## 전략 드라이 루프

```bash
screen -S sniper-dry
bash run_strategy_loop_dry.sh
```

분리:

```bash
Ctrl + A, D
```

다시 보기:

```bash
screen -r sniper-dry
```

## 실제 micro order test

v0.4에서 사용한 안전장치 포함 실주문 테스트는 유지된다.

```bash
DRY_RUN=false LIVE_TEST_CONFIRM=I_UNDERSTAND_REAL_ORDER LIVE_TEST_SYMBOL=BTCUSDT bash run_micro_live_test.sh
```

## 중요

`.env`는 절대 GitHub에 올리지 않는다.

v0.5는 전략 신호 검증 단계다. 실전 전략 자동 주문은 v0.6 이후에 붙인다.
