"""The upgrade preview must not create lifecycle tables in the source database."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import watch_bridge_preflight as preflight


@unittest.skipIf(os.name == 'nt', 'POSIX state ownership')
class PreflightTests(unittest.TestCase):
    def test_migration_only_changes_the_in_memory_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            root.chmod(0o700)
            path = root / 'officer-messages.sqlite3'
            with sqlite3.connect(path) as db:
                preflight.peer_bridge.shared_schema(db)
                db.execute('DROP TABLE peer_execution')
                db.execute('DROP TABLE peer_execution_events')
                db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)',
                           ('fictional-client', 'x:fictional-target', '{}', 'fictional-hash',
                            'fictional-transport', 'fictional-credential', 1, 2, ''))
            path.chmod(0o600)
            before = path.read_bytes()
            result = preflight.inspect(str(root))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(result['execution_counts'], {'uncertain': 1})
            self.assertEqual(result['recipients_with_unresolved_execution'], 1)
            self.assertFalse(result['client_receipts_checked'])
            self.assertNotIn('fictional-target', json.dumps(result))
            with sqlite3.connect(path) as db:
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='peer_execution'").fetchone())

    def test_symlink_state_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / 'state').mkdir(mode=0o700)
            (root / 'alias').symlink_to(root / 'state', target_is_directory=True)
            with self.assertRaises(ValueError):
                preflight.inspect(str(root / 'alias'))

    def test_owner_retirement_is_reported_without_counting_a_blocked_recipient(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            root.chmod(0o700)
            path = root / 'officer-messages.sqlite3'
            with sqlite3.connect(path) as db:
                preflight.peer_bridge.shared_schema(db)
                db.execute('INSERT INTO peer_execution VALUES (?,?,?,?,?,?)',
                           ('fictional-request', 'c:fictional-recipient', 1,
                            'retired', 'withheld', '{}'))
            path.chmod(0o600)
            before = path.read_bytes()
            result = preflight.inspect(str(root))
            self.assertEqual(result['execution_counts'], {'retired': 1})
            self.assertEqual(result['recipients_with_unresolved_execution'], 0)
            self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
