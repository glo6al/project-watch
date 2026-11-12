#!/usr/bin/env python3
"""Explicit user-scope installation controls for native Antigravity navigation."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import antigravity_navigation as navigation


def main():
    parser = argparse.ArgumentParser(description='Connect or disconnect WATCH Navigation. No app restart or agent work is started.')
    parser.add_argument('action', choices=('install', 'disconnect', 'remove', 'status'))
    args = parser.parse_args()
    try:
        home = Path.home()
        if args.action == 'install':
            result = navigation.install(home, Path(__file__).resolve().parents[1])
        else:
            result = getattr(navigation, 'local_state' if args.action == 'status' else args.action)(home)
    except (navigation.NavigationError, OSError, ValueError):
        print(json.dumps(dict(state='unavailable', message=navigation.MESSAGES['unavailable'])))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
