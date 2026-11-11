#!/usr/bin/env python3
"""Inspect or delete retained Officer usage records; never call an API."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from officer_usage import UsageLedger


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=int, default=30, help='Retained period to summarise, one to ninety days.')
    parser.add_argument('--delete', action='store_true', help='Delete retained usage records; recording continues if WATCH is running.')
    args = parser.parse_args()
    ledger = UsageLedger(Path.home() / '.live-room')
    try:
        if args.delete:
            deleted = ledger.delete()
            print(json.dumps({'deleted': deleted, **ledger.status()}))
            return 0 if deleted else 1
        value = ledger.summary(args.days)
        print(json.dumps(value, indent=2))
        return 0 if value['available'] else 1
    finally:
        ledger.close()


if __name__ == '__main__':
    raise SystemExit(main())
