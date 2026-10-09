#!/usr/bin/env python3
"""Erase one customer's personal data on request (roadmap 7.7, see docs/privacy.md).

Run on an ADMIN's computer with the service-account key. Dry run by default:

    py -3.11 tools/delete_user_data.py customer@example.com            # shows what would be deleted
    py -3.11 tools/delete_user_data.py customer@example.com --apply    # deletes it

Deleted: the sign-in account, users/{uid}, profiles/{uid}, and every waste_logs, redemptions and
qr_scan_history document of that uid. QR tokens they scanned are kept, because they hold no personal data
(only the opaque uid in `usedBy`), and other customers' totals do not change.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from set_claims import credential_path  # noqa: E402

# (collection, field holding the customer's uid); None = the document id is the uid
PERSONAL_DATA = [
    ("users", None),
    ("profiles", None),
    ("waste_logs", "userId"),
    ("redemptions", "userId"),
    ("qr_scan_history", "scannedByUid"),
]


def plan(db, uid):
    """Document references to delete, grouped by collection."""
    found = {}
    for coll, field in PERSONAL_DATA:
        if field is None:
            ref = db.collection(coll).document(uid)
            found[coll] = [ref] if ref.get().exists else []
        else:
            found[coll] = [d.reference for d in db.collection(coll).where(field, "==", uid).stream()]
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("who", help="the customer's e-mail address or uid")
    ap.add_argument("--apply", action="store_true", help="really delete (default: dry run)")
    ap.add_argument("--key", help="service-account key file")
    args = ap.parse_args(argv)

    import firebase_admin
    from firebase_admin import auth, credentials, firestore
    firebase_admin.initialize_app(credentials.Certificate(str(credential_path(args.key))))
    db = firestore.client()

    try:
        user = auth.get_user_by_email(args.who) if "@" in args.who else auth.get_user(args.who)
        uid = user.uid
    except auth.UserNotFoundError:
        user, uid = None, args.who
        print("[INFO] No sign-in account found; looking for leftover data under that uid.")
    if user and (user.custom_claims or {}).get("admin"):
        sys.exit("[REFUSED] That is an admin account. Remove the admin claim first (set_claims.py unset-admin).")

    found = plan(db, uid)
    total = sum(len(v) for v in found.values())
    for coll, refs in found.items():
        print(f"  {coll:16} {len(refs)} document(s)")
    print(f"  {'sign-in account':16} {'1' if user else '0'}")

    if not args.apply:
        print(f"\n[DRY RUN] {total} document(s) would be deleted. Re-run with --apply to delete.")
        return
    batch, pending = db.batch(), 0
    for refs in found.values():
        for ref in refs:
            batch.delete(ref)
            pending += 1
            if pending == 400:          # Firestore batches hold at most 500 writes
                batch.commit()
                batch, pending = db.batch(), 0
    if pending:
        batch.commit()
    if user:
        auth.delete_user(uid)
    print(f"\n[DONE] Deleted {total} document(s){' and the sign-in account' if user else ''} for uid {uid[:8]}...")


if __name__ == "__main__":
    main()
