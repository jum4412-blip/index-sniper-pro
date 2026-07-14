# Larry Williams Core v1.0 — ETHUSDT + SKHYUSDT

Bitget UTA용 **완전 신규 엔진**입니다. 기존 v6.x의 점수·IMPULSE·레짐 로직은 가져오지 않고, 기존 프로젝트에서는 API 통신 클래스와 텔레그램 전송 기반만 재사용합니다.

## 확정된 실거래 조건

- 거래 대상: `ETHUSDT`, `SKHYUSDT`
- 레버리지: 두 종목 모두 `Cross 5x`
- 진입 증거금: 신호 1회마다 현재 계좌 자산의 `30%`
- 동시 포지션: **전체 합산 1개**
- 주문: 시장가 진입 + 거래소 측 초기 손절 + 비상 익절
- 신규 진입은 명시적 ARM 절차를 통과한 뒤에만 허용

스크린샷의 가용자산 `1,251.7674 USDT`를 예로 들면:

- 진입 증거금: 약 `375.53 USDT`
- 5배 명목가치: 약 `1,877.65 USDT`
- 기초자산이 반대로 1% 움직일 때 계좌 영향: 약 `-1.5%` 전후(수수료·슬리피지 제외)
- 두 종목을 동시에 30%씩 열면 명목가치가 계좌의 약 300%가 되므로, 이 버전은 신호 점수가 높은 한 종목만 선택합니다.

## 래리 윌리엄스 핵심의 구현 방식

### 1. Volatility Breakout

- ETH: UTC 00:00 일일 세션 시가를 기준으로 최근 일일 변동폭의 일부를 더하거나 빼서 돌파선을 계산합니다.
- SKHY: 미국 정규장 시가와 전 정규장 고저 범위를 사용합니다.
- 전일 변동폭이 압축됐으면 K를 낮추고, 이미 확장됐으면 K를 높입니다.
- 돌파 직후 너무 멀리 추격한 신호는 폐기합니다.

### 2. OOPS!

- ETH: 전 UTC 세션 고가·저가를 순간적으로 넘긴 뒤 다시 범위 안으로 복귀한 실패 돌파를 거래합니다.
- SKHY: 실제 미국 장 시작 갭 또는 전일 고저 스윕 후 복귀를 사용하므로 원형 OOPS에 더 가깝습니다.

### 3. Williams %R

과매수·과매도 자체로 역매매하지 않습니다. 돌파에서는 모멘텀 지속 여부, OOPS에서는 극단권 이탈 여부를 진입 타이밍 확인용으로 씁니다.

### 4. Ultimate Oscillator

7·14·28 구간을 결합해 단일 기간 오실레이터의 거짓 다이버전스를 줄이는 확인 신호로 사용합니다.

### 5. WILLSTOP 방식의 가격점 트레일

공개되지 않은 독점 수식을 임의로 꾸며내지 않습니다. 공개된 핵심인 **이동평균이 아닌 시장의 특정 가격점과 패턴**을 따라 다음과 같이 구현합니다.

- 최초 손절: 신호봉 저점/고점 또는 OOPS 스윕 극단값 바깥
- +1R 도달: 손절을 손익분기점+비용 버퍼로 이동
- 이후: 직전 2개 완료 15분봉의 저점/고점을 이용해 스톱을 한 방향으로만 조정
- 거래소 초기 손절은 항상 남아 있고, 동적 트레일은 엔진이 시장가 청산으로 집행합니다.

### 6. Bailout Exit

- ETH: 최소 2시간 보유 후, +1R 이전 구간에서 첫 수익성 1시간 구간 시작 시 청산
- SKHY: 최소 1시간 보유 후, +1R 이전 구간에서 첫 수익성 30분 구간 시작 시 청산
- +1R 이후에는 bailout보다 가격점 트레일로 수익을 끌고 갑니다.

## 크립토와 Stock Perp의 차별 처리

### ETHUSDT

- 24시간 거래를 UTC 일일 세션으로 재구성
- 펀딩이 한쪽으로 과도하게 몰린 경우 점수 감점
- OI와 스프레드 확인
- 최대 보유 36시간

### SKHYUSDT

SKHYUSDT는 일반 코인처럼 24시간 같은 품질의 기초가격이 들어오는 상품이 아닙니다. 따라서:

- 신규 진입: 미국 뉴욕시간 `09:45–15:30`만
- 첫 15분은 일반 돌파 진입에서 제외
- 뉴욕시간 `15:55` 이전 강제 청산
- 주말·미국 증시 휴장일 진입 금지
- 장외 시간에는 신규 신호를 만들지 않음
- `status=online`, `symbolType=stock`, `isReality=no`, `maxLeverage>=5`를 매번 검증
- 종목 상태가 `restrictedAPI`, `limit_open`, `offline`이면 자동 차단
- 거래소 instrument API에서 가격·수량 단위, 최소 주문금액을 동적으로 읽음
- SKHY는 상장 이력이 짧으므로 계절성 가중치는 데이터가 쌓일 때까지 사용하지 않음

## 위험 제한

- ETH 한 거래 계좌 위험 상한: 약 2.0%
- SKHY 한 거래 계좌 위험 상한: 약 2.5%
- 구조적 손절폭이 이 상한을 넘으면 수량을 줄이는 대신 **그 신호를 건너뜁니다**. 진입 증거금 30% 조건을 유지하기 위해서입니다.
- 일간 계좌 낙폭 5%: 신규 진입 정지
- 주간 계좌 낙폭 9%: 신규 진입 정지
- 2연속 손실: 신규 진입 정지
- 물타기·손실 포지션 추가진입 없음
- 거래소 전체에서 관리되지 않은 포지션이 발견되면 자동 중단

## 설치

압축을 풀고 서버로 올린 뒤:

```bash
cd <압축을 푼 폴더>
bash install_larry_core_v1.sh ~/index-sniper-pro
```

설치는 기존 동명 파일을 `local_backups/larry_core_v1_날짜시간`에 백업하고, 신규 엔진을 **DISARMED** 상태로 설치합니다. 기존 v6.3 프로세스는 중지하지만 포지션 청산 주문은 보내지 않습니다.

## 실행 순서

```bash
cd ~/index-sniper-pro

# 1. API·계정·종목 상태 확인
bash doctor_larry_core_v1.sh

# 2. ETHUSDT와 SKHYUSDT를 Cross 5x로 설정
bash setup_larry_core_v1_account.sh

# 3. 엔진 실행 — 아직 신규 실진입은 차단됨
bash start_larry_core_v1.sh

# 4. 실매매 활성화
bash arm_larry_core_v1.sh \
  START_LARRY_CORE_LIVE_5X_CROSS_30_ETH_SKHY \
  I_UNDERSTAND_30PCT_MARGIN_5X \
  API_HAS_NO_WITHDRAW_PERMISSION \
  API_IP_WHITELISTED
```

관찰 전용으로 먼저 실행하려면:

```bash
bash start_larry_core_v1_observe.sh
```

상태 확인:

```bash
bash status_larry_core_v1.sh
```

신규 진입만 차단하고 열린 포지션 관리는 계속하려면:

```bash
bash disarm_larry_core_v1.sh
```

엔진 완전 정지:

```bash
bash stop_larry_core_v1.sh
```

정지는 청산 명령이 아닙니다. 열린 포지션이 있을 때 정지하면 거래소 초기 SL/비상 TP는 남지만 소프트웨어 WILLSTOP과 bailout은 멈춥니다.

## 주요 파일

- `index_sniper/larry_williams_core_v1.py`: 전략·주문·위험관리 엔진
- `config/larry_williams_core_v1.json`: ETH/SKHY별 설정
- `data/larry_williams_core_v1_state.json`: 런타임 상태
- `research/larry_williams_core_v1_trades.csv`: 거래 기록
- `research/larry_williams_core_v1_events.jsonl`: 이벤트 기록
- `logs/larry-williams-core-v1.log`: 실행 로그

## API 권한

Bitget API 키에는 UTA 조회·거래·계정설정 권한이 필요합니다. 출금 권한은 제거하고 IP 화이트리스트를 적용해야 ARM 문구와 실제 보안 상태가 일치합니다.
