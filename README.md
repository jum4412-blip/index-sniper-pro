# index-sniper-pro

고정 프로젝트: `index-sniper-pro`

## v0.3 목표

- Bitget UTA 연결 확인
- `.env`의 `SYMBOLS` 전체를 대상으로 가격/포지션/심볼 규격 조회
- 계좌 가용 USDT 기준 자동 수량 계산
- `CAPITAL_RATIO=0.10`이면 전체 가용 USDT의 10%를 사용하고, 종목 수만큼 균등 분배
- 목표 레버리지 확인
- 현재 마진모드 확인
- 주문 payload 생성
- `DRY_RUN=true`에서만 동작
- 실주문 없음

## 설치

```bash
cd ~/index-sniper-pro
bash install.sh
```

## 연결 체크

```bash
bash run_check.sh
```

## DRY 주문/수량 체크

```bash
bash run_dry_order.sh
```

또는:

```bash
bash run_order_dry.sh
```

## 중요

`.env`는 절대 GitHub에 올리지 않는다. 실제 주문 테스트 전까지 `DRY_RUN=true` 유지.
