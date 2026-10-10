"""The deposit transaction as a pure state machine (no camera, Firestore, OpenCV or threads).

    READY ─item seen─▶ DETECTED ─SPACE (attendant)───────────────────────────▶ ISSUING ─▶ QR ─scan─▶ COMPLETE
                               ├─stable vote (esp32/sim)─▶ SORTING ─OK─▶ CONFIRMING ─weight─┤      │
                               └─stable vote (camera)───▶ DROPPING ─item left the view──────┘      │
    any timeout / fault ─▶ FAILED ─(a few seconds or "OK")─▶ READY ◀── expiry / walk-away / Done ──┘

Camera mode is self-service without sorting hardware: the screen names the bin, and the item disappearing
from view counts as the drop. It cannot prove the item went into the bin (the ESP32 load cell can), so it
adds a cool-down between codes.

app.py feeds it inputs with the current time and executes the actions it returns (send a servo command,
start the token write, watch/stop watching the token, play a sound). Every transaction has an id; results
that arrive for an older transaction are ignored, so a slow network or a repeated sensor line can never
issue a second code or resurrect a finished one.
"""
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .hardware import Ack, DeviceError, Weight

VOTE_WINDOW = 7    # frames in the majority vote (manuscript)
HOLD_FRAMES = 15   # keep the last label this many empty frames before forgetting it


class Phase(Enum):
    READY = "ready"
    DETECTED = "detected"
    SORTING = "sorting"
    DROPPING = "dropping"       # camera mode: waiting for the item to leave the camera's view
    CONFIRMING = "confirming"
    ISSUING = "issuing"
    QR = "qr"
    COMPLETE = "complete"
    FAILED = "failed"


ACTIVE_PHASES = (Phase.SORTING, Phase.CONFIRMING, Phase.DROPPING, Phase.ISSUING)


@dataclass(frozen=True)
class Detection:
    label: str
    waste_type: object          # str, or None when the label is not in the waste map
    confidence: float
    box: object = None          # (x1, y1, x2, y2) in camera-frame pixels


@dataclass
class Transaction:
    id: int
    waste_type: str
    label: str
    confidence: float
    started: float
    sorted: bool = False
    grams: object = None
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
BLOCK_SORTER = "[BLOCKED] The sorter is not connected - not starting a sort."


@dataclass
class FlowConfig:
    mode: str = "attendant"     # attendant | camera | esp32 | sim
    points: dict = field(default_factory=lambda: {"Biodegradable": 15, "Recyclable": 10, "Residual": 5})
    sort_ack_timeout: float = 5.0
    drop_confirm_timeout: float = 10.0
    issue_timeout: float = 20.0
    qr_display_seconds: float = 180.0
    success_seconds: float = 4.0
    failure_seconds: float = 7.0
    lock_votes: int = 5
    rearm_clear_frames: int = 10
    min_drop_grams: float = 5.0
    drop_clear_frames: int = 8      # camera mode: consecutive empty frames that count as "dropped"
    drop_timeout: float = 15.0      # camera mode: seconds to drop the item before giving up
    cooldown_seconds: float = 15.0  # camera mode: minimum time between two codes
    debug: bool = False


class TransactionFlow:
    def __init__(self, cfg):
        self.cfg = cfg
        self.phase, self.since = Phase.READY, 0.0
        self.tx, self.failure, self.toast = None, None, None
        self.current = None                 # voted detection (label, category, best confidence)
        self.latest = None                  # this frame's top detection (for the box on screen)
        self._votes = deque(maxlen=VOTE_WINDOW)
        self._miss = 0
        self._clear = 0
        self.armed = True                   # esp32/sim: may start an automatic sort
        self._next_id = 1
        self.notices = []                   # log lines for app.py to print
        self.completed = 0
        self.history = deque(maxlen=200)
        self.online = self.model_ok = self.sorter_ready = True
        self.cooldown_until = 0.0
        self._gone = 0

    # ── helpers ────────────────────────────────────────────────────────────
    @property
    def automatic(self):
        """No staff key needed: the kiosk starts the deposit by itself."""
        return self.cfg.mode in ("esp32", "sim", "camera")

    @property
    def camera_only(self):
        return self.cfg.mode == "camera"

    def cooldown_left(self, now):
        return max(0.0, self.cooldown_until - now) if self.camera_only else 0.0

    def points_for(self, waste_type):
        return int(self.cfg.points.get(waste_type, 0))

    def set_context(self, *, online, model_ok, sorter_ready):
        self.online, self.model_ok, self.sorter_ready = online, model_ok, sorter_ready

    def _go(self, phase, now):
        self.phase, self.since = phase, now

    def _say(self, text):
        self.notices.append(text)

    def _toast(self, text, kind, now, seconds=3.0):
        self.toast = Toast(text, kind, now + seconds)

    def _start(self, now):
        c = self.current
        self.tx = Transaction(self._next_id, c.waste_type, c.label, c.confidence, now)
        self._next_id += 1
        self.failure = None
        self.armed = False

    def _fail(self, failure, now):
        self.failure = failure
        self._go(Phase.FAILED, now)
        self._say(f"[FAILED] {failure.title}: {failure.message}")
        return [Sound("error")]

    def _to_ready(self, now):
        self._go(Phase.READY, now)
        self.tx, self.current, self.latest = None, None, None
        self._votes.clear()
        self._miss = 0
        self._clear = 0
        if not self.automatic:
            self.armed = True

    @property
    def lock_progress(self):
        if not self._votes or self.current is None:
            return 0.0
        n = sum(1 for d in self._votes if d.label == self.current.label)
        return min(1.0, n / max(1, self.cfg.lock_votes)) * min(1.0, len(self._votes) / VOTE_WINDOW)

    # ── camera ─────────────────────────────────────────────────────────────
    def observe(self, det, now):
        """One analysed camera frame: the most confident detection, or None."""
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
        self._miss = 0
        self._clear = 0
        self.latest = det
        self._votes.append(det)
        label, count = Counter(d.label for d in self._votes).most_common(1)[0]
        best = max((d for d in self._votes if d.label == label), key=lambda d: d.confidence)
        previous = self.current.label if self.current else None
        self.current = Detection(label, best.waste_type, best.confidence, det.box)
        actions = [Sound("detect")] if label != previous else []
        if self.phase == Phase.READY:
            self._go(Phase.DETECTED, now)
        if (self.automatic and self.armed and len(self._votes) == VOTE_WINDOW and count >= self.cfg.lock_votes
                and self.current.waste_type and self.model_ok and self._can_start(now)):
            actions += self._begin_sort(now)
        return actions

    def _can_start(self, now):
        if self.camera_only:      # no hardware to wait for; a code is the only outcome, so it must be possible
            return self.online and now >= self.cooldown_until
        return self.sorter_ready

    def _watch_drop(self, det, now):
        """Camera mode: the item leaving the view for `drop_clear_frames` frames in a row counts as the drop."""
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

    def _begin_sort(self, now):
        self._start(now)
        if self.camera_only:
            self._gone = 0
            self._go(Phase.DROPPING, now)
            self._say(f"[DROP?] #{self.tx.id} {self.tx.waste_type} ({self.tx.label}, {self.tx.confidence:.0%}) - "
                      "waiting for the item to go in")
            return []
        self._go(Phase.SORTING, now)
        self._say(f"[SORT] #{self.tx.id} {self.tx.waste_type} ({self.tx.label}, {self.tx.confidence:.0%})")
        return [SendSort(self.tx.id, self.tx.waste_type)]

    def _begin_issue(self, now):
        self._go(Phase.ISSUING, now)
        return [IssueToken(self.tx.id, self.tx.waste_type, self.tx.label, self.tx.confidence)]

    # ── staff key (SPACE) ──────────────────────────────────────────────────
    def confirm(self, now):
        if self.phase not in (Phase.READY, Phase.DETECTED):
            return []                                    # one transaction at a time
        if self.automatic and not self.cfg.debug:
            return []
        if not self.online and not self.automatic:
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
            if self.camera_only and not self.online:
                self._say(BLOCK_OFFLINE)
                self._toast("Can't issue a code: the kiosk is offline", "warning", now)
                return []
            if not self.camera_only and not self.sorter_ready:
                self._say(BLOCK_SORTER)
                self._toast("The sorter is not connected", "warning", now)
                return []
            return self._begin_sort(now)
        self._start(now)
        self._say(f"[CONFIRMED] #{self.tx.id} {self.tx.waste_type} by staff")
        return self._begin_issue(now)

    # ── sorter ─────────────────────────────────────────────────────────────
    def on_sorter_event(self, ev, now):
        if isinstance(ev, Ack) and self.phase == Phase.SORTING:
            self._go(Phase.CONFIRMING, now)
            return []
        if isinstance(ev, Weight) and self.phase == Phase.CONFIRMING:
            self.tx.grams = ev.grams
            if ev.grams < self.cfg.min_drop_grams:
                return self._fail(Failure("no_drop", "We couldn't confirm the drop",
                                          "Make sure the item went into the chute, then try again."), now)
            self.tx.sorted = True
            self._say(f"[DROP] #{self.tx.id} {ev.grams:g} g")
            if not self.online:
                return self._fail(Failure("offline_after_sort", "Sorted, but no reward code",
                                          f"Your item went into the {self.tx.waste_type} bin, but the kiosk is "
                                          "offline so we couldn't create a code. Please ask staff."), now)
            return self._begin_issue(now)
        if isinstance(ev, DeviceError) and self.phase in (Phase.SORTING, Phase.CONFIRMING):
            return self._fail(Failure("device_error", "The sorter reported a problem",
                                      f"{ev.message}. Please ask staff for help.", "error"), now)
        if isinstance(ev, (Ack, Weight)):
            self._say(f"[SORTER] ignored {ev} during {self.phase.value}")
        return []

    # ── token write (runs on a worker; results come back here) ─────────────
    def on_issue_ok(self, tx_id, token, claim_code, expires_at, payload, now):
        if self.phase != Phase.ISSUING or self.tx is None or self.tx.id != tx_id:
            self._say(f"[ISSUE] late result for #{tx_id} ignored")
            return []
        self.tx.token, self.tx.claim_code, self.tx.expires_at, self.tx.payload = token, claim_code, expires_at, payload
        self.tx.qr_shown_at = now
        if self.camera_only:
            self.cooldown_until = now + self.cfg.cooldown_seconds
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

    # ── phone ──────────────────────────────────────────────────────────────
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

    # ── user / staff dismiss ───────────────────────────────────────────────
    def dismiss(self, now):
        if self.phase == Phase.QR:
            self._say("[QR] dismissed; the code stays valid until it expires")
            self._to_ready(now)
            return [StopWatching()]
        if self.phase in (Phase.FAILED, Phase.COMPLETE):
            self._to_ready(now)
        return []

    # ── time ───────────────────────────────────────────────────────────────
    def tick(self, now, wall_utc):
        if self.toast and now >= self.toast.until:
            self.toast = None
        dt = now - self.since
        p = self.phase
        if p == Phase.SORTING and dt >= self.cfg.sort_ack_timeout:
            return self._fail(Failure("sorter_timeout", "The sorter didn't respond",
                                      "Please take your item and try again. If this keeps happening, ask staff."), now)
        if p == Phase.DROPPING and dt >= self.cfg.drop_timeout:
            return self._fail(Failure("no_drop_seen", "We didn't see it go in",
                                      f"Drop the item into the {self.tx.waste_type} bin, then show the next one."),
                              now)
        if p == Phase.CONFIRMING and dt >= self.cfg.drop_confirm_timeout:
            return self._fail(Failure("no_confirm", "We couldn't confirm the drop",
                                      "The bin didn't register your item. Please try again or ask staff."), now)
        if p == Phase.ISSUING and dt >= self.cfg.issue_timeout:
            return self._fail(Failure("issue_timeout", "We couldn't reach ECOReward",
                                      ("Your item was sorted, but the code could not be created in time. Please ask staff."
                                       if self.tx and self.tx.sorted else
                                       "Creating your code took too long. Please try again.")), now)
        if p == Phase.QR:
            if self.tx.expires_at is not None and wall_utc >= self.tx.expires_at:
                self._say("[EXPIRED] QR expired.")
                self._to_ready(now)
                return [StopWatching()]
            if dt >= self.cfg.qr_display_seconds:
                self._say(f"[QR] back to ready after {self.cfg.qr_display_seconds:g} s without a scan; "
                          "the code stays valid until it expires")
                self._to_ready(now)
                return [StopWatching()]
        if p == Phase.COMPLETE and dt >= self.cfg.success_seconds:
            self._to_ready(now)
        if p == Phase.FAILED and dt >= self.cfg.failure_seconds:
            self._to_ready(now)
        return []

    # ── for the UI ─────────────────────────────────────────────────────────
    def stages(self):
        """[(key, label, status)] for the stepper; status in done/active/pending/failed."""
        if self.camera_only:
            steps = [("identify", "Identify the item"), ("drop", "Drop it into the bin"),
                     ("code", "Create your reward code")]
            active = {Phase.READY: 0, Phase.DETECTED: 0, Phase.DROPPING: 1, Phase.ISSUING: 2, Phase.QR: 3,
                      Phase.COMPLETE: 3}
        elif self.automatic:
            steps = [("identify", "Identify the item"), ("sort", "Sort it into the bin"),
                     ("confirm", "Confirm the drop"), ("code", "Create your reward code")]
            active = {Phase.READY: 0, Phase.DETECTED: 0, Phase.SORTING: 1, Phase.CONFIRMING: 2,
                      Phase.ISSUING: 3, Phase.QR: 4, Phase.COMPLETE: 4}
        else:
            steps = [("identify", "Identify the item"), ("confirm", "Staff confirms the deposit"),
                     ("code", "Create your reward code")]
            active = {Phase.READY: 0, Phase.DETECTED: 1, Phase.ISSUING: 2, Phase.QR: 3, Phase.COMPLETE: 3}
        if self.phase == Phase.FAILED:
            failed_at = {"sorter_timeout": 1, "device_error": 1, "no_drop": 2, "no_confirm": 2, "no_drop_seen": 1,
                         "offline_after_sort": len(steps) - 1, "issue_failed": len(steps) - 1, "issue_timeout": len(steps) - 1}
            idx = failed_at.get(self.failure.kind if self.failure else "", len(steps) - 1)
            return [(k, lbl, "done" if i < idx else "failed" if i == idx else "pending")
                    for i, (k, lbl) in enumerate(steps)]
        idx = active.get(self.phase, 0)
        return [(k, lbl, "done" if i < idx else "active" if i == idx else "pending") for i, (k, lbl) in enumerate(steps)]
