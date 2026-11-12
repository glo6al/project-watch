#!/usr/bin/env python3
"""Inspect bridge migration in memory; never migrate or repair the live database."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import peer_bridge
import peer_lifecycle


def inspect(state_dir):
    directory = Path(state_dir).absolute()
    path = directory / 'officer-messages.sqlite3'
    if directory.resolve() != directory or path.resolve() != path:
        raise ValueError('Use a plain private state directory.')
    for candidate, regular in ((directory, False), (path, True)):
        info = candidate.stat()
        if (info.st_uid != os.getuid() or info.st_mode & 0o077
                or (not stat.S_ISREG(info.st_mode) if regular else not stat.S_ISDIR(info.st_mode))):
            raise ValueError('State must be private and owned by the current user.')
    identity = path.stat()
    snapshot = sqlite3.connect(':memory:')
    try:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2) as source:
            source.execute('PRAGMA query_only=ON')
            source.backup(snapshot)
        after = path.stat()
        if (identity.st_dev, identity.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError('The state database changed identity during inspection.')
        # Schema creation and migration operate only on the private memory copy.
        peer_bridge.shared_schema(snapshot)
        counts = dict(snapshot.execute('SELECT execution,COUNT(*) FROM peer_execution GROUP BY execution'))
        unresolved = snapshot.execute("SELECT COUNT(DISTINCT agent) FROM peer_execution WHERE execution NOT IN ('completed','dropped')").fetchone()[0]
        return {'read_only': True, 'execution_counts': counts,
                'recipients_with_unresolved_execution': unresolved,
                'server_execution_allows_downgrade': peer_lifecycle.downgrade_ready(snapshot),
                'client_receipts_checked': False,
                'downgrade_requirement': 'Plugin completion receipts must also be acknowledged before downgrade.'}
    finally:
        snapshot.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', default=str(Path.home() / '.live-room'))
    args = parser.parse_args()
    try:
        print(json.dumps(inspect(args.state_dir), indent=2))
    except (OSError, ValueError, sqlite3.Error):
        print('Bridge preflight could not verify the private state snapshot; no records were changed.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
