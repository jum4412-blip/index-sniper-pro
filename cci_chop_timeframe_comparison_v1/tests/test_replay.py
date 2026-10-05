"""Independent causal selection, cost and resampling checks on synthetic markets."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from cci_chop_timeframe_comparison_v1 import runner_base as runner

MIN = runner.replay.MIN
DAY = runner.replay.DAY


def market(symbol="BTCUSDT", prices=None, funding=None):
    prices = np.asarray(prices if prices is not None else [100.] * 40, float)
    minute = np.column_stack((np.arange(len(prices)) * MIN, prices,
                              prices + 1, prices - 1, prices,
                              np.ones(len(prices)), prices))
    item = SimpleNamespace(symbol=symbol, minute=minute,
                           funding=np.asarray(funding if funding is not None else [], float).reshape(-1, 2),
                           tick=.1, qty_step=.0001, fee=.0006,
                           personal_fee=.0004, manifest={})
    return {"market": item}


def event(symbol, minute, exit_minute, identifier, net=0., side="long"):
    signal = {"symbol": symbol, "side": side, "guard": 90., "score": 1.,
              "event_id": identifier, "entry_ms": minute * MIN,
              "entry_i": minute, "entry_ref": 100., "m5_j": 0,
              "metadata": {"volume_ratio": 2.}}
    path = {"exit_ms": exit_minute * MIN, "exit_i": exit_minute,
            "exit_ref": 100., "last_guard": 90., "intraminute": False,
            "reason": "synthetic", "censored": False, "test_net": net}
    return signal, path


def stub_materialize(prepared, signal, path, leverage, multiplier, personal, equity):
    return {"symbol": signal["symbol"], "side": signal["side"],
            "event_id": signal["event_id"], "entry_ms": signal["entry_ms"],
            "exit_ms": path["exit_ms"], "entry": 100., "qty": 1.,
            "entry_fees_usdt": 1., "margin_usdt": 20.,
            "planned_guard_risk_usdt": 1., "net_usdt": path["test_net"],
            "funding_usdt": 0., "cost_multiplier": multiplier,
            "fees_usdt": 2., "notional_usdt": 100., "censored": False}


class DecisionAndSelectionTests(unittest.TestCase):
    def test_only_completed_bar_at_signal_close_not_at_entry_time(self):
        series = np.array([[0., 0.], [runner.replay.HOUR, 0.]])
        signals = [{"entry_ms": runner.replay.HOUR, "side": "long"},
                   {"entry_ms": runner.replay.HOUR + MIN, "side": "long"},
                   {"entry_ms": 2 * runner.replay.HOUR, "side": "long"},
                   {"entry_ms": 2 * runner.replay.HOUR + MIN, "side": "long"}]
        allow, valid = runner.decision_mask(series, runner.replay.HOUR, signals,
                                            np.array([True, False]), np.array([False, True]),
                                            np.array([True, True]))
        np.testing.assert_array_equal(allow, [False, True, True, False])
        np.testing.assert_array_equal(valid, [False, True, True, True])

    def test_short_mask_and_warmup_are_applied_independently(self):
        series = np.array([[0., 0.], [5 * MIN, 0.]])
        signals = [{"entry_ms": 6 * MIN, "side": "short"},
                   {"entry_ms": 11 * MIN, "side": "short"},
                   {"entry_ms": 11 * MIN, "side": "long"}]
        allow, valid = runner.decision_mask(series, 5 * MIN, signals,
                                            np.array([True, False]), np.array([True, True]),
                                            np.array([False, True]))
        np.testing.assert_array_equal(allow, [False, True, False])
        np.testing.assert_array_equal(valid, [False, True, True])

    def test_misaligned_filter_arrays_fail_instead_of_silent_zip(self):
        with self.assertRaisesRegex(ValueError, "alignment"):
            runner.decision_mask(np.zeros((2, 2)), MIN, [],
                                 np.ones(1, bool), np.ones(2, bool), np.ones(2, bool))

    def test_missing_indicator_series_is_unavailable_not_future_fallback(self):
        signals = [{"entry_ms": MIN, "side": "long"}]
        allowed, valid = runner.decision_mask(np.empty((0, 2)), MIN, signals,
                                              np.empty(0, bool), np.empty(0, bool), np.empty(0, bool))
        np.testing.assert_array_equal(allowed, [False])
        np.testing.assert_array_equal(valid, [False])

    def test_filter_rejection_frees_later_full_pool_candidate(self):
        prepared = market()
        prepared["frames"] = {"M5": np.array([[0., 0.], [5 * MIN, 0.]])}
        pairs = [event("BTCUSDT", 6, 20, "early"),
                 event("BTCUSDT", 11, 15, "later")]
        with patch.object(runner.replay, "materialize", stub_materialize), \
                patch.object(runner.replay, "funding_cash", return_value=0.):
            baseline, _ = runner.portfolio({"BTCUSDT": prepared}, {"BTCUSDT": pairs}, 1)
            selected, mask, valid_count = runner.selected_pairs(
                prepared, pairs, {"id": "filter", "frame": "M5"},
                {"filter": {"long": np.array([False, True]),
                            "short": np.array([False, True]), "valid": np.array([True, True])}})
            filtered, _ = runner.portfolio({"BTCUSDT": prepared}, {"BTCUSDT": selected}, 1)
        self.assertEqual([t["event_id"] for t in baseline], ["early"])
        self.assertEqual([t["event_id"] for t in filtered], ["later"])
        np.testing.assert_array_equal(mask, [False, True])
        self.assertEqual(valid_count, 2)

    def test_pending_future_profit_does_not_change_contemporary_sizing(self):
        markets = {"BTCUSDT": market(prices=[100., 100., 105.] + [100.] * 30),
                   "ETHUSDT": market("ETHUSDT")}
        seen = []
        def capture(*args):
            seen.append((args[1]["symbol"], args[-1]))
            return stub_materialize(*args)
        for future_profit in (-100000., 100000.):
            pools = {"BTCUSDT": [event("BTCUSDT", 1, 20, "btc", future_profit)],
                     "ETHUSDT": [event("ETHUSDT", 2, 10, "eth")]}
            with patch.object(runner.replay, "materialize", capture), \
                    patch.object(runner.replay, "funding_cash", return_value=-2.):
                runner.portfolio(markets, pools, 1)
        eth_equities = [eq for symbol, eq in seen if symbol == "ETHUSDT"]
        self.assertEqual(eth_equities, [runner.replay.ACCOUNT_SNAPSHOT + 5. - 1. - 2.] * 2)

    def test_same_exit_minute_retains_position_and_does_not_settle_future_net(self):
        markets = {"BTCUSDT": market(), "ETHUSDT": market("ETHUSDT")}
        seen = []
        def capture(*args):
            seen.append((args[1]["event_id"], args[-1]))
            return stub_materialize(*args)
        pools = {"BTCUSDT": [event("BTCUSDT", 1, 2, "old", 100.),
                             event("BTCUSDT", 2, 4, "same_exit")],
                 "ETHUSDT": [event("ETHUSDT", 2, 3, "other")]}
        with patch.object(runner.replay, "materialize", capture), \
                patch.object(runner.replay, "funding_cash", return_value=0.):
            trades, rejected = runner.portfolio(markets, pools, 1)
        self.assertEqual({t["event_id"] for t in trades}, {"old", "other"})
        self.assertEqual(rejected["position_limit"], 1)
        self.assertEqual(dict(seen)["other"], runner.replay.ACCOUNT_SNAPSHOT - 1.)

    def test_previously_settled_profit_is_available_at_next_entry(self):
        markets = {"BTCUSDT": market(), "ETHUSDT": market("ETHUSDT")}
        seen = []
        def capture(*args):
            seen.append(args[-1])
            return stub_materialize(*args)
        pools = {"BTCUSDT": [event("BTCUSDT", 1, 2, "old", 10.)],
                 "ETHUSDT": [event("ETHUSDT", 3, 4, "next")]}
        with patch.object(runner.replay, "materialize", capture):
            runner.portfolio(markets, pools, 1)
        self.assertEqual(seen, [runner.replay.ACCOUNT_SNAPSHOT, runner.replay.ACCOUNT_SNAPSHOT + 10.])

    def test_aggregate_risk_and_cash_reserve_fail_before_acceptance(self):
        prepared = market()
        pairs = [event("BTCUSDT", 1, 3, "candidate")]
        for field, value, reason in (("planned_guard_risk_usdt", 50., "aggregate_risk"),
                                     ("margin_usdt", runner.replay.ACCOUNT_SNAPSHOT, "cash_reserve")):
            def expensive(*args):
                row = stub_materialize(*args)
                row[field] = value
                return row
            with patch.object(runner.replay, "materialize", expensive):
                trades, rejected = runner.portfolio({"BTCUSDT": prepared}, {"BTCUSDT": pairs}, 1)
            self.assertEqual(trades, [])
            self.assertEqual(rejected[reason], 1)


class CostAndMarkedEquityTests(unittest.TestCase):
    def test_actual_materialization_charges_both_sides_and_directional_friction(self):
        prepared = market()
        signal, path = event("BTCUSDT", 1, 3, "cost")
        path["exit_ref"] = 110.
        personal = runner.replay.materialize(prepared, signal, path, 5, 1, True,
                                             runner.replay.ACCOUNT_SNAPSHOT, unit_notional=True)
        public = runner.replay.materialize(prepared, signal, path, 5, 1, False,
                                           runner.replay.ACCOUNT_SNAPSHOT, unit_notional=True)
        doubled = runner.replay.materialize(prepared, signal, path, 5, 2, True,
                                            runner.replay.ACCOUNT_SNAPSHOT, unit_notional=True)
        self.assertGreater(personal["entry"], signal["entry_ref"])
        self.assertLess(personal["exit"], path["exit_ref"])
        self.assertAlmostEqual(personal["fees_usdt"], .0004 *
                               (personal["entry"] + personal["exit"]) * personal["qty"])
        self.assertAlmostEqual(personal["entry_fees_usdt"], .0004)
        self.assertGreater(public["fees_usdt"], personal["fees_usdt"])
        self.assertLess(doubled["net_usdt"], personal["net_usdt"])
        self.assertEqual(personal["net_usdt"], personal["gross_usdt"] - personal["fees_usdt"])

    def test_funding_boundary_uncertainty_never_grants_ambiguous_receipts(self):
        m = market(funding=[[MIN, -.001], [2 * MIN, -.002], [3 * MIN, -.003]])["market"]
        path = {"exit_ms": 3 * MIN, "intraminute": False}
        # Entry and exit receipts are not credited; only unambiguous midpoint receipt.
        self.assertAlmostEqual(runner.replay.funding_cash(m, MIN, path, 1, 2., 1), .4)
        self.assertAlmostEqual(runner.replay.funding_cash(m, MIN, path, 1, 2., 2), .4)
        # Short owes all three payments; stress multiplies negative funding only.
        self.assertAlmostEqual(runner.replay.funding_cash(m, MIN, path, -1, 2., 2), -2.4)

    def test_intraminute_exit_excludes_following_minute_funding(self):
        m = market(funding=[[MIN, .001], [2 * MIN, .001], [3 * MIN, .001]])["market"]
        value = runner.replay.funding_cash(m, MIN,
                                           {"exit_ms": 2 * MIN, "intraminute": True}, 1, 1., 1)
        self.assertAlmostEqual(value, -.2)

    def test_drawdown_includes_entry_fee_funding_and_terminal_settlement(self):
        prepared = market(prices=[100., 95., 90., 100., 100.],
                          funding=[[MIN, .001]])
        row = {"symbol": "BTCUSDT", "side": "long", "entry_ms": 0,
               "exit_ms": 2 * MIN, "entry": 100., "qty": 1.,
               "entry_fees_usdt": 1., "funding_usdt": -.095,
               "cost_multiplier": 1, "exit_intraminute": False,
               "net_usdt": -12.095}
        drawdown, floor = runner.drawdown_proxy({"BTCUSDT": prepared}, [row], 0, 4 * MIN)
        self.assertAlmostEqual(floor, runner.replay.ACCOUNT_SNAPSHOT - 12.095)
        self.assertAlmostEqual(drawdown, 12.095 / runner.replay.ACCOUNT_SNAPSHOT)

    def test_drawdown_checks_recorded_funding_against_cash_events(self):
        prepared = market(prices=[100.] * 5, funding=[[MIN, .001]])
        row = {"symbol": "BTCUSDT", "side": "long", "entry_ms": 0,
               "exit_ms": 2 * MIN, "entry": 100., "qty": 1.,
               "entry_fees_usdt": 1., "funding_usdt": 0.,
               "cost_multiplier": 1, "exit_intraminute": False, "net_usdt": -2.}
        with self.assertRaisesRegex(ValueError, "Fundingcurve"):
            runner.drawdown_proxy({"BTCUSDT": prepared}, [row], 0, 4 * MIN)

    def test_empty_portfolio_drawdown_stays_at_reference_equity(self):
        self.assertEqual(runner.drawdown_proxy({}, [], 0, MIN),
                         (0., runner.replay.ACCOUNT_SNAPSHOT))


class CalendarAndGateTests(unittest.TestCase):
    def test_week_bins_use_utc_monday_even_when_research_starts_sunday(self):
        start = runner.replay.WEEK_ORIGIN + 6 * DAY
        rows = [{"exit_ms": start + day * DAY, "net_usdt": value, "censored": False}
                for day, value in ((0, 2.), (1, 5.), (7, 3.), (8, 7.))]
        np.testing.assert_array_equal(
            runner.calendar_buckets(rows, start, start + 9 * DAY, runner.replay.WEEK), [2., 8., 7.])

    def test_calendar_keeps_zero_days_and_excludes_censored_marks(self):
        rows = [{"exit_ms": DAY, "net_usdt": 10., "censored": False},
                {"exit_ms": 2 * DAY, "net_usdt": 10000., "censored": True},
                {"exit_ms": 3 * DAY, "net_usdt": 99., "censored": False}]
        np.testing.assert_array_equal(runner.calendar_buckets(rows, 0, 3 * DAY, DAY),
                                      [0., 10., 0.])

    def test_bootstrap_uses_paired_common_draws_identical_strategy_zero_difference(self):
        rows = [{"symbol": "BTCUSDT", "exit_ms": DAY,
                 "net_usdt": 10., "censored": False},
                {"symbol": "ETHUSDT", "exit_ms": 3 * DAY,
                 "net_usdt": -3., "censored": False}]
        with patch.object(runner, "BOOT_REPS", 2000):
            bounds = runner.calendar_bootstrap({"baseline|personal|cost1": rows,
                                                "copy|personal|cost1": list(rows)},
                                               0, 8 * DAY, DAY)
        for coin in ("BTCUSDT", "ETHUSDT", "PORTFOLIO"):
            b = bounds[("copy|personal|cost1", coin)]
            self.assertEqual(b["incremental_lower_5pct_usdt"], 0.)
            self.assertEqual(b["incremental_lower_familywise_usdt"], 0.)
            self.assertEqual(b["net_lower_5pct_usdt"],
                             bounds[("baseline|personal|cost1", coin)]["net_lower_5pct_usdt"])

    def test_censored_rows_do_not_inflate_completed_win_rate_or_samples(self):
        base = {"fees_usdt": 1., "funding_usdt": 0., "notional_usdt": 100., "entry_ms": 0,
                "exit_ms": DAY, "censored": False, "net_usdt": -3.}
        marked = dict(base, censored=True, net_usdt=1000.)
        stat = runner.metrics([base, marked], 0, 4 * DAY)
        self.assertEqual(stat["completed_trades"], 1)
        self.assertEqual(stat["completed_net_usdt"], -3.)
        self.assertEqual(stat["total_marked_net_usdt"], 997.)
        self.assertEqual(stat["net_win_fraction"], 0.)
        self.assertFalse(stat["sample_sufficient"])

    def test_gate_requires_both_coins_both_costs_net_and_improvement(self):
        def stat():
            return {"sample_sufficient": True,
                    "chronological_four_blocks": [{"net_usdt": 1.}] * 4,
                    "bootstrap": {unit: {"net_lower_familywise_usdt": 1.,
                                          "incremental_lower_familywise_usdt": 1.}
                                  for unit in ("day", "week")}}
        block = {f"variant|personal|cost{cost}":
                 {"by_symbol": {coin: stat() for coin in ("BTCUSDT", "ETHUSDT")}}
                 for cost in (1, 2)}
        self.assertTrue(runner.variant_gate(block, "variant")["passed"])
        block["variant|personal|cost2"]["by_symbol"]["ETHUSDT"]["bootstrap"]["week"]["incremental_lower_familywise_usdt"] = 0.
        gate = runner.variant_gate(block, "variant")
        self.assertFalse(gate["passed"])
        self.assertIn("ETHUSDT:cost2:week:IMPROVEMENT_LOWER_NOT_POSITIVE", gate["failure_reasons"])


if __name__ == "__main__":
    unittest.main()
