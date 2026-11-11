#!/usr/bin/env python3
"""Synthetic private archive tests; never opens native conversations or user state."""
import importlib.util
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))
import deck_log as d


class DeckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = os.path.join(os.path.realpath(self.temp.name), 'state')
        self.now = 1800000000.0
        self.store = d.DeckLog(self.root, clock=lambda: self.now)
        self.assertTrue(self.store.available, self.store.reason)
        self.epoch = self.store.configure(enabled=True)['capture_epoch']
        self.store.activate(self.epoch)
        self.number = 0

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def record(self, *, chat='x:chat-one', provider='codex', project=None, ts=None, key=None):
        self.number += 1
        return dict(key=key or self.store.opaque_identity('record', self.number),
                    stream=self.store.opaque_identity('stream', 'fictional-log'), provider=provider,
                    chat_id=chat, project_id=project or self.store.opaque_identity('project', 'Orchard'),
                    source_ts=self.now if ts is None else ts, time_basis='source')

    def prose(self, text='A useful update', slot='0', **extra):
        return dict(kind='message', role='assistant', text=text, chat_name='Harbor', project_name='Orchard', source={'slot': slot}, **extra)

    def offer(self, record=None, candidates=None):
        result = self.store.offer_record(record or self.record(), [self.prose()] if candidates is None else candidates, capture_epoch=self.epoch)
        self.assertEqual(result['status'], 'queued', result)
        self.assertTrue(self.store.flush())
        return result

    def test_default_missing_archive_is_read_only_and_lazy(self):
        root = os.path.join(os.path.realpath(self.temp.name), 'never-enabled')
        fresh = d.DeckLog(root)
        try:
            self.assertTrue(fresh.status()['available'])
            self.assertFalse(fresh.status()['enabled'])
            self.assertFalse(fresh.search()['events'])
            fresh.opaque_identity('project', 'Fictional')
            self.assertFalse(os.path.exists(root), 'viewing disabled status must not create archive state')
            fresh.configure(retention_days=14)
            self.assertTrue(os.path.isfile(fresh.path), 'explicit owner settings action may initialize state')
            self.assertFalse(fresh.status()['recording'])
        finally:
            fresh.close()

    def test_offers_queue_while_writer_is_busy(self):
        original = self.store._write
        def delayed(*args):
            time.sleep(0.01)
            return original(*args)
        with patch.object(self.store, '_write', delayed):
            results = []
            for n in range(60):
                results.append(self.store.offer_record(self.record(), [self.prose('burst %d' % n)], capture_epoch=self.epoch))
                time.sleep(0.001)
            self.assertTrue(self.store.flush(3))
        self.assertEqual([r['status'] for r in results], ['queued'] * 60)
        self.assertEqual(len(self.store.search(limit=100)['events']), 60)
        self.assertEqual(self.store.status()['dropped'], 0)

    def test_inflight_stale_generation_cannot_cross_control_barrier(self):
        with self.store.lock:
            self.store.offer_record(self.record(), [self.prose('stale inflight')], capture_epoch=self.epoch)
            deadline = time.monotonic() + 1
            while not self.store.inflight and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertEqual(self.store.inflight, 1)
            self.epoch = self.store.configure(enabled=True)['capture_epoch']
            self.store.activate(self.epoch)
        self.assertTrue(self.store.flush())
        self.assertFalse(self.store.search()['events'])

    def test_fts_cleanup_also_runs_when_search_falls_back(self):
        self.offer(candidates=[self.prose('fallbackeraseword')])
        self.store.fts = False
        self.assertTrue(self.store.delete(all=True)['ok'])
        self.assertNotIn(b'fallbackeraseword', pathlib.Path(self.store.path).read_bytes())

    def test_delete_cannot_claim_cleanup_success_on_compaction_failure(self):
        self.offer()
        with patch.object(self.store, '_cleanup', side_effect=sqlite3.OperationalError('synthetic')):
            result = self.store.delete(all=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'deletion_cleanup_failed')
        self.assertFalse(self.store.status()['recording'])

    def test_default_off_arming_restart_and_durable_identity(self):
        self.offer()
        event = self.store.search()['events'][0]
        identity = self.store.opaque_identity('project', 'Orchard')
        self.store.close()
        self.store = d.DeckLog(self.root, clock=lambda: self.now)
        state = self.store.status()
        self.assertFalse(state['recording'])
        self.assertEqual(state['state'], 'arming')
        self.assertEqual(self.store.get_event(event['id'])['citation'], event['citation'])
        self.assertEqual(self.store.opaque_identity('project', 'Orchard'), identity)
        self.assertEqual(self.store.offer_record(self.record(), [self.prose()], capture_epoch=self.epoch)['status'], 'ignored')
        other_root = os.path.join(os.path.realpath(self.temp.name), 'other')
        other = d.DeckLog(other_root)
        self.assertFalse(other.status()['enabled'])
        other.close()

    def test_f1_whole_record_dedupe_and_empty_receipt(self):
        raw = self.record()
        self.offer(raw, [self.prose('first', '0'), self.prose('owner question', '1')])
        # Recreated Tail suppresses prior metadata; the same user moves to slot zero.
        self.offer(raw, [self.prose('owner question', '0')])
        self.assertEqual(len(self.store.search()['events']), 2)
        self.assertEqual(len(self.store.search(query='question')['events']), 1)
        empty = self.record()
        self.offer(empty, [])
        self.offer(empty, [self.prose('must not appear')])
        self.assertFalse(self.store.search(query='must not appear')['events'])
        self.offer(self.record(), [self.prose('owner question')])
        self.assertEqual(len(self.store.search(query='question')['events']), 2, 'same text from another raw record survives')

    def test_source_identity_typed_not_concatenated_or_epoch(self):
        self.assertNotEqual(self.store.opaque_identity('record', 'ab', 'c'), self.store.opaque_identity('record', 'a', 'bc'))
        self.assertNotEqual(self.store.opaque_identity('record', 1), self.store.opaque_identity('record', '1'))
        first = self.store.opaque_identity('record', 'stream', 2, 4, b'digest')
        self.store.configure(paused=True)
        self.assertEqual(first, self.store.opaque_identity('record', 'stream', 2, 4, b'digest'))

    def test_privacy_projection_and_strict_whole_batch_validation(self):
        token = 'sk' + '-ant-' + 'a' * 32
        self.offer(candidates=[self.prose('credential ' + token)])
        self.offer(candidates=[self.prose('{\"access_token\": \"' + token + '\"}')])
        self.offer(candidates=[self.prose('open https://private-user@example.test/path')])
        self.offer(candidates=[self.prose('Before `secret code` after\n```python\nprivate_code()\n```\n/home/person/private/file /etc/private-config')])
        operation = dict(kind='run', operation='run', phase='started', outcome='pending', text=token, file_ref='../private', source={'slot': '0'})
        self.offer(candidates=[operation])
        events = self.store.search()['events']
        serialized = json.dumps(events)
        for forbidden in (token, 'private-user', 'private_code()', 'secret code', '/home/person', '/etc/private-config', '../private'):
            self.assertNotIn(forbidden, serialized)
        self.assertTrue(any(e['redacted'] for e in events))
        self.assertEqual(next(e for e in events if e['kind'] == 'run')['text'], '')
        valid, invalid = self.prose('valid'), dict(self.prose('bad', '1'), cmd='private command')
        result = self.store.offer_record(self.record(), [valid, invalid], capture_epoch=self.epoch)
        self.assertEqual(result['status'], 'dropped')
        self.assertFalse(self.store.search(query='valid')['events'])
        self.store.close()
        self.store = d.DeckLog(self.root, clock=lambda: self.now)
        data = pathlib.Path(self.store.path).read_bytes()
        self.assertNotIn(token.encode(), data)
        self.assertNotIn(b'private_code()', data)

    def test_preview_capabilities_do_not_survive_prose_metadata_or_fts(self):
        capability = 'fixturePreview' + 'Capability123'
        links = [
            'http://127.0.0.1:8793/fs/' + capability + '/fictional.html',
            'http://localhost:8793/fs/' + capability + '/fictional.html',
            'http://[::1]:8793/fs/' + capability + '/fictional.html',
            '/fs/' + capability + '/fictional.html',
            'fs/' + capability + '/fictional.html',
        ]
        for link in links:
            self.offer(candidates=[dict(self.prose('Open ' + link), chat_name=link, project_name=link, file_ref=link)])
        events = self.store.search()['events']
        self.assertEqual(len(events), len(links))
        self.assertNotIn(capability, json.dumps(events))
        self.assertFalse(self.store.search(query=capability)['events'])
        day = time.strftime('%Y-%m-%d', time.localtime(self.now))
        self.assertNotIn(capability, json.dumps(self.store.session_day_summary(day)))
        if self.store.fts:
            self.assertNotIn(capability, json.dumps([tuple(r) for r in self.store.db.execute('SELECT * FROM event_fts')]))
        self.store.close()
        self.assertNotIn(capability.encode(), pathlib.Path(self.store.path).read_bytes())
        self.store = d.DeckLog(self.root, clock=lambda: self.now)
        self.assertNotIn(capability, json.dumps(self.store.search()['events']))

    def test_malformed_unicode_and_oversized_projection_cannot_kill_writer(self):
        self.offer(candidates=[dict(self.prose('Visible ' + chr(0xD800)), chat_name=chr(0xD800))])
        self.assertEqual(len(self.store.search()['events']), 1)
        bad = self.prose('x' * 20000)
        self.assertEqual(self.store.offer_record(self.record(), [bad], capture_epoch=self.epoch)['status'], 'dropped')
        self.offer(candidates=[self.prose('still running')])
        self.assertEqual(len(self.store.search(query='still running')['events']), 1)

    def test_atomic_size_and_event_quota(self):
        self.store.limits['projections'] = 1
        result = self.store.offer_record(self.record(), [self.prose('one'), self.prose('two', '1')], capture_epoch=self.epoch)
        self.assertEqual(result['status'], 'dropped')
        self.assertFalse(self.store.search()['events'])
        self.store.limits['events'] = 1
        self.offer()
        result = self.store.offer_record(self.record(), [self.prose('quota')], capture_epoch=self.epoch)
        self.assertEqual(result['status'], 'queued')
        self.store.flush()
        self.assertFalse(self.store.status()['recording'])
        self.assertEqual(len(self.store.search()['events']), 1)
        self.assertEqual(self.store.status()['reason'], 'storage_full_or_unavailable')

    def test_queue_bound_and_policy_generation(self):
        self.store.limits['queue'] = 1
        with self.store.wake:
            first = self.store.offer_record(self.record(), [self.prose('old queued')], capture_epoch=self.epoch)
            second = self.store.offer_record(self.record(), [self.prose('overflow')], capture_epoch=self.epoch)
            self.assertEqual(first['status'], 'queued')
            self.assertEqual(second['status'], 'dropped')
            configured = self.store.configure(paused=True)
        self.assertTrue(self.store.flush())
        self.assertFalse(self.store.search()['events'])
        self.assertNotEqual(configured['capture_epoch'], self.epoch)
        self.assertFalse(self.store.activate(configured['capture_epoch'])['recording'])
        self.epoch = self.store.configure(paused=False)['capture_epoch']
        self.assertFalse(self.store.status()['recording'])
        self.store.activate(self.epoch)
        self.offer()

    def test_delete_barrier_scope_fts_cleanup_and_no_resurrection(self):
        original = self.record(ts=self.now + 99999999)
        self.offer(original, [self.prose('uniqueeraseword')])
        self.offer(self.record(chat='x:other'), [self.prose('other retained')])
        with self.store.lock:
            self.store.offer_record(self.record(), [self.prose('queued erased')], capture_epoch=self.epoch)
            result = self.store.delete(chat_id='x:chat-one')
        self.assertTrue(result['ok'])
        self.assertFalse(self.store.search(query='uniqueeraseword')['events'])
        self.assertEqual(len(self.store.search()['events']), 1)
        self.epoch = result['capture_epoch']
        self.store.activate(self.epoch)
        self.now += 1
        self.offer(original, [self.prose('uniqueeraseword')])
        self.assertFalse(self.store.search(query='uniqueeraseword')['events'])
        self.assertNotIn(b'uniqueeraseword', pathlib.Path(self.store.path).read_bytes(), 'compaction clears FTS shadow/free-page copies')
        self.now += 1
        self.offer(candidates=[self.prose('after deletion')])
        self.assertEqual(len(self.store.search()['events']), 2)
        self.store.delete(all=True)
        self.assertFalse(self.store.search()['events'])
        self.assertFalse(self.store.chats()['chats'])
        self.assertFalse(self.store.projects()['projects'])

    def test_retention_source_age_future_clock_cannot_extend(self):
        self.offer(self.record(ts=self.now - 31 * 86400), [self.prose('already expired')])
        self.assertFalse(self.store.search()['events'])
        future = self.record(ts=self.now + 99999999)
        self.offer(future, [self.prose('future source clock')])
        self.assertEqual(len(self.store.search()['events']), 1)
        self.now += 31 * 86400
        self.assertFalse(self.store.search()['events'])
        self.assertFalse(self.store.db.execute('SELECT * FROM source_records').fetchall())
        self.epoch = self.store.configure(retention_days=365)['capture_epoch']
        self.store.activate(self.epoch)
        self.offer(future, [self.prose('future source clock')])
        self.assertFalse(self.store.search()['events'], 'future-clock erased record cannot resurrect')
        # Original source timestamp is in the future, but its receipt expired by observation age.
        # Recording integration fences prevent recapture; store also preserves a retention floor.
        expired = self.record(ts=self.now - 31 * 86400, key=self.store.opaque_identity('record', 'expired'))
        self.offer(expired, [self.prose('expired replay')])
        self.assertFalse(self.store.search()['events'])
        with self.assertRaises(ValueError):
            self.store.configure(retention_days=366)

    def test_exclusions_do_not_erase_existing_and_resume_requires_arm(self):
        self.offer()
        self.epoch = self.store.configure(excluded_chats=['x:chat-one'])['capture_epoch']
        self.store.activate(self.epoch)
        self.offer(candidates=[self.prose('excluded')])
        self.assertEqual(len(self.store.search()['events']), 1)
        self.assertFalse(self.store.search(query='excluded')['events'])
        self.epoch = self.store.configure(excluded_chats=[])['capture_epoch']
        self.store.activate(self.epoch)
        self.offer(candidates=[self.prose('included')])
        self.assertEqual(len(self.store.search()['events']), 2)

    def test_name_ambiguity_provider_project_and_bounded_fallback(self):
        self.offer()
        self.offer(self.record(chat='c:second', provider='claude'), [self.prose('second')])
        self.assertEqual(self.store.resolve_chat('Harbor')['error'], 'ambiguous')
        self.assertEqual(self.store.resolve_chat('Harbor', provider='claude')['chat_id'], 'c:second')
        self.assertEqual(self.store.resolve_chat('x:chat-one')['chat_id'], 'x:chat-one')
        self.assertEqual(len(self.store.chats()['chats']), 2)
        self.store.fts = False
        self.assertEqual(self.store.search(query='second')['search_mode'], 'bounded_plain')
        self.assertEqual(len(self.store.search(query='second')['events']), 1)
        self.assertFalse(self.store.search(query="' OR 1=1 --")['events'])
        page = self.store.search(limit=1)
        self.assertIsNotNone(page['cursor'])
        self.assertNotEqual(page['events'][0]['id'], self.store.search(limit=1, cursor=page['cursor'])['events'][0]['id'])

    def test_daily_summary_preserves_unknown_and_citations(self):
        operation = self.store.opaque_identity('operation', 'operation-one')
        self.offer(candidates=[dict(kind='operation', operation='edit', operation_id=operation, phase='started', outcome='pending', source={'slot': '0'}, file_ref='src/example.py')])
        self.now += 1
        self.offer(candidates=[dict(kind='result', operation_id=operation, phase='result', outcome='unknown', source={'slot': '0'})])
        day = time.strftime('%Y-%m-%d', time.localtime(self.now))
        summary = self.store.session_day_summary(day, chat_id='x:chat-one')
        self.assertEqual(summary['outcomes'][operation], 'unknown')
        self.assertEqual(len(summary['events']), 2)
        self.assertEqual(len(self.store.search(query='example')['events']), 1)
        self.assertEqual(len(self.store.search(query='edit')['events']), 1)
        self.assertFalse(summary['complete'])
        self.assertTrue(all(self.store.get_event(e['id'])['citation'] == e['citation'] for e in summary['events']))
        self.assertEqual(self.store.session_day_summary('2000-01-01')['events'], [])

    def test_v1_archive_certainty_column_migrates_without_losing_evidence(self):
        self.offer(candidates=[self.prose('legacy evidence')])
        event = self.store.search()['events'][0]
        self.store.close()
        database = sqlite3.connect(self.store.path)
        try:
            sql = database.execute("SELECT sql FROM sqlite_master WHERE name='events'").fetchone()[0]
            legacy_sql = sql.replace(",certainty TEXT NOT NULL DEFAULT 'recorded'", '')
            database.execute('ALTER TABLE events RENAME TO old_events')
            database.execute(legacy_sql)
            columns = [r[1] for r in database.execute('PRAGMA table_info(events)')]
            database.execute('INSERT INTO events SELECT ' + ','.join(columns) + ' FROM old_events')
            database.execute('DROP TABLE old_events')
            database.execute("UPDATE settings SET value='1' WHERE key='schema_version'")
            database.commit()
        finally:
            database.close()
        self.store = d.DeckLog(self.root, clock=lambda: self.now)
        self.assertTrue(self.store.available)
        restored = self.store.get_event(event['id'])
        self.assertEqual(restored['text'], 'legacy evidence')
        self.assertEqual(restored['certainty'], 'recorded')

    def test_certainty_persists_independently_of_outcome(self):
        operation = self.store.opaque_identity('operation', 'qualified-operation')
        self.offer(candidates=[dict(kind='run', operation='run', operation_id=operation, phase='result', outcome='success', certainty='probably', source={'slot': '0'})])
        event = self.store.search(query='probably')['events'][0]
        self.assertEqual(event['outcome'], 'success')
        self.assertEqual(event['certainty'], 'probably')
        day = time.strftime('%Y-%m-%d', time.localtime(self.now))
        summary = self.store.session_day_summary(day)
        self.assertEqual(summary['outcome_certainty'][operation], 'probably')
        self.assertEqual(summary['certainty_counts'], {'probably': 1})
        self.store.close()
        self.store = d.DeckLog(self.root, clock=lambda: self.now)
        self.assertEqual(self.store.get_event(event['id'])['certainty'], 'probably')
        self.epoch = self.store.configure(enabled=True)['capture_epoch']
        self.store.activate(self.epoch)
        self.assertEqual(self.store.offer_record(self.record(), [self.prose(certainty='confirmed')], capture_epoch=self.epoch)['status'], 'dropped')
        self.offer(candidates=[self.prose('inferred evidence', certainty='inferred')])
        self.assertEqual(self.store.search(query='inferred')['events'][0]['certainty'], 'inferred')

    @unittest.skipIf(os.name == 'nt', 'POSIX ownership and modes')
    def test_posix_private_modes_and_symlink_refusal(self):
        self.assertEqual(os.stat(self.root).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.store.path).st_mode & 0o777, 0o600)
        linked = os.path.join(os.path.realpath(self.temp.name), 'link')
        os.symlink(self.root, linked)
        denied = d.DeckLog(linked)
        self.assertFalse(denied.available)
        denied.close()
        os.chmod(self.root, 0o755)
        denied = d.DeckLog(self.root)
        self.assertFalse(denied.available)
        denied.close()
        os.chmod(self.root, 0o700)

    @unittest.skipUnless(os.name == 'nt', 'requires real Windows DACL APIs')
    def test_windows_actual_private_acl(self):
        d._windows_security(self.root, True)
        d._windows_security(self.store.path)
        self.offer()
        self.assertEqual(len(self.store.search()['events']), 1)

    @unittest.skipUnless(os.name == 'nt', 'requires real Windows inherited ACLs')
    def test_windows_existing_state_directory_matches_server_creation(self):
        existing = os.path.join(os.path.realpath(self.temp.name), 'existing-state')
        # The server creates its shared state directory with this exact mode;
        # on older Python/Windows its effective private ACL is inherited.
        os.mkdir(existing, 0o700)
        os.chmod(existing, 0o700)
        archive = d.DeckLog(existing)
        try:
            self.assertTrue(archive.available, archive.reason)
            epoch = archive.configure(enabled=True)['capture_epoch']
            self.assertTrue(archive.activate(epoch)['recording'])
            d._windows_security(archive.path)
        finally:
            archive.close()

    @unittest.skipUnless(os.name == 'nt', 'requires real Windows unsafe DACL rejection')
    def test_windows_world_readable_archive_is_rejected(self):
        import ctypes
        from ctypes import wintypes as w
        self.store.close()
        adv = ctypes.WinDLL('advapi32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        descriptor, dacl = w.LPVOID(), w.LPVOID()
        present, defaulted = w.BOOL(), w.BOOL()
        adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.DWORD)]
        adv.GetSecurityDescriptorDacl.argtypes = [w.LPVOID, ctypes.POINTER(w.BOOL), ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.BOOL)]
        adv.SetNamedSecurityInfoW.argtypes = [w.LPWSTR, ctypes.c_int, w.DWORD, w.LPVOID, w.LPVOID, w.LPVOID, w.LPVOID]
        kernel.LocalFree.argtypes = [w.LPVOID]
        self.assertTrue(adv.ConvertStringSecurityDescriptorToSecurityDescriptorW('D:P(A;;FA;;;WD)', 1, ctypes.byref(descriptor), None))
        try:
            self.assertTrue(adv.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)))
            self.assertEqual(adv.SetNamedSecurityInfoW(self.store.path, 1, 0x80000004, None, None, dacl, None), 0)
        finally:
            kernel.LocalFree(descriptor)
        denied = d.DeckLog(self.root)
        self.assertFalse(denied.available)
        self.assertEqual(denied.reason, 'private_storage_unavailable')
        denied.close()

    def test_sqlite_page_cap_and_corrupt_store_fail_visibly(self):
        root = os.path.join(os.path.realpath(self.temp.name), 'small-store')
        archive = d.DeckLog(root, clock=lambda: self.now, limits={'bytes': 128 * 1024})
        try:
            epoch = archive.configure(enabled=True)['capture_epoch']
            archive.activate(epoch)
            for n in range(200):
                record = self.record(key=archive.opaque_identity('record', n))
                result = archive.offer_record(record, [self.prose(('prose %d ' % n) * 230)], capture_epoch=epoch)
                if result['status'] != 'queued':
                    break
                archive.flush()
                if archive.reason:
                    break
            self.assertEqual(archive.status()['reason'], 'storage_full_or_unavailable')
            self.assertLessEqual(os.path.getsize(archive.path), 128 * 1024)
            self.assertFalse(archive.status()['recording'])
        finally:
            archive.close()
        corrupt = os.path.join(os.path.realpath(self.temp.name), 'corrupt')
        os.mkdir(corrupt, 0o700)
        path = os.path.join(corrupt, 'deck-log.sqlite3')
        descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.write(descriptor, b'not a SQLite archive')
        os.close(descriptor)
        if os.name == 'nt':
            # This fixture's inherited private directory is permitted, but its
            # invalid/unprotected file remains safely unavailable either way.
            pass
        broken = d.DeckLog(corrupt)
        self.assertFalse(broken.status()['available'])
        self.assertEqual(broken.status()['reason'], 'private_storage_unavailable')
        broken.close()

    def test_busy_database_stops_capture_without_affecting_caller(self):
        other = sqlite3.connect(self.store.path)
        other.execute('BEGIN IMMEDIATE')
        try:
            started = time.monotonic()
            self.assertEqual(self.store.offer_record(self.record(), [self.prose()], capture_epoch=self.epoch)['status'], 'queued')
            self.assertLess(time.monotonic() - started, 0.1)
            self.store.flush()
            self.assertFalse(self.store.status()['recording'])
        finally:
            other.rollback()
            other.close()


class MutationTests(unittest.TestCase):
    def test_critical_guards_are_exercised(self):
        import types
        source = pathlib.Path(d.__file__).read_text()
        cases = [
            ('local preview capability', "clean = LOCAL_CAPABILITY.sub('[local link omitted]', value)", 'clean = value', 'test_preview_capabilities_do_not_survive_prose_metadata_or_fts'),
            ('record receipt', 'if cursor.rowcount == 0:', 'if False:', 'test_f1_whole_record_dedupe_and_empty_receipt'),
            ('capture generation', 'if epoch != self.epoch or not self.active:', 'if not self.active:', 'test_inflight_stale_generation_cannot_cross_control_barrier'),
            ('erased record ledger', "if self.db.execute('SELECT 1 FROM erased_records WHERE key=?', (record['key'],)).fetchone():", 'if False:', 'test_delete_barrier_scope_fts_cleanup_and_no_resurrection'),
            ('future-clock age', "age = min(record['source_ts'], observed)", "age = record['source_ts']", 'test_retention_source_age_future_clock_cannot_extend'),
            ('certainty preservation', "int(redacted), c.get('certainty', 'recorded')", "int(redacted), 'recorded'", 'test_certainty_persists_independently_of_outcome'),
        ]
        original = globals()['d']
        try:
            for label, before, after, method in cases:
                self.assertIn(before, source, label)
                module = types.ModuleType('deck_log_mutant')
                exec(compile(source.replace(before, after, 1), '<isolated deck-log mutant>', 'exec'), module.__dict__)
                globals()['d'] = module
                result = unittest.TestResult()
                DeckTests(method).run(result)
                self.assertTrue(result.failures or result.errors, label + ' mutation must be detected')
        finally:
            globals()['d'] = original


if __name__ == '__main__':
    unittest.main()
