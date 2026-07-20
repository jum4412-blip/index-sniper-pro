# VWAP VIDEO TOP10 LIVE 3X

Bitget UTA용 실전 실행기다. 사용자가 제공한 영상의 **최종 ADX + VWAP 밴드 횡보장 전략**만 구현한다.

## 고정 전략

- 종목: CoinGecko 시가총액 순으로 내려가며 Bitget USDT 무기한 선물에서 거래 가능한 비스테이블 코인 10개
- 계정 모드: `hedge_mode`
- 마진 모드: `isolated`
- 레버리지: `3x`
- 주문 수량: 각 상품의 거래소 최소 가능 수량
- VWAP: 실시간 체결 가격 × 체결량의 UTC 일간 누적 VWAP
- 워밍업: 10분
- 밴드: VWAP ± 체결량 가중 표준편차 × `2.0`
- ADX: 1분봉 ADX(14)
- 진입 허용: ADX ≤ 20
- 롱: 하단 밴드에 `post_only` 지정가
- 숏: 상단 밴드에 `post_only` 지정가
- 익절: 진입가 대비 +0.6% 수익 지점에 지정가
- 손절: 진입가 대비 -0.3% 지점에서 시장가
- VWAP 회귀: 현재가가 VWAP에 닿으면 시장가 청산
- 급변: 5초 절대변동률 ≥ 0.15%이면 미체결 진입 지정가 취소 후 10분 대기
- 추가 EMA·뉴스·스프레드·일손실·거래횟수·최대보유·트레일링 필터 없음

영상은 표준편차 배수를 숫자로 공개하지 않았기 때문에 설정 파일의 `band_std_mult` 기본값은 `2.0`이다. 다른 전략 조건은 고정 계약으로 잠겨 있다.

## 설치 후 상태

설치만으로 주문하지 않는다. 기본 상태는 `DISARMED`다.

필수 파일:

- `index_sniper/vwap_video_live_v1.py`
- `config/vwap_video_live_v1.json`
- `data/vwap_video_live_v1/`
- `research/vwap_video_live_v1_trades.csv`
- `research/vwap_video_live_v1_events.jsonl`
- `logs/vwap-video-live-v1.log`

## 적용 순서

```bash
cd ~/index-sniper-pro

# 1. 시총 상위 거래 가능 10종목 갱신
bash refresh_vwap_video_top10.sh

# 2. 일반 점검
bash doctor_vwap_video_live.sh

# 3. 계정이 완전히 비어 있을 때 hedge + isolated 3x 설정
bash setup_vwap_video_account.sh

# 4. ARM 전 최종 점검
bash doctor_vwap_video_live.sh --prearm

# 5. 실주문 활성화
bash arm_vwap_video_live.sh \
  START_VWAP_VIDEO_TOP10_LIVE_3X \
  I_CONFIRM_REAL_ORDERS_MINIMUM_QTY \
  API_HAS_NO_WITHDRAW_PERMISSION \
  DEDICATED_SUBACCOUNT_ONLY

# 6. 실행
bash start_vwap_video_live.sh
```

## 상태·중지

```bash
bash status_vwap_video_live.sh

tail -f logs/vwap-video-live-v1.log

bash disarm_vwap_video_live.sh
bash stop_vwap_video_live.sh
```

`disarm`은 신규 진입 주문을 취소하지만 현재 포지션의 거래소 TP/SL 관리는 유지한다. `stop`은 엔진을 종료하고 미체결 진입 주문을 취소한다. 이미 열린 포지션이 있으면 거래소에 등록된 TP/SL은 남지만 VWAP 시장가 회귀 청산은 엔진이 정지한 동안 실행되지 않는다.

## 실주문 없이 확인

```bash
bash start_vwap_video_observe.sh
bash status_vwap_video_live.sh
bash stop_vwap_video_live.sh
```

## API 설정

프로젝트 `.env`에 다음 값이 있어야 한다.

```text
BITGET_API_KEY=...
BITGET_SECRET_KEY=...
BITGET_PASSPHRASE=...
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
```

API에는 UTA 조회·관리·거래 권한이 필요하다. 출금 권한은 사용하지 않는다.

## 프로그램 동작 순서

1. 실시간 `publicTrade` 체결을 받아 VWAP과 가중 표준편차 밴드를 갱신한다.
2. 1분봉으로 ADX(14)를 갱신한다.
3. 워밍업 10분이 끝나고 ADX ≤ 20이면 상·하단 밴드에 양방향 maker 지정가를 둔다.
4. 한쪽이 체결되면 반대쪽과 잔량 진입 주문을 취소한다.
5. 체결 수량에 대해 +0.6% 지정가 TP와 -0.3% 시장가 SL을 유지한다.
6. 가격이 현재 VWAP에 닿으면 시장가로 포지션을 닫는다.
7. 5초 변동이 0.15% 이상이면 진입 지정가를 취소하고 10분 뒤 재개한다.

## 현재 10개 확인

```bash
cat data/vwap_video_live_v1/universe_latest.json
```
