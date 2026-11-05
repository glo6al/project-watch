"""Fictional silent hook receipts; no native apps or provider calls."""
import copy
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import watch_reply_hook as hook


def fixture(provider='codex',explicit=False):
    rid=str(uuid.uuid4());target='c:'+str(uuid.uuid4())
    args=dict(agent_id=target,identity_revision='b'*64,text='FICTIONAL_QUESTION_PRIVATE',declared_sender='FICTIONAL_LABEL')
    if explicit:args['request_id']=rid
    result=dict(origin='peer',request_id=rid,agent_id=target,state='sending',created_at=1800000000,duplicate=False,text_preview='FICTIONAL_PREVIEW',reply='',error='',
                target=dict(agent_id=target,name='FICTIONAL_NAME',provider='claude',project_id='a'*64,project_label='FICTIONAL_PROJECT',identity_revision='b'*64))
    event=dict(hook_event_name='PostToolUse',session_id=str(uuid.uuid4()),tool_use_id='call_fixture',turn_id='turn_fixture',
               cwd='/fictional/working',transcript_path=None,model='fixture-model',permission_mode='ask',
               tool_name=hook.TOOLS[provider],tool_input=args,tool_response=dict(content=[dict(type='text',text=json.dumps(dict(origin='peer',untrusted_peer_data=result)))]))
    return event,result


def set_result(event,result):
    response=event['tool_response'];blocks=response if type(response) is list else response['content']
    blocks[0]['text']=json.dumps(dict(origin='peer',untrusted_peer_data=result))


def claude_desktop_fixture():
    event,result=fixture('claude')
    event.pop('turn_id');event.pop('model');event['tool_input'].pop('declared_sender')
    target='x:'+str(uuid.uuid4());event['tool_input']['agent_id']=target
    result['agent_id']=target;result['target'].update(agent_id=target,provider='codex')
    event.update(transcript_path='/fictional/transcript',prompt_id=str(uuid.uuid4()),scratchpad_dir='/fictional/scratchpad',
                 effort={'level':'medium'},mcp_server={'name':'FICTIONAL_SERVER','source':'user'},duration_ms=12)
    set_result(event,result);event['tool_response']=event['tool_response']['content']
    return event,result


class ParserTests(unittest.TestCase):
    def test_metadata_only_both_providers_explicit_and_minted_ids(self):
        for provider in ('codex','claude'):
            for explicit in (True,False):
                event,result=fixture(provider,explicit);original=copy.deepcopy(event)
                value=hook.receipt(event,provider)
                self.assertEqual(event,original)
                self.assertEqual(value['request_id'],result['request_id'])
                self.assertEqual(value['native_id'],event['session_id'])
                self.assertEqual(value['source_scope'],'native_parent')
                self.assertEqual(value['turn_id'],'turn_fixture')
                args={k:v for k,v in event['tool_input'].items() if k!='request_id'}
                self.assertEqual(value['args_sha256'],hashlib.sha256(json.dumps(args,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest())
                serialized=json.dumps(value)
                for private in ['FICTIONAL_','working','transcript','permission_mode','model']:self.assertNotIn(private,serialized)
                self.assertNotIn('caller_classification',value)
                event['agent_id']='known-worker';self.assertEqual(hook.receipt(event,provider)['native_id'],event['session_id'])
    def test_errors_duplicates_wrong_tool_target_and_explicit_conflict(self):
        for mutate in [lambda e,r:r.update(duplicate=True),lambda e,r:r.update(duplicate=0),lambda e,r:r['target'].update(identity_revision='c'*64),
                       lambda e,r:r.update(agent_id='x:'+str(uuid.uuid4())),lambda e,r:e.update(tool_name='mcp__watch_peer__get_reply'),
                       lambda e,r:e.update(turn_id='bad\nturn'),lambda e,r:e.update(model={}),lambda e,r:r.update(state='unknown')]:
            event,result=fixture();mutate(event,result);set_result(event,result)
            with self.assertRaises(ValueError):hook.receipt(event,'codex')
        event,result=fixture(explicit=True);event['tool_input']['request_id']=str(uuid.uuid4())
        with self.assertRaises(ValueError):hook.receipt(event,'codex')
        event,result=fixture();event['tool_response']={'isError':True,'content':[{'type':'text','text':'Request ID: '+result['request_id']}]}
        with self.assertRaises(ValueError):hook.receipt(event,'codex')
    def test_claude_desktop_metadata_is_bounded_discarded_and_codex_stays_strict(self):
        event,result=claude_desktop_fixture();original=copy.deepcopy(event)
        self.assertEqual(len(event),14);self.assertEqual(set(event['tool_input']),{'agent_id','identity_revision','text'})
        value=hook.receipt(event,'claude');self.assertEqual(event,original);self.assertNotIn('turn_id',value)
        old=copy.deepcopy(event)
        for key in ('prompt_id','scratchpad_dir','effort','mcp_server','duration_ms'):old.pop(key)
        old['tool_response']={'content':old['tool_response']}
        self.assertEqual(value,hook.receipt(old,'claude')|{'observed_at':value['observed_at']})
        for key in ('prompt_id','scratchpad_dir','effort','mcp_server','duration_ms','cwd','transcript_path'):
            self.assertNotIn(key,value)
        for key in ('prompt_id','scratchpad_dir','effort','mcp_server','duration_ms'):
            codex,_=fixture();codex[key]=event[key]
            with self.assertRaises(ValueError):hook.receipt(codex,'codex')
        codex,_=fixture();codex['tool_response']=codex['tool_response']['content']
        with self.assertRaises(ValueError):hook.receipt(codex,'codex')

    def test_claude_desktop_malformed_metadata_arrays_and_results_are_rejected(self):
        changes=[('prompt_id',None),('prompt_id','x'*257),('scratchpad_dir',{}),('scratchpad_dir','x'*4097),
                 ('effort',{'level':1}),('effort',{'level':'medium','extra':True}),('effort',{'level':'x'*65}),
                 ('mcp_server',{'name':'ok','source':False}),('mcp_server',{'name':'x'*257,'source':'user'}),
                 ('mcp_server',{'name':'ok','source':'bad\nsource'}),('mcp_server',{'name':'ok','source':'user','extra':0}),
                 ('duration_ms',True),('duration_ms',-1),('duration_ms',float('inf')),('duration_ms',float('nan')),
                 ('duration_ms',86400001),('duration_ms','12'),('unreviewed',1)]
        for field,value in changes:
            with self.subTest(field=field,value=value):
                event,_=claude_desktop_fixture();event[field]=value
                with self.assertRaises(ValueError):hook.receipt(event,'claude')
        for response in [[],[{'type':'text','text':'{}'}]*2,[{'type':'image','text':'{}'}],['{}'],
                         [{'type':'text','text':{}}],[{'type':'text','text':'{}','isError':True}],
                         {'content':[{'type':'text','text':'{}'}],'isError':True}]:
            event,_=claude_desktop_fixture();event['tool_response']=response
            with self.assertRaises(ValueError):hook.receipt(event,'claude')
        for change in [lambda r:r.update(duplicate=True),lambda r:r.update(error='fictional tool failure'),
                       lambda r:r.update(state='failed'),lambda r:r['target'].update(identity_revision='c'*64),
                       lambda r:r.update(request_id='invalid')]:
            event,result=claude_desktop_fixture();change(result);set_result(event,result)
            with self.assertRaises(ValueError):hook.receipt(event,'claude')

    def test_bounded_strict_json(self):
        for raw in ['{"x":1,"x":2}','{"x":NaN}','x'*131073]:
            with self.assertRaises(ValueError):hook.decode(raw)


@unittest.skipIf(os.name=='nt','Local private POSIX hook transport')
class TransportTests(unittest.TestCase):
    def test_actual_same_socket_proof_silent_subprocess_and_no_plaintext(self):
        observed=[];errors=[];config=dict(version=1,credential_id=str(uuid.uuid4()),secret='a'*64,port=0,source_scope='native_parent',consent_version=1)
        run=str(uuid.uuid4());key=bytes.fromhex(config['secret']);port=[0];bad=[False]
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*a):pass
            def do_POST(self):
                raw=self.rfile.read(int(self.headers['Content-Length']));value=json.loads(raw);observed.append((self.path,value,self.client_address))
                if self.path.endswith('challenge'):
                    self.nonce=value['nonce'];proof=hook.mac(key,'watch-peer-reply-server-v1',port[0],config['credential_id'],value['nonce'],run)
                    payload=dict(run_id=run,proof='0'*64 if bad[0] else proof);headers={}
                else:
                    stamp=self.headers['X-Watch-Time'];nonce=self.headers['X-Watch-Nonce']
                    expected=hook.mac(key,'watch-peer-reply-request-v1',port[0],'POST',self.path,stamp,nonce,run,hashlib.sha256(raw).hexdigest())
                    if self.headers['X-Watch-Signature']!=expected:raise AssertionError('signature')
                    payload=dict(accepted=True)
                    body=json.dumps(payload,separators=(',',':')).encode()
                    headers={'X-Watch-Signature':hook.mac(key,'watch-peer-reply-response-v1',port[0],run,nonce,200,hashlib.sha256(body).hexdigest())}
                body=json.dumps(payload,separators=(',',':')).encode();self.send_response(200)
                self.send_header('Content-Length',str(len(body)))
                for name,value in headers.items():self.send_header(name,value)
                self.end_headers();self.wfile.write(body)
        class Server(ThreadingHTTPServer):
            def handle_error(self,request,address):
                if sys.exc_info()[0] is not ConnectionResetError:errors.append(sys.exc_info()[0].__name__)
        server=Server(('127.0.0.1',0),Handler);port[0]=server.server_port;config['port']=port[0]
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                path=Path(temp).resolve()/'credential.json';path.write_text(json.dumps(config));path.chmod(0o600)
                for provider,make in [('codex',fixture),('claude',claude_desktop_fixture)]:
                    event,_=make();original=copy.deepcopy(event);observed.clear()
                    command=[sys.executable,str(Path(hook.__file__)),'--provider',provider,'--credential-file',str(path)]
                    result=subprocess.run(command,input=json.dumps(event).encode(),capture_output=True,timeout=10)
                    self.assertEqual((result.returncode,result.stdout,result.stderr),(0,b'',b''))
                    self.assertEqual([row[0] for row in observed],['/peer-reply/v1/challenge','/peer-reply/v1/receipt'])
                    self.assertEqual(observed[0][2],observed[1][2]);self.assertEqual(event,original)
                    wire=json.dumps(observed[1][1]);self.assertNotIn('FICTIONAL_',wire);self.assertNotIn('/fictional/',wire)
                    if provider=='claude':
                        for field in ('turn_id','prompt_id','scratchpad_dir','effort','mcp_server','duration_ms'):self.assertNotIn(field,observed[1][1])
                        for invalidate in [lambda e:e.update(duration_ms=True),lambda e:e.update(tool_response=[]),
                                           lambda e:e.update(tool_response={'content':e['tool_response'],'isError':True})]:
                            invalid=copy.deepcopy(event);invalidate(invalid);observed.clear()
                            rejected=subprocess.run(command,input=json.dumps(invalid).encode(),capture_output=True,timeout=10)
                            self.assertEqual((rejected.returncode,rejected.stdout,rejected.stderr),(0,b'',b''));self.assertEqual(observed,[])
                event,_=fixture()
                command=[sys.executable,str(Path(hook.__file__)),'--provider','codex','--credential-file',str(path)]
                bad[0]=True;observed.clear()
                result=subprocess.run(command,input=json.dumps(event).encode(),capture_output=True,timeout=10)
                self.assertEqual((result.returncode,result.stdout,result.stderr),(0,b'',b''))
                self.assertEqual(len(observed),1,'no source metadata sent before valid server proof')
                path.chmod(0o644)
                with self.assertRaises(ValueError):hook.credential(str(path))
                path.chmod(0o600);link=Path(temp).resolve()/'link';link.symlink_to(path)
                with self.assertRaises(OSError):hook.credential(str(link))
                self.assertEqual(errors,[],'owned server must authenticate the actual receipt')
        finally:server.shutdown();server.server_close();thread.join(5);self.assertFalse(thread.is_alive())


    def test_slow_drip_listener_is_bounded_by_standalone_total_deadline(self):
        finished=threading.Event();seen=[]
        class Slow(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*a):pass
            def do_POST(self):
                seen.append(self.path);self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200);self.send_header('Content-Length','1000');self.end_headers()
                try:
                    for _ in range(1000):self.wfile.write(b' ');self.wfile.flush();time.sleep(0.03)
                except OSError:pass
                finally:self.close_connection=True;finished.set()
        server=ThreadingHTTPServer(('127.0.0.1',0),Slow);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                root=Path(temp).resolve();path=root/'credential.json'
                path.write_text(json.dumps(dict(version=1,credential_id=str(uuid.uuid4()),secret='a'*64,port=server.server_port,source_scope='native_parent',consent_version=1)));path.chmod(0o600)
                source=Path(hook.__file__).read_text();self.assertIn('TOTAL_SECONDS = 8',source)
                script=root/'accelerated.py';script.write_text(source.replace('TOTAL_SECONDS = 8','TOTAL_SECONDS = 0.3'))
                event,_=fixture();start=time.monotonic()
                result=subprocess.run([sys.executable,str(script),'--provider','codex','--credential-file',str(path)],input=json.dumps(event).encode(),capture_output=True,timeout=2)
                self.assertEqual((result.returncode,result.stdout,result.stderr),(0,b'',b''))
                self.assertLess(time.monotonic()-start,1.5)
                self.assertEqual(seen,['/peer-reply/v1/challenge'])
                self.assertTrue(finished.wait(1),'owned slow handler must settle after hook closes')
        finally:server.shutdown();server.server_close();thread.join(5)


if __name__=='__main__':unittest.main()
