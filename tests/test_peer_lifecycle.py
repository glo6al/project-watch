"""Execution ownership survives payload retention; all fixtures are synthetic."""
import json
import sqlite3
import sys
import time
import unittest
import uuid
from types import SimpleNamespace
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import peer_bridge
import peer_lifecycle as life


def uid():
    return str(uuid.uuid4())


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        peer_bridge.shared_schema(self.db)
        self.tid, self.target = uid(), 'c:' + uid()
        self.created = time.time() - 90000

    def tearDown(self):
        self.db.close()

    def admitted(self, launched=True):
        self.db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)',
                        (uid(), self.target, '{}', 'fictional-digest', self.tid, 'fictional-credential',
                         self.created, self.created if launched else None, ''))
        return dict(request_id=self.tid, agent=self.target, created=self.created, launched=launched,
                    state='unknown', origin='peer', runtime='claude', transport='claude_mod', bridge_version=2,
                    mod_enrollment=uid(), mod_attempt_id=uid(), mod_generation=uid(), mod_turn='fictional-turn',
                    mod_project='/fictional/project', target_project='/fictional/project')

    def test_owner_retirement_releases_only_policy_capacity_and_survives_late_save(self):
        row = self.admitted(); row['lifecycle_protocol'] = 2
        self.db.execute('INSERT INTO requests VALUES (?,?)', (self.tid, json.dumps(row)))
        result = life.retire(self.db, self.tid, life.RETIRE_REASON, expected_agent=self.target)
        self.assertEqual((result['execution'], result['capture']), ('retired', 'withheld'))
        self.assertEqual(result['evidence']['owner_retirement']['provider_outcome'], 'unknown')
        self.assertNotIn('execution_outcome', result['evidence'])
        self.assertIsNone(life.capacity_error(self.db, self.target, time.time(), 900))
        self.assertEqual(life.blockers(self.db, self.target), [])
        self.assertFalse(life.downgrade_ready(self.db))
        self.assertEqual(life.retire(self.db, self.tid, life.RETIRE_REASON), result)
        late = dict(row, state='answered', settled=True, execution_outcome='completed', reply='Fictional late reply')
        # Covers MessagingStore's write-before-reconcile ordering.
        self.db.execute('UPDATE requests SET data=? WHERE id=?', (json.dumps(late), self.tid))
        life.reconcile(self.db, late)
        saved = json.loads(self.db.execute('SELECT data FROM requests WHERE id=?', (self.tid,)).fetchone()[0])
        self.assertEqual(saved['state'], 'unknown'); self.assertEqual(saved['reply'], '')
        self.assertNotIn('settled', saved); self.assertNotIn('execution_outcome', saved)
        self.assertTrue(saved['tracking_stopped'])
        life.migrate(self.db)
        self.assertEqual(life.get(self.db, self.tid), result)
        self.db.execute('DELETE FROM requests')
        life.migrate(self.db)
        self.assertEqual(life.get(self.db, self.tid), result)
        self.assertEqual(self.db.execute('SELECT count(*) FROM requests').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM shared_bridge_requests').fetchone()[0], 1)

    def test_legacy_retirement_preserves_tombstone_and_public_unknown_after_pruning(self):
        native, principal, sender, client_id = uid(), uid(), uid(), uid()
        self.db.execute('INSERT INTO peer_principals VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                        (principal, 'Fictional recipient', 'codex', native, '/fictional/project',
                         'exact', '{}', 'fictional-secret', 1, 1, self.created))
        self.db.execute('INSERT INTO peer_requests VALUES (?,?,?,?,?,?,?)',
                        (sender, client_id, principal, 'fictional-hash', self.tid, self.created, self.created))
        retired = life.retire(self.db, self.tid, life.RETIRE_REASON, expected_agent='x:'+native)
        self.assertEqual(retired['execution'], 'retired')
        bridge = peer_bridge.PeerBridge.__new__(peer_bridge.PeerBridge)
        bridge.store = SimpleNamespace(db=self.db)
        result = bridge.result(sender, client_id)
        self.assertEqual(result['state'], 'unknown'); self.assertEqual(result['reply'], '')
        self.assertIn('retired by the owner', result['error'])
        self.assertEqual(self.db.execute('SELECT transport_id FROM peer_requests').fetchall(), [(self.tid,)])
        life.migrate(self.db)
        self.assertEqual(life.get(self.db, self.tid), retired)

    def test_retirement_rejects_unlaunched_terminal_and_wrong_identity(self):
        row = self.admitted(launched=False)
        with self.assertRaisesRegex(ValueError, 'launched'):
            life.retire(self.db, self.tid, life.RETIRE_REASON)
        self.db.execute('UPDATE shared_bridge_requests SET launched=created')
        row['launched'] = True
        with self.assertRaisesRegex(ValueError, 'identity'):
            life.retire(self.db, self.tid, life.RETIRE_REASON, expected_agent='c:'+uid())
        with self.assertRaisesRegex(ValueError, 'decision'):
            life.retire(self.db, self.tid, 'invented_reason')
        for state in ('completed', 'dropped'):
            self.db.execute('DELETE FROM peer_execution')
            terminal = dict(row, state='failed' if state == 'dropped' else 'unknown', execution_outcome=state)
            life.reconcile(self.db, terminal)
            with self.assertRaisesRegex(ValueError, 'terminal'):
                life.retire(self.db, self.tid, life.RETIRE_REASON)

    def test_retirement_migrates_partial_index_and_preserves_recent_global_capacity(self):
        row = self.admitted(); self.created = time.time()
        self.db.execute('UPDATE shared_bridge_requests SET created=?,launched=?', (self.created, self.created))
        row['created'] = self.created
        self.db.execute('INSERT INTO requests VALUES (?,?)', (self.tid, json.dumps(row)))
        self.db.execute('DROP INDEX peer_execution_active_created')
        self.db.execute("CREATE INDEX peer_execution_active_created ON peer_execution(created) WHERE execution NOT IN ('completed','dropped')")
        life.schema(self.db)
        self.assertIn("'retired'", self.db.execute("SELECT sql FROM sqlite_master WHERE name='peer_execution_active_created'").fetchone()[0])
        life.retire(self.db, self.tid, life.RETIRE_REASON)
        count = self.db.execute("SELECT count(*) FROM peer_execution WHERE execution NOT IN ('completed','dropped','retired') AND created>?", (time.time()-900,)).fetchone()[0]
        self.assertEqual(count, 0)

    def test_expired_payload_does_not_release_uncertain_execution(self):
        row = self.admitted()
        life.reconcile(self.db, row)
        # Simulate retention and startup migration: immutable ledger remains,
        # but no message body or pollable transport row exists.
        life.migrate(self.db)
        self.assertEqual(life.get(self.db, self.tid)['execution'], 'uncertain')
        blocked = life.capacity_error(self.db, self.target, time.time(), 900)
        self.assertIsNotNone(blocked)
        self.assertIn('recovery', blocked)
        row.update(execution_outcome='completed', settled=True, capture_disposition='unusable')
        life.reconcile(self.db, row)
        life.migrate(self.db)
        self.assertIsNone(life.capacity_error(self.db, self.target, time.time(), 900))
        self.assertEqual(life.get(self.db, self.tid)['execution'], 'completed')

    def test_migration_distinguishes_never_launched_from_missing_launched_payload(self):
        self.admitted(launched=False)
        life.migrate(self.db)
        self.assertEqual(life.get(self.db, self.tid)['execution'], 'dropped')
        self.assertIsNone(life.capacity_error(self.db, self.target, time.time(), 900))

    def test_terminal_and_withheld_are_monotonic_across_old_events(self):
        row = self.admitted()
        row.update(execution_outcome='completed', capture_disposition='withheld', settled=True)
        life.reconcile(self.db, row)
        for state in ('queued', 'awaiting_reply', 'unknown', 'expired', 'answered'):
            stale = dict(row, state=state, execution_outcome='', settled=False, capture_disposition='', reply='')
            life.reconcile(self.db, stale)
            record = life.get(self.db, self.tid)
            self.assertEqual((record['execution'], record['capture']), ('completed', 'withheld'))
            self.assertEqual(life.blockers(self.db, self.target), [])

    def test_retained_evidence_has_no_payload_or_filesystem_paths(self):
        row = self.admitted()
        row.update(question='Fictional confidential question', reply='Fictional private answer',
                   path='/fictional/private/transcript.jsonl', mod_project='/fictional/private/project', target_project='/fictional/private/project',
                   mod_enrollment=uid(), mod_attempt_id=uid(), mod_turn='fictional-turn', lifecycle_protocol=2)
        life.reconcile(self.db, row)
        stored = self.db.execute('SELECT evidence FROM peer_execution').fetchone()[0]
        for value in (row['question'], row['reply'], row['path'], row['mod_project']):
            self.assertNotIn(value, stored)
        self.assertEqual(json.loads(stored)['mod_attempt_id'], row['mod_attempt_id'])
        self.assertFalse(life.downgrade_ready(self.db))

    def test_original_identity_and_attempt_cannot_be_replaced(self):
        row = self.admitted(); row['mod_attempt_id'] = uid()
        life.reconcile(self.db, row)
        with self.assertRaisesRegex(ValueError, 'original attempt'):
            life.reconcile(self.db, dict(row, mod_attempt_id=uid()))
        with self.assertRaisesRegex(ValueError, 'identity'):
            life.reconcile(self.db, dict(row, agent='c:'+uid()))

    def test_empty_turn_can_bind_once_but_cannot_change_identity(self):
        row = self.admitted(); row['bound_turn'] = ''
        life.reconcile(self.db, row)
        self.assertNotIn('bound_turn', life.get(self.db, self.tid)['evidence'])
        row['bound_turn'] = 'fictional-original-turn'; life.reconcile(self.db, row)
        row['bound_turn'] = ''; life.reconcile(self.db, row)
        self.assertEqual(life.get(self.db, self.tid)['evidence']['bound_turn'], 'fictional-original-turn')
        with self.assertRaisesRegex(ValueError, 'original attempt'):
            life.reconcile(self.db, dict(row, bound_turn='different-turn'))

    def test_malformed_payload_never_supplies_completion_proof_or_another_id(self):
        baseline = self.admitted()
        corruptions = [
            {'settled':'false'}, {'settled':1}, {'request_id':uid()}, {'origin':'owner'},
            {'bridge_version':1}, {'bridge_version':True}, {'agent':'c:'+uid()}, {'launched':'true'},
            {'created':'yesterday'}, {'state':'invented'}, {'runtime':'codex'},
            {'execution_outcome':True}, {'capture_disposition':[]}, {'mod_attempt_id':'not-a-uuid'},
            {'mod_turn':''}, {'target_project':'/fictional/other'}, {'reply':{}}, {'reply':'\ud800'},
        ]
        for corruption in corruptions:
            with self.subTest(corruption=corruption):
                self.db.execute('DELETE FROM peer_execution')
                row = dict(baseline, state='answered', reply='Fictional answer', settled=True)
                row.update(corruption)
                self.db.execute('INSERT OR REPLACE INTO requests VALUES (?,?)', (self.tid, json.dumps(row)))
                life.migrate(self.db)
                record = life.get(self.db, self.tid)
                self.assertIsNotNone(record)
                self.assertEqual(record['execution'], 'uncertain')
                self.assertEqual(record['capture'], 'pending')
                self.assertEqual(record['evidence'], {'launched': True})
                self.assertEqual(self.db.execute('SELECT request_id FROM peer_execution').fetchall(), [(self.tid,)])
                self.assertIsNotNone(life.capacity_error(self.db, self.target, time.time(), 900))
        self.db.execute('DELETE FROM peer_execution')
        malformed = dict(baseline, state='unknown', settled=False, mod_project={'unexpected':'object'})
        self.db.execute('UPDATE requests SET data=? WHERE id=?', (json.dumps(malformed), self.tid))
        life.migrate(self.db)
        self.assertEqual(life.get(self.db, self.tid)['execution'], 'uncertain')
        self.assertEqual(life.get(self.db, self.tid)['evidence'], {'launched': True})
        # The same typed-proof gate also applies to active save/prune callers.
        self.db.execute('DELETE FROM peer_execution')
        life.reconcile(self.db, dict(baseline, state='answered', reply='Fictional answer', settled='false'))
        self.assertEqual(life.get(self.db, self.tid)['execution'], 'uncertain')

    def test_valid_persisted_completion_can_migrate_under_its_ledger_id(self):
        row = dict(self.admitted(), state='answered', reply='Fictional answer', settled=True)
        self.db.execute('INSERT INTO requests VALUES (?,?)', (self.tid, json.dumps(row)))
        life.migrate(self.db)
        record = life.get(self.db, self.tid)
        self.assertEqual((record['execution'], record['capture']), ('completed', 'captured'))
        self.assertIsNone(life.capacity_error(self.db, self.target, time.time(), 900))

    def test_journal_is_bounded_and_duplicate_ack_does_not_grow_it(self):
        life.record_event(self.db, self.tid, 'completed', 'unusable', 'running', 'completed', event_id='same')
        life.record_event(self.db, self.tid, 'completed', 'unusable', 'running', 'completed', event_id='same')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM peer_execution_events').fetchone()[0], 1)
        for index in range(life.MAX_EVENTS + 2):
            life.record_event(self.db, self.tid, 'unknown', 'unknown', 'running', 'uncertain', event_id=str(index))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM peer_execution_events').fetchone()[0], life.MAX_EVENTS)


if __name__ == '__main__':
    unittest.main()
