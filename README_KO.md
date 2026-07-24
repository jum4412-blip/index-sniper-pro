# BTC Structure Trend v1.2 — Cross + Shadow

Bitget UTA용 BTCUSDT 추세 눌림목·되돌림 엔진입니다. 이번 릴리스에는 **실주문이 구조적으로 불가능한 전용 쉐도우 모드**가 추가됐습니다.

## 고정 운용 계약

- 종목: `BTCUSDT` USDT 무기한
- 실거래 증거금 모드: `crossed`
- 포지션 모드: `hedge_mode`
- 레버리지: 5배
- 1회 진입 수량: 평가자산의 50%를 증거금으로 환산
- 명목 포지션: 평가자산의 약 2.5배
- 동시 포지션: 1개
- 물타기·마틴게일·추가진입: 없음
- 고정 익절: 없음

평가자산이 10,000 USDT라면 약 25,000 USDT 명목가치로 수량을 계산합니다. 크로스 실거래에서 50%는 주문 수량 산정 기준일 뿐, 손실을 시드 절반으로 격리하는 경계가 아닙니다.

## 세 가지 실행 모드

| 모드 | 시세 | 계정 조회 | 주문 | 가상 체결·손익 |
|---|---|---|---|---|
| `OBSERVE_PUBLIC_ONLY` | Bitget 공개 API | 없음 | 불가능 | 없음 |
| `SHADOW_PUBLIC_ONLY` | Bitget 공개 API | 없음 | 불가능 | 있음 |
| `LIVE/LIVE_GATED` | 공개+인증 API | 있음 | ARM 조건 충족 시 가능 | 없음 |

### 쉐도우 모드의 안전 경계

쉐도우 모드는 `PublicBitgetClient`라는 별도 클라이언트를 사용합니다.

- `.env`의 Bitget API 키를 읽지 않음
- 인증 요청(`auth=True`)을 거부
- `/api/v3/market/` 이외의 경로를 거부
- 모든 `POST` 요청을 거부
- 라이브 상태 파일과 별도 상태 파일 사용
- 시작 스크립트가 라이브 ARM을 해제하고 `BTC_STRUCTURE_V1_LIVE_ENABLED=false`로 변경

따라서 키가 서버에 남아 있어도 쉐도우 프로세스 자체에는 주문 기능이 없습니다.

## 쉐도우 체결 모델

쉐도우 모드는 신호만 출력하는 관찰 모드가 아니라 실제 전략 흐름을 가상 계정에서 이어갑니다.

- 롱 진입: 최우선 매도호가 + 진입 슬리피지
- 숏 진입: 최우선 매수호가 - 진입 슬리피지
- 롱 청산: 최우선 매수호가 - 청산 슬리피지
- 숏 청산: 최우선 매도호가 + 청산 슬리피지
- 기본 진입 슬리피지: 2bp
- 기본 청산 슬리피지: 2bp
- 기본 시장가 수수료: 편도 0.06%
- 수량: 가상 평가자산 50% × 5배
- 가상 하드스톱: 마크가격이 하드스톱을 통과하면 다음 루프의 호가 기준으로 청산
- 정상 손절: 완성된 15분봉의 강한 매물대 무효화 또는 2개 연속 종가 이탈
- 추세 청산: 1시간 추세 반전 확인
- 고정 익절 없음, +1.5R 이후 구조·ATR 추적

수수료는 진입 시 잔고에서 먼저 차감하고, 청산 시 실현손익과 청산 수수료를 반영합니다. 열린 포지션의 평가자산은 미실현손익에서 예상 청산 수수료까지 차감해 계산합니다.

현재 버전은 펀딩비와 급격한 갭 사이의 초단기 체결 경로를 재현하지 않습니다. 20초 주기의 REST 폴링이므로 실제 거래소 하드스톱보다 가상 하드스톱 체결이 유리하거나 불리할 수 있습니다. 쉐도우 결과는 실거래 결과의 보장이 아닙니다.

## 진입 논리

### 롱

1. 4시간봉 `EMA 50 > EMA 200`
2. 두 EMA 모두 상승 기울기
3. 4시간봉 ADX 18 이상, `+DI > -DI`
4. 1시간봉 `EMA 20 > EMA 50`
5. 최근 168시간의 거래량 프로파일에서 고거래량 지지 구역 계산
6. 15분봉이 구역을 터치한 뒤 상단 회복
7. 거래량·몸통·종가 위치 조건 통과
8. 다음 반대 매물대까지 최소 2.2R
9. 점수 75 이상

### 숏

위 조건을 반대로 적용합니다. 하락 추세에서 고거래량 저항 구역까지 반등한 뒤 15분봉이 다시 아래로 밀릴 때 진입합니다.

## 매물대와 손절

OHLCV 캔들의 거래량을 각 봉의 고가~저가 가격 구간에 분배하고, 64개 가격 구간 중 거래량 상위 30%의 인접 구간을 합쳐 고거래량 매물대로 근사합니다. 체결별 Volume-at-Price와 동일하지는 않습니다.

정상 손절은 다음 중 하나가 완성된 15분봉에서 확인될 때 발생합니다.

- 소프트스톱을 ATR 0.08 이상 추가 돌파
- 거래량이 직전 20봉 중앙값의 1.35배 이상
- 몸통 비율 55% 이상
- 돌파 방향 끝부분에서 마감

또는 소프트스톱 밖에서 15분봉 2개가 연속 마감하면 구조 무효화로 처리합니다.

장애용 하드스톱은 정상 소프트스톱보다 바깥에 두며 진입가 대비 최대 2%입니다. 쉐도우에서는 마크가격으로 가상 감시하고, 라이브에서는 진입 주문에 거래소 마크가격 시장가 스톱을 첨부합니다.

## 추세수익 관리

- 허용 소프트 구조손절 폭: 진입가의 0.25%~1.25%
- +1R 즉시 본절 이동 없음
- +1.5R 이후 추적 활성화
- 15분 고거래량 구조, 확정 피벗, 3.2 ATR 샹들리에 기준 병행
- 스톱은 수익 방향으로만 이동
- 고정 익절 없음
- 1시간 EMA 20/50 반전과 거래량이 확인되면 전량 청산

## 공격적 운용 제한

- 통과 신호: 평가자산 50% × 5배
- 점수 75 미만: 미진입
- 다음 매물대까지 2.2R 미만: 미진입
- 하루 최대 4회
- 3연속 손실: 4시간 신규 진입 중지
- 일간 -7.5%, 주간 -15%, 최고점 대비 -20%: 신규 진입 차단

쉐도우 모드에도 같은 제한이 적용됩니다. 포지션 수량을 작게 만드는 제한이 아니라, 나쁜 장세에서 반복 진입을 막는 회로입니다.

# 설치

기존 서버의 `~/index-sniper-pro` 구조를 기준으로 합니다.

```bash
unzip btc_structure_trend_v1_bitget_cross_5x_50pct_shadow_v1_2.zip
cd btc_structure_trend_v1_shadow_release
bash scripts/install_btc_structure_trend_v1.sh
cd ~/index-sniper-pro
```

다른 경로:

```bash
bash scripts/install_btc_structure_trend_v1.sh /원하는/index-sniper-pro
```

설치 과정은 기존 파일을 `local_backups/`에 백업하고, 라이브 세션을 종료하며, ARM 파일을 삭제하고, `.env`의 라이브 활성값을 `false`로 설정합니다.

# 쉐도우 모드 실행

## 1. 10,000 USDT 가상 시드로 시작

```bash
bash start_btc_structure_trend_v1_shadow.sh 10000
```

인자를 생략하면 설정 파일의 `shadow_initial_equity` 기본값 10,000 USDT를 사용합니다.

```bash
bash start_btc_structure_trend_v1_shadow.sh
```

기존 쉐도우 상태 파일이 있으면 전달한 시드가 아니라 기존 가상 계정을 이어갑니다. 시드를 바꾸려면 먼저 초기화해야 합니다.

## 2. 상태 확인

```bash
bash status_btc_structure_trend_v1_shadow.sh
```

상태 출력에는 누적수익률, 실현·미실현손익, 수수료, 승률, 프로핏 팩터, 평균 순R, 최대 관측 낙폭과 현재 가상 포지션이 포함됩니다.

실시간 로그:

```bash
tail -f ~/index-sniper-pro/logs/btc-structure-trend-v1-shadow.log
```

## 3. 정지

```bash
bash stop_btc_structure_trend_v1.sh
```

쉐도우 포지션은 상태 파일에 남기 때문에 다시 시작하면 이어서 관리합니다. 정지 중 발생한 가격 경로는 재현할 수 없으므로 장기간 정지 후 재개한 결과는 실제 하드스톱과 다를 수 있습니다.

## 4. 초기화

현재 가상 기록을 `.bak`으로 보관하고 10,000 USDT로 새로 시작합니다.

```bash
bash reset_btc_structure_trend_v1_shadow.sh 10000
```

직접 CLI:

```bash
python -m index_sniper.btc_structure_trend_v1 \
  --config config/btc_structure_trend_v1.json \
  shadow-reset RESET_SHADOW --seed 10000
```

# 쉐도우 기록

- 상태: `data/btc_structure_trend_v1_shadow_state.json`
- 로그: `logs/btc-structure-trend-v1-shadow.log`
- 거래: `research/btc_structure_trend_v1_shadow_trades.csv`
- 자산곡선: `research/btc_structure_trend_v1_shadow_equity.csv`
- 이벤트: `research/btc_structure_trend_v1_shadow_events.jsonl`

거래 CSV에는 진입·청산가, 수량, 진입/청산 수수료, 총손익, 순손익, R배수, 계좌수익률, 청산 사유가 기록됩니다.

# 관찰 모드

가상 포지션도 만들지 않고 현재 신호만 보려면:

```bash
bash start_btc_structure_trend_v1_observe.sh
```

v1.2부터 관찰 모드도 공개 시장 API 전용이며 계정 조회와 주문 기능이 없습니다.

# 실매매 전환

쉐도우 결과를 충분히 검토한 뒤에만 별도로 계정 설정과 ARM을 진행합니다.

```bash
bash doctor_btc_structure_trend_v1.sh

BTC_STRUCTURE_CONFIRM_CROSS_ACCOUNT_MODE=YES \
  bash setup_btc_structure_trend_v1_account.sh

bash arm_btc_structure_trend_v1.sh \
  START_BTC_STRUCTURE_TREND_LIVE_5X_CROSS_50 \
  I_UNDERSTAND_CROSS_2_5X_NOTIONAL_CAN_USE_FULL_COLLATERAL \
  API_HAS_NO_WITHDRAW_PERMISSION \
  API_IP_WHITELISTED

bash start_btc_structure_trend_v1.sh
```

라이브 시작 스크립트는 쉐도우 세션을 먼저 종료합니다. 쉐도우 시작 스크립트는 반대로 라이브 ARM을 해제하고 라이브 세션을 종료합니다.

# 핵심 쉐도우 설정

`config/btc_structure_trend_v1.json`:

| 설정 | 기본값 | 의미 |
|---|---:|---|
| `shadow_initial_equity` | 10000 | 새 가상 계정의 초기 USDT |
| `shadow_entry_slippage_bps` | 2 | 진입 호가에 추가하는 불리한 슬리피지 |
| `shadow_exit_slippage_bps` | 2 | 청산 호가에 추가하는 불리한 슬리피지 |
| `shadow_taker_fee_rate` | 0.0006 | 편도 시장가 수수료 0.06% |
| `shadow_equity_sample_seconds` | 60 | 자산곡선 기록 최소 간격 |

개인 Bitget 수수료 등급이 다르면 `shadow_taker_fee_rate`를 실제 값에 맞춰 수정할 수 있습니다. 이 항목은 라이브 고정 계약 SHA에 포함되므로 ARM 후 변경하면 라이브 ARM이 무효화됩니다.

# 테스트

```bash
bash scripts/run_btc_structure_trend_v1_tests.sh
```

포함 검증:

- Python 문법 검사와 내부 self-test
- 캔들 역방향 페이지네이션
- 50% × 5배 수량 계산
- 매물대 생성과 강한 구조 돌파 판정
- 고정 익절 미사용
- 추적스톱 비확대
- 크로스 주문·레버리지 페이로드
- 공개 클라이언트의 인증 요청·비시장 경로·POST 차단
- 호가·슬리피지 가상 체결 계산
- 진입/청산 수수료와 가상 잔고 정산
- 쉐도우 상태 초기화와 기록 분리

# Bitget 공개 API

쉐도우·관찰 모드가 사용하는 경로는 아래 세 개뿐입니다.

- `GET /api/v3/market/instruments`
- `GET /api/v3/market/tickers`
- `GET /api/v3/market/candles`

Bitget 공식 UTA 문서 확인 기준일: 2026-07-24

- https://www.bitget.com/api-doc/uta/public/Instruments
- https://www.bitget.com/api-doc/uta/public/Tickers
- https://www.bitget.com/api-doc/uta/public/Get-Candle-Data

