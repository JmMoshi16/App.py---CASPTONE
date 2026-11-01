# ESP32 firmware

| Folder | What it is |
|---|---|
| `ecoreward_bin_perfboard/` | **The kiosk firmware for the perfboard build (flash this one).** Chute + tray servos, scale, fill sensors. |
| `servo_test/` | Moves both servos only. It does **not** speak the kiosk protocol: with it flashed the kiosk shows "Connected to COM3, but the ESP32 does not answer". |
| `ecoreward_bin/` | The first single-servo sketch (GPIO 13), kept for reference. |

The perfboard sketch speaks the kiosk protocol (README "ESP32 protocol") and compiles for **ESP32 Dev Module**
with "HX711 Arduino Library" (bogde, 0.7.5) and "ESP32Servo".

| Part | Pin / value |
|---|---|
| HX711 DT / SCK | GPIO 16 / GPIO 4; factor 2280 (change with `K<grams>`, keep with `SAVE`) |
| Chute servo | GPIO 18; bins 1/2/3 at 0° / 90° / 180°, home 90° (`SET BIO|REC|RES|HOME <angle>`) |
| Tray servo | GPIO 19; flat 90°, tip 30° (`SET FLAT|TIP <angle>`) |
| Fill sensors: 3 × HC-SR04 | TRIG GPIO 25 / 26 / 27, ECHO GPIO 32 / 33 / 34 (bins 1/2/3); 30 cm = empty, 5 cm = full |

Servos from their own 5 V supply, grounds tied to the ESP32 GND.

**HC-SR04 ECHO is a 5 V signal; ESP32 inputs are 3.3 V.** Each ECHO line needs a divider (for example 1 kΩ
from ECHO to the GPIO and 2 kΩ from the GPIO to GND), or use a 3.3 V sensor such as the HC-SR04P / RCWL-1601.
Measure `BIN_EMPTY_CM` and `BIN_FULL_CM` in the real bins and set them in the sketch.

## What the sketch does
- `W:<g>` five times a second; `FILL:<b>,<r>,<s>` at start-up, after every drop and every 30 s while idle
  (`-1` = that sensor gave no reading: the kiosk shows "no reading", never 0 %).
- `1`/`2`/`3`: chute to the bin, `OK:<n>`, tray tips. Once the scale reads empty: tray flat, `DONE:<n>:<g>`.
  If the item is still on the tray after 4 s the tray levels and tips **once more**; still there after another
  4 s: `JAM:<n>`.
- No scale found: `ERR:HX711 not found` at start-up and on every sort request (it refuses to sort, because it
  could not tell that the item fell). The kiosk then shows "Out of service: the scale is not responding".

## Upload and test
1. **Close the kiosk** and anything else using COM3.
2. Arduino IDE: open `ecoreward_bin_perfboard/ecoreward_bin_perfboard.ino`, Board **ESP32 Dev Module**,
   Port **COM3**, Upload (hold **BOOT** if it stops at "Connecting…").
   Or from a terminal: `arduino-cli compile --fqbn esp32:esp32:esp32 firmware/ecoreward_bin_perfboard` then
   `arduino-cli upload -p COM3 --fqbn esp32:esp32:esp32 firmware/ecoreward_bin_perfboard`.
3. Serial Monitor, 115200, "New Line": `READY`, `FILL:...`, then `W:0.00` five times a second.
   `P` → `PONG`; `T` tares; `INFO` prints the saved settings; put an item on the tray → `W:` shows its weight.
4. Type `1` with an item on the tray: chute to 0°, `OK:1`, tray tips; the item falls → `DONE:1:<g>`.
5. **Quit Arduino IDE completely** (it reopens the port by itself), then start the kiosk: `py -3.11 app.py`.

## Messages the kiosk shows

| "For staff" box | Cause | Fix |
|---|---|---|
| COM3 is in use by another program | Arduino IDE's Serial Monitor holds the port | quit Arduino IDE |
| Cannot open COM3 | ESP32 unplugged, wrong port, driver | replug; check Device Manager; `serial_port` in `config/kiosk_settings.json` |
| Connected to COM3, but the ESP32 does not answer | wrong sketch flashed (e.g. `servo_test`) | step 2 with the perfboard sketch |
| Out of service: the scale is not responding | HX711 not wired / not powered | check DT 16, SCK 4, VCC, GND; reboot the ESP32 |
| The serial library is missing for Python X.Y | PySerial not installed for that Python | `py -X.Y -m pip install pyserial` |
| Item stuck | `JAM:n` or no `DONE` in time | clear the tray, press **C** |
