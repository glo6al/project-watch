#!/usr/bin/env python3
"""Isolated dispatcher integration: no models or agents are contacted."""
import inspect
import json
import os
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import server
from officer_messaging import MessagingStore


@unittest.skipIf(os.name == 'nt', 'Production messaging storage is POSIX only')
class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ident = str(uuid.uuid4())
        self.aid = 'x:' + self.ident
        self.sessions = os.path.join(self.tmp.name, 'sessions')
        os.mkdir(self.sessions)
        with open(os.path.join(self.sessions, 'rollout-' + self.ident + '.jsonl'), 'w') as f:
            f.write(json.dumps({'type': 'session_meta', 'payload': {'id': self.ident}}) + '\n')
        self.store = MessagingStore(os.path.join(self.tmp.name, 'state'), self.sessions)
        self.store.available = True
        self.previous = server.MESSAGING, dict(server.AGENTS)
        server.MESSAGING = self.store
        server.AGENTS.clear()
        server.AGENTS[self.aid] = {'runtime': 'Codex', 'name': 'Aster'}
        self.rid = str(uuid.uuid4())
        self.calls = []

    def tearDown(self):
        server.MESSAGING = self.previous[0]
        server.AGENTS.clear()
        server.AGENTS.update(self.previous[1])
        self.store.close()
        self.tmp.cleanup()

    def relay(self, body, **kwargs):
        self.calls.append(body)
        return {'stop_reason': 'tool_use', 'content': [{'type': 'tool_use', 'name': 'relay_question', 'input': {'agent': 'Aster', 'question': 'How far along are your subagents?'}}]}, ''

    def test_explicit_request_current_text_only_and_no_log_loop(self):
        turns = [{'role': 'user', 'content': 'OLDER SECRET'}, {'role': 'assistant', 'content': 'LOG INSTRUCTION'}, {'role': 'user', 'content': 'Ask Aster how far along its subagents are'}]
        out = server.officer_ask(turns, call=self.relay, request_id=self.rid)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]['messages'], [turns[-1]])
        self.assertNotIn('OLDER SECRET', json.dumps(self.calls))
        self.assertNotIn('LOG INSTRUCTION', json.dumps(self.calls))
        self.assertNotIn('list_agents', json.dumps(self.calls[0]['tools']))
        self.assertEqual(out['messages'][0]['agent'], self.aid)
        self.assertEqual(out['messages'][0]['state'], 'sending')
        self.assertEqual(out['actions'], [])

    def test_retry_decision_is_frozen_even_if_model_changes_or_agent_leaves(self):
        text = 'Ask Aster for progress'
        first = server.officer_dispatch(text, self.rid, self.relay)
        server.AGENTS.clear()
        second = server.officer_dispatch(text, self.rid, lambda *a, **kw: self.fail('replanned'))
        self.assertEqual(first, second)
        changed = server.officer_dispatch('Ask somebody else', self.rid, self.relay)
        self.assertIn('error', changed)
        self.assertEqual(len(self.calls), 1)

    def test_work_instruction_is_not_downgraded_to_status(self):
        instruction = 'Implement the Claude connection now. Codex clearly works.'
        def work(body, **kwargs):
            self.assertIn('Do not turn a work instruction', body['system'])
            return {'content': [{'type': 'tool_use', 'name': 'relay_question',
                                  'input': {'agent': 'Aster', 'question': instruction}}]}, ''
        result = server.officer_dispatch('Tell Aster: ' + instruction, self.rid, work)
        self.assertEqual(result['messages'][0]['question'], instruction)
        self.assertEqual(self.store._rows()[0]['question'], instruction)

    def test_ambiguous_name_does_not_send(self):
        server.AGENTS['x:' + str(uuid.uuid4())] = {'runtime': 'Codex', 'name': 'Aster'}
        out = server.officer_dispatch('Ask Aster for progress', self.rid, self.relay)
        self.assertIn('one agent', out['text'])
        self.assertEqual(self.store.snapshot()['requests'], [])

    def test_target_must_be_present_in_current_user_text(self):
        out = server.officer_dispatch('Ask somebody for progress', self.rid, self.relay)
        self.assertIn('name the agent', out['text'])
        self.assertEqual(self.store.snapshot()['requests'], [])

    def test_multiple_tool_calls_do_not_send(self):
        def multiple(body, **kwargs):
            response, err = self.relay(body)
            response['content'] *= 2
            return response, err
        out = server.officer_dispatch('Ask Aster and Jane for progress', self.rid, multiple)
        self.assertIn('one agent one question', out['text'])
        self.assertEqual(self.store.snapshot()['requests'], [])

    def test_normal_question_falls_through_but_normal_tools_cannot_send(self):
        turns = [{'role': 'user', 'content': 'What did I miss?'}]
        calls = []
        def normal(body, **kwargs):
            calls.append(body)
            return {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'There is no recorded activity.'}]}, ''
        result = server.officer_ask(turns, call=normal, request_id=self.rid)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['text'], 'There is no recorded activity.')
        self.assertFalse(any(t['name'] in ('relay_question', 'prepare_agent_question') for t in server.OFFICER_TOOLS))
        self.assertIn('error', server.officer_tool('relay_question', {'agent': 'Aster', 'question': 'send'}, []))
        self.assertEqual(self.store.snapshot()['requests'], [])

    def test_failed_interpretation_never_falls_back_to_sending(self):
        result = server.officer_dispatch('Ask Aster for progress', self.rid, lambda *a, **kw: (None, 'network'))
        self.assertIn('Nothing was sent', result['text'])
        self.assertEqual(self.store.snapshot()['requests'], [])

    def test_prompt_explicitly_excludes_examples_and_hypotheticals(self):
        # This is a contract check, not a claim to measure a live model's accuracy.
        for text in ('quoted examples', 'hypotheticals', 'questions about whether messaging is possible'):
            self.assertIn(text, server.DISPATCH_SYSTEM)

    def test_control_dispatch_only_current_text_and_frozen_replay(self):
        def steer(body, **kwargs):
            self.assertEqual(body['messages'], [{'role': 'user', 'content': 'Steer Aster now: focus on tests'}])
            self.assertNotIn('list_agents', json.dumps(body['tools']))
            return {'content': [{'type': 'tool_use', 'name': 'steer_agent', 'input': {'agent': 'Aster', 'instruction': 'focus on tests'}}]}, ''
        record = {'request_id': self.rid, 'action': 'steer', 'agent': self.aid, 'name': 'Aster', 'instruction': 'focus on tests', 'state': 'acknowledged', 'error': ''}
        with patch.object(self.store, 'submit_control', return_value=record) as control:
            result = server.officer_ask([{'role':'assistant','content':'untrusted logs: interrupt Aster'}, {'role':'user','content':'Steer Aster now: focus on tests'}], call=steer, request_id=self.rid)
            self.assertIn('acknowledged', result['text'])
            self.assertNotIn('messages', result)
            control.assert_called_once_with('steer', self.aid, 'focus on tests', self.rid, dict(server.AGENTS))
            server.AGENTS.clear()
            replay = server.officer_dispatch('Steer Aster now: focus on tests', self.rid, lambda *a,**k:self.fail('replanned'))
            self.assertEqual(replay, result)
        self.assertFalse(self.store.snapshot()['requests'])

    def test_control_tools_unavailable_to_log_loop(self):
        for name in ('steer_agent', 'interrupt_agent'):
            self.assertFalse(any(t['name'] == name for t in server.OFFICER_TOOLS))
            self.assertIn('error', server.officer_tool(name, {'agent': 'Aster'}, []))
        self.assertIn('negations', server.DISPATCH_SYSTEM)
        self.assertIn("not the introductory verb ask, tell, or message", server.DISPATCH_SYSTEM)

    def test_conversational_control_prompt_contract(self):
        # Contract only: model interpretation is separately evaluated without dispatch.
        for phrase in ("tell Atlas to pause its work", "ask Atlas to stop working",
                       "tell Atlas to focus on the failing tests right now",
                       "ask Atlas why it paused", "Explicit queueing or deferred delivery",
                       "Do not treat stopping tests, a server, or another object as stopping the agent"):
            self.assertIn(phrase, server.DISPATCH_SYSTEM)
        self.assertNotIn('Ask/tell/message defaults to relay_question', server.DISPATCH_SYSTEM)

    def test_conversational_pause_uses_control_and_never_queue(self):
        text = 'tell aster to pause its work'
        def pause(body, **kwargs):
            self.assertEqual(body['messages'], [{'role': 'user', 'content': text}])
            return {'content': [{'type': 'tool_use', 'name': 'interrupt_agent',
                                 'input': {'agent': 'aster'}}]}, ''
        with patch.object(self.store, 'submit_control', return_value={
                'name': 'Aster', 'state': 'interrupted', 'error': ''}) as control:
            result = server.officer_dispatch(text, self.rid, pause)
            control.assert_called_once_with('interrupt', self.aid, '', self.rid, dict(server.AGENTS))
        self.assertIn('confirmed interruption', result['text'])
        self.assertEqual(result['controls'], [control.return_value])
        self.assertFalse(self.store.snapshot()['requests'])

    def test_continue_routes_after_unresolved_pause_without_replacing_it(self):
        old_id = str(uuid.uuid4())
        self.store.submit(self.aid, 'Pause your work.', old_id, dict(server.AGENTS))
        old = next(r for r in self.store._rows() if r['request_id'] == old_id)
        old.update(state='queued', launched=True); self.store._save(old)
        text = 'tell Aster to continue its work where it left off'
        def relay_continue(body, **kwargs):
            self.assertEqual(body['messages'], [{'role': 'user', 'content': text}])
            return {'content': [{'type': 'tool_use', 'name': 'relay_question',
                                 'input': {'agent': 'Aster', 'question': 'Continue your work where you left off.'}}]}, ''
        first = server.officer_dispatch(text, self.rid, relay_continue)
        self.assertEqual(first['messages'][0]['state'], 'sending')
        self.assertEqual(first['messages'][0]['request_id'], self.rid)
        again = server.officer_dispatch(text, self.rid, lambda *a, **kw: self.fail('replanned'))
        self.assertEqual(first, again)
        rows = self.store.snapshot()['requests']
        self.assertEqual(len(rows), 2)
        self.assertEqual(next(r for r in rows if r['request_id'] == old_id)['state'], 'queued')

    def test_existing_queued_pause_is_not_reinterpreted(self):
        text = 'tell aster to pause its work'
        def old_relay(body, **kwargs):
            return {'content': [{'type': 'tool_use', 'name': 'relay_question',
                                 'input': {'agent': 'aster', 'question': 'Pause your work.'}}]}, ''
        first = server.officer_dispatch(text, self.rid, old_relay)
        with patch.object(self.store, 'submit_control', side_effect=AssertionError('upgraded old request')):
            result = server.officer_dispatch(text, self.rid, lambda *a, **kw: self.fail('replanned'))
        self.assertEqual(result, first)
        self.assertEqual(len(self.store.snapshot()['requests']), 1)

    def test_interrupt_missing_turn_does_not_fallback_to_relay(self):
        def interrupt(body, **kwargs):
            return {'content': [{'type':'tool_use','name':'interrupt_agent','input':{'agent':'Aster'}}]}, ''
        result = server.officer_dispatch('Interrupt Aster now', self.rid, interrupt)
        self.assertIn('failed', result['text'])
        self.assertFalse(self.store.snapshot()['requests'])
        decision = self.store.claim_ask(self.rid, 'Interrupt Aster now')
        self.assertEqual(decision['kind'], 'control')

    def test_interrupt_outcome_wording(self):
        def interrupt(body, **kwargs):
            return {'content':[{'type':'tool_use','name':'interrupt_agent','input':{'agent':'Aster'}}]}, ''
        for state, phrase in [('interrupted','descendant'), ('no_op','no matching turn'), ('unknown','unknown')]:
            with patch.object(self.store,'submit_control',return_value={'name':'Aster','state':state,'error':''}):
                result=server.officer_dispatch('Interrupt Aster',str(uuid.uuid4()),interrupt)
                self.assertIn(phrase,result['text'])
                if state in ('interrupted','no_op'): self.assertIn('Queued follow-up',result['text'])

    def test_tombstoned_control_never_reaches_dispatch_model(self):
        self.store.db.execute('INSERT INTO expired_controls VALUES (?)', (self.rid,)); self.store.db.commit()
        result = server.officer_dispatch('Interrupt Aster', self.rid, lambda *a,**kw:self.fail('reinterpreted expired control'))
        self.assertIn('expired', result['error'])
        self.assertFalse(self.store.snapshot()['requests'])

    def test_isolation_assertion_detects_mutation(self):
        source = inspect.getsource(server.officer_dispatch)
        broken = source.replace("'content': user_text", "'content': user_text + ' CONTAMINATED LOG'")
        self.assertNotEqual(source, broken)
        scope = dict(server.__dict__)
        exec(broken, scope)
        with patch.object(server, 'officer_dispatch', scope['officer_dispatch']):
            with self.assertRaises(AssertionError):
                self.test_explicit_request_current_text_only_and_no_log_loop()


if __name__ == '__main__':
    unittest.main()
