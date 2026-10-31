#!/usr/bin/env python3
"""Offline CLI setup fixtures, no real credentials or host configuration writes."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import watch_peer as w
import watch_claude as c


@unittest.skipIf(os.name == 'nt', 'POSIX private state')
class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.temp.name)
        self.state = os.path.join(self.root, 'state')
        self.sessions = os.path.join(self.root, 'sessions')
        os.mkdir(self.sessions)
        self.session = str(uuid.uuid4())
        self.path = os.path.join(self.sessions, 'rollout-' + self.session + '.jsonl')
        with open(self.path, 'w') as f:
            f.write(json.dumps({'type': 'session_meta', 'payload': {'id': self.session, 'cwd': self.root, 'source': 'vscode'}}) + '\n')

    def tearDown(self):
        self.temp.cleanup()

    def runcli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            w.main(['--state-dir', self.state, '--sessions-dir', self.sessions, *args])
        return output.getvalue()

    def test_enroll_config_revoke_offline_shared_schema(self):
        result = json.loads(self.runcli('enroll', '--label', 'Example endpoint', '--session', self.session, '--project', self.root))
        path = result['credential_file']
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        credential = w.read_credential(path)
        config = w.config(path, '/usr/bin/node')
        self.assertNotIn(credential['secret'], json.dumps(config))
        self.assertEqual(config['mcpServers']['watch-peer']['args'][-2:], ['--credential', path])
        snippet = self.runcli('config', path, '--node', '/usr/bin/node')
        self.assertIn('[mcp_servers.watch_peer]', snippet)
        self.assertNotIn(credential['secret'], snippet)
        try:
            import tomllib
        except ImportError:
            pass
        else:
            self.assertEqual(tomllib.loads(snippet)['mcp_servers']['watch_peer']['args'][-1], path)
        self.assertEqual(json.loads(self.runcli('status'))[0]['binding'], 'declared')
        self.runcli('receive', result['endpoint_id'], 'off')
        self.assertFalse(json.loads(self.runcli('status'))[0]['receive'])
        self.runcli('revoke', result['endpoint_id'])
        self.assertFalse(json.loads(self.runcli('status'))[0]['active'])

    def test_parent_root_agent_path_allowed_but_child_rejected(self):
        for agent_path, accepted in (('/root', True), ('/root/child', False)):
            with self.subTest(agent_path=agent_path):
                with open(self.path, 'w') as f:
                    f.write(json.dumps({'type': 'session_meta', 'payload': {
                        'id': self.session, 'cwd': self.root, 'source': 'vscode', 'agent_path': agent_path}}) + '\n')
                if accepted:
                    self.assertEqual(w.transcript_identity(self.sessions, self.session, self.root)['path'], self.path)
                else:
                    with self.assertRaises(ValueError):
                        w.transcript_identity(self.sessions, self.session, self.root)

    def test_project_mismatch_and_duplicate_transcript_fail(self):
        with self.assertRaises(ValueError):
            w.transcript_identity(self.sessions, self.session, self.root + '-other')
        Path(self.sessions, 'second-' + self.session + '.jsonl').write_bytes(Path(self.path).read_bytes())
        with self.assertRaises(ValueError):
            w.transcript_identity(self.sessions, self.session, self.root)

    def test_launch_pre_enroll_without_descriptor_and_secret_not_in_argv(self):
        instance = str(uuid.uuid4())
        path = w.enroll_launch(self.state, self.session, instance, self.root, 'Claude endpoint')
        secret = w.read_credential(path)['secret']
        # Legacy fixture enrollment remains readable for migration, but the
        # launcher cannot inject a duplicate per-chat MCP server.
        with self.assertRaises(TypeError):
            c.command('/bin/claude', '/bin/node', Path('/example/channel.mjs'), Path('/example/state'),
                      self.session, instance, peer_credential=path)
        row = json.loads(self.runcli('status'))[0]
        self.assertEqual(row['binding'], 'launch')
        self.assertFalse(row['reachable'])
        self.runcli('prune')
        self.assertFalse(json.loads(self.runcli('status'))[0]['active'])

    def test_legacy_claude_live_descriptor_cannot_claim_peer_reachability(self):
        session, instance = str(uuid.uuid4()), str(uuid.uuid4())
        credential_path = w.enroll_launch(self.state, session, instance, self.root, 'Claude endpoint')
        credential = w.read_credential(credential_path)
        channel = Path(self.state, 'claude-channels', session)
        channel.mkdir(mode=0o700, parents=True)
        channel.parent.chmod(0o700)
        descriptor = channel / 'connection.json'
        descriptor.write_text(json.dumps(dict(version=2, peer_protocol=1, project=self.root,
            session_id=session, instance_id=instance, port=8793, token='a'*64)))
        descriptor.chmod(0o600)
        codex = json.loads(self.runcli('enroll', '--label', 'Codex endpoint', '--session', self.session, '--project', self.root))
        rows = {row['id']: row for row in json.loads(self.runcli('status'))}
        self.assertFalse(rows[credential['endpoint_id']]['reachable'])
        self.assertEqual(rows[credential['endpoint_id']]['reason'], w.claude_channel.PEER_IDENTITY_REASON)
        self.assertTrue(rows[codex['endpoint_id']]['reachable'])
        # A compatible descriptor remains valid for legacy maintenance: the
        # reporting gate must not cause prune to revoke this endpoint.
        self.runcli('prune')
        rows = {row['id']: row for row in json.loads(self.runcli('status'))}
        self.assertTrue(rows[credential['endpoint_id']]['active'])
        self.runcli('revoke', credential['endpoint_id'])
        rows = {row['id']: row for row in json.loads(self.runcli('status'))}
        self.assertEqual((rows[credential['endpoint_id']]['reachable'], rows[credential['endpoint_id']]['reason']), (False, 'Revoked.'))

    def test_shared_setup_policy_rotation_and_both_client_configs(self):
        result = json.loads(self.runcli('init', '--providers', 'codex,claude'))
        self.assertEqual(set(result), {'credential_file'})
        path = result['credential_file']; credential = w.read_credential(path)
        self.assertEqual(set(credential), {'version', 'credential_id', 'secret', 'port'})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        status = json.loads(self.runcli('status'))
        self.assertEqual(status['policy']['history_days'], 14)
        for client in ('codex', 'claude'):
            config = self.runcli('config', path, '--node', '/usr/bin/node', '--client', client)
            self.assertNotIn(credential['secret'], config)
            self.assertIn(path, config)
            if client == 'claude': self.assertIn('mcpServers', json.loads(config))
        db = w.p.open_database(self.state)
        key = db.execute('SELECT ledger_key FROM shared_bridge_service').fetchone()[0]; db.close()
        rotated = json.loads(self.runcli('rotate', credential['credential_id']))
        replacement = w.read_credential(rotated['credential_file'])
        self.assertNotEqual(replacement['secret'], credential['secret'])
        db = w.p.open_database(self.state)
        self.assertEqual(db.execute('SELECT ledger_key FROM shared_bridge_service').fetchone()[0], key)
        with self.assertRaises(w.p.PeerError): w.p.service_credential(db, credential['credential_id'])
        db.close()
        self.runcli('revoke', replacement['credential_id'])
        self.assertTrue(all(not c['active'] for c in json.loads(self.runcli('status'))['credentials']))

    def test_shared_all_history_filters_and_no_legacy_enrollment(self):
        self.runcli('init', '--providers', 'codex', '--history', 'all', '--allow-project', self.root)
        policy = json.loads(self.runcli('status'))['policy']
        self.assertEqual(policy['history_days'], 'all')
        self.assertEqual(policy['project_allow'], [self.root])
        with self.assertRaises(ValueError):
            self.runcli('enroll', '--label', 'No endpoint', '--session', self.session, '--project', self.root)
        with self.assertRaises(ValueError):
            w.enroll_launch(self.state, self.session, str(uuid.uuid4()), self.root, 'No endpoint')
        help_text = w.parser().format_help()
        self.assertNotIn('enroll', help_text)
        self.assertNotIn('prune', help_text)

    def test_failed_initial_credential_write_can_be_recovered_by_rotation(self):
        with patch.object(w, 'write_credential', side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError): self.runcli('init', '--providers', 'codex')
        status = json.loads(self.runcli('status'))
        self.assertFalse(status['credentials'][0]['active'])
        result = json.loads(self.runcli('rotate', status['credentials'][0]['credential_id']))
        self.assertEqual(w.read_credential(result['credential_file'])['version'], 2)

    def test_offline_policy_update_preserves_omitted_fields_and_service_history(self):
        self.runcli('init', '--providers', 'codex,claude', '--history', 'all', '--allow-project', self.root)
        db = w.p.open_database(self.state)
        tables = ('shared_bridge_credentials', 'shared_bridge_requests', 'requests')
        transport, request = str(uuid.uuid4()), str(uuid.uuid4())
        with db:
            db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)',
                (request, 'x:'+self.session, '{}', 'retained-hash', transport, str(uuid.uuid4()), 1, 2, 'retained preview'))
            db.execute('INSERT INTO requests VALUES (?,?)', (transport, '{"retained":"history"}'))
        retained = {table:db.execute('SELECT * FROM '+table).fetchall() for table in tables}
        key = db.execute('SELECT ledger_key FROM shared_bridge_service').fetchone()[0]
        db.close()
        denied = os.path.join(self.root, 'private')
        updated = json.loads(self.runcli('policy', '--deny-project', denied))
        self.assertTrue(updated['restart_required'])
        self.assertEqual(updated['policy'], dict(providers=['codex','claude'], history_days='all',
            project_allow=[self.root], project_deny=[denied]))
        updated = json.loads(self.runcli('policy', '--providers', 'codex', '--history', '14', '--clear-allow-projects'))
        self.assertEqual(updated['policy'], dict(providers=['codex'], history_days=14, project_allow=[], project_deny=[denied]))
        updated = json.loads(self.runcli('policy', '--clear-deny-projects'))
        self.assertEqual(updated['policy']['project_deny'], [])
        db = w.p.open_database(self.state)
        self.assertEqual(db.execute('SELECT ledger_key FROM shared_bridge_service').fetchone()[0], key)
        self.assertEqual({table:db.execute('SELECT * FROM '+table).fetchall() for table in tables}, retained)
        db.close()

    def test_policy_update_requires_runtime_ownership_before_opening_database(self):
        import officer_messaging
        self.runcli('init', '--providers', 'codex')
        store = officer_messaging.MessagingStore(self.state, self.sessions)
        try:
            with patch.object(w.p, 'open_database', wraps=w.p.open_database) as opened:
                with self.assertRaises(w.p.PeerError) as error:
                    self.runcli('policy', '--history', 'all')
                self.assertEqual(error.exception.status, 409)
                opened.assert_not_called()
        finally:
            store.close()
        # A failed validation releases the lock and rolls the policy back.
        original = json.loads(self.runcli('status'))['policy']
        with self.assertRaises(ValueError): self.runcli('policy', '--history', '0')
        self.assertEqual(json.loads(self.runcli('status'))['policy'], original)
        store = officer_messaging.MessagingStore(self.state, self.sessions)
        store.close()
        with self.assertRaises(w.p.PeerError): self.runcli('policy')

    def test_policy_transaction_holds_runtime_lock_until_database_closes(self):
        import fcntl
        self.runcli('init', '--providers', 'codex')
        original_open = w.p.open_database
        def checked_open(state):
            # The helper must already own the runtime lock even before this read.
            fd = os.open(os.path.join(state, 'officer-messages.lock'), os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError): fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
            return original_open(state)
        with patch.object(w.p, 'open_database', side_effect=checked_open):
            self.runcli('policy', '--history', 'all')
        fd = os.open(os.path.join(self.state, 'officer-messages.lock'), os.O_RDWR)
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally: os.close(fd)

    def test_invalid_shared_policy_creates_no_credential(self):
        for args in (('--providers', 'codex,unknown'), ('--providers', 'codex', '--history', '0')):
            with self.assertRaises(ValueError): self.runcli('init', *args)
        self.assertFalse(os.path.exists(os.path.join(self.state, 'bridge-credentials')))

    def test_unsafe_credentials_rejected(self):
        path = w.enroll_launch(self.state, self.session, str(uuid.uuid4()), self.root, 'Endpoint')
        os.chmod(path, 0o644)
        with self.assertRaises(Exception):
            w.read_credential(path)
        os.chmod(path, 0o600)
        link = path + '-link'
        os.symlink(path, link)
        with self.assertRaises(OSError):
            w.read_credential(link)


if __name__ == '__main__':
    unittest.main()
