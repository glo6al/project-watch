"""Read-only discovery and bounded metadata for Antigravity's local transcripts.

This module never opens a transcript body. The server's normal bounded tailer
reads those records after discovery. Metadata is optional and grants no actions.
"""
import json
import os
import re
import sqlite3
import stat
import time
import urllib.parse
from pathlib import Path


UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')
TRANSCRIPT = ('.system_generated', 'logs', 'transcript.jsonl')
ROOT_NAMES = ('antigravity', 'antigravity-cli', 'antigravity-ide')


def roots(home):
    return tuple(os.path.join(home, '.gemini', name) for name in ROOT_NAMES)


def identity(path, home):
    """Accept only one direct UUID transcript below a fixed native source root."""
    absolute = os.path.abspath(os.fspath(path))
    for root in roots(home):
        brain = os.path.abspath(os.path.join(root, 'brain'))
        try:
            relative = os.path.relpath(absolute, brain)
        except ValueError:
            continue
        parts = relative.split(os.sep)
        if len(parts) != 4 or not UUID.fullmatch(parts[0]) or tuple(parts[1:]) != TRANSCRIPT:
            continue
        if os.path.realpath(absolute) != absolute or os.path.realpath(brain) != brain:
            return None
        try:
            info = os.stat(absolute)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return None
        except OSError:
            return None
        return parts[0], root
    return None


def inventory(home):
    """Duplicate native UUIDs are ambiguous: exclude every copy, never pick one."""
    candidates = {}
    for root in roots(home):
        try:
            with os.scandir(os.path.join(root, 'brain')) as items:
                for index, item in enumerate(items):
                    if index >= 10000:
                        break
                    if not UUID.fullmatch(item.name) or not item.is_dir(follow_symlinks=False):
                        continue
                    path = os.path.join(item.path, *TRANSCRIPT)
                    proof = identity(path, home)
                    if proof:
                        candidates.setdefault(proof[0], []).append(path)
        except OSError:
            continue
    return [paths[0] for paths in candidates.values() if len(paths) == 1]


def _plain(value, cap):
    return value.strip() if isinstance(value, str) and len(value) <= cap and not any(ord(c) < 32 for c in value) else ''


def _workspace(value):
    if not isinstance(value, str) or len(value) > 8192:
        return ''
    try:
        uris = json.loads(value)
        if not isinstance(uris, list) or len(uris) != 1 or not isinstance(uris[0], str):
            return ''
        uri = urllib.parse.urlsplit(uris[0])
        if uri.scheme != 'file' or uri.netloc not in ('', 'localhost') or uri.query or uri.fragment:
            return ''
        path = urllib.parse.unquote(uri.path, errors='strict')
        if path.startswith('//') or '\\' in path:
            return ''
        if os.name == 'nt' and re.match(r'^/[A-Za-z]:/', path):
            path = path[1:]
        return path if _plain(path, 4096) and os.path.isabs(path) else ''
    except (ValueError, UnicodeError):
        return ''


def metadata(path, home):
    """Read only allowlisted scalar columns for this exact native UUID.

    Read-only WAL access needs existing shared memory. Refuse a missing sidecar
    instead of letting SQLite create one in the application's storage directory.
    Errors and optional/unknown schemas yield no metadata, never raw diagnostics.
    """
    proof = identity(path, home)
    if not proof:
        return {}
    ident, root = proof
    database = os.path.join(root, 'conversation_summaries.db')
    conn = None
    try:
        for candidate in (database, database + '-wal', database + '-shm'):
            if candidate != database and not os.path.lexists(candidate):
                continue
            info = os.lstat(candidate)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or os.path.realpath(candidate) != os.path.abspath(candidate):
                return {}
        if os.path.exists(database + '-wal') and not os.path.exists(database + '-shm'):
            return {}
        conn = sqlite3.connect(Path(database).as_uri() + '?mode=ro', uri=True, timeout=.05)
        conn.execute('PRAGMA query_only=ON')
        deadline = time.monotonic() + .1
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        rows = conn.execute('SELECT substr(title,1,201), substr(workspace_uris,1,8193), '
                            'substr(status,1,129), substr(agent_name,1,121), '
                            'substr(parent_conversation_id,1,65), not_fully_idle '
                            'FROM conversation_summaries WHERE conversation_id=? LIMIT 2', (ident,)).fetchall()
        if len(rows) != 1:
            return {}
        title, workspaces, status, agent, parent, busy = rows[0]
        out = {}
        name = _plain(title, 200) or _plain(agent, 120)
        if name:
            out['name'] = name
        cwd = _workspace(workspaces)
        if cwd:
            out['cwd'] = cwd
        if isinstance(parent, str) and UUID.fullmatch(parent) and parent != ident:
            out['parent'] = 'a:' + parent
        # This is native activity metadata, not a tool-result/success signal.
        if type(busy) is int:
            if status == 'CASCADE_RUN_STATUS_IDLE' and busy == 0:
                out['idle'] = True
            elif status == 'CASCADE_RUN_STATUS_RUNNING' and busy == 1:
                out['idle'] = False
        return out
    except (OSError, sqlite3.Error, ValueError):
        return {}
    finally:
        if conn is not None:
            conn.close()
