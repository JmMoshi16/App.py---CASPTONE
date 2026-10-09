#!/usr/bin/env python3
"""H-07 timestamp migration: convert ISO string timestamps to Firestore Timestamps.

Usage:
    # Dry run (default) — prints what would change, writes nothing:
    python tools/migrate_timestamps.py

    # Live run — actually updates Firestore documents:
    python tools/migrate_timestamps.py --apply

Requirements: the kiosk service-account key must be in place (see app.py credential lookup).
Tested against the Firestore emulator; run a dry-run against production first.
"""
import sys
import argparse
from pathlib import Path
from datetime import datetime, timezone

# Locate app.py so we can reuse its credential-loading logic without re-implementing it.
APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
import app as kiosk  # noqa: E402 — intentional

from firebase_admin import firestore  # noqa: E402


def _parse_iso_to_ts(value):
    """Return a Firestore Timestamp for an ISO string, or None if already a datetime / not a string."""
    if not isinstance(value, str):
        return None  # already a Timestamp (returned by Admin SDK as datetime)
    s = value.rstrip('Z').replace('+00:00', '').replace('+0000', '')
    try:
        dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


# (collection, list of field names that should be Timestamps)
COLLECTIONS = [
    ('qr_tokens',       ['createdAt', 'expiresAt']),
    ('waste_logs',      ['timestamp']),
    ('qr_scan_history', ['scannedAt', 'tokenCreatedAt', 'expiresAt']),
    ('users',           ['createdAt']),
    ('redemptions',     ['redeemedAt']),
]


def migrate(apply: bool) -> None:
    db = kiosk.db
    if db is None:
        print('[ERROR] Firebase is offline — cannot run migration.')
        sys.exit(1)

    total_docs = 0
    total_fields = 0

    for coll_name, fields in COLLECTIONS:
        docs = list(db.collection(coll_name).stream())
        print(f'\n{coll_name}: {len(docs)} documents')
        for doc in docs:
            data = doc.to_dict()
            updates = {}
            for field in fields:
                raw = data.get(field)
                new_val = _parse_iso_to_ts(raw)
                if new_val is not None:
                    updates[field] = new_val
                    print(f'  [{doc.id[:8]}] {field}: "{raw}" -> Timestamp({new_val.isoformat()})')
            if updates:
                total_docs += 1
                total_fields += len(updates)
                if apply:
                    doc.reference.update(updates)

    print(f'\n{"[APPLIED]" if apply else "[DRY RUN]"} {total_fields} field(s) in {total_docs} document(s) '
          f'{"updated" if apply else "would be updated"}.')
    if not apply:
        print('Re-run with --apply to write changes to Firestore.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Migrate ISO timestamp strings to Firestore Timestamps.')
    parser.add_argument('--apply', action='store_true', help='Actually write changes (default: dry run)')
    args = parser.parse_args()
    migrate(apply=args.apply)
