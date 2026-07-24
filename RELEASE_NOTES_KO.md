# BTC Structure Trend v1.2.0 릴리스 노트

## 새 기능: 전용 쉐도우 모드

- Bitget 공개 시장 데이터로 실제 전략 신호를 계산합니다.
- 가상 시드의 50%를 증거금으로 환산하고 5배 수량을 적용합니다.
- 최우선 호가, 진입·청산 슬리피지, 편도 시장가 수수료를 반영합니다.
- 매물대 구조손절, 마크가격 하드스톱, 추적손절, 1시간 추세 반전 청산을 가상 포지션에 동일하게 적용합니다.
- 잔고, 평가자산, 미실현손익, 수수료, 거래 기록과 자산곡선을 별도 파일에 저장합니다.
- 쉐도우 상태를 재시작 후 이어서 관리할 수 있습니다.

## 주문 불가능 구조

- 쉐도우와 관찰 모드는 `.env`의 Bitget API 키를 읽지 않습니다.
- 전용 공개 클라이언트가 인증 요청, 비시장 API, 모든 POST를 거부합니다.
- 라이브 상태와 쉐도우 상태 경로를 분리했습니다.
- 쉐도우 시작 시 라이브 세션을 종료하고 ARM을 해제합니다.
- 라이브 시작 시 쉐도우 세션을 종료합니다.

## OBSERVE 안전성 수정

기존 OBSERVE 경로는 라이브 상태가 남아 있을 경우 포지션 복구·관리 코드와 접촉할 수 있었습니다. v1.2부터 OBSERVE는 공개 시세와 신호 계산만 수행하는 독립 경로로 변경됐습니다.

## 새 명령

```bash
bash start_btc_structure_trend_v1_shadow.sh 10000
bash status_btc_structure_trend_v1_shadow.sh
bash reset_btc_structure_trend_v1_shadow.sh 10000
```

직접 CLI:

```bash
python -m index_sniper.btc_structure_trend_v1 --config config/btc_structure_trend_v1.json shadow-once --seed 10000
python -m index_sniper.btc_structure_trend_v1 --config config/btc_structure_trend_v1.json shadow-loop --seed 10000
python -m index_sniper.btc_structure_trend_v1 --config config/btc_structure_trend_v1.json shadow-status
```

## 기본 체결 가정

- 진입 슬리피지: 2bp
- 청산 슬리피지: 2bp
- 시장가 수수료: 편도 0.06%
- 자산곡선 샘플: 60초
- 펀딩비: 현재 미반영

## 검증

- Python 컴파일
- 내부 self-test
- 단위검사 13개
- 공개 클라이언트 주문 차단
- 가상 체결 방향별 호가·슬리피지
- 가상 진입·청산 수수료와 잔고 정산
- 라이브/쉐도우 파일 분리
- 쉘 스크립트 문법

이 실행 환경에서는 외부 DNS가 차단되어 Bitget 공개 API에 직접 연결하는 런타임 스모크 테스트는 수행하지 못했습니다. API 경로와 응답 필드는 2026-07-24 Bitget 공식 UTA 공개 문서와 대조했습니다.
