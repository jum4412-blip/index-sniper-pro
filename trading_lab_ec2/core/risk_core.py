"""Offline position sizing for a linear USDT-margined contract.

No exchange client, credentials, networking, or order submission exists here.
This calculator is not a production risk engine. Instrument rules, margin tiers,
account reconciliation and native protection must be verified independently.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any, Dict

ZERO = Decimal('0')
ONE = Decimal('1')


def number(value: Any, name: str, *, minimum: Decimal = ZERO,
           strictly_positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f'{name}: boolean is not a number')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f'{name}: invalid number') from exc
    if not result.is_finite():
        raise ValueError(f'{name}: non-finite number')
    if result < minimum or (strictly_positive and result <= ZERO):
        raise ValueError(f'{name}: out of range')
    return result


def signed_number(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f'{name}: boolean is not a number')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f'{name}: invalid number') from exc
    if not result.is_finite():
        raise ValueError(f'{name}: non-finite number')
    return result


@dataclass(frozen=True)
class Policy:
    # Proposal, not backtest-optimized settings. All rates are fractions, not %.
    stage: str = 'observe'
    leverage: Decimal = Decimal('5')
    symbol_notional_multiple: Decimal = Decimal('1')
    gross_notional_multiple: Decimal = Decimal('1.5')
    margin_fraction: Decimal = Decimal('0.30')
    max_positions: int = 2

    def validate(self) -> None:
        if self.stage not in {'observe', 'micro', 'standard'}:
            raise ValueError('unknown stage')
        for name in ('leverage', 'symbol_notional_multiple',
                     'gross_notional_multiple', 'margin_fraction'):
            number(getattr(self, name), name, strictly_positive=True)
        if not (ONE <= self.leverage <= Decimal('5')):
            raise ValueError('leverage must be between 1 and 5')
        if self.margin_fraction > ONE:
            raise ValueError('margin_fraction must be <= 1')
        if isinstance(self.max_positions, bool) or self.max_positions not in (1, 2):
            raise ValueError('max_positions must be 1 or 2')


def blocked(reason: str) -> Dict[str, str]:
    return {'status': 'BLOCKED', 'reason': reason,
            'live_order_submission': 'NOT_IMPLEMENTED'}


def plan_size(*, policy: Policy, symbol: str, side: str,
              account_equity: Any, approved_capital: Any,
              entry: Any, structural_stop: Any, cost_fraction: Any,
              qty_step: Any, min_qty: Any, min_notional: Any,
              existing_reserved_risk: Any = '0',
              existing_gross_notional: Any = '0',
              existing_initial_margin: Any = '0',
              available_initial_margin: Any = None,
              open_or_pending_positions: int = 0,
              symbol_already_owned: bool = False,
              drawdown: Any = '0', day_return: Any = '0',
              seven_day_return: Any = '0',
              account_reconciled: bool = False,
              market_data_fresh: bool = False,
              instrument_rules_verified: bool = False,
              protection_capability_verified: bool = False) -> Dict[str, str]:
    """Return a non-executable size proposal, rounding quantity DOWN.

    cost_fraction is a supplied, adverse all-in allowance: estimated entry/exit
    fees, slippage/spread (without double counting) and funding allowance.
    It is NOT a guaranteed upper bound. Existing risk/exposure includes pending
    entries and both symbols; opposite directions receive no offset here.
    structural_stop must already be rounded to the actual exchange price tick.
    available_initial_margin must be a verified account value, not 'max leverage'.
    """
    policy.validate()
    if policy.stage == 'observe':
        return blocked('OBSERVE_ONLY')
    if symbol not in {'BTCUSDT', 'ETHUSDT'}:
        raise ValueError('unsupported symbol')
    if side not in {'long', 'short'}:
        raise ValueError('side must be long or short')
    checks = {'ACCOUNT_NOT_RECONCILED': account_reconciled,
              'STALE_MARKET_DATA': market_data_fresh,
              'INSTRUMENT_RULES_NOT_VERIFIED': instrument_rules_verified,
              'PROTECTION_CAPABILITY_NOT_VERIFIED': protection_capability_verified}
    for reason, ok in checks.items():
        if ok is not True:
            return blocked(reason)
    if (isinstance(open_or_pending_positions, bool)
            or not isinstance(open_or_pending_positions, int)
            or open_or_pending_positions < 0):
        raise ValueError('position count must be a nonnegative integer')
    if symbol_already_owned:
        return blocked('NO_PYRAMIDING_OR_SAME_SYMBOL_REENTRY')
    if open_or_pending_positions >= policy.max_positions:
        return blocked('POSITION_LIMIT')

    equity = number(account_equity, 'account_equity', strictly_positive=True)
    capital = number(approved_capital, 'approved_capital', strictly_positive=True)
    basis = min(equity, capital)
    price = number(entry, 'entry', strictly_positive=True)
    stop = number(structural_stop, 'structural_stop', strictly_positive=True)
    if (side == 'long' and stop >= price) or (side == 'short' and stop <= price):
        raise ValueError('initial protective stop is on the wrong side')
    cost = number(cost_fraction, 'cost_fraction')
    if cost >= ONE:
        raise ValueError('cost_fraction must be < 1; use 0.002 for 0.2%')
    step = number(qty_step, 'qty_step', strictly_positive=True)
    minimum_qty = number(min_qty, 'min_qty', strictly_positive=True)
    minimum_value = number(min_notional, 'min_notional')
    reserved_risk = number(existing_reserved_risk, 'existing_reserved_risk')
    gross = number(existing_gross_notional, 'existing_gross_notional')
    used_margin = number(existing_initial_margin, 'existing_initial_margin')
    if available_initial_margin is None:
        return blocked('AVAILABLE_MARGIN_NOT_VERIFIED')
    available_margin = number(available_initial_margin, 'available_initial_margin')
    dd = number(drawdown, 'drawdown')
    if dd > ONE:
        raise ValueError('drawdown must be <= 1')
    day = signed_number(day_return, 'day_return')
    week = signed_number(seven_day_return, 'seven_day_return')
    if dd >= Decimal('0.08'):
        return blocked('HARD_DRAWDOWN_REVIEW_REQUIRED')
    if day <= Decimal('-0.015'):
        return blocked('DAILY_LOSS_PAUSE')
    if week <= Decimal('-0.03'):
        return blocked('SEVEN_DAY_LOSS_REVIEW_REQUIRED')

    risk_fraction = Decimal('0.0025') if policy.stage == 'micro' else Decimal('0.005')
    total_fraction = risk_fraction * 2
    multiplier = Decimal('0.5') if dd >= Decimal('0.04') else ONE
    risk_budget = min(basis * risk_fraction * multiplier,
                      basis * total_fraction * multiplier - reserved_risk)
    if risk_budget <= ZERO:
        return blocked('PORTFOLIO_RISK_LIMIT')
    distance_fraction = abs(price - stop) / price
    unit_risk = abs(price - stop) + price * cost
    gross_room = basis * policy.gross_notional_multiple - gross
    margin_room = min(basis * policy.margin_fraction - used_margin,
                      available_margin)
    if gross_room <= ZERO or margin_room <= ZERO:
        return blocked('EXPOSURE_OR_MARGIN_LIMIT')

    quantity_limits = {
        'risk': risk_budget / unit_risk,
        'symbol_notional': basis * policy.symbol_notional_multiple / price,
        'portfolio_gross': gross_room / price,
        'initial_margin': margin_room * policy.leverage / price,
    }
    binding = min(quantity_limits, key=quantity_limits.get)
    quantity = (quantity_limits[binding] / step).to_integral_value(rounding=ROUND_FLOOR) * step
    value = quantity * price
    if quantity < minimum_qty or value < minimum_value:
        return blocked('BELOW_EXCHANGE_MINIMUM_DO_NOT_ROUND_UP')
    estimated_risk = quantity * unit_risk
    margin = value / policy.leverage
    return {
        'status': 'SIZE_PROPOSAL_ONLY', 'stage': policy.stage,
        'symbol': symbol, 'side': side, 'risk_basis_usdt': str(basis),
        'quantity': str(quantity), 'notional_usdt': str(value),
        'estimated_initial_margin_usdt': str(margin),
        'planned_loss_allowance_usdt': str(estimated_risk),
        'risk_budget_usdt': str(risk_budget),
        'structural_stop_distance_fraction': str(distance_fraction),
        'supplied_cost_fraction': str(cost), 'binding_limit': binding,
        'risk_multiplier': str(multiplier),
        'live_order_submission': 'NOT_IMPLEMENTED',
        'warning': 'Actual loss may exceed the allowance; this is not an order authorization.'
    }


def proposed_capital_ceiling(*, previous_capital: Any, project_total: Any,
                             evidence_green: bool, unresolved_incident: bool,
                             drawdown: Any) -> Dict[str, str]:
    """Monthly HUMAN-review proposal. Does not move money or change risk rate."""
    previous = number(previous_capital, 'previous_capital', strictly_positive=True)
    total = number(project_total, 'project_total', strictly_positive=True)
    dd = number(drawdown, 'drawdown')
    if dd > ONE:
        raise ValueError('drawdown must be <= 1')
    if evidence_green is not True or unresolved_incident is not False or dd >= Decimal('0.04'):
        return {'status': 'NO_INCREASE', 'proposed_ceiling_usdt': str(previous),
                'reason': 'EVIDENCE_OR_INCIDENT_OR_DRAWDOWN_GATE',
                'automatic_transfer': 'NOT_IMPLEMENTED'}
    ceiling = min(previous * Decimal('1.10'), total * Decimal('0.70'))
    return {'status': 'HUMAN_REVIEW_REQUIRED', 'proposed_ceiling_usdt': str(ceiling),
            'automatic_transfer': 'NOT_IMPLEMENTED'}
