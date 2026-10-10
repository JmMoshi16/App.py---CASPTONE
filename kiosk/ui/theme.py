"""Design tokens (DESIGN_SYSTEM.md §3–§7). Colours are stored as RGB hex and converted to OpenCV's BGR once."""
from dataclasses import dataclass


def _bgr(hex_rgb):
    h = hex_rgb.lstrip("#")
    return int(h[4:6], 16), int(h[2:4], 16), int(h[0:2], 16)


class C:
    canvas = _bgr("#F7F3EC")
    surface = _bgr("#FFFFFF")
    sunken = _bgr("#EFE9DF")
    border = _bgr("#E4DCD0")
    ink = _bgr("#211A17")
    ink_2 = _bgr("#6B645E")
    ink_3 = _bgr("#77716B")
    coffee = _bgr("#795548")
    forest = _bgr("#247A52")
    forest_dark = _bgr("#1B5E3F")
    sage = _bgr("#DCE9DF")
    blue = _bgr("#2B6CB0")
    blue_tint = _bgr("#DDE8F4")
    blue_ink = _bgr("#1F5A94")         # text on blue_tint (5.7:1)
    charcoal = _bgr("#4A4440")
    charcoal_tint = _bgr("#E7E3DF")
    amber = _bgr("#D99A26")
    amber_ink = _bgr("#8A5A0B")
    amber_tint = _bgr("#F8ECD3")
    error = _bgr("#C74440")
    error_ink = _bgr("#A8352F")
    error_tint = _bgr("#F6DEDC")
    white = _bgr("#FFFFFF")
    black = _bgr("#000000")


# Physical bin colours (green / blue / black), always paired with an icon and the category word.
CATEGORY = {
    "Biodegradable": {"color": C.forest, "tint": C.sage, "ink": C.forest_dark, "icon": "leaf", "short": "Bio"},
    "Recyclable": {"color": C.blue, "tint": C.blue_tint, "ink": C.blue_ink, "icon": "recycle", "short": "Recycle"},
    "Residual": {"color": C.charcoal, "tint": C.charcoal_tint, "ink": C.charcoal, "icon": "bin", "short": "Residual"},
}
UNKNOWN_CATEGORY = {"color": C.ink_3, "tint": C.sunken, "ink": C.ink_2, "icon": "alert", "short": "Unknown"}


def category(waste_type):
    return CATEGORY.get(waste_type, UNKNOWN_CATEGORY)


# Typography: (family, weight, size in px on the 1280x720 grid, line height)
TYPE = {
    "display": ("Fraunces", 700, 52, 1.10),
    "title": ("Fraunces", 600, 34, 1.15),
    "title_sm": ("Fraunces", 600, 28, 1.15),
    "heading": ("DM Sans", 700, 24, 1.25),
    "body_lg": ("DM Sans", 500, 20, 1.30),
    "body": ("DM Sans", 400, 18, 1.35),
    "body_strong": ("DM Sans", 600, 18, 1.35),
    "label": ("DM Sans", 600, 15, 1.25),
    "caption": ("DM Sans", 400, 15, 1.30),
    "code": ("DM Sans", 700, 44, 1.10),
    "button": ("DM Sans", 600, 19, 1.20),
}
MIN_TEXT_PX = 13

SPACE = {"xxs": 4, "xs": 8, "sm": 12, "md": 16, "lg": 24, "xl": 32, "xxl": 48}
RADIUS = {"chip": 999, "control": 14, "card": 20, "viewport": 28}

MOTION = {"fast": 0.16, "base": 0.24, "slow": 0.42, "count": 0.60, "spin": 1.2, "pulse": 1.6}

BASE_W, BASE_H = 1280, 720


@dataclass(frozen=True)
class Metrics:
    """Layout scale for one display size: every token is multiplied by `s`."""
    width: int
    height: int
    reduce_motion: bool = False

    @property
    def s(self):
        return min(self.width / BASE_W, self.height / BASE_H)

    def px(self, v):
        return int(round(v * self.s))

    def space(self, name):
        return self.px(SPACE[name])

    def radius(self, name):
        return RADIUS[name] if RADIUS[name] == 999 else self.px(RADIUS[name])

    def duration(self, name):
        return 0.0 if self.reduce_motion else MOTION[name]
