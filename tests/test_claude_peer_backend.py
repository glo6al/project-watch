"""Scoped Claude peer claims over production HTTP/SQLite; fictional local fixtures."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import chat_directory
import peer_bridge
import server
import test_claude_mod as owner_fixture

uid = owner_fixture.uid


@unittest.skipIf(os.name == 'nt', 'Requires private POSIX outbox')
class ClaudePeerBackendTests(unittest.TestCase):
    http = owner_fixture.ClaudeModTests.http
    register_global = owner_fixture.ClaudeModTests.register_global
    global_poll = owner_fixture.ClaudeModTests.global_poll
    request = owner_fixture.ClaudeModTests.request
    enroll = owner_fixture.ClaudeModTests.enroll

    def setUp(self):
        owner_fixture.ClaudeModTests.setUp(self)
        root = self.base/'claude-projects'/'fictional'; root.mkdir(parents=True)
        self.transcript = root/(self.sid+'.jsonl')
        self.transcript.write_text(json.dumps(dict(type='custom-title', customTitle='Fictional',
                                                   sessionId=self.sid, cwd=self.project))+'\n')
        codex = self.base/'codex'; (codex/'sessions').mkdir(parents=True)
        self.directory = chat_directory.ChatDirectory(str(codex), str(root.parent), policy={'history_days':'all'},
            codex_sessions=str(codex/'sessions'), live_snapshot=lambda:server.shared_directory_snapshot(self.store))
        self.store.shared.directory = self.directory
        with self.store.lock:
            credential = peer_bridge.initialize_service(self.store.db, {'history_days':'all'})
            self.credential = peer_bridge.service_credential(self.store.db, credential['credential_id'])

    def tearDown(self):
        owner_fixture.ClaudeModTests.tearDown(self)

    def setup_global(self, consent=True, protocol=1):
        status, setup = self.http('/claude-mod-setup', {'peer_receive':True} if consent else {}, owner=True)
        self.assertEqual(status,200,setup); self.assertIs(setup['peer_receive'],consent)
        status, self.grant = self.http('/claude-mod/setup', {'code':setup['pair_code']})
        self.assertEqual(status,200,self.grant)
        extra = {} if protocol is None else {'peer_protocol':protocol}
        status, self.registration, self.body = self.register_global(self.grant, **extra)
        self.assertEqual(status,200,self.registration)
        return self.registration

    def target(self):
        for _ in range(30):
            if self.directory.refresh(force=True)['complete']:
                break
        return self.directory.list_agents()['agents'][0]

    def send(self, text='Report the fictional status.', request_id=None):
        target = self.target()
        return self.store.shared.send(self.credential,target['agent_id'],target['identity_revision'],text,
                                      request_id or uid(),'Fictional requester')

    def peer_row(self, request_id):
        tid = self.store.db.execute('SELECT transport_id FROM shared_bridge_requests WHERE client_id=?',(request_id,)).fetchone()[0]
        return next(r for r in self.store._rows() if r['request_id']==tid)

    def event(self, claim, kind='answered', **extras):
        data = dict(generation=self.generation,native_id=self.sid,client_id=self.body['client_id'],
                    request_id=claim['request_id'],attempt_id=claim['attempt_id'],kind=kind)
        if kind == 'answered':data.update(turn_id='fictional-turn',answer='WATCH_REPLY '+claim['request_id']+'\nFictional reply.')
        data.update(extras)
        return self.http('/claude-mod/event',data,token=self.token)

    def test_consent_protocol_and_old_manual_or_global_clients_are_independent(self):
        self.enroll()
        self.assertEqual(self.store.mods.authenticate(self.token)['peer_protocol'],0)
        self.assertFalse(self.store.mods.peer_capability(self.aid,self.agents[self.aid])[0])
        with self.store.lock,self.store.db:
            self.store.db.execute('UPDATE claude_mod_enrollments SET active=0,generation=NULL')
        self.setup_global(consent=False)
        self.assertEqual(self.registration['peer_protocol'],1)
        self.assertEqual(self.target()['capability'],'unavailable')
        self.assertFalse(self.store.owner_capabilities(self.aid,self.agents)['claude_setup']['peer_receive'])
        self.setup_global(protocol=None)
        self.assertEqual(self.registration['peer_protocol'],0)
        self.assertEqual(self.target()['capability'],'unavailable')
        old = dict(self.body)
        self.assertEqual(self.http('/claude-mod/register',dict(old,peer_protocol=1),token=self.grant['grant'])[0],409)
        status, upgraded = self.http('/claude-mod/register',dict(old,connection_seq=1,peer_protocol=1),token=self.grant['grant'])
        self.assertEqual(status,200,upgraded)
        self.assertEqual(self.target()['capability'],'reachable')
        status, downgraded = self.http('/claude-mod/register',dict(old,connection_seq=2),token=self.grant['grant'])
        self.assertEqual(status,200,downgraded)
        self.assertEqual(downgraded['peer_protocol'],0)
        self.assertEqual(self.target()['capability'],'unavailable')
        for protocol in (True,2,-1,'1'):
            self.assertEqual(self.http('/claude-mod/register',dict(old,peer_protocol=protocol),token=self.grant['grant'])[0],400)
        for body in ({'peer_receive':False},{'peer_receive':1},{'peer_receive':True,'other':1}):
            self.assertEqual(self.http('/claude-mod-setup',body,owner=True)[0],400)

    def test_production_snapshot_explains_owner_only_mod_without_claiming_readiness(self):
        self.setup_global(consent=False)
        facts=server.shared_directory_snapshot(self.store)[self.aid]
        self.assertEqual(facts['capability'],'unavailable')
        self.assertEqual(facts['supported_transport'],'claude_mod')
        self.assertEqual(facts['mod_enrollment'],self.registration['enrollment_id'])
        self.assertEqual(facts['project'],self.project)
        self.assertIn('explicit WATCH setup',facts['reason'])
        target=self.target();self.assertEqual(target['supported_transport'],'claude_mod')
        self.assertEqual(target['capability'],'unavailable')
        self.assertIn('compatible live official plugin',target['reason'])

    def test_exact_peer_claim_answer_dedupe_private_owner_view_and_influence_warning(self):
        self.setup_global(); sent=self.send(); row=self.peer_row(sent['request_id'])
        self.assertIn('Origin: peer.',row['envelope']);self.assertIn('UNVERIFIED',row['envelope'])
        self.assertIn('Do not call send_message',row['envelope']);self.assertNotIn('channel reply tool',row['envelope'])
        self.assertEqual(self.store.snapshot()['requests'],[])
        status,result=self.global_poll(self.body['client_id']);self.assertEqual(status,200,result)
        claim=result['claim'];self.assertEqual(claim['origin'],'peer')
        self.assertEqual(claim['request_id'],row['request_id'])
        self.assertIsNotNone(self.store.db.execute('SELECT launched FROM shared_bridge_requests').fetchone()[0])
        self.assertEqual(self.event(dict(claim,attempt_id=uid()))[0],409)
        self.assertEqual(self.event(claim,answer='Uncorrelated')[0],409)
        self.assertEqual(self.event(claim)[0],200)
        self.assertEqual(self.event(claim)[0],200)
        self.assertEqual(self.event(claim,answer='WATCH_REPLY '+row['request_id']+'\nDifferent')[0],409)
        self.assertEqual(self.store.shared.result(sent['request_id'])['reply'],'Fictional reply.')
        self.assertTrue(self.send(request_id=sent['request_id'])['duplicate'])
        self.assertEqual(self.store.snapshot()['requests'],[])
        owner,_=self.request('Owner question after the peer.')
        claim=self.global_poll(self.body['client_id'])[1]['claim']
        self.assertNotIn('origin',claim)
        self.assertEqual(self.event(claim)[0],200)
        answer=self.store.snapshot()['requests'][0]
        self.assertEqual(answer['request_id'],owner['request_id'])
        self.assertIn(peer_bridge.PEER_WARNING,answer['delivery_warning'])

    def test_unicode_envelope_rejected_before_reservation_without_consuming_id(self):
        self.setup_global();rid=uid()
        for text in ('😀'*1000,'\x01'*1000):
            with self.assertRaisesRegex(peer_bridge.PeerError,'4000'):
                self.send(text,rid)
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM shared_bridge_requests').fetchone()[0],0)
        self.assertEqual(self.send('Short fictional message.',rid)['request_id'],rid)

    def test_double_poll_and_busy_owner_share_one_atomic_claim(self):
        self.setup_global();sent=self.send();owner,_=self.request('A distinct owner question.')
        barrier=threading.Barrier(2);prepare=self.store.shared.prepare_mod_claim
        def together(row):
            value=prepare(row);barrier.wait(timeout=3);return value
        with patch.object(self.store.shared,'prepare_mod_claim',side_effect=together):
            with ThreadPoolExecutor(2) as pool:
                responses=list(pool.map(lambda _:self.global_poll(self.body['client_id']),range(2)))
        self.assertTrue(all(status==200 for status,_ in responses),responses)
        claims=[value['claim'] for _,value in responses if value['claim']]
        self.assertEqual(len(claims),1);self.assertEqual(claims[0]['request_id'],self.peer_row(sent['request_id'])['request_id'])
        self.assertIsNone(self.global_poll(self.body['client_id'])[1]['claim'])
        self.assertFalse(next(r for r in self.store.mods.rows() if r['request_id']==owner['request_id'])['launched'])
        self.event(claims[0]);self.assertEqual(self.global_poll(self.body['client_id'])[1]['claim']['request_id'],owner['request_id'])

    def test_ledger_and_attempt_write_rollback_together_then_single_retry_claim(self):
        self.setup_global();sent=self.send();put=self.store.mods.put
        def fail_attempt(row):
            if row.get('origin')=='peer' and row.get('mod_attempt_id'):
                raise sqlite3.OperationalError('Fictional persistence fault')
            return put(row)
        with patch.object(self.store.mods,'put',side_effect=fail_attempt):
            self.assertEqual(self.global_poll(self.body['client_id'])[0],400)
        self.assertIsNone(self.store.db.execute('SELECT launched FROM shared_bridge_requests').fetchone()[0])
        row=self.peer_row(sent['request_id']);self.assertFalse(row['launched']);self.assertNotIn('mod_attempt_id',row)
        self.assertEqual(self.global_poll(self.body['client_id'])[1]['claim']['request_id'],row['request_id'])
        self.assertIsNone(self.global_poll(self.body['client_id'])[1]['claim'])

    def test_preparation_outside_lock_and_transaction_revoked_credential_fails_row(self):
        self.setup_global();sent=self.send();prepare=self.store.shared.prepare_mod_claim
        def revoke(row):
            self.assertFalse(self.store.lock._is_owned());self.assertFalse(self.store.db.in_transaction)
            value=prepare(row)
            with self.store.lock,self.store.db:
                self.store.db.execute('UPDATE shared_bridge_credentials SET active=0 WHERE id=?',(self.credential['id'],))
            return value
        with patch.object(self.store.shared,'prepare_mod_claim',side_effect=revoke):
            status,result=self.global_poll(self.body['client_id'])
        self.assertEqual(status,200,result);self.assertIsNone(result['claim'])
        row=self.peer_row(sent['request_id']);self.assertEqual(row['state'],'failed');self.assertFalse(row['launched'])
        self.assertIsNone(self.store.db.execute('SELECT launched FROM shared_bridge_requests').fetchone()[0])

    def test_reconnect_during_preparation_rejects_stale_poll_then_new_generation_can_claim(self):
        self.setup_global();sent=self.send();prepare=self.store.shared.prepare_mod_claim
        def reconnect(row):
            value=prepare(row)
            status,new=self.http('/claude-mod/register',dict(self.body,connection_seq=1),token=self.grant['grant'])
            self.assertEqual(status,200,new);self.next_generation=new['generation']
            return value
        with patch.object(self.store.shared,'prepare_mod_claim',side_effect=reconnect):
            self.assertEqual(self.global_poll(self.body['client_id'])[0],409)
        self.assertFalse(self.peer_row(sent['request_id'])['launched'])
        self.generation=self.next_generation
        self.assertIsNotNone(self.global_poll(self.body['client_id'])[1]['claim'])

    def test_owner_claim_during_peer_preparation_wins_without_second_claim(self):
        self.setup_global();owner,_=self.request('Owner queued before the peer.');sent=self.send()
        prepare=self.store.shared.prepare_mod_claim;entered=False;nested=[]
        def owner_claim(row):
            nonlocal entered
            value=prepare(row)
            if not entered:
                entered=True
                nested.append(self.global_poll(self.body['client_id']))
            return value
        with patch.object(self.store.shared,'prepare_mod_claim',side_effect=owner_claim):
            status,result=self.global_poll(self.body['client_id'])
        self.assertEqual(status,200,result);self.assertIsNone(result['claim'])
        self.assertEqual(nested[0][0],200,nested)
        self.assertEqual(nested[0][1]['claim']['request_id'],owner['request_id'])
        self.assertFalse(self.peer_row(sent['request_id'])['launched'])
        self.assertIsNone(self.store.db.execute('SELECT launched FROM shared_bridge_requests').fetchone()[0])

    def test_grant_revoke_during_preparation_invalidates_poll_and_fails_unclaimed(self):
        self.setup_global();sent=self.send();prepare=self.store.shared.prepare_mod_claim
        def revoke(row):
            value=prepare(row)
            self.assertEqual(self.http('/claude-mod-revoke',{},owner=True)[0],200)
            return value
        with patch.object(self.store.shared,'prepare_mod_claim',side_effect=revoke):
            self.assertEqual(self.global_poll(self.body['client_id'])[0],403)
        row=self.peer_row(sent['request_id']);self.assertEqual(row['state'],'failed');self.assertFalse(row['launched'])
        self.assertIsNone(self.store.db.execute('SELECT launched FROM shared_bridge_requests').fetchone()[0])

    def test_two_same_project_sessions_project_drift_and_peer_lease(self):
        self.setup_global();sent=self.send()
        other=uid();server.AGENTS['c:'+other]=dict(self.agents[self.aid])
        status,enrollment=self.http('/claude-mod/register',dict(self.body,native_id=other,client_id=uid()),token=self.grant['grant'])
        self.assertEqual(status,200,enrollment)
        status,result=self.http('/claude-mod/poll',dict(native_id=other,project=self.project,
            generation=enrollment['generation'],client_id=enrollment['client_id'],accept=True),token=enrollment['token'])
        self.assertEqual(status,200,result);self.assertIsNone(result['claim']);self.assertEqual(result['requests'],[])
        server.AGENTS[self.aid]['cwd']='/fictional/different-project'
        self.assertFalse(self.store.mods.peer_capability(self.aid,server.AGENTS[self.aid])[0])
        self.assertIsNone(self.global_poll(self.body['client_id'])[1]['claim'])
        server.AGENTS[self.aid]['cwd']=self.project
        with self.store.lock,self.store.db:
            self.store.db.execute('UPDATE claude_mod_enrollments SET last_seen=0 WHERE agent=?',(self.aid,))
        self.assertFalse(self.store.mods.peer_capability(self.aid,server.AGENTS[self.aid])[0])
        self.assertEqual(self.global_poll(self.body['client_id'])[0],409)
        self.assertFalse(self.peer_row(sent['request_id'])['launched'])

    def test_failed_identity_preflight_does_not_break_poll_or_route_to_owner(self):
        self.setup_global();sent=self.send();self.transcript.unlink()
        status,result=self.global_poll(self.body['client_id'])
        self.assertEqual(status,200,result);self.assertIsNone(result['claim'])
        self.assertEqual(self.peer_row(sent['request_id'])['state'],'failed')
        self.assertEqual(self.store.snapshot()['requests'],[])

    def test_lost_claim_reconnect_retained_answer_and_generation_stable_directory_identity(self):
        self.setup_global();first=self.target();sent=self.send();claim=self.global_poll(self.body['client_id'])[1]['claim']
        status,new=self.http('/claude-mod/register',dict(self.body,connection_seq=1),token=self.grant['grant'])
        self.assertEqual(status,200,new);self.generation=new['generation']
        self.assertEqual(self.target()['identity_revision'],first['identity_revision'])
        self.assertIsNone(self.global_poll(self.body['client_id'])[1]['claim'])
        self.assertEqual(self.peer_row(sent['request_id'])['state'],'unknown')
        self.assertEqual(self.event(claim)[0],200)
        self.assertEqual(self.store.shared.result(sent['request_id'])['reply'],'Fictional reply.')

    def test_immutable_ledger_selection_must_match_prepared_claim(self):
        self.setup_global();sent=self.send()
        with self.store.lock,self.store.db:
            raw=self.store.db.execute('SELECT target_snapshot FROM shared_bridge_requests').fetchone()[0]
            snapshot=json.loads(raw);snapshot['identity_revision']='0'*64
            self.store.db.execute('UPDATE shared_bridge_requests SET target_snapshot=?',(json.dumps(snapshot),))
        status,result=self.global_poll(self.body['client_id'])
        self.assertEqual(status,200,result);self.assertIsNone(result['claim'])
        row=self.peer_row(sent['request_id']);self.assertEqual(row['state'],'failed');self.assertFalse(row['launched'])
        self.assertIsNone(self.store.db.execute('SELECT launched FROM shared_bridge_requests').fetchone()[0])

    def test_unbacked_and_legacy_peer_rows_never_claim_or_accept_event(self):
        self.setup_global();sent=self.send();row=self.peer_row(sent['request_id'])
        with self.store.lock,self.store.db:
            self.store.db.execute('DELETE FROM shared_bridge_requests')
        self.assertIsNone(self.global_poll(self.body['client_id'])[1]['claim'])
        self.assertEqual(self.event(dict(request_id=row['request_id'],attempt_id=uid()))[0],409)
        with self.store.lock,self.store.db:
            row.update(bridge_version=1,mod_attempt_id=uid(),launched=True,state='queued')
            self.store.mods.put(row)
        self.assertEqual(self.store.mods.rows(),[])
        self.assertEqual(self.event(dict(request_id=row['request_id'],attempt_id=row['mod_attempt_id']))[0],409)


    def test_idle_registration_owner_peer_and_status_without_room(self):
        self.target();server.AGENTS.clear()
        self.setup_global()
        self.assertEqual(server.AGENTS,{})
        target=self.target()
        self.assertEqual((target['supported_transport'],target['capability']),('claude_mod','reachable'))
        self.assertEqual(self.store.mods.setup_status({})['connected_chats'],1)
        result=self.send();poll=self.global_poll(self.body['client_id'])[1]
        self.assertIsNotNone(poll['claim'],'verified idle peer must claim without a room member')
        self.assertEqual(self.event(poll['claim'])[0],200)
        payload,owner=self.request('Fictional owner idle question')
        claim=self.global_poll(self.body['client_id'])[1]['claim']
        self.assertIsNotNone(claim,'verified idle owner request must claim without a room member')
        self.assertEqual(server.AGENTS,{})
        self.assertEqual(self.event(claim)[0],200)
        self.assertEqual(self.store.shared.result(result['request_id'])['state'],'answered')
        self.assertEqual(self.store.mods.setup_status({})['connected_chats'],1)
        entry=self.store.mods.get(self.registration['enrollment_id'])
        self.assertFalse(self.store.mods.peer_ready(dict(entry,last_seen=time.time()-13)),'an expired lease cannot be peer-ready')
        self.store.db.execute('UPDATE claude_mod_enrollments SET last_seen=0');self.store.db.commit()
        self.assertEqual(self.store.mods.setup_status({})['connected_chats'],0)
        self.assertEqual(self.target()['capability'],'unavailable')

    def test_idle_auth_filesystem_boundaries_and_revocation_during_prepare(self):
        self.target();server.AGENTS.clear()
        status,setup=self.http('/claude-mod-setup',{'peer_receive':True},owner=True)
        _,grant=self.http('/claude-mod/setup',{'code':setup['pair_code']})
        original=self.store.shared.metadata_target
        realpath=chat_directory.os.path.realpath
        def outside(path,*args,**kwargs):
            self.assertFalse(self.store.lock._is_owned(),'filesystem must be outside store lock')
            self.assertFalse(self.store.db.in_transaction,'filesystem must be outside transaction')
            return realpath(path,*args,**kwargs)
        with patch.object(self.store.shared,'metadata_target',side_effect=AssertionError('unauthenticated discovery')):
            self.assertEqual(self.register_global(dict(grant,grant='0'*64))[0],403)
        with patch.object(chat_directory.os.path,'realpath',side_effect=outside):
            self.assertEqual(self.register_global(grant)[0],200)
        # A fresh client cannot commit against a grant revoked after its pinned read.
        with self.store.lock,self.store.db:
            self.store.db.execute('UPDATE claude_mod_enrollments SET active=0,generation=NULL')
        def revoke(*args):
            proof=original(*args)
            self.store.mods.revoke()
            return proof
        with patch.object(self.store.shared,'metadata_target',side_effect=revoke):
            self.assertEqual(self.register_global(grant)[0],403)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM claude_mod_enrollments WHERE active=1').fetchone()[0],0)

    def test_idle_registration_preparation_rechecks_host_room_and_metadata(self):
        self.target();server.AGENTS.clear()
        _,setup=self.http('/claude-mod-setup',{'peer_receive':True},owner=True)
        _,grant=self.http('/claude-mod/setup',{'code':setup['pair_code']})
        original=self.store.shared.metadata_target
        def competing(*args):
            proof=original(*args)
            body=dict(native_id=self.sid,project=self.project,client_id=uid(),connection_seq=0,peer_protocol=1)
            with self.store.lock,self.store.db:
                self.store.mods.register(body,grant['grant'],{},proof)
            return proof
        with patch.object(self.store.shared,'metadata_target',side_effect=competing):
            status,value,_=self.register_global(grant,peer_protocol=1)
            self.assertEqual((status,value['error_code']),(409,'host_conflict'))
        with self.store.lock,self.store.db:
            self.store.db.execute('UPDATE claude_mod_enrollments SET active=0,generation=NULL')
        def room_change(*args):
            proof=original(*args)
            server.AGENTS[self.aid]=dict(self.agents[self.aid],sub=True)
            return proof
        with patch.object(self.store.shared,'metadata_target',side_effect=room_change):
            status,value,_=self.register_global(grant,peer_protocol=1)
            self.assertEqual((status,value['error_code']),(409,'identity_rejected'))
        server.AGENTS.clear()
        def metadata_change(*args):
            proof=original(*args)
            replacement=self.transcript.with_suffix('.replacement')
            replacement.write_text(json.dumps(dict(sessionId=self.sid,cwd=self.project,isSidechain=True))+'\n')
            os.replace(replacement,self.transcript)
            with self.assertRaises(chat_directory.DirectoryError): self.directory.claude_metadata(self.aid,self.project)
            return proof
        with patch.object(self.store.shared,'metadata_target',side_effect=metadata_change):
            status,value,_=self.register_global(grant,peer_protocol=1)
            self.assertEqual((status,value['error_code']),(409,'not_observed'))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM claude_mod_enrollments WHERE active=1').fetchone()[0],0)

    def test_idle_present_room_vetoes_and_prepare_race(self):
        self.target();server.AGENTS.clear();self.setup_global()
        result=self.send()
        original=self.store.shared.prepare_mod_claim
        def veto(row):
            prepared=original(row)
            server.AGENTS[self.aid]=dict(self.agents[self.aid],parent='c:'+uid())
            return prepared
        with patch.object(self.store.shared,'prepare_mod_claim',side_effect=veto):
            self.assertIsNone(self.global_poll(self.body['client_id'])[1]['claim'])
        self.assertIsNone(self.store.db.execute('SELECT launched FROM shared_bridge_requests WHERE client_id=?',(result['request_id'],)).fetchone()[0])
        self.assertEqual(self.target()['capability'],'unavailable')
        self.assertEqual(self.store.mods.setup_status(dict(server.AGENTS))['connected_chats'],0)
        with self.assertRaises(peer_bridge.PeerError): self.send('Another fictional question')
        status,_=self.http('/agent-message',dict(agent=self.aid,question='Owner veto',request_id=uid()),owner=True)
        self.assertEqual(status,503)
        server.AGENTS.clear()
        for flags in [dict(runtime='Codex'),dict(sub=True),dict(observed='process'),dict(cwd='/fictional/wrong')]:
            server.AGENTS[self.aid]=dict(self.agents[self.aid],**flags)
            self.assertEqual(self.store.mods.setup_status(dict(server.AGENTS))['connected_chats'],0)
            self.assertEqual(self.target()['capability'],'unavailable')
        server.AGENTS.clear()

    def test_idle_owner_only_membership_never_grants_peer_receiving(self):
        self.target();server.AGENTS.clear();self.setup_global(consent=False,protocol=0)
        self.assertEqual(self.store.mods.setup_status({})['connected_chats'],1)
        self.assertEqual(self.target()['capability'],'unavailable')
        with self.assertRaises(peer_bridge.PeerError): self.send()
        self.request('Fictional owner-only question')
        self.assertIsNotNone(self.global_poll(self.body['client_id'])[1]['claim'])

    def test_idle_missing_receiver_is_truthful_and_metadata_is_not_liveness(self):
        target=self.target();server.AGENTS.clear()
        self.assertEqual(target['capability'],'unavailable')
        self.assertIn('official plugin',target['reason'])
        self.assertNotIn('identity can change',target['reason'])
        with self.assertRaises(peer_bridge.PeerError): self.send()
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM shared_bridge_requests').fetchone()[0],0)


if __name__=='__main__':unittest.main()
