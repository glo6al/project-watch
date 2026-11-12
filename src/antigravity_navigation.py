"""Local, owner-authorized navigation helpers; no agent or model capabilities.

Private runtime descriptors never belong in events, model context or API replies.
Importing this module performs no I/O. Native observations use public process and
bundle metadata, never arguments, environments or conversation contents.
"""
import contextlib
import functools
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.parse
import uuid

ID = 'watch-navigation'
PROTOCOL = 1
VERSION = '2.31.2'
ASSETS = ('broker.py', 'connector.js', 'index.html', 'sidecar.json', 'README.md')
LIMIT = 1024 * 1024
MESSAGES = {
    'unsupported': 'Native Antigravity opening currently supports macOS only.',
    'unsupported_version': 'This Antigravity version does not support WATCH Navigation.',
    'setup_needed': 'Connect Antigravity to enable opening its existing chats.',
    'disconnected': 'Antigravity navigation is disconnected.',
    'waiting_app': 'Open the supported Antigravity desktop app, then try again.',
    'waiting_panel': 'Enable WATCH Navigation in Antigravity Customizations, then open it from Extensions in any existing chat.',
    'unavailable': 'Antigravity navigation is unavailable. Reconnect it before trying again.',
    'changed': 'Navigation setup changed. Refresh its connection before trying again.',
    'busy': 'Turn off WATCH Navigation in Antigravity before changing or removing its files.',
    'collision': 'Existing navigation files were changed or belong to another installation.',
}


class NavigationError(Exception):
    def __init__(self, code='unavailable', message=None):
        self.code = code if code in MESSAGES else 'unavailable'
        # Callers cannot accidentally expose a path, command or credential.
        super().__init__(MESSAGES[self.code])


def require(value, code='unavailable'):
    if not value:
        raise NavigationError(code)


def safe_errors(function):
    @functools.wraps(function)
    def run(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except NavigationError:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            raise NavigationError() from None
    return run


def canonical_uuid(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def exact(value, keys):
    return type(value) is dict and set(value) == set(keys)


def _json(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            require(key not in out)
            out[key] = value
        return out
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                          parse_constant=lambda _: require(False))
    except (ValueError, UnicodeError, TypeError):
        raise NavigationError() from None


def encoded(value):
    return (json.dumps(value, indent=2, ensure_ascii=True) + '\n').encode()


def private_dir(home):
    return Path(home) / '.live-room' / 'antigravity-open'


def sidecar_dir(home):
    return Path(home) / '.gemini' / 'config' / 'sidecars' / ID


def _parent(path, create=False):
    """Walk parent directories without following links; retain a directory fd."""
    require(os.name == 'posix', 'unsupported')
    absolute = os.path.abspath(os.fspath(path))
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.split(os.sep)[1:-1]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        directory = os.fstat(fd)
        require(directory.st_uid == os.getuid() and not directory.st_mode & 0o022)
        return fd, os.path.basename(absolute)
    except BaseException:
        os.close(fd)
        raise


def ensure_private(home):
    for path in (Path(home) / '.live-room', private_dir(home)):
        fd, _ = _parent(path / 'placeholder', True)
        try:
            info = os.fstat(fd)
            require(stat.S_IMODE(info.st_mode) == 0o700)
        finally:
            os.close(fd)


def snapshot(path, private=False, limit=LIMIT):
    fd, name = _parent(path)
    try:
        try:
            file = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            return None, 0o600
        try:
            meta = os.fstat(file)
            require(stat.S_ISREG(meta.st_mode) and meta.st_uid == os.getuid()
                    and meta.st_nlink == 1 and meta.st_size <= limit and not meta.st_mode & 0o022)
            if private:
                require(not meta.st_mode & 0o077)
            raw = os.read(file, limit + 1)
            require(len(raw) <= limit)
            return raw, stat.S_IMODE(meta.st_mode)
        finally:
            os.close(file)
    finally:
        os.close(fd)


def replace(path, raw, expected, mode=0o600):
    fd, name = _parent(path, True)
    temporary = '.watch-navigation-' + secrets.token_hex(12)
    try:
        require(snapshot(path)[0] == expected, 'changed')
        out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd)
        try:
            os.fchmod(out, mode)
            view = memoryview(raw)
            while view:
                written = os.write(out, view)
                require(written > 0)
                view = view[written:]
            os.fsync(out)
        finally:
            os.close(out)
        require(snapshot(path)[0] == expected, 'changed')
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass
        os.close(fd)


def unlink(path, expected):
    fd, name = _parent(path)
    try:
        require(snapshot(path)[0] == expected, 'changed')
        os.unlink(name, dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(fd)


def private_json(home, name):
    path = private_dir(home) / name
    ensure = path.parent
    fd, _ = _parent(ensure / 'placeholder')
    try:
        require(stat.S_IMODE(os.fstat(fd).st_mode) == 0o700)
    finally:
        os.close(fd)
    raw, _ = snapshot(path, private=True)
    if raw is None:
        raise FileNotFoundError()
    return _json(raw)


def authorized(home):
    try:
        grant = private_json(home, 'authorization.json')
        return (exact(grant, ('protocol', 'enabled')) and type(grant['protocol']) is int
                and grant['protocol'] == 1 and grant['enabled'] is True)
    except (OSError, NavigationError):
        return False


@contextlib.contextmanager
def process_lock(home, name='broker.lock'):
    import fcntl
    ensure_private(home)
    path = private_dir(home) / name
    fd, entry = _parent(path)
    lock = None
    try:
        lock = os.open(entry, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=fd)
        info = os.fstat(lock)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and info.st_nlink == 1 and not info.st_mode & 0o077)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise NavigationError('busy') from None
        yield
    finally:
        if lock is not None:
            os.close(lock)
        os.close(fd)


def _regular(path):
    try:
        info = os.lstat(path)
        return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                and os.path.abspath(path) == os.path.realpath(path))
    except OSError:
        return False


def existing_app_chat(home, ident):
    """Identity and scalar primary DB membership only; never open transcript text."""
    if not canonical_uuid(ident):
        return False
    root = Path(home) / '.gemini' / 'antigravity'
    relative = Path('brain') / ident / '.system_generated' / 'logs' / 'transcript.jsonl'
    primary = root / relative
    if not _regular(primary):
        return False
    for name in ('antigravity-cli', 'antigravity-ide'):
        if os.path.lexists(Path(home) / '.gemini' / name / relative):
            return False
    database = root / 'conversation_summaries.db'
    connection = None
    try:
        if not _regular(database):
            return False
        for suffix in ('-wal', '-shm'):
            path = Path(str(database) + suffix)
            if os.path.lexists(path) and not _regular(path):
                return False
        if Path(str(database) + '-wal').exists() and not Path(str(database) + '-shm').exists():
            return False
        connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=.05)
        connection.execute('PRAGMA query_only=ON')
        deadline = time.monotonic() + .1
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        return connection.execute('SELECT conversation_id FROM conversation_summaries '
                                  'WHERE conversation_id=? LIMIT 2', (ident,)).fetchall() == [(ident,)]
    except (OSError, ValueError, sqlite3.Error):
        return False
    finally:
        if connection is not None:
            connection.close()


def _run(args):
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=2, check=False, env={'LC_ALL': 'C', 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'})
        require(result.returncode == 0 and len(result.stdout) <= 65536, 'waiting_app')
        return result.stdout.decode('utf-8', 'strict')
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise NavigationError('waiting_app') from None


def process(pid):
    require(type(pid) is int and 1 <= pid <= 2147483647, 'waiting_app')
    # comm is executable identity, not command arguments or environment.
    line = _run(['/bin/ps', '-ww', '-p', str(pid), '-o', 'pid=,ppid=,uid=,lstart=,comm=']).strip().split(None, 8)
    require(len(line) == 9 and all(v.isdigit() for v in line[:3]), 'waiting_app')
    require(int(line[0]) == pid and int(line[2]) == os.getuid(), 'waiting_app')
    return dict(pid=pid, ppid=int(line[1]), start=' '.join(line[3:8]), executable=line[8])


def listener(pid, port):
    require(type(port) is int and 1 <= port <= 65535)
    try:
        lines = _run(['/usr/sbin/lsof', '-nP', '-a', '-p', str(pid), '-iTCP:' + str(port),
                      '-sTCP:LISTEN', '-Fpn']).splitlines()
        return ('p' + str(pid) in lines and 'n127.0.0.1:' + str(port) in lines
                and all(not line.startswith('n') or line == 'n127.0.0.1:' + str(port) for line in lines))
    except NavigationError:
        return False


def _listener_ports(pid):
    lines = _run(['/usr/sbin/lsof', '-nP', '-a', '-p', str(pid), '-iTCP',
                  '-sTCP:LISTEN', '-Fpn']).splitlines()
    require('p' + str(pid) in lines, 'waiting_app')
    ports = set()
    for line in lines:
        match = re.fullmatch(r'n127\.0\.0\.1:([1-9][0-9]{0,4})', line)
        if match and int(match.group(1)) <= 65535:
            ports.add(int(match.group(1)))
    return ports


def _origin(value):
    if type(value) is not str:
        return None
    match = re.fullmatch(r'https://127\.0\.0\.1:([1-9][0-9]{0,4})', value)
    return int(match.group(1)) if match and int(match.group(1)) <= 65535 else None


def _bundle(executable):
    path = Path(executable)
    require(path.name == 'language_server' and path.parent.name == 'bin', 'waiting_app')
    bundle = path.parent.parent.parent.parent
    require(bundle.suffix == '.app' and os.path.realpath(bundle) == str(bundle), 'waiting_app')
    require(path == bundle / 'Contents' / 'Resources' / 'bin' / 'language_server', 'waiting_app')
    try:
        info = bundle / 'Contents' / 'Info.plist'
        require(info.is_file() and not info.is_symlink() and info.stat().st_size <= LIMIT, 'waiting_app')
        data = plistlib.loads(info.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        raise NavigationError('waiting_app') from None
    require(data.get('CFBundleIdentifier') == 'com.google.antigravity'
            and data.get('CFBundleExecutable') == 'Antigravity', 'waiting_app')
    require(data.get('CFBundleShortVersionString') == VERSION, 'unsupported_version')
    return str(bundle)


@safe_errors
def runtime_for_backend(backend_pid, home):
    require(sys.platform == 'darwin', 'unsupported')
    backend = process(backend_pid)
    bundle = _bundle(backend['executable'])
    app = process(backend['ppid'])
    require(app['executable'] == str(Path(bundle) / 'Contents' / 'MacOS' / 'Antigravity'), 'waiting_app')
    try:
        path = Path(home) / 'Library' / 'Logs' / 'Antigravity' / 'main.log'
        require(_regular(path), 'waiting_app')
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - 131072))
            raw = stream.read(131072)
        candidates = set(re.findall(rb'Local:\s*(https://127\.0\.0\.1:[1-9][0-9]{0,4})/', raw))
        require(len(candidates) <= 32, 'waiting_app')
        ports = _listener_ports(backend_pid)
        origins = [value.decode('ascii') for value in candidates
                   if _origin(value.decode('ascii')) in ports]
        require(len(origins) == 1, 'waiting_app')
    except OSError:
        raise NavigationError('waiting_app') from None
    return dict(backend_pid=backend_pid, backend_start=backend['start'], app_pid=app['pid'],
                app_start=app['start'], bundle=bundle, version=VERSION, origin=origins[0])


@safe_errors
def verify_runtime(runtime, home):
    require(exact(runtime, ('backend_pid', 'backend_start', 'app_pid', 'app_start', 'bundle', 'version', 'origin')))
    require(runtime_for_backend(runtime['backend_pid'], home) == runtime, 'waiting_app')
    return True


@safe_errors
def foreground(runtime, home):
    require(authorized(home), 'disconnected')
    verify_runtime(runtime, home)
    require(authorized(home), 'disconnected')
    # No URI/file/arguments/new-instance flags: preserve native selected route.
    _run(['/usr/bin/open', '-a', runtime['bundle']])
    return True


@safe_errors
def read_connection(home):
    value = private_json(home, 'connection.json')
    require(exact(value, ('protocol', 'generation', 'url', 'owner_token', 'broker_pid', 'broker_start', 'runtime')))
    require(type(value['protocol']) is int and value['protocol'] == PROTOCOL)
    require(type(value['generation']) is str and re.fullmatch(r'[0-9a-f]{32}', value['generation']))
    require(type(value['owner_token']) is str and re.fullmatch(r'[0-9a-f]{64}', value['owner_token']))
    require(type(value['url']) is str and re.fullmatch(r'http://127\.0\.0\.1:[1-9][0-9]{0,4}', value['url']))
    try:
        require(1 <= urllib.parse.urlsplit(value['url']).port <= 65535)
    except ValueError:
        raise NavigationError() from None
    require(type(value['broker_pid']) is int and type(value['broker_start']) is str
            and 1 <= len(value['broker_start']) <= 80)
    return value


@safe_errors
def validate_connection(connection, home):
    require(authorized(home), 'disconnected')
    require(read_connection(home) == connection, 'changed')
    current = process(connection['broker_pid'])
    require(current['start'] == connection['broker_start'], 'changed')
    require(current['ppid'] == connection['runtime']['backend_pid'], 'changed')
    require(listener(connection['broker_pid'], urllib.parse.urlsplit(connection['url']).port), 'changed')
    verify_runtime(connection['runtime'], home)
    return True


def _manifest(home):
    manifest = private_json(home, 'installation.json')
    require(exact(manifest, ('protocol', 'files')) and type(manifest['protocol']) is int and manifest['protocol'] == 1
            and type(manifest['files']) is dict
            and set(manifest['files']) == set(ASSETS + ('antigravity_navigation.py',)), 'collision')
    require(all(type(v) is str and re.fullmatch(r'[0-9a-f]{64}', v) for v in manifest['files'].values()), 'collision')
    return manifest


def _installed(home, manifest):
    for name, digest in manifest['files'].items():
        raw, _ = snapshot(sidecar_dir(home) / name)
        require(raw is not None and hashlib.sha256(raw).hexdigest() == digest, 'collision')


def _native_entry(home):
    # Read only: native settings have an uncooperative writer, so a file replace
    # cannot provide compare-and-swap. Never write or roll back this shared file.
    path = Path(home) / '.gemini' / 'config' / 'config.json'
    try:
        raw, _ = snapshot(path)
    except FileNotFoundError:
        return None
    value = _json(raw) if raw is not None else {}
    require(type(value) is dict and type(value.get('sidecars', {})) is dict, 'collision')
    entry = value.get('sidecars', {}).get(ID)
    require(entry is None or type(entry) is dict, 'collision')
    return entry


def _owned_before(home):
    try:
        value = private_json(home, 'ownership.json')
        return (exact(value, ('protocol', 'sidecar')) and type(value['protocol']) is int
                and value['protocol'] == 1 and value['sidecar'] == ID)
    except FileNotFoundError:
        return False


def local_state(home):
    out = dict(supported=sys.platform == 'darwin', installed=False, authorized=False,
               state='unsupported', message=MESSAGES['unsupported'])
    if not out['supported']:
        return out
    try:
        try:
            manifest = _manifest(home)
        except FileNotFoundError:
            manifest = None
        if manifest is None:
            out.update(state='setup_needed', message=MESSAGES['setup_needed'])
            return out
        _installed(home, manifest)
        allowed = authorized(home)
        state = 'waiting_panel' if allowed else 'disconnected'
        out.update(installed=True, authorized=allowed, state=state, message=MESSAGES[state])
    except (OSError, NavigationError):
        out.update(state='unavailable', message=MESSAGES['unavailable'])
    return out


@safe_errors
def install(home, repo):
    require(sys.platform == 'darwin', 'unsupported')
    ensure_private(home)
    source = Path(repo) / 'integrations' / 'antigravity-open'
    assets = {name: snapshot(source / name)[0] for name in ASSETS}
    assets['antigravity_navigation.py'] = snapshot(Path(repo) / 'src' / 'antigravity_navigation.py')[0]
    require(all(raw is not None for raw in assets.values()), 'collision')
    expected_manifest = dict(protocol=1, files={name: hashlib.sha256(raw).hexdigest() for name, raw in assets.items()})
    with process_lock(home, 'setup.lock'):
        try:
            previous_manifest = _manifest(home)
        except FileNotFoundError:
            previous_manifest = None
        target = sidecar_dir(home)
        directory, _ = _parent(target / 'placeholder', True)
        os.close(directory)
        journal = []
        if previous_manifest:
            _installed(home, previous_manifest)
        else:
            owned = _owned_before(home)
            require(not any((target / name).exists() or (target / name).is_symlink() for name in assets), 'collision')
            require(owned or not any(target.iterdir()), 'collision')
            require(owned or _native_entry(home) is None, 'collision')
        changed = previous_manifest != expected_manifest
        manager = process_lock(home) if changed else contextlib.nullcontext()
        with manager:
            try:
                for name, raw in assets.items():
                    path = target / name
                    before, mode = snapshot(path)
                    if before != raw:
                        replace(path, raw, before)
                        journal.append((path, before, raw, mode))
                for name, value in (('ownership.json', dict(protocol=1, sidecar=ID)),
                                    ('installation.json', expected_manifest),
                                    ('authorization.json', dict(protocol=1, enabled=True))):
                    path = private_dir(home) / name
                    before, mode = snapshot(path, private=True)
                    after = encoded(value)
                    replace(path, after, before)
                    journal.append((path, before, after, mode))
            except BaseException:
                for path, before, after, mode in reversed(journal):
                    try:
                        if before is None:
                            unlink(path, after)
                        else:
                            replace(path, before, after, mode)
                    except (OSError, NavigationError):
                        pass
                raise
    return local_state(home)


@safe_errors
def disconnect(home):
    require(sys.platform == 'darwin', 'unsupported')
    ensure_private(home)
    with process_lock(home, 'setup.lock'):
        path = private_dir(home) / 'authorization.json'
        before, _ = snapshot(path, private=True)
        replace(path, encoded(dict(protocol=1, enabled=False)), before)
    return local_state(home)


@safe_errors
def remove(home):
    disconnect(home)  # Revocation happens even if a changed artifact blocks deletion.
    with process_lock(home, 'setup.lock'), process_lock(home):
        manifest = _manifest(home)
        _installed(home, manifest)
        entry = _native_entry(home)
        require(entry is None or entry.get('enabled') is False, 'busy')
        for name in manifest['files']:
            path = sidecar_dir(home) / name
            raw, _ = snapshot(path)
            unlink(path, raw)
        path = private_dir(home) / 'installation.json'
        raw, _ = snapshot(path, private=True)
        unlink(path, raw)
    return local_state(home)
