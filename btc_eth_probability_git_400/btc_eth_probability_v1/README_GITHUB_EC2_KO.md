# BTC·ETH 확률봇: GitHub → EC2 설치

**현재 배포 모형의 진입 가능 상태는 0개입니다.** 실행해도 새 진입 주문은 발생하지 않습니다. 이번 실행 설정은 BTC·ETH 각각 증거금 400 USDT, 교차 5배, 목표 포지션 각 2,000 USDT, 손실 예산 각 20 USDT입니다. 처음에는 계좌 평가액과 사용 가능 담보가 각각 최소 910 USDT여야 하며 실제 시장 비용과 다른 보유 상황에 따라 주문은 더 엄격하게 차단될 수 있습니다. 과거 가상 사건 23,565개의 사건당 평균 순손익 -7.36 USDT는 **이전 각 500 USDT 규모**로 계산한 값입니다. 새 400 USDT 규모의 성과를 검증한 값이 아니며 수익이 검증된 활발한 실매매 전략으로 소개할 수 없습니다. 과거 모형은 새 실전 주문 규모 출처가 맞지 않아 강제로 진입 불가입니다. 향후 새 모형은 실제 2,000 USDT 목표 주문과 20 USDT 위험 상한을 적용한 표본으로 다시 만들고 `label_notional_usdt`를 2000으로 명시해야 합니다. 자세한 근거는 `BTC_ETH_PROBABILITY_BOT_GUIDE.html`과 `README_KO.txt`를 보세요.

아래는 기존 EC2 저장소 `~/index-sniper-pro`를 사용하는 절차입니다. 현재 체크아웃된 기능 브랜치를 바꾸거나 그 브랜치에 `main`을 병합하지 않습니다. 새 설치 폴더를 만들며 기존 거래 원장도 덮어쓰지 않습니다. 전에 받았던 500 USDT용 `.run` 파일은 이 설정과 다릅니다.

```bash
cd ~/index-sniper-pro
git fetch origin main
DEPLOY="$HOME/btc_eth_probability_v1_git"
if test -e "$DEPLOY"; then echo "이미 설치 폴더가 있습니다: $DEPLOY"; exit 1; fi
STAGE="$(mktemp -d "$HOME/.probability-code.XXXXXX")"
git archive FETCH_HEAD btc_eth_probability_v1 | tar -x -C "$STAGE"
mv "$STAGE/btc_eth_probability_v1" "$DEPLOY"
rmdir "$STAGE"
cd "$DEPLOY"
sudo apt-get update
sudo apt-get install -y python3-venv build-essential
./install_dependencies.sh
./prob self-test
./prob doctor-public
```

API 키와 텔레그램 토큰은 EC2에만 저장하세요. `connection.json`의 `env`는 실제 자격증명 파일의 절대 경로로 바꾸고 기존 봇 경로들도 확인하세요. 기존 `~/index-sniper-pro/.env`를 계속 사용할 수도 있습니다.

```bash
cd ~/btc_eth_probability_v1_git
cp credentials.example.env credentials.env
chmod 600 credentials.env
nano credentials.env
cp connection.example.json connection.json
nano connection.json
./prob doctor
```

`credentials.env`에 Bitget 키 3개와 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`를 설정합니다. `connection.json`의 `env`는 `/home/ubuntu/btc_eth_probability_v1_git/credentials.env`처럼 실제 파일로 지정하세요. 계좌와 기존 봇의 보유·미체결 주문이 모두 정리되어야 `doctor` 검사를 통과합니다. 텔레그램 메시지를 한 건 보내서 확인하려면 `./prob notify-test`, 현재 BTC·ETH 상황을 한 건으로 보내려면 `./prob notify-now`를 실행합니다. 먼저 `./prob notify-preview`로 내용을 확인할 수 있습니다.

기존 Larry v2를 사용 중이라면 다음 순서로 신규 진입을 먼저 멈춥니다. **실제 보유와 미체결 주문을 확인하고, 보호·청산을 완료한 뒤** 관리 프로세스를 종료하세요. 다른 Larry 서비스가 있다면 실제 이름과 원장을 별도로 확인합니다.

```bash
cd ~/index-sniper-pro
./larry-live pause
# 여기서 Bitget 실제 보유·미체결 주문 0건을 직접 확인
./larry-live stop
sudo systemctl stop larry-v2-live.service
sudo systemctl disable larry-v2-live.service
sudo systemctl list-units --all --type=service '*larry*' '*trail3*'
cd ~/btc_eth_probability_v1_git
./prob doctor
```

계좌가 평탄하고 교차 5배 설정이 다를 때에만 `./prob setup`을 사용하세요. 서비스 설치 명령은 부팅 시 자동 시작을 등록하지만 지금 시작하지 않습니다. 다음의 `start-live`가 실제 주문 경로를 허용합니다. 현 모형은 통과 상태가 없으므로 관측과 한국어 알림만 동작합니다.

```bash
cd ~/btc_eth_probability_v1_git
./install_service.sh --user ubuntu --print
./install_service.sh --user ubuntu
./prob start-live
./prob status --mode live
./prob notify-now
```

`./prob pause --mode live`는 신규 진입만 멈추고 보유 포지션 관리를 계속합니다. `./prob stop --mode live`는 계좌·원장이 평탄할 때 정상 종료합니다. 원장 `data/live.sqlite`와 자격증명 파일을 지우거나 새 Git 소스로 덮어쓰지 마세요. 사용자가 `ubuntu`가 아니면 서비스 명령의 사용자 이름을 실제 계정으로 바꾸세요.
