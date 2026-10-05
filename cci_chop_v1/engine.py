"""Durable, injected Bitget execution engine. This module never starts itself.

An acknowledged write is not execution evidence. Every order is reconciled by
its durable client id and fills; an ambiguous write is never dispatched again.
Positions remain managed when entry authorization or the probability gate fails.
"""
from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime, timezone

from cci_chop_v1._compat.account import available_usdt
from cci_chop_v1._compat.api import fill_summary
from cci_chop_v1._compat.core import CAT, DataError, Rejected, SafetyError, UnknownOrder, ds, now_ms, number, rounded

NAMESPACE = "cci_chop_engine"
TERMINAL = {"filled", "cancelled", "canceled", "rejected", "failed"}
ACTIVE = {"live", "new", "partially_filled"}


def _fresh():
    return {"version": 1, "positions": {}, "pending": {}, "modifications": {},
            "halt": None, "last_tick": None,
            "risk": {"days": {}, "weeks": {}, "consecutive_losses": 0,
                     "equity_peak": None, "initial_equity": None},
            "operational_execution_verified": False}


def _row_qty(row):
    return number(row.get("total"), True)


def _period(now):
    dt = datetime.fromtimestamp(now / 1000, timezone.utc)
    year, week, _ = dt.isocalendar()
    return dt.strftime("%Y-%m-%d"), f"{year}-W{week:02d}"


class CCEngine:
    """API methods are injected; authorization and model validation are external.

    ``tick`` accepts serializable signal dictionaries, a gate or symbol-to-gate
    mapping, dynamic_stops (symbol -> tighter price), and exits (symbol -> reason).
    The executable CLI must authenticate and bind the account before constructing
    this engine. No setting change, foreign cancellation, or position adoption is
    performed here.
    """

    def __init__(self, api, store, config, authorized, notify=None, clock=None):
        self.api, self.store, self.c = api, store, copy.deepcopy(config)
        self.authorized = authorized
        self.notify = notify or (lambda event: None)
        self.clock = clock or now_ms
        self.state = store.get(NAMESPACE, _fresh())
        self.instruments = {}
        self._validate_config()

    def _validate_config(self):
        if not callable(self.authorized):
            raise SafetyError("ENTRY_AUTHORIZATION_REQUIRED")
        if type(self.c.get("experimental_live", False)) is not bool:
            raise SafetyError("EXPERIMENTAL_LIVE_FLAG_INVALID")
        mode = self.c.get("mode", "live")
        if mode not in ("live", "demo", "shadow"):
            raise SafetyError("EXECUTION_MODE_UNKNOWN")
        if getattr(self.api, "is_paper", False) and mode != "shadow":
            raise SafetyError("PAPER_ADAPTER_MODE_MISMATCH")
        if getattr(self.api, "demo", False) and mode != "demo":
            raise SafetyError("DEMO_ADAPTER_MODE_MISMATCH")
        symbols = self.c.get("symbols", ["BTCUSDT", "ETHUSDT"])
        if not symbols or len(set(symbols)) != len(symbols) or not set(symbols) <= {"BTCUSDT", "ETHUSDT"}:
            raise SafetyError("UNSUPPORTED_CC_UNIVERSE")
        leverage = number(self.c.get("leverage", 5), True)
        if leverage not in (1, 2, 3, 4, 5):
            raise SafetyError("LEVERAGE_OUT_OF_RANGE")
        if not 0 < number(self.c.get("margin_cap_usdt", 300), True) <= 300:
            raise SafetyError("MARGIN_CAP_EXCEEDED")
        if self.c.get("max_positions", 2) not in (1, 2):
            raise SafetyError("POSITION_CAP_EXCEEDED")
        if not 0 < number(self.c.get("risk_fraction", .01), True) <= .01:
            raise SafetyError("RISK_FRACTION_EXCEEDED")
        if not 0 < number(self.c.get("max_trade_risk_usdt", 12), True) <= 12:
            raise SafetyError("TRADE_RISK_CAP_EXCEEDED")
        caps = {"max_portfolio_risk_fraction": .02, "daily_loss_fraction": .02,
                "weekly_loss_fraction": .05, "max_drawdown_fraction": .10,
                "entry_slippage_bps": 8., "exit_slippage_bps": 10., "max_chase_bps": 15.,
                "protection_deadline_seconds": 10.}
        for key, maximum in caps.items():
            if key in self.c and not 0 < number(self.c[key], True) <= maximum:
                raise SafetyError("CONFIG_RISK_CAP_EXCEEDED:" + key)

    def save(self):
        self.store.set(NAMESPACE, self.state)

    def _time(self, prior):
        # Refresh after slow inventory/API reads. Tests may inject a millisecond
        # clock; monotonicity prevents a backward local adjustment replaying time.
        return max(int(prior), int(self.clock()))

    def event(self, kind, **data):
        self.store.event("CC_" + kind, data)
        # Telegram failure cannot erase an order intent or disable protection.
        try:
            self.notify({"kind": kind, **data})
        except Exception:
            self.store.event("CC_NOTIFICATION_FAILED", {"kind": kind})

    def halt(self, reason):
        if not self.state.get("halt"):
            self.state["halt"] = reason
            self.save()
            self.event("HALT", reason=reason)

    def _instrument(self, symbol):
        inst = self.api.instrument(symbol)
        self.instruments[symbol] = inst
        return inst

    def _pending_for(self, symbol, kind=None):
        return [p for p in self.state["pending"].values()
                if p["symbol"] == symbol and (kind is None or p["kind"] == kind)]

    def _position_row(self, rows, p):
        matched = [r for r in rows if r.get("category", CAT) == CAT and
                   r.get("symbol") == p["symbol"] and
                   str(r.get("posSide", "")).upper() == p["side"]]
        if len(matched) > 1:
            raise SafetyError("AMBIGUOUS_OWNED_POSITION")
        return matched[0] if matched else None

    def _assert_position(self, row, p):
        if row.get("symbol") != p["symbol"] or row.get("category", CAT) != CAT or str(row.get("posSide", "")).upper() != p["side"]:
            raise SafetyError("POSITION_SIDE_OR_CATEGORY_CHANGED")
        if row.get("createdTime") is None or abs(int(row["createdTime"]) - p["opened"]) > 10000:
            raise SafetyError("POSITION_IDENTITY_CHANGED")
        if abs(_row_qty(row) - p["qty"]) > p["qty_step"] / 2:
            raise SafetyError("POSITION_QUANTITY_CHANGED")
        if row.get("marginMode") != "crossed" or number(row.get("leverage")) != p["leverage"]:
            raise SafetyError("POSITION_SETTINGS_CHANGED")
        if p.get("exchange_position_id") and row.get("positionId") and str(row["positionId"]) != p["exchange_position_id"]:
            raise SafetyError("POSITION_IDENTITY_CHANGED")

    def _risk_blocks(self, snap, now):
        risk = self.state["risk"]
        equity = number(snap["equity"], True)
        risk["last_equity"] = equity
        if risk["initial_equity"] is None:
            risk["initial_equity"] = equity
        risk["equity_peak"] = max(risk["equity_peak"] or equity, equity)
        day, week = _period(now)
        risk["days"].setdefault(day, {"start_equity": equity, "net": 0.})
        risk["weeks"].setdefault(week, {"start_equity": equity, "net": 0.})
        blocks = []
        for bucket, key, fraction, label in (("days", day, self.c.get("daily_loss_fraction", .02), "DAILY_LOSS_LIMIT"),
                                             ("weeks", week, self.c.get("weekly_loss_fraction", .05), "WEEKLY_LOSS_LIMIT")):
            item = risk[bucket][key]
            if min(item["net"], equity - item["start_equity"]) <= -fraction * item["start_equity"]:
                blocks.append(label)
        if equity <= risk["equity_peak"] * (1 - self.c.get("max_drawdown_fraction", .10)):
            blocks.append("ACCOUNT_DRAWDOWN_LIMIT")
        risk["active_blocks"] = blocks
        self.save()
        return blocks

    def _settings(self, snap, symbol):
        settings = snap["settings"]
        if settings.get("accountMode") != "unified" or settings.get("holdMode") not in ("one_way_mode", "hedge_mode"):
            raise SafetyError("ACCOUNT_MODE_NOT_VERIFIED")
        rows = [r for r in settings.get("symbolConfigList", []) if r.get("category") == CAT and r.get("symbol") == symbol]
        if not rows or any(r.get("marginMode") != "crossed" or number(r.get("leverage")) != self.c.get("leverage", 5) for r in rows):
            raise SafetyError("CROSSED_CONFIGURED_LEVERAGE_NOT_VERIFIED")
        return settings["holdMode"]

    def _owned_inventory(self, snap):
        verified = True
        observed_at = int(snap.get("observed_at", self._time(self.state.get("last_tick") or 0)))
        known_orders = set(self.state["pending"])
        known_ids = {str(p["oid"]) for p in self.state["pending"].values() if p.get("oid")}
        known_ids.update(child_id for p in self.state["positions"].values()
                         for child_id in p.get("native_children", {}))
        known_stops = {str(p["protection_id"]) for p in self.state["positions"].values() if p.get("protection_id")}
        for row in snap["positions"]:
            symbol = row.get("symbol")
            p = self.state["positions"].get(symbol)
            if p:
                if p.get("closing") and int(snap.get("observed_at", self.state.get("last_tick") or 0)) < p.get("next_exit_at", 0):
                    probe = dict(p, qty=_row_qty(row))
                    self._assert_position(row, probe)
                    if not p["qty"] - p["qty_step"] / 2 <= _row_qty(row) <= p["original_qty"] + p["qty_step"] / 2:
                        raise SafetyError("POSITION_QUANTITY_CHANGED")
                    continue
                self._assert_position(row, p)
                continue
            # A durable entry may precede order detail propagation. Do not adopt it.
            pending = self._pending_for(symbol, "ENTRY")
            if pending and row.get("category", CAT) == CAT and str(row.get("posSide", "")).upper() == pending[0]["signal"]["side"]:
                intent = pending[0]
                if (len(pending) != 1 or row.get("createdTime") is None or
                        abs(int(row["createdTime"]) - intent["created"]) > 10000 or
                        _row_qty(row) > intent["sized"]["qty"] + intent["instrument"]["qty_step"] / 2 or
                        row.get("marginMode") != "crossed" or number(row.get("leverage")) != intent["leverage"]):
                    raise SafetyError("PENDING_POSITION_OWNERSHIP_MISMATCH")
                verified = False
                if observed_at >= intent["created"] + self.c.get("protection_deadline_seconds", 10) * 1000:
                    self.halt("PENDING_POSITION_OWNERSHIP_UNRESOLVED")
                continue
            raise SafetyError("FOREIGN_POSITION_PRESENT")
        for row in snap["orders"]:
            if str(row.get("clientOid", "")) not in known_orders and str(row.get("orderId", "")) not in known_ids:
                raise SafetyError("FOREIGN_ORDER_PRESENT")
        for row in snap["strategies"]:
            if str(row.get("orderId", "")) in known_stops:
                continue
            symbol = row.get("symbol")
            p = self.state["positions"].get(symbol)
            if p and not p.get("protection_id") and self._stop_matches(row, p, binding=True):
                continue
            # Known entry preset can appear before fills become readable.
            expected = [x for x in self.state["pending"].values() if x["kind"] == "ENTRY" and
                        row.get("category", CAT) == CAT and row.get("symbol") == x["symbol"] and
                        str(row.get("posSide", "")).upper() == x["signal"]["side"] and
                        row.get("createdTime") is not None and abs(int(row["createdTime"]) - x["created"]) <= 10000 and
                        row.get("qty") not in (None, "") and 0 < number(row["qty"]) <= x["sized"]["qty"] + x["instrument"]["qty_step"] / 2 and
                        row.get("stopLoss") not in (None, "") and abs(number(row["stopLoss"]) - x["sized"]["stop"]) <= x["instrument"]["price_step"] / 2 and
                        row.get("slTriggerBy") == "mark" and row.get("slOrderType") == "market" and
                        str(row.get("status", "")).lower() == "pending"]
            if len(expected) == 1:
                verified = False
                if observed_at >= expected[0]["created"] + self.c.get("protection_deadline_seconds", 10) * 1000:
                    self.halt("PENDING_STOP_OWNERSHIP_UNRESOLVED")
                continue
            raise SafetyError("FOREIGN_STRATEGY_PRESENT")
        return verified

    def _gate(self, signal, probability):
        gate = signal.get("probability")
        if gate is None and isinstance(probability, dict):
            gate = probability.get(signal["symbol"], probability)
        if not isinstance(gate, dict) or gate.get("eligible") is not True:
            return False
        paper = self.c.get("mode", "live") == "shadow"
        if gate.get("paper_only") is not paper:
            return False
        mode = self.c.get("mode", "live")
        if mode == "demo":
            if gate.get("demo_only") is not True or getattr(self.api, "demo", False) is not True:
                return False
        elif mode == "live":
            if gate.get("demo_only") is not False:
                return False
            proofs = ("deployment_approved", "native_execution_verified", "prospective_verified")
            if gate.get("authorization_kind") == "USER_DIRECTED_EXPERIMENTAL":
                # An explicit experimental authorization is not a fabricated
                # model promotion.  The runner binds its acknowledgement to
                # source/config/evidence/account; every proof stays false.
                if (self.c.get("experimental_live", False) is not True
                        or gate.get("unvalidated_live_acknowledged") is not True
                        or any(gate.get(k) is not False for k in proofs)):
                    return False
            elif any(gate.get(k) is not True for k in proofs):
                return False
        elif mode != "shadow":
            return False
        if gate.get("side") != signal["side"] or not isinstance(gate.get("model_sha256"), str) or len(gate["model_sha256"]) != 64:
            return False
        return True

    def enter(self, signal, probability, snap, now):
        now = self._time(now)
        signal = copy.deepcopy(signal)
        symbol, side = signal["symbol"], str(signal["side"]).upper()
        signal["side"] = side
        if symbol not in self.c.get("symbols", ["BTCUSDT", "ETHUSDT"]) or side not in ("LONG", "SHORT"):
            raise SafetyError("INVALID_SIGNAL")
        if not self._gate(signal, probability) or not self.authorized() or self.state["halt"]:
            return False
        # Entry/exit outcomes and native modifications must settle before taking
        # more exposure. A delayed second signal is not consumed by this deferral.
        if self.state["pending"] or self.state["modifications"]:
            return False
        if symbol in self.state["positions"] or self._pending_for(symbol):
            return False
        entries = sum(p["kind"] == "ENTRY" for p in self.state["pending"].values())
        if len(self.state["positions"]) + entries >= self.c.get("max_positions", 2):
            return False
        signal_ts = int(signal.get("signal_ts", signal.get("closed_ms", 0)))
        if not 60000 <= now - signal_ts <= 90000:
            return False
        key = "CC_SIGNAL_" + str(signal.get("signal_id") or f"{symbol}:{side}:{signal_ts}")
        if self.store.seen(key):
            return False
        if self._risk_blocks(snap, now):
            self.halt("ACCOUNT_RISK_LIMIT")
            return False
        inventory_verified = self._owned_inventory(snap)
        if not inventory_verified or self.state["halt"]:
            return False
        for p in self.state["positions"].values():
            if p.get("native_children_unresolved") or p.get("native_child_active"):
                return False
            if not p.get("protection_id") or not any(self._stop_matches(row, p) for row in snap["strategies"]):
                return False
        hold_mode = self._settings(snap, symbol)
        inst, q = self._instrument(symbol), self.api.quote(symbol)
        now = self._time(now)
        q.validate(now)
        if q.spread_pct * 100 > number(self.c.get("max_spread_bps", 10), True):
            raise SafetyError("ENTRY_SPREAD_TOO_WIDE")
        if abs(q.mark / q.index - 1) * 10000 > number(self.c.get("max_mark_basis_bps", 25), True):
            raise SafetyError("ENTRY_MARK_BASIS_TOO_WIDE")
        sign = 1 if side == "LONG" else -1
        if sign * q.funding > number(self.c.get("max_adverse_funding_rate", .0005), True):
            raise SafetyError("ADVERSE_FUNDING_VETO")
        fee = number(self.api.fee(symbol))
        now = self._time(now)
        q.validate(now)
        if not 0 <= fee < .01:
            raise SafetyError("PERSONAL_FEE_INVALID")
        stop = rounded(number(signal["stop"], True), inst.price_step, up=side == "SHORT")
        reference = number(signal.get("entry", q.entry(side)), True)
        if abs(q.entry(side) / reference - 1) * 10000 > number(self.c.get("max_chase_bps", 15), True):
            raise SafetyError("ENTRY_CHASE_TOO_FAR")
        limit = rounded(q.entry(side) * (1 + sign * number(self.c.get("entry_slippage_bps", 8)) / 10000), inst.price_step, up=side == "LONG")
        if sign * (q.mark - stop) <= inst.price_step or sign * (limit - stop) <= 0:
            raise SafetyError("STOP_ALREADY_BREACHED")
        exit_impact = number(self.c.get("exit_slippage_bps", 10)) / 10000
        worst_exit = stop * (1 - sign * exit_impact)
        cost_per_qty = sign * (limit - worst_exit) + fee * (limit + worst_exit)
        risk_budget = min(number(snap["equity"], True) * self.c.get("risk_fraction", .01), self.c.get("max_trade_risk_usdt", 12))
        leverage = self.c.get("leverage", 5)
        value_cap = self.c.get("margin_cap_usdt", 300) * leverage
        qty = rounded(min(risk_budget / cost_per_qty, value_cap / max(limit, q.entry(side)), inst.max_qty), inst.qty_step)
        if qty < inst.min_qty or qty * limit < inst.min_value:
            raise SafetyError("RISK_SIZED_BELOW_EXCHANGE_MINIMUM")
        planned_risk = qty * cost_per_qty
        aggregate = sum(p.get("planned_risk_usdt", 0.) for p in self.state["positions"].values())
        aggregate += sum(p["sized"]["planned_risk_usdt"] for p in self.state["pending"].values() if p["kind"] == "ENTRY" and p["symbol"] not in self.state["positions"])
        if aggregate + planned_risk > number(snap["equity"], True) * self.c.get("max_portfolio_risk_fraction", .02) + 1e-8:
            raise SafetyError("AGGREGATE_STOP_RISK_EXCEEDED")
        required = qty * max(limit, q.entry(side)) / leverage + 2 * qty * max(limit, q.entry(side)) * fee + self.c.get("margin_reserve_usdt", 150)
        if available_usdt(snap["assets"]) + 1e-8 < required:
            raise SafetyError("FREE_COLLATERAL_WITH_RESERVE_INSUFFICIENT")
        cid = "MSE_" + uuid.uuid4().hex[:26]
        body = {"category": CAT, "symbol": symbol, "side": "buy" if side == "LONG" else "sell", "qty": ds(qty),
                "orderType": "limit", "price": ds(limit), "timeInForce": "ioc", "clientOid": cid,
                "stopLoss": ds(stop), "slTriggerBy": "mark", "slOrderType": "market"}
        if hold_mode == "hedge_mode":
            body["posSide"] = side.lower()
        else:
            body["reduceOnly"] = "no"
        if not self.authorized() or self.state["halt"]:
            return False
        now = self._time(now)
        if not 60000 <= now - signal_ts <= 90000:
            return False
        q.validate(now)
        self.store.signal(key, {"decision": "CONSUMED_BEFORE_WRITE", "signal": signal})
        pending = {"cid": cid, "symbol": symbol, "kind": "ENTRY", "body": body, "signal": signal,
                   "created": now, "hold_mode": hold_mode, "equity": snap["equity"], "leverage": leverage,
                   "sized": {"qty": qty, "stop": stop, "fee": fee, "planned_risk_usdt": planned_risk,
                             "risk_budget": risk_budget, "limit": limit}, "instrument": vars(inst), "reported_qty": 0.}
        self.state["pending"][cid] = pending
        self.save()  # crash here or after request: reconciliation only, never replay
        self.event("ENTRY_INTENT", symbol=symbol, side=side, cid=cid, qty=qty, stop=stop)
        self._dispatch(pending)
        return True

    def _dispatch(self, pending):
        try:
            result = self.api.post("/api/v3/trade/place-order", pending["body"])
            if not isinstance(result, dict) or not result.get("orderId"):
                raise UnknownOrder("ORDER_ACK_MISSING_ID")
            pending["oid"] = str(result["orderId"])
            self.save()
        except Rejected:
            pending["rejected"] = True
            self.save()
            if pending["kind"] != "ENTRY":
                self.state["positions"][pending["symbol"]]["exit_rejected"] = True
                self.halt("CLOSE_ORDER_REJECTED")
                self.save()
            self.event("ORDER_REJECTED", cid=pending["cid"], order_kind=pending["kind"])
        except Exception:
            # Includes unexpected transport errors: dispatched intent stays durable.
            self.event("ORDER_UNKNOWN", cid=pending["cid"], action="NO_RESEND")

    def _order_detail(self, pending):
        info = self.api.order(pending.get("oid", ""), pending["cid"])
        if not isinstance(info, dict) or info.get("symbol") != pending["symbol"] or info.get("clientOid") != pending["cid"]:
            raise SafetyError("ORDER_OWNERSHIP_MISMATCH")
        if not info.get("orderId") or (pending.get("oid") and str(info["orderId"]) != pending["oid"]):
            raise SafetyError("ORDER_ID_MISMATCH")
        if info.get("category", CAT) != CAT or info.get("side") != pending["body"]["side"]:
            raise SafetyError("ORDER_SIDE_OR_CATEGORY_MISMATCH")
        if pending["hold_mode"] == "hedge_mode" and info.get("posSide") != pending["body"]["posSide"]:
            raise SafetyError("ORDER_POSITION_SIDE_MISMATCH")
        qty = number(info.get("cumExecQty"))
        step = pending["instrument"]["qty_step"]
        if not 0 <= qty <= number(pending["body"]["qty"], True) + step / 2:
            raise SafetyError("ORDER_FILL_EXCEEDS_AUTHORIZED_QUANTITY")
        if qty + step / 2 < pending["reported_qty"]:
            raise SafetyError("ORDER_CUMULATIVE_FILL_REGRESSED")
        pending["reported_qty"] = qty
        pending["oid"] = str(info["orderId"])
        self.save()
        return info, qty

    def _fills(self, pending, qty):
        fills = self.api.fills(pending["oid"])
        for fill in fills:
            if str(fill.get("orderId", "")) != pending["oid"] or fill.get("symbol") != pending["symbol"]:
                raise SafetyError("FILL_OWNERSHIP_MISMATCH")
            if fill.get("side") is not None and fill["side"] != pending["body"]["side"]:
                raise SafetyError("FILL_SIDE_MISMATCH")
        summary = fill_summary(fills)
        if summary is None or abs(summary["qty"] - qty) > pending["instrument"]["qty_step"] / 2:
            return None
        return summary

    def reconcile_orders(self, now):
        for cid in list(self.state["pending"]):
            pending = self.state["pending"].get(cid)
            if not pending:
                continue
            if pending.get("rejected"):
                del self.state["pending"][cid]
                self.save()
                continue
            try:
                info, qty = self._order_detail(pending)
            except DataError:
                continue
            now = self._time(now)
            status = str(info.get("orderStatus", "")).lower()
            if status not in TERMINAL | ACTIVE:
                raise SafetyError("UNKNOWN_ORDER_STATUS")
            if qty > 0:
                summary = self._fills(pending, qty)
                now = self._time(now)
                if summary is None:
                    continue
                if pending["kind"] == "ENTRY":
                    self._record_entry(pending, summary, status in TERMINAL, now)
                else:
                    self._record_exit(pending, summary, now)
            if status in TERMINAL:
                if pending["kind"] == "EXIT" and qty == 0:
                    self.halt("CLOSE_ORDER_UNFILLED")
                    self.state["positions"][pending["symbol"]]["exit_rejected"] = True
                del self.state["pending"][cid]
                self.save()
            elif pending["kind"] == "ENTRY" and now - pending["created"] > 10000 and not pending.get("cancel_intent"):
                pending["cancel_intent"] = {"at": now}
                self.save()
                try:
                    self.api.post("/api/v3/trade/cancel-order", {"category": CAT, "symbol": pending["symbol"], "clientOid": cid})
                except Exception:
                    self.event("CANCEL_UNKNOWN", cid=cid, action="NO_RESEND")
                self.halt("ENTRY_IOC_NOT_TERMINAL")

    def _record_entry(self, pending, summary, terminal, now):
        symbol, sig, sized = pending["symbol"], pending["signal"], pending["sized"]
        p = self.state["positions"].get(symbol)
        if p is None:
            p = {"id": pending["cid"], "symbol": symbol, "side": sig["side"], "opened": summary["first"],
                 "entry": summary["price"], "original_qty": summary["qty"], "qty": summary["qty"], "closed_qty": 0.,
                 "price_step": pending["instrument"]["price_step"], "qty_step": pending["instrument"]["qty_step"],
                 "stop": sized["stop"], "initial_stop": sized["stop"], "exchange_stop": sized["stop"],
                 "planned_risk_usdt": sized["planned_risk_usdt"], "risk_budget": sized["risk_budget"],
                 "fee_rate": sized["fee"], "hold_mode": pending["hold_mode"], "leverage": pending["leverage"],
                 "protection_deadline": pending["created"] + int(self.c.get("protection_deadline_seconds", 10) * 1000),
                 "entry_cid": pending["cid"], "entry_oid": pending["oid"], "entry_terminal": terminal,
                 "entry_equity": pending["equity"], "signal": sig, "exit_orders": []}
            self.state["positions"][symbol] = p
            self.event("OPEN", id=p["id"], opened=p["opened"], symbol=symbol, side=sig["side"], entry=summary["price"], qty=summary["qty"], stop=sized["stop"], risk_usdt=sized["planned_risk_usdt"], setup_meta=sig.get("setup_meta",{}), virtual=self.c.get("mode") == "shadow", mode=self.c.get("mode", "live").upper())
        elif p["entry_cid"] != pending["cid"]:
            raise SafetyError("POSITION_ENTRY_ID_CONFLICT")
        p["entry"], p["original_qty"] = summary["price"], summary["qty"]
        p["qty"] = max(0., summary["qty"] - p["closed_qty"])
        p["entry_terminal"] = terminal
        p["entry_fills"] = summary["fills"]
        sign = 1 if p["side"] == "LONG" else -1
        worst = p["initial_stop"] * (1 - sign * self.c.get("exit_slippage_bps", 10) / 10000)
        actual_risk = summary["qty"] * (sign * (p["entry"] - worst) + p["fee_rate"] * (p["entry"] + worst))
        p["initial_risk_usdt"] = actual_risk
        if sign * (p["entry"] - p["initial_stop"]) <= 0 or actual_risk > p["risk_budget"] + 1e-8:
            p["force_exit"] = "POST_FILL_RISK_EXCEEDED"
            self.halt("POST_FILL_RISK_EXCEEDED")
        self.save()

    def _record_exit(self, pending, summary, now):
        p = self.state["positions"][pending["symbol"]]
        if summary["qty"] > pending["pre_qty"] + p["qty_step"] / 2:
            raise SafetyError("EXIT_FILL_EXCEEDS_OWNED_QUANTITY")
        prior = next((x for x in p["exit_orders"] if x["cid"] == pending["cid"]), None)
        accounted = prior["qty"] if prior else 0.
        delta = summary["qty"] - accounted
        if delta < -p["qty_step"] / 2:
            raise SafetyError("EXIT_CONFIRMED_FILL_REGRESSED")
        if delta <= p["qty_step"] / 2:
            return
        p["closed_qty"] += delta
        p["qty"] = max(0., p["original_qty"] - p["closed_qty"])
        if prior:
            prior.update(qty=summary["qty"], fills=summary["fills"])
        else:
            p["exit_orders"].append({"cid": pending["cid"], "oid": pending["oid"], "qty": summary["qty"], "fills": summary["fills"]})
        p["closing"] = True
        p["next_exit_at"] = now + 10000  # allow exchange position propagation
        self.save()

    def reconcile_native_children(self, p, now):
        """Account native stop fills from its owned parent and actual fills only.

        The whole read is staged before changing quantity.  Active/unreadable
        children prohibit another close; durable cumulative fill deltas prevent
        duplicate accounting after a crash.  This is offline-tested protocol
        handling, not evidence of actual Bitget production execution.
        """
        if self.c.get("mode", "live") == "shadow" or not p.get("protection_id"):
            return True
        p["native_children_unresolved"] = True
        self.save()
        read = getattr(self.api, "strategy_sub_orders", None)
        if not callable(read):
            raise DataError("NATIVE_CHILD_READ_UNAVAILABLE")
        rows = read(str(p["protection_id"]))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise DataError("NATIVE_CHILD_RESPONSE_INVALID")
        previous = p.get("native_children", {})
        staged = copy.deepcopy(previous)
        seen = set()
        side = "sell" if p["side"] == "LONG" else "buy"
        extra = 0.
        events = []
        for row in rows:
            child_id = row.get("subOrderId")
            child_cid = row.get("subClientOid")
            if (not isinstance(child_id, str) or not child_id or child_id in seen
                    or not isinstance(child_cid, str) or not child_cid):
                raise SafetyError("NATIVE_CHILD_IDENTITY_INVALID")
            seen.add(child_id)
            if (row.get("category") != CAT or row.get("symbol") != p["symbol"]
                    or row.get("side") != side
                    or str(row.get("posSide", "")).upper() != p["side"]):
                raise SafetyError("NATIVE_CHILD_OWNERSHIP_MISMATCH")
            stamp = number(row.get("createdTime"), True)
            if stamp != int(stamp) or not p["opened"] - 10000 <= stamp <= self._time(now) + 2000:
                raise SafetyError("NATIVE_CHILD_CREATION_TIME_INVALID")
            status = str(row.get("status", "")).lower()
            if status not in TERMINAL | ACTIVE:
                raise DataError("NATIVE_CHILD_STATUS_UNKNOWN")
            ordered = number(row.get("qty"), True)
            cumulative = number(row.get("cumExecQty"))
            step = p["qty_step"]
            if (ordered > p["original_qty"] + step / 2
                    or not 0 <= cumulative <= ordered + step / 2):
                raise SafetyError("NATIVE_CHILD_QUANTITY_INVALID")
            old = previous.get(child_id)
            if old and (old["cid"] != child_cid or old["created"] != int(stamp)
                        or abs(old["ordered_qty"] - ordered) > step / 2
                        or old["parent_id"] != str(p["protection_id"])):
                raise SafetyError("NATIVE_CHILD_IDENTITY_CHANGED")
            accounted = old["qty"] if old else 0.
            if cumulative + step / 2 < accounted:
                raise SafetyError("NATIVE_CHILD_CUMULATIVE_FILL_REGRESSED")
            if old and old["status"] in TERMINAL and status in ACTIVE:
                raise SafetyError("NATIVE_CHILD_STATUS_REGRESSED")
            summary = None
            if cumulative > 0:
                fills = self.api.fills(child_id)
                if not isinstance(fills, list) or any(not isinstance(fill, dict) for fill in fills):
                    raise DataError("NATIVE_CHILD_FILL_LIST_INVALID")
                for fill in fills:
                    if (str(fill.get("orderId", "")) != child_id
                            or fill.get("symbol") != p["symbol"] or fill.get("side") != side
                            or fill.get("category", CAT) != CAT
                            or fill.get("tradeSide", "close") != "close"):
                        raise SafetyError("NATIVE_CHILD_FILL_OWNERSHIP_MISMATCH")
                    fill_stamp = number(fill.get("createdTime"), True)
                    if fill_stamp != int(fill_stamp) or not stamp - 10000 <= fill_stamp <= self._time(now) + 2000:
                        raise SafetyError("NATIVE_CHILD_FILL_TIME_INVALID")
                summary = fill_summary(fills)
                if summary is None or abs(summary["qty"] - cumulative) > step / 2:
                    raise DataError("NATIVE_CHILD_FILLS_NOT_YET_RECONCILED")
                if old and any(f not in summary["fills"] for f in old.get("fills", [])):
                    raise SafetyError("NATIVE_CHILD_PRIOR_FILL_CHANGED")
            record = {"parent_id": str(p["protection_id"]), "cid": child_cid,
                      "created": int(stamp), "ordered_qty": ordered, "qty": cumulative,
                      "status": status, "fills": summary["fills"] if summary else []}
            staged[child_id] = record
            delta = cumulative - accounted
            if delta > step / 2:
                extra += delta
                events.append((child_id, delta, summary["price"]))
        # A disappearing nonterminal child is not proof of its cancellation.
        if any(child_id not in seen and record["status"] in ACTIVE
               for child_id, record in previous.items()):
            raise DataError("NATIVE_CHILD_ACTIVE_READBACK_MISSING")
        if p["closed_qty"] + extra > p["original_qty"] + p["qty_step"] / 2:
            raise SafetyError("NATIVE_CHILD_CLOSE_EXCEEDS_OWNED_QUANTITY")
        p["native_children"] = staged
        p["closed_qty"] += extra
        p["qty"] = max(0., p["original_qty"] - p["closed_qty"])
        p["native_child_active"] = any(record["status"] in ACTIVE for record in staged.values())
        p["native_children_unresolved"] = False
        p["native_children_checked_at"] = self._time(now)
        if any(record["qty"] > p["qty_step"] / 2 for record in staged.values()):
            p["closing"] = True
            p["exit_reason"] = "EXCHANGE_NATIVE_STRUCTURAL_STOP"
            if not p["native_child_active"] and p["qty"] > p["qty_step"] / 2:
                p["force_exit"] = "NATIVE_STOP_TERMINAL_RESIDUAL"
        self.save()
        for child_id, delta, price in events:
            self.event("NATIVE_STOP_FILL_CONFIRMED", symbol=p["symbol"], child_id=child_id,
                       qty=delta, price=price, remaining_qty=p["qty"])
        return not p["native_child_active"]

    def _stop_matches(self, row, p, binding=False, target=None):
        if row.get("category", CAT) != CAT or row.get("symbol") != p["symbol"] or str(row.get("posSide", "")).upper() != p["side"]:
            return False
        if str(row.get("status", "")).lower() != "pending" or row.get("slTriggerBy") != "mark" or row.get("slOrderType") != "market":
            return False
        if not row.get("orderId") or row.get("qty") in (None, "") or row.get("stopLoss") in (None, ""):
            return False
        if abs(number(row["qty"], True) - p["qty"]) > p["qty_step"] / 2:
            return False
        wanted = target if target is not None else p["exchange_stop"]
        if abs(number(row["stopLoss"], True) - wanted) > p["price_step"] / 2:
            return False
        if binding:
            if row.get("createdTime") is None or abs(int(row["createdTime"]) - p["opened"]) > 10000:
                return False
        elif str(row["orderId"]) != str(p.get("protection_id", "")):
            return False
        return True

    def _protection(self, rows, p, now):
        binding = not p.get("protection_id")
        found = [r for r in rows if self._stop_matches(r, p, binding=binding)]
        if len(found) > 1:
            self.halt("AMBIGUOUS_NATIVE_STOP")
            p["force_exit"] = "AMBIGUOUS_NATIVE_STOP"
            self.save()
            return False
        if found:
            if binding:
                p["protection_id"] = str(found[0]["orderId"])
                self.save()
            p["protection_checked_at"] = now
            return True
        if now >= p["protection_deadline"]:
            self.halt("NATIVE_STOP_MISSING_OR_CHANGED")
            p["force_exit"] = "NATIVE_STOP_MISSING_OR_CHANGED"
            self.save()
        return False

    def close(self, symbol, reason, now, rows=None):
        p = self.state["positions"].get(symbol)
        if not p or p["qty"] <= p["qty_step"] / 2 or self._pending_for(symbol, "EXIT") or p.get("exit_rejected"):
            return False
        if now < p.get("next_exit_at", 0):
            return False
        if not self.reconcile_native_children(p, now):
            return False
        now = self._time(now)
        if p.get("native_children_unresolved") or p.get("native_child_active"):
            return False
        # Native fill reads can change quantity after a caller's snapshot.
        # Always obtain a new exchange position before the reducing mutation.
        row = self._position_row(self.api.positions(), p)
        if row is None:
            return False
        self._assert_position(row, p)
        # Subtracting actual native/local fills can introduce binary float
        # tails such as 0.07200000000000001.  Submit a valid quantity multiple,
        # but never round an unexplained fractional position up into exposure.
        exit_qty = rounded(p["qty"] + p["qty_step"] * 1e-9, p["qty_step"])
        if exit_qty <= 0 or abs(exit_qty - p["qty"]) > p["qty_step"] * 1e-8:
            raise SafetyError("RESIDUAL_QUANTITY_NOT_EXCHANGE_INCREMENT")
        cid = "TSX_" + uuid.uuid4().hex[:26]
        body = {"category": CAT, "symbol": symbol, "side": "sell" if p["side"] == "LONG" else "buy",
                "qty": ds(exit_qty), "orderType": "market", "clientOid": cid}
        if p["hold_mode"] == "hedge_mode":
            body["posSide"] = p["side"].lower()
        else:
            body["reduceOnly"] = "yes"
        pending = {"cid": cid, "symbol": symbol, "kind": "EXIT", "body": body, "created": now,
                   "reason": reason, "pre_qty": p["qty"], "hold_mode": p["hold_mode"], "reported_qty": 0.,
                   "instrument": {"qty_step": p["qty_step"]}}
        p["closing"], p["exit_reason"] = True, reason
        self.state["pending"][cid] = pending
        self.save()
        self.event("EXIT_INTENT", symbol=symbol, cid=cid, reason=reason, qty=p["qty"])
        self._dispatch(pending)
        return True

    def tighten(self, symbol, new_stop, now, rows=None):
        p = self.state["positions"].get(symbol)
        if (not p or not p.get("protection_id") or p.get("closing")
                or p.get("native_children_unresolved") or p.get("native_child_active")
                or symbol in self.state["modifications"] or self._pending_for(symbol)):
            return False
        sign = 1 if p["side"] == "LONG" else -1
        target = rounded(number(new_stop, True), p["price_step"], up=p["side"] == "SHORT")
        if sign * (target - p["exchange_stop"]) < p["price_step"] - 1e-10:
            return False
        q = self.api.quote(symbol)
        now = self._time(now)
        q.validate(now)
        if sign * (q.mark - target) <= p["price_step"]:
            self.close(symbol, "STRUCTURE_STOP_ALREADY_BREACHED", now)
            return False
        guards = rows if rows is not None else self.api.strategies(kind="tpsl")
        if not self._protection(guards, p, now):
            return False
        body = {"category": CAT, "symbol": symbol, "orderId": p["protection_id"], "qty": ds(p["qty"]),
                "stopLoss": ds(target), "slTriggerBy": "mark", "slOrderType": "market"}
        mod = {"body": body, "old": p["exchange_stop"], "target": target, "created": now, "outcome": "DISPATCH_UNCONFIRMED"}
        self.state["modifications"][symbol] = mod
        self.save()
        try:
            self.api.post("/api/v3/trade/modify-strategy-order", body)
            mod["outcome"] = "ACK_UNCONFIRMED"
        except Rejected:
            mod["outcome"] = "REJECTED"
            self.halt("NATIVE_STOP_MODIFICATION_REJECTED")
        except Exception:
            mod["outcome"] = "UNKNOWN"
            self.halt("NATIVE_STOP_MODIFICATION_UNKNOWN")
        self.save()
        return True

    def reconcile_modifications(self, rows, now):
        for symbol in list(self.state["modifications"]):
            mod = self.state["modifications"][symbol]
            p = self.state["positions"].get(symbol)
            if not p:
                continue
            matches = [r for r in rows if self._stop_matches(r, p, target=mod["target"])]
            if len(matches) == 1:
                p["exchange_stop"] = p["stop"] = mod["target"]
                p["protection_checked_at"] = now
                del self.state["modifications"][symbol]
                self.save()
                self.event("STOP_UPDATE_VERIFIED", symbol=symbol, stop=p["exchange_stop"])
            elif len(matches) > 1:
                self.halt("NATIVE_STOP_MODIFICATION_AMBIGUOUS")
            elif now - mod["created"] >= 10000:
                # Keep old stop and the unresolved durable intent; never resend.
                self.halt("NATIVE_STOP_MODIFICATION_READBACK_UNRESOLVED")

    def _finish(self, p, now):
        if self._pending_for(p["symbol"]):
            return
        result = self.api.history_match(p, now)
        if result is None:
            p.setdefault("flat_seen", now)
            self.save()
            self.halt("CLOSED_POSITION_ACCOUNTING_UNRESOLVED")
            return
        permitted_accounting = "PAPER_ESTIMATE_BEFORE_FUNDING" if self.c.get("mode") == "shadow" else "EXCHANGE_POSITION_HISTORY_VERIFIED"
        if result.get("accounting") != permitted_accounting:
            raise SafetyError("EXCHANGE_ACCOUNTING_NOT_VERIFIED")
        net = number(result["net_usdt"])
        risk = self.state["risk"]
        day, week = _period(int(result["closed"]))
        for bucket, key in (("days", day), ("weeks", week)):
            risk[bucket].setdefault(key, {"start_equity": p["entry_equity"], "net": 0.})["net"] += net
        risk["consecutive_losses"] = risk["consecutive_losses"] + 1 if net < 0 else 0
        trade = {**p, **result, "closed": int(result["closed"]), "mode": self.c.get("mode", "live").upper(), "reason": p.get("exit_reason", "EXCHANGE_STOP_OR_EXTERNAL_CLOSE")}
        del self.state["positions"][p["symbol"]]
        self.state["modifications"].pop(p["symbol"], None)
        # Reuse Store schema, but never Store.finish: its state key is legacy engine.
        with self.store.db:
            self.store.db.execute("INSERT OR REPLACE INTO trades VALUES(?,?,?)", (trade["id"], trade["closed"], json.dumps(trade, allow_nan=False)))
            self.store.db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (NAMESPACE, json.dumps(self.state, allow_nan=False)))
        self.event("CLOSE", id=trade["id"], closed=trade["closed"], symbol=trade["symbol"], net_usdt=net, funding_usdt=result.get("funding_usdt"), reason=trade["reason"], virtual=self.c.get("mode") == "shadow", mode=self.c.get("mode", "live").upper())

    def tick(self, now, signals=(), probability=None, dynamic_stops=None, exits=None):
        """Manage first; a paused bot can still reduce its own confirmed exposure."""
        now = int(now)
        try:
            self.reconcile_orders(now)
            now = self._time(now)
            snap = self.api.inventory()
            now = self._time(now)
            snap["observed_at"] = now
            self._risk_blocks(snap, now)
            guards = snap["strategies"]
            self.reconcile_modifications(guards, now)
            for symbol, p in list(self.state["positions"].items()):
                try:
                    if not self.reconcile_native_children(p, now):
                        continue
                    now = self._time(now)
                    row = self._position_row(self.api.positions(), p)
                    if row is None:
                        if now >= p.get("next_exit_at", 0):
                            self._finish(p, now)
                        continue
                    if p.get("closing") and now < p.get("next_exit_at", 0):
                        continue
                    self._assert_position(row, p)
                    if row.get("positionId") and not p.get("exchange_position_id"):
                        p["exchange_position_id"] = str(row["positionId"])
                        self.save()
                    self._protection(guards, p, now)
                    q = self.api.quote(symbol)
                    now = self._time(now)
                    q.validate(now)
                    current_rows = self.api.positions()
                    now = self._time(now)
                    current_row = self._position_row(current_rows, p)
                    if current_row is None:
                        self._finish(p, now)
                        continue
                    self._assert_position(current_row, p)
                    sign = 1 if p["side"] == "LONG" else -1
                    if sign * (q.mark - p["exchange_stop"]) <= 0:
                        p["force_exit"] = "NATIVE_STOP_TRIGGER_OR_GAP"
                    reason = p.get("force_exit") or (exits or {}).get(symbol)
                    if reason or p.get("closing"):
                        self.close(symbol, reason or p.get("exit_reason", "CONTINUE_PARTIAL_CLOSE"), now, current_rows)
                    elif (dynamic_stops or {}).get(symbol) is not None:
                        self.tighten(symbol, dynamic_stops[symbol], now, guards)
                except SafetyError as exc:
                    self.halt(str(exc))
                    self.event("MANAGEMENT_BLOCKED", symbol=symbol, reason=str(exc))
            snap = self.api.inventory()
            now = self._time(now)
            snap["observed_at"] = now
            self._owned_inventory(snap)
            self.state["last_tick"] = now
            self.save()
            for signal in signals:
                if self.enter(signal, probability, snap, now):
                    # Reconcile first fills and their native guard before another
                    # signal; never use acknowledgement as ownership evidence.
                    self.reconcile_orders(now)
                    now = self._time(now)
                    snap = self.api.inventory()
                    now = self._time(now)
                    snap["observed_at"] = now
                    self._risk_blocks(snap, now)
                    for p in self.state["positions"].values():
                        row = self._position_row(snap["positions"], p)
                        if row is not None and not p.get("closing"):
                            self._assert_position(row, p)
                            self._protection(snap["strategies"], p, now)
        except SafetyError as exc:
            self.halt(str(exc))
            self.event("MANAGEMENT_BLOCKED", reason=str(exc))
        return copy.deepcopy(self.state)
