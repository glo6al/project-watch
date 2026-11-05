#!/usr/bin/env python3
"""Fixture-only native metadata discovery; no user directories or transports."""
import json
import os
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import chat_directory as d


def uid():
    return str(uuid.uuid4())


@unittest.skipIf(os.name == 'nt', 'POSIX native metadata path validation')
class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self.codex = os.path.join(self.root, 'codex')
        self.sessions = os.path.join(self.codex, 'sessions')
        self.claude = os.path.join(self.root, 'claude-projects')
        os.makedirs(self.sessions)
        os.makedirs(self.claude)
        self.index = os.path.join(self.codex, 'session_index.jsonl')
        self.live = {}

    def tearDown(self):
        self.tmp.cleanup()

    def append(self, path, record):
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record) + '\n')

    def codex_chat(self, name='Atlas', project=None, ident=None, metadata=None, folder=None):
        ident = ident or uid()
        folder = folder or self.sessions
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, 'rollout-' + ident + '.jsonl')
        meta = {'id': ident, 'cwd': project or self.root, 'source': 'vscode'}
        meta.update(metadata or {})
        self.append(path, {'type': 'session_meta', 'payload': meta})
        self.append(self.index, {'id': ident, 'thread_name': name})
        return 'x:' + ident, path

    def claude_chat(self, name='Vega', project=None, ident=None):
        ident = ident or uid()
        folder = os.path.join(self.claude, 'project-key')
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, ident + '.jsonl')
        self.append(path, {'type': 'user', 'cwd': project or self.root, 'sessionId': ident,
                           'message': {'content': 'PRIVATE_TRANSCRIPT'}})
        self.append(path, {'type': 'custom-title', 'customTitle': name, 'sessionId': ident})
        return 'c:' + ident, path

    def directory(self, **kwargs):
        kwargs.setdefault('policy', {'history_days': 'all'})
        return d.ChatDirectory(self.codex, self.claude, live_snapshot=lambda: self.live,
                               cache_seconds=100, **kwargs)

    def complete(self, directory, maximum=200):
        for _ in range(maximum):
            status = directory.refresh(force=True)
            if status['complete']:
                return directory.list_agents()
        self.fail('Fixture scan did not complete: ' + repr(directory.status()))

    def test_named_history_independent_of_activity_and_metadata_only(self):
        codex, cp = self.codex_chat()
        claude, _ = self.claude_chat()
        old = time.time() - 100 * 86400
        os.utime(cp, (old, old))
        directory = self.directory()
        result = self.complete(directory)
        self.assertEqual({e['agent_id'] for e in result['agents']}, {codex, claude})
        by_id = {e['agent_id']: e for e in result['agents']}
        self.assertEqual(by_id[codex]['capability'], 'unverified')
        self.assertEqual(by_id[claude]['capability'], 'unavailable')
        serialized = json.dumps(result)
        for private in ('PRIVATE_TRANSCRIPT', self.root, 'inode', 'device', '"identity"', '"path"'):
            self.assertNotIn(private, serialized)
        with self.assertRaisesRegex(d.DirectoryError, 'unverified'):
            directory.resolve(codex, by_id[codex]['identity_revision'])

    def test_policy_default_recency_provider_and_canonical_allow_deny(self):
        a, ap = self.codex_chat(project=self.root + '/one')
        b, _ = self.codex_chat(project=self.root + '/two')
        self.claude_chat()
        old = time.time() - 15 * 86400
        os.utime(ap, (old, old))
        recent = self.complete(self.directory(policy={'providers': ['codex']}))
        self.assertEqual([e['agent_id'] for e in recent['agents']], [b])
        allowed = self.complete(self.directory(policy={'providers': ['codex'], 'history_days': 'all',
                                                       'project_allow': [self.root + '/one/../one']}))
        self.assertEqual([e['agent_id'] for e in allowed['agents']], [a])
        denied = self.complete(self.directory(policy={'history_days': 'all', 'project_deny': [self.root]}))
        self.assertEqual(denied['agents'], [])
        self.assertEqual(d.validate_policy({})['history_days'], 14)
        for policy in ({'history_days': True}, {'providers': []}, {'project_allow': ['relative']}, []):
            with self.assertRaises(ValueError):
                d.validate_policy(policy)

    def test_same_names_and_project_basenames_stay_distinct_with_pages(self):
        a, _ = self.codex_chat(name='Same', project=self.root + '/a/repo')
        b, _ = self.codex_chat(name='Same', project=self.root + '/b/repo')
        self.claude_chat(name='Other')
        directory = self.directory()
        all_agents = self.complete(directory)['agents']
        same = directory.list_agents(query='sAmE', provider='codex')['agents']
        self.assertEqual({e['agent_id'] for e in same}, {a, b})
        self.assertEqual(len({e['project_id'] for e in same}), 2)
        self.assertEqual(len({e['project_label'] for e in same}), 2)
        self.assertTrue(all(e['project_label'].startswith('repo [') for e in same))
        self.live[a] = {'capability': 'reachable', 'reason': ''}
        selected = next(e for e in same if e['agent_id'] == a)
        target = directory.resolve(a, selected['identity_revision'])
        self.assertEqual(target['project_label'], selected['project_label'])
        self.assertEqual(target['reason'], '')
        page = directory.list_agents(limit=1)
        ids = [page['agents'][0]['agent_id']]
        while page['next_cursor']:
            page = directory.list_agents(limit=1, cursor=page['next_cursor'])
            ids += [e['agent_id'] for e in page['agents']]
        self.assertEqual(set(ids), {e['agent_id'] for e in all_agents})
        self.assertEqual(len(directory.list_agents(project_id=same[0]['project_id'])['agents']), 1)

    def test_rename_reused_name_and_replaced_inode_never_redirect(self):
        aid, path = self.codex_chat()
        self.live[aid] = {'capability': 'reachable', 'reason': 'Fixture transport verified.'}
        directory = self.directory()
        selected = self.complete(directory)['agents'][0]
        revision = selected['identity_revision']
        self.assertEqual(directory.resolve(aid, revision)['identity']['path'], path)
        self.append(self.index, {'id': aid[2:], 'thread_name': 'Renamed'})
        other, _ = self.codex_chat(name='Atlas')
        with self.assertRaisesRegex(d.DirectoryError, 'identity changed'):
            directory.resolve(aid, revision)
        refreshed = {e['agent_id']: e for e in self.complete(directory)['agents']}
        self.assertEqual(refreshed[aid]['name'], 'Renamed')
        self.assertEqual(refreshed[other]['name'], 'Atlas')
        replacement = path + '.new'
        with open(path, 'rb') as original, open(replacement, 'wb') as new:
            new.write(original.read())
        os.replace(replacement, path)
        with self.assertRaisesRegex(d.DirectoryError, 'identity changed'):
            directory.resolve(aid, refreshed[aid]['identity_revision'])

    def test_revision_ignores_activity_size_mtime_and_readiness(self):
        aid, path = self.codex_chat()
        directory = self.directory()
        initial = self.complete(directory)['agents'][0]
        self.append(path, {'type': 'event_msg', 'payload': {'type': 'task_started'}})
        self.live[aid] = {'capability': 'reachable', 'reason': 'Verified fixture.'}
        later = self.complete(directory)['agents'][0]
        self.assertEqual(initial['identity_revision'], later['identity_revision'])
        self.assertEqual(later['capability'], 'reachable')
        with patch.object(directory, '_paths', side_effect=AssertionError('resolve enumerated directories')):
            self.assertEqual(directory.resolve(aid, later['identity_revision'])['agent_id'], aid)

    def test_claude_latest_title_after_many_records_and_channel_instance_pin(self):
        aid, path = self.claude_chat(name='First')
        for n in range(8):
            self.append(path, {'type': 'assistant', 'message': {'content': 'PRIVATE_TRANSCRIPT' * 12000}})
            if n == 3:
                self.append(path, {'type': 'custom-title', 'customTitle': 'Latest', 'sessionId': aid[2:]})
        self.live[aid] = {'capability': 'reachable', 'reason': 'Verified fixture channel.', 'instance_id': uid(), 'project': self.root}
        directory = self.directory(max_bytes=32768, max_files=10)
        first = directory.list_agents()
        self.assertFalse(first['directory']['complete'])
        if first['agents']:
            self.assertEqual(first['agents'][0]['capability'], 'unavailable')
        selected = self.complete(directory)['agents'][0]
        self.assertEqual(selected['name'], 'Latest')
        self.assertEqual(selected['capability'], 'reachable')
        self.live[aid]['instance_id'] = uid()
        with self.assertRaisesRegex(d.DirectoryError, 'identity changed'):
            directory.resolve(aid, selected['identity_revision'])

    def test_bounded_refresh_progresses_beyond_first_prefix_and_stale_pages(self):
        expected = {self.codex_chat(name='Chat ' + str(n))[0] for n in range(12)}
        directory = self.directory(max_files=2, max_bytes=128)
        first = directory.list_agents()
        self.assertFalse(first['directory']['complete'])
        final = self.complete(directory)
        self.assertEqual({e['agent_id'] for e in final['agents']}, expected)
        page = directory.list_agents(limit=1)
        self.codex_chat(name='New chat')
        self.complete(directory)
        with self.assertRaisesRegex(d.DirectoryError, 'page changed'):
            directory.list_agents(limit=1, cursor=page['next_cursor'])
        with self.assertRaises(d.DirectoryError):
            directory.list_agents(cursor='not a cursor')

    def test_exclusions_archived_children_exec_review_symlinks(self):
        keep, _ = self.codex_chat(metadata={'agent_path': '/root'})
        for metadata in ({'agent_path': '/root/child'}, {'parent_thread_id': uid()}, {'source': {'subagent': {}}},
                         {'source': 'exec'}, {'originator': 'codex_exec'}, {'thread_source': 'guardian_review'}):
            self.codex_chat(metadata=metadata)
        self.codex_chat(folder=os.path.join(self.codex, 'archived_sessions'))
        self.codex_chat(folder=os.path.join(self.sessions, 'archived_sessions'))
        child, path = self.claude_chat(name='Child')
        self.append(path, {'isSidechain': True, 'cwd': self.root})
        outside = os.path.join(self.root, 'outside')
        outside_id, outside_path = self.codex_chat(folder=outside)
        os.symlink(outside_path, os.path.join(self.sessions, 'rollout-' + outside_id[2:] + '.jsonl'))
        os.symlink(outside, os.path.join(self.sessions, 'link'))
        entries = self.complete(self.directory())['agents']
        self.assertEqual([e['agent_id'] for e in entries], [keep])

    def test_duplicate_native_ids_are_visible_but_unsendable(self):
        aid, _ = self.codex_chat()
        self.codex_chat(ident=aid[2:], folder=os.path.join(self.sessions, 'another'))
        self.live[aid] = {'capability': 'reachable', 'reason': 'Verified fixture.'}
        directory = self.directory()
        selected = self.complete(directory)['agents'][0]
        self.assertEqual(selected['capability'], 'unavailable')
        with self.assertRaisesRegex(d.DirectoryError, 'Multiple local metadata'):
            directory.resolve(aid, selected['identity_revision'])

    def test_long_records_are_bounded_and_honestly_incomplete(self):
        aid, path = self.claude_chat()
        self.append(path, {'type': 'custom-title', 'customTitle': 'X' * (d.MAX_LINE + 20)})
        self.live[aid] = {'capability': 'reachable', 'reason': 'Fixture.', 'instance_id': uid(), 'project': self.root}
        directory = self.directory(max_bytes=16384)
        for _ in range(30):
            directory.refresh(force=True)
        result = directory.list_agents()
        self.assertFalse(result['directory']['complete'])
        self.assertTrue(any('safe parsing bounds' in r for r in result['directory']['reasons']))
        self.assertEqual(result['agents'][0]['capability'], 'unavailable')
        self.assertNotIn('X' * 300, json.dumps(result))
        with self.assertRaisesRegex(d.DirectoryError, 'unavailable'):
            directory.resolve(aid, result['agents'][0]['identity_revision'])

    def test_provider_metadata_identity_mismatch_and_title_removal(self):
        aid, _ = self.codex_chat()
        bad, path = self.claude_chat()
        self.append(path, {'cwd': self.root, 'sessionId': uid()})
        directory = self.directory()
        self.assertEqual([e['agent_id'] for e in self.complete(directory)['agents']], [aid])
        self.append(self.index, {'id': aid[2:], 'thread_name': ''})
        self.assertEqual(self.complete(directory)['agents'], [])

    def test_transport_fallback_and_claude_project_are_verified(self):
        codex, _ = self.codex_chat()
        claude, _ = self.claude_chat()
        self.live['__codex_transport__'] = {'capability': 'unavailable', 'reason': 'Desktop is closed.'}
        self.live[claude] = {'capability': 'reachable', 'reason': 'Fixture.', 'instance_id': uid(),
                            'project': self.root + '/different'}
        directory = self.directory()
        entries = {e['agent_id']: e for e in self.complete(directory)['agents']}
        self.assertEqual(entries[codex]['capability'], 'unavailable')
        self.assertEqual(entries[codex]['reason'], 'Desktop is closed.')
        self.assertEqual(entries[claude]['capability'], 'unavailable')
        self.assertIn('project does not match', entries[claude]['reason'])

    def test_unicode_pages_fit_wire_limit_without_losing_entries(self):
        expected = {self.codex_chat(name='😀' * 230 + str(n))[0] for n in range(40)}
        directory = self.directory()
        self.complete(directory)
        result = directory.list_agents()
        found = set()
        while True:
            self.assertLess(len(json.dumps(result).encode()), 50000)
            found.update(e['agent_id'] for e in result['agents'])
            if not result['next_cursor']:
                break
            result = directory.list_agents(cursor=result['next_cursor'])
        self.assertEqual(found, expected)

    def test_unfinished_record_is_incomplete_without_cached_transcript_fragment(self):
        aid, path = self.claude_chat()
        with open(path, 'a') as f:
            f.write('{"type":"assistant","message":{"content":"PRIVATE_PARTIAL')
        directory = self.directory()
        result = directory.list_agents()
        self.assertFalse(result['directory']['complete'])
        self.assertEqual(result['agents'][0]['capability'], 'unavailable')
        self.assertEqual(directory._files[path]['pending'], b'')
        with open(path, 'a') as f:
            f.write('"}}\n')
        result = self.complete(directory)
        self.assertEqual(result['agents'][0]['agent_id'], aid)
        self.assertNotIn('PRIVATE_PARTIAL', json.dumps(result))

    def test_huge_content_record_does_not_make_metadata_permanently_incomplete(self):
        aid, path = self.claude_chat()
        self.append(path, {'message': {'content': 'PRIVATE_HUGE' * 100000,
                                       'type': 'custom-title', 'customTitle': 'Nested spoof', 'cwd': '/fictional/spoof'},
                           'type': 'assistant', 'cwd': self.root, 'sessionId': aid[2:]})
        self.append(path, {'type': 'custom-title', 'customTitle': 'After huge content', 'sessionId': aid[2:]})
        self.live[aid] = {'capability': 'reachable', 'reason': '', 'project': self.root, 'instance_id': uid()}
        directory = self.directory(max_bytes=65536)
        result = self.complete(directory)
        self.assertTrue(result['directory']['complete'])
        self.assertEqual(result['agents'][0]['name'], 'After huge content')
        self.assertEqual(result['agents'][0]['capability'], 'reachable')
        self.assertNotIn('PRIVATE_HUGE', repr(directory._files[path]))

    def test_appended_index_keeps_progress_and_does_not_starve_chat_metadata(self):
        aid, _ = self.codex_chat()
        directory = self.directory(max_bytes=256, max_files=20)
        # Grow the index faster than each batch can consume. File discovery still
        # has a reserved byte budget, and the already-read index prefix advances.
        previous = 0
        for n in range(12):
            self.append(self.index, {'id': uid(), 'thread_name': 'Other ' + str(n) + 'x' * 120})
            directory.refresh(force=True)
            self.assertGreater(directory._index['offset'], previous)
            previous = directory._index['offset']
        self.assertIn(aid, directory._entries)
        final = self.complete(directory)
        self.assertEqual(final['agents'][0]['agent_id'], aid)


    def test_idle_metadata_is_strict_offline_and_independent_of_codex(self):
        aid, path = self.claude_chat()
        with open(self.index, 'w') as f: f.write('{broken\n')
        directory = self.directory()
        for _ in range(5): directory.refresh(force=True)
        self.assertFalse(directory.status()['complete'])
        proof = directory.claude_metadata(aid, self.root)
        self.assertEqual(proof['agent'], aid)
        self.assertTrue(directory.claude_proof_current(proof))
        entry = next(e for e in directory.list_agents()['agents'] if e['agent_id'] == aid)
        self.assertEqual(entry['capability'], 'unavailable')  # Metadata never proves an online host.
        for suffix in ['{', '{malformed}\n', json.dumps({'isSidechain': True})+'\n', json.dumps({'sessionId':uid()})+'\n']:
            with self.subTest(suffix=suffix):
                with open(path) as source: original = source.read()
                try:
                    with open(path, 'a') as f: f.write(suffix)
                    with self.assertRaises(d.DirectoryError): directory.claude_metadata(aid,self.root)
                finally:
                    with open(path, 'w') as f: f.write(original)
                    directory._claude_selected.clear()
        with self.assertRaises(d.DirectoryError): directory.claude_metadata(aid,self.root+'/wrong')
        denied = self.directory(policy={'history_days':'all','project_deny':[self.root]})
        for _ in range(3): denied.refresh(force=True)
        with self.assertRaises(d.DirectoryError): denied.claude_metadata(aid,self.root)

    def large_cold_claude(self, project=None):
        aid,path=self.claude_chat(project=project)
        self.append(path,{'type':'assistant','message':{'content':'x'*600000}})
        directory=self.directory(max_bytes=2097152)
        self.complete(directory)
        self.assertGreater(os.path.getsize(path),524288)
        self.assertTrue(directory._files[path]['done'])
        self.assertTrue(directory._files[path]['native_id_verified'])
        self.assertNotIn(path,directory._claude_selected)
        return directory,aid,path

    def test_cold_large_claude_first_lookup_reuses_only_verified_broad_progress(self):
        directory,aid,path=self.large_cold_claude()
        broad=directory._files[path]
        try:proof=directory.claude_metadata(aid,self.root)
        except d.DirectoryError as error:self.fail('Cold first lookup must reuse verified broad metadata: '+str(error))
        self.assertTrue(directory.claude_proof_current(proof))
        selected=directory._claude_selected[path]
        self.assertIsNot(selected,broad);self.assertIsNot(selected['meta'],broad['meta'])
        self.assertIsNot(selected['parser'],broad['parser'])
        offset=broad['offset']
        self.append(path,{'cwd':self.root+'/ordinary-later-cwd'})
        self.assertEqual(directory.claude_metadata(aid,self.root)['project'],self.root)
        self.assertIs(directory._claude_selected[path],selected)
        self.assertEqual(broad['offset'],offset,'selected parsing must not advance the broad cache')
        with self.assertRaises(d.DirectoryError):directory.claude_metadata(aid,self.root+'/wrong-expected')

    def test_cold_seed_preserves_unsafe_broad_full_cache_and_existing_selected_veto(self):
        directory,aid,path=self.large_cold_claude();broad=directory._files[path]
        for field,value in [('provider','codex'),('ident',uid()),('done',False),('native_id_verified',False),
                            ('invalid',True),('uncertain',True),('partial',True),('pending',b'{'),('skip',True)]:
            with self.subTest(field=field):
                previous=broad.get(field);broad[field]=value;directory._claude_selected.clear()
                with self.assertRaises(d.DirectoryError):directory.claude_metadata(aid,self.root)
                self.assertEqual(directory._claude_selected[path]['offset'],524288)
                self.assertEqual(broad[field],value,'selected read must not clear a broad contradiction')
                broad[field]=previous
        # Existing bounded progress must continue, never be replaced by an eligible broad seed.
        selected=directory._claude_selected[path]
        proof=directory.claude_metadata(aid,self.root)
        self.assertTrue(directory.claude_proof_current(proof));self.assertIs(directory._claude_selected[path],selected)
        selected['invalid']=True
        with self.assertRaises(d.DirectoryError):directory.claude_metadata(aid,self.root)
        self.assertTrue(selected['invalid']);self.assertIs(directory._claude_selected[path],selected)
        directory._claude_selected.clear();directory.max_entries=1
        directory._claude_selected['fictional-full-slot']={}
        with self.assertRaises(d.DirectoryError):directory.claude_metadata(aid,self.root)
        self.assertEqual(directory._claude_selected,{'fictional-full-slot':{}})

    def test_cold_seed_always_fresh_reads_tail_inode_truncation_and_canonical_project(self):
        for change in ('uuid','subagent','partial','replacement','truncate','canonical'):
            with self.subTest(change=change):
                if change=='canonical':
                    first=os.path.join(self.root,'first');second=os.path.join(self.root,'second')
                    os.mkdir(first);os.mkdir(second);link=os.path.join(self.root,'launch-link');os.symlink(first,link)
                    directory,aid,path=self.large_cold_claude(project=link)
                    os.unlink(link);os.symlink(second,link);expected=first
                else:
                    directory,aid,path=self.large_cold_claude();expected=self.root
                    if change=='uuid':self.append(path,{'sessionId':uid()})
                    elif change=='subagent':self.append(path,{'isSubagent':True})
                    elif change=='partial':
                        with open(path,'a') as out:out.write('{"sessionId":')
                    elif change=='replacement':
                        replacement=path+'.new'
                        with open(replacement,'w') as out:out.write(json.dumps({'sessionId':uid(),'cwd':self.root})+'\n')
                        os.replace(replacement,path)
                    elif change=='truncate':
                        with open(path,'w') as out:out.write(json.dumps({'cwd':self.root})+'\n')
                try:
                    with self.assertRaises(d.DirectoryError):directory.claude_metadata(aid,expected)
                finally:os.unlink(path)

    def test_idle_duplicate_coverage_precedes_title_and_content_filters(self):
        aid, path = self.claude_chat()
        second = os.path.join(self.claude,'another'); os.mkdir(second)
        with open(os.path.join(second,aid[2:]+'.jsonl'),'w') as f: f.write('{')
        directory = self.directory(max_files=1)
        with self.assertRaises(d.DirectoryError): directory.claude_metadata(aid,self.root)
        for _ in range(10): directory.refresh(force=True)
        self.assertEqual(len(directory._claude_candidates(aid[2:])),2)
        with self.assertRaises(d.DirectoryError): directory.claude_metadata(aid,self.root)

    def test_idle_selected_progress_and_known_rotation_veto(self):
        aid, path = self.claude_chat()
        self.append(path, {'type':'assistant','message':{'content':'x'*6000}})
        for i in range(14): open(os.path.join(self.sessions,str(i)+'.txt'),'w').close()
        directory = self.directory(max_entries=2,max_bytes=4096)
        proof = None
        for _ in range(20):
            directory.refresh(force=True)
            try: proof=directory.claude_metadata(aid,self.root); break
            except d.DirectoryError: pass
        self.assertIsNotNone(proof,'selected metadata must make bounded progress independent of broad traversal')
        self.assertTrue(directory.claude_proof_current(proof))
        replacement=path+'.replacement'
        with open(replacement,'w') as f: f.write(json.dumps({'sessionId':aid[2:],'cwd':self.root,'isSidechain':True})+'\n')
        os.replace(replacement,path)
        for _ in range(4): directory.refresh(force=True)
        with self.assertRaises(d.DirectoryError): directory.claude_metadata(aid,self.root)
        self.assertFalse(directory.claude_proof_current(proof),'known replaced or invalid identity must veto an old prepared proof')

    def test_idle_canonical_project_retarget_invalidates_prepared_proof(self):
        first=os.path.join(self.root,'project-a');second=os.path.join(self.root,'project-b')
        os.mkdir(first);os.mkdir(second)
        link=os.path.join(self.root,'project-link');os.symlink(first,link)
        aid,_=self.claude_chat(project=link)
        directory=self.directory();self.complete(directory)
        for rejected_lookup in (True,False):
            with self.subTest(rejected_lookup=rejected_lookup):
                os.unlink(link);os.symlink(first,link)
                old=directory.claude_metadata(aid,first)
                self.assertTrue(directory.claude_proof_current(old))
                os.unlink(link);os.symlink(second,link)
                if rejected_lookup:
                    with self.assertRaises(d.DirectoryError): directory.claude_metadata(aid,first)
                else:
                    new=directory.claude_metadata(aid,second)
                    self.assertTrue(directory.claude_proof_current(new))
                self.assertFalse(directory.claude_proof_current(old),
                                 'old canonical project proof must be invalidated after observed symlink retarget')
                new=directory.claude_metadata(aid,second)
                self.assertTrue(directory.claude_proof_current(new))

    def test_idle_broad_canonical_observation_invalidates_without_selected_lookup(self):
        first=os.path.join(self.root,'broad-a');second=os.path.join(self.root,'broad-b')
        os.mkdir(first);os.mkdir(second)
        link=os.path.join(self.root,'broad-link');os.symlink(first,link)
        aid,_=self.claude_chat(project=link)
        directory=self.directory()
        row=next(e for e in self.complete(directory)['agents'] if e['agent_id']==aid)
        old=directory.claude_metadata(aid,first)
        os.unlink(link);os.symlink(second,link)
        with self.assertRaises(d.DirectoryError): directory.resolve(aid,row['identity_revision'],False)
        directory.refresh(force=True)
        self.assertFalse(directory.claude_proof_current(old),
                         'a newer broad canonical observation must invalidate the old project proof')
        # A separate ordinary directory keeps broad A cached while its selected read learns B.
        os.unlink(link);os.symlink(first,link)
        older=self.directory();self.complete(older)
        os.unlink(link);os.symlink(second,link)
        current=older.claude_metadata(aid,second)
        older.refresh(force=True)
        self.assertTrue(older.claude_proof_current(current),
                        'reused older broad canonical cache must not veto fresher selected proof')

    def test_idle_requires_positive_native_id_and_safe_file(self):
        aid,path=self.claude_chat()
        with open(path,'w') as f: f.write(json.dumps({'type':'custom-title','customTitle':'No ID','cwd':self.root})+'\n')
        directory=self.directory();self.complete(directory)
        with self.assertRaises(d.DirectoryError): directory.claude_metadata(aid,self.root)
        os.unlink(path);os.symlink(self.index,path)
        with self.assertRaises(d.DirectoryError): directory.claude_metadata(aid,self.root)


@unittest.skipIf(os.name == 'nt', 'POSIX native metadata path validation')
class PerformanceTests(unittest.TestCase):
    setUp = DirectoryTests.setUp
    tearDown = DirectoryTests.tearDown
    append = DirectoryTests.append
    claude_chat = DirectoryTests.claude_chat
    codex_chat = DirectoryTests.codex_chat
    directory = DirectoryTests.directory
    complete = DirectoryTests.complete

    def test_metadata_equivalence_at_chunk_boundaries(self):
        records = [
            {'message': {'content': 'text" slash\\ newline\n' * 20,
                         'type': 'custom-title', 'customTitle': 'Nested spoof'},
             'type': 'assistant', 'cwd': '/fictional/project', 'sessionId': uid()},
            {'type': 'custom-title', 'customTitle': 'Actual'},
            {'type': 'assistant', 'message': {'content': 'Plain text ' * 100}},
        ]
        raw = b''.join(json.dumps(r).encode() + b'\n' for r in records)
        expected = [{key: value for key, value in record.items() if key in d._MetadataRecord.fields}
                    for record in records]
        for chunk in (1, 2, 3, 7, 31, 128, 511, 524288):
            parser, values = d._MetadataRecord(), []
            for offset in range(0, len(raw), chunk):
                parser.feed(raw[offset:offset + chunk], values.append)
            self.assertEqual(values, expected, chunk)
            self.assertEqual((parser.depth, parser.phase, parser.string, parser.error), (0, 'start', False, False))

    def test_huge_uncaptured_text_avoids_per_byte_python_capture(self):
        parser, values = d._MetadataRecord(), []
        raw = json.dumps({'type': 'assistant', 'message': {'content': 'content ' * 150000},
                          'cwd': '/fictional/project'}).encode() + b'\n'
        original, calls = parser._capture, [0]
        def capture(byte):
            calls[0] += 1
            original(byte)
        parser._capture = capture
        parser.feed(raw, values.append)
        self.assertLess(calls[0], 1000)
        self.assertEqual(values[0]['cwd'], '/fictional/project')

    def test_pending_file_consumes_remaining_bounded_batch(self):
        aid, path = self.claude_chat()
        self.append(path, {'type': 'assistant', 'message': {'content': 'content ' * 700000}})
        directory = self.directory()
        # Isolate byte-budget consumption from the separately tested wall-clock bound.
        with patch.object(d.time, 'monotonic', return_value=0.0):
            status = directory.refresh()
        self.assertFalse(status['complete'])
        self.assertTrue(directory.work_pending)
        self.assertGreater(directory._files[path]['offset'], 524288)
        self.assertLessEqual(directory._files[path]['offset'], 2097152)
        self.assertEqual(directory.list_agents()['agents'][0]['capability'], 'unavailable')

    def test_deadline_allows_at_most_one_parse_chunk_overrun(self):
        aid, path = self.claude_chat()
        self.append(path, {'type': 'assistant', 'message': {'content': 'content ' * 700000}})
        directory = self.directory()
        clock, chunks = [0.0], []
        original = directory._read_chat
        def read_chat(*args):
            before = directory._files.get(args[1], {}).get('offset', 0)
            result = original(*args)
            after = directory._files.get(args[1], {}).get('offset', 0)
            chunks.append(after - before)
            clock[0] += 0.03
            return result
        with patch.object(d.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(directory, '_read_chat', side_effect=read_chat):
            directory.refresh()
        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(0 < chunk <= 524288 for chunk in chunks))
        self.assertLessEqual(clock[0], 0.05 + 0.03)

    def test_adaptive_wait_caps_duty_and_backs_off_when_idle(self):
        for elapsed in (0.001, 0.01, 0.05, 0.2):
            delay = d.refresh_delay(elapsed, True)
            self.assertGreaterEqual(delay, 0.01)
            self.assertEqual(delay, max(0.01, elapsed))
            self.assertLessEqual(elapsed / (elapsed + delay), 0.5)
            self.assertEqual(d.refresh_delay(elapsed, False), 1.0)

    def test_oversized_metadata_is_unsendable_without_idle_hotloop(self):
        aid, path = self.claude_chat()
        self.append(path, {'type': 'custom-title', 'customTitle': 'X' * 20000})
        directory = self.directory()
        status = directory.refresh()
        self.assertFalse(status['complete'])
        self.assertFalse(directory.work_pending)
        self.assertEqual(d.refresh_delay(0.001, directory.work_pending), 1.0)
        with patch.object(directory, '_read_chat', side_effect=AssertionError('idle retry scanned files')):
            directory.refresh()
        selected = directory.list_agents()['agents'][0]
        self.assertEqual(selected['capability'], 'unavailable')
        with self.assertRaises(d.DirectoryError):
            directory.resolve(aid, selected['identity_revision'])


@unittest.skipIf(os.name == 'nt', 'POSIX native metadata path validation')
class ReviewRegressionTests(unittest.TestCase):
    setUp = DirectoryTests.setUp
    tearDown = DirectoryTests.tearDown
    append = DirectoryTests.append
    claude_chat = DirectoryTests.claude_chat
    codex_chat = DirectoryTests.codex_chat
    directory = DirectoryTests.directory
    complete = DirectoryTests.complete

    @patch.object(d.time, 'monotonic', return_value=0.0)
    def test_f1_completed_snapshot_survives_unchanged_default_rescan(self, _monotonic):
        # Isolate the default file-count/cache contract from the independent 50ms
        # wall budget; deadline behavior has its own deterministic regression.
        for n in range(300):
            self.codex_chat(name='Named ' + str(n))
        clock = [time.time()]
        directory = d.ChatDirectory(self.codex, self.claude, policy={'history_days': 'all'}, now=lambda: clock[0])
        directory.refresh()
        self.assertEqual(len(directory._files), 256)
        self.assertFalse(directory.status()['complete'])
        self.complete(directory)
        clock[0] += 6
        charged = []
        original = directory._read_chat
        def read_chat(*args):
            result = original(*args)
            charged.append(args[-1] - result[0])
            return result
        with patch.object(directory, '_read_chat', side_effect=read_chat):
            result = directory.list_agents(query='Named 1')
        self.assertTrue(result['directory']['complete'])
        self.assertTrue(directory.work_pending)
        self.assertTrue(charged)
        self.assertEqual(sum(charged), 0)
        while directory.work_pending:
            directory.refresh(force=True)
        directory.max_files = 1
        self.append(self.index, {'id': next(iter(directory._titles)), 'thread_name': 'Renamed'})
        self.assertFalse(directory.refresh(force=True)['complete'])

    def test_f1_unknown_added_or_replaced_identity_invalidates_completion(self):
        first, path = self.codex_chat()
        for n in range(10):
            self.codex_chat(name='Other ' + str(n))
        directory = self.directory(max_files=1)
        self.complete(directory)
        replacement = path + '.new'
        with open(path, 'rb') as source, open(replacement, 'wb') as target:
            target.write(source.read())
        os.replace(replacement, path)
        for _ in range(20):
            status = directory.refresh(force=True)
            if directory._files[path]['token'][1] == os.stat(path).st_ino:
                self.assertFalse(status['complete'])
                break
        else:
            self.fail('Replacement native identity was not revalidated')
        while directory.work_pending:
            directory.refresh(force=True)
        self.complete(directory)
        added, _ = self.codex_chat(name='Added')
        # An existing iterator may miss the addition: allow the remainder of
        # that bounded scan plus a full next scan, regardless of scandir order.
        for _ in range(60):
            status = directory.refresh(force=True)
            if added in directory._entries:
                self.assertFalse(status['complete'])
                break
        else:
            self.fail('New native identity was not enumerated')
        self.complete(directory)

    def test_f1_old_claude_history_is_skipped_before_reading_content(self):
        aid, path = self.claude_chat()
        self.append(path, {'type': 'assistant', 'message': {'content': 'old ' * 1000000}})
        old = time.time() - 15 * 86400
        os.utime(path, (old, old))
        directory = self.directory(policy={'history_days': 14})
        with patch.object(directory, '_read_chat', side_effect=AssertionError('ineligible history was read')):
            result = self.complete(directory)
        self.assertEqual(result['agents'], [])

    def test_f2_project_containment_and_initial_claude_binding(self):
        denied, _ = self.codex_chat(project=self.root + '/secret/sub')
        visible, _ = self.codex_chat(project=self.root + '/secret-other')
        directory = self.directory(policy={'history_days': 'all', 'project_deny': [self.root + '/secret']})
        self.assertEqual([e['agent_id'] for e in self.complete(directory)['agents']], [visible])
        allowed = self.directory(policy={'history_days': 'all', 'project_allow': [self.root + '/secret']})
        self.assertEqual([e['agent_id'] for e in self.complete(allowed)['agents']], [denied])
        aid, path = self.claude_chat(project=self.root + '/launch')
        self.live[aid] = {'capability': 'reachable', 'reason': '', 'project': self.root + '/launch', 'instance_id': uid()}
        directory = self.directory(policy={'providers': ['claude'], 'history_days': 'all'})
        selected = self.complete(directory)['agents'][0]
        self.append(path, {'type': 'assistant', 'cwd': self.root + '/elsewhere', 'sessionId': aid[2:]})
        after = self.complete(directory)['agents'][0]
        self.assertEqual(selected['identity_revision'], after['identity_revision'])
        self.assertEqual(selected['project_id'], after['project_id'])
        self.assertEqual(after['capability'], 'reachable')
        self.assertEqual(directory.resolve(aid, after['identity_revision'])['project'], self.root + '/launch')

    def test_f3_empty_and_partial_codex_heads_recover_without_restart(self):
        for prefix in (b'', b'{"type":"session_meta","payload":'):
            with self.subTest(prefix=prefix):
                aid, path = self.codex_chat()
                complete = json.dumps({'type': 'session_meta', 'payload': {'id': aid[2:], 'cwd': self.root, 'source': 'vscode'}}, separators=(',', ':')).encode() + b'\n'
                with open(path, 'wb') as target:
                    target.write(prefix)
                directory = self.directory()
                self.assertFalse(directory.refresh()['complete'])
                with open(path, 'ab') as target:
                    target.write(complete[len(prefix):])
                agents = self.complete(directory)['agents']
                self.assertIn(aid, [e['agent_id'] for e in agents])
                self.assertFalse(directory._files[path]['uncertain'])
                self.assertFalse(directory._files[path].get('partial'))

    def test_f4_resolve_does_not_wait_for_fifty_thousand_entry_publish(self):
        import threading
        aid, path = self.codex_chat()
        self.live[aid] = {'capability': 'reachable', 'reason': ''}
        directory = self.directory(max_entries=60000)
        selected = self.complete(directory)['agents'][0]
        state = directory._files[path]
        for n in range(50000):
            ident = str(uuid.UUID(int=n + 1))
            directory._titles[ident] = 'Fixture ' + str(n)
            directory._files[path + '.' + str(n)] = dict(state, ident=ident, token=(1, n + 1))
        started = threading.Event()
        original = directory._entry
        def entry(*args, **kwargs):
            started.set()
            return original(*args, **kwargs)
        def publish():
            with directory.refresh_lock:
                directory._publish(directory._live())
        directory._entry = entry
        try:
            worker = threading.Thread(target=publish)
            worker.start()
            self.assertTrue(started.wait(1))
            before = time.monotonic()
            target = directory.resolve(aid, selected['identity_revision'])
            elapsed = time.monotonic() - before
            worker.join(10)
        finally:
            directory._entry = original
        self.assertFalse(worker.is_alive())
        self.assertEqual(target['agent_id'], aid)
        self.assertLess(elapsed, 0.1)
        self.assertEqual(len(directory._entries), 50001)

    def test_f4_unchanged_publish_reuses_project_and_revision_cache(self):
        self.codex_chat()
        directory = self.directory()
        self.complete(directory)
        with patch.object(d.os.path, 'realpath', side_effect=AssertionError('uncached canonical project')):
            with patch.object(d, '_hash', side_effect=AssertionError('uncached identity hash')):
                directory._publish(directory._live())


    def test_f1_page_keeps_matching_completeness_snapshot(self):
        self.codex_chat()
        directory = self.directory()
        self.complete(directory)
        original = d._hash
        def changed(value):
            with directory.lock:
                directory._published_status = dict(directory._published_status, complete=False)
            return original(value)
        with patch.object(d, '_hash', side_effect=changed):
            result = directory.list_agents()
        self.assertTrue(result['directory']['complete'])
        self.assertFalse(directory.status()['complete'])


@unittest.skipIf(os.name == 'nt', 'POSIX native metadata path validation')
class RoundTwoDirectoryTests(unittest.TestCase):
    setUp = DirectoryTests.setUp
    tearDown = DirectoryTests.tearDown
    append = DirectoryTests.append
    claude_chat = DirectoryTests.claude_chat
    codex_chat = DirectoryTests.codex_chat
    directory = DirectoryTests.directory
    complete = DirectoryTests.complete

    def test_r2_f4_indexed_chat_added_to_visited_folder_needs_confirmation(self):
        first_folder = os.path.join(self.sessions, 'first')
        later_folder = os.path.join(self.sessions, 'later')
        first, _ = self.codex_chat(name='Same', folder=first_folder)
        self.codex_chat(name='Other', folder=later_folder)
        directory = self.directory(max_files=1)
        def paths():
            for folder in (first_folder, later_folder):
                snapshot = list(os.scandir(folder))
                for item in snapshot:
                    yield 'codex', item.path, item.name[-42:-6]
        with patch.object(directory, '_paths', side_effect=paths):
            directory.refresh()
            self.assertIn(first, directory._entries)
            added, _ = self.codex_chat(name='Same', folder=first_folder)
            while directory._walk is not None:
                status = directory.refresh(force=True)
            self.assertNotIn(added, directory._entries)
            self.assertFalse(status['complete'])
            self.assertTrue(directory.work_pending)
            final = self.complete(directory)
            self.assertTrue(final['directory']['complete'])
            self.assertEqual({e['agent_id'] for e in final['agents'] if e['name'] == 'Same'}, {first, added})
            self.assertFalse(directory.work_pending)

    def test_r2_f4_final_index_check_catches_append_at_walk_completion(self):
        self.codex_chat(name='First')
        directory = self.directory(max_files=1)
        added = []
        def paths():
            existing = list(os.scandir(self.sessions))
            for item in existing:
                yield 'codex', item.path, item.name[-42:-6]
            if not added:
                added.append(self.codex_chat(name='Late')[0])
        with patch.object(directory, '_paths', side_effect=paths):
            directory.refresh()
            status = directory.refresh(force=True)
            self.assertFalse(status['complete'])
            self.assertTrue(directory.work_pending)
            self.assertNotIn(added[0], directory._entries)
            final = self.complete(directory)
            self.assertIn(added[0], [e['agent_id'] for e in final['agents']])
            self.assertFalse(directory.work_pending)

    def test_r2_f8_invalid_activity_times_are_omitted_without_hiding_names(self):
        zero, zero_path = self.codex_chat(name='Same')
        negative, negative_path = self.codex_chat(name='Same')
        normal, _ = self.codex_chat(name='Normal')
        os.utime(zero_path, (0, 0))
        os.utime(negative_path, (-1, -1))
        directory = self.directory()
        entries = {e['agent_id']: e for e in self.complete(directory)['agents']}
        self.assertEqual(set(entries), {zero, negative, normal})
        self.assertNotIn('last_seen', entries[zero])
        self.assertNotIn('last_seen', entries[negative])
        self.assertGreater(entries[normal]['last_seen'], 0)
        for invalid in (float('nan'), float('inf'), 253402300800):
            directory._files[zero_path]['last_seen'] = invalid
            directory._publish(directory._live())
            page = {e['agent_id']: e for e in directory.list_agents()['agents']}
            self.assertNotIn('last_seen', page[zero])
        recent = self.directory(policy={'history_days': 14})
        for invalid in (0, -1, float('nan'), float('inf'), 253402300800):
            self.assertFalse(recent._eligible(self.root, invalid))

    def test_r2_f5_later_title_cannot_clear_unknown_identity_metadata(self):
        good, _ = self.codex_chat(name='Healthy')
        bad, path = self.claude_chat(name='Before')
        with open(path, 'ab') as target:
            target.write(b'{"type":"custom-title","customTitle":\n')
        self.append(path, {'type': 'custom-title', 'customTitle': 'After', 'sessionId': bad[2:]})
        self.live[good] = {'capability': 'reachable', 'reason': ''}
        self.live[bad] = {'capability': 'reachable', 'reason': '', 'project': self.root, 'instance_id': uid()}
        directory = self.directory()
        result = directory.list_agents()
        self.assertFalse(result['directory']['complete'])
        self.assertFalse(directory.work_pending)
        self.assertTrue(any('1 chat metadata file' in reason and 'unverifiable' in reason for reason in result['directory']['reasons']))
        by_id = {e['agent_id']: e for e in result['agents']}
        self.assertEqual(by_id[bad]['name'], 'After')
        self.assertEqual(by_id[bad]['capability'], 'unavailable')
        self.assertIn('unverifiable', by_id[bad]['reason'])
        self.assertEqual(directory.resolve(good, by_id[good]['identity_revision'])['agent_id'], good)
        with self.assertRaises(d.DirectoryError):
            directory.resolve(bad, by_id[bad]['identity_revision'])

    def test_r2_f5_unverifiable_index_cannot_prove_any_codex_name(self):
        aid, _ = self.codex_chat()
        with open(self.index, 'ab') as target:
            target.write(b'{"id":\n')
        self.append(self.index, {'id': aid[2:], 'thread_name': 'After'})
        self.live[aid] = {'capability': 'reachable', 'reason': ''}
        directory = self.directory()
        result = directory.list_agents()
        self.assertFalse(result['directory']['complete'])
        self.assertTrue(any('unverifiable' in reason for reason in result['directory']['reasons']))
        selected = result['agents'][0]
        self.assertEqual(selected['capability'], 'unavailable')
        with self.assertRaises(d.DirectoryError):
            directory.resolve(aid, selected['identity_revision'])


if __name__ == '__main__':
    unittest.main()
