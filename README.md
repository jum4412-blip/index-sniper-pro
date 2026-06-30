# index-sniper-pro v0.8

## v0.8 변경점

- HOLD 상태 텔레그램 알림 기본 OFF
- 루프 시작 알림 1회
- 신호 발생 / 주문 예정 / 주문 실행 / 오류만 알림
- 하트비트는 `STRATEGY_HEARTBEAT_MINUTES`마다 1회
- 중복 screen 정리 스크립트 추가

## 알림 정책 기본값

```env
NOTIFY_HOLD_SUMMARY=false
NOTIFY_LOOP_START=true
NOTIFY_HEARTBEAT=true
NOTIFY_SIGNAL=true
NOTIFY_ERROR=true
NOTIFY_BLOCKED_SIGNAL=true
STRATEGY_HEARTBEAT_MINUTES=60
LOOP_SECONDS=300
```

## 기존 루프 중지

```bash
bash stop_sniper.sh
```

## 조용한 드라이런 루프 시작

```bash
bash start_exec_dry_quiet.sh
```

## 로그 보기

```bash
bash view_log.sh
```

`DRY_RUN=true` 상태에서는 실주문이 나가지 않습니다.
