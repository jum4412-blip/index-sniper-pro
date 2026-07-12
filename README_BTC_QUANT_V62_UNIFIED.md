# BTC Quant v6.2 Unified + Event Risk v6.2

## 운영 결론

v6.2로 전환한 뒤에는 아래 개별 loop를 **동시에 돌리지 않습니다.**

```text
v4.1 observer
v4.2 observer
v6.0 Signal Lab
v6.1 Event Risk Telegram loop
```

v6.2 한 프로세스가 시장 데이터, 데이터 품질, Regime, Entry Trigger,
Anti-Chase, 이벤트 원문 수집 호출, 이벤트 재판정, episode, paper trade,
Telegram 알림을 통합합니다.

기존 로그·CSV·SQLite·상태 파일은 삭제하지 않습니다. 초기 warm-up과
전후 비교를 위해 읽기 전용으로 보존합니다.

> v6.2는 강제로 `paper_only=True`이며 주문 함수가 없습니다.

---

## 왜 별도 알람을 끄는가

기존 loop를 같이 두면 다음 문제가 생깁니다.

- 같은 시장 상태를 v4.1/v4.2/v6.0이 각각 반복 전송
- 같은 추세 구간을 여러 독립 신호로 잘못 집계
- 캔들·펀딩·OI API 요청 중복
- v6.1의 구형 risk 100과 v6.2 교정 결과 충돌
- Telegram 메시지만 보고 어느 엔진의 최종 판단인지 혼동

`switch_to_v62_unified.sh`는 알려진 screen 세션과 orphan legacy Python
loop를 중지한 뒤, self-test와 1회 무알림 실데이터 점검에 성공해야만
v6.2를 시작합니다.

---

## v6.2 구조

### 1. Data Quality Gate

- Funding: median/MAD 기반 robust z-score, ±3 제한
- Funding: 최소 표본 전에는 점수 기여 0
- OI: API path와 필드명을 source fingerprint로 기록
- OI: source 변경 + 스케일 변화, 5분 급점프, 최근 중앙값 이탈 검사
- OI: ±18% hard jump 또는 중앙값 대비 30% 이상 이탈은 시장 급변 중에도 점수에서 제외
- OI가 불량이어도 전체 엔진을 죽이지 않고 `OI 제외`로 계속 관찰
- 5m/15m/1H/4H closed candle 신선도 검사
- 데이터 치명 오류는 `DATA_INVALID`

### 2. Regime와 Entry Trigger 분리

```text
Regime: LONG_REGIME / SHORT_REGIME / RANGE_REGIME / TRANSITION
Trigger: ENTRY_READY / WATCH / WAIT / CHASE_BLOCKED / EVENT_RISK_WAIT
```

높은 추세 점수가 곧 좋은 매수가격이라는 기존 혼동을 제거했습니다.
`STRONG_LONG`이라는 추격 유발 표현도 사용하지 않습니다.

### 3. Anti-Chase

- 1H EMA20 대비 ATR 이격
- episode 시작 이후 이미 이동한 ATR
- 높은 거래량 대비 작은 가격 반응
- 과밀 펀딩 + OI 증가
- ATR percentile 과열

사전 trigger가 높아도 추격 penalty가 크면 `LONG_CHASE_BLOCKED` 또는
`SHORT_CHASE_BLOCKED`가 됩니다.

### 4. Signal Episode

같은 추세를 60분마다 새 신호로 만들지 않습니다.

```text
SETUP_DETECTED
SETUP_ACTIVE
ENTRY_TRIGGERED
POSITION_ACTIVE
CHASE_BLOCKED
EVENT_RISK_WAIT
CLOSED
```

한 episode에는 기본적으로 한 번의 paper entry만 허용합니다.

### 5. ATR Paper Trade

- ATR stop/target
- trailing stop
- no-followthrough 종료
- regime invalidation 종료
- confirmed event hard block 종료
- MFE/MAE와 exit reason 저장

---

## Event Risk v6.2 교정 내용

v6.1의 provider adapter는 v6.2 프로세스가 내부 주기로 호출하지만,
**v6.1의 risk score와 Telegram 판단은 사용하지 않습니다.** v6.2가
raw title/body/source/symbol/time을 다시 판정합니다.

### 오분류 교정

- `attack` 단일 단어로 전쟁 판정 금지
- `approval` 단일 단어로 규제 승인 판정 금지
- phishing/ wallet drain/ protocol exploit는 security 우선
- 군사 행동 구문과 국가·지역 문맥이 동시에 있어야 `macro_war`
- 영어·이탈리아어·알바니아어 등 동일 사건은 entity/action/time으로 묶음

### 관련성 분리

```text
GLOBAL
BTC
SYMBOL
GENERAL
```

알트 상폐·특정 네트워크 입출금 중단은 해당 심볼 위험으로 기록하며,
BTC 전체 hard block으로 사용하지 않습니다.

### Freshness 교정

- Bitget/RSS/GDELT/CryptoPanic 중 최소 1개 core feed가 45분 이내 정상이어야 유효
- DeFiLlama 과거 해킹 목록만 성공했다고 Event Guard를 정상으로 보지 않음
- 모든 core feed가 오래되면 `STALE_NO_FILTER`
- 오래된 risk 100을 계속 재사용하지 않음
- legacy DeFiLlama 전체-history 호출은 live loop에서 비활성화; 기존 DB 기록은 보존

### Hard block 교정

일반 전쟁 기사 한 건만으로는 hard block이 되지 않습니다.

```text
다중 출처 군사 긴장 + BTC 시장 스트레스 없음
→ CAUTION, size 조절

다중 독립 출처 + 높은 BTC 관련성 + 실제 가격/거래량/OI 스트레스
→ HARD_BLOCK

USDT/USDC 디페그, 신뢰 가능한 대형 CEX 해킹·전체 출금동결,
BTC 네트워크 심각 장애
→ 구조적 HARD_BLOCK 가능
```

---

## 설치 및 전환

설치 파일을 프로젝트 루트에 올린 뒤:

```bash
cd ~/index-sniper-pro
bash apply_v62_unified_quant.sh
```

설치는 파일과 설정만 추가하며 기존 loop를 즉시 끄지 않습니다.

### 1. 점검

```bash
bash doctor_v62_unified.sh
```

### 2. 통합 전환

```bash
bash switch_to_v62_unified.sh
```

전환 스크립트가 중지하는 대표 screen:

```text
quant-v41
quant-v42
sniper-signal-lab
v61_event_guard_pro
v61_event_guard_pro_tg
v61_event_guard
```

### 3. 확인

```bash
bash status_v62_unified.sh
bash view_v62_log.sh
```

### 4. 부팅 자동시작

수동 screen으로 최소 몇 cycle 정상 동작을 확인한 다음:

```bash
bash install_v62_systemd.sh
```

systemd와 screen은 동시에 실행되지 않도록 스크립트에서 검사합니다.

---

## 명령어

```bash
# 1회 실데이터 실행
bash run_v62_once.sh

# 알림 없이 1회 점검
bash run_v62_once.sh --no-notify

# screen 시작/중지/상태
bash start_v62_unified.sh
bash stop_v62_unified.sh
bash status_v62_unified.sh

# 리포트
bash report_v62_unified.sh 7

# self-test + API/환경 doctor
bash doctor_v62_unified.sh
bash doctor_v62_unified.sh --no-network

# 로그
bash view_v62_log.sh

# v6.2 중지
bash rollback_v62_unified.sh

# 명시적으로 legacy 재가동
bash rollback_v62_unified.sh --restart-legacy
```

---

## Telegram 정책

다음 변화만 전송합니다.

- 최종 상태 변경
- Data Quality 상태 변경
- Event Risk 단계 변경
- episode 시작·종료
- paper trade 시작·종료
- Event collector의 새로운 오류
- 기본 60분 heartbeat

매 cycle마다 NEUTRAL을 보내지 않습니다.

사용 환경변수:

```text
TELEGRAM_TOKEN 또는 TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
V62_NOTIFY=true
V62_NOTIFY_HEARTBEAT=true
V62_HEARTBEAT_MINUTES=60
```

`.env`를 bash로 `source`하지 않고 python-dotenv로 읽기 때문에 `^GSPC`,
`^NDX` 같은 값이 있어도 이전처럼 shell command 오류를 만들지 않습니다.

---

## 데이터 위치

```text
data/v62_unified/state.json
data/v62_unified/v62.sqlite3
data/v62_unified/snapshots.jsonl
logs/v62-unified.log
```

raw event DB는 기존 위치를 이어서 사용합니다.

```text
data/event_risk_pro/events.sqlite3
```

v6.2 DB 테이블:

```text
snapshots
episodes
paper_trades
alerts
event_overlays
```

---

## 초기 운영 원칙

현재 threshold는 업로드된 약 3일 자료에서 발견된 구조적 오류를 막기 위한
보수적 초기값이지, 수익 최적값이 아닙니다. 최소 2~4주 또는 방향별 독립
episode 30개 이상을 paper로 모은 뒤 score calibration과 실제 주문 여부를
다시 판단합니다. 특히 SHORT 표본은 아직 부족하므로 계속 paper-only입니다.
