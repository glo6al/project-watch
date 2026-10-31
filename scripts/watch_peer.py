#!/usr/bin/env python3
"""Set up shared WATCH service access. Never edits host configuration."""
import argparse
import glob
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))
import peer_bridge as p
import claude_channel


def parser():
    out = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    out.add_argument('--state-dir', default=str(Path.home() / '.live-room'))
    out.add_argument('--sessions-dir', default=str(Path.home() / '.codex/sessions'))
    out.add_argument('--port', type=int, default=8793)
    sub = out.add_subparsers(dest='command', required=True, metavar='{init,config,status,policy,rotate,revoke}')
    enroll = sub.add_parser('enroll', help='Enroll a declared Codex endpoint; identity is capability possession, not chat attestation')
    enroll.add_argument('--label', required=True)
    enroll.add_argument('--session', required=True)
    enroll.add_argument('--project', required=True)
    init = sub.add_parser('init', help='Initialize one shared service and private access credential')
    init.add_argument('--providers', required=True, help='Comma-separated codex,claude provider selection')
    init.add_argument('--history', default='14', help='History in days, or all')
    init.add_argument('--allow-project', action='append', default=[])
    init.add_argument('--deny-project', action='append', default=[])
    policy = sub.add_parser('policy', help='Update discovery scope while WATCH is stopped; restart required')
    policy.add_argument('--providers', help='Replace the comma-separated provider selection')
    policy.add_argument('--history', help='History in days, or all')
    allow = policy.add_mutually_exclusive_group()
    allow.add_argument('--allow-project', action='append')
    allow.add_argument('--clear-allow-projects', action='store_true')
    deny = policy.add_mutually_exclusive_group()
    deny.add_argument('--deny-project', action='append')
    deny.add_argument('--clear-deny-projects', action='store_true')
    rotate = sub.add_parser('rotate', help='Replace service access while retaining request deduplication')
    rotate.add_argument('credential_id')
    sub.add_parser('status')
    sub.add_parser('prune', help='Revoke launch-bound Claude endpoints whose descriptor is absent or changed')
    revoke = sub.add_parser('revoke')
    revoke.add_argument('endpoint_id')
    receive = sub.add_parser('receive')
    receive.add_argument('endpoint_id')
    receive.add_argument('enabled', choices=('on', 'off'))
    config = sub.add_parser('config')
    config.add_argument('credential_file')
    config.add_argument('--node')
    config.add_argument('--client', choices=('codex', 'claude'), default='codex')
    sub._choices_actions[:] = [action for action in sub._choices_actions if action.dest not in ('enroll', 'receive', 'prune')]
    return out


def transcript_identity(sessions, session, project):
    if not p.is_uuid(session):
        raise ValueError('Use a canonical session UUID.')
    sessions = os.path.realpath(sessions)
    matches = []
    for n, path in enumerate(glob.iglob(os.path.join(sessions, '**', '*.jsonl'), recursive=True)):
        if n >= 20000:
            raise ValueError('Too many transcripts to resolve safely.')
        if path.endswith(session + '.jsonl'):
            matches.append(path)
    if len(matches) != 1:
        raise ValueError('The session must have exactly one transcript.')
    path = matches[0]
    if path != os.path.realpath(path) or not path.startswith(sessions + os.sep):
        raise ValueError('Use a plain local transcript.')
    with open(path, 'rb') as f:
        line = f.readline(1000001)
        st = os.fstat(f.fileno())
    if len(line) > 1000000 or not line.endswith(b'\n') or not stat.S_ISREG(st.st_mode):
        raise ValueError('The transcript is not ready.')
    event = json.loads(line)
    meta = event.get('payload', {})
    if (event.get('type') != 'session_meta' or meta.get('id') != session or not meta.get('cwd')
            or os.path.realpath(meta['cwd']) != project or meta.get('originator') == 'codex_exec'
            or meta.get('source') == 'exec' or meta.get('thread_source') == 'guardian_review'
            or meta.get('parent_thread_id') or meta.get('agent_path') not in (None, '', '/root')):
        raise ValueError('The transcript must identify this interactive session and exact project root.')
    return {'path': path, 'inode': st.st_ino, 'device': st.st_dev}


def write_credential(state, credential, port):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Use a valid WATCH port.')
    directory = p.private_directory(os.path.join(state, 'bridge-credentials' if credential['version'] == 2 else 'peer-credentials'), True)
    path = os.path.join(directory, credential.get('credential_id', credential.get('endpoint_id')) + '.json')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(dict(credential, port=port), f)
        f.flush()
        os.fsync(f.fileno())
    return path


def enroll_launch(state, session, instance, project, label, port=8793):
    """Pre-enroll before exec: the channel descriptor does not exist yet."""
    project = os.path.realpath(project)
    p.private_directory(state, True)
    p.private_directory(os.path.join(state, 'peer-credentials'), True)
    if not p.is_uuid(instance) or not os.path.isdir(project) or not 1 <= port <= 65535:
        raise ValueError('Invalid launch endpoint.')
    db = p.open_database(state)
    try:
        with p.transaction(db):
            p.shared_schema(db)
        if p.shared_enabled(db):
            raise ValueError('Per-chat enrollment is disabled in shared service mode.')
        credential = p.enroll(db, label, 'claude', session, project, {'instance_id': instance}, binding='launch')
        try:
            return write_credential(state, credential, port)
        except BaseException:
            p.update_endpoint(db, credential['endpoint_id'], revoke=True)
            raise
    finally:
        db.close()


def read_credential(path):
    path = os.path.abspath(path)
    directory = p.private_directory(os.path.dirname(path))
    p.private_directory(os.path.dirname(directory))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        claude_channel._private(os.fstat(fd))
        raw = os.read(fd, 8193)
    finally:
        os.close(fd)
    data = json.loads(raw)
    if isinstance(data, dict) and data.get('version') == 2:
        if (len(raw) > 8192 or set(data) != {'version', 'credential_id', 'secret', 'port'}
                or not p.is_uuid(data.get('credential_id')) or not isinstance(data.get('secret'), str)
                or not p.HEX.fullmatch(data['secret']) or type(data.get('port')) is not int or not 1 <= data['port'] <= 65535):
            raise ValueError('Invalid shared credential file.')
        return data
    if (len(raw) > 8192 or not isinstance(data, dict) or data.get('version') != 1
            or not p.is_uuid(data.get('endpoint_id')) or not isinstance(data.get('secret'), str)
            or not p.HEX.fullmatch(data['secret']) or type(data.get('port')) is not int or not 1 <= data['port'] <= 65535
            or not isinstance(data.get('project'), str) or data['project'] != os.path.realpath(data['project'])):
        raise ValueError('Invalid credential file.')
    return data


def config(path, node=None):
    read_credential(path)
    node = node or shutil.which('node')
    if not node:
        raise ValueError('Node.js is required.')
    return {'mcpServers': {'watch-peer': {'command': os.path.abspath(node),
            'args': [str(ROOT / 'integrations/claude/peer.mjs'), '--credential', os.path.abspath(path)]}}}


def status(db, state):
    rows = []
    for (ident,) in db.execute('SELECT id FROM peer_principals ORDER BY created').fetchall():
        endpoint = p.principal(db, ident, active=False)
        reachable, reason = False, 'Revoked.'
        if endpoint['active']:
            try:
                if endpoint['runtime'] == 'claude':
                    descriptor = claude_channel.discover(os.path.join(state, 'claude-channels'), endpoint['session'])
                    if (descriptor.get('version') != 2 or descriptor.get('peer_protocol') != 1
                            or descriptor.get('project') != endpoint['project'] or descriptor.get('instance_id') != endpoint['identity'].get('instance_id')):
                        raise ValueError()
                else:
                    identity = endpoint['identity']
                    info = os.stat(identity['path'])
                    if (info.st_ino, info.st_dev) != (identity['inode'], identity['device']):
                        raise ValueError()
                reachable, reason = bool(endpoint['receive']), '' if endpoint['receive'] else 'Receiving is disabled.'
            except Exception:
                reason = 'The enrolled endpoint is not reachable.'
        if reachable and endpoint['runtime'] == 'claude':
            reachable, reason = False, claude_channel.PEER_IDENTITY_REASON
        rows.append({k: endpoint[k] for k in ('id', 'label', 'runtime', 'binding')} | {
            'active': bool(endpoint['active']), 'receive': bool(endpoint['receive']), 'reachable': reachable, 'reason': reason})
    return rows


def main(argv=None):
    args = parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise ValueError('Use a valid WATCH port.')
    if args.command == 'config':
        settings = config(args.credential_file, args.node)['mcpServers']['watch-peer']
        if args.client == 'claude':
            print(json.dumps({'mcpServers': {'watch-peer': settings}}, indent=2))
            return
        print('[mcp_servers.watch_peer]')
        print('command = ' + json.dumps(settings['command']))
        print('args = ' + json.dumps(settings['args']))
        return
    if args.command == 'policy':
        changes = {}
        if args.providers is not None:
            changes['providers'] = args.providers.split(',')
        if args.history is not None:
            changes['history_days'] = 'all' if args.history == 'all' else int(args.history)
        for kind in ('allow', 'deny'):
            selected = getattr(args, kind + '_project')
            if selected is not None or getattr(args, 'clear_' + kind + '_projects'):
                changes['project_' + kind] = selected or []
        policy = p.update_service_policy(args.state_dir, changes)
        print(json.dumps({'policy': policy, 'restart_required': True}))
        return
    identity, project = None, None
    if args.command == 'enroll':
        project = os.path.realpath(args.project)
        if not os.path.isdir(project):
            raise ValueError('The project directory does not exist.')
        identity = transcript_identity(args.sessions_dir, args.session, project)
        p.private_directory(args.state_dir, True)
        p.private_directory(os.path.join(args.state_dir, 'peer-credentials'), True)
    db = p.open_database(args.state_dir)
    try:
        with p.transaction(db):
            p.shared_schema(db)
        enabled = p.shared_enabled(db)
        if args.command == 'init':
            history = 'all' if args.history == 'all' else int(args.history)
            policy = dict(providers=args.providers.split(','), history_days=history,
                          project_allow=args.allow_project, project_deny=args.deny_project)
            credential = p.initialize_service(db, policy)
            try:
                path = write_credential(args.state_dir, credential, args.port)
            except BaseException:
                with p.transaction(db):
                    db.execute('UPDATE shared_bridge_credentials SET active=0 WHERE id=?', (credential['credential_id'],))
                raise
            print(json.dumps({'credential_file': path}))
            return
        if args.command == 'rotate':
            with p.transaction(db):
                if not p.is_uuid(args.credential_id) or not db.execute('SELECT 1 FROM shared_bridge_credentials WHERE id=?', (args.credential_id,)).fetchone():
                    raise ValueError('Unknown service credential.')
                credential = p.issue_service_credential(db)
                # Write before commit; failed file creation leaves old credential active.
                path = write_credential(args.state_dir, credential, args.port)
                db.execute('UPDATE shared_bridge_credentials SET active=0 WHERE id=?', (args.credential_id,))
            print(json.dumps({'credential_file': path}))
            return
        if enabled:
            if args.command == 'revoke':
                with p.transaction(db):
                    p.service_credential(db, args.endpoint_id)
                    db.execute('UPDATE shared_bridge_credentials SET active=0 WHERE id=?', (args.endpoint_id,))
                print('Service access revoked. Already-authorized deliveries cannot be recalled.')
                return
            if args.command == 'status':
                policy = json.loads(db.execute('SELECT policy FROM shared_bridge_service WHERE singleton=1').fetchone()[0])
                rows = [dict(zip(('credential_id', 'active', 'created_at'), row)) for row in
                        db.execute('SELECT id,active,created FROM shared_bridge_credentials ORDER BY created')]
                print(json.dumps({'version': 2, 'policy': policy, 'credentials': rows}, indent=2))
                return
            raise ValueError('Per-chat enrollment is disabled in shared service mode.')
        if args.command == 'enroll':
            credential = p.enroll(db, args.label, 'codex', args.session, project, identity)
            try:
                path = write_credential(args.state_dir, credential, args.port)
            except BaseException:
                p.update_endpoint(db, credential['endpoint_id'], revoke=True)
                raise
            print(json.dumps({'endpoint_id': credential['endpoint_id'], 'credential_file': path}))
        elif args.command == 'revoke':
            p.update_endpoint(db, args.endpoint_id, revoke=True)
            print('Endpoint revoked. Already-authorized deliveries cannot be recalled.')
        elif args.command == 'receive':
            p.update_endpoint(db, args.endpoint_id, receive=args.enabled == 'on')
            print('Receiving ' + args.enabled + '.')
        else:
            rows = status(db, args.state_dir)  # all filesystem checks outside write transaction
            if args.command == 'prune':
                for row in rows:
                    if row['active'] and row['binding'] == 'launch' and row['reason'] == 'The enrolled endpoint is not reachable.':
                        p.update_endpoint(db, row['id'], revoke=True)
                rows = status(db, args.state_dir)
            print(json.dumps(rows, indent=2))
    finally:
        db.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('WATCH setup failed. Check the command, selected policy, and private state files.', file=sys.stderr)
        sys.exit(1)
