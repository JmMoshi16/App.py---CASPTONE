"""Reusable components (DESIGN_SYSTEM.md §6). Each draws onto `img` and returns what the caller needs
(usually the bottom y or the hit box). Sizes are design units scaled by the Metrics `m`."""
import math

from . import draw as D
from .icons import draw_icon, draw_icon_centered
from .theme import C, category

CHIP_STYLES = {   # background, foreground, outline
    "ok": (C.sage, C.forest_dark, None),       # forest on sage is only 4.2:1
    "busy": (C.amber_tint, C.amber_ink, None),
    "error": (C.error_tint, C.error_ink, None),
    "off": (C.sunken, C.ink_2, None),
    "sim": (C.amber_tint, C.amber_ink, C.amber),
    "info": (C.surface, C.ink, C.border),
}


class UI:
    """Bundles the scale, the typesetter and the clock so components stay one-liners."""

    def __init__(self, m, ts):
        self.m, self.t = m, ts
        self.now = 0.0
        self.buttons = []
        self.pressed = None          # (action, time) of the last tap, for pressed feedback

    def px(self, v):
        return self.m.px(v)

    # ── surfaces ───────────────────────────────────────────────────────────
    def card(self, img, x1, y1, x2, y2, radius=20, fill=C.surface, border=True, elevation="card"):
        r = self.px(radius)
        if elevation == "card":
            D.shadow(img, x1, y1, x2, y2, r, self.px(6), self.px(18), C.ink, 0.10)
        elif elevation == "modal":
            D.shadow(img, x1, y1, x2, y2, r, self.px(14), self.px(36), C.ink, 0.22)
        D.fill_rr(img, x1, y1, x2, y2, r, fill)
        if border:
            D.stroke_rr(img, x1, y1, x2, y2, r, 1, C.border)

    def divider(self, img, x1, x2, y):
        D.blend_rect(img, int(x1), int(y), int(x2), int(y) + 1, C.border, 1)

    # ── text helpers ───────────────────────────────────────────────────────
    def text(self, img, s, x, y, role, color, anchor="lt", max_w=None, opacity=1.0):
        return self.t.draw(img, s, x, y, role, color, anchor, max_w, opacity)

    def para(self, img, s, x, y, w, role, color, max_lines=None, align="l", opacity=1.0):
        return self.t.paragraph(img, s, x, y, w, role, color, max_lines, align, opacity)

    def overline(self, img, s, x, y, color=C.forest):
        return self.text(img, s.upper(), x, y, "label", color)[3]

    # ── chips and badges ───────────────────────────────────────────────────
    def chip(self, img, x_right, y_mid, label, style="ok", icon=None, pulse=False):
        """Right-aligned status chip; returns its left x. A dot (or icon) plus a word — never colour alone."""
        bg, fg, outline = CHIP_STYLES[style]
        h, pad, gap = self.px(32), self.px(12), self.px(8)
        tw = self.t.measure(label, "label")
        mark = self.px(16) if icon else self.px(8)
        w = pad + mark + gap + tw + pad
        x1, y1 = int(x_right - w), int(y_mid - h / 2)
        D.fill_rr(img, x1, y1, x1 + w, y1 + h, 999, bg)
        if outline:
            D.stroke_rr(img, x1, y1, x1 + w, y1 + h, 999, max(1, self.px(1.5)), outline)
        cx = x1 + pad + mark / 2
        if icon:
            draw_icon_centered(img, icon, cx, y_mid, mark, fg)
        else:
            a = 1.0
            if pulse and not self.m.reduce_motion:
                a = 0.45 + 0.55 * (0.5 + 0.5 * math.cos(self.now * 2 * math.pi / 1.6))
            D.circle(img, cx, y_mid, self.px(4), fg)
            if a < 1:
                D.circle(img, cx, y_mid, self.px(4), bg)
                D.blit_mask(img, _dot_mask(self.px(4)), int(cx - self.px(4) - 1), int(y_mid - self.px(4) - 1), fg, a)
        self.text(img, label, x1 + pad + mark + gap, y_mid, "label", fg, "lm")
        return x1

    def category_pill(self, img, x, y, waste_type, suffix=None):
        """Tinted pill: icon + category word (+ suffix). Returns (x2, y2)."""
        cat = category(waste_type)
        label = (waste_type or "Not recognised") + (f"  ·  {suffix}" if suffix else "")
        h, pad, ic, gap = self.px(36), self.px(14), self.px(20), self.px(8)
        w = pad + ic + gap + self.t.measure(label, "body_strong") + pad
        D.fill_rr(img, x, y, x + w, y + h, 999, cat["tint"])
        draw_icon_centered(img, cat["icon"], x + pad + ic / 2, y + h / 2, ic, cat["ink"])
        self.text(img, label, x + pad + ic + gap, y + h / 2, "body_strong", cat["ink"], "lm")
        return x + w, y + h

    def icon_tile(self, img, x, y, size, icon, fg, bg, radius=None):
        r = self.px(radius) if radius is not None else int(size * 0.28)
        D.fill_rr(img, x, y, x + size, y + size, r, bg)
        draw_icon_centered(img, icon, x + size / 2, y + size / 2, int(size * 0.56), fg)

    # ── controls ───────────────────────────────────────────────────────────
    def button(self, img, x1, y1, x2, y2, label, action, kind="primary", icon=None, hint=None):
        """Touch target ≥ 56 px. Registers the hit box. kind: primary | secondary | ghost."""
        pressed = self.pressed and self.pressed[0] == action and self.now - self.pressed[1] < 0.16
        r = self.px(14)
        if kind == "primary":
            D.fill_rr(img, x1, y1, x2, y2, r, C.forest_dark if pressed else C.forest)
            fg = C.white
        elif kind == "secondary":
            D.fill_rr(img, x1, y1, x2, y2, r, C.sunken if pressed else C.surface)
            D.stroke_rr(img, x1, y1, x2, y2, r, max(1, self.px(1.5)), C.border)
            fg = C.ink
        else:
            if pressed:
                D.fill_rr(img, x1, y1, x2, y2, r, C.sunken)
            fg = C.forest
        label_w = self.t.measure(label, "button")
        ic = self.px(22) if icon else 0
        gap = self.px(10) if icon else 0
        total = ic + gap + label_w
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        lx = cx - total / 2
        if icon:
            draw_icon_centered(img, icon, lx + ic / 2, cy, ic, fg)
        self.text(img, label, lx + ic + gap, cy, "button", fg, "lm", max_w=(x2 - x1) - self.px(24) - ic - gap)
        if hint:
            self.text(img, hint, x2 - self.px(12), y2 - self.px(6), "caption", C.ink_3 if kind != "primary" else C.sage, "rb")
        self.buttons.append((int(x1), int(y1), int(x2), int(y2), action))

    def icon_button(self, img, cx, cy, icon, action, size=56):
        s = self.px(size)
        x1, y1 = int(cx - s / 2), int(cy - s / 2)
        pressed = self.pressed and self.pressed[0] == action and self.now - self.pressed[1] < 0.16
        D.fill_rr(img, x1, y1, x1 + s, y1 + s, 999, C.sunken if pressed else C.surface)
        D.stroke_rr(img, x1, y1, x1 + s, y1 + s, 999, 1, C.border)
        draw_icon_centered(img, icon, cx, cy, int(s * 0.46), C.ink)
        self.buttons.append((x1, y1, x1 + s, y1 + s, action))

    # ── progress ───────────────────────────────────────────────────────────
    def meter(self, img, x1, y, x2, frac, color, height=8):
        h = self.px(height)
        D.fill_rr(img, x1, y, x2, y + h, 999, C.sunken)
        w = int((x2 - x1) * max(0.0, min(1.0, frac)))
        if w >= h:
            D.fill_rr(img, x1, y, x1 + w, y + h, 999, color)
        elif w > 0:
            D.fill_rr(img, x1, y, x1 + h, y + h, 999, color, opacity=w / h)
        return y + h

    def spinner(self, img, cx, cy, r, color, thickness=3):
        t = self.px(thickness)
        if self.m.reduce_motion:
            D.arc(img, cx, cy, r, -90, 270, color, t)
            return
        start = (self.now * 300) % 360
        D.arc(img, cx, cy, r, -90, 360, C.sunken, t)
        D.arc(img, cx, cy, r, start, 100, color, t)

    def ring(self, img, cx, cy, r, frac, color, thickness=6):
        t = self.px(thickness)
        D.arc(img, cx, cy, r, -90, 360, C.sunken, t)
        D.arc(img, cx, cy, r, -90, 360 * max(0.0, min(1.0, frac)), color, t)

    def stepper(self, img, x, y, w, stages, compact=False):
        """Vertical stage list: done ✓ / active (spinner) / pending ○ / failed ✕. Returns bottom y."""
        row = self.px(40 if compact else 52)
        r = self.px(11 if compact else 13)
        role = "body" if compact else "body_lg"
        for i, (_, label, status) in enumerate(stages):
            cy = y + row / 2
            cx = x + r + 1
            if i < len(stages) - 1:                      # connector to the next row
                done = status == "done"
                D.blend_rect(img, int(cx - self.px(1)), int(cy + r + self.px(3)), int(cx + self.px(1)),
                             int(cy + row - r - self.px(3)), C.forest if done else C.border, 1)
            if status == "done":
                D.circle(img, cx, cy, r, C.forest)
                draw_icon_centered(img, "check", cx, cy, int(r * 1.3), C.white)
                color = C.ink_2
            elif status == "active":
                D.circle(img, cx, cy, r, C.surface)
                self.spinner(img, cx, cy, r, C.forest, 3)
                color = C.ink
            elif status == "failed":
                D.circle(img, cx, cy, r, C.error)
                draw_icon_centered(img, "cross", cx, cy, int(r * 1.2), C.white)
                color = C.error_ink
            else:
                D.circle(img, cx, cy, r, C.border, max(1, self.px(2)))
                color = C.ink_3
            weight_role = ("body_strong" if compact else "heading") if status in ("active", "failed") else role
            self.text(img, label, x + 2 * r + self.px(14), cy, weight_role, color, "lm", max_w=w - 2 * r - self.px(14))
            y += row
        return y

    # ── messages ───────────────────────────────────────────────────────────
    def banner(self, img, x1, y1, x2, y2, text, kind):
        bg, fg, icon = {"warning": (C.amber, C.ink, "wifi_off"), "error": (C.error, C.white, "alert"),
                        "info": (C.sage, C.ink, "info")}[kind]
        D.blend_rect(img, x1, y1, x2, y2, bg, 1)
        ic = self.px(20)
        tw = self.t.measure(text, "body_strong")
        cx = (x1 + x2) / 2 - (ic + self.px(10) + tw) / 2
        draw_icon_centered(img, icon, cx + ic / 2, (y1 + y2) / 2, ic, fg)
        self.text(img, text, cx + ic + self.px(10), (y1 + y2) / 2, "body_strong", fg, "lm", max_w=x2 - x1 - ic - self.px(40))

    def note(self, img, x1, y, x2, title, body, kind="info", icon=None):
        """Inline callout inside a card. Returns bottom y."""
        bg, fg = {"info": (C.sunken, C.ink), "warning": (C.amber_tint, C.amber_ink),
                  "error": (C.error_tint, C.error_ink), "ok": (C.sage, C.forest)}[kind]
        pad, ic = self.px(16), self.px(24)
        tx = x1 + pad + ic + self.px(12)
        tw = x2 - tx - pad
        lines_title = self.t.wrap(title, "body_strong", tw) if title else []
        lines_body = self.t.wrap(body, "body", tw) if body else []
        h = pad * 2 + len(lines_title) * self.t.line_height("body_strong") + len(lines_body) * self.t.line_height("body")
        D.fill_rr(img, x1, y, x2, y + h, self.px(14), bg)
        draw_icon(img, icon or {"info": "info", "warning": "alert", "error": "alert", "ok": "check"}[kind],
                  x1 + pad, y + pad, ic, fg)
        yy = y + pad
        for ln in lines_title:
            self.text(img, ln, tx, yy, "body_strong", fg)
            yy += self.t.line_height("body_strong")
        for ln in lines_body:
            self.text(img, ln, tx, yy, "body", C.ink if kind != "info" else C.ink_2)
            yy += self.t.line_height("body")
        return y + h

    def toast(self, img, cx, y_bottom, text, kind, alpha=1.0):
        bg, fg, icon = {"info": (C.ink, C.white, "info"), "warning": (C.amber, C.ink, "alert"),
                        "error": (C.error, C.white, "alert")}[kind]
        h, pad, ic = self.px(48), self.px(20), self.px(22)
        w = pad + ic + self.px(10) + self.t.measure(text, "body_strong") + pad
        x1, y1 = int(cx - w / 2), int(y_bottom - h)
        D.shadow(img, x1, y1, x1 + w, y1 + h, 999, self.px(6), self.px(16), C.ink, 0.18 * alpha)
        D.fill_rr(img, x1, y1, x1 + w, y1 + h, 999, bg, opacity=alpha)
        draw_icon_centered(img, icon, x1 + pad + ic / 2, y1 + h / 2, ic, fg, alpha)
        self.text(img, text, x1 + pad + ic + self.px(10), y1 + h / 2, "body_strong", fg, "lm", opacity=alpha)


_dot_cache = D.LRU(16)


def _dot_mask(r):
    import numpy as np
    import cv2

    def make():
        m = np.zeros((2 * r + 3, 2 * r + 3), np.uint8)
        cv2.circle(m, (r + 1, r + 1), r, 255, -1, cv2.LINE_AA)
        return m
    return _dot_cache.get_or(r, make)
