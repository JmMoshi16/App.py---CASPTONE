# ESP32 firmware — `ecoreward_bin/ecoreward_bin.ino`

Speaks the kiosk protocol (README "ESP32 protocol"). Compiles for **ESP32 Dev Module** with the
"HX711 Arduino Library" (bogde, 0.7.5) and "ESP32Servo". Not yet tested on the finished bin.

| Part | Pin / value | Setting in the sketch |
|---|---|---|
| HX711 DT / SCK | GPIO 16 / GPIO 4, factor 2280.0 | `LOADCELL_*_PIN`, `CALIBRATION_FACTOR` |
| Sorter servo (signal) | GPIO 13; bins 1/2/3 at 0° / 90° / 180°, home 90° | `SORTER_PIN`, `BIN_ANGLE`, `HOME_ANGLE` |
| Fill sensors: 3 × HC-SR04 | TRIG GPIO 5 / 19 / 22, ECHO GPIO 32 / 21 / 23 (bins 1/2/3); 30 cm = empty, 5 cm = full | `HAS_FILL_SENSORS` (false = +3 % estimate per drop), `TRIG_PIN`, `ECHO_PIN`, `BIN_EMPTY_CM`, `BIN_FULL_CM` |

Servo power from its own 5 V supply, grounds tied to the ESP32 GND.

**HC-SR04 ECHO is a 5 V signal; ESP32 inputs are 3.3 V.** Put a divider on each ECHO line (for example 1 kΩ
from ECHO to the GPIO and 2 kΩ from the GPIO to GND), or use a 3.3 V sensor such as the HC-SR04P / RCWL-1601.
Measure `BIN_EMPTY_CM` and `BIN_FULL_CM` in the real bins and set them in the sketch.

## Upload and test
1. **Close the kiosk** and anything else using COM3.
2. Arduino IDE: open `ecoreward_bin.ino`, Board **ESP32 Dev Module**, Port **COM3**, Upload (hold **BOOT** if it
   stops at "Connecting…").
3. Serial Monitor, 115200, "New Line": `READY`, `FILL:<b>,<r>,<s>` (the measured fill of the three bins), then `W:0.00` five times a second.
   `P` → `PONG`; `T` tares; put an item on the tray → `W:` shows its weight.
4. Type `1`: the servo turns to 0°, `OK:1` after 0.5 s; remove the item → `DONE:1:<g>`, a fresh `FILL:` reading, servo back
   to 90°. Leave the item → `JAM:1` after 4 s.
5. **Quit Arduino IDE completely** (it reopens the port by itself), then start the kiosk: `py -3.11 app.py`.

## Messages the kiosk shows

| "For staff" box | Cause | Fix |
|---|---|---|
| COM3 is in use by another program | Arduino IDE's Serial Monitor holds the port | quit Arduino IDE |
| Cannot open COM3 | ESP32 unplugged, wrong port, driver | replug; check Device Manager; `serial_port` in `config/kiosk_settings.json` |
| Connected to COM3, but the ESP32 does not answer | firmware not uploaded | step 2 |
| The serial library is missing for Python X.Y | PySerial not installed for that Python | `py -X.Y -m pip install pyserial` |
| Item stuck | `JAM:n` or no `DONE` in time | clear the tray, press **C** |
