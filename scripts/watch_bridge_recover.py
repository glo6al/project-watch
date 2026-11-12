#!/usr/bin/env python3
"""Dry-run or explicitly apply offline recovery of proven pruned peer requests."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from peer_recovery import RecoveryError, recover


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', required=True, help='Existing private bridge state directory')
    parser.add_argument('--codex-sessions', help='Owner-selected native Codex sessions root')
    parser.add_argument('--claude-projects', help='Owner-selected native Claude projects root')
    parser.add_argument('--claude-store', help='Exact private WATCH plugin store JSON file')
    parser.add_argument('--client-id', action='append', dest='selected', help='Client request UUID; repeat for a reviewed allowlist')
    parser.add_argument('--retire-client-id', action='append', dest='retire_selected',
                        help='Explicit owner retirement of an unresolved request; does not claim native completion or cancellation')
    parser.add_argument('--apply', action='store_true', help='Apply only the explicit allowlist; refuses an active bridge writer')
    args = parser.parse_args()
    try:
        report = recover(**vars(args))
    except RecoveryError as error:
        print(json.dumps({'applied': False, 'error': str(error)}))
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
