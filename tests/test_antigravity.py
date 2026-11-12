#!/usr/bin/env python3
"""Fictional Antigravity files only; no installed app, messages, or provider calls."""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import antigravity
import officer_messaging
import server


class AntigravityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name))
        self.sid = str(uuid.uuid4())
        self.aid = 'a:' + self.sid
        self.root = self.home / '.gemini' / 'antigravity'
        self.path = self.transcript(self.sid)
        self.project = self.home / 'projects' / 'fictional-orbit'
        self.project.mkdir(parents=True)
        self.stamp = datetime.now(timezone.utc).replace(microsecond=0)
        self.saved = {}
        for name in ('AGENTS', 'FIRST_PROMPT', 'BACKFILLED', 'WORKED', 'ALLOWED_FILES', 'ALLOWED_DIRS'):
            self.saved[name] = copy.deepcopy(getattr(server, name))
            getattr(server, name).clear()
        for name in ('LOG', 'HISTORY_GAPS'):
            self.saved[name] = list(getattr(server, name))
            getattr(server, name).clear()
        for name in ('HOME', 'BASE', 'HISTORY', 'HISTORY_EPOCH', 'HISTORY_BASELINED',
                     'HISTORY_FENCES', 'HISTORY_GENERATIONS', 'PRIVATE_ROOTS'):
            self.saved[name] = getattr(server, name)
        original_home = server.HOME
        server.HOME = str(self.home)
        server.HISTORY = None
        server.HISTORY_BASELINED = False
        server.HISTORY_FENCES = {}
        server.HISTORY_GENERATIONS = {}
        # Preserve the real configured private-root list, relocating it to the
        # fixture home rather than supplying the protection under test ourselves.
        server.PRIVATE_ROOTS = tuple(p.replace(original_home, str(self.home), 1)
                                     for p in self.saved['PRIVATE_ROOTS'])
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for name, value in self.saved.items():
            current = getattr(server, name)
            if isinstance(value, dict) and isinstance(current, dict):
                current.clear(); current.update(value)
            elif isinstance(value, set) and isinstance(current, set):
                current.clear(); current.update(value)
            elif isinstance(value, list) and isinstance(current, list):
                current[:] = value
            else:
                setattr(server, name, value)
        self.tmp.cleanup()

    def transcript(self, sid, provider='antigravity'):
        p = self.home / '.gemini' / provider / 'brain' / sid / '.system_generated' / 'logs' / 'transcript.jsonl'
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        return p

    def record(self, index, kind='USER_INPUT', content='Inspect the fictional fixture.', **extra):
        row = dict(step_index=index, source='USER_EXPLICIT' if kind == 'USER_INPUT' else 'MODEL',
                   type=kind, status='DONE', created_at=(self.stamp + timedelta(seconds=index)).isoformat(),
                   content=content)
        row.update(extra)
        return row

    def append(self, *rows, path=None):
        with (path or self.path).open('ab') as f:
            for row in rows:
                f.write(json.dumps(row).encode('utf-8') + b'\n')

    def events(self):
        return [e for e in server.LOG if e.get('agent') == self.aid]

    def metadata_db(self, *, title='Orbit', workspace=None, parent='', wal=False):
        self.root.mkdir(parents=True, exist_ok=True)
        db = self.root / 'conversation_summaries.db'
        conn = sqlite3.connect(str(db))
        if wal:
            self.assertEqual(conn.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
            conn.execute('PRAGMA wal_autocheckpoint=0')
        conn.execute('''CREATE TABLE conversation_summaries (
            conversation_id TEXT PRIMARY KEY, title TEXT, preview TEXT,
            step_count INTEGER, last_modified_time TEXT, workspace_uris TEXT,
            status TEXT, source TEXT, project_id TEXT, agent_name TEXT,
            parent_conversation_id TEXT, nesting_depth INTEGER, battle_id TEXT,
            winning_conversation_id TEXT, not_fully_idle NUMERIC, killed NUMERIC,
            last_user_input_time TEXT, last_user_input_step_index INTEGER,
            app_data_dir TEXT, raw_summary BLOB, group_id TEXT)''')
        conn.execute('''INSERT INTO conversation_summaries
            (conversation_id,title,workspace_uris,parent_conversation_id,status,not_fully_idle,
             raw_summary,preview,project_id,agent_name,nesting_depth,killed)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
             (self.sid, title, json.dumps([workspace or self.project.as_uri()]), parent,
              'CASCADE_RUN_STATUS_RUNNING', 1, b'EXCLUDED_RAW_SUMMARY',
              'EXCLUDED_PREVIEW', 'fictional-project-id', '', 0, 0))
        conn.commit()
        return conn

    def test_roots_are_only_fixed_provider_directories(self):
        self.assertEqual(set(map(Path, antigravity.roots(str(self.home)))),
                         {self.home / '.gemini' / p for p in
                          ('antigravity', 'antigravity-cli', 'antigravity-ide')})

    def test_uuid_identity_and_inventory_use_actual_conversation_folder(self):
        ident = antigravity.identity(str(self.path), str(self.home))
        self.assertIsNotNone(ident)
        self.assertEqual(ident[0], self.sid)
        self.assertEqual(Path(ident[1]), self.root)
        self.assertEqual(server.aid_of(str(self.path)), self.aid)
        self.assertIn(str(self.path), antigravity.inventory(str(self.home)))
        self.assertTrue(any(Path(path) == self.path and cls is server.AntigravityTail
                            for path, cls in server.native_sources()))

    def test_unknown_root_non_uuid_and_wrong_file_are_not_sources(self):
        bad = self.transcript('not-a-conversation')
        neighbor = self.transcript(str(uuid.uuid4()), provider='unrelated-app')
        wrong_file = self.path.with_name('other.jsonl'); wrong_file.touch()
        for p in (bad, neighbor, wrong_file):
            self.assertIsNone(antigravity.identity(str(p), str(self.home)))
            self.assertNotIn(str(p), antigravity.inventory(str(self.home)))

    def test_duplicate_uuid_in_multiple_roots_is_not_silently_selected(self):
        twin = self.transcript(self.sid, provider='antigravity-cli')
        found = antigravity.inventory(str(self.home))
        self.assertNotIn(str(self.path), found)
        self.assertNotIn(str(twin), found)

    def test_metadata_title_and_local_workspace_reach_real_agent(self):
        self.metadata_db().close()
        self.append(self.record(0))
        tail = server.AntigravityTail(str(self.path)); tail.poll()
        a = server.AGENTS[self.aid]
        self.assertEqual(a['runtime'], 'Antigravity')
        self.assertEqual(a['name'], 'Orbit')
        self.assertEqual(os.path.normcase(os.path.normpath(a['cwd'])),
                         os.path.normcase(str(self.project)))
        exposed = json.dumps([a, self.events()])
        self.assertNotIn('EXCLUDED_RAW_SUMMARY', exposed)
        self.assertNotIn('EXCLUDED_PREVIEW', exposed)

    def test_first_metadata_read_does_not_assume_monotonic_clock_starts_above_zero(self):
        self.metadata_db().close()
        with patch.object(server.time, 'monotonic', return_value=0) as clock, \
                patch.object(antigravity, 'metadata', wraps=antigravity.metadata) as read:
            tail = server.AntigravityTail(str(self.path))
            self.assertEqual(server.AGENTS[self.aid]['name'], 'Orbit')
            self.assertEqual(read.call_count, 1)
            tail.refresh_metadata()
            clock.return_value = 2.99
            tail.refresh_metadata()
            self.assertEqual(read.call_count, 1)
            clock.return_value = 3
            tail.refresh_metadata()
            self.assertEqual(read.call_count, 2)

    def test_metadata_reader_sees_committed_live_wal_rows(self):
        conn = self.metadata_db(title='Fresh WAL title', wal=True)
        try:
            self.assertTrue(Path(str(self.root / 'conversation_summaries.db') + '-wal').exists())
            self.assertEqual(antigravity.metadata(str(self.path), str(self.home)).get('name'),
                             'Fresh WAL title')
        finally:
            conn.close()

    def test_metadata_idle_requires_consistent_native_status_and_flag(self):
        conn = self.metadata_db()
        try:
            cases = [('CASCADE_RUN_STATUS_IDLE', 0, True),
                     ('CASCADE_RUN_STATUS_RUNNING', 1, False),
                     ('CASCADE_RUN_STATUS_IDLE', 1, None),
                     ('CASCADE_RUN_STATUS_RUNNING', 0, None),
                     ('UNKNOWN_STATUS', 0, None),
                     ('CASCADE_RUN_STATUS_IDLE', 0.5, None),
                     ('CASCADE_RUN_STATUS_IDLE', 'unconfirmed', None),
                     ('CASCADE_RUN_STATUS_IDLE', None, None)]
            for status, flag, expected in cases:
                with self.subTest(status=status, flag=flag):
                    conn.execute('UPDATE conversation_summaries SET status=?,not_fully_idle=?', (status, flag))
                    conn.commit()
                    self.assertIs(antigravity.metadata(str(self.path), str(self.home)).get('idle'), expected)
        finally:
            conn.close()

    def test_metadata_parent_is_the_recorded_uuid_not_workspace_association(self):
        parent = str(uuid.uuid4())
        conn = self.metadata_db(parent=parent)
        try:
            self.assertEqual(antigravity.metadata(str(self.path), str(self.home)).get('parent'), 'a:' + parent)
            conn.execute('UPDATE conversation_summaries SET parent_conversation_id=?', (self.sid,))
            conn.commit()
            self.assertNotIn('parent', antigravity.metadata(str(self.path), str(self.home)))
        finally:
            conn.close()

    def test_native_metadata_reactivation_does_not_leave_station_ended(self):
        conn = self.metadata_db()
        try:
            conn.execute("UPDATE conversation_summaries SET status='CASCADE_RUN_STATUS_IDLE',not_fully_idle=0")
            conn.commit()
            tail = server.AntigravityTail(str(self.path)); tail.poll()
            self.assertTrue(any(e['kind'] == 'turn_end' for e in self.events()))
            before = len(server.LOG)
            conn.execute("UPDATE conversation_summaries SET status='CASCADE_RUN_STATUS_RUNNING',not_fully_idle=1")
            conn.commit()
            tail.metadata_at = None
            tail.refresh_metadata(False)
            later = [e for e in server.LOG[before:] if e.get('agent') == self.aid]
            self.assertTrue(any(e['kind'] == 'beat' for e in later))
            self.assertFalse(any(e['kind'] == 'turn_end' for e in later))
        finally:
            conn.close()

    def test_fresh_native_continuation_is_ended_by_current_idle_metadata(self):
        conn = self.metadata_db()
        try:
            conn.execute("UPDATE conversation_summaries SET status='CASCADE_RUN_STATUS_IDLE',not_fully_idle=0")
            conn.commit()
            base = self.stamp.timestamp()
            with patch.object(server.time, 'time', return_value=base):
                tail = server.AntigravityTail(str(self.path)); tail.poll()
            initial_end = [e for e in self.events() if e['kind'] == 'turn_end'][-1]
            # A fresh native continuation can arrive between metadata samples.
            # Its timestamp is after the last idle observation, so the actual
            # page's historical-event guard admits this beat as active work.
            self.append(self.record(0, 'PLANNER_RESPONSE', 'A fresh fictional continuation.',
                                    created_at=datetime.fromtimestamp(base + 1, timezone.utc).isoformat()))
            with patch.object(server.time, 'time', return_value=base + 1.1):
                tail.poll()
            visible_beat = [e for e in self.events() if e['kind'] == 'beat'][-1]
            self.assertGreater(visible_beat['ts'], initial_end['ts'])
            before_refresh = len(self.events())
            tail.metadata_at = None
            with patch.object(server.time, 'time', return_value=base + 2):
                tail.refresh_metadata(False)
            new_ends = [e for e in self.events()[before_refresh:] if e['kind'] == 'turn_end']
            self.assertTrue(new_ends, 'Fresh native idle must end the visibly active continuation.')
            self.assertGreaterEqual(new_ends[-1]['ts'], visible_beat['ts'])
            # An ignored system record does not reopen the station or trigger
            # another idle end simply by passing through the parser.
            before_system = len(self.events())
            self.append(self.record(1, 'SYSTEM_MESSAGE', 'Ignored fictional system text.', source='SYSTEM',
                                    created_at=datetime.fromtimestamp(base + 3, timezone.utc).isoformat()))
            with patch.object(server.time, 'time', return_value=base + 3.1):
                tail.poll()
            tail.metadata_at = None
            with patch.object(server.time, 'time', return_value=base + 3.2):
                tail.refresh_metadata(False)
            self.assertFalse(any(e['kind'] in ('beat', 'turn_end') for e in self.events()[before_system:]))
        finally:
            conn.close()

    def test_nonlocal_or_nonfile_workspace_uri_never_becomes_cwd(self):
        for uri in ('https://example.invalid/project', 'file://remote.invalid/share/project',
                    'file:///fictional/project?token=fixture', 'file:///fictional/project#fragment'):
            with self.subTest(uri=uri):
                db = self.root / 'conversation_summaries.db'
                if db.exists(): db.unlink()
                self.metadata_db(workspace=uri).close()
                self.assertFalse(antigravity.metadata(str(self.path), str(self.home)).get('cwd'))

    def test_missing_and_malformed_metadata_do_not_break_transcript(self):
        self.assertEqual(antigravity.metadata(str(self.path), str(self.home)), {})
        (self.root / 'conversation_summaries.db').write_bytes(b'not a sqlite database')
        self.assertEqual(antigravity.metadata(str(self.path), str(self.home)), {})
        self.append(self.record(0, content='A transcript survives unavailable metadata.'))
        server.AntigravityTail(str(self.path)).poll()
        self.assertTrue(any(e.get('text') == 'A transcript survives unavailable metadata.' for e in self.events()))

    def test_own_aware_created_at_controls_admission(self):
        self.append(self.record(0))
        observed = server.last_record_ts(str(self.path), timestamp_key='created_at')
        self.assertEqual(observed, self.stamp.timestamp())
        self.assertTrue(server.source_active(str(self.path), time.time(), server.AntigravityTail))
        self.assertIsNone(server.last_record_ts(str(self.path)))

    def test_nested_recent_timestamp_cannot_admit_stale_record(self):
        stale = (self.stamp - timedelta(days=20)).isoformat()
        row = self.record(0, created_at=stale, content='A stale fictional record.',
                          payload={'created_at': self.stamp.isoformat(), 'timestamp': self.stamp.isoformat()})
        self.append(row)
        os.utime(self.path, None)
        self.assertFalse(server.source_active(str(self.path), time.time(), server.AntigravityTail))

    def test_missing_or_naive_created_at_is_not_activity(self):
        for value in (None, 'not a timestamp', self.stamp.replace(tzinfo=None).isoformat(), 123, True):
            with self.subTest(value=value):
                self.path.write_bytes(b'')
                self.append(self.record(0, created_at=value, payload={'created_at': self.stamp.isoformat()}))
                self.assertIsNone(server.last_record_ts(str(self.path), timestamp_key='created_at'))
                self.assertFalse(server.source_active(str(self.path), time.time(), server.AntigravityTail))

    def test_real_parser_emits_user_reply_and_tool_requests(self):
        self.append(self.record(0, content='Check the fictional files.'),
                    self.record(1, 'PLANNER_RESPONSE', 'I will inspect them.', tool_calls=[
                        {'name': 'read_file', 'args': {'Path': str(self.project / 'example.py')}},
                        {'name': 'run_command', 'args': {'CommandLine': 'git push origin main'}}]),
                    self.record(2, 'GENERIC', 'The native log supplied another step.'))
        server.AntigravityTail(str(self.path)).poll()
        events = self.events()
        self.assertTrue(any(e['kind'] == 'user' and e['text'] == 'Check the fictional files.' for e in events))
        self.assertTrue(any(e['kind'] == 'say' and e['text'] == 'I will inspect them.' for e in events))
        self.assertTrue(any(e['kind'] == 'read' and e.get('path') == str(self.project / 'example.py') for e in events))
        self.assertTrue(any(e['kind'] == 'run' and e.get('cmd') == 'git push origin main' and e.get('alert') == 'push' for e in events))
        operations = [e for e in events if e['kind'] in ('read', 'run')]
        self.assertEqual(len({e['id'] for e in operations}), 2)

    def test_done_step_is_never_whole_turn_completion_or_tool_success(self):
        self.append(self.record(0), self.record(1, 'PLANNER_RESPONSE', 'Starting a check.',
                    tool_calls=[{'name': 'run_command', 'args': {'CommandLine': 'python -m unittest'}}]),
                    self.record(2, 'GENERIC', 'A step is recorded.', status='DONE'),
                    self.record(3, 'PLANNER_RESPONSE', 'A later reply.', status='DONE'))
        server.AntigravityTail(str(self.path)).poll()
        events = self.events()
        self.assertFalse(any(e['kind'] == 'turn_end' for e in events))
        self.assertFalse(any((e.get('done') and not e.get('unknown')) or e.get('code') == 0 for e in events))
        results = [e for e in events if e['kind'] == 'result']
        self.assertTrue(results, 'Advancing native steps must retire unmatched requests as unknown.')
        self.assertTrue(all(e.get('unknown') for e in results))

    def test_thinking_and_unverified_model_or_context_are_not_published(self):
        self.append(self.record(0), self.record(1, 'PLANNER_RESPONSE', 'A public reply.',
                    thinking='THINKING_SHOULD_NOT_BE_PUBLISHED', model='unverified-model',
                    input_tokens=100, cache_read_tokens=50, output_tokens=20, context_window=99999))
        server.AntigravityTail(str(self.path)).poll()
        a = server.AGENTS[self.aid]
        self.assertFalse(a.get('model'))
        self.assertFalse(a.get('ctxmax'))
        self.assertFalse(a.get('ctx'))
        self.assertNotIn('THINKING_SHOULD_NOT_BE_PUBLISHED', json.dumps(self.events()))

    def test_native_user_envelope_preserves_exact_owner_text_and_discards_runtime_sections(self):
        user = '  First line.\nUse <aside class="note">literal markup</aside> & keep spacing.  '
        envelope = ('<USER_REQUEST>' + user + '</USER_REQUEST>\n'
                    '<ADDITIONAL_METADATA>NATIVE_METADATA_NOT_FOR_WATCH</ADDITIONAL_METADATA>\n'
                    '<USER_SETTINGS_CHANGE>NATIVE_SETTINGS_NOT_FOR_WATCH</USER_SETTINGS_CHANGE>')
        self.assertEqual(antigravity.user_text(envelope), user)
        self.append(self.record(0, content=envelope))
        server.AntigravityTail(str(self.path)).poll()
        self.assertEqual([e['text'] for e in self.events() if e['kind'] == 'user'], [user])
        self.assertEqual(server.AGENTS[self.aid]['goal'], ' '.join(user.split()))
        exposed = json.dumps([self.events(), server.AGENTS[self.aid]])
        for marker in ('NATIVE_METADATA_NOT_FOR_WATCH', 'NATIVE_SETTINGS_NOT_FOR_WATCH',
                       '<ADDITIONAL_METADATA>', '<USER_SETTINGS_CHANGE>'):
            self.assertNotIn(marker, exposed)

    def test_plain_user_markup_and_literal_closing_tag_remain_verbatim(self):
        plain = 'Compare <section>one</section> with <span>two</span>.\nKeep these examples.'
        self.assertEqual(antigravity.user_text(plain), plain)
        literal = 'Explain the literal token </USER_REQUEST> and keep this newline.\nSecond line.'
        wrapped = ('<USER_REQUEST>' + literal + '</USER_REQUEST>'
                   '<ADDITIONAL_METADATA>Excluded fictional runtime text.</ADDITIONAL_METADATA>')
        self.assertEqual(antigravity.user_text(wrapped), literal)
        self.append(self.record(0, content=plain), self.record(1, content=wrapped))
        server.AntigravityTail(str(self.path)).poll()
        self.assertEqual([e['text'] for e in self.events() if e['kind'] == 'user'], [plain, literal])

    def test_malformed_or_ambiguous_native_envelope_never_becomes_user_text(self):
        malformed = [
            '<USER_REQUEST>Missing close<ADDITIONAL_METADATA>Runtime section.</ADDITIONAL_METADATA>',
            '<USER_REQUEST>Owner text.</USER_REQUEST><ADDITIONAL_METADATA>Unclosed runtime section.',
            '<USER_REQUEST>Owner text.</USER_REQUEST><ADDITIONAL_METADATA>Runtime.</ADDITIONAL_METADATA>unexpected trailing data',
            '<USER_REQUEST>Owner text.</USER_REQUEST><ADDITIONAL_METADATA>Runtime.</ADDITIONAL_METADATA><USER_SETTINGS_CHANGE>Unclosed settings.',
            '<USER_REQUEST>Owner text.</USER_REQUEST><ADDITIONAL_METADATA>First possible suffix.</USER_REQUEST><ADDITIONAL_METADATA>Second possible suffix.</ADDITIONAL_METADATA>',
        ]
        for index, value in enumerate(malformed):
            with self.subTest(case=index):
                self.assertIsNone(antigravity.user_text(value))
                self.append(self.record(index, content=value))
        server.AntigravityTail(str(self.path)).poll()
        self.assertFalse(any(e['kind'] in ('user', 'turn_start') for e in self.events()))
        self.assertFalse(server.AGENTS[self.aid].get('goal'))

    def test_native_envelope_is_clean_before_officer_reads_or_future_memory_capture(self):
        from deck_log import DeckLog
        user = 'Summarize the fictional fixture in one sentence.'
        wrapped = ('<USER_REQUEST>' + user + '</USER_REQUEST>'
                   '<ADDITIONAL_METADATA>EXCLUDED_RUNTIME_METADATA</ADDITIONAL_METADATA>'
                   '<USER_SETTINGS_CHANGE>EXCLUDED_RUNTIME_SETTINGS</USER_SETTINGS_CHANGE>')
        history = DeckLog(str(self.home / 'private-envelope-memory'))
        server.HISTORY = history
        try:
            self.assertTrue(history.available, history.reason)
            history.configure(enabled=True)
            server.history_arm()
            self.append(self.record(0, content=wrapped))
            server.AntigravityTail(str(self.path)).poll()
            self.assertTrue(history.flush(10), history.reason)
            saved = history.search(limit=100)['events']
            self.assertEqual([e['text'] for e in saved if e['kind'] == 'message'], [user])
            timeline = server.officer_tool('agent_timeline', {'agent': self.aid}, [])
            exposed = json.dumps([saved, timeline, self.events(), server.AGENTS[self.aid]])
            self.assertIn(user, exposed)
            self.assertNotIn('EXCLUDED_RUNTIME_METADATA', exposed)
            self.assertNotIn('EXCLUDED_RUNTIME_SETTINGS', exposed)
            self.assertNotIn('<USER_REQUEST>', exposed)
        finally:
            history.close()

    def test_partial_append_and_repoll_emit_one_message(self):
        tail = server.AntigravityTail(str(self.path))
        raw = json.dumps(self.record(0, content='Only one completed record.')).encode()
        self.path.write_bytes(raw[:20]); tail.poll()
        self.assertFalse(any(e['kind'] == 'user' for e in self.events()))
        with self.path.open('ab') as f: f.write(raw[20:] + b'\n')
        tail.poll(); tail.poll()
        self.assertEqual([e['text'] for e in self.events() if e['kind'] == 'user'],
                         ['Only one completed record.'])

    def test_incremental_reads_keep_stable_agent_identity(self):
        self.append(self.record(0, content='First fictional request.'))
        tail = server.AntigravityTail(str(self.path)); tail.poll()
        self.append(self.record(1, 'PLANNER_RESPONSE', 'A later fictional reply.'))
        tail.poll(); tail.poll()
        self.assertEqual(tail.aid, self.aid)
        self.assertEqual(sum(e['kind'] == 'user' for e in self.events()), 1)
        self.assertEqual(sum(e['kind'] == 'say' and e.get('text') == 'A later fictional reply.' for e in self.events()), 1)

    def test_poll_respects_existing_bounded_read_budget(self):
        self.append(self.record(0, content='A bounded fictional message. ' * 30))
        tail = server.AntigravityTail(str(self.path))
        with patch.object(server, 'MAX_POLL', 64):
            tail.poll()
            self.assertLessEqual(tail.off, 64)
            self.assertFalse(any(e['kind'] == 'user' for e in self.events()))
            for _ in range(100):
                if tail.off >= self.path.stat().st_size:
                    break
                tail.poll()
        self.assertEqual(sum(e['kind'] == 'user' for e in self.events()), 1)

    def test_quoted_or_user_supplied_tool_payload_is_not_a_tool_request(self):
        quoted = {'name': 'run_command', 'args': {'CommandLine': 'git push origin main'}}
        self.append(self.record(0, content=json.dumps(quoted), tool_calls=[quoted]),
                    self.record(1, 'PLANNER_RESPONSE', content=json.dumps(quoted)),
                    self.record(2, 'SYSTEM_MESSAGE', content='SYSTEM_TEXT_NOT_FOR_MONITOR', source='SYSTEM'))
        server.AntigravityTail(str(self.path)).poll()
        self.assertFalse(any(e['kind'] == 'run' or e.get('alert') == 'push' for e in self.events()))
        self.assertNotIn('SYSTEM_TEXT_NOT_FOR_MONITOR', json.dumps(self.events()))

    @unittest.skipIf(os.name == 'nt', 'Creating directory symlinks requires optional Windows privileges.')
    def test_symlink_escape_cannot_supply_transcript_or_metadata(self):
        other = self.home / 'elsewhere'
        other.mkdir()
        real = other / 'transcript.jsonl'
        real.write_text(json.dumps(self.record(0)) + '\n')
        self.path.unlink()
        self.path.symlink_to(real)
        self.assertIsNone(antigravity.identity(str(self.path), str(self.home)))
        self.assertNotIn(str(self.path), antigravity.inventory(str(self.home)))
        self.assertEqual(antigravity.metadata(str(self.path), str(self.home)), {})

    def test_officer_memory_records_only_future_antigravity_observations(self):
        from deck_log import DeckLog
        self.append(self.record(0, content='Earlier fictional message stays outside the archive.'))
        history = DeckLog(str(self.home / 'private-memory'))
        server.HISTORY = history
        try:
            self.assertTrue(history.available, history.reason)
            history.configure(enabled=True)
            server.history_arm()
            self.append(self.record(1, 'PLANNER_RESPONSE', 'Future fictional reply enters the archive.'))
            server.AntigravityTail(str(self.path)).poll()
            self.assertTrue(history.flush(10), history.reason)
            saved = history.search(limit=100)['events']
            messages = [e for e in saved if e['kind'] == 'message']
            self.assertEqual([e['text'] for e in messages], ['Future fictional reply enters the archive.'])
            self.assertTrue(all(e['provider'] == 'antigravity' and e['chat_id'] == self.aid for e in saved))
            self.assertTrue(any(e.get('text') == 'Earlier fictional message stays outside the archive.' for e in self.events()))
        finally:
            history.close()

    def test_truncation_marks_old_reader_for_replacement(self):
        self.append(self.record(0, content='Original content long enough to detect truncation.'))
        tail = server.AntigravityTail(str(self.path)); tail.poll()
        self.path.write_bytes(b'')
        tail.poll()
        self.assertTrue(tail.replaced)
        self.append(self.record(1, content='After truncation.'))
        rebuilt = server.AntigravityTail(str(self.path)); rebuilt.poll()
        self.assertEqual(rebuilt.aid, self.aid)
        self.assertTrue(any(e.get('text') == 'After truncation.' for e in self.events()))

    def test_atomic_file_rotation_marks_old_reader_for_replacement(self):
        self.append(self.record(0))
        tail = server.AntigravityTail(str(self.path)); tail.poll()
        replacement = self.path.with_suffix('.replacement')
        replacement.write_text(json.dumps(self.record(1, content='Replacement source.')) + '\n')
        os.replace(str(replacement), str(self.path))
        tail.poll()
        self.assertTrue(tail.replaced)
        rebuilt = server.AntigravityTail(str(self.path)); rebuilt.poll()
        self.assertEqual(rebuilt.aid, self.aid)
        self.assertTrue(any(e.get('text') == 'Replacement source.' for e in self.events()))

    def test_malformed_records_do_not_hide_later_valid_record(self):
        self.path.write_bytes(b'not JSON\n[]\nnull\n')
        self.append(self.record(0, content='Valid after malformed records.'),
                    self.record(1, 'PLANNER_RESPONSE', content=None, tool_calls=[None, 5, {'name': [], 'args': []}]))
        server.AntigravityTail(str(self.path)).poll()
        self.assertTrue(any(e.get('text') == 'Valid after malformed records.' for e in self.events()))

    def test_gemini_private_state_cannot_be_granted_as_html_preview(self):
        private = self.root / 'private-example.html'
        private.write_text('<p>A fictional private file.</p>')
        server.allow_preview(self.aid, False, 0, 'fixture', str(private))
        self.assertNotIn(str(private), server.ALLOWED_FILES)
        self.assertFalse(server.ALLOWED_DIRS)

    def test_open_has_no_claude_or_other_native_fallback(self):
        self.append(self.record(0)); server.AntigravityTail(str(self.path)).poll()
        class Reply:
            def send(self, status, body, *args): self.got = status, body
        reply = Reply()
        with patch.object(server, 'launch', side_effect=AssertionError('Must not open an unrelated provider.')), \
             patch.object(server, 'claude_desktop_id', side_effect=AssertionError('Must not resolve Claude.')):
            server.H.open_session(reply, self.aid)
        self.assertEqual(reply.got[0], 404)
        self.assertIn('Antigravity', reply.got[1].decode())

    def test_native_actions_remain_unavailable(self):
        self.append(self.record(0)); server.AntigravityTail(str(self.path)).poll()
        store = officer_messaging.MessagingStore.__new__(officer_messaging.MessagingStore)
        out = store.owner_capabilities(self.aid, server.AGENTS)['actions']
        self.assertFalse(out['queue']['available'])
        self.assertFalse(out['steer']['supported'])
        self.assertFalse(out['interrupt']['supported'])
        self.assertFalse(out['promote_queued']['available'])


if __name__ == '__main__':
    # Existing CI entry covers navigation too; discovery imports only this file's
    # own tests, so the other suites are never repeated under unittest discovery.
    names = ('__main__', 'test_antigravity_navigation',
             'test_install_antigravity_open', 'test_antigravity_open')
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(not result.wasSuccessful())
