"""Line icons drawn in code on a 24-unit grid (2-unit strokes, round joins), supersampled 4x and cached.

Each icon is an 8-bit alpha mask, so it takes any colour: draw_icon(img, "leaf", x, y, 32, C.forest).
"""
import math

import cv2
import numpy as np

from .draw import LRU, blit_mask

_SS = 4
_cache = LRU(256)


def _bezier(p0, p1, p2, n=24):
    return [((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
             (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1]) for t in (i / n for i in range(n + 1))]


class _Pen:
    def __init__(self, size):
        self.k = size * _SS / 24.0
        self.m = np.zeros((size * _SS, size * _SS), np.uint8)
        self.w = max(1, int(round(2.0 * self.k)))

    def pts(self, points):
        return np.array([[int(round(x * self.k)), int(round(y * self.k))] for x, y in points], np.int32)

    def poly(self, points, closed=False, width=None):
        cv2.polylines(self.m, [self.pts(points)], closed, 255, width or self.w, cv2.LINE_AA)
        for x, y in (points if not closed else []):          # round caps
            cv2.circle(self.m, tuple(self.pts([(x, y)])[0]), (width or self.w) // 2, 255, -1, cv2.LINE_AA)

    def fill(self, points, value=255):
        cv2.fillPoly(self.m, [self.pts(points)], value, cv2.LINE_AA)

    def circle(self, x, y, r, filled=False, value=255, width=None):
        c = tuple(self.pts([(x, y)])[0])
        cv2.circle(self.m, c, int(round(r * self.k)), value, -1 if filled else (width or self.w), cv2.LINE_AA)

    def arc(self, x, y, r, a0, a1, width=None):
        c = tuple(self.pts([(x, y)])[0])
        rr = int(round(r * self.k))
        cv2.ellipse(self.m, c, (rr, rr), 0, a0, a1, 255, width or self.w, cv2.LINE_AA)

    def arrowhead(self, x, y, angle_deg, size=3.2):
        a = math.radians(angle_deg)
        tip = (x + math.cos(a) * size * 0.6, y + math.sin(a) * size * 0.6)
        l = (x + math.cos(a + 2.4) * size, y + math.sin(a + 2.4) * size)
        r = (x + math.cos(a - 2.4) * size, y + math.sin(a - 2.4) * size)
        self.fill([tip, l, r])


def _leaf(p):
    outline = _bezier((19.5, 4.5), (19.5, 16.5), (6, 18.5)) + _bezier((6, 18.5), (6.5, 5), (19.5, 4.5))
    p.fill(outline)
    p.poly([(7.5, 17), (15.5, 9)], width=max(1, int(p.w * 0.7)))
    cv2.polylines(p.m, [p.pts([(8.5, 16), (15.5, 9)])], False, 0, max(1, int(p.w * 0.6)), cv2.LINE_AA)
    p.poly([(4, 20.5), (7.5, 17)])


def _recycle(p):
    p.arc(12, 12, 7.5, 200, 330)
    p.arrowhead(12 + 7.5 * math.cos(math.radians(330)), 12 + 7.5 * math.sin(math.radians(330)), 330 + 90)
    p.arc(12, 12, 7.5, 20, 150)
    p.arrowhead(12 + 7.5 * math.cos(math.radians(150)), 12 + 7.5 * math.sin(math.radians(150)), 150 + 90)


def _bin(p):
    p.poly([(4, 6.5), (20, 6.5)])
    p.poly([(9, 6.5), (9.5, 3.8), (14.5, 3.8), (15, 6.5)])
    p.poly([(6, 6.5), (7.2, 20), (16.8, 20), (18, 6.5)])
    p.poly([(10, 10.5), (10.3, 16.5)])
    p.poly([(14, 10.5), (13.7, 16.5)])


def _check(p):
    p.poly([(5, 12.5), (10, 17.5), (19.5, 7)])


def _cross(p):
    p.poly([(6.5, 6.5), (17.5, 17.5)])
    p.poly([(17.5, 6.5), (6.5, 17.5)])


def _alert(p):
    p.poly([(12, 3.5), (21, 19.5), (3, 19.5)], closed=True)
    p.poly([(12, 9.5), (12, 13.8)])
    p.circle(12, 16.8, 1.3, filled=True)


def _info(p):
    p.circle(12, 12, 9)
    p.poly([(12, 11), (12, 16.5)])
    p.circle(12, 7.8, 1.3, filled=True)


def _wifi(p, off=False):
    for r in (4.5, 9, 13.5):
        p.arc(12, 19.5, r, 228, 312)
    p.circle(12, 19.5, 1.5, filled=True)
    if off:
        p.poly([(4, 3.5), (20, 21)])


def _plug(p):
    p.poly([(9, 3), (9, 7)])
    p.poly([(15, 3), (15, 7)])
    p.poly([(6, 7), (18, 7), (18, 10), (15, 14), (9, 14), (6, 10)], closed=True)
    p.poly([(12, 14), (12, 21)])


def _camera(p):
    p.poly([(3, 8), (8, 8), (9.8, 5), (14.2, 5), (16, 8), (21, 8), (21, 19), (3, 19)], closed=True)
    p.circle(12, 13.3, 3.6)


def _clock(p):
    p.circle(12, 12, 9)
    p.poly([(12, 7), (12, 12), (15.5, 14)])


def _qr(p):
    for x, y in ((3.5, 3.5), (14, 3.5), (3.5, 14)):
        p.poly([(x, y), (x + 6.5, y), (x + 6.5, y + 6.5), (x, y + 6.5)], closed=True)
        p.fill([(x + 2.3, y + 2.3), (x + 4.2, y + 2.3), (x + 4.2, y + 4.2), (x + 2.3, y + 4.2)])
    for x, y in ((14.5, 14.5), (18.5, 14.5), (14.5, 18.5), (18.5, 18.5), (16.5, 16.5)):
        p.fill([(x - 1, y - 1), (x + 1, y - 1), (x + 1, y + 1), (x - 1, y + 1)])


def _cup(p):
    p.poly([(4, 8), (18, 8)])
    p.poly([(5.5, 8), (7, 20.5), (15, 20.5), (16.5, 8)])
    p.arc(17.2, 13, 3, -80, 80)
    for x in (8.5, 12.5):
        p.poly(_bezier((x, 5.5), (x + 1.5, 4), (x, 2.5), 8))


def _receipt(p):
    zig = [(18, 21), (16, 19.5), (14, 21), (12, 19.5), (10, 21), (8, 19.5), (6, 21)]
    p.poly([(6, 3), (18, 3)] + zig, closed=True)
    p.poly([(9, 8), (15, 8)])
    p.poly([(9, 12), (15, 12)])
    p.poly([(9, 16), (12.5, 16)])


def _list(p):
    for y in (6, 12, 18):
        p.circle(5, y, 1.4, filled=True)
        p.poly([(9, y), (20, y)])


def _phone(p):
    p.poly([(7, 2.5), (17, 2.5), (17, 21.5), (7, 21.5)], closed=True)
    p.poly([(10.5, 18.5), (13.5, 18.5)])


def _arrow_down(p):
    p.poly([(12, 3.5), (12, 18)])
    p.poly([(6.5, 13), (12, 18.5), (17.5, 13)])


def _scale(p):
    p.poly([(3, 20), (21, 20)])
    p.poly([(6, 20), (7.5, 11), (16.5, 11), (18, 20)])
    p.poly([(9, 11), (9, 7), (15, 7), (15, 11)])


def _sparkle(p):
    p.fill([(12, 2.5), (14, 10), (21.5, 12), (14, 14), (12, 21.5), (10, 14), (2.5, 12), (10, 10)])


ICONS = {
    "leaf": _leaf, "recycle": _recycle, "bin": _bin, "check": _check, "cross": _cross, "alert": _alert,
    "info": _info, "wifi": _wifi, "wifi_off": lambda p: _wifi(p, off=True), "plug": _plug, "camera": _camera,
    "clock": _clock, "qr": _qr, "cup": _cup, "receipt": _receipt, "list": _list, "phone": _phone,
    "arrow_down": _arrow_down, "scale": _scale, "sparkle": _sparkle,
}


def icon_mask(name, size):
    size = max(8, int(size))

    def make():
        pen = _Pen(size)
        ICONS[name](pen)
        return cv2.resize(pen.m, (size, size), interpolation=cv2.INTER_AREA)
    return _cache.get_or((name, size), make)


def draw_icon(img, name, x, y, size, color, opacity=1.0):
    """Top-left at (x, y)."""
    blit_mask(img, icon_mask(name, size), int(x), int(y), color, opacity)


def draw_icon_centered(img, name, cx, cy, size, color, opacity=1.0):
    size = max(8, int(size))
    draw_icon(img, name, int(cx - size / 2), int(cy - size / 2), size, color, opacity)
