#!/usr/bin/env python3
"""Launcher checks without starting Claude or changing user settings."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('watch_claude', ROOT / 'scripts/watch_claude.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


class LauncherTests(unittest.TestCase):
    def test_only_supported_flags(self):
        for flag in ('--resume', '--continue', '--fork-session', '--session-id', '--mcp-config', '--print', '--dangerously-skip-permissions', '--strict-mcp-config', '--sess'):
            with self.subTest(flag=flag), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                m.parser().parse_args([flag, 'anything'])

    def test_same_identity_without_permission_or_tool_overrides(self):
        args = m.command('/claude', '/node', '/channel.mjs', '/state', 'session', 'instance', 'Atlas')
        self.assertEqual(args[args.index('--session-id') + 1], 'session')
        config = json.loads(args[args.index('--mcp-config') + 1])['mcpServers']['watch']
        self.assertEqual(config['env']['WATCH_CLAUDE_SESSION_ID'], 'session')
        self.assertEqual(config['env']['WATCH_CLAUDE_INSTANCE_ID'], 'instance')
        self.assertEqual(config['env']['WATCH_CLAUDE_STATE_DIR'], '/state')
        self.assertEqual(config['command'], '/node')
        self.assertEqual(config['args'], ['/channel.mjs'])
        self.assertEqual(args[-2:], ['--name', 'Atlas'])
        for forbidden in ('--tools', '--allowedTools', '--strict-mcp-config', '--setting-sources', '--permission-mode', '--dangerously-skip-permissions', '--model', '--effort'):
            self.assertNotIn(forbidden, args)
        self.assertEqual(args[args.index('--dangerously-load-development-channels') + 1], 'server:watch')

    def test_removed_peer_refuses_before_runtime_checks_or_state_writes(self):
        with patch.object(m, 'executable') as executable, patch.object(m, 'ensure_private') as private, patch.object(m.os, 'execv') as execv:
            with self.assertRaisesRegex(ValueError, 'shared WATCH MCP separately'):
                m.main(['--peer', 'Example'])
            executable.assert_not_called()
            private.assert_not_called()
            execv.assert_not_called()
        args = m.command('/claude', '/node', '/channel.mjs', '/state', 'session', 'instance')
        self.assertEqual(set(json.loads(args[args.index('--mcp-config') + 1])['mcpServers']), {'watch'})

    @unittest.skipIf(os.name == 'nt', 'POSIX launcher state')
    def test_launcher_notice_discloses_current_conversation_switching(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            output = io.StringIO()
            with patch.object(m.sys, 'platform', 'darwin'), patch.object(m.sys.stdin, 'isatty', return_value=True), \
                    patch.object(m, 'executable', side_effect=['/fixture/claude', '/fixture/node']), \
                    patch.object(m, 'check_runtime'), patch.object(m.Path, 'home', return_value=home), \
                    patch.object(m.Path, 'is_file', return_value=True), patch.object(m.os, 'execv') as execute, \
                    contextlib.redirect_stdout(output):
                m.main([])
            notice = output.getvalue()
            for text in ('/clear', '/resume', 'fork', 'conversation currently open', 'close and relaunch', 'Shared peer receiving is unavailable'):
                self.assertIn(text, notice)
            self.assertNotIn('Existing tools, permissions, model settings and project instructions remain in effect', notice)
            self.assertIn('does not preserve a prior session', notice)
            execute.assert_called_once()

    def test_bad_name(self):
        for name in ('\nname', ' ' * 3, 'a' * 121):
            with self.assertRaises(ValueError):
                m.command('/claude', '/node', '/c', '/s', 's', 'i', name)

    @unittest.skipIf(os.name == 'nt', 'POSIX permissions')
    def test_private_paths_and_links(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder).resolve()
            private = m.ensure_private(base / 'private')
            self.assertEqual(private.stat().st_mode & 0o777, 0o700)
            (base / 'link').symlink_to(private)
            with self.assertRaises(ValueError): m.ensure_private(base / 'link')
            private.chmod(0o755)
            with self.assertRaises(ValueError): m.ensure_private(private)

    def test_runtime_flags_and_version(self):
        responses = [Mock(stdout='2.1.310 (Claude Code)'), Mock(stdout='--session-id --mcp-config --name'), Mock(stdout='v22.1.0')]
        with patch.object(m.subprocess, 'run', side_effect=responses) as run:
            m.check_runtime('/claude', '/node')
            self.assertEqual(run.call_count, 3)
        with patch.object(m.subprocess, 'run', return_value=Mock(stdout='2.1.138')):
            with self.assertRaises(ValueError): m.check_runtime('/claude', '/node')


if __name__ == '__main__': unittest.main()
