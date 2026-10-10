# -*- coding: utf-8 -*-
"""ECOReward Bin — kiosk program.

Usage:
    python app.py                         # full hardware mode
    python app.py --no-hardware           # simulate ESP32 + load cell (demo / CI)
    python app.py --debug                 # re-enable SPACEBAR manual trigger
    python app.py --port COM4 --baud 9600 # override serial defaults
    python app.py --width 1024 --height 600  # override window size
"""

import argparse
import hashlib
import json
import os
import platform
import queue
import sys
import threading
import time
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from pathlib import Path

import cv2
import numpy as np
import qrcode
import secrets
import uuid
from firebase_admin import credentials, firestore
import firebase_admin

# kiosk_firestore.py sits next to this file
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from kiosk_firestore import KioskFirestore, SERVER_TIMESTAMP as KIOSK_SERVER_TIMESTAMP  # noqa: E402

# ── CLI args ───────────────────────────────────────────────────────────────────
_parser = argparse.ArgumentParser(description="ECOReward Bin kiosk")
_parser.add_argument("--no-hardware", action="store_true",
                     help="simulate ESP32 and load cell (no serial port needed)")
_parser.add_argument("--debug", action="store_true",
                     help="re-enable SPACEBAR to manually trigger the sort flow")
_parser.add_argument("--port", default=os.environ.get("ECOREWARD_SERIAL_PORT", "COM3"),
                     help="serial port for the ESP32 (default: COM3 or $ECOREWARD_SERIAL_PORT)")
_parser.add_argument("--baud", type=int,
                     default=int(os.environ.get("ECOREWARD_SERIAL_BAUD", "115200")),
                     help="baud rate (default 115200)")
_parser.add_argument("--width",  type=int, default=0, help="window width  (0 = auto-detect)")
_parser.add_argument("--height", type=int, default=0, help="window height (0 = auto-detect)")
ARGS = _parser.parse_args()

# ── Platform-safe beep ────────────────────────────────────────────────────────
if platform.system() == "Windows":
    import winsound
    def _beep(freq=880, dur=120):
        winsound.Beep(freq, dur)
else:
    def _beep(freq=880, dur=120):
        pass

# ── Firebase ───────────────────────────────────────────────────────────────────
_APP_DIR     = Path(__file__).resolve().parent
_DEFAULT_KEY = Path.home() / ".ecoreward" / "kiosk-sa.json"
_LEGACY_KEYS = ("serviceAccountKey.json", "serviceAccountKey.json.json")


def find_service_account():
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


_KIOSK_CONFIG = Path(os.environ.get("ECOREWARD_KIOSK_CONFIG", "").strip()
                     or Path.home() / ".ecoreward" / "kiosk.json").expanduser()

db = None
DB_MODE = None
SERVER_TS = firestore.SERVER_TIMESTAMP
if _KIOSK_CONFIG.is_file():
    try:
        db = KioskFirestore.from_config(_KIOSK_CONFIG)
        db.check()
        DB_MODE, SERVER_TS = "kiosk", KIOSK_SERVER_TIMESTAMP
        print("[FIREBASE] Connected as the kiosk identity (least privilege).")
    except Exception as e:
        db = None
        print(f"[FIREBASE ERROR] Kiosk identity failed: {e}")
        print("[FIREBASE ERROR] Running OFFLINE.")
else:
    try:
        _key, _tried = find_service_account()
        if _key is None:
            raise FileNotFoundError("Firebase credential not found. Looked in:\n    " + "\n    ".join(_tried))
        if _APP_DIR in _key.resolve().parents:
            print("[SECURITY WARNING] The Firebase key is inside the project folder.")
            print(f"                   Move it to {_DEFAULT_KEY} and rotate it.")
        cred = credentials.Certificate(str(_key))
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        db = firestore.client()
        DB_MODE = "admin"
        print("[FIREBASE] Connected (admin mode).")
    except Exception as e:
        print(f"[FIREBASE ERROR] {e}")
        print("[FIREBASE ERROR] Running OFFLINE.")

# ── YOLO — tries OpenVINO export first, falls back to PyTorch  # FIX-M-02 ────
from ultralytics import YOLO

print("[AI] Loading YOLO model...")
_OV_PATH = _APP_DIR / "best_openvino_model"
try:
    if _OV_PATH.exists():
        model = YOLO(str(_OV_PATH))
        MODEL_FILE = str(_OV_PATH)
        print(f"[AI] Loaded OpenVINO model: {_OV_PATH}")
    else:
        model = YOLO("best.pt")
        MODEL_FILE = "best.pt"
        print("[AI] Loaded PyTorch model: best.pt  (export to OpenVINO for faster CPU inference)")
except Exception as e:
    print(f"[AI WARNING] best.pt failed ({e}), falling back to yolov8n.pt")
    model = YOLO("yolov8n.pt")
    MODEL_FILE = "yolov8n.pt"


def _sha256(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except (OSError, IsADirectoryError):
        return "unknown"


MODEL_SHA256 = _sha256(MODEL_FILE)

# ── Layout  # FIX-L-01 ────────────────────────────────────────────────────────
# Prefer explicit CLI args; fall back to screen size; fall back to 1280×720.
def _screen_size():
    try:
        import ctypes
        u = ctypes.windll.user32
        return u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    except Exception:
        return 1280, 720


_sw, _sh = _screen_size()
WIN_W  = ARGS.width  if ARGS.width  > 0 else _sw
WIN_H  = ARGS.height if ARGS.height > 0 else _sh
CAM_W  = int(WIN_W * 0.64)
PANEL_W = WIN_W - CAM_W
MARGIN  = 20

# Load canonical business rules
_RULES_FILE = _APP_DIR / "config" / "rules.json"
_RULES = json.loads(_RULES_FILE.read_text(encoding="utf-8")) if _RULES_FILE.exists() else {}
QR_EXPIRY_MINUTES = _RULES.get("qrExpiryMinutes", 20)

KIOSK_NAME = platform.node() or "kiosk-default"
KIOSK_ID   = db.uid if DB_MODE == "kiosk" else KIOSK_NAME

# ── Palette BGR ────────────────────────────────────────────────────────────────
BG        = (  8,  10,  14)
CARD      = ( 18,  22,  28)
CARD2     = ( 26,  31,  40)
BORDER    = ( 38,  44,  54)
ACCENT    = ( 80, 200, 120)
WHITE     = (255, 255, 255)
OFF_WHITE = (200, 205, 215)
GRAY      = (100, 108, 120)
GRAY_DIM  = ( 38,  44,  54)
BLACK     = (  0,   0,   0)
AMBER     = ( 40, 180, 255)
RED_SOFT  = ( 70,  70, 210)

WASTE_COLOR = {
    "Biodegradable": ( 60, 180,  80),
    "Recyclable":    ( 30, 160, 255),
    "Residual":      ( 80,  80, 200),
}
WASTE_EMOJI_LABEL = {
    "Biodegradable": "BIODEGRADABLE",
    "Recyclable":    "RECYCLABLE",
    "Residual":      "RESIDUAL",
}
WASTE_TAG = {"Biodegradable": "BIO", "Recyclable": "REC", "Residual": "RES"}

POINTS_MAP   = _RULES.get("points", {"Biodegradable": 15, "Recyclable": 10, "Residual": 5})
WASTE_MAP    = json.loads((_APP_DIR / "config" / "waste_map.json").read_text(encoding="utf-8"))["map"]
MIN_BOX_CONF = 0.30
MIN_WEIGHT_G = 5     # grams — item must weigh at least this after drop to count

# ASSUMPTION: ESP32 sends servo commands '1', '2', '3' (one ASCII digit + newline).
# Commands: 1 = Biodegradable, 2 = Recyclable, 3 = Residual.
SERVO_CMD = {"Biodegradable": b"1\n", "Recyclable": b"2\n", "Residual": b"3\n"}


def unmapped_labels(m):
    return sorted({str(n).lower() for n in m.names.values()} - set(WASTE_MAP))


F  = cv2.FONT_HERSHEY_SIMPLEX
FB = cv2.FONT_HERSHEY_DUPLEX

# ── Drawing primitives ─────────────────────────────────────────────────────────
def blend(img, x1, y1, x2, y2, color, alpha):
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(img.shape[1], x2), min(img.shape[0], y2)
    if x2 <= x1 or y2 <= y1:
        return
    roi = img[y1:y2, x1:x2]
    ov  = roi.copy()
    ov[:] = color
    cv2.addWeighted(ov, alpha, roi, 1 - alpha, 0, roi)
    img[y1:y2, x1:x2] = roi

def rrect(img, x1, y1, x2, y2, r, color, t=-1):
    r = max(0, min(r, (x2 - x1) // 2, (y2 - y1) // 2))
    if t == -1:
        cv2.rectangle(img, (x1 + r, y1), (x2 - r, y2), color, -1)
        cv2.rectangle(img, (x1, y1 + r), (x2, y2 - r), color, -1)
        for cx, cy in [(x1+r, y1+r), (x2-r, y1+r), (x1+r, y2-r), (x2-r, y2-r)]:
            cv2.circle(img, (cx, cy), r, color, -1)
    else:
        cv2.rectangle(img, (x1 + r, y1), (x2 - r, y2), color, t)
        cv2.rectangle(img, (x1, y1 + r), (x2, y2 - r), color, t)
        for cx, cy in [(x1+r, y1+r), (x2-r, y1+r), (x1+r, y2-r), (x2-r, y2-r)]:
            cv2.ellipse(img, (cx, cy), (r, r), 0, 0, 360, color, t)

def putc(img, text, cx, y, font, scale, color, thick=1):
    (tw, _), _ = cv2.getTextSize(text, font, scale, thick)
    cv2.putText(img, text, (cx - tw // 2, y), font, scale, color, thick, cv2.LINE_AA)

def putr(img, text, rx, y, font, scale, color, thick=1):
    (tw, _), _ = cv2.getTextSize(text, font, scale, thick)
    cv2.putText(img, text, (rx - tw, y), font, scale, color, thick, cv2.LINE_AA)

def putl(img, text, lx, y, font, scale, color, thick=1):
    cv2.putText(img, text, (lx, y), font, scale, color, thick, cv2.LINE_AA)

def arc(img, cx, cy, r, frac, color, bg=GRAY_DIM, thick=8):
    cv2.ellipse(img, (cx, cy), (r, r), -90, 0, 360, bg, thick, cv2.LINE_AA)
    if frac > 0:
        cv2.ellipse(img, (cx, cy), (r, r), -90, 0, int(360 * frac), color, thick, cv2.LINE_AA)

def vgrad(img, x1, y1, x2, y2, c_top, c_bot):
    for y in range(y1, y2):
        t   = (y - y1) / max(y2 - y1, 1)
        col = tuple(int(c_top[i] * (1 - t) + c_bot[i] * t) for i in range(3))
        cv2.line(img, (x1, y), (x2, y), col, 1)

def divider(img, x1, x2, y, color=BORDER):
    cv2.line(img, (x1, y), (x2, y), color, 1)

def dot_divider(img, x1, x2, y, color=BORDER):
    for dx in range(x1, x2, 10):
        cv2.line(img, (dx, y), (dx + 5, y), color, 1)

def generate_qr(data, size=220):
    box = max(4, size // 35)
    qr  = qrcode.QRCode(version=1, box_size=box, border=4,
                        error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    arr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    return cv2.resize(arr, (size, size), interpolation=cv2.INTER_NEAREST)

# ── State machine  # FIX-H-01 ─────────────────────────────────────────────────
class KioskState(Enum):
    IDLE     = auto()   # camera + detection running; waiting for stable label
    SORTING  = auto()   # servo command sent to ESP32; waiting for OK ack
    WEIGHING = auto()   # chute open; waiting for W:<grams> confirmation
    WRITING  = auto()   # Firestore write in-flight
    QR_ACTIVE = auto()  # QR on screen; waiting for Flutter scan
    SUCCESS  = auto()   # brief success screen

# ── Serial thread  # FIX-C-01 ─────────────────────────────────────────────────
# Queues that flow between the serial thread and the main loop.
_serial_rx: "queue.Queue[str]" = queue.Queue()   # lines from ESP32 → main loop
_serial_tx: "queue.Queue[bytes]" = queue.Queue() # bytes from main loop → ESP32

_ser = None   # live serial.Serial object; None when unavailable

def _serial_thread(port, baud):
    """Reads/writes the ESP32 over serial. Reconnects silently on disconnect."""
    global _ser
    try:
        import serial as _serial_mod
    except ImportError:
        print("[SERIAL ERROR] PySerial is not installed. Run: pip install pyserial")
        return

    while True:
        try:
            _ser = _serial_mod.Serial(port, baud, timeout=0.1)
            print(f"[SERIAL] Connected on {port} at {baud} baud.")
            while True:
                # --- TX ---
                try:
                    data = _serial_tx.get_nowait()
                    _ser.write(data)
                except queue.Empty:
                    pass
                # --- RX ---
                line = _ser.readline()
                if line:
                    decoded = line.decode("ascii", errors="replace").strip()
                    if decoded:
                        _serial_rx.put(decoded)
        except Exception as e:
            _ser = None
            print(f"[SERIAL] Lost connection ({e}). Retrying in 3 s…")
            time.sleep(3)


def _send_servo(waste_type: str):
    """Queue a servo command.  # FIX-C-01"""
    cmd = SERVO_CMD.get(waste_type)
    if cmd:
        _serial_tx.put(cmd)
        print(f"[SERIAL] → servo {cmd.strip().decode()} ({waste_type})")


# ── Mock hardware for --no-hardware  # FIX-M-03 ──────────────────────────────
_mock_queue: "queue.Queue[str]" = queue.Queue()

def _mock_sort_sequence(waste_type: str):
    """Simulates ESP32 responses when --no-hardware is active."""
    def _run():
        time.sleep(0.6)
        _mock_queue.put("OK")           # servo ack
        time.sleep(1.0)
        _mock_queue.put("W:42")         # load-cell drop confirmation
    threading.Thread(target=_run, daemon=True).start()


def _drain_rx() -> list:
    """Return all currently available lines from ESP32 (or mock)."""
    lines = []
    src = _mock_queue if ARGS.no_hardware else _serial_rx
    while True:
        try:
            lines.append(src.get_nowait())
        except queue.Empty:
            break
    return lines

# ── Camera HUD ─────────────────────────────────────────────────────────────────
def draw_camera_hud(cam, label, waste_type, confidence, fb_ok, scan_count, state, status_msg=""):
    h, w = cam.shape[:2]
    out  = cam.copy()
    acc  = WASTE_COLOR.get(waste_type, ACCENT)

    blend(out, 0, 0, w, 52, BG, 0.90)
    rrect(out, 12, 10, 130, 42, 8, CARD2)
    putl(out, "ECO",    20, 34, FB, 0.62, ACCENT, 2)
    putl(out, "REWARD", 58, 34, FB, 0.62, WHITE,  1)

    _t = datetime.now()
    putc(out, _t.strftime("%I:%M %p"), w // 2, 32, FB, 0.52, OFF_WHITE, 1)
    putc(out, _t.strftime("%A, %b %d"), w // 2, 46, F, 0.30, GRAY, 1)

    fb_color = ACCENT if fb_ok else RED_SOFT
    fb_text  = "LIVE" if fb_ok else "OFFLINE"
    (ftw, _), _ = cv2.getTextSize(fb_text, F, 0.30, 1)
    fx = w - ftw - 36
    rrect(out, fx - 14, 10, fx + ftw + 10, 30, 6, CARD2)
    cv2.circle(out, (fx - 6, 20), 3, fb_color, -1, cv2.LINE_AA)
    putl(out, fb_text, fx, 26, F, 0.30, fb_color, 1)

    sc_text = f"SCANS  {scan_count}"
    (stw, _), _ = cv2.getTextSize(sc_text, F, 0.30, 1)
    sx = fx - stw - 36
    rrect(out, sx - 10, 10, sx + stw + 10, 30, 6, CARD2)
    putl(out, sc_text, sx, 26, F, 0.30, AMBER, 1)

    cv2.line(out, (0, 52), (w, 52), BORDER, 1)

    if not fb_ok:
        blend(out, 0, 53, w, 85, RED_SOFT, 0.88)
        putc(out, "FIREBASE OFFLINE  -  QR codes disabled",
             w // 2, 75, F, 0.46, WHITE, 1)

    # Status banner for SORTING / WEIGHING states
    if state == KioskState.SORTING:
        blend(out, 0, 86, w, 118, (20, 80, 20), 0.90)
        putc(out, "Sorting…  Sending item to the correct chute",
             w // 2, 108, F, 0.44, ACCENT, 1)
    elif state == KioskState.WEIGHING:
        blend(out, 0, 86, w, 118, (20, 60, 80), 0.90)
        putc(out, "Confirming drop…  Please wait",
             w // 2, 108, F, 0.44, AMBER, 1)
    elif state == KioskState.WRITING:
        blend(out, 0, 86, w, 118, (20, 20, 80), 0.90)
        putc(out, "Saving token…",
             w // 2, 108, F, 0.44, WHITE, 1)
    elif status_msg:
        blend(out, 0, 86, w, 118, RED_SOFT, 0.75)
        putc(out, status_msg, w // 2, 108, F, 0.40, WHITE, 1)

    # Scan zone brackets
    cx, cy = w // 2, h // 2
    bx, by = 190, 160
    x1, y1, x2, y2 = cx - bx, cy - by, cx + bx, cy + by
    blend(out, 0,  0,  w,  y1, BLACK, 0.35)
    blend(out, 0,  y2, w,  h,  BLACK, 0.35)
    blend(out, 0,  y1, x1, y2, BLACK, 0.35)
    blend(out, x2, y1, w,  y2, BLACK, 0.35)
    cl, lw = 28, 3
    for px, py, dx, dy in [(x1, y1, 1, 1), (x2, y1, -1, 1),
                            (x1, y2, 1, -1), (x2, y2, -1, -1)]:
        cv2.line(out, (px, py), (px + dx * cl, py), acc, lw, cv2.LINE_AA)
        cv2.line(out, (px, py), (px, py + dy * cl), acc, lw, cv2.LINE_AA)
    blend(out, x1, y1, x2, y2, acc, 0.03)
    cv2.line(out, (cx - 8, cy), (cx + 8, cy), acc, 1, cv2.LINE_AA)
    cv2.line(out, (cx, cy - 8), (cx, cy + 8), acc, 1, cv2.LINE_AA)

    card_h = 88
    blend(out, 0, h - card_h, w, h, BG, 0.92)
    cv2.line(out, (0, h - card_h), (w, h - card_h), BORDER, 1)

    if label != "Unknown" and confidence > 0:
        cv2.rectangle(out, (0, h - card_h), (4, h), acc, -1)
        badge_text = f"  {WASTE_EMOJI_LABEL[waste_type]}  "
        (bw2, bh2), _ = cv2.getTextSize(badge_text, FB, 0.58, 2)
        bx2 = 18
        rrect(out, bx2, h - card_h + 12, bx2 + bw2 + 4, h - card_h + 12 + bh2 + 10, 8, acc)
        putl(out, badge_text, bx2 + 2, h - card_h + 12 + bh2 + 2, FB, 0.58, BG, 2)
        bar_x, bar_y, bar_w = 18, h - 30, 200
        rrect(out, bar_x, bar_y, bar_x + bar_w, bar_y + 6, 3, CARD2)
        rrect(out, bar_x, bar_y, bar_x + int(bar_w * confidence), bar_y + 6, 3, acc)
        putl(out, f"{confidence * 100:.0f}% confidence", bar_x, h - 10, F, 0.32, GRAY, 1)
        pts_str = f"+{POINTS_MAP[waste_type]}"
        (pw, _), _ = cv2.getTextSize(pts_str, FB, 1.4, 3)
        putr(out, pts_str, w - 16, h - 22, FB, 1.4, AMBER, 3)
        putr(out, "pts", w - 16, h - 8, F, 0.36, GRAY, 1)
        if state == KioskState.IDLE:
            hint = "SPACE — sort & generate QR" if (ARGS.debug or ARGS.no_hardware) else "Item detected — sorting automatically"
            putc(out, hint, cx, h - 10, F, 0.32, GRAY, 1)
    else:
        putc(out, "Place item in front of camera", cx, h - 46, FB, 0.55, OFF_WHITE, 1)
        putc(out, "AI will detect and classify automatically", cx, h - 22, F, 0.34, GRAY, 1)
        putc(out, "H  history     Q  quit", cx, h - 6, F, 0.28, GRAY_DIM, 1)

    return out

# ── QR Panel ───────────────────────────────────────────────────────────────────
def draw_qr_panel(cam_frame, qr_img, waste_type, points, secs_left, claim_code, total_secs):
    acc   = WASTE_COLOR.get(waste_type, ACCENT)
    panel = np.zeros((WIN_H, PANEL_W, 3), np.uint8)
    panel[:] = BG
    PW, M = PANEL_W, MARGIN

    blend(panel, 0, 0, PW, 58, CARD, 1.0)
    putl(panel, "ECO",    M, 38, FB, 0.70, ACCENT, 2)
    putl(panel, "REWARD", M + 40, 38, FB, 0.70, WHITE, 1)
    putr(panel, "Smart Waste Kiosk", PW - M, 38, F, 0.28, GRAY, 1)
    cv2.line(panel, (0, 58), (PW, 58), acc, 2)

    y = 70
    half = (PW - M * 2 - 8) // 2

    rrect(panel, M, y, M + half, y + 56, 10, CARD)
    cv2.rectangle(panel, (M, y), (M + 3, y + 56), acc, -1)
    putl(panel, "WASTE TYPE", M + 10, y + 16, F, 0.25, GRAY, 1)
    tag_txt = WASTE_TAG[waste_type]
    (tw, _), _ = cv2.getTextSize(tag_txt, F, 0.30, 1)
    rrect(panel, M + 10, y + 22, M + 10 + tw + 10, y + 42, 4, acc)
    putl(panel, tag_txt, M + 15, y + 38, F, 0.30, BG, 1)
    putl(panel, waste_type, M + 10 + tw + 18, y + 40, FB, 0.44, WHITE, 1)

    px2 = M + half + 8
    rrect(panel, px2, y, px2 + half, y + 56, 10, CARD)
    cv2.rectangle(panel, (px2, y), (px2 + 3, y + 56), AMBER, -1)
    putl(panel, "POINTS", px2 + 10, y + 16, F, 0.25, GRAY, 1)
    pts_str = f"+{points}"
    putl(panel, pts_str, px2 + 10, y + 46, FB, 0.80, AMBER, 2)
    (ptw, _), _ = cv2.getTextSize(pts_str, FB, 0.80, 2)
    putl(panel, "pts", px2 + 14 + ptw, y + 46, F, 0.30, GRAY, 1)
    y += 64

    qr_size  = min(220, PW - M * 2 - 24)
    pad      = 12
    card_top = y
    card_h   = qr_size + pad * 2 + 36
    rrect(panel, M, card_top, PW - M, card_top + card_h, 12, CARD)
    putc(panel, "Scan with ECOReward App", PW // 2, card_top + 22, F, 0.30, GRAY, 1)
    qx = (PW - qr_size) // 2
    qy = card_top + 30
    rrect(panel, qx - pad - 2, qy - pad - 2, qx + qr_size + pad + 2, qy + qr_size + pad + 2, 10, acc)
    rrect(panel, qx - pad, qy - pad, qx + qr_size + pad, qy + qr_size + pad, 8, WHITE)
    panel[qy:qy + qr_size, qx:qx + qr_size] = cv2.resize(qr_img, (qr_size, qr_size), interpolation=cv2.INTER_NEAREST)
    cl = 20
    for px3, py3, dx, dy in [
        (qx - pad, qy - pad, 1, 1), (qx + qr_size + pad, qy - pad, -1, 1),
        (qx - pad, qy + qr_size + pad, 1, -1), (qx + qr_size + pad, qy + qr_size + pad, -1, -1)
    ]:
        cv2.line(panel, (px3, py3), (px3 + dx * cl, py3), acc, 3, cv2.LINE_AA)
        cv2.line(panel, (px3, py3), (px3, py3 + dy * cl), acc, 3, cv2.LINE_AA)
    y = card_top + card_h + 10

    frac    = secs_left / max(total_secs, 1)
    t_color = ACCENT if frac > 0.4 else AMBER if frac > 0.15 else RED_SOFT
    tch     = 68
    rrect(panel, M, y, PW - M, y + tch, 10, CARD)
    arc_cx, arc_cy = M + 42, y + tch // 2
    arc(panel, arc_cx, arc_cy, 26, frac, t_color, GRAY_DIM, 6)
    mins, secs_r = secs_left // 60, secs_left % 60
    putc(panel, f"{mins:02d}:{secs_r:02d}", arc_cx, arc_cy + 6, F, 0.44, t_color, 2)
    putl(panel, "EXPIRES IN", M + 82, y + 20, F, 0.26, GRAY, 1)
    exp_time = (datetime.now() + timedelta(seconds=secs_left)).strftime("%I:%M %p")
    putl(panel, f"at {exp_time}", M + 82, y + 44, FB, 0.52, WHITE, 1)
    bw2, bx2, by2 = PW - M * 2 - 8, M + 4, y + tch - 8
    rrect(panel, bx2, by2, bx2 + bw2, by2 + 4, 2, GRAY_DIM)
    rrect(panel, bx2, by2, bx2 + int(bw2 * frac), by2 + 4, 2, t_color)
    y += tch + 8

    cch = 38
    rrect(panel, M, y, PW - M, y + cch, 8, CARD)
    cv2.rectangle(panel, (M, y), (M + 3, y + cch), AMBER, -1)
    putl(panel, "CLAIM CODE", M + 10, y + 14, F, 0.24, GRAY, 1)
    putr(panel, f"ECO-{claim_code}", PW - M - 10, y + 28, FB, 0.46, AMBER, 1)
    y += cch + 8

    blend(panel, 0, WIN_H - 44, PW, WIN_H, CARD, 1.0)
    cv2.line(panel, (0, WIN_H - 44), (PW, WIN_H - 44), BORDER, 1)
    putc(panel, "R  — No phone? Show receipt to staff", PW // 2, WIN_H - 24, F, 0.30, AMBER, 1)
    putc(panel, "Single use  •  Auto-expires on scan",  PW // 2, WIN_H - 8,  F, 0.26, GRAY_DIM, 1)

    cam_r = cv2.resize(cam_frame, (CAM_W, WIN_H))
    return np.hstack([cam_r, panel])

# ── Receipt / History / Success screens (unchanged from original) ──────────────
def draw_receipt(cam_frame, waste_type, points, claim_code, token=""):
    base = cv2.resize(cam_frame, (WIN_W, WIN_H))
    acc  = WASTE_COLOR.get(waste_type, ACCENT)
    blend(base, 0, 0, WIN_W, WIN_H, BLACK, 0.86)
    rw, rh = 560, 500
    rx  = WIN_W // 2 - rw // 2
    ry  = WIN_H // 2 - rh // 2
    cx2 = WIN_W // 2
    rrect(base, rx + 4, ry + 4, rx + rw + 4, ry + rh + 4, 16, (4, 5, 7))
    rrect(base, rx, ry, rx + rw, ry + rh, 16, CARD)
    for xi in range(rx, rx + rw):
        t   = (xi - rx) / rw
        col = tuple(int(acc[i] * (1 - t * 0.4)) for i in range(3))
        cv2.line(base, (xi, ry), (xi, ry + 5), col, 1)
    blend(base, rx, ry, rx + rw, ry + 78, CARD2, 1.0)
    cv2.line(base, (rx, ry + 78), (rx + rw, ry + 78), BORDER, 1)
    putc(base, "ECOREWARD", cx2, ry + 36, FB, 0.72, ACCENT, 2)
    (ew, _), _ = cv2.getTextSize("ECOREWARD", FB, 0.72, 2)
    (lw, _), _ = cv2.getTextSize("ECO",       FB, 0.72, 2)
    cv2.putText(base, "REWARD", (cx2 - ew // 2 + lw, ry + 36), FB, 0.72, WHITE, 2, cv2.LINE_AA)
    putc(base, "Official Waste Receipt", cx2, ry + 60, F, 0.30, GRAY, 1)
    def row(label, value, vy, vc=OFF_WHITE, bold=False):
        putl(base, label, rx + 28, vy, F, 0.34, GRAY, 1)
        putr(base, value, rx + rw - 28, vy, FB if bold else F, 0.38, vc, 1)
    dot_divider(base, rx + 20, rx + rw - 20, ry + 88)
    _rt = datetime.now()
    row("Date",       _rt.strftime("%B %d, %Y"),   ry + 112)
    row("Time",       _rt.strftime("%I:%M:%S %p"), ry + 136)
    row("Waste Type", waste_type,                  ry + 160, acc,   bold=True)
    row("Points",     f"+{points} pts",            ry + 184, AMBER, bold=True)
    row("Status",     "PENDING CLAIM",             ry + 208, ACCENT)
    dot_divider(base, rx + 20, rx + rw - 20, ry + 222)
    putc(base, "MANUAL CLAIM CODE", cx2, ry + 244, F, 0.30, GRAY, 1)
    display_code = f"ECO-{claim_code}"
    cw2 = 340
    cbx1, cbx2 = cx2 - cw2 // 2, cx2 + cw2 // 2
    cby1, cby2 = ry + 252, ry + 310
    rrect(base, cbx1, cby1, cbx2, cby2, 12, CARD2)
    rrect(base, cbx1, cby1, cbx2, cby2, 12, acc, t=2)
    putc(base, display_code, cx2, cby2 - 12, FB, 1.10, WHITE, 3)
    putc(base, "Show this code to staff to claim your points", cx2, ry + 330, F, 0.30, GRAY, 1)
    dot_divider(base, rx + 20, rx + rw - 20, ry + 344)
    putc(base, f"Ref: {token[:18].upper()}...", cx2, ry + 366, F, 0.26, GRAY_DIM, 1)
    putc(base, "Valid for one claim only  •  Single use",  cx2, ry + 386, F, 0.27, GRAY_DIM, 1)
    dot_divider(base, rx + 20, rx + rw - 20, ry + 400)
    blend(base, rx, ry + 400, rx + rw, ry + rh, CARD2, 1.0)
    putc(base, "Thank you for helping the environment!", cx2, ry + 432, FB, 0.42, acc, 1)
    putc(base, "ECOReward Smart Waste System", cx2, ry + 456, F, 0.26, GRAY_DIM, 1)
    putc(base, "R  close receipt          Q  quit", WIN_W // 2, WIN_H - 12, F, 0.30, GRAY_DIM, 1)
    return base


def draw_history(cam_frame, history):
    base = cv2.resize(cam_frame, (WIN_W, WIN_H))
    blend(base, 0, 0, WIN_W, WIN_H, BLACK, 0.88)
    pw, ph = 700, 560
    px, py = WIN_W // 2 - pw // 2, WIN_H // 2 - ph // 2
    cx = WIN_W // 2
    rrect(base, px + 4, py + 4, px + pw + 4, py + ph + 4, 16, (4, 5, 7))
    rrect(base, px, py, px + pw, py + ph, 16, CARD)
    blend(base, px, py, px + pw, py + 62, CARD2, 1.0)
    cv2.line(base, (px, py + 62), (px + pw, py + 62), ACCENT, 2)
    ic = (px + 36, py + 32)
    cv2.circle(base, ic, 16, ACCENT, 2, cv2.LINE_AA)
    cv2.line(base, ic, (ic[0], ic[1] - 10), ACCENT, 2, cv2.LINE_AA)
    cv2.line(base, ic, (ic[0] + 8, ic[1]), ACCENT, 2, cv2.LINE_AA)
    putl(base, "SCAN HISTORY", px + 62, py + 28, FB, 0.62, WHITE, 2)
    putl(base, f"{len(history)} total redeemed", px + 64, py + 50, F, 0.28, GRAY, 1)
    putc(base, "H  close", px + pw - 50, py + 38, F, 0.28, GRAY_DIM, 1)
    hy = py + 66
    blend(base, px, hy, px + pw, hy + 22, GRAY_DIM, 0.5)
    for lx, txt in [(px+14,"#"), (px+40,"WASTE TYPE"), (px+238,"POINTS"),
                    (px+338,"CLAIM CODE"), (px+508,"TIME")]:
        putl(base, txt, lx, hy + 15, F, 0.27, GRAY, 1)
    row_h, max_rows = 44, 9
    entries = list(reversed(history))[:max_rows]
    ry2 = hy + 26
    if not entries:
        putc(base, "No scans yet", cx, WIN_H // 2, FB, 0.58, GRAY, 1)
    else:
        for i, entry in enumerate(entries):
            row_y = ry2 + i * row_h
            acc   = WASTE_COLOR.get(entry["waste_type"], ACCENT)
            if i % 2 == 0:
                blend(base, px, row_y, px + pw, row_y + row_h, CARD2, 0.6)
            cv2.rectangle(base, (px, row_y + 4), (px + 3, row_y + row_h - 4), acc, -1)
            ty   = row_y + row_h - 14
            num  = len(history) - i
            putl(base, str(num),                 px + 14,  ty, F,  0.30, GRAY,   1)
            putl(base, entry["waste_type"],       px + 40,  ty, FB, 0.36, WHITE,  1)
            putl(base, f"+{entry['points']} pts", px + 238, ty, FB, 0.38, AMBER,  1)
            putl(base, f"ECO-{entry['claim_code']}", px + 338, ty, FB, 0.36, ACCENT, 1)
            putl(base, entry["ts"],               px + 508, ty, F,  0.30, GRAY,   1)
            cv2.line(base, (px + 8, row_y + row_h), (px + pw - 8, row_y + row_h), BORDER, 1)
        if len(history) > max_rows:
            putc(base, f"+ {len(history) - max_rows} more", cx, py + ph - 14, F, 0.27, GRAY_DIM, 1)
    blend(base, px, py + ph - 36, px + pw, py + ph, CARD2, 1.0)
    cv2.line(base, (px, py + ph - 36), (px + pw, py + ph - 36), BORDER, 1)
    putc(base, "Session history only  •  Press H to close", cx, py + ph - 12, F, 0.28, GRAY_DIM, 1)
    return base


def draw_success(cam_frame, waste_type, points):
    base = cv2.resize(cam_frame, (WIN_W, WIN_H))
    acc  = WASTE_COLOR.get(waste_type, ACCENT)
    blend(base, 0, 0, WIN_W, WIN_H, BLACK, 0.70)
    cw, ch2 = 460, 280
    cx, cy  = WIN_W // 2, WIN_H // 2
    bx, by  = cx - cw // 2, cy - ch2 // 2
    rrect(base, bx, by, bx + cw, by + ch2, 18, CARD)
    rrect(base, bx, by, bx + cw, by + 4, 0, acc)
    rrect(base, bx, by + ch2 - 4, bx + cw, by + ch2, 0, acc)
    cv2.circle(base, (cx, by + 72), 32, acc, -1, cv2.LINE_AA)
    cv2.line(base, (cx - 13, by + 72), (cx - 3,  by + 84), BG, 4, cv2.LINE_AA)
    cv2.line(base, (cx - 3,  by + 84), (cx + 15, by + 56), BG, 4, cv2.LINE_AA)
    putc(base, "TRANSACTION COMPLETE",         cx, by + 122, FB, 0.68, WHITE,     2)
    putc(base, "QR code scanned successfully", cx, by + 146, F,  0.38, GRAY,      1)
    badge_w = 210
    rrect(base, cx - badge_w // 2, by + 158, cx + badge_w // 2, by + 198, 10, CARD2)
    putc(base, f"+{points} POINTS AWARDED", cx, by + 185, FB, 0.56, AMBER, 2)
    putc(base, waste_type.upper(),              cx, by + 224, F, 0.38, acc,      1)
    putc(base, "Ready for next transaction",    cx, by + 252, F, 0.34, GRAY_DIM, 1)
    return base

# ── Firebase watcher ───────────────────────────────────────────────────────────
_scan_event   = threading.Event()
_active_token = None
_watcher      = None

def _now() -> datetime:
    return datetime.now(timezone.utc)

def _to_utc_dt(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            s = value.rstrip("Z").replace("+00:00", "").replace("+0000", "")
            return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _cleanup_stale_tokens():
    if not db:
        return
    try:
        docs = db.collection("qr_tokens").where("isUsed", "==", False).stream()
        now = _now()
        for doc in docs:
            data = doc.to_dict()
            expires_dt = _to_utc_dt(data.get("expiresAt"))
            if expires_dt and now > expires_dt:
                doc.reference.update({"expired": True, "isUsed": True})
                print(f"[CLEANUP] Expired stale token {doc.id[:8]}…")
    except Exception as e:
        print(f"[CLEANUP WARNING] {e}")


if DB_MODE == "admin":
    threading.Thread(target=_cleanup_stale_tokens, daemon=True).start()


def _start_heartbeat():
    if db is None:
        return
    _stop = threading.Event()
    def _beat():
        while not _stop.wait(30):
            try:
                db.collection("kiosks").document(KIOSK_ID).set({
                    "lastSeen": SERVER_TS,
                    "status":   "online",
                    "name":     KIOSK_NAME,
                }, merge=True)
            except Exception:
                pass
    threading.Thread(target=_beat, daemon=True, name="kiosk-heartbeat").start()


def watch_token(token):
    global _watcher, _active_token
    if not db:
        return None
    if _watcher:
        try:
            _watcher.unsubscribe()
        except Exception:
            pass
    _active_token = token
    _scan_event.clear()
    def on_snap(doc_snapshot, changes, read_time):
        for doc in doc_snapshot:
            if doc.exists and doc.id == _active_token and len(doc.to_dict().get("usedBy", [])) > 0:
                _scan_event.set()
    _watcher = db.collection("qr_tokens").document(token).on_snapshot(on_snap)
    return _watcher


def _write_bins(bio_pct: int, rec_pct: int, res_pct: int):  # FIX-H-02
    """Write HC-SR04 fill levels to bins/{KIOSK_ID} for the web dashboard."""
    if not db:
        return
    try:
        now_str = _now().isoformat()
        db.collection("bins").document(KIOSK_ID).set({
            "binId":      KIOSK_ID,
            "name":       KIOSK_NAME,
            "fillLevels": {
                "Biodegradable": bio_pct,
                "Recyclable":    rec_pct,
                "Residual":      res_pct,
            },
            "lastUpdated": now_str,
            "status":      "online",
        }, merge=True)
    except Exception as e:
        print(f"[BINS] Write failed: {e}")

# ── Splash ─────────────────────────────────────────────────────────────────────
def draw_splash_animation(window_name):
    import math
    total_frames = 30
    cx, cy = WIN_W // 2, WIN_H // 2

    def ease_out_cubic(t): return 1 - (1 - t) ** 3
    def ease_in_out(t):    return t * t * (3 - 2 * t)
    def ease_out_back(t):
        c1, c3 = 1.70158, 1.70158 + 1
        return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2

    _card_f = np.array(CARD, np.float32)
    _vig_weights = []
    for _r, _a in ((460, 0.25), (340, 0.18), (200, 0.08)):
        _m = np.zeros((WIN_H, WIN_W), np.float32)
        cv2.circle(_m, (cx, cy), _r, 1.0, -1)
        cv2.GaussianBlur(_m, (0, 0), _r // 3, dst=_m)
        _vig_weights.append((_m * _a).astype(np.float32)[:, :, np.newaxis])

    for frame_i in range(total_frames):
        t = frame_i / (total_frames - 1)
        canvas = np.zeros((WIN_H, WIN_W, 3), np.uint8)
        bg_alpha = min(1.0, t / 0.15)
        bg_val   = int(ease_out_cubic(bg_alpha) * 14)
        canvas[:] = (bg_val, bg_val + 2, bg_val + 4)
        _cf = canvas.astype(np.float32)
        for _wt in _vig_weights:
            _cf += _wt * (_card_f - _cf)
        canvas = np.clip(_cf, 0, 255).astype(np.uint8)
        if t >= 0.10:
            logo_t  = min(1.0, (t - 0.10) / 0.30)
            scale   = ease_out_back(logo_t)
            r_outer = int(80 * scale)
            if r_outer > 2:
                for dr in range(8, 0, -1):
                    alpha_g = 0.04 * (9 - dr) * logo_t
                    glow_col = tuple(int(ACCENT[i] * alpha_g) for i in range(3))
                    cv2.circle(canvas, (cx, cy), r_outer + dr, glow_col, 2, cv2.LINE_AA)
                cv2.circle(canvas, (cx, cy), r_outer, CARD2, -1, cv2.LINE_AA)
                cv2.circle(canvas, (cx, cy), r_outer, ACCENT, 2, cv2.LINE_AA)
                if scale > 0.5:
                    icon_r = int(28 * scale)
                    cv2.circle(canvas, (cx, cy - 8), icon_r, ACCENT, -1, cv2.LINE_AA)
                    cv2.circle(canvas, (cx + icon_r, cy - 8), icon_r, CARD2, -1, cv2.LINE_AA)
                    cv2.line(canvas, (cx, cy - 8 + icon_r - 4), (cx, cy + icon_r + 4), ACCENT, 2, cv2.LINE_AA)
        if t >= 0.40:
            txt_t = min(1.0, (t - 0.40) / 0.28)
            txt_a = ease_out_cubic(txt_t)
            slide  = int((1 - txt_a) * 30)
            ty_eco = cy + 104 + slide
            (ew, _), _ = cv2.getTextSize("ECO",    FB, 1.10, 3)
            (rw, _), _ = cv2.getTextSize("REWARD", FB, 1.10, 2)
            lx = cx - (ew + rw + 12) // 2
            cv2.putText(canvas, "ECO",    (lx, ty_eco),          FB, 1.10, tuple(int(c * txt_a) for c in ACCENT), 3, cv2.LINE_AA)
            cv2.putText(canvas, "REWARD", (lx + ew + 12, ty_eco), FB, 1.10, tuple(int(c * txt_a) for c in WHITE),  2, cv2.LINE_AA)
            putc(canvas, "Smart Waste Kiosk System", cx, ty_eco + 28, F, 0.38, tuple(int(c * txt_a) for c in GRAY), 1)
        if t >= 0.62:
            bar_t  = min(1.0, (t - 0.62) / 0.26)
            bar_a  = ease_in_out(bar_t)
            bar_w2 = 280
            bx1 = cx - bar_w2 // 2
            by1 = cy + 148
            rrect(canvas, bx1, by1, bx1 + bar_w2, by1 + 4, 2, CARD2)
            fill_w = int(bar_w2 * bar_a)
            if fill_w > 2:
                rrect(canvas, bx1, by1, bx1 + fill_w, by1 + 4, 2, ACCENT)
                cv2.circle(canvas, (bx1 + fill_w, by1 + 2), 4, ACCENT, -1, cv2.LINE_AA)
            pct_col = tuple(int(c * min(1.0, bar_t * 2)) for c in GRAY)
            putc(canvas, f"Initializing  {int(bar_a * 100)}%", cx, by1 + 22, F, 0.30, pct_col, 1)
        if t >= 0.88:
            fade_a = ease_in_out((t - 0.88) / 0.12)
            overlay = canvas.copy()
            overlay[:] = (8, 10, 14)
            cv2.addWeighted(overlay, fade_a, canvas, 1 - fade_a, 0, canvas)
        cv2.imshow(window_name, canvas)
        if cv2.waitKey(20) & 0xFF == ord("q"):
            cv2.destroyAllWindows()
            sys.exit(0)

# ── Main loop ──────────────────────────────────────────────────────────────────
def main():
    # Start serial thread unless --no-hardware  # FIX-C-01
    if not ARGS.no_hardware:
        threading.Thread(
            target=_serial_thread,
            args=(ARGS.port, ARGS.baud),
            daemon=True,
            name="serial-esp32",
        ).start()
    else:
        print("[MOCK] --no-hardware: serial port bypassed; ESP32 responses are simulated.")

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[CAMERA ERROR] Could not access webcam!")
        sys.exit(1)

    print(f"\n------------------------------------------")
    print(f"  ECOReward Bin  |  {WIN_W}x{WIN_H}  |  Kiosk Mode")
    if ARGS.no_hardware:
        print("  MODE: --no-hardware (mock)")
    if ARGS.debug:
        print("  MODE: --debug  (SPACEBAR enabled)")
    print("  H = History    Q = Quit")
    print("------------------------------------------\n")

    cv2.namedWindow("ECOReward Bin", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("ECOReward Bin", WIN_W, WIN_H)
    # FIX-L-01: fullscreen on the kiosk display
    cv2.setWindowProperty("ECOReward Bin", cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    draw_splash_animation("ECOReward Bin")
    _start_heartbeat()

    bad_labels = unmapped_labels(model)
    if bad_labels:
        print(f"[AI ERROR] {MODEL_FILE} has {len(bad_labels)} class(es) not in waste_map.json "
              f"({', '.join(bad_labels[:5])}). QR codes DISABLED.")

    # ── Detection state ──
    DETECT_WINDOW   = 7
    detect_history  = deque(maxlen=DETECT_WINDOW)
    HOLD_FRAMES     = 0
    HOLD_MAX        = 15
    _last_beep_type = None

    latest_label      = "Unknown"
    latest_waste_type = "Residual"
    latest_conf       = 0.0
    fb_ok             = db is not None
    scan_count        = 0
    status_msg        = ""

    # ── Kiosk state machine  # FIX-H-01 ──
    state          = KioskState.IDLE
    sort_waste     = None
    sort_deadline  = None   # SORTING: timeout waiting for servo OK
    weigh_deadline = None   # WEIGHING: timeout waiting for W:<g>

    # ── QR state ──
    qr_active      = False
    qr_image       = None
    qr_token       = None
    qr_waste_type  = None
    qr_points      = 0
    qr_expires_at  = None
    qr_total_secs  = QR_EXPIRY_MINUTES * 60
    qr_claim_code  = "----"

    show_success   = False
    success_until  = None
    success_waste  = ""
    success_points = 0

    show_receipt  = False
    show_history  = False
    scan_history  = []

    _cap_fail_count = 0
    _CAP_FAIL_MAX   = 30

    # fill levels from HC-SR04 (last known)  # FIX-H-02
    fill_bio = fill_rec = fill_res = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            _cap_fail_count += 1
            if _cap_fail_count >= _CAP_FAIL_MAX:
                print("[CAMERA] Lost feed — re-opening…")
                cap.release()
                cap = cv2.VideoCapture(0)
                _cap_fail_count = 0
                if not cap.isOpened():
                    print("[CAMERA ERROR] Could not re-open webcam — exiting.")
                    break
                print("[CAMERA] Re-opened.")
            continue
        _cap_fail_count = 0

        now = _now()

        # ── Drain ESP32 serial lines  # FIX-C-01 / FIX-H-01 / FIX-H-02 ──
        for line in _drain_rx():
            print(f"[SERIAL] ← {line}")
            if line.upper() == "OK" and state == KioskState.SORTING:
                state          = KioskState.WEIGHING
                weigh_deadline = now + timedelta(seconds=10)
                print("[STATE] SORTING → WEIGHING")
            elif line.startswith("W:") and state == KioskState.WEIGHING:
                try:
                    grams = int(line[2:])
                except ValueError:
                    grams = 0
                if grams >= MIN_WEIGHT_G:
                    state = KioskState.WRITING
                    print(f"[STATE] WEIGHING → WRITING  ({grams}g confirmed)")
                else:
                    state      = KioskState.IDLE
                    status_msg = f"Nothing detected ({grams}g). Please try again."
                    print(f"[STATE] WEIGHING → IDLE  (only {grams}g — too light)")
            elif line.startswith("F:"):
                # Fill-level update from HC-SR04s  # FIX-H-02
                parts = line[2:].split(",")
                if len(parts) == 3:
                    try:
                        fill_bio, fill_rec, fill_res = (int(p) for p in parts)
                        threading.Thread(
                            target=_write_bins,
                            args=(fill_bio, fill_rec, fill_res),
                            daemon=True,
                        ).start()
                    except ValueError:
                        pass

        # ── State machine transitions ──
        if state == KioskState.SORTING:
            if now > sort_deadline:
                state      = KioskState.IDLE
                status_msg = "Servo timeout — no response from ESP32."
                print("[STATE] SORTING → IDLE (timeout)")

        elif state == KioskState.WEIGHING:
            if now > weigh_deadline:
                state      = KioskState.IDLE
                status_msg = "Load-cell timeout — item not detected. Please try again."
                print("[STATE] WEIGHING → IDLE (timeout)")

        elif state == KioskState.WRITING:
            # Run Firestore write off the main thread so the UI doesn't freeze  # FIX-C-03
            _wt = sort_waste
            state = KioskState.IDLE   # optimistically; thread sets qr_active on success

            def _do_write(waste_type=_wt):
                global qr_image, qr_active, qr_token, qr_waste_type, qr_points
                global qr_expires_at, qr_total_secs, qr_claim_code, show_receipt
                if db is None:
                    print("[BLOCKED] Firebase offline — QR not issued.")
                    return
                if bad_labels:
                    print("[BLOCKED] Model/map mismatch — QR not issued.")
                    return
                try:
                    token      = str(uuid.uuid4())
                    points     = POINTS_MAP[waste_type]
                    # FIX-C-03 / FIX-H-03: store dates as ISO-8601 strings with UTC offset
                    # so Dart's DateTime.parse reads them correctly.
                    now_dt     = _now()
                    expires_dt = now_dt + timedelta(minutes=QR_EXPIRY_MINUTES)
                    created_str  = now_dt.isoformat()       # "2026-10-10T12:34:56.789012+00:00"
                    expires_str  = expires_dt.isoformat()   # same format
                    qr_data    = f"ECOBIN:{waste_type}:{token}"
                    claim_code = str(1000 + secrets.randbelow(9000))

                    db.collection("qr_tokens").document(token).set({
                        "wasteType": waste_type,
                        "createdAt": created_str,   # FIX-C-03: ISO string
                        "expiresAt": expires_str,   # FIX-C-03: ISO string
                        "usedBy":    [],
                        "isUsed":    False,
                        "claimCode": claim_code,
                    })
                    print(f"[FIREBASE] Token saved ({token[:8]}…) — waiting for scan.")

                    # non-critical detection log
                    try:
                        db.collection("detections").add({
                            "wasteType":   waste_type,
                            "label":       latest_label,
                            "confidence":  float(latest_conf),
                            "modelFile":   MODEL_FILE,
                            "modelSha256": MODEL_SHA256,
                            "timestamp":   created_str,
                            "token":       token,
                            "kioskId":     KIOSK_ID,
                        })
                    except Exception:
                        pass

                    watch_token(token)

                    # Update UI state (GIL protects individual assignments)
                    qr_image      = generate_qr(qr_data, size=220)
                    qr_token      = token
                    qr_waste_type = waste_type
                    qr_points     = points
                    qr_expires_at = expires_dt
                    qr_total_secs = QR_EXPIRY_MINUTES * 60
                    qr_claim_code = claim_code
                    show_receipt  = False
                    qr_active     = True
                    print(f"[QR] Active — {waste_type} +{points}pts")
                except Exception as err:
                    print(f"[FIREBASE ERROR] Could not store token: {err}")

            threading.Thread(target=_do_write, daemon=True).start()

        # ── AI detection (only when idle and not in QR/success screen) ──
        cam_r = cv2.resize(frame, (CAM_W, WIN_H))
        if state == KioskState.IDLE and not qr_active and not show_success:
            results  = model(frame, conf=0.15, verbose=False)
            fh, fw   = frame.shape[:2]
            sx, sy   = CAM_W / fw, WIN_H / fh
            box, conf = None, 0.0
            for r in results:
                for b in r.boxes:
                    c = float(b.conf[0])
                    if c >= MIN_BOX_CONF and c > conf:
                        box, conf = b, c
            if box is not None:
                lbl = model.names[int(box.cls[0])].lower()
                detect_history.append((lbl, conf))
                vote_lbl  = Counter(h[0] for h in detect_history).most_common(1)[0][0]
                vote_conf = max(h[1] for h in detect_history if h[0] == vote_lbl)
                latest_label      = vote_lbl
                latest_waste_type = WASTE_MAP.get(vote_lbl, "Residual")
                latest_conf       = vote_conf
                HOLD_FRAMES       = 0
                if vote_lbl != _last_beep_type:
                    _last_beep_type = vote_lbl
                    threading.Thread(target=_beep, args=(880, 120), daemon=True).start()
                # draw bbox
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                x1, y1, x2, y2 = int(x1*sx), int(y1*sy), int(x2*sx), int(y2*sy)
                ac  = WASTE_COLOR[latest_waste_type]
                cv2.rectangle(cam_r, (x1, y1), (x2, y2), ac, 2, cv2.LINE_AA)
                tag = f"{latest_waste_type}  {vote_conf * 100:.0f}%"
                (tw, th), _ = cv2.getTextSize(tag, F, 0.44, 1)
                rrect(cam_r, x1, y1 - th - 10, x1 + tw + 10, y1, 4, ac)
                cv2.putText(cam_r, tag, (x1 + 5, y1 - 3), F, 0.44, BG, 1, cv2.LINE_AA)
                bar_w = x2 - x1
                bar_y = y2 - 6
                rrect(cam_r, x1, bar_y, x2, y2, 0, GRAY_DIM)
                rrect(cam_r, x1, bar_y, x1 + int(bar_w * vote_conf), y2, 0, ac)

                # Auto-trigger sort when stable label locks in (not in debug/no-hardware)
                if (len(detect_history) == DETECT_WINDOW
                        and not ARGS.debug and not ARGS.no_hardware):
                    state         = KioskState.SORTING
                    sort_waste    = latest_waste_type
                    sort_deadline = _now() + timedelta(seconds=5)
                    _send_servo(latest_waste_type)
                    detect_history.clear()
                    _last_beep_type = None
                    print(f"[STATE] IDLE → SORTING  ({latest_waste_type})")
            else:
                HOLD_FRAMES += 1
                if HOLD_FRAMES > HOLD_MAX:
                    detect_history.clear()
                    latest_label      = "Unknown"
                    latest_waste_type = "Residual"
                    latest_conf       = 0.0
                    _last_beep_type   = None

        # ── Build display ──
        if show_history:
            display = draw_history(cam_r, scan_history)
        elif show_receipt and qr_token:
            display = draw_receipt(cam_r, qr_waste_type, qr_points, qr_claim_code, qr_token or "")
        elif show_success and success_until:
            if now < success_until:
                display = draw_success(cam_r, success_waste, success_points)
            else:
                show_success = False
                display = draw_camera_hud(cam_r, latest_label, latest_waste_type,
                                          latest_conf, fb_ok, scan_count, state, status_msg)
        elif qr_active and qr_expires_at:
            secs_left = max(0, int((qr_expires_at - now).total_seconds()))
            if _scan_event.is_set():
                _scan_event.clear()
                scan_count    += 1
                show_success   = True
                success_until  = now + timedelta(seconds=4)
                success_waste  = qr_waste_type
                success_points = qr_points
                scan_history.append({
                    "waste_type":  qr_waste_type,
                    "points":      qr_points,
                    "claim_code":  qr_claim_code,
                    "ts":          datetime.now().strftime("%b %d  %I:%M %p"),
                })
                qr_active = False
                display = draw_success(cam_r, success_waste, success_points)
            elif secs_left == 0:
                print("[EXPIRED] QR expired.")
                qr_active = False
                display = draw_camera_hud(cam_r, latest_label, latest_waste_type,
                                          latest_conf, fb_ok, scan_count, state, status_msg)
            else:
                display = draw_qr_panel(cam_r, qr_image, qr_waste_type, qr_points,
                                        secs_left, qr_claim_code, qr_total_secs)
        else:
            display = draw_camera_hud(cam_r, latest_label, latest_waste_type,
                                      latest_conf, fb_ok, scan_count, state, status_msg)

        cv2.imshow("ECOReward Bin", display)
        key = cv2.waitKey(1) & 0xFF

        if key == ord("r") and qr_active:
            show_receipt = not show_receipt
        if key == ord("h"):
            show_history = not show_history
            show_receipt = False

        # SPACEBAR: only in --debug or --no-hardware mode  # FIX-M-03
        if key == 32 and state == KioskState.IDLE and not qr_active and not show_success:
            if ARGS.debug or ARGS.no_hardware:
                if db is None:
                    print("[BLOCKED] Firebase offline.")
                elif bad_labels:
                    print("[BLOCKED] Model/map mismatch.")
                elif latest_label == "Unknown":
                    print("[BLOCKED] No waste detected yet.")
                else:
                    state         = KioskState.SORTING
                    sort_waste    = latest_waste_type
                    sort_deadline = _now() + timedelta(seconds=5)
                    if ARGS.no_hardware:
                        _mock_sort_sequence(latest_waste_type)
                    else:
                        _send_servo(latest_waste_type)
                    detect_history.clear()
                    _last_beep_type = None
                    status_msg = ""
                    print(f"[DEBUG] Manual trigger → SORTING ({latest_waste_type})")

        if key == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()
    print("\n[SYSTEM] Shut down.")


if __name__ == "__main__":
    main()
