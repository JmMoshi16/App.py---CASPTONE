"""The sorter link: whatever physically weighs, sorts and confirms a deposit.

  AttendantSorter   no hardware; a staff member confirms with SPACE (attendant mode)
  CameraOnlySorter  no hardware; the camera sees the item leave (camera mode)
  SerialSorter      the ESP32 on USB serial (esp32 mode), or the SimulatedESP32 below (sim mode)

ESP32 line protocol (ASCII, one message per line, "\\n"). ASSUMED — confirm against the firmware:
  app -> ESP32   "1" / "2" / "3"     sort into Biodegradable / Recyclable / Residual (rules.json bin_codes)
                 "T"                 tare the scale
                 "P"                 ping
  ESP32 -> app   READY               booted, chute at home
                 W:<grams>           weight, about every 200 ms
                 OK:<n>              chute points at bin n
                 DONE:<n>:<grams>    the item left the tray into bin n (grams = weight before tipping)
                 JAM:<n>             the tray tipped but the weight did not drop
                 FILL:<b>,<r>,<s>    fill percent of bins 1, 2, 3
                 PONG / ERR:<text>

Safety rules: the link is "ready" only after the ESP32 answered PONG; nothing but a ping is sent on
(re)connect; commands queued while disconnected are dropped (a servo must never move minutes later); one
sort command is outstanding at a time; a silent ESP32 (no line for 3 s, no answer to a ping) counts as lost.
"""
import queue
import random
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum

DEFAULT_BIN_CODES = {"Biodegradable": "1", "Recyclable": "2", "Residual": "3"}
BIN_NAMES = {1: "Biodegradable", 2: "Recyclable", 3: "Residual"}
ESP32_USB_VIDS = {0x10C4, 0x1A86, 0x0403, 0x303A}   # CP210x, CH340, FTDI, Espressif native USB
MAX_LINE = 120


class LinkState(Enum):
    NOT_USED = "not used"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


# ── protocol events ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Booted:
    pass


@dataclass(frozen=True)
class Weight:
    grams: float


@dataclass(frozen=True)
class Ok:
    bin: int


@dataclass(frozen=True)
class Done:
    bin: int
    grams: float


@dataclass(frozen=True)
class Jam:
    bin: int


@dataclass(frozen=True)
class Fill:
    bio: int
    rec: int
    res: int


@dataclass(frozen=True)
class Pong:
    pass


@dataclass(frozen=True)
class DeviceError:
    message: str


# Not protocol lines: produced by the link itself.
@dataclass(frozen=True)
class LinkUp:
    pass


@dataclass(frozen=True)
class LinkLost:
    reason: str


def _bin(text):
    n = int(text)
    if n not in BIN_NAMES:
        raise ValueError(n)
    return n


def parse_line(text):
    """One protocol line -> event, or None if it is not a protocol line. Never raises."""
    t = text.strip()
    if not t or len(t) > MAX_LINE:
        return None
    up = t.upper()
    try:
        if up == "READY":
            return Booted()
        if up == "PONG":
            return Pong()
        if up.startswith("W:"):
            g = float(t[2:])
            return Weight(g) if -5000 < g < 100_000 else None
        if up.startswith("OK:"):
            return Ok(_bin(t[3:]))
        if up.startswith("DONE:"):
            n, g = t[5:].split(":")
            return Done(_bin(n), float(g))
        if up.startswith("JAM:"):
            return Jam(_bin(t[4:]))
        if up.startswith("FILL:"):
            parts = [float(p) for p in t[5:].split(",")]
            if len(parts) != 3 or not all(0 <= v <= 150 for v in parts):
                return None
            return Fill(*(min(100, int(round(v))) for v in parts))
        if up.startswith("ERR:"):
            return DeviceError(t[4:].strip()[:80] or "unspecified")
    except (ValueError, TypeError):
        return None
    return None


class WeightTracker:
    """Latest weight and whether it has stayed within `tolerance_g` for `steady_ms`."""

    def __init__(self, steady_ms=500, tolerance_g=1.0, stale_s=1.0):
        self.steady_s, self.tolerance, self.stale_s = steady_ms / 1000.0, tolerance_g, stale_s
        self.samples = deque()
        self.latest = None
        self.latest_t = None

    def add(self, grams, t):
        self.latest, self.latest_t = float(grams), t
        self.samples.append((t, float(grams)))
        while self.samples and self.samples[0][0] < t - self.steady_s - 0.25:
            self.samples.popleft()

    def steady(self, now=None):
        if len(self.samples) < 2:
            return False
        if now is not None and now - self.latest_t > self.stale_s:
            return False
        span = self.samples[-1][0] - self.samples[0][0]
        values = [g for _, g in self.samples]
        return span >= self.steady_s * 0.9 and max(values) - min(values) <= self.tolerance

    def reset(self):
        self.samples.clear()
        self.latest = self.latest_t = None


# ── sorters ───────────────────────────────────────────────────────────────────
class Sorter:
    mode = "base"
    simulated = False

    def __init__(self):
        self.state = LinkState.NOT_USED
        self.detail = ""
        self.port = ""
        self.unparsed = 0
        self.recent = deque(maxlen=20)          # last 20 serial lines, in and out
        self.recent_events = deque(maxlen=20)   # the same without the W: weight stream
        self.tracker = WeightTracker()
        self.device = None

    @property
    def ready(self):
        return self.state == LinkState.CONNECTED

    @property
    def weight(self):
        return self.tracker.latest

    @property
    def steady(self):
        return self.tracker.steady(time.monotonic())

    def sort(self, code):
        return False

    def tare(self):
        return False

    def ping(self):
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
    """No sorting hardware; the camera sees the item leave (camera mode)."""
    mode = "camera"

    def __init__(self):
        super().__init__()
        self.detail = "camera confirms the drop"


def _default_opener(port, baud):
    import serial                                       # only esp32 mode needs PySerial
    return serial.Serial(port, baud, timeout=0.1, write_timeout=0.5)


def _default_ports():
    from serial.tools import list_ports
    return [(p.device, p.vid) for p in list_ports.comports()]


class SerialSorter(Sorter):
    mode = "esp32"

    def __init__(self, port="COM3", baud=115200, opener=_default_opener, list_ports=_default_ports, log=print,
                 retry_s=2.0, silence_s=3.0, ping_wait_s=2.0, steady_ms=500, tolerance_g=1.0,
                 simulated=False, clock=time.monotonic):
        super().__init__()
        self.simulated = simulated
        if simulated:
            self.mode = "sim"
        self._want_port, self._baud = str(port), baud
        self._opener, self._list_ports, self._log = opener, list_ports, log
        self._retry_s, self._silence_s, self._ping_wait_s, self._clock = retry_s, silence_s, ping_wait_s, clock
        self.tracker = WeightTracker(steady_ms, tolerance_g)
        self._lock = threading.Lock()
        self._lines = threading.Condition(self._lock)
        self._line_log = deque(maxlen=200)              # (time, line) for wait_for()
        self._events, self._tx = queue.Queue(), queue.Queue()
        self._stop = threading.Event()
        self._alive = False
        self._set(LinkState.CONNECTING, "opening port")
        self._thread = threading.Thread(target=self._run, daemon=True, name="esp32-serial")
        self._thread.start()

    @property
    def steady(self):
        with self._lock:
            return self.tracker.steady(self._clock())

    @property
    def weight(self):
        with self._lock:
            return self.tracker.latest

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
                v = f"{sys.version_info.major}.{sys.version_info.minor}"
                self._set(LinkState.ERROR, f"PySerial is not installed for Python {v} (py -{v} -m pip install pyserial)")
                return
            except Exception as e:                           # port missing, busy, access denied
                self._set(LinkState.RECONNECTING if attempt else LinkState.CONNECTING, f"{e}"[:80])
                attempt += 1
                self._stop.wait(self._retry_s)
                continue
            self._drain_tx()                                 # never replay stale commands
            self._set(LinkState.CONNECTING, f"{self.port}: waiting for PONG")
            try:
                self._pump(dev)
            except Exception as e:
                self._drain_tx()
                if self._alive:
                    self._events.put(LinkLost(f"{e}"[:80]))
                self._alive = False
                self._set(LinkState.RECONNECTING, f"lost {self.port}: {e}"[:80])
                attempt = 1
            finally:
                try:
                    dev.close()
                except Exception:
                    pass
            self._stop.wait(self._retry_s)

    def _write(self, dev, data):
        dev.write(data)
        text = data.decode(errors="replace").strip()
        with self._lock:
            self.recent.append(f"> {text}")
            self.recent_events.append(f"> {text}")

    def _pump(self, dev):
        buf = b""
        now = self._clock()
        last_rx, ping_t = now, now
        self._write(dev, b"P\n")
        while not self._stop.is_set():
            now = self._clock()
            try:
                cmd = self._tx.get_nowait()
                self._write(dev, cmd)
            except queue.Empty:
                pass
            if not self._alive and now - ping_t >= self._retry_s:       # keep asking until it answers
                self._write(dev, b"P\n")
                ping_t = now
            if self._alive and now - last_rx >= self._silence_s:        # silent: ping, then give up
                if ping_t < last_rx:
                    self._write(dev, b"P\n")
                    ping_t = now
                elif now - ping_t >= self._ping_wait_s:
                    raise OSError("the ESP32 stopped answering")
            chunk = dev.read(getattr(dev, "in_waiting", 0) or 1)
            if not chunk:
                continue
            last_rx = now
            buf += chunk
            if len(buf) > 1024 and b"\n" not in buf:         # garbage without newlines: drop it
                self.unparsed += 1
                buf = b""
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                text = raw.decode("ascii", errors="replace").strip()
                if text:
                    self._handle(text, now)

    def _handle(self, text, now):
        event = parse_line(text)
        with self._lines:
            self.recent.append(f"< {text[:40]}")
            if not isinstance(event, Weight):
                self.recent_events.append(f"< {text[:40]}")
            self._line_log.append((now, text))
            if isinstance(event, Weight):
                self.tracker.add(event.grams, now)
            self._lines.notify_all()
        if event is None:
            self.unparsed += 1
            return
        if isinstance(event, Pong) and not self._alive:
            self._alive = True
            self._set(LinkState.CONNECTED, self.port)
            self._events.put(LinkUp())
        self._events.put(event)

    def _drain_tx(self):
        while True:
            try:
                self._tx.get_nowait()
            except queue.Empty:
                return

    # ── commands ──────────────────────────────────────────────────────────
    def sort(self, code):
        code = str(code)
        if not self.ready or code not in ("1", "2", "3"):
            return False
        self._drain_tx()                                     # one outstanding command at a time
        self._tx.put(f"{code}\n".encode())
        return True

    def tare(self):
        if not self.ready:
            return False
        self._tx.put(b"T\n")
        return True

    def ping(self):
        self._tx.put(b"P\n")
        return True

    def test_item(self, grams=20.0):
        """Bench testing only: ask firmware built with HAS_SCALE = false to pretend an item lies on the tray."""
        if not self.ready:
            return False
        self._tx.put(f"ITEM {grams:g}\n".encode())
        return True

    def wait_for(self, prefix, timeout):
        """Block until a line starting with `prefix` arrives (after this call) or `timeout` passes.
        For tools and bench tests; the kiosk loop never blocks on the ESP32."""
        start = self._clock()
        with self._lines:
            while True:
                for t, line in reversed(self._line_log):
                    if t < start:
                        break
                    if line.upper().startswith(prefix.upper()):
                        return line
                left = timeout - (self._clock() - start)
                if left <= 0:
                    return None
                self._lines.wait(min(left, 0.1))

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


# ── simulated ESP32 (sim mode): a fake serial port that runs the firmware behaviour ──
class SimulatedESP32:
    """Speaks the protocol above with realistic timing, a noisy scale and fill sensors. It is a stand-in for
    the serial port, so sim mode exercises the same SerialSorter parser and timeouts as the real bin.
    place_item() puts an item on the tray (staff key I); jam_next makes the next tip fail."""

    def __init__(self, clock=time.monotonic, rng=None, speed=1.0, fill=(35, 50, 20)):
        self.clock, self.rng, self.k = clock, rng or random.Random(), 1.0 / speed
        self._lock = threading.Lock()
        self.raw = 0.0                     # grams actually on the tray
        self.offset = 0.0                  # tare
        self.fill = list(fill)
        self.jam_next = False
        self.closed = False
        self._out = []                     # (due time, line)
        self._next_w = 0.0
        self._settle = None                # (until, target) while a hand drops the item
        self._busy = False
        self._buf = b""
        self.in_waiting = 0
        self._emit(0.05, "READY")

    def _emit(self, delay, line):
        self._out.append((self.clock() + delay * self.k, line))

    def place_item(self, grams=None):
        with self._lock:
            g = grams if grams is not None else round(self.rng.uniform(8, 45), 1)
            self._settle = (self.clock() + 0.4 * self.k, self.raw + g)
            return g

    def _weight_now(self, now):
        if self._settle is not None:
            until, target = self._settle
            if now >= until:
                self.raw, self._settle = target, None
            else:
                return target + self.rng.uniform(-15, 15)        # still moving: a hand, a bounce
        return self.raw + self.rng.uniform(-0.2, 0.2)

    def write(self, data):
        with self._lock:
            self._buf += data
            while b"\n" in self._buf:
                raw, self._buf = self._buf.split(b"\n", 1)
                self._command(raw.decode().strip())

    def _command(self, c):
        if c == "P":
            self._emit(0.02, "PONG")
        elif c == "T":
            self.offset = self._weight_now(self.clock())
        elif c in ("1", "2", "3") and not self._busy:
            n = int(c)
            self._busy = True
            before = round(self.raw - self.offset, 1)
            self._emit(0.6, f"OK:{n}")                      # chute moves, waits 500 ms
            if self.jam_next:
                self.jam_next = False
                self._emit(0.6 + 3.0, f"JAM:{n}")
                self._out.append((self.clock() + 3.7 * self.k, "__idle__"))
                return
            self._out.append((self.clock() + 0.9 * self.k, "__empty__"))
            self._emit(1.3, f"DONE:{n}:{before:.1f}")
            self.fill[n - 1] = min(100, self.fill[n - 1] + self.rng.randint(1, 4))
            self._emit(1.5, "FILL:" + ",".join(str(v) for v in self.fill))
            self._out.append((self.clock() + 1.5 * self.k, "__idle__"))

    def read(self, n=1):
        if self.closed:
            raise OSError("simulated port closed")
        with self._lock:
            now = self.clock()
            lines = []
            if now >= self._next_w:
                self._next_w = now + 0.2 * self.k
                lines.append(f"W:{self._weight_now(now) - self.offset:.1f}")
            due = sorted(x for x in self._out if x[0] <= now)
            self._out = [x for x in self._out if x[0] > now]
            for _, line in due:
                if line == "__empty__":
                    self.raw, self._settle = 0.0, None
                elif line == "__idle__":
                    self._busy = False
                else:
                    lines.append(line)
        if not lines:
            time.sleep(0.01)
            return b""
        return ("\n".join(lines) + "\n").encode()

    def close(self):
        self.closed = True


def make_sorter(mode, port="COM3", baud=115200, log=print, steady_ms=500, tolerance_g=1.0):
    if mode == "esp32":
        return SerialSorter(port, baud, log=log, steady_ms=steady_ms, tolerance_g=tolerance_g)
    if mode == "sim":
        device = SimulatedESP32()
        s = SerialSorter("SIM", baud, opener=lambda p, b: device, list_ports=lambda: [], log=log,
                         steady_ms=steady_ms, tolerance_g=tolerance_g, simulated=True)
        s.device = device
        return s
    if mode == "camera":
        return CameraOnlySorter()
    return AttendantSorter()
