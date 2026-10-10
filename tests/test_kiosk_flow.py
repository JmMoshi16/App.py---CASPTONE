# -*- coding: utf-8 -*-
"""The deposit state machine (kiosk/flow.py) and the sorter link (kiosk/hardware.py), without hardware.

    py -3.11 -I -m unittest discover -s tests -v
"""
import random
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kiosk import config as cfgmod                                          # noqa: E402
from kiosk.flow import (Detection, FlowConfig, IssueToken, Phase, SendSort, Sound, StopWatching, Tare,  # noqa: E402
                        TransactionFlow, TrayConfig, TrayFlow, WatchToken)
from kiosk.hardware import (AttendantSorter, Booted, DeviceError, Done, Fill, Jam, LinkLost, LinkState,  # noqa: E402
                            LinkUp, Ok, Pong, SerialSorter, SimulatedESP32, Weight, WeightTracker, make_sorter,
                            parse_line)

CAN = Detection("can", "Recyclable", 0.9, (1, 2, 3, 4))
PEEL = Detection("banana peel", "Biodegradable", 0.8, (1, 2, 3, 4))
UTC0 = datetime(2026, 10, 10, 2, 0, tzinfo=timezone.utc)


def flow(mode="attendant", **kw):
    f = TransactionFlow(FlowConfig(mode=mode, **kw))
    f.set_context(online=True, model_ok=True, sorter_ready=True)
    return f


def kinds(actions):
    return [type(a).__name__ for a in actions]


def see(f, det, n, t0=0.0, dt=0.04):
    out = []
    for i in range(n):
        out += f.observe(det, t0 + i * dt)
    return out


class AttendantFlowTests(unittest.TestCase):
    def test_one_space_one_token_and_repeats_are_ignored(self):
        f = flow()
        see(f, CAN, 3)
        a = f.confirm(1.0)
        self.assertEqual(kinds(a), ["IssueToken"])
        self.assertEqual(f.phase, Phase.ISSUING)
        self.assertEqual(f.confirm(1.1), [], "a second SPACE while issuing must not start a second token")
        self.assertEqual(f.on_issue_ok(a[0].tx_id, "tok-1", "1234", UTC0 + timedelta(minutes=20), "ECOBIN:x", 1.2),
                         [WatchToken("tok-1")])
        self.assertEqual(f.phase, Phase.QR)
        self.assertEqual(f.confirm(1.3), [], "SPACE during a QR must not issue another code")

    def test_scan_completes_once_and_returns_to_ready(self):
        f = flow()
        see(f, CAN, 3)
        tx = f.confirm(1.0)[0].tx_id
        f.on_issue_ok(tx, "tok-1", "1234", UTC0 + timedelta(minutes=20), "p", 1.1)
        self.assertEqual(f.on_scanned("other-token", 2.0), [], "a scan of another token is ignored")
        a = f.on_scanned("tok-1", 2.0)
        self.assertEqual(kinds(a), ["StopWatching", "Sound"])
        self.assertEqual(f.phase, Phase.COMPLETE)
        self.assertIn("[SCANNED] +10 pts | Total scans: 1", f.notices)
        self.assertEqual(f.on_scanned("tok-1", 2.1), [], "a repeated snapshot does not count twice")
        f.tick(2.0 + 4.0, UTC0)
        self.assertEqual(f.phase, Phase.READY)
        self.assertEqual(f.completed, 1)
        self.assertEqual(len(f.history), 1)

    def test_blocked_reasons_in_priority_order(self):
        f = flow()
        f.set_context(online=False, model_ok=False, sorter_ready=True)
        f.confirm(0)
        self.assertIn("Firebase is offline", f.notices[-1])
        f.set_context(online=True, model_ok=False, sorter_ready=True)
        f.confirm(0)
        self.assertIn("does not match config/waste_map.json", f.notices[-1])
        f.set_context(online=True, model_ok=True, sorter_ready=True)
        f.confirm(0)
        self.assertIn("No waste detected", f.notices[-1])
        self.assertEqual(f.phase, Phase.READY)

    def test_late_write_result_for_a_timed_out_transaction_is_ignored(self):
        f = flow(issue_timeout=5)
        see(f, CAN, 3)
        old = f.confirm(1.0)[0].tx_id
        f.tick(6.5, UTC0)
        self.assertEqual(f.phase, Phase.FAILED)
        f.tick(6.5 + 7.1, UTC0)
        self.assertEqual(f.phase, Phase.READY)
        see(f, CAN, 3, t0=20)
        new = f.confirm(21)[0].tx_id
        self.assertNotEqual(old, new)
        self.assertEqual(f.on_issue_ok(old, "late", "0000", UTC0, "p", 21.5), [], "stale result resurrected a QR")
        self.assertEqual(f.phase, Phase.ISSUING)

    def test_write_failure_shows_a_failure_and_recovers(self):
        f = flow()
        see(f, CAN, 3)
        tx = f.confirm(1)[0].tx_id
        self.assertEqual(kinds(f.on_issue_failed(tx, "network down", 1.5)), ["Sound"])
        self.assertEqual(f.phase, Phase.FAILED)
        self.assertEqual(f.failure.kind, "issue_failed")
        self.assertEqual(f.dismiss(2), [])
        self.assertEqual(f.phase, Phase.READY)

    def test_label_is_forgotten_after_the_item_leaves(self):
        f = flow()
        see(f, CAN, 3)
        self.assertEqual(f.phase, Phase.DETECTED)
        see(f, None, 16, t0=1)
        self.assertEqual(f.phase, Phase.READY)
        self.assertIsNone(f.current)

    def test_majority_vote_and_one_beep_per_new_label(self):
        f = flow()
        sounds = see(f, CAN, 4) + see(f, PEEL, 2, t0=1)
        self.assertEqual(f.current.label, "can", "two frames of another item must not flip a 4-frame majority")
        self.assertEqual(sum(isinstance(a, Sound) for a in sounds), 1)


class CameraSelfServiceTests(unittest.TestCase):
    """Camera mode: no staff, no sorting hardware; the item leaving the view counts as the drop."""

    def test_hold_then_drop_issues_a_code_without_any_key(self):
        f = flow("camera")
        self.assertEqual(see(f, CAN, 7), [Sound("detect")], "no servo command in camera mode")
        self.assertEqual(f.phase, Phase.DROPPING)
        self.assertEqual(see(f, None, 7, t0=1), [], "7 empty frames is not yet a drop")
        a = see(f, None, 1, t0=2)
        self.assertEqual(kinds(a), ["IssueToken"])
        self.assertTrue(f.tx.sorted)

    def test_a_brief_flicker_is_not_a_drop(self):
        f = flow("camera")
        see(f, CAN, 7)
        a = see(f, None, 5, t0=1) + see(f, CAN, 1, t0=2) + see(f, None, 5, t0=3)
        self.assertNotIn("IssueToken", kinds(a))
        self.assertEqual(f.phase, Phase.DROPPING)

    def test_an_item_that_never_leaves_times_out(self):
        f = flow("camera", drop_timeout=15)
        see(f, CAN, 7)
        see(f, CAN, 100, t0=1, dt=0.1)
        f.tick(16, UTC0)
        self.assertEqual((f.phase, f.failure.kind), (Phase.FAILED, "no_drop_seen"))
        self.assertEqual([s for _, _, s in f.stages()], ["done", "failed", "pending"])

    def test_cool_down_between_codes(self):
        f = flow("camera", cooldown_seconds=15)
        see(f, CAN, 7)
        tx = see(f, None, 8, t0=1)[-1].tx_id
        f.on_issue_ok(tx, "tok", "1234", UTC0 + timedelta(minutes=20), "p", 2.0)
        f.dismiss(3.0)                                  # "Done" straight away
        see(f, None, 10, t0=3.1)                        # re-armed
        self.assertNotIn("SendSort", kinds(see(f, CAN, 7, t0=5)))
        self.assertEqual(f.phase, Phase.DETECTED, "a second code within 15 s must wait")
        self.assertAlmostEqual(f.cooldown_left(5.3), 11.7, delta=0.1)
        see(f, CAN, 1, t0=17.1)
        self.assertEqual(f.phase, Phase.DROPPING)

    def test_offline_does_not_start_a_deposit(self):
        f = flow("camera")
        f.set_context(online=False, model_ok=True, sorter_ready=False)
        see(f, CAN, 10)
        self.assertEqual(f.phase, Phase.DETECTED)

    def test_space_is_not_needed_and_does_nothing(self):
        f = flow("camera")
        see(f, CAN, 3)
        self.assertEqual(f.confirm(1), [])


class QrLifetimeTests(unittest.TestCase):
    def _qr(self, **kw):
        f = flow(**kw)
        see(f, CAN, 3)
        tx = f.confirm(1)[0].tx_id
        f.on_issue_ok(tx, "tok", "1234", UTC0 + timedelta(minutes=20), "p", 2)
        return f

    def test_walk_away_returns_to_ready_and_stops_watching(self):
        f = self._qr(qr_display_seconds=180)
        self.assertEqual(f.tick(2 + 179, UTC0), [])
        self.assertEqual(f.tick(2 + 180, UTC0), [StopWatching()])
        self.assertEqual(f.phase, Phase.READY)

    def test_expiry_returns_to_ready_and_stops_watching(self):
        f = self._qr(qr_display_seconds=10_000)
        self.assertEqual(f.tick(3, UTC0 + timedelta(minutes=20)), [StopWatching()])
        self.assertIn("[EXPIRED] QR expired.", f.notices)

    def test_done_button(self):
        f = self._qr()
        self.assertEqual(f.dismiss(3), [StopWatching()])
        self.assertEqual(f.phase, Phase.READY)




# ── the bin: TrayFlow with a fake ESP32 (events), a fake detector (Detection) and a fake token store ──
PEEL_ITEM = Detection("banana peel", "Biodegradable", 0.82, (1, 2, 3, 4))
FORK = Detection("fork", "Residual", 0.77, (1, 2, 3, 4))
CUP = Detection("cup", "Recyclable", 0.88, (1, 2, 3, 4))
PERSON = Detection("person", None, 0.95, (1, 2, 3, 4))


def tray(online=True, **kw):
    kw.setdefault("max_weight_g", {"default": 500, "can": 40, "cup": 60})
    f = TrayFlow(TrayConfig(**kw))
    f.set_context(online=online, model_ok=True, sorter_ready=True)
    f.on_sorter_event(LinkUp(), 0.0)
    return f


def weigh(f, grams, t0, n=5, dt=0.2):
    out = []
    for i in range(n):
        out += f.on_sorter_event(Weight(grams), t0 + i * dt)
    return out


def identify(f, det, t0, n=7, dt=0.05):
    out = []
    for i in range(n):
        out += f.observe(det, t0 + i * dt)
    return out


def to_sorting(f, det, grams, t0=1.0):
    acts = weigh(f, grams, t0)
    assert f.phase == Phase.IDENTIFYING, f.phase
    acts += identify(f, det, t0 + 1.0)
    assert f.phase == Phase.SORTING, f.phase
    return acts


def finish_qr(f, now, token="tok"):
    tx = f.tx.id
    f.on_issue_ok(tx, token, "1234", UTC0 + timedelta(minutes=20), f"ECOBIN:{f.tx.waste_type}:{token}", now)


class TrayHappyPathTests(unittest.TestCase):
    def test_each_bin_end_to_end(self):
        for det, code in ((PEEL_ITEM, "1"), (CAN, "2"), (FORK, "3")):
            with self.subTest(item=det.label):
                f = tray()
                self.assertEqual(f.phase, Phase.READY)
                acts = to_sorting(f, det, 15.0)
                sorts = [a for a in acts if isinstance(a, SendSort)]
                self.assertEqual([(s.code, s.waste_type) for s in sorts], [(code, det.waste_type)])
                self.assertNotIn("IssueToken", kinds(acts))
                self.assertEqual(f.on_sorter_event(Ok(int(code)), 3.0), [])
                weigh(f, 0.0, 3.2, n=2)                              # the tray tipped
                acts = f.on_sorter_event(Done(int(code), 15.0), 3.6)
                self.assertEqual(kinds(acts), ["IssueToken"])
                self.assertEqual(acts[0].waste_type, det.waste_type)
                finish_qr(f, 4.0)
                self.assertEqual(f.phase, Phase.QR)
                self.assertEqual(kinds(f.on_scanned("tok", 5.0)), ["StopWatching", "Sound"])
                f.tick(9.1, UTC0)
                self.assertEqual(f.phase, Phase.READY)

    def test_no_token_is_ever_created_before_done(self):
        f = tray()
        acts = to_sorting(f, CAN, 15.0)
        acts += f.on_sorter_event(Ok(2), 3.0)
        for i in range(15):                                         # weight stream, ticks, frames: no DONE yet
            acts += f.on_sorter_event(Weight(15.0 if i < 5 else 0.0), 3.0 + i * 0.2)
            acts += f.tick(3.0 + i * 0.2, UTC0)
            acts += f.observe(CAN, 3.0 + i * 0.2)
        self.assertNotIn("IssueToken", kinds(acts), "a reward code was created before DONE")
        self.assertEqual(kinds(f.on_sorter_event(Done(2, 15.0), 6.1)), ["IssueToken"])
        self.assertEqual(f.on_sorter_event(Done(2, 15.0), 6.2), [], "a repeated DONE must not issue a second code")

    def test_unsteady_weight_keeps_waiting_and_an_empty_tray_goes_back(self):
        f = tray()
        for i, g in enumerate([12, 30, 8, 25, 11, 29, 9, 27]):
            f.on_sorter_event(Weight(g), 1.0 + i * 0.2)
        self.assertEqual(f.phase, Phase.WEIGHING, "a hand in the slot must not start identifying")
        self.assertTrue(f.hint(1.0 + 6.0))
        weigh(f, 18.0, 3.0)
        self.assertEqual(f.phase, Phase.IDENTIFYING)
        g = tray()
        weigh(g, 9.0, 1.0, n=1)
        weigh(g, 0.4, 1.2, n=1)
        self.assertEqual(g.phase, Phase.WEIGHING, "one low reading while the item bounces is not an empty tray")
        weigh(g, 0.4, 1.4, n=4)
        self.assertEqual(g.phase, Phase.READY)

    def test_identify_timeout_with_few_frames_still_votes(self):
        f = tray()
        weigh(f, 12.0, 1.0)
        identify(f, CAN, 2.0, n=3)
        self.assertEqual(f.phase, Phase.IDENTIFYING)
        acts = f.tick(2.0 + 3.0, UTC0)
        self.assertEqual([a.code for a in acts if isinstance(a, SendSort)], ["2"])


class TrayRejectTests(unittest.TestCase):
    def _rejected(self, f, code=3):
        self.assertEqual(f.tx.bin_code, str(code))
        acts = f.on_sorter_event(Ok(code), 3.0) + f.on_sorter_event(Done(code, f.tx.grams), 3.5)
        self.assertNotIn("IssueToken", kinds(acts))
        self.assertEqual(f.phase, Phase.REJECTED)
        return f.notice[1]

    def test_not_recognised(self):
        f = tray()
        weigh(f, 12.0, 1.0)
        acts = identify(f, None, 2.0)
        self.assertEqual([a.code for a in acts if isinstance(a, SendSort)], ["3"], "unknown items go to Residual")
        self.assertEqual(self._rejected(f), "We couldn't recognise this item. It went to Residual.")
        g = tray()
        to_sorting(g, PERSON, 50.0)                              # a label that is not in the waste map
        self.assertEqual(g.tx.reject[0], "unknown")

    def test_too_heavy(self):
        f = tray()
        to_sorting(f, CUP, 120.0)                                # a cup that still has drink in it
        self.assertEqual(f.tx.reject[0], "too_heavy")
        self.assertIn("Cups must be empty", self._rejected(f))
        g = tray()
        to_sorting(g, CUP, 45.0)
        self.assertIsNone(g.tx.reject)

    def test_two_items(self):
        f = tray()
        weigh(f, 30.0, 1.0)
        two = Detection("can", "Recyclable", 0.9, (1, 2, 3, 4), objects=2)
        for i, d in enumerate([two, CAN, two, CAN, two, CAN, CAN]):
            f.observe(d, 2.0 + i * 0.05)
        self.assertEqual(f.tx.reject[0], "two_items")
        self.assertEqual(self._rejected(f), "One item at a time, please. It went to Residual.")

    def test_rejected_screen_ends_after_a_few_seconds(self):
        f = tray(reject_seconds=4)
        weigh(f, 12.0, 1.0)
        identify(f, None, 2.0)
        self._rejected(f)
        weigh(f, 0.0, 3.6, n=2)
        f.tick(3.5 + 4.1, UTC0)
        self.assertEqual(f.phase, Phase.READY)


class TrayFaultTests(unittest.TestCase):
    def test_jam_message_then_staff_clear(self):
        f = tray()
        to_sorting(f, CAN, 15.0)
        f.on_sorter_event(Ok(2), 3.0)
        self.assertNotIn("IssueToken", kinds(f.on_sorter_event(Jam(2), 6.0)))
        self.assertEqual(f.phase, Phase.JAM)
        self.assertEqual([s for _, _, s in f.stages()], ["done", "done", "failed", "pending"])
        weigh(f, 15.0, 6.2, n=2)                                 # still on the tray
        self.assertEqual(f.staff_clear(7.0), [])
        self.assertEqual(f.phase, Phase.WEIGHING, "an item still on the tray is weighed again, not dropped")

    def test_jam_ends_after_the_scale_reads_empty_for_10_s(self):
        f = tray()
        to_sorting(f, CAN, 15.0)
        f.on_sorter_event(Jam(2), 3.0)
        weigh(f, 0.5, 4.0, n=1)
        f.tick(13.9, UTC0)
        self.assertEqual(f.phase, Phase.JAM)
        weigh(f, 0.5, 14.0, n=1)
        f.tick(14.1, UTC0)
        self.assertEqual(f.phase, Phase.READY)

    def test_no_ok_and_no_done_are_jams(self):
        f = tray(ok_timeout_s=3)
        to_sorting(f, CAN, 15.0)
        sent = f.since                                           # the sort command went out here
        f.tick(sent + 2.9, UTC0)
        self.assertEqual(f.phase, Phase.SORTING)
        f.tick(sent + 3.1, UTC0)
        self.assertEqual(f.phase, Phase.JAM)
        g = tray(drop_timeout_s=4)
        to_sorting(g, CAN, 15.0)
        g.on_sorter_event(Ok(2), 3.0)
        g.tick(7.1, UTC0)
        self.assertEqual(g.phase, Phase.JAM)

    def test_wrong_bin_reported_is_a_jam(self):
        f = tray()
        to_sorting(f, CAN, 15.0)
        f.on_sorter_event(Ok(1), 3.0)
        self.assertEqual(f.phase, Phase.JAM)

    def test_esp32_disconnects_during_sorting(self):
        f = tray()
        to_sorting(f, CAN, 15.0)
        f.on_sorter_event(Ok(2), 3.0)
        f.on_sorter_event(LinkLost("unplugged"), 3.2)
        self.assertEqual((f.phase, f.pause_reason), (Phase.PAUSED, "link"))
        self.assertIsNone(f.tx)
        self.assertEqual(f.on_sorter_event(Done(2, 15.0), 3.4), [], "a late DONE after the loss issues nothing")
        weigh(f, 15.0, 5.0, n=1)                                  # the item never left the tray
        f.on_sorter_event(LinkUp(), 6.0)
        self.assertEqual(f.phase, Phase.WEIGHING, "back in service: the item is weighed and sorted again")

    def test_out_of_service_until_the_esp32_answers(self):
        f = TrayFlow(TrayConfig())
        self.assertEqual((f.phase, f.pause_reason), (Phase.PAUSED, "link"))
        weigh(f, 20.0, 1.0)
        self.assertEqual(f.phase, Phase.PAUSED)
        f.on_sorter_event(LinkUp(), 2.0)
        self.assertEqual(f.phase, Phase.WEIGHING)

    def test_bin_full_pauses_then_resumes(self):
        f = tray()
        f.on_sorter_event(Fill(92, 40, 10), 1.0)
        self.assertEqual(f.bins_warn(), ["Biodegradable"])
        self.assertEqual(f.phase, Phase.READY)
        f.on_sorter_event(Fill(100, 40, 10), 2.0)
        self.assertEqual((f.phase, f.pause_reason), (Phase.PAUSED, "bin_full"))
        weigh(f, 20.0, 3.0)
        self.assertEqual(f.phase, Phase.PAUSED, "no items are accepted while a bin is full")
        weigh(f, 0.0, 4.0)
        f.on_sorter_event(Fill(5, 40, 10), 5.0)
        self.assertEqual(f.phase, Phase.READY)

    def test_a_bin_filling_up_during_a_deposit_pauses_afterwards(self):
        f = tray()
        to_sorting(f, CAN, 15.0)
        f.on_sorter_event(Ok(2), 3.0)
        f.on_sorter_event(Done(2, 15.0), 3.5)
        f.on_sorter_event(Fill(30, 100, 10), 3.6)
        self.assertEqual(f.phase, Phase.ISSUING, "the customer still gets their code")
        finish_qr(f, 4.0)
        weigh(f, 0.0, 4.0, n=2)
        f.dismiss(5.0)
        self.assertEqual((f.phase, f.pause_reason), (Phase.PAUSED, "bin_full"))

    def test_firebase_offline_sorts_but_shows_no_qr(self):
        f = tray(online=False)
        to_sorting(f, CAN, 15.0)
        f.on_sorter_event(Ok(2), 3.0)
        self.assertNotIn("IssueToken", kinds(f.on_sorter_event(Done(2, 15.0), 3.5)))
        self.assertEqual((f.phase, f.notice), (Phase.NOTICE, ("Sorted!", "Reward codes are offline right now.")))
        weigh(f, 0.0, 3.6, n=2)
        f.tick(3.5 + 3.1, UTC0)
        self.assertEqual(f.phase, Phase.READY)

    def test_items_that_earn_no_points(self):
        f = tray(points={"Biodegradable": 15, "Recyclable": 10, "Residual": 0})
        to_sorting(f, FORK, 10.0)
        f.on_sorter_event(Ok(3), 3.0)
        f.on_sorter_event(Done(3, 10.0), 3.5)
        self.assertEqual(f.notice, ("Thank you!", "This item doesn't earn points."))

    def test_customer_walks_away_from_the_qr(self):
        f = tray(qr_display_seconds=180)
        to_sorting(f, CAN, 15.0)
        f.on_sorter_event(Ok(2), 3.0)
        f.on_sorter_event(Done(2, 15.0), 3.5)
        weigh(f, 0.0, 3.6, n=2)
        finish_qr(f, 4.0)
        self.assertEqual(f.tick(4.0 + 179, UTC0), [])
        self.assertEqual(f.tick(4.0 + 180, UTC0), [StopWatching()])
        self.assertEqual(f.phase, Phase.READY)

    def test_a_new_item_dropped_during_the_qr_is_weighed_afterwards(self):
        f = tray()
        to_sorting(f, CAN, 15.0)
        f.on_sorter_event(Ok(2), 3.0)
        f.on_sorter_event(Done(2, 15.0), 3.5)
        finish_qr(f, 4.0)
        weigh(f, 22.0, 5.0)                                      # the next customer drops an item
        self.assertEqual(f.phase, Phase.QR, "the first customer's QR stays on screen")
        f.dismiss(6.0)
        self.assertEqual(f.phase, Phase.WEIGHING)

    def test_auto_tare_only_when_idle_steady_and_near_zero(self):
        f = tray(auto_tare_idle_s=60)
        weigh(f, 1.2, 1.0)
        self.assertEqual(f.tick(30.0, UTC0), [])
        weigh(f, 1.2, 60.0)
        self.assertEqual(f.tick(61.0, UTC0), [Tare()])
        weigh(f, 1.2, 61.2)
        self.assertEqual(f.tick(62.0, UTC0), [], "at most one tare per minute")
        g = tray(auto_tare_idle_s=60)
        for i, w in enumerate([0.5, 2.5, 0.3, 2.9, 0.4]):
            g.on_sorter_event(Weight(w), 59.0 + i * 0.2)
        self.assertEqual(g.tick(61.0, UTC0), [], "never tare a moving scale")


# ── protocol, serial link, simulated ESP32 ──────────────────────────────────
class ProtocolTests(unittest.TestCase):
    def test_parse_line(self):
        self.assertEqual(parse_line("READY"), Booted())
        self.assertEqual(parse_line("PONG\r"), Pong())
        self.assertEqual(parse_line("W:42.5"), Weight(42.5))
        self.assertEqual(parse_line("W:-0.4"), Weight(-0.4))
        self.assertEqual(parse_line("OK:2"), Ok(2))
        self.assertEqual(parse_line("DONE:3:18.2"), Done(3, 18.2))
        self.assertEqual(parse_line("JAM:1"), Jam(1))
        self.assertEqual(parse_line("FILL:45,22,104"), Fill(45, 22, 100))
        self.assertEqual(parse_line("ERR:servo stalled"), DeviceError("servo stalled"))
        for bad in ("", "OK", "OK:4", "DONE:2", "DONE:x:1", "JAM:", "W:", "W:abc", "FILL:1,2", "FILL:1,2,300",
                    "hello", "x" * 200):
            self.assertIsNone(parse_line(bad), bad)

    def test_weight_tracker_steady(self):
        t = WeightTracker(steady_ms=500, tolerance_g=1.0)
        for i, g in enumerate([10.0, 10.3, 9.8, 10.1]):
            t.add(g, i * 0.2)
        self.assertTrue(t.steady(0.6))
        t.add(14.0, 0.8)
        self.assertFalse(t.steady(0.8))
        self.assertFalse(WeightTracker().steady(0.0))


class FakePort:
    """A scripted serial port: chunks to return from read(), writes recorded, optional failure."""

    def __init__(self, chunks=(), fail_after=None, answer_ping=True):
        self.chunks, self.writes, self.closed = list(chunks), [], False
        self.reads, self.fail_after, self.answer_ping = 0, fail_after, answer_ping
        self.in_waiting = 0

    def read(self, n):
        self.reads += 1
        if self.fail_after is not None and self.reads > self.fail_after:
            raise OSError("device unplugged")
        time.sleep(0.002)
        return self.chunks.pop(0) if self.chunks else b""

    def write(self, data):
        self.writes.append(data)
        if data == b"P\n" and self.answer_ping:
            self.chunks.append(b"PONG\n")

    def close(self):
        self.closed = True


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def drain(s, until, timeout=2.0):
    got = []
    wait_for(lambda: (got.extend(s.poll()) or until(got)), timeout)
    return got


class SerialSorterTests(unittest.TestCase):
    def _sorter(self, port, **kw):
        kw.setdefault("retry_s", 0.05)
        s = SerialSorter("COM9", 115200, opener=lambda p, b: port, list_ports=lambda: [], log=lambda m: None, **kw)
        self.addCleanup(s.close)
        return s

    def test_ready_only_after_pong_and_only_a_ping_on_connect(self):
        port = FakePort(answer_ping=False)
        s = self._sorter(port)
        self.assertTrue(wait_for(lambda: port.writes))
        time.sleep(0.1)
        self.assertFalse(s.ready)
        self.assertTrue(set(port.writes) <= {b"P\n"}, "only pings may be sent before the ESP32 answers")
        self.assertFalse(s.sort("2"), "no sort command before PONG")
        port.chunks.append(b"PONG\n")
        events = drain(s, lambda g: LinkUp() in g)
        self.assertIn(LinkUp(), events)
        self.assertTrue(s.ready)

    def test_lines_split_across_reads_and_commands(self):
        port = FakePort([b"READY\nW:1", b"2.5\nOK", b":2\nDONE:2:12.5\nhello\nFILL:1,2,3\n"])
        s = self._sorter(port)
        events = drain(s, lambda g: any(isinstance(e, Fill) for e in g))
        self.assertEqual([e for e in events if not isinstance(e, (LinkUp, Pong))],
                         [Booted(), Weight(12.5), Ok(2), Done(2, 12.5), Fill(1, 2, 3)])
        self.assertEqual(s.unparsed, 1)
        self.assertEqual(s.weight, 12.5)
        self.assertTrue(wait_for(lambda: s.ready))
        self.assertTrue(s.sort("2") and s.tare())
        self.assertTrue(wait_for(lambda: b"2\n" in port.writes and b"T\n" in port.writes))
        self.assertFalse(s.sort("4"))
        self.assertTrue(all(not line.startswith("< W:") for line in s.recent_events))

    def test_a_silent_esp32_is_lost_and_reconnected(self):
        first = FakePort()
        second = FakePort()
        ports = iter([first, second])
        s = SerialSorter("COM9", 115200, opener=lambda p, b: next(ports), list_ports=lambda: [], log=lambda m: None,
                         retry_s=0.05, silence_s=0.3, ping_wait_s=0.2)
        self.addCleanup(s.close)
        first.answer_ping = True
        self.assertTrue(wait_for(lambda: s.ready))
        first.answer_ping = False                                # it stops talking
        events = drain(s, lambda g: LinkLost in [type(e) for e in g], timeout=3)
        self.assertIn(LinkLost, [type(e) for e in events])
        self.assertTrue(wait_for(lambda: s.ready and second.reads > 0, timeout=3))

    def test_unplug_drops_queued_commands_and_reconnects_quietly(self):
        first = FakePort(fail_after=30)
        second = FakePort()
        plugged_back = threading.Event()
        ports = iter([first, second])

        def opener(p, b):
            port = next(ports)
            if port is second:
                plugged_back.wait(2)           # stay "unplugged" until the stale command is queued
            return port
        logs = []
        s = SerialSorter("COM9", 115200, opener=opener, list_ports=lambda: [], log=logs.append, retry_s=0.05)
        self.addCleanup(s.close)
        self.assertTrue(wait_for(lambda: s.state == LinkState.RECONNECTING))
        s._tx.put(b"1\n")                      # a command left behind while the port was down
        plugged_back.set()
        self.assertTrue(wait_for(lambda: s.ready and second.reads > 2))
        time.sleep(0.05)
        self.assertNotIn(b"1\n", second.writes, "a stale servo command was replayed after reconnecting")
        self.assertLessEqual(sum("reconnecting" in m for m in logs), 2, "reconnect logged on every attempt")

    def test_missing_pyserial_is_a_clear_error_without_retries(self):
        calls = []

        def opener(p, b):
            calls.append(1)
            raise ImportError("No module named 'serial'")
        s = SerialSorter("COM9", 115200, opener=opener, list_ports=lambda: [], log=lambda m: None, retry_s=0.01)
        self.addCleanup(s.close)
        self.assertTrue(wait_for(lambda: s.state == LinkState.ERROR))
        time.sleep(0.1)
        self.assertEqual(len(calls), 1)
        self.assertIn("PySerial", s.detail)

    def test_auto_port_picks_an_esp32_usb_bridge(self):
        port = FakePort()
        s = SerialSorter("auto", 115200, opener=lambda p, b: port,
                         list_ports=lambda: [("COM1", None), ("COM7", 0x10C4)], log=lambda m: None)
        self.addCleanup(s.close)
        self.assertTrue(wait_for(lambda: s.ready))
        self.assertEqual(s.port, "COM7")

    def test_bench_test_item_only_when_connected(self):
        port = FakePort(answer_ping=False)
        s = self._sorter(port)
        self.assertFalse(s.test_item(), "nothing is sent before the ESP32 answers PONG")
        port.chunks.append(b"PONG\n")
        self.assertTrue(wait_for(lambda: s.ready))
        self.assertTrue(s.test_item(20))
        self.assertTrue(wait_for(lambda: b"ITEM 20\n" in port.writes))

    def test_wait_for_helper(self):
        port = FakePort()
        s = self._sorter(port)
        self.assertTrue(wait_for(lambda: s.ready))
        threading.Timer(0.1, lambda: port.chunks.append(b"OK:3\n")).start()
        self.assertEqual(s.wait_for("OK:", 2), "OK:3")
        self.assertIsNone(s.wait_for("DONE:", 0.2))


class SimulatedEsp32Tests(unittest.TestCase):
    def _sim(self):
        device = SimulatedESP32(speed=5.0, rng=random.Random(1))
        s = SerialSorter("SIM", 115200, opener=lambda p, b: device, list_ports=lambda: [], log=lambda m: None,
                         retry_s=0.05, simulated=True)
        s.device = device
        self.addCleanup(s.close)
        self.assertTrue(wait_for(lambda: s.ready))
        return s, device

    def test_a_full_deposit_over_the_real_parser(self):
        s, device = self._sim()
        device.place_item(20.0)
        self.assertTrue(wait_for(lambda: s.weight is not None and abs(s.weight - 20.0) < 1 and s.steady, timeout=3))
        self.assertTrue(s.sort("2"))
        events = drain(s, lambda g: any(isinstance(e, Fill) for e in g), timeout=3)
        kinds_ = [type(e).__name__ for e in events if not isinstance(e, Weight)]
        self.assertEqual([k for k in kinds_ if k in ("Ok", "Done", "Fill")], ["Ok", "Done", "Fill"])
        done = [e for e in events if isinstance(e, Done)][0]
        self.assertEqual(done.bin, 2)
        self.assertAlmostEqual(done.grams, 20.0, delta=1.0)
        self.assertTrue(wait_for(lambda: s.weight is not None and s.weight < 2, timeout=2))
        self.assertTrue(s.simulated)

    def test_jam(self):
        s, device = self._sim()
        device.place_item(15.0)
        device.jam_next = True
        s.sort("1")
        events = drain(s, lambda g: any(isinstance(e, Jam) for e in g), timeout=3)
        self.assertIn(Jam(1), events)

    def test_make_sorter_sim_is_labelled_and_has_the_device(self):
        s = make_sorter("sim", log=lambda m: None)
        self.addCleanup(s.close)
        self.assertTrue(s.simulated)
        self.assertIsInstance(s.device, SimulatedESP32)


class OtherSortersTests(unittest.TestCase):
    def test_attendant_and_camera_modes_have_no_hardware(self):
        for s in (AttendantSorter(), make_sorter("camera")):
            self.assertFalse(s.ready)
            self.assertFalse(s.sort("2"))


class SettingsTests(unittest.TestCase):
    def test_file_command_line_and_old_key_names(self):
        import json
        import tempfile
        d = Path(tempfile.mkdtemp())
        p = d / "s.json"
        p.write_text(json.dumps({"sorter": "camera", "serial_baud": 9600, "width": "1024", "fullscreen": "yes",
                                 "tray_roi": [0.1, 0.2, 0.9, 0.8], "bogus": 1}), "utf-8")
        s, problems = cfgmod.load_settings(p)
        self.assertEqual((s.mode, s.sorter, s.baud, s.width, s.fullscreen), ("camera", "camera", 9600, 1024, False))
        self.assertEqual(s.tray_roi, [0.1, 0.2, 0.9, 0.8])
        self.assertTrue(any("bogus" in x for x in problems) and any("fullscreen" in x for x in problems))
        p.write_text(json.dumps({"tray_roi": [0.5, 0.0, 0.2, 1.0]}), "utf-8")
        s, problems = cfgmod.load_settings(p)
        self.assertEqual(s.tray_roi, [0.0, 0.0, 1.0, 1.0])
        self.assertTrue(any("tray_roi" in x for x in problems))
        s, _ = cfgmod.load_settings(p, ["--no-hardware", "--port", "COM4", "--windowed", "--reduce-motion"])
        self.assertEqual((s.mode, s.serial_port, s.fullscreen, s.reduce_motion), ("sim", "COM4", False, True))
        s, _ = cfgmod.load_settings(p, ["--mode", "attendant"])
        self.assertEqual(s.mode, "attendant")
        s, _ = cfgmod.load_settings(d / "missing.json", None)
        self.assertEqual((s.mode, s.serial_port, s.baud), ("esp32", "COM3", 115200))

    def test_committed_settings_file_is_valid(self):
        s, problems = cfgmod.load_settings(Path(__file__).resolve().parents[1] / "config" / "kiosk_settings.json")
        self.assertEqual(problems, [])
        self.assertEqual((s.mode, s.serial_port, s.baud, s.min_item_g, s.steady_ms, s.identify_timeout_s),
                         ("esp32", "COM3", 115200, 3.0, 500, 3.0))

    def test_rules_have_a_weight_limit_for_every_label(self):
        import json
        root = Path(__file__).resolve().parents[1]
        rules = json.loads((root / "config" / "rules.json").read_text(encoding="utf-8"))
        labels = json.loads((root / "config" / "waste_map.json").read_text(encoding="utf-8"))["map"]
        self.assertEqual(sorted(set(labels) - set(rules["max_weight_g"])), [])
        self.assertEqual(rules["bin_codes"], {"Biodegradable": "1", "Recyclable": "2", "Residual": "3"})
        self.assertEqual((rules["unknown_policy"], rules["bin_warn_pct"], rules["bin_full_pct"]),
                         ("residual_no_code", 90, 100))
        self.assertEqual(rules["points"], {"Biodegradable": 15, "Recyclable": 10, "Residual": 5})
        self.assertEqual(rules["qrExpiryMinutes"], 20)


if __name__ == "__main__":
    unittest.main()
