# VWAP Value-Zone v1.2 — TOP10 / isolated 3x / 30 USDT

기존 VWAP 실전 엔진에서 확인된 두 가지 문제를 교정한 버전이다.

1. hedge mode에서 상단 숏과 하단 롱 주문을 동시에 대기시키면 급격한 왕복 구간에서 두 주문이 모두 체결될 수 있었다.
2. 실전에서 확인된 상·하단 간격이 너무 좁아 VWAP 회귀 수익이 수수료와 슬리피지에 비해 작았다.

## VWAP을 사용하는 방식

VWAP은 단순한 매수·매도 선이 아니라 실제 체결량으로 가중된 시장 참여자의 평균 비용대다. 이 버전은 다음 두 기준을 함께 사용한다.

- **60분 rolling VWAP:** 현재 시장의 단기 공정가치
- **UTC session VWAP:** 하루 동안 형성된 느린 비용 기준

평균회귀 주문은 rolling VWAP이 급하게 기울지 않고, rolling VWAP과 session VWAP의 차이가 크지 않으며, ADX가 20 이하일 때만 허용된다.

## 밴드

한쪽 밴드의 VWAP 대비 거리는 다음 중 가장 큰 값이다.

```text
2.25 × 체결량 가중 표준편차
1.35 × 1분 ATR%
0.30% 경제적 최소 간격
```

최소 상·하단 총 간격은 약 0.60%다. 필요한 반폭이 1.00%를 넘으면 횡보장이 아니라 변동성 확대 구간으로 보고 신규 진입을 중단한다.

## 이중 체결 방지

종목당 진입 주문은 항상 한쪽만 존재한다.

```text
가격이 하단 밴드에 가까움 → LONG 지정가만 대기
가격이 상단 밴드에 가까움 → SHORT 지정가만 대기
중앙부 → 진입 주문 없음
```

방향을 전환할 때는 기존 주문의 최종 상태가 `canceled`, `filled` 등으로 확인되기 전에는 반대 주문을 만들지 않는다. 주문 취소 후에도 다음 계정 스냅샷까지 기다린다.

만약 거래소에서 동일 종목 LONG·SHORT가 동시에 발견되면:

```text
자동 DISARM
해당 종목의 모든 미체결 주문 취소
LONG 시장가 청산
SHORT 시장가 청산
텔레그램 경고
```

## 주문 금액

`order_notional_usdt = 30`이다.

이는 **증거금 30 USDT가 아니라 포지션 명목가치 약 30 USDT**다. isolated 3x에서는 종목당 필요한 증거금이 대략 10 USDT다. 거래소 최소수량과 수량 단위 때문에 BTC·ETH·BNB 등은 실제 명목가치가 30 USDT보다 조금 커질 수 있다.

## 기존 청산 규칙

- 고정 TP: 진입가 대비 +0.6%
- 고정 SL: 진입가 대비 -0.3%
- rolling VWAP 복귀 시 시장가 청산
- 5초간 0.15% 급변 시 진입 주문 취소 후 10분 대기

## 설치 전 필수 상태

설치기는 다음 조건이 아니면 중단한다.

```text
VWAP 프로세스 없음
거래소 포지션 0
일반 미체결 주문 0
TP/SL·트리거 주문 0
```

현재 포지션이 있다면 먼저 신규 진입을 중단하고 앱에서 포지션을 정리해야 한다.

```bash
cd ~/index-sniper-pro
bash disarm_vwap_video_live.sh
bash cancel_vwap_video_entries.sh
bash stop_vwap_video_live.sh
```

## 설치 후 순서

```bash
bash refresh_vwap_video_top10.sh
bash doctor_vwap_value_zone_v12.sh

bash arm_vwap_value_zone_v12.sh \
  START_VWAP_VIDEO_TOP10_LIVE_3X \
  I_CONFIRM_REAL_ORDERS_30USDT_NOTIONAL \
  API_HAS_NO_WITHDRAW_PERMISSION \
  DEDICATED_SUBACCOUNT_ONLY

bash start_vwap_value_zone_v12.sh
```

상태와 로그:

```bash
bash status_vwap_value_zone_v12.sh
tail -f logs/vwap-video-live-v1.log
```

첫 시작 또는 재시작 후에는 rolling VWAP을 다시 구성하므로 10분 워밍업이 진행된다.
