# -*- coding: utf-8 -*-
import os
import sys
import platform
from pathlib import Path
import cv2
import uuid
import random
import qrcode
import numpy as np
import threading
from collections import deque, Counter
from datetime import datetime, timedelta, timezone
import firebase_admin
from firebase_admin import credentials, firestore
from ultralytics import YOLO

# ── Platform-safe beep ────────────────────────────────────────────────────────
if platform.system() == 'Windows':
    import winsound
    def _beep(freq=880, dur=120):
        winsound.Beep(freq, dur)
else:
    def _beep(freq=880, dur=120):  # no-op on Linux/macOS
        pass

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


db = None
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
    print("[FIREBASE] Connected!")
except Exception as e:
    print(f"[FIREBASE ERROR] {e}")
    print("[FIREBASE ERROR] Running OFFLINE: QR codes will NOT be issued until this is fixed (see README).")

# ── YOLO ───────────────────────────────────────────────────────────────────────
print("[AI] Loading YOLO model...")
try:
    model = YOLO("best.pt")
    print("[AI] Loaded: best.pt")
except Exception as e:
    print(f"[AI WARNING] best.pt failed to load ({e}), falling back to yolov8n.pt")
    model = YOLO("yolov8n.pt")
    print("[AI] Loaded: yolov8n.pt")

# ── Layout ─────────────────────────────────────────────────────────────────────
WIN_W, WIN_H      = 1280, 720
CAM_W             = 820
PANEL_W           = WIN_W - CAM_W          # 460
MARGIN            = 20
QR_EXPIRY_MINUTES = 20

# ── Palette BGR ────────────────────────────────────────────────────────────────
BG        = (  8,  10,  14)   # near-black
CARD      = ( 18,  22,  28)   # dark card
CARD2     = ( 26,  31,  40)   # slightly lighter card
BORDER    = ( 38,  44,  54)   # subtle border
ACCENT    = ( 80, 200, 120)   # soft green  (kiosk brand)
WHITE     = (255, 255, 255)
OFF_WHITE = (200, 205, 215)
GRAY      = (100, 108, 120)
GRAY_DIM  = ( 38,  44,  54)
BLACK     = (  0,   0,   0)
AMBER     = ( 40, 180, 255)   # orange-amber for points
RED_SOFT  = ( 70,  70, 210)

WASTE_COLOR = {
    'Biodegradable': ( 60, 180,  80),   # green
    'Recyclable':    ( 30, 160, 255),   # blue
    'Residual':      ( 80,  80, 200),   # muted red
}
WASTE_EMOJI_LABEL = {
    'Biodegradable': 'BIODEGRADABLE',
    'Recyclable':    'RECYCLABLE',
    'Residual':      'RESIDUAL',
}
WASTE_TAG = {
    'Biodegradable': 'BIO',
    'Recyclable':    'REC',
    'Residual':      'RES',
}
POINTS_MAP = {'Biodegradable': 15, 'Recyclable': 10, 'Residual': 5}
WASTE_MAP  = {
    # legacy 3-class
    'biodegradable':  'Biodegradable',
    'recyclable':     'Recyclable',
    'residual':       'Residual',
    # model classes (all lowercased via .lower() in detection loop)
    'fries':          'Biodegradable',
    'plain rice':     'Biodegradable',
    'bowl':           'Biodegradable',
    'bottle':         'Recyclable',
    'can':            'Recyclable',
    'metal':          'Recyclable',
    'straw':          'Recyclable',
    'paper-cup':      'Recyclable',
    'paper cup':      'Recyclable',
    'plastic-cup':    'Recyclable',
    'cigarette':      'Residual',
    'fork':           'Residual',
    'spoon':          'Residual',
    'plastic-spoon':  'Residual',
    'tissue':         'Biodegradable',
    'foam box':       'Residual',
    'styro':          'Residual',
    '1':              'Residual',   # class 0 anomaly — treat as residual
}

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

def hgrad(img, x1, y1, x2, y2, c1, c2):
    for x in range(x1, x2):
        t   = (x - x1) / max(x2 - x1, 1)
        col = tuple(int(c1[i] * (1 - t) + c2[i] * t) for i in range(3))
        cv2.line(img, (x, y1), (x, y2), col, 1)

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

# ── Camera HUD ─────────────────────────────────────────────────────────────────
def draw_camera_hud(cam, label, waste_type, confidence, fb_ok, scan_count):
    h, w = cam.shape[:2]
    out  = cam.copy()
    acc  = WASTE_COLOR.get(waste_type, ACCENT)

    # ── top bar ──
    blend(out, 0, 0, w, 52, BG, 0.90)
    # left: brand
    rrect(out, 12, 10, 130, 42, 8, CARD2)
    putl(out, "ECO",    20, 34, FB, 0.62, ACCENT, 2)
    putl(out, "REWARD", 58, 34, FB, 0.62, WHITE,  1)

    # center: clock
    _t = datetime.now()
    putc(out, _t.strftime("%I:%M %p"), w // 2, 32, FB, 0.52, OFF_WHITE, 1)
    putc(out, _t.strftime("%A, %b %d"), w // 2, 46, F, 0.30, GRAY, 1)

    # right: status pills
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

    # history icon pill — clock symbol + label
    hx = sx - 64
    rrect(out, hx - 10, 10, hx + 54, 30, 6, CARD2)
    # draw mini clock face
    hcx, hcy = hx + 8, 20
    cv2.circle(out, (hcx, hcy), 7, ACCENT, 1, cv2.LINE_AA)
    cv2.line(out, (hcx, hcy), (hcx, hcy - 4), ACCENT, 1, cv2.LINE_AA)
    cv2.line(out, (hcx, hcy), (hcx + 3, hcy), ACCENT, 1, cv2.LINE_AA)
    putl(out, "HIST", hx + 19, 26, F, 0.27, ACCENT, 1)

    # thin accent line under top bar
    cv2.line(out, (0, 52), (w, 52), BORDER, 1)

    # Firebase offline: say so loudly. A QR could never be redeemed, so SPACE is disabled.
    if not fb_ok:
        blend(out, 0, 53, w, 85, RED_SOFT, 0.88)
        putc(out, "FIREBASE OFFLINE  -  QR codes disabled  (check the credential, see README)",
             w // 2, 75, F, 0.46, WHITE, 1)

    # ── scan frame ──
    cx, cy = w // 2, h // 2
    bx, by = 190, 160
    x1, y1, x2, y2 = cx - bx, cy - by, cx + bx, cy + by

    # dim outside scan zone
    blend(out, 0,  0,  w,  y1, BLACK, 0.35)
    blend(out, 0,  y2, w,  h,  BLACK, 0.35)
    blend(out, 0,  y1, x1, y2, BLACK, 0.35)
    blend(out, x2, y1, w,  y2, BLACK, 0.35)

    # corner brackets — thick, clean
    cl, lw = 28, 3
    for px, py, dx, dy in [(x1, y1, 1, 1), (x2, y1, -1, 1),
                            (x1, y2, 1, -1), (x2, y2, -1, -1)]:
        cv2.line(out, (px, py), (px + dx * cl, py), acc, lw, cv2.LINE_AA)
        cv2.line(out, (px, py), (px, py + dy * cl), acc, lw, cv2.LINE_AA)

    # subtle inner glow on scan zone
    blend(out, x1, y1, x2, y2, acc, 0.03)

    # center crosshair — tiny, minimal
    cv2.line(out, (cx - 8, cy), (cx + 8, cy), acc, 1, cv2.LINE_AA)
    cv2.line(out, (cx, cy - 8), (cx, cy + 8), acc, 1, cv2.LINE_AA)

    # ── bottom detection card ──
    card_h = 88
    blend(out, 0, h - card_h, w, h, BG, 0.92)
    cv2.line(out, (0, h - card_h), (w, h - card_h), BORDER, 1)

    if label != "Unknown" and confidence > 0:
        # left accent bar
        cv2.rectangle(out, (0, h - card_h), (4, h), acc, -1)

        # waste type badge
        badge_text = f"  {WASTE_EMOJI_LABEL[waste_type]}  "
        (bw2, bh2), _ = cv2.getTextSize(badge_text, FB, 0.58, 2)
        bx2 = 18
        rrect(out, bx2, h - card_h + 12, bx2 + bw2 + 4, h - card_h + 12 + bh2 + 10, 8, acc)
        putl(out, badge_text, bx2 + 2, h - card_h + 12 + bh2 + 2, FB, 0.58, BG, 2)

        # confidence bar
        bar_x = 18
        bar_y = h - 30
        bar_w = 200
        bar_h2 = 6
        rrect(out, bar_x, bar_y, bar_x + bar_w, bar_y + bar_h2, 3, CARD2)
        rrect(out, bar_x, bar_y, bar_x + int(bar_w * confidence), bar_y + bar_h2, 3, acc)
        putl(out, f"{confidence * 100:.0f}% confidence", bar_x, h - 10, F, 0.32, GRAY, 1)

        # points — right side, large
        pts_str = f"+{POINTS_MAP[waste_type]}"
        (pw, _), _ = cv2.getTextSize(pts_str, FB, 1.4, 3)
        putr(out, pts_str, w - 16, h - 22, FB, 1.4, AMBER, 3)
        putr(out, "pts", w - 16, h - 8, F, 0.36, GRAY, 1)

        # hint
        putc(out, "SPACE  to generate QR      H  history", cx, h - 10, F, 0.32, GRAY, 1)

    else:
        # idle state — centered instruction
        putc(out, "Place item in front of camera", cx, h - 46, FB, 0.55, OFF_WHITE, 1)
        putc(out, "AI will detect and classify automatically", cx, h - 22, F, 0.34, GRAY, 1)
        putc(out, "SPACE  generate QR     H  history     Q  quit", cx, h - 6, F, 0.28, GRAY_DIM, 1)

    return out

# ── QR Panel ───────────────────────────────────────────────────────────────────
def draw_qr_panel(cam_frame, qr_img, waste_type, points, secs_left, claim_code, total_secs):
    acc   = WASTE_COLOR.get(waste_type, ACCENT)
    panel = np.zeros((WIN_H, PANEL_W, 3), np.uint8)
    panel[:] = BG

    PW = PANEL_W
    M  = MARGIN

    # ── header bar ──
    blend(panel, 0, 0, PW, 58, CARD, 1.0)
    # brand
    putl(panel, "ECO",    M, 38, FB, 0.70, ACCENT, 2)
    putl(panel, "REWARD", M + 40, 38, FB, 0.70, WHITE, 1)
    # subtitle right
    putr(panel, "Smart Waste Kiosk", PW - M, 38, F, 0.28, GRAY, 1)
    # thin accent line under header
    cv2.line(panel, (0, 58), (PW, 58), acc, 2)

    y = 70

    # ── waste + points row (side by side) ──
    half = (PW - M * 2 - 8) // 2

    # waste pill
    rrect(panel, M, y, M + half, y + 56, 10, CARD)
    cv2.rectangle(panel, (M, y), (M + 3, y + 56), acc, -1)
    putl(panel, "WASTE TYPE", M + 10, y + 16, F, 0.25, GRAY, 1)
    # tag badge inline
    tag_txt = WASTE_TAG[waste_type]
    (tw, _), _ = cv2.getTextSize(tag_txt, F, 0.30, 1)
    rrect(panel, M + 10, y + 22, M + 10 + tw + 10, y + 42, 4, acc)
    putl(panel, tag_txt, M + 15, y + 38, F, 0.30, BG, 1)
    putl(panel, waste_type, M + 10 + tw + 18, y + 40, FB, 0.44, WHITE, 1)

    # points pill
    px2 = M + half + 8
    rrect(panel, px2, y, px2 + half, y + 56, 10, CARD)
    cv2.rectangle(panel, (px2, y), (px2 + 3, y + 56), AMBER, -1)
    putl(panel, "POINTS", px2 + 10, y + 16, F, 0.25, GRAY, 1)
    pts_str = f"+{points}"
    putl(panel, pts_str, px2 + 10, y + 46, FB, 0.80, AMBER, 2)
    (ptw, _), _ = cv2.getTextSize(pts_str, FB, 0.80, 2)
    putl(panel, "pts", px2 + 14 + ptw, y + 46, F, 0.30, GRAY, 1)

    y += 64

    # ── QR code card ──
    qr_size   = 220
    pad       = 12
    card_top  = y
    card_h    = qr_size + pad * 2 + 36   # 36 for label above QR
    rrect(panel, M, card_top, PW - M, card_top + card_h, 12, CARD)

    # label
    putc(panel, "Scan with ECOReward App", PW // 2, card_top + 22, F, 0.30, GRAY, 1)

    # QR white frame
    qx = (PW - qr_size) // 2
    qy = card_top + 30
    # outer glow border
    rrect(panel, qx - pad - 2, qy - pad - 2,
          qx + qr_size + pad + 2, qy + qr_size + pad + 2, 10, acc)
    rrect(panel, qx - pad, qy - pad,
          qx + qr_size + pad, qy + qr_size + pad, 8, WHITE)
    panel[qy:qy + qr_size, qx:qx + qr_size] = cv2.resize(
        qr_img, (qr_size, qr_size), interpolation=cv2.INTER_NEAREST)

    # corner brackets over QR
    cl = 20
    for px3, py3, dx, dy in [
        (qx - pad, qy - pad, 1, 1), (qx + qr_size + pad, qy - pad, -1, 1),
        (qx - pad, qy + qr_size + pad, 1, -1),
        (qx + qr_size + pad, qy + qr_size + pad, -1, -1)
    ]:
        cv2.line(panel, (px3, py3), (px3 + dx * cl, py3), acc, 3, cv2.LINE_AA)
        cv2.line(panel, (px3, py3), (px3, py3 + dy * cl), acc, 3, cv2.LINE_AA)

    y = card_top + card_h + 10

    # ── timer card ──
    frac    = secs_left / max(total_secs, 1)
    t_color = ACCENT if frac > 0.4 else AMBER if frac > 0.15 else RED_SOFT
    tch     = 68
    rrect(panel, M, y, PW - M, y + tch, 10, CARD)

    # arc timer
    arc_cx = M + 42
    arc_cy = y + tch // 2
    arc(panel, arc_cx, arc_cy, 26, frac, t_color, GRAY_DIM, 6)
    mins, secs_r = secs_left // 60, secs_left % 60
    putc(panel, f"{mins:02d}:{secs_r:02d}", arc_cx, arc_cy + 6, F, 0.44, t_color, 2)

    # text
    putl(panel, "EXPIRES IN", M + 82, y + 20, F, 0.26, GRAY, 1)
    exp_time = (datetime.now() + timedelta(seconds=secs_left)).strftime("%I:%M %p")  # local display only
    putl(panel, f"at {exp_time}", M + 82, y + 44, FB, 0.52, WHITE, 1)

    # thin progress bar
    bw2 = PW - M * 2 - 8
    bx2 = M + 4
    by2 = y + tch - 8
    rrect(panel, bx2, by2, bx2 + bw2, by2 + 4, 2, GRAY_DIM)
    rrect(panel, bx2, by2, bx2 + int(bw2 * frac), by2 + 4, 2, t_color)
    y += tch + 8

    # ── claim code hint row ──
    cch = 38
    rrect(panel, M, y, PW - M, y + cch, 8, CARD)
    cv2.rectangle(panel, (M, y), (M + 3, y + cch), AMBER, -1)
    putl(panel, "CLAIM CODE", M + 10, y + 14, F, 0.24, GRAY, 1)
    putr(panel, f"ECO-{claim_code}", PW - M - 10, y + 28, FB, 0.46, AMBER, 1)
    y += cch + 8

    # ── footer ──
    blend(panel, 0, WIN_H - 44, PW, WIN_H, CARD, 1.0)
    cv2.line(panel, (0, WIN_H - 44), (PW, WIN_H - 44), BORDER, 1)
    putc(panel, "R  — No phone? Show receipt to staff",
         PW // 2, WIN_H - 24, F, 0.30, AMBER, 1)
    putc(panel, "Single use  •  Auto-expires on scan",
         PW // 2, WIN_H - 8, F, 0.26, GRAY_DIM, 1)

    cam_r = cv2.resize(cam_frame, (CAM_W, WIN_H))
    return np.hstack([cam_r, panel])

# ── Receipt / Manual Claim Screen ─────────────────────────────────────────────
def draw_receipt(cam_frame, waste_type, points, claim_code, token=""):
    base = cv2.resize(cam_frame, (WIN_W, WIN_H))
    acc  = WASTE_COLOR.get(waste_type, ACCENT)
    blend(base, 0, 0, WIN_W, WIN_H, BLACK, 0.86)

    rw, rh = 560, 500
    rx  = WIN_W // 2 - rw // 2
    ry  = WIN_H // 2 - rh // 2
    cx2 = WIN_W // 2

    # ── card shadow (subtle dark offset) ──
    rrect(base, rx + 4, ry + 4, rx + rw + 4, ry + rh + 4, 16, (4, 5, 7))

    # ── card body ──
    rrect(base, rx, ry, rx + rw, ry + rh, 16, CARD)

    # ── top accent gradient bar ──
    for xi in range(rx, rx + rw):
        t   = (xi - rx) / rw
        col = tuple(int(acc[i] * (1 - t * 0.4)) for i in range(3))
        cv2.line(base, (xi, ry), (xi, ry + 5), col, 1)

    # ── header section ──
    blend(base, rx, ry, rx + rw, ry + 78, CARD2, 1.0)
    cv2.line(base, (rx, ry + 78), (rx + rw, ry + 78), BORDER, 1)

    # brand mark
    putc(base, "ECO",    cx2 - 28, ry + 36, FB, 0.72, ACCENT, 2)
    putc(base, "REWARD", cx2 + 28, ry + 36, FB, 0.72, WHITE,  1)
    # fix overlap — draw as one centered string
    putc(base, "ECOREWARD", cx2, ry + 36, FB, 0.72, ACCENT, 2)
    # overwrite right half in white
    (ew, _), _ = cv2.getTextSize("ECOREWARD", FB, 0.72, 2)
    (lw, _), _ = cv2.getTextSize("ECO",       FB, 0.72, 2)
    cv2.putText(base, "REWARD", (cx2 - ew // 2 + lw, ry + 36),
                FB, 0.72, WHITE, 2, cv2.LINE_AA)
    putc(base, "Official Waste Receipt", cx2, ry + 60, F, 0.30, GRAY, 1)

    # ── info rows ──
    def row(label, value, vy, vc=OFF_WHITE, bold=False):
        putl(base, label, rx + 28, vy, F, 0.34, GRAY, 1)
        font = FB if bold else F
        putr(base, value, rx + rw - 28, vy, font, 0.38, vc, 1)

    dot_divider(base, rx + 20, rx + rw - 20, ry + 88)

    _rt = datetime.now()
    row("Date",       _rt.strftime("%B %d, %Y"),   ry + 112)
    row("Time",       _rt.strftime("%I:%M:%S %p"), ry + 136)
    row("Waste Type", waste_type,                             ry + 160, acc,   bold=True)
    row("Points",     f"+{points} pts",                       ry + 184, AMBER, bold=True)
    row("Status",     "PENDING CLAIM",                        ry + 208, ACCENT)

    dot_divider(base, rx + 20, rx + rw - 20, ry + 222)

    # ── claim code section ──
    putc(base, "MANUAL CLAIM CODE", cx2, ry + 244, F, 0.30, GRAY, 1)
    display_code = f"ECO-{claim_code}"

    # code box — larger, prominent
    cw2 = 340
    cbx1, cbx2 = cx2 - cw2 // 2, cx2 + cw2 // 2
    cby1, cby2 = ry + 252, ry + 310
    rrect(base, cbx1, cby1, cbx2, cby2, 12, CARD2)
    # accent border
    rrect(base, cbx1, cby1, cbx2, cby2, 12, acc, t=2)
    # letter-spaced code — draw chars with spacing
    putc(base, display_code, cx2, cby2 - 12, FB, 1.10, WHITE, 3)

    # copy hint
    putc(base, "Show this code to staff to claim your points",
         cx2, ry + 330, F, 0.30, GRAY, 1)

    dot_divider(base, rx + 20, rx + rw - 20, ry + 344)

    # ── ref + validity ──
    putc(base, f"Ref: {token[:18].upper()}...", cx2, ry + 366, F, 0.26, GRAY_DIM, 1)
    putc(base, "Valid for one claim only  •  Single use",
         cx2, ry + 386, F, 0.27, GRAY_DIM, 1)

    dot_divider(base, rx + 20, rx + rw - 20, ry + 400)

    # ── eco footer strip ──
    blend(base, rx, ry + 400, rx + rw, ry + rh, CARD2, 1.0)
    putc(base, "Thank you for helping the environment!",
         cx2, ry + 432, FB, 0.42, acc, 1)
    putc(base, "ECOReward Smart Waste System  •  ecoreward-bin",
         cx2, ry + 456, F, 0.26, GRAY_DIM, 1)

    # ── bottom hint ──
    putc(base, "R  close receipt          Q  quit",
         WIN_W // 2, WIN_H - 12, F, 0.30, GRAY_DIM, 1)
    return base


# ── History Overlay ─────────────────────────────────────────────────────────
def draw_history(cam_frame, history):
    base = cv2.resize(cam_frame, (WIN_W, WIN_H))
    blend(base, 0, 0, WIN_W, WIN_H, BLACK, 0.88)

    pw, ph = 700, 560
    px = WIN_W // 2 - pw // 2
    py = WIN_H // 2 - ph // 2
    cx = WIN_W // 2

    # card bg
    rrect(base, px + 4, py + 4, px + pw + 4, py + ph + 4, 16, (4, 5, 7))
    rrect(base, px, py, px + pw, py + ph, 16, CARD)

    # header
    blend(base, px, py, px + pw, py + 62, CARD2, 1.0)
    cv2.line(base, (px, py + 62), (px + pw, py + 62), ACCENT, 2)
    # clock icon
    ic = (px + 36, py + 32)
    cv2.circle(base, ic, 16, ACCENT, 2, cv2.LINE_AA)
    cv2.line(base, ic, (ic[0], ic[1] - 10), ACCENT, 2, cv2.LINE_AA)
    cv2.line(base, ic, (ic[0] + 8, ic[1]), ACCENT, 2, cv2.LINE_AA)
    cv2.circle(base, ic, 2, ACCENT, -1, cv2.LINE_AA)

    putl(base, "SCAN HISTORY", px + 62, py + 28, FB, 0.62, WHITE, 2)
    putl(base, f"{len(history)} total redeemed", px + 64, py + 50, F, 0.28, GRAY, 1)
    putc(base, "H  close", px + pw - 50, py + 38, F, 0.28, GRAY_DIM, 1)

    # column headers
    hy = py + 66
    blend(base, px, hy, px + pw, hy + 22, GRAY_DIM, 0.5)
    putl(base, "#",          px + 14,  hy + 15, F, 0.27, GRAY, 1)
    putl(base, "WASTE TYPE", px + 40,  hy + 15, F, 0.27, GRAY, 1)
    putl(base, "POINTS",     px + 238, hy + 15, F, 0.27, GRAY, 1)
    putl(base, "CLAIM CODE", px + 338, hy + 15, F, 0.27, GRAY, 1)
    putl(base, "TIME",       px + 508, hy + 15, F, 0.27, GRAY, 1)

    row_h  = 44
    max_rows = 9
    entries = list(reversed(history))
    visible = entries[:max_rows]
    ry = hy + 26

    if not visible:
        putc(base, "No scans yet",              cx, WIN_H // 2 - 10, FB, 0.58, GRAY, 1)
        putc(base, "Generate a QR and scan it", cx, WIN_H // 2 + 20, F,  0.34, GRAY_DIM, 1)
    else:
        for i, entry in enumerate(visible):
            row_y = ry + i * row_h
            acc   = WASTE_COLOR.get(entry['waste_type'], ACCENT)
            if i % 2 == 0:
                blend(base, px, row_y, px + pw, row_y + row_h, CARD2, 0.6)
            cv2.rectangle(base, (px, row_y + 4), (px + 3, row_y + row_h - 4), acc, -1)

            num  = len(history) - i
            ty   = row_y + row_h - 14
            putl(base, str(num),               px + 14,  ty, F,  0.30, GRAY,   1)
            putl(base, entry['waste_type'],    px + 40,  ty, FB, 0.36, WHITE,  1)

            tag = WASTE_TAG.get(entry['waste_type'], '?')
            (tw2, _), _ = cv2.getTextSize(tag, F, 0.24, 1)
            rrect(base, px + 168, row_y + 10, px + 168 + tw2 + 10, row_y + row_h - 10, 4, acc)
            putl(base, tag, px + 173, row_y + row_h - 12, F, 0.24, BG, 1)

            putl(base, f"+{entry['points']} pts", px + 238, ty, FB, 0.38, AMBER,  1)
            putl(base, f"ECO-{entry['claim_code']}", px + 338, ty, FB, 0.36, ACCENT, 1)
            putl(base, entry['ts'],             px + 508, ty, F,  0.30, GRAY,   1)

            cv2.line(base, (px + 8, row_y + row_h), (px + pw - 8, row_y + row_h), BORDER, 1)

        if len(history) > max_rows:
            putc(base, f"+ {len(history) - max_rows} more (showing latest {max_rows})",
                 cx, py + ph - 14, F, 0.27, GRAY_DIM, 1)

    # footer
    blend(base, px, py + ph - 36, px + pw, py + ph, CARD2, 1.0)
    cv2.line(base, (px, py + ph - 36), (px + pw, py + ph - 36), BORDER, 1)
    putc(base, "Session history only  •  Press H to close",
         cx, py + ph - 12, F, 0.28, GRAY_DIM, 1)
    return base


# ── Success Overlay ────────────────────────────────────────────────────────────
def draw_success(cam_frame, waste_type, points):
    base = cv2.resize(cam_frame, (WIN_W, WIN_H))
    acc  = WASTE_COLOR.get(waste_type, ACCENT)
    blend(base, 0, 0, WIN_W, WIN_H, BLACK, 0.70)

    cw, ch2 = 460, 280
    cx  = WIN_W // 2
    cy  = WIN_H // 2
    bx  = cx - cw // 2
    by  = cy - ch2 // 2

    # card
    rrect(base, bx, by, bx + cw, by + ch2, 18, CARD)
    # top accent
    rrect(base, bx, by, bx + cw, by + 4, 0, acc)
    # bottom accent
    rrect(base, bx, by + ch2 - 4, bx + cw, by + ch2, 0, acc)

    # check circle
    cv2.circle(base, (cx, by + 72), 32, acc, -1, cv2.LINE_AA)
    # checkmark
    cv2.line(base, (cx - 13, by + 72), (cx - 3,  by + 84), BG, 4, cv2.LINE_AA)
    cv2.line(base, (cx - 3,  by + 84), (cx + 15, by + 56), BG, 4, cv2.LINE_AA)

    putc(base, "TRANSACTION COMPLETE",         cx, by + 122, FB, 0.68, WHITE,     2)
    putc(base, "QR code scanned successfully", cx, by + 146, F,  0.38, GRAY,      1)

    # points badge
    badge_w = 210
    rrect(base, cx - badge_w // 2, by + 158, cx + badge_w // 2, by + 198, 10, CARD2)
    putc(base, f"+{points} POINTS AWARDED", cx, by + 185, FB, 0.56, AMBER, 2)

    putc(base, waste_type.upper(),              cx, by + 224, F, 0.38, acc,      1)
    putc(base, "Ready for next transaction",    cx, by + 252, F, 0.34, GRAY_DIM, 1)
    return base

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
        now_iso = _now().isoformat()
        for doc in docs:
            data = doc.to_dict()
            if data.get('expiresAt', '') < now_iso:
                doc.reference.update({'expired': True, 'isUsed': True})
                print(f"[CLEANUP] Expired stale token {doc.id[:8]}...")
    except Exception as e:
        print(f"[CLEANUP WARNING] {e}")

# Run cleanup in a background thread so startup is not blocked
threading.Thread(target=_cleanup_stale_tokens, daemon=True).start()

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

# ── Splash / Startup Animation ────────────────────────────────────────────────
def draw_splash_animation(window_name):
    """Plays a branded startup splash animation (≈2.5s) using OpenCV."""
    import math
    total_frames = 120   # ~2.5s at 48fps equivalent
    cx, cy = WIN_W // 2, WIN_H // 2

    # Easing helpers
    def ease_out_cubic(t): return 1 - (1 - t) ** 3
    def ease_in_out(t):    return t * t * (3 - 2 * t)
    def ease_out_back(t):
        c1, c3 = 1.70158, 1.70158 + 1
        return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2

    for frame_i in range(total_frames):
        t = frame_i / (total_frames - 1)   # 0.0 → 1.0
        canvas = np.zeros((WIN_H, WIN_W, 3), np.uint8)

        # ── Phase 1 (0.0–0.15): bg fade in ──────────────────────────────────
        bg_alpha = min(1.0, t / 0.15)
        bg_val   = int(ease_out_cubic(bg_alpha) * 14)
        canvas[:] = (bg_val, bg_val + 2, bg_val + 4)

        # subtle radial vignette — darker edges
        for radius, alpha_v in [(460, 0.25), (340, 0.18), (200, 0.08)]:
            mask = np.zeros((WIN_H, WIN_W), np.float32)
            cv2.circle(mask, (cx, cy), radius, 1.0, -1)
            mask = cv2.GaussianBlur(mask, (0, 0), radius // 3)
            for c in range(3):
                canvas[:, :, c] = np.clip(
                    canvas[:, :, c].astype(np.float32) + mask * (CARD[c] - canvas[:, :, c].astype(np.float32)) * alpha_v,
                    0, 255).astype(np.uint8)

        # ── Phase 2 (0.10–0.50): logo circle scale-in ───────────────────────
        if t >= 0.10:
            logo_t  = min(1.0, (t - 0.10) / 0.30)
            scale   = ease_out_back(logo_t)
            r_outer = int(80 * scale)
            r_inner = int(72 * scale)
            if r_outer > 2:
                # glow ring
                for dr in range(8, 0, -1):
                    alpha_g = 0.04 * (9 - dr) * logo_t
                    glow_col = tuple(int(ACCENT[i] * alpha_g) for i in range(3))
                    cv2.circle(canvas, (cx, cy), r_outer + dr, glow_col, 2, cv2.LINE_AA)
                # filled circle
                cv2.circle(canvas, (cx, cy), r_outer, CARD2, -1, cv2.LINE_AA)
                cv2.circle(canvas, (cx, cy), r_outer, ACCENT, 2, cv2.LINE_AA)

                # leaf / eco icon inside — simple overlapping circles
                if scale > 0.5:
                    icon_r = int(28 * scale)
                    leaf_x, leaf_y = cx, cy - 8
                    cv2.circle(canvas, (leaf_x,          leaf_y), icon_r, ACCENT, -1, cv2.LINE_AA)
                    cv2.circle(canvas, (leaf_x + icon_r, leaf_y), icon_r, CARD2,  -1, cv2.LINE_AA)
                    # stem
                    cv2.line(canvas, (cx, cy - 8 + icon_r - 4),
                             (cx, cy + icon_r + 4), ACCENT, 2, cv2.LINE_AA)

        # ── Phase 3 (0.40–0.75): brand text slide up ────────────────────────
        if t >= 0.40:
            txt_t  = min(1.0, (t - 0.40) / 0.28)
            txt_a  = ease_out_cubic(txt_t)
            slide  = int((1 - txt_a) * 30)
            ty_eco = cy + 104 + slide

            # ECO  REWARD  text
            (ew, eh), _ = cv2.getTextSize("ECO", FB, 1.10, 3)
            (rw, _),  _ = cv2.getTextSize("REWARD", FB, 1.10, 2)
            total_w = ew + rw + 12
            lx = cx - total_w // 2

            # draw with alpha-sim — just draw dimmed first then bright
            eco_col    = tuple(int(c * txt_a) for c in ACCENT)
            reward_col = tuple(int(c * txt_a) for c in WHITE)
            cv2.putText(canvas, "ECO",    (lx, ty_eco),          FB, 1.10, eco_col,    3, cv2.LINE_AA)
            cv2.putText(canvas, "REWARD", (lx + ew + 12, ty_eco), FB, 1.10, reward_col, 2, cv2.LINE_AA)

            # tagline
            tag_col = tuple(int(c * txt_a) for c in GRAY)
            putc(canvas, "Smart Waste Kiosk System", cx, ty_eco + 28, F, 0.38, tag_col, 1)

        # ── Phase 4 (0.62–0.88): loading bar ────────────────────────────────
        if t >= 0.62:
            bar_t   = min(1.0, (t - 0.62) / 0.26)
            bar_a   = ease_in_out(bar_t)
            bar_w   = 280
            bar_h_v = 4
            bx1     = cx - bar_w // 2
            by1     = cy + 148
            # track
            rrect(canvas, bx1, by1, bx1 + bar_w, by1 + bar_h_v, 2, CARD2)
            # fill
            fill_w  = int(bar_w * bar_a)
            if fill_w > 2:
                rrect(canvas, bx1, by1, bx1 + fill_w, by1 + bar_h_v, 2, ACCENT)
                # leading glow dot
                cv2.circle(canvas, (bx1 + fill_w, by1 + bar_h_v // 2),
                           4, ACCENT, -1, cv2.LINE_AA)

            # loading label
            pct       = int(bar_a * 100)
            pct_col   = tuple(int(c * min(1.0, bar_t * 2)) for c in GRAY)
            putc(canvas, f"Initializing  {pct}%", cx, by1 + 22, F, 0.30, pct_col, 1)

        # ── Phase 5 (0.88–1.0): fade to black before handoff ────────────────
        if t >= 0.88:
            fade_t = (t - 0.88) / 0.12
            fade_a = ease_in_out(fade_t)
            overlay = canvas.copy()
            overlay[:] = (8, 10, 14)
            cv2.addWeighted(overlay, fade_a, canvas, 1 - fade_a, 0, canvas)

        cv2.imshow(window_name, canvas)
        if cv2.waitKey(20) & 0xFF == ord('q'):
            cv2.destroyAllWindows()
            sys.exit(0)


# ── Webcam ─────────────────────────────────────────────────────────────────────
def main():
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[CAMERA ERROR] Could not access webcam!")
        sys.exit(1)

    print("\n------------------------------------------")
    print("  ECOReward Bin  |  1280x720  |  Kiosk Mode")
    print("  SPACEBAR = Generate QR    H = History    Q = Quit")
    print("------------------------------------------\n")

    cv2.namedWindow("ECOReward Bin", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("ECOReward Bin", WIN_W, WIN_H)

    # ── Play startup splash ──
    draw_splash_animation("ECOReward Bin")


    latest_label      = "Unknown"
    latest_waste_type = "Residual"
    latest_conf       = 0.0
    fb_ok             = db is not None
    scan_count        = 0

    # ── Detection smoothing (majority vote over last 7 frames) ──────────────
    DETECT_WINDOW   = 7
    detect_history  = deque(maxlen=DETECT_WINDOW)
    HOLD_FRAMES     = 0   # counts frames since last real detection
    HOLD_MAX        = 15  # keep last result for 15 frames before resetting
    _last_beep_type = None  # track last beeped class to avoid repeat beeps

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
    scan_history  = []   # list of dicts: {waste_type, points, claim_code, ts}

    cv2.namedWindow("ECOReward Bin", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("ECOReward Bin", WIN_W, WIN_H)

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # ── AI detection ──
        cam_r = cv2.resize(frame, (CAM_W, WIN_H))
        if not qr_active and not show_success:
            # Run YOLO on native resolution for better accuracy
            results  = model(frame, conf=0.15, verbose=False)
            fh, fw   = frame.shape[:2]
            sx, sy   = CAM_W / fw, WIN_H / fh   # scale factors to cam_r coords
            detected = False
            for r in results:
                for box in r.boxes:
                    cid  = int(box.cls[0])
                    lbl  = model.names[cid].lower()
                    conf = float(box.conf[0])
                    if conf < 0.30:
                        continue
                    detected = True
                    detect_history.append((lbl, conf))
                    # majority vote from history
                    vote_lbl = Counter(h[0] for h in detect_history).most_common(1)[0][0]
                    vote_conf = max(h[1] for h in detect_history if h[0] == vote_lbl)
                    latest_label      = vote_lbl
                    latest_waste_type = WASTE_MAP.get(vote_lbl, 'Residual')
                    latest_conf       = vote_conf
                    HOLD_FRAMES       = 0
                    # beep once when a new class is first confirmed
                    if vote_lbl != _last_beep_type:
                        _last_beep_type = vote_lbl
                        threading.Thread(target=_beep, args=(880, 120), daemon=True).start()
                    # scale bbox from native frame → cam_r
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    x1, y1 = int(x1 * sx), int(y1 * sy)
                    x2, y2 = int(x2 * sx), int(y2 * sy)
                    ac  = WASTE_COLOR[latest_waste_type]
                    cv2.rectangle(cam_r, (x1, y1), (x2, y2), ac, 2, cv2.LINE_AA)
                    # label tag above box
                    tag = f"{latest_waste_type}  {vote_conf * 100:.0f}%"
                    (tw, th), _ = cv2.getTextSize(tag, F, 0.44, 1)
                    rrect(cam_r, x1, y1 - th - 10, x1 + tw + 10, y1, 4, ac)
                    cv2.putText(cam_r, tag, (x1 + 5, y1 - 3), F, 0.44, BG, 1, cv2.LINE_AA)
                    # confidence bar directly on bbox bottom edge
                    bar_w = x2 - x1
                    bar_y = y2 - 6
                    rrect(cam_r, x1, bar_y, x2, y2, 0, GRAY_DIM)
                    rrect(cam_r, x1, bar_y, x1 + int(bar_w * vote_conf), y2, 0, ac)
            if not detected:
                HOLD_FRAMES += 1
                if HOLD_FRAMES > HOLD_MAX:
                    detect_history.clear()
                    latest_label      = "Unknown"
                    latest_waste_type = "Residual"
                    latest_conf       = 0.0
                    _last_beep_type   = None  # reset so next detection beeps again

        # ── build display ──
        if show_history:
            display = draw_history(cam_r, scan_history)

        elif show_receipt and qr_token:
            display = draw_receipt(cam_r, qr_waste_type, qr_points, qr_claim_code, qr_token or "")

        elif show_success and success_until:
            if _now() < success_until:
                display = draw_success(cam_r, success_waste, success_points)
            else:
                show_success = False
                display = draw_camera_hud(cam_r, latest_label, latest_waste_type,
                                          latest_conf, fb_ok, scan_count)

        elif qr_active and qr_expires_at:
            secs_left = max(0, int((qr_expires_at - _now()).total_seconds()))

            if _scan_event.is_set():
                _scan_event.clear()
                scan_count    += 1
                print(f"[SCANNED] +{qr_points} pts | Total scans: {scan_count}")
                show_success   = True
                success_until  = _now() + timedelta(seconds=4)
                success_waste  = qr_waste_type
                success_points = qr_points
                # record to session history
                scan_history.append({
                    'waste_type':  qr_waste_type,
                    'points':      qr_points,
                    'claim_code':  qr_claim_code,
                    'ts':          datetime.now().strftime("%b %d  %I:%M %p"),
                })
                qr_active  = False
                display = draw_success(cam_r, success_waste, success_points)

            elif secs_left == 0:
                print("[EXPIRED] QR expired.")
                qr_active = False
                display = draw_camera_hud(cam_r, latest_label, latest_waste_type,
                                          latest_conf, fb_ok, scan_count)
            else:
                display = draw_qr_panel(cam_r, qr_image, qr_waste_type, qr_points,
                                        secs_left, qr_claim_code, qr_total_secs)
        else:
            display = draw_camera_hud(cam_r, latest_label, latest_waste_type,
                                      latest_conf, fb_ok, scan_count)

        cv2.imshow("ECOReward Bin", display)
        key = cv2.waitKey(1) & 0xFF

        if key == ord('r') and qr_active:
            show_receipt = not show_receipt

        if key == ord('h'):
            show_history = not show_history
            show_receipt = False

        if key == 32 and not qr_active and not show_success:
            if db is None:
                # A QR for a token that was never stored could never be redeemed (audit C-03): refuse.
                print("[BLOCKED] Firebase is offline - not issuing a QR code (it could never be redeemed).")
            else:
                token      = str(uuid.uuid4())
                waste_type = latest_waste_type
                points     = POINTS_MAP[waste_type]
                expires_at = _now() + timedelta(minutes=QR_EXPIRY_MINUTES)
                qr_data    = f"ECOBIN:{waste_type}:{token}"

                print(f"\n[QR GENERATED] {waste_type} +{points}pts token={token[:8]}...")

                _scan_event.clear()   # new QR: drop any scan signal left over from the previous one,
                                      # even if the Firestore write below fails and no listener is started
                try:
                    claim_code = str(random.randint(1000, 9999))
                    db.collection("qr_tokens").document(token).set({
                        "wasteType": waste_type,
                        "expiresAt": expires_at.isoformat(),
                        "usedBy":    [],
                        "isUsed":    False,
                        "createdAt": _now().isoformat(),
                        "claimCode": claim_code,
                    })
                    qr_claim_code = claim_code
                    print("[FIREBASE] Token saved — waiting for scan...\n")
                    watch_token(token)   # also clears any scan flag left over from the previous QR
                except Exception as err:
                    print(f"[FIREBASE ERROR] {err}\n")

                qr_image      = generate_qr(qr_data, size=220)
                qr_active     = True
                qr_token      = token
                qr_waste_type = waste_type
                qr_points     = points
                qr_expires_at = expires_at
                qr_total_secs = QR_EXPIRY_MINUTES * 60
                show_receipt  = False

        if key == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()
    print("\n[SYSTEM] Shut down.")


if __name__ == '__main__':
    main()
