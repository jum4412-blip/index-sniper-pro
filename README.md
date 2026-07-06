# Index Sniper Pro v2.9 Quiet + Opposite Signal Guard Patch

## 바뀌는 점

1. **기존 포지션이 있으면 신규 진입 차단 강화**
   - BTC LONG 보유 중 SHORT 신호가 떠도 신규 숏 주문을 넣지 않습니다.
   - BTC SHORT 보유 중 LONG 신호가 떠도 신규 롱 주문을 넣지 않습니다.
   - 기본값: `BLOCK_NEW_ENTRY_WHEN_ANY_POSITION_OPEN=true`, `BLOCK_OPPOSITE_SIGNAL_WHEN_POSITION_OPEN=true`

2. **주문 오류가 루프 전체를 터뜨리지 않도록 처리**
   - Bitget `Insufficient margin` 같은 주문 실패는 이벤트로 기록하고 다음 사이클로 넘어갑니다.
   - 같은 오류 알림은 기본 30분에 1번만 보냅니다.

3. **알림 조용하게 변경**
   - 루프 시작, heartbeat, HOLD 요약, blocked signal 반복 알림을 기본 OFF로 둡니다.
   - 실제 주문/중요 오류만 알립니다.
   - 반복 알림은 `data/alert_throttle_v29.json`으로 제어합니다.

## 적용 순서

GitHub에 ZIP 내용물을 업로드/커밋한 뒤 EC2에서:

```bash
cd ~/index-sniper-pro
source .venv/bin/activate

bash stop_sniper.sh 2>/dev/null || true
for s in $(screen -ls | awk '/sniper|target|observer/ {print $1}'); do
  screen -S "$s" -X quit
done
screen -wipe

git pull
chmod +x apply_v29_quiet_opposite_guard.sh
bash apply_v29_quiet_opposite_guard.sh

python -m py_compile index_sniper/alert_throttle.py
python -m py_compile index_sniper/strategy_executor.py

grep -E 'NOTIFY_|ALERT_|BLOCK_NEW_ENTRY|BLOCK_OPPOSITE' .env
bash run_live_preflight.sh
```

정상 확인 후 시작:

```bash
bash reset_equity_guard.sh
bash start_live_guarded.sh
bash status_sniper.sh
```

## 주의

이 패치는 **반대 신호가 나오면 자동 청산**하는 패치가 아닙니다.  
현재 목적은 안전하게 **신규 반대 포지션 주문을 막고, 반복 알림을 줄이는 것**입니다.
