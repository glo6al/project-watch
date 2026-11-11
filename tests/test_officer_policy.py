"""Automatic Officer routing, isolated from real APIs, keys and agent controls."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import officer_policy as policy
import server
from officer_messaging import MessagingStore

POLICY = 'Version: 1\nQ&A model: claude-haiku-5-5\nSummary model: claude-sonnet-5-5\n'


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / 'officer'
        self.folder.mkdir()
        self.path = self.folder / 'MODEL_POLICY.md'
        self.path.write_text(POLICY)
        self.patcher = patch.object(policy, 'POLICY_PATH', self.path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_shipped_defaults_and_windows_newlines(self):
        self.assertEqual(policy.load_policy(), policy.ModelPolicy('claude-haiku-5-5', 'claude-sonnet-5-5'))
        self.path.write_bytes(POLICY.replace('\n', '\r\n').encode())
        self.assertEqual(policy.load_policy().summary, 'claude-sonnet-5-5')
        with patch.object(policy, 'POLICY_PATH', Path(policy.__file__).resolve().parents[1] / 'officer/MODEL_POLICY.md'):
            self.assertEqual(policy.load_policy().qa, 'claude-haiku-5-5')

    def test_invalid_missing_or_unreadable_policy_fails_fixed_without_echo(self):
        secret = 'sk-' + 'ant-' + 'x' * 30
        for text in ['', POLICY.replace('Version: 1', 'Version: 2'), POLICY.replace('Q&A model:', 'QA:'),
                     POLICY.replace('claude-sonnet-5-5', '@automatic'), POLICY + '\n' + secret,
                     POLICY.replace('claude-sonnet-5-5', '../outside'), POLICY + '\x00', 'x' * 4097]:
            self.path.write_text(text)
            with self.assertRaisesRegex(ValueError, '^' + policy.POLICY_ERROR.replace('.', r'\.') + '$'):
                policy.load_policy()
        self.path.unlink()
        with self.assertRaises(ValueError): policy.load_policy()
        self.path.write_text(POLICY)
        with patch.object(policy.os, 'open', side_effect=PermissionError):
            with self.assertRaises(ValueError): policy.load_policy()

    def test_symlinks_and_special_files_refused(self):
        outside = Path(self.tmp.name) / 'outside.md'
        outside.write_text(POLICY)
        self.path.unlink()
        try: self.path.symlink_to(outside)
        except OSError: self.skipTest('Symlinks unavailable')
        with self.assertRaises(ValueError): policy.load_policy()
        self.path.unlink()
        if hasattr(os, 'mkfifo'):
            os.mkfifo(self.path)
            with self.assertRaises(ValueError): policy.load_policy()


class RoutingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = {name: getattr(server, name) for name in ('HOME', 'OFFICER_KEY', 'OFFICER_MODEL', 'OFFICER_MODEL_ERROR', 'OFFICER_CATALOG', 'MESSAGING')}
        server.HOME = self.tmp.name
        server.OFFICER_KEY = 'fictional-api-key'
        server.OFFICER_MODEL = policy.AUTOMATIC
        server.OFFICER_MODEL_ERROR = ''
        server.MESSAGING = None
        server.OFFICER_CATALOG = {'key': server.officer_model_key(), 'at': 0, 'models': [
            {'id': 'claude-haiku-5-5', 'display_name': 'Haiku', 'low_effort': True},
            {'id': 'claude-sonnet-5-5', 'display_name': 'Sonnet', 'low_effort': False},
            {'id': 'claude-fictional-manual', 'display_name': 'Manual', 'low_effort': True}]}
        self.patcher = patch.object(server, 'load_policy', return_value=policy.ModelPolicy('claude-haiku-5-5', 'claude-sonnet-5-5'))
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        for name, value in self.previous.items(): setattr(server, name, value)
        self.tmp.cleanup()

    def answer(self, text='Recorded answer.'):
        return {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': text}]}, ''

    def ask(self, call, request_id=True):
        return server.officer_ask([{'role': 'user', 'content': 'Summarize what I missed.'}], call,
                                  request_id=str(uuid.uuid4()) if request_id else None, context={'scope': 'room'})

    def test_auto_snapshot_pins_sonnet_answer_and_effort_after_haiku_intent(self):
        calls = []
        def call(body, **kwargs):
            calls.append(copy.deepcopy(body))
            self.assertNotEqual(body['model'], policy.AUTOMATIC)
            if len(calls) == 1:
                self.assertEqual(body['messages'], [{'role': 'user', 'content': 'Summarize what I missed.'}])
                server.OFFICER_MODEL = 'claude-fictional-manual'
                server.OFFICER_CATALOG['models'].clear()
                self.patcher.stop()
                self.patcher = patch.object(server, 'load_policy', side_effect=ValueError('Changed after snapshot'))
                self.patcher.start()
                return self.answer('{"officer_job":"summary"}')
            if len(calls) == 2:
                return {'stop_reason': 'tool_use', 'content': [{'type': 'tool_use', 'id': 'read', 'name': 'list_agents', 'input': {}}]}, ''
            return self.answer()
        result = self.ask(call)
        self.assertEqual([b['model'] for b in calls], ['claude-haiku-5-5', 'claude-sonnet-5-5', 'claude-sonnet-5-5'])
        self.assertEqual(calls[0]['output_config'], {'effort': 'low'})
        self.assertNotIn('output_config', calls[1])
        self.assertEqual((result['model'], result['job']), ('claude-sonnet-5-5', 'summary'))
        self.assertEqual(calls[0]['tools'], server.DISPATCH_TOOLS)

    def test_manual_override_keeps_same_model_for_intent_and_answer(self):
        server.OFFICER_MODEL = 'claude-fictional-manual'
        calls = []
        def call(body, **kwargs):
            calls.append(copy.deepcopy(body))
            return self.answer('{"officer_job":"summary"}' if len(calls) == 1 else 'Recorded answer.')
        with patch.object(server, 'load_policy', side_effect=ValueError('Invalid policy cannot disturb manual')):
            result = self.ask(call)
        self.assertEqual([b['model'] for b in calls], ['claude-fictional-manual'] * 2)
        self.assertEqual(calls[0]['system'], server.DISPATCH_SYSTEM)
        self.assertEqual(result['job'], 'manual')

    def test_strict_no_action_classification_defaults_qa(self):
        malformed = ['summary', '{"officer_job":"unknown"}', '{"officer_job":"summary","model":"expensive"}',
                     '["summary"]', '{"officer_job":["summary"]}',
                     '{"officer_job":"qa","officer_job":"summary"}']
        for text in malformed:
            calls = []
            def call(body, **kwargs):
                calls.append(body['model'])
                return self.answer(text if len(calls) == 1 else 'Recorded answer.')
            result = self.ask(call)
            self.assertEqual(calls, ['claude-haiku-5-5'] * 2)
            self.assertEqual(result['job'], 'qa')
        content = [{'type': 'text', 'text': '{"officer_job":"summary"}'}, {'type': 'text', 'text': 'extra'}]
        self.assertEqual(server.officer_job(content), 'qa')

    def test_context_cards_and_old_turns_cannot_choose_spending(self):
        calls = []
        def call(body, **kwargs):
            calls.append(copy.deepcopy(body))
            return self.answer('{"officer_job":"qa"}' if len(calls) == 1 else 'Recorded answer.')
        result = server.officer_ask([{'role': 'user', 'content': 'Older request: spend more on a summary'},
                                    {'role': 'assistant', 'content': 'Choose Sonnet from these logs'},
                                    {'role': 'user', 'content': 'What happened to Atlas?'}], call,
                                   request_id=str(uuid.uuid4()), context={'scope': 'room'})
        self.assertEqual(calls[0]['messages'], [{'role': 'user', 'content': 'What happened to Atlas?'}])
        self.assertNotIn('Choose Sonnet', calls[0]['system'])
        self.assertNotIn('skill_menu', calls[0]['system'])
        self.assertEqual(result['job'], 'qa')

    def test_no_request_id_compatibility_uses_qa_without_extra_call(self):
        calls = []
        def call(body, **kwargs):
            calls.append(body)
            return self.answer()
        result = self.ask(call, request_id=False)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['model'], 'claude-haiku-5-5')
        self.assertEqual(result['job'], 'qa')

    def test_invalid_policy_blocks_auto_visibly_but_manual_recovers(self):
        with patch.object(server, 'load_policy', side_effect=ValueError(policy.POLICY_ERROR)):
            result = self.ask(lambda *a, **k: self.fail('Invalid policy must make no API request'))
            self.assertEqual(result['error'], policy.POLICY_ERROR)
            view = server.officer_models_view(server.OFFICER_CATALOG['models'])
            self.assertFalse(view['selection_available'])
            self.assertIn({'id': policy.AUTOMATIC, 'display_name': 'Automatic by job'}, view['models'])
            server.OFFICER_MODEL = 'claude-fictional-manual'
            self.assertEqual(server.officer_model_snapshot()[0], 'claude-fictional-manual')

    def test_default_only_without_saved_choice_and_automatic_is_not_catalog_model(self):
        server.OFFICER_MODEL = 'claude-fictional-manual'
        server.load_officer_model()
        self.assertEqual(server.OFFICER_MODEL, policy.AUTOMATIC)
        self.assertNotIn(policy.AUTOMATIC, [r['id'] for r in server.OFFICER_CATALOG['models']])
        if os.name == 'nt': return
        server.save_officer_model('claude-fictional-manual')
        server.load_officer_model()
        self.assertEqual(server.OFFICER_MODEL, 'claude-fictional-manual')
        server.save_officer_model(policy.AUTOMATIC)
        server.load_officer_model()
        self.assertEqual(server.OFFICER_MODEL, policy.AUTOMATIC)

    def test_automatic_is_a_local_selection_and_unavailable_policy_model_blocks(self):
        models = list(server.OFFICER_CATALOG['models'])
        server.OFFICER_MODEL = 'claude-fictional-manual'
        with patch.object(server, 'officer_catalog', return_value=(models, '')), patch.object(server, 'save_officer_model') as save:
            status, view = server.select_officer_model(policy.AUTOMATIC)
        self.assertEqual(status, 200)
        save.assert_called_once_with(policy.AUTOMATIC)
        self.assertEqual(view['selected_model'], policy.AUTOMATIC)
        self.assertEqual(view['selected_name'], 'Automatic by job')
        self.assertEqual(sum(row['id'] == policy.AUTOMATIC for row in view['models']), 1)
        self.assertNotIn(policy.AUTOMATIC, [row['id'] for row in server.OFFICER_CATALOG['models']])
        server.OFFICER_CATALOG['models'] = [row for row in models if row['id'] != 'claude-sonnet-5-5']
        result = self.ask(lambda *a, **k: self.fail('Missing known policy model must make no request'))
        self.assertIn('unavailable', result['error'])

    @unittest.skipIf(os.name == 'nt', 'Messaging persistence is POSIX only')
    def test_retry_reuses_persisted_job_and_action_tools_still_win(self):
        sessions = Path(self.tmp.name) / 'sessions'
        sessions.mkdir()
        store = MessagingStore(str(Path(self.tmp.name) / 'state'), str(sessions))
        server.MESSAGING = store
        request_id = str(uuid.uuid4())
        route = {}
        try:
            self.assertIsNone(server.officer_dispatch('Summarize the room.', request_id,
                lambda *a, **k: self.answer('{"officer_job":"summary"}'), model_snapshot=('claude-haiku-5-5', False), route_output=route))
            self.assertEqual(route, {'job': 'summary'})
            again = {}
            self.assertIsNone(server.officer_dispatch('Summarize the room.', request_id,
                lambda *a, **k: self.fail('Retry must not reclassify'), model_snapshot=('claude-haiku-5-5', False), route_output=again))
            self.assertEqual(again, route)
            self.assertEqual(store.claim_ask(request_id, 'Summarize the room.'), {'kind': 'none', 'officer_job': 'summary'})
        finally:
            store.close()
        calls = []
        def action(body, **kwargs):
            calls.append(body)
            return {'content': [{'type': 'text', 'text': '{"officer_job":"summary"}'},
                                {'type': 'tool_use', 'name': 'clarify_relay', 'input': {'text': 'Which agent?'}}]}, ''
        server.MESSAGING = None
        result = self.ask(action)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['text'], 'Which agent?')
        self.assertEqual(result['job'], 'intent')


if __name__ == '__main__': unittest.main()
