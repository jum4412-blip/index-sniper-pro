# BTC Structure Trend v1.1.0 Cross 전환 릴리스 노트

## 변경 사항

- Bitget UTA 증거금 모드를 `isolated`에서 `crossed`로 변경
- 주문·청산 페이로드에 `marginMode=crossed` 적용
- 크로스 레버리지를 BTCUSDT 거래쌍 단위 5배로 한 번만 설정
- `set-leverage` 요청에서 isolated 전용 `posSide`, `longLeverage`, `shortLeverage` 필드 제거
- accountLevel `basic` 또는 `advanced`를 크로스 가능 상태로 인정
- 기존 accountLevel이 isolated/delta이면 명시적 확인 후 기본 `basic` 전환
- ARM 문구를 크로스 2.5배 명목노출·전체 담보 사용 가능성을 명시하도록 변경
- 크로스 모드에서 50%는 주문 수량 산정 기준이며 손실 격리선이 아니라는 설명 추가

## 유지되는 전략 계약

- BTCUSDT 전용, USDT 무기한
- hedge mode
- 레버리지 5배
- 계좌 평가자산의 50%를 증거금 환산 기준으로 수량 계산
- 명목 노출 약 2.5배
- 고거래량 매물대 강한 종가 이탈형 소프트 손절
- 거래소 마크가격 장애용 하드스톱
- 고정 익절 없음, +1.5R 이후 구조·ATR 추적손절
- 동시 포지션 1개, 물타기·마틴게일 없음

## 검증

- Python 문법 및 compileall
- 내부 self-test
- 크로스 주문 페이로드 검사
- Bitget `crossed` 레버리지 페이로드 검사
- 단위검사 전체
- 셸 스크립트 `bash -n`
- 설치·백업·DISARM 동작 시험

실제 Bitget 인증 API와 주문 체결은 사용자 계정에서 `doctor → setup-account → observe → prearm` 순서로 확인해야 합니다.
