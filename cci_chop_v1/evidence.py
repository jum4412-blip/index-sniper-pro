"""Explicit experimental authorization, never a profitability calibration."""
from ._compat.core import SafetyError
from . import strategy

AUTH_KIND = 'USER_DIRECTED_EXPERIMENTAL'
PROOF_FIELDS = ('native_bitget_data_verified', 'execution_and_protection_verified',
                'prospective_validation_verified', 'deployment_approved')


def experimental_check(model):
    if model.get('live_policy') != AUTH_KIND:
        raise SafetyError('EXPERIMENTAL_POLICY_NOT_DECLARED')
    if any(model.get(key) is not False for key in PROOF_FIELDS):
        raise SafetyError('EXPERIMENTAL_MODEL_PROOF_FLAGS_MUST_REMAIN_FALSE')
    if model.get('future_profitability_guaranteed') is not False:
        raise SafetyError('INVALID_PROFIT_GUARANTEE_FIELD')
    if model.get('strategy_sha256') != strategy.spec_sha256():
        raise SafetyError('MODEL_STRATEGY_MISMATCH')
    if model.get('entry_rule') != strategy.ENTRY_RULE:
        raise SafetyError('MODEL_ENTRY_RULE_MISMATCH')
    if model.get('measured_probability') is not None:
        raise SafetyError('EXPERIMENTAL_MODEL_CANNOT_INVENT_PROBABILITY')


def probability_gate(model, symbol, side, volume_ratio, spec_sha, experimental=False):
    if spec_sha != strategy.spec_sha256() or symbol not in strategy.SYMBOLS or side not in ('long','short'):
        return {'eligible': False, 'reason': 'INVALID_STRATEGY_SIGNAL'}
    if not experimental:
        return {'eligible': False, 'reason': 'NO_VERIFIED_PROBABILITY_CALIBRATION'}
    experimental_check(model)
    return {'eligible': True, 'authorization_kind': AUTH_KIND,
            'unvalidated_live_acknowledged': True,
            'reason': 'USER_DIRECTED_EXPERIMENT_NOT_STATISTICAL_APPROVAL',
            'estimated_probability': None, 'strategy_sha256': spec_sha,
            'deployment_approved': False, 'native_execution_verified': False,
            'prospective_verified': False}
