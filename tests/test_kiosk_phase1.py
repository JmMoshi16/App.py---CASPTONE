# -*- coding: utf-8 -*-
"""Phase 1 regression tests for the kiosk (app.py).

Run from the project folder:

    py -3.11 -I -m unittest discover -s tests -v

What is faked (so the tests are safe and fast):
  * firebase_admin / Firestore  -> in-memory fake; nothing leaves this PC, the real credential is never read
  * ultralytics.YOLO            -> fake detector; best.pt is never loaded
  * cv2 camera + windows        -> scripted camera / key presses; no window is opened
  * the start-up splash and the beep are replaced by no-ops
Everything else (HUD drawing, QR rendering, the main loop) is the real code.

Set KIOSK_APP_DIR to a folder containing a different copy of app.py to test that copy instead
(used to prove these tests fail on the pre-fix snapshot).
"""
import ast
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

APP_DIR = Path(os.environ.get("KIOSK_APP_DIR") or Path(__file__).resolve().parents[1])
APP_FILE = APP_DIR / "app.py"
app = None  # the imported kiosk module (set in setUpModule)


# ── fakes ──────────────────────────────────────────────────────────────────────
class FakeWatcher:
    def __init__(self, db, key):
        self.db, self.key = db, key

    def unsubscribe(self):
        self.db.unsubscribed.append(self.key)


class FakeDocRef:
    def __init__(self, db, coll, doc_id):
        self.db, self.coll, self.doc_id = db, coll, doc_id

    def set(self, data):
        self.db.sets.append((self.coll, self.doc_id, dict(data)))

    def on_snapshot(self, callback):
        self.db.listeners[(self.coll, self.doc_id)] = callback
        return FakeWatcher(self.db, (self.coll, self.doc_id))


class FakeCollection:
    def __init__(self, db, name):
        self.db, self.name = db, name

    def document(self, doc_id):
        return FakeDocRef(self.db, self.name, doc_id)

    def where(self, *args, **kwargs):
        return self

    def stream(self):
        return iter(())


class FakeFirestore:
    """In-memory stand-in for firebase_admin.firestore.client()."""

    def __init__(self):
        self.sets, self.listeners, self.unsubscribed = [], {}, []

    def collection(self, name):
        return FakeCollection(self, name)

    def deliver_snapshot(self, coll, doc_id, data):
        """Pretend Firestore delivered a snapshot of coll/doc_id (e.g. a phone claimed the token)."""
        snap = types.SimpleNamespace(id=doc_id, exists=True, to_dict=lambda: data)
        self.listeners[(coll, doc_id)]([snap], [], None)


class FakeBox:
    def __init__(self, cls_id, conf):
        self.cls, self.conf, self.xyxy = [cls_id], [conf], [[200, 120, 440, 360]]


class FakeModel:
    """Always 'sees' one can (-> Recyclable, +10 pts) with high confidence."""
    names = {0: "can"}

    def __call__(self, frame, conf=0.25, verbose=False):
        return [types.SimpleNamespace(boxes=[FakeBox(0, 0.92)])]


class FakeCap:
    def isOpened(self):
        return True

    def read(self):
        return True, np.full((480, 640, 3), 60, np.uint8)

    def release(self):
        pass


def _install_fakes():
    """Replace heavy / external modules BEFORE app.py is imported."""
    class _NoCredential:
        def __init__(self, *a, **k):
            raise FileNotFoundError("fake: no credential at import time (tests inject app.db themselves)")

    fb = types.ModuleType("firebase_admin")
    fb._apps = {}
    fb.initialize_app = lambda cred=None, **k: None
    fb.credentials = types.SimpleNamespace(Certificate=_NoCredential)
    fb.firestore = types.SimpleNamespace(client=lambda: FakeFirestore())
    sys.modules["firebase_admin"] = fb

    ul = types.ModuleType("ultralytics")
    ul.YOLO = lambda path: FakeModel()
    sys.modules["ultralytics"] = ul


def setUpModule():
    global app
    _install_fakes()
    spec = importlib.util.spec_from_file_location("kiosk_app_under_test", APP_FILE)
    app = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = app
    spec.loader.exec_module(app)


# ── helpers ────────────────────────────────────────────────────────────────────
SPACE, NOKEY, QUIT = 32, 255, ord("q")


def run_main(keys, hooks=None):
    """Run the real main() loop with a scripted keyboard.

    keys  : {waitKey call index: key code}   (default: no key)
    hooks : {waitKey call index: callable}   (run just before that call returns - e.g. a phone claims the QR)
    Returns (stdout text, shown frames).
    """
    hooks = hooks or {}
    calls = {"n": 0}
    frames = []

    def fake_wait_key(delay=0):
        n = calls["n"]
        calls["n"] += 1
        if n > 60:
            raise AssertionError("main() did not quit - test script is wrong")
        if n in hooks:
            hooks[n]()
        return keys.get(n, NOKEY)

    out = io.StringIO()
    with contextlib.redirect_stdout(out), \
            mock.patch.object(app.cv2, "VideoCapture", lambda i: FakeCap()), \
            mock.patch.object(app.cv2, "namedWindow", lambda *a, **k: None), \
            mock.patch.object(app.cv2, "resizeWindow", lambda *a, **k: None), \
            mock.patch.object(app.cv2, "destroyAllWindows", lambda *a, **k: None), \
            mock.patch.object(app.cv2, "imshow", lambda name, img: frames.append(img.shape)), \
            mock.patch.object(app.cv2, "waitKey", fake_wait_key), \
            mock.patch.object(app, "draw_splash_animation", lambda name: None), \
            mock.patch.object(app, "_beep", lambda *a, **k: None), \
            mock.patch.object(app, "model", FakeModel()):
        app.main()
    return out.getvalue(), frames


# ── C-04: the kiosk must notice that the QR was scanned ───────────────────────
class ScanDetectionTests(unittest.TestCase):
    def test_phone_scan_switches_kiosk_to_success_screen(self):
        db = FakeFirestore()
        token_holder = {}

        def phone_claims_the_qr():
            coll, token, data = db.sets[-1]
            token_holder["token"] = token
            db.deliver_snapshot(coll, token, {**data, "usedBy": ["customer-uid-1"]})

        with mock.patch.object(app, "db", db), \
                mock.patch.object(app, "draw_qr_panel", wraps=app.draw_qr_panel) as qr_panel, \
                mock.patch.object(app, "draw_success", wraps=app.draw_success) as success:
            out, frames = run_main({3: SPACE, 6: QUIT}, hooks={4: phone_claims_the_qr})

        self.assertEqual(len(db.sets), 1, "exactly one token should have been written")
        coll, token, data = db.sets[0]
        self.assertEqual(coll, "qr_tokens")
        self.assertEqual(data["wasteType"], "Recyclable")
        self.assertIs(data["isUsed"], False)
        self.assertEqual(data["usedBy"], [])
        self.assertRegex(data["claimCode"], r"^\d{4}$")
        self.assertGreaterEqual(qr_panel.call_count, 1, "the QR must have been shown first")
        self.assertGreaterEqual(success.call_count, 1, "kiosk never reacted to the scan (C-04)")
        self.assertIn("[SCANNED] +10 pts | Total scans: 1", out)
        # the HUD is the 820-px camera area; the QR panel and success screen are the full 1280-px window
        self.assertTrue(all(h == 720 and c == 3 for h, _, c in frames))
        self.assertIn((720, 1280, 3), frames)

    def test_qr_is_not_dismissed_if_nobody_scanned(self):
        db = FakeFirestore()
        with mock.patch.object(app, "db", db), \
                mock.patch.object(app, "draw_success", wraps=app.draw_success) as success:
            out, _ = run_main({3: SPACE, 6: QUIT})
        self.assertEqual(success.call_count, 0)
        self.assertNotIn("[SCANNED]", out)

    def test_callback_for_a_different_token_is_ignored(self):
        """A late callback from a previously watched token must not dismiss the new QR."""
        db = FakeFirestore()
        with mock.patch.object(app, "db", db):
            app.watch_token("token-A")
            app.watch_token("token-B")  # replaces A as the watched token
            stale_cb = db.listeners[("qr_tokens", "token-A")]
            stale_cb([types.SimpleNamespace(id="token-A", exists=True, to_dict=lambda: {"usedBy": ["x"]})], [], None)
            self.assertFalse(_scan_flag_is_set(), "stale callback for token-A wrongly flagged a scan")
            db.deliver_snapshot("qr_tokens", "token-B", {"usedBy": ["x"]})
            self.assertTrue(_scan_flag_is_set())
            _clear_scan_flag()


def _scan_flag_is_set():
    return app._scan_event.is_set() if hasattr(app, "_scan_event") else bool(app.qr_scanned)


def _clear_scan_flag():
    if hasattr(app, "_scan_event"):
        app._scan_event.clear()
    else:
        app.qr_scanned = False


# ── C-03: no Firebase => no QR; credential lookup is explicit ─────────────────
class OfflineTests(unittest.TestCase):
    def test_space_does_not_issue_a_qr_when_firebase_is_offline(self):
        with mock.patch.object(app, "db", None), \
                mock.patch.object(app, "draw_qr_panel", wraps=app.draw_qr_panel) as qr_panel:
            out, _ = run_main({3: SPACE, 5: QUIT})
        self.assertEqual(qr_panel.call_count, 0, "a QR for a token that was never stored was shown (C-03)")
        self.assertIn("[BLOCKED]", out)

    def test_hud_shows_an_offline_banner(self):
        cam = np.full((720, 820, 3), 90, np.uint8)
        online = app.draw_camera_hud(cam, "Unknown", "Residual", 0.0, True, 0)
        offline = app.draw_camera_hud(cam, "Unknown", "Residual", 0.0, False, 0)
        region = (slice(56, 84), slice(100, 700))
        diff = np.abs(online.astype(int) - offline.astype(int))[region].mean()
        self.assertGreater(diff, 20, "no visible banner when Firebase is offline")


class CredentialLookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.app_dir = self.tmp / "project"
        self.app_dir.mkdir()
        self.default_key = self.tmp / "home" / "kiosk-sa.json"

    def _find(self, env=None):
        env = {"GOOGLE_APPLICATION_CREDENTIALS": env} if env is not None else {}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(app, "_APP_DIR", self.app_dir), \
                mock.patch.object(app, "_DEFAULT_KEY", self.default_key):
            if env == {}:
                os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
            return app.find_service_account()

    def test_env_var_wins(self):
        key = self.tmp / "from-env.json"
        key.write_text("{}")
        (self.app_dir / "serviceAccountKey.json.json").write_text("{}")
        found, _ = self._find(env=str(key))
        self.assertEqual(found, key)

    def test_env_var_pointing_to_a_missing_file_does_not_fall_through(self):
        (self.app_dir / "serviceAccountKey.json.json").write_text("{}")
        found, tried = self._find(env=str(self.tmp / "missing.json"))
        self.assertIsNone(found)
        self.assertIn("GOOGLE_APPLICATION_CREDENTIALS", tried[0])

    def test_default_location_outside_the_project(self):
        self.default_key.parent.mkdir()
        self.default_key.write_text("{}")
        found, _ = self._find()
        self.assertEqual(found, self.default_key)

    def test_legacy_names_in_the_project_folder_still_work(self):
        for name in ("serviceAccountKey.json.json",):  # the name the README told people to use
            (self.app_dir / name).write_text("{}")
        found, _ = self._find()
        self.assertEqual(found, self.app_dir / "serviceAccountKey.json.json")

    def test_nothing_found_lists_every_place_that_was_checked(self):
        found, tried = self._find()
        self.assertIsNone(found)
        self.assertEqual(len(tried), 3)


# ── static guard: the C-04 bug class can't come back ──────────────────────────
class NoShadowedGlobalsTest(unittest.TestCase):
    def test_no_function_assigns_a_name_that_another_function_declares_global(self):
        tree = ast.parse(APP_FILE.read_text(encoding="utf-8"))
        funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

        def own_nodes(fn):  # nodes of fn, not of nested functions
            stack = list(ast.iter_child_nodes(fn))
            while stack:
                n = stack.pop()
                yield n
                if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    stack.extend(ast.iter_child_nodes(n))

        declared_global = {}
        for fn in funcs:
            for n in own_nodes(fn):
                if isinstance(n, ast.Global):
                    for name in n.names:
                        declared_global.setdefault(name, set()).add(fn.name)
        problems = []
        for fn in funcs:
            own_globals = {name for n in own_nodes(fn) if isinstance(n, ast.Global) for name in n.names}
            for n in own_nodes(fn):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    if n.id in declared_global and n.id not in own_globals:
                        problems.append(f"{fn.name}() assigns '{n.id}' without 'global' (line {n.lineno}); "
                                        f"declared global in {sorted(declared_global[n.id])}")
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
