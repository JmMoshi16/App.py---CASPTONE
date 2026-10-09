# -*- coding: utf-8 -*-
"""tools/set_claims.py with a fake firebase_admin.auth and fake HTTP (nothing leaves this PC)."""
import importlib.util
import json
import tempfile
import types
import unittest
from pathlib import Path

_FILE = Path(__file__).resolve().parents[1] / "tools" / "set_claims.py"
_spec = importlib.util.spec_from_file_location("set_claims_under_test", _FILE)
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)


class FakeAuth:
    class UserNotFoundError(Exception):
        pass

    def __init__(self):
        self.users, self.custom_token_claims = {}, None

    def get_user(self, uid):
        if uid not in self.users:
            raise self.UserNotFoundError(uid)
        return types.SimpleNamespace(**self.users[uid])

    def create_user(self, uid, display_name):
        self.users[uid] = {"uid": uid, "disabled": False, "custom_claims": None, "display_name": display_name}

    def update_user(self, uid, disabled):
        self.users[uid]["disabled"] = disabled

    def set_custom_user_claims(self, uid, claims):
        self.users[uid]["custom_claims"] = claims

    def create_custom_token(self, uid, claims):
        self.custom_token_claims = claims
        return b"custom-token-for-" + uid.encode()


class FakeHttp:
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        return types.SimpleNamespace(status_code=200, json=lambda: {"refreshToken": "refresh-xyz", "idToken": "id"})


class SetClaimsTests(unittest.TestCase):
    def test_claims_merge_keeps_others_and_removes_with_none(self):
        self.assertEqual(sc.merged_claims({"admin": True, "x": 1}, admin=None, kiosk=True), {"x": 1, "kiosk": True})
        self.assertEqual(sc.merged_claims(None, admin=True), {"admin": True})

    def test_kiosk_uid_is_safe(self):
        self.assertEqual(sc.kiosk_uid("Cafe Kiosk #1"), "kiosk-cafe-kiosk--1")
        with self.assertRaises(ValueError):
            sc.kiosk_uid("   ")

    def test_create_kiosk_writes_a_config_without_password_or_private_key(self):
        auth, http = FakeAuth(), FakeHttp()
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "sub" / "kiosk.json"
            uid, path = sc.create_kiosk(auth, http, "ecoreward-bin", "public-web-key", "cafe-1", out)
            cfg = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(uid, "kiosk-cafe-1")
        self.assertEqual(cfg, {"apiKey": "public-web-key", "projectId": "ecoreward-bin",
                               "refreshToken": "refresh-xyz", "uid": "kiosk-cafe-1"})
        self.assertEqual(auth.users[uid]["custom_claims"], {"kiosk": True})
        self.assertEqual(auth.custom_token_claims, {"kiosk": True})
        self.assertEqual(http.calls[0][1], {"token": "custom-token-for-kiosk-cafe-1", "returnSecureToken": True})

    def test_re_creating_a_revoked_kiosk_re_enables_it_and_never_keeps_admin(self):
        auth = FakeAuth()
        auth.users["kiosk-k"] = {"uid": "kiosk-k", "disabled": True, "custom_claims": {"admin": True}}
        with tempfile.TemporaryDirectory() as d:
            sc.create_kiosk(auth, FakeHttp(), "p", "k", "k", Path(d) / "kiosk.json")
        self.assertFalse(auth.users["kiosk-k"]["disabled"])
        self.assertEqual(auth.users["kiosk-k"]["custom_claims"], {"kiosk": True})


if __name__ == "__main__":
    unittest.main()
