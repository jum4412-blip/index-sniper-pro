#!/usr/bin/env bash
set -euo pipefail
TREND_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$TREND_ROOT"
python3 -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"'
chmod 700 trend install_service.sh
mkdir -p data
chmod 700 data
./trend self-test
echo '설치 점검 완료. 추가 pip 패키지는 필요 없습니다. README_KO.md의 실행 순서를 따르세요.'
