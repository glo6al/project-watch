"""Read-only, pinned native Codex rollout identity. No native queue operations."""
import json
import os
import re
import sqlite3
import stat
from urllib.parse import quote

MAX_HEADER = 1_000_000
MAX_ENTRIES = 20000
UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
NAME = re.compile(r'^(?:rollout-.*?)(?P<id>' + UUID + r')(?:_' + UUID + r')?\.jsonl$')


class Unavailable(ValueError):
    pass


def filename_id(name):
    match = NAME.fullmatch(name)
    if match:
        return match['id']
    tail = name[-42:-6] if name.endswith('.jsonl') else ''
    return tail if re.fullmatch(UUID, tail) else None


def pure_text(content, role):
    """Return complete native text, or None when a part is not understood.

    Missing type is retained for the older text-only record shape. Native output
    annotations/logprobs are output metadata; user input has no such exceptions.
    Unknown parts and fields must not disappear from a peer envelope comparison.
    """
    assistant = role == 'assistant'
    kinds = (None, 'text', 'output_text') if assistant else (None, 'text', 'input_text')
    keys = {'type', 'text', 'annotations', 'logprobs'} if assistant else {'type', 'text'}
    if not isinstance(content, list) or any(
            not isinstance(part, dict) or not isinstance(part.get('text'), str)
            or part.get('type') not in kinds or set(part) - keys
            or 'annotations' in part and not isinstance(part['annotations'], list)
            or 'logprobs' in part and part['logprobs'] is not None and not isinstance(part['logprobs'], list)
            for part in content):
        return None
    return ''.join(part['text'] for part in content)


def open_regular(root, path):
    """Validate every component and keep the descriptor used to read evidence."""
    root, path = os.path.abspath(root), os.path.abspath(path)
    if os.path.commonpath((root, path)) != root:
        raise Unavailable('Codex transcript is outside its configured root.')
    parent = os.open(os.path.sep, os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = path.split(os.path.sep)
        for part in parts[1:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = nxt
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            os.close(fd)
            raise Unavailable('Codex transcript is not a plain regular file.')
        return os.fdopen(fd, 'rb'), st
    finally:
        os.close(parent)


def header(source, ident, project=None):
    source.seek(0)
    raw = source.readline(MAX_HEADER + 1)
    try:
        record = json.loads(raw)
        meta = record['payload']
        if (not raw.endswith(b'\n') or len(raw) > MAX_HEADER or record.get('type') != 'session_meta'
                or not isinstance(meta, dict) or meta.get('id') != ident
                or meta.get('session_id', ident) != ident
                or meta.get('thread_source') == 'guardian_review'
                or meta.get('originator') == 'codex_exec'
                or not isinstance(meta.get('source', ''), str)
                or meta.get('source') in ('exec', 'review', 'subagent')
                or meta.get('parent_thread_id') or meta.get('agent_path') not in (None, '', '/root')
                or meta.get('is_subagent') or meta.get('is_archived')
                or project is not None and meta.get('cwd') != project):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise Unavailable('Codex native transcript identity is unavailable or changed.') from None
    return meta


def index(root, ident=None, limit=MAX_ENTRIES):
    """None means absent; invalid/present metadata never enables legacy fallback.

    A bounded read-only SQLite snapshot is checked against the opened file identity.
    Native source permissions/identities are still independently checked at the log.
    """
    path = os.path.join(root, 'state_5.sqlite')
    if not os.path.lexists(path):
        return None
    try:
        source, original = open_regular(root, path)
        with source:
            db = sqlite3.connect('file:' + quote(path) + '?mode=ro', uri=True, timeout=0.2)
            try:
                db.execute('PRAGMA query_only=ON')
                columns = {row[1] for row in db.execute('PRAGMA table_info(threads)')}
                required = {'id', 'rollout_path', 'cwd', 'archived', 'source'}
                if not required <= columns:
                    raise Unavailable('Codex native index schema is unsupported.')
                extra = [c for c in ('agent_path', 'thread_source', 'originator', 'attached_thread_parent_id') if c in columns]
                fields = ['id', 'rollout_path', 'cwd', 'archived', 'source'] + extra
                query = 'SELECT ' + ','.join(fields) + ' FROM threads'
                params = []
                if ident is not None:
                    query += ' WHERE id=?'; params.append(ident)
                query += ' LIMIT ?'; params.append(limit + 1)
                rows = db.execute(query, params).fetchall()
                if len(rows) > limit:
                    raise Unavailable('Codex native index exceeds its safe lookup bound.')
            finally:
                db.close()
            check, current = open_regular(root, path)
            check.close()
            if (original.st_dev, original.st_ino) != (current.st_dev, current.st_ino):
                raise Unavailable('Codex native index changed during lookup.')
        return {row[0]: dict(zip(fields, row)) for row in rows}
    except (OSError, sqlite3.Error) as error:
        raise Unavailable('Codex native index cannot be read safely.') from error


def selected(entry):
    if (not isinstance(entry, dict) or entry.get('archived') != 0
            or entry.get('source') not in ('vscode', 'cli', 'tui', 'appServer')
            or entry.get('agent_path') not in (None, '', '/root')
            or entry.get('thread_source') in ('guardian_review', 'subagent')
            or entry.get('originator') == 'codex_exec' or entry.get('attached_thread_parent_id')
            or not isinstance(entry.get('rollout_path'), str) or not os.path.isabs(entry['rollout_path'])
            or not isinstance(entry.get('cwd'), str) or not os.path.isabs(entry['cwd'])):
        raise Unavailable('Codex native index does not identify an eligible root chat.')
    return entry['rollout_path']


def candidates(root, ident):
    result, pending, visited = [], [(root, 0)], 0
    while pending:
        folder, depth = pending.pop()
        with os.scandir(folder) as entries:
            for entry in entries:
                visited += 1
                if visited > MAX_ENTRIES:
                    raise Unavailable('Codex transcript inventory exceeds its safe bound.')
                if entry.is_symlink():
                    if filename_id(entry.name) == ident:
                        raise Unavailable('Codex transcript candidate is a symlink.')
                    continue
                if entry.is_dir(follow_symlinks=False) and depth < 3:
                    pending.append((entry.path, depth + 1))
                elif filename_id(entry.name) == ident:
                    result.append(entry.path)
    return result


def resolve(sessions, ident, project=None):
    entries = index(os.path.dirname(sessions), ident)
    if entries is not None:
        entry = entries.get(ident)
        path = selected(entry)
        if project is not None and entry['cwd'] != project:
            raise Unavailable('Codex native project changed.')
        project = entry['cwd']
    else:
        found = candidates(sessions, ident)
        if len(found) != 1:
            raise Unavailable('The chat must have exactly one verified transcript without a native index.')
        path = found[0]
    source, st = open_regular(sessions, path)
    try:
        meta = header(source, ident, project)
    except BaseException:
        source.close()
        raise
    return path, source, st, meta


def open_pinned(sessions, path, ident, project=None):
    """Fresh pinned lookup without enumerating the session inventory."""
    entries = index(os.path.dirname(sessions), ident)
    if entries is not None:
        entry = entries.get(ident)
        if selected(entry) != path or project is not None and entry['cwd'] != project:
            raise Unavailable('The active Codex transcript identity changed.')
        project = entry['cwd']
    source, st = open_regular(sessions, path)
    try:
        meta = header(source, ident, project)
        if entries != index(os.path.dirname(sessions), ident):
            raise Unavailable('Codex native index changed during identity validation.')
        return path, source, st, meta
    except BaseException:
        source.close()
        raise


def segment(row):
    """Cursor identity is separate from immutable original delivery identity."""
    return row.get('codex_tracking') or row


def segment_key(row):
    current = segment(row)
    return current.get('path'), current.get('device'), current.get('inode')


def tracking_source(sessions, row, budget):
    """Advance a proven single-hop continuation, with bounded prefix validation.

    history_base is a complete-record prefix (exclusive ordinal, byte boundary).
    We support a root predecessor only. No filename age or UUID alone establishes
    a chain. Original target pins remain untouched; unknown chains fail closed.
    """
    current = segment(row)
    ident = row['agent'][2:]
    source, st = open_regular(sessions, current['path'])
    successor = None
    try:
        meta = header(source, ident)
        if (st.st_dev, st.st_ino) != (current['device'], current['inode']) or st.st_size < current['cursor']:
            raise Unavailable('Pinned Codex tracking segment changed; recovery is needed.')
        entries = index(os.path.dirname(sessions), ident)
        if entries is None:
            return source, st, current
        path, successor, fresh, head = resolve(sessions, ident, meta.get('cwd'))
        if path == current['path']:
            if (fresh.st_dev, fresh.st_ino) != (st.st_dev, st.st_ino):
                raise Unavailable('Codex active segment changed while it was read.')
            successor.close()
            return source, st, current
        base = head.get('history_base')
        if (row.get('codex_tracking') or 'history_base' in meta or not isinstance(base, dict)
                or base.get('thread_id') != ident
                or type(base.get('end_byte_offset')) is not int or type(base.get('end_ordinal_exclusive')) is not int
                or not 0 < base['end_byte_offset'] <= st.st_size or base['end_ordinal_exclusive'] < 1
                or meta.get('cwd') != head.get('cwd')
                or set(candidates(sessions, ident)) != {current['path'], path}):
            raise Unavailable('Codex continuation is ambiguous or requires multiple segments; recovery is needed.')
        signature = [path, fresh.st_dev, fresh.st_ino, st.st_size,
                     base['end_byte_offset'], base['end_ordinal_exclusive']]
        proof = row.get('codex_continuation')
        if proof is None:
            proof = dict(signature=signature, offset=0, ordinal=0)
            row['codex_continuation'] = proof
        if proof.get('signature') != signature:
            raise Unavailable('Codex continuation evidence changed; recovery is needed.')
        boundary = base['end_byte_offset']
        if proof['offset'] >= boundary:
            source.seek(boundary - 1)
            if (proof['offset'] != boundary or proof['ordinal'] != base['end_ordinal_exclusive']
                    or source.read(1) != b'\n'):
                raise Unavailable('Codex predecessor byte and ordinal boundary do not agree.')
        if proof['offset'] < boundary:
            source.seek(proof['offset'])
            data = source.read(min(budget, boundary - proof['offset']))
            if not data:
                raise Unavailable('Codex predecessor prefix is incomplete.')
            proof['offset'] += len(data)
            proof['ordinal'] += data.count(b'\n')
            if proof['offset'] == boundary and (not data.endswith(b'\n') or proof['ordinal'] != base['end_ordinal_exclusive']):
                raise Unavailable('Codex predecessor byte and ordinal boundary do not agree.')
            source.close()
            successor.close()
            return None
        # Drain all unread predecessor bytes, including an aborted post-base tail.
        # Extra tail evidence may withhold a reply, but must never be discarded.
        if current['cursor'] < st.st_size:
            successor.close()
            return source, st, current
        if row.get('codex_discard_record'):
            raise Unavailable('Codex predecessor ends inside an oversized record; recovery is needed.')
        if st.st_size > boundary and row.get('origin') == 'peer':
            row['peer_mixed_input'] = True
        check = index(os.path.dirname(sessions), ident)
        if check is None or selected(check.get(ident)) != path:
            raise Unavailable('Codex native pointer changed during continuation proof.')
        again, check_stat = open_regular(sessions, current['path'])
        again.close()
        if (check_stat.st_dev, check_stat.st_ino, check_stat.st_size) != (st.st_dev, st.st_ino, st.st_size):
            raise Unavailable('Codex predecessor changed before the tracking switch.')
        tracking = dict(path=path, device=fresh.st_dev, inode=fresh.st_ino, cursor=0)
        row['codex_tracking'] = tracking
        # Keep the original cursor, pin, and completed proof as audit evidence.
        source.close()
        return successor, fresh, tracking
    except BaseException:
        source.close()
        if successor is not None:
            successor.close()
        raise
