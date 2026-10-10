# Getting the ECOReward bin out of "Out of service"

The kiosk shows **Out of service** until an ESP32 on USB serial answers its ping (`P`) with `PONG`. That needs
three things: PySerial on the PC, the ESP32 visible as a COM port, and firmware that speaks the kiosk protocol
(`ecoreward_bin/ecoreward_bin.ino` here). The sketch compiles for "ESP32 Dev Module" (ESP32 core 3.3.12 +
ESP32Servo) but has **not been tested on the bin**: every pin, angle and calibration value is an assumption.

## 1. Check the wiring against the sketch

| Part | ESP32 pin in the sketch | Notes |
|---|---|---|
| MG996R chute servo — signal | GPIO 19 (`PIN_CHUTE_SERVO`) | power from a separate 5–6 V supply (≥ 2 A), **not** the ESP32 |
| Tray servo — signal | GPIO 18 (`PIN_TRAY_SERVO`) | the pin your `microservo.ino` test used |
| HX711 DT / SCK | GPIO 16 / GPIO 4 | HX711 VCC to 3.3 V, GND to GND |
| HC-SR04 bin 1 / 2 / 3 — TRIG | GPIO 25 / 26 / 27 | sensors on 5 V |
| HC-SR04 bin 1 / 2 / 3 — ECHO | GPIO 34 / 35 / 32 | **through a divider** (e.g. 1 kΩ from ECHO, 2 kΩ to GND) so the pin sees ≤ 3.3 V |
| Grounds | — | servo supply GND, sensor GND and ESP32 GND tied together |

Different wiring? Change the constants in section 1 of the sketch; nothing else needs to change.

## 2. Connect the ESP32 and find its COM port
1. Use a USB **data** cable (charge-only cables show no port).
2. Device Manager → **Ports (COM & LPT)** → note e.g. `Silicon Labs CP210x (COM5)` or `USB-SERIAL CH340 (COM5)`.
3. Nothing there, or an "Unknown device" with a warning sign: install the driver for the USB chip printed next to
   the board's USB socket (CP2102 → Silicon Labs CP210x driver; CH340 → WCH CH340 driver), then replug.

## 3. Upload the firmware (Arduino IDE)
1. **Close the kiosk** — only one program can use the COM port.
2. File → Open → `firmware/ecoreward_bin/ecoreward_bin.ino`.
3. Tools → Board → esp32 → **ESP32 Dev Module**; Tools → Port → your COM port.
4. Upload. "Failed to connect": hold the board's **BOOT** button when "Connecting…" appears.

## 4. Test it in the Serial Monitor (115200 baud, line ending "Newline")
- After a reset you see `READY`, a `FILL:` line, then `W:0.0` about five times a second.
- Type `P` → `PONG`. Type `T` with the tray empty → the weight goes to about 0.

## 5. Calibrate the scale
1. Tray empty, type `T`.
2. Put an object of known weight on the tray (weigh it on a kitchen scale first, e.g. 100 g).
3. Type `CAL 100` (your weight in grams) → it prints `CAL factor=…`.
4. Put that number in `float calibration = …;`, upload again, check that `W:` reads the object's weight ± 1 g.

## 6. Tune the servos (tray empty, hands clear)
- Type `1`, `2`, `3`: the chute turns to each bin, the tray tips and comes back, and you get `OK:n`, `DONE:n:…`, `FILL:…`.
- Adjust `CHUTE_ANGLE`, `CHUTE_HOME`, `TRAY_LEVEL`, `TRAY_TIP` until the chute lines up with each bin and the tray
  tips far enough; upload again after each change.
- Jam test: hold an item on the tray while it tips → `JAM:n` after 3 s and the tray levels again.
- Confirm the mapping **1 = Biodegradable, 2 = Recyclable, 3 = Residual** matches the physical bins; if not, either
  move the angles or change `bin_codes` in `config/rules.json`.

## 7. Fill sensors
Measure from each sensor to the bottom of its empty bin (`BIN_EMPTY_CM`) and to the level you call "full"
(`BIN_FULL_CM`). An empty bin should give `FILL:0`-ish, a full one `100`.

## 8. Run the kiosk
1. **Close the Serial Monitor.**
2. In `config/kiosk_settings.json` set `"serial_port"` to your COM port (or `"auto"`).
3. `py -3.11 app.py` → after a few seconds: "Drop one item into the slot".
4. Press **D**: live weight, steady = yes on an empty tray, the serial lines (`> P`, `< PONG`), fill levels, and the
   camera preview — move `tray_roi` until the amber box frames the tray.
5. Drop one empty can: Checking → Sorting into Recyclable → (online) the QR.

## What the kiosk screen tells you

| Screen (right panel, "For staff") | Cause | Fix |
|---|---|---|
| The serial library is missing | PySerial not installed for Python 3.11 | `py -3.11 -m pip install pyserial`, restart the kiosk |
| Cannot open COM3 | wrong port, cable, driver, or another program has the port | steps 2 and 8.2; close Serial Monitor / Arduino IDE |
| Connected to COMx, but the ESP32 does not answer | firmware not uploaded, or it does not answer `P` | step 3; check in the Serial Monitor that `P` → `PONG` |
| Item stuck. Please call staff | `JAM:n`, or no `OK`/`DONE` in time | clear the tray, press **C**; tune angles / timing |
| A bin is full | a sensor reads ≥ 100 % | empty the bin; check `BIN_EMPTY_CM` / `BIN_FULL_CM` |
