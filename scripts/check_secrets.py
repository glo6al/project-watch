#!/usr/bin/env python3
"""Refuse to publish a key. Scans everything under the project (tracked or not; .git skipped; node_modules only as staged), and
every blob git has staged (what a commit would actually publish, which can differ from the working copy), for strings
shaped like an Anthropic API key or other common secrets: in file and folder names, in file contents whatever the
size, and in the stored text of links. It exits 1 naming the file if one is found, or if anything could not be
examined (a folder that could not be listed, a file that could not be read, a pipe, socket or device: not clean until
it is).
Run before any commit: python3 scripts/check_secrets.py   (the test suite runs it, and the server runs the same
scan, for Anthropic keys only and with nothing but .git skipped, before it switches the Officer of the Watch on).

What it does not see, so you know: a key inside .git's own objects (committed earlier and since removed from the
working tree: rewrite history for that), and a key that is encoded or split across lines. Links are not followed,
to a file or a folder: git stores the link's text, which is what is scanned."""
import os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHAPES = [('an Anthropic API key', re.compile(rb'sk-ant-[A-Za-z0-9_-]{20,}')),
          ('an OpenAI-style key', re.compile(rb'sk-[A-Za-z0-9]{32,}')),
          ('a GitHub token', re.compile(rb'gh[pousr]_[A-Za-z0-9]{30,}')),
          ('an AWS access key', re.compile(rb'AKIA[0-9A-Z]{16}')),
          ('a private key block', re.compile(rb'-----BEGIN [A-Z ]*PRIVATE KEY-----'))]
ANTHROPIC = SHAPES[:1]
SKIP = ('.git', 'node_modules')     # the pre-publish check: npm's own files hold key-like test strings; what git has staged there is still scanned, from the index
EVERYTHING = ('.git',)              # the server's check before the Officer starts: the whole tree, nothing but .git skipped
INDEX_DEADLINE = 300                # seconds for the whole staged-content read; past it git is killed and the index counts as unread
CHUNK, KEEP = 1 << 20, 512          # files are read a megabyte at a time, whatever their size; the last 512 bytes of
                                    # each piece are kept in front of the next, so a key cut by the piece boundary is still seen


def shape_in(data, shapes):
    for what, rx in shapes:
        if rx.search(data):
            return what
    return ''


def redact(text, shapes=SHAPES):
    """Text with every secret-shaped string replaced, for anything that is printed (a file name can hold a key)."""
    b = str(text).encode('utf-8', 'surrogateescape')
    for what, rx in shapes:
        b = rx.sub(b'[a key]', b)
    return b.decode('utf-8', 'replace')


FLAGS = getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_BINARY', 0)


def git_index(root):
    """What git has staged: [(mode, blob id, path)], or None if root is not inside a git repository, or an error string.
    Read from the index only; no hook, filter or checkout runs."""
    try:
        out = subprocess.run(['git', '--no-replace-objects', '-C', root, 'ls-files', '-z', '--cached', '--stage'], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return 'git could not be run (%s)' % type(e).__name__
    if out.returncode != 0:
        return None if b'not a git repository' in out.stderr else 'git could not list the index'
    rows = []
    for rec in out.stdout.split(b'\0'):
        if rec:
            meta, _, path = rec.partition(b'\t')
            mode, blob = meta.split()[:2]
            rows.append((mode.decode(), blob.decode(), path.decode('utf-8', 'surrogateescape')))
    return rows


def scan_index(root, rows, shapes, hits, unread):
    """Every staged blob, read from git's object store the way a commit would publish it (not the working copy, which
    may differ or be gone): names, link text and contents. A blob that cannot be read counts as unread."""
    want = []
    for mode, blob, path in rows:
        what = shape_in(path.encode('utf-8', 'surrogateescape'), shapes)
        if what:
            hits.append(('staged: ' + path, what + ', in the name'))
        elif mode not in ('160000',):        # a submodule pointer has no blob here
            want.append((blob, path, mode == '120000'))
    if not want:
        return
    try:
        proc = subprocess.Popen(['git', '--no-replace-objects', '-C', root, 'cat-file', '--batch'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)      # the object as stored, never a replacement standing in for it
    except OSError as e:
        unread.append(('the staged files', 'git could not be run (%s)' % type(e).__name__))
        return
    import threading
    def feed():
        try:
            for blob, _, _ in want:
                proc.stdin.write(blob.encode() + b'\n')
            proc.stdin.close()
        except (OSError, ValueError):     # the pipe closed under it at cleanup: expected
            pass

    state = {'done': False}

    def read():
        try:
            read_all()
            state['done'] = True
        except Exception as e:             # any failure reading or parsing git's output: the index is not checked, and that is recorded, not raised
            unread.append(('the staged files', 'git\'s output could not be read (%s)' % type(e).__name__))

    def read_all():
        for blob, path, link in want:
            head = proc.stdout.readline()
            parts = head.split()
            if len(parts) != 3 or parts[0].decode() != blob:
                unread.append(('staged: ' + path, 'git could not produce the staged content'))
                return
            size, tail, found = int(parts[2]), b'', ''
            while size > 0:
                buf = proc.stdout.read(min(CHUNK, size))
                if not buf:
                    break
                size -= len(buf)
                if not found:
                    found = shape_in(tail + buf, shapes)
                    tail = buf[-KEEP:]
            proc.stdout.read(1)        # the newline after the content
            if size > 0:
                unread.append(('staged: ' + path, 'git stopped before the staged content ended'))
                return
            if found:
                hits.append(('staged: ' + path, found + (', in the link text' if link else '')))
        proc.stdout.close()
        rc = proc.wait()
        if rc != 0:
            unread.append(('the staged files', 'git did not finish cleanly reading the staged content'))

    try:
        threading.Thread(target=feed, daemon=True).start()
        th = threading.Thread(target=read, daemon=True)
        th.start()
        th.join(INDEX_DEADLINE)
        if th.is_alive():                  # a stalled git: kill it so the reads end, and say the index was not checked
            proc.kill()
            th.join(10)
            unread.append(('the staged files', 'git stalled, so the staged content was not fully checked'))
        elif not state['done'] and not any(u[0] == 'the staged files' or u[0].startswith('staged: ') for u in unread):
            unread.append(('the staged files', 'the staged content was not fully checked'))      # the reader ended without finishing and without saying why
    finally:
        if proc.poll() is None:            # git first: a feeder blocked on a full pipe holds stdin's lock, and only git's end releases it
            proc.kill()
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        proc.wait()


def scan(root=ROOT, shapes=SHAPES, skip=SKIP):
    """(hits, unread): hits are (relative path, what) for every name, link text or file whose contents hold one of
    the shapes, in the working tree and in what git has staged; unread are (relative path, why) for anything that could
    not be examined, which counts as not clean."""
    import stat as _st
    hits, unread = [], []
    rel = lambda p: os.path.relpath(p, root)
    def one(p):
        """One entry, by name, by link text or by contents, into hits or unread."""
        what = shape_in(rel(p).encode('utf-8', 'surrogateescape'), shapes)
        if what:
            hits.append((rel(p), what + ', in the name'))
            return
        try:
            st = os.lstat(p)
        except OSError as e:
            unread.append((rel(p), type(e).__name__))
            return
        if _st.S_ISLNK(st.st_mode):
            try:
                what = shape_in(os.readlink(p).encode('utf-8', 'surrogateescape'), shapes)
            except OSError as e:
                unread.append((rel(p), type(e).__name__))
                return
            if what:
                hits.append((rel(p), what + ', in the link text'))
            return
        if _st.S_ISDIR(st.st_mode):
            return
        if not _st.S_ISREG(st.st_mode):
            unread.append((rel(p), 'not a plain file (a pipe, socket or device): not scanned'))
            return
        try:
            fd = os.open(p, os.O_RDONLY | FLAGS)
        except OSError as e:
            unread.append((rel(p), type(e).__name__))
            return
        try:
            if not _st.S_ISREG(os.fstat(fd).st_mode):          # the file that was opened, not the one looked at
                unread.append((rel(p), 'not a plain file when opened: not scanned'))
                return
            with os.fdopen(fd, 'rb') as fh:
                fd = -1
                tail = b''
                while True:
                    buf = fh.read(CHUNK)
                    if not buf:
                        break
                    what = shape_in(tail + buf, shapes)
                    if what:
                        hits.append((rel(p), what))
                        break
                    tail = buf[-KEEP:]
        except OSError as e:
            unread.append((rel(p), type(e).__name__))
        finally:
            if fd >= 0:
                os.close(fd)

    index = git_index(root)
    if isinstance(index, str):
        unread.append(('the staged files', index))
        index = []
    if index is None:          # not a repository: nothing is staged, and nothing can vouch for a skipped folder, so none is skipped
        index, skip = [], tuple(d for d in skip if d == '.git')
    for top, dirs, files in os.walk(root, onerror=lambda e: unread.append((rel(e.filename), 'folder could not be listed'))):
        dirs[:] = [d for d in dirs if d not in skip]     # links to folders are listed here but never entered; what git has staged under a skipped folder is scanned from the index below
        for name in dirs + files:
            one(os.path.join(top, name))
    scan_index(root, index, shapes, hits, unread)       # the staged content itself, which is what a commit publishes
    return hits, unread




if __name__ == '__main__':
    hits, unread = scan(sys.argv[1] if len(sys.argv) > 1 else ROOT)
    for path, what in hits:
        print('SECRET  %s  looks like %s' % (redact(path), what))
    for path, why in unread:
        print('UNREAD  %s  could not be read (%s), so it is not known to be clean' % (redact(path), why))
    if hits or unread:
        print('%d file(s) must be cleaned or made readable before anything is committed' % (len(hits) + len(unread)))
    else:
        print('clean: no secret-shaped string in any name, link, file or staged content under the project (node_modules only as staged), and everything could be examined')
    sys.exit(1 if hits or unread else 0)
