#!/usr/bin/env python3
"""Kiosk performance benchmark (audit Phase 9). No camera, no Firebase: times the real model and the
real drawing code on a synthetic 640x480 frame.

    py -3.11 tools/bench_kiosk.py [--frames 60]

Prints median / p95 milliseconds for YOLO inference at the settings app.py uses, the HUD drawing, and
the resulting UI frame rate when inference runs on every frame vs. on a worker thread.
"""
import argparse
import statistics
import sys
import time
from pathlib import Path

import numpy as np

APP_DIR = Path(__file__).resolve().parents[1]


def timeit(fn, n):
    samples = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t) * 1000)
    samples.sort()
    return statistics.median(samples), samples[int(len(samples) * 0.95) - 1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=60)
    ap.add_argument("--model", default=str(APP_DIR / "best.pt"))
    args = ap.parse_args()

    import cv2
    from ultralytics import YOLO
    rng = np.random.default_rng(0)
    frame = rng.integers(0, 255, (480, 640, 3), dtype=np.uint8)
    cv2.rectangle(frame, (220, 140), (420, 360), (40, 160, 220), -1)   # a "can"-sized blob

    model = YOLO(args.model)
    model(frame, conf=0.15, verbose=False)                              # warm-up
    results = {}
    for label, kw in [("YOLO native 640, conf=0.15 (current)", {"conf": 0.15}),
                      ("YOLO native 640, conf=0.30", {"conf": 0.30}),
                      ("YOLO imgsz=480, conf=0.30", {"conf": 0.30, "imgsz": 480}),
                      ("YOLO imgsz=320, conf=0.30", {"conf": 0.30, "imgsz": 320})]:
        results[label] = timeit(lambda: model(frame, verbose=False, **kw), args.frames)

    # HUD drawing as main() does it every frame (resize + camera HUD), app imported without Firebase.
    sys.path.insert(0, str(APP_DIR))
    import importlib
    import os
    os.environ.setdefault("ECOREWARD_KIOSK_CONFIG", str(APP_DIR / "no-kiosk-config.json"))
    os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", str(APP_DIR / "no-key.json"))
    app = importlib.import_module("app")
    def hud():   # the full 1280x720 kiosk frame: camera viewport + "item detected" panel
        app.draw_camera_hud(frame, "can", "Recyclable", 0.92, True, 3)
    hud()        # warm the text/icon caches once, like the first frame of a session
    results["HUD resize + draw (per UI frame)"] = timeit(hud, args.frames)

    print(f"{'step':40} {'median ms':>10} {'p95 ms':>8}")
    for k, (med, p95) in results.items():
        print(f"{k:40} {med:10.1f} {p95:8.1f}")
    infer = results["YOLO native 640, conf=0.15 (current)"][0]
    hud_ms = results["HUD resize + draw (per UI frame)"][0]
    print(f"\nUI FPS, inference on every frame (current): {1000 / (infer + hud_ms):5.1f}")
    print(f"UI FPS, inference on a worker thread:       {1000 / hud_ms:5.1f}  (detections update at {1000 / infer:4.1f}/s)")


if __name__ == "__main__":
    main()
