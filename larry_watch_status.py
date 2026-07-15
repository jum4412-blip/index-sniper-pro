#!/usr/bin/env python3
from __future__ import annotations

import sys
from datetime import datetime, timedelta, UTC
from pathlib import Path

from index_sniper import larry_williams_core_v1 as lw


def f(x: float, digits: int = 4) -> str:
    if x is None:
        return "-"
    try:
        x = float(x)
    except Exception:
        return str(x)
    if abs(x) >= 1000:
        return f"{x:,.2f}"
    if abs(x) >= 100:
        return f"{x:.2f}"
    if abs(x) >= 1:
        return f"{x:.4f}".rstrip("0").rstrip(".")
    return f"{x:.6f}".rstrip("0").rstrip(".")


def dist_pct(price: float, level: float) -> float:
    return (level / price - 1.0) * 100.0 if price > 0 else 0.0


def common_indicators(bars15, bars1h):
    c15 = lw.completed_bars(bars15, "15m")
    c1h = lw.completed_bars(bars1h, "1H")
    closes1h = [b.close for b in c1h]
    return {
        "atr15": lw.atr_value(c15, 14),
        "ema20": lw.ema_last(closes1h[-120:], 20) if len(closes1h) >= 20 else 0.0,
        "ema50": lw.ema_last(closes1h[-160:], 50) if len(closes1h) >= 50 else 0.0,
        "wpr": lw.williams_r(c15, 14, 0) if len(c15) >= 14 else 0.0,
        "wpr_prev": lw.williams_r(c15, 14, 1) if len(c15) >= 15 else 0.0,
        "uo": lw.ultimate_oscillator(c15, 7, 14, 28, 0) if len(c15) >= 29 else 0.0,
        "uo_prev": lw.ultimate_oscillator(c15, 7, 14, 28, 1) if len(c15) >= 30 else 0.0,
        "vol": lw.volume_ratio(c15, 20) if len(c15) >= 21 else 0.0,
    }


def print_managed(state, client, settings):
    raw = state.get("managed_position")
    if not isinstance(raw, dict):
        print("포지션 모드 : FLAT / 신규 신호 감시")
        return
    m = lw.ManagedPosition(**raw)
    ticker = lw.fetch_ticker(client, m.symbol)
    mark = ticker.mark
    profit_r = lw.side_profit(m, mark) / m.initial_r if m.initial_r > 0 else 0.0
    held = (lw.now_utc() - datetime.fromisoformat(m.entry_ts)).total_seconds() / 60.0
    cfg = settings.symbols[m.symbol]
    max_hold = float(cfg.get("max_hold_minutes", 2160 if m.profile == "crypto" else 390))
    min_bail = float(cfg.get("bailout_min_hold_minutes", 120 if m.profile == "crypto" else 60))
    print("포지션 모드 : MANAGE")
    print(f"  {m.symbol} {m.side} / {m.setup} / 수량 {f(m.qty)}")
    print(f"  진입 {f(m.entry_price)} | 현재 mark {f(mark)} | 현재 {profit_r:+.2f}R")
    print(f"  초기 손절 {f(m.initial_stop)} | 현재 소프트 손절 {f(m.software_stop)}")
    print(f"  비상 익절 {f(m.emergency_tp)} | 추적손절 {'ON' if m.trail_active else '대기(+1R부터)'}")
    print(f"  보유 {held:.0f}분 | bailout 검토 {min_bail:.0f}분 이후 | 최대보유 {max_hold:.0f}분")
    if m.profile == "stock":
        print(f"  SKHY 강제청산: 뉴욕 {cfg.get('force_flat_ny', '15:55')}")


def eth_watch(client, settings, symbol, cfg):
    instrument = lw.fetch_instrument(client, symbol)
    ticker = lw.fetch_ticker(client, symbol)
    bars15 = lw.fetch_candles(client, symbol, "15m", int(cfg.get("bars_15m", 420)))
    bars1h = lw.fetch_candles(client, symbol, "1H", int(cfg.get("bars_1h", 220)))
    bars1d = lw.fetch_candles(client, symbol, "1D", int(cfg.get("bars_1d", 80)))
    candidate = lw.crypto_candidate(settings, symbol, cfg, instrument, ticker, bars15, bars1h, bars1d)
    ind = common_indicators(bars15, bars1h)
    c15 = lw.completed_bars(bars15, "15m")
    c1d = lw.completed_bars(bars1d, "1D")
    print(f"\n[{symbol}] CRYPTO")
    print(f"  현재 {f(ticker.last)} | spread {ticker.spread_pct:.4f}% | funding {ticker.funding:+.6f}")
    if len(c15) < 40 or len(c1d) < 5:
        print("  모드: DATA_WARMUP")
        return
    live_daily = bars1d[-1]
    prev = c1d[-1]
    recent_ranges = [b.high - b.low for b in c1d[-4:-1]] or [prev.high - prev.low]
    range_ref = lw.median(recent_ranges, prev.high - prev.low)
    compression = (prev.high - prev.low) / max(range_ref, 1e-9)
    if compression < 0.85:
        k = float(cfg.get("breakout_k_compressed", 0.28))
    elif compression > 1.25:
        k = float(cfg.get("breakout_k_expanded", 0.45))
    else:
        k = float(cfg.get("breakout_k_base", 0.35))
    long_trigger = live_daily.open + k * range_ref
    short_trigger = live_daily.open - k * range_ref
    latest = c15[-1]
    sweep_low = min(b.low for b in c15[-6:])
    sweep_high = max(b.high for b in c15[-6:])
    if candidate:
        threshold = max(settings.signal_threshold, float(cfg.get("signal_threshold", 0)))
        ready = candidate.score >= threshold
        print(f"  모드: {'READY' if ready else 'SETUP_SCORE_LOW'} / {candidate.setup} {candidate.side}")
        print(f"  점수 {candidate.score:.1f}/{threshold:.1f} | 진입기준 {f(candidate.entry_reference)}")
        print(f"  초기SL {f(candidate.stop_price)} | 비상TP {f(candidate.emergency_tp_price)}")
        print(f"  손절폭 {candidate.stop_distance_pct:.3f}% | 계좌위험 약 {candidate.account_risk_pct:.3f}%")
        print(f"  이유: {', '.join(candidate.reasons)}")
    else:
        mode = "WAIT_BREAKOUT_OR_OOPS"
        if sweep_low < prev.low and latest.close <= prev.low:
            mode = "WAIT_OOPS_LONG_RECLAIM"
        elif sweep_high > prev.high and latest.close >= prev.high:
            mode = "WAIT_OOPS_SHORT_REJECT"
        print(f"  모드: {mode}")
        print(f"  롱 돌파선 {f(long_trigger)} ({dist_pct(ticker.last, long_trigger):+.3f}%)")
        print(f"  숏 돌파선 {f(short_trigger)} ({dist_pct(ticker.last, short_trigger):+.3f}%)")
        print(f"  OOPS 기준: 전일 저가 {f(prev.low)} / 전일 고가 {f(prev.high)}")
        print("  SL/TP: 신호봉·스윕 극단이 확정된 뒤 계산되므로 대기 중에는 고정값 없음")
    trend = "LONG" if ind['ema20'] > ind['ema50'] else "SHORT" if ind['ema20'] < ind['ema50'] else "FLAT"
    print(f"  1H 추세 {trend} (EMA20 {f(ind['ema20'])} / EMA50 {f(ind['ema50'])})")
    print(f"  %R {ind['wpr']:.1f} (이전 {ind['wpr_prev']:.1f}) | UO {ind['uo']:.1f} | 거래량 {ind['vol']:.2f}x")


def stock_watch(client, settings, symbol, cfg):
    instrument = lw.fetch_instrument(client, symbol)
    ticker = lw.fetch_ticker(client, symbol)
    bars15 = lw.fetch_candles(client, symbol, "15m", int(cfg.get("bars_15m", 500)))
    bars1h = lw.fetch_candles(client, symbol, "1H", int(cfg.get("bars_1h", 220)))
    candidate = lw.stock_candidate(settings, symbol, cfg, instrument, ticker, bars15, bars1h)
    ind = common_indicators(bars15, bars1h)
    now = lw.now_utc()
    ny = now.astimezone(lw.NY)
    c15 = lw.completed_bars(bars15, "15m")
    sessions = lw.group_stock_rth_sessions(c15)
    current_date = ny.date()
    dates = sorted(d for d, rows in sessions.items() if rows)
    entry_window = lw.stock_entry_window(now, str(cfg.get("entry_start_ny", "09:45")), str(cfg.get("entry_end_ny", "15:30")))
    print(f"\n[{symbol}] STOCK PERP")
    print(f"  현재 {f(ticker.last)} | spread {ticker.spread_pct:.4f}% | 뉴욕 {ny:%Y-%m-%d %H:%M:%S}")
    print(f"  진입창 {cfg.get('entry_start_ny','09:45')}~{cfg.get('entry_end_ny','15:30')} NY | 현재 {'OPEN' if entry_window else 'CLOSED'}")
    if candidate:
        threshold = max(settings.signal_threshold, float(cfg.get("signal_threshold", 0)))
        ready = candidate.score >= threshold
        print(f"  모드: {'READY' if ready else 'SETUP_SCORE_LOW'} / {candidate.setup} {candidate.side}")
        print(f"  점수 {candidate.score:.1f}/{threshold:.1f} | 진입기준 {f(candidate.entry_reference)}")
        print(f"  초기SL {f(candidate.stop_price)} | 비상TP {f(candidate.emergency_tp_price)}")
        print(f"  손절폭 {candidate.stop_distance_pct:.3f}% | 계좌위험 약 {candidate.account_risk_pct:.3f}%")
        print(f"  이유: {', '.join(candidate.reasons)}")
    elif not entry_window:
        print("  모드: OUTSIDE_RTH_ENTRY_WINDOW")
        print("  신규 진입 없음. 뉴욕 정규장 진입창에서만 OOPS/돌파를 감시")
    elif current_date not in sessions:
        print("  모드: WAIT_CURRENT_RTH_DATA")
    else:
        previous_dates = [d for d in dates if d < current_date]
        if not previous_dates:
            print("  모드: DATA_WARMUP")
        else:
            prev_rows = sessions[previous_dates[-1]]
            today_rows = sessions[current_date]
            prev_high = max(b.high for b in prev_rows)
            prev_low = min(b.low for b in prev_rows)
            prev_range = prev_high - prev_low
            current_open = today_rows[0].open
            k = float(cfg.get("breakout_k_base", 0.28))
            long_trigger = current_open + k * prev_range
            short_trigger = current_open - k * prev_range
            current_low = min(b.low for b in today_rows)
            current_high = max(b.high for b in today_rows)
            mode = "WAIT_BREAKOUT_OR_OOPS"
            if current_low < prev_low and today_rows[-1].close <= prev_low:
                mode = "WAIT_OOPS_LONG_RECLAIM"
            elif current_high > prev_high and today_rows[-1].close >= prev_high:
                mode = "WAIT_OOPS_SHORT_REJECT"
            print(f"  모드: {mode}")
            print(f"  롱 돌파선 {f(long_trigger)} ({dist_pct(ticker.last, long_trigger):+.3f}%)")
            print(f"  숏 돌파선 {f(short_trigger)} ({dist_pct(ticker.last, short_trigger):+.3f}%)")
            print(f"  OOPS 기준: 전일 저가 {f(prev_low)} / 전일 고가 {f(prev_high)}")
            print("  SL/TP: 신호봉·스윕 극단이 확정된 뒤 계산되므로 대기 중에는 고정값 없음")
    trend = "LONG" if ind['ema20'] > ind['ema50'] else "SHORT" if ind['ema20'] < ind['ema50'] else "FLAT"
    print(f"  1H 추세 {trend} (EMA20 {f(ind['ema20'])} / EMA50 {f(ind['ema50'])})")
    print(f"  %R {ind['wpr']:.1f} (이전 {ind['wpr_prev']:.1f}) | UO {ind['uo']:.1f} | 거래량 {ind['vol']:.2f}x")


def main():
    root = Path.home() / "index-sniper-pro"
    config = root / "config/larry_williams_core_v1.json"
    settings = lw.load_settings(str(config))
    client = lw.make_client()
    state = lw.load_state(settings)
    assets = lw.fetch_account_assets(client)
    equity = lw.account_equity(assets)
    positions = lw.nonzero_positions(client)
    orders = lw.fetch_open_orders(client)
    strategies = lw.fetch_strategy_orders_best_effort(client)
    guards = lw.guard_reasons(settings, state, equity, positions, orders, strategies)
    armed, arm_reasons = lw.arm_valid(settings)

    print("=" * 72)
    print(f"Larry Williams Core v{lw.VERSION} 상세 상태 | {lw.iso()}")
    print(f"LIVE ARMED: {armed} | equity {equity:.2f} | Cross {settings.leverage}x | 진입증거금 {settings.entry_margin_pct:.0f}%")
    print(f"계좌 가드: {', '.join(guards) if guards else 'OK'}")
    if arm_reasons:
        print(f"ARM 차단: {', '.join(arm_reasons)}")
    print_managed(state, client, settings)

    if state.get("managed_position"):
        print("\n신규 진입 감시는 동시 포지션 1개 제한으로 중지됩니다.")
        return

    for symbol, cfg in settings.symbols.items():
        if not bool(cfg.get("enabled", True)):
            continue
        if lw.cooldown_active(state, symbol):
            until = (state.get("cooldown_until") or {}).get(symbol)
            print(f"\n[{symbol}] 모드: COOLDOWN until {until}")
            continue
        profile = str(cfg.get("profile", "crypto"))
        if profile == "stock":
            stock_watch(client, settings, symbol, cfg)
        else:
            eth_watch(client, settings, symbol, cfg)

    print("\n청산 규칙")
    print("  초기 SL: 신호 구조(신호봉/스윕 극단) + ATR 완충, 거래소 mark-price SL")
    print("  비상 TP: ETH 3.0R / SKHY 2.75R")
    print("  +1R 이후: 가격점(WILLSTOP 프록시) 추적손절, 손절은 완화하지 않음")
    print("  no-follow-through: ETH 120분 이후 매시, SKHY 60분 이후 30분마다 수익 상태면 bailout")
    print("  최대 보유: ETH 2160분(36시간), SKHY 390분 + 뉴욕 15:55 강제청산")
    print("=" * 72)


if __name__ == "__main__":
    main()
