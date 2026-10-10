"""The sorter: whatever physically confirms that an item went into the right chute.

Three interchangeable implementations with the same small interface (state, send_sort, poll, close):

  AttendantSorter  no hardware; a staff member confirms the deposit with SPACE (the original kiosk flow)
  SerialSorter     ESP32 over USB serial: servo command out, acknowledgement / weight / fill levels in
  SimulatedSorter  scripted ESP32 replies for demos and tests; flagged `simulated` so the UI labels it

ESP32 line protocol (ASSUMPTION — confirm against the firmware; README "ESP32 protocol"):
  kiosk -> ESP32   "1\\n" Biodegradable, "2\\n" Recyclable, "3\\n" Residual
  ESP32 -> kiosk   "OK" or "ACK"          servo reached the chute
                   "W:<grams>"            load-cell reading after the drop, e.g. "W:42.5"
                   "F:<bio>,<rec>,<res>"  fill levels in percent, e.g. "F:45,22,68"
                   "ERR:<text>"           a fault the firmware wants to report
                   anything else          counted as unparsed and shown in diagnostics

Safety rules: no command is ever sent on connect or reconnect; commands queued while disconnected are
dropped (a servo must never move minutes after the customer left); one command is outstanding at a time.
"""
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum

SORT_COMMANDS = {"Biodegradable": b"1\n", "Recyclable": b"2\n", "Residual": b"3\n"}
ESP32_USB_VIDS = {0x10C4, 0x1A86, 0x0403, 0x303A}   # CP210x, CH340, FTDI, Espressif native USB
MAX_LINE = 120


class LinkState(Enum):
    NOT_USED = "not used"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


@dataclass(frozen=True)
class Ack:
    pass


@dataclass(frozen=True)
class Weight:
    grams: float


@dataclass(frozen=True)
class Fill:
    bio: int
    rec: int
    res: int


@dataclass(frozen=True)
class DeviceError:
    message: str


def parse_line(text):
    """One protocol line -> event, or None if it is not a protocol line. Never raises."""
    t = text.strip()
    if not t or len(t) > MAX_LINE:
        return None
    up = t.upper()
    if up in ("OK", "ACK"):
        return Ack()
    if up.startswith("W:"):
        try:
            grams = float(t[2:])
        except ValueError:
            return None
        return Weight(grams) if 0 <= grams < 100_000 else None
    if up.startswith("F:"):
        parts = t[2:].split(",")
        if len(parts) != 3:
            return None
        try:
            levels = [int(round(float(p))) for p in parts]
        except ValueError:
            return None
        if not all(0 <= v <= 100 for v in levels):
            return None
        return Fill(*levels)
    if up.startswith("ERR:"):
        return DeviceError(t[4:].strip()[:80] or "unspecified")
    return None


class Sorter:
    mode = "base"
    simulated = False

    def __init__(self):
        self.state = LinkState.NOT_USED
        self.detail = ""
        self.port = ""
        self.unparsed = 0
        self.recent = deque(maxlen=12)     # last lines in and out, for diagnostics

    @property
    def ready(self):
        return self.state == LinkState.CONNECTED

    def send_sort(self, waste_type):
        return False

    def poll(self, now=None):
        return []

    def close(self):
        pass


class AttendantSorter(Sorter):
    mode = "attendant"

    def __init__(self):
        super().__init__()
        self.detail = "staff confirm with SPACE"


class CameraOnlySorter(Sorter):
    """No sorting hardware; the camera sees the item leave (self-service, see kiosk/flow.py DROPPING)."""
    mode = "camera"

    def __init__(self):
        super().__init__()
        self.detail = "camera confirms the drop"


class SimulatedSorter(Sorter):
    """Replies like a healthy ESP32: OK after `ack_s`, then a weight after `drop_s`. Never real data."""
    mode = "sim"
    simulated = True

    def __init__(self, ack_s=0.6, drop_s=1.6, grams=42.0, clock=time.monotonic):
        super().__init__()
        self.state, self.detail, self.port = LinkState.CONNECTED, "simulated — no hardware", "SIM"
        self._ack_s, self._drop_s, self._grams, self._clock = ack_s, drop_s, grams, clock
        self._due = []

    def send_sort(self, waste_type):
        if waste_type not in SORT_COMMANDS:
            return False
        t = self._clock()
        self._due = [(t + self._ack_s, Ack()), (t + self._drop_s, Weight(self._grams))]
        self.recent.append(f"> {SORT_COMMANDS[waste_type].decode().strip()} (simulated)")
        return True

    def poll(self, now=None):
        now = self._clock() if now is None else now
        out = [e for t, e in self._due if t <= now]
        self._due = [(t, e) for t, e in self._due if t > now]
        for e in out:
            line = "OK" if isinstance(e, Ack) else f"W:{e.grams:g}" if isinstance(e, Weight) else str(e)
            self.recent.append(f"< {line} (simulated)")
        return out


def _default_opener(port, baud):
    import serial                                       # imported here: only esp32 mode needs PySerial
    return serial.Serial(port, baud, timeout=0.1, write_timeout=0.5)


def _default_ports():
    from serial.tools import list_ports
    return [(p.device, p.vid) for p in list_ports.comports()]


class SerialSorter(Sorter):
    mode = "esp32"

    def __init__(self, port="auto", baud=115200, opener=_default_opener, list_ports=_default_ports,
                 log=print, backoff=(1, 2, 4, 8, 10)):
        super().__init__()
        self._want_port, self._baud = port, baud
        self._opener, self._list_ports, self._log, self._backoff = opener, list_ports, log, backoff
        self._events, self._tx = queue.Queue(), queue.Queue()
        self._stop = threading.Event()
        self._set(LinkState.CONNECTING, "opening port")
        self._thread = threading.Thread(target=self._run, daemon=True, name="esp32-serial")
        self._thread.start()

    def _set(self, state, detail):
        if (state, detail) != (self.state, self.detail):
            self.state, self.detail = state, detail
            self._log(f"[SORTER] {state.value}{': ' + detail if detail else ''}")

    def _resolve_port(self):
        if self._want_port.lower() != "auto":
            return self._want_port
        found = [dev for dev, vid in self._list_ports() if vid in ESP32_USB_VIDS]
        if not found:
            raise OSError("no ESP32-style USB serial device found")
        return found[0]

    def _run(self):
        attempt = 0
        while not self._stop.is_set():
            dev = None
            try:
                self.port = self._resolve_port()
                dev = self._opener(self.port, self._baud)
            except ImportError:
                self._set(LinkState.ERROR, "PySerial is not installed (pip install pyserial)")
                return
            except Exception as e:                           # port missing, busy, access denied
                self._set(LinkState.RECONNECTING if attempt else LinkState.CONNECTING, f"{e}"[:80])
                attempt += 1
                self._stop.wait(self._backoff[min(attempt - 1, len(self._backoff) - 1)])
                continue
            self._drain_tx()                                 # never replay stale commands
            attempt = 0
            self._set(LinkState.CONNECTED, self.port)
            try:
                self._pump(dev)
            except Exception as e:
                self._drain_tx()
                self._set(LinkState.RECONNECTING, f"lost {self.port}: {e}"[:80])
                attempt = 1
            finally:
                try:
                    dev.close()
                except Exception:
                    pass
            self._stop.wait(self._backoff[0])

    def _pump(self, dev):
        buf = b""
        while not self._stop.is_set():
            try:
                cmd = self._tx.get_nowait()
                dev.write(cmd)
                self.recent.append(f"> {cmd.decode(errors='replace').strip()}")
            except queue.Empty:
                pass
            chunk = dev.read(getattr(dev, "in_waiting", 0) or 1)
            if not chunk:
                continue
            buf += chunk
            if len(buf) > 1024 and b"\n" not in buf:         # garbage without newlines: drop it
                self.unparsed += 1
                buf = b""
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                text = raw.decode("ascii", errors="replace").strip()
                if not text:
                    continue
                self.recent.append(f"< {text[:40]}")
                event = parse_line(text)
                if event is None:
                    self.unparsed += 1
                else:
                    self._events.put(event)

    def _drain_tx(self):
        while True:
            try:
                self._tx.get_nowait()
            except queue.Empty:
                return

    def send_sort(self, waste_type):
        if not self.ready or waste_type not in SORT_COMMANDS:
            return False
        self._drain_tx()                                     # one outstanding command at a time
        self._tx.put(SORT_COMMANDS[waste_type])
        return True

    def poll(self, now=None):
        out = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)


def make_sorter(mode, port="auto", baud=115200, log=print):
    if mode == "esp32":
        return SerialSorter(port, baud, log=log)
    if mode == "sim":
        return SimulatedSorter()
    if mode == "camera":
        return CameraOnlySorter()
    return AttendantSorter()
