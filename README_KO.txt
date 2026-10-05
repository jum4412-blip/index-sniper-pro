CCI·CHOP 다중 시간봉 구조추종 — Bitget / EC2
==========================================

이 패키지는 BTCUSDT·ETHUSDT 주문 경로를 포함합니다. 기본 증거금 상한은 종목당
300 USDT, 최대 2종목, 레버리지는 5배입니다. 신호가 없으면 진입하지 않습니다. 이 결과에서는 쉐도우 관측부터 시작하는 것이 합리적입니다.
위험 거리와 계좌 여유에 따라 실제 주문은 300 USDT보다 작거나 생략될 수 있습니다.
레버리지는 cci_chop_config.json에서 1~5배로 낮출 수 있습니다. 거래소 설정도 같은
배율로 수동 변경해야 합니다. 다른 종목과 RWA 종목은 이 릴리스에 포함되지 않습니다.

과거 비교에서 가장 높은 결과를 고르는 것과 실매매 수익성 승인은 다릅니다.
기존에 열람한 Binance 자료는 Bitget 체결 기록이나 미열람 검증 구간이 아닙니다.
`--allow-unvalidated-live`는 사용자가 수익성 미승인을 인지하고 실전 주문 경로를
활성화하는 별도 선택입니다. 이 선택은 통계·실제 보호 주문·전향 검증이 완료됐다는
표시를 만들지 않습니다. 미래 수익이나 손실 상한은 보장되지 않습니다.

[시간봉 비교 결과]
최종 선택: CCI20 30분봉 + CHOP14 6시간봉.
선택은 cci_chop_v1/entry_rule.json에 동결되어 있습니다.

각 144개 구성(CCI 단독 11, CHOP 단독 11, 결합 121, 기준 전략 1)을
두 구간·두 수수료 가정·기본/2배 비용으로 대조했습니다.
원래 비교 1,152개와 손실 중지 규칙을 적용한 별도 비교 1,152개, 합계 2,304개
주요 시나리오입니다. 원래 선정 조합에 대한 손실 중지 추가 비교 8개도 보존합니다.
후속 비교는 별도 프로토콜이며, 기존 결과를 지우거나 미열람 검증으로 바꾸지 않았습니다.

통계적 실매매 승인 통과 구성은 0개입니다. 손실 중지 규칙을 반영한 별도 비교에서는
충분한 표본 기준을 충족한 결합 조합도 없었습니다. 최종 조합은 손실 중지 비교의 학습 구간 2배 비용 순손익 순위로 고른
실험용 선택입니다. 수익성 승인을 받은 조합이 아닙니다.
아래 금액은 개인 테이커 수수료 0.04% 가정의 포트폴리오 순손익입니다.

최종 조합의 손실 중지 적용 결과:
구간                    거래수 기본/2배   기본 비용 순손익   2배 비용 순손익
2023–2024 학습          53 / 53            +205.01 USDT        +60.31 USDT
2025–2026.09 재열람     39 / 38             -49.06 USDT        -70.81 USDT

학습 거래수는 BTC 30 / ETH 23으로 작습니다.
학습 기본 비용: BTC +309.27 / ETH -104.26 USDT.
학습 2배 비용: BTC +181.19 / ETH -120.87 USDT.
두 구간 모두 계좌 최대 낙폭 10% 기준의 지속적인 신규 진입 중지가 발생했습니다.
ETH 손실, 부족한 표본과 재열람 구간 손실 때문에 BTC·ETH 수익성이 검증됐다고
말할 수 없습니다. 실시간 3초 간격 Bitget 평가손익 점검과 1분 대체 자료로 근사한
과거 손실 중지는 같지 않으며, 실제 계좌 청산과 보호 주문 실행도 검증되지 않았습니다.

참고: 손실 중지 적용 전 원래 비교에서 골랐던 CCI 6시간 / CHOP 3분 조합은
학습 206거래 기본 +251.96 / 2배 비용 -136.85 USDT,
재열람 134거래 기본 +139.05 / 2배 비용 -68.73 USDT였습니다.
이 과거 조합의 손실 중지 추가 비교 역시 모두 손실이며 최종 실전 조합이 아닙니다.

프로토콜·전체 결과·재현 코드는 cci_chop_timeframe_comparison_v1/에 있습니다.
비교 범위: 주봉, 일봉, 12시간, 6시간, 4시간, 1시간, 30분, 15분, 5분, 3분, 1분.
CCI20은 롱 +100 이상 / 숏 -100 이하, CHOP14는 38.2 이하를 같은 기준으로 비교합니다.
완료되지 않은 현재 봉은 제외합니다. 선택한 CCI 시간봉과 CHOP 시간봉은 같을 수도,
다를 수도 있습니다. 모든 임의의 시간봉·파라미터·매매법을 시험했다는 의미는 아닙니다.
선택 규칙과 비교 결과를 수정하면 기존 실매매 승인 바인딩과 일치하지 않습니다.

[진입과 청산]
주·일봉 큰 방향이 같고, 4시간 가격 구조가 유지되며, 1시간 확정 눌림/반등 구조와
5분봉 거래량 돌파가 있을 때 선정 시간봉 CCI·CHOP으로 신규 진입을 거릅니다.
완료된 5분봉 이후 1분 대기, 새 호가·수수료·스프레드·수량 단위·마진 검사도 거칩니다.
CCI·CHOP은 진입 필터이며 보유 중 지표가 약해졌다는 이유만으로 청산하지 않습니다.

고정 수익률 익절이나 고정 비율 손절 대신 확정된 지지·저항 구조를 보호선으로 씁니다.
추세가 유지되면 1시간 확정 구조로 보호선을 유리한 쪽으로만 이동합니다.
주/일 큰 방향 반전, 4시간 구조 이탈 또는 보호선 이탈은 청산 근거입니다.
거래소 구조 보호 주문과 로컬 청산을 사용하며, 주문 결과가 불명확하면 재전송하지
않고 체결·보유 수량을 확인합니다. 부분 체결·재시작 확인 코드 테스트가 실제 거래소
동작 증거를 대신하지는 않습니다. 보호 주문은 갭·슬리피지·통신 장애로 계획보다
큰 손실을 낼 수 있습니다. 설정의 10초 보호 확인 목표는 API 지연 시 보장된 시간이 아닙니다.

거래당 계획 위험: 현재 평가액 1% 이하 및 12 USDT 이하.
동시 계획 위험: 계좌 평가액 2% 이하. 최소 여유 증거금: 150 USDT.
일간 2% / 주간 5% / 최대 낙폭 10% 기준은 신규 진입을 중지합니다.
계획 위험이나 이 중지 기준은 확정된 최대 손실을 뜻하지 않습니다.

[GitHub 업로드]
사용자가 압축을 풀어 기존 저장소 루트에 새 cci_chop_v1 폴더와 cc 실행 파일,
해당 tests 파일을 추가하고 커밋합니다. 기존 basic_core·turtle·mtf 폴더와 data를
삭제하거나 덮어 정리하지 마세요. credentials.env, connection.json, .venv, data와
로컬 cci_chop_config.json, 데모 키 파일을 커밋하지 마세요.
키를 공개 저장소에 올린 경우 해당 키를 폐기하고 새 키를 발급해야 합니다.
GitHub 설치나 연결은 필요하지 않습니다.

[EC2: 기존 저장소 갱신]
EC2 Ubuntu 터미널에서 한 줄씩 실행합니다. git pull이 로컬 변경 때문에 실패하면
변경 내용을 보존하고 병합하세요. git reset --hard나 data 삭제로 해결하지 마세요.

cd ~/btc_eth_probability_v1_git
git pull --ff-only
chmod 700 cc cci_chop_v1/deploy/install_ec2.sh

기존 .venv가 있으면 그대로 사용합니다. 없을 때만 실행합니다.

sudo apt-get update
sudo apt-get install -y python3-venv
python3 -m venv .venv

실행에는 Python 표준 라이브러리만 필요합니다. 연구 재현에는 NumPy가 필요합니다.

./.venv/bin/python -B -m unittest discover -s tests -p 'test_cci_chop_*.py'

[계정·텔레그램 설정]
이전에 Telegram 연결이 확인된 credentials.env와 connection.json은 그대로 사용합니다.
connection.json의 env가 실제 키 파일을 가리키는지 확인하세요. 키를 화면 캡처나
채팅으로 보내지 마세요. API 키에 거래 권한은 필요하고 출금 권한은 없어야 합니다.
기존 설정 파일에 example 파일을 다시 복사하면 연결 경로를 잃을 수 있습니다.

처음 설치하는 경우에만, 파일이 없을 때 키 파일을 만들고 직접 값을 입력합니다.

[ -f credentials.env ] || (umask 077; cp credentials.example.env credentials.env)
chmod 600 credentials.env
nano credentials.env

Bitget API key, secret, passphrase와 Telegram bot token, chat ID를 채우고
Ctrl+O → Enter로 저장한 뒤 Ctrl+X로 편집기를 나옵니다. Ctrl+C는 저장 종료가 아닙니다.
connection.json은 기존 값이 없을 때만 connection.example.json에서 만들고 env 항목을
/home/ubuntu/btc_eth_probability_v1_git/credentials.env처럼 실제 경로로 지정합니다.

[ -f connection.json ] || cp connection.example.json connection.json
nano connection.json

Bitget 통합 계정(UTA)을 사용하며 BTCUSDT·ETHUSDT는 USDT 선물,
교차 증거금과 설정된 배율이어야 합니다. 단방향/헤지 모드는 프로그램이 계정에서
읽어 해당 주문 형태를 적용합니다. 계정의 기존 포지션과 일반/전략/보호 주문을
정산하고 다른 실매매 봇을 종료한 뒤 시작해야 합니다. API 오류·계정 불일치·미확정
주문을 무시하는 명령이나 데이터 삭제 명령은 없습니다.

[확인 → 사용자의 실전 활성화 → EC2 서비스 시작]
아래 init, doctor, notify-test, arm 자체는 실매매 주문을 전송하지 않습니다.
run 또는 서비스를 시작한 뒤 유효한 신호가 생기면 실제 주문이 전송될 수 있습니다.

./cc init
./cc doctor --mode live
./cc notify-test --mode live
./cc arm --allow-unvalidated-live
bash cci_chop_v1/deploy/install_ec2.sh
sudo systemctl enable --now cci-chop-live.service
sudo systemctl status cci-chop-live.service --no-pager
sudo journalctl -u cci-chop-live.service -n 80 --no-pager
./cc status --mode live
./cc notify-now --mode live

arm은 코드·동결 규칙·설정·검증 자료·계정 식별자에 권한을 연결합니다.
이후 소스/규칙/모델/설정을 수정하면 재승인이 필요합니다. 포지션이나 미확정 주문이
남아 있는 동안 새 코드로 바꾸거나 상태 DB를 지우지 마세요. 기존 코드로 먼저 정산합니다.
서비스 설치기는 서비스 파일만 설치하고 자동 시작하지 않습니다.
service enable --now 명령이 서버 재시작 후 자동 실행도 켭니다. Telegram 메시지의
실전 표시는 '미검증 실전 · 수익성 승인 없음'이며 수익성 승인으로 바뀌지 않습니다.

doctor의 live_release_ready=false는 수익성 승인이 없다는 현재 연구 상태입니다.
experimental_policy_available=true는 명시적인 미검증 실전 실험 경로의 설정이며
수익성 승인이 아닙니다. 보정된 진입 확률·승률 값은 제공하지 않습니다.

doctor 출력이 BLOCKED이면 그 원인을 해결한 뒤 다시 확인합니다. 계정 모드·배율·키
오류나 보호 주문 수량 불일치를 강제로 통과시키지 않습니다. 주·일 방향 불일치 등
전략 대기는 오류가 아니며, 통과하는 5분봉 이벤트가 생길 때만 주문을 검토합니다.

서비스 대신 터미널에서 직접 실행하려면 위 서비스 시작 명령 대신 아래를 사용합니다.
둘을 동시에 실행하면 안 됩니다.

./cc run --mode live

[텔레그램: 적은 빈도로 한 곳에]
시작, 실제 진입 체결, 청산 정산, 신규 진입 중지와 미확정 주문을 한국어로 보냅니다.
정기 현황은 기본 6시간마다 1건입니다. 매 봉과 보호선 이동마다 보내지 않습니다.
진입/현황에는 주·일 방향, 선정 CCI·CHOP 시간봉과 값, 조건 대기 사유가 포함됩니다.
수동 즉시 현황은 ./cc notify-now --mode live 입니다.
저장된 기록 기준 메시지에는 관측이 오래됐거나 프로세스가 종료된 상태도 표시됩니다.
알림은 SQLite 발송함을 사용하며 매매 관리와 별도로 처리됩니다. Telegram 접수 직후
서버가 재시작하면 발송 확인을 저장하지 못해 동일 알림이 다시 올 수 있습니다.

[새 진입 중지 및 종료]
./cc pause

pause는 새 진입 권한을 지우고, 실행 중인 프로그램의 기존 포지션 관리와 보호 확인은
계속합니다. 즉시 전량 시장가 청산 명령이 아닙니다.
다음 명령으로 상태를 확인하고 거래소에서도 BTC·ETH 보유량과 주문을 확인하세요.

./cc status --mode live

모든 포지션·미확정 주문·수량 수정이 정산된 뒤에만 관리 서비스를 종료합니다.

sudo systemctl disable --now cci-chop-live.service

서비스 종료는 계좌를 평탄화하지 않습니다. 서버를 끄면 로컬 구조 청산과 알림도
중단되므로 보유 포지션이 있을 때 서비스를 그냥 끄지 마세요. 거래소 보호 주문을
임의로 취소하거나 data/ 아래 손실·주문 기록을 삭제하지 마세요.

[이전 래리 봇 확인]
아래는 조회만 합니다. 예전에 보였던 eth_core notify 프로세스는 알림 프로세스이며
그 한 줄만으로 매매 봇이 동작 중이라고 판단할 수 없습니다.

ps -eo pid,args | grep -E 'larry_v2|larry_live|trend_core|eth_core|basic_core|dual_live_v63|turtle_structure_v1|mtf_structure_v1' | grep -v grep
systemctl list-units --all --type=service | grep -Ei 'larry|trend|psych|trail3|turtle|mtf|probability'

보유 포지션을 기존 봇으로 먼저 관리/정산하고 해당 봇의 실제 서비스 이름을 찾습니다.
그 서비스의 기존 pause/종료 방법을 사용하세요. 평탄화 확인 뒤 서비스 이름을 정확히
넣어 sudo systemctl disable --now 실제서비스이름.service를 실행합니다.
PID 전체 kill이나 pkill -f python 명령은 다른 관리 프로세스까지 종료하므로 사용하지 않습니다.
서비스가 아닌 수동 실행 프로세스도 정확한 PID와 역할, 포지션 정산을 먼저 확인합니다.

[쉐도우 또는 별도 데모]
실계좌 주문 없이 먼저 관측하려면 아래를 실행합니다.

./cc run --mode shadow

쉐도우는 기존 키로 읽기만 하고 주문은 로컬 가상 체결입니다. 7일 뒤 종료되며,
쉐도우 손익은 실제 펀딩·청산·거래소 보호 실행의 검증 결과가 아닙니다.
Bitget 데모를 사용하려면 별도 데모 API 키가 필요합니다. 실계정 키를 데모로 대체하지 않습니다.

cp cci_chop_v1/demo_credentials.example.env cci_chop_demo_credentials.env
chmod 600 cci_chop_demo_credentials.env
nano cci_chop_demo_credentials.env
./cc doctor --mode demo
./cc run --mode demo

데모 역시 실제 수익성이나 본계정 보호 주문 성공을 보장하지 않습니다.

[공식 API 문서 확인: 2026-10-05]
https://www.bitget.com/docs/catalog/market/market-data
https://www.bitget.com/docs/catalog/trading/order-management
https://www.bitget.com/docs/catalog/trading/strategy-trading
문서 형식·오프라인 테스트는 실제 Bitget 체결 성공 증거가 아닙니다.
