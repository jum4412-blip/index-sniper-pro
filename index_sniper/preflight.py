from __future__ import annotations

import json
from dataclasses import asdict

from index_sniper.config import Settings
from index_sniper.exchange.bitget_uta import BitgetUTAClient
from index_sniper.position import open_positions
from index_sniper.risk.sizing import build_size_plan, extract_instrument, extract_symbol_config, extract_usdt_equity_available
from index_sniper.telegram.bot import TelegramBot


def _short(data: object, limit: int = 20000) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    return text[:limit] + ("..." if len(text) > limit else "")


def run_preflight(settings: Settings, client: BitgetUTAClient, tg: TelegramBot) -> list[dict]:
    assets = client.assets()
    account = client.account_info()
    settings_response = client.settings()
    equity, available = extract_usdt_equity_available(assets)
    reports: list[dict] = []

    tg.send(
        "🧭 <b>Index Sniper Pro v0.4 PRE-FLIGHT</b>\n"
        "실주문 없음\n"
        f"대상: {', '.join(settings.symbols)}\n"
        f"DRY_RUN: {settings.dry_run}\n"
        f"USDT available: {available:.4f}\n"
        f"목표 레버리지: {settings.leverage}x"
    )

    for symbol in settings.symbols:
        item: dict = {"symbol": symbol}
        try:
            price = client.last_price(symbol, settings.category)
            instrument = extract_instrument(client.instruments(symbol, settings.category), symbol)
            sym_cfg = extract_symbol_config(settings_response, symbol) or {}
            positions = client.current_position(symbol, settings.category)
            opens = open_positions(positions, symbol=symbol)
            current_leverage = int(sym_cfg.get("leverage") or 0) if sym_cfg else None
            current_margin_mode = sym_cfg.get("marginMode") if sym_cfg else None
            size_plan = build_size_plan(
                equity=equity,
                available=available,
                symbol_count=len(settings.symbols),
                capital_ratio=settings.capital_ratio,
                leverage=settings.leverage,
                price=price,
                instrument=instrument,
            )
            item.update(
                {
                    "price": price,
                    "current_leverage": current_leverage,
                    "target_leverage": settings.leverage,
                    "leverage_ok": current_leverage == settings.leverage,
                    "current_margin_mode": current_margin_mode,
                    "target_margin_mode": settings.margin_mode,
                    "margin_mode_ok": current_margin_mode == settings.margin_mode,
                    "open_position_count": len(opens),
                    "open_positions": opens,
                    "instrument": {
                        "minOrderQty": instrument.get("minOrderQty"),
                        "minOrderAmount": instrument.get("minOrderAmount"),
                        "quantityPrecision": instrument.get("quantityPrecision"),
                        "quantityMultiplier": instrument.get("quantityMultiplier"),
                        "maxMarketOrderQty": instrument.get("maxMarketOrderQty"),
                    },
                    "size_plan": asdict(size_plan),
                    "ok": current_leverage == settings.leverage
                    and current_margin_mode == settings.margin_mode
                    and len(opens) == 0
                    and size_plan.valid,
                }
            )
        except Exception as exc:
            item["ok"] = False
            item["error"] = str(exc)
        reports.append(item)

    print("===== PREFLIGHT v0.4 =====")
    print(_short({"account_code": account.get("code"), "available": available, "symbols": reports}, 30000))

    failed = [r["symbol"] for r in reports if not r.get("ok")]
    if failed:
        lines = ["⚠️ <b>v0.4 PREFLIGHT 확인 필요</b>", f"실패/경고: {', '.join(failed)}"]
        for r in reports:
            if not r.get("ok"):
                lines.append(
                    f"- {r['symbol']}: lev {r.get('current_leverage')}→{settings.leverage}, "
                    f"margin {r.get('current_margin_mode')}→{settings.margin_mode}, "
                    f"openPos {r.get('open_position_count')}, "
                    f"size {r.get('size_plan', {}).get('final_qty')} ({r.get('size_plan', {}).get('reason')})"
                )
        tg.send("\n".join(lines))
    else:
        tg.send(
            "✅ <b>v0.4 PREFLIGHT 성공</b>\n"
            "3개 심볼 레버리지/마진/포지션/수량 계산 정상\n"
            f"대상: {', '.join(settings.symbols)}"
        )
    return reports
