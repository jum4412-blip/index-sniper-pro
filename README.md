# Index Sniper Pro v5.1 Top20 VWAP Rubber Band Scalp Live Patch

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

- 유니버스: 시가총액 상위 20개 코인을 CoinGecko로 조회 후 Bitget USDT-FUTURES 상장 종목만 사용. 실패 시 대형 코인 fallback 사용.
- 데이터: Bitget 1m candles 기반 VWAP/밴드/ADX 계산. 영상의 AggTrade tick VWAP과 완전히 동일하진 않지만, 실전 적용 가능한 1차 버전입니다.
- 주문: Bitget UTA limit + post_only maker entry.
- TP/SL: 주문 preset TP/SL 포함. VWAP touch는 봇이 시장가로 정리.
- 리스크: 전체 시드 30%, 레버리지 3배, 동시 포지션 3개, 하루 +2% / -1% guard.

## 적용

```bash
cd ~/index-sniper-pro
source .venv/bin/activate

git pull
chmod +x apply_v51_top20_vwap_scalp_live.sh
bash apply_v51_top20_vwap_scalp_live.sh
```

## 시작 전 체크

```bash
python -m py_compile index_sniper/v51_vwap_top20.py
bash run_v51_vwap_universe.sh
bash run_v51_vwap_preflight.sh
```

반드시 확인:

- `ok: true`
- universe가 비어 있지 않음
- 의도하지 않은 open_positions 없음
- V51_TOTAL_CAPITAL_RATIO=0.30 이하
- V51_LEVERAGE=3

## 1회 실행

```bash
bash run_v51_vwap_once.sh
```

## 실전 루프 시작

```bash
bash start_v51_vwap_live.sh
bash status_v51_vwap.sh
```

## 로그

```bash
bash view_v51_vwap_log.sh
```

## 중지

```bash
bash stop_v51_vwap.sh
```

## v5.1 미체결 주문 일괄 취소

주의: 이 명령은 유니버스 심볼의 미체결 주문을 심볼 단위로 취소합니다. 수동 주문도 해당 심볼이면 취소될 수 있습니다.

```bash
bash cancel_v51_vwap_orders.sh
```

## 주요 설정

```env
V51_LEVERAGE=3
V51_TOTAL_CAPITAL_RATIO=0.30
V51_PER_ORDER_CAPITAL_RATIO_CAP=0.003
V51_MAX_ORDER_NOTIONAL_USDT=200
V51_UNIVERSE_COUNT=20
V51_MAX_OPEN_POSITIONS=3
V51_BAND_STD_MULT=2.0
V51_ADX_MAX=20
V51_TP_PCT=0.006
V51_SL_PCT=0.003
V51_SHOCK_PCT=0.15
V51_SHOCK_COOLDOWN_MINUTES=10
V51_DAILY_TARGET_PCT=2.0
V51_DAILY_LOSS_PCT=1.0
```

## 주의

이 전략은 초단타/지정가/다종목 전략이라 수수료, 체결 실패, API 지연, post_only 미체결, 급추세장에 민감합니다. 실전 시작 전 반드시 `once` 결과와 로그를 확인하세요.
