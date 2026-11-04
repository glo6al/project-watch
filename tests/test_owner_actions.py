#!/usr/bin/env python3
"""Real owner HTTP and durable gateway tests; fake model/bridge, no native sends."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import officer_messaging as messaging
import server


@unittest.skipIf(os.name == 'nt', 'Messaging storage requires POSIX')
class OwnerActionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.sessions = self.base / 'sessions'; self.sessions.mkdir()
        self.aid = 'x:' + str(uuid.uuid4()); self.cid = 'c:' + str(uuid.uuid4())
        self.turn = str(uuid.uuid4())
        path = self.sessions / ('rollout-' + self.aid[2:] + '.jsonl')
        records = [{'type':'session_meta','payload':{'id':self.aid[2:],'cwd':self.tmp.name}},
                   {'type':'event_msg','payload':{'type':'task_started','turn_id':self.turn}}]
        path.write_text(''.join(json.dumps(r)+'\n' for r in records))
        self.store = messaging.MessagingStore(str(self.base/'state'), str(self.sessions))
        self.store.available = True
        self.previous = server.MESSAGING, server.KEY, server.PORT, dict(server.AGENTS), list(server.OFFICER_ASKS)
        self.officer_key = server.OFFICER_KEY
        server.OFFICER_KEY = 'fixture-enabled'
        server.MESSAGING, server.KEY = self.store, 'fixture-owner-key'
        server.AGENTS.clear(); server.AGENTS.update({self.aid:{'runtime':'Codex','name':'Atlas'}, self.cid:{'runtime':'Claude','name':'Mira'}})
        server.OFFICER_ASKS.clear()
        self.httpd = server.ThreadingHTTPServer(('127.0.0.1',0),server.H)
        server.PORT = self.httpd.server_port
        self.thread = threading.Thread(target=self.httpd.serve_forever,daemon=True); self.thread.start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join()
        server.OFFICER_KEY = self.officer_key
        server.MESSAGING, server.KEY, server.PORT = self.previous[:3]
        server.AGENTS.clear(); server.AGENTS.update(self.previous[3]); server.OFFICER_ASKS[:] = self.previous[4]
        self.store.close(); self.tmp.cleanup()

    def request(self, method, path, body=None, **headers):
        h = {'X-Live-Room-Key':server.KEY}; h.update(headers)
        c = http.client.HTTPConnection('127.0.0.1',server.PORT,timeout=3)
        c.request(method,path,body=json.dumps(body) if body is not None else None,headers=h)
        response = c.getresponse(); raw = response.read(); status = response.status; cache = response.getheader('Cache-Control'); c.close()
        try: data = json.loads(raw)
        except ValueError: data = raw.decode()
        return status,data,cache

    def payload(self, agent=None):
        return dict(agent=agent or self.aid,question='Report progress',request_id=str(uuid.uuid4()))

    def freeze(self, decision):
        rid = str(uuid.uuid4()); text = 'Explicit owner fixture request'
        self.store.claim_ask(rid,text); self.store.decide_ask(rid,decision)
        return {'turns':[{'role':'user','content':text}], 'request_id':rid}

    def test_capability_auth_bounds_schema_and_no_native_reads(self):
        path = '/agent-capabilities?agent='+self.aid
        for headers in ({'X-Live-Room-Key':'wrong'},{'Origin':'https://evil.invalid'},{'Host':'evil.invalid'}):
            self.assertEqual(self.request('GET',path,**headers)[0],403)
        for query in ('','agent=bad','agent='+self.aid+'&agent='+self.aid,'agent='+self.aid+'&available=true','agent=%zz','agent='+'x'*300):
            self.assertEqual(self.request('GET','/agent-capabilities?'+query)[0],400)
        with patch('codex_desktop_control.execute',side_effect=AssertionError('native control')), patch.object(messaging.claude_channel,'discover',side_effect=AssertionError('discovery')):
            status,out,cache = self.request('GET',path)
            self.assertEqual((status,cache),(200,'no-store'))
            self.assertEqual(set(out),{'agent','actions'})
            self.assertTrue(out['actions']['queue']['available'])
            self.assertEqual([out['actions'][a]['mode'] for a in ('queue','steer','interrupt','promote_queued')],['queue','new_instruction','interrupt','queued_promotion'])
            self.assertFalse(out['actions']['promote_queued']['available'])
            self.assertFalse(self.request('GET','/agent-capabilities?agent='+self.cid)[1]['actions']['queue']['available'])
        self.assertEqual(self.store._rows(),[])

    def test_direct_auth_forged_capabilities_and_wrong_target(self):
        p = self.payload()
        for headers in ({'X-Live-Room-Key':'wrong'},{'Origin':'https://evil.invalid'},{'Host':'evil.invalid'}):
            self.assertEqual(self.request('POST','/agent-message',p,**headers)[0],403)
        self.assertEqual(self.request('POST','/agent-message',dict(p,capabilities={'queue':True}))[0],400)
        self.assertEqual(self.request('POST','/agent-message',dict(p,agent='x:'+str(uuid.uuid4())))[0],404)
        self.assertEqual(self.store._rows(),[])

    def test_direct_and_officer_claude_fail_before_discover_or_request_write(self):
        body = self.freeze({'kind':'relay','agent':self.cid,'question':'Report progress'})
        before = self.store.db.total_changes
        with patch.object(messaging.claude_channel,'discover',side_effect=AssertionError('discovery')), patch.object(self.store,'_save',side_effect=AssertionError('request write')):
            status,out,_ = self.request('POST','/agent-message',self.payload(self.cid))
            self.assertEqual(status,503); self.assertIn('Set up Claude once in WATCH',out['error'])
            status,out,_ = self.request('POST','/ask',body)
            self.assertEqual(status,200); self.assertIn('Set up Claude once in WATCH',out['error'])
        self.assertEqual(self.store.db.total_changes,before)
        self.assertEqual(self.store._rows(),[])

    def test_actual_interpreter_claude_gate_and_no_control_fallback(self):
        def model(*args,**kwargs):
            return {'content':[{'type':'tool_use','name':'relay_question','input':{'agent':'Mira','question':'Report progress'}}]},''
        old = server.officer_ask.__defaults__
        server.officer_ask.__defaults__ = (model,)+old[1:]
        try:
            out = self.request('POST','/ask',{'turns':[{'role':'user','content':'Ask Mira for progress'}],'request_id':str(uuid.uuid4())})[1]
            self.assertIn('Set up Claude once in WATCH',out['error'])
        finally: server.officer_ask.__defaults__ = old
        self.assertEqual(self.store._rows(),[])

    def test_all_three_owner_paths_reach_gateway_with_real_ledgers(self):
        with patch.object(self.store,'owner_action',wraps=self.store.owner_action) as gateway, patch('codex_desktop_control.execute',return_value={'state':'acknowledged'}) as bridge:
            self.assertEqual(self.request('POST','/agent-message',self.payload())[0],200)
            body = self.freeze({'kind':'relay','agent':self.aid,'question':'Another explicit request'})
            self.assertIn('messages',self.request('POST','/ask',body)[1])
            body = self.freeze({'kind':'control','action':'steer','agent':self.aid,'instruction':'Focus on tests'})
            out = self.request('POST','/ask',body)[1]
            self.assertEqual(out['controls'][0]['state'],'acknowledged')
            self.assertEqual([c.args[0] for c in gateway.call_args_list],['queue','queue','steer'])
            self.assertEqual(bridge.call_count,1)
        self.assertEqual(len(self.store._rows()),2)

    def test_duplicate_before_target_transport_gate_and_conflict(self):
        p = self.payload(); first = self.request('POST','/agent-message',p)
        server.AGENTS.clear(); self.store.available=False
        self.assertEqual(self.request('POST','/agent-message',p),first)
        self.assertEqual(self.request('POST','/agent-message',dict(p,question='Changed'))[0],409)
        self.assertEqual(self.request('POST','/agent-message',dict(p,agent=self.cid))[0],409)
        self.assertEqual(len(self.store._rows()),1)

    def test_legacy_claude_duplicate_and_origin_conflict(self):
        p = self.payload(self.cid)
        row = dict(p,name='Mira',state='unknown',reply='',error='',created=1,launched=True,runtime='claude')
        self.store._save(row); server.AGENTS.clear()
        with patch.object(messaging.claude_channel,'discover',side_effect=AssertionError('discovery')), patch.object(self.store,'owner_capabilities',side_effect=AssertionError('preview on legacy replay')), patch.object(self.store,'_metadata',side_effect=AssertionError('metadata on legacy replay')):
            self.assertEqual(self.request('POST','/agent-message',p)[1]['state'],'unknown')
            row['origin']='peer'; self.store._save(row)
            self.assertEqual(self.request('POST','/agent-message',p)[0],409)

    def test_controls_unknown_no_fallback_and_replay_after_target_leaves(self):
        body = self.freeze({'kind':'control','action':'interrupt','agent':self.aid,'instruction':''})
        with patch('codex_desktop_control.execute',return_value={'state':'unknown'}) as bridge:
            first = self.request('POST','/ask',body)
            self.assertEqual(first[1]['controls'][0]['state'],'unknown')
            server.AGENTS.clear()
            self.assertEqual(self.request('POST','/ask',body),first)
            self.assertEqual(bridge.call_count,1)
        self.assertEqual(self.store._rows(),[])

    def test_preview_does_not_authorize_changed_dispatch(self):
        self.assertTrue(self.request('GET','/agent-capabilities?agent='+self.aid)[1]['actions']['queue']['available'])
        self.store.available=False
        self.assertEqual(self.request('POST','/agent-message',self.payload())[0],503)
        self.assertEqual(self.store._rows(),[])

    def test_concurrent_duplicate_http_creates_one_record(self):
        payload = self.payload()
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.request('POST','/agent-message',payload),range(12)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(results[0][0],200)
        self.assertEqual(len(self.store._rows()),1)

    def test_missing_observed_and_child_capabilities_are_not_authority(self):
        missing = 'x:' + str(uuid.uuid4())
        self.assertFalse(self.request('GET','/agent-capabilities?agent='+missing)[1]['actions']['queue']['available'])
        server.AGENTS[self.aid]['observed'] = True
        actions = self.request('GET','/agent-capabilities?agent='+self.aid)[1]['actions']
        self.assertFalse(actions['queue']['available']); self.assertFalse(actions['steer']['supported'])
        server.AGENTS[self.aid].pop('observed'); server.AGENTS[self.aid]['sub'] = True
        self.assertFalse(self.request('GET','/agent-capabilities?agent='+self.aid)[1]['actions']['interrupt']['supported'])

    def test_transcript_eligibility_missing_ambiguous_and_metadata(self):
        path = next(self.sessions.iterdir()); original = path.read_bytes()
        def unavailable():
            status, out, _ = self.request('GET','/agent-capabilities?agent='+self.aid)
            self.assertEqual(status,200)
            self.assertFalse(out['actions']['queue']['available'])
            self.assertTrue(out['actions']['queue']['reason'])
            self.assertNotIn(self.tmp.name,json.dumps(out))
        path.unlink(); unavailable(); path.write_bytes(original)
        second = self.sessions / ('duplicate-'+self.aid[2:]+'.jsonl')
        second.write_bytes(original); unavailable(); second.unlink()
        for extra in ({'id':str(uuid.uuid4())},{'source':'exec'},{'originator':'codex_exec'},{'thread_source':'guardian_review'}):
            meta = {'id':self.aid[2:],**extra}
            path.write_text(json.dumps({'type':'session_meta','payload':meta})+'\n')
            unavailable()
        path.write_bytes(original)
        self.assertTrue(self.request('GET','/agent-capabilities?agent='+self.aid)[1]['actions']['queue']['available'])
        path.write_text(json.dumps({'type':'session_meta','payload':{'id':str(uuid.uuid4())}})+'\n')
        self.assertNotEqual(self.request('POST','/agent-message',self.payload())[0],200)
        self.assertEqual(self.store._rows(),[])

    def test_symlink_and_fifo_capabilities_fail_without_hanging(self):
        path = next(self.sessions.iterdir()); original = path.read_bytes()
        target = self.base / 'ordinary.jsonl'; target.write_bytes(original)
        path.unlink(); path.symlink_to(target)
        self.assertFalse(self.request('GET','/agent-capabilities?agent='+self.aid)[1]['actions']['queue']['available'])
        path.unlink(); os.mkfifo(path)
        # Separate process deadline makes a blocking open a deterministic regression,
        # without leaving the test server/thread or suite hung on the FIFO.
        code = """import sys
from officer_messaging import MessagingStore
store = MessagingStore(sys.argv[1], sys.argv[2])
store.available = True
try:
    result = store.owner_capabilities(sys.argv[3], {sys.argv[3]: {'runtime':'Codex','name':'Fixture'}})
    assert result['actions']['queue']['available'] is False
finally:
    store.close()
"""
        env = dict(os.environ, PYTHONPATH=str(Path(server.__file__).parent))
        result = subprocess.run([sys.executable,'-c',code,str(self.base/'fifo-state'),str(self.sessions),self.aid],env=env,capture_output=True,text=True,timeout=3)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_duplicates_do_not_reinspect_transcripts_or_capabilities(self):
        payload = self.payload(); expected = self.request('POST','/agent-message',payload)
        with patch.object(self.store,'owner_capabilities',side_effect=AssertionError('preview on replay')), patch.object(self.store,'_metadata',side_effect=AssertionError('metadata on replay')):
            self.assertEqual(self.request('POST','/agent-message',payload),expected)

    def test_promotion_is_rejected_without_write(self):
        with self.assertRaises(messaging.MessagingError):
            self.store.owner_action('promote_queued',self.aid,'',str(uuid.uuid4()),server.AGENTS)
        self.assertEqual(self.store._rows(),[])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM controls').fetchone()[0],0)


if __name__ == '__main__': unittest.main()
