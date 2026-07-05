from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from index_sniper.config import load_settings, Settings
from index_sniper.exchange.bitget_uta import BitgetUTAClient
from index_sniper.strategy_executor import _signal_for_symbol
from index_sniper.telegram.bot import TelegramBot


def _fmt_price(x: float | int | str | None) -> str:
    if x is None:
        return "-"
    try:
        f = float(x)
    except Exception:
        return str(x)
    if abs(f) >= 1000:
        return f"{f:,.2f}"
    if abs(f) >= 1:
        return f"{f:.4f}"
    return f"{f:.8f}"


def _fmt_pct(x: float | None) -> str:
    if x is None:
        return "-"
    return f"{x:.2f}%"


def _pct_distance(now: float, target: float) -> float:
    if now <= 0:
        return 0.0
    return (target - now) / now * 100.0


def _utc_dt_from_ms(ts: int | float | None) -> str:
    if not ts:
        return "unknown"
    try:
        return datetime.fromtimestamp(float(ts) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return "unknown"


def _target_key(settings: Settings, target_day_key: str) -> str:
    symbol_part = ",".join(settings.symbols)
    return f"{target_day_key}|{symbol_part}|k={settings.k_value}|no_ma={settings.use_ema_filter is False}|both={settings.no_ma_both_breakout_mode}"


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def build_daily_target_lines(settings: Settings, client: BitgetUTAClient) -> tuple[list[str], str]:
    lines: list[str] = []
    target_day_keys: list[str] = []

    header = [
        "🎯 <b>다음/오늘 변동성 돌파 타겟</b>",
        f"대상: {', '.join(settings.symbols)}",
        f"전략: {'No-MA' if not settings.use_ema_filter else 'EMA filter'} / K={settings.k_value:.2f} / both={settings.no_ma_both_breakout_mode}",
        f"TP/SL 기준: SL ATR×{settings.atr_stop_mult:.2f}, TP ATR×{settings.atr_take_profit_mult:.2f}",
        "형식: 현재가 → 롱타겟 / 숏타겟",
    ]
    lines.extend(header)

    for symbol in settings.symbols:
        try:
            signal, price, _instrument, daily_candles, _warmup = _signal_for_symbol(settings, client, symbol)
            current_candle = daily_candles[-1] if daily_candles else None
            previous_candle = daily_candles[-2] if len(daily_candles) >= 2 else None
            target_day = _utc_dt_from_ms(getattr(current_candle, "ts", None))
            target_day_keys.append(f"{symbol}:{target_day}")

            now = float(price)
            long_target = float(signal.long_target)
            short_target = float(signal.short_target)
            atr = float(signal.atr or 0.0)
            prev_range = float(signal.previous_range or 0.0)
            current_open = float(signal.current_open or 0.0)
            prev_high = float(signal.previous_high or 0.0)
            prev_low = float(signal.previous_low or 0.0)

            long_gap = max(0.0, long_target - now)
            short_gap = max(0.0, now - short_target)
            long_gap_pct = _pct_distance(now, long_target)
            short_gap_pct = -_pct_distance(now, short_target)

            if atr > 0:
                long_sl = long_target - (atr * settings.atr_stop_mult)
                long_tp = long_target + (atr * settings.atr_take_profit_mult)
                short_sl = short_target + (atr * settings.atr_stop_mult)
                short_tp = short_target - (atr * settings.atr_take_profit_mult)
            else:
                long_sl = long_tp = short_sl = short_tp = None

            lines.append("")
            lines.append(f"<b>{symbol}</b> 기준봉: {target_day}")
            lines.append(
                f"현재 {_fmt_price(now)} → L {_fmt_price(long_target)} "
                f"(+{_fmt_price(long_gap)}, {_fmt_pct(long_gap_pct)}) / "
                f"S {_fmt_price(short_target)} "
                f"(-{_fmt_price(short_gap)}, {_fmt_pct(short_gap_pct)})"
            )
            lines.append(
                f"전일 H/L/R: {_fmt_price(prev_high)} / {_fmt_price(prev_low)} / {_fmt_price(prev_range)} | "
                f"오늘 open {_fmt_price(current_open)} | ATR {_fmt_price(atr) if atr else '-'}"
            )
            if atr > 0:
                lines.append(
                    f"롱 체결 시 예상 SL/TP: {_fmt_price(long_sl)} / {_fmt_price(long_tp)} | "
                    f"숏 체결 시 예상 SL/TP: {_fmt_price(short_sl)} / {_fmt_price(short_tp)}"
                )
            lines.append(f"현재 상태: {signal.signal} / {signal.reason}")
        except Exception as exc:
            target_day_keys.append(f"{symbol}:error")
            lines.append("")
            lines.append(f"⚠️ <b>{symbol}</b> 타겟 계산 실패: {str(exc)[:300]}")

    target_key = _target_key(settings, "|".join(target_day_keys))
    return lines, target_key


def send_daily_targets(force: bool = False, once: bool = True) -> bool:
    settings = load_settings()
    enabled = os.getenv("DAILY_TARGET_ALERT_ENABLED", "true").strip().lower() in {"1", "true", "yes", "y", "on"}
    if not enabled and not force:
        print("daily target alert disabled")
        return False

    client = BitgetUTAClient(settings.bitget_api_key, settings.bitget_secret_key, settings.bitget_passphrase)
    tg = TelegramBot(settings.telegram_token, settings.telegram_chat_id)

    state_path = Path(os.getenv("DAILY_TARGET_ALERT_STATE_PATH", "data/daily_target_alert_state.json"))
    state = _load_state(state_path)

    lines, key = build_daily_target_lines(settings, client)
    last_key = state.get("last_key")

    if once and not force and key == last_key:
        print(f"daily target already sent for key: {key}")
        return False

    ok = tg.send("\n".join(lines[:45]))
    state.update({
        "last_key": key,
        "last_sent_at": datetime.now(timezone.utc).isoformat(),
        "symbols": settings.symbols,
        "ok": ok,
    })
    _save_state(state_path, state)
    print("sent" if ok else "telegram_send_failed")
    print("key:", key)
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description="Send daily long/short target alert")
    parser.add_argument("--force", action="store_true", help="send even if already sent for the current target key")
    parser.add_argument("--once", action="store_true", help="send only once per target key")
    args = parser.parse_args()
    send_daily_targets(force=args.force, once=True if args.once else True)


if __name__ == "__main__":
    main()
