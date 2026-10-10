"""The deposit as pure state machines (no camera, Firestore, OpenCV or threads).

TrayFlow (modes esp32 and sim, the bin itself). The load cell is the trigger; YOLO runs only while
IDENTIFYING; no reward code can be created before the ESP32 reports DONE:

    READY ─weight > min─▶ WEIGHING ─steady─▶ IDENTIFYING ─vote / 3 s─▶ SORTING ─OK:n─▶ (wait) ─DONE:n─┐
      ▲                       │ (item gone)                    (reject → still sorted to Residual)   │
      │                       ▼                                                                         ▼
      ├──────────────── READY              ┌── REJECTED (3 messages, no code) ◀── reject ───────── DECIDE
      │                                    ├── NOTICE ("doesn't earn points" / "codes offline") ◀──────┤
      │                                    └── ISSUING ─▶ QR ─scan─▶ CLAIMED                       ◀──────┘
    JAM (JAM:n, no OK, no DONE) ─ staff key C, or weight < min for 10 s ─▶ READY
    PAUSED (a bin is full / the ESP32 is lost) ─ bins below full / PONG ─▶ READY

TransactionFlow (modes camera and attendant, for running without the bin's hardware):

    READY ─item seen─▶ DETECTED ─SPACE (attendant)──────────────────────▶ ISSUING ─▶ QR ─scan─▶ COMPLETE
                               └─stable vote (camera)─▶ DROPPING ─item left the view─┘

app.py feeds inputs with the current time and executes the returned actions. Every transaction has an id;
results for an older transaction are ignored, so a slow network or a repeated serial line can never issue a
second code or resurrect a finished one.
"""
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .hardware import (BIN_NAMES, DEFAULT_BIN_CODES, DeviceError, Done, Fill, Jam, LinkLost, LinkUp, Ok, Weight,
                       WeightTracker)

VOTE_WINDOW = 7    # frames in the majority vote (manuscript)
HOLD_FRAMES = 15   # camera/attendant: keep the last label this many empty frames before forgetting it


class Phase(Enum):
    READY = "ready"
    DETECTED = "detected"         # camera / attendant
    DROPPING = "dropping"         # camera: waiting for the item to leave the view
    WEIGHING = "weighing"         # tray: waiting for a steady weight
    IDENTIFYING = "identifying"   # tray: YOLO on the tray crop
    SORTING = "sorting"           # tray: OK then DONE from the ESP32
    ISSUING = "issuing"
    QR = "qr"
    COMPLETE = "complete"
    CLAIMED = "complete"          # alias: the spec's CLAIMED state is COMPLETE
    REJECTED = "rejected"         # tray: sorted to Residual, no code
    NOTICE = "notice"             # tray: sorted, but no code (no points / offline)
    JAM = "jam"
    PAUSED = "paused"
    FAILED = "failed"


@dataclass(frozen=True)
class Detection:
    label: str
    waste_type: object          # str, or None when the label is not in the waste map
    confidence: float
    box: object = None          # (x1, y1, x2, y2) in camera-frame pixels
    objects: int = 1            # separate objects seen in this frame (tray mode: "one item at a time")


@dataclass
class Transaction:
    id: int
    waste_type: str
    label: str
    confidence: float
    started: float
    sorted: bool = False
    grams: object = None
    reject: object = None       # tray: None or (reason, message)
    bin_code: object = None
    token: object = None
    claim_code: object = None
    expires_at: object = None   # timezone-aware UTC datetime
    payload: object = None
    qr_shown_at: object = None


@dataclass(frozen=True)
class Failure:
    kind: str
    title: str
    message: str
    severity: str = "warning"   # "warning" | "error"


# ── actions for app.py ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SendSort:
    tx_id: int
    waste_type: str
    code: str = ""


@dataclass(frozen=True)
class Tare:
    pass


@dataclass(frozen=True)
class IssueToken:
    tx_id: int
    waste_type: str
    label: str
    confidence: float


@dataclass(frozen=True)
class WatchToken:
    token: str


@dataclass(frozen=True)
class StopWatching:
    pass


@dataclass(frozen=True)
class Sound:
    kind: str                   # "detect" | "success" | "error"


@dataclass
class Toast:
    text: str
    kind: str                   # "info" | "warning" | "error"
    until: float


BLOCK_OFFLINE = "[BLOCKED] Firebase is offline - not issuing a QR code (it could never be redeemed)."
BLOCK_MODEL = "[BLOCKED] The loaded model does not match config/waste_map.json - not issuing a QR code."
BLOCK_NOTHING = ("[BLOCKED] No waste detected — hold an item in front of the camera for a moment until the "
                 "label appears, then press SPACE.")

REJECT_MESSAGES = {
    "unknown": "We couldn't recognise this item. It went to Residual.",
    "too_heavy": "Cups must be empty to earn points. It went to Residual.",
    "two_items": "One item at a time, please. It went to Residual.",
}


class _RewardFlow:
    """What every mode shares once a deposit is confirmed: the token, the QR, the claim and the history."""

    def __init__(self, points, issue_timeout, qr_display_seconds, success_seconds, failure_seconds):
        self.points = points
        self.issue_timeout, self.qr_display_seconds = issue_timeout, qr_display_seconds
        self.success_seconds, self.failure_seconds = success_seconds, failure_seconds
        self.phase, self.since = Phase.READY, 0.0
        self.tx, self.failure, self.toast = None, None, None
        self.current = None
        self.latest = None
        self._next_id = 1
        self.notices = []
        self.completed = 0
        self.history = deque(maxlen=200)
        self.online = self.model_ok = self.sorter_ready = True
        self.armed = True

    def points_for(self, waste_type):
        return int(self.points.get(waste_type, 0))

    def set_context(self, *, online, model_ok, sorter_ready):
        self.online, self.model_ok, self.sorter_ready = online, model_ok, sorter_ready

    def cooldown_left(self, now):
        return 0.0

    def _go(self, phase, now):
        self.phase, self.since = phase, now

    def _say(self, text):
        self.notices.append(text)

    def _toast(self, text, kind, now, seconds=3.0):
        self.toast = Toast(text, kind, now + seconds)

    def _new_tx(self, waste_type, label, confidence, now):
        self.tx = Transaction(self._next_id, waste_type, label, confidence, now)
        self._next_id += 1
        self.failure = None
        return self.tx

    def _fail(self, failure, now):
        self.failure = failure
        self._go(Phase.FAILED, now)
        self._say(f"[FAILED] {failure.title}: {failure.message}")
        return [Sound("error")]

    def _begin_issue(self, now):
        self._go(Phase.ISSUING, now)
        return [IssueToken(self.tx.id, self.tx.waste_type, self.tx.label, self.tx.confidence)]

    def _end(self, now):          # back to the start; each mode decides where that is
        raise NotImplementedError

    def _issued(self, now):       # hook: a code was shown
        pass

    def on_issue_ok(self, tx_id, token, claim_code, expires_at, payload, now):
        if self.phase != Phase.ISSUING or self.tx is None or self.tx.id != tx_id:
            self._say(f"[ISSUE] late result for #{tx_id} ignored")
            return []
        self.tx.token, self.tx.claim_code, self.tx.expires_at, self.tx.payload = token, claim_code, expires_at, payload
        self.tx.qr_shown_at = now
        self._issued(now)
        self._go(Phase.QR, now)
        return [WatchToken(token)]

    def on_issue_failed(self, tx_id, reason, now):
        if self.phase != Phase.ISSUING or self.tx is None or self.tx.id != tx_id:
            return []
        if self.tx.sorted:
            msg = (f"Your item went into the {self.tx.waste_type} bin, but we couldn't create a reward code. "
                   "Please ask staff.")
        else:
            msg = "Please try again in a moment. If it keeps happening, ask staff."
        return self._fail(Failure("issue_failed", "We couldn't create your code", msg, "error"), now)

    def on_scanned(self, token, now, wall_local=None):
        if self.phase != Phase.QR or self.tx is None or token != self.tx.token:
            return []
        self.completed += 1
        pts = self.points_for(self.tx.waste_type)
        self._say(f"[SCANNED] +{pts} pts | Total scans: {self.completed}")
        self.history.append({
            "waste_type": self.tx.waste_type, "points": pts, "claim_code": self.tx.claim_code,
            "ts": (wall_local or datetime.now()).strftime("%I:%M %p").lstrip("0"), "token": self.tx.token,
        })
        self._go(Phase.COMPLETE, now)
        return [StopWatching(), Sound("success")]

    def dismiss(self, now):
        if self.phase == Phase.QR:
            self._say("[QR] dismissed; the code stays valid until it expires")
            return self._end(now) + [StopWatching()]
        if self.phase in (Phase.FAILED, Phase.COMPLETE, Phase.REJECTED, Phase.NOTICE):
            return self._end(now)
        return []

    def _reward_tick(self, now, wall_utc):
        """Timeouts of the shared phases."""
        dt = now - self.since
        p = self.phase
        if p == Phase.ISSUING and dt >= self.issue_timeout:
            return self._fail(Failure("issue_timeout", "We couldn't reach ECOReward",
                                      ("Your item was sorted, but the code could not be created in time. "
                                       "Please ask staff." if self.tx and self.tx.sorted else
                                       "Creating your code took too long. Please try again.")), now)
        if p == Phase.QR:
            if self.tx.expires_at is not None and wall_utc >= self.tx.expires_at:
                self._say("[EXPIRED] QR expired.")
                return self._end(now) + [StopWatching()]
            if dt >= self.qr_display_seconds:
                self._say(f"[QR] back to ready after {self.qr_display_seconds:g} s without a scan; "
                          "the code stays valid until it expires")
                return self._end(now) + [StopWatching()]
        if p == Phase.COMPLETE and dt >= self.success_seconds:
            return self._end(now)
        if p == Phase.FAILED and dt >= self.failure_seconds:
            return self._end(now)
        return []


# ── camera and attendant modes ────────────────────────────────────────────────
@dataclass
class FlowConfig:
    mode: str = "attendant"     # attendant | camera
    points: dict = field(default_factory=lambda: {"Biodegradable": 15, "Recyclable": 10, "Residual": 5})
    issue_timeout: float = 20.0
    qr_display_seconds: float = 180.0
    success_seconds: float = 4.0
    failure_seconds: float = 7.0
    lock_votes: int = 5
    rearm_clear_frames: int = 10
    drop_clear_frames: int = 8      # camera mode: consecutive empty frames that count as "dropped"
    drop_timeout: float = 15.0      # camera mode: seconds to drop the item before giving up
    cooldown_seconds: float = 15.0  # camera mode: minimum time between two codes
    debug: bool = False


class TransactionFlow(_RewardFlow):
    """Camera mode (self-service without hardware; the item leaving the view counts as the drop) and attendant
    mode (staff confirm with SPACE). Camera mode cannot prove the item went in, hence the cool-down."""

    def __init__(self, cfg):
        super().__init__(cfg.points, cfg.issue_timeout, cfg.qr_display_seconds, cfg.success_seconds,
                         cfg.failure_seconds)
        self.cfg = cfg
        self._votes = deque(maxlen=VOTE_WINDOW)
        self._miss = self._clear = self._gone = 0
        self.cooldown_until = 0.0

    @property
    def automatic(self):
        return self.cfg.mode == "camera"

    @property
    def camera_only(self):
        return self.cfg.mode == "camera"

    @property
    def wants_frames(self):
        return self.phase in (Phase.READY, Phase.DETECTED, Phase.DROPPING)

    def cooldown_left(self, now):
        return max(0.0, self.cooldown_until - now) if self.camera_only else 0.0

    @property
    def lock_progress(self):
        if not self._votes or self.current is None:
            return 0.0
        n = sum(1 for d in self._votes if d.label == self.current.label)
        return min(1.0, n / max(1, self.cfg.lock_votes)) * min(1.0, len(self._votes) / VOTE_WINDOW)

    def _end(self, now):
        self._go(Phase.READY, now)
        self.tx, self.current, self.latest = None, None, None
        self._votes.clear()
        self._miss = self._clear = 0
        if not self.automatic:
            self.armed = True
        return []

    def _issued(self, now):
        if self.camera_only:
            self.cooldown_until = now + self.cfg.cooldown_seconds

    def observe(self, det, now):
        if self.phase == Phase.DROPPING:
            return self._watch_drop(det, now)
        if self.phase not in (Phase.READY, Phase.DETECTED):
            return []
        if det is None:
            self.latest = None
            self._miss += 1
            self._clear += 1
            if self._clear >= self.cfg.rearm_clear_frames:
                self.armed = True
            if self._miss > HOLD_FRAMES and self.current is not None:
                self._votes.clear()
                self.current = None
                self._go(Phase.READY, now)
            return []
        self._miss = self._clear = 0
        self.latest = det
        self._votes.append(det)
        label, count = Counter(d.label for d in self._votes).most_common(1)[0]
        best = max((d for d in self._votes if d.label == label), key=lambda d: d.confidence)
        previous = self.current.label if self.current else None
        self.current = Detection(label, best.waste_type, best.confidence, det.box)
        actions = [Sound("detect")] if label != previous else []
        if self.phase == Phase.READY:
            self._go(Phase.DETECTED, now)
        if (self.camera_only and self.armed and len(self._votes) == VOTE_WINDOW and count >= self.cfg.lock_votes
                and self.current.waste_type and self.model_ok and self.online and now >= self.cooldown_until):
            actions += self._begin_drop(now)
        return actions

    def _begin_drop(self, now):
        c = self.current
        self._new_tx(c.waste_type, c.label, c.confidence, now)
        self.armed = False
        self._gone = 0
        self._go(Phase.DROPPING, now)
        self._say(f"[DROP?] #{self.tx.id} {self.tx.waste_type} ({self.tx.label}, {self.tx.confidence:.0%}) - "
                  "waiting for the item to go in")
        return []

    def _watch_drop(self, det, now):
        if det is not None:
            self.latest = det
            self._gone = 0
            return []
        self.latest = None
        self._gone += 1
        if self._gone < self.cfg.drop_clear_frames:
            return []
        self.tx.sorted = True
        self._say(f"[DROP] #{self.tx.id} item left the camera view")
        if not self.online:
            return self._fail(Failure("offline_after_sort", "Dropped, but no reward code",
                                      "The kiosk went offline before your code could be created. Please try "
                                      "again later."), now)
        return self._begin_issue(now)

    def confirm(self, now):
        """SPACE: staff confirm (attendant); with --debug, a manual start in camera mode."""
        if self.phase not in (Phase.READY, Phase.DETECTED):
            return []
        if self.automatic and not self.cfg.debug:
            return []
        if not self.online:
            self._say(BLOCK_OFFLINE)
            self._toast("Can't issue a code: the kiosk is offline", "warning", now)
            return []
        if not self.model_ok:
            self._say(BLOCK_MODEL)
            self._toast("Can't issue a code: model and waste map don't match", "error", now)
            return []
        if self.current is None or not self.current.waste_type:
            self._say(BLOCK_NOTHING)
            self._toast("No item detected yet", "info", now)
            return []
        if self.automatic:
            return self._begin_drop(now)
        c = self.current
        self._new_tx(c.waste_type, c.label, c.confidence, now)
        self._say(f"[CONFIRMED] #{self.tx.id} {self.tx.waste_type} by staff")
        return self._begin_issue(now)

    def on_sorter_event(self, ev, now):
        return []                                       # no sorting hardware in these modes

    def staff_clear(self, now):
        return []

    def tick(self, now, wall_utc):
        if self.toast and now >= self.toast.until:
            self.toast = None
        if self.phase == Phase.DROPPING and now - self.since >= self.cfg.drop_timeout:
            return self._fail(Failure("no_drop_seen", "We didn't see it go in",
                                      f"Drop the item into the {self.tx.waste_type} bin, then show the next one."),
                              now)
        return self._reward_tick(now, wall_utc)

    def stages(self):
        if self.camera_only:
            steps = [("identify", "Identify the item"), ("drop", "Drop it into the bin"),
                     ("code", "Create your reward code")]
            active = {Phase.READY: 0, Phase.DETECTED: 0, Phase.DROPPING: 1, Phase.ISSUING: 2, Phase.QR: 3,
                      Phase.COMPLETE: 3}
        else:
            steps = [("identify", "Identify the item"), ("confirm", "Staff confirms the deposit"),
                     ("code", "Create your reward code")]
            active = {Phase.READY: 0, Phase.DETECTED: 1, Phase.ISSUING: 2, Phase.QR: 3, Phase.COMPLETE: 3}
        if self.phase == Phase.FAILED:
            idx = 1 if self.failure and self.failure.kind == "no_drop_seen" else len(steps) - 1
            return [(k, lbl, "done" if i < idx else "failed" if i == idx else "pending")
                    for i, (k, lbl) in enumerate(steps)]
        idx = active.get(self.phase, 0)
        return [(k, lbl, "done" if i < idx else "active" if i == idx else "pending") for i, (k, lbl) in enumerate(steps)]


# ── the bin: load cell -> YOLO -> ESP32 sort -> DONE -> reward ────────────────
@dataclass
class TrayConfig:
    points: dict = field(default_factory=lambda: {"Biodegradable": 15, "Recyclable": 10, "Residual": 5})
    bin_codes: dict = field(default_factory=lambda: dict(DEFAULT_BIN_CODES))
    max_weight_g: dict = field(default_factory=lambda: {"default": 500})
    min_item_g: float = 3.0
    steady_ms: float = 500
    steady_tolerance_g: float = 1.0
    identify_timeout_s: float = 3.0
    ok_timeout_s: float = 3.0
    drop_timeout_s: float = 4.0
    multi_item_frames: int = 3      # frames showing 2+ separate objects that make it "two items"
    jam_clear_s: float = 10.0       # JAM ends by itself once the scale reads empty this long
    auto_tare_idle_s: float = 60.0
    auto_tare_max_g: float = 5.0
    bin_warn_pct: int = 90
    bin_full_pct: int = 100
    weighing_hint_s: float = 5.0    # "please let go" after this long without a steady weight
    reject_seconds: float = 4.0
    notice_seconds: float = 3.0
    issue_timeout: float = 20.0
    qr_display_seconds: float = 180.0
    success_seconds: float = 4.0
    failure_seconds: float = 7.0


class TrayFlow(_RewardFlow):
    automatic = True
    camera_only = False

    def __init__(self, cfg):
        super().__init__(cfg.points, cfg.issue_timeout, cfg.qr_display_seconds, cfg.success_seconds,
                         cfg.failure_seconds)
        self.cfg = cfg
        self.scale = WeightTracker(cfg.steady_ms, cfg.steady_tolerance_g)
        self.fill = None
        self.link_up = False
        self.pause_reason = "link"           # out of service until the ESP32 answers PONG
        self._go(Phase.PAUSED, 0.0)
        self._votes = []
        self._multi = 0
        self._awaiting = None                # "ok" | "done" while SORTING
        self._step_t = 0.0
        self._empty_since = None             # JAM: since when the scale reads empty
        self._last_tare = -1e9
        self.notice = None                   # (title, message) for NOTICE / REJECTED

    # ── helpers ────────────────────────────────────────────────────────────
    @property
    def wants_frames(self):
        return self.phase == Phase.IDENTIFYING

    @property
    def weight(self):
        return self.scale.latest

    def steady(self, now):
        return self.scale.steady(now)

    @property
    def lock_progress(self):
        return min(1.0, len(self._votes) / VOTE_WINDOW)

    def _empty(self):
        return self.scale.latest is None or self.scale.latest < self.cfg.min_item_g

    def _bin_full(self):
        return self.fill is not None and any(v >= self.cfg.bin_full_pct for v in self.fill)

    def bins_warn(self):
        """Names of bins at or above bin_warn_pct."""
        if self.fill is None:
            return []
        return [BIN_NAMES[i + 1] for i, v in enumerate(self.fill) if v >= self.cfg.bin_warn_pct]

    def _code(self, waste_type):
        return str(self.cfg.bin_codes.get(waste_type, DEFAULT_BIN_CODES[waste_type]))

    def _max_weight(self, label):
        table = self.cfg.max_weight_g
        return float(table.get(label, table.get("default", 500)))

    def hint(self, now):
        """The weight has been moving for a while (a hand in the slot)."""
        return self.phase == Phase.WEIGHING and now - self.since >= self.cfg.weighing_hint_s

    def _end(self, now):
        """Leave a finished deposit. Never READY while the scale still reads an item: that item is new."""
        self.tx, self.current, self.latest, self.notice = None, None, None, None
        self._votes, self._multi, self._awaiting = [], 0, None
        if not self.link_up:
            self.pause_reason = "link"
            self._go(Phase.PAUSED, now)
        elif self._bin_full():
            self.pause_reason = "bin_full"
            self._go(Phase.PAUSED, now)
        elif not self._empty():
            self._go(Phase.WEIGHING, now)
        else:
            self._go(Phase.READY, now)
        return []

    def _abort(self, reason, now):
        """The ESP32 went away mid-deposit: no code, out of service until it answers again."""
        if self.tx is not None and self.phase in (Phase.WEIGHING, Phase.IDENTIFYING, Phase.SORTING):
            self._say(f"[ABORT] #{self.tx.id} {reason}; no code issued")
        self.tx, self._votes, self._awaiting = None, [], None
        self.pause_reason = "link"
        self._go(Phase.PAUSED, now)
        return [Sound("error")]

    def _jam(self, why, now):
        self._say(f"[JAM] #{self.tx.id if self.tx else '-'} {why}; weight {self.weight} g; no code issued")
        self.tx, self._awaiting = None, None
        self._empty_since = None
        self._go(Phase.JAM, now)
        return [Sound("error")]

    # ── ESP32 ──────────────────────────────────────────────────────────────
    def on_sorter_event(self, ev, now):
        if isinstance(ev, Weight):
            self.scale.add(ev.grams, now)
            return self._on_weight(now)
        if isinstance(ev, LinkUp):
            self.link_up = True
            self._say("[SORTER] ESP32 answered PONG")
            if self.phase == Phase.PAUSED and self.pause_reason == "link":
                return self._end(now)
            return []
        if isinstance(ev, LinkLost):
            self.link_up = False
            self._say(f"[SORTER] lost: {ev.reason}")
            if self.phase in (Phase.READY, Phase.WEIGHING, Phase.IDENTIFYING, Phase.SORTING, Phase.JAM):
                return self._abort("ESP32 disconnected", now)
            return []                                       # a QR on screen stays; PAUSED comes after it
        if isinstance(ev, Fill):
            self.fill = (ev.bio, ev.rec, ev.res)
            if self._bin_full() and self.phase in (Phase.READY, Phase.WEIGHING):
                self._say(f"[BINS] full: {self.fill}; paused")
                self.pause_reason = "bin_full"
                self._go(Phase.PAUSED, now)
            elif not self._bin_full() and self.phase == Phase.PAUSED and self.pause_reason == "bin_full":
                self._say(f"[BINS] below full: {self.fill}; resuming")
                return self._end(now)
            return []
        if self.phase != Phase.SORTING or self.tx is None:
            if isinstance(ev, (Ok, Done, Jam)):
                self._say(f"[SORTER] ignored {ev} during {self.phase.value}")
            elif isinstance(ev, DeviceError):
                self._say(f"[SORTER] ESP32 error: {ev.message}")
            return []
        expected = int(self.tx.bin_code)
        if isinstance(ev, Ok):
            if ev.bin != expected:
                return self._jam(f"chute reported bin {ev.bin}, expected {expected}", now)
            if self._awaiting == "ok":
                self._awaiting, self._step_t = "done", now
            return []
        if isinstance(ev, Jam):
            return self._jam(f"JAM:{ev.bin} from the ESP32", now)
        if isinstance(ev, DeviceError):
            return self._jam(f"ESP32 error during sorting: {ev.message}", now)
        if isinstance(ev, Done):
            if ev.bin != expected:
                return self._jam(f"DONE for bin {ev.bin}, expected {expected}", now)
            return self._after_done(ev, now)
        return []

    def _on_weight(self, now):
        w = self.scale.latest
        if self.phase == Phase.READY and w >= self.cfg.min_item_g:
            self._go(Phase.WEIGHING, now)
        if self.phase == Phase.WEIGHING:
            if w < self.cfg.min_item_g:
                if self.scale.steady(now):                  # steadily empty: a false trigger (a bounce is not)
                    self._go(Phase.READY, now)
            elif self.scale.steady(now):
                return self._begin_identify(w, now)
        if self.phase == Phase.JAM:
            if self._empty():
                if self._empty_since is None:
                    self._empty_since = now
            else:
                self._empty_since = None
        return []

    # ── identify and decide ────────────────────────────────────────────────
    def _begin_identify(self, grams, now):
        self._new_tx(None, None, 0.0, now)
        self.tx.grams = round(grams, 1)
        self._votes, self._multi = [], 0
        self.current = self.latest = None
        self._go(Phase.IDENTIFYING, now)
        self._say(f"[ITEM] #{self.tx.id} on the tray: {self.tx.grams} g")
        return []

    def observe(self, det, now):
        if self.phase != Phase.IDENTIFYING:
            return []
        self.latest = det
        self._votes.append(det)
        if det is not None and det.objects >= 2:
            self._multi += 1
        if len(self._votes) >= VOTE_WINDOW:
            return self._decide(now)
        return []

    def _decide(self, now):
        seen = [d for d in self._votes if d is not None]
        label, waste, conf = None, None, 0.0
        if seen:
            top, count = Counter(d.label for d in seen).most_common(1)[0]
            if count >= max(3, len(self._votes) // 2 + 1):           # a real majority of the frames
                best = max((d for d in seen if d.label == top), key=lambda d: d.confidence)
                label, waste, conf = top, best.waste_type, best.confidence
                self.current = best
        tx = self.tx
        tx.label, tx.confidence = label, conf
        reject = None
        if self._multi >= self.cfg.multi_item_frames:
            reject = "two_items"
        elif label is None or waste is None or not self.model_ok:
            reject = "unknown"
        elif tx.grams > self._max_weight(label):
            reject = "too_heavy"
        if reject:
            tx.reject = (reject, REJECT_MESSAGES[reject])
            waste = "Residual"
            limit = f"{self._max_weight(label):g}" if label else "-"
            self._say(f"[REJECT] #{tx.id} {reject}: label={label} {conf:.0%} weight={tx.grams} g "
                      f"(max {limit} g) -> Residual, no code")
        else:
            self._say(f"[IDENTIFIED] #{tx.id} {label} ({conf:.0%}) -> {waste}, {tx.grams} g")
        tx.waste_type = waste
        tx.bin_code = self._code(waste)
        self._awaiting, self._step_t = "ok", now
        self._go(Phase.SORTING, now)
        return [SendSort(tx.id, waste, tx.bin_code)] + ([] if reject else [Sound("detect")])

    # ── after DONE: the only way to a reward code ──────────────────────────
    def _after_done(self, ev, now):
        tx = self.tx
        tx.sorted = True
        self._awaiting = None
        self._say(f"[DONE] #{tx.id} bin {ev.bin} ({tx.waste_type}), {ev.grams:g} g before tipping")
        if tx.reject:
            self.notice = ("Sorted into Residual", tx.reject[1])
            self._go(Phase.REJECTED, now)
            return []
        if self.points_for(tx.waste_type) <= 0:
            self.notice = ("Thank you!", "This item doesn't earn points.")
            self._go(Phase.NOTICE, now)
            return []
        if not self.online:
            self.notice = ("Sorted!", "Reward codes are offline right now.")
            self._go(Phase.NOTICE, now)
            return []
        return self._begin_issue(now)

    # ── staff and time ─────────────────────────────────────────────────────
    def confirm(self, now):
        return []                                           # nothing to confirm: DONE is the proof

    def staff_clear(self, now):
        """Staff key C: the tray was cleared by hand after a jam."""
        if self.phase == Phase.JAM:
            self._say("[JAM] cleared by staff")
            return self._end(now)
        return []

    def tick(self, now, wall_utc):
        if self.toast and now >= self.toast.until:
            self.toast = None
        p, dt = self.phase, now - self.since
        if p == Phase.READY:
            return self._maybe_tare(now)
        if p == Phase.IDENTIFYING and dt >= self.cfg.identify_timeout_s:
            return self._decide(now)
        if p == Phase.SORTING:
            waited = now - self._step_t
            if self._awaiting == "ok" and waited >= self.cfg.ok_timeout_s:
                return self._jam(f"no OK:{self.tx.bin_code} within {self.cfg.ok_timeout_s:g} s", now)
            if self._awaiting == "done" and waited >= self.cfg.drop_timeout_s:
                return self._jam(f"no DONE:{self.tx.bin_code} within {self.cfg.drop_timeout_s:g} s", now)
            return []
        if p == Phase.JAM:
            if self._empty_since is not None and now - self._empty_since >= self.cfg.jam_clear_s:
                self._say(f"[JAM] scale empty for {self.cfg.jam_clear_s:g} s; resuming")
                return self._end(now)
            return []
        if p == Phase.REJECTED and dt >= self.cfg.reject_seconds:
            return self._end(now)
        if p == Phase.NOTICE and dt >= self.cfg.notice_seconds:
            return self._end(now)
        return self._reward_tick(now, wall_utc)

    def _maybe_tare(self, now):
        """Auto-tare only in READY, with a steady near-zero scale and nothing placed for a minute."""
        w = self.scale.latest
        if (self.link_up and w is not None and abs(w) < self.cfg.auto_tare_max_g and self.scale.steady(now)
                and now - self.since >= self.cfg.auto_tare_idle_s
                and now - self._last_tare >= self.cfg.auto_tare_idle_s):
            self._last_tare = now
            self._say(f"[TARE] scale read {w:g} g while empty; taring")
            return [Tare()]
        return []

    def stages(self):
        steps = [("weigh", "Weigh the item"), ("identify", "Recognise it"), ("sort", "Sort it into the bin"),
                 ("code", "Create your reward code")]
        if self.phase in (Phase.REJECTED, Phase.FAILED):
            return [(k, lbl, "done" if i < 3 else "failed") for i, (k, lbl) in enumerate(steps)]
        if self.phase == Phase.JAM:
            return [(k, lbl, "done" if i < 2 else "failed" if i == 2 else "pending") for i, (k, lbl) in enumerate(steps)]
        if self.phase == Phase.PAUSED:
            return [(k, lbl, "pending") for k, lbl in steps]
        idx = {Phase.READY: 0, Phase.WEIGHING: 0, Phase.IDENTIFYING: 1, Phase.SORTING: 2, Phase.ISSUING: 3,
               Phase.QR: 4, Phase.COMPLETE: 4, Phase.NOTICE: 4}.get(self.phase, 0)
        return [(k, lbl, "done" if i < idx else "active" if i == idx else "pending") for i, (k, lbl) in enumerate(steps)]
