# -*- coding: utf-8 -*-
"""ECOReward Bin kiosk: camera -> YOLOv8 -> deposit confirmation -> Firestore token -> QR -> phone claim.

    py -3.11 app.py                        staff confirm deposits with SPACE (no sorter hardware)
    py -3.11 app.py --sorter esp32         ESP32 servo + load cell over USB serial (README "ESP32 protocol")
    py -3.11 app.py --no-hardware          simulated ESP32 for demos (labelled SIMULATION on screen)
    py -3.11 app.py --fullscreen           fill the kiosk display
    py -3.11 app.py --help                 every option; defaults live in config/kiosk_settings.json

Staff keys: SPACE confirm deposit (attendant mode) · R claim-code receipt · H history · D diagnostics ·
Esc close · Q quit. Customers only need the touch screen (or nothing at all in ESP32 mode).

Layout of the code: this file wires Firestore, the model and the camera to the pieces in kiosk/
(config, hardware, flow, ui). The token contract with the phone app and the Firestore rules is in
issue_token() and must not change: ECOBIN:<Type>:<uuid4>, Timestamp expiresAt, server-time createdAt.
"""
import hashlib
import json
import logging
import logging.handlers
import os
import platform
import queue
import secrets
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np
import qrcode
import firebase_admin
from firebase_admin import credentials, firestore
from ultralytics import YOLO

# kiosk_firestore.py and kiosk/ sit next to this file; isolated mode (python -I) does not add this folder itself.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from kiosk_firestore import KioskFirestore, SERVER_TIMESTAMP as KIOSK_SERVER_TIMESTAMP  # noqa: E402
from kiosk.config import load_settings  # noqa: E402
from kiosk.flow import (Detection, FlowConfig, IssueToken, Phase, SendSort, Sound, StopWatching,  # noqa: E402
                        TransactionFlow, WatchToken)
from kiosk.hardware import DeviceError, Fill, make_sorter  # noqa: E402
from kiosk.ui.screens import Renderer, View  # noqa: E402

# ── Platform-safe beep ────────────────────────────────────────────────────────
if platform.system() == 'Windows':
    import winsound
    def _beep(freq=880, dur=120):
        winsound.Beep(freq, dur)
else:
    def _beep(freq=880, dur=120):  # no-op on Linux/macOS
        pass

# ── Logging: console (as before) + one file per day for unattended operation ──
_file_log = logging.getLogger("ecoreward.kiosk")
_file_log.propagate = False
_warned = {}


def log(msg):
    """Print (operators and the tests read stdout) and append to the daily log file once it is set up."""
    print(msg)
    if _file_log.handlers:
        _file_log.info(str(msg).strip())


def _warn_once(key, msg, every=600):
    """Log a repeating problem at most once per `every` seconds instead of on every frame/beat."""
    now = time.monotonic()
    if now - _warned.get(key, -1e9) >= every:
        _warned[key] = now
        log(msg)


def _setup_file_log(log_dir):
    if _file_log.handlers:
        return _file_log.handlers[0].baseFilename
    path = Path(os.environ.get("ECOREWARD_LOG_DIR", "").strip() or _APP_DIR / log_dir)
    try:
        path.mkdir(parents=True, exist_ok=True)
        h = logging.handlers.TimedRotatingFileHandler(path / "kiosk.log", when="midnight", backupCount=14,
                                                      encoding="utf-8")
    except OSError as e:
        print(f"[LOG] file logging disabled ({e})")
        return None
    h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _file_log.addHandler(h)
    _file_log.setLevel(logging.INFO)
    return h.baseFilename

# ── Firebase ───────────────────────────────────────────────────────────────────
_APP_DIR     = Path(__file__).resolve().parent
_DEFAULT_KEY = Path.home() / ".ecoreward" / "kiosk-sa.json"
# Legacy names inside the project folder. Still accepted so existing set-ups keep working,
# but a service-account key must live OUTSIDE the project folder (it is a full-access secret).
_LEGACY_KEYS = ("serviceAccountKey.json", "serviceAccountKey.json.json")


def find_service_account():
    """Locate the Firebase Admin credential file. Returns (path or None, every place checked).

    Order:
      1. GOOGLE_APPLICATION_CREDENTIALS - explicit; if it is set but the file is missing we
         stop there instead of silently using some other key.
      2. ~/.ecoreward/kiosk-sa.json     - recommended location, outside the project / Git / any ZIP.
      3. <this folder>/serviceAccountKey.json[.json] - legacy, deprecated.
    """
    tried = []
    explicit = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        tried.append(f"{path}  (GOOGLE_APPLICATION_CREDENTIALS)")
        return (path if path.is_file() else None), tried
    for path in [_DEFAULT_KEY] + [_APP_DIR / name for name in _LEGACY_KEYS]:
        tried.append(str(path))
        if path.is_file():
            return path, tried
    return None, tried


# Preferred (audit C-02 / Phase 7): the least-privilege kiosk identity written by tools/set_claims.py.
_KIOSK_CONFIG = Path(os.environ.get("ECOREWARD_KIOSK_CONFIG", "").strip()
                     or Path.home() / ".ecoreward" / "kiosk.json").expanduser()

db = None
DB_MODE = None                          # 'kiosk' (rules apply) or 'admin' (legacy, bypasses rules)
SERVER_TS = firestore.SERVER_TIMESTAMP
if _KIOSK_CONFIG.is_file():
    # An explicit kiosk identity wins; if it fails we stay offline rather than silently
    # falling back to the full-access key.
    try:
        db = KioskFirestore.from_config(_KIOSK_CONFIG)
        db.check()
        DB_MODE, SERVER_TS = "kiosk", KIOSK_SERVER_TIMESTAMP
        print("[FIREBASE] Connected as the kiosk identity (least privilege).")
    except Exception as e:
        db = None
        print(f"[FIREBASE ERROR] Kiosk identity in {_KIOSK_CONFIG} could not sign in: {e}")
        print("[FIREBASE ERROR] Running OFFLINE: QR codes will NOT be issued until this is fixed (see README).")
else:
    try:
        _key, _tried = find_service_account()
        if _key is None:
            raise FileNotFoundError("Firebase credential not found. Looked in:\n    " + "\n    ".join(_tried))
        if _APP_DIR in _key.resolve().parents:
            print("[SECURITY WARNING] The Firebase key is stored inside the project folder.")
            print(f"                   Move it to {_DEFAULT_KEY} (or set GOOGLE_APPLICATION_CREDENTIALS),")
            print("                   and rotate it if this folder was ever zipped, shared or pushed.")
        cred = credentials.Certificate(str(_key))
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        db = firestore.client()
        DB_MODE = "admin"
        print("[FIREBASE] Connected!")
        print("[SECURITY] This kiosk uses a FULL-ACCESS service-account key (it can read every user).")
        print("           Switch to the kiosk identity: see README, 'Kiosk identity'.")
    except Exception as e:
        print(f"[FIREBASE ERROR] {e}")
        print("[FIREBASE ERROR] Running OFFLINE: QR codes will NOT be issued until this is fixed (see README).")

# ── YOLO ───────────────────────────────────────────────────────────────────────
# An OpenVINO export (README "Faster inference") is used when present; otherwise the PyTorch weights.
_OPENVINO_DIR = _APP_DIR / "best_openvino_model"
MODEL_FILE = str(_OPENVINO_DIR) if _OPENVINO_DIR.is_dir() else "best.pt"
print("[AI] Loading YOLO model...")
try:
    model = YOLO(MODEL_FILE)
    print(f"[AI] Loaded: {Path(MODEL_FILE).name}")
except Exception as e:
    print(f"[AI WARNING] {Path(MODEL_FILE).name} failed to load ({e}), falling back to yolov8n.pt")
    model = YOLO("yolov8n.pt")
    MODEL_FILE = "yolov8n.pt"
    print("[AI] Loaded: yolov8n.pt")


def _sha256(path):
    """Fingerprint of the weights (for an OpenVINO folder: its .bin file)."""
    p = Path(path)
    try:
        if p.is_dir():
            p = next(iter(sorted(p.glob("*.bin"))))
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except (OSError, StopIteration):
        return "unknown"


# Recorded on every detection so results can be traced to the exact weights (audit H-04 / M-21)
MODEL_SHA256 = _sha256(MODEL_FILE)

# ── Business rules and settings ────────────────────────────────────────────────
# Canonical business rules — single source of truth shared with Flutter and web (audit M-24)
_RULES_FILE = _APP_DIR / "config" / "rules.json"
_RULES = json.loads(_RULES_FILE.read_text(encoding="utf-8")) if _RULES_FILE.exists() else {}
QR_EXPIRY_MINUTES = _RULES.get("qrExpiryMinutes", 20)
# Kiosk settings (display, sorter, timeouts); ECOREWARD_KIOSK_SETTINGS points to another file.
SETTINGS_FILE = Path(os.environ.get("ECOREWARD_KIOSK_SETTINGS", "").strip()
                     or _APP_DIR / "config" / "kiosk_settings.json")
WIN_W, WIN_H = 1280, 720          # default canvas; --fullscreen uses the screen size
WINDOW = "ECOReward Bin"

# Kiosk identity: hostname-based so the web admin can tell which machine sent each heartbeat
KIOSK_NAME = platform.node() or "kiosk-default"
# With the kiosk identity the rules only let it write kiosks/{its own uid}; the PC name is stored as `name`.
KIOSK_ID = db.uid if DB_MODE == "kiosk" else KIOSK_NAME

POINTS_MAP = _RULES.get("points", {'Biodegradable': 15, 'Recyclable': 10, 'Residual': 5})
# Model label (lower-cased) -> waste category; reviewed list lives in config/waste_map.json (audit M-22)
WASTE_MAP = json.loads((_APP_DIR / "config" / "waste_map.json").read_text(encoding="utf-8"))["map"]
MIN_BOX_CONF = 0.30


def unmapped_labels(m):
    """Model class names that have no waste category. Non-empty => the loaded model does not match the map."""
    return sorted({str(n).lower() for n in m.names.values()} - set(WASTE_MAP))

# ── Firebase watcher ───────────────────────────────────────────────────────────
# The Firestore listener runs on its own thread and the UI loop in main() runs on this one.
# A threading.Event is the hand-off: the listener sets it, main() reads and clears it. (It used to
# be a module-global bool that main() re-assigned without `global`, which made it a separate local
# variable, so a scan was never noticed - audit C-04.)
_scan_event   = threading.Event()
_active_token = None   # token whose document is being watched; callbacks for other tokens are ignored
_watcher      = None   # holds active Firestore listener so we can unsubscribe

def _now() -> datetime:
    return datetime.now(timezone.utc)

def _to_utc_dt(value):
    """H-07: tolerant datetime parser — handles Admin SDK datetime objects and ISO string fallback."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            s = value.rstrip('Z').replace('+00:00', '').replace('+0000', '')
            return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _cleanup_stale_tokens():
    """On startup, expire any tokens that passed their expiry time.

    NOTE: Queries 'isUsed == False' (a scalar boolean) instead of
    'usedBy == []' — Firestore does not support equality on array fields.
    New tokens written by app.py include the isUsed flag.
    """
    if not db:
        return
    try:
        docs = db.collection('qr_tokens').where('isUsed', '==', False).stream()
        now = _now()
        for doc in docs:
            data = doc.to_dict()
            expires_dt = _to_utc_dt(data.get('expiresAt'))   # H-07: proper datetime comparison
            if expires_dt and now > expires_dt:
                doc.reference.update({'expired': True, 'isUsed': True})
                print(f"[CLEANUP] Expired stale token {doc.id[:8]}...")
    except Exception as e:
        print(f"[CLEANUP WARNING] {e}")

# Run cleanup in a background thread so startup is not blocked
# Only the legacy admin key may list tokens; the kiosk identity cannot (and expiry is enforced by the
# rules with server time anyway).
if DB_MODE == "admin":
    threading.Thread(target=_cleanup_stale_tokens, daemon=True).start()


def _start_heartbeat(interval=30):
    """3.4: write kiosks/{KIOSK_ID}.lastSeen now and every 30 s so the web admin can see liveness.
    Returns stop(), which also marks the kiosk offline (best effort, at most 3 s)."""
    if db is None:
        return lambda: None
    stop = threading.Event()

    def _write(status):
        try:
            db.collection("kiosks").document(KIOSK_ID).set({
                "lastSeen": SERVER_TS,
                "status":   status,
                "name":     KIOSK_NAME,
            }, merge=True)
        except Exception as e:
            _warn_once("heartbeat", f"[HEARTBEAT] could not update kiosks/{KIOSK_ID}: {e}")

    def _beat():
        _write("online")
        while not stop.wait(interval):
            _write("online")

    threading.Thread(target=_beat, daemon=True, name="kiosk-heartbeat").start()

    def _stop():
        stop.set()
        t = threading.Thread(target=_write, args=("offline",), daemon=True)
        t.start()
        t.join(timeout=3)
    return _stop


def watch_token(token):
    global _watcher, _active_token
    if not db:  # Firebase offline — nothing to watch
        return None
    # unsubscribe previous listener if any
    if _watcher:
        try:
            _watcher.unsubscribe()
        except Exception:
            pass
    _active_token = token
    _scan_event.clear()   # forget any scan of the previous QR before the new listener starts
    def on_snap(doc_snapshot, changes, read_time):
        for doc in doc_snapshot:
            # ignore late callbacks that belong to a QR we are no longer showing
            if doc.exists and doc.id == _active_token and len(doc.to_dict().get('usedBy', [])) > 0:
                _scan_event.set()
    _watcher = db.collection('qr_tokens').document(token).on_snapshot(on_snap)
    return _watcher


def stop_watching():
    """Stop listening/polling for the current QR (called when it is scanned, expires or is dismissed;
    otherwise the kiosk-identity poller would keep reading the old token until the next QR — audit K-10)."""
    global _watcher, _active_token
    if _watcher:
        try:
            _watcher.unsubscribe()
        except Exception:
            pass
    _watcher, _active_token = None, None


# ── Token issuance (runs on a worker thread) ──────────────────────────────────
def issue_token(waste_type, label, confidence):
    """Write qr_tokens/{uuid4} and the detection log; return what the QR screen needs. Raises on failure.

    Contract (Flutter validateAndUseQRToken, web manualClaimPoints, rules isNewKioskToken): payload
    ECOBIN:<Type>:<uuid4>; expiresAt a UTC datetime (stored as a Timestamp, so the rules can enforce expiry
    with server time); createdAt the server time; usedBy []; isUsed false; claimCode four digits.
    The QR is shown only after this write succeeded (audit C-03 / H-14)."""
    if db is None:
        raise RuntimeError("Firebase is offline")
    token = str(uuid.uuid4())
    expires_at = _now() + timedelta(minutes=QR_EXPIRY_MINUTES)
    claim_code = str(1000 + secrets.randbelow(9000))   # unpredictable manual-claim code
    payload = f"ECOBIN:{waste_type}:{token}"
    log(f"\n[QR GENERATED] {waste_type} +{POINTS_MAP[waste_type]}pts token={token[:8]}...")
    db.collection("qr_tokens").document(token).set({
        "wasteType": waste_type,
        "expiresAt": expires_at,            # H-07: Admin SDK stores UTC datetime as Timestamp
        "usedBy":    [],
        "isUsed":    False,
        "createdAt": SERVER_TS,             # H-07
        "claimCode": claim_code,
    })
    log("[FIREBASE] Token saved — waiting for scan...\n")
    try:   # 3.4 / M-21: traceable detection log; never blocks the QR
        db.collection("detections").add({
            "wasteType":   waste_type,
            "label":       label,
            "confidence":  float(confidence),
            "modelFile":   MODEL_FILE,
            "modelSha256": MODEL_SHA256,
            "timestamp":   SERVER_TS,
            "token":       token,
            "kioskId":     KIOSK_ID,
        })
    except Exception as e:
        _warn_once("detections", f"[FIREBASE] detection log not written: {e}")
    return {"token": token, "claim_code": claim_code, "expires_at": expires_at, "payload": payload}


def run_in_background(fn):
    """Network work never runs on the UI thread (audit K-07). Tests replace this with a direct call."""
    threading.Thread(target=fn, daemon=True, name="kiosk-worker").start()


def qr_modules(data):
    """The QR as one pixel per module (quiet zone included); screens scale it by a whole number."""
    qr = qrcode.QRCode(version=None, box_size=1, border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data)
    qr.make(fit=True)
    return np.where(np.array(qr.get_matrix(), dtype=bool), 0, 255).astype(np.uint8)


def generate_qr(data, size=220):
    """BGR QR image of about `size` pixels with sharp modules (kept for tools and older callers)."""
    m = qr_modules(data)
    k = max(1, size // m.shape[0])
    return cv2.cvtColor(cv2.resize(m, (m.shape[0] * k,) * 2, interpolation=cv2.INTER_NEAREST), cv2.COLOR_GRAY2BGR)


# ── Fill levels -> bins/{kioskId}-{bio|rec|res} for the web dashboard ────────
class BinsReporter:
    """Writes real ESP32 fill readings in the web's BinStatus shape. Throttled: on a change of 3+ points
    (at most once per `min_interval` s) or every 5 minutes. Simulated sorters never report."""
    KEYS = (("bio", "Biodegradable"), ("rec", "Recyclable"), ("res", "Residual"))

    def __init__(self, min_interval=60.0, refresh=300.0):
        self.min_interval, self.refresh = min_interval, refresh
        self.last, self.last_t = None, -1e9
        self.latest = None

    def offer(self, fill, now):
        levels = (fill.bio, fill.rec, fill.res)
        self.latest = levels
        if db is None:
            return False
        changed = self.last is None or max(abs(a - b) for a, b in zip(levels, self.last)) >= 3
        due = (changed and now - self.last_t >= self.min_interval) or now - self.last_t >= self.refresh
        if not due:
            return False
        self.last, self.last_t = levels, now
        run_in_background(lambda: self._write(levels))
        return True

    def _write(self, levels):
        try:
            for (suffix, name), level in zip(self.KEYS, levels):
                db.collection("bins").document(f"{KIOSK_ID}-{suffix}").set({
                    "location":    f"{KIOSK_NAME} · {name}",
                    "fillLevel":   int(level),
                    "status":      "online",
                    "lastUpdated": SERVER_TS,
                })
        except Exception as e:
            _warn_once("bins", f"[BINS] fill levels not written: {e}")


# ── Detection ─────────────────────────────────────────────────────────────────
def detect(frame):
    """The most confident box at or above MIN_BOX_CONF in this frame, or None (audit M-20: one vote per
    frame, so several weak boxes of one class cannot outvote a confident box of another)."""
    results = model(frame, conf=0.15, verbose=False)
    box, conf = None, 0.0
    for r in results:
        for b in r.boxes:
            c = float(b.conf[0])
            if c >= MIN_BOX_CONF and c > conf:
                box, conf = b, c
    if box is None:
        return None
    label = str(model.names[int(box.cls[0])]).lower()
    return Detection(label, WASTE_MAP.get(label), conf, tuple(float(v) for v in box.xyxy[0]))


# ── Camera (reconnects instead of exiting — audit K-08) ───────────────────────
class Camera:
    def __init__(self, index, retry_s=3.0, fail_limit=30):
        self.index, self.retry_s, self.fail_limit = index, retry_s, fail_limit
        self.cap, self.last, self.fails, self.next_try = None, None, 0, 0.0
        self.ok, self.ever_opened = False, False
        self.fps, self._t_prev = 0.0, None

    def read(self, now):
        """(frame to show or None, fresh: bool). A short glitch keeps showing the last frame."""
        if self.cap is None:
            if now < self.next_try:
                return None, False
            self._open(now)
            if self.cap is None:
                return None, False
        ret, frame = self.cap.read()
        if not ret or frame is None:
            self.fails += 1
            if self.fails >= self.fail_limit:
                log("[CAMERA] Lost feed — re-opening…")
                self.release()
                self.next_try = now
                return None, False
            return self.last, False
        if self._t_prev is not None and now > self._t_prev:
            inst = 1.0 / (now - self._t_prev)
            self.fps = inst if not self.fps else 0.9 * self.fps + 0.1 * inst
        self._t_prev = now
        self.fails, self.ok, self.last = 0, True, frame
        return frame, True

    def _open(self, now):
        cap = cv2.VideoCapture(self.index)
        if cap is not None and cap.isOpened():
            self.cap, self.fails = cap, 0
            log(f"[CAMERA] {'Re-opened' if self.ever_opened else 'Opened'} camera {self.index}.")
            self.ever_opened = True
            return
        try:
            cap.release()
        except Exception:
            pass
        self.ok, self.next_try = False, now + self.retry_s
        _warn_once("camera", f"[CAMERA ERROR] Could not access webcam {self.index} — retrying every "
                             f"{self.retry_s:g} s.", every=300)

    def release(self):
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
        self.cap, self.ok, self._t_prev = None, False, None


# ── Screens (thin wrappers so tools and tests can draw one screen) ────────────
_renderer = None


def _get_renderer(width=None, height=None, reduce_motion=False):
    global _renderer
    w, h = width or WIN_W, height or WIN_H
    if _renderer is None or (width and (_renderer.size != (w, h) or _renderer.m.reduce_motion != reduce_motion)):
        _renderer = Renderer(w, h, reduce_motion)
    return _renderer


def _default_view(waste_type=None, label=None, confidence=0.0, fb_ok=True):
    det = Detection(label, waste_type, confidence) if label and label != "Unknown" else None
    return View(phase="detected" if det else "ready", online=fb_ok, detection=det, points=POINTS_MAP,
                clock=_clock_text())


def draw_camera_hud(cam, label, waste_type, confidence, fb_ok, scan_count, view=None, now=None):
    """Ready / detected / working screens (camera on the left, stage panel on the right)."""
    v = view or _default_view(waste_type, label, confidence, fb_ok)
    return _get_renderer().render(cam, v, time.monotonic() if now is None else now, transition=view is not None)


def draw_qr_panel(cam_frame, qr_img, waste_type, points, secs_left, claim_code, total_secs, view=None, now=None):
    """QR screen: the code, its countdown, and the claim-code receipt for customers without a phone."""
    v = view or View(phase="qr", points=POINTS_MAP, secs_left=secs_left, total_secs=total_secs, clock=_clock_text())
    return _get_renderer().render(cam_frame, v, time.monotonic() if now is None else now,
                                  transition=view is not None)


def draw_success(cam_frame, waste_type, points, view=None, now=None):
    """The 'Claimed!' moment after the phone took the code."""
    v = view or View(phase="complete", points=POINTS_MAP, clock=_clock_text())
    return _get_renderer().render(cam_frame, v, time.monotonic() if now is None else now,
                                  transition=view is not None)


def draw_splash_animation(window_name, reduce_motion=False):
    """A short (about 0.5 s) brand fade-in while the camera starts. One still frame with reduced motion."""
    r = _get_renderer(reduce_motion=reduce_motion)
    frames = 1 if r.m.reduce_motion else 24
    for i in range(frames):
        cv2.imshow(window_name, r.splash((i + 1) / frames))
        if cv2.waitKey(20) & 0xFF == ord('q'):
            cv2.destroyAllWindows()
            sys.exit(0)


def _clock_text(dt=None):
    return (dt or datetime.now()).strftime("%I:%M %p").lstrip("0")


def _local_time(dt_utc):
    return _clock_text(dt_utc.astimezone()) if dt_utc else ""


# ── Main loop ─────────────────────────────────────────────────────────────────
SOUNDS = {"detect": [(880, 120)], "success": [(1046, 110), (1318, 160)], "error": [(330, 260)]}


def _screen_size():
    """Physical screen size; makes the process DPI-aware first so Windows does not upscale the frame."""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
        u = ctypes.windll.user32
        return int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))
    except Exception:
        return WIN_W, WIN_H


def _diagnostics(settings, flow, sorter, camera, bins, infer_ms, render_ms, started, log_path, bad_labels, renderer):
    up = int(time.monotonic() - started)
    shape = camera.last.shape if camera.last is not None else None
    firestore_mode = {"kiosk": "kiosk identity", "admin": "admin key (legacy)"}.get(DB_MODE, "offline")
    mapped = "all mapped" if not bad_labels else f"{len(bad_labels)} unmapped"
    rows = [
        ("Kiosk", [("Name", KIOSK_NAME), ("Firestore", firestore_mode),
                   ("Uptime", f"{up // 3600} h {up % 3600 // 60} min"),
                   ("Log", Path(log_path).name if log_path else "off")]),
        ("Model", [("File", Path(MODEL_FILE).name), ("SHA-256", MODEL_SHA256[:12] + "…"),
                   ("Classes", f"{len(model.names)} · {mapped}"),
                   ("Rule", f"conf ≥ {MIN_BOX_CONF:.2f} · 7-frame vote"), ("Inference", f"{infer_ms:.0f} ms")]),
        ("Camera", [("Index", settings.camera_index), ("Frame", f"{shape[1]}×{shape[0]}" if shape else "—"),
                    ("State", "ok" if camera.ok else "reconnecting"),
                    ("Display", f"{camera.fps:.1f} FPS · {render_ms:.1f} ms draw")]),
        ("Sorter", [("Mode", sorter.mode + (" (simulated)" if sorter.simulated else "")), ("Port", sorter.port or "—"),
                    ("State", f"{sorter.state.value}{' · ' + sorter.detail if sorter.detail else ''}"),
                    ("Unparsed lines", sorter.unparsed), ("Recent", " | ".join(list(sorter.recent)[-3:]) or "—")]),
        ("Transaction", [("Phase", flow.phase.value), ("Claimed this session", flow.completed),
                         ("Fonts", renderer.ts.source), ("Screen", f"{renderer.size[0]}×{renderer.size[1]}")]),
    ]
    if bins.latest:
        rows.append(("Fill levels (ESP32)", [(n, f"{v} %") for (_, n), v in zip(BinsReporter.KEYS, bins.latest)]))
    return rows


def _view(flow, settings, sorter, camera, overlay, modules_cache, bad_labels, now, wall, diagnostics):
    tx = flow.tx
    age = now - flow.since
    expires = tx.expires_at if tx else None
    return View(
        phase=flow.phase.value, mode=settings.sorter, online=db is not None, model_ok=not bad_labels,
        camera_ok=camera.ok, sorter_state=sorter.state.value, sorter_detail=sorter.detail, sorter_ready=sorter.ready,
        detection=flow.current, latest=flow.latest, lock_progress=flow.lock_progress, armed=flow.armed,
        stages=flow.stages(), tx=tx, points=POINTS_MAP,
        qr_modules=modules_cache.get(tx.token) if tx else None,
        secs_left=max(0, int((expires - wall).total_seconds())) if expires else 0,
        total_secs=QR_EXPIRY_MINUTES * 60,
        expires_local=_local_time(expires),
        issued_local=_local_time(expires - timedelta(minutes=QR_EXPIRY_MINUTES)) if expires else "",
        issued_date=expires.astimezone().strftime("%b %d, %Y") if expires else "",
        failure=flow.failure, toast=flow.toast, phase_age=age, completed=flow.completed,
        history=list(flow.history), overlay=overlay, debug=settings.debug, clock=_clock_text(),
        seconds_to_ready=max(0.0, (settings.success_seconds if flow.phase == Phase.COMPLETE
                                   else settings.failure_seconds) - age),
        diagnostics=diagnostics,
    )


def main(argv=None):
    """argv=None: settings file only (tests); `python app.py` passes the command line."""
    settings, problems = load_settings(SETTINGS_FILE, argv)
    log_path = _setup_file_log(settings.log_dir)
    for p in problems:
        log(f"[CONFIG] {p}")
    width, height = _screen_size() if settings.fullscreen else (settings.width, settings.height)
    renderer = _get_renderer(width, height, settings.reduce_motion)
    sorter = make_sorter(settings.sorter, settings.serial_port, settings.serial_baud, log=log)
    flow = TransactionFlow(FlowConfig(
        mode=settings.sorter, points=POINTS_MAP, sort_ack_timeout=settings.sort_ack_timeout,
        drop_confirm_timeout=settings.drop_confirm_timeout, issue_timeout=settings.issue_timeout,
        qr_display_seconds=settings.qr_display_seconds, success_seconds=settings.success_seconds,
        failure_seconds=settings.failure_seconds, lock_votes=settings.lock_votes,
        rearm_clear_frames=settings.rearm_clear_frames, min_drop_grams=settings.min_drop_grams,
        debug=settings.debug))
    camera = Camera(settings.camera_index)
    bins = BinsReporter(settings.bins_min_interval)
    results = queue.Queue()
    clicks = deque(maxlen=8)

    log("\n------------------------------------------")
    log(f"  ECOReward Bin  |  {width}x{height}  |  sorter: {settings.sorter}"
        f"{' (SIMULATED)' if sorter.simulated else ''}")
    log("  SPACE confirm (staff)   R receipt   H history   D diagnostics   Q quit")
    log("------------------------------------------\n")

    cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WINDOW, width, height)
    if settings.fullscreen:
        try:   # WINDOW_FULLSCREEN is a *property* value; passed to namedWindow it means AUTOSIZE (audit K-04)
            cv2.setWindowProperty(WINDOW, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        except cv2.error:
            pass

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONUP:
            clicks.append((x, y))
    try:
        cv2.setMouseCallback(WINDOW, on_mouse)
    except cv2.error:
        pass

    draw_splash_animation(WINDOW)
    stop_heartbeat = _start_heartbeat()

    # (audit M-22) A model whose classes are not all in waste_map.json would silently score
    # unknown objects as Residual — e.g. the yolov8n.pt fallback sees a "person". Refuse QRs instead.
    bad_labels = unmapped_labels(model)
    if bad_labels:
        log(f"[AI ERROR] {MODEL_FILE} has {len(bad_labels)} class(es) missing from config/waste_map.json "
            f"(e.g. {', '.join(bad_labels[:5])}). QR codes are DISABLED until the map or model is fixed.")

    modules_cache = {}
    overlay = None
    started = time.monotonic()
    infer_ms = render_ms = 0.0
    seen_window = False
    display = None

    def play(kind):
        if settings.sounds:
            def go():
                for f, d in SOUNDS[kind]:
                    _beep(f, d)
            threading.Thread(target=go, daemon=True).start()

    def run(actions, now):
        for a in actions:
            if isinstance(a, SendSort):
                if not sorter.send_sort(a.waste_type):
                    run(flow.on_sorter_event(DeviceError("The sorter is not connected"), now), now)
            elif isinstance(a, IssueToken):
                _scan_event.clear()   # new QR: forget any scan signal left over from the previous one

                def work(a=a):
                    try:
                        results.put((a.tx_id, True, issue_token(a.waste_type, a.label, a.confidence)))
                    except Exception as err:
                        log(f"[FIREBASE ERROR] Could not store token — QR not shown: {err}\n")
                        results.put((a.tx_id, False, str(err)))
                run_in_background(work)
            elif isinstance(a, WatchToken):
                watch_token(a.token)
            elif isinstance(a, StopWatching):
                stop_watching()
            elif isinstance(a, Sound):
                play(a.kind)
        for line in flow.notices:
            log(line)
        flow.notices.clear()

    def handle(action, now):
        nonlocal overlay
        renderer.ui.pressed = (action, now)
        if action == "receipt" and flow.phase == Phase.QR:
            overlay = "receipt"
        elif action == "close":
            overlay = None
        elif action == "dismiss":
            overlay = None
            run(flow.dismiss(now), now)

    try:
        while True:
            now, wall = time.monotonic(), _now()
            try:
                frame, fresh = camera.read(now)
                flow.set_context(online=db is not None, model_ok=not bad_labels, sorter_ready=sorter.ready)
                while True:                                   # token writes finished on the worker
                    try:
                        tx_id, ok, data = results.get_nowait()
                    except queue.Empty:
                        break
                    if ok:
                        run(flow.on_issue_ok(tx_id, data["token"], data["claim_code"], data["expires_at"],
                                             data["payload"], now), now)
                    else:
                        run(flow.on_issue_failed(tx_id, data, now), now)
                for ev in sorter.poll(now):
                    if isinstance(ev, Fill):
                        if not sorter.simulated:
                            bins.offer(ev, now)
                        continue
                    run(flow.on_sorter_event(ev, now), now)
                if flow.phase == Phase.QR and _scan_event.is_set():
                    _scan_event.clear()
                    run(flow.on_scanned(_active_token, now), now)
                run(flow.tick(now, wall), now)
                if fresh and flow.phase in (Phase.READY, Phase.DETECTED):
                    t0 = time.perf_counter()
                    try:
                        det = detect(frame)
                    except Exception as e:
                        det = None
                        _warn_once("inference", f"[AI ERROR] inference failed: {e}", every=60)
                    infer_ms = 0.8 * infer_ms + 0.2 * (time.perf_counter() - t0) * 1000
                    run(flow.observe(det, now), now)
                if overlay == "receipt" and flow.phase != Phase.QR:
                    overlay = None

                tx = flow.tx
                if tx is not None and tx.payload and tx.token not in modules_cache:
                    modules_cache.clear()
                    modules_cache[tx.token] = qr_modules(tx.payload)
                diag = (_diagnostics(settings, flow, sorter, camera, bins, infer_ms, render_ms, started, log_path,
                                     bad_labels, renderer) if overlay == "diagnostics" else [])
                v = _view(flow, settings, sorter, camera, overlay, modules_cache, bad_labels, now, wall, diag)
                t0 = time.perf_counter()
                if flow.phase == Phase.QR:
                    display = draw_qr_panel(frame, None, tx.waste_type, POINTS_MAP[tx.waste_type], v.secs_left,
                                            tx.claim_code, v.total_secs, view=v, now=now)
                elif flow.phase == Phase.COMPLETE:
                    display = draw_success(frame, tx.waste_type, POINTS_MAP[tx.waste_type], view=v, now=now)
                else:
                    cur = flow.current
                    display = draw_camera_hud(frame, cur.label if cur else "Unknown", cur.waste_type if cur else None,
                                              cur.confidence if cur else 0.0, db is not None, flow.completed,
                                              view=v, now=now)
                render_ms = 0.9 * render_ms + 0.1 * (time.perf_counter() - t0) * 1000
            except Exception as e:                             # one bad frame must not end the kiosk (K-09)
                _warn_once(f"loop:{type(e).__name__}", f"[ERROR] {e}\n{traceback.format_exc()}", every=60)
                if display is None:
                    display = np.full((height, width, 3), 236, np.uint8)

            cv2.imshow(WINDOW, display)
            key = cv2.waitKey(1) & 0xFF                     # outside the guard: Ctrl+C and test limits still stop us

            try:
                while clicks:
                    x, y = clicks.popleft()
                    action = renderer.hit(x, y)
                    if action:
                        handle(action, now)
                if key == 32:                                 # SPACE: staff confirm (or debug sort)
                    run(flow.confirm(now), now)
                elif key in (ord('r'), ord('R')) and flow.phase == Phase.QR:
                    overlay = None if overlay == "receipt" else "receipt"
                elif key in (ord('h'), ord('H')):
                    overlay = None if overlay == "history" else "history"
                elif key in (ord('d'), ord('D')):
                    overlay = None if overlay == "diagnostics" else "diagnostics"
                elif key == 27:
                    overlay = None
            except Exception as e:
                _warn_once(f"keys:{type(e).__name__}", f"[ERROR] {e}\n{traceback.format_exc()}", every=60)

            if key in (ord('q'), ord('Q')):
                break
            try:                                              # the window's close button ends the kiosk too
                visible = cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE)
                seen_window = seen_window or visible >= 1
                if seen_window and visible < 1:
                    break
            except cv2.error:
                pass
    finally:
        sorter.close()
        camera.release()
        stop_watching()
        stop_heartbeat()
        cv2.destroyAllWindows()
        log("\n[SYSTEM] Shut down.")


if __name__ == '__main__':
    main(sys.argv[1:])
