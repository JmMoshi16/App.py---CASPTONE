"""Kiosk settings: defaults < config/kiosk_settings.json < command line.

Nothing secret lives here (credentials stay in ~/.ecoreward, see README). Business rules (points, expiry)
stay in config/rules.json, shared with the app and the web admin.
"""
import argparse
import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

SORTER_MODES = ("attendant", "esp32", "sim")


@dataclass
class Settings:
    # display
    width: int = 1280
    height: int = 720
    fullscreen: bool = False
    reduce_motion: bool = False
    sounds: bool = True
    # camera
    camera_index: int = 0
    # sorter: "attendant" = staff presses SPACE to confirm a deposit (no hardware),
    #         "esp32"     = servo + load cell over USB serial (protocol: README "ESP32 protocol"),
    #         "sim"       = simulated ESP32 for demos/tests, labelled SIMULATION on screen
    sorter: str = "attendant"
    serial_port: str = "auto"
    serial_baud: int = 115200
    # transaction timing (seconds)
    sort_ack_timeout: float = 5.0
    drop_confirm_timeout: float = 10.0
    issue_timeout: float = 20.0
    qr_display_seconds: float = 180.0
    success_seconds: float = 4.0
    failure_seconds: float = 7.0
    # detection
    lock_votes: int = 5          # of the last 7 frames, needed before the ESP32 sorts automatically
    rearm_clear_frames: int = 10  # empty frames needed before the next automatic sort
    min_drop_grams: float = 5.0
    # telemetry
    bins_min_interval: float = 60.0
    log_dir: str = "logs"
    debug: bool = False


def _coerce(name, value, default):
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        raise ValueError(f"{name} must be true or false")
    if isinstance(default, int) and not isinstance(default, bool):
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
    known = {f.name: f for f in fields(Settings)}
    for key, value in data.items():
        if key.startswith("_"):
            continue
        if key not in known:
            problems.append(f"{path.name}: unknown setting '{key}' ignored")
            continue
        try:
            setattr(s, key, _coerce(key, value, getattr(Settings, key)))
        except (TypeError, ValueError) as e:
            problems.append(f"{path.name}: '{key}' = {value!r} ignored ({e})")
    if s.sorter not in SORTER_MODES:
        problems.append(f"{path.name}: sorter '{s.sorter}' unknown; using 'attendant'")
        s.sorter = "attendant"
    return s, problems


def build_parser():
    p = argparse.ArgumentParser(prog="app.py", description="ECOReward smart bin kiosk")
    p.add_argument("--sorter", choices=SORTER_MODES, help="how deposits are confirmed (default from config)")
    p.add_argument("--no-hardware", action="store_true", help="shortcut for --sorter sim")
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
    p.add_argument("--debug", action="store_true", help="staff tools: SPACE starts a sort in esp32/sim mode")
    return p


def load_settings(config_path, argv=None):
    """Returns (Settings, problems). argv=None means no command-line overrides (used by tests)."""
    s, problems = load_file(config_path)
    if argv is None:
        return s, problems
    a = build_parser().parse_args(argv)
    if a.no_hardware:
        s.sorter = "sim"
    if a.sorter:
        s.sorter = a.sorter
    for attr, value in (("serial_port", a.port), ("serial_baud", a.baud), ("camera_index", a.camera),
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
    return {k: v for k, v in asdict(s).items()}
