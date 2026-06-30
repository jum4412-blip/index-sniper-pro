# index-sniper-pro

고정 프로젝트: `index-sniper-pro`

## v0.1 목표

- Bitget UTA API 키 로딩
- UTA 계정 조회
- UTA 자산 조회
- 텔레그램 시작/결과 알림
- 실주문 없음

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

## 실행

```bash
bash run_check.sh
```
