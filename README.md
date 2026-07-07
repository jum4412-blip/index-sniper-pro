# Index Sniper Pro v5.2 Top10 VWAP Rubber Band Scalp Live Patch

영상 자막 기준 VWAP 스캘핑 전략을 Bitget UTA USDT-FUTURES용으로 구현한 실전 가능 패치입니다.

## 영상에서 추출한 규칙

### 1부
- VWAP = 거래량 가중 평균가.
- 실시간 체결량 기반 계산이 원칙.
- VWAP보다 현재가가 일정 이상 높으면 평균 회귀 숏.
- VWAP보다 현재가가 일정 이상 낮으면 평균 회귀 롱.
- 단순 시장가 버전은 수수료/슬리피지와 추세장에서 문제가 큼.
- 지정가 주문으로 수수료와 슬리피지를 줄이는 방향.

### 2부 핵심 실전형 고무줄 전략
- VWAP 밴드 생성: VWAP ± 표준편차 × 배수.
- ADX <= 20일 때만 횡보장으로 보고 주문 배치.
- 상단 밴드에 숏 지정가, 하단 밴드에 롱 지정가.
- 진입 즉시 +0.6% 익절 지정가 주문.
- -0.3% 손실이면 시장가 손절.
- 가격이 VWAP에 닿아도 시장가 청산.
- 5초 변동률 0.15% 이상이면 모든 지정가 취소 후 10분 대기.
- 워밍업 10분.

## 이 패치의 구현 방식

- 유니버스: 시가총액 상위 10개 코인을 CoinGecko로 조회 후 Bitget USDT-FUTURES 상장 종목만 사용. 부족하면 대형 코인 fallback으로 채움.
- 데이터: Bitget 1m candles 기반 VWAP/밴드/ADX 계산. 영상의 AggTrade tick VWAP과 완전히 동일하진 않지만, 실전 적용 가능한 1차 버전입니다.
- 주문: Bitget UTA limit + post_only maker entry.
- TP/SL: 주문 preset TP/SL 포함. VWAP touch는 봇이 시장가로 정리.
- 리스크: 전체 시드 30%, 기본 레버리지 1배, 동시 포지션 3개. 사용자의 요청에 따라 하루 +2% / -1% daily guard는 기본 OFF.

## 적용

```bash
cd ~/index-sniper-pro
source .venv/bin/activate

git pull
chmod +x apply_v52_top10_vwap_scalp_live.sh
bash apply_v52_top10_vwap_scalp_live.sh
```

## 시작 전 체크

```bash
python -m py_compile index_sniper/v52_vwap_top10.py
bash run_v52_vwap_universe.sh
bash run_v52_vwap_preflight.sh
```

반드시 확인:

- `ok: true`
- universe가 비어 있지 않음
- 의도하지 않은 open_positions 없음
- V52_TOTAL_CAPITAL_RATIO=0.30 이하
- V52_LEVERAGE=1 기본. 2~5 사이에서 조정 가능.

## 1회 실행

```bash
bash run_v52_vwap_once.sh
```

## 실전 루프 시작

```bash
bash start_v52_vwap_live.sh
bash status_v52_vwap.sh
```

## 로그

```bash
bash view_v52_vwap_log.sh
```

## 중지

```bash
bash stop_v52_vwap.sh
```

## v5.1 미체결 주문 일괄 취소

주의: 이 명령은 유니버스 심볼의 미체결 주문을 심볼 단위로 취소합니다. 수동 주문도 해당 심볼이면 취소될 수 있습니다.

```bash
bash cancel_v52_vwap_orders.sh
```

## 주요 설정

```env
V52_LEVERAGE=3
V52_TOTAL_CAPITAL_RATIO=0.30
V52_PER_ORDER_CAPITAL_RATIO_CAP=0.003
V52_MAX_ORDER_NOTIONAL_USDT=200
V52_UNIVERSE_COUNT=20
V52_MAX_OPEN_POSITIONS=3
V52_BAND_STD_MULT=2.0
V52_ADX_MAX=20
V52_TP_PCT=0.006
V52_SL_PCT=0.003
V52_SHOCK_PCT=0.15
V52_SHOCK_COOLDOWN_MINUTES=10
V52_DAILY_TARGET_PCT=2.0
V52_DAILY_LOSS_PCT=1.0
```

## 주의

이 전략은 초단타/지정가/다종목 전략이라 수수료, 체결 실패, API 지연, post_only 미체결, 급추세장에 민감합니다. 실전 시작 전 반드시 `once` 결과와 로그를 확인하세요.


## 반대 주문 처리

영상 자막에는 ADX<=20일 때 상단 밴드 숏 지정가와 하단 밴드 롱 지정가를 동시에 배치한다고 설명되어 있습니다.
반대편 주문을 체결 직후 즉시 취소하는지는 영상에 명확히 나오지 않습니다. 이 v5.2는 사용자의 요청대로 원형에 가깝게 양쪽 밴드 주문을 유지합니다.
다만 헤지 모드 선물에서는 양쪽 주문이 모두 체결될 수 있으므로 레버리지는 낮게 시작하는 것을 권장합니다.

## 체결 알림

V52_FILL_NOTIFY_ENABLED=true, V52_NOTIFY_ALL_FILLS=true 이므로 최근 체결 내역을 Telegram으로 보냅니다.
주문 배치/취소 내역은 로그에도 JSONL로 저장됩니다.
