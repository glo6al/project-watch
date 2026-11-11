"""Instruction packaging tests; no models or private room state are contacted."""
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import officer_instructions as instructions
import server


class CoreInstructionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'officer'
        self.root.mkdir()
        self.plain = instructions.BUILTIN_PLAIN_ENGLISH
        self.officer = instructions.BUILTIN_OFFICER_SYSTEM[len(self.plain):]
        self.write_core()
        self.patcher = patch.object(instructions, '_OFFICER_DIR', self.root)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def write_core(self, newline='\n'):
        for name, text in [('VOCABULARY.md', self.plain), ('OFFICER.md', self.officer)]:
            (self.root / name).write_bytes(text.replace('\n', newline).encode('utf-8'))

    def assert_fallback(self):
        bundle = instructions.load_instructions()
        self.assertFalse(bundle.packaged)
        self.assertEqual(bundle.system, instructions.BUILTIN_OFFICER_SYSTEM)
        self.assertEqual(bundle.plain_english, instructions.BUILTIN_PLAIN_ENGLISH)

    def test_shipped_prompt_is_exact_original(self):
        # Pins the pre-extraction UTF-8 prompt, including its original whitespace.
        bundle = instructions.load_instructions()
        self.assertTrue(bundle.packaged)
        self.assertEqual(hashlib.sha256(bundle.system.encode()).hexdigest(),
                         'c28133c5f9da7870356be72f67061fca40fc5fd0c1cceafcfa9c8019bf46fe05')
        with patch.object(instructions, '_OFFICER_DIR', Path(instructions.__file__).resolve().parents[1] / 'officer'):
            self.assertEqual(instructions.load_instructions(), bundle)

    def test_windows_line_endings_normalize_without_changing_prompt(self):
        self.write_core('\r\n')
        self.assertEqual(instructions.load_instructions().system, instructions.BUILTIN_OFFICER_SYSTEM)

    def test_bad_core_falls_back_together(self):
        for raw in [b'', b'  \n', b'\xff', b'bad\0text', b'bad\rtext', b'x' * 65537]:
            with self.subTest(raw_length=len(raw)):
                (self.root / 'VOCABULARY.md').write_bytes(raw)
                self.assert_fallback()
        self.write_core()
        (self.root / 'OFFICER.md').unlink()
        self.assert_fallback()

    def test_unreadable_core_falls_back(self):
        with patch.object(instructions.os, 'open', side_effect=PermissionError):
            self.assert_fallback()

    def test_directory_and_symlinks_are_rejected(self):
        path = self.root / 'OFFICER.md'
        path.unlink()
        path.mkdir()
        self.assert_fallback()
        path.rmdir()
        target = Path(self.tmp.name) / 'outside.md'
        target.write_text(self.officer)
        try:
            path.symlink_to(target)
        except OSError:
            self.skipTest('Symlinks unavailable')
        self.assert_fallback()
        path.unlink()
        (self.root / 'VOCABULARY.md').unlink()
        self.root.rmdir()
        self.root.symlink_to(target.parent, target_is_directory=True)
        self.assert_fallback()

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'POSIX special file test')
    def test_special_file_is_rejected_without_opening(self):
        (self.root / 'OFFICER.md').unlink()
        os.mkfifo(self.root / 'OFFICER.md')
        self.assert_fallback()

    def test_exchange_pins_core_for_all_rounds(self):
        calls = []
        def call(body, **kwargs):
            calls.append(body['system'])
            if len(calls) == 1:
                (self.root / 'OFFICER.md').write_text('\nChanged after snapshot')
                return {'stop_reason': 'tool_use', 'content': [{'type': 'tool_use', 'id': 'read', 'name': 'list_agents', 'input': {}}]}, ''
            return {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'Recorded answer.'}]}, ''
        out = server.officer_exchange([{'role': 'user', 'content': 'What happened?'}], call,
                                      context={'scope': 'room'}, model_snapshot=('fictional-test-model', False))
        self.assertEqual(out['text'], 'Recorded answer.')
        self.assertEqual(calls[0], calls[1])
        self.assertTrue(calls[0].startswith(instructions.BUILTIN_OFFICER_SYSTEM + '\nTrusted local calendar metadata:'))


if __name__ == '__main__':
    unittest.main()
