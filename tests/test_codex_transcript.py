"""Fictional native metadata and continuation fixtures; never contacts agents."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch, Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import codex_transcript as c
import officer_messaging as m
import peer_lifecycle


class FilenameTests(unittest.TestCase):
    def test_rollout_instance_suffix_is_not_the_thread_identity(self):
        thread, instance = str(uuid.uuid4()), str(uuid.uuid4())
        self.assertEqual(c.filename_id('rollout-' + thread + '.jsonl'), thread)
        self.assertEqual(c.filename_id('rollout-' + thread + '_' + instance + '.jsonl'), thread)
        self.assertIsNone(c.filename_id('not-a-native-rollout.jsonl'))

    def test_native_output_metadata_is_supported_only_on_output(self):
        part = dict(type='output_text', text='Fictional answer.', annotations=[], logprobs=None)
        self.assertEqual(c.pure_text([part], 'assistant'), 'Fictional answer.')
        self.assertIsNone(c.pure_text([dict(part, type='input_text')], 'user'))
        self.assertEqual(c.pure_text([dict(type='input_text', text='Fictional instruction.')], 'developer'), 'Fictional instruction.')
        self.assertIsNone(c.pure_text([dict(part, image_url='fictional')], 'assistant'))
        self.assertIsNone(c.pure_text([dict(part, annotations='malformed')], 'assistant'))


@unittest.skipIf(os.name == 'nt', 'POSIX native metadata and private messaging storage')
class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.sessions = self.root / 'sessions'; self.sessions.mkdir()
        self.ident = str(uuid.uuid4())
        self.old = self.sessions / ('rollout-' + self.ident + '.jsonl')
        self.new = self.sessions / ('rollout-' + self.ident + '_' + str(uuid.uuid4()) + '.jsonl')
        self.meta = dict(id=self.ident, session_id=self.ident, cwd=str(self.root), source='vscode')
        self.append(self.old, 'session_meta', self.meta)
        self.store = m.MessagingStore(str(self.root / 'watch'), str(self.sessions))
        self.rid = str(uuid.uuid4())
        st = self.old.stat()
        self.row = dict(request_id=self.rid, agent='x:' + self.ident, origin='peer', runtime='codex',
                        state='queued', created=time.time(), launched=True, path=str(self.old), device=st.st_dev,
                        inode=st.st_ino, cursor=st.st_size, current_turn='', bound_turn='', final='', reply='', error='',
                        envelope='WATCH_PEER_REQUEST ' + self.rid + '\nFictional test only.')

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def append(self, path, typ, payload):
        with open(path, 'ab') as f:
            f.write(json.dumps(dict(type=typ, payload=payload)).encode() + b'\n')

    def pointer(self, path, **updates):
        db = sqlite3.connect(self.root / 'state_5.sqlite')
        db.execute('CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, archived INTEGER, source TEXT)')
        value = dict(id=self.ident, rollout_path=str(path), cwd=str(self.root), archived=0, source='vscode'); value.update(updates)
        db.execute('INSERT OR REPLACE INTO threads VALUES (?,?,?,?,?)', tuple(value[k] for k in ('id','rollout_path','cwd','archived','source')))
        db.commit(); db.close()

    def successor(self, **base_updates):
        raw = self.old.read_bytes()
        base = dict(thread_id=self.ident, end_byte_offset=len(raw), end_ordinal_exclusive=raw.count(b'\n'))
        base.update(base_updates)
        self.append(self.new, 'session_meta', dict(self.meta, history_base=base, history_mode='paginated'))
        self.pointer(self.new)

    def message(self, path, turn, text):
        self.append(path, 'response_item', dict(type='message', role='user', content=[dict(text=text)], internal_chat_message_metadata_passthrough=dict(turn_id=turn)))

    def reply(self, turn='t1', start=True):
        if start: self.append(self.new, 'event_msg', dict(type='task_started', turn_id=turn))
        self.message(self.new, turn, self.row['envelope'])
        self.append(self.new, 'event_msg', dict(type='task_complete', turn_id=turn, last_agent_message='WATCH_REPLY ' + self.rid + '\nFictional answer.'))

    def poll(self, count=8):
        for _ in range(count):
            if self.row['state'] in ('answered', 'quarantined') or self.row.get('tracking_stopped'): break
            self.store._poll(self.row)

    def test_native_pointer_selects_suffixed_rollout_not_suffix_agent(self):
        self.successor()
        path, source, _, _ = c.resolve(str(self.sessions), self.ident)
        source.close()
        self.assertEqual(path, str(self.new))
        self.assertEqual(c.filename_id(self.new.name), self.ident)
        self.assertEqual(self.store._target_log(self.ident)[0], str(self.new))
        with self.assertRaises(m.MessagingError): self.store._metadata(str(self.old), self.ident)

    def test_existing_invalid_native_index_never_falls_back(self):
        (self.root / 'state_5.sqlite').write_bytes(b'not a database')
        with self.assertRaises(c.Unavailable): c.resolve(str(self.sessions), self.ident)

    def test_index_missing_row_archived_project_or_header_fail_closed(self):
        for changes in [dict(id=str(uuid.uuid4())), dict(archived=1), dict(cwd='/fictional/wrong'), dict(source='exec')]:
            with self.subTest(changes=changes):
                (self.root / 'state_5.sqlite').unlink(missing_ok=True)
                self.pointer(self.old, **changes)
                with self.assertRaises(c.Unavailable): c.resolve(str(self.sessions), self.ident)
        self.pointer(self.old)
        self.old.write_text(json.dumps(dict(type='session_meta', payload=dict(self.meta, id=str(uuid.uuid4()))))+'\n')
        with self.assertRaises(c.Unavailable): c.resolve(str(self.sessions), self.ident)

    def test_symlink_log_and_index_rejected(self):
        backup = self.root / 'backup'; self.old.rename(backup); self.old.symlink_to(backup)
        with self.assertRaises((OSError, c.Unavailable)): c.resolve(str(self.sessions), self.ident)
        self.old.unlink(); backup.rename(self.old)
        self.pointer(self.old)
        db = self.root / 'state_5.sqlite'; db.rename(self.root/'saved.sqlite'); db.symlink_to(self.root/'saved.sqlite')
        with self.assertRaises(c.Unavailable): c.resolve(str(self.sessions), self.ident)

    def test_replaced_tracking_file_does_not_parse_new_contents(self):
        replacement = self.root / 'replacement'; replacement.write_bytes(self.old.read_bytes())
        os.replace(replacement, self.old)
        self.store._poll(self.row)
        self.assertEqual(self.row['state'], 'unknown')
        self.assertEqual(self.row['bound_turn'], '')

    def test_clean_continuation_preserves_original_pin_and_answers(self):
        self.successor(); self.reply(); original = self.row['path']
        self.poll(); self.store.peers.reconcile([self.row])
        self.assertEqual(self.row['state'], 'answered')
        self.assertEqual(self.row['reply'], 'Fictional answer.')
        self.assertEqual(self.row['path'], original)
        self.assertEqual(self.row['codex_tracking']['path'], str(self.new))
        self.assertTrue(self.row['settled'])

    def test_unread_predecessor_owner_input_is_drained_and_withheld(self):
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='t1'))
        self.message(self.old, 't1', 'Fictional owner input')
        self.successor(); self.reply(start=False)
        self.poll(); self.store.peers.reconcile([self.row])
        self.assertEqual(self.row['state'], 'unknown')
        self.assertTrue(self.row['settled']); self.assertEqual(self.row['reply'], '')
        self.assertEqual(self.row['cursor'], self.old.stat().st_size)

    def test_cursor_past_history_base_with_aborted_old_tail_is_conservative(self):
        prefix = self.old.read_bytes()
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='old'))
        self.message(self.old, 'old', 'Fictional old owner input')
        self.append(self.old, 'event_msg', dict(type='turn_aborted', turn_id='old'))
        self.row['cursor'] = self.old.stat().st_size
        self.successor(end_byte_offset=len(prefix), end_ordinal_exclusive=prefix.count(b'\n'))
        self.reply(); self.poll(); self.store.peers.reconcile([self.row])
        self.assertEqual(self.row['state'], 'unknown'); self.assertTrue(self.row['settled'])
        self.assertEqual(self.row['reply'], '')

    def test_mixed_successor_turn_never_publishes(self):
        self.successor(); self.append(self.new, 'event_msg', dict(type='task_started', turn_id='t1'))
        self.message(self.new, 't1', 'Fictional prior owner message')
        self.reply(start=False); self.poll(); self.store.peers.reconcile([self.row])
        self.assertEqual(self.row['state'], 'unknown'); self.assertTrue(self.row['settled'])

    def test_wrong_boundary_is_not_accepted_on_later_ticks(self):
        self.successor(end_ordinal_exclusive=999); self.reply()
        self.poll(5)
        self.assertEqual(self.row['state'], 'unknown'); self.assertNotIn('codex_tracking', self.row)
        self.assertFalse(self.row.get('settled'))

    def test_multiple_hop_or_extra_segment_rejected(self):
        self.successor(); self.reply()
        third = self.sessions / ('rollout-' + self.ident + '_' + str(uuid.uuid4()) + '.jsonl')
        self.append(third, 'session_meta', self.meta)
        self.poll()
        self.assertEqual(self.row['state'], 'unknown'); self.assertNotIn('codex_tracking', self.row)
        third.unlink()
        # A predecessor with its own history base is not a proven root segment.
        self.old.write_text(json.dumps(dict(type='session_meta', payload=dict(self.meta, history_base={})))+'\n')
        self.poll(); self.assertNotIn('codex_tracking', self.row)

    def test_bounded_prefix_progress_survives_restart_without_send(self):
        for _ in range(30): self.append(self.old, 'event_msg', dict(type='fixture', value='x'*80))
        self.row['cursor'] = self.old.stat().st_size
        self.successor(); self.reply()
        with patch.object(m, 'MAX_READ', 512):
            self.store._poll(self.row)
            self.assertEqual(self.row['codex_continuation']['offset'], 512)
            self.store._save(self.row)
            self.store.close()
            self.store = m.MessagingStore(str(self.root / 'watch'), str(self.sessions))
            self.row = next(row for row in self.store._rows() if row['request_id'] == self.rid)
            with patch.object(m.subprocess, 'Popen', side_effect=AssertionError('no resend')):
                self.poll(50)
        self.store.peers.reconcile([self.row]); self.assertEqual(self.row['state'], 'answered')

    def test_owner_offset_in_other_segment_does_not_release_quarantine(self):
        self.successor(); self.reply(); self.poll()
        owner = dict(self.row, origin='owner', request_id=str(uuid.uuid4()), state='unknown', bound_turn='', cursor=10**9)
        owner.pop('codex_tracking'); owner.pop('complete_segment', None)
        self.store.peers.reconcile([self.row, owner])
        self.assertEqual(self.row['state'], 'quarantined')
        owner['tracking_stopped'] = True
        self.store.peers.reconcile([self.row, owner])
        self.assertEqual(self.row['state'], 'unknown'); self.assertEqual(self.row['reply'], '')

    def test_native_pointer_change_during_proof_never_switches(self):
        self.successor(); self.reply(); self.store._poll(self.row)
        self.pointer(self.old)
        self.store._poll(self.row)
        self.assertNotIn('codex_tracking', self.row)
        self.assertEqual(self.row['bound_turn'], '')

    def oversized(self, path, size=4500):
        self.append(path, 'compacted', {'opaque': 'x' * size})

    def test_oversized_before_explicit_envelope_drains_across_restart_and_withholds(self):
        self.oversized(self.old)
        self.append(self.old, 'response_item', dict(type='message', role='user', content=[dict(text=self.row['envelope'])],
                    internal_chat_message_metadata_passthrough=dict(turn_id='explicit')))
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='explicit',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nMust stay private.'))
        with patch.object(m, 'MAX_LINE', 300), patch.object(m, 'MAX_READ', 600):
            self.store._poll(self.row)
            self.assertTrue(self.row.get('codex_discard_record'))
            self.assertLessEqual(self.row['cursor'], self.old.stat().st_size)
            self.store._save(self.row)
            self.store.close()
            self.store = m.MessagingStore(str(self.root / 'watch'), str(self.sessions))
            self.row = next(row for row in self.store._rows() if row['request_id'] == self.rid)
            with patch.object(m.subprocess, 'Popen', side_effect=AssertionError('no resend')):
                self.poll(30)
        self.store.peers.reconcile([self.row])
        self.assertTrue(self.row.get('settled'))
        self.assertEqual(self.row['state'], 'unknown')
        self.assertEqual(self.row['reply'], '')
        self.assertEqual(self.row['capture_disposition'], 'withheld')
        self.assertNotIn('codex_discard_record', self.row)

    def test_oversized_inside_bound_turn_requires_new_envelope_proof(self):
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='bound'))
        self.message(self.old, 'bound', self.row['envelope'])
        self.oversized(self.old)
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='bound',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nNot enough provenance.'))
        with patch.object(m, 'MAX_LINE', 300), patch.object(m, 'MAX_READ', 600): self.poll(30)
        self.assertFalse(self.row.get('settled'))
        self.assertTrue(self.row.get('codex_gap_needs_envelope'))
        self.assertEqual(self.row['reply'], '')

    def test_oversized_gap_fresh_started_envelope_proves_completion_not_capture(self):
        self.oversized(self.old, 900)
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='fresh'))
        self.append(self.old, 'response_item', dict(type='message', role='user', content=[dict(text=self.row['envelope'])]))
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='fresh',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nWithheld.'))
        with patch.object(m, 'MAX_LINE', 300), patch.object(m, 'MAX_READ', 1600): self.poll(8)
        self.store.peers.reconcile([self.row])
        self.assertTrue(self.row.get('settled')); self.assertEqual(self.row['state'], 'unknown')
        self.assertEqual(self.row['reply'], '')

    def test_gap_does_not_trust_turn_context_without_fresh_start_or_explicit_envelope(self):
        self.oversized(self.old, 900)
        self.append(self.old, 'turn_context', dict(turn_id='unproven'))
        self.append(self.old, 'response_item', dict(type='message', role='user', content=[dict(text=self.row['envelope'])]))
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='unproven',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nUnproven.'))
        with patch.object(m, 'MAX_LINE', 300), patch.object(m, 'MAX_READ', 1600): self.poll(8)
        self.assertFalse(self.row.get('settled')); self.assertEqual(self.row['reply'], '')

    def test_malformed_or_nondict_after_binding_invalidates_completion_proof(self):
        original = json.loads(json.dumps(self.row))
        header = self.old.read_bytes()
        for malformed in (b'{broken native record}\n', b'[]\n', b'null\n', b'{"type":"event_msg","payload":[]}\n'):
            with self.subTest(record=malformed):
                self.old.write_bytes(header); self.row = json.loads(json.dumps(original))
                self.append(self.old, 'event_msg', dict(type='task_started', turn_id='bound'))
                self.message(self.old, 'bound', self.row['envelope'])
                with open(self.old, 'ab') as source: source.write(malformed)
                self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='bound',
                            last_agent_message='WATCH_REPLY ' + self.rid + '\nNo provenance.'))
                self.poll()
                self.assertFalse(self.row.get('settled'))
                self.assertTrue(self.row.get('codex_gap_needs_envelope'))
                self.assertEqual(self.row['reply'], '')

    def test_malformed_before_fresh_explicit_envelope_settles_withheld(self):
        with open(self.old, 'ab') as source: source.write(b'{broken native record}\n')
        self.message(self.old, 'explicit', self.row['envelope'])
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='explicit',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nMust stay withheld.'))
        self.poll(); self.store.peers.reconcile([self.row])
        self.assertTrue(self.row.get('settled')); self.assertEqual(self.row['reply'], '')
        self.assertEqual(self.row['capture_disposition'], 'withheld')

    def test_native_single_lf_envelope_normalization_is_narrow(self):
        original = json.loads(json.dumps(self.row))
        header = self.old.read_bytes()
        for suffix in ('', '\n', '\r\n', '\n\n', ' ', '\nExtra user instruction'):
            with self.subTest(suffix=repr(suffix)):
                self.old.write_bytes(header); self.row = json.loads(json.dumps(original))
                self.append(self.old, 'event_msg', dict(type='task_started', turn_id='native'))
                self.message(self.old, 'native', self.row['envelope'] + suffix)
                self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='native',
                            last_agent_message='WATCH_REPLY ' + self.rid + '\nFictional answer.'))
                self.poll(); self.store.peers.reconcile([self.row])
                self.assertEqual(bool(self.row.get('settled')), suffix in ('', '\n'))
                self.assertEqual(self.row['envelope'], original['envelope'])
                if suffix not in ('', '\n'): self.assertEqual(self.row['reply'], '')

    def test_postgap_conflicting_turn_preserves_durable_binding_without_worker_failure(self):
        self.row['bridge_version'] = 2
        self.store.db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)',
            (str(uuid.uuid4()), self.row['agent'], '{}', 'a'*64, self.row['request_id'], str(uuid.uuid4()),
             self.row['created'], time.time(), 'Fictional'))
        self.store.db.commit()
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='original'))
        self.message(self.old, 'original', self.row['envelope'])
        self.store._poll(self.row); self.store._save(self.row)
        self.assertEqual(peer_lifecycle.get(self.store.db,self.rid)['evidence']['bound_turn'], 'original')
        self.oversized(self.old, 900)
        self.message(self.old, 'different', self.row['envelope'])
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='different',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nWrong turn.'))
        with patch.object(m, 'MAX_LINE', 300), patch.object(m, 'MAX_READ', 1600): self.store.tick()
        self.row = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assertEqual(self.row['bound_turn'], 'original')
        self.assertEqual(self.row['state'], 'unknown'); self.assertFalse(self.row.get('settled'))
        self.assertEqual(self.row['reply'], ''); self.assertFalse(self.store.worker_failed)
        self.assertEqual(peer_lifecycle.get(self.store.db,self.rid)['evidence']['bound_turn'], 'original')

    def test_same_poll_conflicting_peer_turn_preserves_first_binding(self):
        self.row['bridge_version'] = 2
        self.store.db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)',
            (str(uuid.uuid4()), self.row['agent'], '{}', 'a'*64, self.rid, str(uuid.uuid4()),
             self.row['created'], time.time(), 'Fictional'))
        self.store.db.commit(); self.store._save(self.row)
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='original'))
        self.message(self.old, 'original', self.row['envelope'])
        self.message(self.old, 'different', self.row['envelope'])
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='different',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nWrong turn.'))
        self.store.tick()
        self.row = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assertEqual(self.row['bound_turn'], 'original')
        self.assertFalse(self.row.get('settled')); self.assertFalse(self.store.worker_failed)
        self.assertEqual(self.row['reply'], '')
        self.assertEqual(peer_lifecycle.get(self.store.db, self.rid)['evidence']['bound_turn'], 'original')

    def test_same_turn_fresh_postgap_envelope_can_settle_only_withheld(self):
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='same'))
        self.message(self.old, 'same', self.row['envelope'])
        self.oversized(self.old, 900)
        self.message(self.old, 'same', self.row['envelope'])
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='same',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nWithheld.'))
        with patch.object(m, 'MAX_LINE', 300), patch.object(m, 'MAX_READ', 1600): self.poll()
        self.store.peers.reconcile([self.row])
        self.assertTrue(self.row.get('settled')); self.assertEqual(self.row['bound_turn'], 'same')
        self.assertEqual(self.row['state'], 'unknown'); self.assertEqual(self.row['reply'], '')

    def test_unverifiable_foreign_message_metadata_invalidates_binding(self):
        original = json.loads(json.dumps(self.row)); header = self.old.read_bytes()
        variants = [dict(internal_chat_message_metadata_passthrough=[]), dict(turn_id=[]),
                    dict(turn_id='bound', internal_chat_message_metadata_passthrough=dict(turn_id='different'))]
        for metadata in variants:
            with self.subTest(metadata=metadata):
                self.old.write_bytes(header); self.row = json.loads(json.dumps(original))
                self.append(self.old, 'event_msg', dict(type='task_started', turn_id='bound'))
                self.message(self.old, 'bound', self.row['envelope'])
                self.append(self.old, 'response_item', dict(type='message', role='user',
                            content=[dict(text='Fictional owner input')], **metadata))
                self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='bound',
                            last_agent_message='WATCH_REPLY ' + self.rid + '\nMust not escape.'))
                self.poll(); self.store.peers.reconcile([self.row])
                self.assertFalse(self.row.get('settled')); self.assertTrue(self.row.get('codex_gap_needs_envelope'))
                self.assertEqual(self.row['reply'], ''); self.assertEqual(self.row['capture_disposition'], 'withheld')

    def test_pure_native_user_text_content_shapes_remain_correlatable(self):
        original = json.loads(json.dumps(self.row)); header = self.old.read_bytes()
        for kind in ('input_text', 'text', None):
            with self.subTest(kind=kind):
                self.old.write_bytes(header); self.row = json.loads(json.dumps(original))
                part = dict(text=self.row['envelope'])
                if kind is not None: part['type'] = kind
                self.append(self.old, 'event_msg', dict(type='task_started', turn_id='pure'))
                self.append(self.old, 'response_item', dict(type='message', role='user', content=[part],
                            internal_chat_message_metadata_passthrough=dict(turn_id='pure')))
                self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='pure',
                            last_agent_message='WATCH_REPLY ' + self.rid + '\nFictional answer.'))
                self.poll(); self.store.peers.reconcile([self.row])
                self.assertEqual(self.row['state'], 'answered')
                self.assertEqual(self.row['reply'], 'Fictional answer.')

    def test_extra_native_image_or_malformed_part_cannot_be_dropped_from_envelope(self):
        original = json.loads(json.dumps(self.row)); header = self.old.read_bytes()
        extras = [dict(type='input_image', image_url='fictional-image'), dict(type='image', source='fictional'),
                  dict(type='unknown', text=''), dict(type='input_text', text=123), 'invalid part', None]
        for kind in ('input_text', 'text'):
            for extra in extras:
                with self.subTest(kind=kind,extra=extra):
                    self.old.write_bytes(header); self.row = json.loads(json.dumps(original))
                    self.append(self.old, 'event_msg', dict(type='task_started', turn_id='mixed'))
                    self.append(self.old, 'response_item', dict(type='message', role='user',
                                content=[dict(type=kind,text=self.row['envelope']),extra],
                                internal_chat_message_metadata_passthrough=dict(turn_id='mixed')))
                    self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='mixed',
                                last_agent_message='WATCH_REPLY ' + self.rid + '\nPrivate combined answer.'))
                    self.poll(); self.store.peers.reconcile([self.row])
                    self.assertFalse(self.row.get('settled')); self.assertEqual(self.row['reply'], '')
                    self.assertEqual(self.row['capture_disposition'], 'withheld')
                    self.assertTrue(self.row.get('codex_gap_needs_envelope'))

    def test_unknown_content_field_is_not_assumed_to_be_text_metadata(self):
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='mixed'))
        self.append(self.old, 'response_item', dict(type='message', role='user',
                    content=[dict(type='input_text',text=self.row['envelope'],image_url='fictional-image')],
                    internal_chat_message_metadata_passthrough=dict(turn_id='mixed')))
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='mixed',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nMust stay withheld.'))
        self.poll(); self.assertFalse(self.row.get('settled')); self.assertEqual(self.row['reply'], '')
        # Only later pure input can restore execution evidence, never clean capture.
        self.message(self.old, 'mixed', self.row['envelope'])
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='mixed',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nStill withheld.'))
        self.poll(); self.store.peers.reconcile([self.row])
        self.assertTrue(self.row.get('settled')); self.assertEqual(self.row['reply'], '')
        self.assertEqual(self.row['capture_disposition'], 'withheld')

    def test_postauthorization_failure_only_releases_proven_no_native_submission(self):
        for failure in ('metadata', 'spawn', 'nonzero', 'timeout'):
            with self.subTest(failure=failure):
                row = dict(self.row, request_id=str(uuid.uuid4()), bridge_version=2, state='sending')
                self.store.db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)',
                    (str(uuid.uuid4()), row['agent'], '{}', 'a'*64, row['request_id'], str(uuid.uuid4()), row['created'], time.time(), 'Fictional'))
                self.store.db.commit()
                process = Mock(); process.wait.return_value = 1
                if failure == 'timeout': process.wait.side_effect = m.subprocess.TimeoutExpired('fixture', 10)
                with patch.object(self.store, '_metadata', side_effect=m.MessagingError(400, 'fixture')) if failure == 'metadata' else patch.object(self.store, '_metadata', wraps=self.store._metadata):
                    with patch.object(m.subprocess, 'Popen', side_effect=OSError('fixture')) if failure == 'spawn' else patch.object(m.subprocess, 'Popen', return_value=process):
                        row['state'], row['error'] = self.store._queue_codex(row, row['envelope'])
                self.store._save(row)
                state = peer_lifecycle.get(self.store.db, row['request_id'])
                self.assertEqual(state['execution'], 'dropped' if failure in ('metadata','spawn') else 'uncertain')


if __name__ == '__main__': unittest.main()
