CCI·CHOP 시간봉 배치 비교 재현 자료

실주문을 보내는 코드가 아니다. 네트워크/거래소 API/텔레그램 코드를 포함하지 않는다.
실행 프로그램과 EC2 명령은 같은 배포 묶음의 별도 cci_chop_v1 모듈 문서를 따른다.

최종 참고 조합: CCI20 30분봉 + CHOP14 6시간봉.
144개 배치 중 계정 진입 차단까지 반영한 2023–2024 학습 순위 1위다.
충분한 거래 표본을 가진 결합이 없었고 ETH와 진단 기간이 손실이다.
이 결과를 수익성 검증 완료 또는 실매매 승인으로 해석하면 안 된다.
전체 수치와 제한은 REPORT_KO.txt를 읽는다.

원천자료

앞서 제공한 turtle_backtest_raw_cache.zip의 cache/ 디렉터리에 다음 4개 파일이 필요하다.
이 자료는 재다운로드하지 않으며 사전 SHA256과 다른 파일이면 실행을 거절한다.

BTCUSDT_minute.npz
9b75ba34d34c173806ecebb4970ce16358a7a70c77a6319b0f914ba5c7266be9
BTCUSDT_funding.npz
2e63badc8d0f7cf68828b6ce5328c6cf2971d66c6e64cb74c55815d1eccb9df3
ETHUSDT_minute.npz
a71223e1bb57c634b2626bf531dd48df4726eff6a136bd4dd2c473b7b46bcb30
ETHUSDT_funding.npz
0d93f8937f3909573471217ea2cefec544d7b21e80dda8a7f11718198b3a204e

전체 재현 명령

Python 3.10 이상, numpy가 필요하다. 기존 배포 루트에서:

python3 -m pip install -r cci_chop_timeframe_comparison_v1/requirements.txt
python3 cci_chop_timeframe_comparison_v1/reproduce.py --cache /절대경로/cache --output /새로운경로/cci_chop_replay

--output 디렉터리가 이미 있으면 거절한다. 원본 결과와 상태를 삭제하거나 덮어쓰지 않는다.
새 경로에 원본 소스와 후보 캐시를 복사하고 자료 디렉터리를 링크해 아래 순서로 재생한다.
필요한 상위 경로도 새로 생성하며, 원본 NPZ와 원본 배포 폴더는 수정하지 않는다.

1. runner.py: 144개 배치 × 수수료2 × 비용2 × 기간2 = 1,152개 시나리오.
   계정 손실 차단을 제외한 구조 청산/수량/증거금 기준 비교.
   원선정은 CCI6시간 + CHOP3분이다.
2. risk_halt_supplement.py: 원선정 8개 시나리오에 계정 진입 차단 추가.
3. guarded_grid.py: 144개 전체를 같은 차단으로 다시 비교한 1,152개 시나리오.
   별도 선정은 CCI30분 + CHOP6시간이다. 원선정 기록은 보존한다.
4. guarded_bootstrap.py: 기준전략·원선정·차단포함선정의 일/주 공통 재표집 하한.

단일 원 비교만 새로운 결과 경로로 재현하려면:

python3 -m cci_chop_timeframe_comparison_v1.runner --cache /절대경로/cache --output /새로운경로/raw_results --work cci_chop_timeframe_comparison_v1/_work

risk_halt_supplement.py와 guarded_grid.py의 동결 소스는 고정된 상대 자료 경로와 새 결과 폴더를 사용한다.
소스 바이트/해시를 보존하기 위해 이 파일을 수정하지 말고 위 reproduce.py를 사용한다.

테스트

배포 루트에서:
python3 -m unittest discover -s cci_chop_timeframe_comparison_v1/tests -v

39개 테스트는 완료봉만 읽는지, 자료 공백을 거절하는지, CCI/CHOP 수식을 따르는지,
미래 거래 손익을 현재 진입 크기에 사용하지 않는지, 비용·펀딩·달력 재표집을 일관되게 처리하는지,
계정 위험 차단 이후 신규 진입을 지속 멈추는지, 공개한 CSV 합계·소스 해시가 일치하는지를 검사한다.
수익성이나 실제 거래소 체결을 증명하는 테스트가 아니다.

파일 안내

results/: 첫 144개 비교. comparison_all.csv 3,456행(시나리오 × BTC/ETH/합계).
risk_halt_results/: 원선정 8개 차단 근사 비교.
guarded_grid_results/: 두 번째 144개 비교와 3,456행 CSV, 차단포함 선정, 일/주 하한.
각 frozen_protocol.json: 결과를 보기 전에 기록한 규칙·자료·소스 해시.
_work/: 지표와 무관하게 만든 모든 기준 후보 진입의 구조 청산 경로 캐시.
VERIFIED_RESULTS.json: 공개 결과와 레시피의 무결성 기록.

범위와 한계

W/D/H12/H6/H4/H1/M30/M15/M5/M3/M1의 고정 배치만 비교했다.
CCI20 롱>=100/숏<=-100, CHOP14<=38.2를 고정했고 기간/임계값은 탐색하지 않았다.
월봉, 임의 길이 분봉, 모든 가능한 매매법을 전수 조사했다는 뜻은 아니다.

2023–2024 학습과 2025–2026.09 진단 자료는 모두 이전에 열람한 역사 자료다.
Binance USD-M 대용자료이며 Bitget 실제 체결·유지증거금·청산·보호주문 실행과 다를 수 있다.
차단 포함 재생도 1분 시가 자산과 다음분 결산 근사다. 3초 mark/effective-equity 실행과 동일하다고 볼 수 없다.
10% 차단은 손실의 상한이 아니며 이미 보유한 포지션, 갭, 체결비용으로 더 큰 낙폭이 가능하다.
일/주 묶음 재표집과 비교가족 보정은 탐색 편향·자료 재열람·교차거래소 오차를 제거하지 않는다.
