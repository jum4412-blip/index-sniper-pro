# Index Sniper Pro v1.6 Observer

고정 프로젝트명: `index-sniper-pro`

## 핵심 원칙

- 주문/체결/포지션 관리는 계속 Bitget UTA에서 수행한다.
- `BTCUSDT`는 Bitget 캔들로 신호를 만든다.
- `SP500USDT`, `NDX100USDT`는 외부 장기 차트 데이터로 추세/ATR/변동성 돌파 기준을 만든다.
- 외부 데이터는 판단용이고, 최종 돌파 확인과 주문 가격은 Bitget 가격을 기준으로 한다.
- 외부 데이터가 실패하거나 오래됐거나 Bitget 가격과 괴리가 너무 크면 해당 심볼은 HOLD로 처리하고 거래하지 않는다.
- v1.6은 주문 로직을 바꾸지 않는다. 관찰/기록/설명 기능만 강화한다.

## v1.6에서 추가된 관찰 기능

봇은 매 루프마다 각 종목에 대해 아래 정보를 저장한다.

- 지금 봇이 롱을 기다리는지, 숏을 기다리는지, 데이터 오류로 막혔는지
- 현재가에서 롱 기준가까지 남은 거리
- 현재가에서 숏 기준가까지 남은 거리
- 감시 중인 타점까지 남은 거리와 퍼센트
- ATR 기준 남은 거리
- trend rejected breakout 기록
- 생존형 점수, 차단된 조건, 수량 계획

저장 위치:

```text
data/market_observer.json      # 최신 관찰 스냅샷. 사람이 보기 좋음.
logs/signal_observer.jsonl     # 매 루프/종목별 JSONL 누적 기록. 분석용.
logs/signal_distance.csv       # 타점까지 남은 거리 CSV. 엑셀/통계 분석용.
logs/events.jsonl              # 주문/차단/오류 이벤트 로그.
logs/trades.csv                # 실제 주문 또는 dry-run 주문 기록.
```

확인 명령:

```bash
bash view_observer.sh
```

현재 상태까지 같이 보려면:

```bash
bash status_sniper.sh
```

실주문 없이 관찰 스냅샷만 새로 만들려면:

```bash
bash run_observer_snapshot.sh
```

`run_observer_snapshot.sh`는 강제로 `DRY_RUN=true`로 실행한다. 실주문은 나가지 않는다.

## 외부 데이터 기본값

```env
EXTERNAL_SIGNAL_ENABLED=true
EXTERNAL_SIGNAL_SYMBOLS=SP500USDT,NDX100USDT
EXTERNAL_PROVIDER_ORDER=STOOQ,YAHOO
EXTERNAL_YAHOO_SYMBOL_MAP=SP500USDT:ES=F|^GSPC,NDX100USDT:NQ=F|^NDX
EXTERNAL_STOOQ_SYMBOL_MAP=SP500USDT:^spx,NDX100USDT:^ndx
EXTERNAL_YAHOO_RANGE=2y
EXTERNAL_YAHOO_INTERVAL=1d
EXTERNAL_TIMEOUT_SECONDS=10
EXTERNAL_CANDLE_LIMIT=260
EXTERNAL_MAX_STALENESS_HOURS=120
EXTERNAL_MAX_SCALE_DEVIATION_PCT=20
```

## v1.6 관찰 설정

```env
OBSERVATION_ENABLED=true
OBSERVATION_LATEST_PATH=data/market_observer.json
OBSERVATION_JSONL=signal_observer.jsonl
OBSERVATION_CSV=signal_distance.csv
OBSERVATION_NEAR_TARGET_PCT=0.20
```

## 업데이트 순서

실전 봇이 켜져 있다면 먼저 멈춘다.

```bash
cd ~/index-sniper-pro
bash stop_sniper.sh
```

GitHub 업로드 후 EC2에서:

```bash
cd ~/index-sniper-pro
git pull
bash install.sh
```

구버전 잔재 확인:

```bash
grep -R "reduceOnly" -n index_sniper main.py || true
```

정상은 아무것도 안 나오는 것이다.

실주문 없는 관찰/외부 신호 점검:

```bash
bash run_external_signal_check.sh
bash run_observer_snapshot.sh
bash view_observer.sh
```

실전 프리플라이트:

```bash
bash run_live_preflight.sh
```

정상 확인 후 실전 루프:

```bash
bash start_live_guarded.sh
bash status_sniper.sh
```

## 주의

`DRY_RUN=false` 상태에서 `start_live_guarded.sh`를 실행하면 실제 신호 발생 시 주문이 나간다. 실전 전에는 반드시 `run_external_signal_check.sh`, `run_observer_snapshot.sh`, `run_live_preflight.sh`를 먼저 통과시킨다.
