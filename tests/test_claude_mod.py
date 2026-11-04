"""Official-mod owner HTTP/outbox contract, fictional service identities only."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
import sqlite3
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import claude_mod as mod
import officer_messaging as messaging
import server


def uid():
    return str(uuid.uuid4())


@unittest.skipIf(os.name == 'nt', 'Owner outbox requires POSIX private storage')
class ClaudeModTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.project = os.path.realpath(self.temp.name)
        self.sid, self.aid = uid(), None
        self.aid = 'c:'+self.sid
        self.agents = {self.aid: {'runtime': 'Claude', 'name': 'Fictional', 'cwd': self.project}}
        self.store = messaging.MessagingStore(str(self.base/'state'), str(self.base/'sessions'))
        self.store.codex_available = False
        self.previous = server.MESSAGING, server.KEY, server.PORT, dict(server.AGENTS)
        server.MESSAGING, server.KEY = self.store, 'fictional-owner-key'
        server.AGENTS.clear(); server.AGENTS.update(self.agents)
        self.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.H)
        server.PORT = self.httpd.server_port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.token = ''
        self.generation = ''

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join()
        server.MESSAGING, server.KEY, server.PORT = self.previous[:3]
        server.AGENTS.clear(); server.AGENTS.update(self.previous[3])
        self.store.close(); self.temp.cleanup()

    def http(self, path, data, owner=False, token=None, **headers):
        headers = dict(headers)
        if owner:
            headers['X-Live-Room-Key'] = server.KEY
        if token is not None:
            headers['Authorization'] = 'Bearer '+token
        conn = http.client.HTTPConnection('127.0.0.1', server.PORT, timeout=3)
        conn.request('POST', path, json.dumps(data), headers)
        response = conn.getresponse()
        raw = response.read(); status = response.status
        conn.close()
        try: return status, json.loads(raw)
        except ValueError: return status, raw.decode()

    def enroll(self):
        status, pair = self.http('/claude-mod-pair', {'agent': self.aid}, owner=True)
        self.assertEqual(status, 200, pair)
        self.assertEqual(pair['command'], f"/watch-connect {server.PORT}:"+pair['pair_code'])
        status, enrollment = self.http('/claude-mod/enroll', {'code': pair['pair_code'], 'native_id': self.sid, 'project': self.project})
        self.assertEqual(status, 200, enrollment)
        self.token, self.generation = enrollment['token'], enrollment['generation']
        return pair, enrollment

    def request(self, question='How is the fictional work going?', request_id=None):
        payload = dict(agent=self.aid, question=question, request_id=request_id or uid())
        status, row = self.http('/agent-message', payload, owner=True)
        self.assertEqual(status, 200, row)
        return payload, row

    def poll(self, accept=True, **override):
        return self.http('/claude-mod/poll', dict(generation=self.generation, native_id=self.sid, project=self.project, accept=accept, **override), token=self.token)

    def event(self, claim, kind, **extra):
        return self.http('/claude-mod/event', dict(generation=self.generation, native_id=self.sid, request_id=claim['request_id'], attempt_id=claim['attempt_id'], kind=kind, **extra), token=self.token)

    def answer(self, claim, turn=None, text='Fictional completion.'):
        return self.event(claim, 'answered', turn_id=turn or uid(), answer='WATCH_REPLY '+claim['request_id']+'\n'+text)

    def row(self, rid):
        return next(r for r in self.store.snapshot()['requests'] if r['request_id'] == rid)

    def reconnect(self):
        status, value = self.http('/claude-mod/connect', {'native_id': self.sid, 'project': self.project}, token=self.token)
        self.assertEqual(status, 200, value)
        self.generation = value['generation']
        return value

    def test_pair_auth_schema_native_project_expiry_and_single_use(self):
        self.assertEqual(self.http('/claude-mod-pair', {'agent':self.aid})[0], 403)
        self.assertEqual(self.http('/claude-mod-pair', {'agent':self.aid,'token':'forged'},owner=True)[0],400)
        self.assertEqual(self.http('/claude-mod-pair', {'agent':'x:'+uid()},owner=True)[0],400)
        pair, enrollment = self.enroll()
        self.assertNotIn('token', pair)
        body = dict(code=pair['pair_code'],native_id=self.sid,project=self.project)
        self.assertEqual(self.http('/claude-mod/enroll',body)[0],403)
        self.assertNotIn(enrollment['token'],json.dumps(self.store.snapshot()))
        stored = self.store.db.execute('SELECT token_hash FROM claude_mod_enrollments').fetchone()[0]
        self.assertNotEqual(stored,enrollment['token'])
        pair = self.store.mods.pair(self.aid,self.agents,server.PORT)
        self.assertEqual(self.http('/claude-mod/enroll',dict(body,code=pair['pair_code'],native_id=uid()))[0],403)
        self.assertEqual(self.http('/claude-mod/enroll',dict(body,code=pair['pair_code'],project='/fictional/wrong'))[0],403)
        self.store.db.execute('UPDATE claude_mod_pairs SET expires=0');self.store.db.commit()
        self.assertEqual(self.http('/claude-mod/enroll',dict(body,code=pair['pair_code']))[0],403)

    def test_owner_http_to_claim_to_exact_ordinary_reply_without_codex(self):
        self.enroll()
        self.assertTrue(self.store.owner_capabilities(self.aid,self.agents)['actions']['queue']['available'])
        self.assertTrue(self.store.snapshot()['capabilities']['claude'])
        payload, row = self.request()
        self.assertEqual(row['state'],'sending')
        status, poll = self.poll();self.assertEqual(status,200,poll)
        claim = poll['claim']
        self.assertEqual(claim['native_id'],self.sid)
        self.assertEqual(claim['envelope'],messaging.envelope(payload['request_id'],payload['question']))
        self.assertNotIn('reply tool',claim['envelope'])
        turn = uid()
        self.assertEqual(self.event(claim,'started',turn_id=turn)[1]['state'],'awaiting_reply')
        self.assertEqual(self.answer(claim,turn)[1]['state'],'answered')
        row = self.row(payload['request_id'])
        self.assertEqual((row['state'],row['reply']),('answered','Fictional completion.'))
        self.assertIsNone(self.poll()[1]['claim'])

    def test_tick_never_dispatches_polls_or_overwrites_mod_rows(self):
        self.enroll();payload,_=self.request()
        with patch.object(self.store,'_send',side_effect=AssertionError('legacy send')), patch.object(self.store,'_poll',side_effect=AssertionError('legacy poll')):
            self.store.tick()
            self.assertEqual(self.row(payload['request_id'])['state'],'sending')
            claim=self.poll()[1]['claim'];self.answer(claim)
            self.store.tick()
        self.assertEqual(self.row(payload['request_id'])['state'],'answered')

    def test_busy_pipeline_waits_distinct_rows_and_duplicate_clicks_do_not_reclaim(self):
        self.enroll();payload,_=self.request()
        self.assertEqual(self.http('/agent-message',payload,owner=True)[0],200)
        self.assertEqual(self.http('/agent-message',dict(payload,question='Conflict'),owner=True)[0],409)
        second,_=self.request('A distinct later instruction')
        self.assertIsNone(self.poll(False)[1]['claim'])
        first=self.poll()[1]['claim']
        self.assertEqual(first['request_id'],payload['request_id'])
        self.assertIsNone(self.poll()[1]['claim'])
        self.assertIsNone(self.poll(False)[1]['claim'])
        self.answer(first)
        self.assertEqual(self.poll()[1]['claim']['request_id'],second['request_id'])

    def test_atomic_concurrent_claim_and_owner_duplicate(self):
        self.enroll();payload,_=self.request()
        with ThreadPoolExecutor(6) as pool:
            results=list(pool.map(lambda _:self.poll(),range(12)))
        claims=[v['claim'] for status,v in results if status==200 and v['claim']]
        self.assertEqual(len(claims),1)
        self.assertEqual(len(self.store._rows()),1)
        self.assertEqual(self.http('/agent-message',payload,owner=True)[1]['request_id'],payload['request_id'])

    def test_unknown_after_started_releases_next_distinct_message_without_resubmit(self):
        self.enroll()
        first,_=self.request()
        second,_=self.request('Distinct instruction after the earlier turn ends without a reply')
        claim=self.poll()[1]['claim']
        turn=uid()
        self.assertEqual(self.event(claim,'started',turn_id=turn)[1]['state'],'awaiting_reply')
        self.assertEqual(self.event(claim,'unknown')[1]['state'],'unknown')
        self.assertEqual(self.row(first['request_id'])['state'],'unknown')
        self.assertEqual(self.row(first['request_id']).get('reply',''),'')
        self.assertEqual(self.poll()[1]['claim']['request_id'],second['request_id'])
        self.assertEqual(self.answer(claim,turn)[1]['state'],'answered','late matching reply remains recoverable')

    def test_completion_beats_start_and_settlement_monotonic_idempotent_conflict(self):
        self.enroll();payload,_=self.request();claim=self.poll()[1]['claim'];turn=uid()
        complete=self.answer(claim,turn)
        self.assertEqual(complete[0],200)
        self.assertEqual(self.answer(claim,turn),complete)
        for kind,extra in [('submitted',{}),('started',{'turn_id':turn}),('dropped',{}),('unknown',{})]:
            self.assertEqual(self.event(claim,kind,**extra)[1]['state'],'answered')
        self.assertEqual(self.answer(claim,uid())[0],409)
        self.assertEqual(self.answer(claim,turn,text='Changed answer')[0],409)
        self.assertEqual(self.row(payload['request_id'])['reply'],'Fictional completion.')

    def test_wrong_claim_origin_reply_marker_and_turn_do_not_bind(self):
        self.enroll();payload,_=self.request();claim=self.poll()[1]['claim'];turn=uid()
        wrong=dict(claim,attempt_id=uid())
        self.assertEqual(self.event(wrong,'started',turn_id=turn)[0],409)
        self.assertEqual(self.event(claim,'answered',turn_id=turn,answer='Unrelated answer')[0],409)
        bad='WATCH_REPLY '+payload['request_id']+'\nWATCH_REPLY '+uid()
        self.assertEqual(self.event(claim,'answered',turn_id=turn,answer=bad)[0],409)
        self.event(claim,'started',turn_id=turn)
        self.assertEqual(self.event(claim,'submitted')[1]['state'],'awaiting_reply')
        self.assertEqual(self.event(claim,'unknown')[1]['state'],'unknown')
        self.assertEqual(self.answer(claim,uid())[0],409)
        self.assertEqual(self.row(payload['request_id'])['state'],'unknown')
        self.assertEqual(self.event(claim,'answered',turn_id=turn,answer='x'*16001)[0],400)

    def test_peer_row_never_claimed_or_exposed_as_owner_mod_history(self):
        self.enroll();payload,_=self.request()
        with self.store.lock,self.store.db:
            row=next(r for r in self.store._rows() if r['request_id']==payload['request_id'])
            row['origin']='peer';row['created']=time.time();self.store.mods.put(row)
        result=self.poll()[1]
        self.assertIsNone(result['claim'])
        self.assertEqual(result['requests'],[])
        self.assertEqual(self.store.snapshot()['requests'],[])

    def test_service_auth_host_origin_generation_target_and_scope(self):
        self.enroll()
        body=dict(generation=self.generation,native_id=self.sid,project=self.project,accept=True)
        for token in ('bad',server.KEY):
            self.assertEqual(self.http('/claude-mod/poll',body,token=token)[0],403)
        self.assertEqual(self.http('/claude-mod/poll',body,owner=True)[0],403)
        for headers in ({'Host':'localhost:'+str(server.PORT)},{'Origin':'http://127.0.0.1:'+str(server.PORT)}):
            self.assertEqual(self.http('/claude-mod/poll',body,token=self.token,**headers)[0],403)
        self.assertEqual(self.http('/claude-mod/poll',dict(body,generation=uid()),token=self.token)[0],409)
        self.assertEqual(self.http('/claude-mod/poll',dict(body,native_id=uid()),token=self.token)[0],409)
        self.assertEqual(self.http('/claude-mod/poll',dict(body,origin='owner'),token=self.token)[0],400)
        self.assertEqual(self.http('/claude-mod/send_message',body,token=self.token)[0],403)

    def test_disconnect_reconnect_status_and_late_reply_replay_no_resubmit(self):
        self.enroll();payload,_=self.request();claim=self.poll()[1]['claim'];old_generation=self.generation
        self.assertEqual(self.http('/claude-mod/disconnect',dict(generation=self.generation,native_id=self.sid),token=self.token)[0],200)
        self.assertFalse(self.store.owner_capabilities(self.aid,self.agents)['actions']['queue']['available'])
        self.assertEqual(self.row(payload['request_id'])['state'],'unknown')
        self.reconnect();self.assertNotEqual(old_generation,self.generation)
        result=self.poll()[1]
        self.assertIsNone(result['claim'])
        self.assertEqual(result['requests'][0]['attempt_id'],claim['attempt_id'])
        self.assertNotIn('envelope',result['requests'][0])
        self.assertEqual(self.answer(claim)[1]['state'],'answered')
        later,_=self.request('Distinct new explicit request after reconnect')
        self.assertEqual(self.poll()[1]['claim']['request_id'],later['request_id'])

    def test_lost_claim_response_reconnect_never_returns_same_payload(self):
        self.enroll();payload,_=self.request();claim=self.poll()[1]['claim']
        self.assertIsNone(self.poll()[1]['claim'])
        self.reconnect()
        status,result=self.poll()
        self.assertEqual(status,200)
        self.assertIsNone(result['claim'])
        self.assertEqual(result['requests'][0]['state'],'unknown')
        self.assertEqual(result['requests'][0]['request_id'],claim['request_id'])

    def test_watch_restart_preserves_waiting_but_attempted_unknown_no_reclaim(self):
        self.enroll();first,_=self.request();claim=self.poll()[1]['claim'];second,_=self.request('Wait through restart')
        self.store.close()
        self.store=messaging.MessagingStore(str(self.base/'state'),str(self.base/'sessions'))
        self.store.codex_available=False;server.MESSAGING=self.store
        self.assertEqual(self.row(first['request_id'])['state'],'unknown')
        self.assertEqual(self.row(second['request_id'])['state'],'sending')
        self.assertFalse(self.store.owner_capabilities(self.aid,self.agents)['actions']['queue']['available'])
        self.reconnect()
        self.assertEqual(self.poll()[1]['claim']['request_id'],second['request_id'])
        self.assertEqual(self.row(first['request_id'])['state'],'unknown')
        self.answer(claim)
        self.assertEqual(self.row(first['request_id'])['state'],'answered')

    def test_unclaimed_expiry_is_terminal_but_attempted_expiry_can_reply(self):
        self.enroll();first,_=self.request();claim=self.poll()[1]['claim'];second,_=self.request('Never claimed')
        with self.store.lock,self.store.db:
            for row in self.store._rows():
                row['created']=time.time()-mod.TTL-1;self.store.mods.put(row)
        self.assertIsNone(self.poll()[1]['claim'])
        self.assertEqual(self.row(second['request_id'])['state'],'failed')
        self.assertIn('Nothing was submitted',self.row(second['request_id'])['error'])
        self.assertEqual(self.row(first['request_id'])['state'],'expired')
        self.assertEqual(self.answer(claim)[1]['state'],'answered')

    def test_lease_lapse_requires_reconnect_never_retries_pending(self):
        self.enroll();payload,_=self.request();self.poll()
        self.store.db.execute('UPDATE claude_mod_enrollments SET last_seen=0');self.store.db.commit()
        self.assertFalse(self.store.owner_capabilities(self.aid,self.agents)['actions']['queue']['available'])
        self.assertEqual(self.row(payload['request_id'])['state'],'unknown')
        self.assertEqual(self.poll()[0],409)
        self.reconnect();self.assertIsNone(self.poll()[1]['claim'])

    def test_repair_revokes_old_token_without_redirecting_old_waiting_request(self):
        self.enroll();payload,_=self.request();oldtoken=self.token
        self.enroll()
        self.assertEqual(self.http('/claude-mod/connect',dict(native_id=self.sid,project=self.project),token=oldtoken)[0],403)
        self.assertIsNone(self.poll()[1]['claim'])
        self.assertEqual(self.row(payload['request_id'])['state'],'sending')

    def global_setup(self):
        status, pair = self.http('/claude-mod-setup', {}, owner=True)
        self.assertEqual(status, 200, pair)
        self.assertEqual(pair['command'], f'/watch-setup {server.PORT}:'+pair['pair_code'])
        status, grant = self.http('/claude-mod/setup', {'code':pair['pair_code']})
        self.assertEqual(status, 200, grant)
        return pair, grant

    def register_global(self, grant, client=None, seq=0, **overrides):
        body = dict(native_id=self.sid, project=self.project, client_id=client or uid(), connection_seq=seq)
        body.update(overrides)
        status, value = self.http('/claude-mod/register', body, token=grant['grant'])
        if status == 200:
            self.token, self.generation = value['token'], value['generation']
        return status, value, body

    def global_poll(self, client, accept=True):
        return self.http('/claude-mod/poll', dict(generation=self.generation,native_id=self.sid,
            project=self.project,accept=accept,client_id=client),token=self.token)

    def test_global_setup_auth_single_use_status_and_secret_classes(self):
        for path in ('/claude-mod-setup','/claude-mod-setup-status','/claude-mod-revoke'):
            self.assertEqual(self.http(path,{})[0],403)
            self.assertEqual(self.http(path,{'forged':True},owner=True)[0],400)
        pair, grant = self.global_setup()
        self.assertEqual(self.http('/claude-mod/setup',{'code':pair['pair_code']})[0],403)
        status, enrollment, body = self.register_global(grant)
        self.assertEqual(status,200,enrollment)
        for token in (server.KEY,grant['grant']):
            self.assertEqual(self.http('/claude-mod/poll',dict(generation=self.generation,native_id=self.sid,
                project=self.project,accept=True,client_id=body['client_id']),token=token)[0],403)
        self.assertEqual(self.http('/claude-mod/register',body,token=self.token)[0],403)
        self.assertEqual(self.http('/claude-mod/connect',dict(native_id=self.sid,project=self.project),token=self.token)[0],409)
        self.assertEqual(self.poll()[0],400,'Derived poll requires client membership')
        snapshot=json.dumps(self.store.snapshot())
        for secret in (pair['pair_code'],grant['grant'],self.token):
            self.assertNotIn(secret,snapshot)
        self.assertEqual(self.http('/claude-mod-setup-status',{},owner=True)[1],
            {'version':1,'authorized':True,'connected_chats':1,'peer_receive':False})
        self.assertEqual(self.store.owner_capabilities(self.aid,self.agents)['claude_setup'],
            {'authorized':True,'state':'connected','peer_receive':False})
        self.assertEqual(self.http('/agent-message',dict(agent=self.aid,question='Forbidden',request_id=uid()),token=grant['grant'])[0],403)
        for path, data in (('/claude-mod/setup',{'code':pair['pair_code']}),('/claude-mod/register',body)):
            for headers in ({'Origin':'http://127.0.0.1:'+str(server.PORT)}, {'Host':'localhost:'+str(server.PORT)}):
                self.assertEqual(self.http(path,data,token=grant['grant'] if path.endswith('register') else None,**headers)[0],403)
        stored=json.dumps(list(self.store.db.execute('SELECT * FROM claude_mod_grants'))+
                          list(self.store.db.execute('SELECT * FROM claude_mod_enrollments')))
        self.assertNotIn(grant['grant'],stored)
        self.assertNotIn(self.token,stored)

    def test_global_registration_sequence_retry_host_handover_and_receipts(self):
        _,grant=self.global_setup()
        status,first,body=self.register_global(grant)
        self.assertEqual(status,200,first)
        payload,_=self.request();claim=self.global_poll(body['client_id'])[1]['claim']
        second,_=self.request('A distinct instruction after an ambiguous claim')
        with ThreadPoolExecutor(4) as pool:
            retries=list(pool.map(lambda _:self.http('/claude-mod/register',body,token=grant['grant']),range(8)))
        self.assertTrue(all(status==200 and value==first for status,value in retries))
        self.assertEqual(self.row(payload['request_id'])['state'],'queued')
        before=self.store.mods.authenticate(self.token)['last_seen']
        self.http('/claude-mod/register',body,token=grant['grant'])
        self.assertEqual(self.store.mods.authenticate(self.token)['last_seen'],before,'Lost-response retries do not extend readiness')
        self.assertEqual(self.register_global(grant)[1]['error_code'],'host_conflict')
        status,new,seqbody=self.register_global(grant,body['client_id'],1)
        self.assertEqual(status,200,new)
        self.assertEqual(new['enrollment_id'],first['enrollment_id'])
        self.assertEqual(new['token'],first['token'])
        self.assertNotEqual(new['generation'],first['generation'])
        self.assertEqual(self.row(payload['request_id'])['state'],'unknown')
        self.assertEqual(self.http('/claude-mod/register',body,token=grant['grant'])[1]['error_code'],'stale_connection')
        self.assertEqual(self.http('/claude-mod/register',seqbody,token=grant['grant'])[1],new)
        self.store.db.execute('UPDATE claude_mod_enrollments SET last_seen=0');self.store.db.commit()
        status,handoff,other=self.register_global(grant,seq=4)
        self.assertEqual(status,200,handoff)
        self.assertEqual(handoff['enrollment_id'],first['enrollment_id'])
        self.assertEqual(self.global_poll(body['client_id'])[0],409)
        nextclaim=self.global_poll(other['client_id'])[1]['claim']
        self.assertEqual(nextclaim['request_id'],second['request_id'])
        self.assertNotEqual(nextclaim['request_id'],claim['request_id'])
        late=dict(generation=self.generation,native_id=self.sid,client_id=other['client_id'],
            request_id=claim['request_id'],attempt_id=claim['attempt_id'],kind='answered',turn_id=uid(),
            answer='WATCH_REPLY '+claim['request_id']+'\nLate exact completion')
        self.assertEqual(self.http('/claude-mod/event',late,token=self.token)[1]['state'],'answered')

    def test_global_eligibility_drift_stops_claims_preserves_event_recovery(self):
        _,grant=self.global_setup()
        for flags in ({'runtime':'Codex'},{'sub':True},{'parent':'c:'+uid()},{'observed':True},{'cwd':'/fictional/wrong'}):
            server.AGENTS[self.aid]=dict(self.agents[self.aid],**flags)
            status,value,_=self.register_global(grant)
            self.assertEqual((status,value['error_code']),(409,'identity_rejected'))
        server.AGENTS.pop(self.aid)
        self.assertEqual(self.register_global(grant)[1]['error_code'],'not_observed')
        server.AGENTS.update(self.agents)
        _,_,body=self.register_global(grant)
        first,_=self.request('A completion remains recoverable after eligibility changes')
        claim=self.global_poll(body['client_id'])[1]['claim']
        payload,_=self.request()
        server.AGENTS[self.aid]['parent']='c:'+uid()
        self.assertIsNone(self.global_poll(body['client_id'])[1]['claim'])
        self.assertFalse(self.store.owner_capabilities(self.aid,dict(server.AGENTS))['actions']['queue']['available'])
        self.assertEqual(self.row(payload['request_id'])['state'],'sending')
        completed=dict(generation=self.generation,native_id=self.sid,client_id=body['client_id'],kind='answered',
            request_id=first['request_id'],attempt_id=claim['attempt_id'],turn_id=uid(),
            answer='WATCH_REPLY '+first['request_id']+'\nExact completion after eligibility drift')
        self.assertEqual(self.http('/claude-mod/event',completed,token=self.token)[1]['state'],'answered')
        self.assertIsNone(self.global_poll(body['client_id'])[1]['claim'])

    def test_global_revoke_replacement_manual_compatibility_and_no_redirect(self):
        self.enroll();manual_token=self.token
        _,grant=self.global_setup()
        self.assertEqual(self.register_global(grant)[1]['error_code'],'manual_conflict')
        self.assertEqual(self.http('/claude-mod-revoke',{},owner=True)[0],200)
        self.assertEqual(self.http('/claude-mod/connect',dict(native_id=self.sid,project=self.project),token=manual_token)[0],200)
        self.store.db.execute('UPDATE claude_mod_enrollments SET active=0,generation=NULL');self.store.db.commit()
        _,grant=self.global_setup();_,enrollment,body=self.register_global(grant)
        payload,_=self.request();claim=self.global_poll(body['client_id'])[1]['claim']
        queued,_=self.request('Unclaimed on revoke')
        _,newgrant=self.global_setup()
        self.assertEqual(self.register_global(grant)[0],403)
        self.assertEqual(self.global_poll(body['client_id'])[0],403)
        self.assertEqual(self.row(payload['request_id'])['state'],'unknown')
        self.assertEqual(self.row(queued['request_id'])['state'],'failed')
        self.assertEqual(self.register_global(newgrant)[0],200)
        self.assertIsNone(self.global_poll(self.store.mods.authenticate(self.token)['client_id'])[1]['claim'])
        self.assertNotEqual(self.store.mods.authenticate(self.token)['id'],enrollment['enrollment_id'])

    def test_global_setup_expiry_mint_without_revocation_and_lost_response_recovery(self):
        _,grant=self.global_setup();self.register_global(grant)
        _,pending=self.http('/claude-mod-setup',{},owner=True)
        self.assertEqual(self.http('/claude-mod-setup-status',{},owner=True)[1]['connected_chats'],1)
        self.store.db.execute('UPDATE claude_mod_setup_codes SET expires=0');self.store.db.commit()
        self.assertEqual(self.http('/claude-mod/setup',{'code':pending['pair_code']})[0],403)
        _,pending=self.http('/claude-mod-setup',{},owner=True)
        _,lost=self.http('/claude-mod/setup',{'code':pending['pair_code']})
        self.assertEqual(self.http('/claude-mod/setup',{'code':pending['pair_code']})[0],403)
        _,replacement=self.global_setup()
        self.assertEqual(self.register_global(lost)[0],403)
        self.assertEqual(self.register_global(replacement)[0],200)

    def test_additive_legacy_database_migration_preserves_manual_token_and_requests(self):
        _,enrollment=self.enroll();payload,_=self.request()
        old_token=self.token
        self.store.close()
        db=sqlite3.connect(self.base/'state'/'officer-messages.sqlite3')
        legacy=list(db.execute('SELECT id,token_hash,agent,project,generation,last_seen,active FROM claude_mod_enrollments'))
        db.execute('DROP TABLE claude_mod_enrollments')
        db.execute('''CREATE TABLE claude_mod_enrollments (id TEXT PRIMARY KEY,token_hash TEXT NOT NULL UNIQUE,
            agent TEXT NOT NULL,project TEXT NOT NULL,generation TEXT,last_seen REAL NOT NULL,active INTEGER NOT NULL)''')
        db.executemany('INSERT INTO claude_mod_enrollments VALUES (?,?,?,?,?,?,?)',legacy)
        db.commit();db.close()
        self.store=messaging.MessagingStore(str(self.base/'state'),str(self.base/'sessions'))
        self.store.codex_available=False;server.MESSAGING=self.store
        entry=self.store.mods.authenticate(old_token)
        self.assertIsNone(entry['grant_id'])
        self.assertEqual(entry['id'],enrollment['enrollment_id'])
        self.reconnect()
        self.assertEqual(self.poll()[1]['claim']['request_id'],payload['request_id'])

    def test_global_two_chats_same_project_restart_and_stable_registration(self):
        _,grant=self.global_setup();_,first,body=self.register_global(grant)
        firstpayload,_=self.request();self.global_poll(body['client_id'])
        waiting,_=self.request('Survive restart unclaimed')
        other=uid();server.AGENTS['c:'+other]=dict(self.agents[self.aid])
        status,second=self.http('/claude-mod/register',dict(body,native_id=other,client_id=uid()),token=grant['grant'])
        self.assertEqual(status,200,second)
        self.assertNotEqual(second['token'],first['token'])
        self.assertNotEqual(second['enrollment_id'],first['enrollment_id'])
        self.store.close();self.store=messaging.MessagingStore(str(self.base/'state'),str(self.base/'sessions'))
        self.store.codex_available=False;server.MESSAGING=self.store
        status,restarted=self.http('/claude-mod/register',body,token=grant['grant'])
        self.assertEqual(status,200,restarted)
        self.assertEqual(restarted['enrollment_id'],first['enrollment_id'])
        self.assertNotEqual(restarted['generation'],first['generation'])
        self.token,self.generation=restarted['token'],restarted['generation']
        self.assertEqual(self.row(firstpayload['request_id'])['state'],'unknown')
        self.assertEqual(self.global_poll(body['client_id'])[1]['claim']['request_id'],waiting['request_id'])

    def test_global_concurrent_membership_and_bounded_strict_identity(self):
        _,grant=self.global_setup()
        bodies=[dict(native_id=self.sid,project=self.project,client_id=uid(),connection_seq=0) for _ in range(8)]
        with ThreadPoolExecutor(4) as pool:
            results=list(pool.map(lambda body:self.http('/claude-mod/register',body,token=grant['grant']),bodies))
        self.assertEqual(sum(status==200 for status,_ in results),1)
        self.assertTrue(all(status==200 or status==409 and value['error_code']=='host_conflict' for status,value in results))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM claude_mod_enrollments WHERE active=1').fetchone()[0],1)
        for seq in (-1,True,1.5,9007199254740992):
            self.assertEqual(self.http('/claude-mod/register',dict(bodies[0],connection_seq=seq),token=grant['grant'])[0],400)
        other=uid();server.AGENTS['c:'+other]=dict(self.agents[self.aid])
        with patch.object(mod,'MAX_ENROLLMENTS',1):
            self.assertEqual(self.http('/claude-mod/register',dict(bodies[0],native_id=other),token=grant['grant'])[0],429)


if __name__=='__main__':unittest.main()
