#!/usr/bin/env python3
"""Silent, owner-opted PostToolUse observer. It neither decides tools nor delivers messages."""
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import secrets
import signal
import select
import stat
import sys
import time
import uuid

MAX_INPUT = 131072
TOTAL_SECONDS = 8
TOOLS = {'codex':'mcp__watch_peer__send_message','claude':'mcp__watch-peer__send_message'}
HEX = re.compile(r'^[0-9a-f]{64}$')

def require(value):
    if not value:
        raise ValueError()


def text(value, limit, printable=False):
    require(type(value) is str and 0 < len(value) <= limit)
    require(not any(0xD800 <= ord(c) <= 0xDFFF for c in value))
    if printable:
        require(not any(ord(c) < 32 or ord(c) == 127 for c in value))
    return value


def ident(value, request=False):
    require(type(value) is str and str(uuid.UUID(value)) == value and uuid.UUID(value).int != 0)
    if request:
        require(uuid.UUID(value).version == 4)
    return value


def agent(value):
    require(type(value) is str and value[:2] in ('c:', 'x:'))
    ident(value[2:])
    return value


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result)
        result[key] = value
    return result


def decode(raw):
    require(len(raw) <= MAX_INPUT)
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: require(False))
    def bounded(item, depth=0):
        require(depth <= 20)
        if isinstance(item, dict):
            require(len(item) <= 24)
            for v in item.values(): bounded(v, depth + 1)
        elif isinstance(item, list):
            require(len(item) <= 8)
            for v in item: bounded(v, depth + 1)
    bounded(value)
    return value


def receipt(event, provider):
    require(provider in TOOLS and type(event) is dict)
    required = {'hook_event_name', 'session_id', 'tool_use_id', 'tool_name', 'tool_input', 'tool_response'}
    optional = {'cwd', 'transcript_path', 'permission_mode', 'turn_id', 'agent_id', 'agent_type', 'teammate_name', 'model'}
    if provider == 'claude':
        optional |= {'prompt_id', 'scratchpad_dir', 'effort', 'mcp_server', 'duration_ms'}
    require(required <= set(event) <= required | optional)
    if provider == 'claude':
        for field, maximum in [('prompt_id', 256), ('scratchpad_dir', 4096)]:
            if field in event: text(event[field], maximum, True)
        if 'effort' in event:
            value = event['effort']
            require(type(value) is dict and set(value) == {'level'})
            text(value['level'], 64, True)
        if 'mcp_server' in event:
            value = event['mcp_server']
            require(type(value) is dict and set(value) == {'name', 'source'})
            text(value['name'], 256, True); text(value['source'], 64, True)
        if 'duration_ms' in event:
            value = event['duration_ms']
            require(type(value) in (int, float) and 0 <= value <= 86400000 and math.isfinite(value))
    require(event['hook_event_name'] == 'PostToolUse' and event['tool_name'] == TOOLS[provider])
    source = ident(event['session_id'])
    call = text(event['tool_use_id'], 256, True)
    turn = text(event['turn_id'], 256, True) if 'turn_id' in event else None
    for field, maximum in [('cwd', 4096), ('transcript_path', 4096), ('permission_mode', 64)]:
        if field in event and not (field == 'transcript_path' and event[field] is None):
            text(event[field], maximum, True)
    if 'model' in event: text(event['model'], 256, True)
    # User consent explicitly addresses the native parent, including worker-origin calls.
    for field in ('agent_id', 'agent_type', 'teammate_name'):
        if event.get(field) is not None: text(event[field], 256, True)
    args = event['tool_input']
    require(type(args) is dict and {'agent_id', 'identity_revision', 'text'} <= set(args) <=
            {'agent_id', 'identity_revision', 'text', 'request_id', 'declared_sender'})
    target = agent(args['agent_id'])
    require(type(args['identity_revision']) is str and HEX.fullmatch(args['identity_revision']))
    require(text(args['text'], 2000).strip() and '\x00' not in args['text'])
    if 'declared_sender' in args: require(text(args['declared_sender'], 120, True).strip())
    response = event['tool_response']
    if provider == 'claude' and type(response) is list:
        response = {'content': response}
    require(type(response) is dict and {'content'} <= set(response) <= {'content', 'isError'})
    require('isError' not in response or type(response['isError']) is bool and response['isError'] is False)
    content = response['content']
    require(type(content) is list and len(content) == 1 and type(content[0]) is dict and
            set(content[0]) == {'type', 'text'} and content[0]['type'] == 'text')
    wrapped = decode(text(content[0]['text'], 65536))
    require(type(wrapped) is dict and set(wrapped) == {'origin', 'untrusted_peer_data'} and wrapped['origin'] == 'peer')
    result = wrapped['untrusted_peer_data']
    fields = {'origin', 'request_id', 'agent_id', 'state', 'created_at', 'target', 'duplicate', 'text_preview'}
    require(type(result) is dict and fields <= set(result) <= fields | {'reply', 'error'})
    require(result['origin'] == 'peer' and type(result['duplicate']) is bool and result['duplicate'] is False)
    request = ident(result['request_id'], True)
    if 'request_id' in args: require(ident(args['request_id'], True) == request)
    require(result['agent_id'] == target and result['state'] in ('sending', 'queued', 'awaiting_reply', 'answered'))
    require(type(result['created_at']) in (int, float) and 0 < result['created_at'] <= 253402300799)
    require(type(result['text_preview']) is str and len(result['text_preview']) <= 160)
    require(result.get('error', '') == '')
    if 'reply' in result: require(type(result['reply']) is str and len(result['reply']) <= 16000)
    selected = result['target']
    require(type(selected) is dict and set(selected) == {'agent_id', 'name', 'provider', 'project_id', 'project_label', 'identity_revision'})
    require(selected['agent_id'] == target and selected['identity_revision'] == args['identity_revision'])
    require(selected['provider'] == {'c:': 'claude', 'x:': 'codex'}[target[:2]])
    require(type(selected['project_id']) is str and HEX.fullmatch(selected['project_id']))
    for key, maximum in [('name', 240), ('project_label', 300)]:
        require(type(selected[key]) is str and len(selected[key]) <= maximum)
    original = {key:value for key,value in args.items() if key != 'request_id'}
    canonical = json.dumps(original, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
    value = dict(version=1, provider=provider, native_id=source, source_scope='native_parent',
                 tool_use_id=call, request_id=request, target_agent_id=target,
                 target_identity_revision=selected['identity_revision'], created_at=result['created_at'],
                 args_sha256=hashlib.sha256(canonical).hexdigest(), observed_at=time.time(), duplicate=False)
    if turn is not None: value['turn_id'] = turn
    return value


def credential(path):
    require(os.path.isabs(path) and os.path.abspath(path) == path)
    folder = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = path.split(os.sep)
        for part in parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
            os.close(folder); folder = child
        parent = os.fstat(folder)
        require(parent.st_uid == os.getuid() and stat.S_IMODE(parent.st_mode) == 0o700)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        try:
            meta = os.fstat(fd)
            require(stat.S_ISREG(meta.st_mode) and meta.st_uid == os.getuid() and meta.st_nlink == 1
                    and stat.S_IMODE(meta.st_mode) == 0o600 and meta.st_size <= 8192)
            value = decode(os.read(fd,8193))
        finally: os.close(fd)
    finally: os.close(folder)
    require(type(value) is dict and set(value) == {'version','credential_id','secret','port','source_scope','consent_version'})
    require(value['version'] == 1 and type(value['version']) is int and value['consent_version'] == 1
            and type(value['consent_version']) is int and value['source_scope'] == 'native_parent')
    ident(value['credential_id']);require(type(value['secret']) is str and HEX.fullmatch(value['secret']))
    require(type(value['port']) is int and 1 <= value['port'] <= 65535)
    return value


def mac(key, *parts):
    return hmac.new(key, '\n'.join(map(str,parts)).encode(), hashlib.sha256).hexdigest()


def register_receipt(config, value):
    """One bounded, mutually authenticated exchange on the same proven socket."""
    key=bytes.fromhex(config['secret']);port=config['port'];cid=config['credential_id']
    nonce=secrets.token_hex(32);conn=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
    deadline=time.monotonic()+6
    def request(path, payload, headers=None):
        remaining=deadline-time.monotonic();require(remaining>0)
        conn.timeout=min(3,remaining)
        if conn.sock: conn.sock.settimeout(conn.timeout)
        raw=json.dumps(payload,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()
        require(len(raw)<=4096)
        conn.request('POST',path,raw,{'Content-Type':'application/json','Content-Length':str(len(raw)),**(headers or {})})
        response=conn.getresponse();body=response.read(8193);require(len(body)<=8192)
        return response,body
    try:
        response,raw=request('/peer-reply/v1/challenge',dict(credential_id=cid,nonce=nonce))
        require(response.status==200 and not response.will_close and conn.sock is not None)
        proof=decode(raw);require(type(proof) is dict and set(proof)=={'run_id','proof'})
        run=ident(proof['run_id'])
        require(type(proof['proof']) is str and hmac.compare_digest(proof['proof'],mac(key,'watch-peer-reply-server-v1',port,cid,nonce,run)))
        proven=conn.sock
        body=json.dumps(value,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()
        stamp=str(int(time.time()))
        headers={'X-Watch-Reply':cid,'X-Watch-Time':stamp,'X-Watch-Nonce':nonce,'X-Watch-Run':run,
                 'X-Watch-Signature':mac(key,'watch-peer-reply-request-v1',port,'POST','/peer-reply/v1/receipt',stamp,nonce,run,hashlib.sha256(body).hexdigest())}
        require(conn.sock is proven and proven.fileno()>=0)
        # A closed proven socket must fail, never reconnect and expose metadata to another listener.
        conn.auto_open=0
        response,raw=request('/peer-reply/v1/receipt',value,headers)
        signature=response.getheader('X-Watch-Signature','')
        require(hmac.compare_digest(signature,mac(key,'watch-peer-reply-response-v1',port,run,nonce,response.status,hashlib.sha256(raw).hexdigest())))
        require(response.status==200)
    finally: conn.close()


def read_event():
    deadline, raw=time.monotonic()+2,bytearray()
    while len(raw)<=MAX_INPUT:
        remaining=deadline-time.monotonic();require(remaining>0)
        ready,_,_=select.select([0],[],[],remaining);require(ready)
        chunk=os.read(0,min(65536,MAX_INPUT+1-len(raw)))
        if not chunk: break
        raw.extend(chunk)
    return decode(bytes(raw))


def main():
    require(os.name=='posix' and len(sys.argv)==5 and sys.argv[1]=='--provider' and sys.argv[3]=='--credential-file')
    value=receipt(read_event(),sys.argv[2])
    register_receipt(credential(sys.argv[4]),value)


if __name__=='__main__':
    previous_handler=previous_timer=None
    try:
        require(os.name=='posix')
        previous_handler=signal.getsignal(signal.SIGALRM);previous_timer=signal.getitimer(signal.ITIMER_REAL)
        def timeout(signum,frame):raise TimeoutError()
        signal.signal(signal.SIGALRM,timeout);signal.setitimer(signal.ITIMER_REAL,TOTAL_SECONDS)
        main()
    except BaseException:pass  # No decisions, rewrites, diagnostics or model context.
    finally:
        if previous_handler is not None:
            signal.setitimer(signal.ITIMER_REAL,0);signal.signal(signal.SIGALRM,previous_handler)
            if previous_timer[0]>0:signal.setitimer(signal.ITIMER_REAL,*previous_timer)

