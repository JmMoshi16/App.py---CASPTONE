# -*- coding: utf-8 -*-
"""kiosk_firestore.py: the least-privilege kiosk client (audit C-02).

Unit tests use a fake HTTP layer. EmulatorTests run the real client against the real
firebase/firestore.rules in the local emulator; they are skipped unless FIRESTORE_EMULATOR_HOST is set:

    cd firebase/tests
    npx firebase emulators:exec --config ../firebase.json --only firestore --project demo-ecoreward ^
        "py -3.11 -I -m unittest discover -s ../../tests -p test_kiosk_firestore.py -v"
"""
import base64
import importlib.util
import json
import os
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_FILE = Path(__file__).resolve().parents[1] / "kiosk_firestore.py"
_spec = importlib.util.spec_from_file_location("kiosk_firestore_under_test", _FILE)
kf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kf)


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


class FakeHttp:
    def __init__(self):
        self.posts, self.gets, self.docs, self.refreshes = [], [], {}, 0

    def post(self, url, json=None, data=None, headers=None, timeout=None):
        if "securetoken" in url:
            self.refreshes += 1
            return _Resp(200, {"id_token": f"id-{self.refreshes}", "refresh_token": "r2",
                               "expires_in": "3600", "user_id": "kiosk-uid"})
        self.posts.append((url, json, headers))
        return _Resp(200, {"writeResults": [{}]})

    def get(self, url, headers=None, timeout=None):
        self.gets.append(url)
        path = url.split("/documents/", 1)[1]
        if path not in self.docs:
            return _Resp(404, {"error": {"message": "not found"}})
        return _Resp(200, {"fields": self.docs[path]})


class EncodingTests(unittest.TestCase):
    def test_round_trip_of_every_type_the_kiosk_writes(self):
        when = datetime(2026, 10, 9, 1, 2, 3, 456000, tzinfo=timezone.utc)
        value = {"s": "can", "i": 7, "f": 0.92, "b": False, "n": None, "t": when, "a": [], "m": {"x": [1, "y"]}}
        encoded = {k: kf._encode(v) for k, v in value.items()}
        self.assertEqual(encoded["i"], {"integerValue": "7"})
        self.assertEqual(encoded["b"], {"booleanValue": False})          # bool is not an integer
        self.assertEqual(encoded["t"], {"timestampValue": "2026-10-09T01:02:03.456000Z"})
        self.assertEqual({k: kf._decode(v) for k, v in encoded.items()}, value)

    def test_naive_datetimes_are_refused(self):
        with self.assertRaises(ValueError):
            kf._encode(datetime(2026, 1, 1))

    def test_server_timestamp_becomes_a_server_transform_not_a_client_value(self):
        w = kf.build_write("p/qr_tokens/t1", {"isUsed": False, "createdAt": kf.SERVER_TIMESTAMP}, must_not_exist=True)
        self.assertEqual(w["update"]["fields"], {"isUsed": {"booleanValue": False}})
        self.assertEqual(w["updateTransforms"], [{"fieldPath": "createdAt", "setToServerValue": "REQUEST_TIME"}])
        self.assertEqual(w["currentDocument"], {"exists": False})
        self.assertNotIn("updateMask", w)

    def test_merge_only_touches_the_given_fields(self):
        w = kf.build_write("p/kiosks/k", {"status": "online", "name": "PC", "lastSeen": kf.SERVER_TIMESTAMP}, merge=True)
        self.assertEqual(w["updateMask"], {"fieldPaths": ["name", "status"]})


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttp()
        self.tokens = kf.RefreshTokenSource("public-api-key", "r1", http=self.http)
        self.db = kf.KioskFirestore("demo-proj", self.tokens, http=self.http)

    def test_id_token_is_cached_and_uid_comes_from_the_sign_in(self):
        self.assertEqual(self.db.uid, "kiosk-uid")
        self.db.collection("qr_tokens").document("t1").set({"isUsed": False})
        self.db.collection("qr_tokens").document("t2").set({"isUsed": False})
        self.assertEqual(self.http.refreshes, 1)
        self.assertEqual(self.http.posts[0][2], {"Authorization": "Bearer id-1"})

    def test_set_posts_one_commit_to_the_right_document(self):
        self.db.collection("qr_tokens").document("abc").set({"wasteType": "Recyclable"})
        url, body, _ = self.http.posts[0]
        self.assertTrue(url.endswith("/projects/demo-proj/databases/(default)/documents:commit"))
        self.assertEqual(body["writes"][0]["update"]["name"], "projects/demo-proj/databases/(default)/documents/qr_tokens/abc")

    def test_add_creates_a_new_document_and_never_overwrites(self):
        doc_id = self.db.collection("detections").add({"label": "can"})
        write = self.http.posts[0][1]["writes"][0]
        self.assertTrue(write["update"]["name"].endswith(f"/detections/{doc_id}"))
        self.assertEqual(write["currentDocument"], {"exists": False})

    def test_get_decodes_fields_and_reports_missing_documents(self):
        self.http.docs["qr_tokens/t1"] = {"usedBy": {"arrayValue": {"values": [{"stringValue": "u1"}]}}}
        snap = self.db.collection("qr_tokens").document("t1").get()
        self.assertTrue(snap.exists)
        self.assertEqual(snap.to_dict(), {"usedBy": ["u1"]})
        self.assertFalse(self.db.collection("qr_tokens").document("nope").get().exists)

    def test_errors_raise_with_the_status(self):
        class Denied(FakeHttp):
            def post(self, url, json=None, data=None, headers=None, timeout=None):
                if "securetoken" in url:
                    return super().post(url, json, data, headers, timeout)
                return _Resp(403, {"error": {"message": "Missing or insufficient permissions."}})
        db = kf.KioskFirestore("p", kf.RefreshTokenSource("k", "r", http=Denied()), http=Denied())
        with self.assertRaises(kf.KioskFirestoreError) as ctx:
            db.collection("users").document("alice").set({"points": 1})
        self.assertEqual(ctx.exception.status, 403)

    def test_from_config_requires_the_three_fields(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "kiosk.json"
            p.write_text(json.dumps({"apiKey": "k", "projectId": "p"}))
            with self.assertRaises(ValueError):
                kf.KioskFirestore.from_config(p)


class PollWatcherTests(unittest.TestCase):
    def test_calls_back_once_per_change_and_stops_on_unsubscribe(self):
        http = FakeHttp()
        db = kf.KioskFirestore("p", kf.RefreshTokenSource("k", "r", http=http), http=http)
        doc = db.collection("qr_tokens").document("t1")
        http.docs["qr_tokens/t1"] = {"usedBy": {"arrayValue": {}}}
        seen, changed = [], threading.Event()

        def cb(snaps, _changes, _t):
            seen.append(snaps[0].to_dict())
            if snaps[0].to_dict()["usedBy"]:
                changed.set()

        w = kf._PollWatcher(doc, cb, fast=0.02, slow=0.02)
        time.sleep(0.15)                                  # several polls, no change -> one callback
        http.docs["qr_tokens/t1"] = {"usedBy": {"arrayValue": {"values": [{"stringValue": "cust"}]}}}
        self.assertTrue(changed.wait(2))
        w.unsubscribe()
        time.sleep(0.1)
        polls = len(http.gets)
        time.sleep(0.1)
        self.assertEqual(seen, [{"usedBy": []}, {"usedBy": ["cust"]}])
        self.assertEqual(len(http.gets), polls, "watcher kept polling after unsubscribe")


def _unsigned_emulator_token(project, uid, claims):
    """The Firestore emulator accepts unsigned ID tokens; production never does."""
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    now = int(time.time())
    payload = {"iss": f"https://securetoken.google.com/{project}", "aud": project, "sub": uid, "user_id": uid,
               "iat": now, "exp": now + 3600, "auth_time": now,
               "firebase": {"sign_in_provider": "custom", "identities": {}}, **claims}
    return f"{enc({'alg': 'none', 'typ': 'JWT'})}.{enc(payload)}."


@unittest.skipUnless(os.environ.get("FIRESTORE_EMULATOR_HOST"), "Firestore emulator not running")
class EmulatorTests(unittest.TestCase):
    """The real client against the real rules: what a kiosk can and cannot do."""
    PROJECT = "demo-ecoreward"

    def client(self, uid, claims):
        token = _unsigned_emulator_token(self.PROJECT, uid, claims)
        return kf.KioskFirestore(self.PROJECT, lambda: token, base_url=f"http://{os.environ['FIRESTORE_EMULATOR_HOST']}/v1")

    def test_kiosk_flow_works_end_to_end(self):
        db = self.client("kiosk-e2e", {"kiosk": True})
        self.assertEqual(db.uid, "kiosk-e2e")
        token = str(uuid.uuid4())
        db.collection("qr_tokens").document(token).set({
            "wasteType": "Recyclable", "expiresAt": datetime.now(timezone.utc) + timedelta(minutes=20),
            "usedBy": [], "isUsed": False, "createdAt": kf.SERVER_TIMESTAMP, "claimCode": "4821"})
        snap = db.collection("qr_tokens").document(token).get()
        self.assertEqual(snap.to_dict()["usedBy"], [])
        self.assertIsInstance(snap.to_dict()["createdAt"], datetime)
        db.collection("kiosks").document("kiosk-e2e").set(
            {"lastSeen": kf.SERVER_TIMESTAMP, "status": "online", "name": "TEST-PC"}, merge=True)
        db.collection("detections").add({"wasteType": "Recyclable", "label": "can", "confidence": 0.9,
                                         "modelFile": "best.pt", "modelSha256": "x", "timestamp": kf.SERVER_TIMESTAMP,
                                         "token": token, "kioskId": "kiosk-e2e"})

    def test_kiosk_cannot_read_customer_data_or_forge_tokens(self):
        db = self.client("kiosk-e2e", {"kiosk": True})
        for path in ("users", "profiles", "redemptions"):
            with self.assertRaises(kf.KioskFirestoreError) as ctx:
                db.collection(path).document("alice").get()
            self.assertEqual(ctx.exception.status, 403, path)
        with self.assertRaises(kf.KioskFirestoreError):
            db.collection("qr_tokens").document(str(uuid.uuid4())).set({
                "wasteType": "Recyclable", "expiresAt": datetime.now(timezone.utc) + timedelta(days=1),
                "usedBy": [], "isUsed": False, "createdAt": kf.SERVER_TIMESTAMP, "claimCode": "4821"})

    def test_an_ordinary_customer_token_cannot_create_qr_tokens(self):
        db = self.client("alice", {})
        with self.assertRaises(kf.KioskFirestoreError):
            db.collection("qr_tokens").document(str(uuid.uuid4())).set({
                "wasteType": "Recyclable", "expiresAt": datetime.now(timezone.utc) + timedelta(minutes=20),
                "usedBy": [], "isUsed": False, "createdAt": kf.SERVER_TIMESTAMP, "claimCode": "4821"})


if __name__ == "__main__":
    unittest.main()
