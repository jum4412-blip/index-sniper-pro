# Index Sniper Pro v1.4 External Signal Engine

고정 프로젝트명: `index-sniper-pro`

## 핵심 원칙

- 주문/체결/포지션 관리는 계속 Bitget UTA에서 수행한다.
- `BTCUSDT`는 Bitget 캔들로 신호를 만든다.
- `SP500USDT`, `NDX100USDT`는 외부 장기 차트 데이터로 추세/ATR/변동성 돌파 기준을 만든다.
- 외부 데이터는 판단용이고, 최종 돌파 확인과 주문 가격은 Bitget 가격을 기준으로 한다.
- 외부 데이터가 실패하거나 오래됐거나 Bitget 가격과 괴리가 너무 크면 해당 심볼은 거래하지 않는다.

## 외부 데이터 기본값

```env
EXTERNAL_SIGNAL_ENABLED=true
EXTERNAL_SIGNAL_SYMBOLS=SP500USDT,NDX100USDT
EXTERNAL_PROVIDER_ORDER=YAHOO,STOOQ
EXTERNAL_YAHOO_SYMBOL_MAP=SP500USDT:ES=F,NDX100USDT:NQ=F
EXTERNAL_STOOQ_SYMBOL_MAP=SP500USDT:^spx,NDX100USDT:^ndx
EXTERNAL_YAHOO_RANGE=2y
EXTERNAL_YAHOO_INTERVAL=1d
EXTERNAL_TIMEOUT_SECONDS=10
EXTERNAL_CANDLE_LIMIT=260
EXTERNAL_MAX_STALENESS_HOURS=120
EXTERNAL_MAX_SCALE_DEVIATION_PCT=20
```

기본적으로 Yahoo의 `ES=F`, `NQ=F`를 우선 사용한다. 실패하면 Stooq의 `^spx`, `^ndx`를 시도한다.

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

`.env`에 위 외부 데이터 설정을 추가한다.

실주문 없는 외부 신호 점검:

```bash
bash run_external_signal_check.sh
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

`DRY_RUN=false` 상태에서 `start_live_guarded.sh`를 실행하면 실제 신호 발생 시 주문이 나간다. 실전 전에는 반드시 `run_external_signal_check.sh`와 `run_live_preflight.sh`를 먼저 통과시킨다.
