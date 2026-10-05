"""Korean Telegram notices; trade management never waits for delivery."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import sqlite3
import threading
from zoneinfo import ZoneInfo

from cci_chop_v1._compat.notify import send
from cci_chop_v1._compat.core import now_ms

KST = ZoneInfo("Asia/Seoul")


FRAME_LABELS = {"W": "주봉", "D": "일봉", "H12": "12시간봉", "H6": "6시간봉",
                "H4": "4시간봉", "H1": "1시간봉", "M30": "30분봉", "M15": "15분봉",
                "M5": "5분봉", "M3": "3분봉", "M1": "1분봉"}


def _number(value, places=2, signed=False):
    # No raw exchange response, credentials or arbitrary exception text is rendered.
    import math
    if isinstance(value, bool):
        return "확인 필요"
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return "확인 필요"
    if not math.isfinite(number):
        return "확인 필요"
    return format(number, ("+" if signed else "") + "." + str(places) + "f")


def _indicator_line(indicator):
    if not isinstance(indicator, dict):
        return "CCI·CHOP: 완료봉 자료 확인 필요"
    cci_frame = FRAME_LABELS.get(indicator.get("cci_frame", indicator.get("frame")), "선정 시간봉")
    chop_frame = FRAME_LABELS.get(indicator.get("chop_frame", indicator.get("frame")), "선정 시간봉")
    values = (f"CCI20({cci_frame}) {_number(indicator.get('cci20'))} / "
              f"CHOP14({chop_frame}) {_number(indicator.get('chop14'))}")
    return values + (" · 완료봉 기준" if indicator.get("valid") is True else " · 자료 확인 필요")


WAIT_DESCRIPTIONS = {
    "NO_WEEKLY_DIRECTION": "주봉 방향 확인 대기",
    "NO_DAILY_DIRECTION": "일봉 방향 확인 대기",
    "W_D_DIRECTION_CONFLICT": "주봉·일봉 방향 불일치",
    "NO_H1_HIGHER_LOW": "1시간 눌림 저점 상승 대기",
    "NO_H1_LOWER_HIGH": "1시간 반등 고점 하락 대기",
    "NO_ALIGNED_M5_BREAKOUT": "5분 추세 방향 돌파 대기",
    "M5_VOLUME_NOT_CONFIRMED": "5분 거래량 확인 대기",
    "FRAME_DATA_UNAVAILABLE": "완료봉 자료 확인 필요",
    "CCI_CHOP_SELECTED_FRAME_DATA_UNAVAILABLE": "CCI·CHOP 선정 시간봉 자료 확인 필요",
    "CCI_CHOP_DEGENERATE_SELECTED_FRAME": "CCI·CHOP 계산에 필요한 변동 확인 대기",
    "CCI20_LONG_NOT_CONFIRMED": "롱 CCI20 +100 이상 확인 대기",
    "CCI20_SHORT_NOT_CONFIRMED": "숏 CCI20 -100 이하 확인 대기",
    "CHOP14_NOT_TRENDING": "CHOP14 38.2 이하 추세 확인 대기",
}


REASONS = {
    "CC_STRUCTURE_EXIT": "가격 구조 이탈", "GUARD_BREACH": "구조 보호선 이탈",
    "CC_MACRO_REVERSAL": "주봉 또는 일봉 방향 전환",
    "CC_H4_STRUCTURE_BREAK": "4시간 지지·저항 구조 이탈",
    "CC_GUARD_BREACH": "구조 보호선 이탈",
    "W_BIAS_REVERSED": "주봉 방향 전환", "D_BIAS_REVERSED": "일봉 방향 전환",
    "H4_CONFIRMED_STRUCTURE_BREACHED": "4시간 지지·저항 구조 이탈",
    "M5_CLOSE_THROUGH_EXISTING_GUARD": "기존 구조 보호선 이탈",
    "M5_CLOSE_THROUGH_PROPOSED_GUARD": "확정된 새 구조 보호선 이탈",
    "MODEL_NOT_RELEASED": "수익성·실행 검증 승인 없음",
    "DAILY_LOSS_LIMIT": "일일 손실 한도", "WEEKLY_LOSS_LIMIT": "주간 손실 한도",
    "MAX_DRAWDOWN": "계좌 최대 낙폭 한도", "ACCOUNT_RISK_LIMIT": "계정 손실 한도",
    "SHADOW_WEEK_COMPLETE": "일주일 쉐도우 관측 종료",
    "PROCESS_STOPPED": "서버 관리 프로세스 종료",
    "MARKET_OR_ACCOUNT_READ_FAILED": "시세·계정 조회 지연: 신규 진입 제한",
    "NATIVE_PARTIAL_CLOSE": "거래소 보호 주문 부분 청산: 잔여 수량 확인",
    "EXCHANGE_OR_EXTERNAL_CLOSE": "거래소 또는 외부 청산 확인",
}


def render(event, mode):
    kind = str(event.get("kind", event.get("type", "NOTICE")))
    kind = {"OPEN_OBSERVED": "OPEN", "CLOSE_VERIFIED": "CLOSE", "EXIT": "CLOSE"}.get(kind, kind)
    title = {"OPEN": "진입 체결", "CLOSE": "청산 정산", "HALT": "신규 진입 중지",
             "SUMMARY": "현재 상황", "ORDER_UNKNOWN": "주문 결과 확인 중",
             "ERROR": "자료·연결 확인 필요", "START": "관리 시작",
             "TEST": "연결 테스트", "STOPPED": "관리 프로세스 종료"}.get(kind, "운영 알림")
    label = {"live": "미검증 실전 · 수익성 승인 없음", "demo": "비트겟 데모매매",
             "shadow": "쉐도우 가상매매"}[mode]
    lines = [f"[CCI·CHOP 구조추종 · {label}] {title}",
             datetime.fromtimestamp(event.get("at", now_ms())/1000, KST).strftime("%m/%d %H:%M KST")]
    if event.get("symbol") in ("BTCUSDT", "ETHUSDT"):
        side = {"LONG": "롱", "SHORT": "숏", "long": "롱", "short": "숏"}.get(event.get("side"), "")
        lines.append(event["symbol"] + " " + side)
    for key, label, places in (("entry", "진입가", 4), ("qty", "수량", 8),
                               ("stop", "거래소 구조 보호선", 4),
                               ("risk_usdt", "비용 포함 계획 위험", 2)):
        if event.get(key) is not None:
            lines.append(f"{label}: {_number(event[key], places)}")
    if event.get("net_usdt") is not None:
        lines.append(f"순손익: {_number(event['net_usdt'], signed=True)} USDT")
    if kind == "OPEN":
        lines.append("진입 근거: 주·일 방향 일치 / 4시간 구조 / 1시간 눌림·반등 / 5분 거래량 돌파")
        lines.append(_indicator_line((event.get("setup_meta") or {}).get("indicators")))
        lines.append("필터: 롱 CCI ≥ +100 · 숏 CCI ≤ -100 / CHOP ≤ 38.2")
    if kind == "START":
        lines.append("CCI·CHOP은 신규 진입 필터입니다. 확정된 가격 구조로 보호선을 조정하고 청산합니다.")
    if kind == "ORDER_UNKNOWN":
        lines.append("주문을 재전송하지 않습니다. 거래소 체결·보유 수량 확인을 계속합니다.")
    if kind == "STOPPED":
        lines.append("프로세스 종료는 포지션 청산이 아닙니다. 거래소 보유 수량과 보호 주문을 확인하세요.")
    if kind == "SUMMARY":
        lines.extend([f"보유 {event.get('position_count', 0)}종목 / 미확정 주문 {event.get('pending_count', 0)}건",
                      "신규 진입 설정: " + ("활성 (후속 주문 검사 필요)" if event.get("entry_enabled") else "중지"),
                      "규칙: 주·일 방향 → 4시간·1시간 구조 → 5분 진입 / 선정 CCI·CHOP / 구조 청산"])
        if event.get("equity") is not None:
            balance_label = {"live": "최근 계좌 평가액", "demo": "데모 계좌 평가액", "shadow": "참고 가상 잔고"}[mode]
            lines.append(f"{balance_label}: {_number(event['equity'])} USDT")
        for symbol, position in event.get("positions", {}).items():
            if symbol not in ("BTCUSDT", "ETHUSDT"):
                continue
            side = {"LONG": "롱", "SHORT": "숏"}.get(position.get("side"), "방향 확인 중")
            lines.append(f"{symbol}: {side} / 수량 {_number(position.get('qty'), 8)} / 보호선 {_number(position.get('stop'), 4)}")
        def bias_label(value):
            if isinstance(value, dict):
                value = value.get("direction", value.get("bias"))
            return {1: "상승", -1: "하락", 0: "중립", "long": "상승", "short": "하락", "neutral": "중립"}.get(value, "확인 필요")
        for symbol, observation in event.get("markets", {}).items():
            if symbol not in ("BTCUSDT", "ETHUSDT"):
                continue
            bias = observation.get("bias", {})
            lines.append(f"{symbol} 큰 방향: 주 {bias_label(bias.get('W'))} / 일 {bias_label(bias.get('D'))}")
            lines.append(_indicator_line(observation.get("indicators")))
            lines.append("5분 타점: " + ("구조·돌파 후보 확인 (주문 검사 필요)" if observation.get("eligible_setup") else "조건 대기"))
            waits = observation.get("wait_reasons", [])
            descriptions = list(dict.fromkeys(WAIT_DESCRIPTIONS.get(wait, "시간봉별 완료 가격 구조 확인 대기") for wait in waits))
            if descriptions:
                lines.append("대기 사유: " + " / ".join(descriptions[:3]))
        if event.get("state_only"):
            lines.append("저장된 서버 기록 기준입니다. " + ("최근 관측 정상" if event.get("running") else "프로세스 미실행 또는 관측 지연"))
    if event.get("reason"):
        lines.append("상태: " + REASONS.get(str(event["reason"]), "서버 상태·로그에서 확인 필요"))
    if mode == "live":
        lines.append("과거 비교와 코드 테스트는 실제 체결·보호 주문 성공 또는 미래 수익을 보장하지 않습니다.")
    elif mode == "shadow":
        lines.append("실제 Bitget 주문 0건. 가상 손익은 펀딩·실제 체결·청산 검증을 포함하지 않습니다.")
    else:
        lines.append("별도 Bitget 데모 계정 주문입니다. 실계정 자금을 사용하지 않으며 실매매 수익성 증거가 아닙니다.")
    return "\n".join(lines)[:3500]


class Notifier:
    def __init__(self, store, path, settings, mode):
        self.store, self.path, self.settings, self.mode = store, path, settings, mode
        with store.db:
            store.db.execute("""CREATE TABLE IF NOT EXISTS cci_chop_telegram(
                key TEXT PRIMARY KEY, created INTEGER NOT NULL, message TEXT NOT NULL,
                delivered INTEGER, attempts INTEGER NOT NULL DEFAULT 0, due INTEGER NOT NULL DEFAULT 0)""")
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._work, daemon=True, name="cci-chop-telegram")

    def queue(self, event, key=None):
        kind = event.get("kind", event.get("type", "NOTICE"))
        kind = {"OPEN_OBSERVED": "OPEN", "CLOSE_VERIFIED": "CLOSE"}.get(kind, kind)
        # Protection ratchets are recorded locally without a notice every hour.
        if kind not in {"OPEN", "CLOSE", "HALT", "ORDER_UNKNOWN", "SUMMARY", "ERROR", "START", "STOPPED", "TEST"}:
            return
        if key is None:
            if kind in {"OPEN", "CLOSE"} and event.get("id"):
                key = f"{kind}:{self.mode}:{event['id']}"
            else:
                key = hashlib.sha256(json.dumps(event, sort_keys=True, allow_nan=False).encode()).hexdigest()
        with self.store.db:
            self.store.db.execute("INSERT OR IGNORE INTO cci_chop_telegram(key,created,message) VALUES(?,?,?)",
                                  (str(key), now_ms(), render(event, self.mode)))

    def start(self):
        self.thread.start()

    def _work(self):
        with sqlite3.connect(str(self.path), timeout=10) as db:
            drained=0
            while True:
                now = now_ms()
                row = db.execute("SELECT key,message,attempts FROM cci_chop_telegram WHERE delivered IS NULL AND due<=? ORDER BY created LIMIT 1", (now,)).fetchone()
                if self.stop_event.is_set() and (row is None or drained>=3):
                    break
                if row:
                    key, message, attempts = row
                    try:
                        send(self.settings, message)
                    except Exception:
                        with db:
                            db.execute("UPDATE cci_chop_telegram SET attempts=attempts+1,due=? WHERE key=?",
                                       (now+min(600000,5000*2**min(attempts,7)), key))
                    else:
                        with db:
                            db.execute("UPDATE cci_chop_telegram SET delivered=?,attempts=attempts+1 WHERE key=?", (now_ms(),key))
                    if self.stop_event.is_set():
                        drained+=1
                self.stop_event.wait(3)

    def close(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=10)
