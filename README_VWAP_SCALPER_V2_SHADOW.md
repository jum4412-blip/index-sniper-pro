# VWAP Scalper v2.0 SHADOW

영상의 핵심 아이디어를 Bitget UTA v3 환경에 맞춰 별도 모드로 구현한 **실시간 paper/shadow 스캘핑 엔진**입니다.

## 핵심 전략

- Bitget `publicTrade` WebSocket 체결 데이터로 30분 롤링 VWAP 계산
- 체결량 가중 표준편차로 상·하단 밴드 계산
- 하단 밴드에는 가상 LONG 지정가, 상단 밴드에는 가상 SHORT 지정가
- ADX가 낮고 EMA 이격·5분 변동·스프레드가 작을 때만 주문 대기
- 5초 동안 0.15% 이상 급변하면 가상 주문을 모두 취소하고 10분 대기
- 진입 후 VWAP 회귀, 고정 +0.6%, -0.3% 손절, 최대 30분 보유를 비교해 청산
- Maker/Taker 수수료, Taker 슬리피지, 지정가 대기열을 보수적으로 반영

## 영상 그대로 복제하지 않은 부분

영상의 단순 규칙은 추세 전환 때 큰 손실이 날 수 있고, 표시된 짧은 수익 구간만으로 장기 기대값을 판단할 수 없습니다. 이번 버전은 다음을 추가했습니다.

- ADX뿐 아니라 EMA20/EMA60 이격과 5분 가격 변화 필터
- 실시간 스프레드 필터
- 지정가가 가격에 닿았다고 바로 체결시키지 않고 체결량이 주문량의 3배 이상 지나야 가상 체결
- 거래비용 차감 후 순손익 기록
- 일일 거래 횟수와 일일 손실 제한
- 최대 동시 paper 포지션 2개
- v6.2/v6.3 이벤트 상태가 최신이고 위험 70 이상이면 신규 가상 진입 차단

## 안전 범위

이 버전에는 다음 기능이 **의도적으로 없습니다**.

- 거래소 주문 전송
- 실제 포지션 생성
- 실제 주문 취소
- API 키 사용
- 실전 ARM 명령

따라서 기존 v6.3 LIVE와 같은 서버에서 병렬 실행해도 계좌 주문에는 손대지 않습니다. 다만 나중에 LIVE 실행기를 만들 때는 v6.3과 같은 계좌에서 동시에 운용하면 안 되고 별도 서브계정을 사용해야 합니다.

## 설치

```bash
cd ~/index-sniper-pro
bash apply_vwap_scalper_v2_shadow.sh
```

## 점검

```bash
bash doctor_vwap_scalper_v2.sh
bash run_vwap_scalper_v2_once.sh
```

`once`는 최근 공개 체결 100건의 VWAP만 확인합니다. 실제 shadow 전략은 15분 워밍업 후 시작됩니다.

## 시작·중지

```bash
bash start_vwap_scalper_v2_shadow.sh
bash status_vwap_scalper_v2.sh
bash stop_vwap_scalper_v2_shadow.sh
```

실시간 로그:

```bash
tail -f logs/vwap-scalper-v2-shadow.log
```

## 성과 리포트

```bash
bash report_vwap_scalper_v2.sh 7
bash report_vwap_scalper_v2.sh 30
```

확인할 항목:

- 비용 차감 순손익
- 평균 R과 profit factor
- 심볼별 승률·순손익
- `vwap_touch`, `fixed_tp`, `stop`, `max_hold`별 성과
- 평균 MFE/MAE
- 수수료가 총 gross 수익의 몇 %인지

## 결과 묶음

```bash
bash collect_vwap_scalper_v2_results.sh 7
```

생성되는 `vwap_scalper_v2_bundle_*.tar.gz`를 업로드하면 분석할 수 있습니다.

## 기본 설정

`config/vwap_scalper_v2.json`

```text
symbols: BTCUSDT, ETHUSDT, SOLUSDT
VWAP window: 30분
warm-up: 15분
band: 1.8 weighted standard deviation
ADX 최대: 18
EMA20/EMA60 최대 이격: 0.30%
5분 최대 절대변동: 0.35%
shock: 5초 0.15% → 10분 대기
paper notional: 종목당 100 USDT
fixed TP: 0.6%
SL: 0.3%
max hold: 30분
daily max trades: 30
max positions: 2
maker fee model: 2 bps
taker fee model: 6 bps
taker slippage model: 1.5 bps
```

수수료는 계정 등급에 따라 다를 수 있으므로 실제 계정 체결 수수료에 맞게 설정해야 합니다.

## LIVE 전환 전 최소 조건

현재 patch에는 LIVE가 없습니다. 후속 LIVE 실행기는 다음을 만족한 뒤 별도로 제작하는 편이 안전합니다.

```text
최소 7일 이상 연속 작동
완료된 독립 paper 거래 100건 이상
비용 차감 평균 R > 0
profit factor > 1
stop 손실이 특정 종목·시간대에 집중되지 않음
WebSocket 재연결 후 상태 복구 검증
v6.3과 분리된 전용 서브계정 준비
```

하루 2,000회 거래를 목표로 하지 않습니다. 기회가 없는 추세장·고변동 구간에서는 거래하지 않는 것이 이 전략의 일부입니다.
