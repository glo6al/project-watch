#!/usr/bin/env python3
"""Durable controls using synthetic transcripts and fake bridges only."""
import concurrent.futures
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import Mock, patch
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import officer_messaging as m


@unittest.skipIf(os.name == 'nt', 'POSIX store')
class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sessions = os.path.join(self.tmp.name, 'sessions'); os.mkdir(self.sessions)
        self.sid, self.turn, self.rid = [str(uuid.uuid4()) for _ in range(3)]
        self.aid = 'x:' + self.sid
        self.path = os.path.join(self.sessions, 'rollout-' + self.sid + '.jsonl')
        self.append('session_meta', {'id': self.sid, 'cwd': self.tmp.name})
        self.append('event_msg', {'type': 'task_started', 'turn_id': self.turn})
        self.agents = {self.aid: {'runtime': 'Codex', 'name': 'Aster'}}
        self.store = m.MessagingStore(os.path.join(self.tmp.name, 'state'), self.sessions)
        self.store.available = True
        self.bridge = Mock(return_value={'state': 'acknowledged', 'error': ''})
    def tearDown(self): self.store.close(); self.tmp.cleanup()
    def append(self, typ, payload):
        with open(self.path, 'a') as f: f.write(json.dumps({'type': typ, 'payload': payload}) + '\n')
    def send(self, action='steer', rid=None, agents=None):
        return self.store.submit_control(action, self.aid, 'Focus on tests' if action == 'steer' else '', rid or self.rid,
                                         self.agents if agents is None else agents, execute=self.bridge)
    def test_pin_and_private_result(self):
        result = self.send()
        self.assertEqual(result['state'], 'acknowledged')
        self.bridge.assert_called_once_with('steer', self.sid, instruction='Focus on tests', expected_turn_id=self.turn, cwd=self.tmp.name)
        self.assertNotIn('cwd', result); self.assertNotIn('expected_turn_id', result)
        row = json.loads(self.store.db.execute('SELECT data FROM controls').fetchone()[0])
        self.assertEqual(row['expected_turn_id'], self.turn)
    def test_idempotent_replay_absent_agent_and_changed_payload(self):
        first = self.send(); os.unlink(self.path)
        self.assertEqual(first, self.send(agents={}))
        self.bridge.assert_called_once()
        with self.assertRaises(m.MessagingError): self.send('interrupt')
    def test_concurrent_same_id_sends_once(self):
        entered, release = threading.Event(), threading.Event()
        def execute(*args, **kwargs): entered.set(); release.wait(2); return {'state': 'acknowledged'}
        self.bridge.side_effect = execute
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            future = pool.submit(self.send); self.assertTrue(entered.wait(1))
            self.assertEqual(self.send()['state'], 'sending')
            with self.assertRaises(m.MessagingError): self.send('interrupt', str(uuid.uuid4()))
            release.set(); self.assertEqual(future.result()['state'], 'acknowledged')
        self.bridge.assert_called_once()
    def test_unknown_steer_does_not_block_new_interrupt(self):
        self.bridge.return_value = {'state': 'unknown'}
        self.assertEqual(self.send()['state'], 'unknown')
        self.bridge.return_value = {'state': 'interrupted'}
        other = str(uuid.uuid4())
        self.assertEqual(self.send('interrupt', other)['state'], 'interrupted')
        self.assertEqual(self.send()['state'], 'unknown')
        self.assertEqual(self.send('interrupt', other)['state'], 'interrupted')
        self.assertEqual(self.bridge.call_count, 2)
    def test_pending_question_is_not_cleared_or_blocking(self):
        message = self.store.submit(self.aid, 'Progress?', str(uuid.uuid4()), self.agents)
        self.bridge.return_value = {'state': 'interrupted'}
        self.assertEqual(self.send('interrupt')['state'], 'interrupted')
        self.assertEqual(self.store.snapshot()['requests'], [message])
    def test_restart_sending_becomes_unknown_without_dispatch(self):
        self.send()
        row = json.loads(self.store.db.execute('SELECT data FROM controls').fetchone()[0]); row['state'] = 'sending'
        self.store.db.execute('UPDATE controls SET data=?', (json.dumps(row),)); self.store.db.commit(); self.store.close()
        self.store = m.MessagingStore(os.path.join(self.tmp.name, 'state'), self.sessions)
        self.assertEqual(self.send()['state'], 'unknown'); self.bridge.assert_called_once()
    def test_unexpected_bridge_exception_unknown_never_retry(self):
        self.bridge.side_effect = RuntimeError('private details')
        result = self.send(); self.assertEqual(result['state'], 'unknown')
        self.assertNotIn('private details', result['error']); self.send(); self.bridge.assert_called_once()
    def test_terminal_or_partial_or_stale_never_dispatches(self):
        with open(self.path, 'rb') as f: original = f.read()
        for kind in ('task_complete', 'turn_aborted', 'task_aborted'):
            with self.subTest(kind=kind):
                self.rid = str(uuid.uuid4())
                with open(self.path, 'wb') as f: f.write(original)
                self.append('event_msg', {'type': kind, 'turn_id': self.turn})
                self.assertEqual(self.send()['state'], 'failed')
        self.bridge.assert_not_called()
    def test_missing_terminal_identity_fails_closed(self):
        self.append('event_msg', {'type': 'turn_aborted'})
        self.append('turn_context', {'turn_id': self.turn})
        self.assertEqual(self.send()['state'], 'failed')
        self.bridge.assert_not_called()
    def test_partial_line_and_stale_file(self):
        with open(self.path, 'ab') as f: f.write(b'{')
        self.assertEqual(self.send()['state'], 'failed')
        with open(self.path, 'rb+') as f: f.truncate(os.path.getsize(self.path)-1)
        self.rid = str(uuid.uuid4())
        os.utime(self.path, (time.time()-301, time.time()-301))
        self.assertEqual(self.send()['state'], 'failed')
        self.bridge.assert_not_called()
    def test_stale_active_turn_interrupt_is_pinned_but_steer_refused(self):
        os.utime(self.path, (time.time()-3600, time.time()-3600))
        self.assertEqual(self.send()['state'], 'failed')
        self.bridge.assert_not_called()
        self.bridge.return_value = {'state': 'interrupted'}
        result = self.send('interrupt', str(uuid.uuid4()))
        self.assertEqual(result['state'], 'interrupted')
        self.bridge.assert_called_once_with('interrupt', self.sid, instruction='', expected_turn_id=self.turn, cwd=self.tmp.name)

    def test_ambiguous_turn_and_unsupported_runtime(self):
        self.append('event_msg', {'type': 'task_started', 'turn_id': str(uuid.uuid4())})
        self.assertEqual(self.send()['state'], 'failed')
        self.rid = str(uuid.uuid4())
        self.agents[self.aid]['runtime'] = 'Codex cloud'
        self.assertEqual(self.send()['state'], 'failed')
        self.bridge.assert_not_called()
    def test_missing_cwd_and_subagent_rejected(self):
        self.agents[self.aid]['parent'] = 'x:' + str(uuid.uuid4())
        self.assertEqual(self.send()['state'], 'failed')
        self.rid = str(uuid.uuid4())
        del self.agents[self.aid]['parent']
        with open(self.path, 'w') as f: f.write(json.dumps({'type':'session_meta','payload':{'id':self.sid}})+'\n')
        self.append('event_msg', {'type':'task_started','turn_id':self.turn})
        self.assertEqual(self.send()['state'], 'failed')
        self.bridge.assert_not_called()
    def test_file_changes_after_capture_prevent_dispatch(self):
        original = self.store._control_target
        def changed(*args):
            result = original(*args)
            self.append('event_msg', {'type':'task_complete','turn_id':self.turn})
            return result
        with patch.object(self.store, '_control_target', changed): result = self.send()
        self.assertEqual(result['state'], 'failed'); self.bridge.assert_not_called()
    def test_interrupt_outcome_must_not_be_steer_acknowledgment(self):
        self.assertEqual(self.send('interrupt')['state'], 'unknown')
    def test_failed_preflight_replay_never_resolves_new_turn(self):
        self.append('event_msg', {'type': 'task_complete', 'turn_id': self.turn})
        failure = self.send()
        self.assertEqual(failure['state'], 'failed')
        self.append('event_msg', {'type': 'task_started', 'turn_id': str(uuid.uuid4())})
        self.assertEqual(self.send(), failure)
        self.bridge.assert_not_called()

    def test_control_message_ids_cannot_collide(self):
        self.send()
        with self.assertRaises(m.MessagingError): self.store.submit(self.aid, 'Progress?', self.rid, self.agents)
        other = str(uuid.uuid4())
        self.store.submit(self.aid, 'Progress?', other, self.agents)
        with self.assertRaises(m.MessagingError): self.send(rid=other)
        self.bridge.assert_called_once()

    def test_controls_use_existing_authenticated_ask_route(self):
        import server
        handler = server.H.__new__(server.H)
        handler.path = '/ask'
        handler.headers = {'Host': '127.0.0.1:' + str(server.PORT)}
        handler.body = lambda: {'request_id': self.rid, 'turns': [{'role':'user','content':'Interrupt Aster'}]}
        handler.send = Mock()
        with patch.object(server, 'KEY', 'test-control-key'), patch.object(server, 'OFFICER_KEY', 'configured'), patch.object(server, 'officer_ask', return_value={'text':'no live call'}) as ask:
            handler.do_POST()
            self.assertEqual(handler.send.call_args.args[0], 403)
            ask.assert_not_called()
            handler.headers['X-Live-Room-Key'] = 'test-control-key'
            handler.headers['Origin'] = 'https://untrusted.example'
            handler.do_POST()
            self.assertEqual(handler.send.call_args.args[0], 403)
            ask.assert_not_called()

    def test_expired_control_tombstone_prevents_all_replay_paths(self):
        self.store.claim_ask(self.rid, 'Steer Aster now')
        self.store.decide_ask(self.rid, {'kind':'control','action':'steer','agent':self.aid,'instruction':'Focus on tests'})
        self.send()
        old = json.loads(self.store.db.execute('SELECT data FROM controls WHERE id=?', (self.rid,)).fetchone()[0])
        old['created'] -= m.RETENTION + 1
        self.store.db.execute('UPDATE controls SET data=? WHERE id=?', (json.dumps(old), self.rid)); self.store.db.commit()
        self.append('event_msg', {'type':'task_complete','turn_id':self.turn})
        self.append('event_msg', {'type':'task_started','turn_id':str(uuid.uuid4())})
        self.send(rid=str(uuid.uuid4()))
        self.assertEqual(self.bridge.call_count, 2)
        self.assertIsNone(self.store.db.execute('SELECT data FROM controls WHERE id=?', (self.rid,)).fetchone())
        self.assertIsNone(self.store.db.execute('SELECT decision FROM asks WHERE id=?', (self.rid,)).fetchone())
        self.assertEqual(self.store.db.execute('SELECT * FROM expired_controls WHERE id=?', (self.rid,)).fetchone(), (self.rid,))
        self.store.close()
        self.store = m.MessagingStore(os.path.join(self.tmp.name, 'state'), self.sessions)
        for replay in (lambda:self.send(), lambda:self.send('interrupt'),
                       lambda:self.store.claim_ask(self.rid, 'Steer Aster now'),
                       lambda:self.store.claim_ask(self.rid, 'Tell Aster something else'),
                       lambda:self.store.submit(self.aid, 'Progress?', self.rid, self.agents)):
            with self.assertRaises(m.MessagingError) as error: replay()
            self.assertEqual(error.exception.status, 409)
            self.assertIn('expired', str(error.exception))
        self.assertEqual(self.bridge.call_count, 2)
        self.assertFalse(self.store.snapshot()['requests'])

    def test_lost_response_recovers_from_snapshot_and_restart_without_dispatch(self):
        # The HTTP caller may lose the response after this fake bridge succeeds.
        self.bridge.return_value = {'state': 'interrupted'}
        self.send('interrupt')
        rows = self.store.snapshot()['controls']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['state'], 'interrupted')
        self.assertEqual(rows[0]['request_id'], self.rid)
        self.assertEqual(set(rows[0]), {'request_id', 'action', 'agent', 'name', 'instruction', 'state', 'error', 'created'})
        self.assertIsInstance(rows[0]['created'], (float, int))
        self.assertNotIn(self.tmp.name, json.dumps(rows))
        self.assertNotIn(self.turn, json.dumps(rows))
        self.store.snapshot()
        self.store.close()
        self.store = m.MessagingStore(os.path.join(self.tmp.name, 'state'), self.sessions)
        self.assertEqual(self.store.snapshot()['controls'], rows)
        self.assertEqual(self.send('interrupt'), rows[0])
        self.bridge.assert_called_once()

    def test_snapshot_control_retention_is_read_only_and_keeps_sending(self):
        self.send()
        row = json.loads(self.store.db.execute('SELECT data FROM controls').fetchone()[0])
        row['created'] = time.time() - m.RETENTION - 10
        self.store.db.execute('UPDATE controls SET data=? WHERE id=?', (json.dumps(row), self.rid))
        sending_id = str(uuid.uuid4())
        sending = dict(row, request_id=sending_id, state='sending')
        self.store.db.execute('INSERT INTO controls VALUES (?,?)', (sending_id, json.dumps(sending)))
        self.store.db.commit()
        rows = self.store.snapshot()['controls']
        self.assertEqual([r['request_id'] for r in rows], [sending_id])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM controls').fetchone()[0], 2)
        self.assertFalse(self.store.db.execute('SELECT * FROM expired_controls').fetchall())
        self.bridge.assert_called_once()
        # Recovery changes sending to unknown once, and retains fresh unknowns.
        sending['created'] = time.time()
        self.store.db.execute('UPDATE controls SET data=? WHERE id=?', (json.dumps(sending), sending_id))
        self.store.db.commit(); self.store.close()
        self.store = m.MessagingStore(os.path.join(self.tmp.name, 'state'), self.sessions)
        self.assertEqual(self.store.snapshot()['controls'][0]['state'], 'unknown')
        self.bridge.assert_called_once()

    def test_rate_storage_and_retention(self):
        self.send()
        with patch.object(m, 'MAX_RECORDS', 1):
            with self.assertRaises(m.MessagingError): self.send(rid=str(uuid.uuid4()))
        self.bridge.assert_called_once()


if __name__ == '__main__': unittest.main()
