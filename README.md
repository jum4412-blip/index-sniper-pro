# index-sniper-pro v1.7 Weekend Flat

고정 프로젝트: `index-sniper-pro`

## v1.7 핵심

- 기존 전략/진입 기준은 유지
- SP500USDT / NDX100USDT 주말 보유 금지 규칙 추가
- BTCUSDT는 주말 규칙 제외
- 금요일 뉴욕시간 15:30 이후 지수 신규 진입 차단
- 금요일 뉴욕시간 16:30 이후 지수 포지션 자동 청산 시도
- 토요일 전체 / 일요일 뉴욕시간 18:30 전까지 지수 신규 진입 차단
- `.env`를 10% 생존형 운용으로 바꾸는 `apply_live_10pct.sh` 추가
- `run_weekend_flat_check.sh`로 현재 주말 플랫 상태 확인

## 중요한 원칙

기존 포지션은 사용자가 수동으로 정리한 뒤 업데이트/재시작한다.
`DRY_RUN=false` 상태에서는 실제 주문 가능 상태다.
실전 전에는 반드시 `run_live_preflight.sh`를 통과시킨다.

## 설치/업데이트

```bash
cd ~/index-sniper-pro
git pull
bash install.sh
```

## 10% 운용 설정

기존 포지션을 정리한 뒤 실행한다.

```bash
cd ~/index-sniper-pro
bash apply_live_10pct.sh
```

확인:

```bash
grep -E 'DRY_RUN|LIVE_TRADING_ENABLED|CAPITAL_RATIO|MAX_ORDER_NOTIONAL_USDT|MAX_DAILY_LOSS_PCT|INDEX_WEEKEND' .env
```

## 주말 플랫 상태 확인

```bash
bash run_weekend_flat_check.sh
```

## 실전 전 점검

```bash
bash stop_sniper.sh
bash run_check.sh
bash run_live_preflight.sh
bash run_weekend_flat_check.sh
```

## 실전 시작

```bash
bash reset_equity_guard.sh
bash start_live_guarded.sh
bash status_sniper.sh
```

## 종료

```bash
bash stop_sniper.sh
```

## 주의

`stop_sniper.sh`는 봇만 정지한다. 이미 열린 포지션은 사용자가 Bitget 앱에서 확인해야 한다.
주말 플랫 기능은 v1.7부터 지수 포지션 자동 정리를 시도하지만, 실전에서는 Bitget 앱에서 포지션/미체결 주문을 최종 확인해야 한다.
