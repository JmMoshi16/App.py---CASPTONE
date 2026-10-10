"""Every kiosk screen, drawn from one View (a snapshot of the flow + system status) per frame.

Layout (DESIGN_SYSTEM.md §2): top bar, optional banner, camera viewport on the left, stage panel on the
right; full-screen cards for the result moments; modals for staff tools. Static parts (background, top bar
frame, panel card) are cached per layout and copied each frame; only live parts are redrawn.
"""
import math
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import draw as D
from .components import UI
from .icons import draw_icon, draw_icon_centered
from .theme import C, CATEGORY, Metrics, category

PHASE_CAPTION = {
    "ready": ("camera", "Looking for an item…"),
    "detected": ("check", "Item recognised"),
    "sorting": ("arrow_down", "Sorting…"),
    "confirming": ("scale", "Checking the drop…"),
    "issuing": ("qr", "Creating your code…"),
}


@dataclass
class View:
    phase: str = "ready"
    mode: str = "attendant"                 # attendant | esp32 | sim
    online: bool = True
    model_ok: bool = True
    camera_ok: bool = True
    sorter_state: str = "not used"
    sorter_detail: str = ""
    sorter_ready: bool = False
    detection: object = None                # flow.Detection (voted)
    latest: object = None                   # flow.Detection (this frame, has the box)
    frame_size: tuple = (640, 480)
    lock_progress: float = 0.0
    armed: bool = True
    stages: list = field(default_factory=list)
    tx: object = None                       # flow.Transaction
    points: dict = field(default_factory=dict)
    qr_modules: object = None               # QR as 1 px per module (uint8, 0/255), quiet zone included
    secs_left: int = 0
    total_secs: int = 1200
    expires_local: str = ""
    issued_local: str = ""
    issued_date: str = ""
    failure: object = None                  # flow.Failure
    toast: object = None                    # flow.Toast
    phase_age: float = 0.0
    completed: int = 0
    history: list = field(default_factory=list)
    overlay: object = None                  # None | "receipt" | "history" | "diagnostics"
    diagnostics: list = field(default_factory=list)   # [(section, [(key, value), ...]), ...]
    debug: bool = False
    clock: str = ""
    seconds_to_ready: float = 0.0

    @property
    def simulated(self):
        return self.mode == "sim"

    @property
    def automatic(self):
        return self.mode in ("esp32", "sim")


def _ease(t):
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


class Renderer:
    def __init__(self, width, height, reduce_motion=False):
        self.m = Metrics(int(width), int(height), reduce_motion)
        self.ts = D.Typesetter(self.m)
        self.ui = UI(self.m, self.ts)
        self._static = D.LRU(8)
        self._last_key = None
        self._last_out = None
        self._fade_from = None
        self._fade_t0 = 0.0

    @property
    def size(self):
        return self.m.width, self.m.height

    @property
    def buttons(self):
        return self.ui.buttons

    def hit(self, x, y):
        for x1, y1, x2, y2, action in reversed(self.ui.buttons):
            if x1 <= x <= x2 and y1 <= y <= y2:
                return action
        return None

    # ── layout ─────────────────────────────────────────────────────────────
    def layout(self, banner):
        px, W, H = self.m.px, self.m.width, self.m.height
        top = px(64)
        banner_h = px(44) if banner else 0
        margin, gutter = px(24), px(24)
        body_top = top + banner_h + margin
        panel_w = max(px(420), int(round(W * 0.40)))
        panel = (W - margin - panel_w, body_top, W - margin, H - margin)
        view = (margin, body_top, panel[0] - gutter, H - margin)
        return {"top": top, "banner": (0, top, W, top + banner_h) if banner else None,
                "panel": panel, "view": view, "pad": px(28)}

    def _banner(self, v):
        if not v.model_ok:
            return ("Item recognition needs attention — reward codes are paused. Please ask staff.", "error")
        if not v.online:
            return (("Reward codes are paused — the kiosk is offline. You can still recycle."
                     if v.automatic else "Reward codes are paused — the kiosk is offline. Please ask staff."),
                    "warning")
        return None

    # ── entry point ────────────────────────────────────────────────────────
    def render(self, cam, v, now, transition=True):
        """One frame. transition=False draws the screen as is (single screenshots, tools, tests)."""
        if not transition:
            self._last_key = self._last_out = self._fade_from = None
        self.ui.now = now
        self.ui.buttons = []
        banner = self._banner(v)
        L = self.layout(banner is not None)
        img = self._base(L, banner).copy()
        self._top_bar(img, v, L)
        if v.phase in ("complete", "failed"):
            self._viewport(img, cam, v, L, dim=0.55)
            self._panel_card(img, L)
            self._result(img, v, L, now)
        else:
            self._viewport(img, cam, v, L, dim=0.45 if v.phase == "qr" else 0.0)
            self._panel_card(img, L)
            {"ready": self._ready, "detected": self._detected, "qr": self._qr}.get(v.phase, self._working)(img, v, L, now)
        if v.overlay:
            self._modal(img, v, now)
        if v.toast:
            left = v.toast.until - now
            alpha = 1.0 if self.m.reduce_motion else min(1.0, left / 0.3)
            self.ui.toast(img, self.m.width / 2, self.m.height - self.m.px(36), v.toast.text, v.toast.kind,
                          max(0.0, min(1.0, alpha)))
        return self._crossfade(img, (v.phase, v.overlay), now)

    def _crossfade(self, img, key, now):
        dur = self.m.duration("base")
        if key != self._last_key and self._last_out is not None and dur > 0:
            self._fade_from, self._fade_t0 = self._last_out, now
        self._last_key = key
        if self._fade_from is not None:
            a = (now - self._fade_t0) / dur if dur else 1.0
            if a >= 1 or self._fade_from.shape != img.shape:
                self._fade_from = None
            else:
                D.blend_images(img, self._fade_from, 1 - _ease(a))
        self._last_out = img
        return img

    # ── static frame ───────────────────────────────────────────────────────
    def _base(self, L, banner):
        key = (banner,)

        def make():
            W, H = self.size
            img = np.empty((H, W, 3), np.uint8)
            img[:] = C.canvas
            self.ui.divider(img, 0, W, L["top"] - 1)
            if banner:
                x1, y1, x2, y2 = L["banner"]
                self.ui.banner(img, x1, y1, x2, y2, *banner)
            x1, y1, x2, y2 = L["panel"]
            self.ui.card(img, x1, y1, x2, y2)
            self._wordmark(img, L)
            return img
        return self._static.get_or(key, make)

    def _panel_card(self, img, L):
        pass   # the card is part of the cached static frame

    def _wordmark(self, img, L, cx=None, cy=None, big=False):
        px = self.m.px
        r = px(26 if big else 17)
        x0 = px(24) if cx is None else cx
        cy = L["top"] / 2 if cy is None else cy
        D.circle(img, x0 + r, cy, r, C.forest)
        draw_icon_centered(img, "leaf", x0 + r, cy, int(r * 1.15), C.white)
        role = "title" if big else "heading"
        x = x0 + 2 * r + px(12)
        eco = self.ts.draw(img, "ECO", x, cy, role, C.forest, "lm")
        self.ts.draw(img, "Reward", eco[2] - 1, cy, role, C.ink, "lm")

    # ── top bar ────────────────────────────────────────────────────────────
    def _top_bar(self, img, v, L):
        px, ui = self.m.px, self.ui
        y = L["top"] / 2
        x = self.ts.draw(img, v.clock, self.m.width - px(24), y, "body", C.ink_2, "rm")[0] - px(16)
        x = ui.chip(img, x, y, "Camera" if v.camera_ok else "No camera", "ok" if v.camera_ok else "error",
                    icon="camera") - px(8)
        if v.mode == "sim":
            x = ui.chip(img, x, y, "SIMULATION", "sim", icon="alert") - px(8)
        elif v.mode == "esp32":
            st = {"connected": ("Sorter ready", "ok", False), "connecting": ("Sorter connecting", "busy", True),
                  "reconnecting": ("Sorter reconnecting", "busy", True), "error": ("Sorter error", "error", False)}
            label, style, pulse = st.get(v.sorter_state, ("Sorter offline", "error", False))
            x = ui.chip(img, x, y, label, style, pulse=pulse) - px(8)
        else:
            x = ui.chip(img, x, y, "Staff confirm", "off") - px(8)
        ui.chip(img, x, y, "Online" if v.online else "Offline", "ok" if v.online else "error",
                icon="wifi" if v.online else "wifi_off")

    # ── camera viewport ────────────────────────────────────────────────────
    def _viewport(self, img, cam, v, L, dim=0.0):
        px, ui = self.m.px, self.ui
        x1, y1, x2, y2 = L["view"]
        w, h = x2 - x1, y2 - y1
        r = px(28)
        if cam is None or not v.camera_ok:
            D.fill_rr(img, x1, y1, x2, y2, r, C.sunken)
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            draw_icon_centered(img, "camera", cx, cy - px(40), px(56), C.ink_3)
            ui.text(img, "Camera unavailable", cx, cy + px(16), "heading", C.ink, "mt")
            ui.text(img, "Reconnecting automatically…", cx, cy + px(52), "body", C.ink_2, "mt")
            return
        view, (scale, ox, oy) = D.cover(cam, w, h)
        view = view.copy()
        if v.phase in ("ready", "detected") and v.latest is not None and v.latest.box is not None:
            self._detection_box(view, v, scale, ox, oy)
        if dim > 0:
            D.blend_rect(view, 0, 0, w, h, C.canvas, dim)
        D.paste_rr(img, view, x1, y1, r)
        if v.mode == "sim":
            ui.chip(img, x1 + px(16) + self._chip_w("SIMULATED SORTER — NO HARDWARE", "alert"), y1 + px(32),
                    "SIMULATED SORTER — NO HARDWARE", "sim", icon="alert")
        if v.phase == "qr":
            self._qr_instructions(img, v, L)
            return
        if v.phase in ("complete", "failed"):
            return
        icon, label = PHASE_CAPTION.get(v.phase, ("camera", ""))
        if v.phase == "detected" and v.detection is not None:
            label = f"Recognised: {v.detection.waste_type or 'not a known item'}"
            icon = "check" if v.detection.waste_type else "alert"
        cw = self._chip_w(label, icon)
        ui.chip(img, x1 + px(16) + cw, y2 - px(32), label, "info", icon=icon)

    def _chip_w(self, label, icon=None):
        px = self.m.px
        return px(12) + (px(16) if icon else px(8)) + px(8) + self.ts.measure(label, "label") + px(12)

    def _detection_box(self, view, v, scale, ox, oy):
        px = self.m.px
        d = v.latest
        cat = category(v.detection.waste_type if v.detection else d.waste_type)
        bx1, by1, bx2, by2 = [float(c) for c in d.box]
        bx1, bx2 = bx1 * scale - ox, bx2 * scale - ox
        by1, by2 = by1 * scale - oy, by2 * scale - oy
        H, W = view.shape[:2]
        bx1, by1 = max(2, int(bx1)), max(2, int(by1))
        bx2, by2 = min(W - 2, int(bx2)), min(H - 2, int(by2))
        if bx2 - bx1 < 8 or by2 - by1 < 8:
            return
        D.stroke_rr(view, bx1, by1, bx2, by2, px(14), max(2, px(3)), cat["color"])
        conf = v.detection.confidence if v.detection else d.confidence
        tag = f"{(v.detection.waste_type if v.detection else d.waste_type) or 'Unknown'} · {conf * 100:.0f}%"
        tw = self.ts.measure(tag, "label")
        th, pad, ic = px(30), px(10), px(16)
        tx1 = bx1
        ty1 = by1 - th - px(6) if by1 - th - px(6) > 0 else by1 + px(6)
        D.fill_rr(view, tx1, ty1, tx1 + pad + ic + px(6) + tw + pad, ty1 + th, 999, cat["color"])
        draw_icon_centered(view, cat["icon"], tx1 + pad + ic / 2, ty1 + th / 2, ic, C.white)
        self.ts.draw(view, tag, tx1 + pad + ic + px(6), ty1 + th / 2, "label", C.white, "lm")

    def _qr_instructions(self, img, v, L):
        px, ui = self.m.px, self.ui
        x1, y1, x2, y2 = L["view"]
        w = min(px(460), x2 - x1 - px(48))
        h = px(300)
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        cx1, cy1 = int(cx - w / 2), int(cy - h / 2)
        ui.card(img, cx1, cy1, cx1 + w, cy1 + h)
        pad = px(28)
        y = cy1 + pad
        ok = "Sorted" if v.automatic else "Deposit confirmed"
        ui.category_pill(img, cx1 + pad, y, v.tx.waste_type if v.tx else None, ok)
        y += px(56)
        for n, text in ((1, "Open the ECOReward app"), (2, "Tap Scan"), (3, "Point your phone at the code")):
            D.circle(img, cx1 + pad + px(16), y + px(16), px(16), C.sage)
            ui.text(img, str(n), cx1 + pad + px(16), y + px(16), "body_strong", C.forest, "mm")
            ui.text(img, text, cx1 + pad + px(44), y + px(16), "body_lg", C.ink, "lm", max_w=w - pad * 2 - px(44))
            y += px(52)
        draw_icon(img, "phone", cx1 + w - pad - px(40), cy1 + pad + px(2), px(36), C.ink_3)

    # ── panel content ──────────────────────────────────────────────────────
    def _inner(self, L):
        x1, y1, x2, y2 = L["panel"]
        p = L["pad"]
        return x1 + p, y1 + p, x2 - p, y2 - p

    def _ready(self, img, v, L, now):
        ui, px = self.ui, self.m.px
        x1, y, x2, y2 = self._inner(L)
        w = x2 - x1
        y = ui.overline(img, "Ready", x1, y) + px(10)
        y = ui.para(img, "Recycle your cup, earn points", x1, y, w, "title", C.ink, max_lines=2) + px(10)
        body = ("Hold your item up to the camera. We'll sort it and give you a reward code."
                if v.automatic else "Hold your item up to the camera. A staff member will confirm your deposit.")
        y = ui.para(img, body, x1, y, w, "body_lg", C.ink_2, max_lines=3) + px(22)
        ui.text(img, "WHAT YOU EARN", x1, y, "label", C.ink_2)
        y += px(30)
        tile = px(48)
        for name, cat in CATEGORY.items():
            ui.icon_tile(img, x1, y, tile, cat["icon"], cat["color"], cat["tint"])
            ui.text(img, name, x1 + tile + px(16), y + tile / 2, "body_strong", C.ink, "lm")
            pts = f"+{v.points.get(name, 0)}"
            pr = ui.text(img, "pts", x2, y + tile / 2 + px(3), "caption", C.ink_2, "rm")
            ui.text(img, pts, pr[0] - px(6), y + tile / 2, "heading", cat["color"], "rm")
            y += tile + px(12)
        draw_icon(img, "info", x1, y2 - px(40), px(18), C.ink_3)
        ui.para(img, "Base points. Streak bonuses are added in the ECOReward app.", x1 + px(28), y2 - px(41),
                w - px(28), "caption", C.ink_2, max_lines=2)

    def _detected(self, img, v, L, now):
        ui, px = self.ui, self.m.px
        x1, y, x2, y2 = self._inner(L)
        w = x2 - x1
        d = v.detection
        waste = d.waste_type if d else None
        cat = category(waste)
        y = ui.overline(img, "Item detected", x1, y) + px(16)
        tile = px(96)
        ui.icon_tile(img, x1, y, tile, cat["icon"], cat["color"], cat["tint"], radius=24)
        tx = x1 + tile + px(20)
        ui.text(img, waste or "Not recognised", tx, y + px(8), "title", C.ink, max_w=x2 - tx)
        if waste:
            ui.text(img, f"+{v.points.get(waste, 0)} base points", tx, y + px(56), "body_lg", cat["color"], max_w=x2 - tx)
        y += tile + px(22)
        conf = d.confidence if d else 0.0
        ui.text(img, "Recognition confidence", x1, y, "caption", C.ink_2)
        ui.text(img, f"{conf * 100:.0f}%", x2, y, "body_strong", C.ink, "rt")
        y = ui.meter(img, x1, y + px(28), x2, conf, cat["color"]) + px(10)
        if d:
            ui.text(img, f"Recognised as “{d.label}”", x1, y, "caption", C.ink_2, max_w=w)
        y += px(36)
        if not v.model_ok:
            y = ui.note(img, x1, y, x2, "Reward codes are paused", "The recognition model needs attention. Please ask staff.", "error")
        elif not waste:
            y = ui.note(img, x1, y, x2, "We don't know this item", "Please ask staff where it goes.", "warning")
        elif v.automatic and not v.sorter_ready:
            y = ui.note(img, x1, y, x2, "The sorter is reconnecting", "Please wait a moment.", "warning")
        elif v.automatic and not v.armed:
            y = ui.note(img, x1, y, x2, "Move the item away first",
                        "Take it out of view, then show it again to start a new deposit.", "info")
        elif v.automatic:
            y = ui.note(img, x1, y, x2, "Hold still", "Sorting starts in a moment.", "ok", icon="clock")
            ui.meter(img, x1, y + px(12), x2, v.lock_progress, C.forest, height=6)
        else:
            y = ui.note(img, x1, y, x2, "Waiting for staff", "A staff member will confirm your deposit.", "info",
                        icon="list")
            ui.text(img, "Staff: press SPACE to confirm", x1, y + px(12), "caption", C.ink_3)
        ui.stepper(img, x1, y2 - len(v.stages) * px(40), w, v.stages, compact=True)

    def _working(self, img, v, L, now):
        ui, px = self.ui, self.m.px
        x1, y, x2, y2 = self._inner(L)
        w = x2 - x1
        waste = v.tx.waste_type if v.tx else "your"
        title = {"sorting": f"Sorting into the {waste} bin", "confirming": "Checking that it dropped",
                 "issuing": "Creating your reward code"}.get(v.phase, "Working on it")
        y = ui.overline(img, "Working on it", x1, y) + px(10)
        y = ui.para(img, title, x1, y, w, "title", C.ink, max_lines=2) + px(14)
        if v.tx:
            ui.category_pill(img, x1, y, v.tx.waste_type, f"+{v.points.get(v.tx.waste_type, 0)} base points")
        y += px(64)
        y = ui.stepper(img, x1, y, w, v.stages) + px(16)
        ui.text(img, "This takes a few seconds.", x1, y, "body", C.ink_2)

    def _qr(self, img, v, L, now):
        ui, px = self.ui, self.m.px
        x1, y, x2, y2 = self._inner(L)
        w = x2 - x1
        pts = v.points.get(v.tx.waste_type, 0) if v.tx else 0
        y = ui.overline(img, "Your reward code", x1, y) + px(8)
        y = ui.para(img, f"Scan to collect +{pts} points", x1, y, w, "title_sm", C.ink, max_lines=2) + px(14)
        buttons_h = px(56)
        timer_h = px(52)
        fr = px(6)
        avail = (y2 - buttons_h - px(16) - timer_h - px(16)) - y - 2 * fr
        if v.qr_modules is not None:
            n = v.qr_modules.shape[0]
            k = max(1, int(min(w - 2 * fr, avail) // n))   # whole pixels per module: crisp, scannable
            qr = cv2.resize(v.qr_modules, (n * k, n * k), interpolation=cv2.INTER_NEAREST)
            qx = int(x1 + (w - n * k) / 2)
            D.stroke_rr(img, qx - fr, y, qx + n * k + fr, y + n * k + 2 * fr, px(16), 1, C.border)
            y += fr
            region = img[y:y + n * k, qx:qx + n * k]
            qr3 = cv2.cvtColor(qr, cv2.COLOR_GRAY2BGR)
            dur = self.m.duration("slow")
            a = _ease(v.phase_age / dur) if dur else 1.0
            if a >= 1:
                region[:] = qr3
            else:
                cv2.addWeighted(qr3, a, region, 1 - a, 0, dst=region)
            y += n * k + px(20)
        frac = v.secs_left / max(1, v.total_secs)
        color = C.forest if v.secs_left > 300 else C.amber if v.secs_left > 60 else C.error
        r = px(20)
        ui.ring(img, x1 + r + px(2), y + timer_h / 2, r, frac, color, 5)
        draw_icon_centered(img, "clock", x1 + r + px(2), y + timer_h / 2, px(18), color)
        mm, ss = divmod(max(0, int(v.secs_left)), 60)
        ui.text(img, f"{mm}:{ss:02d} left", x1 + 2 * r + px(18), y + px(4), "body_strong", C.ink)
        ui.text(img, f"Valid until {v.expires_local}", x1 + 2 * r + px(18), y + px(28), "caption", C.ink_2)
        by = y2 - buttons_h
        done_w = px(120)
        ui.button(img, x1, by, x2 - done_w - px(12), y2, "No phone? Claim code", "receipt", "secondary", icon="receipt")
        ui.button(img, x2 - done_w, by, x2, y2, "Done", "dismiss", "ghost")

    # ── result moments ─────────────────────────────────────────────────────
    def _result(self, img, v, L, now):
        ui, px = self.ui, self.m.px
        W, H = self.size
        D.blend_rect(img, 0, L["top"], W, H, C.ink, 0.35)
        cw, ch = min(px(600), W - px(48)), px(440)
        cx1, cy1 = int((W - cw) / 2), int(L["top"] + (H - L["top"] - ch) / 2)
        ui.card(img, cx1, cy1, cx1 + cw, cy1 + ch, radius=24, elevation="modal")
        cx = cx1 + cw / 2
        pad = px(36)
        if v.phase == "complete":
            self._success(img, v, cx1, cy1, cw, ch, pad, now)
        else:
            self._failure(img, v, cx1, cy1, cw, ch, pad, now)

    def _success(self, img, v, cx1, cy1, cw, ch, pad, now):
        ui, px = self.ui, self.m.px
        cx = cx1 + cw / 2
        r = px(46)
        cy = cy1 + pad + r
        D.circle(img, cx, cy, r, C.forest)
        dur = self.m.duration("slow")
        p = _ease(v.phase_age / dur) if dur else 1.0
        pts = [(cx - r * 0.42, cy + r * 0.02), (cx - r * 0.1, cy + r * 0.34), (cx + r * 0.45, cy - r * 0.32)]
        seg1 = min(1.0, p * 2)
        mid = (pts[0][0] + (pts[1][0] - pts[0][0]) * seg1, pts[0][1] + (pts[1][1] - pts[0][1]) * seg1)
        D.line(img, pts[0], mid, C.white, px(7))
        if p > 0.5:
            seg2 = (p - 0.5) * 2
            end = (pts[1][0] + (pts[2][0] - pts[1][0]) * seg2, pts[1][1] + (pts[2][1] - pts[1][1]) * seg2)
            D.line(img, pts[1], end, C.white, px(7))
        for dx, dy, s in ((-1.9, -0.6, 22), (1.8, -0.9, 16), (2.1, 0.5, 12)):
            draw_icon_centered(img, "sparkle", cx + dx * r, cy + dy * r, px(s), C.amber, opacity=p)
        y = cy + r + px(22)
        ui.text(img, "Claimed!", cx, y, "display", C.ink, "mt")
        y += px(70)
        waste = v.tx.waste_type if v.tx else ""
        pts_n = v.points.get(waste, 0)
        dur_c = self.m.duration("count")
        shown = int(round(pts_n * (_ease(v.phase_age / dur_c) if dur_c else 1.0)))
        ui.text(img, f"+{shown} base points  ·  {waste}", cx, y, "body_lg", category(waste)["color"], "mt")
        y += px(36)
        ui.text(img, "Check your balance in the ECOReward app.", cx, y, "body", C.ink_2, "mt")
        self._auto_return(img, v, cx1, cy1, cw, ch, pad, "Thank you for recycling.")

    def _failure(self, img, v, cx1, cy1, cw, ch, pad, now):
        ui, px = self.ui, self.m.px
        f = v.failure
        err = f is not None and f.severity == "error"
        x = cx1 + pad
        y = cy1 + pad
        tile = px(64)
        ui.icon_tile(img, x, y, tile, "alert", C.error if err else C.amber_ink, C.error_tint if err else C.amber_tint)
        ui.para(img, f.title if f else "Something went wrong", x + tile + px(18), y + px(4), cw - pad * 2 - tile - px(18),
                "title_sm", C.ink, max_lines=2)
        y += tile + px(20)
        y = ui.para(img, f.message if f else "", x, y, cw - pad * 2, "body_lg", C.ink_2, max_lines=4) + px(12)
        if v.stages:
            ui.stepper(img, x, y, cw - pad * 2, v.stages, compact=True)
        bw = px(160)
        ui.button(img, cx1 + cw - pad - bw, cy1 + ch - pad - px(56), cx1 + cw - pad, cy1 + ch - pad, "OK", "dismiss",
                  "primary")
        self._auto_return(img, v, cx1, cy1, cw, ch, pad, None, leave=bw + px(16))

    def _auto_return(self, img, v, cx1, cy1, cw, ch, pad, lead, leave=0):
        ui, px = self.ui, self.m.px
        secs = max(0, int(math.ceil(v.seconds_to_ready)))
        text = f"{lead + ' ' if lead else ''}Back to the start in {secs} s"
        y = cy1 + ch - pad - px(56) + px(10)
        if leave:
            ui.text(img, text, cx1 + pad, y + px(8), "caption", C.ink_2, max_w=cw - pad * 2 - leave)
        else:
            total = v.seconds_to_ready + v.phase_age
            frac = v.seconds_to_ready / total if total > 0 else 0.0
            ui.meter(img, cx1 + pad, y - px(8), cx1 + cw - pad, frac, C.forest, height=4)
            ui.text(img, text, cx1 + cw / 2, y + px(14), "body", C.ink_2, "mt")

    # ── modals (receipt for customers, history / diagnostics for staff) ────
    def _modal(self, img, v, now):
        ui, px = self.ui, self.m.px
        W, H = self.size
        D.blend_rect(img, 0, 0, W, H, C.ink, 0.55)
        if v.overlay == "receipt" and v.tx is not None and v.tx.claim_code:
            self._receipt(img, v)
        elif v.overlay == "history":
            self._history(img, v)
        elif v.overlay == "diagnostics":
            self._diagnostics(img, v)

    def _modal_card(self, img, w, h, icon, title, subtitle):
        ui, px = self.ui, self.m.px
        W, H = self.size
        w, h = min(w, W - px(32)), min(h, H - px(32))
        x1, y1 = int((W - w) / 2), int((H - h) / 2)
        ui.card(img, x1, y1, x1 + w, y1 + h, radius=24, elevation="modal")
        pad = px(32)
        ui.icon_tile(img, x1 + pad, y1 + pad, px(48), icon, C.forest, C.sage)
        ui.text(img, title, x1 + pad + px(64), y1 + pad - px(2), "heading", C.ink, max_w=w - pad * 2 - px(140))
        if subtitle:
            ui.text(img, subtitle, x1 + pad + px(64), y1 + pad + px(28), "caption", C.ink_2, max_w=w - pad * 2 - px(140))
        ui.icon_button(img, x1 + w - pad - px(28), y1 + pad + px(24), "cross", "close")
        return x1, y1, w, h, pad

    def _receipt(self, img, v):
        ui, px = self.ui, self.m.px
        x1, y1, w, h, pad = self._modal_card(img, px(560), px(600), "receipt", "Manual claim receipt",
                                             "For customers without a phone")
        x, xr = x1 + pad, x1 + w - pad
        y = y1 + pad + px(76)
        rows = [("Date", v.issued_date), ("Issued", v.issued_local), ("Item", v.tx.waste_type),
                ("Base points", f"+{v.points.get(v.tx.waste_type, 0)}"), ("Status", "Not yet claimed")]
        for k, val in rows:
            ui.text(img, k, x, y, "body", C.ink_2)
            ui.text(img, val, xr, y, "body_strong", C.ink, "rt")
            y += px(32)
        y += px(8)
        D.fill_rr(img, x, y, xr, y + px(110), px(16), C.sunken)
        ui.text(img, "CLAIM CODE", (x + xr) / 2, y + px(16), "label", C.ink_2, "mt")
        ui.text(img, f"ECO-{v.tx.claim_code}", (x + xr) / 2, y + px(42), "code", C.ink, "mt")
        y += px(126)
        y = ui.para(img, f"Give this code and your Gmail address to staff before {v.expires_local}. "
                         "Staff credit your points from the ECOReward admin site.", x, y, xr - x, "body", C.ink_2,
                    max_lines=3) + px(6)
        ui.text(img, f"Ref {str(v.tx.token)[:8].upper()} · single use", x, y, "caption", C.ink_3)
        ui.button(img, x, y1 + h - pad - px(56), xr, y1 + h - pad, "Back to the QR code", "close", "secondary",
                  icon="qr")

    def _history(self, img, v):
        ui, px = self.ui, self.m.px
        n = len(v.history)
        x1, y1, w, h, pad = self._modal_card(img, px(820), px(580), "list", "Session history",
                                             f"{n} code{'s' if n != 1 else ''} claimed since this kiosk started · staff view")
        x, xr = x1 + pad, x1 + w - pad
        y = y1 + pad + px(80)
        if not v.history:
            cx, cy = x1 + w / 2, y1 + h / 2 + px(30)
            D.circle(img, cx, cy - px(40), px(36), C.sunken)
            draw_icon_centered(img, "list", cx, cy - px(40), px(32), C.ink_3)
            ui.text(img, "No claimed codes yet", cx, cy + px(8), "heading", C.ink, "mt")
            ui.text(img, "Codes appear here after a customer scans them.", cx, cy + px(44), "body", C.ink_2, "mt")
            return
        cols = [x, x + px(130), xr - px(300), xr - px(150)]
        for cxh, label in zip(cols, ("TIME", "ITEM", "BASE POINTS", "CLAIM CODE")):
            ui.text(img, label, cxh, y, "label", C.ink_2)
        y += px(30)
        ui.divider(img, x, xr, y)
        row = px(50)
        fit = max(1, int((y1 + h - pad - y) // row))
        for e in list(reversed(v.history))[:fit]:
            ui.text(img, e["ts"], cols[0], y + row / 2, "body", C.ink_2, "lm")
            ui.category_pill(img, cols[1], int(y + row / 2 - px(18)), e["waste_type"])
            ui.text(img, f"+{e['points']}", cols[2], y + row / 2, "body_strong", C.ink, "lm")
            ui.text(img, f"ECO-{e['claim_code']}", cols[3], y + row / 2, "body_strong", C.ink, "lm")
            y += row
            ui.divider(img, x, xr, y)
        if n > fit:
            ui.text(img, f"+ {n - fit} earlier", xr, y + px(8), "caption", C.ink_2, "rt")

    def _diagnostics(self, img, v):
        ui, px = self.ui, self.m.px
        x1, y1, w, h, pad = self._modal_card(img, px(900), px(620), "info", "Diagnostics", "Staff view · press D to close")
        col_w = (w - pad * 3) / 2
        x = [x1 + pad, x1 + pad * 2 + col_w]
        y = [y1 + pad + px(80)] * 2
        for i, (section, rows) in enumerate(v.diagnostics):
            c = 0 if y[0] <= y[1] else 1
            yy = y[c]
            ui.text(img, section.upper(), x[c], yy, "label", C.forest)
            yy += px(26)
            for k, val in rows:
                ui.text(img, k, x[c], yy, "caption", C.ink_2, max_w=col_w * 0.42)
                ui.text(img, str(val), x[c] + col_w, yy, "caption", C.ink, "rt", max_w=col_w * 0.56)
                yy += px(22)
            y[c] = yy + px(14)

    # ── splash ─────────────────────────────────────────────────────────────
    def splash(self, progress):
        W, H = self.size
        img = np.empty((H, W, 3), np.uint8)
        img[:] = C.canvas
        a = _ease(progress)
        layer = img.copy()
        r = self.m.px(40)
        cy = H / 2 - self.m.px(30)
        tw = self.ts.measure("ECO", "display") + self.ts.measure("Reward", "display")
        x0 = W / 2 - (2 * r + self.m.px(16) + tw) / 2
        D.circle(layer, x0 + r, cy, r, C.forest)
        draw_icon_centered(layer, "leaf", x0 + r, cy, int(r * 1.15), C.white)
        e = self.ts.draw(layer, "ECO", x0 + 2 * r + self.m.px(16), cy, "display", C.forest, "lm")
        self.ts.draw(layer, "Reward", e[2] - 1, cy, "display", C.ink, "lm")
        self.ts.draw(layer, "Smart café recycling", W / 2, cy + r + self.m.px(24), "body_lg", C.ink_2, "mt")
        cv2.addWeighted(layer, a, img, 1 - a, 0, dst=img)
        return img
