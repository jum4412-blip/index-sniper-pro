# index-sniper-pro

고정 프로젝트: `index-sniper-pro`

## v0.4 목표

- Bitget UTA 연결 확인
- SP500USDT / NDX100USDT / BTCUSDT 대상 고정
- 목표 레버리지 5배 / crossed 여부 확인
- 가용 USDT 기준 10% 배분 수량 계산 확인
- 실주문 전 PRE-FLIGHT 점검
- 안전장치가 걸린 BTCUSDT 최소 실주문 테스트 스크립트 추가

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

`run_check.sh`, `run_dry_order.sh`, `run_preflight.sh`는 실주문을 넣지 않는다.

## 실제 micro order test

이 스크립트는 실제 시장가 주문을 넣는다. 기본 추천 심볼은 `BTCUSDT`다.

```bash
DRY_RUN=false LIVE_TEST_CONFIRM=I_UNDERSTAND_REAL_ORDER LIVE_TEST_SYMBOL=BTCUSDT bash run_micro_live_test.sh
```

동작:

1. BTCUSDT 레버리지/마진모드 확인
2. 기존 포지션이 있으면 중단
3. 최소 수량 시장가 LONG 진입
4. 2초 대기
5. reduceOnly 시장가 청산
6. 텔레그램 알림

## 중요

`.env`는 절대 GitHub에 올리지 않는다.

실전 전략은 v1.0 이후에 붙인다. v0.4는 거래 엔진 검증 단계다.
