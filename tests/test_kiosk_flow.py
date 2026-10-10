# -*- coding: utf-8 -*-
"""The deposit state machine (kiosk/flow.py) and the sorter link (kiosk/hardware.py), without hardware.

    py -3.11 -I -m unittest discover -s tests -v
"""
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kiosk import config as cfgmod                                          # noqa: E402
from kiosk.flow import (Detection, FlowConfig, IssueToken, Phase, SendSort, Sound, StopWatching,  # noqa: E402
                        TransactionFlow, WatchToken)
from kiosk.hardware import (Ack, AttendantSorter, DeviceError, Fill, LinkState, SerialSorter,  # noqa: E402
                            SimulatedSorter, Weight, parse_line)

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


class AutomaticFlowTests(unittest.TestCase):
    def test_full_sort_and_two_deposits_in_a_row(self):
        f = flow("esp32")
        for n in (1, 2):
            a = see(f, CAN, 7, t0=n * 100)
            sends = [x for x in a if isinstance(x, SendSort)]
            self.assertEqual(len(sends), 1, f"deposit {n}: exactly one servo command")
            self.assertEqual(sends[0].waste_type, "Recyclable")
            self.assertEqual(f.phase, Phase.SORTING)
            self.assertEqual(see(f, CAN, 3, t0=n * 100 + 1), [], "frames during sorting must not send again")
            f.on_sorter_event(Ack(), n * 100 + 1)
            self.assertEqual(f.phase, Phase.CONFIRMING)
            a = f.on_sorter_event(Weight(40), n * 100 + 2)
            self.assertEqual(kinds(a), ["IssueToken"])
            self.assertTrue(f.tx.sorted)
            f.on_issue_ok(a[0].tx_id, f"tok-{n}", "1234", UTC0 + timedelta(minutes=20), "p", n * 100 + 3)
            f.on_scanned(f"tok-{n}", n * 100 + 4)
            f.tick(n * 100 + 9, UTC0)
            self.assertEqual(f.phase, Phase.READY)
            see(f, None, 10, t0=n * 100 + 10)          # the item is gone: re-arm
        self.assertEqual(f.completed, 2)

    def test_same_item_still_in_view_does_not_sort_again(self):
        f = flow("esp32")
        see(f, CAN, 7)
        f.tick(10, UTC0)                                 # sorter never answered
        self.assertEqual(f.phase, Phase.FAILED)
        f.tick(20, UTC0)
        self.assertEqual(f.phase, Phase.READY)
        self.assertFalse([a for a in see(f, CAN, 14, t0=21) if isinstance(a, SendSort)],
                         "an item that never left the camera started a second sort")
        self.assertFalse(f.armed)
        see(f, None, 10, t0=30)
        self.assertTrue(f.armed)

    def test_no_sort_until_the_vote_is_stable(self):
        f = flow("esp32", lock_votes=5)
        a = []
        for i, d in enumerate([CAN, PEEL, CAN, PEEL, CAN, PEEL, CAN]):
            a += f.observe(d, i * 0.04)
        self.assertFalse([x for x in a if isinstance(x, SendSort)], "4 of 7 frames is not a stable vote")

    def test_no_sort_while_the_sorter_is_disconnected(self):
        f = flow("esp32")
        f.set_context(online=True, model_ok=True, sorter_ready=False)
        self.assertFalse([x for x in see(f, CAN, 10) if isinstance(x, SendSort)])
        f.set_context(online=True, model_ok=True, sorter_ready=True)
        self.assertTrue([x for x in see(f, CAN, 1, t0=1) if isinstance(x, SendSort)], "sorts once it reconnects")

    def test_light_drop_fails_and_repeated_lines_are_ignored(self):
        f = flow("esp32", min_drop_grams=5)
        see(f, CAN, 7)
        f.on_sorter_event(Ack(), 1)
        self.assertEqual(f.on_sorter_event(Ack(), 1.1), [], "a repeated OK changes nothing")
        self.assertEqual(f.phase, Phase.CONFIRMING)
        f.on_sorter_event(Weight(2), 2)
        self.assertEqual((f.phase, f.failure.kind), (Phase.FAILED, "no_drop"))
        self.assertEqual(f.on_sorter_event(Weight(50), 2.1), [], "a late weight cannot issue a code")
        self.assertEqual([s for _, _, s in f.stages()], ["done", "done", "failed", "pending"])

    def test_offline_still_sorts_but_says_no_code(self):
        f = flow("esp32")
        f.set_context(online=False, model_ok=True, sorter_ready=True)
        see(f, CAN, 7)
        self.assertEqual(f.phase, Phase.SORTING)
        f.on_sorter_event(Ack(), 1)
        self.assertNotIn("IssueToken", kinds(f.on_sorter_event(Weight(30), 2)))
        self.assertEqual(f.failure.kind, "offline_after_sort")
        self.assertIn("Recyclable bin", f.failure.message)

    def test_device_error_and_ack_timeout(self):
        f = flow("esp32", sort_ack_timeout=5)
        see(f, CAN, 7)
        f.on_sorter_event(DeviceError("jammed"), 1)
        self.assertEqual(f.failure.kind, "device_error")
        g = flow("esp32", sort_ack_timeout=5)
        see(g, CAN, 7)
        g.tick(4.9, UTC0)
        self.assertEqual(g.phase, Phase.SORTING)
        g.tick(5.3, UTC0)
        self.assertEqual(g.failure.kind, "sorter_timeout")

    def test_space_starts_a_sort_only_in_debug(self):
        f = flow("esp32")
        see(f, CAN, 2)
        self.assertEqual(f.confirm(1), [])
        g = flow("esp32", debug=True)
        see(g, CAN, 2)
        self.assertEqual(kinds(g.confirm(1)), ["SendSort"])


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


class ProtocolTests(unittest.TestCase):
    def test_parse_line(self):
        self.assertEqual(parse_line("OK"), Ack())
        self.assertEqual(parse_line(" ack\r"), Ack())
        self.assertEqual(parse_line("W:42.5"), Weight(42.5))
        self.assertEqual(parse_line("F:45,22,68"), Fill(45, 22, 68))
        self.assertEqual(parse_line("ERR:servo stalled"), DeviceError("servo stalled"))
        for bad in ("", "W:", "W:abc", "W:-3", "F:1,2", "F:1,2,300", "F:a,b,c", "hello", "x" * 200):
            self.assertIsNone(parse_line(bad), bad)


class FakePort:
    """A scripted serial port: chunks to return from read(), writes recorded, optional failure."""

    def __init__(self, chunks=(), fail_after=None):
        self.chunks, self.writes, self.closed = list(chunks), [], False
        self.reads, self.fail_after = 0, fail_after
        self.in_waiting = 0

    def read(self, n):
        self.reads += 1
        if self.fail_after is not None and self.reads > self.fail_after:
            raise OSError("device unplugged")
        time.sleep(0.002)
        return self.chunks.pop(0) if self.chunks else b""

    def write(self, data):
        self.writes.append(data)

    def close(self):
        self.closed = True


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class SerialSorterTests(unittest.TestCase):
    def _sorter(self, ports, **kw):
        it = iter(ports)
        logs = []
        s = SerialSorter("COM9", 115200, opener=lambda p, b: next(it), list_ports=lambda: [],
                         log=logs.append, backoff=(0.05,), **kw)
        self.addCleanup(s.close)
        return s, logs

    def test_connects_without_sending_and_parses_split_lines(self):
        port = FakePort([b"O", b"K\nW:4", b"0\nhello\n", b"F:10,20,30\n"])
        s, logs = self._sorter([port])
        self.assertTrue(wait_for(lambda: s.ready))
        self.assertEqual(port.writes, [], "nothing may be sent to the ESP32 on connect")
        events = []
        self.assertTrue(wait_for(lambda: (events.extend(s.poll()) or len(events) >= 3)))
        self.assertEqual(events[:3], [Ack(), Weight(40.0), Fill(10, 20, 30)])
        self.assertEqual(s.unparsed, 1)
        self.assertTrue(s.send_sort("Recyclable"))
        self.assertTrue(wait_for(lambda: port.writes == [b"2\n"]))
        self.assertFalse(s.send_sort("Plastic"), "unknown categories are never sent")

    def test_unplug_drops_queued_commands_and_reconnects_quietly(self):
        first = FakePort(fail_after=3)
        second = FakePort()
        plugged_back = threading.Event()
        ports = iter([first, second])

        def opener(p, b):
            port = next(ports)
            if port is second:
                plugged_back.wait(2)           # stay "unplugged" until the stale command is queued
            return port
        logs = []
        s = SerialSorter("COM9", 115200, opener=opener, list_ports=lambda: [], log=logs.append, backoff=(0.05,))
        self.addCleanup(s.close)
        self.assertTrue(wait_for(lambda: s.state == LinkState.RECONNECTING))
        s._tx.put(b"1\n")                      # a command left behind while the port was down
        plugged_back.set()
        self.assertTrue(wait_for(lambda: s.ready and second.reads > 2))
        time.sleep(0.05)
        self.assertEqual(second.writes, [], "a stale servo command was replayed after reconnecting")
        self.assertLessEqual(sum("reconnecting" in m for m in logs), 2, "reconnect logged on every attempt")

    def test_send_is_refused_while_disconnected(self):
        def opener(p, b):
            raise OSError("could not open port COM9")
        s = SerialSorter("COM9", 115200, opener=opener, list_ports=lambda: [], log=lambda m: None, backoff=(0.05,))
        self.addCleanup(s.close)
        self.assertTrue(wait_for(lambda: s.state in (LinkState.CONNECTING, LinkState.RECONNECTING)))
        self.assertFalse(s.send_sort("Residual"))

    def test_missing_pyserial_is_a_clear_error_without_retries(self):
        calls = []

        def opener(p, b):
            calls.append(1)
            raise ImportError("No module named 'serial'")
        s = SerialSorter("COM9", 115200, opener=opener, list_ports=lambda: [], log=lambda m: None, backoff=(0.01,))
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


class OtherSortersTests(unittest.TestCase):
    def test_simulated_sorter_is_labelled_and_scripted(self):
        clock = [0.0]
        s = SimulatedSorter(ack_s=0.5, drop_s=1.0, clock=lambda: clock[0])
        self.assertTrue(s.simulated and s.ready)
        self.assertTrue(s.send_sort("Biodegradable"))
        self.assertEqual(s.poll(0.4), [])
        self.assertEqual(s.poll(0.6), [Ack()])
        self.assertEqual(s.poll(1.2), [Weight(42.0)])

    def test_attendant_mode_has_no_hardware(self):
        s = AttendantSorter()
        self.assertFalse(s.ready)
        self.assertFalse(s.send_sort("Recyclable"))


class SettingsTests(unittest.TestCase):
    def test_file_and_command_line(self):
        import json
        import tempfile
        d = Path(tempfile.mkdtemp())
        p = d / "s.json"
        p.write_text(json.dumps({"sorter": "esp32", "width": "1024", "fullscreen": "yes", "bogus": 1}), "utf-8")
        s, problems = cfgmod.load_settings(p)
        self.assertEqual((s.sorter, s.width, s.fullscreen), ("esp32", 1024, False))
        self.assertTrue(any("bogus" in x for x in problems) and any("fullscreen" in x for x in problems))
        s, _ = cfgmod.load_settings(p, ["--no-hardware", "--port", "COM4", "--fullscreen", "--reduce-motion"])
        self.assertEqual((s.sorter, s.serial_port, s.fullscreen, s.reduce_motion), ("sim", "COM4", True, True))
        s, _ = cfgmod.load_settings(d / "missing.json", None)
        self.assertEqual(s.sorter, "attendant", "tests and first runs default to the original flow")

    def test_committed_settings_file_is_valid(self):
        s, problems = cfgmod.load_settings(Path(__file__).resolve().parents[1] / "config" / "kiosk_settings.json")
        self.assertEqual(problems, [])
        self.assertEqual(s.sorter, "attendant")


if __name__ == "__main__":
    unittest.main()
