# index-sniper-pro

고정 프로젝트: `index-sniper-pro`

## v0.6 목표

v0.5에서 SP500USDT / NDX100USDT가 1일봉 60개 미만이라 `not enough candles` 오류가 발생했다. v0.6은 이 문제를 해결하기 위해 **Adaptive Warm-up Mode**를 추가한다.

## v0.6 핵심

- BTCUSDT는 1D EMA20/60 정상 사용
- SP500USDT / NDX100USDT처럼 1D EMA60이 아직 부족한 심볼은:
  1. 4H EMA50/200으로 추세 필터 대체
  2. 4H도 부족하면 1D EMA8/21 임시 필터 사용
  3. 데이터가 너무 부족하면 ERROR가 아니라 HOLD 처리
- 변동성 돌파 기준은 기존과 동일하게 1D 기준:
  - 롱 기준가 = 오늘 시가 + 전일 Range × K
  - 숏 기준가 = 오늘 시가 - 전일 Range × K
- ATR14가 부족하면 가능한 최근 TR을 사용하되, 최소 10개 미만이면 HOLD
- Warm-up 모드 심볼은 포지션 크기를 `FALLBACK_SIZE_MULTIPLIER`만큼 축소
- `DRY_RUN=true`에서만 전략 신호 확인
- 실전 자동 주문은 아직 붙이지 않음

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

정상 출력 예시:

```text
v0.6 STRATEGY_DRY 완료
실주문 없음
SP500USDT: HOLD / ... / trend 4H_EMA50/200_WARMUP(200+) / size x0.5
NDX100USDT: HOLD / ... / trend 4H_EMA50/200_WARMUP(200+) / size x0.5
BTCUSDT: HOLD / ... / trend 1D_EMA20/60 / size x1.0
```

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

v0.6은 상장 초기 데이터 부족 문제를 해결하는 전략 드라이런 버전이다. 실전 전략 주문은 v0.7 이후에 붙인다.
