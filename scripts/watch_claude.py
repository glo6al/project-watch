#!/usr/bin/env python3
"""Start a new interactive Claude Code session connected to WATCH. No global settings edits."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parent.parent


def parser():
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument('--name', help='Display name for the new Claude session')
    p.add_argument('--claude', help='Claude executable path (defaults to ~/.local/bin/claude, then PATH)')
    p.add_argument('--node', help='Node.js executable path (defaults to PATH)')
    return p


def executable(value, preferred=None, fallback=None):
    candidate = value or (str(preferred) if preferred and preferred.is_file() else shutil.which(fallback or ''))
    if not candidate:
        raise ValueError('Required executable is missing: ' + (fallback or 'runtime'))
    candidate = shutil.which(candidate) if not os.path.isabs(candidate) else candidate
    if not candidate or not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
        raise ValueError('The selected executable is unavailable.')
    return os.path.abspath(candidate)


def ensure_private(path, create=True):
    path = Path(os.path.abspath(path))
    for part in reversed((path,) + tuple(path.parents)):
        if part.exists() or part.is_symlink():
            if part.is_symlink() or not part.is_dir():
                raise ValueError('Channel state must use directories without symbolic links.')
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Channel state directories must be private and owned by you.')
    return path


def command(claude, node, channel, state, session_id, instance_id, name=None):
    # No free-form passthrough: resume/continue/session/MCP overrides cannot change this binding.
    config = {'mcpServers': {'watch': {
        'command': node, 'args': [str(channel)],
        'env': {'WATCH_CLAUDE_SESSION_ID': session_id, 'WATCH_CLAUDE_INSTANCE_ID': instance_id,
                'WATCH_CLAUDE_STATE_DIR': str(state)},
    }}}
    args = [claude, '--session-id', session_id, '--mcp-config', json.dumps(config),
            '--dangerously-load-development-channels', 'server:watch']
    if name:
        if len(name) > 120 or not name.strip() or any(ord(c) < 32 for c in name):
            raise ValueError('Use a session name of one to 120 printable characters.')
        args += ['--name', name]
    return args


def check_runtime(claude, node):
    version = subprocess.run([claude, '--version'], capture_output=True, text=True, timeout=10, check=True).stdout
    match = re.search(r'(\d+)\.(\d+)\.(\d+)', version)
    if not match or tuple(map(int, match.groups())) < (2, 1, 310):
        raise ValueError('Use Claude Code 2.1.310 or newer; this is the version tested with WATCH channels.')
    help_text = subprocess.run([claude, '--help'], capture_output=True, text=True, timeout=10, check=True).stdout
    if not all(flag in help_text for flag in ('--session-id', '--mcp-config', '--name')):
        raise ValueError('This Claude executable does not support the required session options.')
    node_version = subprocess.run([node, '--version'], capture_output=True, text=True, timeout=5, check=True).stdout
    if not re.match(r'v(2[0-9]|[3-9][0-9])\.', node_version):
        raise ValueError('Use Node.js 20 or newer for the optional WATCH channel.')


def main(argv=None):
    args = parser().parse_args(argv)
    if sys.platform != 'darwin':
        raise ValueError('This first WATCH Claude launcher supports macOS only.')
    if not sys.stdin.isatty():
        raise ValueError('Run this launcher in an interactive terminal; Claude requires channel development consent.')
    claude = executable(args.claude, Path.home() / '.local/bin/claude', 'claude')
    node = executable(args.node, fallback='node')
    channel = ROOT / 'integrations/claude/channel.mjs'
    if not (channel.parent / 'node_modules/@modelcontextprotocol/sdk/package.json').is_file():
        raise ValueError('Install the optional connector first: npm --prefix integrations/claude ci --ignore-scripts --no-audit --no-fund')
    check_runtime(claude, node)
    session_id, instance_id = str(uuid.uuid4()), str(uuid.uuid4())
    base = ensure_private(Path.home() / '.live-room')
    channels = ensure_private(base / 'claude-channels')
    state = channels / session_id
    # Validate options before creating even the session directory.
    cmd = command(claude, node, channel, state, session_id, instance_id, args.name)
    state.mkdir(mode=0o700)  # Exclusive: no existing session is claimed or resumed.
    print('Starting a new WATCH-connected Claude session. Accept Claude’s local development channel notice to enable it.', flush=True)
    print('Existing tools, permissions, model settings and project instructions remain in effect.', flush=True)
    os.execv(claude, cmd)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print('WATCH Claude launcher: ' + str(error), file=sys.stderr)
        sys.exit(1)
