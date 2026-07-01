# index-sniper-pro v0.9

고정 프로젝트: `index-sniper-pro`

## v0.9 목표
- v0.8의 2분 HOLD 알림 방지 유지
- heartbeat 시작 알림 추가
- 1시간 생존알림 강화
- `data/loop_status.json`으로 루프 상태 저장
- `logs/heartbeat.log` 저장
- watchdog wrapper로 루프가 죽으면 자동 재시작
- 기본 DRY_RUN=true, 실주문 없음

## 실행

```bash
bash stop_sniper.sh
bash test_heartbeat.sh
bash start_exec_dry.sh
bash status_sniper.sh
```

## 로그

```bash
tail -f logs/sniper-exec-dry.log
```

나가기: `Ctrl + C`

## 안전
실거래 전에는 반드시 다음이 유지되어야 함.

```env
DRY_RUN=true
```
