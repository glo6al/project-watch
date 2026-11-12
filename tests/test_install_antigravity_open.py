#!/usr/bin/env python3
"""Fictional installation/runtime fixtures; never execute or contact native apps."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import sys
import tempfile
import types
import uuid
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import antigravity_navigation as navigation


@unittest.skipUnless(os.name == 'posix', 'Native installation is currently macOS only.')
class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(os.path.realpath(self.temp.name))
        self.platform = mock.patch.object(navigation.sys, 'platform', 'darwin')
        self.platform.start(); self.addCleanup(self.platform.stop)
        self.config = self.home / '.gemini' / 'config' / 'config.json'
        self.config.parent.mkdir(parents=True)
        self.initial = {'theme': {'mode': 'fictional'}, 'sidecars': {'other-extension': {'enabled': True, 'native': 7}}}
        self.config.write_text(json.dumps(self.initial))
        self.config.chmod(0o600)

    def grant(self, enabled):
        navigation.ensure_private(self.home)
        path = navigation.private_dir(self.home) / 'authorization.json'
        before, _ = navigation.snapshot(path, private=True)
        navigation.replace(path, navigation.encoded(dict(protocol=1, enabled=enabled)), before)

    def test_install_once_user_scope_preserves_config_and_requires_no_target_list(self):
        out = navigation.install(self.home, ROOT)
        self.assertEqual(set(out), {'supported','installed','authorized','state','message'})
        self.assertEqual(out['state'], 'waiting_panel')
        self.assertTrue(out['authorized'])
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        current = json.loads(self.config.read_text())
        self.assertEqual(current.get('theme'), self.initial['theme'])
        self.assertEqual(current['sidecars']['other-extension'], self.initial['sidecars']['other-extension'])
        self.assertEqual(self.config.read_text(), json.dumps(self.initial), 'Native shared configuration is never rewritten.')
        self.assertEqual(navigation.private_dir(self.home).stat().st_mode & 0o777, 0o700)
        for path in navigation.private_dir(self.home).iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(navigation.install(self.home, ROOT), out)
        serialized = json.dumps(out)
        self.assertNotIn(str(self.home), serialized)
        self.assertNotIn('owner_token', serialized)
        self.assertNotIn('allowed_ids', json.dumps(navigation.private_json(self.home, 'installation.json')))

    def test_disconnect_revokes_without_touching_native_settings_or_loaded_process(self):
        navigation.install(self.home, ROOT)
        value = json.loads(self.config.read_text())
        value['sidecars'][navigation.ID] = {'enabled':True,'nativeAdded':{'retain':True}}
        before=json.dumps(value).encode();self.config.write_bytes(before)
        real_replace = navigation.replace
        def checked(path,*args,**kwargs):
            self.assertNotEqual(path,self.config,'No shared vendor configuration write is permitted.')
            return real_replace(path,*args,**kwargs)
        with mock.patch.object(navigation,'replace',side_effect=checked):
            out=navigation.disconnect(self.home)
        self.assertFalse(out['authorized']);self.assertEqual(out['state'],'disconnected')
        self.assertEqual(self.config.read_bytes(),before)
        self.assertTrue((navigation.sidecar_dir(self.home)/'broker.py').exists())
        self.assertTrue(navigation.install(self.home,ROOT)['authorized'],'Regrant can resume the still-loaded idle connector.')
        self.assertEqual(self.config.read_bytes(),before)

    def test_remove_only_owned_artifacts_and_preserve_native_configuration(self):
        navigation.install(self.home, ROOT)
        value = json.loads(self.config.read_text())
        value['sidecars'][navigation.ID] = {'enabled':False,'nativeAdded':3}
        self.config.write_text(json.dumps(value))
        added = navigation.sidecar_dir(self.home) / 'native-added.txt'
        added.write_text('fictional native extension state')
        out = navigation.remove(self.home)
        self.assertFalse(out['installed'])
        self.assertFalse(navigation.authorized(self.home))
        self.assertEqual(added.read_text(), 'fictional native extension state')
        self.assertEqual(json.loads(self.config.read_text())['sidecars'][navigation.ID], {'enabled':False,'nativeAdded':3})
        self.assertFalse((navigation.sidecar_dir(self.home) / 'broker.py').exists())
        self.assertTrue(navigation.install(self.home,ROOT)['installed'],'Private ownership stamp permits reinstall without editing stale native settings.')

    def test_changed_or_foreign_files_are_preserved_and_never_reauthorized(self):
        folder = navigation.sidecar_dir(self.home); folder.mkdir(parents=True)
        (folder / 'broker.py').write_text('fictional foreign file')
        with self.assertRaises(navigation.NavigationError):
            navigation.install(self.home, ROOT)
        self.assertEqual((folder / 'broker.py').read_text(), 'fictional foreign file')
        self.assertFalse(navigation.authorized(self.home))
        (folder / 'broker.py').unlink()
        navigation.install(self.home, ROOT)
        script = folder / 'connector.js'; script.write_text('fictional owner change')
        with self.assertRaises(navigation.NavigationError):
            navigation.remove(self.home)
        self.assertFalse(navigation.authorized(self.home), 'Removal failure must leave revocation in force.')
        self.assertEqual(script.read_text(), 'fictional owner change')
        with self.assertRaises(navigation.NavigationError):
            navigation.install(self.home, ROOT)
        self.assertFalse(navigation.authorized(self.home))

    def test_native_write_at_our_final_replace_survives_success_and_rollback(self):
        real = navigation.replace
        for fail in (True,False):
            changed = dict(self.initial,newNativeKey={'kept':True,'iteration':fail})
            changed_bytes=json.dumps(changed).encode()
            def at_final(path,*args,**kwargs):
                self.assertNotEqual(path,self.config,'Never replace native configuration, even in rollback.')
                if Path(path).name=='authorization.json':
                    self.config.write_bytes(changed_bytes)  # Uncooperative native writer at final commit boundary.
                    if fail:raise OSError('fictional private failure')
                return real(path,*args,**kwargs)
            with mock.patch.object(navigation,'replace',side_effect=at_final):
                if fail:
                    with self.assertRaises(navigation.NavigationError) as failed:navigation.install(self.home,ROOT)
                    self.assertNotIn('fictional private failure',str(failed.exception))
                    self.assertFalse(navigation.authorized(self.home))
                    self.assertFalse((navigation.sidecar_dir(self.home)/'broker.py').exists())
                else:self.assertTrue(navigation.install(self.home,ROOT)['authorized'])
            self.assertEqual(self.config.read_bytes(),changed_bytes)

    def test_atomic_compare_prevents_lost_config_updates(self):
        old = self.config.read_bytes()
        changed = navigation.encoded(dict(self.initial, addedByNative=True))
        self.config.write_bytes(changed)
        with self.assertRaises(navigation.NavigationError):
            navigation.replace(self.config, b'{}', old)
        self.assertEqual(self.config.read_bytes(), changed)

    def test_active_broker_lock_blocks_remove_but_revocation_is_immediate(self):
        navigation.install(self.home, ROOT)
        with navigation.process_lock(self.home):
            with self.assertRaises(navigation.NavigationError) as busy:
                navigation.remove(self.home)
            self.assertEqual(busy.exception.code, 'busy')
            self.assertFalse(navigation.authorized(self.home))
            self.assertTrue((navigation.sidecar_dir(self.home) / 'broker.py').exists())
        navigation.remove(self.home)

    def test_broker_lock_is_exclusive_and_released_after_crash_unwind(self):
        with self.assertRaisesRegex(RuntimeError, 'fictional'):
            with navigation.process_lock(self.home):
                with self.assertRaises(navigation.NavigationError):
                    with navigation.process_lock(self.home):
                        self.fail('Concurrent lock acquired.')
                raise RuntimeError('fictional unwind')
        with navigation.process_lock(self.home):
            pass

    def test_symlink_hardlink_and_public_grant_cannot_authorize(self):
        self.grant(True)
        path = navigation.private_dir(self.home) / 'authorization.json'
        path.chmod(0o644)
        self.assertFalse(navigation.authorized(self.home))
        path.chmod(0o600)
        linked = path.with_name('linked.json'); os.link(path, linked)
        self.assertFalse(navigation.authorized(self.home))
        linked.unlink()
        original = path.read_bytes(); path.unlink(); linked.write_bytes(original); linked.chmod(0o600)
        path.symlink_to(linked)
        self.assertFalse(navigation.authorized(self.home))

    def test_foreign_native_entry_and_directory_links_are_not_overwritten(self):
        value = dict(self.initial); value['sidecars'] = dict(value['sidecars'], **{navigation.ID:{'enabled':True,'foreign':True}})
        self.config.write_text(json.dumps(value))
        before = self.config.read_bytes()
        with self.assertRaises(navigation.NavigationError):
            navigation.install(self.home, ROOT)
        self.assertEqual(self.config.read_bytes(), before)
        self.config.write_text(json.dumps(self.initial))
        folder = navigation.sidecar_dir(self.home)
        folder.rmdir(); elsewhere = self.home / 'fictional-destination'; elsewhere.mkdir()
        folder.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(navigation.NavigationError):
            navigation.install(self.home, ROOT)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_unsupported_platform_never_writes(self):
        for platform in ('win32','linux'):
            with mock.patch.object(navigation.sys, 'platform', platform):
                self.assertFalse(navigation.local_state(self.home)['supported'])
                with self.assertRaises(navigation.NavigationError) as unsupported:
                    navigation.install(self.home, ROOT)
                self.assertEqual(unsupported.exception.code, 'unsupported')
        self.assertFalse(navigation.sidecar_dir(self.home).exists())


@unittest.skipUnless(os.name == 'posix', 'Runtime fixtures use POSIX metadata only.')
class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(os.path.realpath(self.temp.name))
        self.platform = mock.patch.object(navigation.sys, 'platform', 'darwin')
        self.platform.start(); self.addCleanup(self.platform.stop)
        self.bundle = self.home / 'Fictional Antigravity.app'
        info = self.bundle / 'Contents' / 'Info.plist'; info.parent.mkdir(parents=True)
        info.write_bytes(plistlib.dumps({'CFBundleIdentifier':'com.google.antigravity','CFBundleExecutable':'Antigravity','CFBundleShortVersionString':'2.31.2'}))
        self.backend_pid,self.app_pid,self.broker_pid = 41001,41002,41003
        self.start = 'Thu Nov 12 09:00:00 2026'
        self.backend = dict(pid=self.backend_pid,ppid=self.app_pid,start=self.start,executable=str(self.bundle/'Contents'/'Resources'/'bin'/'language_server'))
        self.app = dict(pid=self.app_pid,ppid=1,start=self.start,executable=str(self.bundle/'Contents'/'MacOS'/'Antigravity'))
        self.child = dict(pid=self.broker_pid,ppid=self.backend_pid,start=self.start,executable='/usr/bin/python3')
        self.records = {self.backend_pid:self.backend,self.app_pid:self.app,self.broker_pid:self.child}
        self.proc = mock.patch.object(navigation,'process',side_effect=lambda pid:dict(self.records[pid]))
        self.proc.start(); self.addCleanup(self.proc.stop)
        self.ports = mock.patch.object(navigation,'_listener_ports',return_value={49321})
        self.ports.start(); self.addCleanup(self.ports.stop)
        self.listen = mock.patch.object(navigation,'listener',return_value=True)
        self.listen.start(); self.addCleanup(self.listen.stop)
        log = self.home/'Library'/'Logs'/'Antigravity'/'main.log';log.parent.mkdir(parents=True)
        log.write_text('fictional app startup\nLocal: https://127.0.0.1:49320/\nLocal: https://127.0.0.1:49321/\n')
        self.runtime = navigation.runtime_for_backend(self.backend_pid,self.home)
        navigation.ensure_private(self.home)
        grant=navigation.private_dir(self.home)/'authorization.json'
        navigation.replace(grant,navigation.encoded({'protocol':1,'enabled':True}),None)
        self.receipt=dict(protocol=1,generation='a'*32,url='http://127.0.0.1:49322',owner_token='b'*64,
                          broker_pid=self.broker_pid,broker_start=self.start,runtime=self.runtime)
        self.write_receipt(self.receipt)

    def write_receipt(self,value):
        path=navigation.private_dir(self.home)/'connection.json'
        before,_=navigation.snapshot(path,private=True)
        navigation.replace(path,navigation.encoded(value),before)

    def test_primary_runtime_bound_to_unique_owned_listener_and_bundle(self):
        self.assertEqual(self.runtime['origin'],'https://127.0.0.1:49321')
        self.assertEqual(set(self.runtime),{'backend_pid','backend_start','app_pid','app_start','bundle','version','origin'})
        self.assertTrue(navigation.verify_runtime(self.runtime,self.home))
        self.assertTrue(navigation.validate_connection(navigation.read_connection(self.home),self.home))
        with mock.patch.object(navigation,'_listener_ports',return_value={49320,49321}):
            with self.assertRaises(navigation.NavigationError):
                navigation.runtime_for_backend(self.backend_pid,self.home)

    def test_replaced_broker_parent_listener_and_generation_reject_before_transmission(self):
        original=dict(self.child)
        for field,value in (('start','Thu Nov 12 09:00:01 2026'),('ppid',41004)):
            self.records[self.broker_pid]=dict(original,**{field:value})
            with self.assertRaises(navigation.NavigationError):
                navigation.validate_connection(self.receipt,self.home)
        self.records[self.broker_pid]=original
        with mock.patch.object(navigation,'listener',return_value=False):
            with self.assertRaises(navigation.NavigationError):navigation.validate_connection(self.receipt,self.home)
        self.write_receipt(dict(self.receipt,generation='c'*32))
        with self.assertRaises(navigation.NavigationError):navigation.validate_connection(self.receipt,self.home)

    def test_native_version_bundle_parent_and_start_are_not_guessed(self):
        for field,value in (('start','changed'),('executable','/fictional/other-app')):
            self.records[self.app_pid]=dict(self.app,**{field:value})
            with self.assertRaises(navigation.NavigationError):navigation.verify_runtime(self.runtime,self.home)
        self.records[self.app_pid]=self.app
        info=self.bundle/'Contents'/'Info.plist';value=plistlib.loads(info.read_bytes());value['CFBundleShortVersionString']='9.9.9';info.write_bytes(plistlib.dumps(value))
        with self.assertRaises(navigation.NavigationError) as unsupported:navigation.verify_runtime(self.runtime,self.home)
        self.assertEqual(unsupported.exception.code,'unsupported_version')

    def test_foreground_only_verified_running_bundle_without_uri_or_new_instance(self):
        with mock.patch.object(navigation,'_run',return_value='') as launch:
            self.assertTrue(navigation.foreground(self.runtime,self.home))
            launch.assert_called_once_with(['/usr/bin/open','-a',str(self.bundle)])
        path=navigation.private_dir(self.home)/'authorization.json';raw,_=navigation.snapshot(path,private=True)
        navigation.replace(path,navigation.encoded({'protocol':1,'enabled':False}),raw)
        with mock.patch.object(navigation,'_run') as launch:
            with self.assertRaises(navigation.NavigationError):navigation.foreground(self.runtime,self.home)
            launch.assert_not_called()

    def test_receipt_protection_and_exact_schema_hide_private_errors(self):
        for change in ({'protocol':True},{'url':'http://localhost:49322'},{'url':'http://127.0.0.1:99999'},
                       {'owner_token':'bad'},{'extra':'private'},{'broker_pid':True}):
            self.write_receipt(dict(self.receipt,**change))
            with self.assertRaises(navigation.NavigationError) as error:navigation.read_connection(self.home)
            self.assertNotIn(str(self.home),str(error.exception))
        self.write_receipt(self.receipt)
        (navigation.private_dir(self.home)/'connection.json').chmod(0o644)
        with self.assertRaises(navigation.NavigationError):navigation.read_connection(self.home)

    def test_process_probe_requests_only_public_metadata_not_arguments_or_environment(self):
        self.proc.stop()
        line=f'{self.backend_pid} {self.app_pid} {os.getuid()} {self.start} {self.backend["executable"]}\n'
        with mock.patch.object(navigation,'_run',return_value=line) as run:
            result=navigation.process(self.backend_pid)
            self.assertEqual(result,self.backend)
            argv=run.call_args.args[0]
            self.assertEqual(argv[-1],'pid=,ppid=,uid=,lstart=,comm=')
            self.assertNotIn('args',argv[-1]);self.assertNotIn('env',argv[-1])
        self.proc.start()


@unittest.skipUnless(os.name == 'posix', 'Identity fixtures use owner-scoped local files.')
class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(os.path.realpath(self.temp.name));self.ident=str(uuid.uuid4())
        self.root=self.home/'.gemini'/'antigravity';self.root.mkdir(parents=True)
        self.relative=Path('brain')/self.ident/'.system_generated'/'logs'/'transcript.jsonl'
        self.transcript=self.root/self.relative
        self.database=self.root/'conversation_summaries.db'
        with contextlib.closing(sqlite3.connect(self.database)) as db,db:
            db.execute('CREATE TABLE conversation_summaries(conversation_id TEXT PRIMARY KEY)')
            db.execute('INSERT INTO conversation_summaries VALUES(?)',(self.ident,))

    def test_primary_canonical_transcript_and_primary_row_required_without_body_read(self):
        self.assertFalse(navigation.existing_app_chat(self.home,self.ident))
        self.transcript.parent.mkdir(parents=True);self.transcript.write_text('fictional private content must never be read')
        with mock.patch.object(Path,'open',side_effect=AssertionError('No transcript body access is allowed.')):
            self.assertTrue(navigation.existing_app_chat(self.home,self.ident))
        with contextlib.closing(sqlite3.connect(self.database)) as db,db:
            db.execute('DELETE FROM conversation_summaries')
        self.assertFalse(navigation.existing_app_chat(self.home,self.ident))
        for invalid in ('a:'+self.ident,self.ident.upper(),True,[],{'uuid':self.ident}):
            self.assertFalse(navigation.existing_app_chat(self.home,invalid))

    def test_other_runtime_duplicate_or_symlink_is_ineligible(self):
        self.transcript.parent.mkdir(parents=True);self.transcript.write_text('')
        for name in ('antigravity-cli','antigravity-ide'):
            other=self.home/'.gemini'/name/self.relative;other.parent.mkdir(parents=True);other.write_text('')
            self.assertFalse(navigation.existing_app_chat(self.home,self.ident))
            other.unlink()
        original=self.transcript.with_name('original.jsonl');self.transcript.rename(original);self.transcript.symlink_to(original)
        self.assertFalse(navigation.existing_app_chat(self.home,self.ident))

    def test_missing_malformed_and_linked_database_never_create_or_expand_identity(self):
        self.transcript.parent.mkdir(parents=True);self.transcript.write_text('')
        original=self.database.with_name('original.db');self.database.rename(original)
        self.assertFalse(navigation.existing_app_chat(self.home,self.ident));self.assertFalse(self.database.exists())
        self.database.write_text('fictional malformed DB')
        self.assertFalse(navigation.existing_app_chat(self.home,self.ident))
        self.database.unlink();self.database.symlink_to(original)
        self.assertFalse(navigation.existing_app_chat(self.home,self.ident))


class UnsupportedTests(unittest.TestCase):
    def test_other_platform_status_and_setup_are_explicit_and_do_no_io(self):
        for platform in ('win32', 'linux'):
            with mock.patch.object(navigation.sys, 'platform', platform), mock.patch.object(navigation, '_parent') as files:
                self.assertEqual(navigation.local_state(Path('/fictional/home'))['state'], 'unsupported')
                for action in (navigation.install, navigation.disconnect, navigation.remove):
                    with self.assertRaises(navigation.NavigationError) as unsupported:
                        action(Path('/fictional/home'), ROOT) if action is navigation.install else action(Path('/fictional/home'))
                    self.assertEqual(unsupported.exception.code, 'unsupported')
                files.assert_not_called()


class GuardMutationTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix', 'Kernel/file fixtures are POSIX-only.')
    def test_native_runtime_and_installer_guards_are_causally_exercised(self):
        global navigation
        original=navigation
        path=ROOT/'src'/'antigravity_navigation.py';source=path.read_text()
        mutations=[
            ("require(listener(connection['broker_pid'], urllib.parse.urlsplit(connection['url']).port), 'changed')",'pass',RuntimeTests,'test_replaced_broker_parent_listener_and_generation_reject_before_transmission'),
            ("    return local_state(home)", "    path = Path(home) / '.gemini' / 'config' / 'config.json'; raw, _ = snapshot(path); replace(path, b'{}', raw)\n    return local_state(home)",InstallTests,'test_install_once_user_scope_preserves_config_and_requires_no_target_list'),
            ("if os.path.lexists(Path(home) / '.gemini' / name / relative):",'if False:',IdentityTests,'test_other_runtime_duplicate_or_symlink_is_ineligible'),
        ]
        for before,after,suite,test in mutations:
            with self.subTest(guard=before):
                self.assertIn(before,source)
                mutant=types.ModuleType('fictional_navigation_mutant');mutant.__file__=str(path)
                exec(compile(source.replace(before,after,1),str(path),'exec'),mutant.__dict__)
                try:
                    navigation=mutant;result=unittest.TestResult();suite(test).run(result)
                finally:navigation=original
                self.assertEqual(result.errors,[],'A mutation must fail a real assertion, not fixture setup.')
                self.assertTrue(result.failures,'Removing the boundary must fail its real regression.')


if __name__ == '__main__':
    unittest.main()
