# -*- coding: utf-8 -*-
"""Product-level kiosk tests: every screen renders and scans, and the real main() loop survives the things
an unattended café kiosk meets (slow network, camera loss, model errors, repeated deposits, taps).

Same fakes as test_kiosk_phase1 (no Firebase, no model weights, no camera, no window). Real time passes:
the sorter simulator and the timeouts run on the wall clock with short settings.

    py -3.11 -I -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

import test_kiosk_phase1 as p1
from test_kiosk_phase1 import FakeBox, FakeCap, FakeFirestore, FakeModel

app = None
Renderer = View = None


def setUpModule():
    global app, Renderer, View
    if p1.app is None:
        p1.setUpModule()
    app = p1.app
    from kiosk.ui.screens import Renderer as R, View as V
    Renderer, View = R, V


# ── a flexible driver for the real main() loop ────────────────────────────────
class ScriptedModel:
    """Sees a can while `present[0]` is true."""
    names = {0: "can"}

    def __init__(self, present):
        self.present = present

    def __call__(self, frame, conf=0.25, verbose=False):
        return [types.SimpleNamespace(boxes=[FakeBox(0, 0.92)] if self.present[0] else [])]


def drive(controller, *, model=None, db=None, settings=None, max_frames=900, inline=True, extra=()):
    """Run app.main() until `controller(n, seen)` returns ord('q'). `seen` lists (phase, overlay) per frame.
    Returns (stdout, seen, mouse_callback_holder)."""
    seen, mouse = [], {}
    calls = {"n": 0}
    s = {"width": 1280, "height": 720, "sounds": False, "success_seconds": 0.3, "failure_seconds": 0.3,
         "sorter": "attendant"}
    s.update(settings or {})
    tmp = Path(tempfile.mkdtemp()) / "settings.json"
    tmp.write_text(json.dumps(s), encoding="utf-8")
    real_render = Renderer.render

    def render(self, cam, v, now, transition=True):
        seen.append((v.phase, v.overlay))
        return real_render(self, cam, v, now, transition)

    def wait_key(delay=0):
        n = calls["n"]
        calls["n"] += 1
        if n > max_frames:
            raise AssertionError(f"main() did not finish in {max_frames} frames; last phases {seen[-5:]}")
        time.sleep(0.005)
        return controller(n, seen) or 255

    out = io.StringIO()
    with contextlib.ExitStack() as st:
        st.enter_context(contextlib.redirect_stdout(out))
        st.enter_context(mock.patch.object(app, "SETTINGS_FILE", tmp))
        st.enter_context(mock.patch.object(app.cv2, "VideoCapture", lambda i: FakeCap()))
        for name in ("namedWindow", "resizeWindow", "destroyAllWindows"):
            st.enter_context(mock.patch.object(app.cv2, name, lambda *a, **k: None))
        st.enter_context(mock.patch.object(app.cv2, "imshow", lambda name, img: None))
        st.enter_context(mock.patch.object(app.cv2, "waitKey", wait_key))
        st.enter_context(mock.patch.object(app.cv2, "setMouseCallback", lambda w, cb: mouse.update(cb=cb)))
        st.enter_context(mock.patch.object(app, "draw_splash_animation", lambda name: None))
        st.enter_context(mock.patch.object(app, "model", model or FakeModel()))
        st.enter_context(mock.patch.object(app, "db", db if db is not None else FakeFirestore()))
        st.enter_context(mock.patch.object(Renderer, "render", render))
        if inline:
            st.enter_context(mock.patch.object(app, "run_in_background", lambda fn: fn()))
        for p in extra:
            st.enter_context(p)
        app.main()
    return out.getvalue(), seen, mouse


def phase(seen):
    return seen[-1][0] if seen else None


def tokens(db):
    return [doc for coll, doc, _ in db.sets if coll == "qr_tokens"]


def claim(db, token):
    db.deliver_snapshot("qr_tokens", token, {"usedBy": ["customer-1"]})


def fast_sim():
    """make_sorter replacement: the real SerialSorter over a SimulatedESP32 running 4x faster."""
    from kiosk.hardware import SerialSorter, SimulatedESP32
    holder = {}

    def make(*a, **k):
        device = SimulatedESP32(speed=4.0)
        s = SerialSorter("SIM", 115200, opener=lambda p, b: device, list_ports=lambda: [], log=k.get("log", print),
                         retry_s=0.05, simulated=True)
        s.device = holder["device"] = device
        return s
    return make, holder


class TrayEndToEnd(unittest.TestCase):
    """The bin flow through the real main(): load cell -> YOLO on the tray -> ESP32 sort -> DONE -> QR."""

    def test_two_items_in_a_row_without_any_key(self):
        db = FakeFirestore()
        make, holder = fast_sim()
        state = {"placed": 0, "claimed": 0}

        def controller(n, seen):                       # never presses a key except Q to end the test
            ph = phase(seen)
            if ph == "ready" and state["placed"] == state["claimed"] and state["placed"] < 2:
                holder["device"].place_item(15.0)      # a customer drops an empty can
                state["placed"] += 1
            if ph == "qr" and len(tokens(db)) > state["claimed"]:
                claim(db, tokens(db)[state["claimed"]])
                state["claimed"] += 1
            if state["claimed"] == 2 and ph == "ready":
                return ord("q")

        out, seen, _ = drive(controller, db=db, settings={"mode": "sim", "success_seconds": 0.3},
                             extra=[mock.patch.object(app, "make_sorter", make)], max_frames=3000)
        self.assertEqual(len(tokens(db)), 2, out[-2000:])
        order = [p for i, (p, _) in enumerate(seen) if i == 0 or seen[i - 1][0] != p]
        one = ["ready", "weighing", "identifying", "sorting", "issuing", "qr", "complete"]
        self.assertEqual(order[0], "paused", "out of service until the ESP32 answers PONG")
        self.assertEqual(order[1:15], one + one, order)
        self.assertLess(out.index("[DONE] #1"), out.index("[QR GENERATED]"), "a token was created before DONE")
        self.assertIn("[ITEM] #1 on the tray: ", out)
        self.assertIn("(SIMULATED)", out)
        self.assertEqual([doc for _, doc in db.unsubscribed], tokens(db))

    def test_staff_key_i_places_a_simulated_item(self):
        make, holder = fast_sim()
        state = {}

        def controller(n, seen):
            ph = phase(seen)
            if ph == "ready" and "pressed" not in state:
                state["pressed"] = n
                return ord("i")
            if ph == "sorting":
                return ord("q")

        out, seen, _ = drive(controller, settings={"mode": "sim"}, extra=[mock.patch.object(app, "make_sorter", make)],
                             max_frames=2000)
        self.assertIn("[SIM] item placed on the tray:", out)
        self.assertIn("sorting", [p for p, _ in seen])

    def test_an_unrecognised_item_goes_to_residual_without_a_code(self):
        db = FakeFirestore()
        make, holder = fast_sim()
        state = {}

        def controller(n, seen):
            ph = phase(seen)
            if ph == "ready" and "placed" not in state:
                state["placed"] = holder["device"].place_item(30.0)
            if ph == "rejected":
                return ord("q")

        out, seen, _ = drive(controller, db=db, model=ScriptedModel([False]), settings={"mode": "sim"},
                             extra=[mock.patch.object(app, "make_sorter", make)], max_frames=2000)
        self.assertIn("[REJECT] #1 unknown", out)
        self.assertIn("[DONE] #1 bin 3 (Residual)", out)
        self.assertEqual(tokens(db), [])
        self.assertNotIn("[QR GENERATED]", out)


class CameraSelfServiceEndToEnd(unittest.TestCase):
    def test_a_customer_alone_gets_a_code_without_any_key(self):
        db = FakeFirestore()
        present = [True]

        def controller(n, seen):                       # never presses a key except Q to end the test
            ph = phase(seen)
            if ph == "dropping" and n > 12:
                present[0] = False                      # the customer drops the cup into the bin
            if ph == "qr" and not db.unsubscribed:
                claim(db, tokens(db)[0])
            if ph == "ready" and any(p == "complete" for p, _ in seen):
                return ord("q")

        out, seen, _ = drive(controller, db=db, model=ScriptedModel(present), settings={"sorter": "camera"})
        order = [p for i, (p, _) in enumerate(seen) if i == 0 or seen[i - 1][0] != p]
        self.assertEqual(order[:6], ["detected", "dropping", "issuing", "qr", "complete", "ready"], order)
        self.assertEqual(len(tokens(db)), 1)
        self.assertIn("[SCANNED] +10 pts | Total scans: 1", out)


class TokenContractTests(unittest.TestCase):
    """What the phone app, the web Manual Claim and the rules' isNewKioskToken() rely on (audit K-01)."""

    def test_token_document_and_payload(self):
        import re
        import uuid
        from datetime import datetime, timedelta, timezone
        db = FakeFirestore()
        with mock.patch.object(app, "db", db), contextlib.redirect_stdout(io.StringIO()):
            before = datetime.now(timezone.utc)
            info = app.issue_token("Biodegradable", "plain rice", 0.81)
        coll, token, data = db.sets[0]
        self.assertEqual(coll, "qr_tokens")
        self.assertEqual(set(data), {"wasteType", "expiresAt", "usedBy", "isUsed", "createdAt", "claimCode"})
        self.assertEqual(str(uuid.UUID(token, version=4)), token)
        self.assertEqual(info["payload"], f"ECOBIN:Biodegradable:{token}")
        self.assertEqual(len(info["payload"].split(":")), 3)
        self.assertIsInstance(data["expiresAt"], datetime, "expiresAt must be stored as a Timestamp, not a string")
        self.assertIsNotNone(data["expiresAt"].tzinfo, "naive datetimes are read as local time")
        self.assertAlmostEqual((data["expiresAt"] - before).total_seconds(), app.QR_EXPIRY_MINUTES * 60, delta=5)
        self.assertIs(data["createdAt"], app.SERVER_TS, "the rules require createdAt == request.time")
        self.assertEqual((data["usedBy"], data["isUsed"]), ([], False))
        self.assertRegex(data["claimCode"], r"^\d{4}$")
        self.assertEqual(info["claim_code"], data["claimCode"])
        det = [d for c, d in db.adds if c == "detections"][0]
        self.assertIs(det["timestamp"], app.SERVER_TS)
        self.assertEqual(det["kioskId"], app.KIOSK_ID)

    def test_kiosk_never_writes_customer_collections(self):
        src = Path(app.__file__).read_text(encoding="utf-8")
        for coll in ("users", "waste_logs", "redemptions", "profiles"):
            self.assertNotRegex(src, rf'collection\(\s*["\']{coll}["\']', f"the kiosk must not touch {coll}")


class NonBlockingTests(unittest.TestCase):
    def test_a_slow_token_write_does_not_freeze_the_screen(self):
        class SlowDb(FakeFirestore):
            def collection(self, name):
                coll = super().collection(name)
                if name == "qr_tokens":
                    real = coll.document

                    def document(doc_id):
                        ref = real(doc_id)
                        orig = ref.set
                        ref.set = lambda data: (time.sleep(0.6), orig(data))
                        return ref
                    coll.document = document
                return coll

        db = SlowDb()
        marks = {}

        def controller(n, seen):
            if n == 3:
                marks["space"] = len(seen)
                return 32
            if phase(seen) == "qr":
                marks["qr"] = len(seen)
                return ord("q")

        out, seen, _ = drive(controller, db=db, inline=False)
        self.assertIn("qr", marks, out)
        frames_while_writing = marks["qr"] - marks["space"]
        self.assertGreaterEqual(frames_while_writing, 10,
                                f"only {frames_while_writing} frames drawn during a 0.6 s write: the UI blocked (K-07)")
        self.assertIn("issuing", [p for p, _ in seen[marks["space"]:marks["qr"]]])


class QrLifetimeInTheLoop(unittest.TestCase):
    def test_walk_away_returns_to_ready_and_stops_polling(self):
        db = FakeFirestore()

        def controller(n, seen):
            if n == 3:
                return 32
            if any(p == "qr" for p, _ in seen) and phase(seen) != "qr":   # left the QR (ready, then the can again)
                return ord("q")

        out, seen, _ = drive(controller, db=db, settings={"qr_display_seconds": 0.3})
        self.assertEqual([doc for _, doc in db.unsubscribed], tokens(db))
        self.assertIn("[QR] back to ready", out)

    def test_tap_on_claim_code_opens_the_receipt_and_back_closes_it(self):
        db = FakeFirestore()
        holder = {}

        def tap(action):
            for x1, y1, x2, y2, a in app._renderer.buttons:
                if a == action:
                    holder["cb"](cv2.EVENT_LBUTTONUP, (x1 + x2) // 2, (y1 + y2) // 2, 0, None)
                    return True
            return False

        def controller(n, seen):
            if n == 3:
                return 32
            if phase(seen) == "qr" and seen[-1][1] is None and "opened" not in holder:
                holder["opened"] = tap("receipt")
            elif seen[-1][1] == "receipt" and "closed" not in holder:
                holder["closed"] = tap("close")
            elif holder.get("closed") and seen[-1][1] is None:
                return ord("q")

        def capture(window, cb):
            holder["cb"] = cb
        out, seen, _ = drive(controller, db=db, extra=[mock.patch.object(app.cv2, "setMouseCallback", capture)])
        self.assertTrue(holder.get("opened") and holder.get("closed"), seen[-5:])
        self.assertIn(("qr", "receipt"), seen)


class RecoveryTests(unittest.TestCase):
    def test_camera_outage_shows_a_message_and_recovers(self):
        opens = {"n": 0}

        class FlakyCap(FakeCap):
            def __init__(self):
                opens["n"] += 1
                self.reads = 0
                self.opened = opens["n"] > 2          # the first two attempts fail

            def isOpened(self):
                return self.opened

            def read(self):
                self.reads += 1
                if opens["n"] == 3 and self.reads > 5:    # then the feed drops once
                    return False, None
                return super().read()

        class FastCamera(app.Camera):
            def __init__(self, index):
                super().__init__(index, retry_s=0.05, fail_limit=5)

        def controller(n, seen):
            if opens["n"] >= 4 and phase(seen) == "detected":
                return ord("q")

        out, seen, _ = drive(controller, extra=[mock.patch.object(app.cv2, "VideoCapture", lambda i: FlakyCap()),
                                                mock.patch.object(app, "Camera", FastCamera)])
        self.assertIn("[CAMERA ERROR] Could not access webcam", out)
        self.assertIn("[CAMERA] Lost feed", out)
        self.assertIn("[CAMERA] Re-opened camera 0.", out)

    def test_inference_errors_are_logged_and_the_loop_continues(self):
        state = {"n": 0}

        class Crashy(FakeModel):
            def __call__(self, frame, conf=0.25, verbose=False):
                state["n"] += 1
                if state["n"] <= 3:
                    raise RuntimeError("bad tensor")
                return super().__call__(frame, conf, verbose)

        out, seen, _ = drive(lambda n, seen: ord("q") if phase(seen) == "detected" else None, model=Crashy())
        self.assertIn("[AI ERROR] inference failed: bad tensor", out)
        self.assertEqual(phase(seen), "detected")

    def test_a_drawing_error_does_not_end_the_kiosk(self):
        state = {"n": 0}
        real = app.draw_camera_hud

        def flaky(*a, **k):
            state["n"] += 1
            if state["n"] == 2:
                raise ValueError("bad layout")
            return real(*a, **k)

        out, seen, _ = drive(lambda n, seen: ord("q") if n > 6 else None,
                             extra=[mock.patch.object(app, "draw_camera_hud", flaky)])
        self.assertIn("[ERROR] bad layout", out)
        self.assertGreater(state["n"], 4)

    def test_staff_overlays_open_and_close(self):
        keys = {2: ord("d"), 4: ord("d"), 5: ord("h"), 7: 27, 9: ord("q")}
        out, seen, _ = drive(lambda n, seen: keys.get(n))
        overlays = [o for _, o in seen]
        self.assertIn("diagnostics", overlays)
        self.assertIn("history", overlays)
        self.assertIsNone(overlays[-1])
        self.assertNotIn("[ERROR]", out)


class BinsTests(unittest.TestCase):
    def _sorter(self, simulated):
        from kiosk.hardware import Fill, LinkState
        s = types.SimpleNamespace(mode="esp32", simulated=simulated, state=LinkState.CONNECTED, detail="", port="COM3",
                                  unparsed=0, recent=[], recent_events=[], ready=True, sort=lambda c: True,
                                  tare=lambda: True, weight=None, steady=False, device=None, close=lambda: None)
        sent = {"done": False}

        def poll(now=None):
            if sent["done"]:
                return []
            sent["done"] = True
            return [Fill(12, 55, 90)]
        s.poll = poll
        return s

    def test_real_fill_levels_are_written_in_the_dashboard_shape(self):
        db = FakeFirestore()
        drive(lambda n, seen: ord("q") if n > 3 else None, db=db, settings={"sorter": "esp32"},
              extra=[mock.patch.object(app, "make_sorter", lambda *a, **k: self._sorter(False))])
        bins = {doc: data for coll, doc, data in db.sets if coll == "bins"}
        self.assertEqual(sorted(bins), sorted(f"{app.KIOSK_ID}-{s}" for s in ("bio", "rec", "res")))
        b = bins[f"{app.KIOSK_ID}-res"]
        self.assertEqual(set(b), {"location", "fillLevel", "status", "lastUpdated"})   # web lib/types.ts BinStatus
        self.assertEqual((b["fillLevel"], b["status"]), (90, "online"))
        self.assertIs(b["lastUpdated"], app.SERVER_TS)

    @unittest.skipUnless(__import__("os").environ.get("FIRESTORE_EMULATOR_HOST"), "Firestore emulator not running")
    def test_the_rules_accept_the_kiosks_own_bins_and_the_admin_reads_them(self):
        kiosk_db = p1._emulator_client("kiosk-bins", {"kiosk": True})
        admin_db = p1._emulator_client("staff-bins", {"admin": True})
        with mock.patch.object(app, "db", kiosk_db), mock.patch.object(app, "KIOSK_ID", "kiosk-bins"), \
                mock.patch.object(app, "SERVER_TS", app.KIOSK_SERVER_TIMESTAMP):
            with mock.patch.object(app, "_warn_once", lambda key, msg, every=600: self.fail(msg)):
                app.BinsReporter()._write((12, 55, 90))
        doc = admin_db.collection("bins").document("kiosk-bins-res").get().to_dict()
        self.assertEqual((doc["fillLevel"], doc["status"]), (90, "online"))
        self.assertIsNotNone(doc["lastUpdated"])

    def test_simulated_readings_are_never_written(self):
        db = FakeFirestore()
        drive(lambda n, seen: ord("q") if n > 3 else None, db=db, settings={"sorter": "esp32"},
              extra=[mock.patch.object(app, "make_sorter", lambda *a, **k: self._sorter(True))])
        self.assertEqual([c for c, _, _ in db.sets if c == "bins"], [])


# ── screens ───────────────────────────────────────────────────────────────────
def _views():
    from kiosk.flow import Detection, Failure, FlowConfig, Phase, Toast, TransactionFlow
    det = Detection("can", "Recyclable", 0.87, (260, 140, 380, 350))
    tx = types.SimpleNamespace(waste_type="Recyclable", claim_code="4821", token="2f1c0a8e-1111-4c2d-9e7a-0b5d2c7e9a10")
    payload = "ECOBIN:Recyclable:" + tx.token
    pts = {"Biodegradable": 15, "Recyclable": 10, "Residual": 5}

    def stages(mode, ph, failure=None):
        f = TransactionFlow(FlowConfig(mode=mode))
        f.phase, f.failure = Phase(ph), failure
        return f.stages()
    fail = Failure("sorter_timeout", "The sorter didn't respond", "Please take your item and try again.")
    from kiosk.flow import TrayConfig, TrayFlow

    def tray_st(ph):
        f = TrayFlow(TrayConfig())
        f.phase = Phase(ph)
        return f.stages()
    tray_tx = types.SimpleNamespace(waste_type="Recyclable", grams=15.0, reject=None, label="can", confidence=0.91,
                                    claim_code="4821", token=tx.token)
    tray_reject = types.SimpleNamespace(waste_type="Residual", grams=120.0, label="cup", confidence=0.88,
                                        reject=("too_heavy", "Cups must be empty to earn points. It went to Residual."))
    base = dict(points=pts, clock="10:42 AM")
    qr = dict(tx=tx, qr_modules=app.qr_modules(payload), secs_left=1080, total_secs=1200,
              expires_local="11:02 AM", issued_local="10:42 AM", issued_date="Oct 10, 2026", phase_age=5)
    return payload, {
        "ready": View(phase="ready", stages=stages("attendant", "ready"), **base),
        "ready-offline": View(phase="ready", mode="esp32", online=False, sorter_state="reconnecting",
                              stages=stages("esp32", "ready"), **base),
        "detected": View(phase="detected", detection=det, latest=det, stages=stages("attendant", "detected"), **base),
        "detected-esp32": View(phase="detected", mode="esp32", sorter_state="connected", sorter_ready=True,
                               detection=det, latest=det, lock_progress=0.6, stages=stages("esp32", "detected"), **base),
        "sorting-sim": View(phase="sorting", mode="sim", sorter_state="connected", tx=tx,
                            stages=stages("sim", "sorting"), **base),
        "issuing": View(phase="issuing", tx=tx, stages=stages("attendant", "issuing"), **base),
        "dropping-camera": View(phase="dropping", mode="camera", tx=tx, drop_left=11.0, drop_timeout=15.0,
                                stages=stages("camera", "dropping"), **base),
        "detected-cooldown": View(phase="detected", mode="camera", detection=det, latest=det, cooldown_left=9.0,
                                  stages=stages("camera", "detected"), **base),
        "qr": View(phase="qr", stages=stages("attendant", "qr"), **qr, **base),
        "receipt": View(phase="qr", overlay="receipt", stages=stages("attendant", "qr"), **qr, **base),
        "complete": View(phase="complete", tx=tx, phase_age=5, seconds_to_ready=3, **base),
        "failed": View(phase="failed", mode="esp32", tx=tx, failure=fail, seconds_to_ready=5,
                       stages=stages("esp32", "failed", fail), **base),
        "history": View(phase="ready", overlay="history", history=[
            {"waste_type": "Residual", "points": 5, "claim_code": "7310", "ts": "9:31 AM"}], **base),
        "history-empty": View(phase="ready", overlay="history", **base),
        "diagnostics": View(phase="ready", overlay="diagnostics", diagnostics=[("Kiosk", [("Name", "x")])], **base),
        "no-camera": View(phase="ready", camera_ok=False, toast=Toast("No item detected yet", "info", 2.0), **base),
        "tray-ready": View(phase="ready", mode="esp32", tray=True, sorter_state="connected", stages=tray_st("ready"),
                           **base),
        "tray-weighing": View(phase="weighing", mode="esp32", tray=True, weight=14.2, weighing_hint=True,
                              stages=tray_st("weighing"), **base),
        "tray-identifying": View(phase="identifying", mode="sim", tray=True, weight=14.2, lock_progress=0.4,
                                 tx=types.SimpleNamespace(waste_type=None, grams=14.2, reject=None, label=None,
                                                          confidence=0.0), stages=tray_st("identifying"), **base),
        "tray-sorting": View(phase="sorting", mode="esp32", tray=True, tx=tray_tx, stages=tray_st("sorting"), **base),
        "tray-sorting-reject": View(phase="sorting", mode="esp32", tray=True, tx=tray_reject,
                                    stages=tray_st("sorting"), **base),
        "tray-rejected": View(phase="rejected", mode="esp32", tray=True, tx=tray_reject,
                              notice=("Sorted into Residual", "Cups must be empty to earn points. It went to Residual."),
                              stages=tray_st("rejected"), **base),
        "tray-notice": View(phase="notice", mode="esp32", tray=True, tx=tray_tx, online=False,
                            notice=("Sorted!", "Reward codes are offline right now."), stages=tray_st("notice"), **base),
        "tray-jam": View(phase="jam", mode="esp32", tray=True, weight=15.0, stages=tray_st("jam"), **base),
        "tray-bin-full": View(phase="paused", mode="esp32", tray=True, pause_reason="bin_full", fill=(100, 62, 30),
                              **base),
        "tray-out-of-service": View(phase="paused", mode="esp32", tray=True, pause_reason="link",
                                    sorter_state="reconnecting", **base),
        "tray-no-pyserial": View(phase="paused", mode="esp32", tray=True, pause_reason="link", sorter_state="error",
                                 sorter_detail="PySerial is not installed (pip install pyserial)", **base),
        "tray-no-port": View(phase="paused", mode="esp32", tray=True, pause_reason="link",
                             sorter_state="reconnecting", sorter_port="COM3",
                             sorter_detail="could not open port 'COM3': FileNotFoundError", **base),
        "tray-port-busy": View(phase="paused", mode="esp32", tray=True, pause_reason="link",
                               sorter_state="reconnecting", sorter_port="COM3",
                               sorter_detail="could not open port 'COM3': PermissionError(13, 'Access is denied.')", **base),
        "tray-bin-warn": View(phase="ready", mode="esp32", tray=True, bins_warn=["Recyclable"], fill=(40, 93, 10),
                              stages=tray_st("ready"), **base),
        "tray-qr": View(phase="qr", mode="esp32", tray=True, stages=tray_st("qr"), **qr, **base),
        "tray-complete": View(phase="complete", mode="esp32", tray=True, tx=tx, phase_age=5, seconds_to_ready=3, **base),
        "tray-diagnostics": View(phase="ready", mode="sim", tray=True, overlay="diagnostics", weight=0.3, steady=True,
                                 fill=(35, 50, 20), serial_lines=["> P", "< PONG", "> 2", "< OK:2", "< DONE:2:15.0"],
                                 preview=np.full((480, 640, 3), 120, np.uint8), tray_roi=[0.2, 0.1, 0.8, 0.9],
                                 diagnostics=[("Kiosk", [("Name", "x")])], **base),
        "model-mismatch": View(phase="detected", model_ok=False, detection=Detection("person", None, 0.9),
                               latest=Detection("person", None, 0.9, (10, 10, 600, 470)), **base),
    }


class SorterHelpTextTests(unittest.TestCase):
    """The staff box names the problem in plain words; raw (and truncated) errors only as a last resort."""

    def setUp(self):
        from kiosk.ui.screens import sorter_help_text
        self.f = sorter_help_text

    def test_nothing_plugged_in_says_so_without_the_raw_error(self):
        detail = "could not open port 'COM3': FileNotFoundError(2, 'The system cannot find the fil"   # cut at 80
        title, body = self.f(detail, "reconnecting", "COM3")
        self.assertEqual(title, "No ESP32 found on COM3")
        self.assertIn("data USB cable", body)
        self.assertNotIn("FileNotFoundError", body)

    def test_the_other_cases_keep_their_wording(self):
        self.assertEqual(self.f("could not open port 'COM3': PermissionError(13, 'Access is denied.')",
                                "reconnecting", "COM3")[0], "COM3 is in use by another program")
        self.assertEqual(self.f("PySerial is not installed for Python 3.14 (py -3.14 -m pip install pyserial)",
                                "error", None)[0], "The serial library is missing for Python 3.14")
        self.assertEqual(self.f("COM3: waiting for PONG", "connecting", "COM3")[0],
                         "Connected to COM3, but the ESP32 does not answer")
        self.assertEqual(self.f("something odd", "reconnecting", "COM3")[0], "Cannot open COM3")


class ScreenTests(unittest.TestCase):
    SIZES = [(1280, 720), (1024, 600), (1920, 1080)]

    def setUp(self):
        self.cam = np.full((480, 640, 3), 120, np.uint8)

    def test_every_screen_renders_at_every_supported_size(self):
        _, views = _views()
        for w, h in self.SIZES:
            r = Renderer(w, h)
            for name, v in views.items():
                with self.subTest(size=f"{w}x{h}", screen=name):
                    img = r.render(None if not v.camera_ok else self.cam, v, 1.0, transition=False)
                    self.assertEqual(img.shape, (h, w, 3))
                    self.assertGreater(float(img.std()), 5.0, "blank frame")

    def test_the_qr_on_screen_scans_to_the_exact_payload(self):
        payload, views = _views()
        detector = cv2.QRCodeDetector()
        for w, h in self.SIZES:
            with self.subTest(size=f"{w}x{h}"):
                img = Renderer(w, h).render(self.cam, views["qr"], 10.0, transition=False)
                text, _, _ = detector.detectAndDecode(img)
                self.assertEqual(text, payload)

    def test_touch_buttons_are_at_least_80_px_tall_at_every_size(self):
        _, views = _views()
        for w, h in self.SIZES:
            r = Renderer(w, h)
            for name in ("qr", "receipt", "failed", "history", "tray-qr", "diagnostics"):
                r.render(self.cam, views[name], 1.0, transition=False)
                self.assertTrue(r.buttons, name)
                for x1, y1, x2, y2, action in r.buttons:
                    self.assertGreaterEqual(y2 - y1, 80, f"{w}x{h} {name}/{action} button is too small")

    def test_text_colours_meet_wcag_aa(self):
        from kiosk.ui.theme import C

        def lum(bgr):
            def ch(c):
                c /= 255
                return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
            b, g, r = bgr
            return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)

        def ratio(a, b):
            la, lb = sorted([lum(a), lum(b)], reverse=True)
            return (la + 0.05) / (lb + 0.05)
        pairs = {   # (text, background) pairs the screens actually use for normal-size text
            "ink/canvas": (C.ink, C.canvas), "ink_2/canvas": (C.ink_2, C.canvas), "ink_2/surface": (C.ink_2, C.surface),
            "forest/surface": (C.forest, C.surface), "blue/surface": (C.blue, C.surface),
            "charcoal/surface": (C.charcoal, C.surface), "white/forest": (C.white, C.forest),
            "white/error": (C.white, C.error), "ink/amber": (C.ink, C.amber), "amber_ink/amber_tint": (C.amber_ink, C.amber_tint),
            "error_ink/error_tint": (C.error_ink, C.error_tint), "forest_dark/sage(chip)": (C.forest_dark, C.sage),
            "blue_ink/blue_tint": (C.blue_ink, C.blue_tint), "charcoal/charcoal_tint": (C.charcoal, C.charcoal_tint), "ink/sunken": (C.ink, C.sunken), "ink_2/sunken": (C.ink_2, C.sunken),
            "white/blue": (C.white, C.blue), "white/charcoal": (C.white, C.charcoal),
        }
        low = {k: round(ratio(*v), 2) for k, v in pairs.items() if ratio(*v) < 4.5}
        self.assertEqual(low, {})

    def test_steady_state_frame_cost(self):
        _, views = _views()
        r = Renderer(1280, 720)
        r.render(self.cam, views["detected-esp32"], 0.0)
        t = time.perf_counter()
        for i in range(30):
            r.render(self.cam, views["detected-esp32"], 1.0 + i * 0.03)
        ms = (time.perf_counter() - t) / 30 * 1000
        self.assertLess(ms, 40, f"{ms:.1f} ms per frame")   # measured ≈ 6 ms on the development PC


if __name__ == "__main__":
    unittest.main()
