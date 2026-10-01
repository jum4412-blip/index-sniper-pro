현재 제공 모형은 매매 가능한 상태가 0개여서 주문 없이 WAIT 합니다.

BTC·ETH 가격·거래량 확률봇 v1
BTCUSDT·ETHUSDT 각각 증거금 배분400 USDT, 교차5배, 목표 포지션2,000 USDT.
초기 계좌 평가액과 사용 가능 담보 최소910 USDT: 두 종목 증거금800 + 현금 여유100 + 수수료 여유10.
교차계정의 실제 자산은 공유됩니다. 종목별400 배분은 봇 내부 원장입니다.

이 프로그램은 실전 주문 경로를 포함하지만 수익이 검증된 적극 매매 전략이 아닙니다.
확률 통과 상태가0인 현재 모형으로 시작하면 시세 관측/상태/알림만 유지합니다.
현재 제공된 과거 모형은500 USDT 증거금 규모로 계산돼 400 USDT 실매매 설정과 다릅니다.
새 모형은2,000 USDT 주문 규모의 표본으로 재계산해 규모 출처를 명시해야 신규 진입이 허용됩니다.
새 자료로 모형을 만들고 다시 검증하기 전까지 통과 상태가 자동으로 생기지 않습니다.
0건 거래의 순손익0과 낙폭0은 거래하지 않은 결과이며 수익성과 강건성 증거가 아닙니다.

이번 기록의 사건70,658개 중 체결·비용 조건 통과47,621개,
후속 평가 가상 사건23,565개, 종목별 비중복 가상 사건17,978개를 따로 봤습니다.
기존 500 USDT 증거금 기준 평가 가상 사건당 평균 순손익은-7.36045 USDT, 확률 통과 사건/가드 적용 거래는0개입니다.
지금 400 USDT 실전 규모로 위 과거 결과를 재산출하지 않았으므로 400 USDT 수익률로 읽으면 안 됩니다.
청산2/3/4R ×60/180/360분의 추가9프로필도 훈련·후속 평가의 확률 기준 통과가0개였습니다.
추가 실험의 사후 상위 수익값으로 실전 설정을 바꾸지 않았습니다. 실전은2R/60분입니다.

전략
1) 완료된5분봉의 이전20봉 고저점 이탈 후 복귀(실패한 돌파) 사건.
2) 이전 고저점 돌파 후 다음5분봉까지 가격이 유지되는 수용 사건.
가격 위치/꼬리/구간 폭과 이전96봉의 기본 거래량 백분위만 사용합니다.
RSI/MACD/EMA/ADX 같은 지표 패키지를 사용하지 않습니다.
사람의 심리를 직접 관측하지 않습니다. 손절 몰림/급한 추격/실패한 돌파를
가격·거래량에서 검사할 수 있는 가설로 바꾸었습니다.

조건별 P(비용 포함 순손익>0)를 베타-이항 축소 추정합니다.
E = p × 평균 순이익 - (1-p) × 평균 순손실.
수수료/스프레드/슬리피지/펀딩은 순이익과 순손실에 이미 들어 있습니다.
단순2R목표라고 손익분기 승률을1/3로 고정하지 않습니다. 만기 청산도 포함합니다.
그룹300/상태80표본/서로 다른30일, 확률 하한>손익분기+2%p,
기대 순R 하한>0을 모두 충족해야 주문 후보가 됩니다. 거래 수를 강제하지 않습니다.
완료봉 끝+60초에서20초 유효창, 실제 시세/비용/여유증거금/손실 가드를 다시 확인합니다.
구조 손절은 신호 가격에서0.25~0.60%; 실제 체결 기준0.20~0.80% 가드.
실제 체결가부터 구조손절까지 거리의2배 목표, 최대60분, 비용 포함 계획손실 최대20 USDT.
급격한 갭/장애 때 실제 손실은 계획손실보다 클 수 있습니다.

설치·운영 (EC2 Ubuntu의 본인 계정, Python3.10이상)
GitHub 설치 절차는 README_GITHUB_EC2_KO.md에 있습니다. 과거 500 USDT용 .run 파일을 사용하지 마세요.
새 설치 폴더에 GitHub main의 btc_eth_probability_v1 코드를 추출합니다. 기존 원장은 덮어쓰지 않습니다.
전체 self-test는 포함된 오프라인 C++ 체결 시험을 빌드하므로 g++가 필요합니다.
처음 설치하거나 venv/g++가 없는 Ubuntu에서 먼저 패키지를 설치합니다:
  sudo apt-get update
  sudo apt-get install -y python3-venv build-essential
GitHub 코드 추출과 기존 디렉터리 충돌 검사는 README_GITHUB_EC2_KO.md 절차대로 진행하세요.
새 설치 폴더가 만들어진 다음:
  cd ~/btc_eth_probability_v1_git
  ./install_dependencies.sh
  ./prob self-test
  ./prob doctor-public

자격증명은 서버에서만 입력합니다. 대화창에 키를 보내지 마세요.
  cp credentials.example.env credentials.env
  chmod 600 credentials.env
  nano credentials.env
템플릿 키: BITGET_API_KEY, BITGET_SECRET_KEY, BITGET_PASSPHRASE,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
  cp connection.example.json connection.json
  nano connection.json
connection.json의 env를 ~/btc_eth_probability_v1_git/credentials.env로 지정하고
legacy_root/trend_root/basic_root/psych_root/trail3_root는 기존 설치의 실제 경로로 확인합니다.
trail3_root 기본값은 ~/eth_larry_trail3_500입니다. 기존 Larry를 원격 종료한 것은 아닙니다.
원하는 경우 기존 .env를 env경로로 명시하여 재사용할 수 있습니다.

기존 봇에서 신규 진입 중지→보유 포지션/미확정 주문 정리→정상 종료를 완료합니다.
이 프로그램은 기존 봇을 자동으로 종료하지 않으며 기존 원장/보유를 발견하면 차단합니다.
기존 Larry v2가 ~/index-sniper-pro에 설치된 경우, EC2에서 아래 순서로 실행하세요.
  cd ~/index-sniper-pro
  ./larry-live pause
먼저 Bitget UTA의 BTC·ETH 실제 보유·미체결 주문이 0인지 확인하세요.
보유가 있으면 기존 관리자에서 보호·청산을 유지한 채 정리한 뒤 다음을 실행하세요.
  ./larry-live stop
  sudo systemctl stop larry-v2-live.service
  sudo systemctl disable larry-v2-live.service
  sudo systemctl is-active larry-v2-live.service
  sudo systemctl is-enabled larry-v2-live.service
마지막 두 명령의 기대 출력은 inactive, disabled입니다.
기존 ETH Larry Trail3도 쓴다면 서비스 이름을 먼저 확인하세요.
  sudo systemctl list-units --all --type=service '*larry*' '*trail3*'
Trail3의 실제 경로·서비스를 확인하고 보유·미체결을 정리한 후 정상 종료해야 합니다.
서비스를 먼저 강제 중지하면 기존 포지션의 서버 관리 청산이 중단될 수 있습니다.
새 프로그램의 충돌 검사는 기존 봇이 아직 실행·진입 허용·보유 중이면 신규 진입을 차단합니다.
  ./prob doctor
doctor는 계좌/보유/기존 봇/텔레그램 설정을 읽고 검사합니다.
교차5배 설정이 맞지 않고 계좌가 평탄할 때:
  ./prob setup
setup은 레버리지/증거금 설정을 쓰는 명령이며 매매 주문을 넣지 않습니다.
단일 Telegram 대화 연결 테스트(이 명령을 실행하면 실제 메시지를 보냅니다):
  ./prob notify-test
현재 BTC·ETH 상황 한 건을 한국어로 확인하고 보내려면:
  ./prob notify-preview
  ./prob notify-now
notify-preview는 로컬 화면 출력, notify-now는 설정된 대화로 실제 전송합니다.
실행 기록이 없거나 오래되었다면 그 사실을 메시지에 표시합니다.

EC2 자동 재시작·부팅 실행 서비스 (선택, 설치만 하고 현재 실행하지 않음):
  ./install_service.sh --user ubuntu --print
  ./install_service.sh --user ubuntu
현재 계정이ubuntu가 아니면 실제 서비스 소유자 이름을 사용합니다.
wrapper는 .venv/bin/python을 서비스 인터프리터로 지정합니다.
직접 설치할 때 동일 명령:
  .venv/bin/python -m basic_core.service --root "$PWD" --user ubuntu --python "$PWD/.venv/bin/python"
설치 위치는 서비스 사용자 홈 안이어야 합니다. 설치는 서비스를 enable하지만 start하지 않습니다.

아래 명령부터 실제 매매를 허용합니다. 현재 모형0상태이므로 주문 조건은 통과하지 않습니다.
  ./prob start-live
  ./prob status --mode live
  ./prob report --mode live
  ./prob pause --mode live
  ./prob stop --mode live
pause는 신규 진입만 중지하고 기존 포지션 관리를 유지합니다.
stop은 원장과 거래소 모두 보유/미확정이 없어야 프로세스를 정상 종료합니다.
보유/미확정 상태에서 강제종료하거나 data/live.sqlite를 지우지 마세요.
복구 관리만 시작하려면 ./prob resume-live (신규 진입 중지 상태).

실전 관리
진입은 가격 제한 IOC와 거래소의 mark기준 초기 손절을 함께 요청합니다.
실제 체결·실제 보유수량과 거래소 손절 적용을 확인합니다.
미확정 요청은 SQLite에 보관하고 clientOid로 조회하며 같은 진입을 무작정 재전송하지 않습니다.
부분 체결/보호주문 미확인/기존 봇 충돌은 추가 진입을 막고 조회·필요한 축소청산을 시도합니다.
초기 손절은 거래소에 두지만2R/60분 청산은 EC2 관리 프로세스가 수행합니다.
SQLite는 미확정/보유/손실/알림 상태를 재시작 후 복구합니다.
일일4%, 주간8%, 최대낙폭15%, 연속3손실1시간 중지, 청산후5분 재진입 대기.
BTC·ETH의 실제 진입·청산·중요 중단은 즉시 한국어로 같은 Telegram chat_id에 보냅니다.
시작 시와 이후 6시간마다 BTC·ETH 보유·대기 이유·계좌 평가액·진입 가능 상태를 한 건으로 요약합니다.
시세 확인 지연 등의 반복 진단은 6시간에 한 건으로 묶고 긴급 청산 오류는 즉시 알립니다.
연결 장애로 상태 알림이 미전송이면 오래된 여러 건 대신 최신 요약만 남깁니다.
현재 모형의 매매 가능 상태가 0개이므로 실제 진입·청산 알림이 발생하지 않습니다.
알림은 거래와 분리된 재시도 outbox입니다. Telegram은 수락 후 확인 전 장애가 나면 중복될 수 있습니다.

검증 범위와 자료
완료봉 인과성/훈련 누수/회계/상태 복구/부분체결/미확정/보호 손절/알림 등은
이번 GitHub 실행 소스의 가짜 API·로컬 자동 테스트139개가 통과했습니다.
앞선 실행부+연구 전체 조합검증158개는 기존500 USDT 실험 기록입니다.
실제 개인키 API, 실주문, 실제 Telegram은 실행하지 않았습니다.
Binance USD-M 분봉·과거 펀딩을 Bitget UTA의 대체 자료로 사용했습니다.
최종 자료는2026-09-21 15:00 UTC 전까지이며2026-09-30까지의 최신 시세가 아닙니다.
이 기록은 앞선 연구에서도 살펴본 자료라 완전히 새로운 미사용 검증자료라고 할 수 없습니다.
분봉 내부는 보수적인 가정 경로이며 실제 틱/호가잔량/장애/부분체결/청산가격을 완벽히 재현하지 않습니다.
자세한 표본과 결과는 BTC_ETH_PROBABILITY_BOT_GUIDE.html 및 research/*.json에 있습니다.

원자료 재현은 연구 ZIP을 사용합니다. GitHub 실행용 소스에는 대용량 원자료가 없습니다.
실전 라이브러리: NumPy/Pandas/SciPy. HTTP/서명/Telegram은 Python 표준 urllib.
연구 그래프만 추가 matplotlib이 필요합니다. requirements_research.txt를 사용하세요.
전체 self-test와 연구의 C++ 재생기 빌드에 g++와C++17이 필요합니다.
실전 관리 프로세스 자체는 C++ 재생기를 실행하지 않습니다.
