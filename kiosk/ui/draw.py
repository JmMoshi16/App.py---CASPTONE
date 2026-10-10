"""Drawing primitives on OpenCV BGR images.

Text is set with Pillow (TrueType, anti-aliased, any Unicode) and cached as alpha masks, so the colour is
applied at draw time and the same string in two colours costs one render. Rounded shapes blend only their
four corners; the straight parts are plain array fills, which keeps a full kiosk frame in a few milliseconds.
"""
from collections import OrderedDict
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .theme import MIN_TEXT_PX, TYPE

FONT_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"
_BRAND_FONTS = {
    ("DM Sans", 400): "DMSans-400.ttf", ("DM Sans", 500): "DMSans-500.ttf",
    ("DM Sans", 600): "DMSans-600.ttf", ("DM Sans", 700): "DMSans-700.ttf",
    ("Fraunces", 600): "Fraunces-600.ttf", ("Fraunces", 700): "Fraunces-700.ttf",
}
_SYSTEM_FONTS = {   # Windows names; Pillow also searches the system font folder
    400: ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"),
    500: ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"),
    600: ("seguisb.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"),
    700: ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"),
}


class LRU(OrderedDict):
    def __init__(self, size):
        super().__init__()
        self.size = size

    def get_or(self, key, make):
        if key in self:
            self.move_to_end(key)
            return self[key]
        value = self[key] = make()
        if len(self) > self.size:
            self.popitem(last=False)
        return value


# ── blending ───────────────────────────────────────────────────────────────────
def _clip(img, x, y, w, h):
    H, W = img.shape[:2]
    x1, y1, x2, y2 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    return (x1, y1, x2, y2) if x2 > x1 and y2 > y1 else None


def blit_mask(img, mask, x, y, color, opacity=1.0):
    """Paint `color` through an 8-bit alpha mask placed with its top-left at (x, y)."""
    h, w = mask.shape[:2]
    box = _clip(img, x, y, w, h)
    if box is None or opacity <= 0:
        return
    x1, y1, x2, y2 = box
    a = mask[y1 - y:y2 - y, x1 - x:x2 - x].astype(np.float32) * (opacity / 255.0)
    roi = img[y1:y2, x1:x2].astype(np.float32)
    roi += (np.asarray(color, np.float32) - roi) * a[..., None]
    img[y1:y2, x1:x2] = (roi + 0.5).astype(np.uint8)


def blend_rect(img, x1, y1, x2, y2, color, opacity):
    box = _clip(img, x1, y1, x2 - x1, y2 - y1)
    if box is None or opacity <= 0:
        return
    x1, y1, x2, y2 = box
    if opacity >= 1:
        img[y1:y2, x1:x2] = color
        return
    roi = img[y1:y2, x1:x2]
    over = np.empty_like(roi)
    over[:] = color
    cv2.addWeighted(over, opacity, roi, 1 - opacity, 0, dst=roi)


def blend_images(dst, src, alpha):
    """dst = src*alpha + dst*(1-alpha), whole image (used for cross-fades)."""
    cv2.addWeighted(src, alpha, dst, 1 - alpha, 0, dst=dst)


# ── rounded shapes ─────────────────────────────────────────────────────────────
_corner_cache = LRU(256)
_mask_cache = LRU(64)


def _quarter(r, t=None):
    """Anti-aliased top-left quarter of a disc (or of a ring of thickness t), r x r."""
    def make():
        ss = 4
        big = np.zeros((r * ss, r * ss), np.uint8)
        cv2.circle(big, (r * ss, r * ss), r * ss, 255, -1)
        if t:
            cv2.circle(big, (r * ss, r * ss), max(0, (r - t) * ss), 0, -1)
        return cv2.resize(big, (r, r), interpolation=cv2.INTER_AREA)
    return _corner_cache.get_or((r, t), make)


def _corners(r, t=None):
    q = _quarter(r, t)
    return {"tl": q, "tr": q[:, ::-1], "bl": q[::-1, :], "br": q[::-1, ::-1]}


def _radius(x1, y1, x2, y2, r):
    return max(0, min(int(r), (x2 - x1) // 2, (y2 - y1) // 2))


def fill_rr(img, x1, y1, x2, y2, r, color, opacity=1.0):
    x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
    r = _radius(x1, y1, x2, y2, r)
    if opacity < 1:
        blit_mask(img, rr_mask(x2 - x1, y2 - y1, r), x1, y1, color, opacity)
        return
    blend_rect(img, x1 + r, y1, x2 - r, y2, color, 1)
    blend_rect(img, x1, y1 + r, x1 + r, y2 - r, color, 1)
    blend_rect(img, x2 - r, y1 + r, x2, y2 - r, color, 1)
    if r:
        c = _corners(r)
        blit_mask(img, c["tl"], x1, y1, color)
        blit_mask(img, c["tr"], x2 - r, y1, color)
        blit_mask(img, c["bl"], x1, y2 - r, color)
        blit_mask(img, c["br"], x2 - r, y2 - r, color)


def stroke_rr(img, x1, y1, x2, y2, r, t, color):
    x1, y1, x2, y2, t = int(x1), int(y1), int(x2), int(y2), max(1, int(t))
    r = max(_radius(x1, y1, x2, y2, r), t)
    blend_rect(img, x1 + r, y1, x2 - r, y1 + t, color, 1)
    blend_rect(img, x1 + r, y2 - t, x2 - r, y2, color, 1)
    blend_rect(img, x1, y1 + r, x1 + t, y2 - r, color, 1)
    blend_rect(img, x2 - t, y1 + r, x2, y2 - r, color, 1)
    c = _corners(r, t)
    blit_mask(img, c["tl"], x1, y1, color)
    blit_mask(img, c["tr"], x2 - r, y1, color)
    blit_mask(img, c["bl"], x1, y2 - r, color)
    blit_mask(img, c["br"], x2 - r, y2 - r, color)


def rr_mask(w, h, r):
    def make():
        m = np.zeros((h, w), np.uint8)
        fill_rr_mask = m[..., None].repeat(3, 2)
        fill_rr(fill_rr_mask, 0, 0, w, h, r, (255, 255, 255))
        return fill_rr_mask[..., 0].copy()
    return _mask_cache.get_or((w, h, r), make)


def shadow(img, x1, y1, x2, y2, r, dy, blur, color, opacity):
    """Soft drop shadow under a rounded rectangle (blurred mask, cached per size)."""
    w, h, pad = int(x2 - x1), int(y2 - y1), int(blur * 2)

    def make():
        m = np.zeros((h + pad * 2, w + pad * 2), np.uint8)
        m[pad:pad + h, pad:pad + w] = rr_mask(w, h, r)
        return cv2.GaussianBlur(m, (0, 0), max(1, blur / 2))
    mask = _mask_cache.get_or(("shadow", w, h, r, blur), make)
    blit_mask(img, mask, int(x1) - pad, int(y1) - pad + int(dy), color, opacity)


def paste_rr(img, src, x, y, r):
    """Paste `src` with rounded corners: straight copy, then blend the four corner squares."""
    h, w = src.shape[:2]
    box = _clip(img, x, y, w, h)
    if box is None:
        return
    if box != (x, y, x + w, y + h):            # partly off-canvas: plain clipped copy
        x1, y1, x2, y2 = box
        img[y1:y2, x1:x2] = src[y1 - y:y2 - y, x1 - x:x2 - x]
        return
    r = _radius(0, 0, w, h, r)
    saved = {k: img[yy:yy + r, xx:xx + r].copy() for k, (xx, yy) in
             {"tl": (x, y), "tr": (x + w - r, y), "bl": (x, y + h - r), "br": (x + w - r, y + h - r)}.items()} if r else {}
    img[y:y + h, x:x + w] = src
    if r:
        for k, m in _corners(r).items():
            xx = x if k[1] == "l" else x + w - r
            yy = y if k[0] == "t" else y + h - r
            a = m.astype(np.float32)[..., None] / 255.0
            img[yy:yy + r, xx:xx + r] = (img[yy:yy + r, xx:xx + r] * a + saved[k] * (1 - a) + 0.5).astype(np.uint8)


def cover(src, w, h):
    """Resize + centre-crop `src` to fill w x h (CSS object-fit: cover)."""
    sh, sw = src.shape[:2]
    scale = max(w / sw, h / sh)
    nw, nh = max(w, int(round(sw * scale))), max(h, int(round(sh * scale)))
    resized = cv2.resize(src, (nw, nh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    ox, oy = (nw - w) // 2, (nh - h) // 2
    return resized[oy:oy + h, ox:ox + w], (scale, ox, oy)


def circle(img, cx, cy, r, color, thickness=-1):
    cv2.circle(img, (int(cx), int(cy)), int(r), color, thickness, cv2.LINE_AA)


def arc(img, cx, cy, r, start_deg, sweep_deg, color, thickness):
    if sweep_deg <= 0:
        return
    cv2.ellipse(img, (int(cx), int(cy)), (int(r), int(r)), 0, start_deg, start_deg + sweep_deg,
                color, int(thickness), cv2.LINE_AA)


def line(img, p1, p2, color, thickness=1):
    cv2.line(img, (int(p1[0]), int(p1[1])), (int(p2[0]), int(p2[1])), color, int(thickness), cv2.LINE_AA)


# ── text ───────────────────────────────────────────────────────────────────────
class Typesetter:
    """TrueType text for one layout scale. `source` says which fonts were found (shown in diagnostics)."""

    def __init__(self, metrics):
        self.m = metrics
        self._fonts = {}
        self._masks = LRU(4000)
        self.source = "brand" if all((FONT_DIR / f).is_file() for f in _BRAND_FONTS.values()) else "system"

    def _load(self, family, weight, px):
        key = (family, weight, px)
        if key in self._fonts:
            return self._fonts[key]
        font = None
        name = _BRAND_FONTS.get((family, weight)) or _BRAND_FONTS.get((family, 700))
        if name and (FONT_DIR / name).is_file():
            font = ImageFont.truetype(str(FONT_DIR / name), px)
        if font is None:
            for candidate in _SYSTEM_FONTS[weight]:
                try:
                    font = ImageFont.truetype(candidate, px)
                    break
                except OSError:
                    continue
        if font is None:
            self.source = "pillow-default"
            font = ImageFont.load_default(size=px)
        self._fonts[key] = font
        return font

    def font(self, role):
        family, weight, size, lh = TYPE[role]
        px = max(MIN_TEXT_PX, self.m.px(size))
        return self._load(family, weight, px), px, lh

    def line_height(self, role):
        _, px, lh = self.font(role)
        return int(round(px * lh))

    def measure(self, text, role):
        return int(np.ceil(self.font(role)[0].getlength(text)))

    def mask(self, text, role):
        def make():
            font, px, _ = self.font(role)
            ascent, descent = font.getmetrics()
            w = max(1, self.measure(text, role) + 2)
            im = Image.new("L", (w, ascent + descent), 0)
            ImageDraw.Draw(im).text((1, ascent), text, font=font, fill=255, anchor="ls")
            return np.asarray(im, np.uint8)
        return self._masks.get_or((text, role, self.m.s), make)

    def fit(self, text, role, max_w):
        if max_w is None or self.measure(text, role) <= max_w:
            return text
        while text and self.measure(text + "…", role) > max_w:
            text = text[:-1]
        return text.rstrip() + "…"

    def wrap(self, text, role, max_w, max_lines=None):
        lines = []
        for para in str(text).split("\n"):
            words, cur = para.split(" "), ""
            for word in words:
                trial = f"{cur} {word}".strip()
                if self.measure(trial, role) <= max_w or not cur:
                    cur = trial
                else:
                    lines.append(cur)
                    cur = word
            lines.append(cur)
        if max_lines and len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = self.fit(lines[-1] + " …", role, max_w)
        return lines

    def draw(self, img, text, x, y, role, color, anchor="lt", max_w=None, opacity=1.0):
        """Draw one line. anchor = horizontal (l/m/r) + vertical (t/m/b) of the line box. Returns its box."""
        text = self.fit(str(text), role, max_w)
        m = self.mask(text, role)
        h, w = m.shape
        x = int(x - (w / 2 if anchor[0] == "m" else w if anchor[0] == "r" else 0))
        y = int(y - (h / 2 if anchor[1] == "m" else h if anchor[1] == "b" else 0))
        blit_mask(img, m, x, y, color, opacity)
        return x, y, x + w, y + h

    def paragraph(self, img, text, x, y, max_w, role, color, max_lines=None, align="l", opacity=1.0):
        """Wrapped text from the top-left (or top-centre with align='m'). Returns the y below the last line."""
        lh = self.line_height(role)
        for ln in self.wrap(text, role, max_w, max_lines):
            self.draw(img, ln, x, y, role, color, anchor=align + "t", opacity=opacity)
            y += lh
        return y
