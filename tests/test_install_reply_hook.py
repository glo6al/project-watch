"""Owner installer uses private temporary HOME only; never native hooks or trust state."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import install_reply_hook as installer


@unittest.skipIf(os.name=='nt','POSIX private user hook installation')
class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.home=Path(self.tmp.name).resolve()
        self.state=self.home/'.live-room';self.state.mkdir(mode=0o700)
        self.store=self.state/'reply-hook';self.store.mkdir(mode=0o700)
        self.paths={'codex':self.home/'.codex/hooks.json','claude':self.home/'.claude/settings.json'}
        self.original={}
        for provider,path in self.paths.items():
            path.parent.mkdir(mode=0o700)
            raw=json.dumps({'unrelated':provider,'hooks':{'PermissionRequest':[{'hooks':[{'type':'command','command':'fictional existing permission'}]}],
                 'PostToolUse':[{'matcher':'^other_tool$','hooks':[{'type':'command','command':'fictional other observer'}]}]}}).encode()
            path.write_bytes(raw);path.chmod(0o600);self.original[provider]=json.loads(raw)
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
        import peer_reply_delivery
        self.backend=peer_reply_delivery
        self.credential=self.backend.provision(str(self.state),['codex','claude'])
    def tearDown(self):self.tmp.cleanup()
    def main(self,*extra):
        args=['install_reply_hook.py','--provider','both',*extra]
        with patch.object(sys,'argv',args),patch.object(installer.Path,'home',return_value=self.home),contextlib.redirect_stdout(io.StringIO()):installer.main()
    def test_global_install_idempotence_remove_and_unrelated_preservation(self):
        with self.assertRaises(ValueError):self.main('--credential-file',self.credential)
        self.main('--credential-file',self.credential,'--accept-native-parent-return')
        first={p:p.read_bytes() for p in self.paths.values()}
        for provider,path in self.paths.items():
            value=json.loads(path.read_bytes());owned=value['hooks']['PostToolUse'][-1]
            self.assertEqual(owned['matcher'],'^'+installer.hook.TOOLS[provider]+'$')
            command=owned['hooks'][0]['command']
            self.assertIn(str(self.store/'watch_reply_hook.py'),command)
            with open(self.credential) as source:self.assertNotIn(json.load(source)['secret'],command)
            self.assertEqual(value['hooks']['PermissionRequest'],self.original[provider]['hooks']['PermissionRequest'])
        self.main('--credential-file',self.credential,'--accept-native-parent-return')
        self.assertEqual(first,{p:p.read_bytes() for p in self.paths.values()})
        self.main('--remove');self.main('--remove')
        for provider,path in self.paths.items():self.assertEqual(json.loads(path.read_bytes()),self.original[provider])
        self.assertFalse((self.home/'.codex/config.toml').exists())
    def test_offline_second_provider_failure_restores_prior_bytes_and_grant(self):
        self.main('--credential-file',self.credential,'--accept-native-parent-return')
        helper=self.store/'watch_reply_hook.py';helper.write_bytes(helper.read_bytes()+b'\n# prior working fixture version\n')
        tracked=list(self.paths.values())+[self.store/'install-codex.json',self.store/'install-claude.json',helper,Path(self.credential)]
        before={p:p.read_bytes() for p in tracked}
        configure=installer.configure
        def second_failure(provider,*args,**kwargs):
            if provider=='claude':raise ValueError('fictional second config failure')
            return configure(provider,*args,**kwargs)
        with patch.object(installer,'configure',side_effect=second_failure):
            with self.assertRaises(ValueError):self.main('--setup','--accept-native-parent-return')
        self.assertEqual(before,{p:p.read_bytes() for p in tracked},'prior hooks/manifests/helper/credential must restore byte-exact')
        db=sqlite3.connect(self.state/'officer-messages.sqlite3')
        try:rows=db.execute('SELECT id FROM peer_reply_hook_credentials WHERE active=1').fetchall()
        finally:db.close()
        with open(self.credential) as f:old=json.load(f)
        self.assertEqual(rows,[(old['credential_id'],)],'failed install must not retain an active new credential')
        self.assertEqual(len(list((self.state/'reply-hook-credentials').glob('*.json'))),1)
    def test_offline_remove_revoke_is_atomic_with_configuration_failure(self):
        self.main('--credential-file',self.credential,'--accept-native-parent-return')
        tracked=list(self.paths.values())+[self.store/'install-codex.json',self.store/'install-claude.json',self.store/'watch_reply_hook.py']
        before={p:p.read_bytes() for p in tracked};configure=installer.configure
        def fail_second(provider,*args,**kwargs):
            if provider=='claude':raise ValueError('fictional removal failure')
            return configure(provider,*args,**kwargs)
        with patch.object(installer,'configure',side_effect=fail_second):
            with self.assertRaises(ValueError):self.main('--remove','--revoke')
        self.assertEqual(before,{p:p.read_bytes() for p in tracked})
        db=sqlite3.connect(self.state/'officer-messages.sqlite3')
        try:self.assertEqual(db.execute('SELECT COUNT(*) FROM peer_reply_hook_credentials WHERE active=1').fetchone()[0],1)
        finally:db.close()
        self.main('--remove','--revoke')
        db=sqlite3.connect(self.state/'officer-messages.sqlite3')
        try:self.assertEqual(db.execute('SELECT COUNT(*) FROM peer_reply_hook_credentials WHERE active=1').fetchone()[0],0)
        finally:db.close()
        for provider,path in self.paths.items():self.assertEqual(json.loads(path.read_bytes()),self.original[provider])

    def test_post_callback_commit_failure_restores_setup_and_revoke(self):
        self.main('--credential-file',self.credential,'--accept-native-parent-return')
        tracked=list(self.paths.values())+[self.store/'install-codex.json',self.store/'install-claude.json',self.store/'watch_reply_hook.py']
        before={p:p.read_bytes() for p in tracked};transaction=self.backend.p.transaction;reached=[]
        @contextlib.contextmanager
        def failed_commit(db,*args,**kwargs):
            with transaction(db,*args,**kwargs):
                yield
                if any(p.read_bytes()!=before[p] for p in self.paths.values()):
                    reached.append(True)
                    raise OSError('fictional final commit failure after install callback')
        for operation in [('--setup','--accept-native-parent-return'),('--remove','--revoke')]:
            with patch.object(self.backend.p,'transaction',failed_commit):
                with self.assertRaises(OSError):self.main(*operation)
            self.assertEqual(before,{p:p.read_bytes() for p in tracked})
            self.assertEqual(len(list((self.state/'reply-hook-credentials').glob('*.json'))),1)
            with open(self.credential) as f:old=json.load(f)
            db=sqlite3.connect(self.state/'officer-messages.sqlite3')
            try:self.assertEqual(db.execute('SELECT id FROM peer_reply_hook_credentials WHERE active=1').fetchall(),[(old['credential_id'],)])
            finally:db.close()
        self.assertEqual(reached,[True,True],'both failures must occur after actual config replacement')

    def test_concurrent_edit_symlink_and_private_backup_guards(self):
        replace=installer.replace;target=self.paths['codex'];replacement=b'{"unrelated":"concurrent owner edit"}'
        def edit(path,*args):
            if Path(path)==target:target.write_bytes(replacement)
            return replace(path,*args)
        with patch.object(installer,'replace',side_effect=edit):
            with self.assertRaises(ValueError):self.main('--credential-file',self.credential,'--accept-native-parent-return')
        self.assertEqual(target.read_bytes(),replacement)
        target.unlink();victim=self.home/'victim';victim.write_text('unchanged');target.symlink_to(victim)
        with self.assertRaises(OSError):self.main('--credential-file',self.credential,'--accept-native-parent-return')
        self.assertEqual(victim.read_text(),'unchanged')
        for path in self.store.glob('backup-*.json'):self.assertEqual(path.stat().st_mode&0o777,0o600)


if __name__=='__main__':unittest.main()
