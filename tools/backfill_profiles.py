#!/usr/bin/env python3
"""Backfill users.lifetimePoints and the public profiles/{uid} copies (audit H-13 / M-11).

Accounts created before Phase 3/5 have no `profiles` document (so they are missing from the leaderboard)
and no `lifetimePoints`. For every customer this sets
    lifetimePoints = current balance + every point ever redeemed   (only if the field is missing)
and writes profiles/{uid} = {username, points, lifetimePoints, badges} copied from users/{uid}.
Profiles of non-customers (admins) are deleted so they do not appear on the leaderboard.

Usage:
    python tools/backfill_profiles.py            # dry run (default): prints what would change
    python tools/backfill_profiles.py --apply    # writes

Uses the kiosk's Admin SDK credential (see app.py). Run a dry run first.
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]


def plan(users, redemptions, profiles):
    """Pure planning step. users/profiles: {uid: dict}; redemptions: [dict]. Returns list of actions."""
    redeemed = defaultdict(int)
    for r in redemptions:
        redeemed[r.get("userId", "")] += int(r.get("pointsUsed", 0) or 0)

    actions = []
    for uid, u in users.items():
        if u.get("role") != "customer":
            if uid in profiles:
                actions.append(("delete_profile", uid, None))
            continue
        lifetime = u.get("lifetimePoints")
        if not isinstance(lifetime, int):
            lifetime = int(u.get("points", 0) or 0) + redeemed[uid]
            actions.append(("set_lifetime", uid, lifetime))
        mirror = {
            "username": u.get("username", ""),
            "points": int(u.get("points", 0) or 0),
            "lifetimePoints": lifetime,
            "badges": list(u.get("badges", []) or []),
        }
        if profiles.get(uid) != mirror:
            actions.append(("set_profile", uid, mirror))
    return actions


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    sys.path.insert(0, str(APP_DIR))
    import app as kiosk  # reuses the kiosk's credential lookup (imported here, not at module load)
    db = kiosk.db
    if db is None:
        sys.exit("[ERROR] Firebase is offline - cannot run the backfill.")

    users = {d.id: d.to_dict() for d in db.collection("users").stream()}
    profiles = {d.id: d.to_dict() for d in db.collection("profiles").stream()}
    redemptions = [d.to_dict() for d in db.collection("redemptions").stream()]
    actions = plan(users, redemptions, profiles)

    for kind, uid, value in actions:
        print(f"  {kind:15} {uid[:10]:10} {value if kind != 'set_profile' else value['lifetimePoints']}")
        if not args.apply:
            continue
        if kind == "set_lifetime":
            db.collection("users").document(uid).update({"lifetimePoints": value})
        elif kind == "set_profile":
            db.collection("profiles").document(uid).set(value)
        elif kind == "delete_profile":
            db.collection("profiles").document(uid).delete()

    print(f"\n{'[APPLIED]' if args.apply else '[DRY RUN]'} {len(actions)} change(s) for {len(users)} user(s).")
    if not args.apply and actions:
        print("Re-run with --apply to write them.")


if __name__ == "__main__":
    main()
