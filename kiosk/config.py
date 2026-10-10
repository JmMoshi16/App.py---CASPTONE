"""Kiosk settings: defaults < config/kiosk_settings.json < command line.

Nothing secret lives here (credentials stay in ~/.ecoreward, see README). Business rules (points, expiry, bin
codes, weight limits, bin thresholds) stay in config/rules.json. Every key has a default, so an older settings
file still works; the older names "sorter" and "serial_baud" are still read.
"""
import argparse
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

MODES = ("esp32", "sim", "camera", "attendant")
SORTER_MODES = MODES                     # older name
LEGACY_KEYS = {"sorter": "mode", "serial_baud": "baud"}


@dataclass
class Settings:
    # mode: "esp32"     = the bin: load cell triggers, YOLO on the tray, ESP32 sorts, DONE proves the deposit
    #       "sim"       = the same flow with a simulated ESP32 (labelled SIMULATION; staff key I places an item)
    #       "camera"    = no hardware: the camera sees the item leave (testing)
    #       "attendant" = no hardware: staff press SPACE (testing)
    mode: str = "esp32"
    serial_port: str = "COM3"
    baud: int = 115200
    # tray (esp32 / sim)
    min_item_g: float = 3.0
    steady_ms: float = 500
    steady_tolerance_g: float = 1.0
    identify_timeout_s: float = 3.0
    ok_timeout_s: float = 3.0
    drop_timeout_s: float = 4.0
    tray_roi: list = field(default_factory=lambda: [0.0, 0.0, 1.0, 1.0])   # x0, y0, x1, y1 as fractions
    multi_item_frames: int = 3
    jam_clear_s: float = 10.0
    auto_tare_idle_s: float = 60.0
    # display
    width: int = 1280
    height: int = 720
    fullscreen: bool = False
    reduce_motion: bool = False
    sounds: bool = True
    camera_index: int = 0
    # reward screens
    issue_timeout: float = 20.0
    qr_display_seconds: float = 180.0
    success_seconds: float = 4.0
    failure_seconds: float = 7.0
    # camera / attendant test modes
    lock_votes: int = 5
    rearm_clear_frames: int = 10
    drop_clear_frames: int = 8
    drop_timeout: float = 15.0
    cooldown_seconds: float = 15.0
    # telemetry
    bins_min_interval: float = 60.0
    log_dir: str = "logs"
    debug: bool = False

    @property
    def sorter(self):                    # older name used across the code
        return self.mode

    @property
    def serial_baud(self):
        return self.baud

    @property
    def tray(self):
        return self.mode in ("esp32", "sim")


def _coerce(name, value, default):
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        raise ValueError(f"{name} must be true or false")
    if isinstance(default, list):
        roi = [float(v) for v in value]
        if len(roi) != 4 or not (0 <= roi[0] < roi[2] <= 1 and 0 <= roi[1] < roi[3] <= 1):
            raise ValueError("tray_roi must be [x0, y0, x1, y1] fractions with x0 < x1 and y0 < y1")
        return roi
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return str(value)


def load_file(path):
    """Settings from a JSON file; unknown keys and bad values are reported, never fatal."""
    s, problems = Settings(), []
    path = Path(path)
    if not path.is_file():
        return s, problems
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        return s, [f"{path.name} is not valid JSON ({e}); using defaults"]
    for old, new in LEGACY_KEYS.items():                 # older files: "sorter", "serial_baud"
        if old in data and new not in data:
            data[new] = data[old]
        data.pop(old, None)
    defaults = Settings()
    known = {f.name for f in fields(Settings)}
    for key, value in data.items():
        if key.startswith("_"):
            continue
        if key not in known:
            problems.append(f"{path.name}: unknown setting '{key}' ignored")
            continue
        try:
            setattr(s, key, _coerce(key, value, getattr(defaults, key)))
        except (TypeError, ValueError) as e:
            problems.append(f"{path.name}: '{key}' = {value!r} ignored ({e})")
    if s.mode not in MODES:
        problems.append(f"{path.name}: mode '{s.mode}' unknown; using 'esp32'")
        s.mode = "esp32"
    return s, problems


def build_parser():
    p = argparse.ArgumentParser(prog="app.py", description="ECOReward smart bin kiosk")
    p.add_argument("--mode", "--sorter", dest="mode", choices=MODES, help="how deposits work (default from config)")
    p.add_argument("--no-hardware", action="store_true", help="shortcut for --mode sim")
    p.add_argument("--port", help="ESP32 serial port, e.g. COM3, or 'auto'")
    p.add_argument("--baud", type=int, help="ESP32 baud rate")
    p.add_argument("--camera", type=int, help="camera index")
    p.add_argument("--width", type=int, help="canvas width in pixels")
    p.add_argument("--height", type=int, help="canvas height in pixels")
    screen = p.add_mutually_exclusive_group()
    screen.add_argument("--fullscreen", action="store_true", help="fill the screen (canvas = screen size)")
    screen.add_argument("--windowed", action="store_true", help="use a window even if the config says fullscreen")
    p.add_argument("--reduce-motion", action="store_true", help="turn off animations")
    p.add_argument("--no-sound", action="store_true", help="turn off beeps")
    p.add_argument("--debug", action="store_true", help="staff tools: SPACE starts a deposit in camera mode")
    return p


def load_settings(config_path, argv=None):
    """Returns (Settings, problems). argv=None means no command-line overrides (used by tests)."""
    s, problems = load_file(config_path)
    if argv is None:
        return s, problems
    a = build_parser().parse_args(argv)
    if a.no_hardware:
        s.mode = "sim"
    if a.mode:
        s.mode = a.mode
    for attr, value in (("serial_port", a.port), ("baud", a.baud), ("camera_index", a.camera),
                        ("width", a.width), ("height", a.height)):
        if value is not None:
            setattr(s, attr, value)
    if a.fullscreen:
        s.fullscreen = True
    if a.windowed:
        s.fullscreen = False
    s.reduce_motion |= a.reduce_motion
    s.sounds &= not a.no_sound
    s.debug |= a.debug
    return s, problems


def describe(s):
    return asdict(s)
