"""Automatic notice contracts. Fictional metadata; native admission is mocked."""
import copy
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import officer_messaging as messaging
import peer_bridge as bridge
import peer_reply_delivery as replies
import server


def uid():
    return str(uuid.uuid4())


class Directory:
    def __init__(self):
        self.lock, self._entries = threading.RLock(), {}

    def resolve(self, aid, revision, require_reachable=True):
        value = self._entries[aid]
        if value['identity_revision'] != revision or require_reachable and value['capability'] != 'reachable':
            raise bridge.PeerError(409, 'Changed fictional metadata.')
        return copy.deepcopy(value)


class PortableContracts(unittest.TestCase):
    def test_canonical_digest_and_fixed_notice(self):
        data = dict(agent_id='x:'+uid(), identity_revision='a'*64, text='Fictional 😀\ntext')
        expected = bridge.digest(json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode())
        self.assertEqual(bridge.receipt_fingerprint(**data), expected)
        data['declared_sender'] = 'Fictional sender'
        self.assertEqual(bridge.receipt_fingerprint(**data), bridge.digest(json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()))
        self.assertNotEqual(bridge.receipt_fingerprint(**data), expected)
        rid = uid(); text = replies.notice(rid)
        self.assertEqual(text.splitlines()[0], 'WATCH_PEER_REPLY '+rid)
        self.assertIn('get_reply once', text); self.assertNotIn('WATCH_REPLY ', text)
        self.assertLess(len(text), 4000)

    def test_strict_duplicate_and_metadata_fields(self):
        manager = replies.ReplyDelivery.__new__(replies.ReplyDelivery)
        data = dict(version=1, provider='codex', native_id=uid(), source_scope='native_parent', tool_use_id='fictional-call',
                    request_id=uid(), target_agent_id='c:'+uid(), target_identity_revision='a'*64, created_at=time.time(),
                    args_sha256='b'*64, observed_at=time.time(), duplicate=False)
        manager.validate(data)
        for changes in [dict(duplicate=True), dict(duplicate=0), dict(version=True), dict(provider='other'), dict(source_scope='worker'),
                        dict(tool_use_id='bad\ncall'), dict(turn_id=''), dict(args_sha256=True), dict(observed_at=float('nan')),
                        dict(cwd='/fictional'), dict(answer='must not accept body'), dict(created_at=True)]:
            with self.subTest(changes=changes), self.assertRaises(bridge.PeerError):manager.validate(dict(data, **changes))


@unittest.skipIf(os.name == 'nt', 'POSIX private state and lifetime lock')
class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.base = Path(self.temp.name).resolve()
        self.sessions = self.base/'sessions'; self.sessions.mkdir()
        self.store = messaging.MessagingStore(str(self.base/'state'), str(self.sessions)); self.store.available = True
        self.directory = Directory(); self.store.shared.directory = self.directory
        self.service = bridge.initialize_service(self.store.db, {'providers':['codex','claude'], 'history_days':'all'})
        self.auth = bridge.service_credential(self.store.db, self.service['credential_id'])
        self.hook = self.store.reply_delivery.setup(dict(enabled=True, providers=['codex','claude'], source_scope='native_parent', consent_version=1), 8793)
        self.key = self.store.reply_delivery.credential(self.hook['credential_id'])
        self.source, self.target = self.codex('Source'), self.codex('Target')

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def codex(self, name):
        native = uid(); path = self.sessions/(native+'.jsonl')
        path.write_text(json.dumps(dict(type='session_meta',payload=dict(id=native,cwd=str(self.base),source='vscode')))+'\n')
        info = path.stat()
        value = dict(agent_id='x:'+native, native_id=native, provider='codex', name=name, identity_revision=bridge.digest((native+name).encode()),
                     project=str(self.base), project_id='a'*64, project_label='Fictional', capability='reachable', reason='',
                     identity=dict(path=str(path),inode=info.st_ino,device=info.st_dev))
        self.directory._entries[value['agent_id']] = value
        return value

    def send(self, explicit=None, sender=None):
        self.rid = explicit or uid(); self.text = 'One fictional scoped request.'
        self.sent = self.store.shared.send(self.auth,self.target['agent_id'],self.target['identity_revision'],self.text,self.rid,sender)
        self.receipt = dict(version=1,provider=self.source['provider'],native_id=self.source['native_id'],source_scope='native_parent',
                            tool_use_id='call-'+uid(),turn_id='turn-'+uid(),request_id=self.rid,target_agent_id=self.target['agent_id'],
                            target_identity_revision=self.target['identity_revision'],created_at=self.sent['created_at'],
                            args_sha256=bridge.receipt_fingerprint(self.target['agent_id'],self.target['identity_revision'],self.text,sender),
                            observed_at=time.time(),duplicate=False)
        return self.receipt

    def bind(self):return self.store.reply_delivery.observe(self.key,self.receipt)

    def answer(self):
        tid = self.store.db.execute('SELECT transport_id FROM shared_bridge_requests WHERE client_id=?',(self.rid,)).fetchone()[0]
        row = next(r for r in self.store._rows() if r['request_id']==tid)
        # This suite isolates return notices, but its completed target fixture
        # still needs a real launch receipt and bound native-turn provenance.
        self.store.shared.authorize(row)
        row.update(state='answered', launched=True, settled=True, execution_outcome='completed',
                   bound_turn='fictional-target-turn-'+uid(), reply='Fictional answer that must not enter the notice.')
        self.store._save(row)

    def dispatch(self):
        process = Mock(); process.wait.return_value = 0
        with patch.object(messaging.subprocess,'Popen',return_value=process) as launch:
            self.store.reply_delivery.tick()
        return launch

    def test_both_arrival_orders_fixed_public_id_and_one_attempt(self):
        for answer_first in [False,True]:
            self.send()
            if answer_first:self.answer()
            self.assertFalse(self.bind()['duplicate'])
            self.assertTrue(self.bind()['duplicate'])
            if not answer_first:self.answer()
            launch = self.dispatch(); self.assertEqual(launch.call_count,1)
            args = launch.call_args.args[0]
            self.assertEqual(args[args.index('--thread')+1],self.source['native_id'])
            self.assertEqual(args[args.index('--message')+1],replies.notice(self.rid))
            self.assertNotIn('Fictional answer',args[-1]); self.assertEqual(self.dispatch().call_count,0)
            row=self.store.reply_delivery.get(self.rid);self.assertEqual(row['state'],'queued');self.assertTrue(row['attempt_id'])
            self.assertNotEqual(row['notice_id'],self.rid)
            self.assertEqual(self.store.snapshot()['requests'],[],'notices and peer rows never enter owner history')

    def test_duplicate_first_explicit_id_immutable_mismatches_and_old_requests(self):
        self.send(explicit=uid(),sender='Fictional sender')
        self.receipt['duplicate']=True
        with self.assertRaises(bridge.PeerError):self.bind()
        self.assertIsNone(self.store.reply_delivery.get(self.rid));self.receipt['duplicate']=False
        for field,value in [('target_identity_revision','d'*64),('created_at',self.receipt['created_at']+1),('args_sha256','e'*64)]:
            original=self.receipt[field];self.receipt[field]=value
            with self.assertRaises(bridge.PeerError):self.bind()
            self.receipt[field]=original
        self.bind();self.receipt['native_id']=self.codex('Sibling')['native_id']
        with self.assertRaises(bridge.PeerError):self.bind()
        self.answer();self.send();self.store.db.execute('DELETE FROM peer_reply_admissions WHERE request_id=?',(self.rid,));self.store.db.commit()
        with self.assertRaises(bridge.PeerError):self.bind()
        duplicate=self.store.shared.send(self.auth,self.target['agent_id'],self.target['identity_revision'],self.text,self.rid)
        self.assertTrue(duplicate['duplicate']);self.assertIsNone(self.store.db.execute('SELECT 1 FROM peer_reply_admissions WHERE request_id=?',(self.rid,)).fetchone())

    def test_source_drift_and_revoke_during_prepare_prevent_admission(self):
        self.send();self.bind();self.answer()
        old=Path(self.source['identity']['path']);old.write_text(json.dumps(dict(type='session_meta',payload=dict(id=self.source['native_id'],cwd='/fictional/other-project')))+'\n')
        self.assertEqual(self.dispatch().call_count,0)
        self.store.reply_delivery.revoke();self.assertEqual(self.store.reply_delivery.get(self.rid)['state'],'failed')
        with self.assertRaises(bridge.PeerError):self.bind()
        self.assertEqual(self.dispatch().call_count,0)

    def test_revoked_shared_access_and_attempt_before_unknown_launch(self):
        self.send();self.bind();self.answer()
        def uncertain(row,message):
            current=self.store.reply_delivery.get(self.rid)
            self.assertEqual(current['state'],'dispatching');self.assertTrue(current['attempted_at']);self.assertTrue(current['attempt_id'])
            self.assertFalse(self.store.lock._is_owned(),'native input must be outside the Store lock')
            return 'unknown','Fictional uncertain admission.'
        with patch.object(self.store,'_queue_codex',side_effect=uncertain) as queue:self.store.reply_delivery.tick()
        self.assertEqual(queue.call_count,1);self.assertEqual(self.dispatch().call_count,0)
        self.send();self.bind();self.answer();self.store.db.execute('UPDATE shared_bridge_credentials SET active=0');self.store.db.commit()
        self.assertEqual(self.dispatch().call_count,0)

    def test_concurrent_receipts_and_restart_never_resubmit(self):
        self.send()
        with ThreadPoolExecutor(max_workers=2) as pool:values=list(pool.map(lambda _:self.bind(),range(2)))
        self.assertEqual(sorted(v['duplicate'] for v in values),[False,True])
        row=self.store.reply_delivery.get(self.rid);row.update(state='dispatching',attempt_id=uid(),attempted_at=time.time())
        self.store.reply_delivery.put(row);self.store.db.commit()
        self.store.close();self.store=messaging.MessagingStore(str(self.base/'state'),str(self.sessions));self.store.available=True;self.store.shared.directory=self.directory
        self.assertEqual(self.store.reply_delivery.get(self.rid)['state'],'unknown');self.assertEqual(self.dispatch().call_count,0)

    def test_terminal_retention_scrubs_paths_and_preserves_tombstone(self):
        for state in ['queued','failed','unknown','dispatching','waiting_reply','pending']:
            self.send();self.bind();self.answer();row=self.store.reply_delivery.get(self.rid);row.update(state=state,created=time.time()-bridge.RETENTION-1,attempt_id=uid(),attempted_at=time.time())
            self.store.reply_delivery.put(row);self.store.db.execute('UPDATE peer_reply_deliveries SET created=? WHERE request_id=?',(row['created'],self.rid));self.store.db.commit()
            self.store.reply_delivery.tick();saved=self.store.reply_delivery.get(self.rid)
            self.assertNotIn('source',saved);self.assertNotIn('receipt',saved);self.assertNotIn(str(self.base),json.dumps(saved));self.assertTrue(saved['attempt_id'])
            self.assertIn(saved['state'],['queued','failed','unknown'],'expired rows retain only terminal no-replay disposition')
            self.assertEqual(self.dispatch().call_count,0)

    def test_offline_busy_failure_and_install_compensation(self):
        with self.assertRaisesRegex(ValueError,'Stop WATCH'):
            replies.provision(str(self.base/'state'),['codex'])
        self.assertEqual(self.store.reply_delivery.credential(self.key['id'])['id'],self.key['id'])
        self.send();self.bind();original_request=self.rid
        self.store.close()
        fsync=os.fsync;install=Mock()
        def directory_sync_failure(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):raise OSError('Fictional credential directory sync failure')
            return fsync(fd)
        with patch.object(replies.os,'fsync',side_effect=directory_sync_failure):
            with self.assertRaisesRegex(OSError,'Fictional credential directory sync'):
                replies.provision(str(self.base/'state'),['codex'],install=install)
        self.assertEqual(install.call_count,0,'credential durability must precede hook configuration')
        self.assertEqual(list((self.base/'state/reply-hook-credentials').glob('*.json')),[])
        def fail(path):
            config=json.loads(Path(path).read_text());self.assertEqual(set(config),{'version','credential_id','secret','port','source_scope','consent_version'})
            with self.assertRaisesRegex(ValueError,'Stop WATCH'):replies.provision(str(self.base/'state'),['codex'])
            raise RuntimeError('Fictional install failure')
        with self.assertRaisesRegex(RuntimeError,'Fictional install'):replies.provision(str(self.base/'state'),['codex'],install=fail)
        self.store=messaging.MessagingStore(str(self.base/'state'),str(self.sessions))
        self.assertEqual(self.store.reply_delivery.credential(self.key['id'])['id'],self.key['id'])
        self.assertEqual(self.store.reply_delivery.get(original_request)['state'],'waiting_reply','failed install rolls back pending notice revocation')
        self.assertEqual(list((self.base/'state/reply-hook-credentials').glob('*.json')),[])
        self.store.close()
        def remove_failure():
            with self.assertRaisesRegex(ValueError,'Stop WATCH'):replies.provision(str(self.base/'state'),['codex'])
            raise RuntimeError('Fictional remove failure')
        with self.assertRaisesRegex(RuntimeError,'Fictional remove'):replies.revoke_offline(str(self.base/'state'),self.key['id'],install=remove_failure)
        self.store=messaging.MessagingStore(str(self.base/'state'),str(self.sessions))
        self.assertEqual(self.store.reply_delivery.credential(self.key['id'])['id'],self.key['id'])
        self.assertEqual(self.store.reply_delivery.get(original_request)['state'],'waiting_reply','failed remove restores prior grant and notice')

    def test_offline_final_commit_failure_runs_install_undo_under_lock(self):
        self.send();self.bind();original_request=self.rid
        self.store.close()
        config=self.base/'fictional-hooks.json';config.write_bytes(b'previous working configuration')
        connect=sqlite3.connect
        for operation in ['setup','revoke']:
            armed={'value':False};undone=[]
            class CommitFailure(sqlite3.Connection):
                def commit(connection):
                    if armed['value']:raise sqlite3.OperationalError('Fictional final commit failure')
                    return super().commit()
            def failing_connection(*args,**kwargs):return connect(*args,**dict(kwargs,factory=CommitFailure))
            def install(*args):
                before=config.read_bytes();config.write_bytes(b'new owned configuration');armed['value']=True
                def undo():
                    with self.assertRaisesRegex(ValueError,'Stop WATCH'):replies.provision(str(self.base/'state'),['codex'])
                    if config.read_bytes()==b'new owned configuration':config.write_bytes(before)
                    undone.append(True)
                return undo
            with patch.object(bridge.sqlite3,'connect',side_effect=failing_connection):
                with self.assertRaisesRegex(sqlite3.OperationalError,'Fictional final commit'):
                    if operation=='setup':replies.provision(str(self.base/'state'),['codex'],install=install)
                    else:replies.revoke_offline(str(self.base/'state'),self.key['id'],install=install)
            self.assertEqual(undone,[True]);self.assertEqual(config.read_bytes(),b'previous working configuration')
            self.assertEqual(list((self.base/'state/reply-hook-credentials').glob('*.json')),[])
            self.store=messaging.MessagingStore(str(self.base/'state'),str(self.sessions))
            self.assertEqual(self.store.reply_delivery.credential(self.key['id'])['id'],self.key['id'])
            self.assertEqual(self.store.reply_delivery.get(original_request)['state'],'waiting_reply')
            if operation=='setup':self.store.close()


    def test_admission_fingerprint_rollback_and_failed_attempt_write(self):
        rid=uid()
        self.store.db.execute("CREATE TRIGGER fail_fingerprint BEFORE INSERT ON peer_reply_admissions BEGIN SELECT RAISE(ABORT,'fixture'); END")
        self.store.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.shared.send(self.auth,self.target['agent_id'],self.target['identity_revision'],'Fictional rollback.',rid)
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM shared_bridge_requests WHERE client_id=?',(rid,)).fetchone())
        self.assertEqual(self.store._rows(),[])
        self.store.db.execute('DROP TRIGGER fail_fingerprint');self.store.db.commit()
        self.send();self.bind();self.answer()
        put=self.store.reply_delivery.put
        def fault(row):
            put(row)
            if row['state']=='dispatching':raise sqlite3.OperationalError('fixture after attempt write')
        with patch.object(self.store.reply_delivery,'put',side_effect=fault):self.assertEqual(self.dispatch().call_count,0)
        self.assertIsNone(self.store.reply_delivery.get(self.rid)['attempt_id'],'rolled-back admission never leaves an attempt')
        self.assertEqual(self.dispatch().call_count,1)

    def test_production_http_mutual_proof_and_scoped_receipt_only(self):
        httpd=server.Server(('127.0.0.1',0),server.H);thread=threading.Thread(target=httpd.serve_forever,daemon=True)
        with patch.multiple(server,MESSAGING=self.store,KEY='fictional-owner-key',PORT=httpd.server_port):
            thread.start()
            def request(connection,path,body,headers=None):
                connection.request('POST',path,body,headers or {'Content-Type':'application/json'})
                response=connection.getresponse();raw=response.read()
                try:value=json.loads(raw)
                except ValueError:value=raw.decode()
                return response.status,value,response.headers
            try:
                c=http.client.HTTPConnection('127.0.0.1',httpd.server_port,timeout=4)
                setup=dict(enabled=True,providers=['codex','claude'],source_scope='native_parent',consent_version=1)
                self.assertEqual(request(c,'/peer-reply-setup',json.dumps(setup))[0],403)
                status,self.hook,_=request(c,'/peer-reply-setup',json.dumps(setup),{'X-Live-Room-Key':server.KEY,'Content-Type':'application/json'})
                self.assertEqual(status,200);self.key=self.store.reply_delivery.credential(self.hook['credential_id']);self.send()
                nonce='c'*64
                status,proof,_=request(c,'/peer-reply/v1/challenge',json.dumps(dict(credential_id=self.key['id'],nonce=nonce)))
                self.assertEqual(status,200);port=httpd.server_port
                self.assertEqual(proof['proof'],bridge.mac(self.key['secret'],'watch-peer-reply-server-v1',port,self.key['id'],nonce,proof['run_id']))
                body=json.dumps(self.receipt).encode();at=str(int(time.time()))
                headers={'Content-Type':'application/json','X-Watch-Reply':self.key['id'],'X-Watch-Time':at,'X-Watch-Nonce':nonce,'X-Watch-Run':proof['run_id'],
                         'X-Watch-Signature':bridge.mac(self.key['secret'],'watch-peer-reply-request-v1',port,'POST','/peer-reply/v1/receipt',at,nonce,proof['run_id'],bridge.digest(body))}
                other=http.client.HTTPConnection('127.0.0.1',port,timeout=4)
                self.assertEqual(request(other,'/peer-reply/v1/receipt',body,headers)[0],403,'proof must stay on its socket');other.close()
                status,value,response_headers=request(c,'/peer-reply/v1/receipt',body,headers)
                self.assertEqual(status,200);self.assertTrue(value['accepted'])
                raw=json.dumps(value).encode()
                self.assertEqual(response_headers['X-Watch-Signature'],bridge.mac(self.key['secret'],'watch-peer-reply-response-v1',port,proof['run_id'],nonce,status,bridge.digest(raw)))
                for mode in ['signature','time','nonce','run','key','host','origin']:
                    trial=http.client.HTTPConnection('127.0.0.1',port,timeout=4);n=uuid.uuid4().hex*2
                    status,pf,_=request(trial,'/peer-reply/v1/challenge',json.dumps(dict(credential_id=self.key['id'],nonce=n)));self.assertEqual(status,200)
                    h=dict(headers,**{'X-Watch-Nonce':n,'X-Watch-Run':pf['run_id'],'X-Watch-Time':str(int(time.time()))})
                    h['X-Watch-Signature']=bridge.mac(self.key['secret'],'watch-peer-reply-request-v1',port,'POST','/peer-reply/v1/receipt',h['X-Watch-Time'],n,pf['run_id'],bridge.digest(body))
                    if mode=='signature':h['X-Watch-Signature']='0'*64
                    elif mode=='time':h['X-Watch-Time']=str(int(time.time())-31)
                    elif mode=='nonce':h['X-Watch-Nonce']='f'*64
                    elif mode=='run':h['X-Watch-Run']=uid()
                    elif mode=='key':h['X-Watch-Reply']=self.auth['id']
                    elif mode=='host':h['Host']='example.invalid'
                    else:h['Origin']='https://example.invalid'
                    if mode in ('time','nonce','run','key'):
                        h['X-Watch-Signature']=bridge.mac(self.key['secret'],'watch-peer-reply-request-v1',port,'POST','/peer-reply/v1/receipt',h['X-Watch-Time'],h['X-Watch-Nonce'],h['X-Watch-Run'],bridge.digest(body))
                    self.assertEqual(request(trial,'/peer-reply/v1/receipt',body,h)[0],403,mode+' must reject');trial.close()
                self.assertNotIn('reply',value);self.assertNotIn('secret',value)
                self.assertEqual(request(c,'/peer-reply/v1/receipt',body,headers)[0],403,'challenge is single-use')
                status,_,_=request(c,'/bridge/v2/challenge',json.dumps(dict(credential_id=self.key['id'],nonce='d'*64)))
                self.assertEqual(status,403,'hook key is not shared service access')
                c.close();c=http.client.HTTPConnection('127.0.0.1',port,timeout=4)
                status,_,_=request(c,'/peer-reply-setup',json.dumps(setup),{'X-Watch-Reply':self.key['id']})
                self.assertEqual(status,403,'hook key has no owner permissions');c.close()
            finally:
                httpd.shutdown();httpd.server_close();thread.join()

    def test_claude_legacy_negotiation_idle_notice_claim_and_ack(self):
        native=uid();aid='c:'+native;agents={aid:dict(runtime='Claude',cwd=str(self.base),name='Fictional Claude')}
        pair=self.store.mods.setup_code(8793,peer_receive=True);grant=self.store.mods.call('setup',{'code':pair['pair_code']},port=8793)
        body=dict(native_id=native,project=str(self.base),client_id=uid(),connection_seq=0,peer_protocol=1)
        old=self.store.mods.call('register',body,grant['grant'],agents)
        self.assertEqual(old['reply_notice_protocol'],0);self.assertEqual(old['supported_reply_notice_protocol'],1)
        with self.assertRaises(Exception):self.store.mods.call('register',dict(body,reply_notice_protocol=1),grant['grant'],agents)
        legacy_source=dict(agent_id=aid,native_id=native,provider='claude',name='Fictional Claude',identity_revision='c'*64,project=str(self.base),
                           project_id='b'*64,project_label='Fictional',capability='reachable',identity=dict(path=str(self.base/'fictional-claude.jsonl'),device=1,inode=2,mod_enrollment=old['enrollment_id']))
        self.directory._entries[aid]=legacy_source;self.source=legacy_source
        legacy_proof=dict(agent=aid,project=str(self.base),name='Fictional Claude',identity={k:v for k,v in legacy_source['identity'].items() if k!='mod_enrollment'})
        with patch.object(self.store.shared,'metadata_target',return_value=legacy_proof):
            self.send()
            with self.assertRaises(bridge.PeerError):self.bind()
            self.answer()
        body.update(connection_seq=1,reply_notice_protocol=1)
        new=self.store.mods.call('register',body,grant['grant'],agents)
        entry=self.store.mods.get(new['enrollment_id'])
        source=dict(agent_id=aid,native_id=native,provider='claude',name='Fictional Claude',identity_revision='c'*64,project=str(self.base),
                    project_id='b'*64,project_label='Fictional',capability='reachable',supported_transport='claude_mod',identity=dict(path=str(self.base/'fictional-claude.jsonl'),device=1,inode=2,mod_enrollment=entry['id']))
        self.directory._entries[aid]=source;self.source=source
        proof=dict(agent=aid,project=str(self.base),name='Fictional Claude',identity={k:v for k,v in source['identity'].items() if k!='mod_enrollment'})
        current={'value':True}
        with patch.object(self.store.shared,'metadata_target',return_value=proof),patch.object(self.store.shared,'metadata_current',side_effect=lambda _:current['value']):
            self.store.db.execute('UPDATE claude_mod_grants SET peer_receive=0 WHERE id=?',(entry['grant_id'],));self.store.db.commit()
            source['capability']='unavailable'
            with self.assertRaises(bridge.PeerError):self.store.reply_delivery.prepare(aid)
            source['capability']='reachable'
            self.store.db.execute('UPDATE claude_mod_grants SET peer_receive=1 WHERE id=?',(entry['grant_id'],));self.store.db.commit()
            replacement=copy.deepcopy(proof);replacement['identity']['inode']+=1
            with patch.object(self.store.shared,'metadata_target',return_value=replacement):
                with self.assertRaises(bridge.PeerError):self.store.reply_delivery.prepare(aid)
            self.send();self.bind();self.answer();self.store.reply_delivery.tick()
            poll=dict(generation=new['generation'],native_id=native,client_id=body['client_id'],project=str(self.base),accept=True)
            prepare=self.store.reply_delivery.prepare
            def withdraw_after_prepare(*args,**kwargs):
                prepared=prepare(*args,**kwargs)
                self.store.db.execute('UPDATE claude_mod_grants SET peer_receive=0 WHERE id=?',(entry['grant_id'],));self.store.db.commit()
                return prepared
            with patch.object(self.store.reply_delivery,'prepare',side_effect=withdraw_after_prepare):
                self.assertIsNone(self.store.mods.call('poll',poll,new['token'],{})['claim'],'withdrawn agent consent must veto a prepared notice')
            self.assertIsNone(self.store.reply_delivery.get(self.rid)['attempt_id'])
            self.store.db.execute('UPDATE claude_mod_grants SET peer_receive=1 WHERE id=?',(entry['grant_id'],));self.store.db.commit()
            result=self.store.mods.call('poll',poll,new['token'],{})
            claim=result['claim'];self.assertIsNotNone(claim,'notice-only idle parent must claim without room AGENTS')
            self.assertEqual(claim['kind'],'peer_reply_notice');self.assertEqual(claim['public_request_id'],self.rid)
            event=dict(generation=new['generation'],native_id=native,client_id=body['client_id'],request_id=claim['request_id'],attempt_id=claim['attempt_id'],kind='submitted')
            self.assertEqual(self.store.mods.call('event',event,new['token'],{})['state'],'queued')
            self.assertEqual(self.store.mods.call('event',event,new['token'],{})['state'],'queued')
            self.assertIsNone(self.store.mods.call('poll',poll,new['token'],{})['claim'])
            with self.assertRaises(Exception):self.store.mods.call('event',dict(event,kind='answered',turn_id='fake',answer='fake'),new['token'],{})
            saved=self.store.reply_delivery.get(self.rid)
            self.store.db.execute('UPDATE peer_reply_deliveries SET created=? WHERE request_id=?',(time.time()-bridge.RETENTION-1,self.rid));self.store.db.commit()
            self.store.reply_delivery.tick();tombstone=self.store.reply_delivery.get(self.rid)
            self.assertNotIn('source',tombstone);self.assertNotIn('mod_project',tombstone);self.assertNotIn(str(self.base),json.dumps(tombstone),'terminal Claude metadata expires too')
            self.assertEqual(self.store.mods.call('event',event,new['token'],{})['state'],'queued','retained original ACK drains after retention')
            self.assertEqual(self.store.reply_delivery.get(self.rid),tombstone,'late ACK never mutates or reopens a tombstone')
            with self.assertRaises(Exception):self.store.mods.call('event',dict(event,attempt_id=uid()),new['token'],{})
            with bridge.transaction(self.store.db,self.store.lock):
                with self.assertRaises(bridge.PeerError):self.store.reply_delivery.event_in_transaction(dict(entry,id=uid()),event)
            ordinary=self.store.shared.send(self.auth,aid,source['identity_revision'],'Fictional next ordinary peer question.',uid())
            following=self.store.mods.call('poll',poll,new['token'],agents)['claim']
            self.assertIsNotNone(following,'next ordinary work is claimable after retained notice ACK')
            self.assertEqual(following['origin'],'peer');self.assertNotIn('kind',following)
            self.store.mods.call('event',dict(event,request_id=following['request_id'],attempt_id=following['attempt_id'],kind='dropped'),new['token'],agents)
            self.send();self.bind();self.answer();self.store.reply_delivery.tick()
            original=self.store.reply_delivery.prepare
            def changed(*args,**kwargs):
                value=original(*args,**kwargs);current['value']=False;return value
            with patch.object(self.store.reply_delivery,'prepare',side_effect=changed):
                self.assertIsNone(self.store.mods.call('poll',poll,new['token'],agents)['claim'],'interposed identity invalidation must veto claim')


if __name__=='__main__':unittest.main()
