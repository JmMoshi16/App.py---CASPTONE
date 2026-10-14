# ESP32 firmware — `ecoreward_bin/ecoreward_bin.ino`

Speaks the kiosk protocol (README "ESP32 protocol"). Compiles for **ESP32 Dev Module** with the
"HX711 Arduino Library" (bogde, 0.7.5) and "ESP32Servo". Not yet tested on the finished bin.

| Part | Pin / value | Setting in the sketch |
|---|---|---|
| HX711 DT / SCK | GPIO 16 / GPIO 4, factor 2280.0 | `LOADCELL_*_PIN`, `CALIBRATION_FACTOR` |
| Sorter servo (signal) | GPIO 13; bins 1/2/3 at 0° / 90° / 180°, home 90° | `SORTER_PIN`, `BIN_ANGLE`, `HOME_ANGLE` |
| Fill sensors (analog) | GPIO 34 / 35 / 32, 0–4095 → 0–100 % | `HAS_FILL_SENSORS` (false = +3 % estimate per drop), `FILL_PIN` |

Servo power from its own 5 V supply, grounds tied to the ESP32 GND.

## Upload and test
1. **Close the kiosk** and anything else using COM3.
2. Arduino IDE: open `ecoreward_bin.ino`, Board **ESP32 Dev Module**, Port **COM3**, Upload (hold **BOOT** if it
   stops at "Connecting…").
3. Serial Monitor, 115200, "New Line": `READY`, `FILL:0,0,0`, then `W:0.00` five times a second.
   `P` → `PONG`; `T` tares; put an item on the tray → `W:` shows its weight.
4. Type `1`: the servo turns to 0°, `OK:1` after 0.5 s; remove the item → `DONE:1:<g>`, `FILL:3,0,0`, servo back
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
