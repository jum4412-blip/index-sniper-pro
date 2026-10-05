"""CLI authorization/restart wiring with durable state and no external effects."""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cci_chop_v1 import cli, config, strategy
from cci_chop_v1.evidence import AUTH_KIND, PROOF_FIELDS
from cci_chop_v1._compat.core import SafetyError
from cci_chop_v1._compat.store import Store

AT = int(datetime(2026, 10, 5, 7, 1, tzinfo=timezone.utc).timestamp()*1000)


class OneLoopStop:
    def __init__(self):
        self.stopped = False

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, _seconds):
        self.stopped = True


class NoNetworkNotifier:
    instances = []

    def __init__(self, store, path, settings, mode):
        self.events = []
        self.started = False
        self.thread = SimpleNamespace(is_alive=lambda: self.started)
        type(self).instances.append(self)

    def start(self):
        self.started = True

    def queue(self, event, key=None):
        self.events.append(deepcopy(event))

    def close(self):
        self.started = False


class NoNetworkMarket:
    def __init__(self, api):
        self.api = api

    def get_partial(self, symbol, decision_time):
        return {'_errors': {}, 'symbol': symbol, 'decision_time': decision_time, 'tick_size': .1}


class CapturingEngine:
    """Capture CLI inputs; simulate ownership to exercise restart, never orders."""
    instances = []

    def __init__(self, api, store, settings, authorized, notify=None):
        self.store = store
        self.c = deepcopy(settings)
        self.authorized = authorized
        self.state = store.get('cci_chop_engine', {'positions': {}, 'pending': {},
                                                'modifications': {}, 'halt': None, 'risk': {}})
        self.ticks = []
        self.authorized_entries = []
        type(self).instances.append(self)

    def tick(self, at, *, signals, dynamic_stops, exits):
        permitted = self.authorized()
        self.ticks.append({'at': at, 'signals': deepcopy(signals),
                           'dynamic_stops': deepcopy(dynamic_stops), 'exits': deepcopy(exits),
                           'authorized': permitted,
                           'managed_positions': deepcopy(self.state['positions'])})
        if permitted:
            self.authorized_entries.extend(deepcopy(signals))
            if not self.state['positions'] and signals:
                self.state['positions']['BTCUSDT'] = {
                    'id': 'OWNED_ONLY_IN_TEST', 'side': 'LONG', 'qty': .01, 'stop': 59000.0,
                }
        for symbol, guard in dynamic_stops.items():
            if symbol in self.state['positions']:
                self.state['positions'][symbol]['stop'] = guard
        self.store.set('cci_chop_engine', self.state)


def candidate(symbol, frames, decision_time):
    return SimpleNamespace(side='long', guard=59000.0, closed_ms=decision_time,
                           event_id='test-event-'+symbol, metadata={
                               'volume_ratio': 2.0, 'signal_close': 60000.0,
                               'spec_sha256': strategy.spec_sha256(),
                               'indicators': {'cci20': 150.0, 'chop14': 20.0,
                                              'cci_frame': strategy.ENTRY_RULE['cci_frame'],
                                              'chop_frame': strategy.ENTRY_RULE['chop_frame']},
                           })


class ExperimentalLiveRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        target = cli.model_path(self.root)
        target.parent.mkdir(parents=True)
        shutil.copyfile(Path(cli.__file__).parent/'results'/'probability_model.json', target)
        self.c = config.validate(config.DEFAULT)
        self.model, self.model_sha = cli.load_model(self.root)
        self.assertEqual(self.model['live_policy'], AUTH_KIND)
        self.assertTrue(all(self.model[k] is False for k in PROOF_FIELDS))
        CapturingEngine.instances.clear()
        NoNetworkNotifier.instances.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def mocks(self):
        stack = ExitStack()
        stack.enter_context(patch.object(cli, 'now_ms', return_value=AT))
        stack.enter_context(patch.object(cli.process_signal, 'signal'))
        stack.enter_context(patch.object(cli.threading, 'Event', OneLoopStop))
        stack.enter_context(patch.object(cli, 'connection', return_value={'env': 'test.env'}))
        stack.enter_context(patch.object(cli, 'telegram_credentials', return_value={'token': 'unused', 'chat': 'unused'}))
        self.api_mock = stack.enter_context(patch.object(cli, 'make_api', return_value=SimpleNamespace()))
        stack.enter_context(patch.object(cli.account, 'identity', return_value='TEST_UID'))
        stack.enter_context(patch.object(cli, 'legacy_guard'))
        stack.enter_context(patch.object(cli, 'Notifier', NoNetworkNotifier))
        stack.enter_context(patch.object(cli, 'MarketFrames', NoNetworkMarket))
        stack.enter_context(patch.object(cli, 'CCEngine', CapturingEngine))
        stack.enter_context(patch.object(cli.strategy, 'analyze', return_value={
            'eligible': True, 'bias': {'W': {'direction': 1}, 'D': {'direction': 1}},
            'wait_reasons': [], 'indicators': {'cci20': 150, 'chop14': 20},
        }))
        stack.enter_context(patch.object(cli.strategy, 'generate_signal', side_effect=candidate))
        stack.enter_context(patch.object(cli.strategy, 'update_guard', return_value=SimpleNamespace(
            should_exit=False, tightened=True, guard=59100.0, reason='STRUCTURAL_GUARD_TIGHTENED')))
        return stack

    def write_arm(self, acknowledged=True):
        cli.state_dir(self.root).mkdir(parents=True, exist_ok=True)
        payload = cli.arm_payload(self.root, self.c, self.model_sha, 'TEST_UID', True)
        payload['unvalidated_live_acknowledged'] = acknowledged
        (cli.state_dir(self.root)/'ARM.json').write_text(json.dumps(payload))
        return payload

    def test_explicit_experiment_admits_eligible_signals_without_fabricated_proof(self):
        with self.mocks():
            self.write_arm()
            cli.run(self.root, 'live', self.c)
        engine = CapturingEngine.instances[0]
        self.assertTrue(engine.c['experimental_live'])
        self.assertTrue(engine.ticks[0]['authorized'])
        self.assertEqual(len(engine.ticks), 1)
        self.assertEqual(len(engine.ticks[0]['signals']), 2)
        for signal in engine.ticks[0]['signals']:
            gate = signal['probability']
            self.assertTrue(gate['eligible'])
            self.assertEqual(gate['authorization_kind'], AUTH_KIND)
            self.assertTrue(gate['unvalidated_live_acknowledged'])
            self.assertIsNone(gate['estimated_probability'])
            self.assertFalse(gate['deployment_approved'])
            self.assertFalse(gate['native_execution_verified'])
            self.assertFalse(gate['prospective_verified'])
        reread, _ = cli.load_model(self.root)
        self.assertTrue(all(reread[k] is False for k in PROOF_FIELDS))
        self.api_mock.assert_called_once_with(self.root, 'live', write=True, demo_env=None)

    def test_pause_then_restart_retains_experimental_binding_and_manages_owned_position(self):
        with self.mocks():
            self.write_arm()
            cli.run(self.root, 'live', self.c)
            before = cli.read_status(self.root, 'live')
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['pause', '--root', str(self.root)]), 0)
            self.assertFalse((cli.state_dir(self.root)/'ARM.json').exists())
            self.assertTrue((cli.state_dir(self.root)/'PAUSE').exists())
            cli.run(self.root, 'live', self.c)
        restarted = CapturingEngine.instances[1]
        self.assertTrue(restarted.c['experimental_live'])
        self.assertFalse(restarted.ticks[0]['authorized'])
        self.assertEqual(restarted.authorized_entries, [])
        self.assertIn('BTCUSDT', restarted.ticks[0]['managed_positions'])
        self.assertEqual(restarted.ticks[0]['dynamic_stops'], {'BTCUSDT': 59100.0})
        after = cli.read_status(self.root, 'live')
        self.assertEqual(after['runtime']['binding'], before['runtime']['binding'])
        self.assertEqual(after['runtime']['started_ms'], before['runtime']['started_ms'])
        self.assertFalse(after['runtime']['running'])
        self.assertFalse(after['observation']['entry_enabled'])
        self.assertEqual(after['positions']['BTCUSDT']['stop'], 59100.0)

    def test_deleted_arm_without_pause_still_manages_owned_position_without_new_entries(self):
        with self.mocks():
            self.write_arm()
            cli.run(self.root, 'live', self.c)
            (cli.state_dir(self.root)/'ARM.json').unlink()
            cli.run(self.root, 'live', self.c)
        restarted = CapturingEngine.instances[1]
        self.assertTrue(restarted.c['experimental_live'])
        self.assertFalse(restarted.ticks[0]['authorized'])
        self.assertEqual(restarted.authorized_entries, [])
        self.assertIn('BTCUSDT', restarted.ticks[0]['dynamic_stops'])

    def test_missing_acknowledgement_cannot_start_an_experimental_runner(self):
        with self.mocks():
            self.write_arm(acknowledged=False)
            with self.assertRaises(SafetyError):
                cli.run(self.root, 'live', self.c)
        self.assertEqual(CapturingEngine.instances, [])
        self.assertEqual(cli.read_status(self.root, 'live')['positions'], {})

    def test_missing_arm_cannot_start_fresh_live_runner_even_with_experimental_model(self):
        with self.mocks():
            with self.assertRaises(SafetyError):
                cli.run(self.root, 'live', self.c)
        self.assertEqual(CapturingEngine.instances, [])

    def test_changed_acknowledgement_after_ownership_revokes_entries_and_keeps_management(self):
        with self.mocks():
            self.write_arm()
            cli.run(self.root, 'live', self.c)
            before = cli.read_status(self.root, 'live')
            self.write_arm(acknowledged=False)
            cli.run(self.root, 'live', self.c)
        restarted = CapturingEngine.instances[1]
        self.assertTrue(restarted.c['experimental_live'])
        self.assertFalse(restarted.ticks[0]['authorized'])
        self.assertEqual(restarted.authorized_entries, [])
        self.assertIn('BTCUSDT', restarted.ticks[0]['managed_positions'])
        self.assertEqual(restarted.ticks[0]['dynamic_stops'], {'BTCUSDT': 59100.0})
        after = cli.read_status(self.root, 'live')
        self.assertEqual(after['runtime']['binding'], before['runtime']['binding'])
        self.assertTrue(after['runtime']['binding']['unvalidated_live_acknowledged'])
        self.assertFalse(after['observation']['entry_enabled'])
        self.assertEqual(after['positions']['BTCUSDT']['stop'], 59100.0)

    def test_malformed_arm_after_ownership_revokes_entries_and_keeps_management(self):
        with self.mocks():
            self.write_arm()
            cli.run(self.root, 'live', self.c)
            before = cli.read_status(self.root, 'live')
            for payload in ('{broken', '[]', 'false'):
                with self.subTest(payload=payload):
                    (cli.state_dir(self.root)/'ARM.json').write_text(payload)
                    cli.run(self.root, 'live', self.c)
                    restarted = CapturingEngine.instances[-1]
                    self.assertFalse(restarted.ticks[0]['authorized'])
                    self.assertEqual(restarted.authorized_entries, [])
                    self.assertIn('BTCUSDT', restarted.ticks[0]['dynamic_stops'])
                    self.assertEqual(cli.read_status(self.root, 'live')['runtime']['binding'], before['runtime']['binding'])

    def test_changed_executable_source_blocks_rebinding_and_preserves_owned_state(self):
        with self.mocks():
            self.write_arm()
            cli.run(self.root, 'live', self.c)
            before = cli.read_status(self.root, 'live')
            with patch.object(cli.config, 'release_digest', return_value='CHANGED_EXECUTABLE_SOURCE'):
                with self.assertRaisesRegex(SafetyError, 'RUNTIME_CHANGED'):
                    cli.run(self.root, 'live', self.c)
        self.assertEqual(len(CapturingEngine.instances), 1)
        after = cli.read_status(self.root, 'live')
        self.assertEqual(after['runtime']['binding'], before['runtime']['binding'])
        self.assertEqual(after['positions'], before['positions'])

    def test_changed_sizing_config_blocks_rebinding_and_preserves_owned_state(self):
        with self.mocks():
            self.write_arm()
            cli.run(self.root, 'live', self.c)
            before = cli.read_status(self.root, 'live')
            changed = config.validate({**self.c, 'margin_cap_usdt': 200.0})
            with self.assertRaisesRegex(SafetyError, 'RUNTIME_CHANGED'):
                cli.run(self.root, 'live', changed)
        self.assertEqual(len(CapturingEngine.instances), 1)
        after = cli.read_status(self.root, 'live')
        self.assertEqual(after['runtime']['binding'], before['runtime']['binding'])
        self.assertEqual(after['positions'], before['positions'])

    def test_changed_evidence_model_blocks_rebinding_and_preserves_owned_state(self):
        with self.mocks():
            self.write_arm()
            cli.run(self.root, 'live', self.c)
            before = cli.read_status(self.root, 'live')
            cli.model_path(self.root).write_text(json.dumps({**self.model, 'native_bitget_data_verified': True}))
            with self.assertRaisesRegex(SafetyError, 'RUNTIME_CHANGED'):
                cli.run(self.root, 'live', self.c)
        self.assertEqual(len(CapturingEngine.instances), 1)
        after = cli.read_status(self.root, 'live')
        self.assertEqual(after['runtime']['binding'], before['runtime']['binding'])
        self.assertEqual(after['positions'], before['positions'])


if __name__ == '__main__':
    unittest.main()
