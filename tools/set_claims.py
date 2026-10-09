#!/usr/bin/env python3
"""Manage the kiosk identity and admin claims (audit C-01 / C-02, roadmap 7.1 / 7.4).

Run this on an ADMIN's computer that has the service-account key - never on the kiosk.

    # 1. Create the kiosk identity and its config file (copy that file to the kiosk afterwards)
    py -3.11 tools/set_claims.py create-kiosk --name cafe-kiosk-1 --api-key <Web API key>

    # 2. Give / take away admin rights (the person must sign out and in again)
    py -3.11 tools/set_claims.py set-admin   staff@example.com
    py -3.11 tools/set_claims.py unset-admin staff@example.com

    # 3. A kiosk PC was lost or stolen: cut it off (its tokens stop working within the hour)
    py -3.11 tools/set_claims.py revoke-kiosk cafe-kiosk-1

    py -3.11 tools/set_claims.py list           # who has the admin / kiosk claim

The Web API key is public (it is also in the Android app): Firebase console -> Project settings ->
General -> "Web API key". The kiosk config holds that key, the project id and a REVOCABLE refresh
token. No password is ever created for the kiosk user: it signs in with a one-time custom token.
"""
import argparse
import getpass
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
DEFAULT_KEY = Path.home() / ".ecoreward" / "kiosk-sa.json"
DEFAULT_OUT = Path.home() / ".ecoreward" / "kiosk.json"
SIGN_IN_WITH_CUSTOM_TOKEN = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken"


def credential_path(explicit=None):
    """Same order as the kiosk: --key, GOOGLE_APPLICATION_CREDENTIALS, ~/.ecoreward/kiosk-sa.json, legacy."""
    candidates = [explicit, os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip() or None, DEFAULT_KEY,
                  APP_DIR / "serviceAccountKey.json", APP_DIR / "serviceAccountKey.json.json"]
    for c in candidates:
        if c and Path(c).expanduser().is_file():
            return Path(c).expanduser()
    sys.exit("[ERROR] Service-account key not found (use --key or GOOGLE_APPLICATION_CREDENTIALS).")


def merged_claims(current, **changes):
    """Existing custom claims with `changes` applied; a value of None removes that claim."""
    claims = dict(current or {})
    for k, v in changes.items():
        if v is None:
            claims.pop(k, None)
        else:
            claims[k] = v
    return claims


def kiosk_uid(name):
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name.strip().lower())
    if not safe:
        raise ValueError("kiosk name is empty")
    return f"kiosk-{safe}"[:128]


def restrict_to_current_user(path):
    """Best effort: only the current OS user may read the config file."""
    if platform.system() == "Windows":
        user = os.environ.get("USERNAME") or getpass.getuser()
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                       check=False, capture_output=True)
    else:
        os.chmod(path, 0o600)


def write_kiosk_config(path, api_key, project_id, refresh_token, uid):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"apiKey": api_key, "projectId": project_id, "refreshToken": refresh_token,
                                "uid": uid}, indent=2), encoding="utf-8")
    restrict_to_current_user(path)
    return path


def create_kiosk(auth, http, project_id, api_key, name, out):
    uid = kiosk_uid(name)
    try:
        user = auth.get_user(uid)
        if user.disabled:
            auth.update_user(uid, disabled=False)
    except auth.UserNotFoundError:
        auth.create_user(uid=uid, display_name=f"Kiosk {name}")
        user = auth.get_user(uid)
    auth.set_custom_user_claims(uid, merged_claims(user.custom_claims, kiosk=True, admin=None))
    custom = auth.create_custom_token(uid, {"kiosk": True})
    custom = custom.decode() if isinstance(custom, bytes) else custom
    r = http.post(f"{SIGN_IN_WITH_CUSTOM_TOKEN}?key={api_key}",
                  json={"token": custom, "returnSecureToken": True}, timeout=15)
    if r.status_code != 200:
        msg = r.json().get("error", {}).get("message", r.text[:200])
        sys.exit(f"[ERROR] Could not sign the kiosk in ({msg}). Check the Web API key and that it "
                 "belongs to this project.")
    return uid, write_kiosk_config(out, api_key, project_id, r.json()["refreshToken"], uid)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--key", help="service-account key file (admin computer only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-kiosk", help="create/refresh the kiosk identity and write its config")
    c.add_argument("--name", default=platform.node() or "kiosk")
    c.add_argument("--api-key", required=True, help="Firebase Web API key (public)")
    c.add_argument("--out", default=str(DEFAULT_OUT))
    r = sub.add_parser("revoke-kiosk", help="disable a kiosk identity and revoke its tokens")
    r.add_argument("name")
    for cmd in ("set-admin", "unset-admin"):
        sub.add_parser(cmd).add_argument("email")
    sub.add_parser("list")
    args = ap.parse_args(argv)

    import firebase_admin
    import requests
    from firebase_admin import auth, credentials
    key = credential_path(args.key)
    app = firebase_admin.initialize_app(credentials.Certificate(str(key)))
    project_id = app.project_id

    if args.cmd == "create-kiosk":
        uid, out = create_kiosk(auth, requests, project_id, args.api_key, args.name, args.out)
        print(f"[DONE] Kiosk identity '{uid}' (claim kiosk=true) - config written to {out}")
        print("       Copy that file to the kiosk PC as %USERPROFILE%\\.ecoreward\\kiosk.json, then REMOVE the")
        print("       service-account key from the kiosk PC. Revoke with: set_claims.py revoke-kiosk " + args.name)
    elif args.cmd == "revoke-kiosk":
        uid = kiosk_uid(args.name)
        auth.update_user(uid, disabled=True)
        auth.revoke_refresh_tokens(uid)
        print(f"[DONE] '{uid}' disabled and its refresh tokens revoked; any ID token it holds expires within 1 hour.")
    elif args.cmd in ("set-admin", "unset-admin"):
        user = auth.get_user_by_email(args.email)
        claims = merged_claims(user.custom_claims, admin=True if args.cmd == "set-admin" else None)
        auth.set_custom_user_claims(user.uid, claims)
        print(f"[DONE] {args.email}: admin={'yes' if claims.get('admin') else 'no'}. "
              "They must sign out and back in for it to take effect.")
    elif args.cmd == "list":
        for u in auth.list_users().iterate_all():
            claims = u.custom_claims or {}
            if claims.get("admin") or claims.get("kiosk"):
                kind = "kiosk" if claims.get("kiosk") else "admin"
                print(f"  {kind:5}  {u.uid}  {u.email or ''}  {'(disabled)' if u.disabled else ''}")


if __name__ == "__main__":
    main()
