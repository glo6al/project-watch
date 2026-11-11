"""Officer accounting observes API attempts without influencing delivery."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import server
from officer_usage import UsageLedger


def response(text='Recorded answer.', tools=None):
    return {'model': 'claude-haiku-5-5', 'stop_reason': 'tool_use' if tools else 'end_turn',
            'content': tools or [{'type': 'text', 'text': text}],
            'usage': {'input_tokens': 100, 'output_tokens': 20, 'cache_creation_input_tokens': 0,
                      'cache_read_input_tokens': 0, 'cache_creation': {'ephemeral_5m_input_tokens': 0,
                      'ephemeral_1h_input_tokens': 0}, 'service_tier': 'standard',
                      'inference_geo': 'global', 'speed': 'standard'}}


class OfficerAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = UsageLedger(Path(self.tmp.name).resolve() / 'private')
        self.assertTrue(self.ledger.available)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_dispatch_and_every_answer_round_are_counted_once(self):
        calls = []
        def call(body, **kwargs):
            calls.append(copy.deepcopy(body))
            if len(calls) == 1:
                return response(''), ''
            if len(calls) == 2:
                return response(tools=[{'type': 'tool_use', 'id': 'fleet', 'name': 'list_agents', 'input': {}}]), ''
            return response(), ''
        with patch.object(server, 'OFFICER_USAGE', self.ledger), patch.object(server, 'MESSAGING', None), \
             patch.object(server, 'officer_routing_snapshot', return_value=server.RoutingSnapshot(
                 'claude-haiku-5-5', ('claude-haiku-5-5', False), ('claude-haiku-5-5', False))):
            result = server.officer_ask([{'role': 'user', 'content': 'What is recorded?'}], call,
                                        request_id=str(uuid.uuid4()))
        self.assertEqual(len(calls), 3)
        self.assertEqual(result['text'], 'Recorded answer.')
        self.assertEqual(result['usage_tracking'], {'enabled': True, 'recorded_calls': 3, 'tracking_incomplete': False})
        summary = self.ledger.summary()
        self.assertEqual(summary['calls'], 3)
        self.assertEqual(summary['observed_tokens']['input_tokens'], 300)
        self.assertEqual(summary['priced_calls'], 3)
        self.assertEqual([row[0] for row in self.ledger.db.execute('SELECT stage FROM calls ORDER BY started')],
                         ['dispatch', 'answer', 'answer'])

    def test_storage_failure_preserves_authoritative_control_and_never_retries(self):
        control = {'text': 'Codex confirmed interruption.', 'controls': [{'state': 'interrupted'}], 'actions': [], 'cites': []}
        def dispatch(text, request_id, call, **kwargs):
            call({'model': 'claude-haiku-5-5'}, end=123)
            return copy.deepcopy(control)
        for failure in ('begin', 'finish'):
            with self.subTest(failure=failure), patch.object(self.ledger, failure, side_effect=OSError('synthetic write failure')), \
                 patch.object(server, 'OFFICER_USAGE', self.ledger), patch.object(server, 'officer_dispatch', side_effect=dispatch), \
                 patch.object(server, 'officer_routing_snapshot', return_value=server.RoutingSnapshot(
                     'manual', ('claude-haiku-5-5', False), ('claude-haiku-5-5', False))), \
                 patch.object(server, 'officer_exchange', side_effect=AssertionError('A control cannot become an answer request')):
                calls = []
                def call(body, **kwargs):
                    calls.append(body)
                    return response(), ''
                result = server.officer_ask([{'role': 'user', 'content': 'Interrupt Atlas.'}], call,
                                            request_id=str(uuid.uuid4()))
                self.assertEqual(len(calls), 1)
                self.assertEqual(result['controls'], control['controls'])
                self.assertTrue(result['text'].startswith(control['text']))
                self.assertIn('Cost tracking is incomplete', result['text'])
                self.assertTrue(result['usage_tracking']['tracking_incomplete'])

    def test_error_missing_usage_and_current_reply_snapshot_stay_unknown(self):
        wrap, complete = server.officer_accounting(lambda body, **kwargs: (None, 'timeout'), self.ledger)
        wrap('answer')({'model': 'claude-haiku-5-5'})
        self.assertEqual(self.ledger.summary()['unknown_calls'], 1)
        with patch.object(server, 'OFFICER_USAGE', self.ledger):
            actions = []
            out = server.officer_tool('officer_spend', {}, actions)
            self.assertTrue(out['snapshot_before_final_reply'])
            self.assertTrue(out['estimate_is_partial'])
            self.assertIsNone(out['estimated_usd'])
            self.assertEqual(actions, [])
            self.assertIn('error', server.officer_tool('officer_spend', {'days': True}, actions))
        with patch.object(server, 'OFFICER_USAGE', None):
            self.assertFalse(server.officer_tool('officer_spend', {}, [])['available'])

    def test_raised_transport_leaves_unknown_attempt_without_retry(self):
        calls = []
        def call(body, **kwargs):
            calls.append(body)
            raise RuntimeError('synthetic failure')
        wrapped, _ = server.officer_accounting(call, self.ledger)
        with self.assertRaises(RuntimeError):
            wrapped('dispatch')({'model': 'claude-haiku-5-5'})
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.ledger.summary()['unknown_calls'], 1)

    def test_accounting_warning_preserves_truncation_notice_and_output_limit(self):
        with patch.object(self.ledger, 'begin', return_value=None):
            wrap, complete = server.officer_accounting(lambda body, **kwargs: (response(), ''), self.ledger)
            wrap('answer')({'model': 'claude-haiku-5-5'})
            result = complete({'text': 'word ' * 1000 + 'The answer was cut short.',
                               'controls': [{'state': 'interrupted'}]})
        self.assertLessEqual(len(result['text']), server.OFFICER_TURN_CHARS)
        self.assertIn('The answer was cut short.', result['text'])
        self.assertIn('Cost tracking is incomplete', result['text'])
        self.assertEqual(result['controls'], [{'state': 'interrupted'}])


if __name__ == '__main__':
    unittest.main()
