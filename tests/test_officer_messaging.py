#!/usr/bin/env python3
"""No live sends: mocked CLI, temporary transcripts and an ephemeral HTTP server."""
import concurrent.futures
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch, Mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import officer_messaging as m
import server


@unittest.skipIf(os.name == "nt", "POSIX-private Codex messaging storage; Windows startup leaves it unavailable")
class MessagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.sessions = os.path.join(self.temp.name, 'sessions')
        os.mkdir(self.sessions)
        self.ident = str(uuid.uuid4())
        self.agent = 'x:' + self.ident
        self.path = os.path.join(self.sessions, 'rollout-' + self.ident + '.jsonl')
        self.append('session_meta', {'id': self.ident, 'source': 'vscode', 'originator': 'Codex Desktop'})
        self.state = os.path.join(self.temp.name, 'state')
        self.store = m.MessagingStore(self.state, self.sessions)
        self.store.available, self.store.reason = True, ''
        self.agents = {self.agent: {'runtime': 'Codex', 'name': 'Aster'}}
        self.rid = str(uuid.uuid4())
        self.question = 'How far along are your subagents?'

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def append(self, typ, payload, partial=False):
        with open(self.path, 'ab') as f:
            f.write(json.dumps({'type': typ, 'payload': payload}).encode() + (b'' if partial else b'\n'))

    def submit(self):
        return self.store.submit(self.agent, self.question, self.rid, self.agents)

    def row(self):
        return self.store.snapshot()['requests'][0]

    def tick(self, returncode=0):
        proc = Mock()
        proc.wait.return_value = returncode
        with patch.object(m.subprocess, 'Popen', return_value=proc) as launch:
            self.store.tick()
        return launch

    def user(self, turn='t1', text=None):
        self.append('event_msg', {'type': 'task_started', 'turn_id': turn})
        self.append('response_item', {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text if text is not None else m.envelope(self.rid, self.question)}]})

    def answer(self, turn='t1', text=None, partial=False):
        self.append('event_msg', {'type': 'task_complete', 'turn_id': turn,
                                 'last_agent_message': text or 'WATCH_REPLY ' + self.rid + '\nTwo workers are testing.'}, partial)

    def test_ask_concurrent_claims_only_one_planner(self):
        def claim(_):
            try: return self.store.claim_ask(self.rid, self.question)
            except m.MessagingError as error: return error.status
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            results = list(pool.map(claim, range(16)))
        self.assertEqual(results.count(None), 1)
        self.assertEqual(results.count(409), 15)
        with self.assertRaises(m.MessagingError) as error:
            self.store.claim_ask(self.rid, 'Different text')
        self.assertEqual(error.exception.status, 409)

    def test_work_authorization_and_status_scope_preserved(self):
        self.question = 'Implement the Claude connection now. Codex clearly works.'
        self.submit()
        launch = self.tick()
        sent = launch.call_args.args[0][-1]
        self.assertIn(self.question, sent)
        self.assertIn('an explicit work instruction authorizes that work under your existing permissions', sent)
        self.assertIn('a status question asks only for information', sent)
        self.assertNotIn('not permission to change files', sent)
        self.store.close()
        self.store = m.MessagingStore(self.state, self.sessions)
        with patch.object(m, 'envelope', return_value='future wrapper must not change this request'):
            self.user(text=sent)
            self.answer()
            self.store.tick()
        self.assertEqual(self.row()['state'], 'answered')

    def test_legacy_pending_envelope_survives_upgrade(self):
        self.submit()
        with self.store.lock:
            row = self.store._rows()[0]
            row.pop('envelope')
            row.update(state='queued', launched=True)
            self.store._save(row)
        old_text = ('WATCH_REQUEST ' + self.rid + '\nThe owner asks through the Officer of the Watch:\n' + self.question +
                    '\nReply in your final answer with WATCH_REPLY ' + self.rid + ' on its own line, followed by your answer. '
                    'This is a question, not permission to change files or send messages. For a progress question, inspect your own current state.')
        self.store.close()
        self.store = m.MessagingStore(self.state, self.sessions)
        self.user(text=old_text)
        self.answer()
        with patch.object(m.subprocess, 'Popen') as launch:
            self.store.tick()
        launch.assert_not_called()
        self.assertEqual(self.row()['state'], 'answered')

    def test_ask_decision_persisted_before_submission_and_reopen(self):
        self.assertIsNone(self.store.claim_ask(self.rid, self.question))
        decision = {'kind': 'relay', 'agent': self.agent, 'name': 'Aster', 'question': self.question}
        self.assertEqual(self.store.decide_ask(self.rid, decision), decision)
        self.assertFalse(self.store.snapshot()['requests'])
        self.store.close()
        self.store = m.MessagingStore(self.state, self.sessions)
        self.assertEqual(self.store.claim_ask(self.rid, self.question), decision)
        self.assertEqual(self.store.decide_ask(self.rid, decision), decision)
        with self.assertRaises(m.MessagingError):
            self.store.decide_ask(self.rid, {'kind': 'none'})
        self.store.available = True
        self.submit()
        self.assertEqual(self.store.claim_ask(self.rid, self.question), decision)
        self.assertEqual(self.store.submit(self.agent, self.question, self.rid, {})['request_id'], self.rid)

    def test_ask_planning_crash_does_not_replan(self):
        self.store.claim_ask(self.rid, self.question)
        self.store.close()
        self.store = m.MessagingStore(self.state, self.sessions)
        with self.assertRaises(m.MessagingError) as error:
            self.store.claim_ask(self.rid, self.question)
        self.assertEqual(error.exception.status, 409)
        self.assertIn('already started', str(error.exception))

    def test_ask_validation_rate_retention_and_capacity(self):
        for text in ('', ' ', 'a' * 4001, '\x00', None):
            with self.assertRaises(m.MessagingError): self.store.claim_ask(self.rid, text)
        with self.assertRaises(m.MessagingError): self.store.claim_ask('bad', self.question)
        with self.assertRaises(m.MessagingError): self.store.decide_ask(self.rid, {'kind': 'none'})
        for _ in range(20): self.store.claim_ask(str(uuid.uuid4()), self.question)
        with self.assertRaises(m.MessagingError) as error: self.store.claim_ask(self.rid, self.question)
        self.assertEqual(error.exception.status, 429)
        self.store.db.execute('UPDATE asks SET created=?', (time.time() - m.RETENTION - 1,)); self.store.db.commit()
        self.assertIsNone(self.store.claim_ask(self.rid, self.question))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM asks').fetchone()[0], 1)
        with patch.object(m, 'MAX_RECORDS', 1):
            with self.assertRaises(m.MessagingError): self.store.claim_ask(str(uuid.uuid4()), self.question)

    def test_lifetime_lock_prevents_second_store_and_releases_on_close(self):
        with self.assertRaises(OSError): m.MessagingStore(self.state, self.sessions)
        self.store.close()
        self.store = m.MessagingStore(self.state, self.sessions)
        self.assertIsNone(self.store.claim_ask(self.rid, self.question))

    def test_import_quiet_and_private_state(self):
        self.assertIsNone(server.MESSAGING)
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(os.path.join(self.state, 'officer-messages.sqlite3')).st_mode & 0o777, 0o600)

    def test_queue_acceptance_is_not_answer(self):
        self.assertEqual(self.submit()['state'], 'sending')
        call = self.tick().call_args
        self.assertEqual(call.args[0], [m.CLI, 'queue', '--thread', self.ident, '--message', m.envelope(self.rid, self.question)])
        self.assertEqual(self.row()['state'], 'queued')
        self.user()
        self.store.tick()
        self.assertEqual(self.row()['state'], 'awaiting_reply')
        self.answer()
        self.store.tick()
        self.assertEqual(self.row()['reply'], 'Two workers are testing.')
        self.assertEqual(self.row()['state'], 'answered')

    def test_exact_envelope_and_same_turn_required(self):
        self.submit(); self.tick()
        self.user('unrelated', 'Someone quoted WATCH_REQUEST ' + self.rid)
        self.answer('unrelated')
        self.store.tick()
        self.assertEqual(self.row()['state'], 'queued')
        self.user('actual')
        self.answer('other')
        self.append('response_item', {'type': 'function_call_output', 'output': 'WATCH_REPLY ' + self.rid})
        self.store.tick()
        self.assertEqual(self.row()['state'], 'awaiting_reply')
        self.answer('actual')
        self.store.tick()
        self.assertEqual(self.row()['state'], 'answered')

    def test_nested_turn_metadata_overrides_current_turn(self):
        self.submit(); self.tick()
        self.append('event_msg', {'type': 'task_started', 'turn_id': 'new'})
        self.append('response_item', {'type': 'message', 'role': 'user',
            'internal_chat_message_metadata_passthrough': {'turn_id': 'old'},
            'content': [{'text': m.envelope(self.rid, self.question)}]})
        self.answer('new')
        self.store.tick()
        self.assertEqual(self.row()['state'], 'awaiting_reply')
        self.assertEqual(self.store._rows()[0]['bound_turn'], 'old')
        self.append('response_item', {'type': 'message', 'role': 'assistant', 'phase': 'final_answer',
            'internal_chat_message_metadata_passthrough': {'turn_id': 'other'},
            'content': [{'text': 'WATCH_REPLY ' + self.rid + '\nWrong turn'}]})
        self.append('event_msg', {'type': 'task_complete', 'turn_id': 'old'})
        self.store.tick()
        self.assertEqual(self.row()['state'], 'awaiting_reply')
        self.answer('old')
        self.store.tick()
        self.assertEqual(self.row()['state'], 'answered')

    def test_conflicting_or_malformed_turn_metadata_never_binds(self):
        self.submit(); self.tick()
        self.append('event_msg', {'type': 'task_started', 'turn_id': 'new'})
        for nested in ({'turn_id': 'old'}, {'turn_id': []}, 'old'):
            self.append('response_item', {'type': 'message', 'role': 'user', 'turn_id': 'new',
                'internal_chat_message_metadata_passthrough': nested,
                'content': [{'text': m.envelope(self.rid, self.question)}]})
        self.answer('new'); self.store.tick()
        self.assertEqual(self.row()['state'], 'queued')

    def test_rebinding_cannot_reuse_previous_turn_final(self):
        self.submit(); self.tick(); self.user('old')
        self.append('response_item', {'type': 'message', 'role': 'assistant', 'phase': 'final_answer',
            'internal_chat_message_metadata_passthrough': {'turn_id': 'old'},
            'content': [{'text': 'WATCH_REPLY ' + self.rid + '\nStale answer'}]})
        self.store.tick()
        self.assertTrue(self.store._rows()[0]['final'])
        self.user('new')
        self.append('event_msg', {'type': 'task_complete', 'turn_id': 'new'})
        self.store.tick()
        self.assertEqual(self.row()['state'], 'awaiting_reply')
        self.assertEqual(self.store._rows()[0]['final'], '')
        self.answer('new'); self.store.tick()
        self.assertEqual(self.row()['state'], 'answered')

    def test_final_only_after_completed_matching_turn(self):
        self.submit(); self.tick(); self.user()
        self.append('response_item', {'type': 'message', 'role': 'assistant', 'phase': 'final_answer', 'content': [{'text': 'WATCH_REPLY ' + self.rid + '\nDone'}]})
        self.store.tick()
        self.assertEqual(self.row()['state'], 'awaiting_reply')
        self.append('event_msg', {'type': 'task_complete', 'turn_id': 't1'})
        self.store.tick()
        self.assertEqual(self.row()['reply'], 'Done')

    def test_partial_records_and_restart_keep_cursor(self):
        self.submit(); self.tick(); self.user(); self.answer(partial=True)
        self.store.tick()
        self.assertEqual(self.row()['state'], 'awaiting_reply')
        self.store.close()
        self.store = m.MessagingStore(self.state, self.sessions)
        with open(self.path, 'ab') as f:
            f.write(b'\n')
        self.store.tick()
        self.assertEqual(self.row()['state'], 'answered')

    def test_preexisting_records_are_not_replies(self):
        self.user(); self.answer(); self.submit(); self.tick()
        self.assertEqual(self.row()['state'], 'queued')

    def test_idempotency_concurrent_and_target_departure(self):
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda _: self.submit(), range(16)))
        self.assertEqual(len(self.store.snapshot()['requests']), 1)
        self.assertTrue(all(r['request_id'] == self.rid for r in results))
        self.assertEqual(self.store.submit(self.agent, self.question, self.rid, {})['request_id'], self.rid)
        with self.assertRaises(m.MessagingError) as e:
            self.store.submit(self.agent, 'different', self.rid, {})
        self.assertEqual(e.exception.status, 409)
        self.assertEqual(self.tick().call_count, 1)
        self.assertEqual(self.tick().call_count, 0)

    def test_timeout_nonzero_unknown_never_retry(self):
        self.submit()
        proc = Mock()
        proc.wait.side_effect = [subprocess.TimeoutExpired('codex', 10), -9]
        with patch.object(m.subprocess, 'Popen', return_value=proc) as launch:
            self.store.tick(); self.store.tick()
            self.assertEqual(launch.call_count, 1)
        self.assertEqual(self.row()['state'], 'unknown')
        self.user(); self.answer(); self.store.tick()
        self.assertEqual(self.row()['state'], 'answered')
        self.rid = str(uuid.uuid4())
        self.submit(); self.tick(1)
        self.assertEqual(self.store.snapshot()['requests'][-1]['state'], 'unknown')

    def test_postlaunch_wait_error_is_unknown(self):
        self.submit()
        proc = Mock()
        proc.wait.side_effect = OSError('process status unavailable')
        with patch.object(m.subprocess, 'Popen', return_value=proc) as launch:
            self.store.tick(); self.store.tick()
            self.assertEqual(launch.call_count, 1)
        self.assertEqual(self.row()['state'], 'unknown')

    def test_restart_sending_unknown(self):
        self.submit()
        self.store.close()
        self.store = m.MessagingStore(self.state, self.sessions)
        self.assertEqual(self.row()['state'], 'unknown')
        self.assertEqual(self.tick().call_count, 0)

    def test_identity_exec_guardian_and_ambiguity(self):
        for payload in ({'id': str(uuid.uuid4())}, {'id': self.ident, 'source': 'exec'},
                        {'id': self.ident, 'originator': 'codex_exec'}, {'id': self.ident, 'thread_source': 'guardian_review'}):
            open(self.path, 'w').close()
            self.append('session_meta', payload)
            with self.assertRaises(m.MessagingError):
                self.submit()
        open(self.path, 'w').close()
        self.append('session_meta', {'id': self.ident})
        other = os.path.join(self.sessions, 'other-' + self.ident + '.jsonl')
        with open(other, 'w') as f: f.write('{}\n')
        with self.assertRaises(m.MessagingError): self.submit()
        self.assertFalse(self.store.snapshot()['requests'])

    def test_changed_identity_before_launch_fails(self):
        self.submit()
        open(self.path, 'w').close()
        self.append('session_meta', {'id': str(uuid.uuid4())})
        self.assertEqual(self.tick().call_count, 0)
        self.assertEqual(self.row()['state'], 'failed')

    def test_expiry_late_reply_and_target_limit(self):
        self.submit(); self.tick()
        with self.store.lock:
            row = self.store._rows()[0]
            row['created'] -= m.TTL + 1
            self.store._save(row)
        self.store.tick()
        self.assertEqual(self.row()['state'], 'expired')
        with self.assertRaises(m.MessagingError):
            self.store.submit(self.agent, 'Again?', str(uuid.uuid4()), self.agents)
        self.user(); self.answer(); self.store.tick()
        self.assertEqual(self.row()['state'], 'answered')

    def test_reject_invalid_target_question_uuid(self):
        for target in ('c:' + self.ident, 'x:bad', 'e:' + self.ident):
            with self.assertRaises(m.MessagingError): self.store.submit(target, self.question, self.rid, self.agents)
        for question in ('', ' ', 'a' * 2001, '\x00', None):
            with self.assertRaises(m.MessagingError): self.store.submit(self.agent, question, self.rid, self.agents)
        with self.assertRaises(m.MessagingError): self.store.submit(self.agent, self.question, 'bad', self.agents)

    def test_capacity_rate_and_retention_bounds(self):
        with patch.object(m, 'MAX_PENDING', 0):
            with self.assertRaises(m.MessagingError) as error: self.submit()
            self.assertEqual(error.exception.status, 429)
        with patch.object(m, 'MAX_RECORDS', 0):
            with self.assertRaises(m.MessagingError): self.submit()
        self.submit(); self.tick(); self.user(); self.answer(); self.store.tick()
        with self.store.lock:
            original = self.store._rows()[0]
            for _ in range(9):
                copy = dict(original, request_id=str(uuid.uuid4()))
                self.store._save(copy)
        self.rid = str(uuid.uuid4())
        with self.assertRaises(m.MessagingError) as error: self.submit()
        self.assertEqual(error.exception.status, 429)
        with self.store.lock:
            for row in self.store._rows():
                row['created'] -= m.RETENTION + 1
                self.store._save(row)
        self.assertEqual(self.submit()['state'], 'sending')
        self.assertEqual(len(self.store.snapshot()['requests']), 1)

    def test_capability_probe_is_bounded_and_platform_specific(self):
        with patch.object(m.sys, 'platform', 'win32'), patch.object(m.subprocess, 'run') as run:
            self.assertFalse(self.store.probe()[0])
            run.assert_not_called()
        with patch.object(m.sys, 'platform', 'darwin'), patch.object(m.subprocess, 'run') as run:
            run.side_effect = [Mock(returncode=0, stdout='codex-cli 0.163.0-alpha.11.2'), Mock(returncode=0, stdout='--thread --message')]
            self.assertTrue(self.store.probe()[0])
            self.assertTrue(all(c.kwargs['timeout'] == 5 for c in run.call_args_list))
            run.side_effect = [Mock(returncode=0, stdout='codex-cli 0.163.0'), Mock(returncode=0, stdout='unrelated')]
            self.assertFalse(self.store.probe()[0])

    def test_oversized_partial_record_stops_bounded_tracking(self):
        self.submit(); self.tick()
        with open(self.path, 'ab') as f: f.write(b'x' * (m.MAX_LINE + 1))
        self.store.tick()
        self.assertEqual(self.row()['state'], 'unknown')
        self.assertTrue(self.store._rows()[0]['tracking_stopped'])

    def test_http_auth_host_origin_and_idempotency(self):
        old_key, old_store, old_port = server.KEY, server.MESSAGING, server.PORT
        old_agents = server.AGENTS.copy()
        server.KEY, server.MESSAGING = 'test-key', self.store
        server.AGENTS.clear(); server.AGENTS.update(self.agents)
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.H)
        server.PORT = httpd.server_port
        thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
        def request(method, path, headers=None, body=None):
            conn = http.client.HTTPConnection('127.0.0.1', server.PORT, timeout=3)
            conn.request(method, path, body=json.dumps(body or {}) if method == 'POST' else None, headers=headers or {})
            res = conn.getresponse(); data = res.read(); conn.close()
            return res.status, data
        try:
            self.assertEqual(request('GET', '/agent-messages')[0], 403)
            self.assertEqual(request('GET', '/agent-messages?k=test-key')[0], 403)
            auth = {'X-Live-Room-Key': 'test-key'}
            self.assertEqual(request('GET', '/agent-messages', dict(auth, Origin='https://evil.test'))[0], 403)
            self.assertEqual(request('GET', '/agent-messages', dict(auth, Host='evil.test'))[0], 403)
            payload = {'agent': self.agent, 'question': self.question, 'request_id': self.rid}
            self.assertEqual(request('POST', '/agent-message', body=payload)[0], 403)
            self.assertEqual(request('POST', '/agent-message', auth, payload)[0], 200)
            self.assertEqual(request('POST', '/agent-message', auth, payload)[0], 200)
            self.assertEqual(request('POST', '/agent-message', auth, dict(payload, question='changed'))[0], 409)
            status, body = request('GET', '/agent-messages', auth)
            self.assertEqual(status, 200)
            self.assertEqual(len(json.loads(body)['requests']), 1)
        finally:
            httpd.shutdown(); httpd.server_close(); thread.join()
            server.KEY, server.MESSAGING, server.PORT = old_key, old_store, old_port
            server.AGENTS.clear(); server.AGENTS.update(old_agents)


if __name__ == '__main__':
    unittest.main()
