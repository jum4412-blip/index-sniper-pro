# Index Sniper Pro v2.9 Opposite Signal Close-Only Patch

## 목적

반대 신호가 확정되면 기존 포지션만 청산하고, 바로 반대 포지션으로 뒤집지 않는 패치입니다.

예시:

- BTCUSDT LONG 보유 중 SHORT 신호 확정 → LONG 시장가 청산
- BTCUSDT SHORT 보유 중 LONG 신호 확정 → SHORT 시장가 청산
- 청산 후 같은 봇 일자에는 신규 진입 잠금

## 핵심 동작

- `OPPOSITE_SIGNAL_EXIT_ENABLED=true`일 때만 작동합니다.
- `OPPOSITE_SIGNAL_EXIT_MODE=close_only`만 지원합니다.
- 진입 차단 조건인 Daily loss guard, max open position은 청산 주문에는 적용하지 않습니다.
- 신규 반대 포지션 진입은 하지 않습니다.
- 청산 성공 후 `data/strategy_state.json`에 `opposite_exit_locks`를 기록해 같은 일자 신규 진입을 막습니다.

## 적용

```bash
cd ~/index-sniper-pro
git pull
source .venv/bin/activate
chmod +x apply_opposite_signal_close_only.sh
bash apply_opposite_signal_close_only.sh
python -m py_compile index_sniper/strategy_executor.py
```

## 테스트

```bash
bash run_live_preflight.sh
```

반대 신호가 잡히면 DRY 결과에 `opposite_signal_exit`와 청산 payload가 표시됩니다. DRY에서는 실주문이 나가지 않습니다.

## 시작

```bash
bash stop_sniper.sh 2>/dev/null || true
for s in $(screen -ls | awk '/sniper/ {print $1}'); do screen -S "$s" -X quit; done
screen -wipe
bash reset_equity_guard.sh
bash run_check.sh
bash run_live_preflight.sh
bash start_live_guarded.sh
bash status_sniper.sh
```

## 끄기

```bash
bash disable_opposite_signal_close_only.sh
```
