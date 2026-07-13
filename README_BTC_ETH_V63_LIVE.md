# BTC/ETH Quant v6.3.2 Dual Live

> Bitget UTA / BTCUSDT + ETHUSDT / 5배 / 격리마진 / 자동 실주문 가능
>
> 설치 직후에는 **SHADOW 모드**입니다. API 키 교체, 계정 점검, 보호주문 어댑터 검사, 4중 확인 및 `LIVE_ARMED` 파일이 모두 갖춰져야 실주문이 열립니다.

## 1. 무엇이 달라졌는가

v6.2의 데이터 품질 검사, 이벤트 위험 필터, 레짐과 트리거 분리, Anti-Chase를 유지하면서, 기존에 놓쳤던 횡보·전환 구간의 짧은 방향성을 별도 `IMPULSE` 진입으로 처리합니다.

동시에 BTCUSDT와 ETHUSDT를 감시하며, 두 종목이 동시에 조건을 만족하면 다음 점수를 합쳐 한 종목을 먼저 선택합니다.

1. 방향 트리거와 반대 방향 대비 edge
2. ATR 대비 단기 가격 이동
3. 최근 5분 거래량 z-score
4. 24시간 거래대금 ÷ 시가총액 대용치

한 사이클에는 신규 주문을 최대 한 건만 보냅니다. 다음 사이클에도 두 번째 종목의 조건이 유지되고 계좌 위험 한도가 남아 있으면 두 번째 종목에 진입할 수 있습니다.

## 2. 50% / 50% 배분의 실제 의미

`bucket_margin_ratio=0.50`은 BTC와 ETH가 각각 시드의 최대 50%를 사용할 수 있다는 **종목별 상한**입니다. 매 거래마다 시드의 50%를 증거금으로 강제로 투입하지 않습니다.

5배에서 BTC 50% + ETH 50%를 모두 증거금으로 쓰면 총 명목 노출이 시드의 5배가 됩니다. 두 종목이 동시에 1% 역행하면 수수료·슬리피지·펀딩 전에도 계좌 기준 약 5% 손실이 발생할 수 있습니다. 따라서 주문 수량은 손절폭으로 역산하고 다음 상한을 모두 적용합니다.

- 첫 번째 일반 TREND 포지션 계획손실: 계좌자산의 **0.60%**
- 두 번째 포지션 계획손실: 계좌자산의 **0.40%**
- IMPULSE: 위 위험의 **50%**
- 총 초기증거금 상한: 계좌자산의 **40%**
- 총 명목 노출 상한: 계좌자산의 **2.0배**
- 종목별 증거금 상한: 계좌자산의 **50%**
- 레버리지: **5배 고정**
- 마진: **격리마진 고정**

예: 자산 1,000 USDT, BTC 손절폭 1%, 첫 TREND 위험 0.60%라면 계획손실은 6 USDT, 명목 주문금액은 약 600 USDT, 5배 필요 증거금은 약 120 USDT입니다. “50% 배분”은 최대치이지 실제 주문액이 아닙니다.

## 3. 진입 방식

### TREND

큰 추세와 단기 트리거가 함께 정렬될 때 진입합니다.

- 방향 레짐 55 이상
- 방향 트리거 44 이상
- 반대 방향 대비 edge 14 이상
- 1시간 EMA20에서 1.25 ATR 이내
- 이벤트 HARD BLOCK 아님

### IMPULSE

RANGE 또는 TRANSITION에서도 순간적으로 가격·거래량·OI가 같은 방향으로 붙을 때 제한적으로 진입합니다.

- 방향 트리거 30 이상
- edge 16 이상
- 최근 20개 완료 5분봉 고가 또는 저가 돌파
- 약 45분 이동이 1시간 ATR의 0.50배 이상
- 5분 거래량 z-score 1.20 이상
- OI 15분 변화가 방향과 일치하거나, 거래량 z 2.0 이상과 강한 가격 이동이 함께 나타나는 예외적 테이프
- 종가가 돌파 방향 쪽에 위치하고 반대 꼬리가 과도하지 않음
- 2회 연속 확인
- 1시간 EMA20에서 1.55 ATR 이내

IMPULSE는 빠른 움직임을 잡되 추격매수를 줄이기 위해 일반 TREND 위험의 절반만 사용합니다.

## 4. 뉴스는 방향 명령이 아니다

뉴스는 **단독 매수·매도 신호로 사용하지 않습니다.**

- 좋은 뉴스가 나와도 가격·거래량·OI가 상승으로 반응하지 않으면 롱하지 않습니다.
- 나쁜 뉴스가 나와도 가격이 버티거나 상승하면 숏하지 않습니다.
- 최소 2개 독립 제공자와 실제 시장 반응이 확인될 때만 트리거에 최대 ±6점을 보조합니다.
- 뉴스 피드가 오래되면 방향 점수는 사용하지 않고 신규 주문 크기를 35%로 축소합니다.

이벤트 위험 단계:

- risk 30~54: 주문 크기 75%
- risk 55~74: 주문 크기 40%
- risk 75 이상: 신규 진입 금지
- USDT/USDC 디페그, 신뢰 가능한 대형 거래소 지급불능·전체 출금동결·중대한 해킹, 체인 정지 같은 구조적 위험: 즉시 HARD BLOCK 및 관리 중 포지션 청산 가능

v6.2는 이벤트 상태와 원문 수집을 계속 공급합니다. v6.3.2은 v6.2 주문 기능을 켜는 방식이 아니라 별도의 실주문 실행기입니다.

## 5. 손절·익절·청산

진입 주문에 mark price 기준 시장가 TP/SL을 함께 넣고, 체결 뒤 거래소 전략 주문 목록에서 보호주문이 실제 생성되었는지 확인합니다. Bitget UTA가 TP와 SL을 한 개 TPSL 행에 함께 반환하는 경우와 두 개 행으로 반환하는 경우를 모두 검사합니다. 보호주문을 확인하지 못하면 포지션을 긴급 시장가 청산하고 신규 진입을 잠급니다.

### BTCUSDT

- 손절: `1.05 × ATR1H`
- 최소 손절폭 0.55%, 최대 손절폭 1.00%

### ETHUSDT

- 손절: `1.10 × ATR1H`
- 최소 손절폭 0.70%, 최대 손절폭 1.40%

### 익절

- TREND: 2.0R
- IMPULSE: 1.6R

### 추가 로컬 청산

- 25분이 지나도 최대 유리 움직임이 0.25R 미만이고 신호가 약화되면 종료
- IMPULSE 최대 4시간, TREND 최대 8시간
- MFE 1.10R 이후 최고·최저점에서 0.85 ATR 되돌리면 종료
- 반대 트리거가 2회 연속 우세하면 종료
- 구조적 이벤트 HARD BLOCK이면 종료

물타기, 마틴게일, 손실 포지션 추가매수는 없습니다. 시장가 주문은 급변 시 계획 가격보다 불리하게 체결될 수 있으며, 거래소·네트워크 장애 시 보호 기능이 지연될 수 있습니다.

## 6. 계좌 방어

- 일 손실 2%: 그날 신규 진입 중지
- 주 손실 5%: 해당 주 신규 진입 중지
- 최고자산 대비 6% 하락: 신규 진입 중지
- 1회 손실 후 60분 대기
- 2연속 손실 후 240분 대기
- 종목별 하루 최대 3회 진입
- 최대 동시 포지션 2개, 종목당 1개
- 알 수 없는 포지션·외부 주문·체결 불확실성 발견 시 신규 진입 금지
- 포지션 소실을 2개 사이클 연속 확인하고 거래소 청산 이력과 대조한 뒤에만 종료로 기록
- 전용 계좌 원칙: 다른 USDT 선물 포지션이나 일반·전략 미체결 주문이 있으면 LIVE arm과 신규 진입을 차단
- `clientOid`로 주문 상세를 재조회하며 API 접수 응답만으로 체결로 간주하지 않음

## 7. 설치

패치 압축을 홈 디렉터리에 푼 뒤 프로젝트 루트에서 실행합니다.

```bash
tar -xzf ~/v63_btc_eth_live_release_6.3.2.tar.gz -C ~/
cd ~/index-sniper-pro
bash ~/v63_btc_eth_live_release_6.3.2/apply_v63_btc_eth_live.sh --systemd
```

설치 결과:

- v6.3.2 systemd 서비스는 시작되지만 SHADOW 상태
- v6.2는 중지하지 않음
- 평문 자격증명 파일이 발견되면 설치는 가능하지만 LIVE arm은 차단
- 기존 대상 파일과 `.env`는 `local_backups/v63_install_*`에 백업
- Python 컴파일과 내부 self-test를 통과해야 설치 완료

## 8. 실전 전 필수 보안 조치

코드·채팅·공개 저장소에 한 번이라도 노출된 API 키·시크릿·패스프레이즈·텔레그램 토큰은 폐기 대상으로 간주합니다.

1. Bitget에서 기존 API 키 삭제
2. 새 API 키 생성
3. 읽기 + 선물거래 권한만 허용
4. 출금 권한 금지
5. 서버 공인 IP만 화이트리스트
6. Telegram BotFather에서 기존 토큰 폐기 후 새 토큰 발급
7. 새 값만 `.env`에 기록

```bash
cd ~/index-sniper-pro
nano .env
```

필수 변수:

```text
BITGET_API_KEY=새키
BITGET_SECRET_KEY=새시크릿
BITGET_PASSPHRASE=새패스프레이즈
TELEGRAM_TOKEN=새텔레그램토큰
TELEGRAM_CHAT_ID=채팅ID
```

Python 파일에 자격증명을 직접 적지 않습니다.

## 9. 계정 설정과 SHADOW 점검

BTC/ETH를 포함한 모든 USDT 선물 포지션과 일반·전략 미체결 주문이 없는 전용 계좌에서 실행합니다.

```bash
cd ~/index-sniper-pro
bash stop_v63_dual_live.sh
bash setup_v63_account.sh
bash doctor_v63_dual_live.sh --prearm
bash run_v63_once.sh --force-shadow
bash start_v63_dual_live.sh
```

`setup_v63_account.sh`는 다음을 적용하고 다시 조회해 검증합니다.

- hedge mode
- BTCUSDT 격리 5배
- ETHUSDT 격리 5배

## 10. 실전 활성화

아래 문구는 보안 조치를 실제로 끝냈다는 확인입니다.

```bash
cd ~/index-sniper-pro
bash arm_v63_dual_live.sh \
  START_V63_DUAL_LIVE_5X_BTC_ETH \
  I_ROTATED_ALL_EXPOSED_KEYS \
  API_HAS_NO_WITHDRAW_PERMISSION \
  API_IP_WHITELISTED
```

활성화 스크립트는 다음을 다시 검사합니다.

- 평문 자격증명 없음
- API·시장 데이터 정상
- 모든 USDT 선물 포지션과 미체결 주문 없음
- hedge mode
- BTC/ETH 격리 5배
- 보호 TP/SL 주문 페이로드 어댑터 정상
- v6.2 이벤트 및 원문 소스 접근 여부

조건을 통과하지 못하면 LIVE로 전환되지 않습니다.

## 11. 운영 명령어

```bash
# 현재 상태
bash status_v63_dual_live.sh

# 로그
journalctl -u index-sniper-v63.service -f
# 또는
tail -f logs/v63-dual-live.log

# 신규 진입만 일시 중지: 기존 포지션 관리는 계속
bash pause_v63_entries.sh

# 신규 진입 재개
bash resume_v63_entries.sh

# 포지션이 없을 때 SHADOW로 전환
bash disarm_v63_dual_live.sh

# BTC/ETH 일반·전략 주문 취소 + 시장가 전량 청산 + 신규 진입 중지
bash panic_flat_v63.sh FLAT_BTC_ETH_NOW

# 30일 성과
bash report_v63_dual_live.sh 30

# 업로드용 결과 묶음
bash collect_v63_results.sh 30
```

엔진 정지는 포지션 청산 명령이 아닙니다. 열린 포지션이 있을 때 서비스를 중지하면 거래소 TP/SL은 남지만 no-followthrough·time-stop·ATR trailing 관리는 멈춥니다. 긴급 종료에는 `panic_flat_v63.sh`를 사용하고 거래소 앱에서 포지션과 미체결 주문이 0인지 직접 확인합니다.

## 12. 결과 파일

```text
data/v63_dual_live/state.json
data/v63_dual_live/snapshots.jsonl
data/v63_dual_live/events.jsonl
data/v63_dual_live/trades.csv
logs/v63-dual-live.log
```

몇 주 후 다음 명령으로 비밀값을 제외한 업로드용 파일을 만듭니다.

```bash
bash collect_v63_results.sh 30
```

실전 자동매매는 원금 손실과 청산 위험이 있습니다. 이 버전의 임계값은 현재 보유 자료를 바탕으로 만든 초기 실전값이지 수익을 보장하는 최적값이 아닙니다. 첫 몇 주 결과에서는 승률만 보지 말고 순손익, profit factor, 평균 R, 최대 손실 연속 횟수, 슬리피지, TREND/IMPULSE별 성과를 함께 평가해야 합니다.


## 13. v6.3.2 수정사항

- 업로드된 묶음에 없던 프로젝트 내부 position/risk/formatting 모듈에 의존하지 않도록 필요한 파서를 실행기 내부에 포함
- Bitget UTA 공식 응답의 `takeProfit`/`stopLoss` 보호주문 필드 검사 추가
- TP와 SL이 한 개 TPSL 행에 함께 표시되는 공식 응답 형식 self-test 추가
