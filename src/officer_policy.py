"""Fixed local Officer routing policy; policy text never enters a model request."""
import os
from pathlib import Path
import re
import stat
from dataclasses import dataclass

AUTOMATIC = '@automatic'
POLICY_PATH = Path(__file__).resolve().parents[1] / 'officer' / 'MODEL_POLICY.md'
MAX_POLICY_BYTES = 4096
_MODEL_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z')
_KEY_SHAPE = re.compile(rb'sk-ant-[A-Za-z0-9_-]{20,}')
POLICY_ERROR = 'Automatic Officer routing is unavailable. Check MODEL_POLICY.md or choose a model manually.'


@dataclass(frozen=True)
class ModelPolicy:
    qa: str
    summary: str


@dataclass(frozen=True)
class RoutingSnapshot:
    selection: str
    qa: tuple
    summary: tuple

    @property
    def automatic(self):
        return self.selection == AUTOMATIC


def load_policy():
    """Read bounded regular UTF-8 Markdown, rejecting links and invalid metadata."""
    try:
        directory = POLICY_PATH.parent.lstat()
        if not stat.S_ISDIR(directory.st_mode) or getattr(directory, 'st_file_attributes', 0) & 0x400:
            raise ValueError()
        before = POLICY_PATH.lstat()
        if not stat.S_ISREG(before.st_mode) or getattr(before, 'st_file_attributes', 0) & 0x400 or before.st_size > MAX_POLICY_BYTES:
            raise ValueError()
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
        fd = os.open(POLICY_PATH, flags)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(before, opened):
                raise ValueError()
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                raw = stream.read(MAX_POLICY_BYTES + 1)
        finally:
            os.close(fd)
        if len(raw) > MAX_POLICY_BYTES or _KEY_SHAPE.search(raw):
            raise ValueError()
        text = raw.decode('utf-8').replace('\r\n', '\n')
        if '\x00' in text or '\r' in text:
            raise ValueError()
        header = text.partition('\n\n')[0].splitlines()
        if len(header) != 3 or header[0] != 'Version: 1':
            raise ValueError()
        values = []
        for line, prefix in zip(header[1:], ('Q&A model: ', 'Summary model: ')):
            if not line.startswith(prefix):
                raise ValueError()
            value = line[len(prefix):]
            if not _MODEL_ID.fullmatch(value):
                raise ValueError()
            values.append(value)
        return ModelPolicy(*values)
    except (OSError, ValueError, UnicodeError):
        raise ValueError(POLICY_ERROR) from None
