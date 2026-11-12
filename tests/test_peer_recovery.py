"""Offline recovery uses fresh native proof, never an audit report's claims."""
import copy
import builtins
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import claude_mod
import officer_messaging
import peer_bridge
import peer_lifecycle
import peer_recovery as recovery


def ident():
    return str(uuid.uuid4())


def jsonl(path, records):
    path.write_text(''.join(json.dumps(r) + '\n' for r in records))


class PlatformTests(unittest.TestCase):
    def test_unsupported_platform_refuses_before_state_access(self):
        with patch.object(recovery.os, 'name', 'nt'), \
                patch.object(recovery, 'private_directory', side_effect=AssertionError('State accessed')), \
                patch.object(recovery.sqlite3, 'connect', side_effect=AssertionError('Database opened')):
            for apply in (False, True):
                with self.subTest(apply=apply), self.assertRaisesRegex(recovery.RecoveryError, 'requires POSIX file locking'):
                    recovery.recover('fictional-state', selected=[ident()], apply=apply)

    def test_missing_fcntl_allows_import_but_refuses_recovery(self):
        original_import = builtins.__import__
        def without_fcntl(name, *args, **kwargs):
            if name == 'fcntl':
                raise ModuleNotFoundError('Simulated unavailable POSIX module')
            return original_import(name, *args, **kwargs)
        spec = importlib.util.spec_from_file_location('recovery_without_fcntl', recovery.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch.object(builtins, '__import__', side_effect=without_fcntl):
            spec.loader.exec_module(module)
        self.assertIsNone(module.fcntl)
        with patch.object(module, 'private_directory', side_effect=AssertionError('State accessed')):
            with self.assertRaisesRegex(module.RecoveryError, 'requires POSIX file locking'):
                module.recover('fictional-state')


@unittest.skipUnless(os.name == 'posix' and recovery.fcntl is not None, 'Recovery requires POSIX file locking')
class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / 'state'; self.state.mkdir(mode=0o700)
        self.codex = self.root / 'sessions'; self.codex.mkdir()
        self.claude = self.root / 'projects'; self.claude.mkdir()
        (self.claude / 'fictional').mkdir()
        self.store_path = self.root / 'plugin-store.json'
        self.store_path.write_text('{}'); self.store_path.chmod(0o600)
        self.lock_path = self.state / 'officer-messages.lock'
        self.lock_path.touch(mode=0o600)
        self.dbpath = self.state / 'officer-messages.sqlite3'
        self.db = sqlite3.connect(self.dbpath)
        self.dbpath.chmod(0o600)
        peer_bridge.shared_schema(self.db)
        self.key = secrets.token_hex(32)
        self.db.execute('INSERT INTO shared_bridge_service VALUES (1,?,?,?)', (self.key, '{}', 1))
        self.db.execute('''CREATE TABLE claude_mod_enrollments (id TEXT PRIMARY KEY, token_hash TEXT,
            agent TEXT, project TEXT, generation TEXT, last_seen REAL, active INTEGER,
            grant_id TEXT, client_id TEXT, connection_seq INTEGER, peer_protocol INTEGER,
            reply_notice_protocol INTEGER, lifecycle_protocol INTEGER)''')
        self.db.commit()
        self.kwargs = dict(codex_sessions=str(self.codex), claude_projects=str(self.claude), claude_store=str(self.store_path))

    def tearDown(self):
        self.db.close(); self.tmp.cleanup()

    def ledger(self, provider='codex', *, legacy=False):
        native, tid, cid = ident(), ident(), ident()
        target = ('x:' if provider == 'codex' else 'c:') + native
        path = self.codex / ('rollout-fictional-' + native + '.jsonl') if provider == 'codex' else self.claude / 'fictional' / (native + '.jsonl')
        path.touch(); project = str(self.root / 'fictional-project')
        enrollment, attempt, generation, turn = ident(), ident(), ident(), ident()
        grant, namespace = ident(), 'watch-owner-runtime-v2:' + native + ':' + ident()
        identity = dict(path=str(path), device=path.stat().st_dev, inode=path.stat().st_ino)
        if provider == 'claude': identity['mod_enrollment'] = enrollment
        revision = recovery.directory_hash([provider, native, project, 'Fictional Agent', identity] + (['claude_mod'] if provider == 'claude' else []))
        snap = dict(agent_id=target, name='Fictional Agent', provider=provider,
                    project_id=recovery.directory_hash(['watch-project-v1', project]), project_label='fictional', identity_revision=revision)
        text, sender = 'Reply with a fictional number.', 'Fictional sender'
        envelope = (recovery.legacy_envelope if legacy else recovery.compact_envelope)(tid, sender, text)
        digest = peer_bridge.mac(self.key, 'watch-bridge-dedupe-v2', json.dumps([target, revision, text, sender], ensure_ascii=True))
        self.db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)', (cid,target,json.dumps(snap),digest,tid,ident(),1.,2.,''))
        if provider == 'codex':
            records = [dict(type='session_meta', payload=dict(id=native,cwd=project,source='cli')),
                       dict(type='event_msg',timestamp='1970-01-01T00:00:03Z',payload=dict(type='task_started',turn_id=turn)),
                       dict(type='response_item',timestamp='1970-01-01T00:00:04Z',payload=dict(type='message',role='user',turn_id=turn,content=[dict(type='input_text',text=envelope)])),
                       dict(type='event_msg',timestamp='1970-01-01T00:00:05Z',payload=dict(type='task_complete',turn_id=turn))]
        else:
            token = secrets.token_hex(32)
            self.db.execute('INSERT INTO claude_mod_enrollments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (enrollment,recovery.sha(token),target,project,generation,time.time(),1,grant,ident(),1,1,0,2))
            p_id, a_id = ident(), ident()
            records = [dict(type='user',uuid=p_id,parentUuid=None,sessionId=native,cwd=project,isSidechain=False,
                       timestamp='1970-01-01T00:00:04Z',origin=dict(kind='plugin',name='watch-owner'),
                       pluginPrompt=dict(name='watch-owner',text=envelope),message=dict(role='user',content=envelope)),
                       dict(type='assistant',uuid=a_id,parentUuid=p_id,sessionId=native,isSidechain=False,
                       timestamp='1970-01-01T00:00:05Z',message=dict(role='assistant',stop_reason='end_turn',content=[dict(type='text',text='WATCH_REPLY '+tid+'\n42')])),
                       dict(type='system',subtype='stop_hook_summary',uuid=ident(),parentUuid=a_id,sessionId=native,isSidechain=False,preventedContinuation=False)]
            receipt = dict(request_id=tid,attempt_id=attempt,generation=generation,turn_id=turn,phase='answered',completed=True,attempted=True,
                           envelope_digest=recovery.sha(json.dumps(envelope,ensure_ascii=False,separators=(',',':'))),events=[],expires_at=901.)
            store = json.loads(self.store_path.read_text())
            store[namespace] = dict(version=2,mode='derived',grant_id=grant,active=True,acknowledged={},native_id=native,project=project,enrollment_id=enrollment,token=token,requests={tid:receipt})
            self.store_path.write_text(json.dumps(store))
        jsonl(path, records); self.db.commit()
        return dict(namespace=namespace,cid=cid,tid=tid,target=target,path=path,records=records,turn=turn,native=native,enrollment=enrollment,attempt=attempt,generation=generation,project=project,envelope=envelope)

    def run_recovery(self, *rows, apply=False):
        return recovery.recover(str(self.state), [r['cid'] for r in rows] if rows else None, apply=apply, **self.kwargs)

    def assert_excluded(self, row):
        result = self.run_recovery(row)
        self.assertEqual(result['requests'][0]['status'], 'excluded', result)
        with self.assertRaises(recovery.RecoveryError): self.run_recovery(row, apply=True)
        self.assertIsNone(peer_lifecycle.get(self.db,row['tid']))

    def test_dry_run_is_read_only_then_apply_is_withheld_idempotent(self):
        a,b = self.ledger(),self.ledger('claude',legacy=True)
        before = self.dbpath.read_bytes(),self.lock_path.read_bytes(),self.store_path.read_bytes()
        report = self.run_recovery(a,b)
        self.assertEqual([r['status'] for r in report['requests']], ['eligible','eligible'])
        self.assertEqual(before,(self.dbpath.read_bytes(),self.lock_path.read_bytes(),self.store_path.read_bytes()))
        original = list(self.db.execute('SELECT * FROM shared_bridge_requests'))
        self.assertEqual([r['status'] for r in self.run_recovery(a,b,apply=True)['requests']],['recovered','recovered'])
        for row in (a,b):
            saved = peer_lifecycle.get(self.db,row['tid'])
            self.assertEqual((saved['execution'],saved['capture']),('completed','withheld'))
            encoded=json.dumps(saved['evidence'])
            for forbidden in (row['project'],str(row['path']),row['envelope'],'Reply with a fictional number.'):
                self.assertNotIn(forbidden,encoded)
            self.assertFalse(peer_lifecycle.blockers(self.db,row['target']))
        self.assertEqual(list(self.db.execute('SELECT * FROM shared_bridge_requests')),original)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0],0)
        self.assertEqual([r['status'] for r in self.run_recovery(a,b,apply=True)['requests']],['already_recovered','already_recovered'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM peer_execution_events').fetchone()[0],2)

    def test_unknown_claude_receipt_excluded_even_with_final_and_stop_hook(self):
        row=self.ledger('claude');store=json.loads(self.store_path.read_text());rc=store[row['namespace']]['requests'][row['tid']]
        rc['phase']='unknown';rc.pop('completed');self.store_path.write_text(json.dumps(store));self.assert_excluded(row)

    def test_wrong_ledger_hash_or_target_revision_excluded(self):
        for field,value in [('payload_hash','0'*64),('target','x:'+ident())]:
            with self.subTest(field=field):
                row=self.ledger();self.db.execute('UPDATE shared_bridge_requests SET '+field+'=? WHERE client_id=?',(value,row['cid']));self.db.commit();self.assert_excluded(row)

    def test_wrong_attempt_or_turn_against_existing_reservation_rejected(self):
        for field in ('mod_attempt_id','mod_turn','mod_generation'):
            with self.subTest(field=field):
                row=self.ledger('claude')
                self.db.execute('INSERT INTO peer_execution VALUES (?,?,?,?,?,?)',(row['tid'],row['target'],1.,'uncertain','pending',json.dumps({field:ident()})));self.db.commit()
                with self.assertRaises(recovery.RecoveryError):self.run_recovery(row,apply=True)
                self.assertEqual(peer_lifecycle.get(self.db,row['tid'])['execution'],'uncertain')

    def test_codex_missing_start_wrong_turn_duplicate_input_and_gap(self):
        changes=[lambda rs:rs.pop(1),lambda rs:rs[-1]['payload'].update(turn_id=ident()),
                 lambda rs:rs.insert(3,copy.deepcopy(rs[2])),lambda rs:rs.insert(3,dict(type='compacted',payload={})),
                 lambda rs:rs[2]['payload'].pop('turn_id'),
                 lambda rs:rs[2]['payload']['content'].append(dict(type='input_image',image_url='fictional'))]
        for change in changes:
            row=self.ledger();change(row['records']);jsonl(row['path'],row['records']);self.assert_excluded(row)

    def test_dirty_json_and_incomplete_tail_rejected(self):
        for suffix in ('not json\n','{"type":"event_msg"}'):
            row=self.ledger();row['path'].write_text(row['path'].read_text()+suffix);self.assert_excluded(row)

    def test_claude_missing_final_or_stop_or_bad_ancestry_rejected(self):
        changes=[lambda rs:rs.pop(2),lambda rs:rs[1]['message'].update(stop_reason='tool_use'),
                 lambda rs:rs[1].update(parentUuid=ident()),lambda rs:rs[0].update(message=None),
                 lambda rs:rs[1].update(parentUuid={}),lambda rs:rs[2].update(parentUuid=[])]
        for change in changes:
            row=self.ledger('claude');change(row['records']);jsonl(row['path'],row['records']);self.assert_excluded(row)

    def test_malformed_store_receipt_and_native_identity_rejected(self):
        for change in (lambda s,r:s[r['namespace']]['requests'].update({r['tid']:[]}),
                       lambda s,r:s[r['namespace']].update(native_id=ident()),
                       lambda s,r:s[r['namespace']]['requests'][r['tid']].update(completed='true'),
                       lambda s,r:s[r['namespace']]['requests'][r['tid']].update(envelope_digest='0'*64)):
            row=self.ledger('claude');store=json.loads(self.store_path.read_text());change(store,row);self.store_path.write_text(json.dumps(store));self.assert_excluded(row)

    def test_active_writer_and_missing_lock_refuse_apply(self):
        row=self.ledger()
        with self.lock_path.open('rb') as lock:
            recovery.fcntl.flock(lock.fileno(),recovery.fcntl.LOCK_EX|recovery.fcntl.LOCK_NB)
            with self.assertRaisesRegex(recovery.RecoveryError,'writer is active'):self.run_recovery(row,apply=True)
        self.lock_path.unlink()
        with self.assertRaises(recovery.RecoveryError):self.run_recovery(row,apply=True)
        self.assertFalse(self.lock_path.exists())

    def test_one_invalid_selection_rolls_back_entire_apply(self):
        a,b=self.ledger(),self.ledger('claude');b['records'][0]['message']=None;jsonl(b['path'],b['records'])
        with self.assertRaises(recovery.RecoveryError):self.run_recovery(a,b,apply=True)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM peer_execution').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM peer_execution_events').fetchone()[0],0)

    def test_fresh_source_mutation_rolls_back(self):
        row=self.ledger();original=recovery.Sources.revalidate
        def change(sources):
            row['path'].write_text(row['path'].read_text()+json.dumps(row['records'][2])+'\n'+json.dumps(row['records'][3])+'\n')
            return original(sources)
        with patch.object(recovery.Sources,'revalidate',change),self.assertRaises(recovery.RecoveryError):self.run_recovery(row,apply=True)
        self.assertIsNone(peer_lifecycle.get(self.db,row['tid']))

    def test_append_during_revalidation_hash_rolls_back(self):
        row=self.ledger();original=recovery.codex_transcript.open_regular;changed=False
        def source(root,path):
            nonlocal changed
            fd,st=original(root,path)
            if path==str(row['path']):
                read=fd.read
                def append_on_read(size=-1):
                    nonlocal changed
                    data=read(size)
                    if not changed:
                        changed=True
                        with row['path'].open('a') as out:out.write(json.dumps(row['records'][2])+'\n'+json.dumps(row['records'][3])+'\n')
                    return data
                fd.read=append_on_read
            return fd,st
        with patch.object(recovery.codex_transcript,'open_regular',source),self.assertRaises(recovery.RecoveryError):
            self.run_recovery(row,apply=True)
        self.assertTrue(changed)
        self.assertIsNone(peer_lifecycle.get(self.db,row['tid']))

    def test_receipt_store_timestamp_touch_with_identical_bytes_is_safe(self):
        row=self.ledger('claude');before=self.store_path.read_bytes();original=recovery.Sources.revalidate
        def touch(sources):
            st=self.store_path.stat();os.utime(self.store_path,ns=(st.st_atime_ns,st.st_mtime_ns+1000000))
            return original(sources)
        with patch.object(recovery.Sources,'revalidate',touch):result=self.run_recovery(row,apply=True)
        self.assertEqual(result['requests'][0]['status'],'recovered')
        self.assertEqual(self.store_path.read_bytes(),before)

    def test_receipt_store_same_length_change_swap_or_privacy_weakening_refused(self):
        for change in ('bytes','swap','permissions'):
            with self.subTest(change=change):
                row=self.ledger('claude');original=recovery.Sources.revalidate
                def mutate(sources):
                    if change=='bytes':self.store_path.write_text(self.store_path.read_text().replace('"answered"','"unknown "'))
                    elif change=='swap':
                        fresh=self.store_path.with_suffix('.new');fresh.write_bytes(self.store_path.read_bytes());fresh.chmod(0o600);fresh.replace(self.store_path)
                    else:self.store_path.chmod(0o644)
                    return original(sources)
                with patch.object(recovery.Sources,'revalidate',mutate),self.assertRaises(recovery.RecoveryError):self.run_recovery(row,apply=True)
                self.assertIsNone(peer_lifecycle.get(self.db,row['tid']))
                self.store_path.chmod(0o600)

    def test_receipt_mutation_during_initial_or_final_hash_rolls_back(self):
        for when in (2,3):  # First read parses; subsequent reads revalidate bytes.
            with self.subTest(read=when):
                row=self.ledger('claude');original=recovery.codex_transcript.open_regular;reads=0
                def source(root,path):
                    fd,st=original(root,path)
                    if path==str(self.store_path):
                        read=fd.read
                        def mutate_on_read(size=-1):
                            nonlocal reads
                            data=read(size);reads+=1
                            if reads==when:self.store_path.write_text(self.store_path.read_text().replace('"answered"','"unknown "'))
                            return data
                        fd.read=mutate_on_read
                    return fd,st
                with patch.object(recovery.codex_transcript,'open_regular',source),self.assertRaises(recovery.RecoveryError):self.run_recovery(row,apply=True)
                self.assertGreaterEqual(reads,when)
                self.assertIsNone(peer_lifecycle.get(self.db,row['tid']))

    def test_no_retained_payload_and_no_apply_without_allowlist(self):
        row=self.ledger();self.db.execute('INSERT INTO requests VALUES (?,?)',(row['tid'],'{}'));self.db.commit();self.assert_excluded(row)
        with self.assertRaises(recovery.RecoveryError):recovery.recover(str(self.state),apply=True,**self.kwargs)

    def test_private_dir_symlink_store_and_source_rejected(self):
        row=self.ledger('claude');self.state.chmod(0o755)
        with self.assertRaises(recovery.RecoveryError):self.run_recovery(row)
        self.state.chmod(0o700)
        other=self.root/'actual-store.json';self.store_path.rename(other);self.store_path.symlink_to(other);self.assert_excluded(row)

    def test_late_claude_events_cannot_publish_or_reacquire_capacity(self):
        row=self.ledger('claude');self.run_recovery(row,apply=True)
        store=officer_messaging.MessagingStore(str(self.state),str(self.codex))
        try:
            store.db.execute('UPDATE claude_mod_enrollments SET generation=?,last_seen=? WHERE id=?',(row['generation'],time.time(),row['enrollment']));store.db.commit()
            entry=store.mods.get(row['enrollment'])
            common=dict(client_id=entry['client_id'],generation=entry['generation'],native_id=row['native'],request_id=row['tid'],attempt_id=row['attempt'])
            for kind in ('started','unknown','answered'):
                args=dict(common,kind=kind)
                if kind in ('started','answered'):args['turn_id']=row['turn']
                if kind=='answered':args['answer']='WATCH_REPLY '+row['tid']+'\n42'
                with store.db:store.mods.event(entry,args)
                saved=peer_lifecycle.get(store.db,row['tid'])
                self.assertEqual((saved['execution'],saved['capture']),('completed','withheld'))
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0],0)
            # Old historical receipts never negotiate v2 retroactively.
            with self.assertRaises(claude_mod.ModError):
                with store.db:store.mods.event(entry,dict(common,kind='completed',turn_id=row['turn'],outcome='answered',answer='WATCH_REPLY '+row['tid']+'\n42',event_id=ident(),occurred_at=5000))
            self.assertFalse(peer_lifecycle.blockers(store.db,row['target']))
        finally:store.close()

    def test_earlier_terminal_and_later_abort_are_not_original_completion(self):
        for position, kind in ((2,'task_complete'), (4,'turn_aborted'), (4,'task_complete')):
            row=self.ledger()
            row['records'].insert(position,dict(type='event_msg',timestamp='1970-01-01T00:00:05Z',
                                               payload=dict(type=kind,turn_id=row['turn'])))
            jsonl(row['path'],row['records']);self.assert_excluded(row)

    def test_receipt_namespace_mode_grant_and_conflicts_are_checked(self):
        changes=[lambda s,r:s.update({'unrelated':s.pop(r['namespace'])}),
                 lambda s,r:s[r['namespace']].update(mode='manual'),
                 lambda s,r:s[r['namespace']].update(grant_id=ident()),
                 lambda s,r:s[r['namespace']]['requests'][r['tid']].update(events=[{'kind':'invented'}])]
        for change in changes:
            row=self.ledger('claude');store=json.loads(self.store_path.read_text());change(store,row);self.store_path.write_text(json.dumps(store));self.assert_excluded(row)
        row=self.ledger('claude');store=json.loads(self.store_path.read_text());other=copy.deepcopy(store[row['namespace']])
        other['requests'][row['tid']]['attempt_id']=ident()
        store['watch-owner-runtime-v2:'+row['native']+':'+ident()]=other
        self.store_path.write_text(json.dumps(store));self.assert_excluded(row)

    def test_same_path_replacement_cannot_supply_original_pin(self):
        row=self.ledger();other=row['path'].with_suffix('.new');jsonl(other,row['records']);other.replace(row['path']);self.assert_excluded(row)

    def test_recovery_provenance_survives_migration_and_conflicting_reapply_fails(self):
        row=self.ledger();self.run_recovery(row,apply=True)
        before=peer_lifecycle.get(self.db,row['tid']);peer_lifecycle.migrate(self.db);self.db.commit()
        self.assertEqual(peer_lifecycle.get(self.db,row['tid']),before)
        row['records'][-1]['timestamp']='1970-01-01T00:00:06Z';jsonl(row['path'],row['records'])
        with self.assertRaises(recovery.RecoveryError):self.run_recovery(row,apply=True)
        self.assertEqual(peer_lifecycle.get(self.db,row['tid']),before)

    def test_explicit_retirement_is_separate_unknown_policy_and_idempotent(self):
        row=self.ledger('claude');store=json.loads(self.store_path.read_text())
        store[row['namespace']]['requests'][row['tid']].update(phase='unknown',completed=False);self.store_path.write_text(json.dumps(store))
        self.assertEqual(self.run_recovery(row)['requests'][0]['status'],'excluded')
        before=self.dbpath.read_bytes()
        kwargs=dict(retire_selected=[row['cid']],**self.kwargs)
        result=recovery.recover(str(self.state),**kwargs)
        self.assertEqual(result['requests'][0]['status'],'retirement_eligible');self.assertEqual(before,self.dbpath.read_bytes())
        result=recovery.recover(str(self.state),apply=True,**kwargs)
        self.assertEqual(result['requests'][0]['status'],'retired')
        saved=peer_lifecycle.get(self.db,row['tid'])
        self.assertEqual((saved['execution'],saved['capture']),('retired','withheld'))
        self.assertEqual(saved['evidence']['owner_retirement']['provider_outcome'],'unknown')
        self.assertNotIn('historical_completion',saved['evidence'])
        self.assertEqual(recovery.recover(str(self.state),apply=True,**kwargs)['requests'][0]['status'],'already_retired')
        self.assertFalse(peer_lifecycle.blockers(self.db,row['target']))

    def test_proof_and_retirement_batch_is_atomic_and_cannot_overlap(self):
        row=self.ledger()
        with self.assertRaises(recovery.RecoveryError):
            recovery.recover(str(self.state),[row['cid']],retire_selected=[ident()],apply=True,**self.kwargs)
        self.assertIsNone(peer_lifecycle.get(self.db,row['tid']))
        with self.assertRaises(recovery.RecoveryError):
            recovery.recover(str(self.state),[row['cid']],retire_selected=[row['cid']],apply=True,**self.kwargs)

    def test_retirement_can_handle_retained_unknown_without_restoring_reply(self):
        row=self.ledger('claude')
        data=dict(request_id=row['tid'],agent=row['target'],created=1.,launched=True,origin='peer',bridge_version=2,
                  runtime='claude',transport='claude_mod',state='unknown',reply='',mod_enrollment=row['enrollment'],
                  mod_attempt_id=row['attempt'],mod_generation=row['generation'],mod_turn=row['turn'],
                  mod_project=row['project'],target_project=row['project'])
        self.db.execute('INSERT INTO requests VALUES (?,?)',(row['tid'],json.dumps(data)));self.db.commit()
        result=recovery.recover(str(self.state),retire_selected=[row['cid']],apply=True,**self.kwargs)
        self.assertEqual(result['requests'][0]['provider_outcome'],'unknown')
        data=json.loads(self.db.execute('SELECT data FROM requests WHERE id=?',(row['tid'],)).fetchone()[0])
        self.assertEqual(data['state'],'unknown');self.assertEqual(data['reply'],'')
        self.assertNotEqual(data.get('execution_outcome'),'completed')

    def test_proof_never_relabels_owner_retirement_completed(self):
        row=self.ledger();recovery.recover(str(self.state),retire_selected=[row['cid']],apply=True,**self.kwargs)
        with self.assertRaises(recovery.RecoveryError):self.run_recovery(row,apply=True)
        self.assertEqual(peer_lifecycle.get(self.db,row['tid'])['execution'],'retired')


if __name__=='__main__':unittest.main()
