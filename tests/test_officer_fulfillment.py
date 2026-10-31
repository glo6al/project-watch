#!/usr/bin/env python3
"""Historical pause fulfillment: synthetic transcripts and fake bridges only."""
import concurrent.futures
from datetime import datetime, timezone
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


def ident():
    return str(uuid.uuid4())


@unittest.skipIf(os.name == 'nt', 'POSIX store')
class FulfillmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sessions = os.path.join(self.tmp.name, 'sessions')
        os.mkdir(self.sessions)
        self.state = os.path.join(self.tmp.name, 'state')
        self.sid, self.turn, self.rid = ident(), ident(), ident()
        self.aid = 'x:' + self.sid
        self.path = os.path.join(self.sessions, 'rollout-' + self.sid + '.jsonl')
        self.base = time.time() - 100
        self.append('session_meta', {'id': self.sid, 'cwd': self.tmp.name})
        self.append('event_msg', {'type': 'task_started', 'turn_id': self.turn})
        self.agents = {self.aid: {'runtime': 'Codex', 'name': 'Atlas'}}
        self.store = m.MessagingStore(self.state, self.sessions)
        self.store.available = True
        self.bridge = Mock(return_value={'state': 'interrupted'})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def append(self, typ, payload, stamp=None):
        event = {'type': typ, 'payload': payload}
        if stamp is not None:
            event['timestamp'] = stamp
        with open(self.path, 'a') as f:
            f.write(json.dumps(event) + '\n')

    def source(self, owner='tell Atlas to pause its work', question='Pause your work.', provenance=True):
        if provenance:
            with patch.object(m.time, 'time', return_value=self.base):
                self.store.claim_ask(self.rid, owner)
            self.store.decide_ask(self.rid, {'kind': 'relay', 'agent': self.aid, 'question': question})
        with patch.object(m.time, 'time', return_value=self.base + 1):
            self.store.submit(self.aid, question, self.rid, self.agents)
        row = self.store._rows()[0]
        row.update(state='queued', launched=True)
        self.store._save(row)
        return row

    def interrupt(self, state='interrupted', created=10, completed=20):
        self.bridge.return_value = {'state': state}
        with patch.object(m.time, 'time', side_effect=[self.base + created, self.base + completed]):
            return self.store.submit_control('interrupt', self.aid, '', ident(), self.agents, execute=self.bridge)

    def row(self):
        return next(r for r in self.store.snapshot()['requests'] if r['request_id'] == self.rid)

    def reconcile(self):
        with self.store.lock, self.store.db:
            self.store._reconcile_fulfillments()

    def reopen(self):
        self.store.close()
        self.store = m.MessagingStore(self.state, self.sessions)
        self.store.available = True

    def rewrite_control(self, cid, **changes):
        row = json.loads(self.store.db.execute('SELECT data FROM controls WHERE id=?', (cid,)).fetchone()[0])
        row.update(changes)
        self.store.db.execute('UPDATE controls SET data=? WHERE id=?', (json.dumps(row), cid))
        self.store.db.commit()
        return row

    def test_success_is_separate_private_and_replay_consistent(self):
        original = self.source()
        result = self.interrupt()
        current = self.row()
        self.assertEqual(current['state'], 'queued')
        self.assertEqual(current['fulfillment'], {'state': 'fulfilled', 'intent': 'pause', 'via': 'interrupt',
                                                 'control_request_id': result['request_id']})
        self.assertEqual(self.store._rows()[0], original)
        self.assertEqual(self.store.submit(self.aid, original['question'], self.rid, {}), current)
        self.assertNotIn(self.tmp.name, json.dumps(current))
        self.assertNotIn(self.turn, json.dumps(current))
        self.bridge.assert_called_once()
        self.reopen()
        self.assertEqual(self.row(), current)

    def test_only_successful_interrupt_can_fulfill(self):
        self.source()
        for state in ('unknown', 'no_op', 'failed', 'acknowledged'):
            with self.subTest(state=state):
                self.interrupt(state)
                self.reconcile()  # Startup reconciliation must reject these outcomes too.
                self.assertNotIn('fulfillment', self.row())
        self.assertEqual(self.bridge.call_count, 4)

    def test_narrow_owner_and_payload_grammar(self):
        self.source()
        negatives = [
            ('queue a message to Atlas: pause its work', 'Pause your work.'),
            ('tell Atlas to pause its work later', 'Pause your work.'),
            ('tell Atlas to pause its work after this task', 'Pause your work.'),
            ('tell Atlas to pause its work', 'Pause your work after this task.'),
            ('tell Atlas to pause its work and send an update', 'Pause your work.'),
            ('tell Atlas to pause its work', 'Pause your work and send an update.'),
            ('do not tell Atlas to pause its work', 'Pause your work.'),
            ('ask Atlas why it paused', 'Why did you pause?'),
            ('tell Atlas to stop the server', 'Stop the server.'),
            ('if needed tell Atlas to pause its work', 'Pause your work.'),
            ('example: tell Atlas to pause its work', 'Pause your work.'),
            ('tell Other to pause its work', 'Pause your work.'),
        ]
        for owner, question in negatives:
            with self.subTest(owner=owner, question=question):
                self.assertFalse(m.MessagingStore._pure_pause(owner, self.store._rows()[0], True)
                                 and m.MessagingStore._pure_pause(question, self.store._rows()[0]))
        for owner, payload in [('tell Atlas to pause its work', 'Pause your work.'),
                               ('ask Atlas to stop working now', 'Stop working now.'),
                               ('Interrupt Atlas now', 'Interrupt your work now.')]:
            self.assertTrue(self.store._pure_pause(owner, self.store._rows()[0], True))
            self.assertTrue(self.store._pure_pause(payload, self.store._rows()[0]))

    def test_missing_or_mismatched_source_provenance(self):
        self.source(provenance=False)
        self.interrupt()
        self.assertNotIn('fulfillment', self.row())
        with self.store.db:
            self.store.db.execute('INSERT INTO asks VALUES (?,?,?,?)',
                                  (self.rid, 'tell Atlas to pause its work', self.base,
                                   json.dumps({'kind': 'relay', 'agent': self.aid, 'question': 'Different'})))
        self.reconcile()
        self.assertNotIn('fulfillment', self.row())

    def test_deferred_original_with_immediate_payload_never_annotated(self):
        self.source(owner='queue a message to Atlas: pause its work')
        self.interrupt()
        self.assertNotIn('fulfillment', self.row())
        self.reopen()
        self.assertNotIn('fulfillment', self.row())

    def test_wrong_target_bound_turn_transcript_and_order_fail_closed(self):
        self.source()
        result = self.interrupt('unknown')
        cid = result['request_id']
        original = self.rewrite_control(cid, state='interrupted')
        variants = [dict(agent='x:' + ident()), dict(expected_turn_id=ident()),
                    dict(expected_turn_id='invalid'), dict(created=self.base + 1),
                    dict(created=self.base - 1), dict(completed_at=self.base + 9),
                    dict(transcript={**original['transcript'], 'inode': -1})]
        source = self.store._rows()[0]
        source['bound_turn'] = self.turn
        self.store._save(source)
        for changes in variants:
            with self.subTest(changes=changes):
                self.rewrite_control(cid, **{**original, **changes})
                self.reconcile()
                self.assertNotIn('fulfillment', self.row())

    def test_intervening_claims_and_direct_requests_block(self):
        self.source()
        other = ident()
        variants = [None, '{', json.dumps({'kind': 'relay', 'agent': self.aid, 'question': 'Continue'}),
                    json.dumps({'kind': 'control', 'agent': self.aid, 'action': 'steer', 'instruction': 'Continue'}),
                    json.dumps({'kind': 'relay', 'agent': 'x:' + ident()})]
        control = self.interrupt('unknown')
        self.rewrite_control(control['request_id'], state='interrupted')
        for raw in variants:
            with self.subTest(raw=raw):
                self.store.db.execute('INSERT OR REPLACE INTO asks VALUES (?,?,?,?)',
                                      (other, 'Continue', self.base + 5, raw))
                self.store.db.commit()
                self.reconcile()
                self.assertNotIn('fulfillment', self.row())
        self.store.db.execute('DELETE FROM asks WHERE id=?', (other,)); self.store.db.commit()
        with patch.object(m.time, 'time', return_value=self.base + 6):
            self.store.submit(self.aid, 'Continue working', other, self.agents)
        self.reconcile()
        self.assertNotIn('fulfillment', self.row())

    def test_peer_transport_blocks_owner_pause_fulfillment_without_public_leak(self):
        self.source()
        other = ident()
        with patch.object(m.time, 'time', return_value=self.base + 6):
            self.store.submit(self.aid, 'Peer follow-up', other, self.agents)
        peer = next(r for r in self.store._rows() if r['request_id'] == other)
        peer['origin'] = 'peer'
        self.store._save(peer)
        self.store.db.execute('INSERT INTO peer_requests VALUES (?,?,?,?,?,?,NULL)',
                              (ident(), ident(), ident(), 'hash', other, self.base + 6))
        self.store.db.commit()
        self.interrupt()
        self.assertNotIn('fulfillment', self.row())
        self.assertEqual(len(self.store.snapshot()['requests']), 1)
        self.assertEqual(peer['agent'], self.aid)

    def test_corrupt_launched_legacy_payload_still_blocks_pause_fulfillment(self):
        self.assert_corrupt_peer_launch_blocks(1)

    def test_corrupt_launched_shared_payload_still_blocks_pause_fulfillment(self):
        self.assert_corrupt_peer_launch_blocks(2)

    def test_peer_launch_outside_pause_interval_does_not_block_fulfillment(self):
        for launch_offset in (-5, 21):
            with self.subTest(launch_offset=launch_offset):
                self.source()
                transport_id = ident()
                self.store.db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)',
                    (ident(), self.aid, '{}', 'hash', transport_id, ident(), self.base-10, self.base+launch_offset, ''))
                self.store.db.execute('INSERT INTO requests VALUES (?,?)', (transport_id, '{corrupt'))
                self.store.db.commit()
                self.interrupt()
                self.assertEqual(self.row()['fulfillment']['state'], 'fulfilled')
                self.store.db.execute('DELETE FROM request_fulfillments')
                self.store.db.execute('DELETE FROM requests WHERE id=?', (transport_id,))
                self.store.db.execute('DELETE FROM shared_bridge_requests')
                self.store.db.execute('DELETE FROM controls')
                self.store.db.commit()

    def assert_corrupt_peer_launch_blocks(self, version):
        self.source()
        transport_id, client_id, target_id = ident(), ident(), ident()
        # Reservation predates authority; launch falls inside it. Only
        # durable launch metadata proves this interference after corruption.
        if version == 1:
            self.store.db.execute('INSERT INTO peer_principals VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (target_id, 'Recipient', 'codex', self.sid, self.tmp.name, 'declared', '{}', 'a'*64, 1, 1, self.base-30))
            self.store.db.execute('INSERT INTO peer_requests VALUES (?,?,?,?,?,?,?)',
                (ident(), client_id, target_id, 'hash', transport_id, self.base-10, self.base+6))
        else:
            self.store.db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)',
                (client_id, self.aid, '{}', 'hash', transport_id, ident(), self.base-10, self.base+6, ''))
        self.store.db.execute('INSERT INTO requests VALUES (?,?)', (transport_id, '{corrupt'))
        self.store.db.commit()
        self.interrupt()
        self.assertNotIn('fulfillment', self.row())
        self.assertEqual(len(self.store.snapshot()['requests']), 1)
        self.reopen()
        self.assertNotIn('fulfillment', self.row())
        self.store.db.execute('DELETE FROM requests WHERE id=?', (transport_id,))
        self.store.db.execute('DELETE FROM peer_requests')
        self.store.db.execute('DELETE FROM shared_bridge_requests')
        self.store.db.execute('DELETE FROM controls')
        self.store.db.commit()

    def test_delayed_source_interpretation_does_not_hide_intervening_resume(self):
        self.source()
        source = self.store._rows()[0]
        source['created'] = self.base + 8; self.store._save(source)
        self.store.db.execute('INSERT INTO asks VALUES (?,?,?,?)',
                              (ident(), 'Continue', self.base + 4,
                               json.dumps({'kind': 'relay', 'agent': self.aid, 'question': 'Continue'})))
        self.store.db.commit()
        self.interrupt()
        self.assertNotIn('fulfillment', self.row())

    def test_same_time_claim_is_ambiguous_but_later_instruction_preserves_history(self):
        self.source()
        other = ident()
        self.store.db.execute('INSERT INTO asks VALUES (?,?,?,NULL)', (other, 'Unresolved', self.base))
        self.store.db.commit()
        control = self.interrupt()
        self.assertNotIn('fulfillment', self.row())
        self.store.db.execute('UPDATE asks SET created=? WHERE id=?', (self.base + 21, other))
        self.store.db.commit()
        self.reconcile()
        self.assertEqual(self.row()['fulfillment']['control_request_id'], control['request_id'])

    def test_new_request_claimed_during_bridge_blocks(self):
        self.source()
        def bridge(*args, **kwargs):
            # claim_ask's timestamp is inside the completion interval.
            self.store.claim_ask(ident(), 'What should I do next?')
            return {'state': 'interrupted'}
        self.bridge.side_effect = bridge
        with patch.object(m.time, 'time', side_effect=[self.base + 10, self.base + 15, self.base + 20]):
            self.store.submit_control('interrupt', self.aid, '', ident(), self.agents, execute=self.bridge)
        self.assertNotIn('fulfillment', self.row())

    def test_tick_stale_copy_cannot_overwrite_fulfillment(self):
        self.source()
        entered, release = threading.Event(), threading.Event()
        def poll(row):
            entered.set(); release.wait(3)
            row['error'] = 'Polled independently'
        with patch.object(self.store, '_poll', side_effect=poll), concurrent.futures.ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.store.tick)
            self.assertTrue(entered.wait(1))
            control = self.interrupt()
            release.set(); future.result(timeout=3)
        self.assertEqual(self.row()['fulfillment']['control_request_id'], control['request_id'])
        self.assertEqual(self.row()['error'], 'Polled independently')

    def legacy(self, witness=True, stamp=None, turn=None):
        self.source()
        control = self.interrupt('unknown')
        stored = self.rewrite_control(control['request_id'], state='interrupted')
        stored.pop('completed_at')
        self.store.db.execute('UPDATE controls SET data=? WHERE id=?', (json.dumps(stored), control['request_id']))
        self.store.db.commit()
        if witness:
            stamp = stamp if stamp is not None else datetime.fromtimestamp(self.base + 15, timezone.utc).isoformat()
            self.append('event_msg', {'type': 'turn_aborted', 'turn_id': turn or self.turn}, stamp)
        return control, stored

    def test_startup_legacy_witness_with_large_later_log(self):
        control, stored = self.legacy()
        with open(self.path, 'a') as f:
            f.write(' ' * (m.MAX_READ + 100))
        self.reopen()
        self.assertEqual(self.row()['fulfillment']['control_request_id'], control['request_id'])
        self.assertEqual(self.row()['state'], 'queued')
        self.bridge.assert_called_once()

    def test_legacy_without_exact_aware_witness_never_guesses(self):
        self.legacy(witness=False)
        self.reopen(); self.assertNotIn('fulfillment', self.row())
        for kind, turn, stamp in [('task_complete', self.turn, datetime.fromtimestamp(self.base + 15, timezone.utc).isoformat()),
                                   ('turn_aborted', ident(), datetime.fromtimestamp(self.base + 15, timezone.utc).isoformat()),
                                   ('turn_aborted', self.turn, '2026-10-30T12:00:00'),
                                   ('turn_aborted', self.turn, 'bad')]:
            with self.subTest(kind=kind, stamp=stamp):
                original = os.path.getsize(self.path)
                self.append('event_msg', {'type': kind, 'turn_id': turn}, stamp)
                self.reconcile()
                self.assertNotIn('fulfillment', self.row())
                with open(self.path, 'rb+') as f: f.truncate(original)

    def test_legacy_replaced_transcript_cannot_supply_witness(self):
        self.legacy()
        os.rename(self.path, self.path + '.old')
        with open(self.path + '.old', 'rb') as source, open(self.path, 'wb') as target:
            target.write(source.read())
        self.reopen()
        self.assertNotIn('fulfillment', self.row())

    def test_fulfillment_survives_control_pruning_late_reply_and_is_removed_with_request(self):
        self.source()
        control = self.interrupt()
        current = self.row()['fulfillment']
        self.rewrite_control(control['request_id'], created=self.base - m.RETENTION - 1)
        # A new control prunes the old control, but cannot replace its annotation.
        self.interrupt('no_op', created=30, completed=31)
        self.assertIsNone(self.store.db.execute('SELECT data FROM controls WHERE id=?', (control['request_id'],)).fetchone())
        row = self.store._rows()[0]
        row['current_turn'] = self.turn
        self.store._consume(row, {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                                    'content': [{'text': m.outgoing_message(row)}]}})
        self.store._consume(row, {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': self.turn,
                                    'last_agent_message': 'WATCH_REPLY ' + self.rid + '\nPaused.'}})
        self.store._save(row)
        self.assertEqual(self.row()['state'], 'answered')
        self.assertEqual(self.row()['reply'], 'Paused.')
        self.assertEqual(self.row()['fulfillment'], current)
        self.reopen()
        self.assertEqual(self.row()['fulfillment'], current)
        self.store._prune(self.store._rows(), self.base + m.RETENTION + 2)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM request_fulfillments').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
