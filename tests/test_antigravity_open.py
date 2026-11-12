#!/usr/bin/env python3
"""Owner HTTP and navigation transport fixtures; never an installed app."""
import copy
import http.client
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import antigravity_open as gateway
import antigravity_navigation as navigation
import server


class FixtureBroker(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.respond('GET')

    def do_POST(self):
        self.respond('POST')

    def respond(self, method):
        fixture = self.server.fixture
        fixture.calls.append((method, self.path, dict(self.headers)))
        if self.headers.get('Authorization') != 'Bearer ' + fixture.connection['owner_token']:
            self.send_response(403); self.end_headers(); return
        if method == 'POST':
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            fixture.posts.append(body)
            fixture.row = dict(protocol=1, generation=fixture.connection['generation'],
                               request_id=body['request_id'], conversation_id=body['conversation_id'],
                               state=fixture.initial, confirmed=False)
            out = dict(fixture.row)
        elif '?' in self.path:
            out = dict(fixture.row, state=fixture.final)
        else:
            out = dict(protocol=1, generation=fixture.connection['generation'], connected=fixture.connected, pending=False)
        if fixture.callback:
            fixture.callback(method, self.path, out)
        raw = json.dumps(out).encode()
        if getattr(fixture, 'slow_headers', False):
            try:
                for byte in b'HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n':
                    self.wfile.write(bytes([byte])); self.wfile.flush(); time.sleep(.1)
            except OSError:
                pass
            return
        self.send_response(fixture.http_status)
        self.send_header('Content-Type', fixture.content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(os.path.realpath(self.temp.name))
        self.cid = str(uuid.uuid4()); self.aid = 'a:' + self.cid
        self.agent = {'runtime': 'Antigravity', 'name': 'Fixture'}
        self.agents = {self.aid: self.agent}
        self.posts, self.calls = [], []
        self.initial, self.final, self.connected = 'queued', 'called', True
        self.callback, self.http_status, self.content_type = None, 200, 'application/json'
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), FixtureBroker)
        self.httpd.fixture = self
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True); self.thread.start()
        self.connection = dict(protocol=1, generation='a'*32, url='http://127.0.0.1:%d' % self.httpd.server_port,
                               owner_token='b'*64, broker_pid=123, broker_start='fixture', runtime={'fixture': True})
        self.local_status = dict(supported=True, installed=True, authorized=True, state='waiting_panel', message='Fixture')
        self.patches = [patch.object(navigation, 'read_connection', side_effect=lambda home: copy.deepcopy(self.connection)),
                        patch.object(navigation, 'validate_connection', return_value=True),
                        patch.object(navigation, 'local_state', side_effect=lambda home: dict(self.local_status)),
                        patch.object(navigation, 'existing_app_chat', return_value=True),
                        patch.object(navigation, 'foreground', return_value=True)]
        self.mocks = [p.start() for p in self.patches]
        self.adapter = gateway.Adapter(str(self.home))
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join()
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()


class GatewayTests(FixtureCase):
    def test_status_is_sanitized_and_eligibility_is_fresh_even_when_cached(self):
        status = self.adapter.status(self.agents)
        self.assertEqual(set(status), {'supported','installed','authorized','available','state','message','generation','eligible_agents'})
        self.assertEqual((status['state'], status['eligible_agents']), ('ready', [self.aid]))
        self.assertNotIn(self.connection['owner_token'], json.dumps(status))
        self.assertNotIn(self.connection['url'], json.dumps(status))
        self.mocks[3].return_value = False
        self.assertEqual(self.adapter.status(self.agents)['eligible_agents'], [])
        self.assertEqual(len(self.calls), 1, 'only readiness is cached')

    def test_live_lease_lost_and_forced_status_cache_is_bounded(self):
        self.assertTrue(self.adapter.status(self.agents)['available'])
        self.connected = False
        self.assertFalse(self.adapter.status(self.agents, force=True)['available'])
        self.assertEqual(self.adapter.status(self.agents)['state'], 'waiting_panel')
        self.assertEqual(self.adapter.status(self.agents)['eligible_agents'], [])

    def test_readiness_cache_expires_within_two_seconds_without_force(self):
        clock=[100.0]; self.adapter.clock=lambda:clock[0]
        self.assertTrue(self.adapter.status(self.agents)['available'])
        self.connected=False; clock[0]+=2.01
        self.assertFalse(self.adapter.status(self.agents)['available'])
        self.assertEqual(len(self.calls),2)

    def test_absolute_http_deadline_rejects_trickled_headers(self):
        self.slow_headers=True
        started=time.monotonic()
        self.assertFalse(self.adapter.status(self.agents,force=True)['available'])
        self.assertLess(time.monotonic()-started,2.5)
        self.assertEqual(len(self.calls),1)

    def test_open_sends_once_correlates_then_activates_without_confirmation_claim(self):
        result = self.adapter.open(self.aid, self.agent)
        self.assertEqual(result, dict(status='dispatched', confirmed=False, foreground_requested=True, message=gateway.DISPATCHED))
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(set(self.posts[0]), {'request_id','conversation_id','expires_at_ms'})
        self.assertEqual(self.posts[0]['conversation_id'], self.cid)
        self.assertGreater(self.posts[0]['expires_at_ms'], time.time()*1000)
        self.mocks[4].assert_called_once_with(self.connection['runtime'], str(self.home))
        self.assertTrue(all(c[2]['Authorization'] == 'Bearer '+self.connection['owner_token'] for c in self.calls))

    def test_runtime_must_be_verified_before_any_capability_is_sent(self):
        self.mocks[1].side_effect = navigation.NavigationError('unavailable', 'fixed')
        with self.assertRaises(gateway.OpenError): self.adapter.open(self.aid, self.agent)
        self.assertEqual(self.calls, []); self.mocks[4].assert_not_called()

    def test_changed_generation_after_post_is_uncertain_never_retried_or_foregrounded(self):
        def change(method, path, out):
            if method == 'POST': self.connection['generation'] = 'c'*32
        self.callback = change
        self.assertEqual(self.adapter.open(self.aid, self.agent)['status'], 'uncertain')
        self.assertEqual(len(self.posts), 1); self.mocks[4].assert_not_called()

    def test_mismatched_reply_does_not_activate(self):
        def change(method, path, out):
            if method == 'POST': out['conversation_id'] = str(uuid.uuid4())
        self.callback = change
        self.assertEqual(self.adapter.open(self.aid, self.agent)['status'], 'uncertain')
        self.assertEqual(len(self.posts), 1); self.mocks[4].assert_not_called()

    def test_expiry_and_uncertain_states_never_auto_resend(self):
        for state in ('expired', 'unknown', 'failed'):
            self.initial = state
            self.assertEqual(self.adapter.open(self.aid, self.agent)['status'], 'uncertain')
        self.assertEqual(len(self.posts), 3); self.mocks[4].assert_not_called()

    def test_definitive_target_rejection_is_safe_failure_not_uncertain(self):
        def reject(method,path,out):
            if method=='POST': self.http_status=404
        self.callback=reject
        with self.assertRaises(gateway.OpenError) as error:
            self.adapter.open(self.aid,self.agent)
        self.assertEqual(error.exception.status,404)
        self.assertEqual(len(self.posts),1)
        self.mocks[4].assert_not_called()

    def test_post_transport_failure_is_uncertain_without_retry(self):
        self.initial = 'called'
        self.callback = lambda method, path, out: out.update(protocol=99) if method == 'POST' else None
        self.assertEqual(self.adapter.open(self.aid, self.agent)['status'], 'uncertain')
        self.assertEqual(len(self.posts), 1); self.mocks[4].assert_not_called()

    def test_disconnect_during_action_blocks_foreground(self):
        def change(method, path, out):
            if method == 'POST': self.adapter.invalidate()
        self.callback = change
        self.assertEqual(self.adapter.open(self.aid, self.agent)['status'], 'uncertain')
        self.mocks[4].assert_not_called()

    def test_source_eligibility_not_provider_label_or_other_provider(self):
        for aid, row in [('x:'+self.cid, self.agent), (self.aid, {'runtime':'Codex'}),
                         (self.aid, dict(self.agent, observed=True)), ('a:bad', self.agent)]:
            with self.assertRaises(gateway.OpenError): self.adapter.open(aid, row)
        self.mocks[3].return_value = False
        with self.assertRaises(gateway.OpenError): self.adapter.open(self.aid, self.agent)
        self.assertEqual(self.calls, [])

    def test_setup_and_disconnect_use_fixed_local_inputs_and_public_state(self):
        with patch.object(navigation, 'install') as install, patch.object(navigation, 'disconnect') as disconnect:
            self.assertEqual(self.adapter.setup('/fictional/repository', self.agents)['state'], 'ready')
            install.assert_called_once_with(str(self.home), '/fictional/repository')
            self.local_status['authorized'] = False; self.local_status['state'] = 'disconnected'
            result = self.adapter.disconnect(self.agents)
            disconnect.assert_called_once_with(str(self.home))
            self.assertEqual((result['state'], result['available']), ('disconnected', False))

    def test_disconnect_after_setup_in_progress_finishes_revoked(self):
        entered=threading.Event(); release=threading.Event(); revoked=threading.Event(); failures=[]
        def install(home,repo):
            entered.set(); self.assertTrue(release.wait(2))
            self.local_status.update(authorized=True,state='waiting_panel')
        def disconnect(home):
            self.local_status.update(authorized=False,state='disconnected'); revoked.set()
        def action(call):
            try: call()
            except BaseException as error: failures.append(error)
        with patch.object(navigation,'install',side_effect=install), patch.object(navigation,'disconnect',side_effect=disconnect):
            setup=threading.Thread(target=lambda:action(lambda:self.adapter.setup('/fictional/repository',self.agents)))
            setup.start(); self.assertTrue(entered.wait(2))
            stop=threading.Thread(target=lambda:action(lambda:self.adapter.disconnect(self.agents)))
            stop.start()
            try:
                self.assertFalse(revoked.wait(.05),'revoke must finish after an in-flight installation')
            finally: release.set()
            setup.join(3); stop.join(3)
            self.assertFalse(setup.is_alive()); self.assertFalse(stop.is_alive()); self.assertEqual(failures,[])
            self.assertFalse(self.adapter.status(self.agents,force=True)['authorized'])
            self.assertTrue(revoked.is_set())

    def test_single_flight_rejects_second_explicit_action(self):
        self.adapter._action.acquire()
        try:
            with self.assertRaises(gateway.OpenError) as err: self.adapter.open(self.aid, self.agent)
            self.assertEqual(err.exception.status, 409)
            self.assertEqual(self.posts, [])
        finally: self.adapter._action.release()

    def test_expired_local_deadline_never_foregrounds(self):
        wall = [1000]
        self.adapter.wall = lambda: wall[0]
        def advance(method, path, out):
            if method == 'POST': wall[0] += 6
        self.callback = advance
        self.initial = 'called'
        self.assertEqual(self.adapter.open(self.aid, self.agent)['status'], 'uncertain')
        self.mocks[4].assert_not_called()

    def test_redirect_and_nonjson_response_never_followed(self):
        for status, content_type in [(302,'application/json'), (200,'text/html')]:
            self.http_status, self.content_type = status, content_type
            self.assertFalse(self.adapter.status(self.agents, force=True)['available'])
        self.assertEqual(len(self.calls), 2)


class OwnerHTTPTests(FixtureCase):
    # Inherits the transport fixtures; only this class starts the WATCH HTTP side.
    def setUp(self):
        super().setUp()
        self.previous = server.KEY, server.PORT, server.ANTIGRAVITY_OPEN, dict(server.AGENTS)
        server.KEY, server.ANTIGRAVITY_OPEN = 'fixture-owner-key', self.adapter
        server.AGENTS.clear(); server.AGENTS.update(self.agents)
        self.watch = ThreadingHTTPServer(('127.0.0.1',0), server.H)
        server.PORT = self.watch.server_port
        self.watchthread = threading.Thread(target=self.watch.serve_forever, daemon=True); self.watchthread.start()
        self.addCleanup(self.owner_cleanup)

    def owner_cleanup(self):
        self.watch.shutdown(); self.watch.server_close(); self.watchthread.join()
        server.KEY, server.PORT, server.ANTIGRAVITY_OPEN = self.previous[:3]
        server.AGENTS.clear(); server.AGENTS.update(self.previous[3])

    def owner(self, method, path, body=None, **headers):
        h = {'X-Live-Room-Key':server.KEY, 'Content-Type':'application/json'}; h.update(headers)
        c = http.client.HTTPConnection('127.0.0.1',server.PORT,timeout=3)
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse(); raw = r.read(); status = r.status; c.close()
        try: out = json.loads(raw)
        except ValueError: out = raw.decode()
        return status, out

    def test_owner_http_auth_roles_host_origin_and_exact_schema(self):
        for path, method, body in [('/antigravity-open/status','GET',None), ('/antigravity-open/setup','POST',{}),
                                    ('/antigravity-open/disconnect','POST',{}), ('/open','POST',{'agent':self.aid})]:
            for headers in [{'X-Live-Room-Key':'wrong'}, {'X-Live-Room-Key':self.connection['owner_token']},
                            {'X-Live-Room-Key':'', 'X-Sidecar-Token':'fictional-ui-token'},
                            {'Origin':'https://foreign.invalid'}, {'Host':'foreign.invalid'}]:
                self.assertEqual(self.owner(method,path,body,**headers)[0],403)
        with patch.object(navigation,'install') as install, patch.object(navigation,'disconnect') as disconnect:
            for path in ('/antigravity-open/setup','/antigravity-open/disconnect'):
                self.assertEqual(self.owner('POST',path,{'url':'http://foreign.invalid'})[0],400)
                self.assertEqual(self.owner('POST',path+'?x=1',{})[0],400)
            install.assert_not_called(); disconnect.assert_not_called()
        self.assertEqual(self.owner('GET','/antigravity-open/status?agent='+self.aid)[0],400)
        self.assertEqual(self.owner('POST','/open',{'agent':self.aid,'prompt':'No new work'})[0],400)
        self.assertEqual(self.owner('POST','/open?retry=true',{'agent':self.aid})[0],400)
        self.assertEqual(self.calls, [])

    def test_real_owner_open_returns_truthful_202_without_leaking_receipt(self):
        status, out = self.owner('POST','/open',{'agent':self.aid})
        self.assertEqual(status,202); self.assertEqual(out['status'],'dispatched')
        self.assertIs(out['confirmed'],False)
        self.assertNotIn('Opened',out['message'])
        self.assertNotIn(self.connection['owner_token'],json.dumps(out))
        self.assertEqual(len(self.posts),1)

    def test_status_install_disconnect_are_owner_only_and_messaging_capability_unchanged(self):
        status,out = self.owner('GET','/antigravity-open/status')
        self.assertEqual((status,out['eligible_agents']),(200,[self.aid]))
        self.assertEqual(self.owner('GET','/agent-capabilities?agent='+self.aid)[0],400)
        with patch.object(navigation,'install') as install, patch.object(navigation,'disconnect') as disconnect:
            self.assertEqual(self.owner('POST','/antigravity-open/setup',{})[0],200)
            self.assertEqual(self.owner('POST','/antigravity-open/disconnect',{})[0],200)
            self.assertEqual(install.call_args.args,(str(self.home),str(Path(server.HERE).parent)))
            disconnect.assert_called_once_with(str(self.home))

    @unittest.skipUnless(os.name == 'posix', 'Native private-file navigation requires POSIX; owner gateway is tested everywhere.')
    def test_real_native_broker_round_trip_with_actual_private_authorization_and_source(self):
        # Real broker, WATCH HTTP, protected receipt and SQLite identity. Only
        # process/listener verification and OS foregrounding are fixture seams.
        path = Path(__file__).resolve().parents[1] / 'integrations' / 'antigravity-open' / 'broker.py'
        spec = importlib.util.spec_from_file_location('fixture_navigation_broker', path)
        native = importlib.util.module_from_spec(spec); spec.loader.exec_module(native)
        for index in (0, 1, 3): self.patches[index].stop()
        state = navigation.private_dir(str(self.home)); state.mkdir(parents=True, mode=0o700)
        state.parent.chmod(0o700)
        grant = state / 'authorization.json'
        grant.write_text(json.dumps({'protocol':1,'enabled':True})); grant.chmod(0o600)
        transcript = self.home / '.gemini' / 'antigravity' / 'brain' / self.cid / '.system_generated' / 'logs' / 'transcript.jsonl'
        transcript.parent.mkdir(parents=True); transcript.write_text('Private fictional transcript is never read.\n')
        database = transcript.parents[4] / 'conversation_summaries.db'
        with sqlite3.connect(str(database)) as db:
            db.execute('CREATE TABLE conversation_summaries(conversation_id TEXT PRIMARY KEY)')
            db.execute('INSERT INTO conversation_summaries VALUES (?)',(self.cid,))
        runtime = dict(backend_pid=101,backend_start='backend-fixture',app_pid=102,app_start='app-fixture',
                       bundle='/fictional/Antigravity.app',version='2.31.2',origin='https://127.0.0.1:12345')
        broker = native.Broker(str(self.home),'b'*64,'u'*64,runtime,generation='a'*32)
        httpd = native.make_server(broker,port=0)
        worker = threading.Thread(target=httpd.serve_forever,daemon=True); worker.start()
        self.connection.update(url='http://127.0.0.1:%d'%httpd.server_port,runtime=runtime)
        receipt = state / 'connection.json'; receipt.write_text(json.dumps(self.connection)); receipt.chmod(0o600)
        def ui(route,body):
            client = http.client.HTTPConnection('127.0.0.1',httpd.server_port,timeout=2)
            client.request('POST',route,json.dumps(body),{'Content-Type':'application/json','X-Sidecar-Token':'u'*64})
            response=client.getresponse(); raw=response.read(); status=response.status; client.close()
            self.assertEqual(status,200,raw)
            return json.loads(raw)
        try:
            with patch.object(navigation,'verify_runtime',return_value=True), \
                 patch.object(navigation,'process',return_value={'start':'fixture','ppid':101}), \
                 patch.object(navigation,'listener',return_value=True):
                page_id=str(uuid.uuid4())
                hello=ui('/ui/hello',dict(page_id=page_id,parent_origin=runtime['origin']))
                page=dict(page_id=page_id,lease=hello['lease'])
                seen=[]; failures=[]
                def panel():
                    try:
                        for _ in range(100):
                            command=ui('/ui/poll',page)['command']
                            if command:
                                seen.append(command)
                                ui('/ui/receipt',dict(page,request_id=command['request_id'],conversation_id=command['conversation_id'],state='called'))
                                return
                            time.sleep(.02)
                        raise AssertionError('No fixture command arrived')
                    except BaseException as error: failures.append(error)
                panelthread=threading.Thread(target=panel); panelthread.start()
                status,result=self.owner('POST','/open',{'agent':self.aid})
                panelthread.join(3)
                self.assertFalse(panelthread.is_alive()); self.assertEqual(failures,[])
                self.assertEqual((status,result['status'],result['confirmed']),(202,'dispatched',False))
                self.assertEqual([c['conversation_id'] for c in seen],[self.cid])
                self.assertIsNone(ui('/ui/poll',page)['command'],'a consumed command is never replayed')
                self.mocks[4].assert_called_once_with(runtime,str(self.home))
                # A same-UUID CLI source makes the target ambiguous immediately.
                duplicate=self.home / '.gemini' / 'antigravity-cli' / 'brain' / self.cid / '.system_generated' / 'logs' / 'transcript.jsonl'
                duplicate.parent.mkdir(parents=True); duplicate.write_text('')
                self.assertEqual(self.adapter.status(self.agents)['eligible_agents'],[])
                ui('/ui/bye',page)
                self.assertEqual(self.adapter.status(self.agents,force=True)['state'],'waiting_panel')
        finally:
            httpd.shutdown(); httpd.server_close(); worker.join()

    def test_antigravity_open_targets_exact_chat_not_recorded_parent(self):
        parent = 'a:'+str(uuid.uuid4())
        server.AGENTS[parent] = {'runtime':'Antigravity'}
        server.AGENTS[self.aid].update(sub=True,parent=parent)
        self.assertEqual(self.owner('POST','/open',{'agent':self.aid})[0],202)
        self.assertEqual(self.posts[0]['conversation_id'],self.cid)

    def test_officer_open_link_only_reads_public_availability(self):
        actions=[]
        result=server.officer_tool('open_session_link',{'agent':self.aid},actions)
        self.assertTrue(result['can_open'])
        self.assertEqual(actions,[])
        self.assertEqual(self.posts,[])
        self.assertNotIn(self.connection['owner_token'],json.dumps(result))
        self.mocks[4].assert_not_called()

    def test_other_provider_open_still_uses_existing_native_link(self):
        other = 'x:'+str(uuid.uuid4()); server.AGENTS[other]={'runtime':'Codex'}
        with patch.object(server,'launch') as launch:
            self.assertEqual(self.owner('POST','/open',{'agent':other})[0],200)
            launch.assert_called_once_with('codex://threads/'+other[2:])
        self.assertEqual(self.calls,[])


if __name__ == '__main__':
    unittest.main()
