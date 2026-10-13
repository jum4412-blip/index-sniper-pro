import unittest
from decimal import Decimal
from risk_core import Policy, plan_size, proposed_capital_ceiling
from ledger import UnitLedger


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.kw = dict(policy=Policy(stage='standard'), symbol='BTCUSDT', side='long',
                       account_equity='700', approved_capital='700', entry='80000',
                       structural_stop='78800', cost_fraction='0.002',
                       qty_step='0.0001', min_qty='0.0001', min_notional='5',
                       available_initial_margin='700', account_reconciled=True,
                       market_data_fresh=True, instrument_rules_verified=True,
                       protection_capability_verified=True)

    def test_standard_calculation(self):
        r = plan_size(**self.kw)
        self.assertEqual(Decimal(r['quantity']), Decimal('0.0025'))
        self.assertEqual(Decimal(r['notional_usdt']), Decimal('200'))
        self.assertEqual(Decimal(r['planned_loss_allowance_usdt']), Decimal('3.4'))
        self.assertEqual(Decimal(r['estimated_initial_margin_usdt']), Decimal('40'))

    def test_micro_calculation(self):
        self.kw['policy'] = Policy(stage='micro')
        self.assertEqual(Decimal(plan_size(**self.kw)['quantity']), Decimal('0.0012'))

    def test_observe_cannot_propose_trade(self):
        self.kw['policy'] = Policy()
        self.assertEqual(plan_size(**self.kw)['reason'], 'OBSERVE_ONLY')

    def test_unreconciled_default_blocks(self):
        del self.kw['account_reconciled']
        self.assertEqual(plan_size(**self.kw)['reason'], 'ACCOUNT_NOT_RECONCILED')

    def test_stale_data_blocks(self):
        self.kw['market_data_fresh'] = False
        self.assertEqual(plan_size(**self.kw)['reason'], 'STALE_MARKET_DATA')

    def test_missing_protection_blocks(self):
        self.kw['protection_capability_verified'] = False
        self.assertEqual(plan_size(**self.kw)['reason'], 'PROTECTION_CAPABILITY_NOT_VERIFIED')

    def test_wrong_side_stop(self):
        self.kw['structural_stop'] = '81000'
        with self.assertRaises(ValueError): plan_size(**self.kw)

    def test_short(self):
        self.kw.update(side='short', structural_stop='81200')
        self.assertEqual(Decimal(plan_size(**self.kw)['quantity']), Decimal('0.0025'))

    def test_nan_fails(self):
        self.kw['account_equity'] = 'NaN'
        with self.assertRaises(ValueError): plan_size(**self.kw)

    def test_missing_cost_fails(self):
        self.kw['cost_fraction'] = None
        with self.assertRaises(ValueError): plan_size(**self.kw)

    def test_below_minimum_not_rounded_up(self):
        self.kw['min_qty'] = '0.01'
        self.assertEqual(plan_size(**self.kw)['reason'], 'BELOW_EXCHANGE_MINIMUM_DO_NOT_ROUND_UP')

    def test_pending_risk_counts(self):
        self.kw['existing_reserved_risk'] = '7'
        self.assertEqual(plan_size(**self.kw)['reason'], 'PORTFOLIO_RISK_LIMIT')

    def test_gross_limit(self):
        self.kw['existing_gross_notional'] = '1050'
        self.assertEqual(plan_size(**self.kw)['reason'], 'EXPOSURE_OR_MARGIN_LIMIT')

    def test_daily_pause(self):
        self.kw['day_return'] = '-0.015'
        self.assertEqual(plan_size(**self.kw)['reason'], 'DAILY_LOSS_PAUSE')

    def test_weekly_pause(self):
        self.kw['seven_day_return'] = '-0.03'
        self.assertEqual(plan_size(**self.kw)['reason'], 'SEVEN_DAY_LOSS_REVIEW_REQUIRED')

    def test_hard_drawdown(self):
        self.kw['drawdown'] = '0.08'
        self.assertEqual(plan_size(**self.kw)['reason'], 'HARD_DRAWDOWN_REVIEW_REQUIRED')

    def test_half_risk(self):
        self.kw['drawdown'] = '0.04'
        self.assertEqual(Decimal(plan_size(**self.kw)['risk_budget_usdt']), Decimal('1.75'))

    def test_no_automatic_size_increase_from_profit(self):
        self.kw['account_equity'] = '1000'
        self.assertEqual(Decimal(plan_size(**self.kw)['risk_basis_usdt']), Decimal('700'))

    def test_losses_shrink_basis_immediately(self):
        self.kw['account_equity'] = '600'
        self.assertEqual(Decimal(plan_size(**self.kw)['risk_basis_usdt']), Decimal('600'))

    def test_same_symbol_no_pyramiding(self):
        self.kw['symbol_already_owned'] = True
        self.assertEqual(plan_size(**self.kw)['reason'], 'NO_PYRAMIDING_OR_SAME_SYMBOL_REENTRY')

    def test_position_limit(self):
        self.kw['open_or_pending_positions'] = 2
        self.assertEqual(plan_size(**self.kw)['reason'], 'POSITION_LIMIT')

    def test_unknown_available_margin(self):
        del self.kw['available_initial_margin']
        self.assertEqual(plan_size(**self.kw)['reason'], 'AVAILABLE_MARGIN_NOT_VERIFIED')

    def test_rounding_grid_risk_invariant(self):
        for stop in ['79999', '79600', '79200', '78800', '76000', '60000']:
            self.kw['structural_stop'] = stop
            r = plan_size(**self.kw)
            if r['status'] == 'SIZE_PROPOSAL_ONLY':
                self.assertLessEqual(Decimal(r['planned_loss_allowance_usdt']), Decimal('3.5'))
                self.assertLessEqual(Decimal(r['notional_usdt']), Decimal('700'))
                self.assertEqual(Decimal(r['quantity']) % Decimal('0.0001'), Decimal('0'))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.ledger = UnitLedger()
        self.ledger.append(event_id='start', timestamp='2026-10-13T00:00:00Z', kind='INIT', amount='700')

    def tearDown(self):
        self.ledger.close()

    def test_deposit_is_not_profit(self):
        self.ledger.append(event_id='loss', timestamp='2026-10-14T00:00:00Z', kind='MARK', amount='630')
        self.ledger.append(event_id='dep', timestamp='2026-10-15T00:00:00Z', kind='FLOW', amount='200', pre_equity='630')
        r = self.ledger.report()
        self.assertEqual(Decimal(r['equity_usdt']), Decimal('830'))
        self.assertEqual(Decimal(r['flow_neutral_return_fraction']), Decimal('-0.1'))
        self.assertEqual(Decimal(r['drawdown_fraction']), Decimal('0.1'))
        self.assertEqual(Decimal(r['trading_pnl_usdt']), Decimal('-70'))

    def test_flow_prevaluation_captures_prior_profit(self):
        self.ledger.append(event_id='dep', timestamp='2026-10-14T00:00:00Z', kind='FLOW', amount='100', pre_equity='770')
        self.assertEqual(Decimal(self.ledger.report()['nav']), Decimal('1.1'))
        self.assertEqual(Decimal(self.ledger.report()['trading_pnl_usdt']), Decimal('70'))

    def test_withdrawal_not_loss(self):
        self.ledger.append(event_id='out', timestamp='2026-10-14T00:00:00Z', kind='FLOW', amount='-100', pre_equity='700')
        self.assertEqual(Decimal(self.ledger.report()['nav']), Decimal('1'))
        self.assertEqual(Decimal(self.ledger.report()['trading_pnl_usdt']), Decimal('0'))

    def test_duplicate_event_noop(self):
        kw = dict(event_id='mark', timestamp='2026-10-14T00:00:00Z', kind='MARK', amount='710')
        self.assertTrue(self.ledger.append(**kw))
        self.assertFalse(self.ledger.append(**kw))
        self.assertEqual(self.ledger.report()['event_count'], '2')

    def test_id_conflict_rejected(self):
        kw = dict(event_id='mark', timestamp='2026-10-14T00:00:00Z', kind='MARK', amount='710')
        self.ledger.append(**kw)
        kw['amount'] = '720'
        with self.assertRaises(ValueError): self.ledger.append(**kw)
        self.assertEqual(Decimal(self.ledger.report()['equity_usdt']), Decimal('710'))

    def test_out_of_order_rejected(self):
        with self.assertRaises(ValueError):
            self.ledger.append(event_id='old', timestamp='2026-10-12T00:00:00Z', kind='MARK', amount='710')

    def test_no_init_reset(self):
        with self.assertRaises(ValueError):
            self.ledger.append(event_id='reset', timestamp='2026-10-14T00:00:00Z', kind='INIT', amount='800')

    def test_flow_needs_prevaluation(self):
        with self.assertRaises(ValueError):
            self.ledger.append(event_id='dep', timestamp='2026-10-14T00:00:00Z', kind='FLOW', amount='100')

    def test_timezone_required(self):
        with self.assertRaises(ValueError):
            self.ledger.append(event_id='bad', timestamp='2026-10-14T00:00:00', kind='MARK', amount='700')

    def test_zero_equity_records_total_loss(self):
        self.ledger.append(event_id='loss', timestamp='2026-10-14T00:00:00Z', kind='MARK', amount='0')
        self.assertEqual(Decimal(self.ledger.report()['drawdown_fraction']), Decimal('1'))
        with self.assertRaises(ValueError):
            self.ledger.append(event_id='reset', timestamp='2026-10-15T00:00:00Z', kind='FLOW', amount='700', pre_equity='1')

    def test_infinite_equity_rejected(self):
        with self.assertRaises(ValueError):
            self.ledger.append(event_id='bad', timestamp='2026-10-14T00:00:00Z', kind='MARK', amount='Infinity')

    def test_full_withdrawal_requires_explicit_closure(self):
        with self.assertRaises(ValueError):
            self.ledger.append(event_id='out', timestamp='2026-10-14T00:00:00Z', kind='FLOW', amount='-700', pre_equity='700')

    def test_unitized_return_after_deposit_and_gain(self):
        self.ledger.append(event_id='flow', timestamp='2026-10-14T00:00:00Z', kind='FLOW', amount='700', pre_equity='700')
        self.ledger.append(event_id='gain', timestamp='2026-10-15T00:00:00Z', kind='MARK', amount='1540')
        self.assertEqual(Decimal(self.ledger.report()['nav']), Decimal('1.1'))
        self.assertEqual(Decimal(self.ledger.report()['trading_pnl_usdt']), Decimal('140'))


class CapitalTests(unittest.TestCase):
    def setUp(self):
        self.kw = dict(previous_capital='700', project_total='1130',
                       evidence_green=True, unresolved_incident=False, drawdown='0')

    def test_green_increase_capped(self):
        self.assertEqual(Decimal(proposed_capital_ceiling(**self.kw)['proposed_ceiling_usdt']), Decimal('770'))

    def test_no_green_no_increase(self):
        self.kw['evidence_green'] = False
        self.assertEqual(proposed_capital_ceiling(**self.kw)['status'], 'NO_INCREASE')

    def test_incident_blocks_increase(self):
        self.kw['unresolved_incident'] = True
        self.assertEqual(proposed_capital_ceiling(**self.kw)['status'], 'NO_INCREASE')

    def test_drawdown_blocks_increase(self):
        self.kw['drawdown'] = '0.04'
        self.assertEqual(proposed_capital_ceiling(**self.kw)['status'], 'NO_INCREASE')

    def test_project_reserve_constraint(self):
        self.kw['project_total'] = '1000'
        self.assertEqual(Decimal(proposed_capital_ceiling(**self.kw)['proposed_ceiling_usdt']), Decimal('700'))


if __name__ == '__main__':
    unittest.main()
