# BTC Structure Trend v1.1 — Cross

Bitget UTA용 BTCUSDT 추세 눌림목·되돌림 실매매 엔진입니다.

## 고정 운용 계약

- 종목: `BTCUSDT` USDT 무기한
- 계정: 전용 계정
- 증거금 모드: `crossed` (Bitget 화면의 크로스 모드)
- 포지션 모드: `hedge_mode`
- 레버리지: 5배
- 1회 주문 수량 기준 증거금 환산: 계좌 평가자산의 50%
- 명목 포지션: 계좌 평가자산의 약 2.5배
- 동시 포지션: 1개
- 물타기·마틴게일·추가진입: 없음
- 고정 익절: 없음

예를 들어 평가자산이 10,000 USDT라면 주문 수량은 5,000 USDT × 5배, 즉 약 25,000 USDT 명목가치로 계산됩니다. 수수료와 펀딩비를 빼기 전 기준으로 BTC가 1% 역행하면 계좌 영향은 약 -2.5%, 1% 순행하면 약 +2.5%입니다.

**크로스 모드에서는 “시드 절반 진입”이 손실을 시드 절반으로 격리한다는 뜻이 아닙니다.** 50%는 주문 수량 산정 기준일 뿐이며, Bitget의 크로스 담보로 인정되는 나머지 계정 자산도 포지션 유지에 사용될 수 있습니다. 따라서 같은 계정의 다른 크로스 포지션·손익·담보 변화가 청산 여유에 영향을 줄 수 있습니다.

설정 파일의 레버리지, 수량 기준 비율, 종목, 크로스 모드를 바꾸면 엔진이 시작 단계에서 계약 위반으로 중단됩니다. 의도치 않은 설정 드리프트를 막기 위한 동작입니다.

## 진입 논리

### 롱

1. 4시간봉 `EMA 50 > EMA 200`
2. 두 EMA가 모두 상승 중
3. 4시간봉 ADX가 18 이상이고 `+DI > -DI`
4. 1시간봉 `EMA 20 > EMA 50`
5. 최근 168시간의 거래량 프로파일에서 고거래량 지지 구역을 계산
6. 15분봉이 그 구역을 터치한 뒤 구역 상단을 다시 회복
7. 신호 봉 거래량이 직전 20개 중앙값의 1.05배 이상
8. 신호 봉 몸통 비율, 상단 마감 위치, 다음 매물대까지의 공간을 점수화
9. 점수 75 이상인 A급 이상 셋업만 계좌 평가자산 50%의 증거금 환산 수량으로 시장가 진입

### 숏

위 조건을 반대로 적용합니다. 4시간·1시간 하락 추세에서 1시간 고거래량 저항 구역까지 반등한 뒤, 15분봉이 구역 아래로 다시 밀리는 경우에만 숏 진입합니다.

## 매물대 계산

일반 OHLCV 캔들에는 체결별 가격대 거래량이 없으므로 완전한 거래소 Volume Profile을 재현할 수 없습니다. 이 엔진은 각 1시간봉의 거래량을 해당 봉의 고가~저가에 걸친 가격 구간에 분산하고, 64개 가격 구간 중 거래량 상위 30%에 해당하는 인접 구간을 합쳐 고거래량 매물대로 봅니다.

모든 거래량을 종가나 HLC3 한 지점에 몰아넣지 않기 때문에 일반적인 캔들 기반 근사치보다 과도한 정밀도 착각을 줄이는 방식입니다. 다만 체결별 데이터 기반 프로파일과 동일하지는 않습니다.

## 손절: 두 겹 구조

### 1. 정상 손절 — 소프트 구조손절

단순히 가격이 매물대를 잠깐 찌른 것만으로 청산하지 않습니다. 다음 중 하나가 완성된 15분봉에서 확인될 때 시장가 청산합니다.

- 매물대 무효화 가격을 ATR 0.08만큼 더 돌파하고,
- 거래량이 직전 20봉 중앙값의 1.35배 이상이고,
- 몸통이 전체 봉 범위의 55% 이상이며,
- 롱은 저가 부근, 숏은 고가 부근에서 마감한 강한 돌파봉

또는 강한 단일봉 조건이 아니더라도 소프트스톱 바깥에서 15분봉이 2개 연속 마감하면 구조가 무효화된 것으로 처리합니다.

이 방식은 단순 1% 계좌손실 고정손절보다 꼬리와 짧은 휩쏘에 덜 민감하지만, 15분봉 마감을 기다리는 동안 손실이 커질 수 있습니다.

### 2. 장애 손절 — 거래소 하드스톱

진입 주문에는 마크가격 기준 시장가 `stopLoss`를 반드시 첨부합니다. 정상 전략은 이 가격까지 기다리지 않으며, 서버 정지·네트워크 단절·API 오류·급격한 갭에 대비한 최후 방어선입니다.

- 정상 소프트스톱 바깥으로 ATR 0.65 또는 진입가의 0.30% 중 큰 값을 추가
- 진입가 대비 최대 2.00% 이내
- 주문 체결 후 `order-info`로 하드스톱이 실제 주문에 기록됐는지 재검증
- 스톱이 확인되지 않으면 즉시 시장가 비상청산 후 자동 DISARM
- 타임아웃·알 수 없는 주문 오류는 `clientOid`로 조회해 중복 주문 없이 복구

계좌 평가자산 50%의 증거금 환산 수량·5배에서 2% 가격 역행은 수수료 전 계좌 약 -5%에 해당합니다. 하드스톱은 목표 손실이 아니라 장애 시 상한선에 가까운 장치입니다. 급변·슬리피지·청산 위험으로 실제 결과는 더 나쁠 수 있습니다.

## 짧은 손절, 추세수익 끝까지

- 구조손절 폭이 진입가의 0.25% 미만이면 미세 잡음일 가능성이 높아 진입하지 않습니다.
- 구조손절 폭이 1.25%를 넘으면 50% 고정 증거금에서 손실이 너무 커지므로 진입하지 않습니다.
- 고정 익절 주문은 사용하지 않습니다.
- +1R에서 자동 본절 이동을 하지 않습니다. 강한 추세가 본절 휩쏘로 잘리는 것을 줄이기 위한 선택입니다.
- +1.5R부터 추적손절을 활성화합니다.
- 추적손절은 새로 형성된 15분 고거래량 구역·확정 피벗과 `최고가/최저가 ± 3.2 ATR` 중 더 여유 있는 값을 사용합니다.
- 스톱은 수익 방향으로만 이동하며 절대로 다시 넓히지 않습니다.
- 1시간 EMA 20/50 추세가 반전되고 거래량까지 동반되면 전량 청산합니다.

즉, 손실은 가까운 구조 무효화에서 끊고 수익은 고정 목표가 없이 추세 구조가 살아 있는 동안 보유합니다.

## 공격적 운용 방식

이 버전은 모든 신호에 작게 들어가는 방식이 아니라 **좋은 신호에는 크게, 애매한 신호에는 아예 안 들어가는 방식**입니다.

- 점수 75 미만: 진입하지 않음
- 다음 반대 매물대까지 최소 2.2R 공간이 없으면 진입하지 않음
- 통과한 셋업: 계좌 평가자산 50% × 5배로 주문 수량 계산
- 하루 최대 4회
- 3연속 손실이면 4시간 신규 진입 중지
- 일간 -7.5%, 주간 -15%, 계좌 최고점 대비 -20% 도달 시 신규 진입 차단

이 제한은 진입 자체를 지나치게 작게 만드는 것이 아니라, 나쁜 구간에서 2.5배 명목 노출이 연속으로 복리 훼손을 일으키는 것을 막습니다. 물타기와 손실 후 배팅 확대는 구현하지 않았습니다.

## 설치

기존 GitHub 저장소를 서버의 `~/index-sniper-pro`에 배포한 구조를 기준으로 합니다. 저장소에 다음 모듈이 있어야 합니다.

- `index_sniper.exchange.bitget_uta.BitgetUTAClient`
- 선택: `index_sniper.telegram.bot.TelegramBot`

압축을 풀고 릴리스 폴더에서 실행합니다.

```bash
bash scripts/install_btc_structure_trend_v1.sh
```

다른 경로에 설치할 때:

```bash
bash scripts/install_btc_structure_trend_v1.sh /원하는/index-sniper-pro
```

설치만으로는 주문하지 않습니다. `.env`의 `BTC_STRUCTURE_V1_LIVE_ENABLED=false`로 저장되고 ARM 파일도 제거됩니다.

## 환경변수

`~/index-sniper-pro/.env`:

```dotenv
BITGET_API_KEY=...
BITGET_SECRET_KEY=...
BITGET_PASSPHRASE=...

# 선택 사항
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...

# arm 스크립트가 true로 설정하기 전에는 신규 실진입 없음
BTC_STRUCTURE_V1_LIVE_ENABLED=false
```

API 키에는 거래 권한만 부여하고 출금 권한은 부여하지 않습니다. 전용 서브계정과 IP 화이트리스트 사용을 전제로 ARM 문구를 구성했습니다.

## 계정 설정

앱과 다른 봇에서 모든 포지션·일반 주문·TP/SL 주문을 정리한 전용 계정에서 실행합니다.

```bash
bash doctor_btc_structure_trend_v1.sh
```

크로스 사용이 가능한 account level과 BTCUSDT 5배·hedge 설정:

```bash
BTC_STRUCTURE_CONFIRM_CROSS_ACCOUNT_MODE=YES \
  bash setup_btc_structure_trend_v1_account.sh
```

현재 accountLevel이 이미 `basic` 또는 `advanced`이면 계정 레벨을 변경하지 않습니다. `isolated`, `delta` 또는 알 수 없는 상태라면 위 확인 변수가 있을 때 기본적으로 `basic`으로 전환합니다. 기존에 `advanced`를 사용하는 계정은 그대로 유지됩니다. 강제로 advanced 전환을 요청하려면 다음처럼 지정할 수 있지만, Bitget의 자격 조건을 충족해야 합니다.

```bash
BTC_STRUCTURE_CONFIRM_CROSS_ACCOUNT_MODE=YES \
BTC_STRUCTURE_CROSS_ACCOUNT_LEVEL=advanced \
  bash setup_btc_structure_trend_v1_account.sh
```

설정 스크립트는 다음을 검증합니다.

- 계정에 포지션 0
- 일반 미체결 주문 0
- TP/SL 전략 주문 0
- account level: `basic` 또는 `advanced`
- hold mode: `hedge_mode`
- BTCUSDT margin mode: `crossed`
- BTCUSDT cross leverage: 5

## 관찰 모드

실주문 없이 실제 Bitget 시세·계정 읽기와 신호 계산을 반복합니다.

```bash
bash start_btc_structure_trend_v1_observe.sh
bash status_btc_structure_trend_v1.sh
```

로그:

```bash
tail -f ~/index-sniper-pro/logs/btc-structure-trend-v1.log
```

## 실매매 활성화

먼저 관찰 프로세스를 정지합니다.

```bash
bash stop_btc_structure_trend_v1.sh
```

ARM:

```bash
bash arm_btc_structure_trend_v1.sh \
  START_BTC_STRUCTURE_TREND_LIVE_5X_CROSS_50 \
  I_UNDERSTAND_CROSS_2_5X_NOTIONAL_CAN_USE_FULL_COLLATERAL \
  API_HAS_NO_WITHDRAW_PERMISSION \
  API_IP_WHITELISTED
```

시작:

```bash
bash start_btc_structure_trend_v1.sh
```

상태:

```bash
bash status_btc_structure_trend_v1.sh
```

신규 진입만 차단하면서 열린 포지션 관리는 계속하려면:

```bash
bash disarm_btc_structure_trend_v1.sh
```

프로세스까지 정지하려면:

```bash
bash stop_btc_structure_trend_v1.sh
```

프로세스를 정지하면 거래소 초기 하드스톱은 남아 있지만 소프트 구조손절과 수익 추적손절은 작동하지 않습니다.

## 상태와 기록

- 상태: `data/btc_structure_trend_v1_state.json`
- ARM: `data/BTC_STRUCTURE_TREND_V1_ARMED.json`
- 로그: `logs/btc-structure-trend-v1.log`
- 거래: `research/btc_structure_trend_v1_trades.csv`
- 이벤트: `research/btc_structure_trend_v1_events.jsonl`

ARM 파일에는 설정 파일 SHA-256이 들어갑니다. ARM 이후 설정을 변경하면 ARM이 자동 무효화됩니다.

## 테스트

릴리스 폴더에서:

```bash
bash scripts/run_btc_structure_trend_v1_tests.sh
```

포함된 검증:

- Python 문법 검사
- 내부 self-test
- 100개 단위 캔들 역방향 페이지네이션
- 50% × 5배 수량 계산
- 거래량 프로파일 매물대 생성
- 거래소 하드스톱 주문 페이로드
- 고정 익절이 없는지 확인
- 강한 매물대 돌파 손절 판정
- 추적스톱이 절대 느슨해지지 않는지 확인
- 설정 계약 변경 차단

릴리스 환경에서는 모의 API 단위검사까지 수행했습니다. 실제 사용자 Bitget 계정의 인증 API 호출과 실주문 체결 검증은 서버에서 `doctor`, 관찰 모드, 소액 또는 별도 테스트 계정 순서로 확인해야 합니다.

## 핵심 설정값

`config/btc_structure_trend_v1.json`의 전략 민감도는 수정할 수 있지만, `symbol`, `leverage`, `margin_mode`, `hold_mode`, `entry_margin_pct`는 고정 계약입니다.

| 설정 | 기본값 | 의미 |
|---|---:|---|
| `signal_score_min` | 75 | 진입 최소 품질 점수 |
| `min_stop_pct` | 0.25 | 지나치게 짧은 구조손절 진입 제외 |
| `max_stop_pct` | 1.25 | 50% 증거금 환산 수량 진입 허용 최대 소프트스톱 폭 |
| `hard_stop_max_pct` | 2.00 | 장애용 거래소 스톱 최대 거리 |
| `strong_break_volume_ratio` | 1.35 | 강한 돌파 거래량 기준 |
| `trail_activate_r` | 1.50 | 추적손절 시작 수익 |
| `trail_chandelier_atr` | 3.20 | 추세 보유용 느슨한 ATR 추적 |
| `max_daily_loss_pct` | 7.5 | 신규 진입 일간 차단선 |
| `max_peak_drawdown_pct` | 20.0 | 신규 진입 최고점 낙폭 차단선 |

## Bitget UTA API 경로

이 엔진이 사용하는 핵심 경로:

- `GET /api/v3/market/candles`
- `GET /api/v3/market/tickers`
- `GET /api/v3/market/instruments`
- `GET /api/v3/account/assets`
- `GET /api/v3/account/settings`
- `POST /api/v3/account/adjust-account-mode` — 현재 accountLevel이 basic/advanced가 아닐 때만 사용
- `POST /api/v3/account/set-hold-mode`
- `POST /api/v3/account/set-leverage`
- `GET /api/v3/position/current-position`
- `POST /api/v3/trade/place-order`
- `GET /api/v3/trade/order-info`
- `POST /api/v3/trade/close-positions`

Bitget UTA는 주문과 레버리지의 크로스 열거값으로 `crossed`를 사용하며, 크로스 선물은 `basic` 또는 `advanced` account level에서 설정합니다. 현재 공식 UTA 문서 기준으로 작성했습니다. 거래소가 요청 필드나 계정 모드 동작을 변경하면 `doctor`가 실패할 수 있으므로 오류를 무시하고 ARM하지 마세요.
## 공식 Bitget UTA 문서

확인 기준일: 2026-07-23

- 주문 `marginMode`: https://www.bitget.com/api-doc/uta/trade/Place-Order
- 거래쌍 레버리지 설정: https://www.bitget.com/api-doc/uta/account/Change-Leverage
- accountLevel·symbolConfig 조회: https://www.bitget.com/api-doc/uta/account/Get-Account-Setting
- UTA 계정·크로스 설정 가이드: https://www.bitget.com/api-doc/uta/best-practices

