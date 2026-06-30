# index-sniper-pro

고정 프로젝트: `index-sniper-pro`

## v0.2 목표

- Bitget UTA API 키 로딩
- UTA 계정/자산 조회
- 심볼별 ticker 조회
- 심볼별 position 조회
- 주문 엔진 payload 생성
- 텔레그램 시작/결과 알림
- **실주문 없음**

## 서버 설치

```bash
cd ~
git clone https://github.com/jum4412-blip/index-sniper-pro.git
cd index-sniper-pro
bash install.sh
```

## .env 만들기

```bash
cp .env.example .env
nano .env
```

`.env`에는 실제 API/텔레그램 값을 넣는다. `.env`는 GitHub에 올리지 않는다.

## CHECK 실행

```bash
bash run_check.sh
```

## DRY ORDER 실행

실주문 없이 주문 payload만 만든다.

```bash
bash run_dry_order.sh
```

## 안전 규칙

v0.2에서는 `DRY_RUN=true`일 때만 dry-order가 실행된다. `DRY_RUN=false`면 안전을 위해 중단한다.
